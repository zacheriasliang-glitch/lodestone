"""Prepare one exact native string for learned support scoring; never score it.

The caller pins a complete parent and selects a complete stored string value.
Native record validators recheck retention/history; original source paths are
not opened. Neither byte custody nor a historical review establishes semantic
reliability, current applicability, or permission to promote a SEED.
"""
from __future__ import annotations

import json

from .fold_export import _retained, regular_bytes
from .loom_context import SCHEMA as CONTEXT_SCHEMA, compact_context, validate_context
from .loom_nli import MAX_TOKENS, validate_pair
from .loom_text import MAX_DOCUMENT_BYTES, _document, sha
from .records import RecordStore, canonical


SCHEMA = "loom-support-preparation/v1"


def _selector(selector):
    if type(selector) is not dict or set(selector) != {"id", "path"}:
        raise ValueError("Selector requires exactly id and path")
    identifier, path = selector["id"], selector["path"]
    if type(identifier) is not str or not identifier or len(identifier.encode("utf-8")) > 256:
        raise ValueError("Selector id must contain 1..256 UTF-8 bytes")
    if type(path) is not list or len(path) > 16:
        raise ValueError("Selector path must be an explicit list of at most 16 keys/indices")
    for part in path:
        if not ((type(part) is str and 1 <= len(part.encode("utf-8")) <= 256)
                or (type(part) is int and 0 <= part < 2000)):
            raise ValueError("Selector path requires nonempty keys or nonnegative integer indices")
    return json.loads(canonical(selector))


def _string(value, path):
    for part in path:
        if type(value) is dict and type(part) is str and part in value:
            value = value[part]
        elif type(value) is list and type(part) is int and part < len(value):
            value = value[part]
        else:
            raise ValueError("Selector does not resolve within the selected native content")
    if type(value) is not str:
        raise ValueError("Select one complete stored string; structured values cannot be verbalized")
    return value


