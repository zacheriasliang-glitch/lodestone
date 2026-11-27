"""Explicit candidate engineering through the configured native practice lifecycle."""
import argparse
import json
from pathlib import Path


def add_parser(subparsers):
    parser = subparsers.add_parser("improve", help="Version, try and revise candidate skills/functions; no automatic training or installation")
    commands = parser.add_subparsers(dest="improvement_command", required=True)
    command = commands.add_parser("propose", help="Retain a candidate manifest and its exact source as advice")
    command.add_argument("--file", required=True)
    command = commands.add_parser("show", help="Read the complete bounded candidate family and actual trial history")
    command.add_argument("candidate")
    command = commands.add_parser("prepare", help="Bind this candidate and prior corrections to one native practice attempt")
    command.add_argument("candidate")
    command.add_argument("--encounter", required=True)
    command.add_argument("--purpose", required=True)
    command.add_argument("--reader", required=True)
    command.add_argument("--seed", action="append")
    command.add_argument("--run-id")
    command = commands.add_parser("run", help="Invoke one explicitly supplied trusted trial command; never executes commands from records")
    command.add_argument("attempt")
    command.add_argument("--provider-command", required=True, help="JSON argv for a trusted local evaluator, given the native packet on stdin; no shell or default provider")
    command.add_argument("--timeout", type=int, default=120)
    command.add_argument("--artifact-directory", help="Verify exact materialized candidate files before and after the trial; required for function candidates")
    command.add_argument("--voxel-context", help="Optional canonical voxel context JSON file for this attempt")
    command.add_argument("--pack-tool-context", action="store_true", help="Losslessly pack the complete snapshot envelope for a trusted tool evaluator that decodes it; not a model-readable prompt")
    command = commands.add_parser("review", help="Return an attributed correction to Bezoar for later attempts; does not promote a learning fold")
    command.add_argument("attempt")
    command.add_argument("--correction", required=True)
    command.add_argument("--author", required=True)
    command.add_argument("--observation", default="")
    command.add_argument("--event-id")
    command = commands.add_parser("materialize", help="Write retained candidate source to a NEW directory; never imports, runs or installs it")
    command.add_argument("candidate")
    command.add_argument("--destination", required=True)
    # Toolkit requests append named inputs after the chosen subcommand. Keep
    # the global --config form while supporting that exact explicit path too.
    for command in commands.choices.values():
        command.add_argument("--config", default=argparse.SUPPRESS, help=argparse.SUPPRESS)


def dispatch(args, memory, config):
    from .learning_cli import read, command_provider
    from .loom_improvement import ImprovementCycle
    cycle = ImprovementCycle(memory)
    operation = args.improvement_command
    if operation in ("prepare", "run") and (Path(args.config).parent / "STOP").exists():
        raise ValueError("Learning practice is stopped; retention and inspection remain available")
    if operation == "propose":
        return {"candidate_handle": cycle.propose(read(args.file)), "weight_update": False}
    if operation == "show":
        return cycle.inspect(args.candidate)
    if operation == "prepare":
        return cycle.prepare(args.candidate, seed_handles=args.seed or config["seed_handles"],
                             encounter=args.encounter, purpose=args.purpose, reader=args.reader,
                             run_id=args.run_id)
    if operation == "materialize":
        return cycle.materialize(args.candidate, args.destination)
    if operation == "review":
        return cycle.review(args.attempt, correction=args.correction, author=args.author,
                            observation=args.observation, event_id=args.event_id)
    if operation == "run":
        attempt = cycle.read_attempt(args.attempt)
        candidate_handle = attempt["candidate_handle"]
        candidate = cycle.read_candidate(candidate_handle)
        provider = command_provider(json.loads(args.provider_command), args.timeout)

        def trial(packet):
            # Verify within the broker callback so refusal/drift is retained as
            # an actual terminal attempt, rather than bypassing native capture.
            if candidate["kind"] == "function" and not args.artifact_directory:
                return {"status": "REFUSED", "response": None,
                        "observation": {"reason": "Function trials require --artifact-directory to bind exact candidate files"}}
            before = None
            if args.artifact_directory:
                try:
                    before = cycle.verify_materialized(candidate_handle, args.artifact_directory)
                except (OSError, ValueError) as exc:
                    return {"status": "REFUSED", "response": None,
                            "observation": {"reason": "Candidate artifact verification failed before command", "error_type": type(exc).__name__}}
            returned = provider(packet)
            observed = returned["observation"]
            observed["trial_command"] = json.loads(args.provider_command)
            observed["candidate_handle"] = candidate_handle
            observed["artifact_verification"] = before
            observed["scope"] = "Actual trusted evaluator result; does not prove the command used the candidate or that it improved later practice"
            if args.pack_tool_context:
                observed["tool_context_transport"] = {"schema": "loom-improvement-tool-input/v1",
                    "codec": "zlib+base64", "complete_snapshot_retained": True,
                    "model_readability": "NOT_CLAIMED", "authority": "NONE"}
            if args.artifact_directory:
                try:
                    cycle.verify_materialized(candidate_handle, args.artifact_directory)
                except (OSError, ValueError) as exc:
                    returned["status"] = "FAILED"
                    observed["artifact_drift"] = True
                    observed["artifact_error_type"] = type(exc).__name__
            return returned

        from lodestone_nurarihyon.common import read_bytes
        voxel = read_bytes(args.voxel_context, 262144).decode("utf-8") if args.voxel_context else None
        return cycle.run(args.attempt, provider=trial, pack_tool_context=args.pack_tool_context, voxel=voxel)
    raise ValueError("Unknown improvement operation")
