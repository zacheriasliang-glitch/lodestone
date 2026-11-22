"""Lifecycle lineage tree over the append-only receipt corpus.

Yggdrasil reads `receipts/` as a record of separated Host-authorized lifecycles
and reports whether that record satisfies four stated laws. It invents no new
address type: every field it reads is one the corpus already writes -- the
filename triple, the declared `mode`, and the declared predecessor path set.

This module certifies nothing. A PASS verdict is a statement about the receipt
record's internal shape, not about any operation's technical correctness, any
design's merit, or any Host authorization. It grants no authority to act.

Laws, and the claimed law each mechanizes:

  Y1 ONE_TRUNK     no two lifecycles declare the same predecessor set
                   ("not one Hydra from within")
  Y2 ONE_ORGAN     a declared mode is exactly one value from the policy set
                   (one selected organ per lifecycle, node.md:90-126)
  Y3 CLOSED_PREFIX a declared predecessor set is exactly the set of lifecycles
                   preceding it in the total order (append-only custody)
  Y4 ONE_HEAD      the declared subgraph presents exactly one head
                   ("one voxel from outside")

Join rule (Host ruling 2026-09-20; docs/yggdrasil/join-rule-v0.1.0.md): a
receipt may declare two predecessor sets and name the newest lifecycle of
each as a predecessor. Y1 excuses two lifecycles that share a set when a later
join names both; Y3 reads a join's closed prefix as the union of its
predecessors' prefixes and does not require the later predecessor to list the
earlier; Y4 counts a join as one head and its predecessors as none. A join naming fewer than two distinct preceding lifecycles is malformed
and is reported under IDENTITY.

Coverage is reported, never assumed. The corpus is heterogeneous: older
receipts predate the JSON form and many declare no predecessor set at all. A
lifecycle that declares nothing for a law is UNDECLARED for that law and is
excluded from it rather than counted as a violation -- and rather than given a
derived value, which for Y1 would make the law vacuously true. Silence is
reported as silence; only a declaration that contradicts a law is a finding.
"""
from __future__ import annotations

import json
from pathlib import Path
import re

VERSION = "0.2.0"

# Allowed modes are the Universal Receipt Policy v1.0.0 set, not an invented one.
MODES = ("read-only", "plan-only", "write", "rollback", "verification")
LAWS = ("UNREAD", "IDENTITY", "Y1_ONE_TRUNK", "Y2_ONE_ORGAN", "Y3_CLOSED_PREFIX", "Y4_ONE_HEAD")
UNREAD_DETAIL = "receipt exceeds the read bound and was not tested against any law"

# <UTC-basic-timestamp>__<operation-slug>__<receipt-id>.md
FILENAME = re.compile(r"^(?P<stamp>\d{8}T\d{6}Z)__(?P<slug>.+?)__(?P<rid>RCP-[0-9A-Za-z-]+)\.md$")
CANONICAL_ID = re.compile(r"^RCP-\d{8}T\d{6}Z-[0-9a-f]{12}$")

JSON_BLOCK = re.compile(r"```json\s*(\{.*?\})\s*```", re.S)
PATH_SET_KEY = "receipt_directory_path_set_before_creation"
JOIN_SETS_KEY = "receipt_directory_path_sets_before_creation"
JOIN_PREDECESSORS_KEY = "join_predecessors"
JOIN_DETAIL = "join must name two distinct predecessors, each the newest lifecycle of its own declared set"
SECTION = re.compile(r"^#{1,6}\s")

# Within a declaration section a path appears backticked, bulleted, bare on its
# own line inside a fenced block, or as a Markdown link target. All four forms
# read the same way, so the terminator must exclude link and list punctuation
# as well as whitespace -- otherwise `](../receipts/x.md)` yields a path with a
# trailing parenthesis that matches no file and is reported as absent.
RECEIPT_PATH = re.compile(r"receipts/[^\s`)\]>,;]+")

