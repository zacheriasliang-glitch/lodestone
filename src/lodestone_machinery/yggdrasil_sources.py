"""Reopen explicitly selected Origami source bindings; never follow saved roots.

Byte freshness is distinct from semantic support and reviewer independence.
The caller supplies the current root, including an explicitly relocated copy.
"""
from pathlib import Path
import hashlib
import stat

from . import macro_voxel as mv
from .origami import _lines, _passage, MAX_SCOPE_BYTES
from .organs.sources import MAX_SOURCE_BYTES, MAX_SOURCES


class BoundSourceError(ValueError):
    def __init__(self, code, message, *, read_bytes=0, source=None,
                 expected_sha256=None, observed_sha256=None):
        self.code, self.read_bytes, self.source = code, read_bytes, source
        self.sources_attempted = 0
        self.expected_sha256, self.observed_sha256 = expected_sha256, observed_sha256
        super().__init__(message)


def binding_payload(record):
    """Validate the saved binding schema without reading its attributed root."""
    try:
        mv._keys(record, {"schema", "owner", "kind", "payload", "links"}, "source record")
        if (record["schema"] != "organ-record/v1" or record["owner"] != "origami"
                or record["kind"] != "source_bindings" or record["links"] != []):
            raise ValueError("Expected an Origami source_bindings record")
        payload = record["payload"]
        mv._keys(payload, {"run_id", "source_root", "scope", "evidence", "authority"}, "source bindings")
        mv._text(payload["run_id"], "source run")
        mv._text(payload["source_root"], "original source root")
        if (not Path(payload["source_root"]).is_absolute() or payload["authority"] != "NONE"
                or any(ord(c) < 32 for c in payload["source_root"])):
            raise ValueError("Invalid source attribution or authority")
        scope = payload["scope"]
        mv._keys(scope, {"sources", "coverage"}, "source scope")
        mv._text(scope["coverage"], "source coverage")
        entries = scope["sources"]
        if type(entries) is not list or not 1 <= len(entries) <= MAX_SOURCES:
            raise ValueError("Expected 1 to 100 source bindings")
        seen, total = set(), 0
        for entry in entries:
            mv._keys(entry, {"path", "sha256", "bytes", "lines"}, "bound source")
            path = mv.safe_path(entry["path"])
            if path in seen:
                raise ValueError("Duplicate bound source")
            seen.add(path)
            if (type(entry["sha256"]) is not str or mv.SHA.fullmatch(entry["sha256"]) is None
                    or type(entry["bytes"]) is not int or not 0 <= entry["bytes"] <= MAX_SOURCE_BYTES
                    or type(entry["lines"]) is not int or not 0 <= entry["lines"] <= MAX_SOURCE_BYTES):
                raise ValueError("Invalid bound source identity or size")
            total += entry["bytes"]
        if total > MAX_SCOPE_BYTES:
            raise ValueError("Bound source scope exceeds 8 MiB")
        if type(payload["evidence"]) is not list or len(payload["evidence"]) > 4096:
            raise ValueError("Invalid bounded passage list")
        return payload
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise BoundSourceError("CAPTURE_INVALID", str(exc)) from exc


def read_bound_sources(record, root, *, max_bytes=MAX_SCOPE_BYTES, max_sources=MAX_SOURCES):
    """Return sources, manifest, evidence and actual charged read_bytes.

    BoundSourceError distinguishes SOURCE_DRIFT, CAPTURE_INVALID and
    COVERAGE_LIMIT. Missing/unreadable filesystem entries raise OSError with
    read_bytes/source attached. No path is taken from payload.source_root.
    """
    payload = binding_payload(record)
    if not isinstance(root, (str, Path)) or not str(root):
        raise BoundSourceError("CAPTURE_INVALID", "Capture root must be an explicit path")
    if (type(max_bytes) is not int or not 0 <= max_bytes <= MAX_SCOPE_BYTES
            or type(max_sources) is not int or not 0 <= max_sources <= MAX_SOURCES):
        raise BoundSourceError("CAPTURE_INVALID", "Invalid remaining source-read budget")
    entries = payload["scope"]["sources"]
    if len(entries) > max_sources or sum(x["bytes"] for x in entries) > max_bytes:
        raise BoundSourceError("COVERAGE_LIMIT", "Bound sources exceed remaining read budget")
    used, attempted, current_path = 0, 0, None
    try:
        root = Path(root).expanduser().absolute()
        if root.resolve(strict=True) != root or not root.is_dir():
            raise BoundSourceError("CAPTURE_INVALID", "Capture root must be a resolved directory, not an alias")
        sources, manifest = {}, []
        for entry in entries:
            attempted += 1
            current_path = entry["path"]
            path = root
            for part in current_path.split("/"):
                path = path / part
                if path.is_symlink():
                    raise BoundSourceError("CAPTURE_INVALID", "Symlink in bound source", read_bytes=used, source=current_path)
            before = path.stat()
            if not stat.S_ISREG(before.st_mode):
                raise BoundSourceError("CAPTURE_INVALID", "Bound source must be a regular file", read_bytes=used, source=current_path)
            if before.st_size > MAX_SOURCE_BYTES or before.st_size > max_bytes - used:
                raise BoundSourceError("COVERAGE_LIMIT", "Current source exceeds remaining read budget", read_bytes=used, source=current_path)
            # A concurrent growth cannot make this read exceed the aggregate.
            with path.open("rb") as stream:
                raw = stream.read(min(MAX_SOURCE_BYTES + 1, max_bytes - used))
            used += len(raw)
            actual = hashlib.sha256(raw).hexdigest()
            if (len(raw) != entry["bytes"] or actual != entry["sha256"]
                    or path.stat().st_size != len(raw)):
                raise BoundSourceError("SOURCE_DRIFT", "Bound source bytes changed", read_bytes=used,
                                       source=current_path, expected_sha256=entry["sha256"], observed_sha256=actual)
            try:
                text = raw.decode("utf-8", errors="strict")
            except UnicodeError as exc:
                raise BoundSourceError("CAPTURE_INVALID", "Bound source must be strict UTF-8", read_bytes=used, source=current_path) from exc
            source = {"id": current_path, "path": current_path, "sha256": actual, "bytes": len(raw), "text": text}
            sources[current_path] = source
            manifest.append({"path": current_path, "sha256": actual, "bytes": len(raw), "lines": len(_lines(text))})
        if manifest != entries:
            raise BoundSourceError("CAPTURE_INVALID", "Bound source line metadata differs", read_bytes=used)
        evidence, seen = [], set()
        for passage in payload["evidence"]:
            try:
                current = _passage(sources[passage["source"]], passage["start_line"], passage["end_line"])
            except (ValueError, KeyError, TypeError) as exc:
                raise BoundSourceError("CAPTURE_INVALID", "Invalid bound passage", read_bytes=used) from exc
            if mv.canonical(current) != mv.canonical(passage) or current["id"] in seen:
                raise BoundSourceError("CAPTURE_INVALID", "Bound passage identity differs or repeats", read_bytes=used)
            seen.add(current["id"])
            evidence.append(current)
        return {"sources": sources, "manifest": manifest, "evidence": evidence,
                "read_bytes": used, "sources_attempted": attempted}
    except BoundSourceError as exc:
        exc.sources_attempted = attempted
        raise
    except OSError as exc:
        exc.read_bytes, exc.source, exc.sources_attempted = used, current_path, attempted
        raise
    except (RuntimeError, TypeError, ValueError) as exc:
        error = BoundSourceError("CAPTURE_INVALID", str(exc), read_bytes=used, source=current_path)
        error.sources_attempted = attempted
        raise error from exc
