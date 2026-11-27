"""Durable single-dispatch broker for explicitly configured learning practice.

No model, network endpoint, billable adapter or retry is selected here. The
caller supplies one synchronous provider and owns its authorization. Nurarihyon
delivers checked Bezoar context, records the actual result, and reconciles an
abandoned dispatch without replaying it. Unrelated daemon tasks are unchanged.

A carved run (``execute_carved``) holds the broker only to serve (prepare,
admit, dispatch) and to clear (response, outcome, terminal). Its provider call
runs under a per-run lease that the OS releases on process exit, so the
observer can tell a live dispatch from an abandoned one without the broker
lock, and corrections, status and handoffs are not held still while it runs.
``weaving`` keeps encounters on one memory one at a time, so practice stays
cumulative and one reader runs on the machine.
"""
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time
import threading
import uuid

from lodestone_machinery.records import canonical
from .common import BusyError, file_lock, read_bytes, strict_json
from .portable_files import ensure_directory, is_alias
from .learning_conscience import assess, MAX_PACKET_BYTES
from . import learning_recurrence

SCHEMA = "nurarihyon-learning-run/v1"
MAX_JOURNAL_RECORDS = 4096
_PROCESS_TOKEN = uuid.uuid4().hex
_PROCESS_OBSERVED_AT = datetime.now(timezone.utc).isoformat()
_LOCKS = threading.local()


def _memory(memory):
    if hasattr(memory, "validate_prepared") and hasattr(memory, "store"):
        return memory
    from lodestone_machinery.bezoar_learning import LearningMemory
    return LearningMemory(memory)


def _copy(value):
    return json.loads(canonical(value))


def _now():
    return datetime.now(timezone.utc).isoformat()


def _check_stop(memory):
    root = Path(memory.workspace)
    if any((parent / "STOP").exists() or (parent / "STOP").is_symlink()
           for parent in (root, root.parent)):
        raise ValueError("Learning practice is stopped; retained outcomes remain readable and finishable")


@contextmanager
def locked(memory, *, timeout=0):
    """One active broker per memory; OS releases custody on process exit."""
    memory = _memory(memory)
    path = Path(memory.workspace).resolve()
    held = getattr(_LOCKS, "held", None)
    if held is None:
        held = _LOCKS.held = set()
    if path in held:
        yield memory
        return
    with file_lock(path / "learning-broker.lock", timeout=timeout):
        held.add(path)
        try:
            yield memory
        finally:
            held.remove(path)


@contextmanager
def weaving(memory, *, timeout=0):
    """One weaving encounter per memory, held across its whole carved lifecycle.

    A later encounter waits here, then prepares from the earlier outcome. The
    observer, corrections and status reads never take this lock.
    """
    with file_lock(Path(_memory(memory).workspace).resolve() / "learning-weaving.lock", timeout=timeout):
        yield


def _lease_path(memory, run_handle):
    directory = Path(memory.workspace).resolve() / "learning-leases"
    if (directory.exists() or directory.is_symlink()) and is_alias(directory.lstat()):
        raise ValueError("Learning lease directory is an alias")
    return directory / (hashlib.sha256(run_handle.encode()).hexdigest() + ".lock")


def _held(path):
    if not path.exists():
        return False
    try:
        with file_lock(path):
            return False
    except BusyError:
        return True


def _lease_held(memory, run_handle):
    """True only while a live process holds this run's dispatch lease."""
    return _held(_lease_path(memory, run_handle))


def _require_one_reader(memory, own=None):
    """Keep one provider at a time on the machine across every dispatch path.

    Before carving, the broker lock held through each provider call gave this
    for free. A carved run frees the broker while its lease is live, so any
    other dispatch (CLI, Origami, another carved run) must find no live lease.
    """
    directory = _lease_path(memory, own or "-").parent
    if not directory.is_dir():
        return
    mine = _lease_path(memory, own).name if own else None
    for path in sorted(directory.glob("*.lock"))[:4096]:
        if path.name != mine and _held(path):
            raise BusyError("Another encounter's provider is running; one reader at a time on this machine")


def _publish(memory, kind, payload, links):
    handle = memory.store.publish("nurarihyon", kind, payload, links=links)
    memory.store.verify_links([handle])
    # RecordStore.publish already durably publishes through the native backend.
    return handle


