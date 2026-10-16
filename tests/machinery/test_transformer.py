"""Numerical and custody checks; seeded examples are not trained model evidence."""
from copy import deepcopy
import json
import math
from pathlib import Path
import tempfile
import unittest

from lodestone_machinery import transformer as tf


def identity(width):
    return {"weight": [[float(i == j) for j in range(width)] for i in range(width)],
            "bias": [0.0] * width}


def zero_dense(state):
    state["weight"] = [[0.0] * len(row) for row in state["weight"]]
    state["bias"] = [0.0] * len(state["bias"])


class TransformerTests(unittest.TestCase):
    def small(self, **kwargs):
        options = {"seed": 7, "width": 4, "heads": 2, "max_tokens": 8}
        options.update(kwargs)
        return tf.initialize_model(["a", "b", "c", " "], **options)

    def test_scaled_causal_attention_known_values(self):
        output, weights = tf._attention([[2, 0], [0, 2]], [[1, 0], [0, 1]], [[1, 10], [3, 30]])
        latter = 1 / (1 + math.exp(-math.sqrt(2)))
        self.assertEqual(weights[0], [1.0, 0.0])
        self.assertEqual(output[0], [1.0, 10.0])
        self.assertAlmostEqual(weights[1][1], latter, places=14)
        self.assertAlmostEqual(output[1][0], 1 + 2 * latter, places=14)
        self.assertAlmostEqual(output[1][1], 10 + 20 * latter, places=13)
        self.assertAlmostEqual(sum(weights[1]), 1.0, places=14)

    def test_layer_norm_uses_population_variance_gain_and_bias(self):
        state = {"gain": [2, 3], "bias": [10, 20]}
        output = tf._layer_norm([[1, 3], [5, 5]], state, 1e-5)
        self.assertAlmostEqual(output[0][0], 10 - 2 / math.sqrt(1.00001), places=14)
        self.assertAlmostEqual(output[0][1], 20 + 3 / math.sqrt(1.00001), places=14)
        self.assertEqual(output[1], [10.0, 20.0])

    def test_dense_orientation_and_stable_softmax(self):
        self.assertEqual(tf._dense([[2, 3]], {"weight": [[1, 4], [5, 6]], "bias": [4, 3]}), [[21, 29]])
        self.assertEqual(tf._softmax([10000, 10000]), [0.5, 0.5])
        for left, right in zip(tf._softmax([0, 1]), tf._softmax([10000, 10001])):
            self.assertAlmostEqual(left, right, places=14)

    def test_complete_block_matches_hand_calculation(self):
        model = tf.initialize_model(["a", "b"], width=2, heads=1, ff_width=2, max_tokens=2)
        model["token_embeddings"][0] = [1.0, 3.0]
        model["position_embeddings"] = [[0.0, 0.0], [0.0, 0.0]]
        block = model["blocks"][0]
        block["attention"] = {name: identity(2) for name in ("query", "key", "value", "output")}
        block["ff"] = {"input": identity(2), "output": identity(2)}
        model["lm_head"] = identity(2)
        # x=[1,3]; attention residual adds [-a,a]. ReLU FFN adds [0,b].
        # Final normalized half-difference is c, so the identity head gives [-c,c].
        epsilon = model["config"]["epsilon"]
        a = 1 / math.sqrt(1 + epsilon)
        b = (1 + a) / math.sqrt((1 + a)**2 + epsilon)
        difference = 1 + a + b / 2
        c = difference / math.sqrt(difference**2 + epsilon)
        result = tf.forward(model, [0])
        self.assertAlmostEqual(result["logits"][0][0], -c, places=14)
        self.assertAlmostEqual(result["logits"][0][1], c, places=14)
        self.assertEqual(result["attention"], [[[[1.0]]]])
        self.assertEqual(result["semantic_validation"], "NOT_ESTABLISHED")

    def test_multi_head_slices_have_distinct_known_weights(self):
        model = self.small()
        model["token_embeddings"][0] = [1.0, -1.0, 0.0, 0.0]
        model["token_embeddings"][1] = [0.0, 0.0, 1.0, -1.0]
        model["position_embeddings"] = [[0.0] * 4 for _ in range(8)]
        model["blocks"][0]["attention"] = {name: identity(4) for name in ("query", "key", "value", "output")}
        result = tf.forward(model, [0, 1])
        self.assertEqual(result["attention"][0][0][1], [0.5, 0.5])
        score = math.sqrt(2) / (0.5 + model["config"]["epsilon"])
        self.assertAlmostEqual(result["attention"][0][1][1][1], 1 / (1 + math.exp(-score)), places=14)

    def test_future_tokens_cannot_change_earlier_logits(self):
        model = self.small(layers=2, width=8)
        before = deepcopy(model)
        short = tf.forward(model, [0, 1])
        long = tf.forward(model, [0, 1, 2, 3, 2])
        changed = tf.forward(model, [0, 1, 3, 0, 1])
        self.assertEqual(short["logits"], long["logits"][:2])
        self.assertEqual(short["logits"], changed["logits"][:2])
        for layer in long["attention"]:
            for head in layer:
                for query, row in enumerate(head):
                    self.assertAlmostEqual(sum(row), 1, places=14)
                    self.assertEqual(row[query + 1:], [0.0] * (len(row) - query - 1))
        self.assertEqual(model, before)

    def test_position_embeddings_distinguish_repeated_tokens(self):
        model = self.small()
        positioned = tf.forward(model, [0, 0])["logits"]
        self.assertGreater(max(abs(a - b) for a, b in zip(*positioned)), 0.01)
        model["position_embeddings"] = [[0.0] * 4 for _ in range(8)]
        repeated = tf.forward(model, [0, 0])["logits"]
        for a, b in zip(*repeated):
            self.assertAlmostEqual(a, b, places=14)

    def test_initializer_is_reproducible_explicitly_untrained_and_independent(self):
        first, second = self.small(), self.small()
        self.assertEqual(first, second)
        self.assertEqual(first["provenance"]["training_status"], "UNTRAINED")
        self.assertNotEqual(tf.model_sha256(first), tf.model_sha256(self.small(seed=8)))
        copy = tf.validate_model(first)
        copy["token_embeddings"][0][0] += 1
        self.assertEqual(first, second)

    def test_json_roundtrip_preserves_predictions_and_detects_tampering(self):
        model = self.small()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.json"
            saved = tf.save_model(model, path)
            self.assertEqual(saved["sha256"], tf.model_sha256(model))
            self.assertEqual(tf.forward(tf.load_model(path), [0, 1]), tf.forward(model, [0, 1]))
            with self.assertRaises(FileExistsError):
                tf.save_model(model, path)
            envelope = json.loads(path.read_text())
            envelope["model"]["token_embeddings"][0][0] += 0.125
            path.write_text(json.dumps(envelope))
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                tf.load_model(path)

    def test_load_rejects_duplicate_keys_oversize_and_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.json"
            path.write_text('{"format":"first","format":"second"}')
            with self.assertRaisesRegex(ValueError, "Duplicate"):
                tf.load_model(path)
            path.write_bytes(b" " * (tf.MAX_MODEL_BYTES + 1))
            with self.assertRaisesRegex(ValueError, "size bound"):
                tf.load_model(path)
            link = Path(directory) / "link.json"
            link.symlink_to(path)
            with self.assertRaisesRegex(ValueError, "symlink"):
                tf.load_model(link)

    def test_shape_numeric_and_provenance_validation(self):
        malformed = []
        for invalid in (float("nan"), float("inf"), True, "1", 1e7, 10**500):
            model = self.small()
            model["token_embeddings"][0][0] = invalid
            malformed.append(model)
        model = self.small()
        model["blocks"][0]["attention"]["value"]["weight"].pop()
        malformed.append(model)
        model = self.small()
        model["vocabulary"][1] = model["vocabulary"][0]
        malformed.append(model)
        model = self.small()
        model["provenance"]["training_status"] = "SEMANTICALLY_VERIFIED"
        malformed.append(model)
        model = self.small()
        model["extra"] = "unrecognized state"
        malformed.append(model)
        for index, model in enumerate(malformed):
            with self.subTest(case=index), self.assertRaises(ValueError):
                tf.validate_model(model)

    def test_initialization_caps_reject_before_allocation(self):
        for argument in ({"width": 33}, {"heads": 9}, {"layers": 3}, {"max_tokens": 65},
                         {"ff_width": 129}, {"seed": True}, {"width": 5, "heads": 2}):
            with self.subTest(argument=argument), self.assertRaises(ValueError):
                self.small(**argument)
        with self.assertRaises(ValueError):
            tf.initialize_model([str(i) for i in range(257)])

    def test_bad_token_ids_and_generation_caps(self):
        model = self.small(max_tokens=4)
        for tokens in ([], [0] * 5, [-1], [4], [True], [1.0], (0, 1)):
            with self.subTest(tokens=tokens), self.assertRaises(ValueError):
                tf.forward(model, tokens)
        for options in ({"max_new_tokens": 4}, {"max_new_tokens": -1}, {"max_new_tokens": True}, {"eos_token_id": 4}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                tf.generate(model, [0], **options)

    def test_generation_uses_budget_eos_and_records_actual_context(self):
        model = self.small()
        zero_dense(model["lm_head"])
        model["lm_head"]["bias"] = [0.0, 3.0, 0.0, 0.0]
        generated = tf.generate(model, [0], max_new_tokens=3)
        self.assertEqual(generated["token_ids"], [0, 1, 1, 1])
        self.assertEqual(generated["text"], "abbb")
        self.assertEqual(generated["stop_reason"], "TOKEN_BUDGET")
        self.assertEqual(generated["model_sha256"], tf.model_sha256(model))
        self.assertEqual([row["input_length"] for row in generated["trace"]], [1, 2, 3])
        for step in generated["trace"]:
            for layer in step["attention"]:
                for head in layer:
                    self.assertEqual(len(head), step["input_length"])
                    self.assertAlmostEqual(sum(head), 1.0, places=14)
        stopped = tf.generate(model, [0], max_new_tokens=3, eos_token_id=1)
        self.assertEqual(stopped["generated_ids"], [1])
        self.assertEqual(stopped["stop_reason"], "EOS_TOKEN")
        zero = tf.generate(model, [0], max_new_tokens=0)
        self.assertEqual(zero["trace"], [])
        self.assertEqual(zero["generated_ids"], [])
        zero_dense(model["lm_head"])
        self.assertEqual(tf.generate(model, [2], max_new_tokens=1)["generated_ids"], [0])


if __name__ == "__main__":
    unittest.main()
