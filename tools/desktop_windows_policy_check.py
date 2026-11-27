#!/usr/bin/env python3
"""Exercise Windows path policy only on a disposable public hosted CI runner."""
from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import time
import traceback
import uuid

import desktop_mechanical_check as mechanical

KEY = r"SYSTEM\CurrentControlSet\Control\FileSystem"
VALUE = "LongPathsEnabled"
SCHEMA = "lodestone-windows-long-path-policy/v1"
REFUSAL = "LODESTONE_WINDOWS_PATH_TOO_LONG"


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def encode_policy(policy):
    result = dict(policy)
    if isinstance(result.get("value"), bytes):
        result["value"] = {"base64": base64.b64encode(result["value"]).decode("ascii")}
    return result


class NativeRegistry:
    def __init__(self):
        import winreg
        self.api = winreg

    def read(self):
        w = self.api
        with w.OpenKey(w.HKEY_LOCAL_MACHINE, KEY, 0, w.KEY_READ) as key:
            try:
                value, kind = w.QueryValueEx(key, VALUE)
            except FileNotFoundError:
                return {"present": False, "type": None, "value": None}
        return {"present": True, "type": kind, "value": value}

    def restore(self, policy):
        w = self.api
        with w.OpenKey(w.HKEY_LOCAL_MACHINE, KEY, 0, w.KEY_SET_VALUE) as key:
            if policy["present"]:
                w.SetValueEx(key, VALUE, 0, policy["type"], policy["value"])
            else:
                try:
                    w.DeleteValue(key, VALUE)
                except FileNotFoundError:
                    pass
            w.FlushKey(key)

    def disable(self):
        self.restore({"present": True, "type": self.api.REG_DWORD, "value": 0})


def restore_after(registry, report, action):
    """Preserve exact native type/value/absence, even when the exercise fails."""
    original = registry.read()
    report["original_policy"] = encode_policy(original)
    try:
        action()
    finally:
        try:
            registry.restore(original)
            restored = registry.read()
            report["restored_policy"] = encode_policy(restored)
            report["registry_restored"] = restored == original
            if restored != original:
                raise ValueError("Windows path policy did not restore its original value and type")
        except BaseException as exc:
            report["registry_restored"] = False
            report["restoration_error"] = {"type": type(exc).__name__, "message": str(exc)}
            raise


def require_public_runner(args, *, environ=None, platform=None):
    env = os.environ if environ is None else environ
    if ((sys.platform if platform is None else platform) != "win32"
            or env.get("GITHUB_ACTIONS") != "true"
            or env.get("RUNNER_OS") != "Windows"
            or env.get("RUNNER_ENVIRONMENT") != "github-hosted"
            or env.get("GITHUB_REPOSITORY") != "zacheriasliang-glitch/lodestone"
            or read(env.get("GITHUB_EVENT_PATH", ""))["repository"]["private"] is not False):
        raise ValueError("Registry exercise requires the public Lodestone Windows hosted CI runner")
    if not re.fullmatch(r"[0-9a-f]{40}", args.source_commit) or not re.fullmatch(r"[0-9a-f]{64}", args.release_id):
        raise ValueError("Exact source and installed release identities are required")
    temporary = Path(env["RUNNER_TEMP"]).resolve()
    for value in (args.root, args.payload, args.cache, args.evidence):
        if not Path(value).resolve().is_relative_to(temporary):
            raise ValueError("Existing installation, payload, cache and evidence must belong to RUNNER_TEMP")
    return temporary


def verify_cache(payload, cache):
    """Require all immutable pins before invoking setup; no download fallback."""
    lock = read(Path(payload) / "dependencies.windows-x86_64.lock.json")
    pins = {}
    def visit(value):
        if isinstance(value, dict):
            if {"sha256", "size", "url"}.issubset(value):
                old = pins.setdefault(value["sha256"], value["size"])
                if old != value["size"]:
                    raise ValueError("Conflicting cache pins")
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(lock)
    if not pins:
        raise ValueError("Dependency cache inventory is empty")
    for sha, size in pins.items():
        if not re.fullmatch(r"[0-9a-f]{64}", sha) or type(size) is not int or size <= 0:
            raise ValueError("Invalid immutable cache pin")
        path = Path(cache) / sha
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
                or info.st_size != size or digest(path) != sha):
            raise ValueError("Policy exercise cache is missing or differs from pin: " + sha)
    return {"verified_objects": len(pins), "verified_bytes": sum(pins.values()), "downloads": 0}


