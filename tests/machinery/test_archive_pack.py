"""Production archive writing on small fixtures; no research workloads."""
import os
from pathlib import Path
import stat
import tarfile
import tempfile
import unittest
from unittest import mock

from lodestone_machinery import archive_format as fmt
from lodestone_machinery import archive_pack as packer
from lodestone_machinery import archive_restore as reader


class ArchivePackTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.source = self.base / "source"
        self.source.mkdir()
        self.destination = self.base / "archive"
        self.roots = {"project": self.source}
        self.data = b"exact source bytes\r\n\x00\xff" * 13
        (self.source / "one.bin").write_bytes(self.data)

    def pack(self, destination=None, **kwargs):
        return packer.pack(self.roots, destination or self.destination,
                           epoch="fixture-epoch", authority_ref="fixture authorization", **kwargs)

    def test_plan_is_a_metadata_census_and_declares_exclusions(self):
        (self.source / "sub").mkdir()
        (self.source / "sub" / "empty").write_bytes(b"")
        (self.source / ".git").mkdir()
        (self.source / ".git" / "private").write_bytes(b"not archived")
        (self.source / "link").symlink_to("missing-target")
        with mock.patch.object(packer, "_hash_source", side_effect=AssertionError("file body read")):
            result = packer.plan(self.roots)
        self.assertEqual(result["counts"], {"files": 2, "directories": 2, "symlinks": 1,
                                          "logical_file_bytes": len(self.data)})
        self.assertEqual(result["excluded"], [{"path": "project/.git", "reason": "excluded name: .git"}])
        self.assertFalse(result["content_hashes_checked"])
        self.assertFalse(self.destination.exists())

    def test_pack_deduplicates_and_preserves_original_bytes_and_metadata(self):
        other = self.source / "other"
        other.mkdir()
        (other / "duplicate").write_bytes(self.data)
        (other / "empty").write_bytes(b"")
        (self.source / "link").symlink_to("/outside/not-followed")
        (self.source / ".git").mkdir()
        (self.source / ".git" / "excluded").write_bytes(b"excluded")
        os.chmod(self.source / "one.bin", 0o640)
        os.utime(self.source / "one.bin", ns=(1_700_000_000_000_000_000, 1_700_000_000_123_456_789))
        before = {str(p.relative_to(self.source)): (p.lstat().st_mode, p.lstat().st_mtime_ns,
                                                   os.readlink(p) if p.is_symlink() else p.read_bytes() if p.is_file() else None)
                  for p in [self.source, *self.source.rglob("*")]}
        result = self.pack()
        descriptor, manifest = fmt.load_archive(self.destination)
        self.assertEqual(result["descriptor"], descriptor)
        self.assertEqual(descriptor["counts"]["files"], 3)
        self.assertEqual(descriptor["counts"]["unique_blobs"], 2)
        self.assertEqual(descriptor["counts"]["logical_file_bytes"], 2 * len(self.data))
        self.assertEqual(descriptor["counts"]["unique_file_bytes"], len(self.data))
        with tarfile.open(self.destination / "archive.tar.gz") as archive:
            members = archive.getmembers()
            self.assertEqual(members[0].name, "manifest.json")
            self.assertEqual(len(members), 3)
            self.assertTrue(all(item.isfile() for item in members))
            self.assertEqual(archive.extractfile(members[0]).read(), fmt.canonical(manifest))
            self.assertEqual(archive.extractfile("blobs/" + fmt.digest(self.data)).read(), self.data)
        after = {str(p.relative_to(self.source)): (p.lstat().st_mode, p.lstat().st_mtime_ns,
                                                  os.readlink(p) if p.is_symlink() else p.read_bytes() if p.is_file() else None)
                 for p in [self.source, *self.source.rglob("*")]}
        self.assertEqual(before, after)
        self.assertEqual(result["costs"]["total_output_bytes"], sum(p.stat().st_size for p in self.destination.iterdir()))
        self.assertTrue(result["source_inventory_unchanged"])
        self.assertTrue(result["originals_retained"])

    def test_identical_manifest_and_inputs_make_identical_archive_bytes(self):
        first = self.pack()
        second_path = self.base / "second"
        second = self.pack(second_path)
        for name in ("archive.tar.gz", "manifest.json", "archive.json"):
            self.assertEqual((self.destination / name).read_bytes(), (second_path / name).read_bytes())
        self.assertEqual(first["descriptor_sha256"], second["descriptor_sha256"])

    def test_existing_destination_and_overlapping_roots_are_refused(self):
        self.destination.mkdir()
        (self.destination / "saved").write_bytes(b"keep")
        with self.assertRaises(fmt.ArchiveError):
            self.pack()
        self.assertEqual((self.destination / "saved").read_bytes(), b"keep")
        with self.assertRaises(fmt.ArchiveError):
            self.pack(self.source / "inside")
        linked = self.base / "linked-output"
        linked.symlink_to(self.destination, target_is_directory=True)
        with self.assertRaises(fmt.ArchiveError):
            self.pack(linked)
        self.assertFalse((self.source / "inside").exists())

    def test_file_and_symlink_roots_are_refused(self):
        link = self.base / "source-link"
        link.symlink_to(self.source, target_is_directory=True)
        for path in (self.source / "one.bin", link):
            with self.subTest(path=path), self.assertRaises(fmt.ArchiveError):
                packer.plan({"root": path})

    def test_overlap_check_handles_case_and_unicode_aliases(self):
        # Portable containment is deliberately conservative on case-sensitive
        # hosts too, and must not depend on pathlib normalizing Mac aliases.
        alias = self.source.with_name("SOURCE")
        with self.assertRaisesRegex(fmt.ArchiveError, "overlap"):
            packer._destination(self.source / "new-output", {"root": alias})

    def test_entry_logical_and_single_file_bounds_refuse_before_output(self):
        for kwargs in ({"max_entries": 1}, {"max_bytes": len(self.data) - 1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(fmt.ArchiveError):
                self.pack(**kwargs)
        with mock.patch.object(fmt, "MAX_FILE_BYTES", len(self.data) - 1), self.assertRaises(fmt.ArchiveError):
            self.pack()
        self.assertFalse(self.destination.exists())

    def test_unsupported_entry_fails_but_explicit_exclusion_is_recorded(self):
        fifo = self.source / "pipe"
        os.mkfifo(fifo)
        with self.assertRaisesRegex(fmt.ArchiveError, "Unsupported"):
            self.pack()
        result = self.pack(exclude_names=("pipe",))
        self.assertEqual(result["excluded"], [{"path": "project/pipe", "reason": "excluded name: pipe"}])
        self.assertTrue(stat.S_ISFIFO(fifo.lstat().st_mode))

    def test_empty_directory_and_zero_byte_source_roundtrip(self):
        (self.source / "one.bin").write_bytes(b"")
        (self.source / "empty-directory").mkdir()
        result = self.pack()
        self.assertEqual(result["counts"]["logical_file_bytes"], 0)
        self.assertEqual(result["counts"]["unique_blobs"], 1)
        with tarfile.open(self.destination / "archive.tar.gz") as archive:
            self.assertEqual(archive.extractfile("blobs/" + fmt.digest(b"")).read(), b"")

    def test_changed_source_after_archive_verification_leaves_no_completion_descriptor(self):
        actual_verify = reader.verify_payload
        mutated = []

        def changed(*args, **kwargs):
            result = actual_verify(*args, **kwargs)
            (self.source / "one.bin").write_bytes(b"changed bytes")
            mutated.append(True)
            return result

        with mock.patch.object(reader, "verify_payload", side_effect=changed):
            with self.assertRaises(fmt.ArchiveError):
                self.pack()
        self.assertTrue((self.destination / "archive.tar.gz").exists())
        self.assertFalse((self.destination / "archive.json").exists())
        self.assertEqual(mutated, [True])

    def test_new_source_after_archive_verification_is_detected(self):
        actual_verify = reader.verify_payload
        mutated = []

        def added(*args, **kwargs):
            result = actual_verify(*args, **kwargs)
            (self.source / "late").write_bytes(b"late arrival")
            mutated.append(True)
            return result

        with mock.patch.object(reader, "verify_payload", side_effect=added), self.assertRaises(fmt.ArchiveError):
            self.pack()
        self.assertFalse((self.destination / "archive.json").exists())
        self.assertEqual(mutated, [True])

    def test_replaced_output_directory_does_not_receive_completion_descriptor(self):
        actual_verify = reader.verify_payload
        outside = self.base / "outside-output"
        outside.mkdir()
        saved = self.base / "saved-partial-output"
        mutated = []

        def substitute(*args, **kwargs):
            result = actual_verify(*args, **kwargs)
            self.destination.rename(saved)
            self.destination.symlink_to(outside, target_is_directory=True)
            mutated.append(True)
            return result

        with mock.patch.object(reader, "verify_payload", side_effect=substitute):
            with self.assertRaises((fmt.ArchiveError, OSError)):
                self.pack()
        self.assertEqual(mutated, [True])
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse((saved / "archive.json").exists())

    def test_source_substituted_with_symlink_is_never_read(self):
        outsider = self.base / "outside"
        outsider.write_bytes(b"outside content")
        actual_hash = packer._hash_source
        changed = False

        def substitute(*args, **kwargs):
            nonlocal changed
            if not changed:
                changed = True
                path = self.source / "one.bin"
                path.unlink()
                path.symlink_to(outsider)
            return actual_hash(*args, **kwargs)

        with mock.patch.object(packer, "_hash_source", side_effect=substitute):
            with self.assertRaises((fmt.ArchiveError, OSError)):
                self.pack()
        self.assertEqual(outsider.read_bytes(), b"outside content")
        self.assertFalse(self.destination.exists())

    def test_substituted_directory_ancestor_is_not_followed(self):
        child = self.source / "nested"
        child.mkdir()
        (child / "data").write_bytes(b"owned")
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "data").write_bytes(b"unowned")
        actual_hash = packer._hash_source
        changed = False

        def substitute(*args, **kwargs):
            nonlocal changed
            if not changed:
                changed = True
                child.rename(self.base / "saved-child")
                child.symlink_to(outside, target_is_directory=True)
            return actual_hash(*args, **kwargs)

        with mock.patch.object(packer, "_hash_source", side_effect=substitute):
            with self.assertRaises((fmt.ArchiveError, OSError)):
                self.pack()
        self.assertEqual((outside / "data").read_bytes(), b"unowned")
        self.assertFalse(self.destination.exists())

    def test_compressed_archive_bound_retains_incomplete_output(self):
        with mock.patch.object(fmt, "MAX_ARCHIVE_BYTES", 32), self.assertRaises(fmt.ArchiveError):
            self.pack()
        self.assertTrue((self.destination / "manifest.json").exists())
        self.assertLessEqual((self.destination / "archive.tar.gz").stat().st_size, 32)
        self.assertFalse((self.destination / "archive.json").exists())

    def test_manifest_bound_refuses_before_destination_creation(self):
        with mock.patch.object(fmt, "MAX_MANIFEST_BYTES", 10), self.assertRaises(fmt.ArchiveError):
            self.pack()
        self.assertFalse(self.destination.exists())

    def test_directory_sync_failure_is_reported_after_verified_descriptor(self):
        actual_fsync = os.fsync
        parent_identity = (self.base.stat().st_dev, self.base.stat().st_ino)
        synced_parent, failed_destination = [], []

        def fail_final_directory(fd):
            info = os.fstat(fd)
            if stat.S_ISDIR(info.st_mode):
                if (info.st_dev, info.st_ino) == parent_identity:
                    synced_parent.append(True)
                else:
                    self.assertTrue((self.destination / "archive.json").is_file())
                    failed_destination.append(True)
                    raise OSError("fixture directory fsync failure")
            return actual_fsync(fd)

        with mock.patch.object(packer.os, "fsync", side_effect=fail_final_directory):
            with self.assertRaisesRegex(OSError, "fixture directory fsync failure"):
                self.pack()
        self.assertEqual(synced_parent, [True])
        self.assertEqual(failed_destination, [True])
        self.assertEqual((self.source / "one.bin").read_bytes(), self.data)
        # Verified bytes are retained, but pack did not return a success result.
        descriptor, _ = fmt.load_archive(self.destination)
        self.assertEqual(descriptor["counts"]["files"], 1)

    def test_exact_subtree_exclusion_preserves_same_named_other_subtree(self):
        for parent in ("active", "saved"):
            subtree = self.source / parent / "records"
            subtree.mkdir(parents=True)
            (subtree / "data").write_bytes(parent.encode())
        plan = packer.plan(self.roots, exclude_paths=("project/active/records",))
        self.assertEqual(plan["excluded"], [{"path": "project/active/records", "reason": "excluded exact path"}])
        self.assertIn("project/saved/records/data", {item["path"] for item in plan["entries"]})
        result = self.pack(exclude_paths=("project/active/records",))
        self.assertEqual(result["excluded"], plan["excluded"])
        self.assertEqual((self.source / "active/records/data").read_bytes(), b"active")

    def test_unknown_root_or_shadowed_exact_exclusions_are_refused(self):
        (self.source / ".git").mkdir()
        (self.source / ".git/file").write_bytes(b"saved")
        for paths in (("project",), ("other/file",), ("project/misspelled",), ("project/.git/file",)):
            with self.subTest(paths=paths), self.assertRaises(fmt.ArchiveError):
                self.pack(exclude_paths=paths)
        self.assertFalse(self.destination.exists())

    def test_streaming_file_larger_than_read_chunk(self):
        with (self.source / "one.bin").open("wb") as output:
            output.write(b"a" * fmt.CHUNK_BYTES)
            output.write(b"b" * (fmt.CHUNK_BYTES + 17))
        result = self.pack()
        self.assertEqual(result["counts"]["logical_file_bytes"], 2 * fmt.CHUNK_BYTES + 17)
        self.assertFalse((self.destination / "archive.json").is_symlink())

    def test_invalid_bounds_labels_and_deadlines_fail_closed(self):
        for kwargs in ({"max_seconds": 0}, {"max_seconds": float("inf")}, {"max_seconds": 21601},
                       {"max_seconds": True}, {"max_entries": True},
                       {"max_bytes": -1}, {"exclude_names": ("../escape",)}):
            with self.subTest(kwargs=kwargs), self.assertRaises(fmt.ArchiveError):
                packer.plan(self.roots, **kwargs)
        for roots in ({"a/b": self.source}, {"A": self.source, "a": self.source},
                      {"é": self.source, "e\u0301": self.source}):
            with self.subTest(roots=roots), self.assertRaises(fmt.ArchiveError):
                packer.plan(roots)
        with mock.patch.object(packer.time, "monotonic", side_effect=[0, 2]):
            with self.assertRaisesRegex(fmt.ArchiveError, "time"):
                packer.plan(self.roots, max_seconds=1)


if __name__ == "__main__":
    unittest.main()
