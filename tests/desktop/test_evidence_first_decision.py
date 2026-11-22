"""Evidence-first source binding and review; authored fixtures, no model calls."""
from copy import deepcopy
import hashlib
import json
import unittest
from unittest.mock import patch

from test_decision_format import fixture as format_fixture, formats
from test_decision_contract import fixture, bind, answer, policy_for
from lodestone_machinery import decision_contract as contracts


def nested_answer(case, *, index=0, action=None, transition="HOLD"):
    return {"basis": deepcopy(case["observations"][index]), "case_id": case["case_id"],
            "decision": {"action": action or case["recorded_prior_action"],
                         "transition": transition}}


class EvidenceFirstFormatTests(unittest.TestCase):
    def test_default_binding_and_flat_review_match_prefeature_golden_bytes(self):
        _, text, encounter = format_fixture()
        default = formats.bind_decision_format(encounter, case_json=text)
        explicit = formats.bind_decision_format(encounter, case_json=text, strategy="flat")
        self.assertEqual(default, explicit)
        # Recorded from the unchanged parent implementation, before this feature.
        self.assertEqual(hashlib.sha256(formats._canonical(default)).hexdigest(),
                         "38910e778ad542f8029f1279b8bc725469188a724c431cb1c0893150aba8d421")
        case, policy = fixture()
        encounter, contract = bind(case, policy)
        reviewed = contracts.review_decision(encounter, " \n" + answer(case) + "\n ", contract)
        self.assertEqual(hashlib.sha256(contracts._canonical(reviewed)).hexdigest(),
                         "1343203be0bff249ad1c8ab1d3c8676c028b1960beae23609d34a273eb16e232")

    def test_all_exact_rows_and_decisions_survive_canonical_wire_order(self):
        case, text, encounter = format_fixture()
        before = deepcopy(case)
        with patch.object(contracts, "_latest", side_effect=AssertionError("No selection")), \
                patch.object(contracts, "_derive", side_effect=AssertionError("No answer derivation")):
            bound = formats.bind_decision_format(encounter, case_json=text, strategy="evidence_first")
            checked = formats.validate_decision_format(encounter, bound)
        wire = json.loads(formats._canonical(checked))
        props = wire["response_schema"]["properties"]
        self.assertEqual(list(props), ["basis", "case_id", "decision"])
        self.assertEqual(props["basis"], {"enum": case["observations"]})
        self.assertEqual(props["decision"]["properties"]["action"]["enum"], case["allowed_actions"])
        self.assertEqual(props["decision"]["properties"]["transition"]["enum"], ["HOLD", "CHANGE"])
        self.assertEqual(set(wire["response_schema"]["required"]), set(props))
        self.assertFalse(props["decision"]["additionalProperties"])
        self.assertFalse(wire["response_schema"]["additionalProperties"])
        self.assertEqual(bound["schema"], formats.EVIDENCE_FIRST_SCHEMA)
        self.assertEqual(bound["strategy"], "evidence_first")
        self.assertIsNone(bound["source_contract"]["policy"])
        self.assertEqual(case, before)
        bound["response_schema"]["properties"]["basis"]["enum"][0]["text"] = "mutated"
        self.assertEqual(checked["response_schema"]["properties"]["basis"]["enum"], before["observations"])

    def test_no_target_or_verification_filter_and_source_order_is_retained(self):
        case, _, _ = format_fixture()
        case["target"] = "absent-target"
        case["observations"].reverse()
        for row in case["observations"]:
            row["verified"] = False
        raw = json.dumps(case, ensure_ascii=False)
        bound = formats.bind_decision_format(raw, case_json=raw, strategy="evidence_first")
        self.assertEqual(bound["response_schema"]["properties"]["basis"]["enum"], case["observations"])

    def test_rehashed_narrowing_row_tamper_and_schema_switch_are_rejected(self):
        _, text, encounter = format_fixture()
        original = formats.bind_decision_format(encounter, case_json=text, strategy="evidence_first")
        mutations = [
            lambda b: b["response_schema"]["properties"]["basis"]["enum"].pop(),
            lambda b: b["response_schema"]["properties"]["basis"]["enum"][0].update(verified=1),
            lambda b: b["response_schema"]["properties"]["basis"]["enum"][0].update(text="changed"),
            lambda b: b["response_schema"]["properties"]["decision"]["properties"]["action"].update(enum=["KEEP"]),
            lambda b: b["response_schema"]["properties"]["decision"]["properties"]["transition"].update(enum=["HOLD"]),
            lambda b: b.update(strategy="flat"),
            lambda b: b.update(schema=formats.SCHEMA),
            lambda b: b["source_contract"].update(policy={"rules": [], "observed_conditions": []}),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                bound = deepcopy(original)
                mutate(bound)
                bound["response_schema_sha256"] = formats._sha(formats._canonical(bound["response_schema"]))
                bound.pop("format_sha256")
                bound["format_sha256"] = formats._sha(formats._canonical(bound))
                with self.assertRaises(ValueError):
                    formats.validate_decision_format(encounter, bound)
        with self.assertRaises(ValueError):
            formats.validate_decision_format(encounter + " drift", original)
        for strategy in (None, True, "unknown", []):
            with self.assertRaises(ValueError):
                formats.bind_decision_format(encounter, case_json=text, strategy=strategy)


class EvidenceFirstReviewTests(unittest.TestCase):
    def test_raw_nested_response_and_exact_source_basis_are_preserved(self):
        case, policy = fixture()
        encounter, contract = bind(case, policy)
        original = nested_answer(case)
        raw = "\n " + json.dumps(original, ensure_ascii=False, indent=2) + "\n"
        reviewed = contracts.review_decision(encounter, raw, contract)
        self.assertEqual(reviewed["status"], "PASS")
        self.assertEqual(reviewed["raw_response"], raw)
        self.assertEqual(reviewed["response_sha256"], hashlib.sha256(raw.encode()).hexdigest())
        self.assertEqual(reviewed["parsed_response"], original)
        self.assertEqual(reviewed["decision_response"], json.loads(answer(case)))
        self.assertEqual(reviewed["response_format"], "evidence-first/v1")
        self.assertEqual(reviewed["policy_attribution"], "DECLARED_INTERPRETATION")
        self.assertEqual(reviewed["semantic_learning"], "NOT_ESTABLISHED")

    def test_exact_row_copy_does_not_establish_action_support(self):
        case, policy = fixture()
        encounter, contract = bind(case)
        raw = json.dumps(nested_answer(case))
        reviewed = contracts.review_decision(encounter, raw, contract)
        self.assertEqual(reviewed["status"], "UNRESOLVED")
        self.assertIsNone(reviewed["condition_action_support"])
        encounter, contract = bind(case, policy)
        wrong = json.dumps(nested_answer(case, action="REDIRECT", transition="CHANGE"))
        reviewed = contracts.review_decision(encounter, wrong, contract)
        self.assertEqual(reviewed["status"], "REJECTED")
        self.assertIn("ACTION_CONTRADICTS_DECLARED_CONDITION", reviewed["reason_codes"])
        self.assertEqual(reviewed["decision_response"]["action"], "REDIRECT")

    def test_other_unverified_old_and_inconsistent_decisions_remain_failures(self):
        case, _ = fixture()
        case["observations"].extend([
            {"id": "new", "order": 2, "target": "unit-A", "verified": True, "text": "Condition β."},
            {"id": "rumour", "order": 10, "target": "unit-A", "verified": False, "text": "Condition α."},
        ])
        encounter, contract = bind(case, policy_for(case, ["α", "β", "β", "α"]))
        for index, code in ((0, "EVIDENCE_NOT_LATEST"), (1, "EVIDENCE_TARGET_MISMATCH"),
                            (3, "UNVERIFIED_EVIDENCE")):
            reviewed = contracts.review_decision(encounter, json.dumps(nested_answer(case, index=index)), contract)
            self.assertEqual(reviewed["status"], "REJECTED")
            self.assertIn(code, reviewed["reason_codes"])
        good = nested_answer(case, index=2, action="REDIRECT", transition="CHANGE")
        self.assertEqual(contracts.review_decision(encounter, json.dumps(good), contract)["status"], "PASS")
        good["decision"]["transition"] = "HOLD"
        reviewed = contracts.review_decision(encounter, json.dumps(good), contract)
        self.assertIn("TRANSITION_INCONSISTENT", reviewed["reason_codes"])

    def test_tampered_rows_types_duplicate_and_unexpected_fields_are_rejected(self):
        case, policy = fixture()
        encounter, contract = bind(case, policy)
        original = nested_answer(case)
        variants = []
        for key, value in (("id", "missing"), ("order", True), ("order", 1.0),
                           ("target", "unit-B"), ("verified", 1), ("text", "changed"),
                           ("extra", "unexpected")):
            changed = deepcopy(original)
            changed["basis"][key] = value
            variants.append(changed)
        changed = deepcopy(original)
        changed["decision"]["evidence_ids"] = ["old"]
        variants += [changed, dict(original, action="KEEP"), dict(original, basis=None)]
        raws = [json.dumps(row) for row in variants]
        raws.append(json.dumps(original).replace('"id": "old"', '"id": "old", "id": "old"'))
        for raw in raws:
            with self.subTest(raw=raw):
                reviewed = contracts.review_decision(encounter, raw, contract)
                self.assertEqual(reviewed["status"], "REJECTED")
                self.assertFalse(reviewed["response_format_valid"])
                self.assertEqual(reviewed["raw_response"], raw)

    def test_incomplete_nested_answer_never_passes(self):
        case, policy = fixture()
        encounter, contract = bind(case, policy)
        raw = json.dumps(nested_answer(case))
        reviewed = contracts.review_decision(encounter, raw, contract, completed=False)
        self.assertEqual(reviewed["status"], "UNRESOLVED")
        self.assertIn("PROVIDER_INCOMPLETE", reviewed["reason_codes"])
        self.assertEqual(reviewed["raw_response"], raw)


if __name__ == "__main__":
    unittest.main()
