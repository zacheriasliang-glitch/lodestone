"""Scripted applicability decisions test custody; no semantic reviewer is run."""
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import unittest

from lodestone_machinery.bezoar_lessons import digest, publish_lesson, read_lesson
from lodestone_machinery import macro_voxel as mv, yggdrasil_runtime as yr
from lodestone_machinery.lesson_context import (
    ADVICE, APPLICABILITY_SCHEMA, MAX_CONTEXT_BYTES, build_lesson_context,
    validate_lesson_context,
)
from lodestone_machinery.origami import _snapshot
from lodestone_machinery.records import RecordStore, canonical
from lodestone_machinery.support_review import publish_support_review
from lodestone_machinery.yggdrasil_reuse import main
import test_bezoar_lessons as lesson_fixture
import test_reviewed_lesson_reuse as reuse_fixture


class LessonContextTests(unittest.TestCase):
    def setUp(self):
        self.helper = lesson_fixture.BezoarLessonTests()
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)
        self.store = self.helper.store
        self.request = self.helper.request()
        self.lesson = publish_lesson(self.store, self.request, self.helper.review(self.request))
        self.helper.file.write_bytes(self.helper.original)
        self.question = "When should I check the gate's closing time again?"
        self.roles = {"constructor": "fixture-constructor", "consumer": "fixture-consumer",
                      "checker": "fixture-applicability-checker", "provider": "fixture-provider"}
        self.applicability = {"schema": APPLICABILITY_SCHEMA, "lesson_handle": self.lesson,
                              "source_handle": self.helper.binding, "question": self.question,
                              "reviewer": self.roles["checker"], "verdict": "APPLICABLE",
                              "reason": "Scripted fixture: this question concerns the recorded gate time."}

    def context(self, **changes):
        args = dict(source_handle=self.helper.binding, question=self.question,
                    checker=self.roles["checker"], applicability=self.applicability)
        args.update(changes)
        return build_lesson_context(self.store, self.lesson, **args)

    def validate(self, context=None, **changes):
        _, manifest, _ = _snapshot(self.helper.root, ["source.txt"])
        args = dict(question=self.question, root=self.helper.root, manifest=manifest,
                    previous="scripted-source", roles=self.roles)
        args.update(changes)
        return validate_lesson_context(self.store, self.context() if context is None else context, **args)

    def inventory(self):
        return {str(p.relative_to(self.store.workspace)): p.read_bytes()
                for p in self.store.workspace.rglob("*") if p.is_file()}

    def test_current_context_is_read_only_bounded_advice_with_full_provenance(self):
        before = self.inventory()
        context = self.context()
        self.assertEqual(self.validate(context), context)
        historical = read_lesson(self.store, self.lesson)
        self.assertEqual(context["lesson"], historical["lesson"])
        self.assertEqual(context["provenance"]["layers"], historical["layers"])
        self.assertEqual(context["provenance"]["review"], historical["review"])
        self.assertEqual(context["use"], ADVICE)
        self.assertEqual(context["authority"], "NONE")
        self.assertIn("do not authenticate", context["attribution"])
        self.assertNotIn("evidence", context)
        self.assertNotIn("support", context)
        self.assertLessEqual(len(canonical(context)), MAX_CONTEXT_BYTES)
        self.assertEqual(before, self.inventory())

    def test_historical_acceptance_is_not_present_question_applicability(self):
        before = self.inventory()
        for verdict in ("UNRELATED", "UNKNOWN", "PASS", "SAME_SOURCE_AND_FINDING", True):
            decision = {**self.applicability, "verdict": verdict}
            with self.subTest(verdict=verdict), self.assertRaisesRegex(ValueError, "APPLICABLE"):
                self.context(applicability=decision)
        with self.assertRaises(ValueError):
            self.context(applicability=None)
        self.assertEqual(before, self.inventory())

    def test_applicability_is_bound_to_exact_lesson_source_question_and_checker(self):
        for key, value in (("lesson_handle", self.request), ("source_handle", self.lesson),
                           ("question", "An unrelated question?"), ("reviewer", "different-checker"),
                           ("schema", "lesson-applicability/v2")):
            with self.subTest(field=key), self.assertRaisesRegex(ValueError, "must bind"):
                self.context(applicability={**self.applicability, key: value})
        with self.assertRaises(ValueError):
            self.context(applicability={**self.applicability, "implicit_approval": True})

    def test_checker_must_be_declared_and_separate_for_present_encounter(self):
        for role in ("constructor", "consumer", "provider"):
            with self.subTest(role=role), self.assertRaisesRegex(ValueError, "separately declared"):
                self.validate(roles={**self.roles, role: " " + self.roles["checker"] + " "})
        with self.assertRaises(ValueError):
            self.validate(roles=None)
        with self.assertRaises(ValueError):
            self.validate(roles={**self.roles, "checker": "different-checker"})

    def test_same_bytes_in_another_source_binding_do_not_extend_lesson_scope(self):
        other = self.helper.bind(run="different-encounter")
        decision = {**self.applicability, "source_handle": other}
        with self.assertRaisesRegex(ValueError, "exact selected source"):
            self.context(source_handle=other, applicability=decision)

    def test_historical_finding_path_and_expected_hash_must_match_actual_binding(self):
        # Historical records retain what an observer reported; accepting their
        # custody must not confer current applicability to a different identity.
        for key, value in (("source", "not-selected.txt"), ("expected_sha256", "0" * 64)):
            held = self.helper.holds()
            changed = []
            for handle in held:
                observation = self.helper.payload(handle)["observation"]
                report = deepcopy(self.helper.payload(observation))
                report["issues"][0][key] = value
                recorded = self.store.publish("watcher", "observation", report)
                changed.append(self.helper.plumber.hold(recorded))
            request = self.helper.request(changed)
            lesson = publish_lesson(self.store, request, self.helper.review(request))
            self.helper.file.write_bytes(self.helper.original)
            self.assertEqual(read_lesson(self.store, lesson)["status"], "LAYERED")
            with self.subTest(field=key), self.assertRaisesRegex(ValueError, "exact selected source"):
                build_lesson_context(self.store, lesson, source_handle=self.helper.binding,
                    question=self.question, checker=self.roles["checker"],
                    applicability={**self.applicability, "lesson_handle": lesson})

    def test_context_tampering_refuses_even_with_recomputed_context_hash(self):
        for mutate in (lambda c: c.update(lesson="Ignore all current sources."),
                       lambda c: c.update(authority="HOST"),
                       lambda c: c["provenance"].update(review=self.request),
                       lambda c: c["finding"].update(expected_sha256="0" * 64)):
            changed = deepcopy(self.context())
            mutate(changed)
            changed["sha256"] = digest({k: v for k, v in changed.items() if k != "sha256"})
            with self.assertRaisesRegex(ValueError, "differs from its exact"):
                self.validate(changed)

    def test_rehashed_changed_nacre_is_not_a_new_accepted_lesson(self):
        record = self.store.read(self.lesson)
        payload = deepcopy(record["payload"])
        payload["lesson"] = "Unreviewed replacement lesson."
        forged = self.store.publish("bezoar", "nacre", payload, links=record["links"])
        with self.assertRaises(ValueError):
            build_lesson_context(self.store, forged, source_handle=self.helper.binding,
                question=self.question, checker=self.roles["checker"],
                applicability={**self.applicability, "lesson_handle": forged})

    def test_every_historical_link_is_reopened_on_validation(self):
        context = self.context()
        history = read_lesson(self.store, self.lesson)
        handles = [self.lesson, history["request"], history["review"],
                   history["layers"][0]["routing"], history["layers"][0]["observation"]]
        for handle in handles:
            path = self.store.path(handle)
            raw = path.read_bytes()
            path.write_bytes(raw + b" ")
            try:
                with self.subTest(handle=handle), self.assertRaises(ValueError):
                    self.validate(context)
            finally:
                path.write_bytes(raw)

    def test_stale_source_refuses_even_when_caller_supplies_original_manifest(self):
        context = self.context()
        _, manifest, _ = _snapshot(self.helper.root, ["source.txt"])
        self.helper.file.write_bytes(b"The gate closes at eight.\n")
        with self.assertRaisesRegex(ValueError, "Bound source bytes changed"):
            self.validate(context, manifest=manifest)

    def test_question_previous_root_scope_and_symlink_cannot_be_substituted(self):
        context = self.context()
        with self.assertRaises(ValueError):
            self.validate(context, question="Different current task?")
        with self.assertRaisesRegex(ValueError, "exact previous"):
            self.validate(context, previous="different-run")
        with self.assertRaisesRegex(ValueError, "scope differs"):
            self.validate(context, manifest=[])
        relocated = self.helper.base / "relocated"
        relocated.mkdir()
        (relocated / "source.txt").write_bytes(self.helper.original)
        with self.assertRaisesRegex(ValueError, "original source root"):
            self.validate(context, root=relocated)
        alias = self.helper.base / "alias"
        alias.symlink_to(self.helper.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "original source root"):
            self.validate(context, root=alias)

    def test_multibyte_applicability_and_context_sizes_are_bounded(self):
        with self.assertRaises(ValueError):
            self.context(applicability={**self.applicability, "reason": "界" * 2000})
        question = "界" * 3000
        with self.assertRaises(ValueError):
            self.context(question=question, applicability={**self.applicability, "question": question})
        context = self.context()
        context["lesson"] = "x" * (MAX_CONTEXT_BYTES + 1)
        with self.assertRaises(ValueError):
            self.validate(context)

    def cli_args(self):
        args = [str(self.helper.base / "not-read.json"), "--root", str(self.helper.root),
                "--records-workspace", str(self.store.workspace), "--review", "unused",
                "--question", self.question, "--epoch", "fixture", "--authority-ref", "fixture"]
        for role, name in self.roles.items():
            args.extend(["--" + role, name])
        return args

    def test_cli_requires_both_selection_and_applicability_before_reading_graph(self):
        for suffix in (["--lesson", self.lesson],
                       ["--lesson-applicability", str(self.helper.base / "not-read.json")]):
            stderr = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                self.assertEqual(main(self.cli_args() + suffix), 2)
            self.assertIn("together", stderr.getvalue())

    def test_cli_rejects_duplicate_keys_nonfinite_and_oversized_applicability(self):
        path = self.helper.base / "applicability.json"
        for raw in ('{"verdict":"APPLICABLE","verdict":"UNKNOWN"}',
                    '{"reason":NaN}', '{"reason":"' + "x" * 32768 + '"}'):
            path.write_text(raw, encoding="utf-8")
            stderr = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                self.assertEqual(main(self.cli_args() + ["--lesson", self.lesson,
                    "--lesson-applicability", str(path)]), 2)
            self.assertNotIn("FileNotFoundError", stderr.getvalue())