def _journal(memory):
    directory = Path(memory.workspace) / "organs/nurarihyon/records"
    if directory.is_symlink():
        raise ValueError("Learning journal directory is an alias")
    paths = sorted(directory.glob("*.json")) if directory.exists() else []
    if len(paths) > MAX_JOURNAL_RECORDS:
        raise ValueError("Learning journal capacity exhausted; preserve history before extending it")
    entries = []
    for path in paths:
        handle = "nurarihyon:" + path.stem
        record = memory.store.read(handle)
        if record["kind"].startswith("learning-"):
            memory.store.verify_links([handle])
            entries.append((handle, record))
    return entries


def _assessment(memory, prepared, packet, execution_binding=None):
    result = assess(memory, prepared, packet)
    if result["verdict"] == "ALLOW" and execution_binding is not None:
        recurrence = learning_recurrence.assess(memory, _journal(memory), execution_binding)
        result["recurrence"] = recurrence
        if recurrence["verdict"] == "REFUSE":
            result["verdict"] = "REFUSE"
            result["reason"] = recurrence["reason"]
    if result["verdict"] != "ALLOW":
        _record_rejection(memory, prepared, stage="CONSCIENCE_ADMISSION", status="REFUSED",
                          reason=result["reason"], assessment=result)
        raise ValueError("Conscience learning contract refused: " + result["reason"])
    handle = _publish(memory, "learning-admission", result, [prepared["run_handle"]])
    return {**result, "assessment_handle": handle}


def assess_prepared(memory, prepared, packet):
    """Enforce and retain an input obligation for a caller-managed lifecycle."""
    with locked(memory) as memory:
        _check_stop(memory)
        return _assessment(memory, prepared, packet)


def _record_rejection(memory, prepared, *, stage, status, reason, assessment=None):
    """Preserve rejected input assembly without treating it as a model judgment."""
    try:
        original = memory.read_prepared(prepared["run_handle"])
    except (KeyError, TypeError, ValueError, OSError):
        return None
    receipt = _publish(memory, "learning-refusal", {
        "schema": "nurarihyon-learning-refusal/v1", "run_handle": original["run_handle"],
        "stage": stage, "reason": reason, "assessment": assessment,
        "provider_called": False, "semantic_refusal": False, "authority": "NONE"},
        [original["run_handle"]])
    # A corrupted candidate cannot change the outcome of the intact original.
    if canonical(original) != canonical(prepared) or memory.outcome_for(original) is not None:
        return receipt
    observation = {
        "stage": stage, "reason": reason, "receipt_handle": receipt,
        "provider_called": False, "semantic_refusal": False}
    if assessment and assessment.get("recurrence", {}).get("verdict") == "REFUSE":
        observation["recurrence_source_outcome"] = assessment["recurrence"]["source_outcome"]
    outcome = memory.finish(original, response=None, status=status, observation=observation)
    _terminal(memory, original, outcome, receipt=receipt, response_source="input_admission_failure")
    return receipt


def _terminal(memory, prepared, outcome, *, dispatch=None, receipt=None, response_source):
    links = [prepared["run_handle"], outcome]
    if dispatch is not None:
        links.append(dispatch)
    if receipt is not None:
        links.append(receipt)
    memory.read_outcome(outcome)
    return _publish(memory, "learning-terminal", {
        "schema": "nurarihyon-learning-terminal/v1", "run_handle": prepared["run_handle"],
        "outcome_handle": outcome, "dispatch_handle": dispatch, "response_receipt": receipt,
        "response_source": response_source, "authority": "NONE"}, links)


def _report(memory, prepared, outcome, terminal, *, provider_called=False):
    checked = memory.read_outcome(outcome)
    payload = checked.get("payload", checked)
    return {"schema": SCHEMA, "status": payload["status"], "run_id": prepared["run_id"],
            "run_handle": prepared["run_handle"], "outcome_handle": outcome,
            "terminal_handle": terminal, "outcome": payload,
            "provider_called": provider_called, "automatic_retry": False,
            "authority": "NONE"}


def _core_observation(receipt, observation, wall):
    retained = observation if len(canonical(observation)) <= 30000 else {
        "reason": "Full provider observation is retained in the linked response receipt",
        "observation_bytes": len(canonical(observation))}
    return {"provider_receipt": receipt, "provider_observation": retained, "wall_seconds": wall}


