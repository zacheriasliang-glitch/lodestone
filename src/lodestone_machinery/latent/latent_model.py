"""Bounded experimental geometric bottleneck and reconstruction mechanism.

An eight-byte block becomes 16 nibble tokens. Three final-position transformer
logits are rounded to signed four-bit coordinates; a separate leaky-ReLU decoder
receives only those coordinates and predicts 16 nibbles. Its negative slope is
0.1, also used at zero, so the zero code does not erase reconstruction gradients.
No source or hidden-state skip
connection enters that decoder. This is a new reconstruction objective, not the
historical next-token/selector training contract.

The reverse pass deliberately uses a straight-through surrogate for rounding:
identity strictly inside (-8, 7), zero at and beyond its endpoints. It is NOT
the mathematical derivative of rounding. The commitment term pulls raw logits
toward the detached quantized values, including outside the clipping interval.

compile_decoder materializes all 4,096 reconstructions into a 32,768-byte table.
The caller must pin, retain and charge this entire compiled decoder/codebook.
Use that table, not repeat floating-point inference, to create AND decode exact
residuals. Compilation is deployment of a learned decoder, not free storage or
runtime neural reasoning. Untrained initialization and this mechanism alone
establish no compression, semantic fidelity, or useful learning result.

Pinned private dependencies (frozen files are not modified): transformer.py
91c8de71cb0972bc61936698871ab0133db0d18a15dfc2ca0f102b93208c0e30;
transformer_training.py
447035b779f7587f87752d5105bd6cc4a2d9dbbf0240e4e563da469d8f3e5a2d.
These document the contract; the caller's manifest must enforce code pins.
"""
from __future__ import annotations

import hashlib
import json
import math
import random

from .. import transformer as tf
from .. import transformer_training as training


SCHEMA = "crystal-latent-model/v1"
ARCHITECTURE = "nibble16-transformer8-lattice3-leakyrelu01-8-reconstruction16x16/v1"
VOCABULARY = list("0123456789abcdef")
CONFIG = {"width": 8, "heads": 2, "layers": 1, "ff_width": 16,
          "max_tokens": 16, "epsilon": 1e-5}
QUANTIZER = {"dimensions": 3, "minimum": -8, "maximum": 7,
             "rounding": "floor(z+0.5), then clip",
             "surrogate": "identity strictly inside (-8,7); zero otherwise"}
COMMITMENT_BETA = 0.01
LEAKY_SLOPE = 0.1
BLOCK_BYTES = 8
TABLE_BYTES = 32768
MAX_MODEL_BYTES = 1024 * 1024
MAX_UPDATES = 1000
_DECODER_SEED_XOR = 0x4C415445


class LatentError(ValueError):
    """Malformed or out-of-bounds mechanism input or arithmetic."""


class LatentTimeout(LatentError):
    """A bounded operation expired; no partial result is admitted."""


def _keys(value, expected, label):
    if type(value) is not dict or set(value) != set(expected):
        raise LatentError(label + " has invalid fields")


def _number(value, label):
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and abs(value) <= tf.MAX_WEIGHT
    except OverflowError:
        valid = False
    if not valid:
        raise LatentError(label + " must be a finite bounded number, not bool")


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _dense_validate(state, inputs, outputs, label):
    _keys(state, ("weight", "bias"), label)
    if type(state["weight"]) is not list or len(state["weight"]) != inputs:
        raise LatentError(label + " weight rows differ")
    for row in state["weight"]:
        if type(row) is not list or len(row) != outputs:
            raise LatentError(label + " weight columns differ")
        for value in row:
            _number(value, label)
    if type(state["bias"]) is not list or len(state["bias"]) != outputs:
        raise LatentError(label + " bias differs")
    for value in state["bias"]:
        _number(value, label)


