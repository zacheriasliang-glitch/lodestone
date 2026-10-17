#!/usr/bin/env python3
"""check_bezoar.py -- the node registry gate.

A finding is a line in a report. Nobody can pick up a line. A node is an
object: it has an identity, a state, and a condition under which it would
dissolve. Sludge cannot be removed; a stone can be handed to someone.

This tool validates the registry and reconciles it against the findings a
detector actually emits, so the registry cannot quietly fall behind the thing
it is supposed to crystallise.

Laws:
  N1_ID_UNIQUE        every node id matches BZ-nnnn and appears once
  N2_FIELDS_PRESENT   law, subject, first_seen, state, clears_when on each
  N3_STATE_LEGAL      HELD | CLEARED | WAIVED; CLEARED carries cleared_by
                      (a citation), WAIVED carries waived_by (a named owner)
  N4_CLEARING_STATED  clears_when is non-empty. The literal NONE POSSIBLE is
                      legal and means the node can end only in WAIVED or in a
                      law change -- which is the point of stating it
  N5_COVERAGE         (with --findings) every emitted finding is covered by a
                      HELD or WAIVED node, and no CLEARED node's finding is
                      still being emitted

Exit 0 = PASS, exit 2 = ATTENTION, exit 1 = usage error. Stdlib only.

Layout: fields and their continuations are indented; prose sits at column
zero. That is how the tool tells a wrapped clearing condition from a
description, without guessing.

Findings file format, one per line, tab- or double-space separated:

    Y2_ONE_ORGAN<TAB>20260914T043000Z__something__RCP-...

Producing that file from a detector's output is the detector adapter's job,
not this tool's: this tool does not parse any detector's prose.
"""

from __future__ import annotations

import argparse
import os
import re
import sys

NODE_RE = re.compile(r"^##\s+(BZ-\d{4})\s*$")
FIELD_RE = re.compile(r"^\s{0,8}([a-z][a-z0-9_]*)\s*:\s*(\S.*?)\s*$")
KINDS = ("SEED", "NACRE")
SEED_STATES = ("HELD", "CLEARED", "WAIVED")
NACRE_STATES = ("LAYERED",)
COMMON = ("kind", "law", "subject", "first_seen", "state")
NONE_POSSIBLE = "NONE POSSIBLE"
LAYER_RE = re.compile(r"^\s*[-*]\s+(\d{4}-\d{2}-\d{2})\s+\S")


class Finding:
    def __init__(self, law: str, where: str, message: str) -> None:
        self.law, self.where, self.message = law, where, message

    def render(self) -> str:
        return "%-20s %-10s %s" % (self.law, self.where, self.message)


class Node:
    def __init__(self, node_id: str, line: int) -> None:
        self.id = node_id
        self.line = line
        self.fields: dict[str, str] = {}
        self.subjects: list[str] = []
        self.layers: list[str] = []
        self.prose: list[str] = []

    @property
    def kind(self) -> str:
        return self.fields.get("kind", "")

    @property
    def state(self) -> str:
        return self.fields.get("state", "")

    def covers(self, law: str, subject: str) -> bool:
        return self.fields.get("law", "") == law and subject in self.subjects


def parse_registry(text: str) -> tuple[list[Node], list[str]]:
    nodes: list[Node] = []
    duplicates: list[str] = []
    seen: set[str] = set()
    node = None
    fenced = False

    for number, raw in enumerate(text.splitlines(), start=1):
        if raw.strip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue

        match = NODE_RE.match(raw)
        if match:
            node_id = match.group(1)
            if node_id in seen:
                duplicates.append(node_id)
            seen.add(node_id)
            node = Node(node_id, number)
            nodes.append(node)
            continue

        if node is None:
            continue

        layer = LAYER_RE.match(raw)
        if layer:
            node.layers.append(raw.strip())
            continue

        field = FIELD_RE.match(raw)
        if field and field.group(1) in (
            *COMMON, "clears_when", "adage", "cleared_by", "waived_by", "note",
        ):
            name, value = field.group(1), field.group(2)
            if name == "subject":
                node.subjects.extend(
                    part.strip() for part in value.split(",") if part.strip()
                )
                node.fields.setdefault("subject", value)
            else:
                node.fields.setdefault(name, value)
        elif raw.strip() and not raw[:1].isspace():
            node.prose.append(raw.strip())

    return nodes, duplicates


