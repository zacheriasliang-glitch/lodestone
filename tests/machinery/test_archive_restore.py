"""Tiny operational fixtures for exact, bounded and nontraversing restoration."""
import copy
import gzip
import io
import os
from pathlib import Path
import stat
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from lodestone_machinery import archive_format as fmt
from lodestone_machinery import archive_restore as reader


class ArchiveRestoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.archive = self.root / "archive"
        self.output = self.root / "restored"
        self.stamp = 1_700_000_000_000_000_000
        self.raw = b"Exact file bytes.\r\n\x00\xff" * 23
        self.other = b"Unselected source must still be verified."
        self.blobs = {fmt.digest(raw): raw for raw in (self.raw, self.other, b"")}
        self.manifest = {
            "schema": fmt.MANIFEST_SCHEMA, "epoch": "tiny-reader-fixture",
            "authority_ref": "implementation verification", "roots": [
                {"label": "files", "path": "/not-opened-original"}],
            "entries": [
                self.entry("files", "directory", mode=0o750),
                self.entry("files/a.bin", "file", size=len(self.raw), sha256=fmt.digest(self.raw), mode=0o640),
                self.entry("files/empty", "file", size=0, sha256=fmt.digest(b"")),
                self.entry("files/link", "symlink", target="../../outside", mode=0o777),
                self.entry("files/sub", "directory", mode=0o700),
                self.entry("files/sub/copy.bin", "file", size=len(self.raw), sha256=fmt.digest(self.raw)),
                self.entry("files/sub/other.bin", "file", size=len(self.other), sha256=fmt.digest(self.other)),
            ],
            "excluded": [{"path": "files/.git", "reason": "explicit basename exclusion"}],
            "metadata_scope": fmt.METADATA_SCOPE,
        }

    def entry(self, path, kind, mode=0o600, **extra):
        return dict(path=path, kind=kind, mode=mode, mtime_ns=self.stamp, **extra)

    @staticmethod
    def member(name, raw, kind=tarfile.REGTYPE, linkname=""):
        info = tarfile.TarInfo(name)
        info.mode, info.uid, info.gid, info.mtime = 0o600, 0, 0, 0
        info.uname = info.gname = ""
        info.size, info.type, info.linkname = len(raw), kind, linkname
        return info, raw

    def tar_bytes(self, manifest=None, blobs=None, extra=(), embedded=None, omit=()):
        manifest = self.manifest if manifest is None else manifest
        blobs = self.blobs if blobs is None else blobs
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w", format=tarfile.GNU_FORMAT) as tar:
            raw = fmt.canonical(manifest) if embedded is None else embedded
            members = [self.member("manifest.json", raw)]
            members += [self.member("blobs/" + sha, body) for sha, body in sorted(blobs.items()) if sha not in omit]
            members += list(extra)
            for info, body in members:
                tar.addfile(info, io.BytesIO(body))
        return output.getvalue()

    def publish(self, *, tar=None, manifest=None, compressed=None):
        manifest = self.manifest if manifest is None else manifest
        self.archive.mkdir(exist_ok=True)
        if compressed is None:
            compressed = gzip.compress(self.tar_bytes(manifest=manifest) if tar is None else tar, mtime=0)
        raw_manifest = fmt.canonical(manifest)
        descriptor = {
            "schema": fmt.DESCRIPTOR_SCHEMA,
            "archive": dict(path="archive.tar.gz", bytes=len(compressed), sha256=fmt.digest(compressed)),
            "manifest": dict(path="manifest.json", bytes=len(raw_manifest), sha256=fmt.digest(raw_manifest)),
            "counts": fmt.manifest_counts(manifest), "metadata_scope": fmt.METADATA_SCOPE,
        }
        (self.archive / "archive.tar.gz").write_bytes(compressed)
        (self.archive / "manifest.json").write_bytes(raw_manifest)
        (self.archive / "archive.json").write_bytes(fmt.canonical(descriptor))
        return descriptor

    def test_verify_prepublication_and_full_cost(self):
        descriptor = self.publish()
        descriptor_path = self.archive / "archive.json"
        saved = descriptor_path.read_bytes()
        descriptor_path.unlink()
        result = reader.verify_payload(self.archive / "archive.tar.gz", self.manifest)
        self.assertEqual(result["coverage"], "ALL_MEMBERS_AND_BLOBS")
        self.assertEqual(result["members"], 4)
        self.assertEqual(result["counts"]["files"], 4)
        descriptor_path.write_bytes(saved)
        result = reader.verify(self.archive, expected_descriptor_sha256=fmt.digest(saved))
        self.assertEqual(result["cost"]["total_artifact_bytes"], sum(p.stat().st_size for p in self.archive.iterdir()))
        self.assertEqual(result["archive"]["sha256"], descriptor["archive"]["sha256"])
        self.assertEqual(result["excluded"], self.manifest["excluded"])

    def test_exact_restore_dedup_empty_bytes_and_metadata(self):
        self.publish()
        outside = self.root / "outside"
        outside.write_bytes(b"untouched")
        with patch.object(tarfile.TarFile, "extractall", side_effect=AssertionError("never extractall")):
            result = reader.restore(self.archive, self.output)
        self.assertEqual(result["status"], "RESTORED")
        self.assertEqual(result["restored"], dict(files=4, directories=2, symlinks=1,
            logical_file_bytes=2 * len(self.raw) + len(self.other)))
        for entry in self.manifest["entries"]:
            path = self.output / entry["path"]
            info = path.lstat()
            self.assertEqual(stat.S_IMODE(info.st_mode), entry["mode"])
            self.assertEqual(info.st_mtime_ns, entry["mtime_ns"])
            if entry["kind"] == "file":
                self.assertEqual(path.read_bytes(), self.blobs[entry["sha256"]])
            elif entry["kind"] == "symlink":
                self.assertEqual(os.readlink(path), entry["target"])
        self.assertEqual(outside.read_bytes(), b"untouched")
        self.assertNotEqual((self.output / "files/a.bin").stat().st_ino,
                            (self.output / "files/sub/copy.bin").stat().st_ino)

    def test_selected_file_and_subtree_create_only_required_ancestors(self):
        self.publish()
        result = reader.restore(self.archive, self.output, paths=["files/sub/copy.bin"])
        self.assertEqual(result["restored"]["files"], 1)
        self.assertEqual(result["restored"]["directories"], 2)
        self.assertFalse((self.output / "files/a.bin").exists())
        self.assertEqual((self.output / "files/sub/copy.bin").read_bytes(), self.raw)
        second = self.root / "subtree"
        result = reader.restore(self.archive, second, paths=["files/sub", "files/sub/copy.bin"])
        self.assertEqual(result["restored"]["files"], 2)
        self.assertFalse((second / "files/link").exists())

    def test_selected_restore_still_rejects_unselected_corruption(self):
        blobs = dict(self.blobs)
        blobs[fmt.digest(self.other)] = b"X" * len(self.other)
        self.publish(tar=self.tar_bytes(blobs=blobs))
        with self.assertRaisesRegex(fmt.ArchiveError, "partial output.*Blob SHA-256"):
            reader.restore(self.archive, self.output, paths=["files/a.bin"])
        self.assertTrue(self.output.is_dir())
        self.assertFalse((self.output / "files/link").is_symlink())

    def test_exclusive_destination_preserves_existing_bytes(self):
        self.publish()
        self.output.mkdir()
        marker = self.output / "saved"
        marker.write_bytes(b"preserve")
        with self.assertRaises(fmt.ArchiveError):
            reader.restore(self.archive, self.output)
        self.assertEqual(marker.read_bytes(), b"preserve")
        self.assertEqual(list(self.output.iterdir()), [marker])

    def test_symlink_archive_and_destination_parents_are_refused(self):
        self.publish()
        alias = self.root / "alias"
        alias.symlink_to(self.archive, target_is_directory=True)
        with self.assertRaises(fmt.ArchiveError):
            reader.verify(alias)
        destination_parent = self.root / "alias-parent"
        destination_parent.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(fmt.ArchiveError):
            reader.restore(self.archive, destination_parent / "escape")
        self.assertFalse((self.root / "escape").exists())
        payload = self.archive / "archive.tar.gz"
        saved = self.root / "real-payload"
        payload.rename(saved)
        payload.symlink_to(saved)
        with self.assertRaises(fmt.ArchiveError):
            reader.verify_payload(payload, self.manifest)

    def test_manifest_traversal_and_symlink_parent_fail_before_output(self):
        for mutation in ("traversal", "link-parent"):
            with self.subTest(mutation=mutation):
                manifest = copy.deepcopy(self.manifest)
                if mutation == "traversal":
                    manifest["entries"][1]["path"] = "files/../escape"
                else:
                    manifest["entries"].append(self.entry("files/link/child", "file", size=0, sha256=fmt.digest(b"")))
                    manifest["entries"].sort(key=lambda entry: entry["path"])
                self.publish(manifest=manifest)
                with self.assertRaises(fmt.ArchiveError):
                    reader.restore(self.archive, self.output)
                self.assertFalse(self.output.exists())

    def test_replaced_restore_directory_cannot_redirect_file_writes(self):
        self.publish()
        outside = self.root / "outside-directory"
        outside.mkdir()
        original = reader._Destination.directories

        def substitute(sink):
            original(sink)
            (self.output / "files/sub").rmdir()
            (self.output / "files/sub").symlink_to(outside, target_is_directory=True)

        with patch.object(reader._Destination, "directories", substitute):
            with self.assertRaisesRegex(fmt.ArchiveError, "partial output"):
                reader.restore(self.archive, self.output, paths=["files/sub"])
        self.assertEqual(list(outside.iterdir()), [])

    def test_replaced_archive_path_is_not_reported_verified(self):
        self.publish()
        path = self.archive / "archive.tar.gz"
        compressed = path.read_bytes()
        original = reader._GzipReader._fill
        replaced = False

        def substitute(stream):
            nonlocal replaced
            if not replaced:
                replaced = True
                path.rename(self.archive / "saved-old-payload")
                path.write_bytes(compressed)
            return original(stream)

        with patch.object(reader._GzipReader, "_fill", substitute):
            with self.assertRaisesRegex(fmt.ArchiveError, "changed during"):
                reader.verify(self.archive)

    def test_replaced_restored_file_is_not_reported_complete(self):
        self.publish()
        original = reader._Destination.file_complete

        def substitute(sink, stream, entry):
            original(sink, stream, entry)
            path = self.output / entry["path"]
            path.rename(path.with_name("saved-old-output"))
            path.write_bytes(b"substitution")

        with patch.object(reader._Destination, "file_complete", substitute):
            with self.assertRaisesRegex(fmt.ArchiveError, "Restored file path changed"):
                reader.restore(self.archive, self.output, paths=["files/a.bin"])

    def test_noncanonical_or_mismatched_embedded_manifest_is_rejected(self):
        for embedded in (fmt.canonical(self.manifest) + b"\n", b"X" * len(fmt.canonical(self.manifest))):
            with self.subTest(size=len(embedded)):
                self.publish(tar=self.tar_bytes(embedded=embedded))
                with self.assertRaises(fmt.ArchiveError):
                    reader.verify(self.archive)

    def test_sidecar_and_independent_pin_authentication(self):
        self.publish()
        with self.assertRaises(fmt.ArchiveError):
            reader.verify(self.archive, expected_descriptor_sha256="0" * 64)
        manifest = self.archive / "manifest.json"
        manifest.write_bytes(manifest.read_bytes() + b"\n")
        with self.assertRaises(fmt.ArchiveError):
            reader.verify(self.archive)

    def test_duplicate_extra_missing_link_and_extension_members_rejected(self):
        sha = fmt.digest(self.raw)
        cases = [
            dict(extra=[self.member("blobs/" + sha, self.raw)]),
            dict(extra=[self.member("../escape", b"bad")]),
            dict(omit=[sha]),
            dict(extra=[self.member("link", b"", kind=tarfile.SYMTYPE, linkname="/outside")]),
            dict(extra=[self.member("pax", b"", kind=tarfile.XHDTYPE)]),
        ]
        for index, options in enumerate(cases):
            with self.subTest(case=index):
                self.publish(tar=self.tar_bytes(**options))
                with self.assertRaises(fmt.ArchiveError):
                    reader.verify(self.archive)
        self.assertFalse((self.root / "escape").exists())

    def test_padding_hidden_trailing_data_and_gzip_concatenation_rejected(self):
        raw = self.tar_bytes()
        broken_padding = bytearray(raw)
        manifest_end = 512 + len(fmt.canonical(self.manifest))
        self.assertNotEqual(manifest_end % 512, 0)
        broken_padding[manifest_end] = 1
        variants = [gzip.compress(broken_padding, mtime=0),
                    gzip.compress(raw + b"hidden", mtime=0),
                    gzip.compress(raw, mtime=0) + gzip.compress(b"\0" * 512, mtime=0),
                    gzip.compress(raw, mtime=0) + b"trailing"]
        for index, compressed in enumerate(variants):
            with self.subTest(case=index):
                self.publish(compressed=compressed)
                with self.assertRaises(fmt.ArchiveError):
                    reader.verify(self.archive)

    def test_gzip_truncation_and_crc_failure_rejected_even_with_new_container_pin(self):
        compressed = gzip.compress(self.tar_bytes(), mtime=0)
        corrupt_crc = bytearray(compressed)
        corrupt_crc[-8] ^= 1
        for body in (compressed[:-1], bytes(corrupt_crc)):
            self.publish(compressed=body)
            with self.assertRaises(fmt.ArchiveError):
                reader.verify(self.archive)

    def test_compressed_digest_size_and_optional_pin_shape(self):
        descriptor = self.publish()
        path = self.archive / "archive.tar.gz"
        for pin in ({"bytes": descriptor["archive"]["bytes"], "sha256": "0" * 64},
                    {"bytes": 0, "sha256": descriptor["archive"]["sha256"]},
                    descriptor["archive"], {"bytes": True, "sha256": "0" * 64}):
            with self.subTest(pin=pin):
                with self.assertRaises(fmt.ArchiveError):
                    reader.verify_payload(path, self.manifest, expected_archive=pin)

    def test_streaming_small_chunks_and_refusal_bounds(self):
        self.publish()
        with patch.object(reader, "CHUNK_BYTES", 31):
            self.assertTrue(reader.verify(self.archive)["full_integrity"])
            reader.restore(self.archive, self.output, paths=["files/sub"])
        with patch.object(reader, "MAX_ARCHIVE_BYTES", 32):
            with self.assertRaises(fmt.ArchiveError):
                reader.verify(self.archive)
        for seconds in (0, -1, True, 21601, float("nan"), float("inf")):
            with self.subTest(seconds=seconds), self.assertRaises(fmt.ArchiveError):
                reader.verify(self.archive, max_seconds=seconds)
        with patch.object(reader.time, "monotonic", side_effect=[100, 102]):
            with self.assertRaisesRegex(fmt.ArchiveError, "exceeded"):
                reader.verify(self.archive, max_seconds=1)

    def test_selection_is_explicit_and_invalid_selection_creates_nothing(self):
        self.publish()
        for paths in ([], "files", ["files/missing"], ["../escape"]):
            with self.subTest(paths=paths), self.assertRaises(fmt.ArchiveError):
                reader.restore(self.archive, self.output, paths=paths)
            self.assertFalse(self.output.exists())

    def test_gnu_large_size_header_has_no_extension_member(self):
        # No large allocation or I/O: validate the agreed numeric header format.
        header = reader._header("blobs/" + "a" * 64, 9 * 1024**3)
        info = tarfile.TarInfo.frombuf(header, "utf-8", "strict")
        self.assertEqual(len(header), 512)
        self.assertEqual(info.size, 9 * 1024**3)
        self.assertEqual(info.type, tarfile.REGTYPE)


if __name__ == "__main__":
    unittest.main()
