"""Pure configuration tests: no service, host files, models or research data."""
import ast
import copy
import hashlib
import inspect
from pathlib import Path
import plistlib
import unittest
from unittest.mock import patch

from lodestone_nurarihyon import startup


INTENT = {
    "runtime": Path("/Users/fixture/Library/Application Support/Lodestone/nurarihyon/releases/pinned"),
    "state": Path("/Users/fixture/Documents/Lodestone/nurarihyon-state"),
    "log_file": Path("/Users/fixture/Library/Logs/Lodestone/nurarihyon/events.jsonl"),
    "code_sha": "a" * 64,
    "environment_sha": "b" * 64,
    "python": Path("/usr/local/bin/python3"),
}


def xml(value):
    return plistlib.dumps(value, fmt=plistlib.FMT_XML, sort_keys=True)


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.raw = startup.render_agent(**INTENT)
        self.agent = plistlib.loads(self.raw)

    def test_exact_gui_login_single_process_contract(self):
        self.assertEqual(set(self.agent), {"Label", "ProgramArguments", "WorkingDirectory",
            "EnvironmentVariables", "LimitLoadToSessionType", "RunAtLoad", "KeepAlive",
            "ProcessType", "ThrottleInterval", "ExitTimeOut", "StandardOutPath", "StandardErrorPath"})
        self.assertEqual(self.agent["Label"], "com.lodestone.nurarihyon")
        self.assertEqual(self.agent["LimitLoadToSessionType"], "Aqua")
        self.assertIs(self.agent["RunAtLoad"], True)
        self.assertIs(self.agent["KeepAlive"], False)
        self.assertEqual(self.agent["ProcessType"], "Standard")
        self.assertEqual(self.agent["ThrottleInterval"], 60)
        self.assertEqual(self.agent["ExitTimeOut"], 45)
        self.assertEqual(self.agent["StandardOutPath"], "/dev/null")
        self.assertEqual(self.agent["StandardErrorPath"], "/dev/null")
        self.assertEqual(self.agent["EnvironmentVariables"], {"PATH": "/usr/bin:/bin",
            "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1"})

    def test_spaces_are_one_argument_and_code_pin_is_required(self):
        self.assertEqual(self.agent["ProgramArguments"], [str(INTENT["python"]), "-B",
            str(INTENT["runtime"]) + "/tools/nurarihyon/run", "--home", str(INTENT["state"]),
            "daemon", "--log-file", str(INTENT["log_file"]), "--expected-code-sha", "a" * 64,
            "--expected-environment-sha", "b" * 64])
        self.assertEqual(self.agent["WorkingDirectory"], str(INTENT["runtime"]))

    def test_deterministic_bytes_and_independent_return(self):
        self.assertEqual(self.raw, startup.render_agent(**INTENT))
        value = startup.validate_agent(self.raw, **INTENT)
        value["ProgramArguments"].append("untrusted")
        self.assertEqual(startup.validate_agent(self.raw, **INTENT), self.agent)

    def test_no_filesystem_inspection_or_creation_required(self):
        def fail(*args, **kwargs):
            raise AssertionError("Pure preparation must not touch filesystem")
        with patch.object(Path, "exists", fail), patch.object(Path, "resolve", fail), \
                patch.object(Path, "stat", fail), patch.object(Path, "open", fail), \
                patch("builtins.open", fail):
            raw = startup.render_agent(**INTENT)
            manifest = startup.preparation_manifest(raw, **INTENT)
            startup.validate_manifest(manifest, raw, **INTENT)

    def test_lexical_path_aliases_control_characters_and_expansion_refuse(self):
        bad = ["relative/path", "~/state", "/", "//Users/fixture/state", "/Users//fixture/state",
               "/Users/./fixture/state", "/Users/fixture/../state", "/Users/fixture/state/",
               "/Users/fixture/~", "/Users/fixture/$HOME", "/Users/fixture/`id`",
               "/Users/fixture/line\nfeed", "/Users/fixture/\x00null", "/Users/fixture/\x7fdel",
               "/Users/fixture/\u202ehidden", "/Users/fixture/\ud800invalid", "/" + "x" * 4097]
        for key in ("runtime", "state", "log_file", "python"):
            for value in bad:
                with self.subTest(key=key, value=repr(value)), self.assertRaises(ValueError):
                    startup.render_agent(**{**INTENT, key: value})

    def test_shell_interpreters_and_wrong_types_refuse(self):
        for value in ("/bin/sh", "/bin/zsh", "/usr/bin/env", "/usr/bin/python", None, 3, b"/bin/python3"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                startup.render_agent(**{**INTENT, "python": value})
        self.assertEqual(plistlib.loads(startup.render_agent(**{**INTENT, "python": "/usr/bin/python3.12"}))["ProgramArguments"][0],
                         "/usr/bin/python3.12")

    def test_overlapping_runtime_state_and_logs_refuse(self):
        for changes in ({"state": INTENT["runtime"]}, {"state": INTENT["runtime"] / "state"},
                        {"runtime": INTENT["state"] / "runtime"}, {"log_file": INTENT["state"] / "events.jsonl"},
                        {"log_file": INTENT["runtime"] / "events.jsonl"}, {"log_file": "/Users"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                startup.render_agent(**{**INTENT, **changes})

    def test_only_pinned_code_and_dedicated_label_supported(self):
        for key in ("code_sha", "environment_sha"):
            for code in ("A" * 64, "a" * 63, "g" * 64, "a" * 64 + "\n", None, 1):
                with self.subTest(key=key, code=code), self.assertRaises(ValueError):
                    startup.render_agent(**{**INTENT, key: code})
        for label in ("com.lodestone.mailbox", "", "com.lodestone.nurarihyon\n", None):
            with self.subTest(label=label), self.assertRaises(ValueError):
                startup.render_agent(**INTENT, label=label)

    def test_automatic_retry_security_and_remote_fields_refuse(self):
        extras = {"StartInterval": 60, "StartCalendarInterval": {"Minute": 1}, "Sockets": {},
                  "MachServices": {"remote": True}, "UserName": "root", "GroupName": "wheel",
                  "RootDirectory": "/", "SessionCreate": True, "Disabled": False,
                  "Program": "/bin/sh", "WatchPaths": ["/tmp"], "QueueDirectories": ["/tmp"],
                  "AbandonProcessGroup": True, "SoftResourceLimits": {}, "NetworkState": True}
        for key, value in extras.items():
            with self.subTest(key=key), self.assertRaises(ValueError):
                startup.validate_agent(xml({**self.agent, key: value}), **INTENT)
        for value in (True, {"SuccessfulExit": False}, {"NetworkState": True}):
            with self.subTest(keepalive=value), self.assertRaises(ValueError):
                startup.validate_agent(xml({**self.agent, "KeepAlive": value}), **INTENT)

    def test_arguments_environment_and_values_must_match_intent(self):
        for key in self.agent:
            value = copy.deepcopy(self.agent)
            del value[key]
            with self.subTest(missing=key), self.assertRaises(ValueError):
                startup.validate_agent(xml(value), **INTENT)
        mutations = [
            {"ProgramArguments": ["/bin/sh", "-c", "anything"]},
            {"ProgramArguments": self.agent["ProgramArguments"] + ["--retry"]},
            {"EnvironmentVariables": {**self.agent["EnvironmentVariables"], "API_KEY": "fixture"}},
            {"EnvironmentVariables": {**self.agent["EnvironmentVariables"], "PYTHONPATH": "/tmp"}},
            {"LimitLoadToSessionType": "System"}, {"ProcessType": "Background"},
            {"StandardOutPath": str(INTENT["log_file"])}, {"ExitTimeOut": 60},
            {"WorkingDirectory": "/tmp"}]
        for changes in mutations:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                startup.validate_agent(xml({**self.agent, **changes}), **INTENT)
        with self.assertRaises(ValueError):
            startup.validate_agent(self.raw, **{**INTENT, "code_sha": "b" * 64})
        with self.assertRaises(ValueError):
            startup.validate_agent(self.raw, **{**INTENT, "environment_sha": "a" * 64})

    def test_plist_type_confusion_refuses(self):
        for key, value in (("KeepAlive", 0), ("RunAtLoad", 1), ("ThrottleInterval", 60.0),
                           ("ExitTimeOut", "45"), ("LimitLoadToSessionType", ["Aqua"])):
            with self.subTest(key=key), self.assertRaises(ValueError):
                startup.validate_agent(xml({**self.agent, key: value}), **INTENT)

    def test_duplicate_keys_malformed_binary_and_noncanonical_bytes_refuse(self):
        duplicate = self.raw.replace(b"<key>KeepAlive</key>", b"<key>KeepAlive</key><false/><key>KeepAlive</key>")
        nested_duplicate = self.raw.replace(b"<key>PATH</key>", b"<key>PATH</key><string>/usr/bin:/bin</string><key>PATH</key>")
        invalid = [duplicate, nested_duplicate, b"", self.raw.decode(), b"x" * (startup.MAX_PLIST_BYTES + 1),
                   self.raw + b"\n", self.raw.replace(b"<false/>", b"<unknown/><false/>", 1),
                   plistlib.dumps(self.agent, fmt=plistlib.FMT_BINARY), b"<plist><dict>", xml([])]
        for raw in invalid:
            with self.subTest(kind=type(raw), length=len(raw)), self.assertRaises(ValueError):
                startup.validate_agent(raw, **INTENT)

    def test_manifest_is_bound_preparation_only(self):
        manifest = startup.preparation_manifest(self.raw, **INTENT)
        self.assertEqual(manifest["status"], "PREPARED_NOT_INSTALLED")
        self.assertEqual(manifest["plist"], {"bytes": len(self.raw), "sha256": hashlib.sha256(self.raw).hexdigest()})
        self.assertIs(manifest["automatic_restart"], False)
        self.assertIs(manifest["research_or_remote_actions"], False)
        self.assertEqual(manifest["expected_environment_sha256"], "b" * 64)
        self.assertIn("unverified", manifest["path_validation"])
        self.assertEqual(startup.validate_manifest(manifest, self.raw, **INTENT), manifest)
        for changes in ({"status": "INSTALLED"}, {"automatic_restart": 0}, {"unbound": True},
                        {"expected_code_sha256": "b" * 64}, {"expected_environment_sha256": "a" * 64},
                        {"plist": {"bytes": len(self.raw), "sha256": "b" * 64}}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                startup.validate_manifest({**manifest, **changes}, self.raw, **INTENT)

    def test_module_has_no_installer_or_execution_dependencies(self):
        tree = ast.parse(inspect.getsource(startup))
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.add(node.module)
        self.assertEqual(imports, {"hashlib", "pathlib", "plistlib", "re", "unicodedata"})
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        self.assertFalse(names & {"open", "exec", "eval", "compile", "__import__", "print", "input"})


if __name__ == "__main__":
    unittest.main()
