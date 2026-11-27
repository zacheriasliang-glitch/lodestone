"""Explicit practice options bind the native request; no model calls."""
from contextlib import nullcontext
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "desktop/runtime/src"))
from lodestone_machinery import loom_runtime as runtime
from lodestone_nurarihyon import learning

FIRST = "bezoar:" + "1" * 64
SECOND = "bezoar:" + "2" * 64


class PracticeSelectionTests(unittest.TestCase):
    def bridge(self, directory):
        directory = Path(directory).resolve()
        active_path = Path(directory) / "active.json"
        active_path.write_text(json.dumps({"schema": "lodestone-learning-active/v1",
            "required": True, "memory_workspace": str(Path(directory) / "memory"),
            "seed_handles": [FIRST]}))
        config = {"backend": "ollama_qwen3_base", "runs_dir": str(directory),
                  "learning_config": str(active_path), "max_new_tokens": 64,
                  "max_total_tokens": 1024, "wall_seconds": 3,
                  "max_rss_bytes": 10000000, "ollama_model_digest": "scripted"}
        bridge = object.__new__(runtime.LoomBridge)
        bridge.config_path, bridge._config = "/unused", config
        bridge._lock, bridge._active, bridge._unsafe_groups = threading.Lock(), set(), set()
        bridge._worker = Mock(return_value={"status": "COMPLETED", "response": "scripted raw"})
        native = Mock()
        native.memory.preparation_lessons.return_value = []
        native.run.side_effect = lambda **kw: kw["provider"]({
            "encounter": kw["encounter"], "purpose": kw["purpose"], "context": kw["context"]})
        return bridge, config, native

    def test_selected_lessons_reach_native_and_change_execution_identity_without_changing_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            bridge, config, native = self.bridge(directory)
            selected = [SECOND]
            with patch.object(runtime, "_profiles", return_value={runtime.ROUTE: config}), \
                    patch.object(runtime, "runtime_identity", return_value={"model": "scripted"}), \
                    patch.object(runtime, "_native", return_value=native) as construct, \
                    patch.object(learning, "locked", return_value=nullcontext()):
                result = bridge.run(encounter="Current", purpose="Purpose", seed_handles=selected)
                effective = construct.call_args.args[0]
                baseline = bridge.run(encounter="Current", purpose="Purpose")
                self.assertNotIn("seed_handles", construct.call_args.args[0])
            selected.clear()
            self.assertEqual(effective["seed_handles"], [SECOND])
            self.assertNotIn("seed_handles", config)
            first = json.loads((Path(result["artifacts_dir"]) / "execution-binding.json").read_text())
            second = json.loads((Path(baseline["artifacts_dir"]) / "execution-binding.json").read_text())
            self.assertNotEqual(first["input_sha256"], second["input_sha256"])
            self.assertEqual(result["response"], "scripted raw")

    def test_native_default_and_explicit_selection_share_workspace_and_leave_active_unchanged(self):
        active = {"schema": "lodestone-learning-active/v1", "required": True,
                  "memory_workspace": "/retained-workspace", "seed_handles": [FIRST]}
        memory = Mock()
        with patch.object(runtime, "_json", return_value=active), \
                patch("lodestone_machinery.bezoar_learning.LearningMemory.from_active", return_value=memory) as load, \
                patch.object(runtime, "UnifiedLoom") as loom:
            runtime._native({"learning_config": "/active"})
            load.assert_called_once_with(active)
            loom.assert_called_with(memory, [FIRST])
            runtime._native({"learning_config": "/active", "seed_handles": [SECOND]})
            self.assertEqual(load.call_count, 2)
            load.assert_called_with(active)
            loom.assert_called_with(memory, [SECOND])
        self.assertEqual(active["seed_handles"], [FIRST])

    def test_invalid_selection_or_unbound_strategy_never_reaches_native_provider(self):
        for options in ({"seed_handles": []}, {"seed_handles": [FIRST, FIRST]},
                        {"seed_handles": ["not-a-handle"]}, {"seed_handles": [True]},
                        {"seed_handles": [FIRST] * 17}, {"decision_strategy": "evidence_first"},
                        {"decision_strategy": "automatic"}, {"decision_strategy": None}):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as directory:
                bridge, config, native = self.bridge(directory)
                with patch.object(runtime, "_profiles", return_value={runtime.ROUTE: config}), \
                        patch.object(runtime, "_native", return_value=native) as construct, \
                        patch.object(runtime, "_retain_local_failure", return_value={"status": "TEST_CAPTURE"}) as retain:
                    result = bridge.run(encounter="Current", purpose="Purpose", **options)
                self.assertEqual(result["status"], "FAILED")
                self.assertIsNone(result["outcome_handle"])
                construct.assert_not_called()
                bridge._worker.assert_not_called()
                retain.assert_called_once()

    def test_options_cannot_become_persistent_defaults(self):
        for key, value in (("seed_handles", [FIRST]), ("decision_strategy", "evidence_first")):
            with self.subTest(key=key), patch.object(runtime, "_json", return_value={
                    "schema": runtime.SCHEMA, "backend": "ollama_qwen3_base", key: value}):
                with self.assertRaisesRegex(ValueError, "per-encounter"):
                    runtime._config("/unused")

    def test_evidence_first_and_selection_reach_worker_as_independent_options(self):
        case = {"case_id": "fixture", "target": "A", "recorded_prior_action": "KEEP",
                "allowed_actions": ["KEEP", "CHANGE"], "rules": ["Use the supplied condition."],
                "observations": [{"id": "one", "target": "A", "order": 1,
                                  "verified": True, "text": "A source condition."},
                                 {"id": "two", "target": "B", "order": 4,
                                  "verified": False, "text": "Another source condition."}]}
        source = json.dumps(case)
        with tempfile.TemporaryDirectory() as directory:
            bridge, config, native = self.bridge(directory)
            with patch.object(runtime, "_profiles", return_value={runtime.ROUTE: config}), \
                    patch.object(runtime, "runtime_identity", return_value={"model": "scripted"}), \
                    patch.object(runtime, "_native", return_value=native), \
                    patch.object(learning, "locked", return_value=nullcontext()):
                result = bridge.run(encounter=source, purpose="Choose", seed_handles=[SECOND],
                                    decision_case_json=source, decision_strategy="evidence_first")
            effective, _, packet, _ = bridge._worker.call_args.args
            self.assertEqual(effective["seed_handles"], [SECOND])
            self.assertEqual(effective["decision_format"]["schema"], "lodestone-decision-format/v2")
            props = effective["decision_format"]["response_schema"]["properties"]
            self.assertEqual(props["basis"]["enum"], case["observations"])
            self.assertEqual(props["decision"]["properties"]["action"]["enum"], case["allowed_actions"])
            self.assertEqual(packet["encounter"], source)
            self.assertEqual(result["response"], "scripted raw")
            self.assertNotIn("decision_strategy", config)

    def test_inventory_reads_native_candidates_without_a_practice_run(self):
        native = Mock()
        native.seed_handles = [FIRST]
        native.memory.practice_candidates.return_value = {"schema": "loom-practice-candidates/v1"}
        with tempfile.TemporaryDirectory() as directory:
            bridge, config, _ = self.bridge(directory)
            with patch.object(runtime, "_config", return_value=config), \
                    patch.object(runtime, "_native", return_value=native), \
                    patch.object(learning, "locked", return_value=nullcontext()):
                self.assertEqual(bridge.practice_candidates(), {"schema": "loom-practice-candidates/v1"})
        native.memory.practice_candidates.assert_called_once_with([FIRST])
        native.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
