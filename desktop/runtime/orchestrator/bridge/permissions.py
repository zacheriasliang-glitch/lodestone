"""B_PERMISSION and B_FRESHNESS capability and epoch guards."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .contracts import (
    CAPABILITY_NAMES,
    BodyIdentity,
    CapabilityState,
    canonical_path,
    is_within,
    path_is_protected,
)


def B_PERMISSION(
    requested_root: str | Path,
    writable_roots: list[str],
    capabilities: dict[str, str],
    target_files: list[str],
    protected_files: list[str],
    authorized_roots: list[str],
    body_identity: str,
    *,
    approval_required: bool,
    approval_satisfied: bool,
) -> dict[str, object]:
    requested = canonical_path(requested_root)
    writable = {canonical_path(root) for root in writable_roots}
    authorized = [canonical_path(root) for root in authorized_roots]
    target_paths = [(Path(requested) / target).resolve() for target in target_files]
    targets_authorized = all(any(is_within(path, root) for root in authorized) for path in target_paths)
    protected_overlap = any(
        path_is_protected(target, protected)
        for target in target_files
        for protected in protected_files
    )
    direct_allowed = all(
        (
            body_identity == BodyIdentity.MATCH.value,
            requested in writable,
            capabilities.get("write") == CapabilityState.OBSERVED.value,
            targets_authorized,
            not protected_overlap,
            not approval_required or approval_satisfied,
        )
    )
    return {
        "direct_allowed": direct_allowed,
        "requested_root_writable": requested in writable,
        "targets_authorized": targets_authorized,
        "protected_overlap": protected_overlap,
        "approval_satisfied": not approval_required or approval_satisfied,
    }


def B_FRESHNESS(contract: dict, *, now: datetime | None = None) -> bool:
    observed = now or datetime.now(timezone.utc)
    expiry = datetime.fromisoformat(contract["expires_at_utc"].replace("Z", "+00:00"))
    return observed < expiry and contract.get("body_identity") != BodyIdentity.STALE.value


def unknown_capabilities() -> dict[str, str]:
    return {name: CapabilityState.UNKNOWN.value for name in CAPABILITY_NAMES}
