#!/usr/bin/env python3
"""Origami re-entry fold: a saved, source-linked navigation aid for one Lab work item.

Commands: ``orient``, ``expand``, ``refresh``, ``render``, ``verify``, ``rebind``.

The fold is derived data. It points at repository records with exact handles
(repository-relative path, whole-file SHA-256, 1-based LF line range, slice
SHA-256 and a short verbatim quote). It is not an authority over its sources
and never generates replacement evidence: ``expand`` prints the bytes that are
on disk now and says whether they still match what was bound.

Locator convention ``line-bytes-v1``: every LF byte ends its segment; bytes
after the final LF form one unterminated segment. This is the partition used by
``experiments/003-roundtrip-text-segmenter/segmenter.py`` (``segment_bytes``);
it is restated here rather than imported because that file is protected and
lives inside an experiment.

Source gate order mirrors ``orchestrator/engine/kernel20_phenotype.py``
``audit_finding_source_state`` (relative path, inside root, no symlink,
present, readable, byte count, SHA-256). That function is not imported because
its admissibility rule is specific to audit-finding reports and would reject
legitimate fold sources such as the receipt policy.

Standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

SCHEMA_NAME = "origami-fold"
SCHEMA_VERSION = "1.0.0"
LOCATOR_CONVENTION = "line-bytes-v1"

CLAIM_STATUSES = ("ESTABLISHED", "PROPOSAL", "UNKNOWN", "UNRESOLVED")
OPEN_KINDS = ("HOST_INPUT_REQUIRED", "UNKNOWN", "CONTRADICTION_UNRESOLVED", "RESTRICTION")
LAYERS = ("EX", "AP")
BASES = ("recorded", "interpretation")
SCOPE_KINDS = ("current", "historical")
RELATION_TYPES = {
    "supersedes": "temporal",
    "accepts": "authority",
    "disposes": "authority",
    "defines": "authority",
    "authorizes": "authority",
    "does_not_authorize": "authority",
    "requires_next": "operational",
    "records_completion_of": "provenance",
    "verifies": "provenance",
    "contradicts": "semantic",
    "depends_on": "operational",
}
TRIGGERS = ("state_index_changed", "new_host_disposition", "new_origami_record", "new_receipt")
FRESH = "FRESH"
NEEDS_REVIEW = "NEEDS_REVIEW"
MISSING_SOURCE = "MISSING_SOURCE"

RECEIPT_RE = re.compile(r"^\d{8}T\d{6}Z__.+__RCP-.+\.md$")
HANDLE_RE = re.compile(r"^([A-Za-z0-9_]+):L(\d+)(?:-(\d+))?$")
HEADER_RE = re.compile(r"^([a-z_]+): (.*)$")

DEFAULT_FOLD_REL = "tools/origami-fold/examples/demo/fold.json"


class FoldError(Exception):
    """Raised for malformed folds, bad handles, or refused writes."""


# --------------------------------------------------------------------------
# exact byte helpers
# --------------------------------------------------------------------------

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def utc_basic(iso: str) -> str:
    """ISO ``2026-09-10T23:59:59Z`` -> basic ``20260910T235959Z``."""
    return iso.replace("-", "").replace(":", "")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def segment_lines(data: bytes) -> list[bytes]:
    """line-bytes-v1 partition: LF-terminated segments plus one unterminated tail."""
    segments: list[bytes] = []
    start = 0
    while True:
        index = data.find(b"\n", start)
        if index < 0:
            break
        segments.append(data[start:index + 1])
        start = index + 1
    if start < len(data):
        segments.append(data[start:])
    return segments


def slice_lines(data: bytes, first: int, last: int) -> tuple[bytes, int, int]:
    """Return (bytes, start_byte, end_byte) of 1-based inclusive lines."""
    segments = segment_lines(data)
    if not (1 <= first <= last <= len(segments)):
        raise FoldError(f"line range L{first}-{last} outside file of {len(segments)} lines")
    start = sum(len(segment) for segment in segments[: first - 1])
    payload = b"".join(segments[first - 1: last])
    return payload, start, start + len(payload)


def lines_covering(data: bytes, start_byte: int, end_byte: int) -> tuple[int, int]:
    """1-based inclusive line range covering byte range [start_byte, end_byte)."""
    first = last = None
    offset = 0
    for number, segment in enumerate(segment_lines(data), 1):
        next_offset = offset + len(segment)
        if first is None and start_byte < next_offset:
            first = number
        if end_byte <= next_offset:
            last = number
            break
        offset = next_offset
    if first is None or last is None:
        raise FoldError("byte range outside file")
    return first, max(first, last)


def handle_string(support: dict) -> str:
    lines = support.get("lines")
    if not lines:
        return support["source"]
    return f"{support['source']}:L{lines[0]}-{lines[1]}"


# --------------------------------------------------------------------------
# repository observation
# --------------------------------------------------------------------------

def default_root() -> Path:
    return Path(__file__).resolve().parents[3]


def git_out(root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def observe_git(root: Path) -> dict:
    inside = git_out(root, "rev-parse", "--is-inside-work-tree")
    if inside != "true":
        return {"available": False, "head": "UNKNOWN", "branch": "UNKNOWN"}
    return {
        "available": True,
        "head": git_out(root, "rev-parse", "HEAD") or "UNKNOWN",
        "branch": git_out(root, "rev-parse", "--abbrev-ref", "HEAD") or "UNKNOWN",
    }


def tracked_in_git(root: Path, relative: str):
    if git_out(root, "rev-parse", "--is-inside-work-tree") != "true":
        return "UNKNOWN"
    return git_out(root, "ls-files", "--error-unmatch", "--", relative) is not None


def listing(root: Path, patterns: list[str]) -> list[str]:
    names: set[str] = set()
    for pattern in patterns:
        for path in root.glob(pattern):
            if path.is_file():
                names.add(path.relative_to(root).as_posix())
    return sorted(names)


def listing_sha256(names: list[str]) -> str:
    return sha256_bytes("\n".join(names).encode("utf-8"))


def receipt_files(root: Path) -> list[str]:
    directory = root / "receipts"
    if not directory.is_dir():
        return []
    return sorted(
        f"receipts/{path.name}" for path in directory.iterdir()
        if path.is_file() and RECEIPT_RE.match(path.name)
    )


def receipt_header(path: Path) -> dict:
    header: dict = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return header
    for line in text.splitlines()[:60]:
        if line.startswith("## "):
            break
        match = HEADER_RE.match(line)
        if match:
            header[match.group(1)] = match.group(2).strip()
    return header


def initiating_prompt_payload(path: Path) -> str | None:
    """Exact payload of the first fenced block after ``## initiating_prompt`` (no final LF)."""
    try:
        lines = path.read_text(encoding="utf-8").split("\n")
    except OSError:
        return None
    try:
        start = lines.index("## initiating_prompt")
    except ValueError:
        return None
    fence_open = None
    for index in range(start + 1, len(lines)):
        if lines[index].startswith("```"):
            fence_open = index
            break
        if lines[index].startswith("## "):
            return None
    if fence_open is None:
        return None
    body: list[str] = []
    for line in lines[fence_open + 1:]:
        if line == "```":
            return "\n".join(body)
        body.append(line)
    return None


