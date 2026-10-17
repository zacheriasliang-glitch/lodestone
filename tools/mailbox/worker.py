#!/usr/bin/env python3
"""Run one or two explicitly admitted, read-only Codex jobs and return results."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
try:
    import fcntl
except ImportError:  # Transport rejects unsupported execution; help still works.
    fcntl = None
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid

import mailbox as transport


MANIFEST_SCHEMA = "lodestone-worker-manifest/v1"
JOB_SCHEMA = "lodestone-readonly-job/v1"
RESULT_SCHEMA = "lodestone-worker-result/v1"
MAX_TASK = 32768
MAX_RESULT = 65536
MAX_OUTPUT = 1024 * 1024


class WorkerError(Exception):
    pass


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def encode(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("utf-8")


def decode(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise WorkerError("Duplicate JSON field")
            result[key] = value
        return result
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
    except (ValueError, UnicodeError) as exc:
        raise WorkerError("Invalid JSON") from exc


def read_limited(path, limit):
    if path.is_symlink() or not path.is_file():
        raise WorkerError("Expected a regular local file: " + str(path))
    with path.open("rb") as handle:
        value = handle.read(limit + 1)
    if len(value) > limit:
        raise WorkerError("File exceeds size limit: " + str(path))
    return value


def atomic_write(path, raw):
    """Install durable bytes once; an existing different value is an error."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists():
        if read_limited(path, max(len(raw), MAX_RESULT)) != raw:
            raise WorkerError("Immutable local record differs: " + str(path))
        return
    fd, temporary = tempfile.mkstemp(prefix=".worker-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        # Hard linking is an atomic create-if-absent, never a replacement.
        try:
            os.link(temporary, str(path))
        except FileExistsError:
            if read_limited(path, max(len(raw), MAX_RESULT)) != raw:
                raise WorkerError("Immutable local record differs: " + str(path))
        directory = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        os.unlink(temporary)


def load_manifest(path):
    value = decode(read_limited(Path(path).expanduser(), MAX_TASK))
    return validate_manifest(value)


def validate_manifest(value):
    if type(value) is not dict or set(value) != {"schema", "jobs"}:
        raise WorkerError("Manifest has missing or unknown fields")
    if value["schema"] != MANIFEST_SCHEMA or type(value["jobs"]) is not list:
        raise WorkerError("Invalid worker manifest")
    if not 1 <= len(value["jobs"]) <= 2:
        raise WorkerError("A worker batch contains one or two admitted jobs")
    seen = set()
    for job in value["jobs"]:
        if type(job) is not dict or set(job) != {"id", "sha256"}:
            raise WorkerError("Invalid manifest job fields")
        name, digest = job["id"], job["sha256"]
        if (not isinstance(name, str) or not transport.ID.fullmatch(name)
                or len(name) > 114 or name in seen):
            raise WorkerError("Invalid or duplicate admitted job id")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise WorkerError("Invalid admitted SHA-256")
        seen.add(name)
    return value["jobs"]


def validate_job(payload, expected_id):
    if len(payload) > MAX_TASK:
        raise WorkerError("Task exceeds size limit")
    value = decode(payload)
    if type(value) is not dict or set(value) != {"schema", "id", "prompt"}:
        raise WorkerError("Task has missing or unknown fields")
    if value["schema"] != JOB_SCHEMA or value["id"] != expected_id:
        raise WorkerError("Task schema or id does not match admission")
    if not isinstance(value["prompt"], str) or not value["prompt"].strip():
        raise WorkerError("Task prompt must be nonempty text")
    if "\x00" in value["prompt"]:
        raise WorkerError("Task prompt contains a NUL byte")
    return value


def codex_options(mcp_options=(), disable_code_mode_host=False):
    options = ["-a", "never", "-c", "features.apps=false",
               "-c", 'web_search="disabled"'] + list(mcp_options)
    if disable_code_mode_host:
        options.extend(["-c", "features.code_mode_host=false"])
    return options


def parse_features(raw):
    """Parse the CLI's name/stage/boolean rows; fail closed on format changes."""
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeError as exc:
        raise WorkerError("Codex feature inventory is not UTF-8") from exc
    features = {}
    for line in lines:
        if not line.strip():
            continue
        match = re.fullmatch(r"([A-Za-z0-9_]+)\s+(.+?)\s+(true|false)", line.strip())
        if match is None or match[1] in features:
            raise WorkerError("Codex feature inventory has malformed or duplicate rows")
        features[match[1]] = (match[2], match[3] == "true")
    return features


def validate_mcp_servers(value):
    """Reject ambiguous names and unknown enabled states before making overrides."""
    if type(value) is not list:
        raise WorkerError("Codex MCP inventory must be a JSON list")
    seen = set()
    for server in value:
        if (type(server) is not dict or not isinstance(server.get("name"), str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", server["name"])
                or type(server.get("enabled")) is not bool
                or server["name"] in seen):
            raise WorkerError("Codex MCP inventory has an unsupported name or enabled state")
        seen.add(server["name"])
    return value


def build_command(codex, repo, answer_path, mcp_options=(), disable_code_mode_host=False):
    return [str(codex)] + codex_options(mcp_options, disable_code_mode_host) + ["exec", "--sandbox", "read-only",
            "--ephemeral", "--cd", str(repo),
            "--output-last-message", str(answer_path), "-"]


def run_bounded(argv, input_bytes, cwd, timeout, stdout_path, stderr_path,
                max_output=MAX_OUTPUT, watched_path=None):
    """Capture bounded output and kill the process group on timeout/overflow."""
    if timeout <= 0 or max_output <= 0:
        raise WorkerError("Subprocess limits must be positive")
    paths = [Path(stdout_path), Path(stderr_path)]
    for path in paths:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    env = os.environ.copy()
    env["GIT_OPTIONAL_LOCKS"] = "0"
    with tempfile.TemporaryFile() as source, paths[0].open("wb") as out, paths[1].open("wb") as err:
        source.write(input_bytes)
        source.seek(0)
        process = subprocess.Popen(argv, stdin=source, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, cwd=str(cwd), env=env,
                                   start_new_session=True)
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, out)
        selector.register(process.stderr, selectors.EVENT_READ, err)
        started = time.monotonic()
        stopped = None
        timed_out = output_limited = False
        written = 0
        try:
            while selector.get_map() or process.poll() is None:
                now = time.monotonic()
                if stopped is None:
                    timed_out = now - started >= timeout
                    if watched_path is not None and Path(watched_path).exists():
                        output_limited = output_limited or Path(watched_path).stat().st_size > max_output
                    if timed_out or output_limited:
                        stopped = now
                        try:
                            os.killpg(process.pid, signal.SIGTERM)
                        except ProcessLookupError:
                            pass
                elif now - stopped >= 0.5:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    if now - stopped >= 1.0:
                        # An escaped descendant may still own inherited pipes.
                        # Bound draining independently of that descendant's life.
                        for key in list(selector.get_map().values()):
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                        if process.poll() is None:
                            process.kill()
                        break
                for key, unused in selector.select(0.05):
                    data = os.read(key.fileobj.fileno(), 8192)
                    if not data:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                        continue
                    remaining = max(0, max_output - written)
                    key.data.write(data[:remaining])
                    written += min(len(data), remaining)
                    if len(data) > remaining:
                        output_limited = True
            code = process.wait(timeout=1.0)
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                pass
            raise
        finally:
            selector.close()
            for handle in (process.stdout, process.stderr):
                if not handle.closed:
                    handle.close()
        return {"returncode": code, "timed_out": timed_out,
                "output_limited": output_limited, "pid": process.pid}


def git_read(repo, arguments):
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("GIT_"):
            del env[key]
    env["GIT_OPTIONAL_LOCKS"] = "0"
    command = ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
               "-C", str(repo)] + arguments
    result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env, timeout=20)
    if result.returncode or len(result.stdout) > 16384:
        raise WorkerError("Repository inspection failed or exceeded output limit")
    return result.stdout.decode("utf-8", errors="replace").rstrip("\n")


