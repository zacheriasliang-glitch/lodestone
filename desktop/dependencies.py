"""Pinned, resumable local dependencies for each supported desktop platform.

This module uses the standard library and system curl. It installs only into
the supplied root, never invokes a model, and never consults an existing Loom
installation. Downloaded archives and wheels are checked before execution.
"""
from __future__ import annotations

from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import posixpath
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import zipfile
import stat

try:
    from .platform_support import (atomic_write, file_lock, venv_python, check_path,
                                   guarded_directory, open_regular, read_file, durable_move)
except ImportError:
    from platform_support import (atomic_write, file_lock, venv_python, check_path,
                                  guarded_directory, open_regular, read_file, durable_move)
from urllib.parse import urlsplit


SCHEMA = "lodestone-desktop-dependencies/v1"
LOCK_PATH = Path(__file__).with_name("dependencies.lock.json")


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with open_regular(path) as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json(path: Path, value) -> None:
    atomic_write(path, (json.dumps(value, indent=2, sort_keys=True) + "\n").encode())


def _artifact(value: dict) -> None:
    if (not isinstance(value, dict)
            or not re.fullmatch(r"[0-9a-f]{64}", value.get("sha256", ""))
            or type(value.get("size")) is not int or not 0 < value["size"] < 2**31
            or type(value.get("filename")) is not str
            or Path(value["filename"]).name != value["filename"]
            or value["filename"] in (".", "..")):
        raise ValueError("Invalid pinned dependency artifact")
    url = urlsplit(value.get("url", ""))
    if url.scheme != "https" or not url.hostname or url.username or url.password:
        raise ValueError("Dependencies require public HTTPS artifact URLs")


def _verified(path: Path, artifact: dict) -> bool:
    path = check_path(path)
    return (not path.is_symlink() and path.is_file()
            and path.stat().st_size == artifact["size"] and _sha(path) == artifact["sha256"])


def fetch(artifact: dict, cache: Path, *, offline: bool = False) -> Path:
    """Return a verified cache object; retain interrupted transfers for retry."""
    _artifact(artifact)
    cache = check_path(cache)
    with guarded_directory(cache, create=True):
        return _fetch(artifact, cache, offline=offline)


def _fetch(artifact: dict, cache: Path, *, offline: bool) -> Path:
    target = cache / artifact["sha256"]
    if _verified(target, artifact):
        return target
    if target.exists() or target.is_symlink():
        raise ValueError("Cached dependency failed its pinned hash: " + str(target))
    if offline:
        raise FileNotFoundError("Offline cache is missing verified artifact: " + artifact["filename"])
    partial = target.with_name(target.name + ".partial")
    if partial.is_symlink():
        raise ValueError("Download partial must not be a symlink")
    curl = shutil.which("curl.exe" if os.name == "nt" else "curl")
    if not curl:
        raise RuntimeError("HTTPS downloads require the operating system curl executable")
    check_path(cache)
    check_path(partial)
    command = [curl, "--fail", "--location", "--silent", "--show-error",
               "--proto", "=https", "--proto-redir", "=https", "--tlsv1.2",
               "--retry", "2", "--connect-timeout", "30", "--max-time", "1800",
               "--output", str(partial)]
    if partial.exists() and partial.stat().st_size:
        command.extend(["--continue-at", "-"])
    subprocess.run(command + [artifact["url"]], check=True, timeout=1830)
    if not _verified(partial, artifact):
        raise ValueError("Downloaded dependency failed its pinned hash: " + artifact["filename"])
    # Never replace a different process's completed immutable cache object.
    try:
        durable_move(partial, target)
    except FileExistsError:
        if not _verified(target, artifact):
            raise ValueError("Concurrent cache publication differs from pinned artifact")
    return target


def _relative(name: str) -> str:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name:
        raise ValueError("Archive path escapes its destination")
    return str(path)


def _archive_inventory(archive: Path, *, maximum=1024**3) -> dict:
    """Validate every member before extraction and describe exact file bytes."""
    with (nullcontext(archive) if hasattr(archive, "read") else open_regular(archive)) as source:
        source.seek(0)
        return _archive_stream_inventory(source, maximum=maximum)


