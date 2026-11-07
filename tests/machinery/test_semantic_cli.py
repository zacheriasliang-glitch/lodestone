"""Real D1 records through the CLI; replies/reviews are explicitly scripted fixtures."""
from contextlib import redirect_stdout, redirect_stderr
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT / "tools/local-memory/src"))
import lodestone_memory as memory
import source_addresses
from lodestone_machinery.origami_cli import main


class SemanticCLITests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.scope = self.base / "capture"
        self.scope.mkdir()
        (self.scope / "decision.txt").write_text("Heading\nThe gate closes at noon.\n", encoding="utf-8")
        self.notebook = self.base / "notebook"
        record = memory.make_record("Gate timing", "Locate the gate schedule.", "fixture author", "note", [], 1.0)
        identity, _ = memory.add_record(self.notebook, record)
        self.bindings = self.base / "bindings.json"
        source_addresses.add_binding(self.notebook, self.bindings, self.scope, identity,
                                     "decision.txt", 2, 2, "fixture capture", "fixture author")
        self.report = self.base / "addresses.json"
        self.report.write_text(json.dumps(source_addresses.resolve(
            self.notebook, self.bindings, self.scope, "gate timing")), encoding="utf-8")
        self.workspace = self.base / "workspace"

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            status = main([args[0], "--workspace", str(self.workspace), *args[1:]])
        return status, json.loads(out.getvalue() or err.getvalue())

    def start(self, *extra):
        return self.cli("ask", "--source-address-report", str(self.report),
                        "--question", "When does the gate close?", "--epoch", "fixture",
                        "--authority-ref", "local test only", "--constructor", "fixture-author",
                        "--provider", "fixture-author", "--consumer", "fixture-reader",
                        "--checker", "fixture-checker", *extra)

    def finish(self):
        status, started = self.start()
        self.assertEqual(status, 0, started)
        request = started["request"]
        passage = request["evidence"][0]
        response = {"provider": "fixture-author", "model": "SCRIPTED-NO-MODEL", "response_id": None,
                    "usage": None, "response": {
            "request_id": request["request_id"], "decision": "ANSWER",
            "claims": [{"id": "gate-time", "subject": "gate.closing", "value": "noon",
                        "scope": "provided schedule", "epoch": "fixture", "basis": "RECORDED",
                        "support": [passage["id"]]}],
            "relations": [], "alternatives": [], "answer": "The gate closes at noon.",
            "answer_claim_ids": ["gate-time"], "next_check": None,
            "coverage_assumptions": ["Only the captured schedule."], "unresolved": []}}
        path = self.base / "response.json"
        path.write_text(json.dumps(response), encoding="utf-8")
        code, result = self.cli("advance", started["run_id"], "--response", str(path))
        self.assertEqual(code, 0, result)
        return started["run_id"]

    def test_actual_source_binding_seeds_the_answer_request(self):
        code, result = self.start("--initial-passages", "0")
        self.assertEqual(code, 0, result)
        self.assertEqual([p["text"] for p in result["request"]["evidence"]], ["The gate closes at noon.\n"])
        self.assertEqual(result["request"]["evidence"][0]["start_line"], 2)

    def test_mixed_selection_modes_refuse_before_encounter(self):
        code, result = self.start("--root", str(self.scope), "--source", "decision.txt")
        self.assertEqual(code, 2)
        self.assertIn("OR", result["message"])
        self.assertFalse(any((self.workspace / "origami").glob("*/intent.json")))

    def test_changed_source_refuses_saved_resolver_report(self):
        (self.scope / "decision.txt").write_text("Heading\nThe gate closes at dusk.\n", encoding="utf-8")
        code, _ = self.start()
        self.assertEqual(code, 2)
        self.assertFalse(any((self.workspace / "origami").glob("*/intent.json")))

    def test_unreviewed_answer_does_not_exit_as_accepted(self):
        run_id = self.finish()
        code, result = self.cli("answer", run_id)
        self.assertEqual(code, 2)
        self.assertEqual(result["status"], "NOT_INDEPENDENTLY_REVIEWED")

    def test_public_launcher_injects_workspace_for_all_review_operations(self):
        spec = importlib.util.spec_from_file_location("semantic_test_ecosystem", PROJECT / "tools/ecosystem/cli.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for operation in ("review-request", "review", "answer"):
            args = module.machinery_args(["origami", operation, "run-id"], self.workspace, PROJECT)
            self.assertEqual(args[:4], ["origami", operation, "--workspace", str(self.workspace)])

    def test_review_roundtrip_and_stale_source_refusal(self):
        run_id = self.finish()
        code, request = self.cli("review-request", run_id)
        self.assertEqual(code, 0, request)
        review = {"schema": "origami-support-review/v1",
                  "review_request_sha256": request["request_sha256"], "reviewer": "fixture-checker",
                  "claim_verdicts": [{"id": "gate-time", "verdict": "SUPPORTED",
                                      "reason": "Scripted fixture: the selected line explicitly says noon."}],
                  "answer_verdict": "PASS", "omissions": [], "contradictions": [],
                  "applicability": "SAME_QUESTION", "reason": "Scripted fixture, not an independent semantic evaluation."}
        path = self.base / "review.json"
        path.write_text(json.dumps(review), encoding="utf-8")
        code, receipt = self.cli("review", run_id, "--response", str(path))
        self.assertEqual(code, 0, receipt)
        code, answer = self.cli("answer", run_id, "--review", receipt["handle"])
        self.assertEqual(code, 0, answer)
        self.assertEqual(answer["status"], "REVIEWED_PASS")
        self.assertIn("gate-time [SUPPORTED]", answer["markdown"])
        (self.scope / "decision.txt").write_text("Heading\nThe gate closes at dusk.\n", encoding="utf-8")
        code, _ = self.cli("answer", run_id, "--review", receipt["handle"])
        self.assertEqual(code, 2)

    def test_duplicate_json_report_keys_refuse(self):
        self.report.write_text('{"schema":"first","schema":"second"}', encoding="utf-8")
        code, result = self.start()
        self.assertEqual(code, 2)
        self.assertIn("Duplicate", result["message"])


if __name__ == "__main__":
    unittest.main()
