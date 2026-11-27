"""Captured validation is read-only and never opens an attributed workspace."""
import hashlib
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "desktop/runtime/src"))
from lodestone_machinery import bezoar_learning as learning
from lodestone_machinery.records import canonical
from lodestone_machinery.yggdrasil_learning import CapturedLearningMemory, validate_learning_records


class ArchiveCustody(unittest.TestCase):
    def record(self):
        return {"schema":"organ-record/v1", "owner":"bezoar", "kind":"learning-evidence", "links":[],
                "payload":{"schema":"bezoar-learning-evidence/v1", "authority":"NONE",
                           "source":"captured-fixture", "event_id":"one", "observed_at":"2026-11-27T00:00:00Z",
                           "text":"Original observation", "attribution":"Scripted test only"}}

    def test_actual_capture_validator_uses_no_filesystem_custody_and_rechecks_later(self):
        from lodestone_machinery.yggdrasil_runtime import _CapturedRecords
        record = self.record()
        handle = "bezoar:" + hashlib.sha256(canonical(record)).hexdigest()
        store = _CapturedRecords({handle: ("captured-node", record)})
        with patch.object(learning, "guarded_directory", side_effect=AssertionError("No filesystem for captured validation")):
            checked = validate_learning_records(store)
            self.assertEqual(checked.read_evidence(handle)["text"], "Original observation")
            self.assertFalse(hasattr(checked, "_validated_records"))
            store.records[handle][1]["payload"]["schema"] = "foreign"
            with self.assertRaises(ValueError):
                checked.read_evidence(handle)
            self.assertFalse(hasattr(checked, "_validated_records"))

    def test_capture_cannot_publish_even_when_passed_a_writable_store(self):
        store = Mock()
        memory = CapturedLearningMemory(store)
        with self.assertRaisesRegex(ValueError, "read-only"):
            memory._publish("evidence", self.record()["payload"])
        store.publish.assert_not_called()

    def test_normal_memory_cannot_omit_its_workspace_guard(self):
        memory = object.__new__(learning.LearningMemory)
        memory.workspace, memory.store = None, Mock()
        with self.assertRaisesRegex(ValueError, "explicit read-only capture"):
            memory.read_evidence("bezoar:" + "1" * 64)
        memory.store.relay.assert_not_called()
        with self.assertRaisesRegex(ValueError, "read-only"):
            memory._publish("evidence", {})
        memory.store.publish.assert_not_called()


if __name__ == "__main__":
    unittest.main()
