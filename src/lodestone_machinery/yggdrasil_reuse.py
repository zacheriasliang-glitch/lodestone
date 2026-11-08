"""Prepare a new Origami question from current, explicitly reviewed graph evidence."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from . import macro_voxel as mv
from . import yggdrasil_runtime as yr
from .lesson_context import MAX_APPLICABILITY_BYTES, build_lesson_context
from .origami import Origami, ROLES
from .organs._kernel import strict_load
from .records import RecordStore
from .source_seeds import SEED_FIELDS
from .support_review import reviewed_answer
from .yggdrasil_sources import binding_payload


def prepare_reuse(voxel, root, *, records_workspace, capture_roots, review_handle,
                  question, epoch, authority_ref, roles, max_calls=4, max_read_bytes=65536,
                  lesson_handle=None, lesson_applicability=None):
    """Reopen the original review and sources; never inherit its answer approval."""
    if (lesson_handle is None) != (lesson_applicability is None):
        raise ValueError("Select both a lesson handle and explicit present-question applicability")
    graph = mv.decode(voxel)
    if review_handle not in yr.record_anchors(graph):
        raise ValueError("Select a review handle already captured in this graph")
    if lesson_handle is not None and lesson_handle not in yr.record_anchors(graph):
        raise ValueError("Select a lesson handle already captured in this graph")
    freshness = yr.reenter(voxel, root, records_workspace=records_workspace,
                           capture_roots=capture_roots)
    if freshness["freshness"] != "CURRENT":
        raise ValueError("Reusing graph evidence requires CURRENT sources and records")
    store = RecordStore(records_workspace)
    review = store.relay("consumer", review_handle, owner="checker", kind="support_review")
    if len(review["links"]) != 1:
        raise ValueError("Review must identify one original answer")
    answer = store.relay("consumer", review["links"][0], owner="origami", kind="answer_evidence")
    if len(answer["links"]) != 1:
        raise ValueError("Answer must identify one original source binding")
    source_handle = answer["links"][0]
    source = store.relay("consumer", source_handle, owner="origami", kind="source_bindings")
    payload = binding_payload(source)
    mapped = (capture_roots or {}).get(source_handle)
    if mapped is None or str(Path(mapped).expanduser().resolve(strict=True)) != payload["source_root"]:
        raise ValueError("Origami reuse requires the original encounter source root; graph relocation does not migrate an encounter")
    organ = Origami(records_workspace)
    old_run = payload["run_id"]
    checked = reviewed_answer(organ, old_run, review_handle)
    if checked["status"] != "REVIEWED_PASS":
        raise ValueError("The selected original answer has no PASS review")
    seeds = [{key: passage[key] for key in sorted(SEED_FIELDS)} for passage in payload["evidence"]]
    if not seeds:
        raise ValueError("The original review has no retained passages to reuse")
    lesson_args = {}
    if lesson_handle is not None:
        if type(roles) is not dict or "checker" not in roles:
            raise ValueError("Lesson applicability requires a separately declared checker")
        lesson_args["lesson_context"] = build_lesson_context(
            store, lesson_handle, source_handle=source_handle, question=question,
            checker=roles["checker"], applicability=lesson_applicability)
    result = organ.start(mapped, [row["path"] for row in payload["scope"]["sources"]],
                         question, epoch, authority_ref, roles=roles, previous=old_run,
                         seed_passages=seeds, max_calls=max_calls, max_read_bytes=max_read_bytes,
                         **lesson_args)
    return {**result, "request": organ.request(result["run_id"]),
            "reuse": {"graph_sha256": voxel["sha256"], "review_handle": review_handle,
                      "source_handle": source_handle, "previous_run_id": old_run,
                      "source_freshness": "CURRENT", "retained_passages": len(seeds),
                      "new_answer_review": "REQUIRED", "review_inherited": False,
                      **({"lesson_handle": lesson_handle,
                          "lesson_context_sha256": lesson_args["lesson_context"]["sha256"],
                          "lesson_use": "UNTRUSTED_ADVICE_NOT_SOURCE_EVIDENCE"}
                         if lesson_handle is not None else {})},
            "authority": "NONE"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("voxel", type=Path)
    parser.add_argument("--root", required=True)
    parser.add_argument("--records-workspace", required=True)
    parser.add_argument("--capture-root", action="append", default=[], metavar="HANDLE=PATH")
    parser.add_argument("--review", required=True)
    parser.add_argument("--lesson", help="Exact reviewed NACRE handle captured in this graph")
    parser.add_argument("--lesson-applicability", type=Path,
                        help="Bounded lesson-applicability/v1 JSON for this question and checker")
    for name in ("question", "epoch", "authority-ref"):
        parser.add_argument("--" + name, required=True)
    for role in ROLES:
        parser.add_argument("--" + role, required=True)
    parser.add_argument("--max-calls", type=int, default=4)
    parser.add_argument("--max-read-bytes", type=int, default=65536)
    args = parser.parse_args(argv)
    try:
        if (args.lesson is None) != (args.lesson_applicability is None):
            raise ValueError("Supply --lesson and --lesson-applicability together")
        applicability = (strict_load(args.lesson_applicability, MAX_APPLICABILITY_BYTES)
                         if args.lesson_applicability is not None else None)
        voxel = mv.parse_json(yr.read(args.voxel.parent, args.voxel.name, mv.MAX_BYTES))
        result = prepare_reuse(voxel, args.root, records_workspace=args.records_workspace,
                               capture_roots=yr.parse_capture_roots(args.capture_root),
                               review_handle=args.review, question=args.question, epoch=args.epoch,
                               authority_ref=args.authority_ref,
                               roles={role: getattr(args, role) for role in ROLES},
                               max_calls=args.max_calls, max_read_bytes=args.max_read_bytes,
                               lesson_handle=args.lesson, lesson_applicability=applicability)
        print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
        return 0
    except (ValueError, OSError, RuntimeError, KeyError, TypeError, RecursionError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)[:1000]}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
