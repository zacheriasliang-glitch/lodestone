"""Replaceable bounded local executor. Only the parent can publish state."""
import os
from pathlib import Path
import resource
import signal
import sys
import time

from .common import canonical, create_file, digest, file_lock, read_bytes, strict_json
from .policy import block_external_calls


def build(stage, source, task, *, fault=None):
    # This worker executes reviewed local APIs, not instructions from documents.
    # It has no network or subprocess capability through these declared APIs.
    block_external_calls(stage)
    from lodestone_machinery.engine import Engine
    from lodestone_pando.store import Board
    from .plans import validate_plan
    start = time.monotonic()
    stage, source = Path(stage), Path(source)
    raw = read_bytes(source, 65536)
    board = Board(stage / "pando")
    plan = task["plan"]
    validate_plan(plan, raw, board.settings())
    if fault == "wait":
        time.sleep(60)
    if fault == "cpu":
        while True:
            pass
    if fault == "memory":
        held = []
        for _ in range(40):
            held.append(bytearray(8 * 1024 * 1024))
            time.sleep(0.05)
        time.sleep(60)
    if fault == "output":
        with (stage / "oversize.bin").open("xb") as stream:
            for _ in range(32):
                stream.write(b"x" * 1024 * 1024)
                stream.flush()
                time.sleep(0.05)
    if fault == "exit":
        os._exit(71)
    if plan["toolkit"] in ("bezoar.document/v1", "bezoar.reconciliation/v1", "bezoar.reconciliation-capture/v1", "bezoar.reconciliation-observation/v1"):
        if plan["toolkit"] in ("bezoar.reconciliation-capture/v1", "bezoar.reconciliation-observation/v1"):
            from .complete_capture import build as build_capture
            from lodestone_machinery.bezoar_capture import sibling, enable_worker_cost_trace
            if plan["toolkit"] == "bezoar.reconciliation-observation/v1":
                enable_worker_cost_trace()  # Diagnostic overlay only; stderr is already owned and retained.
            result = build_capture(board, raw, task, bundle=sibling(source), scratch=stage / "reconciliation-work")
        elif plan["toolkit"] == "bezoar.document/v1":
            from .documents import build_document
            result = build_document(board, raw, task)
        else:
            from .reconciliation import build_reconciliation
            result = build_reconciliation(board, raw, task, scratch=stage / "reconciliation-work")
        result.update({"schema": "nurarihyon-worker-result/v1", "task_id": task["id"],
                       "worker_wall_seconds": time.monotonic() - start,
                       "worker_cpu_seconds": time.process_time(),
                       "worker_maxrss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == "darwin" else 1024),
                       "cpu_measurement_scope": "Whole worker process through report construction; excludes report write and exit",
                       "external_adapters": "BLOCKED_IN_PROCESS", "publication_authority": "NONE; staging only"})
        create_file(stage / "worker-result.json", canonical(result) + b"\n")
        return
    # Existing durable literal-brief engine and mechanical auditor are reused.
    # Their job is completed here; historical absolute source locations are
    # provenance, not commands to replay after export.
    engine = Engine(stage / "machinery")
    job = engine.submit(source.parent, [source.name], "board_name", max_passages=4)
    brief = engine.run(job)
    if brief["state"] != "COMPLETED":
        raise ValueError("Existing literal toolkit did not pass its acceptance checks")
    brief_path = Path(brief["result"]["artifact"])
    source_id = board.ingest(raw, title="Authorized local profile source",
                             uri="sha256:" + digest(raw), author="host", kind="file")
    request = board.eat([source_id], author="host", question="Copy the declared baseline before one versioned view change.",
                        title="Local profile ingestion")
    proposal = {
        "schema": "kernel-digestion/v1", "id": "baseline-" + task["id"], "actor": "host",
        "request_handle": board.artifact(request)["handle"],
        "operators": {name: {"value": value, "source": source_id + ".txt", "quote": raw.decode("utf-8"),
                              "rationale": "Exact typed baseline from the authorized local source."}
                      for name, value in plan["baseline"].items()},
        "instructions": [], "rejected": [],
        "uncertainties": ["Mechanically derived from the approved typed source, not host-authored reasoning. "
                          + "Task " + task["id"] + "; plan_sha256 " + digest(canonical(plan)) + ".",
                          "A declared display change; no utility, learning or compression claim."],
    }
    baseline = board.digest(request, proposal, author="host", title="Source-backed baseline")
    operator = plan["operator"]
    mutation = {"schema": "kernel-mutagen/v1", "id": "change-" + task["id"],
                "new_id": "candidate-" + task["id"], "operator": operator,
                "expected": plan["baseline"][operator], "replacement": plan["candidate"][operator],
                "rationale": plan["rationale"]}
    candidate = board.mutate(baseline, mutation, author="host", title="Staged versioned profile")
    validate_plan(plan, raw, board.settings())
    application = board.apply_profile(candidate, author="host")
    verification = board.verify()
    result = {"schema": "nurarihyon-worker-result/v1", "task_id": task["id"],
              "source_id": source_id, "request_artifact": request,
              "baseline_artifact": baseline, "candidate_artifact": candidate,
              "application_handle": application["handle"],
              "brief_job": job, "brief_path": brief_path.relative_to(stage).as_posix(),
              "brief_sha256": digest(read_bytes(brief_path, 1048576)),
              "before": plan["baseline"], "after": plan["candidate"],
              "board_verification": verification,
              "worker_wall_seconds": time.monotonic() - start,
              "worker_cpu_seconds": time.process_time(),
              "cpu_measurement_scope": "Whole worker process through final report construction; excludes report write and exit",
              "worker_maxrss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == "darwin" else 1024),
              "external_adapters": "BLOCKED_IN_PROCESS",
              "publication_authority": "NONE; staging only"}
    create_file(stage / "worker-result.json", canonical(result) + b"\n")


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("stage"); parser.add_argument("source"); parser.add_argument("task")
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("--worker-lock", required=True)
    parser.add_argument("--fault", choices=("wait", "cpu", "memory", "output", "exit"))
    args = parser.parse_args(argv)
    task = strict_json(read_bytes(args.task))
    limits = task["limits"]
    resource.setrlimit(resource.RLIMIT_CPU, (limits["worker_cpu_seconds"], limits["worker_cpu_seconds"]))
    resource.setrlimit(resource.RLIMIT_FSIZE, (limits["max_stage_bytes"], limits["max_stage_bytes"]))
    signal.signal(signal.SIGXFSZ, signal.SIG_DFL)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    # Retains a wall deadline even if the supervising process is killed.
    signal.alarm(limits["worker_wall_seconds"])
    try:
        with file_lock(args.worker_lock):
            if os.getppid() != args.parent_pid:
                raise ValueError("Controller exited before worker acquired custody")
            build(args.stage, args.source, task, fault=args.fault)
    except Exception as exc:
        print(canonical({"error": type(exc).__name__, "message": str(exc)[:1500]}).decode(), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
