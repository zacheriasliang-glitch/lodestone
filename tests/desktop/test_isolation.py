"""Kernel fences fail closed: no command provider or trial checker runs unfenced.

Platforms are simulated for the selection and refusal checks; the real-fence
checks run only where that fence exists. No model or network call.
"""
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = Path(os.environ.get("LODESTONE_DESKTOP_TEST_RUNTIME", ROOT / "desktop/runtime")).resolve()
sys.path.insert(0, str(RUNTIME / "src"))

from lodestone_machinery import isolation, loom_garden, origami  # noqa: E402


def simulated(platform, tools, bwrap_starts=True):
    """Patch platform, tool discovery and the bwrap start probe."""
    isolation._bwrap_starts.cache_clear()
    return [patch.object(isolation.sys, "platform", platform),
            patch.object(isolation.shutil, "which", lambda name: tools.get(name)),
            patch.object(isolation, "_bwrap_starts", lambda path: bwrap_starts),
            patch.object(isolation, "_appcontainer_starts", lambda path: tools.get("appcontainer", False))]


class Simulated:
    def __init__(self, *patches):
        self.patches = patches

    def __enter__(self):
        for item in self.patches:
            item.start()

    def __exit__(self, *exc):
        for item in reversed(self.patches):
            item.stop()


class FenceSelectionTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "Seatbelt profiles express POSIX paths")
    def test_macos_selects_seatbelt_and_keeps_the_exact_earlier_invocation(self):
        with Simulated(*simulated("darwin", {"sandbox-exec": "/usr/bin/sandbox-exec"})):
            fence = isolation.fence()
        self.assertEqual(fence, isolation.Fence("sandbox-exec", "/usr/bin/sandbox-exec"))
        with tempfile.TemporaryDirectory() as raw:
            work, source = Path(raw) / "w", Path(raw) / "s"
            work.mkdir(), source.mkdir()
            argv = [sys.executable, "-c", "pass"]
            earlier = ["/usr/bin/sandbox-exec", "-p",
                       origami._profile(work, source, origami._program_paths(argv), None, 8080), *argv]
            self.assertEqual(origami._fenced_argv(fence, argv, work, source, None, 8080), earlier)
        # The recorded constraints are byte-for-byte what the bool form produced.
        for endpoints in ((), ("example.com:443",)):
            self.assertEqual(origami._constraints("command", fence, endpoints, None),
                             origami._constraints("command", True, endpoints, None))

    def test_linux_selects_bwrap_only_when_it_starts(self):
        with Simulated(*simulated("linux", {"bwrap": "/usr/bin/bwrap"})):
            self.assertEqual(isolation.fence(), isolation.Fence("bwrap", "/usr/bin/bwrap"))
        with Simulated(*simulated("linux", {"bwrap": "/usr/bin/bwrap"}, bwrap_starts=False)):
            self.assertIsNone(isolation.fence())
            with self.assertRaises(isolation.FenceUnavailable):
                isolation.require("A command")
        with Simulated(*simulated("linux", {})):
            self.assertIsNone(isolation.fence())

    def test_windows_selects_appcontainer_only_when_it_starts(self):
        with Simulated(*simulated("win32", {"appcontainer": True})):
            found = isolation.fence()
            self.assertEqual(found.kind, "appcontainer")
            self.assertTrue(found.path.endswith("windows_appcontainer.py"))
            with self.assertRaisesRegex(isolation.FenceUnavailable, "endpoint"):
                isolation.require("A command", endpoints=("example.com:443",))
        with Simulated(*simulated("win32", {"appcontainer": False})):
            self.assertIsNone(isolation.fence())

    def test_appcontainer_argv_uses_the_launcher_with_explicit_grants(self):
        fence = isolation.Fence("appcontainer", "/x/windows_appcontainer.py")
        with tempfile.TemporaryDirectory() as raw:
            work, source = Path(raw) / "w", Path(raw) / "s"
            work.mkdir(), source.mkdir()
            argv = origami._fenced_argv(fence, [sys.executable, "-c", "pass"], work, source, None, None)
            self.assertEqual(argv[1:4], ["-I", "-B", "/x/windows_appcontainer.py"])
            self.assertEqual(argv[argv.index("--workspace") + 1], str(work))
            reads = [argv[i + 1] for i, item in enumerate(argv) if item == "--read"]
            self.assertIn(str(source), reads)
            self.assertNotIn(str(work), reads)
            self.assertEqual(argv[argv.index("--") + 1:], [sys.executable, "-c", "pass"])
            with self.assertRaises(ValueError):
                origami._fenced_argv(fence, [sys.executable], work, source, None, 8080)
        constraints = origami._constraints("command", fence, (), None)
        self.assertEqual((constraints["launcher"], constraints["no_network"], constraints["no_further_subprocesses"]),
                         ("appcontainer", "ENFORCED", "DECLARED"))

    def test_other_platforms_have_no_fence_and_refuse(self):
        for platform in ("cygwin", "freebsd14"):
            with self.subTest(platform=platform), Simulated(*simulated(platform, {"sandbox-exec": "/x", "bwrap": "/y"})):
                self.assertIsNone(isolation.fence())
                with self.assertRaisesRegex(isolation.FenceUnavailable, "refused"):
                    isolation.require("A command")

    def test_linux_refuses_endpoint_network(self):
        with Simulated(*simulated("linux", {"bwrap": "/usr/bin/bwrap"})):
            with self.assertRaisesRegex(isolation.FenceUnavailable, "endpoint"):
                isolation.require("A command", endpoints=("example.com:443",))
            self.assertEqual(isolation.require("A command").kind, "bwrap")

    def test_bwrap_argv_has_no_network_and_one_writable_place(self):
        with tempfile.TemporaryDirectory() as raw:
            work, source = Path(raw) / "w", Path(raw) / "s"
            work.mkdir(), source.mkdir()
            fence = isolation.Fence("bwrap", "/usr/bin/bwrap")
            argv = origami._fenced_argv(fence, ["/usr/bin/python3", "-c", "pass"], work, source, None, None)
            head, tail = argv[:argv.index("--")], argv[argv.index("--") + 1:]
            self.assertEqual(tail, ["/usr/bin/python3", "-c", "pass"])
            self.assertIn("--unshare-all", head)
            self.assertNotIn("--share-net", head)
            writable = [head[i + 1] for i, item in enumerate(head) if item == "--bind"]
            self.assertEqual(writable, [str(work)])
            self.assertLess(head.index(str(source)), head.index("--bind"))
            with self.assertRaises(ValueError):
                origami._fenced_argv(fence, ["/usr/bin/true"], work, source, None, 8080)
        constraints = origami._constraints("command", fence, (), None)
        self.assertEqual(constraints["launcher"], "bwrap")
        self.assertEqual(constraints["no_network"], "ENFORCED")
        self.assertEqual(constraints["no_further_subprocesses"], "DECLARED")


