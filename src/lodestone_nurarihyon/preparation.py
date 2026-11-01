"""Prepare an inert plan with verified retained lessons from a pinned export.

Only the caller's NEW restoration directory is read as orchestration state.
Recovered code, source prose and lessons are never executed. This is deterministic
context retrieval, not model training or permission to submit/approve a task.
"""
from contextlib import contextmanager
import hashlib
from pathlib import Path
import resource
import re
import sqlite3
import sys
import time

from .common import canonical, create_file, digest, read_bytes, strict_json
from .controller import Controller, code_identity
from .inspection import ReadOnlyBoard
from .plans import (LESSONS, MAX_SOURCE_BYTES, SHA256, lesson_for,
                    make_profile_plan, validate_profile_plan)
from . import portable

MAX_CONTEXT_BYTES = 65536
MAX_LESSON_BYTES = 4096
MAX_PLAN_BYTES = 16384
MAX_EVENTS = 4096
MAX_GROUPS = 8
MAX_PROVENANCE_PER_GROUP = 4
MAX_EVENT_REFS = 4


class _SnapshotController(Controller):
    """Only for the closed isolated snapshot just verified by restore_package."""
    @contextmanager
    def connection(self):
        if self.database.is_symlink():
            raise ValueError("Snapshot database symlink refused")
        db = sqlite3.connect(self.database.as_uri() + "?mode=ro&immutable=1",
                             uri=True, timeout=1)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA query_only=ON")
            yield db
        finally:
            db.close()


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _sha(value):
    return type(value) is str and SHA256.fullmatch(value) is not None


def _sequence_digest(records):
    """Hash a canonical JSON array without materializing the full array bytes."""
    result = hashlib.sha256(b"[")
    for number, record in enumerate(records):
        if number:
            result.update(b",")
        result.update(canonical(record))
    result.update(b"]")
    return result.hexdigest()


def _bounded_evidence(controller):
    """Bound counts and text before materialization; no source/body instructions."""
    with controller.connection() as db:
        # Explicit reader budgets, not an assertion that every legitimate
        # history fits: pre-start rejections need not consume a worker attempt.
        limits = {"tasks": controller.policy["max_tasks"],
                  "lessons": controller.policy["max_tasks"] * controller.policy["max_attempts"],
                  "events": MAX_EVENTS, "generations": controller.policy["max_tasks"] + 1}
        for table, limit in limits.items():
            _require(db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] <= limit,
                     "Snapshot evidence count exceeds bound: " + table)
        for table, column, bound in (("lessons", "body", MAX_LESSON_BYTES),
                                     ("tasks", "plan", MAX_PLAN_BYTES),
                                     ("events", "payload", MAX_CONTEXT_BYTES)):
            largest = db.execute(f"SELECT max(length(CAST({column} AS BLOB))) FROM {table}").fetchone()[0]
            _require(largest is None or largest <= bound, "Snapshot evidence text exceeds bound")
        # Controller.verify reads complete task/generation rows. Refuse extra
        # columns and oversized fields before that existing verifier runs.
        schemas = {
            "tasks": {"sequence", "id", "dedup_key", "intent_sha", "plan", "plan_sha", "source_sha",
                      "base_generation", "policy_sha", "code_sha", "priority", "state", "approval",
                      "attempts", "created", "updated", "result", "error"},
            "generations": {"id", "parent", "task_id", "manifest_sha", "created"},
        }
        for table, fields in schemas.items():
            _require({r["name"] for r in db.execute(f"PRAGMA table_info({table})")} == fields,
                     "Snapshot evidence schema differs from declared contract")
            for field in fields:
                largest = db.execute(f"SELECT max(length(CAST({field} AS BLOB))) FROM {table}").fetchone()[0]
                _require(largest is None or largest <= MAX_CONTEXT_BYTES, "Snapshot evidence text exceeds bound")
        tasks = {r["id"]: dict(r) for r in db.execute(
            "SELECT id,plan,plan_sha,source_sha,base_generation,code_sha FROM tasks")}
        for task in tasks.values():
            _require(all(_sha(task[field]) for field in ("source_sha", "plan_sha", "code_sha")),
                     "Invalid retained task pin")
            _require(type(task["id"]) is str and re.fullmatch(r"[0-9a-f]{32}", task["id"])
                     and type(task["base_generation"]) is str
                     and re.fullmatch(r"[0-9a-f]{32}", task["base_generation"]),
                     "Invalid retained task/generation identity")
        lessons = [dict(r) for r in db.execute("SELECT id,task_id,body FROM lessons ORDER BY id")]
        # Do not materialize unrelated event payloads. Retained rejection events
        # are bounded again below, since retries can legitimately repeat a code.
        marks = ",".join("?" for _ in LESSONS)
        events = [dict(r) for r in db.execute(
            f"SELECT sequence,task_id,kind,payload FROM events WHERE kind IN ({marks}) ORDER BY sequence",
            tuple(LESSONS))]
        _require(len(events) <= limits["lessons"], "Failure event count exceeds bound")
        generation = controller._meta(db, "active_generation")
        stopped = controller._meta(db, "stop_requested") == "1" or (controller.home / "STOP").exists()
    return tasks, lessons, events, generation, stopped


