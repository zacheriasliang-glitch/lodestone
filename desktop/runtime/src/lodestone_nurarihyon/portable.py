"""Explicit local Nurarihyon snapshots using the existing exact archive format.

No upload, service activation or recovered-code execution happens here. A caller
must retain the returned descriptor pin independently of the archive directory.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
import zlib

from lodestone_machinery import archive_format, archive_pack, archive_restore


# The controller admits up to64MiB/8192 state files. Include that complete
# state plus its bounded runtime and manifests, rather than truncating state.
MAX_BYTES = 128 * 1024 * 1024
MAX_ENTRIES = 16384
MAX_SECONDS = 180
CHUNK = 1024 * 1024
RUNTIME_DIRS = ("src/lodestone_machinery", "src/lodestone_pando", "src/lodestone_nurarihyon")
RUNTIME_FILES = ("tools/nurarihyon/run", "tools/nurarihyon/configure.py",
                 "tools/pando/run", "tools/machinery/run",
                 "tools/ecosystem/lodestone", "tools/ecosystem/cli.py",
                 "tools/ecosystem/conscience.py", "tools/ecosystem/conscience_targets.py",
                 "tools/ecosystem/conscience-policy.json",
                 "tools/local-memory/src/lodestone_memory.py")
TRANSIENT = frozenset(("state.lock", "executor.lock", "worker.lock"))
SQLITE_TRANSIENT = frozenset(("control.sqlite3-wal", "control.sqlite3-shm", "control.sqlite3-journal"))
CACHES = frozenset((".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".DS_Store"))
SECRET_NAMES = frozenset((".ssh", ".gnupg", ".aws", ".azure", ".credentials.json",
                          "credentials.json", "credentials", "auth.json", "authentication.json",
                          "token", "token.json", "oauth.json", "client_secret.json", ".netrc", ".npmrc", ".pypirc",
                          "id_rsa", "id_ed25519", "id_ecdsa", "keychain", "browser-profile"))
SECRET_KEYS = frozenset(("password", "api_key", "apikey", "access_token", "refresh_token",
                        "client_secret", "private_key", "authorization", "credential", "credentials"))


def _json(value):
    return json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False).encode() + b"\n"


def _deadline(end):
    if time.monotonic() >= end:
        raise ValueError("Portable snapshot exceeded its wall limit")


def _remaining(end):
    _deadline(end)
    return end - time.monotonic()


def _path(path, *, directory=False, new=False):
    """Reject path aliases before opening; archive readers impose the same rule."""
    path = Path(os.path.abspath(os.path.expanduser(os.fspath(path))))
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ValueError("Symlink path is not a portable snapshot input/destination")
    if new:
        if path.exists() or not path.parent.is_dir():
            raise ValueError("Destination must be new and its parent must exist")
    elif directory and not path.is_dir():
        raise ValueError("Expected an existing directory")
    elif not directory and not path.is_file():
        raise ValueError("Expected an existing regular file")
    return path


def _overlap(a, b):
    return a == b or a in b.parents or b in a.parents


def _excluded(relative):
    for part in relative.parts:
        lower = part.casefold()
        if part in CACHES or lower.endswith((".pyc", ".pyo")):
            return "cache or Git metadata"
        if lower in SECRET_NAMES or lower == ".env" or lower.startswith(".env."):
            return "authentication/secret path"
        if lower.startswith(("passwords.", "secrets.", "tokens.", "oauth.")):
            return "authentication/secret path"
    return None


def _identity(st):
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns, stat.S_IMODE(st.st_mode))


def _open_regular(path):
    """Open each parent through directory descriptors; never follow a link."""
    parent_fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:-1]:
            next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = next_fd
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise ValueError("Only regular files may be read")
        return fd
    finally:
        os.close(parent_fd)


def _read(path, limit):
    with os.fdopen(_open_regular(path), "rb") as source:
        data = source.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Portable metadata exceeds its read bound")
    return data


def _pin(path, end, *, copy_to=None):
    _deadline(end)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_BYTES:
        raise ValueError("Only bounded regular files are portable")
    digest, size = hashlib.sha256(), 0
    fd = _open_regular(path)
    with os.fdopen(fd, "rb") as source:
        if _identity(os.fstat(source.fileno())) != _identity(before):
            raise ValueError("Source changed before snapshot read")
        target = copy_to.open("xb") if copy_to is not None else None
        try:
            while chunk := source.read(CHUNK):
                _deadline(end)
                size += len(chunk)
                if size > MAX_BYTES:
                    raise ValueError("File exceeded portable byte bound")
                digest.update(chunk)
                if target is not None:
                    target.write(chunk)
        finally:
            if target is not None:
                target.close()
        if _identity(os.fstat(source.fileno())) != _identity(before):
            raise ValueError("Source changed during snapshot read")
    if _identity(path.lstat()) != _identity(before) or size != before.st_size:
        raise ValueError("Source changed during snapshot read")
    if copy_to is not None:
        os.chmod(copy_to, stat.S_IMODE(before.st_mode) & 0o777)
        os.utime(copy_to, ns=(before.st_atime_ns, before.st_mtime_ns))
    return {"bytes": size, "sha256": digest.hexdigest()}


def _scan(root, end, *, state=False):
    files, directories, exclusions = [], [], []
    pending = [root]
    total = 0
    while pending:
        current = pending.pop()
        _deadline(end)
        for path in sorted(current.iterdir()):
            relative = path.relative_to(root)
            info = path.lstat()
            # A secret-looking symlink is also refused, never followed or hidden.
            if stat.S_ISLNK(info.st_mode):
                raise ValueError("Symlinks are not permitted in portable state/code")
            reason = _excluded(relative)
            if state and relative.as_posix() in TRANSIENT:
                reason = "transient orchestration lock; recreated on destination"
            if state and relative.as_posix() in SQLITE_TRANSIENT:
                reason = "SQLite control snapshot includes committed contents; live companions excluded"
            elif state and path.name.endswith(("-wal", "-shm", "-journal")):
                raise ValueError("Generation SQLite companions require a quiescent checkpointed state")
            if reason:
                exclusions.append({"path": relative.as_posix(), "reason": reason})
                continue
            if stat.S_ISDIR(info.st_mode):
                directories.append(relative.as_posix())
                pending.append(path)
            elif stat.S_ISREG(info.st_mode):
                total += info.st_size
                files.append({"path": relative.as_posix(), "identity": _identity(info)})
            else:
                raise ValueError("Special filesystem entries cannot be exported")
            if len(files) + len(directories) + len(exclusions) > MAX_ENTRIES or total > MAX_BYTES:
                raise ValueError("Portable snapshot input exceeds count/byte limit")
    return {"files": sorted(files, key=lambda f: f["path"]),
            "directories": sorted(directories), "excluded": sorted(exclusions, key=lambda f: f["path"])}


def _config_secrets(path):
    """Check known configuration keys, not private user/source prose."""
    if path.name != "policy.json":
        return
    value = json.loads(_read(path, MAX_BYTES))
    def visit(item):
        if isinstance(item, dict):
            for key, child in item.items():
                if key.casefold().replace("-", "_") in SECRET_KEYS and child not in (None, "", False):
                    raise ValueError("Export policy contains a credential field; retain credentials externally")
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)
    visit(value)


def _snapshot_db(source, target, end):
    uri = source.as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=1)) as reader:
        page_size = reader.execute("PRAGMA page_size").fetchone()[0]
        def progress(_status, _remaining, pages):
            _deadline(end)
            if pages * page_size > MAX_BYTES:
                raise ValueError("Control database snapshot exceeds byte limit")
        with closing(sqlite3.connect(target)) as writer:
            reader.backup(writer, pages=64, progress=progress, sleep=0.01)
            # Only the new snapshot is normalized: it must not create/rely on
            # WAL companions when another computer inspects this closed copy.
            if writer.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                raise ValueError("Cannot make the staged database self-contained")
            if writer.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise ValueError("Control database snapshot failed integrity_check")
    if target.stat().st_size > MAX_BYTES:
        raise ValueError("Control database snapshot exceeds byte limit")
    os.chmod(target, stat.S_IMODE(source.stat().st_mode) & 0o777)


def _external_dependencies(policy_path):
    """Describe sealed execution dependencies without opening or copying them."""
    policy = json.loads(_read(policy_path, MAX_BYTES))
    result = [{"name": "Python and operating system", "included": False,
               "required": "Python >=3.10 with standard library on macOS or Linux"},
              {"name": "Separate outcome-memory catalogue", "included": False,
               "required": "Optional explicitly selected memory directory must be backed up separately"}]
    if isinstance(policy, dict) and "conscience" in policy:
        binding = policy["conscience"]
        result.append({"name": "Sealed Conscience binding", "included": False,
                       "pins": binding,
                       "required": "Exact trusted modules, policy and workspace at their bound paths for execution; restoration does not rebind paths or transfer approval"})
    return result


def export_package(controller, destination: Path, source_root: Path) -> dict:
    """Capture dedicated, locked state and runnable code to a new local archive.

    Failure retains any new archive/staging bytes for inspection. Original state
    and source files are never rewritten. Caller must retain the external pin.
    """
    end = time.monotonic() + MAX_SECONDS
    destination = _path(destination, new=True)
    source_root = _path(source_root, directory=True)
    home = _path(controller.home, directory=True)
    database = _path(controller.database)
    if database != home / "control.sqlite3":
        raise ValueError("Controller database must be its dedicated control.sqlite3")
    if _overlap(home, source_root) or _overlap(destination, home) or _overlap(destination, source_root):
        raise ValueError("Code, state and export destination must not overlap")
    pin_path = _path(destination.with_name(destination.name + ".descriptor.sha256"), new=True)
    for name in RUNTIME_DIRS:
        _path(source_root / name, directory=True)
        _path(source_root / name / "__init__.py")
    for name in RUNTIME_FILES:
        _path(source_root / name)
    _path(home / "policy.json")
    stage = Path(tempfile.mkdtemp(prefix="." + destination.name + ".capture-", dir=destination.parent))
    runtime, state, metadata = (stage / part for part in ("runtime", "state", "metadata"))
    for part in (runtime, state, metadata):
        part.mkdir(mode=0o700)
    source_pins, state_pins, excluded, source_censuses = {}, {}, [], {}
    selected_bytes = 0
    def reserve(path):
        nonlocal selected_bytes
        selected_bytes += path.stat().st_size
        if selected_bytes > MAX_BYTES:
            raise ValueError("Combined code/state exceeds portable byte limit")
    try:
        with controller.locked():
            controller.verify()
            initial_state = _scan(home, end, state=True)
            for directory in initial_state["directories"]:
                (state / directory).mkdir(parents=True, exist_ok=True)
            for item in initial_state["files"]:
                name = item["path"]
                path = home / name
                reserve(path)
                _config_secrets(path)
                if name == "control.sqlite3":
                    state_pins[name] = _pin(path, end)
                    _snapshot_db(path, state / name, end)
                else:
                    state_pins[name] = _pin(path, end, copy_to=state / name)
            excluded.extend({"root": "state", **item} for item in initial_state["excluded"])
            for directory in RUNTIME_DIRS:
                census = _scan(source_root / directory, end)
                source_censuses[directory] = census
                (runtime / directory).mkdir(parents=True)
                for name in census["directories"]:
                    (runtime / directory / name).mkdir(parents=True)
                for item in census["files"]:
                    name = directory + "/" + item["path"]
                    reserve(source_root / name)
                    source_pins[name] = _pin(source_root / name, end, copy_to=runtime / name)
                excluded.extend({"root": "runtime", "path": directory + "/" + item["path"],
                                 "reason": item["reason"]} for item in census["excluded"])
            for name in RUNTIME_FILES:
                reserve(source_root / name)
                (runtime / name).parent.mkdir(parents=True, exist_ok=True)
                source_pins[name] = _pin(source_root / name, end, copy_to=runtime / name)
            for name, expected in source_pins.items():
                if _pin(source_root / name, end) != expected:
                    raise ValueError("Runtime source changed during capture")
            for directory, before in source_censuses.items():
                if _scan(source_root / directory, end) != before:
                    raise ValueError("Runtime source inventory changed during capture")
            for name, expected in state_pins.items():
                if _pin(home / name, end) != expected:
                    raise ValueError("Nurarihyon state changed during capture")
            if _scan(home, end, state=True) != initial_state:
                raise ValueError("Nurarihyon state inventory changed during capture")
            controller.verify()
        # The immutable staging tree is now detached from the live controller.
        files = {}
        for root, label in ((runtime, "runtime"), (state, "state")):
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    files[label + "/" + path.relative_to(root).as_posix()] = _pin(path, end)
        manifest = {"schema": "nurarihyon-portable/v1", "captured_utc": datetime.now(timezone.utc).isoformat(),
                    "scope": "Explicit dedicated Nurarihyon state and runnable source; no host crawl or full machine clone",
                    "dependencies": {"python": ">=3.10", "stdlib_only": True, "models_required": False,
                                     "captured_python": sys.version, "sqlite": sqlite3.sqlite_version,
                                     "zlib": zlib.ZLIB_RUNTIME_VERSION},
                    "relationships": {"runtime": "runtime", "home": "state", "database": "state/control.sqlite3",
                                      "command": "python3 -B runtime/tools/nurarihyon/run --home state status"},
                    "files": files, "exclusions": excluded,
                    "source_file_pins": source_pins, "original_state_file_pins": state_pins,
                    "external_dependencies": _external_dependencies(state / "policy.json"),
                    "database_capture": "SQLite consistent backup under exclusive controller state lock; staged copy journal_mode=DELETE, source journal mode unchanged",
                    "limits": {"combined_logical_bytes": MAX_BYTES, "entries": MAX_ENTRIES,
                               "wall_seconds": MAX_SECONDS,
                               "reason": "Includes complete admitted64MiB/8192-file state plus bounded code and metadata; no selective truncation"},
                    "privacy_limit": "Known secret paths and policy credential keys excluded/refused; explicitly authorized user content may itself contain private text. This is a private package, not a general secret scanner."}
        (metadata / "package.json").write_bytes(_json(manifest))
        (metadata / "RESTORE.md").write_text(
            "# Nurarihyon selected-state recovery\n\n"
            "Verify using the independently retained descriptor SHA-256 before restoration. "
            "The archive contains runtime/, state/ and metadata/. No recovered service is started.\n\n"
            "From this restored directory: `python3 -B runtime/tools/nurarihyon/run --home state status`. "
            "Inspect policy and durable STOP state before any explicit execution. "
            "The executor/state/worker locks are intentionally absent and may be recreated by the controller.\n\n"
            "Python 3.10+ and its standard library are required; no learned models are required. "
            "The current process-control/OS-lock implementation may require a POSIX host; "
            "a Windows service migration is not certified. Mac login services, credentials, "
            "browser state and external research/backup artifacts are not restored. "
            "Referenced external artifacts remain separate dependencies, not included backups. "
            "In particular a sealed Conscience binding retains its original external paths and pins; "
            "the manifest lists them without reading those files. Moving or replacing them requires "
            "explicit owner configuration and new task approvals; restored history is not rewritten. "
            "A separate outcome-memory catalogue is not included.\n",
            encoding="utf-8")
        roots = {"runtime": runtime, "state": state, "metadata": metadata}
        plan = archive_pack.plan(roots, max_entries=MAX_ENTRIES, max_bytes=MAX_BYTES,
                                 max_seconds=_remaining(end))
        result = archive_pack.pack(roots, destination, epoch=manifest["captured_utc"],
                                   authority_ref="Explicit local Nurarihyon export; preserve original research and backup state",
                                   max_entries=MAX_ENTRIES, max_bytes=MAX_BYTES,
                                   max_seconds=_remaining(end))
        verified = archive_restore.verify(destination, expected_descriptor_sha256=result["descriptor_sha256"],
                                          max_seconds=_remaining(end))
        with pin_path.open("x", encoding="ascii") as out:
            out.write(result["descriptor_sha256"] + "\n")
            out.flush()
            os.fsync(out.fileno())
        shutil.rmtree(stage)
        return {"status": "EXPORTED_VERIFIED", "package": str(destination), "descriptor": str(pin_path),
                "descriptor_sha256": result["descriptor_sha256"], "costs": result["costs"],
                "plan_counts": plan["counts"], "verification": verified,
                "external_pin_bytes": pin_path.stat().st_size, "exclusions": excluded,
                "source_hashes_unchanged": True, "state_hashes_unchanged": True,
                "service_started": False, "uploaded": False}
    except Exception as exc:
        raise ValueError(f"Portable export failed; preserve staging {stage}: {exc}") from exc


def restore_package(package: Path, descriptor: Path, destination: Path) -> dict:
    """Verify an externally pinned package and restore into a new inert tree."""
    end = time.monotonic() + MAX_SECONDS
    package = _path(package, directory=True)
    descriptor = _path(descriptor)
    destination = _path(destination, new=True)
    if _overlap(package, destination) or package in descriptor.parents or _overlap(destination, descriptor):
        raise ValueError("Descriptor pin must be external; restore must not overlap package/pin")
    if descriptor.stat().st_size > 128:
        raise ValueError("External descriptor pin exceeds its bound")
    expected = _read(descriptor, 128).decode("ascii").strip()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("External descriptor pin must contain one lowercase SHA-256")
    descriptor_value, contents = archive_format.load_archive(package, expected_descriptor_sha256=expected)
    if descriptor_value["archive"]["bytes"] > MAX_BYTES * 2:
        raise ValueError("Compressed package exceeds Nurarihyon restore bound")
    if len(contents["entries"]) > MAX_ENTRIES or sum(e.get("size", 0) for e in contents["entries"]) > MAX_BYTES:
        raise ValueError("Package exceeds Nurarihyon restore bounds")
    if {r["label"] for r in contents["roots"]} != {"runtime", "state", "metadata"}:
        raise ValueError("Unexpected portable package roots")
    for item in contents["entries"]:
        if item["kind"] == "symlink" or _excluded(Path(item["path"])):
            raise ValueError("Package contains a symlink or excluded path")
        if item["path"] in {"state/" + name for name in TRANSIENT | SQLITE_TRANSIENT}:
            raise ValueError("Package contains transient locks or SQLite companions")
    archive_restore.verify(package, expected_descriptor_sha256=expected,
                           max_seconds=_remaining(end))
    result = archive_restore.restore(package, destination, expected_descriptor_sha256=expected,
                                     max_seconds=_remaining(end))
    manifest_path = destination / "metadata/package.json"
    manifest = json.loads(_read(manifest_path, MAX_BYTES))
    if manifest.get("schema") != "nurarihyon-portable/v1" or not isinstance(manifest.get("files"), dict):
        raise ValueError("Restored package manifest has an unsupported schema")
    actual = {}
    for root in ("runtime", "state"):
        census = _scan(destination / root, end)
        if census["excluded"]:
            raise ValueError("Restored package contains excluded paths")
        for item in census["files"]:
            name = root + "/" + item["path"]
            actual[name] = _pin(destination / name, end)
    if actual != manifest["files"]:
        raise ValueError("Restored code/state differs from portable file manifest")
    required = {"state/control.sqlite3", "state/policy.json"}
    required.update("runtime/" + name for name in RUNTIME_FILES)
    required.update("runtime/" + name + "/__init__.py" for name in RUNTIME_DIRS)
    if not required <= actual.keys():
        raise ValueError("Restored package omits required runnable code/state dependencies")
    if any("state/" + name in actual for name in TRANSIENT):
        raise ValueError("Restored package must not activate or retain transient process locks")
    _config_secrets(destination / "state/policy.json")
    database = destination / "state/control.sqlite3"
    # This is a completed, externally pinned, isolated closed snapshot, unlike
    # the live source database used by the backup API above.
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=1)) as connection:
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("Restored control database failed integrity_check")
    return {"status": "RESTORED_VERIFIED_INERT", "destination": str(destination),
            "descriptor_sha256": expected, "file_count": len(actual),
            "restored_file_bytes": sum(i["bytes"] for i in actual.values()), "archive_restore": result,
            "home": str(destination / "state"), "runtime": str(destination / "runtime"),
            "command": [sys.executable, "-B", str(destination / "runtime/tools/nurarihyon/run"),
                        "--home", str(destination / "state"), "status"],
            "service_started": False, "recovered_code_executed": False}