class RefusalTests(unittest.TestCase):
    def test_origami_command_provider_is_refused_before_any_state_without_a_fence(self):
        with tempfile.TemporaryDirectory() as raw:
            organ = origami.Origami(Path(raw) / "ws")
            before = sorted(p.relative_to(raw) for p in Path(raw).rglob("*"))
            with Simulated(*simulated("win32", {})):
                with self.assertRaises(isolation.FenceUnavailable):
                    organ.run("never-created", ["/bin/echo", "{}"])
            self.assertEqual(sorted(p.relative_to(raw) for p in Path(raw).rglob("*")), before)

    def test_garden_trial_is_refused_without_a_fence(self):
        with Simulated(*simulated("win32", {})):
            with self.assertRaises(isolation.FenceUnavailable):
                loom_garden.sandboxed(["/bin/echo"], "/tmp", "/tmp")

    @unittest.skipUnless(os.name == "posix", "Seatbelt profiles express POSIX paths")
    def test_garden_trial_records_the_fence_kind(self):
        with Simulated(*simulated("darwin", {"sandbox-exec": "/usr/bin/sandbox-exec"})):
            argv, kind = loom_garden.sandboxed(["/bin/echo"], "/tmp", "/tmp")
        self.assertEqual((argv[:2], kind), (["/usr/bin/sandbox-exec", "-p"], "sandbox-exec"))


class HostOutcomeTests(unittest.TestCase):
    def test_this_host_is_fenced_or_refuses_never_unfenced(self):
        """On a stock host the answer may be either; it must never be an unfenced run."""
        isolation._bwrap_starts.cache_clear()
        found = isolation.fence()
        if found is None:
            with self.assertRaises(isolation.FenceUnavailable):
                isolation.require("A command")
            outcome = "REFUSED"
        else:
            self.assertIn(found.kind, ("sandbox-exec", "bwrap", "appcontainer"))
            outcome = "FENCED:" + found.kind
        print("ISOLATION_HOST_OUTCOME", sys.platform, outcome)
        expected = os.environ.get("LODESTONE_EXPECT_ISOLATION")
        if expected:
            self.assertEqual(outcome.split(":")[0], expected)


