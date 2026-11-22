"""One supervised local Loom inference; no training, credentials or cloud route."""
from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import time
import urllib.request

from .records import canonical

URL = "http://127.0.0.1:11435"
MAX_RESPONSE_BYTES = 65536
MAX_STREAM_BYTES = 2 * 1024 * 1024
TOKENIZER_FILES = ("tokenizer.json", "tokenizer_config.json", "vocab.json", "merges.txt",
                   "special_tokens_map.json", "added_tokens.json", "config.json")


def read_json(path, maximum=2 * 1024 * 1024):
    with Path(path).open("rb") as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError("Local input exceeds byte limit")
    return json.loads(raw)


def write_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as stream:
        stream.write(canonical(value))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def verified_tokenizer(root):
    """Verify actual tokenizer files without reading unused float32 weights."""
    from .loom_adapter_local import ASSETS, _tokenizer_versions, _verify_file
    _tokenizer_versions()
    root = Path(root)
    if not root.is_absolute() or root.resolve(strict=True) != root:
        raise ValueError("Tokenizer assets require a resolved absolute directory")
    identities = {}
    for name in TOKENIZER_FILES:
        identities[name] = _verify_file(root / name, ASSETS[name])
    from transformers import Qwen2TokenizerFast
    tokenizer = Qwen2TokenizerFast.from_pretrained(
        str(root), local_files_only=True, trust_remote_code=False)
    if tokenizer.eos_token_id != 151645 or tokenizer.pad_token_id != 151643:
        raise ValueError("Unexpected tokenizer identity")
    for name in TOKENIZER_FILES:
        if identities[name] != _verify_file(root / name, ASSETS[name]):
            raise ValueError("Tokenizer assets changed while loading")
    return tokenizer, {name: {"bytes": ASSETS[name][0], "sha256": ASSETS[name][1]}
                       for name in TOKENIZER_FILES}


def http_json(opener, path, body=None, timeout=10):
    data = None if body is None else canonical(body)
    request = urllib.request.Request(URL + path, data=data,
                                     headers={"Content-Type": "application/json"})
    with opener.open(request, timeout=timeout) as response:
        raw = response.read(MAX_STREAM_BYTES + 1)
    if len(raw) > MAX_STREAM_BYTES:
        raise ValueError("Local provider response exceeds byte limit")
    return json.loads(raw)


def start_ollama(config, log_path):
    """Own a private loopback server inside this worker's process group."""
    binary = Path(config["ollama_binary"])
    models = Path(config["ollama_models"])
    if not binary.is_absolute() or not binary.is_file() or not models.is_dir():
        raise ValueError("Existing local Ollama binary and model directory are required")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 11435))  # Never use another process's server.
    env = {key: os.environ[key] for key in ("HOME", "USER", "LOGNAME", "TMPDIR", "PATH")
           if key in os.environ}
    env.update({"OLLAMA_HOST": "127.0.0.1:11435", "OLLAMA_MODELS": str(models),
                "OLLAMA_NUM_PARALLEL": "1", "OLLAMA_MAX_LOADED_MODELS": "1",
                "OLLAMA_KEEP_ALIVE": "0", "OLLAMA_MAX_QUEUE": "1",
                "OLLAMA_CONTEXT_LENGTH": str(config["max_total_tokens"]),
                "OLLAMA_NOPRUNE": "1"})
    with open(log_path, "ab") as log:
        process = subprocess.Popen([str(binary), "serve"], env=env,
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=log)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        for _ in range(100):
            if process.poll() is not None:
                raise RuntimeError("Local model server exited during startup")
            try:
                version = http_json(opener, "/api/version", timeout=1)
                return process, opener, version
            except (OSError, ValueError):
                time.sleep(.1)
        raise TimeoutError("Local model server did not become ready")
    except BaseException:
        stop_server(process)
        raise


def stop_server(process):
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)
    # The parent supervisor also checks/kills the entire private process group,
    # including any model runner left behind by the server.


