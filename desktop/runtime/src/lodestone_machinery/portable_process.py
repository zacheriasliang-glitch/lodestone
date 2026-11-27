"""Native ownership for bounded Loom workers; not a filesystem/network sandbox.

POSIX workers keep their private process group. On Windows an unnamed Job owns
the worker before its primary thread is resumed, and ordinary descendants
(including Ollama runners) inherit that Job. Breakaway is never enabled.
Windows JobMemoryLimit bounds committed memory, *not* resident working set;
the supervisor separately samples RSS and records the two measurements distinctly.

Win32 contracts: learn.microsoft.com/windows/win32/procthread/job-objects and
learn.microsoft.com/windows/win32/api/winnt/ns-winnt-jobobject_extended_limit_information.
"""
from __future__ import annotations

import ctypes
import errno
import hashlib
import math
import os
from pathlib import Path
import subprocess
import sys
import time


def linux_group_snapshot(pgid):
    """Read bounded procfs records, with no probe subprocess inside the group."""
    if not sys.platform.startswith("linux"):
        raise OSError(errno.ENOTSUP, "Linux process accounting requires procfs")
    page_size = os.sysconf("SC_PAGE_SIZE")
    deadline = time.monotonic() + 1
    members = []
    with os.scandir("/proc") as entries:
        for count, entry in enumerate(entries):
            if count > 65536 or time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired("owned process-group procfs sample", 1)
            if not entry.name.isdecimal():
                continue
            try:
                with open(entry.path + "/stat", "rb") as stream:
                    raw = stream.read(4097)
            except (FileNotFoundError, ProcessLookupError):
                continue  # A process exited during enumeration.
            except PermissionError:
                # procfs hidepid=1 can deny stat records for unrelated users.
                # Only a native directory owner different from this worker's
                # euid permits omission. An inaccessible own-user process or
                # an uninspectable owner still makes the whole sample fail.
                try:
                    owner = entry.stat(follow_symlinks=False).st_uid
                except (FileNotFoundError, ProcessLookupError):
                    continue
                if owner != os.geteuid():
                    continue
                raise
            # comm may contain spaces, newlines and ')' characters.
            end = raw.rfind(b") ")
            fields = raw[end + 2:].split()
            if len(raw) > 4096 or end < 1 or len(fields) < 22:
                raise ValueError("Malformed bounded procfs process record")
            if int(fields[2]) == pgid:
                rss = int(fields[21])
                if rss < 0:
                    raise ValueError("Negative procfs resident page count")
                members.append({"pid": int(entry.name), "rss_bytes": rss * page_size,
                                "zombie": fields[0] in (b"Z", b"X")})
    return members


def windows_system_directory():
    """Use the native OS location rather than an inherited executable search path."""
    api = _WindowsAPI()
    buffer = ctypes.create_unicode_buffer(32768)
    size = api.kernel.GetWindowsDirectoryW(buffer, len(buffer))
    if not 0 < size < len(buffer):
        raise ctypes.WinError(ctypes.get_last_error())
    return buffer.value


