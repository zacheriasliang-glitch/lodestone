"""Deterministic lexical paragraph selection; semantic sufficiency is unassessed."""
from __future__ import annotations

from pathlib import Path
import re

from ._values import _hash
from .sources import MAX_SOURCES, anchor

METHOD = "deterministic paragraph lexical overlap; no semantic recognition"
OVERVIEW_METHOD = "bounded all-source paragraph overview; no completeness assurance"


def _words(text: str) -> set[str]:
    return set(re.findall(r"[^\W_]+", text.casefold(), flags=re.UNICODE))


def _paragraphs(source: dict, terms: set[str]) -> list[dict]:
    lines = source["text"].splitlines(keepends=True)
    passages = []
    start = None
    for index in range(len(lines) + 1):
        nonblank = index < len(lines) and bool(lines[index].strip())
        if nonblank and start is None:
            start = index
        if not nonblank and start is not None:
            text = "".join(lines[start:index])
            passages.append({"source": source["path"], "start_line": start + 1,
                             "end_line": index, "text": text,
                             "source_sha256": source["sha256"],
                             "slice_sha256": _hash(text.encode("utf-8")),
                             "score": len(terms & _words(text))})
            start = None
    return passages


def fold(root: Path, paths: list[str], query: str, max_passages: int = 8) -> dict:
    """Select actual paragraphs, with source diversity before additional extracts.

    Every matching source gets its best paragraph before a second paragraph from
    any source, subject to the explicit bound. Ties use source path then line.
    Query terms are case-folded lexical words, including numbers, without a
    stop-word or semantic expansion rule. Omitted source relationships are exposed.
    """
    if type(paths) is not list or len(paths) > MAX_SOURCES:
        raise ValueError(f"paths must be a list of at most {MAX_SOURCES} sources")
    if any(type(path) is not str for path in paths) or len(set(paths)) != len(paths):
        raise ValueError("Source paths must be distinct strings")
    if type(query) is not str:
        raise ValueError("query must be a string")
    if type(max_passages) is not int or not 1 <= max_passages <= 1000:
        raise ValueError("max_passages must be an integer from 1 to 1000")
    terms = _words(query)
    sources = [anchor(root, path) for path in sorted(paths)]
    all_passages = [p for source in sources for p in _paragraphs(source, terms)]
    eligible = [p for p in all_passages if p["score"] > 0 or not terms]
    eligible.sort(key=lambda p: (-p["score"], p["source"], p["start_line"]))
    first, rest, seen = [], [], set()
    for passage in eligible:
        if passage["source"] in seen:
            rest.append(passage)
        else:
            first.append(passage)
            seen.add(passage["source"])
    selected = (first + rest)[:max_passages]
    covered = set().union(*(_words(p["text"]) for p in selected)) if selected else set()
    missing = sorted(terms - covered)
    selected_keys = {(p["source"], p["start_line"]) for p in selected}
    counts = {s["path"]: 0 for s in sources}
    for passage in all_passages:
        if (passage["source"], passage["start_line"]) not in selected_keys:
            counts[passage["source"]] += 1
    selected_sources = {p["source"] for p in selected}
    return {
        "schema_version": "1.0.0", "query": query,
        "sources": [{k: s[k] for k in ("id", "path", "sha256", "bytes")} for s in sources],
        "passages": selected, "missing_terms": missing,
        "selection_method": METHOD if terms else OVERVIEW_METHOD,
        "max_passages": max_passages,
        "omitted_sources": [s["path"] for s in sources if s["path"] not in selected_sources],
        "omitted_passages": sum(counts.values()), "omission_counts": counts,
        "insufficient": not bool(selected) or bool(missing),
        "diagnostics": "Literal extracts only; lexical coverage does not establish semantic completeness."
        if selected else "No matching nonblank passage was found; no answer is established.",
    }
