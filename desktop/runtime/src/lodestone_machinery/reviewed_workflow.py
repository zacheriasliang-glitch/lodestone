"""Complete an existing Origami encounter with an explicit separate reviewer.

This composes existing APIs. It neither grants authority nor retries an uncertain
external call. Accounting describes this invocation, not upstream human work.
"""
from contextlib import contextmanager
import hashlib
import os
import time
import uuid

from .origami import ProviderCommand, _auth_path, _endpoints, _read_state, _roles, ROLES
from .records import canonical
from .support_review import read_review_file
from .support_review_provider import read_review_run, run_review
from .usage import usage_totals


SCHEMA = "origami-completion/v1"
UNAVAILABLE_REASON = "Completion evidence or execution unavailable; no retry."


def _write(path, value):
    with path.open("xb") as stream:
        stream.write(canonical(value))
        stream.flush()
        os.fsync(stream.fileno())


def _read(path):
    value = read_review_file(path)
    if path.read_bytes() != canonical(value):
        raise ValueError("Completion sidecar bytes changed")
    return value


def _command(value):
    if isinstance(value, list):
        return ProviderCommand(value)
    if hasattr(value, "argv") and hasattr(value, "timeout"):
        return ProviderCommand(value.argv, value.timeout)
    if not callable(value):
        raise ValueError("Supply an explicit trusted provider or reviewer")
    return value


def _cpu():
    try:
        import resource
        own = resource.getrusage(resource.RUSAGE_SELF)
        children = resource.getrusage(resource.RUSAGE_CHILDREN)
        return {"process_seconds": own.ru_utime + own.ru_stime,
                "reaped_children_seconds": children.ru_utime + children.ru_stime}
    except ImportError:
        return {"process_seconds": time.process_time(), "reaped_children_seconds": None}


@contextmanager
def _locked(directory):
    import fcntl
    lock = directory / "lock"
    if lock.is_symlink():
        raise ValueError("Completion lock must not be a symlink")
    with lock.open("a+b") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Completion is busy") from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _brief_result(directory, answer):
    data = answer["markdown"].encode("utf-8")
    path = directory / "reviewed-answer.md"
    return ({**answer, "artifact": str(path), "artifact_bytes": len(data),
             "artifact_sha256": hashlib.sha256(data).hexdigest()}, path, data)


def _brief(directory, answer):
    result, path, data = _brief_result(directory, answer)
    if path.is_symlink():
        raise ValueError("Reviewed brief must not be a symlink")
    try:
        with path.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        if path.read_bytes() != data:
            raise ValueError("Saved reviewed brief differs from current reviewed evidence")
    return result


def _unreviewed(state, run_id):
    return {"status": state["status"], "run_id": run_id,
            "reason": state.get("reason", "Encounter did not produce an answer to review"),
            "authority": "NONE"}


def _review_failure(review_run, run_id):
    return {"status": review_run["status"], "run_id": run_id,
            "review_handle": review_run.get("review_handle"), "authority": "NONE"}


def _read_result(organ, run_id, directory, saved, review_run):
    if (type(saved) is not dict or set(saved) != {"schema", "run_id", "result", "invocation", "accounting"}
            or saved["schema"] != SCHEMA or saved["run_id"] != run_id
            or type(saved["accounting"]) is not dict or type(saved["invocation"]) is not str):
        raise ValueError("Invalid saved completion")
    result = saved["result"]
    if (type(result) is not dict or result.get("run_id") != run_id
            or result.get("authority") != "NONE" or type(result.get("status")) is not str):
        raise ValueError("Invalid saved completion result")
    if result["status"].startswith("REVIEW"):
        if review_run is None or review_run["status"] != result["status"]:
            raise ValueError("Saved completion differs from retained review")
        if review_run.get("answer") is not None:
            expected, path, data = _brief_result(directory, review_run["answer"])
            if result != expected or path.is_symlink() or path.read_bytes() != data:
                raise ValueError("Saved reviewed brief differs from current reviewed evidence")
        elif result != _review_failure(review_run, run_id):
            raise ValueError("Saved reviewer failure differs from retained evidence")
    elif result["status"] == "WORKFLOW_UNAVAILABLE":
        if (set(result) != {"status", "run_id", "error_type", "reason", "authority"}
                or type(result["error_type"]) is not str or not result["error_type"]
                or result["reason"] != UNAVAILABLE_REASON):
            raise ValueError("Invalid saved workflow refusal")
        # This records a local validation/persistence failure, not an answer
        # verdict. Any existing review attempt was independently reopened above.
    else:
        current = organ.status(run_id)
        if current["status"] == "NEEDS_PROVIDER" and result["status"] in ("NEEDS_REVIEW", "UNAVAILABLE"):
            current = organ.inspect(run_id)
        if current["status"] == "ANSWER" or result != _unreviewed(current, run_id):
            raise ValueError("Saved completion differs from actual encounter status")
    return result


