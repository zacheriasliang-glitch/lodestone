"""Behavior checks for durable claims and provider-context separation."""
import copy
import math
from pathlib import Path
import tempfile
import unittest

from lodestone_machinery.nodes import NodeStore
from lodestone_machinery.pressure_pair import run_pair


def claim(identifier, text="A bounded claim", **overrides):
    return {"id": identifier, "text": text, "layer": "AP", "status": "candidate",
            "scope": "example task", "epoch": "2026-09-11", "sources": ["source.md"],
            **overrides}


class NodesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = NodeStore(self.tmp.name)

    def test_persistent_retrieval_and_immutable_history(self):
        original = claim("x", "First interpretation")
        self.assertEqual(self.store.put(original), "x")
        original["text"] = "Caller mutation"
        self.store.put(claim("x", "Revised interpretation", status="unresolved"))
        reopened = NodeStore(self.tmp.name)
        self.assertEqual(reopened.get("x")["text"], "Revised interpretation")
        history = reopened.history("x")
        self.assertEqual([entry["revision"] for entry in history], [1, 2])
        self.assertEqual(history[0]["record"]["text"], "First interpretation")
        history[0]["record"]["text"] = "Client rewrites output"
        self.assertEqual(reopened.history("x")[0]["record"]["text"], "First interpretation")
        self.assertTrue((Path(self.tmp.name) / "nodes.sqlite3").is_file())
        self.assertIsNone(reopened.get("missing"))

    def test_contradictions_survive_and_are_not_merged(self):
        self.store.put(claim("a", "The treatment helped"))
        self.store.put(claim("b", "The treatment did not help", status="unresolved"))
        edge = self.store.link("a", "b", "contradicts")
        self.assertEqual(self.store.neighbors("a"), [edge])
        self.assertEqual(self.store.neighbors("b"), [edge])
        self.assertNotEqual(self.store.get("a")["text"], self.store.get("b")["text"])
        self.assertEqual(self.store.invalidate_dependents("a", "Changed evidence"), [])
        self.assertEqual(self.store.get("b")["freshness"], "CURRENT")

    def test_transitive_invalidation_is_cycle_safe_and_preserves_status(self):
        for identifier in ("a", "b", "c", "d"):
            self.store.put(claim(identifier, status="stable_canon", acceptance_ref="Host disposition 1"))
        self.store.link("b", "a", "depends_on")
        self.store.link("c", "b", "depends_on")
        self.store.link("a", "c", "depends_on")
        self.store.link("d", "a", "supports")
        affected = self.store.invalidate_dependents("a", "Source revised")
        self.assertEqual(affected, ["b", "c"])
        for identifier in affected:
            record = self.store.get(identifier)
            self.assertEqual(record["freshness"], "NEEDS_REVIEW")
            self.assertEqual(record["status"], "stable_canon")
            self.assertIn("Source revised", record["review_reasons"][0])
            self.assertEqual(self.store.history(identifier)[0]["record"]["freshness"], "CURRENT")
        self.assertEqual(self.store.get("a")["freshness"], "CURRENT")
        self.assertEqual(self.store.get("d")["freshness"], "CURRENT")

    def test_stable_requires_explicit_reference_and_no_automatic_upgrade(self):
        self.store.put(claim("x"))
        with self.assertRaises(ValueError):
            self.store.put(claim("x", status="stable_canon"))
        self.assertEqual(self.store.get("x")["status"], "candidate")
        self.assertEqual(len(self.store.history("x")), 1)
        self.store.put(claim("x", status="stable_canon", acceptance_ref="Recorded acceptance"))
        self.assertEqual(self.store.get("x")["acceptance_ref"], "Recorded acceptance")

    def test_invalid_claims_and_relations_do_not_mutate_store(self):
        bad_records = [claim("x", layer="FACT"), claim("x", status="proven"),
                       claim("x", scope=""), claim("x", sources="source.md"),
                       claim("x", metadata={"x": math.nan}), claim("x", metadata=(1, 2)),
                       claim("x", metadata={1: "coerced key"}), claim("x", extra="ignored")]
        for record in bad_records:
            with self.subTest(record=record), self.assertRaises(ValueError):
                self.store.put(record)
        self.assertIsNone(self.store.get("x"))
        self.store.put(claim("a"))
        with self.assertRaises(ValueError):
            self.store.link("a", "missing", "depends_on")
        with self.assertRaises(ValueError):
            self.store.link("a", "a", "proves")
        self.assertEqual(self.store.neighbors("a"), [])


