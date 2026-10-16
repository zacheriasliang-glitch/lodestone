"""Conscience: local readiness records and an advisory command harness.

The harness evaluates a supplied command line against a local policy. It is
not a sandbox or an authorization service. Wakes preserve observed package
and workspace state; they do not start services, contact accounts or run jobs.
"""
from __future__ import annotations

from datetime import datetime, timezone
import fnmatch
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SCHEMA = "lodestone-conscience/v1"
OWNER = "conscience"
LIMIT = 2 * 1024 * 1024

PHI = (1 + math.sqrt(5)) / 2
GOLDEN_ANGLE_DEGREES = 360 * (1 - 1 / PHI)  # 137.50776405003785

VERDICTS = ("ALLOW", "DEFER", "NEEDS_HOST", "REFUSE")


# ----------------------------------------------------------------------------- mathematics (EX)

def golden_fraction(k: int) -> float:
    """k-th term of the golden low-discrepancy sequence, frac(k / phi)."""
    if k < 0:
        raise ValueError("Sequence index must be non-negative")
    return (k * (PHI - 1)) % 1.0


def golden_permutation(n: int) -> list[int]:
    """Sort slot indices by their computed golden fractions, breaking ties by index.

    This is a deterministic permutation, not a guarantee about how any prefix
    spreads indices or distributes work among differently priced checks.
    """
    if n < 0:
        raise ValueError("Slot count must be non-negative")
    return sorted(range(n), key=lambda i: (golden_fraction(i), i))


def golden_schedule(names: list[str], cursor: int, budget: int) -> tuple[list[str], int]:
    """Cyclic slice of the golden order starting at cursor; returns (chosen, next cursor)."""
    if not names or budget <= 0:
        return [], cursor
    order = golden_permutation(len(names))
    chosen = [names[order[(cursor + i) % len(names)]] for i in range(min(budget, len(names)))]
    return chosen, (cursor + len(chosen)) % len(names)


def golden_interval(base_seconds: float, k: int) -> float:
    """Vary the interval by index; callers with identical indices remain synchronized."""
    if base_seconds <= 0:
        raise ValueError("Base interval must be positive")
    return base_seconds * (1 + golden_fraction(k))


# ----------------------------------------------------------------------------- helpers

def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_bounded(path: Path, limit: int = LIMIT) -> bytes:
    with Path(path).open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError(f"Read bound exceeded: {path}")
    return raw


