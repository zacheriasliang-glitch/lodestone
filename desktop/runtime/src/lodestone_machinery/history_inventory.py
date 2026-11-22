"""Pure prototype for a frozen native history reference inventory.

Content addresses preserve exact sources;
loader callbacks must perform the existing native typed/hash/link validation.
This module performs no filesystem IO, policy activation, inference or promotion.
"""
from copy import deepcopy
from datetime import datetime
import hashlib

from .records import HANDLE, canonical

SCHEMA = "bezoar-learning-history-inventory/v1"
MAX_INVENTORY_BYTES = 131072  # Existing native context cap; not a larger context.
MAX_REFERENCES = 256  # Existing Yggdrasil typed-provenance cap, including teaching.


def _require(value, message):
    if not value:
        raise ValueError(message)


def _handle(value):
    _require(type(value) is str and HANDLE.fullmatch(value) is not None
             and value.startswith("bezoar:"), "History requires exact native Bezoar handles")
    return value


def _handles(values):
    _require(type(values) is list and len(values) <= MAX_REFERENCES, "History references must be bounded lists")
    for value in values:
        _handle(value)
    _require(values == sorted(set(values)), "History references must be unique and sorted")
    return values


def _reference_count(rows):
    return sum(1 + int(row["outcome_handle"] is not None)
               + len(row["review_handles"]) + len(row["lesson_handles"]) for row in rows)


def _body(rows):
    return {"schema": SCHEMA, "rows": rows, "row_count": len(rows),
            "reference_count": _reference_count(rows), "authority": "NONE"}


def checked_inventory(value, *, reference_budget=MAX_REFERENCES):
    """Validate representation only; native source relationships need validation."""
    _require(type(reference_budget) is int and 0 <= reference_budget <= MAX_REFERENCES,
             "History reference budget must remain within existing provenance bounds")
    _require(type(value) is dict and set(value) == {
        "schema", "rows", "row_count", "reference_count", "inventory_sha256", "authority"},
        "Unknown history inventory fields")
    _require(value["schema"] == SCHEMA and value["authority"] == "NONE",
             "Unsupported native history inventory")
    _require(type(value["rows"]) is list and len(value["rows"]) <= reference_budget,
             "History row count exceeds remaining native provenance budget")
    seen = set()
    for row in value["rows"]:
        _require(type(row) is dict and set(row) == {
            "run_handle", "outcome_handle", "review_handles", "lesson_handles"},
            "Unknown history reference row")
        run = _handle(row["run_handle"])
        _require(run not in seen, "Repeated historical run")
        seen.add(run)
        if row["outcome_handle"] is not None:
            _handle(row["outcome_handle"])
        _handles(row["review_handles"]); _handles(row["lesson_handles"])
        _require(row["outcome_handle"] is not None or
                 not row["review_handles"] and not row["lesson_handles"],
                 "Incomplete history cannot claim reviews or lessons")
    body = _body(value["rows"])
    _require(type(value["row_count"]) is int and type(value["reference_count"]) is int
             and value["row_count"] == body["row_count"]
             and value["reference_count"] == body["reference_count"]
             and body["reference_count"] <= reference_budget, "History inventory counts exceed bounds or differ")
    _require(value["inventory_sha256"] == hashlib.sha256(canonical(body)).hexdigest(),
             "Frozen history inventory digest differs")
    _require(len(canonical(value)) <= MAX_INVENTORY_BYTES, "History inventory exceeds native context bound")
    return deepcopy(value)


