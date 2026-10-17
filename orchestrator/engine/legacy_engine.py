"""Phase 1 Python engine preserved behind ``--engine legacy``."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _fail(message: str) -> None:
    raise ValueError(message)


def _is_subpath(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _validate_targets(repository_root: Path, protected_paths: list[str], candidates: list[str]) -> None:
    protected_abs = [(repository_root / item).resolve() for item in protected_paths]
    for candidate in candidates:
        candidate_path = (repository_root / candidate).resolve()
        if not _is_subpath(candidate_path, repository_root):
            _fail(f"path escapes repository root: {candidate}")
        for protected in protected_abs:
            if _is_subpath(candidate_path, protected):
                _fail(f"protected-path overlap: {candidate}")


def _inspect_experiment(repository_root: Path, experiment: str) -> tuple[Path, list[str]]:
    experiments_root = repository_root / "experiments"
    if not experiments_root.is_dir():
        _fail("experiments directory is missing")
    matches = sorted(
        item for item in experiments_root.iterdir()
        if item.is_dir() and item.name.startswith(f"{experiment}-")
    )
    if not matches:
        _fail(f"experiment directory not found for {experiment}")
    if len(matches) > 1:
        _fail(f"ambiguous experiment selector {experiment}: {', '.join(p.name for p in matches)}")
    experiment_dir = matches[0]
    filenames = [
        str(item.relative_to(experiment_dir))
        for item in sorted(experiment_dir.rglob("*"))
        if item.is_file()
    ]
    return experiment_dir, filenames


def _recent_receipts(repository_root: Path, limit: int) -> list[str]:
    receipt_root = repository_root / "receipts"
    if not receipt_root.is_dir():
        return []
    files = [(item.stat().st_mtime, item.name) for item in receipt_root.iterdir() if item.is_file()]
    files.sort(key=lambda item: item[0], reverse=True)
    return [name for _, name in files[:limit]]


def _build_packet(
    repository_root: Path,
    experiment: str,
    goal: str,
    state_sha256: str,
    experiment_dir: Path,
    experiment_files: list[str],
    receipts: list[str],
    protected_paths: list[str],
) -> Path:
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    packet_path = repository_root / "orchestrator" / "output" / f"{now}__experiment-{experiment}__task-packet.md"
    proposal_targets = [
        f"experiments/{experiment_dir.name}/{filename}" for filename in experiment_files[:20]
    ] or [f"experiments/{experiment_dir.name}"]
    _validate_targets(repository_root, protected_paths, proposal_targets)
    payload = {
        "packet_version": "v1",
        "generated_at_utc": now,
        "experiment": experiment,
        "goal": goal,
        "repository_root": str(repository_root),
        "observed_state_sha256": state_sha256,
        "next_action": {
            "observed_state": "CURRENT_STATE.md read; experiment files and recent receipts enumerated",
            "proposed_next_action": "Prepare task packet; no repository write outside orchestrator/output in this phase.",
            "target_files": proposal_targets,
            "protected_files": protected_paths,
            "approval_requirement": "Host approval required before any non-packet file is modified.",
            "stop_condition": "Stop after writing packet; do not execute any repository task.",
            "risk_notes": "No API call and no task execution in bootstrap.",
            "confidence": "high",
        },
    }
    markdown = "\n".join([
        "# Orchestrator Task Packet (Python bootstrap)",
        f"Generated: {now}",
        f"Experiment: {experiment}",
        f"Goal: {goal}",
        f"Repository: {repository_root}",
        f"State SHA-256: {state_sha256}",
        "", "## Recent receipts", *(f"- {name}" for name in receipts),
        "", "## Experiment files", *(f"- {name}" for name in experiment_files[:10]),
        "", "## Proposed action", payload["next_action"]["proposed_next_action"],
        "", "## Controls",
        f"- approval_requirement: {payload['next_action']['approval_requirement']}",
        f"- stop_condition: {payload['next_action']['stop_condition']}",
        f"- protected_files: {', '.join(protected_paths)}",
        "", "## Packet payload", "```json", json.dumps(payload, indent=2), "```", "",
    ])
    if not packet_path.resolve().is_relative_to(repository_root.resolve()):
        raise ValueError("orchestrator output escapes the project root")
    packet_path.parent.mkdir(parents=True, exist_ok=True)
    with packet_path.open("x", encoding="utf-8") as handle:
        handle.write(markdown)
    return packet_path


def run_legacy(
    repository_root: Path,
    policy: dict[str, Any],
    experiment: str,
    goal: str,
    dry_test: bool = False,
) -> dict[str, str]:
    protected_paths = [str(item) for item in policy.get("protectedPaths", [])]
    if dry_test:
        dry_experiment = experiment
        experiment_dir, filenames = _inspect_experiment(repository_root, dry_experiment)
        _validate_targets(repository_root, protected_paths, ["orchestrator/run"])
        _validate_targets(repository_root, protected_paths, ["orchestrator/output/dry-check.txt"])
        if not filenames:
            _fail("experiment inspection found no files")
        _recent_receipts(repository_root, int(policy.get("maxRecentReceipts", 10)))
        return {
            "DRY_TEST_RESULT": "PASS",
            "DRY_TEST_EXPERIMENT": dry_experiment,
            "DRY_TEST_EXPERIMENT_FILE_COUNT": str(len(filenames)),
        }
    experiment_dir, filenames = _inspect_experiment(repository_root, experiment)
    state_path = repository_root / "CURRENT_STATE.md"
    state_text = state_path.read_text(encoding="utf-8")
    state_sha256 = hashlib.sha256(state_text.encode("utf-8")).hexdigest()
    _validate_targets(repository_root, protected_paths, [str(experiment_dir.relative_to(repository_root))])
    _validate_targets(
        repository_root,
        protected_paths,
        [f"experiments/{experiment_dir.name}/{item}" for item in filenames[:1]],
    )
    packet_path = _build_packet(
        repository_root,
        experiment,
        goal,
        state_sha256,
        experiment_dir,
        filenames,
        _recent_receipts(repository_root, int(policy.get("maxRecentReceipts", 10))),
        protected_paths,
    )
    return {
        "Packet path": str(packet_path),
        "Packet SHA-256": hashlib.sha256(packet_path.read_bytes()).hexdigest(),
    }
