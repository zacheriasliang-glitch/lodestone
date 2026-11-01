"""Exposed engineering fixtures, not learning or held-out utility evidence.

One real local worker completion makes a second approved fixture stale. No live
state, models or external assets are used.
"""
from contextlib import ExitStack, contextmanager
import hashlib
import json
from pathlib import Path
import shutil
import socket
import sqlite3
import tempfile
import unittest
from unittest import mock

from lodestone_nurarihyon import outcome_memory as memory
from lodestone_nurarihyon.common import BusyError, canonical, digest, file_lock
from lodestone_nurarihyon.controller import Controller
from lodestone_nurarihyon.inspection import ReadOnlyBoard
from lodestone_nurarihyon.plans import make_profile_plan
from lodestone_nurarihyon.policy import DEFAULT_POLICY
from lodestone_pando.store import Board


def source_bytes(controller, name="Later engineering task", shelves=None):
    current = ReadOnlyBoard(controller.active_directory() / "pando").settings()
    baseline = {"board_name": current["name"], "enabled_shelves": current["shelves"]}
    candidate = {**baseline, "board_name": name} if shelves is None else {
        **baseline, "enabled_shelves": shelves}
    return canonical({"schema": "nurarihyon-profile-source/v1", "baseline": baseline,
                      "candidate": candidate, "rationale": "Explicit synthetic engineering fixture."})


def census(root):
    return {p.relative_to(root).as_posix(): digest(p.read_bytes())
            for p in sorted(root.rglob("*")) if p.is_file()}


class OutcomeMemoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = tempfile.TemporaryDirectory(prefix="outcome-memory-history-")
        cls.addClassCleanup(cls.fixture.cleanup)
        cls.fixture_root = Path(cls.fixture.name).resolve()
        cls.fixture_home = cls.fixture_root / "state"
        controller = Controller.initialize(cls.fixture_home, {**DEFAULT_POLICY, "max_tasks": 4})
        old_path = cls.fixture_root / "old.json"
        old_path.write_bytes(source_bytes(controller, "Stale engineering target"))
        old = controller.submit(old_path, key="old", priority=1)
        controller.approve(old["id"], old["plan_sha"])
        winner_path = cls.fixture_root / "winner.json"
        winner_path.write_bytes(source_bytes(controller, "Completed engineering target"))
        winner = controller.submit(winner_path, key="winner", priority=9)
        controller.approve(winner["id"], winner["plan_sha"])
        completed = controller.run_once()
        if completed.get("state") != "COMPLETED":
            raise AssertionError("Real completion fixture failed: " + repr(completed))
        rejected = controller.run_once()
        if rejected.get("result", {}).get("outcome") != "STALE_BASELINE":
            raise AssertionError("Real stale rejection fixture failed: " + repr(rejected))
        controller.stop()
        cls.winner, cls.old = winner["id"], old["id"]

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="outcome-memory-case-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.home = self.base / "state"
        shutil.copytree(self.fixture_home, self.home)
        self.controller = Controller(self.home)
        self.memory = self.base / "memory"

    def plan(self, shelves=None):
        return make_profile_plan(source_bytes(self.controller, shelves=shelves),
                                 ReadOnlyBoard(self.controller.active_directory() / "pando").settings())

    @contextmanager
    def inert(self):
        with ExitStack() as stack:
            def isolated_home(instance):
                self.assertTrue(instance.home.is_relative_to(self.base))
            # These isolated bridge fixtures never read service registration.
            stack.enter_context(mock.patch("lodestone_nurarihyon.context.check_context_home", side_effect=isolated_home))
            for name in ("submit", "approve", "run_once", "serve", "stop", "clear_stop", "retry", "recover"):
                stack.enter_context(mock.patch.object(Controller, name, side_effect=AssertionError("Unexpected action: " + name)))
            stack.enter_context(mock.patch.object(Board, "__init__", side_effect=AssertionError("Mutable board")))
            stack.enter_context(mock.patch.object(socket, "socket", side_effect=AssertionError("Network")))
            stack.enter_context(mock.patch.object(socket, "create_connection", side_effect=AssertionError("Network")))
            stack.enter_context(mock.patch("subprocess.Popen", side_effect=AssertionError("Subprocess")))
            yield

    def retain(self, task=None, root=None):
        with self.controller.locked(), self.inert():
            return memory.retain_outcome(self.controller, task or self.winner, root or self.memory)

    def retrieve(self, plan=None, root=None):
        with self.controller.locked(), self.inert():
            return memory.retrieve_outcomes(self.controller, plan or self.plan(), root or self.memory)

    def records(self):
        return [json.loads(line) for line in (self.memory / "records.jsonl").read_text().splitlines()]

    def write_records(self, records):
        (self.memory / "records.jsonl").write_text(
            "".join(json.dumps(record, sort_keys=True) + "\n" for record in records))

    def mutate_events(self, statement, parameters=()):
        with sqlite3.connect(self.controller.database) as db:
            db.execute("DROP TRIGGER immutable_events_update")
            db.execute("DROP TRIGGER immutable_events_delete")
            db.execute(statement, parameters)

    def test_actual_completed_and_rejected_events_use_existing_memory_format(self):
        before = census(self.home)
        success = self.retain()
        rejected = self.retain(self.old)
        self.assertEqual(success["outcome"], "COMPLETED")
        self.assertEqual(rejected["outcome"], "STALE_BASELINE")
        self.assertEqual(success["memory_engine_sha256"], digest(memory.TOOL.read_bytes()))
        api, _ = memory._memory_api()
        self.assertEqual(api.verify(self.memory), [])
        records = api.load_records(self.memory)
        self.assertEqual(len(records), 2)
        facts = [json.loads(record["body"]) for record in records]
        self.assertEqual({fact["event_kind"] for fact in facts}, {"PUBLISHED", "STALE_BASELINE"})
        completed = next(fact for fact in facts if fact["outcome"] == "COMPLETED")
        self.assertEqual(completed["output_evidence"]["kind"], "published_generation")
        self.assertGreater(completed["observed_metrics"]["wall_seconds"], 0)
        for record in records:
            self.assertEqual(record["confidence"], 1.0)
            self.assertEqual(record["kind"], "nurarihyon_outcome")
        self.assertEqual(before, census(self.home))

    def test_repeat_retention_is_idempotent_without_rewriting_catalogue(self):
        first = self.retain(self.old)
        before = census(self.memory)
        second = self.retain(self.old)
        self.assertEqual(second["status"], "ALREADY_RETAINED")
        self.assertEqual(first["record_id"], second["record_id"])
        self.assertEqual(before, census(self.memory))

    def test_new_source_context_is_consumable_but_not_an_applicability_claim(self):
        self.retain()
        self.retain(self.old)
        before = census(self.home), census(self.memory)
        plan = self.plan()
        report = self.retrieve(plan)
        self.assertTrue(report["fresh_current_baseline"])
        self.assertEqual(report["matching_records"], 2)
        self.assertEqual(report["plan_sha256"], digest(canonical(plan)))
        for row in report["records"]:
            self.assertEqual(row["match"], "same_toolkit_and_operator")
            self.assertEqual(row["applicability"], "NOT_ESTABLISHED")
            self.assertFalse(row["historical_baseline_equals_current"])
            self.assertNotEqual(row["evidence"]["source_sha256"], plan["source_sha256"])
        self.assertEqual(report["authority"], "NONE")
        self.assertFalse(report["admission_checked"])
        self.assertFalse(report["learned_parameters"])
        self.assertEqual(report["excluded"], [])
        self.assertEqual(report["selection_sha256"], digest(canonical(report["records"])))
        self.assertTrue(all(row["code_relation"] == "SAME_CODE" for row in report["records"]))
        self.assertEqual(before, (census(self.home), census(self.memory)))

    def test_other_operator_and_empty_catalogue_have_no_matches(self):
        cold = self.retrieve()
        self.assertEqual(cold["records"], [])
        self.assertFalse(self.memory.exists())
        self.retain(self.old)
        report = self.retrieve(self.plan(shelves=["commons", "memory"]))
        self.assertEqual(report["records_checked"], 1)
        self.assertEqual(report["records"], [])
        self.assertEqual(report["selection"], "NONE")
        with file_lock(self.memory / "catalogue.lock"):
            with self.assertRaises(BusyError):
                self.retrieve()

    def test_self_consistent_memory_hashes_do_not_establish_source_truth(self):
        self.retain(self.old)
        record, = self.records()
        facts = json.loads(record["body"])
        facts["observed_metrics"]["work_started"] = True
        record["body"] = canonical(facts).decode()
        record["content_hash"] = hashlib.sha256(record["body"].encode()).hexdigest()
        record["id"] = hashlib.sha256((record["source"] + "\n" + record["title"] + "\n" + record["body"]).encode()).hexdigest()[:16]
        self.write_records([record])
        api, _ = memory._memory_api()
        self.assertEqual(api.verify(self.memory), [])
        report = self.retrieve(self.plan(shelves=["commons", "memory"]))
        self.assertEqual(report["records"], [])
        self.assertEqual(report["excluded"][0]["status"], "UNVERIFIED")
        self.assertIn("original task/event/output", report["excluded"][0]["reason"])

    def test_missing_original_terminal_event_is_unverified(self):
        receipt = self.retain(self.old)
        self.mutate_events("DELETE FROM events WHERE sequence=?", (receipt["event_sequence"],))
        report = self.retrieve()
        self.assertEqual(report["records"], [])
        self.assertIn("terminal event is missing", report["excluded"][0]["reason"])

    def test_changed_terminal_metrics_and_publication_bytes_are_rejected(self):
        self.retain(self.old)
        with sqlite3.connect(self.controller.database) as db:
            event = db.execute("SELECT sequence,payload FROM events WHERE kind='STALE_BASELINE'").fetchone()
        payload = json.loads(event[1])
        payload["metrics"]["work_started"] = True
        self.mutate_events("UPDATE events SET payload=? WHERE sequence=?", (canonical(payload).decode(), event[0]))
        report = self.retrieve()
        self.assertEqual(report["records"], [])
        self.assertIn("original task/event/output", report["excluded"][0]["reason"])
        # Separate disposable state: this does not repair or retry the failed case.
        other = self.base / "other-state"
        shutil.copytree(self.fixture_home, other)
        self.controller = Controller(other)
        self.memory = self.base / "other-memory"
        self.retain()
        report = self.controller.active_directory() / "worker-result.json"
        report.write_bytes(report.read_bytes() + b" ")
        with self.assertRaises(ValueError):
            self.retrieve()

    def test_bounds_precede_existing_loader_and_capacity_refuses(self):
        with mock.patch.object(memory, "MAX_SECONDS", -1):
            with self.assertRaisesRegex(ValueError, "deadline exceeded"):
                self.retain()
        self.assertFalse(self.memory.exists())
        self.retain(self.old)
        original = (self.memory / "records.jsonl").read_bytes()
        api, pin = memory._memory_api()
        for body in (b"x" * (memory.MAX_MEMORY_BYTES + 1), original * (memory.MAX_RECORDS + 1)):
            with self.subTest(bytes=len(body)):
                (self.memory / "records.jsonl").write_bytes(body)
                with mock.patch.object(memory, "_memory_api", return_value=(api, pin)), \
                     mock.patch.object(api, "load_records", side_effect=AssertionError("Unbounded load")):
                    with self.assertRaises(ValueError):
                        self.retrieve()

    def test_symlink_hardlink_and_overlap_roots_are_refused(self):
        for root in (self.home / "memory", self.base, memory.ROOT / "memory-output"):
            with self.subTest(root=str(root)), self.assertRaises(ValueError):
                self.retain(root=root)
        target = self.base / "real"
        target.mkdir()
        alias = self.base / "alias"
        alias.symlink_to(target, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.retain(root=alias / "memory")
        self.retain(self.old)
        records = self.memory / "records.jsonl"
        outside = self.base / "outside.jsonl"
        outside.write_bytes(records.read_bytes())
        records.unlink()
        records.symlink_to(outside)
        with self.assertRaises(ValueError):
            self.retrieve()
        records.unlink()
        records.hardlink_to(outside)
        with self.assertRaises(ValueError):
            self.retrieve()

    def test_relocated_state_reopens_same_evidence_and_nonterminal_is_refused(self):
        self.retain()
        self.retain(self.old)
        plan = self.plan()
        before = self.retrieve(plan)
        relocated = self.base / "relocated"
        shutil.copytree(self.home, relocated)
        self.controller = Controller(relocated)
        after = self.retrieve(plan)
        self.assertEqual(before, after)
        record, other = self.records()
        record["kind"] = "note"
        self.write_records([record, other])
        foreign = self.retrieve(plan)
        self.assertEqual(len(foreign["records"]), 1)
        self.assertEqual(foreign["excluded"][0]["status"], "UNVERIFIED")
        source = self.base / "pending.json"
        source.write_bytes(source_bytes(self.controller))
        pending = self.controller.submit(source, key="pending")
        with self.assertRaisesRegex(ValueError, "Only evidenced COMPLETED or REJECTED"):
            self.retain(pending["id"])


if __name__ == "__main__":
    unittest.main()
