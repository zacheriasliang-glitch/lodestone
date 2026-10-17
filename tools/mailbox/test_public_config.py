"""Public transport must never infer a private mailbox or create state on bad configuration."""
from pathlib import Path
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).with_name("mailbox.py")
SPEC = importlib.util.spec_from_file_location("public_mailbox", SCRIPT)
transport = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(transport)


class ExplicitConfigurationTests(unittest.TestCase):
    def test_cli_requires_remote_branch_and_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run([sys.executable, "-B", str(SCRIPT), "receive", "--side", "mac"],
                cwd=temporary, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            for option in ("--remote", "--branch", "--state"):
                self.assertIn(option, result.stderr)
            self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_missing_posix_lock_creates_no_state(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(transport, "fcntl", None):
            state = Path(temporary) / "state"
            with self.assertRaisesRegex(transport.MailboxError, "POSIX"):
                transport.Mailbox("local-remote.git", "example-mailbox", state)
            self.assertFalse(state.exists())

    def test_blank_configuration_creates_no_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            for remote, branch in (("", "example"), ("remote", "")):
                with self.assertRaises(transport.MailboxError):
                    transport.Mailbox(remote, branch, state)
                self.assertFalse(state.exists())


if __name__ == "__main__":
    unittest.main()
