"""Inert routing of freshly recomputed discrepancies to existing HELD nodes.

This is a deterministic, conservative consumer of retained reconciliation.
The caller supplies the hash of its current complete captured input. Both
records are recomputed. Version 1 could refer a finding to a registry node
only when the current and prior records were the same content-addressed
record. Version 2 keeps that path and adds per-item reuse across distinct
captures for one source-proven local finding family: findings whose truth
depends only on their own receipt bytes, the pinned verifier and the complete
registry. Every other law stays review-required across distinct captures.

Nothing here creates a review, asserts reviewer approval, suppresses a
finding, clears a node or dispatches an action. Current means the
caller-declared captured bytes, not a claim about an external repository now.
"""
from __future__ import annotations

from pathlib import Path
import stat

from . import bezoar_reconciliation as reconciliation
from .bezoar_registry import subject_of
from .records import HANDLE, canonical


SCHEMA = "bezoar-holder-routing/v2"
KIND = "holder-routing"
# IDENTITY details that the verifier derives from the receipt's own name and
# text alone (yggdrasil.read_lifecycle). The join-position detail depends on
# other receipts and is deliberately absent.
LOCAL_IDENTITY_DETAILS = frozenset((
    "receipt id is not RCP-<UTC-basic-timestamp>-<12 lowercase hex>",
    "declared receipt_id does not match the filename",
))
ITEM_REUSE = {
    "supported": "Y2_ONE_ORGAN with any detail; IDENTITY with the two receipt-local details",
    "dependency_key": "law, subject path, complete finding, subject file sha256 and byte count, "
                      "complete docs/bezoar.md sha256, reconciliation implementation pins",
    "not_supported": "UNREAD, Y1_ONE_TRUNK, Y3_CLOSED_PREFIX, Y4_ONE_HEAD, joins and other IDENTITY "
                     "details depend on other receipts and require review across distinct captures",
}


def implementation():
    pins = reconciliation.implementation()
    path = reconciliation.plain(Path(__file__).resolve())
    info = path.stat()
    reconciliation.require(stat.S_ISREG(info.st_mode)
                           and info.st_size <= reconciliation.MAX_RECORD_BYTES,
                           "Bounded routing source required")
    raw = path.read_bytes()
    reconciliation.require(len(raw) == info.st_size, "Routing source changed")
    return {"reconciliation": pins, "bezoar_routing.py": reconciliation.digest(raw)}


def _handle(value):
    reconciliation.require(type(value) is str and HANDLE.fullmatch(value)
                           and value.startswith("bezoar:"), "Exact Bezoar handle required")
    return value


def local_item_key(finding, files, registry_sha256, pins):
    """Dependency key of a receipt-local finding, or None when the finding's
    truth can depend on other receipts and must not be reused across captures."""
    law = finding.get("law")
    if law == "Y2_ONE_ORGAN":
        pass
    elif law == "IDENTITY" and finding.get("detail") in LOCAL_IDENTITY_DETAILS:
        pass
    else:
        return None
    subject = subject_of(finding)
    record = files.get(subject) if type(files) is dict else None
    if (type(record) is not dict or type(record.get("text")) is not str
            or type(record.get("sha256")) is not str):
        return None
    return reconciliation.digest(canonical({
        "schema": SCHEMA, "law": law, "subject": subject, "finding": finding,
        "subject_sha256": record["sha256"],
        "subject_bytes": len(record["text"].encode("utf-8")),
        "registry_sha256": registry_sha256, "implementation": pins}))


def _read_evidence(store, handle, scratch):
    try:
        checked = reconciliation.read_reconciliation(store, handle, scratch=scratch)
        source = store.read(checked["source"])["payload"]
        value, _, _ = reconciliation.capsule(source["raw_utf8"].encode("utf-8"), source["sha256"])
        report = checked["report"]
        return ({"handle": handle, "status": "RECOMPUTED", "source": checked["source"],
                 "input_sha256": report["input_sha256"],
                 "registry_sha256": value["files"]["docs/bezoar.md"]["sha256"],
                 "report_sha256": reconciliation.digest(canonical(report)),
                 "implementation": report["implementation"]}, report, value["files"])
    except (OSError, ValueError) as exc:
        # Unavailable, corrupted, old-version or changed evidence never supplies
        # a holder. Do not retain private host paths from exception messages.
        return {"handle": handle, "status": "UNVERIFIED", "error_type": type(exc).__name__}, None, None


