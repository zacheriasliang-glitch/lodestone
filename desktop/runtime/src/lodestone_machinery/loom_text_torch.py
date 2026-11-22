"""Optional, bounded CPU text classifier. Importing does not load a model.

One fit accepts only training rows. Development/test evaluation is a separate
explicit call. Checkpoints are strictly shaped finite JSON, never pickle.
"""
from __future__ import annotations

import math
import random
import time

from . import loom_text as text
from .records import canonical

CONFIG = {"vocabulary": 259, "max_tokens": 772, "width": 32, "heads": 4,
          "layers": 1, "ff_width": 64, "dropout": 0, "pooling": "masked_mean", "classes": 2}
MAX_UPDATES, BATCH_SIZE, LEARNING_RATE, MAX_SECONDS = 400, 4, 0.001, 300
DEFAULT_SEED = 7
_THREADS_CONFIGURED = False


def _torch():
    global _THREADS_CONFIGURED
    try:
        import torch
    except ImportError as exc:
        raise RuntimeError("Optional existing PyTorch 2.2.2 environment required; no automatic installation") from exc
    if torch.__version__.split("+")[0] != "2.2.2":
        raise RuntimeError("This experimental backend requires PyTorch 2.2.2")
    if not _THREADS_CONFIGURED:
        torch.set_num_threads(2)
        try:
            torch.set_num_interop_threads(1)
        except RuntimeError:
            if torch.get_num_interop_threads() != 1:
                raise RuntimeError("Start a fresh process to enforce one interop thread")
        _THREADS_CONFIGURED = True
    return torch


