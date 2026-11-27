"""Bounded local readers for five additional toolkit families.

These invoke existing validators/recovery/reporting contracts. No mailbox
transport, host task execution, simulator run, candidate generation or deployment
is exposed. OperationBridge supplies the enclosing wall/output/process bounds.
"""
from __future__ import annotations

import argparse
import ast
from contextlib import contextmanager
import importlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import re
import sqlite3
import sys
import zipfile

from lodestone_nurarihyon.portable_files import read_bytes, absolute_path, guarded_directory

MAX_INPUT_BYTES = 2 * 1024 * 1024
MAX_RELEASE_FILE_BYTES = 32 * 1024 * 1024
MAX_RELEASE_TOTAL_BYTES = 128 * 1024 * 1024
MAX_RELEASE_FILES = 10000


def _spec(family, description, operation, inputs, effects):
    return {"family": family, "description": description, "command": ["loom-tools", "native", operation],
            "inputs": inputs, "effects": effects}


EXTRA_SPECS = {
    "mailbox.validate-envelope": _spec("mailbox", "Validate one immutable mailbox envelope without transport",
        "mailbox.validate-envelope", {"envelope": ("file", "--envelope"), "expected_id": ("text", "--expected-id"),
                                      "destination": ("text", "--destination")},
        "Read selected envelope; check destination, identity, canonical encoding and payload hash; no send, poll, acknowledgement or extraction"),
    "orchestrator.verify-return": _spec("orchestrator", "Verify a returned receipt against explicit envelope and independent carrier",
        "orchestrator.verify-return", {"project": ("directory", "--project"), "envelope": ("file", "--envelope"),
            "receipt": ("file", "--receipt"), "attachment": ("file", "--attachment"), "transport": ("file", "--transport")},
        "Read selected project and carrier files through native B_RETURN; no planning, host approval, task execution or state advancement"),
    "release.verify-download": _spec("release", "Verify the listed files of an already extracted release",
        "release.verify-download", {"package": ("directory", "--package"), "manifest": ("file", "--manifest")},
        "Read explicit release root and CONTENT_SHA256.json with the trusted native verifier; manifest at most 2 MiB, 10000 listed files at most 32 MiB each, 128 MiB total including manifest; no packaging, download, installation or publication"),
    "supplied-candidates.get": _spec("supplied-candidates", "Recover bytes from a pinned supplied-candidate store",
        "supplied-candidates.get", {"store": ("file", "--store"), "receipt": ("file", "--receipt"),
                                   "output": ("new-file", "--output")},
        "Read a quiescent existing database and external hash receipt; create new recovered file; no models, encoding search or candidate generation"),
    "porcelain.report-existing": _spec("porcelain", "Render an existing recorded episode when the optional environment is configured",
        "porcelain.report-existing", {"episode": ("file", "--episode"), "output_directory": ("new-file", "--output-directory")},
        "Create a new report directory from saved states with native report.py; no simulation, verification study or animation; optional environment required"),
}


def _porcelain_paths(root):
    package = root / "experiments/porcelain-simulator"
    environment = Path(os.environ.get("LODESTONE_PORCELAIN_VENV", root / ".venv-porcelain")).expanduser().resolve()
    interpreter = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return package, interpreter


def availability(package_root):
    root = Path(package_root).resolve()
    paths = {
        "mailbox.validate-envelope": [root / "tools/mailbox/mailbox.py"],
        "orchestrator.verify-return": [root / "orchestrator/bridge/__init__.py", root / "orchestrator/bridge/return_verify.py"],
        "release.verify-download": [root / "verify_download.py"],
        "supplied-candidates.get": [root / "src/lodestone_machinery/latent/cli.py"],
    }
    package, interpreter = _porcelain_paths(root)
    paths["porcelain.report-existing"] = [package / "porcelain/__init__.py", package / "porcelain/report.py", interpreter]
    return {operation: {"status": "AVAILABLE" if all(path.is_file() for path in required) else "UNAVAILABLE",
            "reason": ("Trusted native reader present; only the listed bounded operation is exposed."
                       if operation != "porcelain.report-existing" else
                       "Requires packaged report.py and configured LODESTONE_PORCELAIN_VENV (or package .venv-porcelain); dependencies are checked on invocation."),
            "selected_native_paths": [str(path) for path in required],
            "missing": [str(path) for path in required if not path.is_file()]}
            for operation, required in paths.items()}