# --------------------------------------------------------------------------
# fold I/O and binding
# --------------------------------------------------------------------------

def load_fold(path: Path) -> dict:
    try:
        fold = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise FoldError(f"cannot read fold: {path} ({error})") from error
    except json.JSONDecodeError as error:
        raise FoldError(f"fold is not valid JSON: {path} ({error})") from error
    schema = fold.get("schema", {})
    if schema.get("name") != SCHEMA_NAME or schema.get("version") != SCHEMA_VERSION:
        raise FoldError(f"unsupported fold schema: {schema!r}")
    return fold


def save_fold(path: Path, fold: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fold, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def repository_relative(relative) -> str | None:
    """Return a reason the path is not usable as repository-relative, else None."""
    if not isinstance(relative, str) or not relative.strip():
        return "NOT_REPOSITORY_RELATIVE"
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts:
        return "NOT_REPOSITORY_RELATIVE"
    return None


def source_state(root: Path, source: dict, cache: dict | None = None) -> dict:
    """Fail-closed gate sequence for one source; bytes are cached in ``cache``."""
    state = {"status": None, "checks_performed": 0, "sha256_now": None,
             "byte_count_now": None, "changed": None}
    relative = source.get("path")
    if relative is None:
        state["status"] = "BY_RECEIPT"
        return state
    state["checks_performed"] += 1
    problem = repository_relative(relative)
    if problem:
        state["status"] = problem
        return state
    state["checks_performed"] += 1
    resolved_root = root.resolve()
    probe = resolved_root
    for part in Path(relative).parts:
        probe = probe / part
        if probe.is_symlink():
            state["status"] = "SYMLINK"
            return state
    try:
        resolved = (resolved_root / Path(relative)).resolve()
        resolved.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError):
        state["status"] = "ESCAPES_ROOT"
        return state
    state["checks_performed"] += 1
    if not resolved.is_file():
        state["status"] = "MISSING"
        return state
    state["checks_performed"] += 1
    try:
        data = resolved.read_bytes()
    except OSError:
        state["status"] = "UNREADABLE"
        return state
    state["checks_performed"] += 1
    state["byte_count_now"] = len(data)
    state["checks_performed"] += 1
    state["sha256_now"] = sha256_bytes(data)
    bound = source.get("bound", {})
    state["changed"] = (
        state["sha256_now"] != bound.get("sha256")
        or state["byte_count_now"] != bound.get("byte_count")
    )
    state["status"] = "PRESENT"
    if cache is not None:
        cache[source["id"]] = data
    return state


def bind_source(root: Path, source_id: str, relative: str, **meta) -> dict:
    data = (root / relative).read_bytes()
    entry = {"id": source_id, "path": relative}
    entry.update(meta)
    entry["bound"] = {
        "sha256": sha256_bytes(data),
        "byte_count": len(data),
        "line_count": len(segment_lines(data)),
        "bound_at_utc": utc_now(),
    }
    entry["tracked_in_git_at_binding"] = tracked_in_git(root, relative)
    return entry


def bind_support(data: bytes, source_id: str, first: int, last: int, quote: str | None) -> dict:
    payload, start, end = slice_lines(data, first, last)
    text = payload.decode("utf-8")
    if quote is not None and quote not in text:
        raise FoldError(f"quote not found in {source_id}:L{first}-{last}: {quote!r}")
    return {
        "source": source_id,
        "lines": [first, last],
        "start_byte": start,
        "end_byte": end,
        "slice_sha256": sha256_bytes(payload),
        "quote": quote,
    }


def relocation_candidate(data: bytes, quote: str | None) -> dict:
    if not quote:
        return {"relocation_candidate": None, "quote_occurrences": 0}
    needle = quote.encode("utf-8")
    positions = []
    start = 0
    while True:
        index = data.find(needle, start)
        if index < 0:
            break
        positions.append(index)
        start = index + 1
    if len(positions) == 1:
        first, last = lines_covering(data, positions[0], positions[0] + len(needle))
        return {"relocation_candidate": [first, last], "quote_occurrences": 1}
    return {"relocation_candidate": None, "quote_occurrences": len(positions)}


def passage_state(data: bytes, support: dict) -> dict:
    first, last = support["lines"]
    line_count = len(segment_lines(data))
    if not (1 <= first <= last <= line_count):
        return {"status": "OUT_OF_RANGE", "line_count_now": line_count,
                **relocation_candidate(data, support.get("quote"))}
    payload, start, end = slice_lines(data, first, last)
    digest = sha256_bytes(payload)
    text = payload.decode("utf-8", errors="replace")
    quote = support.get("quote")
    quote_present = quote is None or quote in text
    state = {
        "status": "INTACT" if (digest == support["slice_sha256"] and quote_present) else "CHANGED",
        "start_byte": start,
        "end_byte": end,
        "slice_sha256_now": digest,
        "slice_sha256_bound": support["slice_sha256"],
        "quote_present": quote_present,
        "text": text,
    }
    if state["status"] == "CHANGED":
        state.update(relocation_candidate(data, quote))
    return state


def resolve_by_receipt(root: Path, source: dict) -> dict:
    wanted = source.get("resolution", {}).get("operation_name")
    matches = []
    for relative in receipt_files(root):
        if receipt_header(root / relative).get("operation_name") == wanted:
            matches.append(relative)
    if not matches:
        return {"status": "NOT_FOUND", "path": None, "match": None}
    if len(matches) > 1:
        return {"status": "MULTIPLE", "path": matches, "match": None}
    payload = initiating_prompt_payload(root / matches[0])
    if payload is None:
        return {"status": "RESOLVED", "path": matches[0], "match": None,
                "note": "receipt has no fenced initiating_prompt block"}
    digest = sha256_bytes(payload.encode("utf-8"))
    return {"status": "RESOLVED", "path": matches[0], "embedded_sha256": digest,
            "match": digest == source.get("bound", {}).get("sha256")}


