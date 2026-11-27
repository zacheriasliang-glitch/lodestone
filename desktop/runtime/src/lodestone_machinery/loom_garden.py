"""The Loom's garden: the Loom writes the next revision of one of its own candidate functions.

Fence. The Loom revises candidates retained by ``loom_improvement`` and nothing else. The fixed
checker, Conscience, Nurarihyon, Bezoar, the installers and this module stay outside every
candidate, so a revision cannot change how it is judged. On macOS each trial runs under
Origami's Seatbelt profile: no network, no fork, writes only inside the candidate's own
directory, reads limited to the system, the interpreter, the candidate and the checker.

One generation:

1. The current candidate is materialized into a new directory and tried once with the fixed
   checker through the improvement lifecycle (Conscience admission, Nurarihyon dispatch,
   Bezoar outcome).
2. The checker's actual result returns as an attributed, unresolved review. The checker is the
   reviewer; neither the Loom nor this module resolves its own work.
3. If every check passed, the candidate becomes the garden's CURRENT version for that skill
   id: a pointer file, never an installation into a runtime.
4. Otherwise the reader receives the candidate's purpose, conditions, optional source and failing checks
   through ``UnifiedLoom.run``, so its authoring is itself retained practice. The first fenced
   code block in its answer becomes a revision proposed with the parent's handle and the
   reader's name as author. No code block, or unchanged source, ends the cycle.

The cycle stops when the checker passes, the reader proposes nothing new, or the generation
bound is reached. Failures are kept: they are what the next generation reads.
"""
from datetime import datetime, timezone
import ast
import json
import os
from pathlib import Path
import re
import uuid

from .learning_cli import command_provider
from .loom_improvement import ImprovementCycle
from .loom_unified import UnifiedLoom
from .origami import _fenced_argv
from .records import canonical

SCHEMA = "loom-garden-generation/v1"
MAX_GENERATIONS = 8
CODE_BLOCK = re.compile(r"```[ \t]*(?:python3?|py)?[ \t]*\r?\n(.*?)```", re.S | re.I)
AUTHOR_PURPOSE = ("Write the next version of this function so the failing checks pass and the passing ones "
                  "keep passing. Return the complete file in one ```python block, then one line saying what "
                  "you changed. Keep its purpose and conditions.")


def first_code(response):
    """The first fenced code block in a reader's answer, or None."""
    match = CODE_BLOCK.search(response or "")
    return match.group(1) if match and match.group(1).strip() else None


def _failing_case(row):
    """One failing check as the reader will see it, keeping a difference JSON cannot carry.

    On 19 Nov the checker's tuples arrived as JSON lists, so the Loom read "expected ['name', 'value']
    got ['name', 'value']": told it failed, and shown it was right. A checker's own Python forms win;
    without them, values that print alike leave the reason for the mismatch unknown.
    """
    expected = row.get("expected_python", repr(row.get("expected")))
    actual = row.get("actual_python", repr(row.get("actual")))
    case = f"input {row.get('input')!r} expected {expected} got {actual}"
    if expected == actual:
        case += " (these print alike but did not match: the difference is not represented in this output)"
    return case


def _signatures(artifacts):
    """Render top-level function headers without copying an inline body or decorators."""
    signatures = []
    for artifact in artifacts:
        try:
            tree = ast.parse(artifact["content"])
        except SyntaxError:
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                node.decorator_list = []
                node.body = [ast.Pass()]
                signatures.append(ast.unparse(node).splitlines()[0])
    return signatures


def sandboxed(argv, candidate_dir, checker_root):
    """Wrap a trial command in this platform's kernel fence; refuse where there is none.

    Returns (argv, fence kind). The macOS form is the earlier Seatbelt invocation.
    """
    from .isolation import require
    fence = require("A garden trial checker")
    return _fenced_argv(fence, list(argv), Path(candidate_dir), Path(checker_root), None, None), fence.kind


