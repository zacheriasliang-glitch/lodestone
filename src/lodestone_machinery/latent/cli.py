"""Explicit candidate-only storage through the existing CrystalStore.

Input hashes establish byte identity, not who prepared a model or whether a table
was compiled from it. No model execution or candidate generation occurs here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

from ..crystals import CrystalStore
from . import codec, store_adapter as adapter

MAX_PLAN_BYTES = 2 * 1024 * 1024
RECEIPT_SCHEMA = "lodestone-supplied-candidate-receipt/v1"


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _json_bytes(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


def _read(path, limit):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("Input must be a regular file, not a symlink")
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("Input exceeds byte bound")
    return raw


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _json(raw):
    return json.loads(raw, object_pairs_hook=_pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON")))


def _new(path, raw):
    with Path(path).open("xb") as stream:
        stream.write(raw)


def _distinct_outputs(inputs, outputs):
    paths = [Path(p) for p in inputs + outputs]
    for i, destination in enumerate(paths[len(inputs):], len(inputs)):
        for other in paths[:i]:
            if (destination.resolve() == other.resolve() or
                    (destination.exists() and other.exists() and destination.samefile(other))):
                raise ValueError("Output paths must be distinct from inputs and one another")


def _store_boundary(store_path, other_paths):
    """SQLite may create/delete these names independently of the database file."""
    companions = [Path(str(store_path) + suffix) for suffix in ("-journal", "-wal", "-shm")]
    _distinct_outputs(other_paths, companions)
    if any(path.exists() or path.is_symlink() for path in companions):
        raise ValueError("SQLite companion paths already exist; use a quiescent store or a new path")


def _existing_schema(store_path):
    # CrystalStore intentionally initializes schema-less stores for writes.
    # A recovery command must reject them without invoking that constructor.
    connection = sqlite3.connect(Path(store_path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1").fetchone() is None:
            raise ValueError("Recovery requires an initialized crystal database")
    finally:
        connection.close()


def _recovery_header(store_path):
    # WAL mode persists after its companion files disappear. Opening it through
    # CrystalStore would change journal mode and rewrite the database header.
    # Read only the fixed SQLite header before either SQLite connection opens.
    with Path(store_path).open("rb") as stream:
        header = stream.read(100)
    if len(header) != 100 or header[:16] != b"SQLite format 3\x00":
        raise ValueError("Recovery requires a complete SQLite format 3 header")
    versions = header[18:20]  # File-format write version, then read version.
    if versions == b"\x02\x02":
        raise ValueError("WAL-mode recovery is unsupported; database is left unchanged")
    if versions != b"\x01\x01":
        raise ValueError("Recovery requires SQLite rollback read/write versions 1/1")


def put(*, store_path, source_id, source_path, model_path, table_path,
        expected_model_sha256, expected_table_sha256, candidates_path,
        receipt_path, max_seconds=60, index_mode=None):
    """Validate all inputs before opening a store, then append and reopen/recover.

    A failed append can leave immutable intermediate artifacts, as in the
    original adapter. The new receipt is published after exact recovery.
    """
    started, cpu = time.monotonic(), time.process_time()
    _distinct_outputs([source_path, model_path, table_path, candidates_path],
                      [store_path, receipt_path])
    _store_boundary(store_path, [source_path, model_path, table_path, candidates_path, receipt_path])
    if Path(receipt_path).exists() or Path(receipt_path).is_symlink():
        raise ValueError("Receipt output must be new")
    if not Path(receipt_path).parent.is_dir():
        raise ValueError("Receipt parent directory must exist")
    raw = _read(source_path, adapter.MAX_SOURCE_BYTES)
    model = _read(model_path, adapter.latent_model.MAX_MODEL_BYTES)
    table = _read(table_path, codec.TABLE_BYTES)
    plan_bytes = _read(candidates_path, MAX_PLAN_BYTES)
    plan = _json(plan_bytes)
    bundle = adapter.load_candidate_bundle(model, table,
        expected_model_sha256=expected_model_sha256,
        expected_table_sha256=expected_table_sha256)
    adapter.validate_candidate_source(source_id, raw, bundle, plan, max_seconds=max_seconds)
    with CrystalStore(store_path, index_mode=index_mode) as store:
        result = adapter.store_source(store, source_id, raw, mandatory=False,
            prepared=(bundle,), supplied_candidates=plan, max_seconds=max_seconds)
    pin = result["manifest"]
    # A new connection must recover without opening any source/model/plan file.
    with CrystalStore(store_path) as store:
        recovered = adapter.restore_source(store, pin["id"],
            expected_manifest_sha256=pin["sha256"], max_seconds=max_seconds)
        storage = store.inspect()
    if recovered != raw:
        raise ValueError("Reopened store recovery differs from input")
    receipt = dict(schema=RECEIPT_SCHEMA, manifest_id=pin["id"],
        manifest_sha256=pin["sha256"], source_sha256=_sha(raw), source_bytes=len(raw))
    receipt_raw = _json_bytes(receipt)
    _new(receipt_path, receipt_raw)
    database_bytes = Path(store_path).stat().st_size
    result.update(recovery="EXACT_FROM_REOPENED_STORE", receipt=str(receipt_path),
        bundle_provenance="CALLER_PINNED_BYTES_NOT_COMPILE_AUTHENTICATED",
        retained_cost=dict(database_file_bytes=database_bytes,
            external_receipt_bytes=len(receipt_raw),
            database_plus_receipt_bytes=database_bytes + len(receipt_raw),
            logical_artifact_bytes=result["logical_artifact_bytes_total"],
            store_logical_accounting=storage,
            external_input_plan_bytes=len(plan_bytes),
            note="Logical bytes are inside the database; do not add them to physical bytes. "
                 "Plan/input copies retained elsewhere are additional; generation cost is unmeasured."),
        command_wall_seconds=time.monotonic() - started,
        command_cpu_seconds=time.process_time() - cpu)
    return result


def get(*, store_path, receipt_path, output_path, max_seconds=60):
    """Recover using the database and external receipt only; never overwrite."""
    _distinct_outputs([store_path, receipt_path], [output_path])
    _store_boundary(store_path, [receipt_path, output_path])
    if Path(output_path).exists() or Path(output_path).is_symlink():
        raise ValueError("Recovered output must be new")
    if not Path(store_path).is_file() or Path(store_path).is_symlink():
        raise ValueError("Recovery requires an existing regular database")
    receipt = _json(_read(receipt_path, 8192))
    if (type(receipt) is not dict or set(receipt) != {
            "schema", "manifest_id", "manifest_sha256", "source_sha256", "source_bytes"}
            or receipt["schema"] != RECEIPT_SCHEMA):
        raise ValueError("Invalid external receipt")
    adapter._hash(receipt["source_sha256"])
    adapter._hash(receipt["manifest_sha256"])
    if receipt["manifest_id"] != "latent:manifest:" + receipt["manifest_sha256"]:
        raise ValueError("Manifest ID differs from external receipt hash")
    adapter._integer(receipt["source_bytes"], 0, adapter.MAX_SOURCE_BYTES)
    adapter._Deadline(max_seconds)
    _recovery_header(store_path)
    _existing_schema(store_path)
    with CrystalStore(store_path) as store:
        raw = adapter.restore_source(store, receipt["manifest_id"],
            expected_manifest_sha256=receipt["manifest_sha256"], max_seconds=max_seconds)
    if len(raw) != receipt["source_bytes"] or _sha(raw) != receipt["source_sha256"]:
        raise ValueError("Recovered source differs from external receipt")
    _new(output_path, raw)
    return dict(status="EXACT_RECOVERY", source_bytes=len(raw),
                source_sha256=_sha(raw), output=str(output_path))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Explicit supplied-candidate latent storage; no model execution")
    commands = parser.add_subparsers(dest="operation", required=True)
    write = commands.add_parser("put", help="Store one source of at most 4096 bytes with a bound plan")
    read = commands.add_parser("get", help="Recover from a database and hash receipt only")
    for command in (write, read):
        command.add_argument("--store", required=True, dest="store_path")
        command.add_argument("--receipt", required=True, dest="receipt_path")
        command.add_argument("--max-seconds", type=float, default=60,
                             help="Cooperative per-phase bound, 0 < seconds <= 300; not a process watchdog")
    write.add_argument("--source-id", required=True)
    write.add_argument("--file", required=True, dest="source_path")
    write.add_argument("--model", required=True, dest="model_path")
    write.add_argument("--table", required=True, dest="table_path")
    write.add_argument("--expected-model-sha256", required=True)
    write.add_argument("--expected-table-sha256", required=True)
    write.add_argument("--candidates", required=True, dest="candidates_path")
    write.add_argument("--index-mode", choices=("indexed", "scan"))
    read.add_argument("--output", required=True, dest="output_path")
    args = vars(parser.parse_args(argv))
    operation = args.pop("operation")
    try:
        result = put(**args) if operation == "put" else get(**args)
    except (ValueError, TypeError, KeyError, OSError, RuntimeError, RecursionError, sqlite3.DatabaseError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)[:1000]}), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