def _retain_mechanical_failure(memory, dispatch, receipt, failure):
    if failure is None:
        return None
    source = memory.store.relay("nurarihyon", dispatch, owner="nurarihyon", kind="learning-dispatch")
    failure = learning_recurrence.checked_failure(failure, source["payload"].get("execution_binding"))
    return _publish(memory, "learning-mechanical-failure", {
        "schema": "loom-mechanical-failure/v1", "dispatch_handle": dispatch,
        "response_handle": receipt, "failure": failure,
        "authority": "NONE", "semantic_learning": "NOT_ESTABLISHED"}, [dispatch, receipt])


def _external_response(payload):
    """Read only the registered Origami run; never run recovery/provider code."""
    source = payload["source"]
    directory = Path(source["workspace"]) / "origami" / source["run_id"]
    if directory.resolve(strict=True) != directory:
        raise ValueError("External learning run path changed or became an alias")
    intent_raw = read_bytes(directory / "intent.json", MAX_PACKET_BYTES)
    if hashlib.sha256(intent_raw).hexdigest() != source["intent_sha256"]:
        raise ValueError("External learning intent differs from its registered source")
    intent = strict_json(intent_raw, MAX_PACKET_BYTES)
    if (intent.get("id") != source["run_id"]
            or intent.get("learning_context", {}).get("run_handle") != payload["run_handle"]):
        raise ValueError("External learning source no longer binds this preparation")
    responses = sorted(directory.glob("response-*.json"))
    if len(responses) > payload["provider_call_limit"]:
        raise ValueError("External learning response count exceeds its registered call bound")
    if not responses:
        return None, {"provider_response": "UNKNOWN", "source": source}
    path = responses[-1]
    if not re.fullmatch(r"response-[0-9]{3}\.json", path.name):
        raise ValueError("External response filename is invalid")
    envelope = strict_json(read_bytes(path, MAX_PACKET_BYTES), MAX_PACKET_BYTES)
    raw = canonical(envelope["response"])
    step = int(path.stem.rsplit("-", 1)[1])
    request = strict_json(read_bytes(directory / ("request-%03d.json" % step), MAX_PACKET_BYTES), MAX_PACKET_BYTES)
    if (envelope["response"].get("request_id") != request["request_id"]
            or request.get("learning_context", {}).get("run_handle") != payload["run_handle"]):
        raise ValueError("External response does not bind its retained request")
    observation = {"provider_response": "RETAINED_PARTIAL_RUN_RESPONSE", "source": source,
                   "response_file": path.name, "response_sha256": hashlib.sha256(raw).hexdigest(),
                   "response_bytes": len(raw), "semantic_completion": "NOT_INFERRED"}
    return raw.decode() if len(raw) <= 65536 else None, observation


def begin_external(memory, prepared, packet, *, call_limit, source_workspace, source_run_id):
    """Register an Origami-owned provider lifecycle under the shared memory lock.

    Origami remains responsible for its own per-step call bound and raw captures.
    This record makes abandonment discoverable to the read-only observer.
    """
    with locked(memory) as memory:
        _check_stop(memory)
        if type(call_limit) is not int or not 1 <= call_limit <= 16:
            raise ValueError("External learning call limit must be 1..16")
        if type(source_run_id) is not str or re.fullmatch(r"[0-9a-f]{32}", source_run_id) is None:
            raise ValueError("External learning source requires an exact Origami run id")
        workspace = Path(source_workspace)
        if not workspace.is_absolute() or workspace.resolve(strict=True) != workspace:
            raise ValueError("External learning workspace must be an existing resolved absolute path")
        directory = workspace / "origami" / source_run_id
        if directory.resolve(strict=True) != directory:
            raise ValueError("External learning source path is aliased")
        intent_raw = read_bytes(directory / "intent.json", MAX_PACKET_BYTES)
        intent = strict_json(intent_raw, MAX_PACKET_BYTES)
        if (intent.get("id") != source_run_id or intent.get("learning_context") != prepared
                or packet.get("run_id") != source_run_id
                or intent.get("limits", {}).get("max_calls") != call_limit):
            raise ValueError("External learning source differs from prepared encounter or call limit")
        if memory.outcome_for(prepared) is not None:
            raise ValueError("External learning run already has a terminal outcome")
        _require_one_reader(memory)
        assessment = _assessment(memory, prepared, packet)
        source = {"workspace": str(workspace), "run_id": source_run_id,
                  "intent_sha256": hashlib.sha256(intent_raw).hexdigest()}
        existing = [(h, r) for h, r in _journal(memory)
                    if r["kind"] in ("learning-dispatch", "learning-external-dispatch")
                    and r["payload"]["run_handle"] == prepared["run_handle"]]
        if existing:
            if (len(existing) != 1 or existing[0][1]["kind"] != "learning-external-dispatch"
                    or existing[0][1]["payload"]["source"] != source
                    or existing[0][1]["payload"]["input_sha256"] != assessment["input_sha256"]):
                raise ValueError("Learning run already belongs to a different dispatch")
            return existing[0][0]
        return _publish(memory, "learning-external-dispatch", {
            "schema": "nurarihyon-learning-external-dispatch/v1", "run_handle": prepared["run_handle"],
            "input": _copy(packet), "input_sha256": assessment["input_sha256"],
            "assessment_handle": assessment["assessment_handle"], "source": source,
            "owner_pid": os.getpid(), "owner_process_token": _PROCESS_TOKEN,
            "owner_process_observed_at": _PROCESS_OBSERVED_AT, "dispatched_at": _now(),
            "provider_call_limit": call_limit, "call_bound_owner": "Origami",
            "authority": "NONE"}, [prepared["run_handle"], assessment["assessment_handle"]])


