"""Source-verified terminal observations in the existing local-memory catalogue.

Callers MUST hold controller.locked() throughout either public operation and own
the dedicated memory directory exclusively. This module never acquires that lock
recursively. It does not execute tasks, approve plans, set priority, learn utility,
or treat a prior outcome as applicable to a new task. Trusted local Python and a
quiescent, caller-owned store are required; these checks are not an OS sandbox.
"""
from contextlib import contextmanager
from datetime import datetime
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys
import time
from types import SimpleNamespace

from .common import canonical, digest, file_lock, read_bytes, strict_json, tree_inventory
from .inspection import ReadOnlyBoard
from .plans import (LESSONS, MAX_SOURCE_BYTES, PLAN_SCHEMA, TOOLKIT,
                    make_profile_plan, validate_profile_plan)

MAX_RECORDS = 64
MAX_MEMORY_BYTES = 1048576
MAX_RECORD_BYTES = 16384
MAX_EVIDENCE_BYTES = 8388608
MAX_FIELD_BYTES = 65536
MAX_EVENTS = 4096
MAX_REPORT_BYTES = 65536
MAX_SECONDS = 10
KIND = "nurarihyon_outcome"
SCHEMA = "nurarihyon-outcome-memory/v1"
ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools/local-memory/src/lodestone_memory.py"
SHA = re.compile(r"[0-9a-f]{64}\Z")
IDENTITY = re.compile(r"[0-9a-f]{32}\Z")
RECORD_FIELDS = {"id", "title", "body", "source", "kind", "tags", "confidence",
                 "created_at", "updated_at", "content_hash", "superseded_by"}
ACCEPTANCE = "Independent structural/source/target/recovery validation; no learned utility claim"


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _deadline(end):
    _require(time.monotonic() <= end, "Outcome memory cooperative deadline exceeded")


def _sha(value):
    return type(value) is str and SHA.fullmatch(value) is not None


def _identity(value):
    return type(value) is str and IDENTITY.fullmatch(value) is not None


def _overlap(left, right):
    return left == right or left in right.parents or right in left.parents


def _plain_path(value):
    path = Path(value)
    _require(path.is_absolute() and ".." not in path.parts, "Explicit absolute path required")
    # Lexical normalization first; never resolve a symlink into an allowed root.
    path = Path(os.path.abspath(path))
    for ancestor in (*reversed(path.parents), path):
        _require(not ancestor.is_symlink(), "Symlink path component refused")
    return path


def _memory_path(controller, value):
    candidate = Path(value)
    _require(candidate.is_absolute() and ".." not in candidate.parts,
             "Memory root must be an explicit absolute path")
    candidate = Path(os.path.abspath(candidate))
    # Dedicated user-owned memory must not overlap code or durable controller state.
    forbidden = (controller.home, ROOT)
    _require(not any(_overlap(candidate, p) for p in forbidden),
             "Memory root overlaps source or controller state")
    result = _plain_path(candidate)
    _require(not any((p / ".git").exists() or (p / ".git").is_symlink()
                     for p in (result, *result.parents)), "Memory root must be outside source checkouts")
    from .context import check_context_home
    check_context_home(SimpleNamespace(home=result))
    _require(not result.exists() or result.is_dir(), "Memory root must be a directory")
    return result


def _memory_api():
    path = _plain_path(TOOL)
    raw = read_bytes(path, 262144)
    pin = digest(raw)
    name = "_nurarihyon_local_memory_" + pin
    # Execute only the fixed trusted repository dependency; no source body or
    # catalogue entry is executable. Compiling the captured bytes avoids .pyc writes.
    module = importlib.util.module_from_spec(importlib.util.spec_from_file_location(name, path))
    previous = sys.modules.get(name)
    sys.modules[name] = module  # dataclasses resolves its module during definition.
    try:
        exec(compile(raw, str(path), "exec"), module.__dict__)
    finally:
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous
    return module, pin