def _items(report, files, registry_sha256):
    """Local item keys of a recomputed report, and every (law, subject) pair it carries."""
    keys, pairs = {}, set()
    for index, finding in enumerate(report["lineage"]["findings"]):
        pairs.add((finding["law"], subject_of(finding)))
        key = local_item_key(finding, files, registry_sha256, report["implementation"])
        if key is not None:
            keys.setdefault(key, []).append(index)
    return keys, pairs


def _build(store, current, *, expected_source_sha256, previous, scratch):
    _handle(current)
    if previous is not None:
        _handle(previous)
    reconciliation.require(type(expected_source_sha256) is str
                           and reconciliation.SHA.fullmatch(expected_source_sha256),
                           "Exact external current-source hash required")
    root, workspace = reconciliation.plain(scratch), reconciliation.plain(store.workspace)
    reconciliation.require(root != workspace and root not in workspace.parents
                           and workspace not in root.parents, "Scratch and records must be separate")
    reconciliation.require(not root.exists() and root.parent.is_dir(), "Fresh routing scratch required")
    identity = implementation()
    root.mkdir(mode=0o700)
    current_evidence, report, files = _read_evidence(store, current, root / "current")
    if previous is None:
        previous_evidence, prior, prior_files = None, None, None
    elif previous == current:
        # One immutable content-addressed record needs one fresh computation.
        # This is exact record reuse, not a second independent observation.
        previous_evidence, prior, prior_files = dict(current_evidence), report, files
    else:
        previous_evidence, prior, prior_files = _read_evidence(store, previous, root / "previous")

    if report is None:
        reason = "CURRENT_UNVERIFIED"
    elif report["input_sha256"] != expected_source_sha256:
        reason = "CURRENT_SOURCE_MISMATCH"
    elif previous is None:
        reason = "NO_PREVIOUS_EVIDENCE"
    elif prior is None:
        reason = "PREVIOUS_UNVERIFIED"
    elif report["registry_verdict"] != "PASS":
        reason = "REGISTRY_REQUIRES_REVIEW"
    elif report == prior:
        reason = "EXACT_UNCHANGED_EVIDENCE"
    elif prior["registry_verdict"] != "PASS":
        reason = "PRIOR_REGISTRY_REQUIRES_REVIEW"
    else:
        reason = "DISTINCT_EVIDENCE_ITEM_COMPARISON"

    prior_keys, prior_pairs = {}, set()
    if reason == "DISTINCT_EVIDENCE_ITEM_COMPARISON":
        prior_keys, prior_pairs = _items(prior, prior_files, previous_evidence["registry_sha256"])

    routes = []
    if report is not None:
        # Retain every full finding, including repetitions and detail text.
        for index, finding in enumerate(report["lineage"]["findings"]):
            subject = subject_of(finding)
            matches = [item for item in report["holder_matches"]
                       if item["law"] == finding["law"] and item["subject"] == subject]
            holders = matches[0]["holders"] if len(matches) == 1 else []
            route_reason, key, prior_indexes = reason, None, []
            if reason == "EXACT_UNCHANGED_EVIDENCE":
                prior_indexes = [index]
            elif reason == "DISTINCT_EVIDENCE_ITEM_COMPARISON":
                key = local_item_key(finding, files, current_evidence["registry_sha256"],
                                     report["implementation"])
                if key is None:
                    route_reason = "UNSUPPORTED_LAW_FOR_ITEM_REUSE"
                elif key in prior_keys:
                    route_reason, prior_indexes = "EXACT_UNCHANGED_ITEM", list(prior_keys[key])
                elif (finding["law"], subject) in prior_pairs:
                    route_reason = "ITEM_DEPENDENCY_CHANGED"
                else:
                    route_reason = "NO_PRIOR_ITEM"
            if route_reason in ("EXACT_UNCHANGED_EVIDENCE", "EXACT_UNCHANGED_ITEM"):
                if len(matches) != 1 or len(holders) != 1:
                    route_reason = "NO_UNIQUE_HOLDER"
                elif holders[0]["kind"] != "SEED" or holders[0]["state"] != "HELD":
                    route_reason = "HOLDER_NOT_SEED_HELD"
            eligible = route_reason in ("EXACT_UNCHANGED_EVIDENCE", "EXACT_UNCHANGED_ITEM")
            routes.append({"index": index, "finding": finding,
                           "finding_sha256": reconciliation.digest(canonical(finding)),
                           "subject": subject, "holder_matches": matches,
                           "item_key": key, "prior_finding_indexes": prior_indexes if eligible else [],
                           "decision": "REFER_EXISTING_HELD_NODE" if eligible else "REVIEW_REQUIRED",
                           "reason": route_reason,
                           "target": {"registry_path": "docs/bezoar.md",
                                      "registry_sha256": current_evidence["registry_sha256"],
                                      "node_id": holders[0]["id"], "kind": "SEED", "state": "HELD"}
                           if eligible else None})

    referred = sum(1 for item in routes if item["decision"] == "REFER_EXISTING_HELD_NODE")
    if report is None or reason not in ("EXACT_UNCHANGED_EVIDENCE", "DISTINCT_EVIDENCE_ITEM_COMPARISON"):
        status = "REVIEW_REQUIRED"
    elif not routes:
        status = "NO_FINDINGS"
    elif referred == len(routes):
        status = "EXISTING_HELD_REFERENCES_PROPOSED"
    elif referred:
        status = "PARTIAL_HELD_REFERENCES_PROPOSED"
    else:
        status = "REVIEW_REQUIRED"
    payload = {"schema": SCHEMA, "implementation": identity,
               "current": current, "previous": previous,
               "expected_source_sha256": expected_source_sha256,
               "current_evidence": current_evidence, "previous_evidence": previous_evidence,
               "status": status, "reason": reason, "routes": routes,
               "summary": {"findings": len(routes), "referred": referred,
                           "review_required": len(routes) - referred},
               "item_reuse": ITEM_REUSE,
               "registry_findings": report["registry_findings"] if report is not None else [],
               "finding_coverage": "FULL_RECOMPUTED_CURRENT_FINDINGS" if report is not None else "UNAVAILABLE",
               "evidence_scope": "Caller-declared captured bytes; no assertion of current external state",
               "authority": "NONE", "activation": "NONE", "dispatch": "PROPOSAL_ONLY",
               "repair_performed": False, "registry_states_changed": False, "learning": False}
    links = sorted({item["handle"] for item in (current_evidence, previous_evidence)
                    if item is not None and item["status"] == "RECOMPUTED"})
    reconciliation.require(implementation() == identity, "Routing implementation changed")
    record = {"schema": "organ-record/v1", "owner": "bezoar", "kind": KIND,
              "payload": payload, "links": links}
    reconciliation.require(len(canonical(record)) <= reconciliation.MAX_RECORD_BYTES,
                           "Complete routing record exceeds bound; no truncation permitted")
    return payload, links


