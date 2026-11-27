"""Native catalogue ownership, bounds and retention; no model or network calls."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

RUNTIME = Path(__file__).resolve().parents[2] / "desktop/runtime"
sys.path.insert(0, str(RUNTIME / "src"))
from lodestone_machinery import loom_operations as operations
from lodestone_machinery import loom_runtime
from lodestone_machinery.bezoar_learning import LearningMemory


TREE = """
import os, subprocess, sys, time
from pathlib import Path
level = int(sys.argv[-1]) if sys.argv[-1] in ("0", "1") else 2
if level:
    subprocess.Popen([sys.executable, "-B", __file__, str(level - 1)])
Path("pid-" + str(level)).write_text(str(os.getpid()), encoding="ascii")
while True:
    time.sleep(.1)
"""


class PortableOperationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="Loom catalogue ü ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.package = self.root / "fixture package"
        self.package.mkdir()
        (self.package / "src").mkdir()
        self.script = self.package / "lodestone.py"
        self.script.write_text("print('fixture')\n", encoding="utf-8")

    def bridge(self, real=False, **bounds):
        return operations.OperationBridge(RUNTIME if real else self.package,
            self.root / "receipts", **bounds)

    def run_fixture(self, code, **bounds):
        self.script.write_text(code, encoding="utf-8")
        bridge = self.bridge(**bounds)
        request = bridge.prepare("ecosystem.context", {}, self.root / "workspace")
        return bridge, request, bridge.run(request, approved_digest=request["digest"])

    def assert_closed(self, result):
        self.assertTrue(result["cleanup"]["confirmed_stopped"], result)
        self.assertTrue(result["output_capture_stopped"], result)
        self.assertFalse(result["execution"]["filesystem_network_sandbox"])
        if sys.platform == "win32":
            self.assertEqual(result["execution"]["ownership"], "windows_job")
            self.assertEqual(result["windows_job"]["windows_job_commit_limit_bytes"],
                             operations.MAX_JOB_MEMORY)
            self.assertEqual(result["windows_job"]["member_pids_after_stop"], [])
        else:
            self.assertEqual(result["execution"]["ownership"], "posix_process_group")
            self.assertFalse([row for row in loom_runtime._group_snapshot(result["pid"])
                              if not row["zombie"]])

    def test_real_native_doctor_and_documentation_results_are_retained(self):
        bridge = self.bridge(real=True)
        workspace = self.root / "workspace not created"
        for operation, schema in (("ecosystem.doctor", "lodestone-doctor/v2"),
                                  ("ecosystem.context", "lodestone-context/v2")):
            request = bridge.prepare(operation, {}, workspace)
            result = bridge.run(request, approved_digest=request["digest"])
            self.assertEqual(result["status"], "COMPLETED", result)
            self.assertEqual(json.loads(result["output"])["schema"], schema)
            self.assert_closed(result)
            self.assertEqual(json.loads((Path(result["receipt_directory"]) / "request.json").read_bytes()), request)
            self.assertEqual(json.loads((Path(result["receipt_directory"]) / "result.json").read_bytes()), result)
        self.assertFalse(workspace.exists())
        self.assertEqual(len(bridge.history()), 2)

    def test_output_pipe_keeps_exact_limit_and_stops_overflow(self):
        for length in (4096, 1000000):
            # Separate receipts avoid reusing an approved mutation request.
            bridge, request, result = self.run_fixture(
                "import os\nos.write(1, b'x' * " + str(length) + ")\n", max_bytes=4096)
            self.assertEqual(result["status"], "COMPLETED" if length == 4096 else "OUTPUT_LIMIT", result)
            self.assertEqual(result["output_bytes"], 4096)
            self.assertEqual(result["output_sha256"], hashlib.sha256(b"x" * 4096).hexdigest())
            self.assert_closed(result)

    def test_native_child_grandchild_cancellation_stops_all_members(self):
        self.script.write_text(TREE, encoding="utf-8")
        bridge = self.bridge(max_seconds=15)
        request = bridge.prepare("ecosystem.context", {}, self.root / "workspace")
        cancel, box = threading.Event(), {}
        def run():
            try:
                box["result"] = bridge.run(request, approved_digest=request["digest"], cancel_event=cancel)
            except BaseException as exc:
                box["error"] = exc
        worker = threading.Thread(target=run)
        worker.start()
        pids = set()
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    pids = {int((self.package / ("pid-" + str(level))).read_text()) for level in range(3)}
                    break
                except (OSError, ValueError):
                    time.sleep(.02)
            self.assertEqual(len(pids), 3, box)
        finally:
            cancel.set()
            worker.join(timeout=15)
        self.assertFalse(worker.is_alive())
        self.assertNotIn("error", box)
        result = box["result"]
        self.assertEqual(result["status"], "INTERRUPTED", result)
        self.assert_closed(result)
        if sys.platform == "win32":
            self.assertTrue(pids <= set(result["cleanup"]["member_pids_before_stop"]), result)

    def test_native_elapsed_timeout_stops_worker_and_keeps_partial_output(self):
        _, _, result = self.run_fixture(
            "import time\nprint('partial before timeout', flush=True)\ntime.sleep(60)\n",
            max_seconds=.75)
        self.assertEqual(result["status"], "TIMEOUT", result)
        self.assertIn("partial before timeout", result["output"])
        self.assertLess(result["finished_at"] - result["started_at"], 12)
        self.assert_closed(result)

    def test_changed_source_or_input_refuses_before_launch(self):
        marker = self.root / "must not execute"
        self.script.write_text("from pathlib import Path\nPath(" + repr(str(marker)) + ").touch()\n")
        source = self.root / "input ü.txt"
        source.write_text("first", encoding="utf-8")
        descriptor = operations.spec("ecosystem", "Fixture selected input", ["context"],
                                      {"source": ("file", "--source")})
        bridge = self.bridge()
        with patch.dict(operations.SPECS, {"ecosystem.context": descriptor}):
            request = bridge.prepare("ecosystem.context", {"source": str(source)}, self.root / "workspace")
            source.write_text("changed", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "changed after preview"):
                bridge.run(request, approved_digest=request["digest"])
            source.write_text("first", encoding="utf-8")
            self.script.write_text("raise SystemExit('changed package')\n")
            with self.assertRaisesRegex(ValueError, "changed after preview"):
                bridge.run(request, approved_digest=request["digest"])
        self.assertFalse(marker.exists())
        self.assertFalse((bridge.receipts_dir / request["id"]).exists())

    def test_failed_operation_retains_original_receipt_and_native_advisory_evidence(self):
        memory = LearningMemory(self.root / "memory")
        evidence = memory.evidence({"source": "catalogue fixture", "event_id": "first",
            "observed_at": "2026-11-28T00:00:00Z", "text": "Scripted operation failure.",
            "attribution": "Synthetic test, not model evidence."})
        seed = memory.seed({"id": "catalogue-fixture", "correction": "Keep actual failed attempts.",
            "conditions": ["Scripted operation test."], "frame_limits": ["No semantic learning claim."],
            "evidence_handles": [evidence], "author": "catalogue fixture"})
        config = self.root / "active.json"
        config.write_text(json.dumps({"schema": "lodestone-learning-active/v1", "required": True,
            "memory_workspace": str(memory.workspace), "seed_handles": [seed]}))
        bridge, request, result = self.run_fixture(
            "print('retained failure', flush=True)\nraise SystemExit(7)\n", learning_config=config)
        self.assertEqual(result["status"], "FAILED", result)
        self.assertEqual(result["returncode"], 7)
        self.assert_closed(result)
        retained = result["failure_learning"]
        self.assertFalse(retained["provider_called"])
        self.assertFalse(retained["outcome_created"])
        observed = json.loads(memory.read_evidence(retained["evidence_handle"])["text"])
        self.assertEqual(observed["observation"]["output_sha256"], result["output_sha256"])
        self.assertEqual(observed["observation"]["request_digest"], request["digest"])
        self.assertEqual([row["handle"] for row in memory.preparation_lessons("loom.tool-operation")],
                         [retained["seed_handle"]])
        original = (Path(result["receipt_directory"]) / "result.json").read_bytes()
        with self.assertRaises(FileExistsError):
            bridge.run(request, approved_digest=request["digest"])
        self.assertEqual((Path(result["receipt_directory"]) / "result.json").read_bytes(), original)

    def test_windows_only_reviewed_operation_closures_are_available(self):
        with patch.object(sys, "platform", "win32"):
            rows = operations.catalogue(RUNTIME)["operations"]
            self.assertEqual({row["id"] for row in rows if row["status"] == "AVAILABLE"},
                             operations.WINDOWS_OPERATIONS)
            with self.assertRaisesRegex(OSError, "dependency/custody"):
                self.bridge().prepare("pando.init", {}, self.root / "workspace")
        self.assertFalse((self.root / "workspace").exists())

    @unittest.skipUnless(os.name == "nt", "Native Windows path custody")
    def test_windows_raw_ambiguous_names_are_refused_before_absolute_normalization(self):
        source = self.root / "input.txt"
        source.write_bytes(b"original")
        for suffix in (".", " ", ":stream"):
            with self.subTest(suffix=suffix):
                for invoke in (
                        lambda: operations.OperationBridge(str(self.package) + suffix, self.root / "receipts"),
                        lambda: operations.OperationBridge(self.package, str(self.root) + suffix + "/receipts"),
                        lambda: self.bridge().prepare("ecosystem.context", {}, str(self.root) + suffix + "/workspace")):
                    with self.assertRaises((ValueError, OSError)):
                        invoke()
                descriptor = operations.spec("ecosystem", "Fixture raw selected file", ["context"],
                                              {"selected": ("file", "--selected")})
                with patch.dict(operations.SPECS, {"ecosystem.context": descriptor}), \
                        self.assertRaises((ValueError, OSError)):
                    self.bridge().prepare("ecosystem.context", {"selected": str(source) + suffix},
                                          self.root / "workspace")
        self.assertEqual(source.read_bytes(), b"original")
        self.assertFalse((self.root / "receipts").exists())

    @unittest.skipUnless(os.name == "nt", "Native Windows path custody")
    def test_windows_selected_junctions_are_refused_before_resolution_or_mutation(self):
        target = self.root / "target"
        target.mkdir()
        (target / "input.txt").write_bytes(b"original")
        link = self.root / "chosen junction"
        result = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.addCleanup(os.rmdir, link)
        for operation in (
                lambda: operations.OperationBridge(link, self.root / "receipts"),
                lambda: operations.OperationBridge(self.package, link / "receipts"),
                lambda: operations.OperationBridge(self.package, self.root / "receipts", learning_config=link / "active.json"),
                lambda: self.bridge().prepare("ecosystem.context", {}, link / "workspace")):
            with self.subTest(operation=operation), self.assertRaises((ValueError, OSError)):
                operation()
        for kind, chosen in (("file", link / "input.txt"), ("directory", link),
                             ("new-file", link / "output.json")):
            descriptor = operations.spec("ecosystem", "Fixture selected path", ["context"],
                                          {"selected": (kind, "--selected")})
            with patch.dict(operations.SPECS, {"ecosystem.context": descriptor}), \
                    self.subTest(kind=kind), self.assertRaises((ValueError, OSError)):
                self.bridge().prepare("ecosystem.context", {"selected": str(chosen)}, self.root / "workspace")
        descriptor = operations.spec("ecosystem", "Fixture source selection", ["context"],
            {"sources": ("sources", "--source"), "root": ("directory", "--root")})
        with patch.dict(operations.SPECS, {"ecosystem.context": descriptor}), \
                self.assertRaises((ValueError, OSError)):
            self.bridge().prepare("ecosystem.context", {"sources": ["input.txt"], "root": str(link)},
                                  self.root / "workspace")
        self.assertEqual([(p.name, p.read_bytes()) for p in target.iterdir()], [("input.txt", b"original")])
        self.assertFalse((self.root / "receipts").exists())
        self.assertFalse((self.root / "workspace").exists())

    @unittest.skipUnless(os.name == "nt", "Native Windows path custody")
    def test_windows_junction_replacement_refuses_source_pin_read(self):
        original = self.package / "src"
        moved = self.root / "moved source"
        original.rename(moved)
        (moved / "fixture.py").write_text("original source")
        result = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(original), str(moved)],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.addCleanup(os.rmdir, original)
        with self.assertRaises((ValueError, OSError)):
            self.bridge().prepare("ecosystem.context", {}, self.root / "workspace")


if __name__ == "__main__":
    unittest.main()

