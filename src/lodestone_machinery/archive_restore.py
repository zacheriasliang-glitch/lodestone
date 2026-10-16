"""Bounded exact archive verification and exclusive, descriptor-relative restore.

The container has a deliberately small grammar: one canonical manifest followed
by sorted, ordinary GNU-format blob members, without extensions or links.
No tar extraction API is used.
"""
from contextlib import contextmanager, nullcontext
import hashlib
import math
import os
from pathlib import Path
import stat
import tarfile
import time
import zlib

from .archive_format import (
    ArchiveError, CHUNK_BYTES, MAX_ARCHIVE_BYTES, MAX_MANIFEST_BYTES,
    METADATA_SCOPE, canonical, digest, load_archive, manifest_counts,
    safe_path, validate_manifest,
)


def _deadline(seconds):
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        raise ArchiveError("max_seconds must be a positive finite number")
    if not math.isfinite(seconds) or not 0 < seconds <= 21600:
        raise ArchiveError("max_seconds must be a positive finite number no greater than 21600")
    return time.monotonic() + seconds


def _check(deadline):
    if time.monotonic() >= deadline:
        raise ArchiveError("Archive operation exceeded max_seconds")


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns)


def _absolute(path):
    path = Path(path).absolute()
    if any(part in (".", "..") for part in path.parts):
        raise ArchiveError("Paths must not contain dot or dotdot components")
    return path


def _directory_flags():
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise ArchiveError("Safe archive access requires O_NOFOLLOW and O_DIRECTORY")
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


@contextmanager
def _parent(path):
    """Open each absolute parent without resolving or following links."""
    path = _absolute(path)
    if path == Path(path.anchor):
        raise ArchiveError("A filesystem root is not an archive file/destination")
    fd = os.open(path.anchor, _directory_flags())
    try:
        for part in path.parts[1:-1]:
            next_fd = os.open(part, _directory_flags(), dir_fd=fd)
            os.close(fd)
            fd = next_fd
        yield fd, path.name
    finally:
        os.close(fd)


@contextmanager
def _regular(path):
    with _parent(path) as (parent, name):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise ArchiveError("Archive payload must be a regular file")
            if info.st_size > MAX_ARCHIVE_BYTES:
                raise ArchiveError("Archive payload exceeds MAX_ARCHIVE_BYTES")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                yield stream, info
            if _identity(info) != _identity(os.stat(name, dir_fd=parent, follow_symlinks=False)):
                raise ArchiveError("Archive payload path changed during verification")
        finally:
            os.close(fd)


class _GzipReader:
    """Single-member gzip decoder with bounded buffers and compressed hashing."""
    def __init__(self, source, deadline, limit):
        self.source, self.deadline, self.limit = source, deadline, limit
        self.decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        self.pending = b""
        self.buffer = bytearray()
        self.done = False
        self.compressed = 0
        self.expanded = 0
        self.hasher = hashlib.sha256()

    def _fill(self):
        while not self.buffer and not self.done:
            _check(self.deadline)
            block = self.pending
            self.pending = b""
            if not block:
                block = self.source.read(CHUNK_BYTES)
                self.compressed += len(block)
                if self.compressed > MAX_ARCHIVE_BYTES:
                    raise ArchiveError("Archive payload exceeds MAX_ARCHIVE_BYTES")
                self.hasher.update(block)
                if not block:
                    raise ArchiveError("Truncated gzip stream")
            try:
                output = self.decoder.decompress(block, CHUNK_BYTES)
            except zlib.error as exc:
                raise ArchiveError(f"Invalid gzip stream: {exc}") from exc
            self.pending = self.decoder.unconsumed_tail
            self.expanded += len(output)
            if self.expanded > self.limit:
                raise ArchiveError("Expanded tar exceeds the exact manifest bound")
            self.buffer.extend(output)
            if self.decoder.eof:
                if self.decoder.unused_data or self.pending or self.source.read(1):
                    raise ArchiveError("Trailing bytes or concatenated gzip members")
                self.done = True

    def read(self, amount):
        output = bytearray()
        while len(output) < amount:
            self._fill()
            if not self.buffer:
                break
            count = min(amount - len(output), len(self.buffer))
            output.extend(self.buffer[:count])
            del self.buffer[:count]
        return bytes(output)

    def exact(self, amount):
        value = self.read(amount)
        if len(value) != amount:
            raise ArchiveError("Truncated tar member or end marker")
        return value


