"""Optional local learned NLI scores from a fixed, pinned candidate registry.

Only premise and hypothesis text enter the model. Scores are uncalibrated model
outputs, not established reliability, factual truth, or record-promotion rights.
Imports, downloads, model loading, and inference never occur at module import.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import struct


REPOSITORY = "MoritzLaurer/DeBERTa-v3-xsmall-mnli-fever-anli-ling-binary"
REVISION = "b80e2b3219194b8685948dedfb73d594ed088732"
WEIGHTS_SHA256 = "644a8866c06be5bd6374c84aaea0d428590483ae025f807aa44c5dde43dcf3d9"
FILE_SIZES = {"README.md": 4734, "added_tokens.json": 18, "config.json": 1086,
              "model.safetensors": 283351628, "special_tokens_map.json": 156,
              "spm.model": 2464616, "tokenizer.json": 8656646, "tokenizer_config.json": 488}
MAX_PARAMETERS = 100_000_000
MAX_PREMISE_BYTES, MAX_HYPOTHESIS_BYTES, MAX_TOKENS = 8192, 2048, 512
ID2LABEL = {"0": "entailment", "1": "not_entailment"}


def _spec(repository, revision, file_sizes, git_blobs, weights_sha256,
          storage_dtype, float_elements, width, heads, layers, ff_width, labels):
    upstream = {name: {"git_blob_sha1": digest} for name, digest in git_blobs.items()}
    upstream["model.safetensors"] = {"sha256": weights_sha256}
    upstream["spm.model"] = {"sha256": "c679fbf93643d19aab7ee10c0b99e460bdbc02fedf34b92b05af343b4af586fd"}
    return {"repository": repository, "revision": revision, "file_sizes": file_sizes,
            "upstream": upstream, "weights_sha256": weights_sha256, "storage_dtype": storage_dtype,
            "float_elements": float_elements, "int64_elements": 512, "parameter_cap": float_elements + 512,
            "width": width, "heads": heads, "layers": layers, "ff_width": ff_width, "labels": labels}


# Identity and architecture pins from the preregistered three-candidate ladder.
# Git blob IDs bind every non-LFS metadata/tokenizer file to its pinned revision;
# the acquisition receipt additionally supplies full SHA-256 pins for all files.
_XSMALL = _spec(REPOSITORY, REVISION, dict(FILE_SIZES), {
    "README.md": "863679afbd1746021eb5b28dd7bd3c6ceb38d6f9",
    "added_tokens.json": "7eb072694bd948f7977a20c25ecc9c381d5be1e3",
    "config.json": "bc3089101517683b51e0ca008bccf01daa043abb",
    "special_tokens_map.json": "4d9e49690449efe416b116fa6153b094dc818020",
    "tokenizer.json": "f9bbf3fa0b51e2c1d9c2bf284e3de29c542720c7",
    "tokenizer_config.json": "5ae03d6894248152505eed9a32d9435019e32b62"},
    WEIGHTS_SHA256, "F32", 70_830_722, 384, 6, 12, 1536, ["entailment", "not_entailment"])
_BASE = _spec("MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli",
    "6f5cf0a2b59cabb106aca4c287eed12e357e90eb", {
    "README.md": 23571, "added_tokens.json": 23, "config.json": 1090,
    "model.safetensors": 368877646, "special_tokens_map.json": 286,
    "spm.model": 2464616, "tokenizer.json": 8656646, "tokenizer_config.json": 1284}, {
    "README.md": "c9e9b0d4233641b89a5e612e55c59c68b66a0e97",
    "added_tokens.json": "8ee2b3623dc526b123cde0aaa401755b82299af2",
    "config.json": "f5ae94167b1efbea3338ba93860abddc9f0de5f3",
    "special_tokens_map.json": "2c9cb07c8fdeeb5ac3ceafb170592e990b204dcd",
    "tokenizer.json": "f9bbf3fa0b51e2c1d9c2bf284e3de29c542720c7",
    "tokenizer_config.json": "13d6406af565bd9e59dd649afd4c2b48bddb6a4c"},
    "06d6fd89edd4f97816831626daafbdb0b029cf63bae8edc0bccab1d64e2e7707",
    "F16", 184_424_451, 768, 12, 12, 3072, ["entailment", "neutral", "contradiction"])
_LARGE = _spec("MoritzLaurer/DeBERTa-v3-large-mnli-fever-anli-ling-wanli",
    "b3546ea6b0346eb6f8d5d68b13c7dc6d0376b3d7", {
    "README.md": 7580, "added_tokens.json": 18, "config.json": 1056,
    "model.safetensors": 870182606, "special_tokens_map.json": 156,
    "spm.model": 2464616, "tokenizer.json": 8648889, "tokenizer_config.json": 395}, {
    "README.md": "0747410db50f1345f9e409cbe010c457535ba761",
    "added_tokens.json": "7eb072694bd948f7977a20c25ecc9c381d5be1e3",
    "config.json": "4132f0331b3d94b85de11ade18e48e0dae07d2d2",
    "special_tokens_map.json": "4d9e49690449efe416b116fa6153b094dc818020",
    "tokenizer.json": "08e1fd9af8a65c4ab19cb0b3f6e1a83ee911f8fb",
    "tokenizer_config.json": "60b134d3ec353ffa99d215bd626f91be3c7ac7de"},
    "c03cd208bf920b4fbbb182a0535859a566e8acc3477d2b536bf87b769978524b",
    "F16", 435_064_835, 1024, 16, 24, 4096, ["entailment", "neutral", "contradiction"])
CANDIDATES = {(entry["repository"], entry["revision"]): entry for entry in (_XSMALL, _BASE, _LARGE)}


def _candidate(manifest):
    if type(manifest) is not dict:
        raise ValueError("Acquisition receipt must be a JSON object")
    key = (manifest.get("repository"), manifest.get("revision"))
    if any(type(value) is not str for value in key) or key not in CANDIDATES:
        raise ValueError("Repository and revision are absent from the fixed candidate registry")
    return CANDIDATES[key]


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def validate_pair(premise, hypothesis):
    """Check complete UTF-8 strings without trimming, normalization, or rules."""
    encoded = []
    for value, limit, name in ((premise, MAX_PREMISE_BYTES, "premise"),
                               (hypothesis, MAX_HYPOTHESIS_BYTES, "hypothesis")):
        if type(value) is not str or not value:
            raise ValueError(name + " must be a nonempty string")
        try:
            raw = value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError(name + " must be valid UTF-8") from exc
        if len(raw) > limit:
            raise ValueError(name + " exceeds its UTF-8 byte bound; truncation is forbidden")
        encoded.append(raw)
    return tuple(encoded)


def _manifest(manifest):
    candidate = _candidate(manifest)
    file_sizes = candidate["file_sizes"]
    if "status" in manifest and manifest["status"] not in ("PASS", "COMPLETED"):
        raise ValueError("Acquisition receipt does not report successful completion")
    raw = _canonical(manifest)
    if len(raw) > 131072:
        raise ValueError("Acquisition receipt exceeds 128 KiB")
    entries = manifest.get("files")
    if type(entries) is not list or len(entries) != len(file_sizes):
        raise ValueError("Receipt must list exactly the eight allowlisted model files")
    files = {}
    for item in entries:
        required = {"name", "bytes", "sha256"}
        metadata = {"upstream_identity_verified", "url", "reuse_verified_completed_download"}
        if type(item) is not dict or not required <= set(item) or set(item) - required - metadata:
            raise ValueError("Each file requires name, bytes, sha256, and only recognized acquisition metadata")
        name = item["name"]
        if type(name) is not str or name not in file_sizes or name in files:
            raise ValueError("Duplicate or non-allowlisted file name")
        if type(item["bytes"]) is not int or item["bytes"] != file_sizes[name]:
            raise ValueError("File size differs from the pinned repository inventory")
        if type(item["sha256"]) is not str or re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) is None:
            raise ValueError("File requires a lowercase SHA-256 digest")
        pinned_sha256 = candidate["upstream"][name].get("sha256")
        if pinned_sha256 is not None and item["sha256"] != pinned_sha256:
            raise ValueError("Asset differs from its pinned upstream LFS SHA-256 identity")
        files[name] = dict(item)
    return files, _sha(raw)


def _root(model_dir, file_sizes=FILE_SIZES):
    supplied = Path(model_dir)
    if ".." in supplied.parts:
        raise ValueError("Parent traversal is forbidden in model_dir")
    root = Path(os.path.abspath(supplied))
    if any(path.is_symlink() for path in (root, *root.parents)) or not root.is_dir():
        raise ValueError("Existing model directory without symlink components required")
    if {path.name for path in root.iterdir()} != set(file_sizes):
        raise ValueError("Model directory must contain exactly the eight allowlisted files")
    return root


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _verify_file(path, expected, upstream=None):
    """Stream the entire file once, rejecting links and concurrent mutations."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size != expected["bytes"]:
            raise ValueError("Model asset is not a singly linked regular file of its pinned size")
        digest, total = hashlib.sha256(), 0
        blob = hashlib.sha1(b"blob " + str(expected["bytes"]).encode() + b"\0") if upstream and "git_blob_sha1" in upstream else None
        with os.fdopen(fd, "rb", closefd=False) as stream:
            while True:
                block = stream.read(1024 * 1024)
                if not block:
                    break
                total += len(block)
                if total > expected["bytes"]:
                    raise ValueError("Model asset grew beyond its pinned size")
                digest.update(block)
                if blob is not None:
                    blob.update(block)
        if _identity(before) != _identity(os.fstat(fd)) or _identity(before) != _identity(path.stat(follow_symlinks=False)):
            raise ValueError("Model asset changed during verification")
        if total != expected["bytes"] or digest.hexdigest() != expected["sha256"]:
            raise ValueError("Model asset SHA-256 or byte size differs from acquisition receipt")
        if blob is not None and blob.hexdigest() != upstream["git_blob_sha1"]:
            raise ValueError("Model metadata/tokenizer asset differs from its pinned Git blob identity")
        return _identity(before)
    finally:
        os.close(fd)


