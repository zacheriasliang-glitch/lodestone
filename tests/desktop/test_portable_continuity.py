"""Bundled continuity CLI and native file custody; authored local fixtures only."""
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
SCRIPT = RUNTIME / "tools/continuity/checkpoint.py"
spec = importlib.util.spec_from_file_location("portable_checkpoint", SCRIPT)
checkpoint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checkpoint)


class PortableContinuity(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.sources = self.root / "sources λ"
        self.sources.mkdir()
        self.source = self.sources / "note.txt"
        self.source.write_text("An authored observation — 窯.\n", encoding="utf-8")
        self.output = self.root / "checkpoint λ.json"

    def invoke(self, *args):
        # Exercise the real packaged entry point/child import, with an ASCII
        # stdout stream even when selected paths and source contents are Unicode.
        return subprocess.run([sys.executable, "-I", "-B", str(SCRIPT), *map(str, args)],
            capture_output=True, timeout=30, env={**os.environ, "PYTHONIOENCODING": "ascii:strict"})

    def create_args(self, output=None):
        return ["create", "--root", self.sources, "--output", output or self.output,
                "--task", "Authored fixture", "--status", "Unresolved observation",
                "--next-action", "Inspect the actual result", "--source", "note.txt",
                "--note", "Retained exact note", "--unresolved", "Still not independently checked"]

    def create(self, output=None, notes=None, unresolved=None):
        return checkpoint.create(self.sources, output or self.output, "Authored fixture",
            "Unresolved observation", "Inspect the actual result", ["note.txt"],
            notes=notes or ["Retained exact note"], unresolved=unresolved or ["Still not independently checked"])

    def success(self, result, status):
        self.assertEqual(result.returncode, 0, result.stderr.decode("ascii", errors="replace"))
        self.assertTrue(result.stdout.isascii())
        row = json.loads(result.stdout)
        self.assertEqual(row["status"], status)
        return row

    def test_actual_cli_create_verify_and_pinned_preserve_keep_exact_history(self):
        created = self.success(self.invoke(*self.create_args()), "CREATED")
        first = self.output.read_bytes()
        self.assertEqual(created["sha256"], hashlib.sha256(first).hexdigest())
        verified = self.success(self.invoke("verify", self.output, "--root", self.sources,
            "--expected-sha256", created["sha256"]), "VERIFIED")
        self.assertTrue(verified["external_pin_checked"])
        self.assertEqual(verified["notes"], ["Retained exact note"])
        later = self.root / "later.json"
        candidate = self.create(later, notes=["Retained exact note", "A later declared note"])
        preserved = self.success(self.invoke("preserve", self.output, later, "--root", self.sources,
            "--predecessor-sha256", created["sha256"], "--candidate-sha256", candidate["sha256"]), "PRESERVED")
        self.assertEqual(preserved["losses"], [])
        self.assertEqual(self.output.read_bytes(), first)
        self.source.write_text("A later source revision", encoding="utf-8")
        drift = self.invoke("verify", self.output, "--root", self.sources, "--expected-sha256", created["sha256"])
        self.assertEqual(drift.returncode, 1)
        self.assertEqual(json.loads(drift.stdout)["status"], "DRIFT")
        self.assertEqual(self.output.read_bytes(), first)

    def test_existing_output_and_raw_pin_mismatch_never_rewrite_evidence(self):
        self.create()
        original = self.output.read_bytes()
        refused = self.invoke(*self.create_args())
        self.assertEqual(refused.returncode, 2)
        self.assertEqual(self.output.read_bytes(), original)
        with patch.object(checkpoint, "fingerprint", side_effect=AssertionError("Pin must precede source reads")), \
                self.assertRaisesRegex(ValueError, "expected raw-file"):
            checkpoint.verify(self.output, self.sources, expected_sha256="0" * 64)
        self.assertEqual(self.output.read_bytes(), original)

    def test_schema_duplicate_key_and_source_bounds_preserve_no_partial_output(self):
        raw = b'{"schema":"lodestone-checkpoint/v1","schema":"repeated"}'
        self.output.write_bytes(raw)
        with self.assertRaisesRegex(ValueError, "Duplicate JSON"):
            checkpoint.read_checkpoint(self.output)
        self.assertEqual(self.output.read_bytes(), raw)
        too_large = self.root / "large.json"
        too_large.write_bytes(b" " * (checkpoint.MAX_CHECKPOINT_BYTES + 1))
        with self.assertRaises(ValueError):
            checkpoint.read_checkpoint(too_large)
        candidate = self.root / "candidate.json"
        with patch.object(checkpoint, "MAX_SOURCE_BYTES", 4), self.assertRaises(ValueError):
            self.create(candidate)
        self.assertFalse(candidate.exists())

    def test_two_native_creators_publish_exactly_one_complete_record(self):
        command = [sys.executable, "-I", "-B", str(SCRIPT), *map(str, self.create_args())]
        children = []
        try:
            children = [subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
            observed = [(child.communicate(timeout=30), child.returncode) for child in children]
            self.assertEqual(sorted(code for _, code in observed), [0, 2], observed)
            result = checkpoint.verify(self.output, self.sources)
            self.assertEqual(result["status"], "VERIFIED")
            winner = next(json.loads(io[0]) for io, code in observed if code == 0)
            self.assertEqual(hashlib.sha256(self.output.read_bytes()).hexdigest(), winner["sha256"])
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=15)

    def junction(self, alias, target):
        result = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(alias), str(target)],
                                capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.addCleanup(alias.rmdir)

    @unittest.skipUnless(os.name == "nt", "Native Windows alias/stream custody")
    def test_windows_junction_source_checkpoint_and_output_refuse_without_mutation(self):
        self.create()
        original = self.output.read_bytes()
        source_alias, output_alias = self.root / "source alias", self.root / "output alias"
        self.junction(source_alias, self.sources)
        destination = self.root / "destination"
        destination.mkdir()
        selected = destination / "existing.json"
        selected.write_bytes(original)
        self.junction(output_alias, destination)
        with self.assertRaises((OSError, ValueError)):
            checkpoint.create(source_alias, self.root / "new.json", "task", "status", "next", ["note.txt"])
        for name in ("note.txt:stream", "C:/outside.txt", "NUL.txt", "note.txt.", "note.txt "):
            with self.subTest(name=name), self.assertRaises((OSError, ValueError)):
                checkpoint.create(self.sources, self.root / "new.json", "task", "status", "next", [name])
        with self.assertRaises((OSError, ValueError)):
            self.create(output_alias / "new.json")
        with self.assertRaises((OSError, ValueError)):
            checkpoint.read_checkpoint(output_alias / "existing.json")
        self.assertFalse((destination / "new.json").exists())
        self.assertFalse((self.root / "new.json").exists())
        self.assertEqual(selected.read_bytes(), original)
        self.assertEqual(self.output.read_bytes(), original)

    @unittest.skipUnless(os.name == "nt", "Native Windows held read/root and durable write")
    def test_windows_streaming_identity_root_guard_and_flush_failure(self):
        original_read = checkpoint.os.read
        replacement = self.sources / "replacement.txt"
        replacement.write_bytes(b"substitution")
        attempted = []
        def read(fd, count):
            if not attempted:
                attempted.append(True)
                with self.assertRaises(OSError):
                    os.replace(replacement, self.source)
                with self.assertRaises(OSError):
                    self.sources.rename(self.root / "moved")
            return original_read(fd, count)
        original_write = checkpoint._files.durable_write
        def publish(*args, **kwargs):
            with self.assertRaises(OSError):
                self.sources.rename(self.root / "moved")
            return original_write(*args, **kwargs)
        with patch.object(checkpoint.os, "read", side_effect=read), \
                patch.object(checkpoint._files, "durable_write", side_effect=publish):
            self.create()
        self.assertTrue(attempted)
        first = self.output.read_bytes()
        candidate = self.root / "candidate.json"
        with patch.object(checkpoint._files.os, "fsync", side_effect=OSError("authored flush failure")), \
                self.assertRaisesRegex(OSError, "flush failure"):
            self.create(candidate)
        self.assertFalse(candidate.exists())
        self.assertEqual(self.output.read_bytes(), first)
        self.sources.rename(self.root / "moved")  # Custody is released on return.


if __name__ == "__main__":
    unittest.main()
