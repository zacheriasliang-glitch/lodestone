"""Native release verification reads explicit bytes without publishing anything."""
import hashlib
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
from lodestone_machinery import loom_optional_operations as optional


class PortableReleaseReader(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="Loom release reader ü ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.package = self.root / "selected release"
        self.package.mkdir()
        self.body = "Inert released bytes 水\n".encode()
        (self.package / "readme.txt").write_bytes(self.body)
        (self.package / "empty").write_bytes(b"")
        self.manifest = self.package / "CONTENT_SHA256.json"
        self.manifest.write_text(json.dumps({"readme.txt": hashlib.sha256(self.body).hexdigest(),
                                             "empty": hashlib.sha256(b"").hexdigest()}))
        self.inputs = {"package": str(self.package), "manifest": str(self.manifest)}
        self.workspace = self.root / "unused workspace"

    def invoke(self):
        bridge = OperationBridge(RUNTIME, self.root / "receipts")
        request = bridge.prepare("release.verify-download", self.inputs, self.workspace)
        result = bridge.run(request, approved_digest=request["digest"])
        self.assertTrue(result["cleanup"]["confirmed_stopped"], result)
        self.assertTrue(result["output_capture_stopped"], result)
        self.assertFalse(self.workspace.exists())
        return result

    def direct(self):
        return optional.execute("release.verify-download", self.inputs, root=RUNTIME, workspace=self.workspace)

    def snapshot(self):
        return {p.name: p.read_bytes() for p in self.package.iterdir()}

    def test_actual_catalogue_verifies_exact_files_and_standalone_contract_agrees(self):
        before = self.snapshot()
        result = self.invoke()
        self.assertEqual(result["status"], "COMPLETED", result)
        report = json.loads(result["output"])
        self.assertEqual(report["status"], "VERIFIED")
        self.assertEqual(report["files"], 2)
        self.assertEqual(report["bytes_read"], len(self.body))
        self.assertEqual(report["failures"], [])
        standalone = subprocess.run([sys.executable, "-B", str(RUNTIME / "verify_download.py"),
                                     "--root", str(self.package)], capture_output=True, text=True, timeout=30)
        self.assertEqual(standalone.returncode, 0, standalone.stderr)
        self.assertEqual(json.loads(standalone.stdout), report)
        self.assertEqual(self.snapshot(), before)

    def test_actual_hash_mismatch_and_missing_files_remain_failed_and_unchanged(self):
        (self.package / "readme.txt").write_bytes(b"changed released bytes")
        (self.package / "empty").unlink()
        before = self.snapshot()
        result = self.invoke()
        self.assertEqual(result["status"], "FAILED", result)
        report = json.loads(result["output"])
        self.assertEqual(report["status"], "MISMATCH")
        self.assertEqual({row["reason"] for row in report["failures"]},
                         {"SHA-256 mismatch", "missing regular file"})
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(json.loads((Path(result["receipt_directory"]) / "result.json").read_bytes()), result)

    def test_unsafe_manifest_paths_and_wrong_selected_manifest_are_refused(self):
        outside = self.root / "outside"
        outside.write_bytes(b"must not be selected by traversal")
        self.manifest.write_text(json.dumps({"../outside": hashlib.sha256(outside.read_bytes()).hexdigest()}))
        with self.assertRaisesRegex(ValueError, "Unsafe"):
            self.direct()
        alternate = self.package / "alternate.json"
        alternate.write_bytes(self.manifest.read_bytes())
        self.inputs["manifest"] = str(alternate)
        with self.assertRaisesRegex(ValueError, "exact CONTENT"):
            self.direct()
        self.assertEqual(outside.read_bytes(), b"must not be selected by traversal")

    def test_actual_file_and_aggregate_read_bounds_refuse_without_mutating_inputs(self):
        before = self.snapshot()
        for name, limit in (("MAX_RELEASE_FILES", 1), ("MAX_RELEASE_TOTAL_BYTES", 1),
                            ("MAX_RELEASE_FILE_BYTES", 1)):
            with self.subTest(bound=name), patch.object(optional, name, limit), self.assertRaises(ValueError):
                self.direct()
        self.assertEqual(self.snapshot(), before)

    def test_manifest_that_grows_after_initial_validation_still_has_two_mib_bound(self):
        original_read = optional._read
        grown = b" " * optional.MAX_INPUT_BYTES + self.manifest.read_bytes()
        self.assertIsInstance(json.loads(grown), dict)
        changed = False
        def change_after_initial_read(path, maximum=optional.MAX_INPUT_BYTES):
            nonlocal changed
            raw = original_read(path, maximum)
            if Path(path) == self.manifest and not changed:
                changed = True
                self.manifest.write_bytes(grown)
            return raw
        with patch.object(optional, "_read", side_effect=change_after_initial_read), \
                self.assertRaisesRegex(ValueError, "byte bound"):
            self.direct()
        self.assertTrue(changed)
        self.assertEqual(self.manifest.read_bytes(), grown)

    @unittest.skipUnless(os.name == "nt", "Native Windows release custody")
    def test_windows_reparse_member_refuses_even_when_hash_matches_target(self):
        outside = self.root / "other"
        outside.mkdir()
        (outside / "readme.txt").write_bytes(self.body)
        link = self.package / "linked"
        result = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(outside)],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.addCleanup(os.rmdir, link)
        self.manifest.write_text(json.dumps({"linked/readme.txt": hashlib.sha256(self.body).hexdigest()}))
        with self.assertRaises((ValueError, OSError)):
            self.direct()
        self.assertEqual((outside / "readme.txt").read_bytes(), self.body)

    @unittest.skipUnless(os.name == "nt", "Native Windows release custody")
    def test_windows_raw_member_names_and_selected_root_aliases_are_refused(self):
        for suffix in (".", " ", ":stream"):
            self.manifest.write_text(json.dumps({"readme.txt" + suffix: hashlib.sha256(self.body).hexdigest()}))
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                self.direct()
        self.manifest.write_text(json.dumps({"readme.txt": hashlib.sha256(self.body).hexdigest()}))
        for suffix in (".", " "):
            self.inputs["package"] = str(self.package) + suffix
            with self.subTest(root_suffix=suffix), self.assertRaises(ValueError):
                self.direct()
        self.assertEqual((self.package / "readme.txt").read_bytes(), self.body)


if __name__ == "__main__":
    unittest.main()

