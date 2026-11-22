"""Read-only pins for critical local interpreter and launcher dependencies.

This is a bounded deployment observation, not a complete OS/stdlib inventory,
vendor signature check or protection against code running as the host account.
"""
import hashlib
import json
import os
from pathlib import Path
import stat
import sys


ROOT = Path(__file__).resolve().parents[2]
MAX_FILE_BYTES = 32 * 1024 * 1024
LAUNCHER = "tools/nurarihyon/run"


def _pin(path, *, resolve_alias=False):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError("Dependency path must be absolute")
    if resolve_alias:
        path = path.resolve(strict=True)
    elif path.is_symlink():
        raise ValueError("Dependency symlink refused")
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
        raise ValueError("Dependency must be a bounded regular file")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE_BYTES:
            raise ValueError("Dependency changed type or exceeds byte bound")
        digest, size = hashlib.sha256(), 0
        while True:
            block = stream.read(min(65536, MAX_FILE_BYTES + 1 - size))
            if not block:
                break
            size += len(block)
            if size > MAX_FILE_BYTES:
                raise ValueError("Dependency exceeds byte bound")
            digest.update(block)
        after = os.fstat(stream.fileno())
    def identity(value):
        return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns
    if (identity(info) != identity(before) or identity(before) != identity(after)
            or identity(after) != identity(path.stat()) or size != after.st_size):
        raise ValueError("Dependency changed during hashing")
    return {"bytes": size, "sha256": digest.hexdigest()}, path


def environment_record():
    """Hash the existing runtime dependencies; never enumerate environment secrets.

    Python command aliases are resolved and recorded explicitly. The launcher
    has a relative key so moving identical project files does not change this
    pin. A changed interpreter installation requires a new explicit approval.
    """
    executable = Path(sys.executable)
    if not sys.executable or not executable.is_absolute():
        raise ValueError("Existing absolute Python executable required")
    python_pin, resolved = _pin(executable, resolve_alias=True)
    prefix = Path(sys.prefix).resolve(strict=True)
    framework = {}
    if sys.platform == "darwin":
        for relative in ("Python", "Resources/Python.app/Contents/MacOS/Python"):
            path = prefix / relative
            if path.exists() or path.is_symlink():
                item, target = _pin(path, resolve_alias=True)
                framework[relative] = {**item, "resolved_path": str(target)}
    root = ROOT.resolve(strict=True)
    launcher = root / LAUNCHER
    for path in (launcher, launcher.parent, launcher.parent.parent):
        if path.is_symlink():
            raise ValueError("Launcher path symlink refused")
    launcher_pin, _ = _pin(launcher)
    return {
        "schema": "nurarihyon-critical-environment/v1",
        "scope": "Python version, executable, existing Mac framework binaries and launcher only; excludes other stdlib, shared libraries, OS and credentials",
        "python": {"version": sys.version, "command_path": str(executable),
                   "resolved_path": str(resolved), "prefix": str(prefix),
                   **python_pin, "framework": framework},
        "launcher": {LAUNCHER: launcher_pin},
    }


def environment_identity():
    """Re-observe dependencies and return SHA-256 of their canonical record."""
    raw = json.dumps(environment_record(), sort_keys=True, ensure_ascii=False,
                     separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()
