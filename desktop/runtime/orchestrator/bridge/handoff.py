"""B_HANDOFF: preserve Kernel 20 semantics in a deterministic hash-bound envelope."""

from __future__ import annotations

from typing import Any

from .contracts import (
    AUTHORITY,
    ENGINE_NAME,
    ENGINE_VERSION,
    compute_contract_id,
    sha256_json,
    validate_envelope,
)
from .tribunal import B_ATTACHMENT_TRIBUNAL


def B_HANDOFF(
    decision: dict[str, Any],
    attachment: dict[str, Any],
    transport_plan: dict[str, Any],
) -> dict[str, Any]:
    payload_hash = sha256_json(decision)
    verdict = B_ATTACHMENT_TRIBUNAL(
        decision,
        attachment,
        transport_plan,
        expected_payload_sha256=payload_hash,
    )
    if not verdict["accepted"]:
        raise ValueError("attachment tribunal rejected handoff: " + "; ".join(verdict["issues"]))
    envelope: dict[str, Any] = {
        "schema_version": "1.0.0",
        "engine": ENGINE_NAME,
        "engine_version": ENGINE_VERSION,
        "organ": "B_HANDOFF",
        "authority": AUTHORITY,
        "envelope_id": "",
        "kernel20_decision_id": decision["decision_id"],
        "kernel20_payload_sha256": payload_hash,
        "kernel20_decision": decision,
        "attachment_contract_id": attachment["attachment_contract_id"],
        "attachment_contract_sha256": sha256_json(attachment),
        "attachment_epoch": attachment["attachment_epoch"],
        "attachment_expires_at_utc": attachment["expires_at_utc"],
        "transport_plan_id": transport_plan["transport_plan_id"],
        "transport_plan_sha256": sha256_json(transport_plan),
        "requested_root": attachment["requested_root"],
        "delivery_root": transport_plan["delivery_root"],
        "return_root": transport_plan["return_root"],
        "route": decision["route"],
        "goal": decision["goal"],
        "target_files": list(decision["target_files"]),
        "protected_files": list(decision["protected_files"]),
        "predicted_outputs": list(decision["predicted_outputs"]),
        "receipt_requirement": transport_plan["receipt_requirement"],
        "approval_requirement": {
            "required": bool(transport_plan["host_approval_required"]),
            "satisfied_at_handoff": bool(transport_plan["host_approval_satisfied"]),
        },
        "stop_condition": decision["stop_condition"],
    }
    envelope["envelope_id"] = compute_contract_id(envelope, "envelope_id", "ENV")
    validate_envelope(envelope)
    return envelope
