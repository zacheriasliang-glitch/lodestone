"""B_TRANSPORT: choose a reversible proposal without escalating authority."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .contracts import (
    AUTHORITY,
    ENGINE_NAME,
    ENGINE_VERSION,
    SAFE_TRANSPORT_PRECEDENCE,
    Transport,
    canonical_path,
    compute_contract_id,
    validate_transport_plan,
)
from .permissions import B_PERMISSION


def select_transport(candidate_scores: dict[str, float]) -> str:
    """Select the highest viable score and resolve exact ties by safe precedence."""

    if not candidate_scores:
        return Transport.NONE.value
    allowed = {item.value for item in Transport}
    if not set(candidate_scores).issubset(allowed):
        raise ValueError("unknown transport candidate")
    best = max(float(score) for score in candidate_scores.values())
    tied = {name for name, score in candidate_scores.items() if float(score) == best}
    return next(name for name in SAFE_TRANSPORT_PRECEDENCE if name in tied)


def B_TRANSPORT(
    attachment: dict[str, Any],
    decision: dict[str, Any],
    *,
    host_approval_satisfied: bool = False,
) -> dict[str, Any]:
    requested_root = attachment["requested_root"]
    execution_root = attachment["execution_root"]
    approval_required = bool(decision["approval_required"])
    permission = B_PERMISSION(
        requested_root,
        attachment["writable_roots"],
        attachment["capabilities"],
        decision["target_files"],
        decision["protected_files"],
        [requested_root],
        attachment["body_identity"],
        approval_required=approval_required,
        approval_satisfied=host_approval_satisfied,
    )

    if decision["route"] == "STOP":
        candidates = {Transport.NONE.value: 1.0}
        selected = select_transport(candidates)
        rationale = "Kernel 20 selected STOP; no transportable task exists."
    elif permission["direct_allowed"]:
        candidates = {
            Transport.DIRECT_FILESYSTEM.value: 1.0,
            Transport.CODEX_WORKSPACE.value: 0.95,
            Transport.PACKET_EXPORT.value: 0.50,
            Transport.HOST_MANUAL.value: 0.25,
        }
        if attachment["capabilities"].get("git_write") == "OBSERVED":
            candidates[Transport.GIT.value] = 0.80
        if attachment["capabilities"].get("mcp") == "OBSERVED":
            candidates[Transport.MCP.value] = 0.75
        if attachment["capabilities"].get("api") == "OBSERVED":
            candidates[Transport.API.value] = 0.70
        selected = select_transport(candidates)
        rationale = "Canonical body, write authority, target scope, protection, and approval checks passed."
    elif execution_root in attachment["writable_roots"]:
        selected = select_transport({
            Transport.PACKET_EXPORT.value: 0.50,
            Transport.HOST_MANUAL.value: 0.25,
        })
        rationale = "Direct mutation authority is absent; export a proposal packet for explicit host handling."
    else:
        selected = select_transport({Transport.HOST_MANUAL.value: 0.25})
        rationale = "No observed writable delivery body supports an automated bridge artifact route."

    requires_host = selected in {
        Transport.PACKET_EXPORT.value,
        Transport.HOST_MANUAL.value,
    } or (approval_required and not host_approval_satisfied)
    if selected == Transport.DIRECT_FILESYSTEM.value:
        delivery_root = requested_root
    elif selected == Transport.PACKET_EXPORT.value:
        delivery_root = canonical_path(Path(execution_root) / "orchestrator" / "output")
    else:
        delivery_root = execution_root

    status = "HOST_APPROVAL_REQUIRED" if requires_host and not host_approval_satisfied else "PROPOSED"
    plan: dict[str, Any] = {
        "schema_version": "1.0.0",
        "engine": ENGINE_NAME,
        "engine_version": ENGINE_VERSION,
        "organ": "B_TRANSPORT",
        "authority": AUTHORITY,
        "transport_plan_id": "",
        "attachment_contract_id": attachment["attachment_contract_id"],
        "transport_selected": selected,
        "delivery_root": delivery_root,
        "return_root": requested_root,
        "authorized_roots": [requested_root],
        "target_files": list(decision["target_files"]),
        "protected_files": list(decision["protected_files"]),
        "protected_overlap": bool(permission["protected_overlap"]),
        "host_approval_required": requires_host,
        "host_approval_satisfied": bool(host_approval_satisfied),
        "receipt_requirement": (
            "One append-only receipt must reside under requested_root/receipts and bind "
            "the Kernel 20 decision ID, attachment contract ID, observed outputs, and declared hashes."
        ),
        "stop_condition": decision["stop_condition"],
        "rationale": rationale,
        "status": status,
    }
    plan["transport_plan_id"] = compute_contract_id(plan, "transport_plan_id", "TRN")
    validate_transport_plan(plan)
    return plan
