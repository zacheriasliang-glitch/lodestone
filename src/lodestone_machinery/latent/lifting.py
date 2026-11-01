"""Exact bounded integer content transforms and discrete parameter fitting.

Fitting minimizes complete packet bytes plus serialized model bytes, on supplied
training blocks only. This is content-transform parameter fitting, not transformer
training. The caller owns data splits and execution admission. No corpus discovery,
filesystem access, held-out evaluation, or automatic fitting occurs here.

Serialized shear coefficients approximate geometry; they are not angles and do
not promise Euclidean rotation or compression benefit. Identical checked integer
operations in reverse establish exactness, including negative rounding.
"""
from __future__ import annotations

import hashlib
import json
import math
import struct
import time
import zlib

SCHEMA = "crystal-integer-transform/v1"
MAX_INPUT = 1024
MAX_SERIALIZED = 5 * MAX_INPUT
HEADER = struct.Struct(">4sBH32s32s")
MAX_PACKET = HEADER.size + MAX_INPUT
MIN_INT, MAX_INT = -(2**31), 2**31 - 1
TRANSFORMS = ("identity", "raw_zlib", "predict_update", "shear")


class TransformError(ValueError):
    pass


def _int(value, low=MIN_INT, high=MAX_INT):
    if type(value) is not int or not low <= value <= high:
        raise TransformError("integer outside declared domain")
    return value


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def validate(model):
    if (type(model) is not dict or set(model) != {"schema", "transform", "coefficients", "shift"}
            or model["schema"] != SCHEMA or model["transform"] not in TRANSFORMS):
        raise TransformError("unsupported transform model")
    if type(model["coefficients"]) is not list or len(model["coefficients"]) != 2:
        raise TransformError("two serialized coefficients required")
    coefficients = [_int(x, -16, 16) for x in model["coefficients"]]
    shift = _int(model["shift"], 0, 4)
    if model["transform"] in ("identity", "raw_zlib") and (coefficients != [0, 0] or shift != 0):
        raise TransformError("control model has noncanonical parameters")
    return {"schema": SCHEMA, "transform": model["transform"],
            "coefficients": coefficients, "shift": shift}


def model_bytes(model):
    return json.dumps(validate(model), sort_keys=True, separators=(",", ":")).encode("ascii")


def model_sha256(model):
    return _sha(model_bytes(model))


def _round(value, shift):
    _int(value)
    _int(shift, 0, 4)
    return value if shift == 0 else _int(value + (1 << (shift - 1))) // (1 << shift)


def _scaled(value, coefficient, shift):
    return _round(_int(_int(value) * _int(coefficient, -16, 16)), shift)


def _forward(a, b, model):
    a, b = _int(a, 0, 255), _int(b, 0, 255)
    p, u = model["coefficients"]
    k = model["shift"]
    if model["transform"] in ("identity", "raw_zlib"):
        return a, b
    if model["transform"] == "predict_update":
        d = _int(b - _scaled(a, p, k))
        return _int(a + _scaled(d, u, k)), d
    first = _int(a - _scaled(b, p, k))
    second = _int(b + _scaled(first, u, k))
    return _int(first - _scaled(second, p, k)), second


def _inverse(first, second, model):
    first, second = _int(first), _int(second)
    p, u = model["coefficients"]
    k = model["shift"]
    if model["transform"] in ("identity", "raw_zlib"):
        a, b = first, second
    elif model["transform"] == "predict_update":
        a = _int(first - _scaled(second, u, k))
        b = _int(second + _scaled(a, p, k))
    else:
        lifted = _int(first + _scaled(second, p, k))
        b = _int(second - _scaled(lifted, u, k))
        a = _int(lifted + _scaled(b, p, k))
    return _int(a, 0, 255), _int(b, 0, 255)


def _varint(value):
    value = _int(value)
    number = 2 * value if value >= 0 else -2 * value - 1
    result = bytearray()
    while number >= 128:
        result.append((number & 127) | 128)
        number >>= 7
    result.append(number)
    return bytes(result)


def _read_varint(raw, offset):
    initial, value = offset, 0
    for shift in range(0, 35, 7):
        if offset >= len(raw):
            raise TransformError("truncated coefficient")
        byte = raw[offset]
        offset += 1
        value |= (byte & 127) << shift
        if not byte & 128:
            if value > 0xffffffff:
                raise TransformError("coefficient exceeds int32")
            decoded = value // 2 if value % 2 == 0 else -(value // 2) - 1
            if _varint(decoded) != raw[initial:offset]:
                raise TransformError("noncanonical coefficient")
            return decoded, offset
    raise TransformError("coefficient exceeds five bytes")


def _serialize(raw, model):
    if model["transform"] == "raw_zlib":
        return raw
    data = bytearray()
    for pos in range(0, len(raw) - 1, 2):
        for value in _forward(raw[pos], raw[pos + 1], model):
            data.extend(_varint(value))
    if len(raw) % 2:
        data.append(raw[-1])
    assert len(data) <= MAX_SERIALIZED
    return bytes(data)


