import importlib.util
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("verify_release", ROOT / "verify_download.py")
verify_release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify_release)


class DownloadChecks(unittest.TestCase):
    def test_changed_byte_and_missing_file_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "payload.txt"
            path.write_bytes(b"original\n")
            (root / "CONTENT_SHA256.json").write_text(json.dumps({"payload.txt": hashlib.sha256(path.read_bytes()).hexdigest()}))
            self.assertEqual(verify_release.verify(root)["status"], "VERIFIED")
            path.write_bytes(b"Original\n")
            self.assertEqual(verify_release.verify(root)["status"], "MISMATCH")
            path.unlink()
            self.assertEqual(verify_release.verify(root)["status"], "MISMATCH")

    def test_manifest_cannot_select_external_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ("../outside", "/outside", "C:/outside", "a\\outside", ".git/config"):
                with self.subTest(name=name):
                    (root / "CONTENT_SHA256.json").write_text(json.dumps({name: "0" * 64}))
                    with self.assertRaises(ValueError):
                        verify_release.verify(root)


if __name__ == "__main__":
    unittest.main()
