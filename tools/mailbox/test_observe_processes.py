import json
import contextlib
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import observe_processes as observer


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, duration):
        self.now += duration


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output = Path(self.directory.name) / "observations.jsonl"
        self.ready = Path(self.directory.name) / "ready.json"
        self.clock = Clock()
        self.root = observer.parse_processes(b"100 1 100 /usr/bin/python3\n")

    def run_observer(self, samples, seconds=2, ps_timeout=1.0):
        with mock.patch.object(observer.time, "monotonic", self.clock.monotonic), \
                mock.patch.object(observer.time, "sleep", self.clock.sleep), \
                mock.patch.object(observer, "read_processes", side_effect=samples):
            return observer.observe(100, self.output, self.ready, seconds, ps_timeout)

    def records(self):
        return [json.loads(line) for line in self.output.read_text().splitlines()]

    def test_current_ancestry_includes_separate_group_and_redacts_paths(self):
        rows = observer.parse_processes(
            b"100 1 100 /Users/private-name/launcher\n"
            b"200 100 200 /usr/local/bin/codex\n"
            b"201 200 200 /private/package/codex-code-mode-host\n"
            b"300 1 200 /unrelated/process\n")
        selected = observer.descendants(rows, 100)
        self.assertEqual([row["pid"] for row in selected], [100, 200, 201])
        self.assertEqual(selected[-1]["comm_basename"], "codex-code-mode-host")
        self.assertNotIn(b"/", observer.encode(selected))
        rows[201]["ppid"] = 1
        self.assertEqual([row["pid"] for row in observer.descendants(rows, 100)], [100, 200])
        self.assertEqual(observer.descendants(rows, 999), [])

    def test_ready_and_root_disappearance_with_measurable_cadence(self):
        self.assertEqual(self.run_observer([self.root, self.root, {}]), 0)
        self.assertEqual(json.loads(self.ready.read_text())["root_pid"], 100)
        records = self.records()
        samples = [row for row in records if row["type"] == "sample"]
        self.assertEqual([row["elapsed_seconds"] for row in samples], [0, .25, .5])
        self.assertEqual([row["sample"] for row in samples], [0, 1, 2])
        self.assertTrue(all("utc" in row and "duration_seconds" in row for row in samples))
        self.assertEqual(records[-1]["reason"], "root_not_seen")
        self.assertEqual(records[0]["ps_timeout_seconds"], 1.0)

    def test_configured_ps_timeout_respects_remaining_observation_budget(self):
        deadlines = []
        samples = iter([self.root, self.root, {}])
        def capture(timeout):
            deadlines.append(timeout)
            return next(samples)
        self.assertEqual(self.run_observer(capture, seconds=3.2, ps_timeout=3), 0)
        for actual, expected in zip(deadlines, [3, 2.95, 2.7]):
            self.assertAlmostEqual(actual, expected)
        self.assertEqual(len(deadlines), 3)
        self.assertEqual(self.records()[0]["ps_timeout_seconds"], 3)

    def test_invalid_ps_limits_reject_before_files_or_processes(self):
        for limit in (0, -1, 3.01, float('inf'), float('nan')):
            with self.assertRaises(ValueError):
                observer.observe(100, self.output, self.ready, ps_timeout=limit)
            with mock.patch.object(observer.subprocess, 'Popen') as spawn:
                with self.assertRaises(ValueError):
                    observer.read_processes(limit)
                spawn.assert_not_called()
            with contextlib.redirect_stderr(io.StringIO()), \
                    mock.patch.object(observer, 'observe') as observe:
                with self.assertRaises(SystemExit) as error:
                    observer.main(['--root-pid', '100', '--output', str(self.output),
                                   '--ready-file', str(self.ready), '--ps-timeout', str(limit)])
                self.assertEqual(error.exception.code, 2)
                observe.assert_not_called()
        self.assertFalse(self.output.exists())
        self.assertFalse(self.ready.exists())

    def test_missing_root_never_acknowledges_ready(self):
        self.assertEqual(self.run_observer([{}]), 2)
        self.assertFalse(self.ready.exists())
        self.assertEqual(self.records()[-1]["reason"], "root_missing_at_start")

    def test_sampling_error_is_distinct_from_missing_root(self):
        self.assertEqual(self.run_observer([observer.ObservationError("ps_timeout")]), 2)
        self.assertFalse(self.ready.exists())
        self.assertEqual(self.records()[-2]["status"], "failed")
        self.assertEqual(self.records()[-1]["reason"], "ps_timeout")

    def test_deadline_does_not_require_signalling_processes(self):
        self.assertEqual(self.run_observer([self.root] * 4, seconds=.5), 2)
        self.assertEqual(self.records()[-1]["reason"], "deadline")
        self.assertEqual(self.clock.now, .5)

    def test_output_cap_is_bounded_and_has_explicit_terminal_record(self):
        with mock.patch.object(observer, "MAX_LOG_BYTES", 1500):
            self.assertEqual(self.run_observer([self.root] * 4), 2)
        self.assertLessEqual(self.output.stat().st_size, 1500)
        self.assertEqual(self.records()[-1]["reason"], "observation_output_limit")

    def test_existing_output_and_readiness_are_never_overwritten(self):
        self.output.write_bytes(b"existing\n")
        with self.assertRaises(FileExistsError):
            self.run_observer([self.root])
        self.assertEqual(self.output.read_bytes(), b"existing\n")
        self.output.unlink()
        self.ready.write_bytes(b"existing\n")
        self.assertEqual(self.run_observer([self.root]), 2)
        self.assertEqual(self.ready.read_bytes(), b"existing\n")
        self.assertEqual(self.records()[-1]["reason"], "ready_file_failed")

    def test_invalid_or_duplicate_ps_records_fail_closed(self):
        for raw in (b"100 1 invalid path\n", b"100 1 100 x\n100 1 100 y\n"):
            with self.assertRaises(observer.ObservationError):
                observer.parse_processes(raw)

    def with_fake_ps(self, script):
        real_popen = subprocess.Popen
        children = []
        def spawn(command, **kwargs):
            self.assertEqual(command, ["/bin/ps", "-axo", "pid=,ppid=,pgid=,comm="])
            self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
            self.assertIs(kwargs["stdout"], subprocess.PIPE)
            self.assertIs(kwargs["stderr"], subprocess.DEVNULL)
            child = real_popen([sys.executable, "-c", script], **kwargs)
            children.append(child)
            return child
        return mock.patch.object(observer.subprocess, "Popen", side_effect=spawn), children

    def test_ps_uses_metadata_only_and_bounded_pipe_capture(self):
        patch, children = self.with_fake_ps("print('100 1 100 /private/codex')")
        with patch:
            self.assertEqual(observer.read_processes(1)[100]["comm_basename"], "codex")
        self.assertEqual(children[0].returncode, 0)

    def test_three_second_limit_allows_a_capture_longer_than_one_second(self):
        patch, children = self.with_fake_ps(
            "import time; time.sleep(1.1); print('100 1 100 /private/sleep')")
        with patch:
            self.assertEqual(observer.read_processes(3)[100]['comm_basename'], 'sleep')
        self.assertEqual(children[0].returncode, 0)

    def test_ps_output_overflow_reaps_only_sampler_child(self):
        patch, children = self.with_fake_ps(
            "import os,time; os.write(1,b'x'*64); time.sleep(10)")
        with patch, mock.patch.object(observer, "MAX_PS_BYTES", 5):
            with self.assertRaisesRegex(observer.ObservationError, "^ps_output_limit$"):
                observer.read_processes(1)
        self.assertIsNotNone(children[0].returncode)
        self.assertTrue(children[0].stdout.closed)

    def test_ps_timeout_reaps_sampler_child(self):
        patch, children = self.with_fake_ps("import time; time.sleep(10)")
        with patch:
            with self.assertRaisesRegex(observer.ObservationError, "^ps_timeout$"):
                observer.read_processes(.05)
        self.assertIsNotNone(children[0].returncode)
        self.assertTrue(children[0].stdout.closed)

    def test_ps_nonzero_and_spawn_errors_are_sanitized(self):
        patch, children = self.with_fake_ps("raise SystemExit(1)")
        with patch:
            with self.assertRaisesRegex(observer.ObservationError, "^ps_nonzero_exit$"):
                observer.read_processes(1)
        self.assertEqual(children[0].returncode, 1)
        with mock.patch.object(observer.subprocess, "Popen", side_effect=OSError("private")):
            with self.assertRaisesRegex(observer.ObservationError, "^ps_spawn_failed$"):
                observer.read_processes(1)


if __name__ == "__main__":
    unittest.main()