def complete_external(memory, prepared, outcome, dispatch):
    """Close an Origami-owned lifecycle only around its actual retained outcome."""
    with locked(memory) as memory:
        record = memory.store.relay("nurarihyon", dispatch, owner="nurarihyon", kind="learning-external-dispatch")
        if record["payload"].get("run_handle") != prepared["run_handle"]:
            raise ValueError("External dispatch belongs to another learning run")
        if memory.outcome_for(prepared) != outcome:
            raise ValueError("External completion does not bind the retained outcome")
        return _terminal(memory, prepared, outcome, dispatch=dispatch, response_source="origami_retained_outcome")


def _reconcile(memory):
    entries = _journal(memory)
    terminals = {r["payload"].get("dispatch_handle"): h for h, r in entries
                 if r["kind"] == "learning-terminal" and r["payload"].get("dispatch_handle")}
    responses = {}
    for handle, record in entries:
        if record["kind"] == "learning-response":
            dispatch = record["payload"]["dispatch_handle"]
            if dispatch in responses:
                raise ValueError("Conflicting provider returns for one dispatch")
            responses[dispatch] = (handle, record["payload"])
    reconciled = []
    for dispatch, record in entries:
        if record["kind"] not in ("learning-dispatch", "learning-external-dispatch") or dispatch in terminals:
            continue
        if record["kind"] == "learning-dispatch" and _lease_held(memory, record["payload"]["run_handle"]):
            continue  # A carved run is still with its provider; its lease ends with its process.
        prepared = memory.read_prepared(record["payload"]["run_handle"])
        outcome = memory.outcome_for(prepared)
        retained = responses.get(dispatch)
        if outcome is None:
            if retained is not None:
                receipt, payload = retained
                _retain_mechanical_failure(memory, dispatch, receipt, payload.get("mechanical_failure"))
                outcome = memory.finish(prepared, response=payload["response"], status=payload["status"],
                                        observation=_core_observation(receipt, payload["observation"], payload["wall_seconds"]))
            else:
                response, known = None, {"provider_response": "UNKNOWN"}
                if record["kind"] == "learning-external-dispatch":
                    try:
                        response, known = _external_response(record["payload"])
                    except (OSError, ValueError, KeyError, TypeError) as exc:
                        known = {"provider_response": "UNVERIFIED", "source_error_type": type(exc).__name__,
                                 "source": record["payload"]["source"]}
                outcome = memory.finish(prepared, response=response, status="INTERRUPTED", observation={
                    "reason": "Dispatch was retained but no terminal outcome was retained before the broker lock was released",
                    "dispatch_handle": dispatch, **known,
                    "replayed": False, "learning_claim": "NONE"})
        terminal = _terminal(memory, prepared, outcome, dispatch=dispatch,
                             receipt=retained[0] if retained else None,
                             response_source="recovered_retained_outcome")
        reconciled.append(_report(memory, prepared, outcome, terminal))
    lessons = memory.reconcile_lessons()
    return {"schema": "nurarihyon-learning-reconciliation/v1", "status": "RECONCILED",
            "runs": reconciled, "provider_calls": 0, "automatic_retry": False,
            "lessons": lessons,
            "pending": [p["run_handle"] for p in memory.pending()], "authority": "NONE"}


