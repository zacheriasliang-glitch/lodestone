"""Read-only adapters for existing Pando state and relocated source captures."""
from contextlib import contextmanager
from pathlib import Path
import sqlite3

from lodestone_machinery.bezoar import Plumber
from lodestone_machinery.records import RecordStore
from lodestone_machinery.organs.ingest import read_ingest
from lodestone_pando.store import Board
from .common import digest, read_bytes


class ReadOnlyRecords(RecordStore):
    def __init__(self, workspace):
        self.workspace = Path(workspace).resolve()
        if not self.workspace.is_dir() or not (self.workspace / "organs").is_dir():
            raise ValueError("Existing record store required")

    def publish(self, *args, **kwargs):
        raise PermissionError("Inspection cannot publish records")


class ReadOnlyPlumber(Plumber):
    def __init__(self, workspace):
        self.records = ReadOnlyRecords(workspace)


class ReadOnlyBoard(Board):
    """Reuse Board's read/verify contracts without constructor initialization."""
    def __init__(self, home):
        self.home = Path(home).resolve()
        self.db = self.home / "pando.sqlite3"
        self.captures = self.home / "captures"
        if self.db.is_symlink() or not self.db.is_file() or not self.captures.is_dir():
            raise ValueError("Existing complete Pando state required")
        self.plumber = ReadOnlyPlumber(self.home / "kernel")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.db.as_uri() + "?mode=ro", uri=True, timeout=2)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()


def verify_captures(board):
    """Bind immutable embedded source records to this generation's local copies.

    Original absolute roots remain immutable provenance. They are never opened
    or treated as permission. This is the declared portable validation route;
    the legacy Watcher still checks its original absolute source locations.
    """
    checked = 0
    roots = set()
    records = board.plumber.records
    directory = records.workspace / "organs" / "ingest" / "records"
    for path in sorted(directory.glob("*.json")):
        payload = read_ingest(records, "ingest:" + path.stem, "nurarihyon")
        roots.add(payload["root"])
        for source in payload["sources"]:
            raw = read_bytes(board.captures / source["path"], 2097152)
            if len(raw) != source["bytes"] or digest(raw) != source["sha256"] or raw != source["text"].encode("utf-8"):
                raise ValueError("Portable captured-source identity mismatch")
            checked += 1
    return {"captures_checked": checked, "provenance_roots": sorted(roots),
            "validation_root": str(board.captures), "historical_roots_accessed": False}
