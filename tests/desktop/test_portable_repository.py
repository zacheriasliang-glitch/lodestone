"""Local Markdown navigation only; no Git, transport, models or source execution."""
from contextlib import closing
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = Path(os.environ.get("LODESTONE_DESKTOP_TEST_RUNTIME", ROOT / "desktop/runtime")).resolve()
SCRIPT = RUNTIME / "tools/machinery/check_repository.py"
spec = importlib.util.spec_from_file_location("portable_navigation", SCRIPT)
navigation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(navigation)
sys.path.insert(0, str(RUNTIME / "src"))
from lodestone_nurarihyon import portable_files


class PortableRepository(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "project λ"
        self.root.mkdir()
        (self.root / "docs").mkdir()
        (self.root / "target λ.txt").write_text("Destination contents are not read", encoding="utf-8")
        (self.root / "README.md").write_text(
            '[local](<target%20%CE%BB.txt>)\n[docs](docs/guide.md)\n'
            '[remote](https://example.invalid/no-request)\n[remote share](//example.invalid/share)\n'
            '[anchor](#unchecked)\n```\n[code](missing-example.txt)\n```\n', encoding="utf-8")
        (self.root / "docs/guide.md").write_text('[back](../README.md)\n[ref]: ../target%20%CE%BB.txt "title"\n', encoding="utf-8")
        (self.root / "node_modules").mkdir()
        (self.root / "node_modules/ignore.md").write_text("[ignored](missing.txt)", encoding="utf-8")

    def snapshot(self):
        return {str(p.relative_to(self.root)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.root.rglob("*") if p.is_file() and not p.is_symlink()}

    def invoke(self, root=None):
        return subprocess.run([sys.executable, "-I", "-B", str(SCRIPT), "--root", str(root or self.root)],
            capture_output=True, timeout=30, env={**os.environ, "PYTHONIOENCODING": "ascii:strict"})

    def test_actual_cli_counts_local_navigation_and_retains_all_bytes(self):
        # POSIX historically treats protocol-relative URLs as local paths;
        # Windows explicitly excludes them without touching an SMB share.
        if os.name != "nt":
            readme = self.root / "README.md"
            readme.write_text(readme.read_text(encoding="utf-8").replace(
                '[remote share](//example.invalid/share)\n', ''), encoding="utf-8")
        before = self.snapshot()
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr.decode("ascii", errors="replace"))
        self.assertTrue(result.stdout.isascii())
        report = json.loads(result.stdout)
        self.assertEqual((report["status"], report["files"], report["local_links"]), ("PASS", 2, 4))
        self.assertEqual(report["failures"], [])
        self.assertEqual(before, self.snapshot())

    def test_actual_cli_reports_missing_destination_without_modifying_sources(self):
        (self.root / "README.md").write_text('[missing](missing.txt)\n', encoding="utf-8")
        before = self.snapshot()
        result = self.invoke()
        self.assertEqual(result.returncode, 2)
        report = json.loads(result.stdout)
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(report["failures"], [{"source": "README.md", "target": "missing.txt"}])
        self.assertEqual(before, self.snapshot())

    def test_windows_target_grammar_refuses_unc_stream_and_device_forms_before_io(self):
        for target in ('\\\\server\\share\\file', 'target:stream', 'C:/outside', 'bad\x00name'):
            with self.subTest(target=target), self.assertRaises(ValueError):
                navigation._windows_target(self.root / "README.md", target, None)

    def test_bounded_recursive_reader_refuses_oversized_files_and_excessive_entries(self):
        with patch.object(navigation, "MAX_ENTRIES", 1), self.assertRaisesRegex(ValueError, "entry bound"):
            with closing(navigation._windows_markdown(self.root, portable_files, [0, 0])) as rows:
                list(rows)
        with patch.object(navigation, "MAX_MARKDOWN_BYTES", 8), self.assertRaises(ValueError):
            with closing(navigation._windows_markdown(self.root, portable_files, [0, 0])) as rows:
                list(rows)
        with patch.object(navigation, "MAX_TOTAL_BYTES", 1), self.assertRaisesRegex(ValueError, "total byte bound"):
            with closing(navigation._windows_markdown(self.root, portable_files, [0, 0])) as rows:
                list(rows)

    def junction(self, alias, target):
        completed = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(alias), str(target)],
                                   capture_output=True, timeout=15)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.addCleanup(alias.rmdir)

    @unittest.skipUnless(os.name == "nt", "Native Windows Markdown custody")
    def test_windows_selected_root_and_descendant_junctions_are_refused(self):
        alias = self.base / "project alias"
        self.junction(alias, self.root)
        self.assertEqual(self.invoke(alias).returncode, 2)
        inner = self.root / "child alias"
        self.junction(inner, self.root / "docs")
        result = self.invoke()
        self.assertEqual(result.returncode, 2)
        self.assertIn("reparse", json.loads(result.stdout)["error"])

    @unittest.skipUnless(os.name == "nt", "Native Windows Markdown local paths")
    def test_windows_raw_decoded_ambiguous_destination_names_are_refused(self):
        for target in ('target%20%CE%BB.txt.', 'target%20%CE%BB.txt%20', 'target.txt%3Astream',
                       'NUL.txt', '%5C%5Cserver%5Cshare%5Cfile'):
            with self.subTest(target=target):
                (self.root / "README.md").write_text('[selected](' + target + ')\n', encoding="utf-8")
                result = self.invoke()
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn("error", json.loads(result.stdout))
        self.assertEqual(self.invoke(Path(str(self.root) + ".")).returncode, 2)

    @unittest.skipUnless(os.name == "nt", "Native Windows held Markdown bytes")
    def test_windows_markdown_identity_and_root_are_held_until_report(self):
        source = self.root / "README.md"
        replacement = self.base / "replacement.md"
        replacement.write_text("Substitution", encoding="utf-8")
        original_read = portable_files.os.read
        attempts = []
        def during_read(fd, count):
            if not attempts:
                attempts.append(True)
                with self.assertRaises(OSError):
                    os.replace(replacement, source)
                with self.assertRaises(OSError):
                    self.root.rename(self.base / "moved")
            return original_read(fd, count)
        with patch.object(portable_files.os, "read", side_effect=during_read), \
                patch("subprocess.Popen", side_effect=AssertionError("Navigation must not execute source commands")), \
                patch("socket.socket", side_effect=AssertionError("No Python network client")):
            report = navigation.check(self.root)
        self.assertEqual(report["status"], "PASS")
        self.assertTrue(attempts)
        self.root.rename(self.base / "moved")


if __name__ == "__main__":
    unittest.main()
