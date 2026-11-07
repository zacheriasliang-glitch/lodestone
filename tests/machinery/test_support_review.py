"""Scripted judgments exercise review custody, never independent semantic skill."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from lodestone_machinery.origami import Origami
from lodestone_machinery.records import RecordStore
from lodestone_machinery.support_review import (
    publish_support_review, read_review_file, reviewed_answer, support_request,
)


class SupportReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "sources"
        self.root.mkdir()
        self.source = self.root / "decision.md"
        self.source.write_bytes(b"Current label: blue. Mode: paper.\n")
        self.organ = Origami(self.base / "workspace")
        self.roles = {"constructor": "fixture-constructor", "consumer": "fixture-consumer",
                      "checker": "fixture-checker", "provider": "fixture-provider"}

    def completed(self, *, question="Which label and mode are recorded?", previous=None):
        run = self.organ.start(self.root, ["decision.md"], question, "fixture-current",
                               "bounded fixture source reads", roles=self.roles, previous=previous)
        run_id = run["run_id"]
        request = self.organ.request(run_id)
        evidence = request["evidence"][0]
        response = {
            "request_id": request["request_id"], "decision": "ANSWER",
            "claims": [{"id": name, "subject": name, "value": value,
                        "scope": "fixture only", "epoch": "fixture-current", "basis": "RECORDED",
                        "support": [evidence["id"]]} for name, value in (("label", "blue"), ("mode", "paper"))],
            "relations": [], "alternatives": [], "answer": "The label is blue and the mode is paper.",
            "answer_claim_ids": ["label", "mode"], "next_check": None,
            "coverage_assumptions": ["Only the scripted fixture source is in scope."], "unresolved": [],
        }
        self.organ.advance(run_id, {"provider": "fixture-provider", "model": "NONE",
                                   "response_id": None, "usage": None, "response": response})
        return run_id

    def review(self, run_id):
        request = support_request(self.organ, run_id)
        return {
            "schema": "origami-support-review/v1", "review_request_sha256": request["request_sha256"],
            "reviewer": "fixture-checker", "claim_verdicts": [
                {"id": claim["id"], "verdict": "SUPPORTED", "reason": "Scripted literal-match verdict."}
                for claim in request["fold"]["claims"]],
            "answer_verdict": "PASS", "omissions": [], "contradictions": [],
            "applicability": "SAME_QUESTION", "reason": "Scripted fixture addresses its supplied question.",
        }

    def test_request_is_read_only_and_missing_review_is_visible(self):
        run_id = self.completed()
        before = sorted(str(p.relative_to(self.organ.workspace)) for p in self.organ.workspace.rglob("*"))
        request = support_request(self.organ, run_id)
        self.assertEqual(request["reviewer"], "fixture-checker")
        self.assertEqual(set(request["files"]), {"intent.json", "result.json", "request-000.json",
                                                "response-000.json", "call-000.json", "answer.md"})
        rendered = reviewed_answer(self.organ, run_id)
        self.assertEqual(rendered["status"], "NOT_INDEPENDENTLY_REVIEWED")
        self.assertIn("NOT_INDEPENDENTLY_REVIEWED", rendered["markdown"])
        self.assertEqual(before, sorted(str(p.relative_to(self.organ.workspace)) for p in self.organ.workspace.rglob("*")))

    def test_review_is_linked_immutable_and_idempotent(self):
        run_id = self.completed()
        review = self.review(run_id)
        result = publish_support_review(self.organ, run_id, review)
        again = publish_support_review(self.organ, run_id, review)
        self.assertEqual(result["handle"], again["handle"])
        store = RecordStore(self.organ.workspace)
        self.assertEqual(store.verify_links([result["handle"]])["records_checked"], 3)
        rendered = reviewed_answer(self.organ, run_id, result["handle"])
        self.assertEqual(rendered["status"], "REVIEWED_PASS")
        self.assertIn("label [SUPPORTED]", rendered["markdown"])
        self.assertIn("identity strings do not authenticate", rendered["attribution"])

    def test_changed_source_refuses_request_publish_and_consume(self):
        run_id = self.completed()
        review = self.review(run_id)
        accepted = publish_support_review(self.organ, run_id, review)
        self.source.write_bytes(b"Current label: green. Mode: paper.\n")
        for operation in (lambda: support_request(self.organ, run_id),
                          lambda: publish_support_review(self.organ, run_id, review),
                          lambda: reviewed_answer(self.organ, run_id, accepted["handle"])):
            with self.assertRaisesRegex(ValueError, "Source scope changed"):
                operation()

    def test_review_for_another_run_is_not_reused(self):
        old = self.completed()
        accepted = publish_support_review(self.organ, old, self.review(old))
        later = self.completed(previous=old)
        with self.assertRaisesRegex(ValueError, "does not match"):
            reviewed_answer(self.organ, later, accepted["handle"])

    def test_unrelated_question_with_current_bytes_never_gets_pass(self):
        old = self.completed()
        later = self.completed(question="What is the capital of France?", previous=old)
        request = support_request(self.organ, later)
        self.assertEqual(request["prior_knowledge"]["status"], "BYTES_CURRENT")
        review = self.review(later)
        review["applicability"] = "UNRELATED"
        with self.assertRaisesRegex(ValueError, "PASS requires"):
            publish_support_review(self.organ, later, review)
        review["answer_verdict"] = "FAIL"
        review["reason"] = "The source and answer say nothing about the requested capital."
        accepted = publish_support_review(self.organ, later, review)
        rendered = reviewed_answer(self.organ, later, accepted["handle"])
        self.assertEqual(rendered["status"], "REVIEWED_FAIL")
        self.assertIn("not accepted by this review", rendered["markdown"])

    def test_related_question_requires_its_own_review(self):
        old = self.completed()
        later = self.completed(question="Does the recorded mode permit a paper label?", previous=old)
        review = self.review(later)
        review["applicability"] = "RELATED"
        accepted = publish_support_review(self.organ, later, review)
        self.assertEqual(reviewed_answer(self.organ, later, accepted["handle"])["status"], "REVIEWED_PASS")

    def test_claim_coverage_rejects_missing_extra_duplicate_and_unknown(self):
        run_id = self.completed()
        original = self.review(run_id)
        mutations = [lambda x: x["claim_verdicts"].pop(),
                     lambda x: x["claim_verdicts"].append(deepcopy(x["claim_verdicts"][0])),
                     lambda x: x["claim_verdicts"][1].update(id="label"),
                     lambda x: x["claim_verdicts"][1].update(id="invented")]
        for mutate in mutations:
            review = deepcopy(original)
            mutate(review)
            with self.assertRaises(ValueError):
                publish_support_review(self.organ, run_id, review)
        self.assertFalse((self.organ.workspace / "organs").exists())

    def test_wrong_and_self_reviewer_refuse_before_records(self):
        run_id = self.completed()
        for reviewer in ("somebody-else", "fixture-provider"):
            review = self.review(run_id)
            review["reviewer"] = reviewer
            with self.assertRaisesRegex(ValueError, "separate checker"):
                publish_support_review(self.organ, run_id, review)
        self.assertFalse((self.organ.workspace / "organs").exists())

    def test_pass_cannot_hide_unsupported_unknown_omitted_or_conflicting_claims(self):
        run_id = self.completed()
        for verdict in ("UNSUPPORTED", "UNKNOWN"):
            review = self.review(run_id)
            review["claim_verdicts"][0]["verdict"] = verdict
            with self.assertRaisesRegex(ValueError, "PASS requires"):
                publish_support_review(self.organ, run_id, review)
        for field in ("omissions", "contradictions"):
            review = self.review(run_id)
            review[field] = ["A material issue remains."]
            with self.assertRaisesRegex(ValueError, "PASS requires"):
                publish_support_review(self.organ, run_id, review)

    def test_unknown_review_remains_unknown(self):
        run_id = self.completed()
        review = self.review(run_id)
        review["answer_verdict"] = "UNKNOWN"
        review["claim_verdicts"][0]["verdict"] = "UNKNOWN"
        accepted = publish_support_review(self.organ, run_id, review)
        self.assertEqual(reviewed_answer(self.organ, run_id, accepted["handle"])["status"], "REVIEWED_UNKNOWN")

    def test_byte_change_even_with_same_parsed_json_invalidates_review(self):
        run_id = self.completed()
        review = self.review(run_id)
        accepted = publish_support_review(self.organ, run_id, review)
        path = self.organ._dir(run_id) / "result.json"
        path.write_bytes(path.read_bytes() + b"\n")
        with self.assertRaisesRegex(ValueError, "different or changed"):
            publish_support_review(self.organ, run_id, review)
        with self.assertRaisesRegex(ValueError, "does not match"):
            reviewed_answer(self.organ, run_id, accepted["handle"])

    def test_changed_response_or_answer_artifact_refuses(self):
        run_id = self.completed()
        response_path = self.organ._dir(run_id) / "response-000.json"
        original = response_path.read_bytes()
        response = json.loads(original)
        response["response"]["answer"] = "Changed answer."
        response_path.write_text(json.dumps(response))
        with self.assertRaisesRegex(ValueError, "does not match"):
            support_request(self.organ, run_id)
        response_path.write_bytes(original)
        (self.organ._dir(run_id) / "answer.md").write_text("changed artifact")
        with self.assertRaisesRegex(ValueError, "artifact changed"):
            support_request(self.organ, run_id)

    def test_internal_source_symlink_is_refused_even_with_identical_bytes(self):
        run_id = self.completed()
        real = self.root / "identical.md"
        real.write_bytes(self.source.read_bytes())
        self.source.unlink()
        self.source.symlink_to(real)
        with self.assertRaisesRegex(ValueError, "symlink"):
            support_request(self.organ, run_id)

    def test_saved_file_symlink_and_damaged_record_are_refused(self):
        run_id = self.completed()
        accepted = publish_support_review(self.organ, run_id, self.review(run_id))
        store = RecordStore(self.organ.workspace)
        record_path = store.path(accepted["handle"])
        record_path.write_bytes(record_path.read_bytes() + b" ")
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            reviewed_answer(self.organ, run_id, accepted["handle"])
        path = self.organ._dir(run_id) / "result.json"
        copy = self.base / "copy.json"
        copy.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(copy)
        with self.assertRaisesRegex(ValueError, "symlink"):
            support_request(self.organ, run_id)

    def test_strict_input_rejects_duplicate_nonfinite_symlink_and_oversize(self):
        path = self.base / "review.json"
        for raw in (b'{"answer_verdict":"FAIL","answer_verdict":"PASS"}',
                    b'{"score":NaN}', b'{"score":1e10000}'):
            path.write_bytes(raw)
            with self.assertRaises(ValueError):
                read_review_file(path)
        path.write_bytes(b'{}')
        alias = self.base / "alias.json"
        alias.symlink_to(path)
        with self.assertRaises(ValueError):
            read_review_file(alias)
        path.write_bytes(b'{}\n\n')
        with self.assertRaises(ValueError):
            read_review_file(path, max_bytes=3)
        # A malformed scalar also cannot become a structurally valid review.
        path.write_bytes(b'[]')
        run_id = self.completed()
        with self.assertRaises(ValueError):
            publish_support_review(self.organ, run_id, read_review_file(path))

    def test_malformed_saved_request_is_a_structured_refusal(self):
        run_id = self.completed()
        path = self.organ._dir(run_id) / "request-000.json"
        path.write_bytes(b'[]')
        with self.assertRaises(ValueError):
            support_request(self.organ, run_id)

    def test_invalid_encoding_malformed_and_deep_json_are_structured_refusals(self):
        path = self.base / "malformed.json"
        for raw in (b'{"text":"\xff"}', '{"text":"valid"}'.encode("utf-16"),
                    b'{"unfinished":', b'{"surrogate":"\\ud800"}',
                    b'[' * 10000 + b'0' + b']' * 10000):
            path.write_bytes(raw)
            with self.assertRaisesRegex(ValueError, "strict UTF-8 JSON"):
                read_review_file(path)

    def test_pending_run_has_no_reviewable_answer(self):
        run = self.organ.start(self.root, ["decision.md"], "Which label?", "fixture-current",
                               "test only", roles=self.roles)
        with self.assertRaises(OSError):
            support_request(self.organ, run["run_id"])


if __name__ == "__main__":
    unittest.main()
