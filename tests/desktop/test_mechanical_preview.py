"""Offline CI routing and artifact binding; no installation, GUI or models."""
from contextlib import redirect_stderr
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("desktop_mechanical_check", ROOT / "tools/desktop_mechanical_check.py")
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)
COMMIT = "a" * 40


class MechanicalPreviewTests(unittest.TestCase):
    def test_manual_modes_require_exact_head_and_cannot_conflict(self):
        for mechanical, prior, expected in ((True, False, "mechanical_only"), (False, True, "prior_local_evidence")):
            self.assertEqual(check.select_mode("workflow_dispatch", mechanical, prior, COMMIT, COMMIT, COMMIT), expected)
            for event, head, github, checkout in (("pull_request", COMMIT, COMMIT, COMMIT),
                    ("workflow_dispatch", "", COMMIT, COMMIT),
                    ("workflow_dispatch", COMMIT, "b" * 40, COMMIT),
                    ("workflow_dispatch", COMMIT, COMMIT, "b" * 40)):
                with self.subTest(event=event, head=head, github=github, checkout=checkout), self.assertRaises(ValueError):
                    check.select_mode(event, mechanical, prior, head, github, checkout)
        with self.assertRaisesRegex(ValueError, "mutually exclusive"):
            check.select_mode("workflow_dispatch", True, True, COMMIT, COMMIT, COMMIT)

    def test_pull_requests_are_mechanical_and_manual_legacy_is_distinct(self):
        self.assertEqual(check.select_mode("pull_request", False, False, "", COMMIT, COMMIT), "mechanical_only")
        self.assertEqual(check.select_mode("workflow_dispatch", False, False, "", COMMIT, COMMIT), "inference_smoke")
        for github, checkout in ((COMMIT, "b" * 40), ("", ""), ("short", "short")):
            with self.subTest(github=github, checkout=checkout), self.assertRaisesRegex(ValueError, "identities must match"):
                check.select_mode("pull_request", False, False, "", github, checkout)
        self.assertFalse(check.flag(""))
        self.assertFalse(check.flag("false"))
        self.assertTrue(check.flag("true"))
        with self.assertRaises(ValueError):
            check.flag("yes")

    def test_commit_binding_rejects_dirty_or_different_checkouts(self):
        with patch.object(check.subprocess, "run", side_effect=[SimpleNamespace(stdout=COMMIT + "\n"),
                                                               SimpleNamespace(stdout="")]):
            self.assertEqual(check.checkout_commit("/fixture", COMMIT), COMMIT)
        for dirty in (" M desktop/build.py\n", "?? desktop/runtime/untracked.py\n"):
            with patch.object(check.subprocess, "run", side_effect=[SimpleNamespace(stdout=COMMIT),
                                                                   SimpleNamespace(stdout=dirty)]), \
                 self.assertRaisesRegex(ValueError, "uncommitted or untracked"):
                check.checkout_commit("/fixture", COMMIT)
        with patch.object(check.subprocess, "run", return_value=SimpleNamespace(stdout="b" * 40)), \
             self.assertRaisesRegex(ValueError, "changed from selected commit"):
            check.checkout_commit("/fixture", COMMIT)

    def fixture(self, base):
        base = base.resolve()
        source, payload, preview = base / "source", base / "download", base / "preview"
        files = {"gui/dialog.py": b"# Authored identity fixture\n", "runtime/core.py": b"value = 1\n"}
        for root in (source / "desktop", payload):
            for name, content in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
        document = {"schema": "lodestone-desktop-payload/v1", "version": "fixture",
                    "files": {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}}
        (payload / "MANIFEST.json").write_text(json.dumps(document))
        identity = hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        release = preview / "releases" / identity
        shutil.copytree(payload, release)
        (preview / "ACTIVE.json").write_text(json.dumps({"release_id": identity, "release": str(release)}))
        return source, payload, preview, release

    def test_current_source_download_and_installed_bytes_must_all_match(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, payload, preview, release = self.fixture(Path(temporary))
            result = check.installed_identity(source, payload, preview, COMMIT)
            self.assertEqual(result["source_commit"], COMMIT)
            self.assertEqual(result["files_verified"], 2)
            self.assertEqual(result["model_calls_in_this_check"], 0)
            for root in (source / "desktop", payload, release):
                path = root / "runtime/core.py"
                original = path.read_bytes()
                path.write_text("changed = True\n")
                with self.subTest(root=root), self.assertRaises(ValueError):
                    check.installed_identity(source, payload, preview, COMMIT)
                path.write_bytes(original)
            (release / "unlisted.py").write_text("unexpected = True\n")
            with self.assertRaisesRegex(ValueError, "Unlisted"):
                check.installed_identity(source, payload, preview, COMMIT)

    def test_foreign_release_pointer_and_manifest_drift_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, payload, preview, release = self.fixture(Path(temporary))
            active = preview / "ACTIVE.json"
            original = active.read_text()
            value = json.loads(original)
            value["release"] = str(payload)
            active.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, "escaped"):
                check.installed_identity(source, payload, preview, COMMIT)
            active.write_text(original)
            document = json.loads((release / "MANIFEST.json").read_text())
            document["version"] = "foreign-version"
            (release / "MANIFEST.json").write_text(json.dumps(document))
            with self.assertRaisesRegex(ValueError, "manifest differs"):
                check.installed_identity(source, payload, preview, COMMIT)

    def test_gui_gate_enables_installed_root_and_rejects_skipped_coverage(self):
        class PassingFixture(unittest.TestCase):
            def runTest(self):
                pass
        class SkippedFixture(unittest.TestCase):
            @unittest.skip("Authored coverage gap")
            def runTest(self):
                pass
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ), redirect_stderr(io.StringIO()):
            root = Path(temporary).resolve()
            for test, expected in ((PassingFixture(), "PASSED"), (SkippedFixture(), "FAILED")):
                with patch.object(check.unittest.defaultTestLoader, "discover", return_value=unittest.TestSuite([test])):
                    if expected == "PASSED":
                        check.gui_checks(root / "tests", root / "installed", root / "evidence")
                    else:
                        with self.assertRaisesRegex(ValueError, "skipped"):
                            check.gui_checks(root / "tests", root / "installed", root / "evidence")
                result = json.loads((root / "evidence/installed-gui-check.json").read_text())
                self.assertEqual(result["status"], expected)
                self.assertEqual(os.environ["LODESTONE_GUI_EVENT_TESTS"], "1")
                self.assertEqual(os.environ["LODESTONE_DESKTOP_TEST_PAYLOAD"], str(root / "installed"))


if __name__ == "__main__":
    unittest.main()
