"""Independent address/provenance cases; no model, daemon or network needed."""

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

MEMORY_TOOL = Path(__file__).resolve().parents[1]
PROJECT = MEMORY_TOOL.parents[1]
sys.path.insert(0, str(PROJECT / "src"))
sys.path.insert(0, str(MEMORY_TOOL / "src"))

import lodestone_memory as lm
import source_addresses as addresses


class SourceAddressTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="source addresses ")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "notebook"
        self.scope = self.base / "captured sources"
        self.scope.mkdir()
        self.manifest = self.base / "source-bindings.json"
        lm.ensure_root(self.root)

    def note(self, title="Harbour decision", body="The harbour uses the eastern gate.",
             source="fixture://harbour"):
        value = lm.make_record(title, body, source, "note", [], 0.8)
        identity, _ = lm.add_record(self.root, value)
        return identity

    def source(self, relative="harbour.txt", raw=b"Heading\nUse the eastern gate.\nEnd.\n"):
        path = self.scope / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return path

    def bind(self, identity, source="harbour.txt", start=2, end=2, **changes):
        args = dict(root=self.root, manifest=self.manifest, scope=self.scope,
                    record_id=identity, source=source, start_line=start, end_line=end,
                    capture="fixture-capture-01", attribution="Fixture author", source_url=None)
        args.update(changes)
        return addresses.add_binding(**args)

    def resolve(self, query="harbour", **changes):
        args = dict(root=self.root, manifest=self.manifest, scope=self.scope,
                    query=query, limit=10, include_superseded=False)
        args.update(changes)
        return addresses.resolve(**args)

    def seed(self):
        self.source()
        identity = self.note()
        self.bind(identity)
        return identity

    @staticmethod
    def snapshot(directory):
        return {str(path.relative_to(directory)): path.read_bytes()
                for path in directory.rglob("*") if path.is_file()}

    def row(self, report, identity):
        matches = [row for row in report["results"] if row["record_id"] == identity]
        self.assertEqual(len(matches), 1)
        return matches[0]

    def write_manifest(self, value):
        self.manifest.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def test_actual_repository_capture_returns_inspectable_section_and_provenance(self):
        raw = (PROJECT / "README.md").read_bytes()
        self.source("release/README.md", raw)
        identity = self.note("Project knowledge handoff", "Find the public toolkit's purpose.",
                             "repository://README.md")
        url = "https://github.com/zacheriasliang-glitch/lodestone/blob/80e3de0d72ae02de065d463f4d45a176df81c7a2/README.md"
        memory_before = self.snapshot(self.root)
        sources_before = self.snapshot(self.scope)
        with patch("socket.create_connection", side_effect=AssertionError("No network for source addresses")):
            added = self.bind(identity, "release/README.md", 3, 4,
                              capture="public-source-copy", attribution="Lodestone README authors",
                              source_url=url)
            report = self.resolve("project knowledge")
        expected = ("**Keep useful project knowledge between AI sessions, recover your files, and run\n"
                    "local toolkit tasks with a record of what happened.**\n")
        self.assertTrue(added["created"])
        self.assertEqual(added["text"], expected)
        self.assertEqual(report["schema"], "lodestone-source-addresses/v1")
        self.assertEqual(report["query"], "project knowledge")
        self.assertEqual(report["scope"], str(self.scope.resolve()))
        self.assertEqual(report["manifest_sha256"], hashlib.sha256(self.manifest.read_bytes()).hexdigest())
        result = self.row(report, identity)
        self.assertEqual(result["title"], "Project knowledge handoff")
        self.assertEqual(result["status"], "VERIFIED_ADDRESS")
        self.assertEqual(len(result["bindings"]), 1)
        binding = result["bindings"][0]
        self.assertEqual(binding["source"], "release/README.md")
        self.assertEqual((binding["start_line"], binding["end_line"]), (3, 4))
        self.assertEqual(binding["source_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(binding["slice_sha256"], hashlib.sha256(expected.encode()).hexdigest())
        self.assertEqual(binding["expansion"]["text"], expected)
        self.assertTrue(binding["expansion"]["coordinates_valid"])
        self.assertEqual(binding["capture"], "public-source-copy")
        self.assertEqual(binding["attribution"], "Lodestone README authors")
        self.assertEqual(binding["source_url"], url)
        self.assertEqual(self.snapshot(self.root), memory_before)
        self.assertEqual(self.snapshot(self.scope), sources_before)

    def test_unicode_and_line_endings_remain_exact(self):
        raw = "Title\r\n北門 — café\r\nEnd\r\n".encode("utf-8")
        self.source(raw=raw)
        identity = self.note()
        self.bind(identity)
        binding = self.row(self.resolve(), identity)["bindings"][0]
        self.assertEqual(binding["expansion"]["text"], "北門 — café\r\n")
        self.assertEqual(binding["slice_sha256"], hashlib.sha256("北門 — café\r\n".encode()).hexdigest())

    def test_bind_shows_actual_text_with_universal_line_boundaries(self):
        self.source(raw="Heading\fSecond\u2028Third\n".encode("utf-8"))
        identity = self.note()
        first = self.bind(identity)
        repeated = self.bind(identity)
        self.assertTrue(first["created"])
        self.assertFalse(repeated["created"])
        self.assertEqual(first["text"], "Second\u2028")
        self.assertEqual(repeated["text"], first["text"])
        self.assertEqual(first["binding"]["slice_sha256"],
                         hashlib.sha256("Second\u2028".encode("utf-8")).hexdigest())
        self.assertEqual(self.row(self.resolve(), identity)["bindings"][0]["expansion"]["text"],
                         first["text"])

    def test_changed_selected_source_is_stale_even_when_note_is_unchanged(self):
        identity = self.seed()
        memory_before = self.snapshot(self.root)
        manifest_before = self.manifest.read_bytes()
        self.source(raw=b"Heading\nUse the western gate.\nEnd.\n")
        result = self.row(self.resolve(), identity)
        self.assertEqual(result["status"], "STALE")
        self.assertEqual(result["bindings"][0]["expansion"]["text"], "Use the western gate.\n")
        self.assertEqual(self.snapshot(self.root), memory_before)
        self.assertEqual(self.manifest.read_bytes(), manifest_before)

    def test_change_outside_selected_lines_also_invalidates_full_source_pin(self):
        identity = self.seed()
        self.source(raw=b"New heading\nUse the eastern gate.\nEnd.\n")
        binding = self.row(self.resolve(), identity)["bindings"][0]
        self.assertEqual(binding["status"], "STALE")
        self.assertEqual(binding["expansion"]["text"], "Use the eastern gate.\n")
        self.assertEqual(binding["expansion"]["slice_sha256"], binding["slice_sha256"])
        self.assertNotEqual(binding["expansion"]["source_sha256"], binding["source_sha256"])

    def test_removed_source_is_unavailable_without_invented_replacement(self):
        identity = self.seed()
        (self.scope / "harbour.txt").unlink()
        result = self.row(self.resolve(), identity)
        self.assertEqual(result["status"], "UNAVAILABLE")
        binding = result["bindings"][0]
        self.assertEqual(binding["status"], "UNAVAILABLE")
        self.assertIsNone(binding["expansion"])
        self.assertIsInstance(binding["reason"], str)
        self.assertTrue(binding["reason"])

    def test_shrunken_source_reports_stale_invalid_coordinates(self):
        identity = self.seed()
        self.source(raw=b"Only one line.\n")
        result = self.row(self.resolve(), identity)
        self.assertEqual(result["status"], "STALE")
        self.assertFalse(result["bindings"][0]["expansion"]["coordinates_valid"])

    def test_alias_link_finds_candidates_but_does_not_transfer_source_authority(self):
        target = self.seed()
        alias = self.note("Legacy marker", "An attributed association only.", "fixture://alias")
        lm.add_link(self.root, alias, target, "explicit", "amberstone alias supplied by author")
        report = self.resolve("amberstone")
        self.assertEqual({item["record_id"] for item in report["results"]}, {target, alias})
        self.assertEqual(self.row(report, target)["status"], "VERIFIED_ADDRESS")
        unknown = self.row(report, alias)
        self.assertEqual(unknown["status"], "UNKNOWN_WITHIN_SCOPE")
        self.assertEqual(unknown["bindings"], [])

    def test_superseded_notes_are_hidden_unless_explicitly_requested(self):
        old = self.seed()
        new = self.note("Harbour revision", "Harbour arrival now needs a booking.", "fixture://revision")
        self.source("revision.txt", b"Harbour revision\nA booking is required.\n")
        self.bind(new, "revision.txt")
        lm.supersede(self.root, old, new)
        self.assertEqual([item["record_id"] for item in self.resolve()["results"]], [new])
        expanded = self.resolve(include_superseded=True)
        self.assertEqual({item["record_id"] for item in expanded["results"]}, {old, new})
        self.assertEqual(self.row(expanded, old)["status"], "VERIFIED_ADDRESS")

    def test_multiple_addresses_remain_ambiguous_and_all_are_checked(self):
        identity = self.seed()
        self.source("second.txt", b"Other record\nA conflicting harbour address.\n")
        self.bind(identity, "second.txt", capture="second-capture")
        result = self.row(self.resolve(), identity)
        self.assertEqual(result["status"], "AMBIGUOUS")
        self.assertEqual({item["source"] for item in result["bindings"]}, {"harbour.txt", "second.txt"})
        self.assertEqual({item["status"] for item in result["bindings"]}, {"VERIFIED_ADDRESS"})
        (self.scope / "second.txt").unlink()
        result = self.row(self.resolve(), identity)
        self.assertEqual(result["status"], "AMBIGUOUS")
        self.assertEqual({item["status"] for item in result["bindings"]}, {"VERIFIED_ADDRESS", "UNAVAILABLE"})

    def test_no_candidate_and_unbound_note_are_scoped_unknown(self):
        self.seed()
        unknown = self.note("Unlocated meteor", "Meteor origin is not in this captured corpus.", "fixture://unknown")
        result = self.row(self.resolve("meteor"), unknown)
        self.assertEqual(result["status"], "UNKNOWN_WITHIN_SCOPE")
        self.assertEqual(result["bindings"], [])
        for query in ("quartz-no-such-record", "", "  \t\n"):
            with self.subTest(query=query):
                report = self.resolve(query)
                self.assertEqual(report["results"], [])
                self.assertEqual(report["status"], "UNKNOWN_WITHIN_SCOPE")

    def test_identical_binding_is_idempotent_and_does_not_rewrite_manifest(self):
        identity = self.seed()
        before = self.manifest.read_bytes()
        added = self.bind(identity)
        self.assertFalse(added["created"])
        self.assertEqual(self.manifest.read_bytes(), before)
        self.assertEqual(len(json.loads(before)["bindings"]), 1)

    def test_source_registration_and_resolution_leave_memory_and_unrelated_files_unchanged(self):
        self.source()
        self.source("unrelated.txt", b"Do not change unrelated source bytes.\n")
        identity = self.note()
        before_memory = self.snapshot(self.root)
        before_sources = self.snapshot(self.scope)
        self.bind(identity)
        before_manifest = self.manifest.read_bytes()
        self.resolve()
        self.resolve("unrelated-never-found")
        self.assertEqual(self.snapshot(self.root), before_memory)
        self.assertEqual(self.snapshot(self.scope), before_sources)
        self.assertEqual(self.manifest.read_bytes(), before_manifest)

    def test_manifest_must_be_outside_the_memory_store(self):
        self.source()
        identity = self.note()
        forbidden = self.root / "source-bindings.json"
        before = self.snapshot(self.root)
        with self.assertRaises(ValueError):
            self.bind(identity, manifest=forbidden)
        self.assertFalse(forbidden.exists())
        self.assertEqual(self.snapshot(self.root), before)

    def test_invalid_line_coordinates_refuse_without_publishing(self):
        identity = self.seed()
        before = self.manifest.read_bytes()
        for start, end in ((0, 1), (-1, 2), (2, 1), (1, 99), (True, 2), (1, False),
                           (1.0, 2), (1, "2")):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                self.bind(identity, start=start, end=end)
            self.assertEqual(self.manifest.read_bytes(), before)

    def test_absolute_traversal_and_noncanonical_source_paths_refuse(self):
        identity = self.seed()
        before = self.manifest.read_bytes()
        outside = self.base / "outside.txt"
        outside.write_bytes(b"Outside source must not be accepted.\n")
        for source in ("../outside.txt", str(outside), "./harbour.txt", "sub/../harbour.txt",
                       "sub//harbour.txt", "sub\\harbour.txt", ""):
            with self.subTest(source=source), self.assertRaises(ValueError):
                self.bind(identity, source=source, start=1, end=1)
            self.assertEqual(self.manifest.read_bytes(), before)

    def test_source_symlink_cannot_escape_scope_on_add_or_retrieval(self):
        identity = self.seed()
        outside = self.base / "outside.txt"
        outside.write_bytes(b"Foreign heading\nDo not expose this line.\n")
        alias = self.scope / "alias.txt"
        try:
            alias.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("Symlink creation unavailable on this platform")
        before = self.manifest.read_bytes()
        with self.assertRaises(ValueError):
            self.bind(identity, "alias.txt")
        path = self.scope / "harbour.txt"
        path.unlink()
        path.symlink_to(outside)
        with self.assertRaises(ValueError):
            self.resolve()
        self.assertEqual(self.manifest.read_bytes(), before)
        self.assertEqual(outside.read_bytes(), b"Foreign heading\nDo not expose this line.\n")

    def test_malformed_manifest_structure_and_hashes_refuse(self):
        self.seed()
        valid = json.loads(self.manifest.read_text())
        malformed = [[], {"schema": "wrong", "bindings": []},
                     {**valid, "bindings": "not-a-list"}]
        for key, value in (("source_sha256", "not-a-hash"), ("slice_sha256", "0" * 63),
                           ("record_content_hash", "G" * 64), ("start_line", True),
                           ("end_line", 0), ("source", "../outside.txt")):
            bad = deepcopy(valid)
            bad["bindings"][0][key] = value
            malformed.append(bad)
        for value in malformed:
            with self.subTest(value=value):
                self.write_manifest(value)
                before = self.manifest.read_bytes()
                with self.assertRaises(ValueError):
                    self.resolve()
                self.assertEqual(self.manifest.read_bytes(), before)

    def test_duplicate_json_keys_and_invalid_utf8_manifest_refuse(self):
        self.seed()
        for raw in (b'{"schema":"lodestone-source-bindings/v1","schema":"wrong","bindings":[]}',
                    b'{"schema":', b'\xff\xfe'):
            with self.subTest(raw=raw):
                self.manifest.write_bytes(raw)
                with self.assertRaises(ValueError):
                    self.resolve()
                self.assertEqual(self.manifest.read_bytes(), raw)

    def test_manifest_byte_and_binding_count_bounds_refuse(self):
        self.seed()
        valid = json.loads(self.manifest.read_text())
        self.manifest.write_bytes(b" " * (1024 * 1024 + 1))
        with self.assertRaises(ValueError):
            self.resolve()
        many = deepcopy(valid)
        many["bindings"] = [dict(valid["bindings"][0], capture=f"capture-{number}")
                            for number in range(513)]
        self.write_manifest(many)
        self.assertLess(self.manifest.stat().st_size, 1024 * 1024)
        with self.assertRaises(ValueError):
            self.resolve()

    def test_source_limit_is_two_mib_even_when_selected_excerpt_is_small(self):
        raw = b"OK\n" + b"x" * (2 * 1024 * 1024 - 3)
        path = self.source(raw=raw)
        identity = self.note()
        self.bind(identity, start=1, end=1)
        self.assertEqual(self.row(self.resolve(), identity)["status"], "VERIFIED_ADDRESS")
        path.write_bytes(raw + b"x")
        with self.assertRaises(ValueError):
            self.resolve()

    def test_selected_excerpt_limit_counts_exact_utf8_bytes(self):
        self.source("exact.txt", b"a" * (16 * 1024 - 1) + b"\n")
        self.source("over.txt", b"a" * (16 * 1024) + b"\n")
        identity = self.note()
        self.bind(identity, "exact.txt", 1, 1)
        before = self.manifest.read_bytes()
        with self.assertRaises(ValueError):
            self.bind(identity, "over.txt", 1, 1)
        self.assertEqual(self.manifest.read_bytes(), before)

    def test_invalid_utf8_source_refuses_on_add_and_after_valid_binding(self):
        identity = self.seed()
        self.source("invalid.txt", b"Heading\n\xff\n")
        before = self.manifest.read_bytes()
        with self.assertRaises(ValueError):
            self.bind(identity, "invalid.txt")
        self.source(raw=b"Heading\n\xff\n")
        with self.assertRaises(ValueError):
            self.resolve()
        self.assertEqual(self.manifest.read_bytes(), before)

    def test_tampered_memory_body_is_not_trusted_as_the_bound_note(self):
        self.seed()
        records = lm.load_records(self.root)
        records[0]["body"] = "Changed without updating the recorded content hash."
        lm.write_jsonl(self.root / "records.jsonl", records)
        before = self.manifest.read_bytes()
        with self.assertRaises(ValueError):
            self.resolve()
        self.assertEqual(self.manifest.read_bytes(), before)

    def test_wrong_stored_source_pin_is_stale_not_verified(self):
        identity = self.seed()
        value = json.loads(self.manifest.read_text())
        value["bindings"][0]["source_sha256"] = "0" * 64
        self.write_manifest(value)
        self.assertEqual(self.row(self.resolve(), identity)["status"], "STALE")

    def test_unknown_record_and_binding_to_wrong_note_hash_refuse(self):
        identity = self.seed()
        before = self.manifest.read_bytes()
        with self.assertRaises(ValueError):
            self.bind("0" * 16)
        self.assertEqual(self.manifest.read_bytes(), before)
        value = json.loads(before)
        value["bindings"][0]["record_content_hash"] = "0" * 64
        self.write_manifest(value)
        with self.assertRaises(ValueError):
            self.resolve()

    def test_query_limit_requires_an_integer_between_one_and_ten(self):
        self.seed()
        for limit in (0, 11, -1, True, False, 1.0, "2"):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.resolve(limit=limit)

    def test_aggregate_source_read_bound_counts_each_valid_source(self):
        identity = self.note()
        raw = b"OK\n" + b"x" * (2 * 1024 * 1024 - 3)
        for number in range(8):
            name = f"large-{number}.txt"
            self.source(name, raw)
            self.bind(identity, name, 1, 1)
        # Eight individually valid sources exactly fill the 16 MiB read budget.
        accepted = self.row(self.resolve(), identity)
        self.assertEqual(len(accepted["bindings"]), 8)
        self.assertTrue(all(item["status"] == "VERIFIED_ADDRESS"
                            for item in accepted["bindings"]))
        self.source("large-8.txt", raw)
        self.bind(identity, "large-8.txt", 1, 1)
        before = self.manifest.read_bytes()
        with self.assertRaisesRegex(ValueError, "Source reopening exceeds total byte bound"):
            self.resolve()
        self.assertEqual(self.manifest.read_bytes(), before)

    def test_binding_fanout_limit_is_independent_of_source_and_manifest_byte_limits(self):
        identity = self.note()
        for number in range(64):
            name = f"small-{number}.txt"
            self.source(name, b"Exact small source.\n")
            self.bind(identity, name, 1, 1)
        accepted = self.row(self.resolve(), identity)
        self.assertEqual(len(accepted["bindings"]), 64)
        self.assertTrue(all(item["status"] == "VERIFIED_ADDRESS"
                            for item in accepted["bindings"]))
        self.source("small-64.txt", b"Exact small source.\n")
        self.bind(identity, "small-64.txt", 1, 1)
        self.assertLess(self.manifest.stat().st_size, 1024 * 1024)
        with self.assertRaisesRegex(ValueError, "Too many bindings to reopen in one query"):
            self.resolve()

    def test_result_byte_limit_counts_canonical_utf8_escaping_and_final_newline(self):
        self.source(raw='Heading\nQuote: "yes"; path C:\\notes; café 🪵.\n'.encode("utf-8"))
        identity = self.note('Harbour "quoted" café')
        self.bind(identity)
        report = self.resolve()
        # An independent serializer specifies the public wire contract. Counting
        # characters, unescaped strings or omitting the final newline is wrong.
        wire = (json.dumps(report, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")) + "\n").encode("utf-8")
        self.assertEqual(addresses.MAX_RESULT_BYTES, 1024 * 1024)
        self.assertGreater(len(wire), len(wire.decode("utf-8")))
        with patch.object(addresses, "MAX_RESULT_BYTES", len(wire) - 1):
            with self.assertRaisesRegex(ValueError, "Address result exceeds byte bound"):
                self.resolve()
        with patch.object(addresses, "MAX_RESULT_BYTES", len(wire)):
            self.assertEqual(self.resolve(), report)
        emitted = self.cli("source-address", str(self.root), "harbour",
                           "--bindings", str(self.manifest), "--scope", str(self.scope))
        self.assertEqual(emitted.returncode, 0, emitted.stderr)
        self.assertEqual(emitted.stdout.encode("utf-8"), wire)

    def test_limit_one_keeps_overall_ambiguity_visible_in_api_and_cli(self):
        first = self.seed()
        second = self.note("Harbour alternative", "Another harbour source must remain visible.",
                           "fixture://second-harbour")
        self.source("alternative.txt", b"Alternative\nAnother source address.\n")
        self.bind(second, "alternative.txt")
        api = self.resolve(limit=1)
        emitted = self.cli("source-address", str(self.root), "harbour",
                           "--bindings", str(self.manifest), "--scope", str(self.scope),
                           "--limit", "1")
        self.assertEqual(emitted.returncode, 0, emitted.stderr)
        for report in (api, json.loads(emitted.stdout)):
            self.assertEqual(report["status"], "AMBIGUOUS")
            self.assertTrue(report["truncated"])
            self.assertEqual(len(report["results"]), 1)
            self.assertIn(report["results"][0]["record_id"], {first, second})
            self.assertEqual(report["results"][0]["status"], "VERIFIED_ADDRESS")

    def cli(self, *args):
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONIOENCODING"] = "ascii"
        return subprocess.run([sys.executable, "-B", str(PROJECT / "lodestone.py"), "memory", *args],
                              cwd=self.base, env=env, encoding="utf-8", capture_output=True,
                              timeout=15, check=False)

    def test_public_launcher_binds_then_resolves_in_separate_fresh_processes(self):
        self.source()
        identity = self.note()
        before = self.snapshot(self.root)
        added = self.cli("source-bind", str(self.root), identity,
                         "--bindings", str(self.manifest), "--scope", str(self.scope),
                         "--source", "harbour.txt", "--start-line", "2", "--end-line", "2",
                         "--capture", "cli-capture", "--attribution", "CLI fixture author")
        self.assertEqual(added.returncode, 0, added.stderr)
        self.assertTrue(json.loads(added.stdout)["created"])
        self.assertEqual(json.loads(added.stdout)["text"], "Use the eastern gate.\n")
        resolved = self.cli("source-address", str(self.root), "harbour",
                            "--bindings", str(self.manifest), "--scope", str(self.scope), "--limit", "1")
        self.assertEqual(resolved.returncode, 0, resolved.stderr)
        binding = self.row(json.loads(resolved.stdout), identity)["bindings"][0]
        self.assertEqual(binding["status"], "VERIFIED_ADDRESS")
        self.assertEqual(binding["expansion"]["text"], "Use the eastern gate.\n")
        self.assertEqual(binding["capture"], "cli-capture")
        self.assertEqual(self.snapshot(self.root), before)

    def test_public_launcher_reports_missing_source_and_refuses_invalid_limit(self):
        identity = self.seed()
        (self.scope / "harbour.txt").unlink()
        args = ("source-address", str(self.root), "harbour", "--bindings", str(self.manifest),
                "--scope", str(self.scope))
        missing = self.cli(*args)
        self.assertEqual(missing.returncode, 0, missing.stderr)
        self.assertEqual(self.row(json.loads(missing.stdout), identity)["status"], "UNAVAILABLE")
        invalid = self.cli(*args, "--limit", "11")
        self.assertNotEqual(invalid.returncode, 0)
        self.assertNotIn("Traceback", invalid.stderr)


if __name__ == "__main__":
    unittest.main()
