"""Durable, append-only lifecycle evidence for the Kernel 21 bridge."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from engine.contracts import validate_decision, validate_json_schema

from .attachment import B_ATTACH
from .contracts import (
    canonical_json,
    sha256_json,
    validate_attachment_contract,
    validate_envelope,
    validate_transport_plan,
)
from .handoff import B_HANDOFF
from .tribunal import B_ATTACHMENT_TRIBUNAL
from .transport import B_TRANSPORT


MANIFEST_NAME = "lifecycle-witness.json"
CONSTITUENT_ROLES = {
    "decision",
    "node_source",
    "attachment",
    "transport",
    "handoff",
    "context",
}
CONSTITUENT_FILENAMES = {
    "decision": "decision.json",
    "node_source": "node-source.md",
    "attachment": "attachment-contract.json",
    "transport": "transport-plan.json",
    "handoff": "task-envelope.json",
    "context": "lifecycle-context.json",
}
GOVERNING_DOCUMENTS = ("LABORATORY_CONSTITUTION.md", "INTERACTION_PROTOCOL.md")


def _utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("lifecycle time must be timezone-aware")
    return current.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _identity(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    return {
        "path": str(path),
        "byte_count": len(data),
        "sha256": _sha256(data),
    }


def _relative_identity(path: Path, repository_root: Path) -> dict[str, Any]:
    identity = _identity(path)
    identity["path"] = str(path.relative_to(repository_root))
    return identity


def _write_exclusive(path: Path, data: bytes, role: str, order: int, root: Path) -> dict[str, Any]:
    with path.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    return {
        "role": role,
        "path": str(path.relative_to(root)),
        "byte_count": len(data),
        "sha256": _sha256(data),
        "write_order": order,
    }


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def _assert_node_source(data: bytes) -> None:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("node source is not UTF-8") from error
    lines = {line.strip() for line in text.splitlines()}
    if "AUTHORITY               = NONE" not in lines:
        raise ValueError("node source does not declare AUTHORITY = NONE")
    if "EXECUTABLE              = FALSE" not in lines:
        raise ValueError("node source does not declare EXECUTABLE = FALSE")


def _load_schema(repository_root: Path) -> dict[str, Any]:
    path = Path(__file__).resolve().parents[1] / "schemas" / "lifecycle_witness.schema.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _existing_manifests(output_root: Path) -> Iterable[Path]:
    if not output_root.is_dir():
        return ()
    return output_root.glob(f"*/{MANIFEST_NAME}")


def _repository_relative(path: Path, root: Path, label: str) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError as error:
        raise ValueError(f"{label} must be inside the canonical repository") from error


def _content_id(value: dict[str, Any], field: str, prefix: str) -> str:
    payload = dict(value)
    payload.pop(field, None)
    return f"{prefix}-{_sha256(canonical_json(payload).encode('utf-8'))[:24]}"


def _validate_authorization(
    root: Path,
    decision_file: Path,
    decision_bytes: bytes,
    decision: dict[str, Any],
    access_point_file: Path,
    approval_file: Path,
    selected_organ: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    access_bytes = access_point_file.read_bytes()
    approval_bytes = approval_file.read_bytes()
    try:
        access = json.loads(access_bytes.decode("utf-8"))
        approval = json.loads(approval_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("authorization evidence is not valid UTF-8 JSON") from error

    if approval.get("approval_id") != _content_id(approval, "approval_id", "APR"):
        raise ValueError("Host approval record ID is not bound to its exact content")
    body_stat = root.stat()
    body = {"path": str(root), "device": body_stat.st_dev, "inode": body_stat.st_ino}
    if access.get("canonical_body") != body or approval.get("canonical_body") != body:
        raise ValueError("authorization evidence is bound to the wrong canonical body")
    access_id = access.get("access_point_id")
    if not access_id or decision.get("host_authorization_id") != access_id:
        raise ValueError("decision is not bound to the exact access point")
    if approval.get("selected_organ") != selected_organ:
        raise ValueError("Host approval record selected a different organ")
    if approval.get("status") != "APPROVED" or not approval.get("authorization", {}).get(
        "builder_standing_authorized"
    ):
        raise ValueError("Host approval record does not authorize Builder")

    decision_identity = {
        "path": _repository_relative(decision_file, root, "decision"),
        "byte_count": len(decision_bytes),
        "sha256": _sha256(decision_bytes),
        "decision_id": decision["decision_id"],
    }
    access_identity = {
        "path": _repository_relative(access_point_file, root, "access point"),
        "byte_count": len(access_bytes),
        "sha256": _sha256(access_bytes),
        "access_point_id": access_id,
    }
    approval_identity = {
        "path": _repository_relative(approval_file, root, "Host approval record"),
        "byte_count": len(approval_bytes),
        "sha256": _sha256(approval_bytes),
        "approval_id": approval["approval_id"],
    }
    if approval.get("repair_decision") != decision_identity:
        raise ValueError("Host approval record is not bound to the exact decision")
    if approval.get("access_point") != access_identity:
        raise ValueError("Host approval record is not bound to the exact access point")

    targets = decision.get("allowed_modify_targets")
    protected = decision.get("protected_scope")
    if not targets or targets != decision.get("target_files"):
        raise ValueError("decision does not contain one exact modify-target set")
    if targets != access.get("exact_modify_targets") or targets != approval.get("modify_targets"):
        raise ValueError("authorization target scope differs from the decision")
    if not protected or protected != access.get("protected_scope") or protected != approval.get(
        "protected_scope"
    ):
        raise ValueError("authorization protected scope differs from the decision")
    if approval.get("create_targets") != decision.get("allowed_create_targets", []):
        raise ValueError("authorization create-target scope differs from the decision")
    return access, approval, access_identity, approval_identity


def create_lifecycle_witness(
    repository_root: str | Path,
    decision_path: str | Path,
    node_source_path: str | Path,
    *,
    access_point_path: str | Path,
    host_approval_record_path: str | Path,
    selected_organ: str = "BUILDER",
    execution_root: str | Path | None = None,
    observed_at: datetime | None = None,
    ttl_seconds: int = 900,
) -> dict[str, Any]:
    """Create one committed witness without executing the approved task."""

    root = Path(repository_root).resolve()
    execution = Path(execution_root or Path.cwd()).resolve()
    if not root.is_dir() or not os.path.samefile(root, execution):
        raise ValueError("execution root is not the canonical repository body")
    if not os.access(root, os.R_OK | os.W_OK):
        raise ValueError("canonical repository body is not readable and writable")
    if selected_organ != "BUILDER":
        raise ValueError("this lifecycle witness requires selected organ BUILDER")

    decision_file = Path(decision_path).resolve()
    source_file = Path(node_source_path).resolve()
    access_point_file = Path(access_point_path).resolve()
    approval_file = Path(host_approval_record_path).resolve()
    decision_bytes = decision_file.read_bytes()
    source_bytes = source_file.read_bytes()
    decision_sha256 = _sha256(decision_bytes)
    source_sha256 = _sha256(source_bytes)
    try:
        decision = json.loads(decision_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Host-approved decision is not valid UTF-8 JSON") from error
    validate_decision(decision)
    if decision.get("route") != "BUILDER":
        raise ValueError("Host-approved decision does not select BUILDER")
    access, approval, access_identity, approval_identity = _validate_authorization(
        root,
        decision_file,
        decision_bytes,
        decision,
        access_point_file,
        approval_file,
        selected_organ,
    )
    source_identity = decision.get("source_node_identity")
    if not isinstance(source_identity, dict):
        raise ValueError("decision does not bind a mandatory node source identity")
    expected_source_path = (root / source_identity.get("path", "")).resolve()
    if source_file != expected_source_path:
        raise ValueError("node source path differs from the decision binding")
    if (
        len(source_bytes) != source_identity.get("byte_count")
        or source_sha256 != source_identity.get("sha256")
        or source_identity.get("authority") != "NONE"
        or source_identity.get("executable") is not False
    ):
        raise ValueError("node source bytes differ from the decision binding")
    _assert_node_source(source_bytes)

    observed = _utc(observed_at)
    if observed > datetime.now(timezone.utc):
        raise ValueError("attachment observation time cannot be in the future")
    attachment = B_ATTACH(root, execution, observed_at=observed, ttl_seconds=ttl_seconds)
    if attachment["body_identity"] != "MATCH" or attachment["attachment_state"] != "WRITE_READY":
        raise ValueError("fresh writable canonical attachment was not established")
    transport = B_TRANSPORT(attachment, decision, host_approval_satisfied=True)
    if not transport["host_approval_satisfied"]:
        raise ValueError("transport did not preserve explicit Host approval")
    handoff = B_HANDOFF(decision, attachment, transport)

    governing = {
        name: _relative_identity(root / name, root)
        for name in GOVERNING_DOCUMENTS
    }
    body_stat = root.stat()
    context: dict[str, Any] = {
        "schema_version": "1.0.0",
        "record_type": "lifecycle_context",
        "selected_organ": selected_organ,
        "host_authorization_id": access["access_point_id"],
        "host_approval_id": approval["approval_id"],
        "access_point": access_identity,
        "host_approval_record": approval_identity,
        "canonical_body": {
            "path": str(root),
            "device": body_stat.st_dev,
            "inode": body_stat.st_ino,
        },
        "decision": {
            "source_path": str(decision_file),
            "decision_id": decision["decision_id"],
            "byte_count": len(decision_bytes),
            "sha256": decision_sha256,
        },
        "node_source": {
            "source_path": str(source_file),
            "authority": "NONE",
            "executable": False,
            "byte_count": len(source_bytes),
            "sha256": source_sha256,
        },
        "governing_documents": governing,
        "attachment_contract_id": attachment["attachment_contract_id"],
        "attachment_epoch": attachment["attachment_epoch"],
        "attachment_expires_at_utc": attachment["expires_at_utc"],
        "transport_plan_id": transport["transport_plan_id"],
        "envelope_id": handoff["envelope_id"],
    }
    lifecycle_seed = {
        "context": context,
        "attachment": attachment,
        "transport": transport,
        "handoff_id": handoff["envelope_id"],
    }
    lifecycle_id = "LCW-" + _sha256(canonical_json(lifecycle_seed).encode("utf-8"))[:24]
    context["lifecycle_id"] = lifecycle_id

    output_root = (root / "orchestrator" / "output").resolve()
    if not output_root.is_relative_to(root):
        raise ValueError("orchestrator output escapes the project root")
    output_root.mkdir(parents=True, exist_ok=True)
    for prior_path in _existing_manifests(output_root):
        prior = json.loads(prior_path.read_text(encoding="utf-8"))
        if prior.get("lifecycle_id") == lifecycle_id:
            raise FileExistsError("lifecycle evidence has already been committed")
        if prior.get("attachment_contract_id") == attachment["attachment_contract_id"]:
            raise ValueError("attachment evidence has already been used")

    directory = output_root / f"{attachment['attachment_epoch']}__{lifecycle_id}__lifecycle-witness"
    directory.mkdir(mode=0o755, parents=False, exist_ok=False)
    context["lifecycle_directory"] = str(directory.relative_to(root))
    artifacts = [
        _write_exclusive(directory / "decision.json", decision_bytes, "decision", 1, root),
        _write_exclusive(directory / "node-source.md", source_bytes, "node_source", 2, root),
        _write_exclusive(
            directory / "attachment-contract.json", _json_bytes(attachment), "attachment", 3, root
        ),
        _write_exclusive(
            directory / "transport-plan.json", _json_bytes(transport), "transport", 4, root
        ),
        _write_exclusive(directory / "task-envelope.json", _json_bytes(handoff), "handoff", 5, root),
        _write_exclusive(
            directory / "lifecycle-context.json", _json_bytes(context), "context", 6, root
        ),
    ]

    committed_at = datetime.now(timezone.utc)
    expiry = datetime.fromisoformat(attachment["expires_at_utc"].replace("Z", "+00:00"))
    if committed_at >= expiry:
        raise ValueError("attachment became stale before manifest commit")
    manifest: dict[str, Any] = {
        "schema_version": "1.0.0",
        "record_type": "lifecycle_witness",
        "status": "COMMITTED",
        "lifecycle_id": lifecycle_id,
        "created_at_utc": _utc_text(committed_at),
        "repository_root": str(root),
        "canonical_body": context["canonical_body"],
        "selected_organ": selected_organ,
        "host_authorization_id": access["access_point_id"],
        "host_approval_id": approval["approval_id"],
        "access_point": access_identity,
        "host_approval_record": approval_identity,
        "decision_id": decision["decision_id"],
        "decision_sha256": decision_sha256,
        "node_source_sha256": source_sha256,
        "attachment_contract_id": attachment["attachment_contract_id"],
        "attachment_epoch": attachment["attachment_epoch"],
        "attachment_expires_at_utc": attachment["expires_at_utc"],
        "transport_plan_id": transport["transport_plan_id"],
        "envelope_id": handoff["envelope_id"],
        "governing_documents": governing,
        "artifacts": artifacts,
        "manifest_written_last": True,
    }
    validate_json_schema(manifest, _load_schema(root))
    manifest_path = directory / MANIFEST_NAME
    manifest_identity = _write_exclusive(
        manifest_path, _json_bytes(manifest), "manifest", len(artifacts) + 1, root
    )
    newest_constituent = max((root / item["path"]).stat().st_mtime_ns for item in artifacts)
    if manifest_path.stat().st_mtime_ns <= newest_constituent:
        os.utime(
            manifest_path,
            ns=(manifest_path.stat().st_atime_ns, newest_constituent + 1),
        )
    result = verify_lifecycle_witness(directory, repository_root=root)
    return {
        "lifecycle_id": lifecycle_id,
        "directory": str(directory.relative_to(root)),
        "manifest_path": str(manifest_path.relative_to(root)),
        "manifest": manifest_identity,
        "artifacts": artifacts,
        "verification": result,
    }


def verify_lifecycle_witness(
    lifecycle_directory: str | Path,
    *,
    repository_root: str | Path | None = None,
    used_lifecycle_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Independently recover and verify a committed lifecycle witness."""

    directory = Path(lifecycle_directory).resolve()
    manifest_path = directory / MANIFEST_NAME
    if not manifest_path.is_file():
        raise ValueError("lifecycle witness is incomplete: final manifest is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    root = Path(repository_root or manifest["repository_root"]).resolve()
    validate_json_schema(manifest, _load_schema(root))
    if manifest["repository_root"] != str(root):
        raise ValueError("lifecycle witness repository body mismatch")
    if directory.parent != (root / "orchestrator" / "output").resolve():
        raise ValueError("lifecycle witness is outside orchestrator/output")
    root_stat = root.stat()
    body = manifest["canonical_body"]
    if body != {"path": str(root), "device": root_stat.st_dev, "inode": root_stat.st_ino}:
        raise ValueError("lifecycle witness canonical body identity mismatch")
    if used_lifecycle_ids is not None and manifest["lifecycle_id"] in used_lifecycle_ids:
        raise ValueError("lifecycle witness has already been used")

    artifacts = manifest["artifacts"]
    if len(artifacts) != len(CONSTITUENT_ROLES):
        raise ValueError("witness constituent cardinality is invalid")
    role_names = [artifact.get("role") for artifact in artifacts]
    artifact_paths = [artifact.get("path") for artifact in artifacts]
    orders = [artifact.get("write_order") for artifact in artifacts]
    if len(set(role_names)) != len(role_names):
        raise ValueError("witness constituent roles are not unique")
    if len(set(artifact_paths)) != len(artifact_paths):
        raise ValueError("witness constituent paths are not unique")
    if set(role_names) != CONSTITUENT_ROLES or orders != list(
        range(1, len(CONSTITUENT_ROLES) + 1)
    ):
        raise ValueError("witness constituent roles or write order are invalid")

    roles: dict[str, Path] = {}
    constituent_mtimes: list[int] = []
    directory_relative = str(directory.relative_to(root))
    for artifact in artifacts:
        path = (root / artifact["path"]).resolve()
        if path.parent != directory:
            raise ValueError("witness artifact escaped its append-only lifecycle directory")
        expected_path = f"{directory_relative}/{CONSTITUENT_FILENAMES[artifact['role']]}"
        if artifact["path"] != expected_path:
            raise ValueError("witness constituent role is bound to the wrong path")
        if not path.is_file():
            raise ValueError(f"witness artifact is unrecoverable: {artifact['path']}")
        data = path.read_bytes()
        if len(data) != artifact["byte_count"] or _sha256(data) != artifact["sha256"]:
            raise ValueError(f"witness artifact was overwritten: {artifact['path']}")
        roles[artifact["role"]] = path
        constituent_mtimes.append(path.stat().st_mtime_ns)
    if not manifest["manifest_written_last"]:
        raise ValueError("witness manifest does not declare final commit")
    if constituent_mtimes and manifest_path.stat().st_mtime_ns <= max(constituent_mtimes):
        raise ValueError("witness manifest was not written last")

    decision_bytes = roles["decision"].read_bytes()
    decision = json.loads(decision_bytes.decode("utf-8"))
    validate_decision(decision)
    if decision["decision_id"] != manifest["decision_id"]:
        raise ValueError("recovered decision identity mismatch")
    if _sha256(decision_bytes) != manifest["decision_sha256"]:
        raise ValueError("recovered decision bytes mismatch")
    source_bytes = roles["node_source"].read_bytes()
    _assert_node_source(source_bytes)
    if _sha256(source_bytes) != manifest["node_source_sha256"]:
        raise ValueError("recovered node source bytes mismatch")
    source_identity = decision.get("source_node_identity")
    if not isinstance(source_identity, dict) or (
        len(source_bytes) != source_identity.get("byte_count")
        or _sha256(source_bytes) != source_identity.get("sha256")
        or source_identity.get("authority") != "NONE"
        or source_identity.get("executable") is not False
    ):
        raise ValueError("recovered node source differs from the decision binding")

    attachment = json.loads(roles["attachment"].read_text(encoding="utf-8"))
    transport = json.loads(roles["transport"].read_text(encoding="utf-8"))
    handoff = json.loads(roles["handoff"].read_text(encoding="utf-8"))
    context = json.loads(roles["context"].read_text(encoding="utf-8"))
    validate_attachment_contract(attachment)
    validate_transport_plan(transport)
    validate_envelope(handoff)
    committed = datetime.fromisoformat(manifest["created_at_utc"].replace("Z", "+00:00"))
    verdict = B_ATTACHMENT_TRIBUNAL(decision, attachment, transport, now=committed)
    if not verdict["accepted"]:
        raise ValueError("recovered bridge evidence is invalid: " + "; ".join(verdict["issues"]))
    if handoff.get("attachment_contract_id") != attachment["attachment_contract_id"]:
        raise ValueError("handoff attachment identity mismatch")
    if handoff.get("attachment_contract_sha256") != sha256_json(attachment):
        raise ValueError("recovered attachment does not match the original handoff hash")
    if handoff.get("transport_plan_id") != transport["transport_plan_id"]:
        raise ValueError("handoff transport identity mismatch")
    if handoff.get("transport_plan_sha256") != sha256_json(transport):
        raise ValueError("recovered transport does not match the original handoff hash")

    expected_directory = (
        f"{attachment['attachment_epoch']}__{manifest['lifecycle_id']}__lifecycle-witness"
    )
    if directory.name != expected_directory:
        raise ValueError("lifecycle directory identity mismatch")
    seed_context = copy.deepcopy(context)
    seed_context.pop("lifecycle_id", None)
    seed_context.pop("lifecycle_directory", None)
    lifecycle_seed = {
        "context": seed_context,
        "attachment": attachment,
        "transport": transport,
        "handoff_id": handoff["envelope_id"],
    }
    expected_lifecycle_id = "LCW-" + _sha256(
        canonical_json(lifecycle_seed).encode("utf-8")
    )[:24]
    if manifest["lifecycle_id"] != expected_lifecycle_id:
        raise ValueError("lifecycle ID is not bound to its exact context")
    cross_refs = {
        "lifecycle_id": manifest["lifecycle_id"],
        "attachment_contract_id": manifest["attachment_contract_id"],
        "transport_plan_id": manifest["transport_plan_id"],
        "envelope_id": manifest["envelope_id"],
    }
    for field, expected in cross_refs.items():
        if context.get(field) != expected:
            raise ValueError(f"lifecycle context {field} mismatch")
    if context.get("lifecycle_directory") != directory_relative:
        raise ValueError("lifecycle context directory mismatch")
    if context.get("selected_organ") != manifest["selected_organ"]:
        raise ValueError("lifecycle context selected organ mismatch")
    if context.get("canonical_body") != manifest["canonical_body"]:
        raise ValueError("lifecycle context canonical body mismatch")
    if context.get("attachment_epoch") != manifest["attachment_epoch"]:
        raise ValueError("lifecycle context attachment epoch mismatch")
    if manifest["attachment_epoch"] != attachment["attachment_epoch"]:
        raise ValueError("manifest attachment epoch mismatch")
    if context["decision"]["decision_id"] != manifest["decision_id"]:
        raise ValueError("lifecycle context decision_id mismatch")
    if context["decision"]["sha256"] != manifest["decision_sha256"]:
        raise ValueError("lifecycle context decision bytes mismatch")
    if context["node_source"]["sha256"] != manifest["node_source_sha256"]:
        raise ValueError("lifecycle context node source mismatch")
    if context["host_authorization_id"] != manifest["host_authorization_id"]:
        raise ValueError("lifecycle context Host authorization mismatch")
    if context["host_approval_id"] != manifest["host_approval_id"]:
        raise ValueError("lifecycle context Host approval mismatch")
    if context.get("access_point") != manifest["access_point"]:
        raise ValueError("lifecycle context access-point binding mismatch")
    if context.get("host_approval_record") != manifest["host_approval_record"]:
        raise ValueError("lifecycle context Host approval record mismatch")
    if attachment["attachment_contract_id"] != manifest["attachment_contract_id"]:
        raise ValueError("attachment identity mismatch")
    if transport["transport_plan_id"] != manifest["transport_plan_id"]:
        raise ValueError("transport identity mismatch")
    if handoff["envelope_id"] != manifest["envelope_id"]:
        raise ValueError("handoff identity mismatch")
    governing = manifest["governing_documents"]
    if set(governing) != set(GOVERNING_DOCUMENTS):
        raise ValueError("governing document set is not exact and complete")
    if context.get("governing_documents") != governing:
        raise ValueError("lifecycle context governing documents mismatch")
    for name in GOVERNING_DOCUMENTS:
        identity = governing[name]
        if identity.get("path") != name:
            raise ValueError("governing document path is not canonical")
        live = root / identity["path"]
        actual = _relative_identity(live, root)
        if actual != identity:
            raise ValueError(f"governing document identity mismatch: {identity['path']}")

    decision_reference = root / context["decision"]["source_path"]
    access_point_file = root / manifest["access_point"]["path"]
    approval_file = root / manifest["host_approval_record"]["path"]
    if decision_reference.read_bytes() != decision_bytes:
        raise ValueError("recovered decision differs from its protected source")
    _, _, access_identity, approval_identity = _validate_authorization(
        root,
        decision_reference,
        decision_bytes,
        decision,
        access_point_file,
        approval_file,
        manifest["selected_organ"],
    )
    if access_identity != manifest["access_point"]:
        raise ValueError("manifest access-point identity mismatch")
    if approval_identity != manifest["host_approval_record"]:
        raise ValueError("manifest Host approval identity mismatch")
    if manifest["host_authorization_id"] != access_identity["access_point_id"]:
        raise ValueError("manifest Host authorization is not decision-bound")
    if manifest["host_approval_id"] != approval_identity["approval_id"]:
        raise ValueError("manifest Host approval is not decision-bound")

    for other_path in _existing_manifests(directory.parent):
        if other_path.resolve() == manifest_path:
            continue
        other = json.loads(other_path.read_text(encoding="utf-8"))
        if other.get("lifecycle_id") == manifest["lifecycle_id"]:
            raise ValueError("lifecycle identity was reused")
        if other.get("attachment_contract_id") == manifest["attachment_contract_id"]:
            raise ValueError("attachment evidence was reused")
    return {
        "status": "VALID",
        "lifecycle_id": manifest["lifecycle_id"],
        "artifact_count": len(manifest["artifacts"]),
        "manifest_written_last": True,
    }