# --------------------------------------------------------------------------
# structural validation
# --------------------------------------------------------------------------

def all_entries(fold: dict) -> list[dict]:
    return list(fold.get("claims", [])) + list(fold.get("relations", [])) + list(fold.get("open_items", []))


def structural_errors(fold: dict) -> list[str]:
    errors: list[str] = []
    for key in ("schema", "work_item", "construction", "sources", "claims", "relations",
                "open_items", "index_dependencies", "recipe"):
        if key not in fold:
            errors.append(f"missing top-level key: {key}")
    if errors:
        return errors
    source_ids = set()
    for source in fold["sources"]:
        if source.get("id") in source_ids:
            errors.append(f"duplicate source id: {source.get('id')}")
        source_ids.add(source.get("id"))
        if source.get("path") is not None and repository_relative(source["path"]):
            errors.append(f"source {source.get('id')}: path not repository-relative")
        if "bound" not in source or "sha256" not in source["bound"]:
            errors.append(f"source {source.get('id')}: no bound identity")
    entry_ids = set()
    for entry in all_entries(fold):
        entry_id = entry.get("id")
        if entry_id in entry_ids:
            errors.append(f"duplicate entry id: {entry_id}")
        entry_ids.add(entry_id)
        if entry.get("basis") not in BASES:
            errors.append(f"{entry_id}: basis must be one of {BASES}")
        for trigger in entry.get("refresh_when", []):
            if trigger not in TRIGGERS:
                errors.append(f"{entry_id}: unknown trigger {trigger}")
        if not entry.get("support"):
            errors.append(f"{entry_id}: no support handles")
        for support in entry.get("support", []):
            if support.get("source") not in source_ids:
                errors.append(f"{entry_id}: unknown source {support.get('source')}")
            elif support.get("lines") is not None:
                lines = support["lines"]
                if not (isinstance(lines, list) and len(lines) == 2 and 1 <= lines[0] <= lines[1]):
                    errors.append(f"{entry_id}: bad line range {lines}")
                if not support.get("slice_sha256"):
                    errors.append(f"{entry_id}: support without slice_sha256")
    for claim in fold["claims"]:
        if claim.get("status") not in CLAIM_STATUSES:
            errors.append(f"{claim.get('id')}: bad status {claim.get('status')}")
        if claim.get("layer") not in LAYERS:
            errors.append(f"{claim.get('id')}: bad layer {claim.get('layer')}")
        if claim.get("scope", {}).get("kind") not in SCOPE_KINDS:
            errors.append(f"{claim.get('id')}: bad scope kind")
    for relation in fold["relations"]:
        if relation.get("type") not in RELATION_TYPES:
            errors.append(f"{relation.get('id')}: unknown relation type {relation.get('type')}")
        for endpoint in (relation.get("from"), relation.get("to")):
            if endpoint not in entry_ids and endpoint not in source_ids and endpoint != fold["work_item"].get("id"):
                errors.append(f"{relation.get('id')}: endpoint {endpoint!r} is not a known entry, source or work item")
    for item in fold["open_items"]:
        if item.get("kind") not in OPEN_KINDS:
            errors.append(f"{item.get('id')}: bad open-item kind {item.get('kind')}")
    return errors


# --------------------------------------------------------------------------
# freshness check (no writes)
# --------------------------------------------------------------------------

def check_fold(root: Path, fold: dict) -> dict:
    cache: dict = {}
    sources = {source["id"]: source for source in fold["sources"]}
    states = {sid: source_state(root, source, cache) for sid, source in sources.items()}
    by_receipt = {sid: resolve_by_receipt(root, source) for sid, source in sources.items()
                  if source.get("path") is None}

    index = fold["index_dependencies"]
    state_source = index["state_index"]["source"]
    receipts_now = receipt_files(root)
    cutoff = index["receipts"]["cutoff_basic_utc"]
    construction_name = fold["construction"]["operation_name"]
    acknowledged = {item["path"] for item in fold.get("acknowledged_receipts", [])}
    source_paths = {source.get("path") for source in fold["sources"]}
    construction_receipt = None
    unfolded = []
    for relative in receipts_now:
        stamp = relative.split("/", 1)[1][:16]
        if stamp <= cutoff or relative in source_paths or relative in acknowledged:
            continue
        header = receipt_header(root / relative)
        if header.get("operation_name") == construction_name and construction_receipt is None:
            construction_receipt = relative
            continue
        unfolded.append({"path": relative, "operation_name": header.get("operation_name", "UNKNOWN")})
    host_now = listing(root, index["host_dispositions"]["patterns"])
    origami_now = listing(root, index["origami_records"]["patterns"])
    fired = {
        "state_index_changed": bool(states[state_source]["changed"]),
        "new_host_disposition": listing_sha256(host_now) != index["host_dispositions"]["bound"]["listing_sha256"],
        "new_origami_record": listing_sha256(origami_now) != index["origami_records"]["bound"]["listing_sha256"],
        "new_receipt": bool(unfolded),
    }
    recipe_files = []
    recipe_changed = False
    for item in fold["recipe"]["files"]:
        path = root / item["path"]
        now = sha256_bytes(path.read_bytes()) if path.is_file() else None
        changed = now != item["sha256"]
        recipe_changed = recipe_changed or changed
        recipe_files.append({"path": item["path"], "bound": item["sha256"], "now": now, "changed": changed})

    entries: dict = {}
    for entry in all_entries(fold):
        reasons: list[str] = []
        notes: list[str] = []
        passages: dict = {}
        for support in entry.get("support", []):
            sid = support["source"]
            source = sources[sid]
            if source.get("path") is None:
                resolution = by_receipt[sid]
                if resolution["status"] == "NOT_FOUND":
                    notes.append(f"authorization_receipt_not_found:{sid}")
                elif resolution["status"] == "MULTIPLE":
                    reasons.append(f"authorization_receipt_ambiguous:{sid}")
                elif resolution.get("match") is False:
                    reasons.append(f"authorization_record_mismatch:{sid}")
                passages[sid] = resolution
                continue
            state = states[sid]
            handle = handle_string(support)
            if state["status"] != "PRESENT":
                reasons.append(f"source_{state['status'].lower()}:{sid}")
                passages[handle] = {"status": state["status"]}
                continue
            passage = passage_state(cache[sid], support)
            passages[handle] = {key: value for key, value in passage.items() if key != "text"}
            if passage["status"] != "INTACT":
                reasons.append(f"passage_{passage['status'].lower()}:{handle}")
                if passage.get("relocation_candidate"):
                    first, last = passage["relocation_candidate"]
                    notes.append(f"relocation_candidate:{sid}:L{first}-{last}")
            elif state["changed"]:
                notes.append(f"source_changed_passage_intact:{sid}")
        for trigger in entry.get("refresh_when", []):
            if fired.get(trigger):
                reasons.append(trigger)
        if recipe_changed:
            reasons.append("recipe_changed")
        if entry.get("freshness", {}).get("pending_review"):
            reasons.append("interpretation_review_required")
        if any(reason.startswith("source_missing") or reason.startswith("source_unreadable")
               for reason in reasons):
            status = MISSING_SOURCE
        elif reasons:
            status = NEEDS_REVIEW
        else:
            status = FRESH
        entries[entry["id"]] = {"status": status, "reasons": reasons, "notes": notes, "passages": passages}

    summary = {status: sum(1 for value in entries.values() if value["status"] == status)
               for status in (FRESH, NEEDS_REVIEW, MISSING_SOURCE)}
    return {
        "checked_at_utc": utc_now(),
        "root": str(root),
        "git": observe_git(root),
        "sources": states,
        "by_receipt": by_receipt,
        "index": {
            "state_index": {"source": state_source, "changed": fired["state_index_changed"]},
            "receipts": {"count": len(receipts_now), "newest": receipts_now[-1] if receipts_now else None,
                          "unfolded": unfolded, "construction_receipt": construction_receipt},
            "host_dispositions": {"names": host_now, "changed": fired["new_host_disposition"]},
            "origami_records": {"names": origami_now, "changed": fired["new_origami_record"]},
        },
        "triggers_fired": fired,
        "recipe": {"changed": recipe_changed, "files": recipe_files},
        "entries": entries,
        "summary": summary,
    }


