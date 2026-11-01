"""Foreground login service; launchd owns startup, never task authorization.

No credentials, network adapters, task creation, implicit recovery, automatic
retry, STOP clearing or detached subprocess supervision is provided here.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import signal
import sqlite3
import stat
import time

from .common import BusyError, file_lock
from .controller import code_identity
from .deployment import environment_identity

POLL_SECONDS = 5.0
LOG_BYTES = 262144


def _stopped_or_work(controller):
    if (controller.home / "STOP").exists():
        return "STOPPED"
    try:
        # RUNNING is interrupted only when neither a manual controller nor an
        # orphaned worker owns custody. A busy lock is not a recovery diagnosis.
        with controller.locked(timeout=0), controller.connection() as db:
            if controller._meta(db, "stop_requested") == "1":
                return "STOPPED"
            if db.execute("SELECT 1 FROM tasks WHERE state='RUNNING' LIMIT 1").fetchone():
                return "RECOVERY_REQUIRED"
            if db.execute("SELECT 1 FROM tasks WHERE state='QUEUED' LIMIT 1").fetchone():
                return "QUEUED"
    except BusyError:
        return "STATE_BUSY"
    return "IDLE"


@contextmanager
def _events(path):
    path = Path(path)
    if not path.is_absolute() or not path.parent.is_dir():
        raise ValueError("Existing absolute event-log directory required")
    for parent in (path.parent, *path.parent.parents):
        if parent.is_symlink():
            raise ValueError("Event-log parent symlink refused")
    for candidate in (path, Path(str(path) + ".1")):
        if candidate.is_symlink():
            raise ValueError("Event-log symlink refused")
        if candidate.exists():
            info = candidate.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("Event log must be an ordinary private file")
            if info.st_size > LOG_BYTES:
                raise ValueError("Oversized existing log retained; inspect it before starting")
    handler = RotatingFileHandler(path, maxBytes=LOG_BYTES, backupCount=1, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    def emit(status, *, task=None, outcome=None):
        # Fixed enum-like labels and opaque IDs only. No input, rationale, path,
        # traceback, environment or exception message is written to this log.
        if not re.fullmatch(r"[A-Z_]{1,40}", status):
            raise ValueError("Invalid service event")
        item = {"schema": "nurarihyon-service-event/v1", "time": datetime.now(timezone.utc).isoformat(),
                "pid": os.getpid(), "status": status}
        if task is not None:
            if not re.fullmatch(r"[0-9a-f]{32}", task):
                raise ValueError("Invalid service task identity")
            item["task"] = task
        if outcome is not None:
            if not re.fullmatch(r"[A-Z_]{1,40}", outcome):
                raise ValueError("Invalid service outcome")
            item["outcome"] = outcome
        message = json.dumps(item, sort_keys=True, separators=(",", ":"))
        # Handler.emit ordinarily swallows filesystem errors. Fail closed if
        # the durable observation cannot be written instead of continuing work.
        record = logging.LogRecord("nurarihyon", logging.INFO, "", 0, message, (), None)
        if handler.shouldRollover(record):
            handler.doRollover()
        handler.stream.write(message + "\n")
        handler.flush()
    try:
        yield emit
    finally:
        handler.close()


def run_service(controller, *, log_file, expected_code_sha, expected_environment_sha):
    """One foreground owner until STOP, termination or a fail-closed boundary.

