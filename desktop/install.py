#!/usr/bin/env python3
"""Fresh, per-user Lodestone Desktop preview installation and recovery.

No administrator privileges, inherited memories or installed Lodestone are needed.
An immutable verified payload is prepared before the active pointer is replaced.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import uuid

# Isolated (-I) bootstrap Python omits the script directory from sys.path.
# The only added directory is this installer payload, never a user workspace.
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from platform_support import (atomic_write, check_path, default_locations, durable_move,
                              file_lock, guarded_directory, host_platform, read_file, remove_file)

VERSION = "0.4.0-preview.6"
DEFAULT_ROOT, DEFAULT_APP = default_locations()
SCHEMA = "lodestone-desktop-active/v1"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read(path):
    return json.loads(read_file(path))


def release_module(release, name):
    spec = importlib.util.spec_from_file_location("desktop_" + name + "_" + uuid.uuid4().hex,
                                                 Path(release) / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def active_install(root):
    active = read(root / "ACTIVE.json")
    if active.get("schema") != SCHEMA:
        raise ValueError("Unrecognized active installation")
    identity = active.get("release_id", "")
    if not re.fullmatch(r"[0-9a-f]{64}", identity):
        raise ValueError("Invalid active release identity")
    release = root / "releases" / identity
    if active.get("release") != str(release) or release.resolve() != release:
        raise ValueError("Active release is outside this installation")
    return active


def atomic(path, data, mode=0o600):
    atomic_write(path, data, mode)


def save(path, value):
    atomic(path, json.dumps(value, indent=2, sort_keys=True).encode() + b"\n")


def checked_root(path):
    path = Path(path).expanduser().absolute()
    if path.is_symlink():
        raise ValueError("Installation directory must not be a symbolic link")
    if os.name == "nt":
        check_path(path)
    path = path.resolve()
    check_path(path)
    if path in (Path.home(), Path(path.anchor)):
        raise ValueError("Use a dedicated application directory")
    return path


@contextmanager
def locked(root):
    with guarded_directory(root, create=True):
        try:
            installation = file_lock(root / ".installation.lock")
            installation.__enter__()
        except BlockingIOError as exc:
            raise ValueError("Another installation or recovery is already running") from exc
        try:
            try:
                lifecycle = file_lock(root / ".lifecycle.lock")
                lifecycle.__enter__()
            except BlockingIOError as exc:
                raise ValueError("Close this Lodestone Preview normally before changing its installation") from exc
            try:
                yield
            finally:
                lifecycle.__exit__(None, None, None)
        finally:
            installation.__exit__(None, None, None)


def ownership(root):
    marker = root / "DESKTOP.json"
    expected = {"schema": "lodestone-desktop-owned/v1", "root": str(root)}
    if marker.exists():
        if read(marker) != expected:
            raise ValueError("Directory belongs to another installation")
        return
    allowed = {"downloads", "deps", ".installation.lock", ".lifecycle.lock"}
    if any(path.name not in allowed for path in root.iterdir()):
        raise ValueError("Choose an empty directory; existing unrelated data will not be adopted")
    save(marker, expected)


def platform_for(record):
    # Existing v1 transactions predate a platform field and are Mac bundles.
    platform_name = record.get("platform", "macos")
    if platform_name not in ("macos", "linux", "windows"):
        raise ValueError("Unsupported launcher platform")
    return platform_name


def app_files(app, platform_name=None):
    platform_name = platform_name or host_platform()
    if platform_name == "macos":
        return [app / "Contents/Info.plist", app / "Contents/MacOS/Lodestone"]
    if platform_name not in ("windows", "linux"):
        raise ValueError("Unsupported launcher platform")
    return [app]


def launcher_marker(root):
    return base64.b64encode(str(root).encode("utf-8")).decode("ascii")


def owned_app(app, root=None, platform_name=None):
    platform_name = platform_name or host_platform()
    check_path(app)
    if platform_name != "macos":
        if app.exists():
            if not app.is_file():
                raise ValueError("Application launcher is not a regular file")
            marker = (("rem LodestonePreviewRoot: " if platform_name == "windows" else
                       "X-Lodestone-Root=") + launcher_marker(root))
            if marker not in read_file(app).decode("utf-8").splitlines():
                raise ValueError("Application launcher belongs to another installation")
        return
    if app.resolve() != app:
        raise ValueError("Application location changed to a symbolic link")
    for path in app_files(app, platform_name):
        check_path(path)
    info = app / "Contents/Info.plist"
    if info.exists() and plistlib.loads(info.read_bytes()).get("CFBundleIdentifier") != "org.lodestone.desktop.preview":
        raise ValueError("Application does not belong to this preview")


def launcher_files(root, app, deps, platform_name):
    if platform_name == "macos":
        shell = ("#!/bin/zsh\numask 077\nexport PYTHONDONTWRITEBYTECODE=1\nexec " +
                 shlex.quote(deps["worker_python"]) + " -B " + shlex.quote(str(root / "launch.py")) +
                 ' "$@" >> ' + shlex.quote(str(root / "launcher.log")) + " 2>&1\n").encode()
        info = {"CFBundleDisplayName": "Lodestone Preview", "CFBundleName": "Lodestone Preview",
                "CFBundleIdentifier": "org.lodestone.desktop.preview", "CFBundleExecutable": "Lodestone",
                "CFBundlePackageType": "APPL", "CFBundleShortVersionString": "0.4.0",
                "CFBundleVersion": "1", "NSHighResolutionCapable": True}
        return {app / "Contents/Info.plist": (plistlib.dumps(info), 0o644),
                app / "Contents/MacOS/Lodestone": (shell, 0o755)}
    if platform_name == "windows":
        def quote(value):
            value = str(value)
            if any(c in value for c in '\r\n\x00"'):
                raise ValueError("Invalid Windows launcher path")
            return '"' + value.replace('%', '%%') + '"'
        body = ("@echo off\r\nsetlocal DisableDelayedExpansion\r\nchcp 65001 >nul\r\n"
                "rem LodestonePreviewRoot: " + launcher_marker(root) + "\r\n"
                "set PYTHONDONTWRITEBYTECODE=1\r\n" + quote(deps["worker_python"]) +
                " -B " + quote(root / "launch.py") + " %* >> " + quote(root / "launcher.log") + " 2>&1\r\n")
    elif platform_name == "linux":
        if "=" in deps["worker_python"]:
            raise ValueError("Desktop executable paths cannot contain an equals sign")
        def quote(value):
            value = str(value)
            if any(c in value for c in "\r\n\x00"):
                raise ValueError("Invalid desktop launcher path")
            # Exec quoting is its own grammar, not a shell. Escape literal field
            # codes and backslashes through both desktop-file parsing layers.
            value = value.replace("\\", "\\\\").replace('"', '\\"').replace('`', '\\`').replace('$', '\\$')
            return '"' + value.replace("\\", "\\\\").replace('%', '%%') + '"'
        body = ("[Desktop Entry]\nType=Application\nName=Lodestone Preview\n"
                "Comment=Local Loom practice and retained corrections\n"
                "Exec=" + quote(deps["worker_python"]) + " -B " + quote(root / "launch.py") + "\n"
                "Terminal=false\nCategories=Education;Utility;\n"
                "X-Lodestone-Root=" + launcher_marker(root) + "\n")
    else:
        raise ValueError("Unsupported launcher platform")
    return {app: (body.encode("utf-8"), 0o755)}


def verify_payload(payload):
    payload = Path(payload).resolve()
    document = read(payload / "MANIFEST.json")
    if document.get("schema") != "lodestone-desktop-payload/v1":
        raise ValueError("Unrecognized download manifest")
    files = document.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("Missing payload file manifest")
    for name, expected in files.items():
        p = Path(name)
        if p.is_absolute() or ".." in p.parts or name == "MANIFEST.json":
            raise ValueError("Invalid payload path")
        full = payload / p
        check_path(full)
        if any((payload / Path(*p.parts[:n])).is_symlink() for n in range(1, len(p.parts) + 1)):
            raise ValueError("Payload symbolic links are unsupported")
        if not full.is_file() or digest(full.read_bytes()) != expected:
            raise ValueError("Download integrity check failed: " + name)
    actual = {p.relative_to(payload).as_posix() for p in payload.rglob("*") if p.is_file()
              and "__pycache__" not in p.parts and p != payload / "MANIFEST.json"}
    if actual != set(files):
        raise ValueError("Download contains unlisted or missing files")
    for required in ("install.py", "run_loom.py", "bootstrap_learning.py", "dependencies.py",
                     "runtime/lodestone.py", "runtime/src/lodestone_machinery/loom_runtime.py",
                     "gui/basilisk/main_app.py"):
        if required not in files:
            raise ValueError("Incomplete application payload: " + required)
    return document, digest(canonical(document))


def snapshot(paths):
    result = {}
    for path in paths:
        path = Path(path)
        check_path(path)
        if path.exists() and not path.is_file():
            raise ValueError("Managed file is not a regular file: " + str(path))
        result[str(path)] = None if not path.exists() else {
            "bytes": base64.b64encode(read_file(path)).decode(),
            "mode": path.stat().st_mode & 0o777,
        }
    return result


def restore(before):
    for name, value in before.items():
        path = Path(name)
        check_path(path)
        if value is None:
            remove_file(path)
        else:
            atomic(path, base64.b64decode(value["bytes"], validate=True), value["mode"])


def managed_paths(root, app, platform_name=None):
    return [root / "ACTIVE.json", root / "Loom/runtime.json", root / "launch.py",
            *app_files(app, platform_name)]


def transaction_paths(root, app, report):
    paths = managed_paths(root, app, platform_for(report))
    operation = report.get("operation", "ACTIVATE")
    if operation == "REMOVE":
        paths.append(root / "UNINSTALLED.json")
    elif operation == "ROLLBACK":
        source = Path(report["source_transaction"])
        if source.parent != root / "transactions" or source.suffix != ".json":
            raise ValueError("Rollback source transaction is outside this installation")
        paths.append(source)
    elif operation != "ACTIVATE":
        raise ValueError("Unsupported installation transaction operation")
    return paths


def recover_pending(root):
    """Caller holds the lifecycle lock; retain evidence of interrupted activations."""
    recovered = []
    for path in sorted((root / "transactions").glob("*.json")):
        report = read(path)
        if report.get("state") != "PREPARED":
            continue
        if report.get("schema") != "lodestone-desktop-transaction/v1" or report.get("root") != str(root):
            raise ValueError("Interrupted transaction does not belong to this installation")
        app = Path(report["app"])
        platform_name = platform_for(report)
        owned_app(app, root, platform_name)
        if set(report["before"]) != {str(p) for p in transaction_paths(root, app, report)}:
            raise ValueError("Interrupted transaction contains unexpected restoration paths")
        restore(report["before"])
        report["state"] = {"REMOVE": "RECOVERED_INTERRUPTED_REMOVAL",
                           "ROLLBACK": "RECOVERED_INTERRUPTED_ROLLBACK"}.get(
                               report.get("operation"), "RECOVERED_INTERRUPTED_ACTIVATION")
        save(path, report)
        recovered.append(str(path))
    return recovered


def recover(root):
    root = checked_root(root)
    if not any(read(p).get("state") == "PREPARED" for p in (root / "transactions").glob("*.json")):
        return []
    with locked(root):
        ownership(root)
        return recover_pending(root)


def defaults(root):
    account = "2d6cefb7-5475-4bb2-9773-8330121f704f"
    profile = "411eb50e-238d-4537-b3c7-9360509b769e"
    return {
        "accounts.yml": {"accounts": [{"id": account, "name": "Loom local", "provider_id": "loom"}],
                         "default_account_info": account},
        "profiles.yml": {"profiles": [{"id": profile, "name": "Loom", "account_info": account,
            "ai_model_info": "loom/loom-qwen3-base", "system_prompt": "", "max_tokens": 512,
            "temperature": 0, "top_p": 1, "stream_mode": False}], "default_profile_id": profile},
        "config.yml": {"general": {"language": "en", "automatic_update_mode": "off", "quit_on_close": True},
            "conversation": {"use_accessible_output": False, "reopen_last_conversation": True},
            "server": {"enable": False}},
    }


def runtime_config(root, deps):
    return {
        "schema": "loom-unified-runtime/v1", "backend": "ollama_qwen3_base",
        "learning_config": str(root / "learning/ACTIVE.json"), "python": deps["worker_python"],
        "assets_dir": deps["assets_dir"], "ollama_binary": deps["ollama_binary"],
        "ollama_models": deps["ollama_models"], "ollama_model": "qwen3:0.6b",
        "ollama_model_digest": deps["model_digest"], "max_total_tokens": 8192,
        "max_new_tokens": 512, "max_rss_bytes": 4294967296, "wall_seconds": 1200,
        "runs_dir": str(root / "Loom/runs"), "adapter_state": None,
    }


LAUNCHER_BOOTSTRAP = '''import json, os, sys
from pathlib import Path
root = Path(__file__).resolve().parent
sys.path.insert(0, RELEASE_PATH)
from install import recover
recover(root)
if not (root / "ACTIVE.json").exists():
    raise SystemExit("Interrupted initial activation recovered. Run Install Lodestone.command again.")
active = json.loads((root / "ACTIVE.json").read_text())
'''

LAUNCHER = LAUNCHER_BOOTSTRAP + '''\
os.execv(active["gui_python"], [active["gui_python"], "-B", str(Path(active["release"]) / "run_loom.py"), "--root", str(root), *sys.argv[1:]])
'''

WINDOWS_LAUNCHER = LAUNCHER_BOOTSTRAP + '''\
import subprocess
raise SystemExit(subprocess.call([active["gui_python"], "-B", str(Path(active["release"]) / "run_loom.py"), "--root", str(root), *sys.argv[1:]]))
'''


def launcher_source(release, platform_name):
    # Windows exec creates a separate process and ends the caller. Keep the
    # launcher waiting for its GUI child and propagate that child's exit code.
    # subprocess.call also reaps its own child if waiting is interrupted.
    template = WINDOWS_LAUNCHER if platform_name == "windows" else LAUNCHER
    source = template.replace("RELEASE_PATH", repr(str(release)))
    if platform_name != "macos":
        source = source.replace("Install Lodestone.command", "the Lodestone installer")
    return source


def install(root, payload, *, app=None, cache=None, provision=None, bootstrap=None, platform_name=None):
    platform_name = platform_name or host_platform()
    root = checked_root(root)
    app = checked_root(app or default_locations(platform_name)[1])
    extension = {"windows": ".cmd", "linux": ".desktop"}.get(platform_name)
    if extension and app.suffix.lower() != extension:
        raise ValueError("Application launcher must end with " + extension)
    document, release_id = verify_payload(payload)
    with locked(root):
        ownership(root)
        recover_pending(root)
        existing = active_install(root) if (root / "ACTIVE.json").exists() else None
        if existing and existing.get("schema") != SCHEMA:
            raise ValueError("This directory belongs to a different installation")
        if app.exists() and not existing and not (root / "UNINSTALLED.json").exists() and not (root / "transactions").exists():
            raise ValueError("Application already exists; choose a different app path")
        prior = existing or (read(root / "UNINSTALLED.json") if (root / "UNINSTALLED.json").exists() else None)
        if prior and prior.get("app") != str(app):
            raise ValueError("An upgrade must retain this installation's application path")
        if prior and platform_for(prior) != platform_name:
            raise ValueError("An upgrade must retain this installation's platform")
        owned_app(app, root, platform_name)
        already = bool(existing and existing["release_id"] == release_id)
        release = root / "releases" / release_id
        if not release.exists():
            release.parent.mkdir(parents=True, exist_ok=True)
            stage = release.parent / (".stage-" + uuid.uuid4().hex)
            shutil.copytree(payload, stage, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            verify_payload(stage)
            durable_move(stage, release)
        else:
            verify_payload(release)
        if provision is None:
            provision = release_module(release, "dependencies").ensure
        deps = provision(root, release, cache=cache)
        for field in ("gui_python", "worker_python", "assets_dir", "ollama_binary", "ollama_models"):
            if not Path(deps[field]).is_absolute() or not Path(deps[field]).exists():
                raise ValueError("Dependency was not provisioned: " + field)
        if bootstrap is None:
            bootstrap = release_module(release, "bootstrap_learning").initialize
        learning = bootstrap(root, release / "runtime", Path(deps["worker_python"]))
        launcher = launcher_source(release, platform_name).encode()
        launch_files = launcher_files(root, app, deps, platform_name)
        healthy = all(p.is_file() for p in managed_paths(root, app, platform_name))
        if (already and healthy and (root / "launch.py").read_bytes() == launcher
                and all(path.read_bytes() == content for path, (content, _) in launch_files.items())):
            return dict(existing, status="ALREADY_INSTALLED", user_data_preserved=True)
        for name, value in defaults(root).items():
            path = root / "Loom/data" / name
            if not path.exists():
                save(path, value)
        config_path = root / "Loom/runtime.json"
        wanted = runtime_config(root, deps)
        if config_path.exists():
            old = read(config_path)
            # Keep user limits and additional profiles; update provisioned executable paths.
            for field in ("max_total_tokens", "max_new_tokens", "max_rss_bytes", "wall_seconds", "additional_profiles"):
                if field in old:
                    wanted[field] = old[field]
        paths = managed_paths(root, app, platform_name)
        before = snapshot(paths)
        transaction_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
        transaction = root / "transactions" / (transaction_id + ".json")
        report = {"schema": "lodestone-desktop-transaction/v1", "id": transaction_id,
                  "root": str(root), "app": str(app), "platform": platform_name, "before": before,
                  "release_id": release_id, "state": "PREPARED"}
        save(transaction, report)
        try:
            save(config_path, wanted)
            atomic(root / "launch.py", launcher)
            for path, (content, mode) in launch_files.items():
                atomic(path, content, mode)
            active = {"schema": SCHEMA, "version": document["version"], "release_id": release_id,
                      "release": str(release), "app": str(app), "platform": platform_name, "gui_python": deps["gui_python"],
                      "worker_python": deps["worker_python"], "transaction": str(transaction),
                      "activated_at": datetime.now(timezone.utc).isoformat()}
            save(root / "ACTIVE.json", active)
            report["state"] = "ACTIVATED"
            save(transaction, report)
        except BaseException:
            restore(before)
            report["state"] = "ROLLED_BACK_AFTER_FAILURE"
            save(transaction, report)
            raise
        remove_file(root / "UNINSTALLED.json")
        return dict(active, status="INSTALLED", learning=learning, user_data_preserved=True)


def learning_readiness(root):
    selected = read(root / "learning/ACTIVE.json")
    policy = read(root / "runtime/learning-policy.json")
    for field in ("memory_workspace", "seed_handles"):
        if selected.get(field) != policy.get(field):
            raise ValueError("Learning and Origami selections differ; inspect selection recovery records")
    memory = Path(selected["memory_workspace"]).resolve()
    if not memory.is_relative_to(root) or not memory.is_dir():
        raise ValueError("Selected learning memory is outside this preview or missing")
    if (memory.parent / "STOP").exists():
        raise ValueError("Learning is stopped; inspect the retained STOP and recovery records")
    return {"status": "READY", "memory_workspace": str(memory), "origami_selection_matches": True}


def doctor(root, *, verify_dependencies=None, verify_learning=None):
    root = checked_root(root)
    active = active_install(root)
    _, release_id = verify_payload(Path(active["release"]))
    if release_id != active["release_id"]:
        raise ValueError("Active payload identity differs")
    conf = read(root / "Loom/runtime.json")
    paths = ("python", "assets_dir", "ollama_binary", "ollama_models", "learning_config")
    for name in paths:
        if not Path(conf[name]).exists():
            raise ValueError("Required runtime path is missing: " + name)
    if verify_dependencies is None:
        verify_dependencies = release_module(active["release"], "dependencies").verify
    dependencies = verify_dependencies(root, Path(active["release"]))
    if "paths" in dependencies:
        expected = dependencies["paths"]
        for field in ("gui_python", "worker_python"):
            if active.get(field) != expected[field]:
                raise ValueError("Active interpreter differs from the verified dependency set: " + field)
        for field, dependency in (("python", "worker_python"), ("assets_dir", "assets_dir"),
                                  ("ollama_binary", "ollama_binary"), ("ollama_models", "ollama_models"),
                                  ("ollama_model_digest", "model_digest")):
            if conf.get(field) != expected[dependency]:
                raise ValueError("Runtime differs from the verified dependency set: " + field)
    learning = (verify_learning or learning_readiness)(root)
    return {"status": "READY", "version": active["version"], "release_id": release_id,
            "app": active["app"], "local_reader": "qwen3:0.6b", "model_called": False,
            "dependencies": dependencies, "learning": learning}


def rollback(root):
    root = checked_root(root)
    with locked(root):
        recover_pending(root)
        active = active_install(root)
        transaction = Path(active["transaction"])
        if transaction.parent != root / "transactions":
            raise ValueError("Invalid transaction location")
        report = read(transaction)
        if report.get("root") != str(root) or report.get("release_id") != active["release_id"]:
            raise ValueError("Transaction does not belong to this installation")
        app = Path(active["app"])
        platform_name = platform_for(active)
        owned_app(app, root, platform_name)
        if platform_for(report) != platform_name:
            raise ValueError("Transaction launcher platform differs")
        allowed = {str(p) for p in managed_paths(root, app, platform_name)}
        if set(report["before"]) != allowed:
            raise ValueError("Transaction contains unexpected restoration paths")
        previous = report["before"][str(root / "ACTIVE.json")]
        if previous is not None:
            previous_active = json.loads(base64.b64decode(previous["bytes"], validate=True))
            selected_path = root / "learning/ACTIVE.json"
            selected = read(selected_path)
            if (type(selected) is not dict or selected.get("schema") not in
                    ("lodestone-learning-active/v1", "lodestone-learning-active/v2")):
                raise ValueError("Unsupported learning selection prevents runtime rollback")
            previous_release = root / "releases" / previous_active.get("release_id", "")
            if (previous_active.get("schema") != SCHEMA
                    or previous_active.get("release") != str(previous_release)
                    or previous_release.parent != root / "releases"):
                raise ValueError("Previous runtime does not belong to this installation")
            _, identity = verify_payload(previous_release)
            if identity != previous_active["release_id"]:
                raise ValueError("Previous runtime payload identity differs")
            selection_files = (selected_path, root / "runtime/learning-policy.json")
            before_selection = snapshot(selection_files)
            try:
                # Use this verifier with the target reader's actual modules.
                # Never silently roll an old runtime onto a new-format memory.
                bootstrap = release_module(Path(__file__).resolve().parent, "bootstrap_learning")
                bootstrap.validate_selection(root, previous_release / "runtime",
                                             Path(previous_active["worker_python"]))
            except (ValueError, OSError, subprocess.SubprocessError) as exc:
                raise ValueError("Rollback requires a compatible paired runtime and memory workspace. "
                    "Select a validated v1 import (or restore the exact saved pre-selection ACTIVE and "
                    "policy together) before retrying; all records and active pointers are unchanged. "
                    "Compatibility check: " + str(exc)) from exc
            if snapshot(selection_files) != before_selection:
                raise ValueError("Learning selection changed during rollback compatibility check")
        current = snapshot([*managed_paths(root, app, platform_name), transaction])
        identity = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
        recovery_file = root / "transactions" / (identity + ".json")
        recovery = {"schema": "lodestone-desktop-transaction/v1", "id": identity,
                    "root": str(root), "app": str(app), "platform": platform_name,
                    "operation": "ROLLBACK", "before": current, "state": "PREPARED",
                    "source_transaction": str(transaction),
                    "release_id": active["release_id"]}
        save(recovery_file, recovery)
        try:
            restore(report["before"])
            report["state"] = "ROLLED_BACK"
            save(transaction, report)
            recovery["state"] = "ROLLBACK_COMPLETED"
            save(recovery_file, recovery)
        except BaseException:
            restore(current)
            recovery["state"] = "ROLLBACK_RESTORED_AFTER_FAILURE"
            save(recovery_file, recovery)
            raise
        return {"status": "ROLLED_BACK", "transaction": str(transaction), "user_data_preserved": True}


def uninstall(root):
    root = checked_root(root)
    with locked(root):
        recover_pending(root)
        active = active_install(root)
        app = Path(active["app"])
        platform_name = platform_for(active)
        owned_app(app, root, platform_name)
        before = snapshot([*managed_paths(root, app, platform_name), root / "UNINSTALLED.json"])
        identity = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
        transaction = root / "transactions" / (identity + ".json")
        report = {"schema": "lodestone-desktop-transaction/v1", "id": identity,
                  "root": str(root), "app": str(app), "platform": platform_name,
                  "operation": "REMOVE", "before": before, "state": "PREPARED",
                  "release_id": active["release_id"]}
        save(transaction, report)
        try:
            # Remove only owned launchers. Records, drafts and releases stay.
            for path in app_files(app, platform_name):
                remove_file(path)
            save(root / "UNINSTALLED.json", dict(active, uninstalled_at=datetime.now(timezone.utc).isoformat()))
            remove_file(root / "ACTIVE.json")
            report["state"] = "REMOVED"
            save(transaction, report)
        except BaseException:
            restore(before)
            report["state"] = "REMOVAL_ROLLED_BACK_AFTER_FAILURE"
            save(transaction, report)
            raise
        if platform_name == "macos":
            for directory in (app / "Contents/MacOS", app / "Contents", app):
                try:
                    directory.rmdir()
                except OSError:
                    pass
        return {"status": "UNINSTALLED", "data_retained_at": str(root), "user_data_preserved": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    sub = parser.add_subparsers(dest="command", required=True)
    setup = sub.add_parser("install", help="Install or upgrade from this verified download")
    setup.add_argument("--payload", type=Path, default=Path(__file__).resolve().parent)
    setup.add_argument("--app", type=Path, default=DEFAULT_APP)
    setup.add_argument("--cache", type=Path)
    sub.add_parser("doctor")
    sub.add_parser("rollback")
    sub.add_parser("uninstall")
    launch = sub.add_parser("open")
    launch.add_argument("--verify-startup", action="store_true")
    imp = sub.add_parser("import-memory")
    imp.add_argument("source", type=Path)
    imp.add_argument("destination", type=Path)
    imp.add_argument("--seed", action="append", required=True)
    imp.add_argument("--history-references", action="store_true",
                     help="Import into an isolated reference-history workspace; requires an explicit practice history window")
    select = sub.add_parser("select-memory", help="Use a verified import inside this preview")
    select.add_argument("destination", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.command == "install":
            result = install(args.root, args.payload, app=args.app, cache=args.cache)
        elif args.command == "doctor":
            result = doctor(args.root)
        elif args.command == "rollback":
            result = rollback(args.root)
        elif args.command == "uninstall":
            result = uninstall(args.root)
        elif args.command == "import-memory":
            root = checked_root(args.root)
            active = active_install(root)
            bootstrap = release_module(active["release"], "bootstrap_learning")
            result = bootstrap.import_memory(args.source, args.destination, Path(active["release"]) / "runtime",
                                   Path(active["worker_python"]), seed_handles=args.seed,
                                   **({"history_references": True} if args.history_references else {}))
        elif args.command == "select-memory":
            root = checked_root(args.root)
            with locked(root):
                active = active_install(root)
                bootstrap = release_module(active["release"], "bootstrap_learning")
                result = bootstrap.select_import(root, args.destination, Path(active["release"]) / "runtime",
                                                 Path(active["worker_python"]))
        else:
            root = checked_root(args.root)
            recover(root)
            active = active_install(root)
            argv = [active["gui_python"], "-B", str(Path(active["release"]) / "run_loom.py"),
                    "--root", str(root)]
            if args.verify_startup:
                argv.append("--verify-startup")
            return subprocess.call(argv)
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print("Lodestone setup: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
