"""Deterministic, proposal-only Kernel 20 orchestration phenotype."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from .contracts import (
    Bounds,
    OrchestrationDecision,
    ROUTES,
    _validate_audit_finding_dict,
    compute_decision_id,
    validate_decision,
    validate_json_schema,
)


ENGINE_NAME = "K20_ORCHESTRATION_PHENOTYPE"
ENGINE_VERSION = "1.0.0"
SAFE_TIE_PRECEDENCE = ("STOP", "HOST_APPROVAL", "AUDITOR", "BUILDER")
LAYER_PRECEDENCE = (
    "SAFETY_LAYER",
    "PROVENANCE_LAYER",
    "ADMISSION_LAYER",
    "AUDIT_LAYER",
    "DEVELOPMENT_LAYER",
    "PLANNING_LAYER",
)
_FINDING_HEADING = "## AUDIT_FINDING"
_FINDING_BLOCK_KEYS = {"schema_version", "producer_role", "verdict", "load_bearing_gap"}
_RETURN_FINDING_HEADING = "audit_finding:"
_AUDIT_FINDING_FIELDS = (
    "schema_version",
    "producer_role",
    "verdict",
    "load_bearing_gap",
    "source_path",
    "source_byte_count",
    "source_sha256",
)
_SOURCE_FRESHNESS_CHECKS = (
    "source_path_relative",
    "source_path_inside_trusted_root",
    "source_path_admissible",
    "source_file_present",
    "source_file_readable",
    "source_byte_count",
    "source_sha256",
)

# Known instruction surfaces. A role prompt, template, or configuration file
# explains what a report must contain; it is never itself an authoritative
# Auditor report. The menu is not the dinner.
_REPORT_SUFFIXES = (".md",)
_INSTRUCTION_SURFACE_DIRECTORIES = (
    ".github",
    ".claude",
    ".vscode",
    "orchestrator/prompts",
    "orchestrator/config",
    "orchestrator/schemas",
)
_INSTRUCTION_SURFACE_NAMES = (
    "AGENTS.md",
    "CLAUDE.md",
    "node.md",
    "LABORATORY_CONSTITUTION.md",
    "INTERACTION_PROTOCOL.md",
    "UNIVERSAL_RECEIPT_POLICY_v1.0.0.md",
)
_INSTRUCTION_SURFACE_SUFFIXES = (
    ".agent.md",
    ".template.md",
    ".example.md",
    ".sample.md",
)


def audit_finding_report_admissibility(relative_posix: str) -> str | None:
    """Explain why a repository-relative path is inadmissible as report evidence.

    Returns ``None`` when the path may carry an authoritative Auditor report.
    Shared by the ingress loader and by later source-freshness rechecks so one
    policy governs both.
    """

    path = PurePosixPath(relative_posix)
    name = path.name
    if not name:
        return f"audit finding report has no file name: {relative_posix!r}"
    if path.suffix.lower() not in _REPORT_SUFFIXES:
        return (
            "audit finding report must be a Markdown report artifact: "
            f"{relative_posix!r}"
        )
    lowered = name.lower()
    if any(lowered.endswith(suffix) for suffix in _INSTRUCTION_SURFACE_SUFFIXES):
        return (
            "instruction, template, or configuration surfaces cannot be finding-report "
            f"evidence: {relative_posix!r}"
        )
    if name in _INSTRUCTION_SURFACE_NAMES:
        return (
            "instruction, template, or configuration surfaces cannot be finding-report "
            f"evidence: {relative_posix!r}"
        )
    parts = path.parts
    for directory in _INSTRUCTION_SURFACE_DIRECTORIES:
        prefix = PurePosixPath(directory).parts
        if parts[: len(prefix)] == prefix:
            return (
                "instruction, template, or configuration surfaces cannot be "
                f"finding-report evidence: {relative_posix!r}"
            )
    return None


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"AUDIT_FINDING JSON has duplicate key: {key!r}")
        result[key] = value
    return result


def load_audit_finding(repository_root: Path, report_path: str) -> dict[str, Any]:
    """Load and hash-bind exactly one AUDIT_FINDING block from a saved report.

    The single validated ingress path. Fails closed on any missing, duplicate,
    out-of-repository, instruction-surface, non-UTF-8, malformed-fence, or
    schema-violating input.
    """

    resolved_root = repository_root.resolve()
    candidate = (repository_root / report_path).resolve()
    try:
        relative = candidate.relative_to(resolved_root)
    except ValueError as error:
        raise ValueError(
            f"audit finding report must be inside the repository: {report_path!r}"
        ) from error
    inadmissible = audit_finding_report_admissibility(relative.as_posix())
    if inadmissible is not None:
        raise ValueError(inadmissible)
    if not candidate.is_file():
        raise ValueError(f"audit finding report not found: {report_path!r}")

    raw_bytes = candidate.read_bytes()
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"audit finding report is not valid UTF-8: {error}") from error

    lines = text.splitlines()
    headings = [index for index, line in enumerate(lines) if line == _FINDING_HEADING]
    if len(headings) == 0:
        raise ValueError("report contains no ## AUDIT_FINDING block")
    if len(headings) > 1:
        raise ValueError("report contains more than one ## AUDIT_FINDING block")
    start = headings[0]
    if start + 1 >= len(lines) or lines[start + 1] != "```json":
        raise ValueError("## AUDIT_FINDING must be immediately followed by a ```json line")

    body: list[str] = []
    closed = False
    for line in lines[start + 2:]:
        if line == "```":
            closed = True
            break
        body.append(line)
    if not closed:
        raise ValueError("AUDIT_FINDING json fence has no closing ``` line")
    if not body:
        raise ValueError("AUDIT_FINDING json fence is empty")

    try:
        block = json.loads("\n".join(body), object_pairs_hook=_reject_duplicate_json_keys)
    except (json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"AUDIT_FINDING block is not valid JSON: {error}") from error
    if not isinstance(block, dict):
        raise ValueError("AUDIT_FINDING block must be a JSON object")
    if block.keys() != _FINDING_BLOCK_KEYS:
        raise ValueError(
            "AUDIT_FINDING block must declare exactly: "
            + ", ".join(sorted(_FINDING_BLOCK_KEYS))
        )

    finding = {
        **block,
        "source_path": candidate.relative_to(resolved_root).as_posix(),
        "source_byte_count": len(raw_bytes),
        "source_sha256": hashlib.sha256(raw_bytes).hexdigest(),
    }
    _validate_audit_finding_dict(finding)
    return finding


def parse_return_audit_finding(receipt_text: str) -> dict[str, Any] | None:
    """Parse the optional exact decision-owned finding projection in a receipt."""

    lines = receipt_text.splitlines()
    headings = [
        index
        for index, line in enumerate(lines)
        if line.strip().lower() == _RETURN_FINDING_HEADING
    ]
    if not headings:
        return None
    if len(headings) > 1:
        raise ValueError("receipt contains more than one audit_finding block")
    start = headings[0]
    if start + 1 >= len(lines) or lines[start + 1].strip() != "```json":
        raise ValueError("receipt audit_finding must be immediately followed by a ```json line")

    body: list[str] = []
    closed = False
    for line in lines[start + 2:]:
        if line.strip() == "```":
            closed = True
            break
        body.append(line)
    if not closed:
        raise ValueError("receipt audit_finding JSON fence has no closing ``` line")
    if not body:
        raise ValueError("receipt audit_finding JSON fence is empty")

    try:
        finding = json.loads("\n".join(body), object_pairs_hook=_reject_duplicate_json_keys)
    except (json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"receipt audit_finding is not valid JSON: {error}") from error
    _validate_audit_finding_dict(finding)
    return finding


def audit_finding_comparisons(
    expected: dict[str, Any] | None,
    returned: dict[str, Any] | None,
) -> tuple[list[str], int]:
    """Return exact semantic differences and how many comparisons were performed.

    One traversal produces both results, so the mismatch report and the
    normalization count can never drift apart. Assigns no route or authority.
    """

    if expected is None:
        if returned is None:
            return [], 0
        return ["return audit_finding is not owned by the prior decision"], 1
    try:
        _validate_audit_finding_dict(expected)
    except ValueError as error:
        return [f"prior decision audit_finding is invalid: {error}"], 1
    if returned is None:
        return ["return audit_finding is missing"], 1
    mismatches = [
        f"return audit_finding {field} mismatch"
        for field in _AUDIT_FINDING_FIELDS
        if returned[field] != expected[field]
    ]
    return mismatches, len(_AUDIT_FINDING_FIELDS)


def audit_finding_mismatches(
    expected: dict[str, Any] | None,
    returned: dict[str, Any] | None,
) -> list[str]:
    """Return exact semantic differences without assigning route or authority."""

    return audit_finding_comparisons(expected, returned)[0]


def _has_symlink_below(candidate: Path, root: Path) -> bool:
    """Detect a symlink at or under the trusted root on the way to ``candidate``."""

    current = candidate
    while True:
        if current.is_symlink():
            return True
        if current == root or current == current.parent:
            return False
        current = current.parent


def audit_finding_source_state(
    repository_root: Path | str | None,
    finding: dict[str, Any] | None,
) -> tuple[list[str], int]:
    """Recheck a decision-owned finding's *current* source bytes, fail-closed.

    A decision binds ``source_path``, ``source_byte_count``, and
    ``source_sha256`` when it is created. Those bytes can change afterwards, so
    return verification and reconciliation resolve the decision-owned source
    against the trusted repository root and compare it with what exists now.
    Returns ``(issues, checks_performed)``; checks stop at the first failed gate
    because later gates cannot be evaluated.
    """

    if finding is None:
        return [], 0

    performed = 0
    relative = finding.get("source_path")

    performed += 1  # source_path_relative
    if not isinstance(relative, str) or not relative.strip():
        return ["audit_finding source_path is not a usable repository-relative path"], performed
    raw = PurePosixPath(relative)
    if raw.is_absolute() or ".." in raw.parts:
        return [
            f"audit_finding source_path is not repository-relative: {relative!r}"
        ], performed

    performed += 1  # source_path_inside_trusted_root
    if repository_root is None:
        return [
            "audit_finding source cannot be rechecked without the trusted repository root"
        ], performed
    root = Path(repository_root).resolve()
    candidate = root / Path(relative)
    if _has_symlink_below(candidate, root):
        return [
            f"audit_finding source path is a substituted or symlinked path: {relative!r}"
        ], performed
    try:
        resolved = candidate.resolve()
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return [
            f"audit_finding source escapes the trusted repository root: {relative!r}"
        ], performed

    performed += 1  # source_path_admissible
    inadmissible = audit_finding_report_admissibility(raw.as_posix())
    if inadmissible is not None:
        return [inadmissible], performed

    performed += 1  # source_file_present
    if not resolved.is_file():
        return [f"audit_finding source is missing from the trusted body: {relative!r}"], performed

    performed += 1  # source_file_readable
    try:
        data = resolved.read_bytes()
    except OSError as error:
        return [f"audit_finding source is unreadable: {relative!r} ({error})"], performed

    issues: list[str] = []
    performed += 1  # source_byte_count
    if len(data) != finding.get("source_byte_count"):
        issues.append(
            "audit_finding source_byte_count no longer matches the current source: "
            f"{relative!r}"
        )
    performed += 1  # source_sha256
    if hashlib.sha256(data).hexdigest() != finding.get("source_sha256"):
        issues.append(
            f"audit_finding source_sha256 no longer matches the current source: {relative!r}"
        )
    return issues, performed


@dataclass(frozen=True)
class Observation:
    experiment_dir: Path | None
    experiment_files: list[str]
    recent_receipts: list[str]
    current_state_text: str
    current_state_sha256: str | None
    evidence: list[str]
    missing: list[str]
    contradictions: list[str]
    affordances: list[str]
    provenance_complete: bool
    implementation_receipt_exists: bool
    audit_receipt_exists: bool
    already_complete: bool
    intended_targets: list[str]
    protected_overlap: bool


def _contains_any(text: str, words: Iterable[str]) -> bool:
    return any(
        re.search(
            rf"(?<![A-Za-z0-9]){re.escape(word)}(?![A-Za-z0-9])",
            text,
            flags=re.IGNORECASE,
        )
        for word in words
    )


def _is_subpath(candidate: Path, container: Path) -> bool:
    try:
        candidate.resolve().relative_to(container.resolve())
        return True
    except ValueError:
        return False


def _path_is_protected(target: str, protected: str) -> bool:
    """Treat numbered protected stems (experiments/009) as guarding 009-* too."""

    target_path = Path(target)
    protected_path = Path(protected)
    try:
        target_path.relative_to(protected_path)
        return True
    except ValueError:
        pass
    return (
        target_path.parent == protected_path.parent
        and protected_path.name.isdigit()
        and target_path.name.startswith(f"{protected_path.name}-")
    )


def select_route(route_scores: dict[str, float], threshold: float) -> str:
    """Select the strongest sufficient route; resolve exact ties by safe precedence."""

    sufficient = {route: float(score) for route, score in route_scores.items() if score >= threshold}
    candidates = sufficient or {route: float(score) for route, score in route_scores.items()}
    best = max(candidates.values())
    tied = {route for route, score in candidates.items() if score == best}
    return next(route for route in SAFE_TIE_PRECEDENCE if route in tied)


class Kernel20Phenotype:
    """A bounded repository observer and deterministic task-packet planner."""

    def __init__(self, repository_root: Path, policy: dict[str, Any]):
        self.repository_root = repository_root.resolve()
        self.orchestrator_root = (self.repository_root / "orchestrator").resolve()
        if not self.orchestrator_root.is_relative_to(self.repository_root):
            raise ValueError("orchestrator directory escapes the project root")
        self.protected_paths = sorted({str(item).strip("/") for item in policy.get("protectedPaths", [])})
        bounds_config = policy.get("kernel20Bounds", {})
        if "maxRecentReceipts" in policy and "max_receipts" not in bounds_config:
            bounds_config = {**bounds_config, "max_receipts": policy["maxRecentReceipts"]}
        self.bounds = Bounds.from_mapping(bounds_config)
        self.output_dir = str(policy.get("outputDir", "orchestrator/output"))
        output_path = (self.repository_root / self.output_dir).resolve()
        if not _is_subpath(output_path, self.orchestrator_root):
            raise ValueError("Kernel 20 output directory must remain inside orchestrator/")
        self.output_path = output_path

    def _find_experiment(self, experiment: str) -> Path | None:
        root = self.repository_root / "experiments"
        if not root.is_dir():
            return None
        matches = sorted(
            item for item in root.iterdir()
            if item.is_dir() and item.name.startswith(f"{experiment}-")
            # Candidate shelves contain historical documents, not runnable experiments.
            and item.name != f"{experiment}-candidate"
        )
        if len(matches) > 1:
            raise ValueError(f"ambiguous experiment selector {experiment}")
        return matches[0] if matches else None

    def _list_recent_receipts(self) -> list[str]:
        receipt_root = self.repository_root / "receipts"
        if not receipt_root.is_dir():
            return []
        entries = [
            (item.stat().st_mtime_ns, item.name)
            for item in receipt_root.iterdir()
            if item.is_file()
        ]
        entries.sort(key=lambda item: (-item[0], item[1]))
        return [name for _, name in entries[: self.bounds.max_receipts]]

    def observe(self, experiment: str) -> Observation:
        evidence: list[str] = []
        missing: list[str] = []
        contradictions: list[str] = []
        affordances: list[str] = []

        state_path = self.repository_root / "CURRENT_STATE.md"
        if state_path.is_file():
            state_text = state_path.read_text(encoding="utf-8")
            state_hash = hashlib.sha256(state_text.encode("utf-8")).hexdigest()
            evidence.append(f"CURRENT_STATE.md exists with SHA-256 {state_hash}")
            affordances.append("current state exists")
        else:
            state_text = ""
            state_hash = None
            missing.append("CURRENT_STATE.md")

        experiment_dir = self._find_experiment(experiment)
        experiment_files: list[str] = []
        intended_targets: list[str] = [f"experiments/{experiment}"]
        if experiment_dir is None:
            missing.append(f"experiment directory for {experiment}")
        else:
            experiment_rel = str(experiment_dir.relative_to(self.repository_root))
            intended_targets = [experiment_rel]
            experiment_files = [
                str(item.relative_to(self.repository_root))
                for item in sorted(experiment_dir.rglob("*"))
                if item.is_file()
            ][: self.bounds.max_files]
            evidence.append(f"experiment directory exists: {experiment_rel}")
            evidence.append(f"bounded experiment file count inspected: {len(experiment_files)}")
            affordances.append("experiment directory exists")
            if os.access(experiment_dir, os.W_OK):
                affordances.append("target path is writable")

        recent_receipts = self._list_recent_receipts()
        if recent_receipts:
            evidence.append(f"bounded recent receipt count inspected: {len(recent_receipts)}")
            affordances.append("receipt exists")
        else:
            missing.append("receipt directory entries")

        for name, label in (("Builder", "Builder is available"), ("Auditor", "Auditor is available")):
            if (self.repository_root / name).exists():
                affordances.append(label)

        source_dir = self.repository_root / "sources" / f"experiment-{experiment}"
        source_requirements = (
            ("SOURCE_MANIFEST.json", "manifest exists"),
            ("SOURCE_DECLARATION.md", "source declaration exists"),
            ("SHA256SUMS.txt", "checksum file exists"),
        )
        source_presence = []
        for filename, label in source_requirements:
            present = (source_dir / filename).is_file()
            source_presence.append(present)
            if present:
                affordances.append(label)
            else:
                missing.append(f"sources/experiment-{experiment}/{filename}")
        provenance_complete = all(source_presence)

        receipt_key = f"experiment-{experiment}"
        relevant_receipts = [name.lower() for name in recent_receipts if receipt_key in name.lower()]
        implementation_receipt_exists = any(
            _contains_any(name, ("implement", "import", "create", "build"))
            for name in relevant_receipts
        )
        audit_receipt_exists = any(
            _contains_any(name, ("audit", "verify", "verification"))
            for name in relevant_receipts
        )

        state_lines = [
            line.lower() for line in state_text.splitlines()
            if f"experiment {experiment}" in line.lower() or f"experiment_{experiment}" in line.lower()
        ]
        has_not_begun = any("not begun" in line for line in state_lines)
        has_complete = any("complete" in line for line in state_lines)
        if has_not_begun and has_complete:
            contradictions.append(f"Experiment {experiment} is described as both not begun and complete")
        completion_marker = f"EXPERIMENT_{experiment}_COMPLETE"
        already_complete = completion_marker in state_text
        if already_complete:
            affordances.append("task is already complete")

        protected_overlap = any(
            _path_is_protected(target, protected)
            for target in intended_targets
            for protected in self.protected_paths
        )
        if protected_overlap:
            affordances.append("protected path overlap exists")

        return Observation(
            experiment_dir=experiment_dir,
            experiment_files=experiment_files,
            recent_receipts=recent_receipts,
            current_state_text=state_text,
            current_state_sha256=state_hash,
            evidence=sorted(evidence),
            missing=sorted(set(missing)),
            contradictions=sorted(contradictions),
            affordances=sorted(set(affordances)),
            provenance_complete=provenance_complete,
            implementation_receipt_exists=implementation_receipt_exists,
            audit_receipt_exists=audit_receipt_exists,
            already_complete=already_complete,
            intended_targets=intended_targets,
            protected_overlap=protected_overlap,
        )

    def _classify_problem(self, goal: str) -> tuple[str, list[str], str]:
        goal_lower = goal.lower()
        candidates: list[str] = []
        if _contains_any(goal_lower, ("provenance", "checksum")):
            candidates.append("PROVENANCE")
        if _contains_any(goal_lower, ("audit", "verify", "inspect")):
            candidates.append("AUDIT")
        if _contains_any(goal_lower, ("admit", "canonicalize", "authorize", "close")):
            candidates.append("ADMISSION")
        if _contains_any(goal_lower, ("recover", "repair", "reconcile", "recalibrate")):
            candidates.append("RECOVERY")
        if _contains_any(goal_lower, ("implement", "create", "fix", "import", "build")):
            candidates.append("IMPLEMENTATION")
        if _contains_any(goal_lower, ("plan", "propose", "prepare")):
            candidates.append("PLANNING")
        priority = ("PROVENANCE", "AUDIT", "ADMISSION", "RECOVERY", "IMPLEMENTATION", "PLANNING")
        primary = next((item for item in priority if item in candidates), "UNKNOWN")
        rivals = [item for item in priority if item in candidates and item != primary]
        if not rivals and primary != "UNKNOWN":
            fallback = {
                "PROVENANCE": "AUDIT", "AUDIT": "PLANNING", "ADMISSION": "AUDIT",
                "RECOVERY": "AUDIT", "IMPLEMENTATION": "AUDIT", "PLANNING": "UNKNOWN",
            }
            rivals = [fallback[primary]]
        falsifier = (
            f"Direct repository evidence that the goal is principally {rivals[0]} rather than {primary}."
            if rivals else "A host-supplied bounded goal that maps unambiguously to a supported problem type."
        )
        return primary, rivals, falsifier

    def _score_routes(self, goal: str, primary: str, obs: Observation) -> dict[str, float]:
        scores = {route: 0.0 for route in ROUTES}
        explicit_admission = _contains_any(goal, ("admit", "canonicalize", "authorize"))
        closure = _contains_any(goal, ("close",))
        no_action = _contains_any(goal, ("no action", "already complete", "nothing to do"))

        if primary in {"AUDIT", "PROVENANCE", "RECOVERY"}:
            scores["AUDITOR"] += 0.90
        elif primary == "IMPLEMENTATION":
            scores["BUILDER"] += 0.90
        elif primary == "PLANNING":
            scores["HOST_APPROVAL"] += 0.85
        elif primary == "UNKNOWN":
            scores["HOST_APPROVAL"] += 0.90

        if explicit_admission:
            scores["HOST_APPROVAL"] = 1.0
            scores["BUILDER"] = 0.0
        elif closure:
            if obs.already_complete:
                scores["STOP"] = 1.0
            elif obs.implementation_receipt_exists and obs.audit_receipt_exists:
                scores["HOST_APPROVAL"] = 0.95
            else:
                scores["AUDITOR"] = 0.95
            scores["BUILDER"] = 0.0

        if primary == "IMPLEMENTATION" and not obs.provenance_complete:
            scores["BUILDER"] = 0.0
            scores["AUDITOR"] = max(scores["AUDITOR"], 0.95)
        if obs.missing or obs.contradictions:
            scores["BUILDER"] = 0.0
            scores["AUDITOR"] = max(scores["AUDITOR"], 0.95)
        if obs.protected_overlap:
            scores["BUILDER"] = 0.0
            scores["HOST_APPROVAL"] = 1.0
        if obs.already_complete or no_action:
            scores = {route: 0.0 for route in ROUTES}
            scores["STOP"] = 1.0
        return {route: round(min(1.0, score), 6) for route, score in scores.items()}

    def _regime(self, goal: str, primary: str, route: str, obs: Observation) -> str:
        if obs.missing or obs.contradictions or primary == "RECOVERY":
            return "RECALIBRATION"
        if primary == "PROVENANCE":
            return "CONTACT"
        if primary == "ADMISSION":
            return "REGULATION"
        if route == "AUDITOR":
            return "AUDIT"
        if route == "BUILDER":
            return "DEVELOPMENT"
        return "INFERENCE"

    def _route_rationale(self, route: str, goal: str, primary: str, obs: Observation) -> str:
        if route == "STOP":
            return "Observed completion or an explicit no-action goal suppresses every action route."
        if obs.protected_overlap:
            return "The safety layer suppresses Builder because an intended target overlaps protected scope."
        if route == "HOST_APPROVAL" and primary == "ADMISSION":
            return "Admission and canonicalization remain external host-authority decisions."
        if route == "AUDITOR" and primary == "IMPLEMENTATION" and not obs.provenance_complete:
            return "The provenance layer suppresses Builder until missing source evidence is audited."
        if route == "AUDITOR" and _contains_any(goal, ("close",)):
            return "Closure lacks sufficient independent audit evidence, so audit precedes host approval."
        if route == "AUDITOR":
            return "The audit layer is the highest sufficient route for the classified goal and evidence."
        if route == "BUILDER":
            return "The development layer passed provenance and protected-path gates for an implementation goal."
        return "The goal is materially ambiguous or requires external authority, so execution is withheld."

    def _equivalence(
        self,
        goal: str,
        primary: str,
        route: str,
        targets: list[str],
        obs: Observation,
    ) -> dict[str, Any]:
        mismatches: list[str] = []
        if obs.protected_overlap:
            mismatches.append("intended target overlaps a protected path")
        if any(target.startswith("../") or Path(target).is_absolute() for target in targets):
            mismatches.append("target path is not repository-relative")

        allowed_routes = {
            "IMPLEMENTATION": {"BUILDER"} if obs.provenance_complete else {"AUDITOR", "HOST_APPROVAL"},
            "AUDIT": {"AUDITOR"},
            "PROVENANCE": {"AUDITOR"},
            "RECOVERY": {"AUDITOR", "HOST_APPROVAL"},
            "ADMISSION": {"AUDITOR", "HOST_APPROVAL", "STOP"},
            "PLANNING": {"HOST_APPROVAL"},
            "UNKNOWN": {"HOST_APPROVAL"},
        }[primary]
        if route not in allowed_routes and not obs.already_complete:
            mismatches.append(f"route {route} does not preserve the {primary} goal contract")
        if route in {"HOST_APPROVAL", "STOP"} and targets:
            mismatches.append(f"route {route} must not carry executable target paths")
        equivalent = not mismatches
        return {
            "equivalent": equivalent,
            "mismatches": mismatches,
            "falsifier": "Any mismatch in experiment identity, route, targets, protection, outputs, approval, or stop condition.",
        }

    def plan(
        self,
        experiment: str,
        goal: str,
        *,
        audit_finding: dict[str, Any] | None = None,
    ) -> OrchestrationDecision:
        if not re.fullmatch(r"\d+", experiment):
            raise ValueError("experiment must be numeric")
        experiment = experiment.zfill(3)
        if not goal.strip():
            raise ValueError("goal must be non-empty")

        obs = self.observe(experiment)
        primary, rival_problems, problem_falsifier = self._classify_problem(goal)
        route_scores = self._score_routes(goal, primary, obs)
        route = select_route(route_scores, self.bounds.satisficing_threshold)
        targets = list(obs.intended_targets) if route in {"BUILDER", "AUDITOR"} else []
        equivalence = self._equivalence(goal, primary, route, targets, obs)
        if not equivalence["equivalent"] and route not in {"HOST_APPROVAL", "STOP"}:
            route_scores["BUILDER"] = 0.0
            route_scores["HOST_APPROVAL"] = 1.0
            route = "HOST_APPROVAL"
            targets = []
            equivalence = self._equivalence(goal, primary, route, targets, obs)

        regime = self._regime(goal, primary, route, obs)
        uncertainty = min(1.0, (len(obs.missing) + (1 if primary == "UNKNOWN" else 0)) / 6.0)
        surprise = min(1.0, len(obs.contradictions) / 3.0)
        confidence = round(max(0.0, route_scores[route] * (1.0 - 0.5 * uncertainty)), 6)
        predicted_outputs = {
            "BUILDER": ["bounded implementation at the declared target", "one append-only implementation receipt"],
            "AUDITOR": ["read-only audit report", "one append-only audit receipt"],
            "HOST_APPROVAL": ["host decision"],
            "STOP": [],
        }[route]
        approval_required = route != "STOP"
        receipt_required = route in {"BUILDER", "AUDITOR"}
        stop_condition = {
            "BUILDER": "Stop after the bounded implementation is verified and receipted; do not broaden scope.",
            "AUDITOR": "Stop after reporting exact audit evidence and its receipt; do not implement changes.",
            "HOST_APPROVAL": "Stop without executable instruction until the host supplies an explicit decision.",
            "STOP": "Stop immediately because no repository task is required.",
        }[route]
        route_rationale = self._route_rationale(route, goal, primary, obs)
        rival_routes = [candidate for candidate in SAFE_TIE_PRECEDENCE if candidate != route]
        rival_rejections = [
            {"route": candidate, "reason": f"Suppressed by {route}: {route_rationale}"}
            for candidate in rival_routes
        ]
        routing_layers = [
            f"{layer}: {'selected' if layer == self._selected_layer(route, primary, obs) else 'not selected'}"
            for layer in LAYER_PRECEDENCE
        ]
        algorithmic_steps = [
            "Observe bounded repository evidence.",
            "Classify one primary problem and record rivals.",
            "Separate computational, algorithmic, and implementation levels.",
            "Apply safety, provenance, admission, and authority gates.",
            "Score allowed routes using observable signals.",
            "Select the first sufficient route with safe tie precedence.",
            "Compare the task contract with the host goal.",
            "Predict outputs and stop without task execution.",
        ][: self.bounds.max_steps]
        regime_rivals = [item for item in ("CONTACT", "INFERENCE", "REGULATION", "RECALIBRATION", "DEVELOPMENT", "AUDIT") if item != regime]
        decision: dict[str, Any] = {
            "engine": ENGINE_NAME,
            "engine_version": ENGINE_VERSION,
            "authority": "PROPOSAL_ONLY",
            "decision_id": "",
            "experiment": experiment,
            "goal": goal,
            "observed": {
                "evidence": obs.evidence,
                "missing": obs.missing,
                "contradictions": obs.contradictions,
            },
            "problem": {
                "primary": primary,
                "rivals": rival_problems,
                "evidence_used": obs.evidence[: self.bounds.max_steps],
                "minimal_falsifier": problem_falsifier,
            },
            "marr": {
                "computational": f"Produce a sufficient, non-executing next route for Experiment {experiment} and explain why it preserves the goal.",
                "algorithmic": algorithmic_steps,
                "implementation": f"Local Python engine; route {route}; targets {targets or ['none']}.",
            },
            "affordances": obs.affordances,
            "regime_contract": {
                "regime": regime,
                "entry_conditions": [f"problem classified as {primary}", f"route selected as {route}"],
                "observed_evidence": obs.evidence,
                "confidence": confidence,
                "rival_regimes": regime_rivals,
                "minimal_falsifier": f"Evidence that invalidates the {regime} entry conditions or makes a rival regime sufficient.",
                "protected_variables": self.protected_paths,
                "action_authority": "proposal",
                "exit_condition": stop_condition,
                "handoff_target": route,
                "receipt_requirement": receipt_required,
            },
            "routing_layers": routing_layers,
            "route_scores": route_scores,
            "route": route,
            "route_rationale": route_rationale,
            "rival_routes": rival_routes,
            "rival_route_rejections": rival_rejections,
            "equivalence_ledger": equivalence,
            "bounds": self.bounds.as_contract(),
            "target_files": targets,
            "protected_files": self.protected_paths,
            "predicted_outputs": predicted_outputs,
            "predicted_protected_file_impact": "none",
            "approval_required": approval_required,
            "stop_condition": stop_condition,
            "prediction_error_proxy": 0.0,
            "uncertainty_proxy": round(uncertainty, 6),
            "surprise_proxy": round(surprise, 6),
            "confidence": confidence,
            "status": "PROPOSED",
        }
        if audit_finding is not None:
            _validate_audit_finding_dict(audit_finding)
            decision["audit_finding"] = audit_finding
        decision["decision_id"] = compute_decision_id(decision)
        validate_decision(decision)
        return decision  # type: ignore[return-value]

    @staticmethod
    def _selected_layer(route: str, primary: str, obs: Observation) -> str:
        if obs.protected_overlap or route == "STOP":
            return "SAFETY_LAYER"
        if primary == "PROVENANCE" or (primary == "IMPLEMENTATION" and not obs.provenance_complete):
            return "PROVENANCE_LAYER"
        if primary == "ADMISSION":
            return "ADMISSION_LAYER"
        if route == "AUDITOR":
            return "AUDIT_LAYER"
        if route == "BUILDER":
            return "DEVELOPMENT_LAYER"
        return "PLANNING_LAYER"

    def validate_with_schema(self, decision: OrchestrationDecision) -> None:
        schema_path = Path(__file__).resolve().parents[1] / "schemas" / "orchestration_decision.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        validate_json_schema(decision, schema)

    def emit_outputs(
        self,
        decision: OrchestrationDecision,
        timestamp: str | None = None,
    ) -> dict[str, str]:
        validate_decision(decision)
        self.validate_with_schema(decision)
        timestamp = timestamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        if re.fullmatch(r"\d{8}T\d{6}Z", timestamp) is None:
            raise ValueError("timestamp must use UTC basic format")
        stem = f"{timestamp}__experiment-{decision['experiment']}__{decision['decision_id'][:12]}"
        decision_path = self.output_path / f"{stem}__decision.json"
        packet_path = self.output_path / f"{stem}__task-packet.md"
        if decision_path.exists() or packet_path.exists():
            raise FileExistsError("refusing to overwrite an existing planning artifact")
        self.output_path.mkdir(parents=True, exist_ok=True)
        decision_text = json.dumps(decision, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        decision_sha256 = hashlib.sha256(decision_text.encode("utf-8")).hexdigest()
        packet_text = self._format_packet(decision, decision_path, decision_sha256)
        packet_sha256 = hashlib.sha256(packet_text.encode("utf-8")).hexdigest()
        with decision_path.open("x", encoding="utf-8") as handle:
            handle.write(decision_text)
        try:
            with packet_path.open("x", encoding="utf-8") as handle:
                handle.write(packet_text)
        except Exception:
            decision_path.unlink(missing_ok=True)
            raise
        return {
            "decision_path": str(decision_path),
            "decision_sha256": decision_sha256,
            "packet_path": str(packet_path),
            "packet_sha256": packet_sha256,
        }

    def _format_packet(self, decision: OrchestrationDecision, decision_path: Path, decision_hash: str) -> str:
        route = decision["route"]
        if route == "BUILDER":
            task = "Mode: IMPLEMENT\nPrepare only the bounded target changes. Do not execute this packet automatically."
        elif route == "AUDITOR":
            task = "Mode: READ_ONLY_AUDIT\nInspect only the declared targets and report literal evidence. Do not implement changes."
        elif route == "HOST_APPROVAL":
            task = "No executable instruction. Host approval is required before any repository task exists."
        else:
            task = "No task. Stop without repository action."
        bullet = lambda values: "\n".join(f"- {value}" for value in values) if values else "- (none)"
        rival_lines = [
            f"- {entry['route']}: {entry['reason']}" for entry in decision["rival_route_rejections"]
        ]
        finding_lines: list[str] = []
        if "audit_finding" in decision:
            finding_lines = [
                "",
                "## Decision-owned audit finding",
                "Preserve this exact projection unchanged under `audit_finding:` in the operation receipt.",
                "```json",
                json.dumps(decision["audit_finding"], ensure_ascii=False, sort_keys=True, indent=2),
                "```",
            ]
        return "\n".join([
            "# Orchestrator Task Packet — Kernel 20 Phenotype",
            "", f"Engine: {decision['engine']} {decision['engine_version']}",
            f"Authority: {decision['authority']}", f"Experiment: {decision['experiment']}",
            f"Goal: {decision['goal']}", f"Decision ID: {decision['decision_id']}",
            f"Decision JSON: {decision_path}", f"Decision JSON SHA-256: {decision_hash}",
            *finding_lines,
            "", "## Task", task,
            "", "## Observed evidence", bullet(decision["observed"]["evidence"]),
            "", "## Missing evidence", bullet(decision["observed"]["missing"]),
            "", "## Contradictions", bullet(decision["observed"]["contradictions"]),
            "", "## Problem recovery / council",
            f"Primary: {decision['problem']['primary']}",
            f"Rivals: {', '.join(decision['problem']['rivals']) or '(none)'}",
            f"Minimal falsifier: {decision['problem']['minimal_falsifier']}",
            "", "## Marr frame", f"Computational: {decision['marr']['computational']}",
            "Algorithmic:", bullet(decision["marr"]["algorithmic"]),
            f"Implementation: {decision['marr']['implementation']}",
            "", "## Detected affordances", bullet(decision["affordances"]),
            "", "## Regime contract", "```json", json.dumps(decision["regime_contract"], indent=2), "```",
            "", "## Route", f"Selected: {route}", f"Rationale: {decision['route_rationale']}",
            "Rival routes and rejection reasons:", *(rival_lines or ["- (none)"]),
            "", "## Target files", bullet(decision["target_files"]),
            "", "## Protected files", bullet(decision["protected_files"]),
            "", "## Predicted outputs", bullet(decision["predicted_outputs"]),
            "", "## Controls", f"Approval required: {str(decision['approval_required']).lower()}",
            f"Expected protected-file impact: {decision['predicted_protected_file_impact']}",
            f"Stop condition: {decision['stop_condition']}",
            f"Equivalence: {str(decision['equivalence_ledger']['equivalent']).lower()}",
            f"Confidence: {decision['confidence']:.6f}", "",
        ])


def _extract_receipt_list(receipt_text: str, field_names: tuple[str, ...]) -> list[str]:
    values: list[str] = []
    active = False
    normalized_fields = {name.lower() for name in field_names}
    for line in receipt_text.splitlines():
        stripped = line.strip()
        heading = stripped.rstrip(":").lower()
        if heading in normalized_fields:
            active = True
            continue
        if active and re.match(r"^[A-Za-z_][A-Za-z0-9 _-]*:\s*", stripped):
            active = False
        if active and stripped.startswith("-"):
            value = stripped[1:].strip().strip("`")
            if value and value.lower() != "(none)":
                values.append(value)
    return values


def reconcile_decision(
    decision: dict[str, Any],
    receipt_text: str,
    receipt_path: str,
    *,
    repository_root: Path | str | None = None,
) -> dict[str, Any]:
    """Compare a receipt's literal fields with the prior prediction.

    When the decision owns an ``audit_finding``, the decision-owned source is
    also resolved against ``repository_root`` and its *current* bytes are
    rechecked. Decisions without a finding keep their historical behavior,
    including the historical normalization denominator.
    """

    try:
        validate_decision(decision)
    except (KeyError, TypeError, ValueError) as error:
        return {"outcome": "UNKNOWN", "mismatches": [f"invalid prior decision: {error}"]}

    mismatches: list[str] = []
    if not receipt_text.strip():
        return {"outcome": "UNKNOWN", "mismatches": ["receipt is empty"]}
    if re.search(r"(?:completion_status|status):\s*COMPLETE\b", receipt_text, flags=re.IGNORECASE) is None:
        mismatches.append("receipt does not record COMPLETE status")

    owned_finding = decision.get("audit_finding")
    finding_checks = 0
    source_checks = 0
    try:
        returned_finding = parse_return_audit_finding(receipt_text)
    except ValueError as error:
        mismatches.append(f"invalid return audit_finding: {error}")
        finding_checks = 1
    else:
        finding_mismatches, finding_checks = audit_finding_comparisons(
            owned_finding, returned_finding
        )
        mismatches.extend(finding_mismatches)
    if owned_finding is not None:
        source_mismatches, source_checks = audit_finding_source_state(
            repository_root, owned_finding
        )
        mismatches.extend(source_mismatches)
    else:
        # No finding is owned: preserve the historical normalization exactly.
        finding_checks = 0

    observed_outputs = _extract_receipt_list(receipt_text, ("observed_outputs", "outputs"))
    observed_paths = _extract_receipt_list(
        receipt_text,
        ("files_created", "files_modified", "target_files"),
    )
    for predicted in decision["predicted_outputs"]:
        if predicted not in observed_outputs:
            mismatches.append(f"predicted output not observed: {predicted}")
    for target in decision["target_files"]:
        if not any(path == target or path.startswith(f"{target}/") for path in observed_paths):
            mismatches.append(f"predicted target not observed: {target}")
    receipt_name = Path(receipt_path).name
    protected_impacts = [
        path for path in observed_paths
        if not (Path(path).parent.name == "receipts" and Path(path).name == receipt_name)
        and any(_path_is_protected(path, protected) for protected in decision["protected_files"])
    ]
    for path in protected_impacts:
        mismatches.append(f"protected path impact observed: {path}")
    stop_match = re.search(r"stop_condition_met:\s*(true|false)", receipt_text, flags=re.IGNORECASE)
    if stop_match is None:
        mismatches.append("receipt does not state stop_condition_met")
    elif stop_match.group(1).lower() != "true":
        mismatches.append("predicted stop condition was not met")

    if not mismatches:
        outcome = "CLOSE"
    elif protected_impacts:
        outcome = "AUDIT"
    elif decision["route"] == "HOST_APPROVAL":
        outcome = "HOST_APPROVAL"
    else:
        outcome = "RECALIBRATE"
    # Normalize by the checks actually performed, not by a fixed proxy shape.
    comparison_count = (
        len(decision["predicted_outputs"])
        + len(decision["target_files"])
        + 2
        + finding_checks
        + source_checks
    )
    return {
        "outcome": outcome,
        "packet_decision_id": decision["decision_id"],
        "receipt_path": receipt_path,
        "mismatches": mismatches,
        "comparison_count": comparison_count,
        "prediction_error_proxy": round(
            min(1.0, len(mismatches) / max(1, comparison_count)), 6
        ),
    }


def reconcile_files(repository_root: Path, packet_path: str, receipt_path: str) -> dict[str, Any]:
    root = repository_root.resolve()
    packet = (root / packet_path).resolve() if not Path(packet_path).is_absolute() else Path(packet_path).resolve()
    receipt = (root / receipt_path).resolve() if not Path(receipt_path).is_absolute() else Path(receipt_path).resolve()
    if not _is_subpath(packet, root / "orchestrator" / "output"):
        raise ValueError("reconciliation packet must be inside orchestrator/output")
    if not _is_subpath(receipt, root / "receipts"):
        raise ValueError("reconciliation receipt must be inside receipts/")
    decision = json.loads(packet.read_text(encoding="utf-8"))
    receipt_text = receipt.read_text(encoding="utf-8")
    return reconcile_decision(decision, receipt_text, str(receipt), repository_root=root)
