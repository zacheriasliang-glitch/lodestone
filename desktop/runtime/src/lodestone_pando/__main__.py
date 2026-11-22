"""Portable Pando CLI; no model calls, automatic sync or implicit job execution."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
import tarfile

from .store import Board, encoded, read_file
from lodestone_machinery.organs._kernel import strict_load


def main(argv=None):
    parser = argparse.ArgumentParser(prog="lodestone pando")
    parser.add_argument("--home", type=Path, required=True, help="Explicit user-owned board directory")
    commands = parser.add_subparsers(dest="command", required=True)
    initialize = commands.add_parser("init")
    initialize.add_argument("--participants", type=Path, help="JSON list of user-owned attribution records; host required")
    for name in ("status", "verify"):
        commands.add_parser(name)
    p = commands.add_parser("serve"); p.add_argument("--port", type=int, default=8765)
    for name in ("ingest", "mailbox"):
        p = commands.add_parser(name)
        p.add_argument("--file", required=True); p.add_argument("--title", required=True)
        p.add_argument("--uri", required=True); p.add_argument("--author", required=True)
        if name == "ingest":
            p.add_argument("--kind", choices=("file", "notion", "github", "host"), default="file")
    p = commands.add_parser("post")
    for field in ("author", "shelf", "title", "body"):
        p.add_argument("--" + field, required=True)
    p.add_argument("--parent-id"); p.add_argument("--source", action="append", default=[])
    p = commands.add_parser("eat")
    p.add_argument("--source", action="append", required=True); p.add_argument("--question", required=True)
    p.add_argument("--author", required=True); p.add_argument("--title", required=True)
    for name in ("digest", "mutate"):
        p = commands.add_parser(name); p.add_argument("--parent", required=True)
        p.add_argument("--file", required=True); p.add_argument("--author", required=True)
        p.add_argument("--title", required=True)
    p = commands.add_parser("show"); p.add_argument("id"); p.add_argument("--source", action="store_true")
    p = commands.add_parser("apply-profile"); p.add_argument("id"); p.add_argument("--author", required=True)
    p = commands.add_parser("export"); p.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "serve":
            from .server import serve
            serve(args.home, args.port); return 0
        participants = strict_load(args.participants) if args.command == "init" and args.participants else None
        board = Board(args.home, participants=participants)
        if args.command in ("init", "status"):
            result = board.state()
        elif args.command == "verify":
            result = board.verify()
        elif args.command == "ingest":
            result = {"source_id": board.ingest(read_file(args.file), title=args.title, uri=args.uri, author=args.author, kind=args.kind)}
        elif args.command == "mailbox":
            result = {"source_id": board.import_mailbox(json.loads(read_file(args.file)), title=args.title, uri=args.uri, author=args.author)}
        elif args.command == "post":
            result = {"post_id": board.post(author=args.author, shelf=args.shelf, title=args.title,
                      body=args.body, parent_id=args.parent_id, source_ids=args.source)}
        elif args.command == "eat":
            result = {"artifact_id": board.eat(args.source, author=args.author, question=args.question, title=args.title)}
        elif args.command in ("digest", "mutate"):
            result = {"artifact_id": getattr(board, args.command)(args.parent, strict_load(args.file), author=args.author, title=args.title)}
        elif args.command == "show":
            result = board.source(args.id) if args.source else board.artifact(args.id)
        elif args.command == "apply-profile":
            result = board.apply_profile(args.id, author=args.author)
        elif args.command == "export":
            # SQLite backup obtains a consistent database snapshot even with a live UI.
            # Immutable organ files can only be added; copying all of them preserves
            # its dependency closure. This export is data, not a restore/extract tool.
            import tempfile
            with tempfile.TemporaryDirectory() as temp:
                copy = Path(temp) / "pando.sqlite3"
                with board.connection() as source:
                    destination = sqlite3.connect(copy)
                    try:
                        source.backup(destination)
                    finally:
                        destination.close()
                with args.output.open("xb") as stream:
                    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
                        archive.add(copy, arcname="pando/pando.sqlite3")
                        for name in ("captures", "kernel"):
                            archive.add(board.home / name, arcname="pando/" + name)
            import hashlib
            result = {"path": str(args.output.resolve()), "bytes": args.output.stat().st_size,
                      "sha256": hashlib.sha256(args.output.read_bytes()).hexdigest(),
                      "scope": "Private Pando data and organ records. Source code is a separate Git dependency."}
        print(encoded(result))
        return 0
    except (ValueError, TypeError, OSError, sqlite3.Error) as exc:
        print(encoded({"error": type(exc).__name__, "message": str(exc)[:1000]}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
