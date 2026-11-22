"""B_RECOVERY: fail-closed next actions for bridge verification states."""

from __future__ import annotations

from .contracts import ReturnStatus


def B_RECOVERY(status: str) -> dict[str, str]:
    actions = {
        ReturnStatus.COMPLETE.value: "Close the bridge cycle; no recovery action is required.",
        ReturnStatus.WRONG_BODY.value: "Re-run verification from the canonical requested repository body.",
        ReturnStatus.MISMATCH.value: "Stop and reconcile the decision, attachment, receipt, and hashes.",
        ReturnStatus.STALE.value: "Discard the handoff and run a fresh attachment preflight.",
        ReturnStatus.AUDIT_REQUIRED.value: "Obtain independent evidence for missing outputs or hashes.",
        ReturnStatus.HOST_APPROVAL_REQUIRED.value: "Request explicit host approval; do not escalate automatically.",
        ReturnStatus.UNKNOWN.value: "Stop; gather the missing exact state before selecting a transport.",
    }
    return {"organ": "B_RECOVERY", "status": status, "next_action": actions.get(status, actions["UNKNOWN"])}