# `- mode: `write``, `- Mode: IMPLEMENT`, `- `mode`: write`, bare `mode: write`,
# and any of those followed by a parenthetical qualifying the declaration.
# A purely numeric value is a filesystem permission from a hash block, never an
# operation mode, so the value must open with a letter to be read as one.
MD_MODE = re.compile(r"^[-*]?\s*`?mode`?\s*:\s*`?([A-Za-z][A-Za-z_-]*)`?\s*(?:\([^\n]*)?\s*$",
                     re.M | re.I)

# A section body states its mode as a leading token, sometimes followed by prose.
LEADING_MODE = re.compile(r"^[\s\-*`]*([A-Za-z][A-Za-z0-9_-]*)")

# A receipt states its mode near the top, before the long evidence sections. The
# fallback scan is bounded to that region so that a mode quoted inside a later
# section -- a rollback_procedure narrating a rollback receipt, or observed
# inputs citing a prior operation -- is not read as a second declaration.
MODE_SCAN_LINES = 40

MAX_RECEIPT_BYTES = 8 * 1024 * 1024


def _section_lines(text, key, heading_only=False):
    """Return the lines of the `## <key>` section, or of a `- <key>:` block.

    `heading_only` suppresses the bullet form. A bullet declaration is a single
    line, so reading it as a section would swallow the lines after it and hide
    any second declaration of the same key.

    The two forms end differently. A heading section runs to the next heading.
    A bullet declaration is that line plus any indented or bulleted continuation
    immediately under it, and ends at the first blank or unindented line -- so
    ordinary prose after the bullet is not absorbed into the declaration.
    """
    heading = re.compile(r"^#{1,6}\s*" + re.escape(key) + r"\s*$", re.M).search(text)
    if heading is not None:
        body = []
        for line in text[heading.end():].splitlines():
            if SECTION.match(line):
                break
            body.append(line)
        return body
    if heading_only:
        return None
    bullet = re.compile(r"^[-*]\s*`?" + re.escape(key) + r"`?\s*:", re.M).search(text)
    if bullet is None:
        return None
    lines = text[bullet.end():].splitlines()
    body = [lines[0]] if lines else []
    for line in lines[1:]:
        if not line.strip() or not line.startswith((" ", "\t", "-", "*")):
            break
        body.append(line)
    return body


def _declared_paths(text, document):
    """Return the declared predecessor path set, or None when none is declared.

    A JSON document is authoritative only for what it actually declares. When
    it carries no path set the Markdown section is still read, because a
    receipt may state its custody in prose while carrying JSON for something
    else entirely -- hashes, an inventory, a quoted prior record. Treating the
    JSON as terminal there would report such a receipt as declaring nothing,
    and a lifecycle that declares nothing is excluded from Y1, Y3 and Y4. A
    single unrelated JSON block would then switch those three laws off for that
    receipt, silently, including a fork Y1 exists to catch.
    """
    if document is not None:
        value = document.get(PATH_SET_KEY)
        if value is not None:
            if type(value) is not list or any(type(item) is not str for item in value):
                return []
            return sorted(set(value))
    body = _section_lines(text, PATH_SET_KEY)
    if body is None:
        return None
    return sorted(set(RECEIPT_PATH.findall("\n".join(body))))


def _newest_lifecycle(paths):
    """The newest lifecycle-shaped path in a declared set, by the filename triple."""
    candidates = []
    for path in paths:
        match = FILENAME.match(path.rsplit("/", 1)[-1])
        if match:
            candidates.append(((match.group("stamp"), match.group("rid")), path))
    return max(candidates)[1] if candidates else None


def _declared_join(document, problems):
    """Return a well-formed join declaration from the JSON document, or None.

    A join is declared only in JSON: two predecessor sets and the two
    predecessors they name. Each predecessor must be the newest lifecycle of
    its own set, and the two must differ. Anything else is recorded as a
    problem so that a malformed join cannot quietly excuse a fork.
    """
    if document is None or (JOIN_SETS_KEY not in document and JOIN_PREDECESSORS_KEY not in document):
        return None
    sets, predecessors = document.get(JOIN_SETS_KEY), document.get(JOIN_PREDECESSORS_KEY)
    well_formed = (type(sets) is list and len(sets) == 2
                   and all(type(s) is list and all(type(i) is str for i in s) for s in sets)
                   and type(predecessors) is list and len(predecessors) == 2
                   and all(type(x) is str for x in predecessors) and predecessors[0] != predecessors[1]
                   and all(_newest_lifecycle(sets[i]) == predecessors[i] for i in (0, 1)))
    if not well_formed:
        problems.append(JOIN_DETAIL)
        return None
    return {"predecessors": list(predecessors), "sets": [sorted(set(s)) for s in sets]}


