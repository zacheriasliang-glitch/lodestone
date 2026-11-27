"""Bounded values and file custody shared by the local control core."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import stat
import math
import time

from .portable_files import (durable_write, open_regular, read_bytes, sync_directory,
                             is_alias)


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


def create_file(path, body):
    """No replacement; publish only complete, durably flushed bytes."""
    durable_write(path, body)


def tree_inventory(root, *, max_bytes=67108864, max_files=8192):
    root = Path(root)
    if is_alias(root.lstat()) or not root.is_dir():
        raise ValueError("Tree root must be a regular directory")
    result = []
    total = 0
    entries = 0
    for directory, names, files in os.walk(root, followlinks=False):
        entries += len(names) + len(files)
        if entries > max_files:
            raise ValueError("Retained filesystem entries exceed resource limit")
        for name in names:
            if is_alias((Path(directory) / name).lstat()):
                raise ValueError("Symlink directory is not admitted")
        for name in sorted(files):
            path = Path(directory) / name
            metadata = path.lstat()
            if is_alias(metadata) or not stat.S_ISREG(metadata.st_mode):
                raise ValueError("Nonregular file is not admitted")
            total += metadata.st_size
            if total > max_bytes or len(result) >= max_files:
                raise ValueError("Retained output exceeds resource limit")
            sha = hashlib.sha256()
            with open_regular(path) as fd:
                if (metadata.st_dev, metadata.st_ino) != (os.fstat(fd).st_dev, os.fstat(fd).st_ino):
                    raise ValueError("Tree file identity changed during inventory")
                for block in iter(lambda: os.read(fd, 65536), b""):
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
            if is_alias((Path(directory) / name).lstat()):
                raise ValueError("Symlink directory is not admitted")
        for name in files:
            path = Path(directory) / name
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                if live:  # SQLite journals/atomic record temporaries can disappear during sampling.
                    continue
                raise
            if is_alias(metadata) or not stat.S_ISREG(metadata.st_mode):
                raise ValueError("Nonregular file is not admitted")
            total += metadata.st_size
    return total, entries


def tree_size(root):
    return tree_usage(root)[0]


@contextmanager
def file_lock(path, *, timeout=0):
    """Native OS exclusion, released on process exit; no PID ownership fiction."""
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout < 0:
        raise ValueError("Lock timeout must be finite and nonnegative")
    with open_regular(path, os.O_RDWR | os.O_CREAT, lock=True) as fd:
        if os.name == "nt":
            import msvcrt
            def acquire():
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            def release():
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            def acquire():
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            def release():
                fcntl.flock(fd, fcntl.LOCK_UN)
        deadline = time.monotonic() + timeout
        while True:
            try:
                acquire()
                break
            except OSError as exc:
                import errno
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                if time.monotonic() >= deadline:
                    raise BusyError("Local state is busy; another executor or snapshot owns the lock") from exc
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        try:
            # A waiter must not enter after its named lock was replaced.
            info, named = os.fstat(fd), Path(path).lstat()
            if is_alias(named) or (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino):
                raise ValueError("Lock file identity changed while acquiring custody")
            yield
        finally:
            release()
