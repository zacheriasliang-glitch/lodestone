import json
from pathlib import Path
import tempfile
import unittest

from lodestone_machinery import Engine, RecordStore
from lodestone_machinery.records import RecordUnavailable, route_result


class RecordTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.store = RecordStore(self.base / "work")

    def tearDown(self):
        self.temp.cleanup()

    def decision(self, verdict, *, envelope="task-a", submission=None):
        submission = submission or self.store.publish("builder", "worker_return", {"artifact": "same answer"})
        audit = self.store.publish("auditor", "audit_result", {
            "envelope_id": envelope, "submission": submission, "verdict": verdict}, links=[submission])
        measurement = self.store.publish("ruler", "measurement", {
            "envelope_id": envelope, "measurement": {"verdict": "PASS"}}, links=[audit])
        return submission, audit, measurement

    def test_same_artifact_different_auditor_output_changes_orchestrator_action(self):
        expected = {"PASS": ("COMPLETED", "RETURN_OUTPUT"),
                    "FAIL": ("REJECTED", "REVISE_OUTPUT"),
                    "UNKNOWN": ("BLOCKED", "RETRIEVE_EVIDENCE")}
        subjects = set()
        for verdict, (state, action) in expected.items():
            submission, audit, measurement = self.decision(verdict)
            subjects.add(submission)
            result = route_result(self.store, audit, measurement, envelope_id="task-a",
                                  submission_handle=submission)
            self.assertEqual((result["state"], result["next_action"]), (state, action))
            self.assertEqual(result["authority"], "NONE")
        self.assertEqual(len(subjects), 1)

    def test_matching_verdict_for_different_task_cannot_be_reused(self):
        submission, audit, measurement = self.decision("PASS", envelope="other-task")
        with self.assertRaisesRegex(ValueError, "custody"):
            route_result(self.store, audit, measurement, envelope_id="task-a", submission_handle=submission)

    def test_wrong_producer_cannot_supply_the_builder_contract(self):
        wrong = self.store.publish("selection", "source_fold", {"artifact": "same answer"})
        submission, audit, measurement = self.decision("PASS", submission=wrong)
        with self.assertRaisesRegex(ValueError, "contract mismatch"):
            route_result(self.store, audit, measurement, envelope_id="task-a", submission_handle=submission)

    def test_changed_submission_after_audit_blocks_routing(self):
        submission, audit, measurement = self.decision("PASS")
        self.store.path(submission).write_text('{"artifact":"different answer"}')
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            route_result(self.store, audit, measurement, envelope_id="task-a", submission_handle=submission)

    def test_missing_owner_record_is_visible_in_catalog_and_route(self):
        submission, audit, measurement = self.decision("PASS")
        self.store.path(submission).unlink()
        self.assertEqual(self.store.catalog()["verdict"], "ATTENTION")
        with self.assertRaises(FileNotFoundError):
            route_result(self.store, audit, measurement, envelope_id="task-a", submission_handle=submission)
        with self.assertRaises(RecordUnavailable) as observed:
            self.store.verify_links([measurement])
        self.assertEqual(observed.exception.handle, submission)

    def test_owner_storage_is_canonical_and_catalog_only_has_pointers(self):
        handle = self.store.publish("builder", "worker_return", {"artifact": "retain literal evidence"})
        self.assertEqual(self.store.path(handle).parent, self.base / "work/organs/builder/records")
        reopened = RecordStore(self.base / "work")
        self.assertEqual(reopened.relay("auditor", handle, owner="builder", kind="worker_return")
                         ["payload"]["artifact"], "retain literal evidence")
        catalog = reopened.catalog()
        self.assertFalse(catalog["contains_payload_copies"])
        self.assertNotIn("retain literal evidence", json.dumps(catalog))
        self.assertFalse((self.base / "work/receipts").exists())

    def test_immutable_publish_preserves_corruption_for_owner(self):
        handle = self.store.publish("builder", "worker_return", {"artifact": "original"})
        self.assertEqual(self.store.publish("builder", "worker_return", {"artifact": "original"}), handle)
        self.store.path(handle).write_bytes(b"damaged")
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            self.store.publish("builder", "worker_return", {"artifact": "original"})
        self.assertEqual(self.store.path(handle).read_bytes(), b"damaged")

    def test_path_and_producer_contract_mismatches_reject(self):
        handle = self.store.publish("builder", "worker_return", {"artifact": "literal"})
        for invalid in ("../builder:x", "builder:../../outside", "builder:" + "0" * 63):
            with self.assertRaises(ValueError):
                self.store.read(invalid)
        with self.assertRaisesRegex(ValueError, "contract mismatch"):
            self.store.relay("orchestrator", handle, owner="auditor", kind="audit_result")
        with self.assertRaises(ValueError):
            self.store.publish("../builder", "worker_return", {})

    def test_symlinked_record_cannot_redirect_peer_read(self):
        handle = self.store.publish("builder", "worker_return", {"artifact": "literal"})
        path = self.store.path(handle)
        outside = self.base / "outside.json"
        outside.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.store.read(handle)
        self.assertEqual(self.store.catalog()["verdict"], "ATTENTION")


class EngineRecordTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.sources = self.base / "sources"
        self.sources.mkdir()
        (self.sources / "note.md").write_bytes(b"Ceramic evidence retains failure.\r\n")
        self.engine = Engine(self.base / "work")

    def tearDown(self):
        self.temp.cleanup()

    def run_job(self, query="ceramic", **kwargs):
        job = self.engine.submit(self.sources, ["note.md"], query, **kwargs)
        return self.engine.run(job)

    def test_actual_engine_consumes_owner_records_and_keeps_unknown_distinct(self):
        passed = self.run_job()
        blocked = self.run_job("unfindable")
        self.assertEqual(passed["state"], "COMPLETED")
        self.assertEqual(blocked["state"], "BLOCKED")
        self.assertEqual(passed["result"]["next_action"], "RETURN_OUTPUT")
        self.assertEqual(blocked["result"]["next_action"], "RETRIEVE_EVIDENCE")
        for result in (passed, blocked):
            handles = result["result"]["organ_records"]
            self.assertEqual(set(handles), {"source_binding", "selection", "builder", "auditor",
                                            "ruler", "residuals", "orchestrator"})
            decision = self.engine.records.read(handles["orchestrator"])["payload"]
            self.assertEqual(decision["state"], result["state"])
            submitted = self.engine.records.read(handles["builder"])["payload"]["artifact"].encode()
            self.assertEqual(submitted, Path(result["result"]["artifact"]).read_bytes())
        self.assertEqual(self.engine.records.catalog()["verdict"], "OK")

    def test_changed_stored_builder_output_is_rejected_before_auditor_runs(self):
        called = []
        class Auditor:
            def audit(self, root, folded, artifact):
                called.append(artifact)
                return {"verdict": "PASS", "findings": []}
        self.engine.register_auditor("watch", Auditor())
        publish = self.engine.records.publish
        def damage(owner, kind, payload, **kwargs):
            handle = publish(owner, kind, payload, **kwargs)
            if owner == "builder":
                self.engine.records.path(handle).write_text("changed after publication")
            return handle
        self.engine.records.publish = damage
        result = self.run_job(auditor="watch")
        self.assertEqual(result["state"], "FAILED")
        self.assertIn("identity mismatch", result["error"])
        self.assertEqual(called, [])

    def test_missing_record_routes_maintenance_to_owner_without_rewriting_history(self):
        result = self.run_job()
        handle = result["result"]["organ_records"]["builder"]
        path = self.engine.records.path(handle)
        path.unlink()
        maintained = self.engine.maintain()
        own = [f for f in maintained["findings"] if f.get("owner") == "builder"]
        self.assertEqual(own[0]["next_action"], "ASK_OWNER")
        self.assertEqual(len(maintained["findings"]), 1)
        recovered = self.engine.recover(result["job_id"])
        self.assertEqual(recovered["verdict"], "BLOCKED")
        self.assertEqual([item["owner"] for item in recovered["unavailable"]], ["builder"])
        self.assertFalse(path.exists())
        self.assertEqual(self.engine.status(result["job_id"])["state"], "COMPLETED")


if __name__ == "__main__":
    unittest.main()
