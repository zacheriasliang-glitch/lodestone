"""Native locking and portable launcher lifecycle, without downloads or user data."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import test_install as installation_fixtures
import install
import platform_support as platform


class PlatformPaths(unittest.TestCase):
    def test_macos_paths_stay_unchanged(self):
        home = Path(tempfile.gettempdir()).resolve() / "home"
        self.assertEqual(platform.default_locations("macos", home=home, environ={}),
            (home / "Library/Application Support/Lodestone Preview", home / "Applications/Lodestone Preview.app"))

    def test_windows_paths_are_per_user_and_not_app_bundles(self):
        home = Path(tempfile.gettempdir()).resolve() / "home"
        env = {"LOCALAPPDATA": str(home / "local"), "APPDATA": str(home / "roaming")}
        root, app = platform.default_locations("windows", home=home, environ=env)
        self.assertEqual(root, home / "local/Lodestone Preview")
        self.assertEqual(app, home / "roaming/Microsoft/Windows/Start Menu/Programs/Lodestone Preview.cmd")
        self.assertEqual(platform.default_locations("windows", home=home, environ={})[0],
                         home / "AppData/Local/Lodestone Preview")

    def test_linux_xdg_paths_and_relative_variable_fallback(self):
        home = Path(tempfile.gettempdir()).resolve() / "home"
        data = home / "custom data"
        self.assertEqual(platform.default_locations("linux", home=home, environ={"XDG_DATA_HOME": str(data)}),
                         (data / "lodestone-preview", data / "applications/lodestone-preview.desktop"))
        self.assertEqual(platform.default_locations("linux", home=home, environ={"XDG_DATA_HOME": "relative"})[0],
                         home / ".local/share/lodestone-preview")

    def test_install_and_launch_import_in_isolated_python(self):
        desktop = Path(install.__file__).parent
        for script in ("install.py", "run_loom.py"):
            result = subprocess.run([sys.executable, "-I", "-B", str(desktop / script), "--help"],
                        capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("--root", result.stdout)

    def test_host_venv_python(self):
        path = Path("venv")
        self.assertEqual(platform.venv_python(path), path / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))

    def test_linux_launcher_escapes_both_desktop_parsing_layers(self):
        root = Path(tempfile.gettempdir()).resolve() / 'a "$quote`percent% back\\slash'
        app = root / "test.desktop"
        body, _ = install.launcher_files(root, app, {"worker_python": str(root / "python")}, "linux")[app]
        command = next(line.removeprefix("Exec=") for line in body.decode().splitlines() if line.startswith("Exec="))
        # Desktop string decoding precedes Exec double-quote parsing.
        decoded = command.replace("\\\\", "\\").replace("%%", "%")
        argv = shlex.split(decoded)
        # shlex preserves backslashes before $ and `, unlike the desktop grammar.
        argv = [value.replace("\\$", "$").replace("\\`", "`") for value in argv]
        self.assertEqual(argv, [str(root / "python"), "-B", str(root / "launch.py")])

    def test_windows_launcher_quotes_percent_and_disables_delayed_expansion(self):
        root = Path(tempfile.gettempdir()).resolve() / "percent% & bang!"
        app = root / "test.cmd"
        body, _ = install.launcher_files(root, app, {"worker_python": str(root / "python.exe")}, "windows")[app]
        self.assertIn(b"setlocal DisableDelayedExpansion\r\n", body)
        self.assertIn(b"percent%% & bang!", body)
        self.assertIn(b"chcp 65001 >nul", body)
        self.assertNotIn(b"Contents/MacOS", body)


class PortableLifecycle(unittest.TestCase):
    @contextmanager
    def fixture(self, target):
        fixture = installation_fixtures.InstallationTests()
        fixture.setUp()
        fixture.app = fixture.base / "launchers" / ("Lodestone.cmd" if target == "windows" else "lodestone.desktop")
        def setup():
            return install.install(fixture.root, fixture.payload, app=fixture.app,
                    provision=fixture.provision, bootstrap=fixture.bootstrap, platform_name=target)
        fixture.setup_install = setup
        try:
            yield fixture
        finally:
            fixture.tearDown()

    def test_both_file_launcher_lifecycles_preserve_drafts_and_records(self):
        for target in ("linux", "windows"):
            with self.subTest(target=target), self.fixture(target) as f:
                first = f.setup_install()
                self.assertTrue(f.app.is_file())
                self.assertEqual(first["platform"], target)
                self.assertEqual(f.setup_install()["status"], "ALREADY_INSTALLED")
                draft = f.root / "Loom/data/draft.json"
                record = f.root / "learning/failed.json"
                draft.write_bytes(b'{"draft":"unsent"}')
                record.write_bytes(b'{"failure":"preserved"}')
                before = install.snapshot(install.managed_paths(f.root, f.app, target))
                f.manifest("next-version")
                f.setup_install()
                f.rollback()
                self.assertEqual(install.snapshot(map(Path, before)), before)
                self.assertEqual(install.uninstall(f.root)["status"], "UNINSTALLED")
                self.assertFalse(f.app.exists())
                self.assertTrue(f.app.parent.is_dir())
                self.assertEqual(f.setup_install()["status"], "INSTALLED")
                self.assertEqual(draft.read_bytes(), b'{"draft":"unsent"}')
                self.assertEqual(record.read_bytes(), b'{"failure":"preserved"}')

    def test_foreign_launcher_is_not_replaced_or_removed(self):
        for target in ("linux", "windows"):
            with self.subTest(target=target), self.fixture(target) as f:
                f.setup_install()
                foreign = b"unrelated user application\n"
                f.app.write_bytes(foreign)
                active = (f.root / "ACTIVE.json").read_bytes()
                for action in (f.setup_install, f.rollback, lambda: install.uninstall(f.root)):
                    with self.assertRaisesRegex(ValueError, "another installation"):
                        action()
                    self.assertEqual(f.app.read_bytes(), foreign)
                    self.assertEqual((f.root / "ACTIVE.json").read_bytes(), active)

    def test_first_rollback_deactivates_file_launcher(self):
        for target in ("linux", "windows"):
            with self.subTest(target=target), self.fixture(target) as f:
                f.setup_install()
                retained = (f.root / "learning/ACTIVE.json").read_bytes()
                f.rollback()
                self.assertFalse(f.app.exists())
                self.assertFalse((f.root / "ACTIVE.json").exists())
                self.assertEqual((f.root / "learning/ACTIVE.json").read_bytes(), retained)

    def test_failed_activation_and_failed_removal_restore_managed_files(self):
        for target in ("linux", "windows"):
            with self.subTest(target=target), self.fixture(target) as f:
                f.setup_install()
                before = install.snapshot(install.managed_paths(f.root, f.app, target))
                f.manifest("next-version")
                original = install.atomic
                failed = False
                def fail_activation(path, data, mode=0o600):
                    nonlocal failed
                    if Path(path) == f.root / "ACTIVE.json" and not failed:
                        failed = True
                        raise OSError("activation failure")
                    return original(path, data, mode)
                with patch.object(install, "atomic", side_effect=fail_activation):
                    with self.assertRaisesRegex(OSError, "activation failure"):
                        f.setup_install()
                self.assertEqual(install.snapshot(map(Path, before)), before)
                failed = False
                def fail_removal(path, data, mode=0o600):
                    nonlocal failed
                    if Path(path) == f.root / "UNINSTALLED.json" and not failed:
                        failed = True
                        raise OSError("removal failure")
                    return original(path, data, mode)
                with patch.object(install, "atomic", side_effect=fail_removal):
                    with self.assertRaisesRegex(OSError, "removal failure"):
                        install.uninstall(f.root)
                self.assertEqual(install.snapshot(map(Path, before)), before)

    def test_interrupted_removal_recovers_before_next_launch(self):
        for target in ("linux", "windows"):
            with self.subTest(target=target), self.fixture(target) as f:
                active = f.setup_install()
                before = install.snapshot([*install.managed_paths(f.root, f.app, target), f.root / "UNINSTALLED.json"])
                transaction = f.root / "transactions/zz-interrupted-removal.json"
                install.save(transaction, {"schema": "lodestone-desktop-transaction/v1",
                    "root": str(f.root), "app": str(f.app), "platform": target,
                    "operation": "REMOVE", "before": before, "state": "PREPARED",
                    "release_id": active["release_id"]})
                f.app.unlink()
                install.save(f.root / "UNINSTALLED.json", active)
                self.assertEqual(install.recover(f.root), [str(transaction)])
                self.assertEqual(install.snapshot(map(Path, before)), before)
                self.assertEqual(install.read(transaction)["state"], "RECOVERED_INTERRUPTED_REMOVAL")

    def test_failed_rollback_restores_current_release_and_transaction(self):
        with self.fixture("linux") as f:
            f.setup_install()
            f.manifest("new-release")
            active = f.setup_install()
            before = install.snapshot([*install.managed_paths(f.root, f.app, "linux"), Path(active["transaction"])])
            original = install.atomic
            failed = False
            def fail_once(path, data, mode=0o600):
                nonlocal failed
                if Path(path) == f.root / "Loom/runtime.json" and not failed:
                    failed = True
                    raise OSError("rollback interrupted")
                return original(path, data, mode)
            with patch.object(install, "atomic", side_effect=fail_once):
                with self.assertRaisesRegex(OSError, "rollback interrupted"):
                    f.rollback()
            self.assertEqual(install.snapshot(map(Path, before)), before)

    def test_launcher_from_other_root_is_refused(self):
        with self.fixture("linux") as f:
            f.setup_install()
            replacement = install.launcher_files(f.base / "other-root", f.app,
                          {"worker_python": sys.executable}, "linux")[f.app][0]
            f.app.write_bytes(replacement)
            with self.assertRaisesRegex(ValueError, "another installation"):
                install.uninstall(f.root)


class NativeFileOperations(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()

    def tearDown(self):
        self.temp.cleanup()

    def exercise_windows_python_launcher(self, *, batch):
        root = self.root / "Loom λ percent% & bang!"
        root.mkdir()
        release = root / "release source"
        release.mkdir()
        (release / "install.py").write_text(
            "def recover(root):\n    (root / 'recovered').write_text('yes')\n")
        desktop = str(Path(install.__file__).parent)
        (release / "run_loom.py").write_text(
            "import json, sys, time\nfrom pathlib import Path\n"
            "sys.path.insert(0, " + repr(desktop) + ")\n"
            "from platform_support import file_lock\n"
            "root = Path(sys.argv[2])\n"
            "with file_lock(root / '.lifecycle.lock', shared=True):\n"
            "    (root / 'ready').write_text('ready')\n"
            "    deadline = time.monotonic() + 15\n"
            "    while not (root / 'finish').exists():\n"
            "        if time.monotonic() > deadline: raise SystemExit(99)\n"
            "        time.sleep(0.01)\n"
            "    (root / 'arguments.json').write_text(json.dumps(sys.argv[1:]))\n"
            "raise SystemExit(42)\n", encoding="utf-8")
        (root / "ACTIVE.json").write_text(json.dumps(
            {"gui_python": sys.executable, "release": str(release)}))
        launch = root / "launch.py"
        launch.write_text(install.launcher_source(release, "windows"), encoding="utf-8")
        arguments = ["--fixture-argument", "space λ"]
        if batch:
            app = root / "Lodestone.cmd"
            body, mode = install.launcher_files(root, app, {"worker_python": sys.executable}, "windows")[app]
            platform.atomic_write(app, body, mode)
            executable = os.environ.get("COMSPEC", "cmd.exe")
            inner = " ".join('"' + value + '"' for value in [str(app), *arguments])
            command = '"' + executable + '" /d /v:off /s /c "' + inner + '"'
        else:
            command = [sys.executable, "-B", str(launch), *arguments]
        child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 10
            while not (root / "ready").exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue((root / "ready").exists(), "GUI child did not become ready")
            self.assertIsNone(child.poll(), "Launcher returned while its GUI child was running")
            self.assertEqual((root / "recovered").read_text(), "yes")
            with self.assertRaisesRegex(ValueError, "Close this Lodestone"):
                with install.locked(root):
                    self.fail("Installer admitted while the GUI child's lease was held")
            (root / "finish").write_text("finish")
            stdout, stderr = child.communicate(timeout=15)
            self.assertEqual(child.returncode, 42, repr((stdout, stderr)))
            self.assertEqual(json.loads((root / "arguments.json").read_text()),
                             ["--root", str(root), *arguments])
            with install.locked(root):
                pass
        finally:
            (root / "finish").write_text("finish")
            try:
                child.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.communicate(timeout=5)

    def test_windows_python_launcher_waits_for_gui_and_preserves_lease_and_exit(self):
        self.exercise_windows_python_launcher(batch=False)

    @unittest.skipUnless(os.name == "nt", "native Windows installed batch launcher")
    def test_windows_batch_launcher_waits_for_gui_and_propagates_exit(self):
        self.exercise_windows_python_launcher(batch=True)

    def test_shared_leases_exclude_installation_and_release_on_exit(self):
        path = self.root / ".lifecycle.lock"
        with platform.file_lock(path, shared=True):
            with platform.file_lock(path, shared=True):
                with self.assertRaises(BlockingIOError):
                    with platform.file_lock(path):
                        self.fail("exclusive lease admitted")
        with platform.file_lock(path):
            with self.assertRaises(BlockingIOError):
                with platform.file_lock(path, shared=True):
                    self.fail("shared lease admitted")

    def test_native_cross_process_lock_blocks_and_recovers_after_process_exit(self):
        path = self.root / ".lifecycle.lock"
        script = ("import sys; from pathlib import Path; from platform_support import file_lock; "
                  "lease=file_lock(Path(sys.argv[1]),shared=True); lease.__enter__(); "
                  "print('HELD',flush=True); sys.stdin.read()")
        child = subprocess.Popen([sys.executable, "-B", "-c", script, str(path)],
            cwd=Path(install.__file__).parent, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "HELD")
            with self.assertRaisesRegex(ValueError, "Close this Lodestone"):
                with install.locked(self.root):
                    self.fail("live application was not protected")
            child.terminate()
            child.wait(timeout=10)
            with install.locked(self.root):
                pass
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)

    def test_exclusive_publication_never_overwrites_existing_file_or_directory(self):
        path = self.root / "immutable.json"
        platform.atomic_write(path, b"original", replace=False)
        with self.assertRaises(FileExistsError):
            platform.atomic_write(path, b"other", replace=False)
        self.assertEqual(platform.read_file(path), b"original")
        source = self.root / "stage"
        source.mkdir()
        platform.atomic_write(source / "record", b"record")
        target = self.root / "release"
        target.mkdir()
        with self.assertRaises(FileExistsError):
            platform.durable_move(source, target)
        self.assertTrue(source.exists())
        target.rmdir()
        platform.durable_move(source, target)
        self.assertEqual(platform.read_file(target / "record"), b"record")
        self.assertFalse(source.exists())

    def test_bootstrap_metadata_is_idempotent_and_preserves_differing_bytes(self):
        import bootstrap_learning
        path = self.root / "bootstrap.json"
        bootstrap_learning._write_new(path, b"original")
        bootstrap_learning._write_new(path, b"original")
        with self.assertRaisesRegex(ValueError, "metadata differs"):
            bootstrap_learning._write_new(path, b"changed")
        self.assertEqual(path.read_bytes(), b"original")

    def test_atomic_replace_is_complete_and_refuses_symlinks(self):
        path = self.root / "active.json"
        platform.atomic_write(path, b"before")
        platform.atomic_write(path, b"after")
        self.assertEqual(path.read_bytes(), b"after")
        platform.remove_file(path)
        self.assertFalse(path.exists())
        outside = self.root / "outside"
        outside.write_bytes(b"private")
        try:
            path.symlink_to(outside)
        except OSError as exc:
            if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
                self.skipTest("Windows symlink privilege unavailable")
            raise
        with self.assertRaisesRegex(ValueError, "symbolic link|reparse"):
            platform.atomic_write(path, b"replacement")
        self.assertEqual(outside.read_bytes(), b"private")

    @unittest.skipUnless(os.name == "nt", "native Windows junction semantics")
    def test_windows_junction_parent_is_rejected(self):
        target = self.root / "target"
        target.mkdir()
        link = self.root / "junction"
        subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
                       check=True, capture_output=True)
        try:
            with self.assertRaisesRegex(ValueError, "reparse"):
                platform.atomic_write(link / "record.json", b"wrong")
            self.assertFalse((target / "record.json").exists())
        finally:
            link.rmdir()

    @unittest.skipUnless(os.name == "nt", "native Windows batch execution")
    def test_windows_cmd_launcher_runs_unicode_and_special_character_paths(self):
        root = self.root / "Loom λ percent% & bang!"
        root.mkdir()
        app = root / "Lodestone.cmd"
        (root / "launch.py").write_text("from pathlib import Path\nPath(__file__).with_name('started').write_text('ok')\n")
        content, mode = install.launcher_files(root, app, {"worker_python": sys.executable}, "windows")[app]
        platform.atomic_write(app, content, mode)
        # cmd has its own outer-quote rules; list2cmdline quotes for the C
        # runtime and lets /c strip the only pair around this batch path.
        executable = os.environ.get("COMSPEC", "cmd.exe")
        command = '"' + executable + '" /d /v:off /s /c ""' + str(app) + '""'
        result = subprocess.run(command, capture_output=True, encoding="utf-8",
                                errors="replace", timeout=15)
        log = root / "launcher.log"
        details = result.stdout + result.stderr
        if log.exists():
            details += log.read_text(encoding="utf-8", errors="replace")
        self.assertEqual(result.returncode, 0, details)
        self.assertEqual((root / "started").read_text(), "ok")


if __name__ == "__main__":
    unittest.main()
