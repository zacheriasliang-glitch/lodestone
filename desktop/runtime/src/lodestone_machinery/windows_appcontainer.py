"""Windows kernel fence: run one command inside a fresh AppContainer.

Used by isolation.py as this platform's fence; runs as a small launcher process
outside the container so callers keep their ordinary argv/stdio contract:

    python -I -B windows_appcontainer.py --workspace DIR [--read PATH ...] -- PROGRAM ARGS...

Each run creates a uniquely named AppContainer profile with no capabilities
(no network, no loopback), grants that container's SID modify access to the
workspace and read/execute access to the declared paths, starts the command
suspended inside the container and inside a kill-on-close Job, resumes it, waits,
and returns its exit code. Afterwards the grants are removed and the profile is
deleted. Anything that cannot be set up is refused; nothing runs unconfined.

Limits, stated: programs inside the container may start further programs inside
the same container (recorded as DECLARED, not ENFORCED); Windows system
directories readable by "ALL APPLICATION PACKAGES" remain readable, as system
prefixes are under the macOS and Linux fences.

Win32 contracts: learn.microsoft.com/windows/win32/secauthz/implementing-an-appcontainer
and /api/userenv/nf-userenv-createappcontainerprofile.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import uuid

PREFIX = "Lodestone.Fence."
EXIT_SETUP_FAILED = 125


def _ledger_directory():
    base = os.environ.get("LOCALAPPDATA")
    if not base or not Path(base).is_absolute():
        raise OSError("LOCALAPPDATA is required for the fence's recovery ledger")
    directory = Path(base) / "Lodestone Fence" / "pending"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _no_reparse(path):
    """Refuse a path that is, or passes through, a junction/symlink/reparse point."""
    path = Path(os.path.abspath(path))
    for item in (path, *path.parents):
        info = os.lstat(item)
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Fence paths may not pass through a reparse point: " + str(item))
    return path


def _icacls():
    return str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "icacls.exe")


class _Api:
    def __init__(self):
        import ctypes
        from ctypes import wintypes as w
        if sys.platform != "win32" or ctypes.sizeof(ctypes.c_void_p) != 8:
            raise OSError("The AppContainer fence requires 64-bit Windows")
        self.c, self.w = ctypes, w
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.userenv = ctypes.WinDLL("userenv", use_last_error=True)
        self.advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        p, size = ctypes.c_void_p, ctypes.c_size_t
        for lib, name, args, result in (
            (self.userenv, "CreateAppContainerProfile", [w.LPCWSTR, w.LPCWSTR, w.LPCWSTR, p, w.DWORD, ctypes.POINTER(p)], ctypes.c_long),
            (self.userenv, "DeleteAppContainerProfile", [w.LPCWSTR], ctypes.c_long),
            (self.advapi, "ConvertSidToStringSidW", [p, ctypes.POINTER(w.LPWSTR)], w.BOOL),
            (self.advapi, "FreeSid", [p], p),
            (self.kernel, "LocalFree", [p], p),
            (self.kernel, "GetStdHandle", [w.DWORD], w.HANDLE),
            (self.kernel, "GetCurrentProcess", [], w.HANDLE),
            (self.kernel, "DuplicateHandle", [w.HANDLE, w.HANDLE, w.HANDLE, p, w.DWORD, w.BOOL, w.DWORD], w.BOOL),
            (self.kernel, "InitializeProcThreadAttributeList", [p, w.DWORD, w.DWORD, p], w.BOOL),
            (self.kernel, "UpdateProcThreadAttribute", [p, w.DWORD, size, p, size, p, p], w.BOOL),
            (self.kernel, "DeleteProcThreadAttributeList", [p], None),
            (self.kernel, "CreateProcessW", [w.LPCWSTR, w.LPWSTR, p, p, w.BOOL, w.DWORD, p, w.LPCWSTR, p, p], w.BOOL),
            (self.kernel, "CreateJobObjectW", [p, w.LPCWSTR], w.HANDLE),
            (self.kernel, "SetInformationJobObject", [w.HANDLE, ctypes.c_int, p, w.DWORD], w.BOOL),
            (self.kernel, "AssignProcessToJobObject", [w.HANDLE, w.HANDLE], w.BOOL),
            (self.kernel, "ResumeThread", [w.HANDLE], w.DWORD),
            (self.kernel, "TerminateProcess", [w.HANDLE, w.UINT], w.BOOL),
            (self.kernel, "WaitForSingleObject", [w.HANDLE, w.DWORD], w.DWORD),
            (self.kernel, "GetExitCodeProcess", [w.HANDLE, p], w.BOOL),
            (self.kernel, "CloseHandle", [w.HANDLE], w.BOOL)):
            fn = getattr(lib, name)
            fn.argtypes, fn.restype = args, result

    def check(self, value):
        if not value:
            raise self.c.WinError(self.c.get_last_error())
        return value


def _grant(path, sid, rights):
    path = Path(path)
    spec = "*" + sid + ":" + ("(OI)(CI)" if path.is_dir() else "") + rights
    subprocess.run([_icacls(), str(path), "/grant", spec], check=True, timeout=600,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _revoke(path, sid):
    subprocess.run([_icacls(), str(path), "/remove:g", "*" + sid], check=False, timeout=600,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _write_ledger(handle, record):
    handle.seek(0)
    handle.truncate()
    handle.write(json.dumps(record, sort_keys=True).encode("utf-8"))
    handle.flush()
    os.fsync(handle.fileno())


def sweep(api=None):
    """Undo grants and profiles left by an interrupted launcher. Live runs hold their ledger lock."""
    import msvcrt
    api = api or _Api()
    for entry in sorted(_ledger_directory().glob(PREFIX + "*.json")):
        try:
            handle = open(entry, "r+b")
        except OSError:
            continue
        try:
            try:
                handle.seek(0)  # msvcrt.locking locks from the current position
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                continue  # A live launcher still owns this run.
            record = json.loads(handle.read().decode("utf-8") or "{}")
            for item in record.get("granted", []):
                if record.get("sid"):
                    _revoke(item, record["sid"])
            if record.get("name", "").startswith(PREFIX):
                api.userenv.DeleteAppContainerProfile(record["name"])
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            handle.close()
        entry.unlink(missing_ok=True)


def run(argv, *, workspace, readable):
    import msvcrt
    api = _Api()
    c, w, k = api.c, api.w, api.kernel
    sweep(api)
    workspace = _no_reparse(Path(workspace).resolve(strict=True))
    readable = [_no_reparse(Path(item).resolve(strict=True)) for item in readable]
    name = PREFIX + uuid.uuid4().hex
    # The ledger exists and is locked before any host state changes, so an
    # interrupted run is undone by the next sweep rather than left in place.
    ledger_path = _ledger_directory() / (name + ".json")
    ledger = open(ledger_path, "x+b")
    msvcrt.locking(ledger.fileno(), msvcrt.LK_NBLCK, 1)
    record = {"name": name, "sid": None, "granted": []}
    _write_ledger(ledger, record)
    sid_pointer = c.c_void_p()
    result = api.userenv.CreateAppContainerProfile(name, name, "Lodestone per-run command fence", None, 0,
                                                   c.byref(sid_pointer))
    if result != 0:
        ledger.close()
        ledger_path.unlink(missing_ok=True)
        raise OSError("CreateAppContainerProfile failed: HRESULT 0x%08x" % (result & 0xFFFFFFFF))
    granted, job, process, sid = [], None, None, None
    try:
        text = w.LPWSTR()
        api.check(api.advapi.ConvertSidToStringSidW(sid_pointer, c.byref(text)))
        sid = text.value
        k.LocalFree(text)
        record["sid"] = sid
        for item, rights in [(workspace, "(M)")] + [(item, "(RX)") for item in readable]:
            # Record before granting: a crash between the two leaves a revocable entry.
            record["granted"].append(str(item))
            _write_ledger(ledger, record)
            _grant(item, sid, rights)
            granted.append(item)

        class Capabilities(c.Structure):
            _fields_ = [("AppContainerSid", c.c_void_p), ("Capabilities", c.c_void_p),
                        ("CapabilityCount", w.DWORD), ("Reserved", w.DWORD)]

        class Startup(c.Structure):
            _fields_ = [("cb", w.DWORD), ("lpReserved", w.LPWSTR), ("lpDesktop", w.LPWSTR), ("lpTitle", w.LPWSTR)]
            _fields_ += [(field, w.DWORD) for field in ("dwX", "dwY", "dwXSize", "dwYSize",
                         "dwXCountChars", "dwYCountChars", "dwFillAttribute", "dwFlags")]
            _fields_ += [("wShowWindow", w.WORD), ("cbReserved2", w.WORD), ("lpReserved2", c.c_void_p),
                         ("hStdInput", w.HANDLE), ("hStdOutput", w.HANDLE), ("hStdError", w.HANDLE)]

        class StartupEx(c.Structure):
            _fields_ = [("StartupInfo", Startup), ("lpAttributeList", c.c_void_p)]

        class ProcessInfo(c.Structure):
            _fields_ = [("hProcess", w.HANDLE), ("hThread", w.HANDLE),
                        ("dwProcessId", w.DWORD), ("dwThreadId", w.DWORD)]

        class BasicLimits(c.Structure):
            _fields_ = [("PerProcessUserTimeLimit", c.c_int64), ("PerJobUserTimeLimit", c.c_int64),
                        ("LimitFlags", c.c_uint32), ("MinimumWorkingSetSize", c.c_size_t),
                        ("MaximumWorkingSetSize", c.c_size_t), ("ActiveProcessLimit", c.c_uint32),
                        ("Affinity", c.c_size_t), ("PriorityClass", c.c_uint32), ("SchedulingClass", c.c_uint32)]

        class ExtendedLimits(c.Structure):
            _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", c.c_uint64 * 6),
                        ("ProcessMemoryLimit", c.c_size_t), ("JobMemoryLimit", c.c_size_t),
                        ("PeakProcessMemoryUsed", c.c_size_t), ("PeakJobMemoryUsed", c.c_size_t)]

        job = api.check(k.CreateJobObjectW(None, None))
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE; no breakaway
        api.check(k.SetInformationJobObject(job, 9, c.byref(limits), c.sizeof(limits)))

        capabilities = Capabilities(sid_pointer, None, 0, 0)
        duplicates = []
        for standard in (-10, -11, -12):
            copied = w.HANDLE()
            source = k.GetStdHandle(standard & 0xFFFFFFFF)
            api.check(source and source != c.c_void_p(-1).value)
            api.check(k.DuplicateHandle(k.GetCurrentProcess(), source, k.GetCurrentProcess(),
                                        c.byref(copied), 0, True, 2))
            duplicates.append(copied.value)
        attributes, initialized = None, False
        info = ProcessInfo()
        try:
            size = c.c_size_t()
            k.InitializeProcThreadAttributeList(None, 2, 0, c.byref(size))
            attributes = c.create_string_buffer(size.value)
            api.check(k.InitializeProcThreadAttributeList(attributes, 2, 0, c.byref(size)))
            initialized = True
            api.check(k.UpdateProcThreadAttribute(attributes, 0, 0x20009, c.byref(capabilities),
                                                  c.sizeof(capabilities), None, None))  # SECURITY_CAPABILITIES
            handles = (w.HANDLE * 3)(*duplicates)
            api.check(k.UpdateProcThreadAttribute(attributes, 0, 0x20002, handles,
                                                  c.sizeof(handles), None, None))  # HANDLE_LIST
            startup = StartupEx()
            startup.StartupInfo.cb = c.sizeof(startup)
            startup.StartupInfo.dwFlags = 0x100  # STARTF_USESTDHANDLES
            startup.StartupInfo.hStdInput, startup.StartupInfo.hStdOutput, startup.StartupInfo.hStdError = duplicates
            startup.lpAttributeList = c.addressof(attributes)
            # Temporary files and caches stay inside the one writable place.
            env = {key: os.environ[key] for key in ("SystemRoot", "WINDIR", "PATH", "LANG") if key in os.environ}
            env.update({"TEMP": str(workspace), "TMP": str(workspace), "USERPROFILE": str(workspace),
                        "HOME": str(workspace), "PYTHONDONTWRITEBYTECODE": "1"})
            block = c.create_unicode_buffer("\x00".join(key + "=" + value for key, value in
                                            sorted(env.items(), key=lambda row: row[0].upper())) + "\x00")
            # SUSPENDED | UNICODE_ENVIRONMENT | EXTENDED_STARTUPINFO_PRESENT
            api.check(k.CreateProcessW(str(argv[0]), c.create_unicode_buffer(subprocess.list2cmdline(argv)),
                                       None, None, True, 0x4 | 0x400 | 0x80000, block, str(workspace),
                                       c.byref(startup), c.byref(info)))
            process = info.hProcess
            api.check(k.AssignProcessToJobObject(job, process))
            if k.ResumeThread(info.hThread) == 0xFFFFFFFF:
                raise c.WinError(c.get_last_error())
        finally:
            if info.hThread:
                k.CloseHandle(info.hThread)
            if initialized:
                k.DeleteProcThreadAttributeList(attributes)
            for handle in duplicates:
                k.CloseHandle(handle)
        k.WaitForSingleObject(process, 0xFFFFFFFF)
        code = w.DWORD()
        api.check(k.GetExitCodeProcess(process, c.byref(code)))
        return code.value
    except BaseException:
        if process:
            k.TerminateProcess(process, 1)
        raise
    finally:
        if process:
            k.CloseHandle(process)
        if job:
            k.CloseHandle(job)  # kill-on-close: no descendant outlives the run
        failures = []
        for item in record["granted"]:
            if subprocess.run([_icacls(), item, "/remove:g", "*" + sid], check=False, timeout=600,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
                failures.append(item)
        api.advapi.FreeSid(sid_pointer)
        deleted = api.userenv.DeleteAppContainerProfile(name) == 0
        ledger.seek(0)
        msvcrt.locking(ledger.fileno(), msvcrt.LK_UNLCK, 1)
        ledger.close()
        if not failures and deleted:
            ledger_path.unlink(missing_ok=True)  # otherwise the next sweep retries


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--read", action="append", default=[])
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(arguments)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command or not Path(command[0]).is_absolute():
        print("An absolute program path is required inside the fence", file=sys.stderr)
        return EXIT_SETUP_FAILED
    try:
        return run(command, workspace=args.workspace, readable=args.read)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print("Windows AppContainer fence refused: " + type(exc).__name__ + ": " + str(exc), file=sys.stderr)
        return EXIT_SETUP_FAILED


if __name__ == "__main__":
    sys.exit(main())
