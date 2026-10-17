import json
import tempfile
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import lodestone_memory as lm


class MemoryEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "memory"
        lm.ensure_root(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def add(self, title, body, source, tags=None):
        record = lm.make_record(
            title, body, source, "note", tags or [], 0.9
        )
        return lm.add_record(self.root, record)

    def test_duplicate_is_not_appended(self):
        first, created1 = self.add("A", "Same body", "a.md")
        second, created2 = self.add("A again", "Same body", "a.md")
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(first, second)
        self.assertEqual(len(lm.load_records(self.root)), 1)

    def test_search_finds_relevant_record(self):
        record_id, _ = self.add(
            "Rule 30 edge policy",
            "Finite rows require an explicit boundary rule.",
            "experiments/001-rule30/notes.md",
            ["rule30", "boundary"],
        )
        results = lm.search(self.root, "rule30 boundary")
        self.assertTrue(results)
        self.assertEqual(results[0].record["id"], record_id)

    def test_supersession_hides_old_record(self):
        old_id, _ = self.add("Old", "Earlier statement", "notes.md")
        new_id, _ = self.add("New", "Current statement", "notes.md")
        lm.supersede(self.root, old_id, new_id)
        current_ids = {x.record["id"] for x in lm.search(self.root, "statement", 10)}
        self.assertNotIn(old_id, current_ids)
        self.assertIn(new_id, current_ids)

    def test_verify_clean_store(self):
        self.add("A", "Body", "a.md")
        self.assertEqual(lm.verify(self.root), [])

    def test_unmatched_and_blank_queries_do_not_fill_context_with_confidence(self):
        self.add("Gardening", "Prune roses in winter.", "garden.md")
        before = {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()}
        for query in ("quartz", "", "   \t\n"):
            with self.subTest(query=query):
                self.assertEqual(lm.search(self.root, query), [])
                self.assertNotIn("Prune roses", lm.build_packet(self.root, query, 500))
        after = {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()}
        self.assertEqual(before, after)

    def test_search_excludes_unrelated_records_without_changing_relevant_score(self):
        relevant, _ = self.add("delta", "delta", "delta", ["delta"])
        self.add("Gardening", "Prune roses in winter.", "garden.md")
        results = lm.search(self.root, "delta")
        self.assertEqual([x.record["id"] for x in results], [relevant])
        self.assertAlmostEqual(results[0].score, 4.0 + 1.5 + 1.5 + 1.5 + 0.9)

    def test_explicit_query_link_retains_nonlexical_endpoints(self):
        first, _ = self.add("Gardening", "Prune roses", "garden.md")
        second, _ = self.add("Delta", "Association correction", "delta.md")
        lm.add_link(self.root, first, second, "questions", "quartz context")
        results = lm.search(self.root, "quartz")
        self.assertEqual({x.record["id"] for x in results}, {first, second})
        self.assertTrue(all(abs(x.score - 4.4) < 1e-9 for x in results))
        self.assertEqual(lm.search(self.root, ""), [])

    def test_related_requires_overlap_or_an_explicit_edge(self):
        first, _ = self.add("Delta", "Association correction", "delta.md")
        unrelated, _ = self.add("Gardening", "Prune roses", "garden.md")
        matched, _ = self.add("Correction", "Association delta", "memory.md")
        self.assertEqual([x.record["id"] for x in lm.related(self.root, first)], [matched])
        lm.add_link(self.root, first, unrelated, "contradicts")
        results = lm.related(self.root, first)
        self.assertEqual({x.record["id"] for x in results}, {matched, unrelated})
        self.assertAlmostEqual(next(x.score for x in results if x.record["id"] == unrelated), 3.9)

    def test_direct_phrase_and_superseded_matches_remain_available(self):
        # TOKEN_RE is ASCII; exact phrases remain a separate eligibility route.
        old, _ = self.add("記憶", "以前", "old.md")
        new, _ = self.add("記憶", "現在", "new.md")
        lm.supersede(self.root, old, new)
        self.assertEqual([x.record["id"] for x in lm.search(self.root, "記憶")], [new])
        self.assertEqual({x.record["id"] for x in lm.search(self.root, "記憶", include_superseded=True)}, {old, new})


if __name__ == "__main__":
    unittest.main()
