"""Run Origami through a trusted provider command or an explicit file exchange."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from .origami import Origami, VERSION, MAX_JSON, ROLES, _read, _endpoints, _auth_path


class CommandProvider:
    """JSON stdin -> JSON stdout, shell disabled, one invocation per decision.

    The command is caller-selected trusted code. Origami.run applies an environment
    allowlist and a kernel sandbox where available; otherwise local filesystem and
    network privileges remain. Calling this adapter directly does not apply that
    launch policy. Never configure a command supplied by evidence documents.
    stderr is not persisted because model clients can print credentials there.
    """

    def __init__(self, argv, timeout=120):
        if type(argv) is not list or not argv or any(type(x) is not str or not x for x in argv):
            raise ValueError("provider command must be a nonempty JSON array of strings")
        if type(timeout) is not int or not 1 <= timeout <= 600:
            raise ValueError("provider timeout must be 1 to 600 seconds")
        self.argv, self.timeout = argv, timeout

    def __call__(self, request):
        # Spool output to a disposable file so it cannot exhaust process memory.
        with tempfile.TemporaryFile() as output:
            result = subprocess.run(self.argv, input=json.dumps(request, ensure_ascii=False).encode("utf-8"),
                                    stdout=output, stderr=subprocess.DEVNULL,
                                    timeout=self.timeout, check=False)
            if result.returncode:
                raise RuntimeError("Provider command failed")
            output.seek(0)
            data = output.read(MAX_JSON + 1)
            if len(data) > MAX_JSON:
                raise ValueError("Provider output exceeds size bound")
            return json.loads(data)


def main(argv=None):
    supplied = sys.argv[1:] if argv is None else argv
    if supplied and supplied[0] == "corpus-fold":
        from .corpus_fold_cli import main as corpus_fold_main
        return corpus_fold_main(supplied[1:])
    if supplied and supplied[0] == "archive":
        from .archive_cli import main as archive_main
        return archive_main(supplied[1:])
    if supplied and supplied[0] == "transformer":
        from .transformer_cli import main as transformer_main
        return transformer_main(supplied[1:])
    if supplied and supplied[0] == "crystal":
        from .crystals_cli import main as crystal_main
        return crystal_main(supplied[1:])
    parser = argparse.ArgumentParser(description=f"Origami source-linked reasoning loop v{VERSION}")
    parser.epilog = "Use 'origami archive' for exact file archives, 'origami corpus-fold' for source-linked reading maps, 'origami transformer' for the bounded decoder, or 'origami crystal' for the Yggdrasil crystal store."
    commands = parser.add_subparsers(dest="operation", required=True)
    for name in ("ask", "start", "request", "advance", "run", "status", "inspect"):
        sub = commands.add_parser(name)
        sub.add_argument("--workspace", required=True)
        if name in ("ask", "start"):
            sub.add_argument("--root", required=True)
            sub.add_argument("--source", action="append", required=True, dest="sources")
            sub.add_argument("--question", required=True)
            sub.add_argument("--epoch", required=True, help="Caller-declared target epoch; not an inferred live Git HEAD")
            sub.add_argument("--authority-ref", required=True, help="Reference to caller's authorization for bounded source reads")
            sub.add_argument("--max-calls", type=int, default=4)
            sub.add_argument("--max-read-bytes", type=int, default=65536)
            sub.add_argument("--initial-passages", type=int, default=6)
            sub.add_argument("--previous", help="Prior run id in this workspace; reassessment is mandatory")
            for role in ROLES:
                sub.add_argument("--" + role, help="Identity of the " + role + " body; all four must be given, or none; "
                                 "constructor, consumer and checker distinct; provider never the checker")
        else:
            sub.add_argument("run_id")
        if name == "advance":
            sub.add_argument("--response", required=True, help="JSON provider envelope responding to the current request")
        if name in ("ask", "run"):
            sub.add_argument("--provider-command", required=name == "run", help='Explicit trusted argv as JSON, e.g. ["python3", "/path/provider.py"]; ask without it prepares a request')
            sub.add_argument("--timeout", type=int, default=120)
            sub.add_argument("--allowed-endpoint", action="append", default=[], dest="allowed_endpoints",
                             help="host:port allowed through the proxy; repeatable; network restriction requires a kernel sandbox")
            sub.add_argument("--provider-auth-path", help="Additional readable auth directory in the sandbox profile; no filesystem isolation without a kernel sandbox")
    args = parser.parse_args(supplied)
    try:
        organ = Origami(args.workspace)
        if args.operation in ("ask", "start"):
            provider = None
            if args.operation == "ask":
                if args.provider_command is not None:
                    # Reject malformed launch settings before persisting a new encounter.
                    provider = CommandProvider(json.loads(args.provider_command), args.timeout)
                    _endpoints(args.allowed_endpoints)
                    _auth_path(args.provider_auth_path)
                elif args.allowed_endpoints or args.provider_auth_path is not None:
                    raise ValueError("Provider endpoint/auth options require --provider-command")
            supplied = {role: getattr(args, role) for role in ROLES}
            roles = supplied if any(value is not None for value in supplied.values()) else None
            result = organ.start(args.root, args.sources, args.question, args.epoch, args.authority_ref,
                                 max_calls=args.max_calls, max_read_bytes=args.max_read_bytes,
                                 initial_passages=args.initial_passages, previous=args.previous, roles=roles)
            if args.operation == "ask":
                if provider is not None:
                    result = organ.run(result["run_id"], provider,
                                       allowed_endpoints=args.allowed_endpoints,
                                       provider_auth_path=args.provider_auth_path)
                else:
                    result = {**result, "request": organ.request(result["run_id"])}
        elif args.operation == "advance":
            result = organ.advance(args.run_id, _read(Path(args.response)))
        elif args.operation == "run":
            result = organ.run(args.run_id, CommandProvider(json.loads(args.provider_command), args.timeout),
                               allowed_endpoints=args.allowed_endpoints, provider_auth_path=args.provider_auth_path)
        else:
            result = getattr(organ, args.operation)(args.run_id)
        print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
        return 0 if args.operation == "request" or result.get("status") in ("NEEDS_PROVIDER", "ANSWER") else 2
    except (ValueError, OSError, RuntimeError, KeyError, TypeError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)[:1000]}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
