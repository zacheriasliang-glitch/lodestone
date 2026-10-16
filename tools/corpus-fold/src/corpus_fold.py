#!/usr/bin/env python3
"""Corpus fold: a derived, source-linked reading plan over an append-only record corpus.

The problem this organ solves is not disk. It is that answering one question now
requires reading a corpus that only ever grows, in which many records have been
corrected or superseded by later ones and say so in their own words.

The fold is derived data. It never rewrites, moves or deletes a record. It binds
each record by repository-relative path, byte count and SHA-256, reads the lineage
the records already declare, and emits the set a reader must actually read to hold
current truth, with every displaced record still present and pointed at its successor.

It invents no address type. A record is identified by its content hash. An edge
exists only where one record names another's hash or filename in a field whose name
states the relation. Nothing is inferred from similarity, recency or title.

Commands: build, orient, verify, expand, plan.
Standard library only.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import stat
import sys
import time
from pathlib import Path, PurePosixPath

SCHEMA_NAME = "corpus-fold"
SCHEMA_VERSION = "1.0.0"

SHA_RE = re.compile(r"\b[0-9a-f]{64}\b")
SHORT_SHA_RE = re.compile(r"\b[0-9a-f]{8,63}\b")
FILE_RE = re.compile(r"\b[A-Za-z0-9][A-Za-z0-9._-]*\.(?:json|md)\b")

# A field name states a relation only if it appears here. Nothing else creates an edge.
RELATION_FIELDS = {
    "corrects": "corrects",
    "supersedes": "supersedes",
    "amends": "amends",
    "supersedes_in_part": "amends",   # partial by definition; see PARTIAL
    "pre_registration": "derives_from",
    "preregistration": "derives_from",
    "authority": "authorized_by",
    "corrected_by": "corrected_by",
    "prior_ack": "derives_from",
    "subject": "about",
    "raised_by": "raised_by",
    "reviewed_file": "about",
    "source": "about",
}

# A displacing relation removes a record from the reading set: the later record
# stands in its place entirely. A PARTIAL relation does not. An amendment that
# withdraws one condition leaves the rest of the original standing, and a fold
# that hides it tells the reader to skip something still current.
DISPLACING = {"corrects", "supersedes"}
PARTIAL = {"amends"}

# Only these relations bind records into one thread. "about" and "raised_by" are
# recorded but do not merge threads: a mention is not a shared subject.
THREAD_FORMING = {"corrects", "supersedes", "amends", "derives_from", "authorized_by", "corrected_by"}

# Names that exist in many directories and index rather than argue. They are never
# edge targets: linking every record to its directory README destroys thread structure
# and asserts a relation the record did not declare.
INDEX_NAMES = {"README.md", "index.json", "index.md"}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _strings(node, out):
    """Every string in the document, with the field path that reached it."""
    if isinstance(node, dict):
        for key, val in node.items():
            _strings(val, out)
    elif isinstance(node, list):
        for val in node:
            _strings(val, out)
    elif isinstance(node, str):
        out.append(node)


def _relation_strings(node, out, field=None):
    """Strings reachable under a field whose name states a relation."""
    if isinstance(node, dict):
        for key, val in node.items():
            nxt = RELATION_FIELDS.get(key, field)
            _relation_strings(val, out, nxt)
    elif isinstance(node, list):
        for val in node:
            _relation_strings(val, out, field)
    elif isinstance(node, str) and field:
        out.append((field, node))


class Bounds:
    """Explicit refusal limits. A fold that silently truncates its corpus is worse
    than one that refuses, because the reader cannot see what is missing."""

    def __init__(self, max_records=20000, max_file_bytes=32 * 1024 * 1024,
                 max_total_bytes=512 * 1024 * 1024, max_seconds=900.0,
                 max_citation_rows=2000):
        self.max_records = max_records
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes
        self.max_seconds = max_seconds
        self.max_citation_rows = max_citation_rows

    def as_dict(self):
        return {"max_records": self.max_records, "max_file_bytes": self.max_file_bytes,
                "max_total_bytes": self.max_total_bytes, "max_seconds": self.max_seconds,
                "max_citation_rows": self.max_citation_rows}


class BoundExceeded(Exception):
    """Raised instead of quietly folding part of a corpus."""


class Deadline:
    """One clock for the whole build. A bound that only covers discovery is not a
    bound: the expensive phases run after it."""

    def __init__(self, seconds):
        self.seconds = seconds
        self.start = time.monotonic()

    def check(self, phase):
        if time.monotonic() - self.start > self.seconds:
            raise BoundExceeded("the %.0f second bound elapsed during %s" % (self.seconds, phase))

    def elapsed(self):
        return time.monotonic() - self.start


class Record:
    """Exactly ONE byte observation per record. Size, hash, text and parsed document
    all come from the same buffer, so a semantic edge can never be derived from bytes
    that differ from the pinned hash."""

    __slots__ = ("path", "rel", "bytes", "sha", "doc", "text", "parse_error",
                 "stat_size_before", "mtime_ns_before")

    def __init__(self, path: Path, root: Path, bounds: "Bounds"):
        self.path = path
        self.rel = str(path.relative_to(root))
        st = path.stat()
        self.stat_size_before = st.st_size
        self.mtime_ns_before = st.st_mtime_ns
        if st.st_size > bounds.max_file_bytes:
            raise BoundExceeded("%s is %d bytes, above the per-file bound of %d"
                                % (self.rel, st.st_size, bounds.max_file_bytes))
        with path.open("rb") as fh:
            data = fh.read(bounds.max_file_bytes + 1)
        if len(data) > bounds.max_file_bytes:
            raise BoundExceeded("%s grew past the per-file bound while being read" % self.rel)
        self.bytes = len(data)
        self.sha = hashlib.sha256(data).hexdigest()
        self.doc = None
        self.text = ""
        self.parse_error = None
        try:
            self.text = data.decode("utf-8")
        except UnicodeDecodeError:
            self.text = data.decode("utf-8", errors="replace")
        if path.suffix == ".json":
            try:
                self.doc = json.loads(self.text)
            except Exception as exc:  # noqa: BLE001 - recorded, not raised
                self.parse_error = "%s: %s" % (type(exc).__name__, str(exc)[:160])

    def drifted_since_read(self):
        """Did the file change underneath us during this build?"""
        try:
            st = self.path.stat()
        except OSError:
            return "disappeared"
        if st.st_size != self.stat_size_before or st.st_mtime_ns != self.mtime_ns_before:
            return "changed during the build"
        return None


def scan(root: Path, globs, bounds: "Bounds" = None, exclude=(), deadline: "Deadline" = None):
    bounds = bounds or Bounds()
    deadline = deadline or Deadline(bounds.max_seconds)
    records = []
    seen = set()
    total = 0
    excluded = {Path(p).resolve() for p in exclude}
    root_real = Path(root).resolve()
    for pattern in globs:
        if ".." in PurePosixPath(pattern).parts or PurePosixPath(pattern).is_absolute():
            raise BoundExceeded("glob %r may escape the root; patterns must be relative and contain no .."
                                % pattern)
        for path in sorted(root.glob(pattern)):
            if not path.is_file() or path.is_symlink():
                continue
            real = path.resolve()
            if not real.is_relative_to(root_real):
                # a parent directory in the matched path is a link out of the root
                raise BoundExceeded("%s resolves outside the root (%s); refusing to fold it" % (path, real))
            if path in seen or real in excluded:
                continue
            seen.add(path)
            if len(records) >= bounds.max_records:
                raise BoundExceeded("corpus exceeds the record bound of %d; narrow the globs"
                                    % bounds.max_records)
            deadline.check("discovery")
            rec = Record(path, root, bounds)
            total += rec.bytes
            if total > bounds.max_total_bytes:
                raise BoundExceeded("corpus exceeds the total byte bound of %d" % bounds.max_total_bytes)
            records.append(rec)
    return records


def build_index(records):
    by_sha = {}
    by_name = {}
    for rec in records:
        by_sha.setdefault(rec.sha, []).append(rec)
        by_name.setdefault(Path(rec.rel).name, []).append(rec)
    return by_sha, by_name


def derive_edges(records, by_sha, by_name):
    """An edge exists only where a relation-named field names another record."""
    edges = []
    unresolved = []
    for rec in records:
        if rec.doc is None:
            continue
        pairs = []
        _relation_strings(rec.doc, pairs)
        for field, text in pairs:
            targets = []
            for sha in SHA_RE.findall(text):
                for other in by_sha.get(sha, ()):
                    if other.rel != rec.rel:
                        targets.append((other, "sha256"))
            if not targets:
                for name in FILE_RE.findall(text):
                    if name in INDEX_NAMES:
                        continue
                    candidates = [o for o in by_name.get(name, ()) if o.rel != rec.rel]
                    if len(candidates) != 1:
                        # the name is ambiguous in this corpus; a guess would invent a relation
                        if candidates:
                            unresolved.append({"from": rec.rel, "field": field, "name": name,
                                               "reason": "filename is ambiguous across %d records; no edge created"
                                                         % len(candidates)})
                        continue
                    targets.append((candidates[0], "filename"))
            if not targets:
                for sha in SHA_RE.findall(text):
                    unresolved.append({"from": rec.rel, "field": field,
                                       "sha256": sha, "reason": "no record in corpus has this content hash"})
                continue
            for other, how in targets:
                edges.append({"from": rec.rel, "from_sha256": rec.sha, "type": field,
                              "to": other.rel, "to_sha256": other.sha, "matched_by": how,
                              "quote": text[:180]})
    # de-duplicate
    uniq, seen = [], set()
    for e in edges:
        key = (e["from"], e["type"], e["to"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(e)
    return uniq, unresolved


def displaced_set(edges):
    """A record is displaced only when a later record replaces it entirely.

    Conservative on conflict: if A replaces B and B also replaces A, neither is
    displaced. A corpus that contradicts itself about replacement has not told us
    which to skip, and skipping either would be our invention."""
    out = {}
    for e in edges:
        if e["type"] in DISPLACING:
            out.setdefault(e["to"], []).append({"by": e["from"], "relation": e["type"]})
    # Any cycle, of any length, is a corpus contradicting itself about replacement.
    # Tarjan's strongly connected components; every member of a non-trivial
    # component is retained, because skipping any of them would be our invention.
    graph = {}
    for e in edges:
        if e["type"] in DISPLACING:
            graph.setdefault(e["from"], set()).add(e["to"])
            graph.setdefault(e["to"], set())
    index, stack, on_stack, indices, low, components = [0], [], set(), {}, {}, []

    def strongconnect(v):
        work = [(v, iter(sorted(graph.get(v, ()))))]
        indices[v] = low[v] = index[0]; index[0] += 1
        stack.append(v); on_stack.add(v)
        while work:
            node, it = work[-1]
            advanced = False
            for w in it:
                if w not in indices:
                    indices[w] = low[w] = index[0]; index[0] += 1
                    stack.append(w); on_stack.add(w)
                    work.append((w, iter(sorted(graph.get(w, ())))))
                    advanced = True
                    break
                if w in on_stack:
                    low[node] = min(low[node], indices[w])
            if advanced:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[node])
            if low[node] == indices[node]:
                comp = []
                while True:
                    w = stack.pop(); on_stack.discard(w); comp.append(w)
                    if w == node:
                        break
                if len(comp) > 1 or node in graph.get(node, ()):
                    components.append(sorted(comp))

    for v in sorted(graph):
        if v not in indices:
            strongconnect(v)
    conflicted = sorted({m for comp in components for m in comp})
    for rel in conflicted:
        out.pop(rel, None)
    return out, conflicted


def amended_set(edges):
    """Partially amended records stay in the reading set, carrying a pointer."""
    out = {}
    for e in edges:
        if e["type"] in PARTIAL:
            out.setdefault(e["to"], []).append({"by": e["from"], "relation": e["type"]})
    return out



def threads(records, edges):
    """Connected components over the declared edges. A thread is what a reader
    must take together, because its records name one another. Isolated records
    are threads of one: nothing in the corpus relates them to anything else."""
    parent = {r.rel: r.rel for r in records}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for e in edges:
        if e["type"] in THREAD_FORMING and e["from"] in parent and e["to"] in parent:
            union(e["from"], e["to"])
    groups = {}
    for rel in parent:
        groups.setdefault(find(rel), []).append(rel)
    return sorted(groups.values(), key=len, reverse=True)



def cohorts(records, by_sha):
    """Association by shared evidence, which is weaker than declared lineage and
    is kept separate from it. Two records are in one cohort when both cite the
    SHA-256 of the same artifact that is not itself in this corpus. That is a
    fact each record states about itself, not a similarity judgement. A cohort
    says the records speak about the same artifact; it does not say one answers,
    corrects or supersedes another."""
    citations = {}
    for rec in records:
        external = {h for h in SHA_RE.findall(rec.text) if h not in by_sha}
        if external:
            citations[rec.rel] = external
    shared = {}
    for rel, hashes in citations.items():
        for h in hashes:
            shared.setdefault(h, set()).add(rel)
    shared = {h: v for h, v in shared.items() if len(v) >= 2}

    parent = {rel: rel for rel in citations}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for members in shared.values():
        members = sorted(members)
        for other in members[1:]:
            a, b = find(members[0]), find(other)
            if a != b:
                parent[a] = b
    groups = {}
    for rel in parent:
        if any(rel in m for m in shared.values()):
            groups.setdefault(find(rel), []).append(rel)
    sized = sorted(groups.values(), key=len, reverse=True)
    return sized, {h: sorted(v) for h, v in shared.items()}




def write_exclusive(path: Path, data: bytes, replace=False):
    """Create the file through a descriptor, never through a path that something
    else can redirect between the check and the write.

    O_EXCL defeats a symlink or an existing file planted after a check. O_NOFOLLOW
    refuses a symlink at the final component. A hardlink is caught by fstat: a
    regular file with more than one link is some other file too, and writing it
    would modify that one as well."""
    # O_TRUNC must NOT appear here. It would empty the file before fstat could see
    # that it has more than one link, destroying the other path. Truncate only after
    # the descriptor has been inspected.
    # O_RDWR rather than O_WRONLY: the same descriptor is read back after writing,
    # so the bytes verified are the bytes in the file this organ opened.
    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    if not replace:
        flags |= os.O_EXCL
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, flags, 0o644)
    except FileExistsError:
        raise BoundExceeded(
            "%s already exists. This organ publishes new revisions rather than overwriting "
            "earlier output; choose a new path, or pass replace=True deliberately." % path) from None
    except OSError as exc:
        raise BoundExceeded("cannot create %s exclusively: %s" % (path, exc)) from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise BoundExceeded("%s is not a regular file" % path)
        if st.st_nlink != 1:
            raise BoundExceeded(
                "%s has %d hard links; writing it would modify another path as well"
                % (path, st.st_nlink))
        os.ftruncate(fd, 0)
        # os.write may write fewer bytes than asked. Loop until every byte lands,
        # or the file is silently truncated and its hash will not match itself.
        view = memoryview(data)
        written = 0
        while written < len(view):
            n = os.write(fd, view[written:])
            if n <= 0:
                raise BoundExceeded("short write to %s after %d of %d bytes" % (path, written, len(view)))
            written += n
        os.fsync(fd)
        # Read back through the SAME descriptor, so the bytes verified are the bytes
        # in the file this organ opened, not whatever a later path lookup resolves to.
        os.lseek(fd, 0, os.SEEK_SET)
        back = bytearray()
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            back.extend(chunk)
        if bytes(back) != data:
            raise BoundExceeded(
                "readback of %s does not match what was written: %d bytes back against %d written"
                % (path, len(back), len(data)))
    finally:
        os.close(fd)
    return path


def safe_output(out_path: Path, records, root: Path = None, globs=(), label="output"):
    """Refuse to write where it could destroy a record or make the fold fold itself.

    Two dangers, and only these two. Writing ONTO a record destroys it. Writing
    where the globs would pick it up means the next build folds its own output.
    Resolution is on the real path so an alias or symlink cannot smuggle a
    destination past the check."""
    if out_path.is_symlink():
        raise BoundExceeded("%s path is a symlink; refusing to follow it: %s" % (label, out_path))
    resolved = out_path.resolve()
    for rec in records:
        if resolved == rec.path.resolve():
            raise BoundExceeded("%s would overwrite a record in the corpus: %s" % (label, resolved))
    if root is not None and globs:
        try:
            rel = PurePosixPath(resolved.relative_to(Path(root).resolve()).as_posix())
        except ValueError:
            rel = None
        if rel is not None:
            for pattern in globs:
                if rel.full_match(pattern) if hasattr(rel, "full_match") else fnmatch.fnmatch(str(rel), pattern):
                    raise BoundExceeded(
                        "%s at %s matches the corpus pattern %r; the next build would fold its own "
                        "output. Choose a destination the globs do not reach." % (label, rel, pattern))
    if resolved.exists() and not resolved.is_file():
        raise BoundExceeded("%s exists and is not a regular file: %s" % (label, resolved))
    return resolved


def build(root: Path, globs, out_path: Path, title: str, built_at: str, bounds: "Bounds" = None,
          replace=False, built_by="unspecified caller"):
    if not isinstance(built_by, str) or not built_by.strip() or len(built_by) > 128:
        raise BoundExceeded("built_by must be a nonempty caller label of at most 128 characters")
    bounds = bounds or Bounds()
    deadline = Deadline(bounds.max_seconds)
    records = scan(root, globs, bounds, exclude=[out_path, Path(str(out_path)[:-5] + ".md")],
                   deadline=deadline)
    safe_output(out_path, records, root, globs, "fold")
    by_sha, by_name = build_index(records)
    deadline.check("indexing")
    edges, unresolved = derive_edges(records, by_sha, by_name)
    deadline.check("edge derivation")
    displaced, replacement_conflicts = displaced_set(edges)
    amended = amended_set(edges)
    def _check_drift(stage):
        drift = [{"path": r.rel, "observation": d}
                 for r in records for d in [r.drifted_since_read()] if d]
        if drift:
            raise BoundExceeded(
                "the corpus changed while being folded (detected %s), so this fold would bind bytes "
                "that no longer exist: %s. Nothing was published; re-run on a quiet corpus."
                % (stage, ", ".join(d["path"] for d in drift[:5])))
        return drift

    _drift = _check_drift("before deriving content")
    _bytes_by_rel = {r.rel: r.bytes for r in records}
    thread_groups = threads(records, edges)
    deadline.check("cohorts")
    cohort_groups, shared_citations = cohorts(records, by_sha)
    _ordered = sorted(shared_citations.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    shared_citations_kept = {h: v for h, v in _ordered[:bounds.max_citation_rows]}

    total_bytes = sum(r.bytes for r in records)
    reading = [r for r in records if r.rel not in displaced]
    reading_bytes = sum(r.bytes for r in reading)

    entries = []
    for rec in sorted(records, key=lambda r: r.rel):
        d = rec.doc if isinstance(rec.doc, dict) else {}
        entries.append({
            "path": rec.rel,
            "sha256": rec.sha,
            "bytes": rec.bytes,
            "parse_error": rec.parse_error,
            "id": d.get("id"),
            "schema": d.get("schema"),
            "author": d.get("author"),
            "written_at": d.get("written_at_utc") or d.get("written_at_sgt") or d.get("observed_at_utc"),
            "decision": d.get("decision") or d.get("verdict") or d.get("status"),
            "status": "DISPLACED" if rec.rel in displaced else "CURRENT",
            "displaced_by": displaced.get(rec.rel, []),
            "amended_by": amended.get(rec.rel, []),
        })

    fold = {
        "schema": {"name": SCHEMA_NAME, "version": SCHEMA_VERSION,
                   "meaning": "derived reading plan over an append-only corpus; not an authority over its sources",
                   "edge_rule": "an edge exists only where a field whose name states a relation contains another record's content hash, or failing that its filename. Nothing is inferred from similarity, recency or title.",
                   "relation_fields": RELATION_FIELDS,
                   "displacing_relations": sorted(DISPLACING),
                   "partial_relations": sorted(PARTIAL),
                   "partial_rule": "a partial amendment does NOT remove the original from the reading set. The original stays current and carries a pointer to what amended it.",
                   "identity_rule": "size, hash, text and parsed document all come from one read of one buffer, so a semantic edge can never derive from bytes that differ from the pinned hash",
                   "thread_forming_relations": sorted(THREAD_FORMING),
                   "filename_rule": "a filename creates an edge only when exactly one record in the corpus carries that name. Ambiguous names are recorded as unresolved, never guessed.",
                   "index_names_excluded": sorted(INDEX_NAMES)},
        "title": title,
        "built_at_utc": built_at,
        "built_by": built_by,
        "root": str(root),
        "globs": list(globs),
        "threads": [{"size": len(g), "bytes": sum(_bytes_by_rel[m] for m in g),
                     "members": sorted(g)} for g in thread_groups],
        "cohorts": [{"size": len(g), "bytes": sum(_bytes_by_rel[m] for m in g),
                     "members": sorted(g),
                     "basis": "connected component over shared citations; adjacent members share an "
                              "artifact, but two members at distance two need not share any"}
                    for g in cohort_groups],
        "shared_citations": shared_citations_kept,
        "shared_citations_coverage": {
            "total_shared_artifacts": len(shared_citations),
            "retained": len(shared_citations_kept),
            "omitted": len(shared_citations) - len(shared_citations_kept),
            "ordering": "by descending citing-record count, ties broken by hash, so selection is deterministic",
            "warning": "an artifact absent from this table may be uncited, singly cited, OR omitted by the retention bound. artifact lookup says which."},
        "counts": {"records": len(records), "edges": len(edges),
                   "threads": len(thread_groups),
                   "cohorts": len(cohort_groups),
                   "records_in_a_cohort": sum(len(g) for g in cohort_groups),
                   "shared_artifacts": len(shared_citations),
                   "largest_thread": len(thread_groups[0]) if thread_groups else 0,
                   "displaced": len(displaced), "current": len(reading),
                   "unparsable": sum(1 for r in records if r.parse_error),
                   "unresolved_references": len(unresolved)},
        "read_cost": {"corpus_bytes": total_bytes, "current_bytes": reading_bytes,
                      "displaced_bytes": total_bytes - reading_bytes,
                      "fold_bytes": None,
                      "note": "no record is deleted or rewritten. The saving is in what a reader must read to hold current truth, not on disk."},
        "entries": entries,
        "edges": edges,
        "unresolved_references": unresolved,
        "coverage": {
            "bounds": bounds.as_dict(),
            "records_read": len(records),
            "bytes_read": sum(r.bytes for r in records),
            "note": "the organ refuses rather than truncates; if a bound were exceeded this fold would not exist",
        },
        "source_drift_during_build": _drift,
        "replacement_conflicts": replacement_conflicts,
        "partially_amended": [
            {"path": k, "amended_by": v} for k, v in sorted(amended.items())],
        "recovery_contract": {
            "originals": "untouched; the fold is derived data",
            "proof": "every entry carries path, byte count and SHA-256 as observed at build time. verify re-reads the corpus and reports any drift.",
            "deletion": "this organ never deletes. A displaced record stays on disk and is reachable from its successor."},
    }
    # Writing the size into the document changes the size. Converge instead of
    # publishing a number that is two bytes wrong, and record that it converged.
    for _ in range(8):
        raw = canonical(fold)
        if fold["read_cost"]["fold_bytes"] == len(raw):
            break
        fold["read_cost"]["fold_bytes"] = len(raw)
    else:
        fold["read_cost"]["fold_bytes"] = None
        fold["read_cost"]["fold_bytes_note"] = "did not converge; the written file is the authority"
    raw = canonical(fold)
    deadline.check("publication")
    # The last thing before anything is written: the sources must still be the bytes
    # this document describes. Checking only at the start leaves every derived phase
    # and the serialization itself outside the window.
    _check_drift("after serialization, immediately before publication")
    write_exclusive(out_path, raw, replace=replace)
    # elapsed is deliberately NOT inside the fold: a timing value would make the
    # document unreproducible, and reproducibility is worth more than the metric.
    build.last_elapsed_seconds = round(deadline.elapsed(), 3)
    return fold, raw


def verify(root: Path, fold_path: Path):
    """Check the bound records AND re-select the corpus, so a record added since
    the build is reported rather than silently absent from a CLEAN verdict."""
    fold = json.loads(fold_path.read_text(encoding="utf-8"))
    drift, missing, ok = [], [], 0
    rejected = []
    for entry in fold["entries"]:
        rel = PurePosixPath(entry["path"])
        if rel.is_absolute() or any(part in ("..", "") for part in rel.parts):
            rejected.append({"path": entry["path"], "reason": "not canonical root-relative"})
            continue
        path = root / rel
        if path.is_symlink():
            rejected.append({"path": entry["path"], "reason": "is a symlink; not followed"})
            continue
        try:
            if path.exists() and not path.resolve().is_relative_to(Path(root).resolve()):
                rejected.append({"path": entry["path"],
                                 "reason": "resolves outside the root through a parent link"})
                continue
        except OSError as exc:
            rejected.append({"path": entry["path"], "reason": "cannot resolve: %s" % exc})
            continue
        if not path.is_file():
            missing.append(entry["path"])
            continue
        sha = sha256_file(path)
        if sha != entry["sha256"] or path.stat().st_size != entry["bytes"]:
            drift.append({"path": entry["path"], "bound": entry["sha256"], "now": sha})
        else:
            ok += 1
    bound = {e["path"] for e in fold["entries"]}
    added = []
    out_names = {fold_path.resolve(), fold_path.with_suffix(".md").resolve()}
    for pattern in fold.get("globs", []):
        for path in sorted(Path(root).glob(pattern)):
            if not path.is_file() or path.is_symlink():
                continue
            try:
                real = path.resolve()
            except OSError:
                continue
            if real in out_names:
                continue
            if not real.is_relative_to(Path(root).resolve()):
                rejected.append({"path": str(path.relative_to(root)),
                                 "reason": "selected path resolves outside the root"})
                continue
            rel = str(path.relative_to(root))
            if rel not in bound:
                added.append(rel)
    added = sorted(set(added))
    clean = not drift and not missing and not added and not rejected
    return {"checked": len(fold["entries"]), "unchanged": ok,
            "drifted": drift, "missing": missing, "added_since_build": added,
            "rejected_paths": rejected,
            "inventory_scope": "the fold's own globs, re-selected at verify time",
            "verdict": "CLEAN" if clean else "DRIFTED"}


def orient(fold):
    c, rc = fold["counts"], fold["read_cost"]
    lines = ["%s" % fold["title"],
             "built %s by %s" % (fold["built_at_utc"], fold["built_by"]),
             "",
             "%d records, %d edges, %d displaced, %d current" % (c["records"], c["edges"], c["displaced"], c["current"]),
             "corpus %d B -> current set %d B (%.1f%% of the corpus), fold %d B"
             % (rc["corpus_bytes"], rc["current_bytes"],
                100.0 * rc["current_bytes"] / rc["corpus_bytes"] if rc["corpus_bytes"] else 0.0,
                rc["fold_bytes"]),
             ""]
    if c["unparsable"]:
        lines.append("WARNING %d records did not parse as JSON and carry no lineage" % c["unparsable"])
    if c["unresolved_references"]:
        lines.append("%d references name a hash no record in this corpus has" % c["unresolved_references"])
    lines.append("")
    lines.append("displaced records, still on disk, reachable from their successor:")
    for e in fold["entries"]:
        if e["status"] == "DISPLACED":
            for d in e["displaced_by"]:
                lines.append("  %-62s %s by %s" % (Path(e["path"]).name, d["relation"], Path(d["by"]).name))
    return "\n".join(lines)


def plan(fold, query):
    """The records a reader must read, newest first, optionally filtered."""
    rows = [e for e in fold["entries"] if e["status"] == "CURRENT"]
    if query:
        q = query.lower()
        def hay(e):
            return " ".join(str(e.get(k) or "") for k in ("path", "id", "decision", "schema")).lower()
        rows = [e for e in rows if q in hay(e)]
    rows.sort(key=lambda e: (e["written_at"] or "", e["path"]), reverse=True)
    total = sum(e["bytes"] for e in rows)
    out = ["%d records, %d bytes" % (len(rows), total), ""]
    for e in rows:
        out.append("%-10s %7d B  %s" % ((e["written_at"] or "")[:10], e["bytes"], e["path"]))
    return "\n".join(out)




def lint(records, by_sha, by_name):
    """Report which records declare their place in the corpus and which do not.

    A record is LINKED when a relation-named field resolves to another record in
    the corpus. A record is ANCHORED when it at least cites some artifact hash, so
    a cohort can reach it. A record that is neither is an ISLAND: nothing in the
    corpus can lead a reader to it except its filename.

    This reports. It changes nothing and it does not propose edges: guessing a
    relation that a record did not declare is how a fold starts lying."""
    edges, _ = derive_edges(records, by_sha, by_name)
    linked = {e["from"] for e in edges} | {e["to"] for e in edges}
    rows = []
    for rec in records:
        cites_external = any(h not in by_sha for h in SHA_RE.findall(rec.text))
        if rec.rel in linked:
            state = "LINKED"
        elif cites_external:
            state = "ANCHORED"
        else:
            state = "ISLAND"
        rows.append({"path": rec.rel, "bytes": rec.bytes, "state": state,
                     "declares": sorted({e["type"] for e in edges if e["from"] == rec.rel})})
    return rows


def render(fold):
    """A readable current view. Everything here is derived from the fold; nothing is new."""
    c, rc = fold["counts"], fold["read_cost"]
    L = []
    w = L.append
    w("# %s" % fold["title"])
    w("")
    w("Derived reading plan, built %s by %s." % (fold["built_at_utc"], fold["built_by"]))
    w("This document is derived data. It is not an authority over any record it cites,")
    w("and it never replaces one. Every record named here is still on disk, unchanged.")
    w("")
    w("## Size of the problem")
    w("")
    w("| measure | value |")
    w("|---|---|")
    w("| records bound | %d |" % c["records"])
    w("| corpus bytes | %s |" % f"{rc['corpus_bytes']:,}")
    w("| declared edges between records | %d |" % c["edges"])
    w("| records displaced by a later correction | %d |" % c["displaced"])
    w("| bytes a reader can skip as displaced | %s (%.1f%%) |"
      % (f"{rc['displaced_bytes']:,}", 100.0 * rc["displaced_bytes"] / rc["corpus_bytes"] if rc["corpus_bytes"] else 0))
    w("| threads of two or more | %d |" % sum(1 for t in fold.get("threads", []) if t["size"] > 1))
    w("| cohorts by shared cited artifact | %d |" % sum(1 for g in fold.get("cohorts", []) if g["size"] > 1))
    w("| records in at least one cohort | %d |" % c.get("records_in_a_cohort", 0))
    w("| records that parse as JSON | %d |" % (c["records"] - c["unparsable"]))
    w("")
    if c["edges"] * 10 < c["records"]:
        w("**The corpus barely links itself.** %d edges across %d records means almost every"
          % (c["edges"], c["records"]))
        w("record is an island. A fold cannot compact what was never linked, so the saving from")
        w("displacement alone is %.1f percent. Cohorts recover more, because records cite shared"
          % (100.0 * rc["displaced_bytes"] / rc["corpus_bytes"] if rc["corpus_bytes"] else 0))
        w("artifact hashes even when they do not cite each other.")
        w("")
    w("## Declared as fully replaced")
    w("")
    displaced = [e for e in fold["entries"] if e["status"] == "DISPLACED"]
    if not displaced:
        w("No record in this corpus is declared fully replaced by a later one.")
    else:
        w("Each record below is still present and unchanged. A later record declares")
        w("that it corrects or supersedes it, so read the later one first. The claim is")
        w("the later record's, not this view's, and it has not been independently checked.")
        w("")
        w("| record | the later record declares | later record |")
        w("|---|---|---|")
        for e in displaced:
            for d in e["displaced_by"]:
                w("| %s | %s | %s |" % (Path(e["path"]).name, d["relation"], Path(d["by"]).name))
    w("")
    partial = fold.get("partially_amended", [])
    if partial:
        w("")
        w("## Amended in part, and still to be read")
        w("")
        w("A later record withdraws or changes part of these. The rest stands, so they")
        w("remain in the reading set. An earlier revision of this organ wrongly hid them.")
        w("")
        for row in partial:
            for a in row["amended_by"]:
                w("- `%s` amended by `%s`" % (Path(row["path"]).name, Path(a["by"]).name))
    conflicts = fold.get("replacement_conflicts", [])
    if conflicts:
        w("")
        w("## Contradictory replacement declarations")
        w("")
        w("Two records each declare they replace the other. Neither is skipped.")
        for c in conflicts:
            w("- `%s`" % Path(c).name)
    w("")
    w("## Threads: records that declare a relation to one another")
    w("")
    w("A thread is formed only by a strong declared relation: corrects, supersedes, amends,")
    w("derives from, authorized by. A mere mention does not join a thread.")
    w("")
    for t in [x for x in fold.get("threads", []) if x["size"] > 1]:
        w("- **%d records, %s bytes**" % (t["size"], f"{t['bytes']:,}"))
        for m in t["members"]:
            entry = next((e for e in fold["entries"] if e["path"] == m), {})
            mark = " (displaced)" if entry.get("status") == "DISPLACED" else ""
            w("  - `%s`%s" % (Path(m).name, mark))
    w("")
    w("## Cohorts: connected by shared citations")
    w("")
    w("Weaker than a thread and kept separate from it. These are connected components")
    w("over shared citations. Two ADJACENT members cite the same artifact hash. Two")
    w("members further apart need not share any citation at all, so a cohort is a")
    w("chain of overlaps, not a single subject. It says nothing about who corrects whom.")
    w("")
    def area(member):
        """A short, always-defined label for where a record lives."""
        parts = PurePosixPath(member).parts
        return parts[-3] if len(parts) >= 3 else (parts[0] if parts else "?")

    for g in [x for x in fold.get("cohorts", []) if x["size"] > 2][:12]:
        labels = sorted({area(m) for m in g["members"]})
        w("- **%d records, %s bytes**: %s%s"
          % (g["size"], f"{g['bytes']:,}", ", ".join(labels[:6]),
             " and %d more areas" % (len(labels) - 6) if len(labels) > 6 else ""))
    w("")
    w("## How to use this")
    w("")
    w("1. Find the artifact you care about by its hash, then ask which records cite it.")
    w("2. Follow the thread from those records to reach any correction of them.")
    w("3. Read that set first.")
    w("")
    w("What this view cannot tell you: that the rest is irrelevant. A record with no")
    w("declared relation and no cited hash is invisible to every grouping here, and")
    w("may still matter. Omission is an unresolved coverage limit, not a verdict.")
    w("")
    w("## Recovery")
    w("")
    w("Every record is bound by path, byte count and SHA-256 as observed at build time.")
    w("Re-running verify re-reads the corpus and names anything that has drifted. No record")
    w("was rewritten, moved or deleted to produce this view.")
    return "\n".join(L) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Corpus fold: derived reading plan over an append-only record corpus")
    ap.add_argument("--root", default=".")
    ap.add_argument("--fold", default="fold/corpus-fold.json")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="scan the corpus and write the fold")
    b.add_argument("--glob", action="append", required=True)
    b.add_argument("--title", default="Lodestone record corpus")
    b.add_argument("--built-at", required=True, help="ISO 8601 UTC, supplied by the caller")
    b.add_argument("--built-by", default="unspecified caller",
                   help="Caller-supplied attribution label; not authenticated identity")
    b.add_argument("--replace", action="store_true",
                   help="deliberately overwrite an existing fold instead of publishing a new revision")
    sub.add_parser("orient", help="summarise the fold")
    r = sub.add_parser("render", help="write a readable current view beside the fold")
    r.add_argument("--out", default=None)
    r.add_argument("--replace", action="store_true",
                   help="deliberately overwrite an existing view instead of publishing a new one")
    sub.add_parser("verify", help="re-read the corpus and report drift from the fold")
    li = sub.add_parser("lint", help="report which records declare their place in the corpus")
    li.add_argument("--glob", action="append", required=True)
    li.add_argument("--state", default=None, choices=["LINKED", "ANCHORED", "ISLAND"])
    t = sub.add_parser("threads", help="show connected threads, largest first")
    t.add_argument("--min-size", type=int, default=2)
    co = sub.add_parser("cohorts", help="show cohorts associated by shared cited artifacts")
    co.add_argument("--min-size", type=int, default=2)
    ar = sub.add_parser("artifact", help="which records cite one artifact hash")
    ar.add_argument("sha256")
    p = sub.add_parser("plan", help="list the records a reader must actually read")
    p.add_argument("--query", default=None)
    e = sub.add_parser("expand", help="print one bound record and say whether it still matches")
    e.add_argument("path")
    args = ap.parse_args(argv)
    root = Path(args.root).resolve()
    fold_path = root / args.fold if not Path(args.fold).is_absolute() else Path(args.fold)

    if args.cmd == "build":
        fold, raw = build(root, args.glob, fold_path, args.title, args.built_at,
                          replace=args.replace, built_by=args.built_by)
        print(orient(fold))
        print()
        print("fold written to %s (%d B, sha256 %s)" % (fold_path, len(raw), hashlib.sha256(raw).hexdigest()))
        return 0
    fold = json.loads(fold_path.read_text(encoding="utf-8"))
    if args.cmd == "orient":
        print(orient(fold))
    elif args.cmd == "render":
        text = render(fold)
        out = Path(args.out) if args.out else fold_path.with_suffix(".md")
        bound_paths = {root / e["path"] for e in fold["entries"]}
        if out.resolve() in {q.resolve() for q in bound_paths if q.exists()}:
            print("refusing: --out would overwrite a bound record: %s" % out, file=sys.stderr)
            return 2
        try:
            # the same descriptor discipline as the fold itself: a rendered view is
            # also a file something could have replaced since the check above
            write_exclusive(out, text.encode("utf-8"), replace=args.replace)
        except BoundExceeded as exc:
            print("refusing: %s" % exc, file=sys.stderr)
            return 2
        print("wrote %s (%d B)" % (out, len(text.encode())))
    elif args.cmd == "verify":
        result = verify(root, fold_path)
        print(json.dumps(result, indent=1))
        return 0 if result["verdict"] == "CLEAN" else 1
    elif args.cmd == "lint":
        recs = scan(root, args.glob)
        bs, bn = build_index(recs)
        rows = lint(recs, bs, bn)
        counts = {}
        for r in rows:
            counts[r["state"]] = counts.get(r["state"], 0) + 1
        total = len(rows) or 1
        print("%d records: %s" % (len(rows), ", ".join(
            "%s %d (%.0f%%)" % (k, counts.get(k, 0), 100.0 * counts.get(k, 0) / total)
            for k in ("LINKED", "ANCHORED", "ISLAND"))))
        island_bytes = sum(r["bytes"] for r in rows if r["state"] == "ISLAND")
        print("islands hold %s bytes that nothing in the corpus points at" % f"{island_bytes:,}")
        if args.state:
            print()
            for r in sorted((x for x in rows if x["state"] == args.state), key=lambda x: -x["bytes"])[:40]:
                print("  %8d B  %s" % (r["bytes"], r["path"]))
        return 1 if counts.get("ISLAND", 0) else 0
    elif args.cmd == "threads":
        groups = [g for g in fold.get("threads", []) if g["size"] >= args.min_size]
        print("%d threads of %d or more records, %d bytes in total"
              % (len(groups), args.min_size, sum(g["bytes"] for g in groups)))
        for g in groups:
            print("\n  thread of %d records, %d bytes" % (g["size"], g["bytes"]))
            for m in g["members"]:
                print("    %s" % m)
    elif args.cmd == "cohorts":
        groups = [g for g in fold.get("cohorts", []) if g["size"] >= args.min_size]
        print("%d cohorts of %d or more records; association by shared cited artifact, NOT declared lineage"
              % (len(groups), args.min_size))
        for g in groups[:20]:
            print("\n  cohort of %d records, %d bytes" % (g["size"], g["bytes"]))
            for m in g["members"][:14]:
                print("    %s" % m)
            if g["size"] > 14:
                print("    ... and %d more" % (g["size"] - 14))
    elif args.cmd == "artifact":
        table = fold.get("shared_citations", {})
        cov = fold.get("shared_citations_coverage", {})
        hits = table.get(args.sha256)
        if not hits:
            if cov.get("omitted"):
                print("%s is not in the retained citation table. %d of %d shared artifacts were "
                      "omitted by the retention bound, so this may be uncited, singly cited, or omitted."
                      % (args.sha256[:16], cov["omitted"], cov["total_shared_artifacts"]))
            else:
                print("no record in this fold cites %s more than once; coverage is complete, so it is "
                      "uncited or singly cited" % args.sha256[:16])
            return 1
        total = sum(e["bytes"] for e in fold["entries"] if e["path"] in set(hits))
        print("%d records cite %s, %d bytes to read them all" % (len(hits), args.sha256[:16], total))
        for h in hits:
            print("  %s" % h)
    elif args.cmd == "plan":
        print(plan(fold, args.query))
    elif args.cmd == "expand":
        exact = [e for e in fold["entries"] if e["path"] == args.path]
        if exact:
            entry = exact[0]
        else:
            byname = [e for e in fold["entries"] if Path(e["path"]).name == args.path]
            if len(byname) > 1:
                print("refusing: %r names %d bound records. Give the full path:"
                      % (args.path, len(byname)), file=sys.stderr)
                for e in byname:
                    print("  %s" % e["path"], file=sys.stderr)
                return 2
            entry = byname[0] if byname else None
        if entry is None:
            print("no such bound record: %s" % args.path, file=sys.stderr)
            return 2
        rel = PurePosixPath(entry["path"])
        if rel.is_absolute() or any(part in ("..", "") for part in rel.parts):
            print("refusing: bound path is not canonical root-relative: %s" % entry["path"], file=sys.stderr)
            return 2
        path = root / rel
        if path.is_symlink():
            print("refusing: %s is a symlink" % entry["path"], file=sys.stderr)
            return 2
        try:
            if path.exists() and not path.resolve().is_relative_to(Path(root).resolve()):
                print("refusing: %s resolves outside the root through a parent link"
                      % entry["path"], file=sys.stderr)
                return 2
        except OSError as exc:
            print("refusing: cannot resolve %s: %s" % (entry["path"], exc), file=sys.stderr)
            return 2
        if not path.is_file():
            print("MISSING since binding: %s" % entry["path"], file=sys.stderr)
            return 1
        data = path.read_bytes()                      # the exact bytes printed below
        now = hashlib.sha256(data).hexdigest()
        print("# %s" % entry["path"])
        print("# bound %s, now %s, %s" % (entry["sha256"][:16], now[:16],
                                          "UNCHANGED" if now == entry["sha256"] else "CHANGED"))
        print(data.decode("utf-8", errors="replace"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
