"""Deterministic synthetic reconciliation checks.

Engineering contracts, not unseen learning data or real historical verdicts.
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
from lodestone_machinery import bezoar_reconciliation as reconcile
from lodestone_machinery.bezoar import Plumber
from lodestone_machinery.records import RecordStore, canonical


RECEIPT = "receipts/20260101T000000Z__fixture__RCP-20260101T000000Z-0123456789ab.md"


def source_text(predecessors=()):
    document = {"receipt_id": "RCP-20260101T000000Z-0123456789ab",
                "operation_name": "fixture", "mode": "BOGUS",
                "receipt_directory_path_set_before_creation": list(predecessors)}
    return "# Fixture\n\n```json\n" + json.dumps(document) + "\n```\n"


def registry_text(state="HELD"):
    citation = "    cleared_by: fixture-review\n" if state == "CLEARED" else ""
    return ("# Captured fixture registry\n\n## BZ-0001\n\n    kind: SEED\n"
            "    law: Y2_ONE_ORGAN\n    subject: " + RECEIPT + "\n"
            "    first_seen: fixture\n    state: " + state + "\n" + citation +
            "    clears_when: a separate reviewed correction establishes a legal mode\n\n"
            "The captured fixture declares a mode outside the supported set.\n")


def file_record(text):
    return {"text": text, "sha256": reconcile.digest(text.encode("utf-8"))}


def fixture():
    receipt = source_text()
    failure = {"schema_version": "0.2.0", "run_id": "fixture-failure", "status": "BLOCKED",
               "reason": "PROVIDER_UNDECLARED", "fold": None,
               "scope": {"sources": [{"path": RECEIPT, "sha256": reconcile.digest(receipt.encode())}]},
               "authority": "NONE: output does not authorize actions"}
    return {"schema": reconcile.SOURCE_SCHEMA, "corpus": "explicit-complete-fixture",
            "receipt_inventory": [RECEIPT],
            "files": {RECEIPT: file_record(receipt), "docs/bezoar.md": file_record(registry_text()),
                      "results/origami.json": file_record(json.dumps(failure))},
            "origami_results": ["results/origami.json"],
            "saved_lineage": {"verdict": "PASS", "findings": []}}


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="bezoar-reconciliation-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def evaluate(self, value, name="scratch"):
        raw = canonical(value)
        return reconcile.evaluate(raw, expected_sha256=reconcile.digest(raw), scratch=self.root / name)

    def test_recomputes_false_saved_verdict_and_retains_origami_failure(self):
        value = fixture()
        before = canonical(value)
        report = self.evaluate(value)
        self.assertEqual(report["lineage"]["verdict"], "VIOLATION")
        self.assertEqual([x["law"] for x in report["lineage"]["findings"]], ["Y2_ONE_ORGAN"])
        self.assertFalse(report["saved_lineage_matches"])
        self.assertEqual(report["registry_verdict"], "PASS")
        self.assertEqual(report["holder_matches"][0]["holders"], [{"id": "BZ-0001", "kind": "SEED", "state": "HELD"}])
        self.assertEqual(report["origami_results"][0]["result"], json.loads(value["files"]["results/origami.json"]["text"]))
        self.assertEqual(report["authority"], "NONE")
        self.assertFalse(report["repair_performed"])
        self.assertEqual(canonical(value), before)

    def test_changed_registry_does_not_clear_a_still_emitted_finding(self):
        value = fixture()
        value["files"]["docs/bezoar.md"] = file_record(registry_text("CLEARED"))
        report = self.evaluate(value)
        self.assertEqual(report["registry_verdict"], "ATTENTION")
        self.assertEqual([x["law"] for x in report["registry_findings"]], ["N5_COVERAGE"])
        self.assertIn("still emitted", report["registry_findings"][0]["message"])

    def test_incomplete_predecessor_closure_refuses_before_record_publication(self):
        value = fixture()
        receipt = source_text(["receipts/absent.md"])
        value["files"][RECEIPT] = file_record(receipt)
        failure = json.loads(value["files"]["results/origami.json"]["text"])
        failure["scope"]["sources"][0]["sha256"] = reconcile.digest(receipt.encode())
        value["files"]["results/origami.json"] = file_record(json.dumps(failure))
        store = RecordStore(self.root / "records")
        raw = canonical(value)
        with self.assertRaisesRegex(ValueError, "predecessor closure incomplete"):
            reconcile.publish_reconciliation(store, raw, expected_sha256=reconcile.digest(raw), scratch=self.root / "partial")
        self.assertEqual(list((store.workspace / "organs").rglob("*.json")), [])
        self.assertEqual((self.root / "partial" / RECEIPT).read_text(), receipt)

    def test_wrong_hash_inventory_and_path_refuse_before_scratch_creation(self):
        variants = []
        value = fixture(); variants.append((value, "0" * 64))
        value = fixture(); value["receipt_inventory"] = []; variants.append((value, None))
        value = fixture(); value["files"]["../outside"] = file_record("inert"); variants.append((value, None))
        value = fixture(); value["files"]["oversize.txt"] = file_record("x" * 65536); variants.append((value, None))
        for index, (value, expected) in enumerate(variants):
            with self.subTest(index=index):
                raw = canonical(value); destination = self.root / ("refused-" + str(index))
                with self.assertRaises(ValueError):
                    reconcile.evaluate(raw, expected_sha256=expected or reconcile.digest(raw), scratch=destination)
                self.assertFalse(destination.exists())

    def test_missing_origami_source_binding_refuses(self):
        value = fixture()
        failed = json.loads(value["files"]["results/origami.json"]["text"])
        failed["scope"]["sources"][0]["path"] = "sources/not-captured.md"
        value["files"]["results/origami.json"] = file_record(json.dumps(failed))
        with self.assertRaisesRegex(ValueError, "Origami evidence closure"):
            self.evaluate(value)
        self.assertFalse((self.root / "scratch").exists())

    def test_relocated_records_recompute_without_original_capture_paths(self):
        raw = canonical(fixture())
        plumber = Plumber(self.root / "original-records")
        handle = plumber.reconcile_captured(raw, expected_sha256=reconcile.digest(raw), scratch=self.root / "first-capture")
        shutil.copytree(plumber.records.workspace, self.root / "copied-records")
        copied = Plumber(self.root / "copied-records")
        checked = copied.read_reconciliation(handle, scratch=self.root / "second-capture")
        self.assertEqual(checked["verification"], "RECOMPUTED_FROM_RETAINED_BYTES")
        self.assertEqual(checked["report"]["lineage"]["verdict"], "VIOLATION")
        self.assertNotIn(str(self.root), canonical(copied.records.read(handle)).decode())
        self.assertTrue((self.root / "first-capture" / RECEIPT).exists())

    def test_forged_content_addressed_result_is_not_computation_evidence(self):
        raw = canonical(fixture())
        store = RecordStore(self.root / "records")
        handle = reconcile.publish_reconciliation(store, raw, expected_sha256=reconcile.digest(raw), scratch=self.root / "producer")
        record = store.read(handle)
        payload = copy.deepcopy(record["payload"])
        payload["report"]["registry_verdict"] = "ATTENTION"
        forged = store.publish("bezoar", "reconciliation", payload, links=record["links"])
        with self.assertRaisesRegex(ValueError, "differs from recomputed"):
            reconcile.read_reconciliation(store, forged, scratch=self.root / "reader")
        self.assertEqual(store.read(handle), record)


if __name__ == "__main__":
    unittest.main()