def reconcile(memory):
    """Observer-safe once operation: active work yields BUSY, never interruption."""
    memory = _memory(memory)
    if Path(memory.workspace).resolve() in getattr(_LOCKS, "held", set()):
        return {"schema": "nurarihyon-learning-reconciliation/v1", "status": "BUSY",
                "runs": [], "provider_calls": 0, "automatic_retry": False,
                "authority": "NONE"}
    try:
        with locked(memory) as memory:
            return _reconcile(memory)
    except BusyError:
        return {"schema": "nurarihyon-learning-reconciliation/v1", "status": "BUSY",
                "runs": [], "provider_calls": 0, "automatic_retry": False,
                "authority": "NONE"}


def status(memory):
    with locked(memory) as memory:
        entries = _journal(memory)
        return {"schema": "nurarihyon-learning-status/v1",
                "workspace": str(memory.workspace), "journal_records": len(entries),
                "pending": memory.pending(),
                "terminals": [{"handle": h, **r["payload"]} for h, r in entries
                              if r["kind"] == "learning-terminal"],
                "provider_calls": 0, "authority": "NONE"}


def prepare(memory, seeds, *, encounter, purpose, reader, run_id=None, applicability=None):
    with locked(memory) as memory:
        _check_stop(memory)
        _reconcile(memory)
        return memory.prepare(seeds, encounter=encounter, purpose=purpose, reader=reader,
                              run_id=run_id, applicability=applicability)


def finish(memory, prepared, *, response, status, observation, review=None):
    """Retain an explicitly supplied return; do not invent provider dispatch."""
    with locked(memory) as memory:
        memory.validate_prepared(prepared)
        outcome = memory.finish(prepared, response=response, status=status,
                                observation=observation, review=review)
        terminal = _terminal(memory, prepared, outcome, response_source="caller_supplied_return")
        return _report(memory, prepared, outcome, terminal)


def _default_packet(prepared, request):
    return {"schema": "nurarihyon-learning-input/v1", "request": request,
            "learning_context": prepared, "exposure": "EXPOSED_PRACTICE"}


def _existing(memory, prepared, packet):
    outcome = memory.outcome_for(prepared)
    if outcome is None:
        return None
    dispatches = [record["payload"] for _, record in _journal(memory)
                  if record["kind"] in ("learning-dispatch", "learning-external-dispatch")
                  and record["payload"]["run_handle"] == prepared["run_handle"]]
    if len(dispatches) > 1:
        raise ValueError("One learning run has conflicting dispatch records")
    if dispatches and canonical(dispatches[0]["input"]) != canonical(packet):
        raise ValueError("Finished learning run binds a different provider input")
    terminal = _terminal(memory, prepared, outcome, response_source="existing_retained_outcome")
    return _report(memory, prepared, outcome, terminal)


def _serve(memory, prepared, request, provider, packet_builder, own_lease=None,
           execution_binding=None, failure_classifier=None):
    """Validate, build, admit and record the dispatch (under the broker)."""
    execution_binding = learning_recurrence.binding(execution_binding)
    if failure_classifier is not None and (not callable(failure_classifier) or execution_binding is None):
        raise ValueError("A trusted failure classifier requires an execution binding")
    try:
        memory.validate_prepared(prepared)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        _record_rejection(memory, prepared, stage="PREPARATION_VALIDATION", status="REFUSED",
                          reason="Saved preparation did not validate: " + type(exc).__name__)
        raise
    builder = packet_builder or _default_packet
    # Builders receive copies. Revalidation catches source replacement and an
    # exact preparation must survive visibly inside the actual provider input.
    try:
        packet = _copy(builder(_copy(prepared), _copy(request)))
    except Exception as exc:
        _record_rejection(memory, prepared, stage="INPUT_ASSEMBLY", status="FAILED",
                          reason="Input builder failed: " + type(exc).__name__)
        raise ValueError("Learning input assembly failed: " + type(exc).__name__) from None
    existing = _existing(memory, prepared, packet)
    if existing is not None:
        return {"existing": existing}
    if not callable(provider):
        raise ValueError("One explicitly supplied synchronous provider is required")
    _require_one_reader(memory, own_lease)
    assessment = _assessment(memory, prepared, packet, execution_binding)
    raw = canonical(packet)
    dispatch = _publish(memory, "learning-dispatch", {
        "schema": "nurarihyon-learning-dispatch/v1", "run_handle": prepared["run_handle"],
        "input": packet, "input_sha256": assessment["input_sha256"],
        "assessment_handle": assessment["assessment_handle"],
        "execution_binding": execution_binding,
        "owner_pid": os.getpid(), "owner_process_token": _PROCESS_TOKEN,
        "owner_process_observed_at": _PROCESS_OBSERVED_AT, "dispatched_at": _now(),
        "authority": "NONE", "provider_call_limit": 1,
        "provider_scope": "Caller-supplied synchronous callback; authorization and billing remain with caller"},
        [prepared["run_handle"], assessment["assessment_handle"]])
    # No caller code executes between this final validation and dispatch.
    memory.validate_prepared(prepared)
    return {"packet": packet, "raw": raw, "dispatch": dispatch, "execution_binding": execution_binding}


