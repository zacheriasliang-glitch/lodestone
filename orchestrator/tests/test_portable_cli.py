"""Fresh-project regression tests for the portable command boundary."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ORCHESTRATOR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ORCHESTRATOR))
from engine import legacy_engine


class PortableCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lodestone-orchestrator-cli-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.project = self.base / "project"
        shutil.copytree(ORCHESTRATOR / "examples/project", self.project)

    def run_cli(self, *args, root=True):
        command = [sys.executable, "-B", str(ORCHESTRATOR / "bootstrap.py")]
        if root:
            command += ["--root", str(self.project)]
        return subprocess.run(command + list(args), cwd=self.project, text=True,
                              capture_output=True, timeout=15,
                              env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})

    def snapshot(self):
        return {p.relative_to(self.project).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.project.rglob("*") if p.is_file()}

    def test_root_is_required_before_any_observation_or_write(self):
        before = self.snapshot()
        result = self.run_cli("--experiment", "001", "--goal", "Audit demo", root=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--root", result.stderr)
        self.assertEqual(before, self.snapshot())

    def test_dry_test_is_read_only_with_package_schemas(self):
        before = self.snapshot()
        result = self.run_cli("--experiment", "001", "--goal", "Audit demo", "--dry-test")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("DRY_TEST_RESULT: PASS", result.stdout)
        self.assertEqual(before, self.snapshot())
        self.assertFalse((self.project / "orchestrator").exists())

    def test_new_project_planning_writes_only_two_proposals(self):
        before = self.snapshot()
        result = self.run_cli("--experiment", "001", "--goal", "Audit demo")
        self.assertEqual(result.returncode, 0, result.stderr)
        after = self.snapshot()
        self.assertEqual(before, {p: after[p] for p in before})
        created = set(after) - set(before)
        self.assertEqual(len(created), 2)
        self.assertTrue(all(p.startswith("orchestrator/output/") for p in created))
        decision = json.loads(next((self.project / "orchestrator/output").glob("*decision.json")).read_text())
        self.assertEqual(decision["status"], "PROPOSED")

    def test_saved_host_root_is_ignored(self):
        policy = json.loads((ORCHESTRATOR / "config/orchestrator.config.json").read_text())
        other = self.base / "other-project"
        policy["repositoryRoot"] = str(other)
        config = self.base / "policy.json"
        config.write_text(json.dumps(policy))
        result = self.run_cli("--config", str(config), "--experiment", "001", "--goal", "Audit demo")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(other.exists())
        self.assertTrue((self.project / "orchestrator/output").is_dir())

    def test_legacy_dry_test_uses_requested_experiment(self):
        before = self.snapshot()
        result = self.run_cli("--engine", "legacy", "--experiment", "001", "--goal", "Audit demo", "--dry-test")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("DRY_TEST_EXPERIMENT: 001", result.stdout)
        self.assertEqual(before, self.snapshot())

    def test_legacy_never_overwrites_an_existing_packet(self):
        policy = json.loads((ORCHESTRATOR / "config/orchestrator.config.json").read_text())
        with mock.patch.object(legacy_engine, "datetime") as clock:
            clock.now.return_value.strftime.return_value = "20260101T000000Z"
            first = legacy_engine.run_legacy(self.project, policy, "001", "Audit demo")
            path = Path(first["Packet path"])
            before = path.read_bytes()
            with self.assertRaises(FileExistsError):
                legacy_engine.run_legacy(self.project, policy, "001", "Different goal")
            self.assertEqual(before, path.read_bytes())

    def test_output_symlink_cannot_redirect_plans_outside_project(self):
        outside = self.base / "outside"
        outside.mkdir()
        try:
            (self.project / "orchestrator").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("directory symlinks unavailable")
        for engine in ("kernel20", "legacy"):
            result = self.run_cli("--engine", engine, "--experiment", "001", "--goal", "Audit demo")
            self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(list(outside.iterdir()), [])

    def test_bridge_creates_proposals_without_a_transport(self):
        before = self.snapshot()
        result = self.run_cli("--bridge-preflight", "--experiment", "001", "--goal", "Audit demo")
        self.assertEqual(result.returncode, 0, result.stderr)
        after = self.snapshot()
        self.assertEqual(before, {p: after[p] for p in before})
        self.assertEqual(len(set(after) - set(before)), 3)
        envelope = json.loads(next((self.project / "orchestrator/output").glob("*task-envelope.json")).read_text())
        self.assertEqual(envelope["authority"], "PROPOSAL_ONLY")
        self.assertIn("HOST_APPROVAL_REQUIRED", result.stdout)


if __name__ == "__main__":
    unittest.main()
