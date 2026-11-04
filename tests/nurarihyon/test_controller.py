"""Synthetic local engineering checks; never reads research/backup state."""
import copy
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import threading

from lodestone_nurarihyon.common import canonical
from lodestone_nurarihyon.controller import Controller
from lodestone_nurarihyon.policy import DEFAULT_POLICY
from lodestone_pando.store import Board, SHELVES

ROOT = Path(__file__).resolve().parents[2]
ENV = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"}


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="nurarihyon-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.home = self.base / "state"
        self.c = Controller.initialize(self.home)

    def source(self, name="Offline project board", filename="source.json", baseline=None):
        if baseline is None:
            v = Board(self.c.active_directory() / "pando").settings()
            baseline = {"board_name": v["name"], "enabled_shelves": v["shelves"]}
        candidate = {**baseline, "board_name": name}
        path = self.base / filename
        path.write_bytes(json.dumps({"schema": "nurarihyon-profile-source/v1", "baseline": baseline,
                                    "candidate": candidate, "rationale": "Use the explicitly requested local board label."}, indent=2).encode())
        return path

    def queued(self, *, key="task-one", priority=0, source=None):
        task = self.c.submit(source or self.source(), key=key, priority=priority)
        self.c.approve(task["id"], task["plan_sha"])
        return task

    def child(self, code, *args):
        return subprocess.Popen([sys.executable, "-B", "-c", code, str(self.home), *args],
                                env=ENV, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def test_plan_cannot_run_without_exact_host_approval(self):
        t = self.c.submit(self.source(), key="inert")
        self.assertEqual(t["state"], "PLANNED")
        self.assertEqual(self.c.run_once()["status"], "IDLE")
        with self.assertRaisesRegex(ValueError, "exact pending plan"):
            self.c.approve(t["id"], "0" * 64)
        self.assertEqual(len(self.c.status()["tasks"]), 1)

    def test_complete_existing_toolkit_chain_and_durable_restart(self):
        t = self.queued()
        result = self.c.run_once()
        self.assertEqual(result["state"], "COMPLETED", result)
        self.assertEqual(result["result"]["metrics"]["rss_observer"],
                         "Darwin proc_pidinfo task RSS" if sys.platform == "darwin"
                         else "Linux procfs statm RSS in bytes")
        restarted = Controller(self.home)
        self.assertEqual(restarted.verify()["generations"], 2)
        board = Board(restarted.active_directory() / "pando")
        self.assertEqual(board.settings()["name"], "Offline project board")
        self.assertEqual(board.state()["stats"]["artifacts"], 3)
        artifact = board.artifact(result["result"]["candidate_artifact"])
        self.assertEqual(artifact["record"]["payload"]["activation"], "NONE")
        self.assertEqual(restarted.status(t["id"])["state"], "COMPLETED")
        self.assertEqual(restarted.run_once()["status"], "IDLE")

    def test_idempotency_before_and_after_publication(self):
        source = self.source()
        t = self.queued(source=source)
        self.assertEqual(self.c.submit(source, key="task-one")["id"], t["id"])
        self.assertEqual(self.c.run_once()["state"], "COMPLETED")
        current = self.c.active_directory().name
        again = Controller(self.home).submit(source, key="task-one")
        self.assertEqual(again["state"], "COMPLETED")
        self.assertEqual(again["attempts"], 1)
        self.assertEqual(self.c.active_directory().name, current)
        with self.assertRaisesRegex(ValueError, "different input"):
            self.c.submit(source, key="task-one", priority=1)

    def test_priority_and_stale_baseline_prevent_lost_update(self):
        low = self.queued(key="low", priority=1, source=self.source("Low board", "low.json"))
        high = self.queued(key="high", priority=8, source=self.source("High board", "high.json"))
        result = self.c.run_once()
        self.assertEqual(result["id"], high["id"])
        self.assertEqual(result["state"], "COMPLETED", result)
        second = self.c.run_once()
        self.assertEqual(second["id"], low["id"])
        self.assertEqual(second["result"]["outcome"], "STALE_BASELINE")
        self.assertEqual(Board(self.c.active_directory() / "pando").settings()["name"], "High board")
        self.assertEqual(len(self.c.status()["lessons"]), 1)

    def test_captured_source_drift_is_not_reinterpreted(self):
        t = self.queued()
        (self.home / "inputs" / (t["source_sha"] + ".json")).write_bytes(b"{}")
        result = self.c.run_once()
        self.assertEqual(result["status"], "BLOCKED_STATE")
        self.assertEqual(self.c.status(t["id"])["attempts"], 0)
        self.assertEqual(self.c.active_directory().name, "0" * 32)

    def test_policy_drift_refuses_without_activating(self):
        t = self.queued()
        altered = {**DEFAULT_POLICY, "worker_wall_seconds": 19}
        (self.home / "policy.json").write_bytes(canonical(altered))
        result = self.c.run_once()
        self.assertEqual(result["status"], "BLOCKED_STATE")
        self.assertEqual(self.c.status(t["id"])["attempts"], 0)
        self.assertEqual(self.c.active_directory().name, "0" * 32)
        with self.assertRaisesRegex(ValueError, "policy identity"):
            Controller(self.home)
        with self.assertRaisesRegex(ValueError, "policy identity"):
            self.c.verify()

    def test_verification_does_not_reinitialize_or_change_state(self):
        before = {str(p): (p.stat().st_mtime_ns, p.stat().st_ctime_ns, p.read_bytes())
                  for p in self.c.active_directory().rglob('*') if p.is_file()}
        self.c.verify()
        after = {str(p): (p.stat().st_mtime_ns, p.stat().st_ctime_ns, p.read_bytes())
                 for p in self.c.active_directory().rglob('*') if p.is_file()}
        self.assertEqual(before, after)

    def test_unrelated_damaged_history_does_not_consume_queued_task(self):
        old = self.queued()
        self.assertEqual(self.c.run_once()["state"], "COMPLETED")
        new = self.queued(key="unrelated", source=self.source("Next board", "next.json"))
        (self.home / "inputs" / (old["source_sha"] + ".json")).write_bytes(b"{}")
        before = self.c.status(new["id"])
        result = self.c.run_once()
        self.assertEqual(result["status"], "BLOCKED_STATE")
        self.assertEqual(self.c.status(new["id"]), before)
        self.assertEqual(self.c.status()["lessons"], [])

    def test_capacity_preflight_does_not_consume_task_or_make_lesson(self):
        t = self.queued()
        with mock.patch("lodestone_nurarihyon.controller.tree_usage", return_value=(0, 8100)):
            result = self.c.run_once()
        self.assertEqual(result["status"], "BLOCKED_RESOURCE")
        self.assertEqual(self.c.status(t["id"])["attempts"], 0)
        self.assertEqual(self.c.status(t["id"])["state"], "QUEUED")
        self.assertEqual(self.c.status()["lessons"], [])

    def test_foreground_service_survives_concurrent_host_lock(self):
        result = []
        with self.c.locked():
            thread = threading.Thread(target=lambda: result.append(self.c.serve(seconds=1)))
            thread.start()
            time.sleep(0.25)
            self.assertTrue(thread.is_alive())
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result[0]["status"], "DURATION_COMPLETE")

    def test_short_foreground_window_never_admits_or_consumes_task(self):
        t = self.queued()
        before = self.c.status()
        with mock.patch.object(self.c, "verify", side_effect=AssertionError("No preflight needed")):
            result = self.c.serve(seconds=1)
        self.assertEqual(result["status"], "WINDOW_TOO_SHORT")
        self.assertFalse(result["work_started"])
        self.assertEqual(result["required_wall_seconds"], 40)
        after = self.c.status()
        self.assertEqual(after["tasks"], before["tasks"])
        self.assertEqual(after["lessons"], before["lessons"])
        self.assertEqual(self.c.status(t["id"])["attempts"], 0)

    def test_status_tolerates_disappearing_live_journal(self):
        transient = self.c.home / "transient-journal"
        transient.write_bytes(b"temporary")
        original = Path.lstat
        injected = []
        def disappearing(path, *args, **kwargs):
            if path == transient:
                injected.append(path)
                transient.unlink(missing_ok=True)
                raise FileNotFoundError(str(path))
            return original(path, *args, **kwargs)
        with mock.patch.object(Path, "lstat", disappearing):
            self.assertTrue(self.c.status()["retained_bytes_are_live_sample"])
        self.assertEqual(injected, [transient])
        self.assertFalse(transient.exists())

    def test_independent_brief_bound_to_task_and_job(self):
        t = self.queued()
        result = self.c.run_once(fault="before_commit")
        stage = self.home / result["result"]["retained_stage"]
        report_path = stage / "worker-result.json"
        report = json.loads(report_path.read_bytes())
        folded_path = (stage / report["brief_path"]).parent / "folded.json"
        original = folded_path.read_bytes()
        folded = json.loads(original); folded["query"] = "unrelated"
        folded_path.write_bytes(canonical(folded))
        with self.assertRaisesRegex(ValueError, "source/query"):
            self.c._validate_stage(stage, self.c.status(t["id"]), t["plan"])
        folded_path.write_bytes(original)
        report["brief_job"] = "0" * 32
        report_path.write_bytes(canonical(report))
        with self.assertRaisesRegex(ValueError, "declared toolkit job"):
            self.c._validate_stage(stage, self.c.status(t["id"]), t["plan"])

    def test_legacy_policy_remains_readable_without_raising_its_budget(self):
        legacy = {k: v for k, v in DEFAULT_POLICY.items() if k != "supervisor_wall_seconds"}
        state = Controller.initialize(self.base / "legacy", legacy)
        self.assertEqual(Controller(state.home).verify()["status"], "INTACT")
        self.assertNotIn("supervisor_wall_seconds", state.policy)

    def test_internal_numeric_overflow_refused(self):
        from lodestone_nurarihyon.common import strict_json
        with self.assertRaisesRegex(ValueError, "Nonfinite"):
            strict_json(b'{"value":1e400}')

    def test_stale_code_approval_does_not_starve_new_plan(self):
        old = self.queued(key="old", priority=9)
        with mock.patch("lodestone_nurarihyon.controller.code_identity", return_value="a" * 64):
            new = self.queued(key="new", priority=9)
            result = self.c.run_once()
            self.assertEqual(result["state"], "REPLAN_REQUIRED")
            self.assertEqual(result["attempts"], 0)
            self.assertEqual(self.c.status()["lessons"], [])
            # A deliberate failing worker proves the second row is selected;
            # no full execution against a mocked code identity is certified.
            result = self.c.run_once(fault="exit")
            self.assertEqual(result["id"], new["id"])
            self.assertEqual(result["attempts"], 1)

    def test_slow_preadmission_check_never_consumes_attempt(self):
        t = self.queued()
        clock = [time.monotonic()]
        validate = self.c._validate_intent
        def slow(row):
            value = validate(row)
            clock[0] += 21
            return value
        with mock.patch("lodestone_nurarihyon.controller.time.monotonic", side_effect=lambda: clock[0]), mock.patch.object(self.c, "_validate_intent", side_effect=slow):
            result = self.c.run_once()
        self.assertEqual(result["status"], "BLOCKED_RESOURCE")
        self.assertEqual(self.c.status(t["id"])["attempts"], 0)
        self.assertEqual(self.c.status()["lessons"], [])

    def test_resource_exit_can_precede_stop_signal(self):
        from lodestone_nurarihyon.controller import _stop_child
        child = mock.Mock()
        child.poll.side_effect = [None, -signal.SIGXFSZ]
        with mock.patch("lodestone_nurarihyon.controller.os.killpg", side_effect=PermissionError()):
            _stop_child(child)
        child.kill.assert_not_called()
        child.wait.assert_called_once()

    def test_database_only_stop_at_publication_is_not_a_source_lesson(self):
        t = self.queued()
        from lodestone_nurarihyon.common import sync_directory as sync
        def stopping(path):
            sync(path)
            if Path(path) == self.c.home / "generations":
                with self.c.connection() as db:
                    db.execute("UPDATE metadata SET value='1' WHERE key='stop_requested'")
        with mock.patch("lodestone_nurarihyon.controller.sync_directory", side_effect=stopping):
            result = self.c.run_once()
        self.assertEqual(result["state"], "INTERRUPTED", result)
        self.assertEqual(result["result"]["outcome"], "STOPPED")
        self.assertEqual(self.c.active_directory().name, "0" * 32)
        self.assertEqual(self.c.status()["lessons"], [])

    def test_reap_timeout_is_retained_instead_of_losing_failure_record(self):
        task = self.queued()
        generation = self.c.active_directory().name
        # Exercise cleanup-error persistence without racing the real worker's
        # SIGALRM against the controller's wall deadline. Real worker limits and
        # STOP handling remain covered by their separate integration tests.
        child = mock.Mock(spec=subprocess.Popen)
        child.poll.return_value = None
        with mock.patch("lodestone_nurarihyon.controller.subprocess.Popen", return_value=child) as spawn, \
                mock.patch("lodestone_nurarihyon.process_rss.sample_owned_worker",
                           return_value=DEFAULT_POLICY["worker_rss_bytes"] + 1), \
                mock.patch("lodestone_nurarihyon.controller._stop_child",
                           side_effect=subprocess.TimeoutExpired("injected child reap", 3)) as stop:
            result = self.c.run_once(fault="wait")
        spawn.assert_called_once()
        self.assertEqual(stop.call_args_list, [mock.call(child), mock.call(child)])
        self.assertEqual(result["state"], "INTERRUPTED", result)
        self.assertEqual(result["result"]["outcome"], "EXECUTOR_ERROR")
        self.assertIn("injected child reap", result["result"]["metrics"]["child_reap_error"])
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(self.c.active_directory().name, generation)
        self.assertEqual(Controller(self.home).status(task["id"])["result"], result["result"])
        self.assertEqual(self.c.status()["lessons"], [])

    def test_late_publication_sync_cannot_cross_deadline_and_commit(self):
        self.queued()
        from lodestone_nurarihyon.common import sync_directory as sync
        original_clock = time.monotonic
        offset = [0]
        def delayed(path):
            sync(path)
            if Path(path) == self.c.home / "generations":
                offset[0] = 41
        with mock.patch("lodestone_nurarihyon.controller.sync_directory", side_effect=delayed), mock.patch("lodestone_nurarihyon.controller.time.monotonic", side_effect=lambda: original_clock()+offset[0]):
            result = self.c.run_once()
        self.assertEqual(result["result"]["outcome"], "EXECUTOR_LIMIT")
        self.assertEqual(self.c.active_directory().name, "0" * 32)

    def test_injected_failure_retains_stage_and_rolls_back(self):
        self.queued()
        before = self.c.active_directory().name
        result = self.c.run_once(fault="before_commit")
        self.assertEqual(result["state"], "REJECTED")
        self.assertEqual(self.c.active_directory().name, before)
        self.assertTrue((self.home / result["result"]["retained_stage"]).is_dir())
        self.assertEqual(self.c.verify()["generations"], 1)
        self.assertEqual(len(self.c.status()["lessons"]), 1)

    def test_real_process_crash_before_commit_and_explicit_recovery(self):
        t = self.queued()
        child = self.child("from lodestone_nurarihyon.controller import Controller; import sys; Controller(sys.argv[1]).run_once(fault='crash_before_commit')")
        stdout, stderr = child.communicate(timeout=30)
        self.assertEqual(child.returncode, 72, (stdout, stderr))
        restarted = Controller(self.home)
        self.assertEqual(restarted.status(t["id"])["state"], "RUNNING")
        self.assertEqual(restarted.recover()["reconciled_tasks"], [t["id"]])
        self.assertEqual(restarted.active_directory().name, "0" * 32)
        self.assertEqual(restarted.run_once()["status"], "IDLE")
        restarted.retry(t["id"])
        result = restarted.run_once()
        self.assertEqual(result["state"], "COMPLETED", result)
        self.assertEqual(result["attempts"], 2)

    def test_lost_response_after_commit_never_replays_effect(self):
        source = self.source()
        t = self.queued(source=source)
        child = self.child("from lodestone_nurarihyon.controller import Controller; import sys; Controller(sys.argv[1]).run_once(fault='after_commit')")
        stdout, stderr = child.communicate(timeout=30)
        self.assertEqual(child.returncode, 73, (stdout, stderr))
        restored = Controller(self.home)
        self.assertEqual(restored.recover()["reconciled_tasks"], [])
        self.assertEqual(restored.submit(source, key="task-one")["attempts"], 1)
        self.assertEqual(restored.status(t["id"])["state"], "COMPLETED")
        self.assertEqual(restored.verify()["generations"], 2)

    def test_host_stop_interrupts_worker_and_prevents_publication(self):
        t = self.queued()
        child = self.child("from lodestone_nurarihyon.controller import Controller; import sys,json; print(json.dumps(Controller(sys.argv[1]).run_once(fault='wait')))")
        deadline = time.monotonic() + 10
        while self.c.status(t["id"])["state"] != "RUNNING" and time.monotonic() < deadline:
            time.sleep(0.02)
        self.c.stop()
        stdout, stderr = child.communicate(timeout=10)
        self.assertEqual(child.returncode, 0, stderr)
        self.assertEqual(json.loads(stdout)["state"], "INTERRUPTED")
        self.assertEqual(self.c.active_directory().name, "0" * 32)
        self.assertEqual(self.c.run_once()["status"], "STOPPED")
        self.assertEqual(Controller(self.home).status()["stopped"], True)
        self.c.clear_stop()
        self.assertEqual(self.c.run_once()["status"], "IDLE")

    def test_stopped_marker_survives_restart(self):
        self.c.stop()
        self.assertTrue(Controller(self.home).status()["stopped"])
        self.assertFalse(self.c.clear_stop()["work_started"])

    def test_database_stop_without_marker_is_reported_and_not_busy_polled(self):
        with self.c.connection() as db:
            db.execute("UPDATE metadata SET value='1' WHERE key='stop_requested'")
        self.assertTrue(self.c.status()["stopped"])
        before = time.monotonic()
        self.assertEqual(self.c.serve(seconds=2)["status"], "STOPPED")
        self.assertLess(time.monotonic() - before, 1)
        self.c.clear_stop()
        self.assertFalse(self.c.status()["stopped"])

    def test_cpu_memory_and_output_limits_prevent_publication(self):
        for fault, override in [("cpu", {"worker_cpu_seconds": 1}),
                                ("memory", {"worker_rss_bytes": 67108864}),
                                ("output", {"max_stage_bytes": 1048576})]:
            with self.subTest(fault=fault):
                self.home = self.base / fault
                self.c = Controller.initialize(self.home, {**DEFAULT_POLICY, **override})
                self.queued()
                result = self.c.run_once(fault=fault)
                self.assertEqual(result["result"]["outcome"], "RESOURCE_LIMIT", result)
                self.assertEqual(self.c.active_directory().name, "0" * 32)

    def test_complete_cycle_with_process_network_blocked(self):
        self.queued()
        script = '''import sys,json
def guard(event,args):
 if event.startswith('socket.'):
  raise PermissionError('Network disabled for offline test')
sys.addaudithook(guard)
from lodestone_nurarihyon.controller import Controller
c=Controller(sys.argv[1]); result=c.run_once()
assert result['state']=='COMPLETED', result
assert Controller(sys.argv[1]).recover()['reconciled_tasks']==[]
print(json.dumps(Controller(sys.argv[1]).verify()))
'''
        child = self.child(script)
        stdout, stderr = child.communicate(timeout=30)
        self.assertEqual(child.returncode, 0, stderr)
        self.assertEqual(json.loads(stdout)["status"], "INTACT")

    def test_os_lock_rejects_simultaneous_execution_and_snapshot(self):
        with self.c.locked():
            with self.assertRaisesRegex(RuntimeError, "busy"):
                with Controller(self.home).locked():
                    pass

    def test_wall_resource_limit_retains_failure(self):
        limited = self.base / "limited"
        self.c = Controller.initialize(limited, {**DEFAULT_POLICY, "worker_wall_seconds": 1})
        self.home = limited
        self.queued()
        result = self.c.run_once(fault="wait")
        self.assertEqual(result["result"]["outcome"], "RESOURCE_LIMIT", result)
        self.assertEqual(self.c.active_directory().name, "0" * 32)

    def test_process_local_network_and_write_guards(self):
        allowed = self.base / "allowed"; allowed.mkdir()
        outside = self.base / "outside.txt"
        script = '''from lodestone_nurarihyon.policy import block_external_calls
import socket, pathlib, sys
block_external_calls(sys.argv[2])
for f in [lambda: socket.socket(), lambda: pathlib.Path(sys.argv[3]).write_text('forbidden')]:
 try: f()
 except PermissionError: pass
 else: raise AssertionError('guard failed')
print('PASS')
'''
        child = self.child(script, str(allowed), str(outside))
        stdout, stderr = child.communicate(timeout=10)
        self.assertEqual(child.returncode, 0, stderr)
        self.assertEqual(stdout.strip(), "PASS")
        self.assertFalse(outside.exists())

    def test_data_cannot_supply_commands_or_permissions(self):
        source = self.source()
        value = json.loads(source.read_bytes()); value["command"] = "touch /tmp/unapproved"
        source.write_bytes(canonical(value))
        with self.assertRaises(ValueError):
            self.c.submit(source, key="injected")
        self.assertEqual(self.c.status()["tasks"], [])

    def test_sqlite_intent_and_event_history_are_not_rewritten(self):
        t = self.c.submit(self.source(), key="immutable")
        with self.assertRaises(sqlite3.IntegrityError):
            with self.c.connection() as db:
                db.execute("UPDATE tasks SET priority=9 WHERE id=?", (t["id"],))
        with self.assertRaises(sqlite3.IntegrityError):
            with self.c.connection() as db:
                db.execute("DELETE FROM events")


if __name__ == "__main__":
    unittest.main()
