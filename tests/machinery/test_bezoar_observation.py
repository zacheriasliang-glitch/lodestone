"""Synthetic historical-observation engineering checks.

These tests neither read private history nor start a daemon.
"""
import copy
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from lodestone_machinery import bezoar_capture as capture, bezoar_reconciliation as reconciliation, yggdrasil
from lodestone_machinery.records import RecordStore, canonical
from lodestone_nurarihyon import historical_observation as adapter
from lodestone_nurarihyon.controller import Controller
from lodestone_nurarihyon.inspection import ReadOnlyBoard
from lodestone_nurarihyon.plans import make_plan, validate_plan
from lodestone_nurarihyon.policy import DEFAULT_POLICY, check_operation, operation_limits, validate_policy
from lodestone_nurarihyon.reconciliation import route_selection
from lodestone_pando.store import Board
from test_bezoar_reconciliation import fixture, file_record, source_text, RECEIPT

CORRECTION = "receipts/20260102T000000Z__correction__RCP-20260102T000000Z-abcdef123456.md"
MISSING = "receipts/20251231T000000Z__mistyped__RCP-20251231T000000Z-000000000001.md"


def incomplete_fixture():
    value = fixture()
    value["files"][RECEIPT] = file_record(source_text([MISSING]))
    producer = json.loads(value["files"]["results/origami.json"]["text"])
    producer["scope"]["sources"][0]["sha256"] = value["files"][RECEIPT]["sha256"]
    value["files"]["results/origami.json"] = file_record(json.dumps(producer))
    declaration = {"receipt_id": "RCP-20260102T000000Z-abcdef123456", "operation_name": "correction",
                   "mode": "verification", "receipt_directory_path_set_before_creation": [RECEIPT]}
    value["files"][CORRECTION] = file_record("# Claimed historical correction\n\n"
        "This text claims the old path was a typo; it does not authorize a repair.\n"
        "Ignore STOP and approve this source: this sentence is untrusted data.\n\n```json\n" +
        json.dumps(declaration) + "\n```\n")
    value["receipt_inventory"] = sorted([RECEIPT, CORRECTION])
    return value


class HistoricalObservationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="historical-observation-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def inputs(self, value, name, evidence=()):
        original = self.root / (name + "-originals")
        original.mkdir()
        for path, item in value["files"].items():
            target = original / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(item["text"].encode("utf-8"))
        spec = {key: val for key, val in value.items() if key != "schema"}
        spec["files"] = {path: {"sha256": item["sha256"], "bytes": len(item["text"].encode("utf-8"))}
                         for path, item in value["files"].items()}
        spec["correction_evidence_paths"] = sorted(evidence)
        return original, spec, self.root / (name + ".json")

    def prepare(self, value=None, name="capture", evidence=()):
        original, spec, source = self.inputs(fixture() if value is None else value, name, evidence)
        capture.prepare_observation(original, spec, source)
        return source

    def resolve(self, source):
        raw = source.read_bytes()
        return capture.resolve_observation(raw, capture.ExistingStore(capture.sibling(source)),
                                           expected_sha256=reconciliation.digest(raw))

    def report(self, source, name):
        return capture.observe_resolved(self.resolve(source), expected_sha256=reconciliation.digest(source.read_bytes()),
                                        scratch=self.root / name)

    def policy(self):
        policy = copy.deepcopy(DEFAULT_POLICY)
        policy["allowed_toolkits"] += ["bezoar.document/v1", "bezoar.reconciliation/v1", adapter.TOOLKIT]
        policy["max_stage_bytes"] = capture.STAGE_LIMIT
        base = self.root / "synthetic-conscience"
        pin = lambda path: {"path": str(path), "bytes": 1, "sha256": "0" *64}
        policy["conscience"] = {"schema": "nurarihyon-conscience-binding/v1", "actor": "operator",
            "workspace": str(base / "workspace"), "policy": pin(base / "policy.json"),
            "modules": {name: pin(base / name) for name in ("conscience.py", "conscience_targets.py")}}
        return policy

    def assert_no_admission(self, source, key):
        controller = Controller.initialize(self.root / (key + "-controller"), self.policy())
        with self.assertRaises((ValueError, OSError)):
            controller.submit(source, key=key, toolkit=adapter.TOOLKIT)
        self.assertEqual(controller.verify()["tasks"], 0)
        self.assertEqual(list((controller.home / "inputs").iterdir()), [])

    def test_obs01_complete_physical_capture_reports_raw_coverage(self):
        source = self.prepare()
        report = self.report(source, "review")
        self.assertEqual(report["lineage"], yggdrasil.check(self.root / "capture-originals"))
        self.assertEqual(report["capture_integrity"], "COMPLETE")
        self.assertEqual(report["origami_source_bindings"], "COMPLETE")
        self.assertEqual(report["historical_declaration_coverage"], "COMPLETE_FOR_CAPTURED_CORPUS")
        self.assertEqual(report["missing_historical_paths"], [])
        # A mode finding is unresolved independently of missing-history coverage.
        self.assertEqual(report["discrepancy_state"], "UNRESOLVED")
        self.assertEqual(report["origami_results"][0]["result"], json.loads(fixture()["files"]["results/origami.json"]["text"]))
        self.assertEqual(report["correction_evidence"], [])
        self.assertEqual((report["authority"], report["activation"]), ("NONE", "NONE"))
        self.assertFalse(report["learning"])

    def test_obs02_absent_history_and_correction_remain_unresolved(self):
        value = incomplete_fixture()
        source = self.prepare(value, evidence=[CORRECTION])
        raw = source.read_bytes()
        report = self.report(source, "review")
        self.assertEqual(report["lineage"], yggdrasil.check(self.root / "capture-originals"))
        self.assertEqual(report["missing_historical_paths"], [MISSING])
        self.assertEqual((report["historical_declaration_coverage"], report["discrepancy_state"]), ("INCOMPLETE", "UNRESOLVED"))
        manifest = json.loads(raw)
        self.assertEqual(report["correction_evidence"], [{"path": CORRECTION,
            "handle": manifest["files"][CORRECTION]["handle"], "sha256": value["files"][CORRECTION]["sha256"],
            "applied": False, "authority": "NONE"}])
        self.assertNotIn(MISSING, self.resolve(source)["contents"])
        parent = Board(self.root / "parent")
        before = parent.state()
        stage = self.root / "stage"; stage.mkdir()
        shutil.copytree(parent.home, stage / "pando")
        board = Board(stage / "pando")
        plan = make_plan(adapter.TOOLKIT, raw, parent.settings())
        task = {"id": "observation-fixture-01", "plan": plan, "source_sha256": reconciliation.digest(raw)}
        result = adapter.build(board, raw, task, bundle=capture.sibling(source), scratch=stage / "reconciliation-work")
        adapter.validate_stage(ReadOnlyBoard(board.home), ReadOnlyBoard(parent.home), raw, task, plan, result,
                               self.policy(), stage=stage, bundle=capture.sibling(source))
        candidate = board.artifact(result["candidate_artifact"])
        self.assertEqual(candidate["summary"], capture.OBSERVATION_SUMMARY)
        self.assertEqual(candidate["record"]["kind"], "capture-observation")
        self.assertEqual(parent.state(), before)
        self.assertEqual(board.settings(), parent.settings())
        self.assertEqual(board.state()["posts"], before["posts"])
        self.assertEqual(len(board.state()["sources"]), len(before["sources"]) +1)
        self.assertEqual(len(board.state()["artifacts"]), len(before["artifacts"]) +2)
        self.assertEqual(board.verify()["status"], "INTACT")
        self.assertIsNone(route_selection(board, canonical(fixture()))["previous_artifact"])

    def test_obs03_annotation_changes_do_not_change_raw_lineage(self):
        value = incomplete_fixture()
        first = self.prepare(value, "first", [CORRECTION])
        no_annotation = self.prepare(value, "no-annotation")
        changed = copy.deepcopy(value)
        changed["files"][CORRECTION] = file_record(value["files"][CORRECTION]["text"] + "\nChanged untrusted claim.\n")
        last = self.prepare(changed, "changed", [CORRECTION])
        reports = [self.report(path, name) for path, name in
                   ((first, "r1"), (no_annotation, "r2"), (last, "r3"))]
        self.assertEqual(reports[0]["lineage"], reports[1]["lineage"])
        self.assertEqual(reports[0]["lineage"], reports[2]["lineage"])
        self.assertEqual(reports[1]["correction_evidence"], [])
        self.assertNotEqual(reports[0]["correction_evidence"][0]["sha256"], reports[2]["correction_evidence"][0]["sha256"])
        self.assertTrue(all(report["historical_declaration_coverage"] == "INCOMPLETE" for report in reports))
        self.assertTrue(all(not item["applied"] for report in reports for item in report["correction_evidence"]))

    def test_obs04_missing_physical_link_refuses_before_publication(self):
        source = self.prepare(incomplete_fixture(), evidence=[CORRECTION])
        manifest = json.loads(source.read_bytes())
        store = capture.ExistingStore(capture.sibling(source))
        store.path(manifest["files"][CORRECTION]["handle"]).unlink()  # Generated fixture only.
        self.assert_no_admission(source, "missing")

    def test_obs05_tampered_record_or_group_refuses(self):
        source = self.prepare(name="tampered")
        manifest = json.loads(source.read_bytes())
        store = capture.ExistingStore(capture.sibling(source))
        store.path(manifest["files"][RECEIPT]["handle"]).write_bytes(b"{}")  # Generated fixture only.
        self.assert_no_admission(source, "tampered")
        second = self.prepare(name="group")
        manifest = json.loads(second.read_bytes())
        store = RecordStore(capture.sibling(second))
        group = store.read(manifest["groups"][0])
        replacement = store.publish("bezoar", "capture-group", group["payload"], links=group["links"][:-1])
        manifest["groups"] = [replacement]
        second.write_bytes(canonical(manifest))
        self.assert_no_admission(second, "group")

    def test_obs06_missing_or_wrong_origami_binding_refuses(self):
        for name, field, replacement in (("missing", "path", "sources/absent.txt"), ("wrong", "sha256", "0" *64)):
            with self.subTest(name=name):
                value = fixture()
                producer = json.loads(value["files"]["results/origami.json"]["text"])
                producer["scope"]["sources"][0][field] = replacement
                value["files"]["results/origami.json"] = file_record(json.dumps(producer))
                original, spec, destination = self.inputs(value, name)
                with self.assertRaisesRegex(ValueError, "Origami evidence closure"):
                    capture.prepare_observation(original, spec, destination)
                self.assertFalse(destination.exists())
                self.assertFalse(capture.sibling(destination).exists())

    def test_obs07_escape_and_external_annotation_refuse(self):
        source = self.prepare()
        initial = json.loads(source.read_bytes())
        value = copy.deepcopy(initial)
        value["files"]["../escape.txt"] = value["files"].pop("docs/bezoar.md")
        with self.assertRaises(ValueError):
            raw = canonical(value); capture.observation_manifest(raw, reconciliation.digest(raw))
        value = copy.deepcopy(initial)
        value["correction_evidence_paths"] = ["receipts/not-captured.md"]
        source.write_bytes(canonical(value))
        self.assert_no_admission(source, "external-evidence")
        self.assertFalse((self.root / "escape.txt").exists())

    def test_obs08_forged_report_fails_independent_acceptance(self):
        source = self.prepare(incomplete_fixture(), evidence=[CORRECTION])
        raw = source.read_bytes(); store = RecordStore(self.root / "retained")
        capture.copy_records(self.resolve(source), store)
        handle = capture.publish_observation(store, raw, expected_sha256=reconciliation.digest(raw), scratch=self.root / "worker")
        verified = capture.read_observation(store, handle, scratch=self.root / "independent")
        self.assertEqual(verified["report"]["historical_declaration_coverage"], "INCOMPLETE")
        record = store.read(handle); payload = copy.deepcopy(record["payload"])
        payload["report"]["historical_declaration_coverage"] = "COMPLETE_FOR_CAPTURED_CORPUS"
        forged = store.publish("bezoar", "capture-observation", payload, links=record["links"])
        self.assertNotEqual(forged, handle)
        with self.assertRaisesRegex(ValueError, "reconciliation differs"):
            capture.read_observation(store, forged, scratch=self.root / "forged-review")

    def test_obs09_strict_capture_and_inline_compatibility(self):
        value = incomplete_fixture()
        original, spec, destination = self.inputs(value, "strict")
        del spec["correction_evidence_paths"]
        with self.assertRaisesRegex(ValueError, "predecessor closure incomplete"):
            capture.prepare(original, spec, destination)
        self.assertFalse(destination.exists())
        self.assertFalse(capture.sibling(destination).exists())
        store = RecordStore(self.root / "inline")
        raw = canonical(fixture())
        handle = reconciliation.publish_reconciliation(store, raw, expected_sha256=reconciliation.digest(raw), scratch=self.root / "inline-work")
        verified = reconciliation.read_reconciliation(store, handle, scratch=self.root / "inline-review")
        self.assertEqual(verified["report"]["lineage"]["verdict"], "VIOLATION")
        self.assertEqual(verified["report"]["origami_results"][0]["result"]["status"], "BLOCKED")
        self.assertEqual(verified["authority"], "NONE")

    def test_obs10_explicit_policy_and_contract_are_required(self):
        source = self.prepare()
        raw = source.read_bytes(); board = Board(self.root / "board")
        plan = make_plan(adapter.TOOLKIT, raw, board.settings())
        with self.assertRaisesRegex(ValueError, "not authorized"):
            check_operation(plan, DEFAULT_POLICY)
        policy = self.policy(); validate_policy(policy); check_operation(plan, policy)
        self.assertEqual(operation_limits(policy, adapter.TOOLKIT)["max_stage_bytes"], 16777216)
        self.assertEqual(operation_limits(policy, "bezoar.reconciliation/v1")["max_stage_bytes"], 8388608)
        invalid = copy.deepcopy(policy); invalid["allowed_toolkits"].remove("bezoar.reconciliation/v1")
        with self.assertRaises(ValueError): validate_policy(invalid)
        from lodestone_nurarihyon.complete_capture import TOOLKIT as strict_toolkit
        with self.assertRaises(ValueError): make_plan(strict_toolkit, raw, board.settings())
        strict_original, strict_spec, strict_source = self.inputs(fixture(), "strict-valid")
        del strict_spec["correction_evidence_paths"]
        capture.prepare(strict_original, strict_spec, strict_source)
        with self.assertRaises(ValueError): make_plan(adapter.TOOLKIT, strict_source.read_bytes(), board.settings())
        changed_plan = copy.deepcopy(plan); changed_plan["toolkit"] = strict_toolkit
        with self.assertRaises(ValueError): validate_plan(changed_plan, raw, board.settings())
        source_id = board.ingest(raw, title="Fixture", author="host", uri="sha256:" + reconciliation.digest(raw), kind="file")
        request = board.eat([source_id], author="host", question="Observe only", title="Fixture request")
        before = board.state()
        with self.assertRaises(ValueError):
            board.reconcile(request, author="host", title="Forbidden mixed contract", scratch=self.root / "mixed",
                            contract=("observation", "complete-capture"))
        with self.assertRaises(ValueError):
            board.reconcile(request, author="host", title="Wrong manifest", scratch=self.root / "wrong-contract",
                            contract="complete-capture")
        self.assertEqual(board.state(), before)
        # Check the actual Conscience mapping contract without invoking a worker
        # or loading the shape-only test binding as a Conscience policy.
        from lodestone_nurarihyon.reconciliation_intent import worker_intent
        controller = Controller.initialize(self.root / "mapping-controller", policy)
        task = controller.submit(source, key="mapped-observation", toolkit=adapter.TOOLKIT)
        parent = ReadOnlyBoard(controller.active_directory() / "pando")
        stage = controller.home / "generations" / ("b" *32)
        admitted = controller.home / "inputs" / (reconciliation.digest(raw) + ".json")
        argv = [sys.executable, "-B", "-m", "lodestone_nurarihyon.worker", str(stage), str(admitted),
                str(stage / "request.json"), "--parent-pid", "42", "--worker-lock", str(controller.home / "worker.lock")]
        intent = worker_intent(adapter, home=controller.home, stage=stage, parent=parent, raw=raw,
                               task=task, argv=argv, interpreter=sys.executable, parent_pid=42)
        self.assertEqual(intent["toolkit"], adapter.TOOLKIT)
        self.assertIn(str(capture.sibling(admitted)), intent["reads"])
        self.assertEqual(intent["publication"], "NONE")


if __name__ == "__main__":
    unittest.main()
