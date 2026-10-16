"""Subprocess acceptance tests for real-source ask orchestration.

The provider program is an explicitly scripted fixture. Its literal response
tests the software exchange, not semantic competence or research performance.
"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


RUNNER = Path(__file__).resolve().parents[2] / "tools/machinery/run"
PROVIDER = '''import json, sys
request = json.load(sys.stdin)
reply = {"request_id": request["request_id"], "decision": "NEED_EVIDENCE",
         "claims": [], "relations": [], "alternatives": [], "answer": None,
         "answer_claim_ids": [], "next_check": None,
         "coverage_assumptions": ["Scripted fixture over the explicitly supplied source only."],
         "unresolved": []}
if not request["evidence"]:
    source = request["scope"]["sources"][0]
    reply["next_check"] = {"source": source["path"], "start_line": 1, "end_line": source["lines"],
                           "purpose": "Read the actual source before producing the fixture response",
                           "outcomes": ["A recorded line can be quoted", "An unavailable line cannot support an answer"]}
else:
    passage = request["evidence"][-1]
    reply["decision"] = "ANSWER"
    reply["answer"] = passage["text"].strip()
    reply["answer_claim_ids"] = ["literal-line"]
    reply["claims"] = [{"id": "literal-line", "subject": "fixture.recorded_line",
                        "value": passage["text"].strip(), "scope": "supplied fixture source",
                        "epoch": request["epoch"], "basis": "RECORDED", "support": [passage["id"]]}]
print(json.dumps({"provider": "scripted ask test fixture", "model": "NONE",
                  "response_id": None, "usage": None, "response": reply}))
'''


class OrigamiAskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.sources = self.base / "sources"
        self.sources.mkdir()
        self.source = self.sources / "decision.md"
        self.source.write_text("Current label: blue.\n", encoding="utf-8")
        self.workspace = self.base / "workspace"
        self.provider = self.base / "scripted_provider.py"
        self.provider.write_text(PROVIDER, encoding="utf-8")

    def command(self, *extra):
        return [sys.executable, "-B", str(RUNNER), "origami", "ask",
                "--workspace", str(self.workspace), "--root", str(self.sources),
                "--source", "decision.md", "--question", "Which label is recorded?",
                "--epoch", "fixture-current", "--authority-ref", "Test source reads only",
                "--constructor", "fixture-constructor", "--consumer", "fixture-consumer",
                "--checker", "fixture-checker", "--provider", "scripted ask test fixture", *extra]

    def invoke(self, *extra):
        return subprocess.run(self.command(*extra), capture_output=True, text=True)

    def configured(self, *extra):
        return self.invoke("--provider-command", json.dumps([sys.executable, "-B", str(self.provider)]), *extra)

    def runs(self):
        path = self.workspace / "origami"
        return sorted(path.iterdir()) if path.is_dir() else []

    def test_offline_ask_returns_the_exact_saved_request_without_answer_or_call(self):
        before = self.source.read_bytes()
        process = self.invoke("--max-calls", "2", "--max-read-bytes", "1024", "--initial-passages", "1")
        self.assertEqual(process.returncode, 0, process.stderr)
        value = json.loads(process.stdout)
        self.assertEqual(value["status"], "NEEDS_PROVIDER")
        saved = json.loads(Path(value["request_path"]).read_text())
        self.assertEqual(value["request"], saved)
        self.assertEqual(value["request_id"], saved["request_id"])
        self.assertIn("provider_envelope", saved)
        self.assertIn("response_schema", saved)
        self.assertEqual(saved["evidence"][0]["text"], "Current label: blue.\n")
        directory = self.runs()[0]
        self.assertFalse((directory / "result.json").exists())
        self.assertEqual(list(directory.glob("call-*.json")), [])
        self.assertEqual(self.source.read_bytes(), before)

    def test_configured_ask_executes_a_read_and_persists_the_provider_answer(self):
        process = self.configured("--initial-passages", "0", "--max-calls", "2")
        self.assertEqual(process.returncode, 0, process.stderr)
        value = json.loads(process.stdout)
        self.assertEqual(value["status"], "ANSWER")
        self.assertEqual(value["answer"], "Current label: blue.")
        self.assertEqual(value["transport"], "PROGRAM")
        self.assertEqual(len(value["calls"]), 2)
        self.assertEqual([x["kind"] for x in value["observations"]].count("read_passage"), 1)
        self.assertIn("Current label: blue.", Path(value["artifact"]).read_text())
        self.assertEqual(json.loads((self.runs()[0] / "result.json").read_text()), value)
        self.assertIsNone(value["usage"]["counts"]["total_tokens"])

    def test_reentry_reassesses_changed_sources_and_preserves_the_old_encounter(self):
        first = self.configured()
        self.assertEqual(first.returncode, 0, first.stderr)
        original = json.loads(first.stdout)
        original_dir = self.workspace / "origami" / original["run_id"]
        old_bytes = {p.name: p.read_bytes() for p in original_dir.iterdir() if p.is_file()}
        self.source.write_text("Current label: green.\n", encoding="utf-8")
        second = self.configured("--previous", original["run_id"], "--epoch", "fixture-revised",
                                 "--initial-passages", "0")
        self.assertEqual(second.returncode, 0, second.stderr)
        revised = json.loads(second.stdout)
        self.assertEqual(revised["answer"], "Current label: green.")
        self.assertEqual(revised["prior_knowledge_status"], "NEEDS_REVIEW")
        self.assertEqual(revised["previous"], original["run_id"])
        current_dir = self.workspace / "origami" / revised["run_id"]
        initial = json.loads((current_dir / "request-000.json").read_text())
        self.assertEqual(initial["prior_knowledge"]["changed_sources"], ["decision.md"])
        self.assertTrue(initial["prior_knowledge"]["epoch_changed"])
        inspected = subprocess.run([sys.executable, "-B", str(RUNNER), "origami", "inspect",
                                    "--workspace", str(self.workspace), original["run_id"]],
                                   capture_output=True, text=True)
        self.assertEqual(inspected.returncode, 2, inspected.stderr)
        self.assertEqual(json.loads(inspected.stdout)["status"], "NEEDS_REVIEW")
        self.assertEqual({p.name: p.read_bytes() for p in original_dir.iterdir() if p.is_file()}, old_bytes)

    def test_budget_stop_is_not_reported_as_answer_or_success(self):
        process = self.configured("--initial-passages", "0", "--max-calls", "1")
        self.assertEqual(process.returncode, 2, process.stderr)
        value = json.loads(process.stdout)
        self.assertEqual(value["status"], "BUDGET_EXHAUSTED")
        self.assertIsNone(value["answer"])
        self.assertEqual(len(value["calls"]), 1)
        self.assertNotIn("read_passage", [x["kind"] for x in value["observations"]])

    def test_invalid_input_fails_before_a_run_is_created(self):
        cases = [("--provider-command", "not json"), ("--provider-command", "[]"),
                 ("--provider-command", json.dumps([sys.executable]), "--timeout", "0"),
                 ("--allowed-endpoint", "example.invalid:443"),
                 ("--provider-auth-path", str(self.base)),
                 ("--provider-command", json.dumps([sys.executable]), "--allowed-endpoint", "*:443"),
                 ("--provider-command", json.dumps([sys.executable]), "--provider-auth-path", str(self.base / "missing")),
                 ("--source", "../outside.md"), ("--max-calls", "0"),
                 ("--provider", "fixture-checker")]
        for args in cases:
            with self.subTest(args=args):
                process = self.invoke(*args)
                self.assertEqual(process.returncode, 2)
                self.assertIn("error", json.loads(process.stderr))
                self.assertEqual(self.runs(), [])


if __name__ == "__main__":
    unittest.main()
