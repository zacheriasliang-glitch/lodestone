"""Bounded completion-only LoRA for an already loaded, pinned Qwen3-0.6B.

This module never loads model assets, downloads, opens a dataset, or grants run
authority. The caller verifies asset hashes and supervises the process. Numeric
unit checks do not establish useful learned relationships or task reliability.
"""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import math
import re
import time


SCHEMA = "loom-qwen-adapter/v1"
MAX_ROWS = 2048
MAX_STEPS = 4096
MAX_SEQUENCE_TOKENS = 8192
IDENTITY = {"model_type": "qwen3", "hidden_size": 1024,
            "num_hidden_layers": 28, "num_attention_heads": 16,
            "num_key_value_heads": 8, "intermediate_size": 3072,
            "head_dim": 128, "vocab_size": 151936,
            "max_position_embeddings": 40960, "tie_word_embeddings": True}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def _finite_number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def torch_module():
    import torch
    require(torch.__version__.split("+")[0] == "2.2.2", "Unreviewed torch version")
    torch.set_num_threads(2)
    # Match existing Loom backends: PyTorch permits setting this only once.
    if torch.get_num_interop_threads() > 1:
        try:
            torch.set_num_interop_threads(1)
        except RuntimeError as error:
            raise ValueError("Set the CPU interop thread limit before tensor work") from error
    return torch


@dataclass(frozen=True)
class AdapterConfig:
    rank: int = 4
    alpha: int = 8
    layers: tuple = (26, 27)

    def validate(self):
        require(type(self.rank) is int and self.rank == 4 and
                type(self.alpha) is int and self.alpha == 8 and
                type(self.layers) is tuple and self.layers == (26, 27) and
                all(type(x) is int for x in self.layers), "Unsupported adapter configuration")
        return self

    def document(self):
        result = asdict(self.validate())
        result["layers"] = list(self.layers)
        return result


