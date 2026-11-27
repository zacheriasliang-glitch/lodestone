"""Retain machine structural findings separately from human correction advice.

Only explicit selection of a definite, source-bound rejection produces voxel
advice. No policy is inferred, no response is changed, and no model is called.
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from . import decision_contract as checker
from .records import canonical
from .loom_voxels import CONTEXT_SCHEMA, render_voxel_sections
from lodestone_nurarihyon import learning
from lodestone_nurarihyon.portable_files import read_bytes

SCHEMA = "loom-machine-decision-assessment/v1"
KIND = "MACHINE_STRUCTURAL_FINDING"
AUTHOR = "Loom source-bound structural reviewer/v1"
DEFINITE_CODES = frozenset(("INVALID_RESPONSE_FORMAT", "CASE_ID_MISMATCH", "ACTION_NOT_ALLOWED",
    "TRANSITION_INCONSISTENT", "UNKNOWN_EVIDENCE_ID", "UNVERIFIED_EVIDENCE",
    "EVIDENCE_TARGET_MISMATCH", "EVIDENCE_NOT_LATEST"))


def _version():
    return {"decision_contract": hashlib.sha256(read_bytes(Path(checker.__file__), 1024 * 1024)).hexdigest(),
            "decision_assessment": hashlib.sha256(read_bytes(Path(__file__), 1024 * 1024)).hexdigest()}


def _reusable(assessment):
    return (assessment["status"] == "REJECTED" and assessment["selected_evidence_id"] is not None
            and bool(DEFINITE_CODES.intersection(assessment["reason_codes"])))


def _compact(assessment):
    # Verbatim response and encounter remain in the linked native records.
    return {k: v for k, v in assessment.items() if k not in ("raw_response", "parsed_response")}


def _check(memory, outcome_handle, contract):
    outcome = memory.read_outcome(outcome_handle)
    prepared = memory.read_prepared(outcome["run_handle"])
    contract, case = checker._checked_contract(prepared["binding"]["encounter"], contract)
    if contract["policy"] is not None:
        raise ValueError("Automatic structural findings cannot supply an action policy")
    assessment = checker.review_decision(prepared["binding"]["encounter"], outcome["response"],
        contract, completed=outcome["status"] == "COMPLETED")
    return outcome, case, assessment


def assess(memory, outcome_handle, contract):
    """Persist one idempotent assessment as evidence, never as a teacher review."""
    contract = checker._copy_json(contract, checker.MAX_CONTRACT_BYTES)
    with learning.locked(memory, timeout=30):
        outcome, _, assessment = _check(memory, outcome_handle, contract)
        body = {"schema": SCHEMA, "kind": KIND, "reviewer": AUTHOR,
            "checker_version": _version(), "contract": contract,
            "assessment": _compact(assessment), "outcome_handle": outcome_handle,
            "run_handle": outcome["run_handle"], "native_status": outcome["status"],
            "surprise_bits": None, "timing": "POST_OUTCOME_STRUCTURAL_CHECK",
            "authority": "NONE", "semantic_learning": "NOT_ESTABLISHED", "weight_update": False}
        text = canonical(body).decode()
        source = "loom-machine-decision:" + outcome_handle
        event = hashlib.sha256(text.encode()).hexdigest()
        observed_at = None
        for handle in memory._catalog("evidence"):
            old = memory.read_evidence(handle)
            if (old["source"], old["event_id"]) == (source, event):
                observed_at = old["observed_at"]
                break
        handle = memory.evidence({"source": source, "event_id": event,
            "observed_at": observed_at or datetime.now(timezone.utc).isoformat(),
            "text": text, "attribution": AUTHOR + "; machine structural finding, not a human correction"})
    return {**body, "evidence_handle": handle, "retention": "RETAINED_MACHINE_EVIDENCE",
            "advice_delivery": "EXPLICIT_VOXEL_SELECTION_ONLY" if _reusable(assessment)
            else "NOT_ELIGIBLE", "provider_called": False}


def read(memory, handle, *, for_delivery=False):
    """Read inspectable source bindings; delivery also verifies the actual finding."""
    record = memory.read_evidence(handle)
    body = json.loads(record["text"])
    if (type(body) is not dict or body.get("schema") != SCHEMA or body.get("kind") != KIND
            or body.get("reviewer") != AUTHOR
            or record["source"] != "loom-machine-decision:" + body.get("outcome_handle", "")
            or record["event_id"] != hashlib.sha256(canonical(body)).hexdigest()):
        raise ValueError("Not a retained machine structural finding")
    outcome = memory.read_outcome(body["outcome_handle"])
    prepared = memory.read_prepared(outcome["run_handle"])
    contract, _ = checker._checked_contract(prepared["binding"]["encounter"], body["contract"])
    assessment = body["assessment"]
    if (body["run_handle"] != outcome["run_handle"] or body["native_status"] != outcome["status"]
            or assessment["contract_sha256"] != contract["contract_sha256"]
            or assessment["response_sha256"] != outcome["response_sha256"]):
        raise ValueError("Machine assessment differs from the retained source or outcome")
    if for_delivery:
        if body["checker_version"] != _version():
            raise ValueError("Structural reviewer version changed; reassess before selecting its advice")
        _, _, actual = _check(memory, body["outcome_handle"], contract)
        if _compact(actual) != assessment or not _reusable(actual):
            raise ValueError("Only definite source-bound structural rejections may become named knots")
    return {**body, "evidence_handle": handle}


def for_outcome(memory, outcome_handle):
    """Reopen saved assessments without recomputing or changing their verdicts."""
    rows = []
    for handle in memory._catalog("evidence"):
        record = memory.read_evidence(handle)
        if record["source"] == "loom-machine-decision:" + outcome_handle:
            rows.append(read(memory, handle))
    return rows


def knot_voxel(memory, handles):
    """Explicit, bounded machine findings enter the existing voxel prompt route."""
    if (type(handles) is not list or not 1 <= len(handles) <= 16
            or any(type(h) is not str for h in handles) or len(set(handles)) != len(handles)):
        raise ValueError("Select 1..16 distinct retained machine finding handles")
    lines = []
    with learning.locked(memory, timeout=30):
        for handle in handles:
            row = read(memory, handle, for_delivery=True)
            outcome, case, assessment = _check(memory, row["outcome_handle"], row["contract"])
            try:
                answer, _, _ = checker._answer(outcome["response"], case)
            except ValueError:
                answer = None
            # These are contract conventions, not a post-hoc invented model bet.
            expected = {"target": case["target"], "case_id": case["case_id"],
                "evidence_id": assessment["selected_evidence_id"],
                "transition_rule": "HOLD iff chosen action equals recorded prior action",
                "recorded_prior_action": case["recorded_prior_action"],
                "action": "UNASSESSED; no condition/action policy inferred"}
            came = {"evidence_ids": answer["evidence_ids"] if answer else None,
                "action": answer["action"] if answer else None,
                "transition": answer["transition"] if answer else None,
                "case_id": answer["case_id"] if answer else None}
            line = ("Machine structural finding (" + AUTHOR + "; " + handle + "). "
                "Expected under the explicitly selected case contract: " + canonical(expected).decode()
                + ". What came: " + canonical(came).decode()
                + ". Definite failures: " + ", ".join(key for key in assessment["reason_codes"]
                    if key in DEFINITE_CODES)
                + ". Post-outcome finding; surprise unknown. The original response is retained. "
                  "These conventions apply only to matching structured tasks; no action advice for another case.")
            lines.append(line)
    voxel = canonical({"schema": CONTEXT_SCHEMA,
        "teaching": "Selected machine structural findings, not human corrections or proof of learning. "
                    "Recheck applicability to the current case; do not copy a prior answer.",
        "named_knots": lines, "shoulds": []}).decode()
    render_voxel_sections(voxel, "Validate selected structural findings")
    return voxel
