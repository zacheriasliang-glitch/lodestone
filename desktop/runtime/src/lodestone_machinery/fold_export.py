"""Exact retained folds through Yggdrasil, without reopening original sources.

The controller is trusted. It pins an existing answer record, validates its
retained evidence contract, and exports the complete fold using canonical JSON.
The consumer receives only that JSON and trusted Python source code. Historical
support remains historical; this module does not certify a new answer.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from .crystals import CrystalStore, MAX_SOURCE_BYTES
from .records import HANDLE, MAX_BYTES, RecordStore, canonical
from .origami import DECISIONS, RESPONSE_SCHEMA, _keys, _text, _validate_response
from .support_review import REQUEST_SCHEMA, ATTRIBUTION
from .yggdrasil_sources import binding_payload
from .yggdrasil_runtime import _lesson_refs, _check_packet_lesson
from lodestone_nurarihyon.portable_files import open_regular, absolute_path

SCHEMA = "origami-fold-export/v1"
SCHEMA_V2 = "origami-fold-export/v2"
RETAINED_SCHEMA = "origami-retained-fold/v1"
NEXT_CHECK_POLICY = "HISTORICAL_DATA_ONLY_NO_SOURCE_READ_AUTHORITY"
ENCODING = "UTF-8 JSON; sorted keys; compact separators; ensure_ascii=false; no NaN; no trailing LF"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def _schema(version):
    if type(version) is not int or version not in (1, 2):
        raise ValueError("Export version must be 1 or 2")
    return SCHEMA if version == 1 else SCHEMA_V2


def _identity(value, version):
    keys = ["answer_handle", "run_id", "request_id", "encoding", "fold_sha256", "fold_bytes"]
    if version == 2:
        keys += ["fold_decision", "next_check_policy"]
    return {key: value[key] for key in keys}


def plain_path(value, *, directory=False, new=False):
    supplied = Path(value)
    if ".." in supplied.parts:
        raise ValueError("Parent traversal is forbidden")
    path = absolute_path(supplied) if os.name == "nt" else Path(os.path.abspath(supplied))
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError("Symlink path is forbidden: " + str(part))
    if new:
        if path.exists() or not path.parent.is_dir():
            raise ValueError("Destination must be new with an existing parent")
    elif not path.exists() or (directory and not path.is_dir()):
        raise ValueError("Missing or invalid path: " + str(path))
    return path


def regular_bytes(value, limit=MAX_BYTES, *, stat_only=False):
    path = plain_path(value)
    with open_regular(path) as fd:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
            raise ValueError("Expected a bounded singly linked regular file")
        if stat_only:
            return None
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError("File exceeds byte bound")
        return data


def _json(data):
    value = json.loads(data)
    if canonical(value) != data:
        raise ValueError("JSON must have exact canonical encoding")
    return value


def _write(path, data):
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    path.chmod(0o400)


def _retained_packet(packet, bound):
    """Validate attributed retention, without inventing a support review."""
    _keys(packet, {"schema", "run_id", "question", "epoch", "source_root", "scope", "fold",
                   "evidence", "attribution", "authority", "request_sha256"}, "retained fold packet")
    for key in ("run_id", "question", "epoch", "attribution"):
        _text(packet[key], key)
    if (packet["schema"] != RETAINED_SCHEMA or packet["authority"] != "NONE"
            or any(canonical(packet[k]) != canonical(bound[k]) for k in ("run_id", "source_root", "scope", "evidence"))
            or digest(canonical({k: v for k, v in packet.items() if k != "request_sha256"})) != packet["request_sha256"]):
        raise ValueError("Retained fold identity or source binding differs")


def retain_fold(workspace, *, run_id, source_root, scope, evidence, fold, question, epoch, attribution):
    """Retain a caller-supplied native fold without claiming a checker PASS.

    This checks structure and identity, not source currentness or semantic truth.
    The caller supplies attribution and an existing workspace; original sources
    are never reopened. The record kind is ``retained_fold``, not answer evidence.
    Exporting this record requires version 2, even when its decision is ANSWER.
    """
    root = plain_path(workspace, directory=True)
    # Canonical serialization detaches caller-owned values before validation.
    bound = _json(canonical({"run_id": run_id, "source_root": source_root, "scope": scope,
                             "evidence": evidence, "authority": "NONE"}))
    binding_payload({"schema": "organ-record/v1", "owner": "origami", "kind": "source_bindings",
                     "payload": bound, "links": []})
    packet = _json(canonical({"schema": RETAINED_SCHEMA, **bound, "fold": fold, "question": question,
                              "epoch": epoch, "attribution": attribution}))
    packet["request_sha256"] = digest(canonical(packet))
    _retained_packet(packet, bound)
    if type(packet["fold"]) is not dict:
        raise ValueError("Retained record requires its complete fold")
    _validate_response(packet["fold"], {"request_id": packet["fold"].get("request_id"),
                                        "evidence": packet["evidence"], "scope": packet["scope"]})
    if not re.fullmatch(re.escape(packet["run_id"]) + r":(?:[0-9]|1[0-5])", packet["fold"]["request_id"]):
        raise ValueError("Fold request identity differs from retained run")
    if not 1 <= len(canonical(packet["fold"])) <= MAX_SOURCE_BYTES:
        raise ValueError("Complete fold exceeds Yggdrasil source bound")
    # Include record envelopes in the same bound enforced by RecordStore.
    for kind, payload, links in (("source_bindings", bound, []),
                                 ("retained_fold", packet, ["origami:" + "0" * 64])):
        if len(canonical({"schema": "organ-record/v1", "owner": "origami", "kind": kind,
                          "payload": payload, "links": links})) > MAX_BYTES:
            raise ValueError("Retained fold record exceeds byte bound")
    # All validation precedes the first immutable record publication.
    store = RecordStore(root)
    source_handle = store.publish("origami", "source_bindings", bound)
    handle = store.publish("origami", "retained_fold", packet, links=[source_handle])
    return {"status": "RETAINED_UNREVIEWED", "fold_handle": handle, "answer_handle": handle,
            "source_handle": source_handle, "decision": fold["decision"], "authority": "NONE"}


def _retained(workspace, answer_handle, version=1):
    if not isinstance(answer_handle, str) or not HANDLE.fullmatch(answer_handle):
        raise ValueError("Expected exact answer record handle")
    root = plain_path(workspace, directory=True)
    # Check before constructing RecordStore so absent stores are not created.
    plain_path(root / "organs", directory=True)
    store = RecordStore(root)
    pending, records = [answer_handle], {}
    while pending:
        handle = pending.pop()
        if handle in records:
            continue
        if len(records) >= 256:
            raise ValueError("Record graph exceeds 256 records")
        raw = regular_bytes(store.path(handle))
        record = store.read(handle)
        if canonical(record) != raw or digest(raw) != handle.split(":")[1]:
            raise ValueError("Retained record changed during read")
        records[handle] = (raw, record)
        pending.extend(record["links"])
        pending.extend(handle for _, handle in _lesson_refs(record))
    answer = records[answer_handle][1]
    allowed_kinds = {"answer_evidence"} if version == 1 else {"answer_evidence", "retained_fold"}
    if answer["owner"] != "origami" or answer["kind"] not in allowed_kinds:
        raise ValueError("Selected record is not Origami answer evidence")
    if len(answer["links"]) != 1:
        raise ValueError("Answer evidence requires its exact source binding")
    bound = binding_payload(records[answer["links"][0]][1])
    packet = answer["payload"]
    if answer["kind"] == "retained_fold":
        _retained_packet(packet, bound)
    else:
        _answer_packet(packet, bound, records)
    fold = packet["fold"]
    if not isinstance(fold, dict):
        raise ValueError("Retained record requires its complete fold")
    _validate_response(fold, {"request_id": fold.get("request_id"), "evidence": packet["evidence"], "scope": packet["scope"]})
    if version == 1 and fold["decision"] != "ANSWER":
        raise ValueError("Version 1 retained fold must be an ANSWER")
    if not re.fullmatch(re.escape(packet["run_id"]) + r":(?:[0-9]|1[0-5])", packet["fold"]["request_id"]):
        raise ValueError("Fold request identity differs from retained run")
    data = canonical(packet["fold"])
    if not 1 <= len(data) <= MAX_SOURCE_BYTES:
        raise ValueError("Complete fold exceeds Yggdrasil source bound")
    return packet, data, sorted(records)


def _answer_packet(packet, bound, records):
    required = {"schema", "run_id", "question", "epoch", "reviewer", "provider", "files", "source_root",
                "scope", "fold", "evidence", "prior_knowledge", "instructions", "response_schema",
                "attribution", "authority", "request_sha256"}
    if "lesson_context" in packet:
        required.add("lesson_context")
    _keys(packet, required, "answer evidence packet")
    # Origami accepts longer question strings than the macro-voxel graph's
    # 4096-character text cap. Validate its own contract without truncating it.
    for field in ("question", "epoch", "reviewer", "provider", "instructions", "run_id"):
        _text(packet[field], field)
    if (packet["schema"] != REQUEST_SCHEMA or packet["authority"] != "NONE"
            or packet["attribution"] != ATTRIBUTION
            or any(canonical(packet[k]) != canonical(bound[k]) for k in ("run_id", "source_root", "scope", "evidence"))
            or digest(canonical({k: v for k, v in packet.items() if k != "request_sha256"})) != packet["request_sha256"]):
        raise ValueError("Answer evidence identity or source binding differs")
    if "lesson_context" in packet:
        _check_packet_lesson(packet, records)


def export_fold(workspace, answer_handle, destination, *, version=1):
    """Export selected record's entire fold; never follow original source paths."""
    schema = _schema(version)
    dest = plain_path(destination, new=True)
    packet, data, records = _retained(workspace, answer_handle, version)
    identity = {"answer_handle": answer_handle, "run_id": packet["run_id"],
                "request_id": packet["fold"]["request_id"], "encoding": ENCODING,
                "fold_sha256": digest(data), "fold_bytes": len(data)}
    if version == 2:
        identity.update(fold_decision=packet["fold"]["decision"], next_check_policy=NEXT_CHECK_POLICY)
    source_id = "origami-fold:" + answer_handle.split(":")[1]
    dest.mkdir(mode=0o700)
    archive = dest / "fold-crystals.sqlite"
    with CrystalStore(archive) as store:
        ref = store.put(source_id, data, metadata=identity)
        if ref["metadata"] != identity or store.get(source_id) != data:
            raise ValueError("Yggdrasil export readback differs")
    manifest = {"schema": schema, **identity, "source_id": source_id,
                "crystal_root": ref["root"], "retained_record_handles": records,
                "provenance_status": "Retained record byte and structural identity only; no current source or new answer verification",
                "authority": "NONE"}
    raw = canonical(manifest)
    _write(dest / "manifest.json", raw)
    return {**manifest, "manifest_sha256": digest(raw), "export": str(dest)}