def validate(model):
    """Validate fixed shapes/bounds and return an independent canonical copy."""
    _keys(model, ("schema", "architecture", "provenance", "encoder", "decoder",
                  "quantizer", "commitment_beta"), "model")
    if model["schema"] != SCHEMA or model["architecture"] != ARCHITECTURE:
        raise LatentError("Unsupported latent model schema or architecture")
    _keys(model["quantizer"], QUANTIZER, "quantizer")
    if (model["quantizer"] != QUANTIZER or any(type(model["quantizer"][key]) is not int
            for key in ("dimensions", "minimum", "maximum"))):
        raise LatentError("Quantizer differs from the fixed contract")
    if type(model["commitment_beta"]) not in (int, float) or model["commitment_beta"] != COMMITMENT_BETA:
        raise LatentError("Commitment beta must be exactly 0.01")
    provenance = model["provenance"]
    _keys(provenance, ("training_status", "origin", "seed", "updates"), "provenance")
    if type(provenance["seed"]) is not int or not 0 <= provenance["seed"] < 2**32:
        raise LatentError("seed must be an integer from 0 through 2**32-1")
    if type(provenance["updates"]) is not int or not 0 <= provenance["updates"] <= MAX_UPDATES:
        raise LatentError("updates exceed the model bound")
    expected_status = "UNTRAINED" if provenance["updates"] == 0 else "UNVERIFIED"
    if provenance["training_status"] != expected_status:
        raise LatentError("training_status disagrees with declared updates")
    if type(provenance["origin"]) is not str or not provenance["origin"].strip():
        raise LatentError("origin must be nonempty text")
    try:
        if len(provenance["origin"].encode("utf-8")) > 4096:
            raise LatentError("origin exceeds its byte bound")
        encoder = tf.validate_model(model["encoder"])
    except (ValueError, UnicodeError, OverflowError, TypeError) as exc:
        raise LatentError("Invalid encoder or provenance: " + str(exc)) from exc
    if encoder["config"] != CONFIG or encoder["vocabulary"] != VOCABULARY:
        raise LatentError("Encoder must use the fixed nibble vocabulary and configuration")
    if (encoder["provenance"]["training_status"] != expected_status
            or encoder["provenance"]["seed"] != provenance["seed"]):
        raise LatentError("Encoder initialization/training provenance disagrees with its wrapper")
    _keys(model["decoder"], ("input", "output"), "decoder")
    _dense_validate(model["decoder"]["input"], 3, 8, "decoder input")
    _dense_validate(model["decoder"]["output"], 8, 256, "decoder output")
    # Encode the validated independent encoder rather than retaining caller aliases.
    independent = {**model, "encoder": encoder}
    try:
        raw = _canonical(independent)
    except (ValueError, UnicodeError, TypeError, RecursionError) as exc:
        raise LatentError("Model is not canonical JSON data") from exc
    if len(raw) > MAX_MODEL_BYTES:
        raise LatentError("Latent model exceeds its serialized byte bound")
    return json.loads(raw)


def initialize(seed=7):
    """Create fresh UNTRAINED encoder and decoder; never load existing weights."""
    if type(seed) is not int or not 0 <= seed < 2**32:
        raise LatentError("seed must be an integer from 0 through 2**32-1")
    encoder = tf.initialize_model(VOCABULARY, seed=seed, width=8, heads=2,
                                  layers=1, max_tokens=16, ff_width=16)
    rng = random.Random(seed ^ _DECODER_SEED_XOR)

    def dense(inputs, outputs):
        return {"weight": [[rng.gauss(0, 1 / math.sqrt(inputs)) for _ in range(outputs)]
                           for _ in range(inputs)], "bias": [0.0] * outputs}

    return validate({"schema": SCHEMA, "architecture": ARCHITECTURE,
                     "provenance": {"training_status": "UNTRAINED",
                                    "origin": "fresh_seeded_encoder_and_decoder_v1",
                                    "seed": seed, "updates": 0},
                     "encoder": encoder,
                     "decoder": {"input": dense(3, 8), "output": dense(8, 256)},
                     "quantizer": dict(QUANTIZER), "commitment_beta": COMMITMENT_BETA})


def model_sha256(model):
    return hashlib.sha256(_canonical(validate(model))).hexdigest()


def _tokens(raw):
    if type(raw) is not bytes or len(raw) != BLOCK_BYTES:
        raise LatentError("Supply exactly eight bytes; partial blocks are not padded")
    return [nibble for byte in raw for nibble in (byte >> 4, byte & 15)]


def _coordinates(q):
    if type(q) not in (tuple, list) or len(q) != 3:
        raise LatentError("Supply exactly three quantized coordinates")
    if any(type(value) is not int or not -8 <= value <= 7 for value in q):
        raise LatentError("Coordinates must be signed four-bit integers")
    return tuple(q)