def verify(root: Path, fold: dict) -> list[str]:
    """Errors when the fold is malformed or no longer matches the repository bytes."""
    errors = structural_errors(fold)
    if errors:
        return errors
    report = check_fold(root, fold)
    for sid, state in report["sources"].items():
        if state["status"] == "BY_RECEIPT":
            resolution = report["by_receipt"][sid]
            if resolution.get("match") is False:
                errors.append(f"source {sid}: embedded directive hash differs from bound")
            continue
        if state["status"] != "PRESENT":
            errors.append(f"source {sid}: {state['status']}")
        elif state["changed"]:
            errors.append(f"source {sid}: current bytes differ from bound identity")
    for entry_id, entry in report["entries"].items():
        for handle, passage in entry["passages"].items():
            if passage.get("status") in ("CHANGED", "OUT_OF_RANGE"):
                errors.append(f"{entry_id}: passage {handle} is {passage['status']}")
    if report["recipe"]["changed"]:
        errors.append("recipe files differ from the recorded recipe hashes")
    return errors


# --------------------------------------------------------------------------
# refresh (dry-run or apply) and rebind
# --------------------------------------------------------------------------

def apply_report(fold: dict, report: dict) -> dict:
    """Update only derived entries: freshness blocks, observed identities, history."""
    updated = json.loads(json.dumps(fold))
    for source in updated["sources"]:
        state = report["sources"][source["id"]]
        source["observed"] = {key: state[key] for key in ("status", "sha256_now", "byte_count_now", "changed")}
        source["observed"]["checked_at_utc"] = report["checked_at_utc"]
    for entry in all_entries(updated):
        result = report["entries"][entry["id"]]
        previous = entry.get("freshness", {})
        entry["freshness"] = {
            "status": result["status"],
            "reasons": result["reasons"],
            "notes": result["notes"],
            "checked_at_utc": report["checked_at_utc"],
        }
        for key in ("pending_review", "review"):
            if key in previous:
                entry["freshness"][key] = previous[key]
    index = updated["index_dependencies"]
    index["receipts"]["observed"] = report["index"]["receipts"]
    index["host_dispositions"]["observed"] = {
        "names": report["index"]["host_dispositions"]["names"],
        "listing_sha256": listing_sha256(report["index"]["host_dispositions"]["names"]),
    }
    index["origami_records"]["observed"] = {
        "names": report["index"]["origami_records"]["names"],
        "listing_sha256": listing_sha256(report["index"]["origami_records"]["names"]),
    }
    index["head"]["observed"] = report["git"]
    updated["recipe"]["observed"] = report["recipe"]
    updated["freshness"] = {
        "checked_at_utc": report["checked_at_utc"],
        "summary": report["summary"],
        "triggers_fired": report["triggers_fired"],
        "recipe_changed": report["recipe"]["changed"],
    }
    history = updated.setdefault("refresh_history", [])
    history.append({"checked_at_utc": report["checked_at_utc"], "head": report["git"]["head"],
                    "summary": report["summary"], "triggers_fired": report["triggers_fired"],
                    "recipe_changed": report["recipe"]["changed"]})
    del history[:-20]
    return updated


def rebind(root: Path, fold: dict, source_id: str | None = None, receipt: str | None = None,
           listings: bool = False, recipe: bool = False, reviewed_by: str | None = None,
           note: str = "") -> dict:
    """Re-accept one changed dependency after a worker has reviewed it. Refuses partial fits."""
    updated = json.loads(json.dumps(fold))
    stamp = utc_now()
    review = {"by": reviewed_by or "UNKNOWN", "at": stamp, "note": note}
    if source_id:
        sources = {source["id"]: source for source in updated["sources"]}
        if source_id not in sources:
            raise FoldError(f"unknown source id: {source_id}")
        source = sources[source_id]
        if source.get("path") is None:
            raise FoldError("authorization sources are resolved by receipt; nothing to rebind")
        state = source_state(root, source)
        if state["status"] != "PRESENT":
            raise FoldError(f"cannot rebind {source_id}: {state['status']}")
        data = (root / source["path"]).read_bytes()
        touched = []
        for entry in all_entries(updated):
            for support in entry.get("support", []):
                if support["source"] != source_id:
                    continue
                first, last = support["lines"]
                fresh = bind_support(data, source_id, first, last, support.get("quote"))
                support.update(fresh)
                touched.append(entry)
        source["bound"] = {"sha256": state["sha256_now"], "byte_count": state["byte_count_now"],
                           "line_count": len(segment_lines(data)), "bound_at_utc": stamp}
        source.pop("observed", None)
        source.setdefault("rebind_log", []).append(review)
        for entry in touched:
            freshness = entry.setdefault("freshness", {})
            if entry.get("basis") == "interpretation" and not reviewed_by:
                freshness["pending_review"] = True
            else:
                freshness.pop("pending_review", None)
                freshness["review"] = review
    if receipt:
        if receipt not in receipt_files(root):
            raise FoldError(f"not a receipt in this repository: {receipt}")
        entry = {"path": receipt, "sha256": sha256_bytes((root / receipt).read_bytes()), **review}
        updated.setdefault("acknowledged_receipts", []).append(entry)
    if listings:
        index = updated["index_dependencies"]
        for key in ("host_dispositions", "origami_records"):
            names = listing(root, index[key]["patterns"])
            index[key]["bound"] = {"names": names, "listing_sha256": listing_sha256(names),
                                   "bound_at_utc": stamp, "review": review}
    if recipe:
        for item in updated["recipe"]["files"]:
            path = root / item["path"]
            if not path.is_file():
                raise FoldError(f"recipe file missing: {item['path']}")
            item["sha256"] = sha256_bytes(path.read_bytes())
        updated["recipe"]["rebound"] = review
    if not (source_id or receipt or listings or recipe):
        raise FoldError("rebind needs --source, --receipt, --listings or --recipe")
    return updated


