"""Explicit reviewed NACRE lessons over retained Bezoar observations.

A pearl is a NACRE record, not another organ or a promotion of a live seed.
Distinct observations may describe one continuing incident. Role strings and
hashes retain attribution and bytes; they cannot authenticate an independent
reviewer, demonstrate useful learning or authorize corrective action.
"""
from datetime import datetime, timezone
import hashlib

from . import bezoar_registry as registry
from .nodes import _json_copy
from .organs._kernel import fields, require, text, typed_equal
from .records import HANDLE, canonical

REQUEST_SCHEMA = "bezoar-nacre-request/v1"
REVIEW_SCHEMA = "bezoar-nacre-review/v1"
NACRE_SCHEMA = "bezoar-nacre/v1"
MAX_BYTES = 262144
MAX_LAYERS = 16
ATTRIBUTION = "DECLARED_EXTERNAL_REVIEW: role strings do not authenticate a reviewer or establish independence"
LAYER_SCOPE = "Distinct recorded observations; repeated observations may describe one continuing incident"


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _read(store, handle, owner, kind):
    require(type(handle) is str and HANDLE.fullmatch(handle), "Exact record handle required")
    require(store.path(handle).stat().st_size <= MAX_BYTES, "Lesson input record exceeds bound")
    try:
        record = store.relay("bezoar", handle, owner=owner, kind=kind)
    except (RecursionError, TypeError) as exc:
        raise ValueError("Invalid lesson evidence record") from exc
    return _json_copy(record, max_bytes=MAX_BYTES)


def _date(value):
    text(value, "Observation timestamp", 64)
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Observation needs its recorded UTC timestamp") from exc
    require(stamp.tzinfo is not None and stamp.utcoffset() == timezone.utc.utcoffset(stamp),
            "Observation timestamp must be UTC")
    return stamp.date().isoformat()


def finding_identity(finding):
    """Hold the source identity fixed while the observed bytes may differ."""
    require(type(finding) is dict, "Finding object required")
    keys = ("code", "handle", "source", "expected_sha256")
    family = {key: finding.get(key) for key in keys}
    text(family["code"], "Finding code", 128)
    require(type(family["handle"]) is str and HANDLE.fullmatch(family["handle"]),
            "Lesson requires a finding bound to a record handle")
    text(family["source"], "Finding source", 2048)
    expected = family["expected_sha256"]
    require(type(expected) is str and len(expected) == 64
            and all(c in "0123456789abcdef" for c in expected), "Finding source hash required")
    return family


def _layer(store, handle):
    from .bezoar import held_payload
    record = _read(store, handle, "bezoar", "routing")
    payload = record["payload"]
    observation = payload.get("observation")
    observed = _read(store, observation, "watcher", "observation")
    require(record["links"] == [observation] and observed["links"] == [],
            "Held observation link differs")
    require(typed_equal(payload, held_payload(observed["payload"], observation))
            and payload["status"] == "HELD", "Layer requires exact HELD routing evidence")
    date = _date(observed["payload"].get("observed_at"))
    findings = {}
    for held in payload["held_findings"]:
        try:
            identity = finding_identity(held["finding"])
        except ValueError:
            # Coverage/record failures without an exact captured source cannot
            # be silently folded into a source-specific boundary lesson.
            continue
        key = digest(identity)
        require(key not in findings, "Ambiguous duplicate finding in observation")
        findings[key] = (identity, held)
    return {"routing": handle, "observation": observation,
            "observed_at": observed["payload"]["observed_at"], "date": date}, findings


def _registry_node(identifier, fields_dict, prose, layers=()):
    node = registry.Node(identifier, 0)
    node.fields = fields_dict
    node.subjects = [fields_dict["subject"]]
    node.prose = [prose]
    node.layers = list(layers)
    return node