def _first_mode(body):
    """Read the mode token that opens a declaration section, ignoring prose."""
    for line in body:
        if not line.strip() or line.lstrip().startswith("```"):
            continue
        found = LEADING_MODE.match(line)
        return found.group(1) if found else None
    return None


def _declared_modes(text, document):
    """Return the distinct operation modes this receipt declares, in order.

    Distinctness is judged case-insensitively so that `write` and `Write` are
    one declaration, while membership in the policy set stays exact: `READ_ONLY`
    is a different value from `read-only` and remains a finding.
    """
    if document is not None and type(document.get("mode")) is str:
        return (document["mode"],)
    body = _section_lines(text, "mode", heading_only=True)
    if body is not None:
        opening = _first_mode(body)
        if opening is not None:
            return (opening,)
    found, seen = [], set()
    for value in MD_MODE.findall("\n".join(text.splitlines()[:MODE_SCAN_LINES])):
        if value.casefold() not in seen:
            seen.add(value.casefold())
            found.append(value)
    return tuple(found)


def _receipt_document(text, receipt_id, problems):
    """Return the receipt's own JSON document, or None when it carries none.

    A receipt may contain several fenced JSON blocks -- an inventory, a hash
    table, a quoted prior record. The receipt's own document is identified by
    content, not by being first in the file: taking the first block lets a
    quoted record supply this receipt's mode and identity. A block declaring
    this receipt's own identifier is decisive; otherwise a block that declares
    custody, then any block shaped like a receipt, is preferred in that order.
    """
    custody, shaped = None, None
    for block in JSON_BLOCK.finditer(text):
        try:
            parsed = json.loads(block.group(1))
        except ValueError:
            problems.append("json block does not parse")
            continue
        if type(parsed) is not dict:
            continue
        if parsed.get("receipt_id") == receipt_id:
            return parsed
        if custody is None and (PATH_SET_KEY in parsed or JOIN_SETS_KEY in parsed):
            custody = parsed
        if shaped is None and ("receipt_id" in parsed or "operation_name" in parsed):
            shaped = parsed
    return custody if custody is not None else shaped


class Lifecycle:
    """One conforming receipt, read as one lifecycle with an address."""

    __slots__ = ("path", "stamp", "slug", "receipt_id", "modes", "declared", "problems", "unread", "join")

    def __init__(self, path, stamp, slug, receipt_id, modes, declared, problems, unread=False, join=None):
        self.path = path
        self.stamp = stamp
        self.slug = slug
        self.receipt_id = receipt_id
        self.modes = modes
        self.declared = declared
        self.problems = problems
        self.unread = unread
        self.join = join

    @property
    def order_key(self):
        return (self.stamp, self.receipt_id)

    @property
    def mode(self):
        return self.modes[0] if len(self.modes) == 1 else None

    def as_json(self):
        return {"path": self.path, "stamp": self.stamp, "slug": self.slug,
                "receipt_id": self.receipt_id, "mode": self.mode,
                "declares_custody": self.declared is not None,
                "declared_count": None if self.declared is None else len(self.declared),
                "join_predecessors": None if self.join is None else list(self.join["predecessors"])}