# --------------------------------------------------------------------------
# text views
# --------------------------------------------------------------------------

def _handles(entry: dict) -> str:
    return ", ".join(handle_string(support) for support in entry.get("support", []))


def _short(digest: str | None) -> str:
    return (digest or "UNKNOWN")[:12]


def orient_text(fold: dict, report: dict) -> str:
    work = fold["work_item"]
    construction = fold["construction"]
    lines = [
        f"ORIGAMI FOLD — orient — {work['id']}",
        f"work item: {work['title']}",
        f"question:  {work['question']}",
        f"built:     {construction['built_at_utc']} by {construction['built_by']}",
        f"           under {construction['authorization']['title']} (source HD)",
        f"epoch:     {work['epoch']['description']}",
        f"HEAD then: {fold['index_dependencies']['head']['bound']}   HEAD now: {report['git']['head']}   (provenance only)",
        f"checked:   {report['checked_at_utc']}   freshness: "
        + ", ".join(f"{count} {status}" for status, count in report["summary"].items()),
        f"state index (CURRENT_STATE.md): {'CHANGED since binding' if report['index']['state_index']['changed'] else 'unchanged'}"
        f"; recipe: {'CHANGED' if report['recipe']['changed'] else 'unchanged'}",
    ]
    receipts = report["index"]["receipts"]
    lines.append(f"receipts:  {receipts['count']} files; newest {receipts['newest']}")
    if receipts["construction_receipt"]:
        lines.append(f"           construction receipt of this fold: {receipts['construction_receipt']}")
    else:
        lines.append("           construction receipt of this fold: not found (expected only after construction closes)")
    if receipts["unfolded"]:
        lines.append("ALERT: receipts newer than this fold and not acknowledged (read before relying on current-scope entries):")
        for item in receipts["unfolded"]:
            lines.append(f"  - {item['path']}  ({item['operation_name']})")
    for key in ("new_host_disposition", "new_origami_record"):
        if report["triggers_fired"][key]:
            lines.append(f"ALERT: {key} — listing differs from binding: {report['index'][key.replace('new_', '') + ('s' if key == 'new_host_disposition' else 's')]['names']}")

    def block(title: str, entries: list[dict], label) -> None:
        if not entries:
            return
        lines.append("")
        lines.append(title)
        for entry in entries:
            status = report["entries"][entry["id"]]
            tag = status["status"]
            if status["reasons"]:
                tag += " (" + "; ".join(status["reasons"]) + ")"
            lines.append(f"  [{tag}] {entry['id']}")
            lines.append(f"      {label(entry)}")
            lines.append(f"      handles: {_handles(entry)}")

    claims = fold["claims"]
    established = [claim for claim in claims if claim["status"] == "ESTABLISHED"]
    block("ESTABLISHED (recorded facts; scope and epoch as stated):", established,
          lambda claim: f"{claim['text']}  [{claim['basis']}; scope {claim['scope']['kind']}: {claim['scope']['epoch']}]"
          + (f"  accepted by {claim['accepted_by']['actor']} via {claim['accepted_by']['act']}" if claim.get("accepted_by") else ""))
    block("PROPOSALS (on paper only; not authorized to run):",
          [claim for claim in claims if claim["status"] == "PROPOSAL"],
          lambda claim: f"{claim['text']}  [{claim['basis']}]")
    block("UNRESOLVED (recorded, not settled):",
          [claim for claim in claims if claim["status"] in ("UNKNOWN", "UNRESOLVED")],
          lambda claim: f"{claim['text']}  [{claim['basis']}]")
    items = fold["open_items"]
    block("OPEN — Host inputs required:", [item for item in items if item["kind"] == "HOST_INPUT_REQUIRED"],
          lambda item: item["text"])
    block("OPEN — UNKNOWN (not 0.5, not false, not permission):", [item for item in items if item["kind"] == "UNKNOWN"],
          lambda item: item["text"])
    block("CONTRADICTIONS — unresolved, preserved:", [item for item in items if item["kind"] == "CONTRADICTION_UNRESOLVED"],
          lambda item: item["text"])
    block("RESTRICTIONS — decisive, must stay visible:", [item for item in items if item["kind"] == "RESTRICTION"],
          lambda item: item["text"])
    lines.append("")
    lines.append("NEXT PERMITTED ACTION (recorded): " + fold["work_item"]["next_permitted_action"])
    lines.append("")
    lines.append("SOURCES (id  path  bound sha256  bytes  status now):")
    for source in fold["sources"]:
        state = report["sources"][source["id"]]
        if source.get("path") is None:
            resolution = report["by_receipt"][source["id"]]
            lines.append(f"  {source['id']:<5} (by receipt: operation_name {source['resolution']['operation_name']})  "
                         f"{_short(source['bound']['sha256'])}  {resolution['status']}"
                         + (f" {resolution['path']}" if resolution.get("path") else ""))
            continue
        now = state["status"] if state["status"] != "PRESENT" else ("CHANGED" if state["changed"] else "unchanged")
        lines.append(f"  {source['id']:<5} {source['path']}  {_short(source['bound']['sha256'])}  "
                     f"{source['bound']['byte_count']}  {now}")
    lines.append("")
    lines.append("expand: `expand <entry-id>` | `expand <SRC>:L<a>-<b>` | `expand <SRC>` ; refresh: `refresh` (dry) / `refresh --apply`")
    return "\n".join(lines) + "\n"