def _select_lessons(controller, plan, tasks, lessons, events, limit, end):
    supporting = {}
    for event in events:
        portable._deadline(end)
        payload = strict_json(event["payload"].encode(), MAX_CONTEXT_BYTES)
        _require(type(payload) is dict and payload.get("outcome") == event["kind"],
                 "Failure event outcome does not match its kind")
        _require(event["task_id"] in tasks, "Failure event references missing task")
        supporting.setdefault((event["task_id"], event["kind"]), []).append({
            "sequence": event["sequence"], "kind": event["kind"],
            "payload_sha256": digest(event["payload"].encode())})
    groups = {}
    validated_tasks = {}
    for item in lessons:
        portable._deadline(end)
        task = tasks.get(item["task_id"])
        _require(task is not None, "Lesson references missing task")
        _require(type(item["id"]) is int and item["id"] > 0, "Invalid lesson identity")
        if task["id"] not in validated_tasks:
            _require(_sha(task["source_sha"]) and _sha(task["plan_sha"]) and _sha(task["code_sha"]),
                     "Invalid retained task pin")
            _require(digest(task["plan"].encode()) == task["plan_sha"], "Retained plan hash mismatch")
            old_plan = strict_json(task["plan"].encode(), MAX_PLAN_BYTES)
            _require(type(old_plan) is dict and type(old_plan.get("baseline")) is dict,
                     "Retained plan has no baseline")
            baseline = old_plan["baseline"]
            raw = read_bytes(controller.home / "inputs" / (task["source_sha"] + ".json"), MAX_SOURCE_BYTES)
            _require(digest(raw) == task["source_sha"], "Retained source hash mismatch")
            validate_profile_plan(old_plan, raw, {"name": baseline.get("board_name"),
                                                   "shelves": baseline.get("enabled_shelves")})
            _require(old_plan["source_sha256"] == task["source_sha"], "Retained plan/source link mismatch")
            validated_tasks[task["id"]] = old_plan
        old_plan = validated_tasks[task["id"]]
        lesson = strict_json(item["body"].encode(), MAX_LESSON_BYTES)
        _require(type(lesson) is dict and lesson.get("outcome") in LESSONS, "Unsupported retained lesson")
        expected = lesson_for(old_plan["toolkit"], lesson["outcome"], task["source_sha"])
        _require(canonical(lesson) == canonical(expected), "Retained lesson differs from source-bound contract")
        witnesses = supporting.get((task["id"], lesson["outcome"]), [])
        _require(bool(witnesses), "Retained lesson has no supporting failure event")
        # All retained lessons are checked, including those not selected.
        if old_plan["toolkit"] != plan["toolkit"] or old_plan["operator"] != plan["operator"]:
            continue
        key = (task["source_sha"], old_plan["operator"], lesson["outcome"])
        group = groups.setdefault(key, {
            "lesson": expected, "operator": old_plan["operator"],
            "match": "same_source" if task["source_sha"] == plan["source_sha256"] else "same_toolkit_and_operator",
            "provenance": [], "occurrence_count": 0,
            "applicability": "NOT_ESTABLISHED",
            "interpretation": "A contextual prior failure, not a prediction or an independently replicated result"})
        group["occurrence_count"] += 1
        group["provenance"].append({"lesson_id": item["id"], "task_id": task["id"],
                                     "source_sha256": task["source_sha"], "plan_sha256": task["plan_sha"],
                                     "code_sha256": task["code_sha"], "base_generation": task["base_generation"],
                                     "old_baseline_equals_new_baseline": old_plan["baseline"] == plan["baseline"],
                                     "old_candidate_equals_new_candidate": old_plan["candidate"] == plan["candidate"],
                                     "supporting_events": witnesses,
                                     "event_attribution": "Supporting task/outcome events; no unique attempt pairing inferred"})
    ranked = sorted(groups.values(), key=lambda g: (
        g["match"] != "same_source",
        -max(e["sequence"] for p in g["provenance"] for e in p["supporting_events"]),
        g["lesson"]["source_sha256"], g["lesson"]["outcome"]))
    selected = ranked[:limit]
    for group in selected:
        portable._deadline(end)
        complete = group["provenance"]  # Stable ascending lesson ID order.
        group["provenance_sha256"] = _sequence_digest(complete)
        group["provenance_omitted"] = max(0, len(complete) - MAX_PROVENANCE_PER_GROUP)
        bounded = []
        for original in reversed(complete[-MAX_PROVENANCE_PER_GROUP:]):
            item = dict(original)
            events = original["supporting_events"]  # Stable ascending sequence.
            item["supporting_events_count"] = len(events)
            item["supporting_events_omitted"] = max(0, len(events) - MAX_EVENT_REFS)
            item["supporting_events_sha256"] = _sequence_digest(events)
            item["supporting_events"] = list(reversed(events[-MAX_EVENT_REFS:]))
            bounded.append(item)
        group["provenance"] = bounded
    return selected, {"lessons_checked": len(lessons), "matching_groups": len(ranked),
                           "selected_groups": min(limit, len(ranked)),
                           "unselected_groups": max(0, len(ranked) - limit)}


