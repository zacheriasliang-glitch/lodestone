"""Per-process Windows termination completion; no models or external services."""
import ctypes
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parents[2] / "desktop/runtime/src/lodestone_machinery/portable_process.py"
spec = importlib.util.spec_from_file_location("stop_completion_native", SOURCE)
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)


class StopCompletionContractTests(unittest.TestCase):
    def owned(self):
        child = object.__new__(native.WindowsJobProcess)
        kernel = SimpleNamespace(TerminateJobObject=Mock(return_value=1),
                                 WaitForSingleObject=Mock(return_value=0), CloseHandle=Mock())
        child.api = SimpleNamespace(kernel=kernel, check=lambda value: value)
        child._member_handles = {(2, 20): 22, (3, 30): 33}
        child._stop_unconfirmed = False
        child._job, child._process = 1, 11
        child.pids = Mock(return_value=[])
        child.poll = Mock(return_value=0)
        child._total_processes = Mock(side_effect=[3, 3])
        child._capture_stop_members = Mock(return_value=True)
        return child

    def test_empty_pid_list_still_waits_previously_observed_identities(self):
        child = self.owned()
        result = child.stop()
        self.assertTrue(result["confirmed_stopped"], result)
        self.assertEqual([call.args[0] for call in child.api.kernel.WaitForSingleObject.call_args_list], [22, 33, 11])
        self.assertEqual(result["waited_member_count"], 2)
        self.assertTrue(result["parent_signaled"])
        self.assertFalse(child._member_handles)
        self.assertEqual([call.args[0] for call in child.api.kernel.CloseHandle.call_args_list], [22, 33])

    def test_unseen_late_arrival_never_confirms_empty_job(self):
        child = self.owned()
        child._total_processes.side_effect = [3, 4]
        result = child.stop()
        self.assertFalse(result["confirmed_stopped"])
        self.assertEqual(result["reason"], "membership_changed_during_stop")
        self.assertEqual((result["total_processes_before"], result["total_processes_after"]), (3, 4))
        self.assertFalse(child._member_handles)

    def test_unsignaled_descendant_prevents_confirmation(self):
        child = self.owned()
        child.api.kernel.WaitForSingleObject.side_effect = [258, 0, 0]
        result = child.stop()
        self.assertFalse(result["confirmed_stopped"])
        self.assertEqual(result["unsignaled_member_pids"], [2])
        self.assertFalse(child._member_handles)

    def test_unsignaled_direct_parent_prevents_confirmation(self):
        child = self.owned()
        child.api.kernel.WaitForSingleObject.side_effect = [0, 0, 258]
        result = child.stop()
        self.assertFalse(result["confirmed_stopped"])
        self.assertFalse(result["parent_signaled"])

    def test_only_signaled_observed_handles_are_released_between_samples(self):
        child = self.owned()
        child.api.kernel.WaitForSingleObject.side_effect = [0, 258]
        child._release_completed_members()
        self.assertEqual(child._member_handles, {(3, 30): 33})
        child.api.kernel.CloseHandle.assert_called_once_with(22)

    def test_retained_handle_bound_refuses_another_identity(self):
        child = self.owned()
        child._member_handles = {(pid, pid): pid for pid in range(4096)}
        child._member_identity = Mock(return_value=(5000, 5000))
        with self.assertRaisesRegex(OSError, "exceeded"):
            child._retain_member(5000, 5000)
        self.assertEqual(len(child._member_handles), 4096)

    def test_one_deadline_bounds_all_descendant_and_parent_waits(self):
        child = self.owned()
        with patch.object(native.time, "monotonic", side_effect=[100, 101, 105, 107]):
            child.stop()
        self.assertEqual([call.args[1] for call in child.api.kernel.WaitForSingleObject.call_args_list], [5000, 1000, 0])

    def test_failed_capture_requests_termination_and_closes_every_handle(self):
        child = self.owned()
        child._capture_stop_members.side_effect = PermissionError("native member query denied")
        with self.assertRaisesRegex(PermissionError, "denied"):
            child.stop()
        child.api.kernel.TerminateJobObject.assert_called_once_with(1, 1)
        self.assertFalse(child._member_handles)
        self.assertEqual(child.api.kernel.CloseHandle.call_count, 2)

    def test_failed_accounting_requests_termination_and_never_confirms(self):
        child = self.owned()
        child._total_processes.side_effect = PermissionError("native accounting denied")
        with self.assertRaisesRegex(PermissionError, "denied"):
            child.stop()
        child.api.kernel.TerminateJobObject.assert_called_once_with(1, 1)
        self.assertFalse(child._member_handles)

    def test_capture_deadline_still_terminates_without_claiming_completion(self):
        child = self.owned()
        child._capture_stop_members.return_value = False
        result = child.stop()
        self.assertFalse(result["confirmed_stopped"])
        self.assertEqual(result["reason"], "member_capture_timeout")
        child.api.kernel.TerminateJobObject.assert_called_once_with(1, 1)
        self.assertFalse(child._member_handles)

    def test_repeated_stop_cannot_forget_prior_unsignaled_descendants(self):
        child = self.owned()
        child._total_processes.side_effect = [3, 3, 3, 3]
        child.api.kernel.WaitForSingleObject.side_effect = [258, 0, 0, 0]
        self.assertFalse(child.stop()["confirmed_stopped"])
        self.assertFalse(child._member_handles)
        second = child.stop()
        self.assertFalse(second["confirmed_stopped"])
        self.assertEqual(second["reason"], "previous_stop_unconfirmed")

    def test_capture_uses_existing_running_identity_without_reopening_pid(self):
        child = self.owned()
        child.api.kernel.WaitForSingleObject.return_value = 258
        child.api.kernel.OpenProcess = Mock(side_effect=AssertionError("must use retained identity"))
        result = native.WindowsJobProcess._capture_stop_members(child, [2, 3], time.monotonic() + 6)
        self.assertTrue(result)
        child.api.kernel.OpenProcess.assert_not_called()

    def test_unexpected_native_wait_is_not_disappearance(self):
        child = self.owned()
        child.api.kernel.WaitForSingleObject.return_value = 0xffffffff
        with patch.object(native.ctypes, "get_last_error", return_value=6, create=True), \
                patch.object(native.ctypes, "WinError", side_effect=lambda value: OSError(value, "native wait failed"), create=True):
            with self.assertRaisesRegex(OSError, "native wait failed"):
                child.stop()
        self.assertFalse(child._member_handles)
        self.assertEqual(child.api.kernel.CloseHandle.call_count, 2)