def _catalog(root, api, end, *, load=True):
    """Bound and strictly parse all bytes BEFORE the existing loader reads them."""
    if not root.exists():
        return [], 0
    entries = []
    for path in root.iterdir():
        _deadline(end)
        _require(len(entries) < 5 and path.name in {
            "records.jsonl", "links.jsonl", "state.json", "packets", "catalogue.lock"},
            "Only a dedicated local-memory outcome catalogue is admitted")
        entries.append(path)
    total, captured = 0, {}
    for path in entries:
        _deadline(end)
        mode = path.lstat().st_mode
        if path.name == "packets":
            _require(stat.S_ISDIR(mode) and not any(path.iterdir()),
                     "Outcome catalogue packets must be an empty regular directory")
            continue
        _require(stat.S_ISREG(mode) and path.stat().st_nlink == 1,
                 "Memory catalogue files must be regular and unaliased")
        total += path.stat().st_size
        _require(total <= MAX_MEMORY_BYTES, "Memory catalogue exceeds byte bound")
        captured[path.name] = read_bytes(path, MAX_MEMORY_BYTES)
        if path.name == "catalogue.lock":
            _require(captured[path.name] == b"", "Memory lock must remain empty")
    _require(not captured.get("links.jsonl", b"").strip(), "Outcome catalogue has no inferred links")
    if "state.json" in captured:
        state = strict_json(captured["state.json"], 4096)
        _require(type(state) is dict and set(state) == {"version", "created_at"}
                 and state["version"] == "1.0.0" and type(state["created_at"]) is str
                 and len(state["created_at"]) <= 64, "Invalid local-memory state")
    records = []
    for line in captured.get("records.jsonl", b"").splitlines():
        _deadline(end)
        if not line.strip():
            continue
        _require(len(records) < MAX_RECORDS, "Memory record count exceeds bound")
        record = strict_json(line, MAX_RECORD_BYTES)
        _require(type(record) is dict and set(record) == RECORD_FIELDS,
                 "Memory record schema mismatch")
        _require(all(type(record[key]) is str for key in (
            "id", "title", "body", "source", "kind", "created_at", "updated_at", "content_hash"))
            and re.fullmatch(r"[0-9a-f]{16}", record["id"]) and _sha(record["content_hash"])
            and type(record["tags"]) is list and len(record["tags"]) <= 8
            and all(type(tag) is str and len(tag) <= 64 for tag in record["tags"])
            and type(record["confidence"]) in (int, float) and 0 <= record["confidence"] <= 1
            and (record["superseded_by"] is None or type(record["superseded_by"]) is str),
            "Invalid local-memory record metadata")
        records.append(record)
    _require(len({r["id"] for r in records}) == len(records), "Duplicate memory record identity")
    if load:
        loaded = api.load_records(root)
        _require(canonical(loaded) == canonical(records), "Memory catalogue changed while loading")
    for name, raw in captured.items():
        _require(read_bytes(root / name, MAX_MEMORY_BYTES) == raw,
                 "Memory catalogue changed during validation")
    return records, total


@contextmanager
def _catalogue_lock(root, api, end, *, create):
    # Structural checks precede any lock creation. Writers serialize through the
    # existing OS-held file_lock; readers never create a store or its lock.
    _catalog(root, api, end, load=False)
    if not root.exists():
        if not create:
            yield
            return
        root.mkdir(mode=0o700)
    lock = root / "catalogue.lock"
    if not lock.exists():
        _require(not any(root.iterdir()), "Existing outcome catalogue lacks its writer lock; refuse adoption")
        if not create:
            yield
            return
    else:
        _require(not lock.is_symlink() and lock.is_file() and lock.stat().st_nlink == 1
                 and lock.stat().st_size == 0, "Invalid memory writer lock")
    with file_lock(lock, timeout=0):
        _deadline(end)
        yield