def prepare_support_request(workspace, parent_bytes, *, expected_parent_sha256,
                            selector, hypothesis, fold_handle=None):
    """Return an inert premise/hypothesis pair with native provenance.

    ``parent_bytes`` is either the complete canonical Origami fold (with its
    explicit ``fold_handle``), or a producer-pinned native compact context.
    ``selector.id`` names a claim or context-entry handle. ``selector.path`` is
    relative to that claim's value or entry's selected_content. NACRE permits
    only ["lesson"]; SEED permits only
    ["held_findings", index, "finding", "detail"]. Codes, hashes, clears_when,
    and other metadata are not substituted for a held finding's description.

    The full native closure stays in the existing workspace; only its identities
    are returned. The exact stored string value is decoded from JSON, not claimed
    to be a raw serialized-file slice. No tokenizer or model is loaded: the final
    complete tokenized pair must still pass the NLI bound before any inference.
    """
    raw, parent = _document({expected_parent_sha256: parent_bytes},
                            expected_parent_sha256, "parent")
    selector = _selector(selector)
    identifier, path = selector["id"], selector["path"]
    if parent.get("schema") == CONTEXT_SCHEMA:
        if fold_handle is not None:
            raise ValueError("A compact context cannot also select an Origami fold handle")
        context = validate_context(parent)
        handles = [entry["handle"] for entry in context["entries"]]
        rebuilt, records = compact_context(workspace, handles, purpose=context["purpose"],
                                           max_bytes=65536)
        if canonical(rebuilt) != raw:
            raise ValueError("Pinned context differs from its complete native records")
        matches = [entry for entry in context["entries"] if entry["handle"] == identifier]
        if len(matches) != 1:
            raise ValueError("Selector id must identify one retained context entry")
        entry = matches[0]
        if entry["kind"] == "NACRE":
            if path != ["lesson"]:
                raise ValueError("NACRE support preparation selects only the complete lesson string")
            evidence = entry["observations"] + [entry["request"], entry["review"]]
        else:
            if not (len(path) == 4 and path[0] == "held_findings"
                    and type(path[1]) is int and path[2:] == ["finding", "detail"]):
                raise ValueError("SEED preparation requires an explicit finding.detail string, not codes or clears_when")
            evidence = [entry["observation"]]
        content = entry["selected_content"]
        parent_kind = "BEZOAR_COMPACT_CONTEXT"
        scope = {"kind": entry["kind"], "state": entry["state"],
                 "review_status": entry["review_status"], "scope": entry["scope"],
                 "unresolved": entry["unresolved"], "purpose": context["purpose"]}
        omitted_payload_fields = entry["omitted_payload_fields"]
    else:
        if fold_handle is None:
            raise ValueError("A complete Origami fold requires its explicit retained fold handle")
        packet, retained_bytes, handles = _retained(workspace, fold_handle, version=2)
        if retained_bytes != raw:
            raise ValueError("Pinned fold differs from its retained Origami record")
        matches = [claim for claim in parent["claims"] if claim["id"] == identifier]
        if len(matches) != 1:
            raise ValueError("Selector id must identify one retained Origami claim")
        claim = matches[0]
        content, evidence = claim["value"], claim["support"]
        parent_kind = "ORIGAMI_RETAINED_FOLD"
        scope = {"basis": claim["basis"], "scope": claim["scope"], "epoch": claim["epoch"],
                 "attribution": packet["attribution"], "decision": parent["decision"],
                 "coverage_assumptions": parent["coverage_assumptions"],
                 "unresolved": parent["unresolved"]}
        omitted_payload_fields = []
        store = RecordStore(workspace)
        records = {}
        for handle in handles:
            record_bytes = regular_bytes(store.path(handle), MAX_DOCUMENT_BYTES)
            if sha(record_bytes) != handle.split(":", 1)[1]:
                raise ValueError("Complete native record changed during preparation")
            records[handle] = record_bytes
    premise = _string(content, path)
    premise_raw, hypothesis_raw = validate_pair(premise, hypothesis)
    identities = [{"handle": handle, "sha256": sha(data), "bytes": len(data)}
                  for handle, data in sorted(records.items())]
    if sum(item["bytes"] for item in identities) > MAX_DOCUMENT_BYTES:
        raise ValueError("Complete native record closure exceeds 4 MiB preparation bound")
    return {"schema": SCHEMA, "status": "PREPARED_NOT_SCORED",
            "premise": premise, "hypothesis": hypothesis,
            "provenance": {"parent_kind": parent_kind,
                           "parent_sha256": expected_parent_sha256, "parent_bytes": len(raw),
                           "fold_handle": fold_handle, "selector": selector,
                           "evidence_ids": list(evidence), "native_scope": scope,
                           "premise_sha256": sha(premise_raw), "premise_bytes": len(premise_raw),
                           "hypothesis_sha256": sha(hypothesis_raw), "hypothesis_bytes": len(hypothesis_raw),
                           "complete_records": identities,
                           "retention": "EXISTING_NATIVE_RECORD_CLOSURE_UNMODIFIED",
                           "string_identity": "COMPLETE_STORED_STRING_VALUE_NOT_RAW_JSON_FILE_SLICE"},
            "omitted_scope": {"selection": "ONLY_THE_EXPLICIT_COMPLETE_STRING_IS_MODEL_INPUT",
                              "other_fields_entries_claims_and_evidence": "NOT_INCLUDED_IN_PREMISE",
                              "parent_omitted_payload_fields": omitted_payload_fields,
                              "original_source_documents": "NOT_REOPENED"},
            "token_validation": {"status": "NOT_PERFORMED", "complete_pair_limit": MAX_TOKENS,
                                 "required_before_inference": True, "truncation": "FORBIDDEN"},
            "semantic_reliability": "NOT_ESTABLISHED", "current_source_freshness": "NOT_EVALUATED",
            "authority": "NONE", "activation": "NONE", "seed_promotion": "NONE"}
