"""Offline portability checks for the native CI driver; no installs or inference."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch

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
                 ["Payload/linked"], ["Payload/A", "Payload/a"],
                 ["Payload\\hidden"], ["Payload/file\x00hidden"]]
        for paths in cases:
            with self.subTest(paths=paths), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                archive = root / "bad.zip"
                with zipfile.ZipFile(archive, "w") as output:
                    for name in paths:
                        info = zipfile.ZipInfo("fixture")
                        info.filename = name  # Keep the literal stored name on Windows too.
                        if name.endswith("linked"):
                            info.external_attr = (stat.S_IFLNK | 0o777) << 16
                        output.writestr(info, b"outside")
                with zipfile.ZipFile(archive) as output:
                    self.assertEqual([item.orig_filename for item in output.infolist()], paths)
                with self.assertRaises(ValueError):
                    check.extract_archive(archive, root / "extracted")
                self.assertFalse((root / "extracted").exists())

    def test_native_bootstrap_arguments_do_not_rely_on_git_or_shell_expansion(self):
        root = Path(tempfile.gettempdir()) / "portable fixture"
        win = check.bootstrap_command(root, "windows-x86_64", root / "user data", root / "app.cmd", root / "cache")
        self.assertIsInstance(win, str)
        self.assertIn(' /d /v:off /s /c ""' + str(root / "Install Lodestone.cmd") + '"', win)
        self.assertIn('"--root" "' + str(root / "user data") + '"', win)
        self.assertTrue(win.endswith('"--no-open""'))
        self.assertNotIn("-ExecutionPolicy", win)
        self.assertNotIn("powershell", win.lower())
        linux = check.bootstrap_command(root, "ubuntu-24.04-x86_64", root / "user data", root / "app.desktop", root / "cache")
        self.assertEqual(linux[:2], ["/bin/bash", str(root / "Install Lodestone.sh")])
        self.assertEqual(linux[-1], "--no-open")

    @unittest.skipUnless(sys.platform == "win32", "Native Windows cmd argument grammar")
    def test_windows_batch_command_runs_unicode_space_path_with_literal_arguments(self):
        with tempfile.TemporaryDirectory(prefix="Lodestone ü ") as temporary:
            script = Path(temporary) / "fixture ü.cmd"
            script.write_bytes(b"@echo off\r\necho %~1\r\nexit /b 0\r\n")
            command = check.windows_batch_command(script, ["--verify-startup"])
            result = subprocess.run(command, shell=False, capture_output=True, text=True, timeout=10, check=True)
            self.assertEqual(result.stdout.strip(), "--verify-startup")

    def test_windows_batch_command_rejects_shell_metacharacters(self):
        for value in ('a&b', 'a%PATH%b', 'a!b', 'a"b'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                check.windows_batch_command("fixture.cmd", [value])

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
        self.assertIn("uses: actions/setup-python@v5", workflow)
        self.assertIn("python-version: '3.12'", workflow)
        self.assertIn("PYTHONFAULTHANDLER: '1'", workflow)
        for name in ("test_dependencies.py", "test_build.py", "test_dependency_custody.py", "test_dependency_archives.py"):
            self.assertIn(name, check.SOURCE_TESTS)
        for name in ("test_learning_archive_custody.py", "test_decision_retention.py", "test_practice_selection.py",
                     "test_practice_voxels.py", "test_reader_history_window.py"):
            self.assertIn(name, check.SOURCE_TESTS)

    def test_source_checks_collect_independent_failures_before_refusing_install(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "tests/desktop").mkdir(parents=True)
            for name in ("test_a.py", "test_b.py"):
                (root / "tests/desktop" / name).write_text("# authored fixture")
            driver = check.Check(root, root / "work", "macos-x86_64")
            with patch.object(check, "SOURCE_TESTS", ("test_a.py", "test_b.py")), \
                    patch.object(driver, "run", side_effect=[None, subprocess.CalledProcessError(1, "fixture"), None]) as called:
                with self.assertRaisesRegex(ValueError, "source-test_a"):
                    driver.source_checks()
            self.assertEqual(called.call_count, 3)
            outcome = json.loads((driver.evidence / "source-checks.json").read_text())
            self.assertEqual(outcome["passed"], ["source-provenance", "source-test_b"])
            self.assertEqual(outcome["failed"][0]["check"], "source-test_a")

    def test_linux_gui_crash_keeps_original_failure_after_bounded_diagnostic(self):
        for diagnostic_error in (None, subprocess.TimeoutExpired("gdb", 120)):
            with self.subTest(diagnostic_error=diagnostic_error), tempfile.TemporaryDirectory() as temporary:
                driver = check.Check(temporary, Path(temporary) / "work", "ubuntu-24.04-x86_64")
                driver.active = {"gui_python": "/fixture/python", "release": "/fixture/payload"}
                original = subprocess.CalledProcessError(-11, "authored GUI fixture")
                with patch.object(driver, "previous_readers", return_value={}), \
                        patch.object(driver, "run", side_effect=[None, original, diagnostic_error]) as calls:
                    with self.assertRaises(subprocess.CalledProcessError) as raised:
                        driver.gui()
                self.assertIs(raised.exception, original)
                self.assertEqual(calls.call_count, 3)
                diagnostic = calls.call_args_list[-1]
                self.assertEqual(diagnostic.args[0], "gui-startup-native-diagnostic")
                command = diagnostic.args[1]
                self.assertEqual(command[0:2], ["gdb", "--batch"])
                self.assertEqual(command[command.index("--args") + 1:], calls.call_args_list[1].args[1])
                self.assertEqual(diagnostic.kwargs["timeout"], 120)

    def test_smoke_command_receipt_preserves_detailed_report_on_success_and_failure(self):
        for returncode in (0, 1):
            with self.subTest(returncode=returncode), tempfile.TemporaryDirectory() as temporary:
                driver = check.Check(temporary, Path(temporary) / "work", "windows-x86_64")
                driver.active = {"worker_python": sys.executable, "release": str(Path(temporary) / "payload")}
                detail = {"schema": "lodestone-desktop-local-smoke/v1", "status": "PASSED" if returncode == 0 else "FAILED",
                          "attempts": [{"authored_fixture": True}], "delivery": {"exact_correction_in_second_request": returncode == 0}}
                def authored(command, **kwargs):
                    Path(command[command.index("--output") + 1]).write_text(json.dumps(detail))
                    return subprocess.CompletedProcess(command, returncode)
                with patch.object(check.subprocess, "run", side_effect=authored), redirect_stdout(io.StringIO()):
                    if returncode:
                        with self.assertRaises(subprocess.CalledProcessError):
                            driver.local_smoke()
                    else:
                        driver.local_smoke()
                self.assertEqual(json.loads((driver.evidence / "local-smoke.json").read_text()), detail)
                command_receipt = json.loads((driver.evidence / "local-smoke-command.json").read_text())
                self.assertEqual(command_receipt["returncode"], returncode)
                self.assertEqual(command_receipt["status"], detail["status"])


if __name__ == "__main__":
    unittest.main()
