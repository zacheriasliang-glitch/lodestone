"""Bounded, streaming file archives; source files are never changed or removed."""
from __future__ import annotations

from contextlib import contextmanager
import gzip
import hashlib
import io
import math
import os
from pathlib import Path
import stat
import tarfile
import time
import unicodedata

from . import archive_format as fmt


def _check(deadline):
    if time.monotonic() >= deadline:
        raise fmt.ArchiveError("Archive time allowance exhausted")


def _deadline(seconds):
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not 0 < seconds <= 21600:
        raise fmt.ArchiveError("max_seconds must be positive, finite and at most 21600")
    return time.monotonic() + seconds


def _identity(value):
    # Reading may change atime. Every other observable identity field matters.
    return (value.st_dev, value.st_ino, value.st_mode, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns, value.st_nlink)


def _same(value, expected, path):
    if _identity(value) != expected:
        raise fmt.ArchiveError("Source changed during archive: " + str(path))


def _component(value):
    fmt.safe_path(value)
    if "/" in value:
        raise fmt.ArchiveError("Expected a single path component")
    return value


def _settings(roots, exclude_names, exclude_paths, max_entries, max_bytes, deadline):
    if not isinstance(roots, dict) or not roots:
        raise fmt.ArchiveError("At least one labelled directory root is required")
    for name, value, ceiling in (("max_entries", max_entries, fmt.MAX_ENTRIES),
                                 ("max_bytes", max_bytes, fmt.MAX_TOTAL_BYTES)):
        if type(value) is not int or not 0 <= value <= ceiling or (name == "max_entries" and value == 0):
            raise fmt.ArchiveError(name + " exceeds its supported bound")
    if not isinstance(exclude_names, (tuple, list)):
        raise fmt.ArchiveError("exclude_names must be a sequence of names")
    excluded = frozenset(_component(name) for name in exclude_names)
    if not isinstance(exclude_paths, (tuple, list)):
        raise fmt.ArchiveError("exclude_paths must be a sequence of exact relative paths")
    selected_exclusions = frozenset(fmt.safe_path(path) for path in exclude_paths)
    if len(roots) > max_entries:
        raise fmt.ArchiveError("Source roots exceed entry bound")
    prepared, folded = {}, set()
    for label, value in roots.items():
        _check(deadline)
        _component(label)
        key = unicodedata.normalize("NFD", label).casefold()
        if key in folded:
            raise fmt.ArchiveError("Root labels collide after case/Unicode normalization")
        folded.add(key)
        path = Path(value).absolute()
        if path.is_symlink() or not stat.S_ISDIR(path.lstat().st_mode):
            raise fmt.ArchiveError("Source roots must be regular directories, not symlinks")
        # Resolve existing aliases once, then open every canonical component with
        # O_NOFOLLOW. The chosen physical path is recorded in the manifest.
        prepared[label] = path.resolve(strict=True)
    for path in selected_exclusions:
        if "/" not in path or path.split("/", 1)[0] not in prepared:
            raise fmt.ArchiveError("Exact exclusions must name a nonroot path under a declared root")
    return dict(sorted(prepared.items())), excluded, selected_exclusions


def _open_absolute_dir(path, deadline):
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path.anchor, flags)
    try:
        for component in path.parts[1:]:
            _check(deadline)
            next_fd = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
            if not stat.S_ISDIR(os.fstat(fd).st_mode):
                raise fmt.ArchiveError("Source ancestor is not a directory")
        return fd
    except BaseException:
        os.close(fd)
        raise


@contextmanager
def _directory(roots, path, identities, deadline):
    parts = path.split("/")
    fd = _open_absolute_dir(roots[parts[0]], deadline)
    try:
        key = parts[0]
        if key in identities:
            _same(os.fstat(fd), identities[key], key)
        for component in parts[1:]:
            _check(deadline)
            next_fd = os.open(component, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                              | getattr(os, "O_NOFOLLOW", 0), dir_fd=fd)
            os.close(fd)
            fd = next_fd
            key += "/" + component
            _same(os.fstat(fd), identities[key], key)
        yield fd
        _same(os.fstat(fd), identities[path], path)
    finally:
        os.close(fd)


