"""Explicit local practice and retention; no implicit model or billing route."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import uuid

from .records import canonical


DEFAULT_CONFIG = Path.home() / "Library/Application Support/Lodestone/learning/ACTIVE.json"


def read(path):
    from lodestone_nurarihyon.common import read_bytes, strict_json
    return strict_json(read_bytes(path, 2 * 1024 * 1024), 2 * 1024 * 1024)


def command_provider(argv, timeout, *, cwd=None):
    """Trusted explicit argv; preserve actual stdout on errors, suppress stderr.

    This is a caller-selected adapter, not a sandbox or billing controller.
    No default provider, account, credential or network route is selected.
    A fenced caller can select its already-permitted working directory.
    """
    if type(argv) is not list or not argv or any(type(x) is not str or not x for x in argv):
        raise ValueError("Provider command must be a nonempty JSON argv")
    if type(timeout) is not int or not 1 <= timeout <= 600:
        raise ValueError("Provider timeout must be 1..600 seconds")
    def invoke(packet):
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=output,
                                       stderr=subprocess.DEVNULL, start_new_session=True, cwd=cwd)
            timed_out = False
            interruption = None
            try:
                process.communicate(canonical(packet), timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            except (KeyboardInterrupt, SystemExit) as exc:
                interruption = type(exc).__name__
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            except BaseException:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                raise
            size = output.tell()
            output.seek(0)
            raw = output.read(2 * 1024 * 1024)
            observation = {"source": "actual trusted command stdout", "exit_code": process.returncode,
                           "timed_out": timed_out, "stdout_bytes": size,
                           "retained_bytes": len(raw), "semantic_review": "NOT_PERFORMED"}
            if interruption is not None:
                observation["interruption"] = interruption
            try:
                response = raw.decode("utf-8")
            except UnicodeDecodeError:
                observation["stdout_hex"] = raw.hex()
                observation["invalid_utf8"] = True
                return {"status": "INTERRUPTED" if interruption else "FAILED", "response": None, "observation": observation}
            classification = ("INTERRUPTED" if interruption else
                              "COMPLETED" if process.returncode == 0 and not timed_out and size == len(raw) else "FAILED")
            if classification == "COMPLETED":
                try:
                    returned = json.loads(response)
                    state = returned.get("status") if isinstance(returned, dict) else None
                    classification = {"ABSTAIN": "ABSTAINED", "ABSTAINED": "ABSTAINED",
                                      "REFUSED": "REFUSED", "FAILED": "FAILED",
                                      "INTERRUPTED": "INTERRUPTED"}.get(state, classification)
                except ValueError:
                    observation["format"] = "text"
                if not response.strip():
                    classification = "ABSTAINED"
            return {"status": classification, "response": response, "observation": observation}
    return invoke


def main(argv=None):
    p = argparse.ArgumentParser(prog="loom-practice")
    p.add_argument("--config", default=str(DEFAULT_CONFIG))
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("reconcile")
    c = sub.add_parser("capture")
    c.add_argument("kind", choices=("evidence", "seed", "nacre", "familiarity"))
    c.add_argument("--file", required=True)
    c = sub.add_parser("prepare")
    c.add_argument("--encounter", required=True)
    c.add_argument("--purpose", required=True)
    c.add_argument("--reader", required=True)
    c.add_argument("--seed", action="append")
    c.add_argument("--applicability", help="JSON file with declared applicability for selected pairs")
    c = sub.add_parser("show")
    c.add_argument("handle")
    c = sub.add_parser("packet")
    c.add_argument("run_handle")
    c.add_argument("--kind", choices=("adapter", "subscription"), required=True)
    c.add_argument("--context", required=True, help="UTF-8 literal account file")
    c.add_argument("--response-schema", help="Required JSON schema file for subscription packet")
    c = sub.add_parser("run")
    c.add_argument("run_handle")
    c.add_argument("--kind", choices=("adapter", "subscription"), required=True)
    c.add_argument("--packet", required=True)
    c.add_argument("--provider-command", required=True, help="Explicit trusted JSON argv; no default model or billing route")
    c.add_argument("--timeout", type=int, default=120)
    c = sub.add_parser("finish")
    c.add_argument("run_handle")
    c.add_argument("--response", required=True, help="UTF-8 actual response file, not a target answer")
    c.add_argument("--status", required=True, choices=("COMPLETED", "FAILED", "ABSTAINED", "REFUSED", "INTERRUPTED"))
    c.add_argument("--observation", required=True, help="JSON actual observation including uncertainty")
    c = sub.add_parser("review")
    c.add_argument("outcome_handle")
    c.add_argument("--file", required=True)
    c = sub.add_parser("eligibility")
    c.add_argument("outcome_handle")
    c = sub.add_parser("promote-fold")
    c.add_argument("--workspace", required=True)
    c.add_argument("--run-id", required=True)
    c.add_argument("--outcome", required=True)
    from .loom_improvement_cli import add_parser as add_improvement_parser
    add_improvement_parser(sub)
    a = p.parse_args(argv)
    memory = None
    prepared = None
    try:
        from .bezoar_learning import LearningMemory
        from lodestone_nurarihyon import learning
        config = read(a.config)
        if config.get("schema") != "lodestone-learning-active/v1" or config.get("required") is not True:
            raise ValueError("A configured required practice lifecycle is needed")
        memory = LearningMemory(config["memory_workspace"])
        if a.command in ("prepare", "run") and (Path(a.config).parent / "STOP").exists():
            raise ValueError("Learning practice is stopped; retention and status remain available")
        if a.command == "improve":
            from .loom_improvement_cli import dispatch as improvement_dispatch
            result = improvement_dispatch(a, memory, config)
        elif a.command == "status":
            result = learning.status(memory)
            result["active_seed_handles"] = config["seed_handles"]
        elif a.command == "reconcile":
            result = learning.reconcile(memory)
        elif a.command == "capture":
            with learning.locked(memory):
                if a.kind == "familiarity":
                    from .bezoar_familiarity import retain
                    result = {"handle": retain(memory, read(a.file))}
                else:
                    result = {"handle": getattr(memory, a.kind)(read(a.file))}
        elif a.command == "prepare":
            result = learning.prepare(memory, a.seed or config["seed_handles"], encounter=a.encounter,
                                      purpose=a.purpose, reader=a.reader,
                                      applicability=read(a.applicability) if a.applicability else None)
        elif a.command == "show":
            result = memory.store.read(a.handle)
        elif a.command == "packet":
            from .learning_context import build_adapter_request, build_subscription_packet
            from lodestone_nurarihyon.common import read_bytes
            prepared = memory.read_prepared(a.run_handle)
            context = read_bytes(a.context, 32768).decode("utf-8")
            b = prepared["binding"]
            if a.kind == "adapter":
                result = build_adapter_request(memory, prepared, request_id=prepared["run_id"],
                                               context=context, encounter=b["encounter"], purpose=b["purpose"])
            else:
                if not a.response_schema:
                    raise ValueError("A subscription response schema is required")
                result = build_subscription_packet(memory, prepared, task=b["encounter"],
                                                  reference=context, response_schema=read(a.response_schema))
            learning.assess_prepared(memory, prepared, result)
        elif a.command == "run":
            from .learning_context import validate_practice_packet
            prepared = memory.read_prepared(a.run_handle)
            packet = validate_practice_packet(memory, prepared, read(a.packet), a.kind)
            provider = command_provider(json.loads(a.provider_command), a.timeout)
            result = learning.execute_prepared(memory, prepared, packet, provider,
                                               packet_builder=lambda saved, value: validate_practice_packet(memory, saved, value, a.kind))
        elif a.command == "finish":
            from lodestone_nurarihyon.common import read_bytes
            result = learning.finish(memory, memory.read_prepared(a.run_handle),
                                     response=read_bytes(a.response, 1024 * 1024).decode("utf-8"),
                                     status=a.status, observation=read(a.observation))
        elif a.command == "review":
            with learning.locked(memory):
                result = {"handle": memory.review_outcome(a.outcome_handle, read(a.file))}
        elif a.command == "promote-fold":
            from .origami import Origami
            from .learning_context import promote_origami_learning_fold
            result = {"handle": promote_origami_learning_fold(Origami(a.workspace), a.run_id, a.outcome)}
        else:
            result = memory.fold_eligibility(a.outcome_handle)
        print(canonical(result).decode("utf-8"))
        return 0
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        failure = {"status": "ERROR", "error": type(exc).__name__, "message": str(exc)[:500]}
        if memory is not None and a.command in ("prepare", "packet", "run"):
            try:
                with learning.locked(memory):
                    dispatched = prepared is not None and any(
                        row["kind"] in ("learning-dispatch", "learning-external-dispatch")
                        and row["payload"].get("run_handle") == prepared["run_handle"]
                        for _, row in learning._journal(memory))
                    if not dispatched and (prepared is None or memory.outcome_for(prepared) is None):
                        failure["failure_learning"] = memory.retain_preparation_failure(
                            "loom-practice." + a.command, "cli-" + uuid.uuid4().hex,
                            {"error_type": type(exc).__name__, "message": str(exc)[:500],
                             "stage": "command_failed_without_native_terminal",
                             "native_run_handle": prepared["run_handle"] if prepared else None})
            except (OSError, ValueError, KeyError, TypeError, RuntimeError) as retention_error:
                failure["failure_learning_error"] = type(retention_error).__name__
        print(json.dumps(failure), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
