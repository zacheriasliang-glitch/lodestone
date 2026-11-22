import importlib.util
import fcntl
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

DESKTOP = Path(__file__).resolve().parents[2] / "desktop"
sys.path.insert(0, str(DESKTOP))
import install


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "user data"
        self.app = self.base / "Applications/Test Preview.app"
        self.payload = self.base / "download"
        self.payload.mkdir()
        names = ("install.py", "run_loom.py", "bootstrap_learning.py", "dependencies.py",
                 "runtime/lodestone.py", "runtime/src/lodestone_machinery/loom_runtime.py",
                 "gui/basilisk/main_app.py")
        for name in names:
            p = self.payload / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("# inert installation fixture\n")
        self.manifest("0.4.0-preview.1")

    def tearDown(self):
        self.tmp.cleanup()

    def manifest(self, version):
        files = {p.relative_to(self.payload).as_posix(): install.digest(p.read_bytes())
                 for p in self.payload.rglob("*") if p.is_file() and p.name != "MANIFEST.json"}
        install.save(self.payload / "MANIFEST.json", {
            "schema": "lodestone-desktop-payload/v1", "version": version, "files": files})

    def provision(self, root, payload, cache=None):
        values = {}
        for name in ("gui_python", "worker_python", "assets_dir", "ollama_binary", "ollama_models"):
            p = root / "deps" / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.touch()
            values[name] = str(p)
        values["model_digest"] = "fixture-model-digest"
        return values

    def bootstrap(self, root, runtime, python):
        p = root / "learning/ACTIVE.json"
        if not p.exists():
            install.save(p, {"schema": "fixture-learning", "retained": "original"})
        return {"config": str(p)}

    def setup_install(self):
        return install.install(self.root, self.payload, app=self.app,
                               provision=self.provision, bootstrap=self.bootstrap)

    def test_fresh_install_and_idempotence_preserve_configuration(self):
        first = self.setup_install()
        conf = self.root / "Loom/data/profiles.yml"
        conf.write_text("user's edited profile\n")
        again = self.setup_install()
        self.assertEqual(again["status"], "ALREADY_INSTALLED")
        self.assertEqual(conf.read_text(), "user's edited profile\n")
        self.assertEqual(install.doctor(self.root, verify_dependencies=lambda *args: {"fixture": True})["status"], "READY")
        self.assertEqual(first["release_id"], again["release_id"])

    def test_upgrade_then_rollback_preserves_new_records(self):
        first = self.setup_install()
        before = (self.root / "ACTIVE.json").read_bytes()
        conf = self.root / "Loom/runtime.json"
        value = install.read(conf)
        value["max_new_tokens"] = 200
        install.save(conf, value)
        self.manifest("0.4.0-preview.2")
        second = self.setup_install()
        self.assertNotEqual(first["release_id"], second["release_id"])
        self.assertEqual(install.read(conf)["max_new_tokens"], 200)
        record = self.root / "learning/user-added-evidence.json"
        record.write_text('{"actual":"new evidence"}')
        install.rollback(self.root)
        self.assertEqual((self.root / "ACTIVE.json").read_bytes(), before)
        self.assertEqual(record.read_text(), '{"actual":"new evidence"}')
        self.assertTrue(Path(second["release"]).exists())

    def test_dependency_failure_does_not_switch_active_install(self):
        self.setup_install()
        before = (self.root / "ACTIVE.json").read_bytes()
        self.manifest("0.4.0-preview.2")
        def broken(*args, **kwargs):
            raise ValueError("download interrupted")
        with self.assertRaisesRegex(ValueError, "download interrupted"):
            install.install(self.root, self.payload, app=self.app, provision=broken, bootstrap=self.bootstrap)
        self.assertEqual((self.root / "ACTIVE.json").read_bytes(), before)
        self.assertEqual(self.setup_install()["status"], "INSTALLED")

    def test_activation_failure_restores_active_and_executable(self):
        self.setup_install()
        before = install.snapshot([self.root / "ACTIVE.json", self.root / "Loom/runtime.json",
                                   self.app / "Contents/MacOS/Lodestone"])
        self.manifest("0.4.0-preview.2")
        actual = install.atomic
        failed = False
        def fail_once(path, data, mode=0o600):
            nonlocal failed
            if Path(path) == self.root / "ACTIVE.json" and not failed:
                failed = True
                raise OSError("simulated interrupted activation")
            return actual(path, data, mode)
        with patch.object(install, "atomic", side_effect=fail_once):
            with self.assertRaisesRegex(OSError, "interrupted activation"):
                self.setup_install()
        self.assertEqual(install.snapshot(map(Path, before)), before)

    def test_tampered_or_extra_payload_is_rejected(self):
        (self.payload / "install.py").write_text("changed")
        with self.assertRaisesRegex(ValueError, "integrity"):
            self.setup_install()
        self.manifest("0.4.0-preview.1")
        (self.payload / "extra.py").write_text("unexpected")
        with self.assertRaisesRegex(ValueError, "unlisted"):
            self.setup_install()

    def test_symlink_payload_is_rejected_before_provisioning(self):
        target = self.payload / "install.py"
        content = target.read_bytes()
        outside = self.base / "outside.py"
        outside.write_bytes(content)
        target.unlink()
        target.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "symbolic"):
            self.setup_install()
        self.assertFalse(self.root.exists())

    def test_removal_and_reinstallation_keep_records(self):
        self.setup_install()
        record = self.root / "learning/retained.txt"
        record.write_text("keep this")
        install.uninstall(self.root)
        self.assertFalse(self.app.exists())
        self.assertFalse((self.root / "ACTIVE.json").exists())
        self.assertEqual(record.read_text(), "keep this")
        self.setup_install()
        self.assertEqual(record.read_text(), "keep this")
        self.assertEqual(install.doctor(self.root, verify_dependencies=lambda *args: {"fixture": True})["status"], "READY")

    def test_first_install_rollback_leaves_records(self):
        self.setup_install()
        before = (self.root / "learning/ACTIVE.json").read_bytes()
        install.rollback(self.root)
        self.assertFalse((self.root / "ACTIVE.json").exists())
        self.assertEqual((self.root / "learning/ACTIVE.json").read_bytes(), before)

    def test_unrelated_nonempty_directory_is_not_adopted(self):
        self.root.mkdir()
        note = self.root / "personal.txt"
        note.write_text("private")
        with self.assertRaisesRegex(ValueError, "empty directory"):
            self.setup_install()
        self.assertFalse((self.root / "releases").exists())
        self.assertEqual(note.read_text(), "private")

    def test_upgrade_cannot_replace_a_different_app(self):
        self.setup_install()
        self.manifest("0.4.0-preview.2")
        other = self.base / "Other.app"
        with self.assertRaisesRegex(ValueError, "application path"):
            install.install(self.root, self.payload, app=other,
                            provision=self.provision, bootstrap=self.bootstrap)
        self.assertFalse(other.exists())

    def test_live_application_prevents_upgrade_removal_and_rollback(self):
        self.setup_install()
        with (self.root / ".lifecycle.lock").open("a+") as lease:
            fcntl.flock(lease, fcntl.LOCK_SH | fcntl.LOCK_NB)
            for action in (self.setup_install, lambda: install.uninstall(self.root),
                           lambda: install.rollback(self.root)):
                with self.assertRaisesRegex(ValueError, "Close this Lodestone"):
                    action()

    def test_changed_application_parent_cannot_delete_external_files(self):
        self.setup_install()
        original = self.app / "Contents"
        moved = self.base / "saved-contents"
        original.rename(moved)
        original.symlink_to(moved)
        before = (moved / "Info.plist").read_bytes()
        with self.assertRaisesRegex(ValueError, "symbolic"):
            install.uninstall(self.root)
        self.assertEqual((moved / "Info.plist").read_bytes(), before)

    def test_same_version_reinstall_repairs_missing_application(self):
        self.setup_install()
        executable = self.app / "Contents/MacOS/Lodestone"
        executable.unlink()
        repaired = self.setup_install()
        self.assertEqual(repaired["status"], "INSTALLED")
        self.assertTrue(executable.is_file())

    def test_next_launch_recovers_interrupted_activation_exactly(self):
        first = self.setup_install()
        before = install.snapshot(install.managed_paths(self.root, self.app))
        transaction = self.root / "transactions/zz-interrupted.json"
        install.save(transaction, {"schema": "lodestone-desktop-transaction/v1",
            "root": str(self.root), "app": str(self.app), "before": before,
            "state": "PREPARED", "release_id": first["release_id"]})
        (self.root / "Loom/runtime.json").write_text('{"partial":"new release"}')
        (self.root / "learning/retained-failure.json").write_text('{"failure":"retained"}')
        self.assertEqual(install.recover(self.root), [str(transaction)])
        self.assertEqual(install.snapshot(install.managed_paths(self.root, self.app)), before)
        self.assertTrue((self.root / "learning/retained-failure.json").is_file())
        self.assertEqual(install.read(transaction)["state"], "RECOVERED_INTERRUPTED_ACTIVATION")


if __name__ == "__main__":
    unittest.main()