def pin(path: Path) -> dict:
    raw = read_bounded(path)
    return {"path": str(path), "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def canonical_bytes(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def record_store(workspace: Path):
    records = load_module("_lodestone_records", ROOT / "src/lodestone_machinery/records.py")
    return records.RecordStore(workspace)


def load_policy(path: Path | None = None) -> dict:
    path = path or HERE / "conscience-policy.json"
    policy = json.loads(read_bounded(path))
    if policy.get("schema") != "lodestone-conscience-policy/v1":
        raise ValueError("Unsupported conscience policy schema")
    policy["_pin"] = pin(path)
    return policy


# ----------------------------------------------------------------------------- enotherm (AP thresholds over EX observations)

def temperature(policy: dict, workspace: Path, *, woken: bool) -> dict:
    """COLD before a waking record exists; WARM when within budget; HOT when over it."""
    limits = policy["enotherm"]
    cores = os.cpu_count() or 1  # Logical CPUs, not physical cores.
    try:
        load_1, load_5, _ = os.getloadavg()
    except (OSError, AttributeError):
        load_1 = load_5 = None
    anchor = workspace
    while not anchor.exists() and anchor != anchor.parent:
        anchor = anchor.parent
    free = shutil.disk_usage(anchor).free
    reasons = []
    if load_1 is not None and load_1 / cores > limits["hot_load_per_core"]:
        reasons.append(f"load_1 {load_1:.2f} over {limits['hot_load_per_core']} per logical CPU on {cores} logical CPUs")
    if free < limits["hot_free_bytes"]:
        reasons.append(f"free bytes {free} under {limits['hot_free_bytes']}")
    state = "HOT" if reasons else ("WARM" if woken else "COLD")
    return {"state": state, "cores": cores, "cpu_count_kind": "logical", "load_1": load_1, "load_5": load_5,
            "volume_free_bytes": free, "volume_free_label": "whole volume, not a size of anything",
            "reasons": reasons, "observed_at": now()}


# ----------------------------------------------------------------------------- harness (EX rules)

def _owner_of(path: str, policy: dict) -> str | None:
    for writer in policy["writers"]:
        if "prefix" in writer and (path == writer["prefix"] or path.startswith(writer["prefix"].rstrip("/") + "/")):
            return writer["owner"]
        if "glob" in writer and (fnmatch.fnmatchcase(path, writer["glob"]) or fnmatch.fnmatchcase(path, writer["glob"] + "/*")):
            return writer["owner"]
    return None


def _writable(path: str, actor: str, policy: dict) -> bool:
    for pattern in policy["writable_roots_by_actor"].get(actor, []):
        if fnmatch.fnmatchcase(path, pattern) or fnmatch.fnmatchcase(path, pattern + "/*"):
            return True
    return False


def _path_like(token: str, cwd: Path) -> str | None:
    if token.startswith("-") or len(token) < 2:
        return None
    if token.startswith("/") or token.startswith("~") or "/" in token or token.endswith((".json", ".md", ".py")):
        return str(Path(os.path.expanduser(token)) if token.startswith(("/", "~")) else (cwd / token))
    return None


def tokens_of(argv: list[str]) -> list[str]:
    """Flatten a command line, including `bash -c` strings, into tokens."""
    out = []
    shells = ("sh", "bash", "zsh", "dash")
    for index, item in enumerate(argv):
        out.append(item)
        after_dash_c = index >= 2 and argv[index - 1] == "-c" and Path(argv[index - 2]).name in shells
        operators = any(ch in item for ch in "|;&><")
        # A path with spaces ("Application Support") is one token; only shell strings are split.
        if (after_dash_c or operators) and len(item) > 3:
            try:
                out.extend(shlex.split(item))
            except ValueError:
                pass
    return out


def _git_read_only(sub, after_sub, policy) -> bool:
    """Read-only Git: a listed subcommand, no write flag for that subcommand, and for `branch` no ref creation."""
    if sub not in policy["git_read_only"]:
        return False
    write_flags = policy.get("git_write_flags_by_subcommand", {}).get(sub, [])
    if any(tok in write_flags or tok.split("=")[0] in write_flags for tok in after_sub):
        return False
    if sub == "branch":
        listing = any(tok in policy.get("git_branch_list_flags", []) or tok.split("=")[0] in policy.get("git_branch_list_flags", []) for tok in after_sub)
        positional = [tok for tok in after_sub if not tok.startswith("-")]
        if positional and not listing:
            return False  # `git branch <name>` creates a ref
    return True


def _git_invocation(argv, cwd, inspector):
    """Identify Git only in executable position, through bounded simple shells.

    Parse supported global flags and sequential -C operands relative to the
    supplied working directory. Unrecognized global forms remain uncertain.
    """
    command = list(argv)
    for _ in range(5):
        if not command:
            return None
        name = Path(command[0]).name
        if name == "git":
            break
        if name not in inspector.SHELLS or len(command) != 3 or command[1] != "-c":
            return None
        try:
            command, _ = inspector._simple_shell(command[2])
        except ValueError:
            return None  # mutation inspection already reports uncertainty
    else:
        return None
    directory = cwd.resolve()
    index = 1
    while index < len(command):
        option = command[index]
        if option == "-C":
            if index + 1 == len(command):
                return {"directory": str(directory), "sub": None, "args": [], "uncertain": "missing Git -C directory"}
            target = Path(command[index + 1])
            directory = (target if target.is_absolute() else directory / target).resolve()
            index += 2
        elif option in {"--no-pager", "--paginate", "--no-optional-locks"}:
            index += 1
        elif option.startswith("-"):
            return {"directory": str(directory), "sub": None, "args": [], "uncertain": "unsupported Git global option: " + option}
        else:
            return {"directory": str(directory), "sub": option, "args": command[index + 1:], "uncertain": None}
    return {"directory": str(directory), "sub": None, "args": [], "uncertain": "Git subcommand absent"}


def assess(argv: list[str], *, cwd: Path, actor: str, policy: dict, woken: bool, temp: dict,
           workspace: Path | None = None) -> dict:
    """Judge one command line against the custody rules. Returns a verdict packet."""
    if not argv:
        raise ValueError("Nothing to assess")
    toks = tokens_of(argv)
    joined = " ".join(toks)
    rules: list[dict] = []
    advisories: list[str] = []
    paths = [p for p in (_path_like(t, cwd) for t in toks) if p]

    for token in toks:
        for pattern in policy["sealed_patterns"]:
            if pattern in token:
                rules.append({"rule": "sealed-store", "verdict": "REFUSE", "token": token, "pattern": pattern})

    inspector = load_module("_lodestone_conscience_targets", HERE / "conscience_targets.py")
    mutations = inspector.inspect_mutations(argv, cwd)
    git = _git_invocation(argv, cwd, inspector)
    git_dirs = [git["directory"]] if git else []
    read_only = True
    if git:
        if git["uncertain"]:
            rules.append({"rule": "unparsed-write", "verdict": "NEEDS_HOST", "reason": git["uncertain"]})
        read_only = not git["uncertain"] and _git_read_only(git["sub"], git["args"], policy)
        for directory in git_dirs:
            owner = _owner_of(directory, policy)
            if owner not in (None, "any", "shared-primary", actor) and not read_only:
                rules.append({"rule": "one-writer-per-path", "verdict": "REFUSE", "directory": directory, "owner": owner, "subcommand": git["sub"]})
            if owner == "shared-primary" and not read_only:
                rules.append({"rule": "shared-primary-read-only", "verdict": "NEEDS_HOST", "directory": directory, "subcommand": git["sub"]})

    writes = mutations["writes"] or bool(git and not read_only)
    targets = sorted(set(mutations["targets"] + mutations["destructive_sources"]))
    if git and not read_only:
        targets = sorted(set(targets + paths + git_dirs))
    for reason in mutations["uncertain"]:
        rules.append({"rule": "unparsed-write", "verdict": "NEEDS_HOST", "reason": reason})
    cwd_owner = _owner_of(str(cwd), policy)
    cwd_is_mine = cwd_owner in (None, "any", actor)
    if writes and not cwd_is_mine and cwd_owner != "shared-primary":
        rules.append({"rule": "one-writer-per-path", "verdict": "REFUSE", "directory": str(cwd), "owner": cwd_owner,
                      "reading": "a write while standing in another actor's directory"})
    record_roots = [Path(r) if Path(r).is_absolute() else ROOT / r for r in policy["record_roots"]]
    record_roots.append((workspace or Path.home() / ".lodestone/workspace") / "organs")
    record_roots = [p.resolve() for p in record_roots]
    for path in targets:
        if writes:
            candidate = Path(path)
            for record_root in record_roots:
                # Existing protected entries and directories/ancestors containing them.
                inside = candidate == record_root or record_root in candidate.parents
                ancestor = candidate in record_root.parents
                if ((inside and (candidate.exists() or candidate.is_symlink()))
                        or (ancestor and record_root.exists())):
                    rules.append({"rule": "records-append-only", "verdict": "REFUSE", "path": path})
            owner = _owner_of(path, policy)
            inside_own_cwd = cwd_is_mine and (candidate == cwd.resolve() or cwd.resolve() in candidate.parents)
            inside_workspace = workspace is not None and (candidate == workspace.resolve() or workspace.resolve() in candidate.parents)
            if owner == "shared-primary" and not inside_own_cwd:
                rules.append({"rule": "shared-primary-read-only", "verdict": "NEEDS_HOST", "path": path})
            elif owner not in (None, "any", actor):
                rules.append({"rule": "one-writer-per-path", "verdict": "REFUSE", "path": path, "owner": owner})
            elif owner is None and not _writable(path, actor, policy) and not inside_own_cwd and not inside_workspace:
                rules.append({"rule": "massless-write", "verdict": "NEEDS_HOST", "path": path,
                              "reading": "write outside every pinned writable root: no rule and no pin carry it"})

    for pattern in policy["launch_patterns"]:
        if pattern in joined:
            if "--preregistration" in toks:
                rules.append({"rule": "launch-preregistered", "verdict": "ALLOW", "pattern": pattern})
            else:
                rules.append({"rule": "launch-needs-preregistration", "verdict": "NEEDS_HOST", "pattern": pattern})
            break

    for heavy in policy["heavy_patterns"]:
        if all(any(part in t for t in toks) for part in heavy["all"]):
            if temp["state"] == "HOT" or not woken:
                rules.append({"rule": "heavy-while-hot-or-cold", "verdict": "DEFER", "pattern": heavy["all"], "temperature": temp["state"]})
            else:
                advisories.append("heavy command: go quiet first on this host")
            break

    interpreter = policy["interpreter"]
    if any(Path(t).name in {"python", "python3", "python.exe"} or Path(t).name.startswith("python3.") for t in toks):
        if not any(flag in toks for flag in interpreter["isolation_flags"]):
            advisories.append("python without isolation flag; prefer -I when reading untrusted data")

    unique, seen = [], set()
    for rule in rules:
        key = (rule["rule"], rule["verdict"], rule.get("directory") or rule.get("path") or rule.get("token") or str(rule.get("pattern")) + str(rule.get("reason", "")))
        if key not in seen:
            seen.add(key)
            unique.append(rule)
    rules = unique
    order = {v: i for i, v in enumerate(VERDICTS)}
    verdict = max((r["verdict"] for r in rules), key=lambda v: order[v], default="ALLOW")
    return {"verdict": verdict, "rules": rules, "advisories": advisories, "actor": actor,
            "cwd": str(cwd), "argv": list(argv), "paths_seen": sorted(set(paths + targets)),
            "mutation_targets": targets, "unparsed_mutations": mutations["uncertain"], "temperature": temp["state"],
            "scope": "Intent judged from the command line; not a sandbox; no permission granted"}


# ----------------------------------------------------------------------------- four-part accounting (AP reading of EX bytes)

def four_part(literal: dict, model: dict, table: dict, packet: dict) -> dict:
    """Literal, model, table, packet: sizes in canonical bytes, and the coupling hash.

    The returned coupling hash covers literal, model and table; it is separate
    from the supplied packet, which this function does not modify. Rest sums
    model and table bytes; kinetic sums literal and packet bytes. These labels
    do not establish constancy between commands or any physical property.
    """
    sizes = {name: len(canonical_bytes(value)) for name, value in
             (("literal", literal), ("model", model), ("table", table), ("packet", packet))}
    coupling = hashlib.sha256(canonical_bytes([literal, model, table])).hexdigest()
    empty = lambda v: v is None or v == {} or v == [] or (isinstance(v, dict) and all(empty(x) for x in v.values()))
    return {"bytes": sizes, "rest": sizes["model"] + sizes["table"], "kinetic": sizes["literal"] + sizes["packet"],
            "coupling_sha256": coupling, "massless": empty(model) and empty(table)}


# ----------------------------------------------------------------------------- waking

def newest_restart_readme(runtime_dir: Path) -> dict:
    candidates = sorted(runtime_dir.glob("RESTART-README-*.md")) if runtime_dir.is_dir() else []
    if not candidates:
        return {"status": "ABSENT", "directory": str(runtime_dir)}
    path = candidates[-1]
    result = {"status": "PRESENT", **pin(path), "verified": None}
    sidecar = path.with_name(path.name + ".sha256")
    if sidecar.is_file():
        fields = read_bounded(sidecar).decode("utf-8", "replace").split()
        if not fields:
            raise ValueError("Restart checksum sidecar is empty")
        expected = fields[0]
        result["verified"] = expected == result["sha256"]
        result["sidecar"] = str(sidecar)
    return result


def default_runtime_dir(workspace: Path | None = None) -> Path:
    """Optional local restart notes belong to the selected workspace."""
    return (workspace or Path.home() / ".lodestone/workspace") / "continuity"


def _check(name: str, root: Path, workspace: Path, runtime_dir: Path, policy: dict) -> dict:
    cli = load_module("_lodestone_cli", HERE / "cli.py")
    if name == "source_files_present":
        result = cli.core_capabilities(root)
        missing = [name for name, present in result["required_files"].items() if not present]
        return {"ok": not missing, "detail": "required source files present" if not missing else ", ".join(missing)}
    if name == "runtime_workspace_writable":
        result = cli.workspace_state(workspace)
        return {"ok": result["status"] == "READY", "detail": result["path_check"]}
    if name == "interpreter_supported":
        return {"ok": sys.version_info >= (3, 10), "detail": platform.python_version()}
    if name == "restart_readme_present":
        result = newest_restart_readme(runtime_dir)
        return {"ok": result["status"] == "PRESENT" and result.get("verified") is not False,
                "detail": result.get("sha256") or result["status"]}
    return {"ok": False, "detail": "unknown portable check: " + name}


def previous_wakings(store) -> list[str]:
    """Return a single complete waking chain, oldest first; never guess its tip.

    Damaged catalog rows omit kind, so any damaged Conscience row prevents a
    trustworthy history selection. This also blocks assess/run until inspected.
    Historical bytes, timestamps and stored digests are never rewritten.
    """
    entries = [e for e in store.catalog()["entries"] if e.get("owner") == OWNER]
    if any(e.get("status") != "INTACT" for e in entries):
        raise ValueError("Damaged or unavailable conscience record; inspect the catalog")
    wakings = {e["handle"]: e for e in entries if e.get("kind") == "waking"}
    if not wakings:
        return []
    parents, referenced = {}, set()
    for handle, entry in wakings.items():
        links = entry.get("links", [])
        if len(links) > 1 or any(link not in wakings for link in links):
            raise ValueError("Ambiguous waking history: each wake must link only to one waking predecessor")
        parents[handle] = links[0] if links else None
        referenced.update(links)
    tips = set(wakings) - referenced
    if len(tips) != 1:
        raise ValueError("Ambiguous waking history: expected exactly one chain tip")
    reverse, seen, cursor = [], set(), next(iter(tips))
    while cursor is not None:
        if cursor in seen:
            raise ValueError("Ambiguous waking history: cycle")
        seen.add(cursor)
        reverse.append(cursor)
        cursor = parents[cursor]
    if len(seen) != len(wakings):
        raise ValueError("Ambiguous waking history: disconnected records or cycle")
    return list(reversed(reverse))


def wake(*, root: Path, workspace: Path, runtime_dir: Path, policy: dict,
         literal: str | None = None, actor: str | None = None) -> dict:
    """Observe local readiness and append exactly one waking record."""
    started = time.monotonic()
    cli = load_module("_lodestone_cli", HERE / "cli.py")
    store = record_store(workspace)
    chain = previous_wakings(store)
    history_digest = hashlib.sha256(canonical_bytes(sorted(chain))).hexdigest()
    readme = newest_restart_readme(runtime_dir)
    documents = []
    for name in policy["context_documents"]:
        path = root / name
        documents.append(pin(path) if path.is_file() else {"path": str(path), "status": "ABSENT"})
    readiness = cli.doctor(root, workspace)
    temp = temperature(policy, workspace, woken=True)
    names = list(policy["checks"])
    cursor = len(chain) * policy["schedule"]["budget_per_wake"] % max(len(names), 1)
    chosen, next_cursor = golden_schedule(names, cursor, policy["schedule"]["budget_per_wake"])
    checks = {name: _check(name, root, workspace, runtime_dir, policy) for name in chosen}
    if readme.get("verified") is False:
        checks["restart_readme_checksum"] = {"ok": False, "detail": "Restart note differs from its checksum"}
    shape = {"doctor_status": readiness["status"],
             "restart_readme.sha256": readme.get("sha256"),
             "context_pins": [d.get("sha256") for d in documents], "temperature": temp["state"]}
    payload = {
        "schema": SCHEMA, "kind": "waking", "woke_at": now(),
        "clock_source": "datetime.now(UTC) on this device",
        "actor": actor or policy["actor_default"], "root": str(root), "workspace": str(workspace),
        "restart_readme": readme, "documents": documents, "readiness": readiness,
        "temperature": temp,
        "checks": {"chosen": chosen, "cursor_before": cursor, "cursor_after": next_cursor,
                   "order": [names[i] for i in golden_permutation(len(names))], "results": checks},
        "history": {"previous_wakings": len(chain), "last": chain[-1] if chain else None,
                    "digest": history_digest, "ordering": "unique-predecessor-chain/v1",
                    "digest_encoding": "sha256-sorted-handles/v1"},
        "shape": shape, "host_literal": literal, "policy": policy["_pin"],
        "route": ["clock", "optional workspace restart note", "local document pins",
                  "portable readiness", "temperature", "scheduled checks", "waking record"],
        "wall_seconds": round(time.monotonic() - started, 3),
        "scope": "Local observations only; no network requests, account probes or background services",
    }
    handle = store.publish(OWNER, "waking", payload, links=chain[-1:] if chain else ())
    accounting = four_part({"literal": literal, "argv": ["wake"]}, {"policy": policy["_pin"]},
                          {"documents": documents, "restart_readme": readme}, {"handle": handle})
    return {"schema": SCHEMA, "status": "WOKEN", "handle": handle, "shape": shape,
            "temperature": temp["state"], "checks": {name: result["ok"] for name, result in checks.items()},
            "failed_checks": [name for name, result in checks.items() if not result["ok"]],
            "history": payload["history"], "restart_readme": readme.get("status"),
            "restart_readme_verified": readme.get("verified"), "four_part": accounting,
            "next": "python3 lodestone.py --workspace PATH conscience assess -- COMMAND",
            "authority": "none conferred; readiness describes observed local capabilities"}


# ----------------------------------------------------------------------------- compare (phase-two probe on real state)

def compare(store, handle_a: str, handle_b: str, policy: dict) -> dict:
    """Same visible shape, different history: does the compaction keep the distinction?"""
    a = store.relay(OWNER, handle_a, owner=OWNER, kind="waking")["payload"]
    b = store.relay(OWNER, handle_b, owner=OWNER, kind="waking")["payload"]
    fields = policy["phase_two"]["action_relevant_fields"]
    shape_equal = all(a["shape"].get(f) == b["shape"].get(f) for f in fields)
    history_equal = a["history"]["digest"] == b["history"]["digest"]
    differing = [f for f in fields if a["shape"].get(f) != b["shape"].get(f)]
    return {"schema": SCHEMA, "kind": "compare", "a": handle_a, "b": handle_b,
            "shape_equal": shape_equal, "history_equal": history_equal, "shape_fields_differing": differing,
            "distinguishable": handle_a != handle_b,
            "reading": ("same shape, different history: the record keeps the distinction through its history digest"
                        if shape_equal and not history_equal else
                        "different shape" if not shape_equal else "same shape and same history"),
            "phase_two_criterion": "E(F_a(s)) ~ F_a(E(s)) is not tested here; this probe only checks that E keeps history distinctions"}


# ----------------------------------------------------------------------------- run

def run(argv: list[str], *, cwd: Path, actor: str, policy: dict, workspace: Path, literal: str | None, execute: bool) -> dict:
    store = record_store(workspace)
    chain = previous_wakings(store)
    woken = bool(chain)
    temp = temperature(policy, workspace, woken=woken)
    verdict = assess(argv, cwd=cwd, actor=actor, policy=policy, woken=woken, temp=temp, workspace=workspace)
    outcome = None
    if execute and verdict["verdict"] == "ALLOW":
        try:
            proc = subprocess.run(argv, cwd=str(cwd), check=False)
            outcome = {"returncode": proc.returncode, "finished_at": now()}
        except OSError as exc:
            outcome = {"error": str(exc)[:500]}
    payload = {"schema": SCHEMA, "kind": "assessment", "at": now(), "verdict": verdict, "executed": execute and verdict["verdict"] == "ALLOW",
               "outcome": outcome, "host_literal": literal, "policy": policy["_pin"], "waking": chain[-1] if chain else None}
    handle = store.publish(OWNER, "assessment", payload, links=chain[-1:] if chain else ())
    accounting = four_part({"literal": literal, "argv": argv, "cwd": str(cwd)}, {"rules": [r["rule"] for r in verdict["rules"]]},
                           {"paths": verdict["paths_seen"]}, {"handle": handle, "policy": policy["_pin"]["sha256"]})
    return {"schema": SCHEMA, "verdict": verdict["verdict"], "rules": verdict["rules"], "advisories": verdict["advisories"],
            "executed": payload["executed"], "outcome": outcome, "handle": handle, "woken": woken,
            "temperature": temp["state"], "four_part": accounting,
            "surface": None if verdict["verdict"] == "ALLOW" else "surface to host; never route around a refusal"}


# ----------------------------------------------------------------------------- CLI

def main(argv: list[str] | None = None, *, root: Path | None = None, workspace: Path | None = None) -> int:
    import argparse
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--policy", type=Path, default=argparse.SUPPRESS)
    common.add_argument("--actor", default=argparse.SUPPRESS)
    common.add_argument("--literal", default=argparse.SUPPRESS, help="The host's words that led to this command, verbatim")
    common.add_argument("--runtime-dir", type=Path, default=argparse.SUPPRESS, help="Optional restart-note directory; default WORKSPACE/continuity")
    parser = argparse.ArgumentParser(prog="lodestone conscience", description=__doc__.splitlines()[0], parents=[common])
    sub = parser.add_subparsers(dest="op", required=True)
    sub.add_parser("wake", parents=[common])
    s = sub.add_parser("assess", parents=[common]); s.add_argument("command", nargs=argparse.REMAINDER)
    s = sub.add_parser("run", parents=[common]); s.add_argument("command", nargs=argparse.REMAINDER)
    s = sub.add_parser("compare", parents=[common]); s.add_argument("a"); s.add_argument("b")
    sub.add_parser("catalog", parents=[common])
    sub.add_parser("temperature", parents=[common])
    s = sub.add_parser("golden", parents=[common]); s.add_argument("n", type=int)
    options = parser.parse_args(argv)
    for field in ("policy", "actor", "literal", "runtime_dir"):
        if not hasattr(options, field):
            setattr(options, field, None)
    root = (root or ROOT).resolve()
    workspace = (workspace or Path.home() / ".lodestone/workspace").resolve()
    try:
        policy = load_policy(options.policy)
        actor = options.actor or policy["actor_default"]
        if options.op == "wake":
            result = wake(root=root, workspace=workspace, runtime_dir=options.runtime_dir or default_runtime_dir(workspace), policy=policy,
                          literal=options.literal, actor=actor)
        elif options.op in ("assess", "run"):
            command = options.command[1:] if options.command and options.command[0] == "--" else list(options.command)
            if not command:
                raise ValueError("Give the command after --")
            result = run(command, cwd=Path.cwd(), actor=actor, policy=policy, workspace=workspace,
                         literal=options.literal, execute=options.op == "run")
        elif options.op == "compare":
            result = compare(record_store(workspace), options.a, options.b, policy)
        elif options.op == "catalog":
            entries = [e for e in record_store(workspace).catalog()["entries"] if e.get("owner") == OWNER]
            result = {"schema": SCHEMA, "kind": "catalog", "entries": entries, "count": len(entries)}
        elif options.op == "temperature":
            result = temperature(policy, workspace, woken=bool(previous_wakings(record_store(workspace))))
        else:
            n = options.n
            result = {"schema": SCHEMA, "kind": "golden", "phi": PHI, "golden_angle_degrees": GOLDEN_ANGLE_DEGREES,
                      "n": n, "order": golden_permutation(n), "fractions": [golden_fraction(i) for i in range(n)]}
        if options.op == "run" and result.get("executed"):
            # The command owned stdout; the harness summary goes to stderr and the exit code is the command's.
            print(json.dumps(result, indent=2, ensure_ascii=True, allow_nan=False), file=sys.stderr)
            return int((result.get("outcome") or {}).get("returncode", 2))
        # Only presentation uses escapes; canonical_bytes and stored records stay UTF-8.
        print(json.dumps(result, indent=2, ensure_ascii=True, allow_nan=False))
        bad = (result.get("verdict") in ("REFUSE", "NEEDS_HOST", "DEFER") or result.get("failed_checks")
               or (options.op == "wake" and result["shape"]["doctor_status"] != "READY"))
        return 2 if bad else 0
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)[:1000]}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
