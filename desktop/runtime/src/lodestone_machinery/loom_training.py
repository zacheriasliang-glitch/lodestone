"""Opt-in outer supervisor and weights-only completed-update export contract.

This low-level runner grants no training authority and is not wired to the GUI,
practice command, scheduler, or native learning store. A future pilot caller must
provide Conscience admission, capture/recovery and outcome/correction retention.
Importing it performs no training. It never automatically retries or deploys.
"""
import errno
import hashlib
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

from .loom_adapter import validate_training_dataset

MAX_JOB_STEPS = 24  # reviewed dose-response ceiling; was 3 for the pilot cycles
from .loom_adapter_local import MANIFEST_SHA256, validate_state_document
from .loom_runtime import _bounded_number, _environment, _group_snapshot, _json, _save, _stop_group
from lodestone_nurarihyon.common import canonical


SCHEMA = "loom-supervised-training/v1"
CHECKPOINT = "loom-completed-update/v1"
LIMIT = 2 * 1024 * 1024


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def validate_job(job):
    if type(job) is not dict or job.get("schema") != SCHEMA:
        raise ValueError("Expected supervised training job")
    for name in ("python", "assets_dir", "runs_dir"):
        if type(job.get(name)) is not str or not Path(job[name]).is_absolute():
            raise ValueError(name + " must be an absolute local path")
    if job.get("base_manifest_sha256") != MANIFEST_SHA256:
        raise ValueError("Training base/tokenizer manifest differs")
    # A dose-response cycle may take up to 24 updates; 3-update pilots showed balance shifts of ~0.0003,
    # too small to read. Every other bound (wall, RSS, tokens, learning rate) is unchanged.
    _bounded_number(job.get("max_steps"), "max_steps", 1, MAX_JOB_STEPS, integer=True)
    _bounded_number(job.get("wall_seconds"), "wall_seconds", 0.1, 1800)
    _bounded_number(job.get("max_rss_bytes"), "max_rss_bytes", 1, 4294967296, integer=True)
    _bounded_number(job.get("max_total_tokens"), "max_total_tokens", 2, 4096, integer=True)
    _bounded_number(job.get("max_new_tokens"), "max_new_tokens", 1, 256, integer=True)
    if job["max_new_tokens"] >= job["max_total_tokens"]:
        raise ValueError("Training must leave space for the complete prompt")
    _bounded_number(job.get("seed"), "seed", 0, 2 ** 31 - 1, integer=True)
    if job.get("learning_rate") != 1e-4 or type(job.get("learning_rate")) is not float:
        raise ValueError("Pilot learning rate must match the reviewed 0.0001 configuration")
    initial = job.get("initial_adapter_state")
    if initial is not None:
        if type(initial) is not dict or set(initial) != {"path", "sha256"}:
            raise ValueError("initial_adapter_state requires an explicit path and canonical SHA-256")
        if type(initial["path"]) is not str or not Path(initial["path"]).is_absolute():
            raise ValueError("Initial adapter path must be absolute")
        if type(initial["sha256"]) is not str or re.fullmatch(r"[0-9a-f]{64}", initial["sha256"]) is None:
            raise ValueError("Initial adapter requires its exact canonical SHA-256")
    return job


def initial_state_for(job, *, snapshot=None):
    """Validate a bounded weights-only parent without loading any model.

    The supervisor reads the explicit source once. The worker reads only the
    captured snapshot, so subsequent changes to that source cannot change a run.
    """
    validate_job(job)
    initial = job.get("initial_adapter_state")
    if initial is None:
        return None
    state = validate_state_document(_json(snapshot if snapshot is not None else initial["path"], LIMIT))
    if digest(state) != initial["sha256"]:
        raise ValueError("Initial adapter state differs from its canonical SHA-256")
    return state


def initialization_for(job):
    initial = job.get("initial_adapter_state")
    return {"mode": "WEIGHTS_ONLY_CONTINUATION" if initial is not None else "FRESH_ADAPTER",
            "initial_adapter_state_sha256": initial["sha256"] if initial is not None else None,
            "optimizer": "FRESH_ADAMW", "rng": "RESEEDED_FROM_JOB_SEED", "seed": job["seed"],
            "optimizer_state_retained": False, "rng_state_retained": False,
            "exact_resume_supported": False}


def bindings_for(job, dataset, split_manifest):
    bindings = {"job_sha256": digest(job), "dataset_sha256": digest(dataset),
                "split_manifest_sha256": digest(split_manifest),
                "base_manifest_sha256": MANIFEST_SHA256}
    # Keep historical fresh-run bindings readable; continuation adds its parent.
    if job.get("initial_adapter_state") is not None:
        bindings["initial_adapter_state_sha256"] = job["initial_adapter_state"]["sha256"]
    return bindings


def _sync_directory(path):
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _step_limit(max_steps):
    _bounded_number(max_steps, "max_steps", 1, MAX_JOB_STEPS, integer=True)
    return max_steps


