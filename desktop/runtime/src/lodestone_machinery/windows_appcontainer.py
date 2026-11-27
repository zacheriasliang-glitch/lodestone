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

Host assumption, stated: the launcher checks each granted path as written for
reparse points and rechecks its file identity before the grant, after the grant,
before launch and before restore, but it does not hold directory handles for
the whole run. It is built for a trusted host directory tree; it does not claim
race-proof custody against a hostile process that can rename the user's own
directories between those checks.

Win32 contracts: learn.microsoft.com/windows/win32/secauthz/implementing-an-appcontainer
and /api/userenv/nf-userenv-createappcontainerprofile.
"""
from __future__ import annotations

import argparse
import json
import re
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
import uuid

PREFIX = "Lodestone.Fence."
EXIT_SETUP_FAILED = 125
EXIT_CLEANUP_UNCONFIRMED = 126


def _no_reparse(path):
    """Refuse a path that is, or passes through, a junction/symlink/reparse point."""
    path = Path(os.path.abspath(path))
    for item in (path, *path.parents):
        info = os.lstat(item)
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Fence paths may not pass through a reparse point: " + str(item))
    return path


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


FILE_OBJECT, DACL_INFO = 1, 0x4
PROTECTED_DACL, UNPROTECTED_DACL, SE_DACL_PROTECTED = 0x80000000, 0x20000000, 0x1000
MODIFY, READ_EXECUTE = "0x1301bf", "0x1200a9"


class _Security:
    """Exact DACL save/restore. icacls-style edits can drop DACL protection,
    silently re-enabling inherited access; restoring the saved SDDL cannot."""

    def __init__(self, api):
        c, w = api.c, api.w
        self.api, self.c = api, c
        a = api.advapi
        p = c.c_void_p
        for name, args, result in (
            ("GetNamedSecurityInfoW", [w.LPCWSTR, c.c_int, w.DWORD, p, p, c.POINTER(p), p, c.POINTER(p)], w.DWORD),
            ("SetNamedSecurityInfoW", [w.LPWSTR, c.c_int, w.DWORD, p, p, p, p], w.DWORD),
            ("ConvertSecurityDescriptorToStringSecurityDescriptorW", [p, w.DWORD, w.DWORD, c.POINTER(w.LPWSTR), p], w.BOOL),
            ("ConvertStringSecurityDescriptorToSecurityDescriptorW", [w.LPCWSTR, w.DWORD, c.POINTER(p), p], w.BOOL),
            ("GetSecurityDescriptorDacl", [p, c.POINTER(w.BOOL), c.POINTER(p), c.POINTER(w.BOOL)], w.BOOL),
            ("GetSecurityDescriptorControl", [p, c.POINTER(w.WORD), c.POINTER(w.DWORD)], w.BOOL),
            ("SetFileSecurityW", [w.LPCWSTR, w.DWORD, p], w.BOOL)):
            fn = getattr(a, name)
            fn.argtypes, fn.restype = args, result

    def read(self, path):
        c, a, k = self.c, self.api.advapi, self.api.kernel
        dacl, descriptor = c.c_void_p(), c.c_void_p()
        error = a.GetNamedSecurityInfoW(str(path), FILE_OBJECT, DACL_INFO, None, None, c.byref(dacl), None,
                                        c.byref(descriptor))
        if error:
            raise c.WinError(error)
        try:
            text = self.api.w.LPWSTR()
            self.api.check(a.ConvertSecurityDescriptorToStringSecurityDescriptorW(descriptor, 1, DACL_INFO,
                                                                                    c.byref(text), None))
            value = text.value
            k.LocalFree(text)
            return value
        finally:
            k.LocalFree(descriptor)

    def write(self, path, sddl):
        c, w, a, k = self.c, self.api.w, self.api.advapi, self.api.kernel
        descriptor = c.c_void_p()
        self.api.check(a.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, c.byref(descriptor), None))
        try:
            present, defaulted, dacl = w.BOOL(), w.BOOL(), c.c_void_p()
            self.api.check(a.GetSecurityDescriptorDacl(descriptor, c.byref(present), c.byref(dacl), c.byref(defaulted)))
            control, revision = w.WORD(), w.DWORD()
            self.api.check(a.GetSecurityDescriptorControl(descriptor, c.byref(control), c.byref(revision)))
            flags = DACL_INFO | (PROTECTED_DACL if control.value & SE_DACL_PROTECTED else UNPROTECTED_DACL)
            error = a.SetNamedSecurityInfoW(str(path), FILE_OBJECT, flags, None, None, dacl, None)
            if error:
                raise c.WinError(error)
        finally:
            k.LocalFree(descriptor)


    def restore(self, path, original, sid):
        """Undo one grant exactly: strip our ACE with propagation (clearing the
        copies descendants inherited), then write the saved DACL verbatim,
        because SetNamedSecurityInfoW may re-express explicit ACEs as inherited."""
        current = self.read(path)
        if sid:
            self.write(path, _without_sid(current, sid))
        c, a, k = self.c, self.api.advapi, self.api.kernel
        descriptor = c.c_void_p()
        self.api.check(a.ConvertStringSecurityDescriptorToSecurityDescriptorW(original, 1, c.byref(descriptor), None))
        try:
            self.api.check(a.SetFileSecurityW(str(path), DACL_INFO, descriptor))
        finally:
            k.LocalFree(descriptor)


def _without_sid(sddl, sid):
    head, _, rest = sddl.partition("(")
    aces = re.findall(r"\([^)]*\)", "(" + rest) if rest else []
    return head + "".join(ace for ace in aces if not (ace.endswith(";" + sid + ")") and "ID" not in ace.split(";")[1]))


def _protected(sddl):
    head = sddl.partition("(")[0]
    return "P" in head[2:]


def _aces(sddl):
    _, _, rest = sddl.partition("(")
    return re.findall(r"\([^)]*\)", "(" + rest) if rest else []


def _with_ace(sddl, sid, mask, directory):
    """Insert one explicit allow ACE after the explicit ACEs (canonical order)."""
    head, _, rest = sddl.partition("(")
    aces = re.findall(r"\([^)]*\)", "(" + rest) if rest else []
    if not head.startswith("D:") or "".join(aces) != ("(" + rest if rest else ""):
        raise ValueError("Unexpected DACL format; refusing to edit it")
    explicit = [ace for ace in aces if "ID" not in ace.split(";")[1]]
    inherited = aces[len(explicit):]
    if explicit + inherited != aces:
        raise ValueError("DACL is not in canonical order; refusing to edit it")
    ace = "(A;" + ("OICI" if directory else "") + ";" + mask + ";;;" + sid + ")"
    return head + "".join(explicit) + ace + "".join(inherited)


def _fence_root():
    base = os.environ.get("LOCALAPPDATA")
    if not base or not os.path.isabs(base):
        raise OSError("LOCALAPPDATA is required for the fence's recovery journal")
    root = _lexical(Path(base) / "Lodestone Fence", create=True)
    pending = root / "pending"
    pending.mkdir(exist_ok=True)
    _no_reparse(pending)
    return root


def _ledger_directory():
    return _fence_root() / "pending"


def _lexical(path, *, create=False):
    """Validate the path as written, before any resolution can erase a junction.

    Windows silently drops trailing dots/spaces and treats ':' as a stream
    separator; such components are refused rather than normalized.
    """
    text = str(path)
    drive, rest = os.path.splitdrive(text)
    if len(drive) != 2 or drive[1] != ":" or not os.path.isabs(text):
        raise ValueError("A local absolute drive path is required: " + text)
    for part in rest.replace("/", "\\").split("\\"):
        if part in (".", ".."):
            raise ValueError("Relative path components are not admitted: " + text)
        if part and (part[-1] in ". " or ":" in part):
            raise ValueError("Ambiguous Windows path component is not admitted: " + text)
    lexical = Path(os.path.normpath(text))
    if create:
        lexical.mkdir(parents=True, exist_ok=True)
    return _no_reparse(lexical)


def _identity(path):
    info = os.lstat(path)
    return [info.st_dev, info.st_ino]


class _Journal:
    """Append-only, fsynced recovery journal. Earlier entries are never rewritten,
    so an interruption at any point leaves every saved original DACL intact.
    A torn final line (no newline) is ignored: it was written before its mutation."""

    def __init__(self, path, *, create):
        self.path = path
        self.handle = open(path, "xb" if create else "ab")

    def append(self, entry):
        self.handle.write(json.dumps(entry, sort_keys=True).encode("utf-8") + b"\n")
        self.handle.flush()
        os.fsync(self.handle.fileno())

    def close(self):
        self.handle.close()

    @staticmethod
    def entries(path):
        raw = path.read_bytes()
        lines = raw.split(b"\n")
        return [json.loads(line) for line in lines[:-1] if line.strip()]


class _GlobalLock:
    """Serializes every ACL-mutating fence run and recovery for this user, so two
    runs never snapshot or restore the same DACL concurrently. Bounded wait."""

    def __init__(self, timeout):
        import msvcrt
        self.msvcrt = msvcrt
        self.handle = open(_fence_root() / "fence.lock", "a+b")
        deadline = time.monotonic() + timeout
        while True:
            try:
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    self.handle.close()
                    raise OSError("Another fenced command holds the Windows fence; refused after waiting")
                time.sleep(0.25)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.handle.seek(0)
        self.msvcrt.locking(self.handle.fileno(), self.msvcrt.LK_UNLCK, 1)
        self.handle.close()


def _wait_seconds():
    try:
        return max(0.0, float(os.environ.get("LODESTONE_FENCE_WAIT_SECONDS", "600")))
    except ValueError:
        return 600.0


def _undo(security, entries, sid, *, inject_failure=False):
    """Undo this run's grants; returns the paths whose cleanup is unconfirmed."""
    grants, expected, done = [], {}, set()
    for entry in entries:
        if "grant" in entry:
            grants.append(entry)
        elif "granted" in entry:
            expected[entry["granted"]] = entry["expected"]
        elif "restored" in entry:
            done.add(entry["restored"])
    unconfirmed = []
    for entry in reversed(grants):
        path = entry["grant"]
        if path in done:
            continue
        try:
            if inject_failure:
                raise OSError("injected restore failure (test hook)")
            try:
                os.lstat(path)
            except FileNotFoundError:
                # The object no longer exists, so no Lodestone entry remains on it.
                print("Fence restore for " + path + ": path_gone", file=sys.stderr)
                continue
            _lexical(path)
            if _identity(path) != entry["id"]:
                raise ValueError("Path identity changed during the run; its DACL is left for inspection")
            current = security.read(path)
            stripped = _without_sid(current, sid) if sid else current
            # Exact restore only when nothing but Lodestone's ACE differs: same
            # remaining ACEs and the same protection flag. (SetNamedSecurityInfoW
            # itself may set the AI flag, so that bit alone is not a user edit.)
            only_ours = (_aces(stripped) == _aces(entry["sddl"])
                         and _protected(stripped) == _protected(entry["sddl"]))
            if (path in expected and current == expected[path]) or only_ours:
                # Only Lodestone's ACE differs from the saved original (including a
                # run interrupted before confirming its grant): restore it exactly.
                security.restore(path, entry["sddl"], sid)
                mode = "exact"
            elif sid and _without_sid(current, sid) != current:
                # Someone edited the DACL meanwhile (or we crashed before
                # confirming): remove only our ACE and keep their edits.
                security.write(path, _without_sid(current, sid))
                mode = "removed_own_entry_only"
            else:
                mode = "nothing_to_undo"
            after = security.read(path)
            if sid and _without_sid(after, sid) != after:
                raise OSError("Lodestone's entry is still present after restore")
            if mode != "exact" and mode != "nothing_to_undo":
                print("Fence restore for " + path + ": " + mode, file=sys.stderr)
        except (OSError, ValueError) as exc:
            print("Fence grant restore failed for " + path + ": " + str(exc), file=sys.stderr)
            unconfirmed.append(path)
    return unconfirmed


