"""Read-only, typed learning provenance for Yggdrasil's captured records.

Advice and actual outcomes are distinct from factual source support. Validation
reuses the producer's versioned contracts over captured bytes; it neither opens
an attributed workspace nor calls a provider or promotes a learning fold.
"""
from . import macro_voxel as mv
from .bezoar_learning import LearningMemory, MAX_CONTEXT_BYTES, _operation
from .records import HANDLE, canonical


def _improvement_input(record):
    """Recognize the explicit trial wrapper, never search arbitrary prose.

    The broker input is already retained as inert JSON. This extracts only the
    versioned adapter/practice/literal-context path; it does not follow an
    attributed pathname or interpret candidate source as instructions.
    """
    if (record["owner"] != "nurarihyon" or record["kind"] != "learning-dispatch"):
        return None
    packet = record["payload"].get("input")
    if type(packet) is not dict or packet.get("schema") != "loom-adapter-request/v1":
        return None
    context = packet.get("context")
    if type(context) is not str or len(context.encode()) > 2 * 1024 * 1024:
        raise ValueError("Retained adapter context exceeds its bound")
    practice = mv.parse_json(context.encode())
    if type(practice) is not dict or practice.get("schema") != "loom-practice-context/v1":
        return None
    literal = practice.get("literal_context")
    if type(literal) is not str or not literal.lstrip().startswith("{"):
        return None
    try:
        wrapper = mv.parse_json(literal.encode())
    except ValueError:
        # A user's ordinary literal account can begin with an unmatched brace.
        return None
    schema = wrapper.get("schema") if type(wrapper) is dict else None
    if type(schema) is str and schema.startswith("loom-improvement-tool-input/"):
        from .loom_improvement import decode_tool_context
        wrapper = decode_tool_context(wrapper)
        schema = wrapper["schema"]
    if type(schema) is not str or not schema.startswith("loom-improvement-input/"):
        return None
    if (schema != "loom-improvement-input/v1"
            or set(wrapper) != {"schema", "attempt_handle", "snapshot"}):
        raise ValueError("Unsupported retained improvement input contract")
    return wrapper


def _improvement_snapshot_refs(snapshot):
    """Typed dependencies embedded in a complete historical trial snapshot."""
    if (type(snapshot) is not dict or snapshot.get("schema") != "loom-improvement-context/v1"
            or snapshot.get("authority") != "NONE"):
        raise ValueError("Unsupported retained improvement snapshot")
    refs = [("improvement_selected_candidate", snapshot["candidate_handle"])]
    refs.extend(("improvement_candidate_revision", row["handle"]) for row in snapshot["lineage"])
    for row in snapshot["history"]:
        refs.extend((("improvement_prior_attempt", row["attempt_handle"]),
                     ("improvement_trial_candidate", row["candidate_handle"]),
                     ("improvement_native_preparation", row["run_handle"])))
        if "outcome_handle" in row:
            refs.append(("improvement_actual_outcome", row["outcome_handle"]))
        refs.extend(("improvement_correction_review", review["handle"]) for review in row["reviews"])
    return refs


def _check_improvement_dispatch(store, record, attempt):
    """Keep the admitted input identity when a trial is captured historically."""
    body = record["payload"]
    raw = canonical(body["input"])
    if (body.get("schema") != "nurarihyon-learning-dispatch/v1" or body.get("authority") != "NONE"
            or body.get("provider_call_limit") != 1 or body["run_handle"] != attempt["run_handle"]
            or body["input_sha256"] != mv.digest(raw)
            or record["links"] != sorted([body["run_handle"], body["assessment_handle"]])):
        raise ValueError("Improvement dispatch differs from its admitted native input")
    admission = store.relay("yggdrasil", body["assessment_handle"], owner="nurarihyon", kind="learning-admission")
    assessment = admission["payload"]
    if (assessment.get("schema") != "conscience-learning-assessment/v1" or assessment.get("verdict") != "ALLOW"
            or assessment.get("authority") != "NONE" or assessment.get("executed") is not False
            or assessment.get("run_handle") != body["run_handle"]
            or assessment.get("input_sha256") != body["input_sha256"]
            or type(assessment.get("input_bytes")) is not int or assessment["input_bytes"] != len(raw)
            or assessment.get("context_delivery") not in (
                "FULL_NATIVE_PREPARATION", "EXACT_NATIVE_READER_PROJECTION",
                "RULE_FOLDED_NATIVE_READER_PROJECTION", "RULE_DEPARTED_NATIVE_READER_PROJECTION")
            or admission["links"] != [body["run_handle"]]):
        raise ValueError("Improvement admission differs from the captured dispatch")
    if assessment["context_delivery"].startswith("RULE_"):
        from .learning_context import expand_learning_projection
        practice = mv.parse_json(body["input"]["context"].encode())
        method = expand_learning_projection(practice["learning_context"])["projection"]["method"]
        expected = {"RULE_FOLDED_NATIVE_READER_PROJECTION": "EXACT_TEACHING_FOLDED_HISTORY",
                    "RULE_DEPARTED_NATIVE_READER_PROJECTION": "EXACT_TEACHING_DEPARTED_HISTORY"}
        if method != expected[assessment["context_delivery"]]:
            raise ValueError("Improvement admission differs from its retained selection rule")