def expand_text(root: Path, fold: dict, handle: str, context: int = 0) -> tuple[str, int]:
    sources = {source["id"]: source for source in fold["sources"]}
    entries = {entry["id"]: entry for entry in all_entries(fold)}
    out: list[str] = []
    code = 0

    def show_passage(support: dict) -> None:
        nonlocal code
        source = sources[support["source"]]
        if source.get("path") is None:
            resolution = resolve_by_receipt(root, source)
            out.append(f"handle: {support['source']} (authorization resolved by receipt operation_name "
                       f"{source['resolution']['operation_name']})")
            out.append(f"bound directive sha256: {source['bound']['sha256']}")
            if resolution["status"] != "RESOLVED":
                out.append(f"receipt: {resolution['status']} — no passage can be shown (nothing is generated)")
                code = 1
                return
            out.append(f"receipt: {resolution['path']}  embedded sha256 {resolution.get('embedded_sha256')}  "
                       f"[{'MATCH' if resolution.get('match') else 'MISMATCH'}]")
            payload = initiating_prompt_payload(root / resolution["path"]) or ""
            out.append("--- initiating_prompt payload (verbatim) ---")
            out.append(payload)
            out.append("--- end ---")
            return
        state = source_state(root, source)
        handle_id = handle_string(support)
        out.append(f"handle: {handle_id}")
        out.append(f"path:   {source['path']}")
        if state["status"] != "PRESENT":
            out.append(f"source: {state['status']} — no passage can be shown (nothing is generated)")
            code = 1
            return
        match = "MATCH" if not state["changed"] else "CURRENT BYTES DIFFER FROM BOUND"
        out.append(f"sha256: bound {source['bound']['sha256']}")
        out.append(f"        now   {state['sha256_now']}  [{match}]")
        data = (root / source["path"]).read_bytes()
        passage = passage_state(data, support)
        if passage["status"] == "OUT_OF_RANGE":
            out.append(f"lines L{support['lines'][0]}-{support['lines'][1]}: OUT OF RANGE (file now has "
                       f"{passage['line_count_now']} lines); relocation candidate: {passage.get('relocation_candidate')}")
            code = 1
            return
        first, last = support["lines"]
        out.append(f"lines:  L{first}-{last} = bytes [{passage['start_byte']}, {passage['end_byte']})  "
                   f"slice sha256 bound {_short(passage['slice_sha256_bound'])} now {_short(passage['slice_sha256_now'])}  "
                   f"[{passage['status']}]"
                   + ("" if passage["status"] == "INTACT" else f"; relocation candidate {passage.get('relocation_candidate')}"))
        if support.get("quote") is not None:
            out.append(f"quote present: {'yes' if passage['quote_present'] else 'NO'}")
        if passage["status"] != "INTACT":
            out.append("NOTE: passage shown is the CURRENT text at these lines, not the bound passage; review before relying on it")
            code = 1
        shown_first, shown_last = max(1, first - context), last + context
        text, _, _ = slice_lines(data, shown_first, min(shown_last, len(segment_lines(data))))
        out.append(f"--- passage (verbatim, L{shown_first}-{min(shown_last, len(segment_lines(data)))}) ---")
        out.append(text.decode("utf-8", errors="replace").rstrip("\n"))
        out.append("--- end ---")

    match = HANDLE_RE.match(handle)
    if match and match.group(1) in sources:
        first = int(match.group(2))
        last = int(match.group(3) or match.group(2))
        source = sources[match.group(1)]
        data = (root / source["path"]).read_bytes() if source.get("path") and (root / source["path"]).is_file() else None
        if data is None:
            out.append(f"handle: {handle}\nsource: MISSING — no passage can be shown")
            return "\n".join(out) + "\n", 1
        try:
            payload, _, _ = slice_lines(data, first, last)
        except FoldError as error:
            return f"handle: {handle}\n{error}\n", 1
        support = {"source": match.group(1), "lines": [first, last], "slice_sha256": sha256_bytes(payload), "quote": None}
        show_passage(support)
        out.append("(ad-hoc handle: slice hash computed now, not a bound locator)")
        return "\n".join(out) + "\n", code
    if handle in entries:
        entry = entries[handle]
        kind = "claim" if handle in {claim["id"] for claim in fold["claims"]} else (
            "relation" if handle in {relation["id"] for relation in fold["relations"]} else "open item")
        out.append(f"{kind}: {handle}")
        if kind == "relation":
            out.append(f"type: {entry['type']} ({RELATION_TYPES[entry['type']]})  {entry['from']} -> {entry['to']}")
        out.append(f"text: {entry.get('text') or entry.get('note')}")
        out.append(f"basis: {entry['basis']}" + (f"; status {entry['status']}" if "status" in entry else "")
                   + (f"; scope {entry['scope']['kind']}: {entry['scope']['epoch']}" if entry.get("scope") else ""))
        for support in entry["support"]:
            out.append("")
            show_passage(support)
        return "\n".join(out) + "\n", code
    if handle in sources:
        source = sources[handle]
        out.append(f"source: {handle}")
        for key, value in source.items():
            if key not in ("id",):
                out.append(f"  {key}: {json.dumps(value, ensure_ascii=False)}")
        out.append("cited by:")
        for entry in all_entries(fold):
            for support in entry.get("support", []):
                if support["source"] == handle:
                    out.append(f"  {entry['id']}  {handle_string(support)}")
        return "\n".join(out) + "\n", 0
    raise FoldError(f"unknown handle: {handle!r} (use an entry id, a source id, or SRC:La-b)")


