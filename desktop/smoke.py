#!/usr/bin/env python3
"""Two actual local encounters checking generation and correction delivery.

Run with the installed worker Python. All encounters and failed attempts remain
in the selected preview's native history. This is an installation check, not a
semantic evaluation, training run or permission to retry a failed generation.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import traceback
import uuid


def read(path):
    return json.loads(Path(path).read_text())


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def check_run(result, root, config):
    """Bind the receipt to a real completed loopback worker and its files."""
    directory = Path(result["artifacts_dir"]).resolve()
    require(directory.is_relative_to(root / "Loom/runs"), "Run artifacts escaped the preview")
    summary = {"status": result.get("status"), "provider_called": result.get("provider_called"),
               "outcome_handle": result.get("outcome_handle"), "artifacts_dir": str(directory),
               "files": {p.name: sha256(p) for p in sorted(directory.iterdir()) if p.is_file()}}
    require(result.get("status") == "COMPLETED", "Encounter did not complete: " + str(result.get("status")))
    require(result.get("provider_called") is True, "No actual provider call was retained")
    require(isinstance(result.get("response"), str) and result["response"].strip(), "Empty local response")
    require(result.get("outcome_handle") and result.get("terminal_handle"), "Native terminal evidence is missing")
    worker = read(directory / "worker.json")
    generated = read(directory / "result.json")
    supervisor = read(directory / "supervisor.json")
    observation = generated["observation"]
    require(type(worker.get("pid")) is int and worker["pid"] > 0, "Worker process evidence is missing")
    require(worker.get("backend") == "ollama_qwen3_base", "Unexpected worker backend")
    require(generated.get("status") == "COMPLETED" and generated.get("response") == result["response"],
            "Native outcome differs from the completed worker response")
    require(observation.get("source") == "actual supervised local Loom worker", "Worker source differs")
    require(observation.get("endpoint") == "http://127.0.0.1:11435", "Worker was not the owned loopback reader")
    require(observation.get("model_digest") == config["ollama_model_digest"], "Loaded model digest differs")
    require(observation.get("paid_api_calls") == 0 and observation.get("weight_updates") == 0,
            "Worker did not report the expected local inference scope")
    require(observation.get("token_count_matches") is True and observation.get("done_reason") == "stop",
            "Token binding or terminal generation check failed")
    require(supervisor["observation"]["supervisor"]["stop"].get("confirmed_stopped") is True,
            "Worker process group was not confirmed stopped")
    summary.update({"worker_pid": worker["pid"], "model_digest": observation["model_digest"],
                    "prompt_tokens": observation["prompt_tokens"], "generated_tokens": observation["eval_count"],
                    "rendered_prompt_sha256": observation["rendered_prompt_sha256"],
                    "response_sha256": hashlib.sha256(result["response"].encode()).hexdigest()})
    return summary


def run(root, output, max_tokens):
    root, output = root.expanduser().resolve(), output.expanduser().resolve()
    require(not output.exists(), "Use a new output file; previous smoke evidence is preserved")
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {"schema": "lodestone-desktop-local-smoke/v1", "status": "RUNNING",
              "root": str(root), "started_at": datetime.now(timezone.utc).isoformat(),
              "max_encounters": 2, "max_tokens_per_encounter": max_tokens, "attempts": [],
              "paid_api_calls": 0, "weight_updates": 0, "semantic_learning": "NOT_ESTABLISHED",
              "scope": "Actual local generation, native outcome retention and exact later correction delivery; no answer-quality score."}
    save(output, report)
    bridge = None
    try:
        with (root / ".lifecycle.lock").open("a+") as lease:
            fcntl.flock(lease, fcntl.LOCK_SH | fcntl.LOCK_NB)
            active = read(root / "ACTIVE.json")
            require(active.get("schema") == "lodestone-desktop-active/v1", "Select a desktop preview installation")
            release = Path(active["release"]).resolve()
            require(release == root / "releases" / active["release_id"], "Active release is outside this preview")
            require(Path(sys.executable).resolve() == Path(active["worker_python"]).resolve(),
                    "Run smoke.py with the installed worker_python from ACTIVE.json")
            spec = importlib.util.spec_from_file_location("desktop_smoke_installer", release / "install.py")
            installer = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(installer)
            report["doctor"] = installer.doctor(root)
            config = read(root / "Loom/runtime.json")
            require(config.get("backend") == "ollama_qwen3_base" and config.get("adapter_state") is None,
                    "Smoke requires the local quantized base route")
            require(Path(config["learning_config"]).resolve() == root / "learning/ACTIVE.json",
                    "Smoke must use this preview's learning selection")
            memory = read(config["learning_config"])
            require(Path(memory["memory_workspace"]).resolve().is_relative_to(root),
                    "Smoke must not add practice to another installation")
            sys.path.insert(0, str(release / "runtime/src"))
            from lodestone_machinery.loom_runtime import LoomBridge
            bridge = LoomBridge(root / "Loom/runtime.json")
            report["release_id"] = active["release_id"]
            purpose = "Check local generation and correction delivery. Respond in one short sentence; this is not a semantic evaluation."
            for index in range(2):
                result = bridge.run(
                    encounter=("A fresh local application check needs a brief acknowledgement." if index == 0 else
                               "A later local application check needs a brief acknowledgement."),
                    purpose=purpose, context="This is an explicitly requested installation smoke check using authored test material.",
                    max_tokens=max_tokens)
                # Save the actual receipt before any assertion can fail.
                report["attempts"].append({"encounter": index + 1, "receipt": result})
                save(output, report)
                report["attempts"][-1]["verification"] = check_run(result, root, config)
                if index == 0:
                    correction = ("For subsequent installation checks, distinguish delivered advice from demonstrated learning. "
                                  "Delivery marker: desktop-smoke-" + uuid.uuid4().hex + ".")
                    captured = bridge.correct(result["outcome_handle"], correction=correction,
                        author="Lodestone Desktop installation smoke fixture",
                        observation="Authored delivery-check instruction; no claim that the preceding response was semantically wrong.")
                    report["correction"] = {"text": correction, "receipt": captured,
                                            "sha256": hashlib.sha256(correction.encode()).hexdigest()}
                    save(output, report)
                    require(captured.get("state") == "RETAINED_UNRESOLVED", "Correction was not retained as unresolved advice")
                else:
                    packet = read(Path(result["artifacts_dir"]) / "request.json")
                    require(correction in packet["context"], "Second actual request does not contain the exact retained correction")
                    from lodestone_machinery.loom_runtime_worker import verified_tokenizer
                    from lodestone_machinery.loom_adapter import render_prompt
                    tokenizer, _ = verified_tokenizer(config["assets_dir"])
                    rendered = render_prompt(packet, tokenizer)
                    require(correction in rendered, "Retained correction was omitted from the rendered provider input")
                    rendered_hash = hashlib.sha256(rendered.encode()).hexdigest()
                    require(rendered_hash == report["attempts"][-1]["verification"]["rendered_prompt_sha256"],
                            "Reconstructed correction-bearing prompt differs from the actual worker receipt")
                    report["delivery"] = {"exact_correction_in_second_request": True,
                                          "exact_correction_in_rendered_prompt": True,
                                          "rendered_prompt_sha256": rendered_hash,
                                          "semantic_use_assessed": False}
                save(output, report)
            report["status"] = "PASSED"
    except BaseException as error:
        report["status"] = "INTERRUPTED" if isinstance(error, (KeyboardInterrupt, SystemExit)) else "FAILED"
        report["error"] = {"type": type(error).__name__, "message": str(error), "traceback": traceback.format_exc()}
    finally:
        if bridge is not None:
            bridge.cancel_all()
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        save(output, report)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-tokens", type=int, choices=(128, 256), default=256)
    args = parser.parse_args()
    os.umask(0o077)
    result = run(args.root, args.output, args.max_tokens)
    print(json.dumps({"status": result["status"], "report": str(args.output),
                      "encounters_attempted": len(result["attempts"]), "semantic_learning": "NOT_ESTABLISHED"}, indent=2))
    raise SystemExit(0 if result["status"] == "PASSED" else 1)
