"""Exact staged-file reduction, source preservation, and actual organ dispatch."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from lodestone_machinery.__main__ import main
from lodestone_machinery.crystals import CrystalError, CrystalStore
from lodestone_machinery.crystals_cli import stage, consolidate


class CrystalCLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = self.root / "crystals.sqlite"
        self.stage = self.root / "staging"
        self.a, self.b = self.root / "a.txt", self.root / "b.txt"
        self.raw = b"Host approval remains scoped.\r\n" * 500
        self.a.write_bytes(self.raw)
        self.b.write_bytes(self.raw)

    def prepare(self):
        stage([self.a, self.b], self.stage, epoch="unit", authority_ref="unit test")

    def invoke(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_consume_only_staged_copies_and_restore_without_originals(self):
        self.prepare()
        manifest = json.loads((self.stage / "stage.json").read_text())
        result = consolidate(self.stage, self.database, consume=True)
        self.assertEqual(result["staged_copies_removed"], 2)
        self.assertFalse(self.stage.exists())
        self.assertEqual(self.a.read_bytes(), self.raw)
        self.assertEqual(self.b.read_bytes(), self.raw)
        # Recovery reads the store, even after the test moves its original inputs.
        self.a.rename(self.root / "a.offline")
        self.b.rename(self.root / "b.offline")
        with CrystalStore(self.database) as store:
            for record in manifest["files"]:
                self.assertEqual(store.get(record["source_id"]), self.raw)
        self.assertEqual(len(list(self.root.glob("crystals.sqlite*"))), 1)

    def test_copy_default_retains_all_stage_files(self):
        self.prepare()
        before = {p.name: p.read_bytes() for p in self.stage.iterdir()}
        consolidate(self.stage, self.database)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.stage.iterdir()})

    def test_tamper_extra_file_symlink_and_existing_stage_rejected(self):
        self.prepare()
        with self.assertRaises(FileExistsError):
            self.prepare()
        staged = self.stage / "source-000000.bin"
        staged.write_bytes(b"changed")
        with self.assertRaisesRegex(CrystalError, "changed"):
            consolidate(self.stage, self.database, consume=True)
        self.assertFalse(self.database.exists())
        staged.write_bytes(self.raw)
        extra = self.stage / "extra"
        extra.write_text("unowned")
        with self.assertRaisesRegex(CrystalError, "Unexpected"):
            consolidate(self.stage, self.database, consume=True)
        extra.unlink()
        staged.unlink()
        staged.symlink_to(self.a)
        with self.assertRaisesRegex(CrystalError, "symlink"):
            consolidate(self.stage, self.database, consume=True)
        self.assertEqual(self.a.read_bytes(), self.raw)

    def test_traversal_manifest_and_store_inside_stage_rejected(self):
        self.prepare()
        with self.assertRaisesRegex(CrystalError, "outside"):
            consolidate(self.stage, self.stage / "db", consume=True)
        path = self.stage / "stage.json"
        manifest = json.loads(path.read_text())
        manifest["files"][0]["file"] = "../a.txt"
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(CrystalError, "direct filenames"):
            consolidate(self.stage, self.database, consume=True)
        self.assertEqual(self.a.read_bytes(), self.raw)

    def test_dispatch_both_organs_and_output_budget(self):
        code, out, err = self.invoke("origami", "crystal", "put", "--store", str(self.database),
                                    "--source-id", "approval", "--file", str(self.a))
        self.assertEqual(code, 0, err)
        code, out, err = self.invoke("yggdrasil", "crystal", "packet", "--store", str(self.database),
                                    "--query", "approval", "--max-bytes", "1200")
        self.assertEqual(code, 0, err)
        self.assertLessEqual(len(out.encode()), 1200)
        self.assertEqual(json.loads(out)["authority"], "NONE")
        output = self.root / "restored"
        code, _, err = self.invoke("yggdrasil", "crystal", "get", "--store", str(self.database),
                                   "--source-id", "approval", "--output", str(output))
        self.assertEqual(code, 0, err)
        self.assertEqual(output.read_bytes(), self.raw)
        code, _, _ = self.invoke("origami", "crystal", "get", "--store", str(self.database),
                                 "--source-id", "approval", "--output", str(output))
        self.assertEqual(code, 2)
        self.assertEqual(output.read_bytes(), self.raw)

    def test_read_command_does_not_create_store(self):
        code, _, _ = self.invoke("origami", "crystal", "inspect", "--store", str(self.database))
        self.assertEqual(code, 2)
        self.assertFalse(self.database.exists())

    def test_explicit_policy_selects_exact_codec_through_origami(self):
        from lodestone_machinery.crystal_policy import initialize, make_artifact, store_artifact
        policies = self.root / "policies.sqlite"
        artifact = make_artifact(initialize(7), "identity", {"kind": "SYNTHETIC_TEST_ONLY"})
        with CrystalStore(policies, index_mode="scan") as store:
            store_artifact(store, "test-policy", artifact)
        code, out, err = self.invoke("origami", "crystal", "put", "--store", str(self.database),
            "--source-id", "approval", "--file", str(self.a), "--index-mode", "scan",
            "--policy-store", str(policies), "--policy-id", "test-policy")
        self.assertEqual(code, 0, err)
        with CrystalStore(self.database) as store:
            self.assertEqual(store.get("approval"), self.raw)
            self.assertEqual(store.inspect()["lexical_mode"], "scan")
        code, _, err = self.invoke("origami", "crystal", "put", "--store", str(self.database),
            "--source-id", "conflict", "--file", str(self.a), "--strategy", "zlib",
            "--policy-store", str(policies), "--policy-id", "test-policy")
        self.assertEqual(code, 2)
        self.assertIn("without a manual strategy", err)

    def test_experimental_router_preserves_required_source_and_output_bound(self):
        from lodestone_machinery.crystal_routing import initialize, make_artifact, store_artifact
        policies = self.root / "router.sqlite"
        with CrystalStore(policies) as store:
            store_artifact(store, "synthetic-router", make_artifact(initialize(), "identity", {"kind": "TEST"}))
        approval = b"Host permits this test. Unrelated actions remain pending."
        with CrystalStore(self.database) as store:
            store.put("required", approval, {})
            for index, raw in enumerate([b"silver casings source", b"bronze gates", b"round stones"]):
                store.put(f"candidate{index}", raw, {})
        options = ["--store", str(self.database), "--query", "silver casings",
                   "--policy-store", str(policies), "--policy-id", "synthetic-router",
                   "--required-source-id", "required"]
        for index in range(3):
            options += ["--candidate-source-id", f"candidate{index}"]
        code, out, err = self.invoke("origami", "crystal", "route", *options, "--max-bytes", "4096")
        self.assertEqual(code, 0, err)
        self.assertLessEqual(len(out.encode()), 4096)
        result = json.loads(out)
        self.assertEqual(result["packet"]["required_sources"][0]["text"].encode(), approval)
        self.assertEqual(result["routing"]["authority"], "NONE")
        self.assertEqual(len(result["packet"]["references"]), 1)
        code, _, _ = self.invoke("yggdrasil", "crystal", "route", *options, "--max-bytes", "100")
        self.assertEqual(code, 2)

    def test_required_approval_is_full_or_packet_fails(self):
        approval = "Host permits engineering only. Merge remains pending.\r\n"
        with CrystalStore(self.database) as store:
            store.put("approval", approval.encode(), {})
            store.put("distraction", b"casings approval " * 600, {})
            packet = store.packet("casings", max_bytes=1200, required_source_ids=["approval"])
            self.assertEqual(packet["required_sources"][0]["text"], approval)
            with self.assertRaisesRegex(CrystalError, "required sources"):
                store.packet("casings", max_bytes=700, required_source_ids=["approval"])


if __name__ == "__main__":
    unittest.main()
