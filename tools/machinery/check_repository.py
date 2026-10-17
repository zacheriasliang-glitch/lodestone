#!/usr/bin/env python3
"""Check local Markdown navigation in an explicit project, without lab-specific migration state."""
import argparse
import json
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

EXCLUDED = {".git", ".venv", "node_modules", "__pycache__"}
LINK = re.compile(r"\[[^\]\n]*\]\(([^)\n]+)\)")
REFERENCE = re.compile(r"^\s{0,3}\[[^\]\n]+\]:\s*(<[^>\n]*>|\S+)", re.M)


def destination(value):
    value = value.strip()
    if value.startswith("<") and ">" in value:
        return value[1:value.index(">")]
    return re.split(r"\s+[\"']", value, maxsplit=1)[0]


def check(root):
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError("root must be an existing directory")
    failures, count, files = [], 0, 0
    for source in sorted(root.rglob("*.md")):
        if set(source.relative_to(root).parts) & EXCLUDED or source.is_symlink():
            continue
        if not source.resolve().is_relative_to(root):
            continue
        files += 1
        text = source.read_text(encoding="utf-8")
        # Examples in fenced code are not navigation links.
        text = re.sub(r"```.*?```", "", text, flags=re.S)
        for match in (*LINK.finditer(text), *REFERENCE.finditer(text)):
            target = destination(match[1])
            if not target or target.startswith("#") or urlsplit(target).scheme:
                continue
            target = unquote(urlsplit(target).path)
            count += 1
            if not (source.parent / target).exists():
                failures.append({"source": source.relative_to(root).as_posix(), "target": target})
    return {"status": "PASS" if not failures else "FAIL", "files": files,
            "local_links": count, "failures": failures,
            "scope": "Local Markdown destinations only; external URLs and anchors not checked; no historical migration audit"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    try:
        result = check(args.root)
    except (OSError, ValueError) as exc:
        result = {"status": "FAIL", "error": str(exc)}
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
