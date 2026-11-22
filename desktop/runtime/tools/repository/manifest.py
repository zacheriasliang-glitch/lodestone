#!/usr/bin/env python3
"""Prepare an explicit committed-file allowlist for local export; review before use."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import export


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--commit", default="HEAD")
    parser.add_argument("--file", action="append", required=True, help="One relative file, repeat explicitly")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        root = args.repository.resolve()
        if args.commit.startswith("-"):
            raise ValueError("invalid commit")
        commit = export.git(root, "rev-parse", "--verify", args.commit + "^{commit}").decode().strip()
        tree = export.inventory(root, commit)
        selected = []
        for name in sorted(set(args.file)):
            export.safe_path(name)
            mode, kind, oid = tree[name]
            if mode not in ("100644", "100755") or kind != "blob":
                raise ValueError("select ordinary committed files only")
            selected.append({"path": name, "mode": mode, "blob_sha": oid})
        value = {"version": 1, "source_commit": commit, "files": selected, "generated": {}}
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(value, indent=2) + "\n")
        print(json.dumps({"status": "REVIEW_REQUIRED", "manifest": str(args.output), "files": len(selected)}))
        return 0
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        print(json.dumps({"status": "FAILED", "error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
