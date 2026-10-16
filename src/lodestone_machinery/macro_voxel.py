"""Lossless, bounded graph carrier. Byte identity never grants authority.

The constitutional projection remains explicit and is checked on every decode.
Source bytes are retained so a fresh process can reopen evidence without the
producer's memory. This codec executes no source, provider, route or callback.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
from pathlib import PurePosixPath
import re
import zlib

from .nodes import _json_copy

VERSION = "0.2.0"
MAX_BYTES = 32 * 1024 * 1024
MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_NODES = 4096
MAX_EDGES = 65536
SHA = re.compile(r"[0-9a-f]{64}\Z")
GRAPH_KEYS = {"schema", "body_id", "epoch", "host", "scopes", "sources", "nodes",
              "edges", "lineage", "residue"}
NODE_KEYS = {"id", "address", "role", "selected_organ", "phase", "state",
             "evidence_refs", "unknowns", "contradictions", "debt"}
ADDRESS_KEYS = {"node_id", "body_id", "lifecycle_id", "epoch", "source_contract"}
PHASE_KEYS = {"pulse_id", "phase_name", "ordinal", "direction"}
EDGE_KEYS = {"source", "target", "kind", "bridge", "residue"}
RESIDUE_KEYS = {"authority", "executable", "contradictions", "unknowns", "debt", "declared_losses"}


def canonical(value):
    value = _json_copy(value, MAX_BYTES)
    data = json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(data) > MAX_BYTES:
        raise ValueError("Macro-voxel exceeds the byte bound")
    return data


def digest(data):
    return hashlib.sha256(data).hexdigest()


def _keys(value, expected, label):
    if type(value) is not dict or set(value) != set(expected):
        raise ValueError(label + " has missing or unknown fields")


def _text(value, label):
    if type(value) is not str or not value.strip() or len(value) > 4096:
        raise ValueError(label + " must be a nonempty bounded string")
    return value


def _strings(value, label):
    if type(value) is not list:
        raise ValueError(label + " must be a list")
    for item in value:
        _text(item, label)
    if len(set(value)) != len(value):
        raise ValueError(label + " contains duplicates")
    return value


def safe_path(value):
    _text(value, "path")
    parts = value.split("/")
    if (value.startswith("/") or any(p in ("", ".", "..") for p in parts)
            or "\\" in value or ":" in value or any(ord(c) < 32 for c in value)
            or PurePosixPath(value).is_absolute()):
        raise ValueError("Unsafe relative source path")
    return value


def parse_json(data):
    if type(data) is not bytes or len(data) > MAX_BYTES:
        raise ValueError("Expected bounded JSON bytes")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key: " + key)
            result[key] = value
        return result

    def constant(value):
        raise ValueError("Nonfinite JSON number: " + value)

    try:
        return _json_copy(json.loads(data.decode("utf-8"), object_pairs_hook=pairs,
                                     parse_constant=constant), MAX_BYTES)
    except (UnicodeError, RecursionError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid macro-voxel JSON") from exc


def residue(*, contradictions=(), unknowns=(), debt=()):
    return {"authority": "NONE", "executable": False,
            "contradictions": list(contradictions), "unknowns": list(unknowns),
            "debt": list(debt), "declared_losses": []}


def source(scope, path, data):
    if type(data) is not bytes or len(data) > MAX_SOURCE_BYTES:
        raise ValueError("Source exceeds the read bound")
    return {"scope": _text(scope, "scope"), "path": safe_path(path),
            "sha256": digest(data), "content": base64.b64encode(data).decode("ascii")}


def source_bytes(item):
    _keys(item, {"scope", "path", "sha256", "content"}, "source")
    _text(item["scope"], "scope")
    safe_path(item["path"])
    if type(item["sha256"]) is not str or SHA.fullmatch(item["sha256"]) is None:
        raise ValueError("Invalid source hash")
    if type(item["content"]) is not str or len(item["content"]) > (MAX_SOURCE_BYTES + 2) // 3 * 4:
        raise ValueError("Encoded source exceeds the read bound")
    try:
        data = base64.b64decode(item["content"], validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("Invalid source encoding") from exc
    if (len(data) > MAX_SOURCE_BYTES or digest(data) != item["sha256"]
            or base64.b64encode(data).decode("ascii") != item["content"]):
        raise ValueError("Source bytes do not match their identity")
    return data


def _residue(value):
    _keys(value, RESIDUE_KEYS, "residue")
    if value["authority"] != "NONE" or value["executable"] is not False:
        raise ValueError("Residue cannot acquire authority or execute")
    for key in RESIDUE_KEYS - {"authority", "executable"}:
        if type(value[key]) is not list:
            raise ValueError("Residue evidence must remain a list: " + key)


def validate(graph):
    """Validate structure without silently normalizing, pruning or upgrading it."""
    graph = _json_copy(graph, MAX_BYTES)
    _keys(graph, GRAPH_KEYS, "graph")
    if graph["schema"] != "yggdrasil-graph/v1":
        raise ValueError("Unknown graph schema")
    _text(graph["body_id"], "body id")
    _text(graph["epoch"], "epoch")
    _keys(graph["host"], {"authority", "approval_inherited"}, "Host boundary")
    if graph["host"]["authority"] != "EXTERNAL" or graph["host"]["approval_inherited"] is not False:
        raise ValueError("Host authority must remain external and non-transitive")
    if type(graph["scopes"]) is not dict or not graph["scopes"]:
        raise ValueError("At least one explicit source scope is required")
    if type(graph["sources"]) is not dict or not 1 <= len(graph["sources"]) <= MAX_NODES:
        raise ValueError("Invalid source count")
    seen_sources = set()
    for identifier, item in graph["sources"].items():
        _text(identifier, "source id")
        source_bytes(item)
        if item["scope"] not in graph["scopes"]:
            raise ValueError("Undeclared source scope")
        address = (item["scope"], item["path"])
        if address in seen_sources:
            raise ValueError("Duplicate source address")
        seen_sources.add(address)
    for name, scope in graph["scopes"].items():
        _text(name, "scope")
        _keys(scope, {"kind", "paths"}, "scope")
        _text(scope["kind"], "scope kind")
        paths = _strings(scope["paths"], "scope paths")
        for path in paths:
            safe_path(path)
        if set(paths) != {path for s, path in seen_sources if s == name}:
            raise ValueError("Scope inventory differs from bound sources")
    if type(graph["nodes"]) is not list or not 1 <= len(graph["nodes"]) <= MAX_NODES:
        raise ValueError("Invalid node count")
    nodes, owners = {}, {}
    for node in graph["nodes"]:
        _keys(node, NODE_KEYS, "node")
        identifier = _text(node["id"], "node id")
        if identifier in nodes:
            raise ValueError("Duplicate addressed node")
        nodes[identifier] = node
        address = node["address"]
        _keys(address, ADDRESS_KEYS, "node address")
        if address["node_id"] != identifier or address["body_id"] != graph["body_id"]:
            raise ValueError("Node identity or body mismatch")
        _text(address["epoch"], "node epoch")
        if address["lifecycle_id"] is not None:
            _text(address["lifecycle_id"], "lifecycle id")
        if _text(address["source_contract"], "source contract") not in graph["sources"]:
            raise ValueError("Unbound source contract")
        _text(node["role"], "role contract")
        if node["selected_organ"] is not None:
            organ = _text(node["selected_organ"], "selected organ")
            if address["lifecycle_id"] is None:
                raise ValueError("Operational ownership requires an explicit lifecycle")
            key = (address["body_id"], address["lifecycle_id"], address["epoch"])
            if key in owners and owners[key] != organ:
                raise ValueError("Multiple selected organs claim one lifecycle")
            owners[key] = organ
        _keys(node["phase"], PHASE_KEYS, "phase address")
        phase = node["phase"]
        for key in PHASE_KEYS - {"ordinal"}:
            if phase[key] is not None:
                _text(phase[key], "phase " + key)
        if phase["ordinal"] is not None and (type(phase["ordinal"]) is not int or phase["ordinal"] < 0):
            raise ValueError("Invalid exact phase ordinal")
        if type(node["state"]) is not dict:
            raise ValueError("Local state must be an object")
        refs = _strings(node["evidence_refs"], "evidence refs")
        if not refs or not set(refs) <= graph["sources"].keys():
            raise ValueError("Node evidence is missing or unbound")
        for key in ("unknowns", "contradictions", "debt"):
            if type(node[key]) is not list:
                raise ValueError("Node must preserve " + key)
    if type(graph["edges"]) is not list or len(graph["edges"]) > MAX_EDGES:
        raise ValueError("Invalid edge count")
    for edge in graph["edges"]:
        _keys(edge, EDGE_KEYS, "edge")
        _text(edge["source"], "edge source")
        _text(edge["target"], "edge target")
        if edge["source"] not in nodes or edge["target"] not in nodes:
            raise ValueError("Dangling typed edge")
        _text(edge["kind"], "edge kind")
        bridge = edge["bridge"]
        _keys(bridge, {"authority", "approval_binding", "declared_invariants", "evidence_refs"}, "bridge")
        if bridge["authority"] != "NONE" or bridge["approval_binding"] is not None:
            raise ValueError("Crossing must not inherit approval")
        _strings(bridge["declared_invariants"], "declared invariants")
        if not set(_strings(bridge["evidence_refs"], "bridge evidence")) <= graph["sources"].keys():
            raise ValueError("Unbound crossing evidence")
        _residue(edge["residue"])
    _residue(graph["residue"])
    if type(graph["lineage"]) is not dict:
        raise ValueError("Lineage observation must remain an object")
    return graph


def projection(graph):
    """All graph fields, with exact source hashes replacing inline source bytes."""
    result = _json_copy(graph, MAX_BYTES)
    for item in result["sources"].values():
        item.pop("content")
    return result


def encode(graph):
    graph = validate(graph)
    raw = canonical(graph)
    compressed = zlib.compress(raw, level=9)
    return {"schema": "macro-voxel/v1", "codec": "zlib+base64",
            "sha256": digest(raw), "projection_sha256": digest(canonical(projection(graph))),
            "raw_bytes": len(raw), "compressed_bytes": len(compressed),
            "payload": base64.b64encode(compressed).decode("ascii")}


def decode(voxel, *, expected_sha256=None):
    voxel = _json_copy(voxel, MAX_BYTES)
    _keys(voxel, {"schema", "codec", "sha256", "projection_sha256", "raw_bytes",
                  "compressed_bytes", "payload"}, "macro-voxel")
    if voxel["schema"] != "macro-voxel/v1" or voxel["codec"] != "zlib+base64":
        raise ValueError("Unsupported macro-voxel codec")
    for key in ("raw_bytes", "compressed_bytes"):
        if type(voxel[key]) is not int or not 1 <= voxel[key] <= MAX_BYTES:
            raise ValueError("Invalid macro-voxel size")
    for key in ("sha256", "projection_sha256"):
        if type(voxel[key]) is not str or SHA.fullmatch(voxel[key]) is None:
            raise ValueError("Invalid macro-voxel hash")
    if expected_sha256 is not None and voxel["sha256"] != expected_sha256:
        raise ValueError("Macro-voxel differs from the expected original")
    if type(voxel["payload"]) is not str or len(voxel["payload"]) > MAX_BYTES:
        raise ValueError("Invalid compressed payload")
    try:
        compressed = base64.b64decode(voxel["payload"], validate=True)
        if (len(compressed) != voxel["compressed_bytes"]
                or base64.b64encode(compressed).decode("ascii") != voxel["payload"]):
            raise ValueError("Compressed size or encoding mismatch")
        stream = zlib.decompressobj()
        raw = stream.decompress(compressed, voxel["raw_bytes"] + 1)
        if (len(raw) != voxel["raw_bytes"] or not stream.eof
                or stream.unused_data or stream.unconsumed_tail):
            raise ValueError("Truncated, trailing or oversized macro-voxel")
    except (binascii.Error, zlib.error) as exc:
        raise ValueError("Invalid compressed macro-voxel") from exc
    if digest(raw) != voxel["sha256"]:
        raise ValueError("Macro-voxel content mismatch")
    graph = validate(parse_json(raw))
    if canonical(graph) != raw or digest(canonical(projection(graph))) != voxel["projection_sha256"]:
        raise ValueError("Constitutional projection mismatch")
    return graph


def expand(voxel, anchor, *, direction="both", depth=1):
    """Return a bounded exact-node neighborhood, preserving external edge ids."""
    graph = decode(voxel)
    nodes = {n["id"]: n for n in graph["nodes"]}
    if anchor not in nodes:
        raise ValueError("Unknown exact anchor")
    if direction not in ("both", "ancestors", "descendants") or type(depth) is not int or not 0 <= depth <= 16:
        raise ValueError("Invalid traversal direction or depth")
    selected, frontier = {anchor}, {anchor}
    for _ in range(depth):
        next_ids = set()
        for edge in graph["edges"]:
            if direction in ("both", "descendants") and edge["source"] in frontier:
                next_ids.add(edge["target"])
            if direction in ("both", "ancestors") and edge["target"] in frontier:
                next_ids.add(edge["source"])
        frontier = next_ids - selected
        selected.update(frontier)
        if not frontier:
            break
    chosen = [n for n in graph["nodes"] if n["id"] in selected]
    edges = [e for e in graph["edges"] if e["source"] in selected or e["target"] in selected]
    refs = {n["address"]["source_contract"] for n in chosen}
    refs.update(ref for n in chosen for ref in n["evidence_refs"])
    refs.update(ref for e in edges for ref in e["bridge"]["evidence_refs"])
    return {"macro_sha256": voxel["sha256"], "anchor": anchor, "nodes": chosen,
            "edges": edges, "external_node_ids": sorted({e[k] for e in edges for k in ("source", "target")} - selected),
            "sources": {k: graph["sources"][k] for k in sorted(refs)},
            "residue": graph["residue"], "authority": "NONE"}
