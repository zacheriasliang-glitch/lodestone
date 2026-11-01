"""Captured worker directory traversal checks using owned temporary fixtures.

No production, profiling, network access or external corpus is involved.
"""
import errno
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from lodestone_machinery.bezoar_capture import worker_copy_store
from lodestone_machinery.records import RecordStore
from lodestone_nurarihyon.inspection import ReadOnlyRecords


class CapturedCopyDirectoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="captured-copy-directory-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.original = RecordStore(self.root / "records")
        self.store = worker_copy_store(self.original)

    def test_existing_directory_avoids_creation_and_preserves_writer(self):
        expected = self.original._directory("organs/bezoar/records", create=True)
        before = expected.stat()
        with patch.object(Path, "mkdir", side_effect=AssertionError("Existing directory was recreated")):
            self.assertEqual(self.store._directory("organs/bezoar/records", create=True), expected)
        after = expected.stat()
        self.assertEqual((before.st_dev, before.st_ino), (after.st_dev, after.st_ino))
        self.assertIs(type(self.original), RecordStore)

    def test_missing_read_path_stays_missing_and_create_builds_directories(self):
        expected = self.original.workspace / "organs/bezoar/records"
        self.assertEqual(self.store._directory("organs/bezoar/records"), expected)
        self.assertFalse((self.original.workspace / "organs/bezoar").exists())
        self.assertEqual(self.store._directory("organs/bezoar/records", create=True), expected)
        self.assertTrue(expected.is_dir())

    def test_regular_file_errors_preserve_create_and_read_contracts(self):
        path = self.original.workspace / "organs/file"
        path.write_bytes(b"owned fixture")
        for store in (self.original, self.store):
            with self.subTest(type=type(store).__name__, create=False):
                with self.assertRaisesRegex(ValueError, "not a directory"):
                    store._directory("organs/file")
            with self.subTest(type=type(store).__name__, create=True):
                with self.assertRaises(FileExistsError) as failure:
                    store._directory("organs/file", create=True)
                self.assertEqual(failure.exception.errno, errno.EEXIST)

    def test_valid_and_dangling_symlinks_are_rejected_without_creation(self):
        target = self.root / "target"
        target.mkdir()
        for name, destination in (("valid", target), ("dangling", self.root / "absent")):
            link = self.original.workspace / "organs" / name
            link.symlink_to(destination, target_is_directory=True)
            for create in (False, True):
                with self.subTest(name=name, create=create):
                    with self.assertRaisesRegex(ValueError, "symlink"):
                        self.store._directory("organs/" + name, create=create)

    def test_creation_race_to_directory_is_accepted_after_recheck(self):
        target = self.original.workspace / "organs/race"
        original_mkdir = Path.mkdir
        def create_between_check_and_mkdir(path, *args, **kwargs):
            if path == target:
                original_mkdir(path)
            return original_mkdir(path, *args, **kwargs)
        with patch.object(Path, "mkdir", create_between_check_and_mkdir):
            self.assertEqual(self.store._directory("organs/race", create=True), target)
        self.assertTrue(target.is_dir())

    def test_creation_race_to_symlink_is_rejected_after_recheck(self):
        target = self.original.workspace / "organs/race"
        outside = self.root / "other-owned-fixture"
        outside.mkdir()
        original_mkdir = Path.mkdir
        def replace_between_check_and_mkdir(path, *args, **kwargs):
            if path == target:
                path.symlink_to(outside, target_is_directory=True)
            return original_mkdir(path, *args, **kwargs)
        with patch.object(Path, "mkdir", replace_between_check_and_mkdir):
            with self.assertRaisesRegex(ValueError, "symlink"):
                self.store._directory("organs/race", create=True)

    def test_replacement_on_later_call_is_not_cached(self):
        target = self.store._directory("organs/replace", create=True)
        target.rmdir()
        target.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.store._directory("organs/replace", create=True)

    def test_permission_errors_are_propagated(self):
        original_lstat = Path.lstat
        denied = self.original.workspace / "organs/denied"
        def deny(path, *args, **kwargs):
            if path == denied:
                raise PermissionError(errno.EACCES, "owned fixture denial", str(path))
            return original_lstat(path, *args, **kwargs)
        with patch.object(Path, "lstat", deny):
            with self.assertRaises(PermissionError):
                self.store._directory("organs/denied", create=True)

    def test_custom_and_readonly_stores_are_not_replaced(self):
        class CustomStore(RecordStore):
            pass
        for store in (ReadOnlyRecords(self.original.workspace), CustomStore(self.root / "custom")):
            with self.subTest(type=type(store).__name__):
                with self.assertRaisesRegex(ValueError, "ordinary RecordStore writer"):
                    worker_copy_store(store)
        self.original.publish = lambda *args, **kwargs: self.fail("Custom publisher was bypassed")
        with self.assertRaisesRegex(ValueError, "ordinary RecordStore writer"):
            worker_copy_store(self.original)

    def test_original_publisher_reader_links_and_conflicts_remain_effective(self):
        child = self.store.publish("bezoar", "fixture", {"value": "retained"})
        parent = self.store.publish("bezoar", "fixture", {"value": "parent"}, links=[child])
        self.assertEqual(self.original.read(child), self.store.read(child))
        self.assertEqual(self.original.verify_links([parent])["records_checked"], 2)
        self.assertEqual(self.store.publish("bezoar", "fixture", {"value": "retained"}), child)
        with self.assertRaises((OSError, ValueError)):
            self.store.publish("bezoar", "fixture", {}, links=["bezoar:" + "0" * 64])
        self.original.path(child).write_bytes(b"changed owned fixture")
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            self.store.publish("bezoar", "fixture", {"value": "retained"})


if __name__ == "__main__":
    unittest.main()