def complete(organ, run_id, provider, reviewer, *, allowed_endpoints=(),
             provider_auth_path=None, reviewer_allowed_endpoints=(), reviewer_auth_path=None):
    """Answer then review once; completed readback never invokes either provider.

    Both commands are caller-selected trusted code. Their endpoint and auth
    settings are separate. Review/model identity remains declared attribution.
    """
    began, cpu_before = time.perf_counter(), _cpu()
    encounter = organ._dir(run_id)
    state = organ.status(run_id)
    intent = _read_state(encounter / "intent.json")
    _roles({key: intent["roles"][key] for key in ROLES})
    directory = encounter / "completion"
    if directory.is_symlink():
        raise ValueError("Completion directory must not be a symlink")
    directory.mkdir(exist_ok=True)
    with _locked(directory):
        invocation = uuid.uuid4().hex
        receipt = directory / ("invocation-" + invocation + ".json")
        result_path, started_path = directory / "result.json", directory / "started.json"
        calls_before = len(organ._calls(encounter))
        result, review_run, snapshot_bytes = None, None, None
        reused, answer_invoked = False, False
        owns_attempt = False
        configuring = False
        try:
            if result_path.exists() or result_path.is_symlink():
                saved = _read(result_path)
                reused = True
                review_run = read_review_run(organ, run_id)
                result = _read_result(organ, run_id, directory, saved, review_run)
            elif started_path.exists() or started_path.is_symlink():
                started = _read(started_path)
                if (type(started) is not dict or started.get("schema") != SCHEMA
                        or started.get("run_id") != run_id):
                    raise ValueError("Invalid interrupted completion")
                review_run = read_review_run(organ, run_id)
                result = {"status": "WORKFLOW_INTERRUPTED", "run_id": run_id,
                          "reason": "Previous completion did not finish; no automatic replay.", "authority": "NONE"}
                reused = True
            else:
                review_run = read_review_run(organ, run_id)
                # Configuration only matters when a program will actually run.
                # Completed evidence stays readable after old auth paths vanish.
                if review_run is None and state["status"] in ("NEEDS_PROVIDER", "ANSWER"):
                    configuring = True
                    reviewer = _command(reviewer)
                    _endpoints(reviewer_allowed_endpoints); _auth_path(reviewer_auth_path)
                    if state["status"] == "NEEDS_PROVIDER":
                        provider = _command(provider)
                        _endpoints(allowed_endpoints); _auth_path(provider_auth_path)
                        if provider is reviewer or (hasattr(provider, "argv") and hasattr(reviewer, "argv")
                                                    and provider.argv == reviewer.argv):
                            raise ValueError("Answer and review require separately configured providers")
                    configuring = False
                _write(started_path, {"schema": SCHEMA, "run_id": run_id,
                                     "invocation": invocation, "initial_status": state["status"]})
                owns_attempt = True
                if review_run is None:
                    if state["status"] == "NEEDS_PROVIDER":
                        checked = organ.inspect(run_id)
                        snapshot_bytes = checked.get("inspection", {}).get("source_bytes_read_and_hashed")
                        if checked["status"] == "NEEDS_PROVIDER":
                            answer_invoked = True
                            state = organ.run(run_id, provider, allowed_endpoints=allowed_endpoints,
                                              provider_auth_path=provider_auth_path)
                        else:
                            state = checked
                    if state["status"] == "ANSWER":
                        review_run = run_review(organ, run_id, reviewer,
                                                allowed_endpoints=reviewer_allowed_endpoints,
                                                provider_auth_path=reviewer_auth_path)
                if review_run is not None:
                    result = (_brief(directory, review_run["answer"]) if review_run.get("answer") is not None
                              else _review_failure(review_run, run_id))
                else:
                    result = _unreviewed(state, run_id)
        except (ValueError, OSError, RuntimeError, KeyError, TypeError) as exc:
            if configuring:
                raise
            # Never persist arbitrary provider exception text; it may contain credentials.
            result = {"status": "WORKFLOW_UNAVAILABLE", "run_id": run_id,
                      "error_type": type(exc).__name__, "reason": UNAVAILABLE_REASON,
                      "authority": "NONE"}
        calls = organ._calls(encounter)
        new_calls = calls[calls_before:]
        review_call = (review_run or {}).get("call")
        reviewer_invoked = bool((review_run or {}).get("reviewer_invoked"))
        cpu_after = _cpu()
        accounting = {
            "wall_seconds_prefix": time.perf_counter() - began,
            "cpu_seconds_prefix": {key: None if cpu_before[key] is None or cpu_after[key] is None
                                    else max(0, cpu_after[key] - cpu_before[key]) for key in cpu_before},
            "measurement_scope": "This complete() invocation through final measurement, including validation and provider waits; excludes final result/receipt writes, CLI startup/output, prior preparation and external human work.",
            "cpu_scope": "Current process and separately reported reaped children; concurrent same-process work may be included; not physical provider compute. Do not add nested provider timings to wall/CPU prefixes.",
            "answer_calls_added": len(new_calls), "answer_run_requested": answer_invoked,
            "answer_calls_scope": "New retained call records during this invocation; one caller must own this encounter. Dispatch and returned usage do not prove a remote model actually ran.",
            "answer_usage_this_invocation": usage_totals(new_calls),
            "answer_usage_retained": usage_totals(calls),
            "reviewer_invoked": reviewer_invoked,
            "review_call": review_call,
            "review_call_scope": "Retained review call; only charged to this invocation when reviewer_invoked is true.",
            "preflight_source_bytes_read_and_hashed": snapshot_bytes,
            "source_validation_total_bytes": None,
            "source_validation_scope": "Preflight inspection reports actual source bytes; repeated internal review/lesson validation reads and record reads are not completely metered.",
            "provider_cost": "UNKNOWN", "human_and_upstream_cost": "UNKNOWN",
        }
        output = {**result, "workflow": {"schema": SCHEMA, "invocation": invocation,
                                         "reused": reused, "sidecar_path": str(result_path),
                                         "invocation_receipt": str(receipt), "accounting": accounting}}
        if owns_attempt:
            _write(result_path, {"schema": SCHEMA, "run_id": run_id, "result": result,
                                 "invocation": invocation, "accounting": accounting})
        _write(receipt, {"schema": SCHEMA, "run_id": run_id, "output": output})
        return output