def _read(path, maximum=MAX_INPUT_BYTES):
    return read_bytes(path, maximum)


def _load(root, relative, name):
    path = root / relative
    source = _read(path)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    # Execute exactly the source bytes held and checked by native custody,
    # without reopening the pathname through an import loader or bytecode cache.
    exec(compile(source, str(path), "exec"), module.__dict__)
    return module


def _check_episode(path):
    """Bound both ZIP inflation and NPY's declared allocation before numpy runs."""
    required = {"positions.npy", "surface_triangles.npy", "times.npy", "records.npy", "metadata.npy"}
    total = 0
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        names = [item.filename for item in members]
        if (not required <= set(names) or len(names) != len(set(names)) or len(members) > 64
                or sum(item.file_size for item in members) > 32 * 1024 * 1024
                or any(item.file_size > 16 * 1024 * 1024 for item in members)):
            raise ValueError("Recorded episode lacks required arrays or exceeds the report memory bound")
        for name in required:
            with archive.open(name) as stream:
                prefix = stream.read(8)
                if prefix[:6] != b"\x93NUMPY" or prefix[6:] not in (b"\x01\x00", b"\x02\x00", b"\x03\x00"):
                    raise ValueError("Unsupported recorded-array header")
                width = 2 if prefix[6] == 1 else 4
                length = int.from_bytes(stream.read(width), "little")
                if not 1 <= length <= 65536:
                    raise ValueError("Recorded-array header exceeds bound")
                try:
                    header = ast.literal_eval(stream.read(length).decode("utf-8" if prefix[6] == 3 else "latin1"))
                except (ValueError, SyntaxError, RecursionError) as exc:
                    raise ValueError("Invalid recorded-array header") from exc
                if type(header) is not dict or set(header) != {"descr", "fortran_order", "shape"}:
                    raise ValueError("Invalid recorded-array fields")
                dtype = re.fullmatch(r"[<>=|]?([biufcSUV])(\d+)", str(header["descr"]))
                shape = header["shape"]
                if (dtype is None or type(shape) is not tuple or len(shape) > 4
                        or any(type(n) is not int or not 0 <= n <= 32 * 1024 * 1024 for n in shape)):
                    raise ValueError("Unsupported or oversized recorded-array shape/type")
                size = int(dtype[2]) * (4 if dtype[1] == "U" else 1)
                for dimension in shape:
                    size *= dimension
                total += size
                if size > 16 * 1024 * 1024 or total > 32 * 1024 * 1024 or size > archive.getinfo(name).file_size - 8 - width - length:
                    raise ValueError("Recorded-array allocation exceeds its bounded retained bytes")


