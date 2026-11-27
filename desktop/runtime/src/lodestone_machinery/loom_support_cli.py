"""One local learned passage-support decision behind pinned confirmation gates."""
import argparse
import json
from pathlib import Path
import sys

from . import loom_nli
from .fold_export import plain_path, regular_bytes
from .loom_support import prepare_support_request
from .loom_support_decision import decide, pin, require, validate_confirmation
from .loom_text import MAX_DOCUMENT_BYTES, sha, write_artifact
from .records import canonical


def _reject_constant(value):
    raise ValueError("Nonfinite JSON constant")


def _read(path, expected, *, canonical_required):
    raw = regular_bytes(path, MAX_DOCUMENT_BYTES)
    require(sha(raw) == pin(expected), "Input differs from its complete pinned bytes")
    value = json.loads(raw, object_pairs_hook=loom_nli._unique_pairs, parse_constant=_reject_constant)
    require(type(value) is dict, "A JSON object is required")
    if canonical_required:
        require(canonical(value) == raw, "Input requires exact canonical JSON")
    return raw, value


def _loaded_library_versions():
    """Read modules already loaded by LocalNLI; never import optional packages."""
    versions = {}
    for name in ("torch", "transformers"):
        version = getattr(sys.modules.get(name), "__version__", None)
        require(isinstance(version, str) and bool(version.strip()), "Loaded " + name + " version is unavailable")
        versions[name] = str(version)
    return versions


def main(argv=None):
    parser = argparse.ArgumentParser(prog="origami loom support",
        description="One learned passage-support decision; passing pinned confirmation is required. No native promotion.")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--fold-handle")
    parser.add_argument("--model-dir", required=True)
    for name in ("parent", "selector", "request", "model-manifest", "confirmation", "run-freeze"):
        parser.add_argument("--" + name, required=True)
        parser.add_argument("--expected-" + name + "-sha256", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        if sys.platform == "win32":
            raise OSError("Optional learned support execution is unavailable on Windows: the frozen NLI model custody requires POSIX")
        output = plain_path(args.output, new=True)
        snapshots, values = {}, {}
        for name in ("parent", "selector", "request", "model_manifest", "confirmation", "run_freeze"):
            snapshots[name], values[name] = _read(getattr(args, name), getattr(args, "expected_" + name + "_sha256"),
                canonical_required=name not in ("confirmation", "run_freeze"))
        require(set(values["request"]) == {"hypothesis"}, "Request requires exactly one hypothesis string")
        runtime_path = Path(loom_nli.__file__)
        require(runtime_path.suffix == ".py", "Confirmed NLI runtime must be a Python source file")
        runtime_bytes = regular_bytes(runtime_path, 262144)
        policy = validate_confirmation(values["confirmation"], values["run_freeze"],
            freeze_sha256=sha(snapshots["run_freeze"]), manifest=values["model_manifest"],
            manifest_sha256=sha(snapshots["model_manifest"]), runtime_sha256=sha(runtime_bytes))
        preparation_kwargs = {"expected_parent_sha256": sha(snapshots["parent"]),
                              "selector": values["selector"], "hypothesis": values["request"]["hypothesis"],
                              "fold_handle": args.fold_handle}
        prepared = prepare_support_request(args.workspace, snapshots["parent"], **preparation_kwargs)
        model_dir = plain_path(args.model_dir, directory=True)
        # All report, native-source, request, threshold and runtime bindings pass
        # before LocalNLI imports optional libraries and loads any model weights.
        scorer = loom_nli.LocalNLI(model_dir, values["model_manifest"], max_tokens=512, threads=2)
        require(canonical(scorer.identity) == canonical(policy["model"]),
                "Loaded model identity differs from the confirmed model")
        library_versions = _loaded_library_versions()
        score = scorer.score(prepared["premise"], prepared["hypothesis"])
        decision = decide(score, policy, prepared)
        unchanged = scorer.verify_unchanged()
        require(type(unchanged) is dict and unchanged.get("status") == "UNCHANGED", "Model assets changed")
        for name, raw in snapshots.items():
            require(regular_bytes(getattr(args, name), MAX_DOCUMENT_BYTES) == raw,
                    "Pinned " + name + " changed during support scoring")
        require(regular_bytes(runtime_path, 262144) == runtime_bytes, "Confirmed NLI runtime changed during scoring")
        # Reopen only the explicit native record closure, never original source
        # documents, so a changed record cannot acquire a result under old pins.
        require(canonical(prepare_support_request(args.workspace, snapshots["parent"], **preparation_kwargs))
                == canonical(prepared), "Native source preparation changed during scoring")
        result = {"schema": "loom-learned-support/v1", "status": "SCORED_WITH_PINNED_CONFIRMATION",
                  "decision": decision, "scope": "PASSAGE_SUPPORT_NOT_WORLD_TRUTH",
                  "preparation": prepared, "model": scorer.identity, "learned_score": score,
                  "loaded_library_versions": library_versions,
                  "claim_only_diagnostic": policy["claim_only_diagnostic"],
                  "policy": policy, "model_assets_unchanged": unchanged,
                  "complete_inputs": {name: {"sha256": sha(raw), "bytes": len(raw)}
                                      for name, raw in snapshots.items()},
                  "confirmation_scope": "NECESSARY_GATES_ONLY; natural deployment and current applicability remain unvalidated",
                  "not_supported_meaning": "NEUTRAL_OR_CONTRADICTED_NOT_NECESSARILY_FALSE",
                  "activation": "NONE", "authority": "NONE", "seed_promotion": "NONE"}
        receipt = write_artifact(output, result)
        print(json.dumps({"output": str(output), **receipt, "decision": decision,
                          "scope": result["scope"], "activation": "NONE", "seed_promotion": "NONE"}))
        return 0
    except (ValueError, OSError, KeyError, TypeError, UnicodeError, RecursionError, RuntimeError, OverflowError, ImportError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)[:1000]}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
