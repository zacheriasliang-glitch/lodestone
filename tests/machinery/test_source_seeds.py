"""Exact D1→Origami boundary cases; scripted fixtures, not semantic evidence."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT / "tools/local-memory/src"))
import lodestone_memory as memory
import source_addresses as addresses

from lodestone_machinery.origami import Origami
from lodestone_machinery.source_seeds import source_address_seeds


class SourceSeedTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="source-seed-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.scope = self.base / "captures"
        self.scope.mkdir()
        self.notebook = self.base / "notebook"
        memory.ensure_root(self.notebook)
        self.manifest = self.base / "bindings.json"
        self.workspace = self.base / "workspace"
        self.organ = Origami(self.workspace)

    def record(self, title="Harbour entry"):
        record = memory.make_record(title, "The harbour entrance is recorded in the capture.",
                                    "fixture://" + title, "note", [], 0.8)
        rid, _ = memory.add_record(self.notebook, record)
        return rid

    def bind(self, rid, *, name="harbour.txt", start=2, end=2):
        return addresses.add_binding(self.notebook, self.manifest, self.scope, rid,
                                     name, start, end, "fixture-capture", "Fixture author")

    def report(self):
        return addresses.resolve(self.notebook, self.manifest, self.scope, "harbour")

    def prepare(self, raw=b"Heading\nUse the eastern gate.\nEnd.\n", start=2, end=2):
        (self.scope / "harbour.txt").write_bytes(raw)
        rid = self.record()
        self.bind(rid, start=start, end=end)
        return rid, self.report()

    def start(self, selected, **options):
        return self.organ.start(**selected, question="Which entrance?", epoch="fixture-current",
                                authority_ref="fixture source reads", **options)

    def runs(self):
        directory = self.workspace / "origami"
        return list(directory.iterdir()) if directory.exists() else []

    def test_actual_resolver_seeds_exact_passage_and_retains_attribution(self):
        rid, report = self.prepare()
        selected = source_address_seeds(report)
        state = self.start(selected, initial_passages=0)
        request = self.organ.request(state["run_id"])
        self.assertEqual([p["text"] for p in request["evidence"]], ["Use the eastern gate.\n"])
        self.assertEqual(request["budget"]["evidence_bytes_used"], len(b"Use the eastern gate.\n"))
        intent = json.loads((self.runs()[0] / "intent.json").read_text())
        seed = intent["preparation"]["seed_passages"][0]
        provenance = seed["address_provenance"]
        self.assertEqual(provenance["record_id"], rid)
        self.assertEqual(provenance["manifest_sha256"], hashlib.sha256(self.manifest.read_bytes()).hexdigest())
        self.assertEqual(provenance["capture"], "fixture-capture")
        self.assertEqual(provenance["attribution"], "Fixture author")
        self.assertEqual(provenance["selector"], rid + ":0")
        self.assertIn("No resolver execution", intent["preparation"]["seed_provenance"])

    def test_unselected_whole_source_change_refuses_before_run(self):
        _, report = self.prepare()
        selected = source_address_seeds(report)
        (self.scope / "harbour.txt").write_bytes(b"Other heading\nUse the eastern gate.\nEnd.\n")
        with self.assertRaisesRegex(ValueError, "hash changed"):
            self.start(selected)
        self.assertEqual(self.runs(), [])

    def test_changed_selected_passage_refuses_before_run(self):
        _, report = self.prepare()
        selected = source_address_seeds(report)
        (self.scope / "harbour.txt").write_bytes(b"Heading\nUse the western gate.\nEnd.\n")
        with self.assertRaisesRegex(ValueError, "hash changed"):
            self.start(selected)
        self.assertEqual(self.runs(), [])

    def test_ambiguity_requires_selection_and_selection_is_preserved(self):
        _, _ = self.prepare()
        second = self.record("Harbour second record")
        (self.scope / "second.txt").write_bytes(b"Use a ferry.\n")
        self.bind(second, name="second.txt", start=1, end=1)
        report = self.report()
        self.assertEqual(report["status"], "AMBIGUOUS")
        with self.assertRaisesRegex(ValueError, "explicit"):
            source_address_seeds(report)
        selected = source_address_seeds(report, selections=[second + ":0"])
        state = self.start(selected)
        self.assertEqual(self.organ.request(state["run_id"])["evidence"][0]["text"], "Use a ferry.\n")
        self.assertEqual(selected["paths"], ["second.txt"])

    def test_multiple_bindings_in_one_record_require_exact_binding_selection(self):
        rid, _ = self.prepare()
        self.bind(rid, start=1, end=1)
        report = self.report()
        self.assertEqual(report["status"], "AMBIGUOUS")
        with self.assertRaisesRegex(ValueError, "explicit"):
            source_address_seeds(report)
        state = self.start(source_address_seeds(report, selections=[rid + ":1"]))
        self.assertEqual(self.organ.request(state["run_id"])["evidence"][0]["text"], "Heading\n")

    def test_truncated_report_cannot_be_silently_selected(self):
        rid, report = self.prepare()
        report["truncated"] = True
        with self.assertRaisesRegex(ValueError, "Truncated"):
            source_address_seeds(report, selections=[rid + ":0"])
        self.assertEqual(self.runs(), [])

    def test_duplicate_passages_across_records_are_refused(self):
        first, _ = self.prepare()
        second = self.record("Harbour duplicate reference")
        self.bind(second)
        with self.assertRaisesRegex(ValueError, "same exact passage"):
            source_address_seeds(self.report(), selections=[first + ":0", second + ":0"])

    def test_unknown_and_duplicate_selection_refuse(self):
        rid, report = self.prepare()
        for selections in ([rid + ":1"], ["f" * 16 + ":0"], [rid + ":0", rid + ":0"],
                           [rid + ":00"], [], [False]):
            with self.subTest(selections=selections), self.assertRaises(ValueError):
                source_address_seeds(report, selections=selections)
        self.assertEqual(self.runs(), [])

    def test_stale_binding_cannot_be_selected_from_ambiguous_report(self):
        rid, _ = self.prepare()
        second = self.record("Harbour other")
        (self.scope / "second.txt").write_bytes(b"Other passage.\n")
        self.bind(second, name="second.txt", start=1, end=1)
        (self.scope / "harbour.txt").write_bytes(b"Changed source.\n")
        report = self.report()
        with self.assertRaisesRegex(ValueError, "unavailable|VERIFIED_ADDRESS"):
            source_address_seeds(report, selections=[rid + ":0"])
        self.assertEqual(self.runs(), [])

    def test_forged_expansion_text_does_not_override_reopened_bytes(self):
        _, report = self.prepare()
        report["results"][0]["bindings"][0]["expansion"]["text"] = "Use the western gate.\n"
        with self.assertRaisesRegex(ValueError, "text hash"):
            source_address_seeds(report)
        self.assertEqual(self.runs(), [])

    def test_universal_line_boundaries_refuse_instead_of_reinterpreting_coordinates(self):
        rid, report = self.prepare("Heading\u2028Use the eastern gate.\nEnd.\n".encode("utf-8"))
        self.assertEqual(report["status"], "VERIFIED_ADDRESS")
        selected = source_address_seeds(report)
        with self.assertRaisesRegex(ValueError, "line conventions differ"):
            self.start(selected)
        self.assertEqual(self.runs(), [])

    def test_line_mismatch_refuses_even_if_selected_text_happens_to_match(self):
        _, report = self.prepare(b"same\nsame\rsame\n", start=1, end=1)
        with self.assertRaisesRegex(ValueError, "line conventions differ"):
            self.start(source_address_seeds(report))
        self.assertEqual(self.runs(), [])

    def test_crlf_bytes_are_preserved_when_line_boundaries_agree(self):
        _, report = self.prepare(b"Heading\r\nUse the eastern gate.\r\nEnd.\r\n")
        state = self.start(source_address_seeds(report))
        evidence = self.organ.request(state["run_id"])["evidence"][0]
        self.assertEqual(evidence["text"], "Use the eastern gate.\r\n")
        self.assertEqual(evidence["start_byte"], len(b"Heading\r\n"))

    def test_direct_seed_validates_hashes_scope_and_duplicates_before_state(self):
        _, report = self.prepare()
        selected = source_address_seeds(report)
        handle = {k: v for k, v in selected["seed_passages"][0].items()
                  if k not in ("line_convention", "address_provenance")}
        cases = [[{**handle, "slice_sha256": "0" * 64}], [{**handle, "source": "missing.txt"}],
                 [{**handle, "start_line": True}], [handle, handle], [], [handle] * 65]
        for seeds in cases:
            with self.subTest(seeds=seeds), self.assertRaises(ValueError):
                self.start({**selected, "seed_passages": seeds})
        self.assertEqual(self.runs(), [])

    def test_seed_budget_refuses_without_truncating_the_requested_evidence(self):
        _, report = self.prepare(b"x" * 1100 + b"\n", start=1, end=1)
        with self.assertRaisesRegex(ValueError, "byte budget"):
            self.start(source_address_seeds(report), max_read_bytes=1024)
        self.assertEqual(self.runs(), [])

    def test_default_no_seed_path_preserves_lexical_selection(self):
        self.prepare()
        state = self.organ.start(self.scope, ["harbour.txt"], "Which entrance?", "current",
                                 "fixture reads", initial_passages=0)
        self.assertEqual(self.organ.request(state["run_id"])["evidence"], [])
        intent = json.loads((self.runs()[0] / "intent.json").read_text())
        self.assertNotIn("seed_passages", intent["preparation"])
        self.assertIn("lexical hints", intent["preparation"]["initial_selection"])


if __name__ == "__main__":
    unittest.main()
