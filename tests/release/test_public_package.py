"""Extend the existing CI release-test entry point without changing workflow access.

Developer tests require a clean committed Git checkout and use only synthetic
state in temporary directories. They never contact models or install services.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class PublicPackageTests(unittest.TestCase):
    def checked(self, *args, env=None):
        result = subprocess.run([sys.executable, "-B", *map(str, args)], cwd=ROOT,
                                env=env, capture_output=True, text=True, timeout=720)
        self.assertEqual(result.returncode, 0, result.stdout[-12000:] + result.stderr[-12000:])
        return result

    def test_committed_release_archive_with_fresh_user_state(self):
        with tempfile.TemporaryDirectory(prefix="lodestone-release-ci-") as temporary:
            base = Path(temporary).resolve()
            self.checked(ROOT / "tools/release/package.py", "--output", base / "artifacts")
            version = (ROOT / "VERSION").read_text().strip()
            result = self.checked(ROOT / "tools/release/verify_archive.py",
                                  "--archive", base / "artifacts" / ("lodestone-" + version + ".zip"),
                                  "--output", base / "fresh")
            report = json.loads(result.stdout)
            self.assertEqual((report["status"], report["version"]), ("PASS", version))
            self.assertFalse(report["service_installed"])

    @unittest.skipUnless(sys.platform in ("darwin", "linux"), "New runtimes are macOS/Linux only")
    def test_new_runtime_contracts_in_isolation(self):
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
        for suite in ("nurarihyon", "pando"):
            with self.subTest(suite=suite):
                self.checked("-m", "unittest", "discover", "-s", ROOT / "tests" / suite, "-q", env=env)


if __name__ == "__main__":
    unittest.main()
