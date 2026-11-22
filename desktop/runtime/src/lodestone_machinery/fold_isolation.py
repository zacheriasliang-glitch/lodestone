"""A local, provider-free fold consumer with a fail-closed macOS boundary.

This launcher trusts the host, this module, its interpreter and the supplied
runtime sources. It does not turn arbitrary host code into trusted code. The
consumer gets one checked fold and a fresh writable workspace; provenance and
captures belong to the controller. No network or provider invocation is made.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import resource
import selectors
import signal
import stat
import subprocess
import sys
import sysconfig
import time


class IsolationError(ValueError):
    """Preparation failed; no unsandboxed consumer is allowed as a fallback."""


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _plain_path(value, *, exists=True):
    if ".." in Path(value).parts:
        raise IsolationError("Parent traversal in path")
    path = Path(os.path.abspath(os.fspath(value)))
    # Do not silently resolve a user-controlled alias into an allowed tree.
    for part in [path, *path.parents]:
        if part.is_symlink():
            raise IsolationError("Symlink in path: " + str(part))
    if exists and not path.exists():
        raise IsolationError("Missing path: " + str(path))
    return path


def _regular_bytes(path, *, maximum=8 * 1024 * 1024):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise IsolationError("Expected a singly linked regular file: " + str(path))
        if info.st_size > maximum:
            raise IsolationError("File exceeds the preparation size limit")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read(maximum + 1)
            if len(data) > maximum:
                raise IsolationError("File grew past the preparation size limit")
            return data
    finally:
        os.close(descriptor)


def _capsule(capsule, expected_sha256, expected_bytes):
    root = _plain_path(capsule)
    if not root.is_dir() or {p.name for p in root.iterdir()} != {"source", "workspace", "runtime"}:
        raise IsolationError("Capsule must contain exactly source, workspace and runtime")
    for folder in (root / "source", root / "workspace", root / "runtime"):
        if folder.is_symlink() or not folder.is_dir():
            raise IsolationError("Capsule members must be real directories")
    if list((root / "workspace").iterdir()):
        raise IsolationError("Consumer workspace must be fresh and empty")
    if {p.name for p in (root / "source").iterdir()} != {"fold.json"}:
        raise IsolationError("Source must contain only fold.json")
    fold = _regular_bytes(root / "source" / "fold.json")
    if type(expected_bytes) is not int or expected_bytes < 1 or len(fold) != expected_bytes:
        raise IsolationError("Fold size mismatch or empty fold")
    if not isinstance(expected_sha256, str) or _hash(fold) != expected_sha256:
        raise IsolationError("Fold digest mismatch")
    runtime_files = {}
    runtime_bytes = 0
    for item in (root / "runtime").rglob("*"):
        if item.is_symlink():
            raise IsolationError("Runtime symlink is forbidden")
        if item.is_dir():
            continue
        if item.suffix != ".py":
            raise IsolationError("Runtime contains a non-Python source file")
        data = _regular_bytes(item, maximum=16 * 1024 * 1024)
        runtime_bytes += len(data)
        if runtime_bytes > 16 * 1024 * 1024:
            raise IsolationError("Runtime exceeds the 16 MiB preparation limit")
        runtime_files[str(item.relative_to(root / "runtime"))] = _hash(data)
    return root, runtime_files


def _rule(kind, path):
    value = str(path)
    if any(character in value for character in ('"', "\\", "\n", "\r", "\x00")):
        raise IsolationError("Path cannot be represented in the sandbox profile")
    return '(' + kind + ' "' + value + '")'


def _runtime_paths():
    executable = Path(sys.executable).resolve(strict=True)
    stdlib = Path(sysconfig.get_path("stdlib")).resolve(strict=True)
    # CPython's normal macOS framework, or its standalone shared library.
    libraries = []
    framework = Path(sys.base_prefix) / "Python"
    if framework.is_file():
        libraries.append(framework.resolve())
        # python.org's bin/python is a launch stub that posix_spawns this real
        # binary. Launch it directly so the boundary can deny process-fork.
        framework_executable = Path(sys.base_prefix) / "Resources/Python.app/Contents/MacOS/Python"
        if framework_executable.is_file():
            executable = framework_executable.resolve(strict=True)
    library_name = sysconfig.get_config_var("LDLIBRARY")
    library_dir = sysconfig.get_config_var("LIBDIR")
    if library_name and library_dir:
        library = Path(library_dir) / library_name
        if library.is_file():
            libraries.append(library.resolve())
    return executable, stdlib, sorted(set(libraries))


def _profile(root, executable, stdlib, libraries):
    literals = [Path("/"), executable, *libraries,
                root / "source" / "fold.json", Path("/dev/null"), Path("/dev/urandom"), Path("/dev/random")]
    trees = [stdlib, root / "runtime", root / "workspace", Path("/usr/lib"), Path("/System/Library/Frameworks"),
             Path("/System/Library/PrivateFrameworks"), Path("/System/Library/dyld")]
    # Metadata needed for Python path resolution, limited to those paths and
    # their ancestors. This never grants data reads on ancestor descendants.
    ancestors = set()
    for path in [*literals, *trees, root / "source"]:
        ancestors.update(path.parents)
        ancestors.add(path)
    return "\n".join([
        "(version 1)", "(deny default)",
        "(allow process-exec " + _rule("literal", executable) + ")",
        "(allow file-read-metadata " + " ".join(_rule("literal", p) for p in sorted(ancestors)) + ")",
        "(allow file-read* " + " ".join([*(_rule("literal", p) for p in literals), *(_rule("subpath", p) for p in trees)]) + ")",
        "(deny file-read* " + _rule("subpath", stdlib / "site-packages") + ")",
        "(allow file-write* " + _rule("subpath", root / "workspace") + ")",
        "(allow file-write-data (literal \"/dev/null\"))",
        "(deny network*)", "(deny process-fork)",
    ]) + "\n"


def launch_consumer(capsule, code, capture_dir, *, expected_sha256, expected_bytes,
                    forbidden_paths=(), timeout=20, max_output_bytes=1024 * 1024):
    """Run Python code with only the fold, trusted runtime and new workspace.

    ``forbidden_paths`` are evidence labels, never read grants. They are checked
    against the capsule and runtime allowance before launching. The caller must
    supply actual denied-read probes to establish behavior for those files.
    Captures are outside the capsule and contain the exact profile and limits.
    Same-interpreter exec is permitted by Seatbelt (needed for initial exec);
    it retains the sandbox. Fork and other executables are denied.
    """
    if platform.system() != "Darwin" or not Path("/usr/bin/sandbox-exec").is_file():
        raise IsolationError("macOS sandbox-exec is required; no fallback")
    if not isinstance(code, str) or not code or len(code.encode()) > 128 * 1024:
        raise IsolationError("Consumer code must be nonempty and at most 128 KiB")
    if type(timeout) not in (int, float) or not 0 < timeout <= 60:
        raise IsolationError("Timeout must be positive and at most 60 seconds")
    if type(max_output_bytes) is not int or not 1 <= max_output_bytes <= 4 * 1024 * 1024:
        raise IsolationError("Output limit must be between 1 byte and 4 MiB")
    root, runtime_files = _capsule(capsule, expected_sha256, expected_bytes)
    captures = _plain_path(capture_dir, exists=False)
    if captures == root or root in captures.parents or captures in root.parents:
        raise IsolationError("Capture directory must be separate from the capsule")
    if captures.exists():
        raise IsolationError("Capture directory must be new")
    executable, stdlib, libraries = _runtime_paths()
    forbidden = [Path(os.path.abspath(os.fspath(p))) for p in forbidden_paths]
    allowed_trees = [root, stdlib, Path("/usr/lib"), Path("/System/Library/Frameworks"),
                     Path("/System/Library/PrivateFrameworks"), Path("/System/Library/dyld")]
    for path in forbidden:
        if any(path == p or p in path.parents or path in p.parents for p in allowed_trees) or path in [executable, *libraries]:
            raise IsolationError("Forbidden path intersects a required readable tree: " + str(path))
    policy = _profile(root, executable, stdlib, libraries)
    captures.mkdir(mode=0o700)
    (captures / "consumer.sb").write_text(policy, encoding="utf-8")
    (captures / "consumer.sb").chmod(0o400)
    bootstrap = ("import sys\nsys.path.insert(0, " + repr(str(root / "runtime")) + ")\n" + code)
    environment = {"HOME": str(root / "workspace"), "TMPDIR": str(root / "workspace"),
                   "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
    cpu_limit = max(1, min(60, int(timeout) + 1))

    def limits():
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_limit, cpu_limit))
        resource.setrlimit(resource.RLIMIT_FSIZE, (8 * 1024 * 1024, 8 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    started = time.monotonic()
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    process = subprocess.Popen(["/usr/bin/sandbox-exec", "-f", str(captures / "consumer.sb"),
                                str(executable), "-I", "-S", "-B", "-c", bootstrap],
                               cwd=root / "workspace", env=environment, close_fds=True,
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True, preexec_fn=limits)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    output = {"stdout": bytearray(), "stderr": bytearray()}
    stop = None
    try:
        while selector.get_map() or process.poll() is None:
            if time.monotonic() - started >= timeout:
                stop = "TIMEOUT"
                break
            for key, _ in selector.select(min(0.1, max(0, timeout - (time.monotonic() - started)))):
                data = os.read(key.fileobj.fileno(), 65536)
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                remaining = max_output_bytes - sum(len(value) for value in output.values())
                output[key.data].extend(data[:remaining])
                if len(data) > remaining:
                    stop = "OUTPUT_LIMIT"
                    break
            if stop:
                break
        if stop and process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        returncode = process.wait(timeout=2)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        selector.close()
        process.stdout.close()
        process.stderr.close()
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    receipt = {"schema": "lodestone.fold-isolation.v1", "status": stop or ("COMPLETED" if returncode == 0 else "FAILED"),
               "returncode": returncode, "fold_sha256": expected_sha256, "fold_bytes": expected_bytes,
               "runtime_files": runtime_files, "code_sha256": _hash(bootstrap.encode()),
               "profile_sha256": _hash(policy.encode()), "interpreter": str(executable),
               "forbidden_paths": [str(p) for p in forbidden], "wall_seconds": time.monotonic() - started,
               "child_cpu_seconds": after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime,
               "max_output_bytes": max_output_bytes, "output_bytes": sum(len(value) for value in output.values()),
               "wall_limit_seconds": timeout, "cpu_limit_seconds": cpu_limit,
               "file_limit_bytes": 8 * 1024 * 1024,
               "model_calls": None, "model_call_measurement": "NOT_INSTRUMENTED_BY_LAUNCHER",
               "provider_dispatch": "NONE_IN_LAUNCHER",
               "boundary": {"network": "DENIED", "fork": "DENIED", "extra_executables": "DENIED",
                            "same_interpreter_exec": "ALLOWED_WITH_INHERITED_SANDBOX", "environment": "SCRUBBED",
                            "inherited_fds": "CLOSED_EXCEPT_STANDARD_IO", "writes": "WORKSPACE_ONLY_PLUS_DEV_NULL",
                            "ipc": "DEFAULT_DENY", "metadata": "ALLOWLIST_AND_ANCESTORS", "sysctl_read": "DEFAULT_DENY"},
               "limits": ["Trusted host, kernel, interpreter and packaged Python runtime; no hostile concurrent host mutation.",
                          "OS libraries and Python stdlib are readable; site-packages is denied.",
                          "No hard aggregate workspace-size or memory limit; per-file size, CPU, wall and capture limits apply.",
                          "Arbitrary trusted Python code can compute locally; the launcher does not count local model calls.",
                          "Historical references inside fold text are visible as text, without reopening their files."]}
    for name, data in output.items():
        (captures / (name + ".bin")).write_bytes(data)
    (captures / "receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return receipt
