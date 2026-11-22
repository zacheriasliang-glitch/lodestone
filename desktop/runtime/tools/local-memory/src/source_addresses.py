"""Explicit, bounded bindings from memory records to captured source passages.

The resolver reads only. Bindings are a separate, single-writer manifest; they
authenticate addresses and bytes, not the truth or relevance of source claims.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tempfile
from urllib.parse import urlsplit

import lodestone_memory as memory

# Same checked-out package as the existing standalone memory CLI; no install.
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
from lodestone_machinery.organs.sources import anchor, expand

SCHEMA = "lodestone-source-bindings/v1"
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_MEMORY_BYTES = 1024 * 1024
MAX_BINDINGS = 512
MAX_RECORDS = 512
MAX_LINKS = 1024
MAX_EXCERPT_BYTES = 16 * 1024
MAX_REOPEN_BYTES = 16 * 1024 * 1024
MAX_REOPEN_BINDINGS = 64
MAX_RESULT_BYTES = 1024 * 1024
SHA = re.compile(r"[0-9a-f]{64}\Z")
ID = re.compile(r"[0-9a-f]{16}\Z")
FIELDS = {"record_id", "record_content_hash", "capture", "attribution",
          "source_url", "source", "start_line", "end_line",
          "source_sha256", "slice_sha256"}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _text(value, maximum: int, label: str) -> None:
    _require(type(value) is str and bool(value.strip()) and "\0" not in value,
             f"{label} must be nonempty text")
    _require(len(value.encode("utf-8")) <= maximum, f"{label} exceeds byte bound")


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _pairs(items):
    result = {}
    for key, value in items:
        _require(key not in result, f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _constant(value):
    raise ValueError(f"Nonfinite JSON value: {value}")


def _json(raw: bytes):
    try:
        return json.loads(raw.decode("utf-8", errors="strict"),
                          object_pairs_hook=_pairs, parse_constant=_constant)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("Invalid bounded UTF-8 JSON") from exc


def _read(path: Path, limit: int) -> bytes:
    # A manifest/catalogue is an ordinary file, not a device or a linked target.
    info = path.lstat()
    _require(stat.S_ISREG(info.st_mode), f"Expected a regular non-symlink file: {path}")
    _require(info.st_size <= limit, f"File exceeds byte bound: {path}")
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    _require(len(raw) <= limit, f"File exceeds byte bound: {path}")
    return raw


def _relative(value: str) -> None:
    _text(value, 1024, "source")
    _require("\\" not in value and not PurePosixPath(value).is_absolute()
             and all(part not in ("", ".", "..") for part in value.split("/")),
             "Source must be a contained relative POSIX path")


def _binding(value: dict) -> None:
    _require(type(value) is dict and set(value) == FIELDS, "Invalid binding fields")
    _require(type(value["record_id"]) is str and ID.fullmatch(value["record_id"]),
             "Invalid bound record id")
    for key in ("record_content_hash", "source_sha256", "slice_sha256"):
        _require(type(value[key]) is str and SHA.fullmatch(value[key]), f"Invalid {key}")
    _text(value["capture"], 256, "capture")
    _text(value["attribution"], 1024, "attribution")
    _relative(value["source"])
    start, end = value["start_line"], value["end_line"]
    _require(type(start) is int and type(end) is int and 1 <= start <= end <= 2097152,
             "Invalid passage line interval")
    url = value["source_url"]
    if url is not None:
        _text(url, 2048, "source_url")
        parsed = urlsplit(url)
        _require(parsed.scheme in ("http", "https") and bool(parsed.netloc)
                 and parsed.username is None and parsed.password is None,
                 "source_url must be an HTTP(S) attribution URL without credentials")


def _manifest_path(path: Path, root: Path) -> Path:
    path = Path(path)
    try:
        root = Path(root).resolve()
        _require(not path.is_symlink(), "Binding manifest must not be a symlink")
        target = path.resolve()
    except RuntimeError as exc:
        raise ValueError("Invalid manifest path or symlink loop") from exc
    _require(not target.is_relative_to(root), "Keep the binding manifest outside the memory store")
    _require(target.parent.is_dir(), "Binding manifest parent directory must exist")
    return target


def _manifest(path: Path, *, allow_missing: bool = False) -> tuple[dict, bytes | None]:
    try:
        raw = _read(path, MAX_MANIFEST_BYTES)
    except FileNotFoundError:
        if allow_missing:
            return {"schema": SCHEMA, "bindings": []}, None
        raise ValueError("Binding manifest is unavailable") from None
    data = _json(raw)
    _require(type(data) is dict and set(data) == {"schema", "bindings"}
             and data["schema"] == SCHEMA and type(data["bindings"]) is list,
             "Invalid binding manifest schema")
    _require(len(data["bindings"]) <= MAX_BINDINGS, "Too many source bindings")
    seen = set()
    for entry in data["bindings"]:
        _binding(entry)
        identity = _canonical(entry)
        _require(identity not in seen, "Duplicate source binding")
        seen.add(identity)
    return data, raw


def _catalogue(root: Path) -> tuple[list[dict], list[dict]]:
    root = Path(root)
    _require(root.is_dir(), "Memory store is unavailable")
    records_raw = _read(root / "records.jsonl", MAX_MEMORY_BYTES)
    links_raw = _read(root / "links.jsonl", MAX_MEMORY_BYTES)
    _require(len(records_raw) + len(links_raw) <= MAX_MEMORY_BYTES,
             "Memory catalogue exceeds total byte bound")
    records = [_json(line) for line in records_raw.splitlines() if line.strip()]
    links = [_json(line) for line in links_raw.splitlines() if line.strip()]
    _require(len(records) <= MAX_RECORDS and len(links) <= MAX_LINKS,
             "Memory catalogue exceeds record/link bound")
    ids = set()
    for record in records:
        _require(type(record) is dict, "Invalid memory record")
        for key in ("title", "body", "source"):
            _text(record.get(key), MAX_MEMORY_BYTES, f"record {key}")
        rid = record.get("id")
        title, body, source = (memory.normalize(record[k]) for k in ("title", "body", "source"))
        expected = memory.sha256(f"{source}\n{title}\n{body}")[:16]
        _require(type(rid) is str and rid == expected and rid not in ids,
                 "Memory record identity mismatch or duplicate")
        _require(record.get("content_hash") == memory.sha256(body),
                 "Memory record content hash mismatch")
        tags, confidence = record.get("tags"), record.get("confidence")
        _require(type(tags) is list and all(type(tag) is str for tag in tags), "Invalid tags")
        _require(type(confidence) in (int, float) and 0 <= confidence <= 1
                 and math.isfinite(confidence), "Invalid confidence")
        _require("superseded_by" in record and (record["superseded_by"] is None
                 or type(record["superseded_by"]) is str), "Invalid supersession")
        ids.add(rid)
    for record in records:
        _require(record["superseded_by"] is None or record["superseded_by"] in ids,
                 "Unknown superseding record")
    for link in links:
        _require(type(link) is dict and type(link.get("source_id")) is str
                 and type(link.get("target_id")) is str
                 and link["source_id"] in ids and link["target_id"] in ids
                 and type(link.get("type")) is str and link["type"] in memory.VALID_LINK_TYPES
                 and type(link.get("note", "")) is str, "Invalid memory link")
    return records, links


def _location(scope: Path, source: str) -> Path:
    """Differentiate absent capture from invalid/escaping paths before expand."""
    _relative(source)
    try:
        root = Path(scope).resolve(strict=True)
        _require(root.is_dir(), "Capture scope must be a directory")
        target = (root / source).resolve(strict=False)
        _require(target.is_relative_to(root), "Source escapes captured scope")
        info = target.stat()
    except RuntimeError as exc:
        raise ValueError("Invalid source path or symlink loop") from exc
    _require(stat.S_ISREG(info.st_mode), "Source must be a regular file")
    return target


def add_binding(root: Path, manifest: Path, scope: Path, record_id: str,
                source: str, start_line: int, end_line: int,
                capture: str, attribution: str, source_url: str | None = None) -> dict:
    """Append an explicit source binding atomically; never modify memory/source."""
    path = _manifest_path(manifest, root)
    records, _ = _catalogue(root)
    matches = [r for r in records if r["id"] == record_id]
    _require(len(matches) == 1, "Unknown memory record")
    target = _location(scope, source)
    _require(target != path, "A binding manifest cannot be its own source")
    _require(type(start_line) is int and type(end_line) is int
             and 1 <= start_line <= end_line, "Invalid passage line interval")
    original = anchor(Path(scope), source)
    lines = original["text"].splitlines(keepends=True)
    _require(end_line <= len(lines), "Passage extends past source EOF")
    excerpt = "".join(lines[start_line - 1:end_line]).encode("utf-8")
    _require(len(excerpt) <= MAX_EXCERPT_BYTES, "Selected passage exceeds byte bound")
    entry = {"record_id": record_id, "record_content_hash": matches[0]["content_hash"],
             "source": source, "start_line": start_line, "end_line": end_line,
             "source_sha256": original["sha256"], "slice_sha256": _digest(excerpt),
             "capture": capture, "attribution": attribution, "source_url": source_url}
    _binding(entry)
    data, old_raw = _manifest(path, allow_missing=True)
    if entry in data["bindings"]:
        return {"created": False, "binding": entry, "text": excerpt.decode("utf-8")}
    _require(len(data["bindings"]) < MAX_BINDINGS, "Too many source bindings")
    data["bindings"].append(entry)
    new_raw = _canonical(data) + b"\n"
    _require(len(new_raw) <= MAX_MANIFEST_BYTES, "Binding manifest exceeds byte bound")
    # Ordinary single-writer file semantics, not a multi-process transaction.
    # Refuse observed changes; replace only after all validation is complete.
    if old_raw is None:
        _require(not path.exists(), "Binding manifest appeared during preparation")
    else:
        _require(_read(path, MAX_MANIFEST_BYTES) == old_raw,
                 "Binding manifest changed during preparation")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent,
                                         prefix=".source-bindings-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(new_raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {"created": True, "binding": entry, "text": excerpt.decode("utf-8")}


def _reopen(scope: Path, entry: dict, record: dict, remaining: list[int]) -> dict:
    result = {**entry, "status": "STALE", "expansion": None}
    _require(entry["record_content_hash"] == record["content_hash"],
             "Binding does not match memory record content")
    try:
        target = _location(scope, entry["source"])
        size = target.stat().st_size
        _require(size <= remaining[0], "Source reopening exceeds total byte bound")
        remaining[0] -= size
        reopened = expand(Path(scope), entry)
    except FileNotFoundError:
        result.update(status="UNAVAILABLE", reason="Captured source or scope is missing")
        return result
    _require(len(reopened["text"].encode("utf-8")) <= MAX_EXCERPT_BYTES,
             "Reopened passage exceeds byte bound")
    result.update(status="VERIFIED_ADDRESS" if reopened["fresh"] else "STALE",
                  expansion=reopened)
    return result


def resolve(root: Path, manifest: Path, scope: Path, query: str,
            limit: int = 10, include_superseded: bool = False) -> dict:
    """Return candidate addresses, preserving ambiguity and exact source drift."""
    _require(type(query) is str and len(query.encode("utf-8")) <= 4096, "Invalid query")
    _require(type(limit) is int and 1 <= limit <= 10, "limit must be between 1 and 10")
    _require(type(include_superseded) is bool, "include_superseded must be boolean")
    path = _manifest_path(manifest, root)
    data, raw = _manifest(path)
    records, links = _catalogue(root)
    candidates = memory.search_records(records, links, query, limit + 1, include_superseded)
    results = []
    remaining = [MAX_REOPEN_BYTES]
    reopened_count = 0
    for hit in candidates[:limit]:
        entries = [e for e in data["bindings"] if e["record_id"] == hit.record["id"]]
        reopened_count += len(entries)
        _require(reopened_count <= MAX_REOPEN_BINDINGS, "Too many bindings to reopen in one query")
        bound = [_reopen(scope, entry, hit.record, remaining) for entry in entries]
        status = ("UNKNOWN_WITHIN_SCOPE" if not bound else
                  "AMBIGUOUS" if len(bound) > 1 else bound[0]["status"])
        results.append({"record_id": hit.record["id"], "title": hit.record["title"],
                        "score": hit.score, "superseded_by": hit.record["superseded_by"],
                        "status": status, "bindings": bound})
    status = ("UNKNOWN_WITHIN_SCOPE" if not results else "AMBIGUOUS" if len(candidates) > 1
              else results[0]["status"])
    report = {"schema": "lodestone-source-addresses/v1", "query": query,
            "scope": str(Path(scope).resolve()), "manifest_sha256": _digest(raw),
            "status": status, "truncated": len(candidates) > limit, "results": results,
            "meaning": "Verified source address and bytes; not source truth, semantic support, or learning"}
    _require(len(_canonical(report)) + 1 <= MAX_RESULT_BYTES, "Address result exceeds byte bound")
    return report