def parse_findings(text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "\t" in line:
            law, _, subject = line.partition("\t")
        else:
            law, _, subject = line.partition("  ")
        law, subject = law.strip(), subject.strip()
        if law and subject:
            out.append((law, subject))
    return out


def check(nodes: list[Node], duplicates: list[str],
          findings: list[tuple[str, str]] | None) -> list[Finding]:
    out: list[Finding] = []

    for node_id in duplicates:
        out.append(Finding("N1_ID_UNIQUE", node_id, "id appears more than once"))

    for node in nodes:
        kind = node.kind
        state = node.state

        for name in COMMON:
            if name not in node.fields:
                out.append(
                    Finding("N2_FIELDS_PRESENT", node.id,
                            "missing '%s:' (block at line %d)" % (name, node.line))
                )
        if kind and kind not in KINDS:
            out.append(
                Finding("N2_FIELDS_PRESENT", node.id,
                        "kind '%s' is not one of %s" % (kind, " / ".join(KINDS)))
            )
        if not node.prose:
            out.append(
                Finding("N2_FIELDS_PRESENT", node.id,
                        "no prose: a node with no description is a label, not an object")
            )
        if len(node.subjects) > 1 and not node.prose:
            out.append(
                Finding("N2_FIELDS_PRESENT", node.id,
                        "%d subjects merged with no stated common cause"
                        % len(node.subjects))
            )

        if kind == "SEED":
            if "clears_when" not in node.fields:
                out.append(
                    Finding("N2_FIELDS_PRESENT", node.id,
                            "a SEED needs 'clears_when:' -- it is open by definition")
                )
            if state and state not in SEED_STATES:
                out.append(
                    Finding("N3_STATE_LEGAL", node.id,
                            "SEED state '%s' is not one of %s"
                            % (state, " / ".join(SEED_STATES)))
                )
            if state == "CLEARED" and not node.fields.get("cleared_by"):
                out.append(
                    Finding("N3_STATE_LEGAL", node.id,
                            "CLEARED without 'cleared_by:' -- a node does not clear by say-so")
                )
            if state == "WAIVED" and not node.fields.get("waived_by"):
                out.append(
                    Finding("N3_STATE_LEGAL", node.id,
                            "WAIVED without 'waived_by:' -- a waiver needs an owner")
                )
            clears = node.fields.get("clears_when", "")
            if clears == NONE_POSSIBLE and state == "CLEARED":
                out.append(
                    Finding("N4_CLEARING_STATED", node.id,
                            "clears_when is NONE POSSIBLE but the node is CLEARED")
                )

        elif kind == "NACRE":
            if state and state not in NACRE_STATES:
                out.append(
                    Finding("N3_STATE_LEGAL", node.id,
                            "NACRE state '%s' is not %s -- a pearl is not cleared or waived, "
                            "it is kept" % (state, NACRE_STATES[0]))
                )
            if "adage" not in node.fields:
                out.append(
                    Finding("N2_FIELDS_PRESENT", node.id,
                            "a NACRE needs 'adage:' -- the lesson is the reason it is kept")
                )
            if "clears_when" in node.fields:
                out.append(
                    Finding("N4_CLEARING_STATED", node.id,
                            "a NACRE has no clears_when: the mistake already happened")
                )
            if len(node.layers) < 2:
                out.append(
                    Finding("N6_NACRE_LAYERED", node.id,
                            "%d dated layer(s): one instance is an accident and belongs "
                            "in the receipt history, not in a pearl" % len(node.layers))
                )

    if findings is None:
        return out

    for law, subject in sorted(set(findings)):
        holders = [n for n in nodes if n.covers(law, subject)]
        seeds_open = [
            n for n in holders if n.kind == "SEED" and n.state in ("HELD", "WAIVED")
        ]
        if seeds_open:
            continue
        cleared = [n for n in holders if n.kind == "SEED" and n.state == "CLEARED"]
        if cleared:
            out.append(
                Finding("N5_COVERAGE", cleared[0].id,
                        "SEED is CLEARED but its finding is still emitted: %s on %s"
                        % (law, subject))
            )
            continue
        pearls = [n for n in holders if n.kind == "NACRE"]
        if pearls:
            out.append(
                Finding("N5_COVERAGE", pearls[0].id,
                        "NACRE holds a finding that is still emitted: %s on %s -- a live "
                        "matter wants a crystal, not an archive" % (law, subject))
            )
            continue
        out.append(
            Finding("N5_COVERAGE", "-",
                    "finding held by no node: %s on %s" % (law, subject))
        )

    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate the node registry and reconcile it against emitted findings."
    )
    parser.add_argument("registry", help="path to the node registry")
    parser.add_argument(
        "--findings",
        help="path to a findings file (law<TAB>subject per line) produced by the "
             "detector adapter; without it, N5 is not checked",
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    if not os.path.isfile(args.registry):
        sys.stderr.write("check_bezoar: no such registry: %s\n" % args.registry)
        return 1

    with open(args.registry, "r", encoding="utf-8") as handle:
        nodes, duplicates = parse_registry(handle.read())

    findings = None
    if args.findings:
        if not os.path.isfile(args.findings):
            sys.stderr.write("check_bezoar: no such findings file: %s\n" % args.findings)
            return 1
        with open(args.findings, "r", encoding="utf-8") as handle:
            findings = parse_findings(handle.read())

    results = check(nodes, duplicates, findings)

    if not args.quiet:
        seeds = [n for n in nodes if n.kind == "SEED"]
        pearls = [n for n in nodes if n.kind == "NACRE"]
        held = sum(1 for n in seeds if n.state == "HELD")
        waived = sum(1 for n in seeds if n.state == "WAIVED")
        cleared = sum(1 for n in seeds if n.state == "CLEARED")
        layers = sum(len(n.layers) for n in pearls)
        print("registry:  %s" % args.registry)
        print("SEED:      %d   HELD %d   WAIVED %d   CLEARED %d"
              % (len(seeds), held, waived, cleared))
        print("NACRE:     %d   layers %d" % (len(pearls), layers))
        if findings is not None:
            print("findings:  %d emitted, %d distinct" % (len(findings), len(set(findings))))
        else:
            print("findings:  not supplied -- N5 unchecked")
        print()

    if results:
        print("FINDINGS")
        for item in results:
            print(item.render())
        print()
        print("ATTENTION: %d finding(s)." % len(results))
        return 2

    print("PASS: every node is well formed and every emitted finding is held by a seed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