def initialize(*, seed=DEFAULT_SEED):
    """Create the fixed <100k-parameter model; caller controls any invocation."""
    if type(seed) is not int or not 0 <= seed < 2 ** 31:
        raise ValueError("seed must be an integer in [0, 2**31)")
    torch = _torch()

    class Classifier(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.tokens = torch.nn.Embedding(259, 32, padding_idx=text.PAD)
            self.positions = torch.nn.Embedding(772, 32)
            self.block = torch.nn.TransformerEncoderLayer(32, 4, 64, dropout=0,
                activation="gelu", batch_first=True, norm_first=True)
            self.norm = torch.nn.LayerNorm(32)
            self.head = torch.nn.Linear(32, 2)
            torch.nn.init.normal_(self.tokens.weight, std=0.02)
            torch.nn.init.normal_(self.positions.weight, std=0.02)
            with torch.no_grad():
                self.tokens.weight[text.PAD].zero_()

        def forward(self, ids):
            if ids.dtype != torch.long or ids.ndim != 2 or not 1 <= ids.shape[0] <= BATCH_SIZE or not 1 <= ids.shape[1] <= 772:
                raise ValueError("Token tensor shape, batch, or dtype exceeds the fixed contract")
            if bool(((ids < 0) | (ids >= 259)).any()) or bool((ids[:, 0] != text.CLS).any()):
                raise ValueError("Invalid token IDs or missing CLS")
            keep = ids != text.PAD
            if bool((keep[:, 1:] & ~keep[:, :-1]).any()):
                raise ValueError("Padding must be a contiguous suffix")
            hidden = self.tokens(ids) + self.positions(torch.arange(ids.shape[1], device=ids.device))
            hidden = self.norm(self.block(hidden, src_key_padding_mask=~keep))
            pooled = (hidden * keep.unsqueeze(-1)).sum(1) / keep.sum(1, keepdim=True)
            return self.head(pooled)

    with torch.random.fork_rng(devices=[]):
        torch.manual_seed(seed)
        model = Classifier().cpu()
    if sum(parameter.numel() for parameter in model.parameters()) >= 100000:
        raise ValueError("Model exceeds fixed parameter budget")
    return model


def checkpoint(model, *, seed=DEFAULT_SEED, training_status="UNTRAINED"):
    if training_status not in ("UNTRAINED", "FIT_UNVERIFIED"):
        raise ValueError("Unsupported training status")
    state = {name: value.detach().cpu().tolist() for name, value in model.state_dict().items()}
    result = {"schema": "loom-text-checkpoint/v1", "config": dict(CONFIG), "seed": seed,
              "training_status": training_status, "state": state}
    # The same loader validates numerical finiteness and every tensor shape.
    _validate_checkpoint(result)
    return result


def _validate_checkpoint(value):
    text._keys(value, {"schema", "config", "seed", "training_status", "state"}, "checkpoint")
    if value["schema"] != "loom-text-checkpoint/v1" or canonical(value["config"]) != canonical(CONFIG):
        raise ValueError("Checkpoint architecture differs from the fixed model")
    if value["training_status"] not in ("UNTRAINED", "FIT_UNVERIFIED"):
        raise ValueError("Invalid checkpoint training status")
    if type(value["seed"]) is not int or not 0 <= value["seed"] < 2 ** 31:
        raise ValueError("Invalid checkpoint seed")
    if len(canonical(value)) > text.MAX_DOCUMENT_BYTES:
        raise ValueError("Checkpoint exceeds 4 MiB")
    model = initialize(seed=value["seed"])
    expected = model.state_dict()
    text._keys(value["state"], expected.keys(), "checkpoint state")
    torch = _torch()
    tensors = {}

    def check(data, shape):
        if shape:
            if type(data) is not list or len(data) != shape[0]:
                raise ValueError("Checkpoint tensor shape differs")
            for item in data:
                check(item, shape[1:])
        elif type(data) not in (int, float) or abs(data) > 1e6 or not math.isfinite(data):
            raise ValueError("Checkpoint weights must be finite numbers with magnitude <= 1e6")

    for name, tensor in expected.items():
        check(value["state"][name], tuple(tensor.shape))
        tensors[name] = torch.tensor(value["state"][name], dtype=torch.float32)
    return model, tensors


def load_checkpoint(value):
    model, state = _validate_checkpoint(value)
    model.load_state_dict(state, strict=True)
    model.eval()
    return model


def _batch(rows, *, allow_empty_source=False):
    torch = _torch()
    sequences = [text.encode_text(row["source_text"], row["query"], row["candidate"],
                                  allow_empty_source=allow_empty_source) for row in rows]
    length = max(map(len, sequences))
    return torch.tensor([sequence + [text.PAD] * (length - len(sequence)) for sequence in sequences], dtype=torch.long)


def _score(model, rows, *, allow_empty_source=False):
    torch = _torch()
    model.eval()
    result = []
    with torch.inference_mode():
        for start in range(0, len(rows), BATCH_SIZE):
            logits = model(_batch(rows[start:start + BATCH_SIZE], allow_empty_source=allow_empty_source))
            if not bool(torch.isfinite(logits).all()):
                raise ValueError("Nonfinite evaluation logits")
            probabilities = torch.softmax(logits, dim=-1)
            for row, raw, probability in zip(rows[start:start + BATCH_SIZE], logits.tolist(), probabilities.tolist()):
                prediction = max(range(2), key=probability.__getitem__)
                item = {"id": row["id"], "logits": raw, "probabilities": probability, "prediction": prediction,
                        "prediction_meaning": text.LABELS[prediction]}
                if "provenance" in row:
                    item["provenance"] = row["provenance"]
                if "label" in row:
                    item["label"] = row["label"]
                result.append(item)
    return result


def train(train_examples, sources, records, *, steps=MAX_UPDATES, seed=DEFAULT_SEED, max_seconds=MAX_SECONDS):
    """One fresh fit, training split only; outer process limits are also required."""
    if type(steps) is not int or not 1 <= steps <= MAX_UPDATES:
        raise ValueError("steps must be 1..400")
    if type(max_seconds) not in (int, float) or not math.isfinite(max_seconds) or not 0 < max_seconds <= MAX_SECONDS:
        raise ValueError("max_seconds must be finite and in (0, 300]")
    wall_start, cpu_start = time.monotonic(), time.process_time()
    rows = text.validate_dataset(train_examples, sources, records)
    torch = _torch()
    model = initialize(seed=seed)
    initial = checkpoint(model, seed=seed)
    optimizer = torch.optim.AdamW(model.parameters(), lr=LEARNING_RATE, foreach=False)
    rng = random.Random(seed)
    losses, gradient_norms, completed = [], [], 0
    model.train()
    for step in range(steps):
        if max(time.monotonic() - wall_start, time.process_time() - cpu_start) >= max_seconds:
            break
        batch = [rows[rng.randrange(len(rows))] for _ in range(BATCH_SIZE)]
        optimizer.zero_grad(set_to_none=True)
        logits = model(_batch(batch))
        labels = torch.tensor([row["label"] for row in batch], dtype=torch.long)
        loss = torch.nn.functional.cross_entropy(logits, labels)
        if not bool(torch.isfinite(loss)):
            raise ValueError("Nonfinite training loss")
        loss.backward()
        for parameter in model.parameters():
            if parameter.grad is not None and not bool(torch.isfinite(parameter.grad).all()):
                raise ValueError("Nonfinite gradient")
        norm = torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0, error_if_nonfinite=True, foreach=False)
        optimizer.step()
        if any(not bool(torch.isfinite(parameter).all()) for parameter in model.parameters()):
            raise ValueError("Nonfinite updated parameters")
        losses.append(float(loss.detach()))
        gradient_norms.append(float(norm))
        completed = step + 1
    output = checkpoint(model, seed=seed, training_status="FIT_UNVERIFIED" if completed else "UNTRAINED")
    report = {"schema": "loom-text-training-report/v1", "split": "train",
              "status": "COMPLETED" if completed == steps else "TIME_LIMIT", "requested_updates": steps,
              "completed_updates": completed, "batch_size": BATCH_SIZE, "learning_rate": LEARNING_RATE,
              "gradient_clip": 1.0, "optimizer": "AdamW", "seed": seed, "max_seconds": max_seconds,
              "losses": losses, "gradient_norms_before_clip": gradient_norms,
              "initial_checkpoint_sha256": text.sha(canonical(initial)),
              "checkpoint_sha256": text.sha(canonical(output)), "dataset_sha256": text.sha(canonical(rows)),
              "initial_state_sha256": text.sha(canonical(initial["state"])),
              "state_sha256": text.sha(canonical(output["state"])),
              "changed_tensors": [name for name in initial["state"] if initial["state"][name] != output["state"][name]],
              "distribution": text.distribution(rows), "parameter_count": sum(p.numel() for p in model.parameters()),
              "wall_seconds": time.monotonic() - wall_start, "cpu_seconds": time.process_time() - cpu_start,
              "device": "cpu", "torch_version": torch.__version__, "threads": torch.get_num_threads(),
              "interop_threads": torch.get_num_interop_threads(), "budget_enforcement": "COOPERATIVE_BETWEEN_UPDATES_REQUIRES_OUTER_SUPERVISOR",
              **text.BOUNDARY}
    return output, report


