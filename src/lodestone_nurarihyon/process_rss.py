"""Read-only macOS/Linux RSS sampling for an already owned Python worker.

Avoids launching /bin/ps (a setuid executable on this Mac). This does not grant
process access or suppress permission errors. Samples are not a hard peak cap;
the controller separately checks the worker's final getrusage maximum.
"""
import ctypes
import errno
import os
import subprocess
import sys


class TaskInfo(ctypes.Structure):
    # Darwin proc_taskinfo, identical to the reviewed supervisor's TaskInfo.
    _fields_ = [(name, ctypes.c_uint64) for name in
                ("virtual", "resident", "user", "system", "threads_user", "threads_system")]
    _fields_ += [(name, ctypes.c_int32) for name in
                 ("policy", "faults", "pageins", "cow", "sent", "received", "mach_calls",
                  "unix_calls", "switches", "threads", "running", "priority")]


class WorkerGone(ProcessLookupError):
    """Only a zero-length native result with ESRCH, not an arbitrary short read."""


class DarwinTaskRSS:
    """One native reader; never enumerates processes or starts a subprocess."""
    def __init__(self):
        if sys.platform != "darwin" or ctypes.sizeof(ctypes.c_void_p) != 8:
            raise OSError(errno.ENOTSUP, "Worker RSS requires 64-bit Darwin")
        if ctypes.sizeof(TaskInfo) != 96:
            raise OSError(errno.ENOTSUP, "Darwin proc_taskinfo layout differs from 96-byte contract")
        self.library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        self.info = self.library.proc_pidinfo
        self.info.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                              ctypes.c_void_p, ctypes.c_int]
        self.info.restype = ctypes.c_int

    def rss(self, pid):
        if type(pid) is not int or not 0 < pid <= 2147483647:
            raise ValueError("Worker PID must be a positive signed 32-bit integer")
        value = TaskInfo()
        size = ctypes.sizeof(value)
        ctypes.set_errno(0)
        got = self.info(pid, 4, 0, ctypes.byref(value), size)  # PROC_PIDTASKINFO
        error = ctypes.get_errno()
        if got != size:
            if got == 0 and error == errno.ESRCH:
                raise WorkerGone(error, "Owned worker disappeared before RSS sample", pid)
            raise OSError(error or errno.EIO,
                          f"proc_pidinfo worker={pid} flavor=4 expected={size} got={got} errno={error}")
        return int(value.resident)


class LinuxTaskRSS:
    """Read Linux procfs RSS without spawning a memory-probe subprocess."""
    def __init__(self):
        if not sys.platform.startswith("linux"):
            raise OSError(errno.ENOTSUP, "Worker RSS requires Linux procfs")
        self.page_size = os.sysconf("SC_PAGE_SIZE")
        if type(self.page_size) is not int or not 0 < self.page_size <= 1048576:
            raise OSError(errno.ENOTSUP, "Invalid kernel page size")

    def rss(self, pid):
        if type(pid) is not int or not 0 < pid <= 2147483647:
            raise ValueError("Worker PID must be a positive signed 32-bit integer")
        try:
            with open(f"/proc/{pid}/statm", "rb") as stream:
                raw = stream.read(257)
        except FileNotFoundError as exc:
            raise WorkerGone(errno.ESRCH, "Owned worker procfs entry disappeared", pid) from exc
        fields = raw.split()
        if len(raw) > 256 or len(fields) != 7 or any(not field.isdigit() for field in fields):
            raise OSError(errno.EIO, "Invalid bounded procfs statm record")
        resident = int(fields[1]) * self.page_size
        # Linux reports zero for zombies and some exit/exec transitions. It is
        # an unavailable sample, not evidence of a zero-byte resident process.
        return resident or None


def owned_worker_observer():
    """Fail explicitly on platforms whose RSS accounting is not supported."""
    if sys.platform == "darwin":
        return DarwinTaskRSS()
    if sys.platform.startswith("linux"):
        return LinuxTaskRSS()
    raise OSError(errno.ENOTSUP, "Nurarihyon execution requires macOS or Linux")


def sample_owned_worker(child, observer):
    """No sample for Linux zero RSS, or ESRCH plus this owned child's confirmed exit.

    A native process record can disappear during exit before waitpid reports
    termination. Allow at most 100 ms for that owned child to be reaped; a child
    still running after this interval remains an observer failure. This wait is
    part of the controller's elapsed-time accounting. Permission failures and
    malformed native records are never suppressed.
    """
    try:
        return observer.rss(child.pid)
    except WorkerGone:
        if child.poll() is not None:
            return None
        try:
            child.wait(timeout=0.1)
        except subprocess.TimeoutExpired:
            pass
        else:
            return None
        raise