class PairTests(unittest.TestCase):
    def test_independent_initial_and_actual_cross_read_preserve_disagreement(self):
        board = {"question": "Which claim survives?", "facts": ["observation 1"]}
        original = copy.deepcopy(board)
        seen = []

        def provider(**payload):
            seen.append(copy.deepcopy(payload))
            role = payload["role"]
            self.assertEqual(payload["contract_version"], "0.1.0")
            payload["board"]["facts"].append(f"{role} mutation")
            if payload["phase"] == "initial":
                self.assertIsNone(payload["own"])
                self.assertIsNone(payload["counterpart"])
                return {"claim": f"{role} position", "disagreements": [f"{role} open tension"]}
            opposite = "white" if role == "black" else "black"
            self.assertEqual(payload["counterpart"]["claim"], f"{opposite} position")
            self.assertEqual(payload["own"]["claim"], f"{role} position")
            payload["counterpart"]["claim"] = "Provider attempts to rewrite contrary"
            return {"position": f"{role} still disagrees", "status": "unresolved"}

        result = run_pair(board, provider, provider)
        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(result["semantic_verdict"], "UNRESOLVED")
        self.assertEqual(board, original)
        self.assertTrue(all(item["board"] == original for item in seen))
        self.assertEqual([item["phase"] for item in seen], ["initial", "initial", "cross_read", "cross_read"])
        self.assertEqual(result["initial"]["black"]["response"]["claim"], "black position")
        self.assertEqual(result["initial"]["white"]["response"]["claim"], "white position")
        self.assertEqual(len(result["response_differences"]), 2)
        self.assertIn("black open tension", result["initial"]["black"]["response"]["disagreements"])

    def test_missing_provider_preserves_other_output_and_no_invented_cross_read(self):
        calls = []

        def black(**payload):
            calls.append(payload)
            return {"candidate": "Recorded black proposal"}

        result = run_pair({"q": "test"}, black, None)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["initial"]["black"]["status"], "RETURNED")
        self.assertEqual(result["initial"]["white"]["error"], "PROVIDER_NOT_CONFIGURED")
        self.assertNotIn("response", result["cross_read"]["black"])

    def test_failing_cross_read_keeps_both_initials_and_other_cross_read(self):
        def black(**payload):
            if payload["phase"] == "cross_read":
                raise RuntimeError("sensitive provider internals")
            return {"value": "black"}

        result = run_pair({}, black, lambda **payload: {"value": "white"})
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["initial"]["black"]["response"], {"value": "black"})
        self.assertEqual(result["cross_read"]["white"]["status"], "RETURNED")
        self.assertEqual(result["cross_read"]["black"]["exception_type"], "RuntimeError")
        self.assertNotIn("sensitive provider internals", str(result))

    def test_non_json_and_oversized_input_or_response_are_bounded(self):
        for board in ({"x": float("inf")}, {"x": (1, 2)}, {1: "bad key"}, {"x": "x" * 1100}):
            with self.subTest(board=board), self.assertRaises(ValueError):
                run_pair(board, None, None, max_payload_bytes=1024)
        for bad in (float("nan"), {"x": "x" * 1100}, {"x": object()}):
            with self.subTest(response_type=type(bad).__name__):
                result = run_pair({}, lambda **payload: bad, lambda **payload: {}, max_payload_bytes=1024)
                self.assertEqual(result["status"], "UNKNOWN")
                self.assertEqual(result["initial"]["black"]["status"], "UNKNOWN")

    def test_finite_json_scalar_differences_are_not_coerced(self):
        result = run_pair({}, lambda **payload: True, lambda **payload: 1)
        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(len(result["response_differences"]), 2)


if __name__ == "__main__":
    unittest.main()
