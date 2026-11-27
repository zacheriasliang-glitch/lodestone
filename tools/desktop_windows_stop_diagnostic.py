#!/usr/bin/env python3
"""Bounded hosted Windows Job teardown diagnostic; no models or product edits.

Contracts: learn.microsoft.com/windows/win32/api/jobapi2/nf-jobapi2-terminatejobobject
and learn.microsoft.com/windows/win32/api/processthreadsapi/nf-processthreadsapi-terminateprocess.
Restart Manager is queried for one fixture file, never asked to stop/restart owners.
"""
import argparse
import ctypes
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import traceback

CHILD = r'''
import ctypes, os, subprocess, sys, time
from pathlib import Path
from ctypes import wintypes as w
level, count = map(int, sys.argv[1:])
if level:
    subprocess.Popen([sys.executable, '-B', __file__, str(level - 1), str(count)])
handles = []
if count:
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.GetCurrentProcess.restype = w.HANDLE
    k.GetStdHandle.argtypes, k.GetStdHandle.restype = [w.DWORD], w.HANDLE
    k.DuplicateHandle.argtypes = [w.HANDLE,w.HANDLE,w.HANDLE,ctypes.POINTER(w.HANDLE),w.DWORD,w.BOOL,w.DWORD]
    k.DuplicateHandle.restype = w.BOOL
    current, stderr = k.GetCurrentProcess(), k.GetStdHandle(-12)
    for _ in range(count):
        copied = w.HANDLE()
        if not k.DuplicateHandle(current,stderr,current,ctypes.byref(copied),0,False,2):
            raise ctypes.WinError(ctypes.get_last_error())
        handles.append(copied.value)
Path('pid-' + str(level)).write_text(str(os.getpid()),encoding='ascii')
if level == 2:
    raise SystemExit(0)
while True:
    time.sleep(.1)
'''


def failure(exc):
    return {"type": type(exc).__name__, "message": str(exc), "winerror": getattr(exc, "winerror", None)}


def unlink_once(path):
    try:
        path.unlink()
        return {"deleted": True}
    except OSError as exc:
        return {"deleted": False, **failure(exc)}


def record_waits(api, handles, row, key, save, deadline=None):
    """Retain unexpected native wait failures before refusing interpretation."""
    states, errors = {}, {}
    row[key] = states
    for pid, handle in handles:
        milliseconds = 0 if deadline is None else max(0, int((deadline - time.monotonic()) * 1000))
        state = api.kernel.WaitForSingleObject(handle, milliseconds)
        states[str(pid)] = state
        if state not in (0, 258):
            errors[str(pid)] = {"wait_code": state, "winerror": ctypes.get_last_error()}
    if errors:
        row[key + "_errors"] = errors
        save()
        raise RuntimeError("Unexpected native process wait result: " + json.dumps(errors, sort_keys=True))
    return states


def process_identity(api, handle):
    w, kernel = api.w, api.kernel
    kernel.GetProcessId.argtypes, kernel.GetProcessId.restype = [w.HANDLE], w.DWORD
    kernel.GetProcessTimes.argtypes = [w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4
    kernel.GetProcessTimes.restype = w.BOOL
    created, exited, kernel_time, user_time = (w.FILETIME() for _ in range(4))
    api.check(kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                    ctypes.byref(kernel_time), ctypes.byref(user_time)))
    return {"pid": api.check(kernel.GetProcessId(handle)),
            "process_start_filetime": (created.dwHighDateTime << 32) | created.dwLowDateTime}


