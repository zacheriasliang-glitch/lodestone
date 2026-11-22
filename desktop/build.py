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

VERSION = "0.4.0-preview.2"
ROOT_FILES = ("install.py", "run_loom.py", "bootstrap_learning.py", "dependencies.py",
              "dependencies.lock.json", "Install Lodestone.command", "README.md",
              "THIRD_PARTY.md", "SOURCE_PROVENANCE.json", "LICENSE", "smoke.py",
              "build.py", "verify_payload.py")


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


def build(source, destination):
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
    manifest = {"schema": "lodestone-desktop-payload/v1", "version": VERSION,
                "platform": "macOS", "architecture": "x86_64",
                "files": {name: hashlib.sha256(content).hexdigest() for name, content in contents.items()}}
    data = json.dumps(manifest, indent=2, sort_keys=True).encode() + b"\n"
    prefix = "Lodestone-Desktop-" + VERSION
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / (prefix + "-macos-x86_64.zip")
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for name in sorted([*files, "MANIFEST.json"]):
            item = zipfile.ZipInfo(prefix + "/" + name, date_time=(2026, 1, 1, 0, 0, 0))
            executable = name == "Install Lodestone.command" or (name in files and files[name].stat().st_mode & 0o111)
            item.external_attr = (0o100755 if executable else 0o100644) << 16
            item.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(item, data if name == "MANIFEST.json" else contents[name])
    sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    (destination / "SHA256SUMS.txt").write_text(sha + "  " + archive.name + "\n")
    report = {"schema": "lodestone-desktop-build/v1", "version": VERSION,
              "archive": archive.name, "sha256": sha, "bytes": archive.stat().st_size,
              "payload_files": len(files), "model_weights_bundled": False,
              "private_user_records_bundled": False}
    (destination / "BUILD.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", type=Path, default=Path(__file__).resolve().parent)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    print(json.dumps(build(args.source, args.output), indent=2))
