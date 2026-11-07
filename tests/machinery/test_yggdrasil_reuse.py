"""A new question reuses exact evidence but cannot inherit answer acceptance."""
from pathlib import Path
import shutil
import unittest

from lodestone_machinery import yggdrasil_runtime as yr
from lodestone_machinery.support_review import publish_support_review
from lodestone_machinery.yggdrasil_reuse import prepare_reuse
import test_support_review as support_fixture
from test_macro_voxel import fixture


class YggdrasilReuseTests(unittest.TestCase):
    def setUp(self):
        self.helper = support_fixture.SupportReviewTests()
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)
        self.root = self.helper.base / "canon"
        fixture(self.root)
        self.run_id = self.helper.completed()
        self.receipt = publish_support_review(self.helper.organ, self.run_id,
                                              self.helper.review(self.run_id))
        self.roots = {self.receipt["source_handle"]: self.helper.root}
        self.voxel = yr.snapshot(self.root, records_workspace=self.helper.organ.workspace,
                                 capture_roots=self.roots, record_handles=[self.receipt["handle"]])

    def reuse(self, **changes):
        args = dict(records_workspace=self.helper.organ.workspace, capture_roots=self.roots,
                    review_handle=self.receipt["handle"], question="Which operating mode applies now?",
                    epoch="fixture-current", authority_ref="scripted follow-up fixture",
                    roles=self.helper.roles)
        args.update(changes)
        return prepare_reuse(self.voxel, self.root, **args)

    def inventory(self):
        return sorted(str(p.relative_to(self.helper.organ.workspace))
                      for p in self.helper.organ.workspace.rglob("*"))

    def test_new_question_keeps_passages_and_requires_new_answer_review(self):
        result = self.reuse()
        self.assertEqual(result["status"], "NEEDS_PROVIDER")
        self.assertNotEqual(result["run_id"], self.run_id)
        self.assertEqual(result["request"]["question"], "Which operating mode applies now?")
        self.assertEqual(result["reuse"]["new_answer_review"], "REQUIRED")
        self.assertFalse(result["reuse"]["review_inherited"])
        self.assertEqual(result["request"]["prior_knowledge"]["run_id"], self.run_id)
        old = self.helper.organ.status(self.run_id)
        self.assertEqual(result["request"]["evidence"], old["evidence"])
        self.assertEqual(yr.reenter(self.voxel, self.root,
            records_workspace=self.helper.organ.workspace, capture_roots=self.roots)["freshness"], "CURRENT")

    def test_drift_refuses_before_creating_new_encounter(self):
        self.helper.source.write_text("Changed source.\n")
        before = self.inventory()
        with self.assertRaisesRegex(ValueError, "CURRENT"):
            self.reuse()
        self.assertEqual(before, self.inventory())

    def test_absent_graph_handle_is_not_silently_loaded(self):
        before = self.inventory()
        with self.assertRaisesRegex(ValueError, "captured in this graph"):
            self.reuse(review_handle=self.receipt["source_handle"] + "invalid")
        self.assertEqual(before, self.inventory())

    def test_fail_review_cannot_seed_an_accepted_reuse(self):
        review = self.helper.review(self.run_id)
        review["answer_verdict"] = "FAIL"
        review["reason"] = "Scripted negative review."
        failed = publish_support_review(self.helper.organ, self.run_id, review)
        self.voxel = yr.snapshot(self.root, records_workspace=self.helper.organ.workspace,
                                 capture_roots=self.roots, record_handles=[failed["handle"]])
        before = self.inventory()
        with self.assertRaisesRegex(ValueError, "no PASS review"):
            self.reuse(review_handle=failed["handle"])
        self.assertEqual(before, self.inventory())

    def test_graph_relocation_does_not_silently_migrate_original_encounter(self):
        relocated = self.helper.base / "relocated"
        shutil.copytree(self.helper.root, relocated)
        roots = {self.receipt["source_handle"]: relocated}
        self.assertEqual(yr.reenter(self.voxel, self.root,
            records_workspace=self.helper.organ.workspace, capture_roots=roots)["freshness"], "CURRENT")
        before = self.inventory()
        with self.assertRaisesRegex(ValueError, "original encounter source root"):
            self.reuse(capture_roots=roots)
        self.assertEqual(before, self.inventory())

    def test_changed_encounter_artifact_refuses_even_when_graph_sources_are_current(self):
        artifact = self.helper.organ._dir(self.run_id) / "answer.md"
        artifact.write_text("Replaced answer.\n")
        before = self.inventory()
        with self.assertRaisesRegex(ValueError, "artifact changed"):
            self.reuse()
        self.assertEqual(before, self.inventory())
