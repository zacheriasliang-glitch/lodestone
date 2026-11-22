"""Render literal extracts and check their current source fidelity."""
from __future__ import annotations

import json
from pathlib import Path
import re

from ._values import _hash, _json, _validate_json
from .sources import MAX_SOURCE_BYTES, MAX_SOURCES, anchor
from .selection import METHOD, OVERVIEW_METHOD, _words, _paragraphs


def _fence(text: str) -> str:
    return "`" * max(3, max((len(m.group()) + 1 for m in re.finditer(r"`+", text)), default=3))


def build_brief(folded: dict) -> str:
    """Render source-linked literal extracts, including omission and uncertainty."""
    if type(folded) is not dict:
        raise ValueError("folded must be a dictionary")
    try:
        lines = ["# Source-grounded brief", "", "Query: " + _json(folded["query"]),
                 "Method: " + _json(folded["selection_method"]),
                 "Completeness: NOT ESTABLISHED",
                 "Insufficient evidence: " + _json(folded["insufficient"]),
                 "Missing terms: " + _json(folded["missing_terms"]),
                 "Omitted sources: " + _json(folded["omitted_sources"]),
                 "Omitted passages: " + str(folded["omitted_passages"]),
                 "", "Literal extracts follow. No unverified paraphrase is supplied.", ""]
        chunks = ["\n".join(lines)]
        for index, passage in enumerate(folded["passages"], 1):
            text = passage["text"]
            fence = _fence(text)
            chunks.append("\n".join([
                f"## Extract {index}", "Source: " + _json(passage["source"]),
                f"Lines: {passage['start_line']}-{passage['end_line']}",
                "Source SHA256: " + passage["source_sha256"],
                "Slice SHA256: " + passage["slice_sha256"],
                "Text UTF-8 bytes: " + str(len(text.encode("utf-8"))),
                fence + "text", ""]) + text + "\n" + fence + "\n")
        return "\n".join(chunks)
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("Invalid fold cannot be rendered") from exc


def _parse_brief(brief: str) -> tuple[dict, list[dict]]:
    """Independent byte-counted parser; no invocation of the renderer."""
    if not brief.startswith("# Source-grounded brief\n\n"):
        raise ValueError("Missing literal-brief header")
    cursor = len("# Source-grounded brief\n\n")

    def line(prefix: str) -> str:
        nonlocal cursor
        end = brief.find("\n", cursor)
        if end < 0 or not brief[cursor:end].startswith(prefix):
            raise ValueError(f"Missing or corrupted {prefix}")
        value = brief[cursor + len(prefix):end]
        cursor = end + 1
        return value

    header = {}
    for label, key in [("Query: ", "query"), ("Method: ", "selection_method")]:
        header[key] = json.loads(line(label))
    if line("Completeness: ") != "NOT ESTABLISHED":
        raise ValueError("Unsupported completeness claim")
    for label, key in [("Insufficient evidence: ", "insufficient"),
                       ("Missing terms: ", "missing_terms"),
                       ("Omitted sources: ", "omitted_sources")]:
        header[key] = json.loads(line(label))
    header["omitted_passages"] = int(line("Omitted passages: "))
    trailer = "\nLiteral extracts follow. No unverified paraphrase is supplied.\n"
    if brief[cursor:cursor + len(trailer)] != trailer:
        raise ValueError("Missing limitations or unexpected additional claims")
    cursor += len(trailer)
    passages = []
    while cursor < len(brief):
        if brief[cursor:cursor + 1] != "\n":
            raise ValueError("Unexpected material outside extracts")
        cursor += 1
        if line("## Extract ") != str(len(passages) + 1):
            raise ValueError("Extract numbering is invalid")
        source = json.loads(line("Source: "))
        interval = re.fullmatch(r"([1-9][0-9]*)-([1-9][0-9]*)", line("Lines: "))
        if interval is None:
            raise ValueError("Invalid extract coordinates")
        sha, slice_sha = line("Source SHA256: "), line("Slice SHA256: ")
        byte_count = int(line("Text UTF-8 bytes: "))
        if not 0 <= byte_count <= MAX_SOURCE_BYTES:
            raise ValueError("Invalid literal length")
        fence_line = line("")
        if not re.fullmatch(r"`{3,}text", fence_line):
            raise ValueError("Invalid literal fence")
        fence = fence_line[:-4]
        remaining = brief[cursor:].encode("utf-8")
        literal = remaining[:byte_count].decode("utf-8", errors="strict")
        cursor += len(literal)
        closer = "\n" + fence + "\n"
        if brief[cursor:cursor + len(closer)] != closer:
            raise ValueError("Literal length or boundary has changed")
        cursor += len(closer)
        passages.append({"source": source, "start_line": int(interval.group(1)),
                         "end_line": int(interval.group(2)), "text": literal,
                         "source_sha256": sha, "slice_sha256": slice_sha})
    return header, passages


