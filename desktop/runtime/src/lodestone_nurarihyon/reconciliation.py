"""Explicit captured-corpus operation, using existing Bezoar/Yggdrasil code.

The capsule is the exact input, not authority. No arbitrary repository is read,
no finding is cleared, and no saved Origami or lineage claim becomes proof.
Worker production and controller acceptance use separate captured roots. Both
are retained with the published generation; acceptance never trusts the
worker's report of an independent check. This is fixed engineering, not learning.
"""
from pathlib import Path
import re

from lodestone_machinery.bezoar_reconciliation import capsule, read_reconciliation
from .common import canonical, digest, read_bytes, tree_inventory
from .plans import PlanError, _current, _typed_equal

TOOLKIT = "bezoar.reconciliation/v1"
PLAN_SCHEMA = "nurarihyon-reconciliation-plan/v1"
SOURCE_TITLE = "Authorized captured discrepancy corpus"
REQUEST_TITLE = "Authorized captured-corpus reconciliation"
RESULT_TITLE = "Captured discrepancy reconciliation"
SUMMARY = "Captured evidence checked; original outcomes retained. No repair, activation or learning."
ROUTE_TITLE = "Retained discrepancy routing"
ROUTE_SUMMARY = "Prior evidence checked for applicability; findings retained. No repair or activation."


def route_selection(board, raw):
    """Select the latest retained reconciliation in the declared corpus only.

    This fixed selection rule is not learning. The router independently checks
    the selected evidence before proposing any existing holder reference.
    """
    source, _, _ = parse_source(raw)
    prior = None
    for item in board.state()["artifacts"]:
        if item["stage"] != "RECONCILE" or len(item["source_ids"]) != 1:
            continue
        artifact = board.artifact(item["id"])
        record = artifact["record"]
        if (record["owner"] == "bezoar" and (record["kind"], record["payload"].get("schema")) in
                (("capture-reconciliation", "bezoar-capture-reconciliation/v1"),
                 ("capture-observation", "bezoar-capture-observation/v1"))):
            # A distinct admitted source contract is visible history, but is
            # not silently imported into the existing inline-v1 reuse rule.
            continue
        if record["owner"] != "bezoar" or record["kind"] != "reconciliation":
            raise ValueError("Retained reconciliation has an invalid producer")
        captured = board.source(item["source_ids"][0])
        value, _, _ = parse_source(captured["text"].encode("utf-8"))
        if value["corpus"] == source["corpus"]:
            prior = artifact
            break
    return {"schema": "nurarihyon-retained-route/v1", "selection": "latest-same-corpus/v1",
            "previous_artifact": prior["id"] if prior else None,
            "previous_handle": prior["handle"] if prior else None}


def attach_route_plan(plan, board, raw):
    result = dict(plan)
    result["schema"] = "nurarihyon-reconciliation-plan/v2"
    result["retained_routing"] = route_selection(board, raw)
    return result


def validate_route_selection(plan, board, raw):
    if "retained_routing" in plan and not _typed_equal(plan["retained_routing"], route_selection(board, raw)):
        raise ValueError("Retained routing differs from the parent generation")


def _validated_route(value):
    if (type(value) is not dict or set(value) != {"schema", "selection", "previous_artifact", "previous_handle"}
            or value["schema"] != "nurarihyon-retained-route/v1"
            or value["selection"] != "latest-same-corpus/v1"):
        raise ValueError("Invalid retained-routing plan")
    identity, handle = value["previous_artifact"], value["previous_handle"]
    if not ((identity is None and handle is None) or
            (type(identity) is str and re.fullmatch(r"[0-9a-f]{64}", identity)
             and type(handle) is str and re.fullmatch(r"bezoar:[0-9a-f]{64}", handle))):
        raise ValueError("Invalid retained reconciliation reference")
    return dict(value)


def parse_source(raw):
    try:
        return capsule(raw, digest(raw))
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise PlanError("REJECTED_VALIDATION", "Invalid bounded captured discrepancy corpus") from None


