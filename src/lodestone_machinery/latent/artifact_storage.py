"""Bounded lossless physical storage; logical compact artifacts never change.

All nonmandatory artifact kinds use the same shortest(raw, zlib9) rule with raw
winning ties. Any artifact used by a mandatory source remains physically raw.
The index follows the already validated logical artifact order. Its external
hash and the complete physical-file hash are the caller's responsibility.
No model, file, source discovery, original data or execution authority is used.
"""
from __future__ import annotations

import struct
import zlib

from . import compact_layout as layout

NAME = "zlib-artifacts-v1"
RAW, ZLIB = 0, 1
INDEX_ROW = struct.Struct(">BI")
MAX_ARTIFACT_BYTES = max(layout.MAXIMUMS.values())


class Error(layout.Error):
    pass


def info():
    return {"policy": NAME, "level": 9, "wbits": 15,
            "strategy": "Z_DEFAULT_STRATEGY", "tie": "RAW",
            "mandatory_used": "RAW", "compile_version": zlib.ZLIB_VERSION,
            "runtime_version": zlib.ZLIB_RUNTIME_VERSION}


def validate_info(value):
    """Validate a recorded policy; version strings describe the writer host."""
    expected = info()
    if type(value) is not dict or set(value) != set(expected):
        raise Error("Invalid storage policy fields")
    for key in expected:
        if key in ("compile_version", "runtime_version"):
            if type(value[key]) is not str or not 1 <= len(value[key]) <= 64:
                raise Error("Invalid bounded zlib version declaration")
        elif type(value[key]) is not type(expected[key]) or value[key] != expected[key]:
            raise Error("Storage policy differs")


def select(raw, *, force_raw=False):
    """One compression attempt, never a model-specific exemption or search."""
    if type(raw) is not bytes or len(raw) > MAX_ARTIFACT_BYTES or type(force_raw) is not bool:
        raise Error("Invalid bounded physical artifact")
    if force_raw or not raw:
        return RAW, raw
    encoded = zlib.compress(raw, level=9)
    return (ZLIB, encoded) if len(encoded) < len(raw) else (RAW, raw)


def mandatory_refs(directory):
    layout.validate(directory)
    return {chunk["record"] for source in directory["sources"] if source["mandatory"]
            for chunk in source["chunks"]}


def _entries(entries, directory):
    forced = mandatory_refs(directory)
    artifacts = directory["artifacts"]
    if type(entries) is not list or len(entries) != len(artifacts):
        raise Error("Storage index count differs from logical artifacts")
    normalized, offset = [], 0
    for ordinal, (entry, artifact) in enumerate(zip(entries, artifacts)):
        if type(entry) is not dict or set(entry) != {"mode", "stored_bytes"}:
            raise Error("Invalid storage index fields")
        mode, size, logical = entry["mode"], entry["stored_bytes"], artifact["bytes"]
        if type(mode) is not int or mode not in (RAW, ZLIB) or type(size) is not int:
            raise Error("Invalid storage mode or length")
        if ((mode == RAW and size != logical) or
                (mode == ZLIB and not 0 < size < logical) or
                (ordinal in forced and mode != RAW)):
            raise Error("Storage length or mandatory-raw constraint differs")
        normalized.append({"mode": mode, "stored_bytes": size, "offset": offset})
        offset += size
        if offset > layout.MAX_PAYLOAD_BYTES:
            raise Error("Stored payload exceeds bound")
    return normalized


def encode_index(entries, directory):
    return b"".join(INDEX_ROW.pack(row["mode"], row["stored_bytes"])
                    for row in _entries(entries, directory))


def decode_index(raw, directory):
    layout.validate(directory)
    if type(raw) is not bytes or len(raw) != INDEX_ROW.size * len(directory["artifacts"]):
        raise Error("Storage index byte length differs")
    entries = [{"mode": mode, "stored_bytes": size} for mode, size in INDEX_ROW.iter_unpack(raw)]
    return _entries(entries, directory)


def decode_payload(stored, mode, logical_size):
    """Bound output before inflation; never flush an unbounded remainder."""
    if (type(stored) is not bytes or type(mode) is not int or mode not in (RAW, ZLIB)
            or type(logical_size) is not int or not 0 <= logical_size <= MAX_ARTIFACT_BYTES):
        raise Error("Invalid bounded storage decoding request")
    if mode == RAW:
        if len(stored) != logical_size:
            raise Error("Raw physical length differs")
        return stored
    if not 0 < len(stored) < logical_size:
        raise Error("Compressed physical length differs")
    try:
        decoder = zlib.decompressobj(wbits=15)
        raw = decoder.decompress(stored, logical_size + 1)
        if (len(raw) != logical_size or not decoder.eof or decoder.unused_data
                or decoder.unconsumed_tail):
            raise Error("Compressed stream is incomplete, trailing or exceeds logical length")
        return raw
    except zlib.error as exc:
        raise Error("Invalid compressed artifact stream") from exc