def batch_command(script, arguments=()):
    # Values stay inside cmd double quotes. The helper fixture deliberately
    # includes an apostrophe, ampersand and parentheses; none enters PS code.
    executable = str(Path(os.environ["SystemRoot"]) / "System32/cmd.exe")
    values = [str(script), *map(str, arguments)]
    if any(c in value for value in (executable, *values) for c in '%!^|<>"\r\n\x00'):
        raise ValueError("Unsupported CI batch argument")
    return '"' + executable + '" /d /v:off /s /c "' + " ".join('"' + value + '"' for value in values) + '"'


class Exercise:
    def __init__(self, args, report):
        import ctypes
        if not ctypes.windll.shell32.IsUserAnAdmin():
            raise ValueError("Disposable policy exercise requires the already elevated hosted runner")
        self.args, self.report = args, report
        self.evidence = Path(args.evidence)
        self.registry = NativeRegistry()
        self.owned = []
        self.deadline = time.monotonic() + 1200
        location = Path(args.source) / "desktop/runtime/src/lodestone_machinery/portable_process.py"
        spec = importlib.util.spec_from_file_location("policy_native_process", location)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.job_factory = module.WindowsJobProcess

    def command(self, label, command, *, refusal=False, timeout=300):
        start = time.monotonic()
        log = self.evidence / "logs" / (label + ".log")
        log.parent.mkdir(parents=True, exist_ok=True)
        receipt = {"command": command, "status": "FAILED"}
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", UV_OFFLINE="1", PIP_NO_INDEX="1")
        try:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Policy work budget exhausted; restoring original registry state")
            timeout = min(timeout, remaining)
            with log.open("w", encoding="utf-8") as output:
                # The Python shim preserves raw cmd quoting while the native
                # Job owns it and every installer child before they can run.
                # No UAC broker is involved: this CI runner is already admin.
                wrapper = [sys.executable, "-I", "-B", "-c",
                    "import json,subprocess,sys; raise SystemExit(subprocess.call(json.loads(sys.argv[1]), shell=False))",
                    json.dumps(command)]
                job = self.job_factory(wrapper, cwd=self.args.source, env=env,
                                       stdout=output, stderr=output, max_memory_bytes=4294967296)
                try:
                    returncode = job.wait(timeout=timeout)
                    receipt["returncode"] = returncode
                finally:
                    try:
                        stopped = job.stop()
                        receipt["owned_tree_cleanup"] = stopped
                        if not stopped.get("confirmed_stopped"):
                            raise ValueError("Owned installer process tree did not stop")
                    finally:
                        job.close()
            text = log.read_text(encoding="utf-8", errors="replace")
            if refusal:
                if returncode == 0 or REFUSAL not in text:
                    raise ValueError("Long root did not refuse before setup")
                receipt["status"] = "EXPECTED_REFUSAL"
                receipt["refusal_marker"] = REFUSAL
            else:
                if returncode:
                    raise subprocess.CalledProcessError(returncode, command)
                receipt["status"] = "PASSED"
            return text
        except BaseException as exc:
            receipt["error"] = {"type": type(exc).__name__, "message": str(exc)}
            raise
        finally:
            receipt["elapsed_seconds"] = round(time.monotonic() - start, 3)
            mechanical.write(self.evidence / (label + ".json"), receipt)
            self.report["commands"][label] = receipt

    def own(self, path):
        path.mkdir(exist_ok=False)
        info = path.stat()
        self.owned.append((path, info.st_dev, info.st_ino))
        return path

    def doctor(self, label, root, active):
        text = self.command(label, [active["worker_python"], "-I", "-B",
            str(Path(active["release"]) / "install.py"), "--root", str(root), "doctor"], timeout=600)
        outcome = json.loads(text)
        if outcome.get("status") != "READY" or outcome.get("release_id") != self.args.release_id or outcome.get("model_called") is not False:
            raise ValueError("Doctor did not prove the expected installed release without inference")

    def enable_helper(self, helper):
        copied = {}
        try:
            self.command("windows-policy-enable", batch_command(helper, ["--yes"]))
        finally:
            # A failed elevation can already have a prior-value receipt. Keep
            # both authored records even when the command raises or times out.
            log = self.evidence / "logs/windows-policy-enable.log"
            text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
            for marker, name in (("LODESTONE_LONG_PATH_RECEIPT", "windows-policy-helper-prior.json"),
                                 ("LODESTONE_LONG_PATH_RESULT", "windows-policy-helper-result.json")):
                paths = [line.partition("=")[2].strip() for line in text.splitlines() if line.startswith(marker + "=")]
                if not paths:
                    continue
                if len(paths) != 1:
                    raise ValueError("Helper printed ambiguous policy change receipts")
                source = Path(paths[0])
                info = source.lstat()
                if (not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
                        or info.st_size > 65536):
                    raise ValueError("Helper receipt is not a bounded regular file")
                copied[marker] = read(source)
                shutil.copyfile(source, self.evidence / name)
        if len(copied) != 2:
            raise ValueError("Helper did not retain both policy change receipts")
        return copied["LODESTONE_LONG_PATH_RECEIPT"], copied["LODESTONE_LONG_PATH_RESULT"]

    def perform(self):
        args = self.args
        root, payload, cache = Path(args.root), Path(args.payload), Path(args.cache)
        active = read(root / "ACTIVE.json")
        if active["release_id"] != args.release_id:
            raise ValueError("Normal installed release differs from policy exercise identity")
        original_active = digest(root / "ACTIVE.json")
        budget = read(payload / "windows-path-budget.json")["scopes"]["root"]
        root_units = len(str(root).encode("utf-16-le")) // 2
        if (root_units + budget["max_file_relative_utf16"] <= 259
                and root_units + budget["max_directory_relative_utf16"] <= 247):
            raise ValueError("Normal installed root does not exercise paths beyond the legacy limit")
        self.report["normal_root_requires_long_paths"] = True
        self.registry.disable()
        if self.registry.read() != {"present": True, "type": 4, "value": 0}:
            raise ValueError("Policy zero could not be confirmed")
        self.report["policy_zero"]["effective_value"] = 0

        # Deliberately outside RUNNER_TEMP: its nested path can itself exceed
        # this payload's legacy root budget. This fresh drive-root child is
        # used only on the guarded disposable runner, recorded, then removed.
        short = self.own(Path(Path(os.environ["RUNNER_TEMP"]).anchor) / ("lp-" + uuid.uuid4().hex[:8]))
        self.report["short_root"] = {"path": str(short), "created_by_this_check": True,
            "reason_outside_runner_temp": "Verified legacy root budget requires a direct drive-root fixture"}
        short_app = short / "Loom.cmd"
        self.command("windows-policy-short-install", batch_command(payload / "Install Lodestone.cmd",
            ["--root", short, "--app", short_app, "--cache", cache, "--no-open"]), timeout=1800)
        short_active = read(short / "ACTIVE.json")
        if short_active["release_id"] != args.release_id:
            raise ValueError("Short installation selected a different release")
        self.report["policy_zero"]["short_root_install"] = True
        self.doctor("windows-policy-short-doctor", short, short_active)
        self.report["policy_zero"]["doctor_ready"] = True

        refused = Path(os.environ["RUNNER_TEMP"]) / ("lp-refuse-" + uuid.uuid4().hex[:8]) / ("long-root-" + "x" * 64)
        if refused.parent.exists():
            raise ValueError("Refusal fixture path already exists")
        def cache_state():
            return {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in cache.iterdir()}
        before_cache = cache_state()
        self.command("windows-policy-long-refusal", batch_command(payload / "Install Lodestone.cmd",
            ["--root", refused, "--app", refused / "Loom.cmd", "--cache", cache, "--no-open"]), refusal=True)
        if refused.parent.exists():
            raise ValueError("Rejected long path created installation directories")
        if cache_state() != before_cache:
            raise ValueError("Rejected long path changed the verified cache")
        self.report["policy_zero"]["long_root_refused_before_mutation"] = True

        helpers = self.own(Path(os.environ["RUNNER_TEMP"]) / ("lp-helper-" + uuid.uuid4().hex[:8] + " O'Brien & (paths)"))
        helper = helpers / "Enable Windows Long Paths.cmd"
        shutil.copyfile(payload / helper.name, helper)
        helper_hash = digest(helper)
        prior, result = self.enable_helper(helper)
        if (prior.get("schema") != "lodestone-windows-long-path-change/v1"
                or prior.get("previous_exists") is not True or prior.get("previous_kind") != "DWord"
                or prior.get("previous_value") != 0 or prior.get("requested_value") != 1
                or prior.get("helper_sha256") != helper_hash or result.get("state") != "APPLIED"
                or self.registry.read() != {"present": True, "type": 4, "value": 1}):
            raise ValueError("Explicit helper did not prove its single-value policy change")
        self.report["policy_one"].update(helper_explicit_yes=True, effective_value=1,
                                        special_character_helper_path=True)
        text = self.command("windows-policy-enabled-preflight", [short_active["worker_python"], "-I", "-B",
            str(payload / "windows_preflight.py"), "--payload", str(payload), "--root", str(root),
            "--app", args.app, "--cache", str(cache)])
        outcome = json.loads(text)
        if outcome.get("status") != "PATHS_ACCEPTED" or outcome.get("long_paths_enabled") is not True:
            raise ValueError("Enabled-policy preflight did not accept the normal installation")
        self.report["policy_one"]["long_root_preflight_passed"] = True
        self.doctor("windows-policy-deep-doctor", root, active)
        if digest(root / "ACTIVE.json") != original_active:
            raise ValueError("Policy exercise changed the normal active installation")
        self.report["policy_one"]["deep_doctor_ready"] = True

    def cleanup(self):
        failures = []
        for path, device, inode in reversed(self.owned):
            try:
                if not path.exists():
                    continue
                info = path.lstat()
                if ((info.st_dev, info.st_ino) != (device, inode) or not stat.S_ISDIR(info.st_mode)
                        or getattr(info, "st_file_attributes", 0) & 0x400):
                    raise ValueError("Owned fixture changed identity; refusing cleanup")
                # Failed setup can have learning/transaction records without
                # ACTIVE.json. Preserve those as well, before deleting our tree.
                destination = self.evidence / "windows-policy-short-state"
                files = [p for folder in ("learning", "Loom/data", "transactions")
                         for p in (path / folder).rglob("*") if not p.is_dir()]
                files += [path / name for name in ("ACTIVE.json", "DESKTOP.json") if (path / name).exists()]
                total = 0
                for source in files:
                    info = source.lstat()
                    total += info.st_size
                    if (not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
                            or info.st_size > 8 * 1024**2 or total > 32 * 1024**2 or len(files) > 1024):
                        raise ValueError("Authored policy state exceeds its bounded regular-file evidence scope")
                    target = destination / source.relative_to(path)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
                shutil.rmtree(path)
            except BaseException as exc:
                failures.append({"path": str(path), "error": str(exc)})
        self.report["cleanup"] = {"owned_fixtures_removed": not failures, "failures": failures}
        if failures:
            raise ValueError("Disposable Windows policy fixture cleanup failed")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "source-commit", "payload", "root", "app", "cache", "evidence", "release-id"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args(argv)
    report = {"schema": SCHEMA, "status": "FAILED", "source_commit": args.source_commit,
        "release_id": args.release_id, "original_policy": None, "restored_policy": None,
        "registry_restored": False, "policy_zero": {}, "policy_one": {}, "commands": {},
        "model_calls": 0, "paid_api_calls": 0, "work_budget_seconds": 1200,
        "process_ownership": "Suspended-then-assigned Windows Jobs; 4 GiB commit cap; stop/close before registry restoration"}
    exercise = None
    try:
        require_public_runner(args)
        mechanical.checkout_commit(Path(args.source), args.source_commit)
        _, manifest = mechanical.payload_files(Path(args.payload))
        canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        if hashlib.sha256(canonical).hexdigest() != args.release_id:
            raise ValueError("Payload differs from the expected installed release")
        report["cache"] = verify_cache(args.payload, args.cache)
        exercise = Exercise(args, report)
        restore_after(exercise.registry, report, exercise.perform)
        report["status"] = "PASSED"
    except BaseException as exc:
        report["error"] = {"type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc()}
    finally:
        if exercise is not None:
            try:
                exercise.cleanup()
            except BaseException as exc:
                report["status"] = "FAILED"
                report["cleanup_error"] = str(exc)
        mechanical.write(Path(args.evidence) / "windows-long-path-policy.json", report)
        print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
