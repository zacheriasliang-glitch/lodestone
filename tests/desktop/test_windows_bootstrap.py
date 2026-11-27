"""Windows user-entrypoint checks use corrupt local cache fixtures, never network."""
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = ROOT / "desktop/Install Lodestone.cmd"


class WindowsBootstrapSource(unittest.TestCase):
    def test_download_pin_and_visible_native_entrypoint(self):
        body = BOOTSTRAP.read_text(encoding="utf-8")
        self.assertIn("setlocal DisableDelayedExpansion", body)
        self.assertIn("cpython-3.12.12%%2B20251120-x86_64-pc-windows-msvc", body)
        self.assertIn("be95dc535d2de36ea895e6535da09bb970fdcaa82d6301a92f8d97809478f8e4", body)
        self.assertIn("Get-FileHash -LiteralPath", body)
        self.assertIn('System32\\curl.exe"', body)
        self.assertIn('System32\\tar.exe"', body)
        self.assertNotRegex(body.lower(), r"-(?:executionpolicy|encodedcommand)\b|set-executionpolicy|invoke-expression")
        verify = body.index(":verify_archive")
        self.assertLess(verify, body.index('System32\\tar.exe"'))
        self.assertLess(body.index('System32\\tar.exe"'), body.index('python\\python.exe" -I -B'))


@unittest.skipUnless(os.name == "nt", "native Windows cmd bootstrap")
class WindowsBootstrapNative(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.payload = self.base / "download space λ"
        self.payload.mkdir()
        self.script = self.payload / BOOTSTRAP.name
        shutil.copyfile(BOOTSTRAP, self.script)
        self.root = self.base / "application data λ"
        self.app = self.base / "launchers/Lodestone Preview.cmd"
        self.cache = self.base / "download cache λ"
        self.cache.mkdir()
        body = BOOTSTRAP.read_text(encoding="utf-8")
        self.pin = re.search(r'LODESTONE_SETUP_HASH=([0-9a-f]{64})', body).group(1)
        self.archive = self.cache / self.pin
        self.archive.write_bytes(b"deliberately corrupt cached Python; must never execute")

    def run_setup(self, *, root=None, app=None, extra=()):
        command = [str(self.script), "--no-open", "--root", str(root or self.root),
                   "--app", str(app or self.app), "--cache", str(self.cache), *extra]
        # Quote every argument, including a metacharacter path without spaces.
        # Pass the final native command line directly: a second list2cmdline
        # pass would add C-runtime escapes that cmd does not interpret as quotes.
        executable = os.environ.get("COMSPEC", "cmd.exe")
        inner = " ".join('"' + value + '"' for value in command)
        line = '"' + executable + '" /d /v:off /s /c "' + inner + '"'
        return subprocess.run(line,
            capture_output=True, encoding="utf-8", errors="replace", timeout=30)

    def test_corrupt_cache_preserves_bytes_and_never_extracts(self):
        before = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        result = self.run_setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("checksum mismatch", result.stdout + result.stderr)
        self.assertEqual(hashlib.sha256(self.archive.read_bytes()).hexdigest(), before)
        self.assertFalse((self.root / "ACTIVE.json").exists())
        self.assertFalse(self.app.exists())
        self.assertEqual(list((self.root / "deps/bootstrap").glob("python-*")), [])

    def test_shell_metacharacters_refuse_before_creating_root(self):
        for character in ("&", "!", "%", "(", ")", "^"):
            with self.subTest(character=character):
                selected = self.base / ("unsafe" + character + "directory")
                result = self.run_setup(root=selected)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("metacharacters", result.stdout + result.stderr)
                self.assertFalse(selected.exists())
                self.assertTrue(self.archive.is_file())

    def test_unrelated_existing_root_is_untouched(self):
        self.root.mkdir()
        note = self.root / "personal.txt"
        note.write_bytes(b"retain")
        result = self.run_setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unrelated existing data", result.stdout + result.stderr)
        self.assertEqual(list(self.root.iterdir()), [note])
        self.assertEqual(note.read_bytes(), b"retain")

    def test_reparse_root_is_refused_without_touching_target(self):
        outside = self.base / "outside"
        outside.mkdir()
        result = subprocess.run([os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", "mklink", "/J",
                                 str(self.root), str(outside)], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        try:
            result = self.run_setup()
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("junction or symbolic link", result.stdout + result.stderr)
            self.assertEqual(list(outside.iterdir()), [])
        finally:
            self.root.rmdir()


if __name__ == "__main__":
    unittest.main()
