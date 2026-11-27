#!/usr/bin/env python3
"""Build a reproducible source-complete preview download, independently of Git."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import zipfile

VERSION = "0.4.0"
TARGETS = {
    "macos-x86_64": ("macOS", "dependencies.lock.json"),
    "ubuntu-24.04-x86_64": ("Linux", "dependencies.ubuntu-24.04-x86_64.lock.json"),
    "windows-x86_64": ("Windows", "dependencies.windows-x86_64.lock.json"),
}
ROOT_FILES = ("install.py", "run_loom.py", "bootstrap_learning.py", "dependencies.py",
              "dependencies.lock.json", "dependencies.ubuntu-24.04-x86_64.lock.json",
              "dependencies.windows-x86_64.lock.json", "gui-dependencies.json", "platform_support.py",
              "windows-path-budget.json", "windows_preflight.py", "Enable Windows Long Paths.cmd",
              "Install Lodestone.command", "Install Lodestone.sh", "Install Lodestone.ps1",
              "Install Lodestone.cmd", "Enable Linux Command Fence.sh", "README.md",
              "THIRD_PARTY.md", "SOURCE_PROVENANCE.json", "LICENSE", "smoke.py",
              "build.py", "verify_payload.py")
EXECUTABLE_FILES = frozenset(("Install Lodestone.command", "Install Lodestone.sh",
                            "Enable Linux Command Fence.sh", "runtime/orchestrator/run",
                            "runtime/tools/machinery/run", "runtime/tools/machinery/yggdrasil_findings.py"))


def selected(source):
    source = Path(source).resolve()
    # The same allowlist used for export review is a mandatory build boundary.
    # Never package incidental user_data, private notes or an unreviewed file.
    spec = importlib.util.spec_from_file_location("desktop_export_verifier",
                                                 Path(__file__).with_name("verify_payload.py"))
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    verifier.verify(source)
    files = {}
    for name in ROOT_FILES:
        path = source / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("Missing distribution file: " + name)
        files[name] = path
    for directory in ("runtime", "gui"):
        for path in sorted((source / directory).rglob("*")):
            if path.is_symlink():
                raise ValueError("Source links are not distributable: " + str(path))
            if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            files[path.relative_to(source).as_posix()] = path
    return files


def build(source, destination, target="macos-x86_64"):
    if target not in TARGETS:
        raise ValueError("Unsupported distribution target: " + str(target))
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if destination == source or source in destination.parents:
        raise ValueError("Write downloads outside the source payload")
    files = selected(source)
    # Read once so the manifest and ZIP bind the same bytes even if a source
    # editor saves during a build. Exported bytes must still match provenance.
    contents = {name: path.read_bytes() for name, path in sorted(files.items())}
    provenance = json.loads(contents["SOURCE_PROVENANCE.json"])
    for name, identity in provenance["exported_files"].items():
        if hashlib.sha256(contents[name]).hexdigest() != identity["sha256"]:
            raise ValueError("Export changed while building: " + name)
    # A target download always exposes its selected pins at the stable installer path.
    contents["dependencies.lock.json"] = contents[TARGETS[target][1]]
    selected_lock = json.loads(contents["dependencies.lock.json"])
    lock_platform = selected_lock.get("platform", {})
    expected_system = {"macOS": "Darwin", "Windows": "Windows", "Linux": "Linux"}[TARGETS[target][0]]
    if (selected_lock.get("schema") != "lodestone-desktop-dependencies/v1"
            or lock_platform.get("system") != expected_system
            or lock_platform.get("machine") != "x86_64"
            or (expected_system == "Linux" and (lock_platform.get("distribution"), lock_platform.get("distribution_version")) != ("ubuntu", "24.04"))):
        raise ValueError("Dependency lock does not match the requested build target")
    manifest = {"schema": "lodestone-desktop-payload/v1", "version": VERSION,
                "platform": TARGETS[target][0], "architecture": "x86_64", "target": target,
                "files": {name: hashlib.sha256(content).hexdigest() for name, content in contents.items()}}
    data = json.dumps(manifest, indent=2, sort_keys=True).encode() + b"\n"
    prefix = "Lodestone-Desktop-" + VERSION
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / (prefix + "-" + target + ".zip")
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for name in sorted([*files, "MANIFEST.json"]):
            item = zipfile.ZipInfo(prefix + "/" + name, date_time=(2026, 1, 1, 0, 0, 0))
            # ZIP metadata must not depend on the host's Git checkout modes or
            # ZipInfo's native Windows/POSIX default. These are entrypoints.
            item.create_system = 3
            item.external_attr = (0o100755 if name in EXECUTABLE_FILES else 0o100644) << 16
            item.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(item, data if name == "MANIFEST.json" else contents[name])
    sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    (destination / "SHA256SUMS.txt").write_text(sha + "  " + archive.name + "\n")
    report = {"schema": "lodestone-desktop-build/v1", "version": VERSION,
              "archive": archive.name, "sha256": sha, "bytes": archive.stat().st_size,
              "target": target, "payload_files": len(files), "model_weights_bundled": False,
              "private_user_records_bundled": False}
    (destination / "BUILD.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", type=Path, default=Path(__file__).resolve().parent)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--target", choices=sorted(TARGETS), default="macos-x86_64")
    args = p.parse_args()
    print(json.dumps(build(args.source, args.output, args.target), indent=2))
