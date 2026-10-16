"""Adversarial metadata and exact-byte bindings for portable file archives."""
import copy
import os
from pathlib import Path
import tempfile
import unittest

from lodestone_machinery import archive_format as fmt


class ArchiveFormatTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.manifest = {
            "schema": fmt.MANIFEST_SCHEMA, "epoch": "fixture", "authority_ref": "implementation test",
            "roots": [{"label": "source", "path": "/source"}],
            "entries": [{"path": "source", "kind": "directory", "mode": 0o700, "mtime_ns": 1},
                        {"path": "source/file", "kind": "file", "mode": 0o600, "mtime_ns": 2,
                         "size": 3, "sha256": fmt.digest(b"abc")}],
            "excluded": [{"path": "source/.git", "reason": "explicit exclusion"}],
            "metadata_scope": fmt.METADATA_SCOPE}

    def write_metadata(self):
        raw = fmt.canonical(self.manifest)
        (self.root / "manifest.json").write_bytes(raw)
        (self.root / "archive.tar.gz").write_bytes(b"fixture payload; not yet verified")
        descriptor = {
            "schema": fmt.DESCRIPTOR_SCHEMA, "metadata_scope": fmt.METADATA_SCOPE,
            "manifest": {"path": "manifest.json", "bytes": len(raw), "sha256": fmt.digest(raw)},
            "archive": {"path": "archive.tar.gz", **fmt.hash_file(self.root / "archive.tar.gz")},
            "counts": fmt.manifest_counts(self.manifest)}
        (self.root / "archive.json").write_bytes(fmt.canonical(descriptor))
        return descriptor

    def test_metadata_binds_counts_bytes_and_external_digest(self):
        descriptor = self.write_metadata()
        self.assertEqual(fmt.load_archive(self.root, expected_descriptor_sha256=fmt.digest(
            fmt.canonical(descriptor))), (descriptor, self.manifest))
        with self.assertRaisesRegex(fmt.ArchiveError, "pin"):
            fmt.load_archive(self.root, expected_descriptor_sha256="0" * 64)
        (self.root / "manifest.json").write_bytes(fmt.canonical(self.manifest).replace(b"fixture", b"altered"))
        with self.assertRaises(fmt.ArchiveError):
            fmt.load_archive(self.root)

    def test_duplicate_json_fields_and_wrong_counts_are_rejected(self):
        descriptor = self.write_metadata()
        raw = fmt.canonical(descriptor)
        (self.root / "archive.json").write_bytes(b'{"schema":"wrong",' + raw[1:])
        with self.assertRaisesRegex(fmt.ArchiveError, "canonical"):
            fmt.load_archive(self.root)
        descriptor["counts"]["files"] = True
        (self.root / "archive.json").write_bytes(fmt.canonical(descriptor))
        with self.assertRaisesRegex(fmt.ArchiveError, "counts"):
            fmt.load_archive(self.root)

    def test_path_traversal_missing_parent_and_symlink_parent_rejected(self):
        for path in ("/outside", "source/../outside", "source//file", "source/./file", "source/missing/file"):
            with self.subTest(path=path):
                value = copy.deepcopy(self.manifest)
                value["entries"][1]["path"] = path
                value["entries"].sort(key=lambda e: e["path"])
                with self.assertRaises(fmt.ArchiveError):
                    fmt.validate_manifest(value)
        value = copy.deepcopy(self.manifest)
        value["entries"].insert(1, {"path": "source/a", "kind": "symlink", "mode": 0o777,
                                    "mtime_ns": 1, "target": "/outside"})
        value["entries"][2]["path"] = "source/a/file"
        with self.assertRaisesRegex(fmt.ArchiveError, "parent"):
            fmt.validate_manifest(value)

    def test_unicode_case_collision_and_root_collision_rejected(self):
        for a, b in (("File", "file"), ("caf\u00e9", "cafe\u0301")):
            value = copy.deepcopy(self.manifest)
            value["entries"][1]["path"] = "source/" + a
            entry = dict(value["entries"][1], path="source/" + b)
            value["entries"].append(entry)
            value["entries"].sort(key=lambda e: e["path"])
            with self.assertRaisesRegex(fmt.ArchiveError, "normalized"):
                fmt.validate_manifest(value)
        value = copy.deepcopy(self.manifest)
        value["roots"].append({"label": "Source", "path": "/another"})
        with self.assertRaises(fmt.ArchiveError):
            fmt.validate_manifest(value)

    def test_strict_integer_bounds_and_hash_size_consistency(self):
        for key, value in (("size", True), ("size", -1), ("mode", 0o4000),
                           ("mtime_ns", False), ("sha256", "A" * 64),
                           ("kind", []), ("kind", {})):
            manifest = copy.deepcopy(self.manifest)
            manifest["entries"][1][key] = value
            with self.subTest(key=key), self.assertRaises(fmt.ArchiveError):
                fmt.validate_manifest(manifest)
        manifest = copy.deepcopy(self.manifest)
        manifest["entries"].append(dict(manifest["entries"][1], path="source/second", size=4))
        with self.assertRaisesRegex(fmt.ArchiveError, "inconsistent"):
            fmt.validate_manifest(manifest)

    def test_no_symlink_archive_components_and_hash_bounds(self):
        self.write_metadata()
        link = self.root / "link"
        link.symlink_to(self.root / "manifest.json")
        with self.assertRaises(fmt.ArchiveError):
            fmt.hash_file(link)
        with self.assertRaises(fmt.ArchiveError):
            fmt.hash_file(self.root / "manifest.json", max_bytes=1)
        original = self.root / "archive.json"
        moved = self.root / "saved.json"
        original.rename(moved)
        original.symlink_to(moved)
        with self.assertRaises(fmt.ArchiveError):
            fmt.load_archive(self.root)


if __name__ == "__main__":
    unittest.main()
