"""Five original literal/validation compact-catalog baseline tests, without neural fixtures.

Method bodies are copied unchanged from test_compact_checkpoint.py at
5cd4cf8aa0211b0264607b0e17c9d91346bb22cc. Only required literal scaffolding is
retained; the original prepared property, compilation and model fixtures are absent.
"""
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from lodestone_machinery.crystals import CrystalStore
from lodestone_machinery.latent import compact_checkpoint as cp
from lodestone_machinery.latent import compact_layout as layout


def source(identifier, raw, mandatory=False):
    return {"id": identifier, "data": raw, "mandatory": mandatory}

def artifact_id(record):
    return "compact:" + record["kind"] + ":" + record["sha256"]


class CompactDefaultTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = CrystalStore(self.root / "invented.sqlite", index_mode="scan")
        self.addCleanup(self.store.close)
        self.host = b"Preserve this explicit invented condition."

    def build(self, sources, **options):
        built = cp.build_catalog(self.store, sources, **options)
        pin = built["directory"]
        return pin, layout.decode(self.store.get(pin["id"]))

    def literal_fixture(self):
        alpha = b"A" * 1024 + b"tail-17"
        beta = b"Unrequested invented source."
        pin, directory = self.build([source("alpha", alpha), source("beta", beta),
                                     source("host", self.host, True)])
        return pin, directory, alpha, beta

    @staticmethod
    def entry(directory, identifier):
        return next(row for row in directory["sources"] if row["id"] == identifier)

    def chunk_record(self, directory, identifier, ordinal=0):
        chunk = self.entry(directory, identifier)["chunks"][ordinal]
        return directory["artifacts"][chunk["record"]]

    def test_literal_preserves_flags_aliases_unicode_empty_sources_and_tails(self):
        rows = [source("\u00e9", b"same bytes!"), source("e\u0301", b"same bytes!"),
                source("empty", b""), source("host", self.host, True)]
        pin, directory = self.build(rows)
        self.assertEqual(len(directory["sources"]), 4)
        self.assertFalse(self.entry(directory, "\u00e9")["mandatory"])
        self.assertFalse(self.entry(directory, "e\u0301")["mandatory"])
        self.assertEqual(self.entry(directory, "\u00e9")["chunks"][0]["record"],
                         self.entry(directory, "e\u0301")["chunks"][0]["record"])
        self.assertTrue(all(row["kind"] == "literal" for row in directory["artifacts"]))
        session = cp.ReadSession(self.store, pin)
        for row in rows:
            with self.subTest(identifier=row["id"]):
                result = session.read(row["id"])
                self.assertEqual(result["data"], row["data"])
                self.assertEqual(result["mandatory"], {"host": self.host})
                self.assertEqual(result["selected_whole_source_integrity"], "CHECKED")
                self.assertIn(row["id"], result["checked_whole_source_ids"])
                self.assertEqual(result["global_integrity"], "UNCHECKED")

    def test_selective_read_gets_only_intersections_and_mandatory_payloads(self):
        pin, directory, alpha, _ = self.literal_fixture()
        with mock.patch.object(self.store, "get", wraps=self.store.get) as getter:
            result = cp.ReadSession(self.store, pin).read("alpha", 1025, 1029)
        ids = [call.args[0] for call in getter.call_args_list]
        self.assertEqual(result["data"], alpha[1025:1029])
        self.assertEqual(result["mandatory"], {"host": self.host})
        self.assertNotIn(artifact_id(self.chunk_record(directory, "alpha", 0)), ids)
        self.assertNotIn(artifact_id(self.chunk_record(directory, "beta")), ids)
        self.assertIn(artifact_id(self.chunk_record(directory, "alpha", 1)), ids)
        self.assertEqual(result["selected_whole_source_integrity"], "UNCHECKED")
        self.assertEqual(result["checked_whole_source_ids"], ["host"])
        self.assertEqual(result["metrics"]["chunks_decoded"], 2)
        self.assertEqual(result["metrics"]["full_chunk_bytes_decoded"], len(alpha) - 1024 + len(self.host))
        self.assertEqual(result["metrics"]["requested_bytes_returned"], 4)

    def test_empty_request_still_recovers_every_mandatory_source(self):
        pin, directory, _, _ = self.literal_fixture()
        session = cp.ReadSession(self.store, pin)
        with mock.patch.object(self.store, "get", wraps=self.store.get) as getter:
            result = session.read("alpha", 3, 3)
        ids = [call.args[0] for call in getter.call_args_list]
        self.assertEqual(result["data"], b"")
        self.assertEqual(result["mandatory"], {"host": self.host})
        self.assertEqual(result["metrics"]["chunks_decoded"], 1)
        self.assertEqual(result["metrics"]["mandatory_bytes_returned"], len(self.host))
        self.assertNotIn(artifact_id(self.chunk_record(directory, "alpha")), ids)
        self.assertEqual(result["selected_whole_source_integrity"], "UNCHECKED")

    def test_invalid_ranges_fail_before_body_reads(self):
        pin, _, alpha, _ = self.literal_fixture()
        session = cp.ReadSession(self.store, pin)
        before = session.metrics()["logical_get_calls"]
        for args in (("alpha", -1, 2), ("alpha", 2, 1),
                     ("alpha", 0, len(alpha) + 1), ("alpha", True, 2)):
            with self.subTest(args=args), self.assertRaises(cp.CheckpointError):
                session.read(*args)
        with self.assertRaises(KeyError):
            session.read("missing")
        self.assertEqual(session.metrics()["logical_get_calls"], before)

    def test_duplicate_ids_and_bad_source_bounds_fail_before_writes(self):
        cases = ([source("same", b"a"), source("same", b"b")],
                 [source("oversize", b"x" * 65537)],
                 [source("bad", b"x", 1)],
                 [source(str(i), b"") for i in range(65)])
        for rows in cases:
            with self.subTest(count=len(rows)), self.assertRaises(cp.CheckpointError):
                cp.build_catalog(self.store, rows)
            self.assertEqual(self.store.inspect()["sources"], 0)


if __name__ == "__main__":
    unittest.main()
