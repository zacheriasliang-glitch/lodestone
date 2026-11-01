"""Explicit preparation of user-owned policy and optional macOS startup files.

No service registration, task admission, network access or STOP changes.
"""
import argparse
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))


def _json(value):
    return json.dumps(value, indent=2, sort_keys=True, allow_nan=False).encode() + b"\n"


def _overlap(a, b):
    return a == b or a in b.parents or b in a.parents


def _new_output(path):
    path = Path(path).expanduser().absolute()
    if path != path.resolve() or path.exists() or not path.parent.is_dir():
        raise ValueError("Output must be a new directory beneath an existing canonical parent")
    if _overlap(path, ROOT):
        raise ValueError("User configuration must be outside the package")
    return path


def _pin(path, raw=None):
    path = Path(path)
    raw = path.read_bytes() if raw is None else raw
    return {"path": str(path), "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def configure(output, state, profile):
    from lodestone_nurarihyon.policy import DEFAULT_POLICY, validate_policy
    output = _new_output(output)
    state = Path(state).expanduser().absolute()
    if state != state.resolve() or state.exists() or _overlap(state, ROOT) or _overlap(state, output):
        raise ValueError("State must be a new canonical directory separate from package and configuration")
    policy = copy.deepcopy(DEFAULT_POLICY)
    if profile != "profile":
        policy["allowed_toolkits"].append("bezoar.document/v1")
    files = {}
    if profile not in ("profile", "documents"):
        policy["allowed_toolkits"].append("bezoar.reconciliation/v1")
        for name in ("capture", "observation"):
            if profile in (name, "all"):
                policy["allowed_toolkits"].append("bezoar.reconciliation-" + name + "/v1")
                policy["max_stage_bytes"] = 16 * 1024 * 1024
        source = ROOT / "tools/ecosystem"
        spec = importlib.util.spec_from_file_location("_public_conscience", source / "conscience.py")
        conscience = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(conscience)
        custody = conscience.load_policy(source / "conscience-policy.json")
        custody.pop("_pin")
        custody["actor_default"] = "local-user"
        custody["interpreter"]["pinned"] = sys.executable
        custody["writers"] = [
            {"prefix": str(state / "generations"), "owner": "local-user"},
            {"prefix": str(state / "worker.lock"), "owner": "local-user"},
        ]
        custody["writable_roots_by_actor"] = {}
        custody["policy_note"] = "Explicit local worker stage/lock custody only. No permission granted; controller approval, STOP and acceptance still required."
        conscience._validate_policy(custody)
        raw = _json(custody)
        files["conscience-policy.json"] = raw
        policy["conscience"] = {
            "schema": "nurarihyon-conscience-binding/v1", "actor": "local-user",
            "workspace": str(output / "assessment-workspace"),
            "policy": _pin(output / "conscience-policy.json", raw),
            "modules": {name: _pin(source / name) for name in ("conscience.py", "conscience_targets.py")},
        }
    validate_policy(policy)
    files["nurarihyon-policy.json"] = _json(policy)
    files["README.txt"] = ("Prepared user-owned policy; state not initialized.\n"
        "Review policy before explicit init --policy. No service or task has started.\n"
        "Keep this configuration and the pinned package at their chosen paths.\n"
        "Selected-state exports do not include this external policy or separate outcome memory.\n"
        "Editing a policy after init invalidates the sealed state; no implicit migration is supported.\n").encode()
    output.mkdir(mode=0o700)
    for name, raw in files.items():
        with (output / name).open("xb") as stream:
            stream.write(raw)
    return {"status": "PREPARED_NOT_INITIALIZED", "policy": str(output / "nurarihyon-policy.json"),
            "state": str(state), "toolkits": policy["allowed_toolkits"], "service_installed": False}


def startup(home, output, log_file):
    if sys.platform != "darwin":
        raise ValueError("Graphical-login LaunchAgent preparation is macOS-only; Linux may explicitly use foreground daemon")
    from lodestone_nurarihyon.controller import Controller, code_identity
    from lodestone_nurarihyon.deployment import environment_identity
    from lodestone_nurarihyon.startup import render_agent, preparation_manifest
    output = _new_output(output)
    controller = Controller(home)
    if _overlap(output, controller.home):
        raise ValueError("Startup output must not overlap state")
    with controller.locked():
        controller.verify()
        intent = {"runtime": str(ROOT), "state": str(controller.home),
                  "log_file": str(Path(log_file).expanduser().absolute()),
                  "code_sha": code_identity(), "python": sys.executable,
                  "environment_sha": environment_identity()}
        raw = render_agent(**intent)
        manifest = preparation_manifest(raw, **intent)
    output.mkdir(mode=0o700)
    for name, data in (("com.lodestone.nurarihyon.plist", raw), ("STARTUP.json", _json(manifest))):
        with (output / name).open("xb") as stream:
            stream.write(data)
    return {**manifest, "output": str(output)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    config = commands.add_parser("configure", help="Prepare policy only; no state/service/task creation")
    config.add_argument("--output", type=Path, required=True)
    config.add_argument("--state", type=Path, required=True)
    config.add_argument("--profile", choices=("profile", "documents", "reconciliation", "capture", "observation", "all"), default="documents")
    service = commands.add_parser("startup", help="Prepare a macOS plist only; never install or load it")
    service.add_argument("--home", type=Path, required=True)
    service.add_argument("--output", type=Path, required=True)
    service.add_argument("--log-file", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = (configure(args.output, args.state, args.profile) if args.command == "configure"
                  else startup(args.home, args.output, args.log_file))
        print(json.dumps(result, sort_keys=True))
        return 0
    except (ValueError, OSError, RuntimeError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
