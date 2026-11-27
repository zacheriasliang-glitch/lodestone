"""Owned Windows process inspection during natural exit; no model calls."""
import ctypes
import errno
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

RUNTIME = Path(__file__).resolve().parents[2] / "desktop/runtime"
sys.path.insert(0, str(RUNTIME / "src"))
from lodestone_machinery import portable_process as native
from lodestone_machinery import loom_runtime as runtime


def winerror(code):
    # Match ctypes.WinError's PermissionError classification on non-Windows
    # hosts too, so the refusal contract runs on every supported native job.
    error = PermissionError(errno.EACCES, "native inspection denied")
    error.winerror = code
    return error


class SnapshotExitContractTests(unittest.TestCase):
    def setUp(self):
        self.last_error = 5
        for patcher in (patch.object(native.ctypes, "get_last_error", side_effect=lambda: self.last_error, create=True),
                        patch.object(native.ctypes, "WinError", side_effect=winerror, create=True)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def owned(self, pids=(7,)):
        child = object.__new__(native.WindowsJobProcess)
        def belongs(process, job, result):
            result._obj.value = 1
            return 1
        def memory(process, counters, size):
            counters._obj.WorkingSetSize = 4096
            return 1
        def image(process, flags, buffer, length):
            buffer.value = sys.executable
            return 1
        kernel = SimpleNamespace(OpenProcess=Mock(side_effect=lambda access, inherit, pid: pid + 100),
            IsProcessInJob=Mock(side_effect=belongs), QueryFullProcessImageNameW=Mock(side_effect=image),
            WaitForSingleObject=Mock(return_value=0), CloseHandle=Mock())
        child.api = SimpleNamespace(kernel=kernel, check=native._WindowsAPI.check,
            w=SimpleNamespace(BOOL=ctypes.c_int, DWORD=ctypes.c_uint32),
            psapi=SimpleNamespace(GetProcessMemoryInfo=Mock(side_effect=memory)))
        child._job, child._member_handles, child._sampled_members = 1, {}, {}
        child._member_evidence_truncated = False
        child.pids = Mock(return_value=list(pids))
        child._member_identity = Mock(side_effect=lambda process, pid: (pid, 1000 + pid))
        return child

    def fail_image(self, child):
        child.api.kernel.QueryFullProcessImageNameW.side_effect = None
        child.api.kernel.QueryFullProcessImageNameW.return_value = 0

    def test_image_failure_omits_only_exact_signaled_owned_handle(self):
        child = self.owned()
        self.fail_image(child)
        with patch.object(native.time, "monotonic", side_effect=[0, .025]):
            self.assertEqual(child.snapshot(), [])
        self.assertEqual(child.api.kernel.WaitForSingleObject.call_args.args, (107, 75))
        child._member_identity.assert_called_once_with(107, 7)
        self.assertEqual(child._member_handles, {(7, 1007): 107})
        child.api.kernel.CloseHandle.assert_not_called()  # stop still owns completion custody

    def test_memory_failure_uses_same_confirmed_exit_rule(self):
        child = self.owned()
        child.api.psapi.GetProcessMemoryInfo.side_effect = None
        child.api.psapi.GetProcessMemoryInfo.return_value = 0
        self.assertEqual(child.snapshot(), [])
        child.api.kernel.QueryFullProcessImageNameW.assert_not_called()
        self.assertEqual(child.api.kernel.WaitForSingleObject.call_args.args[0], 107)

    def test_live_process_keeps_original_image_api_and_error(self):
        child = self.owned()
        self.fail_image(child)
        child.api.kernel.WaitForSingleObject.return_value = 258
        with self.assertRaises(PermissionError) as caught:
            child.snapshot()
        error = caught.exception
        self.assertEqual((error.errno, error.winerror), (errno.EACCES, 5))
        self.assertEqual(runtime._native_inspection_details(error), {
            "api": "QueryFullProcessImageNameW", "pid": 7, "errno": errno.EACCES,
            "winerror": 5, "wait_result": 258})
        self.assertIn("QueryFullProcessImageNameW", error.__notes__[0])
        self.assertEqual(child._member_handles, {(7, 1007): 107})

    def test_failed_wait_does_not_replace_original_memory_api_error(self):
        child = self.owned()
        child.api.psapi.GetProcessMemoryInfo.side_effect = None
        child.api.psapi.GetProcessMemoryInfo.return_value = 0
        def failed_wait(*args):
            self.last_error = 6
            return 0xffffffff
        child.api.kernel.WaitForSingleObject.side_effect = failed_wait
        with self.assertRaises(PermissionError) as caught:
            child.snapshot()
        self.assertEqual(runtime._native_inspection_details(caught.exception), {
            "api": "GetProcessMemoryInfo", "pid": 7, "errno": errno.EACCES,
            "winerror": 5, "wait_result": 0xffffffff, "wait_winerror": 6})

    def test_unexpected_wait_result_does_not_establish_exit(self):
        child = self.owned()
        self.fail_image(child)
        child.api.kernel.WaitForSingleObject.return_value = 0x80
        with self.assertRaises(PermissionError) as caught:
            child.snapshot()
        self.assertEqual(caught.exception.native_wait_result, 0x80)

    def test_completion_grace_is_one_deadline_for_all_members(self):
        child = self.owned((7, 8))
        self.fail_image(child)
        with patch.object(native.time, "monotonic", side_effect=[0, .025, .101]):
            self.assertEqual(child.snapshot(), [])
        self.assertEqual([call.args for call in child.api.kernel.WaitForSingleObject.call_args_list],
                         [(107, 75), (108, 0)])

    def test_repeated_identity_waits_current_verified_handle_and_closes_duplicate(self):
        child = self.owned()
        child._member_handles = {(7, 1007): 99}
        self.fail_image(child)
        child.api.kernel.WaitForSingleObject.side_effect = [258, 0]
        self.assertEqual(child.snapshot(), [])
        self.assertEqual([call.args[0] for call in child.api.kernel.WaitForSingleObject.call_args_list], [99, 107])
        self.assertEqual(child._member_handles, {(7, 1007): 99})
        child._member_identity.assert_called_once_with(107, 7)
        child.api.kernel.CloseHandle.assert_called_once_with(107)

    def test_unverified_identity_never_uses_exit_fallback(self):
        child = self.owned()
        child._member_identity.side_effect = OSError("creation identity unavailable")
        with self.assertRaisesRegex(OSError, "identity unavailable"):
            child.snapshot()
        child.api.kernel.WaitForSingleObject.assert_not_called()
        child.api.psapi.GetProcessMemoryInfo.assert_not_called()
        child.api.kernel.CloseHandle.assert_called_once_with(107)

    def test_job_membership_failure_never_uses_exit_fallback(self):
        child = self.owned()
        child.api.kernel.IsProcessInJob.side_effect = None
        child.api.kernel.IsProcessInJob.return_value = 0
        with self.assertRaises(PermissionError):
            child.snapshot()
        child._member_identity.assert_not_called()
        child.api.kernel.WaitForSingleObject.assert_not_called()

    def test_openprocess_access_denied_is_not_inferred_as_exit(self):
        child = self.owned()
        child.api.kernel.OpenProcess.side_effect = None
        child.api.kernel.OpenProcess.return_value = 0
        with self.assertRaises(PermissionError) as caught:
            child.snapshot()
        self.assertEqual(caught.exception.winerror, 5)
        self.assertIsNone(runtime._native_inspection_details(caught.exception))
        child.api.kernel.WaitForSingleObject.assert_not_called()


class SamplingFailureReceiptTests(unittest.TestCase):
    def failed_worker(self, error):
        with tempfile.TemporaryDirectory(prefix="Loom sampling receipt ") as temporary:
            root = Path(temporary).resolve()
            bridge = object.__new__(runtime.LoomBridge)
            bridge._lock, bridge._unsafe_groups = threading.Lock(), set()
            config = {"python": sys.executable, "backend": "ollama_qwen3_base", "wall_seconds": 5,
                      "max_rss_bytes": 256 * 1024 * 1024, "runs_dir": str(root)}
            with patch.object(runtime, "launch_worker", side_effect=error):
                result = bridge._worker(config, root, {"fixture": True}, lambda: False)
            self.assertEqual(json.loads((root / "supervisor.json").read_bytes()), result)
            self.assertEqual(result["status"], "FAILED")
            return result["observation"]

    def test_native_error_details_survive_supervisor_json_without_text(self):
        error = winerror(5)
        error.native_api, error.native_pid = "QueryFullProcessImageNameW", 77
        error.native_wait_result, error.native_wait_winerror = 0xffffffff, 6
        error.private_path = "private source must not be recorded"
        observation = self.failed_worker(error)
        self.assertEqual(observation["reason"], "supervisor_error:PermissionError")
        self.assertEqual(observation["native_inspection_error"], {
            "api": "QueryFullProcessImageNameW", "pid": 77, "errno": errno.EACCES,
            "winerror": 5, "wait_result": 0xffffffff, "wait_winerror": 6})
        self.assertNotIn(error.private_path, json.dumps(observation))

    def test_unknown_exception_has_no_native_details(self):
        observation = self.failed_worker(OSError("private arbitrary exception text"))
        self.assertNotIn("native_inspection_error", observation)
        self.assertNotIn("private arbitrary", json.dumps(observation))

    def test_malformed_or_unbounded_details_are_not_serialized(self):
        for api, pid in (("arbitrary/path", 1), ("GetProcessMemoryInfo", "private/path"),
                         ("GetProcessMemoryInfo", 2 ** 100), ("GetProcessMemoryInfo", True)):
            with self.subTest(api=api, pid=pid):
                error = winerror(5)
                error.native_api, error.native_pid, error.native_wait_result = api, pid, 258
                self.assertIsNone(runtime._native_inspection_details(error))


CHILD = r'''
import os, subprocess, sys, time
from pathlib import Path
mode = sys.argv[1]
if mode == 'parent':
    children = [subprocess.Popen([sys.executable, '-B', __file__, str(i)]) for i in range(8)]
    while True:
        time.sleep(.01)
else:
    Path('ready-' + mode).write_text(str(os.getpid()), encoding='ascii')
    while not Path('exit').exists():
        time.sleep(.005)
    time.sleep(.025)
'''


@unittest.skipUnless(sys.platform == "win32", "Native Windows natural-exit inspection")
class NativeSnapshotExitTests(unittest.TestCase):
    def test_natural_descendant_exit_does_not_abort_owned_sampling(self):
        with tempfile.TemporaryDirectory(prefix="Loom snapshot ü ") as temporary:
            root = Path(temporary)
            script = root / "child.py"
            script.write_bytes(CHILD.encode())
            child, handles = None, []
            try:
                with (root / "out").open("wb") as out, (root / "err").open("wb") as err:
                    child = native.WindowsJobProcess([sys.executable, "-B", str(script), "parent"],
                        cwd=str(root), env=runtime._environment("ollama_qwen3_base"), stdout=out, stderr=err,
                        max_memory_bytes=512 * 1024 * 1024)
                deadline = time.monotonic() + 10
                while True:
                    try:
                        descendants = {int((root / ("ready-" + str(i))).read_text()) for i in range(8)}
                        break
                    except (FileNotFoundError, ValueError):
                        if time.monotonic() >= deadline:
                            self.fail("Natural-exit descendants did not become ready")
                        time.sleep(.01)
                observed = child.snapshot()
                self.assertTrue(descendants <= {row["pid"] for row in observed})
                self.assertTrue(all(row["rss_bytes"] > 0 for row in observed if row["pid"] in descendants))
                for pid in descendants:
                    handle = child.api.check(child.api.kernel.OpenProcess(0x100000 | 0x1000, False, pid))
                    handles.append(handle)
                    belongs = child.api.w.BOOL()
                    child.api.check(child.api.kernel.IsProcessInJob(handle, child._job, ctypes.byref(belongs)))
                    self.assertTrue(belongs.value)
                    self.assertEqual(child._member_identity(handle, pid)[0], pid)
                (root / "exit").write_bytes(b"exit authored descendants")
                deadline = time.monotonic() + 5
                child.snapshot()  # Always exercise sampling, even after scheduler-delayed exit.
                samples = 1
                while not all(child.api.kernel.WaitForSingleObject(handle, 0) == 0 for handle in handles):
                    self.assertLess(time.monotonic(), deadline, "Natural descendant exit exceeded bound")
                    self.assertLess(samples, 10000)
                    child.snapshot()  # Any real API failure fails the regression, without a retry.
                    samples += 1
                self.assertGreater(samples, 0)
                self.assertFalse(descendants & {row["pid"] for row in child.snapshot()})
                result = child.stop()
                self.assertTrue(result["confirmed_stopped"], result)
                (root / "err").unlink()  # No delayed cleanup or retry after confirmation.
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
