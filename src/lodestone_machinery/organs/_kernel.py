"""Shared strict values for kernel metabolism; source text never executes."""
import json
from ..nodes import _json_copy
from ..records import canonical
import hashlib


def require(value, message):
    if not value:
        raise ValueError(message)


def fields(value, names, label):
    require(type(value) is dict and set(value) == set(names.split()), label + ' fields mismatch')


def text(value, label, limit=4096):
    require(type(value) is str and bool(value.strip()) and len(value) <= limit, label + ' invalid text')
    return value


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def strict_load(path, limit=262144):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, 'Duplicate JSON key')
            result[key] = value
        return result
    with open(path, 'rb') as f:
        raw = f.read(limit + 1)
    require(len(raw) <= limit, 'Input exceeds byte bound')
    value = json.loads(raw.decode('utf-8'), object_pairs_hook=unique,
                       parse_constant=lambda x: (_ for _ in ()).throw(ValueError('Nonfinite JSON')))
    return _json_copy(value, max_bytes=limit)


def typed_equal(a, b):
    if type(a) is not type(b):
        return False
    if type(a) is dict:
        return a.keys() == b.keys() and all(typed_equal(a[k], b[k]) for k in a)
    if type(a) is list:
        return len(a) == len(b) and all(typed_equal(x, y) for x, y in zip(a, b))
    return a == b
