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
from types import SimpleNamespace
import unittest
from unittest.mock import patch

origami = importlib.import_module("lodestone_machinery.origami")
cli = importlib.import_module("lodestone_machinery.origami_cli")


class ProviderPortabilityTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "darwin", "macOS kernel sandbox startup")
    def test_sandboxed_python_startup_preserves_diagnostic_stderr(self):
        sandbox = origami._sandbox()
        if sandbox is None:
            self.skipTest("sandbox-exec is unavailable on this macOS host")
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            workspace = base / "workspace"; workspace.mkdir()
            source = base / "sources"; source.mkdir()
            program = base / "synthetic_startup.py"
            program.write_text(
                "import json, os, socket, subprocess, threading\n"
                "print(json.dumps({'startup': 'PASS'}))\n", encoding="utf-8")
            argv = [sys.executable, "-B", str(program)]
            readable = origami._program_paths(argv)
            profile = origami._profile(workspace, source, readable, None, None)
            env = {key: os.environ[key] for key in ("PATH", "HOME", *origami.LOCALE_KEYS)
                   if key in os.environ}
            env.setdefault("PATH", "/usr/bin:/bin")
            result = subprocess.run([sandbox, "-p", profile, *argv], cwd=workspace,
                                    env=env, capture_output=True, text=True, timeout=20)
            # Only the synthetic test program's stderr is exposed. Do not apply
            # this diagnostic to user-selected providers or print environment data.
            diagnostic = json.dumps({"python": sys.executable,
                                     "resolved_python": str(Path(sys.executable).resolve()),
                                     "prefix": sys.prefix, "base_prefix": sys.base_prefix,
                                     "readable": [str(path) for path in readable],
                                     "returncode": result.returncode,
                                     "stderr": result.stderr[:8192]}, indent=2)
            self.assertEqual(result.returncode, 0, diagnostic)
            self.assertEqual(json.loads(result.stdout), {"startup": "PASS"})

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
