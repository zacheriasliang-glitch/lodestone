"""Pure preparation of one user-login LaunchAgent; never an installer.

Paths are checked lexically without expansion, resolution or filesystem access.
This does not establish ownership, existence, executable identity, symlink
custody, or launchd admission. An explicitly authorized activation must inspect
those separately. The manifest authenticates nothing without an independent pin.
"""
import hashlib
from pathlib import PurePath, PurePosixPath
import plistlib
import re
import unicodedata


LABEL = "com.lodestone.nurarihyon"
MAX_PLIST_BYTES = 16384
MANIFEST_SCHEMA = "nurarihyon-startup-preparation/v1"


def _path(value, name):
    if not isinstance(value, (str, PurePath)):
        raise ValueError(name + " must be an absolute POSIX path")
    value = str(value)
    if (not value.startswith("/") or value == "/" or value.startswith("//")
            or value != str(PurePosixPath(value))
            or any(part in (".", "..", "~") for part in value.split("/"))
            or any(unicodedata.category(char) in ("Cc", "Cf", "Cs")
                   or ord(char) in (0xfffe, 0xffff) for char in value)
            or any(char in value for char in ("$", "`"))):
        raise ValueError(name + " must use an explicit canonical path without control characters or expansion")
    if len(value.encode("utf-8")) > 4096:
        raise ValueError(name + " exceeds the path byte bound")
    return value


def _overlap(first, second):
    first, second = PurePosixPath(first), PurePosixPath(second)
    return first == second or first in second.parents or second in first.parents


def _expected(runtime, state, log_file, code_sha, python, environment_sha, label):
    if type(label) is not str or label != LABEL:
        raise ValueError("Only the dedicated Nurarihyon LaunchAgent label is supported")
    if type(code_sha) is not str or re.fullmatch(r"[0-9a-f]{64}", code_sha) is None:
        raise ValueError("Expected code identity must be one lowercase SHA-256")
    if type(environment_sha) is not str or re.fullmatch(r"[0-9a-f]{64}", environment_sha) is None:
        raise ValueError("Expected interpreter/launcher identity must be one lowercase SHA-256")
    runtime, state = _path(runtime, "runtime"), _path(state, "state")
    log_file, python = _path(log_file, "log_file"), _path(python, "python")
    if re.fullmatch(r"python3(?:\.[0-9]+)?", PurePosixPath(python).name) is None:
        raise ValueError("Interpreter must be an explicit Python 3 executable, not a shell")
    if _overlap(runtime, state) or _overlap(log_file, runtime) or _overlap(log_file, state):
        raise ValueError("Runtime, dedicated state and event log must not overlap")
    return {
        "Label": label,
        "ProgramArguments": [python, "-B", runtime + "/tools/nurarihyon/run",
                             "--home", state, "daemon", "--log-file", log_file,
                             "--expected-code-sha", code_sha, "--expected-environment-sha", environment_sha],
        "WorkingDirectory": runtime,
        "EnvironmentVariables": {"PATH": "/usr/bin:/bin", "PYTHONNOUSERSITE": "1",
                                 "PYTHONDONTWRITEBYTECODE": "1"},
        "LimitLoadToSessionType": "Aqua",
        "RunAtLoad": True,
        "KeepAlive": False,
        "ProcessType": "Standard",
        "ThrottleInterval": 60,
        "ExitTimeOut": 45,
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": "/dev/null",
    }


def render_agent(runtime, state, log_file, code_sha, python, environment_sha, label=LABEL):
    """Return deterministic XML plist bytes; do not inspect or change the host.

    RunAtLoad applies at explicit load or subsequent GUI login. KeepAlive is
    false: the throttle is defensive, not permission for automatic retry. The
    service itself owns bounded event logging; launchd output is discarded.
    """
    value = _expected(runtime, state, log_file, code_sha, python, environment_sha, label)
    data = plistlib.dumps(value, fmt=plistlib.FMT_XML, sort_keys=True)
    if len(data) > MAX_PLIST_BYTES:
        raise ValueError("LaunchAgent exceeds the preparation byte bound")
    return data


class _UniqueDict(dict):
    def __setitem__(self, key, value):
        if key in self:
            raise ValueError("Duplicate plist key")
        super().__setitem__(key, value)


def _plain(value):
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def _same(actual, expected):
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(_same(actual[key], item) for key, item in expected.items())
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(_same(a, b) for a, b in zip(actual, expected))
    return actual == expected


def validate_agent(data, *, runtime, state, log_file, code_sha, python, environment_sha, label=LABEL):
    """Validate exact keys, typed values and argv against caller-supplied intent.

    Only the renderer's canonical XML serialization is accepted. This also
    refuses plist extensions or alternate XML interpretation not emitted here.
    Returned data is a fresh ordinary dictionary, not an activation receipt.
    """
    expected = _expected(runtime, state, log_file, code_sha, python, environment_sha, label)
    if type(data) is not bytes or not 0 < len(data) <= MAX_PLIST_BYTES:
        raise ValueError("LaunchAgent must be bounded XML plist bytes")
    try:
        actual = _plain(plistlib.loads(data, fmt=plistlib.FMT_XML, dict_type=_UniqueDict))
    except Exception as exc:
        raise ValueError("Invalid or duplicate-key XML plist") from exc
    if not _same(actual, expected):
        raise ValueError("LaunchAgent differs from the exact prepared intent")
    if data != plistlib.dumps(actual, fmt=plistlib.FMT_XML, sort_keys=True):
        raise ValueError("LaunchAgent must retain its canonical prepared XML bytes")
    return actual


def preparation_manifest(data, *, runtime, state, log_file, code_sha, python, environment_sha, label=LABEL):
    """Bind prepared bytes and intent without claiming installation or execution."""
    value = validate_agent(data, runtime=runtime, state=state, log_file=log_file,
                           code_sha=code_sha, python=python, environment_sha=environment_sha, label=label)
    return {
        "schema": MANIFEST_SCHEMA,
        "status": "PREPARED_NOT_INSTALLED",
        "label": value["Label"],
        "plist": {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()},
        "runtime": value["WorkingDirectory"],
        "state": value["ProgramArguments"][4],
        "log_file": value["ProgramArguments"][7],
        "python": value["ProgramArguments"][0],
        "expected_code_sha256": code_sha,
        "expected_environment_sha256": environment_sha,
        "activation": "NONE; explicit host activation and filesystem custody checks required",
        "automatic_restart": False,
        "research_or_remote_actions": False,
        "path_validation": "Lexical only; existence, ownership and symlink custody unverified",
    }


def validate_manifest(value, data, *, runtime, state, log_file, code_sha, python, environment_sha, label=LABEL):
    """Recompute the entire preparation manifest against externally supplied intent."""
    expected = preparation_manifest(data, runtime=runtime, state=state, log_file=log_file,
                                    code_sha=code_sha, python=python, environment_sha=environment_sha, label=label)
    if not _same(value, expected):
        raise ValueError("Startup preparation manifest differs from its bound intent")
    return expected
