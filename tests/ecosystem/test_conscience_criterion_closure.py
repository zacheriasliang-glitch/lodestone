"""Focused portable evidence for the adopted Conscience criteria.

These fixtures exercise advisory assessment only. They do not establish a
sandbox, grant authority, run a judged command, or reopen the historical G5 run.
"""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]


class CriterionClosureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="conscience criteria ")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.home = self.base / "home"
        self.cwd = self.home / "current directory"
        self.workspace = self.home / "workspace"
        self.cwd.mkdir(parents=True)
        self.workspace.mkdir()
        # Match the existing history fixtures: isolate Python's home reader
        # and pass workspace/cwd explicitly; do not change the process HOME.
        home_path = mock.patch.object(Path, "home", return_value=self.home)
        home_path.start()
        self.addCleanup(home_path.stop)

        spec = importlib.util.spec_from_file_location(
            "_conscience_criterion_closure", ROOT / "tools/ecosystem/conscience.py")
        self.c = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.c)
        self.policy_path = self.home / "policy-copy.json"
        self.policy_bytes = (ROOT / "tools/ecosystem/conscience-policy.json").read_bytes()
        self.policy_path.write_bytes(self.policy_bytes)
        self.policy = self.c.load_policy(self.policy_path)

    def assess(self, argv, *, policy=None):
        return self.c.assess(
            argv, cwd=self.cwd, actor="local-user",
            policy=self.policy if policy is None else policy,
            workspace=self.workspace, woken=True, temp={"state": "WARM"})

    def test_rule_path_pairs_are_deduplicated_for_overlapping_ownership_rules(self):
        # The Git directory, working directory and mutation target all name
        # the same foreign-owned path, so duplicate findings are non-vacuous.
        self.policy["writers"] = [{"prefix": str(self.cwd), "owner": "another-user"}]
        packet = self.assess(["git", "-C", str(self.cwd), "commit"])
        self.assertEqual(packet["verdict"], "REFUSE")
        self.assertTrue(packet["rules"])
        keys = [(rule["rule"], rule.get("directory") or rule.get("path"))
                for rule in packet["rules"]]
        self.assertIn(("one-writer-per-path", str(self.cwd)), keys)
        self.assertEqual(len(keys), len(set(keys)))

    def test_golden_schedule_returns_to_zero_after_complete_cycle(self):
        names = ["check" + str(index) for index in range(8)]
        seen = []
        cursor = 0
        for _ in range(4):
            chosen, cursor = self.c.golden_schedule(names, cursor, 2)
            seen.extend(chosen)
        self.assertEqual(sorted(seen), sorted(names))
        self.assertEqual(cursor, 0)

    def test_same_policy_copy_and_inputs_produce_identical_assessment_in_temporary_home(self):
        source = self.cwd / "source.txt"
        source.write_bytes(b"retained fixture\n")
        destination = self.workspace / "copied.txt"
        command = ["cp", str(source), str(destination)]
        first = self.assess(command, policy=self.c.load_policy(self.policy_path))
        second = self.assess(command, policy=self.c.load_policy(self.policy_path))
        self.assertEqual(first, second)
        self.assertEqual(first["verdict"], "ALLOW")
        self.assertEqual(Path.home(), self.home)
        self.assertEqual(self.policy_path.read_bytes(), self.policy_bytes)
        self.assertEqual(source.read_bytes(), b"retained fixture\n")
        self.assertFalse(destination.exists())

    def test_allow_classification_neither_executes_command_nor_grants_authority(self):
        source = self.cwd / "source.txt"
        source.write_bytes(b"retained fixture\n")
        destination = self.workspace / "copied.txt"
        with mock.patch.object(self.c.subprocess, "Popen", side_effect=AssertionError("assessment launched a process")) as popen:
            with mock.patch.object(self.c.subprocess, "run", side_effect=AssertionError("assessment ran a command")) as run:
                packet = self.assess(["cp", str(source), str(destination)])
        self.assertEqual(packet["verdict"], "ALLOW")
        self.assertEqual(packet["scope"],
                         "Intent judged from the command line; not a sandbox; no permission granted")
        popen.assert_not_called()
        run.assert_not_called()
        self.assertFalse(destination.exists())
        self.assertEqual(source.read_bytes(), b"retained fixture\n")
        self.assertEqual(self.policy_path.read_bytes(), self.policy_bytes)


if __name__ == "__main__":
    unittest.main()
