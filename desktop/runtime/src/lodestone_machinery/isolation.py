"""Kernel fences for commands Lodestone launches on the user's behalf.

Fail closed: a command provider or trial checker runs only inside a kernel
fence this platform actually has. There is no unfenced fallback.

- macOS: ``sandbox-exec`` with Origami's Seatbelt profile (unchanged).
- Windows: a fresh per-run AppContainer with no capabilities (no network),
  workspace-only writes and per-run read grants (windows_appcontainer.py),
  when a probe shows it actually starts.
- Linux: bubblewrap (``bwrap``) in fresh namespaces, with read-only binds for
  the system, the program, the source root and any declared auth directory;
  the workspace is the one writable place; no network.
- Anything else, or a fence that cannot start here: refuse.

Linux limits, stated rather than hidden: bwrap does not stop the command from
starting its own child processes inside the fence (Seatbelt denies forks), and
it cannot admit only a local endpoint proxy, so commands that need declared
network endpoints are refused on Linux.
"""
from __future__ import annotations

from dataclasses import dataclass
import functools
import os
import shutil
import subprocess
import sys

SEATBELT = "sandbox-exec"
BWRAP = "bwrap"
APPCONTAINER = "appcontainer"
# Linux system prefixes a fenced program may read so that an interpreter can start.
LINUX_READ_PREFIXES = ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/etc", "/opt")


class FenceUnavailable(ValueError):
    """No kernel fence can confine this command here; it is refused, never run unfenced."""


@dataclass(frozen=True)
class Fence:
    kind: str
    path: str


@functools.lru_cache(maxsize=None)
def _bwrap_starts(path):
    """Unprivileged user namespaces can be disabled; prove bwrap starts before relying on it."""
    true = shutil.which("true") or "/bin/true"
    try:
        done = subprocess.run([path, "--unshare-all", "--die-with-parent", "--ro-bind", "/", "/", "--", true],
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


@functools.lru_cache(maxsize=None)
def _appcontainer_starts(launcher):
    """Prove a no-capability AppContainer starts here before relying on it."""
    import tempfile
    system = os.environ.get("SystemRoot", r"C:\Windows")
    try:
        with tempfile.TemporaryDirectory(prefix="lodestone-fence-probe-") as workspace:
            done = subprocess.run([sys.executable, "-I", "-B", launcher, "--workspace", workspace, "--",
                                   os.path.join(system, "System32", "cmd.exe"), "/c", "exit 0"],
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def fence():
    """The kernel fence available here, or None."""
    if sys.platform == "darwin":
        path = shutil.which(SEATBELT)
        return Fence(SEATBELT, path) if path else None
    if sys.platform.startswith("linux"):
        path = shutil.which(BWRAP)
        return Fence(BWRAP, path) if path and _bwrap_starts(path) else None
    if sys.platform == "win32":
        launcher = os.path.join(os.path.dirname(os.path.abspath(__file__)), "windows_appcontainer.py")
        return Fence(APPCONTAINER, launcher) if os.path.isfile(launcher) and _appcontainer_starts(launcher) else None
    return None


def require(kind_of_work, *, endpoints=()):
    """Return the fence for this work or refuse it."""
    found = fence()
    if found is None:
        raise FenceUnavailable(kind_of_work + " needs a kernel fence and none is available on this platform "
                               "(macOS sandbox-exec, or Linux bubblewrap with user namespaces); refused")
    if found.kind in (BWRAP, APPCONTAINER) and endpoints:
        raise FenceUnavailable(kind_of_work + " declares network endpoints; this platform's fence cannot admit "
                               "only the local endpoint proxy, so it is refused")
    return found


def appcontainer_argv(launcher, argv, *, workspace, readable):
    """Run argv through the out-of-container launcher; argv[0] must be absolute."""
    program = shutil.which(argv[0]) or argv[0]
    if not os.path.isabs(program):
        raise FenceUnavailable("The Windows fence needs an absolute program path: " + str(argv[0]))
    out = [sys.executable, "-I", "-B", launcher, "--workspace", str(workspace)]
    for item in readable:
        out += ["--read", str(item)]
    return out + ["--", program, *argv[1:]]


def bwrap_argv(path, argv, *, workspace, readable):
    """bubblewrap argv: fresh namespaces (no network), read-only binds, the workspace writable."""
    out = [path, "--die-with-parent", "--new-session", "--unshare-all", "--cap-drop", "ALL",
           "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"]
    for prefix in LINUX_READ_PREFIXES:
        out += ["--ro-bind-try", prefix, prefix]
    for item in readable:
        out += ["--ro-bind", str(item), str(item)]
    # Bound last so it stays writable even when it lies under a read-only path.
    out += ["--bind", str(workspace), str(workspace), "--chdir", str(workspace), "--", *argv]
    return out
