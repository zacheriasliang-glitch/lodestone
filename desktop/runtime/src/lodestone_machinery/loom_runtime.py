"""Local Loom interface bridge with bounded, separately owned inference workers.

The bridge uses the native learning cycle for each encounter and feedback review.
It neither trains weights nor treats successful generation as demonstrated learning.
The default quantized Qwen3 base is explicitly distinct from a fitted LocalAdapter.
RSS is an observed process-group bound, not a guarantee against between-sample peaks.
"""
import json
import hashlib
import math
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
import uuid
import re

from lodestone_nurarihyon.common import canonical, read_bytes, strict_json
from .loom_unified import UnifiedLoom


SCHEMA = "loom-unified-runtime/v1"
ROUTE = "loom-qwen3-base"
TURN_WAIT_SECONDS = 1800  # a Send waits its turn behind a weaving encounter instead of failing busy
STATUSES = {"COMPLETED", "FAILED", "ABSTAINED", "REFUSED", "INTERRUPTED"}
_MAX_FILE = 2 * 1024 * 1024


def _json(path, limit=_MAX_FILE):
    return strict_json(read_bytes(path, limit), limit)


def _save(path, value):
    """Publish a private, complete local artifact without replacing an old one."""
    data = canonical(value)
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _bounded_number(value, name, low, high, *, integer=False):
    types = (int,) if integer else (int, float)
    if type(value) not in types or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(name + " is outside the runtime resource envelope")
    return value


def _config(path):
    value = _json(path, 65536)
    if type(value) is not dict or value.get("schema") != SCHEMA:
        raise ValueError("Expected Loom unified runtime configuration")
    if value.get("backend") not in ("ollama_qwen3_base", "local_adapter"):
        raise ValueError("Unknown explicit local Loom backend")
    for field in ("learning_config", "python", "assets_dir", "runs_dir"):
        if type(value.get(field)) is not str or not Path(value[field]).is_absolute():
            raise ValueError(field + " must be an absolute local path")
    _bounded_number(value.get("max_total_tokens"), "max_total_tokens", 128, 8192, integer=True)
    _bounded_number(value.get("max_new_tokens"), "max_new_tokens", 1, 512, integer=True)
    _bounded_number(value.get("wall_seconds"), "wall_seconds", 0.1, 1200)
    _bounded_number(value.get("max_rss_bytes"), "max_rss_bytes", 1, 4294967296, integer=True)
    if value["backend"] == "ollama_qwen3_base":
        for field in ("ollama_binary", "ollama_models"):
            if type(value.get(field)) is not str or not Path(value[field]).is_absolute():
                raise ValueError(field + " must be an absolute local path")
        if value.get("ollama_model") != "qwen3:0.6b":
            raise ValueError("Only the configured Qwen3 local base is admitted")
        if type(value.get("ollama_model_digest")) is not str or not value["ollama_model_digest"]:
            raise ValueError("The local model must have an explicit digest")
    if value.get("adapter_state") is not None:
        if type(value["adapter_state"]) is not str or not Path(value["adapter_state"]).is_absolute():
            raise ValueError("adapter_state must be an explicit absolute path or null")
    # Reject an incompatible artifact before native preparation or model loading.
    if value["backend"] == "ollama_qwen3_base":
        if value.get("adapter_state") is not None or value.get("adapter_state_sha256") is not None:
            raise ValueError("Quantized base route cannot load or ignore an adapter artifact")
    else:
        from .loom_adapter_local import MANIFEST_SHA256
        if value.get("base_manifest_sha256") != MANIFEST_SHA256:
            raise ValueError("local_adapter requires the registered base/tokenizer manifest pin")
        _adapter_document(value)
    return value


def _adapter_document(config):
    """Read and bind the exact artifact without importing a model framework."""
    from .loom_adapter_local import validate_state_document
    if config.get("adapter_state") is None:
        if config.get("adapter_state_sha256") is not None:
            raise ValueError("Adapter digest supplied without an adapter artifact")
        return None
    expected = config.get("adapter_state_sha256")
    if type(expected) is not str or re.fullmatch(r"[0-9a-f]{64}", expected) is None:
        raise ValueError("Adapter artifact requires its exact canonical SHA-256")
    state = validate_state_document(_json(config["adapter_state"]))
    if hashlib.sha256(canonical(state)).hexdigest() != expected:
        raise ValueError("Adapter state differs from configured digest")
    return state


