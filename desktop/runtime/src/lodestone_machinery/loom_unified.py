"""One practice entry point for Loom's interface, reader and retained feedback.

The caller explicitly supplies the reader and synchronous provider. This module
does not choose a model, open a network connection, train weights, or promote a
learning fold. Its provider receives Loom's existing natural-language request,
including the exact native Bezoar reader projection checked by Conscience.
Nurarihyon owns dispatch, actual outcome capture and interruption reconciliation.
"""
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
import uuid

from .bezoar_learning import LearningMemory
from .learning_context import build_adapter_request, validate_practice_packet
from .records import canonical
from lodestone_nurarihyon import learning


RESULT_SCHEMA = "loom-unified-result/v1"
CORRECTION_SCHEMA = "loom-unified-correction/v1"


def _text(value, name, maximum, *, empty=False):
    if (type(value) is not str or (not empty and not value.strip())
            or len(value.encode("utf-8")) > maximum):
        raise ValueError(name + " must be bounded text")
    return value


class UnifiedLoom:
    """Join one encounter and its corrections to the installed learning cycle.

    ``seed_handles`` is an explicit caller selection; source records retain their
    conditions and matching NACRE. Latest revisions and previous corrections are
    read again for each encounter. No history is silently dropped to fit a model.
    """

    def __init__(self, memory_workspace, seed_handles):
        self.memory = (memory_workspace if isinstance(memory_workspace, LearningMemory)
                       else LearningMemory(memory_workspace))
        if (type(seed_handles) not in (list, tuple) or not 1 <= len(seed_handles) <= 16
                or any(type(item) is not str for item in seed_handles)
                or len(set(seed_handles)) != len(seed_handles)):
            raise ValueError("Unified Loom requires 1..16 explicitly selected SEED handles")
        self.seed_handles = list(seed_handles)

    @staticmethod
    def _result(report, reader):
        return {"schema": RESULT_SCHEMA, "status": report["status"],
                "response": report["outcome"]["response"], "run_id": report["run_id"],
                "run_handle": report["run_handle"], "outcome_handle": report["outcome_handle"],
                "terminal_handle": report["terminal_handle"],
                "provider_called": report["provider_called"], "reader": reader,
                "broker": report, "weight_update": False,
                "semantic_learning": "NOT_ESTABLISHED", "authority": "NONE"}

    def _turn(self, turn, wait, cancelled):
        """Wait for this memory's weaving turn; cancellable between short attempts."""
        deadline = time.monotonic() + wait
        while True:
            try:
                turn.enter_context(learning.weaving(
                    self.memory, timeout=max(0, min(2, deadline - time.monotonic()))))
                return
            except learning.BusyError:
                if cancelled is not None and cancelled():
                    raise InterruptedError("Cancelled while waiting for the Loom's turn")
                if time.monotonic() >= deadline:
                    raise

    def run(self, *, encounter, purpose, reader, context="", provider,
            run_id=None, applicability=None, wait=0, cancelled=None,
            execution_binding=None, failure_classifier=None, history_window=None):
        """Prepare, admit, dispatch once and retain an actual practice outcome.

        The provider receives ``loom-adapter-request/v1`` and returns either text
        or ``{status, response, observation}``, using Nurarihyon's terminal status
        names. It must implement its own bounded execution and confirmed stop.
        Repeating an identical completed run ID returns the retained result; it
        does not call the provider again. A different request cannot reuse it.

        Encounters take turns (``wait`` seconds at most, ``cancelled`` checked
        while waiting) so each prepares from the previous outcome. The broker is
        held only to serve and to clear; corrections, status and the observer
        reach the memory while the provider runs.
        """
        if not callable(provider):
            raise ValueError("Unified Loom needs one explicitly supplied synchronous provider")
        if type(wait) not in (int, float) or not 0 <= wait <= 86400:
            raise ValueError("Loom turn wait must be 0..86400 seconds")
        from .learning_context import history_window_policy
        history_window_policy(history_window)

        def build(saved, literal):
            request = build_adapter_request(
                self.memory, saved, request_id=saved["run_id"], context=literal,
                encounter=encounter, purpose=purpose, history_window=history_window)
            return validate_practice_packet(self.memory, saved, request, "adapter")

        held = {}

        def prepare(memory):
            held["prepared"] = memory.prepare(
                self.seed_handles, encounter=encounter, purpose=purpose,
                reader=reader, run_id=run_id, applicability=applicability)
            return held["prepared"]

        with ExitStack() as turn:
            self._turn(turn, wait, cancelled)
            # Building inside the broker preserves malformed/oversized input as
            # a failed attempt before inference, instead of leaving it pending.
            try:
                _, report = learning.execute_carved(
                    self.memory, prepare, context, provider, packet_builder=build,
                    serve_wait=min(60, wait), execution_binding=execution_binding,
                    failure_classifier=failure_classifier)
            except (ValueError, TypeError, KeyError):
                # The broker retained admission/assembly failure already. Return
                # that outcome for the UI, while never replacing a prior result
                # when a caller tried to reuse its ID with different input.
                prepared = held.get("prepared")
                if prepared is None:
                    raise
                outcome_handle = self.memory.outcome_for(prepared)
                if outcome_handle is None:
                    raise
                outcome = self.memory.read_outcome(outcome_handle)
                observation = outcome["observation"]
                if (type(observation) is not dict or observation.get("stage") not in
                        ("INPUT_ASSEMBLY", "CONSCIENCE_ADMISSION", "PREPARATION_VALIDATION")):
                    raise
                report = learning.finish(
                    self.memory, prepared, response=outcome["response"],
                    status=outcome["status"], observation=observation)
            return self._result(report, reader)

    def correct(self, outcome_handle, *, correction, author, observation="",
                event_id=None, observed_at=None):
        """Retain a teacher's correction for the next applicable encounter.

        Feedback is an UNRESOLVED review, not a declaration of learning. Its
        resolution checks explicitly remain unestablished. The original author
        may correct their own teaching without impersonating an independent
        reviewer. Only a separately attributed RESOLVED review can promote a fold.
        """
        _text(correction, "correction", 8192)
        _text(author, "author", 512)
        _text(observation, "observation", 4096, empty=True)
        event_id = uuid.uuid4().hex if event_id is None else _text(event_id, "event id", 2048)
        with learning.locked(self.memory, timeout=30):
            outcome = self.memory.read_outcome(outcome_handle)
            prepared = self.memory.read_prepared(outcome["run_handle"])
            source = "bezoar-outcome:" + outcome_handle
            text = canonical({"schema": "loom-unified-feedback-evidence/v1",
                              "run_handle": outcome["run_handle"],
                              "outcome_handle": outcome_handle,
                              "response_sha256": outcome["response_sha256"],
                              "author": author, "correction": correction,
                              "reported_observation": observation,
                              "assessment": "USER_FEEDBACK_NOT_INDEPENDENT_RESOLUTION"}).decode()
            # A transport retry with a stable event ID reuses the original time;
            # it cannot manufacture an additional recurrence or alter feedback.
            if observed_at is None:
                for handle in self.memory._catalog("evidence"):
                    old = self.memory.read_evidence(handle)
                    if (old["source"], old["event_id"]) == (source, event_id):
                        observed_at = old["observed_at"]
                        break
            if observed_at is None:
                observed_at = datetime.now(timezone.utc).isoformat()
            evidence = self.memory.evidence({
                "source": source, "event_id": event_id, "observed_at": observed_at,
                "text": text, "attribution": author + "; user-supplied correction and observation"})
            reason = ("User correction retained for subsequent practice; applicability, "
                      "pattern resolution and regression have not been independently assessed.")
            if observation:
                reason += " Reported observation: " + observation
            checks = [{"seed_handle": pair["seed_handle"],
                       "nacre_handle": pair["nacre_handle"],
                       "conditions_met": False, "pattern_resolved": False,
                       "regression_observed": False,
                       "reason": "Not assessed by this feedback capture; these flags do not establish resolution.",
                       "evidence_handles": [evidence]}
                      for pair in json.loads(prepared["context"])["pairs"]]
            review = self.memory.review_outcome(outcome_handle, {
                "reviewer": author, "verdict": "UNRESOLVED",
                "response_sha256": outcome["response_sha256"], "reason": reason,
                "correction": correction, "evidence_handles": [evidence], "checks": checks})
            return {"schema": CORRECTION_SCHEMA, "outcome_handle": outcome_handle,
                    "run_handle": outcome["run_handle"], "review_handle": review,
                    "evidence_handle": evidence, "state": "RETAINED_UNRESOLVED",
                    "weight_update": False, "semantic_learning": "NOT_ESTABLISHED",
                    "authority": "NONE"}

    def decision_review_context(self, outcome_handle):
        """Read the exact native answer and saved assessments without regrading.

        A project receipt is a pointer, never a replacement for native evidence.
        Missing or damaged records therefore raise their normal read error.
        """
        from . import decision_contract
        with learning.locked(self.memory, timeout=30):
            outcome = self.memory.read_outcome(outcome_handle)
            prepared = self.memory.read_prepared(outcome["run_handle"])
            encounter = prepared["binding"]["encounter"]
            reviews = []
            for handle in self.memory._reviews(outcome_handle):
                record = self.memory.read_review(handle)
                try:
                    saved = json.loads(record["review"]["correction"])
                except (ValueError, TypeError):
                    continue
                if type(saved) is not dict or saved.get("schema") != "loom-decision-feedback/v1":
                    continue
                decision_contract._keys(saved, ("schema", "contract", "assessment",
                    "checker_sha256", "outcome_handle", "native_status", "reviewer", "instruction"),
                    "Saved decision feedback")
                if type(saved["assessment"]) is not dict:
                    raise ValueError("Saved decision assessment must be an object")
                if (saved.get("outcome_handle") != outcome_handle
                        or saved.get("native_status") != outcome["status"]):
                    raise ValueError("Saved decision feedback differs from its native outcome")
                decision_contract._checked_contract(encounter, saved["contract"])
                assessment = saved["assessment"]
                if (assessment.get("response_sha256") != outcome["response_sha256"]
                        or assessment.get("contract_sha256") != saved["contract"]["contract_sha256"]):
                    raise ValueError("Saved decision assessment differs from its source binding")
                reviews.append({"review_handle": handle, "contract": saved["contract"],
                    "assessment": assessment, "checker_sha256": saved["checker_sha256"],
                    "reviewer": saved["reviewer"],
                    "learning_verdict": record["review"]["verdict"]})
        return {"schema": "loom-decision-review-context/v1", "outcome_handle": outcome_handle,
            "encounter": encounter, "purpose": prepared["binding"]["purpose"],
            "response": outcome["response"], "native_status": outcome["status"],
            "response_sha256": outcome["response_sha256"], "reviews": reviews}

    def preview_decision_review(self, outcome_handle, *, case_json, author, policy=None):
        """Preview a source-bound assessment; no writes, inference or promotion."""
        from . import decision_contract
        with learning.locked(self.memory, timeout=30):
            outcome = self.memory.read_outcome(outcome_handle)
            prepared = self.memory.read_prepared(outcome["run_handle"])
        encounter = prepared["binding"]["encounter"]
        contract = decision_contract.bind_decision_contract(
            encounter, case_json=case_json, author=author, policy=policy)
        assessment = decision_contract.review_decision(encounter, outcome["response"], contract,
            completed=outcome["status"] == "COMPLETED")
        return {"contract": contract, "assessment": assessment,
                "native_status": outcome["status"], "outcome_handle": outcome_handle}

    def review_decision(self, outcome_handle, *, contract, author):
        """Check a retained structured decision and retain the separate review.

        This is opt-in, requires an exact source-bound contract, and never calls
        a provider or replaces its answer/status. A declared policy may derive
        an action; that interpretation remains attributed, not independently
        established. Even a passing check is retained as an UNRESOLVED learning
        review. Oversized feedback is refused before any write, not truncated.
        """
        from . import decision_contract
        _text(author, "author", 384)
        # Assess and retain the same bounded snapshot, even if a caller later
        # mutates its dictionary while the native feedback is being written.
        contract = decision_contract._copy_json(contract, decision_contract.MAX_CONTRACT_BYTES)
        with learning.locked(self.memory, timeout=30):
            outcome = self.memory.read_outcome(outcome_handle)
            prepared = self.memory.read_prepared(outcome["run_handle"])
        assessment = decision_contract.review_decision(
            prepared["binding"]["encounter"], outcome["response"], contract,
            completed=outcome["status"] == "COMPLETED")
        checker_sha = hashlib.sha256(Path(decision_contract.__file__).read_bytes()).hexdigest()
        # The raw response is already retained verbatim in the bound outcome.
        # Keep the complete contract here so hashes never replace inspectable
        # evidence or hide the policy author's declared interpretation.
        retained = {key: value for key, value in assessment.items()
                    if key not in ("raw_response", "parsed_response")}
        correction = canonical({"schema": "loom-decision-feedback/v1",
            "contract": contract, "assessment": retained,
            "checker_sha256": checker_sha, "outcome_handle": outcome_handle,
            "native_status": outcome["status"], "reviewer": author,
            "instruction": "Review the cited condition, target and transition before reusing this answer. "
                           "The derived decision follows a declared interpretation; it is not a new model answer "
                           "or proof of semantic learning."}).decode()
        _text(correction, "decision feedback", 8192)
        event_id = "decision-review-" + hashlib.sha256(correction.encode()).hexdigest()
        receipt = self.correct(outcome_handle, correction=correction,
            author=author + "; deterministic decision-contract check",
            observation="Source-bound code check; declared interpretation is not an independent semantic review.",
            event_id=event_id)
        return {"schema": "loom-retained-decision-review/v1", "assessment": assessment,
                "retention": receipt, "checker_sha256": checker_sha,
                "native_status": outcome["status"], "provider_called": False,
                "weight_update": False, "semantic_learning": "NOT_ESTABLISHED", "authority": "NONE"}

    def status(self):
        """Read native pending runs and terminal receipts without a model call."""
        return learning.status(self.memory)

    def preview_origami(self, outcome_handle, *, workspace, root, paths):
        from .loom_handoff import LoomHandoff
        return LoomHandoff(self, workspace).preview(outcome_handle, root=root, paths=paths)

    def complete_origami(self, handoff_id, curation, *, workspace):
        from .loom_handoff import LoomHandoff
        return LoomHandoff(self, workspace).complete(handoff_id, curation)
