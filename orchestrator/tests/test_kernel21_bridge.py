from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest import mock


ORCHESTRATOR_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = ORCHESTRATOR_ROOT.parent
sys.path.insert(0, str(ORCHESTRATOR_ROOT))

import bootstrap
from bridge import (
    build_preflight,
    create_lifecycle_witness,
    emit_preflight_artifacts,
    verify_lifecycle_witness,
)
from bridge.attachment import B_ATTACH
from bridge.contracts import (
    CapabilityState,
    ReturnStatus,
    compute_contract_id,
    sha256_json,
    validate_envelope,
)
from bridge.handoff import B_HANDOFF
from bridge.permissions import unknown_capabilities
from bridge.return_verify import B_RETURN
from bridge.transport import B_TRANSPORT, select_transport
from bridge.tribunal import B_ATTACHMENT_TRIBUNAL
from engine.contracts import compute_decision_id
from engine.kernel20_phenotype import Kernel20Phenotype
from engine.legacy_engine import run_legacy


class Kernel21BridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = Path(tempfile.mkdtemp(prefix="lodestone-kernel21-test-")).resolve()
        self.repository, self.policy = self.make_repository()
        self.carrier: dict[str, tuple[dict, dict]] = {}

    def tearDown(self) -> None:
        shutil.rmtree(self.fixture)

    def make_repository(self) -> tuple[Path, dict]:
        repository = self.fixture / "repository"
        schema_root = repository / "orchestrator" / "schemas"
        schema_root.mkdir(parents=True)
        (repository / "orchestrator" / "output").mkdir()
        for name in (
            "orchestration_decision.schema.json",
            "attachment_contract.schema.json",
            "transport_plan.schema.json",
            "return_evidence.schema.json",
            "lifecycle_witness.schema.json",
        ):
            shutil.copyfile(ORCHESTRATOR_ROOT / "schemas" / name, schema_root / name)
        for name in ("LABORATORY_CONSTITUTION.md", "INTERACTION_PROTOCOL.md"):
            (repository / name).write_text("# Synthetic local governing document\nNo inherited authority.\n", encoding="utf-8")
        (repository / "node.md").write_text("# Synthetic inert node\nAUTHORITY               = NONE\nEXECUTABLE              = FALSE\n", encoding="utf-8")
        (repository / "CURRENT_STATE.md").write_text(
            "# State\nExperiment 010 is planned.\n", encoding="utf-8"
        )
        experiment = repository / "experiments" / "010-fixture"
        experiment.mkdir(parents=True)
        (experiment / "notes.md").write_text("fixture\n", encoding="utf-8")
        (repository / "receipts").mkdir()
        source = repository / "sources" / "experiment-010"
        source.mkdir(parents=True)
        for name in ("SOURCE_MANIFEST.json", "SOURCE_DECLARATION.md", "SHA256SUMS.txt"):
            (source / name).write_text("fixture\n", encoding="utf-8")
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

    def capabilities(self, *, read: str = "OBSERVED", write: str = "OBSERVED") -> dict[str, str]:
        capabilities = unknown_capabilities()
        capabilities["read"] = read
        capabilities["write"] = write
        capabilities["execute"] = CapabilityState.OBSERVED.value
        capabilities["git_read"] = CapabilityState.DENIED.value
        capabilities["git_write"] = CapabilityState.DENIED.value
        return capabilities

    def decision(
        self,
        goal: str = "Implement the bounded fixture",
        *,
        audit_finding: dict | None = None,
    ) -> dict:
        return Kernel20Phenotype(self.repository, self.policy).plan(
            "010", goal, audit_finding=audit_finding
        )

    def _finding(self, load_bearing_gap: bool, sha: str = "0" * 64) -> dict:
        """A synthetic finding for identity and handoff tests that never return."""

        return {
            "schema_version": "1.0.0",
            "producer_role": "AUDITOR",
            "verdict": "CONDITIONAL",
            "load_bearing_gap": load_bearing_gap,
            "source_path": "receipts/example-finding.md",
            "source_byte_count": 128,
            "source_sha256": sha,
        }

    def _report_finding(self, load_bearing_gap: bool, *, name: str = "owned-audit") -> dict:
        """Write a genuine report artifact and load the finding it actually carries.

        Return verification and reconciliation recheck the current source bytes,
        so anything that reaches those legs must be bound to a real artifact.
        """

        relative = f"reports/{name}.md"
        path = self.repository / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "# Independent audit\n\n"
            f"Bounded evidence for {name}.\n\n"
            "## AUDIT_FINDING\n"
            "```json\n"
            "{\n"
            '  "schema_version": "1.0.0",\n'
            '  "producer_role": "AUDITOR",\n'
            '  "verdict": "CONDITIONAL",\n'
            f'  "load_bearing_gap": {str(load_bearing_gap).lower()}\n'
            "}\n"
            "```\n",
            encoding="utf-8",
        )
        from engine.kernel20_phenotype import load_audit_finding

        return load_audit_finding(self.repository, relative)

    def attachment(
        self,
        *,
        execution_root: Path | None = None,
        writable: bool = True,
        observed_at: datetime | None = None,
    ) -> dict:
        execution = execution_root or self.repository
        readable_roots = [str(self.repository), str(execution)]
        writable_roots: list[str] = []
        if writable and execution != self.repository:
            writable_roots = [str(execution), str(self.repository)]
        elif writable:
            writable_roots = [str(self.repository)]
        return B_ATTACH(
            self.repository,
            execution,
            readable_roots=readable_roots,
            writable_roots=writable_roots,
            capabilities=self.capabilities(write="OBSERVED" if writable else "DENIED"),
            observed_at=observed_at,
        )

    def envelope(
        self,
        audit_finding: dict | None = None,
    ) -> tuple[dict, dict, dict]:
        decision = self.decision(audit_finding=audit_finding)
        attachment = self.attachment()
        plan = B_TRANSPORT(attachment, decision, host_approval_satisfied=True)
        envelope = B_HANDOFF(decision, attachment, plan)
        # Remember this lifecycle's trusted carrier so return verification can
        # bind outer custody to something the envelope's holder cannot rewrite.
        self.carrier[envelope["envelope_id"]] = (attachment, plan)
        return envelope, attachment, decision

    def carrier_for(self, envelope: dict) -> tuple[dict, dict]:
        return self.carrier[envelope["envelope_id"]]

    def verify_return(self, envelope: dict, receipt, **kwargs):
        """Verify a return with this lifecycle's persisted trusted carrier."""

        attachment, plan = kwargs.pop("carrier", None) or self.carrier_for(envelope)
        return B_RETURN(
            envelope,
            receipt,
            attachment=attachment,
            transport_plan=plan,
            **kwargs,
        )

    def test_recomputed_envelope_with_invalid_nested_decision_id_is_rejected(self) -> None:
        decision = self.decision()
        decision["audit_finding"] = self._finding(False)
        decision["decision_id"] = compute_decision_id(decision)
        attachment = self.attachment()
        plan = B_TRANSPORT(attachment, decision, host_approval_satisfied=True)
        envelope = B_HANDOFF(decision, attachment, plan)
        envelope["kernel20_decision"]["audit_finding"]["load_bearing_gap"] = True
        envelope["kernel20_payload_sha256"] = sha256_json(envelope["kernel20_decision"])
        with self.assertRaises(ValueError):
            validate_envelope(envelope)

    def test_finding_bearing_decision_survives_handoff_unchanged(self) -> None:
        decision = self.decision()
        decision["audit_finding"] = self._finding(False)
        decision["decision_id"] = compute_decision_id(decision)
        attachment = self.attachment()
        plan = B_TRANSPORT(attachment, decision, host_approval_satisfied=True)
        envelope = B_HANDOFF(decision, attachment, plan)
        validate_envelope(envelope)
        self.assertEqual(
            envelope["kernel20_decision"]["audit_finding"], decision["audit_finding"]
        )
        self.assertEqual(
            envelope["kernel20_decision"]["decision_id"], decision["decision_id"]
        )

    def test_boolean_and_source_tamper_after_handoff_are_rejected(self) -> None:
        cases = (
            ("load_bearing_gap", True),
            ("source_path", "reports/substituted.md"),
            ("source_byte_count", 129),
            ("source_sha256", "f" * 64),
        )
        for field, value in cases:
            with self.subTest(field=field):
                decision = self.decision(audit_finding=self._finding(False))
                attachment = self.attachment()
                plan = B_TRANSPORT(attachment, decision, host_approval_satisfied=True)
                envelope = B_HANDOFF(decision, attachment, plan)
                envelope["kernel20_decision"]["audit_finding"][field] = value
                envelope["kernel20_payload_sha256"] = sha256_json(
                    envelope["kernel20_decision"]
                )
                with self.assertRaises(ValueError):
                    validate_envelope(envelope)

    def witness_inputs(self, *, goal: str | None = None) -> dict[str, Path | dict]:
        decision = self.decision(goal or "Implement the bounded fixture")
        decision["route"] = "BUILDER"
        decision["route_scores"] = {
            "AUDITOR": 0.0,
            "BUILDER": 1.0,
            "HOST_APPROVAL": 0.5,
            "STOP": 0.0,
        }
        decision["route_rationale"] = "Fixture Host approval selects the Builder lifecycle."
        decision["target_files"] = ["orchestrator/bridge/witness.py"]
        decision["host_authorization_id"] = "AP-FIXTURE"
        decision["allowed_create_targets"] = []
        decision["allowed_modify_targets"] = list(decision["target_files"])
        decision["protected_scope"] = ["node.md", *decision["protected_files"]]
        node_data = (self.repository / "node.md").read_bytes()
        decision["source_node_identity"] = {
            "path": "node.md",
            "byte_count": len(node_data),
            "sha256": hashlib.sha256(node_data).hexdigest(),
            "authority": "NONE",
            "executable": False,
            "mutation_allowed": False,
        }
        decision["decision_id"] = compute_decision_id(decision)
        evidence_root = self.repository / "evidence"
        evidence_root.mkdir(exist_ok=True)
        decision_path = evidence_root / "approved-decision.json"
        decision_path.write_text(
            json.dumps(decision, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        body_stat = self.repository.stat()
        body = {
            "path": str(self.repository),
            "device": body_stat.st_dev,
            "inode": body_stat.st_ino,
        }
        access = {
            "access_point_id": "AP-FIXTURE",
            "canonical_body": body,
            "exact_modify_targets": decision["allowed_modify_targets"],
            "protected_scope": decision["protected_scope"],
        }
        access_path = evidence_root / "access-point.json"
        access_path.write_text(
            json.dumps(access, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        approval = {
            "approval_id": "",
            "status": "APPROVED",
            "authorization": {"builder_standing_authorized": True},
            "selected_organ": "BUILDER",
            "canonical_body": body,
            "access_point": {
                "path": str(access_path.relative_to(self.repository)),
                "byte_count": access_path.stat().st_size,
                "sha256": hashlib.sha256(access_path.read_bytes()).hexdigest(),
                "access_point_id": access["access_point_id"],
            },
            "repair_decision": {
                "path": str(decision_path.relative_to(self.repository)),
                "byte_count": decision_path.stat().st_size,
                "sha256": hashlib.sha256(decision_path.read_bytes()).hexdigest(),
                "decision_id": decision["decision_id"],
            },
            "create_targets": [],
            "modify_targets": decision["allowed_modify_targets"],
            "protected_scope": decision["protected_scope"],
        }
        approval_payload = copy.deepcopy(approval)
        approval_payload.pop("approval_id")
        approval["approval_id"] = "APR-" + hashlib.sha256(
            json.dumps(
                approval_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()[:24]
        approval_path = evidence_root / "host-approval.json"
        approval_path.write_text(
            json.dumps(approval, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        return {
            "decision": decision,
            "decision_path": decision_path,
            "node_source_path": self.repository / "node.md",
            "access_point_path": access_path,
            "approval_path": approval_path,
        }

    def witness(self, *, observed_at: datetime | None = None, goal: str | None = None) -> dict:
        inputs = self.witness_inputs(goal=goal)
        return self.create_from_inputs(inputs, observed_at=observed_at)

    def create_from_inputs(
        self,
        inputs: dict[str, Path | dict],
        *,
        observed_at: datetime | None = None,
        node_source_path: Path | None = None,
    ) -> dict:
        return create_lifecycle_witness(
            self.repository,
            inputs["decision_path"],
            node_source_path or inputs["node_source_path"],
            access_point_path=inputs["access_point_path"],
            host_approval_record_path=inputs["approval_path"],
            execution_root=self.repository,
            observed_at=observed_at,
        )

    def rewrite_witness_json(self, result: dict, filename: str, transform) -> Path:
        directory = self.repository / result["directory"]
        path = directory / filename
        value = json.loads(path.read_text(encoding="utf-8"))
        transform(value)
        path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        manifest_path = directory / "lifecycle-witness.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for artifact in manifest["artifacts"]:
            if artifact["path"] == str(path.relative_to(self.repository)):
                artifact["byte_count"] = path.stat().st_size
                artifact["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
                break
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        newest = max(
            (self.repository / artifact["path"]).stat().st_mtime_ns
            for artifact in manifest["artifacts"]
        )
        if manifest_path.stat().st_mtime_ns <= newest:
            os.utime(manifest_path, ns=(manifest_path.stat().st_atime_ns, newest + 1))
        return directory

    def write_return_receipt(
        self,
        envelope: dict,
        *,
        decision_id: str | None = None,
        observed_root: Path | None = None,
        approval: bool = True,
        audit_finding: dict | None = None,
        filename: str = "return.md",
    ) -> Path:
        path = self.repository / "receipts" / filename
        lines = [
            "completion_status: COMPLETE",
            f"decision_id: {decision_id or envelope['kernel20_decision_id']}",
            f"attachment_contract_id: {envelope['attachment_contract_id']}",
            f"attachment_epoch: {envelope['attachment_epoch']}",
            f"observed_root: {observed_root or self.repository}",
            f"host_approval_obtained: {str(approval).lower()}",
            "observed_outputs:",
            *(f"- {item}" for item in envelope["predicted_outputs"]),
            "target_files:",
            *(f"- {item}" for item in envelope["target_files"]),
            "stop_condition_met: true",
        ]
        if audit_finding is not None:
            lines.extend(
                (
                    "audit_finding:",
                    "```json",
                    json.dumps(audit_finding, ensure_ascii=False, sort_keys=True, indent=2),
                    "```",
                )
            )
        lines.append("")
        path.write_text(
            "\n".join(lines),
            encoding="utf-8",
        )
        return path

    def test_local_matching_writable_body_permits_direct_transport_proposal(self) -> None:
        attachment = self.attachment()
        plan = B_TRANSPORT(attachment, self.decision(), host_approval_satisfied=True)
        self.assertEqual(attachment["body_identity"], "MATCH")
        self.assertEqual(plan["transport_selected"], "DIRECT_FILESYSTEM")

    def test_temporary_copy_cannot_use_direct_filesystem_transport(self) -> None:
        sandbox = self.fixture / "sandbox"
        sandbox.mkdir()
        attachment = B_ATTACH(
            self.repository,
            sandbox,
            readable_roots=[str(self.repository), str(sandbox)],
            writable_roots=[str(sandbox)],
            capabilities=self.capabilities(),
        )
        plan = B_TRANSPORT(attachment, self.decision(), host_approval_satisfied=True)
        self.assertEqual(attachment["environment"], "TEMPORARY_COPY")
        self.assertEqual(attachment["body_identity"], "MISMATCH")
        self.assertNotEqual(plan["transport_selected"], "DIRECT_FILESYSTEM")

    def test_read_only_requested_repository_suppresses_write_transport(self) -> None:
        attachment = self.attachment(writable=False)
        plan = B_TRANSPORT(attachment, self.decision(), host_approval_satisfied=True)
        self.assertEqual(attachment["attachment_state"], "READ_ONLY")
        self.assertNotEqual(plan["transport_selected"], "DIRECT_FILESYSTEM")

    def test_display_name_match_with_path_mismatch_remains_mismatch(self) -> None:
        displayed_match = self.fixture / "other" / self.repository.name
        displayed_match.mkdir(parents=True)
        attachment = B_ATTACH(
            self.repository,
            displayed_match,
            readable_roots=[str(self.repository), str(displayed_match)],
            writable_roots=[str(displayed_match)],
            capabilities=self.capabilities(),
        )
        self.assertEqual(displayed_match.name, self.repository.name)
        self.assertEqual(attachment["body_identity"], "MISMATCH")

    def test_protected_path_overlap_suppresses_direct_transport(self) -> None:
        decision = copy.deepcopy(self.decision())
        decision["target_files"] = ["experiments/009-protected"]
        plan = B_TRANSPORT(self.attachment(), decision, host_approval_satisfied=True)
        self.assertTrue(plan["protected_overlap"])
        self.assertNotEqual(plan["transport_selected"], "DIRECT_FILESYSTEM")

    def test_stale_attachment_epoch_invalidates_handoff(self) -> None:
        observed = datetime.now(timezone.utc) - timedelta(hours=1)
        attachment = self.attachment(observed_at=observed)
        decision = self.decision()
        plan = B_TRANSPORT(attachment, decision, host_approval_satisfied=True)
        verdict = B_ATTACHMENT_TRIBUNAL(decision, attachment, plan)
        self.assertFalse(verdict["accepted"])
        self.assertIn("attachment epoch is stale", verdict["issues"])

    def test_altered_kernel20_payload_hash_fails_tribunal(self) -> None:
        decision = self.decision()
        expected = sha256_json(decision)
        decision["goal"] = "Altered after declaration"
        attachment = self.attachment()
        plan = B_TRANSPORT(attachment, decision, host_approval_satisfied=True)
        verdict = B_ATTACHMENT_TRIBUNAL(
            decision, attachment, plan, expected_payload_sha256=expected
        )
        self.assertFalse(verdict["accepted"])
        self.assertIn("Kernel 20 payload hash changed after declaration", verdict["issues"])

    def test_transport_without_return_path_fails_tribunal(self) -> None:
        decision = self.decision()
        attachment = self.attachment()
        plan = B_TRANSPORT(attachment, decision, host_approval_satisfied=True)
        plan["return_root"] = ""
        from bridge.contracts import compute_contract_id
        plan["transport_plan_id"] = compute_contract_id(plan, "transport_plan_id", "TRN")
        verdict = B_ATTACHMENT_TRIBUNAL(decision, attachment, plan)
        self.assertFalse(verdict["accepted"])
        self.assertIn("transport has no return path", verdict["issues"])

    def test_sandbox_outputs_do_not_satisfy_return_verification(self) -> None:
        envelope, _, _ = self.envelope()
        receipt = self.write_return_receipt(envelope)
        sandbox = self.fixture / "sandbox-return"
        sandbox.mkdir()
        result = self.verify_return(envelope, receipt, observed_root=sandbox)
        self.assertEqual(result["status"], ReturnStatus.WRONG_BODY.value)

    def test_receipt_in_requested_body_can_satisfy_return_verification(self) -> None:
        envelope, _, _ = self.envelope()
        receipt = self.write_return_receipt(envelope)
        result = self.verify_return(envelope, receipt, observed_root=self.repository)
        self.assertEqual(result["status"], ReturnStatus.COMPLETE.value)

    def test_return_without_trusted_carrier_fails_closed(self) -> None:
        """A self-consistent box is not lawful custody."""

        envelope, _, _ = self.envelope()
        receipt = self.write_return_receipt(envelope)
        for label, kwargs in (
            ("both missing", {}),
            ("attachment missing", {"transport_plan": self.carrier_for(envelope)[1]}),
            ("transport missing", {"attachment": self.carrier_for(envelope)[0]}),
        ):
            with self.subTest(label=label):
                result = B_RETURN(
                    envelope, receipt, observed_root=self.repository, **kwargs
                )
                self.assertNotEqual(result["status"], ReturnStatus.COMPLETE.value)
                self.assertEqual(result["status"], ReturnStatus.UNKNOWN.value)
                self.assertTrue(
                    any("outer custody" in issue for issue in result["issues"])
                )

    def test_rebound_outer_custody_is_rejected_against_the_trusted_carrier(self) -> None:
        """The auditor's three exact custody counterexamples must now reject."""

        foreign_root = self.fixture / "foreign-return-root"
        foreign_root.mkdir()

        def rebind(label: str, mutate) -> dict:
            envelope, _, _ = self.envelope()
            forged = copy.deepcopy(envelope)
            mutate(forged)
            forged["envelope_id"] = compute_contract_id(forged, "envelope_id", "ENV")
            # The forged envelope is internally self-consistent by construction.
            validate_envelope(forged)
            receipt = self.write_return_receipt(forged, filename=f"forged-{label}.md")
            return B_RETURN(
                forged,
                receipt,
                observed_root=self.repository,
                attachment=self.carrier_for(envelope)[0],
                transport_plan=self.carrier_for(envelope)[1],
            )

        def substitute_return_root(forged: dict) -> None:
            forged["return_root"] = str(foreign_root)

        def substitute_contract_id(forged: dict) -> None:
            forged["attachment_contract_id"] = "ATT-" + "0" * 24

        def substitute_epoch(forged: dict) -> None:
            forged["attachment_epoch"] = "20990101T000000Z"
            forged["attachment_expires_at_utc"] = "2099-01-01T00:15:00Z"

        expectations = (
            ("return-root", substitute_return_root, "return root"),
            ("contract-id", substitute_contract_id, "attachment contract ID"),
            ("epoch", substitute_epoch, "attachment epoch"),
        )
        for label, mutate, expected_issue in expectations:
            with self.subTest(label=label):
                result = rebind(label, mutate)
                self.assertEqual(result["status"], ReturnStatus.MISMATCH.value)
                self.assertTrue(
                    any(expected_issue in issue for issue in result["issues"]),
                    result["issues"],
                )

    def test_matching_trusted_carrier_keeps_a_valid_return_valid(self) -> None:
        envelope, _, _ = self.envelope()
        receipt = self.write_return_receipt(envelope)
        attachment, plan = self.carrier_for(envelope)
        result = B_RETURN(
            envelope,
            receipt,
            observed_root=self.repository,
            attachment=attachment,
            transport_plan=plan,
        )
        self.assertEqual(result["status"], ReturnStatus.COMPLETE.value)
        self.assertEqual(result["issues"], [])

    def test_matching_false_and_true_finding_return_is_complete(self) -> None:
        for gap in (False, True):
            with self.subTest(gap=gap):
                finding = self._report_finding(gap, name=f"return-{str(gap).lower()}")
                envelope, _, _ = self.envelope(finding)
                receipt = self.write_return_receipt(
                    envelope,
                    audit_finding=finding,
                    filename=f"return-{str(gap).lower()}.md",
                )
                result = self.verify_return(envelope, receipt, observed_root=self.repository)
                self.assertEqual(result["status"], ReturnStatus.COMPLETE.value)
                self.assertEqual(result["evidence"]["audit_finding"], finding)

    def test_missing_or_mismatched_finding_return_fails_closed(self) -> None:
        finding = self._report_finding(False, name="mismatch-source")
        cases: dict[str, dict | None] = {"missing": None}
        for field, value in (
            ("load_bearing_gap", True),
            ("source_path", "reports/substituted.md"),
            ("source_byte_count", 129),
            ("source_sha256", "f" * 64),
        ):
            cases[field] = {**finding, field: value}
        for label, returned in cases.items():
            with self.subTest(label=label):
                envelope, _, _ = self.envelope(finding)
                receipt = self.write_return_receipt(
                    envelope,
                    audit_finding=returned,
                    filename=f"mismatch-{label}.md",
                )
                result = self.verify_return(envelope, receipt, observed_root=self.repository)
                self.assertEqual(result["status"], ReturnStatus.MISMATCH.value)
                self.assertTrue(any("audit_finding" in issue for issue in result["issues"]))

    def test_changed_missing_or_foreign_raw_source_never_returns_complete(self) -> None:
        """An exact old echo cannot certify raw evidence that has since moved."""

        # 1. the raw bytes change after the decision was created
        finding = self._report_finding(False, name="drift")
        envelope, _, _ = self.envelope(finding)
        receipt = self.write_return_receipt(
            envelope, audit_finding=finding, filename="drift.md"
        )
        source = self.repository / finding["source_path"]
        source.write_text("# Independent audit\n\nTampered.\n", encoding="utf-8")
        result = self.verify_return(envelope, receipt, observed_root=self.repository)
        self.assertNotEqual(result["status"], ReturnStatus.COMPLETE.value)
        self.assertEqual(result["status"], ReturnStatus.MISMATCH.value)
        self.assertTrue(
            any("source_sha256 no longer matches" in issue for issue in result["issues"]),
            result["issues"],
        )

        # 2. the raw source disappears
        missing = self._report_finding(False, name="vanished")
        envelope_m, _, _ = self.envelope(missing)
        receipt_m = self.write_return_receipt(
            envelope_m, audit_finding=missing, filename="vanished.md"
        )
        (self.repository / missing["source_path"]).unlink()
        result_m = self.verify_return(envelope_m, receipt_m, observed_root=self.repository)
        self.assertEqual(result_m["status"], ReturnStatus.MISMATCH.value)
        self.assertTrue(
            any("source is missing" in issue for issue in result_m["issues"]),
            result_m["issues"],
        )

        # 3. the source escapes the trusted root
        escaped = {**self._report_finding(False, name="escaped"), "source_path": "../outside.md"}
        envelope_e, _, _ = self.envelope(escaped)
        receipt_e = self.write_return_receipt(
            envelope_e, audit_finding=escaped, filename="escaped.md"
        )
        result_e = self.verify_return(envelope_e, receipt_e, observed_root=self.repository)
        self.assertEqual(result_e["status"], ReturnStatus.MISMATCH.value)
        self.assertTrue(
            any("not repository-relative" in issue for issue in result_e["issues"]),
            result_e["issues"],
        )

        # 4. the source is an instruction surface
        role_relative = ".github/agents/Whitestone Auditor.agent.md"
        role_path = self.repository / role_relative
        role_path.parent.mkdir(parents=True, exist_ok=True)
        raw = b'Synthetic role instructions only. This is not an audit finding.\n'
        role_path.write_bytes(raw)
        instruction = {
            **self._report_finding(False, name="instruction"),
            "source_path": role_relative,
            "source_byte_count": len(raw),
            "source_sha256": hashlib.sha256(raw).hexdigest(),
        }
        envelope_i, _, _ = self.envelope(instruction)
        receipt_i = self.write_return_receipt(
            envelope_i, audit_finding=instruction, filename="instruction.md"
        )
        result_i = self.verify_return(envelope_i, receipt_i, observed_root=self.repository)
        self.assertEqual(result_i["status"], ReturnStatus.MISMATCH.value)
        self.assertTrue(
            any(
                "instruction, template, or configuration" in issue
                for issue in result_i["issues"]
            ),
            result_i["issues"],
        )

    def test_unowned_or_authorizing_finding_return_is_rejected(self) -> None:
        plain_envelope, _, _ = self.envelope()
        unowned = self.write_return_receipt(
            plain_envelope,
            audit_finding=self._finding(False),
            filename="unowned-finding.md",
        )
        result = self.verify_return(plain_envelope, unowned, observed_root=self.repository)
        self.assertEqual(result["status"], ReturnStatus.MISMATCH.value)

        finding = self._report_finding(False, name="authorizing")
        envelope, _, _ = self.envelope(finding)
        authorizing = self.write_return_receipt(
            envelope,
            audit_finding={**finding, "authority": "BUILDER"},
            filename="authorizing-finding.md",
        )
        result = self.verify_return(envelope, authorizing, observed_root=self.repository)
        self.assertEqual(result["status"], ReturnStatus.MISMATCH.value)
        self.assertNotEqual(envelope["authority"], "BUILDER")

    def test_decision_id_mismatch_fails_reconciliation(self) -> None:
        envelope, _, _ = self.envelope()
        receipt = self.write_return_receipt(envelope, decision_id="wrong")
        result = self.verify_return(envelope, receipt, observed_root=self.repository)
        self.assertEqual(result["status"], ReturnStatus.MISMATCH.value)

    def test_legacy_phase1_engine_remains_callable(self) -> None:
        policy = json.loads((ORCHESTRATOR_ROOT / "config" / "orchestrator.config.json").read_text())
        result = run_legacy(self.repository, policy, "010", "Close Experiment 010", dry_test=True)
        self.assertEqual(result["DRY_TEST_RESULT"], "PASS")

    def test_kernel20_engine_remains_callable(self) -> None:
        decision = self.decision("Audit the bounded fixture")
        self.assertEqual(decision["engine"], "K20_ORCHESTRATION_PHENOTYPE")
        self.assertEqual(decision["decision_id"], compute_decision_id(decision))

    def test_bridge_preflight_writes_only_bridge_artifacts_under_output(self) -> None:
        before = {path.relative_to(self.repository) for path in self.repository.rglob("*") if path.is_file()}
        preflight = build_preflight(
            self.repository,
            self.policy,
            "010",
            "Close Experiment 010",
            execution_root=self.repository,
        )
        artifacts = emit_preflight_artifacts(self.repository, "010", preflight)
        after = {path.relative_to(self.repository) for path in self.repository.rglob("*") if path.is_file()}
        created = after - before
        self.assertEqual(len(created), 3)
        self.assertTrue(all(str(path).startswith("orchestrator/output/") for path in created))
        self.assertTrue(artifacts["envelope_path"].endswith("task-envelope.json"))

    def test_programmatic_preflight_preserves_optional_finding(self) -> None:
        for gap in (False, True):
            with self.subTest(gap=gap):
                finding = self._finding(gap)
                preflight = build_preflight(
                    self.repository,
                    self.policy,
                    "010",
                    "Implement the bounded fixture",
                    execution_root=self.repository,
                    audit_finding=finding,
                )
                self.assertEqual(preflight["decision"]["audit_finding"], finding)
                self.assertEqual(
                    preflight["envelope"]["kernel20_decision"]["audit_finding"],
                    finding,
                )

    def test_cli_preflight_loads_report_once_and_passes_validated_finding(self) -> None:
        report = self.repository / "auditor-report.md"
        raw = (
            "# Audit\n\n## AUDIT_FINDING\n```json\n"
            '{"schema_version":"1.0.0","producer_role":"AUDITOR",'
            '"verdict":"CONDITIONAL","load_bearing_gap":false}\n```\n'
        )
        report.write_text(raw, encoding="utf-8")
        captured: dict = {}

        def fake_preflight(repository_root, policy, experiment, goal, **kwargs):
            captured.update(kwargs)
            return {
                "attachment": {
                    "environment": "LOCAL_WORKSPACE",
                    "requested_root": str(repository_root),
                    "execution_root": str(repository_root),
                    "body_identity": "MATCH",
                    "attachment_state": "WRITE_READY",
                    "readable_roots": [str(repository_root)],
                    "writable_roots": [str(repository_root)],
                    "capabilities": {},
                },
                "transport_plan": {
                    "transport_selected": "DIRECT_FILESYSTEM",
                    "delivery_root": str(repository_root),
                    "return_root": str(repository_root),
                    "host_approval_required": True,
                    "status": "HOST_APPROVAL_REQUIRED",
                },
                "artifacts": {
                    "attachment_contract_path": "orchestrator/output/attachment.json",
                    "attachment_contract_sha256": "0" * 64,
                    "transport_plan_path": "orchestrator/output/transport.json",
                    "transport_plan_sha256": "0" * 64,
                    "envelope_path": "orchestrator/output/envelope.json",
                    "envelope_sha256": "0" * 64,
                },
            }

        policy = {**self.policy, "repositoryRoot": str(self.repository)}
        stdout = StringIO()
        with (
            mock.patch.object(bootstrap, "load_policy", return_value=policy),
            mock.patch.object(bootstrap, "run_preflight", side_effect=fake_preflight),
            mock.patch.object(
                bootstrap,
                "load_audit_finding",
                wraps=bootstrap.load_audit_finding,
            ) as loader,
            redirect_stdout(stdout),
        ):
            result = bootstrap.main(
                [
                    "--root", str(self.repository),
                    "--bridge-preflight",
                    "--experiment",
                    "010",
                    "--goal",
                    "Implement the bounded fixture",
                    "--audit-finding-report",
                    report.name,
                ]
            )
        self.assertEqual(result, 0)
        loader.assert_called_once_with(self.repository.resolve(), report.name)
        self.assertIs(captured["audit_finding"]["load_bearing_gap"], False)
        self.assertEqual(captured["audit_finding"]["source_byte_count"], len(raw.encode()))
        self.assertEqual(
            captured["audit_finding"]["source_sha256"],
            hashlib.sha256(raw.encode()).hexdigest(),
        )
        self.assertIn("STATUS: HOST_APPROVAL_REQUIRED", stdout.getvalue())

    def test_audit_finding_flag_remains_incompatible_with_terminal_modes(self) -> None:
        stderr = StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit):
            bootstrap.main(
                [
                    "--root", str(self.repository),
                    "--verify-return",
                    "--audit-finding-report",
                    "auditor-report.md",
                ]
            )
        self.assertIn("only valid for Kernel 20 planning or bridge preflight", stderr.getvalue())

    def test_fresh_process_reconstructs_false_true_and_mismatch_residue(self) -> None:
        """A fresh process reconstructs the lifecycle from persisted carriers alone.

        The worker receives no conversation and no audit conclusion. It gets the
        decision-bearing envelope, the receipt, and — because outer custody is no
        longer self-certifying — this lifecycle's persisted attachment contract
        and transport plan.
        """

        child = """
import json
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from bridge.return_verify import B_RETURN
from engine.kernel20_phenotype import reconcile_decision
repository = Path(sys.argv[2])
envelope = json.loads(Path(sys.argv[3]).read_text(encoding='utf-8'))
receipt = Path(sys.argv[4])
attachment = json.loads(Path(sys.argv[5]).read_text(encoding='utf-8'))
transport = json.loads(Path(sys.argv[6]).read_text(encoding='utf-8'))
returned = B_RETURN(
    envelope,
    receipt,
    observed_root=repository,
    attachment=attachment,
    transport_plan=transport,
)
uncarried = B_RETURN(envelope, receipt, observed_root=repository)
reconciled = reconcile_decision(
    envelope['kernel20_decision'],
    receipt.read_text(encoding='utf-8'),
    str(receipt.relative_to(repository)),
    repository_root=repository,
)
print(json.dumps({
    'decision_id': envelope['kernel20_decision_id'],
    'route': envelope['route'],
    'authority': envelope['authority'],
    'status': returned['status'],
    'uncarried_status': uncarried['status'],
    'finding': returned.get('evidence', {}).get('audit_finding'),
    'outcome': reconciled['outcome'],
    'mismatches': reconciled['mismatches'],
}, sort_keys=True))
"""

        def persist_and_run(label: str, envelope: dict, receipt_finding: dict) -> dict:
            output = self.repository / "orchestrator" / "output"
            envelope_path = output / f"{label}.json"
            envelope_path.write_text(
                json.dumps(envelope, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            attachment, plan = self.carrier_for(envelope)
            attachment_path = output / f"{label}-attachment.json"
            attachment_path.write_text(
                json.dumps(attachment, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            transport_path = output / f"{label}-transport.json"
            transport_path.write_text(
                json.dumps(plan, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            receipt = self.write_return_receipt(
                envelope,
                audit_finding=receipt_finding,
                filename=f"{label}.md",
            )
            environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
            completed = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    child,
                    str(ORCHESTRATOR_ROOT),
                    str(self.repository),
                    str(envelope_path),
                    str(receipt),
                    str(attachment_path),
                    str(transport_path),
                ],
                cwd=self.repository,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            return json.loads(completed.stdout)

        false_finding = self._report_finding(False, name="fresh-false")
        true_finding = self._report_finding(True, name="fresh-true")
        false_envelope, _, _ = self.envelope(false_finding)
        true_envelope, _, _ = self.envelope(true_finding)
        mismatch_envelope, _, _ = self.envelope(false_finding)
        false_result = persist_and_run("fresh-false", false_envelope, false_finding)
        true_result = persist_and_run("fresh-true", true_envelope, true_finding)
        mismatch_result = persist_and_run(
            "fresh-mismatch",
            mismatch_envelope,
            {**false_finding, "source_sha256": "b" * 64},
        )

        self.assertIs(false_result["finding"]["load_bearing_gap"], False)
        self.assertIs(true_result["finding"]["load_bearing_gap"], True)
        self.assertEqual(false_result["finding"]["source_path"], false_finding["source_path"])
        self.assertEqual(
            false_result["finding"]["source_byte_count"], false_finding["source_byte_count"]
        )
        self.assertEqual(
            false_result["finding"]["source_sha256"], false_finding["source_sha256"]
        )
        self.assertNotEqual(
            false_finding["source_sha256"], true_finding["source_sha256"]
        )
        self.assertNotEqual(false_result["decision_id"], true_result["decision_id"])
        self.assertEqual(false_result["route"], true_result["route"])
        self.assertEqual(false_result["authority"], "PROPOSAL_ONLY")
        self.assertEqual(false_result["status"], ReturnStatus.COMPLETE.value)
        self.assertEqual(true_result["status"], ReturnStatus.COMPLETE.value)
        self.assertEqual(false_result["outcome"], "CLOSE")
        self.assertEqual(true_result["outcome"], "CLOSE")
        self.assertEqual(mismatch_result["status"], ReturnStatus.MISMATCH.value)
        self.assertNotEqual(mismatch_result["outcome"], "CLOSE")
        self.assertTrue(
            any("audit_finding" in item for item in mismatch_result["mismatches"])
        )
        # The same fresh worker, denied the custody carrier, fails closed.
        for result in (false_result, true_result):
            self.assertEqual(result["uncarried_status"], ReturnStatus.UNKNOWN.value)

    def test_no_task_executes_automatically(self) -> None:
        sentinel = self.repository / "executed.txt"
        build_preflight(
            self.repository,
            self.policy,
            "010",
            f"Implement fixture and create {sentinel}",
            execution_root=self.repository,
        )
        self.assertFalse(sentinel.exists())

    def test_preflight_preserves_observed_inputs(self) -> None:
        protected = [self.repository / "CURRENT_STATE.md", self.repository / "experiments/010-fixture/notes.md"]
        before = {path: path.read_bytes() for path in protected}
        build_preflight(self.repository, self.policy, "010", "Audit fixture", execution_root=self.repository)
        self.assertEqual(before, {path: path.read_bytes() for path in protected})

    def test_detached_workspace_requires_host_approval(self) -> None:
        # Outside the temporary directory so classification remains SANDBOX.
        sandbox = Path.home() / "synthetic-lodestone-detached-workspace"
        attachment = B_ATTACH(self.repository, sandbox,
            readable_roots=[str(self.repository), str(sandbox)], writable_roots=[str(sandbox)],
            capabilities=self.capabilities())
        plan = B_TRANSPORT(attachment, self.decision())
        self.assertEqual(attachment["environment"], "SANDBOX")
        self.assertEqual(attachment["body_identity"], "MISMATCH")
        self.assertEqual(plan["status"], "HOST_APPROVAL_REQUIRED")

    def test_missing_host_approval_is_not_inferred(self) -> None:
        decision = self.decision()
        plan = B_TRANSPORT(self.attachment(), decision)
        self.assertTrue(plan["host_approval_required"])
        self.assertNotEqual(plan["transport_selected"], "DIRECT_FILESYSTEM")

    def test_transport_tie_uses_safe_precedence(self) -> None:
        tied = {
            "DIRECT_FILESYSTEM": 0.5,
            "PACKET_EXPORT": 0.5,
            "HOST_MANUAL": 0.5,
            "NONE": 0.5,
        }
        self.assertEqual(select_transport(tied), "NONE")

    def test_declared_output_hash_mismatch_requires_audit(self) -> None:
        envelope, _, _ = self.envelope()
        artifact = self.repository / "artifact.txt"
        artifact.write_text("actual\n", encoding="utf-8")
        receipt = self.write_return_receipt(envelope)
        with receipt.open("a", encoding="utf-8") as handle:
            handle.write("output_hashes:\n- artifact.txt = " + "0" * 64 + "\n")
        result = self.verify_return(envelope, receipt, observed_root=self.repository)
        self.assertEqual(result["status"], ReturnStatus.AUDIT_REQUIRED.value)

    def test_envelope_preserves_kernel20_semantics(self) -> None:
        envelope, _, decision = self.envelope()
        for field in ("route", "goal", "target_files", "protected_files", "predicted_outputs", "stop_condition"):
            self.assertEqual(envelope[field], decision[field])
        self.assertEqual(envelope["authority"], "PROPOSAL_ONLY")

    def test_lifecycle_witness_is_append_only_hash_bound_and_manifest_last(self) -> None:
        result = self.witness()
        directory = self.repository / result["directory"]
        manifest_path = self.repository / result["manifest_path"]
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(result["verification"]["status"], "VALID")
        self.assertEqual(manifest["canonical_body"]["path"], str(self.repository))
        self.assertEqual(manifest["selected_organ"], "BUILDER")
        self.assertRegex(manifest["host_approval_id"], r"^APR-[0-9a-f]{24}$")
        self.assertEqual(manifest["attachment_contract_id"], json.loads(
            (directory / "attachment-contract.json").read_text(encoding="utf-8")
        )["attachment_contract_id"])
        self.assertEqual(manifest["transport_plan_id"], json.loads(
            (directory / "transport-plan.json").read_text(encoding="utf-8")
        )["transport_plan_id"])
        self.assertEqual(manifest["envelope_id"], json.loads(
            (directory / "task-envelope.json").read_text(encoding="utf-8")
        )["envelope_id"])
        for artifact in manifest["artifacts"]:
            path = self.repository / artifact["path"]
            self.assertEqual(len(path.read_bytes()), artifact["byte_count"])
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), artifact["sha256"])
        constituent_mtimes = [
            (self.repository / artifact["path"]).stat().st_mtime_ns
            for artifact in manifest["artifacts"]
        ]
        self.assertGreater(manifest_path.stat().st_mtime_ns, max(constituent_mtimes))
        with self.assertRaises(FileExistsError):
            self.witness(
                observed_at=datetime.fromisoformat(
                    json.loads((directory / "attachment-contract.json").read_text())["observed_at_utc"]
                    .replace("Z", "+00:00")
                )
            )

    def test_missing_lifecycle_manifest_is_invalid(self) -> None:
        result = self.witness()
        manifest_path = self.repository / result["manifest_path"]
        manifest_path.unlink()
        with self.assertRaisesRegex(ValueError, "final manifest is missing"):
            verify_lifecycle_witness(manifest_path.parent, repository_root=self.repository)

    def test_stale_lifecycle_attachment_is_rejected_before_output(self) -> None:
        observed = datetime.now(timezone.utc) - timedelta(hours=1)
        with self.assertRaisesRegex(ValueError, "stale"):
            self.witness(observed_at=observed)
        self.assertFalse(any((self.repository / "orchestrator" / "output").iterdir()))

    def test_reused_lifecycle_evidence_is_rejected(self) -> None:
        result = self.witness()
        with self.assertRaisesRegex(ValueError, "already been used"):
            verify_lifecycle_witness(
                self.repository / result["directory"],
                repository_root=self.repository,
                used_lifecycle_ids={result["lifecycle_id"]},
            )

    def test_overwritten_lifecycle_artifact_is_rejected(self) -> None:
        result = self.witness()
        directory = self.repository / result["directory"]
        with (directory / "decision.json").open("ab") as handle:
            handle.write(b"\n")
        with self.assertRaisesRegex(ValueError, "overwritten"):
            verify_lifecycle_witness(directory, repository_root=self.repository)

    def test_lifecycle_witness_performs_no_automatic_task_execution(self) -> None:
        sentinel = self.repository / "executed-by-witness.txt"
        self.witness(goal=f"Create {sentinel}")
        self.assertFalse(sentinel.exists())

    def test_host_authorization_and_approval_are_exact_decision_bound(self) -> None:
        inputs = self.witness_inputs(goal="Reject caller-only authority")
        with self.assertRaises(TypeError):
            create_lifecycle_witness(
                self.repository,
                inputs["decision_path"],
                inputs["node_source_path"],
                host_authorization_id="CALLER-ASSERTED",
                host_approval_id="CALLER-ASSERTED",
                execution_root=self.repository,
            )

        def rebind_approval(path: Path, transform) -> None:
            approval = json.loads(path.read_text(encoding="utf-8"))
            transform(approval)
            approval["approval_id"] = ""
            payload = copy.deepcopy(approval)
            payload.pop("approval_id")
            approval["approval_id"] = "APR-" + hashlib.sha256(
                json.dumps(
                    payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
            ).hexdigest()[:24]
            path.write_text(
                json.dumps(approval, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )

        mutations = {
            "wrong-decision": lambda value: value["repair_decision"].update(
                {"sha256": "0" * 64}
            ),
            "wrong-body": lambda value: value["canonical_body"].update(
                {"inode": value["canonical_body"]["inode"] + 1}
            ),
            "wrong-target": lambda value: value.update(
                {"modify_targets": value["modify_targets"][:-1]}
            ),
            "wrong-organ": lambda value: value.update({"selected_organ": "AUDITOR"}),
        }
        for label, mutation in mutations.items():
            with self.subTest(label=label):
                case = self.witness_inputs(goal=f"Reject {label}")
                rebind_approval(case["approval_path"], mutation)
                with self.assertRaises(ValueError):
                    self.create_from_inputs(case)

        altered_access = self.witness_inputs(goal="Reject altered access point")
        access_path = altered_access["access_point_path"]
        access = json.loads(access_path.read_text(encoding="utf-8"))
        access["exact_modify_targets"] = access["exact_modify_targets"][:-1]
        access_path.write_text(json.dumps(access, sort_keys=True) + "\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.create_from_inputs(altered_access)

    def test_production_boundary_requires_exact_sealed_node_identity(self) -> None:
        inputs = self.witness_inputs(goal="Reject omitted node source")
        with self.assertRaises(TypeError):
            create_lifecycle_witness(
                self.repository,
                inputs["decision_path"],
                None,
                access_point_path=inputs["access_point_path"],
                host_approval_record_path=inputs["approval_path"],
                execution_root=self.repository,
            )

        node = self.repository / "node.md"
        original = node.read_bytes()
        node.write_bytes(original + b"altered")
        with self.assertRaisesRegex(ValueError, "node source bytes"):
            self.create_from_inputs(inputs)
        node.write_bytes(original)

        mismatch = self.witness_inputs(goal="Reject decision source mismatch")
        decision_path = mismatch["decision_path"]
        decision = json.loads(decision_path.read_text(encoding="utf-8"))
        decision["source_node_identity"]["sha256"] = "0" * 64
        decision["decision_id"] = compute_decision_id(decision)
        decision_path.write_text(
            json.dumps(decision, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        approval_path = mismatch["approval_path"]
        approval = json.loads(approval_path.read_text(encoding="utf-8"))
        approval["repair_decision"].update(
            {
                "byte_count": decision_path.stat().st_size,
                "sha256": hashlib.sha256(decision_path.read_bytes()).hexdigest(),
                "decision_id": decision["decision_id"],
            }
        )
        approval["approval_id"] = ""
        payload = copy.deepcopy(approval)
        payload.pop("approval_id")
        approval["approval_id"] = "APR-" + hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:24]
        approval_path.write_text(
            json.dumps(approval, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "node source bytes"):
            self.create_from_inputs(mismatch)

    def test_lifecycle_and_context_identity_mismatches_are_rejected(self) -> None:
        mutations = {
            "lifecycle": lambda value: value.update({"lifecycle_id": "LCW-" + "0" * 24}),
            "organ": lambda value: value.update({"selected_organ": "AUDITOR"}),
            "body": lambda value: value["canonical_body"].update(
                {"inode": value["canonical_body"]["inode"] + 1}
            ),
            "epoch": lambda value: value.update({"attachment_epoch": "19000101T000000Z"}),
        }
        for label, mutation in mutations.items():
            with self.subTest(label=label):
                result = self.witness(goal=f"Reject context {label}")
                directory = self.rewrite_witness_json(result, "lifecycle-context.json", mutation)
                with self.assertRaises(ValueError):
                    verify_lifecycle_witness(directory, repository_root=self.repository)

        result = self.witness(goal="Reject lifecycle directory mismatch")
        directory = self.repository / result["directory"]
        moved = directory.with_name(directory.name + "-wrong")
        directory.rename(moved)
        with self.assertRaises(ValueError):
            verify_lifecycle_witness(moved, repository_root=self.repository)

    def test_recovered_handoff_hash_bindings_cannot_be_recomputed_away(self) -> None:
        result = self.witness(goal="Reject substituted Bridge evidence")
        identities: dict[str, str] = {}

        def alter_attachment(value: dict) -> None:
            value["minimal_falsifier"] = "substituted"
            value["attachment_contract_id"] = compute_contract_id(
                value, "attachment_contract_id", "ATT"
            )
            identities["attachment"] = value["attachment_contract_id"]

        directory = self.rewrite_witness_json(
            result, "attachment-contract.json", alter_attachment
        )

        def alter_transport(value: dict) -> None:
            value["attachment_contract_id"] = identities["attachment"]
            value["transport_plan_id"] = compute_contract_id(value, "transport_plan_id", "TRN")
            identities["transport"] = value["transport_plan_id"]

        self.rewrite_witness_json(result, "transport-plan.json", alter_transport)

        def alter_handoff(value: dict) -> None:
            value["attachment_contract_id"] = identities["attachment"]
            value["transport_plan_id"] = identities["transport"]
            value["envelope_id"] = compute_contract_id(value, "envelope_id", "ENV")

        self.rewrite_witness_json(result, "task-envelope.json", alter_handoff)
        with self.assertRaisesRegex(ValueError, "original handoff hash"):
            verify_lifecycle_witness(directory, repository_root=self.repository)

    def test_governing_documents_are_exact_and_complete(self) -> None:
        cases = ("empty", "incomplete", "extra", "stale", "renamed")
        for label in cases:
            with self.subTest(label=label):
                result = self.witness(goal=f"Reject governing documents {label}")
                directory = self.repository / result["directory"]
                manifest_path = directory / "lifecycle-witness.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                governing = copy.deepcopy(manifest["governing_documents"])
                if label == "empty":
                    governing = {}
                elif label == "incomplete":
                    governing.pop("INTERACTION_PROTOCOL.md")
                elif label == "extra":
                    governing["EXTRA.md"] = copy.deepcopy(
                        governing["INTERACTION_PROTOCOL.md"]
                    )
                elif label == "stale":
                    governing["INTERACTION_PROTOCOL.md"]["sha256"] = "0" * 64
                else:
                    governing["RENAMED.md"] = governing.pop("INTERACTION_PROTOCOL.md")
                self.rewrite_witness_json(
                    result,
                    "lifecycle-context.json",
                    lambda value, binding=governing: value.update(
                        {"governing_documents": binding}
                    ),
                )
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest["governing_documents"] = governing
                manifest_path.write_text(
                    json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
                )
                newest = max(
                    (self.repository / artifact["path"]).stat().st_mtime_ns
                    for artifact in manifest["artifacts"]
                )
                os.utime(manifest_path, ns=(manifest_path.stat().st_atime_ns, newest + 1))
                with self.assertRaises(ValueError):
                    verify_lifecycle_witness(directory, repository_root=self.repository)

    def test_constituent_roles_paths_and_cardinality_are_exact_unique(self) -> None:
        def duplicate_role(items):
            items[1]["role"] = items[0]["role"]

        def duplicate_path(items):
            items[1]["path"] = items[0]["path"]

        def missing(items):
            items.pop()

        def extra(items):
            value = copy.deepcopy(items[-1])
            value["write_order"] = 7
            items.append(value)

        def bad_order(items):
            items[-1]["write_order"] = items[-2]["write_order"]

        for label, mutation in {
            "duplicate-role": duplicate_role,
            "duplicate-path": duplicate_path,
            "missing": missing,
            "extra": extra,
            "bad-order": bad_order,
        }.items():
            with self.subTest(label=label):
                result = self.witness(goal=f"Reject constituent {label}")
                directory = self.repository / result["directory"]
                manifest_path = directory / "lifecycle-witness.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                mutation(manifest["artifacts"])
                manifest_path.write_text(
                    json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
                )
                newest = max(path.stat().st_mtime_ns for path in directory.iterdir() if path != manifest_path)
                os.utime(manifest_path, ns=(manifest_path.stat().st_atime_ns, newest + 1))
                with self.assertRaises(ValueError):
                    verify_lifecycle_witness(directory, repository_root=self.repository)

    def test_manifest_timestamp_must_be_strictly_later_than_every_constituent(self) -> None:
        result = self.witness(goal="Reject equal manifest timestamp")
        directory = self.repository / result["directory"]
        manifest_path = directory / "lifecycle-witness.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        newest = max(
            (self.repository / artifact["path"]).stat().st_mtime_ns
            for artifact in manifest["artifacts"]
        )
        os.utime(manifest_path, ns=(manifest_path.stat().st_atime_ns, newest))
        self.assertEqual(manifest_path.stat().st_mtime_ns, newest)
        with self.assertRaisesRegex(ValueError, "manifest was not written last"):
            verify_lifecycle_witness(directory, repository_root=self.repository)


if __name__ == "__main__":
    unittest.main()
