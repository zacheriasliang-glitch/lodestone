"""Exact recovery and adversarial boundaries for the SQLite crystal store."""
import hashlib
import json
from pathlib import Path
import random
import sqlite3
import tempfile
import unittest
from unittest import mock
import zlib

from lodestone_machinery import crystals as cr


class CrystalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "crystals.sqlite"
        self.store = cr.CrystalStore(self.path)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.store.close)

    def put(self, name, data, strategy="recursive", metadata=None):
        return self.store.put(name, data, {"origin": "synthetic test", "epoch": "2026-10-08"}
                              if metadata is None else metadata, strategy=strategy)

    def rewrite_root(self, name, root):
        """Make a hash-consistent source record point at an adversarial graph."""
        record = self.store._source(name)
        record["root"] = root
        self.store._db.execute("UPDATE sources SET root=?,record_sha256=? WHERE source_id=?",
                               (root, cr._digest(cr.packet_bytes(record)), name))
        self.store._db.commit()

    def node(self, kind, raw, payload):
        values = (kind, len(raw), cr._digest(raw), payload)
        handle = cr._digest(cr.packet_bytes(cr._descriptor(*values)))
        self.store._db.execute("INSERT OR REPLACE INTO nodes VALUES (?,?,?,?,?)", (handle,) + values)
        self.store._db.commit()
        return handle

    def test_exact_roundtrip_all_strategies_and_reopen(self):
        rng = random.Random(401)
        cases = [b"", bytes(range(256)) * 20, b"one\r\ntwo\nthree\r\n",
                 "樹と記憶 — café 🧭\r\n".encode(), bytes(rng.randrange(256) for _ in range(19003))]
        expected = {}
        for strategy in cr.STRATEGIES:
            for index, data in enumerate(cases):
                name = f"{strategy}/{index}"
                result = self.put(name, data, strategy)
                self.assertEqual(result["sha256"], hashlib.sha256(data).hexdigest())
                self.assertEqual(self.store.get(name), data)
                expected[name] = data
        with cr.CrystalStore(self.path) as reopened:
            for name, data in expected.items():
                self.assertEqual(reopened.get(name), data)
            verified = reopened.verify()
            self.assertEqual(verified["integrity_scope"], "ALL_SOURCES_AND_INDEX")
            self.assertEqual(verified["recovered_bytes"], sum(map(len, expected.values())))
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["crystals.sqlite"])

    def test_identical_source_id_is_immutable_idempotent(self):
        first = self.put("doc", b"exact words", metadata={"origin": "first"})
        before = self.path.read_bytes()
        second = self.put("doc", b"exact words", "literal", metadata={"origin": "second"})
        self.assertTrue(second["idempotent"])
        self.assertEqual(second["metadata"], {"origin": "first"})
        self.assertEqual(second["root"], first["root"])
        self.assertEqual(self.path.read_bytes(), before)
        with self.assertRaisesRegex(cr.CrystalError, "different bytes"):
            self.put("doc", b"new words")
        self.assertEqual(self.store.get("doc"), b"exact words")

    def test_exact_shared_chunks_and_batched_rules(self):
        shared = b"shared crystal paragraph\n".ljust(cr.CHUNK_BYTES, b".")
        first = self.put("a", shared * 70 + b"A")
        second = self.put("b", shared * 70 + b"B")
        self.assertGreater(second["reused_nodes"], 0)
        self.assertLess(first["new_nodes"], 10)
        self.assertEqual(self.store.get("b"), shared * 70 + b"B")
        self.assertLess(self.store.inspect()["unique_nodes"], 15)
        self.assertEqual(self.store.verify()["checked_source_ids"], ["a", "b"])

    def test_payload_corruption_fails_without_false_whole_store_claim(self):
        good = self.put("good", b"good source")
        bad = self.put("bad", b"bad source", "literal")
        self.store._db.execute("UPDATE nodes SET payload=? WHERE handle=?", (b"different!", bad["root"]))
        self.store._db.commit()
        self.assertEqual(self.store.get("good"), b"good source")
        selected = self.store.verify(["good"])
        self.assertEqual(selected["unselected_sources"], "UNCHECKED")
        self.assertEqual(selected["checked_source_ids"], ["good"])
        self.assertEqual(self.store.inspect()["archive_integrity"], "UNCHECKED")
        with self.assertRaisesRegex(cr.CrystalCorruptionError, "handle mismatch"):
            self.store.get("bad")
        with self.assertRaises(cr.CrystalCorruptionError):
            self.store.verify()

    def test_missing_child_fails_selective_recovery(self):
        result = self.put("doc", b"a" * cr.CHUNK_BYTES + b"tail")
        children = json.loads(self.store._db.execute("SELECT payload FROM nodes WHERE handle=?",
                                                     (result["root"],)).fetchone()[0])
        self.store._db.execute("DELETE FROM nodes WHERE handle=?", (children[0],))
        self.store._db.commit()
        with self.assertRaisesRegex(cr.CrystalCorruptionError, "Missing"):
            self.store.get("doc")

    def test_source_provenance_is_hash_bound(self):
        self.put("doc", b"data")
        self.store._db.execute("UPDATE sources SET metadata='{}' WHERE source_id='doc'")
        self.store._db.commit()
        with self.assertRaisesRegex(cr.CrystalCorruptionError, "source record"):
            self.store.get("doc")

    def test_direct_cycle_and_oversized_metadata_fail_closed(self):
        result = self.put("doc", b"data")
        self.store._db.execute("UPDATE nodes SET kind='concat',payload=? WHERE handle=?",
                               (cr.packet_bytes([result["root"]]), result["root"]))
        self.store._db.commit()
        with self.assertRaises(cr.CrystalCorruptionError):
            self.store.get("doc")
        self.store._db.execute("UPDATE sources SET metadata=? WHERE source_id='doc'",
                               ("x" * (cr.MAX_METADATA_BYTES + 1),))
        self.store._db.commit()
        with self.assertRaisesRegex(cr.CrystalCorruptionError, "field bounds"):
            self.store.get("doc")

    def test_compressed_bomb_trailing_stream_and_wrong_hash_fail(self):
        self.put("doc", b"a")
        for payload in (zlib.compress(b"a" * 1000000), zlib.compress(b"a") + b"trailing",
                        zlib.compress(b"b")):
            with self.subTest(payload_size=len(payload)):
                root = self.node("zlib", b"a", payload)
                self.rewrite_root("doc", root)
                with self.assertRaises(cr.CrystalCorruptionError):
                    self.store.get("doc")

    def test_depth_bound_with_hash_consistent_graph(self):
        result = self.put("doc", b"x", "literal")
        root = result["root"]
        for _ in range(cr.MAX_DEPTH + 1):
            root = self.node("concat", b"x", cr.packet_bytes([root]))
        self.rewrite_root("doc", root)
        with self.assertRaisesRegex(cr.CrystalCorruptionError, "depth"):
            self.store.get("doc")

    def test_fanout_and_output_size_bounds(self):
        leaf = self.put("doc", b"x", "literal")["root"]
        root = self.node("concat", b"x", cr.packet_bytes([leaf] * (cr.FANOUT + 1)))
        self.rewrite_root("doc", root)
        with self.assertRaises(cr.CrystalCorruptionError):
            self.store.get("doc")
        root = self.node("concat", b"x", cr.packet_bytes([leaf, leaf]))
        self.rewrite_root("doc", root)
        with self.assertRaisesRegex(cr.CrystalCorruptionError, "declared size"):
            self.store.get("doc")

    def test_input_and_aggregate_bounds_are_atomic(self):
        with self.assertRaises(cr.CrystalLimitError):
            self.put("big", b"x" * (cr.MAX_SOURCE_BYTES + 1))
        with mock.patch.object(cr, "MAX_TOTAL_BYTES", 12):
            self.put("one", b"12345678")
            before = self.store.inspect()
            with self.assertRaises(cr.CrystalLimitError):
                self.put("two", b"12345")
            self.assertEqual(self.store.inspect(), before)
        with mock.patch.object(cr, "MAX_SOURCES", 1):
            with self.assertRaises(cr.CrystalLimitError):
                self.put("two", b"")
        with self.assertRaises(cr.CrystalLimitError):
            self.put("meta", b"", metadata={"x": "x" * cr.MAX_METADATA_BYTES})
        self.assertEqual(self.store.inspect()["sources"], 1)

    def test_maximum_source_recovers_with_bounded_hierarchy(self):
        data = bytes(range(256)) * (cr.MAX_SOURCE_BYTES // 256)
        self.put("max", data)
        self.assertEqual(self.store.get("max"), data)
        self.assertLess(self.store.inspect()["unique_nodes"], 10)

    def test_freshness_explicitly_distinguishes_checked_and_unchecked(self):
        self.put("a", b"a", metadata={"path": "/never/open/this", "authority": "HOST"})
        self.put("b", b"b")
        self.put("c", b"c")
        result = self.store.verify(current_sources={"a": b"a", "b": b"changed"})
        self.assertEqual([row["status"] for row in result["freshness"]], ["MATCH", "DRIFT", "UNCHECKED"])
        self.assertEqual(result["authority"], "NONE")
        self.assertEqual(self.store.freshness("a")["status"], "UNCHECKED")
        with self.assertRaises(cr.CrystalError):
            self.store.verify(["a"], current_sources={"b": b"b"})

    def test_search_uses_index_and_does_not_decode_graph(self):
        self.put("a", b"crystal crystal provenance")
        self.put("b", b"crystal water")
        self.put("c", b"unrelated")
        with mock.patch.object(self.store, "_decode", side_effect=AssertionError("must not decode")):
            matches = self.store.search("CRYSTAL provenance")
        self.assertEqual([row["source_id"] for row in matches], ["a", "b"])
        self.assertEqual(matches[0]["matched_terms"], 2)
        self.assertEqual(matches[0]["integrity"], "SOURCE_RECORD_CHECKED_GRAPH_UNCHECKED")
        self.assertEqual(self.store.search("doesnotexist"), [])
        self.assertEqual(self.store.search(""), [])

    def test_packet_strict_whole_encoding_budget_and_mandatory_fields(self):
        self.put("a", ("crystal \"quoted\" 🧭\r\n" * 500).encode())
        self.put("b", b"crystal\x00\xff" * 200)
        self.put("huge_meta", b"crystal", metadata={"large": "x" * 5000})
        for budget in (700, 1000, 1500, 4096):
            with self.subTest(budget=budget):
                packet = self.store.packet("crystal", max_bytes=budget)
                self.assertLessEqual(len(cr.packet_bytes(packet)), budget)
                self.assertEqual(packet["encoded_bytes"], len(cr.packet_bytes(packet)))
                self.assertEqual(packet["authority"], "NONE")
                self.assertEqual(packet["source_freshness"], "UNCHECKED")
                self.assertEqual(packet["omitted_detail"]["matching_sources"], 3)
                for reference in packet["references"]:
                    self.assertEqual(len(reference["sha256"]), 64)
                    self.assertIn("metadata", reference)
                    self.assertEqual(reference["omitted_bytes"], reference["raw_bytes"] -
                                     reference["byte_end"] + reference["byte_start"])
        with self.assertRaisesRegex(cr.CrystalLimitError, "mandatory metadata"):
            self.store.packet("crystal", max_bytes=100)

    def test_packet_does_not_decode_unrelated_corrupt_source(self):
        self.put("selected", b"crystal selection")
        bad = self.put("other", b"irrelevant bytes")
        self.store._db.execute("UPDATE nodes SET payload=x'00' WHERE handle=?", (bad["root"],))
        self.store._db.commit()
        packet = self.store.packet("crystal")
        self.assertEqual([ref["source_id"] for ref in packet["references"]], ["selected"])
        with self.assertRaises(cr.CrystalCorruptionError):
            self.store.verify()

    def test_index_corruption_and_orphan_nodes_are_found_by_verify(self):
        self.put("a", b"crystal selection")
        self.store._db.execute("UPDATE postings SET frequency=99 WHERE term_key="
                               "(SELECT term_key FROM terms WHERE term='crystal')")
        self.store._db.commit()
        self.assertEqual(self.store.get("a"), b"crystal selection")
        with self.assertRaisesRegex(cr.CrystalCorruptionError, "Lexical index"):
            self.store.verify()
        self.store._db.execute("UPDATE postings SET frequency=1 WHERE term_key="
                               "(SELECT term_key FROM terms WHERE term='crystal')")
        self.store._db.commit()
        self.node("literal", b"orphan", b"orphan")
        self.assertEqual(self.store.verify(["a"])["unselected_sources"], "UNCHECKED")
        with self.assertRaisesRegex(cr.CrystalCorruptionError, "Unreferenced"):
            self.store.verify()

    def test_index_truncation_is_declared_and_verifiable(self):
        with mock.patch.object(cr, "MAX_TERMS", 2):
            result = self.put("a", b"one two three one")
            self.assertTrue(result["index_truncated"])
            self.assertEqual(self.store.search("three"), [])
            self.assertEqual(self.store.verify()["checked_source_ids"], ["a"])

    def test_inspection_counts_complete_sqlite_not_just_payload(self):
        result = self.put("a", b"a " * 10000)
        storage = result["storage"]
        self.assertEqual(storage["database_file_bytes"], self.path.stat().st_size)
        self.assertEqual(storage["sqlite_bytes"], storage["database_file_bytes"])
        self.assertGreater(storage["sqlite_bytes"], storage["node_payload_bytes"])
        self.assertGreater(storage["lexical_logical_bytes"], 0)
        self.assertGreater(storage["source_metadata_bytes"], 0)
        self.assertEqual(set(result["candidate_stats"]), set(cr.STRATEGIES))
        self.assertIn("SQLite pages and indexes", result["candidate_stats"]["zlib"]["excludes"])

    def test_v1_artifact_is_rejected_without_any_byte_change(self):
        legacy = self.path.parent / "v1.sqlite"
        db = sqlite3.connect(legacy)
        try:
            db.executescript("""
                CREATE TABLE store_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE nodes (handle TEXT PRIMARY KEY, kind TEXT NOT NULL,
                    size INTEGER NOT NULL, sha256 TEXT NOT NULL, payload BLOB NOT NULL);
                CREATE TABLE sources (source_id TEXT PRIMARY KEY, sha256 TEXT NOT NULL,
                    size INTEGER NOT NULL, root TEXT NOT NULL, strategy TEXT NOT NULL,
                    metadata TEXT NOT NULL, indexed_terms INTEGER NOT NULL,
                    index_truncated INTEGER NOT NULL, record_sha256 TEXT NOT NULL);
                CREATE TABLE postings (term TEXT NOT NULL, source_id TEXT NOT NULL,
                    frequency INTEGER NOT NULL, PRIMARY KEY(term, source_id));
                CREATE INDEX postings_by_source ON postings(source_id);
                INSERT INTO store_meta VALUES ('schema', 'yggdrasil-crystals/v1');
            """)
            db.execute(f"PRAGMA application_id={cr._APPLICATION_ID}")
            db.commit()
        finally:
            db.close()
        before = legacy.read_bytes()
        with self.assertRaisesRegex(cr.CrystalError, "never migrated"):
            cr.CrystalStore(legacy)
        self.assertEqual(legacy.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in legacy.parent.iterdir()), ["crystals.sqlite", "v1.sqlite"])

    def test_normalized_index_keeps_all_terms_frequencies_and_ranking(self):
        data = {
            "z/source/with/a/long/identity": b"alpha alpha beta delta",
            "a/source/with/a/long/identity": b"alpha beta beta delta",
            "m/source/with/a/long/identity": b"beta gamma",
        }
        for name, raw in data.items():
            result = self.store.put(name, raw, {}, report=False)
            self.assertNotIn("storage", result)
            self.assertNotIn("candidate_stats", result)
        storage = self.store.inspect()
        self.assertEqual(storage["schema"], "yggdrasil-crystals/v2")
        self.assertEqual(storage["lexical_postings"], 8)
        self.assertEqual(storage["lexical_unique_terms"], 4)
        self.assertEqual(storage["lexical_term_utf8_bytes"], len("alphabetagammadelta"))
        self.assertEqual(storage["source_id_utf8_bytes"], sum(len(name.encode()) for name in data))
        self.assertEqual(storage["lexical_logical_bytes"], len("alphabetagammadelta") + 8 * 4 + 24 * 8)
        self.assertIn("logical estimate", storage["lexical_logical_bytes_basis"])
        rows = list(self.store._db.execute("SELECT typeof(term_key),typeof(source_key),typeof(frequency) FROM postings"))
        self.assertTrue(all(tuple(row) == ("integer", "integer", "integer") for row in rows))
        ddl = self.store._db.execute("SELECT sql FROM sqlite_master WHERE name='postings'").fetchone()[0]
        self.assertIn("WITHOUT ROWID", ddl)
        matches = self.store.search("alpha beta")
        self.assertEqual([(row["source_id"], row["matched_terms"], row["score"]) for row in matches],
                         [("a/source/with/a/long/identity", 2, 3),
                          ("z/source/with/a/long/identity", 2, 3),
                          ("m/source/with/a/long/identity", 1, 1)])
        self.assertEqual(self.store.verify()["checked_source_ids"], sorted(data))
        required = self.store.packet("notamatch", required_source_ids=[next(iter(data))])
        self.assertEqual(required["required_sources"][0]["text"], next(iter(data.values())).decode())

    def test_missing_term_link_and_unused_dictionary_term_are_corruption(self):
        self.put("doc", b"one two")
        # Simulate externally damaged storage, bypassing ordinary foreign-key guards.
        self.store._db.execute("PRAGMA foreign_keys=OFF")
        self.store._db.execute("INSERT INTO postings VALUES (999999,1,1)")
        self.store._db.commit()
        self.assertEqual(self.store.get("doc"), b"one two")
        with self.assertRaisesRegex(cr.CrystalCorruptionError, "Lexical index"):
            self.store.verify(["doc"])
        self.store._db.execute("DELETE FROM postings WHERE term_key=999999")
        self.store._db.execute("INSERT INTO terms(term) VALUES ('unreferenced')")
        self.store._db.commit()
        self.assertEqual(self.store.verify(["doc"])["checked_source_ids"], ["doc"])
        with self.assertRaisesRegex(cr.CrystalCorruptionError, "Unreferenced lexical term"):
            self.store.verify()

    def test_orphan_source_link_and_changed_dictionary_text_are_corruption(self):
        self.put("doc", b"one two")
        self.store._db.execute("PRAGMA foreign_keys=OFF")
        self.store._db.execute("INSERT INTO postings VALUES (1,999999,1)")
        self.store._db.commit()
        self.assertEqual(self.store.verify(["doc"])["checked_source_ids"], ["doc"])
        with self.assertRaisesRegex(cr.CrystalCorruptionError, "Orphan lexical posting"):
            self.store.verify()
        self.store._db.execute("DELETE FROM postings WHERE source_key=999999")
        self.store._db.execute("UPDATE terms SET term='modified' WHERE term='one'")
        self.store._db.commit()
        with self.assertRaisesRegex(cr.CrystalCorruptionError, "Lexical index"):
            self.store.verify()

    def test_invalid_api_inputs_and_unrelated_database_are_not_overwritten(self):
        for name, data, metadata, strategy in (("", b"", {}, "recursive"),
                                               ("a", "text", {}, "recursive"),
                                               ("a", b"", [], "recursive"),
                                               ("a", b"", {}, "unknown")):
            with self.assertRaises(cr.CrystalError):
                self.store.put(name, data, metadata, strategy)
        other = self.path.parent / "unrelated.sqlite"
        db = sqlite3.connect(other)
        try:
            db.execute("CREATE TABLE private (value TEXT)")
            db.execute("INSERT INTO private VALUES ('keep')")
            db.commit()
        finally:
            db.close()
        before = other.read_bytes()
        with self.assertRaises(cr.CrystalError):
            cr.CrystalStore(other)
        self.assertEqual(other.read_bytes(), before)
        link = self.path.parent / "link.sqlite"
        link.symlink_to(self.path)
        with self.assertRaises(cr.CrystalError):
            cr.CrystalStore(link)
        self.assertEqual(self.store.inspect()["sources"], 0)


if __name__ == "__main__":
    unittest.main()
