"""Provider isolation labels plus a harmless macOS sandbox startup diagnostic.

The startup test launches only this test's synthetic program, with no model or
network call. Production provider stderr remains undisclosed.
"""
import contextlib
from copy import deepcopy
import importlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

origami = importlib.import_module("lodestone_machinery.origami")
cli = importlib.import_module("lodestone_machinery.origami_cli")


class ProviderPortabilityTests(unittest.TestCase):
    @staticmethod
    def _synthetic_crash_report(pid, started):
        """Read only recent small macOS crash reports matching this test's PID."""
        directory = Path.home() / "Library/Logs/DiagnosticReports"
        for _ in range(10):
            try:
                candidates = sorted((p for p in directory.glob("*.ips")
                                     if p.name.lower().startswith(("python", "sandbox-exec"))),
                                    key=lambda p: p.stat().st_mtime,
                                    reverse=True)[:20]
                for path in candidates:
                    if path.stat().st_mtime < started - 1 or path.stat().st_size > 2 * 1024 * 1024:
                        continue
                    # macOS .ips files have a JSON metadata line followed by a
                    # separate JSON crash body. Never emit a whole crash report.
                    try:
                        with path.open("r", encoding="utf-8") as stream:
                            raw = stream.read(2 * 1024 * 1024 + 1)
                        if len(raw) > 2 * 1024 * 1024:
                            continue
                        header, end = json.JSONDecoder().raw_decode(raw)
                        report = json.loads(raw[end:].strip()) if raw[end:].strip() else header
                    except (OSError, ValueError):
                        continue
                    if report.get("pid") == pid:
                        selected = {key: report[key] for key in
                                    ("pid", "procName", "osVersion", "exception", "termination", "asi", "ktriageinfo")
                                    if key in report}
                        threads = report.get("threads", [])
                        fault = report.get("faultingThread", 0)
                        if isinstance(fault, int) and 0 <= fault < len(threads):
                            selected["faulting_symbols"] = [frame.get("symbol") for frame in
                                                            threads[fault].get("frames", [])[:15]]
                        return selected
            except (OSError, ValueError):
                pass
            time.sleep(0.3)
        return {"status": "No matching bounded crash report available"}

    @staticmethod
    def _synthetic_denial_messages(pid, workspace):
        """Read only system messages naming this synthetic PID and a denial."""
        predicate = (f'eventMessage CONTAINS "({pid})" AND '
                     '(eventMessage CONTAINS "deny" OR eventMessage CONTAINS "ignition")')
        try:
            with tempfile.TemporaryFile(dir=workspace) as output:
                result = subprocess.run(["/usr/bin/log", "show", "--last", "1m", "--style", "compact",
                                         "--info", "--debug", "--predicate", predicate],
                                        stdout=output, stderr=subprocess.PIPE, timeout=10)
                output.seek(0)
                messages = output.read(8193)
            return {"returncode": result.returncode,
                    "messages": messages[:8192].decode("utf-8", "replace"),
                    "truncated": len(messages) > 8192,
                    "stderr": result.stderr[:1024].decode("utf-8", "replace")}
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"status": type(exc).__name__}

    @unittest.skipUnless(sys.platform == "darwin", "macOS kernel sandbox startup")
    def test_sandboxed_python_startup_preserves_diagnostic_stderr(self):
        sandbox = origami._sandbox()
        if sandbox is None:
            self.skipTest("sandbox-exec is unavailable on this macOS host")
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            workspace = base / "workspace"; workspace.mkdir()
            source = base / "sources"; source.mkdir()
            unlisted = base / "not-readable.txt"
            unlisted.write_text("synthetic sibling outside allowed roots", encoding="utf-8")
            program = base / "synthetic_startup.py"
            program.write_text(
                "import json, os, socket, subprocess, threading\n"
                "root = os.open('/', os.O_RDONLY)\n"
                "os.close(root)\n"
                "denied = False\n"
                "try:\n"
                f"    with open({str(unlisted)!r}, 'rb') as stream: stream.read(1)\n"
                "except PermissionError:\n"
                "    denied = True\n"
                "print(json.dumps({'startup': 'PASS', 'unlisted_file_denied': denied}))\n",
                encoding="utf-8")
            argv = [sys.executable, "-B", str(program)]
            readable = origami._program_paths(argv)
            profile = origami._profile(workspace, source, readable, None, None)
            env = {key: os.environ[key] for key in ("PATH", "HOME", *origami.LOCALE_KEYS)
                   if key in os.environ}
            env.setdefault("PATH", "/usr/bin:/bin")
            started = time.time()
            with subprocess.Popen([sandbox, "-p", profile, *argv], cwd=workspace,
                                  env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True) as process:
                try:
                    stdout, stderr = process.communicate(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    stdout, stderr = process.communicate()
                    self.fail("Synthetic sandbox startup timed out: " + stderr[:8192])
                pid, returncode = process.pid, process.returncode
            # Only the synthetic test program's stderr is exposed. Do not apply
            # this diagnostic to user-selected providers or print environment data.
            diagnostic = {"python": sys.executable,
                          "resolved_python": str(Path(sys.executable).resolve()),
                          "prefix": sys.prefix, "base_prefix": sys.base_prefix,
                          "readable": [str(path) for path in readable],
                          "pid": pid, "returncode": returncode, "stderr": stderr[:8192]}
            if returncode:
                diagnostic["crash"] = self._synthetic_crash_report(pid, started)
                diagnostic["pid_filtered_denial_log"] = self._synthetic_denial_messages(pid, workspace)
                # Both controls are known test programs. The production sandbox
                # is unchanged, and no user-selected provider is run unconfined.
                for label, control in (("synthetic_python_without_sandbox", argv),
                                       ("true_with_same_sandbox", [sandbox, "-p", profile, "/usr/bin/true"])):
                    outcome = subprocess.run(control, cwd=workspace, env=env, capture_output=True,
                                             text=True, timeout=20)
                    diagnostic[label] = {"returncode": outcome.returncode,
                                         "stdout": outcome.stdout[:1024], "stderr": outcome.stderr[:8192]}
            self.assertEqual(returncode, 0, json.dumps(diagnostic, indent=2))
            self.assertEqual(json.loads(stdout), {"startup": "PASS", "unlisted_file_denied": True})

    def test_loader_root_read_is_literal_and_other_restrictions_remain(self):
        profile = origami._profile(Path("/workspace"), Path("/sources"), [], None, None)
        self.assertIn('(allow file-read-data (literal "/"))', profile)
        self.assertNotIn('(subpath "/")', profile)
        for rule in ("(deny file-read*)", "(deny file-write*)", "(deny network*)", "(deny process-fork)"):
            self.assertIn(rule, profile)

    def test_unsandboxed_environment_scrubbing_does_not_claim_file_isolation(self):
        for endpoints in ((), ("model.example:443",)):
            with self.subTest(endpoints=endpoints):
                observed = origami._constraints("command", False, endpoints, "/example/auth")
                self.assertEqual(observed["environment_allowlist"], "ENFORCED")
                for name in ("no_lab_credentials", "provider_auth_path", "no_network",
                             "read_only_source_root", "workspace_only_writes",
                             "no_further_subprocesses"):
                    self.assertEqual(observed[name], "DECLARED", name)
                self.assertEqual(observed["launcher"], "subprocess")

    def test_available_sandbox_keeps_kernel_and_endpoint_statuses(self):
        denied = origami._constraints("command", True)
        self.assertEqual(denied["no_network"], "ENFORCED")
        self.assertEqual(denied["no_lab_credentials"], "ENFORCED")
        self.assertEqual(denied["environment_allowlist"], "ENFORCED")
        proxied = origami._constraints("command", True, ("model.example:443",))
        self.assertEqual(proxied["no_network"], "ENFORCED_ENDPOINT_ONLY")
        self.assertEqual(proxied["allowed_endpoints"], ["model.example:443"])
        self.assertEqual(proxied["launcher"], "sandbox-exec")

    def test_unlaunched_transports_have_no_environment_allowlist(self):
        for transport in ("callable", "external_exchange"):
            observed = origami._constraints(transport)
            self.assertEqual(observed["environment_allowlist"], "ABSENT")
            self.assertEqual(observed["no_lab_credentials"], "ABSENT")

    def test_legacy_record_does_not_acquire_a_new_enforcement_claim(self):
        legacy = origami._constraints("command", True)
        del legacy["environment_allowlist"]
        current = origami._constraints("command", False)
        calls = [{"constraints": legacy}, {"constraints": current}]
        before = deepcopy(calls)
        result = origami._aggregate(calls)
        self.assertEqual(result["environment_allowlist"], "ABSENT")
        self.assertEqual(result["no_lab_credentials"], "DECLARED")
        self.assertEqual(calls, before)

    def test_no_provider_calls_claim_no_environment_enforcement(self):
        self.assertEqual(origami._aggregate([])["environment_allowlist"], "ABSENT")

    def test_non_macos_platform_does_not_discover_a_kernel_sandbox(self):
        with patch.object(origami.sys, "platform", "linux"), patch.object(origami.shutil, "which") as which:
            self.assertIsNone(origami._sandbox())
        which.assert_not_called()

    def test_command_environment_is_allowlisted_but_keeps_home(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary).resolve()
            organ = origami.Origami(workspace)
            command = origami.ProviderCommand(["example-provider"])

            def fake_run(argv, **kwargs):
                self.assertEqual(argv, ["example-provider"])
                self.assertEqual(kwargs["env"], {"PATH": "/usr/bin:/bin", "HOME": str(workspace), "LANG": "C"})
                self.assertEqual(kwargs["cwd"], str(workspace))
                kwargs["stdout"].write(json.dumps({"mock": True}).encode())
                return SimpleNamespace(returncode=0)

            env = {"PATH": "/usr/bin:/bin", "HOME": str(workspace), "LANG": "C",
                   "SYNTHETIC_PROVIDER_SECRET": "test-only-not-inherited"}
            with patch.dict(os.environ, env, clear=True), patch.object(origami.subprocess, "run", side_effect=fake_run) as run:
                result = organ._launch(command, {"request": "mock"}, None, workspace, (), None)
            self.assertEqual(result, {"mock": True})
            run.assert_called_once()

    def test_sandbox_launch_profile_is_retained_without_executing_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary).resolve()
            organ = origami.Origami(workspace)

            def fake_run(argv, **kwargs):
                self.assertEqual(argv[:2], ["/example/sandbox-exec", "-p"])
                self.assertIn("(deny network*)", argv[2])
                self.assertIn("(deny file-read*)", argv[2])
                self.assertEqual(argv[3:], ["example-provider"])
                kwargs["stdout"].write(b"{}")
                return SimpleNamespace(returncode=0)

            with patch.object(origami, "_program_paths", return_value=[]), patch.object(origami.subprocess, "run", side_effect=fake_run):
                self.assertEqual(organ._launch(origami.ProviderCommand(["example-provider"]), {},
                                              "/example/sandbox-exec", workspace, (), None), {})

    def test_cli_help_qualifies_network_and_filesystem_restrictions(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as stopped:
            cli.main(["ask", "--help"])
        self.assertEqual(stopped.exception.code, 0)
        text = " ".join(output.getvalue().split())
        self.assertIn("network restriction requires a kernel sandbox", text)
        self.assertIn("no filesystem isolation without a kernel sandbox", text)


if __name__ == "__main__":
    unittest.main()