def prepare(package, descriptor, source, output, *, limit=4):
    """Restore to a new owned directory and save an inert, source-bound review.

    Never imports restored code or calls submit/approve/run. Existing portable
    checks bound and verify archive content. Failure preserves the new output.
    """
    _require(type(limit) is int and 1 <= limit <= MAX_GROUPS, "Lesson group limit must be1..8")
    start, cpu = time.monotonic(), time.process_time()
    end = start + portable.MAX_SECONDS
    package = portable._path(package, directory=True)
    descriptor = portable._path(descriptor)
    source = portable._path(source)
    output = portable._path(output, new=True)
    _require(not any(portable._overlap(output, p) for p in (package, descriptor, source)),
             "Preparation output must not overlap its inputs")
    raw = read_bytes(source, MAX_SOURCE_BYTES)
    pin = read_bytes(descriptor, 128)
    output.mkdir(mode=0o700)
    try:
        create_file(output / "source.json", raw)
        recovered = portable.restore_package(package, descriptor, output / "snapshot")
        portable._deadline(end)
        controller = _SnapshotController(recovered["home"])
        tasks, lessons, events, generation, stopped = _bounded_evidence(controller)
        integrity = controller.verify()
        portable._deadline(end)
        plan = make_profile_plan(raw, ReadOnlyBoard(controller.active_directory() / "pando").settings())
        selected, counts = _select_lessons(controller, plan, tasks, lessons, events, limit, end)
        _require(read_bytes(source, MAX_SOURCE_BYTES) == raw and read_bytes(descriptor, 128) == pin,
                 "Preparation inputs changed during capture")
        # Recheck the full census, including added and excluded paths.
        # Inspection must not modify the restored code/state.
        manifest = strict_json(portable._read(output / "snapshot/metadata/package.json", portable.MAX_BYTES),
                               portable.MAX_BYTES)
        actual = {}
        for root in ("runtime", "state"):
            census = portable._scan(output / "snapshot" / root, end)
            _require(not census["excluded"], "Restored snapshot gained excluded paths")
            for item in census["files"]:
                name = root + "/" + item["path"]
                actual[name] = portable._pin(output / "snapshot" / name, end)
        _require(actual == manifest["files"], "Restored snapshot changed during preparation")
        report = {"schema": "nurarihyon-preparation/v1", "status": "PREPARED_INERT",
                  "authority": "NONE", "execution_allowed": False, "task_created": False,
                  "admission_checked": False,
                  "activation": "Existing submit/approve path must recompute against current state and code",
                  "source_sha256": digest(raw), "snapshot_descriptor_sha256": recovered["descriptor_sha256"],
                  "snapshot_active_generation": generation, "snapshot_stopped": stopped,
                  "snapshot_integrity": integrity, "policy_sha256": controller.policy_sha,
                  "preparer_code_sha256": code_identity(), "plan": plan, "plan_sha256": digest(canonical(plan)),
                  "lesson_context": selected, "retrieval": {**counts, "method": "fixed toolkit/operator then source identity; newest event tie break",
                                                            "learned_parameters": False},
                  "costs": {"wall_seconds": time.monotonic() - start,
                            "cpu_seconds": time.process_time() - cpu,
                            "process_lifetime_peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == "darwin" else 1024),
                            "restored_code_state_bytes": recovered["restored_file_bytes"],
                            "restored_code_state_files": recovered["file_count"],
                            "model_calls": 0, "training_updates": 0,
                            "limits": {"wall_seconds": portable.MAX_SECONDS, "context_bytes": MAX_CONTEXT_BYTES,
                                       "archive_logical_bytes": portable.MAX_BYTES, "events": MAX_EVENTS}},
                  "limitations": ["Read-only inspection of an isolated verified export, not a live-state view",
                                  "Context reuse is engineered retrieval, not learned transfer or useful-task acceptance",
                                  "Whole-harness acceptance and independent Claude review are separate"]}
        body = canonical(report) + b"\n"
        _require(len(body) <= MAX_CONTEXT_BYTES, "Preparation context exceeds byte bound")
        portable._deadline(end)
        create_file(output / "PREPARATION.json", body)
        return report
    except Exception as exc:
        # This directory is ours and new; originals and failed snapshots remain.
        try:
            create_file(output / "PREPARATION-FAILED.json", canonical({
                "status": "PREPARATION_FAILED", "error": type(exc).__name__,
                "code": getattr(exc, "code", None), "authority": "NONE", "task_created": False,
                "retained_output": True, "automatic_retry": False}) + b"\n")
        except Exception:
            # Preserve the primary failure even if storage cannot retain a
            # receipt. Never echo arbitrary I/O exception content or secrets.
            if hasattr(exc, "add_note"):
                exc.add_note("Preparation failure receipt could not be written; isolated output retained.")
        raise
