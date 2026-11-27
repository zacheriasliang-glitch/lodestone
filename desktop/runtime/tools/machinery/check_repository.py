#!/usr/bin/env python3
"""Check local Markdown navigation in an explicit project, without lab-specific migration state."""
import argparse
from contextlib import closing
import json
import ntpath
import os
from pathlib import Path
import re
import stat
import sys
from urllib.parse import unquote, urlsplit

EXCLUDED = {".git", ".venv", "node_modules", "__pycache__"}
LINK = re.compile(r"\[[^\]\n]*\]\(([^)\n]+)\)")
REFERENCE = re.compile(r"^\s{0,3}\[[^\]\n]+\]:\s*(<[^>\n]*>|\S+)", re.M)

MAX_MARKDOWN_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_ENTRIES = 10000
MAX_LINKS = 10000


def _native_files():
    source = str(Path(__file__).resolve().parents[2] / "src")
    if source not in sys.path:
        sys.path.insert(0, source)
    from lodestone_nurarihyon import portable_files
    return portable_files


def _windows_markdown(root, files, budget, depth=0):
    # Keep each traversed directory pinned while enumerating and reading below
    # it; do not let rglob follow Windows junctions into another source scope.
    if depth > 128:
        raise ValueError("Repository navigation exceeds its directory depth bound")
    with files.guarded_directory(root):
        with os.scandir(root) as iterator:
            entries = []
            for entry in iterator:
                budget[0] += 1
                if budget[0] > MAX_ENTRIES:
                    raise ValueError("Repository navigation exceeds its entry bound")
                if entry.name not in EXCLUDED:
                    entries.append(entry.name)
        for name in sorted(entries):
            path = root / name
            metadata = path.lstat()
            if files.is_alias(metadata):
                raise ValueError("Repository navigation does not follow reparse paths: " + str(path))
            if stat.S_ISDIR(metadata.st_mode):
                yield from _windows_markdown(path, files, budget, depth + 1)
            elif name.lower().endswith(".md"):
                raw = files.read_bytes(path, MAX_MARKDOWN_BYTES)
                budget[1] += len(raw)
                if budget[1] > MAX_TOTAL_BYTES:
                    raise ValueError("Repository Markdown exceeds its total byte bound")
                yield path, raw.decode("utf-8", errors="strict")


def _windows_target(source, target, files):
    # Link destinations are local path data, never commands or remote shares.
    # Parent-relative navigation remains supported; normalize it only after
    # checking the caller's literal component names for Windows aliases.
    if "\\" in target or "\x00" in target or ":" in target:
        raise ValueError("Local Markdown destinations must use '/' without device or stream names")
    for part in target.split("/"):
        if part not in ("", ".", ".."):
            files.absolute_path(source.parent / part)
    path = files.absolute_path(ntpath.normpath(str(source.parent / target)))
    try:
        with files.guarded_directory(path.parent):
            metadata = path.lstat()
            if files.is_alias(metadata):
                raise ValueError("Local Markdown destination is a reparse path: " + target)
            return True
    except FileNotFoundError:
        return False


def _windows_check(selected):
    native = _native_files()
    root = native.absolute_path(Path(selected).expanduser())
    failures, count, files = [], 0, 0
    with native.guarded_directory(root), closing(_windows_markdown(root, native, [0, 0])) as sources:
        for source, text in sources:
            files += 1
            text = re.sub(r"```.*?```", "", text, flags=re.S)
            for match in (*LINK.finditer(text), *REFERENCE.finditer(text)):
                target = destination(match[1])
                parsed = urlsplit(target)
                if not target or target.startswith("#") or parsed.scheme or parsed.netloc:
                    continue
                target = unquote(parsed.path)
                count += 1
                if count > MAX_LINKS:
                    raise ValueError("Repository navigation exceeds its link bound")
                if not _windows_target(source, target, native):
                    failures.append({"source": source.relative_to(root).as_posix(), "target": target})
    return {"status": "PASS" if not failures else "FAIL", "files": files,
            "local_links": count, "failures": failures,
            "scope": "Local Markdown destinations only; external URLs and anchors not checked; no historical migration audit"}


def destination(value):
    value = value.strip()
    if value.startswith("<") and ">" in value:
        return value[1:value.index(">")]
    return re.split(r"\s+[\"']", value, maxsplit=1)[0]


def check(root):
    if os.name == "nt":
        return _windows_check(root)
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
