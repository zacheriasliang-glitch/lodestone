#!/usr/bin/env python3
"""check_brief.py -- the brief gate.

A brief is an instruction one body writes and hands to another. This tool
refuses to let a brief travel unless every clause it states is bound
byte-for-byte to the preregistration it claims to serve.

It exists because on 2026-09-20 a brief written from a summary of a
preregistration, rather than from the preregistration, reached a consumer
body twice and voided two runs of SF-01. Nothing in the lab checked the
brief, because the lab had no organ that read one. Every other gate guards
a body. This one guards what is handed to a body.

Laws:
  B1_SOURCE_PINNED       the header names a preregistration path and its
                         SHA-256; that file exists and hashes to that value
  B2_QUOTES_EXACT        every quoted span appears byte-for-byte in that file
  B3_CLAUSE_SHAPE        every clause block carries at least one quote and at
                         least one instruction, and no instruction sits
                         outside a clause block or the declared additions
  B4_ADDITIONS_DECLARED  a "Declared additions" section is present and not
                         empty (write "none" when there are none)
  B5_IN_CUSTODY          the brief is tracked by git and its worktree is clean

Exit 0 = PASS. Exit 2 = ATTENTION, findings printed. Exit 1 = usage error.

Stdlib only. Reads; writes nothing. No network. No subprocess except git,
and only when --custody is in force (the default); --no-custody drops B5.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys

SECTION_RE = re.compile(r"^##\s+(.*?)\s*$")
CLAUSE_RE = re.compile(r"^###\s+(.*?)\s*$")
HEADER_RE = re.compile(r"^\s{0,4}([a-z][a-z0-9_]*)\s*:\s*(\S.*?)\s*$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

BOUND = "bound clauses"
ADDITIONS = "declared additions"


class Finding:
    def __init__(self, law: str, where: str, message: str, detail: str = "") -> None:
        self.law = law
        self.where = where
        self.message = message
        self.detail = detail

    def render(self) -> str:
        head = "%-22s %-18s %s" % (self.law, self.where, self.message)
        if self.detail:
            body = "\n".join("    " + line for line in self.detail.splitlines())
            return head + "\n" + body
        return head


class Quote:
    def __init__(self, clause: str, index: int, text: str, line: int) -> None:
        self.clause = clause
        self.index = index
        self.text = text
        self.line = line


class Clause:
    def __init__(self, label: str, line: int) -> None:
        self.label = label
        self.line = line
        self.quotes: list[Quote] = []
        self.instructions: list[int] = []


class Brief:
    def __init__(self) -> None:
        self.header: dict[str, str] = {}
        self.clauses: list[Clause] = []
        self.additions: list[str] = []
        self.has_bound_section = False
        self.has_additions_section = False
        self.stray_instructions: list[int] = []


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 16), b""):
            h.update(block)
    return h.hexdigest()


def is_prose(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if stripped.startswith(">"):
        return False
    if stripped.startswith("#"):
        return False
    if stripped.startswith("```"):
        return False
    return True


def parse(text: str) -> Brief:
    """Parse a brief. Structure only; no judgement about content."""
    brief = Brief()
    lines = text.splitlines()
    section = None          # None = header region
    clause = None
    fenced = False
    pending: list[str] = []  # contiguous run of quote lines
    pending_line = 0

    def flush_quote() -> None:
        nonlocal pending, pending_line
        if pending and clause is not None:
            clause.quotes.append(
                Quote(clause.label, len(clause.quotes) + 1, "\n".join(pending), pending_line)
            )
        pending = []
        pending_line = 0

    for number, raw in enumerate(lines, start=1):
        if raw.strip().startswith("```"):
            if not fenced:
                flush_quote()
            fenced = not fenced
            continue

        if fenced:
            continue

        section_match = SECTION_RE.match(raw)
        if section_match:
            flush_quote()
            clause = None
            section = section_match.group(1).strip().lower().rstrip(":")
            if section == BOUND:
                brief.has_bound_section = True
            elif section == ADDITIONS:
                brief.has_additions_section = True
            continue

        clause_match = CLAUSE_RE.match(raw)
        if clause_match and section == BOUND:
            flush_quote()
            clause = Clause(clause_match.group(1).strip(), number)
            brief.clauses.append(clause)
            continue

        if section is None:
            header_match = HEADER_RE.match(raw)
            if header_match and not raw.lstrip().startswith(">"):
                brief.header.setdefault(header_match.group(1), header_match.group(2))
            continue

        stripped = raw.strip()

        if stripped.startswith(">"):
            body = raw.lstrip()[1:]
            if body.startswith(" "):
                body = body[1:]
            if not pending:
                pending_line = number
            pending.append(body.rstrip("\n"))
            continue

        flush_quote()

        if section == ADDITIONS:
            if stripped:
                brief.additions.append(stripped)
            continue

        if section == BOUND and is_prose(raw):
            if clause is None:
                brief.stray_instructions.append(number)
            else:
                clause.instructions.append(number)

    flush_quote()
    return brief


def check_b1(brief: Brief, brief_path: str, findings: list[Finding]) -> str | None:
    """Returns the resolved preregistration path when the pin holds."""
    declared = brief.header.get("prereg")
    digest = brief.header.get("prereg_sha256")

    if not declared:
        findings.append(Finding("B1_SOURCE_PINNED", "header", "no 'prereg:' line"))
    if not digest:
        findings.append(Finding("B1_SOURCE_PINNED", "header", "no 'prereg_sha256:' line"))
    if not declared or not digest:
        return None

    if not SHA256_RE.match(digest):
        findings.append(
            Finding("B1_SOURCE_PINNED", "header", "prereg_sha256 is not 64 lowercase hex")
        )
        return None

    root = os.path.dirname(os.path.abspath(brief_path))
    candidates = [declared]
    if not os.path.isabs(declared):
        candidates = [os.path.join(os.getcwd(), declared), os.path.join(root, declared)]

    resolved = next((c for c in candidates if os.path.isfile(c)), None)
    if resolved is None:
        findings.append(
            Finding("B1_SOURCE_PINNED", "header", "preregistration not found", declared)
        )
        return None

    actual = sha256_file(resolved)
    if actual != digest:
        findings.append(
            Finding(
                "B1_SOURCE_PINNED",
                "header",
                "preregistration has changed since the brief was written",
                "declared %s\nactual   %s\nSTALE_SOURCE: rewrite the brief against the file as it is now."
                % (digest, actual),
            )
        )
        return None

    return resolved


def check_b2(brief: Brief, prereg_text: str | None, findings: list[Finding]) -> None:
    if prereg_text is None:
        for clause in brief.clauses:
            for quote in clause.quotes:
                findings.append(
                    Finding(
                        "B2_QUOTES_EXACT",
                        "%s q%d" % (clause.label, quote.index),
                        "not checkable: the source is unpinned (see B1)",
                    )
                )
        return

    normalized_source = "\n".join(line.rstrip() for line in prereg_text.splitlines())

    for clause in brief.clauses:
        for quote in clause.quotes:
            if quote.text in prereg_text:
                continue
            normalized_quote = "\n".join(line.rstrip() for line in quote.text.splitlines())
            if normalized_quote and normalized_quote in normalized_source:
                findings.append(
                    Finding(
                        "B2_QUOTES_EXACT",
                        "%s q%d" % (clause.label, quote.index),
                        "matches only after trailing whitespace is dropped (line %d)" % quote.line,
                        "Copy the bytes, do not retype them.",
                    )
                )
                continue
            findings.append(
                Finding(
                    "B2_QUOTES_EXACT",
                    "%s q%d" % (clause.label, quote.index),
                    "quoted text does not appear in the preregistration (line %d)" % quote.line,
                    _excerpt(quote.text),
                )
            )


def _excerpt(text: str, width: int = 96) -> str:
    flat = " ".join(text.split())
    if len(flat) > width:
        flat = flat[: width - 1] + "…"
    return 'quoted: "%s"' % flat


def check_b3(brief: Brief, findings: list[Finding]) -> None:
    if not brief.has_bound_section:
        findings.append(
            Finding("B3_CLAUSE_SHAPE", "document", "no '## Bound clauses' section")
        )
    if brief.has_bound_section and not brief.clauses:
        findings.append(
            Finding("B3_CLAUSE_SHAPE", "bound clauses", "section holds no '### <clause>' block")
        )
    for clause in brief.clauses:
        if not clause.quotes:
            findings.append(
                Finding(
                    "B3_CLAUSE_SHAPE",
                    clause.label,
                    "clause states an instruction with nothing quoted behind it (line %d)"
                    % clause.line,
                )
            )
        if not clause.instructions:
            findings.append(
                Finding(
                    "B3_CLAUSE_SHAPE",
                    clause.label,
                    "clause quotes the source but instructs nobody (line %d)" % clause.line,
                )
            )
    for number in brief.stray_instructions:
        findings.append(
            Finding(
                "B3_CLAUSE_SHAPE",
                "line %d" % number,
                "instruction sits outside any clause block",
            )
        )


def check_b4(brief: Brief, findings: list[Finding]) -> None:
    if not brief.has_additions_section:
        findings.append(
            Finding(
                "B4_ADDITIONS_DECLARED",
                "document",
                "no '## Declared additions' section",
                'Write "none" when the brief adds nothing to the preregistration.',
            )
        )
        return
    if not brief.additions:
        findings.append(
            Finding(
                "B4_ADDITIONS_DECLARED",
                "declared additions",
                'section is empty; write "none" rather than leaving it blank',
            )
        )


def check_b5(brief_path: str, findings: list[Finding]) -> None:
    directory = os.path.dirname(os.path.abspath(brief_path)) or "."
    try:
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", os.path.abspath(brief_path)],
            cwd=directory,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as error:
        findings.append(
            Finding("B5_IN_CUSTODY", "git", "custody not determinable", str(error))
        )
        return

    if tracked.returncode != 0:
        findings.append(
            Finding(
                "B5_IN_CUSTODY",
                "git",
                "brief is not tracked; commit it before it travels",
                "An untracked brief is not evidence. /tmp is not custody.",
            )
        )
        return

    status = subprocess.run(
        ["git", "status", "--porcelain", "--", os.path.abspath(brief_path)],
        cwd=directory,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if status.stdout.strip():
        findings.append(
            Finding(
                "B5_IN_CUSTODY",
                "git",
                "brief has uncommitted changes; the relayed text would not be the committed text",
                status.stdout.strip(),
            )
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Refuse a brief that is not bound to its preregistration."
    )
    parser.add_argument("brief", help="path to the brief")
    parser.add_argument(
        "--no-custody",
        action="store_true",
        help="drop B5 (use only when checking a brief outside a repository)",
    )
    parser.add_argument("--quiet", action="store_true", help="print findings only")
    args = parser.parse_args(argv)

    if not os.path.isfile(args.brief):
        sys.stderr.write("check_brief: no such brief: %s\n" % args.brief)
        return 1

    with open(args.brief, "r", encoding="utf-8") as handle:
        text = handle.read()

    brief = parse(text)
    findings: list[Finding] = []

    prereg_path = check_b1(brief, args.brief, findings)
    prereg_text = None
    if prereg_path is not None:
        with open(prereg_path, "r", encoding="utf-8") as handle:
            prereg_text = handle.read()

    check_b2(brief, prereg_text, findings)
    check_b3(brief, findings)
    check_b4(brief, findings)
    if not args.no_custody:
        check_b5(args.brief, findings)

    quote_count = sum(len(clause.quotes) for clause in brief.clauses)

    if not args.quiet:
        print("brief:     %s" % args.brief)
        print(
            "prereg:    %s"
            % (
                "%s (%s…)" % (prereg_path, brief.header.get("prereg_sha256", "")[:12])
                if prereg_path
                else "UNPINNED"
            )
        )
        for key in ("run_id", "audience"):
            if key in brief.header:
                print("%-10s %s" % (key + ":", brief.header[key]))
        print(
            "clauses:   %d   quotes: %d   declared additions: %d"
            % (len(brief.clauses), quote_count, len(brief.additions))
        )
        print()

    if findings:
        print("FINDINGS")
        for finding in findings:
            print(finding.render())
        print()
        print("ATTENTION: %d finding(s). This brief does not travel." % len(findings))
        return 2

    print("PASS: every clause is bound to the preregistration. This brief may travel.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
