#!/usr/bin/env python3
"""Preflight one exact external receipt; --append publishes without replacement.

Snapshot scope: Git tracked plus nonignored untracked files, excluding receipts/;
symlinks hash their literal target. Git metadata and ignored files are excluded.
The advisory Git-common-directory lock serializes cooperating calls only. A
noncooperating writer can still race the final observations and publication.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from lodestone_machinery import yggdrasil as lineage  # noqa: E402

REQUIRED = ("receipt_id timestamp operation_name initiating_prompt objective mode "
            "observed_inputs repository_state_before repository_state_after files_read "
            "files_created files_modified files_deleted tests_executed verification_results "
            "hashes_before hashes_after warnings unresolved_questions rollback_procedure "
            "completion_status approval_reference deterministic_snapshot_hash").split()
OBSERVED_PATHS = "receipt_directory_observed_path_set_before_creation"


def _git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.PIPE)


def snapshot_hash(root):
    """Policy manifest over the current Git-visible non-receipt working tree."""
    root = Path(root).resolve()
    paths = set(_git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z").split(b"\0"))
    manifest = hashlib.sha256()
    for relative in sorted(paths - {b""}):
        if relative.startswith(b"receipts/"):
            continue
        name = relative.decode("utf-8")
        if "\n" in name or "\r" in name:
            raise ValueError("snapshot path contains a line break")
        path = root / name
        if path.is_symlink():
            digest = hashlib.sha256(os.readlink(path).encode("utf-8")).hexdigest()
        else:
            if not path.is_file() or not path.resolve().is_relative_to(root):
                raise ValueError("snapshot requires in-repository regular files: " + name)
            digest_state = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest_state.update(chunk)
            digest = digest_state.hexdigest()
        manifest.update(digest.encode("ascii") + b"  " + relative + b"\n")
    return manifest.hexdigest()


def _corpus(root):
    directory = root / "receipts"
    if directory.is_symlink() or not directory.is_dir() or directory.resolve() != directory:
        raise ValueError("receipts must be a canonical physical directory")
    captured = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ValueError("receipt corpus contains a symlink")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError("receipt corpus contains a nonregular file")
        captured[path.relative_to(root).as_posix()] = path.read_bytes()
    return captured


def _draft(path, root):
    path = Path(path).absolute()
    if path.is_symlink() or not path.is_file() or path.resolve().is_relative_to(root):
        raise ValueError("draft must be an external regular file, not a symlink")
    match = lineage.FILENAME.fullmatch(path.name)
    if (not match or not lineage.CANONICAL_ID.fullmatch(match["rid"])
            or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", match["slug"])
            or not match["rid"].startswith("RCP-" + match["stamp"] + "-")):
        raise ValueError("draft filename and receipt ID must be canonical and agree")
    with path.open("rb") as stream:
        raw = stream.read(lineage.MAX_RECEIPT_BYTES + 1)
    if len(raw) > lineage.MAX_RECEIPT_BYTES:
        raise ValueError("draft exceeds the receipt read bound")
    return path, raw, match


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field: " + key)
        result[key] = value
    return result


def _validate(raw, match, corpus, snapshot):
    blocks = list(lineage.JSON_BLOCK.finditer(raw.decode("utf-8")))
    if len(blocks) != 1:
        raise ValueError("draft must have exactly one fenced receipt JSON object")
    document = json.loads(blocks[0][1], object_pairs_hook=_unique_object,
                          parse_constant=lambda value: (_ for _ in ()).throw(ValueError("nonfinite JSON value")))
    missing = [key for key in REQUIRED if key not in document or document[key] is None]
    if missing:
        raise ValueError("missing required receipt fields: " + ", ".join(missing))
    for key in ("receipt_id", "timestamp", "operation_name", "initiating_prompt", "objective",
                "rollback_procedure", "approval_reference"):
        if type(document[key]) is not str or not document[key].strip():
            raise ValueError("required nonblank string: " + key)
    for key in ("files_read", "files_created", "files_modified", "files_deleted", "warnings", "unresolved_questions"):
        if type(document[key]) is not list or any(type(item) is not str for item in document[key]):
            raise ValueError("required list of strings: " + key)
    if document["receipt_id"] != match["rid"]:
        raise ValueError("JSON receipt ID does not match filename")
    stamp = datetime.fromisoformat(document["timestamp"].replace("Z", "+00:00"))
    if stamp.tzinfo is None or stamp.utcoffset().total_seconds() != 0 or stamp.strftime("%Y%m%dT%H%M%SZ") != match["stamp"]:
        raise ValueError("timestamp must be UTC and match filename")
    if stamp > datetime.now(timezone.utc):
        raise ValueError("receipt timestamp must not be in the future")
    if document["mode"] not in lineage.MODES or document["completion_status"] not in ("COMPLETE", "INCOMPLETE", "FAILED"):
        raise ValueError("invalid mode or completion_status")
    if document["deterministic_snapshot_hash"] != snapshot:
        raise ValueError("deterministic snapshot hash does not match current files")
    key = OBSERVED_PATHS if lineage.JOIN_SETS_KEY in document or lineage.JOIN_PREDECESSORS_KEY in document else lineage.PATH_SET_KEY
    paths = document.get(key)
    if type(paths) is not list or any(type(p) is not str for p in paths) or sorted(paths) != sorted(corpus):
        raise ValueError("receipt directory observed path set does not match current corpus")


def _delta(before, after):
    def findings(report):
        return Counter((f["law"], f["receipt"], f["detail"]) for f in report["findings"] if f["law"] != "Y4_ONE_HEAD")
    new = findings(after) - findings(before)
    if new:
        raise ValueError("new lineage findings: " + json.dumps(list(new)))
    # An empty corpus may acquire its first head; an existing corpus may not gain one.
    if len(after["heads"]) > max(1 if before["lifecycles"] == 0 else 0, len(before["heads"])):
        raise ValueError("receipt would increase lineage head count")


@contextmanager
def _lock(root):
    common = Path(os.fsdecode(_git(root, "rev-parse", "--git-common-dir")).strip())
    if not common.is_absolute():
        common = root / common
    with (common / "append-receipt.lock").open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield common
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def process(root, draft, append=False):
    """Validate immutable observations, then optionally append those exact bytes."""
    root = Path(root).resolve(strict=True)
    if Path(os.fsdecode(_git(root, "rev-parse", "--show-toplevel")).strip()).resolve() != root:
        raise ValueError("root must be the Git working-tree root")
    with _lock(root) as common:
        path, raw, match = _draft(draft, root)
        target = root / "receipts" / path.name
        corpus = _corpus(root)
        if target.exists() or target.is_symlink() or any(match["rid"] in name for name in corpus):
            raise ValueError("receipt filename or ID already exists")
        snapshot = snapshot_hash(root)
        _validate(raw, match, corpus, snapshot)
        with tempfile.TemporaryDirectory(prefix="receipt-preflight-") as temporary:
            staged_root = Path(temporary)
            (staged_root / "receipts").mkdir()
            for name, content in corpus.items():
                destination = staged_root / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
            before = lineage.check(staged_root)
            (staged_root / "receipts" / path.name).write_bytes(raw)
            after = lineage.check(staged_root)
        _delta(before, after)
        result = {"status": "PREFLIGHT_OK", "target": "receipts/" + path.name,
                  "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
                  "findings_before": len(before["findings"]), "findings_after": len(after["findings"]),
                  "heads_before": len(before["heads"]), "heads_after": len(after["heads"]),
                  "deterministic_snapshot_hash": snapshot}
        # Stage complete bytes outside receipts; link publishes atomically and refuses replacement.
        with tempfile.NamedTemporaryFile(prefix="receipt-publish-", dir=common) as prepared:
            prepared.write(raw)
            prepared.flush()
            os.fsync(prepared.fileno())
            os.chmod(prepared.name, 0o644)
            if _draft(path, root)[1] != raw:
                raise ValueError("draft changed during preflight")
            if snapshot_hash(root) != snapshot:
                raise ValueError("non-receipt files changed during preflight")
            if _corpus(root) != corpus:
                raise ValueError("receipt corpus changed during preflight")
            if append:
                os.link(prepared.name, target)
                result["status"] = "APPENDED"
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    parser.add_argument("--draft", required=True, help="External file with its exact final canonical receipt basename")
    parser.add_argument("--append", action="store_true", help="Publish exact validated bytes; default only preflights")
    args = parser.parse_args(argv)
    try:
        result = process(args.root, args.draft, args.append)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "REFUSED", "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
