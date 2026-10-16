"""Bounded standard-library reverse-mode training for the tiny transformer.

This optimizes next-token cross entropy on explicit integer examples. Numerical
optimization is not semantic validation. Every returned trained model retains
the existing transformer schema and declares training_status UNVERIFIED.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import math
import random
import time

from . import transformer as tf


MAX_WIDTH = 8
MAX_LAYERS = 1
MAX_SEQUENCE = 16
MAX_FF_WIDTH = 16
MAX_VOCABULARY = 128
MAX_EXAMPLES = 2000
MAX_UPDATES = 1000
MAX_SECONDS = 300
MAX_TAPE_NODES = 300000
GRADIENT_CLIP = 1.0
_ROOTS = ("token_embeddings", "position_embeddings", "blocks", "final_norm", "lm_head")


class TrainingError(ValueError):
    """Malformed training request or nonfinite/out-of-bounds computation."""


class TrainingTimeout(TrainingError):
    """An explicitly bounded computation reached its CPU or wall deadline."""


def _integer(value, low, high, name):
    if type(value) is not int or not low <= value <= high:
        raise TrainingError(f"{name} must be an integer from {low} to {high}")


def _positive(value, maximum, name):
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and 0 < value <= maximum
    except OverflowError:
        valid = False
    if not valid:
        raise TrainingError(f"{name} must be finite, positive, and at most {maximum}")


def _model(model):
    model = tf.validate_model(model)
    config = model["config"]
    for name, maximum in (("width", MAX_WIDTH), ("layers", MAX_LAYERS),
                          ("max_tokens", MAX_SEQUENCE), ("ff_width", MAX_FF_WIDTH)):
        if config[name] > maximum:
            raise TrainingError(f"Training {name} exceeds {maximum}")
    if len(model["vocabulary"]) > MAX_VOCABULARY:
        raise TrainingError("Training vocabulary exceeds 128")
    return model


def _mode(mode):
    if mode not in ("full", "head"):
        raise TrainingError("mode must be full or head")


def _actions(model, action_ids):
    vocabulary = len(model["vocabulary"])
    if action_ids is None:
        return list(range(vocabulary))
    if type(action_ids) is not list or not 2 <= len(action_ids) <= vocabulary:
        raise TrainingError("action_ids must be a list of at least two distinct vocabulary IDs")
    for value in action_ids:
        _integer(value, 0, vocabulary - 1, "action ID")
    if len(set(action_ids)) != len(action_ids):
        raise TrainingError("action_ids must be distinct")
    return list(action_ids)


def _example(model, token_ids, target, actions):
    tf._token_ids(token_ids, model)
    _integer(target, 0, len(model["vocabulary"]) - 1, "target")
    if target not in actions:
        raise TrainingError("target is absent from action_ids")


def _locations(model, mode="full", actions=None):
    """Iteratively yield (slash path, containing list, index), never metadata."""
    _mode(mode)
    stack = [(name, model[name], None, None) for name in reversed(_ROOTS)
             if mode == "full" or name == "lm_head"]
    while stack:
        path, value, parent, key = stack.pop()
        if type(value) is dict:
            stack.extend((path + "/" + name, value[name], value, name)
                         for name in reversed(sorted(value)))
        elif type(value) is list:
            stack.extend((path + "/" + str(index), value[index], value, index)
                         for index in reversed(range(len(value))))
        else:
            if actions is None or not path.startswith("lm_head/") or int(path.rsplit("/", 1)[1]) in actions:
                yield path, parent, key


def parameter_map(model, mode="full", action_ids=None):
    """Return stable slash-separated parameter paths and independent float values."""
    model = _model(model)
    actions = _actions(model, action_ids)
    return {path: float(parent[key]) for path, parent, key in _locations(model, mode, actions)}


def _group(path):
    parts = path.split("/")
    if parts[0] != "blocks":
        return parts[0]
    return "/".join(parts[:4] if parts[2] in ("attention", "ff") else parts[:3])


def _parameter_hash(values):
    return hashlib.sha256(tf._canonical(values)).hexdigest()


class _Budget:
    def __init__(self, seconds):
        _positive(seconds, MAX_SECONDS, "max_seconds")
        self.seconds = seconds
        self.wall_start = time.monotonic()
        self.cpu_start = time.process_time()

    def elapsed(self):
        return time.monotonic() - self.wall_start, time.process_time() - self.cpu_start

    def check(self):
        wall, cpu = self.elapsed()
        if wall >= self.seconds or cpu >= self.seconds:
            raise TrainingTimeout("Training reached its explicit CPU or wall-time limit")


class _Tape:
    """Creation order is already topological; backward needs no recursive walk."""
    def __init__(self, budget):
        self.nodes = []
        self.budget = budget

    def node(self, data, parents=()):
        if len(self.nodes) >= MAX_TAPE_NODES:
            raise TrainingError("Autodiff graph exceeds its node bound")
        if len(self.nodes) % 256 == 0:
            self.budget.check()
        if not math.isfinite(data) or any(not math.isfinite(derivative) for _, derivative in parents):
            raise TrainingError("Nonfinite autodiff value or local derivative")
        result = _Value(float(data), parents, self)
        self.nodes.append(result)
        return result

    def backward(self, loss):
        loss.grad = 1.0
        for index, node in enumerate(reversed(self.nodes)):
            if index % 256 == 0:
                self.budget.check()
            if node.grad:
                for parent, derivative in node.parents:
                    parent.grad += derivative * node.grad
                    if not math.isfinite(parent.grad):
                        raise TrainingError("Nonfinite accumulated gradient")


class _Value:
    __slots__ = ("data", "parents", "tape", "grad")

    def __init__(self, data, parents, tape):
        self.data, self.parents, self.tape, self.grad = data, parents, tape, 0.0

    def __add__(self, other):
        parents = ((self, 1.0), (other, 1.0)) if isinstance(other, _Value) else ((self, 1.0),)
        return self.tape.node(self.data + _number(other), parents)

    __radd__ = __add__

    def __neg__(self):
        return self.tape.node(-self.data, ((self, -1.0),))

    def __sub__(self, other):
        return self + (-other)

    def __rsub__(self, other):
        return (-self) + other

    def __mul__(self, other):
        parents = ((self, _number(other)),)
        if isinstance(other, _Value):
            parents += ((other, self.data),)
        return self.tape.node(self.data * _number(other), parents)

    __rmul__ = __mul__

    def __truediv__(self, other):
        value = _number(other)
        parents = ((self, 1.0 / value),)
        if isinstance(other, _Value):
            parents += ((other, -self.data / value**2),)
        return self.tape.node(self.data / value, parents)

    def __rtruediv__(self, other):
        return self.tape.node(other / self.data, ((self, -other / self.data**2),))

    def __pow__(self, exponent):
        return self.tape.node(self.data**exponent, ((self, exponent * self.data**(exponent - 1)),))


def _number(value):
    return value.data if isinstance(value, _Value) else value


def _sqrt(value):
    result = math.sqrt(_number(value))
    return value.tape.node(result, ((value, 0.5 / result),)) if isinstance(value, _Value) else result


def _exp(value):
    result = math.exp(_number(value))
    return value.tape.node(result, ((value, result),)) if isinstance(value, _Value) else result


def _log(value):
    result = math.log(_number(value))
    return value.tape.node(result, ((value, 1.0 / value.data),)) if isinstance(value, _Value) else result


def _relu(value):
    result = max(0.0, _number(value))
    return value.tape.node(result, ((value, float(value.data > 0)),)) if isinstance(value, _Value) else result


def _dense(rows, state):
    return [[sum(x * row[j] for x, row in zip(vector, state["weight"])) + bias
             for j, bias in enumerate(state["bias"])] for vector in rows]


def _norm(rows, state, epsilon):
    result = []
    for row in rows:
        mean = sum(row) / len(row)
        variance = sum((value - mean)**2 for value in row) / len(row)
        scale = _sqrt(variance + epsilon)
        result.append([(value - mean) / scale * gain + bias
                       for value, gain, bias in zip(row, state["gain"], state["bias"])])
    return result


def _add(left, right):
    return [[a + b for a, b in zip(row_a, row_b)] for row_a, row_b in zip(left, right)]


def _forward(model, token_ids, budget):
    config = model["config"]
    width, heads = config["width"], config["heads"]
    head_width = width // heads
    hidden = _add([model["token_embeddings"][token] for token in token_ids],
                  model["position_embeddings"][:len(token_ids)])
    for block in model["blocks"]:
        budget.check()
        normalized = _norm(hidden, block["attention_norm"], config["epsilon"])
        matrices = [_dense(normalized, block["attention"][name]) for name in ("query", "key", "value")]
        concatenated = [[] for _ in token_ids]
        for head in range(heads):
            budget.check()
            start, end = head * head_width, (head + 1) * head_width
            queries, keys, values = [[row[start:end] for row in matrix] for matrix in matrices]
            for position, query in enumerate(queries):
                scores = [sum(q * k for q, k in zip(query, key)) / math.sqrt(head_width)
                          for key in keys[:position + 1]]
                # A detached stability shift cancels exactly in softmax derivatives.
                maximum = max(_number(score) for score in scores)
                exponentials = [_exp(score - maximum) for score in scores]
                total = sum(exponentials)
                weights = [value / total for value in exponentials]
                concatenated[position].extend(sum(weight * value[j] for weight, value in zip(weights, values))
                                               for j in range(head_width))
        hidden = _add(hidden, _dense(concatenated, block["attention"]["output"]))
        normalized = _norm(hidden, block["ff_norm"], config["epsilon"])
        activated = [[_relu(value) for value in row] for row in _dense(normalized, block["ff"]["input"])]
        hidden = _add(hidden, _dense(activated, block["ff"]["output"]))
    budget.check()
    logits = _dense(_norm(hidden, model["final_norm"], config["epsilon"]), model["lm_head"])
    if any(not math.isfinite(_number(value)) for row in logits for value in row):
        raise TrainingError("Nonfinite training logits")
    return logits


def _loss(logits, target, actions):
    maximum = max(_number(logits[action]) for action in actions)
    return maximum + _log(sum(_exp(logits[action] - maximum) for action in actions)) - logits[target]


def _gradients(model, token_ids, target, mode, actions, budget):
    # Only selected parameters become differentiable; the original stays intact.
    state = tf.validate_model(model)
    tape, parameters = _Tape(budget), {}
    try:
        for path, parent, key in _locations(state, mode, actions):
            parent[key] = parameters[path] = tape.node(parent[key])
        logits = _forward(state, token_ids, budget)
        loss = _loss(logits[-1], target, actions)
        tape.backward(loss)
        return {"loss": loss.data, "logits": [[_number(value) for value in row] for row in logits],
                "gradients": {path: value.grad for path, value in parameters.items()},
                "graph_nodes": len(tape.nodes)}
    finally:
        # Release the tape/value ownership cycle even on a deadline or failure.
        tape.nodes.clear()


def loss_and_gradients(model, token_ids, target, *, mode="full", action_ids=None, max_seconds=30):
    """Last-position cross entropy and path-keyed gradients; caller state unchanged.

    Optional action_ids restricts the loss softmax and trainable head columns,
    preserving all other output columns exactly. A timeout raises TrainingTimeout.
    """
    budget = _Budget(max_seconds)
    model = _model(model)
    _mode(mode)
    actions = _actions(model, action_ids)
    _example(model, token_ids, target, actions)
    try:
        result = _gradients(model, token_ids, target, mode, actions, budget)
    except (OverflowError, ZeroDivisionError) as exc:
        raise TrainingError("Training arithmetic exceeded finite bounds") from exc
    budget.check()
    wall, cpu = budget.elapsed()
    return {**result, "action_ids": actions, "mode": mode, "wall_seconds": wall, "cpu_seconds": cpu}


def _evaluate(model, examples, actions, budget):
    losses = []
    try:
        for example in examples:
            budget.check()
            logits = _forward(model, example["token_ids"], budget)
            loss = _loss(logits[-1], example["target"], actions)
            if not math.isfinite(loss):
                raise TrainingError("Nonfinite evaluation loss")
            budget.check()
            losses.append(loss)
    except TrainingTimeout:
        pass
    return {"mean_loss": sum(losses) / len(losses) if losses else None,
            "examples_evaluated": len(losses), "total_examples": len(examples),
            "coverage": "COMPLETE" if len(losses) == len(examples) else "PARTIAL"}


def train(model, examples, steps, seed=0, learning_rate=0.01, mode="full", max_seconds=60, action_ids=None):
    """Return (new_model, report) after deterministic shuffled single-example SGD.

    Global gradient norm is clipped at 1.0, reported explicitly. ``steps`` counts
    optimizer updates, not epochs. Time limits return the last complete update
    with TIME_LIMIT status and actual evaluation coverage; no retry or truncation
    of examples occurs. Full means every numeric parameter is trainable; unused
    embeddings and mathematically zero gradients can legitimately remain equal.
    """
    budget = _Budget(max_seconds)
    model = _model(model)
    _mode(mode)
    _integer(steps, 1, MAX_UPDATES, "steps")
    _integer(seed, 0, 2**32 - 1, "seed")
    _positive(learning_rate, 1.0, "learning_rate")
    if type(examples) is not list or not 1 <= len(examples) <= MAX_EXAMPLES:
        raise TrainingError("examples must contain 1 to 2000 explicit training examples")
    actions = _actions(model, action_ids)
    for example in examples:
        if type(example) is not dict or set(example) != {"token_ids", "target"}:
            raise TrainingError("Each example needs exactly token_ids and target")
        _example(model, example["token_ids"], example["target"], actions)
    # Copy input examples as well as parameters: no caller-owned object is edited.
    examples = [{"token_ids": list(item["token_ids"]), "target": item["target"]} for item in examples]
    initial_hash = tf.model_sha256(model)
    initial_values = {path: float(parent[key]) for path, parent, key in _locations(model)}
    trainable = [(path, parent, key) for path, parent, key in _locations(model, mode, actions)]
    trainable_paths = {path for path, _, _ in trainable}
    frozen_parameters_before = {path: value for path, value in initial_values.items() if path not in trainable_paths}
    frozen_before = {path: value for path, value in initial_values.items() if not path.startswith("lm_head/")}
    initial_eval = _evaluate(model, examples, actions, budget)
    rng = random.Random(seed)
    order, losses, observed_order = [], [], []
    clipped, graph_nodes = 0, 0
    status = "COMPLETED"
    try:
        for step in range(steps):
            budget.check()
            if step % len(examples) == 0:
                order = list(range(len(examples)))
                rng.shuffle(order)
            index = order[step % len(examples)]
            example = examples[index]
            result = _gradients(model, example["token_ids"], example["target"], mode, actions, budget)
            gradient_values = result["gradients"]
            norm = math.hypot(*gradient_values.values())
            if not math.isfinite(norm):
                raise TrainingError("Nonfinite gradient norm")
            scale = GRADIENT_CLIP / norm if norm > GRADIENT_CLIP else 1.0
            updates = [parent[key] - learning_rate * gradient_values[path] * scale
                       for path, parent, key in trainable]
            if any(not math.isfinite(value) or abs(value) > tf.MAX_WEIGHT for value in updates):
                raise TrainingError("Parameter update exceeds finite model bounds")
            budget.check()
            # Commit an entire update atomically after all validation and deadline checks.
            for (_, parent, key), value in zip(trainable, updates):
                parent[key] = value
            losses.append(result["loss"])
            observed_order.append(index)
            clipped += int(scale < 1.0)
            graph_nodes = max(graph_nodes, result["graph_nodes"])
    except TrainingTimeout:
        status = "TIME_LIMIT"
    except (OverflowError, ZeroDivisionError) as exc:
        raise TrainingError("Training arithmetic exceeded finite bounds") from exc
    final_eval = _evaluate(model, examples, actions, budget)
    if final_eval["coverage"] != "COMPLETE":
        status = "TIME_LIMIT"
    model["provenance"] = {
        "training_status": "UNVERIFIED", "seed": seed,
        "origin": f"bounded_stdlib_sgd_v1; mode={mode}; updates={len(losses)}/{steps}; "
                  f"status={status}; source_sha256={initial_hash}; semantic_validation=NOT_ESTABLISHED",
    }
    model = tf.validate_model(model)
    final_values = {path: float(parent[key]) for path, parent, key in _locations(model)}
    frozen_after = {path: value for path, value in final_values.items() if not path.startswith("lm_head/")}
    frozen_parameters_after = {path: value for path, value in final_values.items() if path not in trainable_paths}
    group_counts = dict(Counter(_group(path) for path, _, _ in trainable))
    changed_counts = dict(Counter(_group(path) for path in final_values if final_values[path] != initial_values[path]))
    wall, cpu = budget.elapsed()
    report = {"trainer": "bounded_stdlib_sgd_v1", "status": status, "mode": mode,
              "optimizer": "single-example shuffled SGD", "learning_rate": learning_rate,
              "gradient_clip_norm": GRADIENT_CLIP, "clipped_updates": clipped,
              "requested_updates": steps, "completed_updates": len(losses), "seed": seed,
              "examples": len(examples), "action_ids": actions,
              "initial_model_sha256": initial_hash, "final_model_sha256": tf.model_sha256(model),
              "encoder_sha256_before": _parameter_hash(frozen_before),
              "encoder_sha256_after": _parameter_hash(frozen_after),
              "frozen_parameters_sha256_before": _parameter_hash(frozen_parameters_before),
              "frozen_parameters_sha256_after": _parameter_hash(frozen_parameters_after),
              "trainable_parameters": len(trainable), "trainable_groups": group_counts,
              "changed_parameters": sum(changed_counts.values()), "changed_groups": changed_counts,
              "initial_evaluation": initial_eval, "final_evaluation": final_eval,
              "update_losses": losses, "example_order_sha256": _parameter_hash(observed_order),
              "largest_autodiff_graph_nodes": graph_nodes,
              "max_seconds": max_seconds, "wall_seconds": wall, "cpu_seconds": cpu,
              "deadline_enforcement": "cooperative checks per 256 autodiff nodes, layer/head, example and update; "
                                      "bounded bookkeeping can finish after the deadline",
              "semantic_validation": "NOT_ESTABLISHED", "authority": "NONE"}
    return model, report