def _history(controller, end):
    """Bound complete rows before inherited verify() materializes them."""
    _plain_path(controller.home)
    _require(controller.database.stat().st_size <= controller.policy["max_state_bytes"],
             "Control database exceeds byte bound")
    schemas = {
        "tasks": {"sequence", "id", "dedup_key", "intent_sha", "plan", "plan_sha", "source_sha",
                  "base_generation", "policy_sha", "code_sha", "priority", "state", "approval",
                  "attempts", "created", "updated", "result", "error"},
        "generations": {"id", "parent", "task_id", "manifest_sha", "created"},
        "events": {"sequence", "task_id", "time", "kind", "payload"},
        "metadata": {"key", "value"},
    }
    counts = {"tasks": controller.policy["max_tasks"],
              "generations": controller.policy["max_tasks"] + 1, "events": MAX_EVENTS, "metadata": 8}
    with controller.connection() as db:
        total = 0
        for table, fields in schemas.items():
            _deadline(end)
            _require({r["name"] for r in db.execute(f"PRAGMA table_info({table})")} == fields,
                     "Control evidence schema mismatch")
            _require(db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] <= counts[table],
                     "Control evidence count exceeds bound")
            for field in fields:
                _deadline(end)
                maximum, size = db.execute(
                    f"SELECT max(length(CAST({field} AS BLOB))), sum(length(CAST({field} AS BLOB))) FROM {table}"
                ).fetchone()
                _require(maximum is None or maximum <= MAX_FIELD_BYTES, "Control field exceeds byte bound")
                total += size or 0
                _require(total <= MAX_EVIDENCE_BYTES, "Control evidence exceeds byte bound")
        tasks = {r["id"]: dict(r) for r in db.execute("SELECT * FROM tasks")}
        generations = {r["id"]: dict(r) for r in db.execute("SELECT * FROM generations")}
        events = [dict(r) for r in db.execute("SELECT * FROM events ORDER BY sequence")]
    for task in tasks.values():
        _require(_identity(task["id"]) and _identity(task["base_generation"])
                 and all(_sha(task[key]) for key in ("intent_sha", "plan_sha", "source_sha", "policy_sha", "code_sha"))
                 and type(task["plan"]) is str and (task["result"] is None or type(task["result"]) is str),
                 "Invalid retained task identity")
    for generation in generations.values():
        _require(_identity(generation["id"]) and _sha(generation["manifest_sha"])
                 and (generation["parent"] is None or _identity(generation["parent"]))
                 and (generation["task_id"] is None or _identity(generation["task_id"])),
                 "Invalid retained generation identity")
    for event in events:
        _require(type(event["sequence"]) is int and event["sequence"] > 0
                 and (event["task_id"] is None or event["task_id"] in tasks)
                 and all(type(event[key]) is str for key in ("kind", "payload", "time")),
                 "Invalid event identity")
    controller.verify()
    _deadline(end)
    return tasks, generations, events


