"""Bounded values and file custody shared by the local control core."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import stat
import math
import time


class BusyError(RuntimeError):
    """Normal OS lock contention; no task or executor failure."""


class StateBlocked(ValueError):
    """A controller-wide dependency needs attention, not a source lesson."""


class ApprovalStale(StateBlocked):
    """This plan's executable identity needs a new explicit approval."""


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(value).hexdigest()


def strict_json(raw, limit=262144):
    if type(raw) is not bytes or len(raw) > limit:
        raise ValueError("JSON input exceeds byte bound")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result
    def nonfinite(value):
        raise ValueError("Nonfinite JSON value")
    def number(value):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("Nonfinite JSON number")
        return result
    return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                      parse_constant=nonfinite, parse_float=number)


def read_bytes(path, limit=262144):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("Expected a regular, non-symlink file")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("File exceeds byte bound")
    return data


def sync_directory(path):
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def create_file(path, body):
    """No replacement; complete and flush before publishing a reference."""
    path = Path(path)
    with path.open("xb") as stream:
        stream.write(body)
        stream.flush()
        os.fsync(stream.fileno())
    sync_directory(path.parent)


def tree_inventory(root, *, max_bytes=67108864, max_files=8192):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Tree root must be a regular directory")
    result = []
    total = 0
    entries = 0
    for directory, names, files in os.walk(root, followlinks=False):
        entries += len(names) + len(files)
        if entries > max_files:
            raise ValueError("Retained filesystem entries exceed resource limit")
        for name in names:
            if (Path(directory) / name).is_symlink():
                raise ValueError("Symlink directory is not admitted")
        for name in sorted(files):
            path = Path(directory) / name
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("Nonregular file is not admitted")
            total += metadata.st_size
            if total > max_bytes or len(result) >= max_files:
                raise ValueError("Retained output exceeds resource limit")
            sha = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(65536), b""):
                    sha.update(block)
            result.append({"path": path.relative_to(root).as_posix(),
                           "bytes": metadata.st_size, "sha256": sha.hexdigest()})
    return sorted(result, key=lambda item: item["path"])


def tree_usage(root, *, live=False):
    total = 0
    entries = 0
    for directory, names, files in os.walk(root, followlinks=False):
        entries += len(names) + len(files)
        for name in names:
            if (Path(directory) / name).is_symlink():
                raise ValueError("Symlink directory is not admitted")
        for name in files:
            path = Path(directory) / name
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                if live:  # SQLite journals/atomic record temporaries can disappear during sampling.
                    continue
                raise
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("Nonregular file is not admitted")
            total += metadata.st_size
    return total, entries


def tree_size(root):
    return tree_usage(root)[0]


@contextmanager
def file_lock(path, *, timeout=0):
    """An OS-held lock, released by process exit; PID files confer no ownership."""
    import fcntl
    path = Path(path)
    if path.is_symlink():
        raise ValueError("Lock symlink refused")
    with path.open("a+b") as stream:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise BusyError("Local state is busy; another executor or snapshot owns the lock") from exc
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)