def run_ollama(config, request, partial_path, observation):
    from . import loom_adapter
    tokenizer, token_identity = verified_tokenizer(config["assets_dir"])
    maximum = config.get("generation", {}).get("max_tokens", config["max_new_tokens"])
    encoded = loom_adapter.encode_prediction(
        request, tokenizer, max_total_tokens=config["max_total_tokens"], max_new_tokens=maximum)
    prompt = loom_adapter.render_prompt(request, tokenizer)
    required_tokens = encoded["prompt_tokens"] + maximum
    effective_context = min(config["max_total_tokens"],
                            max(256, ((required_tokens + 255) // 256) * 256))
    # Allocate room for this complete encounter and its reserved output, rather
    # than paying for an unused 8192-token KV cache on every short encounter.
    assert effective_context >= required_tokens
    observation.update({"tokenizer_files": token_identity, "prompt_tokens": encoded["prompt_tokens"],
                        "rendered_prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                        "max_total_tokens": config["max_total_tokens"], "max_new_tokens": maximum,
                        "effective_context_tokens": effective_context,
                        "required_prompt_and_output_tokens": required_tokens,
                        "prompt_truncated": False, "adapter_enabled": False,
                        "weight_status": "QUANTIZED_BASE_NO_FITTED_LOOM_ADAPTER"})
    write_json(partial_path, {"response": "", "observation": observation})
    process = None
    text = ""
    try:
        process, opener, version = start_ollama(config, Path(partial_path).with_suffix(".server.log"))
        tags = http_json(opener, "/api/tags")
        selected = [row for row in tags.get("models", [])
                    if row.get("name") == config["ollama_model"]]
        if len(selected) != 1 or selected[0].get("digest") != config["ollama_model_digest"]:
            raise ValueError("Installed model differs from the configured local digest")
        observation.update({"model": selected[0]["name"], "model_digest": selected[0]["digest"],
                            "model_details": selected[0].get("details"), "server_version": version,
                            "endpoint": URL, "paid_api_calls": 0})
        body = {"model": config["ollama_model"], "prompt": prompt, "raw": True, "stream": True,
                "keep_alive": 0, "options": {"num_ctx": effective_context,
                    "num_predict": maximum, "num_thread": 2, "num_gpu": 0,
                    "temperature": 0, "seed": 0}}
        observation["provider_input_sha256"] = hashlib.sha256(canonical(body)).hexdigest()
        http_request = urllib.request.Request(URL + "/api/generate", data=canonical(body),
                                             headers={"Content-Type": "application/json"})
        final = None
        total = 0
        with opener.open(http_request, timeout=config["wall_seconds"]) as response:
            while True:
                raw = response.readline(131073)
                if not raw:
                    break
                total += len(raw)
                if len(raw) > 131072 or total > MAX_STREAM_BYTES:
                    raise ValueError("Provider stream exceeds byte limit")
                row = json.loads(raw)
                if row.get("error"):
                    raise RuntimeError("Local provider returned an error")
                piece = row.get("response", "")
                if type(piece) is not str:
                    raise ValueError("Malformed local response")
                text += piece
                if len(text.encode()) > MAX_RESPONSE_BYTES:
                    raise ValueError("Generated response exceeds retention limit")
                write_json(partial_path, {"response": text, "observation": observation})
                if row.get("done") is True:
                    final = row
                    break
        if final is None:
            return {"status": "FAILED", "response": text or None,
                    "observation": {**observation, "reason": "STREAM_ENDED_WITHOUT_TERMINAL_RESULT"}}
        observed_tokens = final.get("prompt_eval_count")
        observation.update({key: final.get(key) for key in (
            "done_reason", "prompt_eval_count", "eval_count", "total_duration", "load_duration",
            "prompt_eval_duration", "eval_duration")})
        observation["token_count_matches"] = observed_tokens == encoded["prompt_tokens"]
        if type(final.get("eval_count")) is not int or not 0 <= final["eval_count"] <= maximum:
            status = "FAILED"
            observation["reason"] = "INVALID_GENERATED_TOKEN_COUNT"
        elif type(observed_tokens) is not int or observed_tokens != encoded["prompt_tokens"]:
            status = "FAILED"
            observation["reason"] = "PROVIDER_TOKEN_COUNT_DIFFERS_FROM_PINNED_TOKENIZER"
        elif final.get("done_reason") != "stop":
            status = "FAILED"
            observation["reason"] = "OUTPUT_LIMIT_OR_INCOMPLETE_GENERATION"
        else:
            status = "COMPLETED" if text.strip() else "ABSTAINED"
        return {"status": status, "response": text or None, "observation": observation}
    finally:
        stop_server(process)


def run_native(config, request, partial_path, observation):
    from .loom_adapter_local import MANIFEST_SHA256, VerifiedAssets, LocalAdapter, load_tokenizer
    from .loom_adapter import encode_prediction
    from .loom_runtime import _adapter_document, runtime_identity
    if config.get("base_manifest_sha256") != MANIFEST_SHA256:
        raise ValueError("local_adapter requires the registered base/tokenizer manifest pin")
    state = _adapter_document(config)
    identity = runtime_identity(config)
    observation["configured_reader_identity"] = identity
    assets = VerifiedAssets(config["assets_dir"])
    maximum = config.get("generation", {}).get("max_tokens", config["max_new_tokens"])
    tokenizer = load_tokenizer(assets)
    encoded = encode_prediction(request, tokenizer, max_total_tokens=config["max_total_tokens"],
                                max_new_tokens=maximum)
    observation.update({"prompt_tokens": encoded["prompt_tokens"], "assets": assets.receipt()})
    prompt_tokens = encoded["prompt_tokens"]
    del tokenizer, encoded  # LocalAdapter owns the sole tokenizer during inference.
    gc.collect()
    write_json(partial_path, {"response": "", "observation": observation})
    adapter = LocalAdapter(assets)
    if state is not None:
        adapter.load_state(state)
        if hashlib.sha256(canonical(adapter.state())).hexdigest() != config["adapter_state_sha256"]:
            raise ValueError("Loaded adapter numeric state differs from configured artifact")
    observation["loaded_reader_identity"] = dict(identity, verification="LOADED_AND_BOUND")

    def retain_progress(event):
        if "response_text" not in event:
            return  # Prefill progress is not generated output.
        text, count, tokens = event["response_text"], event["generated_tokens"], event["token_ids"]
        if (type(text) is not str or len(text.encode("utf-8")) > MAX_RESPONSE_BYTES
                or type(count) is not int or not 1 <= count <= maximum
                or type(tokens) is not list or len(tokens) != count
                or any(type(token) is not int or token < 0 for token in tokens)):
            raise ValueError("Generated partial exceeds native retention bounds")
        partial_observation = dict(observation, generation_progress={
            "status": "PARTIAL_UNVALIDATED", "complete": False, "token_ids": tokens})
        if len(canonical(partial_observation)) > 16384:
            raise ValueError("Partial observation exceeds native retention bound")
        write_json(partial_path, {"response": text, "prompt_tokens": prompt_tokens,
                                 "generated_tokens": count, "observation": partial_observation})

    result = adapter.predict(request, adapter_enabled=state is not None,
                             max_total_tokens=config["max_total_tokens"], max_new_tokens=maximum,
                             progress_callback=retain_progress)
    observation.update({key: value for key, value in result.items() if key not in ("response_text", "token_ids")})
    response = result["response_text"]
    status = "COMPLETED" if result["status"] == "GENERATED_UNVALIDATED" else "FAILED"
    if status == "COMPLETED" and not response.strip():
        status = "ABSTAINED"
    return {"status": status, "response": response, "observation": observation}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("config", "request", "result", "partial"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args(argv)
    os.umask(0o077)
    started = time.monotonic()
    observation = {"source": "actual supervised local Loom worker", "training_steps": 0,
                   "weight_updates": 0, "semantic_learning_established": False}
    result = None
    try:
        config, request = read_json(args.config), read_json(args.request)
        if config.get("schema") != "loom-unified-runtime/v1":
            raise ValueError("Unsupported local runtime configuration")
        observation["backend"] = config["backend"]
        if config["backend"] == "ollama_qwen3_base":
            if config.get("adapter_state") is not None or config.get("adapter_state_sha256") is not None:
                raise ValueError("Quantized base route cannot load or silently ignore fitted adapter state")
            result = run_ollama(config, request, args.partial, observation)
        elif config["backend"] == "local_adapter":
            result = run_native(config, request, args.partial, observation)
        else:
            raise ValueError("Unsupported local model route")
    except Exception as exc:
        partial = read_json(args.partial) if Path(args.partial).exists() else {}
        result = {"status": "FAILED", "response": partial.get("response") or None,
                  "observation": {**observation, "error_type": type(exc).__name__,
                                  "error": str(exc)[:1000]}}
    result["observation"]["wall_seconds"] = time.monotonic() - started
    write_json(args.result, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