def runtime_identity(config):
    """Declared configuration identity; worker receipts separately prove loading."""
    from .loom_adapter_local import ASSETS, MANIFEST_SHA256, REPOSITORY, REVISION, VERSIONS
    from .loom_adapter_torch import AdapterConfig
    adapted = config.get("adapter_state") is not None
    local = config["backend"] == "local_adapter"
    return {"schema": "loom-reader-identity/v1", "backend": config["backend"],
            "model": REPOSITORY, "revision": REVISION if local else None,
            "tokenizer_revision": REVISION,
            "base_manifest_sha256": MANIFEST_SHA256 if local else None,
            "tokenizer_files_sha256": {name: sha for name, (_, sha) in ASSETS.items()
                                       if name not in ("model.safetensors", "config.json")},
            "model_digest": config.get("ollama_model_digest") if config["backend"] == "ollama_qwen3_base" else None,
            "adapter_enabled": adapted,
            "adapter_state_sha256": config.get("adapter_state_sha256") if adapted else None,
            "adapter_config": AdapterConfig().document() if local else None,
            "runtime_config_sha256": hashlib.sha256(canonical(config)).hexdigest(),
            "dtype": "quantized" if config["backend"] == "ollama_qwen3_base" else "float32",
            "packages": dict(VERSIONS) if local else None, "verification": "CONFIGURATION_ONLY"}


def _route(config):
    if config["backend"] == "ollama_qwen3_base":
        return ROUTE
    if config.get("adapter_state") is None:
        return "loom-qwen3-local-base"
    return "loom-qwen3-local-adapter-" + config["adapter_state_sha256"]


def _profiles(path):
    primary = _config(path)
    paths = primary.get("additional_profiles", [])
    if (type(paths) is not list or len(paths) > 8 or
            any(type(p) is not str or not Path(p).is_absolute() for p in paths)):
        raise ValueError("additional_profiles requires at most eight absolute config paths")
    profiles = {_route(primary): primary}
    for path in paths:
        candidate = _config(path)
        if candidate.get("additional_profiles"):
            raise ValueError("Nested runtime profiles are not supported")
        route = _route(candidate)
        if route in profiles:
            raise ValueError("Duplicate runtime profile identity")
        # Every GUI arm shares the actual retained-learning workspace and history.
        if candidate["learning_config"] != primary["learning_config"] or candidate["runs_dir"] != primary["runs_dir"]:
            raise ValueError("Runtime profiles must share learning_config and runs_dir")
        profiles[route] = candidate
    return profiles


def _native(config):
    active = _json(config["learning_config"], 65536)
    if (type(active) is not dict or active.get("schema") != "lodestone-learning-active/v1"
            or active.get("required") is not True):
        raise ValueError("Required native learning configuration is unavailable")
    workspace = active.get("memory_workspace")
    if type(workspace) is not str or not Path(workspace).is_absolute():
        raise ValueError("Native learning workspace must be an absolute path")
    return UnifiedLoom(workspace, active.get("seed_handles"))


def _retain_local_failure(learning_config, *, operation, source_event, observation):
    """Append advisory evidence only; never invent a prepared/model outcome."""
    from lodestone_nurarihyon import learning
    try:
        memory = _native({"learning_config": str(learning_config)}).memory
        with learning.locked(memory):
            return memory.retain_preparation_failure(operation, source_event, observation)
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        # The original local failure remains durable even when native admission
        # is busy or unavailable. This is no execution retry or claimed lesson.
        return {"status": "NOT_RETAINED", "error_type": type(exc).__name__,
                "outcome_created": False, "provider_called": False, "authority": "NONE"}


