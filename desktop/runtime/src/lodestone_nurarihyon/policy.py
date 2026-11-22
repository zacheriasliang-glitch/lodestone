"""Trusted admission and acceptance policy, never a transformation target.

This is an application control core, not a macOS kernel extension or an OS
sandbox. Host account code is trusted. Source documents cannot register code,
change these limits, execute instructions or approve their own proposals.
"""
from .common import canonical, digest
from .conscience_binding import validate_binding

DEFAULT_POLICY = {
    "schema": "nurarihyon-policy/v1",
    "allowed_toolkits": ["pando.profile/v1"],
    "external_adapters": False,
    "max_source_bytes": 65536,
    "max_tasks": 128,
    "max_attempts": 3,
    "worker_wall_seconds": 20,
    "supervisor_wall_seconds": 20,
    "worker_cpu_seconds": 10,
    "worker_rss_bytes": 268435456,
    "max_stage_bytes": 8388608,
    "max_state_bytes": 67108864,
    "max_files": 8192,
}

# Host-configured ceilings for an explicit policy. Defaults remain unchanged;
# source/task data cannot select or grant these limits. Other bounds still use
# DEFAULT_POLICY and existing admission, Conscience and STOP checks.
RESOURCE_CEILINGS = {
    "worker_cpu_seconds": 30,
    "worker_wall_seconds": 60,
    "supervisor_wall_seconds": 90,
}

RECONCILIATION_TOOLKIT = "bezoar.reconciliation/v1"
COMPLETE_CAPTURE_TOOLKIT = "bezoar.reconciliation-capture/v1"
OBSERVATION_TOOLKIT = "bezoar.reconciliation-observation/v1"


def validate_policy(policy):
    # Optional additive field: old sealed policies retain their original total
    # wall budget; newly initialized policies declare separate phase budgets.
    # conscience-wiring-01: an optional sealed Conscience binding block.
    fields = set(policy) - {"conscience"} if type(policy) is dict else None
    if fields not in (set(DEFAULT_POLICY), set(DEFAULT_POLICY) - {"supervisor_wall_seconds"}):
        raise ValueError("Policy fields mismatch")
    if policy["schema"] != DEFAULT_POLICY["schema"] or policy["external_adapters"] is not False:
        raise ValueError("Only the offline policy is supported")
    # The default remains profile-only. A new dedicated host policy may opt in
    # to this reviewed static route; task/source data cannot alter that policy.
    if policy["allowed_toolkits"] not in (["pando.profile/v1"],
                                          ["pando.profile/v1", "bezoar.document/v1"],
                                          ["pando.profile/v1", "bezoar.document/v1", "bezoar.reconciliation/v1"],
                                          ["pando.profile/v1", "bezoar.document/v1", "bezoar.reconciliation/v1", COMPLETE_CAPTURE_TOOLKIT],
                                          ["pando.profile/v1", "bezoar.document/v1", "bezoar.reconciliation/v1", OBSERVATION_TOOLKIT],
                                          ["pando.profile/v1", "bezoar.document/v1", "bezoar.reconciliation/v1", COMPLETE_CAPTURE_TOOLKIT, OBSERVATION_TOOLKIT]):
        raise ValueError("Toolkit registry cannot be extended through state data")
    minimum = {"max_source_bytes": 256, "max_tasks": 1, "max_attempts": 1, "worker_wall_seconds": 1,
               "supervisor_wall_seconds": 1, "worker_cpu_seconds": 1, "worker_rss_bytes": 16777216,
               "max_stage_bytes": 131072, "max_state_bytes": 1048576, "max_files": 32}
    for key, low in minimum.items():
        if key == "supervisor_wall_seconds" and key not in policy:
            continue
        value = policy[key]
        high = (16777216 if key == "max_stage_bytes" and any(
            item in policy["allowed_toolkits"] for item in (COMPLETE_CAPTURE_TOOLKIT, OBSERVATION_TOOLKIT))
                else RESOURCE_CEILINGS.get(key, DEFAULT_POLICY[key]))
        if type(value) is not int or not low <= value <= high:
            raise ValueError("Policy limit out of range: " + key)
    if policy["max_stage_bytes"] * 2 > policy["max_state_bytes"]:
        raise ValueError("State must reserve capacity for a stage and retained evidence")
    # conscience-wiring-01: the reconciliation toolkit runs only with an explicit
    # isolated Conscience policy, actor and workspace sealed into this policy
    # identity. Task/document bytes cannot supply them and there is no fallback.
    if (RECONCILIATION_TOOLKIT in policy["allowed_toolkits"]) != ("conscience" in policy):
        raise ValueError("Reconciliation toolkit and Conscience binding are enabled together or not at all")
    if "conscience" in policy:
        validate_binding(policy["conscience"])
    return digest(canonical(policy))


def check_operation(plan, policy):
    validate_policy(policy)
    if plan.get("toolkit") not in policy["allowed_toolkits"]:
        raise ValueError("Toolkit not authorized by host policy")
    if plan.get("authority") != "PROPOSAL_ONLY":
        raise ValueError("Plan cannot assert execution authority")


def block_external_calls(write_root=None):
    """Process-local network/process audit guard; no system settings changed."""
    import sys
    import os
    from pathlib import Path
    root = Path(write_root).resolve() if write_root is not None else None
    def writable(path):
        if root is None or isinstance(path, int):
            return
        if not Path(os.fsdecode(path)).resolve().is_relative_to(root):
            raise PermissionError("Toolkit write outside private staging refused")
    def guard(event, args):
        if event.startswith("socket.") or event in ("subprocess.Popen", "os.system", "os.exec", "os.posix_spawn"):
            raise PermissionError("External adapters and subprocess spawning are disabled in toolkit workers")
        if event == "open" and root is not None:
            path, mode, flags = args
            if ((type(mode) is str and any(c in mode for c in "wax+"))
                    or (type(flags) is int and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))):
                writable(path)
        elif event in ("os.remove", "os.rmdir", "os.mkdir", "os.chmod", "os.truncate", "os.utime"):
            writable(args[0])
        elif event in ("os.rename", "os.link", "os.symlink"):
            writable(args[0]); writable(args[1])
    sys.addaudithook(guard)


def operation_limits(policy, toolkit):
    """Only an opted-in captured-evidence contract may use the16MiB stage.

    All existing operations retain their original ceiling even in a host policy
    that also admits the new operation. Memory/state/CPU/wall limits are unchanged.
    """
    limits = dict(policy)
    if toolkit not in (COMPLETE_CAPTURE_TOOLKIT, OBSERVATION_TOOLKIT):
        limits["max_stage_bytes"] = min(limits["max_stage_bytes"], DEFAULT_POLICY["max_stage_bytes"])
    return limits
