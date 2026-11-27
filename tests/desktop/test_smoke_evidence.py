"""Synthetic receipt-boundary tests only; no local model is called here."""
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


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
        self.config = {"ollama_model_digest": "fixture-model", "max_rss_bytes": 128 * 1024 * 1024,
                       "ollama_binary": str(self.root / "ollama.exe")}
        self.result = {"artifacts_dir": str(self.run), "status": "COMPLETED", "provider_called": True,
                       "response": "Fixture response", "outcome_handle": "fixture-outcome",
                       "terminal_handle": "fixture-terminal"}
        self.worker = {"status": "COMPLETED", "response": "Fixture response", "observation": {
            "source": "actual supervised local Loom worker", "endpoint": "http://127.0.0.1:11435",
            "model_digest": "fixture-model", "paid_api_calls": 0, "weight_updates": 0,
            "token_count_matches": True, "done_reason": "stop", "prompt_tokens": 12, "eval_count": 3,
            "rendered_prompt_sha256": "fixture-prompt", "server_pid": 456}}
        self.write("worker.json", {"pid": 123, "backend": "ollama_qwen3_base",
                                  "ownership": "windows_job", "filesystem_network_sandbox": False})
        self.write("result.json", self.worker)
        image_digest = hashlib.sha256(os.path.normcase(os.path.normpath(self.config["ollama_binary"])).encode("utf-8")).hexdigest()
        self.supervision = {"stop": {"confirmed_stopped": True}, "rss_accounting": "sampled_entire_owned_windows_job",
                            "rss_valid_sample_count": 3, "observed_group_peak_rss_bytes": 1024,
                            "windows_job_commit_limit_bytes": self.config["max_rss_bytes"],
                            "windows_job_memory_limit_notified": False, "windows_job_member_evidence_truncated": False,
                            "windows_job_sampled_members": [
                                {"pid": 123, "image_name": "python.exe", "image_path_sha256": "fixture-python"},
                                {"pid": 456, "image_name": "ollama.exe", "image_path_sha256": image_digest},
                                {"pid": 789, "image_name": "ollama.exe", "image_path_sha256": image_digest}]}
        self.write("supervisor.json", {"observation": {"supervisor": self.supervision}})

    def write(self, name, value):
        (self.run / name).write_text(json.dumps(value))

    def test_complete_fixture_reports_file_identities(self):
        checked = smoke.check_run(self.result, self.root, self.config)
        self.assertEqual(checked["files"]["result.json"], smoke.sha256(self.run / "result.json"))
        self.assertEqual(checked["generated_tokens"], 3)
        self.assertEqual(checked["windows_job"]["sampled_runner_pids"], [789])

    def test_posix_receipt_does_not_require_windows_job_fields(self):
        self.write("worker.json", {"pid": 123, "backend": "ollama_qwen3_base"})
        self.write("supervisor.json", {"observation": {"supervisor": {"stop": {"confirmed_stopped": True}}}})
        with patch.object(smoke.sys, "platform", "linux"):
            checked = smoke.check_run(self.result, self.root, self.config)
        self.assertNotIn("windows_job", checked)

    def test_windows_cap_sampling_and_actual_server_runner_membership_are_required(self):
        for field, value in (("windows_job_commit_limit_bytes", 1), ("rss_valid_sample_count", 0),
                             ("windows_job_member_evidence_truncated", True),
                             ("windows_job_memory_limit_notified", True),
                             ("windows_job_sampled_members", self.supervision["windows_job_sampled_members"][:2]),
                             ("windows_job_sampled_members", self.supervision["windows_job_sampled_members"][1:]),
                             ("windows_job_sampled_members", self.supervision["windows_job_sampled_members"][::2])):
            with self.subTest(field=field, value=value):
                changed = dict(self.supervision, **{field: value})
                self.write("supervisor.json", {"observation": {"supervisor": changed}})
                with self.assertRaises(ValueError):
                    smoke.check_run(self.result, self.root, self.config)

    def test_windows_cannot_pass_using_a_posix_ownership_receipt(self):
        self.write("worker.json", {"pid": 123, "backend": "ollama_qwen3_base"})
        with patch.object(smoke.sys, "platform", "win32"), self.assertRaisesRegex(ValueError, "ownership"):
            smoke.check_run(self.result, self.root, self.config)

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