def learning_refs(record):
    """Return bounded typed dependencies, including history embedded in a run."""
    try:
        owner, kind, payload = record["owner"], record["kind"], record["payload"]
        refs = []
        if owner == "origami" and kind == "answer_evidence":
            if "learning_context" in payload:
                refs.append(("learning_preparation", payload["learning_context"]["run_handle"]))
            if "learning_outcome" in payload:
                retained = payload["learning_outcome"]
                refs.append(("learning_actual_outcome", retained["outcome_handle"]))
                if "terminal" in retained:
                    refs.append(("learning_terminal", retained["terminal"]))
        elif owner == "bezoar" and kind == "learning-seed" and "derivation" in payload:
            refs.append(("learning_lesson_source", payload["derivation"]["source_handle"]))
        elif owner == "bezoar" and kind == "learning-run":
            context = payload["context"]
            if type(context) is not str or len(context.encode()) > MAX_CONTEXT_BYTES:
                raise ValueError("Retained learning context exceeds its bound")
            context = mv.parse_json(context.encode())
            for field, relation in (("seeds", "learning_candidate_advice"),
                                    ("nacres", "learning_repeated_pattern"),
                                    ("evidence", "learning_observation")):
                refs.extend((relation, row["handle"]) for row in context[field])
            if context.get("schema") == "bezoar-learning-context/v2":
                from .history_inventory import typed_references
                refs.extend(typed_references(context["history"], reference_budget=256 - len(refs)))
            else:
                if context.get("schema") != "bezoar-learning-context/v1":
                    raise ValueError("Unsupported retained learning context")
                for row in context["history"]:
                    refs.append(("learning_prior_attempt", row["run_handle"]))
                    if "outcome_handle" in row:
                        refs.append(("learning_prior_outcome", row["outcome_handle"]))
                    refs.extend(("learning_correction_review", review["handle"]) for review in row["reviews"])
                    refs.extend(("learning_candidate_advice", lesson["handle"])
                                for lesson in row.get("lessons", []))
        elif owner == "bezoar" and kind == "learning-outcome":
            refs.append(("learning_attempt", payload["run_handle"]))
        elif owner == "bezoar" and kind == "learning-review":
            refs.append(("learning_reviewed_outcome", payload["outcome_handle"]))
            refs.extend(("learning_review_observation", h) for h in payload["review"]["evidence_handles"])
        elif owner == "loom" and kind == "improvement-candidate":
            refs.extend(("improvement_source_observation", h) for h in payload["evidence_handles"])
            if payload["parent"] is not None:
                refs.append(("improvement_parent_revision", payload["parent"]))
        elif owner == "loom" and kind == "improvement-attempt":
            refs.extend((("improvement_trial_candidate", payload["candidate_handle"]),
                         ("improvement_native_preparation", payload["run_handle"])))
            refs.extend(_improvement_snapshot_refs(payload["snapshot"]))
        elif owner == "nurarihyon" and kind == "learning-dispatch":
            wrapper = _improvement_input(record)
            if wrapper is not None:
                refs.append(("improvement_dispatched_attempt", wrapper["attempt_handle"]))
                refs.extend(_improvement_snapshot_refs(wrapper["snapshot"]))
        if len(refs) > 256 or any(type(h) is not str or HANDLE.fullmatch(h) is None for _, h in refs):
            raise ValueError("Learning provenance requires bounded exact handles")
        return sorted(set(refs))
    except (KeyError, TypeError, AttributeError, IndexError, RecursionError) as exc:
        raise ValueError("Malformed retained learning provenance") from exc