class RealBubblewrapTests(unittest.TestCase):
    """Skips off Linux; CI sets LODESTONE_REQUIRE_BWRAP=1 so a missing fence fails instead."""

    def test_fence_confines_writes_and_network(self):
        required = os.environ.get("LODESTONE_REQUIRE_BWRAP") == "1"
        isolation._bwrap_starts.cache_clear()
        fence = isolation.fence() if sys.platform.startswith("linux") else None
        if fence is None or fence.kind != "bwrap":
            if required:
                self.fail("A usable bubblewrap fence was required here and is absent")
            self.skipTest("real Linux bubblewrap is unavailable here")
        with tempfile.TemporaryDirectory() as raw:
            work, source, outside = Path(raw) / "w", Path(raw) / "s", Path(raw) / "outside"
            work.mkdir(), source.mkdir(), outside.mkdir()
            (source / "input.txt").write_text("read me")
            program = (
                "import pathlib, socket, sys\n"
                "w, s, o = map(pathlib.Path, sys.argv[1:4])\n"
                "assert (s / 'input.txt').read_text() == 'read me'\n"
                "(w / 'ok.txt').write_text('inside')\n"
                "for target in (s / 'x', o / 'x'):\n"
                "    try:\n"
                "        target.write_text('escape'); print('WROTE', target); sys.exit(3)\n"
                "    except OSError:\n"
                "        pass\n"
                "try:\n"
                "    socket.create_connection(('1.1.1.1', 53), timeout=3); print('NET'); sys.exit(4)\n"
                "except OSError:\n"
                "    pass\n"
                "print('CONFINED')\n")
            argv = origami._fenced_argv(fence, [sys.executable, "-I", "-c", program, str(work), str(source), str(outside)],
                                        work, source, None, None)
            done = subprocess.run(argv, capture_output=True, text=True, timeout=60)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            self.assertIn("CONFINED", done.stdout)
            self.assertEqual((work / "ok.txt").read_text(), "inside")
            self.assertFalse((outside / "x").exists())
            self.assertFalse((source / "x").exists())


if __name__ == "__main__":
    unittest.main()


