"""Optional decoding constraints with scripted transports; zero model calls."""
from contextlib import nullcontext
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "desktop/runtime/src"))
from lodestone_machinery import decision_format as formats
from lodestone_machinery import loom_adapter, loom_runtime as runtime
from lodestone_machinery import loom_runtime_worker as worker
from lodestone_machinery.records import canonical
from lodestone_nurarihyon import learning


def fixture():
    case = {"case_id": "fixture-δ", "target": "unit-A", "recorded_prior_action": "KEEP",
            "rules": ["An explicitly supplied condition determines the action."],
            "observations": [
                {"id": "old", "order": 1, "target": "unit-A", "verified": True, "text": "Earlier condition."},
                {"id": "unverified", "order": 8, "target": "unit-A", "verified": False, "text": "Unverified condition."},
                {"id": "other", "order": 12, "target": "unit-B", "verified": True, "text": "Other target."},
                {"id": "latest", "order": 3, "target": "unit-A", "verified": True, "text": "Latest condition."}],
            "allowed_actions": ["KEEP", 'RE"DIRECT', "NEW-γ"]}
    text = json.dumps(case, ensure_ascii=False)
    return case, text, "Explicit task 雪:\n" + text + "\nReturn a decision."


class DecisionFormatTests(unittest.TestCase):
    def test_all_choices_and_exact_utf8_source_are_preserved(self):
        case, text, encounter = fixture()
        before = deepcopy(case)
        bound = formats.bind_decision_format(encounter, case_json=text)
        props = bound["response_schema"]["properties"]
        self.assertEqual(props["action"]["enum"], case["allowed_actions"])
        self.assertEqual(props["transition"]["enum"], ["HOLD", "CHANGE"])
        self.assertEqual(props["evidence_ids"]["items"]["enum"], [r["id"] for r in case["observations"]])
        self.assertEqual(props["case_id"]["const"], case["case_id"])
        self.assertEqual((props["evidence_ids"]["minItems"], props["evidence_ids"]["maxItems"]), (1, 1))
        self.assertFalse(bound["response_schema"]["additionalProperties"])
        self.assertEqual(set(bound["response_schema"]["required"]), set(props))
        source = bound["source_contract"]
        span = source["case_span"]
        self.assertEqual(encounter.encode()[span["start_byte"]:span["end_byte"]], text.encode())
        self.assertIsNone(source["policy"])
        self.assertEqual(bound["authority"], "NONE")
        self.assertEqual(bound["semantic_learning"], "NOT_ESTABLISHED")
        self.assertEqual(formats.validate_decision_format(encounter, bound), bound)
        self.assertEqual(case, before)

    def test_narrowed_choices_or_injected_answer_are_rejected_even_with_rehashed_document(self):
        _, text, encounter = fixture()
        original = formats.bind_decision_format(encounter, case_json=text)
        mutations = [
            lambda b: b["response_schema"]["properties"]["action"].update(enum=["KEEP"]),
            lambda b: b["response_schema"]["properties"]["evidence_ids"]["items"].update(enum=["latest"]),
            lambda b: b["response_schema"]["properties"]["transition"].update(enum=["HOLD"]),
            lambda b: b.update(derived_decision={"action": "KEEP"}),
            lambda b: b["source_contract"].update(policy={"rules": [], "observed_conditions": []}),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                bound = deepcopy(original)
                mutate(bound)
                bound["response_schema_sha256"] = formats._sha(formats._canonical(bound["response_schema"]))
                bound.pop("format_sha256")
                bound["format_sha256"] = formats._sha(formats._canonical(bound))
                with self.assertRaises(ValueError):
                    formats.validate_decision_format(encounter, bound)

    def test_bad_duplicate_foreign_ambiguous_or_empty_source_is_rejected(self):
        case, text, encounter = fixture()
        bound = formats.bind_decision_format(encounter, case_json=text)
        with self.assertRaises(ValueError):
            formats.validate_decision_format(encounter + " changed", bound)
        with self.assertRaises(ValueError):
            formats.bind_decision_format(text + text, case_json=text)
        with self.assertRaises(ValueError):
            formats.bind_decision_format(encounter, case_json=json.dumps(case, indent=2))
        duplicate = text.replace('"case_id":', '"case_id":"duplicate", "case_id":', 1)
        with self.assertRaises(ValueError):
            formats.bind_decision_format(duplicate, case_json=duplicate)
        for changed in ({**case, "observations": []}, {**case, "allowed_actions": ["KEEP", "KEEP"]}):
            raw = json.dumps(changed)
            with self.assertRaises(ValueError):
                formats.bind_decision_format(raw, case_json=raw)

    def test_source_snapshot_is_detached_and_semantic_reordering_does_not_filter(self):
        case, _, _ = fixture()
        case["target"] = "never-observed"
        case["observations"].reverse()
        for row in case["observations"]:
            row["verified"] = False
        raw = json.dumps(case)
        bound = formats.bind_decision_format(raw, case_json=raw)
        checked = formats.validate_decision_format(raw, bound)
        self.assertEqual(checked["response_schema"]["properties"]["evidence_ids"]["items"]["enum"],
                         [row["id"] for row in case["observations"]])
        bound["response_schema"]["properties"]["action"]["enum"].clear()
        self.assertEqual(checked["response_schema"]["properties"]["action"]["enum"], case["allowed_actions"])


class WorkerTransportTests(unittest.TestCase):
    def run_scripted(self, *, bound=True, done_reason="stop"):
        _, text, encounter = fixture()
        packet = {"encounter": encounter, "purpose": "Choose.", "context": "Identical native history."}
        original = deepcopy(packet)
        config = {"assets_dir": "/unused", "generation": {"max_tokens": 32}, "max_new_tokens": 32,
                  "max_total_tokens": 1024, "wall_seconds": 3, "ollama_model": "qwen3:0.6b",
                  "ollama_model_digest": "scripted-digest"}
        if bound:
            config["decision_format"] = formats.bind_decision_format(encounter, case_json=text)
        # Deliberately wrong but well-formed raw return: this transport must not
        # replace it with the source-derived correct answer or mark semantics PASS.
        raw = ' \n{"case_id":"fixture-δ","action":"KEEP","transition":"CHANGE","evidence_ids":["other"]}\n'
        row = {"response": raw, "done": True, "done_reason": done_reason,
               "prompt_eval_count": 10, "eval_count": 20}
        opener = Mock()
        opener.open.return_value = io.BytesIO(json.dumps(row).encode() + b"\n")
        observation = {}
        with tempfile.TemporaryDirectory() as directory:
            partial = Path(directory) / "partial.json"
            with patch.object(worker, "verified_tokenizer", return_value=(object(), {"fixture": "pinned"})), \
                    patch.object(loom_adapter, "encode_prediction", return_value={"prompt_tokens": 10}) as encode, \
                    patch.object(loom_adapter, "render_prompt", return_value="IDENTICAL RENDERED PROMPT") as render, \
                    patch.object(worker, "start_ollama", return_value=(Mock(pid=12345), opener, {"version": "scripted"})), \
                    patch.object(worker, "stop_server"), \
                    patch.object(worker, "http_json", return_value={"models": [{"name": "qwen3:0.6b", "digest": "scripted-digest"}]}):
                result = worker.run_ollama(config, packet, partial, observation)
            request = opener.open.call_args.args[0]
            body = json.loads(request.data)
            artifact = partial.with_suffix(".provider-request.json")
            if bound:
                self.assertEqual(artifact.read_bytes(), request.data)
            else:
                self.assertFalse(artifact.exists())
            self.assertEqual(observation["provider_input_sha256"], hashlib.sha256(request.data).hexdigest())
            self.assertEqual(packet, original)
            self.assertEqual(encode.call_args.args[0], original)
            self.assertEqual(render.call_args.args[0], original)
            self.assertEqual(result["response"], raw)
            self.assertNotIn("assessment", result)
            return result, body, config

    def test_schema_is_only_provider_parameter_raw_response_and_prompt_unchanged(self):
        result, body, config = self.run_scripted()
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(body["format"], config["decision_format"]["response_schema"])
        self.assertEqual(body["prompt"], "IDENTICAL RENDERED PROMPT")
        self.assertTrue(body["raw"])
        self.assertEqual(result["observation"]["decision_format"]["format_sha256"], config["decision_format"]["format_sha256"])

    def test_default_provider_body_is_unchanged(self):
        result, body, _ = self.run_scripted(bound=False)
        self.assertEqual(body, {"model": "qwen3:0.6b", "prompt": "IDENTICAL RENDERED PROMPT",
            "raw": True, "stream": True, "keep_alive": 0,
            "options": {"num_ctx": 256, "num_predict": 32, "num_thread": 2,
                        "num_gpu": 0, "temperature": 0, "seed": 0}})
        self.assertNotIn("decision_format", result["observation"])

    def test_output_limit_remains_failed_and_raw_output_preserved(self):
        result, _, _ = self.run_scripted(done_reason="length")
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["observation"]["reason"], "OUTPUT_LIMIT_OR_INCOMPLETE_GENERATION")

    def test_worker_rejects_source_drift_before_loading_or_starting(self):
        _, text, encounter = fixture()
        bound = formats.bind_decision_format(encounter, case_json=text)
        with patch.object(worker, "verified_tokenizer") as tokenize, patch.object(worker, "start_ollama") as start:
            with self.assertRaises(ValueError):
                worker.run_ollama({"decision_format": bound}, {"encounter": encounter + " drift"}, "/unused", {})
        tokenize.assert_not_called()
        start.assert_not_called()
        with self.assertRaisesRegex(ValueError, "Ollama"):
            worker.run_native({"decision_format": bound}, {}, "/unused", {})


