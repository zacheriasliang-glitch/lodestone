"""Pinned, resumable local dependencies for the Intel macOS desktop preview.

This module uses the standard library and system curl. It installs only into
the supplied root, never invokes a model, and never consults an existing Loom
installation. Downloaded archives and wheels are checked before execution.
"""
from __future__ import annotations

import fcntl
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
from urllib.parse import urlsplit


SCHEMA = "lodestone-desktop-dependencies/v1"
LOCK_PATH = Path(__file__).with_name("dependencies.lock.json")


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json(path: Path, value) -> None:
    temporary = path.with_name(path.name + ".new")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


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
    return (not path.is_symlink() and path.is_file()
            and path.stat().st_size == artifact["size"] and _sha(path) == artifact["sha256"])


def fetch(artifact: dict, cache: Path, *, offline: bool = False) -> Path:
    """Return a verified cache object; retain interrupted transfers for retry."""
    _artifact(artifact)
    cache.mkdir(parents=True, exist_ok=True)
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
    command = ["/usr/bin/curl", "--fail", "--location", "--silent", "--show-error",
               "--proto", "=https", "--proto-redir", "=https", "--tlsv1.2",
               "--retry", "2", "--connect-timeout", "30", "--max-time", "1800",
               "--output", str(partial)]
    if partial.exists() and partial.stat().st_size:
        command.extend(["--continue-at", "-"])
    subprocess.run(command + [artifact["url"]], check=True, timeout=1830)
    if not _verified(partial, artifact):
        raise ValueError("Downloaded dependency failed its pinned hash: " + artifact["filename"])
    os.replace(partial, target)
    return target


def _relative(name: str) -> str:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name:
        raise ValueError("Archive path escapes its destination")
    return str(path)


def _archive_inventory(archive: Path) -> dict:
    """Validate every member before extraction and describe exact file bytes."""
    result, names, total = {}, set(), 0
    with tarfile.open(archive, "r:gz") as stream:
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
                if total > 1024 * 1024 * 1024:
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
    result = {}
    for path in sorted(root.rglob("*")):
        name = path.relative_to(root).as_posix()
        if path.is_symlink():
            result[name] = {"link": os.readlink(path)}
        elif path.is_file():
            result[name] = {"sha256": _sha(path), "size": path.stat().st_size}
    return result