def _execution_binding(config, identity, *, encounter, purpose, context):
    """Bind the effective recipe; transient run metadata cannot bypass a stop."""
    root = Path(__file__).resolve().parent
    names = ("loom_runtime.py", "loom_runtime_worker.py", "loom_adapter.py",
             "loom_adapter_local.py", "loom_adapter_torch.py", "loom_inference_memory.py",
             "learning_context.py", "learning_reader.py")
    sources = {name: hashlib.sha256(read_bytes(root / name, _MAX_FILE)).hexdigest()
               for name in names}
    reader = {key: value for key, value in identity.items()
              if key not in ("runtime_config_sha256", "verification")}
    source = {"sources": sources, "reader": reader}
    inputs = {"encounter": encounter, "purpose": purpose, "context": context,
              "generation": config["generation"], "max_total_tokens": config["max_total_tokens"]}
    return {"schema": "loom-execution-binding/v1", "operation": "loom.local-inference",
            "source_sha256": hashlib.sha256(canonical(source)).hexdigest(),
            "input_sha256": hashlib.sha256(canonical(inputs)).hexdigest(),
            "limits": {key: config[key] for key in ("wall_seconds", "max_rss_bytes")}}


def _resource_failure(returned, owned_result, config):
    """Classify this supervisor's measured stop, never reader-supplied prose."""
    if returned is not owned_result or type(returned) is not dict or returned.get("status") != "INTERRUPTED":
        return None
    observation = returned.get("observation")
    if (type(observation) is not dict or type(observation.get("stop")) is not dict
            or observation["stop"].get("confirmed_stopped") is not True):
        return None
    reason = observation.get("reason")
    if reason == "rss_limit":
        peak, samples = observation.get("observed_group_peak_rss_bytes"), observation.get("rss_valid_sample_count")
        if (observation.get("rss_accounting") == "sampled_entire_owned_process_group"
                and type(peak) is int and type(samples) is int and samples > 0
                and peak > config["max_rss_bytes"]):
            return {"resource": "max_rss_bytes", "observed": peak, "limit": config["max_rss_bytes"]}
    elif reason == "wall_limit":
        elapsed = observation.get("elapsed_seconds")
        if type(elapsed) in (int, float) and math.isfinite(elapsed) and elapsed >= config["wall_seconds"]:
            return {"resource": "wall_seconds", "observed": elapsed, "limit": config["wall_seconds"]}
    return None


def _environment(backend):
    # Do not forward provider credentials or external provider routing variables.
    env = {key: os.environ[key] for key in ("HOME", "TMPDIR", "LANG", "LC_ALL")
           if key in os.environ}
    env.update({"PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
                "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
                "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
                "HF_HUB_DISABLE_TELEMETRY": "1", "DO_NOT_TRACK": "1",
                "USE_TORCH": "0" if backend == "ollama_qwen3_base" else "1",
                "USE_TF": "0", "TOKENIZERS_PARALLELISM": "false",
                "OMP_NUM_THREADS": "2", "MKL_NUM_THREADS": "2",
                "OPENBLAS_NUM_THREADS": "2", "NUMEXPR_NUM_THREADS": "2",
                "VECLIB_MAXIMUM_THREADS": "2"})
    return env


def _group_snapshot(pgid):
    """Bounded read-only process-group sample, including worker descendants."""
    sample = subprocess.run(["/bin/ps", "-axo", "pid=,pgid=,rss=,stat="],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=1, check=True)
    if len(sample.stdout) > _MAX_FILE:
        raise ValueError("Process enumeration exceeded its byte bound")
    members = []
    for line in sample.stdout.splitlines():
        fields = line.split()
        if len(fields) != 4:
            raise ValueError("Malformed process-group RSS sample")
        pid, group, rss = map(int, fields[:3])
        if group == pgid:
            members.append({"pid": pid, "rss_bytes": rss * 1024,
                            "zombie": fields[3].startswith(b"Z")})
    return members


def _signal_group(pgid, signum):
    try:
        os.killpg(pgid, signum)
    except ProcessLookupError:
        pass


