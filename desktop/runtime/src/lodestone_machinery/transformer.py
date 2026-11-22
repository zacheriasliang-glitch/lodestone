"""Tiny causal transformer inference, using only the Python standard library.

This is numerical machinery, not a semantic provider or a trained language model.
The initializer makes UNTRAINED weights. Caller-supplied training provenance is a
declaration, never a competence claim. Shapes and computation are deliberately
bounded for the laboratory's small CPU host. No code or model is downloaded.

Architecture: token + explicit position embeddings; pre-LayerNorm multi-head
causal attention and residual; pre-LayerNorm ReLU feed-forward and residual;
final LayerNorm and a vocabulary projection. Dense weights use [input][output].
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import random


VERSION = "0.1.0"
ARCHITECTURE = "causal_decoder_pre_ln_relu_v1"
MAX_TOKENS = 64
MAX_WIDTH = 32
MAX_LAYERS = 2
MAX_HEADS = 8
MAX_FF_WIDTH = 128
MAX_VOCABULARY = 256
MAX_MODEL_BYTES = 4 * 1024 * 1024
MAX_WEIGHT = 1_000_000


def _keys(value, names, label):
    if type(value) is not dict or set(value) != set(names):
        raise ValueError(label + " has missing or unknown fields")


def _integer(value, low, high, label):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{label} must be an integer from {low} to {high}")
    return value


def _number(value, label):
    if type(value) not in (int, float):
        raise ValueError(label + " must be a finite number")
    try:
        valid = math.isfinite(value) and abs(value) <= MAX_WEIGHT
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(label + " must be finite with magnitude <= 1000000")


def _vector(value, width, label):
    if type(value) is not list or len(value) != width:
        raise ValueError(label + " has the wrong shape")
    for item in value:
        _number(item, label)


def _matrix(value, rows, columns, label):
    if type(value) is not list or len(value) != rows:
        raise ValueError(label + " has the wrong shape")
    for row in value:
        _vector(row, columns, label)


def _dense_state(value, inputs, outputs, label):
    _keys(value, ("weight", "bias"), label)
    _matrix(value["weight"], inputs, outputs, label + ".weight")
    _vector(value["bias"], outputs, label + ".bias")


def _norm_state(value, width, label):
    _keys(value, ("gain", "bias"), label)
    _vector(value["gain"], width, label + ".gain")
    _vector(value["bias"], width, label + ".bias")


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _vocabulary(value):
    if type(value) is not list or not 2 <= len(value) <= MAX_VOCABULARY:
        raise ValueError("vocabulary must contain 2 to 256 distinct token strings")
    for token in value:
        if type(token) is not str or not token or len(token.encode("utf-8")) > 256:
            raise ValueError("each vocabulary token must contain 1 to 256 UTF-8 bytes")
    if len(set(value)) != len(value):
        raise ValueError("vocabulary tokens must be distinct")


def validate_model(model):
    """Reject malformed/big/nonfinite state and return an independent JSON copy."""
    _keys(model, ("schema_version", "architecture", "vocabulary", "config",
                  "provenance", "token_embeddings", "position_embeddings",
                  "blocks", "final_norm", "lm_head"), "model")
    if model["schema_version"] != VERSION or model["architecture"] != ARCHITECTURE:
        raise ValueError("Unsupported transformer model version or architecture")
    _vocabulary(model["vocabulary"])
    config = model["config"]
    _keys(config, ("width", "heads", "layers", "ff_width", "max_tokens", "epsilon"), "config")
    width = _integer(config["width"], 2, MAX_WIDTH, "width")
    heads = _integer(config["heads"], 1, MAX_HEADS, "heads")
    layers = _integer(config["layers"], 1, MAX_LAYERS, "layers")
    ff_width = _integer(config["ff_width"], 2, MAX_FF_WIDTH, "ff_width")
    length = _integer(config["max_tokens"], 1, MAX_TOKENS, "max_tokens")
    if width % heads:
        raise ValueError("width must be divisible by heads")
    _number(config["epsilon"], "epsilon")
    if not 1e-12 <= config["epsilon"] <= 1:
        raise ValueError("epsilon must be between 1e-12 and 1")
    provenance = model["provenance"]
    _keys(provenance, ("training_status", "origin", "seed"), "provenance")
    if provenance["training_status"] not in ("UNTRAINED", "UNVERIFIED"):
        raise ValueError("training_status must be UNTRAINED or UNVERIFIED")
    if (type(provenance["origin"]) is not str or not provenance["origin"].strip()
            or len(provenance["origin"].encode("utf-8")) > 4096):
        raise ValueError("provenance origin must contain 1 to 4096 UTF-8 bytes")
    if provenance["seed"] is not None:
        _integer(provenance["seed"], 0, 2**32 - 1, "seed")
    _matrix(model["token_embeddings"], len(model["vocabulary"]), width, "token_embeddings")
    _matrix(model["position_embeddings"], length, width, "position_embeddings")
    if type(model["blocks"]) is not list or len(model["blocks"]) != layers:
        raise ValueError("blocks has the wrong shape")
    for block in model["blocks"]:
        _keys(block, ("attention_norm", "attention", "ff_norm", "ff"), "block")
        _norm_state(block["attention_norm"], width, "attention_norm")
        _norm_state(block["ff_norm"], width, "ff_norm")
        _keys(block["attention"], ("query", "key", "value", "output"), "attention")
        for name in ("query", "key", "value", "output"):
            _dense_state(block["attention"][name], width, width, "attention." + name)
        _keys(block["ff"], ("input", "output"), "ff")
        _dense_state(block["ff"]["input"], width, ff_width, "ff.input")
        _dense_state(block["ff"]["output"], ff_width, width, "ff.output")
    _norm_state(model["final_norm"], width, "final_norm")
    _dense_state(model["lm_head"], width, len(model["vocabulary"]), "lm_head")
    try:
        encoded = _canonical(model)
    except (UnicodeError, ValueError) as exc:
        raise ValueError("Model is not valid JSON/UTF-8") from exc
    if len(encoded) > MAX_MODEL_BYTES:
        raise ValueError("Model exceeds its serialized size bound")
    return json.loads(encoded)


def initialize_model(vocabulary, *, seed=0, width=16, heads=2, layers=1,
                     max_tokens=32, ff_width=None):
    """Make reproducible UNTRAINED weights, not a learned language model.

    Position embeddings begin as sinusoidal values and are saved explicitly.
    The seed is initialization provenance; the content hash identifies weights.
    """
    _vocabulary(vocabulary)
    _integer(seed, 0, 2**32 - 1, "seed")
    _integer(width, 2, MAX_WIDTH, "width")
    _integer(heads, 1, MAX_HEADS, "heads")
    _integer(layers, 1, MAX_LAYERS, "layers")
    _integer(max_tokens, 1, MAX_TOKENS, "max_tokens")
    ff_width = 2 * width if ff_width is None else ff_width
    _integer(ff_width, 2, MAX_FF_WIDTH, "ff_width")
    if width % heads:
        raise ValueError("width must be divisible by heads")
    rng = random.Random(seed)

    def weights(rows, columns):
        return [[rng.gauss(0, 1 / math.sqrt(rows)) for _ in range(columns)] for _ in range(rows)]

    def dense(inputs, outputs):
        return {"weight": weights(inputs, outputs), "bias": [0.0] * outputs}

    def norm():
        return {"gain": [1.0] * width, "bias": [0.0] * width}

    model = {"schema_version": VERSION, "architecture": ARCHITECTURE,
             "vocabulary": list(vocabulary),
             "config": {"width": width, "heads": heads, "layers": layers,
                        "ff_width": ff_width, "max_tokens": max_tokens, "epsilon": 1e-5},
             "provenance": {"training_status": "UNTRAINED", "origin": "seeded_gaussian_v1", "seed": seed},
             "token_embeddings": weights(len(vocabulary), width),
             "position_embeddings": [[(math.sin if i % 2 == 0 else math.cos)(
                 position / (10000 ** (2 * (i // 2) / width)))
                 for i in range(width)] for position in range(max_tokens)],
             "blocks": [], "final_norm": norm(), "lm_head": dense(width, len(vocabulary))}
    for _ in range(layers):
        model["blocks"].append({"attention_norm": norm(),
                                "attention": {name: dense(width, width) for name in ("query", "key", "value", "output")},
                                "ff_norm": norm(),
                                "ff": {"input": dense(width, ff_width), "output": dense(ff_width, width)}})
    return validate_model(model)


def model_sha256(model):
    """Identity of canonical complete state, including declared provenance."""
    return hashlib.sha256(_canonical(validate_model(model))).hexdigest()


def save_model(model, path):
    """Publish a new hash-bound JSON envelope; existing paths are never replaced."""
    model = validate_model(model)
    digest = hashlib.sha256(_canonical(model)).hexdigest()
    data = _canonical({"format": "lodestone-transformer-state-v1", "sha256": digest, "model": model})
    if len(data) > MAX_MODEL_BYTES:
        raise ValueError("Model envelope exceeds its serialized size bound")
    with Path(path).open("xb") as stream:
        stream.write(data)
    return {"path": str(Path(path)), "sha256": digest, "bytes": len(data)}


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


def load_model(path):
    """Read bounded JSON only, verifying the complete model's content hash."""
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("Model must be a regular file, not a symlink")
    with path.open("rb") as stream:
        data = stream.read(MAX_MODEL_BYTES + 1)
    if len(data) > MAX_MODEL_BYTES:
        raise ValueError("Model file exceeds its serialized size bound")
    try:
        envelope = json.loads(data, object_pairs_hook=_unique_pairs)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError("Invalid model JSON: " + str(exc)) from exc
    _keys(envelope, ("format", "sha256", "model"), "model envelope")
    if envelope["format"] != "lodestone-transformer-state-v1":
        raise ValueError("Unsupported model envelope format")
    model = validate_model(envelope["model"])
    if envelope["sha256"] != hashlib.sha256(_canonical(model)).hexdigest():
        raise ValueError("Model content hash mismatch")
    return model