def _build(store, held_handles, *, lesson, producer, checker, finding_key=None):
    require(type(held_handles) is list and 2 <= len(held_handles) <= MAX_LAYERS
            and all(type(h) is str and HANDLE.fullmatch(h) for h in held_handles)
            and len(set(held_handles)) == len(held_handles), "Need 2..16 distinct HELD records")
    for value, label, bound in ((lesson, "Boundary lesson", 8192),
                                 (producer, "Producer", 256), (checker, "Checker", 256)):
        text(value, label, bound)
    require(producer.strip() != checker.strip(), "Lesson producer and checker must differ")
    extracted = [_layer(store, h) for h in sorted(held_handles)]
    extracted.sort(key=lambda item: (datetime.fromisoformat(item[0]["observed_at"].replace("Z", "+00:00")),
                                    item[0]["observation"]))
    observations = [layer["observation"] for layer, _ in extracted]
    require(len(set(observations)) == len(observations), "Duplicate observation cannot become another layer")
    common = set.intersection(*(set(found) for _, found in extracted))
    if finding_key is None:
        require(len(common) == 1, "Choose an exact common finding key; ambiguous or unrelated layers")
        finding_key = next(iter(common))
    require(type(finding_key) is str and finding_key in common,
            "Layer findings do not share this source identity and finding")
    family = extracted[0][1][finding_key][0]
    layers, seeds = [], []
    subject = family["handle"] + "/" + family["source"]
    for index, (layer, findings) in enumerate(extracted):
        held = findings[finding_key][1]
        layers.append({**layer, "held_id": held["id"], "finding": held["finding"]})
        seeds.append(_registry_node(f"BZ-{index + 1:04d}", {
            "kind": "SEED", "law": family["code"], "subject": subject,
            "first_seen": layer["observed_at"], "state": "HELD", "clears_when": held["clears_when"]},
            "Retained observed discrepancy; no automatic clearing."))
    nacre = _registry_node("BZ-9999", {
        "kind": "NACRE", "law": family["code"], "subject": subject,
        "first_seen": layers[0]["observed_at"], "state": "LAYERED", "adage": lesson},
        "Boundary lesson over the selected observations; live seeds remain HELD.",
        ["- " + x["date"] + " " + x["observation"] for x in layers])
    require(not registry.check([*seeds, nacre], [], [(family["code"], subject)]),
            "Existing SEED/NACRE registry invariants refuse the lesson")
    body = {"lesson": lesson, "producer": producer, "checker": checker,
            "finding_key": finding_key, "finding": family, "layers": layers,
            "layer_scope": LAYER_SCOPE, "seeds_remain_held": True,
            "registry_node": {"fields": nacre.fields, "layers": nacre.layers, "prose": nacre.prose},
            "authority": "NONE"}
    return _json_copy(body, max_bytes=MAX_BYTES)


def prepare_lesson(store, held_handles, *, lesson, producer, checker, finding_key=None):
    body = _build(store, held_handles, lesson=lesson, producer=producer,
                  checker=checker, finding_key=finding_key)
    payload = {"schema": REQUEST_SCHEMA, "request": body, "request_sha256": digest(body)}
    return store.publish("bezoar", "nacre-request", payload, links=held_handles)


def _request(store, handle):
    record = _read(store, handle, "bezoar", "nacre-request")
    payload = record["payload"]
    fields(payload, "schema request request_sha256", "Lesson request")
    require(payload["schema"] == REQUEST_SCHEMA and type(payload["request"]) is dict,
            "Unsupported lesson request")
    body = payload["request"]
    expected = _build(store, record["links"], lesson=body.get("lesson"),
                      producer=body.get("producer"), checker=body.get("checker"),
                      finding_key=body.get("finding_key"))
    require(typed_equal(body, expected) and payload["request_sha256"] == digest(expected),
            "Lesson request differs from exact retained evidence")
    return record


