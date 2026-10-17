"""Behavior checks against isolated, explicit source fixtures."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[2] / "tools/continuity/checkpoint.py"
SPEC = importlib.util.spec_from_file_location("checkpoint", SCRIPT)
checkpoint = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checkpoint)


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "project"
        self.root.mkdir()
        (self.root / "source.txt").write_bytes(b"exact source\n")
        self.output = self.base / "checkpoint.json"

    def create(self, **kwargs):
        args = {"root": self.root, "output": self.output, "task": "Fixture task",
                "status": "IN_PROGRESS", "next_action": "Inspect fixture",
                "sources": ["source.txt"], "notes": ["Fixture only"],
                "unresolved": ["No scientific validation"]}
        args.update(kwargs)
        return checkpoint.create(**args)

    def test_roundtrip_and_relocated_root(self):
        result = self.create()
        raw = self.output.read_bytes()
        self.assertEqual(result["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertNotIn(str(self.root), raw.decode())
        other = self.base / "moved"
        shutil.copytree(self.root, other)
        report = checkpoint.verify(self.output, other)
        self.assertEqual(report["status"], "VERIFIED", report)
        self.assertEqual(report["recorded_status"], "IN_PROGRESS")
        self.assertEqual(self.output.read_bytes(), raw)

    def test_same_size_change_detected(self):
        self.create()
        (self.root / "source.txt").write_bytes(b"other source\n")
        report = checkpoint.verify(self.output, self.root)
        self.assertEqual(report["status"], "DRIFT")
        self.assertEqual(report["sources"][0]["status"], "CHANGED", report)
        self.assertEqual(report["failures"], 1)

    def test_missing_source_reported(self):
        self.create()
        (self.root / "source.txt").unlink()
        self.assertEqual(checkpoint.verify(self.output, self.root)["sources"][0]["status"], "MISSING")

    def test_only_explicit_files_read(self):
        (self.root / "private.txt").write_text("never selected", encoding="utf-8")
        self.create()
        (self.root / "private.txt").unlink()
        report = checkpoint.verify(self.output, self.root)
        self.assertEqual(report["status"], "VERIFIED")
        self.assertEqual(len(report["sources"]), 1)

    def test_existing_output_and_source_refused_without_changes(self):
        self.create()
        before = self.output.read_bytes()
        with self.assertRaises(FileExistsError):
            self.create()
        self.assertEqual(self.output.read_bytes(), before)
        with self.assertRaises(FileExistsError):
            self.create(output=self.root / "source.txt")
        self.assertEqual((self.root / "source.txt").read_bytes(), b"exact source\n")

    def test_uncreated_output_parent_refused(self):
        with self.assertRaisesRegex(ValueError, "parent"):
            self.create(output=self.base / "missing" / "checkpoint.json")
        self.assertFalse((self.base / "missing").exists())

    def test_unsafe_and_duplicate_sources_refused(self):
        for name in ("../secret", "/etc/passwd", "a/../source.txt", "a//b", "./source.txt", "C:/secret", "a\\secret"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.create(sources=[name])
        with self.assertRaises(ValueError):
            self.create(sources=["source.txt", "source.txt"])
        with self.assertRaises(ValueError):
            self.create(sources=[])
        self.assertFalse(self.output.exists())

    def test_directory_and_special_file_refused(self):
        (self.root / "folder").mkdir()
        with self.assertRaisesRegex(ValueError, "regular file"):
            self.create(sources=["folder"])
        if hasattr(os, "mkfifo"):
            os.mkfifo(self.root / "pipe")
            with self.assertRaisesRegex(ValueError, "regular file"):
                self.create(sources=["pipe"])

    def symlink(self, target, link, is_dir=False):
        try:
            link.symlink_to(target, target_is_directory=is_dir)
        except (OSError, NotImplementedError):
            self.skipTest("Symlinks are unavailable on this host")

    def test_source_symlink_and_parent_refused(self):
        self.symlink(self.root / "source.txt", self.root / "link.txt")
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.create(sources=["link.txt"])
        self.symlink(self.root, self.root / "nested-link", True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.create(sources=["nested-link/source.txt"])

    def test_output_symlink_refused_without_touching_target(self):
        target = self.base / "missing-target"
        self.symlink(target, self.output)
        with self.assertRaisesRegex(ValueError, "Symlink"):
            self.create()
        self.assertFalse(target.exists())

    def test_output_symlink_parent_refused(self):
        self.symlink(self.root, self.base / "linked-root", True)
        with self.assertRaisesRegex(ValueError, "Symlink"):
            self.create(output=self.base / "linked-root" / "new.json")
        self.assertFalse((self.root / "new.json").exists())

    def test_checkpoint_tampering_is_not_source_verification(self):
        self.create()
        value = json.loads(self.output.read_bytes())
        value["next_action"] = "Different action"
        self.output.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            checkpoint.verify(self.output, self.root)

    def test_duplicate_json_keys_refused(self):
        self.output.write_text('{"schema": "x", "schema": "y"}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Duplicate JSON"):
            checkpoint.verify(self.output, self.root)

    def test_source_limit(self):
        original = checkpoint.MAX_SOURCE_BYTES
        self.addCleanup(setattr, checkpoint, "MAX_SOURCE_BYTES", original)
        checkpoint.MAX_SOURCE_BYTES = 4
        with self.assertRaisesRegex(ValueError, "read bound"):
            self.create()
        self.assertFalse(self.output.exists())

    def test_stable_handle_and_path_timestamps_may_differ(self):
        real_fstat = os.fstat

        def shifted_fstat(fd):
            value = real_fstat(fd)
            return SimpleNamespace(st_mode=value.st_mode, st_dev=value.st_dev,
                                   st_ino=value.st_ino, st_size=value.st_size,
                                   st_mtime_ns=value.st_mtime_ns + 100,
                                   st_ctime_ns=value.st_ctime_ns + 200)

        with patch.object(checkpoint.os, "fstat", side_effect=shifted_fstat):
            self.create()
            report = checkpoint.verify(self.output, self.root)
        self.assertEqual(report["status"], "VERIFIED", report)

    def test_mutating_handle_during_hash_is_still_rejected(self):
        real_fstat = os.fstat
        calls = 0

        def changed_fstat(fd):
            nonlocal calls
            calls += 1
            value = real_fstat(fd)
            return SimpleNamespace(st_mode=value.st_mode, st_dev=value.st_dev,
                                   st_ino=value.st_ino, st_size=value.st_size,
                                   st_mtime_ns=value.st_mtime_ns + calls,
                                   st_ctime_ns=value.st_ctime_ns)

        with patch.object(checkpoint.os, "fstat", side_effect=changed_fstat):
            with self.assertRaisesRegex(ValueError, "handle_changed=True"):
                self.create()
        self.assertFalse(self.output.exists())

    def test_cli_exit_codes_and_preserved_record(self):
        prefix = [sys.executable, "-B", str(SCRIPT)]
        result = subprocess.run(prefix + ["create", "--root", str(self.root), "--output", str(self.output),
                                          "--task", "Fixture", "--status", "OPEN", "--next-action", "Read",
                                          "--source", "source.txt"], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        raw = self.output.read_bytes()
        args = prefix + ["verify", str(self.output), "--root", str(self.root)]
        self.assertEqual(subprocess.run(args, capture_output=True).returncode, 0)
        (self.root / "source.txt").unlink()
        self.assertEqual(subprocess.run(args, capture_output=True).returncode, 1)
        self.assertEqual(self.output.read_bytes(), raw)
        self.output.write_text("not json", encoding="utf-8")
        self.assertEqual(subprocess.run(args, capture_output=True).returncode, 2)


if __name__ == "__main__":
    unittest.main()
