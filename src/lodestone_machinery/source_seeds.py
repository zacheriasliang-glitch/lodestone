"""Select exact addresses from a caller-supplied local-memory resolve report.

Selection is explicit when a report contains alternatives. This module validates
the bounded report and its internal byte claims; Origami reopens source bytes
before accepting a seed. A supplied report is attribution, not authentication
that a resolver ran, and neither byte identity nor selection establishes meaning.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
import re

from .nodes import _json_copy

MAX_REPORT_BYTES = 1024 * 1024
MAX_SEEDS = 64
SHA = re.compile(r"[0-9a-f]{64}\Z")
RECORD_ID = re.compile(r"[0-9a-f]{16}\Z")
SELECTOR = re.compile(r"([0-9a-f]{16}):(0|[1-9][0-9]{0,2})\Z")
SEED_FIELDS = {"source", "start_line", "end_line", "source_sha256", "slice_sha256"}
PROVENANCE_FIELDS = {"report_canonical_sha256", "manifest_sha256", "selector", "record_id",
                     "record_content_hash", "capture", "attribution", "source_url"}


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _text(value, limit, name):
    _require(type(value) is str and bool(value.strip()) and "\0" not in value,
             name + " must be nonempty text")
    _require(len(value.encode("utf-8")) <= limit, name + " exceeds byte bound")


def _sha(value, name):
    _require(type(value) is str and SHA.fullmatch(value) is not None, "Invalid " + name)


def validate_seed(seed):
    """Validate one LF-based handle, or a D1 universal-line handle with provenance."""
    _require(type(seed) is dict and SEED_FIELDS <= set(seed)
             and not set(seed) - SEED_FIELDS - {"line_convention", "address_provenance"},
             "Exact seed has missing or unknown fields")
    source = seed["source"]
    _text(source, 1024, "source")
    _require("\\" not in source and not PurePosixPath(source).is_absolute()
             and all(part not in ("", ".", "..") for part in source.split("/")),
             "Seed source must be a contained relative POSIX path")
    start, end = seed["start_line"], seed["end_line"]
    _require(type(start) is int and type(end) is int and 1 <= start <= end <= 2097152,
             "Invalid seed passage line interval")
    for field in ("source_sha256", "slice_sha256"):
        _sha(seed[field], field)
    convention = seed.get("line_convention", "line-bytes-v1")
    _require(convention in ("line-bytes-v1", "python-splitlines-keepends-v1"),
             "Unsupported seed line convention")
    provenance = seed.get("address_provenance")
    if provenance is not None:
        _require(type(provenance) is dict and set(provenance) == PROVENANCE_FIELDS,
                 "Invalid address provenance fields")
        for field in ("report_canonical_sha256", "manifest_sha256", "record_content_hash"):
            _sha(provenance[field], field)
        rid = provenance["record_id"]
        _require(type(rid) is str and RECORD_ID.fullmatch(rid) is not None,
                 "Invalid address record id")
        selector = provenance["selector"]
        match = SELECTOR.fullmatch(selector) if type(selector) is str else None
        _require(match is not None and match.group(1) == rid, "Invalid address selector")
        _text(provenance["capture"], 256, "capture")
        _text(provenance["attribution"], 1024, "attribution")
        if provenance["source_url"] is not None:
            _text(provenance["source_url"], 2048, "source_url")
        _require(convention == "python-splitlines-keepends-v1",
                 "Address provenance requires the resolver's line convention")
    elif convention != "line-bytes-v1":
        raise ValueError("Resolver line convention requires address provenance")


def source_address_seeds(report, *, selections=None):
    """Return ``root, paths, seed_passages`` for ``Origami.start``.

    A selector is ``<record_id>:<zero-based-binding-index>``. Without selectors,
    accept only one unambiguous, nontruncated VERIFIED_ADDRESS. Explicit selection
    can choose among alternatives, but cannot admit a stale/unavailable binding.
    Truncated reports always refuse: retrieve a complete narrower report first.
    """
    report = _json_copy(report, MAX_REPORT_BYTES)
    _require(type(report) is dict and set(report) == {
        "schema", "query", "scope", "manifest_sha256", "status", "truncated", "results", "meaning"}
        and report["schema"] == "lodestone-source-addresses/v1", "Invalid source-address report schema")
    _text(report["scope"], 4096, "scope")
    _require(PurePosixPath(report["scope"]).is_absolute(), "Address scope must be absolute")
    _require(type(report["query"]) is str and len(report["query"].encode("utf-8")) <= 4096,
             "Invalid address query")
    _text(report["meaning"], 4096, "meaning")
    _sha(report["manifest_sha256"], "manifest_sha256")
    _require(report["truncated"] is False, "Truncated address report requires a narrower query")
    _require(report["status"] in ("VERIFIED_ADDRESS", "AMBIGUOUS"),
             "Address report has no verified selectable result")
    rows = report["results"]
    _require(type(rows) is list and 1 <= len(rows) <= 10, "Invalid address result count")
    indexed = {}
    for row in rows:
        _require(type(row) is dict and set(row) == {
            "record_id", "title", "score", "superseded_by", "status", "bindings"},
            "Invalid address result fields")
        rid = row["record_id"]
        _require(type(rid) is str and RECORD_ID.fullmatch(rid) is not None and rid not in indexed,
                 "Invalid or duplicate address record id")
        bindings = row["bindings"]
        _require(type(bindings) is list and len(bindings) <= MAX_SEEDS,
                 "Invalid address binding count")
        indexed[rid] = row
    _require(sum(len(row["bindings"]) for row in rows) <= MAX_SEEDS,
             "Address report exceeds binding bound")
    if selections is None:
        _require(report["status"] == "VERIFIED_ADDRESS" and len(rows) == 1
                 and rows[0]["status"] == "VERIFIED_ADDRESS" and len(rows[0]["bindings"]) == 1,
                 "Ambiguous addresses require explicit source-address selections")
        selections = [rows[0]["record_id"] + ":0"]
    _require(type(selections) is list and 1 <= len(selections) <= MAX_SEEDS,
             "Select 1 to 64 exact source addresses")
    _require(all(type(item) is str for item in selections)
             and len(set(selections)) == len(selections), "Invalid or duplicate address selection")
    report_sha = hashlib.sha256(json.dumps(report, ensure_ascii=False, sort_keys=True,
                                          separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
    seeds, seen = [], set()
    for selector in selections:
        match = SELECTOR.fullmatch(selector)
        _require(match is not None, "Invalid address selector; expected record_id:binding_index")
        rid, offset = match.groups()
        _require(rid in indexed and int(offset) < len(indexed[rid]["bindings"]),
                 "Address selection does not exist")
        row = indexed[rid]
        _require(row["status"] in ("VERIFIED_ADDRESS", "AMBIGUOUS")
                 and row["superseded_by"] is None, "Selected address is unavailable or superseded")
        binding = row["bindings"][int(offset)]
        _require(type(binding) is dict and binding.get("status") == "VERIFIED_ADDRESS",
                 "Selected binding is not VERIFIED_ADDRESS")
        _require(binding.get("record_id") == rid, "Binding belongs to a different record")
        required = SEED_FIELDS | {"record_id", "record_content_hash", "capture", "attribution",
                                  "source_url", "status", "expansion"}
        _require(set(binding) == required, "Invalid verified binding fields")
        seed = {key: binding[key] for key in SEED_FIELDS}
        seed["line_convention"] = "python-splitlines-keepends-v1"
        seed["address_provenance"] = {
            "report_canonical_sha256": report_sha, "manifest_sha256": report["manifest_sha256"],
            "selector": selector, "record_id": rid,
            **{key: binding[key] for key in ("record_content_hash", "capture", "attribution", "source_url")}}
        validate_seed(seed)
        expanded = binding["expansion"]
        _require(type(expanded) is dict and expanded.get("fresh") is True
                 and expanded.get("status") == "FRESH" and expanded.get("coordinates_valid") is True,
                 "Selected address was not freshly expanded")
        for field in SEED_FIELDS:
            _require(expanded.get(field) == binding[field], "Address expansion differs: " + field)
        for field in ("source_sha256", "slice_sha256"):
            _require(expanded.get("expected_" + field) == binding[field], "Address expected hash differs")
        text = expanded.get("text")
        _require(type(text) is str and len(text.encode("utf-8")) <= 16384
                 and hashlib.sha256(text.encode("utf-8")).hexdigest() == binding["slice_sha256"],
                 "Address expansion text hash or size differs")
        identity = tuple(seed[key] for key in sorted(SEED_FIELDS))
        _require(identity not in seen, "Two selections identify the same exact passage")
        seen.add(identity)
        seeds.append(seed)
    return {"root": report["scope"], "paths": sorted({seed["source"] for seed in seeds}),
            "seed_passages": seeds}
