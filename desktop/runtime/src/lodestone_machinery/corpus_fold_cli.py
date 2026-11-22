"""Checkout adapter for the reviewed corpus-fold tool; no implementation copy."""
from __future__ import annotations

import argparse
from pathlib import Path
import runpy
import sys


def main(argv=None):
    supplied = list(sys.argv[1:] if argv is None else argv)
    if not any(argument in ("-h", "--help") for argument in supplied):
        # The standalone tool retains historical defaults. Organ entry points
        # require explicit input/output locations without rewriting its argv.
        parser = argparse.ArgumentParser(prog="lodestone corpus-fold", add_help=False,
                                         allow_abbrev=False)
        parser.add_argument("--root", required=True)
        parser.add_argument("--fold", required=True)
        locations, _ = parser.parse_known_args(supplied)
        if not locations.root or not locations.fold:
            parser.error("--root and --fold must both be nonempty")
    tool = Path(__file__).resolve().parents[2] / "tools/corpus-fold/src/corpus_fold.py"
    namespace = runpy.run_path(str(tool), run_name="lodestone_corpus_fold")
    return namespace["main"](supplied)


if __name__ == "__main__":
    raise SystemExit(main())
