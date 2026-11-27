"""Read-only Windows prerequisite boundaries; real policy changes run only in CI."""
import copy
import hashlib
import importlib.util
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("windows_preflight", ROOT / "desktop/windows_preflight.py")
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)
LOCK = b'{"fixture":"path-boundaries"}'
HELPER = ROOT / "desktop/Enable Windows Long Paths.cmd"


def budget():
    return {"schema": preflight.SCHEMA, "lock_sha256": hashlib.sha256(LOCK).hexdigest(),
            "token_widths": {"hash": 64}, "scopes": {
                name: {"max_file_relative_utf16": 1, "max_directory_relative_utf16": 0,
                       "longest_file_template": "", "longest_directory_template": ""}
                for name in preflight.SCOPES}}


def evaluate(document, **kwargs):
    paths = dict(root=r"C:\r", cache=r"C:\c", payload=r"C:\p", app=r"C:\a.cmd",
                 policy={"enabled": False})
    paths.update(kwargs)
    return preflight.evaluate(document, **paths)


class PathBudgetTests(unittest.TestCase):
    def test_lock_and_rendered_inventory_must_match(self):
        document = budget()
        self.assertEqual(preflight.validate_budget(document, LOCK), document)
        with self.assertRaisesRegex(ValueError, "dependency lock"):
            preflight.validate_budget(document, b"other lock")
        document["scopes"]["root"].update(longest_file_template=r"\{hash}\x",
                                         max_file_relative_utf16=65)
        with self.assertRaisesRegex(ValueError, "understates"):
            preflight.validate_budget(document, LOCK)
        document["scopes"]["root"]["max_file_relative_utf16"] = 67
        preflight.validate_budget(document, LOCK)

    def test_missing_scope_and_boolean_bound_refuse(self):
        document = budget()
        del document["scopes"]["bootstrap_root"]
        with self.assertRaisesRegex(ValueError, "bootstrap_root"):
            preflight.validate_budget(document, LOCK)
        document = budget()
        document["scopes"]["root"]["max_file_relative_utf16"] = True
        with self.assertRaisesRegex(ValueError, "bound"):
            preflight.validate_budget(document, LOCK)

    def test_file_and_directory_limits_are_distinct_and_exact(self):
        document = budget()
        document["scopes"]["root"].update(max_file_relative_utf16=255,
                                         max_directory_relative_utf16=243)
        rows = evaluate(document)["paths"]
        self.assertEqual([row["remaining_utf16"] for row in rows[:2]], [0, 0])
        document["scopes"]["root"]["max_directory_relative_utf16"] += 1
        with self.assertRaisesRegex(ValueError, "root directory: 248"):
            evaluate(document)

    def test_non_bmp_names_count_two_utf16_units(self):
        self.assertEqual(preflight.utf16_units("λ😀"), 3)
        document = budget()
        document["scopes"]["root"]["max_file_relative_utf16"] = 254
        evaluate(document, root="C:\\😀")
        with self.assertRaisesRegex(ValueError, preflight.REFUSAL):
            evaluate(document, root="C:\\😀x")

    def test_policy_one_relaxes_only_managed_root(self):
        document = budget()
        document["scopes"]["root"]["max_file_relative_utf16"] = 260
        with self.assertRaisesRegex(ValueError, "Enable Windows Long Paths"):
            evaluate(document)
        evaluate(document, policy={"enabled": True})
        for scope in ("bootstrap_root", "cache", "payload"):
            selected = copy.deepcopy(document)
            selected["scopes"][scope]["max_file_relative_utf16"] = 260
            with self.subTest(scope=scope), self.assertRaisesRegex(ValueError, scope):
                evaluate(selected, policy={"enabled": True})

    def test_launcher_atomic_name_and_component_limits(self):
        document = budget()
        evaluate(document, app="C:\\" + "a" * 241 + ".cmd")
        with self.assertRaisesRegex(ValueError, "245"):
            evaluate(document, app="C:\\" + "a" * 243 + ".cmd")
        # Absolute path plus temporary filename punctuation/random suffix.
        with self.assertRaisesRegex(ValueError, "app file"):
            evaluate(document, app="C:\\folder\\" + "a" * 236 + ".cmd")
        with self.assertRaisesRegex(ValueError, "component"):
            evaluate(document, root="C:\\" + "a" * 256)

    def test_refusal_names_deepest_member_and_remaining_budget(self):
        document = budget()
        document["scopes"]["root"].update(max_file_relative_utf16=256,
                                         longest_file_template=r"\pinned\deepest.py")
        with self.assertRaisesRegex(ValueError, "deepest.py") as caught:
            evaluate(document)
        self.assertIn("-1 remaining", str(caught.exception))
        self.assertIn("before downloads", str(caught.exception))

    def test_readonly_policy_handles_absent_custom_and_unreadable(self):
        import types
        module = types.SimpleNamespace(HKEY_LOCAL_MACHINE=object(), KEY_READ=1, REG_DWORD=4)
        key = unittest.mock.MagicMock()
        module.OpenKey = unittest.mock.Mock(return_value=key)
        for value, kind, expected in ((1, 4, True), (0, 4, False), (3, 4, False), ("1", 1, False)):
            module.QueryValueEx = unittest.mock.Mock(return_value=(value, kind))
            with self.subTest(value=value, kind=kind), patch.dict("sys.modules", winreg=module):
                self.assertIs(preflight.long_path_policy()["enabled"], expected)
        for error, state in ((FileNotFoundError(), "ABSENT"), (PermissionError(), "UNREADABLE")):
            module.OpenKey.side_effect = error
            with patch.dict("sys.modules", winreg=module):
                self.assertEqual(preflight.long_path_policy()["state"], state)


