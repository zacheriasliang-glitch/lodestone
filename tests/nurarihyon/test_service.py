"""Isolated service engineering checks; no launchd or research artifact access.

Children run only this checkout against fresh canonical temporary directories.
Fault injection uses the Python API in one child, never a normal CLI option.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from lodestone_nurarihyon import service
from lodestone_nurarihyon.common import BusyError, file_lock
from lodestone_nurarihyon.controller import Controller, code_identity
from lodestone_nurarihyon.deployment import environment_identity
from lodestone_nurarihyon.inspection import ReadOnlyBoard


ROOT = Path(__file__).resolve().parents[2]
SECRET = "fixture-only-never-log-this-private-value"
ENV = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1",
       "NURARIHYON_TEST_API_KEY": SECRET}


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="nurarihyon-service-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.c = Controller.initialize(self.base / "state")
        self.log = self.base / "events.jsonl"
        self.pin = code_identity()
        self.environment_pin = environment_identity()

    def events(self, path=None):
        path = path or self.log
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def wait_for(self, predicate, *, timeout=10, child=None):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            value = predicate()
            if value:
                return value
            if child is not None and child.poll() is not None:
                stdout, stderr = child.communicate()
                self.fail(f"Child exited before observation: {child.returncode}; {stdout}; {stderr}")
            time.sleep(0.05)
        self.fail("Timed out waiting for a bounded fixture observation")

    def cleanup_child(self, child):
        if child.poll() is None:
            child.terminate()
        try:
            child.communicate(timeout=8)
        except subprocess.TimeoutExpired:
            child.kill()
            child.communicate(timeout=3)

    def child(self, *, log=None, pin=None, environment_pin=None, code=None):
        log, pin = log or self.log, pin or self.pin
        environment_pin = environment_pin or self.environment_pin
        if code is None:
            args = [sys.executable, "-B", str(ROOT / "tools/nurarihyon/run"), "--home", str(self.c.home),
                    "daemon", "--log-file", str(log), "--expected-code-sha", pin,
                    "--expected-environment-sha", environment_pin]
        else:
            args = [sys.executable, "-B", "-c", code, str(self.c.home), str(log), pin, environment_pin]
        child = subprocess.Popen(args, cwd=ROOT, env=ENV, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.cleanup_child, child)
        return child

    def result(self, child, *, timeout=10, exit_code=0):
        stdout, stderr = child.communicate(timeout=timeout)
        self.assertEqual(child.returncode, exit_code, stderr + stdout)
        self.assertEqual(stderr, "")
        return json.loads(stdout)

    def queued(self, *, approve=True):
        source = self.base / "private-source.json"
        view = ReadOnlyBoard(self.c.active_directory() / "pando").settings()
        baseline = {"board_name": view["name"], "enabled_shelves": view["shelves"]}
        source.write_text(json.dumps({"schema": "nurarihyon-profile-source/v1", "baseline": baseline,
            "candidate": {**baseline, "board_name": "Approved service fixture"}, "rationale": SECRET}))
        task = self.c.submit(source, key="explicit-fixture")
        if approve:
            self.c.approve(task["id"], task["plan_sha"])
        return task

    def test_empty_daemon_waits_without_creating_work_and_sigterm_exits(self):
        before = self.c.status()
        child = self.child()
        self.wait_for(lambda: any(e["status"] == "STARTED" for e in self.events()), child=child)
        time.sleep(0.2)
        self.assertIsNone(child.poll())
        self.assertEqual(self.c.status()["tasks"], [])
        self.assertEqual(self.c.status()["active_generation"], before["active_generation"])
        child.send_signal(signal.SIGTERM)
        result = self.result(child)
        self.assertEqual(result["status"], "TERMINATED")
        self.assertEqual(result["operations_observed"], 0)
        self.assertEqual(result["poll_seconds"], 5.0)
        self.assertFalse(result["automatic_retry"])
        self.assertFalse(self.c.status()["stopped"])
        self.assertEqual([e["status"] for e in self.events()], ["STARTED", "TERMINATED"])
        with file_lock(self.c.home / "executor.lock"):
            pass

    def test_second_daemon_refuses_executor_lock_without_new_log(self):
        first = self.child()
        self.wait_for(lambda: any(e["status"] == "STARTED" for e in self.events()), child=first)
        second_log = self.base / "second-events.jsonl"
        second = self.child(log=second_log)
        self.assertEqual(self.result(second)["status"], "ALREADY_RUNNING")
        self.assertFalse(second_log.exists())
        self.assertIsNone(first.poll())
        first.send_signal(signal.SIGTERM)
        self.assertEqual(self.result(first)["status"], "TERMINATED")

    def test_unapproved_task_remains_inert(self):
        task = self.queued(approve=False)
        child = self.child()
        self.wait_for(lambda: any(e["status"] == "STARTED" for e in self.events()), child=child)
        child.send_signal(signal.SIGTERM)
        self.assertEqual(self.result(child)["operations_observed"], 0)
        after = self.c.status(task["id"])
        self.assertEqual(after["state"], "PLANNED")
        self.assertEqual(after["attempts"], 0)
        self.assertIsNone(after["approval"])

    def test_host_stop_survives_daemon_restart_without_consuming_queue(self):
        task = self.queued()
        self.c.stop()
        stop_bytes = (self.c.home / "STOP").read_bytes()
        before = self.c.status(task["id"])
        for index in range(2):
            child = self.child(log=self.base / f"stopped-{index}.jsonl")
            result = self.result(child)
            self.assertEqual(result["status"], "STOPPED")
            self.assertEqual(result["operations_observed"], 0)
            self.assertFalse(result["stop_cleared"])
        self.assertEqual(self.c.status(task["id"]), before)
        self.assertEqual((self.c.home / "STOP").read_bytes(), stop_bytes)
        self.assertTrue(Controller(self.c.home).status()["stopped"])

    def test_database_only_stop_survives_restart_without_marker_creation(self):
        task = self.queued()
        with self.c.connection() as db:
            db.execute("UPDATE metadata SET value='1' WHERE key='stop_requested'")
        before = self.c.status(task["id"])
        for index in range(2):
            child = self.child(log=self.base / f"database-stop-{index}.jsonl")
            self.assertEqual(self.result(child)["status"], "STOPPED")
        self.assertFalse((self.c.home / "STOP").exists())
        self.assertEqual(self.c.status(task["id"]), before)
        self.assertTrue(Controller(self.c.home).status()["stopped"])

    def test_running_receipt_requires_manual_recovery_and_is_never_replayed(self):
        task = self.queued()
        with self.c.connection() as db:
            db.execute("UPDATE tasks SET state='RUNNING',attempts=1 WHERE id=?", (task["id"],))
        before = self.c.status(task["id"])
        result = self.result(self.child(), exit_code=2)
        self.assertEqual(result["status"], "RECOVERY_REQUIRED")
        self.assertEqual(result["operations_observed"], 0)
        self.assertEqual(self.c.status(task["id"]), before)
        self.assertEqual(self.c.active_directory().name, "0" * 32)
        self.assertEqual(self.c.status()["lessons"], [])

    def test_initial_runtime_pin_mismatch_refuses_without_attempt(self):
        task = self.queued()
        before = self.c.status(task["id"])
        result = self.result(self.child(pin="0" * 64), exit_code=2)
        self.assertEqual(result["status"], "RUNTIME_CHANGED")
        self.assertEqual(self.c.status(task["id"]), before)
        self.assertEqual([e["status"] for e in self.events()], ["RUNTIME_CHANGED"])

    def test_live_manual_state_or_worker_custody_is_busy_not_interrupted(self):
        task = self.queued()
        with self.c.connection() as db:
            db.execute("UPDATE tasks SET state='RUNNING',attempts=1 WHERE id=?", (task["id"],))
        before = self.c.status(task["id"])
        for name in ("state.lock", "worker.lock"):
            with self.subTest(lock=name), file_lock(self.c.home / name):
                self.assertEqual(service._stopped_or_work(self.c), "STATE_BUSY")
                result = service.run_service(self.c, log_file=self.base / (name + ".jsonl"),
                    expected_code_sha=self.pin, expected_environment_sha=self.environment_pin)
                self.assertEqual(result["status"], "STATE_BUSY")
                self.assertEqual(result["operations_observed"], 0)
                self.assertEqual(self.c.status(task["id"]), before)
        self.assertEqual(service._stopped_or_work(self.c), "RECOVERY_REQUIRED")
        self.assertEqual(self.c.status(task["id"]), before)

    def test_environment_pin_mismatch_at_startup_leaves_queue_unchanged(self):
        task = self.queued()
        before = self.c.status(task["id"])
        result = self.result(self.child(environment_pin="0" * 64), exit_code=2)
        self.assertEqual(result["status"], "ENVIRONMENT_CHANGED")
        self.assertEqual(result["operations_observed"], 0)
        self.assertEqual(self.c.status(task["id"]), before)
        self.assertEqual([e["status"] for e in self.events()], ["ENVIRONMENT_CHANGED"])

    def test_environment_admission_race_precedes_intent_mutation(self):
        task = self.queued()
        before = self.c.status(task["id"])
        stale = "0" * 64
        with patch.object(service, "environment_identity", return_value=stale), \
                patch.object(self.c, "_validate_intent", wraps=self.c._validate_intent) as validate, \
                patch.object(self.c, "run_once", wraps=self.c.run_once) as admitted:
            result = service.run_service(self.c, log_file=self.log, expected_code_sha=self.pin,
                                         expected_environment_sha=stale)
        validate.assert_not_called()
        admitted.assert_called_once()
        self.assertEqual(admitted.call_args.kwargs["expected_environment_sha"], stale)
        self.assertEqual(result["status"], "ENVIRONMENT_CHANGED")
        self.assertEqual(self.c.status(task["id"]), before)

    def test_systemic_failure_stops_service_before_second_queued_task(self):
        first = self.queued()
        second = self.c.submit(self.base / "private-source.json", key="next-explicit-fixture")
        self.c.approve(second["id"], second["plan_sha"])
        before = self.c.status(second["id"])
        cases = [("RESOURCE_LIMIT", {}, "RESOURCE_LIMIT"),
                 ("EXECUTOR_ERROR", {}, "EXECUTOR_ERROR"),
                 ("EXECUTOR_LIMIT", {}, "EXECUTOR_LIMIT"),
                 ("REJECTED_VALIDATION", {"child_reap_error": SECRET}, "EXECUTOR_ERROR")]
        for index, (outcome, metrics, expected) in enumerate(cases):
            result_fixture = {"id": first["id"], "state": "REJECTED",
                              "result": {"outcome": outcome, "metrics": metrics}}
            log = self.base / f"systemic-{index}.jsonl"
            with self.subTest(outcome=outcome), \
                    patch.object(self.c, "run_once", side_effect=[result_fixture, AssertionError("Second task executed")]) as admitted, \
                    patch.object(service.time, "sleep", side_effect=AssertionError("Systemic failure continued polling")):
                result = service.run_service(self.c, log_file=log, expected_code_sha=self.pin,
                                             expected_environment_sha=self.environment_pin)
            admitted.assert_called_once()
            self.assertEqual(result["status"], expected)
            self.assertEqual(result["operations_observed"], 1)
            self.assertEqual(self.c.status(second["id"]), before)
            self.assertNotIn(SECRET, log.read_text())

    def test_import_failure_has_prelogger_exit_70_without_sensitive_text(self):
        runtime = self.base / "import-failure-runtime"
        launcher = runtime / "tools/nurarihyon/run"
        launcher.parent.mkdir(parents=True)
        launcher.write_bytes((ROOT / "tools/nurarihyon/run").read_bytes())
        package = runtime / "src/lodestone_nurarihyon"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("")
        (package / "__main__.py").write_text("raise ImportError(" + repr(SECRET) + ")\n")
        child = subprocess.Popen([sys.executable, "-B", str(launcher), "--home", str(self.c.home),
            "daemon", "--log-file", str(self.log), "--expected-code-sha", self.pin,
            "--expected-environment-sha", self.environment_pin], env=ENV,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.cleanup_child, child)
        stdout, stderr = child.communicate(timeout=10)
        self.assertEqual(child.returncode, 70)
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(stderr), {"stage": "DAEMON_IMPORT", "error": "ImportError"})
        self.assertNotIn(SECRET, stderr)
        self.assertFalse(self.log.exists())
        self.assertEqual(self.c.status()["tasks"], [])

    def test_setup_failure_has_prelogger_exit_71_without_sensitive_text(self):
        child = self.child(log=self.base / SECRET / "events.jsonl")
        stdout, stderr = child.communicate(timeout=10)
        self.assertEqual(child.returncode, 71)
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(stderr), {"stage": "DAEMON_SETUP", "error": "ValueError"})
        self.assertNotIn(SECRET, stderr)
        self.assertNotIn(str(self.c.home), stderr)
        self.assertFalse((self.base / SECRET).exists())
        self.assertEqual(self.c.status()["tasks"], [])

    def test_admission_race_checks_loaded_runtime_pin_and_does_not_consume_attempt(self):
        # Simulate a running service that loaded an older generation; a newly
        # approved plan legitimately binds the current code. Only the admission
        # pin can prevent this old service from executing the new plan.
        task = self.queued()
        before = self.c.status(task["id"])
        old_pin = "0" * 64
        with patch.object(service, "code_identity", return_value=old_pin), \
                patch.object(self.c, "run_once", wraps=self.c.run_once) as admitted:
            result = service.run_service(self.c, log_file=self.log, expected_code_sha=old_pin,
                                         expected_environment_sha=self.environment_pin)
        admitted.assert_called_once()
        self.assertEqual(set(admitted.call_args.kwargs), {"expected_code_sha", "expected_environment_sha", "cancel_requested"})
        self.assertEqual(admitted.call_args.kwargs["expected_code_sha"], old_pin)
        self.assertEqual(admitted.call_args.kwargs["expected_environment_sha"], self.environment_pin)
        self.assertTrue(callable(admitted.call_args.kwargs["cancel_requested"]))
        self.assertFalse(admitted.call_args.kwargs["cancel_requested"]())
        self.assertEqual(result["status"], "RUNTIME_CHANGED")
        self.assertEqual(result["operations_observed"], 0)
        self.assertEqual(self.c.status(task["id"]), before)
        self.assertFalse(any((self.c.home / "staging").iterdir()))

    def test_changed_code_stops_service_before_mutating_stale_approval(self):
        task = self.queued()
        before = self.c.status(task["id"])
        with patch("lodestone_nurarihyon.controller.code_identity", return_value="b" * 64), \
                patch.object(self.c, "_validate_intent", wraps=self.c._validate_intent) as validate:
            result = self.c.run_once(expected_code_sha=self.pin, expected_environment_sha=self.environment_pin)
        validate.assert_not_called()
        self.assertEqual(result, {"status": "RUNTIME_CHANGED", "work_started": False})
        self.assertEqual(self.c.status(task["id"]), before)
        self.assertEqual(self.c.active_directory().name, "0" * 32)
        self.assertEqual(self.c.status()["lessons"], [])

    def test_cancellation_before_admission_and_during_preflight_preserves_queue(self):
        task = self.queued()
        before = self.c.status(task["id"])
        cancelled = threading.Event()
        cancelled.set()
        result = self.c.run_once(expected_code_sha=self.pin, cancel_requested=cancelled.is_set)
        self.assertEqual(result, {"status": "INTERRUPTED", "work_started": False})
        self.assertEqual(self.c.status(task["id"]), before)
        cancelled.clear()
        verify = self.c.verify
        def cancel_after_verify():
            result = verify()
            cancelled.set()
            return result
        with patch.object(self.c, "verify", side_effect=cancel_after_verify), \
                patch("lodestone_nurarihyon.controller.subprocess.Popen") as spawn:
            result = self.c.run_once(expected_code_sha=self.pin, cancel_requested=cancelled.is_set)
        spawn.assert_not_called()
        self.assertEqual(result, {"status": "INTERRUPTED", "work_started": False})
        self.assertEqual(self.c.status(task["id"]), before)
        self.assertFalse(self.c.status()["stopped"])

    def test_cancellation_after_real_spawn_before_return_reaps_owned_child(self):
        task = self.queued()
        cancelled = threading.Event()
        original_popen = subprocess.Popen
        created = []
        def spawn_then_cancel(*args, **kwargs):
            child = original_popen(*args, **kwargs)
            command = args[0] if args else kwargs.get("args", ())
            if "lodestone_nurarihyon.worker" not in command:
                # subprocess.run also uses this shared Popen for /bin/ps.
                # Only the actual toolkit child is the injected custody gap.
                return child
            # This is precisely the former async-exception custody gap: the OS
            # process exists, but the controller's assignment has not returned.
            created.append(child)
            self.addCleanup(self.cleanup_child, child)
            cancelled.set()
            return child
        with patch("lodestone_nurarihyon.controller.subprocess.Popen", side_effect=spawn_then_cancel):
            result = self.c.run_once(fault="wait", expected_code_sha=self.pin,
                                     cancel_requested=cancelled.is_set)
        self.assertEqual(len(created), 1)
        self.assertIsNotNone(created[0].returncode)
        with self.assertRaises(ChildProcessError):
            os.waitpid(created[0].pid, os.WNOHANG)
        self.assertEqual(result["state"], "INTERRUPTED")
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(result["result"]["outcome"], "INTERRUPTED")
        self.assertEqual(result["id"], task["id"])
        self.assertTrue((self.c.home / result["result"]["retained_stage"]).is_dir())
        self.assertFalse(any(event["kind"] == "PUBLISHED" for event in result["events"]))
        self.assertEqual(self.c.active_directory().name, "0" * 32)
        with self.c.locked(timeout=0):
            self.assertEqual(self.c.verify()["generations"], 1)

    def test_cancellation_during_publication_preparation_rolls_back_activation(self):
        from lodestone_nurarihyon import controller as controller_module
        task = self.queued()
        cancelled = threading.Event()
        synced = []
        original_sync = controller_module.sync_directory
        def sync_then_cancel(path):
            original_sync(path)
            if path == self.c.home / "generations":
                synced.append(path)
                cancelled.set()
        # Cancellation is deliberately after the pre-publication flag check
        # and before BEGIN IMMEDIATE, so the transactional check is necessary.
        with patch.object(controller_module, "sync_directory", side_effect=sync_then_cancel):
            result = self.c.run_once(expected_code_sha=self.pin, cancel_requested=cancelled.is_set)
        self.assertEqual(synced, [self.c.home / "generations"])
        self.assertEqual(result["state"], "INTERRUPTED")
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(result["result"]["outcome"], "INTERRUPTED")
        self.assertEqual(result["id"], task["id"])
        stage = self.c.home / result["result"]["retained_stage"]
        self.assertTrue((stage / "generation.json").is_file())
        self.assertFalse(any(event["kind"] == "PUBLISHED" for event in result["events"]))
        self.assertEqual(self.c.active_directory().name, "0" * 32)
        self.assertEqual(self.c.verify()["generations"], 1)

    def test_one_approved_toolkit_operation_then_host_stop_exact_state(self):
        task = self.queued()
        original_capture = (self.c.home / "inputs" / (task["source_sha"] + ".json")).read_bytes()
        child = self.child()
        self.wait_for(lambda: self.c.status(task["id"])["state"] == "COMPLETED", timeout=40, child=child)
        self.c.stop()
        result = self.result(child, timeout=10)
        self.assertEqual(result["status"], "STOPPED")
        self.assertEqual(result["operations_observed"], 1)
        after = Controller(self.c.home)
        self.assertEqual(after.verify()["generations"], 2)
        self.assertEqual(after.status(task["id"])["attempts"], 1)
        view = ReadOnlyBoard(after.active_directory() / "pando").settings()
        self.assertEqual(view["name"], "Approved service fixture")
        self.assertEqual(view["shelves"], task["plan"]["baseline"]["enabled_shelves"])
        self.assertEqual((self.c.home / "inputs" / (task["source_sha"] + ".json")).read_bytes(), original_capture)
        rows = self.events()
        self.assertEqual([e["status"] for e in rows], ["STARTED", "TASK_OBSERVED", "STOPPED"])
        self.assertEqual(rows[1]["task"], task["id"])
        self.assertEqual(rows[1]["outcome"], "COMPLETED")
        text = self.log.read_text()
        for excluded in (SECRET, str(self.base), "Approved service fixture", "rationale", "API_KEY", "Traceback"):
            self.assertNotIn(excluded, text)

    def test_active_sigterm_reaps_worker_and_preserves_interrupted_attempt(self):
        task = self.queued()
        code = """