def render_markdown(fold: dict) -> str:
    work = fold["work_item"]
    construction = fold["construction"]
    freshness = fold.get("freshness", {})
    entries = {entry["id"]: entry for entry in all_entries(fold)}

    def fresh_tag(entry: dict) -> str:
        block = entry.get("freshness", {})
        if not block:
            return "unchecked"
        tag = block.get("status", "unchecked")
        if block.get("reasons"):
            tag += " (" + "; ".join(block["reasons"]) + ")"
        return tag

    def esc(text: str) -> str:
        return text.replace("|", "\\|").replace("\n", " ")

    out = [
        f"# {work['title']} — fold view",
        "",
        f"Generated from `fold.json` (schema `{SCHEMA_NAME}` {SCHEMA_VERSION}) by `origami_fold.py render`. "
        "Derived navigation aid; pointers with verification handles, not an authority over its sources. "
        "Do not edit this file by hand.",
        "",
        "## Work item",
        "",
        f"- Question: {work['question']}",
        f"- Epoch: {work['epoch']['description']}",
        f"- Next permitted action (recorded): {work['next_permitted_action']}",
        "",
        "## Construction",
        "",
        f"- Built: {construction['built_at_utc']} by {construction['built_by']}",
        f"- Authorization: {construction['authorization']['title']}; verbatim in the receipt with "
        f"operation_name `{construction['operation_name']}`; directive SHA-256 `{construction['authorization']['directive_sha256']}`",
        f"- HEAD at construction: `{fold['index_dependencies']['head']['bound']}` (provenance only; not an invalidator)",
        f"- Recipe: version {fold['recipe']['version']}; " + "; ".join(
            f"`{item['path']}` `{item['sha256'][:12]}…`" for item in fold["recipe"]["files"]),
        f"- Interpretation: {construction['interpretation_author']}",
        "- This fold is NOT: " + "; ".join(construction["not_this"]),
        f"- Telemetry: tokens {construction['telemetry']['tokens']}, cost {construction['telemetry']['cost']}",
        "",
        "## Freshness at last check",
        "",
        f"- Checked: {freshness.get('checked_at_utc', 'never')}; summary {json.dumps(freshness.get('summary', {}))}; "
        f"triggers fired {json.dumps(freshness.get('triggers_fired', {}))}; recipe changed: {freshness.get('recipe_changed', 'unchecked')}",
        "",
    ]

    def table(title: str, rows: list[dict], columns: list[tuple[str, object]]) -> None:
        if not rows:
            return
        out.append(f"## {title}")
        out.append("")
        out.append("| " + " | ".join(name for name, _ in columns) + " |")
        out.append("|" + "---|" * len(columns))
        for row in rows:
            out.append("| " + " | ".join(esc(str(getter(row))) for _, getter in columns) + " |")
        out.append("")

    claims = fold["claims"]
    claim_columns = [
        ("id", lambda c: f"`{c['id']}`"),
        ("claim", lambda c: c["text"]),
        ("basis", lambda c: c["basis"]),
        ("scope", lambda c: f"{c['scope']['kind']}: {c['scope']['epoch']}"),
        ("accepted by", lambda c: f"{c['accepted_by']['actor']} ({c['accepted_by']['act']})" if c.get("accepted_by") else "—"),
        ("handles", lambda c: _handles(c)),
        ("refresh when", lambda c: ", ".join(c.get("refresh_when", [])) or "source change only"),
        ("freshness", fresh_tag),
    ]
    table("Established", [c for c in claims if c["status"] == "ESTABLISHED"], claim_columns)
    table("Proposals (on paper; not authorized to run)", [c for c in claims if c["status"] == "PROPOSAL"], claim_columns)
    table("Unresolved claims", [c for c in claims if c["status"] in ("UNKNOWN", "UNRESOLVED")], claim_columns)
    item_columns = [
        ("id", lambda i: f"`{i['id']}`"),
        ("kind", lambda i: i["kind"]),
        ("text", lambda i: i["text"]),
        ("basis", lambda i: i["basis"]),
        ("handles", lambda i: _handles(i)),
        ("freshness", fresh_tag),
    ]
    table("Open items, contradictions and restrictions", fold["open_items"], item_columns)
    relation_columns = [
        ("id", lambda r: f"`{r['id']}`"),
        ("type (category)", lambda r: f"{r['type']} ({RELATION_TYPES[r['type']]})"),
        ("from → to", lambda r: f"`{r['from']}` → `{r['to']}`"),
        ("scope / note", lambda r: r.get("note", "")),
        ("basis", lambda r: r["basis"]),
        ("handles", lambda r: _handles(r)),
        ("freshness", fresh_tag),
    ]
    table("Typed relations", fold["relations"], relation_columns)
    signature = fold.get("signature", {})
    if signature:
        out.append("## Signature (reusable structure) and residuals")
        out.append("")
        out.append(f"Basis: {signature.get('basis', 'interpretation')}. {signature.get('note', '')}")
        out.append("")
        for edge in signature.get("edges", []):
            out.append(f"- `{edge['pattern']}` — instances: {', '.join('`' + i + '`' for i in edge['instances'])}")
        out.append("")
        out.append("Residuals (source-specific; do not fit the pattern and are not merged into it):")
        out.append("")
        for residual in signature.get("residuals", []):
            out.append(f"- {residual['text']} — handles: {_handles(residual)}")
        out.append("")
    if fold.get("observations"):
        out.append("## Construction-time observations (not repository records; not refreshed by the tool)")
        out.append("")
        for observation in fold["observations"]:
            out.append(f"- {observation['observed_at_utc']}: {observation['text']}")
        out.append("")
    source_columns = [
        ("id", lambda s: f"`{s['id']}`"),
        ("path", lambda s: f"`{s['path']}`" if s.get("path") else f"by receipt: `{s['resolution']['operation_name']}`"),
        ("record", lambda s: s.get("record_kind", "")),
        ("authority / author", lambda s: s.get("authority", "")),
        ("bound sha256", lambda s: f"`{s['bound']['sha256']}`"),
        ("bytes", lambda s: s["bound"].get("byte_count", "—")),
        ("lines", lambda s: s["bound"].get("line_count", "—")),
        ("tracked at binding", lambda s: s.get("tracked_in_git_at_binding", "UNKNOWN")),
        ("observed", lambda s: (s["observed"]["status"] + ("/changed" if s["observed"].get("changed") else "")) if s.get("observed") else "—"),
        ("notes", lambda s: s.get("notes", "")),
    ]
    table("Sources", fold["sources"], source_columns)
    index = fold["index_dependencies"]
    out.append("## Index dependencies (declared refresh dependencies)")
    out.append("")
    out.append(f"- State index: source `{index['state_index']['source']}` — its whole-file change fires `state_index_changed`.")
    out.append(f"- Receipts: `receipts/` files with filename timestamp after `{index['receipts']['cutoff_basic_utc']}` that are not sources, "
               f"not acknowledged and not this fold's construction receipt fire `new_receipt` (listed by `orient`). "
               f"Bound at construction: {index['receipts']['bound']['count']} files; newest `{index['receipts']['bound']['newest']}`.")
    out.append(f"- Host dispositions: patterns {index['host_dispositions']['patterns']}; bound listing "
               f"`{index['host_dispositions']['bound']['listing_sha256'][:12]}…` ({len(index['host_dispositions']['bound']['names'])} files).")
    out.append(f"- Origami records: patterns {index['origami_records']['patterns']}; bound listing "
               f"`{index['origami_records']['bound']['listing_sha256'][:12]}…` ({len(index['origami_records']['bound']['names'])} files).")
    out.append(f"- HEAD: `{index['head']['bound']}` — {index['head']['role']}")
    out.append("")
    if fold.get("acknowledged_receipts"):
        out.append("## Acknowledged receipts")
        out.append("")
        for item in fold["acknowledged_receipts"]:
            out.append(f"- `{item['path']}` by {item['by']} at {item['at']}: {item.get('note', '')}")
        out.append("")
    if fold.get("refresh_history"):
        out.append("## Refresh history (last 20)")
        out.append("")
        for item in fold["refresh_history"]:
            out.append(f"- {item['checked_at_utc']} HEAD `{item['head']}` summary {json.dumps(item['summary'])} "
                       f"triggers {json.dumps(item['triggers_fired'])} recipe changed {item['recipe_changed']}")
        out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


