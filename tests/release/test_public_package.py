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
    def checked(self, *args, env=None, evidence=None):
        command = [sys.executable, "-B", *map(str, args)]
        if evidence is None:
            result = subprocess.run(command, cwd=ROOT, env=env,
                                    capture_output=True, text=True, timeout=720)
        else:
            evidence.mkdir()
            stdout, stderr = evidence / "stdout.log", evidence / "stderr.log"
            # Files survive failure or timeout; never erase failed fixture evidence.
            with stdout.open("x") as out, stderr.open("x") as err:
                result = subprocess.run(command, cwd=ROOT, env=env,
                                        stdout=out, stderr=err, text=True, timeout=720)
            def tail(path):
                with path.open("rb") as stream:
                    stream.seek(max(0, path.stat().st_size - 12000))
                    return stream.read().decode("utf-8", errors="replace")
            result.stdout, result.stderr = tail(stdout), tail(stderr)
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
        # mkdtemp deliberately retains evidence after both success and failure.
        # Resolve macOS's /var alias before handing the strict fixture reader a path.
        base = Path(tempfile.mkdtemp(prefix="lodestone-runtime-ci-")).resolve()
        documents = base / "documents"
        documents.mkdir()
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1",
               "NURARIHYON_DOCUMENT_TEST_OUTPUT": str(documents)}
        print("Synthetic runtime test evidence retained at " + str(base), flush=True)
        for suite in ("nurarihyon", "pando"):
            with self.subTest(suite=suite):
                self.checked("-m", "unittest", "discover", "-s", ROOT / "tests" / suite, "-q",
                             env=env, evidence=base / suite)


if __name__ == "__main__":
    unittest.main()