def _quantize(value):
    number = training._number(value)
    if not math.isfinite(number):
        raise LatentError("Nonfinite encoder coordinate")
    # Clip first to avoid huge-integer conversion. This has identical results
    # to floor(z+0.5) then clipping for every finite real input.
    result = math.floor(min(7.0, max(-8.0, number)) + 0.5)
    if isinstance(value, training._Value):
        derivative = float(-8.0 < number < 7.0)
        return value.tape.node(result, ((value, derivative),))
    return result


def _leaky_relu(value):
    number = training._number(value)
    derivative = 1.0 if number > 0 else LEAKY_SLOPE
    result = number * derivative
    if isinstance(value, training._Value):
        return value.tape.node(result, ((value, derivative),))
    return result


def _decoder_logits(model, q):
    hidden = [_leaky_relu(value) for value in training._dense([q], model["decoder"]["input"])[0]]
    flat = training._dense([hidden], model["decoder"]["output"])[0]
    return [flat[offset:offset + 16] for offset in range(0, 256, 16)]


def _predicted(logits):
    if any(not math.isfinite(training._number(value)) for row in logits for value in row):
        raise LatentError("Nonfinite reconstruction logits")
    return [max(range(16), key=lambda token: training._number(row[token])) for row in logits]


def _pack_nibbles(nibbles):
    return bytes((nibbles[index] << 4) | nibbles[index + 1] for index in range(0, 16, 2))


def encode_block(model, raw8):
    """Encode one full block to three integers; no residual is made here."""
    tokens = _tokens(raw8)
    model = validate(model)
    logits, _ = tf._forward(model["encoder"], tokens)
    return tuple(_quantize(value) for value in logits[-1][:3])


def decoder_block(model, q):
    """Float-network diagnostic; deploy a pinned compiled table for exact use."""
    q = _coordinates(q)
    model = validate(model)
    return _pack_nibbles(_predicted(_decoder_logits(model, q)))


def compile_decoder(model, *, max_seconds=60):
    """Return all 4,096 eight-byte reconstructions, with no partial result.

    Code order: ((q0+8)<<8)|((q1+8)<<4)|(q2+8). The returned table is the
    portable decoder artifact; its build may depend on floating-point hardware.
    Once its bytes are pinned, lookup and residual reconstruction need no floats.
    """
    budget = training._Budget(max_seconds)
    model = validate(model)
    table = bytearray()
    try:
        for code in range(4096):
            budget.check()
            q = ((code >> 8) - 8, ((code >> 4) & 15) - 8, (code & 15) - 8)
            table.extend(_pack_nibbles(_predicted(_decoder_logits(model, q))))
        budget.check()
    except training.TrainingTimeout as exc:
        raise LatentTimeout(str(exc)) from exc
    return bytes(table)


def _locations(model):
    for path, parent, index in training._locations(model["encoder"], "full", [0, 1, 2]):
        yield "encoder/" + path, parent, index
    for layer in ("input", "output"):
        state = model["decoder"][layer]
        for row_index, row in enumerate(state["weight"]):
            for column in range(len(row)):
                yield f"decoder/{layer}/weight/{row_index}/{column}", row, column
        for column in range(len(state["bias"])):
            yield f"decoder/{layer}/bias/{column}", state["bias"], column


def parameter_map(model):
    """Independent values for the trainable encoder/decoder parameter contract."""
    model = validate(model)
    return {path: float(parent[index]) for path, parent, index in _locations(model)}


def _objective(model, tokens, budget):
    z = training._forward(model["encoder"], tokens, budget)[-1][:3]
    q = [_quantize(value) for value in z]
    logits = _decoder_logits(model, q)
    reconstruction = sum(training._loss(row, target, list(range(16)))
                         for row, target in zip(logits, tokens)) / 16
    commitment = COMMITMENT_BETA * sum((value - training._number(coordinate)) ** 2
                                     for value, coordinate in zip(z, q)) / 3
    return reconstruction + commitment, reconstruction, commitment, z, q, logits