def _archive_stream_inventory(archive, *, maximum):
    result, names, total = {}, set(), 0
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as stream:
            folded = set()
            for member in stream.infolist():
                name = _relative(member.filename)
                if name.casefold() in folded or name in names:
                    raise ValueError("Duplicate or case-aliased archive member")
                names.add(name)
                folded.add(name.casefold())
                if member.is_dir():
                    continue
                if stat.S_ISLNK(member.external_attr >> 16):
                    raise ValueError("ZIP symlinks are not supported")
                total += member.file_size
                if total > maximum:
                    raise ValueError("Dependency archive exceeds extraction bound")
                digest = hashlib.sha256()
                with stream.open(member) as content:
                    for block in iter(lambda: content.read(1024 * 1024), b""):
                        digest.update(block)
                result[name] = {"sha256": digest.hexdigest(), "size": member.file_size}
        return result
    archive.seek(0)
    with tarfile.open(fileobj=archive, mode="r:gz") as stream:
        for member in stream.getmembers():
            name = _relative(member.name)
            if name in names:
                raise ValueError("Duplicate archive member")
            names.add(name)
            if member.isdir():
                continue
            if member.issym():
                target = posixpath.normpath(posixpath.join(posixpath.dirname(name), member.linkname))
                _relative(target)
                if member.linkname.startswith("/"):
                    raise ValueError("Absolute archive symlink")
                result[name] = {"link": member.linkname}
            elif member.isfile():
                total += member.size
                if total > maximum:
                    raise ValueError("Dependency archive exceeds extraction bound")
                digest = hashlib.sha256()
                with stream.extractfile(member) as content:
                    for block in iter(lambda: content.read(1024 * 1024), b""):
                        digest.update(block)
                result[name] = {"sha256": digest.hexdigest(), "size": member.size}
            else:
                raise ValueError("Unsupported dependency archive member")
    return result


def _inventory(root: Path) -> dict:
    root = check_path(root)
    result, pending = {}, [root]
    while pending:
        directory = pending.pop()
        if not directory.exists():
            continue
        with guarded_directory(directory):
            for path in sorted(directory.iterdir()):
                name = path.relative_to(root).as_posix()
                info = path.lstat()
                if getattr(info, "st_file_attributes", 0) & 0x400:
                    raise ValueError("Dependency tree contains a Windows reparse point")
                if stat.S_ISLNK(info.st_mode):
                    # Pinned POSIX Python archives contain legitimate links.
                    # Record their literal text without traversing them.
                    result[name] = {"link": os.readlink(path)}
                elif stat.S_ISDIR(info.st_mode):
                    pending.append(path)
                elif stat.S_ISREG(info.st_mode):
                    result[name] = {"sha256": _sha(path), "size": info.st_size}
                else:
                    raise ValueError("Dependency tree contains a nonregular entry")
    return result


def unpack(artifact: dict, cache: Path, destination: Path, *, offline=False) -> Path:
    destination = check_path(destination)
    archive = fetch(artifact, cache, offline=offline)
    maximum = artifact.get("unpacked_limit_bytes", 1024**3)
    if type(maximum) is not int or not 0 < maximum <= 8 * 1024**3:
        raise ValueError("Invalid extraction bound")
    with open_regular(archive) as source, guarded_directory(destination.parent, create=True):
        expected = _archive_inventory(source, maximum=maximum)
        if destination.exists():
            if _inventory(destination) != expected:
                raise ValueError("Existing dependency tree differs from its verified archive: " + str(destination))
        else:
            stage = Path(tempfile.mkdtemp(prefix=destination.name + ".partial-", dir=destination.parent))
            source.seek(0)
            if zipfile.is_zipfile(source):
                with zipfile.ZipFile(source) as stream:
                    stream.extractall(stage)
            else:
                source.seek(0)
                with tarfile.open(fileobj=source, mode="r:gz") as stream:
                    stream.extractall(stage, filter="data")
            if _inventory(stage) != expected:
                raise ValueError("Extracted dependency differs from verified archive")
            durable_move(stage, destination)
        executable = destination / _relative(artifact["executable"])
        if (not executable.resolve().is_relative_to(destination)
                or not executable.is_file() or not os.access(executable, os.X_OK)):
            raise ValueError("Verified archive lacks its declared executable")
        return executable