def _linear_adapter(torch, base, config, generator):
    class LoRALinear(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.base = base
            self.a = torch.nn.Parameter(torch.empty(config.rank, base.in_features,
                                                    dtype=torch.float32, device="cpu"))
            self.b = torch.nn.Parameter(torch.zeros(base.out_features, config.rank,
                                                    dtype=torch.float32, device="cpu"))
            torch.nn.init.kaiming_uniform_(self.a, a=math.sqrt(5), generator=generator)
            self.scale = config.alpha / config.rank
            self.enabled = True

        def forward(self, inputs):
            output = self.base(inputs)
            if self.enabled:
                output = output + torch.nn.functional.linear(
                    torch.nn.functional.linear(inputs, self.a), self.b) * self.scale
            return output
    return LoRALinear()


class AdapterBundle:
    def __init__(self, model, modules, config, base_manifest_sha256):
        self.model, self.modules = model, modules
        self.config, self.base_manifest_sha256 = config, base_manifest_sha256

    def named_adapter_parameters(self):
        return {name + "." + part: getattr(module, part)
                for name, module in self.modules.items() for part in ("a", "b")}

    @contextmanager
    def baseline(self):
        """Disable deltas, preserving base weights and nested-context state."""
        previous = [(module, module.enabled) for module in self.modules.values()]
        try:
            for module, _ in previous:
                module.enabled = False
            yield self.model
        finally:
            for module, enabled in previous:
                module.enabled = enabled


def attach_adapters(model, config=None, *, base_manifest_sha256, seed=0):
    """Attach four zero-output adapters; all original parameters become frozen.

    ``base_manifest_sha256`` is custody metadata, not proof the caller actually
    verified files. Asset and tokenizer validation belong to the local loader.
    """
    config = AdapterConfig() if config is None else config
    require(type(config) is AdapterConfig, "AdapterConfig required")
    config.validate()
    require(type(seed) is int and 0 <= seed < 2 ** 31, "Invalid adapter seed")
    require(type(base_manifest_sha256) is str and
            re.fullmatch("[0-9a-f]{64}", base_manifest_sha256), "Pinned base manifest required")
    for key, expected in IDENTITY.items():
        actual = getattr(getattr(model, "config", None), key, None)
        require(type(actual) is type(expected) and actual == expected, "Qwen identity differs: " + key)
    torch = torch_module()
    require(len(model.model.layers) == 28, "Qwen layer count differs")
    require(all(p.device.type == "cpu" and p.dtype == torch.float32
                for p in model.parameters()), "CPU float32 base required")
    targets = {}
    for layer in config.layers:
        attention = model.model.layers[layer].self_attn
        for attr, out_features in (("q_proj", 2048), ("v_proj", 1024)):
            base = getattr(attention, attr)
            require(type(base) is torch.nn.Linear and base.in_features == 1024 and
                    base.out_features == out_features and base.bias is None,
                    "Projection shape/type differs or adapter already attached")
            targets[f"model.layers.{layer}.self_attn.{attr}"] = (attention, attr, base)
    # Validate every target before mutation; use a private RNG.
    generator = torch.Generator(device="cpu").manual_seed(seed)
    modules = {name: _linear_adapter(torch, base, config, generator)
               for name, (_, _, base) in targets.items()}
    for parameter in model.parameters():
        parameter.requires_grad_(False)
        parameter.grad = None
    for name, (attention, attr, _) in targets.items():
        setattr(attention, attr, modules[name])
    return AdapterBundle(model, modules, config, base_manifest_sha256)


def adapter_state(bundle):
    """Plain numeric JSON-compatible adapter tensors; no pickle or base copy."""
    torch = torch_module()
    values = {}
    for name, parameter in bundle.named_adapter_parameters().items():
        require(parameter.device.type == "cpu" and parameter.dtype == torch.float32 and
                bool(torch.isfinite(parameter).all()), "Invalid adapter tensor")
        values[name] = {"shape": list(parameter.shape), "values": parameter.detach().tolist()}
    return {"schema": SCHEMA, "base_manifest_sha256": bundle.base_manifest_sha256,
            "config": bundle.config.document(), "tensors": values}


def load_adapter_state(bundle, state):
    """Validate the complete payload before changing any adapter parameter."""
    require(type(state) is dict and set(state) == {
        "schema", "base_manifest_sha256", "config", "tensors"}, "Invalid adapter state fields")
    require(state["schema"] == SCHEMA and state["base_manifest_sha256"] == bundle.base_manifest_sha256,
            "Adapter base binding differs")
    config = state["config"]
    require(type(config) is dict and set(config) == {"rank", "alpha", "layers"} and
            type(config["rank"]) is int and type(config["alpha"]) is int and
            type(config["layers"]) is list and all(type(x) is int for x in config["layers"]) and
            config == bundle.config.document(), "Adapter configuration differs")
    parameters = bundle.named_adapter_parameters()
    require(type(state["tensors"]) is dict and set(state["tensors"]) == set(parameters),
            "Adapter tensor inventory differs")
    torch, prepared = torch_module(), {}
    for name, parameter in parameters.items():
        value = state["tensors"][name]
        shape = list(parameter.shape)
        require(type(value) is dict and set(value) == {"shape", "values"} and
                type(value["shape"]) is list and all(type(x) is int for x in value["shape"]) and
                value["shape"] == shape, "Adapter tensor shape differs")
        rows = value["values"]
        require(type(rows) is list and len(rows) == shape[0] and all(
            type(row) is list and len(row) == shape[1] and all(
                _finite_number(x) for x in row) for row in rows),
            "Invalid adapter numeric values")
        tensor = torch.tensor(rows, dtype=torch.float32, device="cpu")
        require(bool(torch.isfinite(tensor).all()), "Adapter values exceed float32")
        prepared[name] = tensor
    with torch.no_grad():
        for name, parameter in parameters.items():
            parameter.copy_(prepared[name])


def _training_rows(rows, max_sequence_tokens):
    require(type(rows) is list and 1 <= len(rows) <= MAX_ROWS, "Bounded training rows required")
    require(type(max_sequence_tokens) is int and 2 <= max_sequence_tokens <= MAX_SEQUENCE_TOKENS,
            "Invalid sequence-token bound")
    prepared = []
    for row in rows:
        require(type(row) is dict and set(row) == {"input_ids", "labels"}, "Training row fields differ")
        ids, labels = row["input_ids"], row["labels"]
        require(type(ids) is list and 2 <= len(ids) <= max_sequence_tokens and
                all(type(x) is int and 0 <= x < IDENTITY["vocab_size"] for x in ids),
                "Invalid input tokens or overlength sequence")
        require(type(labels) is list and len(labels) == len(ids) and
                all(type(x) is int for x in labels), "Invalid label tokens")
        first = next((i for i, x in enumerate(labels) if x != -100), len(labels))
        require(1 <= first < len(labels) and labels[first:] == ids[first:],
                "Only a contiguous exact completion may be supervised")
        prepared.append((tuple(ids), tuple(labels)))
    return prepared


def _completion_loss(torch, logits, targets, *, vocab_size=IDENTITY["vocab_size"]):
    """Causal loss from the predicting suffix, including its preceding position."""
    require(type(vocab_size) is int and vocab_size >= 2 and
            type(targets) in (tuple, list) and len(targets) > 0 and
            all(type(x) is int and 0 <= x < vocab_size for x in targets),
            "Invalid completion targets")
    require(isinstance(logits, torch.Tensor) and logits.device.type == "cpu" and
            logits.dtype == torch.float32 and
            tuple(logits.shape) == (1, len(targets) + 1, vocab_size),
            "Completion suffix logits shape/dtype differs")
    require(bool(torch.isfinite(logits).all()), "Nonfinite completion logits")
    # Last position predicts the unsupervised token after the complete target.
    predicting = logits[:, :-1, :].reshape(-1, vocab_size)
    return torch.nn.functional.cross_entropy(
        predicting, torch.tensor(targets, dtype=torch.long, device="cpu"), reduction="mean")


def train_completion(bundle, train_rows, *, max_steps, max_seconds, split="train",
                     max_sequence_tokens=1024, learning_rate=1e-4, max_grad_norm=1.0, seed=0,
                     on_completed_update=None):
    """Explicit bounded adapter updates. Caller must enforce outer CPU/RAM/wall caps.

    Train-only custody and source-family separation are caller responsibilities.
    Time is checked between updates; an individual forward/backward can overrun
    this cooperative bound. Final weights always remain scientifically unverified.
    """
    started = time.monotonic()
    require(split == "train", "Only the training split may update weights")
    require(type(max_steps) is int and 1 <= max_steps <= MAX_STEPS, "Invalid update bound")
    for value, upper, label in ((max_seconds, 3600, "wall"),
                                (learning_rate, 0.01, "learning rate"),
                                (max_grad_norm, 10, "gradient norm")):
        require(_finite_number(value) and 0 < value <= upper,
                "Invalid " + label + " bound")
    require(type(seed) is int and 0 <= seed < 2 ** 31, "Invalid training seed")
    require(on_completed_update is None or callable(on_completed_update),
            "Completed-update observer must be callable")
    rows = _training_rows(train_rows, max_sequence_tokens)
    require(all(module.enabled for module in bundle.modules.values()), "Adapters are disabled")
    torch = torch_module()
    named = bundle.named_adapter_parameters()
    permitted = {id(p) for p in named.values()}
    require(all(p.requires_grad == (id(p) in permitted) for p in bundle.model.parameters()),
            "Only adapter parameters may be trainable")
    adapter_state(bundle)  # Validate all parameters before constructing the optimizer.
    optimizer = torch.optim.AdamW(list(named.values()), lr=learning_rate, weight_decay=0.0)
    generator = torch.Generator(device="cpu").manual_seed(seed)
    modes = [(module, module.training) for module in bundle.model.modules()]
    losses, stop_reason, drawn = [], None, []
    bundle.model.eval()  # Frozen base dropout remains disabled; gradients still flow.
    try:
        for _ in range(max_steps):
            if time.monotonic() - started >= max_seconds:
                stop_reason = "COOPERATIVE_WALL_LIMIT"
                break
            index = int(torch.randint(len(rows), (), generator=generator))
            drawn.append(index)  # actual row exposure, recorded per update
            ids, labels = rows[index]
            first = next(i for i, label in enumerate(labels) if label != -100)
            targets = ids[first:]
            optimizer.zero_grad(set_to_none=True)
            result = bundle.model(input_ids=torch.tensor([ids], dtype=torch.long, device="cpu"),
                                  attention_mask=torch.ones((1, len(ids)), dtype=torch.long, device="cpu"),
                                  use_cache=False, logits_to_keep=len(targets) + 1)
            # Qwen3 slices hidden states before lm_head. Do not allocate vocabulary
            # logits for the masked prompt or pass full labels against this suffix.
            loss = _completion_loss(torch, getattr(result, "logits", None), targets)
            require(loss.ndim == 0 and bool(torch.isfinite(loss)), "Nonfinite or nonscalar training loss")
            loss.backward()
            require(all(p.grad is not None and bool(torch.isfinite(p.grad).all()) for p in named.values()),
                    "Missing or nonfinite adapter gradient")
            torch.nn.utils.clip_grad_norm_(list(named.values()), max_grad_norm, error_if_nonfinite=True)
            before = {name: p.detach().clone() for name, p in named.items()}
            try:
                optimizer.step()
                require(all(bool(torch.isfinite(p).all()) for p in named.values()),
                        "Nonfinite adapter update")
            except Exception:
                with torch.no_grad():
                    for name, p in named.items():
                        p.copy_(before[name])
                raise
            losses.append(float(loss.detach()))
            # One continuous optimizer run. Durable export failure stops this run;
            # the completed update is never described as an optimizer resume point.
            if on_completed_update is not None:
                on_completed_update({"completed_steps": len(losses),
                                     "training_losses": list(losses),
                                     "drawn_rows": list(drawn[:len(losses)]),
                                     "adapter_state": adapter_state(bundle)})
    finally:
        optimizer.zero_grad(set_to_none=True)
        for module, training in modes:
            module.training = training
    return {"schema": "loom-qwen-adapter-training/v1",
            "status": "FIT_UNVERIFIED" if losses else "UNTRAINED",
            "completed_steps": len(losses), "requested_steps": max_steps,
            "stop_reason": stop_reason, "wall_seconds": time.monotonic() - started,
            "training_losses": losses, "drawn_rows": drawn[:len(losses)], "train_rows": len(rows), "split": split,
            "trainable_parameters": sum(p.numel() for p in named.values()),
            "base_manifest_sha256": bundle.base_manifest_sha256, "seed": seed,
            "learning_rate": learning_rate, "max_grad_norm": max_grad_norm,
            "loss_mode": "completion_suffix_causal_cross_entropy",
            "max_sequence_tokens": max_sequence_tokens}
