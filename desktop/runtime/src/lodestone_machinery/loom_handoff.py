"""Explicit human curation of a retained Loom encounter into real Origami state.

Preview does not dispatch a provider or prepare native practice. Submission uses
Origami's ordinary Conscience/Nurarihyon lifecycle with the named human curator
as provider. Prior Loom text is advice in learning history, never source evidence.
"""
from copy import deepcopy
import fcntl
import hashlib
import json
from pathlib import Path
import re
import uuid
from lodestone_nurarihyon import learning

from .origami import Origami, _snapshot, _initial_evidence
from .records import canonical
from .source_seeds import SEED_FIELDS


def _write(path, value):
    with path.open("xb") as stream:
        stream.write(canonical(value))


def _text(value, name, maximum=8192):
    if type(value) is not str or not value.strip() or len(value.encode()) > maximum:
        raise ValueError(name + " must be nonempty bounded text")
    return value


class LoomHandoff:
    def __init__(self, loom, workspace):
        self.loom = loom
        self.workspace = Path(workspace).expanduser().resolve()

    def _advice(self):
        memory = self.loom.memory
        seeds = memory.resolve_seeds(self.loom.seed_handles)
        pairs = memory._pairs(seeds, None)
        nacres = sorted({pair["nacre_handle"] for pair in pairs})
        return {"pairs": pairs, "seeds": [{"handle": handle, "record": memory.read_seed(handle)} for handle in seeds],
                "nacres": [{"handle": handle, "record": memory.read_nacre(handle)} for handle in nacres],
                "history": memory._history(nacres)}

    def _directory(self, handoff_id):
        if type(handoff_id) is not str or not re.fullmatch(r"[a-f0-9]{32}", handoff_id):
            raise ValueError("Invalid handoff identifier")
        directory = self.workspace / handoff_id
        if directory.is_symlink() or directory.resolve() != directory:
            raise ValueError("Handoff directory must not be a symbolic link")
        return directory

    def preview(self, outcome_handle, *, root, paths):
        with learning.locked(self.loom.memory):
            return self._preview(outcome_handle, root=root, paths=paths)

    def _preview(self, outcome_handle, *, root, paths):
        outcome = self.loom.memory.read_outcome(outcome_handle)
        original = self.loom.memory.read_prepared(outcome["run_handle"])
        advice = self._advice()
        if not any(row.get("outcome_handle") == outcome_handle for row in advice["history"]):
            raise ValueError("This encounter is outside the explicitly selected learning practice")
        root = Path(root).expanduser().resolve(strict=True)
        if self.workspace.is_relative_to(root):
            raise ValueError("Choose source files in a root outside the handoff workspace")
        if (type(paths) is not list or not 1 <= len(paths) <= 100
                or any(type(path) is not str for path in paths) or len(set(paths)) != len(paths)):
            raise ValueError("Select 1..100 distinct explicit source paths")
        sources, manifest, _ = _snapshot(root, sorted(paths))
        evidence, omitted, _ = _initial_evidence(sources, original["binding"]["encounter"], 64, 16384)
        if not evidence:
            raise ValueError("No source passage fits the 16 KiB handoff evidence limit; select a smaller source")
        handoff_id = uuid.uuid4().hex
        proposal = {"schema": "loom-origami-proposal/v1", "handoff_id": handoff_id,
                    "state": "PROPOSED", "root": str(root), "paths": sorted(paths),
                    "scope": manifest, "evidence": evidence, "omitted_passages": omitted,
                    "outcome_handle": outcome_handle, "original_outcome": outcome,
                    "learning_advice": advice,
                    "question": original["binding"]["encounter"], "purpose": original["binding"]["purpose"],
                    "seed_handles": list(self.loom.seed_handles),
                    "advice_use": "Original contribution and corrections are learning advice, not factual source support.",
                    "authority": "NONE", "weight_update": False}
        proposal["proposal_sha256"] = hashlib.sha256(canonical(proposal)).hexdigest()
        directory = self._directory(handoff_id)
        directory.mkdir(parents=True, exist_ok=False)
        _write(directory / "proposal.json", proposal)
        return proposal

    def complete(self, handoff_id, curation):
        directory = self._directory(handoff_id)
        # One submission per proposal; an uncertain interruption is never retried.
        with (directory / "submission.lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with learning.locked(self.loom.memory):
                return self._complete(directory, curation)

    def _complete(self, directory, curation):
        proposal = json.loads((directory / "proposal.json").read_text())
        digest = proposal.pop("proposal_sha256")
        if hashlib.sha256(canonical(proposal)).hexdigest() != digest:
            raise ValueError("Handoff proposal changed")
        proposal["proposal_sha256"] = digest
        curation = deepcopy(curation)
        curation_digest = hashlib.sha256(canonical(curation)).hexdigest()
        result_path = directory / "result.json"
        if result_path.exists():
            result = json.loads(result_path.read_text())
            if result["curation_sha256"] != curation_digest:
                raise ValueError("A different curation cannot reuse this handoff")
            return result
        if (directory / "curation.json").exists():
            raise ValueError("This handoff was interrupted after submission; inspect retained state before a new proposal")
        _write(directory / "curation.json", curation)
        curator = curation.get("curator", "undeclared-curator") if type(curation) is dict else "undeclared-curator"
        try:
            result = self._convert(directory, proposal, curation)
        except Exception as exc:
            # A failed conversion remains actual failed practice, not a missing fold
            # or a successful answer. This callback makes no model call.
            failure = self.loom.run(encounter=proposal["question"], purpose=proposal["purpose"],
                reader="loom-origami-handoff", provider=lambda packet: {
                    "status": "FAILED", "response": None,
                    "observation": {"error": str(exc), "handoff_id": proposal["handoff_id"],
                                    "original_outcome": proposal["outcome_handle"], "curator": str(curator),
                                    "proposal": str(directory / "proposal.json"),
                                    "curation": str(directory / "curation.json"), "model_called": False}})
            result = {"state": "FAILED", "error": str(exc), "outcome_handle": failure.get("outcome_handle"),
                      "terminal_handle": failure.get("terminal_handle")}
        result.update(schema="loom-origami-handoff/v1", handoff_id=proposal["handoff_id"],
                      original_outcome_handle=proposal["outcome_handle"], curation_sha256=curation_digest,
                      record=str(result_path), weight_update=False, semantic_learning="NOT_ESTABLISHED",
                      support_review="NOT_REVIEWED", authority="NONE")
        _write(result_path, result)
        return result

    def _convert(self, directory, proposal, curation):
        fields = {"curator", "checker", "decision", "answer", "relationship", "conditions", "action",
                  "effect", "uncertainty", "limits", "support_ids"}
        if type(curation) is not dict or set(curation) != fields:
            raise ValueError("Curation must explicitly include relationship, conditions, action, effect, limits and source support")
        if len(canonical(curation)) > 24576:
            raise ValueError("Curation exceeds the retained-response bound; shorten the curation without omitting conditions")
        for key in fields - {"support_ids"}:
            _text(curation[key], key)
        if curation["decision"] not in ("ANSWER", "UNRESOLVED"):
            raise ValueError("Choose a provisional answer or unresolved curation")
        selected = curation["support_ids"]
        available = {row["id"]: row for row in proposal["evidence"]}
        if (type(selected) is not list or not selected or any(type(item) is not str for item in selected)
                or len(set(selected)) != len(selected) or any(item not in available for item in selected)):
            raise ValueError("Select actual source passages supporting this curation")
        _, manifest, _ = _snapshot(Path(proposal["root"]), proposal["paths"])
        if manifest != proposal["scope"]:
            raise ValueError("Selected source bytes changed after preview; prepare a new handoff")
        if self._advice() != proposal["learning_advice"]:
            raise ValueError("Learning advice or correction reviews changed after preview; prepare a new handoff")
        workspace = directory / "origami"
        workspace.mkdir()
        _write(workspace / "learning-policy.json", {
            "schema": "origami-learning-policy/v1", "required": True,
            "memory_workspace": str(self.loom.memory.workspace),
            "purpose": proposal["purpose"], "seed_handles": proposal["seed_handles"]})
        organ = Origami(workspace)
        started = organ.start(proposal["root"], proposal["paths"], proposal["question"],
            "current explicitly selected source snapshot", "Explicit Loom handoff submission",
            max_calls=1, seed_passages=[{key: available[item][key] for key in SEED_FIELDS} for item in selected],
            roles={"constructor": curation["curator"], "provider": curation["curator"],
                   "consumer": "Loom later reader", "checker": curation["checker"]})
        run_id = started["run_id"]

        def provider(request):
            context = json.loads(request["learning_context"]["context"])
            if not any(row.get("outcome_handle") == proposal["outcome_handle"]
                       and row.get("outcome") == proposal["original_outcome"] for row in context["history"]):
                raise ValueError("Original Loom outcome is missing from complete learning advice")
            def claim(ident, subject, value):
                return {"id": ident, "subject": subject, "value": value, "basis": "INFERRED",
                        "scope": proposal["purpose"], "epoch": request["epoch"], "support": selected}
            response = {"request_id": request["request_id"], "decision": curation["decision"],
                "claims": [claim("conditions", "Applicability conditions", curation["conditions"]),
                           claim("relationship", "Conditional action and observed or predicted effect", {
                               key: curation[key] for key in ("answer", "relationship", "action", "effect", "limits", "uncertainty")})],
                "relations": [{"id": "conditional-action", "type": "depends_on", "from": "relationship",
                               "to": "conditions", "support": selected}], "alternatives": [],
                "answer": curation["answer"] if curation["decision"] == "ANSWER" else None,
                "answer_claim_ids": ["conditions", "relationship"] if curation["decision"] == "ANSWER" else [],
                "coverage_assumptions": [curation["limits"],
                    "Human-curated inference over explicitly selected passages; independent support review pending.",
                    "Original Loom contribution is retained advice, not factual source evidence."],
                "unresolved": [curation["uncertainty"]], "next_check": None}
            return {"provider": curation["curator"], "model": "human-curated; no model called",
                    "response_id": None, "usage": None, "response": response}

        completed = organ.run(run_id, provider)
        review_path = None
        review_error = None
        if completed["status"] == "ANSWER":
            from .support_review import support_request
            try:
                review_path = directory / "support-request.json"
                _write(review_path, support_request(organ, run_id))
            except Exception as exc:
                # A later review-packet failure cannot relabel a retained answer
                # as a nonexistent conversion. Preserve both actual outcomes.
                review_path = None
                review_error = str(exc)
        return {"state": "COMPLETED_PROVISIONAL" if completed["status"] == "ANSWER" else completed["status"],
                "origami_status": completed["status"], "origami_run_id": run_id,
                "origami_workspace": str(workspace), "artifact": completed.get("artifact"),
                "support_request": str(review_path) if review_path else None,
                "support_request_error": review_error,
                "learning_outcome": completed.get("learning_outcome"), "reason": completed.get("reason")}
