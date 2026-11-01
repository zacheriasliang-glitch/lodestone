"""Public configuration and platform boundaries; no service installation."""
import importlib.util
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]


def module(path):
    spec = importlib.util.spec_from_file_location("_public_test_" + path.stem, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


class PublicBoundaryTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform in ("darwin", "linux"), "Nurarihyon requires macOS/Linux")
    def test_bound_conscience_refusal_blocks_worker_without_charging_attempt(self):
        helper = module(ROOT / "tools/nurarihyon/configure.py")
        fixture = module(ROOT / "examples/nurarihyon/prepare_sources.py")
        from lodestone_nurarihyon.controller import Controller
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            config, home = base / "config", base / "state"
            helper.configure(config, home, "reconciliation")
            custody_path = config / "conscience-policy.json"
            custody = json.loads(custody_path.read_text())
            custody["writers"][0]["owner"] = "another-owner"
            raw = json.dumps(custody).encode()
            custody_path.write_bytes(raw)
            policy = json.loads((config / "nurarihyon-policy.json").read_text())
            policy["conscience"]["policy"].update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
            controller = Controller.initialize(home, policy)
            source = base / "source.json"
            source.write_text(json.dumps(fixture.fixture()))
            task = controller.submit(source, key="refusal", toolkit="bezoar.reconciliation/v1")
            controller.approve(task["id"], task["plan_sha"])
            with mock.patch("lodestone_nurarihyon.controller.subprocess.Popen", side_effect=AssertionError("No worker may start")):
                result = controller.run_once()
            self.assertEqual(result["result"]["outcome"], "CUSTODY_HELD", result)
            self.assertEqual(result["result"]["verdict"], "REFUSE", result)
            self.assertEqual(result["attempts"], 0)
            self.assertEqual(controller.verify()["generations"], 1)

    def test_native_windows_routes_fail_before_posix_import_or_state_creation(self):
        cli = module(ROOT / "tools/ecosystem/cli.py")
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(cli.platform, "system", return_value="Windows"):
            home = Path(temporary) / "untouched"
            for command in ("nurarihyon", "pando"):
                with mock.patch.object(cli.subprocess, "run", side_effect=AssertionError("No child allowed")):
                    self.assertEqual(cli.main([command, "--home", str(home), "init"]), 2)
            self.assertFalse(home.exists())

    def test_duplicate_or_nonfinite_conscience_policy_cannot_be_pinned(self):
        conscience = module(ROOT / "tools/ecosystem/conscience.py")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "policy.json"
            for raw in ('{"schema":"a","schema":"b"}', '{"enotherm": NaN}', '{"value": 1e999}'):
                path.write_text(raw)
                with self.assertRaises(ValueError):
                    conscience.load_policy(path)

    @unittest.skipUnless(sys.platform in ("darwin", "linux"), "Nurarihyon requires macOS/Linux")
    def test_user_configuration_is_explicit_and_never_initializes_or_overwrites(self):
        helper = module(ROOT / "tools/nurarihyon/configure.py")
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            output, state = base / "config", base / "state"
            result = helper.configure(output, state, "all")
            self.assertEqual(result["status"], "PREPARED_NOT_INITIALIZED")
            self.assertEqual(len(result["toolkits"]), 5)
            self.assertFalse(state.exists())
            policy = json.loads((output / "nurarihyon-policy.json").read_text())
            self.assertEqual(policy["external_adapters"], False)
            self.assertEqual(policy["conscience"]["actor"], "local-user")
            saved = {p.name: p.read_bytes() for p in output.iterdir()}
            with self.assertRaises(ValueError):
                helper.configure(output, state, "all")
            self.assertEqual(saved, {p.name: p.read_bytes() for p in output.iterdir()})
            with self.assertRaises(ValueError):
                helper.configure(base / "nested", base / "nested/state", "all")
            self.assertFalse((base / "nested").exists())


if __name__ == "__main__":
    unittest.main()
