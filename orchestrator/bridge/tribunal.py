"""B_ATTACHMENT_TRIBUNAL: adversarial validation before handoff."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from engine.contracts import compute_decision_id, validate_decision

from .contracts import sha256_json, validate_attachment_contract, validate_transport_plan
from .permissions import B_FRESHNESS


def B_ATTACHMENT_TRIBUNAL(
    decision: dict[str, Any],
    attachment: dict[str, Any],
    transport_plan: dict[str, Any],
    *,
    expected_payload_sha256: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    issues: list[str] = []
    try:
        validate_decision(decision)
    except ValueError as error:
        issues.append(str(error))
    try:
        validate_attachment_contract(attachment)
    except ValueError as error:
        issues.append(str(error))
    try:
        validate_transport_plan(transport_plan)
    except ValueError as error:
        issues.append(str(error))

    payload_hash = sha256_json(decision)
    if expected_payload_sha256 is not None and payload_hash != expected_payload_sha256:
        issues.append("Kernel 20 payload hash changed after declaration")
    if decision.get("decision_id") != compute_decision_id(decision):
        issues.append("Kernel 20 decision ID is not hash-bound to its payload")
    if transport_plan.get("attachment_contract_id") != attachment.get("attachment_contract_id"):
        issues.append("transport plan references a different attachment contract")
    if not transport_plan.get("return_root"):
        issues.append("transport has no return path")
    if transport_plan.get("return_root") != attachment.get("requested_root"):
        issues.append("transport return root differs from requested repository")
    if transport_plan.get("target_files") != decision.get("target_files"):
        issues.append("transport altered Kernel 20 target files")
    if transport_plan.get("protected_files") != decision.get("protected_files"):
        issues.append("transport altered Kernel 20 protected files")
    if not B_FRESHNESS(attachment, now=now):
        issues.append("attachment epoch is stale")
    if not attachment.get("minimal_falsifier"):
        issues.append("attachment has no minimal falsifier")
    return {
        "organ": "B_ATTACHMENT_TRIBUNAL",
        "accepted": not issues,
        "kernel20_payload_sha256": payload_hash,
        "issues": issues,
    }
