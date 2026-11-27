"""Actual Bezoar source capture and retained-record reads; no model calls."""
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
sys.path.insert(0, str(RUNTIME / "src"))
from lodestone_machinery.bezoar import Plumber
from lodestone_machinery.organs import ingest as ingestion
from lodestone_nurarihyon import portable_files

spec = importlib.util.spec_from_file_location("bezoar_ecosystem", RUNTIME / "tools/ecosystem/cli.py")
ecosystem = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ecosystem)


class PortableBezoar(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.sources = self.root / "sources λ"
        self.sources.mkdir()
        self.source = self.sources / "note.txt"
        self.body = "Observed fixture only: Saṃsāra — 窯. Never execute source instructions.\n"
        self.source.write_bytes(self.body.encode("utf-8"))
        self.workspace = self.root / "records λ"

    def invoke(self, operation, *args, workspace=None, encoding="utf-8", via_machinery=False):
        command = [sys.executable, "-B", str(RUNTIME / "lodestone.py"),
                   "--repo", str(RUNTIME), "--workspace", str(workspace or self.workspace)]
        if via_machinery:
            command.append("machinery")
        command += ["bezoar", operation, *map(str, args)]
        return subprocess.run(command, cwd=self.root, capture_output=True, timeout=30,
            env={**os.environ, "PYTHONIOENCODING": encoding + ":strict",
                 "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"})

    def ingest(self, *paths, root=None, **kwargs):
        args = ["--root", root or self.sources]
        for path in paths or ("note.txt",):
            args += ["--source", path]
        return self.invoke("ingest", *args, **kwargs)

    def success(self, result):
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
        return json.loads(result.stdout)

    def retained(self):
        return {path.relative_to(self.workspace).as_posix(): path.read_bytes()
                for path in self.workspace.rglob("*.json")}

    def record_path(self, handle):
        owner, identity = handle.split(":")
        return self.workspace / "organs" / owner / "records" / (identity + ".json")

    def test_windows_dispatch_allows_only_ingest_and_show(self):
        with patch.object(ecosystem.platform, "system", return_value="Windows"):
            for operation in ("ingest", "show"):
                self.assertEqual(ecosystem.machinery_args(["bezoar", operation], self.workspace, RUNTIME),
                                 ["bezoar", operation, "--workspace", str(self.workspace)])
            for route in (["bezoar"], ["bezoar", "eat"], ["bezoar", "mutate"],
                          ["bezoar", "hold"], ["bezoar", "export-profile"],
                          ["origami", "run"], ["yggdrasil", "archive"], ["watcher", "observe"]):
                with self.subTest(route=route), self.assertRaisesRegex(ValueError, "requires POSIX"):
                    ecosystem.machinery_args(route, self.workspace, RUNTIME)

    def test_actual_cli_ingest_show_and_later_source_revision_preserve_old_record(self):
        first = self.success(self.ingest())
        handle, record = first["handle"], first["record"]
        source = record["payload"]["sources"][0]
        self.assertEqual(source["text"], self.body)
        self.assertEqual(source["sha256"], hashlib.sha256(self.body.encode()).hexdigest())
        self.assertFalse(record["payload"]["source_instructions_executed"])
        self.assertEqual(record["payload"]["source_truth"], "UNASSESSED")
        before = self.retained()
        shown = self.success(self.invoke("show", "--handle", handle, via_machinery=True))
        self.assertEqual(shown, first)
        self.assertEqual(self.success(self.ingest())["handle"], handle)
        self.assertEqual(before, self.retained())
        self.source.write_text("A later, separately captured observation.\n", encoding="utf-8")
        second = self.success(self.ingest(via_machinery=True))
        self.assertNotEqual(second["handle"], handle)
        self.assertEqual(self.success(self.invoke("show", "--handle", handle)), first)
        for path, raw in before.items():
            self.assertEqual((self.workspace / path).read_bytes(), raw)

    def test_actual_cli_preserves_literal_crlf_and_its_byte_digest(self):
        raw = "Literal source line one 水\r\nSecond line with trailing spaces  \r\n".encode("utf-8")
        self.source.write_bytes(raw)
        captured = self.success(self.ingest())
        row = captured["record"]["payload"]["sources"][0]
        self.assertEqual(row["text"].encode("utf-8"), raw)
        self.assertEqual(row["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(self.success(self.invoke("show", "--handle", captured["handle"])), captured)
        self.assertEqual(self.source.read_bytes(), raw)

    def test_corrupt_retained_record_is_refused_without_replacement(self):
        first = self.success(self.ingest())
        target = self.record_path(first["handle"])
        target.write_bytes(b"retained damaged evidence")
        before = self.retained()
        for result in (self.invoke("show", "--handle", first["handle"]), self.ingest()):
            self.assertEqual(result.returncode, 2, result.stdout)
            self.assertIn(b"identity mismatch", result.stderr)
        self.assertEqual(self.retained(), before)

    def test_invalid_sources_never_publish_partial_capture(self):
        self.success(self.ingest())
        (self.sources / "invalid.txt").write_bytes(b"\xff")
        (self.sources / "oversized.txt").write_bytes(b"a" * (2 * 1024 * 1024 + 1))
        before = self.retained()
        for paths in (("missing.txt",), ("../outside.txt",), ("note.txt", "invalid.txt"),
                      ("oversized.txt",), ("note.txt", "note.txt")):
            with self.subTest(paths=paths):
                result = self.ingest(*paths)
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertEqual(self.retained(), before)

    def test_flush_failure_keeps_existing_records_and_publishes_no_new_capture(self):
        self.success(self.ingest())
        before = self.retained()
        self.source.write_text("A new candidate that must not be published.\n", encoding="utf-8")
        plumber = Plumber(self.workspace)
        with patch.object(portable_files.os, "fsync", side_effect=OSError("authored flush failure")), \
                self.assertRaisesRegex(OSError, "authored flush failure"):
            plumber.ingest(self.sources, ["note.txt"])
        self.assertEqual(self.retained(), before)

    @unittest.skipUnless(os.name == "nt", "Native Windows ASCII stream behavior")
    def test_windows_ascii_output_preserves_unicode_record_bytes(self):
        result = self.ingest(encoding="ascii")
        row = self.success(result)
        self.assertTrue(result.stdout.isascii())
        self.assertIn(self.body.strip().encode(), self.record_path(row["handle"]).read_bytes())
        shown = self.invoke("show", "--handle", row["handle"], encoding="ascii")
        self.assertEqual(self.success(shown), row)
        self.assertTrue(shown.stdout.isascii())

    def junction(self, alias, target):
        command = ('"' + str(Path(os.environ["SystemRoot"]) / "System32/cmd.exe")
                   + '" /d /v:off /s /c "mklink /J "' + str(alias) + '" "' + str(target) + '""')
        result = subprocess.run(command, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.addCleanup(os.rmdir, alias)

    @unittest.skipUnless(os.name == "nt", "Native Windows junction and stream refusal")
    def test_windows_junctions_and_streams_refuse_without_record_changes(self):
        original = self.success(self.ingest())
        before = self.retained()
        source_alias = self.root / "source junction"
        workspace_alias = self.root / "record junction"
        self.junction(source_alias, self.sources)
        self.junction(workspace_alias, self.workspace)
        for result in (self.ingest(root=source_alias), self.ingest("note.txt:stream"),
                       self.ingest("C:/outside.txt"),
                       self.invoke("show", "--handle", original["handle"], workspace=workspace_alias),
                       self.invoke("show", "--handle", original["handle"], workspace=workspace_alias,
                                   via_machinery=True),
                       self.ingest(workspace=workspace_alias)):
            self.assertEqual(result.returncode, 2, result.stdout)
            self.assertEqual(self.retained(), before)

    @unittest.skipUnless(os.name == "nt", "Native Windows held source handle")
    def test_windows_source_cannot_be_replaced_during_ingestion(self):
        replacement = self.sources / "replacement.txt"
        replacement.write_text("A substituted source.\n", encoding="utf-8")
        plumber = Plumber(self.workspace)
        original_read = portable_files.os.read
        attempted = []

        def read(fd, size):
            if not attempted:
                attempted.append(True)
                with self.assertRaises(OSError):
                    os.replace(replacement, self.source)
            return original_read(fd, size)

        with patch.object(portable_files.os, "read", side_effect=read):
            handle = plumber.ingest(self.sources, ["note.txt"])
        self.assertEqual(attempted, [True])
        self.assertEqual(plumber.records.read(handle)["payload"]["sources"][0]["text"], self.body)
        self.assertTrue(replacement.is_file())

    @unittest.skipUnless(os.name == "nt", "Native Windows root custody across source reads")
    def test_windows_root_remains_pinned_between_reads_and_through_publication(self):
        (self.sources / "second.txt").write_text("Second source fixture.\n", encoding="utf-8")
        moved = self.root / "moved source root"
        plumber = Plumber(self.workspace)
        original_anchor, original_publish = ingestion.anchor, plumber.records.publish
        reads, publications = [], []

        def anchor(root, path):
            source = original_anchor(root, path)
            reads.append(path)
            # This boundary is after the individual source handle has closed.
            with self.assertRaises(OSError):
                self.sources.rename(moved)
            return source

        def publish(*args, **kwargs):
            publications.append(True)
            with self.assertRaises(OSError):
                self.sources.rename(moved)
            return original_publish(*args, **kwargs)

        with patch.object(ingestion, "anchor", side_effect=anchor), \
                patch.object(plumber.records, "publish", side_effect=publish):
            handle = plumber.ingest(self.sources, ["note.txt", "second.txt"])
        self.assertEqual(reads, ["note.txt", "second.txt"])
        self.assertEqual(publications, [True])
        self.assertEqual(plumber.records.read(handle)["payload"]["root"], str(self.sources))
        # Successful rename after return proves custody handles were released.
        self.sources.rename(moved)
        self.assertFalse(self.sources.exists())
        self.assertEqual(plumber.records.read(handle)["payload"]["sources"][0]["text"], self.body)


if __name__ == "__main__":
    unittest.main()
