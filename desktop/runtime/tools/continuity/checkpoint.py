"""Explicit, portable checkpoints for resuming source-backed work.

This is a file ledger, not a native context-window controller or a backup.
It reads only named regular files and never follows source symlinks.
Preservation compares caller-pinned records for one task and source epoch;
it does not infer semantic resolution, authorization, or a new baseline.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

SCHEMA = "lodestone-checkpoint/v1"
MAX_SOURCES = 256
MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_CHECKPOINT_BYTES = 1024 * 1024


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def relative_name(name):
    if not isinstance(name, str) or not name or "\\" in name or ":" in name or "\x00" in name:
        raise ValueError("Source paths must be portable relative paths using '/' separators")
    if any(part in ("", ".", "..") for part in name.split("/")):
        raise ValueError("Source paths must not be absolute or contain empty, '.' or '..' components")
    return name


def project_root(root):
    root = Path(root).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("--root must be an existing directory")
    return root


def no_symlinks(path):
    """Reject both the selected leaf and symlink ancestors before opening."""
    for candidate in (path, *path.parents):
        if candidate.is_symlink():
            raise ValueError(f"Symlink paths are not supported: {candidate}")


def fingerprint(root, name):
    path = root / relative_name(name)
    # The resolved root is user-selected. Every component below it is checked.
    current = root
    for part in name.split("/"):
        current = current / part
        if current.is_symlink():
            raise ValueError(f"Source symlinks are not supported: {name}")
    before = path.stat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"Source must be a regular file: {name}")
    if before.st_size > MAX_SOURCE_BYTES:
        raise ValueError(f"Source exceeds the {MAX_SOURCE_BYTES}-byte read bound: {name}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    digest = hashlib.sha256()
    count = 0
    with os.fdopen(fd, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError(f"Source changed while opening: {name}")
        while chunk := stream.read(1024 * 1024):
            count += len(chunk)
            if count > MAX_SOURCE_BYTES:
                raise ValueError(f"Source exceeds the {MAX_SOURCE_BYTES}-byte read bound: {name}")
            digest.update(chunk)
        after = os.fstat(stream.fileno())
    path_after = path.stat()
    identity = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
    # Compare timestamp stability within the same API. On Windows, stat(path)
    # and fstat(fd) can expose different timestamp values for the same file.
    # Cross-API comparisons bind file identity and byte length instead.
    opened_changed = identity(opened) != identity(after)
    path_changed = identity(before) != identity(path_after)
    replaced = (after.st_dev, after.st_ino, after.st_size) != (path_after.st_dev, path_after.st_ino, path_after.st_size)
    if opened_changed or path_changed or replaced or count != after.st_size:
        raise ValueError(f"Source changed while hashing: {name}; handle_changed={opened_changed}, "
                         f"path_changed={path_changed}, replaced={replaced}, bytes_read={count}, "
                         f"handle_size={after.st_size}")
    return {"path": name, "bytes": count, "sha256": digest.hexdigest()}


def short_text(value, field):
    if not isinstance(value, str) or not value.strip() or len(value) > 12000:
        raise ValueError(f"{field} must be nonempty text of at most 12,000 characters")
    return value


def text_list(values, field):
    if not isinstance(values, list) or len(values) > 100:
        raise ValueError(f"{field} must be a list of at most 100 notes")
    return [short_text(value, field) for value in values]


def create(root, output, task, status, next_action, sources, notes=None, unresolved=None):
    root = project_root(root)
    output = Path(os.path.abspath(Path(output).expanduser()))
    no_symlinks(output)
    if not output.parent.is_dir():
        raise ValueError("Checkpoint output parent must already exist")
    if output.exists():
        raise FileExistsError(f"Checkpoint output already exists; choose a new filename: {output}")
    if not sources or len(sources) > MAX_SOURCES or len(set(sources)) != len(sources):
        raise ValueError(f"Select 1 to {MAX_SOURCES} distinct source files")
    payload = {"schema": SCHEMA, "created_at": datetime.now(timezone.utc).isoformat(),
               "task": short_text(task, "task"), "status": short_text(status, "status"),
               "next_action": short_text(next_action, "next_action"),
               "notes": text_list(notes or [], "notes"),
               "unresolved": text_list(unresolved or [], "unresolved"),
               "sources": [fingerprint(root, name) for name in sorted(sources)]}
    payload["checkpoint_sha256"] = hashlib.sha256(canonical(payload)).hexdigest()
    raw = json.dumps(payload, indent=2, ensure_ascii=True, allow_nan=False).encode("ascii") + b"\n"
    if len(raw) > MAX_CHECKPOINT_BYTES:
        raise ValueError("Checkpoint exceeds the 1 MiB size bound; shorten the notes")
    # Exclusive creation is the immutability contract: never replace earlier bytes.
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    return {"status": "CREATED", "path": str(output), "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "checkpoint_sha256": payload["checkpoint_sha256"], "sources": len(payload["sources"])}


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def expected_pin(value, field="Expected checkpoint SHA-256"):
    if (not isinstance(value, str) or len(value) != 64
            or any(char not in "0123456789abcdef" for char in value)):
        raise ValueError(f"{field} must be 64 lowercase hexadecimal characters")
    return value


def _checkpoint_bytes(path, expected_sha256=None):
    if expected_sha256 is not None:
        expected_pin(expected_sha256)
    path = Path(os.path.abspath(Path(path).expanduser()))
    no_symlinks(path)
    if not path.is_file():
        raise ValueError("Checkpoint must be a regular file")
    with path.open("rb") as stream:
        raw = stream.read(MAX_CHECKPOINT_BYTES + 1)
    if len(raw) > MAX_CHECKPOINT_BYTES:
        raise ValueError("Checkpoint exceeds the 1 MiB size bound")
    actual_sha256 = hashlib.sha256(raw).hexdigest()
    if expected_sha256 is not None and actual_sha256 != expected_sha256:
        raise ValueError("Checkpoint file differs from expected raw-file SHA-256")
    return raw, actual_sha256


def _parse_checkpoint(raw):
    value = json.loads(raw, object_pairs_hook=unique_object)
    fields = {"schema", "created_at", "task", "status", "next_action", "notes", "unresolved", "sources", "checkpoint_sha256"}
    if not isinstance(value, dict) or set(value) != fields or value["schema"] != SCHEMA:
        raise ValueError("Unsupported checkpoint schema or fields")
    digest = value["checkpoint_sha256"]
    unsigned = {key: item for key, item in value.items() if key != "checkpoint_sha256"}
    if not isinstance(digest, str) or hashlib.sha256(canonical(unsigned)).hexdigest() != digest:
        raise ValueError("Checkpoint digest mismatch")
    for field in ("created_at", "task", "status", "next_action"):
        short_text(value[field], field)
    text_list(value["notes"], "notes")
    text_list(value["unresolved"], "unresolved")
    sources = value["sources"]
    if not isinstance(sources, list) or not 1 <= len(sources) <= MAX_SOURCES:
        raise ValueError("Invalid checkpoint source count")
    names = set()
    for source in sources:
        if not isinstance(source, dict) or set(source) != {"path", "bytes", "sha256"}:
            raise ValueError("Invalid checkpoint source entry")
        name = relative_name(source["path"])
        if name in names:
            raise ValueError("Duplicate checkpoint source path")
        names.add(name)
        sha = source["sha256"]
        if (type(source["bytes"]) is not int or not 0 <= source["bytes"] <= MAX_SOURCE_BYTES
                or not isinstance(sha, str) or len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha)):
            raise ValueError("Invalid checkpoint byte count or SHA-256")
    return value


def read_checkpoint(path, *, expected_sha256=None):
    """Read a v1 record; check an optional external raw-file pin before parsing."""
    raw, _ = _checkpoint_bytes(path, expected_sha256)
    return _parse_checkpoint(raw)


def _verify_sources(value, root):
    root = project_root(root)
    comparisons = []
    for expected in value["sources"]:
        row = {"path": expected["path"], "expected": expected}
        try:
            actual = fingerprint(root, expected["path"])
            row.update(status="MATCH" if actual == expected else "CHANGED", actual=actual)
        except FileNotFoundError:
            row.update(status="MISSING")
        except (OSError, ValueError) as exc:
            row.update(status="UNREADABLE", error=str(exc)[:1000])
        comparisons.append(row)
    failures = sum(row["status"] != "MATCH" for row in comparisons)
    return comparisons, failures


def verify(checkpoint, root, *, expected_sha256=None):
    """Check listed bytes; an absent external pin means self-consistency only."""
    raw, file_sha256 = _checkpoint_bytes(checkpoint, expected_sha256)
    value = _parse_checkpoint(raw)
    comparisons, failures = _verify_sources(value, root)
    return {"status": "VERIFIED" if not failures else "DRIFT", "failures": failures,
            "file_sha256": file_sha256, "external_pin_checked": expected_sha256 is not None,
            "checkpoint_sha256": value["checkpoint_sha256"],
            "created_at": value["created_at"], "task": value["task"],
            "recorded_status": value["status"], "next_action": value["next_action"],
            "notes": value["notes"], "unresolved": value["unresolved"], "sources": comparisons,
            "scope": "Named file bytes only. Notes are recorded claims; no commands were replayed."}


def _missing_text(previous, candidate):
    """Keep exact text, including repeated records; order need not be retained."""
    remaining = list(candidate)
    missing = []
    for text in previous:
        if text in remaining:
            remaining.remove(text)
        else:
            missing.append(text)
    return missing


def preserve(predecessor, candidate, root, *, predecessor_sha256, candidate_sha256):
    """Check a same-task, fixed-source-epoch extension without writing anything.

    Both raw-file pins must come from independent earlier observations. They
    bind bytes, not trusted authorship or authorization. Every predecessor
    source identity, note and unresolved item must survive. Additions and
    reordered notes are allowed; rewording or resolving inherited text requires
    a separately reviewed new baseline, never an automatic drop or rebind.
    All candidate source files are reopened, including additions. A passing
    report covers these finite observations, not semantic decision preservation.
    """
    expected_pin(predecessor_sha256, "Predecessor SHA-256")
    expected_pin(candidate_sha256, "Candidate SHA-256")
    # Check BOTH external identities before interpreting either record.
    before_raw, before_sha = _checkpoint_bytes(predecessor, predecessor_sha256)
    after_raw, after_sha = _checkpoint_bytes(candidate, candidate_sha256)
    before, after = _parse_checkpoint(before_raw), _parse_checkpoint(after_raw)
    losses = []
    if after["task"] != before["task"]:
        losses.append({"kind": "TASK_CHANGED", "expected": before["task"], "candidate": after["task"]})
    candidate_sources = {item["path"]: item for item in after["sources"]}
    for expected in before["sources"]:
        actual = candidate_sources.get(expected["path"])
        if actual != expected:
            losses.append({"kind": "SOURCE_IDENTITY_NOT_RETAINED", "expected": expected,
                           "candidate": actual})
    for field in ("notes", "unresolved"):
        for text in _missing_text(before[field], after[field]):
            losses.append({"kind": "TEXT_NOT_RETAINED", "field": field, "text": text})
    comparisons, source_failures = _verify_sources(after, root)
    status = "LOSS" if losses else "DRIFT" if source_failures else "PRESERVED"
    return {"status": status, "failures": len(losses) + source_failures,
            "predecessor_sha256": before_sha, "candidate_sha256": after_sha,
            "task": after["task"], "losses": losses,
            "source_status": "DRIFT" if source_failures else "VERIFIED",
            "source_failures": source_failures, "sources": comparisons,
            "scope": "Same task and fixed source identities; exact recorded notes and unresolved text. "
                     "All candidate files reopened. No semantic resolution, authentication, authorization, "
                     "native compaction, backup or command replay is established."}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    save = commands.add_parser("create", help="Create a new checkpoint without replacing any file")
    save.add_argument("--root", required=True, type=Path, help="Project root for relative source names")
    save.add_argument("--output", required=True, type=Path, help="New checkpoint filename; parent must exist")
    save.add_argument("--task", required=True)
    save.add_argument("--status", required=True, help="Recorded task status, not an execution verdict")
    save.add_argument("--next-action", required=True)
    save.add_argument("--source", action="append", required=True, help="Explicit regular file relative to --root; repeatable")
    save.add_argument("--note", action="append", default=[])
    save.add_argument("--unresolved", action="append", default=[])
    check = commands.add_parser("verify", help="Read a checkpoint and detect changed or missing source bytes")
    check.add_argument("checkpoint", type=Path)
    check.add_argument("--root", required=True, type=Path, help="Current root; may differ from the original machine")
    check.add_argument("--expected-sha256", help="Independent raw-file SHA-256; omit for self-consistency only")
    keep = commands.add_parser("preserve", help="Check a pinned same-task checkpoint transition and reopen candidate sources")
    keep.add_argument("predecessor", type=Path)
    keep.add_argument("candidate", type=Path)
    keep.add_argument("--root", required=True, type=Path)
    keep.add_argument("--predecessor-sha256", required=True, help="Independently supplied predecessor raw-file SHA-256")
    keep.add_argument("--candidate-sha256", required=True, help="Independently supplied candidate raw-file SHA-256")
    options = parser.parse_args(argv)
    try:
        if options.command == "create":
            result = create(options.root, options.output, options.task, options.status,
                            options.next_action, options.source, options.note, options.unresolved)
        elif options.command == "verify":
            result = verify(options.checkpoint, options.root, expected_sha256=options.expected_sha256)
        else:
            result = preserve(options.predecessor, options.candidate, options.root,
                              predecessor_sha256=options.predecessor_sha256,
                              candidate_sha256=options.candidate_sha256)
        print(json.dumps(result, indent=2, ensure_ascii=True))
        return 1 if result["status"] in ("DRIFT", "LOSS") else 0
    except (OSError, ValueError, TypeError) as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)[:1000]}, ensure_ascii=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
