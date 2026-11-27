"""Composed catalogue routes on disposable authored inputs; no model calls."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

RUNTIME = Path(__file__).resolve().parents[2] / "desktop/runtime"
sys.path.insert(0, str(RUNTIME / "src"))
from lodestone_machinery import loom_operations as operations


class CatalogueRoutes(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="Catalogue composition ü ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.bridge = operations.OperationBridge(RUNTIME, self.root / "receipts")

    def invoke(self, operation, inputs, workspace, *, status="COMPLETED"):
        request = self.bridge.prepare(operation, inputs, workspace)
        result = self.bridge.run(request, approved_digest=request["digest"])
        self.assertEqual(result["status"], status, result)
        self.assertTrue(result["cleanup"]["confirmed_stopped"], result)
        self.assertTrue(result["output_capture_stopped"], result)
        self.assertFalse(result["execution"]["filesystem_network_sandbox"])
        self.assertFalse(result["semantic_learning_established"])
        if sys.platform == "win32":
            self.assertEqual(result["execution"]["ownership"], "windows_job")
            self.assertEqual(result["windows_job"]["windows_job_commit_limit_bytes"],
                             operations.MAX_JOB_MEMORY)
            self.assertEqual(result["windows_job"]["member_pids_after_stop"], [])
        receipt = Path(result["receipt_directory"])
        self.assertEqual(json.loads((receipt / "request.json").read_bytes()), request)
        self.assertEqual(json.loads((receipt / "result.json").read_bytes()), result)
        return request, result

    def test_windows_available_routes_are_exactly_the_reviewed_closures(self):
        expected = {"ecosystem.doctor", "ecosystem.context", "mailbox.validate-envelope",
            "release.verify-download", "loom.inference-memory-assess", "loom.inference-memory-skill",
            "bezoar.ingest", "bezoar.show", "memory.add", "memory.search", "memory.verify",
            "continuity.create", "continuity.verify", "pando.init", "pando.verify", "repository.check"}
        self.assertEqual(operations.WINDOWS_OPERATIONS, expected)
        with patch.object(operations.sys, "platform", "win32"):
            availability = operations._availability(RUNTIME)
        for operation in operations.SPECS:
            actual = availability.get(operation, {"status": "AVAILABLE"})["status"]
            self.assertEqual(actual, "AVAILABLE" if operation in expected else "UNAVAILABLE", operation)

    def test_notebook_add_search_verify_retains_unicode_and_inert_attribution(self):
        notebook = self.root / "notebook 窯"
        attribution = self.root / "attribution is not a selected input.txt"
        _, added = self.invoke("memory.add", {"title": "Authored fixture",
            "body": "Retained observation λ 窯", "source": str(attribution)}, notebook)
        row = json.loads(added["output"])
        self.assertTrue(row["created"])
        records = (notebook / "records.jsonl").read_bytes()
        self.assertIn("Retained observation λ 窯".encode(), records)
        for operation, inputs in (("memory.search", {"query": "retained"}), ("memory.verify", {})):
            request, result = self.invoke(operation, inputs, notebook)
            self.assertIn(".local-memory.lock", request["effects"])
            if operation.endswith("search"):
                self.assertIn(row["id"], result["output"])
                self.assertIn("Retained observation λ 窯", result["output"])
            else:
                self.assertIn("Memory store verified.", result["output"])
        self.assertEqual((notebook / "records.jsonl").read_bytes(), records)
        self.assertFalse(attribution.exists())
        if os.name == "nt":
            self.assertTrue((notebook / ".local-memory.lock").is_file())
        self.assertEqual(len(self.bridge.history()), 3)

    def test_continuity_create_verify_and_drift_keep_original_checkpoint(self):
        sources = self.root / "sources λ"
        sources.mkdir()
        source = sources / "note.txt"
        raw = "Authored observation — 窯.\n".encode()
        source.write_bytes(raw)
        output = self.root / "checkpoint.json"
        workspace = self.root / "uncreated workspace"
        request, result = self.invoke("continuity.create", {"root": str(sources), "output": str(output),
            "task": "Authored fixture", "status": "Unresolved", "next_action": "Inspect retained source",
            "sources": ["note.txt"]}, workspace)
        self.assertEqual(request["input_sha256"][str(source)], hashlib.sha256(raw).hexdigest())
        created = json.loads(result["output"])
        self.assertEqual(created["status"], "CREATED")
        checkpoint = output.read_bytes()
        self.assertEqual(created["sha256"], hashlib.sha256(checkpoint).hexdigest())
        inputs = {"checkpoint": str(output), "root": str(sources)}
        request, verified = self.invoke("continuity.verify", inputs, workspace)
        self.assertEqual(request["input_sha256"][str(output)], created["sha256"])
        self.assertEqual(json.loads(verified["output"])["status"], "VERIFIED")
        source.write_bytes(b"A later independent revision.\n")
        _, drift = self.invoke("continuity.verify", inputs, workspace, status="FAILED")
        self.assertEqual(json.loads(drift["output"])["status"], "DRIFT")
        self.assertEqual(output.read_bytes(), checkpoint)
        self.assertFalse(workspace.exists())
        self.assertEqual(len(self.bridge.history()), 3)

    def test_repository_navigation_reads_selected_tree_and_retains_failed_result(self):
        project = self.root / "project λ"
        project.mkdir()
        readme = project / "README.md"
        readme.write_bytes(b"[retained source](note.txt)\n[remote](https://example.invalid/no-request)\n")
        target = project / "note.txt"
        target.write_bytes(b"Inert local source.\n")
        workspace = self.root / "uncreated workspace"
        before = {path.name: path.read_bytes() for path in project.iterdir()}
        _, passed = self.invoke("repository.check", {"root": str(project)}, workspace)
        report = json.loads(passed["output"])
        self.assertEqual((report["status"], report["files"], report["local_links"]), ("PASS", 1, 1))
        self.assertEqual({path.name: path.read_bytes() for path in project.iterdir()}, before)
        target.unlink()
        _, failed = self.invoke("repository.check", {"root": str(project)}, workspace, status="FAILED")
        self.assertEqual(json.loads(failed["output"])["failures"], [{"source": "README.md", "target": "note.txt"}])
        self.assertEqual(readme.read_bytes(), before["README.md"])
        self.assertFalse(workspace.exists())
        self.assertEqual(len(self.bridge.history()), 2)


if __name__ == "__main__":
    unittest.main()
