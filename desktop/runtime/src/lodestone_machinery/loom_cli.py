"""Explicit local Loom advisory CLI; training uses the bounded transformer CLI."""
import argparse
import json
from pathlib import Path
import sys

def _json(path, maximum=65536):
    from . import transformer
    from .fold_export import regular_bytes
    raw = regular_bytes(path, maximum)
    return json.loads(raw, object_pairs_hook=transformer._unique_pairs), raw


def main(argv=None):
    parser = argparse.ArgumentParser(prog="origami loom")
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("advise", help="One local forward pass over explicit context-bound features")
    p.add_argument("--experimental", action="store_true",
                   help="Explicitly opt in to unvalidated model scores; useful transfer is not established")
    p.add_argument("--model", required=True)
    p.add_argument("--expected-model-sha256", required=True)
    p.add_argument("--context", required=True)
    p.add_argument("--expected-context-sha256", required=True)
    p.add_argument("--request", required=True)
    p.add_argument("--expected-request-sha256", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--fold", help="Optional complete fold to retain in the reader packet")
    p.add_argument("--expected-fold-sha256")
    args = parser.parse_args(argv)
    try:
        if not args.experimental:
            raise ValueError("Loom model predictions require --experimental; reliable transfer has not been established")
        from . import transformer
        from .fold_export import plain_path
        from .loom import advise, reader_packet
        from .loom_context import load_context, sha
        from .records import canonical
        destination = plain_path(args.output, new=True)
        context = load_context(args.context, expected_sha256=args.expected_context_sha256)
        request, raw = _json(args.request)
        if sha(raw) != args.expected_request_sha256:
            raise ValueError("Request differs from the caller's pinned digest")
        if (args.fold is None) != (args.expected_fold_sha256 is None):
            raise ValueError("Supply both --fold and --expected-fold-sha256")
        model = transformer.load_model(plain_path(args.model))
        if transformer.model_sha256(model) != args.expected_model_sha256:
            raise ValueError("Model differs from the caller's pinned digest")
        fold = None
        if args.fold is not None:
            fold, raw_fold = _json(args.fold, 1024 * 1024)
            if type(fold) is not dict or canonical(fold) != raw_fold:
                raise ValueError("Complete fold must retain exact canonical bytes")
            if sha(raw_fold) != args.expected_fold_sha256:
                raise ValueError("Complete fold differs from the caller's pinned digest")
        result = advise(model, request, context)
        if fold is not None:
            result = reader_packet(fold, context, result, expected_fold_sha256=args.expected_fold_sha256)
        raw = canonical(result)
        with destination.open("xb") as stream:
            stream.write(raw)
        print(json.dumps({"output": str(destination), "bytes": len(raw), "sha256": sha(raw),
                          "authority": "NONE", "activation": "NONE"}))
        return 0
    except (ValueError, OSError, KeyError, TypeError, UnicodeError, RecursionError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)[:1000]}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
