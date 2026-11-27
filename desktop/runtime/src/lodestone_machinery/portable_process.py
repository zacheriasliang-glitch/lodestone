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
            "GetProcessId": ([w.HANDLE], w.DWORD),
            "GetProcessTimes": ([w.HANDLE, pointer, pointer, pointer, pointer], w.BOOL),
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


class _BasicAccounting(ctypes.Structure):
    _fields_ = [(name, ctypes.c_int64) for name in (
        "TotalUserTime", "TotalKernelTime", "ThisPeriodTotalUserTime", "ThisPeriodTotalKernelTime")]
    _fields_ += [(name, ctypes.c_uint32) for name in (
        "TotalPageFaultCount", "TotalProcesses", "ActiveProcesses", "TotalTerminatedProcesses")]


class _MemoryCounters(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_uint32), ("PageFaultCount", ctypes.c_uint32)]
    _fields_ += [(name, ctypes.c_size_t) for name in (
        "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
        "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]


class WindowsJobCompletion:
    """Bounded completion tracker borrowing one Job and direct-process handle.

    The caller owns those two handles and must keep them open until close().
    Only identity-pinned descendant handles opened by this tracker are closed
    here. Construct after Job assignment and before resuming the root process.
    Strict history is for cleanup that must account for every lifetime member;
    an unseen, already-exited child keeps that cleanup unconfirmed.
    """
    def __init__(self, job, process, pid, *, api=None, command="owned Windows command",
                 require_complete_history=False):
        if not job or not process or type(pid) is not int or pid <= 0:
            raise ValueError("An assigned Job and direct-process handle/PID are required")
        self.api = api or _WindowsAPI()
        self._job, self._process, self.pid = job, process, pid
        self.command, self.returncode = command, None
        self._member_handles = {}
        self._stop_unconfirmed = False
        belongs = self.api.w.BOOL()
        self.api.check(self.api.kernel.IsProcessInJob(process, job, ctypes.byref(belongs)))
        if not belongs.value:
            raise OSError("The direct process is not assigned to the supplied Job")
        identity = self._member_identity(process, pid)
        self._lifetime_identities = {identity} if require_complete_history else None

    def observe_members(self):
        """Retain current members without querying images, RSS or memory limits."""
        self._release_completed_members()
        if not self._capture_stop_members(self.pids(), time.monotonic() + 1):
            raise subprocess.TimeoutExpired("Windows Job member capture", 1)

    def close(self):
        """Release private descendant handles; borrowed root/Job remain open."""
        self._close_member_handles()

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

    def _member_identity(self, process, pid):
        if self.api.check(self.api.kernel.GetProcessId(process)) != pid:
            raise OSError("Windows Job member handle has a different PID")
        times = [self.api.w.FILETIME() for _ in range(4)]
        self.api.check(self.api.kernel.GetProcessTimes(process, *(ctypes.byref(value) for value in times)))
        created = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        return pid, created

    def _retain_member(self, process, pid):
        # The caller has verified IsProcessInJob on this exact handle. A PID
        # alone cannot distinguish an exiting process from later PID reuse.
        identity = self._member_identity(process, pid)
        if identity in self._member_handles:
            return False
        history = getattr(self, "_lifetime_identities", None)
        if history is not None and identity not in history and len(history) >= 4096:
            raise OSError("Windows verified lifetime member identities exceeded their bound")
        if len(self._member_handles) >= 4096:
            raise OSError("Windows retained Job member handles exceeded their bound")
        self._member_handles[identity] = process
        if history is not None:
            history.add(identity)
        return True

    def _release_completed_members(self):
        for identity, process in list(self._member_handles.items()):
            state = self.api.kernel.WaitForSingleObject(process, 0)
            if state == 0:
                self.api.kernel.CloseHandle(process)
                del self._member_handles[identity]
            elif state != 258:
                raise ctypes.WinError(ctypes.get_last_error())

    def _close_member_handles(self):
        for process in self._member_handles.values():
            self.api.kernel.CloseHandle(process)
        self._member_handles.clear()

    def _total_processes(self):
        accounting = _BasicAccounting()
        self.api.check(self.api.kernel.QueryInformationJobObject(
            self._job, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None))
        return accounting.TotalProcesses

    def _capture_stop_members(self, pids, deadline):
        retained_by_pid = {}
        for (pid, _), process in self._member_handles.items():
            retained_by_pid.setdefault(pid, []).append(process)
        for pid in pids:
            if time.monotonic() >= deadline:
                return False
            if getattr(self, "_lifetime_identities", None) is not None and pid == self.pid:
                # Strict tracking already verified this borrowed root handle
                # before resume. It remains the completion proof even when a
                # short-lived root can no longer be reopened by PID. Any unseen
                # replacement identity still fails full lifetime accounting.
                state = self.api.kernel.WaitForSingleObject(self._process, 0)
                if state not in (0, 258):
                    raise ctypes.WinError(ctypes.get_last_error())
                continue
            observed_running = False
            for process in retained_by_pid.get(pid, []):
                state = self.api.kernel.WaitForSingleObject(process, 0)
                if state not in (0, 258):
                    raise ctypes.WinError(ctypes.get_last_error())
                observed_running |= state == 258
            if observed_running:
                # An unsignaled process still owns this PID; the previously
                # verified handle remains valid even while access is closing.
                continue
            process = self.api.kernel.OpenProcess(0x100000 | 0x1000, False, pid)
            if not process:
                error = ctypes.get_last_error()
                # An inaccessible selected PID cannot establish which process
                # object it denoted, even if an older PID match was retained.
                raise ctypes.WinError(error)
            retained = False
            try:
                belongs = self.api.w.BOOL()
                self.api.check(self.api.kernel.IsProcessInJob(process, self._job, ctypes.byref(belongs)))
                if not belongs.value:
                    raise OSError("Windows stop cannot bind a listed member to its owned Job")
                retained = self._retain_member(process, pid)
            finally:
                if not retained:
                    self.api.kernel.CloseHandle(process)
        return True

    def stop(self):
        # TerminateJobObject is asynchronous. An empty PID list can precede
        # descendant handle teardown, even after the direct parent has exited.
        # Wait on identity-bound process handles, including sampled members
        # that disappeared from the current list before termination began.
        deadline = time.monotonic() + 6
        previously_unconfirmed = self._stop_unconfirmed
        self._stop_unconfirmed = True
        result = {"confirmed_stopped": False, "kill_escalated": True, "zombie_pids": []}
        try:
            try:
                before = self._total_processes()
                if not self._capture_stop_members(self.pids(), deadline):
                    result["reason"] = "member_capture_timeout"
                    return result
            finally:
                # Inspection failures must still request termination; they
                # cannot become a successful stop or leave a running Job.
                self.api.check(self.api.kernel.TerminateJobObject(self._job, 1))
            if not self._capture_stop_members(self.pids(), deadline):
                result["reason"] = "member_capture_timeout"
                return result
            waits = {}
            for (pid, created), process in self._member_handles.items():
                milliseconds = max(0, min(6000, math.ceil((deadline - time.monotonic()) * 1000)))
                state = self.api.kernel.WaitForSingleObject(process, milliseconds)
                if state not in (0, 258):
                    raise ctypes.WinError(ctypes.get_last_error())
                waits[(pid, created)] = state
            milliseconds = max(0, min(6000, math.ceil((deadline - time.monotonic()) * 1000)))
            parent_state = self.api.kernel.WaitForSingleObject(self._process, milliseconds)
            if parent_state not in (0, 258):
                raise ctypes.WinError(ctypes.get_last_error())
            remaining = self.pids()
            after = self._total_processes()
            result.update(member_pids=remaining, waited_member_count=len(waits), parent_signaled=parent_state == 0,
                          unsignaled_member_pids=sorted(pid for (pid, _), state in waits.items() if state != 0),
                          total_processes_before=before, total_processes_after=after)
            # Lifetime accounting detects a child created and removed between
            # capture and termination, even when it never appears in a later
            # PID list. Its unobserved completion must remain unconfirmed.
            history = getattr(self, "_lifetime_identities", None)
            if history is not None:
                result["verified_lifetime_member_count"] = len(history)
            if previously_unconfirmed:
                # A prior failed attempt released its retained handles. A
                # later empty PID list cannot recover that missing proof.
                result["reason"] = "previous_stop_unconfirmed"
            elif after != before:
                result["reason"] = "membership_changed_during_stop"
            elif history is not None and after != len(history):
                result["reason"] = "unobserved_lifetime_member"
            elif remaining or parent_state != 0 or any(state != 0 for state in waits.values()) or self.poll() is None:
                result["reason"] = "process_completion_unconfirmed"
            else:
                result["confirmed_stopped"] = True
                self._stop_unconfirmed = False
            return result
        finally:
            self._close_member_handles()


class WindowsJobProcess(WindowsJobCompletion):
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
        self._member_handles = {}
        self._stop_unconfirmed = False
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
                super().__init__(self._job, self._process, self.pid, api=self.api, command=command)
                if k.ResumeThread(info.hThread) == 0xffffffff:
                    raise ctypes.WinError(ctypes.get_last_error())
        finally:
            if info.hThread:
                k.CloseHandle(info.hThread)
            if initialized:
                k.DeleteProcThreadAttributeList(attributes)
            for handle in duplicates:
                k.CloseHandle(handle)


    def _require_sampled_exit(self, process, pid, api, error, deadline):
        # Called only after this exact handle passed Job membership and
        # PID/creation-time verification. Inspection can become unavailable
        # just before exit signals the process object. PID disappearance alone
        # is not completion, and live/unknown states still refuse the sample.
        milliseconds = min(100, max(0, int((deadline - time.monotonic()) * 1000)))
        state = self.api.kernel.WaitForSingleObject(process, milliseconds)
        if state == 0:  # WAIT_OBJECT_0 on this process, never on the Job.
            return
        wait_error = ctypes.get_last_error() if state == 0xffffffff else None
        failure = ctypes.WinError(error)  # Preserve the original API's errno/winerror.
        failure.native_api, failure.native_pid = api, pid
        failure.native_wait_result = state
        if wait_error is not None:
            failure.native_wait_winerror = wait_error
        failure.add_note(f"{api} failed for owned PID {pid}; completion wait returned {state}")
        raise failure

    def snapshot(self):
        # One short completion grace for the whole sample, not per member.
        exit_deadline = time.monotonic() + .1
        self._release_completed_members()
        members = []
        for pid in self.pids():
            # SYNCHRONIZE also permits an exit check when the memory sample races teardown.
            process = self.api.kernel.OpenProcess(0x100000 | 0x400 | 0x10, False, pid)
            if not process:
                error = ctypes.get_last_error()
                if error == 87 and pid not in self.pids():
                    continue
                raise ctypes.WinError(error)
            retained = False
            try:
                belongs = self.api.w.BOOL()
                self.api.check(self.api.kernel.IsProcessInJob(process, self._job, ctypes.byref(belongs)))
                if not belongs.value:
                    continue  # PID reused after job member exit; never measure unrelated processes.
                retained = self._retain_member(process, pid)
                counters = _MemoryCounters()
                counters.cb = ctypes.sizeof(counters)
                if not self.api.psapi.GetProcessMemoryInfo(process, ctypes.byref(counters), counters.cb):
                    error = ctypes.get_last_error()
                    self._require_sampled_exit(process, pid, "GetProcessMemoryInfo", error, exit_deadline)
                    continue
                image = ctypes.create_unicode_buffer(32768)
                length = self.api.w.DWORD(len(image))
                if not self.api.kernel.QueryFullProcessImageNameW(process, 0, image, ctypes.byref(length)):
                    error = ctypes.get_last_error()
                    self._require_sampled_exit(process, pid, "QueryFullProcessImageNameW", error, exit_deadline)
                    continue
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
                if not retained:
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


    def close(self):
        # Closing the Job remains a kill-on-close fallback even if stop failed.
        self._close_member_handles()
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