import json,sys
from lodestone_nurarihyon.controller import Controller
from lodestone_nurarihyon.service import run_service
original=Controller.run_once
def wait_worker(self, **kwargs):
    return original(self, fault='wait', **kwargs)
Controller.run_once=wait_worker
print(json.dumps(run_service(Controller(sys.argv[1]), log_file=sys.argv[2], expected_code_sha=sys.argv[3], expected_environment_sha=sys.argv[4])))
"""
        child = self.child(code=code)
        def worker_owns_lock():
            if self.c.status(task["id"])["state"] != "RUNNING":
                return False
            try:
                with file_lock(self.c.home / "worker.lock"):
                    return False
            except BusyError:
                return True
        self.wait_for(worker_owns_lock, timeout=12, child=child)
        child.send_signal(signal.SIGTERM)
        result = self.result(child)
        self.assertEqual(result["status"], "TERMINATED")
        after = self.c.status(task["id"])
        self.assertEqual(after["state"], "INTERRUPTED")
        self.assertEqual(after["attempts"], 1)
        self.assertEqual(after["result"]["outcome"], "INTERRUPTED")
        self.assertTrue((self.c.home / after["result"]["retained_stage"]).is_dir())
        self.assertEqual(self.c.active_directory().name, "0" * 32)
        with self.c.locked(timeout=0):
            self.assertEqual(self.c.verify()["generations"], 1)
        self.assertFalse(self.c.status()["stopped"])
        self.assertEqual(self.c.status()["lessons"], [])

    def test_bounded_event_log_rolls_over_and_never_accepts_sensitive_fields(self):
        with patch.object(service, "LOG_BYTES", 512):
            with service._events(self.log) as emit:
                for index in range(30):
                    emit("TASK_OBSERVED", task=f"{index:032x}", outcome="COMPLETED")
                for kwargs in ({"status": SECRET}, {"status": "STARTED", "task": SECRET},
                               {"status": "STARTED", "outcome": SECRET}):
                    with self.assertRaises(ValueError):
                        emit(**kwargs)
                with self.assertRaises(TypeError):
                    emit("STARTED", credential=SECRET)
            paths = [self.log, Path(str(self.log) + ".1")]
            self.assertTrue(all(path.is_file() for path in paths))
            self.assertTrue(all(path.stat().st_size <= 512 for path in paths))
            self.assertFalse(Path(str(self.log) + ".2").exists())
            for path in paths:
                for event in self.events(path):
                    self.assertEqual(set(event), {"schema", "time", "pid", "status", "task", "outcome"})
                self.assertNotIn(SECRET, path.read_text())

    def test_service_error_logs_fixed_status_without_exception_or_credentials(self):
        with patch.object(self.c, "verify", side_effect=ValueError(SECRET + str(self.base))):
            result = service.run_service(self.c, log_file=self.log, expected_code_sha=self.pin,
                                         expected_environment_sha=self.environment_pin)
        self.assertEqual(result["status"], "SERVICE_ERROR")
        self.assertEqual([e["status"] for e in self.events()], ["SERVICE_ERROR"])
        self.assertNotIn(SECRET, self.log.read_text())
        self.assertNotIn(str(self.base), self.log.read_text())

    def test_bad_log_paths_symlinks_hardlinks_and_oversize_refuse(self):
        target = self.base / "preserved.bin"
        target.write_bytes(b"untouched")
        symlink = self.base / "symbolic.jsonl"
        symlink.symlink_to(target)
        link_parent = self.base / "linked-parent"
        link_parent.symlink_to(self.base, target_is_directory=True)
        hardlink = self.base / "hardlinked.jsonl"
        os.link(target, hardlink)
        oversize = self.base / "oversize.jsonl"
        oversize.write_bytes(b"x" * (service.LOG_BYTES + 1))
        backup_symlink = self.base / "backup-link.jsonl"
        Path(str(backup_symlink) + ".1").symlink_to(target)
        bad = [Path("relative.jsonl"), self.base / "absent" / "events.jsonl", self.base,
               symlink, link_parent / "events.jsonl", hardlink, oversize, backup_symlink]
        for path in bad:
            with self.subTest(path=path), self.assertRaises((ValueError, OSError)):
                with service._events(path):
                    self.fail("Bad log path was admitted")
        self.assertEqual(target.read_bytes(), b"untouched")
        self.assertEqual(oversize.stat().st_size, service.LOG_BYTES + 1)
        self.assertFalse(backup_symlink.exists())


if __name__ == "__main__":
    unittest.main()