def audit_brief(root: Path, folded: dict, brief: str) -> dict:
    """Independently inspect structure, literal text, source identity and coverage.

    PASS means faithful bounded extraction only. It does not assert that a source
    is true, that the selection is cognitively sufficient, or that an organ works
    better than a baseline. Missing lexical evidence gives UNKNOWN, not PASS.
    """
    if type(folded) is not dict or type(brief) is not str:
        raise ValueError("audit requires a fold dictionary and brief string")
    findings = []
    try:
        _validate_json(folded)
        header, output_passages = _parse_brief(brief)
        if folded.get("schema_version") != "1.0.0":
            raise ValueError("Unsupported fold schema")
        query = folded["query"]
        if type(query) is not str:
            raise ValueError("Query is not text")
        terms = _words(query)
        expected_method = METHOD if terms else OVERVIEW_METHOD
        if folded["selection_method"] != expected_method:
            raise ValueError("Selection method was changed")
        source_records = folded["sources"]
        if type(source_records) is not list or len(source_records) > MAX_SOURCES:
            raise ValueError("Source bounds are invalid")
        sources = {}
        for recorded in source_records:
            path = recorded["path"]
            if path in sources:
                raise ValueError("Duplicate source identity")
            source = anchor(root, path)
            if any(recorded.get(key) != source[key] for key in ("id", "path", "sha256", "bytes")):
                raise ValueError(f"Source identity changed: {path}")
            sources[path] = source
        selected = folded["passages"]
        bound = folded["max_passages"]
        if type(bound) is not int or not 1 <= bound <= 1000:
            raise ValueError("Invalid passage bound")
        if type(selected) is not list or len(selected) > bound or len(selected) != len(output_passages):
            raise ValueError("Extract coverage or passage count changed")
        covered, selected_sources, selected_keys = set(), set(), set()
        for recorded, actual in zip(selected, output_passages):
            if any(recorded.get(key) != value for key, value in actual.items()):
                raise ValueError("Artifact content differs from its selected source handle")
            path, start, end = recorded["source"], recorded["start_line"], recorded["end_line"]
            if path not in sources or type(start) is not int or type(end) is not int:
                raise ValueError("Invalid selected source or line coordinate")
            lines = sources[path]["text"].splitlines(keepends=True)
            if start < 1 or end < start or end > len(lines):
                raise ValueError("Selected coordinates are out of range")
            exact = "".join(lines[start - 1:end])
            if (exact != recorded["text"] or sources[path]["sha256"] != recorded["source_sha256"]
                    or _hash(exact.encode("utf-8")) != recorded["slice_sha256"]):
                raise ValueError("Literal source fidelity or hash check failed")
            score = len(terms & _words(exact))
            if type(recorded["score"]) is not int or recorded["score"] != score or (terms and not score):
                raise ValueError("Unsupported lexical selection score")
            key = (path, start)
            if key in selected_keys:
                raise ValueError("Duplicate selected passage")
            selected_keys.add(key)
            selected_sources.add(path)
            covered.update(_words(exact))
        # Recompute omissions from source boundaries, not renderer output or hashes.
        counts = {path: 0 for path in sources}
        paragraph_keys = set()
        for path, source in sources.items():
            for paragraph in _paragraphs(source, terms):
                key = (path, paragraph["start_line"])
                paragraph_keys.add((path, paragraph["start_line"], paragraph["end_line"]))
                if key not in selected_keys:
                    counts[path] += 1
        if any((p["source"], p["start_line"], p["end_line"]) not in paragraph_keys for p in selected):
            raise ValueError("Selected text is not a complete literal paragraph")
        missing = sorted(terms - covered)
        omitted = sorted(set(sources) - selected_sources)
        insufficient = not bool(selected) or bool(missing)
        if (folded["missing_terms"] != missing or folded["omitted_sources"] != omitted
                or type(folded["insufficient"]) is not bool or folded["insufficient"] != insufficient
                or folded["omitted_passages"] != sum(counts.values()) or folded["omission_counts"] != counts):
            raise ValueError("Coverage or omission claims are unsupported")
        for key, value in header.items():
            if type(value) is not type(folded[key]) or value != folded[key]:
                raise ValueError(f"Rendered metadata changed: {key}")
        if insufficient:
            findings.append({"code": "INSUFFICIENT_EVIDENCE", "detail": "No selected evidence or query terms absent from extracts", "missing_terms": missing})
        else:
            findings.append({"code": "LITERAL_FIDELITY_CONFIRMED", "detail": "Selected output and current sources match; semantic adequacy is unassessed"})
        return {"verdict": "UNKNOWN" if insufficient else "PASS", "findings": findings,
                "passage_count": len(selected), "source_count": len(sources),
                "scope": "mechanical source fidelity and lexical coverage only"}
    except (ValueError, KeyError, TypeError, IndexError, UnicodeError) as exc:
        return {"verdict": "FAIL", "findings": [{"code": "FIDELITY_OR_CONTRACT_FAILURE", "detail": str(exc)[:500]}],
                "scope": "mechanical source fidelity and lexical coverage only"}
