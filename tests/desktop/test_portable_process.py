"""Native owned-worker lifecycle fixtures; no model, network or paid API calls."""
import json
import hashlib
from contextlib import redirect_stderr
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

RUNTIME = Path(__file__).resolve().parents[2] / "desktop/runtime"
sys.path.insert(0, str(RUNTIME / "src"))
from lodestone_machinery import portable_process as native
from lodestone_machinery import loom_runtime as runtime


TREE = '''
import json, os, subprocess, sys, time
from pathlib import Path
level = int(sys.argv[1])
if level:
    subprocess.Popen([sys.executable, __file__, str(level - 1)])
Path('pid-' + str(level)).write_text(str(os.getpid()))
while True:
    time.sleep(.1)
'''


class OwnedProcessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="Loom process ü ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def launch(self, code=TREE, args=("2",), cap=256 * 1024 * 1024):
        script = self.root / "child ü.py"
        script.write_text(code, encoding="utf-8")
        with (self.root / "out").open("wb") as output, (self.root / "err").open("wb") as error:
            child = native.launch_worker([sys.executable, str(script), *args], cwd=str(self.root),
                env=runtime._environment("ollama_qwen3_base"), stdout=output, stderr=error,
                max_memory_bytes=cap)
        self.addCleanup(self.cleanup, child)
        return child

    def cleanup(self, child):
        if isinstance(child, native.WindowsJobProcess) and child._job is None:
            return
        try:
            runtime._stop_group(child)
        finally:
            if isinstance(child, native.WindowsJobProcess):
                child.close()

    def ready(self):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            files = [self.root / ("pid-" + str(i)) for i in range(3)]
            try:
                return {int(path.read_text()) for path in files}
            except (FileNotFoundError, ValueError):
                time.sleep(.02)
        self.fail("Owned child/grandchild fixture did not become ready")

    def snapshot(self, child):
        return child.snapshot() if isinstance(child, native.WindowsJobProcess) else runtime._group_snapshot(child.pid)

    def test_native_child_and_grandchild_are_owned_and_cancelled(self):
        child = self.launch()
        pids = self.ready()
        # Windows can create additional native helper processes in the Job.
        # Every fixture descendant must be present; every Job member must stop.
        self.assertTrue(pids <= {row["pid"] for row in self.snapshot(child)})
        self.assertGreater(sum(row["rss_bytes"] for row in self.snapshot(child)), 0)
        if isinstance(child, native.WindowsJobProcess):
            evidence = child.memory_observation()
            image_digest = hashlib.sha256(os.path.normcase(os.path.normpath(sys.executable)).encode("utf-8")).hexdigest()
            matching = {row["pid"] for row in evidence["windows_job_sampled_members"]
                        if row["image_path_sha256"] == image_digest}
            self.assertTrue(pids <= matching, evidence)
            self.assertFalse(evidence["windows_job_member_evidence_truncated"])
        stopped = runtime._stop_group(child)
        self.assertTrue(stopped["confirmed_stopped"], stopped)
        self.assertIsNotNone(child.poll())
        self.assertFalse([row for row in self.snapshot(child) if not row["zombie"]])

    def test_parent_exit_leaves_descendants_owned_until_verified_stop(self):
        code = TREE.replace("while True:", "if level == 2:\n    raise SystemExit(0)\nwhile True:")
        child = self.launch(code)
        pids = self.ready()
        self.assertEqual(child.wait(timeout=5), 0)
        remaining = {row["pid"] for row in self.snapshot(child) if not row["zombie"]}
        self.assertTrue((pids - {child.pid}) <= remaining)
        self.assertNotIn(child.pid, remaining)
        self.assertTrue(runtime._stop_group(child)["confirmed_stopped"])
        self.assertFalse([row for row in self.snapshot(child) if not row["zombie"]])

    def supervised(self, *, cancelled=lambda: False, seconds=5, cap=256 * 1024 * 1024):
        script = self.root / "fixture.py"
        script.write_text(TREE, encoding="utf-8")
        bridge = object.__new__(runtime.LoomBridge)
        bridge._lock, bridge._unsafe_groups = threading.Lock(), set()
        config = {"python": sys.executable, "backend": "ollama_qwen3_base",
                  "wall_seconds": seconds, "max_rss_bytes": cap, "runs_dir": str(self.root)}
        original_launch = native.launch_worker
        def fixture_launch(command, **kwargs):
            # Replace only the model worker command; use the actual supervisor
            # and native process ownership, clocks, sampling and termination.
            return original_launch([sys.executable, str(script), "2"], **kwargs)
        with patch.object(runtime, "launch_worker", side_effect=fixture_launch):
            return bridge._worker(config, self.root, {"fixture": True}, cancelled)

    def test_supervisor_elapsed_timeout_kills_owned_tree_and_retains_reason(self):
        started = time.monotonic()
        result = self.supervised(seconds=1.5)
        observation = result["observation"]
        self.assertEqual(result["status"], "INTERRUPTED")
        self.assertEqual(observation["reason"], "wall_limit")
        self.assertGreaterEqual(observation["elapsed_seconds"], 1.5)
        self.assertLess(time.monotonic() - started, 10)
        self.assertTrue(observation["stop"]["confirmed_stopped"])
        self.assertEqual(json.loads((self.root / "supervisor.json").read_text()), result)

    def test_supervisor_user_cancel_after_grandchild_starts(self):
        result = self.supervised(cancelled=lambda: (self.root / "pid-0").exists())
        self.assertEqual(result["status"], "INTERRUPTED")
        self.assertEqual(result["observation"]["reason"], "cancelled")
        self.assertTrue(result["observation"]["stop"]["confirmed_stopped"])

    @unittest.skipUnless(os.name == "posix", "POSIX sampled RSS bound")
    def test_posix_supervisor_rss_stop_is_measured_not_inferred(self):
        result = self.supervised(cap=1)
        self.assertEqual(result["observation"]["reason"], "rss_limit")
        self.assertGreater(result["observation"]["observed_group_peak_rss_bytes"], 1)
        self.assertTrue(result["observation"]["stop"]["confirmed_stopped"])
        self.assertEqual(runtime._resource_failure(result, result, {"max_rss_bytes": 1})["resource"], "max_rss_bytes")

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux native procfs accounting")
    def test_linux_sampling_starts_no_probe_process(self):
        child = self.launch()
        self.ready()
        with patch.object(subprocess, "run", side_effect=AssertionError("probe process forbidden")):
            self.assertEqual(len(runtime._group_snapshot(child.pid)), 3)

    @unittest.skipUnless(sys.platform == "win32", "Native Windows Job memory contract")
    def test_windows_job_refuses_committed_memory_allocation(self):
        child = self.launch('''
import ctypes, json
from pathlib import Path
kernel = ctypes.WinDLL('kernel32', use_last_error=True)
kernel.VirtualAlloc.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32, ctypes.c_uint32]
kernel.VirtualAlloc.restype = ctypes.c_void_p
kernel.VirtualFree.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32]
kernel.VirtualFree.restype = ctypes.c_int
value = kernel.VirtualAlloc(None, 512 * 1024 * 1024, 0x3000, 0x4)
error = ctypes.get_last_error()
if value:
    kernel.VirtualFree(value, 0, 0x8000)
Path('allocation.json').write_text(json.dumps({'allocated': bool(value), 'last_error': error}))
''', args=(), cap=128 * 1024 * 1024)
        self.assertEqual(child.wait(timeout=10), 0, (self.root / "err").read_text())
        allocation = json.loads((self.root / "allocation.json").read_text())
        self.assertFalse(allocation["allocated"], allocation)
        self.assertIn(allocation["last_error"], (8, 1455), allocation)  # insufficient memory / commitment limit
        evidence = child.memory_observation()
        self.assertEqual(evidence["windows_job_commit_limit_bytes"], 128 * 1024 * 1024)
        self.assertGreaterEqual(evidence["windows_job_peak_commit_bytes"], 0)
        # The native run showed PeakJobMemoryUsed rising above the cap even
        # when allocation was denied. Preserve it as telemetry, not an RSS cap.
        # Completion-port notifications are documented as best effort. A denied
        # actual allocation proves this fixture's cap even without a message.

    @unittest.skipUnless(sys.platform == "win32", "Native Windows suspended launch")
    def test_windows_failed_job_assignment_never_executes_worker(self):
        original = native._WindowsAPI
        class RejectAssignment(original):
            def __init__(self):
                super().__init__()
                def reject(*args):
                    native.ctypes.set_last_error(5)
                    return False
                self.kernel.AssignProcessToJobObject = reject
        with patch.object(native, "_WindowsAPI", RejectAssignment):
            with self.assertRaises(OSError):
                self.launch("from pathlib import Path; Path('escaped').write_text('ran')", args=())
        self.assertFalse((self.root / "escaped").exists())

    @unittest.skipUnless(sys.platform == "win32", "Native Windows Job last-handle semantics")
    def test_windows_closing_owner_job_kills_descendants(self):
        child = self.launch()
        pids = self.ready()
        handles = [child.api.check(child.api.kernel.OpenProcess(0x100000, False, pid)) for pid in pids]
        try:
            child.close()
            for handle in handles:
                self.assertEqual(child.api.kernel.WaitForSingleObject(handle, 5000), 0)
        finally:
            for handle in handles:
                child.api.kernel.CloseHandle(handle)