def _review(envelope, request_handle, request):
    envelope = _json_copy(envelope, max_bytes=MAX_BYTES)
    fields(envelope, "schema request_handle request_sha256 reviewer verdict layer_verdicts applicability reason",
           "Lesson review")
    body = request["payload"]["request"]
    require(envelope["schema"] == REVIEW_SCHEMA and envelope["request_handle"] == request_handle
            and envelope["request_sha256"] == request["payload"]["request_sha256"],
            "Review is not bound to this lesson request")
    require(envelope["reviewer"] == body["checker"], "Review must come from the declared separate checker")
    require(envelope["verdict"] in ("PASS", "FAIL", "UNKNOWN")
            and envelope["applicability"] in ("SAME_SOURCE_AND_FINDING", "UNRELATED", "UNKNOWN"),
            "Invalid lesson review verdict or applicability")
    text(envelope["reason"], "Review rationale", 8192)
    rows = envelope["layer_verdicts"]
    require(type(rows) is list and len(rows) == len(body["layers"]), "Review every selected layer exactly once")
    seen = set()
    for row in rows:
        fields(row, "observation verdict reason", "Layer review")
        require(type(row["observation"]) is str and row["observation"] not in seen,
                "Duplicate or invalid reviewed observation")
        seen.add(row["observation"])
        require(row["verdict"] in ("SUPPORTED", "UNSUPPORTED", "UNKNOWN"), "Invalid layer support verdict")
        text(row["reason"], "Layer review rationale", 8192)
    require(seen == {x["observation"] for x in body["layers"]}, "Review belongs to different layer evidence")
    if envelope["verdict"] == "PASS":
        require(envelope["applicability"] == "SAME_SOURCE_AND_FINDING"
                and all(row["verdict"] == "SUPPORTED" for row in rows),
                "PASS requires related applicability and support for every selected layer")
    return envelope


def publish_lesson(store, request_handle, envelope):
    request = _request(store, request_handle)
    review = _review(envelope, request_handle, request)
    checked = store.publish("bezoar", "nacre-review", {
        "schema": REVIEW_SCHEMA, "review": review, "status": "REVIEWED_" + review["verdict"],
        "attribution": ATTRIBUTION, "authority": "NONE"}, links=[request_handle])
    if review["verdict"] != "PASS":
        return checked
    body = request["payload"]["request"]
    payload = {"schema": NACRE_SCHEMA, "kind": "NACRE", "state": "LAYERED", "status": "LAYERED",
               "request": request_handle, "review": checked, "lesson": body["lesson"],
               "finding": body["finding"], "layers": body["layers"], "registry_node": body["registry_node"],
               "seeds_remain_held": True, "layer_scope": LAYER_SCOPE,
               "attribution": ATTRIBUTION, "authority": "NONE", "activation": "NONE",
               "current_source_freshness": "NOT_EVALUATED; these are retained observations"}
    return store.publish("bezoar", "nacre", payload, links=[checked])


def read_lesson(store, handle):
    """Recheck the lesson's historical evidence and review, without reading live sources."""
    record = _read(store, handle, "bezoar", "nacre")
    payload = record["payload"]
    review_handle, request_handle = payload.get("review"), payload.get("request")
    review = _read(store, review_handle, "bezoar", "nacre-review")
    request = _request(store, request_handle)
    checked = _review(review["payload"].get("review"), request_handle, request)
    require(checked["verdict"] == "PASS" and record["links"] == [review_handle]
            and review["links"] == [request_handle], "NACRE requires its exact accepted review chain")
    require(typed_equal(review["payload"], {"schema": REVIEW_SCHEMA, "review": checked, "status": "REVIEWED_PASS",
            "attribution": ATTRIBUTION, "authority": "NONE"}), "Stored lesson review differs")
    body = request["payload"]["request"]
    expected = {"schema": NACRE_SCHEMA, "kind": "NACRE", "state": "LAYERED", "status": "LAYERED",
                "request": request_handle, "review": review_handle, "lesson": body["lesson"],
                "finding": body["finding"], "layers": body["layers"], "registry_node": body["registry_node"],
                "seeds_remain_held": True, "layer_scope": LAYER_SCOPE,
                "attribution": ATTRIBUTION, "authority": "NONE", "activation": "NONE",
                "current_source_freshness": "NOT_EVALUATED; these are retained observations"}
    require(typed_equal(payload, expected), "NACRE differs from its reviewed lesson evidence")
    return payload