def unpack(artifact: dict, cache: Path, destination: Path, *, offline=False) -> Path:
    archive = fetch(artifact, cache, offline=offline)
    expected = _archive_inventory(archive)
    if destination.is_symlink():
        raise ValueError("Dependency destination must not be a symlink")
    if destination.exists():
        if _inventory(destination) != expected:
            raise ValueError("Existing dependency tree differs from its verified archive: " + str(destination))
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=destination.name + ".partial-", dir=destination.parent))
        with tarfile.open(archive, "r:gz") as stream:
            stream.extractall(stage, filter="data")
        if _inventory(stage) != expected:
            raise ValueError("Extracted dependency differs from verified archive")
        os.replace(stage, destination)
    executable = destination / _relative(artifact["executable"])
    if (not executable.resolve().is_relative_to(destination.resolve())
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
    env_path = root / name
    receipt_path = root / (name + ".installed.json")
    fingerprint = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
    wheels = []
    for wheel in spec["wheels"]:
        cached = fetch(wheel, cache, offline=offline)
        directory = root / "wheels" / wheel["sha256"]
        directory.mkdir(parents=True, exist_ok=True)
        local = directory / wheel["filename"]
        if not local.exists():
            shutil.copyfile(cached, local)
        if not _verified(local, wheel):
            raise ValueError("Local installation wheel changed")
        wheels.append((local, wheel))
    if receipt_path.is_file():
        receipt = json.loads(receipt_path.read_text())
        if receipt["spec_sha256"] != fingerprint or receipt["files"] != _inventory(env_path):
            raise ValueError("Installed dependency environment changed: " + name)
    else:
        if env_path.exists():
            # A failed attempt is retained, and the next attempt uses the same
            # final path so generated script shebangs never point into staging.
            env_path.rename(env_path.with_name(name + ".interrupted-" + str(time.time_ns())))
        _run([str(uv), "venv", "--python", str(python), str(env_path)], root)
        requirements = root / (name + ".requirements.txt")
        requirements.write_text("\n".join(local.as_uri() + " --hash=sha256:" + wheel["sha256"]
                                          for local, wheel in wheels) + "\n")
        _run([str(uv), "pip", "install", "--python", str(env_path / "bin/python"),
              "--offline", "--no-index", "--no-deps", "--require-hashes",
              "--only-binary", ":all:", "--requirements", str(requirements)], root)
        _run([str(uv), "pip", "check", "--python", str(env_path / "bin/python")], root)
        _json(receipt_path, {"spec_sha256": fingerprint, "files": _inventory(env_path)})
    actual = json.loads(_run([str(env_path / "bin/python"), "-I", "-B", "-c",
        "import importlib.metadata as m,json,re; print(json.dumps({re.sub(r'[-_.]+','-',d.metadata['Name']).lower():d.version for d in m.distributions()}))"], root))
    expected = {wheel["name"]: wheel["version"] for wheel in spec["wheels"]}
    if actual != expected:
        raise ValueError("Installed package versions differ from the complete dependency lock")
    return env_path / "bin/python"


def _copy_asset(artifact: dict, destination: Path, cache: Path, *, offline: bool) -> None:
    source = fetch(artifact, cache, offline=offline)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        if not _verified(destination, artifact):
            raise ValueError("Installed asset differs from pinned identity: " + str(destination))
    else:
        temporary = destination.with_name(destination.name + ".partial")
        if temporary.is_symlink():
            raise ValueError("Asset partial must not be a symlink")
        shutil.copyfile(source, temporary)
        os.replace(temporary, destination)


def ensure(root: Path, payload: Path, cache: Path | None = None, *, offline: bool = False) -> dict:
    """Build/recheck isolated environments and assets; never run inference."""
    root, payload = Path(root).resolve(), Path(payload).resolve()
    lock_bytes = (payload / "dependencies.lock.json").read_bytes()
    lock_sha256 = hashlib.sha256(lock_bytes).hexdigest()
    lock = json.loads(lock_bytes)
    if lock.get("schema") != SCHEMA:
        raise ValueError("Unsupported dependency lock")
    target = lock["platform"]
    if platform.system() != target["system"] or platform.machine() != target["machine"]:
        raise ValueError("This preview has verified dependencies only for macOS x86_64")
    actual_os = tuple(int(part) for part in platform.mac_ver()[0].split(".")[:2])
    if actual_os < tuple(int(part) for part in target["minimum_macos"].split(".")):
        raise ValueError("This preview requires macOS " + target["minimum_macos"] + " or later")
    if not (payload / "runtime/src/lodestone_machinery/loom_runtime_worker.py").is_file():
        raise ValueError("Desktop payload is missing its local worker")
    # A release keeps its complete dependency set. Upgrades create another set;
    # selecting an earlier payload therefore still selects its original pins.
    deps = root / "deps/sets" / lock_sha256
    deps.mkdir(parents=True, exist_ok=True)
    cache = Path(cache).resolve() if cache is not None else root / "downloads"
    with (deps / "bootstrap.lock").open("a") as guard:
        try:
            fcntl.flock(guard.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Dependency setup is already running for this root") from None
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
    root, payload = Path(root).resolve(), Path(payload).resolve()
    lock_bytes = (payload / "dependencies.lock.json").read_bytes()
    lock_sha256 = hashlib.sha256(lock_bytes).hexdigest()
    lock = json.loads(lock_bytes)
    if lock.get("schema") != SCHEMA:
        raise ValueError("Unsupported dependency lock")
    deps = root / "deps/sets" / lock_sha256
    installed = json.loads((deps / "dependencies.json").read_text())
    if installed["evidence"]["lock_sha256"] != lock_sha256:
        raise ValueError("Installed dependencies belong to another lock")
    if not (payload / "runtime/src/lodestone_machinery/loom_runtime_worker.py").is_file():
        raise ValueError("Desktop payload is missing its local worker")
    cache = Path(installed["evidence"]["cache"])
    for name, artifact in lock["artifacts"].items():
        archive = cache / artifact["sha256"]
        if not _verified(archive, artifact):
            raise ValueError("Doctor needs the verified archive cache: " + name)
        destination = (deps / "python" / artifact["version"] if name.startswith("python_")
                       else deps / name)
        if _inventory(destination) != _archive_inventory(archive):
            raise ValueError("Dependency tree changed: " + name)
    for name, spec in lock["environments"].items():
        receipt = json.loads((deps / (name + ".installed.json")).read_text())
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
    paths = {"gui_python": str(deps / "gui/bin/python"),
             "worker_python": str(deps / "worker/bin/python"),
             "assets_dir": str(deps / "tokenizer"),
             "ollama_binary": str(deps / "ollama" / _relative(lock["artifacts"]["ollama"]["executable"])),
             "ollama_models": str(deps / "ollama-models"),
             "model_digest": model["digest"]}
    return {"status": "VERIFIED", "lock_sha256": lock_sha256, "dependency_set": str(deps),
            "paths": paths,
            "model_digest": model["digest"], "network_calls": 0, "model_calls": 0,
            "environment_integrity": "Matches installation file receipts",
            "artifact_integrity": "Matches pinned archives, tokenizer files and model blobs"}
