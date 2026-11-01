"""Read-only Conscience mapping for the reviewed reconciliation worker.

Source candidate only: not installed or exercised. This is an additional
custody assessment, never an approval, executor, sandbox or publication API.
The controller remains responsible for exact approval, code/policy identity,
STOP, locks, limits and independent stage acceptance.
"""
from pathlib import Path
import re


ORDER = {name: rank for rank, name in enumerate(("ALLOW", "DEFER", "NEEDS_HOST", "REFUSE"))}


def _plain(path):
    path = Path(path)
    if not path.is_absolute() or path != path.resolve():
        raise ValueError("Absolute canonical paths without aliases are required")
    return path


def _overlap(left, right):
    return left == right or left in right.parents or right in left.parents


def worker_intent(reconciliation, *, home, stage, parent, raw, task,
                  argv, interpreter, parent_pid):
    """Derive effects from one fixed operation, never from supplied advice.

    Called by the controller after creating its private stage, before Popen.
    `parent` is the controller's read-only active-generation Board. It is not
    the worker's Board. Callers cannot supply/override an effects list.
    """
    home, stage = _plain(home), _plain(stage)
    if stage.parent != home / "generations" or not re.fullmatch(r"[0-9a-f]{32}", stage.name):
        raise ValueError("Expected the controller's private generation directory")
    parent_home = _plain(parent.home)
    if (parent_home.name != "pando" or parent_home.parent.parent != home / "generations"
            or not re.fullmatch(r"[0-9a-f]{32}", parent_home.parent.name)
            or stage == parent_home.parent):
        raise ValueError("Stage must differ from the active parent generation")
    if type(task) is not dict or not re.fullmatch(r"[0-9a-f]{32}", task.get("id", "")):
        raise ValueError("Expected a controller task identity")
    plan = task["plan"]
    if plan.get("toolkit") != reconciliation.TOOLKIT:
        raise ValueError("Only the existing reconciliation toolkit is mapped")
    reconciliation.validate_reconciliation_plan(plan, raw, parent.settings())
    reconciliation.validate_route_selection(plan, parent, raw)
    if type(parent_pid) is not int or parent_pid <= 0:
        raise ValueError("Expected the supervising controller PID")
    # Match the controller's literal executable spelling, including an existing
    # interpreter alias. The outer reviewed identity check owns its resolution;
    # changing argv to a resolved path would describe a different invocation.
    if type(interpreter) is not str or not Path(interpreter).is_absolute():
        raise ValueError("Expected the controller's absolute interpreter spelling")
    executable = interpreter
    source = home / "inputs" / (plan["source_sha256"] + ".json")
    lock = home / "worker.lock"
    expected = [str(executable), "-B", "-m", "lodestone_nurarihyon.worker",
                str(stage), str(source), str(stage / "request.json"),
                "--parent-pid", str(parent_pid), "--worker-lock", str(lock)]
    if type(argv) is not list or argv != expected:
        raise ValueError("Actual worker command differs from the mapped operation")
    additional_reads = []
    if plan["toolkit"] in ("bezoar.reconciliation-capture/v1", "bezoar.reconciliation-observation/v1"):
        from lodestone_machinery.bezoar_capture import sibling
        additional_reads = [str(sibling(source))]
    return {
        "schema": "nurarihyon-conscience-worker-intent/v1",
        "task_id": task["id"], "toolkit": plan["toolkit"],
        "source_sha256": plan["source_sha256"],
        "plan_sha256": reconciliation.digest(reconciliation.canonical(plan)),
        "base_generation": parent_home.parent.name,
        "argv": list(argv), "cwd": str(stage),
        "reads": [str(source), str(stage / "request.json"), str(parent_home)] + additional_reads,
        "writes": [
            {"path": str(stage), "scope": "subtree", "operation": "private-staged-transformation"},
            {"path": str(lock), "scope": "entry", "operation": "append-open-and-flock"},
        ],
        "write_detail": ["staged Pando SQLite and sidecars", "staged captures and kernel records",
                         "reconciliation-work", "optional routing-work", "worker-result.json",
                         "inherited worker.log descriptor"],
        "publication": "NONE", "repair": "NONE", "learning": False,
        "scope": "Worker only; controller queue updates and pointer publication remain outside this assessment",
    }


