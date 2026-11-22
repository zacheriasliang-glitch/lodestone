"""Deterministic plans for explicit Pando view-profile changes.

Pure functions over caller-supplied values: no file, network, clock, random,
environment or process access, and caller objects are never modified. A plan
is an inert proposal. Approval, execution, acceptance and publication belong
to the application control core, which must recompute the plan from the
captured source bytes and current view instead of trusting stored fields.

Plan identity is the SHA-256 of its canonical JSON (sorted keys, compact
separators, UTF-8), the same form as lodestone_machinery.records.canonical.
Baseline and candidate use the operator names consumed by Board.apply_profile;
enabled_shelves order is significant and preserved.
"""
import hashlib
import json
import math
import re

SOURCE_SCHEMA = "nurarihyon-profile-source/v1"
PLAN_SCHEMA = "nurarihyon-plan/v1"
LESSON_SCHEMA = "nurarihyon-lesson/v1"
TOOLKIT = "pando.profile/v1"
AUTHORITY = "PROPOSAL_ONLY"
MAX_SOURCE_BYTES = 4096  # The whole capture must fit the kernel's 4,096-character quote bound.
MAX_RATIONALE_BYTES = 1024
MAX_NAME_BYTES = 120  # Board.apply_profile board name bound.
# Mirrors of lodestone_pando.store values; tests compare them with the originals.
SHELVES = ("commons", "memory", "research", "workshop", "samsara")
PARTICIPANT_IDS = ("host", "operator", "reviewer")  # Default Pando attribution; custom boards may declare more.
OPERATORS = ("board_name", "enabled_shelves")
VIEW_KEYS = {"board_name": "name", "enabled_shelves": "shelves"}  # Board.settings names
MAX_SEQUENCE = 2 ** 63 - 1  # SQLite INTEGER range
TASK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")  # ASCII: Python and SQLite BINARY order agree
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
LESSONS = {
    "SOURCE_DRIFT": (
        "Captured source bytes differ from the source_sha256 bound into the plan.",
        "Capture the intended source again and create a new plan; the earlier approval does not transfer."),
    "STALE_BASELINE": (
        "The current Board view no longer equals the plan baseline.",
        "Write a new profile source against the current view; the new plan needs its own approval."),
    "RESOURCE_LIMIT": (
        "A bounded operation reached its CPU, wall-time, memory or output limit.",
        "Review the inputs and limits; a repeat needs a new explicit host decision and limits are not raised automatically."),
    "REJECTED_VALIDATION": (
        "Independent recomputation or an existing contract check rejected the plan or its outputs.",
        "Inspect the receipt, correct the source, then create and approve a new plan."),
}


class PlanError(ValueError):
    """Rejected input; code is SOURCE_DRIFT, STALE_BASELINE, REJECTED_VALIDATION,
    INVALID_TASK or INVALID_LESSON. Messages name fields, never echo content."""

    def __init__(self, code, message):
        self.code = code
        super().__init__(f"{code}: {message}")


def _require(value, message, code="REJECTED_VALIDATION"):
    if not value:
        raise PlanError(code, message)


def _text(value, label, limit):
    _require(type(value) is str and bool(value.strip()), label + " must be nonempty text")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise PlanError("REJECTED_VALIDATION", label + " is not encodable as UTF-8") from None
    _require(size <= limit, f"{label} exceeds {limit} UTF-8 bytes")
    return value


def _shelves(value, label):
    _require(type(value) is list and 1 <= len(value) <= len(SHELVES)
             and all(type(s) is str and s in SHELVES for s in value)
             and len(set(value)) == len(value),
             label + " must be a nonempty ordered list of distinct known shelves")
    return list(value)


def _view(value, label):
    _require(type(value) is dict and set(value) == set(OPERATORS), label + " fields mismatch")
    return {"board_name": _text(value["board_name"], label + " board_name", MAX_NAME_BYTES),
            "enabled_shelves": _shelves(value["enabled_shelves"], label + " enabled_shelves")}


def _unique(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "duplicate JSON key")
        result[key] = value
    return result


