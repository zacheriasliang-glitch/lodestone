"""Scripted end-to-end custody checks, not semantic learning experiments."""
from contextlib import redirect_stdout
from datetime import datetime, timezone
import io
import json
import unittest
from unittest.mock import patch

from lodestone_machinery import yggdrasil_runtime as yr
from lodestone_machinery.bezoar import Plumber
from lodestone_machinery.bezoar_lessons import (
    REVIEW_SCHEMA, prepare_lesson, publish_lesson, read_lesson,
)
from lodestone_machinery.organs.watcher import observe
from lodestone_machinery.origami import Origami
from lodestone_machinery.records import RecordStore
from lodestone_machinery.support_review import (
    publish_support_review, reviewed_answer, support_request,
)
from lodestone_machinery.yggdrasil_reuse import main, prepare_reuse
from test_macro_voxel import fixture
import test_support_review as support_fixture


class ReviewedLessonReuseTests(unittest.TestCase):
    def setUp(self):
        self.helper = support_fixture.SupportReviewTests()
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)
        self.organ = self.helper.organ
        self.store = RecordStore(self.organ.workspace)
        self.canon = self.helper.base / "canon"
        fixture(self.canon)
        self.original = self.helper.source.read_bytes()
        self.old_run = self.helper.completed()
        self.old_review = publish_support_review(
            self.organ, self.old_run, self.helper.review(self.old_run))
        self.source_handle = self.old_review["source_handle"]
        self.roots = {self.source_handle: self.helper.root}
        self.question = "Which operating mode is recorded in the current source?"
        self.lesson_text = (
            "Repeated drift observations need not be separate incidents. "
            "Distinguish the current recorded mode from the historical finding.")
        self.lesson_handle = self.make_lesson(self.source_handle)
        self.voxel = self.snapshot([self.old_review["handle"], self.lesson_handle])

    def make_lesson(self, source_handle):
        self.helper.source.write_bytes(b"Current label: green. Mode: live.\n")
        plumber = Plumber(self.organ.workspace)
        held = []
        # These dates and every review/answer below are scripted fixture inputs.
        for day in (3, 4):
            with patch("lodestone_machinery.organs.watcher.datetime") as clock:
                clock.now.return_value = datetime(2026, 1, day, tzinfo=timezone.utc)
                watched = observe(self.store, [source_handle],
                                  capture_roots={source_handle: self.helper.root})
            held.append(plumber.hold(watched))
        request = prepare_lesson(
            self.store, held, lesson=self.lesson_text,
            producer="scripted-lesson-author", checker="scripted-lesson-checker")
        payload = self.store.read(request)["payload"]
        review = {
            "schema": REVIEW_SCHEMA, "request_handle": request,
            "request_sha256": payload["request_sha256"],
            "reviewer": "scripted-lesson-checker", "verdict": "PASS",
            "layer_verdicts": [
                {"observation": layer["observation"], "verdict": "SUPPORTED",
                 "reason": "Scripted fixture: both observations describe the same continuing change."}
                for layer in payload["request"]["layers"]],
            "applicability": "SAME_SOURCE_AND_FINDING",
            "reason": "Scripted review of this source discrepancy, not a general learning claim.",
        }
        lesson = publish_lesson(self.store, request, review)
        self.helper.source.write_bytes(self.original)
        return lesson

    def snapshot(self, handles, roots=None):
        return yr.snapshot(self.canon, records_workspace=self.organ.workspace,
                           capture_roots=self.roots if roots is None else roots,
                           record_handles=handles)

    def applicability(self, **changes):
        review = {
            "schema": "lesson-applicability/v1", "lesson_handle": self.lesson_handle,
            "source_handle": self.source_handle, "question": self.question,
            "reviewer": self.helper.roles["checker"], "verdict": "APPLICABLE",
            "reason": "Scripted assessment: current mode must remain distinct from historical drift.",
        }
        review.update(changes)
        return review

    def reuse(self, **changes):
        args = dict(records_workspace=self.organ.workspace, capture_roots=self.roots,
                    review_handle=self.old_review["handle"], question=self.question,
                    epoch="fixture-current", authority_ref="scripted integration fixture",
                    roles=self.helper.roles, lesson_handle=self.lesson_handle,
                    lesson_applicability=self.applicability())
        args.update(changes)
        return prepare_reuse(self.voxel, self.canon, **args)

    def inventory(self):
        return sorted(str(p.relative_to(self.organ.workspace))
                      for p in self.organ.workspace.rglob("*"))

    def answer(self, request, *, support=None):
        return {
            "provider": self.helper.roles["provider"], "model": "NONE_SCRIPTED_FIXTURE",
            "response_id": None, "usage": None,
            "response": {
                "request_id": request["request_id"], "decision": "ANSWER",
                "claims": [{"id": "mode", "subject": "mode", "value": "paper",
                            "scope": "fixture only", "epoch": "fixture-current",
                            "basis": "RECORDED", "support":
                            [request["evidence"][0]["id"]] if support is None else support}],
                "relations": [], "alternatives": [], "answer": "The current recorded mode is paper.",
                "answer_claim_ids": ["mode"], "next_check": None,
                "coverage_assumptions": ["Only the current fixture source supports the answer."],
                "unresolved": [],
            },
        }

    def test_reviewed_lesson_reaches_provider_and_new_answer_gets_its_own_review(self):
        historical = {handle: self.store.path(handle).read_bytes()
                      for handle in (self.lesson_handle, self.old_review["handle"])}
        prepared = self.reuse()
        self.assertEqual(prepared["status"], "NEEDS_PROVIDER")
        self.assertNotEqual(prepared["run_id"], self.old_run)
        self.assertFalse(prepared["reuse"]["review_inherited"])
        self.assertEqual(prepared["reuse"]["new_answer_review"], "REQUIRED")
        # Reopen through a fresh object to exercise persisted provider transport.
        reopened = Origami(self.organ.workspace)
        request = reopened.request(prepared["run_id"])
        context = request["lesson_context"]
        self.assertEqual(context["lesson_handle"], self.lesson_handle)
        self.assertEqual(context["lesson"], self.lesson_text)
        self.assertEqual(context["source_handle"], self.source_handle)
        self.assertEqual(context["applicability"], self.applicability())
        self.assertEqual(context["authority"], "NONE")
        self.assertIn("UNTRUSTED_ADVICE_NOT_SOURCE_EVIDENCE", context["use"])
        self.assertEqual(request["evidence"], self.organ.status(self.old_run)["evidence"])
        self.assertNotIn(self.lesson_handle, {row["id"] for row in request["evidence"]})
        result = reopened.advance(prepared["run_id"], self.answer(request))
        self.assertEqual(result["status"], "ANSWER")
        self.assertEqual(result["lesson_context"], context)
        self.assertEqual(result["transport"], "HAND_CARRIED")
        self.assertEqual(reviewed_answer(reopened, result["run_id"])["status"],
                         "NOT_INDEPENDENTLY_REVIEWED")
        packet = support_request(reopened, result["run_id"])
        self.assertEqual(packet["lesson_context"], context)
        with self.assertRaises(ValueError):
            reviewed_answer(reopened, result["run_id"], self.old_review["handle"])
        accepted = publish_support_review(reopened, result["run_id"],
                                          self.helper.review(result["run_id"]))
        self.assertNotEqual(accepted["handle"], self.old_review["handle"])
        self.assertEqual(reviewed_answer(reopened, result["run_id"], accepted["handle"])["status"],
                         "REVIEWED_PASS")
        for handle, data in historical.items():
            self.assertEqual(self.store.path(handle).read_bytes(), data)
        self.assertTrue(read_lesson(self.store, self.lesson_handle)["seeds_remain_held"])
        self.assertEqual(self.helper.source.read_bytes(), self.original)

    def test_unrelated_unknown_or_question_swapped_review_creates_no_encounter(self):
        for changes in ({"verdict": "UNRELATED"}, {"verdict": "UNKNOWN"},
                        {"question": "What is the capital of France?"},
                        {"reviewer": self.helper.roles["provider"]}):
            before = self.inventory()
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.reuse(lesson_applicability=self.applicability(**changes))
            self.assertEqual(before, self.inventory())

    def test_lesson_must_be_selected_in_the_graph(self):
        self.voxel = self.snapshot([self.old_review["handle"]])
        before = self.inventory()
        with self.assertRaises(ValueError):
            self.reuse()
        self.assertEqual(before, self.inventory())

    def test_cli_transports_explicit_selected_lesson_to_pending_request(self):
        voxel_path = self.helper.base / "reviewed-graph.json"
        applicability_path = self.helper.base / "applicability.json"
        voxel_path.write_text(json.dumps(self.voxel))
        applicability_path.write_text(json.dumps(self.applicability()))
        args = [str(voxel_path), "--root", str(self.canon),
                "--records-workspace", str(self.organ.workspace),
                "--capture-root", self.source_handle + "=" + str(self.helper.root),
                "--review", self.old_review["handle"], "--question", self.question,
                "--epoch", "fixture-current", "--authority-ref", "scripted CLI fixture",
                "--lesson", self.lesson_handle, "--lesson-applicability", str(applicability_path)]
        for role, identity in self.helper.roles.items():
            args.extend(["--" + role, identity])
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(args)
        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "NEEDS_PROVIDER")
        self.assertEqual(result["request"]["lesson_context"]["lesson"], self.lesson_text)
        self.assertEqual(self.organ.request(result["run_id"]), result["request"])

    def test_same_bytes_in_another_encounter_do_not_rebind_a_lesson(self):
        other_run = self.helper.completed(question="Which label is recorded in this other encounter?")
        other_review = publish_support_review(self.organ, other_run, self.helper.review(other_run))
        other_lesson = self.make_lesson(other_review["source_handle"])
        roots = {**self.roots, other_review["source_handle"]: self.helper.root}
        self.voxel = self.snapshot(
            [self.old_review["handle"], other_review["handle"], other_lesson], roots)
        before = self.inventory()
        with self.assertRaises(ValueError):
            self.reuse(capture_roots=roots, lesson_handle=other_lesson,
                       lesson_applicability=self.applicability(lesson_handle=other_lesson))
        self.assertEqual(before, self.inventory())

    def test_retained_lesson_cannot_be_cited_as_current_source_evidence(self):
        prepared = self.reuse()
        result = self.organ.advance(prepared["run_id"],
                                    self.answer(prepared["request"], support=[self.lesson_handle]))
        self.assertEqual(result["status"], "INVALID")
        self.assertIn("unobserved evidence", result["reason"])
        self.assertTrue((self.organ._dir(prepared["run_id"]) / "response-000.json").exists())

    def test_later_source_drift_cannot_be_overridden_by_accepted_lesson(self):
        self.helper.source.write_bytes(b"Current label: green. Mode: live.\n")
        before = self.inventory()
        with self.assertRaisesRegex(ValueError, "CURRENT"):
            self.reuse()
        self.assertEqual(before, self.inventory())

    def test_source_changed_after_preparation_refuses_before_provider_response(self):
        prepared = self.reuse()
        self.helper.source.write_bytes(b"Current label: green. Mode: live.\n")
        before = self.inventory()
        with self.assertRaises(ValueError):
            self.organ.request(prepared["run_id"])
        self.assertEqual(before, self.inventory())
        self.assertFalse((self.organ._dir(prepared["run_id"]) / "response-000.json").exists())

    def test_lesson_survives_an_additional_evidence_read(self):
        self.original += b"\nAdditional note: paper mode does not authorize live execution.\n"
        self.helper.source.write_bytes(self.original)
        first = self.organ.start(self.helper.root, ["decision.md"], self.question,
                                 "fixture-current", "scripted multi-step fixture",
                                 roles=self.helper.roles, initial_passages=1)
        self.old_run = first["run_id"]
        self.organ.advance(self.old_run, self.answer(self.organ.request(self.old_run)))
        self.old_review = publish_support_review(self.organ, self.old_run,
                                                 self.helper.review(self.old_run))
        self.source_handle = self.old_review["source_handle"]
        self.roots = {self.source_handle: self.helper.root}
        self.lesson_handle = self.make_lesson(self.source_handle)
        self.voxel = self.snapshot([self.old_review["handle"], self.lesson_handle])
        prepared = self.reuse()
        request = prepared["request"]
        self.assertEqual(len(request["evidence"]), 1)
        self.assertEqual(request["evidence"][0]["end_line"], 1)
        envelope = self.answer(request)
        envelope["response"].update(
            decision="NEED_EVIDENCE", answer=None, answer_claim_ids=[],
            next_check={"source": "decision.md", "start_line": 3, "end_line": 3,
                        "purpose": "Check whether the separate note records action authority.",
                        "outcomes": ["Explicit authority permits the stated action.",
                                     "No authority is recorded by this note."]})
        pending = self.organ.advance(prepared["run_id"], envelope)
        self.assertEqual(pending["status"], "NEEDS_PROVIDER")
        next_request = self.organ.request(prepared["run_id"])
        self.assertEqual(next_request["step"], 1)
        self.assertEqual(len(next_request["evidence"]), 2)
        self.assertEqual(next_request["lesson_context"], request["lesson_context"])
        result = self.organ.advance(prepared["run_id"], self.answer(next_request))
        self.assertEqual(result["status"], "ANSWER")
        self.assertEqual(len(result["calls"]), 2)
        self.assertEqual(support_request(self.organ, result["run_id"])["lesson_context"],
                         request["lesson_context"])

    def test_completed_review_refuses_a_changed_context_copy(self):
        prepared = self.reuse()
        result = self.organ.advance(prepared["run_id"], self.answer(prepared["request"]))
        directory = self.organ._dir(result["run_id"])
        for name in ("request-000.json", "result.json"):
            path = directory / name
            original = path.read_bytes()
            changed = json.loads(original)
            changed["lesson_context"]["lesson"] = "Changed after the answer was supplied."
            path.write_text(json.dumps(changed))
            try:
                with self.subTest(file=name), self.assertRaises(ValueError):
                    support_request(self.organ, result["run_id"])
            finally:
                path.write_bytes(original)

    def test_change_during_provider_preserves_returned_response_and_cost_without_retry(self):
        for change in ("source", "lesson"):
            prepared = self.reuse()
            calls, returned = [], []
            path = self.helper.source if change == "source" else self.store.path(self.lesson_handle)
            original = path.read_bytes()

            def provider(request):
                calls.append(request["request_id"])
                envelope = self.answer(request)
                envelope["usage"] = {"input_tokens": 40, "output_tokens": 10}
                returned.append(envelope)
                path.write_bytes(original + b" ")
                return envelope

            try:
                result = self.organ.run(prepared["run_id"], provider)
                self.assertEqual(result["status"], "UNAVAILABLE")
                self.assertEqual(len(calls), 1)
                directory = self.organ._dir(result["run_id"])
                self.assertEqual(json.loads((directory / "response-000.json").read_text()), returned[0])
                self.assertEqual(result["calls"][0]["usage"]["counts"]["input_tokens"], 40)
                self.assertEqual(result["calls"][0]["usage"]["counts"]["output_tokens"], 10)
                self.assertGreaterEqual(result["calls"][0]["seconds"], 0)
                self.organ.run(prepared["run_id"], provider)
                self.assertEqual(len(calls), 1)
            finally:
                path.write_bytes(original)


if __name__ == "__main__":
    unittest.main()
