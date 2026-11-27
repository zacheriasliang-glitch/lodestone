"""Cumulative, evidence-linked practice in the existing Bezoar record store.

SEEDs retain candidate corrections; NACRE retains recurring observations. A
completed call is not learning. Resolution is a separately attributed review of
an actual response under declared conditions, bounded to that encounter. These
checks verify custody and declarations, not a reviewer's identity or truth.
There are no model calls, weight updates, permission grants or implicit actions.
Callers serialize prepare/dispatch/finish with their existing execution lock.
"""
from datetime import datetime, timezone
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import uuid

from .records import HANDLE, RecordStore, canonical
from lodestone_nurarihyon.portable_files import guarded_directory

PREFIX = "bezoar-learning-"
MAX_RECORDS = 2048
MAX_TEXT_BYTES = 32768
MAX_CONTEXT_BYTES = 131072
MAX_RESPONSE_BYTES = 65536
STATUS = ("COMPLETED", "FAILED", "ABSTAINED", "INTERRUPTED", "REFUSED")
VERDICTS = ("RESOLVED", "UNRESOLVED", "UNKNOWN", "REOPENED")
ATTRIBUTION = "DECLARED_EXTERNAL_REVIEW; names and hashes do not authenticate independence or semantic truth"
LESSON_SCHEMA = "bezoar-derived-lesson/v1"


def _require(ok, message):
    if not ok:
        raise ValueError(message)


def _sha(value):
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value)).hexdigest()


def _text(value, label, limit=MAX_TEXT_BYTES):
    _require(type(value) is str and bool(value.strip()) and len(value.encode("utf-8")) <= limit,
             label + " must be bounded nonempty text")
    return value


def _copy(value, limit=262144):
    raw = canonical(value)
    _require(len(raw) <= limit, "Learning record exceeds its explicit byte bound")
    return json.loads(raw)


def _fields(value, required, optional=()):
    _require(type(value) is dict and set(required) <= set(value)
             and set(value) <= set(required) | set(optional), "Learning fields mismatch")


def _strings(values, label, *, minimum=1, maximum=32):
    _require(type(values) is list and minimum <= len(values) <= maximum, label + " count exceeds bound")
    for value in values:
        _text(value, label, 8192)
    _require(len(values) == len(set(values)), label + " contains duplicates")
    return values


def _handles(values, label, minimum=1):
    _strings(values, label, minimum=minimum)
    _require(all(HANDLE.fullmatch(h) for h in values), label + " requires exact record handles")
    return values


def _operation(method):
    """Cache immutable bytes only during one synchronous, caller-locked operation.

    A later public call rereads disk, so tampering never inherits an old check.
    Nested validation of history should not reopen the same record exponentially.
    """
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with guarded_directory(self.workspace):
            outer = not hasattr(self, "_validated_records")
            if outer:
                self._validated_records = {}
                self._catalog_rows = None
                self._validated_values = {}
                self._checking_values = set()
            key = (method.__name__, args[0]) if method.__name__.startswith("read_") and len(args) == 1 and not kwargs else None
            try:
                if key is not None and key in self._validated_values:
                    return _copy(self._validated_values[key])
                if key is not None:
                    _require(key not in self._checking_values, "Recursive learning validation cycle")
                    self._checking_values.add(key)
                try:
                    result = method(self, *args, **kwargs)
                    if key is not None:
                        self._validated_values[key] = _copy(result)
                    return result
                finally:
                    if key is not None:
                        self._checking_values.discard(key)
            finally:
                if outer:
                    del self._validated_records
                    del self._catalog_rows
                    del self._validated_values
                    del self._checking_values
    return wrapped


