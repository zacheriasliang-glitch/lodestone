"""Synthetic encode/store/recover example; no fitting or model execution.

Usage: python3 -B examples/machinery/supplied_candidates.py NEW_OUTPUT_DIRECTORY
All files remain in that new directory. No historical research input is used.
"""
from contextlib import ExitStack, redirect_stdout
import argparse
import hashlib
import io
import json
from pathlib import Path
import sys
import time
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from lodestone_machinery.__main__ import main as machinery_main
from lodestone_machinery.crystals import CrystalStore
from lodestone_machinery.latent import candidate_inputs, codec, compact_checkpoint as cp
from lodestone_machinery.latent import latent_model, store_adapter as adapter


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def encoded(value):
    return (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()


def plan_for(source, table, chunk_bytes):
    """Bind two explicitly chosen fixture rows; never search/rank/generate rows."""
    raw = source["data"]
    chunks = []
    for offset in range(0, max(1, len(raw)), chunk_bytes):
        body = raw[offset:offset + chunk_bytes]
        chunks.append(dict(offset=offset, bytes=len(body), sha256=sha(body),
            candidates=[[[-8, -8, -7], [-8, -8, -8]] for _ in range(len(body) // 8)]))
    return dict(schema=candidate_inputs.SCHEMA,
        corpus_sha256=candidate_inputs.corpus_sha256([source]), table_sha256=sha(table),
        sources=[dict(id=source["id"], bytes=len(raw), sha256=sha(raw), chunks=chunks)])


def invoke(*args):
    output = io.StringIO()
    with redirect_stdout(output):
        status = machinery_main(list(map(str, args)))
    if status != 0:
        raise RuntimeError("Example CLI operation failed")
    return json.loads(output.getvalue())


def run(directory):
    started, cpu = time.monotonic(), time.process_time()
    directory = Path(directory)
    directory.mkdir()  # Never overwrite or reuse an earlier demonstration.
    source = dict(id="explicit-example", mandatory=False,
        data=bytes(8) + b"\x11" * 4 + bytes(4) + b"\x11" * 4 + b"\x10" + bytes(3) + b"tail")
    model = b"synthetic-engineering-model-placeholder"
    table = bytes(8) + b"\xff" * 8 + bytes(codec.TABLE_BYTES - 16)
    inputs = {"input.bin": source["data"], "model.bin": model, "table.bin": table,
              "candidates.json": encoded(plan_for(source, table, adapter.MAX_SOURCE_BYTES)),
              "catalog-candidates.json": encoded(plan_for(source, table, 1024))}
    for name, raw in inputs.items():
        (directory / name).write_bytes(raw)
    with ExitStack() as guards:
        for module, name in ((adapter.PreparedModel, "encode"), (adapter, "prepare_model"),
                             (latent_model, "initialize"), (latent_model, "encode_block"),
                             (latent_model, "compile_decoder")):
            guards.enter_context(mock.patch.object(module, name,
                side_effect=AssertionError("Example must not execute models")))
        stored = invoke("origami", "crystal", "latent", "put", "--store", directory / "source.sqlite",
            "--source-id", source["id"], "--file", directory / "input.bin", "--model", directory / "model.bin",
            "--table", directory / "table.bin", "--expected-model-sha256", sha(model),
            "--expected-table-sha256", sha(table), "--candidates", directory / "candidates.json",
            "--receipt", directory / "source-receipt.json", "--index-mode", "scan")
        recovered = invoke("yggdrasil", "crystal", "latent", "get", "--store", directory / "source.sqlite",
            "--receipt", directory / "source-receipt.json", "--output", directory / "recovered.bin")
        if (directory / "recovered.bin").read_bytes() != source["data"]:
            raise RuntimeError("CLI recovered bytes differ")
        bundle = adapter.load_candidate_bundle(model, table,
            expected_model_sha256=sha(model), expected_table_sha256=sha(table))
        with CrystalStore(directory / "catalog.sqlite", index_mode="scan") as store:
            catalog = cp.build_catalog(store, [source], encoding="latent", prepared=(bundle,),
                supplied_candidates=json.loads(inputs["catalog-candidates.json"]))
        (directory / "catalog-receipt.json").write_bytes(encoded(catalog["directory"]))
        with CrystalStore(directory / "catalog.sqlite") as store:
            body = cp.ReadSession(store, catalog["directory"]).read(source["id"])["data"]
            catalog_storage = store.inspect()
        if body != source["data"]:
            raise RuntimeError("Reopened catalog recovery differs")
        (directory / "catalog-recovered.bin").write_bytes(body)
        # An engineering baseline over the same fixture, not another research panel.
        first = codec.fold(source["data"], [(-8, -8, -7)] * 3, table)["accounting"]
    for name, raw in inputs.items():
        if (directory / name).read_bytes() != raw:
            raise RuntimeError("Example input changed")
    files = {p.name: dict(bytes=p.stat().st_size, sha256=sha(p.read_bytes()))
             for p in sorted(directory.iterdir()) if p.is_file()}
    catalog_deployed = sum(files[name]["bytes"] for name in ("catalog.sqlite", "catalog-receipt.json"))
    report = dict(classification="SYNTHETIC_ENGINEERING_ONLY", original_recovery_exact=True,
        source_bytes=len(source["data"]), source_sha256=sha(source["data"]),
        store_source=stored, cli_recovery=recovered, build_catalog=catalog,
        catalog_storage=catalog_storage, catalog_database_plus_receipt_bytes=catalog_deployed,
        first_candidate_only_accounting=first, files=files,
        retained_files_bytes_before_report=sum(f["bytes"] for f in files.values()),
        wall_seconds=time.monotonic() - started, cpu_seconds=time.process_time() - cpu,
        scope="Exact engineering recovery and accounted storage; not useful compression or learned transfer. "
              "Two independent deployments; input/reproduction copies are charged in files separately.")
    report_raw = encoded(report)
    (directory / "result.json").write_bytes(report_raw)
    return dict(result=str(directory / "result.json"), result_sha256=sha(report_raw),
        result_bytes=len(report_raw), total_retained_directory_file_bytes=report["retained_files_bytes_before_report"] + len(report_raw),
        original_recovery_exact=True, source_bytes=len(source["data"]),
        first_candidate_payload_bits=first["payload_bits"],
        selected_payload_bits=stored["layer_accounting"][0]["payload_bits"],
        source_database_plus_receipt_bytes=stored["retained_cost"]["database_plus_receipt_bytes"],
        catalog_database_plus_receipt_bytes=catalog_deployed)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    print(json.dumps(run(args.directory), indent=2))


if __name__ == "__main__":
    main()