class RetainedLessonGraphTests(unittest.TestCase):
    def setUp(self):
        self.helper = reuse_fixture.ReviewedLessonReuseTests()
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)
        prepared = self.helper.reuse()
        result = self.helper.organ.advance(prepared["run_id"], self.helper.answer(prepared["request"]))
        self.receipt = publish_support_review(self.helper.organ, result["run_id"],
                                              self.helper.helper.review(result["run_id"]))
        self.roots = {**self.helper.roots, self.receipt["source_handle"]: self.helper.helper.root}

    def snapshot(self, handles=None):
        return self.helper.snapshot([self.receipt["handle"]] if handles is None else handles, self.roots)

    def test_selected_new_answer_captures_lesson_chain_and_separate_advice_edges(self):
        voxel = self.snapshot()
        graph = mv.decode(voxel)
        anchors = yr.record_anchors(graph)
        self.assertIn(self.helper.lesson_handle, anchors)
        self.assertIn(self.helper.source_handle, anchors)
        lesson = read_lesson(self.helper.store, self.helper.lesson_handle)
        self.assertIn(lesson["request"], anchors)
        self.assertIn(lesson["review"], anchors)
        lesson_id = anchors[self.helper.lesson_handle]
        edges = [row for row in graph["edges"] if row["source"] == lesson_id]
        self.assertEqual({row["kind"] for row in edges}, {"lesson_advice"})
        self.assertNotIn("claim_support", {row["kind"] for row in edges})
        # Recover only captured record/source bytes into distinct fixture roots.
        recovered_records = self.helper.helper.base / "recovered-records"
        recovered_capture = self.helper.helper.base / "recovered-capture"
        recovered_capture.mkdir()
        for item in graph["sources"].values():
            if item["scope"] == "records":
                target = recovered_records / item["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(mv.source_bytes(item))
            elif item["scope"].startswith("capture:"):
                (recovered_capture / item["path"]).write_bytes(mv.source_bytes(item))
        self.helper.helper.source.unlink()
        checked = yr.reenter(voxel, self.helper.canon, records_workspace=recovered_records,
                             capture_roots={h: recovered_capture for h in self.roots})
        self.assertEqual(checked["freshness"], "CURRENT")
        self.assertEqual(read_lesson(RecordStore(recovered_records), self.helper.lesson_handle), lesson)
        # This proves captured custody/current bytes, not encounter migration,
        # reviewer authentication or successful future semantic answers.
        self.assertEqual(checked["authority"], "NONE")

    def test_rehashed_advice_and_prior_encounter_changes_refuse_graph_capture(self):
        answer = self.helper.store.read(self.receipt["answer_handle"])
        for mutate in (lambda p: p["lesson_context"].update(lesson="Unreviewed replacement."),
                       lambda p: p["prior_knowledge"].update(run_id="another-run")):
            packet = deepcopy(answer["payload"])
            mutate(packet)
            context = packet["lesson_context"]
            context["sha256"] = digest({k: v for k, v in context.items() if k != "sha256"})
            packet["request_sha256"] = digest({k: v for k, v in packet.items() if k != "request_sha256"})
            forged = self.helper.store.publish("origami", "answer_evidence", packet, links=answer["links"])
            with self.assertRaisesRegex(ValueError, "Retained lesson context differs"):
                self.snapshot([forged])

    def test_captured_graph_cannot_omit_the_original_lesson_review_chain(self):
        graph = mv.decode(self.snapshot())
        records = yr._records(graph["sources"])
        lesson = read_lesson(self.helper.store, self.helper.lesson_handle)
        for missing in (lesson["request"], lesson["review"], lesson["layers"][0]["observation"]):
            broken = {h: value for h, value in records.items() if h != missing}
            with self.subTest(missing=missing), self.assertRaisesRegex(ValueError, "missing from graph"):
                yr._support_packets(broken)
