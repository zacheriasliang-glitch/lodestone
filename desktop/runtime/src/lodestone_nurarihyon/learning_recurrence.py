"""Scoped resource-stop checks over broker-captured execution facts.

Trusted adapters supply effective source/input/limits and classify their own
supervisor measurements. SEED text, model output, record names and run IDs never
become executable rules. This prevents an unchanged resource-stop recipe from
being dispatched again; it cannot predict arbitrary semantic or transient faults.
"""
import math
import re

from lodestone_machinery.records import canonical


def binding(value):
    if value is None:
        return None
    if (type(value) is not dict or set(value) != {
            "schema", "operation", "source_sha256", "input_sha256", "limits"}
            or value["schema"] != "loom-execution-binding/v1"
            or type(value["operation"]) is not str
            or re.fullmatch(r"[a-z][a-z0-9_.-]{0,95}", value["operation"]) is None):
        raise ValueError("Invalid trusted execution binding")
    for key in ("source_sha256", "input_sha256"):
        if type(value[key]) is not str or re.fullmatch(r"[0-9a-f]{64}", value[key]) is None:
            raise ValueError("Execution source and effective input require exact SHA-256 identities")
    limits = value["limits"]
    if type(limits) is not dict or set(limits) != {"wall_seconds", "max_rss_bytes"}:
        raise ValueError("Execution binding requires explicit resource limits")
    for limit in limits.values():
        if type(limit) not in (int, float) or not math.isfinite(limit) or limit <= 0:
            raise ValueError("Execution resource limits must be positive finite numbers")
    # Return a detached value; callback mutation must not rewrite the dispatch.
    import json
    return json.loads(canonical(value))


def checked_failure(value, execution):
    """Validate a trusted supervisor measurement, never infer it from prose."""
    if value is None:
        return None
    execution = binding(execution)
    if (execution is None or type(value) is not dict
            or set(value) != {"resource", "observed", "limit"}
            or value["resource"] not in ("max_rss_bytes", "wall_seconds")):
        raise ValueError("Invalid measured resource failure")
    for key in ("observed", "limit"):
        if type(value[key]) not in (int, float) or not math.isfinite(value[key]):
            raise ValueError("Resource failure requires finite measurements")
    if (value["limit"] != execution["limits"][value["resource"]]
            or value["observed"] < value["limit"]):
        raise ValueError("Resource failure does not meet its effective bound")
    return dict(value)


def assess(memory, entries, execution):
    execution = binding(execution)
    base = {"schema": "loom-recurrence-assessment/v1", "authority": "NONE",
            "scope": "Repeated measured resource stop; not semantic failure prevention"}
    if execution is None:
        return {**base, "verdict": "ADVICE_ONLY", "reason": "No trusted execution binding supplied"}
    for handle, record in entries:
        if record["kind"] != "learning-mechanical-failure":
            continue
        payload = record["payload"]
        dispatch = memory.store.relay("nurarihyon", payload["dispatch_handle"],
                                      owner="nurarihyon", kind="learning-dispatch")
        response = memory.store.relay("nurarihyon", payload["response_handle"],
                                      owner="nurarihyon", kind="learning-response")
        old = binding(dispatch["payload"].get("execution_binding"))
        if (response["payload"]["dispatch_handle"] != payload["dispatch_handle"]
                or response["payload"]["status"] not in ("FAILED", "INTERRUPTED")
                or old is None):
            raise ValueError("Measured resource failure lost its executed source")
        failure = checked_failure(payload["failure"], old)
        if failure != response["payload"].get("mechanical_failure"):
            raise ValueError("Measured resource failure differs from its captured response")
        if (any(execution[key] != old[key] for key in
                ("operation", "source_sha256", "input_sha256"))
                or execution["limits"][failure["resource"]] != failure["limit"]):
            continue
        prepared = memory.read_prepared(dispatch["payload"]["run_handle"])
        outcome = memory.outcome_for(prepared)
        if outcome is None:
            raise ValueError("Measured resource failure is awaiting outcome recovery")
        terminal = memory.read_outcome(outcome)
        if (terminal["status"] != response["payload"]["status"]
                or type(terminal["observation"]) is not dict
                or terminal["observation"].get("provider_receipt") != payload["response_handle"]):
            raise ValueError("Measured resource failure differs from its actual terminal outcome")
        return {**base, "verdict": "REFUSE", "failure_handle": handle,
                "source_outcome": outcome, "resource": failure["resource"],
                "reason": "This source and effective input already reached the same "
                          + failure["resource"] + " limit. Change the relevant input, implementation "
                          "or resource condition before another attempt; retained advice alone does not change execution."}
    return {**base, "verdict": "ALLOW", "reason": "No matching measured resource-stop recipe retained"}