def locking_owners(path):
    """Two bounded RmGetList calls, at most 64 owners; no shutdown/restart APIs."""
    from ctypes import wintypes as w
    class Unique(ctypes.Structure):
        _fields_ = [("pid", w.DWORD), ("started", w.FILETIME)]
    class Info(ctypes.Structure):
        _fields_ = [("process", Unique), ("name", w.WCHAR * 256), ("service", w.WCHAR * 64),
                    ("kind", ctypes.c_int), ("status", w.ULONG), ("session", w.DWORD), ("restartable", w.BOOL)]
    api = ctypes.WinDLL("rstrtmgr", use_last_error=True)
    api.RmStartSession.argtypes = [ctypes.POINTER(w.DWORD), w.DWORD, w.LPWSTR]
    api.RmStartSession.restype = w.DWORD
    api.RmRegisterResources.argtypes = [w.DWORD, w.UINT, ctypes.POINTER(w.LPCWSTR),
                                      w.UINT, ctypes.POINTER(Unique), w.UINT, ctypes.POINTER(w.LPCWSTR)]
    api.RmRegisterResources.restype = w.DWORD
    api.RmGetList.argtypes = [w.DWORD, ctypes.POINTER(w.UINT), ctypes.POINTER(w.UINT),
                            ctypes.POINTER(Info), ctypes.POINTER(w.DWORD)]
    api.RmGetList.restype = w.DWORD
    api.RmEndSession.argtypes, api.RmEndSession.restype = [w.DWORD], w.DWORD
    session, key = w.DWORD(), ctypes.create_unicode_buffer(33)
    code = api.RmStartSession(ctypes.byref(session), 0, key)
    result = {"start_code": code, "scope": "Single fixture stderr path; query only; no shutdown/restart"}
    if code:
        return result
    try:
        names = (w.LPCWSTR * 1)(str(path))
        result["register_code"] = api.RmRegisterResources(session, 1, names, 0, None, 0, None)
        if result["register_code"]:
            return result
        needed, count, reasons = w.UINT(), w.UINT(), w.DWORD()
        code = api.RmGetList(session, ctypes.byref(needed), ctypes.byref(count), None, ctypes.byref(reasons))
        result.update(size_code=code, needed=needed.value, reboot_reasons=reasons.value)
        if code == 0:
            result["owners"] = []
        elif code == 234 and 0 < needed.value <= 64:
            rows = (Info * needed.value)(); count.value = needed.value
            code = api.RmGetList(session, ctypes.byref(needed), ctypes.byref(count), rows, ctypes.byref(reasons))
            result.update(list_code=code, needed=needed.value, returned=count.value)
            if code == 0:
                result["owners"] = [{"pid": row.process.pid,
                    "process_start_filetime": (row.process.started.dwHighDateTime << 32) | row.process.started.dwLowDateTime,
                    "name": row.name, "service": row.service, "kind": row.kind, "status": row.status}
                    for row in rows[:count.value]]
        return result
    finally:
        result["end_code"] = api.RmEndSession(session)


