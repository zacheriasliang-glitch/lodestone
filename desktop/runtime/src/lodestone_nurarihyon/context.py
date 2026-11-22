"""Source-backed retained context for an explicitly selected local state.

This is deterministic evidence retrieval. It does not infer utility, change a
plan, waive a check, approve work, or treat a previous failure as a prediction.
The existing export-based preparation command remains a separate operation.
"""
import time
import re
from pathlib import Path

from .common import canonical, digest, read_bytes, strict_json
from .controller import code_identity
from .inspection import ReadOnlyBoard
from .plans import MAX_SOURCE_BYTES, make_profile_plan
from .preparation import (_bounded_evidence, _select_lessons,
                          MAX_CONTEXT_BYTES, MAX_GROUPS)


def check_context_home(controller):
    """Require an explicit unaliased state path without reading host registration.

    Public users may inspect their own configured service state under the normal
    controller lock. Context never starts a worker or grants execution authority.
    Laboratory-specific live-state exclusions do not travel with this package.
    """
    home = controller.home
    requested = getattr(controller, "requested_home", home)
    if not home.is_absolute() or any(p.is_symlink() for p in (requested, *requested.parents)):
        raise ValueError("Context requires an explicit unaliased home")


def verify_context_events(home, events, tasks):
    """Check retained context identity against immutable task intent."""
    if len(events) > len(tasks):
        raise ValueError("More context events than tasks")
    by_id = {t["id"]: t for t in tasks}
    seen = set()
    for event in events:
        task = by_id.get(event["task_id"])
        if task is None or task["id"] in seen:
            raise ValueError("Orphan or duplicate context event")
        seen.add(task["id"])
        value = strict_json(event["payload"].encode(), MAX_CONTEXT_BYTES)
        if (type(value) is not dict
                or set(value) != {"schema", "context_sha256", "context_bytes", "source_sha256", "plan_sha256", "authority"}
                or value["schema"] != "nurarihyon-context-reference/v1"
                or value["authority"] != "NONE"
                or type(value["context_sha256"]) is not str
                or re.fullmatch(r"[0-9a-f]{64}", value["context_sha256"]) is None
                or type(value["context_bytes"]) is not int
                or not 0 < value["context_bytes"] <= MAX_CONTEXT_BYTES
                or value["source_sha256"] != task["source_sha"]
                or value["plan_sha256"] != task["plan_sha"]):
            raise ValueError("Invalid retained context reference")
        if (home / "contexts").is_symlink():
            raise ValueError("Context directory symlink refused")
        raw = read_bytes(home / "contexts" / (value["context_sha256"] + ".json"), MAX_CONTEXT_BYTES)
        if len(raw) != value["context_bytes"] or digest(raw) != value["context_sha256"]:
            raise ValueError("Captured context identity mismatch")
        context = strict_json(raw, MAX_CONTEXT_BYTES)
        if (type(context) is not dict
                or canonical(context) != raw
                or context.get("schema") != "nurarihyon-current-context/v1"
                or context.get("authority") != "NONE"
                or context.get("execution_allowed") is not False
                or context.get("source_sha256") != task["source_sha"]
                or context.get("plan_sha256") != task["plan_sha"]
                or digest(canonical(context.get("plan"))) != task["plan_sha"]
                or context.get("active_generation") != task["base_generation"]
                or context.get("code_sha256") != task["code_sha"]
                or context.get("policy_sha256") != task["policy_sha"]):
            raise ValueError("Retained context no longer matches task intent")


def context_locked(controller, raw, plan, *, memory_root=None, limit=4):
    """Caller holds controller.locked(); evidence is read, never activated."""
    end = time.monotonic() + 10
    def deadline():
        if time.monotonic() > end:
            raise ValueError("Current context cooperative deadline exceeded")
    check_context_home(controller)
    if type(limit) is not int or not 1 <= limit <= MAX_GROUPS:
        raise ValueError("Context limit must be1..8")
    tasks, lessons, events, generation, stopped = _bounded_evidence(controller)
    deadline()
    integrity = controller.verify()
    deadline()
    recomputed = make_profile_plan(
        raw, ReadOnlyBoard(controller.active_directory() / "pando").settings())
    if canonical(recomputed) != canonical(plan):
        raise ValueError("Context plan differs from captured source and current view")
    selected, counts = _select_lessons(
        controller, plan, tasks, lessons, events, limit, end)
    current_code = code_identity()
    for group in selected:
        group["version_relations"] = []
        for item in group["provenance"]:
            deadline()
            old = tasks[item["task_id"]]
            # Older projected task rows do not contain policy_sha. Read the
            # existing task record; do not infer it from current policy.
            old_status = controller.status(old["id"])
            group["version_relations"].append({
                "task_id": item["task_id"], "lesson_id": item["lesson_id"],
                "code_relation": "SAME_CODE" if item["code_sha256"] == current_code else "CODE_CHANGED",
                "policy_relation": "SAME_POLICY" if old_status["policy_sha"] == controller.policy_sha else "POLICY_CHANGED"})
    memory = None
    if memory_root is not None:
        from .outcome_memory import retrieve_outcomes
        memory = retrieve_outcomes(controller, plan, memory_root, limit=limit)
    deadline()
    result = {
        "schema": "nurarihyon-current-context/v1",
        "authority": "NONE", "execution_allowed": False,
        "source_sha256": digest(raw), "plan_sha256": digest(canonical(plan)),
        "active_generation": generation, "stopped": stopped,
        "policy_sha256": controller.policy_sha, "code_sha256": current_code,
        "integrity": integrity, "plan": plan,
        "lesson_context": selected, "retrieval": counts,
        "outcome_memory": memory,
        "evidence_set_sha256": digest(canonical({"tasks": tasks, "lessons": lessons, "events": events})),
        "deadline_seconds": 10, "deadline_enforcement": "cooperative_between_bounded_operations",
        "consumption": "Context only; exact source, priority, approval and acceptance remain mandatory",
        "learning_claim": "NONE: fixed retrieval and provenance validation; applicability is not inferred",
        "current_baseline_valid": True,
        "prior_failure_effect": "No veto or retry; fresh compatible plans remain eligible for explicit approval",
    }
    if len(canonical(result)) > MAX_CONTEXT_BYTES:
        raise ValueError("Combined retained context exceeds64KiB")
    return result


def prepare_current(controller, source, *, memory_root=None, limit=4):
    """Read one consistent current view without an archive or state writes."""
    check_context_home(controller)
    raw = read_bytes(source, MAX_SOURCE_BYTES)
    with controller.locked():
        plan = make_profile_plan(
            raw, ReadOnlyBoard(controller.active_directory() / "pando").settings())
        return context_locked(controller, raw, plan, memory_root=memory_root, limit=limit)
