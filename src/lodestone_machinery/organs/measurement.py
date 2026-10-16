"""Compare explicit observations with unit-bearing criteria."""
from __future__ import annotations

from ._values import _validate_json


def measure(observed: dict, criteria: list[dict]) -> dict:
    """Measure explicit observations against frozen, unit-bearing criteria."""
    if type(observed) is not dict or type(criteria) is not list:
        raise ValueError("Expected observations dictionary and criteria list")
    _validate_json(observed)
    _validate_json(criteria)
    rows, names = [], set()
    for criterion in criteria:
        if type(criterion) is not dict or not all(k in criterion for k in ("name", "metric", "op", "value", "unit")):
            raise ValueError("Criterion lacks required fields")
        name, metric, op, expected, unit = (criterion[k] for k in ("name", "metric", "op", "value", "unit"))
        if not all(type(v) is str and v for v in (name, metric, op, unit)) or name in names:
            raise ValueError("Criterion names, metrics, units, and operations must be nonempty; names unique")
        if op not in ("eq", "le", "ge") or type(expected) not in (str, bool, int, float):
            raise ValueError("Unsupported criterion operator or scalar")
        if op != "eq" and type(expected) not in (int, float):
            raise ValueError("Ordered criteria require finite numbers, not booleans")
        names.add(name)
        entry = observed.get(metric)
        if entry is not None and type(entry) is not dict:
            raise ValueError("Each observation must provide value and unit")
        actual = entry.get("value") if entry is not None else None
        actual_unit = entry.get("unit") if entry is not None else None
        row = {"name": name, "metric": metric, "op": op, "actual": actual,
               "expected": expected, "unit": unit, "actual_unit": actual_unit}
        if entry is None or actual is None or actual_unit is None:
            row.update(status="UNKNOWN", reason="Missing observed value or unit")
        elif actual_unit != unit:
            row.update(status="UNKNOWN", reason="Observation and criterion units differ")
        else:
            if type(actual) not in (str, bool, int, float):
                raise ValueError("Observed values must be finite scalars")
            if op == "eq":
                compatible = type(actual) is type(expected) or (type(actual) in (int, float) and type(expected) in (int, float))
                passed = compatible and actual == expected
            else:
                if type(actual) not in (int, float):
                    raise ValueError("Ordered observation must be numeric, not boolean")
                passed = actual <= expected if op == "le" else actual >= expected
            row.update(status="PASS" if passed else "FAIL", reason="Exact typed comparison")
        rows.append(row)
    verdict = "FAIL" if any(row["status"] == "FAIL" for row in rows) else (
        "UNKNOWN" if not rows or any(row["status"] == "UNKNOWN" for row in rows) else "PASS")
    return {"verdict": verdict, "rows": rows, "scope": "declared observations and criteria only"}
