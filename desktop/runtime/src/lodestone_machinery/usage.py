"""Account for provider-reported usage without estimating absent counters.

Counts describe the provider's reporting boundary, not physical GPU work.
Cached input is a subset of input; reasoning is a subset of output. Cache
write semantics vary by provider, so write counts are retained, never subtracted
to invent an ordinary-input total or a bill.
"""
from __future__ import annotations

from copy import deepcopy

FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_tokens",
          "output_tokens", "reasoning_tokens", "total_tokens")


def normalize_usage(raw: dict | None) -> dict:
    """Accept Responses-style counters or explicit normalized counter names."""
    if raw is None:
        return {"status": "UNKNOWN", "counts": dict.fromkeys(FIELDS), "raw": None}
    if type(raw) is not dict:
        raise ValueError("usage must be an object or null")
    details = raw.get("input_tokens_details", {}) or {}
    outputs = raw.get("output_tokens_details", {}) or {}
    if type(details) is not dict or type(outputs) is not dict:
        raise ValueError("usage detail fields must be objects")
    counts = {key: raw.get(key) for key in FIELDS}
    for key, source, nested in (
        ("cached_input_tokens", details, "cached_tokens"),
        ("cache_write_tokens", details, "cache_write_tokens"),
        ("reasoning_tokens", outputs, "reasoning_tokens"),
    ):
        if key not in raw:
            counts[key] = source.get(nested)
        elif nested in source and source[nested] != counts[key]:
            raise ValueError("Conflicting usage counters: " + key)
    for key, value in counts.items():
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError("Usage counter must be a nonnegative integer or null: " + key)
    for subset, total in (("cached_input_tokens", "input_tokens"),
                          ("reasoning_tokens", "output_tokens")):
        if counts[subset] is not None and counts[total] is not None:
            if counts[subset] > counts[total]:
                raise ValueError(subset + " exceeds " + total)
    if all(counts[k] is not None for k in ("input_tokens", "output_tokens", "total_tokens")):
        if counts["total_tokens"] != counts["input_tokens"] + counts["output_tokens"]:
            raise ValueError("total_tokens disagrees with input + output")
    present = sum(value is not None for value in counts.values())
    return {"status": "REPORTED" if present == len(FIELDS) else "PARTIAL" if present else "UNKNOWN",
            "counts": counts, "raw": deepcopy(raw)}


def usage_totals(calls: list[dict]) -> dict:
    """Missing values propagate; known subtotals remain separately inspectable."""
    result = {"calls": len(calls), "counts": {}, "known_subtotals": {}, "missing_calls": {}}
    for field in FIELDS:
        values = [call["usage"]["counts"][field] for call in calls]
        missing = sum(value is None for value in values)
        known = sum(value for value in values if value is not None)
        result["counts"][field] = None if missing else known
        result["known_subtotals"][field] = known
        result["missing_calls"][field] = missing
    result["cost"] = "UNKNOWN"
    result["boundary"] = "Recorded provider invocations/response exchanges only; excludes unreported upstream preparation and physical compute."
    return result
