"""Run Origami's experimental numerical transformer and retain producer records."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from time import perf_counter

from . import transformer
from .origami import Origami, MAX_JSON, _aggregate, render_answer
from .records import RecordStore, canonical
from .usage import usage_totals


MAX_TOKEN_JSON_BYTES = 4096


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _read_bytes(path, limit):
    path = Path(path).expanduser().absolute()
    if path.is_symlink() or not path.is_file():
        raise ValueError("Input must be a regular file, not a symlink")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Input exceeds its byte bound")
    return path, data


def _tokens(args, model):
    if args.fold_run is not None:
        organ = Origami(args.workspace)
        # Validate the run ID and directory through the existing runtime first.
        directory = organ._dir(args.fold_run)
        snapshots = {name: _read_bytes(directory / name, MAX_JSON)[1]
                     for name in ("intent.json", "result.json", "answer.md")}
        intent = json.loads(snapshots["intent.json"])
        saved_result = json.loads(snapshots["result.json"])
        if type(intent) is not dict or type(saved_result) is not dict:
            raise ValueError("Fold input requires object intent and result records")
        if intent.get("id") != args.fold_run or saved_result.get("run_id") != args.fold_run:
            raise ValueError("Fold result and intent must match the requested run ID")
        if saved_result.get("scope") != intent.get("scope"):
            raise ValueError("Fold result scope differs from saved intent.scope")
        fold = saved_result.get("fold")
        if type(fold) is not dict or saved_result.get("answer") != fold.get("answer"):
            raise ValueError("Fold result answer differs from fold.answer")
        # _finish renders these freshly computed dictionaries before canonical
        # result JSON sorts their keys. Recompute their original display order,
        # requiring the recorded values to agree before using the same renderer.
        summaries = {"provider_constraints": _aggregate(saved_result["calls"]),
                     "usage": usage_totals(saved_result["calls"])}
        if any(saved_result.get(key) != value for key, value in summaries.items()):
            raise ValueError("Fold call summaries differ from the recorded provider calls")
        rendered = render_answer({**saved_result, **summaries}).encode("utf-8")
        if (rendered != snapshots["answer.md"]
                or _hash(rendered) != saved_result.get("artifact_sha256")):
            raise ValueError("Fold input requires an intact answer artifact matching the rendered result")
        result = organ.inspect(args.fold_run)
        inspection = result.get("inspection", {})
        if (result.get("status") != "ANSWER" or not inspection.get("fresh")
                or not inspection.get("artifact_intact")):
            raise ValueError("Fold input requires an ANSWER with current sources and an intact answer artifact")
        if {key: value for key, value in result.items() if key != "inspection"} != saved_result:
            raise ValueError("Origami inspection did not use the saved result snapshot")
        for name, original in snapshots.items():
            if _read_bytes(directory / name, MAX_JSON)[1] != original:
                raise ValueError("Origami " + name + " changed while inspecting the token input")
        answer = result.get("answer")
        if type(answer) is not str or not 1 <= len(answer) <= model["config"]["max_tokens"]:
            raise ValueError("Fold answer must contain 1 to max_tokens characters; no automatic shortening")
        if any(len(token) != 1 for token in model["vocabulary"]):
            raise ValueError("Fold input requires a one-character-per-token vocabulary")
        token_map = {token: index for index, token in enumerate(model["vocabulary"])}
        if any(character not in token_map for character in answer):
            raise ValueError("Fold answer contains characters absent from the explicit vocabulary")
        ids = [token_map[character] for character in answer]
        source = {"kind": "origami_answer", "run_id": args.fold_run,
                  "result_path": str(directory / "result.json"),
                  "result_sha256": _hash(snapshots["result.json"]),
                  "intent_sha256": _hash(snapshots["intent.json"]),
                  "artifact_sha256": _hash(snapshots["answer.md"]),
                  "bindings_checked": ["run_id", "intent_scope", "fold_answer", "rendered_artifact", "unchanged_snapshot"],
                  "text": answer, "text_sha256": _hash(answer.encode("utf-8")),
                  "inspection": inspection, "sources": result["scope"]["sources"],
                  "interpretation": "Literal token input only; the transformer does not validate or update this answer."}
    else:
        if args.tokens_file is not None:
            path, data = _read_bytes(args.tokens_file, MAX_TOKEN_JSON_BYTES)
            source = {"kind": "token_ids_file", "path": str(path)}
        else:
            data = args.token_ids_json.encode("utf-8")
            if len(data) > MAX_TOKEN_JSON_BYTES:
                raise ValueError("Token JSON exceeds its byte bound")
            source = {"kind": "inline_token_ids"}
        ids = json.loads(data)
        source.update(sha256=_hash(data), bytes=len(data), json_text=data.decode("utf-8"))
    return ids, source


def _model_record(store, model):
    return store.publish("origami", "transformer_model", {
        "model_sha256": transformer.model_sha256(model), "model": model,
        "semantic_validation": "NOT_ESTABLISHED"})


def _implementation():
    return [{"file": path.name, "sha256": _hash(path.read_bytes())}
            for path in (Path(transformer.__file__), Path(__file__))]


def _parser():
    parser = argparse.ArgumentParser(
        prog="origami transformer",
        description="Bounded causal transformer inference; seeded initialization is UNTRAINED.")
    operations = parser.add_subparsers(dest="operation", required=True)
    for name in ("init", "forward", "generate"):
        sub = operations.add_parser(name)
        sub.add_argument("--workspace", required=True, help="Explicit producer-record/output workspace")
        sub.add_argument("--model", required=True, help="Hash-bound model JSON; init creates a new file inside the workspace")
        if name == "init":
            sub.add_argument("--vocabulary-json", required=True, help="JSON array of distinct literal token strings")
            sub.add_argument("--seed", type=int, default=0)
            sub.add_argument("--width", type=int, default=16)
            sub.add_argument("--heads", type=int, default=2)
            sub.add_argument("--layers", type=int, default=1)
            sub.add_argument("--max-tokens", type=int, default=32)
            sub.add_argument("--ff-width", type=int)
        else:
            inputs = sub.add_mutually_exclusive_group(required=True)
            inputs.add_argument("--token-ids-json", help="Explicit JSON list of integer vocabulary indices")
            inputs.add_argument("--tokens-file", help="UTF-8 JSON list, at most 4096 bytes")
            inputs.add_argument("--fold-run", help="Existing Origami run in this workspace; use its short fresh ANSWER as character tokens")
        if name == "generate":
            sub.add_argument("--max-new-tokens", type=int, default=8)
            sub.add_argument("--eos-token-id", type=int)
    return parser


def main(argv=None):
    supplied = sys.argv[1:] if argv is None else argv
    if supplied and supplied[0] == "train":
        from .transformer_training_cli import main as training_main
        return training_main(supplied[1:])
    args = _parser().parse_args(supplied)
    try:
        started = perf_counter()
        observed_at = datetime.now(timezone.utc).isoformat()
        workspace = Path(args.workspace).expanduser().resolve()
        if args.operation == "init":
            if len(args.vocabulary_json.encode("utf-8")) > 131072:
                raise ValueError("Vocabulary JSON exceeds its byte bound")
            model = transformer.initialize_model(json.loads(args.vocabulary_json), seed=args.seed,
                width=args.width, heads=args.heads, layers=args.layers, max_tokens=args.max_tokens,
                ff_width=args.ff_width)
            model_path = Path(args.model).expanduser().absolute()
            if not model_path.resolve().is_relative_to(workspace):
                raise ValueError("Initialize the model inside the explicit workspace")
            if model_path.exists() or model_path.is_symlink():
                raise ValueError("Model output already exists; initialization never overwrites it")
            store = RecordStore(workspace)
            model_path.parent.mkdir(parents=True, exist_ok=True)
            saved = transformer.save_model(model, model_path)
            model_record = _model_record(store, model)
            payload = {"observed_at": observed_at, "model": saved,
                       "model_record": model_record, "provenance": model["provenance"],
                       "config": model["config"], "seconds": perf_counter() - started,
                       "implementation": _implementation(), "semantic_validation": "NOT_ESTABLISHED"}
            record = store.publish("origami", "transformer_init", payload, links=[model_record])
            output = {"status": "INITIALIZED", "operation": args.operation,
                      "record": record, "record_path": str(store.path(record)), **payload}
        else:
            model = transformer.load_model(Path(args.model).expanduser())
            ids, source = _tokens(args, model)
            if args.operation == "forward":
                result = transformer.forward(model, ids)
            else:
                result = transformer.generate(model, ids, max_new_tokens=args.max_new_tokens,
                                              eos_token_id=args.eos_token_id)
            # Validate and compute before creating any new producer records.
            store = RecordStore(workspace)
            model_record = _model_record(store, model)
            input_record = store.publish("origami", "transformer_input", {
                "token_ids": ids, "token_ids_sha256": _hash(canonical(ids)), "source": source})
            payload = {"observed_at": observed_at, "model_path": str(Path(args.model).expanduser().absolute()),
                       "model_record": model_record, "input_record": input_record,
                       "token_ids_sha256": _hash(canonical(ids)), "result": result,
                       "seconds": perf_counter() - started, "implementation": _implementation()}
            if args.operation == "generate":
                payload["generation_limits"] = {"max_new_tokens": args.max_new_tokens,
                                                 "eos_token_id": args.eos_token_id}
            record = store.publish("origami", "transformer_" + args.operation, payload,
                                   links=[model_record, input_record])
            output = {"status": "COMPUTED", "operation": args.operation,
                      "record": record, "record_path": str(store.path(record)), **payload}
        print(json.dumps(output, ensure_ascii=False, allow_nan=False, indent=2))
        return 0
    except (ValueError, OSError, KeyError, TypeError, RuntimeError, RecursionError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)[:1000]}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
