"""Bounded Bezoar context for Loom; selected content stays explicitly scoped.

No model summarizes these records. Native HELD routing and reviewed NACRE are
validated, selected fields are copied exactly, omissions are listed, and their
complete immutable record closure is retained separately in Yggdrasil.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

from .bezoar_lessons import ATTRIBUTION, LAYER_SCOPE, _date, _layer, finding_identity, read_lesson
from .crystals import CrystalStore
from .fold_export import plain_path, regular_bytes
from .records import HANDLE, RecordStore, canonical

SCHEMA = "loom-bezoar-context/v1"
MAX_CONTEXT_BYTES = 65536
MAX_RECORDS = 256
MAX_ARCHIVE_BYTES = 4 * 1024 * 1024
MAX_ARCHIVE_FILE_BYTES = 16 * 1024 * 1024
COMPACTION = ("Exact selected_content fields, no prose rewriting; omitted_payload_fields lists "
              "fields not copied into selected_content, even when separately surfaced as provenance")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def _closure(store, handles):
    pending, records, total = list(handles), {}, 0
    while pending:
        handle = pending.pop()
        if handle in records:
            continue
        if len(records) >= MAX_RECORDS:
            raise ValueError("Context closure exceeds 256 records")
        raw = regular_bytes(store.path(handle), 262144)
        record = store.read(handle)
        if canonical(record) != raw:
            raise ValueError("Context record changed during read")
        total += len(raw)
        if total > MAX_ARCHIVE_BYTES:
            raise ValueError("Complete context closure exceeds 4 MiB")
        records[handle] = raw
        pending.extend(record["links"])
    return records


def compact_context(workspace, handles, *, purpose, max_bytes=16384):
    """Return compact view plus full record bytes; no records or sources changed.

    Native routing is the runtime representation of HELD SEED findings. NACRE is
    accepted only through its existing historical evidence/review validator.
    No free-text condition extraction, truth label or SEED promotion is inferred.
    """
    root = plain_path(workspace, directory=True)
    if not (root / "organs").is_dir():
        raise ValueError("Existing organ workspace required")
    if (type(handles) is not list or not 1 <= len(handles) <= 16
            or any(type(h) is not str for h in handles) or len(set(handles)) != len(handles)):
        raise ValueError("Select 1..16 distinct explicit record handles")
    if type(purpose) is not str or not purpose.strip() or len(purpose.encode()) > 2048:
        raise ValueError("Purpose must contain 1..2048 UTF-8 bytes")
    if type(max_bytes) is not int or not 256 <= max_bytes <= MAX_CONTEXT_BYTES:
        raise ValueError("Context budget must be 256..65536 bytes")
    store = RecordStore(root)
    records = _closure(store, handles)
    entries = []
    for handle in handles:
        record = store.read(handle)
        payload = record["payload"]
        if (record["owner"], record["kind"]) == ("bezoar", "routing"):
            layer, _ = _layer(store, handle)
            selected = {"held_findings": payload["held_findings"]}
            entry = {"kind": "SEED", "state": "HELD", "review_status": "UNRESOLVED",
                     "selected_content": selected, "observation": layer["observation"],
                     "observed_at": layer["observed_at"],
                     "scope": "Retained discrepancy; not a reviewed lesson or a training target",
                     "unresolved": ["Findings remain HELD; a prediction cannot clear them."]}
            copied = {"held_findings"}
        elif (record["owner"], record["kind"]) == ("bezoar", "nacre"):
            checked = read_lesson(store, handle)
            copied = {"lesson", "finding", "layer_scope", "seeds_remain_held", "attribution"}
            entry = {"kind": "NACRE", "state": "LAYERED", "review_status": "REVIEWED_PASS",
                     "selected_content": {key: checked[key] for key in sorted(copied)},
                     "request": checked["request"], "review": checked["review"],
                     "observations": [x["observation"] for x in checked["layers"]],
                     "scope": "Historical review for the exact source and finding; not general truth",
                     "unresolved": ["Current source freshness and applicability to this purpose are not evaluated."]}
        elif record["owner"] == "bezoar" and record["kind"] in ("learning-seed", "learning-nacre"):
            from .bezoar_learning import LearningMemory
            memory = LearningMemory(root)
            native = (memory.read_seed(handle) if record["kind"] == "learning-seed"
                      else memory.read_nacre(handle))
            copied = set(native)
            entry = {"kind": native["kind"], "state": native["state"],
                     "review_status": "UNRESOLVED", "selected_content": native,
                     "native_learning_kind": record["kind"],
                     "scope": "Complete retained teaching candidate or recurring pattern; per-run preparation must bind its applicability and outcomes",
                     "unresolved": ["A retained correction or repeated pattern is not demonstrated learning."]}
        else:
            raise ValueError("Select native Bezoar HELD routing, reviewed NACRE, or learning SEED/NACRE records")
        entry.update(handle=handle, record_sha256=sha(records[handle]),
                     omitted_payload_fields=sorted(set(payload) - copied),
                     complete_record_source_id="loom:record:" + handle,
                     authority="NONE", training_label="NOT_DERIVED_FROM_RECORD_STATE")
        entries.append(entry)
    context = {"schema": SCHEMA, "purpose": purpose, "entries": entries,
               "compaction": COMPACTION,
               "original_record_count": len(records), "original_record_bytes": sum(map(len, records.values())),
               "current_source_freshness": "NOT_EVALUATED", "authority": "NONE", "activation": "NONE"}
    context = validate_context(context)
    data = canonical(context)
    if len(data) > max_bytes:
        raise ValueError(f"Complete selected context needs {len(data)} bytes; budget is {max_bytes}; select fewer records or increase the explicit budget")
    # Recheck the complete snapshot before returning a coherent retained view.
    if any(regular_bytes(store.path(handle), 262144) != raw for handle, raw in records.items()):
        raise ValueError("Context evidence changed during compaction")
    return context, records


def pack_context(workspace, handles, destination, *, purpose, max_bytes=16384):
    """Publish compact JSON and a separate full-record Yggdrasil archive."""
    dest = plain_path(destination, new=True)
    context, records = compact_context(workspace, handles, purpose=purpose, max_bytes=max_bytes)
    data = canonical(context)
    dest.mkdir(mode=0o700)
    archive_path = dest / "originals.sqlite"
    # Exact get-by-ID retention needs no lexical index or its extra storage.
    with CrystalStore(archive_path, index_mode="scan") as archive:
        for handle, raw in sorted(records.items()):
            archive.put("loom:record:" + handle, raw,
                        {"kind": "loom_full_context_record", "record_handle": handle, "sha256": sha(raw)},
                        strategy="zlib", report=False)
        archive.put("loom:context:" + sha(data), data,
                    {"kind": "loom_compact_context", "sha256": sha(data)}, strategy="zlib", report=False)
    archive_before = regular_bytes(archive_path, MAX_ARCHIVE_FILE_BYTES)
    with CrystalStore(archive_path, read_only=True) as archive:
        for handle, raw in sorted(records.items()):
            if archive.get("loom:record:" + handle) != raw:
                raise ValueError("Full context record failed Yggdrasil readback")
        if archive.get("loom:context:" + sha(data)) != data:
            raise ValueError("Compact context failed Yggdrasil readback")
        if archive.inspect()["sources"] != len(records) + 1:
            raise ValueError("Unexpected context archive inventory")
    if regular_bytes(archive_path, MAX_ARCHIVE_FILE_BYTES) != archive_before:
        raise ValueError("Context archive changed during readback")
    with (dest / "context.json").open("xb") as stream:
        stream.write(data)
    receipt = {"schema": "loom-context-receipt/v1", "context_sha256": sha(data),
               "context_bytes": len(data), "record_count": len(records),
               "full_record_bytes": sum(map(len, records.values())), "selected_handles": list(handles),
               "record_manifest": [{"handle": handle, "source_id": "loom:record:" + handle,
                                    "sha256": sha(raw), "bytes": len(raw)}
                                   for handle, raw in sorted(records.items())],
               "archive_sha256": sha(archive_before), "archive_bytes": len(archive_before),
               "archive_readback": "ALL_RECORDS_AND_CONTEXT_VERIFIED",
               "retention": "Full native explicit-link record closure retained; original source documents not reopened",
               "consumer_input": "context.json only; originals.sqlite is controller-side provenance",
               "semantic_savings": "NOT_MEASURED", "authority": "NONE"}
    with (dest / "receipt.json").open("xb") as stream:
        stream.write(canonical(receipt))
    return receipt


def load_context(path, *, expected_sha256):
    if (type(expected_sha256) is not str or len(expected_sha256) != 64
            or any(c not in "0123456789abcdef" for c in expected_sha256)):
        raise ValueError("Expected a lowercase SHA-256 context pin")
    raw = regular_bytes(path, MAX_CONTEXT_BYTES)
    if sha(raw) != expected_sha256:
        raise ValueError("Compact context differs from the caller's pinned digest")
    context = json.loads(raw)
    if canonical(context) != raw:
        raise ValueError("Unsupported or noncanonical compact context")
    return validate_context(context)


def validate_context(context):
    """Copy a bounded in-memory projection and validate its declared contract.

    This checks structure and inert states. It does not authenticate an arbitrary
    supplied dictionary against the historical records; use a producer-pinned
    digest via load_context when consuming a saved projection.
    """
    try:
        raw = canonical(context)
    except (TypeError, OverflowError, RecursionError) as exc:
        raise ValueError("Compact context must be bounded JSON") from exc
    if len(raw) > MAX_CONTEXT_BYTES:
        raise ValueError("Compact context exceeds byte bound")
    context = json.loads(raw)
    required = {"schema", "purpose", "entries", "compaction", "original_record_count",
                "original_record_bytes", "current_source_freshness", "authority", "activation"}
    if (type(context) is not dict or set(context) != required
            or context.get("schema") != SCHEMA):
        raise ValueError("Unsupported or noncanonical compact context")
    if context.get("authority") != "NONE" or context.get("activation") != "NONE":
        raise ValueError("Compact context cannot authorize execution")
    if type(context.get("entries")) is not list or not 1 <= len(context["entries"]) <= 16:
        raise ValueError("Invalid compact context entries")
    if (type(context["purpose"]) is not str or not context["purpose"].strip()
            or len(context["purpose"].encode()) > 2048 or context["compaction"] != COMPACTION
            or context["current_source_freshness"] != "NOT_EVALUATED"
            or type(context["original_record_count"]) is not int
            or not len(context["entries"]) <= context["original_record_count"] <= MAX_RECORDS
            or type(context["original_record_bytes"]) is not int
            or not 1 <= context["original_record_bytes"] <= MAX_ARCHIVE_BYTES):
        raise ValueError("Invalid compact context purpose, provenance or bounds")
    seen = set()
    common = {"kind", "state", "review_status", "selected_content", "scope", "unresolved", "handle",
              "record_sha256", "omitted_payload_fields", "complete_record_source_id", "authority",
              "training_label"}
    for entry in context["entries"]:
        if (type(entry) is not dict or
                (entry.get("kind"), entry.get("state"), entry.get("review_status")) not in
                (("SEED", "HELD", "UNRESOLVED"), ("NACRE", "LAYERED", "REVIEWED_PASS"),
                 ("SEED", "CANDIDATE", "UNRESOLVED"), ("NACRE", "LAYERED", "UNRESOLVED"))
                or entry.get("training_label") != "NOT_DERIVED_FROM_RECORD_STATE"
                or entry.get("authority") != "NONE"):
            raise ValueError("Invalid context state or implicit training label")
        native_learning = "native_learning_kind" in entry
        variant = ({"native_learning_kind"} if native_learning else
                   {"observation", "observed_at"} if entry["kind"] == "SEED" else {"request", "review", "observations"})
        handle = entry.get("handle")
        if (set(entry) != common | variant or type(handle) is not str
                or not HANDLE.fullmatch(handle) or not handle.startswith("bezoar:") or handle in seen
                or entry["record_sha256"] != handle.split(":")[1]
                or entry["complete_record_source_id"] != "loom:record:" + handle
                or type(entry["selected_content"]) is not dict
                or type(entry["scope"]) is not str or not entry["scope"].strip()
                or type(entry["unresolved"]) is not list or not entry["unresolved"]
                or any(type(s) is not str or not s.strip() for s in entry["unresolved"])
                or type(entry["omitted_payload_fields"]) is not list
                or any(type(s) is not str or not s for s in entry["omitted_payload_fields"])
                or entry["omitted_payload_fields"] != sorted(set(entry["omitted_payload_fields"]))):
            raise ValueError("Invalid context entry provenance or projection")
        seen.add(handle)
        selected = entry["selected_content"]
        if native_learning:
            kind = "learning-seed" if entry["kind"] == "SEED" else "learning-nacre"
            required_native = {"schema", "authority", "kind", "state", "id", "conditions", "evidence_handles", "author"}
            required_native |= {"correction", "frame_limits"} if entry["kind"] == "SEED" else {"pattern", "seed_handles"}
            allowed_native = required_native | ({"supersedes"} if entry["kind"] == "SEED" else set())
            if (entry["native_learning_kind"] != kind or entry["review_status"] != "UNRESOLVED"
                    or not required_native <= set(selected) <= allowed_native
                    or selected["schema"] != "bezoar-" + kind + "/v1"
                    or selected["kind"] != entry["kind"] or selected["state"] != entry["state"]
                    or selected["authority"] != "NONE" or entry["omitted_payload_fields"]):
                raise ValueError("Invalid complete native learning projection")
            for field in ("id", "author", "correction" if entry["kind"] == "SEED" else "pattern"):
                if type(selected[field]) is not str or not selected[field].strip():
                    raise ValueError("Native learning projection has no usable correction or pattern")
            list_fields = ["conditions", "evidence_handles"]
            list_fields += ["frame_limits"] if entry["kind"] == "SEED" else ["seed_handles"]
            for field in list_fields:
                if (type(selected[field]) is not list or not selected[field]
                        or any(type(item) is not str or not item.strip() for item in selected[field])):
                    raise ValueError("Native learning projection lost conditions or provenance")
            for field in ("evidence_handles",) + (("seed_handles",) if entry["kind"] == "NACRE" else ()):
                if any(not HANDLE.fullmatch(h) for h in selected[field]):
                    raise ValueError("Native learning projection has invalid evidence handles")
        elif entry["kind"] == "SEED":
            findings = selected.get("held_findings")
            if (set(selected) != {"held_findings"} or type(findings) is not list or not findings
                    or any(type(f) is not dict or f.get("state") != "HELD" for f in findings)
                    or type(entry["observation"]) is not str or not HANDLE.fullmatch(entry["observation"])
                    or not entry["observation"].startswith("watcher:")
                    or type(entry["observed_at"]) is not str or not entry["observed_at"].strip()):
                raise ValueError("Invalid HELD context projection")
            _date(entry["observed_at"])
        else:
            if (set(selected) != {"lesson", "finding", "layer_scope", "seeds_remain_held", "attribution"}
                    or selected["seeds_remain_held"] is not True
                    or any(type(selected[k]) is not str or not selected[k].strip()
                           for k in ("lesson", "layer_scope", "attribution"))
                    or type(selected["finding"]) is not dict
                    or type(entry["observations"]) is not list or not 2 <= len(entry["observations"]) <= 16
                    or any(type(h) is not str or not HANDLE.fullmatch(h)
                           for h in [entry["request"], entry["review"], *entry["observations"]])
                    or not entry["request"].startswith("bezoar:") or not entry["review"].startswith("bezoar:")
                    or any(not h.startswith("watcher:") for h in entry["observations"])
                    or selected["attribution"] != ATTRIBUTION or selected["layer_scope"] != LAYER_SCOPE
                    or len(set(entry["observations"])) != len(entry["observations"])):
                raise ValueError("Invalid reviewed NACRE context projection")
            if canonical(finding_identity(selected["finding"])) != canonical(selected["finding"]):
                raise ValueError("NACRE finding differs from its source identity")
    return context


def main(argv=None):
    parser = argparse.ArgumentParser(prog="bezoar compact-context")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--handle", action="append", required=True)
    parser.add_argument("--purpose", required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--max-bytes", type=int, default=16384)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(pack_context(args.workspace, args.handle, args.destination,
                                     purpose=args.purpose, max_bytes=args.max_bytes), ensure_ascii=False))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)[:1000]}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