class LearningMemory:
    def __init__(self, workspace, *, history_references=False):
        _require(type(history_references) is bool, "History representation selection must be explicit")
        self.history_references = history_references
        self.store = RecordStore(workspace)
        self.workspace = self.store.workspace
        if history_references:
            self._check_history_workspace()

    def _check_history_workspace(self):
        from .history_inventory import SCHEMA
        marker = self.workspace / "learning-history-format.json"
        _require(not marker.is_symlink() and marker.is_file(), "Reference history requires an explicitly isolated imported workspace")
        with marker.open("rb") as stream:
            raw = stream.read(4097)
        _require(len(raw) <= 4096 and json.loads(raw) == {
            "schema": "lodestone-learning-workspace-format/v1", "history_representation": SCHEMA,
            "scope": "EXPLICIT_SEPARATE_IMPORT", "authority": "NONE"},
            "Unsupported isolated history workspace capability")

    @classmethod
    def from_active(cls, active):
        """Explicit v2 creation boundary; old configurations keep v1 behaviour."""
        _require(type(active) is dict and active.get("required") is True,
                 "Required native learning configuration is unavailable")
        mode = active.get("schema")
        _require(mode in ("lodestone-learning-active/v1", "lodestone-learning-active/v2"),
                 "Unsupported native learning configuration")
        workspace = active.get("memory_workspace")
        _require(type(workspace) is str and Path(workspace).is_absolute(),
                 "Native learning workspace must be an absolute path")
        if mode == "lodestone-learning-active/v2":
            from .history_inventory import SCHEMA
            _require(active.get("history_representation") == SCHEMA,
                     "Unsupported explicit native history representation")
        else:
            _require("history_representation" not in active, "Legacy active configuration cannot enable reference history")
        return cls(workspace, history_references=mode == "lodestone-learning-active/v2")

    def _publish(self, kind, body, links=()):
        payload = {"schema": PREFIX + kind + "/v1", **_copy(body), "authority": "NONE"}
        result = self.store.publish("bezoar", "learning-" + kind, payload, links=sorted(set(links)))
        # RecordStore syncs the file before its atomic link. Persist that link
        # too: a completed learning write must survive loss of the host process.
        descriptor = os.open(self.store.path(result).parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        rows = getattr(self, "_catalog_rows", None)
        if rows is not None:
            # This operation already checked the complete catalogue. Validate
            # our exact new immutable record and its links, then extend that
            # snapshot instead of repeatedly reopening every unrelated record.
            record = self.store.relay("bezoar", result, owner="bezoar", kind="learning-" + kind)
            self.store.verify_links([result])
            entry = {"handle": result, "owner": "bezoar", "kind": record["kind"],
                     "links": record["links"], "status": "INTACT"}
            existing = [row for row in rows if row.get("handle") == result]
            _require(not existing or existing == [entry], "Published record differs from checked catalogue")
            if not existing:
                rows.append(entry)
                rows.sort(key=lambda row: (row.get("owner", ""), row.get("handle", "")))
        return result

    def _read(self, handle, kind):
        cached = getattr(self, "_validated_records", {}).get((handle, kind))
        if cached is not None:
            return _copy(cached)
        record = self.store.relay("bezoar", handle, owner="bezoar", kind="learning-" + kind)
        _require(record["payload"].get("schema") == PREFIX + kind + "/v1"
                 and record["payload"].get("authority") == "NONE", "Unsupported learning record")
        self.store.verify_links([handle])
        if hasattr(self, "_validated_records"):
            self._validated_records[(handle, kind)] = record
        return record

    def _catalog(self, kind):
        # The catalogue remains pointers into the native producer store.
        rows = getattr(self, "_catalog_rows", None)
        if rows is None:
            rows = self.store.catalog()["entries"]
            if hasattr(self, "_catalog_rows"):
                self._catalog_rows = rows
        _require(len(rows) <= MAX_RECORDS, "Learning catalogue exceeds explicit record bound")
        damaged = [r for r in rows if r.get("owner") == "bezoar" and r.get("status") != "INTACT"]
        _require(not damaged, "Bezoar contains damaged records; preserve and inspect evidence")
        return [r["handle"] for r in rows if r.get("owner") == "bezoar"
                and r.get("kind") == "learning-" + kind]

    def _evidence_body(self, payload):
        _fields(payload, ("source", "event_id", "observed_at", "text", "attribution"))
        value = _copy(payload)
        for name in value:
            _text(value[name], name, MAX_TEXT_BYTES if name == "text" else 2048)
        try:
            stamp = datetime.fromisoformat(value["observed_at"].replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("Evidence timestamp must be ISO-8601") from error
        _require(stamp.tzinfo is not None, "Evidence timestamp needs an explicit timezone")
        return value

    @_operation
    def read_evidence(self, handle):
        record = self._read(handle, "evidence")
        payload = record["payload"]
        body = {k: v for k, v in payload.items() if k not in ("schema", "authority")}
        self._evidence_body(body)
        _require(not record["links"], "Evidence cannot acquire undeclared links")
        return _copy(payload)

    @_operation
    def evidence(self, payload):
        body = self._evidence_body(payload)
        for handle in self._catalog("evidence"):
            old = self.read_evidence(handle)
            if (old["source"], old["event_id"]) == (body["source"], body["event_id"]):
                _require(all(old[k] == v for k, v in body.items()),
                         "Same source/event already retained with different content; retain a distinct correction event")
                return handle
        return self._publish("evidence", body)

    def _seed_body(self, payload):
        _fields(payload, ("id", "correction", "conditions", "frame_limits", "evidence_handles", "author"),
                ("supersedes", "derivation"))
        body = _copy(payload)
        for name in ("id", "correction", "author"):
            _text(body[name], name, MAX_TEXT_BYTES if name == "correction" else 512)
        for name in ("conditions", "frame_limits"):
            _strings(body[name], name)
        _handles(body["evidence_handles"], "seed evidence")
        for handle in body["evidence_handles"]:
            self.read_evidence(handle)
        if body.get("supersedes") is not None:
            self.read_seed(body["supersedes"])
        if "derivation" in body:
            derivation = body["derivation"]
            _fields(derivation, ("schema", "source_kind", "source_handle"))
            _require(derivation["schema"] == LESSON_SCHEMA, "Unsupported lesson derivation")
            evidence, expected = self._lesson_material(derivation["source_kind"], derivation["source_handle"])
            _require(expected is not None and len(body["evidence_handles"]) == 1,
                     "Lesson has no eligible retained source")
            retained = self.read_evidence(body["evidence_handles"][0])
            _require(retained == {"schema": PREFIX + "evidence/v1", "authority": "NONE", **evidence},
                     "Derived lesson evidence differs from its actual source")
            _require(body == {**expected, "evidence_handles": body["evidence_handles"]},
                     "Derived lesson differs from its actual source")
        return body

    @staticmethod
    def _seed_links(body):
        links = body["evidence_handles"] + ([body["supersedes"]] if body.get("supersedes") else [])
        if "derivation" in body:
            links += [body["derivation"]["source_handle"]]
        return sorted(set(links))

    @_operation
    def seed(self, payload):
        body = self._seed_body(payload)
        links = self._seed_links(body)
        return self._publish("seed", {**body, "kind": "SEED", "state": "CANDIDATE"}, links)

    @_operation
    def read_seed(self, handle):
        record = self._read(handle, "seed")
        payload = record["payload"]
        _require(payload.get("kind") == "SEED" and payload.get("state") == "CANDIDATE", "Invalid SEED state")
        body = {k: v for k, v in payload.items() if k not in ("schema", "authority", "kind", "state")}
        self._seed_body(body)
        _require(record["links"] == self._seed_links(body), "SEED links differ")
        return _copy(payload)

    def _lesson_material(self, source_kind, source_handle):
        """Recompute candidate advice from exact native sources; never infer cause."""
        _require(source_kind in ("outcome", "review", "preparation"), "Unsupported lesson source")
        _handles([source_handle], "lesson source")
        derivation = {"schema": LESSON_SCHEMA, "source_kind": source_kind, "source_handle": source_handle}
        limits = ["Candidate advice; no authority, demonstrated learning or inferred cause."]
        if source_kind == "preparation":
            retained = self.read_evidence(source_handle)
            data = json.loads(retained["text"])
            _fields(data, ("schema", "operation", "observation", "provider_called", "outcome_created"))
            _require(data["schema"] == "bezoar-preparation-failure/v1"
                     and data["provider_called"] is False and data["outcome_created"] is False,
                     "Invalid preparation failure evidence")
            _text(data["operation"], "operation", 512)
            _require(retained["source"] == "lodestone-preparation:" + data["operation"],
                     "Preparation failure source differs")
            _require(type(data["observation"]) in (dict, str), "Invalid preparation observation")
            _copy(data["observation"], 16384)
            evidence = {k: retained[k] for k in ("source", "event_id", "observed_at", "text", "attribution")}
            correction = ("Review this local failure before retrying; revise an evidenced input or condition. "
                          "The observation describes any actual tool execution, not a native provider run or inferred cause.")
            author = "Lodestone automatic capture"
            conditions = ["Same retained operation; reassess current input and conditions."]
        else:
            review = self.read_review(source_handle) if source_kind == "review" else None
            outcome_handle = review["outcome_handle"] if review else source_handle
            outcome = self.read_outcome(outcome_handle)
            if source_kind == "outcome" and (outcome["status"] == "COMPLETED"
                                              or self._is_recurrence_refusal(outcome)):
                return None, None
            if review and "correction" not in review["review"]:
                return None, None
            run = self._read(outcome["run_handle"], "run")["payload"]
            evidence = {"source": "bezoar-" + source_kind + ":" + source_handle,
                        "event_id": LESSON_SCHEMA, "observed_at": run["created_at"],
                        "text": canonical({"schema": LESSON_SCHEMA, "source_kind": source_kind,
                                           "source_handle": source_handle, "outcome_handle": outcome_handle,
                                           "outcome_status": outcome["status"],
                                           "response_sha256": outcome["response_sha256"],
                                           "observation_sha256": _sha(outcome["observation"]),
                                           "correction": review["review"]["correction"] if review else None,
                                           "time_basis": "Source run preparation time; outcome/review time is not inferred."}).decode(),
                        "attribution": "Automatic source-bound capture; the linked native source remains complete."}
            correction = (review["review"]["correction"] if review else
                          "Review this attempt and its corrections before retrying; revise an evidenced cause or approach. "
                          "Status alone does not identify a cause.")
            author = (review["review"]["reviewer"] if review else
                      "Lodestone automatic capture")
            conditions = ["Same retained practice pattern; reassess current conditions."]
        return evidence, {"id": "retained-" + source_kind,
                          "correction": correction, "conditions": conditions, "frame_limits": limits,
                          "author": author, "derivation": derivation}

    def _is_recurrence_refusal(self, outcome):
        """A gate-only refusal retains its outcome without inventing another failure."""
        observation = outcome["observation"]
        if (outcome["status"] != "REFUSED" or type(observation) is not dict
                or observation.get("stage") != "CONSCIENCE_ADMISSION"
                or observation.get("provider_called") is not False
                or "recurrence_source_outcome" not in observation):
            return False
        try:
            receipt = self.store.relay("bezoar", observation["receipt_handle"],
                                       owner="nurarihyon", kind="learning-refusal")
            self.store.verify_links([observation["receipt_handle"]])
            body = receipt["payload"]
            recurrence = body["assessment"]["recurrence"]
            source = self.read_outcome(observation["recurrence_source_outcome"])
            failure = self.store.relay("bezoar", recurrence["failure_handle"],
                                       owner="nurarihyon", kind="learning-mechanical-failure")
            self.store.verify_links([recurrence["failure_handle"]])
            fact = failure["payload"]
            dispatch = self.store.relay("bezoar", fact["dispatch_handle"],
                                        owner="nurarihyon", kind="learning-dispatch")
            response = self.store.relay("bezoar", fact["response_handle"],
                                        owner="nurarihyon", kind="learning-response")
            from lodestone_nurarihyon.learning_recurrence import checked_failure
            measured = checked_failure(fact["failure"], dispatch["payload"].get("execution_binding"))
            return (body.get("schema") == "nurarihyon-learning-refusal/v1"
                    and body.get("stage") == "CONSCIENCE_ADMISSION"
                    and body.get("provider_called") is False
                    and body.get("run_handle") == outcome["run_handle"]
                    and recurrence.get("schema") == "loom-recurrence-assessment/v1"
                    and recurrence.get("verdict") == "REFUSE"
                    and recurrence.get("source_outcome") == observation["recurrence_source_outcome"]
                    and fact.get("schema") == "loom-mechanical-failure/v1"
                    and fact.get("authority") == "NONE"
                    and failure["links"] == sorted([fact["dispatch_handle"], fact["response_handle"]])
                    and measured is not None
                    and measured == response["payload"].get("mechanical_failure")
                    and source["status"] in ("FAILED", "INTERRUPTED")
                    and response["payload"].get("status") == source["status"]
                    and dispatch["payload"].get("schema") == "nurarihyon-learning-dispatch/v1"
                    and response["payload"].get("schema") == "nurarihyon-learning-response/v1"
                    and dispatch["payload"].get("run_handle") == source["run_handle"]
                    and response["payload"].get("dispatch_handle") == fact["dispatch_handle"]
                    and response["payload"].get("run_handle") == source["run_handle"]
                    and source["observation"].get("provider_receipt") == fact["response_handle"])
        except (ValueError, OSError, KeyError, TypeError, AttributeError):
            # A caller-supplied assertion is not sufficient to suppress evidence.
            return False

    def _retain_lesson(self, source_kind, source_handle):
        evidence, body = self._lesson_material(source_kind, source_handle)
        if body is None:
            return None
        for handle in self._catalog("seed"):
            existing = self._read(handle, "seed")["payload"].get("derivation")
            if existing == body["derivation"]:
                seed = self.read_seed(handle)  # Recomputes the exact source binding.
                return {"source_kind": source_kind, "source_handle": source_handle,
                        "evidence_handle": seed["evidence_handles"][0], "seed_handle": handle}
        evidence_handle = source_handle if source_kind == "preparation" else self.evidence(evidence)
        seed_handle = self.seed({**body, "evidence_handles": [evidence_handle]})
        return {"source_kind": source_kind, "source_handle": source_handle,
                "evidence_handle": evidence_handle, "seed_handle": seed_handle}

    @_operation
    def reconcile_lessons(self):
        """Repair derivation after a crash, without rewriting any source or running a provider."""
        results = []
        for kind in ("outcome", "review"):
            for handle in self._catalog(kind):
                lesson = self._retain_lesson(kind, handle)
                if lesson is not None:
                    results.append(lesson)
        for handle in self._catalog("evidence"):
            evidence = self.read_evidence(handle)
            if evidence["source"].startswith("lodestone-preparation:"):
                results.append(self._retain_lesson("preparation", handle))
        return {"schema": "bezoar-lesson-reconciliation/v1", "lessons": results,
                "provider_calls": 0, "authority": "NONE"}

    @_operation
    def retain_preparation_failure(self, operation, source_event, observation, *, observed_at=None):
        """Capture a local failure outside a prepared provider run; retain actual tool observations."""
        _text(operation, "operation", 512)
        _text(source_event, "source event", 2048)
        _require(type(observation) in (dict, str), "Preparation observation must be text or an object")
        observation = _copy(observation, 16384)
        source = "lodestone-preparation:" + operation
        if observed_at is None:
            for handle in self._catalog("evidence"):
                old = self.read_evidence(handle)
                if (old["source"], old["event_id"]) == (source, source_event):
                    observed_at = old["observed_at"]
                    break
        evidence = self.evidence({"source": source, "event_id": source_event,
            "observed_at": observed_at or datetime.now(timezone.utc).isoformat(),
            "text": canonical({"schema": "bezoar-preparation-failure/v1", "operation": operation,
                               "observation": observation, "provider_called": False,
                               "outcome_created": False}).decode(),
            "attribution": "Caller-observed local failure outside a prepared provider run; no native provider invocation or inferred cause."})
        lesson = self._retain_lesson("preparation", evidence)
        return {**lesson, "authority": "NONE", "outcome_created": False, "provider_called": False}

    def _lessons_for(self, outcome_handle, review_handles):
        sources = {("outcome", outcome_handle)} | {("review", handle) for handle in review_handles}
        result = []
        for handle in self._catalog("seed"):
            raw = self._read(handle, "seed")["payload"]
            source = raw.get("derivation", {})
            if (source.get("source_kind"), source.get("source_handle")) in sources:
                result.append({"handle": handle, "record": self.read_seed(handle)})
        return sorted(result, key=lambda row: row["handle"])

    @_operation
    def lessons_for_outcome(self, outcome_handle):
        """Read exact supplemental candidates without selecting or promoting them."""
        self.read_outcome(outcome_handle)
        return self._lessons_for(outcome_handle, self._reviews(outcome_handle))

    @_operation
    def preparation_lessons(self, operation):
        """Read exact candidates for this operation, without changing selection or records."""
        _text(operation, "operation", 512)
        result = []
        for handle in self._catalog("seed"):
            source = self._read(handle, "seed")["payload"].get("derivation", {})
            if source.get("source_kind") == "preparation":
                evidence = self.read_evidence(source["source_handle"])
                if evidence["source"] == "lodestone-preparation:" + operation:
                    result.append({"handle": handle, "record": self.read_seed(handle)})
        return sorted(result, key=lambda row: row["handle"])

    def _nacre_body(self, payload):
        _fields(payload, ("id", "pattern", "conditions", "evidence_handles", "seed_handles", "author"))
        body = _copy(payload)
        for name in ("id", "pattern", "author"):
            _text(body[name], name, MAX_TEXT_BYTES if name == "pattern" else 512)
        _strings(body["conditions"], "pattern conditions")
        _handles(body["seed_handles"], "pattern seeds")
        _handles(body["evidence_handles"], "pattern observations", minimum=2)
        events = []
        for handle in body["evidence_handles"]:
            evidence = self.read_evidence(handle)
            events.append((evidence["source"], evidence["event_id"]))
        _require(len(events) == len(set(events)), "Recurrence requires distinct source/event observations")
        for handle in body["seed_handles"]:
            self.read_seed(handle)
        return body

    @_operation
    def nacre(self, payload):
        body = self._nacre_body(payload)
        return self._publish("nacre", {**body, "kind": "NACRE", "state": "LAYERED"},
                             body["evidence_handles"] + body["seed_handles"])

    @_operation
    def read_nacre(self, handle):
        record = self._read(handle, "nacre")
        payload = record["payload"]
        _require(payload.get("kind") == "NACRE" and payload.get("state") == "LAYERED", "Invalid NACRE state")
        body = {k: v for k, v in payload.items() if k not in ("schema", "authority", "kind", "state")}
        self._nacre_body(body)
        _require(record["links"] == sorted(set(body["evidence_handles"] + body["seed_handles"])), "NACRE links differ")
        return _copy(payload)

    def _lineage(self, handle):
        lineage = []
        while handle is not None:
            _require(handle not in lineage and len(lineage) < 32, "SEED lineage exceeds bound or repeats")
            lineage.append(handle)
            handle = self.read_seed(handle).get("supersedes")
        return lineage

    @_operation
    def resolve_seeds(self, handles):
        _handles(handles, "selected seeds")
        available = {h: self.read_seed(h) for h in self._catalog("seed")}
        result = []
        for original in handles:
            self.read_seed(original)
            current, seen = original, set()
            while True:
                _require(current not in seen, "SEED supersession cycle")
                seen.add(current)
                children = [h for h, row in available.items() if row.get("supersedes") == current]
                _require(len(children) <= 1, "Conflicting correction branches require an explicit branch selection")
                if not children:
                    break
                current = children[0]
            if current not in result:
                result.append(current)
        return result

    @_operation
    def practice_candidates(self, handles):
        """Inspect retained candidates and pairing availability without practice.

        Readiness means only that a candidate has a valid retained NACRE. It
        does not assess current conditions, admit a request, or establish that
        the advice has been learned. Malformed native records fail the entire
        read instead of disappearing from the catalogue.
        """
        _handles(handles, "configured seeds")
        configured = list(handles)
        resolved = self.resolve_seeds(configured)
        candidates = []
        for handle in self._catalog("seed"):
            seed = self.read_seed(handle)
            current = self.resolve_seeds([handle])[0]
            error = None
            try:
                self._pairs([current], None)
            except ValueError as exc:
                if str(exc) != "Every practice SEED must use a matching retained NACRE":
                    raise
                error = str(exc)
            if current != handle:
                error = "Superseded by " + current + "; select that current candidate to inspect its advice."
            candidates.append({"handle": handle, "id": seed["id"], "state": seed["state"],
                               "resolved_handle": current,
                               "conditions": _copy(seed["conditions"]),
                               "frame_limits": _copy(seed["frame_limits"]),
                               "correction": seed["correction"],
                               "ready": error is None, "error": error})
        return {"schema": "loom-practice-candidates/v1",
                "configured_seed_handles": configured,
                "resolved_seed_handles": resolved, "candidates": candidates,
                "scope": "Pairing availability only; current applicability, admission and learned benefit are not established.",
                "authority": "NONE", "provider_called": False,
                "semantic_learning": "NOT_ESTABLISHED"}

    def _pairs(self, seeds, applicability):
        if applicability is None:
            nacre_handles = self._catalog("nacre")
        else:
            _require(type(applicability) is list and 1 <= len(applicability) <= 16,
                     "Bounded applicability list required")
            for supplied in applicability:
                _fields(supplied, ("seed_handle", "nacre_handle", "reason", "conditions"))
                _handles([supplied["seed_handle"]], "applicability seed")
                _handles([supplied["nacre_handle"]], "applicability pattern")
            # Saved practice binds exact patterns. A later pattern may refer to
            # a lesson derived from that very practice; validating unrelated
            # catalogue entries here would recurse through its source review.
            # Fresh discovery still examines every retained pattern above.
            nacre_handles = sorted({row["nacre_handle"] for row in applicability})
        nacres = {h: self.read_nacre(h) for h in nacre_handles}
        matched = {(s, n) for s in seeds for n, row in nacres.items()
                   if set(self._lineage(s)) & set(row["seed_handles"])}
        _require(matched and all(any(s == seed for s, _ in matched) for seed in seeds),
                 "Every practice SEED must use a matching retained NACRE")
        if applicability is None:
            pairs = [{"seed_handle": s, "nacre_handle": n,
                      "reason": "Caller selected this seed for practice against its explicitly linked recurring pattern.",
                      "conditions": self.read_seed(s)["conditions"], "status": "DECLARED_FOR_THIS_PRACTICE"}
                     for s, n in sorted(matched)]
        else:
            pairs = []
            for supplied in applicability:
                pair = (supplied["seed_handle"], supplied["nacre_handle"])
                _require(pair in matched, "Applicability must bind the selected SEED to its actual NACRE")
                _text(supplied["reason"], "applicability reason", 8192)
                _require(supplied["conditions"] == self.read_seed(pair[0])["conditions"],
                         "Applicability must carry all retained seed conditions")
                pairs.append({**_copy(supplied), "status": "DECLARED_FOR_THIS_PRACTICE"})
            _require({p["seed_handle"] for p in pairs} == set(seeds), "Every selected seed needs applicability")
        _require(len(pairs) <= 16 and len({(p["seed_handle"], p["nacre_handle"]) for p in pairs}) == len(pairs),
                 "Repeated or excessive learning pairs")
        return pairs

    def _history(self, nacres):
        history = []
        for handle in self._catalog("run"):
            prepared = self.read_prepared(handle)
            if not set(nacres) & set(prepared["binding"]["nacre_handles"]):
                continue
            outcome_handle = self.outcome_for(prepared)
            row = {"run_handle": handle, "run_id": prepared["run_id"],
                   "encounter": prepared["binding"]["encounter"], "purpose": prepared["binding"]["purpose"],
                   "reader": prepared["binding"]["reader"]}
            if outcome_handle is None:
                row.update(status="INCOMPLETE", outcome=None, reviews=[])
            else:
                outcome = self.read_outcome(outcome_handle)
                reviews = self._reviews(outcome_handle)
                row.update(status=outcome["status"], outcome_handle=outcome_handle, outcome=outcome,
                           reviews=[{"handle": h, "review": self.read_review(h)} for h in reviews],
                           lessons=self._lessons_for(outcome_handle, reviews))
            history.append(row)
        return sorted(history, key=lambda row: (self._read(row["run_handle"], "run")["payload"]["created_at"], row["run_handle"]))

    def _history_reference_rows(self, nacres, *, reference_budget=256, exclude_run=None):
        """Select exact native identities without collecting historical bodies."""
        from .history_inventory import MAX_REFERENCES
        _require(type(reference_budget) is int and 0 <= reference_budget <= MAX_REFERENCES,
                 "History references exceed remaining native provenance budget")
        rows = []
        reference_count = 0
        for handle in self._catalog("run"):
            if handle == exclude_run:
                continue
            prepared = self.read_prepared(handle)
            if not set(nacres) & set(prepared["binding"]["nacre_handles"]):
                continue
            outcome = self.outcome_for(prepared)
            reviews = self._reviews(outcome) if outcome is not None else []
            reference_count += 1 + int(outcome is not None) + len(reviews)
            _require(reference_count <= reference_budget, "History inventory exceeds existing provenance bound; archive remains unchanged")
            lessons = self._lessons_for(outcome, reviews) if outcome is not None else []
            reference_count += len(lessons)
            _require(reference_count <= reference_budget, "History inventory exceeds existing provenance bound; archive remains unchanged")
            rows.append({"run_handle": handle, "outcome_handle": outcome,
                         "review_handles": reviews,
                         "lesson_handles": [row["handle"] for row in lessons]})
        return sorted(rows, key=lambda row: (self._read(row["run_handle"], "run")["payload"]["created_at"], row["run_handle"]))

    def _history_reference_sources(self, snapshot, *, run_handle, created_at, nacres):
        from .history_inventory import validate_sources, MAX_REFERENCES
        teaching_count = sum(len(snapshot[key]) for key in ("seeds", "nacres", "evidence"))
        def read_run(handle):
            self.read_prepared(handle)
            return self._read(handle, "run")["payload"]
        return validate_sources(snapshot["history"], current_run_handle=run_handle,
            current_created_at=created_at, selected_nacres=nacres,
            read_run=read_run, read_outcome=self.read_outcome, read_review=self.read_review,
            read_seed=self.read_seed, reference_budget=MAX_REFERENCES - teaching_count)

    def _history_row_from_reference(self, row):
        prepared = self.read_prepared(row["run_handle"])
        result = {"run_handle": row["run_handle"], "run_id": prepared["run_id"],
                  **{k: prepared["binding"][k] for k in ("encounter", "purpose", "reader")}}
        if row["outcome_handle"] is None:
            result.update(status="INCOMPLETE", outcome=None, reviews=[])
        else:
            outcome = self.read_outcome(row["outcome_handle"])
            result.update(status=outcome["status"], outcome_handle=row["outcome_handle"], outcome=outcome,
                reviews=[{"handle": h, "review": self.read_review(h)} for h in row["review_handles"]],
                lessons=[{"handle": h, "record": self.read_seed(h)} for h in row["lesson_handles"]])
        return result

    @_operation
    def history_page(self, prepared, *, start=0, limit=1):
        """Bounded complete inspection, distinct from the model's declared view."""
        checked = self.validate_prepared(prepared)
        _require(type(start) is int and start >= 0 and type(limit) is int and 1 <= limit <= 32,
                 "History page requires a nonnegative start and limit1..32")
        snapshot = json.loads(checked["context"])
        history = snapshot["history"]
        if snapshot["schema"] == PREFIX + "context/v2":
            rows = [self._history_row_from_reference(row) for row in history["rows"][start:start + limit]]
            count = history["row_count"]
        else:
            rows, count = history[start:start + limit], len(history)
        _require(len(canonical(rows)) <= MAX_CONTEXT_BYTES,
                 "Complete history page exceeds bound; request fewer rows or inspect exact native handles")
        return {"schema": PREFIX + "history-page/v1", "run_handle": checked["run_handle"],
                "context_sha256": checked["binding"]["context_sha256"],
                "start": start, "total_rows": count, "rows": rows, "authority": "NONE"}

    @_operation
    def prepare(self, seed_handles, *, encounter, purpose, reader, run_id=None, applicability=None):
        if getattr(self, "history_references", False):
            self._check_history_workspace()
        for name, value in (("encounter", encounter), ("purpose", purpose), ("reader", reader)):
            _text(value, name, 32768 if name == "encounter" else 4096)
        run_id = run_id or uuid.uuid4().hex
        _text(run_id, "run id", 256)
        existing = [h for h in self._catalog("run") if self._read(h, "run")["payload"]["run_id"] == run_id]
        _require(len(existing) <= 1, "Conflicting saved run identifiers")
        if existing:
            prepared = self.read_prepared(existing[0])
            binding = prepared["binding"]
            _require((binding["encounter"], binding["purpose"], binding["reader"]) == (encounter, purpose, reader)
                     and binding["seed_handles"] == self.resolve_seeds(seed_handles), "Run id already binds a different request")
            if applicability is not None:
                _require(self._pairs(binding["seed_handles"], applicability) == json.loads(prepared["context"])["pairs"],
                         "Run id already binds a different applicability decision")
            return prepared
        seeds = self.resolve_seeds(seed_handles)
        pairs = self._pairs(seeds, applicability)
        nacres = sorted({p["nacre_handle"] for p in pairs})
        history = ([] if getattr(self, "history_references", False)
                   else self._history(nacres))
        evidence_handles = set()
        for h in seeds:
            evidence_handles.update(self.read_seed(h)["evidence_handles"])
        for h in nacres:
            evidence_handles.update(self.read_nacre(h)["evidence_handles"])
        snapshot = {"schema": PREFIX + "context/v1", "encounter": encounter, "purpose": purpose,
                    "pairs": pairs, "seeds": [{"handle": h, "record": self.read_seed(h)} for h in seeds],
                    "nacres": [{"handle": h, "record": self.read_nacre(h)} for h in nacres],
                    "evidence": [{"handle": h, "record": self.read_evidence(h)} for h in sorted(evidence_handles)],
                    "history": history, "authority": "NONE", "use": "Retained candidate advice and actual prior practice; reassess conditions and frame limits. No record status proves learning."}
        if getattr(self, "history_references", False):
            from .history_inventory import freeze_rows, MAX_REFERENCES
            teaching_count = sum(len(snapshot[key]) for key in ("seeds", "nacres", "evidence"))
            snapshot["schema"] = PREFIX + "context/v2"
            history = self._history_reference_rows(nacres, reference_budget=MAX_REFERENCES - teaching_count)
            snapshot["history"] = freeze_rows(history, reference_budget=MAX_REFERENCES - teaching_count)
        context = canonical(snapshot).decode("utf-8")
        _require(len(context.encode()) <= MAX_CONTEXT_BYTES, "Complete learning context exceeds bound; no history or correction was truncated")
        binding = {"encounter": encounter, "purpose": purpose, "reader": reader, "seed_handles": seeds,
                   "nacre_handles": nacres, "context_sha256": _sha(context.encode())}
        # History handles are embedded and validated on read; direct links stay
        # bounded to the candidate/pattern pair, whose full evidence is retained.
        run = self._publish("run", {"run_id": run_id, "created_at": datetime.now(timezone.utc).isoformat(),
                                   "context": context, "binding": binding}, seeds + nacres)
        return self.read_prepared(run)

    @_operation
    def read_prepared(self, run_handle):
        record = self._read(run_handle, "run")
        payload = record["payload"]
        _fields(payload, ("schema", "run_id", "created_at", "context", "binding", "authority"))
        binding = payload["binding"]
        _fields(binding, ("encounter", "purpose", "reader", "seed_handles", "nacre_handles", "context_sha256"))
        for name in ("encounter", "purpose", "reader"):
            _text(binding[name], name, 32768 if name == "encounter" else 4096)
        _handles(binding["seed_handles"], "bound seeds")
        _handles(binding["nacre_handles"], "bound patterns")
        context = _text(payload["context"], "retained context", MAX_CONTEXT_BYTES)
        _require(_sha(context.encode()) == binding["context_sha256"], "Retained context digest differs")
        snapshot = json.loads(context)
        _require(canonical(snapshot).decode() == context, "Retained context is not canonical")
        _fields(snapshot, ("schema", "encounter", "purpose", "pairs", "seeds", "nacres", "evidence", "history", "authority", "use"))
        _require(snapshot["schema"] in (PREFIX + "context/v1", PREFIX + "context/v2") and snapshot["authority"] == "NONE"
                 and snapshot["encounter"] == binding["encounter"] and snapshot["purpose"] == binding["purpose"],
                 "Context encounter/purpose differs")
        for key, loader in (("seeds", self.read_seed), ("nacres", self.read_nacre), ("evidence", self.read_evidence)):
            for row in snapshot[key]:
                _fields(row, ("handle", "record"))
                _require(row["record"] == loader(row["handle"]), "Retained context differs from native record")
        _require([r["handle"] for r in snapshot["seeds"]] == binding["seed_handles"]
                 and [r["handle"] for r in snapshot["nacres"]] == binding["nacre_handles"], "Retained context selection differs")
        pairs = snapshot["pairs"]
        _require(type(pairs) is list and 1 <= len(pairs) <= 16, "Saved learning pairs exceed bounds")
        for pair in pairs:
            _fields(pair, ("seed_handle", "nacre_handle", "reason", "conditions", "status"))
            _require(pair["status"] == "DECLARED_FOR_THIS_PRACTICE", "Applicability declaration was promoted")
        rebuilt = self._pairs(binding["seed_handles"], [{k: v for k, v in pair.items() if k != "status"} for pair in pairs])
        _require(rebuilt == pairs and {p["nacre_handle"] for p in pairs} == set(binding["nacre_handles"]), "Saved pair mapping differs")
        expected_evidence = {h for row in snapshot["seeds"] + snapshot["nacres"] for h in row["record"]["evidence_handles"]}
        _require([r["handle"] for r in snapshot["evidence"]] == sorted(expected_evidence), "Context evidence selection differs")
        _require(record["links"] == sorted(set(binding["seed_handles"] + binding["nacre_handles"])), "Run dependency links differ")
        seen = set()
        if snapshot["schema"] == PREFIX + "context/v2":
            self._history_reference_sources(snapshot, run_handle=run_handle,
                created_at=payload["created_at"], nacres=binding["nacre_handles"])
            history_rows = []  # Source relationships were checked without expanding all bodies.
        else:
            history_rows = snapshot["history"]
        for row in history_rows:
            _fields(row, ("run_handle", "run_id", "encounter", "purpose", "reader", "status", "outcome", "reviews"),
                    ("outcome_handle", "lessons"))
            prior = self._read(row["run_handle"], "run")["payload"]
            _require(row["run_handle"] not in seen and row["run_handle"] != run_handle
                     and prior["created_at"] < payload["created_at"]
                     and row["run_id"] == prior["run_id"]
                     and all(row[k] == prior["binding"][k] for k in ("encounter", "purpose", "reader"))
                     and set(prior["binding"]["nacre_handles"]) & set(binding["nacre_handles"]), "Unrelated or repeated prior practice")
            seen.add(row["run_handle"])
            if row["outcome"] is not None:
                _require(row["outcome"] == self.read_outcome(row["outcome_handle"]), "Prior actual outcome changed")
                _require(row["outcome"]["run_handle"] == row["run_handle"] and row["status"] == row["outcome"]["status"], "History outcome belongs to another run")
                for review in row["reviews"]:
                    _require(review["review"] == self.read_review(review["handle"]), "Prior correction review changed")
                    _require(review["review"]["outcome_handle"] == row["outcome_handle"], "History review belongs to another outcome")
                if "lessons" in row:
                    _require(type(row["lessons"]) is list, "Historical lessons must be a list")
                    sources = {("outcome", row["outcome_handle"])} | {
                        ("review", item["handle"]) for item in row["reviews"]}
                    lesson_handles = []
                    for lesson in row["lessons"]:
                        _fields(lesson, ("handle", "record"))
                        _require(lesson["record"] == self.read_seed(lesson["handle"]),
                                 "Historical lesson differs from its exact native record")
                        derivation = lesson["record"].get("derivation", {})
                        _require((derivation.get("source_kind"), derivation.get("source_handle")) in sources,
                                 "Historical lesson does not bind this outcome or correction")
                        lesson_handles.append(lesson["handle"])
                    _require(lesson_handles == sorted(set(lesson_handles)), "Repeated or unordered historical lessons")
            else:
                _require(row["status"] == "INCOMPLETE" and row["reviews"] == [] and "outcome_handle" not in row
                         and row.get("lessons", []) == [],
                         "Incomplete practice cannot claim an outcome")
        return {"schema": PREFIX + "prepared/v1", "run_id": payload["run_id"], "run_handle": run_handle,
                "context": context, "binding": _copy(binding)}

    @_operation
    def validate_prepared(self, prepared):
        _require(type(prepared) is dict and type(prepared.get("run_handle")) is str, "Saved learning run required")
        expected = self.read_prepared(prepared["run_handle"])
        _require(canonical(prepared) == canonical(expected), "Prepared learning snapshot differs from retained run")
        return expected

    @_operation
    def validate_for_dispatch(self, prepared):
        """Require the latest actual outcomes/corrections at the reader boundary.

        Historical snapshots remain readable. A new terminal response or review
        after preparation requires fresh context instead of silently replaying
        an obsolete lesson. New still-pending preparations do not invalidate
        each other or create a cycle in admission.
        """
        checked = self.validate_prepared(prepared)
        selected = checked["binding"]["seed_handles"]
        _require(self.resolve_seeds(selected) == selected, "A newer SEED correction requires fresh practice preparation")
        _require(self.outcome_for(checked) is None, "A terminal learning run cannot be dispatched again")
        snapshot = json.loads(checked["context"])
        if snapshot["schema"] == PREFIX + "context/v2":
            from .history_inventory import freeze_rows, terminal_signature, MAX_REFERENCES
            previous_signature = terminal_signature(snapshot["history"])
            budget = MAX_REFERENCES - sum(len(snapshot[key]) for key in ("seeds", "nacres", "evidence"))
            current_rows = self._history_reference_rows(checked["binding"]["nacre_handles"],
                reference_budget=budget, exclude_run=checked["run_handle"])
            current_signature = terminal_signature(freeze_rows(current_rows, reference_budget=budget))
            _require(previous_signature == current_signature,
                     "A newer actual outcome or correction review requires fresh practice preparation")
            return checked
        previous = snapshot["history"]
        current = self._history(checked["binding"]["nacre_handles"])

        def signature(rows):
            return {row["run_handle"]: {"outcome": row["outcome_handle"],
                                       "reviews": sorted(r["handle"] for r in row["reviews"]),
                                       "lessons": [r["handle"] for r in row.get("lessons", [])]}
                    for row in rows if row["outcome"] is not None and row["run_handle"] != checked["run_handle"]}

        _require(signature(previous) == signature(current),
                 "A newer actual outcome or correction review requires fresh practice preparation")
        return checked

    @_operation
    def outcome_for(self, prepared):
        run = self.validate_prepared(prepared)["run_handle"]
        found = [h for h in self._catalog("outcome") if self._read(h, "outcome")["payload"].get("run_handle") == run]
        _require(len(found) <= 1, "Conflicting terminal outcomes for one learning run")
        if found:
            self.read_outcome(found[0])
        return found[0] if found else None

    @_operation
    def pending(self):
        result = []
        for handle in self._catalog("run"):
            prepared = self.read_prepared(handle)
            if self.outcome_for(prepared) is None:
                result.append(prepared)
        return result

    @_operation
    def finish(self, prepared, *, response, status, observation, review=None):
        prepared = self.validate_prepared(prepared)
        _require(status in STATUS, "Unsupported learning outcome status")
        _require(response is None or type(response) is str and len(response.encode()) <= MAX_RESPONSE_BYTES,
                 "Response must be text or null within explicit bound")
        _require(status != "COMPLETED" or response is not None and bool(response.strip()), "Completed practice requires actual response")
        _require(type(observation) in (str, dict), "Retain an actual observation or error description")
        observation = _copy(observation, 32768)
        body = {"run_handle": prepared["run_handle"], "run_id": prepared["run_id"], "status": status,
                "response": response, "response_sha256": _sha(response.encode()) if response is not None else None,
                "observation": observation, "learning": "NOT_DEMONSTRATED_BY_COMPLETION"}
        existing = self.outcome_for(prepared)
        if existing:
            old = self.read_outcome(existing)
            _require(all(old[k] == v for k, v in body.items()), "Actual terminal outcome cannot be overwritten")
            handle = existing
        else:
            handle = self._publish("outcome", body, [prepared["run_handle"]])
        # The source is durable before any derived record. Reconciliation can
        # complete this idempotently if the process stops between those writes.
        self._retain_lesson("outcome", handle)
        if review is not None:
            self.review_outcome(handle, review)
        return handle

    @_operation
    def read_outcome(self, handle):
        record = self._read(handle, "outcome")
        body = record["payload"]
        _fields(body, ("schema", "authority", "run_handle", "run_id", "status", "response", "response_sha256", "observation", "learning"))
        run = self._read(body["run_handle"], "run")["payload"]
        _require(record["links"] == [body["run_handle"]] and body["run_id"] == run["run_id"]
                 and body["status"] in STATUS and body["learning"] == "NOT_DEMONSTRATED_BY_COMPLETION", "Invalid outcome binding")
        response = body["response"]
        _require(response is None or type(response) is str and len(response.encode()) <= MAX_RESPONSE_BYTES, "Invalid saved response")
        _require(body["response_sha256"] == (_sha(response.encode()) if response is not None else None), "Actual response digest differs")
        _require(body["status"] != "COMPLETED" or bool(response and response.strip()), "Completed outcome has no actual response")
        return _copy(body)

    def _review_body(self, outcome_handle, supplied):
        _fields(supplied, ("reviewer", "verdict", "response_sha256", "reason", "evidence_handles", "checks"), ("correction",))
        review = _copy(supplied)
        outcome = self.read_outcome(outcome_handle)
        prepared = self.read_prepared(outcome["run_handle"])
        _text(review["reviewer"], "reviewer", 512)
        _text(review["reason"], "review reason", 8192)
        if "correction" in review:
            _text(review["correction"], "review correction", 8192)
        authors = {self.read_seed(h)["author"].strip() for h in prepared["binding"]["seed_handles"]}
        if review["verdict"] == "RESOLVED":
            _require(review["reviewer"].strip() != prepared["binding"]["reader"].strip()
                     and review["reviewer"].strip() not in authors, "Learning resolution requires separately attributed review, not self-grading")
        _require(review["verdict"] in VERDICTS and review["response_sha256"] == outcome["response_sha256"], "Review must bind actual response")
        _handles(review["evidence_handles"], "review evidence")
        for h in review["evidence_handles"]:
            self.read_evidence(h)
        pairs = json.loads(prepared["context"])["pairs"]
        expected = {(p["seed_handle"], p["nacre_handle"]) for p in pairs}
        checks = review["checks"]
        _require(type(checks) is list and len(checks) == len(expected), "Review every selected seed/NACRE pair")
        seen = set()
        for check in checks:
            _fields(check, ("seed_handle", "nacre_handle", "conditions_met", "pattern_resolved", "regression_observed", "reason", "evidence_handles"))
            pair = (check["seed_handle"], check["nacre_handle"])
            _require(pair in expected and pair not in seen, "Review has unrelated or duplicate pair")
            seen.add(pair)
            _text(check["reason"], "pair review reason", 8192)
            _require(all(type(check[k]) is bool for k in ("conditions_met", "pattern_resolved", "regression_observed")), "Review condition results must be explicit booleans")
            _handles(check["evidence_handles"], "pair evidence")
            _require(set(check["evidence_handles"]) <= set(review["evidence_handles"]), "Pair checks must cite selected review evidence")
        if review["verdict"] == "RESOLVED":
            _require(outcome["status"] == "COMPLETED" and all(c["conditions_met"] and c["pattern_resolved"]
                     and not c["regression_observed"] for c in checks), "Resolution requires completed response, satisfied conditions, resolved pattern and no observed regression")
            for check in checks:
                _require(any(self.read_evidence(h)["source"] == "bezoar-outcome:" + outcome_handle
                             for h in check["evidence_handles"]),
                         "Resolution requires current evidence explicitly bound to this actual outcome, not teaching evidence alone")
        return review

    @_operation
    def review_outcome(self, outcome_handle, review):
        body = self._review_body(outcome_handle, review)
        # Identical reviews remain idempotent; differing judgments are preserved.
        handle = self._publish("review", {"outcome_handle": outcome_handle, "review": body,
                                         "attribution": ATTRIBUTION}, [outcome_handle] + body["evidence_handles"])
        self._retain_lesson("review", handle)
        return handle

    @_operation
    def read_review(self, handle):
        record = self._read(handle, "review")
        payload = record["payload"]
        _fields(payload, ("schema", "authority", "outcome_handle", "review", "attribution"))
        review = self._review_body(payload["outcome_handle"], payload["review"])
        _require(payload["attribution"] == ATTRIBUTION
                 and record["links"] == sorted(set([payload["outcome_handle"]] + review["evidence_handles"])), "Review custody differs")
        return _copy(payload)

    def _reviews(self, outcome_handle):
        return [h for h in self._catalog("review") if self._read(h, "review")["payload"].get("outcome_handle") == outcome_handle]

    @_operation
    def fold_eligibility(self, outcome_handle):
        outcome = self.read_outcome(outcome_handle)
        handles = self._reviews(outcome_handle)
        reviews = [self.read_review(h)["review"] for h in handles]
        resolved = bool(reviews) and all(r["verdict"] == "RESOLVED" for r in reviews)
        prepared = self.read_prepared(outcome["run_handle"])
        # Later failed practice on the same pattern reopens eligibility. Reports
        # never erase prior success, but it cannot certify unrestricted reuse.
        history = self._history_without_reviews(prepared, outcome_handle)
        eligible = outcome["status"] == "COMPLETED" and resolved and not history
        return {"eligible": eligible, "status": "REVIEWED_RESOLUTION" if eligible else "NOT_DEMONSTRATED",
                "scope": {"encounter": prepared["binding"]["encounter"], "purpose": prepared["binding"]["purpose"],
                          "seed_handles": prepared["binding"]["seed_handles"], "nacre_handles": prepared["binding"]["nacre_handles"]},
                "run_handle": outcome["run_handle"], "outcome_handle": outcome_handle, "review_handles": handles,
                "reopening_outcomes": history, "attribution": ATTRIBUTION, "authority": "NONE",
                "claim": "Declared reviewed resolution in this retained encounter; general transfer and weight learning remain unestablished"}

    def _history_without_reviews(self, prepared, original_outcome):
        original = self._read(prepared["run_handle"], "run")["payload"]
        reopened = []
        for handle in self._catalog("outcome"):
            if handle == original_outcome:
                continue
            later = self.read_outcome(handle)
            run = self._read(later["run_handle"], "run")["payload"]
            if run["created_at"] <= original["created_at"] or not set(run["binding"]["nacre_handles"]) & set(original["binding"]["nacre_handles"]):
                continue
            reviews = [self.read_review(h)["review"] for h in self._reviews(handle)]
            if later["status"] != "COMPLETED" or any(r["verdict"] != "RESOLVED" for r in reviews):
                reopened.append(handle)
        return reopened
