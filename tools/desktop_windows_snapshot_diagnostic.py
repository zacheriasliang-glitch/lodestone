#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Bounded native process-exit sampling diagnostic; no models or product edits."""
import argparse
import ctypes
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import traceback

PRODUCT_COMMIT = "13dfc915a76470aa876740b4f2ff3650e9021352"
MODULE = "desktop/runtime/src/lodestone_machinery/portable_process.py"
CHILD = r'''
import ctypes, os, subprocess, sys, time
from pathlib import Path
mode, count = sys.argv[1], int(sys.argv[2])
if mode == 'parent':
    children = [subprocess.Popen([sys.executable, '-B', __file__, str(i), str(count)]) for i in range(8)]
    Path('ready-parent').write_text(str(os.getpid()), encoding='ascii')
    while not Path('parent-exit').exists():
        time.sleep(.005)
else:
    from ctypes import wintypes as w
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.GetCurrentProcess.restype = w.HANDLE
    k.GetStdHandle.argtypes, k.GetStdHandle.restype = [w.DWORD], w.HANDLE
    k.DuplicateHandle.argtypes = [w.HANDLE,w.HANDLE,w.HANDLE,ctypes.POINTER(w.HANDLE),w.DWORD,w.BOOL,w.DWORD]
    k.DuplicateHandle.restype = w.BOOL
    current, stderr, handles = k.GetCurrentProcess(), k.GetStdHandle(-12), []
    for _ in range(count):
        copied = w.HANDLE()
        if not k.DuplicateHandle(current, stderr, current, ctypes.byref(copied), 0, False, 2):
            raise ctypes.WinError(ctypes.get_last_error())
        handles.append(copied.value)
    Path('ready-' + mode).write_text(str(os.getpid()), encoding='ascii')
    while not Path('exit').exists():
        time.sleep(.005)
'''


def error(exc):
    return {"type": type(exc).__name__, "message": str(exc),
            "errno": getattr(exc, "errno", None), "winerror": getattr(exc, "winerror", None)}


class ApiTrace:
    """Observe failures without changing native returns or ctypes last-error state."""
    def __init__(self, child, row):
        self.child, self.row, self.originals = child, row, []
        self.handles = {child._process: child.pid}
        self.started = time.monotonic()

    def install(self):
        for owner, names in ((self.child.api.kernel, ("OpenProcess", "IsProcessInJob",
                "QueryFullProcessImageNameW", "QueryInformationJobObject", "WaitForSingleObject",
                "GetExitCodeProcess", "GetQueuedCompletionStatus")),
                (self.child.api.psapi, ("GetProcessMemoryInfo",))):
            for name in names:
                original = getattr(owner, name)
                self.originals.append((owner, name, original))
                def call(*args, original=original, name=name):
                    result = original(*args)
                    code = ctypes.get_last_error()
                    handle = args[0].value if hasattr(args[0], "value") else args[0]
                    pid = int(args[2]) if name == "OpenProcess" else self.handles.get(handle)
                    if name == "OpenProcess" and result:
                        self.handles[result] = pid
                    failed = result == 0xffffffff if name == "WaitForSingleObject" else not result
                    if name == "GetQueuedCompletionStatus" and code == 258:
                        failed = False
                    if failed:
                        self.row["native_failure_count"] += 1
                        if len(self.row["native_failures"]) < 256:
                            self.row["native_failures"].append({"api": name, "winerror": code,
                                "pid": pid, "handle": handle if name != "OpenProcess" else None,
                                "snapshot": self.row["snapshot_calls"],
                                "seconds": round(time.monotonic() - self.started, 6)})
                    ctypes.set_last_error(code)
                    return result
                setattr(owner, name, call)

    def restore(self):
        for owner, name, original in self.originals:
            setattr(owner, name, original)