class RuntimeBoundaryTests(unittest.TestCase):
    def test_linux_hidepid_foreign_record_is_skipped_but_own_denial_fails(self):
        from unittest.mock import MagicMock
        fields = ["S", "1", "999"] + ["0"] * 18 + ["2"]
        visible = ("112 (fixture with spaces) " + " ".join(fields)).encode()
        for owner in (1000, 1001):
            entries = [SimpleNamespace(name="111", path="/proc/111",
                       stat=lambda **kwargs: SimpleNamespace(st_uid=owner)),
                       SimpleNamespace(name="112", path="/proc/112")]
            enumeration = MagicMock()
            enumeration.__enter__.return_value = iter(entries)
            def opened(path, *args):
                if path == "/proc/111/stat":
                    raise PermissionError("Authored hidepid fixture")
                return io.BytesIO(visible)
            with self.subTest(owner=owner), patch.object(sys, "platform", "linux"), \
                    patch.object(native.os, "scandir", return_value=enumeration), \
                    patch.object(native.os, "geteuid", return_value=1001, create=True), \
                    patch.object(native.os, "sysconf", return_value=4096, create=True), \
                    patch("builtins.open", side_effect=opened):
                if owner == 1001:
                    with self.assertRaises(PermissionError):
                        native.linux_group_snapshot(999)
                else:
                    self.assertEqual(native.linux_group_snapshot(999),
                        [{"pid": 112, "rss_bytes": 8192, "zombie": False}])

    def test_windows_optional_handoff_and_training_refuse_before_writes(self):
        from lodestone_machinery.loom_handoff import LoomHandoff
        from lodestone_machinery.loom_training import TrainingSupervisor
        with tempfile.TemporaryDirectory() as temporary, patch.object(sys, "platform", "win32"):
            root = Path(temporary) / "not-created"
            with self.assertRaisesRegex(OSError, "handoff execution is unavailable on Windows"):
                LoomHandoff(None, root)
            with self.assertRaisesRegex(OSError, "Training execution is unavailable on Windows"):
                TrainingSupervisor().run({}, {}, {})
            self.assertFalse(root.exists())

    def test_default_worker_imports_without_resource(self):
        code = '''
import sys
sys.path.insert(0, sys.argv[1])
class Block:
    def find_spec(self, name, *args):
        if name == 'resource':
            raise ImportError('POSIX resource is unavailable')
sys.meta_path.insert(0, Block())
from lodestone_machinery import loom_runtime_worker, loom_runtime
'''
        completed = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(RUNTIME / "src")],
            capture_output=True, text=True, timeout=15)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_windows_optional_learned_support_refuses_before_io_or_model_loading(self):
        from lodestone_machinery import loom_support_cli as support
        arguments = ["--workspace", "unused", "--model-dir", "unused", "--output", "unused"]
        for name in ("parent", "selector", "request", "model-manifest", "confirmation", "run-freeze"):
            arguments += ["--" + name, "unused", "--expected-" + name + "-sha256", "0" * 64]
        error = io.StringIO()
        with patch.object(sys, "platform", "win32"), redirect_stderr(error), \
                patch.object(support, "plain_path", side_effect=AssertionError("unexpected path operation")) as paths, \
                patch.object(support, "_read", side_effect=AssertionError("unexpected input read")) as reads, \
                patch.object(support.loom_nli, "LocalNLI", side_effect=AssertionError("unexpected model loading")) as model:
            self.assertEqual(support.main(arguments), 2)
        self.assertIn("unavailable on Windows", json.loads(error.getvalue())["message"])
        paths.assert_not_called()
        reads.assert_not_called()
        model.assert_not_called()

    def test_worker_environment_drops_provider_credentials_and_custom_path(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "fixture", "ANTHROPIC_API_KEY": "fixture",
                "HTTP_PROXY": "fixture", "OLLAMA_HOST": "fixture", "PATH": "untrusted"}):
            env = runtime._environment("ollama_qwen3_base")
        for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "HTTP_PROXY", "OLLAMA_HOST"):
            self.assertNotIn(name, env)
        self.assertNotIn("untrusted", env["PATH"])
        if sys.platform == "win32":
            self.assertTrue(Path(env["SystemRoot"]).is_dir())

    def test_windows_optional_execution_refuses_before_launch(self):
        from lodestone_machinery.loom_operations import OperationBridge
        with tempfile.TemporaryDirectory() as root:
            bridge = OperationBridge(RUNTIME, Path(root) / "receipts")
            with patch.object(sys, "platform", "win32"), patch.object(subprocess, "Popen") as launch:
                with self.assertRaisesRegex(OSError, "unavailable on Windows"):
                    bridge.prepare("pando.init", {}, root)
                with self.assertRaisesRegex(OSError, "unavailable on Windows"):
                    bridge.run({"operation": "pando.init", "inputs": {}, "workspace": root,
                                "id": "a" * 32, "digest": "fixture"}, approved_digest="fixture")
            launch.assert_not_called()

    def test_windows_commit_refusal_is_not_mislabelled_as_measured_rss_failure(self):
        result = {"status": "INTERRUPTED", "observation": {"reason": "windows_job_memory_limit",
                  "stop": {"confirmed_stopped": True}, "windows_job_memory_limit_notified": True}}
        self.assertIsNone(runtime._resource_failure(result, result, {"max_rss_bytes": 1}))


if __name__ == "__main__":
    unittest.main()
