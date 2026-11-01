"""Exercise public task interfaces using new synthetic state, never an installed service.

Running this script explicitly authorizes its five synthetic test tasks. Package
installation does not invoke it. No model, credentials, network or launchd use.
"""
import argparse
import json
import os
from pathlib import Path
import platform
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]


def exercise(base):
    base = Path(base).resolve()
    base.mkdir(exist_ok=False)
    user = base / "home"
    user.mkdir()
    env = {**os.environ, "HOME": str(user), "USERPROFILE": str(user),
           "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"}
    env.pop("PYTHONPATH", None)
    checks = []
    begin = time.monotonic()

    def command(label, *args, expected=0, launcher=True):
        argv = [sys.executable, "-B", str(ROOT / "lodestone.py"), *map(str, args)] if launcher else list(map(str, args))
        result = subprocess.run(argv, cwd=base, env=env, capture_output=True, text=True, timeout=180)
        if result.returncode != expected:
            raise RuntimeError(label + ": " + result.stdout[-6000:] + result.stderr[-6000:])
        checks.append({"check": label, "exit": result.returncode})
        return json.loads(result.stdout) if result.stdout.strip() else None

    command("synthetic source preparation", sys.executable, "-B", ROOT / "examples/nurarihyon/prepare_sources.py",
            "--output", base / "inputs", launcher=False)
    state, config, sources = base / "state", base / "config", base / "inputs"
    prepared = command("explicit all-toolkit policy", "nurarihyon", "configure", "--output", config,
                       "--state", state, "--profile", "all")
    assert prepared["status"] == "PREPARED_NOT_INITIALIZED" and not state.exists()
    pins = command("read-only execution identity", "nurarihyon", "identity")
    assert len(pins["code_sha256"]) == len(pins["environment_sha256"]) == 64 and not state.exists()
    command("repeat configure refuses overwrite", "nurarihyon", "configure", "--output", config,
            "--state", state, expected=2)
    command("explicit initialization", "nurarihyon", "--home", state, "init", "--policy", config / "nurarihyon-policy.json")

    def control(label, *args, **kwargs):
        return command(label, "nurarihyon", "--home", state, *args, **kwargs)

    assert len(command("all five toolkit descriptions", "nurarihyon", "registry")["toolkits"]) == 5
    before = control("initial board", "view")
    tasks = []

    def task(source, toolkit, key):
        planned = control(key + " submit", "submit", "--toolkit", toolkit, "--source", source, "--key", key)
        assert planned["state"] == "PLANNED", planned
        assert control(key + " no implicit approval", "run")["status"] == "IDLE"
        control(key + " wrong plan refused", "approve", planned["id"], "--plan-sha", "0" * 64, expected=2)
        control(key + " explicit approval", "approve", planned["id"], "--plan-sha", planned["plan_sha"])
        finished = control(key + " worker and independent acceptance", "run")
        assert finished["state"] == "COMPLETED" and finished["attempts"] == 1, finished
        again = control(key + " idempotent resubmission", "submit", "--toolkit", toolkit, "--source", source, "--key", key)
        assert again["id"] == finished["id"] and again["attempts"] == 1
        tasks.append({"id": finished["id"], "toolkit": toolkit, "state": finished["state"], "generation": finished["result"].get("generation")})
        return finished

    profile = task(sources / "profile.json", "pando.profile/v1", "synthetic-profile")
    memory = base / "memory"
    assert control("outcome retention", "remember", profile["id"], "--memory", memory)["status"] == "RETAINED"
    assert control("outcome retention idempotency", "remember", profile["id"], "--memory", memory)["status"] == "ALREADY_RETAINED"
    later = json.loads((sources / "profile.json").read_text())
    later["baseline"] = later["candidate"]
    later["candidate"] = {**later["baseline"], "board_name": "Next local workshop"}
    next_source = base / "later.json"
    next_source.write_text(json.dumps(later))
    context = control("fresh-process retained context", "context", "--source", next_source, "--memory", memory)
    # Inspect saved source-backed context, without claiming useful learning.
    assert context["outcome_memory"]["matching_records"] == 1, context
    assert context["outcome_memory"]["records"][0]["evidence"]["task_id"] == profile["id"], context
    assert context["outcome_memory"]["applicability"] == "NOT_ESTABLISHED"
    command("document preparation", "nurarihyon", "capture-document", "--document", sources / "document.md",
            "--title", "Synthetic workshop", "--output", base / "document.json")
    task(base / "document.json", "bezoar.document/v1", "synthetic-document")
    task(sources / "reconciliation.json", "bezoar.reconciliation/v1", "synthetic-reconciliation")
    for kind in ("capture", "observation"):
        raw = base / (kind + ".json")
        command(kind + " preparation", "nurarihyon", "capture-corpus", "--root", sources / "corpus",
                "--spec", sources / (kind + "-spec.json"), "--output", raw,
                *(["--observation"] if kind == "observation" else []))
        task(raw, "bezoar.reconciliation-" + kind + "/v1", "synthetic-" + kind)
    after = control("final board", "view")
    assert after["posts"] == before["posts"] and after["shelves"] == before["shelves"]
    assert after["name"] == "My local workshop"
    integrity = control("final integrity", "verify")
    assert integrity["status"] == "INTACT", integrity
    status = control("five completed tasks", "status")
    assert len(status["tasks"]) == 5 and all(t["state"] == "COMPLETED" for t in status["tasks"])
    control("explicit STOP", "stop")
    assert control("STOP prevents execution", "run")["status"] == "STOPPED"
    marker = (state / "STOP").read_bytes()
    exported = control("consistent selected-state export", "export", "--output", base / "export")
    assert exported["status"] == "EXPORTED_VERIFIED"
    restored = command("isolated restore", "nurarihyon", "restore", "--package", base / "export",
                       "--descriptor", base / "export.descriptor.sha256", "--output", base / "restored")
    assert restored["status"] == "RESTORED_VERIFIED_INERT"
    restored_home = base / "restored/state"
    assert (restored_home / "STOP").read_bytes() == marker
    for operation in ("status", "verify"):
        reopened = command("restored read-only " + operation, sys.executable, "-B",
                           base / "restored/runtime/tools/nurarihyon/run", "--home", restored_home,
                           operation, launcher=False)
        if operation == "status":
            assert reopened["stopped"] and len(reopened["tasks"]) == 5
        else:
            assert reopened["status"] == "INTACT" and reopened["tasks"] == 5
    with sqlite3.connect((restored_home / "control.sqlite3").as_uri() + "?mode=ro", uri=True) as db:
        assert db.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert db.execute("SELECT count(*) FROM tasks WHERE state='COMPLETED'").fetchone()[0] == 5
    assert control("original state remains stopped", "status")["active_generation"] == status["active_generation"]
    return {"status": "PASS", "platform": platform.platform(), "python": platform.python_version(),
            "checks": checks, "tasks": tasks, "integrity": integrity,
            "restore": restored, "stop_preserved": True, "model_calls": 0,
            "restored_read_only_cli_checks": True, "restored_tasks_executed": False,
            "service_installed": False, "wall_seconds": time.monotonic() - begin,
            "scope": "Synthetic engineering; five real worker/acceptance operations, explicit profile retention, selected-state recovery; no learning claim"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="New retained synthetic evidence directory; default temporary")
    args = parser.parse_args()
    if sys.platform not in ("darwin", "linux"):
        parser.error("Nurarihyon smoke requires macOS or Linux; Windows new runtime is unsupported")
    if args.output:
        result = exercise(args.output)
        (args.output / "RESULT.json").write_text(json.dumps(result, indent=2) + "\n")
    else:
        with tempfile.TemporaryDirectory(prefix="lodestone-nurarihyon-public-") as temp:
            result = exercise(Path(temp) / "run")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