def publish_route(store, current, *, expected_source_sha256, previous=None, scratch):
    """Publish an inert proposal, or a retained requirement for review."""
    payload, links = _build(store, current, expected_source_sha256=expected_source_sha256,
                            previous=previous, scratch=scratch)
    return store.publish("bezoar", KIND, payload, links=links)


def read_route(store, handle, *, scratch):
    """Verify a saved proposal through fresh recomputation, not byte checks alone."""
    _handle(handle)
    reconciliation.require(store.path(handle).stat().st_size <= reconciliation.MAX_RECORD_BYTES,
                           "Routing record exceeds bound")
    record = store.read(handle)
    payload = record["payload"]
    reconciliation.require(record["owner"] == "bezoar" and record["kind"] == KIND
                           and payload.get("schema") == SCHEMA
                           and payload.get("implementation") == implementation(),
                           "Routing type or computation version differs")
    reconciliation.require({"current", "previous", "expected_source_sha256"} <= set(payload),
                           "Routing request fields missing")
    expected, links = _build(store, payload["current"],
                             expected_source_sha256=payload["expected_source_sha256"],
                             previous=payload["previous"], scratch=scratch)
    reconciliation.require(payload == expected and record["links"] == links,
                           "Stored routing differs from recomputed evidence")
    return {"handle": handle, "proposal": expected,
            "verification": "RECOMPUTED_FROM_RETAINED_BYTES",
            "authority": "NONE", "activation": "NONE", "dispatch": "PROPOSAL_ONLY"}
