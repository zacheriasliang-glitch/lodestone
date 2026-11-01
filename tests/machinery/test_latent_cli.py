"""Actual Origami/Yggdrasil dispatch, pre-write rejection and standalone recovery."""
import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from lodestone_machinery.__main__ import main
from lodestone_machinery.crystals import CrystalStore
from lodestone_machinery.latent import codec, candidate_inputs, latent_model, store_adapter
from lodestone_machinery.latent import cli


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class LatentCLITests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.raw = bytes(8) + b"tail"
        self.model = b"explicit synthetic provenance"
        self.table = bytes(codec.TABLE_BYTES)
        source = dict(id="example", data=self.raw, mandatory=False)
        self.plan = dict(schema=candidate_inputs.SCHEMA,
            corpus_sha256=candidate_inputs.corpus_sha256([source]), table_sha256=sha(self.table),
            sources=[dict(id="example", sha256=sha(self.raw), bytes=len(self.raw), chunks=[
                dict(offset=0, bytes=len(self.raw), sha256=sha(self.raw), candidates=[[[-8, -8, -8]]])])])
        for name, raw in (("input", self.raw), ("model", self.model), ("table", self.table)):
            (self.root / name).write_bytes(raw)
        self.save_plan(self.plan)
        for module, name in ((store_adapter, "prepare_model"), (store_adapter.PreparedModel, "encode"),
                             (latent_model, "initialize"), (latent_model, "encode_block"),
                             (latent_model, "compile_decoder")):
            patcher = mock.patch.object(module, name, side_effect=AssertionError("No model execution"))
            patcher.start()
            self.addCleanup(patcher.stop)

    def save_plan(self, plan):
        (self.root / "plan").write_text(json.dumps(plan))

    def invoke(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(map(str, args)))
        return code, out.getvalue(), err.getvalue()

    def put_args(self):
        return ["origami", "crystal", "latent", "put", "--store", self.root / "store.sqlite",
            "--receipt", self.root / "receipt.json", "--source-id", "example", "--file", self.root / "input",
            "--model", self.root / "model", "--table", self.root / "table",
            "--expected-model-sha256", sha(self.model), "--expected-table-sha256", sha(self.table),
            "--candidates", self.root / "plan", "--index-mode", "scan"]

    def get_args(self):
        return ["yggdrasil", "crystal", "latent", "get", "--store", self.root / "store.sqlite",
            "--receipt", self.root / "receipt.json", "--output", self.root / "recovered"]

    def test_actual_dispatch_retained_cost_and_recovery_without_inputs(self):
        code, output, error = self.invoke(*self.put_args())
        self.assertEqual(code, 0, error)
        result = json.loads(output)
        self.assertEqual(result["layer_accounting"][0]["encoder_calls"], 0)
        self.assertEqual(result["bundle_provenance"], "CALLER_PINNED_BYTES_NOT_COMPILE_AUTHENTICATED")
        cost = result["retained_cost"]
        self.assertEqual(cost["database_plus_receipt_bytes"], sum((self.root / p).stat().st_size
            for p in ("store.sqlite", "receipt.json")))
        self.assertGreater(cost["database_plus_receipt_bytes"], len(self.raw))
        # Only disposable test inputs are removed; the decoder cannot use them.
        for p in ("input", "model", "table", "plan"):
            (self.root / p).unlink()
        database_before = (self.root / "store.sqlite").read_bytes()
        receipt_before = (self.root / "receipt.json").read_bytes()
        code, output, error = self.invoke(*self.get_args())
        self.assertEqual(code, 0, error)
        self.assertEqual((self.root / "recovered").read_bytes(), self.raw)
        self.assertEqual(json.loads(output)["source_sha256"], sha(self.raw))
        self.assertEqual((self.root / "store.sqlite").read_bytes(), database_before)
        self.assertEqual((self.root / "receipt.json").read_bytes(), receipt_before)
        for suffix in ("-journal", "-wal", "-shm"):
            self.assertFalse((self.root / ("store.sqlite" + suffix)).exists())

    def test_quiescent_wal_recovery_rejected_without_opening_sqlite(self):
        code, _, error = self.invoke(*self.put_args())
        self.assertEqual(code, 0, error)
        database = self.root / "store.sqlite"
        connection = sqlite3.connect(database)
        try:
            self.assertEqual(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
        finally:
            connection.close()
        # A closed WAL database can have no companion files and still require
        # preservation: its persistent read/write format versions remain 2/2.
        for suffix in ("-journal", "-wal", "-shm"):
            self.assertFalse((self.root / ("store.sqlite" + suffix)).exists())
        self.assertEqual(database.read_bytes()[18:20], b"\x02\x02")
        before = {path.name: path.read_bytes() for path in self.root.iterdir()}
        with mock.patch.object(cli.sqlite3, "connect", side_effect=AssertionError("Must not open SQLite")):
            code, _, error = self.invoke(*self.get_args())
        self.assertEqual(code, 2)
        self.assertIn("WAL-mode recovery is unsupported", error)
        self.assertEqual({path.name: path.read_bytes() for path in self.root.iterdir()}, before)
        self.assertFalse((self.root / "recovered").exists())

    def test_invalid_sqlite_headers_rejected_before_opening_sqlite(self):
        code, _, error = self.invoke(*self.put_args())
        self.assertEqual(code, 0, error)
        database = self.root / "store.sqlite"
        original = database.read_bytes()
        invalid = [original[:99], b"not SQLite!" + original[11:]]
        for versions in (b"\x00\x00", b"\x03\x03", b"\x01\x02", b"\x02\x01"):
            invalid.append(original[:18] + versions + original[20:])
        for index, contents in enumerate(invalid):
            with self.subTest(header=index):
                database.write_bytes(contents)
                before = {path.name: path.read_bytes() for path in self.root.iterdir()}
                with mock.patch.object(cli.sqlite3, "connect", side_effect=AssertionError("Must not open SQLite")):
                    code, _, _ = self.invoke(*self.get_args())
                self.assertEqual(code, 2)
                self.assertEqual({path.name: path.read_bytes() for path in self.root.iterdir()}, before)
                self.assertFalse((self.root / "recovered").exists())

    def test_invalid_plan_never_creates_database_or_receipt(self):
        broken = copy.deepcopy(self.plan)
        broken["sources"][0]["chunks"][0]["candidates"] = [[[-8, -8, -8]] * 65]
        self.save_plan(broken)
        code, _, _ = self.invoke(*self.put_args())
        self.assertEqual(code, 2)
        self.assertFalse((self.root / "store.sqlite").exists())
        self.assertFalse((self.root / "receipt.json").exists())

    def test_invalid_plan_never_opens_existing_database(self):
        path = self.root / "store.sqlite"
        with CrystalStore(path) as store:
            store.put("sentinel", b"preserved", {}, strategy="literal", report=False)
        before = path.read_bytes()
        broken = copy.deepcopy(self.plan)
        broken["sources"][0]["chunks"][0]["offset"] = True
        self.save_plan(broken)
        with mock.patch.object(cli, "CrystalStore", side_effect=AssertionError("Must not open store")):
            code, _, _ = self.invoke(*self.put_args())
        self.assertEqual(code, 2)
        self.assertEqual(path.read_bytes(), before)

    def test_bad_hash_duplicate_keys_and_bad_budget_fail_before_store(self):
        cases = [self.put_args() + ["--expected-model-sha256", "0" * 64],
                 self.put_args() + ["--max-seconds", "0"]]
        for args in cases:
            code, _, _ = self.invoke(*args)
            self.assertEqual(code, 2)
            self.assertFalse((self.root / "store.sqlite").exists())
        (self.root / "plan").write_text('{"schema":"one","schema":"two"}')
        code, _, _ = self.invoke(*self.put_args())
        self.assertEqual(code, 2)
        self.assertFalse((self.root / "store.sqlite").exists())

    def test_input_output_alias_and_existing_receipt_preserved(self):
        before = (self.root / "input").read_bytes()
        code, _, _ = self.invoke(*(self.put_args() + ["--store", self.root / "input"]))
        self.assertEqual(code, 2)
        self.assertEqual((self.root / "input").read_bytes(), before)
        (self.root / "receipt.json").write_bytes(b"preserve")
        code, _, _ = self.invoke(*self.put_args())
        self.assertEqual(code, 2)
        self.assertEqual((self.root / "receipt.json").read_bytes(), b"preserve")
        self.assertFalse((self.root / "store.sqlite").exists())

    def test_missing_recovery_store_and_existing_output_never_created_or_overwritten(self):
        code, _, _ = self.invoke(*self.get_args())
        self.assertEqual(code, 2)
        self.assertFalse((self.root / "store.sqlite").exists())
        (self.root / "recovered").write_bytes(b"preserve")
        code, _, _ = self.invoke(*self.get_args())
        self.assertEqual(code, 2)
        self.assertEqual((self.root / "recovered").read_bytes(), b"preserve")

    def test_sqlite_companion_inputs_and_outputs_are_protected(self):
        for suffix in ("-journal", "-wal", "-shm"):
            with self.subTest(suffix=suffix):
                companion = self.root / ("store.sqlite" + suffix)
                companion.write_bytes(self.raw)
                code, _, _ = self.invoke(*(self.put_args() + ["--file", companion]))
                self.assertEqual(code, 2)
                self.assertEqual(companion.read_bytes(), self.raw)
                self.assertFalse((self.root / "store.sqlite").exists())
                # Even an unrelated preexisting companion must be preserved.
                code, _, _ = self.invoke(*self.put_args())
                self.assertEqual(code, 2)
                self.assertEqual(companion.read_bytes(), self.raw)
                companion.unlink()
                code, _, _ = self.invoke(*(self.put_args() + ["--receipt", companion]))
                self.assertEqual(code, 2)
                self.assertFalse((self.root / "store.sqlite").exists())
                code, _, _ = self.invoke(*(self.get_args() + ["--output", companion]))
                self.assertEqual(code, 2)
                self.assertFalse(companion.exists())

    def test_bad_manifest_binding_does_not_initialize_existing_empty_database(self):
        path = self.root / "store.sqlite"
        path.write_bytes(b"")
        receipt = dict(schema=cli.RECEIPT_SCHEMA, manifest_id="wrong", manifest_sha256="0" * 64,
                       source_sha256=sha(self.raw), source_bytes=len(self.raw))
        (self.root / "receipt.json").write_text(json.dumps(receipt))
        code, _, _ = self.invoke(*self.get_args())
        self.assertEqual(code, 2)
        self.assertEqual(path.read_bytes(), b"")
        receipt["manifest_id"] = "latent:manifest:" + receipt["manifest_sha256"]
        (self.root / "receipt.json").write_text(json.dumps(receipt))
        code, _, _ = self.invoke(*self.get_args())
        self.assertEqual(code, 2)
        self.assertEqual(path.read_bytes(), b"")


if __name__ == "__main__":
    unittest.main()