def _stop_group(child):
    """Terminate once, escalate once and verify there are no live group members."""
    pgid = child.pid  # start_new_session=True; child and all its children share it.
    _signal_group(pgid, signal.SIGTERM)
    escalated = False
    deadline = time.monotonic() + 3
    members = []
    sample_error = None
    while True:
        child.poll()  # Reap the directly owned process before checking the group.
        try:
            members = _group_snapshot(pgid)
            sample_error = None
            if not any(not item["zombie"] for item in members):
                child.wait(timeout=0.1)
                return {"confirmed_stopped": True, "kill_escalated": escalated,
                        "zombie_pids": [item["pid"] for item in members]}
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            sample_error = type(exc).__name__
            # ESRCH independently proves that the entire group has disappeared.
            try:
                os.killpg(pgid, 0)
            except ProcessLookupError:
                try:
                    child.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    pass
                else:
                    return {"confirmed_stopped": True, "kill_escalated": escalated,
                            "zombie_pids": []}
        if time.monotonic() >= deadline:
            if escalated:
                return {"confirmed_stopped": False, "kill_escalated": True,
                        "member_pids": [item["pid"] for item in members],
                        "sample_error": sample_error}
            _signal_group(pgid, signal.SIGKILL)
            escalated = True
            deadline = time.monotonic() + 3
        time.sleep(0.05)


def _partial(directory):
    path = directory / "partial.json"
    if not path.exists():
        return None, {}
    try:
        saved = _json(path)
        if type(saved) is not dict or type(saved.get("response")) is not str:
            raise ValueError("Invalid partial response")
        if len(saved["response"].encode("utf-8")) > 65536:
            raise ValueError("Partial response exceeds native response bound")
        counts = {key: saved[key] for key in ("prompt_tokens", "generated_tokens")
                  if type(saved.get(key)) is int and saved[key] >= 0}
        if "observation" in saved:
            observation = saved["observation"]
            if type(observation) is not dict or len(canonical(observation)) > 16384:
                raise ValueError("Partial observation exceeds native retention bound")
            counts["partial_observation"] = observation
        return saved["response"], counts
    except (OSError, ValueError, TypeError) as exc:
        return None, {"partial_error": type(exc).__name__}