class CapturedLearningMemory(LearningMemory):
    _read_only_capture = True

    def _publish(self, *args, **kwargs):
        raise ValueError("Captured learning archives are read-only")

    def __init__(self, store):
        # Deliberately bypass LearningMemory/RecordStore filesystem constructors.
        self.store = store
        self.workspace = None

    @_operation
    def validate_archive(self):
        """Share validation only within this read-only captured-archive pass.

        Import validates an isolated copy and checks the source bytes again
        before publication. Reusing common ancestors here avoids validating the
        same cumulative history once per row. The operation cache is discarded
        on success or failure; later reads still detect changed bytes.
        """
        _validate_archive(self)


def validate_learning_records(store):
    """Validate historical records without applying today's dispatch selection."""
    memory = CapturedLearningMemory(store)
    memory.validate_archive()
    return memory


def _validate_archive(memory):
    store = memory.store
    loaders = {"learning-" + kind: getattr(memory, "read_" + method)
               for kind, method in (("seed", "seed"), ("nacre", "nacre"),
                                    ("evidence", "evidence"), ("run", "prepared"),
                                    ("outcome", "outcome"), ("review", "review"))}
    improvements = None
    memory._catalog_rows = store.catalog()["entries"]
    for row in memory._catalog_rows:
        if row["owner"] == "bezoar" and row["kind"].startswith("learning-"):
            if row["kind"] not in loaders:
                raise ValueError("Unsupported captured Bezoar learning contract")
            loaders[row["kind"]](row["handle"])
        elif row["owner"] == "loom" and row["kind"].startswith("improvement-"):
            if improvements is None:
                from .loom_improvement import ImprovementCycle
                improvements = ImprovementCycle(memory)
            method = {"improvement-candidate": improvements.read_candidate,
                      "improvement-attempt": improvements.read_attempt}.get(row["kind"])
            if method is None:
                raise ValueError("Unsupported captured Loom improvement contract")
            method(row["handle"])
        elif row["owner"] == "nurarihyon" and row["kind"] == "learning-dispatch":
            record = store.read(row["handle"])
            if _improvement_input(record) is not None:
                if improvements is None:
                    from .loom_improvement import ImprovementCycle
                    improvements = ImprovementCycle(memory)
                attempt = improvements.validate_trial_packet(record["payload"]["input"], historical=True)
                _check_improvement_dispatch(store, record, attempt)


def check_packet_learning(packet, memory):
    prepared = memory.validate_prepared(packet["learning_context"])
    if (prepared["binding"]["encounter"] != packet["question"]
            or prepared["binding"]["reader"] != packet["provider"]):
        raise ValueError("Learning preparation differs from the answer question or provider")
    retained = packet["learning_outcome"]
    fields = {"outcome_handle", "learning", "response_sha256"}
    if type(retained) is dict and "terminal" in retained:
        fields.add("terminal")
    mv._keys(retained, fields, "retained learning outcome")
    outcome = memory.read_outcome(retained["outcome_handle"])
    observation = outcome["observation"]
    mv._keys(observation, {"origami_run_id", "question", "provider_status", "reason"}, "Origami learning observation")
    mv._text(observation["reason"], "Origami outcome reason")
    if (outcome["run_handle"] != prepared["run_handle"] or outcome["status"] != "COMPLETED"
            or outcome["response"] != canonical(packet["fold"]).decode()
            or outcome["response_sha256"] != retained["response_sha256"]
            or retained["learning"] != "NOT_DEMONSTRATED_BY_TERMINAL_STATUS"
            or observation["origami_run_id"] != packet["run_id"]
            or observation["question"] != packet["question"] or observation["provider_status"] != "ANSWER"):
        raise ValueError("Retained learning outcome differs from the actual answer")
    if "terminal" in retained:
        _check_terminal(packet, memory, prepared, retained)


