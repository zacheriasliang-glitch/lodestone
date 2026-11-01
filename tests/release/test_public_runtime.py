"""Public configuration and platform boundaries; no service installation."""
import importlib.util
import copy
from contextlib import redirect_stderr, redirect_stdout
import io
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
        neutral = json.loads((ROOT / "tools/ecosystem/conscience-policy.json").read_text())
        # Each sample is otherwise a valid public policy: ordinary JSON parsing
        # would silently accept the duplicate or nonfinite operational number.
        raw = json.dumps(neutral)
        invalid = [(raw[:-1] + ', "actor_default": "duplicate-user"}',
                    "Duplicate policy key: actor_default")]
        numeric = copy.deepcopy(neutral)
        numeric["enotherm"]["hot_load_per_core"] = "__NONFINITE__"
        for token in ("NaN", "Infinity", "-Infinity", "1e999"):
            invalid.append((json.dumps(numeric).replace('"__NONFINITE__"', token),
                            "Non-finite policy number"))
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "policy.json"
            for raw, error in invalid:
                with self.subTest(raw=raw):
                    path.write_text(raw)
                    with mock.patch.object(conscience.hashlib, "sha256") as pin_hash:
                        with self.assertRaisesRegex(ValueError, error):
                            conscience.load_policy(path)
                    pin_hash.assert_not_called()

    def test_valid_v1_policy_optional_pin_and_metadata_remain_compatible(self):
        conscience = module(ROOT / "tools/ecosystem/conscience.py")
        neutral = json.loads((ROOT / "tools/ecosystem/conscience-policy.json").read_text())
        self.assertNotIn("pinned", neutral["interpreter"])
        variants = [("neutral", neutral)]
        pinned = copy.deepcopy(neutral)
        pinned["interpreter"]["pinned"] = sys.executable
        variants.append(("optional interpreter pin", pinned))
        metadata = copy.deepcopy(pinned)
        metadata["local_documentation"] = {"purpose": "Synthetic caller-owned configuration", "revision": 1}
        variants.append(("optional metadata", metadata))
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            path, workspace = base / "policy.json", base / "workspace"
            for label, policy in variants:
                with self.subTest(policy=label):
                    raw = json.dumps(policy).encode("utf-8")
                    path.write_bytes(raw)
                    loaded = conscience.load_policy(path)
                    self.assertEqual({key: value for key, value in loaded.items() if key != "_pin"}, policy)
                    self.assertEqual(loaded["_pin"], {"path": str(path), "bytes": len(raw),
                                                     "sha256": hashlib.sha256(raw).hexdigest()})
                    # Reach the real CLI's dispatch boundary without executing a command.
                    with mock.patch.object(conscience, "run", return_value={"verdict": "ALLOW", "executed": False}) as dispatch:
                        with redirect_stdout(io.StringIO()):
                            result = conscience.main(["assess", "--policy", str(path), "--", "echo", "synthetic"],
                                                     workspace=workspace)
                    self.assertEqual(result, 0)
                    dispatch.assert_called_once()
                    self.assertEqual(dispatch.call_args.kwargs["policy"], loaded)
                    self.assertFalse(dispatch.call_args.kwargs["execute"])
                    self.assertFalse(workspace.exists())

    def test_malformed_operational_fields_refuse_before_pin_and_dispatch(self):
        conscience = module(ROOT / "tools/ecosystem/conscience.py")
        neutral = json.loads((ROOT / "tools/ecosystem/conscience-policy.json").read_text())
        invalid = [
            (("schedule", "budget_per_wake"), True),
            (("schedule", "budget_per_wake"), False),
            (("schedule", "budget_per_wake"), -1),
            (("schedule", "budget_per_wake"), 1.5),
            (("record_roots",), "synthetic-records"),
            (("writable_roots_by_actor",), ["local-user"]),
            (("writable_roots_by_actor",), {"local-user": "synthetic-output"}),
            (("writers",), {"owner": "local-user", "prefix": "synthetic-output"}),
            (("writers",), [{"prefix": "synthetic-output"}]),
            (("writers",), [{"owner": "local-user", "prefix": 7}]),
            (("writers",), [{"owner": "local-user"}]),
            (("interpreter",), None),
            (("interpreter",), "python3"),
            (("interpreter",), {}),
            (("interpreter", "isolation_flags"), "-B"),
            (("interpreter", "pinned"), ""),
            (("enotherm", "hot_free_bytes"), True),
            (("heavy_patterns",), [{"all": "copy"}]),
            (("phase_two", "action_relevant_fields"), "doctor_status"),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            path, workspace = base / "policy.json", base / "untouched"
            for keys, value in invalid:
                with self.subTest(field=".".join(keys), value=value):
                    policy = copy.deepcopy(neutral)
                    target = policy
                    for key in keys[:-1]:
                        target = target[key]
                    target[keys[-1]] = value
                    path.write_text(json.dumps(policy))
                    error = io.StringIO()
                    # A rejected policy must never acquire a content pin or reach execution.
                    with mock.patch.object(conscience.hashlib, "sha256") as pin_hash:
                        with mock.patch.object(conscience, "run") as dispatch:
                            with redirect_stderr(error):
                                result = conscience.main(["run", "--policy", str(path), "--", "echo", "must-not-run"],
                                                         workspace=workspace)
                    self.assertEqual(result, 2)
                    self.assertIn(keys[0], json.loads(error.getvalue())["error"])
                    pin_hash.assert_not_called()
                    dispatch.assert_not_called()
                    self.assertFalse(workspace.exists())

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
