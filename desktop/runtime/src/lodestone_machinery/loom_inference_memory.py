"""Reusable full-history CPU inference with bounded KV storage and prefill activations.

No model is loaded by this module. The caller owns one already loaded adapter,
native admission, resource supervision and durable progress capture.
"""
from contextlib import contextmanager, nullcontext
import hashlib
import json
import time


CHUNK_TOKENS = 128
STREAM_INTERVAL = 16
MAX_RSS_BYTES = 4294967296


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode()


def sha(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _dimensions(config):
    """The reviewed architecture is bias-free Qwen3 with full causal attention."""
    if (type(config) is not dict or config.get("model_type") != "qwen3" or
            config.get("attention_bias", False) is not False or
            config.get("use_sliding_window", False) is not False or
            config.get("sliding_window") is not None or
            type(config.get("tie_word_embeddings")) is not bool):
        raise ValueError("Expected bias-free full-attention Qwen3 geometry")
    names = ("hidden_size", "intermediate_size", "num_hidden_layers", "num_attention_heads",
             "num_key_value_heads", "head_dim", "vocab_size")
    if any(type(config.get(k)) is not int or not 1 <= config[k] <= 1000000 for k in names):
        raise ValueError("Model dimensions must be explicit bounded positive integers")
    if config["num_attention_heads"] % config["num_key_value_heads"]:
        raise ValueError("Query heads must be divisible by KV heads")
    return tuple(config[k] for k in names)


def assess_memory(model_config, *, prompt_tokens, max_new_tokens,
                  max_rss_bytes=MAX_RSS_BYTES, prefill_chunk_tokens=CHUNK_TOKENS):
    """Tensor-storage lower bound, not a process-RSS estimate or fit promise.

    Does not import torch, load assets or call a model. It counts tied embeddings
    once and includes a full-history KV capacity through the final generation
    forward. Runtime, allocator, adapters and temporary attention work are extra.
    """
    hidden, intermediate, layers, heads, kv_heads, head_dim, vocab = _dimensions(model_config)
    for value, low, high, name in ((prompt_tokens, 1, 8192, "prompt_tokens"),
            (max_new_tokens, 1, 512, "max_new_tokens"),
            (max_rss_bytes, 1, MAX_RSS_BYTES, "max_rss_bytes"),
            (prefill_chunk_tokens, 1, 256, "prefill_chunk_tokens")):
        if type(value) is not int or not low <= value <= high:
            raise ValueError(name + " exceeds the reviewed inference envelope")
    if prompt_tokens + max_new_tokens > 8192:
        raise ValueError("Complete prompt and reserved output exceed token envelope")
    embeddings = vocab * hidden * (1 if model_config["tie_word_embeddings"] else 2)
    attention = hidden * (2 * heads + 2 * kv_heads) * head_dim
    per_layer = attention + 3 * hidden * intermediate + 2 * hidden + 2 * head_dim
    base_elements = embeddings + layers * per_layer + hidden
    capacity = prompt_tokens + max_new_tokens - 1
    cache_per_token = 2 * layers * kv_heads * head_dim * 4
    weights, cache = base_elements * 4, capacity * cache_per_token
    return {"schema": "loom-inference-memory-assessment/v1", "dtype": "float32", "device": "cpu",
        "base_parameter_elements": base_elements, "base_weight_bytes": weights,
        "tied_embeddings": model_config["tie_word_embeddings"], "batch_size": 1,
        "prompt_tokens": prompt_tokens, "max_new_tokens": max_new_tokens,
        "cache_capacity_tokens": capacity, "cache_bytes_per_token": cache_per_token,
        "cache_capacity_bytes": cache, "tensor_storage_lower_bound_bytes": weights + cache,
        "max_rss_bytes": max_rss_bytes, "remaining_for_unmeasured_overhead_bytes": max_rss_bytes - weights - cache,
        "lower_bound_exceeds_rss_cap": weights + cache > max_rss_bytes,
        "prefill_chunk_tokens": prefill_chunk_tokens, "actual_peak_rss_bytes": None,
        "fit_established": False, "model_calls": 0, "weight_updates": 0,
        "limitations": ["No allocation profile or actual RSS measurement is performed.",
            "Runtime, tokenizer, adapter weights, attention temporaries and allocator overhead are additional.",
            "Removing growing cache concatenations does not prove a previous stop's allocation cause.",
            "The caller must keep an external process-group RSS and wall-clock supervisor."]}


def make_prefix_cache(model_config, capacity):
    """Create one append-only float32 CPU cache; expose initialized prefixes only.

    Unlike DynamicCache.update, appends never concatenate all prior K/V data.
    Unlike StaticCache, attention never sees uninitialized capacity or infers
    length from nonzero values. This supports one greedy sequence, not beams,
    sliding windows, padding, offloading, compilation or cache reuse across arms.
    """
    import torch
    import transformers
    from transformers.cache_utils import Cache
    if torch.__version__.split("+")[0] != "2.2.2" or transformers.__version__ != "4.51.3":
        raise ValueError("Unreviewed cache runtime version")
    _, _, layers, _, heads, head_dim, _ = _dimensions(model_config)
    if type(capacity) is not int or not 1 <= capacity <= 8192:
        raise ValueError("Cache capacity exceeds the reviewed token envelope")
    if 2 * layers * heads * head_dim * capacity * 4 > MAX_RSS_BYTES:
        raise ValueError("KV storage alone exceeds the process RSS ceiling")

    class PrefixCache(Cache):
        is_compileable = False

        def __init__(self):
            super().__init__()
            self.key_cache = [None] * layers
            self.value_cache = [None] * layers
            self.lengths = [0] * layers
            self.updates = 0

        def update(self, key_states, value_states, layer_idx, cache_kwargs=None):
            if type(layer_idx) is not int or not 0 <= layer_idx < layers:
                raise ValueError("Unknown cache layer")
            if (key_states.dtype != torch.float32 or value_states.dtype != torch.float32 or
                    key_states.device.type != "cpu" or value_states.device.type != "cpu" or
                    key_states.ndim != 4 or key_states.shape != value_states.shape or
                    key_states.shape[0] != 1 or key_states.shape[1] != heads or key_states.shape[3] != head_dim):
                raise ValueError("Expected single-request CPU float32 KV tensors with pinned geometry")
            start, count = self.lengths[layer_idx], key_states.shape[-2]
            end = start + count
            positions = (cache_kwargs or {}).get("cache_position")
            if (count < 1 or end > capacity or not isinstance(positions, torch.Tensor) or
                    positions.dtype != torch.long or positions.device.type != "cpu" or
                    positions.ndim != 1 or positions.numel() != count or
                    not torch.equal(positions, torch.arange(start, end, dtype=torch.long))):
                raise ValueError("Cache writes must append consecutive absolute positions without eviction")
            if self.key_cache[layer_idx] is None:
                shape = (1, heads, capacity, head_dim)
                self.key_cache[layer_idx] = torch.empty(shape, dtype=torch.float32, device="cpu")
                self.value_cache[layer_idx] = torch.empty(shape, dtype=torch.float32, device="cpu")
            self.key_cache[layer_idx][:, :, start:end, :].copy_(key_states)
            self.value_cache[layer_idx][:, :, start:end, :].copy_(value_states)
            self.lengths[layer_idx] = end
            self.updates += 1
            return (self.key_cache[layer_idx][:, :, :end, :],
                    self.value_cache[layer_idx][:, :, :end, :])

        def get_seq_length(self, layer_idx=0):
            if type(layer_idx) is not int or not 0 <= layer_idx < layers:
                raise ValueError("Unknown cache layer")
            return self.lengths[layer_idx]

        def get_max_cache_shape(self):
            return capacity

        def get_usable_length(self, new_seq_length, layer_idx=0):
            length = self.get_seq_length(layer_idx)
            if type(new_seq_length) is not int or new_seq_length < 0 or length + new_seq_length > capacity:
                raise ValueError("Cache eviction is forbidden")
            return length

        def reorder_cache(self, beam_idx):
            raise ValueError("Only one greedy sequence is supported")

        def all_layers_at(self, length):
            return all(value == length for value in self.lengths)

        def receipt(self):
            return {"schema": "loom-prefix-cache/v1", "capacity_tokens": capacity,
                "layer_lengths": list(self.lengths), "allocated_layers": sum(x is not None for x in self.key_cache),
                "storage_bytes": sum(x.numel() * x.element_size() for x in self.key_cache + self.value_cache if x is not None),
                "updates": self.updates, "growing_concatenations": 0,
                "returned_views": "initialized_prefix_only", "evicted_tokens": 0,
                "dtype": "float32", "device": "cpu", "actual_peak_rss_bytes": None}

    return PrefixCache()


@contextmanager
def grouped_kv_attention(model):
    """Temporarily bind this Qwen3 instance to SDPA without full repeated K/V.

    Keep the installed projection/RoPE/cache forward bytecode. Only its private
    attention-interface lookup changes; neither the class nor global registry is
    modified. Single-request inference only. Kernel workspace and RSS are unknown.
    """
    from types import FunctionType, MethodType
    import torch
    import transformers
    from transformers.models.qwen3.modeling_qwen3 import Qwen3Attention, Qwen3ForCausalLM
    if (torch.__version__.split("+")[0] != "2.2.2" or transformers.__version__ != "4.51.3" or
            type(model) is not Qwen3ForCausalLM or model.training or
            model.config._attn_implementation != "sdpa"):
        raise ValueError("Grouped KV attention requires the reviewed eval Qwen3 CPU runtime")
    _, _, layers, heads, kv_heads, head_dim, _ = _dimensions(model.config.to_dict())
    modules = [layer.self_attn for layer in model.model.layers]
    original = Qwen3Attention.forward
    if len(modules) != layers or any(type(m) is not Qwen3Attention or
            getattr(m.forward, "__func__", None) is not original or m.training or
            m.num_key_value_groups != heads // kv_heads or m.head_dim != head_dim or
            m.sliding_window is not None for m in modules):
        raise ValueError("Unexpected attention instance, existing binding or geometry")
    receipt = {"schema": "loom-grouped-kv-sdpa/v1", "policy": "one_kv_head_per_sdpa_call",
        "attention_calls": 0, "sdpa_calls": 0, "max_key_tokens": 0,
        "max_reference_repeated_kv_bytes": 0, "materialized_repeated_kv_bytes": 0,
        "allocation_accounting": "explicit_helper_operations_only", "backend_workspace_measured": False,
        "kv_views_share_cache_storage": True, "global_registry_modified": False,
        "instance_bindings_restored": False, "actual_peak_rss_bytes": None}

    def grouped(module, query, key, value, attention_mask, dropout=0.0,
                scaling=None, is_causal=None, **kwargs):
        if (module.training or torch.is_grad_enabled() or dropout != 0.0 or
                kwargs.get("sliding_window") is not None or kwargs.get("output_attentions", False) or
                any(x.ndim != 4 or x.device.type != "cpu" or x.dtype != torch.float32
                    for x in (query, key, value)) or query.shape[:2] != (1, heads) or
                key.shape != value.shape or key.shape[:2] != (1, kv_heads) or
                query.shape[-1] != head_dim or key.shape[-1] != head_dim or
                scaling != module.scaling or type(is_causal) not in (bool, type(None))):
            raise ValueError("Unsupported grouped SDPA input, mode or scaling")
        qlen, klen = query.shape[-2], key.shape[-2]
        if not 1 <= qlen <= klen <= 8192:
            raise ValueError("Grouped SDPA requires bounded initialized causal history")
        mask = attention_mask
        if mask is not None:
            if (mask.ndim != 4 or mask.device.type != "cpu" or
                    mask.dtype not in (torch.float32, torch.bool) or mask.shape[0] != 1 or
                    mask.shape[1] not in (1, heads) or mask.shape[2] not in (1, qlen) or
                    mask.shape[3] < klen):
                raise ValueError("Unsupported grouped attention mask")
            mask = mask[..., :klen]
        causal = qlen > 1 and mask is None if is_causal is None else is_causal
        groups = heads // kv_heads
        output = torch.empty((1, qlen, heads, head_dim), dtype=query.dtype, device=query.device)
        for index in range(kv_heads):
            start, end = index * groups, (index + 1) * groups
            # expand uses zero-stride views, unlike repeat_kv(...).reshape(...)
            # followed by contiguous() over the full multi-head history.
            k = key[:, index:index + 1].expand(1, groups, klen, head_dim)
            v = value[:, index:index + 1].expand(1, groups, klen, head_dim)
            group_mask = mask if mask is None or mask.shape[1] == 1 else mask[:, start:end]
            part = torch.nn.functional.scaled_dot_product_attention(
                query[:, start:end].contiguous(), k, v, attn_mask=group_mask,
                dropout_p=0.0, scale=scaling, is_causal=causal)
            output[:, :, start:end].copy_(part.transpose(1, 2))
            del part, k, v
        receipt["attention_calls"] += 1
        receipt["sdpa_calls"] += kv_heads
        receipt["max_key_tokens"] = max(receipt["max_key_tokens"], klen)
        receipt["max_reference_repeated_kv_bytes"] = max(
            receipt["max_reference_repeated_kv_bytes"], 2 * heads * klen * head_dim * 4)
        return output, None

    private = FunctionType(original.__code__, {**original.__globals__,
        "ALL_ATTENTION_FUNCTIONS": {"sdpa": grouped}}, original.__name__,
        original.__defaults__, original.__closure__)
    private.__kwdefaults__ = original.__kwdefaults__

    def forward(module, *args, **kwargs):
        if module.training or module.config._attn_implementation != "sdpa" or kwargs.get("output_attentions", False):
            raise ValueError("Grouped attention cannot fall back to another implementation")
        return private(module, *args, **kwargs)

    saved = [(m, "forward" in m.__dict__, m.__dict__.get("forward")) for m in modules]
    try:
        for module in modules:
            module.forward = MethodType(forward, module)
        yield receipt
    finally:
        for module, existed, value in saved:
            if existed:
                module.forward = value
            else:
                module.__dict__.pop("forward", None)
        receipt["instance_bindings_restored"] = True


class _ProgressStreamer:
    """Keep prompt tokens separate from actual generated tokens."""

    def __init__(self, prompt, tokenizer, callback):
        self.prompt, self.tokenizer, self.callback = prompt, tokenizer, callback
        self.prompt_seen = False
        self.token_ids = []
        self.last_emitted = 0

    def put(self, value):
        values = value.tolist()
        if not self.prompt_seen:
            if values != [self.prompt]:
                raise ValueError("Generation streamer did not receive the exact full prompt first")
            self.prompt_seen = True
            return
        if not isinstance(values, list) or len(values) != 1 or type(values[0]) is not int:
            raise ValueError("Expected one generated token for the single-request arm")
        self.token_ids.append(values[0])
        if len(self.token_ids) == 1 or len(self.token_ids) - self.last_emitted >= STREAM_INTERVAL:
            self._emit()

    def _emit(self):
        if self.callback is not None and self.token_ids:
            self.callback({"phase": "PARTIAL_GENERATION", "complete": False,
                "status": "PARTIAL_UNVALIDATED", "generated_tokens": len(self.token_ids),
                "token_ids": list(self.token_ids),
                "response_text": self.tokenizer.decode(self.token_ids, skip_special_tokens=True)})
        self.last_emitted = len(self.token_ids)

    def end(self):
        if len(self.token_ids) != self.last_emitted:
            self._emit()


def predict(adapter, request, *, adapter_enabled, max_total_tokens,
            max_new_tokens, encoder=None, progress_callback=None):
    """Mirror LocalAdapter.predict with a new, explicitly recorded execution mode.

Each call creates a fresh full-history cache. All but the final prompt token
are prefetched in continuous chunks; installed generate receives the complete
original IDs/mask and handles the final prompt token and normal greedy decoding.
"""
    import torch
    from transformers import GenerationConfig
    from lodestone_machinery.loom_adapter_local import MANIFEST_SHA256

    if encoder is None:
        from .loom_adapter import encode_prediction
        encoder = encode_prediction
    if type(adapter_enabled) is not bool or not callable(encoder):
        raise ValueError("Explicit adapter mode and encoder are required")
    if type(max_total_tokens) is not int or not 1 <= max_total_tokens <= 8192:
        raise ValueError("Total tokens exceed the comparison envelope")
    if type(max_new_tokens) is not int or not 1 <= max_new_tokens <= 512 or max_new_tokens >= max_total_tokens:
        raise ValueError("Output tokens exceed the comparison envelope")
    started, cpu_started = time.monotonic(), time.process_time()
    encoded = encoder(request, adapter.tokenizer, max_total_tokens=max_total_tokens,
                      max_new_tokens=max_new_tokens)
    prompt = encoded["input_ids"]
    mask_values = encoded["attention_mask"]
    if (type(prompt) is not list or not prompt or
            any(type(token) is not int or token < 0 for token in prompt) or
            mask_values != [1] * len(prompt) or encoded["prompt_tokens"] != len(prompt) or
            len(prompt) + max_new_tokens > max_total_tokens):
        raise ValueError("Expected complete, unpadded, bounded encoded input")
    if adapter_enabled and not all(module.enabled is True for module in adapter.bundle.modules.values()):
        raise ValueError("All adapters must be enabled for the candidate arm")
    state_sha = sha(adapter.state())
    adapter.assets.verify_unchanged()
    model_config = adapter.model.config.to_dict()
    _dimensions(model_config)
    if adapter.model.config._attn_implementation != "sdpa":
        raise ValueError("Only the reviewed CPU SDPA attention path is supported")
    parameters = list(adapter.model.parameters())
    if any(p.device.type != "cpu" or p.dtype != torch.float32 for p in parameters):
        raise ValueError("Only unchanged CPU float32 parameters are supported")
    parameter_bytes = sum(p.numel() * p.element_size() for p in parameters)
    if (model_config["tie_word_embeddings"] and
            adapter.model.get_input_embeddings().weight.data_ptr() !=
            adapter.model.get_output_embeddings().weight.data_ptr()):
        raise ValueError("Configured tied embeddings do not share actual storage")
    assessment = assess_memory(model_config, prompt_tokens=len(prompt), max_new_tokens=max_new_tokens)
    if assessment["lower_bound_exceeds_rss_cap"]:
        raise ValueError("Float32 weights and full-history cache alone exceed the unchanged RSS cap")
    adapter.model.eval()
    ids = torch.tensor([prompt], dtype=torch.long)
    mask = torch.tensor([mask_values], dtype=torch.long)
    generation_config = GenerationConfig(do_sample=False, max_new_tokens=max_new_tokens,
        eos_token_id=151645, pad_token_id=151643, bos_token_id=151643, use_cache=True,
        prefill_chunk_size=None, disable_compile=True)
    contract = {"do_sample": False, "max_new_tokens": max_new_tokens,
        "use_model_defaults": False, "max_total_tokens": max_total_tokens,
        "eos_token_id": 151645, "pad_token_id": 151643, "bos_token_id": 151643,
        "use_cache": True, "attention_implementation": "sdpa", "dtype": "float32",
        "device": "cpu", "enable_thinking": False,
        "execution_mode": "grouped_kv_sdpa_prefix_cache_chunked_prefill_then_installed_generate",
        "attention_allocation_policy": "per_kv_head_zero_stride_views_no_full_repeated_kv",
        "prefill_chunk_tokens": CHUNK_TOKENS, "cache_policy": "fresh_fixed_capacity_initialized_prefix_views",
        "cache_capacity_tokens": len(prompt) + max_new_tokens - 1,
        "compiled_forward": False, "prompt_truncated": False,
        "monolithic_numerical_equivalence": "NOT_ESTABLISHED",
        "encoding_contract": encoded.get("encoding_contract", {}),
        "encoding_contract_sha256": sha(encoded.get("encoding_contract", {}))}
    if (encoded.get("encoding_contract_sha256") is not None and
            encoded["encoding_contract_sha256"] != contract["encoding_contract_sha256"]):
        raise ValueError("Encoder contract digest differs")
    streamer = _ProgressStreamer(prompt, adapter.tokenizer, progress_callback)
    cache, output, generated = None, None, None
    mode = nullcontext() if adapter_enabled else adapter.bundle.baseline()
    try:
        with mode, torch.inference_mode(), grouped_kv_attention(adapter.model) as attention_receipt:
            cache = make_prefix_cache(model_config, len(prompt) + max_new_tokens - 1)
            for start in range(0, len(prompt) - 1, CHUNK_TOKENS):
                end = min(start + CHUNK_TOKENS, len(prompt) - 1)
                positions = torch.arange(start, end, dtype=torch.long, device=ids.device)
                output = adapter.model(input_ids=ids[:, start:end], attention_mask=mask[:, :end],
                    past_key_values=cache, cache_position=positions, position_ids=positions.unsqueeze(0),
                    use_cache=True, logits_to_keep=1, return_dict=True,
                    output_attentions=False, output_hidden_states=False)
                cache = output.past_key_values
                if not cache.all_layers_at(end):
                    raise ValueError("Prefill cache lost continuous prompt positions")
                output = None
                if progress_callback is not None:
                    progress_callback({"phase": "PREFILL_CHUNK_COMPLETE", "complete": False,
                        "prefill_tokens": end, "prompt_tokens": len(prompt), "generated_tokens": 0})
            if cache.get_seq_length() != len(prompt) - 1:
                raise ValueError("Final prompt token must remain for normal generation")
            if progress_callback is not None:
                progress_callback({"phase": "DECODING", "complete": False,
                    "prefill_tokens": len(prompt) - 1, "prompt_tokens": len(prompt), "generated_tokens": 0})
            generated = adapter.model.generate(input_ids=ids, attention_mask=mask,
                past_key_values=cache, generation_config=generation_config,
                use_model_defaults=False, do_sample=False, logits_to_keep=1, streamer=streamer)
            if generated[0, :len(prompt)].tolist() != prompt:
                raise ValueError("Normal generation changed the complete prompt prefix")
            tokens = generated[0, len(prompt):].tolist()
            if not cache.all_layers_at(len(prompt) + len(tokens) - 1):
                raise ValueError("Generation left inconsistent initialized cache prefixes")
            cache_receipt = cache.receipt()
            if not streamer.prompt_seen or tokens != streamer.token_ids:
                raise ValueError("Returned generation differs from actual streamed tokens")
    finally:
        # No base-arm cache or output tensor may survive into the candidate arm.
        cache = output = generated = None
    adapter.assets.verify_unchanged()
    if sha(adapter.state()) != state_sha:
        raise ValueError("Comparison generation changed adapter weights")
    complete = bool(tokens) and tokens[-1] == 151645
    return {"schema": "loom-adapter-contribution/v1", "request_sha256": sha(request),
        "base_manifest_sha256": MANIFEST_SHA256, "adapter_state_sha256": state_sha,
        "prompt_tokens_sha256": sha(prompt), "generation_contract": contract,
        "generation_contract_sha256": sha(contract), "adapter_enabled": adapter_enabled,
        "status": "GENERATED_UNVALIDATED" if complete else "OUTPUT_LIMIT_REACHED",
        "response_text": adapter.tokenizer.decode(tokens, skip_special_tokens=True),
        "generated_tokens": len(tokens), "token_ids": tokens, "prompt_tokens": len(prompt),
        "wall_seconds": time.monotonic() - started, "process_cpu_seconds": time.process_time() - cpu_started,
        "provider_calls": 0, "weight_updates": 0, "cache_receipt": cache_receipt,
        "attention_allocation_receipt": attention_receipt,
        "memory_assessment": assessment, "observed_parameter_storage_bytes": parameter_bytes,
        "semantic_success_established": False,
        "record_promotion": False, "execution_authority": False}
