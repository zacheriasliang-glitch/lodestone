import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from lodestone_machinery import Engine, WorkerReturn
from lodestone_machinery.organs import build_brief


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "sources"
        self.root.mkdir()
        self.source = self.root / "note.md"
        self.source.write_text("# Workshop\n\nCeramic evidence preserves a failed result.\n\nCeramic repairs retain the old observation.\n", encoding="utf-8")
        self.workspace = self.base / "work"
        self.engine = Engine(self.workspace)

    def tearDown(self):
        self.temp.cleanup()

    def submit(self, **kwargs):
        return self.engine.submit(self.root, ["note.md"], "ceramic", **kwargs)

    def test_actual_artifact_persists_and_frozen_prediction_survives(self):
        original = self.source.read_bytes()
        prediction = {"audit_verdict": "FAIL"}
        job = self.submit(prediction=prediction)
        prediction["audit_verdict"] = "PASS"
        result = self.engine.run(job)
        self.assertEqual(result["state"], "COMPLETED", result)
        self.assertIn("Ceramic evidence preserves", Path(result["result"]["artifact"]).read_text())
        self.assertEqual(result["result"]["residue"]["prediction"]["audit_verdict"], "FAIL")
        self.assertEqual(result["result"]["residue"]["outcome"], "MISMATCH")
        reopened = Engine(self.workspace).status(job)
        self.assertEqual(reopened["state"], "COMPLETED")
        self.assertEqual(self.source.read_bytes(), original)
        self.assertEqual(self.engine.resume(job)["events"], reopened["events"])

    def test_missing_evidence_blocks_and_keeps_diagnostic_artifact(self):
        job = self.engine.submit(self.root, ["note.md"], "unfindablequartz")
        result = self.engine.run(job)
        self.assertEqual(result["state"], "BLOCKED", result)
        self.assertEqual(result["result"]["combined_verdict"], "UNKNOWN")
        self.assertTrue(Path(result["result"]["artifact"]).exists())

    def test_literal_worker_preserves_crlf_cr_and_lf_through_artifact_audit(self):
        for ending in (b"\r\n", b"\r", b"\n"):
            with self.subTest(line_ending=ending):
                original = ending.join([b"Ceramic evidence records failure.", b"Second literal line.", b""])
                self.source.write_bytes(original)
                job = self.submit()
                result = self.engine.run(job)
                self.assertEqual(result["state"], "COMPLETED", result)
                artifact = Path(result["result"]["artifact"]).read_bytes()
                self.assertIn(original, artifact)
                self.assertEqual(self.source.read_bytes(), original)
                self.assertEqual(result["result"]["combined_verdict"], "PASS")

    def test_corrupt_output_rejected_by_actual_audit(self):
        class Corrupt:
            artifact_contract = "literal_brief"
            def build(self, request):
                return WorkerReturn(request.envelope_id, "All ceramic experiments succeeded.")
        self.engine.register_worker("corrupt", Corrupt())
        job = self.submit(worker="corrupt")
        result = self.engine.run(job)
        self.assertEqual(result["state"], "REJECTED", result)
        self.assertEqual(result["result"]["combined_verdict"], "FAIL")

    def test_task_custody_rejects_changed_goal(self):
        class AlterIntent:
            artifact_contract = "literal_brief"
            def build(self, request):
                request.envelope["goal"] = "Rewrite history"
                return WorkerReturn(request.envelope_id, build_brief(request.folded))
        self.engine.register_worker("alter", AlterIntent())
        job = self.submit(worker="alter")
        result = self.engine.run(job)
        self.assertEqual(result["state"], "FAILED")
        self.assertIn("custody", result["error"])
        self.assertEqual(result["envelope"]["goal"], "ceramic")

    def test_source_drift_is_detected_without_source_repair(self):
        job = self.submit()
        self.assertEqual(self.engine.run(job)["state"], "COMPLETED")
        self.source.write_text("New ceramic evidence contradicts the old result.\n")
        before = self.engine.status(job)["events"]
        result = self.engine.maintain()
        self.assertTrue(any(item["kind"] == "SOURCE_DRIFT" for item in result["findings"]))
        self.assertEqual(before, self.engine.status(job)["events"])
        self.engine.recover(job)
        self.assertEqual(self.source.read_text(), "New ceramic evidence contradicts the old result.\n")

    def test_recovery_restores_real_output_and_preserves_damage(self):
        job = self.submit()
        result = self.engine.run(job)
        path = Path(result["result"]["artifact"])
        original = path.read_bytes()
        path.write_text("damaged record")
        self.assertEqual(self.engine.maintain()["verdict"], "ATTENTION")
        repair = self.engine.recover(job)
        self.assertEqual(repair["verdict"], "OK")
        self.assertEqual(path.read_bytes(), original)
        damaged = Path(repair["repaired"][0]["preserved_damage"])
        self.assertEqual(damaged.read_text(), "damaged record")
        self.assertEqual(self.engine.maintain()["verdict"], "OK")

    def test_recovery_preserves_directory_in_place_of_artifact(self):
        job = self.submit()
        result = self.engine.run(job)
        path = Path(result["result"]["artifact"])
        original = path.read_bytes()
        path.unlink()
        path.mkdir()
        (path / "accidental.txt").write_text("retain this damage")
        repaired = self.engine.recover(job)
        self.assertEqual(repaired["verdict"], "OK", repaired)
        self.assertEqual(path.read_bytes(), original)
        saved = Path(repaired["repaired"][0]["preserved_damage"])
        self.assertEqual((saved / "accidental.txt").read_text(), "retain this damage")
        self.assertEqual(self.engine.status(job)["events"][-1]["kind"], "RECOVERY")

    def test_recovery_records_unrecoverable_content_without_promotion(self):
        job = self.submit()
        result = self.engine.run(job)
        path = Path(result["result"]["artifact"])
        info = next(a for a in result["artifacts"] if a["name"] == "brief.md")
        path.unlink()
        (self.workspace / "blobs" / info["sha256"]).write_text("corrupt blob")
        recovered = self.engine.recover(job)
        self.assertEqual(recovered["verdict"], "BLOCKED")
        self.assertFalse(path.exists())
        self.assertEqual(self.engine.status(job)["events"][-1]["kind"], "RECOVERY")

    def test_rejected_result_is_not_promoted_by_recovery(self):
        class Corrupt:
            artifact_contract = "literal_brief"
            def build(self, request):
                return WorkerReturn(request.envelope_id, "unsupported answer")
        self.engine.register_worker("bad", Corrupt())
        job = self.submit(worker="bad")
        state = self.engine.run(job)
        artifact = Path(state["result"]["artifact"])
        artifact.unlink()
        self.engine.recover(job)
        recovered = self.engine.status(job)
        self.assertEqual(recovered["state"], "REJECTED")
        self.assertTrue(any(e["kind"] == "REJECTED" for e in recovered["events"]))
        self.assertEqual(artifact.read_text(), "unsupported answer")

    def test_interrupted_worker_is_uncertain_and_never_replayed(self):
        class Interrupted:
            artifact_contract = "literal_brief"
            calls = 0
            def build(self, request):
                self.calls += 1
                raise KeyboardInterrupt("simulated process interruption")
        worker = Interrupted()
        self.engine.register_worker("interrupted", worker)
        job = self.submit(worker="interrupted")
        with self.assertRaises(KeyboardInterrupt):
            self.engine.run(job)
        self.assertEqual(self.engine.status(job)["state"], "RUNNING")
        self.assertEqual(self.engine.resume(job)["state"], "UNCERTAIN")
        self.assertEqual(self.engine.resume(job)["state"], "UNCERTAIN")
        with self.assertRaises(ValueError):
            self.engine.run(job)
        self.assertEqual(worker.calls, 1)

    def test_concurrent_claim_cannot_duplicate_worker_effects(self):
        started, release = threading.Event(), threading.Event()
        class Slow:
            artifact_contract = "literal_brief"
            calls = 0
            def build(self, request):
                self.calls += 1
                started.set()
                if not release.wait(5):
                    raise RuntimeError("test synchronization failed")
                return WorkerReturn(request.envelope_id, build_brief(request.folded))
        worker = Slow()
        self.engine.register_worker("slow", worker)
        job = self.submit(worker="slow")
        results = []
        thread = threading.Thread(target=lambda: results.append(self.engine.run(job)))
        thread.start()
        try:
            self.assertTrue(started.wait(5))
            with self.assertRaises(RuntimeError):
                Engine(self.workspace).run(job)
            with self.assertRaises(RuntimeError):
                self.engine.resume(job)
        finally:
            release.set()
            thread.join(5)
        self.assertEqual(worker.calls, 1)
        self.assertEqual(results[0]["state"], "COMPLETED", results)

    def test_partial_evidence_unknown_is_preserved_in_ruler(self):
        job = self.engine.submit(self.root, ["note.md"], "ceramic unfindablequartz")
        result = self.engine.run(job)
        self.assertEqual(result["state"], "BLOCKED", result)
        self.assertEqual(result["result"]["ruler"]["verdict"], "UNKNOWN")
        self.assertNotIn("audit_pass", result["result"]["observations"])

    def test_registered_nonbrief_work_has_its_own_artifact_auditor(self):
        class Inventory:
            def build(self, request):
                return WorkerReturn(request.envelope_id,
                                    json.dumps({"source_names": sorted(s["path"] for s in request.folded["sources"])}),
                                    "Inventory only; no semantic interpretation")
        class InventoryAudit:
            def audit(self, root, folded, artifact):
                actual = json.loads(artifact)
                expected = sorted(s["path"] for s in folded["sources"])
                return {"verdict": "PASS" if actual == {"source_names": expected} else "FAIL",
                        "findings": [], "scope": "Exact inventory of requested sources"}
        self.engine.register_worker("inventory", Inventory())
        self.engine.register_auditor("inventory_audit", InventoryAudit())
        with self.assertRaises(ValueError):
            self.submit(worker="inventory")
        job = self.submit(worker="inventory", auditor="inventory_audit")
        result = self.engine.run(job)
        self.assertEqual(result["state"], "COMPLETED", result)
        self.assertIsNone(result["result"]["mechanical_audit"])
        self.assertEqual(json.loads(Path(result["result"]["artifact"]).read_text()), {"source_names": ["note.md"]})
        self.assertEqual(result["result"]["worker_return"]["notes"], "Inventory only; no semantic interpretation")

    def test_custom_auditor_cannot_override_source_drift(self):
        source = self.source
        class DriftWorker:
            def build(self, request):
                source.write_text("External ceramic source changed during work.\n")
                return WorkerReturn(request.envelope_id, "{}")
        class AlwaysPass:
            def audit(self, root, folded, artifact):
                return {"verdict": "PASS", "findings": []}
        self.engine.register_worker("drift", DriftWorker())
        self.engine.register_auditor("claim_pass", AlwaysPass())
        job = self.submit(worker="drift", auditor="claim_pass")
        result = self.engine.run(job)
        self.assertEqual(result["state"], "REJECTED", result)
        self.assertEqual(result["result"]["source_gate"]["verdict"], "FAIL")

    def test_protected_intent_and_append_only_events(self):
        job = self.submit()
        db = sqlite3.connect(self.engine.database)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE jobs SET envelope='{}' WHERE id=?", (job,))
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("DELETE FROM events WHERE job_id=?", (job,))
        finally:
            db.close()

    def test_reject_escape_sources_and_workspace_symlinks(self):
        with self.assertRaises(ValueError):
            self.engine.submit(self.root, ["../outside.txt"], "ceramic")
        (self.root / "escape").symlink_to(self.workspace, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.engine.submit(self.root, ["escape/tasks.sqlite3"], "ceramic")
        outside = self.base / "outside"
        outside.mkdir()
        (self.workspace / "jobs").rmdir()
        (self.workspace / "jobs").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            Engine(self.workspace)

    def test_retargeted_symlink_cannot_turn_output_into_source(self):
        engine = Engine(self.root / "runtime")
        alias = self.root / "alias.md"
        alias.symlink_to(self.source)
        job = engine.submit(self.root, ["alias.md"], "ceramic")
        runtime_file = engine.workspace / "generated.md"
        runtime_file.write_text("Ceramic generated output is not an original source.\n")
        alias.unlink()
        alias.symlink_to(runtime_file)
        result = engine.run(job)
        self.assertEqual(result["state"], "FAILED")
        self.assertIn("runtime outputs", result["error"])


if __name__ == "__main__":
    unittest.main()
