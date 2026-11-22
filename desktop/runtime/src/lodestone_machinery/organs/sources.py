"""Read bounded UTF-8 sources and reopen exact passage handles."""
from __future__ import annotations

from pathlib import Path, PurePosixPath

from ._values import _hash

MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_SOURCES = 100


def _source_path(root: Path, relative_path: str) -> Path:
    if not isinstance(root, Path):
        raise ValueError("root must be a pathlib.Path")
    if type(relative_path) is not str or not relative_path or "\\" in relative_path:
        raise ValueError("Source must be a nonempty relative POSIX path")
    path = PurePosixPath(relative_path)
    if path.is_absolute() or any(part in ("", ".", "..") for part in relative_path.split("/")):
        raise ValueError("Absolute paths and traversal are forbidden")
    try:
        resolved_root = root.resolve(strict=True)
        if not resolved_root.is_dir():
            raise ValueError("root must be a directory")
        resolved = (resolved_root / relative_path).resolve(strict=True)
        resolved.relative_to(resolved_root)
        if not resolved.is_file():
            raise ValueError("Source must be a regular file")
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError(f"Source is missing, invalid, or outside root: {relative_path}") from exc
    return resolved


def anchor(root: Path, relative_path: str) -> dict:
    """Inspect a bounded UTF-8 file and return its exact byte identity and text."""
    path = _source_path(root, relative_path)
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_SOURCE_BYTES + 1)
        if len(data) > MAX_SOURCE_BYTES:
            raise ValueError(f"Source exceeds {MAX_SOURCE_BYTES} bytes: {relative_path}")
        text = data.decode("utf-8", errors="strict")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"Source cannot be read as strict UTF-8: {relative_path}") from exc
    return {"id": relative_path, "path": relative_path, "sha256": _hash(data),
            "bytes": len(data), "text": text}


def expand(root: Path, passage: dict) -> dict:
    """Reopen the same line coordinates and report byte drift explicitly."""
    if type(passage) is not dict:
        raise ValueError("passage must be a dictionary")
    required = ("source", "start_line", "end_line", "source_sha256", "slice_sha256")
    if any(key not in passage for key in required):
        raise ValueError("passage lacks an exact source handle")
    start, end = passage["start_line"], passage["end_line"]
    if type(start) is not int or type(end) is not int or start < 1 or end < start:
        raise ValueError("Passage line interval is invalid")
    source = anchor(root, passage["source"])
    lines = source["text"].splitlines(keepends=True)
    in_range = end <= len(lines)
    text = "".join(lines[start - 1:end])
    slice_hash = _hash(text.encode("utf-8"))
    fresh = (in_range and source["sha256"] == passage["source_sha256"]
             and slice_hash == passage["slice_sha256"])
    return {"source": source["path"], "start_line": start, "end_line": end,
            "text": text, "fresh": fresh, "status": "FRESH" if fresh else "STALE",
            "source_sha256": source["sha256"], "slice_sha256": slice_hash,
            "expected_source_sha256": passage["source_sha256"],
            "expected_slice_sha256": passage["slice_sha256"], "coordinates_valid": in_range}
