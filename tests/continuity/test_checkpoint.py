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

    def transition(self, candidate, before_pin=None, after_pin=None):
        return checkpoint.preserve(
            self.output, candidate, self.root,
            predecessor_sha256=before_pin or hashlib.sha256(self.output.read_bytes()).hexdigest(),
            candidate_sha256=after_pin or hashlib.sha256(candidate.read_bytes()).hexdigest())

    def test_external_raw_pin_rejects_self_consistent_replacement_before_sources(self):
        original = self.create()
        value = checkpoint.read_checkpoint(self.output, expected_sha256=original["sha256"])
        value["next_action"] = "A different recorded action"
        del value["checkpoint_sha256"]
        value["checkpoint_sha256"] = hashlib.sha256(checkpoint.canonical(value)).hexdigest()
        self.output.write_text(json.dumps(value), encoding="utf-8")
        self.assertEqual(checkpoint.verify(self.output, self.root)["status"], "VERIFIED")
        with patch.object(checkpoint, "_verify_sources") as sources:
            with self.assertRaisesRegex(ValueError, "expected raw-file SHA-256"):
                checkpoint.verify(self.output, self.root, expected_sha256=original["sha256"])
            sources.assert_not_called()
        with self.assertRaisesRegex(ValueError, "expected raw-file SHA-256"):
            checkpoint.read_checkpoint(self.output, expected_sha256=original["sha256"])

    def test_external_pin_binds_wire_bytes_not_only_canonical_payload(self):
        result = self.create()
        value = checkpoint.read_checkpoint(self.output)
        self.output.write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")
        self.assertEqual(checkpoint.read_checkpoint(self.output), value)
        with self.assertRaisesRegex(ValueError, "expected raw-file SHA-256"):
            checkpoint.read_checkpoint(self.output, expected_sha256=result["sha256"])

    def test_ordinary_verify_reports_whether_an_external_pin_was_checked(self):
        result = self.create()
        for pin, checked in ((None, False), (result["sha256"], True)):
            report = checkpoint.verify(self.output, self.root, expected_sha256=pin)
            self.assertEqual(report["status"], "VERIFIED")
            self.assertEqual(report["external_pin_checked"], checked)
            self.assertEqual(report["file_sha256"], result["sha256"])

    def test_preserve_additions_and_relocated_root_without_writing(self):
        original = self.create(notes=["First observation", "Second observation"])
        (self.root / "addition.txt").write_bytes(b"New evidence\n")
        candidate = self.base / "candidate.json"
        addition = self.create(output=candidate, sources=["source.txt", "addition.txt"],
                               notes=["Second observation", "New observation", "First observation"],
                               unresolved=["No scientific validation", "Further review"],
                               status="RECORDED_UPDATE", next_action="Read addition")
        before_bytes, after_bytes = self.output.read_bytes(), candidate.read_bytes()
        moved = self.base / "moved"
        shutil.copytree(self.root, moved)
        report = checkpoint.preserve(self.output, candidate, moved,
                                     predecessor_sha256=original["sha256"],
                                     candidate_sha256=addition["sha256"])
        self.assertEqual(report["status"], "PRESERVED", report)
        self.assertEqual(report["failures"], 0)
        self.assertEqual({row["path"] for row in report["sources"]}, {"source.txt", "addition.txt"})
        self.assertEqual(self.output.read_bytes(), before_bytes)
        self.assertEqual(candidate.read_bytes(), after_bytes)

    def test_standalone_valid_same_count_source_substitution_is_loss(self):
        self.create()
        (self.root / "other.txt").write_bytes(b"different source\n")
        candidate = self.base / "substitution.json"
        self.create(output=candidate, sources=["other.txt"])
        self.assertEqual(checkpoint.verify(candidate, self.root)["status"], "VERIFIED")
        report = self.transition(candidate)
        self.assertEqual(report["status"], "LOSS", report)
        self.assertEqual(report["source_status"], "VERIFIED")
        self.assertEqual(report["losses"], [{"kind": "SOURCE_IDENTITY_NOT_RETAINED",
                         "expected": checkpoint.read_checkpoint(self.output)["sources"][0], "candidate": None}])

    def test_rebound_source_epoch_requires_a_separate_reviewed_baseline(self):
        self.create()
        (self.root / "source.txt").write_bytes(b"other source\n")
        candidate = self.base / "rebound.json"
        self.create(output=candidate)
        # A newly recorded epoch can verify independently, but cannot silently
        # replace the earlier source identity in a preservation transition.
        self.assertEqual(checkpoint.verify(candidate, self.root)["status"], "VERIFIED")
        report = self.transition(candidate)
        self.assertEqual(report["status"], "LOSS", report)
        self.assertEqual(report["losses"][0]["expected"]["bytes"], report["losses"][0]["candidate"]["bytes"])
        self.assertNotEqual(report["losses"][0]["expected"]["sha256"], report["losses"][0]["candidate"]["sha256"])

    def test_notes_and_unresolved_items_cannot_be_dropped_or_reworded(self):
        self.create()
        for field in ("notes", "unresolved"):
            for mode, replacement in (("drop", []), ("reword", ["Apparently resolved now"])):
                with self.subTest(field=field, mode=mode):
                    candidate = self.base / f"{field}-{mode}.json"
                    self.create(output=candidate, **{field: replacement})
                    self.assertEqual(checkpoint.verify(candidate, self.root)["status"], "VERIFIED")
                    report = self.transition(candidate)
                    self.assertEqual(report["status"], "LOSS", report)
                    self.assertTrue(any(row["kind"] == "TEXT_NOT_RETAINED" and row["field"] == field
                                        for row in report["losses"]))

    def test_repeated_recorded_text_is_preserved_with_its_multiplicity(self):
        self.create(notes=["Same text", "Same text"])
        candidate = self.base / "one-copy.json"
        self.create(output=candidate, notes=["Same text"])
        report = self.transition(candidate)
        self.assertEqual(report["status"], "LOSS")
        self.assertEqual(report["losses"], [{"kind": "TEXT_NOT_RETAINED", "field": "notes", "text": "Same text"}])

    def test_task_change_is_loss_even_when_sources_and_text_match(self):
        self.create()
        candidate = self.base / "other-task.json"
        self.create(output=candidate, task="Another task")
        report = self.transition(candidate)
        self.assertEqual(report["status"], "LOSS")
        self.assertEqual(report["losses"][0]["kind"], "TASK_CHANGED")

    def test_preserve_reopens_sources_and_reports_missing_changed_and_unreadable(self):
        self.create()
        candidate = self.base / "candidate.json"
        self.create(output=candidate)
        source = self.root / "source.txt"
        source.unlink()
        report = self.transition(candidate)
        self.assertEqual(report["status"], "DRIFT", report)
        self.assertEqual(report["sources"][0]["status"], "MISSING")
        source.write_bytes(b"other source\n")
        report = self.transition(candidate)
        self.assertEqual(report["status"], "DRIFT", report)
        self.assertEqual(report["sources"][0]["status"], "CHANGED")
        with patch.object(checkpoint, "fingerprint", side_effect=PermissionError("Synthetic refusal")):
            report = self.transition(candidate)
        self.assertEqual(report["status"], "DRIFT", report)
        self.assertEqual(report["sources"][0]["status"], "UNREADABLE")

    def test_added_source_is_checked_and_loss_does_not_hide_drift(self):
        self.create()
        extra = self.root / "extra.txt"
        extra.write_bytes(b"additional evidence")
        candidate = self.base / "candidate.json"
        self.create(output=candidate, sources=["source.txt", "extra.txt"], unresolved=[])
        extra.unlink()
        report = self.transition(candidate)
        self.assertEqual(report["status"], "LOSS", report)
        self.assertEqual(report["source_status"], "DRIFT")
        self.assertEqual(report["source_failures"], 1)
        self.assertEqual(report["failures"], 2)

    def test_both_raw_pins_checked_before_any_record_is_parsed(self):
        original = self.create()
        candidate = self.base / "candidate.json"
        result = self.create(output=candidate)
        for before_pin, after_pin in (("0" * 64, result["sha256"]), (original["sha256"], "0" * 64)):
            with self.subTest(before=before_pin), patch.object(checkpoint, "_parse_checkpoint") as parse:
                with self.assertRaisesRegex(ValueError, "expected raw-file SHA-256"):
                    self.transition(candidate, before_pin, after_pin)
                parse.assert_not_called()

    def test_preserve_requires_valid_explicit_pins_and_valid_schema(self):
        original = self.create()
        candidate = self.base / "candidate.json"
        result = self.create(output=candidate)
        for bad in (None, "", "A" * 64, "x" * 64, 12, [], "0" * 63):
            with self.subTest(pin=bad), patch.object(checkpoint, "_checkpoint_bytes") as read:
                with self.assertRaisesRegex(ValueError, "lowercase hexadecimal"):
                    checkpoint.preserve(self.output, candidate, self.root,
                                        predecessor_sha256=bad, candidate_sha256=result["sha256"])
                read.assert_not_called()
        with self.assertRaises(TypeError):
            checkpoint.preserve(self.output, candidate, self.root)
        candidate.write_text('{"schema":"wrong"}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Unsupported checkpoint schema"):
            self.transition(candidate, original["sha256"])

    def test_preserve_cli_status_and_error_exit_codes(self):
        original = self.create()
        candidate = self.base / "candidate.json"
        result = self.create(output=candidate)
        prefix = [sys.executable, "-B", str(SCRIPT)]
        args = prefix + ["preserve", str(self.output), str(candidate), "--root", str(self.root),
                         "--predecessor-sha256", original["sha256"], "--candidate-sha256", result["sha256"]]
        passed = subprocess.run(args, text=True, capture_output=True)
        self.assertEqual(passed.returncode, 0, passed.stderr)
        self.assertEqual(json.loads(passed.stdout)["status"], "PRESERVED")
        missing_pin = subprocess.run(args[:-2], text=True, capture_output=True)
        self.assertEqual(missing_pin.returncode, 2)
        bad_pin = subprocess.run(args[:-1] + ["invalid"], text=True, capture_output=True)
        self.assertEqual(bad_pin.returncode, 2)
        self.assertEqual(json.loads(bad_pin.stderr)["status"], "ERROR")
        (self.root / "source.txt").unlink()
        drift = subprocess.run(args, text=True, capture_output=True)
        self.assertEqual(drift.returncode, 1)
        self.assertEqual(json.loads(drift.stdout)["status"], "DRIFT")
        (self.root / "source.txt").write_bytes(b"exact source\n")
        loss = self.base / "loss.json"
        lost = self.create(output=loss, unresolved=[])
        loss_args = prefix + ["preserve", str(self.output), str(loss), "--root", str(self.root),
                              "--predecessor-sha256", original["sha256"], "--candidate-sha256", lost["sha256"]]
        failed = subprocess.run(loss_args, text=True, capture_output=True)
        self.assertEqual(failed.returncode, 1)
        self.assertEqual(json.loads(failed.stdout)["status"], "LOSS")


if __name__ == "__main__":
    unittest.main()