def read_export(export_dir, *, expected_manifest_sha256, expected_answer_handle, version=1):
    """Selective readback bound to caller-supplied identity, not a self-report."""
    schema = _schema(version)
    root = plain_path(export_dir, directory=True)
    raw = regular_bytes(root / "manifest.json", 65536)
    if digest(raw) != expected_manifest_sha256:
        raise ValueError("Export manifest digest mismatch")
    manifest = _json(raw)
    if (manifest.get("schema") != schema or manifest.get("answer_handle") != expected_answer_handle
            or manifest.get("encoding") != ENCODING or manifest.get("authority") != "NONE"
            or not isinstance(expected_answer_handle, str) or not HANDLE.fullmatch(expected_answer_handle)):
        raise ValueError("Export identity or encoding mismatch")
    if manifest.get("source_id") != "origami-fold:" + expected_answer_handle.split(":")[1]:
        raise ValueError("Crystal source does not belong to selected record")
    if version == 2 and manifest.get("next_check_policy") != NEXT_CHECK_POLICY:
        raise ValueError("Retained next_check is data, not source-read authority")
    identity = _identity(manifest, version)
    archive = plain_path(root / "fold-crystals.sqlite")
    # Read-only SQLite must not repair/initialize a damaged artifact. Refuse
    # sidecars; the controller directory must be free of concurrent writers.
    regular_bytes(archive, 1024 * 1024 * 1024, stat_only=True)
    with archive.open("rb") as stream:
        header = stream.read(100)
    if (len(header) != 100 or header[:16] != b"SQLite format 3\x00"
            or header[18:20] != b"\x01\x01"):
        raise ValueError("Export archive requires an intact rollback-journal SQLite header; WAL is refused")
    for suffix in ("-journal", "-wal", "-shm"):
        if (root / (archive.name + suffix)).exists() or (root / (archive.name + suffix)).is_symlink():
            raise ValueError("Archive must be closed without SQLite sidecars")
    with CrystalStore(archive, read_only=True) as store:
        ref = store._source(manifest["source_id"])
        data = store.get(manifest["source_id"])
        if ref["metadata"] != identity or ref["root"] != manifest["crystal_root"]:
            raise ValueError("Crystal metadata or root mismatch")
    if (type(manifest["fold_bytes"]) is not int or not 1 <= len(data) <= MAX_SOURCE_BYTES
            or len(data) != manifest["fold_bytes"] or digest(data) != manifest["fold_sha256"]):
        raise ValueError("Fold size or digest mismatch")
    fold = _json(data)
    if not isinstance(fold, dict) or fold.get("request_id") != manifest["request_id"]:
        raise ValueError("Fold request identity mismatch")
    _keys(fold, RESPONSE_SCHEMA, "exported fold")
    if version == 1 and fold.get("decision") != "ANSWER":
        raise ValueError("Version 1 retained fold must be an ANSWER")
    if version == 2 and (fold.get("decision") not in DECISIONS
                         or fold.get("decision") != manifest["fold_decision"]):
        raise ValueError("Fold decision identity mismatch")
    return manifest, data


