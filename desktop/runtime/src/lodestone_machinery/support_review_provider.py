"""One explicit reviewer invocation over an exact, retained support request.

The existing Origami launcher owns command isolation and authentication paths.
Callables are trusted local code. A durable started fence prevents automatic
replay, including after a crash. Attribution is declared, never authenticated.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from time import perf_counter

from . import origami
from .records import RecordStore, canonical
from .support_review import (ATTRIBUTION, _check_json_depth, _validate_review,
                             publish_support_review, read_review_file,
                             reviewed_answer, support_request)
from .usage import normalize_usage

DIRECTORY = "support-review-provider"
SCHEMA = "origami-reviewer-call/v1"
TERMINAL = {"REVIEWED_PASS", "REVIEWED_FAIL", "REVIEWED_UNKNOWN",
            "REVIEW_FAILED", "REVIEW_INVALID", "REVIEW_UNAVAILABLE"}
ACCOUNTING = ("One local reviewer invocation/capture interval, canonical retained JSON bytes and linked raw command capture; "
              "excludes preceding preparation, later validation/persistence, upstream work and physical compute. "
              "Provider-reported tokens are not complete costs; monetary cost is UNKNOWN.")


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _data(value):
    raw = canonical(value)
    _require(len(raw) <= origami.MAX_JSON, "Reviewer evidence exceeds byte bound")
    _check_json_depth(raw.decode("utf-8"))
    return raw


def _pin(raw):
    return {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def _sync(directory):
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write(path, value):
    """Publish complete bytes without replacing retained evidence."""
    raw = _data(value)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".pending-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
        _sync(path.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return _pin(raw)


def _read(path):
    value = read_review_file(path)
    # Every sidecar is canonical. This also detects whitespace-only alteration.
    raw = _data(value)
    _require(path.read_bytes() == raw, "Reviewer sidecar bytes changed")
    return value, _pin(raw)


def _directory(organ, run_id):
    path = organ._dir(run_id) / DIRECTORY
    _require(not path.is_symlink(), "Reviewer sidecar must not be a symlink")
    if path.exists():
        _require(path.is_dir(), "Reviewer sidecar must be a directory")
    return path


def _command_capture(organ, directory, request, started, *, incomplete=False):
    """Reopen exact stdout and execution evidence, bound to this review request."""
    declared = started.get("command_capture")
    if declared is None:
        return None
    _require(declared == "command", "Unknown reviewer command capture directory")
    path = directory / declared
    _require(not path.is_symlink(), "Reviewer command capture must not be a symlink")
    if incomplete and (not path.exists() or not (path / "capture.json").exists()):
        return None
    capture = origami.read_provider_capture(path)
    execution = capture["execution"]
    wire_request = json.dumps(request, ensure_ascii=False).encode("utf-8")
    _require(type(started.get("command_argv_sha256")) is str
             and len(started["command_argv_sha256"]) == 64
             and type(request.get("source_root")) is str, "Invalid reviewer command declaration")
    _require(execution.get("request") == _pin(wire_request)
             and execution.get("constraints") == started.get("constraints")
             and execution["command"].get("argv_sha256") == started["command_argv_sha256"]
             and execution.get("source_root_sha256") == hashlib.sha256(
                 str(Path(request["source_root"]).resolve()).encode("utf-8")).hexdigest()
             and execution.get("workspace_sha256") == hashlib.sha256(
                 str(organ.workspace.resolve()).encode("utf-8")).hexdigest(),
             "Reviewer command capture differs from its declared request or execution scope")
    return capture


def _captured_response(capture):
    if capture is None or capture["execution"]["stdout"]["truncated"]:
        return None, "RAW_RESPONSE_INCOMPLETE"
    try:
        value = read_review_file(Path(capture["directory"]) / "stdout.bin")
        _data(value)
        return value, None
    except (ValueError, OSError, TypeError, RecursionError, UnicodeError) as exc:
        return None, type(exc).__name__


def _command_succeeded(capture):
    execution = capture["execution"]
    return (execution["status"] == "EXITED" and execution["process"].get("returncode") == 0
            and execution["stdout"]["complete"] and not execution["stdout"]["truncated"])


def _usage(envelope):
    try:
        usage = normalize_usage(envelope.get("usage") if type(envelope) is dict else None)
        if len(canonical(usage)) > 65536:
            # Preserve counters without duplicating a huge provider metadata
            # object; the actual raw usage is still in response.json.
            usage["raw"] = None
            return usage, "RAW_USAGE_RETAINED_IN_RESPONSE"
        return usage, None
    except (ValueError, TypeError):
        return normalize_usage(None), "INVALID_REPORTED_USAGE"


def _envelope(value, request):
    origami._keys(value, ("provider", "model", "response_id", "usage", "response"), "reviewer envelope")
    for name in ("provider", "model"):
        origami._text(value[name], name)
    if value["response_id"] is not None:
        origami._text(value["response_id"], "response_id")
    _require(value["provider"] == request["reviewer"], "Reviewer provider differs from declared checker")
    return _validate_review(value["response"], request)


def _result(run_id, directory, status, call, *, review=None, answer=None,
            error_type=None, reused=True, reviewer_invoked=False):
    return {"status": status, "run_id": run_id,
            "review_handle": review["handle"] if review is not None else None,
            "review": review, "answer": answer, "call": call,
            "sidecar_path": str(directory), "error_type": error_type,
            "reused": reused, "reviewer_invoked": reviewer_invoked,
            "automatic_retry": False, "authority": "NONE", "attribution": ATTRIBUTION}


def _read_retained(organ, run_id):
    """Caller holds the encounter lock so appended sidecars form one snapshot."""
    directory = _directory(organ, run_id)
    if not directory.exists():
        return None
    request, request_pin = _read(directory / "request.json")
    started, started_pin = _read(directory / "started.json")
    _require(type(request) is dict and request.get("run_id") == run_id,
             "Reviewer request belongs to another run")
    body = {key: value for key, value in request.items() if key != "request_sha256"}
    _require(request.get("request_sha256") == hashlib.sha256(canonical(body)).hexdigest(),
             "Reviewer request digest differs")
    _require(type(started) is dict and started.get("schema") == SCHEMA
             and started.get("run_id") == run_id and started.get("request") == request_pin
             and started.get("review_request_sha256") == request["request_sha256"],
             "Reviewer start does not bind retained request")
    pins = {"request.json": request_pin, "started.json": started_pin}
    has_result = (directory / "result.json").exists() or (directory / "result.json").is_symlink()
    command_capture = _command_capture(organ, directory, request, started, incomplete=not has_result)
    raw_envelope, raw_error = (None, None)
    if command_capture is not None:
        pins.update({"command/" + name: pin for name, pin in command_capture["files"].items()})
        raw_envelope, raw_error = _captured_response(command_capture)
    envelope = None
    if (directory / "response.json").exists() or (directory / "response.json").is_symlink():
        envelope, pins["response.json"] = _read(directory / "response.json")
        if command_capture is not None:
            _require(raw_error is None and canonical(envelope) == canonical(raw_envelope),
                     "Canonical reviewer response differs from captured stdout")
    call = None
    if (directory / "call.json").exists() or (directory / "call.json").is_symlink():
        call, pins["call.json"] = _read(directory / "call.json")
        _require(type(call) is dict and call.get("schema") == SCHEMA and call.get("run_id") == run_id
                 and call.get("request") == request_pin and call.get("constraints") == started.get("constraints")
                 and call.get("response") == pins.get("response.json")
                 and call.get("request_bytes") == request_pin["bytes"]
                 and call.get("response_bytes") == (pins["response.json"]["bytes"] if "response.json" in pins else None)
                 and call.get("command_capture") == command_capture
                 and call.get("response_retained") == ("response.json" in pins)
                 and type(call.get("seconds")) in (int, float) and math.isfinite(call["seconds"])
                 and call["seconds"] >= 0, "Reviewer accounting differs from retained evidence")
        if "raw_response_retained" in call or started.get("command_capture"):
            _require(call.get("raw_response_retained") is (command_capture is not None),
                     "Reviewer raw response retention differs from captured evidence")
        usage, usage_error = _usage(envelope)
        _require(call.get("usage") == usage and call.get("usage_error") == usage_error,
                 "Reviewer usage differs from retained response")
    if not has_result:
        if call is None:
            usage, usage_error = _usage(envelope if envelope is not None else raw_envelope)
            call = {"usage": usage, "usage_error": usage_error, "request_bytes": request_pin["bytes"],
                    "response_bytes": pins.get("response.json", {}).get("bytes"), "seconds": None,
                    "invocation": "MAY_HAVE_RUN", "constraints": started["constraints"],
                    "command_capture": command_capture,
                    "command_capture_status": "INCOMPLETE" if started.get("command_capture") and command_capture is None else "RETAINED" if command_capture else "NOT_A_COMMAND",
                    "cost": "UNKNOWN", "accounting_scope": ACCOUNTING}
        return _result(run_id, directory, "REVIEW_INTERRUPTED", call)
    result, _ = _read(directory / "result.json")
    _require(type(result) is dict and result.get("schema") == SCHEMA and result.get("run_id") == run_id
             and result.get("status") in TERMINAL and result.get("files") == pins and call is not None,
             "Reviewer completion does not bind retained files")
    review, answer = result.get("review"), None
    if result["status"].startswith("REVIEWED_"):
        if started.get("command_capture"):
            _require(command_capture is not None and _command_succeeded(command_capture),
                     "An unsuccessful reviewer command cannot publish an accepted review")
        current = support_request(organ, run_id)
        _require(current == request, "Reviewed encounter changed since reviewer invocation")
        checked = _envelope(envelope, request)
        _require(type(review) is dict and result["status"] == "REVIEWED_" + checked["answer_verdict"],
                 "Reviewer completion verdict differs")
        origami._keys(review, ("status", "run_id", "handle", "source_handle", "answer_handle",
                               "answer_verdict", "applicability", "attribution", "authority"), "review publication")
        store = RecordStore(organ.workspace)
        record = store.read(review["handle"])
        _require(record["payload"].get("review") == checked, "Published review differs from actual reviewer response")
        answer = reviewed_answer(organ, run_id, review["handle"])
        _require(answer["status"] == result["status"], "Reviewed answer verdict differs")
        answer_record = store.read(record["links"][0])
        _require(review == {"status": "REVIEW_RECORDED", "run_id": run_id, "handle": review["handle"],
                 "source_handle": answer_record["links"][0], "answer_handle": record["links"][0],
                 "answer_verdict": checked["answer_verdict"], "applicability": checked["applicability"],
                 "attribution": ATTRIBUTION, "authority": "NONE"}, "Retained review publication differs")
    else:
        _require(review is None, "Unaccepted reviewer return cannot carry a published review")
    return _result(run_id, directory, result["status"], call, review=review,
                   answer=answer, error_type=result.get("error_type"))


def read_review_run(organ, run_id):
    """Read/revalidate a retained attempt without launching or repairing anything.

    A missing completion receipt is interrupted even if a response was saved.
    Successful reviews reopen current source bytes and their immutable chain.
    The existing run lock prevents observing half of an active append sequence.
    """
    if not _directory(organ, run_id).exists():
        return None
    with organ._lock(organ._dir(run_id)):
        return _read_retained(organ, run_id)


def run_review(organ, run_id, reviewer, *, allowed_endpoints=(), provider_auth_path=None):
    """Call one trusted reviewer; subsequent calls only revalidate retained evidence.

    Reviewer accepts the exact support_request and returns a provider envelope
    whose provider is the declared checker. No wall deadline can preempt a Python
    callable. Commands use Origami's existing launcher and timeout contracts.
    """
    organ.status(run_id)
    with organ._lock(organ._dir(run_id)):
        retained = _read_retained(organ, run_id)
        if retained is not None:
            return retained
        endpoints = origami._endpoints(allowed_endpoints)
        auth_path = origami._auth_path(provider_auth_path)
        command = None
        if isinstance(reviewer, list):
            command = origami.ProviderCommand(reviewer)
        elif (hasattr(reviewer, "argv") and hasattr(reviewer, "timeout")) or not callable(reviewer):
            command = origami.ProviderCommand(getattr(reviewer, "argv", reviewer), getattr(reviewer, "timeout", 120))
        if command:
            from .isolation import require
            sandbox = require("A support-review command", endpoints=endpoints)
        else:
            sandbox = None
        constraints = (origami._constraints("command", sandbox, endpoints, auth_path)
                       if command else origami._constraints("callable"))
        # Stable dictionary order lets a later reader reconstruct the exact
        # command stdin bytes, in addition to the canonical request identity.
        request = json.loads(_data(support_request(organ, run_id)))
        request_pin = _pin(_data(request))
        directory = _directory(organ, run_id)
        started = {"schema": SCHEMA, "run_id": run_id, "at": origami.now(),
                   "request": request_pin, "review_request_sha256": request["request_sha256"],
                   "constraints": constraints, "authority": "NONE"}
        if command is not None:
            started.update({"command_capture": "command",
                            "command_argv_sha256": hashlib.sha256(canonical(list(command.argv))).hexdigest()})
        temporary = Path(tempfile.mkdtemp(prefix=".review-start-", dir=directory.parent))
        # The complete request and start fence become visible together, before
        # the reviewer can run. Unpublished temporary directories never authorize calls.
        _write(temporary / "request.json", request)
        started_pin = _write(temporary / "started.json", started)
        _sync(temporary)
        os.rename(temporary, directory)
        _sync(directory.parent)
        pins = {"request.json": request_pin, "started.json": started_pin}
        began = perf_counter()
        envelope, error_type, status, transport_status = None, None, None, "RETURNED"
        command_capture = None
        try:
            returned = (organ._launch_recorded(command, request, sandbox, Path(request["source_root"]), endpoints, auth_path,
                                               capture_dir=directory / "command")
                        if command else reviewer(deepcopy(request)))
        except Exception as exc:
            # An exception may include credentials: retain its type, never its text.
            error_type, status, transport_status = type(exc).__name__, "REVIEW_FAILED", "ERROR"
        seconds, returned_at = perf_counter() - began, origami.now()
        if command is not None:
            # Reopen bytes written before decoding even when the command failed.
            # A valid returned usage object may still be useful accounting; a
            # nonzero exit or timeout never becomes an accepted review.
            command_capture = _command_capture(organ, directory, request, started)
            pins.update({"command/" + name: pin for name, pin in command_capture["files"].items()})
            envelope, raw_error = _captured_response(command_capture)
            if raw_error is None:
                pins["response.json"] = _write(directory / "response.json", envelope)
            if not _command_succeeded(command_capture):
                error_type = error_type or command_capture["execution"].get("error_type") or "CommandFailed"
                status, transport_status = "REVIEW_FAILED", "ERROR"
            elif raw_error is not None:
                error_type, status, transport_status = raw_error, "REVIEW_INVALID", "RETURNED"
        elif status is None:
            try:
                raw = _data(returned)
                envelope = json.loads(raw)
            except (ValueError, TypeError, RecursionError, UnicodeError) as exc:
                error_type, status = type(exc).__name__, "REVIEW_INVALID"
            else:
                pins["response.json"] = _write(directory / "response.json", envelope)
        usage, usage_error = _usage(envelope)
        def reported(name):
            value = envelope.get(name) if type(envelope) is dict else None
            return value if type(value) is str and len(value.encode("utf-8")) <= 8192 else None
        call = {"schema": SCHEMA, "run_id": run_id, "request": request_pin,
                "response": pins.get("response.json"), "started_at": started["at"], "returned_at": returned_at,
                "seconds": seconds, "transport": "PROGRAM", "transport_status": transport_status,
                "constraints": constraints, "provider": reported("provider"),
                "model": reported("model"), "response_id": reported("response_id"),
                "usage": usage, "usage_error": usage_error, "request_bytes": request_pin["bytes"],
                "response_bytes": pins.get("response.json", {}).get("bytes"),
                "command_capture": command_capture,
                "raw_response_retained": command_capture is not None,
                "response_retained": "response.json" in pins, "error_type": error_type,
                "cost": "UNKNOWN", "accounting_scope": ACCOUNTING, "attribution": ATTRIBUTION}
        pins["call.json"] = _write(directory / "call.json", call)
        # Response and all available usage are durable before post-call source
        # checks. A changed source cannot erase an incurred reviewer invocation.
        review, answer = None, None
        if status is None:
            try:
                checked = _envelope(envelope, request)
            except (ValueError, TypeError, KeyError) as exc:
                error_type, status = type(exc).__name__, "REVIEW_INVALID"
            else:
                try:
                    _require(support_request(organ, run_id) == request, "Reviewer source request changed")
                    review = publish_support_review(organ, run_id, checked)
                    answer = reviewed_answer(organ, run_id, review["handle"])
                    status = answer["status"]
                except (ValueError, OSError, TypeError, KeyError) as exc:
                    review, answer = None, None
                    error_type, status = type(exc).__name__, "REVIEW_UNAVAILABLE"
        _write(directory / "result.json", {"schema": SCHEMA, "run_id": run_id, "status": status,
               "files": pins, "review": review, "error_type": error_type, "authority": "NONE"})
        return _result(run_id, directory, status, call, review=review, answer=answer,
                       error_type=error_type, reused=False, reviewer_invoked=True)
