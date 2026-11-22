"""Local service controls; startup registration remains a separate host action."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

from .common import read_bytes, strict_json
from .controller import Controller
from .registry import describe


def main(argv=None):
    supplied = list(sys.argv[1:] if argv is None else argv)
    if supplied and supplied[0] in ("configure", "startup"):
        import runpy
        helper = Path(__file__).resolve().parents[2] / "tools/nurarihyon/configure.py"
        return runpy.run_path(str(helper))["main"](supplied)
    parser = argparse.ArgumentParser(description="Nurarihyon local application control core; configure/startup prepare explicit user-owned settings")
    parser.add_argument("--home", type=Path, help="Dedicated durable local state directory")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init"); init.add_argument("--policy", type=Path)
    commands.add_parser("registry")
    commands.add_parser("identity", help="Read current code and interpreter/launcher pins; no state or service change")
    capture = commands.add_parser("capture-document", help="Prepare an inert UTF-8 document source; no task or approval")
    capture.add_argument("--document", type=Path, required=True)
    capture.add_argument("--title", required=True)
    capture.add_argument("--output", type=Path, required=True)
    corpus = commands.add_parser("capture-corpus", help="Pin an explicitly selected local corpus; no task or approval")
    corpus.add_argument("--root", type=Path, required=True)
    corpus.add_argument("--spec", type=Path, required=True)
    corpus.add_argument("--output", type=Path, required=True)
    corpus.add_argument("--observation", action="store_true", help="Retain missing historical declarations as unresolved")
    prepare = commands.add_parser("prepare", help="Prepare an inert plan from a verified export; no live state access")
    prepare.add_argument("--package", type=Path, required=True)
    prepare.add_argument("--descriptor", type=Path, required=True)
    prepare.add_argument("--source", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--limit", type=int, default=4)
    submit = commands.add_parser("submit")
    submit.add_argument("--source", type=Path, required=True)
    submit.add_argument("--toolkit", choices=("pando.profile/v1", "bezoar.document/v1", "bezoar.reconciliation/v1", "bezoar.reconciliation-capture/v1", "bezoar.reconciliation-observation/v1"), default="pando.profile/v1")
    submit.add_argument("--key", required=True); submit.add_argument("--priority", type=int, default=0)
    submit.add_argument("--memory", type=Path, help="Verify and attach existing outcome context; does not grant approval")
    submit.add_argument("--reuse-reconciliation", action="store_true",
                        help="Pin the latest same-corpus reconciliation and retain inert routing; exact approval still required")
    context = commands.add_parser("context", help="Read current source-backed context; no archive restore or task creation")
    context.add_argument("--source", type=Path, required=True)
    context.add_argument("--memory", type=Path)
    context.add_argument("--limit", type=int, default=4)
    remember = commands.add_parser("remember", help="Retain a verified terminal outcome in a dedicated local-memory catalogue")
    remember.add_argument("task")
    remember.add_argument("--memory", type=Path, required=True)
    approve = commands.add_parser("approve")
    approve.add_argument("task"); approve.add_argument("--plan-sha", required=True)
    status = commands.add_parser("status"); status.add_argument("--task")
    for name in ("run", "stop", "clear-stop", "recover", "verify", "view", "board-path"):
        commands.add_parser(name)
    serve = commands.add_parser("serve"); serve.add_argument("--seconds", type=int, default=60)
    daemon = commands.add_parser("daemon")
    daemon.add_argument("--log-file", type=Path, required=True)
    daemon.add_argument("--expected-code-sha", required=True)
    daemon.add_argument("--expected-environment-sha", required=True)
    retry = commands.add_parser("retry"); retry.add_argument("task")
    export = commands.add_parser("export"); export.add_argument("--output", type=Path, required=True)
    restore = commands.add_parser("restore")
    restore.add_argument("--package", type=Path, required=True)
    restore.add_argument("--descriptor", type=Path, required=True)
    restore.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "registry":
            result = describe()
        elif args.command == "identity":
            from .controller import code_identity
            from .deployment import environment_identity, environment_record
            result = {"code_sha256": code_identity(), "environment_sha256": environment_identity(),
                      "dependencies": environment_record(), "authority": "NONE", "service_started": False}
        elif args.command == "capture-corpus":
            if args.home is not None:
                raise ValueError("capture-corpus does not access runtime state")
            from lodestone_machinery.bezoar_capture import prepare, prepare_observation
            operation = prepare_observation if args.observation else prepare
            result = operation(args.root, strict_json(read_bytes(args.spec, 65536)), args.output)
        elif args.command == "capture-document":
            if args.home is not None:
                raise ValueError("capture-document does not access runtime state")
            from .documents import prepare_document
            result = prepare_document(args.document, args.title, args.output)
        elif args.command == "prepare":
            if args.home is not None:
                raise ValueError("prepare uses a pinned export, not --home live state")
            from .preparation import prepare
            result = prepare(args.package, args.descriptor, args.source, args.output, limit=args.limit)
        elif args.command == "restore":
            from .portable import restore_package
            result = restore_package(args.package, args.descriptor, args.output)
        else:
            if args.home is None:
                raise ValueError("An explicit --home state directory is required")
            if args.command == "init":
                policy = strict_json(read_bytes(args.policy)) if args.policy else None
                result = Controller.initialize(args.home, policy).status()
            else:
                controller = Controller(args.home)
                if args.command == "submit":
                    result = controller.submit(args.source, key=args.key, priority=args.priority,
                                               memory_root=args.memory, toolkit=args.toolkit,
                                               reuse_reconciliation=args.reuse_reconciliation)
                elif args.command == "context":
                    from .context import prepare_current
                    result = prepare_current(controller, args.source, memory_root=args.memory, limit=args.limit)
                elif args.command == "remember":
                    from .outcome_memory import retain_outcome
                    from .context import check_context_home
                    check_context_home(controller)
                    with controller.locked():
                        result = retain_outcome(controller, args.task, args.memory)
                elif args.command == "approve":
                    result = controller.approve(args.task, args.plan_sha)
                elif args.command == "status":
                    result = controller.status(args.task)
                elif args.command == "run":
                    result = controller.run_once()
                elif args.command == "serve":
                    result = controller.serve(seconds=args.seconds)
                elif args.command == "daemon":
                    from .service import run_service
                    result = run_service(controller, log_file=args.log_file,
                                         expected_code_sha=args.expected_code_sha,
                                         expected_environment_sha=args.expected_environment_sha)
                elif args.command == "stop":
                    result = controller.stop()
                elif args.command == "clear-stop":
                    result = controller.clear_stop()
                elif args.command == "retry":
                    result = controller.retry(args.task)
                elif args.command == "recover":
                    result = controller.recover()
                elif args.command == "verify":
                    with controller.locked():
                        result = controller.verify()
                elif args.command in ("view", "board-path"):
                    from .inspection import ReadOnlyBoard
                    with controller.locked():
                        controller.verify()
                        board = controller.active_directory() / "pando"
                        result = ReadOnlyBoard(board).state() if args.command == "view" else {"path": str(board), "mode": "Published snapshot; do not write directly"}
                elif args.command == "export":
                    from .portable import export_package
                    result = export_package(controller, args.output, Path(__file__).resolve().parents[2])
        print(json.dumps(result, sort_keys=True, ensure_ascii=False, allow_nan=False))
        return 2 if (result.get("state") in ("REJECTED", "INTERRUPTED", "REPLAN_REQUIRED")
                     or result.get("status") in ("BLOCKED_STATE", "BLOCKED_RESOURCE", "WINDOW_TOO_SHORT",
                                                 "RUNTIME_CHANGED", "RECOVERY_REQUIRED", "SERVICE_ERROR", "INTERRUPTED",
                                                 "ENVIRONMENT_CHANGED", "STATE_BUSY", "EXECUTOR_ERROR", "EXECUTOR_LIMIT", "RESOURCE_LIMIT")) else 0
    except (ValueError, OSError, RuntimeError, TypeError, KeyError, sqlite3.Error) as exc:
        if args.command == "daemon":
            # Pre-logger setup failures have a stable distinct launchd exit
            # code. No exception messages, state paths or source data are logged.
            print(json.dumps({"stage": "DAEMON_SETUP", "error": type(exc).__name__}), file=sys.stderr)
            return 71
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)[:1500]}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