def publish_checkpoint(directory, event, bindings, *, max_steps=3):
    """Publish one complete immutable state+receipt directory after an update.

    ``max_steps`` is the run's own requested limit (at most MAX_JOB_STEPS). The
    default keeps the original 3-update pilots readable exactly as before.

    A killed writer leaves only a hidden staging directory. It is never counted
    as a completed export. No optimizer state or random-generator state is saved.
    """
    step = event.get("completed_steps")
    _bounded_number(step, "completed_steps", 1, _step_limit(max_steps), integer=True)
    losses = event.get("training_losses")
    if (type(losses) is not list or len(losses) != step or
            any(type(x) not in (int, float) or not math.isfinite(x) for x in losses)):
        raise ValueError("Completed update requires its finite loss history")
    drawn = event.get("drawn_rows")
    if drawn is not None and (type(drawn) is not list or len(drawn) != step or
                              any(type(i) is not int or i < 0 for i in drawn)):
        raise ValueError("Recorded row exposure must list one row index per completed update")
    state = validate_state_document(event.get("adapter_state"))
    root = Path(directory)
    root.mkdir(mode=0o700, exist_ok=True)
    destination = root / ("step-%04d" % step)
    if destination.exists() or (step > 1 and not (root / ("step-%04d" % (step - 1))).is_dir()):
        raise ValueError("Checkpoint steps must be new and consecutive")
    temporary = root / (".step-%04d-%s.tmp" % (step, uuid.uuid4().hex))
    temporary.mkdir(mode=0o700)
    receipt = {"schema": CHECKPOINT, "completed_steps": step,
               "training_losses": losses, "adapter_state_sha256": digest(state),
               "bindings": bindings, "optimizer_state_retained": False,
               "rng_state_retained": False, "exact_resume_supported": False,
               "status": "FIT_UNVERIFIED", "semantic_learning_established": False}
    if drawn is not None:
        receipt["drawn_rows"] = drawn
    _save(temporary / "adapter-state.json", state)
    _save(temporary / "receipt.json", receipt)
    _sync_directory(temporary)
    os.rename(temporary, destination)
    _sync_directory(root)
    return receipt


def completed_checkpoints(directory, bindings, *, max_steps=3):
    limit = _step_limit(max_steps)
    root = Path(directory)
    if not root.exists():
        return []
    entries = sorted(p for p in root.iterdir() if not p.name.startswith("."))
    result = []
    for step, path in enumerate(entries, 1):
        if path.name != "step-%04d" % step or path.is_symlink() or not path.is_dir() or step > limit:
            raise ValueError("Invalid completed-update inventory")
        state = validate_state_document(_json(path / "adapter-state.json", LIMIT))
        receipt = _json(path / "receipt.json", 65536)
        if (receipt.get("schema") != CHECKPOINT or receipt.get("completed_steps") != step or
                receipt.get("bindings") != bindings or receipt.get("adapter_state_sha256") != digest(state) or
                receipt.get("optimizer_state_retained") is not False or
                receipt.get("rng_state_retained") is not False or
                receipt.get("exact_resume_supported") is not False):
            raise ValueError("Completed-update receipt does not bind its state/run")
        result.append({"completed_steps": step, "adapter_state_sha256": digest(state),
                       "adapter_state": str(path / "adapter-state.json"), "receipt": str(path / "receipt.json")})
    return result


