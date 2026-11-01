"""Synthetic local snapshot fixtures; no host stores or scientific inputs."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from lodestone_nurarihyon import portable


class FixtureController:
    def __init__(self, home):
        self.home = home
        self.database = home / "control.sqlite3"
        self.busy = False
        self.in_lock = False
        self.verifications = 0

    @contextmanager
    def locked(self):
        if self.busy:
            raise ValueError("state operation is active")
        self.in_lock = True
        try:
            yield
        finally:
            self.in_lock = False

    def verify(self):
        assert self.in_lock, "Snapshot verification must hold the state lock"
        self.verifications += 1
        return {"status": "INTACT"}


class PortableTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="nurarihyon-portable-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "state"
        self.home.mkdir()
        self.controller = FixtureController(self.home)
        with sqlite3.connect(self.controller.database) as db:
            db.execute("CREATE TABLE fixture (value TEXT NOT NULL)")
            db.execute("INSERT INTO fixture VALUES ('synthetic original')")
        (self.home / "policy.json").write_text('{"external_adapters": false}\n')
        for directory in ("generations/one", "inputs", "staging/failed-01"):
            (self.home / directory).mkdir(parents=True)
        (self.home / "inputs/source.txt").write_bytes(b"Explicit synthetic source\n")
        (self.home / "staging/failed-01/reason.txt").write_bytes(b"preserved failure\n")
        (self.home / "generations/one/receipt.json").write_text('{"state":"original"}\n')
        (self.home / "STOP").write_bytes(b"Host stop remains in effect\n")
        (self.home / "state.lock").touch()
        (self.home / "executor.lock").touch()
        (self.home / "worker.lock").touch()
        self.source = self.root / "checkout"
        for directory in portable.RUNTIME_DIRS:
            (self.source / directory).mkdir(parents=True)
            (self.source / directory / "__init__.py").write_text('"""Synthetic package."""\n')
        # The restored fixture command demonstrates relative source/home paths,
        # without importing or executing any original research or model.
        module = self.source / "src/lodestone_nurarihyon/__main__.py"
        module.write_text(
            "import argparse,json,sqlite3\nfrom pathlib import Path\n"
            "p=argparse.ArgumentParser();p.add_argument('--home');p.add_argument('command');a=p.parse_args()\n"
            "db=sqlite3.connect((Path(a.home)/'control.sqlite3').resolve().as_uri()+'?mode=ro',uri=True)\n"
            "print(json.dumps({'value':db.execute('SELECT value FROM fixture').fetchone()[0]}));db.close()\n")
        for name in portable.RUNTIME_FILES:
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# selected synthetic support file\n")
        (self.source / "tools/nurarihyon/run").write_text(
            "from pathlib import Path\nimport runpy,sys\n"
            "sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'src'))\n"
            "runpy.run_module('lodestone_nurarihyon',run_name='__main__')\n")
        self.package = self.root / "package"
        self.pin = self.root / "package.descriptor.sha256"
        self.restored = self.root / "restored"

    def export(self):
        return portable.export_package(self.controller, self.package, self.source)

    def snapshot(self, root):
        return {p.relative_to(root).as_posix(): (p.stat().st_size, hashlib.sha256(p.read_bytes()).hexdigest())
                for p in root.rglob("*") if p.is_file()}

    def test_roundtrip_is_self_contained_inert_and_preserves_stop_failed_work(self):
        before_state, before_code = self.snapshot(self.home), self.snapshot(self.source)
        result = self.export()
        self.assertEqual(result["status"], "EXPORTED_VERIFIED")
        self.assertEqual(self.controller.verifications, 2)
        self.assertEqual(before_state, self.snapshot(self.home))
        self.assertEqual(before_code, self.snapshot(self.source))
        self.assertEqual(self.pin.read_text().strip(), hashlib.sha256((self.package / "archive.json").read_bytes()).hexdigest())
        recovered = portable.restore_package(self.package, self.pin, self.restored)
        self.assertFalse(recovered["service_started"])
        self.assertFalse(recovered["recovered_code_executed"])
        for name in ("STOP", "inputs/source.txt", "staging/failed-01/reason.txt", "generations/one/receipt.json"):
            self.assertEqual((self.restored / "state" / name).read_bytes(), (self.home / name).read_bytes())
        self.assertFalse((self.restored / "state/state.lock").exists())
        self.assertFalse((self.restored / "state/executor.lock").exists())
        self.assertFalse((self.restored / "state/worker.lock").exists())
        # Break access to the fixture originals; only restored paths can work.
        self.source.rename(self.root / "unused-original-code")
        self.home.rename(self.root / "unused-original-state")
        process = subprocess.run(recovered["command"], cwd=self.restored, capture_output=True, text=True,
                                 env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"}, timeout=10)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout), {"value": "synthetic original"})

    def test_metadata_inventory_and_full_archive_cost_are_explicit(self):
        result = self.export()
        portable.restore_package(self.package, self.pin, self.restored)
        manifest = json.loads((self.restored / "metadata/package.json").read_bytes())
        self.assertEqual(manifest["dependencies"]["python"], ">=3.10")
        self.assertTrue(manifest["dependencies"]["stdlib_only"])
        self.assertFalse(manifest["dependencies"]["models_required"])
        self.assertEqual(result["costs"]["total_output_bytes"], sum(p.stat().st_size for p in self.package.iterdir()))
        self.assertEqual(result["external_pin_bytes"], 65)
        for name, pin in manifest["files"].items():
            raw = (self.restored / name).read_bytes()
            self.assertEqual(pin, {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})

    def test_excludes_known_secrets_caches_git_and_unrelated_source_tree(self):
        for name in (".env", ".env.local", "credentials.json", "tokens.json"):
            (self.home / name).write_text("synthetic omitted bytes")
        (self.home / "__pycache__").mkdir()
        (self.home / "__pycache__/x.pyc").write_bytes(b"cache")
        (self.source / ".git").mkdir()
        (self.source / ".git/config").write_text("not copied")
        (self.source / "personal.txt").write_text("not selected")
        result = self.export()
        portable.restore_package(self.package, self.pin, self.restored)
        for name in (".env", ".env.local", "credentials.json", "tokens.json", "__pycache__"):
            self.assertFalse((self.restored / "state" / name).exists())
        self.assertFalse((self.restored / "runtime/.git").exists())
        self.assertFalse((self.restored / "runtime/personal.txt").exists())
        self.assertIn("credentials.json", [e["path"] for e in result["exclusions"]])

    def test_policy_credential_fields_are_refused_without_changing_original(self):
        (self.home / "policy.json").write_text('{"nested":{"api_key":"synthetic-only"}}')
        before = self.snapshot(self.home)
        with self.assertRaisesRegex(ValueError, "credential field"):
            self.export()
        self.assertEqual(before, self.snapshot(self.home))
        self.assertFalse(self.package.exists())

    def test_state_or_code_symlink_is_rejected_even_if_secret_named(self):
        link = self.home / ".env"
        link.symlink_to(self.root / "not-opened")
        with self.assertRaisesRegex(ValueError, "Symlinks"):
            self.export()
        self.assertFalse(self.package.exists())

    def test_existing_package_pin_and_restore_destinations_are_preserved(self):
        self.package.mkdir()
        (self.package / "keep").write_bytes(b"existing")
        with self.assertRaisesRegex(ValueError, "Destination must be new"):
            self.export()
        self.assertEqual((self.package / "keep").read_bytes(), b"existing")
        shutil.rmtree(self.package)
        self.pin.write_bytes(b"existing pin")
        with self.assertRaisesRegex(ValueError, "Destination must be new"):
            self.export()
        self.assertEqual(self.pin.read_bytes(), b"existing pin")
        self.pin.unlink()
        self.export()
        self.restored.mkdir()
        (self.restored / "keep").write_bytes(b"existing")
        with self.assertRaisesRegex(ValueError, "Destination must be new"):
            portable.restore_package(self.package, self.pin, self.restored)
        self.assertEqual((self.restored / "keep").read_bytes(), b"existing")

    def test_external_wrong_pin_and_payload_tamper_stop_before_restore(self):
        self.export()
        correct = self.pin.read_bytes()
        self.pin.write_text("0" * 64 + "\n")
        with self.assertRaisesRegex(ValueError, "Descriptor SHA-256"):
            portable.restore_package(self.package, self.pin, self.restored)
        self.assertFalse(self.restored.exists())
        self.pin.write_bytes(correct)
        with (self.package / "archive.tar.gz").open("ab") as stream:
            stream.write(b"tamper")
        with self.assertRaises(ValueError):
            portable.restore_package(self.package, self.pin, self.restored)
        self.assertFalse(self.restored.exists())

    def test_pin_inside_package_is_not_independent(self):
        self.export()
        insider = self.package / "pin"
        insider.write_bytes(self.pin.read_bytes())
        with self.assertRaisesRegex(ValueError, "pin must be external"):
            portable.restore_package(self.package, insider, self.restored)

    def test_active_state_operation_refuses_without_touching_state(self):
        self.controller.busy = True
        before = self.snapshot(self.home)
        with self.assertRaisesRegex(ValueError, "state operation is active"):
            self.export()
        self.assertEqual(before, self.snapshot(self.home))
        self.assertFalse(self.package.exists())

    def test_code_drift_during_capture_is_detected_and_stage_retained(self):
        original = portable._pin
        changed = False
        victim = self.source / "src/lodestone_nurarihyon/__main__.py"
        def pin(path, end, **kw):
            nonlocal changed
            result = original(path, end, **kw)
            if path == victim and kw.get("copy_to") is not None and not changed:
                changed = True
                victim.write_text(victim.read_text() + "# changed during snapshot\n")
            return result
        with mock.patch.object(portable, "_pin", side_effect=pin):
            with self.assertRaisesRegex(ValueError, "source changed"):
                self.export()
        self.assertFalse(self.package.exists())
        self.assertTrue(list(self.root.glob(".package.capture-*")))

    def test_state_drift_during_capture_is_detected(self):
        original = portable._snapshot_db
        def backup(source, target, end):
            original(source, target, end)
            (self.home / "inputs/new.txt").write_bytes(b"new concurrent file")
        with mock.patch.object(portable, "_snapshot_db", side_effect=backup):
            with self.assertRaisesRegex(ValueError, "state inventory changed"):
                self.export()

    def test_missing_runtime_dependency_refuses_before_output(self):
        (self.source / "tools/pando/run").unlink()
        with self.assertRaisesRegex(ValueError, "regular file"):
            self.export()
        self.assertFalse(self.package.exists())

    def test_input_limits_and_wall_limit_refuse(self):
        with mock.patch.object(portable, "MAX_BYTES", 1024):
            with self.assertRaisesRegex(ValueError, "byte limit"):
                self.export()
        with mock.patch.object(portable, "MAX_SECONDS", 0):
            with self.assertRaisesRegex(ValueError, "wall limit"):
                self.export()
        self.assertFalse(self.package.exists())

    def test_generation_wal_is_not_copied_as_an_ordinary_file(self):
        (self.home / "generations/one/pando.sqlite3-wal").write_bytes(b"uncheckpointed")
        with self.assertRaisesRegex(ValueError, "checkpointed"):
            self.export()

    def test_control_wal_uses_consistent_backup_without_copying_companions(self):
        connection = sqlite3.connect(self.controller.database)
        self.addCleanup(connection.close)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("INSERT INTO fixture VALUES ('committed WAL value')")
        connection.commit()
        self.assertTrue(Path(str(self.controller.database) + "-wal").exists())
        self.export()
        portable.restore_package(self.package, self.pin, self.restored)
        with sqlite3.connect(self.restored / "state/control.sqlite3") as db:
            self.assertEqual(db.execute("SELECT count(*) FROM fixture").fetchone()[0], 2)
        self.assertFalse((self.restored / "state/control.sqlite3-wal").exists())
        self.assertFalse((self.restored / "state/control.sqlite3-shm").exists())

    def test_parent_symlink_substitution_cannot_read_outside_file(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "payload").write_bytes(b"must not be read")
        parent = self.root / "parent"
        parent.mkdir()
        (parent / "payload").write_bytes(b"original")
        parent.rename(self.root / "old-parent")
        parent.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            portable._pin(parent / "payload", portable.time.monotonic() + 5)

    def test_semantic_manifest_mismatch_preserves_incomplete_restore(self):
        self.export()
        portable.restore_package(self.package, self.pin, self.restored)
        metadata = self.restored / "metadata/package.json"
        manifest = json.loads(metadata.read_bytes())
        manifest["files"]["state/STOP"]["sha256"] = "0" * 64
        metadata.write_text(json.dumps(manifest))
        wrong_package = self.root / "wrong-package"
        result = portable.archive_pack.pack({name: self.restored / name for name in ("runtime", "state", "metadata")},
                                            wrong_package, epoch="synthetic", authority_ref="synthetic malformed fixture")
        wrong_pin = self.root / "wrong-pin"
        wrong_pin.write_text(result["descriptor_sha256"] + "\n")
        incomplete = self.root / "incomplete"
        with self.assertRaisesRegex(ValueError, "differs from portable file manifest"):
            portable.restore_package(wrong_package, wrong_pin, incomplete)
        self.assertTrue(incomplete.exists())

    def test_combined_code_and_state_budget_refuses(self):
        (self.source / "src/lodestone_pando/large.bin").write_bytes(b"fixture" * 1500)
        # Each individual tree is below the limit, their combined bytes are not.
        with mock.patch.object(portable, "MAX_BYTES", 16000):
            with self.assertRaisesRegex(ValueError, "Combined code/state"):
                self.export()
        self.assertFalse(self.package.exists())

    def test_overlap_and_symlink_parent_refused(self):
        with self.assertRaisesRegex(ValueError, "overlap"):
            portable.export_package(self.controller, self.home / "bad", self.source)
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "Symlink path"):
            portable.export_package(self.controller, alias / "bad", self.source)

    @unittest.skipUnless((ROOT / "src/lodestone_nurarihyon/plans.py").is_file()
                         and (ROOT / "tools/nurarihyon/run").is_file(),
                         "Concurrent controller/CLI implementation has not arrived")
    def test_real_controller_and_restored_cli_in_separate_directory(self):
        from lodestone_nurarihyon.controller import Controller
        home = self.root / "integration-home"
        controller = Controller.initialize(home)
        controller.stop()
        before = {k: v for k, v in self.snapshot(home).items() if k not in portable.TRANSIENT}
        exported = portable.export_package(controller, self.package, ROOT)
        after = {k: v for k, v in self.snapshot(home).items() if k not in portable.TRANSIENT}
        self.assertEqual(before, after)
        recovered = portable.restore_package(self.package, Path(exported["descriptor"]), self.restored)
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        for operation in ("status", "verify"):
            command = [*recovered["command"][:-1], operation]
            process = subprocess.run(command, cwd=self.restored, env=env, capture_output=True,
                                     text=True, timeout=20)
            self.assertEqual(process.returncode, 0, process.stderr)
            result = json.loads(process.stdout)
            if operation == "status":
                self.assertTrue(result["stopped"])
            else:
                self.assertEqual(result["status"], "INTACT")


if __name__ == "__main__":
    unittest.main()
