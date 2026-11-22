"""Local board and explicit adapters to Lodestone's existing kernel organs.

Source text is data. Importing it never invokes a shell, provider or model.
Participant names are attribution supplied by the caller, not authentication.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import base64
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid

from lodestone_machinery.bezoar import Plumber

PARTICIPANTS = [
    {"id": "host", "name": "Host", "role": "Owner and task approver"},
    {"id": "operator", "name": "Operator", "role": "Local contributor"},
    {"id": "reviewer", "name": "Reviewer", "role": "Independent reviewer"},
]


def validate_participants(value):
    """Caller-supplied attribution, never authentication or execution authority."""
    import re
    if type(value) is not list or not 1 <= len(value) <= 32:
        raise ValueError("Expected one to 32 declared participants")
    result, identities = [], set()
    for item in value:
        if type(item) is not dict or set(item) != {"id", "name", "role"}:
            raise ValueError("Participant fields must be id, name and role")
        identity = item["id"]
        if (type(identity) is not str or re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", identity) is None
                or identity in identities):
            raise ValueError("Participant IDs must be unique lowercase slugs")
        identities.add(identity)
        result.append({"id": identity, "name": text(item["name"], "participant name", 120),
                       "role": text(item["role"], "participant role", 240)})
    if "host" not in identities:
        raise ValueError("The host attribution is required for explicitly approved tasks")
    return result

SHELVES = ("commons", "memory", "research", "workshop", "samsara")
MAX_SOURCE = 2 * 1024 * 1024
MAX_BODY = 32000
MAX_ROWS = 4096


def now():
    return datetime.now(timezone.utc).isoformat()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def text(value, label, limit):
    if type(value) is not str or not value.strip() or len(value.encode("utf-8")) > limit:
        raise ValueError(f"Invalid {label}; expected nonempty text within {limit} bytes")
    return value


def read_file(path, limit=MAX_SOURCE):
    with Path(path).open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Input exceeds byte limit")
    return data


class Board:
    def __init__(self, home, *, participants=None):
        supplied_participants = validate_participants(participants) if participants is not None else None
        self.home = Path(home).expanduser().resolve()
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = self.home / "pando.sqlite3"
        self.plumber = Plumber(self.home / "kernel")
        self.captures = self.home / "captures"
        self.captures.mkdir(exist_ok=True, mode=0o700)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sources (id TEXT PRIMARY KEY, title TEXT NOT NULL,
                  uri TEXT NOT NULL, sha256 TEXT NOT NULL, bytes INTEGER NOT NULL,
                  kind TEXT NOT NULL, author TEXT NOT NULL, captured_at TEXT NOT NULL, body BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS posts (id TEXT PRIMARY KEY, author TEXT NOT NULL,
                  shelf TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
                  parent_id TEXT REFERENCES posts(id), source_ids TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS artifacts (id TEXT PRIMARY KEY, title TEXT NOT NULL,
                  author TEXT NOT NULL, stage TEXT NOT NULL, handle TEXT NOT NULL,
                  source_ids TEXT NOT NULL, parent_id TEXT REFERENCES artifacts(id),
                  summary TEXT NOT NULL, created_at TEXT NOT NULL);
            """)
            db.execute("INSERT OR IGNORE INTO metadata VALUES ('schema','lodestone-pando/v1')")
            if db.execute("SELECT value FROM metadata WHERE key='schema'").fetchone()[0] != "lodestone-pando/v1":
                raise ValueError("Unsupported Pando schema")
            existing = db.execute("SELECT value FROM metadata WHERE key='participants'").fetchone()
            if existing:
                current = validate_participants(json.loads(existing[0]))
                if supplied_participants is not None and supplied_participants != current:
                    raise ValueError("Existing participant configuration differs; preserve the board and its history")
            else:
                db.execute("INSERT INTO metadata VALUES ('participants',?)",
                           (encoded(supplied_participants or PARTICIPANTS),))
        self.db.chmod(0o600)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.db, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def participants(self):
        with self.connection() as db:
            row = db.execute("SELECT value FROM metadata WHERE key='participants'").fetchone()
        return validate_participants(json.loads(row[0]) if row else PARTICIPANTS)

    def author(self, actor):
        if actor not in {p["id"] for p in self.participants()}:
            raise ValueError("Unknown participant; use a declared attribution")
        return actor

    def capacity(self, db, table):
        # Per-table record bound; this is not a claim of globally bounded learning.
        if db.execute("SELECT count(*) FROM " + table).fetchone()[0] >= MAX_ROWS:
            raise ValueError("Board record limit reached; export and review retention")

    def refs(self, db, ids):
        if type(ids) is not list or len(ids) > 8 or any(type(i) is not str for i in ids) or len(set(ids)) != len(ids):
            raise ValueError("Expected at most eight distinct source IDs")
        for identity in ids:
            if db.execute("SELECT id FROM sources WHERE id=?", (identity,)).fetchone() is None:
                raise ValueError("Unknown source reference")

    def ingest(self, raw, *, title, uri, author, kind="file"):
        self.author(author)
        text(title, "source title", 300); text(uri, "source URI", 4096)
        if kind not in ("file", "notion", "mailbox", "host", "github"):
            raise ValueError("Unsupported source kind")
        if type(raw) is not bytes or len(raw) > MAX_SOURCE:
            raise ValueError("Source exceeds 2 MiB")
        raw.decode("utf-8", errors="strict")
        sha = hashlib.sha256(raw).hexdigest()
        identity = hashlib.sha256(encoded([uri, sha]).encode()).hexdigest()
        with self.connection() as db:
            row = db.execute("SELECT * FROM sources WHERE id=?", (identity,)).fetchone()
            if row is None:
                self.capacity(db, "sources")
                db.execute("INSERT INTO sources VALUES (?,?,?,?,?,?,?,?,?)",
                           (identity, title, uri, sha, len(raw), kind, author, now(), raw))
            elif bytes(row["body"]) != raw:
                raise ValueError("Source identity collision")
        return identity

    def import_mailbox(self, envelope, *, title, uri, author):
        """Verify explicit mailbox bytes without consuming or replaying a job."""
        if type(envelope) is not dict or envelope.get("schema") != "lodestone-mailbox/v1":
            raise ValueError("Invalid mailbox schema")
        if envelope.get("kind") != "file" or envelope.get("to") not in ("cloud", "mac"):
            raise ValueError("Only file messages are eligible")
        size = envelope.get("size")
        if type(size) is not int or not 0 <= size <= 1024 * 1024:
            raise ValueError("Invalid mailbox size")
        b64 = envelope.get("payload_b64")
        if type(b64) is not str or len(b64) > 1400000:
            raise ValueError("Invalid mailbox payload")
        try:
            raw = base64.b64decode(b64, validate=True)
        except ValueError as exc:
            raise ValueError("Invalid mailbox encoding") from exc
        if len(raw) != size or hashlib.sha256(raw).hexdigest() != envelope.get("sha256"):
            raise ValueError("Mailbox size or hash mismatch")
        return self.ingest(raw, title=title, uri=uri, author=author, kind="mailbox")

    def source(self, identity):
        with self.connection() as db:
            row = db.execute("SELECT * FROM sources WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise ValueError("Unknown source")
        result = dict(row)
        raw = bytes(result.pop("body"))
        if len(raw) != result["bytes"] or hashlib.sha256(raw).hexdigest() != result["sha256"]:
            raise ValueError("Source identity mismatch")
        result["text"] = raw.decode("utf-8")
        return result

    def post(self, *, author, shelf, title, body, parent_id=None, source_ids=None):
        self.author(author)
        if parent_id is not None and type(parent_id) is not str:
            raise ValueError("Reply parent must be a post ID string")
        if shelf not in self.settings()["shelves"]:
            raise ValueError("Unknown shelf")
        text(title, "title", 300); text(body, "body", MAX_BODY)
        source_ids = [] if source_ids is None else source_ids
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self.capacity(db, "posts"); self.refs(db, source_ids)
            if parent_id is not None:
                parent = db.execute("SELECT shelf FROM posts WHERE id=?", (parent_id,)).fetchone()
                if parent is None or parent[0] != shelf:
                    raise ValueError("Reply must reference a post on the same shelf")
            identity = uuid.uuid4().hex
            db.execute("INSERT INTO posts VALUES (?,?,?,?,?,?,?,?)",
                       (identity, author, shelf, title, body, parent_id, encoded(source_ids), now()))
        return identity

    def artifact(self, identity):
        with self.connection() as db:
            row = db.execute("SELECT * FROM artifacts WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise ValueError("Unknown artifact")
        item = dict(row); item["source_ids"] = json.loads(item["source_ids"])
        item["record"] = self.plumber.records.read(item["handle"])
        return item

    def _artifact(self, title, author, stage, handle, source_ids, parent_id, summary):
        self.author(author); text(title, "title", 300); text(summary, "summary", MAX_BODY)
        identity = hashlib.sha256(encoded([stage, handle, author, parent_id]).encode()).hexdigest()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self.refs(db, source_ids)
            if db.execute("SELECT id FROM artifacts WHERE id=?", (identity,)).fetchone() is None:
                self.capacity(db, "artifacts")
                db.execute("INSERT INTO artifacts VALUES (?,?,?,?,?,?,?,?,?)",
                           (identity, title, author, stage, handle, encoded(source_ids), parent_id, summary, now()))
        return identity

    def eat(self, source_ids, *, author, question, title):
        self.author(author); text(title, "title", 300)
        with self.connection() as db:
            self.refs(db, source_ids)
        if not source_ids:
            raise ValueError("EAT needs a source")
        sources = [self.source(identity) for identity in source_ids]
        if sum(s["bytes"] for s in sources) > MAX_SOURCE:
            raise ValueError("EAT total exceeds 2 MiB")
        paths = []
        for source in sources:
            path = self.captures / (source["id"] + ".txt")
            raw = source["text"].encode("utf-8")
            try:
                with path.open("xb") as stream:
                    stream.write(raw)
                path.chmod(0o400)
            except FileExistsError:
                if read_file(path) != raw:
                    raise ValueError("Source capture drift")
            paths.append(path.name)
        ingestion = self.plumber.ingest(str(self.captures), paths)
        request = self.plumber.eat_request(ingestion, question)
        return self._artifact(title, author, "EAT", request, source_ids, None, question)

    def reconcile(self, parent_id, *, author, title, scratch, contract="inline"):
        """Retain a captured-corpus reconciliation; never apply a profile or mutate a registry."""
        self.author(author); text(title, "title", 300)
        parent = self.artifact(parent_id)
        if parent["stage"] != "EAT" or len(parent["source_ids"]) != 1 or parent["author"] != author:
            raise ValueError("RECONCILE requires one source-bound EAT request from this author")
        source = self.source(parent["source_ids"][0])
        raw = source["text"].encode("utf-8")
        summary = "Captured evidence checked; original outcomes retained. No repair, activation or learning."
        if contract == "complete-capture":
            from lodestone_machinery.bezoar_capture import publish
            handle = publish(self.plumber.records, raw, expected_sha256=source["sha256"], scratch=scratch)
        elif contract == "observation":
            from lodestone_machinery.bezoar_capture import publish_observation, OBSERVATION_SUMMARY
            handle = publish_observation(self.plumber.records, raw, expected_sha256=source["sha256"], scratch=scratch)
            summary = OBSERVATION_SUMMARY
        elif contract == "inline":
            handle = self.plumber.reconcile_captured(raw, expected_sha256=source["sha256"], scratch=scratch)
        else:
            raise ValueError("Unknown explicit reconciliation contract")
        return self._artifact(title, author, "RECONCILE", handle, parent["source_ids"], parent_id, summary)

    def digest(self, parent_id, proposal, *, author, title):
        self.author(author); text(title, "title", 300)
        parent = self.artifact(parent_id)
        if parent["stage"] != "EAT":
            raise ValueError("DIGEST requires an EAT request")
        if type(proposal) is not dict or proposal.get("actor") != author:
            raise ValueError("Digestion actor must match declared attribution")
        handle = self.plumber.eat(parent["handle"], proposal)
        return self._artifact(title, author, "DIGEST", handle, parent["source_ids"], parent_id,
                              "Source-cited candidate; semantic review remains required. No model or source instruction executed.")

    def route_reconciliation(self, parent_id, *, author, title, previous_id=None, scratch):
        """Retain a verified routing proposal; never dispatch it or clear a finding."""
        self.author(author); text(title, "title", 300)
        parent = self.artifact(parent_id)
        if parent["stage"] != "RECONCILE" or len(parent["source_ids"]) != 1 or parent["author"] != author:
            raise ValueError("ROUTE requires one source-bound reconciliation")
        previous = self.artifact(previous_id) if previous_id is not None else None
        if previous is not None and (previous["stage"] != "RECONCILE"
                or previous["record"]["owner"] != "bezoar" or previous["record"]["kind"] != "reconciliation"):
            raise ValueError("Prior routing evidence must be a retained reconciliation")
        source = self.source(parent["source_ids"][0])
        handle = self.plumber.propose_reconciliation_route(parent["handle"],
                    expected_source_sha256=source["sha256"],
                    previous=previous["handle"] if previous else None, scratch=scratch)
        return self._artifact(title, author, "ROUTE", handle, parent["source_ids"], parent_id,
                              "Prior evidence checked for applicability; findings retained. No repair or activation.")

    def mutate(self, parent_id, mutagen, *, author, title):
        self.author(author); text(title, "title", 300)
        parent = self.artifact(parent_id)
        if parent["stage"] not in ("DIGEST", "MUTATE"):
            raise ValueError("MUTATE requires a digested candidate or prior variant")
        handle = self.plumber.mutate(parent["handle"], mutagen)
        return self._artifact(title, author, "MUTATE", handle, parent["source_ids"], parent_id,
                              mutagen["rationale"])

    def settings(self):
        with self.connection() as db:
            row = db.execute("SELECT value FROM metadata WHERE key='view'").fetchone()
        return json.loads(row[0]) if row else {"name": "Lodestone Pando", "shelves": list(SHELVES),
                                              "artifact_id": None, "author": None}

    def apply_profile(self, artifact_id, *, author):
        """Explicit consumer of two view fields, never arbitrary profile instructions.

        The original candidate remains immutable and unreviewed. This separately
        records the caller's decision to use its name and shelves in this board.
        """
        self.author(author)
        artifact = self.artifact(artifact_id)
        if artifact["stage"] not in ("DIGEST", "MUTATE"):
            raise ValueError("A digested profile is required")
        self.plumber.records.verify_links([artifact["handle"]])
        operators = artifact["record"]["payload"]["profile"]["operators"]
        if set(operators) != {"board_name", "enabled_shelves"}:
            raise ValueError("View profile accepts only board_name and enabled_shelves")
        name = text(operators["board_name"], "board name", 120)
        shelves = operators["enabled_shelves"]
        if (type(shelves) is not list or not shelves or any(type(s) is not str or s not in SHELVES for s in shelves)
                or len(set(shelves)) != len(shelves)):
            raise ValueError("View shelves must be a nonempty subset of known shelves")
        view = {"name": name, "shelves": shelves, "artifact_id": artifact_id, "author": author}
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            previous_row = db.execute("SELECT value FROM metadata WHERE key='view'").fetchone()
            previous_handle = db.execute("SELECT value FROM metadata WHERE key='view_application'").fetchone()
            previous = json.loads(previous_row[0]) if previous_row else None
            links = [artifact["handle"]]
            if previous_handle:
                self.plumber.records.verify_links([previous_handle[0]])
                links.append(previous_handle[0])
            handle = self.plumber.records.publish("pando", "view_application",
                        {"schema": "pando-view-application/v1", "view": view,
                         "previous": previous, "previous_application": previous_handle[0] if previous_handle else None,
                         "applied_at": now(),
                         "scope": "Board display name and shelves only; no executable instruction or model activation"},
                        links=links)
            self.plumber.records.verify_links([handle])
            # Keep earlier application records; only the current view pointer changes.
            db.execute("INSERT OR REPLACE INTO metadata VALUES ('view',?)", (encoded(view),))
            db.execute("INSERT OR REPLACE INTO metadata VALUES ('view_application',?)", (handle,))
        return {"status": "APPLIED_VIEW", "view": view, "handle": handle}

    def state(self):
        with self.connection() as db:
            sources = [dict(r) for r in db.execute("SELECT id,title,uri,sha256,bytes,kind,author,captured_at FROM sources ORDER BY captured_at DESC")]
            posts = [dict(r) for r in db.execute("SELECT * FROM posts ORDER BY created_at DESC,id")]
            artifacts = [dict(r) for r in db.execute("SELECT * FROM artifacts ORDER BY created_at DESC,id")]
        for item in posts + artifacts:
            item["source_ids"] = json.loads(item["source_ids"])
        view = self.settings()
        return {"schema": "lodestone-pando/v1", "name": view["name"], "view": view,
                "participants": self.participants(), "shelves": view["shelves"], "posts": posts,
                "sources": sources, "artifacts": artifacts,
                "stats": {"posts": len(posts), "sources": len(sources), "artifacts": len(artifacts),
                          "source_bytes": sum(s["bytes"] for s in sources)},
                "scope": "Private local board. Attribution is caller supplied. No participant session is started."}

    def verify(self):
        state = self.state()
        for source in state["sources"]:
            self.source(source["id"])
        count = 0
        for artifact in state["artifacts"]:
            count += self.plumber.records.verify_links([artifact["handle"]])["records_checked"]
        with self.connection() as db:
            application = db.execute("SELECT value FROM metadata WHERE key='view_application'").fetchone()
            if application:
                count += self.plumber.records.verify_links([application[0]])["records_checked"]
                if self.plumber.records.read(application[0])["payload"]["view"] != state["view"]:
                    raise ValueError("Applied view does not match recorded application")
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or db.execute("PRAGMA foreign_key_check").fetchall():
                raise ValueError("Pando database integrity failure")
        return {"status": "INTACT", **state["stats"], "record_checks_including_repeats": count,
                "scope": "Stored source hashes, organ links and SQLite integrity; not semantic truth"}
