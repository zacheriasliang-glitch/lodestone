"""Scripted native machine-finding custody and opt-in delivery; no models."""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_decision_retention as fixtures

SCRIPT = '''
from lodestone_machinery import decision_assessment
from lodestone_machinery.loom_voxels import render_voxel_sections
from unittest.mock import patch
encounter, human_contract = fixture()
contract = decision_contract.bind_decision_contract(encounter,
    case_json=human_contract['case_json'], author='Explicit scripted decoding')
def automatic(raw=None, status='COMPLETED'):
    return loom.run(encounter=encounter, purpose='Scripted structural review.',
        reader='No model; authored fixture', provider=lambda packet: {'status':status,
        'response':response() if raw is None else raw, 'observation':'Scripted fixture'},
        structural_contract=contract)
'''


class MachineDecisionTests(unittest.TestCase):
    setUp = fixtures.DecisionRetentionTests.setUp
    native = fixtures.DecisionRetentionTests.native

    def test_automatic_findings_are_separate_from_human_reviews_and_original_answers(self):
        actual = self.native(SCRIPT + '''
first = automatic()
finding = first['decision_assessment']
original = memory.read_outcome(first['outcome_handle'])
before = snapshot()
repeated = loom.assess_decision(first['outcome_handle'], contract=contract)
after_repeat = snapshot()
with patch.object(decision_contract, 'review_decision', side_effect=AssertionError('No regrading on reopen')):
    reopened = UnifiedLoom(memory, config['seed_handles']).decision_review_context(first['outcome_handle'])
print(json.dumps({'result':first,'same':first['response'] == original['response'] == response(),
    'original_unchanged':original == memory.read_outcome(first['outcome_handle']),
    'idempotent':finding == repeated and before == after_repeat,
    'context':reopened,'eligible':memory.fold_eligibility(first['outcome_handle'])['eligible']}))
''')
        self.assertEqual(actual['result']['status'], 'COMPLETED')
        self.assertEqual(actual['result']['decision_assessment']['assessment']['status'], 'REJECTED')
        self.assertTrue(actual['same'] and actual['original_unchanged'] and actual['idempotent'])
        self.assertEqual(actual['context']['reviews'], [])
        self.assertEqual(len(actual['context']['machine_assessments']), 1)
        self.assertEqual(actual['context']['machine_assessments'][0]['kind'], 'MACHINE_STRUCTURAL_FINDING')
        self.assertFalse(actual['eligible'])

    def test_only_explicit_rejection_selection_reaches_native_voxel_prompt(self):
        actual = self.native(SCRIPT + '''
first = automatic()
handle = first['decision_assessment']['evidence_handle']
packets = []
def provider(packet):
    packets.append(packet)
    return 'Scripted later response'
ordinary = loom.run(encounter='Later encounter', purpose='Observe delivery.',
    reader='Scripted fixture', provider=provider)
voxel = loom.decision_knot_voxel([handle])
selected = loom.run(encounter='Later encounter', purpose='Observe delivery.',
    reader='Scripted fixture', provider=provider, voxel=voxel)
from lodestone_machinery.learning_context import practice_data_candidates
print(json.dumps({'ordinary':ordinary,'selected':selected,'first_packet':packets[0],
    'second_packet':packets[1],'rendered':' '.join(practice_data_candidates(packets[1])),'handle':handle,
    'voxel':voxel,'reviews':memory._reviews(first['outcome_handle'])}))
''')
        self.assertNotIn('decision_assessment', actual['ordinary'])
        self.assertNotIn('voxel', json.loads(actual['first_packet']['context']))
        self.assertNotIn(actual['handle'], json.dumps(actual['first_packet']))
        self.assertEqual(json.loads(actual['second_packet']['context'])['voxel'], actual['voxel'])
        self.assertIn('Named knots from earlier attempts', actual['rendered'])
        self.assertIn('Machine structural finding', actual['rendered'])
        self.assertIn('surprise unknown', actual['rendered'])
        self.assertIn('EVIDENCE_TARGET_MISMATCH', actual['rendered'])
        self.assertEqual(actual['reviews'], [])

    def test_uncertain_and_interrupted_findings_never_become_advice(self):
        actual = self.native(SCRIPT + '''
results=[]
for raw,status in [(response('HOLD','on-target'),'COMPLETED'),(response(),'INTERRUPTED')]:
    result=automatic(raw,status)
    finding=result['decision_assessment']
    try:
        loom.decision_knot_voxel([finding['evidence_handle']])
    except ValueError:
        refused=True
    else:
        refused=False
    results.append({'result':result,'refused':refused,
        'reviews':memory._reviews(result['outcome_handle'])})
print(json.dumps(results))
''')
        for row in actual:
            self.assertEqual(row['result']['decision_assessment']['assessment']['status'], 'UNRESOLVED')
            self.assertEqual(row['result']['decision_assessment']['advice_delivery'], 'NOT_ELIGIBLE')
            self.assertTrue(row['refused'])
            self.assertEqual(row['reviews'], [])

    def test_retention_failure_does_not_erase_or_relabel_actual_outcome(self):
        actual = self.native(SCRIPT + '''
with patch.object(decision_assessment, 'assess', side_effect=OSError('scripted assessment disk failure')):
    result=automatic()
original=memory.read_outcome(result['outcome_handle'])
print(json.dumps({'result':result,'original':original,'pending':loom.status()['pending']}))
''')
        self.assertEqual(actual['result']['status'], 'COMPLETED')
        self.assertEqual(actual['result']['response'], actual['original']['response'])
        self.assertEqual(actual['result']['decision_assessment']['status'], 'ASSESSMENT_UNAVAILABLE')
        self.assertEqual(actual['result']['decision_assessment']['retention'], 'NOT_CONFIRMED')
        self.assertEqual(actual['pending'], [])

    def test_forged_or_changed_reviewer_findings_cannot_enter_voxel_advice(self):
        actual = self.native(SCRIPT + '''
result=automatic()
handle=result['decision_assessment']['evidence_handle']
with patch.object(decision_assessment, '_version', return_value={'decision_contract':'changed'}):
    try:
        loom.decision_knot_voxel([handle])
    except ValueError as exc:
        version_reason=str(exc)
old=memory.read_evidence(handle)
body=json.loads(old['text'])
body['assessment']['selected_evidence_id']='other-target'
from lodestone_machinery.records import canonical
old={k:v for k,v in old.items() if k not in ('schema','authority')}
old['text']=canonical(body).decode()
old['event_id']=hashlib.sha256(old['text'].encode()).hexdigest()
forged=memory.evidence(old)
try:
    loom.decision_knot_voxel([forged])
except ValueError as exc:
    forged_reason=str(exc)
print(json.dumps({'version_reason':version_reason,'forged_reason':forged_reason}))
''')
        self.assertIn('version changed', actual['version_reason'])
        self.assertIn('Only definite', actual['forged_reason'])


if __name__ == '__main__':
    unittest.main()
