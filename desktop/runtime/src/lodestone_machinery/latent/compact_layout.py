"""Canonical bounded directory for compact checkpoints; no payload I/O.

Version 1 uses big-endian unsigned integers and this wire order:
  b'LTCMDIR1'; previous-present u8; optional predecessor hash (32 bytes);
  artifact, recipe, source counts (three u16);
  artifacts: kind u8, SHA-256 (32 bytes), byte length u32;
  recipes: encoding u8, layer count u8, model refs u16, then table refs u16;
  sources: UTF-8 ID length u16 and ID, mandatory u8, byte length u32,
    SHA-256, chunk count u16; then each chunk's SHA-256, recipe u16,
    record u16, and (only for two latent layers) inner length u32 and hash.

Kind and encoding codes are their tuple positions below. Literal recipes have
zero layers, lifting has one model and no tables, latent has one or two models
and equally many tables. References are zero-based. Offsets and chunk lengths
are derived from fixed 1024-byte geometry, with one chunk for an empty source.

Validation checks declarations and their relationships, not actual payload
digests or decoded source bytes. The caller must verify those against this
directory and authenticate the directory externally. Predecessor hashes are
lineage declarations, not dependencies contained in this directory.
"""
from __future__ import annotations

import hashlib
import re
import struct


CHUNK_BYTES = 1024
MAX_SOURCES = 64
MAX_SOURCE_BYTES = 65536
MAX_TOTAL_SOURCE_BYTES = 262144
MAX_CHUNKS = 320
MAX_ARTIFACTS = 2049
MAX_DIRECTORY_BYTES = 1048576
MAX_PAYLOAD_BYTES = 8388608
MAX_RECIPES = 64
MAGIC = b"LTCMDIR1"
KINDS = ("literal", "lifting-model", "lifting-packet", "model", "table", "packet")
MAXIMUMS = dict(zip(KINDS, (1024, 16384, 8192, 1048576, 32768, 8192)))
_MINIMUMS = dict(zip(KINDS, (0, 1, 71, 1, 32768, 74)))
_ENCODINGS = ("literal", "lifting", "latent")
_EMPTY_SHA = hashlib.sha256(b"").hexdigest()
_HASH = re.compile(r"[0-9a-f]{64}\Z")


class Error(ValueError):
    """Malformed, noncanonical or out-of-bounds directory."""


def _keys(value, keys, label):
    if type(value) is not dict or len(value) != len(keys) or set(value) != set(keys):
        raise Error(label + ": invalid fields")


def _integer(value, low, high, label):
    if type(value) is not int or not low <= value <= high:
        raise Error(label + ": invalid bounded integer")
    return value


def _items(value, low, high, label):
    if type(value) is not list or not low <= len(value) <= high:
        raise Error(label + ": invalid list or count")
    return value


def _hash(value, label):
    if type(value) is not str or len(value) != 64 or _HASH.fullmatch(value) is None:
        raise Error(label + ": expected lowercase SHA-256")
    return value


def _id_bytes(value):
    if type(value) is not str:
        raise Error("source ID must be text")
    if not 1 <= len(value) <= 512:
        raise Error("source ID must contain 1 through 512 UTF-8 bytes")
    try:
        raw = value.encode("utf-8")
    except UnicodeError as exc:
        raise Error("source ID is not UTF-8") from exc
    if not 1 <= len(raw) <= 512:
        raise Error("source ID must contain 1 through 512 UTF-8 bytes")
    return raw


def _ordered(key, previous, label):
    if previous is not None and key <= previous:
        raise Error(label + ": duplicate or noncanonical order")
    return key