def materialize_fold(export_dir, destination, *, expected_manifest_sha256, expected_answer_handle, version=1):
    """Create a fresh fold-only capsule; return provenance to the controller."""
    dest = plain_path(destination, new=True)
    manifest, data = read_export(export_dir, expected_manifest_sha256=expected_manifest_sha256,
                                 expected_answer_handle=expected_answer_handle, version=version)
    # Runtime is code from this installation, not from the archive or its manifest.
    package = plain_path(Path(__file__).parent, directory=True)
    runtime = {str(path.relative_to(package)): regular_bytes(path) for path in sorted(package.rglob("*.py"))}
    if "__init__.py" not in runtime:
        raise ValueError("Trusted runtime package is incomplete")
    dest.mkdir(mode=0o700)
    for name in ("source", "workspace", "runtime"):
        (dest / name).mkdir(mode=0o700)
    _write(dest / "source" / "fold.json", data)
    target = dest / "runtime" / "lodestone_machinery"
    target.mkdir(mode=0o700)
    for name, source in runtime.items():
        (target / name).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _write(target / name, source)
    if regular_bytes(dest / "source" / "fold.json") != data:
        raise ValueError("Materialized fold differs")
    return {"schema": "origami-fold-capsule/v" + str(version), "capsule": str(dest),
            "answer_handle": expected_answer_handle, "manifest_sha256": expected_manifest_sha256,
            "fold_sha256": manifest["fold_sha256"], "fold_bytes": len(data),
            "runtime_sha256": {name: digest(source) for name, source in runtime.items()},
            "isolation": "Requires fold_isolation.launch_consumer; a directory alone is not isolation",
            "authority": "NONE"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export")
    export.add_argument("workspace"); export.add_argument("answer_handle"); export.add_argument("destination")
    export.add_argument("--version", type=int, choices=(1, 2), default=1)
    materialize = commands.add_parser("materialize")
    materialize.add_argument("export_dir"); materialize.add_argument("destination")
    materialize.add_argument("--manifest-sha256", required=True)
    materialize.add_argument("--answer-handle", required=True)
    materialize.add_argument("--version", type=int, choices=(1, 2), default=1)
    args = parser.parse_args(argv)
    if args.command == "export":
        result = export_fold(args.workspace, args.answer_handle, args.destination, version=args.version)
    else:
        result = materialize_fold(args.export_dir, args.destination,
                                  expected_manifest_sha256=args.manifest_sha256,
                                  expected_answer_handle=args.answer_handle, version=args.version)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
