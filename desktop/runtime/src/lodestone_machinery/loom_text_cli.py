"""One explicit source-pinned text prediction with the optional local backend."""
import argparse
import json
import sys

def main(argv=None):
    parser = argparse.ArgumentParser(prog="origami loom text-predict",
        description="One uncalibrated record-support prediction; no training or native record promotion.")
    parser.add_argument("--experimental", action="store_true",
                        help="Explicitly opt in to unvalidated model scores; useful transfer is not established")
    parser.add_argument("--checkpoint", "--model", dest="checkpoint", required=True)
    parser.add_argument("--expected-checkpoint-sha256", "--expected-model-sha256", dest="expected_checkpoint_sha256", required=True)
    for name in ("request", "source", "record"):
        parser.add_argument("--" + name, required=True)
        parser.add_argument("--expected-" + name + "-sha256", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        if not args.experimental:
            raise ValueError("Loom text predictions require --experimental; reliable transfer has not been established")
        from . import loom_text as text
        from .fold_export import plain_path, regular_bytes
        output = plain_path(args.output, new=True)
        snapshots, values = {}, {}
        for name in ("checkpoint", "request", "source", "record"):
            path = getattr(args, name)
            raw = regular_bytes(path, text.MAX_DOCUMENT_BYTES)
            pin = getattr(args, "expected_" + name + "_sha256")
            if text.sha(raw) != pin:
                raise ValueError(name + " differs from its complete pinned bytes")
            snapshots[name] = raw
            if name != "source":
                value = json.loads(raw)
                if text.canonical(value) != raw:
                    raise ValueError(name + " requires exact canonical JSON")
                values[name] = value
        sources = {args.expected_source_sha256: snapshots["source"]}
        records = {args.expected_record_sha256: snapshots["record"]}
        # Verify label-free input and all provenance before importing PyTorch.
        request = text.validate_example(values["request"], sources, records, labeled=False)
        from .loom_text_torch import predict
        result = predict(values["checkpoint"], request, sources, records)
        for name, raw in snapshots.items():
            if regular_bytes(getattr(args, name), text.MAX_DOCUMENT_BYTES) != raw:
                raise ValueError("Pinned " + name + " changed during prediction")
        result["complete_inputs"] = {name: {"sha256": text.sha(raw), "bytes": len(raw)}
                                     for name, raw in snapshots.items()}
        result["complete_source_retention"] = "EXTERNAL_PINNED_SOURCE_AND_RECORD_UNMODIFIED"
        receipt = text.write_artifact(output, result)
        print(json.dumps({"output": str(output), **receipt, **text.BOUNDARY}))
        return 0
    except (ValueError, OSError, KeyError, TypeError, UnicodeError, RecursionError, RuntimeError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)[:1000]}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
