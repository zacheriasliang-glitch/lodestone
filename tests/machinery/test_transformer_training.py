"""Synthetic numerical/optimization checks, never semantic training evidence."""
from copy import deepcopy
import math
import unittest
from unittest import mock

from lodestone_machinery import transformer as tf
from lodestone_machinery import transformer_training as tr


def small(**options):
    config = {"seed": 37, "width": 4, "heads": 2, "max_tokens": 4, "ff_width": 6}
    config.update(options)
    return tf.initialize_model(list("abcd"), **config)


def locate(model, path):
    keys = path.split("/")
    parent = model
    for key in keys[:-1]:
        parent = parent[int(key)] if isinstance(parent, list) else parent[key]
    return parent, int(keys[-1])


def cross_entropy(model, tokens, target, actions=None):
    logits = tf.forward(model, tokens)["logits"][-1]
    selected = [logits[index] for index in actions] if actions is not None else logits
    maximum = max(selected)
    return maximum + math.log(sum(math.exp(value - maximum) for value in selected)) - logits[target]


class TransformerTrainingTests(unittest.TestCase):
    def test_training_forward_matches_original_all_positions_multihead(self):
        for heads in (1, 2, 4):
            model = small(heads=heads, max_tokens=16)
            for tokens in ([0], [0, 1, 2], [0, 1, 2, 3] * 4):
                expected = tf.forward(model, tokens)["logits"]
                for mode in ("full", "head"):
                    with self.subTest(heads=heads, tokens=len(tokens), mode=mode):
                        observed = tr.loss_and_gradients(model, list(tokens), 3, mode=mode)
                        for actual_row, expected_row in zip(observed["logits"], expected):
                            for actual, value in zip(actual_row, expected_row):
                                self.assertAlmostEqual(actual, value, places=12)
                        self.assertAlmostEqual(observed["loss"], cross_entropy(model, list(tokens), 3), places=12)

    def test_all_parameter_gradients_against_finite_differences(self):
        model = small()
        tokens, target = [0, 1, 0], 3  # Repeated embeddings must accumulate both uses.
        # Keep every feed-forward ReLU away from its nondifferentiable kink.
        model["blocks"][0]["ff"]["input"]["bias"] = [8.0] * 6
        result = tr.loss_and_gradients(model, tokens, target)
        self.assertEqual(set(result["gradients"]), set(tr.parameter_map(model)))
        step = 1e-6
        largest_error = 0.0
        for path, derivative in result["gradients"].items():
            parent, key = locate(model, path)
            saved = parent[key]
            parent[key] = saved + step
            high = cross_entropy(model, tokens, target)
            parent[key] = saved - step
            low = cross_entropy(model, tokens, target)
            parent[key] = saved
            finite_difference = (high - low) / (2 * step)
            largest_error = max(largest_error, abs(derivative - finite_difference))
            with self.subTest(path=path):
                self.assertAlmostEqual(derivative, finite_difference, delta=2e-6)
        self.assertLess(largest_error, 2e-6)
        groups = {tr._group(path) for path in result["gradients"]}
        self.assertEqual(groups, {"token_embeddings", "position_embeddings", "final_norm", "lm_head",
                                 "blocks/0/attention_norm", "blocks/0/ff_norm", "blocks/0/ff/input",
                                 "blocks/0/ff/output", "blocks/0/attention/query", "blocks/0/attention/key",
                                 "blocks/0/attention/value", "blocks/0/attention/output"})

    def test_negative_relu_derivative_and_nonzero_bias_gradient(self):
        model = small()
        model["blocks"][0]["ff"]["input"]["bias"] = [-100.0] * 6
        result = tr.loss_and_gradients(model, [0, 1], 2)
        for path, value in result["gradients"].items():
            if path.startswith("blocks/0/ff/input/") or path.startswith("blocks/0/ff/output/weight/"):
                self.assertEqual(value, 0.0)
        self.assertTrue(any(value != 0 for path, value in result["gradients"].items()
                            if path.startswith("blocks/0/ff/output/bias/")))

    def test_full_optimization_lowers_loss_and_changes_every_parameter_group(self):
        model = small()
        before = deepcopy(model)
        examples = [{"token_ids": [0, 1], "target": 2}, {"token_ids": [1, 0], "target": 3}]
        examples_before = deepcopy(examples)
        trained, report = tr.train(model, examples, 30, seed=1, learning_rate=0.1)
        self.assertEqual(model, before)
        self.assertEqual(examples, examples_before)
        self.assertEqual(report["status"], "COMPLETED")
        self.assertEqual(report["completed_updates"], 30)
        self.assertEqual(len(report["update_losses"]), 30)
        self.assertLess(report["final_evaluation"]["mean_loss"], report["initial_evaluation"]["mean_loss"] / 2)
        self.assertEqual(set(report["changed_groups"]), set(report["trainable_groups"]))
        self.assertNotEqual(report["encoder_sha256_before"], report["encoder_sha256_after"])
        self.assertEqual(report["initial_model_sha256"], tf.model_sha256(model))
        self.assertEqual(report["final_model_sha256"], tf.model_sha256(trained))
        self.assertEqual(trained["provenance"]["training_status"], "UNVERIFIED")
        self.assertEqual(report["semantic_validation"], "NOT_ESTABLISHED")
        self.assertEqual(report["authority"], "NONE")
        self.assertEqual(tf.validate_model(trained), trained)
        self.assertGreater(report["wall_seconds"], 0)
        self.assertGreater(report["cpu_seconds"], 0)

    def test_six_action_head_has_exact_parameter_count_and_freezes_everything_else(self):
        model = tf.initialize_model([str(i) for i in range(12)], seed=7, width=4,
                                    heads=2, max_tokens=4, ff_width=6)
        actions = [6, 7, 8, 9, 10, 11]
        examples = [{"token_ids": [0, 1, 2], "target": 6}, {"token_ids": [2, 1, 0], "target": 11}]
        trained, report = tr.train(model, examples, 25, seed=1, learning_rate=0.1,
                                   mode="head", action_ids=actions)
        before, after = tr.parameter_map(model), tr.parameter_map(trained)
        selected = tr.parameter_map(model, mode="head", action_ids=actions)
        self.assertEqual(len(selected), 6 * (4 + 1))
        self.assertEqual(report["trainable_parameters"], 30)
        self.assertEqual(report["trainable_groups"], {"lm_head": 30})
        self.assertEqual(report["encoder_sha256_before"], report["encoder_sha256_after"])
        self.assertEqual(report["frozen_parameters_sha256_before"], report["frozen_parameters_sha256_after"])
        for path in before.keys() - selected.keys():
            self.assertEqual(before[path], after[path], path)
        self.assertLess(report["final_evaluation"]["mean_loss"], report["initial_evaluation"]["mean_loss"])

    def test_full_action_subset_updates_encoder_but_preserves_unselected_outputs(self):
        model = small()
        actions = [2, 3]
        example = {"token_ids": [0, 1], "target": 2}
        grads = tr.loss_and_gradients(model, example["token_ids"], 2, action_ids=actions)
        self.assertAlmostEqual(grads["loss"], cross_entropy(model, example["token_ids"], 2, actions), places=12)
        self.assertEqual(set(grads["gradients"]), set(tr.parameter_map(model, action_ids=actions)))
        trained, report = tr.train(model, [example], 10, learning_rate=0.1, action_ids=actions)
        before, after = tr.parameter_map(model), tr.parameter_map(trained)
        for path in before:
            if path.startswith("lm_head/") and int(path.rsplit("/", 1)[1]) not in actions:
                self.assertEqual(before[path], after[path])
        self.assertNotEqual(report["encoder_sha256_before"], report["encoder_sha256_after"])
        self.assertEqual(report["frozen_parameters_sha256_before"], report["frozen_parameters_sha256_after"])

    def test_same_seed_is_exactly_reproducible_and_different_seed_changes_order(self):
        model = small()
        examples = [{"token_ids": [i], "target": (i + 1) % 4} for i in range(4)]
        one, a = tr.train(model, examples, 12, seed=2)
        two, b = tr.train(model, examples, 12, seed=2)
        three, c = tr.train(model, examples, 12, seed=3)
        self.assertEqual(one, two)
        self.assertEqual(a["update_losses"], b["update_losses"])
        self.assertEqual(a["example_order_sha256"], b["example_order_sha256"])
        self.assertNotEqual(a["example_order_sha256"], c["example_order_sha256"])
        self.assertNotEqual(one, three)

    def test_timeout_returns_only_complete_updates_and_reports_actual_counts(self):
        model = small()
        examples = [{"token_ids": [0, 1], "target": 2}]
        real_gradients = tr._gradients
        calls = 0

        def expire_on_third(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise tr.TrainingTimeout("injected deadline during third update")
            return real_gradients(*args, **kwargs)

        with mock.patch.object(tr, "_gradients", side_effect=expire_on_third):
            partial, report = tr.train(model, examples, 10, seed=4)
        complete, short = tr.train(model, examples, 2, seed=4)
        self.assertEqual(report["status"], "TIME_LIMIT")
        self.assertEqual(report["completed_updates"], 2)
        self.assertEqual(tr.parameter_map(partial), tr.parameter_map(complete))
        self.assertEqual(report["update_losses"], short["update_losses"])
        immediate, report = tr.train(model, examples, 10, max_seconds=1e-12)
        self.assertEqual(report["status"], "TIME_LIMIT")
        self.assertEqual(report["completed_updates"], 0)
        self.assertEqual(report["initial_evaluation"]["coverage"], "PARTIAL")
        self.assertEqual(report["final_evaluation"]["coverage"], "PARTIAL")
        self.assertEqual(tr.parameter_map(immediate), tr.parameter_map(model))
        with self.assertRaises(tr.TrainingTimeout):
            tr.loss_and_gradients(model, [0, 1], 2, max_seconds=1e-12)

    def test_graph_bound_and_nonfinite_state_fail_without_mutating_input(self):
        model = small()
        before = deepcopy(model)
        with mock.patch.object(tr, "MAX_TAPE_NODES", 10):
            with self.assertRaisesRegex(tr.TrainingError, "graph exceeds"):
                tr.train(model, [{"token_ids": [0], "target": 1}], 1)
        self.assertEqual(model, before)
        for bad in (float("nan"), float("inf"), -float("inf")):
            broken = deepcopy(model)
            broken["lm_head"]["weight"][0][0] = bad
            with self.assertRaises(ValueError):
                tr.train(broken, [{"token_ids": [0], "target": 1}], 1)

    def test_training_and_example_bounds_reject_without_truncation(self):
        model = small()
        example = [{"token_ids": [0], "target": 1}]
        for options in ({"steps": 0}, {"steps": 1001}, {"steps": True}, {"seed": -1},
                        {"learning_rate": 0}, {"learning_rate": float("nan")},
                        {"learning_rate": 1.01}, {"max_seconds": 0}, {"max_seconds": 301},
                        {"learning_rate": 10**500},
                        {"mode": "unknown"}, {"action_ids": [1, 1]}, {"action_ids": [1]},
                        {"action_ids": [0, 2]}, {"action_ids": [True, 1]}):
            kwargs = {"steps": 1, **options}
            with self.subTest(options=options), self.assertRaises(ValueError):
                tr.train(model, example, **kwargs)
        for examples in ([], example * 2001, [{"token_ids": [0] * 5, "target": 1}],
                         [{"token_ids": [-1], "target": 1}], [{"token_ids": [0], "target": 4}],
                         [{"token_ids": [0], "target": 1, "extra": 1}],
                         [{"token_ids": [0], "target": True}]):
            with self.subTest(examples=len(examples)), self.assertRaises(ValueError):
                tr.train(model, examples, 1)
        models = [small(width=10, heads=2, ff_width=16), small(layers=2),
                  small(max_tokens=17), small(ff_width=17),
                  tf.initialize_model([str(i) for i in range(129)], width=4, max_tokens=4)]
        for bounded in models:
            with self.subTest(config=bounded["config"]), self.assertRaises(ValueError):
                tr.train(bounded, example, 1)


if __name__ == "__main__":
    unittest.main()