def _json_file(path, limit=65536):
    raw = path.read_bytes()
    if len(raw) > limit:
        raise ValueError("Configuration exceeds its bounded size")
    value = json.loads(raw, object_pairs_hook=_unique_pairs)
    if type(value) is not dict:
        raise ValueError("Configuration must be a JSON object")
    return value


def _config(config, candidate=None):
    candidate = _XSMALL if candidate is None else candidate
    expected = {"model_type": "deberta-v2", "architectures": ["DebertaV2ForSequenceClassification"],
                "hidden_size": candidate["width"], "num_hidden_layers": candidate["layers"], "num_attention_heads": candidate["heads"],
                "intermediate_size": candidate["ff_width"], "vocab_size": 128100, "max_position_embeddings": 512,
                "pooler_hidden_size": candidate["width"], "type_vocab_size": 0, "position_buckets": 256,
                "max_relative_positions": -1, "position_biased_input": False,
                "relative_attention": True, "share_att_key": True,
                "pos_att_type": ["p2c", "c2p"], "torch_dtype": "float32" if candidate["storage_dtype"] == "F32" else "float16"}
    known = set(expected) | {"_name_or_path", "attention_probs_dropout_prob", "hidden_act",
            "hidden_dropout_prob", "id2label", "initializer_range", "label2id", "layer_norm_eps",
            "norm_rel_ebd", "pad_token_id", "pooler_dropout", "pooler_hidden_act", "transformers_version"}
    if set(config) != known or any(_canonical(config.get(key)) != _canonical(value) for key, value in expected.items()):
        raise ValueError("Config differs from the bounded pinned DeBERTa architecture")
    id2label = {str(index): label for index, label in enumerate(candidate["labels"])}
    if (_canonical(config.get("id2label")) != _canonical(id2label) or
            _canonical(config.get("label2id")) != _canonical({label: int(index) for index, label in id2label.items()})):
        raise ValueError("Label indices differ from this registered candidate's pinned config")
    if "auto_map" in config or "custom_pipelines" in config:
        raise ValueError("Remote/custom model code is not supported")
    return dict(config)


