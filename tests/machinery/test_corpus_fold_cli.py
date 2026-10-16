"""Public organ aliases over tiny invented records; no real corpus is read."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


REPOSITORY = Path(__file__).resolve().parents[2]
TOOLKIT = REPOSITORY / "tools/ecosystem/lodestone"
MACHINERY = REPOSITORY / "tools/machinery/run"
BUILT_AT = "2026-10-15T00:00:00Z"


class CorpusFoldCLITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="corpus fold fixture ")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.source = self.base / "source with spaces"
        self.records = self.source / "records with spaces"
        self.records.mkdir(parents=True)
        self.original = self.records / "original note.json"
        self.correction = self.records / "correction note.json"
        self.original.write_bytes(self.canonical({"id": "original", "claim": "invented first record"}))
        self.correction.write_bytes(self.canonical({
            "id": "correction", "claim": "invented correction",
            "corrects": hashlib.sha256(self.original.read_bytes()).hexdigest(),
        }))
        self.before = {path.name: path.read_bytes() for path in self.records.iterdir()}
        output = self.base / "output with spaces"
        output.mkdir()
        self.fold = output / "current view.json"

    @staticmethod
    def canonical(value):
        return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()

    def invoke(self, *arguments, direct=False):
        command = ([sys.executable, "-B", str(MACHINERY)] if direct else
                   [sys.executable, "-B", str(TOOLKIT), "--repo", str(REPOSITORY)])
        return subprocess.run(command + [str(value) for value in arguments],
                              cwd=self.base, text=True, capture_output=True, timeout=30)

    def build_arguments(self):
        return ["build", "--glob", "records with spaces/*.json",
                "--title", "Invented two-record fixture", "--built-at", BUILT_AT,
                "--built-by", "CLI fixture caller"]

    def build(self, *route):
        result = self.invoke(*route, "corpus-fold", "--root=" + str(self.source),
                             "--fold=" + str(self.fold), *self.build_arguments())
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        return json.loads(self.fold.read_text())

    def assert_sources_retained(self):
        self.assertEqual({path.name: path.read_bytes() for path in self.records.iterdir()}, self.before)

    def test_explicit_locations_required_and_help_remains_available(self):
        for options in ([], ["--root", str(self.source)], ["--fold", str(self.fold)],
                        ["--root=", "--fold=" + str(self.fold)],
                        ["--root=" + str(self.source), "--fold="]):
            with self.subTest(options=options):
                result = self.invoke("origami", "corpus-fold", *options, *self.build_arguments())
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn("--root", result.stderr)
                self.assertIn("--fold", result.stderr)
        for route, arguments, direct in ((["yggdrasil"], ["--help"], False),
                                          (["origami"], ["build", "-h"], True)):
            result = self.invoke(*route, "corpus-fold", *arguments, direct=direct)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("--glob" if direct else "--fold", result.stdout)
        self.assertFalse(self.fold.exists())
        self.assert_sources_retained()

    def test_build_verify_and_source_mutation_through_both_organs(self):
        fold = self.build("origami")
        self.assertEqual(fold["built_by"], "CLI fixture caller")
        self.assertEqual(fold["counts"]["records"], 2)
        self.assertEqual(fold["counts"]["displaced"], 1)
        self.assertEqual(fold["counts"]["current"], 1)
        self.assertEqual({entry["path"] for entry in fold["entries"]},
                         {"records with spaces/original note.json", "records with spaces/correction note.json"})
        verified = self.invoke("yggdrasil", "corpus-fold", "--root", self.source,
                               "--fold", self.fold, "verify")
        self.assertEqual(verified.returncode, 0, verified.stderr)
        self.assertEqual(json.loads(verified.stdout)["verdict"], "CLEAN")
        self.assert_sources_retained()
        bound_fold = self.fold.read_bytes()
        self.original.write_bytes(self.canonical({"id": "original", "claim": "fixture mutation"}))
        drifted = self.invoke("yggdrasil", "corpus-fold", "--root", self.source,
                              "--fold", self.fold, "verify", direct=True)
        self.assertEqual(drifted.returncode, 1, drifted.stderr or drifted.stdout)
        report = json.loads(drifted.stdout)
        self.assertEqual(report["verdict"], "DRIFTED")
        self.assertEqual([item["path"] for item in report["drifted"]],
                         ["records with spaces/original note.json"])
        self.assertEqual(self.fold.read_bytes(), bound_fold)
        self.assertEqual(self.correction.read_bytes(), self.before[self.correction.name])

    def test_existing_fold_and_original_source_are_never_overwritten_by_default(self):
        self.build("machinery", "yggdrasil")
        saved_fold = self.fold.read_bytes()
        for destination in (self.fold, self.original):
            with self.subTest(destination=destination):
                result = self.invoke("origami", "corpus-fold", "--root", self.source,
                                     "--fold", destination, *self.build_arguments(), direct=True)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertEqual(self.fold.read_bytes(), saved_fold)
                self.assert_sources_retained()


if __name__ == "__main__":
    unittest.main()