class NativeSourceTests(unittest.TestCase):
    def test_helper_is_visible_explicit_narrow_and_crlf(self):
        raw = HELPER.read_bytes()
        self.assertEqual(raw.count(b"\r\n"), raw.count(b"\n"))
        source = raw.decode()
        self.assertNotRegex(source.lower(), r"-(?:executionpolicy|encodedcommand)\b|set-executionpolicy|invoke-expression")
        self.assertIn("Read-Host 'Type ENABLE", source)
        self.assertIn("LODESTONE_LONG_PATH_RECEIPT=", source)
        self.assertIn("LODESTONE_LONG_PATH_RESULT=", source)
        self.assertIn("previous_exists=$previous.exists", source)
        self.assertIn("previous_value=$previous.value", source)
        self.assertIn("4294967295", source)
        self.assertIn("reg.exe delete HKLM", source)
        self.assertIn("-Verb RunAs -Wait -PassThru", source)
        self.assertLess(source.index("Write-Record $receipt $record"), source.index("& $reg @arguments"))
        self.assertIn("$arguments=@('add','HKLM\\SYSTEM\\CurrentControlSet\\Control\\FileSystem','/v','LongPathsEnabled','/t','REG_DWORD','/d','1','/f')", source)

    def test_both_bootstraps_preflight_before_mutation(self):
        cmd = (ROOT / "desktop/Install Lodestone.cmd").read_text()
        ps = (ROOT / "desktop/Install Lodestone.ps1").read_text()
        self.assertLess(cmd.index("call :check_path_budget"), cmd.index('mkdir "%LODESTONE_SETUP_CACHE%"'))
        self.assertLess(ps.index("# END WINDOWS PATH PREFLIGHT"), ps.index("New-Item -ItemType Directory"))
        for source in (cmd, ps):
            self.assertIn("bootstrap_root", source)
            self.assertIn("longest_'+$kind+'_template", source)
            self.assertIn("LODESTONE_WINDOWS_PATH_TOO_LONG", source)
            self.assertNotRegex(source, r"(?i)\bpowershell(?:\.exe)?[^\n]* -File\b")
        # Keep cmd's logical command lines within cmd.exe's documented limit.
        for command in cmd.split("\n\n"):
            if " -Command " in command:
                self.assertLess(len(command), 8191)

    @unittest.skipUnless(os.name == "nt", "native Windows PowerShell parser")
    def test_native_powershell_parses_both_visible_inline_commands(self):
        powershell = str(Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe")
        for path in (HELPER, ROOT / "desktop/Install Lodestone.cmd"):
            body = path.read_text()
            commands = re.findall(r"-Command \^\n((?:.*\^\n)*.*)", body)
            self.assertTrue(commands)
            for index, chunk in enumerate(commands):
                lines = [line.strip().removesuffix("^").strip() for line in chunk.splitlines()]
                command = " ".join(line[1:-1] for line in lines)
                with tempfile.TemporaryDirectory() as temp:
                    source = Path(temp) / "visible-command.ps1"
                    source.write_text(command, encoding="utf-8-sig")
                    environment = dict(os.environ, LODESTONE_PARSE_SOURCE=str(source))
                    parsed = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-Command",
                        "$tokens=$null; $errors=$null; [Management.Automation.Language.Parser]::ParseFile("
                        "$env:LODESTONE_PARSE_SOURCE,[ref]$tokens,[ref]$errors) | Out-Null; "
                        "if ($errors.Count) { $errors | Format-List | Out-String | Write-Host; exit 2 }"],
                        env=environment, capture_output=True, text=True, timeout=30)
                    self.assertEqual(parsed.returncode, 0, (path.name, index, parsed.stdout, parsed.stderr))


if __name__ == "__main__":
    unittest.main()