def read_lifecycle(path, root):
    """Parse one conforming receipt into a Lifecycle.

    Malformed content is reported, never raised: an unparsable JSON block, a
    non-canonical identifier and an oversized file all return a Lifecycle
    carrying the problem. The caller supplies a path whose name matches
    FILENAME; `load` selects those, and an unreadable file still raises OSError.
    """
    relative = path.relative_to(root).as_posix()
    problems = []
    match = FILENAME.match(path.name)
    stamp, slug, receipt_id = match.group("stamp"), match.group("slug"), match.group("rid")
    if not CANONICAL_ID.match(receipt_id):
        problems.append("receipt id is not RCP-<UTC-basic-timestamp>-<12 lowercase hex>")
    raw = path.read_bytes()
    if len(raw) > MAX_RECEIPT_BYTES:
        return Lifecycle(relative, stamp, slug, receipt_id, (), None,
                         problems + [UNREAD_DETAIL], unread=True)
    text = raw.decode("utf-8", errors="replace")
    document = _receipt_document(text, receipt_id, problems)
    if document is not None and document.get("receipt_id") not in (None, receipt_id):
        problems.append("declared receipt_id does not match the filename")

    join = _declared_join(document, problems)
    declared = _declared_paths(text, document)
    if join is not None:
        if declared is not None:
            problems.append("join must not also declare a single predecessor set")
        declared = sorted(set(join["sets"][0]) | set(join["sets"][1]))
    elif declared is None and document is not None and type(document.get(JOIN_SETS_KEY)) is list:
        # A malformed join still declares custody: the union of whatever it listed.
        declared = sorted({item for group in document[JOIN_SETS_KEY] if type(group) is list
                           for item in group if type(item) is str})
    return Lifecycle(relative, stamp, slug, receipt_id,
                     _declared_modes(text, document), declared, problems, join=join)


def load(root):
    """Read receipts under <root>/receipts, returning lifecycles and other files.

    A file in the receipts tree whose name does not match the naming rule is not
    a lifecycle. It is returned separately so it is neither silently dropped nor
    counted as a malformed lifecycle.
    """
    base = Path(root)
    directory = base / "receipts"
    if not directory.is_dir():
        raise ValueError("no receipts directory under the supplied root")
    lifecycles, others = [], []
    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue
        if FILENAME.match(path.name):
            lifecycles.append(read_lifecycle(path, base))
        else:
            others.append(path.relative_to(base).as_posix())
    lifecycles.sort(key=lambda entry: entry.order_key)
    return lifecycles, others


def _prefix_of(entry, lifecycles, position, lifecycle_paths):
    """The lifecycles an entry stands on: its declared set when it declares one, else its positional prefix."""
    if entry.declared is not None:
        return {path for path in entry.declared if path in lifecycle_paths}
    return {other.path for other in lifecycles[:position[entry.path]]}