class BridgeBindingTests(unittest.TestCase):
    def configured(self, directory, backend="ollama_qwen3_base"):
        directory = Path(directory).resolve()
        active_path = Path(directory) / "active.json"
        active_path.write_text(json.dumps({"schema": "lodestone-learning-active/v1",
            "required": True, "memory_workspace": str(Path(directory) / "memory"),
            "seed_handles": ["bezoar:" + "1" * 64]}))
        return {"backend": backend, "runs_dir": str(directory), "learning_config": str(active_path),
                "max_new_tokens": 32, "max_total_tokens": 1024, "wall_seconds": 3,
                "max_rss_bytes": 10000000, "ollama_model_digest": "scripted"}

    def bridge(self, config):
        bridge = object.__new__(runtime.LoomBridge)
        bridge.config_path, bridge._config = "/unused", config
        bridge._lock, bridge._active, bridge._unsafe_groups = threading.Lock(), set(), set()
        bridge._worker = Mock(return_value={"status": "COMPLETED", "response": "actual scripted response"})
        return bridge

    def test_optional_mode_is_bound_before_admission_and_reaches_worker_without_prompt_changes(self):
        _, text, encounter = fixture()
        with tempfile.TemporaryDirectory() as directory:
            config = self.configured(Path(directory))
            bridge = self.bridge(config)
            native = Mock()
            native.memory.preparation_lessons.return_value = []
            native.run.side_effect = lambda **kw: kw["provider"]({"encounter": kw["encounter"],
                "purpose": kw["purpose"], "context": kw["context"]})
            with patch.object(runtime, "_profiles", return_value={runtime.ROUTE: config}), \
                    patch.object(runtime, "runtime_identity", return_value={"model": "scripted"}), \
                    patch.object(runtime, "_native", return_value=native), \
                    patch.object(learning, "locked", return_value=nullcontext()):
                result = bridge.run(encounter=encounter, purpose="Choose", context="Same history",
                                    decision_case_json=text)
            worker_config, path, packet, _ = bridge._worker.call_args.args
            self.assertEqual(packet, {"encounter": encounter, "purpose": "Choose", "context": "Same history"})
            self.assertEqual(worker_config["decision_format"], formats.bind_decision_format(encounter, case_json=text))
            self.assertEqual(native.run.call_args.kwargs["structural_contract"],
                             worker_config["decision_format"]["source_contract"])
            actual_binding = json.loads((path / "execution-binding.json").read_bytes())
            native_binding = native.run.call_args.kwargs["execution_binding"]
            self.assertEqual(actual_binding, native_binding)
            baseline_config = dict(worker_config)
            baseline_config.pop("decision_format")
            baseline = runtime._execution_binding(baseline_config, {"model": "scripted"},
                encounter=encounter, purpose="Choose", context="Same history")
            self.assertNotEqual(actual_binding["input_sha256"], baseline["input_sha256"])
            self.assertNotEqual(actual_binding["source_sha256"], baseline["source_sha256"])
            self.assertEqual(result["response"], "actual scripted response")
            self.assertNotIn("decision_format", config)

    def test_invalid_source_and_unsupported_backend_do_not_reach_native_admission(self):
        _, text, encounter = fixture()
        for backend, selected in (("ollama_qwen3_base", text + " "), ("local_adapter", text)):
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as directory:
                config = self.configured(Path(directory), backend)
                bridge = self.bridge(config)
                with patch.object(runtime, "_profiles", return_value={runtime.ROUTE: config}), \
                        patch.object(runtime, "_native") as native, \
                        patch.object(runtime, "_retain_local_failure", return_value={"provider_called": False}):
                    result = bridge.run(encounter=encounter, purpose="Choose", decision_case_json=selected)
                self.assertEqual(result["status"], "FAILED")
                self.assertIsNone(result["outcome_handle"])
                native.assert_not_called()
                bridge._worker.assert_not_called()

    def test_static_config_cannot_silently_enable_decoding(self):
        with patch.object(runtime, "_json", return_value={"schema": runtime.SCHEMA,
                "backend": "ollama_qwen3_base", "decision_format": None}):
            with self.assertRaisesRegex(ValueError, "per-encounter"):
                runtime._config("/unused")

    def test_native_review_facades_forward_exact_inputs(self):
        native = Mock()
        bridge = self.bridge({})
        with patch.object(runtime, "_config", return_value={"selected": "configured"}), \
                patch.object(runtime, "_native", return_value=native):
            bridge.decision_review_context("actual-outcome")
            bridge.preview_decision_review("actual-outcome", case_json="exact span", policy={"declared": True}, author="Curator")
        native.decision_review_context.assert_called_once_with("actual-outcome")
        native.preview_decision_review.assert_called_once_with("actual-outcome", case_json="exact span", policy={"declared": True}, author="Curator")


if __name__ == "__main__":
    unittest.main()