def _safetensors_inventory(path, *, parameter_cap=MAX_PARAMETERS, storage_dtype="F32"):
    """Bound registered on-disk shapes/dtype before importing ML libraries."""
    if storage_dtype not in ("F32", "F16") or type(parameter_cap) is not int or not 1 <= parameter_cap <= _LARGE["parameter_cap"]:
        raise ValueError("Invalid inventory bounds")
    size = path.stat().st_size
    with path.open("rb") as stream:
        first = stream.read(8)
        if len(first) != 8:
            raise ValueError("Missing safetensors header")
        header_bytes = struct.unpack("<Q", first)[0]
        if not 2 <= header_bytes <= 1024 * 1024 or header_bytes + 8 > size:
            raise ValueError("Safetensors header exceeds its bounded size")
        header = json.loads(stream.read(header_bytes), object_pairs_hook=_unique_pairs)
    if type(header) is not dict or not 1 <= len(header) <= 2001:
        raise ValueError("Invalid safetensors tensor inventory")
    floating, integer, intervals = 0, 0, []
    for name, descriptor in header.items():
        if name == "__metadata__":
            if type(descriptor) is not dict or any(type(k) is not str or type(v) is not str for k, v in descriptor.items()):
                raise ValueError("Malformed safetensors metadata")
            continue
        if not name or len(name.encode()) > 256 or type(descriptor) is not dict or set(descriptor) != {"dtype", "shape", "data_offsets"}:
            raise ValueError("Malformed tensor descriptor")
        shape, dtype, offsets = descriptor["shape"], descriptor["dtype"], descriptor["data_offsets"]
        if type(shape) is not list or len(shape) > 4 or any(type(n) is not int or not 1 <= n <= parameter_cap for n in shape):
            raise ValueError("Tensor dimensions exceed architecture bounds")
        count = math.prod(shape)
        if dtype not in (storage_dtype, "I64") or count > parameter_cap:
            raise ValueError("Tensor dtype or shape differs from the registered on-disk format")
        if type(offsets) is not list or len(offsets) != 2 or any(type(n) is not int for n in offsets):
            raise ValueError("Invalid tensor data offsets")
        start, end = offsets
        itemsize = {"F16": 2, "F32": 4, "I64": 8}[dtype]
        if not 0 <= start <= end <= size - 8 - header_bytes or end - start != count * itemsize:
            raise ValueError("Tensor data size disagrees with dtype and shape")
        floating += count if dtype == storage_dtype else 0
        integer += count if dtype == "I64" else 0
        if floating + integer > parameter_cap:
            raise ValueError("Model exceeds its registered parameter and buffer bound")
        intervals.append((start, end))
    cursor = 0
    for start, end in sorted(intervals):
        if start != cursor:
            raise ValueError("Tensor data overlaps or leaves an unaccounted gap")
        cursor = end
    if cursor != size - 8 - header_bytes or not floating:
        raise ValueError("Safetensors contains unaccounted bytes or no floating parameters")
    floating_key = "float32_elements" if storage_dtype == "F32" else "float16_elements"
    return {floating_key: floating, "int64_elements": integer, "tensor_count": len(intervals)}


