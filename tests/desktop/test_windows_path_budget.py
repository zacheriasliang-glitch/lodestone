"""Path evidence must cover exact pins and count native UTF-16 units."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("windows_path_inventory_test", ROOT / "tools/desktop_windows_path_inventory.py")
inventory = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(inventory)


class WindowsPathBudgetTests(unittest.TestCase):
    def test_budget_binds_every_pinned_archive_and_wheel(self):
        raw = (ROOT / "desktop/dependencies.windows-x86_64.lock.json").read_bytes()
        lock = json.loads(raw)
        budget = json.loads((ROOT / "desktop/windows-path-budget.json").read_bytes())
        self.assertEqual(budget["lock_sha256"], hashlib.sha256(raw).hexdigest())
        expected_wheels = {role + "/" + wheel["name"]:wheel
            for role, environment in lock["environments"].items() for wheel in environment["wheels"]}
        for kind, expected in (("archives", lock["artifacts"]), ("wheels", expected_wheels)):
            self.assertEqual(set(budget["inputs"][kind]), set(expected))
            for name, pin in expected.items():
                evidence = budget["inputs"][kind][name]
                self.assertTrue(evidence["full_hash_verified"])
                self.assertGreater(evidence["files"], 0)
                for field in ("sha256", "size", "filename"):
                    self.assertEqual(evidence[field], pin[field])
        for scope in budget["scopes"].values():
            for kind in ("file", "directory"):
                template = scope["longest_" + kind + "_template"]
                self.assertEqual(scope["max_" + kind + "_relative_utf16"], inventory.units(inventory.expanded(template)))

    def test_unicode_separator_and_retained_failure_paths_are_counted(self):
        summary = inventory.summarize(["data/雪🧪.json", "worker.interrupted-{time_ns}/file"])
        expected = "\\worker.interrupted-" + "x" * 20 + "\\file"
        self.assertEqual(summary["max_file_relative_utf16"], len(expected))
        unicode_summary = inventory.summarize(["data/雪🧪.json"])
        self.assertEqual(unicode_summary["max_file_relative_utf16"], len("\\data\\雪🧪.json") + 1)

    def test_changed_input_and_unsafe_member_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input"
            path.write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "hash differs"):
                inventory.verified(path, {"size":7, "sha256":hashlib.sha256(b"correct").hexdigest()})
        for name in ("../escape", "/absolute", "C:/absolute", "parent\\child", "nul\x00name"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                inventory.safe_member(name)

    def test_default_root_requires_managed_long_paths_but_bootstrap_is_legacy_safe(self):
        budget = json.loads((ROOT / "desktop/windows-path-budget.json").read_bytes())
        example = budget["default_root_example"]
        self.assertEqual(example["utf16_units"], inventory.units(example["root"]))
        self.assertGreater(example["longest_managed_file_utf16"], 259)
        self.assertLessEqual(example["longest_bootstrap_file_utf16"], 259)
        self.assertLessEqual(len("C:\\lp-12345678") + budget["scopes"]["root"]["max_file_relative_utf16"], 259)


if __name__ == "__main__":
    unittest.main()