def _facts(controller, history, task_id, event_sequence=None, *, end):
    _deadline(end)
    tasks, generations, events = history
    _require(_identity(task_id) and task_id in tasks, "Unknown outcome task")
    task = tasks[task_id]
    plan = strict_json(task["plan"].encode(), MAX_FIELD_BYTES)
    _require(digest(task["plan"].encode()) == task["plan_sha"], "Task plan pin mismatch")
    raw = read_bytes(controller.home / "inputs" / (task["source_sha"] + ".json"), MAX_SOURCE_BYTES)
    _require(digest(raw) == task["source_sha"], "Task source pin mismatch")
    _require(type(plan) is dict and type(plan.get("baseline")) is dict, "Invalid historical plan")
    baseline = plan["baseline"]
    validate_profile_plan(plan, raw, {"name": baseline.get("board_name"),
                                     "shelves": baseline.get("enabled_shelves")})
    _require(plan["source_sha256"] == task["source_sha"], "Historical source/plan mismatch")
    _require(type(task["priority"]) is int and 0 <= task["priority"] <= 9
             and task["intent_sha"] == digest(canonical([TOOLKIT, task["source_sha"], task["priority"]])),
             "Historical task intent mismatch")
    terminal = [e for e in events if e["task_id"] == task_id and e["kind"] in (*LESSONS, "PUBLISHED")]
    if event_sequence is None:
        _require(task["state"] in ("COMPLETED", "REJECTED") and bool(terminal),
                 "Only evidenced COMPLETED or REJECTED tasks can be retained")
        _require(type(task["result"]) is str, "Terminal task result is missing")
        event = terminal[-1]
        _require(task["result"] == event["payload"],
                 "Terminal task result does not match its event")
        _require((task["state"] == "COMPLETED") == (event["kind"] == "PUBLISHED"),
                 "Terminal task state does not match its event")
    else:
        found = [e for e in terminal if e["sequence"] == event_sequence]
        _require(type(event_sequence) is int and len(found) == 1, "Retained terminal event is missing")
        event = found[0]
    result = strict_json(event["payload"].encode(), MAX_FIELD_BYTES)
    expected = "COMPLETED" if event["kind"] == "PUBLISHED" else event["kind"]
    _require(type(result) is dict and result.get("outcome") == expected,
             "Terminal event outcome mismatch")
    approvals = [e for e in events if e["task_id"] == task_id and e["kind"] == "APPROVED"
                 and e["sequence"] < event["sequence"]]
    _require(bool(approvals), "Terminal observation lacks preceding approval evidence")
    approved = strict_json(approvals[-1]["payload"].encode(), MAX_FIELD_BYTES)
    _require(type(approved) is dict and approved.get("plan_sha256") == task["plan_sha"]
             and approved.get("policy_sha256") == task["policy_sha"]
             and approved.get("code_sha256") == task["code_sha"] and approved.get("actor") == "host",
             "Historical approval evidence does not bind task intent")
    output = None
    if expected == "COMPLETED":
        _require(result.get("acceptance") == ACCEPTANCE, "Unsupported completion acceptance claim")
        generation_id = result.get("generation")
        _require(_identity(generation_id) and generation_id in generations, "Published generation missing")
        generation = generations[generation_id]
        _require(generation["task_id"] == task_id and generation["parent"] == task["base_generation"]
                 and result.get("parent") == task["base_generation"], "Publication lineage mismatch")
        report = controller._validate_stage(controller.home / "generations" / generation_id, task, plan)
        _deadline(end)
        _require(report["candidate_artifact"] == result.get("candidate_artifact"),
                 "Published result differs from accepted output")
        output = {"kind": "published_generation", "generation": generation_id,
                  "manifest_sha256": generation["manifest_sha"],
                  "candidate_artifact": result["candidate_artifact"],
                  "accepted_view_sha256": digest(canonical(plan["candidate"]))}
    elif result.get("retained_stage") is not None:
        relative = result["retained_stage"]
        _require(type(relative) is str and re.fullmatch(r"generations/[0-9a-f]{32}", relative)
                 and relative.split("/")[1] not in generations, "Invalid rejected output location")
        inventory = tree_inventory(controller.home / relative,
                                   max_bytes=controller.policy["max_stage_bytes"],
                                   max_files=controller.policy["max_files"])
        _deadline(end)
        output = {"kind": "rejected_stage", "relative_path": relative,
                  "inventory_sha256": digest(canonical(inventory)), "file_count": len(inventory),
                  "bytes": sum(item["bytes"] for item in inventory)}
    metrics = result.get("metrics")
    _require(type(metrics) is dict, "Terminal event has no metrics object")
    observed = {}
    for name in ("total_cpu_seconds", "wall_seconds", "stage_bytes", "peak_sampled_rss_bytes", "exit_code", "work_started"):
        if name in metrics:
            value = metrics[name]
            _require(type(value) is bool if name == "work_started" else type(value) in (int, float),
                     "Invalid observed metric")
            observed[name] = value
    _require("0" * 32 in generations and task["base_generation"] in generations,
             "Source home identity evidence is missing")
    return {"schema": SCHEMA, "task_id": task_id, "toolkit": plan["toolkit"],
            "operator": plan["operator"], "source_sha256": task["source_sha"],
            "plan_sha256": task["plan_sha"], "policy_sha256": task["policy_sha"],
            "code_sha256": task["code_sha"], "base_generation": task["base_generation"],
            "event_sequence": event["sequence"], "event_kind": event["kind"],
            "event_payload_sha256": digest(event["payload"].encode()),
            "approval_event_sequence": approvals[-1]["sequence"],
            "approval_payload_sha256": digest(approvals[-1]["payload"].encode()),
            "result_sha256": digest(event["payload"].encode()),
            "result_hash_scope": "terminal_event_payload_bytes",
            "acceptance": result.get("acceptance") if expected == "COMPLETED" else None,
            "home_identity": {"initial_manifest_sha256": generations["0" * 32]["manifest_sha"],
                              "baseline_manifest_sha256": generations[task["base_generation"]]["manifest_sha"],
                              "policy_sha256": task["policy_sha"]},
            "outcome": expected, "observed_metrics": observed, "output_evidence": output,
            "attempt_attribution": "terminal_event_only", "authority": "NONE",
            "applicability": "NOT_ESTABLISHED"}, plan


