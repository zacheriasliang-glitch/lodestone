"""Real native decision-review retention with scripted providers; no models."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
RUNTIME = Path(os.environ.get("LODESTONE_DESKTOP_TEST_RUNTIME", ROOT / "desktop/runtime")).resolve()
spec = importlib.util.spec_from_file_location("decision_test_bootstrap", ROOT / "desktop/bootstrap_learning.py")
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


PREAMBLE = '''
import hashlib
import json
from pathlib import Path
from lodestone_machinery.bezoar_learning import LearningMemory
from lodestone_machinery import decision_contract
from lodestone_machinery.learning_context import expand_learning_projection
from lodestone_machinery.loom_unified import UnifiedLoom

config = json.loads(Path(sys.argv[2]).read_text())
memory = LearningMemory(config['memory_workspace'])
loom = UnifiedLoom(memory, config['seed_handles'])

def fixture(extra_rule_text=''):
    case = {'case_id':'native-fixture', 'target':'unit-A', 'recorded_prior_action':'KEEP',
        'rules':['Declared condition alpha calls for KEEP; beta calls for REDIRECT.' + extra_rule_text],
        'observations':[
            {'id':'on-target', 'order':1, 'target':'unit-A', 'verified':True, 'text':'Observed alpha for unit-A.'},
            {'id':'other-target', 'order':2, 'target':'unit-B', 'verified':True, 'text':'Observed beta for unit-B.'}],
        'allowed_actions':['KEEP','REDIRECT']}
    policy = {'rules':[
        {'condition':'alpha','action':'KEEP','source_quote':case['rules'][0]},
        {'condition':'beta','action':'REDIRECT','source_quote':case['rules'][0]}],
        'observed_conditions':[
            {'observation_id':row['id'],'condition':condition,'source_quote':row['text']}
            for row,condition in zip(case['observations'], ['alpha','beta'])]}
    case_json = json.dumps(case, ensure_ascii=False)
    encounter = 'Explicit scripted lifecycle fixture.\\n' + case_json
    contract = decision_contract.bind_decision_contract(encounter, case_json=case_json,
        author='Fixture interpretation curator', policy=policy)
    return encounter, contract

def response(transition='CHANGE', evidence='other-target'):
    return ' \\n' + json.dumps({'case_id':'native-fixture','action':'KEEP',
        'transition':transition,'evidence_ids':[evidence]}, indent=2) + '\\n '

def run(encounter, raw, status='COMPLETED'):
    return loom.run(encounter=encounter, purpose='Exercise native decision review retention.',
        reader='Scripted test provider; no model', context='Synthetic fixture, not model evidence.',
        provider=lambda packet: {'status':status,'response':raw,
            'observation':{'source':'scripted test provider','model_calls':0}})

def snapshot():
    return {str(path.relative_to(memory.workspace)):hashlib.sha256(path.read_bytes()).hexdigest()
        for path in Path(memory.workspace).rglob('*') if path.is_file() and not path.is_symlink()}
'''


class DecisionRetentionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.initialized = bootstrap.initialize(self.directory / "preview", RUNTIME, Path(sys.executable))

    def native(self, script):
        code = "import sys\nsys.path.insert(0, sys.argv[1])\n" + PREAMBLE + script
        result = subprocess.run([sys.executable, "-I", "-B", "-c", code,
                                 str(RUNTIME / "src"), self.initialized["active_config"]],
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=90, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_actual_rejected_return_is_preserved_and_review_reaches_next_practice(self):
        actual = self.native('''
encounter, contract = fixture()
raw = response()
first = run(encounter, raw)
original = memory.read_outcome(first['outcome_handle'])
before = snapshot()
checked = loom.review_decision(first['outcome_handle'], contract=contract, author='Native test checker')
after_review = snapshot()
repeated = loom.review_decision(first['outcome_handle'], contract=contract, author='Native test checker')
after_repeat = snapshot()
review = memory.read_review(checked['retention']['review_handle'])
feedback = json.loads(review['review']['correction'])
evidence = memory.read_evidence(checked['retention']['evidence_handle'])
received = []
def following(packet):
    practice = json.loads(packet['context'])
    received.append(expand_learning_projection(practice['learning_context'])['content'])
    return {'status':'COMPLETED','response':'Following scripted return',
            'observation':{'source':'scripted test provider','model_calls':0}}
following_run = loom.run(encounter='Read the prior scoped review.', purpose='Observe actual review delivery.',
    reader='Following scripted fixture; no model', context='', provider=following)
history_row = next(row for row in received[0]['history'] if row.get('outcome_handle') == first['outcome_handle'])
delivered = next(row for row in history_row['reviews'] if row['handle'] == checked['retention']['review_handle'])
next_prepared = memory.read_prepared(following_run['run_handle'])
complete_history = json.loads(next_prepared['context'])['history']
full_row = next(row for row in complete_history if row['outcome_handle'] == first['outcome_handle'])
print(json.dumps({'checked':checked, 'repeated':repeated, 'feedback':feedback,
    'review':review, 'evidence':evidence, 'delivered':delivered,
    'original':original, 'after_outcome':memory.read_outcome(first['outcome_handle']),
    'full_history_original':full_row['outcome'],
    'old_records_preserved':all(after_review.get(name) == digest for name,digest in before.items()),
    'repeat_has_no_writes':after_repeat == after_review,
    'checker_file_sha256':hashlib.sha256(Path(decision_contract.__file__).read_bytes()).hexdigest(),
    'eligible':memory.fold_eligibility(first['outcome_handle'])['eligible'],
    'pending':loom.status()['pending']}))
''')
        checked = actual["checked"]
        self.assertEqual(checked["assessment"]["status"], "REJECTED")
        self.assertEqual(checked["native_status"], "COMPLETED")
        self.assertFalse(checked["provider_called"])
        self.assertEqual(actual["original"], actual["after_outcome"])
        self.assertEqual(actual["original"], actual["full_history_original"])
        self.assertEqual(checked["assessment"]["raw_response"], actual["original"]["response"])
        self.assertEqual(checked["assessment"]["response_sha256"], actual["original"]["response_sha256"])
        self.assertEqual(actual["review"]["review"]["response_sha256"], actual["original"]["response_sha256"])
        self.assertEqual(actual["feedback"]["outcome_handle"], actual["review"]["outcome_handle"])
        self.assertEqual(actual["feedback"]["contract"]["encounter_sha256"], checked["assessment"]["encounter_sha256"])
        self.assertEqual(actual["feedback"]["contract"]["contract_sha256"], checked["assessment"]["contract_sha256"])
        self.assertEqual(actual["feedback"]["checker_sha256"], actual["checker_file_sha256"])
        self.assertEqual(actual["feedback"]["checker_sha256"], checked["checker_sha256"])
        self.assertIn("case_json", actual["feedback"]["contract"])
        self.assertIn("policy", actual["feedback"]["contract"])
        self.assertEqual(actual["feedback"]["assessment"]["status"], "REJECTED")
        self.assertEqual(actual["review"]["review"]["verdict"], "UNRESOLVED")
        self.assertEqual(actual["review"]["authority"], "NONE")
        self.assertEqual(json.loads(actual["delivered"]["review"]["review"]["correction"]), actual["feedback"])
        self.assertEqual(actual["evidence"]["source"], "bezoar-outcome:" + actual["review"]["outcome_handle"])
        self.assertEqual(json.loads(actual["evidence"]["text"])["response_sha256"], actual["original"]["response_sha256"])
        self.assertTrue(actual["old_records_preserved"])
        self.assertTrue(actual["repeat_has_no_writes"])
        self.assertEqual(checked, actual["repeated"])
        self.assertFalse(actual["eligible"])
        self.assertEqual(actual["pending"], [])

    def test_preview_is_read_only_and_reopen_uses_saved_assessment(self):
        actual = self.native('''
encounter, contract = fixture()
first = run(encounter, response())
before = snapshot()
preview = loom.preview_decision_review(first['outcome_handle'],
    case_json=contract['case_json'], policy=contract['policy'], author=contract['author'])
empty = loom.decision_review_context(first['outcome_handle'])
readonly = before == snapshot()
saved = loom.review_decision(first['outcome_handle'], contract=preview['contract'], author='Saved reviewer')
after = snapshot()
def no_regrading(*args, **kwargs):
    raise AssertionError('Reopening must not recompute the saved assessment')
decision_contract.review_decision = no_regrading
reopened = UnifiedLoom(LearningMemory(config['memory_workspace']), config['seed_handles'])
context = reopened.decision_review_context(first['outcome_handle'])
print(json.dumps({'readonly':readonly, 'empty':empty, 'preview':preview,
    'saved':saved, 'context':context, 'reopen_readonly':after == snapshot()}))
''')
        self.assertTrue(actual["readonly"])
        self.assertTrue(actual["reopen_readonly"])
        self.assertEqual(actual["empty"]["reviews"], [])
        self.assertEqual(actual["preview"]["assessment"]["status"], "REJECTED")
        context = actual["context"]
        self.assertEqual(context["response"], actual["preview"]["assessment"]["raw_response"])
        self.assertEqual(context["native_status"], "COMPLETED")
        self.assertEqual(len(context["reviews"]), 1)
        saved = context["reviews"][0]
        self.assertEqual(saved["contract"], actual["preview"]["contract"])
        self.assertEqual(saved["assessment"]["status"], "REJECTED")
        self.assertEqual(saved["review_handle"], actual["saved"]["retention"]["review_handle"])
        self.assertEqual(saved["learning_verdict"], "UNRESOLVED")

    def test_context_skips_valid_reason_only_and_non_json_reviews_without_relaxing_native_validation(self):
        actual = self.native('''
encounter, contract = fixture()
first = run(encounter, response())
ordinary = loom.correct(first['outcome_handle'], correction='Ordinary prose correction.', author='Fixture')
review = memory.read_review(ordinary['review_handle'])['review']
reason_only = dict(review)
reason_only.pop('correction')
reason_only['reason'] = 'Valid native review containing reason and checks without a correction.'
reason_handle = memory.review_outcome(first['outcome_handle'], reason_only)
malformed = loom.correct(first['outcome_handle'], correction='{unclosed JSON-shaped prose', author='Fixture')
assert 'correction' not in memory.read_review(reason_handle)['review']
before = snapshot()
context = loom.decision_review_context(first['outcome_handle'])
readonly = before == snapshot()
invalid = dict(reason_only, correction=None)
try:
    memory.review_outcome(first['outcome_handle'], invalid)
except ValueError:
    nontext_rejected = True
else:
    nontext_rejected = False
print(json.dumps({'context':context, 'readonly':readonly,
    'nontext_rejected':nontext_rejected, 'invalid_review_no_writes':before == snapshot(),
    'reason_only':memory.read_review(reason_handle),
    'malformed_prose':memory.read_review(malformed['review_handle']),
    'original_response':memory.read_outcome(first['outcome_handle'])['response']}))
''')
        self.assertEqual(actual["context"]["reviews"], [])
        self.assertEqual(actual["context"]["response"], actual["original_response"])
        self.assertNotIn("correction", actual["reason_only"]["review"])
        self.assertEqual(actual["malformed_prose"]["review"]["correction"], "{unclosed JSON-shaped prose")
        self.assertTrue(actual["readonly"])
        self.assertTrue(actual["nontext_rejected"])
        self.assertTrue(actual["invalid_review_no_writes"])

    def test_native_preview_cannot_rebind_another_encounter(self):
        actual = self.native('''
encounter, contract = fixture()
first = run(encounter, response(), 'INTERRUPTED')
before = snapshot()
try:
    loom.preview_decision_review(first['outcome_handle'],
        case_json=contract['case_json'].replace('unit-A', 'changed-target'), author='Test')
except ValueError as error:
    reason = str(error)
else:
    raise AssertionError('Foreign case must be rejected')
context = loom.decision_review_context(first['outcome_handle'])
print(json.dumps({'reason':reason, 'readonly':before == snapshot(), 'context':context}))
''')
        self.assertIn("exact span", actual["reason"])
        self.assertTrue(actual["readonly"])
        self.assertEqual(actual["context"]["native_status"], "INTERRUPTED")
        self.assertEqual(actual["context"]["reviews"], [])

    def test_passing_contract_check_stays_an_unresolved_learning_review(self):
        actual = self.native('''
encounter, contract = fixture()
returned = run(encounter, response('HOLD','on-target'))
checked = loom.review_decision(returned['outcome_handle'], contract=contract, author='Native test checker')
review = memory.read_review(checked['retention']['review_handle'])
print(json.dumps({'checked':checked,'review':review,
    'outcome':memory.read_outcome(returned['outcome_handle']),
    'eligibility':memory.fold_eligibility(returned['outcome_handle'])}))
''')
        self.assertEqual(actual["checked"]["assessment"]["status"], "PASS")
        self.assertEqual(actual["outcome"]["status"], "COMPLETED")
        self.assertEqual(actual["review"]["review"]["verdict"], "UNRESOLVED")
        self.assertFalse(actual["eligibility"]["eligible"])
        self.assertEqual(actual["checked"]["semantic_learning"], "NOT_ESTABLISHED")
        self.assertFalse(actual["checked"]["weight_update"])
        self.assertTrue(all(not row["conditions_met"] and not row["pattern_resolved"]
                            for row in actual["review"]["review"]["checks"]))

    def test_interrupted_usable_looking_return_remains_interrupted_and_unresolved(self):
        actual = self.native('''
encounter, contract = fixture()
returned = run(encounter, response('HOLD','on-target'), status='INTERRUPTED')
before = memory.read_outcome(returned['outcome_handle'])
checked = loom.review_decision(returned['outcome_handle'], contract=contract, author='Native test checker')
print(json.dumps({'checked':checked,'before':before,
    'after':memory.read_outcome(returned['outcome_handle']),
    'review':memory.read_review(checked['retention']['review_handle'])}))
''')
        self.assertEqual(actual["checked"]["assessment"]["status"], "UNRESOLVED")
        self.assertIn("PROVIDER_INCOMPLETE", actual["checked"]["assessment"]["reason_codes"])
        self.assertEqual(actual["before"], actual["after"])
        self.assertEqual(actual["after"]["status"], "INTERRUPTED")
        self.assertEqual(actual["review"]["review"]["verdict"], "UNRESOLVED")

    def test_wrong_source_and_oversize_feedback_fail_before_writes(self):
        actual = self.native('''
encounter, contract = fixture()
returned = run(encounter, response())
foreign = decision_contract.bind_decision_contract(encounter + ' Different source.',
    case_json=contract['case_json'], author=contract['author'], policy=contract['policy'])
before = snapshot()
try:
    loom.review_decision(returned['outcome_handle'], contract=foreign, author='Native test checker')
    wrong_source_error = None
except ValueError as exc:
    wrong_source_error = str(exc)
source_unchanged = snapshot() == before
large_encounter, large_contract = fixture(' Scoped source detail.' * 110)
large = run(large_encounter, response())
large_before = snapshot()
try:
    loom.review_decision(large['outcome_handle'], contract=large_contract, author='Native test checker')
    oversize_error = None
except ValueError as exc:
    oversize_error = str(exc)
print(json.dumps({'wrong_source_error':wrong_source_error,'source_unchanged':source_unchanged,
    'oversize_error':oversize_error,'oversize_unchanged':snapshot() == large_before,
    'reviews':memory._catalog('review'), 'large_native_status':large['status'],
    'large_outcome':memory.read_outcome(large['outcome_handle'])}))
''')
        self.assertIn("binding differs", actual["wrong_source_error"])
        self.assertTrue(actual["source_unchanged"])
        self.assertIn("decision feedback", actual["oversize_error"])
        self.assertTrue(actual["oversize_unchanged"])
        self.assertEqual(actual["reviews"], [])
        self.assertEqual(actual["large_native_status"], "COMPLETED")
        self.assertEqual(actual["large_outcome"]["status"], "COMPLETED")

    def test_runtime_bridge_forwards_explicit_review_without_provider(self):
        actual = self.native('''
from unittest.mock import Mock, patch
from lodestone_machinery import loom_runtime
encounter, contract = fixture()
bridge = object.__new__(loom_runtime.LoomBridge)
bridge.config_path = Path('only-explicit-config.json')
native = Mock()
native.review_decision.return_value = {'retention':'fixture'}
with patch.object(loom_runtime, '_config', return_value={'selected':'test'}) as config_mock:
    with patch.object(loom_runtime, '_native', return_value=native) as native_mock:
        returned = bridge.review_decision('bezoar:fixture', contract=contract, author='Checker')
        config_mock.assert_called_once_with(bridge.config_path)
        native_mock.assert_called_once_with({'selected':'test'})
        native.review_decision.assert_called_once_with('bezoar:fixture', contract=contract, author='Checker')
        native.run.assert_not_called()
print(json.dumps(returned))
''')
        self.assertEqual(actual, {"retention": "fixture"})

    def test_configured_cli_reviews_retained_outcome_and_rejects_foreign_contract_without_writes(self):
        retained = self.native('''
encounter, contract = fixture()
returned = run(encounter, response())
foreign = decision_contract.bind_decision_contract(encounter + ' Foreign context.',
    case_json=contract['case_json'], author=contract['author'], policy=contract['policy'])
print(json.dumps({'contract':contract,'foreign':foreign,'outcome_handle':returned['outcome_handle'],
    'outcome':memory.read_outcome(returned['outcome_handle'])}))
''')
        contract_path = self.directory / "contract.json"
        contract_path.write_text(json.dumps(retained["contract"]), encoding="utf-8")
        argv = [sys.executable, "-I", "-B", str(RUNTIME / "tools/machinery/loom-practice"),
                "--config", self.initialized["active_config"], "review-decision",
                retained["outcome_handle"], "--contract", str(contract_path),
                "--author", "Explicit CLI test checker"]
        result = subprocess.run(argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=90, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["schema"], "loom-retained-decision-review/v1")
        self.assertEqual(receipt["assessment"]["status"], "REJECTED")
        self.assertFalse(receipt["provider_called"])
        self.assertEqual(receipt["assessment"]["contract_sha256"], retained["contract"]["contract_sha256"])
        self.assertEqual(receipt["retention"]["outcome_handle"], retained["outcome_handle"])
        observed = self.native('''
outcomes = memory._catalog('outcome')
reviews = memory._catalog('review')
print(json.dumps({'outcomes':outcomes,'outcome':memory.read_outcome(outcomes[0]),
    'reviews':[memory.read_review(handle) for handle in reviews]}))
''')
        self.assertEqual(observed["outcomes"], [retained["outcome_handle"]])
        self.assertEqual(observed["outcome"], retained["outcome"])
        self.assertEqual(len(observed["reviews"]), 1)
        self.assertEqual(observed["reviews"][0]["review"]["verdict"], "UNRESOLVED")
        memory_root = Path(self.initialized["memory_workspace"])
        def saved_files():
            return {str(path.relative_to(memory_root)): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in memory_root.rglob("*") if path.is_file() and not path.is_symlink()}
        for invalid in ("{", json.dumps(retained["foreign"])):
            with self.subTest(contract=invalid[:40]):
                contract_path.write_text(invalid, encoding="utf-8")
                before = saved_files()
                refused = subprocess.run(argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         timeout=90, check=False)
                self.assertEqual(refused.returncode, 2)
                self.assertEqual(refused.stdout, "")
                self.assertEqual(json.loads(refused.stderr)["status"], "ERROR")
                self.assertEqual(saved_files(), before)


if __name__ == "__main__":
    unittest.main()
