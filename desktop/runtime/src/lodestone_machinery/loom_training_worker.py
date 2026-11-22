"""Worker for explicit supervised pilots; never launched by importing a module."""
import argparse
import os
from pathlib import Path

from .loom_adapter import validate_training_dataset
from .loom_adapter_local import LocalAdapter, VerifiedAssets
from .loom_runtime import _json, _save
from .loom_runtime_worker import write_json
from .loom_training import (_sync_directory, bindings_for, digest, initial_state_for, initialization_for,
                            publish_checkpoint, validate_job)


def run(root):
    root = Path(root)
    job = validate_job(_json(root / "job.json"))
    dataset, splits = _json(root / "dataset.json"), _json(root / "splits.json")
    validate_training_dataset(dataset, splits)
    bindings = bindings_for(job, dataset, splits)
    if _json(root / "bindings.json") != bindings:
        raise ValueError("Training inputs differ from their captured bindings")
    initial = initial_state_for(job, snapshot=root / "initial-adapter-state.json")
    initialization = initialization_for(job)
    progress = root / "progress.json"
    write_json(progress, {"phase": "VERIFYING_ASSETS", "durable_completed_updates": 0})
    assets = VerifiedAssets(job["assets_dir"])
    write_json(progress, {"phase": "LOADING_MODEL", "assets": assets.receipt(), "durable_completed_updates": 0})
    adapter = LocalAdapter(assets, seed=job["seed"])
    if initial is not None:
        write_json(progress, {"phase": "LOADING_INITIAL_ADAPTER", "durable_completed_updates": 0})
        adapter.load_state(initial)
        loaded_sha256 = digest(adapter.state())
        if loaded_sha256 != initialization["initial_adapter_state_sha256"]:
            raise ValueError("Loaded initial adapter differs from its captured state")
        initialization.update(verification="LOADED_AND_BOUND", loaded_adapter_state_sha256=loaded_sha256)
    else:
        initialization["verification"] = "FRESH_ADAPTER_CONSTRUCTED"
    del initial
    _save(root / "initialization.json", initialization)
    _sync_directory(root)
    def checkpoint(event):
        receipt = publish_checkpoint(root / "checkpoints", event, bindings, max_steps=job["max_steps"])
        write_json(progress, {"phase": "UPDATE_EXPORTED", "durable_completed_updates": event["completed_steps"],
                              "adapter_state_sha256": receipt["adapter_state_sha256"]})
    write_json(progress, {"phase": "TRAINING", "durable_completed_updates": 0})
    report = adapter.train(dataset, splits, max_steps=job["max_steps"], max_seconds=job["wall_seconds"],
                           max_total_tokens=job["max_total_tokens"], max_new_tokens=job["max_new_tokens"],
                           learning_rate=job["learning_rate"], seed=job["seed"], on_completed_update=checkpoint)
    result = {"status": report["status"], "completed_steps": report["completed_steps"],
              "bindings": bindings, "initialization": initialization, "report": report, "optimizer_state_retained": False,
              "rng_state_retained": False, "exact_resume_supported": False}
    _save(root / "result.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True)
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        run(args.run)
    except Exception as exc:
        # Do not assert zero updates: a worker may fail after an unexported update.
        _save(Path(args.run) / "worker-error.json", {"status": "FAILED", "error_type": type(exc).__name__,
                                                    "error": str(exc)[:1000], "completed_updates": "UNKNOWN"})
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
