"""JSON CLI for local durable source work."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

from .engine import Engine
from .inspection import inspect_job, export_nodes
from .nodes import NodeStore


def _lineage(argv):
    """Report the receipt corpus lineage verdict. This certifies nothing."""
    from .yggdrasil import check as check_lineage
    parser = argparse.ArgumentParser(prog="lodestone_machinery yggdrasil",
                                     description="Lifecycle lineage laws over the receipt corpus v0.1.0")
    parser.add_argument("--root", default=".", help="Repository root holding the receipts directory")
    parser.add_argument("--findings", action="store_true", help="Include every finding, not only the counts")
    args = parser.parse_args(argv)
    try:
        result = check_lineage(args.root)
    except (ValueError, OSError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)[:1000]}), file=sys.stderr)
        return 2
    if not args.findings:
        result = {key: value for key, value in result.items() if key != "findings"}
    print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
    return 2 if result["verdict"] == "VIOLATION" else 0


def main(argv=None):
    supplied = sys.argv[1:] if argv is None else argv
    if supplied and supplied[0] == "origami":
        from .origami_cli import main as origami_main
        return origami_main(supplied[1:])
    if supplied and supplied[0] == "yggdrasil":
        if len(supplied) > 1 and supplied[1] == "corpus-fold":
            from .corpus_fold_cli import main as corpus_fold_main
            return corpus_fold_main(supplied[2:])
        if len(supplied) > 1 and supplied[1] == "archive":
            from .archive_cli import main as archive_main
            return archive_main(supplied[2:])
        if len(supplied) > 1 and supplied[1] == "crystal":
            from .crystals_cli import main as crystal_main
            return crystal_main(supplied[2:])
        if len(supplied) > 1 and supplied[1] in ("canon", "pack", "inspect", "expand", "reenter"):
            from .yggdrasil_runtime import main as yggdrasil_main
            return yggdrasil_main(supplied[1:])
        return _lineage(supplied[1:])
    parser = argparse.ArgumentParser(description="Lodestone local source machinery 0.3.0; use 'origami' for the reasoning loop")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("submit", "run", "status", "maintain", "recover", "resume", "demo", "inspect", "export-nodes", "catalog-records"):
        command = commands.add_parser(name)
        command.add_argument("--workspace", required=True, help="Explicit directory for generated output and durable state")
        if name in ("run", "status", "recover", "resume", "inspect"):
            command.add_argument("job_id")
        if name == "export-nodes":
            command.add_argument("--claim", action="append", required=True, dest="claims")
        if name == "submit":
            command.add_argument("--root", required=True)
            command.add_argument("--source", action="append", required=True, dest="sources")
            command.add_argument("--query", required=True)
            command.add_argument("--max-passages", type=int, default=8)
            command.add_argument("--prediction-json", help="Optional frozen prediction JSON object")
        if name == "demo":
            command.add_argument("--root", default=str(Path(__file__).resolve().parent / "samples"))
            command.add_argument("--source", action="append", dest="sources")
            command.add_argument("--query", default="ceramic")
    args = parser.parse_args(argv)
    try:
        if args.command == "export-nodes":
            result = export_nodes(NodeStore(args.workspace), args.claims)
            print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
            return 2 if result["verdict"] == "ATTENTION" else 0
        engine = Engine(args.workspace)
        if args.command == "catalog-records":
            result = engine.records.catalog()
        elif args.command == "inspect":
            result = inspect_job(engine, args.job_id)
        elif args.command == "submit":
            prediction = json.loads(args.prediction_json) if args.prediction_json else None
            job_id = engine.submit(args.root, args.sources, args.query, prediction, args.max_passages)
            result = {"job_id": job_id, "state": "READY", "next": "Run this job to produce and inspect an artifact"}
        elif args.command == "demo":
            sources = args.sources or ["workshop.md", "repair.md"]
            job_id = engine.submit(args.root, sources, args.query)
            result = engine.run(job_id)
        elif args.command == "maintain":
            result = engine.maintain()
        else:
            result = getattr(engine, args.command)(args.job_id)
        print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
        failed = result.get("state") in ("FAILED", "REJECTED", "BLOCKED", "UNCERTAIN") or result.get("verdict") in ("BLOCKED", "ATTENTION")
        return 2 if failed else 0
    except (ValueError, OSError, RuntimeError, sqlite3.DatabaseError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)[:2000]}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
