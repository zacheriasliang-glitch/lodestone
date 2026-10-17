"""B_RETURN: verify returned evidence against the canonical requested body."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from engine.contracts import validate_json_schema
from engine.kernel20_phenotype import (
    audit_finding_mismatches,
    audit_finding_source_state,
    parse_return_audit_finding,
)

from .contracts import (
    ReturnStatus,
    is_within,
    sha256_json,
    validate_attachment_contract,
    validate_envelope,
    validate_transport_plan,
)


_SCALAR_FIELDS = (
    "decision_id",
    "attachment_contract_id",
    "attachment_epoch",
    "observed_root",
    "host_approval_obtained",
)


def _scalar(lines: list[str], field: str) -> str | None:
    pattern = re.compile(rf"^\s*(?:[-*]\s*)?{re.escape(field)}\s*:\s*(.*?)\s*$", re.IGNORECASE)
    for line in lines:
        match = pattern.match(line)
        if match:
            return match.group(1)
    return None


def _section(lines: list[str], name: str) -> list[str]:
    heading = re.compile(rf"^\s*{re.escape(name)}\s*:\s*$", re.IGNORECASE)
    values: list[str] = []
    active = False
    for line in lines:
        if heading.match(line):
            active = True
            continue
        if not active:
            continue
        bullet = re.match(r"^\s*[-*]\s+(.+?)\s*$", line)
        if bullet:
            values.append(bullet.group(1))
            continue
        if not line.strip():
            if values:
                break
            continue
        if re.match(r"^\s*[A-Za-z0-9_.-]+\s*:\s*", line):
            break
    return values


def parse_return_evidence(receipt_text: str, receipt_path: str) -> dict[str, Any]:
    lines = receipt_text.splitlines()
    scalars = {field: _scalar(lines, field) for field in _SCALAR_FIELDS}
    hashes: dict[str, str] = {}
    for item in _section(lines, "output_hashes"):
        match = re.match(r"(.+?)\s*(?:=|\|)\s*([0-9a-fA-F]{64})$", item)
        if match:
            hashes[match.group(1).strip()] = match.group(2).lower()
    approval = (scalars["host_approval_obtained"] or "false").lower() == "true"
    evidence: dict[str, Any] = {
        "schema_version": "1.0.0",
        "organ": "B_RETURN",
        "requested_root": "",
        "observed_root": scalars["observed_root"] or "",
        "receipt_path": receipt_path,
        "decision_id": scalars["decision_id"] or "",
        "attachment_contract_id": scalars["attachment_contract_id"] or "",
        "attachment_epoch": scalars["attachment_epoch"] or "",
        "observed_outputs": _section(lines, "observed_outputs"),
        "output_hashes": hashes,
        "host_approval_obtained": approval,
    }
    audit_finding = parse_return_audit_finding(receipt_text)
    if audit_finding is not None:
        evidence["audit_finding"] = audit_finding
    return evidence


def _result(status: str, issues: list[str], evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "organ": "B_RETURN",
        "status": status,
        "issues": issues,
        "evidence": evidence or {},
    }


def outer_custody_mismatches(
    envelope: dict[str, Any],
    attachment: dict[str, Any],
    transport_plan: dict[str, Any],
) -> list[str]:
    """Bind an envelope's outer custody to an independently persisted carrier.

    ``validate_envelope()`` establishes internal consistency only: every outer
    field can be replaced and the envelope ID recomputed. Custody identity is
    therefore checked against the attachment contract and transport plan that
    were persisted at preflight, which the envelope's holder cannot rewrite.
    """

    issues: list[str] = []
    try:
        validate_attachment_contract(attachment)
    except (KeyError, TypeError, ValueError) as error:
        issues.append(f"trusted attachment contract is invalid: {error}")
    try:
        validate_transport_plan(transport_plan)
    except (KeyError, TypeError, ValueError) as error:
        issues.append(f"trusted transport plan is invalid: {error}")
    if issues:
        return issues

    if sha256_json(attachment) != envelope.get("attachment_contract_sha256"):
        issues.append("envelope attachment contract hash differs from the trusted carrier")
    if attachment["attachment_contract_id"] != envelope.get("attachment_contract_id"):
        issues.append("envelope attachment contract ID differs from the trusted carrier")
    if attachment["attachment_epoch"] != envelope.get("attachment_epoch"):
        issues.append("envelope attachment epoch differs from the trusted carrier")
    if attachment["expires_at_utc"] != envelope.get("attachment_expires_at_utc"):
        issues.append("envelope attachment expiry differs from the trusted carrier")
    if attachment["requested_root"] != envelope.get("requested_root"):
        issues.append("envelope requested body differs from the trusted carrier")

    if sha256_json(transport_plan) != envelope.get("transport_plan_sha256"):
        issues.append("envelope transport plan hash differs from the trusted carrier")
    if transport_plan["transport_plan_id"] != envelope.get("transport_plan_id"):
        issues.append("envelope transport plan ID differs from the trusted carrier")
    if transport_plan["attachment_contract_id"] != attachment["attachment_contract_id"]:
        issues.append("trusted transport plan references a different attachment contract")
    if transport_plan["delivery_root"] != envelope.get("delivery_root"):
        issues.append("envelope delivery root differs from the trusted carrier")
    if transport_plan["return_root"] != envelope.get("return_root"):
        issues.append("envelope return root differs from the trusted carrier")
    if transport_plan["target_files"] != envelope.get("target_files"):
        issues.append("envelope target files differ from the trusted carrier")
    if transport_plan["protected_files"] != envelope.get("protected_files"):
        issues.append("envelope protected files differ from the trusted carrier")
    if transport_plan["stop_condition"] != envelope.get("stop_condition"):
        issues.append("envelope stop condition differs from the trusted carrier")

    decision = envelope.get("kernel20_decision")
    if not isinstance(decision, dict) or decision.get("decision_id") != envelope.get(
        "kernel20_decision_id"
    ):
        issues.append("envelope decision identity differs from its own payload")
    elif transport_plan["target_files"] != decision.get("target_files"):
        issues.append("trusted transport plan was issued for a different decision")
    return issues


def B_RETURN(
    envelope: dict[str, Any],
    receipt_path: str | Path,
    *,
    observed_root: str | Path,
    attachment: dict[str, Any] | None = None,
    transport_plan: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Verify returned evidence against the canonical requested body.

    ``attachment`` and ``transport_plan`` are the independently persisted Bridge
    carrier for this lifecycle. Without them outer custody cannot be established
    and the return fails closed; it is never ``COMPLETE``.
    """

    try:
        validate_envelope(envelope)
    except (KeyError, TypeError, ValueError) as error:
        return _result(ReturnStatus.MISMATCH.value, [str(error)])

    requested = Path(envelope["requested_root"]).resolve()
    observed = Path(observed_root).resolve()
    receipt = Path(receipt_path).resolve()
    receipt_root = requested / "receipts"
    if not (observed == requested or is_within(observed, requested)):
        return _result(ReturnStatus.WRONG_BODY.value, ["verification did not run in the requested body"])
    if not is_within(receipt, receipt_root):
        return _result(ReturnStatus.WRONG_BODY.value, ["receipt is outside requested_root/receipts"])
    if not receipt.is_file():
        return _result(ReturnStatus.UNKNOWN.value, ["receipt does not exist"])

    try:
        evidence = parse_return_evidence(receipt.read_text(encoding="utf-8"), str(receipt))
    except OSError as error:
        return _result(ReturnStatus.UNKNOWN.value, [str(error)])
    except (UnicodeError, ValueError) as error:
        return _result(ReturnStatus.MISMATCH.value, [f"invalid return evidence: {error}"])
    evidence["requested_root"] = str(requested)
    if not evidence["observed_root"]:
        evidence["observed_root"] = str(observed)
    elif Path(evidence["observed_root"]).resolve() != requested:
        return _result(ReturnStatus.WRONG_BODY.value, ["receipt declares a different observed body"], evidence)

    try:
        schema = json.loads(
            (requested / "orchestrator" / "schemas" / "return_evidence.schema.json").read_text(
                encoding="utf-8"
            )
        )
        validate_json_schema(evidence, schema)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        return _result(
            ReturnStatus.MISMATCH.value,
            [f"return evidence schema validation failed: {error}"],
            evidence,
        )

    if attachment is None or transport_plan is None:
        return _result(
            ReturnStatus.UNKNOWN.value,
            [
                "outer custody cannot be verified without the trusted attachment "
                "contract and transport plan"
            ],
            evidence,
        )
    custody_issues = outer_custody_mismatches(envelope, attachment, transport_plan)
    if custody_issues:
        return _result(ReturnStatus.MISMATCH.value, custody_issues, evidence)

    current = now or datetime.now(timezone.utc)
    expiry = datetime.fromisoformat(envelope["attachment_expires_at_utc"].replace("Z", "+00:00"))
    if current >= expiry or evidence["attachment_epoch"] != envelope["attachment_epoch"]:
        return _result(ReturnStatus.STALE.value, ["attachment epoch is missing, changed, or expired"], evidence)

    owned_finding = envelope["kernel20_decision"].get("audit_finding")
    mismatches = audit_finding_mismatches(owned_finding, evidence.get("audit_finding"))
    # An exact echo of the decision's old projection is not evidence that the
    # raw source still says the same thing. Recheck the current bytes.
    mismatches.extend(audit_finding_source_state(requested, owned_finding)[0])
    if evidence["decision_id"] != envelope["kernel20_decision_id"]:
        mismatches.append("Kernel 20 decision ID mismatch")
    if evidence["attachment_contract_id"] != envelope["attachment_contract_id"]:
        mismatches.append("attachment contract ID mismatch")
    if sha256_json(envelope["kernel20_decision"]) != envelope["kernel20_payload_sha256"]:
        mismatches.append("Kernel 20 payload hash mismatch")
    if mismatches:
        return _result(ReturnStatus.MISMATCH.value, mismatches, evidence)

    missing_outputs = [
        output for output in envelope["predicted_outputs"]
        if output not in evidence["observed_outputs"]
    ]
    hash_issues: list[str] = []
    for relative, declared_hash in evidence["output_hashes"].items():
        output_path = (requested / relative).resolve()
        if not is_within(output_path, requested) or not output_path.is_file():
            hash_issues.append(f"declared output is absent or outside requested body: {relative}")
            continue
        observed_hash = hashlib.sha256(output_path.read_bytes()).hexdigest()
        if observed_hash != declared_hash:
            hash_issues.append(f"output hash mismatch: {relative}")
    if missing_outputs or hash_issues:
        return _result(
            ReturnStatus.AUDIT_REQUIRED.value,
            [*(f"missing predicted output: {item}" for item in missing_outputs), *hash_issues],
            evidence,
        )
    if envelope["approval_requirement"]["required"] and not evidence["host_approval_obtained"]:
        return _result(
            ReturnStatus.HOST_APPROVAL_REQUIRED.value,
            ["return evidence does not contain explicit host approval"],
            evidence,
        )
    return _result(ReturnStatus.COMPLETE.value, [], evidence)
