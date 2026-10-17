"""Kernel 21 bridge contracts and canonical hashing helpers."""

from __future__ import annotations

import copy
import hashlib
import json
from enum import Enum
from pathlib import Path
from typing import Any

from engine.contracts import compute_decision_id


ENGINE_NAME = "K21_EMBODIED_TRANSPORT_BRIDGE"
ENGINE_VERSION = "1.0.0"
AUTHORITY = "PROPOSAL_ONLY"


class Environment(str, Enum):
    LOCAL_WORKSPACE = "LOCAL_WORKSPACE"
    SANDBOX = "SANDBOX"
    TEMPORARY_COPY = "TEMPORARY_COPY"
    REMOTE_WORKSPACE = "REMOTE_WORKSPACE"
    GIT_WORKTREE = "GIT_WORKTREE"
    UNKNOWN = "UNKNOWN"


class BodyIdentity(str, Enum):
    MATCH = "MATCH"
    MIRROR = "MIRROR"
    MISMATCH = "MISMATCH"
    UNVERIFIED = "UNVERIFIED"
    STALE = "STALE"


class CapabilityState(str, Enum):
    OBSERVED = "OBSERVED"
    DENIED = "DENIED"
    UNKNOWN = "UNKNOWN"


class Transport(str, Enum):
    DIRECT_FILESYSTEM = "DIRECT_FILESYSTEM"
    CODEX_WORKSPACE = "CODEX_WORKSPACE"
    GIT = "GIT"
    MCP = "MCP"
    API = "API"
    PACKET_EXPORT = "PACKET_EXPORT"
    HOST_MANUAL = "HOST_MANUAL"
    NONE = "NONE"


class ReturnStatus(str, Enum):
    COMPLETE = "COMPLETE"
    WRONG_BODY = "WRONG_BODY"
    MISMATCH = "MISMATCH"
    STALE = "STALE"
    AUDIT_REQUIRED = "AUDIT_REQUIRED"
    HOST_APPROVAL_REQUIRED = "HOST_APPROVAL_REQUIRED"
    UNKNOWN = "UNKNOWN"


CAPABILITY_NAMES = (
    "read",
    "write",
    "execute",
    "network",
    "spawn_process",
    "request_approval",
    "git_read",
    "git_write",
    "mcp",
    "api",
)

SAFE_TRANSPORT_PRECEDENCE = (
    Transport.NONE.value,
    Transport.HOST_MANUAL.value,
    Transport.PACKET_EXPORT.value,
    Transport.MCP.value,
    Transport.API.value,
    Transport.GIT.value,
    Transport.CODEX_WORKSPACE.value,
    Transport.DIRECT_FILESYSTEM.value,
)


def canonical_path(path: str | Path) -> str:
    return str(Path(path).expanduser().resolve())


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def compute_contract_id(value: dict[str, Any], id_field: str, prefix: str) -> str:
    canonical = copy.deepcopy(value)
    canonical.pop(id_field, None)
    return f"{prefix}-{sha256_json(canonical)[:24]}"


