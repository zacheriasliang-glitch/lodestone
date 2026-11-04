"""Focused native-reader contracts using fakes; no processes or libraries loaded."""
import ctypes
import errno
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

from lodestone_nurarihyon.process_rss import (DarwinTaskRSS, LinuxTaskRSS, TaskInfo,
    sample_owned_worker, owned_worker_observer, WorkerGone)


class FakePidInfo:
    def __init__(self, *, result=96, error=0, rss=12345678):
        self.result, self.error, self.rss = result, error, rss
        self.calls = []

    def __call__(self, pid, flavor, arg, target, size):
        self.calls.append((pid, flavor, arg, size))
        ctypes.cast(target, ctypes.POINTER(TaskInfo)).contents.resident = self.rss
        ctypes.set_errno(self.error)
        return self.result


class ProcessRSSTests(unittest.TestCase):
    def reader(self, fake):
        library = SimpleNamespace(proc_pidinfo=fake)
        with mock.patch("lodestone_nurarihyon.process_rss.sys.platform", "darwin"), \
                mock.patch("lodestone_nurarihyon.process_rss.ctypes.CDLL", return_value=library) as load:
            reader = DarwinTaskRSS()
        load.assert_called_once_with("/usr/lib/libproc.dylib", use_errno=True)
        return reader

    def test_native_success_returns_bytes_with_exact_layout_and_signature(self):
        native = FakePidInfo(rss=43210987)
        reader = self.reader(native)
        self.assertEqual(ctypes.sizeof(TaskInfo), 96)
        self.assertEqual(reader.rss(120), 43210987)
        self.assertEqual(native.calls, [(120, 4, 0, 96)])
        self.assertEqual(native.argtypes, [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                                          ctypes.c_void_p, ctypes.c_int])
        self.assertIs(native.restype, ctypes.c_int)

    def test_wrong_platform_or_layout_refuses_before_loading_library(self):
        real_sizeof = ctypes.sizeof
        for platform, pointer_bytes, structure_bytes in (("linux", 8, 96), ("darwin", 4, 96), ("darwin", 8, 95)):
            def size(value):
                if value is ctypes.c_void_p:
                    return pointer_bytes
                if value is TaskInfo:
                    return structure_bytes
                return real_sizeof(value)
            with self.subTest(platform=platform, pointer_bytes=pointer_bytes, structure_bytes=structure_bytes), \
                    mock.patch("lodestone_nurarihyon.process_rss.sys.platform", platform), \
                    mock.patch("lodestone_nurarihyon.process_rss.ctypes.sizeof", side_effect=size), \
                    mock.patch("lodestone_nurarihyon.process_rss.ctypes.CDLL") as load:
                with self.assertRaises(OSError) as error:
                    DarwinTaskRSS()
                self.assertEqual(error.exception.errno, errno.ENOTSUP)
                load.assert_not_called()

    def test_wrong_returned_size_is_an_error_not_a_sample(self):
        reader = self.reader(FakePidInfo(result=95))
        with self.assertRaises(OSError) as error:
            reader.rss(120)
        self.assertEqual(error.exception.errno, errno.EIO)
        self.assertIn("expected=96 got=95", str(error.exception))

    def test_invalid_pid_refused_before_native_call(self):
        native = FakePidInfo()
        reader = self.reader(native)
        for pid in (True, False, 0, -1, 2147483648, "120", 1.5):
            with self.subTest(pid=pid), self.assertRaises(ValueError):
                reader.rss(pid)
        self.assertEqual(native.calls, [])

    def test_permission_denial_is_never_suppressed_by_child_exit(self):
        child = mock.Mock(pid=120)
        child.poll.return_value = 0
        reader = self.reader(FakePidInfo(result=0, error=errno.EPERM))
        with self.assertRaises(PermissionError) as error:
            sample_owned_worker(child, reader)
        self.assertEqual(error.exception.errno, errno.EPERM)
        child.poll.assert_not_called()

    def test_esrch_requires_confirmed_owned_child_exit(self):
        child = mock.Mock(pid=120)
        child.poll.return_value = None
        child.wait.side_effect = subprocess.TimeoutExpired("owned-worker", 0.1)
        reader = self.reader(FakePidInfo(result=0, error=errno.ESRCH))
        with self.assertRaises(WorkerGone):
            sample_owned_worker(child, reader)
        child.poll.assert_called_once_with()
        child.wait.assert_called_once_with(timeout=0.1)

    def test_esrch_during_exit_waits_for_owned_child_without_fabricating_rss(self):
        for code in (0, 1, -9):
            with self.subTest(returncode=code):
                child = mock.Mock(pid=120, returncode=None)
                child.poll.return_value = None
                def reap(*, timeout):
                    child.returncode = code
                    return code
                child.wait.side_effect = reap
                reader = self.reader(FakePidInfo(result=0, error=errno.ESRCH))
                self.assertIsNone(sample_owned_worker(child, reader))
                self.assertEqual(child.returncode, code)
                child.wait.assert_called_once_with(timeout=0.1)

    def test_wait_failure_is_not_mistaken_for_confirmed_exit(self):
        child = mock.Mock(pid=120)
        child.poll.return_value = None
        child.wait.side_effect = PermissionError(errno.EPERM, "wait failed")
        reader = self.reader(FakePidInfo(result=0, error=errno.ESRCH))
        with self.assertRaises(PermissionError):
            sample_owned_worker(child, reader)

    def test_esrch_with_reaped_child_returns_no_sample(self):
        child = mock.Mock(pid=120)
        child.poll.return_value = 0
        reader = self.reader(FakePidInfo(result=0, error=errno.ESRCH))
        self.assertIsNone(sample_owned_worker(child, reader))
        child.poll.assert_called_once_with()
        child.wait.assert_not_called()

    def test_partial_result_with_esrch_is_not_excused_as_disappearance(self):
        child = mock.Mock(pid=120)
        child.poll.return_value = 0
        reader = self.reader(FakePidInfo(result=8, error=errno.ESRCH))
        with self.assertRaises(OSError):
            sample_owned_worker(child, reader)
        child.poll.assert_not_called()


