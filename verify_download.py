#!/usr/bin/env python3
"""Verify the listed release files using Python's standard library."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re


def verify(root):
    root = Path(root).resolve()
    manifest = root / "CONTENT_SHA256.json"
    if manifest.is_symlink():
        raise ValueError("Checksum manifest must not be a symbolic link")
    checksums = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(checksums, dict) or not checksums:
        raise ValueError("Empty or invalid checksum manifest")
    failures = []
    total = 0
    for name, expected in checksums.items():
        rel = PurePosixPath(name)
        if (not isinstance(name, str) or not name or rel.is_absolute()
                or str(rel) != name or "\\" in name or ":" in name
                or any(x in ("..", ".git") for x in rel.parts)
                or not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected)):
            raise ValueError("Unsafe or invalid checksum entry")
        path = root
        for part in rel.parts:
            path = path / part
            if path.is_symlink():
                raise ValueError("Symbolic link in checksum path: " + name)
        if not path.is_file():
            failures.append({"path": name, "reason": "missing regular file"})
            continue
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                total += len(block)
                digest.update(block)
        if digest.hexdigest() != expected:
            failures.append({"path": name, "reason": "SHA-256 mismatch"})
    return {"status": "VERIFIED" if not failures else "MISMATCH",
            "files": len(checksums), "bytes_read": total, "failures": failures,
            "scope": "Listed release files; user-added files are not covered. Hashes are not a publisher signature."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    try:
        result = verify(args.root)
    except (OSError, ValueError, TypeError) as exc:
        result = {"status": "ERROR", "error": str(exc)}
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "VERIFIED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