Per-operation CPU, wall, memory, output and admission limits remain owned by
Controller.run_once. The service never requeues or authorizes a task. Idle
polling is fixed at five seconds; there is no periodic process restart.
"""
    if type(expected_code_sha) is not str or not re.fullmatch(r"[0-9a-f]{64}", expected_code_sha):
        raise ValueError("An exact installed runtime SHA-256 is required")
    if type(expected_environment_sha) is not str or not re.fullmatch(r"[0-9a-f]{64}", expected_environment_sha):
        raise ValueError("An exact interpreter/launcher SHA-256 is required")
    previous = {}
    shutdown = [False]
    def terminate(signum, frame):
        # Never raise asynchronously across Popen assignment or SQLite/worker
        # cleanup, or acquire locks in a Python signal handler. The controller
        # checks this plain flag at safe cancellation points.
        shutdown[0] = True
    def cancelled():
        return shutdown[0]
    started = time.monotonic()
    cpu_started = time.process_time()
    def summary(status, operations=0):
        return {"status": status, "operations_observed": operations,
                "wall_seconds": time.monotonic() - started,
                "process_cpu_seconds": time.process_time() - cpu_started,
                "poll_seconds": POLL_SECONDS, "automatic_retry": False,
                "stop_cleared": False}
    try:
        with file_lock(controller.home / "executor.lock"):
            with _events(log_file) as emit:
                for name in (signal.SIGTERM, signal.SIGINT):
                    previous[name] = signal.signal(name, terminate)
                operations = 0
                try:
                    state = _stopped_or_work(controller)
                    if state in ("STOPPED", "RECOVERY_REQUIRED", "STATE_BUSY"):
                        emit(state)
                        return summary(state)
                    # Import the declared acceptance path before the startup
                    # identity checks, not lazily after a task is admitted.
                    from . import plans, inspection
                    import lodestone_machinery.workers
                    import lodestone_pando
                    if environment_identity() != expected_environment_sha:
                        emit("ENVIRONMENT_CHANGED")
                        return summary("ENVIRONMENT_CHANGED")
                    if code_identity() != expected_code_sha:
                        emit("RUNTIME_CHANGED")
                        return summary("RUNTIME_CHANGED")
                    try:
                        with controller.locked(timeout=2):
                            controller.verify()
                    except BusyError:
                        emit("STATE_BUSY")
                        return summary("STATE_BUSY")
                    emit("STARTED")
                    while True:
                        if cancelled():
                            emit("TERMINATED")
                            return summary("TERMINATED", operations)
                        state = _stopped_or_work(controller)
                        if state in ("STOPPED", "RECOVERY_REQUIRED"):
                            emit(state)
                            return summary(state, operations)
                        if state == "QUEUED":
                            try:
                                result = controller.run_once(expected_code_sha=expected_code_sha,
                                                             expected_environment_sha=expected_environment_sha,
                                                             cancel_requested=cancelled)
                            except BusyError:
                                time.sleep(POLL_SECONDS)
                                continue
                            if "id" in result:
                                operations += 1
                                emit("TASK_OBSERVED", task=result["id"], outcome=result["state"])
                            state = result.get("status", result.get("state"))
                            if cancelled():
                                emit("TERMINATED")
                                return summary("TERMINATED", operations)
                            outcome = (result.get("result") or {}).get("outcome")
                            metrics = (result.get("result") or {}).get("metrics", {})
                            if metrics.get("child_reap_error"):
                                state = "EXECUTOR_ERROR"
                            elif outcome in ("EXECUTOR_ERROR", "EXECUTOR_LIMIT", "RESOURCE_LIMIT"):
                                state = outcome
                            elif outcome == "CUSTODY_HELD":
                                # conscience-wiring-01: a custody hold is a durable host
                                # decision on one task, not an executor interruption.
                                emit("CUSTODY_HELD", task=result["id"])
                                state = "CUSTODY_HELD"
                            if state in ("STOPPED", "BLOCKED_STATE", "BLOCKED_RESOURCE", "RUNTIME_CHANGED",
                                         "ENVIRONMENT_CHANGED", "INTERRUPTED", "EXECUTOR_ERROR",
                                         "EXECUTOR_LIMIT", "RESOURCE_LIMIT"):
                                emit(state)
                                return summary(state, operations)
                        time.sleep(POLL_SECONDS)
                except KeyboardInterrupt:
                    emit("INTERRUPTED")
                    return summary("INTERRUPTED", operations)
                except (ValueError, OSError, RuntimeError, sqlite3.Error, TypeError, KeyError, ImportError):
                    emit("SERVICE_ERROR")
                    return summary("SERVICE_ERROR", operations)
    except BusyError:
        return summary("ALREADY_RUNNING")
    finally:
        for name, handler in previous.items():
            signal.signal(name, handler)