# --------------------------------------------------------------------------
# command line
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Origami re-entry fold: orient / expand / refresh / render / verify / rebind")
    parser.add_argument("--root", type=Path, default=None, help="repository root (default: three levels above this file)")
    parser.add_argument("--fold", type=Path, default=None, help=f"fold.json path (default: <root>/{DEFAULT_FOLD_REL})")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("orient", help="load the fold, check freshness, show established/open state and handles")
    p_expand = sub.add_parser("expand", help="print the actual cited passage for an entry id, SRC:La-b, or source id")
    p_expand.add_argument("handle")
    p_expand.add_argument("--context", type=int, default=0, help="extra lines before/after the bound range")
    p_refresh = sub.add_parser("refresh", help="check declared dependencies; --apply updates only derived entries")
    p_refresh.add_argument("--apply", action="store_true")
    p_refresh.add_argument("--json", action="store_true", help="print the full report as JSON")
    sub.add_parser("render", help="regenerate fold.md from fold.json")
    sub.add_parser("verify", help="structural validation plus exact consistency with current repository bytes")
    p_rebind = sub.add_parser("rebind", help="re-accept a reviewed dependency (source, receipt, listings or recipe)")
    p_rebind.add_argument("--source")
    p_rebind.add_argument("--receipt")
    p_rebind.add_argument("--listings", action="store_true")
    p_rebind.add_argument("--recipe", action="store_true")
    p_rebind.add_argument("--reviewed-by")
    p_rebind.add_argument("--note", default="")
    return parser


def refresh_text(report: dict) -> str:
    lines = [f"refresh check at {report['checked_at_utc']} (root {report['root']}, HEAD {report['git']['head']})",
             "summary: " + ", ".join(f"{count} {status}" for status, count in report["summary"].items()),
             "triggers fired: " + ", ".join(key for key, value in report["triggers_fired"].items() if value) or "triggers fired: none",
             f"recipe changed: {report['recipe']['changed']}"]
    for sid, state in report["sources"].items():
        if state["status"] == "BY_RECEIPT":
            lines.append(f"  source {sid}: by receipt -> {report['by_receipt'][sid]['status']}")
        elif state["status"] != "PRESENT":
            lines.append(f"  source {sid}: {state['status']}")
        elif state["changed"]:
            lines.append(f"  source {sid}: CHANGED (now {_short(state['sha256_now'])}, {state['byte_count_now']} bytes)")
    for item in report["index"]["receipts"]["unfolded"]:
        lines.append(f"  unfolded receipt: {item['path']} ({item['operation_name']})")
    if report["index"]["receipts"]["construction_receipt"]:
        lines.append(f"  construction receipt: {report['index']['receipts']['construction_receipt']}")
    for entry_id, entry in report["entries"].items():
        if entry["status"] != FRESH or entry["notes"]:
            detail = "; ".join(entry["reasons"] + entry["notes"])
            lines.append(f"  {entry['status']:<14} {entry_id}  {detail}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = (args.root or default_root()).resolve()
    fold_path = (args.fold or (root / DEFAULT_FOLD_REL)).resolve()
    try:
        fold = load_fold(fold_path)
        if args.command == "orient":
            errors = structural_errors(fold)
            if errors:
                print("\n".join(errors), file=sys.stderr)
                return 1
            report = check_fold(root, fold)
            sys.stdout.write(orient_text(fold, report))
            return 0
        if args.command == "expand":
            text, code = expand_text(root, fold, args.handle, args.context)
            sys.stdout.write(text)
            return code
        if args.command == "refresh":
            errors = structural_errors(fold)
            if errors:
                print("\n".join(errors), file=sys.stderr)
                return 1
            report = check_fold(root, fold)
            if args.json:
                print(json.dumps(report, indent=2, ensure_ascii=False))
            else:
                sys.stdout.write(refresh_text(report))
            if args.apply:
                updated = apply_report(fold, report)
                save_fold(fold_path, updated)
                fold_path.with_name("fold.md").write_text(render_markdown(updated), encoding="utf-8")
                print(f"applied: updated derived entries in {fold_path} and regenerated fold.md")
            else:
                print("dry run: nothing written (use --apply to record this check)")
            return 0 if report["summary"][MISSING_SOURCE] == 0 and report["summary"][NEEDS_REVIEW] == 0 else 1
        if args.command == "render":
            fold_path.with_name("fold.md").write_text(render_markdown(fold), encoding="utf-8")
            print(f"rendered {fold_path.with_name('fold.md')}")
            return 0
        if args.command == "verify":
            errors = verify(root, fold)
            if errors:
                print("\n".join(errors), file=sys.stderr)
                return 1
            print(f"fold verified against {root}: sources, passages and recipe match their bound identities")
            return 0
        if args.command == "rebind":
            updated = rebind(root, fold, args.source, args.receipt, args.listings, args.recipe,
                             args.reviewed_by, args.note)
            report = check_fold(root, updated)
            updated = apply_report(updated, report)
            save_fold(fold_path, updated)
            fold_path.with_name("fold.md").write_text(render_markdown(updated), encoding="utf-8")
            sys.stdout.write(refresh_text(report))
            print(f"rebound and saved {fold_path}")
            return 0
        return 2
    except (FoldError, OSError, ValueError, KeyError, TypeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
