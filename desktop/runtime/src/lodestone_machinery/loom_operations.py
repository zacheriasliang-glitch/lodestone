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
import stat
import subprocess
import sys
import threading
import time
import uuid

MAX_BYTES = 2 * 1024 * 1024
MAX_SECONDS = 120

from .loom_optional_operations import EXTRA_SPECS, availability as optional_availability


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


def catalogue(package_root):
    root = Path(package_root).expanduser().resolve()
    inventory = json.loads((root / "docs/loom-integration-map.json").read_text())
    available = optional_availability(root)
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
    descriptor = os.open(str(path), os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("Selected input must be a regular file")
        raw = stream.read(32 * 1024 * 1024 + 1)
    if len(raw) > 32 * 1024 * 1024:
        raise ValueError("Selected file exceeds 32 MiB read bound")
    return raw


def _identity(root):
    # Include callable code and policy, not private state or generated bytecode.
    paths = [root / "lodestone.py"]
    memory_skill = root / "skills/lodestone-inference-memory/SKILL.md"
    if memory_skill.is_file():
        paths.append(memory_skill)
    paths.extend(root / name for name in ("verify_download.py", "experiments/porcelain-simulator/porcelain/report.py",
                                         "experiments/porcelain-simulator/porcelain/__init__.py") if (root / name).is_file())
    for directory in ("src", "tools", "orchestrator"):
        paths.extend(p for p in (root / directory).rglob("*") if p.is_file() and
                     "__pycache__" not in p.parts and (p.suffix in (".py", ".json") or p.name == "run"))
    return sha(encoded({str(p.relative_to(root)): sha(_read(p)) for p in sorted(set(paths))}))


def _atomic(path, value):
    temporary = path.with_suffix(".tmp")
    with temporary.open("xb") as stream:
        os.chmod(temporary, 0o600)
        stream.write(encoded(value) + b"\n"); stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, path)


class OperationBridge:
    def __init__(self, package_root, receipts_dir, *, max_seconds=MAX_SECONDS, max_bytes=MAX_BYTES,
                 learning_config=None):
        self.root = Path(package_root).expanduser().resolve()
        self.receipts_dir = Path(receipts_dir).expanduser().resolve()
        if self.receipts_dir == self.root or self.root in self.receipts_dir.parents:
            raise ValueError("Receipt directory must be outside the immutable package")
        if (type(max_seconds) not in (int, float) or type(max_bytes) is not int or
                not 0 < max_seconds <= MAX_SECONDS or not 0 < max_bytes <= MAX_BYTES):
            raise ValueError("Bounds cannot exceed catalogue limits")
        self.max_seconds, self.max_bytes = max_seconds, max_bytes
        self.learning_config = (Path(learning_config).expanduser().resolve()
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
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
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
        prerequisite = optional_availability(self.root).get(operation, {"status": "AVAILABLE"})
        if prerequisite["status"] != "AVAILABLE":
            raise ValueError("Operation unavailable: " + prerequisite.get("reason", operation))
        if set(inputs) != set(descriptor["inputs"]):
            raise ValueError("Provide exactly the operation's named inputs")
        workspace = str(Path(workspace).expanduser().resolve())
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
                    path = Path(item).expanduser().resolve()
                    if kind == "file":
                        pins[str(path)] = sha(_read(path))
                    elif kind == "directory" and not path.is_dir():
                        raise ValueError("Selected input directory does not exist")
                    elif kind == "new-file":
                        if path.exists() or not path.parent.is_dir():
                            raise ValueError("Output must be a new file with an existing parent")
                        if path == self.root or self.root in path.parents:
                            raise ValueError("Output cannot modify the immutable package")
                    item = str(path)
                if kind == "sources":
                    if Path(item).is_absolute():
                        raise ValueError("Sources must use relative paths")
                    source_root = Path(inputs["root"]).expanduser().resolve()
                    source_path = (source_root / item).resolve()
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
                raise RuntimeError("Previous process-group stop unconfirmed; inspect retained evidence")
            directory = self.receipts_dir / request["id"]
            directory.mkdir(mode=0o700, parents=True, exist_ok=False)  # Never repeat a possibly applied operation.
        except (OSError, ValueError, TypeError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
            self._preparation_failure(request.get("operation") if isinstance(request, dict) else None,
                                      "REQUEST_VALIDATION", exc)
            raise
        _atomic(directory / "request.json", request)
        result = {"schema": "loom-tool-result/v1", "request_digest": approved_digest,
                  "operation": request["operation"], "status": "RUNNING", "started_at": time.time(),
                  "receipt_directory": str(directory), "effects": request["effects"],
                  "semantic_learning_established": False, "returncode": None}
        _atomic(directory / "result.json", result)
        process = None
        output = bytearray()
        overflow = False
        try:
            if cancel_event is not None and cancel_event.is_set():
                result["status"] = "CANCELLED_BEFORE_START"
            else:
                env = {**os.environ, "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
                       "PYTHONPATH": str(self.root / "src"), "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1"}
                process = subprocess.Popen(request["argv"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                           stderr=subprocess.STDOUT, env=env, cwd=self.root, start_new_session=True)
                result["pid"] = process.pid
                _atomic(directory / "result.json", result)
                os.set_blocking(process.stdout.fileno(), False)
                output_eof = False
                start = time.monotonic()
                while process.poll() is None or not output_eof:
                    if not output_eof:
                        try:
                            chunk = os.read(process.stdout.fileno(), 4096)
                        except BlockingIOError:
                            chunk = None
                        if chunk == b"":
                            output_eof = True
                        elif chunk:
                            remaining = self.max_bytes - len(output)
                            output.extend(chunk[:remaining])
                            overflow = len(chunk) > remaining
                    if overflow:
                        result["status"] = "OUTPUT_LIMIT"; break
                    if cancel_event is not None and cancel_event.is_set():
                        result["status"] = "INTERRUPTED"; break
                    if time.monotonic() - start >= self.max_seconds:
                        result["status"] = "TIMEOUT"; break
                    time.sleep(0.025)
                if result["status"] == "RUNNING":
                    result["status"] = "COMPLETED" if process.returncode == 0 else "FAILED"
        except BaseException as exc:
            result["status"] = "INTERRUPTED" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else "FAILED"
            result["error"] = f"{type(exc).__name__}: {str(exc)[:1000]}"
        finally:
            if process is not None:
                cleanup = self._stop(process)
                result["cleanup"] = cleanup
                if result["status"] == "COMPLETED" and cleanup.get("termination_required"):
                    result["status"] = "INTERRUPTED"
                result["returncode"] = process.poll()
                if not cleanup["confirmed_stopped"]:
                    result["status"] = "STOP_UNCONFIRMED"
                    _atomic(self.receipts_dir / "STOP_UNCONFIRMED.json", {"pgid": process.pid, "request": request["id"], "cleanup": cleanup})
                if process.stdout is not None:
                    process.stdout.close()
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
