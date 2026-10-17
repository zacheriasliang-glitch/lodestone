from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


ORCHESTRATOR_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = ORCHESTRATOR_ROOT.parent
sys.path.insert(0, str(ORCHESTRATOR_ROOT))

from engine.contracts import Bounds, validate_decision
from engine.kernel20_phenotype import (
    Kernel20Phenotype,
    load_audit_finding,
    reconcile_decision,
    select_route,
)
from engine.legacy_engine import run_legacy


class Kernel20PhenotypeTests(unittest.TestCase):
    def test_candidate_record_shelf_is_not_an_execution_target(self) -> None:
        repository, policy = self.make_repository()
        shelf = repository / "experiments" / "012-candidate" / "records"
        shelf.mkdir(parents=True)
        (shelf / "review.md").write_text("Historical candidate review\n", encoding="utf-8")
        engine = Kernel20Phenotype(repository, policy)
        decision = engine.plan("012", "Implement bounded experiment work")
        self.assertIn("experiment directory for 012", decision["observed"]["missing"])
        self.assertFalse(any("012-candidate" in e for e in decision["observed"]["evidence"]))
        executable = repository / "experiments" / "012-fixture"
        executable.mkdir()
        (executable / "work.py").write_text("# Authorized fixture only\n", encoding="utf-8")
        changed = engine.plan("012", "Implement bounded experiment work")
        self.assertNotIn("experiment directory for 012", changed["observed"]["missing"])
        self.assertIn("experiment directory exists: experiments/012-fixture", changed["observed"]["evidence"])

    def setUp(self) -> None:
        self.fixture = Path(tempfile.mkdtemp(prefix="lodestone-kernel20-test-")).resolve()

    def tearDown(self) -> None:
        shutil.rmtree(self.fixture)

    def make_repository(
        self,
        *,
        experiment: str = "010",
        provenance: bool = True,
        state_text: str = "# State\nExperiment 010 is planned.\n",
        file_count: int = 1,
    ) -> tuple[Path, dict]:
        repository = self.fixture / "repository"
        (repository / "orchestrator" / "schemas").mkdir(parents=True)
        (repository / "orchestrator" / "output").mkdir(parents=True)
        shutil.copyfile(
            ORCHESTRATOR_ROOT / "schemas" / "orchestration_decision.schema.json",
            repository / "orchestrator" / "schemas" / "orchestration_decision.schema.json",
        )
        experiment_dir = repository / "experiments" / f"{experiment}-fixture"
        experiment_dir.mkdir(parents=True)
        for index in range(file_count):
            (experiment_dir / f"file-{index}.md").write_text(f"fixture {index}\n", encoding="utf-8")
        (repository / "receipts").mkdir()
        (repository / "receipts" / "20260801T000000Z__unrelated__RCP-test.md").write_text(
            "completion_status: COMPLETE\n", encoding="utf-8"
        )
        (repository / "CURRENT_STATE.md").write_text(state_text, encoding="utf-8")
        if provenance:
            source_dir = repository / "sources" / f"experiment-{experiment}"
            source_dir.mkdir(parents=True)
            for filename in ("SOURCE_MANIFEST.json", "SOURCE_DECLARATION.md", "SHA256SUMS.txt"):
                (source_dir / filename).write_text("fixture\n", encoding="utf-8")
        policy = {
            "protectedPaths": ["protocol", "runtime", "receipts", "experiments/009"],
            "outputDir": "orchestrator/output",
            "maxRecentReceipts": 10,
            "kernel20Bounds": {
                "max_files": 40,
                "max_receipts": 10,
                "max_steps": 8,
                "satisficing_threshold": 0.8,
            },
        }
        return repository, policy

    def _finding(self, load_bearing_gap: bool, sha: str = "0" * 64) -> dict:
        return {
            "schema_version": "1.0.0",
            "producer_role": "AUDITOR",
            "verdict": "CONDITIONAL",
            "load_bearing_gap": load_bearing_gap,
            "source_path": "receipts/example-finding.md",
            "source_byte_count": 128,
            "source_sha256": sha,
        }

    def _write_audit_report(
        self,
        repository: Path,
        load_bearing_gap: bool,
        *,
        filename: str = "auditor-report.md",
    ) -> tuple[Path, bytes]:
        raw = (
            "# Independent audit\n\n"
            "## AUDIT_FINDING\n"
            "```json\n"
            "{\n"
            '  "schema_version": "1.0.0",\n'
            '  "producer_role": "AUDITOR",\n'
            '  "verdict": "CONDITIONAL",\n'
            f'  "load_bearing_gap": {str(load_bearing_gap).lower()}\n'
            "}\n"
            "```\n"
        ).encode("utf-8")
        path = repository / filename
        path.write_bytes(raw)
        return path, raw

    @staticmethod
    def _receipt_finding_block(finding: dict) -> list[str]:
        return [
            "audit_finding:",
            "```json",
            json.dumps(finding, ensure_ascii=False, sort_keys=True, indent=2),
            "```",
        ]

    def test_auditor_role_file_is_never_ingestible_report_evidence(self) -> None:
        """The menu is not the dinner: a role instruction is not a report."""

        relative = ".github/agents/Whitestone Auditor.agent.md"
        repository, _ = self.make_repository()
        path = repository / relative
        path.parent.mkdir(parents=True)
        path.write_text('Synthetic role instructions only. This is not an audit finding.\n', encoding="utf-8")
        text = path.read_text(encoding="utf-8")
        # The instruction file carries no block the production loader can ingest.
        self.assertEqual(text.count("\n## AUDIT_FINDING\n"), 0)
        self.assertNotIn("\n## AUDIT_FINDING\n```json\n", text)
        # And the loader refuses the path outright, independently of its bytes.
        with self.assertRaises(ValueError) as raised:
            load_audit_finding(repository, relative)
        self.assertIn("instruction, template, or configuration", str(raised.exception))

    def test_genuine_separate_report_artifact_is_accepted(self) -> None:
        """A real, disposable report artifact still loads — absence stays absence."""

        repository, _ = self.make_repository()
        path, raw = self._write_audit_report(repository, False, filename="reports-audit.md")
        finding = load_audit_finding(repository, path.name)
        self.assertEqual(finding["producer_role"], "AUDITOR")
        self.assertEqual(finding["verdict"], "CONDITIONAL")
        self.assertIs(finding["load_bearing_gap"], False)
        self.assertEqual(finding["source_path"], path.name)
        self.assertEqual(finding["source_byte_count"], len(raw))
        self.assertEqual(finding["source_sha256"], hashlib.sha256(raw).hexdigest())

        # A report with no block yields no finding; nothing is defaulted.
        (repository / "no-block.md").write_text("# Report\nNo finding.\n", encoding="utf-8")
        with self.assertRaises(ValueError) as raised:
            load_audit_finding(repository, "no-block.md")
        self.assertIn("no ## AUDIT_FINDING block", str(raised.exception))

    def test_instruction_template_and_configuration_paths_are_rejected(self) -> None:
        repository, _ = self.make_repository()
        _, raw = self._write_audit_report(repository, False, filename="valid-report.md")
        inadmissible = (
            ".github/agents/Some Auditor.agent.md",
            ".github/instructions.md",
            ".claude/agents/auditor.md",
            "orchestrator/prompts/auditor.md",
            "orchestrator/config/report.md",
            "AGENTS.md",
            "CLAUDE.md",
            "node.md",
            "LABORATORY_CONSTITUTION.md",
            "INTERACTION_PROTOCOL.md",
            "reports/finding.template.md",
            "reports/finding.example.md",
            "orchestrator/config/orchestrator.config.json",
            "reports/report.txt",
        )
        for relative in inadmissible:
            with self.subTest(relative=relative):
                path = repository / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(raw)
                with self.assertRaises(ValueError):
                    load_audit_finding(repository, relative)

        # A genuine report path with identical bytes still loads.
        admissible = repository / "reports" / "independent-audit.md"
        admissible.parent.mkdir(parents=True, exist_ok=True)
        admissible.write_bytes(raw)
        finding = load_audit_finding(repository, "reports/independent-audit.md")
        self.assertEqual(finding["source_path"], "reports/independent-audit.md")

    def test_valid_false_and_true_reports_preserve_raw_source_identity(self) -> None:
        repository, _ = self.make_repository()
        findings = []
        for gap in (False, True):
            path, raw = self._write_audit_report(
                repository,
                gap,
                filename=f"auditor-{str(gap).lower()}.md",
            )
            finding = load_audit_finding(repository, path.name)
            self.assertIs(finding["load_bearing_gap"], gap)
            self.assertEqual(finding["source_path"], path.name)
            self.assertEqual(finding["source_byte_count"], len(raw))
            self.assertEqual(finding["source_sha256"], hashlib.sha256(raw).hexdigest())
            findings.append(finding)
        self.assertNotEqual(findings[0]["source_sha256"], findings[1]["source_sha256"])

    def test_malformed_report_blocks_fail_closed(self) -> None:
        repository, _ = self.make_repository()
        valid = (
            "## AUDIT_FINDING\n```json\n"
            '{"schema_version":"1.0.0","producer_role":"AUDITOR",'
            '"verdict":"CONDITIONAL","load_bearing_gap":false}\n```\n'
        )
        cases = {
            "duplicate-heading.md": valid + valid,
            "duplicate-key.md": (
                "## AUDIT_FINDING\n```json\n"
                '{"schema_version":"1.0.0","producer_role":"AUDITOR",'
                '"verdict":"CONDITIONAL","load_bearing_gap":false,'
                '"load_bearing_gap":true}\n```\n'
            ),
            "extra-field.md": (
                "## AUDIT_FINDING\n```json\n"
                '{"schema_version":"1.0.0","producer_role":"AUDITOR",'
                '"verdict":"CONDITIONAL","load_bearing_gap":false,'
                '"source_sha256":"' + "0" * 64 + '"}\n```\n'
            ),
            "wrong-type.md": valid.replace('"load_bearing_gap":false', '"load_bearing_gap":"false"'),
            "open-fence.md": valid.removesuffix("```\n"),
        }
        for filename, text in cases.items():
            with self.subTest(filename=filename):
                (repository / filename).write_text(text, encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_audit_finding(repository, filename)

    def test_finding_boolean_changes_decision_id(self) -> None:
        repository, policy = self.make_repository()
        engine = Kernel20Phenotype(repository, policy)
        false_decision = engine.plan(
            "010", "Consider the fixture", audit_finding=self._finding(False)
        )
        true_decision = engine.plan(
            "010", "Consider the fixture", audit_finding=self._finding(True)
        )
        self.assertNotEqual(false_decision["decision_id"], true_decision["decision_id"])

    def test_finding_source_identity_changes_decision_id(self) -> None:
        repository, policy = self.make_repository()
        engine = Kernel20Phenotype(repository, policy)
        first = engine.plan(
            "010", "Consider the fixture", audit_finding=self._finding(False, "a" * 64)
        )
        second = engine.plan(
            "010", "Consider the fixture", audit_finding=self._finding(False, "b" * 64)
        )
        self.assertNotEqual(first["decision_id"], second["decision_id"])

    def test_finding_does_not_change_routing(self) -> None:
        repository, policy = self.make_repository()
        engine = Kernel20Phenotype(repository, policy)
        baseline = engine.plan("010", "Consider the fixture")
        for gap in (False, True):
            withf = engine.plan(
                "010", "Consider the fixture", audit_finding=self._finding(gap)
            )
            self.assertEqual(withf["route"], baseline["route"])
            self.assertEqual(withf["route_scores"], baseline["route_scores"])
            self.assertEqual(withf["route_rationale"], baseline["route_rationale"])
            self.assertEqual(withf["target_files"], baseline["target_files"])
            self.assertEqual(withf["predicted_outputs"], baseline["predicted_outputs"])
            self.assertEqual(withf["approval_required"], baseline["approval_required"])
            self.assertEqual(withf["stop_condition"], baseline["stop_condition"])
            self.assertEqual(withf["equivalence_ledger"], baseline["equivalence_ledger"])

    def test_finding_preserves_audit_routing(self) -> None:
        repository, policy = self.make_repository()
        engine = Kernel20Phenotype(repository, policy)
        goal = "Audit the fixture"
        baseline = engine.plan("010", goal)
        withf = engine.plan("010", goal, audit_finding=self._finding(True))
        self.assertEqual(baseline["problem"]["primary"], "AUDIT")
        self.assertEqual(withf["route"], baseline["route"])
        self.assertEqual(withf["route_scores"], baseline["route_scores"])
        self.assertTrue(withf["equivalence_ledger"]["equivalent"])

    def test_finding_preserves_provenance_routing(self) -> None:
        repository, policy = self.make_repository()
        engine = Kernel20Phenotype(repository, policy)
        goal = "Check provenance of the fixture"
        baseline = engine.plan("010", goal)
        withf = engine.plan("010", goal, audit_finding=self._finding(False))
        self.assertEqual(baseline["problem"]["primary"], "PROVENANCE")
        self.assertEqual(withf["route"], baseline["route"])
        self.assertEqual(withf["route_scores"], baseline["route_scores"])
        self.assertTrue(withf["equivalence_ledger"]["equivalent"])

    def test_finding_preserves_provenance_complete_implementation_routing(self) -> None:
        repository, policy = self.make_repository()
        engine = Kernel20Phenotype(repository, policy)
        goal = "Implement the fixture change"
        baseline = engine.plan("010", goal)
        withf = engine.plan("010", goal, audit_finding=self._finding(True))
        self.assertEqual(baseline["problem"]["primary"], "IMPLEMENTATION")
        self.assertEqual(baseline["route"], "BUILDER")
        self.assertEqual(withf["route"], "BUILDER")
        self.assertEqual(withf["route_scores"], baseline["route_scores"])
        self.assertEqual(withf["target_files"], baseline["target_files"])
        self.assertTrue(withf["equivalence_ledger"]["equivalent"])

    def test_finding_preserves_stop_behavior(self) -> None:
        repository, policy = self.make_repository()
        engine = Kernel20Phenotype(repository, policy)
        goal = "No action, already complete"
        baseline = engine.plan("010", goal)
        self.assertEqual(baseline["route"], "STOP")
        for gap in (False, True):
            withf = engine.plan("010", goal, audit_finding=self._finding(gap))
            self.assertEqual(withf["route"], "STOP")
            self.assertEqual(withf["route_scores"]["STOP"], 1.0)
            self.assertEqual(withf["route_scores"], baseline["route_scores"])
            self.assertEqual(withf["approval_required"], baseline["approval_required"])

    def test_finding_preserves_all_routes_and_unknown_fallback(self) -> None:
        repository, policy = self.make_repository()
        engine = Kernel20Phenotype(repository, policy)
        cases = {
            "Implement the fixture change": "BUILDER",
            "Audit the fixture": "AUDITOR",
            "Consider the fixture": "HOST_APPROVAL",
            "No action, already complete": "STOP",
        }
        for goal, expected_route in cases.items():
            with self.subTest(goal=goal):
                baseline = engine.plan("010", goal)
                self.assertEqual(baseline["route"], expected_route)
                for gap in (False, True):
                    with_finding = engine.plan(
                        "010", goal, audit_finding=self._finding(gap)
                    )
                    for field in (
                        "route",
                        "route_scores",
                        "route_rationale",
                        "target_files",
                        "authority",
                        "equivalence_ledger",
                    ):
                        self.assertEqual(with_finding[field], baseline[field])

    def test_malformed_finding_rejected_by_plan(self) -> None:
        repository, policy = self.make_repository()
        engine = Kernel20Phenotype(repository, policy)
        for key, value in [
            ("load_bearing_gap", 1),
            ("load_bearing_gap", "false"),
            ("source_sha256", "SHORT"),
            ("source_sha256", "A" * 64),
            ("source_byte_count", True),
            ("source_byte_count", -1),
            ("producer_role", "HOST"),
            ("schema_version", "2.0.0"),
            ("verdict", "conditional"),
            ("source_path", ""),
        ]:
            bad = {**self._finding(False), key: value}
            with self.assertRaises(ValueError, msg=f"{key}={value!r} accepted"):
                engine.plan("010", "Consider the fixture", audit_finding=bad)
        missing = {key: value for key, value in self._finding(False).items() if key != "verdict"}
        with self.assertRaises(ValueError):
            engine.plan("010", "Consider the fixture", audit_finding=missing)
        extra = {**self._finding(False), "unexpected": "value"}
        with self.assertRaises(ValueError):
            engine.plan("010", "Consider the fixture", audit_finding=extra)

    def test_malformed_finding_rejected_by_validate_decision(self) -> None:
        repository, policy = self.make_repository()
        decision = Kernel20Phenotype(repository, policy).plan(
            "010", "Consider the fixture", audit_finding=self._finding(False)
        )
        corrupted = {
            **decision,
            "audit_finding": {**decision["audit_finding"], "load_bearing_gap": 1},
        }
        with self.assertRaises(ValueError):
            validate_decision(corrupted)

    def test_absent_finding_preserves_existing_behavior(self) -> None:
        repository, policy = self.make_repository()
        engine = Kernel20Phenotype(repository, policy)
        positional = engine.plan("010", "Consider the fixture")
        explicit_none = engine.plan("010", "Consider the fixture", audit_finding=None)
        self.assertEqual(positional["decision_id"], explicit_none["decision_id"])
        self.assertEqual(positional, explicit_none)
        self.assertNotIn("audit_finding", positional)
        validate_decision(positional)

    def test_implementation_goal_routes_to_builder(self) -> None:
        repository, policy = self.make_repository()
        decision = Kernel20Phenotype(repository, policy).plan("010", "Implement the bounded fixture")
        self.assertEqual(decision["problem"]["primary"], "IMPLEMENTATION")
        self.assertEqual(decision["route"], "BUILDER")

    def test_audit_goal_routes_to_auditor(self) -> None:
        repository, policy = self.make_repository()
        decision = Kernel20Phenotype(repository, policy).plan("010", "Audit the bounded fixture")
        self.assertEqual(decision["route"], "AUDITOR")

    def test_provenance_uncertainty_suppresses_builder(self) -> None:
        repository, policy = self.make_repository(provenance=False)
        decision = Kernel20Phenotype(repository, policy).plan("010", "Implement the bounded fixture")
        self.assertNotEqual(decision["route"], "BUILDER")
        self.assertEqual(decision["route_scores"]["BUILDER"], 0.0)

    def test_protected_path_overlap_routes_to_host_approval(self) -> None:
        repository, policy = self.make_repository(experiment="009")
        decision = Kernel20Phenotype(repository, policy).plan("009", "Implement the protected fixture")
        self.assertEqual(decision["route"], "HOST_APPROVAL")
        self.assertEqual(decision["target_files"], [])

    def test_missing_protected_experiment_still_routes_to_host_approval(self) -> None:
        repository, policy = self.make_repository(experiment="010")
        decision = Kernel20Phenotype(repository, policy).plan("009", "Implement Experiment 009")
        self.assertEqual(decision["route"], "HOST_APPROVAL")
        self.assertIn("protected path overlap exists", decision["affordances"])

    def test_admission_requires_host_approval(self) -> None:
        repository, policy = self.make_repository()
        decision = Kernel20Phenotype(repository, policy).plan("010", "Admit this fixture")
        self.assertEqual(decision["route"], "HOST_APPROVAL")

    def test_closure_without_independent_audit_routes_to_auditor(self) -> None:
        repository, policy = self.make_repository()
        (repository / "receipts" / "20260802T000000Z__implement-experiment-010__RCP-test.md").write_text(
            "completion_status: COMPLETE\n", encoding="utf-8"
        )
        decision = Kernel20Phenotype(repository, policy).plan("010", "Close Experiment 010")
        self.assertEqual(decision["route"], "AUDITOR")

    def test_closure_with_implementation_and_audit_routes_to_host(self) -> None:
        repository, policy = self.make_repository()
        receipt_root = repository / "receipts"
        (receipt_root / "20260802T000000Z__implement-experiment-010__RCP-test.md").write_text(
            "completion_status: COMPLETE\n", encoding="utf-8"
        )
        (receipt_root / "20260803T000000Z__audit-experiment-010__RCP-test.md").write_text(
            "completion_status: COMPLETE\n", encoding="utf-8"
        )
        decision = Kernel20Phenotype(repository, policy).plan("010", "Close Experiment 010")
        self.assertEqual(decision["route"], "HOST_APPROVAL")

    def test_already_complete_state_routes_to_stop(self) -> None:
        repository, policy = self.make_repository(state_text="EXPERIMENT_010_COMPLETE\n")
        decision = Kernel20Phenotype(repository, policy).plan("010", "Close Experiment 010")
        self.assertEqual(decision["route"], "STOP")
        self.assertEqual(decision["target_files"], [])

    def test_ambiguous_goal_routes_to_host_approval(self) -> None:
        repository, policy = self.make_repository()
        decision = Kernel20Phenotype(repository, policy).plan("010", "Consider the fixture")
        self.assertEqual(decision["problem"]["primary"], "UNKNOWN")
        self.assertEqual(decision["route"], "HOST_APPROVAL")

    def test_bounds_are_enforced_and_file_observation_is_bounded(self) -> None:
        with self.assertRaises(ValueError):
            Bounds.from_mapping({"max_files": 201})
        repository, policy = self.make_repository(file_count=5)
        policy["kernel20Bounds"]["max_files"] = 2
        observation = Kernel20Phenotype(repository, policy).observe("010")
        self.assertEqual(len(observation.experiment_files), 2)

    def test_decision_id_is_deterministic(self) -> None:
        repository, policy = self.make_repository()
        engine = Kernel20Phenotype(repository, policy)
        first = engine.plan("010", "Implement the bounded fixture")
        second = engine.plan("010", "Implement the bounded fixture")
        self.assertEqual(first["decision_id"], second["decision_id"])
        self.assertEqual(first, second)

    def test_legacy_engine_remains_callable(self) -> None:
        policy = json.loads((ORCHESTRATOR_ROOT / "config" / "orchestrator.config.json").read_text())
        repository, policy = self.make_repository()
        result = run_legacy(repository, policy, "010", "Close Experiment 010", dry_test=True)
        self.assertEqual(result["DRY_TEST_RESULT"], "PASS")

    def test_packet_and_json_sidecar_are_created(self) -> None:
        repository, policy = self.make_repository()
        engine = Kernel20Phenotype(repository, policy)
        decision = engine.plan("010", "Audit the bounded fixture")
        outputs = engine.emit_outputs(decision, timestamp="20260804T000000Z")
        decision_path = Path(outputs["decision_path"])
        packet_path = Path(outputs["packet_path"])
        self.assertTrue(decision_path.is_file())
        self.assertTrue(packet_path.is_file())
        self.assertIn("Mode: READ_ONLY_AUDIT", packet_path.read_text(encoding="utf-8"))
        self.assertTrue(str(decision_path).startswith(str(repository / "orchestrator")))
        self.assertTrue(str(packet_path).startswith(str(repository / "orchestrator")))

    def test_packet_renders_exact_false_and_true_finding(self) -> None:
        repository, policy = self.make_repository()
        engine = Kernel20Phenotype(repository, policy)
        for index, gap in enumerate((False, True), start=10):
            finding = self._finding(gap, ("a" if not gap else "b") * 64)
            decision = engine.plan(
                "010", "Implement the bounded fixture", audit_finding=finding
            )
            outputs = engine.emit_outputs(
                decision, timestamp=f"20260804T0000{index}Z"
            )
            packet = Path(outputs["packet_path"]).read_text(encoding="utf-8")
            self.assertIn("## Decision-owned audit finding", packet)
            block = packet.split("## Decision-owned audit finding", 1)[1]
            encoded = block.split("```json\n", 1)[1].split("\n```", 1)[0]
            self.assertEqual(json.loads(encoded), finding)
            self.assertIn(f'"load_bearing_gap": {str(gap).lower()}', block)
            self.assertIn(finding["source_path"], block)
            self.assertIn(finding["source_sha256"], block)

    def test_no_finding_packet_remains_legacy_compatible(self) -> None:
        repository, policy = self.make_repository()
        engine = Kernel20Phenotype(repository, policy)
        decision = engine.plan("010", "Audit the bounded fixture")
        outputs = engine.emit_outputs(decision, timestamp="20260804T000020Z")
        packet = Path(outputs["packet_path"]).read_text(encoding="utf-8")
        self.assertNotIn("Decision-owned audit finding", packet)
        self.assertNotIn("audit_finding:", packet)
        self.assertIn("Mode: READ_ONLY_AUDIT", packet)

    def test_host_and_stop_packets_contain_no_executable_task(self) -> None:
        repository, policy = self.make_repository()
        engine = Kernel20Phenotype(repository, policy)
        host = engine.plan("010", "Admit this fixture")
        host_outputs = engine.emit_outputs(host, timestamp="20260804T000001Z")
        host_packet = Path(host_outputs["packet_path"]).read_text(encoding="utf-8")
        self.assertIn("No executable instruction", host_packet)
        self.assertNotIn("Mode: IMPLEMENT", host_packet)

        (repository / "CURRENT_STATE.md").write_text("EXPERIMENT_010_COMPLETE\n", encoding="utf-8")
        stop = engine.plan("010", "Close Experiment 010")
        stop_outputs = engine.emit_outputs(stop, timestamp="20260804T000002Z")
        stop_packet = Path(stop_outputs["packet_path"]).read_text(encoding="utf-8")
        self.assertIn("No task. Stop without repository action.", stop_packet)
        self.assertNotIn("Mode: IMPLEMENT", stop_packet)

    def test_matching_receipt_reconciles_to_close(self) -> None:
        repository, policy = self.make_repository()
        decision = Kernel20Phenotype(repository, policy).plan("010", "Implement the bounded fixture")
        receipt = "\n".join([
            "completion_status: COMPLETE",
            "observed_outputs:",
            "  - bounded implementation at the declared target",
            "  - one append-only implementation receipt",
            "target_files:",
            "  - experiments/010-fixture",
            "files_created:",
            "  - receipts/matching.md",
            "stop_condition_met: true",
        ])
        result = reconcile_decision(decision, receipt, "receipts/matching.md")
        self.assertEqual(result["outcome"], "CLOSE")
        self.assertEqual(result["mismatches"], [])

    def test_mismatching_receipt_recalibrates(self) -> None:
        repository, policy = self.make_repository()
        decision = Kernel20Phenotype(repository, policy).plan("010", "Implement the bounded fixture")
        receipt = "completion_status: INCOMPLETE\nstop_condition_met: false\n"
        result = reconcile_decision(decision, receipt, "receipts/mismatch.md")
        self.assertIn(result["outcome"], {"RECALIBRATE", "AUDIT"})
        self.assertGreater(len(result["mismatches"]), 0)

    def _decision_owning_real_report(
        self,
        repository: Path,
        policy: dict,
        *,
        load_bearing_gap: bool = False,
        filename: str = "reports/owned-audit.md",
    ) -> dict:
        """Plan a decision that owns a finding bound to a real report artifact."""

        path = repository / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        self._write_audit_report(path.parent, load_bearing_gap, filename=path.name)
        finding = load_audit_finding(repository, filename)
        return Kernel20Phenotype(repository, policy).plan(
            "010", "Implement the bounded fixture", audit_finding=finding
        )

    @staticmethod
    def _close_receipt(decision: dict, name: str, *, finding_lines: list[str]) -> str:
        return "\n".join([
            "completion_status: COMPLETE",
            "observed_outputs:",
            "  - bounded implementation at the declared target",
            "  - one append-only implementation receipt",
            "target_files:",
            "  - experiments/010-fixture",
            "files_created:",
            f"  - receipts/{name}",
            "stop_condition_met: true",
            *finding_lines,
        ])

    def test_matching_finding_receipt_preserves_close_path(self) -> None:
        repository, policy = self.make_repository()
        decision = self._decision_owning_real_report(repository, policy)
        receipt = self._close_receipt(
            decision,
            "matching-finding.md",
            finding_lines=self._receipt_finding_block(decision["audit_finding"]),
        )
        result = reconcile_decision(
            decision,
            receipt,
            "receipts/matching-finding.md",
            repository_root=repository,
        )
        self.assertEqual(result["outcome"], "CLOSE")
        self.assertEqual(result["mismatches"], [])

    def test_missing_or_mismatched_finding_never_closes(self) -> None:
        repository, policy = self.make_repository()
        decision = self._decision_owning_real_report(repository, policy)
        base = [
            "completion_status: COMPLETE",
            "observed_outputs:",
            "  - bounded implementation at the declared target",
            "  - one append-only implementation receipt",
            "target_files:",
            "  - experiments/010-fixture",
            "stop_condition_met: true",
        ]
        cases: dict[str, list[str]] = {"missing": []}
        for field, value in (
            ("load_bearing_gap", True),
            ("source_path", "reports/different.md"),
            ("source_byte_count", 129),
            ("source_sha256", "f" * 64),
        ):
            cases[field] = self._receipt_finding_block(
                {**decision["audit_finding"], field: value}
            )
        for label, finding_lines in cases.items():
            with self.subTest(label=label):
                result = reconcile_decision(
                    decision,
                    "\n".join([*base, *finding_lines]),
                    f"receipts/{label}.md",
                    repository_root=repository,
                )
                self.assertNotEqual(result["outcome"], "CLOSE")
                self.assertTrue(
                    any("audit_finding" in mismatch for mismatch in result["mismatches"])
                )

    def test_changed_missing_or_foreign_raw_source_never_closes(self) -> None:
        """An exact old echo cannot certify raw evidence that has since moved."""

        cases = {
            "changed": lambda repository, relative: (repository / relative).write_text(
                "# Independent audit\n\nTampered after the decision.\n", encoding="utf-8"
            ),
            "missing": lambda repository, relative: (repository / relative).unlink(),
        }
        for label, mutate in cases.items():
            with self.subTest(label=label):
                repository, policy = self.make_repository()
                relative = "reports/owned-audit.md"
                decision = self._decision_owning_real_report(repository, policy)
                receipt = self._close_receipt(
                    decision,
                    f"{label}.md",
                    finding_lines=self._receipt_finding_block(decision["audit_finding"]),
                )
                mutate(repository, relative)
                result = reconcile_decision(
                    decision, receipt, f"receipts/{label}.md", repository_root=repository
                )
                self.assertNotEqual(result["outcome"], "CLOSE")
                self.assertTrue(
                    any(
                        "audit_finding source" in mismatch
                        for mismatch in result["mismatches"]
                    ),
                    result["mismatches"],
                )
                shutil.rmtree(repository)

    def test_owned_source_outside_trusted_root_never_closes(self) -> None:
        repository, policy = self.make_repository()
        decision = self._decision_owning_real_report(repository, policy)
        escaped = dict(decision["audit_finding"], source_path="../escaped-report.md")
        decision = Kernel20Phenotype(repository, policy).plan(
            "010", "Implement the bounded fixture", audit_finding=escaped
        )
        receipt = self._close_receipt(
            decision,
            "escaped.md",
            finding_lines=self._receipt_finding_block(decision["audit_finding"]),
        )
        result = reconcile_decision(
            decision, receipt, "receipts/escaped.md", repository_root=repository
        )
        self.assertNotEqual(result["outcome"], "CLOSE")
        self.assertTrue(
            any("not repository-relative" in item for item in result["mismatches"]),
            result["mismatches"],
        )

    def test_owned_instruction_surface_source_never_closes(self) -> None:
        repository, policy = self.make_repository()
        role_relative = ".github/agents/Whitestone Auditor.agent.md"
        role_path = repository / role_relative
        role_path.parent.mkdir(parents=True, exist_ok=True)
        raw = b'Synthetic role instructions only. This is not an audit finding.\n'
        role_path.write_bytes(raw)
        decision = self._decision_owning_real_report(repository, policy)
        substituted = dict(
            decision["audit_finding"],
            source_path=role_relative,
            source_byte_count=len(raw),
            source_sha256=hashlib.sha256(raw).hexdigest(),
        )
        decision = Kernel20Phenotype(repository, policy).plan(
            "010", "Implement the bounded fixture", audit_finding=substituted
        )
        receipt = self._close_receipt(
            decision,
            "instruction.md",
            finding_lines=self._receipt_finding_block(decision["audit_finding"]),
        )
        result = reconcile_decision(
            decision, receipt, "receipts/instruction.md", repository_root=repository
        )
        self.assertNotEqual(result["outcome"], "CLOSE")
        self.assertTrue(
            any(
                "instruction, template, or configuration" in item
                for item in result["mismatches"]
            ),
            result["mismatches"],
        )

    def test_owned_finding_without_trusted_root_never_closes(self) -> None:
        repository, policy = self.make_repository()
        decision = self._decision_owning_real_report(repository, policy)
        receipt = self._close_receipt(
            decision,
            "no-root.md",
            finding_lines=self._receipt_finding_block(decision["audit_finding"]),
        )
        result = reconcile_decision(decision, receipt, "receipts/no-root.md")
        self.assertNotEqual(result["outcome"], "CLOSE")
        self.assertTrue(
            any("trusted repository root" in item for item in result["mismatches"]),
            result["mismatches"],
        )

    def test_proxy_denominator_counts_the_checks_actually_performed(self) -> None:
        repository, policy = self.make_repository()
        decision = self._decision_owning_real_report(repository, policy)
        mismatched = dict(decision["audit_finding"], load_bearing_gap=True)
        receipt = self._close_receipt(
            decision,
            "denominator.md",
            finding_lines=self._receipt_finding_block(mismatched),
        )
        result = reconcile_decision(
            decision, receipt, "receipts/denominator.md", repository_root=repository
        )
        legacy_denominator = (
            len(decision["predicted_outputs"]) + len(decision["target_files"]) + 2
        )
        # Seven finding-field comparisons and seven source-freshness checks were
        # performed, so normalization must count fourteen more than the legacy shape.
        self.assertEqual(result["comparison_count"], legacy_denominator + 14)
        self.assertEqual(
            result["prediction_error_proxy"],
            round(min(1.0, len(result["mismatches"]) / result["comparison_count"]), 6),
        )
        self.assertNotEqual(
            result["prediction_error_proxy"],
            round(min(1.0, len(result["mismatches"]) / legacy_denominator), 6),
        )

    def test_no_finding_reconciliation_retains_historical_denominator(self) -> None:
        repository, policy = self.make_repository()
        decision = Kernel20Phenotype(repository, policy).plan(
            "010", "Implement the bounded fixture"
        )
        receipt = "completion_status: INCOMPLETE\nstop_condition_met: false\n"
        result = reconcile_decision(
            decision, receipt, "receipts/legacy.md", repository_root=repository
        )
        legacy_denominator = (
            len(decision["predicted_outputs"]) + len(decision["target_files"]) + 2
        )
        self.assertEqual(result["comparison_count"], legacy_denominator)
        self.assertEqual(
            result["prediction_error_proxy"],
            round(min(1.0, len(result["mismatches"]) / legacy_denominator), 6),
        )

    def test_no_task_is_automatically_executed(self) -> None:
        repository, policy = self.make_repository()
        sentinel = repository / "executed.txt"
        goal = f"Implement the fixture then create {sentinel}"
        Kernel20Phenotype(repository, policy).plan("010", goal)
        self.assertFalse(sentinel.exists())

    def test_route_score_ties_use_safe_precedence(self) -> None:
        scores = {"BUILDER": 0.8, "AUDITOR": 0.8, "HOST_APPROVAL": 0.8, "STOP": 0.8}
        self.assertEqual(select_route(scores, 0.8), "STOP")

    def test_missing_state_uses_recalibration_regime(self) -> None:
        repository, policy = self.make_repository()
        (repository / "CURRENT_STATE.md").unlink()
        decision = Kernel20Phenotype(repository, policy).plan("010", "Audit the bounded fixture")
        self.assertEqual(decision["regime_contract"]["regime"], "RECALIBRATION")
        self.assertEqual(decision["route"], "AUDITOR")


if __name__ == "__main__":
    unittest.main()
