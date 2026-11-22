#!/usr/bin/env python3
"""Stable Python entrypoint for Phase 1, Kernel 20, and Kernel 21 preflight."""

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import List
import sys

from bridge import (
    create_lifecycle_witness,
    run_preflight,
    verify_lifecycle_witness,
    verify_return_files,
)
from engine.kernel20_phenotype import Kernel20Phenotype, load_audit_finding, reconcile_files
from engine.legacy_engine import run_legacy


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "orchestrator.config.json"
STATE_FILE = "CURRENT_STATE.md"


def fail(message: str, code: int = 1) -> None:
    print(f"FAIL_CLOSED: {message}", file=sys.stderr)
    raise SystemExit(code)


def parse_args(argv):
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--root", required=True, type=Path, help="project root to inspect; generated records stay under ROOT/orchestrator/output")
    parser.add_argument("--config", type=Path, help="optional local policy JSON (no inherited repositoryRoot)")
    parser.add_argument("--engine", choices=("kernel20", "legacy"), default="kernel20")
    parser.add_argument("--experiment")
    parser.add_argument("--goal")
    parser.add_argument("--dry-test", action="store_true", help="run local dry tests only")
    parser.add_argument("--reconcile", action="store_true", help="compare a prior decision with a receipt")
    parser.add_argument(
        "--bridge-preflight",
        action="store_true",
        help="classify attachment and emit proposal-only bridge artifacts",
    )
    parser.add_argument(
        "--verify-return",
        action="store_true",
        help="verify returned evidence in the requested repository body",
    )
    parser.add_argument(
        "--create-lifecycle-witness",
        action="store_true",
        help="commit one explicitly Host-approved lifecycle witness",
    )
    parser.add_argument(
        "--verify-lifecycle-witness",
        action="store_true",
        help="independently recover and verify a committed lifecycle witness",
    )
    parser.add_argument("--packet", help="decision JSON sidecar under orchestrator/output")
    parser.add_argument("--decision", help="exact Host-approved decision JSON")
    parser.add_argument("--envelope", help="Kernel 21 task envelope under the repository")
    parser.add_argument(
        "--attachment-contract",
        help="persisted attachment contract carrying this lifecycle's outer custody",
    )
    parser.add_argument(
        "--transport-plan",
        help="persisted transport plan carrying this lifecycle's outer custody",
    )
    parser.add_argument("--receipt", help="append-only receipt under receipts/")
    parser.add_argument("--node-source", help="exact inert node source file")
    parser.add_argument("--access-point", help="decision-bound repair access-point JSON")
    parser.add_argument("--host-approval-record", help="decision-bound Host approval JSON")
    parser.add_argument("--witness-directory")
    parser.add_argument(
        "--audit-finding-report",
        help="repository-relative path to an Auditor report containing one AUDIT_FINDING block",
    )
    return parser.parse_args(argv)


def load_policy(path=None):
    with (path or CONFIG_PATH).open("r", encoding="utf-8") as fp:
        return json.load(fp)


def validate_experiment(raw: str) -> str:
    if not raw.isdigit():
        fail(f"invalid experiment number: {raw}")
    return raw.zfill(3)


