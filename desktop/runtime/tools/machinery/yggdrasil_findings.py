#!/usr/bin/env python3
"""yggdrasil_findings.py -- the Yggdrasil adapter for the node registry gate.

Runs Yggdrasil the way this repository runs it (``tools/machinery/run yggdrasil
--root <root> --findings``), or reads a saved report, and emits one
``law<TAB>subject`` line per finding: nothing else on the line, no prose,
stably sorted by (law, subject) with the detector's own order kept for ties.

The subject is the receipt path exactly as the finding names it. Paths are
what Yggdrasil reports, they carry the receipt id inside them, and they stay
usable when the id itself is the defect (IDENTITY). The one finding that names
no receipt, Y4_ONE_HEAD, is about the declared subgraph as a whole; its
subject is that subgraph's path, ``receipts/``.

check_bezoar splits a node's subjects on commas, so a subject containing a
comma, a tab or a newline is refused here rather than silently mis-matched.

Exit 0 = lines emitted (a VIOLATION verdict is still a success for the
adapter), 1 = the report could not be produced or read.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

RUNNER = Path(__file__).resolve().with_name("run")
SUBGRAPH_SUBJECT = "receipts/"
FORBIDDEN = ("\t", ",", "\n", "\r")


def subject_of(finding: dict) -> str:
    """The identifier a finding names; the declared subgraph when it names none."""
    receipt = finding.get("receipt")
    if type(receipt) is not str:
        raise ValueError("finding has no receipt field: " + json.dumps(finding)[:200])
    subject = receipt.strip() or SUBGRAPH_SUBJECT
    if any(mark in subject for mark in FORBIDDEN):
        raise ValueError("subject cannot be matched exactly by the registry: " + repr(subject))
    return subject


def lines_from_report(report: dict) -> list[str]:
    """One law<TAB>subject line per emitted finding, stably sorted by (law, subject)."""
    if type(report) is not dict or type(report.get("findings")) is not list:
        raise ValueError("report carries no findings list; run Yggdrasil with --findings")
    pairs = []
    for finding in report["findings"]:
        law = finding.get("law")
        if type(law) is not str or not law.strip() or any(mark in law for mark in FORBIDDEN):
            raise ValueError("finding has no usable law: " + json.dumps(finding)[:200])
        pairs.append((law.strip(), subject_of(finding)))
    return [law + "\t" + subject for law, subject in sorted(pairs)]


def run_yggdrasil(root: str) -> dict:
    """The repository's own runner, findings included; exit 2 is a VIOLATION verdict, not an error."""
    done = subprocess.run([sys.executable, "-B", str(RUNNER), "yggdrasil", "--root", root, "--findings"],
                          capture_output=True, text=True, timeout=600)
    if done.returncode not in (0, 2) or not done.stdout.strip():
        raise ValueError("Yggdrasil did not produce a report: " + done.stderr.strip()[:1000])
    return json.loads(done.stdout)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Emit Yggdrasil findings as law<TAB>subject lines for check_bezoar.")
    parser.add_argument("--root", default=".", help="Repository root holding the receipts directory")
    parser.add_argument("--report", help="Read a saved Yggdrasil --findings report (path, or - for stdin) instead of running it")
    parser.add_argument("--output", help="Write lines here instead of stdout")
    args = parser.parse_args(argv)
    try:
        if args.report == "-":
            report = json.load(sys.stdin)
        elif args.report:
            report = json.loads(Path(args.report).read_text(encoding="utf-8"))
        else:
            report = run_yggdrasil(args.root)
        lines = lines_from_report(report)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print("yggdrasil_findings: " + str(exc)[:1000], file=sys.stderr)
        return 1
    text = "".join(line + "\n" for line in lines)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
