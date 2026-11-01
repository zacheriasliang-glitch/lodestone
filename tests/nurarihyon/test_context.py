"""Exposed deterministic integration checks; no learning/transfer claim."""
import json
from pathlib import Path
import shutil
import sqlite3
import socket
import sys
import tempfile
import unittest

from lodestone_nurarihyon.common import canonical, digest
from lodestone_nurarihyon.context import prepare_current
from lodestone_nurarihyon.controller import Controller
from lodestone_nurarihyon.inspection import ReadOnlyBoard
from lodestone_nurarihyon.outcome_memory import retain_outcome


def _offline(event, args):
    if event.startswith("socket."):
        raise PermissionError("Operating-cycle evaluation has external networking disabled")


sys.addaudithook(_offline)


def source(controller, path, *, name=None, shelves=None):
    settings = ReadOnlyBoard(controller.active_directory() / "pando").settings()
    baseline = {"board_name": settings["name"], "enabled_shelves": settings["shelves"]}
    candidate = dict(baseline)
    if name is not None:
        candidate["board_name"] = name
    if shelves is not None:
        candidate["enabled_shelves"] = shelves
    path.write_bytes(canonical({"schema": "nurarihyon-profile-source/v1", "baseline": baseline,
                                "candidate": candidate, "rationale": "Isolated operating-cycle integration fixture."}))
    return path


def census(root):
    return {str(f.relative_to(root)): digest(f.read_bytes()) for f in root.rglob("*") if f.is_file()}


class ContextIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = tempfile.TemporaryDirectory(prefix="context-fixture-")
        cls.addClassCleanup(cls.fixture.cleanup)
        cls.root = Path(cls.fixture.name).resolve()
        c = Controller.initialize(cls.root / "state")
        old = c.submit(source(c, cls.root / "old.json", name="Old requested label"), key="old", priority=1)
        c.approve(old["id"], old["plan_sha"])
        winner = c.submit(source(c, cls.root / "winner.json", name="Current completed label"), key="winner", priority=8)
        c.approve(winner["id"], winner["plan_sha"])
        if c.run_once().get("state") != "COMPLETED":
            raise AssertionError("The existing profile worker failed")
        if c.run_once().get("result", {}).get("outcome") != "STALE_BASELINE":
            raise AssertionError("The explicit competing old baseline did not become stale")
        cls.winner, cls.old = winner["id"], old["id"]

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="context-case-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        shutil.copytree(self.root / "state", self.base / "state")
        self.c = Controller(self.base / "state")
        self.memory = self.base / "memory"
        self.source = source(self.c, self.base / "later.json", name="Fresh compatible task")

    def remember(self):
        with self.c.locked():
            retain_outcome(self.c, self.winner, self.memory)
            retain_outcome(self.c, self.old, self.memory)

    def test_network_is_blocked_in_evaluation_process(self):
        with self.assertRaisesRegex(PermissionError, "networking disabled"):
            socket.socket()

    def test_ia1_plain_submit_keeps_existing_plan_and_events(self):
        context = prepare_current(self.c, self.source)
        task = self.c.submit(self.source, key="plain")
        self.assertEqual(task["plan_sha"], context["plan_sha256"])
        self.assertEqual([e["kind"] for e in task["events"]], ["PLANNED"])
        self.assertEqual(task["state"], "PLANNED")
        self.assertFalse((self.c.home / "contexts").exists())

    def test_ia2_ia3_source_bound_context_adds_no_authority(self):
        self.remember()
        before = census(self.c.home)
        context = prepare_current(self.c, self.source, memory_root=self.memory)
        self.assertEqual(census(self.c.home), before)
        self.assertEqual(context["outcome_memory"]["matching_records"], 2)
        task = self.c.submit(self.source, key="with-context", priority=3, memory_root=self.memory)
        self.assertEqual(task["plan"], context["plan"])
        self.assertEqual(task["priority"], 3)
        self.assertIsNone(task["approval"])
        self.assertEqual([e["kind"] for e in task["events"]], ["PLANNED", "CONTEXT_ATTACHED"])
        event = json.loads(task["events"][-1]["payload"])
        raw = (self.c.home / "contexts" / (event["context_sha256"] + ".json")).read_bytes()
        self.assertEqual(digest(raw), event["context_sha256"])
        self.assertEqual(json.loads(raw)["plan_sha256"], task["plan_sha"])
        self.assertLess(len(task["events"][-1]["payload"].encode()), 1024)
        self.assertEqual(self.c.run_once()["status"], "IDLE")
        self.assertEqual(self.c.verify()["status"], "INTACT")

    def test_ia4_captured_context_drift_is_detected(self):
        task = self.c.submit(self.source, key="capture", memory_root=self.memory)
        event = json.loads(task["events"][-1]["payload"])
        path = self.c.home / "contexts" / (event["context_sha256"] + ".json")
        path.write_bytes(b"{}")
        with self.assertRaisesRegex(ValueError, "context identity"):
            self.c.verify()

    def test_ia6_other_operator_does_not_match_old_name_outcomes(self):
        self.remember()
        other = source(self.c, self.base / "shelves.json", shelves=["commons", "research"])
        context = prepare_current(self.c, other, memory_root=self.memory)
        self.assertEqual(context["lesson_context"], [])
        self.assertEqual(context["outcome_memory"]["matching_records"], 0)

    def test_ia7_empty_memory_read_does_not_create_catalogue(self):
        before = census(self.c.home)
        context = prepare_current(self.c, self.source, memory_root=self.memory)
        self.assertEqual(context["outcome_memory"]["records"], [])
        self.assertFalse(self.memory.exists())
        self.assertEqual(census(self.c.home), before)

    def test_ia8_invalid_catalogue_refuses_submit_before_task_event(self):
        self.memory.mkdir()
        (self.memory / "unexpected.bin").write_bytes(b"outside the declared catalogue")
        before = census(self.c.home)
        with self.assertRaises(ValueError):
            self.c.submit(self.source, key="invalid-memory", memory_root=self.memory)
        self.assertEqual(census(self.c.home), before)

    def test_ia9_stale_lesson_does_not_veto_a_fresh_valid_baseline(self):
        self.remember()
        context = prepare_current(self.c, self.source, memory_root=self.memory)
        self.assertTrue(context["current_baseline_valid"])
        self.assertTrue(context["lesson_context"])
        self.assertTrue(all(g["applicability"] == "NOT_ESTABLISHED" for g in context["lesson_context"]))
        task = self.c.submit(self.source, key="healthy", memory_root=self.memory)
        self.c.approve(task["id"], task["plan_sha"])
        result = self.c.run_once()
        self.assertEqual(result["state"], "COMPLETED", result)
        self.assertEqual(ReadOnlyBoard(self.c.active_directory() / "pando").settings()["name"], "Fresh compatible task")

    def test_ia10_repeat_preserves_original_context_and_flags_mismatch(self):
        task = self.c.submit(self.source, key="repeat", memory_root=self.memory)
        before = census(self.c.home)
        again = self.c.submit(self.source, key="repeat", memory_root=self.memory)
        self.assertEqual(again["id"], task["id"])
        self.assertFalse(again["context_request"]["flag_mismatch"])
        self.assertTrue(again["context_request"]["original_context_sha256"])
        without = self.c.submit(self.source, key="repeat")
        self.assertTrue(without["context_request"]["flag_mismatch"])
        self.assertEqual(census(self.c.home), before)

    def test_ia11_alias_and_unsupported_source_refused_without_task(self):
        alias = self.base / "alias"
        alias.symlink_to(self.c.home, target_is_directory=True)
        with self.assertRaises(ValueError):
            prepare_current(Controller(alias), self.source)
        invalid = self.base / "paper.html"
        invalid.write_text("<html>This source is data, not an authorized profile.</html>")
        before = census(self.c.home)
        with self.assertRaises(ValueError):
            self.c.submit(invalid, key="bad-profile", memory_root=self.memory)
        self.assertEqual(census(self.c.home), before)

    def test_ia12_context_survives_reopen_and_keeps_reader_bounds(self):
        from lodestone_nurarihyon.preparation import _bounded_evidence
        task = self.c.submit(self.source, key="reopen", memory_root=self.memory)
        reopened = Controller(self.c.home)
        with reopened.locked():
            _bounded_evidence(reopened)
            self.assertEqual(reopened.verify()["status"], "INTACT")
        self.assertEqual(reopened.status(task["id"])["state"], "PLANNED")
        self.c.stop()
        context = prepare_current(reopened, self.source, memory_root=self.memory)
        self.assertTrue(context["stopped"])
        self.assertEqual(reopened.run_once()["status"], "STOPPED")