class RealAppContainerTests(unittest.TestCase):
    """Windows only; CI sets LODESTONE_REQUIRE_APPCONTAINER=1 so a missing fence fails instead."""
    maxDiff = None

    def setUp(self):
        required = os.environ.get("LODESTONE_REQUIRE_APPCONTAINER") == "1"
        isolation._appcontainer_starts.cache_clear()
        self.fence = isolation.fence() if sys.platform == "win32" else None
        if self.fence is None or self.fence.kind != "appcontainer":
            if required:
                self.fail("A usable Windows AppContainer fence was required here and is absent")
            self.skipTest("real Windows AppContainer is unavailable here")

    def _sddl(self, path):
        from lodestone_machinery import windows_appcontainer as wac
        return wac._Security(wac._Api()).read(path)

    def _icacls(self, path):
        system = os.environ.get("SystemRoot", r"C:\\Windows")
        return subprocess.run([os.path.join(system, "System32", "icacls.exe"), str(path)],
                              capture_output=True, text=True, timeout=60).stdout

    def test_fence_confines_writes_reads_and_network_and_cleans_up(self):
        from lodestone_machinery import windows_appcontainer as wac
        ledger = wac._ledger_directory()
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            work, source, outside, private = base / "w", base / "s", base / "outside", base / "private"
            for item in (work, source, outside, private):
                item.mkdir()
            (source / "input.txt").write_text("read me")
            (private / "secret.txt").write_text("not for the fence")
            # Absolute argument paths are deliberately readable inside every fence,
            # so the private location reaches the program only through a source file.
            (source / "private-path.txt").write_text(str(private))
            before = {p: self._sddl(p) for p in (work, source, outside, private)}
            program = (
                "import pathlib, socket, sys\n"
                "w, s, o = map(pathlib.Path, sys.argv[1:4])\n"
                "p = pathlib.Path((s / 'private-path.txt').read_text())\n"
                "assert (s / 'input.txt').read_text() == 'read me'\n"
                "(w / 'ok.txt').write_text('inside')\n"
                "for target in (s / 'x', o / 'x'):\n"
                "    try:\n"
                "        target.write_text('escape'); print('WROTE', target); sys.exit(3)\n"
                "    except OSError:\n"
                "        pass\n"
                "try:\n"
                "    (p / 'secret.txt').read_text(); print('READ PRIVATE'); sys.exit(5)\n"
                "except OSError:\n"
                "    pass\n"
                "for address in (('1.1.1.1', 53), ('127.0.0.1', 445)):\n"
                "    try:\n"
                "        socket.create_connection(address, timeout=3); print('NET', address); sys.exit(4)\n"
                "    except OSError:\n"
                "        pass\n"
                "print('CONFINED')\n")
            argv = origami._fenced_argv(self.fence, [sys.executable, "-I", "-c", program, str(work), str(source),
                                                     str(outside)], work, source, None, None)
            done = subprocess.run(argv, capture_output=True, text=True, timeout=180)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            self.assertIn("CONFINED", done.stdout)
            self.assertEqual((work / "ok.txt").read_text(), "inside")
            self.assertFalse((outside / "x").exists())
            self.assertFalse((source / "x").exists())
            # Every transient grant is gone and no recovery entry remains.
            self.assertEqual({p: self._sddl(p) for p in (work, source, outside, private)}, before,
                             "stderr:\n" + done.stderr)
            self.assertEqual(list(ledger.glob(wac.PREFIX + "*.journal")), [])

    def _launch(self, work, source, code, **env):
        argv = origami._fenced_argv(self.fence, [sys.executable, "-I", "-c", code], work, source, None, None)
        return subprocess.run(argv, capture_output=True, text=True, timeout=300, env={**os.environ, **env})

    def _pending(self):
        from lodestone_machinery import windows_appcontainer as wac
        return list(wac._ledger_directory().glob(wac.PREFIX + "*.journal"))

    def test_interruption_after_one_real_acl_mutation_is_recovered_exactly(self):
        from lodestone_machinery import windows_appcontainer as wac
        with tempfile.TemporaryDirectory() as raw:
            work, source = Path(raw) / "w", Path(raw) / "s"
            work.mkdir(), source.mkdir()
            before = {p: self._sddl(p) for p in (work, source)}
            done = self._launch(work, source, "pass", LODESTONE_FENCE_TEST_CRASH_AFTER_FIRST_GRANT="1")
            self.assertEqual(done.returncode, 99, done.stderr)
            self.assertNotEqual(self._sddl(work), before[work])  # the mutation really happened
            self.assertEqual(len(self._pending()), 1)
            wac.sweep()
            self.assertEqual({p: self._sddl(p) for p in (work, source)}, before)
            self.assertEqual(self._pending(), [])

    def test_cleanup_failure_is_reported_and_retried_not_hidden(self):
        from lodestone_machinery import windows_appcontainer as wac
        with tempfile.TemporaryDirectory() as raw:
            work, source = Path(raw) / "w", Path(raw) / "s"
            work.mkdir(), source.mkdir()
            before = {p: self._sddl(p) for p in (work, source)}
            done = self._launch(work, source, "pass", LODESTONE_FENCE_TEST_FAIL_RESTORE="1")
            self.assertEqual(done.returncode, wac.EXIT_CLEANUP_UNCONFIRMED, done.stderr)
            self.assertIn("cleanup unconfirmed", done.stderr)
            self.assertEqual(len(self._pending()), 1)
            wac.sweep()
            self.assertEqual({p: self._sddl(p) for p in (work, source)}, before)
            self.assertEqual(self._pending(), [])

    def test_overlapping_runs_on_one_tree_serialize_and_restore_exactly(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            source, work_a, work_b = base / "s", base / "a", base / "b"
            for item in (source, work_a, work_b):
                item.mkdir()
            (source / "input.txt").write_text("shared")
            before = {p: self._sddl(p) for p in (source, work_a, work_b)}
            code = ("import pathlib, sys, time\n"
                    "assert (pathlib.Path(sys.argv[1]) / 'input.txt').read_text() == 'shared'\n"
                    "time.sleep(3)\n")
            runs = [subprocess.Popen(origami._fenced_argv(self.fence, [sys.executable, "-I", "-c", code, str(source)],
                                                         work, source, None, None),
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    for work in (work_a, work_b)]
            results = [run.communicate(timeout=300) for run in runs]
            self.assertEqual([run.returncode for run in runs], [0, 0], results)
            self.assertEqual({p: self._sddl(p) for p in (source, work_a, work_b)}, before)
            self.assertEqual(self._pending(), [])

    def test_a_protection_change_made_during_the_run_is_kept(self):
        from lodestone_machinery import windows_appcontainer as wac
        security = wac._Security(wac._Api())
        with tempfile.TemporaryDirectory() as raw:
            work, source = Path(raw) / "w", Path(raw) / "s"
            work.mkdir(), source.mkdir()
            self.assertFalse(wac._protected(self._sddl(source)))
            argv = origami._fenced_argv(self.fence, [sys.executable, "-I", "-c", "import time; time.sleep(8)"],
                                        work, source, None, None)
            launcher = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            time.sleep(4)
            current = self._sddl(source)
            security.write(source, "D:P" + current[2:].lstrip("AIR"))  # a user's protection-only edit
            out, err = launcher.communicate(timeout=120)
            self.assertEqual(launcher.returncode, 0, err)
            self.assertIn("removed_own_entry_only", err)
            final = self._sddl(source)
            self.assertTrue(wac._protected(final), final)
            self.assertNotIn("S-1-15-2-", final)
            self.assertEqual(self._pending(), [])

    def test_unresolved_earlier_recovery_refuses_new_runs_until_swept(self):
        from lodestone_machinery import windows_appcontainer as wac
        with tempfile.TemporaryDirectory() as raw:
            work, source = Path(raw) / "w", Path(raw) / "s"
            work.mkdir(), source.mkdir()
            stale = wac._ledger_directory() / (wac.PREFIX + "synthetic-unresolved.journal")
            # A recorded grant whose identity cannot be confirmed: recovery must keep it.
            entries = [{"profile": wac.PREFIX + "synthetic-unresolved"}, {"sid": "S-1-15-2-1-2-3-4-5-6-7"},
                       {"grant": str(work), "sddl": self._sddl(work), "id": [0, 0]}]
            stale.write_text("".join(json.dumps(e) + "\n" for e in entries))
            try:
                done = self._launch(work, source, "pass")
                self.assertEqual(done.returncode, wac.EXIT_SETUP_FAILED, done.stderr)
                self.assertIn("recovery is unresolved", done.stderr)
                self.assertTrue(stale.exists())
            finally:
                stale.unlink(missing_ok=True)
            self.assertEqual(self._launch(work, source, "pass").returncode, 0)

    def test_paths_through_a_junction_are_refused_before_any_grant(self):
        from lodestone_machinery import windows_appcontainer as wac
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            target, link = base / "target", base / "link"
            (target / "w").mkdir(parents=True)
            (base / "s").mkdir()
            system = os.environ.get("SystemRoot", r"C:\\Windows")
            subprocess.run([os.path.join(system, "System32", "cmd.exe"), "/c", "mklink", "/J", str(link), str(target)],
                           check=True, capture_output=True, timeout=60)
            before = self._sddl(target / "w")
            for work, source in ((link / "w", base / "s"), (base / "s", link / "w")):
                with self.subTest(work=str(work)):
                    done = self._launch(work, source, "pass")
                    self.assertEqual(done.returncode, wac.EXIT_SETUP_FAILED, done.stdout + done.stderr)
                    self.assertIn("reparse point", done.stderr)
            self.assertEqual(self._sddl(target / "w"), before)
            self.assertEqual(self._pending(), [])

    def test_interrupted_launcher_is_undone_by_the_next_sweep(self):
        from lodestone_machinery import windows_appcontainer as wac
        with tempfile.TemporaryDirectory() as raw:
            work, source = Path(raw) / "w", Path(raw) / "s"
            work.mkdir(), source.mkdir()
            before = self._sddl(source)
            argv = origami._fenced_argv(self.fence, [sys.executable, "-I", "-c", "import time; time.sleep(60)"],
                                        work, source, None, None)
            launcher = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline = time.monotonic() + 60
            while not list(wac._ledger_directory().glob(wac.PREFIX + "*.journal")) and time.monotonic() < deadline:
                time.sleep(0.2)
            time.sleep(5)  # let the grants be applied
            launcher.kill()  # no cleanup runs in the launcher
            launcher.wait(timeout=30)
            # Windows releases a dead process's byte-range lock after an unspecified
            # delay; until then the sweep correctly treats the run as live.
            deadline = time.monotonic() + 60
            while True:
                wac.sweep()
                if not list(wac._ledger_directory().glob(wac.PREFIX + "*.journal")) or time.monotonic() > deadline:
                    break
                time.sleep(1)
            self.assertEqual(self._sddl(source), before)
            self.assertEqual(list(wac._ledger_directory().glob(wac.PREFIX + "*.journal")), [])