def is_subpath(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def path_sha256_bytes(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def read_current_state(repository_root: Path):
    state_path = repository_root / STATE_FILE
    if not state_path.is_file():
        fail(f"required file missing: {state_path}")
    text = state_path.read_text(encoding="utf-8")
    return text, path_sha256_bytes(text)


def inspect_experiment(repository_root: Path, experiment: str):
    experiments_root = repository_root / "experiments"
    if not experiments_root.is_dir():
        fail("experiments directory is missing")

    matches = sorted([p for p in experiments_root.iterdir() if p.is_dir() and p.name.startswith(f"{experiment}-")])
    if len(matches) == 0:
        fail(f"experiment directory not found for {experiment}")
    if len(matches) > 1:
        names = ", ".join(p.name for p in matches)
        fail(f"ambiguous experiment selector {experiment}: {names}")

    exp_dir = matches[0]
    filenames = []
    for item in sorted(exp_dir.rglob("*")):
        if item.is_file():
            filenames.append(str(item.relative_to(exp_dir)))
    return exp_dir, filenames


def list_recent_receipts(repository_root: Path, limit: int = 10) -> List[str]:
    receipt_root = repository_root / "receipts"
    if not receipt_root.is_dir():
        return []
    files = []
    for item in receipt_root.iterdir():
        if item.is_file():
            files.append((item.stat().st_mtime, item.name))
    files.sort(key=lambda x: x[0], reverse=True)
    return [name for _, name in files[:limit]]


def validate_target_paths(repository_root: Path, protected_paths: List[str], candidates: List[str]) -> None:
    protected_abs = [(repository_root / p).resolve() for p in protected_paths]
    for candidate in candidates:
        candidate_path = (repository_root / candidate).resolve()
        if not is_subpath(candidate_path, repository_root):
            fail(f"path escapes repository root: {candidate}")
        for protected in protected_abs:
            if is_subpath(candidate_path, protected):
                fail(f"protected-path overlap: {candidate}")


def build_packet(
    repository_root: Path,
    experiment: str,
    goal: str,
    state_sha256: str,
    experiment_dir: Path,
    experiment_files: List[str],
    receipts: List[str],
    protected_paths: List[str],
) -> Path:
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    packet_filename = f"{now}__experiment-{experiment}__task-packet.md"
    output_dir = repository_root / "orchestrator" / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    packet_path = output_dir / packet_filename

    proposal_targets = [
        f"experiments/{experiment_dir.name}/" + f
        for f in experiment_files[:20]
    ]
    if not proposal_targets:
        proposal_targets = [f"experiments/{experiment_dir.name}"]

    validate_target_paths(repository_root, protected_paths, proposal_targets)

    packet_payload = {
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

    observed_state_preview = experiment_files[:10]
    markdown = "\n".join(
        [
            "# Orchestrator Task Packet (Python bootstrap)",
            f"Generated: {now}",
            f"Experiment: {experiment}",
            f"Goal: {goal}",
            f"Repository: {repository_root}",
            f"State SHA-256: {state_sha256}",
            "",
            "## Recent receipts",
            *(f"- {name}" for name in receipts),
            "",
            "## Experiment files",
            *(f"- {name}" for name in observed_state_preview),
            "",
            "## Proposed action",
            packet_payload["next_action"]["proposed_next_action"],
            "",
            "## Controls",
            f"- approval_requirement: {packet_payload['next_action']['approval_requirement']}",
            f"- stop_condition: {packet_payload['next_action']['stop_condition']}",
            f"- protected_files: {', '.join(protected_paths)}",
            "",
            "## Packet payload",
            "```json",
            json.dumps(packet_payload, indent=2),
            "```",
            "",
        ]
    )

    packet_path.write_text(markdown, encoding="utf-8")
    return packet_path


def packet_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_dry_test(repository_root: Path, policy: dict, goal: str):
    if policy.get("protectedPaths") is None:
        fail("protectedPaths missing from config")
    protected_paths = policy["protectedPaths"]

    validate_target_paths(repository_root, protected_paths, ["orchestrator/run"])
    validate_target_paths(repository_root, protected_paths, ["orchestrator/output/dry-check.txt"])

    experiment = validate_experiment("010")
    exp_dir, filenames = inspect_experiment(repository_root, experiment)
    if not exp_dir.name.startswith("010-"):
        fail("experiment inspection sanity check failed")

    if not filenames:
        fail("experiment inspection found no files")

    list_recent_receipts(repository_root, int(policy.get("maxRecentReceipts", 10)))
    print("DRY_TEST_RESULT: PASS")
    print(f"DRY_TEST_EXPERIMENT: {experiment}")
    print(f"DRY_TEST_EXPERIMENT_FILE_COUNT: {len(filenames)}")


def main(argv=None):
    args = parse_args(argv if argv is not None else None)
    try:
        policy = load_policy(args.config)
        if not isinstance(policy, dict):
            fail("policy must be a JSON object")
    except (OSError, ValueError) as error:
        fail(f"cannot load policy: {error}")
    repository_root = args.root.expanduser().resolve()
    if not repository_root.is_dir():
        fail("--root must name an existing project directory")
    # The observation/output workspace is caller selected, never a saved host path.
    policy.pop("repositoryRoot", None)

    if args.audit_finding_report is not None and (
        args.create_lifecycle_witness
        or args.verify_lifecycle_witness
        or args.verify_return
        or args.reconcile
        or args.engine != "kernel20"
    ):
        fail("--audit-finding-report is only valid for Kernel 20 planning or bridge preflight")

    if args.create_lifecycle_witness:
        required = {
            "--decision": args.decision,
            "--node-source": args.node_source,
            "--access-point": args.access_point,
            "--host-approval-record": args.host_approval_record,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            fail("--create-lifecycle-witness requires " + ", ".join(missing))
        incompatible = (
            args.verify_lifecycle_witness,
            args.verify_return,
            args.bridge_preflight,
            args.reconcile,
            args.experiment is not None,
            args.goal is not None,
            args.envelope is not None,
            args.packet is not None,
            args.receipt is not None,
        )
        if any(incompatible):
            fail("--create-lifecycle-witness cannot be combined with another operation")
        try:
            result = create_lifecycle_witness(
                repository_root,
                args.decision,
                args.node_source,
                access_point_path=args.access_point,
                host_approval_record_path=args.host_approval_record,
                execution_root=Path.cwd(),
            )
        except (OSError, ValueError, json.JSONDecodeError) as error:
            fail(f"lifecycle witness creation failed: {error}")
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    if args.verify_lifecycle_witness:
        if not args.witness_directory:
            fail("--verify-lifecycle-witness requires --witness-directory")
        incompatible = (
            args.create_lifecycle_witness,
            args.verify_return,
            args.bridge_preflight,
            args.reconcile,
            args.experiment is not None,
            args.goal is not None,
            args.decision is not None,
            args.envelope is not None,
            args.packet is not None,
            args.receipt is not None,
        )
        if any(incompatible):
            fail("--verify-lifecycle-witness cannot be combined with another operation")
        try:
            result = verify_lifecycle_witness(
                repository_root / args.witness_directory,
                repository_root=repository_root,
            )
        except (OSError, ValueError, json.JSONDecodeError) as error:
            fail(f"lifecycle witness verification failed: {error}")
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    if args.verify_return:
        if args.envelope is None or args.receipt is None:
            fail("--verify-return requires --envelope and --receipt")
        if args.attachment_contract is None or args.transport_plan is None:
            fail(
                "--verify-return requires --attachment-contract and --transport-plan; "
                "outer custody cannot be verified without the trusted carrier"
            )
        if args.bridge_preflight or args.reconcile or args.experiment is not None or args.goal is not None:
            fail("--verify-return cannot be combined with planning or reconciliation")
        try:
            result = verify_return_files(
                repository_root,
                args.envelope,
                args.receipt,
                observed_root=Path.cwd(),
                attachment_path=args.attachment_contract,
                transport_plan_path=args.transport_plan,
            )
        except (OSError, ValueError, json.JSONDecodeError) as error:
            fail(f"return verification failed: {error}")
        print(result["status"])
        return 0

    if args.reconcile:
        if args.packet is None or args.receipt is None:
            fail("--reconcile requires --packet and --receipt")
        if args.experiment is not None or args.goal is not None:
            fail("--reconcile does not accept --experiment or --goal")
        try:
            result = reconcile_files(repository_root, args.packet, args.receipt)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            fail(f"reconciliation failed: {error}")
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    if args.experiment is None or args.goal is None:
        fail("--experiment and --goal are required for planning")
    experiment = validate_experiment(args.experiment)

    audit_finding = None
    if args.audit_finding_report is not None:
        try:
            audit_finding = load_audit_finding(repository_root, args.audit_finding_report)
        except ValueError as error:
            fail(str(error))

    if args.bridge_preflight:
        if args.engine != "kernel20":
            fail("Kernel 21 bridge preflight requires the Kernel 20 engine")
        if args.reconcile or args.envelope is not None or args.packet is not None or args.receipt is not None:
            fail("--bridge-preflight cannot be combined with reconciliation inputs")
        try:
            result = run_preflight(
                repository_root,
                policy,
                experiment,
                args.goal,
                execution_root=Path.cwd(),
                audit_finding=audit_finding,
            )
        except (OSError, ValueError, json.JSONDecodeError) as error:
            fail(f"bridge preflight failed: {error}")
        attachment = result["attachment"]
        transport = result["transport_plan"]
        artifacts = result["artifacts"]
        fields = (
            ("EXECUTION_ENVIRONMENT", attachment["environment"]),
            ("REQUESTED_ROOT", attachment["requested_root"]),
            ("EXECUTION_ROOT", attachment["execution_root"]),
            ("BODY_IDENTITY", attachment["body_identity"]),
            ("ATTACHMENT_STATE", attachment["attachment_state"]),
            ("READABLE_ROOTS", json.dumps(attachment["readable_roots"], separators=(",", ":"))),
            ("WRITABLE_ROOTS", json.dumps(attachment["writable_roots"], separators=(",", ":"))),
            ("CAPABILITIES", json.dumps(attachment["capabilities"], sort_keys=True, separators=(",", ":"))),
            ("TRANSPORT_SELECTED", transport["transport_selected"]),
            ("DELIVERY_ROOT", transport["delivery_root"]),
            ("RETURN_ROOT", transport["return_root"]),
            ("HOST_APPROVAL_REQUIRED", str(transport["host_approval_required"]).lower()),
            ("ATTACHMENT_CONTRACT_PATH", artifacts["attachment_contract_path"]),
            ("ATTACHMENT_CONTRACT_SHA256", artifacts["attachment_contract_sha256"]),
            ("TRANSPORT_PLAN_PATH", artifacts["transport_plan_path"]),
            ("TRANSPORT_PLAN_SHA256", artifacts["transport_plan_sha256"]),
            ("STATUS", transport["status"]),
        )
        for key, value in fields:
            print(f"{key}: {value}")
        return 0

    if args.engine == "legacy":
        required = policy.get("requiredFiles", [])
        if not (repository_root / "CURRENT_STATE.md").is_file():
            fail("required file missing: CURRENT_STATE.md")
        for rel in required:
            if not (repository_root / rel).exists():
                fail(f"required file missing: {rel}")
        try:
            result = run_legacy(repository_root, policy, experiment, args.goal, args.dry_test)
        except (OSError, ValueError) as error:
            fail(str(error))
        for key, value in result.items():
            print(f"{key}: {value}")
        return 0

    try:
        engine = Kernel20Phenotype(repository_root, policy)
        decision = engine.plan(experiment, args.goal, audit_finding=audit_finding)
        engine.validate_with_schema(decision)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        fail(str(error))
    if args.dry_test:
        print("DRY_TEST_RESULT: PASS")
        print(f"DRY_TEST_ENGINE: {decision['engine']}")
        print(f"DRY_TEST_DECISION_ID: {decision['decision_id']}")
        print(f"DRY_TEST_ROUTE: {decision['route']}")
        return 0
    try:
        outputs = engine.emit_outputs(decision)
    except (OSError, ValueError) as error:
        fail(str(error))
    print(f"Decision path: {outputs['decision_path']}")
    print(f"Decision SHA-256: {outputs['decision_sha256']}")
    print(f"Packet path: {outputs['packet_path']}")
    print(f"Packet SHA-256: {outputs['packet_sha256']}")
    print(f"Route: {decision['route']}")
    print(f"Approval required: {str(decision['approval_required']).lower()}")
    print(f"Stop condition: {decision['stop_condition']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
