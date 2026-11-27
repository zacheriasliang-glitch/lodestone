"""Native-backed Windows read paths; no model or installed runtime changes."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "desktop/runtime/src"))
from lodestone_machinery import learning_context as learning
from lodestone_machinery import support_review as support
from lodestone_nurarihyon import portable_files


class WindowsReviewCustodyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="Loom review custody ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.memory = self.root / "memory"
        self.memory.mkdir()
        self.policy = {"schema": learning.POLICY_SCHEMA, "required": True,
                       "memory_workspace": str(self.memory), "purpose": "Read retained evidence",
                       "seed_handles": ["bezoar:" + "a" * 64]}
        self.policy_path = self.workspace / learning.POLICY_FILE
        self.policy_path.write_text(json.dumps(self.policy), encoding="utf-8")
        # Exercise the Windows routing on every host through that host's real
        # custody implementation. Native Windows additionally tests junctions.
        self.enterContext(mock.patch.object(learning, "_WINDOWS", True))
        self.enterContext(mock.patch.object(support, "_WINDOWS", True))

    def test_policy_read_and_absence_preserve_existing_contract(self):
        self.assertEqual(learning.read_learning_policy(self.workspace), self.policy)
        with mock.patch.object(learning._files, "read_bytes", side_effect=FileNotFoundError("observed file removed")):
            with self.assertRaises(FileNotFoundError):
                learning.read_learning_policy(self.workspace)
        self.policy_path.unlink()
        self.assertIsNone(learning.read_learning_policy(self.workspace))
        self.assertIsNone(learning.read_learning_policy(self.root / "missing-workspace"))

    def test_policy_and_support_reads_enforce_bounds_and_strict_json(self):
        self.policy_path.write_bytes(b" " * 32769)
        with self.assertRaisesRegex(ValueError, "byte bound"):
            learning.read_learning_policy(self.workspace)
        evidence = self.workspace / "evidence.json"
        evidence.write_bytes(b'{"claim":1,"claim":2}')
        with self.assertRaisesRegex(ValueError, "Duplicate JSON"):
            support._read_file(evidence, self.workspace, [0])
        evidence.write_bytes(b"12345")
        with self.assertRaisesRegex(ValueError, "byte bound"):
            support._read_file(evidence, self.workspace, [0], max_bytes=4)

    def test_support_identity_and_total_limit_are_retained(self):
        evidence = self.workspace / "answer.md"
        raw = b"Retained answer\n"
        evidence.write_bytes(raw)
        data, identity = support._read_file(evidence, self.workspace, [0], json_file=False)
        self.assertEqual(data, raw)
        self.assertEqual(identity, {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
        with self.assertRaisesRegex(ValueError, "byte bound"):
            support._read_file(evidence, self.workspace, [support.MAX_JSON], json_file=False)
        outside = self.root / "outside.json"
        outside.write_text("{}")
        with self.assertRaises(ValueError):
            support._read_file(outside, self.workspace, [0])

    def test_read_time_file_change_is_rejected_or_prevented(self):
        evidence = self.workspace / "evidence.json"
        evidence.write_bytes(b'{"claim":1}')
        original_read = portable_files.os.read
        attempted = []
        def mutate_after_read(fd, count):
            data = original_read(fd, count)
            if data and not attempted:
                attempted.append(True)
                try:
                    evidence.write_bytes(b'{"claim":2}')
                except PermissionError:
                    # Windows open_regular denies concurrent writer handles.
                    attempted.append("denied")
            return data
        with mock.patch.object(portable_files.os, "read", side_effect=mutate_after_read):
            try:
                value, _ = support._read_file(evidence, self.workspace, [0])
            except ValueError as exc:
                self.assertIn("identity changed", str(exc).lower())
            else:
                self.assertEqual(attempted, [True, "denied"])
                self.assertEqual(value, {"claim": 1})

    def assert_alias_refused(self, alias):
        with self.assertRaises((ValueError, OSError)):
            learning.read_learning_policy(alias)
        with self.assertRaises((ValueError, OSError)):
            support._read_file(alias / learning.POLICY_FILE, self.root, [0])
        with self.assertRaises((ValueError, OSError)):
            support._no_links(alias / learning.POLICY_FILE, self.root)
        with self.assertRaises((ValueError, OSError)):
            support.read_review_file(alias / learning.POLICY_FILE)

    @unittest.skipIf(os.name == "nt", "Windows junction coverage is separate")
    def test_parent_alias_is_not_resolved_away(self):
        alias = self.root / "alias"
        alias.symlink_to(self.workspace, target_is_directory=True)
        self.assert_alias_refused(alias)

    @unittest.skipUnless(os.name == "nt", "Native Windows junction test")
    def test_windows_junction_is_refused(self):
        alias = self.root / "junction"
        command = '"' + str(Path(os.environ["SystemRoot"]) / "System32/cmd.exe") + '" /d /v:off /s /c "mklink /J "' + str(alias) + '" "' + str(self.workspace) + '""'
        result = subprocess.run(command, shell=False, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.addCleanup(lambda: os.rmdir(alias) if alias.exists() else None)
        self.assert_alias_refused(alias)

    def test_dispatch_intent_read_uses_native_identity_custody(self):
        run_id = "b" * 32
        directory = self.workspace / "origami" / run_id
        directory.mkdir(parents=True)
        intent = directory / "intent.json"
        raw = b'{"schema_version":"0.2.0"}'
        intent.write_bytes(raw)
        prepared = {"run_handle": "bezoar:" + "c" * 64}
        dispatch = {"dispatch_handle": "nurarihyon:" + "d" * 64,
                    "learning_run_handle": prepared["run_handle"]}
        payload = {"run_handle": prepared["run_handle"], "provider_call_limit": 1,
                   "source": {"workspace": str(self.workspace), "run_id": run_id,
                              "intent_sha256": hashlib.sha256(raw).hexdigest()}}
        memory = SimpleNamespace(store=SimpleNamespace(relay=lambda *a, **kw: {"payload": payload}))
        with mock.patch.object(learning, "origami_learning_memory", return_value=memory):
            self.assertEqual(learning.check_origami_dispatch(self.workspace, prepared, dispatch,
                             run_id=run_id, call_limit=1), dispatch["dispatch_handle"])
            with mock.patch.object(learning._files, "read_bytes", side_effect=ValueError("File identity changed")):
                with self.assertRaisesRegex(ValueError, "identity changed"):
                    learning.check_origami_dispatch(self.workspace, prepared, dispatch, run_id=run_id, call_limit=1)


if __name__ == "__main__":
    unittest.main()