def _header(name, size):
    member = tarfile.TarInfo(name)
    member.size = size
    member.mode = 0o600
    member.uid = member.gid = member.mtime = 0
    member.uname = member.gname = ""
    return member.tobuf(format=tarfile.GNU_FORMAT,
                        encoding="utf-8", errors="strict")


def _pin(value):
    if not isinstance(value, dict) or set(value) != {"bytes", "sha256"}:
        raise ArchiveError("Archive pin must contain exactly bytes and sha256")
    if type(value["bytes"]) is not int or not 0 <= value["bytes"] <= MAX_ARCHIVE_BYTES:
        raise ArchiveError("Invalid archive pin size")
    sha = value["sha256"]
    if not isinstance(sha, str) or len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
        raise ArchiveError("Invalid archive pin SHA-256")
    return value


def _payload(archive_path, manifest, expected_archive, deadline, sink=None):
    _check(deadline)
    validate_manifest(manifest)
    raw_manifest = canonical(manifest)
    if len(raw_manifest) > MAX_MANIFEST_BYTES:
        raise ArchiveError("Manifest exceeds MAX_MANIFEST_BYTES")
    expected_archive = _pin(expected_archive) if expected_archive is not None else None
    blobs = {entry["sha256"]: entry["size"] for entry in manifest["entries"]
             if entry["kind"] == "file"}
    members = [("manifest.json", len(raw_manifest))]
    members.extend((f"blobs/{sha}", blobs[sha]) for sha in sorted(blobs))
    # tarfile writes two zero blocks, then padding to its 10 KiB record size.
    tail_limit = 1024 + tarfile.RECORDSIZE
    expanded_limit = sum(512 + ((size + 511) // 512) * 512
                         for _, size in members) + tail_limit
    with _regular(archive_path) as (source, before):
        if expected_archive is not None and before.st_size != expected_archive["bytes"]:
            raise ArchiveError("Compressed archive size does not match its pin")
        reader = _GzipReader(source, deadline, expanded_limit)
        for name, size in members:
            _check(deadline)
            if reader.exact(512) != _header(name, size):
                raise ArchiveError(f"Unexpected tar member/header; expected {name}")
            sha = name.removeprefix("blobs/")
            context = sink.blob(sha, size) if sink is not None and name != "manifest.json" else nullcontext(None)
            with context as target:
                hasher = hashlib.sha256()
                position = 0
                while position < size:
                    _check(deadline)
                    block = reader.exact(min(CHUNK_BYTES, size - position))
                    if name == "manifest.json":
                        if block != raw_manifest[position:position + len(block)]:
                            raise ArchiveError("Embedded manifest is not the exact canonical manifest")
                    else:
                        hasher.update(block)
                        if target is not None:
                            target.write(block)
                    position += len(block)
                if name != "manifest.json" and hasher.hexdigest() != sha:
                    raise ArchiveError(f"Blob SHA-256 mismatch: {sha}")
            padding = (-size) % 512
            if padding and any(reader.exact(padding)):
                raise ArchiveError("Nonzero tar member padding")
        if any(reader.exact(1024)):
            raise ArchiveError("Missing tar end marker or extra tar members")
        trailing = 1024
        while True:
            _check(deadline)
            block = reader.read(min(CHUNK_BYTES, tail_limit + 1 - trailing))
            if not block:
                break
            trailing += len(block)
            if trailing > tail_limit or any(block):
                raise ArchiveError("Unexpected trailing tar data")
        if trailing % 512:
            raise ArchiveError("Tar end padding is not block aligned")
        if _identity(before) != _identity(os.fstat(source.fileno())):
            raise ArchiveError("Archive payload changed during verification")
        archive = {"bytes": reader.compressed, "sha256": reader.hasher.hexdigest()}
        if expected_archive is not None and archive != expected_archive:
            raise ArchiveError("Compressed archive SHA-256 does not match its pin")
    return {
        "status": "VERIFIED", "full_integrity": True,
        "coverage": "ALL_MEMBERS_AND_BLOBS", "members": len(members),
        "counts": manifest_counts(manifest), "archive": archive,
        "manifest": {"bytes": len(raw_manifest), "sha256": digest(raw_manifest)},
        "metadata_scope": METADATA_SCOPE, "excluded": manifest["excluded"],
    }


def verify_payload(archive_path, manifest, *, expected_archive=None, max_seconds=7200):
    """Verify a closed writer payload before its completion descriptor exists."""
    deadline = _deadline(max_seconds)
    try:
        return _payload(archive_path, manifest, expected_archive, deadline)
    except (OSError, ValueError, tarfile.TarError) as exc:
        raise ArchiveError(f"Archive payload verification failed: {exc}") from exc


def _loaded(directory, expected_descriptor_sha256, deadline):
    _check(deadline)
    descriptor, manifest = load_archive(
        directory, expected_descriptor_sha256=expected_descriptor_sha256)
    _check(deadline)
    return descriptor, manifest


def _decorate(result, descriptor):
    raw_descriptor = canonical(descriptor)
    result["descriptor"] = {"bytes": len(raw_descriptor), "sha256": digest(raw_descriptor)}
    result["cost"] = {
        "archive_bytes": result["archive"]["bytes"],
        "manifest_bytes": result["manifest"]["bytes"],
        "descriptor_bytes": len(raw_descriptor),
        "total_artifact_bytes": result["archive"]["bytes"] + result["manifest"]["bytes"] + len(raw_descriptor),
    }
    return result


def verify(directory, *, expected_descriptor_sha256=None, max_seconds=7200):
    """Authenticate published sidecars and verify the complete compressed payload."""
    deadline = _deadline(max_seconds)
    try:
        descriptor, manifest = _loaded(directory, expected_descriptor_sha256, deadline)
        pin = {key: descriptor["archive"][key] for key in ("bytes", "sha256")}
        result = _payload(Path(directory) / "archive.tar.gz", manifest, pin, deadline)
        return _decorate(result, descriptor)
    except (OSError, ValueError, tarfile.TarError) as exc:
        raise ArchiveError(f"Archive verification failed: {exc}") from exc


def _selection(manifest, paths):
    entries = {entry["path"]: entry for entry in manifest["entries"]}
    if paths is None:
        return list(entries.values()), None
    if not isinstance(paths, (list, tuple)) or not paths:
        raise ArchiveError("paths must be a nonempty list/tuple of exact archive paths")
    requested = {safe_path(path) for path in paths}
    if not requested <= entries.keys():
        raise ArchiveError("Selected path is absent from the archive manifest")
    selected = set()
    for path in entries:
        components = path.split("/")
        ancestors = ["/".join(components[:i]) for i in range(1, len(components) + 1)]
        if requested.intersection(ancestors):
            selected.update(ancestors)
    return [entries[path] for path in sorted(selected)], sorted(requested)


class _Destination:
    def __init__(self, fd, entries, deadline):
        self.fd, self.entries, self.deadline = fd, entries, deadline
        self.files = {}
        self.restored_files = 0
        for entry in entries:
            if entry["kind"] == "file":
                self.files.setdefault(entry["sha256"], []).append(entry)

    @contextmanager
    def parent(self, path):
        pieces = safe_path(path).split("/")
        fd = os.dup(self.fd)
        try:
            for piece in pieces[:-1]:
                _check(self.deadline)
                child = os.open(piece, _directory_flags(), dir_fd=fd)
                os.close(fd)
                fd = child
            yield fd, pieces[-1]
        finally:
            os.close(fd)

    def directories(self):
        for entry in sorted(self.entries, key=lambda e: (e["path"].count("/"), e["path"])):
            _check(self.deadline)
            if entry["kind"] == "directory":
                with self.parent(entry["path"]) as (parent, name):
                    os.mkdir(name, 0o700, dir_fd=parent)

    @contextmanager
    def new_file(self, entry):
        with self.parent(entry["path"]) as (parent, name):
            fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=parent)
            with os.fdopen(fd, "w+b") as stream:
                yield stream
                if _identity(os.fstat(stream.fileno())) != _identity(os.stat(name, dir_fd=parent, follow_symlinks=False)):
                    raise ArchiveError(f"Restored file path changed: {entry['path']}")

    def file_complete(self, stream, entry):
        stream.flush()
        os.fsync(stream.fileno())
        stream.seek(0)
        hasher, size = hashlib.sha256(), 0
        while True:
            _check(self.deadline)
            block = stream.read(CHUNK_BYTES)
            if not block:
                break
            size += len(block)
            if size > entry["size"]:
                raise ArchiveError("Restored file exceeds its declared size")
            hasher.update(block)
        if size != entry["size"] or hasher.hexdigest() != entry["sha256"]:
            raise ArchiveError(f"Restored file readback mismatch: {entry['path']}")
        os.fchmod(stream.fileno(), entry["mode"])
        os.utime(stream.fileno(), ns=(entry["mtime_ns"], entry["mtime_ns"]))
        self.check_metadata(os.fstat(stream.fileno()), entry)
        self.restored_files += 1

    @staticmethod
    def check_metadata(info, entry):
        if stat.S_IMODE(info.st_mode) != entry["mode"] or info.st_mtime_ns != entry["mtime_ns"]:
            raise ArchiveError(f"Filesystem cannot preserve requested metadata: {entry['path']}")

    @contextmanager
    def blob(self, sha, size):
        targets = self.files.get(sha, [])
        if not targets:
            yield None
            return
        with self.new_file(targets[0]) as first:
            yield first
            self.file_complete(first, targets[0])
            for entry in targets[1:]:
                _check(self.deadline)
                first.seek(0)
                with self.new_file(entry) as duplicate:
                    remaining = size
                    while remaining:
                        _check(self.deadline)
                        block = first.read(min(CHUNK_BYTES, remaining))
                        if not block:
                            raise ArchiveError("Restored deduplication source changed")
                        duplicate.write(block)
                        remaining -= len(block)
                    self.file_complete(duplicate, entry)

    def finish_metadata(self):
        # Links exist only after all regular payloads and the complete archive
        # have passed verification. No subsequent operation traverses a link.
        for entry in self.entries:
            _check(self.deadline)
            if entry["kind"] == "symlink":
                with self.parent(entry["path"]) as (parent, name):
                    os.symlink(entry["target"], name, dir_fd=parent)
                    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                    if stat.S_IMODE(info.st_mode) != entry["mode"]:
                        os.chmod(name, entry["mode"], dir_fd=parent, follow_symlinks=False)
                    os.utime(name, ns=(entry["mtime_ns"], entry["mtime_ns"]),
                             dir_fd=parent, follow_symlinks=False)
                    self.check_metadata(os.stat(name, dir_fd=parent, follow_symlinks=False), entry)
                    if os.readlink(name, dir_fd=parent) != entry["target"]:
                        raise ArchiveError("Restored symlink target changed")
        for entry in sorted(self.entries, key=lambda e: (-e["path"].count("/"), e["path"])):
            _check(self.deadline)
            if entry["kind"] == "directory":
                with self.parent(entry["path"]) as (parent, name):
                    fd = os.open(name, _directory_flags(), dir_fd=parent)
                try:
                    os.fchmod(fd, entry["mode"])
                    os.utime(fd, ns=(entry["mtime_ns"], entry["mtime_ns"]))
                    self.check_metadata(os.fstat(fd), entry)
                finally:
                    os.close(fd)