def loom_reader(runtime_json, *, max_tokens=512):
    """The installed Loom reader as an author provider, prepared exactly as ``LoomBridge.run`` prepares it.

    Each call gets its own run directory with ``config.json`` and ``reader-identity.json`` saved before
    the supervised worker starts. Returns ``(reader_name, provider)``.
    """
    from . import loom_runtime
    runtime_json = Path(runtime_json)
    bridge = loom_runtime.LoomBridge(runtime_json)
    base = next(iter(loom_runtime._profiles(runtime_json).values()))
    config = dict(base, generation={"max_tokens": min(base["max_new_tokens"], max_tokens), "temperature": 0, "top_p": 1},
                  requested_generation={"max_tokens": None, "temperature": 0, "top_p": 1})
    if config["backend"] == "ollama_qwen3_base":
        reader = "Loom/Qwen3-0.6B/ollama-quantized-base/" + config["ollama_model_digest"]
    else:
        reader = "Loom/LocalAdapter/Qwen3-0.6B/" + ("adapter-sha256:" + config["adapter_state_sha256"]
                                                   if config.get("adapter_state") else "base-adapter-disabled")

    def provider(packet):
        directory = Path(config["runs_dir"]) / uuid.uuid4().hex
        directory.mkdir(mode=0o700, parents=True)
        loom_runtime._save(directory / "config.json", config)
        loom_runtime._save(directory / "reader-identity.json", loom_runtime.runtime_identity(config))
        return bridge._worker(config, directory, packet, lambda: False)
    return reader, provider


def _now():
    return datetime.now(timezone.utc).isoformat()