def _nonfinite(token):
    raise PlanError("REJECTED_VALIDATION", "nonfinite JSON number")


def _float(token):
    value = float(token)
    _require(math.isfinite(value), "nonfinite JSON number")
    return value


def _typed_equal(a, b):
    # Same semantics as lodestone_machinery.organs._kernel.typed_equal.
    if type(a) is not type(b):
        return False
    if type(a) is dict:
        return a.keys() == b.keys() and all(_typed_equal(a[k], b[k]) for k in a)
    if type(a) is list:
        return len(a) == len(b) and all(_typed_equal(x, y) for x, y in zip(a, b))
    return a == b


def parse_profile_source(raw):
    """Strictly parse profile-source bytes; return fresh validated fields.

    Result: {"schema", "baseline": {"board_name", "enabled_shelves"},
    "candidate": {same}, "rationale"}. Exactly one operator must differ.
    """
    _require(type(raw) is bytes, "profile source must be bytes")
    _require(len(raw) <= MAX_SOURCE_BYTES, f"profile source exceeds {MAX_SOURCE_BYTES} bytes")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique,
                           parse_constant=_nonfinite, parse_float=_float)
    except PlanError:
        raise
    except (ValueError, RecursionError):  # UTF-8, JSON syntax or nesting failure
        raise PlanError("REJECTED_VALIDATION", "profile source is not strict UTF-8 JSON") from None
    _require(type(value) is dict and set(value) == {"schema", "baseline", "candidate", "rationale"},
             "profile source fields mismatch")
    _require(type(value["schema"]) is str and value["schema"] == SOURCE_SCHEMA,
             "unsupported profile source schema")
    source = {"schema": SOURCE_SCHEMA, "baseline": _view(value["baseline"], "baseline"),
              "candidate": _view(value["candidate"], "candidate"),
              "rationale": _text(value["rationale"], "rationale", MAX_RATIONALE_BYTES)}
    changed = [op for op in OPERATORS if source["baseline"][op] != source["candidate"][op]]
    _require(len(changed) == 1, "profile source must change exactly one operator")
    return source