def freeze_rows(rows, *, reference_budget=MAX_REFERENCES):
    """Freeze supplied exact native reference rows, with no catalogue discovery."""
    _require(type(reference_budget) is int and 0 <= reference_budget <= MAX_REFERENCES,
             "History reference budget must remain within existing provenance bounds")
    _require(type(rows) is list and len(rows) <= reference_budget, "History rows must be a bounded list")
    # Shape-check before copying/serializing any caller-supplied payload.
    for row in rows:
        _require(type(row) is dict and set(row) == {"run_handle", "outcome_handle", "review_handles", "lesson_handles"}, "Unknown history reference row")
        _handle(row["run_handle"])
        if row["outcome_handle"] is not None: _handle(row["outcome_handle"])
        _handles(row["review_handles"]); _handles(row["lesson_handles"])
    result = _body(deepcopy(rows))
    result["inventory_sha256"] = hashlib.sha256(canonical(result)).hexdigest()
    return checked_inventory(result, reference_budget=reference_budget)


def typed_references(inventory, *, reference_budget=MAX_REFERENCES):
    """The complete typed closure roots; never follow attributed source paths."""
    checked = checked_inventory(inventory, reference_budget=reference_budget)
    result = []
    for row in checked["rows"]:
        result.append(("learning_prior_attempt", row["run_handle"]))
        if row["outcome_handle"] is not None:
            result.append(("learning_prior_outcome", row["outcome_handle"]))
        result.extend(("learning_correction_review", handle) for handle in row["review_handles"])
        result.extend(("learning_candidate_advice", handle) for handle in row["lesson_handles"])
    return result


def terminal_signature(inventory):
    """Frozen completed-history identity for later dispatch freshness checking."""
    checked = checked_inventory(inventory)
    return {row["run_handle"]: {"outcome": row["outcome_handle"],
            "reviews": row["review_handles"], "lessons": row["lesson_handles"]}
            for row in checked["rows"] if row["outcome_handle"] is not None}


def _stamp(value):
    _require(type(value) is str, "Native run chronology must be explicit")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Invalid native run chronology") from exc
    _require(stamp.tzinfo is not None, "Native chronology needs a timezone")
    return stamp


def validate_sources(inventory, *, current_run_handle, current_created_at,
                     selected_nacres, read_run, read_outcome, read_review, read_seed,
                     reference_budget=MAX_REFERENCES):
    """Validate each exact source without collecting complete history bodies.

    ``read_run`` must return the already validated native run payload (including
    created_at, binding and run_id); other callbacks are the native typed readers.
    The caller retains its existing locked operation and cycle-detection scope.
    Readback uses only the frozen review/lesson sets. Dispatch separately compares
    terminal_signature with a fresh inventory from the current native catalogue.
    """
    checked = checked_inventory(inventory, reference_budget=reference_budget)
    current_stamp = _stamp(current_created_at)
    _require(type(selected_nacres) is list and bool(selected_nacres), "Selected native patterns are required")
    patterns = set(_handles(sorted(selected_nacres)))
    previous_order = None
    for row in checked["rows"]:
        handle = row["run_handle"]
        _require(handle != current_run_handle, "A run cannot reference its own history")
        run = read_run(handle)
        _require(_stamp(run["created_at"]) < current_stamp,
                 "Historical run must strictly precede the current preparation")
        order = (run["created_at"], handle)  # Preserve the existing native sort convention.
        _require(previous_order is None or previous_order < order, "Historical run order differs")
        previous_order = order
        _require(bool(patterns.intersection(run["binding"]["nacre_handles"])),
                 "Historical run does not share the selected native pattern")
        outcome_handle = row["outcome_handle"]
        if outcome_handle is None:
            continue
        outcome = read_outcome(outcome_handle)
        _require(outcome["run_handle"] == handle and outcome["run_id"] == run["run_id"],
                 "Historical outcome belongs to another run")
        sources = {("outcome", outcome_handle)}
        for review_handle in row["review_handles"]:
            review = read_review(review_handle)
            _require(review["outcome_handle"] == outcome_handle,
                     "Historical review belongs to another outcome")
            sources.add(("review", review_handle))
        for seed_handle in row["lesson_handles"]:
            seed = read_seed(seed_handle)
            derivation = seed.get("derivation", {})
            _require((derivation.get("source_kind"), derivation.get("source_handle")) in sources,
                     "Historical lesson does not derive from this exact outcome or frozen review set")
    return checked