@contextmanager
def _cwd(path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def execute(operation, inputs, *, root, workspace):
    root = absolute_path(root) if os.name == "nt" else Path(root).resolve()
    if os.name == "nt":
        with guarded_directory(root):
            pass
    if operation not in EXTRA_SPECS or type(inputs) is not dict or set(inputs) != set(EXTRA_SPECS[operation]["inputs"]):
        raise ValueError("Supply exactly the selected native operation's named inputs")
    state = availability(root)[operation]
    if state["status"] != "AVAILABLE":
        return {"status": "UNAVAILABLE", "operation": operation, **state}
    for key, (kind, _) in EXTRA_SPECS[operation]["inputs"].items():
        value = inputs[key]
        if type(value) is not str or not value or "\x00" in value or len(value) > 32768:
            raise ValueError("Inputs must be nonempty bounded strings")
        path = Path(value)
        if kind == "file":
            _read(path)
        elif kind == "directory" and not path.is_dir():
            raise ValueError("Selected directory is unavailable")
        elif kind == "new-file" and (path.exists() or path.is_symlink() or not path.parent.is_dir()):
            raise ValueError("Output must be new, with an existing parent")
        if kind == "new-file" and path.resolve().is_relative_to(root):
            raise ValueError("Output cannot modify the immutable native package")

    if operation == "mailbox.validate-envelope":
        native = _load(root, "tools/mailbox/mailbox.py", "_loom_mailbox_validator")
        try:
            packet = native._decode(_read(inputs["envelope"], native.MAX_ENVELOPE))
            payload = native.validate(packet, expected_id=inputs["expected_id"], expected_to=inputs["destination"])
        except native.MailboxError as exc:
            raise ValueError(str(exc)) from exc
        return {"status": "VALIDATED", "id": packet["id"], "destination": packet["to"],
                "kind": packet["kind"], "payload_bytes": len(payload), "payload_sha256": packet["sha256"],
                "scope": "Envelope byte validation only; no transport, authenticated authorship or content endorsement"}

    if operation == "orchestrator.verify-return":
        project = Path(inputs["project"]).resolve()
        selected = {key: Path(inputs[key]).resolve() for key in ("envelope", "receipt", "attachment", "transport")}
        if any(not path.is_relative_to(project) for path in selected.values()):
            raise ValueError("Selected carrier and receipt files must be inside the explicit project root")
        envelope = json.loads(_read(selected["envelope"]))
        if (type(envelope) is not dict or type(envelope.get("requested_root")) is not str
                or Path(envelope["requested_root"]).resolve() != project):
            raise ValueError("Envelope requested root must equal the explicitly selected project")
        sys.path.insert(0, str(root / "orchestrator"))
        try:
            native = importlib.import_module("bridge")
            if Path(native.__file__).resolve() != root / "orchestrator/bridge/__init__.py":
                raise ValueError("Loaded orchestrator does not match the trusted package")
            with _cwd(project):
                return native.verify_return_files(project, str(selected["envelope"]), str(selected["receipt"]),
                    observed_root=Path.cwd(), attachment_path=str(selected["attachment"]), transport_plan_path=str(selected["transport"]))
        finally:
            sys.path.pop(0)

    if operation == "release.verify-download":
        from .loom_operations import _selected_path
        package = _selected_path(inputs["package"], directory=True)
        if _selected_path(inputs["manifest"]) != package / "CONTENT_SHA256.json":
            raise ValueError("Select this release's exact CONTENT_SHA256.json")
        native = _load(root, "verify_download.py", "_loom_release_validator")
        total, files = 0, 0
        def read_release(path):
            nonlocal total, files
            files += 1
            if files > MAX_RELEASE_FILES + 1:  # The manifest is one additional read.
                raise ValueError("Release verification exceeds its listed-file bound")
            raw = _read(path, MAX_RELEASE_FILE_BYTES)
            total += len(raw)
            if total > MAX_RELEASE_TOTAL_BYTES:
                raise ValueError("Release verification exceeds its total byte bound")
            return raw
        with guarded_directory(package):
            return native.verify(package, read_file=read_release)

    if operation == "supplied-candidates.get":
        from .latent.cli import get
        return get(store_path=inputs["store"], receipt_path=inputs["receipt"], output_path=inputs["output"], max_seconds=60)

    # Only recorded arrays are rendered. Bound decompression before optional
    # numpy/matplotlib imports; no solver module or user-selected executable.
    _check_episode(inputs["episode"])
    output = Path(inputs["output_directory"]).resolve()
    output.mkdir()
    package, interpreter = _porcelain_paths(root)
    env = {**os.environ, "PYTHONPATH": str(package), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
           "MPLCONFIGDIR": str(output / ".matplotlib"), "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"}
    result = subprocess.run([str(interpreter), "-B", "-m", "porcelain.report", str(Path(inputs["episode"]).resolve()),
                             "--out", str(output)], env=env, cwd=output, timeout=100, check=False)
    artifact = output / "simulation_summary.png"
    return {"status": "REPORTED" if result.returncode == 0 and artifact.is_file() else "REPORT_FAILED",
            "returncode": result.returncode, "artifact": str(artifact) if artifact.is_file() else None,
            "output_directory": str(output), "simulation_steps_run": 0,
            "scope": "Rendering existing recorded states; no new physics, validation study or semantic-learning claim"}


def main(argv=None, *, root=None, workspace=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    for operation, descriptor in EXTRA_SPECS.items():
        command = commands.add_parser(operation, help=descriptor["description"])
        for key, (_, flag) in descriptor["inputs"].items():
            command.add_argument(flag, dest=key, required=True)
    args = vars(parser.parse_args(argv))
    operation = args.pop("operation")
    try:
        result = execute(operation, args, root=root or Path(__file__).resolve().parents[2], workspace=workspace)
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, ImportError,
            subprocess.SubprocessError, sqlite3.DatabaseError, zipfile.BadZipFile) as exc:
        result = {"status": "ERROR", "error": type(exc).__name__, "reason": str(exc)[:2000]}
    print(json.dumps(result, indent=2, ensure_ascii=True))
    return 0 if result.get("status") in {"VALIDATED", "COMPLETE", "VERIFIED", "EXACT_RECOVERY", "REPORTED"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
