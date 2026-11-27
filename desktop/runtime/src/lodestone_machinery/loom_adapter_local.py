"""Explicit, local-only Qwen reader for Loom's generative adapter.

Importing or verifying this module performs no model forward or training.
The registered assets are the already acquired Qwen3 checkpoint. No download,
provider call, remote model code, pickle loader, or automatic retry is used.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import stat
import time
import math

from lodestone_nurarihyon.portable_files import (absolute_path, guarded_directory,
                                                 open_regular)


REPOSITORY = "Qwen/Qwen3-0.6B"
REVISION = "167b8104f88905a951069f5f95f9776908da5f68"
# Exact existing acquisition binding; these are model assets, never login files.
ASSETS = {
    "added_tokens.json": (707, "c0284b582e14987fbd3d5a2cb2bd139084371ed9acbae488829a1c900833c680"),
    "config.json": (726, "660db3b73d788119c04535e48cf9be5f55bc3100841a718637ae695b442f27dd"),
    "generation_config.json": (239, "2325da0f15bb848e018c5ae071b7943332e9f871d6b60e2ed22ca97d4cb993d2"),
    "merges.txt": (1671853, "8831e4f1a044471340f7c0a83d7bd71306a5b867e95fd870f74d0c5308a904d5"),
    "model.safetensors": (1503300328, "f47f71177f32bcd101b7573ec9171e6a57f4f4d31148d38e382306f42996874b"),
    "special_tokens_map.json": (613, "76862e765266b85aa9459767e33cbaf13970f327a0e88d1c65846c2ddd3a1ecd"),
    "tokenizer.json": (11422654, "aeb13307a71acd8fe81861d94ad54ab689df773318809eed3cbe794b4492dae4"),
    "tokenizer_config.json": (9676, "bbc2c089e3bef8753f63348c10595767be8998bc6ca7c7ebcaada346d401fba8"),
    "vocab.json": (2776833, "ca10d7e9fb3ed18575dd1e277a2579c16d108e32f27439684afa0e10b1440910"),
}
VERSIONS = {"torch": "2.2.2", "transformers": "4.51.3", "accelerate": "0.26.1",
            "safetensors": "0.5.3", "tokenizers": "0.21.1"}
# The Ollama reader only loads these tokenizer dependencies. Float32 model and
# training routes continue to require the complete VERSIONS binding below.
TOKENIZER_VERSIONS = {name: VERSIONS[name] for name in
                      ("transformers", "safetensors", "tokenizers")}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def manifest():
    return {"model": REPOSITORY, "revision": REVISION,
            "files": {name: {"bytes": size, "sha256": sha}
                      for name, (size, sha) in ASSETS.items()}}


MANIFEST_SHA256 = hashlib.sha256(canonical(manifest())).hexdigest()


def validate_state_document(state):
    """Check a serving artifact before importing torch or loading base weights.

    This verifies a format/base binding, not that the artifact was trained or
    that its output will be useful. The numeric loader still validates again.
    """
    from .loom_adapter_torch import AdapterConfig, SCHEMA
    if (type(state) is not dict or set(state) != {
            "schema", "base_manifest_sha256", "config", "tensors"}
            or state["schema"] != SCHEMA
            or state["base_manifest_sha256"] != MANIFEST_SHA256):
        raise ValueError("Adapter artifact schema or pinned base differs")
    config = state["config"]
    if (type(config) is not dict or config != AdapterConfig().document()
            or type(config.get("rank")) is not int or type(config.get("alpha")) is not int
            or type(config.get("layers")) is not list
            or any(type(x) is not int for x in config["layers"])):
        raise ValueError("Adapter artifact configuration differs")
    shapes = {}
    for layer in (26, 27):
        for projection, output in (("q_proj", 2048), ("v_proj", 1024)):
            name = f"model.layers.{layer}.self_attn.{projection}"
            shapes[name + ".a"] = [4, 1024]
            shapes[name + ".b"] = [output, 4]
    if type(state["tensors"]) is not dict or set(state["tensors"]) != set(shapes):
        raise ValueError("Adapter artifact tensor inventory differs")
    for name, shape in shapes.items():
        tensor = state["tensors"][name]
        if (type(tensor) is not dict or set(tensor) != {"shape", "values"}
                or type(tensor["shape"]) is not list or tensor["shape"] != shape
                or any(type(x) is not int for x in tensor["shape"])):
            raise ValueError("Adapter artifact tensor shape differs")
        rows = tensor["values"]
        if type(rows) is not list or len(rows) != shape[0]:
            raise ValueError("Adapter artifact numeric values differ")
        for row in rows:
            if type(row) is not list or len(row) != shape[1]:
                raise ValueError("Adapter artifact numeric values differ")
            for value in row:
                try:
                    valid = type(value) in (int, float) and math.isfinite(value) and abs(value) <= 3.4028234663852886e38
                except OverflowError:
                    valid = False
                if not valid:
                    raise ValueError("Adapter artifact contains invalid float32 values")
    return state


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _root(directory):
    supplied = Path(directory)
    if ".." in supplied.parts:
        raise ValueError("Parent traversal in model directory")
    root = absolute_path(supplied)
    with guarded_directory(root):
        if {p.name for p in root.iterdir()} != set(ASSETS):
            raise ValueError("Model directory must contain exactly the registered assets")
    return root


def _verify_file(path, expected):
    size, expected_sha = expected
    with open_regular(path) as fd:
        before = os.fstat(fd)
        named_before = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or before.st_size != size:
            raise ValueError("Model asset is not a regular file of the registered size")
        digest = hashlib.sha256()
        total = 0
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block:
                break
            total += len(block)
            if total > size:
                raise ValueError("Model asset grew while being verified")
            digest.update(block)
        if (_identity(before) != _identity(os.fstat(fd)) or
                _identity(named_before) != _identity(path.stat(follow_symlinks=False))):
            raise ValueError("Model asset changed during verification")
        if total != size or digest.hexdigest() != expected_sha:
            raise ValueError("Model asset differs from its registered SHA-256")
        return _identity(before)


class VerifiedAssets:
    """Full initial content check, then explicit identity or full-content recheck."""

    def __init__(self, directory):
        self.root = _root(directory)
        with guarded_directory(self.root):
            self.root_identity = (self.root.stat().st_dev, self.root.stat().st_ino)
            self.identities = {name: _verify_file(self.root / name, expected)
                               for name, expected in ASSETS.items()}

    def verify_unchanged(self, *, full=False):
        with guarded_directory(self.root):
            root = _root(self.root)
            if root != self.root or (root.stat().st_dev, root.stat().st_ino) != self.root_identity:
                raise ValueError("Model root changed")
            for name, expected in ASSETS.items():
                path = self.root / name
                # Compare handle metadata with handle metadata: Windows path
                # ctime means creation time while fstat ctime means ChangeTime.
                with open_regular(path) as fd:
                    info = os.fstat(fd)
                    if not stat.S_ISREG(info.st_mode) or _identity(info) != self.identities[name]:
                        raise ValueError("Verified model asset identity changed")
                    if full and _verify_file(path, expected) != self.identities[name]:
                        raise ValueError("Verified model content changed")

    def receipt(self):
        return {"model": REPOSITORY, "revision": REVISION,
                "manifest_sha256": MANIFEST_SHA256, "files_verified": len(ASSETS),
                "full_content_check": True, "model_forwards": 0}


def _versions(expected=None):
    expected = VERSIONS if expected is None else expected
    actual = {name: importlib.metadata.version(name) for name in expected}
    if actual != expected:
        raise ValueError("Use the registered local package versions; no implicit installation")
    return actual


def _tokenizer_versions():
    """Check tokenizer pins without requiring unused tensor/model packages."""
    return _versions(TOKENIZER_VERSIONS)


def load_tokenizer(assets):
    """Tokenization only; no neural weight load or inference."""
    if not isinstance(assets, VerifiedAssets):
        raise ValueError("VerifiedAssets required")
    _versions()
    assets.verify_unchanged()
    from transformers import Qwen2TokenizerFast
    tokenizer = Qwen2TokenizerFast.from_pretrained(
        str(assets.root), local_files_only=True, trust_remote_code=False)
    if tokenizer.eos_token_id != 151645 or tokenizer.pad_token_id != 151643:
        raise ValueError("Unexpected tokenizer EOS/PAD identity")
    assets.verify_unchanged()
    return tokenizer


class LocalAdapter:
    """An explicitly loaded model. Caller owns study authorization and supervision.

    Neither initialization nor training/generation is an automatic daemon task.
    Cooperative time limits in training require an outer CPU/wall/RAM supervisor.
    """

    def __init__(self, assets, *, seed=0):
        from . import loom_adapter_torch as backend
        _versions()
        if not isinstance(assets, VerifiedAssets):
            raise ValueError("VerifiedAssets required")
        if type(seed) is not int or not 0 <= seed < 2 ** 31:
            raise ValueError("Invalid seed")
        assets.verify_unchanged()
        torch = backend.torch_module()  # Set both thread limits before tensor work.
        from transformers import Qwen3ForCausalLM
        torch.manual_seed(seed)
        self.assets, self.tokenizer = assets, load_tokenizer(assets)
        self.model = Qwen3ForCausalLM.from_pretrained(
            str(assets.root), local_files_only=True, trust_remote_code=False,
            use_safetensors=True, torch_dtype=torch.float32,
            low_cpu_mem_usage=True, attn_implementation="sdpa")
        self.model.to("cpu")
        self.model.eval()
        self.bundle = backend.attach_adapters(
            self.model, base_manifest_sha256=MANIFEST_SHA256, seed=seed)
        assets.verify_unchanged()

    def train(self, dataset, split_manifest, *, max_steps, max_seconds,
              max_total_tokens=1024, max_new_tokens=256, learning_rate=1e-4, seed=0,
              on_completed_update=None):
        from . import loom_adapter as contract
        from . import loom_adapter_torch as backend
        started, cpu_started = time.monotonic(), time.process_time()
        rows = contract.validate_training_dataset(dataset, split_manifest)
        # Validate/encode every row before the first optimizer update. No skipped rows.
        encoded = [contract.encode_training_row(
            row, self.tokenizer, max_total_tokens=max_total_tokens,
            max_new_tokens=max_new_tokens) for row in rows]
        train_rows = [{"input_ids": row["input_ids"], "labels": row["labels"]}
                      for row in encoded]
        self.assets.verify_unchanged()
        before_sha = hashlib.sha256(canonical(self.state())).hexdigest()
        report = backend.train_completion(
            self.bundle, train_rows, split="train", max_steps=max_steps,
            max_seconds=max_seconds, max_sequence_tokens=max_total_tokens,
            learning_rate=learning_rate, seed=seed, on_completed_update=on_completed_update)
        self.assets.verify_unchanged()
        report["local_execution"] = {
            "dataset_sha256": hashlib.sha256(canonical(dataset)).hexdigest(),
            "split_manifest_sha256": hashlib.sha256(canonical(split_manifest)).hexdigest(),
            "adapter_before_sha256": before_sha,
            "adapter_after_sha256": hashlib.sha256(canonical(self.state())).hexdigest(),
            "prompt_tokens_per_row": [row["prompt_tokens"] for row in encoded],
            "target_tokens_per_row": [row["target_tokens"] for row in encoded],
            "wall_seconds": time.monotonic() - started,
            "process_cpu_seconds": time.process_time() - cpu_started,
            "attention_implementation": "sdpa", "packages": dict(VERSIONS),
            "provider_calls": 0, "semantic_success_established": False}
        return report

    def predict(self, request, *, adapter_enabled, max_total_tokens=1024,
                max_new_tokens=256, encoder=None, progress_callback=None):
        """Use the shared full-history bounded-cache CPU inference path.

        Existing request/policy arguments are unchanged. An explicit alternate
        encoder is caller-owned; native admission and supervision remain outside
        the reader. No runtime/profile activation is performed by this method.
        """
        from .loom_inference_memory import predict
        return predict(self, request, adapter_enabled=adapter_enabled,
                       max_total_tokens=max_total_tokens, max_new_tokens=max_new_tokens,
                       encoder=encoder, progress_callback=progress_callback)

    def state(self):
        from . import loom_adapter_torch as backend
        return backend.adapter_state(self.bundle)

    def load_state(self, state):
        from . import loom_adapter_torch as backend
        backend.load_adapter_state(self.bundle, state)