def _dense(rows, state):
    return [[sum(x * row[j] for x, row in zip(vector, state["weight"])) + bias
             for j, bias in enumerate(state["bias"])] for vector in rows]


def _layer_norm(rows, state, epsilon):
    output = []
    for row in rows:
        mean = sum(row) / len(row)
        variance = sum((value - mean)**2 for value in row) / len(row)
        scale = math.sqrt(variance + epsilon)
        output.append([(value - mean) / scale * gain + bias
                       for value, gain, bias in zip(row, state["gain"], state["bias"])])
    return output


def _softmax(scores):
    maximum = max(scores)
    exponentials = [math.exp(score - maximum) for score in scores]
    total = sum(exponentials)
    return [value / total for value in exponentials]


def _attention(queries, keys, values):
    """Scaled causal attention: disallowed keys never enter the softmax."""
    scale = math.sqrt(len(queries[0]))
    output, weights = [], []
    for position, query in enumerate(queries):
        scores = [sum(q * k for q, k in zip(query, key)) / scale
                  for key in keys[:position + 1]]
        row = _softmax(scores)
        output.append([sum(weight * value[j] for weight, value in zip(row, values))
                       for j in range(len(values[0]))])
        weights.append(row + [0.0] * (len(keys) - len(row)))
    return output, weights


