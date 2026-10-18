"""Restart-checksum regressions with temporary records and mocked host observations."""
import hashlib
import importlib.util
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[2]


class RestartTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="conscience restart ")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.home = self.base / "home"
        self.home.mkdir()
        home = patch.object(Path, "home", return_value=self.home)
        home.start()
        self.addCleanup(home.stop)
        spec = importlib.util.spec_from_file_location(
            "_conscience_restart_under_test", ROOT / "tools/ecosystem/conscience.py")
        self.c = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.c)
        self.policy = self.c.load_policy()
        self.workspace = self.base / "workspace"
        self.store = self.c.record_store(self.workspace)
        self.runtime = self.base / "runtime"
        self.runtime.mkdir()
        self.note = self.runtime / "RESTART-README-20261017.md"
        self.note.write_bytes(b"Temporary restart instructions.\n")
        self.sidecar = self.note.with_name(self.note.name + ".sha256")
        # All readiness observations and stores are temporary public fixtures.
        self.cli = SimpleNamespace(doctor=Mock(return_value={"status": "READY"}))

    def snapshot(self):
        return {str(path.relative_to(self.workspace)): path.read_bytes()
                for path in self.workspace.rglob("*.json")}

    def invoke(self, *extra_args):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(self.c, "load_policy", return_value=self.policy), \
                patch.object(self.c, "record_store", return_value=self.store), \
                patch.object(self.c, "load_module", return_value=self.cli), \
                patch.object(self.c, "temperature", return_value={"state": "WARM"}), \
                patch.object(self.c, "_check", return_value={"ok": True, "detail": "fixture"}), \
                redirect_stdout(out), redirect_stderr(err):
            code = self.c.main(["wake", "--runtime-dir", str(self.runtime), *extra_args],
                               root=self.base, workspace=self.workspace)
        return code, out.getvalue(), err.getvalue()

    def test_mismatch_fails_even_when_restart_check_is_not_scheduled(self):
        self.sidecar.write_text("0" * 64 + "  " + self.note.name + "\n")
        code, out, err = self.invoke()
        self.assertEqual(code, 2)
        self.assertEqual(err, "")
        result = json.loads(out)
        self.assertFalse(result["restart_readme_verified"])
        self.assertIn("restart_readme_checksum", result["failed_checks"])
        payload = self.store.read(result["handle"])["payload"]
        self.assertNotIn("restart_readme_present", payload["checks"]["chosen"])
        self.assertEqual(payload["shape"]["doctor_status"], "READY")
        self.assertFalse(payload["checks"]["results"]["restart_readme_checksum"]["ok"])

    def test_empty_sidecar_reports_error_and_publishes_nothing(self):
        original = self.store.publish("conscience", "waking", {"fixture": "original"})
        before = self.snapshot()
        for raw in (b"", b" \t\n"):
            with self.subTest(raw=raw):
                self.sidecar.write_bytes(raw)
                with patch.object(self.store, "publish", wraps=self.store.publish) as publish:
                    code, out, err = self.invoke()
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertEqual(json.loads(err), {"error": "Restart checksum sidecar is empty"})
                publish.assert_not_called()
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(self.c.previous_wakings(self.store), [original])

    def test_missing_sidecar_is_reported_as_failed_unverified_wake(self):
        self.assertFalse(self.sidecar.exists())
        code, out, err = self.invoke()
        self.assertEqual(code, 2)
        self.assertEqual(err, "")
        result = json.loads(out)
        self.assertIsNone(result["restart_readme_verified"])
        self.assertIn("restart_readme_checksum", result["failed_checks"])
        payload = self.store.read(result["handle"])["payload"]
        self.assertNotIn("restart_readme_present", payload["checks"]["chosen"])
        self.assertFalse(payload["checks"]["results"]["restart_readme_checksum"]["ok"])

    def test_valid_checksum_succeeds_and_preserves_previous_record(self):
        original = self.store.publish("conscience", "waking", {"fixture": "original"})
        before = self.snapshot()
        digest = hashlib.sha256(self.note.read_bytes()).hexdigest()
        self.sidecar.write_text(digest + "  " + self.note.name + "\n")
        code, out, err = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        result = json.loads(out)
        self.assertTrue(result["restart_readme_verified"])
        self.assertEqual(result["actor"], self.policy["actor_default"])
        self.assertEqual(self.store.read(result["handle"])["payload"]["actor"], self.policy["actor_default"])
        self.assertEqual(result["failed_checks"], [])
        self.assertEqual(result["history"]["last"], original)
        self.assertEqual(self.store.read(result["handle"])["links"], [original])
        self.assertEqual(self.c.previous_wakings(self.store), [original, result["handle"]])
        after = self.snapshot()
        self.assertEqual({name: after[name] for name in before}, before)
        self.assertEqual(len(after), len(before) + 1)

    def test_absent_restart_note_remains_normal(self):
        self.note.unlink()
        code, out, err = self.invoke()
        self.assertEqual((code, err), (0, ""))
        result = json.loads(out)
        self.assertEqual(result["restart_readme"], "ABSENT")
        self.assertEqual(result["failed_checks"], [])
        self.assertNotIn("restart_readme_checksum", result["checks"])
        self.assertEqual(result["actor"], "local-user")

    def test_explicit_actor_is_recorded_and_returned(self):
        digest = hashlib.sha256(self.note.read_bytes()).hexdigest()
        self.sidecar.write_text(digest + "\n")
        code, out, err = self.invoke("--actor", "fixture-reviewer")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        result = json.loads(out)
        self.assertEqual(result["actor"], "fixture-reviewer")
        self.assertEqual(self.store.read(result["handle"])["payload"]["actor"], "fixture-reviewer")
        self.assertEqual(self.policy["actor_default"], "local-user")


if __name__ == "__main__":
    unittest.main()