def repo_snapshot(repo):
    return {"head": git_read(repo, ["rev-parse", "HEAD"]),
            "branch": git_read(repo, ["rev-parse", "--abbrev-ref", "HEAD"]),
            "status": git_read(repo, ["status", "--porcelain=v1", "--untracked-files=normal"])}


class Worker:
    def __init__(self, mailbox, repo, manifest, codex="codex", timeout=300, interval=30,
                 disable_mcp=False, disable_code_mode_host=False):
        self.mailbox = mailbox
        self.repo = Path(repo).expanduser().resolve(strict=True)
        if not self.repo.is_dir() or Path(git_read(self.repo, ["rev-parse", "--show-toplevel"])).resolve() != self.repo:
            raise WorkerError("--repo must name the existing Git checkout root")
        if os.path.commonpath([str(self.repo), str(mailbox.state.resolve())]) == str(self.repo):
            raise WorkerError("Worker state must be outside the inspected checkout")
        self.jobs = (validate_manifest({"schema": MANIFEST_SCHEMA, "jobs": manifest})
                     if isinstance(manifest, list) else load_manifest(manifest))
        self.codex = codex
        self.disable_mcp = disable_mcp
        self.disable_code_mode_host = disable_code_mode_host
        self._mcp_options = []
        self.timeout, self.interval = timeout, interval
        if timeout <= 0 or interval < 0:
            raise WorkerError("Invalid timeout or interval")
        self.directory = mailbox.state / "worker"
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.directory.is_symlink():
            raise WorkerError("Worker directory must not be a symlink")
        self.worker_run_id = uuid.uuid4().hex
        self._locked = False
        self._preflight_result = None

    @contextmanager
    def _lock(self):
        if self._locked:
            yield
            return
        with (self.directory / "worker.lock").open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise WorkerError("A worker already owns this mailbox") from exc
            self._locked = True
            try:
                atomic_write(self.directory / "binding.json", encode({
                    "repo": str(self.repo), "jobs": self.jobs}))
                yield
            finally:
                self._locked = False
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _feature_inventory(self, label, disable_host):
        prefix = self.directory / ("preflight-" + self.worker_run_id + "-" + label)
        outcome = run_bounded([self.codex] + codex_options(self._mcp_options, disable_host) +
                              ["features", "list"], b"", self.repo, 20,
                              Path(str(prefix) + ".stdout"), Path(str(prefix) + ".stderr"),
                              max_output=65536)
        if outcome["returncode"] or outcome["timed_out"] or outcome["output_limited"]:
            raise WorkerError("Codex " + label + " preflight did not succeed")
        return parse_features(read_limited(Path(str(prefix) + ".stdout"), 65536))

    def _preflight(self):
        if self._preflight_result is not None:
            return self._preflight_result
        version, problem = None, None
        executable = shutil.which(str(self.codex))
        if any(os.environ.get(name) for name in ("CODEX_API_KEY", "OPENAI_API_KEY")):
            problem = "An API key is configured; this trial requires the existing ChatGPT login"
        elif executable is None:
            problem = "Codex CLI was not found; no installation or new API key was attempted"
        else:
            self.codex = executable
            try:
                outputs = []
                for label, arguments in (("version", ["--version"]),
                                         ("help", ["exec", "--help"]),
                                         ("auth", ["login", "status"]),
                                         ("mcp", codex_options() + ["mcp", "list", "--json"])):
                    prefix = self.directory / ("preflight-" + self.worker_run_id + "-" + label)
                    outcome = run_bounded([executable] + arguments, b"", self.repo, 20,
                                          Path(str(prefix) + ".stdout"), Path(str(prefix) + ".stderr"),
                                          max_output=65536)
                    raw = read_limited(Path(str(prefix) + ".stdout"), 65536)
                    if label != "mcp":
                        raw += read_limited(Path(str(prefix) + ".stderr"), 65536)
                    outputs.append(raw.decode("utf-8", errors="replace"))
                    if outcome["returncode"] or outcome["timed_out"] or outcome["output_limited"]:
                        raise WorkerError("Codex " + label + " preflight did not succeed")
                version = outputs[0].strip()[:200]
                if not all(flag in outputs[1] for flag in
                           ("--sandbox", "--ephemeral", "--cd", "--output-last-message")):
                    raise WorkerError("Installed Codex lacks required read-only execution flags")
                if "logged in using chatgpt" not in outputs[2].lower():
                    raise WorkerError("A saved ChatGPT login was not confirmed; no API key was requested")
                servers = decode(outputs[3].encode("utf-8"))
                if not self.disable_mcp and (type(servers) is not list or servers):
                    raise WorkerError("Configured MCP tools need explicit read-only scoping")
                if self.disable_mcp:
                    servers = validate_mcp_servers(servers)
                    options = []
                    for server in servers:
                        options.extend(["-c", "mcp_servers." + server["name"] + ".enabled=false"])
                    prefix = self.directory / ("preflight-" + self.worker_run_id + "-mcp-scoped")
                    outcome = run_bounded([executable] + codex_options(options, self.disable_code_mode_host) +
                                          ["mcp", "list", "--json"], b"", self.repo, 20,
                                          Path(str(prefix) + ".stdout"), Path(str(prefix) + ".stderr"),
                                          max_output=65536)
                    if outcome["returncode"] or outcome["timed_out"] or outcome["output_limited"]:
                        raise WorkerError("Codex scoped MCP preflight did not succeed")
                    effective = validate_mcp_servers(decode(read_limited(
                        Path(str(prefix) + ".stdout"), 65536)))
                    if {server["name"] for server in effective} != {server["name"] for server in servers}:
                        raise WorkerError("Codex MCP inventory changed during per-run scoping")
                    if any(server["enabled"] for server in effective):
                        raise WorkerError("Codex still has enabled MCP servers after per-run scoping")
                    self._mcp_options = options
                if self.disable_code_mode_host:
                    baseline = self._feature_inventory("features-baseline", False)
                    scoped = self._feature_inventory("features-scoped", True)
                    if "code_mode_host" not in baseline or "code_mode_host" not in scoped:
                        raise WorkerError("Codex feature inventory does not expose code_mode_host")
                    if scoped["code_mode_host"][1] is not False:
                        raise WorkerError("Codex code_mode_host remains enabled after per-run scoping")
                    expected = dict(baseline)
                    expected["code_mode_host"] = (baseline["code_mode_host"][0], False)
                    if scoped != expected:
                        raise WorkerError("Codex feature inventory changed beyond code_mode_host")
                    if not all(scoped.get(name, (None, False))[1] is True
                               for name in ("shell_tool", "unified_exec")):
                        raise WorkerError("Codex shell_tool and unified_exec must remain enabled")
            except (OSError, WorkerError) as exc:
                problem = str(exc)
        self._preflight_result = (version, problem)
        return self._preflight_result

    def _task(self, admitted):
        folder = self.mailbox.state / "inbox" / "mac" / admitted["id"]
        if not folder.exists():
            self.mailbox.receive("mac")
        if not folder.exists():
            return None
        if folder.is_symlink():
            raise WorkerError("Task directory must not be a symlink")
        try:
            envelope = transport._decode(read_limited(folder / "envelope.json", transport.MAX_ENVELOPE))
            payload = transport.validate(envelope, admitted["id"], "mac")
        except transport.MailboxError as exc:
            raise WorkerError(str(exc)) from exc
        if envelope["kind"] != "file" or envelope["sha256"] != admitted["sha256"]:
            raise WorkerError("Task envelope does not match the admitted payload hash")
        if read_limited(folder / "payload", MAX_TASK) != payload:
            raise WorkerError("Inbox payload differs from its envelope")
        return validate_job(payload, admitted["id"])

    def _result(self, admitted, claim, status, answer="", error=None, exit_code=None,
                invocation_pid=None, after=None):
        answer = answer.encode("utf-8")[:32768].decode("utf-8", errors="ignore")
        value = {"schema": RESULT_SCHEMA, "request_id": admitted["id"],
                 "request_sha256": admitted["sha256"], "status": status,
                 "worker": "codex-cli", "version": claim.get("version"),
                 "worker_run_id": claim["worker_run_id"], "invocation_pid": invocation_pid,
                 "started_at": claim["started_at"], "finished_at": utc_now(),
                 "repo_before": claim.get("repo_before"), "repo_after": after,
                 "exit_code": exit_code, "answer": answer, "error": error}
        raw = encode(value)
        if len(raw) > MAX_RESULT:
            value.update(status="error", answer="", error="Result exceeded 64 KiB")
            raw = encode(value)
        if len(raw) > MAX_RESULT:
            raise WorkerError("Repository metadata exceeded result limit")
        return raw

    def _save_result(self, folder, raw):
        atomic_write(folder / "result.json", raw)
        result = decode(raw)
        packet = transport.pack(raw, result["request_id"] + "-reply", "cloud")
        atomic_write(folder / "envelope.json", transport._encode(packet))

    def _publish(self, folder, admitted):
        raw = read_limited(folder / "result.json", MAX_RESULT)
        result = decode(raw)
        if result.get("request_id") != admitted["id"] or result.get("request_sha256") != admitted["sha256"]:
            raise WorkerError("Stored result does not match admission")
        self._save_result(folder, raw)
        packet = transport._decode(read_limited(folder / "envelope.json", transport.MAX_ENVELOPE))
        if transport.validate(packet, admitted["id"] + "-reply", "cloud") != raw:
            raise WorkerError("Stored reply envelope differs from result")
        self.mailbox.send(packet)
        atomic_write(folder / "sent.json", encode({"id": packet["id"], "sha256": packet["sha256"]}))
        print("Sent %s: %s" % (packet["id"], result["status"]), flush=True)

    def _verify_sent(self, folder, admitted):
        result_raw = read_limited(folder / "result.json", MAX_RESULT)
        result = decode(result_raw)
        if result.get("request_id") != admitted["id"] or result.get("request_sha256") != admitted["sha256"]:
            raise WorkerError("Sent result does not match admission")
        packet = transport._decode(read_limited(folder / "envelope.json", transport.MAX_ENVELOPE))
        if transport.validate(packet, admitted["id"] + "-reply", "cloud") != result_raw:
            raise WorkerError("Sent envelope differs from its result")
        sent = decode(read_limited(folder / "sent.json", MAX_RESULT))
        if sent != {"id": packet["id"], "sha256": packet["sha256"]}:
            raise WorkerError("Sent record differs from its envelope")

    def _step(self):
        for admitted in self.jobs:
            folder = self.directory / "jobs" / admitted["id"]
            if folder.is_symlink():
                raise WorkerError("Job record directory must not be a symlink")
            if (folder / "sent.json").exists():
                self._verify_sent(folder, admitted)
                continue
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            if (folder / "result.json").exists():
                self._publish(folder, admitted)
                return False
            if (folder / "claim.json").exists():
                claim = decode(read_limited(folder / "claim.json", MAX_RESULT))
                if (claim.get("request_id") != admitted["id"]
                        or claim.get("request_sha256") != admitted["sha256"]
                        or claim.get("repo") != str(self.repo)
                        or not isinstance(claim.get("started_at"), str)
                        or not isinstance(claim.get("worker_run_id"), str)
                        or not re.fullmatch(r"[0-9a-f]{32}", claim["worker_run_id"])):
                    raise WorkerError("Stored claim does not match this job")
                # The previous worker could have died while its separate Codex
                # process group survived. Never launch another job in this batch.
                aborted = self.directory / "aborted.json"
                if not aborted.exists():
                    atomic_write(aborted, encode({"request_id": admitted["id"],
                        "worker_run_id": claim["worker_run_id"],
                        "reason": "Unfinished claim may have a surviving process"}))
                self._save_result(folder, self._result(admitted, claim, "interrupted",
                    error="Earlier execution has no durable result; it will not be repeated"))
                self._publish(folder, admitted)
                return False
            problem = None
            if (self.directory / "aborted.json").exists():
                job, problem = None, "Batch stopped after an interrupted claim; a previous process may still be alive"
            else:
                try:
                    job = self._task(admitted)
                    if job is None:
                        return False
                except (WorkerError, ValueError) as exc:
                    job, problem = None, str(exc)
            version = None
            if problem is None:
                version, problem = self._preflight()
            before = None
            try:
                before = repo_snapshot(self.repo)
            except (WorkerError, OSError, subprocess.SubprocessError) as exc:
                problem = str(exc)
            claim = {"request_id": admitted["id"], "request_sha256": admitted["sha256"],
                     "repo": str(self.repo), "repo_before": before,
                     "worker_run_id": self.worker_run_id, "version": version, "started_at": utc_now()}
            atomic_write(folder / "claim.json", encode(claim))
            if problem is not None:
                self._save_result(folder, self._result(admitted, claim, "blocked", error=problem, after=before))
            else:
                answer_path = folder / "answer.txt"
                print("Running " + admitted["id"], flush=True)
                try:
                    outcome = run_bounded(build_command(self.codex, self.repo, answer_path,
                                                        self._mcp_options, self.disable_code_mode_host),
                                          job["prompt"].encode("utf-8"), self.repo, self.timeout,
                                          folder / "stdout.log", folder / "stderr.log",
                                          watched_path=answer_path)
                    after = repo_snapshot(self.repo)
                    answer, error = "", None
                    if outcome["timed_out"]:
                        status, error = "timeout", "Codex exceeded the task deadline"
                    elif outcome["output_limited"]:
                        status, error = "error", "Codex exceeded the output limit"
                    elif outcome["returncode"]:
                        status, error = "error", "Codex exited unsuccessfully; inspect local stderr.log"
                    elif after != before:
                        status, error = "error", "Repository state changed during the read-only task; no restoration attempted"
                    elif not answer_path.exists():
                        status, error = "error", "Codex returned no final answer file"
                    else:
                        answer = read_limited(answer_path, MAX_OUTPUT).decode("utf-8", errors="replace")
                        status = "ok" if answer.strip() else "error"
                        if status != "ok":
                            error = "Codex returned an empty final answer"
                    raw = self._result(admitted, claim, status, answer, error,
                                       outcome["returncode"], outcome["pid"], after)
                except (WorkerError, OSError, subprocess.SubprocessError) as exc:
                    raw = self._result(admitted, claim, "error", error=str(exc))
                self._save_result(folder, raw)
            self._publish(folder, admitted)
            return False
        return True

    def step(self):
        with self._lock():
            return self._step()

    def run(self):
        with self._lock():
            print("Worker %s: %d admitted read-only job(s); Ctrl-C stops." %
                  (self.worker_run_id, len(self.jobs)), flush=True)
            print("Script: " + str(Path(__file__).resolve()), flush=True)
            print("State: " + str(self.mailbox.state), flush=True)
            while True:
                try:
                    if self._step():
                        break
                except (transport.MailboxError, OSError) as exc:
                    print("Transport pending: " + str(exc), file=sys.stderr, flush=True)
                # No mailbox operation lock is held while waiting or running Codex.
                time.sleep(self.interval)
            results = [decode(read_limited(self.directory / "jobs" / item["id"] / "result.json", MAX_RESULT))
                       for item in self.jobs]
            return 0 if all(item["status"] == "ok" for item in results) else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--disable-mcp", action="store_true",
                        help="Disable configured MCP servers for this worker's Codex invocations only")
    parser.add_argument("--disable-code-mode-host", action="store_true",
                        help="Disable code_mode_host for this worker's Codex invocations only")
    parser.add_argument("--state", required=True)
    parser.add_argument("--remote", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--interval", type=float, default=30)
    parser.add_argument("--timeout", type=float, default=300)
    args = parser.parse_args(argv)
    if args.interval < 1 or args.timeout < 1 or args.timeout > 900:
        parser.error("--interval must be >=1 and --timeout must be between 1 and 900 seconds")
    os.umask(0o077)
    try:
        mailbox = transport.Mailbox(args.remote, args.branch, args.state)
        return Worker(mailbox, args.repo, args.manifest, args.codex, args.timeout, args.interval,
                      disable_mcp=args.disable_mcp, disable_code_mode_host=args.disable_code_mode_host).run()
    except KeyboardInterrupt:
        print("Worker stopped; an unfinished claimed job will not run again.", file=sys.stderr)
        return 130
    except (WorkerError, transport.MailboxError, OSError, subprocess.SubprocessError) as exc:
        print("Worker error: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