def _add(left, right):
    return [[a + b for a, b in zip(row_a, row_b)] for row_a, row_b in zip(left, right)]


def _token_ids(value, model):
    if type(value) is not list or not 1 <= len(value) <= model["config"]["max_tokens"]:
        raise ValueError("Supply 1 to max_tokens token IDs; context is never silently truncated")
    for token in value:
        _integer(token, 0, len(model["vocabulary"]) - 1, "token ID")


def _forward(model, token_ids):
    config = model["config"]
    width, heads = config["width"], config["heads"]
    head_width = width // heads
    hidden = _add([model["token_embeddings"][token] for token in token_ids],
                  model["position_embeddings"][:len(token_ids)])
    attention = []
    for block in model["blocks"]:
        normalized = _layer_norm(hidden, block["attention_norm"], config["epsilon"])
        query, key, value = [_dense(normalized, block["attention"][name]) for name in ("query", "key", "value")]
        concatenated = [[] for _ in token_ids]
        layer_attention = []
        for head in range(heads):
            start, end = head * head_width, (head + 1) * head_width
            attended, weights = _attention(*[[row[start:end] for row in matrix] for matrix in (query, key, value)])
            for row, result in zip(concatenated, attended):
                row.extend(result)
            layer_attention.append(weights)
        hidden = _add(hidden, _dense(concatenated, block["attention"]["output"]))
        normalized = _layer_norm(hidden, block["ff_norm"], config["epsilon"])
        activated = [[max(0.0, value) for value in row] for row in _dense(normalized, block["ff"]["input"])]
        hidden = _add(hidden, _dense(activated, block["ff"]["output"]))
        attention.append(layer_attention)
    logits = _dense(_layer_norm(hidden, model["final_norm"], config["epsilon"]), model["lm_head"])
    if any(not math.isfinite(value) for row in logits for value in row):
        raise ValueError("Transformer computation produced nonfinite logits")
    return logits, attention