def _environment(root: Path) -> dict:
    env = dict(os.environ)
    env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1",
               UV_CACHE_DIR=str(root / "uv-cache"), UV_PYTHON_DOWNLOADS="never",
               UV_NO_CONFIG="1", UV_NATIVE_TLS="true", PIP_CONFIG_FILE=os.devnull,
               HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
               USE_TORCH="0", USE_TF="0", USE_FLAX="0")
    env.pop("PYTHONPATH", None)
    env.pop("VIRTUAL_ENV", None)
    return env


def _run(command: list[str], root: Path) -> str:
    result = subprocess.run(command, env=_environment(root), capture_output=True,
                            text=True, timeout=900, cwd=root)
    if result.returncode:
        raise RuntimeError("Dependency command failed: " + repr(command)
                           + "\n" + result.stderr[-4000:])
    return result.stdout


def _install_environment(name: str, spec: dict, python: Path, uv: Path,
                         root: Path, cache: Path, *, offline: bool) -> Path:
    env_path = check_path(root / name)
    receipt_path = check_path(root / (name + ".installed.json"))
    fingerprint = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
    wheels = []
    for wheel in spec["wheels"]:
        directory = check_path(root / "wheels" / wheel["sha256"])
        local = directory / wheel["filename"]
        _copy_asset(wheel, local, cache, offline=offline)
        wheels.append((local, wheel))
    if receipt_path.is_file():
        receipt = json.loads(read_file(receipt_path))
        if receipt["spec_sha256"] != fingerprint or receipt["files"] != _inventory(env_path):
            raise ValueError("Installed dependency environment changed: " + name)
    else:
        if env_path.exists():
            # A failed attempt is retained, and the next attempt uses the same
            # final path so generated script shebangs never point into staging.
            env_path.rename(env_path.with_name(name + ".interrupted-" + str(time.time_ns())))
        _run([str(uv), "venv", "--python", str(python), str(env_path)], root)
        requirements = root / (name + ".requirements.txt")
        atomic_write(requirements, ("\n".join(local.as_uri() + " --hash=sha256:" + wheel["sha256"]
                                            for local, wheel in wheels) + "\n").encode())
        _run([str(uv), "pip", "install", "--python", str(venv_python(env_path)),
              "--offline", "--no-index", "--no-deps", "--require-hashes",
              "--only-binary", ":all:", "--requirements", str(requirements)], root)
        _run([str(uv), "pip", "check", "--python", str(venv_python(env_path))], root)
        _json(receipt_path, {"spec_sha256": fingerprint, "files": _inventory(env_path)})
    actual = json.loads(_run([str(venv_python(env_path)), "-I", "-B", "-c",
        "import importlib.metadata as m,json,re; print(json.dumps({re.sub(r'[-_.]+','-',d.metadata['Name']).lower():d.version for d in m.distributions()}))"], root))
    expected = {wheel["name"]: wheel["version"] for wheel in spec["wheels"]}
    if actual != expected:
        raise ValueError("Installed package versions differ from the complete dependency lock")
    return venv_python(env_path)


