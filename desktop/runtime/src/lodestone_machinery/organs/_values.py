"""Finite JSON validation and exact byte identities."""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any


def _validate_json(value: Any) -> None:
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            _validate_json(item)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _validate_json(item)
        return
    raise ValueError("Values must be finite JSON data with string dictionary keys")


def _json(value: Any) -> str:
    _validate_json(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