def make_reconciliation_plan(raw, current_view):
    source, contents, _ = parse_source(raw)
    return {"schema": PLAN_SCHEMA, "toolkit": TOOLKIT,
            "source_sha256": digest(raw), "source_bytes": len(raw),
            "corpus": source["corpus"], "receipt_inventory": source["receipt_inventory"],
            "captured_files": [{"path": name, "bytes": len(data), "sha256": digest(data)}
                               for name, data in sorted(contents.items())],
            "origami_results": source["origami_results"],
            "baseline": _current(current_view), "authority": "PROPOSAL_ONLY",
            "operation": "captured-lineage-and-registry-coverage/v1", "activation": "NONE",
            "closure": "Entire declared captured corpus; unseen repositories are outside this operation",
            "original_outcomes": "RETAIN_UNCHANGED", "repair": "NONE", "learning": False}


def validate_reconciliation_plan(plan, raw, current_view):
    if type(plan) is not dict or plan.get("source_sha256") != digest(raw):
        raise PlanError("SOURCE_DRIFT", "Captured corpus differs from planned source")
    expected = make_reconciliation_plan(raw, current_view)
    if "retained_routing" in plan:
        expected["schema"] = "nurarihyon-reconciliation-plan/v2"
        expected["retained_routing"] = _validated_route(plan["retained_routing"])
    if not _typed_equal(plan, expected):
        raise PlanError("REJECTED_VALIDATION", "Reconciliation plan differs from independent recomputation")


def question(task_id):
    return "Check captured lineage and registry coverage as inert evidence for authorized task " + task_id + "."


def build_reconciliation(board, raw, task, *, scratch, capture_bundle=None):
    """Worker-only publication into its private staged Board."""
    from lodestone_machinery.bezoar_capture import _cost_phase, worker_copy_store
    if capture_bundle is None:
        validate_reconciliation_plan(task["plan"], raw, board.settings())
        validate_route_selection(task["plan"], board, raw)
    else:
        from . import complete_capture
        bezoar_capture = complete_capture.capture_module(task["plan"]["toolkit"])
        validator = (complete_capture.validate_observation_plan if task["plan"]["toolkit"] ==
                     complete_capture.OBSERVATION_TOOLKIT else complete_capture.validate_reconciliation_plan)
        validator(task["plan"], raw, board.settings())
        complete_capture.validate_route_selection(task["plan"], board, raw)
        _cost_phase("worker_resolve", "BEGIN")
        resolved = bezoar_capture.resolve(raw, bezoar_capture.ExistingStore(capture_bundle), expected_sha256=digest(raw))
        _cost_phase("worker_resolve", "END")
        _cost_phase("worker_copy", "BEGIN")
        bezoar_capture.copy_records(resolved, worker_copy_store(board.plumber.records))
        _cost_phase("worker_copy", "END")
    _cost_phase("ingestion_eat", "BEGIN")
    source_id = board.ingest(raw, title=SOURCE_TITLE, uri="sha256:" + digest(raw), author="host", kind="file")
    request = board.eat([source_id], author="host", question=question(task["id"]), title=REQUEST_TITLE)
    _cost_phase("ingestion_eat", "END")
    contract = ("observation" if task["plan"]["toolkit"] == "bezoar.reconciliation-observation/v1"
                else "complete-capture" if capture_bundle is not None else "inline")
    candidate = board.reconcile(request, author="host", title=RESULT_TITLE, scratch=scratch, contract=contract)
    artifact = board.artifact(candidate)
    result = {"toolkit": task["plan"]["toolkit"], "source_id": source_id, "request_artifact": request,
            "candidate_artifact": candidate, "source_sha256": digest(raw),
            "reconciliation_sha256": digest(canonical(artifact["record"]["payload"]["report"])),
            "activation": "NONE", "repair_performed": False, "learning": False,
            }
    _cost_phase("final_board_verify", "BEGIN")
    result["board_verification"] = board.verify()
    _cost_phase("final_board_verify", "END")
    if "retained_routing" in task["plan"]:
        route = board.route_reconciliation(candidate, author="host", title=ROUTE_TITLE,
                    previous_id=task["plan"]["retained_routing"]["previous_artifact"],
                    scratch=Path(scratch).parent / "routing-work")
        result["routing_artifact"] = route
        result["routing_sha256"] = digest(canonical(board.artifact(route)["record"]["payload"]))
        result["board_verification"] = board.verify()
    return result


