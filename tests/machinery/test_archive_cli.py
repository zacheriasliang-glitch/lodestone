"""Real public toolkit routes, explicit archive copies, and exact recovery."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPOSITORY = Path(__file__).resolve().parents[2]
TOOLKIT = REPOSITORY / "tools/ecosystem/lodestone"


class ArchiveCLITests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.source = self.root / "source with spaces"
        self.source.mkdir()
        self.raw = b"Exact original bytes.\r\n" * 200
        (self.source / "one.txt").write_bytes(self.raw)
        (self.source / "two.txt").write_bytes(self.raw)
        self.archive = self.root / "archive"

    def invoke(self, *args):
        return subprocess.run([sys.executable, "-B", str(TOOLKIT), "--repo", str(REPOSITORY),
                               *map(str, args)], cwd=self.root, text=True, capture_output=True,
                              timeout=30)

    def success(self, *args):
        result = self.invoke(*args)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        return json.loads(result.stdout)

    def test_pack_origami_verify_yggdrasil_restore_selected(self):
        self.success("origami", "archive", "pack", "--root", "files=" + str(self.source),
                     "--destination", self.archive, "--epoch", "fixture", "--authority-ref", "unit test")
        inspected = self.success("yggdrasil", "archive", "inspect", "--archive", self.archive)
        self.assertEqual(inspected["status"], "METADATA_VALIDATED_PAYLOAD_UNVERIFIED")
        self.assertEqual(inspected["descriptor"]["counts"]["files"], 2)
        self.assertEqual(inspected["descriptor"]["counts"]["unique_blobs"], 1)
        listed = self.success("yggdrasil", "archive", "list", "--archive", self.archive,
                              "--prefix", "files", "--limit", "1", "--offset", "1")
        self.assertEqual([entry["path"] for entry in listed["entries"]], ["files/one.txt"])
        self.assertEqual(listed["total_matches"], 3)
        self.assertEqual(listed["next_offset"], 2)
        self.success("machinery", "yggdrasil", "archive", "verify", "--archive", self.archive,
                     "--expected-sha256", inspected["descriptor_sha256"])
        restored = self.root / "restored"
        self.success("yggdrasil", "archive", "restore", "--archive", self.archive,
                     "--destination", restored, "--path", "files/one.txt")
        self.assertEqual((restored / "files/one.txt").read_bytes(), self.raw)
        self.assertFalse((restored / "files/two.txt").exists())
        self.assertEqual((self.source / "one.txt").read_bytes(), self.raw)
        result = self.invoke("origami", "archive", "restore", "--archive", self.archive,
                             "--destination", restored)
        self.assertEqual(result.returncode, 2)
        self.assertEqual((restored / "files/one.txt").read_bytes(), self.raw)

    def test_crystal_help_routes_to_crystal_instead_of_lineage(self):
        for route in (("yggdrasil",), ("machinery", "yggdrasil")):
            with self.subTest(route=route):
                result = self.invoke(*route, "crystal", "--help")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("consolidate", result.stdout)
                self.assertNotIn("receipt corpus lineage", result.stdout)

    def test_invalid_roots_refuse_before_output(self):
        for roots in (("missing_equals",), ("a=" + str(self.source), "a=" + str(self.source))):
            arguments = []
            for root in roots:
                arguments += ["--root", root]
            result = self.invoke("origami", "archive", "pack", *arguments,
                                 "--destination", self.archive, "--epoch", "fixture",
                                 "--authority-ref", "unit test")
            self.assertEqual(result.returncode, 2, result.stdout)
            self.assertFalse(self.archive.exists())


if __name__ == "__main__":
    unittest.main()