def _record(api, facts):
    return api.make_record(title="Nurarihyon " + facts["outcome"] + " " + facts["operator"],
                           body=canonical(facts).decode(),
                           source="nurarihyon://task/" + facts["task_id"] + "/event/" + str(facts["event_sequence"]),
                           kind=KIND, tags=[facts["toolkit"], facts["operator"], "runtime-outcome"],
                           confidence=1.0)  # Required API metadata; fixed, unused, not an outcome label.


def _verified_records(controller, history, api, records, end):
    result, excluded, seen = [], [], set()
    for record in records:
        _deadline(end)
        try:
            _require(record["kind"] == KIND and record["superseded_by"] is None,
                     "Foreign or superseded memory record")
            saved = strict_json(record["body"].encode(), MAX_RECORD_BYTES)
            _require(type(saved) is dict and saved.get("schema") == SCHEMA, "Unsupported outcome memory record")
            identity = (saved.get("task_id"), saved.get("event_sequence"))
            _require(_identity(identity[0]) and type(identity[1]) is int and identity not in seen,
                     "Invalid or duplicate outcome identity")
            facts, plan = _facts(controller, history, *identity, end=end)
            expected = _record(api, facts)
            for field in RECORD_FIELDS - {"created_at", "updated_at"}:
                _require(canonical(record[field]) == canonical(expected[field]),
                         "Memory record differs from original task/event/output evidence")
            _require(len(record["created_at"]) <= 64 and record["created_at"] == record["updated_at"],
                     "Invalid outcome indexing timestamp")
            stamp = datetime.fromisoformat(record["created_at"])
            _require(stamp.tzinfo is not None, "Outcome timestamp must declare its timezone")
        except (ValueError, OSError, KeyError, TypeError) as exc:
            _deadline(end)  # A deadline is a whole-operation refusal, not an excluded item.
            excluded.append({"record_id": record["id"], "status": "UNVERIFIED",
                             "reason": str(exc)[:240], "authority": "NONE"})
        else:
            seen.add(identity)
            result.append((record, facts, plan))
    return result, excluded


def retain_outcome(controller, task_id, memory_root):
    """Export one actual terminal event. Caller holds controller.locked()."""
    from .context import check_context_home
    check_context_home(controller)
    end = time.monotonic() + MAX_SECONDS
    root = _memory_path(controller, memory_root)
    api, dependency = _memory_api()
    history = _history(controller, end)
    facts, _ = _facts(controller, history, task_id, end=end)
    with _catalogue_lock(root, api, end, create=True):
        return _retain_locked(controller, root, api, dependency, history, facts, end)


