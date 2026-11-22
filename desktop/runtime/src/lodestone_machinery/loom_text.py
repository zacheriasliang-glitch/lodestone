"""Exact UTF-8 inputs and retained-fold provenance for experimental Loom.

IDs and state are sidecars, never learned tokens. Binary labels describe supplied
record support, not factual truth. This module never changes native records.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import re

from .fold_export import plain_path, regular_bytes
from .records import canonical

PAD, CLS, SEP = 0, 1, 2
VOCABULARY = 259
SOURCE_BYTES, QUERY_BYTES, CANDIDATE_BYTES = 512, 128, 128
MAX_TOKENS = SOURCE_BYTES + QUERY_BYTES + CANDIDATE_BYTES + 4
MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
MAX_EXAMPLES = 2000
EXAMPLE_FIELDS = {"id", "source_text", "query", "candidate", "label", "provenance"}
PROVENANCE_FIELDS = {"source_sha256", "record_sha256", "claim_id", "evidence_ids", "byte_start", "byte_end"}
LABELS = {0: "NOT_SUPPORTED_BY_SUPPLIED_RECORD", 1: "SUPPORTED_BY_SUPPLIED_RECORD"}
BOUNDARY = {"semantic_validation": "NOT_ESTABLISHED", "seed_promotion": "NONE",
            "authority": "NONE", "label_zero": "NOT_SUPPORTED_DOES_NOT_MEAN_FACTUALLY_FALSE"}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _keys(value, fields, name):
    if type(value) is not dict or set(value) != set(fields):
        raise ValueError(name + " has missing or unknown fields")


def _text(value, maximum, name):
    if type(value) is not str or not value:
        raise ValueError(name + " must be a nonempty string")
    try:
        raw = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(name + " is not valid UTF-8") from exc
    if len(raw) > maximum:
        raise ValueError(name + " exceeds its UTF-8 byte bound; truncation is forbidden")
    return raw


def encode_text(source_text, query, candidate, *, allow_empty_source=False):
    """Encode three exact strings, with no normalization or metadata tokens."""
    if type(allow_empty_source) is not bool:
        raise ValueError("allow_empty_source must be explicit boolean")
    result = [CLS]
    for value, limit, name in ((source_text, SOURCE_BYTES, "source_text"),
                               (query, QUERY_BYTES, "query"),
                               (candidate, CANDIDATE_BYTES, "candidate")):
        raw = b"" if name == "source_text" and value == "" and allow_empty_source else _text(value, limit, name)
        result.extend(byte + 3 for byte in raw)
        result.append(SEP)
    return result


def _full_bytes(mapping, digest, name):
    if type(digest) is not str or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError(name + " requires a lowercase SHA-256 pin")
    if type(mapping) is not dict or digest not in mapping:
        raise ValueError("Missing complete " + name)
    raw = mapping[digest]
    if type(raw) is not bytes or not 1 <= len(raw) <= MAX_DOCUMENT_BYTES or sha(raw) != digest:
        raise ValueError(name + " full bytes differ from their pin or size bound")
    return raw


def _document(mapping, digest, name):
    raw = _full_bytes(mapping, digest, name)
    try:
        value = json.loads(raw)
        if type(value) is not dict or canonical(value) != raw:
            raise ValueError(name + " must be an exact canonical JSON object")
    except (UnicodeError, RecursionError, TypeError) as exc:
        raise ValueError("Invalid complete " + name) from exc
    return raw, value


def _fold(value):
    if type(value.get("claims")) is list:
        return value
    if type(value.get("fold")) is dict:
        return value["fold"]
    if type(value.get("payload")) is dict and type(value["payload"].get("fold")) is dict:
        return value["payload"]["fold"]
    raise ValueError("Expected a complete native fold or a record containing its fold")


def _strings(value):
    pending = [value]
    while pending:
        item = pending.pop()
        if type(item) is str:
            yield item
        elif type(item) is dict:
            pending.extend(item.values())
        elif type(item) is list:
            pending.extend(item)


def _selected_value(record, claim_id):
    """Return only selected content and declared links; retain all states inert."""
    from .loom_context import SCHEMA, validate_context
    if record.get("schema") == SCHEMA:
        context = validate_context(record)
        entries = [entry for entry in context["entries"] if entry["handle"] == claim_id]
        if len(entries) != 1:
            raise ValueError("Claim ID must identify exactly one compact-context entry handle")
        entry = entries[0]
        support = ([entry["observation"]] if entry["kind"] == "SEED" else
                   entry["observations"] + [entry["request"], entry["review"]])
        return entry["selected_content"], support
    fold = _fold(record)
    claims = fold.get("claims")
    if type(claims) is not list or len(claims) > 2000:
        raise ValueError("Malformed fold claims")
    selected = [claim for claim in claims if type(claim) is dict and claim.get("id") == claim_id]
    if len(selected) != 1:
        raise ValueError("Claim ID must identify exactly one retained claim")
    return selected[0].get("value"), selected[0].get("support")


def validate_example(example, sources, records, *, labeled=True):
    """Verify full snapshots and exact source-to-claim text correspondence.

    This authenticates consistency with the supplied pinned records. It does not
    independently establish their authorship, historical truth, or label quality.
    """
    fields = EXAMPLE_FIELDS if labeled else EXAMPLE_FIELDS - {"label"}
    _keys(example, fields, "example")
    _text(example["id"], 128, "id")
    encode_text(example["source_text"], example["query"], example["candidate"])
    if labeled and (type(example["label"]) is not int or example["label"] not in LABELS):
        raise ValueError("label must be integer 0 or 1; record state is not a label")
    sidecar = example["provenance"]
    _keys(sidecar, PROVENANCE_FIELDS, "provenance")
    _text(sidecar["claim_id"], 256, "claim_id")
    evidence = sidecar["evidence_ids"]
    if type(evidence) is not list or not 1 <= len(evidence) <= 32:
        raise ValueError("evidence_ids must contain 1..32 IDs")
    for item in evidence:
        _text(item, 256, "evidence_id")
    if len(set(evidence)) != len(evidence):
        raise ValueError("Duplicate evidence IDs")
    raw = _full_bytes(sources, sidecar["source_sha256"], "source")
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Complete source must be UTF-8") from exc
    _, record = _document(records, sidecar["record_sha256"], "record")
    selected_value, support = _selected_value(record, sidecar["claim_id"])
    if type(support) is not list or any(type(item) is not str for item in support) or not set(evidence) <= set(support):
        raise ValueError("Evidence IDs are not supported by the selected claim")
    text = example["source_text"]
    if not any(text in value for value in _strings(selected_value)):
        raise ValueError("Source text is absent from string leaves under the selected claim value or compact content")
    start, end = sidecar["byte_start"], sidecar["byte_end"]
    if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(raw):
        raise ValueError("Invalid source byte interval")
    if raw[start:end] != text.encode("utf-8"):
        raise ValueError("Selected source bytes differ from source_text")
    return json.loads(canonical(example))


def validate_dataset(examples, sources, records):
    if type(examples) is not list or not 1 <= len(examples) <= MAX_EXAMPLES:
        raise ValueError("Expected 1..2000 explicit examples")
    if len(canonical(examples)) > MAX_DOCUMENT_BYTES:
        raise ValueError("Dataset exceeds 4 MiB")
    rows = [validate_example(row, sources, records) for row in examples]
    if len({row["id"] for row in rows}) != len(rows):
        raise ValueError("Example IDs must be unique within a split")
    return rows


def distribution(examples):
    """Source/link counts for a previously validated split, never training input."""
    return {"examples": len(examples), "labels": dict(sorted(Counter(str(row["label"]) for row in examples).items())),
            "sources": dict(sorted(Counter(row["provenance"]["source_sha256"] for row in examples).items())),
            "records": dict(sorted(Counter(row["provenance"]["record_sha256"] for row in examples).items())),
            "claims": dict(sorted(Counter(row["provenance"]["claim_id"] for row in examples).items())),
            "evidence_links": dict(sorted(Counter(item for row in examples for item in row["provenance"]["evidence_ids"]).items()))}


def write_artifact(path, value):
    """Exclusive canonical JSON output; never pickle and never overwrite."""
    data = canonical(value)
    if len(data) > MAX_DOCUMENT_BYTES:
        raise ValueError("Artifact exceeds 4 MiB")
    destination = plain_path(path, new=True)
    with destination.open("xb") as stream:
        stream.write(data)
    destination.chmod(0o600)
    return {"sha256": sha(data), "bytes": len(data)}


def read_artifact(path, *, expected_sha256):
    raw = regular_bytes(path, MAX_DOCUMENT_BYTES)
    if sha(raw) != expected_sha256:
        raise ValueError("Artifact differs from its complete pinned bytes")
    value = json.loads(raw)
    if canonical(value) != raw:
        raise ValueError("Artifact is not exact canonical JSON")
    return value