CHILD = r'''
import ctypes, os, subprocess, sys, time
from pathlib import Path
from ctypes import wintypes as w
level, count = map(int, sys.argv[1:])
if level:
    subprocess.Popen([sys.executable, '-B', __file__, str(level - 1), str(count)])
held = []
if count:
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetCurrentProcess.restype = w.HANDLE
    kernel.GetStdHandle.argtypes, kernel.GetStdHandle.restype = [w.DWORD], w.HANDLE
    kernel.DuplicateHandle.argtypes = [w.HANDLE,w.HANDLE,w.HANDLE,ctypes.POINTER(w.HANDLE),w.DWORD,w.BOOL,w.DWORD]
    kernel.DuplicateHandle.restype = w.BOOL
    current, stderr = kernel.GetCurrentProcess(), kernel.GetStdHandle(-12)
    for _ in range(count):
        copied = w.HANDLE()
        if not kernel.DuplicateHandle(current,stderr,current,ctypes.byref(copied),0,False,2):
            raise ctypes.WinError(ctypes.get_last_error())
        held.append(copied.value)
Path('pid-' + str(level)).write_text(str(os.getpid()),encoding='ascii')
if level == 2:
    raise SystemExit(0)
while True:
    time.sleep(.1)
'''


@unittest.skipUnless(sys.platform == "win32", "Native Windows descendant handle completion")
class NativeStopCompletionTests(unittest.TestCase):
    def test_parent_exit_descendants_are_signaled_and_stderr_immediately_reusable(self):
        # The ordinary fixture reproduced the original failure in hosted CI.
        # Duplicate inherited stderr handles also exercise a longer teardown.
        for duplicate_count in (0, 8192):
            with self.subTest(duplicate_count=duplicate_count), tempfile.TemporaryDirectory(prefix="Loom stop ü ") as temporary:
                root = Path(temporary)
                script = root / "child.py"
                script.write_bytes(CHILD.encode())
                handles, child = [], None
                try:
                    with (root / "out").open("wb") as out, (root / "err").open("wb") as err:
                        child = native.WindowsJobProcess([sys.executable, "-B", str(script), "2", str(duplicate_count)],
                            cwd=str(root), env=dict(os.environ), stdout=out, stderr=err, max_memory_bytes=256 * 1024 * 1024)
                    deadline = time.monotonic() + 10
                    while True:
                        try:
                            known = {int((root / ("pid-" + str(index))).read_text()) for index in range(3)}
                            break
                        except (FileNotFoundError, ValueError):
                            if time.monotonic() >= deadline:
                                self.fail("Owned descendants did not become ready")
                            time.sleep(.01)
                    self.assertEqual(child.wait(timeout=5), 0)
                    observed = {row["pid"] for row in child.snapshot()}
                    self.assertTrue(known - {child.pid} <= observed)
                    for pid in child.pids():
                        handle = child.api.check(child.api.kernel.OpenProcess(0x100000 | 0x1000, False, pid))
                        handles.append(handle)
                        member = child.api.w.BOOL()
                        child.api.check(child.api.kernel.IsProcessInJob(handle, child._job, ctypes.byref(member)))
                        self.assertTrue(member.value)
                        self.assertEqual(child.api.kernel.GetProcessId(handle), pid)
                    result = child.stop()
                    self.assertTrue(result["confirmed_stopped"], result)
                    self.assertGreaterEqual(result["waited_member_count"], 2)
                    self.assertTrue(all(child.api.kernel.WaitForSingleObject(handle, 0) == 0 for handle in handles))
                    # No retry, sleep or cleanup workaround after confirmation.
                    (root / "err").unlink()
                    self.assertFalse(child._member_handles)
                finally:
                    if child is not None:
                        try:
                            child.stop()
                        finally:
                            child.close()
                            for handle in handles:
                                child.api.kernel.CloseHandle(handle)


if __name__ == "__main__":
    unittest.main()