def _eat(provider, packet, raw, execution_binding=None, failure_classifier=None):
    """Call the provider once and bound its return; touches no memory or lock."""
    started = time.monotonic()
    response, returned, exception = None, None, None
    observation = {}
    result_status = "FAILED"
    try:
        returned = provider(packet)
        returned_raw = canonical(returned)
        if len(returned_raw) > MAX_PACKET_BYTES:
            raise ValueError("Provider return exceeds the declared 2 MiB retention bound")
        if type(returned) is str:
            response = returned
            result_status = "COMPLETED" if returned.strip() else "ABSTAINED"
        elif type(returned) is dict and set(returned) <= {"status", "response", "observation"}:
            result_status = returned.get("status", "COMPLETED")
            response = returned.get("response")
            observation = returned.get("observation", {})
            if (result_status not in ("COMPLETED", "FAILED", "ABSTAINED", "REFUSED", "INTERRUPTED")
                    or response is not None and type(response) is not str
                    or type(observation) not in (str, dict)):
                raise ValueError("Provider return status/response/observation is invalid")
        else:
            response = returned_raw.decode()
            raise ValueError("Provider must return text or status/response/observation object")
        if canonical(packet) != raw:
            result_status = "FAILED"
            observation = {"reason": "Provider mutated the checked input", "provider_observation": observation}
    except BaseException as exc:
        exception = exc
        result_status = "INTERRUPTED" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else "FAILED"
        observation = {"error_type": type(exc).__name__,
                       "error": "Provider callback or return validation failed; exception text omitted to avoid credential disclosure",
                       "response_returned": returned is not None}
    wall = time.monotonic() - started
    # Core learning records keep a smaller reader-facing response while the
    # broker receipt preserves the actual bounded provider return unchanged.
    if response is not None and type(response) is not str:
        response = None
    if type(response) is str and len(response.encode()) > 65536:
        result_status = "FAILED"
        observation = {"reason": "Provider response exceeds the core 65536-byte response bound",
                       "response_bytes": len(response.encode()), "provider_observation": observation}
        response = None
    if result_status == "COMPLETED" and (response is None or not response.strip()):
        result_status = "ABSTAINED"
    # Always preserve the actual bounded return, including invalid/abstaining
    # outputs. An oversized return is identified explicitly, never passed off as
    # a complete retained response.
    try:
        encoded_return = canonical(returned)
        captured_return = returned if len(encoded_return) <= MAX_PACKET_BYTES else {
            "retention": "OVERSIZE_NOT_RETAINED", "bytes": len(encoded_return),
            "sha256": hashlib.sha256(encoded_return).hexdigest()}
    except (TypeError, ValueError):
        captured_return = {"retention": "NON_JSON_RETURN", "python_type": type(returned).__name__}
    measured_failure, classification_error = None, None
    if failure_classifier is not None and result_status in ("FAILED", "INTERRUPTED"):
        try:
            measured_failure = learning_recurrence.checked_failure(
                failure_classifier(returned), execution_binding)
        except Exception as exc:
            # Keep the actual provider result even if supplemental diagnosis
            # fails. An unverified cause cannot establish a recurrence rule.
            classification_error = type(exc).__name__
    return {"status": result_status, "response": response, "observation": observation,
            "returned": captured_return, "wall": wall, "exception": exception,
            "mechanical_failure": measured_failure, "classification_error": classification_error}


