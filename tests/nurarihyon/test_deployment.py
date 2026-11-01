"""Synthetic dependency-pin checks; no host binaries, services or credentials."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from lodestone_nurarihyon import deployment


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="nurarihyon-dependency-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "runtime"
        self.launcher = self.root / deployment.LAUNCHER
        self.launcher.parent.mkdir(parents=True)
        self.launcher.write_bytes(b"fixture launcher v1\n")
        self.prefix = self.base / "python"
        self.prefix.mkdir()
        self.binary = self.prefix / "python3.12"
        self.binary.write_bytes(b"fixture interpreter v1\n")
        self.command = self.base / "python3"
        self.command.symlink_to(self.binary)
        self.system = SimpleNamespace(executable=str(self.command), prefix=str(self.prefix),
                                      version="fixture Python 3.12.7", platform="linux")
        for target, value in (("ROOT", self.root), ("sys", self.system)):
            mock = patch.object(deployment, target, value)
            mock.start(); self.addCleanup(mock.stop)

    def test_deterministic_canonical_hash_and_legitimate_python_alias(self):
        record = deployment.environment_record()
        canonical = json.dumps(record, sort_keys=True, ensure_ascii=False,
                               separators=(",", ":"), allow_nan=False).encode()
        self.assertEqual(deployment.environment_identity(), hashlib.sha256(canonical).hexdigest())
        self.assertEqual(deployment.environment_identity(), deployment.environment_identity())
        self.assertEqual(record["python"]["command_path"], str(self.command))
        self.assertEqual(record["python"]["resolved_path"], str(self.binary))
        self.assertEqual(record["python"]["framework"], {})

    def test_critical_binary_launcher_or_version_change_invalidates_pin(self):
        for path in (self.binary, self.launcher):
            with self.subTest(path=path):
                old = deployment.environment_identity()
                path.write_bytes(path.read_bytes() + b"changed")
                self.assertNotEqual(old, deployment.environment_identity())
        old = deployment.environment_identity()
        self.system.version += " revised"
        self.assertNotEqual(old, deployment.environment_identity())

    def test_launcher_identity_survives_project_relocation(self):
        before = deployment.environment_identity()
        relocated = self.base / "relocated-runtime"
        shutil.copytree(self.root, relocated)
        with patch.object(deployment, "ROOT", relocated):
            self.assertEqual(before, deployment.environment_identity())
        self.assertEqual(set(deployment.environment_record()["launcher"]), {deployment.LAUNCHER})

    def test_mac_framework_binaries_are_pinned_when_present(self):
        self.system.platform = "darwin"
        framework = [self.prefix / "Python", self.prefix / "Resources/Python.app/Contents/MacOS/Python"]
        for path in framework:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture framework\n")
        self.assertEqual(len(deployment.environment_record()["python"]["framework"]), 2)
        for path in framework:
            old = deployment.environment_identity()
            path.write_bytes(path.read_bytes() + b"changed")
            self.assertNotEqual(old, deployment.environment_identity())
        old = deployment.environment_identity()
        framework[0].unlink()
        self.assertNotEqual(old, deployment.environment_identity())

    def test_missing_required_files_and_dangling_optional_alias_refuse(self):
        self.launcher.unlink()
        with self.assertRaises(OSError):
            deployment.environment_record()
        self.launcher.write_bytes(b"fixture launcher restored")
        self.binary.unlink()
        with self.assertRaises(OSError):
            deployment.environment_record()
        self.binary.write_bytes(b"fixture interpreter restored")
        self.system.platform = "darwin"
        (self.prefix / "Python").symlink_to(self.prefix / "missing-framework")
        with self.assertRaises(OSError):
            deployment.environment_record()

    def test_symlink_launcher_and_parent_refuse(self):
        self.launcher.unlink()
        self.launcher.symlink_to(self.binary)
        with self.assertRaises(ValueError):
            deployment.environment_record()
        self.launcher.unlink()
        self.launcher.parent.rmdir()
        alternate = self.base / "alternate"
        alternate.mkdir()
        (alternate / "run").write_bytes(b"fixture launcher alternate")
        self.launcher.parent.symlink_to(alternate)
        with self.assertRaises(ValueError):
            deployment.environment_record()

    def test_nonregular_and_bounded_dependencies_refuse(self):
        self.launcher.unlink()
        self.launcher.mkdir()
        with self.assertRaises(ValueError):
            deployment.environment_record()
        self.launcher.rmdir()
        self.launcher.write_bytes(b"a" * 129)
        with patch.object(deployment, "MAX_FILE_BYTES", 128), self.assertRaises(ValueError):
            deployment.environment_record()

    def test_environment_is_not_enumerated_or_secret_captured(self):
        class ForbiddenEnvironment(dict):
            def __getitem__(self, key):
                raise AssertionError("Environment access forbidden")
            def __iter__(self):
                raise AssertionError("Environment enumeration forbidden")
            def get(self, *args):
                raise AssertionError("Environment access forbidden")
        with patch.object(os, "environ", ForbiddenEnvironment()):
            record = deployment.environment_record()
        self.assertEqual(set(record), {"schema", "scope", "python", "launcher"})
        self.assertEqual(set(record["python"]), {"version", "command_path", "resolved_path",
                         "prefix", "bytes", "sha256", "framework"})


if __name__ == "__main__":
    unittest.main()