def _inventory(roots, excluded_names, excluded_paths, max_entries, max_bytes, deadline):
    entries, excluded, identities, folded = [], [], {}, set()
    matched_exclusions = set()
    total, visited = 0, 0

    def record(path, info, target=None):
        nonlocal total, visited
        _check(deadline)
        fmt.safe_path(path)
        key = unicodedata.normalize("NFD", path).casefold()
        if key in folded:
            raise fmt.ArchiveError("Source paths collide after case/Unicode normalization")
        folded.add(key)
        visited += 1
        if visited > max_entries:
            raise fmt.ArchiveError("Source inventory exceeds entry bound")
        mode = info.st_mode
        kind = "directory" if stat.S_ISDIR(mode) else "file" if stat.S_ISREG(mode) else "symlink" if stat.S_ISLNK(mode) else None
        if kind is None:
            raise fmt.ArchiveError("Unsupported filesystem entry: " + path)
        if not 0 <= info.st_mtime_ns <= 2**63 - 1:
            raise fmt.ArchiveError("Filesystem mtime outside supported bound: " + path)
        item = {"path": path, "kind": kind, "mode": stat.S_IMODE(mode) & 0o777,
                "mtime_ns": info.st_mtime_ns}
        if kind == "file":
            if not 0 <= info.st_size <= fmt.MAX_FILE_BYTES:
                raise fmt.ArchiveError("Source file exceeds byte bound: " + path)
            total += info.st_size
            if total > max_bytes:
                raise fmt.ArchiveError("Source inventory exceeds logical byte bound")
            item["size"] = info.st_size
        elif kind == "symlink":
            if not isinstance(target, str) or not target or "\x00" in target:
                raise fmt.ArchiveError("Invalid symlink target: " + path)
            try:
                target.encode("utf-8")
            except UnicodeError as exc:
                raise fmt.ArchiveError("Symlink target is not UTF-8") from exc
            item["target"] = target
        entries.append(item)
        identities[path] = _identity(info)

    for label, root in roots.items():
        fd = _open_absolute_dir(root, deadline)
        try:
            info = os.fstat(fd)
            if not stat.S_ISDIR(info.st_mode):
                raise fmt.ArchiveError("Source root is not a directory")
            record(label, info)
        finally:
            os.close(fd)
        pending = [label]
        while pending:
            directory = pending.pop()
            with _directory(roots, directory, identities, deadline) as directory_fd:
                # Bound enumeration before sorting or retaining a whole directory.
                children = []
                with os.scandir(directory_fd) as scanner:
                    for child in scanner:
                        _check(deadline)
                        if visited + len(children) >= max_entries:
                            raise fmt.ArchiveError("Source inventory exceeds entry bound")
                        children.append(child.name)
                for name in sorted(children):
                    _check(deadline)
                    path = directory + "/" + name
                    fmt.safe_path(path)
                    if path in excluded_paths or name in excluded_names:
                        visited += 1
                        key = unicodedata.normalize("NFD", path).casefold()
                        if key in folded:
                            raise fmt.ArchiveError("Excluded source paths collide after case/Unicode normalization")
                        folded.add(key)
                        if path in excluded_paths:
                            matched_exclusions.add(path)
                        reason = "excluded exact path" if path in excluded_paths else "excluded name: " + name
                        excluded.append({"path": path, "reason": reason})
                        continue
                    info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    target = os.readlink(name, dir_fd=directory_fd) if stat.S_ISLNK(info.st_mode) else None
                    _same(os.stat(name, dir_fd=directory_fd, follow_symlinks=False), _identity(info), path)
                    record(path, info, target)
                    if stat.S_ISDIR(info.st_mode):
                        pending.append(path)
    if matched_exclusions != excluded_paths:
        missing = sorted(excluded_paths - matched_exclusions)
        raise fmt.ArchiveError("Exact exclusion was not encountered (missing or hidden by another exclusion): " + missing[0])
    return {"entries": sorted(entries, key=lambda item: item["path"]),
            "excluded": sorted(excluded, key=lambda item: item["path"]),
            "identities": identities}