def _deserialize(raw, size, model):
    if model["transform"] == "raw_zlib":
        if len(raw) != size:
            raise TransformError("raw-zlib size mismatch")
        return raw
    output, offset = bytearray(), 0
    for _ in range(size // 2):
        first, offset = _read_varint(raw, offset)
        second, offset = _read_varint(raw, offset)
        output.extend(_inverse(first, second, model))
    if len(raw) - offset != size % 2:
        raise TransformError("coefficient count or odd-byte tail differs")
    output.extend(raw[offset:])
    return bytes(output)


def encode(raw, model):
    model = validate(model)
    if type(raw) is not bytes or len(raw) > MAX_INPUT:
        raise TransformError("supply at most 1024 source bytes")
    compressed = zlib.compress(_serialize(raw, model), level=9)
    mode, payload = (1, compressed) if len(compressed) < len(raw) else (0, raw)
    return HEADER.pack(b"LIF1", mode, len(raw), bytes.fromhex(model_sha256(model)),
                       bytes.fromhex(_sha(raw))) + payload


def decode(packet, model, *, expected_sha256, expected_size):
    model = validate(model)
    _int(expected_size, 0, MAX_INPUT)
    if (type(expected_sha256) is not str or len(expected_sha256) != 64 or
            any(c not in "0123456789abcdef" for c in expected_sha256)):
        raise TransformError("invalid external source pin")
    if type(packet) is not bytes or not HEADER.size <= len(packet) <= MAX_PACKET:
        raise TransformError("invalid packet bounds")
    magic, mode, size, model_hash, source_hash = HEADER.unpack_from(packet)
    if (magic != b"LIF1" or mode not in (0, 1) or size != expected_size
            or source_hash.hex() != expected_sha256 or model_hash.hex() != model_sha256(model)):
        raise TransformError("packet identity mismatch")
    payload = packet[HEADER.size:]
    if mode == 0:
        if len(payload) != size:
            raise TransformError("literal size mismatch")
        raw = payload
    else:
        if len(payload) >= size:
            raise TransformError("noncanonical fallback: literal wins or ties")
        decoder = zlib.decompressobj()
        try:
            limit = size if model["transform"] == "raw_zlib" else 5 * (size - size % 2) + size % 2
            coefficients = decoder.decompress(payload, limit + 1)
        except zlib.error as exc:
            raise TransformError("invalid compressed coefficients") from exc
        if (len(coefficients) > limit or not decoder.eof
                or decoder.unused_data or decoder.unconsumed_tail):
            raise TransformError("compressed stream exceeds bounds or has trailing data")
        raw = _deserialize(coefficients, size, model)
    if _sha(raw) != expected_sha256:
        raise TransformError("reconstructed source hash mismatch")
    return raw


def fit(training_blocks, candidates, *, max_seconds=60):
    """Bounded content-parameter selection; never discover or split data here.

    Requires raw_zlib among candidates. Every candidate and fallback is counted;
    the serialized model remains charged even when all its blocks use literal.
    No held-out test, generalization gate or scientific authorization is implied.
    """
    if (type(training_blocks) not in (tuple, list) or not 1 <= len(training_blocks) <= 64
            or any(type(b) is not bytes or not 1 <= len(b) <= MAX_INPUT for b in training_blocks)):
        raise TransformError("supply 1..64 nonempty bounded training blocks")
    if type(candidates) not in (tuple, list) or not 1 <= len(candidates) <= 32:
        raise TransformError("supply 1..32 candidate parameter models")
    if type(max_seconds) not in (float, int) or not 0 < max_seconds <= 60 or not math.isfinite(max_seconds):
        raise TransformError("max_seconds must be positive and at most 60")
    models = [validate(m) for m in candidates]
    hashes = [model_sha256(m) for m in models]
    if len(set(hashes)) != len(hashes) or not any(m["transform"] == "raw_zlib" for m in models):
        raise TransformError("unique candidates including raw_zlib control required")
    start, cpu = time.monotonic(), time.process_time()
    reports = []
    for index, model in enumerate(models):
        sizes, literal = [], 0
        for block in training_blocks:
            if time.monotonic() - start >= max_seconds:
                raise TransformError("fit deadline reached; no partial winner")
            packet = encode(block, model)
            if decode(packet, model, expected_sha256=_sha(block), expected_size=len(block)) != block:
                raise TransformError("candidate failed exact recovery")
            sizes.append(len(packet))
            literal += packet[4] == 0
        reports.append({"candidate_index": index, "model_sha256": hashes[index],
                        "model_bytes": len(model_bytes(model)), "packet_bytes": sum(sizes),
                        "total_bytes": len(model_bytes(model)) + sum(sizes),
                        "literal_blocks": literal, "block_count": len(sizes)})
    if time.monotonic() - start >= max_seconds:
        raise TransformError("fit deadline reached; no partial winner")
    winner = min(range(len(reports)), key=lambda i: (reports[i]["total_bytes"], i))
    return {"schema": "crystal-integer-fit/v1", "status": "PARAMETERS_SELECTED_TRAINING_INPUTS_ONLY",
            "model": models[winner], "model_sha256": hashes[winner], "selected_index": winner,
            "training_blocks": [{"sha256": _sha(b), "bytes": len(b)} for b in training_blocks],
            "candidates": reports, "objective": "model bytes once + complete packet bytes",
            "tie_order": "supplied candidate order", "zlib_runtime_version": zlib.ZLIB_RUNTIME_VERSION,
            "wall_seconds": time.monotonic() - start, "cpu_seconds": time.process_time() - cpu,
            "held_out_benefit": "NOT_EVALUATED", "authority": "NONE"}