def validate_reconciliation_stage(board, parent, raw, task, plan, report, policy, *, stage, capture_bundle=None):
    """Controller-only independent acceptance after the owned worker has exited.

    A fresh controller-owned review root is mandatory. Rechecking a published
    generation uses its checksums or the portable retained-source reader, not
    this single-use publication function.
    """
    from lodestone_machinery.organs.ingest import read_ingest
    from lodestone_pando.store import encoded

    stage = Path(stage)
    complete = capture_bundle is not None
    capture_dependencies = []
    if complete:
        from . import complete_capture
        bezoar_capture = complete_capture.capture_module(plan["toolkit"])
        validator = (complete_capture.validate_observation_plan if plan["toolkit"] ==
                     complete_capture.OBSERVATION_TOOLKIT else complete_capture.validate_reconciliation_plan)
        validator(plan, raw, parent.settings())
        complete_capture.validate_route_selection(plan, parent, raw)
        resolved = bezoar_capture.resolve(raw, bezoar_capture.ExistingStore(capture_bundle), expected_sha256=digest(raw))
        retained_capture = bezoar_capture.resolve(raw, board.plumber.records, expected_sha256=digest(raw))
        if resolved != retained_capture:
            raise ValueError("Worker retained dependencies differ from admitted capture")
        contents = resolved["contents"]
        capture_dependencies = sorted(resolved["records"])
    else:
        validate_reconciliation_plan(plan, raw, parent.settings())
        validate_route_selection(plan, parent, raw)
        _, contents, _ = parse_source(raw)
    read_limit = bezoar_capture.MAX_FILE_BYTES if complete else 65536
    expected_toolkit = plan["toolkit"] if complete else TOOLKIT
    observation = complete and expected_toolkit == complete_capture.OBSERVATION_TOOLKIT
    if observation:
        from lodestone_machinery.bezoar_capture import OBSERVATION_SUMMARY
    expected_summary = OBSERVATION_SUMMARY if observation else SUMMARY
    expected_kind = "capture-observation" if observation else "capture-reconciliation" if complete else "reconciliation"
    routing = plan.get("retained_routing")
    prior_contents = {}
    if routing and routing["previous_artifact"] is not None:
        previous = parent.artifact(routing["previous_artifact"])
        _, prior_contents, _ = parse_source(parent.source(previous["source_ids"][0])["text"].encode("utf-8"))
    allowed_scratch = {"reconciliation-work/" + p: data for p, data in contents.items()}
    if routing:
        allowed_scratch.update({"routing-work/current/" + p: data for p, data in contents.items()})
        allowed_scratch.update({"routing-work/previous/" + p: data for p, data in prior_contents.items()})
    for key in ("source_id", "request_artifact", "candidate_artifact"):
        identity = report.get(key)
        if type(identity) is not str or re.fullmatch(r"[0-9a-f]{64}", identity) is None:
            raise ValueError("Reconciliation worker report has an invalid " + key)
    if (report.get("toolkit") != expected_toolkit or report.get("activation") != "NONE"
            or report.get("repair_performed") is not False or report.get("learning") is not False):
        raise ValueError("Reconciliation worker contract mismatch")
    # Reject worker-written review files and any other undeclared staging data.
    if (stage / "reconciliation-review").exists() or (stage / "routing-review").exists():
        raise ValueError("Controller review root must be absent before independent acceptance")
    before_paths = tree_inventory(stage, max_bytes=policy["max_stage_bytes"], max_files=policy["max_files"])
    for item in before_paths:
        name = item["path"]
        if (not name.startswith("pando/")
                and name not in {"request.json", "worker.log", "worker-result.json"}
                and name not in allowed_scratch):
            raise ValueError("Reconciliation stage contains an undeclared file")
        if name in allowed_scratch and (item["sha256"] != digest(allowed_scratch[name])
                or read_bytes(stage / name, read_limit) != allowed_scratch[name]):
            raise ValueError("Worker captured corpus differs from the approved bytes")
    captured_work = {item["path"][len("reconciliation-work/"):]: item
                     for item in before_paths if item["path"].startswith("reconciliation-work/")}
    if (set(captured_work) != set(contents)
            or any(captured_work[name]["sha256"] != digest(data)
                   or read_bytes(stage / "reconciliation-work" / name, read_limit) != data
                   for name, data in contents.items())):
        raise ValueError("Worker captured corpus differs from the approved bytes")
    if board.settings() != parent.settings():
        raise ValueError("Reconciliation changed the board view")
    before, after = parent.state(), board.state()
    if before["posts"] != after["posts"]:
        raise ValueError("Reconciliation changed existing posts")
    for collection in ("sources", "artifacts"):
        existing = {x["id"]: x for x in after[collection]}
        if any(existing.get(x["id"]) != x for x in before[collection]):
            raise ValueError("Reconciliation changed existing history")
    with parent.connection() as a, board.connection() as b:
        for sql in ("SELECT * FROM metadata ORDER BY key",
                    "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY name"):
            if [tuple(x) for x in a.execute(sql)] != [tuple(x) for x in b.execute(sql)]:
                raise ValueError("Reconciliation changed metadata or database schema")
    source_id = digest(encoded(["sha256:" + digest(raw), digest(raw)]).encode("utf-8"))
    if report.get("source_id") != source_id or report.get("source_sha256") != digest(raw):
        raise ValueError("Reconciliation source binding mismatch")
    captured = board.source(source_id)
    if (captured["text"].encode("utf-8") != raw or captured["sha256"] != digest(raw)
            or captured["bytes"] != len(raw) or captured["uri"] != "sha256:" + digest(raw)
            or captured["author"] != "host" or captured["kind"] != "file"):
        raise ValueError("Reconciliation source capture mismatch")
    old_sources = {x["id"] for x in before["sources"]}
    if {x["id"] for x in after["sources"]} != old_sources | {source_id}:
        raise ValueError("Unexpected reconciliation source additions")
    if source_id not in old_sources and captured["title"] != SOURCE_TITLE:
        raise ValueError("Reconciliation source title mismatch")
    request, candidate = (board.artifact(report[k]) for k in ("request_artifact", "candidate_artifact"))
    if (request["stage"] != "EAT" or request["parent_id"] is not None
            or candidate["stage"] != "RECONCILE" or candidate["parent_id"] != request["id"]
            or request["title"] != REQUEST_TITLE or request["summary"] != question(task["id"])
            or candidate["title"] != RESULT_TITLE or candidate["summary"] != expected_summary
            or any(a["source_ids"] != [source_id] or a["author"] != "host" for a in (request, candidate))):
        raise ValueError("Reconciliation artifact lineage mismatch")
    for artifact, owner, kind in ((request, "eat", "digestion_request"),
                                 (candidate, "bezoar", expected_kind)):
        if (artifact["id"] != digest(encoded([artifact["stage"], artifact["handle"], "host", artifact["parent_id"]]).encode("utf-8"))
                or artifact["record"]["owner"] != owner or artifact["record"]["kind"] != kind):
            raise ValueError("Reconciliation artifact identity/producer mismatch")
    old_artifacts = {x["id"] for x in before["artifacts"]}
    new_ids = {request["id"], candidate["id"]}
    route = None
    if routing:
        identity = report.get("routing_artifact")
        if type(identity) is not str or re.fullmatch(r"[0-9a-f]{64}", identity) is None:
            raise ValueError("Invalid routing artifact identity")
        route = board.artifact(identity)
        if (route["stage"] != "ROUTE" or route["parent_id"] != candidate["id"]
                or route["source_ids"] != [source_id] or route["author"] != "host"
                or route["title"] != ROUTE_TITLE or route["summary"] != ROUTE_SUMMARY
                or route["id"] != digest(encoded(["ROUTE", route["handle"], "host", candidate["id"]]).encode("utf-8"))
                or route["record"]["owner"] != "bezoar" or route["record"]["kind"] != "holder-routing"):
            raise ValueError("Routing artifact identity/lineage mismatch")
        new_ids.add(route["id"])
    if (len(new_ids) != (3 if routing else 2) or new_ids & old_artifacts
            or {x["id"] for x in after["artifacts"]} != old_artifacts | new_ids):
        raise ValueError("Reconciliation has unexpected task-bound artifacts")
    req = request["record"]["payload"]
    ingestion = req["ingest_handle"]
    ingested = read_ingest(board.plumber.records, ingestion, "nurarihyon")
    expected_sources = [{"id": source_id + ".txt", "path": source_id + ".txt",
                         "sha256": digest(raw), "bytes": len(raw), "text": raw.decode("utf-8")}]
    if (req.get("schema") != "kernel-eat-request/v1" or req.get("question") != question(task["id"])
            or req.get("sources") != expected_sources or ingested["sources"] != expected_sources
            or request["record"]["links"] != [ingestion]):
        raise ValueError("Reconciliation request input mismatch")
    # Existing typed reader checks record types/links/current implementation pins,
    # then reruns the actual Yggdrasil and Bezoar functions on a separate root.
    reader = bezoar_capture.read if complete else read_reconciliation
    checked = reader(board.plumber.records, candidate["handle"], scratch=stage / "reconciliation-review")
    retained = board.plumber.records.read(checked["source"])["payload"]
    if (retained["raw_utf8"].encode("utf-8") != raw or retained["sha256"] != digest(raw)
            or checked["report"]["input_sha256"] != digest(raw)
            or report.get("reconciliation_sha256") != digest(canonical(checked["report"]))):
        raise ValueError("Recomputed reconciliation is not bound to this task")
    if route is not None:
        # Reopen both retained inputs and recompute in controller-owned scratch.
        # The worker cannot supply approval, suppress a finding, or invent prior history.
        checked_route = board.plumber.read_reconciliation_route(route["handle"], scratch=stage / "routing-review")
        proposal = checked_route["proposal"]
        if (proposal["current"] != candidate["handle"]
                or proposal["previous"] != routing["previous_handle"]
                or proposal["expected_source_sha256"] != digest(raw)
                or report.get("routing_sha256") != digest(canonical(proposal))
                or checked_route["authority"] != "NONE" or checked_route["activation"] != "NONE"
                or checked_route["dispatch"] != "PROPOSAL_ONLY"):
            raise ValueError("Recomputed routing is not bound to this task")
    previous = {x["path"]: x for x in tree_inventory(parent.home, max_bytes=policy["max_stage_bytes"])}
    current = {x["path"]: x for x in tree_inventory(board.home, max_bytes=policy["max_stage_bytes"])}
    for name, item in previous.items():
        if name != "pando.sqlite3" and current.get(name) != item:
            raise ValueError("Reconciliation changed an immutable file")
    allowed = {"captures/" + source_id + ".txt"}
    for handle in (ingestion, request["handle"], candidate["handle"], checked["source"], *capture_dependencies):
        owner, identity = handle.split(":")
        allowed.add(f"kernel/organs/{owner}/records/{identity}.json")
    if route is not None:
        owner, identity = route["handle"].split(":")
        allowed.add(f"kernel/organs/{owner}/records/{identity}.json")
    if set(current) - set(previous) - allowed:
        raise ValueError("Reconciliation added undeclared Board files")
    # Review adds a second bounded input copy, counted before publication.
    tree_inventory(stage, max_bytes=policy["max_stage_bytes"], max_files=policy["max_files"])
    return report
