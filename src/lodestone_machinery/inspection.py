"""Consumers for source handles, pressure results, residuals, and typed records.

Reports expose recorded work and changed evidence. They grant no authority and
do not establish semantic correctness or independent review.
"""
from pathlib import Path
import hashlib
import json

from .organs import expand, pressure
from .nodes import NodeStore
from .pressure_pair import run_pair


def inspect_job(engine, job_id: str) -> dict:
    """Read a completed job and re-open its captured source handles.

    Input: an Engine and existing job id. Output: current passages, mechanical
    fault-detection results, prediction differences and actionable findings.
    Original job state and artifacts are never repaired or reclassified here.
    """
    saved = engine.status(job_id)
    findings = [f for f in engine.maintain()["findings"]
                if f.get("job_id") in (None, job_id)]
    result = {"job_id": job_id, "recorded_state": saved["state"],
              "passages": [], "pressure": None, "prediction": None,
              "findings": findings, "verdict": "ATTENTION"}
    view = next((a for a in saved["artifacts"] if a["name"] == "folded.json"), None)
    if view is None:
        findings.append({"kind": "SOURCE_VIEW_UNAVAILABLE"})
        return result
    try:
        data = engine._path("blobs/" + view["sha256"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != view["sha256"]:
            raise ValueError("Captured source-view blob is corrupt")
        folded = json.loads(data)
        root = Path(saved["envelope"]["root"])
        engine._source_boundary(root, saved["envelope"]["paths"])
        for handle in folded["passages"]:
            try:
                current = expand(root, handle)
                result["passages"].append(current)
                if not current["fresh"]:
                    findings.append({"kind": "PASSAGE_DRIFT", "source": handle["source"],
                                     "start_line": handle["start_line"]})
            except (OSError, ValueError) as exc:
                findings.append({"kind": "PASSAGE_UNAVAILABLE", "source": handle["source"],
                                 "detail": str(exc)[:500]})
        checked = pressure(root, folded)
        result["pressure"] = checked
        if checked["baseline_verdict"] != "PASS":
            findings.append({"kind": "BASELINE_NOT_CONFIRMED", "verdict": checked["baseline_verdict"]})
        for case in checked["undetected"]:
            findings.append({"kind": "UNDETECTED_FAULT", "case": case})
    except (OSError, ValueError, KeyError, TypeError) as exc:
        findings.append({"kind": "SOURCE_VIEW_UNAVAILABLE", "detail": str(exc)[:500]})
    if saved["result"] is not None:
        residual = saved["result"]["residue"]
        result["prediction"] = residual
        for difference in residual["differences"]:
            findings.append({"kind": "PREDICTION_MISMATCH", **difference})
        for missing in residual["missing_values"]:
            findings.append({"kind": "PREDICTION_INCOMPLETE", **missing})
    result["verdict"] = "ATTENTION" if findings else "OK"
    return result


def export_nodes(store: NodeStore, claim_ids: list[str]) -> dict:
    """Export explicitly selected current records and their incident edges.

    Missing ids and relations leaving the selected set remain visible. Export
    preserves caller-supplied status separately from freshness; it infers none.
    """
    if (type(claim_ids) is not list or not 1 <= len(claim_ids) <= 100
            or any(type(i) is not str or not i for i in claim_ids)
            or len(set(claim_ids)) != len(claim_ids)):
        raise ValueError("Supply 1–100 distinct nonempty claim ids")
    records, missing, edges = [], [], {}
    for identifier in sorted(claim_ids):
        record = store.get(identifier)
        if record is None:
            missing.append(identifier)
            continue
        records.append(record)
        for edge in store.neighbors(identifier):
            key = (edge["source_id"], edge["target_id"], edge["relation"])
            edges[key] = edge
    included = {r["id"] for r in records}
    outside = sorted({e[k] for e in edges.values() for k in ("source_id", "target_id")
                      if e[k] not in included})
    return {"records": records, "relations": [edges[k] for k in sorted(edges)],
            "missing_ids": missing, "external_ids": outside,
            "review_ids": [r["id"] for r in records if r["freshness"] == "NEEDS_REVIEW"],
            "verdict": "ATTENTION" if missing or any(r["freshness"] == "NEEDS_REVIEW" for r in records) else "OK"}


def pair_packet(board: dict, black_provider, white_provider) -> dict:
    """Turn actual paired exchanges into a review handoff with retained content.

    The consumer routes incomplete exchanges to provider repair, differing
    responses to comparison, and matching responses to content review. None of
    these routes means approval. Providers remain explicit trusted callables.
    """
    exchanges = run_pair(board, black_provider, white_provider)
    gaps = [{"phase": phase, "role": role, "error": item.get("error")}
            for phase in ("initial", "cross_read")
            for role, item in exchanges[phase].items() if item["status"] != "RETURNED"]
    next_action = ("RESTORE_PROVIDER" if gaps else
                   "COMPARE_RESPONSES" if exchanges["response_differences"] else "REVIEW_CONTENT")
    return {"next_action": next_action, "gaps": gaps,
            "responses": {phase: exchanges[phase] for phase in ("initial", "cross_read")},
            "differences": exchanges["response_differences"], "board": exchanges["board"],
            "authority": "NONE", "semantic_verdict": "UNRESOLVED",
            "transport_status": exchanges["status"]}
