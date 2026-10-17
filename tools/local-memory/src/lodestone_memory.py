#!/usr/bin/env python3
"""Lodestone Local Memory Engine.

Small local JSONL catalogue for compact records, typed links, retrieval,
supersession, verification, and budgeted context packets.

Standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

TOKEN_RE = re.compile(r"[A-Za-z0-9_'-]+")
VALID_LINK_TYPES = {
    "explicit", "similar", "supersedes",
    "contradicts", "supports", "questions"
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize(text: str) -> str:
    return " ".join(text.strip().split())


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def tokenize(text: str) -> set[str]:
    return {m.group(0).lower() for m in TOKEN_RE.finditer(text)}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def paths(root: Path) -> tuple[Path, Path, Path, Path]:
    return (
        root / "records.jsonl",
        root / "links.jsonl",
        root / "state.json",
        root / "packets",
    )


def ensure_root(root: Path) -> None:
    records, links, state, packets = paths(root)
    root.mkdir(parents=True, exist_ok=True)
    packets.mkdir(parents=True, exist_ok=True)
    records.touch(exist_ok=True)
    links.touch(exist_ok=True)
    if not state.exists():
        state.write_text(
            json.dumps({"version": "1.0.0", "created_at": utc_now()}, indent=2) + "\n",
            encoding="utf-8",
        )


def read_jsonl(path: Path) -> Iterator[dict]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: invalid JSON: {exc}") from exc


def write_jsonl(path: Path, items: Iterable[dict]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for item in items:
            handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")


def append_jsonl(path: Path, item: dict) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")


def load_records(root: Path) -> list[dict]:
    records, _, _, _ = paths(root)
    return list(read_jsonl(records))


def load_links(root: Path) -> list[dict]:
    _, links, _, _ = paths(root)
    return list(read_jsonl(links))


def make_record(
    title: str,
    body: str,
    source: str,
    kind: str,
    tags: list[str],
    confidence: float,
) -> dict:
    title_n = normalize(title)
    body_n = normalize(body)
    source_n = normalize(source)
    if not title_n or not body_n or not source_n:
        raise ValueError("title, body, and source must be non-empty")
    if not 0.0 <= confidence <= 1.0:
        raise ValueError("confidence must be between 0 and 1")
    now = utc_now()
    return {
        "id": sha256(f"{source_n}\n{title_n}\n{body_n}")[:16],
        "title": title_n,
        "body": body_n,
        "source": source_n,
        "kind": normalize(kind) or "note",
        "tags": sorted({normalize(tag).lower() for tag in tags if normalize(tag)}),
        "confidence": confidence,
        "created_at": now,
        "updated_at": now,
        "content_hash": sha256(body_n),
        "superseded_by": None,
    }


def add_record(root: Path, record: dict) -> tuple[str, bool]:
    ensure_root(root)
    records_path, _, _, _ = paths(root)
    existing = load_records(root)
    for item in existing:
        if item["content_hash"] == record["content_hash"] and item["source"] == record["source"]:
            return item["id"], False
    append_jsonl(records_path, record)
    return record["id"], True


def add_link(root: Path, source_id: str, target_id: str, relation: str, note: str = "") -> None:
    ensure_root(root)
    if relation not in VALID_LINK_TYPES:
        raise ValueError(f"invalid relation type: {relation}")
    ids = {r["id"] for r in load_records(root)}
    if source_id not in ids or target_id not in ids:
        raise ValueError("both link endpoints must exist")
    _, links_path, _, _ = paths(root)
    link = {
        "source_id": source_id,
        "target_id": target_id,
        "type": relation,
        "note": normalize(note),
        "created_at": utc_now(),
    }
    existing = load_links(root)
    key = (source_id, target_id, relation, normalize(note))
    if any((x["source_id"], x["target_id"], x["type"], x.get("note", "")) == key for x in existing):
        return
    append_jsonl(links_path, link)


def supersede(root: Path, old_id: str, new_id: str) -> None:
    if old_id == new_id:
        raise ValueError("record cannot supersede itself")
    records_path, _, _, _ = paths(root)
    records = load_records(root)
    ids = {r["id"] for r in records}
    if old_id not in ids or new_id not in ids:
        raise ValueError("both records must exist")
    for record in records:
        if record["id"] == old_id:
            record["superseded_by"] = new_id
            record["updated_at"] = utc_now()
    write_jsonl(records_path, records)
    add_link(root, old_id, new_id, "supersedes")


@dataclass(frozen=True)
class Scored:
    score: float
    record: dict


def search(root: Path, query: str, limit: int = 10, include_superseded: bool = False) -> list[Scored]:
    if not query.strip():
        return []
    records = load_records(root)
    links = load_links(root)
    q_tokens = tokenize(query)
    explicit_ids = set()
    query_lower = query.lower()
    for link in links:
        if query_lower in link.get("note", "").lower():
            explicit_ids.update([link["source_id"], link["target_id"]])

    scored: list[Scored] = []
    for record in records:
        if record.get("superseded_by") and not include_superseded:
            continue
        haystack = " ".join([
            record["title"], record["body"], record["source"],
            " ".join(record.get("tags", []))
        ])
        tokens = tokenize(haystack)
        overlap = len(q_tokens & tokens) / max(1, len(q_tokens))
        similarity = jaccard(q_tokens, tokens)
        direct_phrase = 1.0 if query_lower in haystack.lower() else 0.0
        explicit_bonus = 1.0 if record["id"] in explicit_ids else 0.0
        # Confidence and currency rank relevant candidates; neither is a match.
        if not (overlap or direct_phrase or explicit_bonus):
            continue
        current_bonus = 1.0 if not record.get("superseded_by") else 0.0
        score = (
            4.0 * overlap
            + 1.5 * similarity
            + 1.5 * direct_phrase
            + 2.0 * explicit_bonus
            + 1.5 * current_bonus
            + 1.0 * float(record.get("confidence", 0.5))
        )
        if score > 1.5:
            scored.append(Scored(score, record))
    scored.sort(key=lambda x: (-x.score, x.record["id"]))
    return scored[:limit]


def related(root: Path, record_id: str, limit: int = 10) -> list[Scored]:
    records = load_records(root)
    links = load_links(root)
    by_id = {r["id"]: r for r in records}
    if record_id not in by_id:
        raise ValueError(f"unknown record id: {record_id}")
    base = by_id[record_id]
    linked_ids = set()
    for link in links:
        if link["source_id"] == record_id:
            linked_ids.add(link["target_id"])
        if link["target_id"] == record_id:
            linked_ids.add(link["source_id"])

    base_tokens = tokenize(" ".join([base["title"], base["body"], " ".join(base.get("tags", []))]))
    scored = []
    for record in records:
        if record["id"] == record_id or record.get("superseded_by"):
            continue
        candidate_tokens = tokenize(" ".join([record["title"], record["body"], " ".join(record.get("tags", []))]))
        sim = jaccard(base_tokens, candidate_tokens)
        explicit = 1.0 if record["id"] in linked_ids else 0.0
        if not (sim or explicit):
            continue
        score = 3.0 * explicit + 2.0 * sim + float(record.get("confidence", 0.5))
        if score > 0.75:
            scored.append(Scored(score, record))
    scored.sort(key=lambda x: (-x.score, x.record["id"]))
    return scored[:limit]


def render_entry(record: dict, score: float | None = None) -> str:
    lines = [f"[{record['title']}]", f"id: {record['id']}"]
    if score is not None:
        lines.append(f"retrieval_score: {score:.3f}")
    lines.extend([
        f"kind: {record['kind']}",
        f"source: {record['source']}",
        f"confidence: {record['confidence']:.2f}",
        f"tags: {', '.join(record.get('tags', [])) or '-'}",
        f"body: {record['body']}",
    ])
    return "\n".join(lines)


def build_packet(root: Path, query: str, max_words: int) -> str:
    results = search(root, query, limit=100)
    chunks = [
        "LODESTONE LOCAL CONTEXT PACKET",
        f"query: {normalize(query)}",
        "note: records are retrieval aids; inspect source files for exact state.",
        "",
    ]
    used = sum(len(x.split()) for x in chunks)
    for result in results:
        entry = render_entry(result.record, result.score)
        words = len(entry.split())
        if used + words > max_words:
            continue
        chunks.extend([entry, ""])
        used += words
    return "\n".join(chunks).rstrip() + "\n"


def verify(root: Path) -> list[str]:
    errors: list[str] = []
    records = load_records(root)
    links = load_links(root)
    ids = set()
    hashes = set()
    for index, record in enumerate(records, 1):
        missing = [key for key in (
            "id", "title", "body", "source", "kind", "tags", "confidence",
            "created_at", "updated_at", "content_hash", "superseded_by"
        ) if key not in record]
        if missing:
            errors.append(f"record {index}: missing {missing}")
            continue
        if record["id"] in ids:
            errors.append(f"duplicate record id: {record['id']}")
        ids.add(record["id"])
        pair = (record["source"], record["content_hash"])
        if pair in hashes:
            errors.append(f"duplicate source/content pair: {pair}")
        hashes.add(pair)
        if not 0 <= float(record["confidence"]) <= 1:
            errors.append(f"record {record['id']}: confidence outside [0,1]")
        if record["content_hash"] != sha256(normalize(record["body"])):
            errors.append(f"record {record['id']}: content hash mismatch")
        if record["superseded_by"] is not None and record["superseded_by"] not in ids | {r.get("id") for r in records}:
            errors.append(f"record {record['id']}: unknown superseding record")
    for index, link in enumerate(links, 1):
        if link.get("type") not in VALID_LINK_TYPES:
            errors.append(f"link {index}: invalid type")
        if link.get("source_id") not in ids or link.get("target_id") not in ids:
            errors.append(f"link {index}: unknown endpoint")
    return errors


def print_results(results: list[Scored]) -> None:
    if not results:
        print("No matching records.")
        return
    for result in results:
        print(render_entry(result.record, result.score))
        print()


def parse_tags(value: str) -> list[str]:
    return [x.strip() for x in value.split(",") if x.strip()]


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init")
    p_init.add_argument("root", type=Path)

    p_add = sub.add_parser("add")
    p_add.add_argument("root", type=Path)
    p_add.add_argument("--title", required=True)
    p_add.add_argument("--body", required=True)
    p_add.add_argument("--source", required=True)
    p_add.add_argument("--kind", default="note")
    p_add.add_argument("--tags", default="")
    p_add.add_argument("--confidence", type=float, default=0.8)

    p_link = sub.add_parser("link")
    p_link.add_argument("root", type=Path)
    p_link.add_argument("source_id")
    p_link.add_argument("target_id")
    p_link.add_argument("--type", required=True, choices=sorted(VALID_LINK_TYPES))
    p_link.add_argument("--note", default="")

    p_sup = sub.add_parser("supersede")
    p_sup.add_argument("root", type=Path)
    p_sup.add_argument("old_id")
    p_sup.add_argument("new_id")

    p_search = sub.add_parser("search")
    p_search.add_argument("root", type=Path)
    p_search.add_argument("query")
    p_search.add_argument("--limit", type=int, default=10)
    p_search.add_argument("--include-superseded", action="store_true")

    p_related = sub.add_parser("related")
    p_related.add_argument("root", type=Path)
    p_related.add_argument("record_id")
    p_related.add_argument("--limit", type=int, default=10)

    p_packet = sub.add_parser("packet")
    p_packet.add_argument("root", type=Path)
    p_packet.add_argument("query")
    p_packet.add_argument("--max-words", type=int, default=500)
    p_packet.add_argument("--output", type=Path)

    p_verify = sub.add_parser("verify")
    p_verify.add_argument("root", type=Path)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    try:
        if args.command == "init":
            ensure_root(args.root)
            print(f"Initialized {args.root}")
        elif args.command == "add":
            record = make_record(
                args.title, args.body, args.source, args.kind,
                parse_tags(args.tags), args.confidence
            )
            record_id, created = add_record(args.root, record)
            print(json.dumps({"id": record_id, "created": created}))
        elif args.command == "link":
            add_link(args.root, args.source_id, args.target_id, args.type, args.note)
            print("Link stored.")
        elif args.command == "supersede":
            supersede(args.root, args.old_id, args.new_id)
            print("Supersession stored.")
        elif args.command == "search":
            print_results(search(args.root, args.query, args.limit, args.include_superseded))
        elif args.command == "related":
            print_results(related(args.root, args.record_id, args.limit))
        elif args.command == "packet":
            packet = build_packet(args.root, args.query, args.max_words)
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(packet, encoding="utf-8")
                print(f"Wrote {args.output}")
            else:
                print(packet, end="")
        elif args.command == "verify":
            errors = verify(args.root)
            if errors:
                print("\n".join(errors), file=sys.stderr)
                return 1
            print("Memory store verified.")
        return 0
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