def _delete_profile(api, name):
    result = api.userenv.DeleteAppContainerProfile(name) & 0xFFFFFFFF
    return result in (0, 0x80070002, 0x80070490)  # deleted, or already absent


def _sweep(api):
    """Returns how many interrupted runs remain unrecovered (their journals kept)."""
    unresolved = 0
    security = _Security(api)
    for journal in sorted(_ledger_directory().glob(PREFIX + "*.journal")):
        entries = _Journal.entries(journal)
        header = next((e for e in entries if "profile" in e), {})
        sid = next((e["sid"] for e in entries if "sid" in e), None)
        unconfirmed = _undo(security, list(entries), sid)
        name = header.get("profile", "")
        deleted = _delete_profile(api, name) if name.startswith(PREFIX) else True
        if not unconfirmed and deleted:
            journal.unlink()
        else:
            unresolved += 1
            print("Fence recovery kept " + journal.name + " for inspection", file=sys.stderr)
    return unresolved


def sweep(api=None):
    """Undo grants and profiles left by interrupted launchers (under the global lock)."""
    api = api or _Api()
    with _GlobalLock(_wait_seconds()):
        _sweep(api)


def run(argv, *, workspace, readable):
    api = _Api()
    c, w, k = api.c, api.w, api.kernel
    workspace = _lexical(workspace)
    # One grant per object: an 8.3 short name and its long name are the same
    # directory, and granting it twice would make one restore resurrect the other.
    seen, unique = {tuple(_identity(workspace))}, []
    for item in readable:
        path = _lexical(item)
        identity = tuple(_identity(path))
        if identity not in seen:
            seen.add(identity)
            unique.append(path)
    readable = unique
    with _GlobalLock(_wait_seconds()):
        if _sweep(api):
            # Never stack new grants on top of an unrecovered earlier run.
            raise OSError("An earlier fenced run's recovery is unresolved; its journal is kept for "
                          "inspection and sweep can be retried. New fenced runs are refused until then.")
        return _run_locked(api, c, w, k, argv, workspace, readable)