def validate_assets(model_dir, manifest):
    """Read-only full asset validation; imports no torch/transformers code."""
    candidate = _candidate(manifest)
    files, manifest_sha256 = _manifest(manifest)
    root = _root(model_dir, candidate["file_sizes"])
    identities = {name: _verify_file(root / name, files[name], candidate["upstream"][name]) for name in sorted(files)}
    config = _config(_json_file(root / "config.json"), candidate)
    tokenizer_config = _json_file(root / "tokenizer_config.json")
    if "auto_map" in tokenizer_config or tokenizer_config.get("tokenizer_class") not in ("DebertaV2Tokenizer", "DebertaV2TokenizerFast"):
        raise ValueError("Expected the built-in DeBERTa fast tokenizer without remote code")
    inventory = _safetensors_inventory(root / "model.safetensors", parameter_cap=candidate["parameter_cap"], storage_dtype=candidate["storage_dtype"])
    floating_key = "float32_elements" if candidate["storage_dtype"] == "F32" else "float16_elements"
    if inventory[floating_key] != candidate["float_elements"] or inventory["int64_elements"] != candidate["int64_elements"]:
        raise ValueError("Tensor element counts differ from the registered checkpoint inventory")
    return {"root": root, "manifest_sha256": manifest_sha256, "file_identities": identities,
            "config": config, "inventory": inventory, "candidate": candidate}


