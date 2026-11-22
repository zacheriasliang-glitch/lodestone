"""Native derived-pattern reuse and historical bindings; scripted calls only."""
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
spec = importlib.util.spec_from_file_location("derived_pattern_bootstrap", ROOT / "desktop/bootstrap_learning.py")
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


class DerivedPatternRetentionTests(unittest.TestCase):
    def test_catalogue_discloses_supersession_without_selecting_old_advice(self):
        with tempfile.TemporaryDirectory() as directory:
            initialized = bootstrap.initialize(Path(directory) / "preview", RUNTIME, Path(sys.executable))
            code = r'''
import sys,json,hashlib
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from lodestone_machinery.bezoar_learning import LearningMemory
from lodestone_nurarihyon import learning
config=json.loads(Path(sys.argv[2]).read_text());root=Path(config['memory_workspace'])
memory=LearningMemory(root);old=config['seed_handles'][0]
with learning.locked(memory):
    original=memory.read_seed(old)
    current=memory.seed({'id':'revised-fixture-advice','correction':'Use the corrected fixture condition.',
        'conditions':['Only the revised fixture condition applies.'],'frame_limits':['Declared test revision, no learned benefit.'],
        'evidence_handles':original['evidence_handles'],'author':'Fixture correction author','supersedes':old})
    before={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}
    observed=memory.practice_candidates([old])
    after={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}
print(json.dumps({'old':old,'current':current,'catalogue':observed,'read_only':before==after}))
'''
            result = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(RUNTIME / "src"), initialized["active_config"]],
                                    capture_output=True, text=True, timeout=90)
            self.assertEqual(result.returncode, 0, result.stderr)
            actual = json.loads(result.stdout)
            catalogue = actual["catalogue"]
            self.assertTrue(actual["read_only"])
            self.assertEqual(catalogue["configured_seed_handles"], [actual["old"]])
            self.assertEqual(catalogue["resolved_seed_handles"], [actual["current"]])
            rows = {row["handle"]: row for row in catalogue["candidates"]}
            self.assertFalse(rows[actual["old"]]["ready"])
            self.assertEqual(rows[actual["old"]]["resolved_handle"], actual["current"])
            self.assertIn(actual["current"], rows[actual["old"]]["error"])
            self.assertTrue(rows[actual["current"]]["ready"])
            self.assertEqual(rows[actual["current"]]["resolved_handle"], actual["current"])
            self.assertEqual(rows[actual["current"]]["correction"], "Use the corrected fixture condition.")

    def test_later_derived_pattern_preserves_exact_history_and_rejects_foreign_or_tampered_pairs(self):
        with tempfile.TemporaryDirectory() as directory:
            initialized = bootstrap.initialize(Path(directory) / "preview", RUNTIME, Path(sys.executable))
            code = r'''
import sys, json, hashlib
from copy import deepcopy
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from lodestone_machinery.bezoar_learning import LearningMemory
from lodestone_machinery.loom_unified import UnifiedLoom
from lodestone_machinery.learning_context import build_adapter_request, expand_learning_projection
from lodestone_nurarihyon import learning
config=json.loads(Path(sys.argv[2]).read_text())
root=Path(config['memory_workspace']);memory=LearningMemory(root)
loom=UnifiedLoom(memory,config['seed_handles']);calls=[]
def scripted(packet):
    calls.append(packet['id'])
    return 'Actual unsupported fixture response '+str(len(calls))
outcomes=[];reviews=[]
for number in (1,2):
    outcome=loom.run(encounter='Explicit fixture '+str(number),purpose='Inspect scoped advice.',
                     reader='scripted fixture only',provider=scripted,context='Authored test input.')
    outcomes.append(outcome)
    reviews.append(loom.correct(outcome['outcome_handle'],author='Fixture reviewer',
        correction='Keep the action and its supporting observation together.',
        observation='Scripted fixture omitted required support.',event_id='derived-fixture-'+str(number)))
original=memory.read_prepared(outcomes[0]['run_handle'])
original_context=original['context']
old_pair=json.loads(original_context)['pairs'][0]
old_nacre=old_pair['nacre_handle'];base_seed=old_pair['seed_handle']
derived=next(row['handle'] for row in memory.lessons_for_outcome(outcomes[0]['outcome_handle'])
             if row['record']['derivation']['source_handle']==reviews[0]['review_handle'])
payload={'id':'observed-fixture-pattern','pattern':'Two explicit scripted fixtures omitted support; no model learning claim.',
         'conditions':['Authored fixture practice only; reassess conditions.'],
         'evidence_handles':[row['evidence_handle'] for row in reviews],
         'seed_handles':[derived],'author':'Fixture curator'}
with learning.locked(memory):
    new_nacre=memory.nacre(payload)
    loaded=memory.read_nacre(new_nacre)
    historical=memory.read_prepared(outcomes[0]['run_handle'])
    # Additional valid current teaching must still be discovered, while older
    # prepared snapshots retain their exact original applicability mapping.
    extra=memory.nacre(dict(payload,id='another-current-pattern',seed_handles=[base_seed]))
    current_base=memory._pairs([base_seed],None)
    current_derived=memory._pairs([derived],None)
    immutable_before={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}
    candidates=memory.practice_candidates([base_seed])
    assert immutable_before=={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}
    unpaired=next(row['handle'] for row in memory.lessons_for_outcome(outcomes[1]['outcome_handle'])
                  if row['record']['derivation']['source_handle']==reviews[1]['review_handle'])
    assert historical['context']==original_context
    assert memory.read_prepared(outcomes[0]['run_handle'])['context']==original_context
    pair={k:v for k,v in current_derived[0].items() if k!='status'}
    rejected={}
    for name,selection,override in [
        ('derived_with_unrelated_pattern',[derived],dict(pair,nacre_handle=old_nacre)),
        ('historical_with_foreign_pattern',[base_seed],dict(pair,seed_handle=base_seed)),
        ('altered_conditions',[derived],dict(pair,conditions=['Different condition']))]:
        try: memory._pairs(selection,[override])
        except ValueError as error: rejected[name]=str(error)
        else: raise AssertionError('Invalid pair accepted: '+name)
    path=root/'organs/bezoar/records'/(new_nacre.split(':')[1]+'.json')
    raw=path.read_bytes();record=json.loads(raw);record['payload']['pattern']='Tampered content at original address'
    path.write_text(json.dumps(record))
    try:
        try: memory._pairs([derived],[pair])
        except ValueError as error: rejected['tampered_referenced_pattern']=str(error)
        else: raise AssertionError('Tampered referenced pattern accepted')
        try: memory.practice_candidates([base_seed])
        except ValueError as error: rejected['catalogue_fails_closed_on_tamper']=str(error)
        else: raise AssertionError('Catalogue silently omitted malformed pattern')
    finally: path.write_bytes(raw)
    assert memory.read_nacre(new_nacre)==loaded
prepared=learning.prepare(memory,[derived],encounter='Candidate-only fixture preparation',
                           purpose='Inspect actual retained delivery.',reader='No model fixture')
packet=build_adapter_request(memory,prepared,request_id=prepared['run_id'],context='No provider requested.',
                             encounter=prepared['binding']['encounter'],purpose=prepared['binding']['purpose'])
admission=learning.assess_prepared(memory,prepared,packet)
visible=expand_learning_projection(json.loads(packet['context'])['learning_context'])['content']
terminal=learning.finish(memory,prepared,response='',status='ABSTAINED',
                         observation={'source':'Actual fixture preparation only','provider_called':False})
print(json.dumps({'scripted_callbacks':len(calls),'native_nacre':loaded,'historical_context_unchanged':historical['context']==original_context,
  'fresh_base_patterns':sorted(p['nacre_handle'] for p in current_base),'expected_base_patterns':sorted([old_nacre,extra]),
  'fresh_derived_patterns':[p['nacre_handle'] for p in current_derived],'expected_derived_pattern':new_nacre,
  'rejected':rejected,'delivered_seed_handles':[s['handle'] for s in visible['seeds']],
  'expected_seed':derived,'delivered_correction':visible['seeds'][0]['record']['correction'],
  'candidates':candidates,'configured_seed':base_seed,'unpaired_seed':unpaired,
  'admission':admission['verdict'],'terminal_status':terminal['status'],'pending':learning.status(memory)['pending'],
  'retained_review_verdict':memory.read_review(reviews[0]['review_handle'])['review']['verdict']}))
'''
            actual = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(RUNTIME / "src"), initialized["active_config"]],
                                    capture_output=True, text=True, timeout=120)
            self.assertEqual(actual.returncode, 0, actual.stderr)
            result = json.loads(actual.stdout)
            self.assertEqual(result["scripted_callbacks"], 2)
            self.assertTrue(result["historical_context_unchanged"])
            self.assertEqual(result["fresh_base_patterns"], result["expected_base_patterns"])
            self.assertEqual(result["fresh_derived_patterns"], [result["expected_derived_pattern"]])
            self.assertEqual(set(result["rejected"]), {"derived_with_unrelated_pattern", "historical_with_foreign_pattern",
                                                       "altered_conditions", "tampered_referenced_pattern", "catalogue_fails_closed_on_tamper"})
            catalogue = result["candidates"]
            self.assertEqual(catalogue["configured_seed_handles"], [result["configured_seed"]])
            self.assertEqual(catalogue["resolved_seed_handles"], [result["configured_seed"]])
            by_handle = {row["handle"]: row for row in catalogue["candidates"]}
            self.assertTrue(by_handle[result["expected_seed"]]["ready"])
            self.assertFalse(by_handle[result["unpaired_seed"]]["ready"])
            self.assertIn("matching retained NACRE", by_handle[result["unpaired_seed"]]["error"])
            self.assertEqual(catalogue["authority"], "NONE")
            self.assertFalse(catalogue["provider_called"])
            self.assertEqual(result["delivered_seed_handles"], [result["expected_seed"]])
            self.assertEqual(result["delivered_correction"], "Keep the action and its supporting observation together.")
            self.assertEqual(result["admission"], "ALLOW")
            self.assertEqual(result["terminal_status"], "ABSTAINED")
            self.assertEqual(result["pending"], [])
            self.assertEqual(result["retained_review_verdict"], "UNRESOLVED")


if __name__ == "__main__":
    unittest.main()
