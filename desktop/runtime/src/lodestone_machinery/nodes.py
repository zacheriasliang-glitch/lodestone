"""Persistent typed claim graph; recorded claims are not inferred truths.

``A depends_on B`` means invalidating B marks A and its transitive dependents
NEEDS_REVIEW. Semantic status is preserved. Every change creates a new revision;
historical records are never edited by this API. An acceptance reference is a
caller-supplied record of acceptance, not an authenticated authorization token.
"""
from __future__ import annotations

import json
import math
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MAX_PAYLOAD_BYTES = 262_144
LAYERS = {"EX", "AP", "NV"}
STATUSES = {"stable_canon", "candidate", "experimental", "superseded", "unresolved"}
RELATIONS = {"depends_on", "contradicts", "supports", "supersedes"}
REQUIRED = {"id", "text", "layer", "status", "scope", "epoch", "sources"}
OPTIONAL = {"freshness", "review_reasons", "acceptance_ref", "metadata"}


def _json_copy(value: Any, max_bytes: int = MAX_PAYLOAD_BYTES) -> Any:
    """Strict JSON only: no tuple coercion, nonstring keys, or nonfinite numbers."""
    def check(item: Any, depth: int = 0) -> None:
        if depth > 64:
            raise ValueError("JSON nesting exceeds 64 levels")
        kind = type(item)
        if item is None or kind in (str, bool, int):
            return
        if kind is float:
            if not math.isfinite(item):
                raise ValueError("JSON numbers must be finite")
            return
        if kind is list:
            for child in item:
                check(child, depth + 1)
            return
        if kind is dict:
            for key, child in item.items():
                if type(key) is not str:
                    raise ValueError("JSON object keys must be strings")
                check(child, depth + 1)
            return
        raise ValueError(f"Unsupported JSON value: {kind.__name__}")

    try:
        check(value)
        encoded = json.dumps(value, allow_nan=False, ensure_ascii=False, sort_keys=True)
        if len(encoded.encode("utf-8")) > max_bytes:
            raise ValueError(f"JSON payload exceeds {max_bytes} bytes")
        return json.loads(encoded)
    except (TypeError, OverflowError, RecursionError, UnicodeError) as exc:
        raise ValueError("Invalid JSON payload") from exc


