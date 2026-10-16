"""Storage/latency tradeoff must preserve exact search scoring and source bytes."""
from pathlib import Path
import tempfile
import unittest

from lodestone_machinery.crystals import CrystalStore, CrystalError


class ColdStoreTests(unittest.TestCase):
    def test_cold_recovery_search_and_restart_match_indexed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            docs = {"a": b"approval remains pending exception " * 50,
                    "b": b"exception granted pending pending " * 30,
                    "c": b"other context"}
            observed = {}
            for mode in ("indexed", "scan"):
                with CrystalStore(root / mode, index_mode=mode) as store:
                    for sid, raw in docs.items():
                        store.put(sid, raw, {})
                    self.assertEqual(store.verify()["recovered_bytes"], sum(map(len, docs.values())))
                    hits = store.search("pending exception")
                    observed[mode] = [(h["source_id"], h["score"], h["matched_terms"]) for h in hits]
                    self.assertEqual(store.inspect()["lexical_mode"], mode)
                    if mode == "scan":
                        self.assertEqual(store.inspect()["lexical_postings"], 0)
                with CrystalStore(root / mode) as store:
                    self.assertEqual(store.index_mode, mode)
                    for sid, raw in docs.items():
                        self.assertEqual(store.get(sid), raw)
                    self.assertEqual(store.packet("pending")["retrieval_mode"], mode)
            self.assertEqual(observed["indexed"], observed["scan"])
            before = (root / "scan").read_bytes()
            with self.assertRaisesRegex(CrystalError, "never converted"):
                CrystalStore(root / "scan", index_mode="indexed")
            self.assertEqual((root / "scan").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