def loss_and_gradients(model, raw8, *, max_seconds=30):
    """Joint full-block loss and surrogate gradients; caller state unchanged.

    The report can be consumed by apply_gradients only at this exact model hash.
    No parameter update or corpus evaluation occurs in this operation.
    """
    tokens = _tokens(raw8)
    budget = training._Budget(max_seconds)
    state = validate(model)
    initial_hash = hashlib.sha256(_canonical(state)).hexdigest()
    tape = training._Tape(budget)
    try:
        parameters = {}
        for path, parent, index in _locations(state):
            parent[index] = parameters[path] = tape.node(parent[index])
        loss, reconstruction, commitment, z, q, logits = _objective(state, tokens, budget)
        tape.backward(loss)
        predicted = _predicted(logits)
        gradients = {path: value.grad for path, value in parameters.items()}
        norm = math.hypot(*gradients.values())
        if not math.isfinite(norm):
            raise LatentError("Nonfinite global gradient norm")
        budget.check()
        wall, cpu = budget.elapsed()
        return {"schema": "crystal-latent-gradients/v1", "model_sha256": initial_hash,
                "block_sha256": hashlib.sha256(raw8).hexdigest(),
                "loss": loss.data, "reconstruction_loss": training._number(reconstruction),
                "commitment_loss": training._number(commitment),
                "latent_unquantized": [training._number(value) for value in z],
                "coordinates": [int(training._number(value)) for value in q],
                "predicted_nibbles": predicted,
                "nibble_mismatches": sum(a != b for a, b in zip(predicted, tokens)),
                "gradients": gradients, "gradient_norm": norm, "graph_nodes": len(tape.nodes),
                "wall_seconds": wall, "cpu_seconds": cpu,
                "gradient_contract": QUANTIZER["surrogate"],
                "semantic_validation": "NOT_ESTABLISHED", "authority": "NONE"}
    except training.TrainingTimeout as exc:
        raise LatentTimeout(str(exc)) from exc
    except (training.TrainingError, OverflowError, ZeroDivisionError) as exc:
        raise LatentError("Gradient computation failed: " + str(exc)) from exc
    finally:
        tape.nodes.clear()


def apply_gradients(model, gradient_report, *, learning_rate):
    """Apply one atomic clipped SGD update from an exact-model-bound report.

    This is an update mechanism, not an admitted fit or a training controller.
    A future caller must separately bound its examples, update schedule and
    total process resources. No in-place mutation occurs, even on failure.
    """
    _number(learning_rate, "learning_rate")
    if not 0 < learning_rate <= 1:
        raise LatentError("learning_rate must be positive and at most 1")
    state = validate(model)
    original_hash = hashlib.sha256(_canonical(state)).hexdigest()
    if (type(gradient_report) is not dict
            or gradient_report.get("schema") != "crystal-latent-gradients/v1"
            or gradient_report.get("model_sha256") != original_hash):
        raise LatentError("Gradient report is not bound to this exact model")
    if state["provenance"]["updates"] >= MAX_UPDATES:
        raise LatentError("The cumulative update bound is exhausted")
    locations = list(_locations(state))
    gradients = gradient_report.get("gradients")
    if (type(gradients) is not dict or len(gradients) != len(locations)
            or set(gradients) != {path for path, _, _ in locations}):
        raise LatentError("Gradient parameter set differs")
    for value in gradients.values():
        try:
            valid = type(value) in (int, float) and math.isfinite(value)
        except OverflowError:
            valid = False
        if not valid:
            raise LatentError("Gradients must be finite numbers, not bool")
    norm = math.hypot(*gradients.values())
    if not math.isfinite(norm):
        raise LatentError("Nonfinite global gradient norm")
    scale = training.GRADIENT_CLIP / norm if norm > training.GRADIENT_CLIP else 1.0
    updates = [parent[index] - learning_rate * gradients[path] * scale
               for path, parent, index in locations]
    for value in updates:
        _number(value, "updated parameter")
    for (_, parent, index), value in zip(locations, updates):
        parent[index] = value
    state["provenance"] = {"training_status": "UNVERIFIED",
                           "origin": "single_atomic_clipped_sgd; source_sha256=" + original_hash,
                           "seed": state["provenance"]["seed"],
                           "updates": state["provenance"]["updates"] + 1}
    state["encoder"]["provenance"] = {"training_status": "UNVERIFIED",
                                      "origin": "joint_latent_reconstruction_surrogate_update",
                                      "seed": state["encoder"]["provenance"]["seed"]}
    state = validate(state)
    return state, {"schema": "crystal-latent-update/v1", "initial_model_sha256": original_hash,
                   "final_model_sha256": model_sha256(state), "updates_applied": 1,
                   "gradient_norm": norm, "gradient_clip_norm": training.GRADIENT_CLIP,
                   "clipped": scale < 1, "learning_rate": learning_rate,
                   "training_status": "UNVERIFIED", "semantic_validation": "NOT_ESTABLISHED"}