def case(native, root, row, save, duplicate_count):
    child, handles, stopped = None, [], False
    root.mkdir()
    script = root / "child.py"; script.write_text(CHILD, encoding="utf-8")
    environment = {name: os.environ[name] for name in ("SystemRoot", "WINDIR") if name in os.environ}
    environment.update(TEMP=str(root), TMP=str(root), PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
    try:
        with (root / "out").open("wb") as stdout, (root / "err").open("wb") as stderr:
            child = native.WindowsJobProcess([sys.executable, "-B", str(script), "2", str(duplicate_count)],
                cwd=str(root), env=environment, stdout=stdout, stderr=stderr, max_memory_bytes=256 * 1024 * 1024)
        row["root_pid"] = child.pid
        deadline = time.monotonic() + 10
        while True:
            try:
                pids = {int((root / ("pid-" + str(i))).read_text()) for i in range(3)}
                break
            except (FileNotFoundError, ValueError):
                if time.monotonic() >= deadline:
                    raise TimeoutError("Known fixture descendants did not become ready")
                time.sleep(.01)
        row["root_exit"] = child.wait(timeout=5)
        row["before_stop_job_pids"] = child.pids()
        if row["root_exit"] != 0 or not pids - {child.pid} <= set(row["before_stop_job_pids"]):
            raise RuntimeError("Fixture process ownership differs before stop")
        if len(row["before_stop_job_pids"]) > 64:
            raise RuntimeError("Diagnostic Job unexpectedly exceeds 64 members")
        row["retained_process_identities"] = []
        row["supervisor_identity"] = process_identity(child.api, child.api.kernel.GetCurrentProcess())
        for pid in sorted(set(row["before_stop_job_pids"]) - {child.pid}):
            handle = child.api.check(child.api.kernel.OpenProcess(0x100000 | 0x1000, False, pid))
            handles.append((pid, handle))
            belongs = child.api.w.BOOL()
            child.api.check(child.api.kernel.IsProcessInJob(handle, child._job, ctypes.byref(belongs)))
            if not belongs.value:
                raise RuntimeError("Selected process handle is not in the owned fixture Job")
            identity = process_identity(child.api, handle)
            if identity["pid"] != pid:
                raise RuntimeError("Retained handle PID differs from the selected Job member")
            row["retained_process_identities"].append(identity)
        row["retained_identity_checked_descendants"] = [pid for pid, _ in handles]
        save()
        started = time.monotonic()
        row["product_stop"] = child.stop(); stopped = True
        row["stop_seconds"] = time.monotonic() - started
        row["after_stop_job_pids"] = child.pids()
        record_waits(child.api, handles, row, "immediate_process_waits", save)
        record_waits(child.api, handles, row, "waits_before_unlink", save)
        row["unlink_before_wait"] = unlink_once(root / "err")
        record_waits(child.api, handles, row, "waits_after_unlink", save)
        save()
        row["file_lock_with_signaled_pre_unlink_handles"] = (row["unlink_before_wait"].get("winerror") == 32
            and all(state == 0 for state in row["waits_before_unlink"].values()))
        if row["file_lock_with_signaled_pre_unlink_handles"]:
            row["restart_manager"] = locking_owners(root / "err")
            for owner in row["restart_manager"].get("owners", []):
                observed = {key: owner[key] for key in ("pid", "process_start_filetime")}
                owner["matches_retained_process_identity"] = observed in row["retained_process_identities"]
                owner["matches_diagnostic_supervisor"] = observed == row["supervisor_identity"]
            save()
        deadline = time.monotonic() + 6
        record_waits(child.api, handles, row, "completed_process_waits", save, deadline)
        all_signaled = all(state == 0 for state in row["completed_process_waits"].values())
        row["unlink_after_wait"] = (unlink_once(root / "err") if not row["unlink_before_wait"]["deleted"]
                                    else {"not_needed": "The single before-wait attempt deleted the file"})
        row["all_known_descendants_signaled"] = all_signaled
        if row["product_stop"].get("confirmed_stopped") and any(state == 258 for state in row["immediate_process_waits"].values()):
            row["finding"] = "CONFIRMED_STOP_WITH_UNSIGNALED_DESCENDANT"
        elif row["file_lock_with_signaled_pre_unlink_handles"]:
            row["finding"] = "FILE_LOCK_WITH_SIGNALED_KNOWN_DESCENDANTS"
        else:
            row["finding"] = "NO_GAP_OBSERVED"
        if not all_signaled or not row["product_stop"].get("confirmed_stopped"):
            raise RuntimeError("A required stop or per-process wait was not confirmed")
        if not row["unlink_before_wait"]["deleted"] and not row["unlink_after_wait"].get("deleted"):
            raise RuntimeError("The one after-wait deletion still failed; preserve the fixture")
        row["cleanup_permitted"] = True
        save()
    finally:
        if child is not None:
            try:
                if not stopped:
                    row["exception_cleanup_stop"] = child.stop()
                    for pid, handle in handles:
                        child.api.kernel.WaitForSingleObject(handle, 6000)
            finally:
                child.close()
                for _, handle in handles:
                    child.api.kernel.CloseHandle(handle)
        row["fixture_files"] = {}
        for path in root.iterdir():
            if not path.is_file():
                continue
            try:
                raw = path.read_bytes()
                details = {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
                if path.name == "err":
                    details["bounded_stderr_utf8"] = raw[:16384].decode("utf-8", errors="replace")
                row["fixture_files"][path.name] = details
            except OSError as exc:
                row["fixture_files"][path.name] = {"read_error": failure(exc)}
        # One final cleanup attempt: preserve and report any remaining failure.
        if row.get("cleanup_permitted"):
            try:
                shutil.rmtree(root)
                row["fixture_removed"] = True
            except OSError as exc:
                row["fixture_removed"] = False
                row["cleanup_error"] = failure(exc)
                raise
        else:
            row["fixture_removed"] = False
            row["preserved_fixture"] = str(root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-head", required=True)
    args = parser.parse_args()
    if (sys.platform != "win32" or os.environ.get("GITHUB_ACTIONS") != "true"
            or os.environ.get("RUNNER_OS") != "Windows" or os.environ.get("REPOSITORY_PRIVATE") != "false"
            or os.environ.get("GITHUB_REPOSITORY") != "zacheriasliang-glitch/lodestone"):
        raise SystemExit("Diagnostic requires the explicitly selected public Windows runner")
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=args.source, text=True).strip()
    if commit != args.expected_head or commit != os.environ.get("GITHUB_SHA") or args.output.exists():
        raise SystemExit("Exact source head and a new diagnostic output are required")
    script = args.source / "desktop/runtime/src/lodestone_machinery/portable_process.py"
    spec = importlib.util.spec_from_file_location("diagnostic_native_process", script)
    native = importlib.util.module_from_spec(spec); spec.loader.exec_module(native)
    report = {"schema": "lodestone-windows-stop-diagnostic/v1", "source_commit": commit,
              "runtime_source_sha256": hashlib.sha256(script.read_bytes()).hexdigest(), "cases": [],
              "status": "RUNNING", "model_calls": 0, "product_modified": False,
              "scope": "Diagnostic only. No Job-handle completion inference, installation or model call."}
    def save():
        pending = args.output.with_suffix(".pending")
        with pending.open("w", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2, sort_keys=True); stream.write("\n")
            stream.flush(); os.fsync(stream.fileno())
        os.replace(pending, args.output)
    fixture = Path(tempfile.mkdtemp(prefix="lodestone-stop-diagnostic-", dir=os.environ["RUNNER_TEMP"]))
    report["owned_fixture_root"] = str(fixture)
    save(); deadline = time.monotonic() + 180
    try:
        # One bounded experiment, not a rerun-until-green loop. End at the first
        # discrepancy and retain every preceding outcome.
        for index in range(40):
            if time.monotonic() >= deadline:
                raise TimeoutError("Diagnostic reached its 180-second work bound")
            duplicates = 0 if index < 20 else 8192
            row = {"index": index, "duplicate_stderr_handles_per_process": duplicates}
            report["cases"].append(row); save()
            case(native, fixture / str(index), row, save, duplicates)
            print(json.dumps(row), flush=True)
            if row["finding"] != "NO_GAP_OBSERVED":
                report["status"] = row["finding"]
                break
        else:
            report["status"] = "NO_GAP_OBSERVED_IN_BOUNDED_EXPERIMENT"
    except BaseException as exc:
        report.update(status="DIAGNOSTIC_ERROR", error=failure(exc), traceback=traceback.format_exc())
    finally:
        try:
            fixture.rmdir()
            report["owned_fixture_root_removed"] = True
        except OSError as exc:
            report["owned_fixture_root_removed"] = False
            report["fixture_root_cleanup_error"] = failure(exc)
        save()
    print(json.dumps({"status": report["status"], "cases": len(report["cases"]), "output": str(args.output)}), flush=True)
    return 0 if report["status"] == "NO_GAP_OBSERVED_IN_BOUNDED_EXPERIMENT" else 1


if __name__ == "__main__":
    raise SystemExit(main())
