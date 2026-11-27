"""Read-only inference memory assessment and explicit reusable skill packaging.

The catalogue never executes a model. Actual inference remains inside a caller's
configured native practice provider and bounded supervisor.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys

from .fold_export import regular_bytes
from .records import canonical
from lodestone_nurarihyon.portable_files import absolute_path, durable_write, guarded_directory

INPUT_SCHEMA = "loom-inference-memory-input/v1"
SKILL_PATH = "skills/lodestone-inference-memory/SKILL.md"
HELPER_PATH = "src/lodestone_machinery/loom_inference_memory.py"
MAX_ARTIFACT_BYTES = 32768


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate assessment field")
        result[key] = value
    return result


def assess(spec):
    """Assess caller-supplied geometry, never attest model identity or RSS fit."""
    expected = {"schema", "model_config", "prompt_tokens", "max_new_tokens",
                "max_rss_bytes", "prefill_chunk_tokens"}
    if type(spec) is not dict or set(spec) != expected or spec["schema"] != INPUT_SCHEMA:
        raise ValueError("Expected the complete explicit inference-memory input")
    from .loom_inference_memory import assess_memory
    result = assess_memory(spec["model_config"], prompt_tokens=spec["prompt_tokens"],
        max_new_tokens=spec["max_new_tokens"], max_rss_bytes=spec["max_rss_bytes"],
        prefill_chunk_tokens=spec["prefill_chunk_tokens"])
    return {"schema": "loom-inference-memory-assessment/v1", "assessment": result,
            "input_sha256": hashlib.sha256(canonical(spec)).hexdigest(),
            "input_attribution": "CALLER_SUPPLIED_PLANNING_DATA",
            "model_identity_verified": False, "rss_fit_guaranteed": False,
            "model_calls": 0, "weight_updates": 0, "authority": "NONE",
            "use": "Inspect estimates and actual prior stop evidence; this result neither authorizes nor launches inference."}


def candidate_manifest(root, *, evidence_handles=()):
    """Package only this skill and helper for the existing improvement cycle."""
    supplied = Path(root).expanduser()
    root = absolute_path(supplied) if os.name == "nt" else supplied.resolve()
    if (type(evidence_handles) not in (list, tuple) or len(evidence_handles) > 16 or
            len(set(evidence_handles)) != len(evidence_handles) or
            any(type(value) is not str or re.fullmatch(r"bezoar:[0-9a-f]{64}", value) is None
                for value in evidence_handles)):
        raise ValueError("Supply at most 16 explicit distinct native evidence handles")
    artifacts = []
    with guarded_directory(root):
        for relative, exported in ((SKILL_PATH, "SKILL.md"), (HELPER_PATH, "loom_inference_memory.py")):
            raw = regular_bytes(root / relative, MAX_ARTIFACT_BYTES)
            artifacts.append({"path": exported, "content": raw.decode("utf-8")})
    if sum(len(row["content"].encode()) for row in artifacts) > MAX_ARTIFACT_BYTES:
        raise ValueError("Complete skill and helper exceed the native 32768-byte candidate limit")
    return {"id": "lodestone-inference-memory", "kind": "function",
            "purpose": "Reduce avoidable allocation during explicitly bounded local inference while preserving the full admitted input and outcomes.",
            "conditions": [
                "A local inference task has a measured full prompt, explicit output reserve, resource bounds and an applicable supported model/runtime contract.",
                "The caller has a current reason to inspect inference memory; unrelated organ operations need no model or inference skill.",
                "An actual trial uses the selected exact helper with the matching Lodestone runtime, native admission and an external process-group supervisor."],
            "frame_limits": [
                "Geometry estimates exclude runtime and transient overhead; they do not guarantee RSS fit or establish the cause of a prior stop.",
                "A completed generation or passing fixture does not establish semantic learning, transfer, usefulness or weight updates.",
                "No automatic model call, retry, larger resource cap, context truncation, provider change or installation is implied.",
                "The helper depends on its compatible Lodestone package and explicitly supported backend; materialization alone is not an executable deployment."],
            "change": "Provide a shared memory assessment and bounded full-history inference implementation, with concise instructions for actual-outcome retention and later reuse.",
            "artifacts": artifacts, "evidence_handles": list(evidence_handles),
            "author": "Lodestone engineering candidate; actual evidence supplied explicitly by the caller"}


def main(argv=None, *, root=None):
    parser = argparse.ArgumentParser(prog="lodestone loom-tools inference-memory")
    commands = parser.add_subparsers(dest="action", required=True)
    assessment = commands.add_parser("assess", help="Estimate memory from explicit planning geometry; no model load")
    assessment.add_argument("--file", required=True)
    skill = commands.add_parser("candidate", help="Write a lean native candidate manifest; no execution or proposal")
    skill.add_argument("--output", required=True)
    skill.add_argument("--evidence-handle", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        if args.action == "assess":
            raw = regular_bytes(args.file, 65536)
            result = assess(json.loads(raw, object_pairs_hook=_unique))
            result["input_file_sha256"] = hashlib.sha256(raw).hexdigest()
        else:
            root = root or Path(__file__).resolve().parents[2]
            value = candidate_manifest(root, evidence_handles=args.evidence_handle)
            destination = Path(args.output).expanduser()
            destination = (absolute_path(destination) if os.name == "nt" else
                           destination.parent.resolve() / destination.name)
            raw = canonical(value)
            durable_write(destination, raw)
            result = {"schema": "loom-inference-memory-candidate-export/v1", "output": str(destination.resolve()),
                      "sha256": hashlib.sha256(raw).hexdigest(), "artifacts": len(value["artifacts"]),
                      "native_proposal_created": False, "model_calls": 0, "authority": "NONE"}
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)[:1000]}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
