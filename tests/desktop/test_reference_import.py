"""Explicit Desktop reference-memory selection; native fixtures, no models."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from test_bootstrap_learning import bootstrap, RUNTIME, ROOT, snapshot
import test_install as installer_fixtures


class ReferenceSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve()
        self.root = self.directory / "preview"

    def initialize(self):
        return bootstrap.initialize(self.root, RUNTIME, Path(sys.executable))

    def native(self, script, *args):
        result = subprocess.run([sys.executable, "-I", "-B", "-c",
            "import sys\nsys.path.insert(0, sys.argv[1])\n" + script,
            str(RUNTIME / "src"), *map(str, args)], capture_output=True, text=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_explicit_selection_reopen_reconcile_and_return_to_v1_preserve_records(self):
        initial = self.initialize()
        original = Path(initial["memory_workspace"])
        original_records = snapshot(original / "organs")
        active = Path(initial["active_config"])
        policy = Path(initial["origami_policy"])
        local_policy = json.loads(policy.read_text())
        local_policy["purpose"] = "The user's exact local purpose, never imported policy."
        policy.write_text(json.dumps(local_policy, indent=4) + "\n")
        preimages = (active.read_bytes(), policy.read_bytes())
        # The ordinary default is still a v1 import and provides an explicit
        # compatible selection for a later old-reader rollback.
        legacy = bootstrap.import_memory(original, self.root / "imports/v1", RUNTIME,
            Path(sys.executable), seed_handles=initial["seed_handles"])
        self.assertEqual(json.loads(Path(legacy["active_config"]).read_text())["schema"],
                         "lodestone-learning-active/v1")
        imported = bootstrap.import_memory(original, self.root / "imports/v2", RUNTIME,
            Path(sys.executable), seed_handles=initial["seed_handles"], history_references=True)
        self.assertEqual((active.read_bytes(), policy.read_bytes()), preimages)
        selected = bootstrap.select_import(self.root, Path(imported["destination"]), RUNTIME, Path(sys.executable))
        self.assertEqual(selected["status"], "SELECTED")
        self.assertEqual(json.loads(active.read_text())["schema"], "lodestone-learning-active/v2")
        selected_policy = json.loads(policy.read_text())
        self.assertEqual(selected_policy["schema"], "origami-learning-policy/v2")
        self.assertEqual(selected_policy["purpose"], local_policy["purpose"])
        transaction = Path(selected["transaction"])
        self.assertEqual((transaction / "before-ACTIVE.json").read_bytes(), preimages[0])
        self.assertEqual((transaction / "before-learning-policy.json").read_bytes(), preimages[1])
        self.assertEqual(self.initialize()["memory_workspace"], imported["memory_workspace"])
        self.assertEqual(bootstrap.reconcile(self.root, RUNTIME, Path(sys.executable))["provider_calls"], 0)
        self.assertEqual(bootstrap.validate_selection(self.root, RUNTIME, Path(sys.executable))["status"], "COMPATIBLE")
        self.assertEqual(bootstrap.select_import(self.root, Path(imported["destination"]), RUNTIME,
                                                Path(sys.executable))["status"], "UNCHANGED")
        # Resolve the configured native route without loading a model. An actual
        # explicit preparation is closed ABSTAINED; no provider is supplied.
        observed = self.native('''
from lodestone_machinery.loom_runtime import _native
from lodestone_machinery.learning_context import build_adapter_request, read_learning_policy
from lodestone_nurarihyon import learning
from pathlib import Path
import json
native = _native({'learning_config': sys.argv[2]})
prepared = learning.prepare(native.memory, native.seed_handles,
    encounter='Authored compatibility fixture.', purpose='Inspect reference-history selection.',
    reader='scripted no-provider preparation')
try:
    packet = build_adapter_request(native.memory, prepared, request_id=prepared['run_id'], context='',
        encounter=prepared['binding']['encounter'], purpose=prepared['binding']['purpose'], history_window=1)
    assert native.memory.history_references is True
    assert json.loads(packet['context'])['reader_history_window']['keep_latest_runs'] == 1
    try:
        read_learning_policy(Path(sys.argv[3]))
    except ValueError:
        origami_refused = True
    else:
        raise AssertionError('Non-windowed Origami must still refuse this policy')
finally:
    terminal = learning.finish(native.memory, prepared, response=None, status='ABSTAINED',
        observation={'reason': 'Scripted preparation only; no provider invoked.'})
    native.correct(terminal['outcome_handle'], correction='This was preparation only, not a model result.',
        author='Scripted compatibility reviewer', observation='The actual native fixture abstained.')
print(json.dumps({'origami_refused': origami_refused,
    'status': terminal['status'], 'pending': native.memory.pending()}))
''', active, self.root / "runtime")
        self.assertEqual(observed, {"origami_refused": True, "status": "ABSTAINED", "pending": []})
        changed = bootstrap.select_import(self.root, Path(legacy["destination"]), RUNTIME, Path(sys.executable))
        self.assertEqual(changed["status"], "SELECTED")
        self.assertEqual(json.loads(active.read_text())["schema"], "lodestone-learning-active/v1")
        self.assertNotIn("history_representation", json.loads(active.read_text()))
        self.assertNotIn("history_representation", json.loads(policy.read_text()))
        self.assertEqual(json.loads(policy.read_text())["purpose"], local_policy["purpose"])
        self.assertEqual(original_records, snapshot(original / "organs"))
        for name, digest in original_records.items():
            self.assertEqual(snapshot(Path(imported["memory_workspace"]) / "organs")[name], digest)

    def test_missing_marker_or_mismatched_receipt_refuses_without_selecting(self):
        initial = self.initialize()
        imported = bootstrap.import_memory(Path(initial["memory_workspace"]), self.root / "imports/v2", RUNTIME,
            Path(sys.executable), seed_handles=initial["seed_handles"], history_references=True)
        active = Path(initial["active_config"]); policy = Path(initial["origami_policy"])
        expected = (active.read_bytes(), policy.read_bytes())
        marker = Path(imported["memory_workspace"]) / "learning-history-format.json"
        original_marker = marker.read_bytes(); marker.unlink()
        with self.assertRaisesRegex(ValueError, "isolated imported workspace"):
            bootstrap.select_import(self.root, Path(imported["destination"]), RUNTIME, Path(sys.executable))
        self.assertEqual((active.read_bytes(), policy.read_bytes()), expected)
        marker.write_bytes(original_marker)
        receipt_file = Path(imported["import_receipt"])
        receipt = json.loads(receipt_file.read_text()); receipt["history_representation"] = "unsupported/v99"
        receipt_file.write_text(json.dumps(receipt))
        with self.assertRaisesRegex(ValueError, "differs from its receipt"):
            bootstrap.select_import(self.root, Path(imported["destination"]), RUNTIME, Path(sys.executable))
        self.assertEqual((active.read_bytes(), policy.read_bytes()), expected)
        self.assertFalse((self.root / "learning/selection-history").exists())

    def test_reopen_and_reconcile_reject_policy_drift_without_changing_selection(self):
        initial = self.initialize()
        imported = bootstrap.import_memory(Path(initial["memory_workspace"]), self.root / "imports/v2", RUNTIME,
            Path(sys.executable), seed_handles=initial["seed_handles"], history_references=True)
        bootstrap.select_import(self.root, Path(imported["destination"]), RUNTIME, Path(sys.executable))
        active = Path(initial["active_config"]); policy = Path(initial["origami_policy"])
        altered = json.loads(policy.read_text()); altered["schema"] = "origami-learning-policy/v1"
        policy.write_text(json.dumps(altered))
        before = (active.read_bytes(), policy.read_bytes())
        records = snapshot(Path(imported["memory_workspace"]) / "organs")
        for operation in (bootstrap.initialize, bootstrap.reconcile):
            with self.assertRaisesRegex(ValueError, "Invalid paired reference-history"):
                operation(self.root, RUNTIME, Path(sys.executable))
            self.assertEqual((active.read_bytes(), policy.read_bytes()), before)
            self.assertEqual(snapshot(Path(imported["memory_workspace"]) / "organs"), records)

    def test_v2_selection_write_failure_restores_both_exact_preimages(self):
        initial = self.initialize()
        imported = bootstrap.import_memory(Path(initial["memory_workspace"]), self.root / "imports/v2", RUNTIME,
            Path(sys.executable), seed_handles=initial["seed_handles"], history_references=True)
        active = Path(initial["active_config"]); policy = Path(initial["origami_policy"])
        before = (active.read_bytes(), policy.read_bytes())
        observed = self.native('''
import importlib.util, json
from pathlib import Path
spec = importlib.util.spec_from_file_location('reference_selection_fixture', sys.argv[2])
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
original = module._replace
calls = []
def fail_once(path, raw, expected):
    calls.append(str(path))
    if len(calls) == 2:
        raise OSError('Scripted second selection write failed')
    return original(path, raw, expected)
module._replace = fail_once
try:
    module._select(Path(sys.argv[3]), Path(sys.argv[4]))
except OSError:
    print(json.dumps({'calls': len(calls)}))
else:
    raise AssertionError('Expected scripted selection failure')
''', ROOT / "desktop/bootstrap_learning.py", self.root, imported["destination"])
        self.assertEqual(observed["calls"], 3)
        self.assertEqual((active.read_bytes(), policy.read_bytes()), before)
        self.assertFalse((self.root / "learning/STOP").exists())
        self.assertFalse((Path(imported["destination"]) / "STOP").exists())

    @unittest.skipUnless(os.environ.get("LODESTONE_OLD_RUNTIME"), "Explicit old reader required")
    def test_actual_old_runtime_refuses_new_import_and_selection_without_changes(self):
        old = Path(os.environ["LODESTONE_OLD_RUNTIME"]).resolve()
        initial = self.initialize()
        original = Path(initial["memory_workspace"])
        destination = self.root / "imports/unsupported"
        with self.assertRaises(ValueError):
            bootstrap.import_memory(original, destination, old, Path(sys.executable),
                seed_handles=initial["seed_handles"], history_references=True)
        self.assertFalse(destination.exists())
        imported = bootstrap.import_memory(original, self.root / "imports/v2", RUNTIME, Path(sys.executable),
            seed_handles=initial["seed_handles"], history_references=True)
        paths = [Path(initial["active_config"]), Path(initial["origami_policy"])]
        before = [p.read_bytes() for p in paths]
        with self.assertRaises(ValueError):
            bootstrap.select_import(self.root, Path(imported["destination"]), old, Path(sys.executable))
        self.assertEqual([p.read_bytes() for p in paths], before)
        self.assertEqual(bootstrap.validate_selection(self.root, old, Path(sys.executable))["status"], "COMPATIBLE")
        # Merely relabelling marker-bearing memory as v1 must not bypass the
        # legacy reader's inability to validate its workspace capability.
        alias = json.loads(before[0]); alias["memory_workspace"] = imported["memory_workspace"]
        legacy_policy = json.loads(before[1]); legacy_policy["memory_workspace"] = imported["memory_workspace"]
        paths[0].write_text(json.dumps(alias)); paths[1].write_text(json.dumps(legacy_policy))
        labelled = [p.read_bytes() for p in paths]
        with self.assertRaisesRegex(ValueError, "Legacy runtime cannot validate.*marker"):
            bootstrap.validate_selection(self.root, old, Path(sys.executable))
        self.assertEqual([p.read_bytes() for p in paths], labelled)

    def test_idle_stopped_workspace_can_be_checked_but_live_reader_cannot(self):
        import fcntl
        initial = self.initialize()
        stop = self.root / "learning/STOP"
        stop.write_text("Explicit fixture STOP; maintenance must not clear it.\n")
        self.assertEqual(bootstrap.validate_selection(self.root, RUNTIME, Path(sys.executable))["status"], "COMPATIBLE")
        memory = Path(initial["memory_workspace"])
        with (memory / "learning-weaving.lock").open("a+b") as lease:
            fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(ValueError):
                bootstrap.validate_selection(self.root, RUNTIME, Path(sys.executable))
        self.assertEqual(stop.read_text(), "Explicit fixture STOP; maintenance must not clear it.\n")


class ReferenceInstallerBoundaryTests(unittest.TestCase):
    def test_unsupported_selection_schema_cannot_skip_rollback_validation(self):
        import install
        fixture = installer_fixtures.InstallationTests(methodName="test_upgrade_then_rollback_preserves_new_records")
        fixture.setUp(); self.addCleanup(fixture.tearDown)
        fixture.setup_install(); fixture.manifest("0.4.0-preview.next"); fixture.setup_install()
        for value in ({"schema": "lodestone-learning-active/v99"}, {}, []):
            install.save(fixture.root / "learning/ACTIVE.json", value)
            before = snapshot(fixture.root)
            with patch.object(install, "restore") as restore, patch.object(install, "release_module") as module:
                with self.assertRaisesRegex(ValueError, "Unsupported learning selection"):
                    install.rollback(fixture.root)
                restore.assert_not_called(); module.assert_not_called()
            self.assertEqual(snapshot(fixture.root), before)

    def test_cli_reference_import_is_explicit_and_uses_installed_runtime(self):
        import install
        module = Mock()
        module.import_memory.return_value = {"status": "CAPTURED", "provider_calls": 0}
        active = {"release": "/fixture/installed-release", "worker_python": sys.executable}
        for flag in ([], ["--history-references"]):
            module.reset_mock()
            argv = ["install.py", "--root", "/fixture/preview", "import-memory", "/fixture/source",
                    "/fixture/destination", "--seed", "bezoar:" + "a" * 64, *flag]
            with patch.object(sys, "argv", argv), patch.object(install, "active_install", return_value=active), \
                    patch.object(install, "release_module", return_value=module), patch("builtins.print"):
                self.assertEqual(install.main(), 0)
            args, kwargs = module.import_memory.call_args
            self.assertEqual(args[2], Path(active["release"]) / "runtime")
            self.assertEqual(args[3], Path(sys.executable))
            if flag:
                self.assertIs(kwargs["history_references"], True)
            else:
                self.assertNotIn("history_references", kwargs)

    def test_rollback_checks_target_reader_before_any_pointer_write(self):
        import install
        fixture = installer_fixtures.InstallationTests(methodName="test_upgrade_then_rollback_preserves_new_records")
        fixture.setUp(); self.addCleanup(fixture.tearDown)
        first = fixture.setup_install()
        fixture.manifest("0.4.0-preview.next")
        fixture.setup_install()
        selection = fixture.root / "learning/ACTIVE.json"
        install.save(selection, {"schema": "lodestone-learning-active/v2", "required": True,
            "memory_workspace": str(fixture.root / "imports/new/memory"),
            "seed_handles": ["bezoar:" + "b" * 64],
            "history_representation": "bezoar-learning-history-inventory/v1"})
        policy = fixture.root / "runtime/learning-policy.json"
        install.save(policy, {"schema": "fixture-policy", "original": True})
        before = snapshot(fixture.root)
        validator = Mock()
        validator.validate_selection.side_effect = ValueError("Actual target reader refuses v2")
        with patch.object(install, "release_module", return_value=validator), \
                patch.object(install, "restore", wraps=install.restore) as restore:
            with self.assertRaisesRegex(ValueError, "compatible paired runtime and memory workspace"):
                install.rollback(fixture.root)
            restore.assert_not_called()
        self.assertEqual(snapshot(fixture.root), before)
        validator.validate_selection.assert_called_once_with(
            fixture.root, Path(first["release"]) / "runtime", Path(first["worker_python"]))
        # Only a compatible exact selection permits the existing pointer restore.
        validator.validate_selection.side_effect = None
        validator.validate_selection.return_value = {"status": "COMPATIBLE"}
        with patch.object(install, "release_module", return_value=validator):
            self.assertEqual(install.rollback(fixture.root)["status"], "ROLLED_BACK")
        self.assertEqual(install.active_install(fixture.root)["release_id"], first["release_id"])
        self.assertEqual(snapshot(fixture.root)[str(selection.relative_to(fixture.root))],
                         before[str(selection.relative_to(fixture.root))])


if __name__ == "__main__":
    unittest.main()
