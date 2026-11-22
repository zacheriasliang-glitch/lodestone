"""Synthetic receipt-boundary tests only; no local model is called here."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[2] / "desktop/smoke.py"
SPEC = importlib.util.spec_from_file_location("desktop_smoke_test", SOURCE)
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


class SmokeEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.run = self.root / "Loom/runs/fixture"
        self.run.mkdir(parents=True)
        self.config = {"ollama_model_digest": "fixture-model"}
        self.result = {"artifacts_dir": str(self.run), "status": "COMPLETED", "provider_called": True,
                       "response": "Fixture response", "outcome_handle": "fixture-outcome",
                       "terminal_handle": "fixture-terminal"}
        self.worker = {"status": "COMPLETED", "response": "Fixture response", "observation": {
            "source": "actual supervised local Loom worker", "endpoint": "http://127.0.0.1:11435",
            "model_digest": "fixture-model", "paid_api_calls": 0, "weight_updates": 0,
            "token_count_matches": True, "done_reason": "stop", "prompt_tokens": 12, "eval_count": 3,
            "rendered_prompt_sha256": "fixture-prompt"}}
        self.write("worker.json", {"pid": 123, "backend": "ollama_qwen3_base"})
        self.write("result.json", self.worker)
        self.write("supervisor.json", {"observation": {"supervisor": {"stop": {"confirmed_stopped": True}}}})

    def write(self, name, value):
        (self.run / name).write_text(json.dumps(value))

    def test_complete_fixture_reports_file_identities(self):
        checked = smoke.check_run(self.result, self.root, self.config)
        self.assertEqual(checked["files"]["result.json"], smoke.sha256(self.run / "result.json"))
        self.assertEqual(checked["generated_tokens"], 3)

    def test_local_outcome_without_real_worker_receipt_is_rejected(self):
        (self.run / "worker.json").unlink()
        with self.assertRaises(FileNotFoundError):
            smoke.check_run(self.result, self.root, self.config)

    def test_other_endpoint_and_changed_model_are_rejected(self):
        for field, value in (("endpoint", "https://example.invalid"), ("model_digest", "different-model")):
            with self.subTest(field=field):
                original = self.worker["observation"][field]
                self.worker["observation"][field] = value
                self.write("result.json", self.worker)
                with self.assertRaises(ValueError):
                    smoke.check_run(self.result, self.root, self.config)
                self.worker["observation"][field] = original

    def test_incomplete_generation_cannot_pass_with_a_partial_response(self):
        self.worker["observation"]["done_reason"] = "length"
        self.write("result.json", self.worker)
        with self.assertRaises(ValueError):
            smoke.check_run(self.result, self.root, self.config)

    def test_native_and_worker_response_disagreement_is_rejected(self):
        self.result["response"] = "Replaced fixture response"
        with self.assertRaises(ValueError):
            smoke.check_run(self.result, self.root, self.config)


if __name__ == "__main__":
    unittest.main()
