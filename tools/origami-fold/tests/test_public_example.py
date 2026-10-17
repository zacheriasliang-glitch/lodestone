"""Public example recovery, actual command use and drift checks."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SOURCE))
import build_example
import origami_fold


class PublicExampleTests(unittest.TestCase):
    def test_generated_example_and_changed_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "example"
            path = build_example.create_example(root)
            before = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}
            command = [sys.executable, "-B", str(root / build_example.TOOL)]
            for action in (["verify"], ["orient"], ["expand", "sample.observation"]):
                result = subprocess.run(command + action, cwd=temporary, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
            after = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}
            self.assertEqual(before, after)
            source = root / build_example.EXAMPLE / "RESULT.md"
            source.write_text(source.read_text().replace("three blue", "four blue"))
            result = subprocess.run(command + ["expand", "sample.observation"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn("CHANGED", result.stdout)
            self.assertIn("four blue tiles", result.stdout)
            self.assertIn("three blue tiles", origami_fold.load_fold(path)["claims"][0]["text"])

    def test_existing_destination_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "keep.txt"
            marker.write_bytes(b"keep exact bytes\r\n")
            with self.assertRaises(ValueError):
                build_example.create_example(root)
            self.assertEqual(marker.read_bytes(), b"keep exact bytes\r\n")
            self.assertEqual(list(root.iterdir()), [marker])

    def test_shipped_fold_is_exact(self):
        root = SOURCE.parents[2]
        fold = origami_fold.load_fold(root / origami_fold.DEFAULT_FOLD_REL)
        self.assertEqual(origami_fold.verify(root, fold), [])


if __name__ == "__main__":
    unittest.main()