class _WindowsAPI:
    def __init__(self):
        if sys.platform != "win32" or ctypes.sizeof(ctypes.c_void_p) != 8:
            raise OSError(errno.ENOTSUP, "Loom Job supervision requires 64-bit Windows")
        from ctypes import wintypes as w
        self.w = w
        self.kernel = k = ctypes.WinDLL("kernel32", use_last_error=True)
        self.psapi = ctypes.WinDLL("psapi", use_last_error=True)
        pointer = ctypes.c_void_p
        size = ctypes.c_size_t
        signatures = {
            "CreateJobObjectW": ([pointer, w.LPCWSTR], w.HANDLE),
            "SetInformationJobObject": ([w.HANDLE, ctypes.c_int, pointer, w.DWORD], w.BOOL),
            "QueryInformationJobObject": ([w.HANDLE, ctypes.c_int, pointer, w.DWORD, pointer], w.BOOL),
            "AssignProcessToJobObject": ([w.HANDLE, w.HANDLE], w.BOOL),
            "TerminateJobObject": ([w.HANDLE, w.UINT], w.BOOL),
            "CloseHandle": ([w.HANDLE], w.BOOL),
            "GetCurrentProcess": ([], w.HANDLE),
            "DuplicateHandle": ([w.HANDLE, w.HANDLE, w.HANDLE, pointer, w.DWORD, w.BOOL, w.DWORD], w.BOOL),
            "InitializeProcThreadAttributeList": ([pointer, w.DWORD, w.DWORD, pointer], w.BOOL),
            "UpdateProcThreadAttribute": ([pointer, w.DWORD, size, pointer, size, pointer, pointer], w.BOOL),
            "DeleteProcThreadAttributeList": ([pointer], None),
            "CreateProcessW": ([w.LPCWSTR, w.LPWSTR, pointer, pointer, w.BOOL, w.DWORD,
                                pointer, w.LPCWSTR, pointer, pointer], w.BOOL),
            "ResumeThread": ([w.HANDLE], w.DWORD),
            "TerminateProcess": ([w.HANDLE, w.UINT], w.BOOL),
            "WaitForSingleObject": ([w.HANDLE, w.DWORD], w.DWORD),
            "GetExitCodeProcess": ([w.HANDLE, pointer], w.BOOL),
            "OpenProcess": ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            "IsProcessInJob": ([w.HANDLE, w.HANDLE, pointer], w.BOOL),
            "QueryFullProcessImageNameW": ([w.HANDLE, w.DWORD, w.LPWSTR, pointer], w.BOOL),
            "GetWindowsDirectoryW": ([w.LPWSTR, w.UINT], w.UINT),
            "CreateIoCompletionPort": ([w.HANDLE, w.HANDLE, size, w.DWORD], w.HANDLE),
            "GetQueuedCompletionStatus": ([w.HANDLE, pointer, pointer, pointer, w.DWORD], w.BOOL),
        }
        for name, (args, result) in signatures.items():
            fn = getattr(k, name)
            fn.argtypes, fn.restype = args, result
        self.psapi.GetProcessMemoryInfo.argtypes = [w.HANDLE, pointer, w.DWORD]
        self.psapi.GetProcessMemoryInfo.restype = w.BOOL

    @staticmethod
    def check(value):
        if not value:
            raise ctypes.WinError(ctypes.get_last_error())
        return value


class _BasicLimits(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32)]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimits), ("IoInfo", ctypes.c_uint64 * 6),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