def is_within(path: str | Path, root: str | Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def path_is_protected(target: str, protected: str) -> bool:
    """Apply Kernel 20's numbered-stem protection to bridge target checks."""

    target_path = Path(target)
    protected_path = Path(protected)
    try:
        target_path.relative_to(protected_path)
        return True
    except ValueError:
        return (
            target_path.parent == protected_path.parent
            and protected_path.name.isdigit()
            and target_path.name.startswith(f"{protected_path.name}-")
        )


def validate_capabilities(capabilities: dict[str, str]) -> None:
    if set(capabilities) != set(CAPABILITY_NAMES):
        raise ValueError("capability map must contain every declared capability exactly once")
    allowed = {state.value for state in CapabilityState}
    for name, value in capabilities.items():
        if value not in allowed:
            raise ValueError(f"invalid capability state for {name}: {value}")


def validate_attachment_contract(contract: dict[str, Any]) -> None:
    required = {
        "schema_version", "engine", "engine_version", "organ", "authority",
        "attachment_contract_id", "requested_root", "execution_root", "environment",
        "body_identity", "attachment_state", "readable_roots", "writable_roots",
        "capabilities", "attachment_epoch", "observed_at_utc", "expires_at_utc",
        "minimal_falsifier", "expires_when",
    }
    missing = sorted(required - set(contract))
    if missing:
        raise ValueError(f"attachment contract missing: {', '.join(missing)}")
    if contract["engine"] != ENGINE_NAME or contract["authority"] != AUTHORITY:
        raise ValueError("invalid bridge engine or authority")
    if contract["environment"] not in {item.value for item in Environment}:
        raise ValueError("invalid execution environment")
    if contract["body_identity"] not in {item.value for item in BodyIdentity}:
        raise ValueError("invalid body identity")
    validate_capabilities(contract["capabilities"])
    if canonical_path(contract["requested_root"]) != contract["requested_root"]:
        raise ValueError("requested root is not canonical")
    if canonical_path(contract["execution_root"]) != contract["execution_root"]:
        raise ValueError("execution root is not canonical")
    expected = compute_contract_id(contract, "attachment_contract_id", "ATT")
    if contract["attachment_contract_id"] != expected:
        raise ValueError("attachment contract ID does not match canonical content")


def validate_transport_plan(plan: dict[str, Any]) -> None:
    required = {
        "schema_version", "engine", "engine_version", "organ", "authority",
        "transport_plan_id", "attachment_contract_id", "transport_selected",
        "delivery_root", "return_root", "authorized_roots", "target_files",
        "protected_files", "protected_overlap", "host_approval_required",
        "host_approval_satisfied", "receipt_requirement", "stop_condition",
        "rationale", "status",
    }
    missing = sorted(required - set(plan))
    if missing:
        raise ValueError(f"transport plan missing: {', '.join(missing)}")
    if plan["authority"] != AUTHORITY:
        raise ValueError("transport authority must be proposal-only")
    if plan["transport_selected"] not in {item.value for item in Transport}:
        raise ValueError("invalid transport selection")
    if not plan["return_root"]:
        raise ValueError("transport requires a return root")
    expected = compute_contract_id(plan, "transport_plan_id", "TRN")
    if plan["transport_plan_id"] != expected:
        raise ValueError("transport plan ID does not match canonical content")


def validate_envelope(envelope: dict[str, Any]) -> None:
    required = {
        "schema_version", "engine", "engine_version", "organ", "authority",
        "envelope_id", "kernel20_decision_id", "kernel20_payload_sha256",
        "kernel20_decision", "attachment_contract_id", "attachment_contract_sha256",
        "attachment_epoch", "attachment_expires_at_utc", "transport_plan_id",
        "transport_plan_sha256", "requested_root", "delivery_root", "return_root",
        "route", "goal", "target_files", "protected_files", "predicted_outputs",
        "receipt_requirement", "approval_requirement", "stop_condition",
    }
    missing = sorted(required - set(envelope))
    if missing:
        raise ValueError(f"task envelope missing: {', '.join(missing)}")
    if envelope["authority"] != AUTHORITY:
        raise ValueError("task envelope authority must be proposal-only")
    if sha256_json(envelope["kernel20_decision"]) != envelope["kernel20_payload_sha256"]:
        raise ValueError("Kernel 20 payload hash mismatch")
    nested = envelope["kernel20_decision"]
    if nested.get("decision_id") != compute_decision_id(nested):
        raise ValueError("nested Kernel 20 decision_id does not match its canonical content")
    if envelope["kernel20_decision"].get("decision_id") != envelope["kernel20_decision_id"]:
        raise ValueError("Kernel 20 decision ID mismatch")
    decision = envelope["kernel20_decision"]
    semantic_fields = {
        "route": "route",
        "goal": "goal",
        "target_files": "target_files",
        "protected_files": "protected_files",
        "predicted_outputs": "predicted_outputs",
        "stop_condition": "stop_condition",
    }
    for envelope_field, decision_field in semantic_fields.items():
        if envelope[envelope_field] != decision[decision_field]:
            raise ValueError(f"task envelope altered Kernel 20 {decision_field}")
    expected = compute_contract_id(envelope, "envelope_id", "ENV")
    if envelope["envelope_id"] != expected:
        raise ValueError("task envelope ID does not match canonical content")