def restore(directory, destination, *, expected_descriptor_sha256=None,
            paths=None, max_seconds=7200):
    """Restore authenticated bytes into a new tree; failures retain partial output."""
    deadline = _deadline(max_seconds)
    created = False
    try:
        descriptor, manifest = _loaded(directory, expected_descriptor_sha256, deadline)
        entries, requested = _selection(manifest, paths)
        destination = _absolute(destination)
        with _parent(destination) as (parent, name):
            _check(deadline)
            os.mkdir(name, 0o700, dir_fd=parent)
            created = True
            fd = os.open(name, _directory_flags(), dir_fd=parent)
        try:
            sink = _Destination(fd, entries, deadline)
            sink.directories()
            pin = {key: descriptor["archive"][key] for key in ("bytes", "sha256")}
            result = _payload(Path(directory) / "archive.tar.gz", manifest, pin, deadline, sink)
            sink.finish_metadata()
            _check(deadline)
            if sink.restored_files != sum(entry["kind"] == "file" for entry in entries):
                raise ArchiveError("Restored file coverage mismatch")
            os.fsync(fd)
            _decorate(result, descriptor)
            result.update({
                "status": "RESTORED", "destination": str(destination),
                "selection": requested, "restored": {
                    "files": sink.restored_files,
                    "directories": sum(e["kind"] == "directory" for e in entries),
                    "symlinks": sum(e["kind"] == "symlink" for e in entries),
                    "logical_file_bytes": sum(e["size"] for e in entries if e["kind"] == "file"),
                },
            })
            return result
        finally:
            os.close(fd)
    except (OSError, ValueError, tarfile.TarError, NotImplementedError) as exc:
        detail = f"; partial output retained at {destination}" if created else ""
        raise ArchiveError(f"Restore incomplete{detail}: {exc}") from exc