class TrainingSupervisor:
    """Explicit low-level invocation only; caller owns admission and retention."""

    def run(self, job, dataset, split_manifest, *, cancel_event=None):
        if sys.platform == "win32":
            raise OSError(errno.ENOTSUP, "Training execution is unavailable on Windows; its POSIX supervision contract is required")
        import fcntl
        validate_job(job)
        validate_training_dataset(dataset, split_manifest)
        if any(len(canonical(value)) > LIMIT for value in (job, dataset, split_manifest)):
            raise ValueError("Training input exceeds the private artifact byte bound")
        if cancel_event is not None and not callable(getattr(cancel_event, "is_set", None)):
            raise ValueError("Cancellation must provide is_set()")
        runs = Path(job["runs_dir"])
        runs.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (runs / ".training.lock").open("a") as lock:
            os.chmod(lock.name, 0o600)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("Another training supervisor owns this run directory") from exc
            if (runs / "TRAINING_STOP_UNCONFIRMED.json").exists():
                raise RuntimeError("Previous training process group is not confirmed stopped")
            return self._run(job, dataset, split_manifest, cancel_event)

    def _run(self, job, dataset, split_manifest, cancel_event):
        initial = initial_state_for(job)
        root = Path(job["runs_dir"]) / ("training-" + uuid.uuid4().hex)
        root.mkdir(mode=0o700)
        bindings = bindings_for(job, dataset, split_manifest)
        for name, value in (("job", job), ("dataset", dataset), ("splits", split_manifest), ("bindings", bindings)):
            _save(root / (name + ".json"), value)
        if initial is not None:
            _save(root / "initial-adapter-state.json", initial)
        del initial
        _sync_directory(root)
        command = [job["python"], "-B", "-m", "lodestone_machinery.loom_training_worker", "--run", str(root)]
        child, reason, peak, samples = None, None, 0, 0
        started, next_sample = time.monotonic(), 0
        cleanup = {"confirmed_stopped": True}
        cancelled = lambda: cancel_event is not None and cancel_event.is_set()
        try:
            if cancelled():
                reason = "cancelled_before_start"
            else:
                with (root / "stdout.log").open("xb") as stdout, (root / "stderr.log").open("xb") as stderr:
                    os.chmod(stdout.name, 0o600)
                    os.chmod(stderr.name, 0o600)
                    child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                             cwd=str(root), env=_environment("local_adapter"), start_new_session=True)
                    _save(root / "worker.json", {"pid": child.pid, "pgid": child.pid})
                    while child.poll() is None:
                        if cancelled():
                            reason = "cancelled"
                            break
                        if time.monotonic() - started >= job["wall_seconds"]:
                            reason = "wall_limit"
                            break
                        if time.monotonic() >= next_sample:
                            sample = _group_snapshot(child.pid)
                            samples += 1
                            peak = max(peak, sum(row["rss_bytes"] for row in sample if not row["zombie"]))
                            if peak > job["max_rss_bytes"]:
                                reason = "rss_limit"
                                break
                            if any((root / name).stat().st_size > LIMIT for name in ("stdout.log", "stderr.log")):
                                reason = "log_byte_limit"
                                break
                            next_sample = time.monotonic() + 0.5
                        time.sleep(0.05)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            reason = "supervisor_error:" + type(exc).__name__
        finally:
            if child is not None:
                try:
                    cleanup = _stop_group(child)
                except (OSError, ValueError, subprocess.SubprocessError) as exc:
                    cleanup = {"confirmed_stopped": False, "error": type(exc).__name__}
                if not cleanup["confirmed_stopped"]:
                    _save(Path(job["runs_dir"]) / "TRAINING_STOP_UNCONFIRMED.json",
                          {"pgid": child.pid, "artifacts_dir": str(root), "stop": cleanup})
                    reason = "worker_group_stop_unconfirmed"
        checkpoints, worker, initialization = [], None, None
        try:
            checkpoints = completed_checkpoints(root / "checkpoints", bindings, max_steps=job["max_steps"])
            if job.get("initial_adapter_state") is not None:
                expected = dict(initialization_for(job), verification="LOADED_AND_BOUND",
                                loaded_adapter_state_sha256=job["initial_adapter_state"]["sha256"])
                if (root / "initialization.json").exists():
                    observed = _json(root / "initialization.json", 65536)
                    if observed != expected:
                        raise ValueError("Training initialization does not bind its loaded adapter")
                    initialization = observed
                elif checkpoints or reason is None:
                    raise ValueError("Training initialization receipt is missing")
            if reason is None:
                if child.returncode != 0:
                    raise ValueError("Training worker exited unsuccessfully")
                worker = _json(root / "result.json")
                if (worker.get("status") != "FIT_UNVERIFIED" or worker.get("bindings") != bindings or
                        worker.get("completed_steps") != len(checkpoints) or not checkpoints or
                        len(checkpoints) > job["max_steps"]):
                    raise ValueError("Training result and completed exports disagree")
                if job.get("initial_adapter_state") is not None:
                    if worker.get("initialization") != initialization:
                        raise ValueError("Training result does not bind its loaded initial adapter")
        except (OSError, ValueError, TypeError, KeyError) as exc:
            reason = reason or "invalid_worker_artifact:" + type(exc).__name__
        result = {"schema": "loom-training-supervisor-result/v1",
                  "status": "FIT_UNVERIFIED" if reason is None else (
                      "INTERRUPTED" if reason in ("cancelled_before_start", "cancelled", "wall_limit", "rss_limit") else "FAILED"),
                  "reason": reason, "bindings": bindings, "artifacts_dir": str(root),
                  "initialization_contract": initialization_for(job),
                  "initialization": initialization,
                  "durable_completed_updates": len(checkpoints), "checkpoints": checkpoints,
                  "uncheckpointed_update_possible": child is not None and reason is not None,
                  "optimizer_state_retained": False, "rng_state_retained": False,
                  "exact_resume_supported": False, "automatic_retry": False, "deployed": False,
                  "semantic_learning_established": False, "worker": worker,
                  "observation": {"elapsed_seconds": time.monotonic() - started,
                                  "observed_group_peak_rss_bytes": peak, "rss_samples": samples,
                                  "rss_accounting": "sampled_entire_owned_process_group",
                                  "stop": cleanup, "worker_exit_code": child.returncode if child else None}}
        _save(root / "supervisor.json", result)
        return result