def case(native, root, copies, save, row):
    root.mkdir()
    script = root / "child.py"
    script.write_text(CHILD, encoding="utf-8")
    child, trace, handles = None, None, []
    environment = {key: os.environ[key] for key in ("SystemRoot", "WINDIR") if key in os.environ}
    environment.update(TEMP=str(root), TMP=str(root), PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
    try:
        with (root / "out").open("wb") as out, (root / "err").open("wb") as err:
            child = native.WindowsJobProcess([sys.executable, "-B", str(script), "parent", str(copies)],
                cwd=str(root), env=environment, stdout=out, stderr=err, max_memory_bytes=1024**3)
        ready = [root / ("ready-" + str(i)) for i in range(8)] + [root / "ready-parent"]
        deadline = time.monotonic() + 20
        while True:
            try:
                declared = {int(path.read_text()) for path in ready}
                break
            except (FileNotFoundError, ValueError):
                if time.monotonic() >= deadline or child.poll() is not None:
                    raise RuntimeError("Authored descendants did not become ready")
                time.sleep(.01)
        pids = child.pids()
        if not declared <= set(pids) or len(pids) > 64:
            raise RuntimeError("Fixture Job membership differs or exceeds bound")
        row.update(root_pid=child.pid, declared_pids=sorted(declared), before_exit_pids=pids)
        for pid in pids:
            handle = child.api.check(child.api.kernel.OpenProcess(0x100000 | 0x1000, False, pid))
            belongs = child.api.w.BOOL()
            try:
                child.api.check(child.api.kernel.IsProcessInJob(handle, child._job, ctypes.byref(belongs)))
                if not belongs.value:
                    raise RuntimeError("Held diagnostic handle is outside the fixture Job")
            except BaseException:
                child.api.kernel.CloseHandle(handle)
                raise
            handles.append((pid, handle))
        trace = ApiTrace(child, row)
        trace.install()
        row["before_exit_rss"] = child.snapshot()
        (root / "exit").write_bytes(b"natural exit")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and row["snapshot_calls"] < 10000:
            row["snapshot_calls"] += 1
            try:
                child.memory_observation()
                sample = child.snapshot()
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                row["sampling_exception"] = error(exc)
                row["failure_traceback"] = traceback.format_exc()
                # Preserve the failing call before subsequent native observations.
                row["failure_api_tail"] = row["native_failures"][-16:]
                try:
                    row["membership_after_error"] = child.pids()
                except (OSError, ValueError) as membership_error:
                    row["membership_error"] = error(membership_error)
                row["held_waits_after_error"] = {str(pid): child.api.kernel.WaitForSingleObject(handle, 0)
                                                for pid, handle in handles}
                break
            row["last_sample_pids"] = [member["pid"] for member in sample]
            if all(child.api.kernel.WaitForSingleObject(handle, 0) == 0
                   for pid, handle in handles if pid in declared - {child.pid}):
                row["natural_descendants_signaled"] = True
                break
        row["finding"] = ("SAMPLING_REFUSAL_OBSERVED" if "sampling_exception" in row
                          else "NO_SAMPLING_REFUSAL_OBSERVED")
        save()
    finally:
        if trace is not None:
            trace.restore()
        if child is not None:
            try:
                row["cleanup_stop"] = child.stop()
                deadline = time.monotonic() + 6
                row["cleanup_held_waits"] = {str(pid): child.api.kernel.WaitForSingleObject(
                    handle, max(0, int((deadline - time.monotonic()) * 1000))) for pid, handle in handles}
                if any(state != 0 for state in row["cleanup_held_waits"].values()):
                    raise RuntimeError("Diagnostic known-member termination was not confirmed")
            finally:
                for _, handle in handles:
                    child.api.kernel.CloseHandle(handle)
                child.close()
                save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-head", required=True)
    args = parser.parse_args()
    if (os.name != "nt" or os.environ.get("GITHUB_ACTIONS") != "true"
            or os.environ.get("REPOSITORY_PRIVATE") != "false"
            or os.environ.get("GITHUB_REPOSITORY") != "zacheriasliang-glitch/lodestone"):
        raise RuntimeError("Diagnostic requires the disposable public Windows runner")
    source = args.source.resolve()
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    if head != args.expected_head:
        raise RuntimeError("Diagnostic source HEAD differs")
    raw = (source / MODULE).read_bytes()
    if raw != subprocess.check_output(["git", "show", PRODUCT_COMMIT + ":" + MODULE], cwd=source):
        raise RuntimeError("Diagnostic must observe the unchanged failing product")
    spec = importlib.util.spec_from_file_location("observed_native", source / MODULE)
    native = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(native)
    report = {"schema": "lodestone-windows-snapshot-diagnostic/v1", "head": head,
        "product_commit": PRODUCT_COMMIT, "product_sha256": hashlib.sha256(raw).hexdigest(),
        "model_calls": 0, "paid_api_calls": 0, "weight_updates": 0, "cases": []}
    def save():
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        root = Path(tempfile.mkdtemp(prefix="snapshot-diagnostic-", dir=os.environ["RUNNER_TEMP"]))
        report["fixture_root"] = str(root)
        for copies in (0, 4096, 32768):
            row = {"copies_per_descendant": copies, "descendants": 8, "snapshot_calls": 0,
                   "native_failure_count": 0, "native_failures": []}
            report["cases"].append(row)
            case(native, root / str(copies), copies, save, row)
            if "sampling_exception" in row:
                break
        report["status"] = "OBSERVATIONS_RETAINED_NOT_RELEASE_PROOF"
    except BaseException as exc:
        report.update(status="DIAGNOSTIC_ERROR", error=error(exc), traceback=traceback.format_exc())
        raise
    finally:
        save()


if __name__ == "__main__":
    main()
