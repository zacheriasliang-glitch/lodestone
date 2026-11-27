"""Practice/voxel delivery and retained failures with scripted providers only."""
from contextlib import redirect_stdout
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from test_reader_history_window import bootstrap, RUNTIME
from test_learning_rendering import Tokenizer
from lodestone_machinery.bezoar_learning import LearningMemory
from lodestone_machinery import learning_context as lc, loom_voxels as v, loom_runtime
from lodestone_machinery.learning_cli import main
from lodestone_machinery.loom_adapter import render_prompt
from lodestone_machinery.loom_unified import UnifiedLoom
from lodestone_machinery.records import canonical
from lodestone_nurarihyon.learning_conscience import assess


class PracticeVoxelTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        initialized = bootstrap.initialize(self.root / "preview", RUNTIME, Path(sys.executable))
        self.active = Path(initialized["active_config"])
        config = json.loads(self.active.read_text())
        self.memory = LearningMemory(config["memory_workspace"])
        self.seeds = config["seed_handles"]
        self.loom = UnifiedLoom(self.memory, self.seeds)
        self.encounter, self.purpose = "Inspect the current observation.", "Keep useful distinctions."
        self.prepared = self.memory.prepare(self.seeds, encounter=self.encounter,
            purpose=self.purpose, reader="Scripted test; no model")
        self.voxel = v.voxel_context(teaching="Keep competing readings attached to evidence.")

    def build(self, voxel=None, window=None):
        return lc.build_adapter_request(self.memory, self.prepared, request_id="fixture",
            context="The literal account.", encounter=self.encounter, purpose=self.purpose,
            history_window=window, voxel=voxel)

    def test_voxel_and_window_preserve_the_complete_selected_history_in_every_candidate(self):
        for window in (None, 1):
            with self.subTest(window=window):
                plain, added = self.build(window=window), self.build(self.voxel, window)
                body = json.loads(added["context"])
                self.assertEqual(body.pop("voxel"), self.voxel)
                self.assertEqual(canonical(body).decode(), plain["context"])
                self.assertEqual(lc.validate_practice_packet(self.memory, self.prepared, added, "adapter"), added)
                self.assertEqual(assess(self.memory, self.prepared, added)["verdict"], "ALLOW")
                candidates = lc.practice_data_candidates(added)
                self.assertGreater(len(candidates), 1)
                for candidate in candidates:
                    self.assertIn("VOXEL_TEACHING", candidate)
                    self.assertIn("Teaching:\nKeep competing readings", candidate)
                    self.assertIn("LEARNING_CONTEXT_JSON", candidate)
                self.assertIn("VOXEL_TEACHING", render_prompt(added, Tokenizer()))

    def test_malformed_voxel_is_refused_even_when_full_native_preparation_is_embedded(self):
        packet = self.build(self.voxel)
        for voxel in (None, "{}", json.dumps(json.loads(self.voxel), indent=2)):
            with self.subTest(voxel=voxel):
                body = json.loads(packet["context"])
                body["voxel"] = voxel
                body["literal_context"] = canonical(self.prepared).decode()
                text = canonical(body).decode()
                changed = dict(packet, context=text, context_sha256=hashlib.sha256(text.encode()).hexdigest())
                self.assertEqual(assess(self.memory, self.prepared, changed)["verdict"], "REFUSE")
                with self.assertRaises(ValueError):
                    render_prompt(changed, Tokenizer())

    def test_changed_purpose_keeps_old_should_as_history(self):
        move = v.should(intention="make it tall", act="pull upward", unless="complete", purpose="Height.")
        voxel = v.voxel_context(teaching="Check current purpose.", shoulds=[move])
        prompt = render_prompt(self.build(voxel), Tokenizer())
        self.assertIn("Earlier advice, kept as history", prompt)
        self.assertNotIn("Current advice for this purpose", prompt)

    def test_oversized_opt_in_does_not_select_less_history(self):
        plain = self.build(window=1)
        with patch("lodestone_machinery.loom_adapter.MAX_CONTEXT_BYTES", len(plain["context"].encode())):
            self.assertEqual(self.build(window=1), plain)
            with self.assertRaisesRegex(ValueError, "retained history is unchanged"):
                self.build(self.voxel, 1)

    def test_real_lifecycle_keeps_failed_return_and_correction_with_voxel(self):
        first = self.loom.run(encounter="Earlier fixture", purpose=self.purpose, reader="Scripted fixture",
            provider=lambda packet: {"status":"FAILED", "response":"actual incomplete return", "observation":{}})
        self.loom.correct(first["outcome_handle"], author="Fixture", correction="Measure before choosing.")
        seen = []
        def provide(packet):
            seen.append(deepcopy(packet))
            reference = json.loads(packet["context"])
            projection = lc.expand_learning_projection(reference["learning_context"])
            prepared = self.memory.read_prepared(projection["run_handle"])
            with patch.object(lc, "validate_practice_packet", wraps=lc.validate_practice_packet) as rebuild:
                verdict = assess(self.memory, prepared, packet)
                self.assertIn(verdict["context_delivery"], ("RULE_FOLDED_NATIVE_READER_PROJECTION",
                                                           "RULE_DEPARTED_NATIVE_READER_PROJECTION"))
                self.assertEqual(verdict["verdict"], "ALLOW")
                self.assertEqual(rebuild.call_count, 1)
                reference["voxel"] = "{}"
                changed = dict(packet, context=canonical(reference).decode())
                changed["context_sha256"] = hashlib.sha256(changed["context"].encode()).hexdigest()
                self.assertEqual(assess(self.memory, prepared, changed)["verdict"], "REFUSE")
                self.assertEqual(rebuild.call_count, 2)
            return "scripted return"
        result = self.loom.run(encounter=self.encounter, purpose=self.purpose, reader="Scripted fixture",
            voxel=self.voxel, history_window=1, provider=provide)
        self.assertEqual(result["status"], "COMPLETED", self.memory.read_outcome(result["outcome_handle"]))
        self.assertEqual(result["semantic_learning"], "NOT_ESTABLISHED")
        self.assertFalse(result["weight_update"])
        body = json.loads(seen[0]["context"])
        self.assertEqual(body["voxel"], self.voxel)
        self.assertIn("Measure before choosing.", seen[0]["context"])
        native = self.memory.read_outcome(first["outcome_handle"])
        self.assertEqual(native["status"], "FAILED")
        self.assertEqual(native["response"], "actual incomplete return")
        provider = Mock()
        bad = self.loom.run(encounter="Invalid fixture", purpose=self.purpose, reader="Scripted fixture",
            voxel="{}", provider=provider)
        provider.assert_not_called()
        self.assertEqual(bad["status"], "FAILED")
        self.assertIsNotNone(bad["outcome_handle"])

    def test_configured_packet_command_includes_explicit_voxel_without_dispatch(self):
        literal, voxel = self.root / "literal.txt", self.root / "voxel.json"
        literal.write_text("The literal account."); voxel.write_text(self.voxel)
        output = io.StringIO()
        with redirect_stdout(output):
            status = main(["--config", str(self.active), "packet", self.prepared["run_handle"],
                "--kind", "adapter", "--context", str(literal), "--voxel-context", str(voxel), "--history-window", "1"])
        self.assertEqual(status, 0)
        packet = json.loads(output.getvalue())
        self.assertEqual(json.loads(packet["context"])["voxel"], self.voxel)
        self.assertIsNone(self.memory.outcome_for(self.prepared))

    def test_execution_identity_binds_optional_voxel_and_it_is_not_a_persistent_default(self):
        config = {"generation":{"max_tokens":32}, "max_total_tokens":1024,
                  "wall_seconds":3, "max_rss_bytes":10000000}
        plain = loom_runtime._execution_binding(config, {}, encounter=self.encounter, purpose=self.purpose, context="")
        opted = loom_runtime._execution_binding(config, {}, encounter=self.encounter, purpose=self.purpose,
            context="", voxel=self.voxel)
        self.assertNotEqual(plain["input_sha256"], opted["input_sha256"])
        self.assertNotEqual(plain["source_sha256"], opted["source_sha256"])
        with patch.object(loom_runtime, "_json", return_value={"schema":loom_runtime.SCHEMA,
                "backend":"ollama_qwen3_base", "voxel":self.voxel}):
            with self.assertRaisesRegex(ValueError, "per-encounter"):
                loom_runtime._config("/unused")


if __name__ == "__main__":
    unittest.main()
