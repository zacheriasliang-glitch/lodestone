"""Inject faults into derived copies and check mechanical detection."""
from __future__ import annotations

import copy
from pathlib import Path

from .brief import build_brief, audit_brief


def pressure(root: Path, folded: dict) -> dict:
    """Run bounded adversarial mechanical checks on copies, never source mutation."""
    if type(folded) is not dict:
        raise ValueError("folded must be a dictionary")
    baseline = build_brief(folded)
    checks = []

    def challenge(name: str, state: dict, artifact: str) -> None:
        result = audit_brief(root, state, artifact)
        checks.append({"name": name, "detected": result["verdict"] == "FAIL", "verdict": result["verdict"]})

    challenge("unsupported extra conclusion", folded, baseline + "\nAll source claims are certainly true.\n")
    if folded.get("passages"):
        changed = copy.deepcopy(folded)
        changed["passages"][0]["text"] += " FABRICATED"
        challenge("changed literal text", changed, build_brief(changed))
        changed = copy.deepcopy(folded)
        changed["passages"][0]["source_sha256"] = "0" * 64
        challenge("changed source identity", changed, build_brief(changed))
        changed = copy.deepcopy(folded)
        changed["passages"][0]["start_line"] += 1
        challenge("changed line handle", changed, build_brief(changed))
    return {"checks": checks, "max_cases": 4, "undetected": [c["name"] for c in checks if not c["detected"]],
            "baseline_verdict": audit_brief(root, folded, baseline)["verdict"],
            "skipped_cases": [] if folded.get("passages") else ["No passage exists to mutate"],
            "untested_hypotheses": ["Source truth", "Semantic adequacy", "Cognitive or causal advantage", "General resistance to adversarial input"],
            "scope": "deterministic fault injection into derived copies only"}