def _identity(model):
    return {"schema_version": VERSION, "architecture": ARCHITECTURE,
            "model_sha256": hashlib.sha256(_canonical(model)).hexdigest(),
            "provenance": dict(model["provenance"]), "semantic_validation": "NOT_ESTABLISHED"}


def forward(model, token_ids):
    """Return next-token logits at every position and [layer][head][query][key] attention."""
    model = validate_model(model)
    _token_ids(token_ids, model)
    logits, attention = _forward(model, token_ids)
    return {**_identity(model), "token_ids": list(token_ids), "logits": logits, "attention": attention}


def generate(model, prompt_ids, *, max_new_tokens=8, eos_token_id=None):
    """Greedy autoregressive generation, with complete context and no hidden restart.

    Equal logits select the lowest token ID. Attention in each trace entry is
    [layer][head][key] for the last input position, whose logits select the next
    token. Vocabulary tokens concatenate literally; this is not a tokenizer.
    """
    model = validate_model(model)
    _token_ids(prompt_ids, model)
    _integer(max_new_tokens, 0, MAX_TOKENS, "max_new_tokens")
    if len(prompt_ids) + max_new_tokens > model["config"]["max_tokens"]:
        raise ValueError("Requested generation exceeds max_tokens; lower the explicit budget")
    if eos_token_id is not None:
        _integer(eos_token_id, 0, len(model["vocabulary"]) - 1, "eos_token_id")
    token_ids = list(prompt_ids)
    trace = []
    reason = "TOKEN_BUDGET"
    for _ in range(max_new_tokens):
        logits, attention = _forward(model, token_ids)
        next_id = max(range(len(model["vocabulary"])), key=lambda token: logits[-1][token])
        trace.append({"input_length": len(token_ids), "next_token_id": next_id,
                      "next_token_logits": logits[-1],
                      "attention": [[head[-1] for head in layer] for layer in attention]})
        token_ids.append(next_id)
        if next_id == eos_token_id:
            reason = "EOS_TOKEN"
            break
    tokens = [model["vocabulary"][token] for token in token_ids]
    return {**_identity(model), "method": "GREEDY", "prompt_ids": list(prompt_ids),
            "token_ids": token_ids, "generated_ids": token_ids[len(prompt_ids):],
            "tokens": tokens, "text": "".join(tokens), "stop_reason": reason, "trace": trace}
