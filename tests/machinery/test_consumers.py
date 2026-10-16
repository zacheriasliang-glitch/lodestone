"""Exercise consumers with changed evidence, checker faults, and durable revisions."""
import importlib
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lodestone_machinery import Engine
from lodestone_machinery.__main__ import main
from lodestone_machinery.inspection import inspect_job, export_nodes, pair_packet
from lodestone_machinery.nodes import NodeStore


class ConsumerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "sources"
        self.root.mkdir()
        self.source = self.root / "note.md"
        self.source.write_text("Ceramic evidence records failure.\n", encoding="utf-8")
        self.workspace = self.base / "work"
        self.engine = Engine(self.workspace)

    def job(self, prediction=None, query="ceramic"):
        identifier = self.engine.submit(self.root, ["note.md"], query, prediction)
        self.engine.run(identifier)
        return identifier

    def test_inspector_reopens_changed_passage_without_rewriting_history(self):
        job = self.job()
        original = self.engine.status(job)
        first = inspect_job(self.engine, job)
        self.assertTrue(first["passages"][0]["fresh"])
        self.assertEqual(first["pressure"]["baseline_verdict"], "PASS")
        self.source.write_text("Ceramic evidence now records a contrary result.\n")
        changed = inspect_job(self.engine, job)
        self.assertFalse(changed["passages"][0]["fresh"])
        self.assertIn("contrary result", changed["passages"][0]["text"])
        self.assertIn("PASSAGE_DRIFT", [f["kind"] for f in changed["findings"]])
        self.assertIn("BASELINE_NOT_CONFIRMED", [f["kind"] for f in changed["findings"]])
        self.assertEqual(changed["verdict"], "ATTENTION")
        self.assertEqual(self.engine.status(job), original)

    def test_pressure_consumer_reports_a_checker_that_accepts_injected_faults(self):
        job = self.job()
        good = inspect_job(self.engine, job)
        self.assertEqual(good["pressure"]["undetected"], [])
        checker_module = importlib.import_module("lodestone_machinery.organs.pressure")
        # Fault-inject the checker, leaving the real pressure cases and consumer intact.
        with patch.object(checker_module, "audit_brief", return_value={"verdict": "PASS"}):
            weak = inspect_job(self.engine, job)
        missed = [f["case"] for f in weak["findings"] if f["kind"] == "UNDETECTED_FAULT"]
        self.assertEqual(len(missed), 4)
        self.assertIn("changed source identity", missed)
        self.assertEqual(weak["verdict"], "ATTENTION")
        self.assertEqual(inspect_job(self.engine, job)["pressure"]["undetected"], [])

    def test_prediction_changes_diagnosis_while_observed_work_stays_completed(self):
        probe = self.job()
        observation = self.engine.status(probe)["result"]["residue"]["observation"]
        matched = self.job(prediction=observation)
        wrong = dict(observation, audit_verdict="FAIL")
        mismatched = self.job(prediction=wrong)
        match_report = inspect_job(self.engine, matched)
        mismatch_report = inspect_job(self.engine, mismatched)
        self.assertEqual(match_report["verdict"], "OK", match_report)
        self.assertEqual(mismatch_report["prediction"]["outcome"], "MISMATCH")
        difference = next(f for f in mismatch_report["findings"] if f["kind"] == "PREDICTION_MISMATCH")
        self.assertEqual((difference["predicted"], difference["observed"]), ("FAIL", "PASS"))
        self.assertEqual(mismatch_report["recorded_state"], "COMPLETED")
        incomplete = inspect_job(self.engine, probe)
        self.assertIn("PREDICTION_INCOMPLETE", [f["kind"] for f in incomplete["findings"]])

    def test_selection_and_measurement_change_the_actual_worker_result(self):
        found, absent = self.job(), self.job(query="unfindablequartz")
        good, unknown = self.engine.status(found), self.engine.status(absent)
        self.assertEqual(good["state"], "COMPLETED")
        self.assertEqual(unknown["state"], "BLOCKED")
        self.assertIn("records failure", Path(good["result"]["artifact"]).read_text())
        self.assertNotIn("records failure", Path(unknown["result"]["artifact"]).read_text())
        self.assertEqual(unknown["result"]["combined_verdict"], "UNKNOWN")

    def test_cli_export_reads_revisions_and_dependency_invalidation(self):
        store = NodeStore(self.workspace)
        def record(identifier, text):
            return {"id": identifier, "text": text, "layer": "AP", "status": "candidate",
                    "scope": "consumer test", "epoch": "test-1", "sources": ["note.md"]}
        store.put(record("source", "Initial evidence"))
        store.put(record("claim", "Dependent interpretation"))
        store.link("claim", "source", "depends_on")
        def export():
            stream = io.StringIO()
            with redirect_stdout(stream):
                code = main(["export-nodes", "--workspace", str(self.workspace),
                             "--claim", "claim", "--claim", "source"])
            return code, json.loads(stream.getvalue())
        code, before = export()
        self.assertEqual(code, 0)
        self.assertEqual(before["review_ids"], [])
        store.put(record("source", "Revised contrary evidence"))
        store.invalidate_dependents("source", "Evidence changed")
        code, after = export()
        self.assertEqual(code, 2)
        self.assertEqual(after["review_ids"], ["claim"])
        self.assertEqual(after["records"][1]["text"], "Revised contrary evidence")
        self.assertEqual(after["records"][0]["status"], "candidate")
        self.assertEqual(before["relations"], after["relations"])
        partial = export_nodes(store, ["claim", "absent"])
        self.assertEqual(partial["external_ids"], ["source"])
        self.assertEqual(partial["missing_ids"], ["absent"])

    def test_pair_consumer_routes_differences_and_gaps_without_approval(self):
        black = lambda **payload: {"literal": "failure remains"}
        white = lambda **payload: {"literal": "success claimed"}
        same = pair_packet({"question": "compare"}, black, black)
        different = pair_packet({"question": "compare"}, black, white)
        missing = pair_packet({"question": "compare"}, black, None)
        self.assertEqual(same["next_action"], "REVIEW_CONTENT")
        self.assertEqual(different["next_action"], "COMPARE_RESPONSES")
        self.assertEqual(missing["next_action"], "RESTORE_PROVIDER")
        self.assertEqual(different["responses"]["initial"]["white"]["response"]["literal"], "success claimed")
        self.assertEqual(missing["responses"]["initial"]["black"]["response"]["literal"], "failure remains")
        for result in (same, different, missing):
            self.assertEqual(result["authority"], "NONE")
            self.assertEqual(result["semantic_verdict"], "UNRESOLVED")


if __name__ == "__main__":
    unittest.main()