def _nonempty(value: Any, field: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")
    return value


class NodeStore:
    """SQLite-backed claim storage in ``workspace/nodes.sqlite3``.

    ``get`` returns the current record or None. ``history`` returns ordered
    revision entries with record, revision, timestamp, and change reason.
    ``neighbors`` returns both incoming and outgoing explicit edges; contradiction
    links do not delete or merge either endpoint. No topology is learned here.
    """

    def __init__(self, workspace: str | Path):
        self.workspace = Path(workspace).expanduser().resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.path = self.workspace / "nodes.sqlite3"
        if self.path.is_symlink():
            raise ValueError("Node database must not be a symlink")
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS claims (
                    id TEXT PRIMARY KEY,
                    current_revision INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS claim_revisions (
                    id TEXT NOT NULL REFERENCES claims(id),
                    revision INTEGER NOT NULL,
                    record_json TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    PRIMARY KEY(id, revision)
                );
                CREATE TABLE IF NOT EXISTS relations (
                    source_id TEXT NOT NULL REFERENCES claims(id),
                    target_id TEXT NOT NULL REFERENCES claims(id),
                    relation TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    PRIMARY KEY(source_id, target_id, relation)
                );
                CREATE INDEX IF NOT EXISTS relations_target
                    ON relations(target_id, relation);
            """)

    @contextmanager
    def _connect(self):
        if self.path.is_symlink():
            raise ValueError("Node database must not be a symlink")
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _validate(record: dict) -> dict:
        data = _json_copy(record)
        if type(data) is not dict:
            raise ValueError("Claim must be a JSON object")
        missing = REQUIRED - data.keys()
        unknown = data.keys() - REQUIRED - OPTIONAL
        if missing or unknown:
            raise ValueError(f"Claim fields missing={sorted(missing)} unknown={sorted(unknown)}")
        for key in ("id", "text", "scope", "epoch"):
            _nonempty(data[key], key)
        if type(data["layer"]) is not str or data["layer"] not in LAYERS:
            raise ValueError("layer must be EX, AP, or NV")
        if type(data["status"]) is not str or data["status"] not in STATUSES:
            raise ValueError("Invalid semantic status")
        if type(data["sources"]) is not list:
            raise ValueError("sources must be a list of strings or JSON source objects")
        for source in data["sources"]:
            if type(source) is str:
                _nonempty(source, "source")
            elif type(source) is not dict or not source:
                raise ValueError("Each source must be a nonempty string or object")
        data.setdefault("freshness", "CURRENT")
        if type(data["freshness"]) is not str or data["freshness"] not in {"CURRENT", "NEEDS_REVIEW"}:
            raise ValueError("freshness must be CURRENT or NEEDS_REVIEW")
        data.setdefault("review_reasons", [])
        if type(data["review_reasons"]) is not list:
            raise ValueError("review_reasons must be a list")
        for reason in data["review_reasons"]:
            _nonempty(reason, "review_reason")
        if "acceptance_ref" in data:
            _nonempty(data["acceptance_ref"], "acceptance_ref")
        if data["status"] == "stable_canon" and not data.get("acceptance_ref"):
            raise ValueError("stable_canon requires an explicit acceptance_ref")
        return _json_copy(data)

    @staticmethod
    def _append(db: sqlite3.Connection, data: dict, reason: str) -> None:
        row = db.execute("SELECT current_revision FROM claims WHERE id=?", (data["id"],)).fetchone()
        revision = row[0] + 1 if row else 1
        if row is None:
            db.execute("INSERT INTO claims(id,current_revision) VALUES (?,?)", (data["id"], revision))
        else:
            db.execute("UPDATE claims SET current_revision=? WHERE id=?", (revision, data["id"]))
        db.execute(
            "INSERT INTO claim_revisions VALUES (?,?,?,?,?)",
            (data["id"], revision, json.dumps(data, allow_nan=False, sort_keys=True),
             datetime.now(timezone.utc).isoformat(), reason),
        )

    @staticmethod
    def _get(db: sqlite3.Connection, claim_id: str) -> dict | None:
        row = db.execute("""
            SELECT r.record_json FROM claims c JOIN claim_revisions r
              ON c.id=r.id AND c.current_revision=r.revision WHERE c.id=?
        """, (claim_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, record: dict) -> str:
        data = self._validate(record)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._append(db, data, "caller_supplied_revision")
        return data["id"]

    def get(self, claim_id: str) -> dict | None:
        _nonempty(claim_id, "id")
        with self._connect() as db:
            return self._get(db, claim_id)

    def history(self, claim_id: str) -> list[dict]:
        _nonempty(claim_id, "id")
        with self._connect() as db:
            rows = db.execute("SELECT * FROM claim_revisions WHERE id=? ORDER BY revision", (claim_id,)).fetchall()
        return [{"revision": row["revision"], "record": json.loads(row["record_json"]),
                 "recorded_at": row["recorded_at"], "reason": row["reason"]} for row in rows]

    def link(self, source_id: str, target_id: str, relation: str) -> dict:
        _nonempty(source_id, "source_id")
        _nonempty(target_id, "target_id")
        if type(relation) is not str or relation not in RELATIONS:
            raise ValueError(f"relation must be one of {sorted(RELATIONS)}")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for claim_id in (source_id, target_id):
                if self._get(db, claim_id) is None:
                    raise ValueError(f"Unknown claim: {claim_id}")
            db.execute("INSERT OR IGNORE INTO relations VALUES (?,?,?,?)",
                       (source_id, target_id, relation, datetime.now(timezone.utc).isoformat()))
            row = db.execute("SELECT * FROM relations WHERE source_id=? AND target_id=? AND relation=?",
                             (source_id, target_id, relation)).fetchone()
        return dict(row)

    def neighbors(self, claim_id: str) -> list[dict]:
        _nonempty(claim_id, "id")
        with self._connect() as db:
            if self._get(db, claim_id) is None:
                raise ValueError(f"Unknown claim: {claim_id}")
            rows = db.execute("""SELECT * FROM relations WHERE source_id=? OR target_id=?
                                 ORDER BY source_id,target_id,relation""", (claim_id, claim_id)).fetchall()
        return [dict(row) for row in rows]

    def invalidate_dependents(self, claim_id: str, reason: str) -> list[str]:
        """Append freshness revisions to transitive dependents; exclude the seed.

        Cycles are visited once. The caller must revise the seed separately if its
        own record changed. Non-dependency edges never propagate invalidation.
        """
        _nonempty(claim_id, "id")
        _nonempty(reason, "reason")
        _json_copy(reason)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if self._get(db, claim_id) is None:
                raise ValueError(f"Unknown claim: {claim_id}")
            seen = {claim_id}
            queue = [claim_id]
            affected = []
            for target in queue:
                rows = db.execute("SELECT source_id FROM relations WHERE target_id=? AND relation='depends_on' ORDER BY source_id",
                                  (target,)).fetchall()
                for row in rows:
                    dependent = row[0]
                    if dependent not in seen:
                        seen.add(dependent)
                        queue.append(dependent)
                        affected.append(dependent)
            for dependent in affected:
                data = self._get(db, dependent)
                data["freshness"] = "NEEDS_REVIEW"
                message = f"Dependency {claim_id}: {reason}"
                if message not in data["review_reasons"]:
                    data["review_reasons"].append(message)
                self._append(db, _json_copy(data), "dependency_invalidation")
        return affected
