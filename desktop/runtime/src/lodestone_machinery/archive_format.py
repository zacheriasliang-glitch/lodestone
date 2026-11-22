"""Strict, bounded metadata for portable exact-content archives.

This format preserves the documented content/metadata subset, not a complete
filesystem backup. A sidecar digest binds bytes; independent authenticity needs
an expected descriptor digest supplied from a trusted record.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
import unicodedata

MAX_ENTRIES = 500_000
MAX_TOTAL_BYTES = 128 * 1024**3
MAX_FILE_BYTES = 64 * 1024**3
MAX_ARCHIVE_BYTES = 4 * 1024**3
MAX_MANIFEST_BYTES = 256 * 1024**2
MAX_DESCRIPTOR_BYTES = 64 * 1024
CHUNK = CHUNK_BYTES = 1024**2
MANIFEST_SCHEMA = "lodestone-file-archive/v1"
DESCRIPTOR_SCHEMA = "lodestone-file-archive-descriptor/v1"
METADATA_SCOPE = (
    "Regular file bytes, relative names, directories, symlink targets, ordinary "
    "permissions and mtimes. No ownership, ACL, xattr/resource-fork, hardlink-identity, "
    "sparse-layout or atomic-filesystem-snapshot guarantee. Originals retained."
)
_HASH = re.compile(r"[0-9a-f]{64}\Z")


class ArchiveError(ValueError):
    """A refused archive operation; originals must remain untouched."""


def canonical(value):
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ArchiveError("Not canonical UTF-8 JSON") from exc


def digest(data):
    return hashlib.sha256(data).hexdigest()


def _integer(value, maximum, name, minimum=0):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ArchiveError(f"{name} must be an integer in {minimum}..{maximum}")
    return value


def _text(value, name, maximum=4096):
    if type(value) is not str or not value or "\0" in value:
        raise ArchiveError(f"{name} must be nonempty text without NUL")
    try:
        if len(value.encode("utf-8")) > maximum:
            raise ArchiveError(f"{name} exceeds UTF-8 byte limit")
    except UnicodeError as exc:
        raise ArchiveError(f"{name} must be UTF-8") from exc
    return value


def _keys(value, required, name):
    if type(value) is not dict or set(value) != set(required):
        raise ArchiveError(f"Unexpected {name} fields")


def _sha(value):
    if type(value) is not str or not _HASH.fullmatch(value):
        raise ArchiveError("Expected a lowercase SHA-256 digest")
    return value


def safe_path(value):
    _text(value, "relative path")
    if any(part in ("", ".", "..") for part in value.split("/")):
        raise ArchiveError("Path must be relative with no empty, dot or dotdot components")
    return value


def _portable_key(value):
    return unicodedata.normalize("NFD", value).casefold()


def validate_manifest(value):
    _keys(value, ("schema", "epoch", "authority_ref", "roots", "entries", "excluded",
                  "metadata_scope"), "manifest")
    if value["schema"] != MANIFEST_SCHEMA or value["metadata_scope"] != METADATA_SCOPE:
        raise ArchiveError("Unsupported archive schema or metadata scope")
    _text(value["epoch"], "epoch")
    _text(value["authority_ref"], "authority_ref")
    roots = value["roots"]
    if type(roots) is not list or not 1 <= len(roots) <= MAX_ENTRIES:
        raise ArchiveError("Manifest needs bounded nonempty roots")
    labels, folded = set(), set()
    for root in roots:
        _keys(root, ("label", "path"), "root")
        label = safe_path(root["label"])
        if "/" in label or _portable_key(label) in folded:
            raise ArchiveError("Root labels must be unique portable single components")
        labels.add(label)
        folded.add(_portable_key(label))
        _text(root["path"], "source root", maximum=32768)
        if not root["path"].startswith("/"):
            raise ArchiveError("Source root must be an absolute path")
    entries = value["entries"]
    if type(entries) is not list or not 1 <= len(entries) <= MAX_ENTRIES:
        raise ArchiveError("Manifest entry count outside bound")
    previous, by_path, folded, sizes, total = None, {}, set(), {}, 0
    for entry in entries:
        if type(entry) is not dict:
            raise ArchiveError("Expected manifest entry object")
        kind = entry.get("kind")
        extra = {"file": ("size", "sha256"), "directory": (), "symlink": ("target",)}
        if type(kind) is not str or kind not in extra:
            raise ArchiveError("Unsupported entry kind")
        _keys(entry, ("path", "kind", "mode", "mtime_ns", *extra[kind]), "entry")
        path = safe_path(entry["path"])
        if previous is not None and path <= previous:
            raise ArchiveError("Entries must be sorted by unique path")
        previous = path
        if _portable_key(path) in folded:
            raise ArchiveError("Duplicate case/Unicode-normalized path")
        folded.add(_portable_key(path))
        if path.split("/", 1)[0] not in labels:
            raise ArchiveError("Entry has undeclared root")
        if "/" not in path:
            if kind != "directory":
                raise ArchiveError("Root entry must be a directory")
        elif by_path.get(path.rsplit("/", 1)[0], {}).get("kind") != "directory":
            raise ArchiveError("Entry parent must be a declared directory")
        _integer(entry["mode"], 0o777, "mode")
        _integer(entry["mtime_ns"], 2**63 - 1, "mtime_ns")
        if kind == "file":
            size = _integer(entry["size"], MAX_FILE_BYTES, "file size")
            sha = _sha(entry["sha256"])
            if sha in sizes and sizes[sha] != size:
                raise ArchiveError("Equal blob hashes have inconsistent sizes")
            sizes[sha] = size
            total += size
            if total > MAX_TOTAL_BYTES:
                raise ArchiveError("Logical file bytes exceed archive bound")
        elif kind == "symlink":
            _text(entry["target"], "symlink target", maximum=32768)
        by_path[path] = entry
    if not labels.issubset(by_path):
        raise ArchiveError("Missing root directory entry")
    excluded = value["excluded"]
    if type(excluded) is not list or len(excluded) > MAX_ENTRIES:
        raise ArchiveError("Exclusion list exceeds bound")
    excluded_paths = set()
    for entry in excluded:
        _keys(entry, ("path", "reason"), "exclusion")
        path = safe_path(entry["path"])
        if path.split("/", 1)[0] not in labels or path in by_path or path in excluded_paths:
            raise ArchiveError("Invalid or duplicated exclusion path")
        excluded_paths.add(path)
        _text(entry["reason"], "exclusion reason")
    return value


def manifest_counts(manifest):
    counts = dict(files=0, directories=0, symlinks=0, unique_blobs=0,
                  logical_file_bytes=0, unique_file_bytes=0)
    blobs = {}
    names = {"file": "files", "directory": "directories", "symlink": "symlinks"}
    for entry in manifest["entries"]:
        counts[names[entry["kind"]]] += 1
        if entry["kind"] == "file":
            counts["logical_file_bytes"] += entry["size"]
            blobs[entry["sha256"]] = entry["size"]
    counts["unique_blobs"] = len(blobs)
    counts["unique_file_bytes"] = sum(blobs.values())
    return counts


def _identity(st):
    return (st.st_dev, st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _deadline(deadline):
    if deadline is not None and time.monotonic() >= deadline:
        raise ArchiveError("Archive operation exceeded its time bound")


def hash_file(path, *, max_bytes=MAX_ARCHIVE_BYTES, deadline=None):
    _integer(max_bytes, MAX_TOTAL_BYTES, "hash byte bound")
    path = Path(path)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
        raise ArchiveError("Expected a bounded regular file, not a symlink")
    sha, size = hashlib.sha256(), 0
    _deadline(deadline)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as handle:
        if _identity(before) != _identity(os.fstat(handle.fileno())):
            raise ArchiveError("File changed before hashing")
        while True:
            _deadline(deadline)
            chunk = handle.read(min(CHUNK, max_bytes - size + 1))
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise ArchiveError("File exceeds hash byte bound")
            sha.update(chunk)
        if _identity(before) != _identity(os.fstat(handle.fileno())):
            raise ArchiveError("File changed while hashing")
    if _identity(before) != _identity(path.lstat()) or size != before.st_size:
        raise ArchiveError("File changed after hashing")
    return {"bytes": size, "sha256": sha.hexdigest()}


def _read_regular(path, maximum):
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
        raise ArchiveError("Expected a bounded regular metadata file")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as handle:
        if _identity(before) != _identity(os.fstat(handle.fileno())):
            raise ArchiveError("Metadata file changed before reading")
        raw = handle.read(maximum + 1)
        if len(raw) > maximum or _identity(before) != _identity(os.fstat(handle.fileno())):
            raise ArchiveError("Metadata file changed or exceeded its bound")
    if _identity(before) != _identity(path.lstat()) or len(raw) != before.st_size:
        raise ArchiveError("Metadata file changed after reading")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ArchiveError("Invalid archive JSON") from exc
    if canonical(value) != raw:
        raise ArchiveError("Archive JSON is not canonical")
    return raw, value


def load_archive(directory, *, expected_descriptor_sha256=None):
    """Authenticate sidecars and file sizes; verify() authenticates payload SHA.

    Ancestors must be real directories: resolve a trusted OS alias such as /tmp
    before passing it here. No source path in the manifest is opened by readers.
    """
    directory = Path(os.path.abspath(os.fspath(directory)))
    for parent in (*reversed(directory.parents), directory):
        if not stat.S_ISDIR(parent.lstat().st_mode):
            raise ArchiveError("Archive directory components must not be symlinks")
    raw, descriptor = _read_regular(directory / "archive.json", MAX_DESCRIPTOR_BYTES)
    if expected_descriptor_sha256 is not None and digest(raw) != _sha(expected_descriptor_sha256):
        raise ArchiveError("Descriptor SHA-256 does not match expected pin")
    _keys(descriptor, ("schema", "archive", "manifest", "counts", "metadata_scope"), "descriptor")
    if descriptor["schema"] != DESCRIPTOR_SCHEMA or descriptor["metadata_scope"] != METADATA_SCOPE:
        raise ArchiveError("Unsupported descriptor schema or metadata scope")
    for name, filename, maximum in (("archive", "archive.tar.gz", MAX_ARCHIVE_BYTES),
                                    ("manifest", "manifest.json", MAX_MANIFEST_BYTES)):
        ref = descriptor[name]
        _keys(ref, ("path", "bytes", "sha256"), name)
        if ref["path"] != filename:
            raise ArchiveError("Unexpected archive component filename")
        _integer(ref["bytes"], maximum, name + " bytes")
        _sha(ref["sha256"])
        observed = (directory / filename).lstat()
        if not stat.S_ISREG(observed.st_mode) or observed.st_size != ref["bytes"]:
            raise ArchiveError("Archive component is not regular or has wrong size")
    manifest_raw, manifest = _read_regular(directory / "manifest.json", MAX_MANIFEST_BYTES)
    if digest(manifest_raw) != descriptor["manifest"]["sha256"]:
        raise ArchiveError("Manifest SHA-256 mismatch")
    validate_manifest(manifest)
    counts = manifest_counts(manifest)
    _keys(descriptor["counts"], counts, "counts")
    if any(type(v) is not int for v in descriptor["counts"].values()) or descriptor["counts"] != counts:
        raise ArchiveError("Descriptor counts do not match manifest")
    return descriptor, manifest
