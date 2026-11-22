"""The public archive must contain exactly reviewed source and matching bytes."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest
import zipfile


DESKTOP = Path(__file__).resolve().parents[2] / "desktop"
SPEC = importlib.util.spec_from_file_location("desktop_build_test", DESKTOP / "build.py")
build = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build)


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.source = self.base / "source"
        self.source.mkdir()
        for name in build.ROOT_FILES:
            path = self.source / name
            path.write_text("Synthetic packaging fixture: " + name + "\n")
            path.chmod(0o644)
        for name in ("build.py", "verify_payload.py"):
            shutil.copyfile(DESKTOP / name, self.source / name)
        exported = {
            "runtime/LICENSE": "Synthetic runtime notice\n",
            "gui/LICENSE": "Synthetic GUI notice\n",
            "runtime/docs/loom-integration-map.json": json.dumps({
                "programme_rows": [{} for _ in range(31)],
                "toolkit_families": [{} for _ in range(15)]}),
        }
        manifest = {}
        for name, content in exported.items():
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            path.chmod(0o644)
            manifest[name] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        (self.source / "SOURCE_PROVENANCE.json").write_text(json.dumps({
            "schema": "lodestone-desktop-source-provenance/v1", "exported_files": manifest}))

    def test_build_is_reproducible_and_every_archived_byte_is_manifested(self):
        first = build.build(self.source, self.base / "download-a")
        second = build.build(self.source, self.base / "download-b")
        self.assertEqual(first["sha256"], second["sha256"])
        with zipfile.ZipFile(self.base / "download-a" / first["archive"]) as archive:
            prefix = "Lodestone-Desktop-" + build.VERSION + "/"
            manifest = json.loads(archive.read(prefix + "MANIFEST.json"))
            self.assertEqual(set(archive.namelist()), {prefix + name for name in manifest["files"]} | {prefix + "MANIFEST.json"})
            for name, expected in manifest["files"].items():
                self.assertEqual(hashlib.sha256(archive.read(prefix + name)).hexdigest(), expected)
            for name in ("build.py", "verify_payload.py"):
                self.assertEqual(archive.read(prefix + name), (DESKTOP / name).read_bytes())
            installer = archive.getinfo(prefix + "Install Lodestone.command")
            self.assertTrue(installer.external_attr >> 16 & 0o111)

    def test_incidental_private_file_is_rejected_instead_of_published(self):
        private = self.source / "gui/user_data/private-conversation.txt"
        private.parent.mkdir()
        private.write_text("Synthetic private material must not enter an archive.")
        with self.assertRaisesRegex(ValueError, "file inventory differs"):
            build.build(self.source, self.base / "download")
        self.assertFalse((self.base / "download").exists())

    def test_changed_export_is_rejected(self):
        (self.source / "runtime/LICENSE").write_text("Changed source fixture")
        with self.assertRaisesRegex(ValueError, "Source payload changed"):
            build.build(self.source, self.base / "download")

    def test_symlinked_root_distribution_file_is_rejected(self):
        path = self.source / "README.md"
        path.unlink()
        path.symlink_to(DESKTOP / "README.md")
        with self.assertRaisesRegex(ValueError, "Missing distribution file"):
            build.build(self.source, self.base / "download")

    def test_symlinked_component_directory_is_rejected(self):
        outside = self.base / "other-gui"
        (self.source / "gui").rename(outside)
        (self.source / "gui").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symbolic link"):
            build.build(self.source, self.base / "download")


if __name__ == "__main__":
    unittest.main()
