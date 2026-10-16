"""Bounded exact-byte crystals, with one SQLite artifact and selective recovery.

A crystal is a content-addressed literal, zlib leaf, or concatenation rule.
Recursive storage chunks at 4096 bytes and groups at most 32 child handles per
rule. Sharing is exact; the lexical index and packet excerpts are lossy views.
No source path is followed, no original is modified, and authority is NONE.
SQLite's DELETE journal is temporary during writes; no permanent sidecar is used.
The v2 index stores source and term strings in dictionaries, with integer posting
references. Existing v1 artifacts are rejected without modification or migration.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import zlib


VERSION = "yggdrasil-crystals/v2"
MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_DATABASE_BYTES = 1024 * 1024 * 1024
MAX_SOURCES = 4096
MAX_METADATA_BYTES = 64 * 1024
MAX_TERMS = 16384
MAX_DEPTH = 32
MAX_GRAPH_VISITS = 32768
MAX_DECODE_WORK = 4 * MAX_SOURCE_BYTES
MAX_PACKET_BYTES = 1024 * 1024
CHUNK_BYTES = 4096
FANOUT = 32
STRATEGIES = ("literal", "zlib", "recursive")
_APPLICATION_ID = 0x59474352
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_WORDS = re.compile(r"\w+", re.UNICODE)


class CrystalError(ValueError):
    """Invalid crystal request or incompatible store."""


class CrystalCorruptionError(CrystalError):
    """Stored source, graph, index, or database failed an integrity check."""


class CrystalLimitError(CrystalError):
    """A request or stored representation exceeds a hard resource bound."""


def packet_bytes(value):
    """The canonical UTF-8 encoding used for every packet byte-budget claim."""
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise CrystalError("Value must be bounded JSON with valid UTF-8") from exc


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _integer(value, low, high, label):
    if type(value) is not int or not low <= value <= high:
        raise CrystalLimitError(f"{label} must be an integer from {low} to {high}")
    return value


def _source_id(value):
    try:
        valid = type(value) is str and bool(value) and len(value.encode("utf-8")) <= 1024
    except UnicodeError:
        valid = False
    if not valid:
        raise CrystalError("source_id must contain 1 to 1024 UTF-8 bytes")
    return value


def _data(value):
    if type(value) is not bytes:
        raise CrystalError("Source must be bytes")
    if len(value) > MAX_SOURCE_BYTES:
        raise CrystalLimitError("Source exceeds 8 MiB")
    return value


def _descriptor(kind, size, sha256, payload):
    return {"kind": kind, "size": size, "sha256": sha256,
            "payload_sha256": _digest(payload)}


def _plan(data, strategy):
    if strategy not in STRATEGIES:
        raise CrystalError("strategy must be literal, zlib, or recursive")
    nodes = {}

    def add(kind, raw, payload):
        desc = _descriptor(kind, len(raw), _digest(raw), payload)
        handle = _digest(packet_bytes(desc))
        nodes[handle] = (kind, len(raw), desc["sha256"], payload)
        return handle

    if strategy != "recursive":
        payload = data if strategy == "literal" else zlib.compress(data, 9)
        return add(strategy, data, payload), nodes
    level = []
    for offset in range(0, max(1, len(data)), CHUNK_BYTES):
        raw = data[offset:offset + CHUNK_BYTES]
        compressed = zlib.compress(raw, 9)
        kind, payload = ("zlib", compressed) if len(compressed) < len(raw) else ("literal", raw)
        level.append((add(kind, raw, payload), raw))
    while len(level) > 1:
        next_level = []
        for offset in range(0, len(level), FANOUT):
            group = level[offset:offset + FANOUT]
            raw = b"".join(item[1] for item in group)
            payload = packet_bytes([item[0] for item in group])
            next_level.append((add("concat", raw, payload), raw))
        level = next_level
    return level[0][0], nodes


def measure_candidates(data):
    """Measure unique node payload/rules, excluding SQLite, metadata and index.

    These are representation measurements, not total compression ratios. Actual
    database bytes and all logical cost categories are reported by inspect().
    """
    _data(data)
    result = {}
    for strategy in STRATEGIES:
        root, nodes = _plan(data, strategy)
        result[strategy] = {
            "root": root, "unique_nodes": len(nodes),
            "payload_bytes": sum(len(row[3]) for row in nodes.values()),
            "descriptor_bytes": sum(len(packet_bytes(_descriptor(*row))) for row in nodes.values()),
            "excludes": ["SQLite pages and indexes", "source provenance", "lexical postings"],
        }
    return result


def _terms(data):
    counts = Counter()
    truncated = False
    for match in _WORDS.finditer(data.decode("utf-8", errors="replace").casefold()):
        term = match.group()
        if len(term.encode("utf-8")) > 96:
            truncated = True
            continue
        if term in counts or len(counts) < MAX_TERMS:
            counts[term] += 1
        else:
            truncated = True
    return counts, truncated


class CrystalStore:
    """Caller-owned database path; immutable source IDs and no filesystem import.

    ``metadata`` is caller-declared provenance, hash-bound to the source record.
    It cannot confer authority or prove a source is still current. Freshness is
    checked only against bytes explicitly supplied to freshness()/verify().
    """

    def __init__(self, database_path, *, index_mode=None):
        if index_mode not in (None, "indexed", "scan"):
            raise CrystalError("index_mode must be indexed or scan")
        self.path = Path(database_path)
        if self.path.is_symlink() or (self.path.exists() and not self.path.is_file()):
            raise CrystalError("Database must be a regular file, not a symlink")
        if self.path.exists() and self.path.stat().st_size > MAX_DATABASE_BYTES:
            raise CrystalLimitError("Database exceeds 1 GiB")
        self._db = sqlite3.connect(str(self.path), timeout=10)
        self._db.row_factory = sqlite3.Row
        try:
            tables = {row[0] for row in self._db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if not tables:
                self._db.executescript("""
                    CREATE TABLE store_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE nodes (handle TEXT PRIMARY KEY, kind TEXT NOT NULL,
                        size INTEGER NOT NULL, sha256 TEXT NOT NULL, payload BLOB NOT NULL);
                    CREATE TABLE sources (source_key INTEGER PRIMARY KEY, source_id TEXT NOT NULL UNIQUE,
                        sha256 TEXT NOT NULL,
                        size INTEGER NOT NULL, root TEXT NOT NULL, strategy TEXT NOT NULL,
                        metadata TEXT NOT NULL, indexed_terms INTEGER NOT NULL,
                        index_truncated INTEGER NOT NULL, record_sha256 TEXT NOT NULL);
                    CREATE TABLE terms (term_key INTEGER PRIMARY KEY, term TEXT NOT NULL UNIQUE);
                    CREATE TABLE postings (term_key INTEGER NOT NULL, source_key INTEGER NOT NULL,
                        frequency INTEGER NOT NULL, PRIMARY KEY(term_key, source_key),
                        FOREIGN KEY(term_key) REFERENCES terms(term_key),
                        FOREIGN KEY(source_key) REFERENCES sources(source_key)) WITHOUT ROWID;
                    CREATE INDEX postings_by_source ON postings(source_key);
                """)
                self._db.execute("INSERT INTO store_meta VALUES ('schema', ?)", (VERSION,))
                self._db.execute("INSERT INTO store_meta VALUES ('index_mode', ?)", (index_mode or "indexed",))
                self._db.execute(f"PRAGMA application_id={_APPLICATION_ID}")
                self._db.commit()
            elif (tables != {"store_meta", "nodes", "sources", "terms", "postings"}
                  or self._db.execute("PRAGMA application_id").fetchone()[0] != _APPLICATION_ID
                  or self._db.execute("SELECT value FROM store_meta WHERE key='schema'").fetchone()[0] != VERSION):
                raise CrystalError("Not a supported v2 crystal store; existing artifacts are never migrated")
            saved_mode = self._db.execute("SELECT value FROM store_meta WHERE key='index_mode'").fetchone()
            self.index_mode = saved_mode[0] if saved_mode else "indexed"
            if self.index_mode not in ("indexed", "scan") or (index_mode is not None and index_mode != self.index_mode):
                raise CrystalError("Store index mode differs; existing stores are never converted")
            self._db.execute("PRAGMA journal_mode=DELETE")
            self._db.execute("PRAGMA trusted_schema=OFF")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("PRAGMA synchronous=FULL")
            page_size = self._db.execute("PRAGMA page_size").fetchone()[0]
            self._db.execute(f"PRAGMA max_page_count={MAX_DATABASE_BYTES // page_size}")
        except (sqlite3.Error, TypeError, IndexError) as exc:
            self._db.close()
            raise CrystalCorruptionError("Invalid crystal database: " + str(exc)) from exc
        except Exception:
            self._db.close()
            raise

    def close(self):
        self._db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _source(self, source_id):
        _source_id(source_id)
        row = self._db.execute("SELECT * FROM sources WHERE source_id=? "
                               "AND length(CAST(metadata AS BLOB))<=? AND length(root)=64 "
                               "AND length(sha256)=64 AND length(record_sha256)=64 AND length(strategy)<=9",
                               (source_id, MAX_METADATA_BYTES)).fetchone()
        if row is None:
            if self._db.execute("SELECT 1 FROM sources WHERE source_id=?", (source_id,)).fetchone():
                raise CrystalCorruptionError("Source record exceeds its field bounds")
            raise KeyError(source_id)
        try:
            metadata = json.loads(row["metadata"])
            if type(metadata) is not dict or packet_bytes(metadata).decode("utf-8") != row["metadata"]:
                raise CrystalCorruptionError("Noncanonical source metadata")
            if (not _HEX.fullmatch(row["root"]) or not _HEX.fullmatch(row["sha256"])
                    or row["strategy"] not in STRATEGIES
                    or row["index_truncated"] not in (0, 1)):
                raise CrystalCorruptionError("Invalid source record")
            _integer(row["size"], 0, MAX_SOURCE_BYTES, "Stored source size")
            _integer(row["indexed_terms"], 0, MAX_TERMS, "Stored index terms")
            record = {key: row[key] for key in ("source_id", "sha256", "size", "root", "strategy",
                                               "indexed_terms", "index_truncated")}
            record["metadata"] = metadata
            if _digest(packet_bytes(record)) != row["record_sha256"]:
                raise CrystalCorruptionError("Source record hash mismatch")
            return record
        except (TypeError, ValueError, RecursionError) as exc:
            raise CrystalCorruptionError("Invalid source record") from exc

    def put(self, source_id, data, metadata, strategy="recursive", *, report=True):
        """Append one immutable source; identical bytes are an idempotent no-op.

        An existing ID retains its original metadata and strategy. Changed bytes
        require the caller to supply an explicit different revision ID.
        """
        _source_id(source_id)
        _data(data)
        if type(metadata) is not dict:
            raise CrystalError("metadata must be a JSON object")
        metadata_bytes = packet_bytes(metadata)
        if len(metadata_bytes) > MAX_METADATA_BYTES:
            raise CrystalLimitError("Metadata exceeds 64 KiB")
        if strategy not in STRATEGIES:
            raise CrystalError("Unknown strategy")
        root, nodes = _plan(data, strategy)
        counts, truncated = _terms(data)
        record = {"source_id": source_id, "sha256": _digest(data), "size": len(data),
                  "root": root, "strategy": strategy, "metadata": json.loads(metadata_bytes),
                  "indexed_terms": len(counts), "index_truncated": int(truncated)}
        new_nodes = 0
        idempotent = False
        try:
            self._db.execute("BEGIN IMMEDIATE")
            if self._db.execute("SELECT 1 FROM sources WHERE source_id=?", (source_id,)).fetchone():
                existing = self._source(source_id)
                if existing["sha256"] != record["sha256"] or existing["size"] != len(data):
                    raise CrystalError("source_id already holds different bytes; use a revision ID")
                if self.get(source_id) != data:
                    raise CrystalCorruptionError("Existing source failed exact comparison")
                record = existing
                idempotent = True
            else:
                total, count = self._db.execute("SELECT coalesce(sum(size),0), count(*) FROM sources").fetchone()
                if total + len(data) > MAX_TOTAL_BYTES or count >= MAX_SOURCES:
                    raise CrystalLimitError("Store exceeds 256 MiB managed input or 4096 sources")
                for handle, values in nodes.items():
                    old = self._db.execute("SELECT kind,size,sha256,payload FROM nodes WHERE handle=? "
                                           "AND length(payload)<=?", (handle, MAX_SOURCE_BYTES + 65536)).fetchone()
                    if old is not None:
                        if tuple(old) != values:
                            raise CrystalCorruptionError("Existing shared node differs from its content hash")
                    else:
                        self._db.execute("INSERT INTO nodes VALUES (?,?,?,?,?)", (handle,) + values)
                        new_nodes += 1
                cursor = self._db.execute("INSERT INTO sources "
                                 "(source_id,sha256,size,root,strategy,metadata,indexed_terms,index_truncated,record_sha256) "
                                 "VALUES (?,?,?,?,?,?,?,?,?)",
                                 (source_id, record["sha256"], len(data), root, strategy,
                                  metadata_bytes.decode("utf-8"), len(counts), int(truncated),
                                  _digest(packet_bytes(record))))
                source_key = cursor.lastrowid
                if self.index_mode == "indexed":
                    self._db.executemany("INSERT OR IGNORE INTO terms(term) VALUES (?)",
                                         ((term,) for term in counts))
                    self._db.executemany("INSERT INTO postings(term_key,source_key,frequency) "
                                         "SELECT term_key,?,? FROM terms WHERE term=?",
                                         ((source_key, frequency, term) for term, frequency in counts.items()))
            self._db.commit()
        except sqlite3.Error as exc:
            self._db.rollback()
            raise CrystalCorruptionError("Crystal write failed: " + str(exc)) from exc
        except Exception:
            self._db.rollback()
            raise
        result = {**self._reference(record), "idempotent": idempotent,
                  "new_nodes": new_nodes, "reused_nodes": 0 if idempotent else len(nodes) - new_nodes}
        # Bulk callers take one final inspect; repeated whole-index aggregation
        # and alternate codec measurements are not necessary for an exact append.
        if report:
            result.update(candidate_stats=measure_candidates(data), storage=self.inspect())
        return result

    @staticmethod
    def _reference(record):
        return {"source_id": record["source_id"], "sha256": record["sha256"],
                "root": record["root"], "raw_bytes": record["size"],
                "strategy": record["strategy"], "metadata": record["metadata"],
                "index_truncated": bool(record["index_truncated"])}

    def _decode(self, record):
        cache, active = {}, set()
        visits, work = 0, 0

        def expand(handle, depth):
            nonlocal visits, work
            visits += 1
            if depth > MAX_DEPTH or visits > MAX_GRAPH_VISITS:
                raise CrystalCorruptionError("Graph depth or visit limit exceeded")
            if handle in active:
                raise CrystalCorruptionError("Crystal graph cycle")
            if handle in cache:
                return cache[handle]
            row = self._db.execute("SELECT kind,size,sha256,payload FROM nodes WHERE handle=? "
                                   "AND length(payload)<=? AND length(kind)<=7 AND length(sha256)=64",
                                   (handle, MAX_SOURCE_BYTES + 65536)).fetchone()
            if row is None:
                raise CrystalCorruptionError("Missing or oversized crystal node: " + handle)
            kind, size, sha256, payload = row
            if (type(size) is not int or not 0 <= size <= MAX_SOURCE_BYTES
                    or type(payload) is not bytes or type(sha256) is not str or not _HEX.fullmatch(sha256)):
                raise CrystalCorruptionError("Invalid crystal node fields")
            if _digest(packet_bytes(_descriptor(kind, size, sha256, payload))) != handle:
                raise CrystalCorruptionError("Crystal node handle mismatch")
            work += size
            if work > MAX_DECODE_WORK:
                raise CrystalCorruptionError("Graph reconstruction work limit exceeded")
            active.add(handle)
            if kind == "literal":
                raw = payload
            elif kind == "zlib":
                try:
                    decoder = zlib.decompressobj()
                    raw = decoder.decompress(payload, size + 1)
                    if (len(raw) != size or not decoder.eof or decoder.unused_data
                            or decoder.unconsumed_tail):
                        raise CrystalCorruptionError("Invalid or oversized compressed node")
                except zlib.error as exc:
                    raise CrystalCorruptionError("Invalid compressed node") from exc
            elif kind == "concat":
                if len(payload) > FANOUT * 68 + 2:
                    raise CrystalCorruptionError("Concatenation rule exceeds bound")
                try:
                    children = json.loads(payload)
                except (ValueError, UnicodeError) as exc:
                    raise CrystalCorruptionError("Invalid concatenation rule") from exc
                if (type(children) is not list or not 1 <= len(children) <= FANOUT
                        or any(type(child) is not str or not _HEX.fullmatch(child) for child in children)
                        or packet_bytes(children) != payload):
                    raise CrystalCorruptionError("Invalid concatenation children")
                parts, expanded = [], 0
                for child in children:
                    part = expand(child, depth + 1)
                    expanded += len(part)
                    if expanded > size:
                        raise CrystalCorruptionError("Concatenation exceeds declared size")
                    parts.append(part)
                raw = b"".join(parts)
            else:
                raise CrystalCorruptionError("Unknown crystal codec")
            if len(raw) != size or _digest(raw) != sha256:
                raise CrystalCorruptionError("Crystal decoded size or hash mismatch")
            active.remove(handle)
            cache[handle] = raw
            return raw

        result = expand(record["root"], 0)
        if len(result) != record["size"] or _digest(result) != record["sha256"]:
            raise CrystalCorruptionError("Recovered source size or hash mismatch")
        return result, set(cache)

    def get(self, source_id):
        """Recover only this source's graph; unrelated integrity stays unchecked."""
        try:
            return self._decode(self._source(source_id))[0]
        except sqlite3.Error as exc:
            raise CrystalCorruptionError("Crystal read failed: " + str(exc)) from exc

    def inspect(self):
        """Storage accounting only: no graph or live source is checked here."""
        raw, sources, metadata = self._db.execute(
            "SELECT coalesce(sum(size),0),count(*),coalesce(sum(length(CAST(metadata AS BLOB))),0) FROM sources").fetchone()
        nodes, payload, rules = self._db.execute(
            "SELECT count(*),coalesce(sum(length(payload)),0),"
            "coalesce(sum(CASE WHEN kind='concat' THEN length(payload) ELSE 0 END),0) FROM nodes").fetchone()
        postings = self._db.execute("SELECT count(*) FROM postings").fetchone()[0]
        terms, term_bytes = self._db.execute(
            "SELECT count(*),coalesce(sum(length(CAST(term AS BLOB))),0) FROM terms").fetchone()
        source_id_bytes = self._db.execute(
            "SELECT coalesce(sum(length(CAST(source_id AS BLOB))),0) FROM sources").fetchone()[0]
        page_size = self._db.execute("PRAGMA page_size").fetchone()[0]
        pages = self._db.execute("PRAGMA page_count").fetchone()[0]
        return {"schema": VERSION, "authority": "NONE", "sources": sources,
                "raw_source_bytes": raw, "unique_nodes": nodes, "node_payload_bytes": payload,
                "concat_rule_bytes_in_payload": rules, "source_metadata_bytes": metadata,
                "lexical_postings": postings, "lexical_unique_terms": terms,
                "lexical_term_utf8_bytes": term_bytes, "source_id_utf8_bytes": source_id_bytes,
                "lexical_logical_bytes": term_bytes + 8 * terms + 24 * postings,
                "lexical_logical_bytes_basis": "term UTF-8 bytes once plus one int64 per term and three int64s "
                                               "per posting; logical estimate, not SQLite disk usage",
                "lexical_mode": self.index_mode,
                "lexical_layout": "integer term/source keys; WITHOUT ROWID postings" if self.index_mode == "indexed" else "no persisted lexical postings; queries scan exact source bytes",
                "sqlite_bytes": page_size * pages, "database_file_bytes": self.path.stat().st_size,
                "sqlite_bytes_include": "nodes, provenance, index, schema, page overhead and free pages",
                "archive_integrity": "UNCHECKED", "live_source_freshness": "UNCHECKED",
                "limits": {"source_bytes": MAX_SOURCE_BYTES, "total_input_bytes": MAX_TOTAL_BYTES,
                           "sources": MAX_SOURCES, "depth": MAX_DEPTH, "graph_visits": MAX_GRAPH_VISITS}}

    def freshness(self, source_id, current_data=None):
        """Compare caller-supplied bytes; no stored path or URI is opened."""
        record = self._source(source_id)
        if current_data is None:
            return {"source_id": source_id, "status": "UNCHECKED", "sha256": record["sha256"]}
        _data(current_data)
        current_hash = _digest(current_data)
        return {"source_id": source_id, "status": "MATCH" if current_hash == record["sha256"] else "DRIFT",
                "sha256": record["sha256"], "current_sha256": current_hash,
                "scope": "caller-supplied current bytes only"}

    def verify(self, source_ids=None, current_sources=None):
        """Raise on corruption; distinguish selected graphs, whole store and live bytes.

        Whole-store verification also checks orphan nodes/postings and reconstructs
        the lexical index. Selected verification intentionally leaves other rows
        unchecked. Neither mode proves semantic truth or remote source freshness.
        """
        all_sources = source_ids is None
        if all_sources:
            if self._db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise CrystalCorruptionError("SQLite integrity check failed")
            source_ids = [row[0] for row in self._db.execute("SELECT source_id FROM sources ORDER BY source_id")]
        elif type(source_ids) not in (list, tuple) or len(source_ids) > MAX_SOURCES:
            raise CrystalLimitError("source_ids must be a bounded list or tuple")
        source_ids = list(dict.fromkeys(source_ids))
        if len(source_ids) > MAX_SOURCES:
            raise CrystalCorruptionError("Store source count exceeds bound")
        current_sources = {} if current_sources is None else current_sources
        if type(current_sources) is not dict or set(current_sources) - set(source_ids):
            raise CrystalError("current_sources must map selected source IDs to current bytes")
        visited, recovered_bytes, freshness = set(), 0, []
        for source_id in source_ids:
            record = self._source(source_id)
            raw, nodes = self._decode(record)
            recovered_bytes += len(raw)
            if recovered_bytes > MAX_TOTAL_BYTES:
                raise CrystalCorruptionError("Managed input exceeds store bound")
            visited.update(nodes)
            counts, truncated = _terms(raw)
            stored = dict(self._db.execute(
                "SELECT t.term,p.frequency FROM postings p LEFT JOIN terms t ON p.term_key=t.term_key "
                "WHERE p.source_key=(SELECT source_key FROM sources WHERE source_id=?)", (source_id,)))
            expected = dict(counts) if self.index_mode == "indexed" else {}
            if (expected != stored or len(counts) != record["indexed_terms"]
                    or bool(truncated) != bool(record["index_truncated"])):
                raise CrystalCorruptionError("Lexical index differs from exact source")
            freshness.append(self.freshness(source_id, current_sources.get(source_id)))
        if all_sources:
            if self.index_mode == "scan" and (self._db.execute("SELECT count(*) FROM postings").fetchone()[0]
                                              or self._db.execute("SELECT count(*) FROM terms").fetchone()[0]):
                raise CrystalCorruptionError("Cold store contains unexpected lexical index rows")
            if self._db.execute("SELECT count(*) FROM nodes").fetchone()[0] != len(visited):
                raise CrystalCorruptionError("Unreferenced crystal nodes")
            if self._db.execute("SELECT 1 FROM postings p LEFT JOIN sources s ON p.source_key=s.source_key "
                                "LEFT JOIN terms t ON p.term_key=t.term_key "
                                "WHERE s.source_key IS NULL OR t.term_key IS NULL LIMIT 1").fetchone():
                raise CrystalCorruptionError("Orphan lexical posting")
            if self._db.execute("SELECT 1 FROM terms t LEFT JOIN postings p ON t.term_key=p.term_key "
                                "WHERE p.term_key IS NULL LIMIT 1").fetchone():
                raise CrystalCorruptionError("Unreferenced lexical term")
        return {"schema": VERSION, "authority": "NONE", "archive_integrity": "CHECKED",
                "integrity_scope": "ALL_SOURCES_AND_INDEX" if all_sources else "SELECTED_SOURCES_AND_INDEX",
                "checked_source_ids": source_ids, "checked_nodes": len(visited),
                "recovered_bytes": recovered_bytes, "unselected_sources": "NONE" if all_sources else "UNCHECKED",
                "freshness": freshness, "freshness_scope": "caller-supplied bytes only; others UNCHECKED"}

    def _search(self, query, limit):
        if type(query) is not str or len(query.encode("utf-8")) > 1024:
            raise CrystalLimitError("query must be at most 1024 UTF-8 bytes")
        _integer(limit, 1, 128, "limit")
        terms = sorted(_terms(query.encode("utf-8"))[0])[:32]
        if not terms:
            return [], 0
        if self.index_mode == "scan":
            ids = [row[0] for row in self._db.execute("SELECT source_id FROM sources ORDER BY source_id LIMIT ?", (MAX_SOURCES + 1,))]
            if len(ids) > MAX_SOURCES:
                raise CrystalLimitError("Store source count exceeds bound")
            matches, total, decoded = [], 0, 0
            for source_id in ids:
                raw = self.get(source_id)
                decoded += len(raw)
                if decoded > MAX_TOTAL_BYTES:
                    raise CrystalLimitError("Search scan exceeds total source bound")
                counts, _ = _terms(raw)
                hits = sum(term in counts for term in terms)
                if hits:
                    total += 1
                    matches.append({**self._reference(self._source(source_id)), "matched_terms": hits,
                                    "score": sum(counts[term] for term in terms), "integrity": "CHECKED"})
                    matches.sort(key=lambda item: (-item["matched_terms"], -item["score"], item["source_id"]))
                    del matches[limit:]
            return matches, total
        marks = ",".join("?" for _ in terms)
        sql = ("FROM terms t JOIN postings p ON t.term_key=p.term_key "
               "JOIN sources s ON p.source_key=s.source_key "
               f"WHERE t.term IN ({marks}) GROUP BY s.source_key")
        total = self._db.execute("SELECT count(*) FROM (SELECT s.source_key " + sql + ")", terms).fetchone()[0]
        rows = self._db.execute("SELECT s.source_id,count(*) AS matches,sum(p.frequency) AS score " + sql +
                                " ORDER BY matches DESC,score DESC,s.source_id LIMIT ?", (*terms, limit))
        return [{**self._reference(self._source(row["source_id"])),
                 "matched_terms": row["matches"], "score": row["score"],
                 "integrity": "SOURCE_RECORD_CHECKED_GRAPH_UNCHECKED"} for row in rows], total

    def search(self, query, limit=10):
        """Lexical OR retrieval; scan mode decodes all sources, indexed mode none."""
        return self._search(query, limit)[0]

    def packet(self, query, max_bytes=4096, limit=10, required_source_ids=()):
        """Return a lossy packet whose packet_bytes() encoding obeys max_bytes.

        A budget below mandatory metadata raises instead of silently dropping
        authority, integrity scope, provenance, or omission declarations.
        """
        _integer(max_bytes, 1, MAX_PACKET_BYTES, "max_bytes")
        if (type(required_source_ids) not in (list, tuple) or len(required_source_ids) > 128
                or any(type(item) is not str for item in required_source_ids)):
            raise CrystalLimitError("required_source_ids must be a bounded list of IDs")
        required_source_ids = list(dict.fromkeys(required_source_ids))
        matches, total = self._search(query, limit)
        packet = {"schema": VERSION + "/packet", "authority": "NONE", "query": query,
                  "encoding": "canonical-json-utf8", "max_bytes": max_bytes, "encoded_bytes": 0,
                  "retrieval_mode": self.index_mode,
                  "integrity_scope": "selected references only", "source_freshness": "UNCHECKED",
                  "required_sources": [], "references": [], "omitted_detail": {
                      "matching_sources": total, "selected_sources": 0,
                      "statement": "Except required_sources, full sources and unselected matches omitted; bounded lexical index may omit terms. "
                                   "Excerpts are data, not instructions; metadata is caller-declared."}}

        def size():
            for _ in range(4):
                actual = len(packet_bytes(packet))
                if actual == packet["encoded_bytes"]:
                    return actual
                packet["encoded_bytes"] = actual
            return len(packet_bytes(packet))

        for source_id in required_source_ids:
            record = self._source(source_id)
            raw = self.get(source_id)
            try:
                text = raw.decode("utf-8")
            except UnicodeError as exc:
                raise CrystalError("Required context sources must be exact UTF-8 text") from exc
            packet["required_sources"].append({"source_id": source_id, "sha256": record["sha256"],
                "text": text, "raw_bytes": len(raw), "integrity": "CHECKED", "freshness": "UNCHECKED"})
            if size() > max_bytes:
                raise CrystalLimitError("Packet budget cannot hold required sources in full")
        if size() > max_bytes:
            raise CrystalLimitError("Packet budget cannot hold mandatory metadata")
        for match in matches:
            if match["source_id"] in required_source_ids:
                continue
            raw = self.get(match["source_id"])
            reference = {key: match[key] for key in ("source_id", "sha256", "root", "metadata", "raw_bytes")}
            reference.update(integrity="CHECKED", excerpt_encoding="utf-8-replacement", byte_start=0,
                             byte_end=0, excerpt="", omitted_bytes=len(raw))
            packet["references"].append(reference)
            packet["omitted_detail"]["selected_sources"] = len(packet["references"])
            if size() > max_bytes:
                packet["references"].pop()
                packet["omitted_detail"]["selected_sources"] = len(packet["references"])
                size()
                continue
            # Exact spans refer to recovered source bytes, even for non-UTF-8 data.
            terms = _terms(query.encode("utf-8"))[0]
            starts = [raw.find(term.encode("utf-8")) for term in terms]
            start = max(0, min((n for n in starts if n >= 0), default=0) - 80)
            low, high = 0, min(1024, len(raw) - start)
            while low < high:
                amount = (low + high + 1) // 2
                reference.update(byte_start=start, byte_end=start + amount,
                                 excerpt=raw[start:start + amount].decode("utf-8", errors="replace"),
                                 omitted_bytes=len(raw) - amount)
                if size() <= max_bytes:
                    low = amount
                else:
                    high = amount - 1
            reference.update(byte_start=start, byte_end=start + low,
                             excerpt=raw[start:start + low].decode("utf-8", errors="replace"),
                             omitted_bytes=len(raw) - low)
            if size() > max_bytes:
                # Offset digit growth can make even an empty excerpt too large.
                packet["references"].pop()
                packet["omitted_detail"]["selected_sources"] = len(packet["references"])
        size()
        if len(packet_bytes(packet)) > max_bytes:
            raise CrystalLimitError("Packet budget exceeded")
        return packet
