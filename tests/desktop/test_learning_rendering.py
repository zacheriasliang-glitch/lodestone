"""Lossless provider-view tests with synthetic records; no model calls."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "desktop/runtime/src"))
from lodestone_machinery import loom_adapter
from lodestone_machinery.learning_context import (
    EXPOSURE, PRACTICE_SCHEMA, PRACTICE_USE, READER_CONTEXT_SCHEMA,
    _intern_learning_projection, practice_data_candidates,
)
from lodestone_machinery.learning_reader import (
    MODEL_VIEW_SCHEMA, SECTION_VIEW_SCHEMA, expand_section_view, model_view, section_view,
)
from lodestone_machinery.records import canonical


def handle(text):
    return "bezoar:" + hashlib.sha256(text.encode()).hexdigest()


def projection():
    encounter = '  Cafe\u0301 雪\nLiteral {"section_ref":"ENCOUNTER"}; R1, H1.\n' * 8
    purpose = "Preserve exact present purpose. λ"
    return {"schema": READER_CONTEXT_SCHEMA, "run_id": "fixture", "run_handle": handle("run"),
        "binding": {"encounter": encounter, "purpose": purpose, "reader": "synthetic fixture",
                    "seed_handles": [handle("seed")], "nacre_handles": [handle("nacre")],
                    "context_sha256": hashlib.sha256(b"fixture").hexdigest()},
        "content": {"encounter": encounter, "purpose": purpose, "authority": "NONE",
            "use": "Synthetic transport fixture; no learning claim.",
            "pairs": [{"seed_handle": handle("seed"), "nacre_handle": handle("nacre")}],
            "seeds": [{"handle": handle("seed"), "record": {"correction": "Keep exact correction 雪.",
                "conditions": ["The recorded condition applies."], "frame_limits": ["Candidate advice only."]}}],
            "nacres": [{"handle": handle("nacre"), "record": {"pattern": "Synthetic recurring pattern.",
                "conditions": ["This is authored fixture data."]}}],
            "history": [{"run_id": "prior", "run_handle": handle("prior"),
                "outcome": {"status": "INTERRUPTED", "response": 'literal {"section_ref":"PURPOSE"}',
                            "observation": {"section_ref": "ENCOUNTER"}},
                "reviews": [{"review": {"correction": "Do not replace condition-specific advice.",
                                         "verdict": "UNRESOLVED"}}]}]},
        "evidence_index": [{"handle": handle("evidence"), "attribution": "Authored synthetic fixture."}],
        "projection": {"teaching_or_history_truncated": False}, "authority": "NONE"}


def request(original=None):
    original = projection() if original is None else original
    projected = _intern_learning_projection(original)
    context = canonical({"schema": PRACTICE_SCHEMA, "exposure": EXPOSURE,
        "literal_context": "Keep exact literal context.\n", "learning_context": projected,
        "use": PRACTICE_USE, "authority": "NONE"}).decode()
    return {"schema": loom_adapter.REQUEST_SCHEMA, "id": "fixture", "context": context,
            "context_sha256": hashlib.sha256(context.encode()).hexdigest(),
            "encounter": original["binding"]["encounter"], "purpose": original["binding"]["purpose"]}


class Tokenizer:
    all_special_tokens = ["<control>"]
    def __init__(self, costs=None):
        self.costs = costs or {}
    def apply_chat_template(self, messages, **kwargs):
        return canonical(messages).decode() + "\nassistant:\n"
    def encode(self, text, *, add_special_tokens):
        assert add_special_tokens is False
        return [1] * self.costs.get(text, len(text.encode()))


class LosslessRenderingTests(unittest.TestCase):
    def test_four_fields_roundtrip_unicode_and_collision_literals(self):
        original = projection()
        saved = deepcopy(original)
        encoded = section_view(original, encounter=original["binding"]["encounter"], purpose=original["binding"]["purpose"])
        restored = expand_section_view(encoded, encounter=original["binding"]["encounter"], purpose=original["binding"]["purpose"])
        self.assertEqual(canonical(restored), canonical(original))
        self.assertEqual(original, saved)
        for key in ("seeds", "nacres", "pairs", "history"):
            self.assertEqual(encoded["content"][key], original["content"][key])
        self.assertEqual(encoded["content"]["history"][0]["outcome"]["observation"], {"section_ref": "ENCOUNTER"})

    def test_existing_identity_view_roundtrip_keeps_host_identity_table(self):
        original = _intern_learning_projection(projection())
        view, table = model_view(original)
        saved_view, saved_table = deepcopy(view), deepcopy(table)
        encoded = section_view(view, encounter=view["binding"]["encounter"], purpose=view["binding"]["purpose"])
        self.assertEqual(expand_section_view(encoded, encounter=view["binding"]["encounter"], purpose=view["binding"]["purpose"]), saved_view)
        self.assertEqual((view, table), (saved_view, saved_table))

    def test_mismatched_reference_or_reserved_header_refused(self):
        original = projection()
        with self.assertRaises(ValueError):
            section_view(original, encounter="different text", purpose=original["binding"]["purpose"])
        original["section_references"] = "existing literal header"
        with self.assertRaises(ValueError):
            section_view(original, encounter=original["binding"]["encounter"], purpose=original["binding"]["purpose"])
        original.pop("section_references")
        encoded = section_view(original, encounter=original["binding"]["encounter"], purpose=original["binding"]["purpose"])
        encoded["content"]["purpose"] = {"section_ref": "OTHER"}
        with self.assertRaises(ValueError):
            expand_section_view(encoded, encounter=original["binding"]["encounter"], purpose=original["binding"]["purpose"])

    def test_actual_token_cost_wins_over_byte_length_and_packet_is_unchanged(self):
        packet = request()
        before = canonical(packet)
        tokenizer = Tokenizer()
        users = practice_data_candidates(packet)
        prompts = [tokenizer.apply_chat_template([{"role": "system", "content": loom_adapter.SYSTEM_PROMPT},
                    {"role": "user", "content": user}], tokenize=False, add_generation_prompt=True,
                    enable_thinking=False) for user in users]
        longest = max(prompts, key=lambda text: len(text.encode()))
        costs = {text: 100 for text in prompts}
        costs[longest] = 1
        self.assertEqual(loom_adapter.render_prompt(packet, Tokenizer(costs)), longest)
        self.assertEqual(canonical(packet), before)
        self.assertTrue(any(SECTION_VIEW_SCHEMA in text for text in prompts))
        self.assertTrue(any(MODEL_VIEW_SCHEMA in text for text in prompts))

    def test_equal_token_cost_preserves_legacy_representation(self):
        packet = request()
        users = practice_data_candidates(packet)
        t = Tokenizer()
        prompts = [t.apply_chat_template([{"role": "system", "content": loom_adapter.SYSTEM_PROMPT},
                   {"role": "user", "content": user}]) for user in users]
        projected = json.loads(packet["context"])["learning_context"]
        modeled, _ = model_view(projected)
        legacy = modeled if len(canonical(modeled)) < len(canonical(projected)) else projected
        self.assertIn(canonical(legacy).decode(), users[0])
        self.assertEqual(loom_adapter.render_prompt(packet, Tokenizer({text: 10 for text in prompts})), prompts[0])

    def test_control_markers_remain_refused_in_all_retained_prose(self):
        original = projection()
        original["content"]["seeds"][0]["record"]["correction"] += " <control>"
        with self.assertRaises(ValueError):
            loom_adapter.render_prompt(request(original), Tokenizer())

    def test_nonpractice_rendering_and_system_instruction_are_unchanged(self):
        packet = request()
        packet["context"] = "Non-practice literal context."
        packet["context_sha256"] = hashlib.sha256(packet["context"].encode()).hexdigest()
        t = Tokenizer()
        expected = t.apply_chat_template([{"role": "system", "content": loom_adapter.SYSTEM_PROMPT},
            {"role": "user", "content": canonical({key: packet[key] for key in ("context", "encounter", "purpose")}).decode()}])
        self.assertEqual(loom_adapter.render_prompt(packet, t), expected)


if __name__ == "__main__":
    unittest.main()