def assess_worker(conscience, reconciliation, *, policy, actor, woken, temp,
                  workspace, home, stage, parent, raw, task, argv, interpreter, parent_pid):
    """Join the real command assessment with explicit write-custody checks.

    Reuses Conscience's actual policy validation and ownership predicates.
    Added checks can only make its verdict more restrictive. In particular,
    ALLOW from Python argv alone cannot erase a typed-effects refusal.
    No policy is loaded implicitly; no command or Conscience.run is invoked.
    """
    conscience._validate_policy(policy)
    intent = worker_intent(reconciliation, home=home, stage=stage, parent=parent,
                           raw=raw, task=task, argv=argv, interpreter=interpreter,
                           parent_pid=parent_pid)
    command = conscience.assess(argv, cwd=Path(intent["cwd"]), actor=actor,
                               policy=policy, woken=woken, temp=temp,
                               workspace=_plain(workspace))
    if command["verdict"] not in ORDER:
        raise ValueError("Unknown Conscience verdict")
    rules = []

    def add(rule, verdict, **detail):
        rules.append({"rule": rule, "verdict": verdict, **detail})

    roots = [Path(p) if Path(p).is_absolute() else conscience.ROOT / p
             for p in policy["record_roots"]]
    roots.append(_plain(workspace) / "organs")
    roots = [p.resolve() for p in roots]
    for effect in intent["writes"]:
        target = _plain(effect["path"])
        subtree = effect["scope"] == "subtree"
        for pattern in policy["sealed_patterns"]:
            if pattern in str(target):
                add("sealed-store", "REFUSE", path=str(target), pattern=pattern)
        for root in roots:
            inside = target == root or root in target.parents
            ancestor = target in root.parents
            if ((inside and (target.exists() or target.is_symlink()))
                    or (ancestor and root.exists())):
                add("records-append-only", "REFUSE", path=str(target))
        owner = conscience._owner_of(str(target), policy)
        if owner == "shared-primary":
            add("shared-primary-read-only", "NEEDS_HOST", path=str(target))
        elif owner not in (None, "any", actor):
            add("one-writer-per-path", "REFUSE", path=str(target), owner=owner)
        elif owner is None and not conscience._writable(str(target), actor, policy):
            # Unlike the command parser's implicit cwd allowance, the adapter
            # requires this externally selected stage/lock to have explicit
            # custody. It does not manufacture a writable root from the task.
            add("massless-write", "NEEDS_HOST", path=str(target))
        if subtree:
            for writer in policy["writers"]:
                if writer["owner"] in ("any", actor):
                    continue
                boundaries = []
                if "prefix" in writer:
                    boundaries.append(Path(writer["prefix"]))
                if "glob" in writer:
                    # A glob's fixed path prefix is a conservative envelope.
                    # Uncertain overlap refuses admission rather than guessing.
                    fixed = re.split(r"[?*\[]", writer["glob"], maxsplit=1)[0]
                    boundaries.append(Path(fixed).parent if not fixed.endswith("/") else Path(fixed))
                if any(not boundary.is_absolute() or _overlap(target, boundary) for boundary in boundaries):
                    add("nested-custody-unresolved", "NEEDS_HOST", path=str(target), writer=writer)
    verdict = max([command["verdict"]] + [r["verdict"] for r in rules], key=ORDER.__getitem__)
    return {"schema": "nurarihyon-conscience-worker-assessment/v1", "verdict": verdict,
            "command_assessment": command, "effects_assessment": rules, "intent": intent,
            "scope": "Additional read-only assessment; not permission or execution",
            "authority": "NONE", "executed": False, "published": False}