class LoomBridge:
    """Thread-safe facade; one native broker serializes actual practice dispatch."""

    def __init__(self, config_path):
        self.config_path = Path(config_path).expanduser().resolve()
        self._config = _config(self.config_path)
        self._lock = threading.Lock()
        self._active = set()
        self._unsafe_groups = set()

    def models(self):
        result = []
        for route, config in _profiles(self.config_path).items():
            if config["backend"] == "ollama_qwen3_base":
                name = "Loom · Qwen3 quantized base"
            elif config.get("adapter_state"):
                name = "Loom · Qwen3 float32 · adapter " + config["adapter_state_sha256"][:12]
            else:
                name = "Loom · Qwen3 float32 base · adapter disabled"
            result.append({"id": route, "name": name,
                           "context_window": config["max_total_tokens"], "max_output_tokens": 512,
                           "max_temperature": 0, "default_temperature": 0})
        return result

    def reader_identities(self):
        return {route: runtime_identity(config) for route, config in _profiles(self.config_path).items()}

    def cancel_all(self):
        """Request cancellation; each run returns only after its stop is checked."""
        with self._lock:
            for event in self._active:
                event.set()

    def tool_catalogue(self):
        from .loom_operations import catalogue
        return catalogue(Path(__file__).resolve().parents[2])

    def _tool_bridge(self):
        from .loom_operations import OperationBridge
        config = _config(self.config_path)
        return OperationBridge(Path(__file__).resolve().parents[2],
                               Path(config["runs_dir"]) / "tool-operations",
                               learning_config=config["learning_config"])

    def prepare_tool(self, operation, inputs, workspace):
        return self._tool_bridge().prepare(operation, inputs, workspace)

    def run_tool(self, request, *, approved_digest, cancel_event=None):
        event = cancel_event if cancel_event is not None else threading.Event()
        with self._lock:
            self._active.add(event)
        try:
            return self._tool_bridge().run(request, approved_digest=approved_digest,
                                           cancel_event=event)
        finally:
            with self._lock:
                self._active.discard(event)

    def correct(self, outcome_handle, *, correction, author, observation="",
                event_id=None, observed_at=None):
        return _native(_config(self.config_path)).correct(
            outcome_handle, correction=correction, author=author, observation=observation,
            event_id=event_id, observed_at=observed_at)

    def review_decision(self, outcome_handle, *, contract, author):
        """Retain a separate source-bound check; never rewrite the reader's answer."""
        return _native(_config(self.config_path)).review_decision(
            outcome_handle, contract=contract, author=author)

    def preview_origami(self, outcome_handle, *, root, paths):
        config = _config(self.config_path)
        return _native(config).preview_origami(outcome_handle,
            workspace=Path(config["runs_dir"]) / "origami-handoffs", root=root, paths=paths)

    def complete_origami(self, handoff_id, curation):
        config = _config(self.config_path)
        return _native(config).complete_origami(handoff_id, curation,
            workspace=Path(config["runs_dir"]) / "origami-handoffs")

    def run(self, *, encounter, purpose, context="", route=None, max_tokens=None,
            temperature=0, top_p=1, cancel_event=None):
        # Keep a failed reread of configuration inside the durable local error
        # boundary. The constructor's validated config supplies only the ledger.
        profile_error = None
        try:
            profiles = _profiles(self.config_path)
            route = next(iter(profiles)) if route is None else route
            config = profiles.get(route, next(iter(profiles.values())))
        except (OSError, ValueError, TypeError, RuntimeError, KeyError) as exc:
            profile_error, profiles, config = exc, {}, self._config
        runs = Path(config["runs_dir"])
        runs.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory = runs / uuid.uuid4().hex
        directory.mkdir(mode=0o700)
        own_cancel = threading.Event()
        native_result = None
        provider_called = False
        owned_result = None
        stage = "CONFIGURATION"
        with self._lock:
            self._active.add(own_cancel)
        def cancelled():
            return own_cancel.is_set() or (cancel_event is not None and cancel_event.is_set())
        try:
            if profile_error is not None:
                raise profile_error
            if route not in profiles:
                raise ValueError("Only an explicitly configured Loom local route is available")
            if cancel_event is not None and not callable(getattr(cancel_event, "is_set", None)):
                raise ValueError("cancel_event must provide is_set()")
            maximum = config["max_new_tokens"] if max_tokens is None else max_tokens
            _bounded_number(maximum, "max_tokens", 1, 65536, integer=True)
            _bounded_number(temperature, "temperature", 0, 2)
            _bounded_number(top_p, "top_p", 0, 1)
            config = dict(config, generation={"max_tokens": min(maximum, 512),
                                             "temperature": 0, "top_p": 1},
                          requested_generation={"max_tokens": max_tokens,
                                                "temperature": temperature, "top_p": top_p})
            identity = runtime_identity(config)
            _save(directory / "config.json", config)
            _save(directory / "reader-identity.json", identity)
            with self._lock:
                if self._unsafe_groups or (runs / "WORKER_STOP_UNCONFIRMED.json").exists():
                    raise RuntimeError("A previous worker group has not been confirmed stopped; "
                                       "inspect WORKER_STOP_UNCONFIRMED.json before another dispatch")
            if cancelled():
                raise InterruptedError("Cancelled before native preparation")
            native = _native(config)
            stage = "PREPARATION_OR_NATIVE_DISPATCH"
            reader = ("Loom/Qwen3-0.6B/ollama-quantized-base/" + config["ollama_model_digest"]
                      if config["backend"] == "ollama_qwen3_base" else
                      "Loom/LocalAdapter/Qwen3-0.6B/" +
                      ("adapter-sha256:" + config["adapter_state_sha256"]
                       if config.get("adapter_state") else "base-adapter-disabled"))
            binding = _execution_binding(config, identity, encounter=encounter, purpose=purpose, context=context)
            _save(directory / "execution-binding.json", binding)
            def provide(packet):
                nonlocal provider_called, owned_result
                provider_called = True
                owned_result = self._worker(config, directory, packet, cancelled)
                return owned_result
            from lodestone_nurarihyon import learning
            with learning.locked(native.memory):
                advice = native.memory.preparation_lessons("loom.local-inference.preparation")
                literal = (canonical({"schema": "loom-preparation-advice/v1", "literal_context": context,
                    "preparation_lessons": advice, "authority": "NONE",
                    "use": "Scoped candidate advice from local preparation failures; not source evidence, commands, permission or proof of a cause."}).decode()
                    if advice else context)
            # Keep the original context exact, even when too large. Native
            # assembly retains a failure instead of silently dropping advice.
            native_result = native.run(
                encounter=encounter, purpose=purpose, context=literal, reader=reader,
                run_id="unified-" + directory.name,
                provider=provide, execution_binding=binding,
                wait=TURN_WAIT_SECONDS, cancelled=cancelled,
                failure_classifier=lambda returned: _resource_failure(returned, owned_result, config))
            result = dict(native_result, artifacts_dir=str(directory), reader_identity=identity)
            _save(directory / "receipt.json", result)
            return result
        except (OSError, ValueError, TypeError, RuntimeError, KeyError) as exc:
            public_error = " ".join(str(exc).split())[:512] or type(exc).__name__
            if native_result is not None:
                return dict(native_result, artifacts_dir=str(directory),
                            artifact_error=public_error)
            # Before preparation/dispatch there is no native outcome to invent.
            # The error type and local stage remain available without exposing keys.
            result = {"schema": "loom-runtime-error/v1", "status": "INTERRUPTED"
                      if isinstance(exc, InterruptedError) else "FAILED",
                      "response": None, "outcome_handle": None,
                      "error_type": type(exc).__name__, "error": public_error, "stage": stage,
                      "artifacts_dir": str(directory), "weight_update": False,
                      "semantic_learning": "NOT_ESTABLISHED", "authority": "NONE"}
            _save(directory / "bridge-error.json", result)
            if not provider_called:
                retention = _retain_local_failure(config["learning_config"],
                    operation="loom.local-inference.preparation", source_event="unified-" + directory.name,
                    observation={"status": result["status"], "stage": stage,
                                 "error_type": type(exc).__name__, "provider_called": False,
                                 "local_receipt_sha256": hashlib.sha256(canonical(result)).hexdigest(),
                                 "validated_runtime_config_sha256": hashlib.sha256(canonical(config)).hexdigest(),
                                 "reason": "Local preparation failed before the provider callback; inspect the retained local receipt. Cause is not inferred."})
                _save(directory / "failure-learning.json", retention)
                result = dict(result, failure_learning=retention)
            _save(directory / "receipt.json", result)
            return result
        finally:
            with self._lock:
                self._active.discard(own_cancel)

    def _worker(self, config, directory, packet, cancelled):
        _save(directory / "request.json", packet)
        if cancelled():
            return {"status": "INTERRUPTED", "response": None,
                    "observation": {"reason": "cancelled_before_worker_start"}}
        command = [config["python"], "-B", "-m", "lodestone_machinery.loom_runtime_worker",
                   "--config", str(directory / "config.json"),
                   "--request", str(directory / "request.json"),
                   "--result", str(directory / "result.json"),
                   "--partial", str(directory / "partial.json")]
        child = None
        started = time.monotonic()
        next_sample = started
        peak = 0
        sample_errors = []
        consecutive_sample_errors = 0
        maximum_consecutive_sample_errors = 0
        valid_sample_count = 0
        last_valid_sample_seconds = None
        reason = None
        cleanup = {"confirmed_stopped": True, "kill_escalated": False, "zombie_pids": []}
        try:
            with (directory / "stdout.log").open("xb") as stdout, (directory / "stderr.log").open("xb") as stderr:
                os.chmod(stdout.name, 0o600)
                os.chmod(stderr.name, 0o600)
                child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=stdout,
                                         stderr=stderr, cwd=str(directory),
                                         env=_environment(config["backend"]), start_new_session=True)
                _save(directory / "worker.json", {"pid": child.pid, "pgid": child.pid,
                                                  "backend": config["backend"]})
                while child.poll() is None:
                    if cancelled():
                        reason = "cancelled"
                        break
                    if time.monotonic() - started >= config["wall_seconds"]:
                        reason = "wall_limit"
                        break
                    if time.monotonic() >= next_sample:
                        try:
                            sample = _group_snapshot(child.pid)
                        except subprocess.TimeoutExpired:
                            consecutive_sample_errors += 1
                            maximum_consecutive_sample_errors = max(
                                maximum_consecutive_sample_errors, consecutive_sample_errors)
                            sample_errors.append({"error": "TimeoutExpired",
                                "elapsed_seconds": round(time.monotonic() - started, 3),
                                "consecutive": consecutive_sample_errors})
                            if consecutive_sample_errors >= 3:
                                reason = "rss_sampling_unavailable"
                                break
                        else:
                            consecutive_sample_errors = 0
                            valid_sample_count += 1
                            last_valid_sample_seconds = round(time.monotonic() - started, 3)
                            rss = sum(item["rss_bytes"] for item in sample if not item["zombie"])
                            peak = max(peak, rss)
                            if rss > config["max_rss_bytes"]:
                                reason = "rss_limit"
                                break
                        next_sample = time.monotonic() + 1
                        if any((directory / name).exists() and (directory / name).stat().st_size > _MAX_FILE
                               for name in ("stdout.log", "stderr.log", "partial.server.log")):
                            reason = "log_byte_limit"
                            break
                    time.sleep(0.05)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            reason = "supervisor_error:" + type(exc).__name__
        finally:
            if child is not None:
                try:
                    cleanup = _stop_group(child)
                except (OSError, ValueError, subprocess.SubprocessError) as exc:
                    cleanup = {"confirmed_stopped": False, "stop_error": type(exc).__name__}
                if not cleanup["confirmed_stopped"]:
                    with self._lock:
                        self._unsafe_groups.add(child.pid)
                    blocked = Path(config["runs_dir"]) / "WORKER_STOP_UNCONFIRMED.json"
                    if not blocked.exists():
                        _save(blocked, {"pgid": child.pid, "artifacts_dir": str(directory),
                                        "stop": cleanup, "automatic_retry": False})
                    reason = "worker_group_stop_unconfirmed"
        observation = {"backend": config["backend"], "worker_exit_code": child.returncode if child else None,
                       "elapsed_seconds": round(time.monotonic() - started, 3),
                       "observed_group_peak_rss_bytes": peak,
                       "rss_accounting": "sampled_entire_owned_process_group",
                       "rss_sample_errors": sample_errors,
                       "rss_valid_sample_count": valid_sample_count,
                       "rss_last_valid_sample_seconds": last_valid_sample_seconds,
                       "rss_consecutive_errors_at_exit": consecutive_sample_errors,
                       "rss_max_consecutive_errors": maximum_consecutive_sample_errors,
                       "stop": cleanup, "artifacts_dir": str(directory)}
        if reason is None and cancelled():
            reason = "cancelled"
        if reason is None and child.returncode != 0:
            reason = "worker_nonzero_exit"
        result = None
        if reason is None:
            try:
                result = _json(directory / "result.json")
                if (type(result) is not dict or set(result) != {"status", "response", "observation"}
                        or result["status"] not in STATUSES
                        or (result["response"] is not None and type(result["response"]) is not str)
                        or type(result["observation"]) is not dict):
                    raise ValueError("Invalid local worker result")
                if result["response"] is not None and len(result["response"].encode("utf-8")) > 65536:
                    raise ValueError("Worker response exceeds native bound")
                result = dict(result, observation={"worker": result["observation"],
                                                   "supervisor": observation})
                if len(canonical(result["observation"])) > 32768:
                    raise ValueError("Worker observation exceeds native bound")
            except (OSError, ValueError, TypeError, KeyError) as exc:
                reason = "invalid_worker_result:" + type(exc).__name__
        if reason is not None:
            response, counts = _partial(directory)
            result = {"status": "INTERRUPTED" if reason in ("cancelled", "wall_limit", "rss_limit")
                      else "FAILED", "response": response,
                      "observation": dict(observation, reason=reason, **counts)}
        _save(directory / "supervisor.json", result)
        return result