@contextmanager
def _source(roots, item, identities, deadline):
    path = item["path"]
    parent, name = path.rsplit("/", 1)
    with _directory(roots, parent, identities, deadline) as directory_fd:
        fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_NONBLOCK", 0), dir_fd=directory_fd)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise fmt.ArchiveError("Source is no longer a regular file: " + path)
            _same(info, identities[path], path)
            with os.fdopen(fd, "rb", buffering=0) as stream:
                fd = None
                yield stream
                _same(os.fstat(stream.fileno()), identities[path], path)
            _same(os.stat(name, dir_fd=directory_fd, follow_symlinks=False), identities[path], path)
        finally:
            if fd is not None:
                os.close(fd)


def _hash_source(roots, item, identities, deadline):
    total, hasher = 0, hashlib.sha256()
    with _source(roots, item, identities, deadline) as stream:
        while True:
            _check(deadline)
            chunk = stream.read(min(fmt.CHUNK_BYTES, item["size"] - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > item["size"]:
                raise fmt.ArchiveError("Source grew during archive: " + item["path"])
            hasher.update(chunk)
    if total != item["size"]:
        raise fmt.ArchiveError("Source size changed during archive: " + item["path"])
    return hasher.hexdigest()


class _BoundedOutput:
    def __init__(self, stream, deadline):
        self.stream, self.deadline, self.count = stream, deadline, 0

    def write(self, data):
        _check(self.deadline)
        if self.count + len(data) > fmt.MAX_ARCHIVE_BYTES:
            raise fmt.ArchiveError("Compressed archive exceeds byte bound")
        result = self.stream.write(data)
        self.count += result
        return result

    def flush(self):
        self.stream.flush()


class _HashingReader:
    def __init__(self, stream, deadline):
        self.stream, self.deadline = stream, deadline
        self.hasher, self.count = hashlib.sha256(), 0

    def read(self, size):
        _check(self.deadline)
        if not 0 <= size <= fmt.CHUNK_BYTES:
            raise fmt.ArchiveError("Archive requested an unbounded source read")
        data = self.stream.read(size)
        self.hasher.update(data)
        self.count += len(data)
        return data


def _member(name, size):
    value = tarfile.TarInfo(name)
    value.size, value.mode, value.mtime = size, 0o600, 0
    value.uid = value.gid = 0
    value.uname = value.gname = ""
    return value


def _destination(value, roots):
    requested = Path(value).absolute()
    if requested.exists() or requested.is_symlink():
        raise fmt.ArchiveError("Archive destination already exists")
    parent = requested.parent.resolve(strict=True)
    destination = parent / requested.name
    destination_key = tuple(unicodedata.normalize("NFD", part).casefold() for part in destination.parts)
    for root in roots.values():
        root_key = tuple(unicodedata.normalize("NFD", part).casefold() for part in root.parts)
        if (destination_key[:len(root_key)] == root_key
                or root_key[:len(destination_key)] == destination_key):
            raise fmt.ArchiveError("Archive destination must not overlap any source root")
    return destination


def _container_identity(info):
    return info.st_dev, info.st_ino, info.st_mode


def _create_destination(destination, deadline):
    parent_fd = _open_absolute_dir(destination.parent, deadline)
    try:
        os.mkdir(destination.name, mode=0o700, dir_fd=parent_fd)
        fd = os.open(destination.name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                     | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
        try:
            _check(deadline)
            # Persist the new archive directory's entry in its existing parent.
            os.fsync(parent_fd)
            _check(deadline)
            return _container_identity(os.fstat(fd))
        finally:
            os.close(fd)
    finally:
        os.close(parent_fd)


def _check_destination(destination, expected, deadline):
    fd = _open_absolute_dir(destination, deadline)
    try:
        if _container_identity(os.fstat(fd)) != expected:
            raise fmt.ArchiveError("Archive destination changed during writing")
    finally:
        os.close(fd)


def _sync_destination(destination, expected, deadline):
    fd = _open_absolute_dir(destination, deadline)
    try:
        if _container_identity(os.fstat(fd)) != expected:
            raise fmt.ArchiveError("Archive destination changed before directory sync")
        _check(deadline)
        os.fsync(fd)
        _check(deadline)
        _check_destination(destination, expected, deadline)
    finally:
        os.close(fd)


@contextmanager
def _new_output(destination, name, expected, deadline):
    directory_fd = _open_absolute_dir(destination, deadline)
    try:
        if _container_identity(os.fstat(directory_fd)) != expected:
            raise fmt.ArchiveError("Archive destination changed during writing")
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                     | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=directory_fd)
        with os.fdopen(fd, "wb") as stream:
            yield stream
        _check_destination(destination, expected, deadline)
    finally:
        os.close(directory_fd)


def plan(roots, *, exclude_names=(".git",), exclude_paths=(), max_entries=fmt.MAX_ENTRIES,
         max_bytes=fmt.MAX_TOTAL_BYTES, max_seconds=3600):
    """Return a bounded metadata census without reading file bodies."""
    deadline = _deadline(max_seconds)
    roots, excluded_names, excluded_paths = _settings(roots, exclude_names, exclude_paths, max_entries, max_bytes, deadline)
    observed = _inventory(roots, excluded_names, excluded_paths, max_entries, max_bytes, deadline)
    entries = observed["entries"]
    counts = {plural: sum(item["kind"] == kind for item in entries)
              for kind, plural in (("file", "files"), ("directory", "directories"),
                                   ("symlink", "symlinks"))}
    counts["logical_file_bytes"] = sum(item.get("size", 0) for item in entries)
    return {"status": "PLANNED_NOT_ARCHIVED", "metadata_scope": fmt.METADATA_SCOPE,
            "roots": [{"label": label, "path": str(path)} for label, path in roots.items()],
            "entries": entries, "excluded": observed["excluded"], "counts": counts,
            "content_hashes_checked": False}


def pack(roots, destination, *, epoch, authority_ref, exclude_names=(".git",), exclude_paths=(),
         max_entries=fmt.MAX_ENTRIES, max_bytes=fmt.MAX_TOTAL_BYTES, max_seconds=7200):
    """Create an exclusive, fully verified archive; retain incomplete output on failure."""
    from .archive_restore import verify_payload

    deadline = _deadline(max_seconds)
    roots, excluded_names, excluded_paths = _settings(roots, exclude_names, exclude_paths, max_entries, max_bytes, deadline)
    destination = _destination(destination, roots)
    observed = _inventory(roots, excluded_names, excluded_paths, max_entries, max_bytes, deadline)
    manifest = {"schema": "lodestone-file-archive/v1", "epoch": epoch,
                "authority_ref": authority_ref,
                "roots": [{"label": label, "path": str(path)} for label, path in roots.items()],
                "entries": [dict(item) for item in observed["entries"]],
                "excluded": observed["excluded"], "metadata_scope": fmt.METADATA_SCOPE}
    blobs = {}
    for item in manifest["entries"]:
        if item["kind"] == "file":
            item["sha256"] = _hash_source(roots, item, observed["identities"], deadline)
            if item["sha256"] in blobs and blobs[item["sha256"]]["size"] != item["size"]:
                raise fmt.ArchiveError("Conflicting sizes for an identical source digest")
            blobs.setdefault(item["sha256"], item)
    fmt.validate_manifest(manifest)
    manifest_bytes = fmt.canonical(manifest)
    if len(manifest_bytes) > fmt.MAX_MANIFEST_BYTES:
        raise fmt.ArchiveError("Manifest exceeds byte bound")
    _check(deadline)
    destination_identity = _create_destination(destination, deadline)
    with _new_output(destination, "manifest.json", destination_identity, deadline) as output:
        output.write(manifest_bytes)
        output.flush()
        os.fsync(output.fileno())
    archive_path = destination / "archive.tar.gz"
    with _new_output(destination, "archive.tar.gz", destination_identity, deadline) as output:
        bounded = _BoundedOutput(output, deadline)
        with gzip.GzipFile(filename="", mode="wb", fileobj=bounded, mtime=0) as compressed:
            # GNU base-256 sizes accommodate the 64 GiB file ceiling. Names are
            # short and fixed, so no extension, long-name or link records occur.
            with tarfile.open(fileobj=compressed, mode="w|", format=tarfile.GNU_FORMAT) as archive:
                archive.addfile(_member("manifest.json", len(manifest_bytes)), io.BytesIO(manifest_bytes))
                for digest, item in sorted(blobs.items()):
                    _check(deadline)
                    with _source(roots, item, observed["identities"], deadline) as stream:
                        reader = _HashingReader(stream, deadline)
                        archive.addfile(_member("blobs/" + digest, item["size"]), reader)
                        if reader.count != item["size"] or reader.hasher.hexdigest() != digest or stream.read(1):
                            raise fmt.ArchiveError("Source bytes changed during archive: " + item["path"])
        output.flush()
        os.fsync(output.fileno())
    _check_destination(destination, destination_identity, deadline)
    archive_pin = fmt.hash_file(archive_path, max_bytes=fmt.MAX_ARCHIVE_BYTES, deadline=deadline)
    _check(deadline)
    payload_check = verify_payload(archive_path, manifest, expected_archive=archive_pin,
                                   max_seconds=deadline - time.monotonic())
    _check_destination(destination, destination_identity, deadline)
    # Rehash every source, including duplicate-content paths that needed no blob
    # write, then re-enumerate to detect additions, removals or changed metadata.
    for item in manifest["entries"]:
        if item["kind"] == "file" and _hash_source(roots, item, observed["identities"], deadline) != item["sha256"]:
            raise fmt.ArchiveError("Source bytes changed after archive: " + item["path"])
    after = _inventory(roots, excluded_names, excluded_paths, max_entries, max_bytes, deadline)
    if after != observed:
        raise fmt.ArchiveError("Source inventory changed during archive")
    counts = fmt.manifest_counts(manifest)
    descriptor = {"schema": "lodestone-file-archive-descriptor/v1",
                  "archive": {"path": "archive.tar.gz", **archive_pin},
                  "manifest": {"path": "manifest.json", "bytes": len(manifest_bytes),
                               "sha256": fmt.digest(manifest_bytes)},
                  "counts": counts, "metadata_scope": fmt.METADATA_SCOPE}
    descriptor_bytes = fmt.canonical(descriptor)
    if len(descriptor_bytes) > fmt.MAX_DESCRIPTOR_BYTES:
        raise fmt.ArchiveError("Descriptor exceeds byte bound")
    _check(deadline)
    # This completion sidecar is created only after every required check passes.
    with _new_output(destination, "archive.json", destination_identity, deadline) as output:
        output.write(descriptor_bytes)
        output.flush()
        os.fsync(output.fileno())
    # File fsyncs persist bytes; the directory fsync persists their names,
    # including the completion descriptor. Any failure propagates to the caller.
    _sync_destination(destination, destination_identity, deadline)
    return {"status": "ARCHIVED_AND_VERIFIED", "destination": str(destination),
            "descriptor": descriptor, "descriptor_sha256": fmt.digest(descriptor_bytes),
            "counts": counts, "excluded": manifest["excluded"],
            "metadata_scope": fmt.METADATA_SCOPE, "source_inventory_unchanged": True,
            "source_content_hashes_checked": True, "payload_verification": payload_check,
            "costs": {"archive_bytes": archive_pin["bytes"], "manifest_bytes": len(manifest_bytes),
                      "descriptor_bytes": len(descriptor_bytes),
                      "total_output_bytes": archive_pin["bytes"] + len(manifest_bytes) + len(descriptor_bytes)},
            "originals_retained": True}