class _MemoryCounters(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_uint32), ("PageFaultCount", ctypes.c_uint32)]
    _fields_ += [(name, ctypes.c_size_t) for name in (
        "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
        "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]


class WindowsJobProcess:
    """Small Popen-compatible process with a non-inheritable owned Job handle.

    A setup failure terminates and reaps the suspended child. Closing the final
    Job handle also kills every member, including if this supervisor crashes.
    No process is resumed after failed assignment; parent/nested Job restrictions
    therefore cause explicit refusal rather than an unowned worker.
    """
    def __init__(self, command, *, cwd, env, stdout, stderr, max_memory_bytes):
        if (type(max_memory_bytes) is not int or not 1 <= max_memory_bytes <= 4294967296
                or not command or not Path(command[0]).is_absolute()):
            raise ValueError("An absolute executable and bounded Windows Job memory cap are required")
        self.api = _WindowsAPI()
        self.command, self.returncode = command, None
        self.pid, self._process, self._job, self._port = None, None, None, None
        self.memory_limit_bytes = max_memory_bytes
        self.memory_limit_seen = False
        self.peak_commit_bytes = 0
        self._sampled_members = {}
        self._member_evidence_truncated = False
        try:
            self._job = self.api.check(self.api.kernel.CreateJobObjectW(None, None))
            limits = _ExtendedLimits()
            # KILL_ON_JOB_CLOSE | JOB_MEMORY; no BREAKAWAY or SILENT_BREAKAWAY.
            limits.BasicLimitInformation.LimitFlags = 0x2000 | 0x200
            limits.JobMemoryLimit = max_memory_bytes
            self.api.check(self.api.kernel.SetInformationJobObject(
                self._job, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
            self._port = self.api.check(self.api.kernel.CreateIoCompletionPort(
                ctypes.c_void_p(-1), None, 0, 1))
            association = (ctypes.c_void_p * 2)(1, self._port)
            self.api.check(self.api.kernel.SetInformationJobObject(
                self._job, 7, association, ctypes.sizeof(association)))
            self._spawn(command, cwd, env, stdout, stderr)
        except BaseException:
            if self._process:
                self.api.kernel.TerminateProcess(self._process, 1)
                self.api.kernel.WaitForSingleObject(self._process, 5000)
            self.close()
            raise

    def _spawn(self, command, cwd, env, stdout, stderr):
        import msvcrt
        w, k = self.api.w, self.api.kernel

        class Startup(ctypes.Structure):
            _fields_ = [("cb", w.DWORD), ("lpReserved", w.LPWSTR), ("lpDesktop", w.LPWSTR),
                        ("lpTitle", w.LPWSTR)]
            _fields_ += [(name, w.DWORD) for name in ("dwX", "dwY", "dwXSize", "dwYSize",
                        "dwXCountChars", "dwYCountChars", "dwFillAttribute", "dwFlags")]
            _fields_ += [("wShowWindow", w.WORD), ("cbReserved2", w.WORD),
                        ("lpReserved2", ctypes.c_void_p), ("hStdInput", w.HANDLE),
                        ("hStdOutput", w.HANDLE), ("hStdError", w.HANDLE)]

        class StartupEx(ctypes.Structure):
            _fields_ = [("StartupInfo", Startup), ("lpAttributeList", ctypes.c_void_p)]

        class ProcessInfo(ctypes.Structure):
            _fields_ = [("hProcess", w.HANDLE), ("hThread", w.HANDLE),
                        ("dwProcessId", w.DWORD), ("dwThreadId", w.DWORD)]

        startup, info = StartupEx(), ProcessInfo()
        startup.StartupInfo.cb = ctypes.sizeof(startup)
        startup.StartupInfo.dwFlags = 0x100  # STARTF_USESTDHANDLES
        duplicates, attributes, initialized = [], None, False
        try:
            with open(os.devnull, "rb") as null:
                for stream in (null, stdout, stderr):
                    copied = w.HANDLE()
                    self.api.check(k.DuplicateHandle(k.GetCurrentProcess(), msvcrt.get_osfhandle(stream.fileno()),
                        k.GetCurrentProcess(), ctypes.byref(copied), 0, True, 2))
                    duplicates.append(copied.value)
                startup.StartupInfo.hStdInput, startup.StartupInfo.hStdOutput, startup.StartupInfo.hStdError = duplicates
                size = ctypes.c_size_t()
                k.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
                if not 0 < size.value <= 65536:
                    raise OSError("Invalid Windows process attribute list size")
                attributes = ctypes.create_string_buffer(size.value)
                self.api.check(k.InitializeProcThreadAttributeList(attributes, 1, 0, ctypes.byref(size)))
                initialized = True
                handles = (w.HANDLE * len(duplicates))(*duplicates)
                self.api.check(k.UpdateProcThreadAttribute(attributes, 0, 0x20002, handles,
                    ctypes.sizeof(handles), None, None))  # PROC_THREAD_ATTRIBUTE_HANDLE_LIST
                startup.lpAttributeList = ctypes.addressof(attributes)
                if any("\x00" in key + value or "=" in key for key, value in env.items()):
                    raise ValueError("Invalid worker environment")
                environment = ctypes.create_unicode_buffer("\x00".join(
                    key + "=" + value for key, value in sorted(env.items(), key=lambda row: row[0].upper())) + "\x00")
                # SUSPENDED | UNICODE_ENVIRONMENT | EXTENDED_STARTUPINFO_PRESENT | NO_WINDOW
                self.api.check(k.CreateProcessW(str(command[0]), ctypes.create_unicode_buffer(subprocess.list2cmdline(command)),
                    None, None, True, 0x4 | 0x400 | 0x80000 | 0x8000000,
                    environment, str(cwd), ctypes.byref(startup), ctypes.byref(info)))
                self._process, self.pid = info.hProcess, info.dwProcessId
                self.api.check(k.AssignProcessToJobObject(self._job, self._process))
                if k.ResumeThread(info.hThread) == 0xffffffff:
                    raise ctypes.WinError(ctypes.get_last_error())
        finally:
            if info.hThread:
                k.CloseHandle(info.hThread)
            if initialized:
                k.DeleteProcThreadAttributeList(attributes)
            for handle in duplicates:
                k.CloseHandle(handle)

    def poll(self):
        if self.returncode is None:
            state = self.api.kernel.WaitForSingleObject(self._process, 0)
            if state == 0:
                code = self.api.w.DWORD()
                self.api.check(self.api.kernel.GetExitCodeProcess(self._process, ctypes.byref(code)))
                self.returncode = code.value
            elif state != 258:  # WAIT_TIMEOUT
                raise ctypes.WinError(ctypes.get_last_error())
        return self.returncode

    def wait(self, timeout=None):
        milliseconds = 0xffffffff if timeout is None else min(0xfffffffe, max(0, math.ceil(timeout * 1000)))
        state = self.api.kernel.WaitForSingleObject(self._process, milliseconds)
        if state == 258:
            raise subprocess.TimeoutExpired(self.command, timeout)
        if state != 0:
            raise ctypes.WinError(ctypes.get_last_error())
        return self.poll()

    def pids(self):
        # JOBOBJECT_BASIC_PROCESS_ID_LIST: two DWORDs then ULONG_PTR entries.
        for capacity in (64, 256, 1024, 4096):
            class Pids(ctypes.Structure):
                _fields_ = [("assigned", ctypes.c_uint32), ("count", ctypes.c_uint32),
                            ("values", ctypes.c_size_t * capacity)]
            result = Pids()
            if self.api.kernel.QueryInformationJobObject(self._job, 3, ctypes.byref(result), ctypes.sizeof(result), None):
                if result.count > capacity or result.assigned > result.count:
                    continue
                return list(result.values[:result.count])
            if ctypes.get_last_error() != 234:  # ERROR_MORE_DATA
                raise ctypes.WinError(ctypes.get_last_error())
        raise OSError("Windows Job member enumeration exceeded its bound")

    def snapshot(self):
        members = []
        for pid in self.pids():
            # SYNCHRONIZE also permits an exit check when the memory sample races teardown.
            process = self.api.kernel.OpenProcess(0x100000 | 0x400 | 0x10, False, pid)
            if not process:
                error = ctypes.get_last_error()
                if error == 87 and pid not in self.pids():
                    continue
                raise ctypes.WinError(error)
            try:
                belongs = self.api.w.BOOL()
                self.api.check(self.api.kernel.IsProcessInJob(process, self._job, ctypes.byref(belongs)))
                if not belongs.value:
                    continue  # PID reused after job member exit; never measure unrelated processes.
                counters = _MemoryCounters()
                counters.cb = ctypes.sizeof(counters)
                if not self.api.psapi.GetProcessMemoryInfo(process, ctypes.byref(counters), counters.cb):
                    error = ctypes.get_last_error()
                    if self.api.kernel.WaitForSingleObject(process, 0) == 0:
                        continue
                    raise ctypes.WinError(error)
                image = ctypes.create_unicode_buffer(32768)
                length = self.api.w.DWORD(len(image))
                if not self.api.kernel.QueryFullProcessImageNameW(process, 0, image, ctypes.byref(length)):
                    error = ctypes.get_last_error()
                    if self.api.kernel.WaitForSingleObject(process, 0) == 0:
                        continue
                    raise ctypes.WinError(error)
                # Bound retained membership evidence independently from the
                # complete Job RSS sum. No executable path or command is run.
                image_digest = hashlib.sha256(os.path.normcase(os.path.normpath(image.value)).encode("utf-8")).hexdigest()
                identity = (pid, image_digest)
                if identity in self._sampled_members or len(self._sampled_members) < 64:
                    self._sampled_members[identity] = {"pid": pid, "image_name": Path(image.value).name[:64],
                                                       "image_path_sha256": image_digest}
                else:
                    self._member_evidence_truncated = True
                members.append({"pid": pid, "rss_bytes": counters.WorkingSetSize, "zombie": False})
            finally:
                self.api.kernel.CloseHandle(process)
        return members

    def memory_observation(self):
        limits = _ExtendedLimits()
        self.api.check(self.api.kernel.QueryInformationJobObject(
            self._job, 9, ctypes.byref(limits), ctypes.sizeof(limits), None))
        if not limits.BasicLimitInformation.LimitFlags & 0x200 or limits.JobMemoryLimit != self.memory_limit_bytes:
            raise OSError("Windows Job committed-memory limit differs from its configured bound")
        self.peak_commit_bytes = max(self.peak_commit_bytes, limits.PeakJobMemoryUsed)
        # Notifications are evidence when present, not a guaranteed delivery API.
        for _ in range(8192):
            message, key, value = self.api.w.DWORD(), ctypes.c_size_t(), ctypes.c_void_p()
            if not self.api.kernel.GetQueuedCompletionStatus(self._port, ctypes.byref(message),
                    ctypes.byref(key), ctypes.byref(value), 0):
                if ctypes.get_last_error() == 258:
                    break
                raise ctypes.WinError(ctypes.get_last_error())
            if message.value in (9, 10):
                self.memory_limit_seen = True
        else:
            raise OSError("Windows Job notification queue exceeded its bound")
        return {"windows_job_commit_limit_bytes": limits.JobMemoryLimit,
                "windows_job_peak_commit_bytes": self.peak_commit_bytes,
                "windows_job_sampled_members": sorted(self._sampled_members.values(), key=lambda row: (row["pid"], row["image_path_sha256"])),
                "windows_job_member_evidence_truncated": self._member_evidence_truncated,
                "windows_job_member_scope": "Native image path hash and IsProcessInJob membership at RSS sample time; bounded to 64 identities; display names at most 64 characters.",
                "windows_job_peak_scope": "Native PeakJobMemoryUsed telemetry; a denied allocation may still raise this counter. It is not an RSS ceiling.",
                "windows_job_memory_limit_notified": self.memory_limit_seen}

    def stop(self):
        self.api.check(self.api.kernel.TerminateJobObject(self._job, 1))
        deadline = time.monotonic() + 6
        while True:
            pids = self.pids()
            if not pids and self.poll() is not None:
                return {"confirmed_stopped": True, "kill_escalated": True, "zombie_pids": []}
            if time.monotonic() >= deadline:
                return {"confirmed_stopped": False, "kill_escalated": True, "member_pids": pids}
            time.sleep(0.05)

    def close(self):
        # Closing the Job remains a kill-on-close fallback even if stop failed.
        for name in ("_job", "_port", "_process"):
            handle = getattr(self, name, None)
            if handle:
                self.api.kernel.CloseHandle(handle)
                setattr(self, name, None)


def launch_worker(command, *, cwd, env, stdout, stderr, max_memory_bytes):
    if sys.platform == "win32":
        return WindowsJobProcess(command, cwd=cwd, env=env, stdout=stdout, stderr=stderr,
                                 max_memory_bytes=max_memory_bytes)
    if sys.platform != "darwin" and not sys.platform.startswith("linux"):
        raise OSError(errno.ENOTSUP, "Loom worker supervision requires macOS, Linux or Windows")
    return subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                            cwd=cwd, env=env, start_new_session=True)