def verify(lifecycles, others=()):
    """Apply the four laws and return a deterministic verdict document."""
    findings = []
    lifecycle_paths = {entry.path for entry in lifecycles}
    by_path = {entry.path: entry for entry in lifecycles}
    declared = [entry for entry in lifecycles if entry.declared is not None]
    position = {entry.path: index for index, entry in enumerate(lifecycles)}
    unread = [entry.path for entry in lifecycles if entry.unread]
    undeclared_mode = [entry.path for entry in lifecycles
                       if not entry.modes and not entry.unread]
    carried, absent = set(), set()

    for entry in lifecycles:
        for problem in entry.problems:
            law = "UNREAD" if problem == UNREAD_DETAIL else "IDENTITY"
            findings.append({"law": law, "receipt": entry.path, "detail": problem})

    # Y2: a declaration that names no policy mode, or names more than one.
    for entry in lifecycles:
        if not entry.modes:
            continue
        if len(entry.modes) > 1:
            findings.append({"law": "Y2_ONE_ORGAN", "receipt": entry.path,
                             "detail": f"declares {len(entry.modes)} distinct modes: {', '.join(entry.modes)}"})
        elif entry.modes[0] not in MODES:
            findings.append({"law": "Y2_ONE_ORGAN", "receipt": entry.path,
                             "detail": f"mode {entry.modes[0]!r} is outside the policy set"})

    # Joins: a later lifecycle naming two preceding lifecycles as its predecessors.
    joins = []
    for entry in declared:
        if entry.join is None:
            continue
        if all(p in position and position[p] < position[entry.path] for p in entry.join["predecessors"]):
            joins.append({"receipt": entry.path, "predecessors": list(entry.join["predecessors"])})
        else:
            findings.append({"law": "IDENTITY", "receipt": entry.path,
                             "detail": "join names a predecessor that is not a preceding lifecycle"})
    valid_joins = {item["receipt"] for item in joins}
    joined = {frozenset(item["predecessors"]): item["receipt"] for item in joins}
    # The later of two joined predecessors could not have seen the earlier one:
    # its closed prefix is read without it.
    excused_omissions = {}
    for pair, join_path in joined.items():
        earlier, later = sorted(pair, key=lambda path: position[path])
        if position[join_path] > position[later]:
            excused_omissions.setdefault(later, set()).add(earlier)

    # Y1: two lifecycles declaring the same predecessor set are two heads on one
    # trunk -- unless a later join names exactly those two as its predecessors.
    seen = {}
    for entry in declared:
        signature = ("join", *entry.join["predecessors"]) if entry.path in valid_joins else tuple(entry.declared)
        if signature in seen:
            pair = frozenset((seen[signature], entry.path))
            if pair in joined and position[joined[pair]] > position[entry.path]:
                continue
            findings.append({"law": "Y1_ONE_TRUNK", "receipt": entry.path,
                             "detail": f"declares the same predecessor set as {seen[signature]}"})
        else:
            seen[signature] = entry.path

    # Y3: a declared set must be exactly the lifecycles preceding it. A join's
    # closed prefix is the union of its predecessors' prefixes plus themselves.
    # Entries naming files that are not lifecycles are carried, and reported separately.
    for entry in declared:
        if entry.path in valid_joins:
            expected = set()
            for predecessor in entry.join["predecessors"]:
                expected |= _prefix_of(by_path[predecessor], lifecycles, position, lifecycle_paths) | {predecessor}
        else:
            expected = {other.path for other in lifecycles[:position[entry.path]]}
            expected -= excused_omissions.get(entry.path, set())
        actual = set(entry.declared)
        for path in sorted(actual - lifecycle_paths):
            (carried if path in others else absent).add(path)
        for missing in sorted(expected - actual):
            findings.append({"law": "Y3_CLOSED_PREFIX", "receipt": entry.path,
                             "detail": f"omits the preceding lifecycle {missing}"})
        for extra in sorted((actual & lifecycle_paths) - expected):
            findings.append({"law": "Y3_CLOSED_PREFIX", "receipt": entry.path,
                             "detail": f"names a lifecycle that does not precede it: {extra}"})

    # Y4: over the declared subgraph, exactly one lifecycle is nobody's predecessor.
    # A join counts as one head and makes both of its predecessors parents.
    heads = []
    if declared:
        parents = set()
        for entry in declared:
            if entry.path in valid_joins:
                parents.update(entry.join["predecessors"])
                continue
            candidates = [path for path in entry.declared if path in position]
            if candidates:
                parents.add(max(candidates, key=lambda path: (position[path], path)))
        heads = sorted(entry.path for entry in declared if entry.path not in parents)
        if len(heads) != 1:
            findings.append({"law": "Y4_ONE_HEAD", "receipt": "",
                             "detail": f"declared subgraph presents {len(heads)} heads"})

    findings.sort(key=lambda item: (LAWS.index(item["law"]), item["receipt"], item["detail"]))
    return {"version": VERSION,
            "verdict": "VIOLATION" if findings else "PASS",
            "lifecycles": len(lifecycles),
            "declaring_custody": len(declared),
            "coverage": {"Y1_ONE_TRUNK": len(declared),
                         "Y2_ONE_ORGAN": len(lifecycles) - len(undeclared_mode) - len(unread),
                         "Y3_CLOSED_PREFIX": len(declared), "Y4_ONE_HEAD": len(declared)},
            "unread": sorted(unread),
            "undeclared_mode": sorted(undeclared_mode),
            "non_lifecycle_files": sorted(others),
            "carried_non_lifecycle_entries": sorted(carried),
            "absent_declared_entries": sorted(absent),
            "joins": joins,
            "heads": heads,
            "counts": {law: sum(1 for item in findings if item["law"] == law) for law in LAWS},
            "findings": findings}


def check(root):
    """Load the corpus at <root> and return its verdict document."""
    lifecycles, others = load(root)
    return verify(lifecycles, others)