class LinuxProcessRSSTests(unittest.TestCase):
    def reader(self):
        with mock.patch("lodestone_nurarihyon.process_rss.sys.platform", "linux"), \
                mock.patch("lodestone_nurarihyon.process_rss.os.sysconf", return_value=4096):
            return LinuxTaskRSS()

    def test_resident_pages_converted_to_bytes_without_subprocess(self):
        with mock.patch("builtins.open", mock.mock_open(read_data=b"100 7 3 1 0 20 0\n")) as opened:
            self.assertEqual(self.reader().rss(120), 7 * 4096)
        opened.assert_called_once_with("/proc/120/statm", "rb")
        opened().read.assert_called_once_with(257)

    def test_zero_rss_is_not_reported_as_zero_memory_usage(self):
        with mock.patch("builtins.open", mock.mock_open(read_data=b"0 0 0 0 0 0 0\n")):
            self.assertIsNone(self.reader().rss(120))

    def test_invalid_procfs_or_pid_is_not_a_sample(self):
        for raw in (b"", b"1 2", b"1 -2 3 4 5 6 7", b"x" *257):
            with self.subTest(raw=raw), mock.patch("builtins.open", mock.mock_open(read_data=raw)):
                with self.assertRaises(OSError):
                    self.reader().rss(120)
        for pid in (True, 0, -1, "120", 2147483648):
            with self.subTest(pid=pid), self.assertRaises(ValueError):
                self.reader().rss(pid)

    def test_missing_procfs_requires_owned_child_exit(self):
        child = mock.Mock(pid=120)
        with mock.patch("builtins.open", side_effect=FileNotFoundError):
            child.poll.return_value = None
            child.wait.side_effect = subprocess.TimeoutExpired("owned-worker", 0.1)
            with self.assertRaises(WorkerGone):
                sample_owned_worker(child, self.reader())
            child.poll.return_value = 0
            self.assertIsNone(sample_owned_worker(child, self.reader()))
        with mock.patch("builtins.open", side_effect=PermissionError):
            with self.assertRaises(PermissionError):
                sample_owned_worker(child, self.reader())

    def test_unsupported_platform_fails_explicitly(self):
        with mock.patch("lodestone_nurarihyon.process_rss.sys.platform", "win32"):
            with self.assertRaisesRegex(OSError, "macOS or Linux"):
                owned_worker_observer()


class LinuxOwnedChildTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform.startswith("linux"),
                         "Actual Linux procfs sampling runs on Linux")
    def test_live_owned_child_has_a_positive_resident_sample(self):
        import subprocess
        import select
        child = subprocess.Popen([sys.executable, "-B", "-c",
            "import time; held=bytearray(1048576); print('ready',flush=True); time.sleep(10)"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            ready, _, _ = select.select([child.stdout], [], [], 2)
            self.assertTrue(ready, "Owned child did not emit readiness within two seconds")
            self.assertEqual(child.stdout.readline().strip(), "ready")
            value = sample_owned_worker(child, owned_worker_observer())
            self.assertIsInstance(value, int)
            self.assertGreater(value, 0)
        finally:
            child.terminate()
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=2)
            child.stdout.close()
            child.stderr.close()


class LauncherPlatformTests(unittest.TestCase):
    def test_windows_is_rejected_before_posix_runtime_import(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[2]
        for name in ("nurarihyon", "pando"):
            launcher = root / "tools" / name / "run"
            with self.subTest(name=name), mock.patch("sys.platform", "win32"):
                with self.assertRaisesRegex(SystemExit, "requires macOS or Linux"):
                    exec(compile(launcher.read_bytes(), str(launcher), "exec"),
                         {"__file__": str(launcher), "__name__": "__main__"})


if __name__ == "__main__":
    unittest.main()