def _check_terminal(packet, memory, prepared, retained):
    """Check the exact Origami broker receipt; attributed filesystem paths are inert."""
    record = memory.store.relay("yggdrasil", retained["terminal"], owner="nurarihyon", kind="learning-terminal")
    payload = record["payload"]
    mv._keys(payload, {"schema", "run_handle", "outcome_handle", "dispatch_handle", "response_receipt",
                       "response_source", "authority"}, "learning terminal")
    if (payload["schema"] != "nurarihyon-learning-terminal/v1" or payload["authority"] != "NONE"
            or payload["run_handle"] != prepared["run_handle"] or payload["outcome_handle"] != retained["outcome_handle"]
            or payload["response_receipt"] is not None or payload["response_source"] != "origami_retained_outcome"
            or type(payload["dispatch_handle"]) is not str
            or record["links"] != sorted([prepared["run_handle"], retained["outcome_handle"], payload["dispatch_handle"]])):
        raise ValueError("Learning terminal differs from the Origami outcome")
    dispatch = memory.store.relay("yggdrasil", payload["dispatch_handle"], owner="nurarihyon", kind="learning-external-dispatch")
    body = dispatch["payload"]
    mv._keys(body, {"schema", "run_handle", "input", "input_sha256", "assessment_handle", "source", "owner_pid",
                   "owner_process_token", "owner_process_observed_at", "dispatched_at", "provider_call_limit",
                   "call_bound_owner", "authority"}, "learning external dispatch")
    source = body["source"]
    mv._keys(source, {"workspace", "run_id", "intent_sha256"}, "learning dispatch source")
    if (body["schema"] != "nurarihyon-learning-external-dispatch/v1" or body["authority"] != "NONE"
            or body["run_handle"] != prepared["run_handle"] or source["run_id"] != packet["run_id"]
            or body["input_sha256"] != mv.digest(canonical(body["input"]))
            or dispatch["links"] != sorted([prepared["run_handle"], body["assessment_handle"]])
            or type(body["provider_call_limit"]) is not int or not 1 <= body["provider_call_limit"] <= 16
            or body["call_bound_owner"] != "Origami"):
        raise ValueError("Learning dispatch differs from the retained encounter")
    for name in ("workspace", "intent_sha256"):
        mv._text(source[name], "learning source " + name)
    if mv.SHA.fullmatch(source["intent_sha256"]) is None:
        raise ValueError("Invalid retained intent identity")
    if type(body["owner_pid"]) is not int or body["owner_pid"] < 1:
        raise ValueError("Invalid retained dispatch process")
    for name in ("owner_process_token", "owner_process_observed_at", "dispatched_at"):
        mv._text(body[name], "learning dispatch " + name)
    admission = memory.store.relay("yggdrasil", body["assessment_handle"], owner="nurarihyon", kind="learning-admission")
    assessment = admission["payload"]
    mv._keys(assessment, {"schema", "verdict", "run_handle", "input_sha256", "input_bytes", "authority", "executed",
                         "context_delivery", "obligation", "scope"}, "learning admission")
    if (assessment["schema"] != "conscience-learning-assessment/v1" or assessment["verdict"] != "ALLOW"
            or assessment["authority"] != "NONE" or assessment["executed"] is not False
            or assessment["run_handle"] != prepared["run_handle"]
            or assessment["input_sha256"] != body["input_sha256"]
            or type(assessment["input_bytes"]) is not int or assessment["input_bytes"] != len(canonical(body["input"]))
            or admission["links"] != [prepared["run_handle"]]):
        raise ValueError("Learning admission differs from retained dispatch input")
    # Validate delivery without invoking present-day admission or a provider.
    from lodestone_nurarihyon.learning_conscience import _contains
    from .learning_context import (
        _intern_learning_projection, expand_learning_projection, project_learning_context,
    )
    delivery = assessment["context_delivery"]
    expected = (prepared if delivery == "FULL_NATIVE_PREPARATION"
                else project_learning_context(memory, prepared) if delivery == "EXACT_NATIVE_READER_PROJECTION" else None)
    delivered = expected is not None and _contains(body["input"], expected)
    if not delivered and delivery == "EXACT_NATIVE_READER_PROJECTION":
        # The older canonical handle-only transport must remain valid after
        # adding compact history. Rebuild it from the same native preparation;
        # neither arbitrary prose nor today's newer history can substitute.
        legacy = _intern_learning_projection(expand_learning_projection(expected))
        delivered = _contains(body["input"], legacy)
    if not delivered:
        raise ValueError("Learning dispatch omitted the retained preparation")
    for name in ("obligation", "scope"):
        mv._text(assessment[name], "learning admission " + name)
