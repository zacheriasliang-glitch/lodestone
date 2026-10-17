"""B_BODYMAP and B_WORKSPACE: identity is based on canonical bodies, not labels."""

from __future__ import annotations

import tempfile
from pathlib import Path

from .contracts import BodyIdentity, Environment, canonical_path, is_within


def B_BODYMAP(
    requested_root: str | Path,
    execution_root: str | Path,
    writable_roots: list[str],
    *,
    marker_match: bool = False,
) -> str:
    requested = canonical_path(requested_root)
    execution = canonical_path(execution_root)
    writable = {canonical_path(root) for root in writable_roots}
    if execution == requested or is_within(execution, requested):
        return BodyIdentity.MATCH.value
    if requested not in writable and execution in writable:
        return BodyIdentity.MISMATCH.value
    if marker_match:
        return BodyIdentity.MIRROR.value
    return BodyIdentity.UNVERIFIED.value


def B_WORKSPACE(
    requested_root: str | Path,
    execution_root: str | Path,
    readable_roots: list[str],
    writable_roots: list[str],
    *,
    marker_match: bool = False,
) -> tuple[str, str, str]:
    requested = canonical_path(requested_root)
    execution = canonical_path(execution_root)
    readable = {canonical_path(root) for root in readable_roots}
    writable = {canonical_path(root) for root in writable_roots}
    identity = B_BODYMAP(requested, execution, list(writable), marker_match=marker_match)
    temp_root = canonical_path(tempfile.gettempdir())

    if identity == BodyIdentity.MATCH.value:
        if (Path(requested) / ".git").exists():
            environment = Environment.GIT_WORKTREE.value
        else:
            environment = Environment.LOCAL_WORKSPACE.value
    elif is_within(execution, temp_root):
        environment = Environment.TEMPORARY_COPY.value
    elif requested not in writable and execution in writable:
        environment = Environment.SANDBOX.value
    elif execution.startswith(("/workspace", "/workspaces")):
        environment = Environment.REMOTE_WORKSPACE.value
    else:
        environment = Environment.UNKNOWN.value

    if identity == BodyIdentity.MATCH.value and requested in writable:
        state = "WRITE_READY"
    elif identity == BodyIdentity.MATCH.value and requested in readable:
        state = "READ_ONLY"
    elif environment in {Environment.SANDBOX.value, Environment.TEMPORARY_COPY.value}:
        state = "SANDBOXED"
    elif identity == BodyIdentity.MISMATCH.value:
        state = "DETACHED"
    else:
        state = "UNKNOWN"
    return environment, identity, state
