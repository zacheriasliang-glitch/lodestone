"""Explicit, receipt-bound GitHub workflow dispatch using existing gh authentication."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import uuid
from urllib.parse import quote

API_VERSION = "2026-03-10"
PLAN_SCHEMA = "lodestone-workflow-dispatch-plan/v1"
MAX_BYTES = 2 * 1024 * 1024


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode() + b"\n"


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def read(path):
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("Receipt or plan exceeds size bound")
    return raw, json.loads(raw)


def save_new(path, value):
    """Exclusive, durable records; never overwrite an earlier dispatch attempt."""
    path = Path(path)
    with path.open("xb") as stream:
        stream.write(canonical(value))
        stream.flush()
        os.fsync(stream.fileno())
    if os.name != "nt":
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def mkdir_durable(path):
    path = Path(path)
    if path.is_dir():
        return
    mkdir_durable(path.parent)
    path.mkdir(exist_ok=True)
    if os.name != "nt":
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


@dataclass(frozen=True)
class Response:
    status: int
    body: object = None


class TransportError(RuntimeError):
    pass


class GhTransport:
    """No shell, token extraction, POST retries, or raw diagnostic logging."""
    def request(self, method, endpoint, body=None):
        if method not in {"GET", "POST"} or not endpoint.startswith("/repos/"):
            raise ValueError("Unsupported API request")
        argv = ["gh", "api", "--hostname", "github.com", "--method", method,
                "--include", "-H", "Accept: application/vnd.github+json",
                "-H", "X-GitHub-Api-Version: " + API_VERSION, endpoint]
        payload = None
        if body is not None:
            argv.extend(["--input", "-"])
            payload = canonical(body)
        env = dict(os.environ)
        env.pop("GH_DEBUG", None)
        env.pop("GH_FORCE_TTY", None)
        env.update(GH_PROMPT_DISABLED="1", GH_PAGER="", PAGER="")
        try:
            result = subprocess.run(argv, input=payload, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, timeout=30, env=env,
                                    check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TransportError(type(exc).__name__) from None
        if len(result.stdout) > MAX_BYTES:
            raise TransportError("Response exceeds size bound")
        raw = result.stdout.replace(b"\r\n", b"\n")
        header, separator, content = raw.partition(b"\n\n")
        matched = re.match(rb"HTTP/\S+ ([0-9]{3})(?: |\n|$)", header)
        if not separator or not matched:
            raise TransportError("No unambiguous HTTP response")
        status = int(matched.group(1))
        if result.returncode and 200 <= status < 300:
            raise TransportError("CLI failed after an apparent success response")
        try:
            decoded = json.loads(content) if content.strip() else None
        except (UnicodeError, ValueError):
            # Preserve the HTTP acceptance class even when its body is unusable.
            decoded = None
        return Response(status, decoded)


def validate_request(request):
    if type(request) is not dict or set(request) != {"repository", "workflow", "ref", "expected_sha", "inputs"}:
        raise ValueError("Unexpected dispatch request fields")
    repo, workflow, ref = (request[k] for k in ("repository", "workflow", "ref"))
    if (type(repo) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", repo)
            or repo.split("/")[-1] in {".", ".."}):
        raise ValueError("Repository must be owner/name on github.com")
    if (type(workflow) is not str or not re.fullmatch(r"[1-9][0-9]*|[A-Za-z0-9_.-]+\.ya?ml", workflow)
            or workflow.startswith(".")):
        raise ValueError("Workflow must be a numeric ID or a workflow filename")
    if (type(ref) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", ref)
            or ".." in ref or "//" in ref or ref.endswith(("/", ".", ".lock"))
            or ref.startswith("refs/")):
        raise ValueError("Use an explicit branch or tag name, not a SHA or full ref")
    if type(request["expected_sha"]) is not str or not re.fullmatch(r"[0-9a-f]{40}", request["expected_sha"]):
        raise ValueError("Expected commit must be a full lowercase SHA")
    inputs = request["inputs"]
    if type(inputs) is not dict or len(inputs) > 25 or len(canonical(inputs)) > 65535:
        raise ValueError("Workflow inputs exceed bounds")
    for key, value in inputs.items():
        if type(key) is not str or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,99}", key):
            raise ValueError("Invalid workflow input name")
        if type(value) not in (str, bool, int, float) or type(value) is float and not math.isfinite(value):
            raise ValueError("Workflow inputs must be scalar strings, booleans or numbers")
    return request


def get(transport, endpoint):
    response = transport.request("GET", endpoint)
    if response.status != 200 or type(response.body) is not dict:
        raise ValueError("Read-only preflight did not return the required object")
    return response.body


def preflight(request, transport):
    request = validate_request(request)
    root = "/repos/" + request["repository"]
    repository = get(transport, root)
    if (repository.get("full_name", "").casefold() != request["repository"].casefold()
            or type(repository.get("id")) is not int or repository.get("private") is not False):
        raise ValueError("This adapter requires the named public repository")
    matches = []
    for kind in ("heads", "tags"):
        response = transport.request("GET", root + "/git/ref/" + kind + "/" + quote(request["ref"], safe=""))
        if response.status == 404:
            continue
        if response.status != 200 or type(response.body) is not dict:
            raise ValueError("Could not resolve the selected branch/tag")
        if response.body.get("ref") != "refs/" + kind + "/" + request["ref"]:
            raise ValueError("GitHub returned a different ref")
        obj = response.body["object"]
        seen = set()
        while obj.get("type") == "tag" and len(seen) < 8 and obj.get("sha") not in seen:
            if not re.fullmatch(r"[0-9a-f]{40}", obj.get("sha", "")):
                raise ValueError("Malformed annotated-tag identity")
            seen.add(obj["sha"])
            obj = get(transport, root + "/git/tags/" + obj["sha"])["object"]
        if obj.get("type") != "commit" or obj.get("sha") != request["expected_sha"]:
            raise ValueError("Ref does not resolve to the expected commit")
        matches.append(kind)
    if len(matches) != 1:
        raise ValueError("Ref must name exactly one branch or tag; no ambiguous names")
    workflow = get(transport, root + "/actions/workflows/" + quote(request["workflow"], safe=""))
    path = workflow.get("path", "")
    if (workflow.get("state") != "active" or type(workflow.get("id")) is not int
            or not re.fullmatch(r"\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml", path)):
        raise ValueError("Workflow must be active with a bounded workflow-file path")
    # The event must be available on the default branch; the selected version is pinned separately.
    default = repository.get("default_branch")
    if type(default) is not str or not default:
        raise ValueError("Repository has no default branch")
    definition = get(transport, root + "/contents/" + path + "?ref=" + request["expected_sha"])
    default_definition = get(transport, root + "/contents/" + path + "?ref=" + quote(default, safe=""))
    for item in (definition, default_definition):
        if item.get("type") != "file" or item.get("path") != path or not re.fullmatch(r"[0-9a-f]{40}", item.get("sha", "")):
            raise ValueError("Workflow file identity unavailable")
    return {"repository_id": repository["id"], "repository_full_name": repository["full_name"],
            "workflow_id": workflow["id"],
            "workflow_path": path, "workflow_blob_sha": definition["sha"],
            "ref_kind": matches[0], "resolved_sha": request["expected_sha"]}


def create_plan(request, state_dir, output, transport=None):
    """GET-only planning. It does not dispatch or prove workflow-side safety."""
    transport = transport or GhTransport()
    request = validate_request(request)
    plan = {"schema": PLAN_SCHEMA, "api_version": API_VERSION, "created_at": now(),
            "request": request, "binding": preflight(request, transport),
            "state_dir": str(Path(state_dir).resolve()), "intent_id": uuid.uuid4().hex}
    save_new(output, plan)
    return {"status": "PLANNED_NOT_DISPATCHED", "plan_sha256": digest(canonical(plan)),
            "plan_file": str(Path(output).resolve()), "post_requests": 0}


def load_plan(path, expected_digest):
    raw, plan = read(path)
    if digest(raw) != expected_digest or raw != canonical(plan):
        raise ValueError("Plan bytes do not match the explicitly selected SHA-256")
    if (set(plan) != {"schema", "api_version", "created_at", "request", "binding", "state_dir", "intent_id"}
            or plan["schema"] != PLAN_SCHEMA or plan["api_version"] != API_VERSION
            or not re.fullmatch(r"[0-9a-f]{32}", plan["intent_id"])
            or not Path(plan["state_dir"]).is_absolute()):
        raise ValueError("Invalid dispatch plan")
    validate_request(plan["request"])
    return plan


def observe(plan, run_id, transport):
    root = "/repos/" + plan["request"]["repository"]
    response = transport.request("GET", root + "/actions/runs/" + str(run_id))
    if response.status != 200 or type(response.body) is not dict:
        return {"state": "RUN_LOOKUP_PENDING", "http_status": response.status, "completed": False}
    run = response.body
    repository = run.get("repository") if type(run.get("repository")) is dict else {}
    workflow_path = run.get("path") if type(run.get("path")) is str else ""
    checks = {"exact_run_id": type(run.get("id")) is int and run["id"] == run_id,
              "repository": type(repository.get("id")) is int and repository["id"] == plan["binding"]["repository_id"],
              "workflow": type(run.get("workflow_id")) is int and run["workflow_id"] == plan["binding"]["workflow_id"],
              "workflow_path": workflow_path.split("@", 1)[0] == plan["binding"]["workflow_path"],
              "event": run.get("event") == "workflow_dispatch",
              "commit": run.get("head_sha") == plan["request"]["expected_sha"],
              "original_attempt": type(run.get("run_attempt")) is int and run["run_attempt"] == 1}
    if not all(checks.values()):
        return {"state": "RUN_BINDING_MISMATCH", "checks": checks, "completed": False}
    completed = run.get("status") == "completed"
    return {"state": "RUN_COMPLETED" if completed else "RUN_PENDING", "checks": checks,
            "run_id": run_id, "run_attempt": 1, "status": run.get("status"),
            "conclusion": run.get("conclusion"), "completed": completed,
            "successful": completed and run.get("conclusion") == "success"}


def execute_plan(path, expected_digest, transport=None):
    """One POST per durable intent. A timeout or legacy 204 never triggers a retry."""
    transport = transport or GhTransport()
    plan = load_plan(path, expected_digest)
    if preflight(plan["request"], transport) != plan["binding"]:
        raise ValueError("Ref or workflow identity changed since planning")
    parent = Path(plan["state_dir"]) / "intents"
    mkdir_durable(parent)
    directory = parent / plan["intent_id"]
    directory.mkdir()  # Exclusive claim, including after a process crash or ambiguous POST.
    if os.name != "nt":
        descriptor = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    save_new(directory / "intent.json", {"schema": "lodestone-workflow-dispatch-intent/v1",
             "created_at": now(), "plan_sha256": expected_digest, "plan": plan,
             "state": "POST_RESERVED_DO_NOT_REPEAT"})
    root = "/repos/" + plan["request"]["repository"]
    endpoint = root + "/actions/workflows/" + str(plan["binding"]["workflow_id"]) + "/dispatches"
    receipt = {"schema": "lodestone-workflow-dispatch-receipt/v1", "recorded_at": now(),
               "plan_sha256": expected_digest, "post_attempts": 1, "automatic_retry": False}
    try:
        response = transport.request("POST", endpoint,
            {"ref": plan["request"]["ref"], "inputs": plan["request"]["inputs"]})
        receipt["http_status"] = response.status
        if response.status == 200:
            body = response.body if type(response.body) is dict else {}
            run_id = body.get("workflow_run_id")
            canonical_repo = plan["binding"]["repository_full_name"]
            api_url = "https://api.github.com/repos/" + canonical_repo + "/actions/runs/" + str(run_id)
            html_url = "https://github.com/" + canonical_repo + "/actions/runs/" + str(run_id)
            if type(run_id) is int and run_id > 0 and body.get("run_url") == api_url and body.get("html_url") == html_url:
                receipt.update(state="ACCEPTED_WITH_RUN_ID", run_id=run_id, html_url=html_url)
            else:
                receipt["state"] = "ACCEPTED_UNBOUND"
        elif response.status == 204:
            receipt["state"] = "ACCEPTED_UNBOUND"
        elif 400 <= response.status < 500 and response.status != 408:
            receipt["state"] = "REJECTED"
        else:
            receipt["state"] = "POST_AMBIGUOUS"
    except (TransportError, OSError, subprocess.TimeoutExpired):
        receipt["state"] = "POST_AMBIGUOUS"
    save_new(directory / "dispatch.json", receipt)
    if "run_id" in receipt:
        try:
            observation = observe(plan, receipt["run_id"], transport)
        except (TransportError, OSError, subprocess.TimeoutExpired):
            observation = {"state": "RUN_LOOKUP_PENDING", "completed": False}
        save_new(directory / "observation-001.json", observation)
    else:
        observation = {"state": receipt["state"], "completed": False}
    return {"intent_directory": str(directory), "dispatch": receipt, "observation": observation}


def inspect_intent(directory, transport=None):
    """Read one known run; never submit a replacement or guess from recent runs."""
    directory = Path(directory)
    _, intent = read(directory / "intent.json")
    plan = intent["plan"]
    if digest(canonical(plan)) != intent["plan_sha256"]:
        raise ValueError("Intent no longer binds its plan")
    validate_request(plan["request"])
    if not (directory / "dispatch.json").exists():
        return {"state": "RESERVED_OUTCOME_UNKNOWN", "completed": False, "automatic_retry": False}
    _, receipt = read(directory / "dispatch.json")
    if receipt.get("plan_sha256") != intent["plan_sha256"]:
        raise ValueError("Dispatch receipt detached from plan")
    if receipt.get("state") != "ACCEPTED_WITH_RUN_ID" or type(receipt.get("run_id")) is not int:
        return {"state": receipt["state"], "completed": False, "automatic_retry": False}
    try:
        observation = observe(plan, receipt["run_id"], transport or GhTransport())
    except (TransportError, OSError, subprocess.TimeoutExpired):
        observation = {"state": "RUN_LOOKUP_PENDING", "completed": False}
    save_new(directory / ("observation-" + uuid.uuid4().hex + ".json"), observation)
    return observation


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command")
    planning = sub.add_parser("plan", help="Read-only API preflight; no workflow starts")
    for field in ("repository", "workflow", "ref", "expected-sha", "state-dir", "output"):
        planning.add_argument("--" + field, required=True)
    planning.add_argument("--inputs", help="Local JSON object; do not put secrets in workflow inputs")
    execute = sub.add_parser("execute", help="Submit the exact reviewed plan once")
    execute.add_argument("--plan", required=True)
    execute.add_argument("--plan-sha256", required=True)
    status = sub.add_parser("status", help="Inspect an existing intent; GET only")
    status.add_argument("intent_directory")
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    try:
        if args.command == "plan":
            inputs = read(args.inputs)[1] if args.inputs else {}
            result = create_plan({"repository": args.repository, "workflow": args.workflow,
                "ref": args.ref, "expected_sha": args.expected_sha, "inputs": inputs}, args.state_dir, args.output)
        elif args.command == "execute":
            result = execute_plan(args.plan, args.plan_sha256)
        else:
            result = inspect_intent(args.intent_directory)
        print(canonical(result).decode(), end="")
        return 0
    except (OSError, ValueError, KeyError, TypeError, TransportError) as exc:
        # Never echo gh stderr, credentials or arbitrary remote response bodies.
        print(json.dumps({"status": "STOPPED", "error_type": type(exc).__name__,
                          "message": "Operation did not finish. Preserve any intent; inspect it before another action."}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
