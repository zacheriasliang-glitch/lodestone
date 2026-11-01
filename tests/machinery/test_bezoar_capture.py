"""Synthetic complete-capture engineering checks.

All fixtures are synthetic; no historical corpus or production state is read.
Adapter calls are not daemon execution evidence.
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
from lodestone_machinery import bezoar_capture as capture
from lodestone_machinery import bezoar_reconciliation as reconciliation
from lodestone_machinery.records import RecordStore, canonical
from lodestone_nurarihyon import complete_capture as adapter
from lodestone_nurarihyon.controller import Controller
from lodestone_nurarihyon.inspection import ReadOnlyBoard
from lodestone_nurarihyon.plans import make_plan, validate_plan
from lodestone_nurarihyon.policy import DEFAULT_POLICY, check_operation, operation_limits, validate_policy
from lodestone_pando.store import Board
from test_bezoar_reconciliation import fixture, file_record, registry_text, source_text, RECEIPT


class CompleteCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="complete-capture-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def prepare(self, value=None, name="capture"):
        value = fixture() if value is None else value
        root = self.root / (name + "-originals")
        root.mkdir()
        for name_in_root, item in value["files"].items():
            path = root / name_in_root
            path.parent.mkdir(exist_ok=True, parents=True)
            path.write_bytes(item["text"].encode("utf-8"))
        spec = {key: val for key, val in value.items() if key != "schema"}
        spec["files"] = {p: {"sha256": item["sha256"], "bytes": len(item["text"].encode("utf-8"))}
                         for p, item in value["files"].items()}
        destination = self.root / (name + ".json")
        receipt = capture.prepare(root, spec, destination)
        return destination, receipt

    def resolve(self, source):
        raw = source.read_bytes()
        return capture.resolve(raw, capture.ExistingStore(capture.sibling(source)),
                               expected_sha256=reconciliation.digest(raw))

    def policy(self):
        policy = copy.deepcopy(DEFAULT_POLICY)
        policy["allowed_toolkits"] += ["bezoar.document/v1", "bezoar.reconciliation/v1", adapter.TOOLKIT]
        policy["max_stage_bytes"] = capture.STAGE_LIMIT
        # Shape-only binding for admission tests; never loaded/executed as Conscience.
        base = self.root / "synthetic-conscience"
        pin = lambda path: {"path": str(path), "bytes": 1, "sha256": "0" *64}
        policy["conscience"] = {"schema": "nurarihyon-conscience-binding/v1", "actor": "operator",
            "workspace": str(base / "workspace"), "policy": pin(base / "policy.json"),
            "modules": {name: pin(base / name) for name in ("conscience.py", "conscience_targets.py")}}
        return policy

    def test_134_files_and_130_receipts_are_complete_without_v1_expansion(self):
        value = fixture()
        names = []
        files = {}
        for index in range(130):
            stamp = "20260101T" + f"{index // 60:02d}{index % 60:02d}00Z"
            rid = "RCP-" + stamp + "-" + f"{index:012x}"
            name = f"receipts/{stamp}__fixture__{rid}.md"
            doc = {"receipt_id": rid, "operation_name": "fixture", "mode": "BOGUS",
                   "receipt_directory_path_set_before_creation": list(names)}
            text = "# Synthetic\n" + "x" *24000 + "\n```json\n" + json.dumps(doc) + "\n```\n"
            files[name] = file_record(text)
            names.append(name)
        files["docs/bezoar.md"] = file_record("# Empty synthetic registry\n")
        for index in range(2):
            files[f"sources/source-{index}.txt"] = file_record("source" + str(index))
        original = {"schema_version": "0.2.0", "status": "BLOCKED", "fold": None,
                    "scope": {"sources": [{"path": names[-1], "sha256": files[names[-1]]["sha256"]}]}}
        files["results/origami.json"] = file_record(json.dumps(original))
        value.update(files=files, receipt_inventory=sorted(names), saved_lineage=None)
        source, receipt = self.prepare(value)
        self.assertEqual((receipt["files"], len(value["receipt_inventory"])), (134,130))
        self.assertGreater(receipt["raw_bytes"], 3386908)
        resolved = self.resolve(source)
        self.assertEqual(set(resolved["contents"]), set(files))
        self.assertLessEqual(len(resolved["records"]) +2, 200)
        self.assertTrue(all(len(r["links"]) <=32 for r in resolved["records"].values()))
        self.assertEqual((reconciliation.MAX_INPUT_BYTES, reconciliation.MAX_FILES,
                          reconciliation.MAX_RECEIPTS), (65536,64,32))
        with self.assertRaises(ValueError):
            reconciliation.capsule(canonical(value), reconciliation.digest(canonical(value)))

    def test_retained_reader_recomputes_and_reopens_without_original_root(self):
        source, _ = self.prepare()
        raw = source.read_bytes()
        store = RecordStore(self.root / "retained")
        capture.copy_records(self.resolve(source), store)
        handle = capture.publish(store, raw, expected_sha256=reconciliation.digest(raw), scratch=self.root / "work")
        shutil.rmtree(self.root / "capture-originals")  # Only this test's generated fixture.
        restored = self.root / "restored-records"
        shutil.copytree(store.workspace, restored)
        result = capture.read(capture.ExistingStore(restored), handle, scratch=self.root / "review")
        self.assertEqual(result["report"]["lineage"]["verdict"], "VIOLATION")
        self.assertEqual(result["report"]["origami_results"][0]["result"]["status"], "BLOCKED")
        self.assertFalse(result["report"]["learning"])
        self.assertEqual(result["authority"], "NONE")
        self.assertEqual(RecordStore(restored).verify_links([handle])["status"], "INTACT")

    def test_historical_inline_v1_reader_compatibility(self):
        # Fixed synthetic v1 records; independent of any Mac or live-state path.
        historical = Path(__file__).parent / "fixtures" / "bezoar_inline_v1"
        retained = capture.ExistingStore(historical)
        handle = "bezoar:93e0a43c0f54e6ef855f3ee4538a3512b4839de5c81efa1be10935d53426c7e9"
        expected = retained.read(handle)
        copied = RecordStore(self.root / "v1-history-copy")
        for identity in (expected["payload"]["source"], handle):
            record = retained.read(identity)
            self.assertEqual(copied.publish(record["owner"], record["kind"], record["payload"],
                                            links=record["links"]), identity)
        actual = reconciliation.read_reconciliation(copied, handle, scratch=self.root / "v1-history-review")
        self.assertEqual(actual["report"], expected["payload"]["report"])

    def test_missing_dependency_link_refuses(self):
        source, _ = self.prepare()
        value = json.loads(source.read_bytes())
        handle = value["files"][RECEIPT]["handle"]
        capture.ExistingStore(capture.sibling(source)).path(handle).unlink()
        with self.assertRaises((ValueError, OSError)):
            self.resolve(source)

    def test_tampered_dependency_bytes_refuse(self):
        source, _ = self.prepare()
        value = json.loads(source.read_bytes())
        path = capture.ExistingStore(capture.sibling(source)).path(value["files"][RECEIPT]["handle"])
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaisesRegex(ValueError, "identity"):
            self.resolve(source)

    def test_group_omission_refuses(self):
        source, _ = self.prepare()
        value = json.loads(source.read_bytes())
        value["files"].pop("results/origami.json")
        value["origami_results"] = []
        raw = canonical(value)
        with self.assertRaisesRegex(ValueError, "group closure differs"):
            capture.resolve(raw, capture.ExistingStore(capture.sibling(source)), expected_sha256=reconciliation.digest(raw))

    def test_per_file_and_total_capture_limits_refuse(self):
        source, _ = self.prepare()
        original = json.loads(source.read_bytes())
        variants = [copy.deepcopy(original), copy.deepcopy(original)]
        variants[0]["files"][RECEIPT]["bytes"] = capture.MAX_FILE_BYTES +1
        for index in range(17):
            variants[1]["files"][f"notes/padding-{index}.txt"] = {
                **original["files"][RECEIPT], "bytes": capture.MAX_FILE_BYTES}
        for value in variants:
            raw = canonical(value)
            with self.assertRaises(ValueError):
                capture.manifest(raw, reconciliation.digest(raw))

    def test_missing_predecessor_never_publishes_package(self):
        value = fixture()
        value["files"][RECEIPT] = file_record(source_text(["receipts/absent.md"]))
        result = json.loads(value["files"]["results/origami.json"]["text"])
        result["scope"]["sources"][0]["sha256"] = value["files"][RECEIPT]["sha256"]
        value["files"]["results/origami.json"] = file_record(json.dumps(result))
        with self.assertRaisesRegex(ValueError, "predecessor closure incomplete"):
            self.prepare(value)
        self.assertFalse((self.root / "capture.json").exists())
        self.assertFalse((self.root / "capture.capture").exists())

    def test_path_escape_and_ambient_root_fields_refuse(self):
        source, _ = self.prepare()
        original = json.loads(source.read_bytes())
        for index, value in enumerate((copy.deepcopy(original), copy.deepcopy(original))):
            if index ==0:
                value["files"]["../escaped.txt"] = value["files"].pop(RECEIPT)
            else:
                value["root"] = str(self.root / "capture-originals")
            raw = canonical(value)
            with self.assertRaises(ValueError):
                capture.manifest(raw, reconciliation.digest(raw))
        alias = self.root / "alias.capture"
        alias.symlink_to(capture.sibling(source), target_is_directory=True)
        with self.assertRaises(ValueError):
            capture.ExistingStore(alias)

    def test_default_refuses_new_toolkit_and_v1_keeps_8mib(self):
        source, _ = self.prepare()
        board = Board(self.root / "board")
        plan = make_plan(adapter.TOOLKIT, source.read_bytes(), board.settings())
        with self.assertRaisesRegex(ValueError, "not authorized"):
            check_operation(plan, DEFAULT_POLICY)
        policy = self.policy()
        validate_policy(policy)
        check_operation(plan, policy)
        self.assertEqual(operation_limits(policy, "bezoar.reconciliation/v1")["max_stage_bytes"], 8388608)
        self.assertEqual(operation_limits(policy, adapter.TOOLKIT)["max_stage_bytes"], 16777216)
        self.assertEqual(policy["worker_rss_bytes"], DEFAULT_POLICY["worker_rss_bytes"])
        self.assertEqual(policy["max_state_bytes"], DEFAULT_POLICY["max_state_bytes"])

    def test_submit_captures_exact_sidecar_and_is_idempotent(self):
        source, _ = self.prepare()
        controller = Controller.initialize(self.root / "controller", self.policy())
        first = controller.submit(source, key="capture-01", toolkit=adapter.TOOLKIT)
        second = controller.submit(source, key="capture-01", toolkit=adapter.TOOLKIT)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["state"], "PLANNED")
        captured = controller.home / "inputs" / (reconciliation.digest(source.read_bytes()) + ".json")
        self.assertEqual(self.resolve(captured), self.resolve(source))
        self.assertEqual(controller.verify()["tasks"], 1)

    def test_invalid_sidecar_creates_no_task_or_admitted_inputs(self):
        source, _ = self.prepare()
        controller = Controller.initialize(self.root / "controller", self.policy())
        value = json.loads(source.read_bytes())
        capture.ExistingStore(capture.sibling(source)).path(value["groups"][0]).unlink()
        with self.assertRaises((ValueError, OSError)):
            controller.submit(source, key="bad-capture", toolkit=adapter.TOOLKIT)
        self.assertEqual(controller.verify()["tasks"], 0)
        self.assertEqual(list((controller.home / "inputs").iterdir()), [])

    def test_stage_reservation_refuses_before_input_publication(self):
        source, _ = self.prepare()
        policy = self.policy()
        policy["max_stage_bytes"] = 131072
        controller = Controller.initialize(self.root / "controller", policy)
        with self.assertRaisesRegex(ValueError, "cannot fit serialized stage reservation"):
            controller.submit(source, key="no-room", toolkit=adapter.TOOLKIT)
        self.assertEqual(controller.verify()["tasks"], 0)
        self.assertEqual(list((controller.home / "inputs").iterdir()), [])

    def test_entry_reservation_refuses_before_input_publication(self):
        source, _ = self.prepare()
        policy = self.policy()
        policy["max_files"] = 128
        controller = Controller.initialize(self.root / "controller", policy)
        with self.assertRaisesRegex(ValueError, "cannot fit filesystem-entry reservation"):
            controller.submit(source, key="no-entries", toolkit=adapter.TOOLKIT)
        self.assertEqual(controller.verify()["tasks"], 0)
        self.assertEqual(list((controller.home / "inputs").iterdir()), [])

    def test_adapter_build_and_independent_acceptance_preserve_board(self):
        source, _ = self.prepare()
        raw = source.read_bytes()
        parent = Board(self.root / "parent")
        before = parent.state()
        stage = self.root / "stage"
        stage.mkdir()
        shutil.copytree(parent.home, stage / "pando")
        board = Board(stage / "pando")
        plan = make_plan(adapter.TOOLKIT, raw, parent.settings())
        task = {"id": "capture-adapter-01", "plan": plan, "source_sha256": reconciliation.digest(raw)}
        report = adapter.build(board, raw, task, bundle=capture.sibling(source), scratch=stage / "reconciliation-work")
        adapter.validate_stage(ReadOnlyBoard(board.home), ReadOnlyBoard(parent.home), raw, task,
                               plan, report, self.policy(), stage=stage, bundle=capture.sibling(source))
        self.assertEqual(board.settings(), parent.settings())
        self.assertEqual(board.state()["posts"], before["posts"])
        self.assertEqual(parent.state(), before)
        self.assertEqual(board.verify()["status"], "INTACT")
        self.assertEqual(len(board.state()["artifacts"]) - len(before["artifacts"]), 2)
        from lodestone_nurarihyon.reconciliation import route_selection
        existing_route = route_selection(board, canonical(fixture()))
        self.assertIsNone(existing_route["previous_artifact"])

    def test_changed_registry_is_recomputed_without_repair_or_inherited_approval(self):
        value = fixture()
        value["files"]["docs/bezoar.md"] = file_record(registry_text("CLEARED"))
        source, _ = self.prepare(value)
        raw = source.read_bytes()
        resolved = self.resolve(source)
        report = capture.evaluate_resolved(resolved, expected_sha256=reconciliation.digest(raw), scratch=self.root / "changed")
        self.assertEqual(report["registry_verdict"], "ATTENTION")
        self.assertFalse(report["repair_performed"])
        board = Board(self.root / "board")
        plan = make_plan(adapter.TOOLKIT, raw, board.settings())
        plan["repair"] = "APPROVED"
        with self.assertRaises(ValueError):
            validate_plan(plan, raw, board.settings())


if __name__ == "__main__":
    unittest.main()
