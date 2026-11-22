"""Synthetic contract checks, including exposed failure shapes; no model calls."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "desktop/runtime/src"))
from lodestone_machinery.decision_contract import (
    MAX_DEPTH, MAX_RESPONSE_BYTES, bind_decision_contract, review_decision,
)


def fixture():
    case = {
        "case_id": "sample", "target": "unit-A", "recorded_prior_action": "KEEP",
        "rules": ["Under condition α choose KEEP; under condition β choose REDIRECT."],
        "observations": [
            {"id": "old", "order": 1, "target": "unit-A", "verified": True,
             "text": "Recorded condition α for unit-A."},
            {"id": "other", "order": 9, "target": "unit-B", "verified": True,
             "text": "Recorded condition β for unit-B."},
        ], "allowed_actions": ["KEEP", "REDIRECT"],
    }
    return case, policy_for(case, ["α", "β"])


def policy_for(case, conditions):
    return {
        "rules": [{"condition": condition, "action": action, "source_quote": case["rules"][0]}
                  for condition, action in zip(("α", "β"), case["allowed_actions"])],
        "observed_conditions": [
            {"observation_id": row["id"], "condition": condition, "source_quote": row["text"]}
            for row, condition in zip(case["observations"], conditions)],
    }


def bind(case, policy=None, *, prefix="Current task:\n", suffix="\nEnd of supplied task."):
    raw = json.dumps(case, ensure_ascii=False, indent=2)
    encounter = prefix + raw + suffix
    return encounter, bind_decision_contract(encounter, case_json=raw,
                                             author="Synthetic contract curator", policy=policy)


def answer(case, *, action=None, transition="HOLD", evidence="old"):
    return json.dumps({"case_id": case["case_id"], "action": action or case["recorded_prior_action"],
                       "transition": transition, "evidence_ids": [evidence]}, indent=2)


class DecisionContractTests(unittest.TestCase):
    def test_complete_declared_support_preserves_raw_response_and_claim_limits(self):
        case, policy = fixture()
        original_case, original_policy = deepcopy(case), deepcopy(policy)
        encounter, contract = bind(case, policy)
        raw = " \n" + answer(case) + "\n "
        reviewed = review_decision(encounter, raw, contract)
        self.assertEqual(reviewed["status"], "PASS")
        self.assertTrue(reviewed["structural_consistency"])
        self.assertTrue(reviewed["condition_action_support"])
        self.assertEqual(reviewed["raw_response"], raw)
        self.assertEqual(reviewed["response_sha256"], hashlib.sha256(raw.encode()).hexdigest())
        self.assertEqual(reviewed["derived_decision"], json.loads(raw))
        self.assertEqual(reviewed["policy_attribution"], "DECLARED_INTERPRETATION")
        self.assertEqual(reviewed["authority"], "NONE")
        self.assertEqual(reviewed["semantic_learning"], "NOT_ESTABLISHED")
        self.assertEqual((case, policy), (original_case, original_policy))
        self.assertEqual(reviewed, review_decision(encounter, raw, json.loads(json.dumps(contract))))

    def test_exposed_cr01_failure_shapes_are_rejected_without_host_keys(self):
        # Authored reproduction of the already exposed request, not an imported
        # scorer/key or a model call. The rule/condition interpretation is explicit.
        case = {
            "case_id": "CR01", "target": "Cedar-64", "recorded_prior_action": "FIRM",
            "rules": ["For the named target: excessive pressure means SOFTEN; insufficient firmness means FIRM."],
            "observations": [
                {"id": "S18", "order": 1, "target": "Cedar-64", "verified": True,
                 "text": "A verified examination records insufficient firmness in Cedar-64."},
                {"id": "S84", "order": 2, "target": "Willow-91", "verified": True,
                 "text": "A subsequent verified examination records excessive pressure in Willow-91."}],
            "allowed_actions": ["FIRM", "SOFTEN"],
        }
        encounter, contract = bind(case, policy_for(case, ["α", "β"]))
        control = review_decision(encounter, answer(case, transition="CHANGE", evidence="S84"), contract)
        treatment = review_decision(encounter, answer(case, action="CHANGE", evidence="S84"), contract)
        self.assertEqual((control["status"], treatment["status"]), ("REJECTED", "REJECTED"))
        self.assertIn("TRANSITION_INCONSISTENT", control["reason_codes"])
        self.assertIn("EVIDENCE_TARGET_MISMATCH", control["reason_codes"])
        self.assertIn("ACTION_NOT_ALLOWED", treatment["reason_codes"])
        self.assertIn("EVIDENCE_TARGET_MISMATCH", treatment["reason_codes"])
        self.assertEqual(control["derived_decision"], {
            "case_id": "CR01", "action": "FIRM", "transition": "HOLD", "evidence_ids": ["S18"]})

    def test_no_policy_checks_structure_without_claiming_semantic_pass(self):
        case, _ = fixture()
        encounter, contract = bind(case)
        good = review_decision(encounter, answer(case), contract)
        self.assertEqual(good["status"], "UNRESOLVED")
        self.assertTrue(good["structural_consistency"])
        self.assertIsNone(good["condition_action_support"])
        self.assertIsNone(good["derived_decision"])
        self.assertIn("CONDITION_POLICY_NOT_SUPPLIED", good["reason_codes"])
        bad = review_decision(encounter, answer(case, evidence="other"), contract)
        self.assertEqual(bad["status"], "REJECTED")

    def test_renamed_actions_targets_unicode_and_reordered_observations(self):
        case, _ = fixture()
        case.update(case_id="épreuve-九", target="器-A", recorded_prior_action="向上",
                    allowed_actions=["向上", "向下"], rules=["Opaque declared mapping, no English cue lookup."])
        case["observations"][0].update(target="器-A", text="記録 α")
        case["observations"][1].update(target="器-B", text="記録 β")
        policy = policy_for(case, ["α", "β"])
        case["observations"].reverse()
        encounter, contract = bind(case, policy, prefix="前置き 🌱\n")
        reviewed = review_decision(encounter, answer(case), contract)
        self.assertEqual(reviewed["status"], "PASS")
        start = contract["case_span"]["start_byte"]
        self.assertEqual(start, len("前置き 🌱\n".encode()))
        self.assertEqual(encounter.encode()[start:contract["case_span"]["end_byte"]], contract["case_json"].encode())

    def test_new_same_target_condition_changes_action_transition_and_citation(self):
        case, _ = fixture()
        case["observations"].append({"id": "new", "order": 2, "target": case["target"],
                                      "verified": True, "text": "New explicit condition β."})
        policy = policy_for(case, ["α", "β", "β"])
        encounter, contract = bind(case, policy)
        old = review_decision(encounter, answer(case), contract)
        new = review_decision(encounter, answer(case, action="REDIRECT", transition="CHANGE", evidence="new"), contract)
        self.assertEqual(old["status"], "REJECTED")
        self.assertIn("EVIDENCE_NOT_LATEST", old["reason_codes"])
        self.assertIn("ACTION_CONTRADICTS_DECLARED_CONDITION", old["reason_codes"])
        self.assertEqual(new["status"], "PASS")

    def test_unverified_later_condition_cannot_overturn_verified_target(self):
        case, _ = fixture()
        case["observations"].append({"id": "rumour", "order": 100, "target": case["target"],
                                      "verified": False, "text": "Unverified β report."})
        encounter, contract = bind(case, policy_for(case, ["α", "β", "β"]))
        self.assertEqual(review_decision(encounter, answer(case), contract)["status"], "PASS")
        rejected = review_decision(encounter, answer(case, evidence="rumour"), contract)
        self.assertIn("UNVERIFIED_EVIDENCE", rejected["reason_codes"])
        self.assertEqual(rejected["status"], "REJECTED")

    def test_tied_latest_target_is_unresolved(self):
        case, _ = fixture()
        case["observations"].append({"id": "tied", "order": 1, "target": case["target"],
                                      "verified": True, "text": "Equally ordered β report."})
        encounter, contract = bind(case, policy_for(case, ["α", "β", "β"]))
        reviewed = review_decision(encounter, answer(case), contract)
        self.assertEqual(reviewed["status"], "UNRESOLVED")
        self.assertIsNone(reviewed["derived_decision"])
        self.assertIn("AMBIGUOUS_LATEST_TARGET_OBSERVATION", reviewed["reason_codes"])

    def test_missing_verified_target_evidence_is_unresolved(self):
        case, _ = fixture()
        for observations in ([], [dict(case["observations"][0], verified=False)],
                             [case["observations"][1]]):
            changed = dict(case, observations=observations)
            encounter, contract = bind(changed, policy_for(changed, ["α"] * len(observations)))
            reviewed = review_decision(encounter, answer(changed), contract)
            self.assertEqual(reviewed["status"], "UNRESOLVED")
            self.assertIn("NO_VERIFIED_TARGET_OBSERVATION", reviewed["reason_codes"])
            self.assertIsNone(reviewed["derived_decision"])

    def test_unknown_condition_or_rule_does_not_infer_from_english(self):
        case, policy = fixture()
        for changed in (
            dict(policy, observed_conditions=[]),
            dict(policy, rules=[]),
            dict(policy, observed_conditions=[dict(policy["observed_conditions"][0], condition="unknown")]),
        ):
            with self.subTest(policy=changed):
                encounter, contract = bind(case, changed)
                reviewed = review_decision(encounter, answer(case), contract)
                self.assertEqual(reviewed["status"], "UNRESOLVED")
                self.assertIsNone(reviewed["derived_decision"])

    def test_conflicting_declared_conditions_or_rules_remain_unresolved(self):
        case, policy = fixture()
        conflicting_rule = deepcopy(policy)
        conflicting_rule["rules"].append(dict(policy["rules"][0], action="REDIRECT"))
        conflicting_observation = deepcopy(policy)
        conflicting_observation["observed_conditions"].append(dict(policy["observed_conditions"][0], condition="β"))
        for changed in (conflicting_rule, conflicting_observation):
            encounter, contract = bind(case, changed)
            self.assertEqual(review_decision(encounter, answer(case), contract)["status"], "UNRESOLVED")

    def test_policy_interpretation_is_not_a_claim_of_prose_understanding(self):
        case, policy = fixture()
        # Reversing only the declared interpretation changes the derived action.
        # The checker pins the quoted text; it does not claim the author read it correctly.
        policy["rules"][0]["action"] = "REDIRECT"
        encounter, contract = bind(case, policy)
        reviewed = review_decision(encounter, answer(case, action="REDIRECT", transition="CHANGE"), contract)
        self.assertEqual(reviewed["status"], "PASS")
        self.assertEqual(reviewed["derivation"]["attribution"], "DECLARED_INTERPRETATION")
        self.assertIn("not verified world truth", reviewed["scope"])

    def test_incomplete_provider_never_passes_even_when_output_looks_correct(self):
        case, policy = fixture()
        encounter, contract = bind(case, policy)
        for raw in (answer(case), "{", None):
            reviewed = review_decision(encounter, raw, contract, completed=False)
            self.assertEqual(reviewed["status"], "UNRESOLVED")
            self.assertIn("PROVIDER_INCOMPLETE", reviewed["reason_codes"])

    def test_response_types_fields_duplicates_and_nonfinite_are_rejected(self):
        case, policy = fixture()
        encounter, contract = bind(case, policy)
        good = answer(case)
        bad = ["{", "[]", "null", None, {"action": "KEEP"},
               good.replace('"case_id": "sample"', '"case_id": "sample", "case_id": "sample"'),
               good.replace('"action": "KEEP"', '"action": NaN'),
               good.replace('"action": "KEEP"', '"action": 1e1000'),
               good.replace('"action": "KEEP"', '"action": true'),
               json.dumps(dict(json.loads(good), evidence_ids=["old", "other"])),
               good[:-1] + ', "extra": 1}', "[" * (MAX_DEPTH + 1) + "0" + "]" * (MAX_DEPTH + 1)]
        for raw in bad:
            with self.subTest(raw=raw):
                reviewed = review_decision(encounter, raw, contract)
                self.assertEqual(reviewed["status"], "REJECTED")
                self.assertFalse(reviewed["response_format_valid"])

    def test_wrong_case_unknown_evidence_invalid_action_and_transition(self):
        case, policy = fixture()
        encounter, contract = bind(case, policy)
        base = json.loads(answer(case))
        for key, value, code in (("case_id", "another", "CASE_ID_MISMATCH"),
                                 ("evidence_ids", ["missing"], "UNKNOWN_EVIDENCE_ID"),
                                 ("action", "HOLD", "ACTION_NOT_ALLOWED"),
                                 ("transition", "REDIRECT", "TRANSITION_INCONSISTENT")):
            reviewed = review_decision(encounter, json.dumps(dict(base, **{key: value})), contract)
            self.assertEqual(reviewed["status"], "REJECTED")
            self.assertIn(code, reviewed["reason_codes"])

    def test_case_must_be_one_exact_unique_utf8_span(self):
        case, policy = fixture()
        raw = json.dumps(case, ensure_ascii=False)
        for encounter, selected in (("prefix " + raw, raw.replace("unit-A", "unit-C")),
                                     (raw + "\n" + raw, raw), ("No case", raw)):
            with self.subTest(encounter=encounter):
                with self.assertRaises(ValueError):
                    bind_decision_contract(encounter, case_json=selected, author="test", policy=policy)

    def test_contract_or_encounter_drift_is_not_a_response_failure(self):
        case, policy = fixture()
        encounter, contract = bind(case, policy)
        with self.assertRaises(ValueError):
            review_decision(encounter + " ", answer(case), contract)
        changes = [dict(contract, author="other"), dict(contract, authority="ALLOW"),
                   dict(contract, contract_sha256="0" * 64),
                   dict(contract, case_span=dict(contract["case_span"], start_byte=0)),
                   dict(contract, encounter_bytes=True)]
        for changed in changes:
            with self.assertRaises(ValueError):
                review_decision(encounter, answer(case), changed)

    def test_case_rejects_duplicate_ids_bad_types_nonfinite_and_extra_fields(self):
        case, policy = fixture()
        malformed = []
        duplicate = deepcopy(case)
        duplicate["observations"].append(deepcopy(case["observations"][0]))
        malformed.append(duplicate)
        for field, value in (("order", True), ("order", -1), ("order", float("inf")), ("verified", 1)):
            changed = deepcopy(case)
            changed["observations"][0][field] = value
            malformed.append(changed)
        malformed.extend([dict(case, extra="no"), dict(case, allowed_actions=["KEEP", "KEEP"]),
                          dict(case, recorded_prior_action="unknown")])
        for changed in malformed:
            with self.assertRaises(ValueError):
                bind(changed)
        raw = json.dumps(case).replace('"id": "old"', '"id": "old", "id": "old"')
        with self.assertRaises(ValueError):
            bind_decision_contract(raw, case_json=raw, author="test")

    def test_policy_quotes_and_references_are_exact_not_substrings(self):
        case, policy = fixture()
        for section, field, value in (("rules", "source_quote", "condition α"),
                                      ("observed_conditions", "source_quote", "Recorded condition α"),
                                      ("observed_conditions", "observation_id", "missing"),
                                      ("rules", "action", "unknown")):
            changed = deepcopy(policy)
            changed[section][0][field] = value
            with self.assertRaises(ValueError):
                bind(case, changed)

    def test_byte_size_and_depth_limits_preserve_hash_only_for_oversize_return(self):
        case, policy = fixture()
        encounter, contract = bind(case, policy)
        raw = "x" * (MAX_RESPONSE_BYTES + 1)
        reviewed = review_decision(encounter, raw, contract)
        self.assertEqual(reviewed["status"], "REJECTED")
        self.assertEqual(reviewed["response_retention"], "OVERSIZE_HASH_ONLY")
        self.assertEqual(reviewed["response_sha256"], hashlib.sha256(raw.encode()).hexdigest())
        self.assertIsNone(reviewed["raw_response"])
        nested = []
        for _ in range(MAX_DEPTH + 1):
            nested = [nested]
        with self.assertRaises(ValueError):
            bind(case, nested)
        with self.assertRaises(ValueError):
            bind_decision_contract("[" * 40 + "]" * 40, case_json="[" * 40 + "]" * 40, author="test")


if __name__ == "__main__":
    unittest.main()