def _probabilities(logits, labels):
    """Retain all learned classes; collapse non-entailment only after softmax."""
    if (type(logits) is not list or type(labels) is not list or len(logits) != len(labels)
            or len(labels) not in (2, 3) or labels.count("entailment") != 1
            or set(labels) not in ({"entailment", "not_entailment"}, {"entailment", "neutral", "contradiction"})):
        raise ValueError("Unsupported NLI logits or label mapping")
    if any(type(value) not in (int, float) or not math.isfinite(value) for value in logits):
        raise ValueError("Nonfinite NLI logits")
    peak = max(logits)
    exponentials = [math.exp(value - peak) for value in logits]
    probabilities = [value / sum(exponentials) for value in exponentials]
    supported = probabilities[labels.index("entailment")]
    return probabilities, supported, 1.0 - supported


class LocalNLI:
    """Load one CPU float32 model once and reuse it for explicit text pairs."""

    def __init__(self, model_dir, manifest, max_tokens=MAX_TOKENS, threads=2):
        if type(max_tokens) is not int or not 1 <= max_tokens <= MAX_TOKENS:
            raise ValueError("max_tokens must be an integer from 1 to 512")
        if type(threads) is not int or not 1 <= threads <= 2:
            raise ValueError("threads must be 1 or 2")
        verified = validate_assets(model_dir, manifest)
        candidate = verified["candidate"]
        # No optional library is imported until every complete asset has passed.
        import torch
        from transformers import AutoTokenizer, AutoModelForSequenceClassification
        torch.set_num_threads(threads)
        try:
            torch.set_num_interop_threads(1)
        except RuntimeError:
            if torch.get_num_interop_threads() != 1:
                raise RuntimeError("Start a fresh process to enforce one interop thread")
        location = str(verified["root"])
        tokenizer = AutoTokenizer.from_pretrained(location, use_fast=True,
            local_files_only=True, trust_remote_code=False,
            tokenizer_file=str(verified["root"] / "tokenizer.json"), from_slow=False)
        if tokenizer.is_fast is not True:
            raise ValueError("The installed tokenizer must be fast; slow fallback is forbidden")
        model = AutoModelForSequenceClassification.from_pretrained(location,
            local_files_only=True, trust_remote_code=False, use_safetensors=True,
            torch_dtype=torch.float32, device_map={"": "cpu"}, low_cpu_mem_usage=True)
        labels = [verified["config"]["id2label"][str(index)] for index in range(len(candidate["labels"]))]
        if model.config.id2label != dict(enumerate(labels)) or model.config.num_labels != len(labels):
            raise ValueError("Loaded model label indices differ from the pinned config")
        parameter_count = sum(parameter.numel() for parameter in model.parameters())
        if parameter_count != candidate["float_elements"] or parameter_count > candidate["parameter_cap"]:
            raise ValueError("Loaded parameter count differs from the registered checkpoint")
        for tensor in list(model.parameters()) + list(model.buffers()):
            if tensor.device.type != "cpu" or (tensor.is_floating_point() and tensor.dtype != torch.float32):
                raise ValueError("Model tensors must remain on CPU with float32 floating weights")
        for name, identity in verified["file_identities"].items():
            if _identity((verified["root"] / name).stat(follow_symlinks=False)) != identity:
                raise ValueError("Verified model asset changed while loading")
        model.eval()
        model.requires_grad_(False)
        self._torch, self._tokenizer, self._model = torch, tokenizer, model
        self._model_dir, self._file_identities = verified["root"], verified["file_identities"]
        self._labels = labels
        self.max_tokens = max_tokens
        self.identity = {"repository": candidate["repository"], "revision": candidate["revision"],
                         "weights_sha256": candidate["weights_sha256"], "manifest_sha256": verified["manifest_sha256"],
                         "parameter_count": parameter_count, "device": "cpu", "dtype": "float32",
                         "storage_dtype": candidate["storage_dtype"], "labels": list(labels),
                         "threads": threads, "interop_threads": 1, **verified["inventory"]}

    def verify_unchanged(self):
        """Cheap post-run identity check; full SHA verification occurs at load."""
        if {path.name for path in self._model_dir.iterdir()} != set(self._file_identities):
            raise ValueError("Model file inventory changed after loading")
        for name, identity in self._file_identities.items():
            if _identity((self._model_dir / name).stat(follow_symlinks=False)) != identity:
                raise ValueError("Verified model asset changed after loading")
        return {"status": "UNCHANGED", "files": len(self._file_identities),
                "verification": "DEVICE_INODE_SIZE_MTIME_CTIME_AGAINST_FULL_LOAD_TIME_SHA256_CHECK"}

    def score(self, premise, hypothesis):
        premise_raw, hypothesis_raw = validate_pair(premise, hypothesis)
        encoded = dict(self._tokenizer(premise, hypothesis, add_special_tokens=True,
            truncation=False, padding=False, return_attention_mask=True,
            return_token_type_ids=False, return_tensors=None))
        if set(encoded) != {"input_ids", "attention_mask"}:
            raise ValueError("Unexpected tokenizer fields")
        ids, mask = encoded["input_ids"], encoded["attention_mask"]
        if type(ids) is not list or not 1 <= len(ids) <= self.max_tokens:
            raise ValueError("Complete tokenized pair exceeds the token bound; truncation is forbidden")
        if any(type(token) is not int or not 0 <= token < 128100 for token in ids):
            raise ValueError("Invalid tokenizer IDs")
        if type(mask) is not list or len(mask) != len(ids) or any(type(value) is not int or value != 1 for value in mask):
            raise ValueError("Tokenizer must return one complete unpadded pair")
        torch = self._torch
        batch = {name: torch.tensor([value], dtype=torch.long, device="cpu") for name, value in encoded.items()}
        with torch.inference_mode():
            logits_tensor = self._model(**batch, return_dict=True).logits
        if tuple(logits_tensor.shape) != (1, len(self._labels)) or logits_tensor.dtype != torch.float32 or logits_tensor.device.type != "cpu":
            raise ValueError("Expected the registered number of CPU float32 logits")
        logits = logits_tensor[0].tolist()
        probabilities, supported, unsupported = _probabilities(logits, self._labels)
        return {"logits": logits, "probabilities": probabilities,
                "p_supported": supported, "p_not_supported": unsupported,
                "token_count": len(ids), "labels": list(self._labels),
                "premise_sha256": _sha(premise_raw), "hypothesis_sha256": _sha(hypothesis_raw),
                "premise_bytes": len(premise_raw), "hypothesis_bytes": len(hypothesis_raw),
                "score_interpretation": "UNCALIBRATED_ENTAILMENT_PREFERENCE",
                "semantic_reliability": "NOT_ESTABLISHED", "not_entailment_meaning": "NEUTRAL_OR_CONTRADICTED_NOT_NECESSARILY_FALSE",
                "authority": "NONE", "seed_promotion": "NONE"}
