"""B_ATTACH: build a bounded, expiring attachment contract."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .contracts import (
    AUTHORITY,
    CAPABILITY_NAMES,
    ENGINE_NAME,
    ENGINE_VERSION,
    CapabilityState,
    canonical_path,
    compute_contract_id,
    validate_attachment_contract,
)
from .permissions import unknown_capabilities
from .workspace_identity import B_WORKSPACE


def observe_local_context(requested_root: str | Path, execution_root: str | Path) -> dict[str, Any]:
    """Observe only local filesystem facts; unobserved integrations remain unknown."""

    requested = Path(requested_root).resolve()
    execution = Path(execution_root).resolve()
    readable: list[str] = []
    writable: list[str] = []
    for candidate in dict.fromkeys((requested, execution)):
        if candidate.exists() and os.access(candidate, os.R_OK):
            readable.append(str(candidate))
        if candidate.exists() and os.access(candidate, os.W_OK):
            writable.append(str(candidate))
    capabilities = unknown_capabilities()
    capabilities["read"] = (
        CapabilityState.OBSERVED.value if str(requested) in readable else CapabilityState.DENIED.value
    )
    capabilities["write"] = (
        CapabilityState.OBSERVED.value if str(requested) in writable else CapabilityState.DENIED.value
    )
    run_path = requested / "orchestrator" / "run"
    capabilities["execute"] = (
        CapabilityState.OBSERVED.value
        if run_path.is_file() and os.access(run_path, os.X_OK)
        else CapabilityState.DENIED.value
    )
    git_marker = requested / ".git"
    if git_marker.exists():
        capabilities["git_read"] = CapabilityState.OBSERVED.value
        capabilities["git_write"] = (
            CapabilityState.OBSERVED.value
            if str(requested) in writable
            else CapabilityState.DENIED.value
        )
    else:
        capabilities["git_read"] = CapabilityState.DENIED.value
        capabilities["git_write"] = CapabilityState.DENIED.value
    return {
        "requested_root": str(requested),
        "execution_root": str(execution),
        "readable_roots": sorted(readable),
        "writable_roots": sorted(writable),
        "capabilities": capabilities,
    }


def B_ATTACH(
    requested_root: str | Path,
    execution_root: str | Path,
    *,
    readable_roots: list[str] | None = None,
    writable_roots: list[str] | None = None,
    capabilities: dict[str, str] | None = None,
    observed_at: datetime | None = None,
    ttl_seconds: int = 900,
    marker_match: bool = False,
) -> dict[str, Any]:
    if ttl_seconds <= 0:
        raise ValueError("attachment TTL must be positive")
    requested = canonical_path(requested_root)
    execution = canonical_path(execution_root)
    if readable_roots is None or writable_roots is None or capabilities is None:
        observed = observe_local_context(requested, execution)
        readable_roots = observed["readable_roots"] if readable_roots is None else readable_roots
        writable_roots = observed["writable_roots"] if writable_roots is None else writable_roots
        capabilities = observed["capabilities"] if capabilities is None else capabilities
    canonical_readable = sorted({canonical_path(root) for root in readable_roots})
    canonical_writable = sorted({canonical_path(root) for root in writable_roots})
    complete_capabilities = unknown_capabilities()
    complete_capabilities.update(capabilities)
    if set(complete_capabilities) != set(CAPABILITY_NAMES):
        raise ValueError("invalid capability keys")
    environment, body_identity, attachment_state = B_WORKSPACE(
        requested,
        execution,
        canonical_readable,
        canonical_writable,
        marker_match=marker_match,
    )
    timestamp = observed_at or datetime.now(timezone.utc)
    timestamp = timestamp.astimezone(timezone.utc)
    expiry = timestamp + timedelta(seconds=ttl_seconds)
    epoch = timestamp.strftime("%Y%m%dT%H%M%SZ")
    contract: dict[str, Any] = {
        "schema_version": "1.0.0",
        "engine": ENGINE_NAME,
        "engine_version": ENGINE_VERSION,
        "organ": "B_ATTACH",
        "authority": AUTHORITY,
        "attachment_contract_id": "",
        "requested_root": requested,
        "execution_root": execution,
        "environment": environment,
        "body_identity": body_identity,
        "attachment_state": attachment_state,
        "readable_roots": canonical_readable,
        "writable_roots": canonical_writable,
        "capabilities": complete_capabilities,
        "attachment_epoch": epoch,
        "observed_at_utc": timestamp.isoformat().replace("+00:00", "Z"),
        "expires_at_utc": expiry.isoformat().replace("+00:00", "Z"),
        "minimal_falsifier": (
            "Any canonical-root, body-identity, writable-root, capability, target-scope, "
            "or epoch observation differs from this contract."
        ),
        "expires_when": (
            "expires_at_utc is reached or execution root, writable roots, capabilities, "
            "body identity, or authorized target scope changes"
        ),
    }
    contract["attachment_contract_id"] = compute_contract_id(
        contract, "attachment_contract_id", "ATT"
    )
    validate_attachment_contract(contract)
    return contract