def _current(view):
    """Validate Board.settings output: name/shelves plus optional application metadata."""
    _require(type(view) is dict and {"name", "shelves"} <= set(view)
             <= {"name", "shelves", "artifact_id", "author"}, "current view fields mismatch")
    artifact, author = view.get("artifact_id"), view.get("author")
    _require(artifact is None or (type(artifact) is str and SHA256.fullmatch(artifact)),
             "current view artifact_id must be null or a Board artifact ID")
    _require(author is None or (type(author) is str and re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", author)),
             "current view author must be null or a valid attribution ID")
    _require((artifact is None) == (author is None),
             "current view artifact_id and author must both be null or both be set")
    return {"board_name": _text(view["name"], "current view name", MAX_NAME_BYTES),
            "enabled_shelves": _shelves(view["shelves"], "current view shelves")}


def make_profile_plan(raw, current_view):
    """Return the inert nurarihyon-plan/v1 for raw if its baseline equals current_view."""
    source = parse_profile_source(raw)
    _require(source["baseline"] == _current(current_view),
             "profile baseline does not equal the current view", "STALE_BASELINE")
    operator = next(op for op in OPERATORS if source["baseline"][op] != source["candidate"][op])
    return {"schema": PLAN_SCHEMA, "toolkit": TOOLKIT,
            "source_sha256": hashlib.sha256(raw).hexdigest(),
            "baseline": source["baseline"], "candidate": source["candidate"],
            "operator": operator, "rationale": source["rationale"], "authority": AUTHORITY}


def validate_profile_plan(plan, raw, current_view):
    """Recompute the plan from raw and current_view; raise PlanError on any difference."""
    _require(type(plan) is dict, "plan must be an object")
    _require(type(raw) is bytes, "profile source must be bytes")
    _require(len(raw) <= MAX_SOURCE_BYTES, f"profile source exceeds {MAX_SOURCE_BYTES} bytes")
    # The stored hash only classifies a rejection; acceptance compares the full recomputation.
    stored = plan.get("source_sha256")
    _require(not (type(stored) is str and SHA256.fullmatch(stored))
             or stored == hashlib.sha256(raw).hexdigest(),
             "captured source differs from the planned source", "SOURCE_DRIFT")
    _require(_typed_equal(plan, make_profile_plan(raw, current_view)),
             "plan differs from independent recomputation")


def make_plan(toolkit, raw, current_view):
    if toolkit == TOOLKIT:
        return make_profile_plan(raw, current_view)
    if toolkit == "bezoar.document/v1":
        from .documents import make_document_plan
        return make_document_plan(raw, current_view)
    if toolkit == "bezoar.reconciliation/v1":
        from .reconciliation import make_reconciliation_plan
        return make_reconciliation_plan(raw, current_view)
    if toolkit == "bezoar.reconciliation-capture/v1":
        from .complete_capture import make_reconciliation_plan
        return make_reconciliation_plan(raw, current_view)
    if toolkit == "bezoar.reconciliation-observation/v1":
        from .historical_observation import make_reconciliation_plan
        return make_reconciliation_plan(raw, current_view)
    raise PlanError("REJECTED_VALIDATION", "Unsupported toolkit")


def validate_plan(plan, raw, current_view):
    if plan.get("toolkit") == TOOLKIT:
        return validate_profile_plan(plan, raw, current_view)
    if plan.get("toolkit") == "bezoar.document/v1":
        from .documents import validate_document_plan
        return validate_document_plan(plan, raw, current_view)
    if plan.get("toolkit") == "bezoar.reconciliation/v1":
        from .reconciliation import validate_reconciliation_plan
        return validate_reconciliation_plan(plan, raw, current_view)
    if plan.get("toolkit") == "bezoar.reconciliation-capture/v1":
        from .complete_capture import validate_reconciliation_plan
        return validate_reconciliation_plan(plan, raw, current_view)
    if plan.get("toolkit") == "bezoar.reconciliation-observation/v1":
        from .historical_observation import validate_reconciliation_plan
        return validate_reconciliation_plan(plan, raw, current_view)
    raise PlanError("REJECTED_VALIDATION", "Unsupported toolkit")


def priority_key(task):
    """Sort key: higher priority first, then lower sequence, then id.

    Equivalent SQL: ORDER BY priority DESC, sequence ASC, id ASC. Other task
    fields are ignored.
    """
    _require(type(task) is dict and {"priority", "sequence", "id"} <= set(task),
             "task needs priority, sequence and id", "INVALID_TASK")
    priority, sequence, identity = task["priority"], task["sequence"], task["id"]
    _require(type(priority) is int and 0 <= priority <= 9,
             "priority must be an integer from 0 to 9", "INVALID_TASK")
    _require(type(sequence) is int and 0 <= sequence <= MAX_SEQUENCE,
             "sequence must be a nonnegative 64-bit integer", "INVALID_TASK")
    _require(type(identity) is str and TASK_ID.fullmatch(identity),
             "id must be 1-128 ASCII letters, digits, '.', '_', ':' or '-'", "INVALID_TASK")
    return (-priority, sequence, identity)


def lesson_for(toolkit, outcome_code, source_sha256):
    """Return an inert lesson record. It never schedules retries or widens permissions."""
    _require(type(toolkit) is str and toolkit in (TOOLKIT, "bezoar.document/v1", "bezoar.reconciliation/v1", "bezoar.reconciliation-capture/v1", "bezoar.reconciliation-observation/v1"),
             "unsupported toolkit", "INVALID_LESSON")
    _require(type(outcome_code) is str and outcome_code in LESSONS,
             "outcome has no lesson", "INVALID_LESSON")
    _require(type(source_sha256) is str and SHA256.fullmatch(source_sha256),
             "source_sha256 must be 64 lowercase hex digits", "INVALID_LESSON")
    condition, next_step = LESSONS[outcome_code]
    return {"schema": LESSON_SCHEMA, "toolkit": toolkit, "outcome": outcome_code,
            "source_sha256": source_sha256, "condition": condition, "next_step": next_step,
            "retry": "NONE", "authority": "NONE"}
