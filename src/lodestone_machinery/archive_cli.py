"""Origami/Yggdrasil exact file archives; every write uses a new destination."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from . import archive_format as fmt


def _roots(values):
    roots = {}
    for value in values:
        label, separator, path = value.partition("=")
        if not separator or not path or label in roots:
            raise fmt.ArchiveError("Each --root must be a unique LABEL=PATH")
        fmt.safe_path(label)
        if "/" in label:
            raise fmt.ArchiveError("Root label must be a single component")
        roots[label] = Path(path).expanduser()
    return roots


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="lodestone archive",
        description="Stream, deduplicate and verify exact content copies. Originals are retained.")
    operations = parser.add_subparsers(dest="operation", required=True)
    for name in ("plan", "pack"):
        sub = operations.add_parser(name)
        sub.add_argument("--root", action="append", required=True, metavar="LABEL=PATH",
                         help="Explicit directory root; repeat for multiple trees")
        sub.add_argument("--exclude-name", action="append", default=[],
                         help="Exclude this basename at any depth; .git is always excluded")
        sub.add_argument("--exclude-path", action="append", default=[],
                         help="Exact LABEL/relative path or subtree to omit explicitly")
        sub.add_argument("--max-entries", type=int, default=fmt.MAX_ENTRIES)
        sub.add_argument("--max-bytes", type=int, default=fmt.MAX_TOTAL_BYTES)
        sub.add_argument("--max-seconds", type=int, default=3600 if name == "plan" else 7200)
        if name == "pack":
            sub.add_argument("--destination", required=True, type=Path)
            sub.add_argument("--epoch", required=True)
            sub.add_argument("--authority-ref", required=True)
    for name in ("inspect", "list", "verify", "restore"):
        sub = operations.add_parser(name)
        sub.add_argument("--archive", type=Path, required=True, help="Archive directory, not its tar file")
        sub.add_argument("--expected-sha256", help="Independently recorded archive.json SHA-256")
        if name not in ("inspect", "list"):
            sub.add_argument("--max-seconds", type=int, default=7200)
        if name == "list":
            sub.add_argument("--prefix", default="", help="Manifest directory or exact path prefix")
            sub.add_argument("--limit", type=int, default=100)
            sub.add_argument("--offset", type=int, default=0)
        if name == "restore":
            sub.add_argument("--destination", type=Path, required=True)
            sub.add_argument("--path", action="append", dest="paths",
                             help="Exact manifest path or directory subtree; default all")
    args = parser.parse_args(argv)
    try:
        if args.operation in ("plan", "pack"):
            from .archive_pack import plan, pack
            roots = _roots(args.root)
            bounds = dict(exclude_names=tuple(dict.fromkeys((".git", *args.exclude_name))),
                          exclude_paths=tuple(args.exclude_path),
                          max_entries=args.max_entries, max_bytes=args.max_bytes,
                          max_seconds=args.max_seconds)
            if args.operation == "plan":
                result = plan(roots, **bounds)
            else:
                result = pack(roots, args.destination, epoch=args.epoch,
                              authority_ref=args.authority_ref, **bounds)
        elif args.operation in ("inspect", "list"):
            descriptor, manifest = fmt.load_archive(
                args.archive, expected_descriptor_sha256=args.expected_sha256)
            result = {"status": "METADATA_VALIDATED_PAYLOAD_UNVERIFIED",
                      "descriptor": descriptor, "epoch": manifest["epoch"],
                      "authority_ref": manifest["authority_ref"], "roots": manifest["roots"],
                      "excluded": manifest["excluded"], "metadata_scope": fmt.METADATA_SCOPE,
                      "descriptor_sha256": fmt.digest(fmt.canonical(descriptor))}
            if args.operation == "list":
                if not 1 <= args.limit <= 10000 or not 0 <= args.offset <= fmt.MAX_ENTRIES:
                    raise fmt.ArchiveError("list requires limit 1..10000 and a bounded nonnegative offset")
                prefix = fmt.safe_path(args.prefix) if args.prefix else ""
                matched = [entry for entry in manifest["entries"] if not prefix
                           or entry["path"] == prefix or entry["path"].startswith(prefix + "/")]
                end = args.offset + args.limit
                result = {"status": result["status"], "descriptor_sha256": result["descriptor_sha256"],
                          "prefix": prefix, "total_matches": len(matched), "offset": args.offset,
                          "entries": matched[args.offset:end],
                          "next_offset": end if end < len(matched) else None}
        else:
            from .archive_restore import verify, restore
            options = dict(expected_descriptor_sha256=args.expected_sha256,
                           max_seconds=args.max_seconds)
            if args.operation == "verify":
                result = verify(args.archive, **options)
            else:
                result = restore(args.archive, args.destination, paths=args.paths, **options)
        print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
        return 0
    except (ValueError, OSError, RuntimeError) as exc:
        print(json.dumps({"status": "REFUSED", "error": type(exc).__name__,
                          "message": str(exc)[:2000], "originals_retained": True}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