def evaluate(checkpoint_value, examples, sources, records, *, split):
    """Explicit independent split evaluation; never updates model weights."""
    if split not in ("train", "dev", "test"):
        raise ValueError("split must be train, dev, or test")
    rows = text.validate_dataset(examples, sources, records)
    model = load_checkpoint(checkpoint_value)
    predictions = _score(model, rows)
    correct = sum(row["prediction"] == row["label"] for row in predictions)
    losses = []
    for row in predictions:
        logits, label = row["logits"], row["label"]
        peak = max(logits)
        losses.append(peak + math.log(sum(math.exp(value - peak) for value in logits)) - logits[label])
    return {"schema": "loom-text-evaluation/v1", "split": split, "count": len(rows), "correct": correct,
            "accuracy": correct / len(rows), "cross_entropy": sum(losses) / len(losses),
            "checkpoint_sha256": text.sha(canonical(checkpoint_value)), "dataset_sha256": text.sha(canonical(rows)),
            "distribution": text.distribution(rows), "predictions": predictions, **text.BOUNDARY}


def predict(checkpoint_value, request, sources, records):
    """One labeled-free advisory prediction bound to complete retained sources."""
    row = text.validate_example(request, sources, records, labeled=False)
    model = load_checkpoint(checkpoint_value)
    return {"schema": "loom-text-prediction/v1", "request": row,
            "request_sha256": text.sha(canonical(row)), "checkpoint_sha256": text.sha(canonical(checkpoint_value)),
            "score": _score(model, [row])[0], "score_interpretation": "UNCALIBRATED_BINARY_RECORD_SUPPORT_PREFERENCE",
            **text.BOUNDARY}


def score_ablation(checkpoint_value, raw_rows, *, ablation):
    """Raw control inputs have no authenticated provenance and no gold labels.

    Compare these preferences with verified-input scores elsewhere. Shuffled
    inputs do not inherit the original examples' support labels.
    """
    if ablation not in ("no-source", "shuffled-source"):
        raise ValueError("Expected an explicit no-source or shuffled-source ablation")
    if type(raw_rows) is not list or not 1 <= len(raw_rows) <= text.MAX_EXAMPLES:
        raise ValueError("Expected 1..2000 raw control rows")
    copied = []
    for row in raw_rows:
        text._keys(row, {"id", "source_text", "query", "candidate"}, "ablation row")
        text._text(row["id"], 128, "id")
        if ablation == "no-source" and row["source_text"] != "":
            raise ValueError("no-source ablation requires explicitly empty source text")
        text.encode_text(row["source_text"], row["query"], row["candidate"],
                         allow_empty_source=ablation == "no-source")
        copied.append(dict(row))
    model = load_checkpoint(checkpoint_value)
    return {"schema": "loom-text-ablation/v1", "ablation": ablation, "count": len(copied),
            "input_sha256": text.sha(canonical(copied)), "checkpoint_sha256": text.sha(canonical(checkpoint_value)),
            "predictions": _score(model, copied, allow_empty_source=ablation == "no-source"),
            "provenance_status": "UNVERIFIED_CONTROL_INPUTS", "gold_label_status": "NONE",
            **text.BOUNDARY}
