"""Would-probe: where a reader's balance sits, base versus adapter.

Greedy decoding keeps only the top token, so two arms can print identical text
while the balance underneath has moved. This probe scores a fixed set of
candidate continuations by their total log-probability (one forward pass per
candidate, logits kept only for the candidate positions), turns the scores into
a balance over the candidates, and compares arms by KL divergence, per-candidate
shift and leading-candidate change. There is no generation, no output cap and
no answer format to obey. Candidates end with EOS, so "say this and stop" is
what gets scored.

A balance restricted to chosen candidates is not the model's whole distribution;
results describe these candidates only. Scores are measurements, not a
semantic grade, and they never activate or promote anything.
"""
from __future__ import annotations

import hashlib
import math
import re
from contextlib import nullcontext

from .records import canonical

SCHEMA = "loom-would-probe/v1"
MAX_CANDIDATES = 16
MAX_CANDIDATE_TOKENS = 256


def _sha(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def balance(scores):
    """Softmax over total log-probabilities: the would, restricted to candidates."""
    if type(scores) is not dict or len(scores) < 2:
        raise ValueError("a balance needs at least two scored candidates")
    top = max(scores.values())
    weights = {key: math.exp(value - top) for key, value in scores.items()}
    total = sum(weights.values())
    return {key: value / total for key, value in weights.items()}


def kl_bits(p, q):
    """KL(p || q) in bits over the same candidate set."""
    if set(p) != set(q):
        raise ValueError("balances must cover the same candidates")
    return sum(p[k] * math.log2(p[k] / q[k]) for k in p if p[k] > 0)


def _leading(values):
    top = max(values.values())
    leaders = [key for key, value in values.items() if value == top]
    return leaders[0] if len(leaders) == 1 else None


def compare(base, adapter):
    """How far the adapter moved the balance; identical text can hide this."""
    shifts = {key: adapter[key] - base[key] for key in base}
    return {"kl_bits_base_to_adapter": kl_bits(base, adapter),
            "max_abs_shift": max(abs(value) for value in shifts.values()),
            "shifts": shifts, "leading_base": _leading(base), "leading_adapter": _leading(adapter),
            "leading_changed": _leading(base) != _leading(adapter)}


def probe_with(score_fn, prompt_ids, candidates):
    """Score each candidate continuation after the same prompt."""
    if type(candidates) is not list or not 2 <= len(candidates) <= MAX_CANDIDATES:
        raise ValueError("probe needs 2..16 candidates")
    ids = [row["id"] for row in candidates]
    if len(set(ids)) != len(ids) or any(type(i) is not str or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", i)
                                        for i in ids):
        raise ValueError("candidate ids must be unique bounded strings")
    scores, detail = {}, {}
    for row in candidates:
        tokens = row["token_ids"]
        if type(tokens) is not list or not 1 <= len(tokens) <= MAX_CANDIDATE_TOKENS:
            raise ValueError("candidate token count out of bounds")
        total = float(score_fn(list(prompt_ids), list(tokens)))
        if not math.isfinite(total) or total > 0:
            raise ValueError("score must be a finite log-probability")
        scores[row["id"]] = total
        detail[row["id"]] = {"logprob": total, "tokens": len(tokens), "mean_logprob": total / len(tokens)}
    return {"scores": detail, "balance": balance(scores)}


def torch_score_fn(adapter, *, adapter_enabled):
    """Sum of candidate-token log-probabilities in one forward pass (CPU, no cache)."""
    from . import loom_adapter_torch as backend
    torch = backend.torch_module()

    def score(prompt_ids, cont_ids):
        ids = torch.tensor([prompt_ids + cont_ids], dtype=torch.long)
        mode = nullcontext() if adapter_enabled else adapter.bundle.baseline()
        with mode, torch.inference_mode():
            # Keep logits only for the positions that predict candidate tokens.
            out = adapter.model(input_ids=ids, use_cache=False, logits_to_keep=len(cont_ids) + 1,
                                return_dict=True)
            logits = out.logits[0, :len(cont_ids)].float()
            logp = torch.log_softmax(logits, dim=-1)
            picked = logp[torch.arange(len(cont_ids)), torch.tensor(cont_ids, dtype=torch.long)]
            return float(picked.sum())
    return score


def run_probe(adapter, request, candidates, *, max_total_tokens=2048, score_factory=torch_score_fn):
    """Base and adapter balances over text candidates for one Loom request."""
    from .loom_adapter import encode_prediction
    tokenizer = adapter.tokenizer
    markers = getattr(tokenizer, "all_special_tokens", [])
    rows = []
    for row in candidates:
        if type(row) is not dict or set(row) != {"id", "text"} or type(row["text"]) is not str \
                or not row["text"].strip():
            raise ValueError("each candidate needs exactly id and nonempty text")
        if any(marker in row["text"] for marker in markers):
            raise ValueError("candidate text contains a tokenizer control token")
        tokens = tokenizer.encode(row["text"], add_special_tokens=False) + [tokenizer.eos_token_id]
        rows.append({"id": row["id"], "token_ids": tokens})
    longest = max(len(row["token_ids"]) for row in rows)
    encoded = encode_prediction(request, tokenizer, max_total_tokens=max_total_tokens,
                                max_new_tokens=max(longest, 1))
    prompt = encoded["input_ids"]
    base = probe_with(score_factory(adapter, adapter_enabled=False), prompt, rows)
    tuned = probe_with(score_factory(adapter, adapter_enabled=True), prompt, rows)
    return {"schema": SCHEMA, "prompt_tokens": len(prompt), "prompt_tokens_sha256": _sha(prompt),
            "candidates_sha256": _sha(candidates), "candidates": [row["id"] for row in rows],
            "base": base, "adapter": tuned, "comparison": compare(base["balance"], tuned["balance"]),
            "scope": "Balance over the listed candidates only; measurement, not a grade.",
            "generation_calls": 0, "weight_updates": 0}
