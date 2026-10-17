"""Production integration surface for the Kernel 21 embodied transport bridge."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from engine.contracts import validate_json_schema
from engine.kernel20_phenotype import Kernel20Phenotype

from .attachment import B_ATTACH
from .contracts import canonical_json, sha256_json
from .handoff import B_HANDOFF
from .return_verify import B_RETURN
from .transport import B_TRANSPORT
from .witness import create_lifecycle_witness, verify_lifecycle_witness


def _load_schema(orchestrator_root: Path, name: str) -> dict[str, Any]:
    return json.loads((Path(__file__).resolve().parents[1] / "schemas" / name).read_text(encoding="utf-8"))


def build_preflight(
    repository_root: Path,
    policy: dict[str, Any],
    experiment: str,
    goal: str,
    *,
    execution_root: Path | None = None,
    observed_at: datetime | None = None,
    readable_roots: list[str] | None = None,
    writable_roots: list[str] | None = None,
    capabilities: dict[str, str] | None = None,
    host_approval_satisfied: bool = False,
    audit_finding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    engine = Kernel20Phenotype(repository_root, policy)
    decision = engine.plan(experiment, goal, audit_finding=audit_finding)
    engine.validate_with_schema(decision)
    attachment = B_ATTACH(
        repository_root,
        execution_root or Path.cwd(),
        readable_roots=readable_roots,
        writable_roots=writable_roots,
        capabilities=capabilities,
        observed_at=observed_at,
    )
    transport_plan = B_TRANSPORT(
        attachment,
        decision,
        host_approval_satisfied=host_approval_satisfied,
    )
    envelope = B_HANDOFF(decision, attachment, transport_plan)
    orchestrator_root = repository_root / "orchestrator"
    validate_json_schema(
        attachment,
        _load_schema(orchestrator_root, "attachment_contract.schema.json"),
    )
    validate_json_schema(
        transport_plan,
        _load_schema(orchestrator_root, "transport_plan.schema.json"),
    )
    return {
        "decision": decision,
        "attachment": attachment,
        "transport_plan": transport_plan,
        "envelope": envelope,
    }


def _write_json_exclusive(path: Path, value: dict[str, Any]) -> str:
    payload = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    with path.open("x", encoding="utf-8") as handle:
        handle.write(payload)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def emit_preflight_artifacts(
    repository_root: Path,
    experiment: str,
    preflight: dict[str, Any],
) -> dict[str, str]:
    output_root = (repository_root / "orchestrator" / "output").resolve()
    if not output_root.is_relative_to(repository_root.resolve()):
        raise ValueError("orchestrator output escapes the project root")
    output_root.mkdir(parents=True, exist_ok=True)
    attachment = preflight["attachment"]
    short_id = attachment["attachment_contract_id"].split("-", 1)[1][:12]
    stem = f"{attachment['attachment_epoch']}__experiment-{experiment}__{short_id}__kernel21"
    paths = {
        "attachment_contract": output_root / f"{stem}__attachment-contract.json",
        "transport_plan": output_root / f"{stem}__transport-plan.json",
        "envelope": output_root / f"{stem}__task-envelope.json",
    }
    hashes = {
        "attachment_contract": _write_json_exclusive(paths["attachment_contract"], attachment),
        "transport_plan": _write_json_exclusive(paths["transport_plan"], preflight["transport_plan"]),
        "envelope": _write_json_exclusive(paths["envelope"], preflight["envelope"]),
    }
    return {
        "attachment_contract_path": str(paths["attachment_contract"].relative_to(repository_root)),
        "attachment_contract_sha256": hashes["attachment_contract"],
        "transport_plan_path": str(paths["transport_plan"].relative_to(repository_root)),
        "transport_plan_sha256": hashes["transport_plan"],
        "envelope_path": str(paths["envelope"].relative_to(repository_root)),
        "envelope_sha256": hashes["envelope"],
    }


def run_preflight(
    repository_root: Path,
    policy: dict[str, Any],
    experiment: str,
    goal: str,
    *,
    execution_root: Path | None = None,
    audit_finding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    preflight = build_preflight(
        repository_root,
        policy,
        experiment,
        goal,
        execution_root=execution_root,
        audit_finding=audit_finding,
    )
    artifacts = emit_preflight_artifacts(repository_root, experiment, preflight)
    return {**preflight, "artifacts": artifacts}


def _load_repository_json(repository_root: Path, relative: str, label: str) -> dict[str, Any]:
    path = (repository_root / relative).resolve()
    if not path.is_file() or not path.is_relative_to(repository_root.resolve()):
        raise ValueError(f"{label} must exist inside the requested repository")
    return json.loads(path.read_text(encoding="utf-8"))


def verify_return_files(
    repository_root: Path,
    envelope_path: str,
    receipt_path: str,
    *,
    observed_root: Path | None = None,
    attachment_path: str | None = None,
    transport_plan_path: str | None = None,
) -> dict[str, Any]:
    """Verify a return against the persisted envelope and its trusted carrier.

    ``attachment_path`` and ``transport_plan_path`` name the attachment contract
    and transport plan emitted at preflight. Return verification fails closed
    without them.
    """

    envelope = _load_repository_json(repository_root, envelope_path, "envelope")
    attachment = (
        _load_repository_json(repository_root, attachment_path, "attachment contract")
        if attachment_path is not None
        else None
    )
    transport_plan = (
        _load_repository_json(repository_root, transport_plan_path, "transport plan")
        if transport_plan_path is not None
        else None
    )
    receipt_file = (repository_root / receipt_path).resolve()
    result = B_RETURN(
        envelope,
        receipt_file,
        observed_root=observed_root or Path.cwd(),
        attachment=attachment,
        transport_plan=transport_plan,
    )
    return result


__all__ = [
    "B_ATTACH",
    "B_HANDOFF",
    "B_RETURN",
    "B_TRANSPORT",
    "build_preflight",
    "create_lifecycle_witness",
    "emit_preflight_artifacts",
    "run_preflight",
    "verify_lifecycle_witness",
    "verify_return_files",
]
