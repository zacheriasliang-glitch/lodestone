"""Shared Origami/Yggdrasil crystal commands; originals are never consumed.

Only files created inside an explicit staging directory may be consolidated.
Recovery from a freshly reopened database is checked before the first unlink.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

from .crystals import (CrystalStore, CrystalError, MAX_SOURCE_BYTES,
                       MAX_TOTAL_BYTES, MAX_SOURCES, packet_bytes)

STAGE_FORMAT = "lodestone-crystal-staging/v1"


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _read(path, limit=MAX_SOURCE_BYTES):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise CrystalError("Input must be a regular file, not a symlink")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise CrystalError("Input exceeds byte bound")
    return data


def _new(path, data):
    with Path(path).open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def stage(files, directory, *, epoch, authority_ref):
    """Copy caller-selected files, with a manifest, into a NEW directory."""
    if not files or len(files) > MAX_SOURCES or not epoch or not authority_ref:
        raise CrystalError("Supply bounded files, an epoch, and an authority reference")
    inputs, total = [], 0
    for path in files:
        raw = _read(path)
        total += len(raw)
        if total > MAX_TOTAL_BYTES:
            raise CrystalError("Staging exceeds total input bound")
        inputs.append((str(Path(path).absolute()), raw))
    directory = Path(directory)
    directory.mkdir()  # Never adopt an existing directory.
    records = []
    for index, (origin, raw) in enumerate(inputs):
        name = f"source-{index:06d}.bin"
        _new(directory / name, raw)
        records.append({"file": name, "source_id": f"{epoch}:{index}:{_sha(raw)}",
                        "origin": origin, "sha256": _sha(raw), "bytes": len(raw)})
    manifest = {"format": STAGE_FORMAT, "epoch": epoch,
                "authority_ref": authority_ref, "files": records,
                "notice": "Approval reference is provenance only; no authority is inherited."}
    _new(directory / "stage.json", packet_bytes(manifest))
    return {"status": "STAGED", "directory": str(directory), "files": len(records),
            "source_bytes": total, "manifest_sha256": _sha(packet_bytes(manifest)),
            "originals_modified": False}


def _stage_inputs(directory):
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise CrystalError("Staging must be a real directory")
    manifest = json.loads(_read(directory / "stage.json", 4 * 1024 * 1024))
    records = manifest.get("files")
    if (manifest.get("format") != STAGE_FORMAT or type(records) is not list
            or not 1 <= len(records) <= MAX_SOURCES):
        raise CrystalError("Invalid staging manifest")
    total = 0
    for index, record in enumerate(records):
        if record.get("file") != f"source-{index:06d}.bin":
            raise CrystalError("Staging paths must be the generated direct filenames")
        raw = _read(directory / record["file"])
        if _sha(raw) != record.get("sha256") or len(raw) != record.get("bytes"):
            raise CrystalError("Staged source changed; no consolidation performed")
        total += len(raw)
    if total > MAX_TOTAL_BYTES:
        raise CrystalError("Staging exceeds input bound")
    expected = {record["file"] for record in records} | {"stage.json"}
    if {p.name for p in directory.iterdir()} != expected:
        raise CrystalError("Unexpected staging contents; no consolidation performed")
    if len({r["source_id"] for r in records}) != len(records):
        raise CrystalError("Duplicate source IDs in staging")
    return manifest


def consolidate(directory, database, *, strategy="recursive", consume=False, index_mode=None):
    directory, database = Path(directory), Path(database)
    if directory.resolve() in database.resolve().parents:
        raise CrystalError("Store must be outside the staging directory")
    manifest = _stage_inputs(directory)
    before_manifest = _read(directory / "stage.json", 4 * 1024 * 1024)
    with CrystalStore(database, index_mode=index_mode) as store:
        for record in manifest["files"]:
            store.put(record["source_id"], _read(directory / record["file"]),
                      {"origin": record["origin"], "epoch": manifest["epoch"],
                       "authority_ref": manifest["authority_ref"],
                       "staging_manifest_sha256": _sha(before_manifest)}, strategy=strategy, report=False)
    # New connection proves persisted recovery, independent of original paths.
    with CrystalStore(database) as store:
        for record in manifest["files"]:
            if _sha(store.get(record["source_id"])) != record["sha256"]:
                raise CrystalError("Persisted recovery mismatch; staged copies retained")
        storage = store.inspect()
    # Recheck the entire stage before deleting any copy. No source path is opened.
    _stage_inputs(directory)
    if _read(directory / "stage.json", 4 * 1024 * 1024) != before_manifest:
        raise CrystalError("Staging manifest changed; staged copies retained")
    removed = 0
    if consume:
        for record in manifest["files"]:
            path = directory / record["file"]
            if _sha(_read(path)) != record["sha256"]:
                raise CrystalError("Concurrent stage change; remaining copies retained")
            path.unlink()
            removed += 1
        (directory / "stage.json").unlink()
        directory.rmdir()
    return {"status": "CONSOLIDATED" if consume else "COPIED_AND_VERIFIED",
            "sources": len(manifest["files"]), "staged_copies_removed": removed,
            "stage_manifest_removed": consume, "originals_modified": False,
            "recovery": "ALL_STAGED_BYTES_VERIFIED_FROM_REOPENED_STORE",
            "storage": storage}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Exact Yggdrasil crystals and bounded context views")
    commands = parser.add_subparsers(dest="operation", required=True)
    staging = commands.add_parser("stage", help="Copy explicit sources into a new disposable stage")
    staging.add_argument("--file", action="append", required=True, dest="files")
    staging.add_argument("--directory", required=True)
    staging.add_argument("--epoch", required=True)
    staging.add_argument("--authority-ref", required=True)
    for name in ("put", "get", "inspect", "verify", "search", "packet", "route", "consolidate"):
        command = commands.add_parser(name)
        command.add_argument("--store", required=True)
        if name in ("put", "get"):
            command.add_argument("--source-id", required=True)
        if name == "put":
            command.add_argument("--file", required=True)
            command.add_argument("--metadata-json", default="{}")
            command.add_argument("--policy-store", help="Explicit store containing an experimental learned codec policy")
            command.add_argument("--policy-id", help="Source ID of that frozen policy artifact")
            command.add_argument("--prefix-source-id", action="append", default=[], dest="prefix_ids",
                                 help="At most two current-store sources as the policy's declared prefix")
        if name in ("put", "consolidate"):
            command.add_argument("--strategy", choices=("literal", "zlib", "recursive"))
            command.add_argument("--index-mode", choices=("indexed", "scan"), help="Creation choice; scan saves storage but searches every source")
        if name == "consolidate":
            command.add_argument("--staging", required=True)
            command.add_argument("--consume-staged", action="store_true")
        if name == "get":
            command.add_argument("--output", required=True, help="New file; existing files never overwritten")
        if name in ("search", "packet", "route"):
            command.add_argument("--query", required=True)
        if name in ("search", "packet"):
            command.add_argument("--limit", type=int, default=10)
        if name == "route":
            command.add_argument("--candidate-source-id", action="append", required=True, dest="candidate_ids")
            command.add_argument("--policy-store", required=True)
            command.add_argument("--policy-id", required=True)
        if name in ("packet", "route"):
            command.add_argument("--max-bytes", type=int, default=4096)
            command.add_argument("--required-source-id", action="append", default=[], dest="required_source_ids",
                                 help="Keep this UTF-8 source in full; fail if it cannot fit")
    args = parser.parse_args(argv)
    try:
        if args.operation == "stage":
            result = stage(args.files, args.directory, epoch=args.epoch, authority_ref=args.authority_ref)
        elif args.operation == "consolidate":
            result = consolidate(args.staging, args.store, strategy=args.strategy or "recursive", consume=args.consume_staged, index_mode=args.index_mode)
        else:
            # Read operations must not accidentally create a new empty store.
            if args.operation != "put" and not Path(args.store).is_file():
                raise CrystalError("Store does not exist")
            with CrystalStore(args.store, index_mode=getattr(args, "index_mode", None)) as store:
                if args.operation == "put":
                    raw, metadata = _read(args.file), json.loads(args.metadata_json)
                    strategy = args.strategy or "recursive"
                    if args.policy_id or args.policy_store or args.prefix_ids:
                        if not args.policy_id or not args.policy_store or args.strategy is not None:
                            raise CrystalError("Supply both policy store/ID, without a manual strategy")
                        if type(metadata) is not dict or "codec_policy" in metadata or len(args.prefix_ids) > 2:
                            raise CrystalError("Invalid policy metadata or prefix bound")
                        if not Path(args.policy_store).is_file():
                            raise CrystalError("Policy store does not exist")
                        from .crystal_policy import choose, load_artifact
                        with CrystalStore(args.policy_store) as policies:
                            artifact = load_artifact(policies, args.policy_id)
                        decision = choose(raw, [store.get(sid) for sid in args.prefix_ids], artifact)
                        metadata["codec_policy"] = decision
                        strategy = decision["codec"]
                    result = store.put(args.source_id, raw, metadata, strategy=strategy)
                elif args.operation == "get":
                    raw = store.get(args.source_id)
                    _new(args.output, raw)
                    result = {"source_id": args.source_id, "bytes": len(raw), "sha256": _sha(raw), "output": args.output}
                elif args.operation == "route":
                    from .crystal_routing import choose, load_artifact, packet_for_source
                    if not Path(args.policy_store).is_file():
                        raise CrystalError("Policy store does not exist")
                    with CrystalStore(args.policy_store) as policies:
                        artifact = load_artifact(policies, args.policy_id)
                    decision = choose(args.query, args.candidate_ids, store, artifact)
                    routing = {key: decision[key] for key in ("model_sha256", "raw_slot", "selected_slot",
                        "fallback_used", "fallback_reason", "authority", "status")}
                    overhead = len(packet_bytes({"routing": routing, "packet": {}})) - 2 + 1
                    view = packet_for_source(args.query, decision["selected_source_id"], store,
                        required_source_ids=args.required_source_ids, max_bytes=args.max_bytes - overhead)
                    result = {"routing": routing, "packet": view}
                elif args.operation in ("search", "packet"):
                    kwargs = {"limit": args.limit}
                    if args.operation == "packet":
                        kwargs["max_bytes"] = args.max_bytes - 1
                        kwargs["required_source_ids"] = args.required_source_ids
                    result = getattr(store, args.operation)(args.query, **kwargs)
                else:
                    result = getattr(store, args.operation)()
        # packet budget includes this newline as one byte reserved by the caller.
        if args.operation in ("packet", "route"):
            encoded = packet_bytes(result)
            if len(encoded) + 1 > args.max_bytes:
                raise CrystalError("Packet plus output newline exceeds byte budget; increase budget")
            print(encoded.decode("utf-8"))
        else:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))
        return 0
    except (ValueError, OSError, KeyError, TypeError, sqlite3.DatabaseError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)[:1000]}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