def _run_locked(api, c, w, k, argv, workspace, readable):
    name = PREFIX + uuid.uuid4().hex
    journal = _Journal(_ledger_directory() / (name + ".journal"), create=True)
    entries = [{"profile": name}]
    journal.append(entries[0])
    sid_pointer = c.c_void_p()
    result = api.userenv.CreateAppContainerProfile(name, name, "Lodestone per-run command fence", None, 0,
                                                   c.byref(sid_pointer))
    if result != 0:
        journal.close()
        (_ledger_directory() / (name + ".journal")).unlink()
        raise OSError("CreateAppContainerProfile failed: HRESULT 0x%08x" % (result & 0xFFFFFFFF))
    job, process, sid, code = None, None, None, None
    crash_after = os.environ.get("LODESTONE_FENCE_TEST_CRASH_AFTER_FIRST_GRANT") == "1"
    try:
        text = w.LPWSTR()
        api.check(api.advapi.ConvertSidToStringSidW(sid_pointer, c.byref(text)))
        sid = text.value
        k.LocalFree(text)
        entries.append({"sid": sid})
        journal.append(entries[-1])
        security = _Security(api)
        for item, mask in [(workspace, MODIFY)] + [(item, READ_EXECUTE) for item in readable]:
            identity = _identity(item)
            original = security.read(item)
            # Appended (never rewritten) before the change it describes.
            entries.append({"grant": str(item), "sddl": original, "id": identity})
            journal.append(entries[-1])
            _lexical(item)
            if _identity(item) != identity:
                raise ValueError("Path identity changed before its grant: " + str(item))
            security.write(item, _with_ace(original, sid, mask, item.is_dir()))
            if crash_after:
                os._exit(99)  # test hook: interruption after one real ACL mutation
            entries.append({"granted": str(item), "expected": security.read(item)})
            journal.append(entries[-1])
            if _identity(item) != identity:
                raise ValueError("Path identity changed during its grant: " + str(item))
        for entry in entries:
            if "grant" in entry and _identity(entry["grant"]) != entry["id"]:
                raise ValueError("Path identity changed before launch: " + entry["grant"])

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
        import msvcrt
        duplicates, null_streams = [], []
        for standard, mode in ((-10, "rb"), (-11, "wb"), (-12, "wb")):
            copied = w.HANDLE()
            source = k.GetStdHandle(standard & 0xFFFFFFFF)
            if not source or source == c.c_void_p(-1).value:
                # A launcher started without this stream (for example by a
                # service) gives the child the null device, never a stale handle.
                stream = open(os.devnull, mode)
                null_streams.append(stream)
                source = msvcrt.get_osfhandle(stream.fileno())
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
            # Windows derives the container's own data folder from LOCALAPPDATA;
            # CreateProcessW fails with ERROR_ENVVAR_NOT_FOUND without it.
            env = {key: os.environ[key] for key in ("SystemRoot", "WINDIR", "PATH", "LANG", "LOCALAPPDATA", "APPDATA")
                   if key in os.environ}
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
            for stream in null_streams:
                stream.close()
        k.WaitForSingleObject(process, 0xFFFFFFFF)
        exit_code = w.DWORD()
        api.check(k.GetExitCodeProcess(process, c.byref(exit_code)))
        code = exit_code.value
    except BaseException:
        if process:
            k.TerminateProcess(process, 1)
        raise
    finally:
        if process:
            k.CloseHandle(process)
        if job:
            k.CloseHandle(job)  # kill-on-close: no descendant outlives the run
        inject = os.environ.get("LODESTONE_FENCE_TEST_FAIL_RESTORE") == "1"
        unconfirmed = _undo(_Security(api), list(entries), sid, inject_failure=inject)
        api.advapi.FreeSid(sid_pointer)
        deleted = _delete_profile(api, name)
        journal.close()
        clean = not unconfirmed and deleted
        if clean:
            (_ledger_directory() / (name + ".journal")).unlink()
    if not clean:
        print("Windows fence cleanup unconfirmed (command exit code %s); recovery journal kept" % code,
              file=sys.stderr)
        return EXIT_CLEANUP_UNCONFIRMED
    return code


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
        import traceback
        print("Windows AppContainer fence refused: " + type(exc).__name__ + ": " + str(exc), file=sys.stderr)
        traceback.print_exc(limit=4, file=sys.stderr)
        return EXIT_SETUP_FAILED


if __name__ == "__main__":
    sys.exit(main())
