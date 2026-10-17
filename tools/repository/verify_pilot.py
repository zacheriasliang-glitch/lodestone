#!/usr/bin/env python3
"""Verify a local export by restoring its Git bundle into a new temporary repository."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import tarfile
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import export


def verify(directory):
    directory = Path(directory).resolve()
    state = json.loads((directory / "private-state.json").read_text())
    for name, key in (("private-repository.bundle", "bundle_sha256"),
                      ("clean-harness.tar.gz", "clean_archive_sha256")):
        digest = hashlib.sha256()
        with (directory / name).open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != state[key]:
            raise ValueError("hash mismatch: " + name)
    with tempfile.TemporaryDirectory(prefix="lodestone-export-verify-") as temporary:
        mirror = Path(temporary)
        export.git(mirror, "init", "--bare")
        export.git(mirror, "fetch", str(directory / "private-repository.bundle"), "refs/*:refs/*")
        restored_refs = export.git(mirror, "show-ref").decode().splitlines()
        if sorted(restored_refs) != sorted(state["private_refs"]):
            raise ValueError("restored Git refs differ from the export ledger")
        export.git(mirror, "fetch", str(directory / "private-repository.bundle"), "HEAD")
        actual = export.git(mirror, "rev-parse", "FETCH_HEAD").decode().strip()
        if actual != state["private_head"]:
            raise ValueError("restored HEAD mismatch")
        export.git(mirror, "fsck", "--full", "--strict")
    with tarfile.open(directory / "clean-harness.tar.gz") as archive:
        observed = {}
        checksums = None
        for member in archive:
            if member.name != "CONTENT_SHA256.json":
                export.safe_path(member.name)
            if not member.isfile() or member.name in observed:
                raise ValueError("unexpected archive member: " + member.name)
            data = archive.extractfile(member).read()
            observed[member.name] = hashlib.sha256(data).hexdigest()
            if member.name == "CONTENT_SHA256.json":
                checksums = json.loads(data)
        if checksums is None or set(observed) != set(checksums) | {"CONTENT_SHA256.json"}:
            raise ValueError("archive member set differs from manifest")
        export.validate_file_tree(observed)
        if any(observed[name] != digest for name, digest in checksums.items()):
            raise ValueError("archive content differs from manifest")
    return {"status": "VERIFIED", "restored_head": actual, "refs": len(restored_refs), "files": len(checksums),
            "scope": "Fresh Git object recovery, strict fsck and clean archive bytes; no user program executed"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(verify(args.directory), indent=2))
        return 0
    except (OSError, ValueError, KeyError, RuntimeError, tarfile.TarError) as exc:
        print(json.dumps({"status": "FAILED", "error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
