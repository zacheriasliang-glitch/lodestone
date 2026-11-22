"""Loom: bounded advisory predictions bound to explicit Bezoar context.

Full prose stays in the context/fold. Integer feature extraction is declared
caller work, not a capability smuggled into the tiny transformer. Predictions
never promote a SEED, override a review, or authorize an action.
"""
from __future__ import annotations

import json
import math

from . import transformer as tf
from .records import canonical
from .loom_context import sha, validate_context

REQUEST_SCHEMA = "loom-advisory-request/v1"
SCORE_MEANING = "Uncalibrated restricted-softmax; not truth, competence or execution permission"
FEATURE_EXTRACTION = "CALLER_DECLARED; not learned or independently verified by this adapter"
READER_OPTIONS = "Choose, contribute an unlisted possibility, revise the question, or stop when its purpose is met"
ADVICE_FIELDS = {"schema", "model_sha256", "model_training_status", "request_sha256",
                 "context_sha256", "purpose", "scores", "highest_scoring_candidate",
                 "score_meaning", "feature_extraction", "feature_bindings", "unresolved",
                 "reader_options", "seed_promotion", "semantic_validation", "authority", "activation"}


def _text(value, limit, name):
    if type(value) is not str or not value.strip() or len(value.encode()) > limit:
        raise ValueError(f"{name} must contain 1..{limit} UTF-8 bytes")


def _pin(value, name):
    if type(value) is not str or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")


def _copy_json(value, limit, name):
    try:
        raw = canonical(value)
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError(f"{name} must be finite bounded JSON") from exc
    if len(raw) > limit:
        raise ValueError(f"{name} exceeds its {limit}-byte bound")
    return json.loads(raw)


def _candidate(candidate, handles):
    if type(candidate) is not dict or set(candidate) != {"id", "description", "context_handles"}:
        raise ValueError("Candidate needs id, description and context_handles")
    _text(candidate["id"], 256, "candidate id")
    _text(candidate["description"], 8192, "candidate description")
    refs = candidate["context_handles"]
    if (type(refs) is not list or not refs or any(type(h) is not str or h not in handles for h in refs)
            or len(set(refs)) != len(refs)):
        raise ValueError("Candidate references must select actual compact-context records")


def _feature_bindings(bindings, handles, *, token_count=None):
    """Validate declared bindings; complete coverage requires the original tokens."""
    if type(bindings) is not list or not 1 <= len(bindings) <= 32:
        raise ValueError("Declare feature interpretation and source bindings")
    covered = set()
    upper_bound = 16 if token_count is None else token_count
    for binding in bindings:
        if type(binding) is not dict or set(binding) != {"context_handle", "positions", "interpretation"}:
            raise ValueError("Invalid feature binding")
        if type(binding["context_handle"]) is not str or binding["context_handle"] not in handles:
            raise ValueError("Feature references a record absent from context")
        _text(binding["interpretation"], 2048, "feature interpretation")
        positions = binding["positions"]
        if (type(positions) is not list or not positions
                or any(type(i) is not int or not 0 <= i < upper_bound for i in positions)
                or len(set(positions)) != len(positions)):
            raise ValueError("Invalid bound token positions")
        covered.update(positions)
    if token_count is not None and covered != set(range(token_count)):
        raise ValueError("Every encoded token needs a declared provenance/interpretation binding")


def advise(model, request, context):
    """Score supplied candidate slots, retaining interpretation and provenance.

    Unlisted contributions remain available to the later reader. Scores are
    uncalibrated restricted-softmax values, not probabilities that a lesson is
    true or that an action will succeed. This performs one local forward pass.
    """
    model = tf.validate_model(model)
    config = model["config"]
    if (config["width"] > 8 or config["layers"] > 1 or config["max_tokens"] > 16
            or config["ff_width"] > 16 or len(model["vocabulary"]) > 128):
        raise ValueError("Loom advisory model exceeds the tiny model bounds")
    context = validate_context(context)
    required = {"schema", "purpose", "context_sha256", "token_ids", "candidates",
                "feature_bindings", "unresolved", "model_sha256"}
    if type(request) is not dict or set(request) != required or request["schema"] != REQUEST_SCHEMA:
        raise ValueError("Invalid Loom advisory request fields/schema")
    request = _copy_json(request, 65536, "Advisory request")
    _pin(request["model_sha256"], "Model pin")
    if request["model_sha256"] != tf.model_sha256(model):
        raise ValueError("Advisory request belongs to a different model")
    _pin(request["context_sha256"], "Context pin")
    _text(request["purpose"], 2048, "purpose")
    if request["context_sha256"] != sha(canonical(context)):
        raise ValueError("Advisory request belongs to different compact context")
    if context.get("authority") != "NONE" or context.get("activation") != "NONE":
        raise ValueError("Context cannot supply execution authority")
    if request["purpose"] != context.get("purpose"):
        raise ValueError("Advisory purpose differs from selected context")
    tokens = request["token_ids"]
    if type(tokens) is not list or not 1 <= len(tokens) <= 16:
        raise ValueError("Loom receives 1..16 explicitly encoded tokens")
    tf._token_ids(tokens, model)
    candidates = request["candidates"]
    if type(candidates) is not list or not 2 <= len(candidates) <= 16:
        raise ValueError("Provide 2..16 explicit candidates")
    handles = {entry["handle"] for entry in context["entries"]}
    actions, seen = [], set()
    for candidate in candidates:
        _candidate(candidate, handles)
        name = candidate["id"]
        if name in seen or name not in model["vocabulary"]:
            raise ValueError("Candidate IDs must be distinct model-vocabulary slots")
        seen.add(name)
        actions.append(model["vocabulary"].index(name))
    _feature_bindings(request["feature_bindings"], handles, token_count=len(tokens))
    if type(request["unresolved"]) is not list or len(request["unresolved"]) > 32:
        raise ValueError("Unresolved contributions must be an explicit list")
    for item in request["unresolved"]:
        _text(item, 8192, "unresolved contribution")
    logits = tf.forward(model, tokens)["logits"][-1]
    maximum = max(logits[i] for i in actions)
    weights = [math.exp(logits[i] - maximum) for i in actions]
    total = sum(weights)
    scores = [{"candidate": candidate, "score": value / total}
              for candidate, value in zip(candidates, weights)]
    return {"schema": "loom-advice/v1", "model_sha256": tf.model_sha256(model),
            "model_training_status": model["provenance"]["training_status"],
            "request_sha256": sha(canonical(request)), "context_sha256": request["context_sha256"],
            "purpose": request["purpose"], "scores": scores,
            "highest_scoring_candidate": scores[max(range(len(scores)), key=lambda i: scores[i]["score"])]["candidate"]["id"],
            "score_meaning": SCORE_MEANING,
            "feature_extraction": FEATURE_EXTRACTION,
            "feature_bindings": request["feature_bindings"], "unresolved": request["unresolved"],
            "reader_options": READER_OPTIONS,
            "seed_promotion": "NONE", "semantic_validation": "NOT_ESTABLISHED",
            "authority": "NONE", "activation": "NONE"}


