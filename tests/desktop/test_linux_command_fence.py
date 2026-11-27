"""The opt-in Linux fence script, isolated: a fake apparmor_parser and a
temporary profile directory. No host policy is read or changed."""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "desktop" / "Enable Linux Command Fence.sh"
TARGET = "lodestone-bwrap-userns-restrict"

FAKE_PARSER = """#!/bin/bash
# Records each call; LOAD_FAIL / UNLOAD_FAIL make it fail; keeps a fake loaded list.
echo "$*" >> "$FAKE_LOG"
case "$1" in
  -a) [[ -n "${LOAD_FAIL:-}" ]] && exit 1
      grep -q '^bwrap ' "$FAKE_LOADED" && exit 1
      printf 'bwrap (enforce)\\nunpriv_bwrap (enforce)\\n' > "$FAKE_LOADED" ;;
  -r) [[ -n "${LOAD_FAIL:-}" ]] && exit 1
      printf 'bwrap (enforce)\\nunpriv_bwrap (enforce)\\n' > "$FAKE_LOADED" ;;
  -R) [[ -n "${UNLOAD_FAIL:-}" ]] && exit 1
      : > "$FAKE_LOADED" ;;
esac
"""


@unittest.skipUnless(os.name == "posix" and shutil.which("bash"), "POSIX shell script")
class CommandFenceScriptTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.base)
        self.profiles = self.base / "apparmor.d"
        self.profiles.mkdir()
        self.loaded = self.base / "loaded"
        self.loaded.write_text("")
        self.log = self.base / "parser.log"
        parser = self.base / "apparmor_parser"
        parser.write_text(FAKE_PARSER)
        parser.chmod(0o755)
        bwrap = self.base / "bwrap"
        bwrap.write_text("#!/bin/sh\n")
        bwrap.chmod(0o755)
        self.env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LODESTONE_FENCE_TEST_NOT_ROOT": "0",
                    "LODESTONE_APPARMOR_DIR": str(self.profiles), "LODESTONE_APPARMOR_PARSER": str(parser),
                    "LODESTONE_APPARMOR_PROFILES": str(self.loaded), "LODESTONE_BWRAP": str(bwrap),
                    "FAKE_LOG": str(self.log), "FAKE_LOADED": str(self.loaded)}

    def run_script(self, *args, **extra):
        return subprocess.run(["bash", str(SCRIPT), *args], env={**self.env, **extra},
                              capture_output=True, text=True, timeout=30)

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def target(self):
        return self.profiles / TARGET

    def test_apply_is_idempotent_and_remove_unloads_before_deleting(self):
        first = self.run_script()
        self.assertEqual(first.returncode, 0, first.stderr)
        body = self.target().read_bytes()
        self.assertTrue(body.startswith(b"# Installed by Lodestone"))
        self.assertIn(b"profile unpriv_bwrap", body)
        self.assertIn(b"audit deny capability,", body)
        again = self.run_script()
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(self.target().read_bytes(), body)
        removed = self.run_script("--remove")
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertFalse(self.target().exists())
        self.assertEqual([c.split()[0] for c in self.calls()], ["-a", "-r", "-R"])
        self.assertEqual(self.run_script("--remove").returncode, 0)
        self.assertEqual(sorted(p.name for p in self.profiles.iterdir()), [])

    def test_failed_unload_keeps_the_file_and_fails(self):
        self.assertEqual(self.run_script().returncode, 0)
        result = self.run_script("--remove", UNLOAD_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.target().is_file())

    def test_failed_load_leaves_no_profile_file(self):
        result = self.run_script(LOAD_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(sorted(p.name for p in self.profiles.iterdir()), [])

    def test_failed_reload_of_an_existing_owned_file_keeps_it_and_says_so(self):
        self.assertEqual(self.run_script().returncode, 0)
        body = self.target().read_bytes()
        result = self.run_script(LOAD_FAIL="1")
        self.assertEqual(result.returncode, 5)
        self.assertIn("is kept as it was", result.stderr)
        self.assertEqual(self.target().read_bytes(), body)

    def test_unknown_loaded_state_changes_nothing_on_apply(self):
        for broken in ("missing", "unreadable"):
            with self.subTest(broken=broken):
                self.loaded.unlink(missing_ok=True)
                if broken == "unreadable":
                    self.loaded.mkdir()  # reading a directory fails
                result = self.run_script()
                self.assertEqual(result.returncode, 6)
                self.assertEqual(sorted(p.name for p in self.profiles.iterdir()), [])
                self.assertEqual(self.calls(), [])
                if self.loaded.is_dir():
                    self.loaded.rmdir()

    def test_unknown_loaded_state_on_remove_still_requires_the_unload(self):
        self.assertEqual(self.run_script().returncode, 0)
        self.loaded.unlink()
        self.loaded.mkdir()
        kept = self.run_script("--remove", UNLOAD_FAIL="1")
        self.assertEqual(kept.returncode, 5)
        self.assertTrue(self.target().is_file())
        removed = self.run_script("--remove", FAKE_LOADED=str(self.base / "scratch-loaded"))
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertFalse(self.target().exists())
        self.assertEqual([c.split()[0] for c in self.calls()], ["-a", "-R", "-R"])

    def test_preexisting_unmanaged_file_or_symlink_is_left_untouched(self):
        self.target().write_text("site policy\n")
        for args in ((), ("--remove",)):
            result = self.run_script(*args)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(self.target().read_text(), "site policy\n")
        self.target().unlink()
        elsewhere = self.base / "elsewhere"
        elsewhere.write_text("keep\n")
        self.target().symlink_to(elsewhere)
        for args in ((), ("--remove",)):
            self.assertNotEqual(self.run_script(*args).returncode, 0)
            self.assertTrue(self.target().is_symlink())
        self.assertEqual(elsewhere.read_text(), "keep\n")
        self.assertEqual(self.calls(), [])

    def test_independent_bwrap_profile_is_not_modified(self):
        (self.profiles / "bwrap-userns-restrict").write_text("distribution copy\n")
        result = self.run_script()
        self.assertEqual(result.returncode, 6)
        self.assertFalse(self.target().exists())
        (self.profiles / "bwrap-userns-restrict").unlink()
        self.loaded.write_text("bwrap (enforce)\n")
        self.assertEqual(self.run_script().returncode, 6)
        self.assertEqual(self.calls(), [])

    def test_invalid_arguments_change_nothing(self):
        for args in (("--rmove",), ("--remove", "extra"), ("",)):
            self.assertEqual(self.run_script(*args).returncode, 2)
        self.assertEqual(sorted(p.name for p in self.profiles.iterdir()), [])
        self.assertEqual(self.calls(), [])

    def test_root_is_required_without_the_test_hook(self):
        if os.geteuid() == 0:
            self.skipTest("running as root")
        result = self.run_script(LODESTONE_FENCE_TEST_NOT_ROOT="1")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
