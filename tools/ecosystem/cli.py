"""Portable local toolkit. No accounts, services or network access are required."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import sqlite3
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
LIMIT = 2 * 1024 * 1024
MACHINERY = "tools/machinery/run"
CORE_FILES = (MACHINERY, "src/lodestone_machinery/__main__.py",
              "src/lodestone_machinery/engine.py", "src/lodestone_machinery/records.py",
              "tools/ecosystem/conscience.py", "tools/ecosystem/conscience_targets.py",
              "tools/ecosystem/conscience-policy.json")
WORKSPACE_OPS = {"submit", "run", "status", "maintain", "recover", "resume",
                 "demo", "inspect", "export-nodes", "catalog-records"}
ORIGAMI_OPS = {"ask", "start", "request", "advance", "run", "status", "inspect"}
PUBLIC_YGGDRASIL = {"inspect", "expand", "crystal", "archive", "corpus-fold", "canon", "pack", "reenter"}
TOOL_SCRIPTS = {
    "memory": "tools/local-memory/src/lodestone_memory.py",
    "continuity": "tools/continuity/checkpoint.py",
    "mailbox": "tools/mailbox/mailbox.py",
    "origami-fold": "tools/origami-fold/src/origami_fold.py",
    "orchestrator": "orchestrator/bootstrap.py",
    "receipt": "tools/machinery/append_receipt.py",
    "check": "tools/machinery/check_repository.py",
    "brief-check": "tools/machinery/check_brief.py",
    "registry-check": "tools/machinery/check_bezoar.py",
    "lineage-findings": "tools/machinery/yggdrasil_findings.py",
}
LOCAL_HELP = {
    "doctor": "Inspect core readiness and workspace availability without initializing either.",
    "context": "Read bounded local documentation; no external sources or private stores are read.",
    "toolkits": "Print the toolkit catalogue, entry commands and prerequisites.",
    "porcelain": "Optional simulator: run | report | verify | pair | verify_hands | report_pair.\n"
                 "Subcommand help and execution require the optional numerical environment.\n"
                 "See docs/porcelain.md for setup; this overview needs no optional packages.",
}


def default_workspace():
    return Path.home() / ".lodestone" / "workspace"


def read_bounded(path, limit=LIMIT):
    with Path(path).open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError(f"Read bound exceeded: {path}")
    return raw


def core_capabilities(root):
    files = {name: (root / name).is_file() and os.access(root / name, os.R_OK)
             for name in CORE_FILES}
    python_ok = sys.version_info >= (3, 10)
    return {"status": "READY" if python_ok and all(files.values()) else "ATTENTION",
            "python_supported": python_ok, "required_files": files,
            "sqlite_version": sqlite3.sqlite_version,
            "scope": "Required source files and standard-library availability; not a test-suite result"}


def workspace_state(workspace):
    ancestor = workspace
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    usable = ancestor.is_dir() and os.access(ancestor, os.W_OK | os.X_OK)
    return {"path": str(workspace), "exists": workspace.is_dir(),
            "status": "READY" if usable else "ATTENTION",
            "initialization": "Created only by an explicit runtime command",
            "path_check": "Writable directory or nearest existing parent" if usable
                          else "Existing path/parent is not a writable directory"}


def doctor_status(capabilities, workspace):
    return "READY" if capabilities["status"] == workspace["status"] == "READY" else "ATTENTION"


def doctor(root, workspace):
    """Inspect only the selected package and workspace; never initialize either."""
    capabilities = core_capabilities(root)
    state = workspace_state(workspace)
    return {"schema": "lodestone-doctor/v2", "observed_at": datetime.now(timezone.utc).isoformat(),
            "python": {"version": platform.python_version(), "executable": sys.executable},
            "package": str(root), "capabilities": capabilities, "workspace": state,
            "status": doctor_status(capabilities, state),
            "scope": "Portable local core. No Git checkout, private memory, mailbox, account or background service required.",
            "optional_porcelain": {"included": (root / "experiments/porcelain-simulator/porcelain").is_dir(),
                                   "environment_checked": False}}


def context(root):
    documents = []
    for name in ("README.md", "docs/conscience.md"):
        path = root / name
        if not path.is_file():
            documents.append({"path": str(path), "status": "ABSENT"})
            continue
        raw = read_bounded(path)
        text = raw.decode("utf-8")
        documents.append({"path": str(path), "bytes": len(raw),
                          "sha256": hashlib.sha256(raw).hexdigest(),
                          "text": text[:12000], "truncated": len(text) > 12000})
    return {"schema": "lodestone-context/v2", "documents": documents,
            "coverage": "Bounded local documentation only; no external sources read"}


def machinery_args(args, workspace, root):
    args = list(args)
    if args and args[0] in {"origami", "yggdrasil"} and platform.system() == "Windows":
        raise ValueError("Origami and Yggdrasil routes require POSIX file-locking and directory APIs. Use Linux or macOS for these commands; native Windows preview supports doctor, machinery demo and Conscience.")
    if args and args[0] in WORKSPACE_OPS:
        args[1:1] = ["--workspace", str(workspace)]
    elif len(args) > 1 and args[0] == "origami" and args[1] in ORIGAMI_OPS:
        args[2:2] = ["--workspace", str(workspace)]
    if args and args[0] == "yggdrasil":
        operation = args[1] if len(args) > 1 else ""
        help_requested = "--help" in args or "-h" in args
        has_root = "--root" in args or any(arg.startswith("--root=") for arg in args)
        if operation in {"canon", "pack", "reenter"} and not help_requested and not has_root:
            raise ValueError("Supply --root for your separately configured source corpus and canon; no laboratory adoption is inherited")
    return args


def main(argv=None):
    parser = argparse.ArgumentParser(description="Lodestone portable toolkit")
    parser.add_argument("--repo", type=Path, default=ROOT, help="Directory containing the extracted package")
    parser.add_argument("--workspace", type=Path, default=default_workspace(),
                        help="Local output and record directory (default: ~/.lodestone/workspace)")
    parser.add_argument("command", choices=("doctor", "context", "machinery", "origami", "yggdrasil",
                                            "wake", "conscience", "porcelain", "repository", "toolkits", *TOOL_SCRIPTS))
    parser.add_argument("args", nargs=argparse.REMAINDER)
    options = parser.parse_args(argv)
    try:
        if sys.version_info < (3, 10):
            raise ValueError("Python 3.10 or later is required")
        root = options.repo.expanduser().resolve()
        workspace = options.workspace.expanduser().resolve()
        command, args = options.command, options.args
        if command in LOCAL_HELP and (args in (["--help"], ["-h"]) or
                                       (command == "porcelain" and not args)):
            print(f"usage: lodestone.py [--repo PACKAGE] [--workspace DIR] {command}" +
                  (" SUBCOMMAND [ARGS]" if command == "porcelain" else ""))
            print("\n" + LOCAL_HELP[command])
            print("\nGlobal options go before the command.")
            return 0
        if command == "toolkits":
            if args:
                raise ValueError("toolkits takes no arguments")
            print(read_bounded(root / "tools/toolkits.json").decode("utf-8"))
            return 0
        if command in TOOL_SCRIPTS or command == "repository":
            if command == "repository":
                scripts = {"manifest": "manifest.py", "export": "export.py", "verify": "verify_pilot.py"}
                if not args or args == ["--help"] or args == ["-h"]:
                    print("repository manifest | export | verify; append --help for each operation")
                    return 0
                if args[0] not in scripts:
                    raise ValueError("repository takes manifest | export | verify")
                script = "tools/repository/" + scripts[args[0]]
                args = args[1:]
            else:
                script = TOOL_SCRIPTS[command]
            if command == "receipt" and os.name == "nt":
                raise ValueError("receipt uses POSIX locks and hardlinks; use macOS/Linux or WSL")
            return subprocess.run([sys.executable, "-B", str(root / script), *args], check=False).returncode
        if command in ("doctor", "context"):
            if args:
                raise ValueError("doctor/context take global options before the command")
            result = doctor(root, workspace) if command == "doctor" else context(root)
            # Terminal JSON must also work with legacy or ASCII output encodings.
            # Escapes preserve Unicode values without changing source/record bytes.
            print(json.dumps(result, indent=2, ensure_ascii=True))
            return 2 if result.get("status") == "ATTENTION" else 0
        if not (root / MACHINERY).is_file():
            raise ValueError("Package is incomplete: tools/machinery/run is missing; extract the complete release ZIP")
        if command == "porcelain":
            package = root / "experiments/porcelain-simulator"
            venv = Path(os.environ.get("LODESTONE_PORCELAIN_VENV", root / ".venv-porcelain")).expanduser().resolve()
            interpreter = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            if not (package / "porcelain/__init__.py").is_file():
                raise ValueError("This package does not include the optional porcelain simulator")
            if not interpreter.is_file():
                raise ValueError(f"Optional porcelain environment missing: {interpreter}. Follow experiments/porcelain-simulator/README.md; core commands need no packages.")
            if not args or args[0] not in ("run", "report", "verify", "pair", "verify_hands", "report_pair"):
                raise ValueError("porcelain takes run | report | verify | pair | verify_hands | report_pair, then module arguments")
            env = {**os.environ, "OPENBLAS_NUM_THREADS": os.environ.get("OPENBLAS_NUM_THREADS", "1"),
                   "PYTHONPATH": str(package)}
            return subprocess.run([str(interpreter), "-B", "-m", "porcelain." + args[0], *args[1:]],
                                  check=False, env=env).returncode
        if command in ("wake", "conscience"):
            spec = importlib.util.spec_from_file_location("_lodestone_conscience", root / "tools/ecosystem/conscience.py")
            conscience = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(conscience)
            rest = args[1:] if command == "wake" and args and args[0] == "--" else list(args)
            return conscience.main((["wake"] if command == "wake" else []) + rest,
                                   root=root, workspace=workspace)
        forwarded = machinery_args(([command] if command != "machinery" else []) + args, workspace, root)
        return subprocess.run([sys.executable, "-B", str(root / MACHINERY), *forwarded], check=False).returncode
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)[:1000]}), file=sys.stderr)
        return 2