def reader_packet(fold, context, advice, *, expected_fold_sha256):
    """An explicit new reader input, not the historical fold-only exam arm.

    Binding structure and context handles are checked here. The original request
    is absent, so its token coverage and digest cannot be independently verified.
    """
    _pin(expected_fold_sha256, "Complete fold pin")
    fold = _copy_json(fold, 1024 * 1024, "Complete fold")
    if type(fold) is not dict or sha(canonical(fold)) != expected_fold_sha256:
        raise ValueError("Complete fold differs from pinned bytes")
    context = validate_context(context)
    advice = _copy_json(advice, 131072, "Loom advice")
    if type(advice) is not dict or set(advice) != ADVICE_FIELDS:
        raise ValueError("Loom advice must have the exact declared fields")
    if advice.get("schema") != "loom-advice/v1" or advice.get("context_sha256") != sha(canonical(context)):
        raise ValueError("Advice belongs to different context")
    if advice.get("authority") != "NONE" or advice.get("activation") != "NONE":
        raise ValueError("Advice cannot authorize execution")
    for key in ("model_sha256", "request_sha256"):
        _pin(advice.get(key), key)
    if (advice.get("purpose") != context["purpose"] or advice.get("seed_promotion") != "NONE"
            or advice.get("semantic_validation") != "NOT_ESTABLISHED"
            or advice.get("model_training_status") not in ("UNTRAINED", "UNVERIFIED")):
        raise ValueError("Advice changes its purpose, evidence status or seed state")
    for field, expected in (("score_meaning", SCORE_MEANING), ("feature_extraction", FEATURE_EXTRACTION),
                            ("reader_options", READER_OPTIONS)):
        if type(advice[field]) is not str or advice[field] != expected:
            raise ValueError(f"Advice changes the declared {field}")
    scores = advice.get("scores")
    if type(scores) is not list or not 2 <= len(scores) <= 16:
        raise ValueError("Advice needs 2..16 explicit candidate scores")
    handles, ids, total = {entry["handle"] for entry in context["entries"]}, set(), 0.0
    _feature_bindings(advice["feature_bindings"], handles)
    for item in scores:
        if type(item) is not dict or set(item) != {"candidate", "score"}:
            raise ValueError("Malformed candidate score")
        _candidate(item["candidate"], handles)
        value, identity = item["score"], item["candidate"]["id"]
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1 or identity in ids:
            raise ValueError("Invalid or duplicate candidate score")
        ids.add(identity)
        total += value
    if not math.isclose(total, 1.0, rel_tol=0, abs_tol=1e-9):
        raise ValueError("Candidate scores must sum to one")
    highest = max(scores, key=lambda item: item["score"])["candidate"]["id"]
    if advice.get("highest_scoring_candidate") != highest:
        raise ValueError("Highest-scoring candidate differs from supplied scores")
    unresolved = advice.get("unresolved")
    if type(unresolved) is not list or len(unresolved) > 32:
        raise ValueError("Advice must preserve explicit unresolved contributions")
    for item in unresolved:
        _text(item, 8192, "unresolved contribution")
    result = {"schema": "loom-reader-packet/v1", "fold": fold, "fold_sha256": expected_fold_sha256,
              "bezoar_context": context, "loom_advice": advice,
              "exposure": "Complete fold plus explicit compact Bezoar context and advisory scores; not a fold-only control",
              "original_sources_reopened": False,
              "source_content": "Supplied fold/context may themselves contain source-derived text",
              "advice_validation": "Structural bindings/status only; original request absent, so token coverage and request digest are not independently verified",
              "execution_isolation": "NOT_ENFORCED_BY_PACKET", "authority": "NONE", "activation": "NONE"}
    if len(canonical(result)) > 1024 * 1024:
        raise ValueError("Complete reader packet exceeds 1 MiB; no silent truncation")
    return result