def validate(directory):
    """Validate an exact plain-Python directory, without modifying it."""
    _keys(directory, ("previous", "artifacts", "recipes", "sources"), "directory")
    previous = directory["previous"]
    if previous is not None:
        _hash(previous, "previous")
    artifacts = _items(directory["artifacts"], 1, MAX_ARTIFACTS, "artifacts")
    recipes = _items(directory["recipes"], 1, MAX_RECIPES, "recipes")
    sources = _items(directory["sources"], 1, MAX_SOURCES, "sources")
    wire_bytes = len(MAGIC) + 1 + (32 if previous is not None else 0) + 6
    payload_bytes = 0
    prior_key = None
    for artifact in artifacts:
        _keys(artifact, ("kind", "sha256", "bytes"), "artifact")
        kind = artifact["kind"]
        if type(kind) is not str or kind not in KINDS:
            raise Error("unknown artifact kind")
        digest = _hash(artifact["sha256"], "artifact hash")
        size = _integer(artifact["bytes"], _MINIMUMS[kind], MAXIMUMS[kind], "artifact size")
        if size == 0 and digest != _EMPTY_SHA:
            raise Error("empty artifact hash mismatch")
        prior_key = _ordered((KINDS.index(kind), digest), prior_key, "artifacts")
        payload_bytes += size
        if payload_bytes > MAX_PAYLOAD_BYTES:
            raise Error("total artifact payload exceeds bound")
        wire_bytes += 37

    used_artifacts = set()
    prior_key = None
    for recipe in recipes:
        _keys(recipe, ("encoding", "models", "tables"), "recipe")
        encoding = recipe["encoding"]
        if type(encoding) is not str or encoding not in _ENCODINGS:
            raise Error("unknown recipe encoding")
        models = _items(recipe["models"], 0, 2, "recipe models")
        tables = _items(recipe["tables"], 0, 2, "recipe tables")
        if ((encoding == "literal" and (models or tables)) or
                (encoding == "lifting" and (len(models) != 1 or tables)) or
                (encoding == "latent" and (len(models) not in (1, 2) or len(tables) != len(models)))):
            raise Error("recipe layer counts do not match encoding")
        for refs, kind in ((models, "lifting-model" if encoding == "lifting" else "model"),
                           (tables, "table")):
            for ref in refs:
                _integer(ref, 0, len(artifacts) - 1, "recipe reference")
                if artifacts[ref]["kind"] != kind:
                    raise Error("recipe dependency has wrong kind")
                used_artifacts.add(ref)
        key = (_ENCODINGS.index(encoding), tuple(models), tuple(tables))
        prior_key = _ordered(key, prior_key, "recipes")
        wire_bytes += 2 + 2 * (len(models) + len(tables))

    total_source_bytes = 0
    total_chunks = 0
    used_recipes = set()
    prior_key = None
    for source in sources:
        _keys(source, ("id", "mandatory", "bytes", "sha256", "chunks"), "source")
        identifier = _id_bytes(source["id"])
        prior_key = _ordered(identifier, prior_key, "sources")
        if type(source["mandatory"]) is not bool:
            raise Error("mandatory flag must be bool")
        size = _integer(source["bytes"], 0, MAX_SOURCE_BYTES, "source size")
        digest = _hash(source["sha256"], "source hash")
        if size == 0 and digest != _EMPTY_SHA:
            raise Error("empty source hash mismatch")
        total_source_bytes += size
        if total_source_bytes > MAX_TOTAL_SOURCE_BYTES:
            raise Error("total source bytes exceed bound")
        count = max(1, (size + CHUNK_BYTES - 1) // CHUNK_BYTES)
        chunks = _items(source["chunks"], count, count, "source chunks")
        total_chunks += count
        if total_chunks > MAX_CHUNKS:
            raise Error("total chunks exceed bound")
        wire_bytes += 41 + len(identifier)
        for index, chunk in enumerate(chunks):
            _keys(chunk, ("sha256", "recipe", "record", "inner"), "chunk")
            chunk_hash = _hash(chunk["sha256"], "chunk hash")
            if count == 1 and chunk_hash != digest:
                raise Error("single chunk/source hash mismatch")
            recipe_ref = _integer(chunk["recipe"], 0, len(recipes) - 1, "chunk recipe")
            record = _integer(chunk["record"], 0, len(artifacts) - 1, "chunk record")
            recipe = recipes[recipe_ref]
            encoding = recipe["encoding"]
            if source["mandatory"] and encoding != "literal":
                raise Error("mandatory source must use literal recipes")
            expected_kind = {"literal": "literal", "lifting": "lifting-packet", "latent": "packet"}[encoding]
            artifact = artifacts[record]
            if artifact["kind"] != expected_kind:
                raise Error("chunk record has wrong kind")
            chunk_bytes = min(CHUNK_BYTES, size - index * CHUNK_BYTES)
            if encoding == "literal" and (artifact["bytes"] != chunk_bytes or artifact["sha256"] != chunk_hash):
                raise Error("literal record does not match chunk")
            inner = chunk["inner"]
            if encoding == "latent" and len(recipe["models"]) == 2:
                _keys(inner, ("bytes", "sha256"), "inner packet")
                _integer(inner["bytes"], 74, 8192, "inner packet size")
                _hash(inner["sha256"], "inner packet hash")
                wire_bytes += 36
            elif inner is not None:
                raise Error("inner packet only belongs to two-layer latent chunks")
            used_recipes.add(recipe_ref)
            used_artifacts.add(record)
            wire_bytes += 36
    if used_recipes != set(range(len(recipes))):
        raise Error("unused recipe")
    if used_artifacts != set(range(len(artifacts))):
        raise Error("artifact closure contains unused records")
    if wire_bytes > MAX_DIRECTORY_BYTES:
        raise Error("directory exceeds byte bound")


def encode(directory):
    """Return the unique version-1 encoding; reject rather than sort input."""
    validate(directory)
    output = bytearray(MAGIC)
    output.append(directory["previous"] is not None)
    if directory["previous"] is not None:
        output.extend(bytes.fromhex(directory["previous"]))
    output.extend(struct.pack(">HHH", *(len(directory[name]) for name in ("artifacts", "recipes", "sources"))))
    for artifact in directory["artifacts"]:
        output.append(KINDS.index(artifact["kind"]))
        output.extend(bytes.fromhex(artifact["sha256"]))
        output.extend(struct.pack(">I", artifact["bytes"]))
    for recipe in directory["recipes"]:
        output.extend((_ENCODINGS.index(recipe["encoding"]), len(recipe["models"])))
        for ref in recipe["models"] + recipe["tables"]:
            output.extend(struct.pack(">H", ref))
    for source in directory["sources"]:
        identifier = source["id"].encode("utf-8")
        output.extend(struct.pack(">H", len(identifier)))
        output.extend(identifier)
        output.extend(struct.pack(">BI", source["mandatory"], source["bytes"]))
        output.extend(bytes.fromhex(source["sha256"]))
        output.extend(struct.pack(">H", len(source["chunks"])))
        for chunk in source["chunks"]:
            output.extend(bytes.fromhex(chunk["sha256"]))
            output.extend(struct.pack(">HH", chunk["recipe"], chunk["record"]))
            if chunk["inner"] is not None:
                output.extend(struct.pack(">I", chunk["inner"]["bytes"]))
                output.extend(bytes.fromhex(chunk["inner"]["sha256"]))
    return bytes(output)


class _Reader:
    def __init__(self, data):
        self.data = data
        self.offset = 0

    def take(self, size):
        end = self.offset + size
        if end > len(self.data):
            raise Error("truncated directory")
        value = self.data[self.offset:end]
        self.offset = end
        return value

    def integer(self, size):
        return int.from_bytes(self.take(size), "big")

    def digest(self):
        return self.take(32).hex()


def decode(data):
    """Parse bounded bytes into a fresh directory; reject noncanonical bytes."""
    if type(data) is not bytes or len(data) > MAX_DIRECTORY_BYTES:
        raise Error("directory must be bounded bytes")
    reader = _Reader(data)
    if reader.take(len(MAGIC)) != MAGIC:
        raise Error("unknown directory magic or version")
    present = reader.integer(1)
    _integer(present, 0, 1, "previous presence flag")
    previous = reader.digest() if present else None
    artifact_count = _integer(reader.integer(2), 1, MAX_ARTIFACTS, "artifact count")
    recipe_count = _integer(reader.integer(2), 1, MAX_RECIPES, "recipe count")
    source_count = _integer(reader.integer(2), 1, MAX_SOURCES, "source count")
    artifacts = []
    for _ in range(artifact_count):
        kind = _integer(reader.integer(1), 0, len(KINDS) - 1, "artifact kind code")
        artifacts.append({"kind": KINDS[kind], "sha256": reader.digest(), "bytes": reader.integer(4)})
    recipes = []
    for _ in range(recipe_count):
        encoding = _integer(reader.integer(1), 0, len(_ENCODINGS) - 1, "recipe encoding code")
        layers = reader.integer(1)
        _integer(layers, (0, 1, 1)[encoding], (0, 1, 2)[encoding], "recipe layer count")
        models = [reader.integer(2) for _ in range(layers)]
        tables = [reader.integer(2) for _ in range(layers if encoding == 2 else 0)]
        recipes.append({"encoding": _ENCODINGS[encoding], "models": models, "tables": tables})
    sources = []
    total_chunks = 0
    for _ in range(source_count):
        identifier_length = _integer(reader.integer(2), 1, 512, "source ID length")
        try:
            identifier = reader.take(identifier_length).decode("utf-8")
        except UnicodeError as exc:
            raise Error("source ID is not UTF-8") from exc
        mandatory = _integer(reader.integer(1), 0, 1, "mandatory flag")
        size = _integer(reader.integer(4), 0, MAX_SOURCE_BYTES, "source size")
        digest = reader.digest()
        expected_count = max(1, (size + CHUNK_BYTES - 1) // CHUNK_BYTES)
        count = _integer(reader.integer(2), expected_count, expected_count, "source chunk count")
        total_chunks += count
        if total_chunks > MAX_CHUNKS:
            raise Error("total chunks exceed bound")
        chunks = []
        for _ in range(count):
            chunk_hash = reader.digest()
            recipe_ref = _integer(reader.integer(2), 0, recipe_count - 1, "chunk recipe")
            record = reader.integer(2)
            recipe = recipes[recipe_ref]
            inner = None
            if recipe["encoding"] == "latent" and len(recipe["models"]) == 2:
                inner = {"bytes": reader.integer(4), "sha256": reader.digest()}
            chunks.append({"sha256": chunk_hash, "recipe": recipe_ref, "record": record, "inner": inner})
        sources.append({"id": identifier, "mandatory": bool(mandatory), "bytes": size,
                        "sha256": digest, "chunks": chunks})
    if reader.offset != len(data):
        raise Error("trailing directory bytes")
    directory = {"previous": previous, "artifacts": artifacts, "recipes": recipes, "sources": sources}
    validate(directory)
    return directory
