#!/usr/bin/env python3
"""Run Lodestone from an extracted download. No installation required."""
import runpy
from pathlib import Path
import sys

if __name__ == "__main__":
    if sys.version_info < (3, 10):
        raise SystemExit("Lodestone requires Python 3.10 or later.")
    sys.dont_write_bytecode = True
    runpy.run_path(str(Path(__file__).resolve().parent / "tools/ecosystem/lodestone"), run_name="__main__")
