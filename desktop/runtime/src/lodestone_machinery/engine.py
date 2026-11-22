"""Durable local task execution; source bodies are read-only inputs.

SQLite records intent, state and append-only events. Content-addressed blobs
support recovery of generated outputs. Neither mechanism establishes correctness:
completion requires an artifact inspection and explicit outcome criteria.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any
import uuid

from .organs import anchor, fold, measure, residue
from .records import RecordStore, route_result
from .workers import LiteralExtractWorker, MechanicalAuditor, WorkerRequest, WorkerReturn


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Engine:
    """A workspace is an explicit writable output directory, never a source store.

    Workers are registered by name in each process; jobs persist their worker and
    auditor names. Default roles are distinct functions in the same process, not
    independent human/model agents. Custom Python workers are trusted local code.
    """

    def __init__(self, workspace: str | Path):
        self.workspace = Path(workspace).expanduser().resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        for name in ("jobs", "blobs", "locks", "damaged"):
            self._path(name).mkdir(exist_ok=True)
        self.database = self._path("tasks.sqlite3")
        self.workers = {"literal_extract": LiteralExtractWorker()}
        self.auditors = {"mechanical": MechanicalAuditor()}
        self.records = RecordStore(self.workspace)
        with self._db() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
              id TEXT PRIMARY KEY, envelope_id TEXT NOT NULL, envelope TEXT NOT NULL,
              state TEXT NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL,
              result TEXT, error TEXT
            );
            CREATE TABLE IF NOT EXISTS events (
              sequence INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
              time TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL
            );
            CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
              BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
            CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
              BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
            CREATE TRIGGER IF NOT EXISTS frozen_intent BEFORE UPDATE OF envelope, envelope_id ON jobs
              BEGIN SELECT RAISE(ABORT, 'job intent is immutable'); END;
            CREATE TABLE IF NOT EXISTS artifacts (
              job_id TEXT NOT NULL, name TEXT NOT NULL, path TEXT NOT NULL,
              sha256 TEXT NOT NULL, bytes INTEGER NOT NULL,
              PRIMARY KEY(job_id, name)
            );
            """)

    def _path(self, relative: str) -> Path:
        path = self.workspace / relative
        if not path.resolve().is_relative_to(self.workspace):
            raise ValueError("Workspace path or symlink escapes output boundary")
        return path

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.database, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @contextmanager
    def _lock(self, job_id: str):
        # OS lock automatically releases after process death. No stale PID guesses.
        path = self._path("locks/" + job_id + ".lock")
        handle = path.open("a+b")
        acquired = False
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                if handle.read(1) == b"":
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                except OSError as exc:
                    raise RuntimeError("Job is actively running or being repaired") from exc
            else:
                import fcntl
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except BlockingIOError as exc:
                    raise RuntimeError("Job is actively running or being repaired") from exc
            yield
        finally:
            if acquired:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    @staticmethod
    def _event(db, job_id: str, kind: str, payload: Any):
        db.execute("INSERT INTO events(job_id,time,kind,payload) VALUES(?,?,?,?)",
                   (job_id, now(), kind, canonical(payload)))

    def register_worker(self, name: str, worker: Any):
        if not isinstance(name, str) or not name or not callable(getattr(worker, "build", None)):
            raise ValueError("Worker needs a nonempty name and build(request) method")
        self.workers[name] = worker

    def register_auditor(self, name: str, auditor: Any):
        if not isinstance(name, str) or not name or not callable(getattr(auditor, "audit", None)):
            raise ValueError("Auditor needs a nonempty name and audit(root, folded, artifact) method")
        self.auditors[name] = auditor

    def _source_boundary(self, root: Path, paths: list[str]):
        if root.resolve() != root:
            raise ValueError("Source root changed its resolved location after submission")
        for path in paths:
            resolved = (root / path).resolve()
            if not resolved.is_relative_to(root) or resolved.is_relative_to(self.workspace):
                raise ValueError("Source path escapes root or refers to runtime outputs")

    def submit(self, root: str | Path, paths: list[str], query: str,
               prediction: dict | None = None, max_passages: int = 8,
               *, worker: str = "literal_extract", auditor: str = "mechanical") -> str:
        root = Path(root).expanduser().resolve()
        if not root.is_dir():
            raise ValueError("Source root must be an existing directory")
        if not isinstance(paths, list) or not 1 <= len(paths) <= 100:
            raise ValueError("Supply between 1 and 100 explicit source paths")
        for path in paths:
            if not isinstance(path, str) or not path or Path(path).is_absolute() or ".." in Path(path).parts:
                raise ValueError("Source paths must be relative and cannot traverse parents")
            source = (root / path).resolve()
            if not source.is_relative_to(root) or source.is_relative_to(self.workspace):
                raise ValueError("Source path escapes root or refers to runtime outputs")
        if len(set(paths)) != len(paths):
            raise ValueError("Duplicate source paths are not allowed")
        if not isinstance(query, str) or len(query) > 16000:
            raise ValueError("Query must be text of at most 16000 characters")
        if type(max_passages) is not int or not 1 <= max_passages <= 100:
            raise ValueError("max_passages must be an integer between 1 and 100")
        if prediction is not None and not isinstance(prediction, dict):
            raise ValueError("Prediction must be a JSON object")
        if worker not in self.workers or auditor not in self.auditors:
            raise ValueError("Register requested worker and auditor before submitting")
        artifact_contract = getattr(self.workers[worker], "artifact_contract", "caller_registered")
        if artifact_contract != "literal_brief":
            artifact_contract = "caller_registered"
            if auditor == "mechanical" or self.workers[worker] is self.auditors[auditor]:
                raise ValueError("A non-brief worker requires a distinct caller-registered artifact auditor")
        artifact_name = getattr(self.workers[worker], "artifact_name", "brief.md")
        if (not isinstance(artifact_name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", artifact_name)
                or artifact_name in ("folded.json", "result.json", "worker_return.json")):
            raise ValueError("Worker artifact_name must be a simple non-reserved filename")
        envelope = {
            "schema_version": "1.0.0", "goal": query, "query": query,
            "root": str(root), "paths": paths,
            "prediction": prediction if prediction is not None else {"audit_verdict": "PASS"},
            "bounds": {"max_passages": max_passages, "max_source_bytes": 2097152,
                       "max_sources": 100, "max_artifact_bytes": 4194304},
            "worker": worker, "auditor": auditor,
            "artifact_contract": artifact_contract,
            "artifact_name": artifact_name,
            "authority": "Produce one local derived artifact; sources read-only; no external execution",
        }
        body = canonical(envelope)
        if len(body.encode()) > 262144:
            raise ValueError("Task envelope exceeds 256 KiB")
        job_id = uuid.uuid4().hex
        with self._db() as db:
            db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?)",
                       (job_id, digest(body.encode()), body, "READY", now(), now(), None, None))
            self._event(db, job_id, "SUBMITTED", {"envelope_id": digest(body.encode()), "prediction": envelope["prediction"]})
        return job_id

    @staticmethod
    def _row(db, job_id: str):
        if not isinstance(job_id, str) or len(job_id) != 32 or any(c not in "0123456789abcdef" for c in job_id):
            raise ValueError("Invalid job id")
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise ValueError("Job not found")
        if digest(row["envelope"].encode()) != row["envelope_id"]:
            raise ValueError("Stored envelope identity mismatch")
        return row

    def status(self, job_id: str) -> dict:
        with self._db() as db:
            row = self._row(db, job_id)
            events = [{"sequence": e["sequence"], "time": e["time"], "kind": e["kind"],
                       "payload": json.loads(e["payload"])}
                      for e in db.execute("SELECT * FROM events WHERE job_id=? ORDER BY sequence", (job_id,))]
            artifacts = [dict(a) for a in db.execute("SELECT * FROM artifacts WHERE job_id=? ORDER BY name", (job_id,))]
            return {"job_id": job_id, "state": row["state"], "envelope_id": row["envelope_id"],
                    "envelope": json.loads(row["envelope"]), "created": row["created"], "updated": row["updated"],
                    "result": json.loads(row["result"]) if row["result"] else None,
                    "error": row["error"], "artifacts": artifacts, "events": events}

    def _write_artifact(self, job_id: str, name: str, text: str):
        content = text.encode("utf-8")
        sha = digest(content)
        blob = self._path("blobs/" + sha)
        try:
            with blob.open("xb") as out:
                out.write(content)
                out.flush()
                os.fsync(out.fileno())
        except FileExistsError:
            if blob.read_bytes() != content:
                raise ValueError("Immutable blob is corrupt; refusing overwrite")
        directory = self._path("jobs/" + job_id)
        directory.mkdir(exist_ok=True)
        path = self._path("jobs/" + job_id + "/" + name)
        with path.open("xb") as out:
            out.write(content)
            out.flush()
            os.fsync(out.fileno())
        with self._db() as db:
            db.execute("INSERT INTO artifacts VALUES(?,?,?,?,?)",
                       (job_id, name, str(path.relative_to(self.workspace)), sha, len(content)))
            self._event(db, job_id, "ARTIFACT_WRITTEN", {"name": name, "sha256": sha, "bytes": len(content)})

    def run(self, job_id: str) -> dict:
        self.status(job_id)  # Validate before constructing any lock path.
        with self._lock(job_id):
            with self._db() as db:
                db.execute("BEGIN IMMEDIATE")
                row = self._row(db, job_id)
                if row["state"] != "READY":
                    raise ValueError("Only READY jobs run; interrupted or finished work is not replayed")
                envelope = json.loads(row["envelope"])
                envelope_id = row["envelope_id"]
                db.execute("UPDATE jobs SET state='RUNNING',updated=? WHERE id=?", (now(), job_id))
                self._event(db, job_id, "CLAIMED", {"worker": envelope["worker"], "auditor": envelope["auditor"]})
            try:
                root = Path(envelope["root"])
                worker = self.workers.get(envelope["worker"])
                auditor = self.auditors.get(envelope["auditor"])
                if worker is None or auditor is None:
                    raise ValueError("Persisted worker or auditor is not registered in this process")
                self._source_boundary(root, envelope["paths"])
                folded = fold(root, envelope["paths"], envelope["query"], envelope["bounds"]["max_passages"])
                self._write_artifact(job_id, "folded.json", canonical(folded) + "\n")
                source_record = self.records.publish("source_binding", "source_manifest",
                    {"envelope_id": envelope_id, "sources": folded["sources"]})
                selection_record = self.records.publish("selection", "source_fold",
                    {"envelope_id": envelope_id, "folded": folded}, links=[source_record])
                selected = self.records.relay("builder", selection_record,
                    owner="selection", kind="source_fold")["payload"]
                request = WorkerRequest(envelope_id, json.loads(canonical(envelope)), selected["folded"])
                returned = worker.build(request)
                if not isinstance(returned, WorkerReturn):
                    raise ValueError("Worker must return WorkerReturn")
                if not isinstance(returned.notes, str) or len(returned.notes.encode()) > 16384:
                    raise ValueError("Worker notes must be text of at most 16 KiB")
                if returned.envelope_id != envelope_id or canonical(request.envelope) != canonical(envelope):
                    raise ValueError("Bridge custody rejected changed task authority or envelope identity")
                if canonical(request.folded) != canonical(folded):
                    raise ValueError("Worker modified its source view; return rejected")
                if not isinstance(returned.artifact, str) or len(returned.artifact.encode()) > envelope["bounds"]["max_artifact_bytes"]:
                    raise ValueError("Worker artifact must be text within the frozen output bound")
                artifact_name = envelope["artifact_name"]
                self._write_artifact(job_id, artifact_name, returned.artifact)
                raw_return = {"envelope_id": returned.envelope_id, "notes": returned.notes,
                              "artifact_sha256": digest(returned.artifact.encode()), "artifact_name": artifact_name}
                self._write_artifact(job_id, "worker_return.json", canonical(raw_return) + "\n")
                # Audit the exact UTF-8 bytes written, retaining source CRLF/CR.
                artifact = self._path("jobs/" + job_id + "/" + artifact_name).read_bytes().decode("utf-8")
                submission_record = self.records.publish("builder", "worker_return",
                    {**raw_return, "artifact": artifact}, links=[selection_record])
                submitted = self.records.relay("auditor", submission_record,
                    owner="builder", kind="worker_return")["payload"]
                selected = self.records.relay("auditor", selection_record,
                    owner="selection", kind="source_fold")["payload"]
                if (submitted["envelope_id"] != envelope_id or selected["envelope_id"] != envelope_id
                        or submitted["artifact_sha256"] != digest(artifact.encode())
                        or submitted["artifact"] != artifact or canonical(selected["folded"]) != canonical(folded)):
                    raise ValueError("Organ record return custody mismatch")
                audited = auditor.audit(root, selected["folded"], submitted["artifact"])
                if not isinstance(audited, dict) or audited.get("verdict") not in ("PASS", "FAIL", "UNKNOWN"):
                    raise ValueError("Auditor returned invalid verdict")
                canonical(audited)
                # All workers retain mandatory source identity/freshness checks.
                # Only literal-brief tasks require the literal Markdown format;
                # other artifacts are judged by their explicit registered auditor.
                source_findings = []
                try:
                    self._source_boundary(root, envelope["paths"])
                except ValueError as exc:
                    source_findings.append({"kind": "SOURCE_BOUNDARY_CHANGED", "detail": str(exc)[:500]})
                for expected in folded["sources"]:
                    try:
                        current = anchor(root, expected["path"])
                        if any(current[key] != expected[key] for key in ("id", "path", "sha256", "bytes")):
                            source_findings.append({"kind": "SOURCE_DRIFT", "path": expected["path"]})
                    except (OSError, ValueError) as exc:
                        source_findings.append({"kind": "SOURCE_UNAVAILABLE", "path": expected["path"], "detail": str(exc)[:500]})
                source_gate = {"verdict": "FAIL" if source_findings else ("UNKNOWN" if folded["insufficient"] else "PASS"),
                               "findings": source_findings,
                               "scope": "Exact source identity and lexical evidence availability, not artifact correctness"}
                mechanical = (MechanicalAuditor().audit(root, folded, artifact)
                              if envelope["artifact_contract"] == "literal_brief" else None)
                verdicts = [audited["verdict"], source_gate["verdict"]]
                if mechanical is not None:
                    verdicts.append(mechanical["verdict"])
                combined = "FAIL" if "FAIL" in verdicts else ("UNKNOWN" if "UNKNOWN" in verdicts else "PASS")
                audit_record = self.records.publish("auditor", "audit_result",
                    {"envelope_id": envelope_id, "submission": submission_record, "verdict": combined,
                     "audit": audited, "source_gate": source_gate, "mechanical_audit": mechanical},
                    links=[submission_record, selection_record])
                observations = {"passage_count": {"value": len(folded["passages"]), "unit": "count"},
                                "artifact_bytes": {"value": len(artifact.encode()), "unit": "bytes"}}
                if combined != "UNKNOWN":
                    observations["audit_pass"] = {"value": combined == "PASS", "unit": "boolean"}
                ruler = measure(observations, [
                    {"name": "artifact_correctness", "metric": "audit_pass", "op": "eq", "value": True, "unit": "boolean"},
                    {"name": "evidence_present", "metric": "passage_count", "op": "ge", "value": 1, "unit": "count"}])
                actual = {"audit_verdict": combined, "passage_count": len(folded["passages"]),
                          "artifact_bytes": len(artifact.encode())}
                remaining = residue(envelope["prediction"], actual)
                measurement_record = self.records.publish("ruler", "measurement",
                    {"envelope_id": envelope_id, "measurement": ruler}, links=[audit_record])
                residual_record = self.records.publish("residuals", "prediction_comparison",
                    {"envelope_id": envelope_id, "residue": remaining}, links=[audit_record])
                routing = route_result(self.records, audit_record, measurement_record,
                    envelope_id=envelope_id, submission_handle=submission_record)
                state = routing["state"]
                decision_record = self.records.publish("orchestrator", "routing_decision",
                    {"envelope_id": envelope_id, **routing},
                    links=[audit_record, measurement_record, residual_record])
                result = {"job_id": job_id, "state": state, "envelope_id": envelope_id,
                          "artifact": str(self._path("jobs/" + job_id + "/" + artifact_name)),
                          "audit": audited, "mechanical_audit": mechanical, "source_gate": source_gate,
                          "combined_verdict": combined,
                          "ruler": ruler, "observations": observations, "residue": remaining,
                          "worker_return": raw_return,
                          "next_action": routing["next_action"],
                          "organ_records": {"source_binding": source_record, "selection": selection_record,
                              "builder": submission_record, "auditor": audit_record, "ruler": measurement_record,
                              "residuals": residual_record, "orchestrator": decision_record},
                          "configuration": {"worker": envelope["worker"], "auditor": envelope["auditor"],
                              "artifact_contract": envelope["artifact_contract"],
                              "correctness_scope": "Literal extracts" if mechanical is not None else "Caller-supplied artifact checker; no general correctness guarantee",
                              "independence": "Separate audit function in same process; no independent human/model review",
                              "worker_isolation": "Trusted in-process Python; no hostile-code sandbox"},
                          "selection_limits": {key: folded.get(key) for key in
                              ("selection_method", "missing_terms", "omitted_sources", "omitted_passages", "insufficient")}}
                self._write_artifact(job_id, "result.json", canonical(result) + "\n")
                with self._db() as db:
                    self._row(db, job_id)
                    db.execute("UPDATE jobs SET state=?,result=?,updated=? WHERE id=?", (state, canonical(result), now(), job_id))
                    self._event(db, job_id, "AUDITED", {"combined_verdict": combined, "ruler": ruler})
                    self._event(db, job_id, state, {"residue": remaining})
            except Exception as exc:
                error = (type(exc).__name__ + ": " + str(exc))[:2000]
                with self._db() as db:
                    db.execute("UPDATE jobs SET state='FAILED',error=?,updated=? WHERE id=?", (error, now(), job_id))
                    self._event(db, job_id, "FAILED", {"error": error})
            return self.status(job_id)

    def maintain(self) -> dict:
        """Read-only inspection. A healthy artifact does not revalidate its claims."""
        findings = []
        with self._db() as db:
            for check in db.execute("PRAGMA integrity_check"):
                if check[0] != "ok":
                    findings.append({"kind": "DATABASE_INTEGRITY", "detail": check[0]})
            jobs = [dict(row) for row in db.execute("SELECT * FROM jobs")]
            artifacts = [dict(row) for row in db.execute("SELECT * FROM artifacts")]
        for item in artifacts:
            for kind, relative in (("ARTIFACT", item["path"]), ("BLOB", "blobs/" + item["sha256"])):
                try:
                    path = self._path(relative)
                    if not path.exists():
                        findings.append({"job_id": item["job_id"], "kind": kind + "_MISSING", "path": relative})
                    elif digest(path.read_bytes()) != item["sha256"]:
                        findings.append({"job_id": item["job_id"], "kind": kind + "_DAMAGED", "path": relative})
                except (OSError, ValueError) as exc:
                    findings.append({"job_id": item["job_id"], "kind": kind + "_UNREADABLE", "detail": str(exc)[:500]})
        for row in jobs:
            if digest(row["envelope"].encode()) != row["envelope_id"]:
                findings.append({"job_id": row["id"], "kind": "ENVELOPE_DAMAGED"})
                continue
            envelope = json.loads(row["envelope"])
            recorded_result = json.loads(row["result"]) if row["result"] else {}
            for owner, handle in recorded_result.get("organ_records", {}).items():
                try:
                    self.records.verify_links([handle])
                except (OSError, ValueError) as exc:
                    missing = getattr(exc, "handle", handle)
                    if not any(f.get("job_id") == row["id"] and f.get("handle") == missing for f in findings):
                        findings.append({"job_id": row["id"], "kind": "ORGAN_RECORD_UNAVAILABLE",
                                         "owner": missing.split(":")[0], "handle": missing,
                                         "consumer": owner, "next_action": "ASK_OWNER", "detail": str(exc)[:500]})
            view = next((a for a in artifacts if a["job_id"] == row["id"] and a["name"] == "folded.json"), None)
            if view:
                try:
                    view_bytes = self._path("blobs/" + view["sha256"]).read_bytes()
                    if digest(view_bytes) != view["sha256"]:
                        continue
                    for source in json.loads(view_bytes)["sources"]:
                        expected = source["sha256"]
                        current = anchor(Path(envelope["root"]), source["path"])
                        if current["sha256"] != expected:
                            findings.append({"job_id": row["id"], "kind": "SOURCE_DRIFT", "path": source["path"]})
                except (OSError, ValueError, KeyError) as exc:
                    findings.append({"job_id": row["id"], "kind": "SOURCE_CHECK_UNAVAILABLE", "detail": str(exc)[:500]})
            if row["state"] in ("RUNNING", "UNCERTAIN", "FAILED"):
                findings.append({"job_id": row["id"], "kind": "TASK_" + row["state"]})
        return {"verdict": "ATTENTION" if findings else "OK", "findings": findings,
                "jobs_checked": len(jobs), "artifacts_checked": len(artifacts),
                "mode": "Read-only integrity and freshness; does not promote semantic claims"}

    def recover(self, job_id: str) -> dict:
        self.status(job_id)
        repaired, unavailable = [], []
        with self._lock(job_id):
            status = self.status(job_id)
            for item in status["artifacts"]:
                preserved = None
                try:
                    path = self._path(item["path"])
                    if path.is_file() and digest(path.read_bytes()) == item["sha256"]:
                        continue
                    blob = self._path("blobs/" + item["sha256"])
                    if not blob.is_file() or digest(blob.read_bytes()) != item["sha256"]:
                        unavailable.append({"name": item["name"], "reason": "Immutable content blob missing or corrupt"})
                        continue
                    if path.exists() or path.is_symlink():
                        preserved = self._path("damaged/" + job_id + "-" + uuid.uuid4().hex + "-" + item["name"])
                        path.replace(preserved)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with path.open("xb") as out:
                        out.write(blob.read_bytes())
                        out.flush()
                        os.fsync(out.fileno())
                    repaired.append({"name": item["name"], "preserved_damage": str(preserved) if preserved else None})
                except (OSError, ValueError) as exc:
                    unavailable.append({"name": item["name"], "reason": (type(exc).__name__ + ": " + str(exc))[:500],
                                        "preserved_damage": str(preserved) if preserved else None})
            for owner, handle in (status["result"] or {}).get("organ_records", {}).items():
                try:
                    self.records.verify_links([handle])
                except (OSError, ValueError) as exc:
                    missing = getattr(exc, "handle", handle)
                    if not any(item.get("handle") == missing for item in unavailable):
                        unavailable.append({"name": "organ_record", "owner": missing.split(":")[0],
                                            "handle": missing, "consumer": owner,
                                            "next_action": "ASK_OWNER", "reason": str(exc)[:500]})
            result = {"job_id": job_id, "state": status["state"], "repaired": repaired,
                      "unavailable": unavailable, "verdict": "BLOCKED" if unavailable else "OK",
                      "note": "Restored recorded outputs only; failures, predictions, and source bodies unchanged"}
            with self._db() as db:
                self._event(db, job_id, "RECOVERY", result)
            return result

    def resume(self, job_id: str) -> dict:
        status = self.status(job_id)
        if status["state"] == "READY":
            return self.run(job_id)
        with self._lock(job_id):
            with self._db() as db:
                row = self._row(db, job_id)
                if row["state"] == "RUNNING":
                    reason = "Interrupted execution; worker effects uncertain. Preserved without automatic rerun."
                    db.execute("UPDATE jobs SET state='UNCERTAIN',error=?,updated=? WHERE id=?", (reason, now(), job_id))
                    self._event(db, job_id, "INTERRUPTION_RECOGNIZED", {"reason": reason})
        return self.status(job_id)
