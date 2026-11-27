"""Native dependency custody with tiny local fixtures, no downloads or installs."""
import hashlib
import io
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from desktop import dependencies as d
from desktop.platform_support import open_regular
from desktop import platform_support as host


class DependencyCustody(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.cache = self.root / "cache"
        self.cache.mkdir()
        self.outside = self.root / "outside"
        self.outside.mkdir()

    def cached(self, raw=b"pinned fixture", filename="fixture.bin"):
        artifact = {"filename": filename, "url": "https://example.invalid/" + filename,
                    "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        (self.cache / artifact["sha256"]).write_bytes(raw)
        return artifact

    def alias(self, link, target, *, directory=True):
        if os.name == "nt" and directory:
            result = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.addCleanup(lambda: os.rmdir(link) if link.exists() else None)
        else:
            try:
                link.symlink_to(target, target_is_directory=directory)
            except OSError as exc:
                if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
                    self.skipTest("File symlink privilege unavailable; directory junction tests still execute")
                raise

    def test_verified_cache_cannot_be_reused_through_aliased_root(self):
        artifact = self.cached()
        alias = self.root / "cache-alias"
        self.alias(alias, self.cache)
        with patch.object(d.subprocess, "run", side_effect=AssertionError("No network process")):
            with self.assertRaises(ValueError):
                d.fetch(artifact, alias, offline=True)
        self.assertEqual((self.cache / artifact["sha256"]).read_bytes(), b"pinned fixture")

    def test_asset_parent_alias_is_refused_before_copy_or_creation(self):
        artifact = self.cached()
        alias = self.root / "tokenizer"
        self.alias(alias, self.outside)
        with self.assertRaises(ValueError):
            d._copy_asset(artifact, alias / "nested/file", self.cache, offline=True)
        self.assertEqual(list(self.outside.iterdir()), [])

    def test_root_alias_is_rejected_before_ensure_or_doctor_resolves_it(self):
        alias = self.root / "installation"
        self.alias(alias, self.outside)
        for operation in (d.ensure, d.verify):
            with self.subTest(operation=operation.__name__), self.assertRaises(ValueError):
                operation(alias, self.root / "absent-payload")
        self.assertEqual(list(self.outside.iterdir()), [])

    def test_asset_copy_is_durable_and_does_not_rewrite_existing_corruption(self):
        artifact = self.cached()
        target = self.root / "tokenizer/file"
        d._copy_asset(artifact, target, self.cache, offline=True)
        self.assertEqual(target.read_bytes(), b"pinned fixture")
        target.write_bytes(b"retained damage")
        with self.assertRaises(ValueError):
            d._copy_asset(artifact, target, self.cache, offline=True)
        self.assertEqual(target.read_bytes(), b"retained damage")

    def test_guarded_directory_supports_atomic_publication_and_refuses_native_rename(self):
        directory = self.root / "guarded"
        with host.guarded_directory(directory, create=True):
            host.atomic_write(directory / "record", b"first", replace=False)
            host.atomic_write(directory / "record", b"second")
            stage = directory / "stage"
            stage.mkdir()
            host.atomic_write(stage / "child", b"retained")
            host.durable_move(stage, directory / "published")
            self.assertEqual(host.read_file(directory / "record"), b"second")
            self.assertEqual(host.read_file(directory / "published/child"), b"retained")
            if os.name == "nt":
                with self.assertRaises(PermissionError):
                    directory.rename(self.root / "redirected")

    def test_streaming_reader_detects_or_prevents_file_replacement(self):
        path, replacement = self.root / "first", self.root / "replacement"
        path.write_bytes(b"original")
        replacement.write_bytes(b"changed")
        if os.name == "nt":
            with open_regular(path) as stream:
                with self.assertRaises(PermissionError):
                    replacement.replace(path)
                self.assertEqual(stream.read(), b"original")
        else:
            with self.assertRaisesRegex(ValueError, "identity changed"):
                with open_regular(path) as stream:
                    replacement.replace(path)
                    self.assertEqual(stream.read(), b"original")

    def archive(self, *, alias=False):
        content = io.BytesIO()
        with tarfile.open(fileobj=content, mode="w:gz") as stream:
            raw = b"#!/bin/sh\nexit 0\n"
            info = tarfile.TarInfo("bin/tool")
            info.mode, info.size = 0o755, len(raw)
            stream.addfile(info, io.BytesIO(raw))
            if alias:
                info = tarfile.TarInfo("bin/alias")
                info.type, info.linkname = tarfile.SYMTYPE, "tool"
                stream.addfile(info)
        artifact = self.cached(content.getvalue(), "fixture.tar.gz")
        artifact["executable"] = "bin/tool"
        return artifact

    def test_archive_destination_alias_is_refused_even_if_tree_bytes_match(self):
        artifact = self.archive()
        destination = self.root / "unpacked"
        d.unpack(artifact, self.cache, self.outside / "tree", offline=True)
        self.alias(destination, self.outside / "tree")
        before = d._inventory(self.outside)
        with self.assertRaises(ValueError):
            d.unpack(artifact, self.cache, destination, offline=True)
        self.assertEqual(d._inventory(self.outside), before)

    @unittest.skipIf(os.name == "nt", "POSIX Python archives retain legitimate links")
    def test_archive_pinned_relative_symlinks_remain_supported(self):
        artifact = self.archive(alias=True)
        destination = self.root / "python"
        executable = d.unpack(artifact, self.cache, destination, offline=True)
        self.assertEqual(os.readlink(destination / "bin/alias"), "tool")
        self.assertEqual(d.unpack(artifact, self.cache, destination, offline=True), executable)
        self.assertEqual(d._inventory(destination)["bin/alias"], {"link": "tool"})

    @unittest.skipUnless(os.name == "nt", "Native Windows junction inventory")
    def test_inventory_refuses_junction_before_traversing_target(self):
        tree = self.root / "tree"
        tree.mkdir()
        self.alias(tree / "redirect", self.outside)
        with self.assertRaisesRegex(ValueError, "reparse point"):
            d._inventory(tree)


if __name__ == "__main__":
    unittest.main()
