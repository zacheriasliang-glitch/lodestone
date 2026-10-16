"""Retain predictions, observations, mismatches, and missing values."""
from __future__ import annotations

import copy
from typing import Any

from ._values import _validate_json


def residue(prediction: dict, observation: dict) -> dict:
    """Retain both records verbatim and mechanically compare exact JSON values."""
    if type(prediction) is not dict or type(observation) is not dict:
        raise ValueError("prediction and observation must be dictionaries")
    _validate_json(prediction)
    _validate_json(observation)
    differences, missing = [], []

    def compare(before: Any, after: Any, path: str) -> None:
        if type(before) is dict and type(after) is dict:
            for key in sorted(set(before) | set(after)):
                child = path + "/" + key.replace("~", "~0").replace("/", "~1")
                if key not in before or key not in after:
                    missing.append({"path": child, "missing_in": "prediction" if key not in before else "observation"})
                else:
                    compare(before[key], after[key], child)
        elif type(before) is list and type(after) is list:
            for index in range(max(len(before), len(after))):
                child = path + "/" + str(index)
                if index >= len(before) or index >= len(after):
                    missing.append({"path": child, "missing_in": "prediction" if index >= len(before) else "observation"})
                else:
                    compare(before[index], after[index], child)
        elif type(before) is not type(after) or before != after:
            differences.append({"path": path or "/", "predicted": copy.deepcopy(before), "observed": copy.deepcopy(after)})

    compare(prediction, observation, "")
    return {"prediction": copy.deepcopy(prediction), "observation": copy.deepcopy(observation),
            "differences": differences, "missing_values": missing,
            "outcome": "MISMATCH" if differences else "INCOMPLETE" if missing else "MATCH",
            "comparison": "mechanical exact JSON comparison, including types; no causal attribution"}
