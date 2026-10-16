"""Train bounded numerical action policies; preserve state in Yggdrasil crystals."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys

from . import transformer
from .crystals import CrystalStore, packet_bytes
from .transformer_cli import _read_bytes
from .transformer_training import train


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="origami transformer train")
    parser.add_argument("--model", required=True)
    parser.add_argument("--dataset", required=True, help="JSON {examples:[{token_ids,target}], action_ids:[...]}")
    parser.add_argument("--expected-dataset-sha256", required=True)
    parser.add_argument("--store", required=True, help="Yggdrasil crystal database for input, model and report")
    parser.add_argument("--run-id", required=True, help="New run identifier; existing runs never replay")
    parser.add_argument("--authority-ref", required=True, help="Caller-supplied approval reference; not inherited authority")
    parser.add_argument("--steps", type=int, required=True)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--learning-rate", type=float, default=0.01)
    parser.add_argument("--mode", choices=("head", "full"), required=True)
    parser.add_argument("--max-seconds", type=float, required=True)
    args = parser.parse_args(argv)
    try:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", args.run_id):
            raise ValueError("run-id must be 1..80 simple identifier characters")
        _, raw = _read_bytes(args.dataset, 1024 * 1024)
        if _sha(raw) != args.expected_dataset_sha256:
            raise ValueError("Dataset differs from the supplied frozen hash")
        dataset = json.loads(raw)
        if type(dataset) is not dict or set(dataset) != {"examples", "action_ids"}:
            raise ValueError("Dataset requires exactly examples and action_ids")
        model = transformer.load_model(args.model)
        prefix = "training:" + args.run_id
        intent = {"run_id": args.run_id, "observed_at": datetime.now(timezone.utc).isoformat(),
                  "authority": "NONE", "authority_ref": args.authority_ref,
                  "input_model_sha256": transformer.model_sha256(model), "dataset_sha256": _sha(raw),
                  "mode": args.mode, "steps": args.steps, "seed": args.seed,
                  "learning_rate": args.learning_rate, "max_seconds": args.max_seconds}
        with CrystalStore(args.store) as store:
            try:
                store.get(prefix + ":intent")
            except KeyError:
                pass
            else:
                raise ValueError("Run ID already exists; completed or interrupted runs are not replayed")
            store.put(prefix + ":intent", packet_bytes(intent), {"kind": "training_intent"}, strategy="zlib", report=False)
            store.put(prefix + ":dataset", raw, {"kind": "training_input", "sha256": _sha(raw)}, strategy="zlib", report=False)
            try:
                trained, report = train(model, dataset["examples"], steps=args.steps, seed=args.seed,
                    learning_rate=args.learning_rate, mode=args.mode, max_seconds=args.max_seconds,
                    action_ids=dataset["action_ids"])
            except Exception as exc:
                store.put(prefix + ":failure", packet_bytes({"type": type(exc).__name__, "message": str(exc),
                           "run_id": args.run_id}), {"kind": "training_failure"}, strategy="zlib", report=False)
                raise
            envelope = {"format": "lodestone-transformer-state-v1", "sha256": transformer.model_sha256(trained),
                        "model": trained}
            store.put(prefix + ":model", packet_bytes(envelope), {"kind": "transformer_model",
                      "run_id": args.run_id, "semantic_validation": "NOT_ESTABLISHED"}, strategy="zlib", report=False)
            report.update(intent=intent, model_source_id=prefix + ":model", dataset_source_id=prefix + ":dataset",
                          semantic_validation="NOT_ESTABLISHED")
            store.put(prefix + ":report", packet_bytes(report), {"kind": "training_report"}, strategy="zlib", report=False)
        print(json.dumps({"status": report["status"], "run_id": args.run_id, "store": args.store,
                          "model_source_id": prefix + ":model", "report_source_id": prefix + ":report",
                          "model_sha256": envelope["sha256"], "report_sha256": _sha(packet_bytes(report))}))
        return 0 if report["status"] == "COMPLETED" else 2
    except (ValueError, OSError, KeyError, TypeError, RuntimeError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)[:1000]}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
