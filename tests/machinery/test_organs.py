"""Behavioral and adversarial checks for bounded, source-grounded organs."""

import copy
import hashlib
import json
import math
from pathlib import Path
import tempfile
import unittest

from lodestone_machinery.organs import (
    anchor,
    audit_brief,
    build_brief,
    expand,
    fold,
    measure,
    pressure,
    residue,
)


class SourceFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "sources"
        self.root.mkdir()
        self.write("a.md", "Amber evidence is bounded.\nSecond amber line.\n\nA different paragraph.\n")
        self.write("b.md", "Amber has a second independent source.\n\nBeta remains unresolved.\n")
        self.write("unrelated.md", "A quiet green forest.\n")

    def write(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))
        return target

    def selected(self, query="amber", max_passages=8):
        return fold(self.root, ["a.md", "b.md", "unrelated.md"], query, max_passages)

    def assert_json(self, value):
        json.dumps(value, allow_nan=False)


class AnchorTests(SourceFixture):
    def test_reads_exact_original_bytes_and_strict_utf8(self):
        raw = "Café\r\nsecond line\n".encode("utf-8")
        (self.root / "unicode.md").write_bytes(raw)
        result = anchor(self.root, "unicode.md")
        self.assertEqual(result["id"], "unicode.md")
        self.assertEqual(result["text"].encode("utf-8"), raw)
        self.assertEqual(result["bytes"], len(raw))
        self.assertEqual(result["sha256"], hashlib.sha256(raw).hexdigest())
        self.assert_json(result)

    def test_rejects_traversal_and_absolute_path(self):
        (self.base / "outside.md").write_text("private", encoding="utf-8")
        for path in ("../outside.md", str(self.base / "outside.md")):
            with self.subTest(path=path), self.assertRaises(ValueError):
                anchor(self.root, path)

    def test_rejects_symlink_escape(self):
        outside = self.base / "outside.md"
        outside.write_text("private", encoding="utf-8")
        (self.root / "escape.md").symlink_to(outside)
        with self.assertRaises(ValueError):
            anchor(self.root, "escape.md")

    def test_rejects_invalid_utf8_and_oversized_source(self):
        (self.root / "invalid.md").write_bytes(b"valid then \xff")
        (self.root / "large.md").write_bytes(b"x" * (2 * 1024 * 1024 + 1))
        for path in ("invalid.md", "large.md"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                anchor(self.root, path)

    def test_rejects_missing_or_directory_source(self):
        for path in ("missing.md", "."):
            with self.subTest(path=path), self.assertRaises(ValueError):
                anchor(self.root, path)


class FoldTests(SourceFixture):
    def test_selection_is_deterministic_and_source_grounded(self):
        result = self.selected()
        self.assertEqual(result, self.selected())
        self.assertTrue(result["passages"])
        self.assertIn("lexical", str(result["selection_method"]).lower())
        for passage in result["passages"]:
            self.assertIn("amber", passage["text"].lower())
            self.assertEqual(
                passage["slice_sha256"],
                hashlib.sha256(passage["text"].encode("utf-8")).hexdigest(),
            )
            self.assertTrue(expand(self.root, passage)["fresh"])
        self.assert_json(result)

    def test_matching_source_relationships_are_preserved(self):
        result = self.selected()
        paths = {
            passage.get("source", passage.get("path"))
            for passage in result["passages"]
        }
        self.assertIn("a.md", paths)
        self.assertIn("b.md", paths)
        self.assertNotIn("unrelated.md", paths)

    def test_zero_match_has_no_arbitrary_successful_passage(self):
        result = self.selected("quasar")
        self.assertEqual(result["passages"], [])
        self.assertTrue(result["insufficient"])
        self.assertIn("quasar", result["missing_terms"])
        self.assertEqual(audit_brief(self.root, result, build_brief(result))["verdict"], "UNKNOWN")

    def test_empty_corpus_discloses_insufficiency(self):
        result = fold(self.root, [], "amber")
        self.assertEqual(result["passages"], [])
        self.assertTrue(result["insufficient"])

    def test_missing_query_term_is_disclosed(self):
        result = self.selected("amber quasar")
        self.assertTrue(result["passages"])
        self.assertIn("quasar", result["missing_terms"])
        self.assertEqual(audit_brief(self.root, result, build_brief(result))["verdict"], "UNKNOWN")

    def test_blank_query_is_bounded_labeled_overview(self):
        result = self.selected("   ", max_passages=1)
        self.assertLessEqual(len(result["passages"]), 1)
        self.assertTrue(result["passages"])
        self.assertIn("overview", str(result["selection_method"]).lower())
        self.assertIn("omitted_sources", result)
        self.assertIn("omitted_passages", result)

    def test_bounded_selection_discloses_omissions(self):
        result = self.selected(max_passages=1)
        self.assertEqual(len(result["passages"]), 1)
        self.assertTrue(result["omitted_sources"] or result["omitted_passages"])

    def test_invalid_bounds_and_too_many_sources_are_rejected(self):
        for limit in (0, -1, True, 1.5):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.selected(max_passages=limit)
        for index in range(101):
            self.write(f"many/{index}.md", "amber\n")
        with self.assertRaises(ValueError):
            fold(self.root, [f"many/{index}.md" for index in range(101)], "amber")


class ExpansionAndAuditTests(SourceFixture):
    def test_expansion_preserves_exact_line_partition(self):
        self.write("crlf.md", "Amber first.\r\nSecond line.\r\n\r\nTail.\r\n")
        selected = fold(self.root, ["crlf.md"], "amber")
        passage = selected["passages"][0]
        result = expand(self.root, passage)
        self.assertTrue(result["fresh"])
        self.assertEqual(result["text"], passage["text"])
        self.assertIn("\r\n", result["text"])
        self.assert_json(result)

    def test_source_change_is_stale_even_outside_selected_slice(self):
        result = self.selected()
        passage = next(p for p in result["passages"] if p.get("source", p.get("path")) == "a.md")
        with (self.root / "a.md").open("ab") as stream:
            stream.write(b"New trailing evidence.\n")
        self.assertFalse(expand(self.root, passage)["fresh"])
        self.assertEqual(audit_brief(self.root, result, build_brief(result))["verdict"], "FAIL")

    def test_literal_brief_passes_and_contains_actual_work(self):
        result = self.selected()
        brief = build_brief(result)
        self.assertIn("amber", brief.lower())
        self.assertIn("Amber evidence is bounded.", brief)
        self.assertIn("a.md", brief)
        audit = audit_brief(self.root, result, brief)
        self.assertEqual(audit["verdict"], "PASS")
        self.assertIsInstance(audit["findings"], list)
        self.assert_json(audit)

    def test_corrupted_output_is_rejected(self):
        result = self.selected()
        brief = build_brief(result)
        corrupted = brief.replace("Amber evidence is bounded.", "Invented evidence proves everything.")
        self.assertNotEqual(brief, corrupted)
        self.assertEqual(audit_brief(self.root, result, corrupted)["verdict"], "FAIL")

    def test_omitted_selected_evidence_is_rejected(self):
        result = self.selected()
        brief = build_brief(result)
        corrupted = brief.replace("Amber has a second independent source.", "")
        self.assertNotEqual(brief, corrupted)
        self.assertEqual(audit_brief(self.root, result, corrupted)["verdict"], "FAIL")

    def test_forged_folded_passage_cannot_validate_itself(self):
        result = self.selected()
        forged = copy.deepcopy(result)
        forged["passages"][0]["text"] = "Fabricated result.\n"
        forged["passages"][0]["slice_sha256"] = hashlib.sha256(b"Fabricated result.\n").hexdigest()
        self.assertEqual(audit_brief(self.root, forged, build_brief(forged))["verdict"], "FAIL")

    def test_empty_brief_is_not_a_success(self):
        result = self.selected()
        self.assertEqual(audit_brief(self.root, result, "")["verdict"], "FAIL")

    def test_pressure_is_bounded_and_never_mutates_sources_or_fold(self):
        result = self.selected()
        original = copy.deepcopy(result)
        before = {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        report = pressure(self.root, result)
        after = {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(original, result)
        self.assertTrue(report["checks"])
        self.assertTrue(any(check["detected"] for check in report["checks"]))
        self.assertLessEqual(len(report["checks"]), report["max_cases"])
        self.assert_json(report)


class MeasurementTests(unittest.TestCase):
    def criterion(self, op="eq", expected=3, unit="items"):
        return {"name": "size", "metric": "count", "op": op, "value": expected, "unit": unit}

    def observation(self, value=3, unit="items"):
        return {"count": {"value": value, "unit": unit}}

    def test_eq_le_ge_compare_actual_outcomes(self):
        for op, actual, expected, verdict in (
            ("eq", 3, 3, "PASS"), ("eq", 4, 3, "FAIL"),
            ("le", 2, 3, "PASS"), ("le", 4, 3, "FAIL"),
            ("ge", 4, 3, "PASS"), ("ge", 2, 3, "FAIL"),
        ):
            with self.subTest(op=op, actual=actual):
                result = measure(self.observation(actual), [self.criterion(op, expected)])
                self.assertEqual(result["verdict"], verdict)
                self.assertEqual(result["rows"][0]["actual"], actual)
                self.assertEqual(result["rows"][0]["expected"], expected)

    def test_missing_evidence_and_unit_mismatch_are_unknown(self):
        for observed in ({}, self.observation(unit="seconds")):
            with self.subTest(observed=observed):
                self.assertEqual(measure(observed, [self.criterion()])["verdict"], "UNKNOWN")

    def test_nan_and_infinity_rejected_on_both_sides(self):
        for bad in (math.nan, math.inf, -math.inf):
            with self.subTest(side="observation", value=bad), self.assertRaises(ValueError):
                measure(self.observation(bad), [self.criterion()])
            with self.subTest(side="criterion", value=bad), self.assertRaises(ValueError):
                measure(self.observation(), [self.criterion(expected=bad)])

    def test_boolean_is_not_numeric_one(self):
        result = measure(self.observation(True), [self.criterion(expected=1)])
        self.assertEqual(result["verdict"], "FAIL")

    def test_boolean_ordering_is_not_numeric_ordering(self):
        for op in ("le", "ge"):
            with self.subTest(op=op), self.assertRaises(ValueError):
                measure(self.observation(True), [self.criterion(op=op, expected=1)])

    def test_no_criteria_cannot_establish_success(self):
        self.assertEqual(measure(self.observation(), [])["verdict"], "UNKNOWN")

    def test_invalid_operator_is_rejected(self):
        with self.assertRaises(ValueError):
            measure(self.observation(), [self.criterion(op="approximately")])


class ResidueTests(unittest.TestCase):
    def test_preserves_prediction_observation_and_miss(self):
        prediction = {"success": True, "cost": {"value": 2, "unit": "calls"}, "expected_only": 9}
        observation = {"success": False, "cost": {"value": 3, "unit": "calls"}, "actual_only": "seen"}
        old_prediction, old_observation = copy.deepcopy(prediction), copy.deepcopy(observation)
        result = residue(prediction, observation)
        self.assertEqual(prediction, old_prediction)
        self.assertEqual(observation, old_observation)
        self.assertEqual(result["prediction"], old_prediction)
        self.assertEqual(result["observation"], old_observation)
        self.assertTrue(result["differences"])
        self.assertTrue(result["missing_values"])
        self.assertIn("outcome", result)
        json.dumps(result, allow_nan=False)

    def test_equal_values_have_no_residual_differences(self):
        value = {"accuracy": 1.0, "note": "bounded result"}
        result = residue(value, copy.deepcopy(value))
        self.assertFalse(result["differences"])
        self.assertFalse(result["missing_values"])


if __name__ == "__main__":
    unittest.main()
