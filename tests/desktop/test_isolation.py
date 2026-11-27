"""Kernel fences fail closed: no command provider or trial checker runs unfenced.

Platforms are simulated for the selection and refusal checks; the real-fence
checks run only where that fence exists. No model or network call.
"""
from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile
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
            patch.object(isolation, "_bwrap_starts", lambda path: bwrap_starts)]


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

    def test_other_platforms_have_no_fence_and_refuse(self):
        for platform in ("win32", "cygwin", "freebsd14"):
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
            self.assertIn(found.kind, ("sandbox-exec", "bwrap"))
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