def _clear(memory, prepared, served, eaten):
    """Retain the actual return, outcome and terminal of a served dispatch (under the broker)."""
    dispatch = served["dispatch"]
    result_status, response, observation = eaten["status"], eaten["response"], eaten["observation"]
    captured_return, wall, exception = eaten["returned"], eaten["wall"], eaten["exception"]
    measured_failure, classification_error = eaten["mechanical_failure"], eaten["classification_error"]
    receipt = _publish(memory, "learning-response", {
        "schema": "nurarihyon-learning-response/v1", "run_handle": prepared["run_handle"],
        "dispatch_handle": dispatch, "status": result_status,
        "returned": captured_return, "response": response, "observation": observation,
        "mechanical_failure": measured_failure, "classification_error": classification_error,
        "wall_seconds": wall, "authority": "NONE"}, [prepared["run_handle"], dispatch])
    if measured_failure is not None:
        _retain_mechanical_failure(memory, dispatch, receipt, measured_failure)
    outcome = memory.finish(prepared, response=response, status=result_status,
                            observation=_core_observation(receipt, observation, wall))
    terminal = _terminal(memory, prepared, outcome, dispatch=dispatch, receipt=receipt,
                         response_source="broker_provider_return")
    report = _report(memory, prepared, outcome, terminal, provider_called=True)
    report["response_receipt"] = receipt
    if isinstance(exception, (KeyboardInterrupt, SystemExit)):
        raise exception
    return report


def _execute(memory, prepared, request, provider, packet_builder,
             execution_binding=None, failure_classifier=None):
    """The single hold: serve, eat and clear while the caller holds the broker."""
    served = _serve(memory, prepared, request, provider, packet_builder,
                    execution_binding=execution_binding, failure_classifier=failure_classifier)
    if "existing" in served:
        return served["existing"]
    return _clear(memory, prepared, served, _eat(provider, served["packet"], served["raw"],
                                               served["execution_binding"], failure_classifier))


def execute_carved(memory, prepare, request, provider, *, packet_builder=None,
                   serve_wait=60, clear_wait=3600, execution_binding=None, failure_classifier=None):
    """Serve and clear under the broker; the provider eats on the run's own lease.

    ``prepare(memory)`` runs in the same broker hold as admission and dispatch,
    so no newer outcome or review can fall between them. The lease is taken
    before the dispatch record exists and is released after the terminal is
    retained, or by the OS when the process ends, after which the observer
    reconciles the dispatch as before. Returns ``(prepared, report)``. A caller
    already holding the broker keeps the single hold.
    """
    memory = _memory(memory)
    nested = Path(memory.workspace).resolve() in getattr(_LOCKS, "held", set())
    with ExitStack() as plate:
        with locked(memory, timeout=serve_wait) as memory:
            _check_stop(memory)
            _reconcile(memory)
            prepared = prepare(memory)
            if nested:
                return prepared, _execute(memory, prepared, request, provider, packet_builder,
                                           execution_binding, failure_classifier)
            lease = _lease_path(memory, prepared["run_handle"])
            ensure_directory(lease.parent)
            plate.enter_context(file_lock(lease))
            served = _serve(memory, prepared, request, provider, packet_builder,
                            own_lease=prepared["run_handle"], execution_binding=execution_binding,
                            failure_classifier=failure_classifier)
        if "existing" in served:
            return prepared, served["existing"]
        eaten = _eat(provider, served["packet"], served["raw"], served["execution_binding"], failure_classifier)
        with locked(memory, timeout=clear_wait) as memory:
            report = _clear(memory, prepared, served, eaten)
            # Release native custody before removal. Windows denies unlinking a
            # held lease; the broker still excludes a new reader during cleanup.
            plate.close()
            lease.unlink(missing_ok=True)
            return prepared, report


def execute_prepared(memory, prepared, request, provider, *, packet_builder=None,
                     execution_binding=None, failure_classifier=None):
    with locked(memory) as memory:
        _check_stop(memory)
        _reconcile(memory)
        return _execute(memory, prepared, request, provider, packet_builder,
                        execution_binding, failure_classifier)


def execute(memory, seeds, request, provider, *, encounter, purpose, reader,
            run_id=None, applicability=None, packet_builder=None,
            execution_binding=None, failure_classifier=None):
    with locked(memory) as memory:
        _check_stop(memory)
        _reconcile(memory)
        prepared = memory.prepare(seeds, encounter=encounter, purpose=purpose, reader=reader,
                                  run_id=run_id, applicability=applicability)
        return _execute(memory, prepared, request, provider, packet_builder,
                        execution_binding, failure_classifier)
