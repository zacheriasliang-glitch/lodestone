"""Explicit local toolkit operations shared by the CLI and Loom desktop.

This is an invocation contract, not a model tool executor or permission oracle.
No model/provider, transport send, daemon start, study or training commands exist
in the catalogue. A caller presents the exact request before submitting its digest.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import uuid

MAX_BYTES = 2 * 1024 * 1024
MAX_SECONDS = 120
MAX_JOB_MEMORY = 4 * 1024 * 1024 * 1024

from .loom_optional_operations import EXTRA_SPECS, availability as optional_availability
from .portable_process import WindowsJobProcess, launch_worker
from lodestone_nurarihyon.portable_files import (read_bytes, durable_write,
                                                ensure_directory, guarded_directory, absolute_path)


class _OutputCapture:
    """Read a native anonymous pipe with bounded retained bytes on every OS."""
    def __init__(self, descriptor, maximum):
        self.descriptor, self.maximum = descriptor, maximum
        self.output = bytearray()
        self.done = threading.Event()
        self.overflow = threading.Event()
        self.error = None
        self.thread = threading.Thread(target=self._read, daemon=True)

    def _read(self):
        try:
            with os.fdopen(self.descriptor, "rb", buffering=0) as stream:
                while True:
                    chunk = stream.read(4096)
                    if not chunk:
                        break
                    remaining = self.maximum - len(self.output)
                    self.output.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        self.overflow.set()
                        break
        except OSError as exc:
            self.error = type(exc).__name__
        finally:
            self.done.set()


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False,
                      separators=(",", ":")).encode()


def sha(value):
    return hashlib.sha256(value).hexdigest()


def spec(family, description, command, inputs=None, effects="Read selected local inputs; write invocation receipt only"):
    return dict(family=family, description=description, command=command,
                inputs=inputs or {}, effects=effects)


# Each input is (kind, CLI flag); None means a positional value. No free argv.
SPECS = {
    "ecosystem.doctor": spec("ecosystem", "Inspect local package and selected workspace readiness", ["doctor"]),
    "ecosystem.context": spec("ecosystem", "Read bounded package documentation", ["context"]),
    "nurarihyon.registry": spec("nurarihyon", "Read native admitted task contracts", ["nurarihyon", "registry"]),
    "nurarihyon.capture-document": spec("nurarihyon", "Capture a selected document as inert source evidence", ["nurarihyon", "capture-document"],
        {"document": ("file", "--document"), "title": ("text", "--title"), "output": ("new-file", "--output")}, "Create selected source capture; no task, approval or service change"),
    "bezoar.ingest": spec("bezoar-reconciliation", "Capture explicit files for Bezoar inspection", ["bezoar", "ingest"],
        {"root": ("directory", "--root"), "sources": ("sources", "--source")}, "Append source records to selected workspace"),
    "bezoar.show": spec("bezoar-reconciliation", "Read one native Bezoar record", ["bezoar", "show"],
        {"handle": ("text", "--handle")}, "Read selected record; native store may initialize workspace metadata"),
    "machinery.submit": spec("machinery", "Prepare a source-bound local machinery job", ["machinery", "submit"],
        {"root": ("directory", "--root"), "sources": ("sources", "--source"), "query": ("text", "--query")}, "Append prepared job and source records to selected workspace; no model call"),
    "machinery.run": spec("machinery", "Run a selected prepared deterministic source job", ["machinery", "run"],
        {"job_id": ("text", None)}, "Advance selected job and append measured source artifacts; no model call"),
    "machinery.inspect": spec("machinery", "Inspect selected job and evidence", ["machinery", "inspect"],
        {"job_id": ("text", None)}, "Read selected job; native store may initialize workspace metadata"),
    "pando.init": spec("pando", "Initialize a separate user-selected local board", ["pando", "--home", "{workspace}", "init"],
        effects="Create local board database and default participant records; no messages or synchronization"),
    "pando.verify": spec("pando", "Verify selected board records", ["pando", "--home", "{workspace}", "verify"],
        effects="Verify board; native Board constructor may initialize missing database or metadata"),
    "memory.add": spec("local-memory", "Retain an attributed local note", ["memory", "add", "{workspace}"],
        {"title": ("text", "--title"), "body": ("text", "--body"), "source": ("text", "--source")}, "Append local note; source attribution is not factual endorsement"),
    "memory.search": spec("local-memory", "Search selected local notes", ["memory", "search", "{workspace}"], {"query": ("text", None)}),
    "memory.verify": spec("local-memory", "Verify selected local memory", ["memory", "verify", "{workspace}"]),
    "corpus-fold.verify": spec("corpus-fold", "Verify captured corpus bindings and drift", ["yggdrasil", "corpus-fold"],
        {"root": ("directory", "--root"), "fold": ("file", "--fold")}),
    "origami-fold.verify": spec("origami-fold", "Verify selected fold and source bindings", ["origami-fold"],
        {"root": ("directory", "--root"), "fold": ("file", "--fold")}),
    "continuity.create": spec("continuity", "Create a source-pinned continuity checkpoint", ["continuity", "create"],
        {"root": ("directory", "--root"), "output": ("new-file", "--output"), "task": ("text", "--task"), "status": ("text", "--status"), "next_action": ("text", "--next-action"), "sources": ("sources", "--source")}, "Create new selected checkpoint without replacing existing files"),
    "continuity.verify": spec("continuity", "Verify a checkpoint against current files", ["continuity", "verify"],
        {"checkpoint": ("file", None), "root": ("directory", "--root")}),
    "repository.manifest": spec("repository", "Prepare a selected committed-file export manifest", ["repository", "manifest"],
        {"repository": ("directory", "--repository"), "commit": ("text", "--commit"), "files": ("strings", "--file"), "output": ("new-file", "--output")}, "Create manifest for explicit Git files; no export, push or publication"),
    "repository.check": spec("repository", "Check selected repository Markdown navigation", ["check"], {"root": ("directory", "--root")}),
}

SPECS.update(EXTRA_SPECS)
SPECS.update({
    "loom.inference-memory-assess": spec("origami-fold", "Estimate local inference memory from explicit planning geometry without loading a model", ["loom-tools", "inference-memory", "assess"],
        {"specification": ("file", "--file")}, "Read supplied model geometry and token bounds; retain an estimate and invocation receipt, not verified model identity or an RSS fit guarantee"),
    "loom.inference-memory-skill": spec("origami-fold", "Export the reusable inference memory skill and exact helper as a native candidate manifest", ["loom-tools", "inference-memory", "candidate"],
        {"output": ("new-file", "--output")}, "Create a new bounded candidate manifest for the existing improvement lifecycle; no proposal, code execution, model call or installation"),
    "learning.improvement-propose": spec("bezoar-reconciliation", "Retain a versioned candidate skill/function with conditions and source", ["loom-practice", "improve", "propose"],
        {"config": ("file", "--config"), "manifest": ("file", "--file")}, "Append a candidate record in the explicitly configured learning memory; no code execution, training or installation"),
    "learning.improvement-show": spec("bezoar-reconciliation", "Inspect candidate revisions, actual trials and corrections", ["loom-practice", "improve", "show"],
        {"candidate": ("text", None), "config": ("file", "--config")}, "Read complete bounded candidate history in the selected learning memory; no trial or model call"),
    "learning.improvement-prepare": spec("bezoar-reconciliation", "Prepare one candidate trial with retained conditions and corrections", ["loom-practice", "improve", "prepare"],
        {"candidate": ("text", None), "config": ("file", "--config"), "encounter": ("text", "--encounter"), "purpose": ("text", "--purpose"), "reader": ("text", "--reader")}, "Append a native preparation and linked candidate attempt using configured SEEDs; no trial command or model call"),
    "learning.improvement-review": spec("bezoar-reconciliation", "Return an attributed correction from a candidate trial to Bezoar", ["loom-practice", "improve", "review"],
        {"attempt": ("text", None), "config": ("file", "--config"), "correction": ("text", "--correction"), "author": ("text", "--author"), "observation": ("text", "--observation")}, "Append native feedback evidence and an unresolved review; no demonstrated-learning promotion"),
    "learning.improvement-materialize": spec("bezoar-reconciliation", "Export exact retained candidate source into a new directory", ["loom-practice", "improve", "materialize"],
        {"candidate": ("text", None), "config": ("file", "--config"), "destination": ("new-file", "--destination")}, "Create candidate files in a new directory; no overwrite, import, execution or runtime installation"),
})

# Each family retains restrictions beyond its explicitly listed operations.
UNAVAILABLE = {
    "mailbox": "No network send/poll in Loom. Existing authenticated transport and consumed-job state remain separate.",
    "orchestrator": "Host-approved decision/envelope protocol required; no generic model-to-command route.",
    "porcelain": "Optional numerical environment and explicit experiment inputs required; no simulation launched by this catalogue.",
    "release": "Release packaging is a separate reviewed deployment operation, not an inference tool.",
    "supplied-candidates": "Experimental pinned-candidate store requires a separately prepared model/table/candidate contract; no automatic candidate generation.",
}


# Reviewed Windows CLI closures. Expand only alongside the operation's actual
# dependency/custody port and native fixtures, not merely a successful import.
# The catalogue runs trusted explicit commands; Job ownership is not a sandbox.
WINDOWS_OPERATIONS = frozenset({"ecosystem.doctor", "ecosystem.context",
    "mailbox.validate-envelope", "release.verify-download", "loom.inference-memory-assess",
    "loom.inference-memory-skill", "bezoar.ingest", "bezoar.show"})


def _availability(root):
    available = optional_availability(root)
    if sys.platform == "win32":
        for key in SPECS:
            if key not in WINDOWS_OPERATIONS:
                available[key] = {"status": "UNAVAILABLE", "reason":
                    "Operation unavailable on Windows: its dependency/custody port is not verified."}
    return available


def _selected_path(value, *, directory=False):
    path = Path(value).expanduser()
    if os.name != "nt":
        return path.resolve()
    # resolve() would erase a junction before the native custody check sees it.
    path = absolute_path(path)
    ancestor = path if directory else path.parent
    while True:
        try:
            ancestor.lstat()
            break
        except FileNotFoundError:
            if ancestor == ancestor.parent:
                raise
            ancestor = ancestor.parent
    with guarded_directory(ancestor):
        pass
    return path


def catalogue(package_root):
    root = _selected_path(package_root, directory=True)
    inventory = json.loads(_read(root / "docs/loom-integration-map.json"))
    available = _availability(root)
    operations = [{"id": key, "family": value["family"], "description": value["description"],
                   "inputs": {name: {"type": item[0], "required": True} for name, item in value["inputs"].items()},
                   "effects": value["effects"], **available.get(key, {"status": "AVAILABLE"})}
                  for key, value in SPECS.items()]
    families = []
    for source in inventory["toolkit_families"]:
        family = dict(source)
        family["operations"] = [row["id"] for row in operations if row["family"] == family["id"]]
        family["status"] = ("EXPLICIT_LOCAL_OPERATIONS" if any(row["status"] == "AVAILABLE" for row in operations if row["family"] == family["id"])
                            else "UNAVAILABLE" if family["operations"] else "NOT_EXPOSED")
        family["limit"] = UNAVAILABLE.get(family["id"], "Only listed operations are exposed; availability is not semantic evidence")
        families.append(family)
    return {"schema": "loom-tool-catalogue/v1", "operations": operations,
            "toolkit_families": families, "programme_rows": inventory["programme_rows"],
            "authority": "User-selected exact request only; generated text remains a proposal",
            "semantic_learning_established": False}


def _read(path):
    return read_bytes(path, 32 * 1024 * 1024)


def _identity(root):
    # Include callable code and policy, not private state or generated bytecode.
    def source_files(directory):
        # Do not silently omit a linked source directory during glob traversal.
        # Native directory leases reject both symbolic links and junctions.
        with guarded_directory(directory):
            for path in sorted(directory.iterdir()):
                if path.name == "__pycache__":
                    continue
                if path.is_dir():
                    yield from source_files(path)
                elif path.suffix in (".py", ".json") or path.name == "run":
                    yield path

    paths = [root / "lodestone.py"]
    for name in ("skills/lodestone-inference-memory/SKILL.md", "README.md", "docs/conscience.md"):
        if (root / name).is_file():
            paths.append(root / name)
    paths.extend(root / name for name in ("verify_download.py", "experiments/porcelain-simulator/porcelain/report.py",
                                         "experiments/porcelain-simulator/porcelain/__init__.py") if (root / name).is_file())
    for directory in ("src", "tools", "orchestrator"):
        if (root / directory).exists():
            paths.extend(source_files(root / directory))
    with guarded_directory(root):
        return sha(encoded({p.relative_to(root).as_posix(): sha(_read(p)) for p in sorted(set(paths))}))


def _atomic(path, value):
    durable_write(path, encoded(value) + b"\n", replace=True)


class OperationBridge:
    def __init__(self, package_root, receipts_dir, *, max_seconds=MAX_SECONDS, max_bytes=MAX_BYTES,
                 learning_config=None):
        self.root = _selected_path(package_root, directory=True)
        self.receipts_dir = _selected_path(receipts_dir, directory=True)
        if self.receipts_dir == self.root or self.root in self.receipts_dir.parents:
            raise ValueError("Receipt directory must be outside the immutable package")
        if (type(max_seconds) not in (int, float) or type(max_bytes) is not int or
                not 0 < max_seconds <= MAX_SECONDS or not 0 < max_bytes <= MAX_BYTES):
            raise ValueError("Bounds cannot exceed catalogue limits")
        self.max_seconds, self.max_bytes = max_seconds, max_bytes
        self.learning_config = (_selected_path(learning_config)
                                if learning_config is not None else None)

    def _retain_failure(self, operation, event, observation):
        if self.learning_config is None:
            return None
        from .loom_runtime import _retain_local_failure
        return _retain_local_failure(self.learning_config, operation="loom.tool-operation",
            source_event=event, observation={"operation": operation if type(operation) is str
                and len(operation.encode()) <= 512 else "INVALID_OPERATION",
                "provider_called": False, **observation})

    def _preparation_failure(self, operation, stage, exc):
        """Keep rejected local tool requests without fabricating a tool/model run."""
        if self.learning_config is None:
            return
        directory = self.receipts_dir / "preparation-failures"
        ensure_directory(directory)
        event = "tool-preparation-" + uuid.uuid4().hex
        result = {"schema": "loom-tool-preparation-failure/v1", "status": "FAILED",
                  "stage": stage, "error_type": type(exc).__name__, "provider_called": False,
                  "subprocess_started": False, "outcome_handle": None, "authority": "NONE"}
        _atomic(directory / (event + ".json"), result)
        result["failure_learning"] = self._retain_failure(operation, event, result)
        _atomic(directory / (event + ".json"), result)

    def prepare(self, operation, inputs, workspace, *, request_id=None):
        try:
            return self._prepare(operation, inputs, workspace, request_id=request_id)
        except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as exc:
            self._preparation_failure(operation, "PREPARE", exc)
            raise

    def _prepare(self, operation, inputs, workspace, *, request_id=None):
        if operation not in SPECS or not isinstance(inputs, dict):
            raise ValueError("Unknown local operation or invalid named inputs")
        descriptor = SPECS[operation]
        prerequisite = _availability(self.root).get(operation, {"status": "AVAILABLE"})
        if prerequisite["status"] != "AVAILABLE":
            error = OSError if sys.platform == "win32" and operation not in WINDOWS_OPERATIONS else ValueError
            raise error("Operation unavailable: " + prerequisite.get("reason", operation))
        if set(inputs) != set(descriptor["inputs"]):
            raise ValueError("Provide exactly the operation's named inputs")
        workspace = str(_selected_path(workspace, directory=True))
        if workspace == str(self.root) or self.root in Path(workspace).parents:
            raise ValueError("Choose an output workspace outside the immutable package")
        argv = [sys.executable, "-B", str(self.root / "lodestone.py"), "--repo", str(self.root), "--workspace", workspace]
        argv += [workspace if part == "{workspace}" else part for part in descriptor["command"]]
        normalized, pins = {}, {}
        for name, (kind, flag) in descriptor["inputs"].items():
            value = inputs[name]
            if operation == "repository.manifest" and name == "commit":
                if not isinstance(value, str) or not value or value.startswith("-") or "\x00" in value:
                    raise ValueError("Invalid Git commit")
                value = subprocess.check_output(["git", "-C", inputs["repository"], "rev-parse", "--verify", value + "^{commit}"],
                                                stderr=subprocess.PIPE, timeout=5).decode().strip()
            values = value if kind in ("strings", "sources") else [value]
            if not isinstance(values, list) or not 1 <= len(values) <= 64:
                raise ValueError("Expected a nonempty bounded input list")
            checked = []
            for item in values:
                if not isinstance(item, str) or not item or len(item) > 32768 or "\x00" in item or item.startswith("-"):
                    raise ValueError("Inputs must be nonempty bounded strings, not CLI options")
                if kind in ("file", "directory", "new-file"):
                    path = _selected_path(item, directory=kind == "directory")
                    if kind == "file":
                        pins[str(path)] = sha(_read(path))
                    elif kind == "directory" and not path.is_dir():
                        raise ValueError("Selected input directory does not exist")
                    elif kind == "new-file":
                        if os.name == "nt":
                            try:
                                path.lstat()
                            except FileNotFoundError:
                                pass
                            else:
                                raise ValueError("Output must be a new file, not an existing path or reparse point")
                        if path.exists() or not path.parent.is_dir():
                            raise ValueError("Output must be a new file with an existing parent")
                        if path == self.root or self.root in path.parents:
                            raise ValueError("Output cannot modify the immutable package")
                    item = str(path)
                if kind == "sources":
                    if Path(item).is_absolute():
                        raise ValueError("Sources must use relative paths")
                    source_root = _selected_path(inputs["root"], directory=True)
                    source_path = _selected_path(source_root / item)
                    if source_root not in source_path.parents:
                        raise ValueError("Source must be a relative file inside the selected root")
                    pins[str(source_path)] = sha(_read(source_path))
                checked.append(item)
                if flag is not None:
                    argv.append(flag)
                argv.append(item)
            normalized[name] = checked if kind in ("strings", "sources") else checked[0]
        if operation in ("corpus-fold.verify", "origami-fold.verify"):
            argv.append("verify")
        request_id = request_id or uuid.uuid4().hex
        if len(request_id) != 32 or any(c not in "0123456789abcdef" for c in request_id):
            raise ValueError("Invalid request identifier")
        result = {"schema": "loom-tool-request/v1", "id": request_id, "operation": operation,
                  "inputs": normalized, "workspace": workspace, "package_root": str(self.root),
                  "package_sha256": _identity(self.root), "input_sha256": pins, "argv": argv,
                  "receipts_dir": str(self.receipts_dir), "prerequisite_state": prerequisite,
                  "effects": descriptor["effects"], "max_seconds": self.max_seconds,
                  "max_output_bytes": self.max_bytes, "authority": "explicit-user-request"}
        result["execution"] = {"ownership": "windows_job" if sys.platform == "win32" else "posix_process_group",
                               "filesystem_network_sandbox": False,
                               "job_commit_limit_bytes": MAX_JOB_MEMORY if sys.platform == "win32" else None}
        if self.learning_config is not None:
            result["failure_learning_config"] = {"path": str(self.learning_config),
                                                  "sha256": sha(_read(self.learning_config))}
        return {**result, "digest": sha(encoded(result))}

    def run(self, request, *, approved_digest, cancel_event=None):
        try:
            if not isinstance(request, dict) or approved_digest != request.get("digest"):
                raise ValueError("Exact displayed request digest is required")
            expected = self._prepare(request["operation"], request["inputs"], request["workspace"], request_id=request["id"])
            if expected != request:
                raise ValueError("Request, source bytes or package changed after preview; prepare again")
            if (self.receipts_dir / "STOP_UNCONFIRMED.json").exists():
                raise RuntimeError("Previous owned process-tree stop unconfirmed; inspect retained evidence")
            directory = self.receipts_dir / request["id"]
            ensure_directory(self.receipts_dir)
            with guarded_directory(self.receipts_dir):
                directory.mkdir(mode=0o700, exist_ok=False)  # Never repeat a possibly applied operation.
        except (OSError, ValueError, TypeError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
            self._preparation_failure(request.get("operation") if isinstance(request, dict) else None,
                                      "REQUEST_VALIDATION", exc)
            raise
        _atomic(directory / "request.json", request)
        result = {"schema": "loom-tool-result/v1", "request_digest": approved_digest,
                  "operation": request["operation"], "status": "RUNNING", "started_at": time.time(),
                  "receipt_directory": str(directory), "effects": request["effects"],
                  "semantic_learning_established": False, "returncode": None,
                  "execution": request["execution"]}
        _atomic(directory / "result.json", result)
        process = None
        capture = None
        output = b""
        try:
            if cancel_event is not None and cancel_event.is_set():
                result["status"] = "CANCELLED_BEFORE_START"
            else:
                env = {**os.environ, "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
                       "PYTHONPATH": str(self.root / "src"), "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1"}
                if sys.platform == "win32":
                    from .loom_runtime import _environment
                    env = {**_environment("ollama_qwen3_base"), "PYTHONPATH": str(self.root / "src"),
                           "PYTHONUTF8": "1", "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1"}
                read_fd, write_fd = os.pipe()
                try:
                    with os.fdopen(write_fd, "wb", buffering=0) as stream:
                        process = launch_worker(request["argv"], stdout=stream, stderr=stream,
                                                env=env, cwd=self.root, max_memory_bytes=MAX_JOB_MEMORY)
                    capture = _OutputCapture(read_fd, self.max_bytes)
                    capture.thread.start()
                except BaseException:
                    os.close(read_fd)
                    capture = None
                    raise
                result["pid"] = process.pid
                _atomic(directory / "result.json", result)
                start = time.monotonic()
                while True:
                    if capture.overflow.is_set():
                        result["status"] = "OUTPUT_LIMIT"; break
                    if capture.error is not None:
                        raise OSError("Operation output capture failed: " + capture.error)
                    if cancel_event is not None and cancel_event.is_set():
                        result["status"] = "INTERRUPTED"; break
                    if time.monotonic() - start >= self.max_seconds:
                        result["status"] = "TIMEOUT"; break
                    if isinstance(process, WindowsJobProcess):
                        if process.memory_observation()["windows_job_memory_limit_notified"]:
                            result["status"] = "MEMORY_LIMIT"; break
                    if process.poll() is not None and capture.done.is_set():
                        break
                    time.sleep(0.025)
                if result["status"] == "RUNNING":
                    result["status"] = "COMPLETED" if process.returncode == 0 else "FAILED"
        except BaseException as exc:
            result["status"] = "INTERRUPTED" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else "FAILED"
            result["error"] = f"{type(exc).__name__}: {str(exc)[:1000]}"
        finally:
            if process is not None:
                try:
                    cleanup = self._stop(process)
                    result["returncode"] = process.poll()
                    if isinstance(process, WindowsJobProcess):
                        try:
                            result["windows_job"] = process.memory_observation()
                            result["windows_job"]["member_pids_after_stop"] = process.pids()
                            if result["windows_job"]["windows_job_memory_limit_notified"]:
                                result["status"] = "MEMORY_LIMIT"
                        except OSError as exc:
                            result["ownership_observation_error"] = type(exc).__name__
                            result["status"] = "FAILED"
                except (OSError, ValueError, subprocess.SubprocessError) as exc:
                    cleanup = {"confirmed_stopped": False, "error": type(exc).__name__}
                finally:
                    if isinstance(process, WindowsJobProcess):
                        process.close()
                result["cleanup"] = cleanup
                if result["status"] == "COMPLETED" and cleanup.get("termination_required"):
                    result["status"] = "INTERRUPTED"
                if not cleanup["confirmed_stopped"]:
                    result["status"] = "STOP_UNCONFIRMED"
                    _atomic(self.receipts_dir / "STOP_UNCONFIRMED.json", {"pgid": process.pid, "request": request["id"], "cleanup": cleanup})
            if capture is not None:
                capture.thread.join(timeout=2)
                result["output_capture_stopped"] = not capture.thread.is_alive()
                if capture.thread.is_alive() and result["status"] != "STOP_UNCONFIRMED":
                    result["status"] = "CAPTURE_UNCONFIRMED"
                    _atomic(self.receipts_dir / "STOP_UNCONFIRMED.json", {
                        "request": request["id"], "reason": "Output pipe did not close after owned-tree stop"})
                output = bytes(capture.output)
            result.update(finished_at=time.time(), output=output.decode("utf-8", errors="replace"),
                          output_sha256=sha(bytes(output)), output_bytes=len(output))
            _atomic(directory / "result.json", result)
            if result["status"] != "COMPLETED" and self.learning_config is not None:
                # Complete output stays in the original local receipt. Native
                # evidence carries bounded measured facts and its exact digest.
                observation = {key: result[key] for key in ("status", "returncode", "output_sha256", "output_bytes")}
                observation.update(request_digest=request["digest"], package_sha256=request["package_sha256"],
                    input_sha256=sha(encoded(request["input_sha256"])), subprocess_started=process is not None,
                    local_receipt_sha256=sha(encoded(result)),
                    cleanup_confirmed=result.get("cleanup", {}).get("confirmed_stopped"),
                    reason="Local operation did not complete; source output is retained in the local receipt. No cause is inferred.")
                result["failure_learning"] = self._retain_failure(request["operation"],
                    "tool-operation-" + request["id"], observation)
                _atomic(directory / "result.json", result)
        return result

    @staticmethod
    def _stop(process):
        if isinstance(process, WindowsJobProcess):
            try:
                members = process.pids()
                stopped = process.stop()
                return {**stopped, "termination_required": bool(members), "member_pids_before_stop": members}
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                return {"confirmed_stopped": False, "error": type(exc).__name__}
        from .loom_runtime import _group_snapshot
        # Inspect first: Darwin may report EPERM for an already empty group.
        deadline, signalled, escalated = time.monotonic() + 2, False, False
        try:
            while True:
                process.poll()
                members = _group_snapshot(process.pid)
                if not any(not item["zombie"] for item in members):
                    process.wait(timeout=0.1)
                    return {"confirmed_stopped": True, "kill_escalated": escalated, "termination_required": signalled}
                if not signalled or (time.monotonic() >= deadline and not escalated):
                    try:
                        os.killpg(process.pid, signal.SIGKILL if signalled else signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    escalated = signalled
                    signalled = True
                    deadline = time.monotonic() + (1 if escalated else 0.5)
                elif time.monotonic() >= deadline:
                    return {"confirmed_stopped": False, "member_pids": [item["pid"] for item in members]}
                time.sleep(0.025)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            return {"confirmed_stopped": False, "error": type(exc).__name__}

    def history(self):
        rows = []
        if self.receipts_dir.exists():
            for path in sorted(self.receipts_dir.glob("*/result.json")):
                row = json.loads(_read(path))
                if row.get("status") == "RUNNING":
                    row["recovery_note"] = "Unfinished invocation: inspect effects; never replay automatically"
                rows.append(row)
        return rows


def main(argv=None, *, root=None, workspace=None):
    supplied = list(sys.argv[1:] if argv is None else argv)
    if supplied and supplied[0] == "inference-memory":
        from .loom_memory_cli import main as memory_main
        return memory_main(supplied[1:], root=root)
    if supplied and supplied[0] == "native":
        from .loom_optional_operations import main as native_main
        return native_main(supplied[1:], root=root, workspace=workspace)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("catalogue", "prepare", "run", "history"))
    parser.add_argument("--receipts", required=True)
    parser.add_argument("--operation")
    parser.add_argument("--inputs", help="Exact named-input JSON file")
    parser.add_argument("--request", help="Previously prepared request JSON")
    parser.add_argument("--approved-digest", help="Digest of exact request selected by user")
    args = parser.parse_args(argv)
    root = root or Path(__file__).resolve().parents[2]
    bridge = OperationBridge(root, args.receipts)
    try:
        if args.action == "catalogue":
            value = catalogue(root)
        elif args.action == "prepare":
            value = bridge.prepare(args.operation, json.loads(_read(args.inputs)), workspace)
        elif args.action == "run":
            value = bridge.run(json.loads(_read(args.request)), approved_digest=args.approved_digest)
        else:
            value = bridge.history()
        print(json.dumps(value, indent=2))
        return 0 if not isinstance(value, dict) or value.get("status", "COMPLETED") == "COMPLETED" else 2
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr); return 2
