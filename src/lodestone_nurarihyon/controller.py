"""Durable queue and independent publication control for local toolkit work."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import resource
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import uuid

from lodestone_pando.store import Board
from .common import (canonical, create_file, digest, file_lock, read_bytes,
                     strict_json, sync_directory, tree_inventory, tree_size, tree_usage, BusyError, StateBlocked, ApprovalStale)
from .policy import DEFAULT_POLICY, check_operation, validate_policy
from .conscience_binding import ConscienceBinding, CustodyUnavailable
from .inspection import ReadOnlyBoard, verify_captures


CUSTODY_PACKET_BYTES = 65536  # revision-01 (CW01): a held packet is retained twice (task result + event);
                              # twice this bound stays inside the 262144-byte admission overhead reserve.


class HostStopped(RuntimeError):
    """An acknowledged host stop is not a rejected source transformation."""


class ProcessCancelled(RuntimeError):
    """Process termination interrupts work without setting durable host STOP."""


def now():
    return datetime.now(timezone.utc).isoformat()


def _stop_child(child):
    """Reap the owned child even if its resource signal beat our group signal."""
    if child.poll() is None:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            if child.poll() is None:
                child.kill()  # Owned Popen PID; workers cannot spawn via declared APIs.
    child.wait(timeout=3)


def _children_cpu():
    value = resource.getrusage(resource.RUSAGE_CHILDREN)
    return value.ru_utime + value.ru_stime


def code_identity():
    """Root-independent source identity; executor restarts need no model session."""
    root = Path(__file__).resolve().parents[1]
    pins = []
    for package in ("lodestone_nurarihyon", "lodestone_pando", "lodestone_machinery"):
        for path in sorted((root / package).rglob("*.py")):
            pins.append([path.relative_to(root).as_posix(), digest(read_bytes(path, 2097152))])
    memory = root.parent / "tools/local-memory/src/lodestone_memory.py"
    if memory.is_file():
        pins.append(["tools/local-memory/src/lodestone_memory.py", digest(read_bytes(memory, 2097152))])
    return digest(canonical(pins))


def _view(board):
    value = board.settings()
    return {"board_name": value["name"], "enabled_shelves": value["shelves"]}


def _generation_inventory(path, policy):
    return [item for item in tree_inventory(path, max_bytes=policy["max_stage_bytes"],
                                           max_files=policy["max_files"])
            if item["path"] != "generation.json"]


class Controller:
    def __init__(self, home):
        candidate = Path(home).expanduser()
        if candidate.is_symlink():
            raise ValueError("State home cannot be a symlink")
        self.requested_home = candidate.absolute()
        self.home = candidate.resolve()
        self.database = self.home / "control.sqlite3"
        if not self.database.is_file() or self.database.is_symlink():
            raise ValueError("Existing local state required; use explicit init")
        self.policy = strict_json(read_bytes(self.home / "policy.json"))
        self.policy_sha = validate_policy(self.policy)
        # conscience-wiring-01: the sealed binding, if any, is loaded on first use.
        self.custody = ConscienceBinding(self.policy["conscience"], self.home) if "conscience" in self.policy else None
        with self.connection() as db:
            if self._meta(db, "schema") != "nurarihyon-state/v1":
                raise ValueError("Unsupported state schema")
            if self._meta(db, "policy_sha256") != self.policy_sha:
                raise ValueError("Host policy identity mismatch")

    @classmethod
    def initialize(cls, home, policy=None):
        home = Path(home).expanduser().absolute()
        policy = dict(DEFAULT_POLICY) if policy is None else policy
        policy_sha = validate_policy(policy)
        home.mkdir(parents=True, exist_ok=False, mode=0o700)
        for name in ("inputs", "generations", "staging"):
            (home / name).mkdir(mode=0o700)
        create_file(home / "policy.json", canonical(policy) + b"\n")
        initial = "0" * 32
        generation = home / "generations" / initial
        generation.mkdir()
        Board(generation / "pando").verify()
        manifest = {"schema": "nurarihyon-generation/v1", "id": initial, "parent": None,
                    "task_id": None, "files": _generation_inventory(generation, policy)}
        create_file(generation / "generation.json", canonical(manifest) + b"\n")
        db = sqlite3.connect(home / "control.sqlite3")
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.executescript("""
                CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE tasks(sequence INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE NOT NULL,
                  dedup_key TEXT UNIQUE NOT NULL,intent_sha TEXT NOT NULL,plan TEXT NOT NULL,
                  plan_sha TEXT NOT NULL,source_sha TEXT NOT NULL,base_generation TEXT NOT NULL,
                  policy_sha TEXT NOT NULL,code_sha TEXT NOT NULL,priority INTEGER NOT NULL,
                  state TEXT NOT NULL,approval TEXT,attempts INTEGER NOT NULL DEFAULT 0,
                  created TEXT NOT NULL,updated TEXT NOT NULL,result TEXT,error TEXT);
                CREATE TABLE events(sequence INTEGER PRIMARY KEY AUTOINCREMENT,task_id TEXT,
                  time TEXT NOT NULL,kind TEXT NOT NULL,payload TEXT NOT NULL);
                CREATE TABLE lessons(id INTEGER PRIMARY KEY AUTOINCREMENT,task_id TEXT NOT NULL,
                  body TEXT NOT NULL);
                CREATE TABLE generations(id TEXT PRIMARY KEY,parent TEXT,task_id TEXT UNIQUE,
                  manifest_sha TEXT NOT NULL,created TEXT NOT NULL);
                CREATE TRIGGER immutable_intent BEFORE UPDATE OF dedup_key,intent_sha,plan,
                  plan_sha,source_sha,base_generation,policy_sha,code_sha,priority ON tasks
                  BEGIN SELECT RAISE(ABORT,'Task intent is immutable'); END;
                CREATE TRIGGER immutable_events_update BEFORE UPDATE ON events
                  BEGIN SELECT RAISE(ABORT,'Events are append-only'); END;
                CREATE TRIGGER immutable_events_delete BEFORE DELETE ON events
                  BEGIN SELECT RAISE(ABORT,'Events are append-only'); END;
            """)
            db.executemany("INSERT INTO metadata VALUES(?,?)", [
                ("schema", "nurarihyon-state/v1"), ("policy_sha256", policy_sha),
                ("active_generation", initial), ("stop_requested", "0")])
            db.execute("INSERT INTO generations VALUES(?,?,?,?,?)",
                       (initial, None, None, digest(read_bytes(generation / "generation.json")), now()))
            db.commit()
        finally:
            db.close()
        sync_directory(home)
        return cls(home)

    @contextmanager
    def connection(self):
        if self.database.is_symlink():
            raise ValueError("Database symlink refused")
        db = sqlite3.connect(self.database, timeout=2)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def locked(self, *, timeout=2):
        with file_lock(self.home / "state.lock", timeout=timeout):
            # A child can outlive a killed controller. It cannot publish, but
            # snapshots/recovery must wait until its private writes stop.
            with file_lock(self.home / "worker.lock"):
                pass
            yield

    @staticmethod
    def _meta(db, key):
        row = db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        if row is None:
            raise ValueError("Missing control metadata: " + key)
        return row[0]

    @staticmethod
    def _event(db, task, kind, value):
        db.execute("INSERT INTO events(task_id,time,kind,payload) VALUES(?,?,?,?)",
                   (task, now(), kind, canonical(value).decode()))

    def _generation(self, identity):
        if type(identity) is not str or re.fullmatch(r"[0-9a-f]{32}", identity) is None:
            raise ValueError("Invalid generation identity")
        path = self.home / "generations" / identity
        if path.is_symlink() or not path.is_dir():
            raise ValueError("Missing or substituted generation")
        return path

    def active_directory(self):
        with self.connection() as db:
            return self._generation(self._meta(db, "active_generation"))

    def _row(self, db, identity):
        row = db.execute("SELECT * FROM tasks WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise ValueError("Unknown task")
        if digest(row["plan"].encode()) != row["plan_sha"]:
            raise ValueError("Task plan identity mismatch")
        return row

    def submit(self, source, *, key, priority=0, memory_root=None, toolkit="pando.profile/v1", reuse_reconciliation=False):
        from .plans import make_plan, priority_key
        check_operation({"toolkit": toolkit, "authority": "PROPOSAL_ONLY"}, self.policy)
        if type(reuse_reconciliation) is not bool or (reuse_reconciliation and toolkit != "bezoar.reconciliation/v1"):
            raise ValueError("Retained reconciliation routing requires its declared toolkit")
        if memory_root is not None and toolkit != "pando.profile/v1":
            raise ValueError("Existing outcome-context bridge supports profile tasks only")
        if memory_root is not None:
            from .context import check_context_home
            check_context_home(self)
        if type(key) is not str or not re.fullmatch(r"[A-Za-z0-9_.-]{1,96}", key):
            raise ValueError("Idempotency key must be a simple explicit identifier")
        priority_key({"priority": priority, "sequence": 0, "id": key})
        raw = read_bytes(Path(source).expanduser(), self.policy["max_source_bytes"])
        capture = None
        source_sha = digest(raw)
        intent = digest(canonical([toolkit, source_sha, priority]))
        if reuse_reconciliation:
            intent = digest(canonical([toolkit, source_sha, priority, "latest-same-corpus/v1"]))
        with self.locked():
            with self.connection() as db:
                existing = db.execute("SELECT * FROM tasks WHERE dedup_key=?", (key,)).fetchone()
                if existing:
                    if existing["intent_sha"] != intent:
                        raise ValueError("Idempotency key already binds different input or priority")
                    previous = self.status(existing["id"])
                    contexts = [e for e in previous["events"] if e["kind"] == "CONTEXT_ATTACHED"]
                    if memory_root is not None or contexts:
                        original = json.loads(contexts[0]["payload"])["context_sha256"] if contexts else None
                        previous["context_request"] = {
                            "requested": memory_root is not None,
                            "original_context_sha256": original,
                            "flag_mismatch": (memory_root is not None) != bool(contexts),
                            "effect": "Existing task returned unchanged; context is attached only on first submission"}
                    return previous
                if db.execute("SELECT count(*) FROM tasks").fetchone()[0] >= self.policy["max_tasks"]:
                    raise ValueError("Task retention limit reached; no automatic deletion")
            if toolkit in ("bezoar.reconciliation-capture/v1", "bezoar.reconciliation-observation/v1"):
                from .complete_capture import capture_module
                bezoar_capture = capture_module(toolkit)
                # Existing identical tasks return above, without replaying capture.
                # New inputs get full closure validation before durable writes.
                capture = bezoar_capture.preflight(raw, bezoar_capture.sibling(Path(source).expanduser().absolute()))
            self.verify()
            generation = self.active_directory()
            plan = make_plan(toolkit, raw, ReadOnlyBoard(generation / "pando").settings())
            if reuse_reconciliation:
                from .reconciliation import attach_route_plan
                plan = attach_route_plan(plan, ReadOnlyBoard(generation / "pando"), raw)
            check_operation(plan, self.policy)
            context = None
            context_event = None
            if memory_root is not None:
                from .context import context_locked
                context = context_locked(self, raw, plan, memory_root=memory_root)
                context_event = {
                    "schema": "nurarihyon-context-reference/v1",
                    "context_sha256": digest(canonical(context)),
                    "context_bytes": len(canonical(context)),
                    "source_sha256": source_sha, "plan_sha256": digest(canonical(plan)),
                    "authority": "NONE"}
                if len(canonical(context_event)) > 65536:
                    raise ValueError("Retained context event exceeds64KiB")
            context_size = len(canonical(context)) if context is not None else 0
            capture_size = 0
            if capture is not None:
                capture_size = capture["record_bytes"]
                required = bezoar_capture.stage_requirement(capture, raw, tree_size(generation / "pando"))
                if required > self.policy["max_stage_bytes"]:
                    raise ValueError("Complete capture cannot fit serialized stage reservation")
                entry_budget = bezoar_capture.entry_requirement(capture["manifest"], tree_usage(generation / "pando")[1])
                if (tree_usage(self.home)[1] + entry_budget["input"] + 2 * entry_budget["stage"] +64
                        > self.policy["max_files"]):
                    raise ValueError("Complete capture cannot fit filesystem-entry reservation")
            if tree_size(self.home) + len(raw) + context_size + capture_size + self.policy["max_stage_bytes"] * 2 > self.policy["max_state_bytes"]:
                raise ValueError("Insufficient reserved state budget")
            captured = self.home / "inputs" / (source_sha + ".json")
            if capture is not None:
                from lodestone_machinery.records import RecordStore
                target_bundle = bezoar_capture.sibling(captured)
                if target_bundle.exists():
                    if bezoar_capture.resolve(raw, bezoar_capture.ExistingStore(target_bundle), expected_sha256=source_sha) != capture:
                        raise ValueError("Existing admitted capture differs")
                else:
                    bezoar_capture.copy_records(capture, RecordStore(target_bundle))
            if captured.exists():
                if read_bytes(captured, self.policy["max_source_bytes"]) != raw:
                    raise ValueError("Captured source hash collision or drift")
            else:
                create_file(captured, raw)
            if context_event is not None:
                directory = self.home / "contexts"
                if directory.is_symlink():
                    raise ValueError("Context directory symlink refused")
                directory.mkdir(exist_ok=True, mode=0o700)
                captured_context = directory / (context_event["context_sha256"] + ".json")
                if captured_context.exists():
                    if read_bytes(captured_context, 65536) != canonical(context):
                        raise ValueError("Captured context identity mismatch")
                else:
                    create_file(captured_context, canonical(context))
            identity = uuid.uuid4().hex
            body = canonical(plan).decode()
            with self.connection() as db:
                db.execute("INSERT INTO tasks(id,dedup_key,intent_sha,plan,plan_sha,source_sha,"
                           "base_generation,policy_sha,code_sha,priority,state,created,updated) "
                           "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (identity, key, intent, body, digest(body.encode()), source_sha,
                            generation.name, self.policy_sha, code_identity(), priority, "PLANNED", now(), now()))
                self._event(db, identity, "PLANNED", {"source_sha256": source_sha, "authority": "NONE"})
                if context_event is not None:
                    self._event(db, identity, "CONTEXT_ATTACHED", context_event)
            return self.status(identity)

    def approve(self, identity, plan_sha):
        with self.locked(), self.connection() as db:
            row = self._row(db, identity)
            if row["state"] != "PLANNED" or row["plan_sha"] != plan_sha:
                raise ValueError("Approval must bind the exact pending plan")
            self._validate_intent(row)
            approval = canonical({"plan_sha256": plan_sha, "policy_sha256": self.policy_sha,
                                  "code_sha256": row["code_sha"], "at": now(), "actor": "host",
                                  "scope": "One staged " + json.loads(row["plan"])["toolkit"] + " operation; no external action or candidate activation"}).decode()
            db.execute("UPDATE tasks SET state='QUEUED',approval=?,updated=? WHERE id=?", (approval, now(), identity))
            self._event(db, identity, "APPROVED", json.loads(approval))
        return self.status(identity)

    def _validate_intent(self, row):
        from .plans import validate_plan, PlanError
        if row["policy_sha"] != self.policy_sha or validate_policy(strict_json(read_bytes(self.home / "policy.json"))) != self.policy_sha:
            raise StateBlocked("Host policy changed")
        if row["code_sha"] != code_identity():
            raise ApprovalStale("Executor or acceptance code changed since planning; new plan required")
        if self.active_directory().name != row["base_generation"]:
            raise PlanError("STALE_BASELINE", "Active generation changed; a new plan is required")
        raw = read_bytes(self.home / "inputs" / (row["source_sha"] + ".json"), self.policy["max_source_bytes"])
        if digest(raw) != row["source_sha"]:
            raise PlanError("SOURCE_DRIFT", "Captured source changed")
        plan = json.loads(row["plan"])
        check_operation(plan, self.policy)
        validate_plan(plan, raw, ReadOnlyBoard(self.active_directory() / "pando").settings())
        if plan["toolkit"] in ("bezoar.reconciliation-capture/v1", "bezoar.reconciliation-observation/v1"):
            from .complete_capture import capture_module
            bezoar_capture = capture_module(plan["toolkit"])
            source = self.home / "inputs" / (row["source_sha"] + ".json")
            bezoar_capture.resolve(raw, bezoar_capture.ExistingStore(bezoar_capture.sibling(source)), expected_sha256=row["source_sha"])
        if plan["toolkit"] == "bezoar.reconciliation/v1":
            from .reconciliation import validate_route_selection
            validate_route_selection(plan, ReadOnlyBoard(self.active_directory() / "pando"), raw)
        return raw, plan

    def status(self, identity=None):
        with self.connection() as db:
            if identity is not None:
                row = dict(self._row(db, identity))
                for key in ("plan", "approval", "result"):
                    row[key] = json.loads(row[key]) if row[key] else None
                row["events"] = [dict(e) for e in db.execute("SELECT * FROM events WHERE task_id=? ORDER BY sequence", (identity,))]
                return row
            return {"schema": "nurarihyon-status/v1", "active_generation": self._meta(db, "active_generation"),
                    "stopped": (self.home / "STOP").exists() or self._meta(db, "stop_requested") == "1",
                    "tasks": [dict(r) for r in db.execute("SELECT id,state,priority,attempts,plan_sha FROM tasks ORDER BY sequence")],
                    "lessons": [dict(r) for r in db.execute("SELECT * FROM lessons ORDER BY id")],
                    "retained_bytes": tree_usage(self.home, live=True)[0],
                    "retained_bytes_are_live_sample": True, "policy": self.policy}

    def stop(self):
        path = self.home / "STOP"
        try:
            create_file(path, canonical({"requested_at": now(), "actor": "host"}) + b"\n")
        except FileExistsError:
            if path.is_symlink():
                raise ValueError("Stop marker symlink refused")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE metadata SET value='1' WHERE key='stop_requested'")
        return {"stop_requested": True, "scope": "Current child and future admissions to execution; service polls this durable marker"}

    def clear_stop(self):
        with self.locked():
            path = self.home / "STOP"
            if path.is_symlink():
                raise ValueError("Stop marker symlink refused")
            with self.connection() as db:
                db.execute("UPDATE metadata SET value='0' WHERE key='stop_requested'")
            # A crash between these writes leaves the marker stopping work.
            path.unlink(missing_ok=True)
            sync_directory(self.home)
        return {"stop_cleared": True, "work_started": False}

    def verify(self):
        """Does not acquire state.lock; callers snapshot under that lock."""
        disk_policy = validate_policy(strict_json(read_bytes(self.home / "policy.json")))
        if disk_policy != self.policy_sha:
            raise ValueError("Host policy identity mismatch")
        size, entries = tree_usage(self.home)
        if size > self.policy["max_state_bytes"] or entries > self.policy["max_files"]:
            raise ValueError("State exceeds retained-byte policy")
        with self.connection() as db:
            if self._meta(db, "policy_sha256") != disk_policy:
                raise ValueError("Host policy metadata identity mismatch")
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Control database integrity failure")
            generations = [dict(r) for r in db.execute("SELECT * FROM generations")]
            current = self._meta(db, "active_generation")
            tasks = [dict(r) for r in db.execute("SELECT * FROM tasks")]
            context_count, context_bytes = db.execute(
                "SELECT count(*),max(length(CAST(payload AS BLOB))) FROM events WHERE kind='CONTEXT_ATTACHED'").fetchone()
            if context_count > self.policy["max_tasks"] or (context_bytes or 0) > 65536:
                raise ValueError("Retained context event bounds exceeded")
            context_events = [dict(r) for r in db.execute(
                "SELECT task_id,payload FROM events WHERE kind='CONTEXT_ATTACHED'")]
        seen = set()
        for row in generations:
            path = self._generation(row["id"])
            raw = read_bytes(path / "generation.json", 2097152)
            if digest(raw) != row["manifest_sha"]:
                raise ValueError("Generation manifest identity mismatch")
            value = strict_json(raw, 2097152)
            if value["id"] != row["id"] or value["parent"] != row["parent"] or value["task_id"] != row["task_id"]:
                raise ValueError("Generation lineage mismatch")
            if value["files"] != _generation_inventory(path, self.policy):
                raise ValueError("Generation payload identity mismatch")
            board = ReadOnlyBoard(path / "pando")
            board.verify()
            verify_captures(board)
            seen.add(row["id"])
        if current not in seen or any(r["parent"] and r["parent"] not in seen for r in generations):
            raise ValueError("Generation dependency missing")
        for task in tasks:
            if digest(task["plan"].encode()) != task["plan_sha"]:
                raise ValueError("Task plan identity mismatch")
            source = read_bytes(self.home / "inputs" / (task["source_sha"] + ".json"), self.policy["max_source_bytes"])
            if digest(source) != task["source_sha"]:
                raise ValueError("Captured source identity mismatch")
            if json.loads(task["plan"])["toolkit"] in ("bezoar.reconciliation-capture/v1", "bezoar.reconciliation-observation/v1"):
                from .complete_capture import capture_module
                bezoar_capture = capture_module(json.loads(task["plan"])["toolkit"])
                captured = self.home / "inputs" / (task["source_sha"] + ".json")
                bezoar_capture.resolve(source, bezoar_capture.ExistingStore(bezoar_capture.sibling(captured)), expected_sha256=task["source_sha"])
            if task["state"] == "COMPLETED" and not any(g["task_id"] == task["id"] for g in generations):
                raise ValueError("Completed task has no publication")
        if context_events:
            from .context import verify_context_events
            verify_context_events(self.home, context_events, tasks)
        return {"status": "INTACT", "generations": len(generations), "tasks": len(tasks),
                "active_generation": current, "scope": "Local identity, lineage and SQLite integrity, not general correctness or learning"}

    def _finish_failure(self, identity, code, error, metrics, stage=None):
        from .plans import lesson_for
        with self.connection() as db:
            row = self._row(db, identity)
            if row["state"] == "COMPLETED":
                return  # A committed publication is never rolled back by a lost response.
            state = "INTERRUPTED" if code in ("STOPPED", "INTERRUPTED", "EXECUTOR_ERROR", "EXECUTOR_LIMIT") else "REJECTED"
            result = {"outcome": code, "metrics": metrics,
                      "retained_stage": stage.relative_to(self.home).as_posix() if stage else None,
                      "active_generation_unchanged": self._meta(db, "active_generation") == row["base_generation"]}
            db.execute("UPDATE tasks SET state=?,result=?,error=?,updated=? WHERE id=?",
                       (state, canonical(result).decode(), str(error)[:1500], now(), identity))
            self._event(db, identity, code, result)
            if code in ("SOURCE_DRIFT", "STALE_BASELINE", "RESOURCE_LIMIT", "REJECTED_VALIDATION"):
                lesson = lesson_for(json.loads(row["plan"])["toolkit"], code, row["source_sha"])
                db.execute("INSERT INTO lessons(task_id,body) VALUES(?,?)", (identity, canonical(lesson).decode()))

    def _hold_custody(self, identity, custody):
        """Record a non-ALLOW custody verdict as a host decision: no attempt, stage or lesson."""
        with self.connection() as db:
            row = self._row(db, identity)
            if row["state"] != "QUEUED":
                raise ValueError("Custody hold requires the admitted queued task")
            result = {"outcome": "CUSTODY_HELD", "verdict": custody["verdict"], "attempt_charged": False,
                      "retained_stage": None, "custody": custody,
                      "active_generation_unchanged": self._meta(db, "active_generation") == row["base_generation"],
                      "next_action": "Host decision; a changed Conscience policy needs a newly initialized state, retry is explicit, no automatic replay"}
            db.execute("UPDATE tasks SET state='INTERRUPTED',result=?,error=?,updated=? WHERE id=?",
                       (canonical(result).decode(), "Conscience custody verdict " + custody["verdict"] + "; host decision required",
                        now(), identity))
            self._event(db, identity, "CUSTODY_HELD", result)
        return self.status(identity)

    def _validate_stage(self, stage, task, plan):
        """Acceptance remains outside the worker and the staged transformation."""
        from .policy import operation_limits
        effective_policy = operation_limits(self.policy, plan["toolkit"])
        if tree_size(stage) > effective_policy["max_stage_bytes"]:
            raise ValueError("Stage exceeds byte policy")
        tree_inventory(stage, max_bytes=self.policy["max_stage_bytes"], max_files=self.policy["max_files"])
        report = strict_json(read_bytes(stage / "worker-result.json"))
        if report.get("schema") != "nurarihyon-worker-result/v1" or report.get("task_id") != task["id"]:
            raise ValueError("Worker response identity mismatch")
        board = ReadOnlyBoard(stage / "pando")
        board.verify()
        verify_captures(board)
        parent_board = ReadOnlyBoard(self._generation(task["base_generation"]) / "pando")
        if plan["toolkit"] in ("bezoar.reconciliation-capture/v1", "bezoar.reconciliation-observation/v1"):
            from .complete_capture import validate_stage
            from lodestone_machinery.bezoar_capture import sibling
            captured = self.home / "inputs" / (task["source_sha"] + ".json")
            raw = read_bytes(captured, self.policy["max_source_bytes"])
            return validate_stage(board, parent_board, raw, task, plan, report, effective_policy,
                                  stage=stage, bundle=sibling(captured))
        if plan["toolkit"] == "bezoar.reconciliation/v1":
            from .reconciliation import validate_reconciliation_stage
            raw = read_bytes(self.home / "inputs" / (task["source_sha"] + ".json"), self.policy["max_source_bytes"])
            return validate_reconciliation_stage(board, parent_board, raw, task, plan, report, effective_policy,
                                                 stage=stage)
        if plan["toolkit"] == "bezoar.document/v1":
            from .documents import validate_document_stage
            for item in tree_inventory(stage, max_bytes=self.policy["max_stage_bytes"], max_files=self.policy["max_files"]):
                if (not item["path"].startswith("pando/")
                        and item["path"] not in {"request.json", "worker.log", "worker-result.json"}):
                    raise ValueError("Document stage contains an undeclared file")
            raw = read_bytes(self.home / "inputs" / (task["source_sha"] + ".json"), self.policy["max_source_bytes"])
            return validate_document_stage(board, parent_board, raw, task, plan, report, self.policy)
        parent_state, child_state = parent_board.state(), board.state()
        if parent_state["posts"] != child_state["posts"]:
            raise ValueError("Profile operation changed unrelated Pando posts")
        for collection in ("sources", "artifacts"):
            children = {entry["id"]: entry for entry in child_state[collection]}
            for entry in parent_state[collection]:
                if children.get(entry["id"]) != entry:
                    raise ValueError("Profile operation changed existing Pando history")
        for item in tree_inventory(parent_board.home, max_bytes=self.policy["max_stage_bytes"]):
            if item["path"] != "pando.sqlite3":
                if digest(read_bytes(board.home / item["path"], self.policy["max_stage_bytes"])) != item["sha256"]:
                    raise ValueError("Profile operation changed an existing immutable record")
        if _view(board) != plan["candidate"]:
            raise ValueError("Staged view does not satisfy the approved target")
        source = board.source(report["source_id"])
        if source["sha256"] != task["source_sha"]:
            raise ValueError("Staged source does not match captured task source")
        baseline = board.artifact(report["baseline_artifact"])
        candidate = board.artifact(report["candidate_artifact"])
        if (baseline["stage"] != "DIGEST" or candidate["stage"] != "MUTATE"
                or candidate["parent_id"] != baseline["id"]
                or candidate["source_ids"] != [report["source_id"]]
                or baseline["record"]["payload"]["profile"]["operators"] != plan["baseline"]
                or candidate["record"]["payload"]["profile"]["operators"] != plan["candidate"]):
            raise ValueError("Staged transformation provenance mismatch")
        mutagen = candidate["record"]["payload"]["mutagen"]
        if (mutagen["operator"] != plan["operator"]
                or mutagen["expected"] != plan["baseline"][plan["operator"]]
                or mutagen["replacement"] != plan["candidate"][plan["operator"]]):
            raise ValueError("Staged transformation differs from approved plan")
        relative = report["brief_path"]
        if type(relative) is not str or Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise ValueError("Invalid brief path")
        job = report.get("brief_job")
        if type(job) is not str or re.fullmatch(r"[0-9a-f]{32}", job) is None or relative != f"machinery/jobs/{job}/brief.md":
            raise ValueError("Brief is not in its declared toolkit job")
        brief_path = stage / relative
        if digest(read_bytes(brief_path, 1048576)) != report["brief_sha256"]:
            raise ValueError("Literal brief identity mismatch")
        # Independently repeat the existing deterministic acceptance function,
        # not the toolkit execution. This is not another model or research run.
        from lodestone_machinery.workers import MechanicalAuditor
        folded = strict_json(read_bytes(brief_path.parent / "folded.json", 2097152), 2097152)
        name = task["source_sha"] + ".json"
        expected_source = {"id": name, "path": name, "sha256": task["source_sha"],
                           "bytes": len(read_bytes(self.home / "inputs" / name, self.policy["max_source_bytes"]))}
        if (folded.get("sources") != [expected_source] or folded.get("query") != "board_name"
                or folded.get("max_passages") != 4
                or not folded.get("passages") or len(folded["passages"]) > 4
                or any(p.get("source") != name or p.get("source_sha256") != task["source_sha"] for p in folded["passages"])):
            raise ValueError("Brief source/query is not bound to this task")
        checked = MechanicalAuditor().audit(self.home / "inputs", folded, read_bytes(brief_path, 1048576).decode())
        if checked.get("verdict") != "PASS":
            raise ValueError("Independent literal-brief acceptance failed")
        if board.settings()["artifact_id"] != candidate["id"]:
            raise ValueError("Applied view points to a different candidate")
        return report

    def run_once(self, *, fault=None, wall_limit=None, expected_code_sha=None,
                 expected_environment_sha=None, cancel_requested=None):
        """At most one admitted operation. Fault injection is explicit test-only API."""
        from .plans import priority_key, PlanError
        permitted_faults = {None, "wait", "cpu", "memory", "output", "exit", "before_commit", "after_commit", "crash_before_commit"}
        if fault not in permitted_faults:
            raise ValueError("Unknown test fault")
        if expected_code_sha is not None and (type(expected_code_sha) is not str or not re.fullmatch(r"[0-9a-f]{64}", expected_code_sha)):
            raise ValueError("Invalid expected runtime identity")
        if expected_environment_sha is not None and (type(expected_environment_sha) is not str or not re.fullmatch(r"[0-9a-f]{64}", expected_environment_sha)):
            raise ValueError("Invalid expected interpreter/launcher identity")
        if cancel_requested is not None and not callable(cancel_requested):
            raise ValueError("Invalid cancellation callback")
        def interruption():
            if (self.home / "STOP").exists():
                return "STOPPED"
            if cancel_requested is not None and cancel_requested():
                return "INTERRUPTED"
            return None
        worker_budget = self.policy["worker_wall_seconds"]
        supervisor_budget = self.policy.get("supervisor_wall_seconds", worker_budget)
        declared_total = worker_budget + self.policy.get("supervisor_wall_seconds", 0)
        budget = declared_total if wall_limit is None else min(declared_total, wall_limit)
        if type(budget) not in (int, float) or not 0 < budget <= declared_total:
            raise ValueError("Invalid execution wall budget")
        if interruption():
            return {"status": interruption(), "work_started": False}
        with self.locked(timeout=0):
            with self.connection() as db:
                if self._meta(db, "stop_requested") == "1":
                    return {"status": "STOPPED", "work_started": False}
                if db.execute("SELECT id FROM tasks WHERE state='RUNNING'").fetchone():
                    raise ValueError("Interrupted execution requires explicit recover before another run")
                rows = [dict(r) for r in db.execute("SELECT * FROM tasks WHERE state='QUEUED'")]
            if not rows:
                return {"status": "IDLE", "work_started": False}
            # A foreground session may end; it must not shorten an admitted
            # operation's declared budget or spend its attempt on that deadline.
            if budget < declared_total:
                return {"status": "WINDOW_TOO_SHORT", "work_started": False,
                        "required_wall_seconds": declared_total, "available_seconds": budget,
                        "next_action": "Start a longer foreground session or use explicit run; queued tasks were not altered"}
            started = time.monotonic()
            cpu_started = time.process_time()
            children_cpu_started = _children_cpu()
            # Global custody/capacity failures never consume an innocent task.
            try:
                self.verify()
            except (ValueError, OSError, sqlite3.Error) as exc:
                return {"status": "BLOCKED_STATE", "work_started": False, "error": str(exc)[:1500],
                        "preflight_wall_seconds": time.monotonic() - started,
                        "next_action": "Inspect state and restore a verified copy if necessary; no queued task was altered"}
            total_size, total_entries = tree_usage(self.home)
            clone_entries = tree_usage(self.active_directory() / "pando")[1]
            if interruption():
                return {"status": interruption(), "work_started": False}
            if (total_size + self.policy["max_stage_bytes"] + 262144 > self.policy["max_state_bytes"]
                    or total_entries + clone_entries + 128 > self.policy["max_files"]
                    or time.monotonic() - started > min(supervisor_budget, budget)):
                return {"status": "BLOCKED_RESOURCE", "work_started": False,
                        "preflight_wall_seconds": time.monotonic() - started,
                        "next_action": "State/verification reserve exhausted; preserve and export history, no automatic deletion or limit increase"}
            # A persistent executor's loaded version must still match disk.
            # Fail at service level before mutating even a stale task approval.
            if expected_code_sha is not None and code_identity() != expected_code_sha:
                return {"status": "RUNTIME_CHANGED", "work_started": False}
            if expected_environment_sha is not None:
                from .deployment import environment_identity
                if environment_identity() != expected_environment_sha:
                    return {"status": "ENVIRONMENT_CHANGED", "work_started": False}
            row = min(rows, key=priority_key)
            identity = row["id"]
            try:
                raw, plan = self._validate_intent(row)
            except ApprovalStale as exc:
                with self.connection() as db:
                    db.execute("UPDATE tasks SET state='REPLAN_REQUIRED',error=?,updated=? WHERE id=?",
                               (str(exc), now(), identity))
                    self._event(db, identity, "APPROVAL_STALE", {"work_started": False, "attempt_charged": False,
                                                              "next_action": "New plan and explicit approval with a new key"})
                return self.status(identity)
            except StateBlocked as exc:
                return {"status": "BLOCKED_STATE", "work_started": False, "error": str(exc),
                        "next_action": "Code/policy dependency changed; create a new plan with explicit approval"}
            except PlanError as exc:
                self._finish_failure(identity, exc.code, str(exc), {"work_started": False})
                return self.status(identity)
            if plan["toolkit"] in ("bezoar.reconciliation-capture/v1", "bezoar.reconciliation-observation/v1"):
                from .complete_capture import capture_module
                bezoar_capture = capture_module(plan["toolkit"])
                capture_manifest = bezoar_capture.manifest(raw, row["source_sha"])
                entry_budget = bezoar_capture.entry_requirement(capture_manifest, clone_entries)
                if tree_usage(self.home)[1] + entry_budget["stage"] +32 > self.policy["max_files"]:
                    return {"status": "BLOCKED_RESOURCE", "work_started": False,
                            "preflight_wall_seconds": time.monotonic() - started,
                            "next_action": "Declared complete capture cannot fit retained entry bound; no task attempt charged"}
            if expected_code_sha is not None and row["code_sha"] != expected_code_sha:
                # A persistent executor may still have an older module loaded
                # even when a new valid on-disk plan has been approved.
                return {"status": "RUNTIME_CHANGED", "work_started": False,
                        "next_action": "Stop the old executor and inspect the installed runtime; no task was admitted"}
            if interruption():
                return {"status": interruption(), "work_started": False}
            if time.monotonic() - started > min(supervisor_budget, budget):
                return {"status": "BLOCKED_RESOURCE", "work_started": False,
                        "preflight_wall_seconds": time.monotonic() - started,
                        "next_action": "Pre-admission validation exceeded its controller budget; no task attempt charged"}
            # conscience-wiring-01: the exact worker command is assessed for
            # custody before any attempt is charged (W1). The binding is part of
            # the sealed policy identity, never task or document bytes (W2).
            attempt = uuid.uuid4().hex
            planned_stage = self.home / "generations" / attempt
            command = [sys.executable, "-B", "-m", "lodestone_nurarihyon.worker", str(planned_stage),
                       str(self.home / "inputs" / (row["source_sha"] + ".json")), str(planned_stage / "request.json"),
                       "--parent-pid", str(os.getpid()), "--worker-lock", str(self.home / "worker.lock")]
            if fault in ("wait", "cpu", "memory", "output", "exit"):
                # revision-01 (CW02): the test-only fault flag is part of the exact
                # command before assessment; the unchanged adapter refuses it for
                # reconciliation, so no attempt is charged. Profile/document faults
                # keep their behaviour: same argv reaches Popen as before.
                command += ["--fault", fault]
            custody = None
            if plan["toolkit"] in ("bezoar.reconciliation/v1", "bezoar.reconciliation-capture/v1", "bezoar.reconciliation-observation/v1"):
                try:
                    if self.custody is None:
                        raise CustodyUnavailable("Reconciliation task without a sealed Conscience binding")
                    custody = self.custody.assess(stage=planned_stage,
                                                  parent=ReadOnlyBoard(self.active_directory() / "pando"),
                                                  raw=raw, task={"id": identity, "plan": plan}, argv=list(command),
                                                  interpreter=sys.executable, parent_pid=os.getpid())
                except CustodyUnavailable as exc:
                    return {"status": "BLOCKED_STATE", "work_started": False, "error": str(exc)[:1500],
                            "preflight_wall_seconds": time.monotonic() - started,
                            "next_action": "Conscience binding or wiring is unusable; verify the sealed pins, no task was altered"}
                if interruption():
                    return {"status": interruption(), "work_started": False}
                # revision-01 (CW01): elapsed time and serialized size are bounded
                # before either persistence branch. An oversize packet is never
                # retained and a refusal is never truncated into ALLOW.
                if time.monotonic() - started > min(supervisor_budget, budget):
                    return {"status": "BLOCKED_RESOURCE", "work_started": False,
                            "preflight_wall_seconds": time.monotonic() - started,
                            "next_action": "Custody assessment exceeded the controller budget; no task attempt charged"}
                packet_bytes = len(canonical(custody))
                if packet_bytes > CUSTODY_PACKET_BYTES:
                    return {"status": "BLOCKED_RESOURCE", "work_started": False,
                            "preflight_wall_seconds": time.monotonic() - started,
                            "custody": {"verdict": custody["verdict"], "packet_bytes": packet_bytes,
                                        "limit_bytes": CUSTODY_PACKET_BYTES,
                                        "command_rules": len(custody["assessment"]["command_assessment"]["rules"]),
                                        "effects_rules": len(custody["assessment"]["effects_assessment"])},
                            "next_action": "Custody packet exceeds the retained-state bound; nothing was persisted and no task attempt charged; a bound Conscience policy with fewer overlapping custody entries needs a newly initialized state"}
                if custody["verdict"] != "ALLOW":
                    return self._hold_custody(identity, custody)
            stage = None
            child = None
            metrics = {"rss_enforcement": "Sampled process RSS; may overshoot between samples; not an allocator hard cap",
                       "rss_observer": "Darwin proc_pidinfo task RSS" if sys.platform == "darwin" else "Linux procfs statm RSS in bytes",
                       "peak_sampled_rss_bytes": 0, "wall_seconds": 0.0, "stage_bytes": 0,
                       "preflight_wall_seconds": time.monotonic() - started,
                       "worker_wall_limit_seconds": worker_budget, "supervisor_wall_limit_seconds": supervisor_budget,
                       "total_wall_limit_seconds": budget,
                       "supervisor_wall_scope": "Preflight/copy/acceptance/publication preparation, excluding concurrent child interval; cooperative checks",
                       "cpu_enforcement": "OS RLIMIT_CPU", "file_enforcement": "OS RLIMIT_FSIZE plus total stage checks"}
            child_started = None
            child_elapsed = 0.0
            def timed():
                metrics["wall_seconds"] = time.monotonic() - started
                metrics["worker_wall_seconds"] = child_elapsed
                metrics["supervisor_wall_seconds"] = max(0.0, metrics["wall_seconds"] - child_elapsed)
                metrics["supervisor_cpu_seconds"] = time.process_time() - cpu_started
                metrics["reaped_children_cpu_seconds"] = _children_cpu() - children_cpu_started
                metrics["total_cpu_seconds"] = metrics["supervisor_cpu_seconds"] + metrics["reaped_children_cpu_seconds"]
                metrics["cpu_accounting_scope"] = "Current controller process plus reaped worker and any platform-fallback sampling children since preflight; worker self-report is a subset, never added twice"
                metrics["wall_measurement_scope"] = "Through the last check inside publication transaction; SQLite atomic commit/return tail is not included or hard-preempted"
            def exceeded():
                timed()
                if metrics["supervisor_wall_seconds"] > supervisor_budget or metrics["wall_seconds"] > budget:
                    return "EXECUTOR_LIMIT"
                if child_elapsed > worker_budget:
                    return "RESOURCE_LIMIT"
                return None
            try:
                if row["attempts"] >= self.policy["max_attempts"]:
                    raise ValueError("Task attempt limit reached")
                with self.connection() as db:
                    if custody is not None:
                        # Durable before STARTED: the assessment precedes the charge and
                        # the assessed argv is the executed argv (revision-01, CW02).
                        self._event(db, identity, "CUSTODY_ASSESSED", custody)
                    db.execute("UPDATE tasks SET state='RUNNING',attempts=attempts+1,updated=? WHERE id=?", (now(), identity))
                    self._event(db, identity, "STARTED", {"base_generation": row["base_generation"]})
                approval = json.loads(row["approval"] or "null")
                if not approval or approval["plan_sha256"] != row["plan_sha"] or approval["code_sha256"] != row["code_sha"]:
                    raise ValueError("Missing exact host approval")
                # Final location from creation: absolute captured-source roots
                # remain usable. Only the database pointer activates a version.
                stage = planned_stage
                stage.mkdir()
                shutil.copytree(self.active_directory() / "pando", stage / "pando")
                from .policy import operation_limits
                effective_limits = operation_limits(self.policy, plan["toolkit"])
                request = {"id": identity, "plan": plan, "source_sha256": row["source_sha"], "limits": effective_limits}
                create_file(stage / "request.json", canonical(request) + b"\n")
                environment = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
                               "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1"}
                with (stage / "worker.log").open("xb") as log:
                    if interruption():
                        timed()
                        self._finish_failure(identity, interruption(), "Execution cancelled before worker creation", metrics, stage)
                        return self.status(identity)
                    if exceeded():
                        self._finish_failure(identity, "EXECUTOR_LIMIT", "Supervisor preparation exceeded budget", metrics, stage)
                        return self.status(identity)
                    from .process_rss import owned_worker_observer, sample_owned_worker
                    rss_observer = owned_worker_observer()
                    child_started = time.monotonic()
                    child = subprocess.Popen(command, cwd=stage, env=environment, stdout=log, stderr=subprocess.STDOUT,
                                             start_new_session=True)
                    breach = None
                    while child.poll() is None:
                        child_elapsed = time.monotonic() - child_started
                        if interruption():
                            breach = interruption()
                        else:
                            breach = exceeded()
                        rss = sample_owned_worker(child, rss_observer)
                        if rss is not None:
                            metrics["peak_sampled_rss_bytes"] = max(metrics["peak_sampled_rss_bytes"], rss)
                            if rss > self.policy["worker_rss_bytes"] and breach not in ("STOPPED", "INTERRUPTED"):
                                breach = "RESOURCE_LIMIT"
                        metrics["stage_bytes"] = tree_usage(stage, live=True)[0]
                        if metrics["stage_bytes"] > effective_limits["max_stage_bytes"] and breach not in ("STOPPED", "INTERRUPTED"):
                            breach = "RESOURCE_LIMIT"
                        if tree_usage(self.home, live=True)[1] > self.policy["max_files"] and breach not in ("STOPPED", "INTERRUPTED"):
                            breach = "RESOURCE_LIMIT"
                        if breach:
                            _stop_child(child)
                            break
                        time.sleep(0.05)
                    metrics["exit_code"] = child.wait()
                child_elapsed = time.monotonic() - child_started
                timed()
                metrics["stage_bytes"] = tree_size(stage)
                if breach:
                    self._finish_failure(identity, breach, "Host stop or resource budget interrupted the stage", metrics, stage)
                    return self.status(identity)
                if child.returncode != 0:
                    reason = "RESOURCE_LIMIT" if child.returncode in (-signal.SIGKILL, -signal.SIGXCPU, -signal.SIGXFSZ, -signal.SIGALRM) else "REJECTED_VALIDATION"
                    self._finish_failure(identity, reason, "Toolkit child failed; inspect retained worker.log", metrics, stage)
                    return self.status(identity)
                self._validate_intent(row)  # Policy, code, source and baseline gate remains external.
                report = self._validate_stage(stage, row, plan)
                timed()
                metrics["worker_cpu_seconds"] = report["worker_cpu_seconds"]
                metrics["worker_maxrss_bytes"] = report["worker_maxrss_bytes"]
                limit_code = exceeded()
                if (limit_code
                        or report["worker_maxrss_bytes"] > self.policy["worker_rss_bytes"]
                        or report["worker_cpu_seconds"] > self.policy["worker_cpu_seconds"]):
                    self._finish_failure(identity, limit_code or "RESOURCE_LIMIT", "Final resource readback exceeds policy", metrics, stage)
                    return self.status(identity)
                manifest = {"schema": "nurarihyon-generation/v1", "id": attempt,
                            "parent": row["base_generation"], "task_id": identity,
                            "files": _generation_inventory(stage, self.policy)}
                create_file(stage / "generation.json", canonical(manifest) + b"\n")
                metrics["stage_bytes"] = tree_size(stage)
                limit_code = exceeded()
                if metrics["stage_bytes"] > effective_limits["max_stage_bytes"] or limit_code:
                    self._finish_failure(identity, limit_code or "RESOURCE_LIMIT", "Final manifest exceeds admitted stage budget", metrics, stage)
                    return self.status(identity)
                if fault == "before_commit":
                    raise ValueError("Injected failure before publication")
                if fault == "crash_before_commit":
                    os._exit(72)
                if interruption():
                    self._finish_failure(identity, interruption(), "Execution cancelled before publication", metrics, stage)
                    return self.status(identity)
                published = self.home / "generations" / attempt
                sync_directory(published.parent)
                limit_code = exceeded()
                if limit_code:
                    self._finish_failure(identity, limit_code, "Publication preparation exceeded controller budget", metrics, stage)
                    return self.status(identity)
                # One transaction publishes pointer, completion and side-effect identity.
                # A directory created before this commit is inert, not the active view.
                with self.connection() as db:
                    db.execute("BEGIN IMMEDIATE")
                    if self._meta(db, "stop_requested") == "1" or (self.home / "STOP").exists():
                        raise HostStopped("Host stop acknowledged before publication")
                    if interruption():
                        raise ProcessCancelled("Process termination acknowledged before publication")
                    if self._meta(db, "active_generation") != row["base_generation"]:
                        raise PlanError("STALE_BASELINE", "Publication compare-and-swap failed")
                    if exceeded():
                        raise TimeoutError("Publication transaction acquisition exceeded controller budget")
                    db.execute("INSERT INTO generations VALUES(?,?,?,?,?)",
                               (attempt, row["base_generation"], identity, digest(read_bytes(published / "generation.json", 2097152)), now()))
                    db.execute("UPDATE metadata SET value=? WHERE key='active_generation'", (attempt,))
                    result = {"outcome": "COMPLETED", "generation": attempt, "parent": row["base_generation"],
                              "metrics": metrics, "candidate_artifact": report["candidate_artifact"],
                              "acceptance": "Independent structural/source/target/recovery validation; no learned utility claim"}
                    db.execute("UPDATE tasks SET state='COMPLETED',result=?,updated=? WHERE id=?",
                               (canonical(result).decode(), now(), identity))
                    self._event(db, identity, "PUBLISHED", result)
                if fault == "after_commit":
                    os._exit(73)
                return self.status(identity)
            except BaseException as exc:
                reap_error = None
                if child is not None and child.poll() is None:
                    try:
                        _stop_child(child)
                    except (OSError, subprocess.SubprocessError) as stop_exc:
                        # Retain a failed stop instead of escaping before the
                        # outcome is durable. The worker retains its own lock,
                        # so recovery/export remain blocked until it exits.
                        reap_error = str(stop_exc)[:1000]
                        metrics["child_reap_error"] = reap_error
                if child_started is not None and child is not None:
                    child_elapsed = time.monotonic() - child_started if not child_elapsed else child_elapsed
                timed()
                text = str(exc)
                code = exc.code if isinstance(exc, PlanError) else "REJECTED_VALIDATION"
                if isinstance(exc, (OSError, subprocess.SubprocessError, sqlite3.Error, StateBlocked, ImportError)):
                    code = "EXECUTOR_ERROR"
                if isinstance(exc, TimeoutError):
                    code = "EXECUTOR_LIMIT"
                if reap_error is not None:
                    code = "EXECUTOR_ERROR"
                    text += "; child stop/reap not confirmed: " + reap_error
                if isinstance(exc, (ProcessCancelled, KeyboardInterrupt, SystemExit)) or interruption() == "INTERRUPTED":
                    code = "INTERRUPTED"
                if isinstance(exc, HostStopped) or (self.home / "STOP").exists():
                    code = "STOPPED"
                self._finish_failure(identity, code, text, metrics, stage)
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                return self.status(identity)

    def recover(self):
        with self.locked():
            self.verify()
            with self.connection() as db:
                running = [r[0] for r in db.execute("SELECT id FROM tasks WHERE state='RUNNING'")]
            for identity in running:
                self._finish_failure(identity, "INTERRUPTED", "Executor exited before publication; no automatic replay", {})
            return {"reconciled_tasks": running, "active_generation": self.active_directory().name,
                    "uncommitted_stages_retained": True, "work_restarted": False}

    def retry(self, identity):
        with self.locked(), self.connection() as db:
            row = self._row(db, identity)
            if row["state"] not in ("INTERRUPTED", "REJECTED"):
                raise ValueError("Only an explicitly reviewed failed/interrupted task can retry")
            if row["attempts"] >= self.policy["max_attempts"]:
                raise ValueError("Task attempt limit reached; no automatic new task")
            self._validate_intent(row)
            db.execute("UPDATE tasks SET state='QUEUED',error=NULL,updated=? WHERE id=?", (now(), identity))
            self._event(db, identity, "HOST_RETRY", {"previous_state": row["state"], "same_plan": row["plan_sha"]})
        return self.status(identity)

    def serve(self, *, seconds=60):
        if type(seconds) is not int or not 1 <= seconds <= 3600:
            raise ValueError("Manual foreground service duration must be 1..3600 seconds")
        until = time.monotonic() + seconds
        stopped = False
        with file_lock(self.home / "executor.lock"):
            while time.monotonic() < until:
                try:
                    result = self.run_once(wall_limit=max(0.01, until - time.monotonic()))
                except BusyError:
                    time.sleep(min(0.1, max(0, until - time.monotonic())))
                    continue
                if result.get("status") in ("BLOCKED_STATE", "BLOCKED_RESOURCE", "WINDOW_TOO_SHORT"):
                    return {**result, "automatic_startup": False}
                if result.get("status") == "STOPPED" or (self.home / "STOP").exists():
                    stopped = True
                    break
                if result.get("status") == "IDLE":
                    time.sleep(0.1)
            return {"status": "STOPPED" if stopped else "DURATION_COMPLETE",
                    "automatic_startup": False}