def _retain_locked(controller, root, api, dependency, history, facts, end):
    records, size = _catalog(root, api, end)
    verified, excluded = _verified_records(controller, history, api, records, end)
    record = _record(api, facts)
    duplicate = next((r for r in records if r["source"] == record["source"]
                      and r["content_hash"] == record["content_hash"]), None)
    _require(duplicate is None or any(r["id"] == duplicate["id"] for r, _, _ in verified),
             "Conflicting unverified outcome record; no automatic repair")
    if duplicate is None:
        encoded_size = len((json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode())
        _require(encoded_size <= MAX_RECORD_BYTES and len(records) < MAX_RECORDS,
                 "Outcome memory record capacity exhausted")
        _require(size + encoded_size + 4096 <= MAX_MEMORY_BYTES,
                 "Outcome memory byte capacity exhausted; no automatic deletion")
        _deadline(end)
        record_id, added = api.add_record(root, record)
    else:
        record_id, added = duplicate["id"], False
    checked, final_size = _catalog(root, api, end)
    final, excluded = _verified_records(controller, history, api, checked, end)
    _require(any(r["id"] == record_id for r, _, _ in final), "Appended outcome did not reverify")
    return {"schema": "nurarihyon-outcome-retention/v1", "status": "RETAINED" if added else "ALREADY_RETAINED",
            "record_id": record_id, "task_id": facts["task_id"], "event_sequence": facts["event_sequence"],
            "outcome": facts["outcome"], "memory_engine_sha256": dependency,
            "record_count": len(checked), "catalogue_bytes": final_size,
            "excluded": excluded, "authority": "NONE", "learned_parameters": False,
            "confidence_semantics": "fixed_one_unused", "deadline_seconds": MAX_SECONDS,
            "deadline_enforcement": "cooperative_between_bounded_operations"}


def retrieve_outcomes(controller, plan, memory_root, limit=4):
    """Reopen every stored source proof, then select inert same-operator context.

    Caller holds controller.locked() and binds the supplied plan to its original
    new source bytes. This function checks plan structure, not that external file.
    """
    from .context import check_context_home
    check_context_home(controller)
    end = time.monotonic() + MAX_SECONDS
    _require(type(limit) is int and 1 <= limit <= 8, "Outcome retrieval limit must be 1..8")
    _require(type(plan) is dict and plan.get("schema") == PLAN_SCHEMA and plan.get("toolkit") == TOOLKIT
             and _sha(plan.get("source_sha256")), "Unsupported retrieval plan")
    raw = canonical({"schema": "nurarihyon-profile-source/v1", "baseline": plan.get("baseline"),
                     "candidate": plan.get("candidate"), "rationale": plan.get("rationale")})
    baseline = plan.get("baseline")
    _require(type(baseline) is dict, "Retrieval plan has no baseline")
    expected = make_profile_plan(raw, {"name": baseline.get("board_name"), "shelves": baseline.get("enabled_shelves")})
    expected["source_sha256"] = plan["source_sha256"]
    _require(canonical(plan) == canonical(expected), "Retrieval plan structure mismatch")
    root = _memory_path(controller, memory_root)
    api, dependency = _memory_api()
    with _catalogue_lock(root, api, end, create=False):
        return _retrieve_locked(controller, plan, root, api, dependency, limit, end)


def _retrieve_locked(controller, plan, root, api, dependency, limit, end):
    from .controller import code_identity
    records, size = _catalog(root, api, end)
    history = _history(controller, end)
    verified, excluded = _verified_records(controller, history, api, records, end)
    current_code = code_identity()
    _deadline(end)
    current = ReadOnlyBoard(controller.active_directory() / "pando").settings()
    current_view = {"board_name": current["name"], "enabled_shelves": current["shelves"]}
    matched = []
    for record, facts, old_plan in verified:
        if (facts["toolkit"], facts["operator"]) != (plan["toolkit"], plan["operator"]):
            continue
        matched.append({"record_id": record["id"], "evidence": facts,
                        "match": "same_source" if facts["source_sha256"] == plan["source_sha256"] else "same_toolkit_and_operator",
                        "historical_baseline_equals_current": old_plan["baseline"] == current_view,
                        "historical_candidate_equals_new": old_plan["candidate"] == plan["candidate"],
                        "code_relation": "SAME_CODE" if facts["code_sha256"] == current_code else "CODE_CHANGED",
                        "policy_relation": "SAME_POLICY" if facts["policy_sha256"] == controller.policy_sha else "POLICY_CHANGED",
                        "applicability": "NOT_ESTABLISHED", "authority": "NONE"})
    matched.sort(key=lambda row: (row["match"] != "same_source", -row["evidence"]["event_sequence"], row["record_id"]))
    report = {"schema": "nurarihyon-outcome-context/v1", "status": "VERIFIED_CONTEXT",
              "plan_sha256": digest(canonical(plan)), "fresh_current_baseline": plan["baseline"] == current_view,
              "records_checked": len(records), "matching_records": len(matched), "records": matched[:limit],
              "omitted_matching_records": max(0, len(matched) - limit), "excluded": excluded,
              "selection": "MATCHED" if matched else "NONE",
              "selection_sha256": digest(canonical(matched[:limit])),
              "evidence_set_sha256": digest(canonical([facts for _, facts, _ in verified])),
              "home_identity": {"policy_sha256": controller.policy_sha,
                                "active_generation": controller.active_directory().name},
              "memory_engine_sha256": dependency, "catalogue_bytes": size,
              "method": "fixed_toolkit_operator_source_then_event_sequence", "learned_parameters": False,
              "authority": "NONE", "applicability": "NOT_ESTABLISHED", "admission_checked": False,
              "deadline_seconds": MAX_SECONDS, "deadline_enforcement": "cooperative_between_bounded_operations",
              "limitations": "Prior exact observations only; no utility prediction, approval, priority change or automatic retry"}
    _require(len(canonical(report)) <= MAX_REPORT_BYTES, "Outcome context exceeds byte bound")
    return report
