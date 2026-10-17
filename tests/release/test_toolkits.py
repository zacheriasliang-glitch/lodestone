"""Portable toolkit integration using synthetic local state only."""
import importlib.util
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


def run(*args, cwd=ROOT):
    return subprocess.run([sys.executable, "-B", str(ROOT / "lodestone.py"), *map(str, args)],
                          cwd=cwd, capture_output=True, text=True, timeout=60)


class ToolkitTests(unittest.TestCase):
    def test_memory_selection_and_continuity_drift_through_launcher(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            store = base / "memory"
            for args in (("memory", "init", store),
                         ("memory", "add", store, "--title", "Constraint", "--body", "Preserve ceramic samples.", "--source", "notes.md"),
                         ("memory", "verify", store)):
                result = run(*args, cwd=base)
                self.assertEqual(result.returncode, 0, result.stderr)
            result = run("memory", "packet", store, "ceramic", "--max-words", "100", cwd=base)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("ceramic", result.stdout)
            source = base / "notes.md"; source.write_text("original\n")
            checkpoint = base / "checkpoint.json"
            result = run("continuity", "create", "--root", base, "--output", checkpoint,
                         "--task", "Inspect", "--status", "IN_PROGRESS", "--next-action", "Review",
                         "--source", "notes.md", cwd=base)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(run("continuity", "verify", checkpoint, "--root", base).returncode, 0)
            source.write_text("changed\n")
            self.assertNotEqual(run("continuity", "verify", checkpoint, "--root", base).returncode, 0)

    def test_link_check_reports_broken_local_navigation(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            (base / "README.md").write_text("[missing](missing.md)\n[web](https://example.test/)\n")
            result = run("check", "--root", base)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)["failures"][0]["target"], "missing.md")
            (base / "missing.md").write_text("present\n")
            self.assertEqual(run("check", "--root", base).returncode, 0)
            (base / "README.md").write_text('[titled](missing.md "A title")\n[ref]: missing.md "Title"\n')
            self.assertEqual(run("check", "--root", base).returncode, 0)
            (base / "README.md").write_text('[ref]: absent.md "Title"\n')
            self.assertEqual(run("check", "--root", base).returncode, 2)

    def test_repository_export_isolated_recovery_and_tamper(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve(); source = base / "repo"; source.mkdir()
            def git(*args):
                return subprocess.run(["git", "-C", str(source), *args], check=True, capture_output=True)
            git("init", "-q")
            (source / "README.md").write_text("Synthetic project\n")
            git("add", "README.md")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-qm", "fixture")
            git("update-ref", "refs/custom/preserved", "HEAD")
            manifest, output = base / "manifest.json", base / "export"
            for invalid in (".", "../outside", "CONTENT_SHA256.json", "CONTENT_SHA256.json/child"):
                result = run("repository", "manifest", "--repository", source, "--file", invalid, "--output", manifest)
                self.assertEqual(result.returncode, 2)
                self.assertNotIn("Traceback", result.stderr)
                self.assertFalse(manifest.exists())
            result = run("repository", "manifest", "--repository", source, "--file", "README.md", "--output", manifest)
            self.assertEqual(result.returncode, 0, result.stderr)
            original_manifest = manifest.read_text()
            collision = json.loads(original_manifest)
            collision["generated"] = {"README.md/child.txt": "invalid hierarchy"}
            manifest.write_text(json.dumps(collision))
            result = run("repository", "export", "--repository", source, "--manifest", manifest, "--output", output)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(output.exists())
            manifest.write_text(original_manifest)
            result = run("repository", "export", "--repository", source, "--manifest", manifest, "--output", output)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(git("status", "--porcelain").stdout, b"")
            result = run("repository", "verify", output)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["status"], "VERIFIED")
            self.assertEqual(json.loads(result.stdout)["refs"], 2)
            bundle = output / "private-repository.bundle"
            original_bundle = bundle.read_bytes()
            state_path = output / "private-state.json"
            original_state = state_path.read_bytes()
            git("bundle", "create", str(base / "head-only.bundle"), "HEAD")
            bundle.write_bytes((base / "head-only.bundle").read_bytes())
            state = json.loads(original_state)
            state["bundle_sha256"] = hashlib.sha256(bundle.read_bytes()).hexdigest()
            state_path.write_text(json.dumps(state))
            self.assertEqual(run("repository", "verify", output).returncode, 2)
            bundle.write_bytes(original_bundle); state_path.write_bytes(original_state)
            with (output / "clean-harness.tar.gz").open("ab") as stream:
                stream.write(b"tampered")
            self.assertEqual(run("repository", "verify", output).returncode, 2)


if __name__ == "__main__":
    unittest.main()
