#!/usr/bin/env python3
"""Create a self-contained, invented re-entry fold in a NEW directory."""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import sys

import origami_fold as fold_tool


TOOL = "tools/origami-fold/src/origami_fold.py"
RECIPE = "tools/origami-fold/src/build_example.py"
EXAMPLE = "tools/origami-fold/examples/demo"
SOURCES = {
    "CURRENT_STATE.md": "# Invented project\n\nStatus: inspection complete.\nNext: review the bound source before using the result.\n",
    "RESULT.md": "# Invented observation\n\nThe sample contains three blue tiles.\nThis observation does not establish a general rule.\n",
    "DIRECTIVE.md": "This is an invented tutorial fixture. It grants no authority to perform external work.\n",
}


def write_example(root: Path) -> Path:
    """Construction recipe: explicit sources and claims, with exact line bindings.

    The caller owns root; exclusive writes refuse an existing source or fold.
    A new project needs its own mapping and claims, not inferred replacements.
    """
    root = root.resolve()
    destination = root / EXAMPLE
    destination.mkdir(parents=True, exist_ok=True)
    for filename, text in SOURCES.items():
        with (destination / filename).open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
    stamp = fold_tool.utc_now()
    ids = {"C": "CURRENT_STATE.md", "R": "RESULT.md", "HD": "DIRECTIVE.md"}
    sources = [fold_tool.bind_source(root, sid, f"{EXAMPLE}/{name}",
               record_kind="invented tutorial fixture", authority="none") for sid, name in ids.items()]
    observed = (destination / "RESULT.md").read_bytes()
    state = (destination / "CURRENT_STATE.md").read_bytes()
    claim = {
        "id": "sample.observation", "status": "ESTABLISHED", "layer": "EX", "basis": "recorded",
        "scope": {"kind": "historical", "epoch": "invented tutorial"},
        "text": "The invented sample contains three blue tiles.",
        "support": [fold_tool.bind_support(observed, "R", 3, 3, "three blue tiles")],
        "refresh_when": [],
    }
    fold = {
        "schema": {"name": fold_tool.SCHEMA_NAME, "version": fold_tool.SCHEMA_VERSION},
        "work_item": {"id": "tutorial", "title": "Invented re-entry example",
            "question": "What does this one sample establish?",
            "epoch": {"description": "Synthetic tutorial; no laboratory result"},
            "next_permitted_action": "Reopen the cited sample; no external work is authorized."},
        "construction": {"built_at_utc": stamp, "built_by": "build_example.py",
            "operation_name": "invented-reentry-example", "interpretation_author": "none",
            "authorization": {"title": "Invented fixture; authority none",
                "directive_sha256": fold_tool.sha256_bytes((destination / "DIRECTIVE.md").read_bytes())},
            "not_this": ["scientific measurement", "authorization", "automatic context compaction"],
            "telemetry": {"tokens": "UNKNOWN", "cost": "UNKNOWN"}},
        "recipe": {"version": "1.0.0", "files": [
            {"path": path, "sha256": fold_tool.sha256_bytes((root / path).read_bytes())}
            for path in (TOOL, RECIPE)]},
        "sources": sources, "claims": [claim], "relations": [],
        "open_items": [{"id": "review.next", "kind": "RESTRICTION", "basis": "recorded",
            "text": "Reopen the bound source before using the result.",
            "support": [fold_tool.bind_support(state, "C", 4, 4, "review the bound source")],
            "refresh_when": ["state_index_changed"]}],
        "index_dependencies": {
            "state_index": {"source": "C"},
            "receipts": {"directory": "receipts", "cutoff_basic_utc": fold_tool.utc_basic(stamp),
                "bound": {"count": 0, "newest": None, "listing_sha256": fold_tool.listing_sha256([])}},
            "host_dispositions": {"patterns": [],
                "bound": {"names": [], "listing_sha256": fold_tool.listing_sha256([])}},
            "origami_records": {"patterns": [],
                "bound": {"names": [], "listing_sha256": fold_tool.listing_sha256([])}},
            "head": {"bound": "UNKNOWN", "role": "provenance only"}},
        "acknowledged_receipts": [],
    }
    errors = fold_tool.structural_errors(fold)
    if errors:
        raise fold_tool.FoldError("; ".join(errors))
    path = root / fold_tool.DEFAULT_FOLD_REL
    # The entire example directory is new and sources were created exclusively.
    fold_tool.save_fold(path, fold)
    path.with_name("fold.md").write_text(fold_tool.render_markdown(fold), encoding="utf-8")
    return path


def create_example(output: Path) -> Path:
    output = output.expanduser().absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("Example destination must be a new directory")
    output.mkdir(parents=False, exist_ok=False)
    code = output / "tools/origami-fold/src"
    code.mkdir(parents=True)
    for name in ("origami_fold.py", "build_example.py"):
        shutil.copyfile(Path(__file__).with_name(name), code / name)
    return write_example(output)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New directory with an existing parent")
    args = parser.parse_args(argv)
    try:
        path = create_example(args.output)
    except (OSError, ValueError, fold_tool.FoldError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