def _copy_asset(artifact: dict, destination: Path, cache: Path, *, offline: bool) -> None:
    destination = check_path(destination)
    source = fetch(artifact, cache, offline=offline)
    with guarded_directory(destination.parent, create=True):
        if destination.exists():
            if not _verified(destination, artifact):
                raise ValueError("Installed asset differs from pinned identity: " + str(destination))
            return
        # An exclusive random temporary cannot truncate a preexisting alias.
        fd, name = tempfile.mkstemp(prefix=destination.name + ".partial-", dir=destination.parent)
        temporary = Path(name)
        with os.fdopen(fd, "wb") as output, open_regular(source) as content:
            shutil.copyfileobj(content, output, 1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        if not _verified(temporary, artifact):
            raise ValueError("Copied asset differs from pinned identity")
        durable_move(temporary, destination)


def ensure(root: Path, payload: Path, cache: Path | None = None, *, offline: bool = False) -> dict:
    """Build/recheck isolated environments and assets; never run inference."""
    root, payload = check_path(root), check_path(payload)
    lock_bytes = read_file(payload / "dependencies.lock.json")
    lock_sha256 = hashlib.sha256(lock_bytes).hexdigest()
    lock = json.loads(lock_bytes)
    if lock.get("schema") != SCHEMA:
        raise ValueError("Unsupported dependency lock")
    target = lock["platform"]
    machine = {"AMD64": "x86_64", "amd64": "x86_64", "aarch64": "arm64"}.get(platform.machine(), platform.machine())
    if platform.system() != target["system"] or machine != target["machine"]:
        raise ValueError("This download requires " + target["system"] + " " + target["machine"])
    if target["system"] == "Darwin":
        actual_os = tuple(int(part) for part in platform.mac_ver()[0].split(".")[:2])
        if actual_os < tuple(int(part) for part in target["minimum_macos"].split(".")):
            raise ValueError("This download requires macOS " + target["minimum_macos"] + " or later")
    elif target["system"] == "Linux":
        release = platform.freedesktop_os_release()
        if release.get("ID") != target["distribution"] or release.get("VERSION_ID") != target["distribution_version"]:
            raise ValueError("This Linux download requires Ubuntu 24.04 x86_64")
    elif target["system"] == "Windows":
        if int(platform.version().split(".")[-1]) < target["minimum_build"]:
            raise ValueError("This download requires Windows 11 or Windows Server 2022 or later")
    if not (payload / "runtime/src/lodestone_machinery/loom_runtime_worker.py").is_file():
        raise ValueError("Desktop payload is missing its local worker")
    # A release keeps its complete dependency set. Upgrades create another set;
    # selecting an earlier payload therefore still selects its original pins.
    deps = root / "deps/sets" / lock_sha256
    cache = check_path(cache) if cache is not None else check_path(root / "downloads")
    with guarded_directory(deps, create=True), file_lock(deps / "bootstrap.lock"):
        artifacts = lock["artifacts"]
        uv = unpack(artifacts["uv"], cache, deps / "uv", offline=offline)
        interpreters = {}
        for key in ("python_worker", "python_gui"):
            artifact = artifacts[key]
            interpreters[key] = unpack(artifact, cache, deps / "python" / artifact["version"], offline=offline)
        environments = {}
        for name in ("worker", "gui"):
            spec = lock["environments"][name]
            environments[name] = _install_environment(name, spec, interpreters[spec["python"]],
                                                     uv, deps, cache, offline=offline)
        ollama = unpack(artifacts["ollama"], cache, deps / "ollama", offline=offline)
        assets = deps / "tokenizer"
        for artifact in lock["tokenizer"]["files"]:
            _copy_asset(artifact, assets / artifact["filename"], cache, offline=offline)
        models = deps / "ollama-models"
        model = lock["model"]
        for artifact in model["blobs"]:
            _copy_asset(artifact, models / "blobs" / ("sha256-" + artifact["sha256"]), cache, offline=offline)
        _copy_asset(model["manifest"], models / _relative(model["manifest_path"]), cache, offline=offline)
        if model["digest"] != model["manifest"]["sha256"]:
            raise ValueError("Model digest differs from pinned manifest")
        _run([str(environments["gui"]), "-I", "-B", "-c",
              "import wx,sqlalchemy,yaml,pydantic,sounddevice; print(wx.version())"], deps)
        verification = _run([str(environments["worker"]), "-I", "-B", "-c",
            "import sys; sys.path.insert(0,sys.argv[1]); "
            "from lodestone_machinery.loom_runtime_worker import verified_tokenizer; "
            "t,_=verified_tokenizer(sys.argv[2]); import jinja2; "
            "print(t.apply_chat_template([{'role':'user','content':'Local tokenizer check.'}],tokenize=False,add_generation_prompt=True,enable_thinking=False))",
            str(payload / "runtime/src"), str(assets)], deps)
        result = {"gui_python": str(environments["gui"]), "worker_python": str(environments["worker"]),
                  "assets_dir": str(assets), "ollama_binary": str(ollama), "ollama_models": str(models),
                  "model_digest": model["digest"], "evidence": {
                      "lock_sha256": lock_sha256, "dependency_set": str(deps),
                      "platform": target, "cache": str(cache), "offline": offline,
                      "gui_imports": "PASS", "tokenizer_only": "PASS",
                      "tokenizer_template_sha256": hashlib.sha256(verification.encode()).hexdigest(),
                      "model_calls": 0, "paid_api_calls": 0}}
        _json(deps / "dependencies.json", result)
        return result


def verify(root: Path, payload: Path) -> dict:
    """Read-only explicit doctor check, including installed model blob hashes.

    Cache archives are retained as the independent expected Python/uv/Ollama
    tree source. Environment file receipts detect change since installation.
    This deliberately does no downloading, repair, importing or inference.
    """
    root, payload = check_path(root), check_path(payload)
    lock_bytes = read_file(payload / "dependencies.lock.json")
    lock_sha256 = hashlib.sha256(lock_bytes).hexdigest()
    lock = json.loads(lock_bytes)
    if lock.get("schema") != SCHEMA:
        raise ValueError("Unsupported dependency lock")
    deps = root / "deps/sets" / lock_sha256
    installed = json.loads(read_file(deps / "dependencies.json"))
    if installed["evidence"]["lock_sha256"] != lock_sha256:
        raise ValueError("Installed dependencies belong to another lock")
    if not (payload / "runtime/src/lodestone_machinery/loom_runtime_worker.py").is_file():
        raise ValueError("Desktop payload is missing its local worker")
    cache = check_path(installed["evidence"]["cache"])
    for name, artifact in lock["artifacts"].items():
        archive = cache / artifact["sha256"]
        if not _verified(archive, artifact):
            raise ValueError("Doctor needs the verified archive cache: " + name)
        destination = (deps / "python" / artifact["version"] if name.startswith("python_")
                       else deps / name)
        if _inventory(destination) != _archive_inventory(archive, maximum=artifact.get("unpacked_limit_bytes", 1024**3)):
            raise ValueError("Dependency tree changed: " + name)
    for name, spec in lock["environments"].items():
        receipt = json.loads(read_file(deps / (name + ".installed.json")))
        fingerprint = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
        if receipt["spec_sha256"] != fingerprint or receipt["files"] != _inventory(deps / name):
            raise ValueError("Installed dependency environment changed: " + name)
    for artifact in lock["tokenizer"]["files"]:
        if not _verified(deps / "tokenizer" / artifact["filename"], artifact):
            raise ValueError("Installed tokenizer file changed")
    model = lock["model"]
    for artifact in model["blobs"]:
        if not _verified(deps / "ollama-models/blobs" / ("sha256-" + artifact["sha256"]), artifact):
            raise ValueError("Installed model blob changed")
    if not _verified(deps / "ollama-models" / _relative(model["manifest_path"]), model["manifest"]):
        raise ValueError("Installed model manifest changed")
    # Derive the runtime contract from the selected, verified lock. Receipt
    # paths are bookkeeping and must never redirect doctor to another set.
    paths = {"gui_python": str(venv_python(deps / "gui")),
             "worker_python": str(venv_python(deps / "worker")),
             "assets_dir": str(deps / "tokenizer"),
             "ollama_binary": str(deps / "ollama" / _relative(lock["artifacts"]["ollama"]["executable"])),
             "ollama_models": str(deps / "ollama-models"),
             "model_digest": model["digest"]}
    return {"status": "VERIFIED", "lock_sha256": lock_sha256, "dependency_set": str(deps),
            "paths": paths,
            "model_digest": model["digest"], "network_calls": 0, "model_calls": 0,
            "environment_integrity": "Matches installation file receipts",
            "artifact_integrity": "Matches pinned archives, tokenizer files and model blobs"}
