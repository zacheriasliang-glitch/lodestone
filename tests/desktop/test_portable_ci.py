"""Offline portability checks for the native CI driver; no installs or inference."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import desktop_portable_check as check


class PortableCITests(unittest.TestCase):
    def test_pull_requests_are_always_zero_inference_and_manual_requires_exact_head(self):
        commit = "a" * 40
        self.assertEqual(check.select_mode("pull_request", "", "", commit), "mechanical_only")
        self.assertEqual(check.select_mode("workflow_dispatch", "false", commit, commit), "mechanical_only")
        self.assertEqual(check.select_mode("workflow_dispatch", "true", commit, commit), "local_inference_smoke")
        for event, requested, expected in (("pull_request", "true", commit),
                ("workflow_dispatch", "true", "b" * 40), ("workflow_dispatch", "false", ""),
                ("push", "true", commit), ("workflow_dispatch", "yes", commit)):
            with self.subTest(event=event, requested=requested), self.assertRaises(ValueError):
                check.select_mode(event, requested, expected, commit)

    def test_extracted_download_preserves_bytes_and_has_no_git(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "download.zip"
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("Payload/Install Lodestone.sh", b"#!/bin/sh\n")
                output.writestr("Payload/runtime/sample.py", b"value = 1\n")
            extracted = check.extract_archive(archive, root / "extracted")
            self.assertEqual((extracted / "runtime/sample.py").read_bytes(), b"value = 1\n")
            self.assertFalse((extracted / ".git").exists())
            with self.assertRaisesRegex(ValueError, "must be new"):
                check.extract_archive(archive, root / "extracted")

    def test_extraction_rejects_traversal_ads_git_symlink_and_case_aliases(self):
        cases = [["../escaped"], ["Payload/file:stream"], ["Payload/.git/config"],
                 ["Payload/linked"], ["Payload/A", "Payload/a"]]
        for paths in cases:
            with self.subTest(paths=paths), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                archive = root / "bad.zip"
                with zipfile.ZipFile(archive, "w") as output:
                    for name in paths:
                        info = zipfile.ZipInfo(name)
                        if name.endswith("linked"):
                            info.external_attr = (stat.S_IFLNK | 0o777) << 16
                        output.writestr(info, b"outside")
                with self.assertRaises(ValueError):
                    check.extract_archive(archive, root / "extracted")
                self.assertFalse((root / "extracted").exists())

    def test_native_bootstrap_arguments_do_not_rely_on_git_or_shell_expansion(self):
        root = Path(tempfile.gettempdir()) / "portable fixture"
        win = check.bootstrap_command(root, "windows-x86_64", root / "user data", root / "app.cmd", root / "cache")
        self.assertEqual(win[:2], ["powershell.exe", "-NoProfile"])
        self.assertIn(str(root / "Install Lodestone.ps1"), win)
        self.assertEqual(win[-1], "-NoOpen")
        linux = check.bootstrap_command(root, "ubuntu-24.04-x86_64", root / "user data", root / "app.desktop", root / "cache")
        self.assertEqual(linux[:2], ["/bin/bash", str(root / "Install Lodestone.sh")])
        self.assertEqual(linux[-1], "--no-open")

    def test_linux_launch_checks_the_actual_desktop_command(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            app = root / "app.desktop"
            python = str(root / "python with spaces")
            app.write_text('[Desktop Entry]\nExec="' + python + '" -B "' + str(root / "launch.py") + '"\n')
            command, method = check.wrapper_command("ubuntu-24.04-x86_64", root, app, {"worker_python": python})
            self.assertEqual(command, [python, "-B", str(root / "launch.py"), "--verify-startup"])
            self.assertEqual(method, "desktop_exec_command")
            app.write_text("[Desktop Entry]\nExec=unrelated-program\n")
            with self.assertRaisesRegex(ValueError, "differs"):
                check.wrapper_command("ubuntu-24.04-x86_64", root, app, {"worker_python": python})

    def test_existing_recovery_tool_recovers_portable_log_envelope(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            native = root / "evidence/native/Loom/runs/fixture"
            native.mkdir(parents=True)
            raw = b'{"status":"INTERRUPTED","provider_called":false}\n'
            (native / "outcome.json").write_bytes(raw)
            captured = io.StringIO()
            with redirect_stdout(captured):
                check.emit(root)
            log = root / "job.log"
            log.write_text(captured.getvalue())
            result = check.evidence_format.recover(log, root / "recovered")
            self.assertEqual(result["status"], "RECOVERED_VERIFIED_BYTES")
            self.assertEqual((root / "recovered/native/Loom/runs/fixture/outcome.json").read_bytes(), raw)

    def test_workflow_keeps_free_public_standard_runners_and_no_storage_actions(self):
        workflow = (ROOT / ".github/workflows/desktop-portable.yml").read_text()
        self.assertIn("github.event.repository.private == false", workflow)
        for name in ("ubuntu-24.04", "windows-2022", "macos-15-intel"):
            self.assertIn("runner: " + name, workflow)
        self.assertIn("default: false", workflow)
        self.assertNotIn("actions/upload-artifact", workflow)
        self.assertNotIn("actions/cache", workflow)
        self.assertNotIn("pull_request_target", workflow)


if __name__ == "__main__":
    unittest.main()