class Garden:
    """Let the Loom revise one candidate function against a fixed checker, a bounded number of times.

    ``checker`` is the trusted argv prefix; the materialized candidate directory is appended as its
    last argument. ``author_provider`` is the Loom reader's provider for ``UnifiedLoom.run``.
    """

    def __init__(self, memory, seed_handles, root, checker, *, reader, author_provider,
                 checker_name, trial_timeout=30, turn_wait=1800, show_source=True):
        if type(show_source) is not bool:
            raise ValueError("Source visibility requires an explicit boolean")
        self.cycle = ImprovementCycle(memory)
        self.loom = UnifiedLoom(self.cycle.memory, seed_handles)
        self.seed_handles = list(seed_handles)
        self.root = Path(root).resolve()
        if type(checker) is not list or not checker or any(type(x) is not str or not x for x in checker):
            raise ValueError("The fixed checker must be a nonempty argv list")
        self.checker = list(checker)
        self.checker_root = Path(checker[-1]).resolve().parent
        if self.checker_root == self.root or self.root in self.checker_root.parents:
            raise ValueError("The fixed checker must live outside the garden's candidate directories")
        self.reader, self.author_provider = reader, author_provider
        self.checker_name = checker_name
        self.trial_timeout, self.turn_wait = trial_timeout, turn_wait
        # This controls only the current file in the authoring literal. Native retained
        # history is unchanged and can still contain source from earlier encounters.
        self.show_source = show_source

    def _log(self, row):
        with (self.root / "GARDEN-LOG.jsonl").open("a") as stream:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    def trial(self, candidate, generation):
        """Materialize, try once under the fence, and return the checker's parsed result."""
        directory = self.root / f"g{generation:02d}-{candidate.split(':')[-1][:16]}-{uuid.uuid4().hex[:8]}"
        self.cycle.materialize(candidate, str(directory))
        argv, fenced = sandboxed(self.checker + [str(directory)], directory, self.checker_root)
        command = command_provider(argv, self.trial_timeout, cwd=directory)
        spec = self.cycle.read_candidate(candidate)
        prepared = self.cycle.prepare(
            candidate, seed_handles=self.seed_handles, reader=self.checker_name,
            encounter=f"Try generation {generation} of {spec['id']} against the fixed checker.",
            purpose="Observe the fixed checker's actual result for this exact candidate.")
        attempt = prepared["attempt_handle"]

        def provider(packet):
            before = self.cycle.verify_materialized(candidate, str(directory))
            returned = command(packet)
            observed = returned["observation"]
            observed.update(candidate_handle=candidate, artifact_verification=before,
                            fence=fenced)
            try:
                self.cycle.verify_materialized(candidate, str(directory))
            except (OSError, ValueError) as exc:
                returned["status"] = "FAILED"
                observed.update(artifact_drift=True, artifact_error_type=type(exc).__name__)
            return returned

        # The checker is a trusted tool that ignores the practice text, so its packet uses Codex's lossless
        # packed transport; a growing lineage then still fits the adapter envelope.
        report = self.cycle.run(attempt, provider=provider, pack_tool_context=True)
        response = report["outcome"].get("response") or ""
        try:
            result = json.loads(response)
        except ValueError:
            result = {"status": "FAILED", "cases": [], "unparsed_output": response[:2000]}
        passed = report["outcome"]["status"] == "COMPLETED" and result.get("status") == "COMPLETED"
        failing = [row for row in result.get("cases", []) if not row.get("matched")]
        if passed:
            correction = "Every fixed check passed for this exact candidate."
        elif failing:
            correction = "Failing checks: " + "; ".join(_failing_case(row) for row in failing[:8])
        else:
            correction = "The candidate did not complete the fixed checks: " + json.dumps(
                {k: result.get(k) for k in ("error_type", "unparsed_output") if result.get(k)})[:2000]
        review = self.cycle.review(attempt, correction=correction, author=self.checker_name,
                                   observation=f"Actual fixed-checker result, generation {generation}.",
                                   event_id=f"garden-check-{attempt}")
        return {"passed": passed, "failing": failing, "result": result, "correction": correction,
                "attempt_handle": attempt, "outcome_handle": report["outcome_handle"],
                "review_handle": review["review_handle"], "directory": str(directory), "fenced": fenced}

    def author(self, candidate, checked):
        """Ask the Loom for the next revision through its own practice lifecycle."""
        spec = self.cycle.read_candidate(candidate)
        source = "\n\n".join(f"# file: {a['path']}\n{a['content']}" for a in spec["artifacts"])
        if self.show_source:
            shown = f"Current file:\n```python\n{source}\n```"
        else:
            signatures = _signatures(spec["artifacts"])
            shown = ("Write the file " + ", ".join(a["path"] for a in spec["artifacts"]) + " from its purpose; "
                     "the current file is withheld from this instruction. Signature: "
                     + ("; ".join(signatures) or "unavailable")
                     + ". Retained history may still contain earlier source.")
        context = (f"Function: {spec['id']}\nPurpose: {spec['purpose']}\nConditions:\n"
                   + "\n".join("- " + c for c in spec["conditions"])
                   + f"\n\n{shown}\n\nFixed checker said: {checked['correction']}\n")
        result = self.loom.run(encounter=f"The function {spec['id']} failed some fixed checks. Revise it.",
                               purpose=AUTHOR_PURPOSE, reader=self.reader, context=context,
                               provider=self.author_provider, wait=self.turn_wait)
        return result, spec

    def grow(self, candidate, *, generations=3):
        """Run up to ``generations`` trial/author turns from ``candidate``; return the generation rows."""
        if type(generations) is not int or not 1 <= generations <= MAX_GENERATIONS:
            raise ValueError(f"Generations must be 1..{MAX_GENERATIONS}")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        rows = []
        for generation in range(1, generations + 1):
            checked = self.trial(candidate, generation)
            row = {"schema": SCHEMA, "generation": generation, "candidate_handle": candidate,
                   "passed": checked["passed"], "failing": len(checked["failing"]),
                   "attempt_handle": checked["attempt_handle"], "review_handle": checked["review_handle"],
                   "fenced": checked["fenced"], "at": _now(), "authority": "NONE", "weight_update": False}
            if checked["passed"]:
                spec = self.cycle.read_candidate(candidate)
                pointer = self.root / f"CURRENT-{spec['id']}.json"
                temporary = pointer.with_suffix(".new")
                temporary.write_text(json.dumps({"schema": "loom-garden-current/v1", "skill": spec["id"],
                    "candidate_handle": candidate, "revision": spec["revision"], "author": spec["author"],
                    "checked_by": self.checker_name, "attempt_handle": checked["attempt_handle"],
                    "installed": False, "at": _now()}, indent=2) + "\n")
                os.replace(temporary, pointer)
                row["stop"] = "PASSED"
                rows.append(row); self._log(row)
                return rows
            if generation == generations:
                row["stop"] = "GENERATION_BOUND"
                rows.append(row); self._log(row)
                return rows
            authored, spec = self.author(candidate, checked)
            row.update(author_status=authored["status"], author_outcome=authored.get("outcome_handle"))
            code = first_code(authored.get("response")) if authored["status"] == "COMPLETED" else None
            current = {a["path"]: a["content"] for a in spec["artifacts"]}
            if code is None or (len(current) == 1 and code.strip() == next(iter(current.values())).strip()):
                row["stop"] = "NO_NEW_REVISION"
                rows.append(row); self._log(row)
                return rows
            path = spec["artifacts"][0]["path"]
            tail = (authored["response"].split("```")[-1].strip().splitlines() or ["(no note)"])[0][:512]
            candidate = self.cycle.propose({
                "id": spec["id"], "kind": spec["kind"], "purpose": spec["purpose"],
                "conditions": spec["conditions"], "frame_limits": spec["frame_limits"],
                "change": "Loom revision after the fixed checker: " + tail,
                "artifacts": [{"path": path, "content": code if code.endswith("\n") else code + "\n"}],
                "evidence_handles": [], "author": self.reader, "parent": candidate})
            row["proposed"] = candidate
            rows.append(row); self._log(row)
        return rows

