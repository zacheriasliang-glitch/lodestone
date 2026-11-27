"""Failure and restoration contracts; native registry exercise runs only in CI."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import desktop_windows_policy_check as policy


class FakeRegistry:
    def __init__(self, value):
        self.value = deepcopy(value)
        self.writes = []

    def read(self):
        return deepcopy(self.value)

    def restore(self, value):
        self.writes.append(deepcopy(value))
        self.value = deepcopy(value)


class PolicyRestorationTests(unittest.TestCase):
    def test_exact_absence_type_and_value_restored_after_success_and_failure(self):
        originals = [{"present": False, "type": None, "value": None},
                     *({"present": True, "type": 4, "value": value}
                       for value in (0, 1, 3, 4294967295)),
                     {"present": True, "type": 3, "value": b"\x00\xff"},
                     {"present": True, "type": 7, "value": ["one", "two"]}]
        for original in originals:
            for fails in (False, True):
                with self.subTest(original=original, fails=fails):
                    registry, report = FakeRegistry(original), {}
                    def action():
                        registry.restore({"present": True, "type": 4, "value": 0})
                        registry.restore({"present": True, "type": 4, "value": 1})
                        if fails:
                            raise RuntimeError("authored installation failed")
                    if fails:
                        with self.assertRaisesRegex(RuntimeError, "installation failed"):
                            policy.restore_after(registry, report, action)
                    else:
                        policy.restore_after(registry, report, action)
                    self.assertEqual(registry.read(), original)
                    self.assertTrue(report["registry_restored"])
                    self.assertEqual(report["original_policy"], report["restored_policy"])
                    json.dumps(report)  # Binary registry values retain JSON-safe evidence.

    def test_wrong_restored_type_fails_even_with_equal_numeric_value(self):
        registry = FakeRegistry({"present": True, "type": 4, "value": 1})
        report = {}
        def wrong_type(value):
            registry.value = dict(value, type=11)
        registry.restore = wrong_type
        with self.assertRaisesRegex(ValueError, "original value and type"):
            policy.restore_after(registry, report, lambda: None)
        self.assertFalse(report["registry_restored"])
        self.assertIn("restoration_error", report)

    def test_restore_exception_is_not_reported_as_success(self):
        registry = FakeRegistry({"present": False, "type": None, "value": None})
        registry.restore = mock.Mock(side_effect=PermissionError("restore denied"))
        report = {}
        with self.assertRaisesRegex(PermissionError, "restore denied"):
            policy.restore_after(registry, report, lambda: None)
        self.assertFalse(report["registry_restored"])
        self.assertEqual(report["restoration_error"]["type"], "PermissionError")


class PolicyFixtureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.args = SimpleNamespace(source=str(self.root), source_commit="a" * 40,
            payload=str(self.root / "payload"), root=str(self.root / "installed"),
            app=str(self.root / "Loom.cmd"), cache=str(self.root / "cache"),
            evidence=str(self.root / "evidence"), release_id="b" * 64)

    def runner_environment(self):
        event = self.root / "event.json"
        event.write_text(json.dumps({"repository": {"private": False}}))
        return {"GITHUB_ACTIONS": "true", "RUNNER_OS": "Windows",
                "RUNNER_ENVIRONMENT": "github-hosted",
                "GITHUB_REPOSITORY": "zacheriasliang-glitch/lodestone",
                "GITHUB_EVENT_PATH": str(event), "RUNNER_TEMP": str(self.root)}

    def test_guard_refuses_other_platform_private_repository_and_self_hosted_runner(self):
        env = self.runner_environment()
        self.assertEqual(policy.require_public_runner(self.args, environ=env, platform="win32"), self.root)
        with self.assertRaisesRegex(ValueError, "hosted CI"):
            policy.require_public_runner(self.args, environ=env, platform="darwin")
        with self.assertRaisesRegex(ValueError, "hosted CI"):
            policy.require_public_runner(self.args, environ=dict(env, RUNNER_ENVIRONMENT="self-hosted"), platform="win32")
        Path(env["GITHUB_EVENT_PATH"]).write_text('{"repository":{"private":true}}')
        with self.assertRaisesRegex(ValueError, "hosted CI"):
            policy.require_public_runner(self.args, environ=env, platform="win32")

    def test_guard_refuses_existing_installation_outside_runner_temp(self):
        env = self.runner_environment()
        self.args.root = str(self.root.parent / "unrelated")
        with self.assertRaisesRegex(ValueError, "RUNNER_TEMP"):
            policy.require_public_runner(self.args, environ=env, platform="win32")

    def test_cache_requires_full_pinned_bytes_before_any_policy_change(self):
        payload, cache = Path(self.args.payload), Path(self.args.cache)
        payload.mkdir()
        cache.mkdir()
        data = b"verified archive fixture"
        sha = hashlib.sha256(data).hexdigest()
        (payload / "dependencies.windows-x86_64.lock.json").write_text(json.dumps(
            {"artifacts": [{"url": "https://example.invalid/archive", "size": len(data), "sha256": sha}]}))
        archive = cache / sha
        archive.write_bytes(data)
        report = policy.verify_cache(payload, cache)
        self.assertEqual(report, {"verified_objects": 1, "verified_bytes": len(data), "downloads": 0})
        archive.write_bytes(b"x" * len(data))
        with self.assertRaisesRegex(ValueError, "differs from pin"):
            policy.verify_cache(payload, cache)

    def exercise(self, *, output="", returncode=0, timeout=False, confirmed=True):
        exercise = object.__new__(policy.Exercise)
        exercise.args, exercise.report = self.args, {"commands": {}}
        exercise.evidence, exercise.owned = Path(self.args.evidence), []
        exercise.deadline = time.monotonic() + 1200
        job = mock.Mock()
        job.wait.side_effect = subprocess.TimeoutExpired("fixture", 1) if timeout else None
        job.wait.return_value = returncode
        job.stop.return_value = {"confirmed_stopped": confirmed}
        def factory(command, **kwargs):
            kwargs["stdout"].write(output)
            return job
        exercise.job_factory = mock.Mock(side_effect=factory)
        return exercise, job

    def test_refusal_preserves_actual_nonzero_code_and_marker(self):
        exercise, job = self.exercise(output=policy.REFUSAL + ": expected refusal\n", returncode=2)
        exercise.command("windows-policy-long-refusal", "authored command", refusal=True)
        result = policy.read(exercise.evidence / "windows-policy-long-refusal.json")
        self.assertEqual(result["returncode"], 2)
        self.assertEqual(result["status"], "EXPECTED_REFUSAL")
        self.assertEqual(result["refusal_marker"], policy.REFUSAL)
        job.stop.assert_called_once()
        job.close.assert_called_once()

    def test_nonzero_without_refusal_marker_remains_failed(self):
        exercise, job = self.exercise(output="unrelated installation failure", returncode=2)
        with self.assertRaisesRegex(ValueError, "did not refuse"):
            exercise.command("windows-policy-long-refusal", "authored command", refusal=True)
        result = policy.read(exercise.evidence / "windows-policy-long-refusal.json")
        self.assertEqual(result["returncode"], 2)
        self.assertEqual(result["status"], "FAILED")

    def test_timeout_stops_and_closes_owned_tree_before_registry_restoration(self):
        exercise, job = self.exercise(timeout=True)
        original = {"present": True, "type": 4, "value": 3}
        registry = FakeRegistry(original)
        events = []
        job.stop.side_effect = lambda: events.append("stop") or {"confirmed_stopped": True}
        job.close.side_effect = lambda: events.append("close")
        restore = registry.restore
        registry.restore = lambda value: events.append("restore") or restore(value)
        with self.assertRaises(subprocess.TimeoutExpired):
            policy.restore_after(registry, exercise.report,
                lambda: exercise.command("windows-policy-short-install", "authored command"))
        self.assertEqual(events, ["stop", "close", "restore"])
        self.assertTrue(exercise.report["registry_restored"])
        self.assertEqual(policy.read(exercise.evidence / "windows-policy-short-install.json")["status"], "FAILED")

    def test_unconfirmed_child_cleanup_fails_successful_command(self):
        exercise, job = self.exercise(confirmed=False)
        with self.assertRaisesRegex(ValueError, "did not stop"):
            exercise.command("windows-policy-enable", "authored command")
        result = policy.read(exercise.evidence / "windows-policy-enable.json")
        self.assertEqual(result["status"], "FAILED")
        self.assertFalse(result["owned_tree_cleanup"]["confirmed_stopped"])
        job.close.assert_called_once()

    def test_exhausted_work_budget_starts_no_child(self):
        exercise, job = self.exercise()
        exercise.deadline = time.monotonic() - 1
        with self.assertRaisesRegex(TimeoutError, "work budget"):
            exercise.command("late-command", "authored command")
        exercise.job_factory.assert_not_called()

    def test_failed_helper_keeps_prior_and_result_records(self):
        prior = self.root / "prior.json"
        result = self.root / "result.json"
        prior.write_text('{"previous_exists":true,"previous_value":0}')
        result.write_text('{"state":"FAILED","error":"authored refusal"}')
        output = "LODESTONE_LONG_PATH_RECEIPT=" + str(prior) + "\nLODESTONE_LONG_PATH_RESULT=" + str(result) + "\n"
        exercise, _ = self.exercise(output=output, returncode=5)
        with mock.patch.dict(os.environ, {"SystemRoot": r"C:\Windows"}), self.assertRaises(subprocess.CalledProcessError):
            exercise.enable_helper(self.root / "helper.cmd")
        self.assertEqual(policy.read(exercise.evidence / "windows-policy-helper-prior.json"), policy.read(prior))
        self.assertEqual(policy.read(exercise.evidence / "windows-policy-helper-result.json")["state"], "FAILED")

    def test_cleanup_retains_failed_learning_state_without_active_install(self):
        exercise, _ = self.exercise()
        fixture = exercise.own(self.root / "owned")
        record = fixture / "learning" / "failed-attempt.json"
        record.parent.mkdir()
        record.write_text('{"status":"FAILED","correction":"retry"}')
        unrelated = self.root / "unrelated.txt"
        unrelated.write_text("preserve")
        exercise.cleanup()
        retained = exercise.evidence / "windows-policy-short-state/learning/failed-attempt.json"
        self.assertEqual(json.loads(retained.read_text())["status"], "FAILED")
        self.assertFalse(fixture.exists())
        self.assertEqual(unrelated.read_text(), "preserve")

    def test_cleanup_refuses_changed_owned_root_identity(self):
        exercise, _ = self.exercise()
        fixture = exercise.own(self.root / "owned")
        path, device, inode = exercise.owned[0]
        exercise.owned[0] = (path, device, inode + 1)
        with self.assertRaisesRegex(ValueError, "cleanup failed"):
            exercise.cleanup()
        self.assertTrue(fixture.exists())
        self.assertFalse(exercise.report["cleanup"]["owned_fixtures_removed"])

    def arguments(self):
        return [value for key, item in vars(self.args).items()
                for value in ("--" + key.replace("_", "-"), item)]

    def test_refused_host_retains_failed_bound_report_without_opening_registry(self):
        with mock.patch.object(policy, "require_public_runner", side_effect=ValueError("host refused")), \
                mock.patch.object(policy, "NativeRegistry") as registry, mock.patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(policy.main(self.arguments()), 1)
        registry.assert_not_called()
        report = policy.read(Path(self.args.evidence) / "windows-long-path-policy.json")
        self.assertEqual(report["status"], "FAILED")
        self.assertEqual(report["source_commit"], self.args.source_commit)
        self.assertEqual(report["release_id"], self.args.release_id)
        self.assertFalse(report["registry_restored"])

    def test_batch_quoting_preserves_special_helper_folder_as_one_quoted_value(self):
        helper = r"C:\runner\O'Brien & (paths)\Enable Windows Long Paths.cmd"
        with mock.patch.dict(os.environ, {"SystemRoot": r"C:\Windows"}):
            command = policy.batch_command(helper, ["--yes"])
            self.assertTrue(command.endswith('/s /c ""' + helper + '" "--yes""'))
            for unsupported in ("%", "!", '"', "\n"):
                with self.subTest(character=unsupported), self.assertRaises(ValueError):
                    policy.batch_command(helper + unsupported)


if __name__ == "__main__":
    unittest.main()
