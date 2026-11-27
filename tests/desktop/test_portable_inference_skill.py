"""Exact inert skill export through the native catalogue; no inference."""
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

RUNTIME = Path(__file__).resolve().parents[2] / "desktop/runtime"
sys.path.insert(0, str(RUNTIME / "src"))
from lodestone_machinery.loom_operations import OperationBridge
from lodestone_machinery import loom_memory_cli as skill
from lodestone_machinery.records import canonical
from lodestone_nurarihyon import portable_files


class PortableInferenceSkill(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="Loom inert skill ü ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.output = self.root / "candidate 水.json"

    def command(self, output=None):
        return [sys.executable, "-B", str(RUNTIME / "lodestone.py"), "--repo", str(RUNTIME),
                "--workspace", str(self.root / "unused"), "loom-tools", "inference-memory",
                "candidate", "--output", str(output or self.output)]

    def test_actual_catalogue_exports_exact_two_artifacts_without_proposal_or_execution(self):
        bridge = OperationBridge(RUNTIME, self.root / "receipts")
        request = bridge.prepare("loom.inference-memory-skill", {"output": str(self.output)}, self.root / "unused")
        result = bridge.run(request, approved_digest=request["digest"])
        self.assertEqual(result["status"], "COMPLETED", result)
        self.assertTrue(result["cleanup"]["confirmed_stopped"], result)
        self.assertTrue(result["output_capture_stopped"])
        report = json.loads(result["output"])
        self.assertEqual(report["model_calls"], 0)
        self.assertFalse(report["native_proposal_created"])
        self.assertEqual(report["authority"], "NONE")
        raw = self.output.read_bytes()
        self.assertEqual(report["sha256"], hashlib.sha256(raw).hexdigest())
        candidate = json.loads(raw)
        self.assertEqual(raw, canonical(candidate))
        self.assertEqual(candidate["evidence_handles"], [])
        artifacts = {row["path"]: row["content"] for row in candidate["artifacts"]}
        self.assertEqual(artifacts, {"SKILL.md": (RUNTIME / skill.SKILL_PATH).read_text(encoding="utf-8"),
            "loom_inference_memory.py": (RUNTIME / skill.HELPER_PATH).read_text(encoding="utf-8")})
        self.assertFalse((self.root / "unused").exists())

    def test_direct_cli_collision_preserves_existing_destination_bytes(self):
        self.output.write_bytes(b"previous retained artifact")
        result = subprocess.run(self.command(), capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("FileExistsError", result.stderr)
        self.assertEqual(self.output.read_bytes(), b"previous retained artifact")

    def test_concurrent_exports_publish_one_complete_candidate_without_overwrite(self):
        children = [subprocess.Popen(self.command(), stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
        results = []
        try:
            for child in children:
                output, error = child.communicate(timeout=30)
                results.append((child.returncode, output, error))
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=5)
        self.assertEqual(sorted(row[0] for row in results), [0, 2], results)
        self.assertEqual(self.output.read_bytes(), canonical(skill.candidate_manifest(RUNTIME)))
        loser = next(row for row in results if row[0] == 2)
        self.assertIn(b"FileExistsError", loser[2])

    def test_failed_flush_never_publishes_candidate_or_reports_success(self):
        output, error = io.StringIO(), io.StringIO()
        with patch.object(portable_files.os, "fsync", side_effect=OSError("injected flush failure")), \
                redirect_stdout(output), redirect_stderr(error):
            status = skill.main(["candidate", "--output", str(self.output)], root=RUNTIME)
        self.assertEqual(status, 2)
        self.assertIn("injected flush failure", error.getvalue())
        self.assertEqual(output.getvalue(), "")
        self.assertFalse(self.output.exists())

    @unittest.skipUnless(os.name == "nt", "Native Windows export custody")
    def test_windows_direct_cli_rejects_raw_output_aliases(self):
        for suffix in (".", " ", ":stream"):
            result = subprocess.run(self.command(str(self.output) + suffix), capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("ambiguous path", result.stderr)
        self.assertFalse(self.output.exists())
        self.assertFalse((self.root / "unused").exists())

    @unittest.skipUnless(os.name == "nt", "Native Windows export custody")
    def test_windows_junction_output_parent_and_source_root_are_refused(self):
        for name, target in (("output alias", self.root), ("source alias", RUNTIME)):
            link = self.root / name
            result = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.addCleanup(os.rmdir, link)
            if name == "output alias":
                result = subprocess.run(self.command(link / self.output.name), capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("reparse", result.stderr)
            else:
                with self.assertRaises((ValueError, OSError)):
                    skill.candidate_manifest(link)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
