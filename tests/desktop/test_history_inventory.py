"""Native reference history custody. Scripted fixtures only, never model calls."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = Path(os.environ.get('LODESTONE_DESKTOP_TEST_RUNTIME', ROOT / 'desktop/runtime')).resolve()
sys.path.insert(0, str(RUNTIME/'src'))
from lodestone_machinery import history_inventory as h
from lodestone_machinery.records import canonical

spec = importlib.util.spec_from_file_location('history_test_bootstrap', ROOT/'desktop/bootstrap_learning.py')
bootstrap = importlib.util.module_from_spec(spec); spec.loader.exec_module(bootstrap)


def handle(number):
    return 'bezoar:' + format(number, '064x')


def references():
    return [{'run_handle': handle(1), 'outcome_handle': handle(2),
             'review_handles': [handle(3)], 'lesson_handles': [handle(4)]}]


class InventoryTests(unittest.TestCase):
    def test_discovery_stops_on_total_references_before_next_row(self):
        from lodestone_machinery.bezoar_learning import LearningMemory
        memory = Mock()
        memory._catalog.return_value = [handle(1), handle(5)]
        memory.read_prepared.return_value = {'binding': {'nacre_handles': [handle(99)]}}
        memory.outcome_for.return_value = handle(2)
        memory._reviews.return_value = [handle(3)]
        memory._lessons_for.return_value = [{'handle': handle(4)}]
        with self.assertRaisesRegex(ValueError, 'provenance bound'):
            LearningMemory._history_reference_rows(memory, [handle(99)], reference_budget=3)
        self.assertEqual(memory.read_prepared.call_count, 1)

    def test_frozen_set_and_typed_dependencies_are_exact(self):
        rows = references(); frozen = h.freeze_rows(rows)
        self.assertEqual(frozen['reference_count'], 4)
        self.assertEqual(len(h.typed_references(frozen)), 4)
        rows[0]['review_handles'].append(handle(5))
        self.assertEqual(frozen['rows'][0]['review_handles'], [handle(3)])
        current = h.freeze_rows(rows)
        self.assertNotEqual(h.terminal_signature(frozen), h.terminal_signature(current))
        changed = copy.deepcopy(frozen); changed['rows'][0]['outcome_handle'] = handle(8)
        with self.assertRaisesRegex(ValueError, 'digest'): h.checked_inventory(changed)

    def test_duplicate_foreign_and_excess_references_refused(self):
        rows = references(); rows[0]['review_handles'] *= 2
        with self.assertRaises(ValueError): h.freeze_rows(rows)
        rows = references(); rows[0]['run_handle'] = '/tmp/not-an-address'
        with self.assertRaises(ValueError): h.freeze_rows(rows)
        rows = [{'run_handle': handle(i), 'outcome_handle': None,
                 'review_handles': [], 'lesson_handles': []} for i in range(257)]
        with self.assertRaises(ValueError): h.freeze_rows(rows)
        with self.assertRaises(ValueError): h.freeze_rows(references(), reference_budget=3)

    def test_exact_source_relationships_and_chronology_not_just_hashes(self):
        inventory = h.freeze_rows(references())
        run = {'created_at': '2026-01-01T00:00:00+00:00', 'run_id': 'earlier',
               'binding': {'nacre_handles': [handle(99)]}}
        outcome = {'run_handle': handle(1), 'run_id': 'earlier'}
        review = {'outcome_handle': handle(2)}
        seed = {'derivation': {'source_kind': 'review', 'source_handle': handle(3)}}
        kwargs = dict(current_run_handle=handle(10), current_created_at='2026-01-02T00:00:00+00:00',
                      selected_nacres=[handle(99)], read_run=lambda _: run,
                      read_outcome=lambda _: outcome, read_review=lambda _: review, read_seed=lambda _: seed)
        self.assertEqual(h.validate_sources(inventory, **kwargs), inventory)
        for field, value in [('read_review', lambda _: {'outcome_handle': handle(88)}),
                             ('read_seed', lambda _: {'derivation': {'source_kind': 'review', 'source_handle': handle(88)}}),
                             ('selected_nacres', [handle(88)]),
                             ('current_created_at', run['created_at']),
                             ('current_run_handle', handle(1))]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                h.validate_sources(inventory, **{**kwargs, field: value})


class NativeReferenceHistoryTests(unittest.TestCase):
    def test_default_v1_context_matches_prior_release_golden_bytes(self):
        from lodestone_machinery.bezoar_learning import LearningMemory
        from lodestone_nurarihyon import learning
        expected = (ROOT/'tests/desktop/fixtures/reference_history_v1_context.json').read_bytes()
        with tempfile.TemporaryDirectory() as directory:
            initialized = bootstrap.initialize(Path(directory)/'preview', RUNTIME, Path(sys.executable))
            active = json.loads(Path(initialized['active_config']).read_text())
            memory = LearningMemory.from_active(active)
            prepared = learning.prepare(memory, active['seed_handles'], encounter='Golden v1 encounter.',
                purpose='Preserve the exact existing representation.', reader='Fixture; no model',
                run_id='golden-v1')
            self.assertEqual(prepared['context'].encode(), expected)
            result = learning.finish(memory, prepared, response=None, status='ABSTAINED',
                observation={'source': 'Golden byte fixture; no provider or model'})
            self.assertEqual(result['status'], 'ABSTAINED')

    def test_complete_window_content_parity_and_dependency_import_refusals(self):
        from lodestone_machinery.bezoar_learning import LearningMemory
        from lodestone_machinery.loom_unified import UnifiedLoom
        from lodestone_machinery.learning_context import history_window_projection, _reference_history_window_projection
        from lodestone_machinery.yggdrasil_runtime import _selected_paths
        from lodestone_nurarihyon import learning
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            initialized = bootstrap.initialize(root/'preview', RUNTIME, Path(sys.executable))
            active = json.loads(Path(initialized['active_config']).read_text())
            memory = LearningMemory.from_active(active)
            loom = UnifiedLoom(memory, active['seed_handles'])
            actual = loom.run(encounter='Synthetic earlier encounter.', purpose='Keep exact history.',
                reader='Scripted fixture; no model', provider=lambda _: 'Actual scripted response')
            loom.correct(actual['outcome_handle'], correction='Keep the scoped condition.\n\nPreserve this exact correction.',
                author='Fixture reviewer', observation='Scripted evidence only.')
            incomplete = learning.prepare(memory, active['seed_handles'], encounter='Still pending fixture.',
                purpose='Include a genuinely incomplete native row.', reader='No provider')
            imported = bootstrap.import_memory(memory.workspace, root/'new-mode', RUNTIME,
                Path(sys.executable), seed_handles=active['seed_handles'], history_references=True)
            new = LearningMemory.from_active(json.loads(Path(imported['active_config']).read_text()))
            prepared = learning.prepare(new, active['seed_handles'], encounter='Compare native representations.',
                purpose='Only byte and dependency custody.', reader='No model')
            snapshot = json.loads(prepared['context'])
            legacy = {**snapshot, 'schema':'bezoar-learning-context/v1',
                'history':[new._history_row_from_reference(row) for row in snapshot['history']['rows']]}
            expected = history_window_projection(prepared, legacy, 2)
            actual_view = _reference_history_window_projection(new, prepared, snapshot, 2)
            self.assertEqual(actual_view['content'], expected['content'])
            for key in ('native_history_representation', 'native_history_inventory_sha256', 'native_history_reference_count'):
                actual_view['projection'].pop(key)
            self.assertEqual(actual_view, expected)
            self.assertEqual(expected['content']['history'][-1]['status'], 'INCOMPLETE')
            self.assertTrue(expected['content']['history'][0]['lessons'])
            # Yggdrasil closure includes sources absent from the run's bounded direct links.
            selected = _selected_paths(new.workspace, [prepared['run_handle']])
            missing_relative = new.store.path(actual['outcome_handle']).relative_to(new.workspace)
            self.assertIn(str(missing_relative), selected)
            for corruption in ('missing-outcome', 'forged-self-cycle'):
                broken = root/corruption
                shutil.copytree(new.workspace, broken)
                if corruption == 'missing-outcome':
                    (broken/missing_relative).unlink()
                else:
                    # A real content-addressed self-cycle would require a SHA256 fixed point.
                    # An advertised self-reference instead fails exact address validation before traversal.
                    path = broken/new.store.path(prepared['run_handle']).relative_to(new.workspace)
                    record = json.loads(path.read_text())
                    body = json.loads(record['payload']['context'])
                    body['history'] = h.freeze_rows([{'run_handle': prepared['run_handle'],
                        'outcome_handle':None, 'review_handles':[], 'lesson_handles':[]}])
                    record['payload']['context'] = canonical(body).decode()
                    record['payload']['binding']['context_sha256'] = hashlib.sha256(canonical(body)).hexdigest()
                    path.write_bytes(canonical(record))
                before = {str(p.relative_to(broken)):p.read_bytes() for p in broken.glob('organs/*/records/*.json')}
                destination = root/('rejected-'+corruption)
                with self.assertRaises(ValueError):
                    bootstrap.import_memory(broken, destination, RUNTIME, Path(sys.executable),
                        seed_handles=active['seed_handles'], history_references=True)
                self.assertFalse(destination.exists())
                self.assertEqual(before, {str(p.relative_to(broken)):p.read_bytes() for p in broken.glob('organs/*/records/*.json')})
            for native, pending in ((memory, incomplete), (new, prepared), (new, new.read_prepared(incomplete['run_handle']))):
                learning.finish(native, pending, response=None, status='ABSTAINED',
                    observation={'source':'Custody fixture completed without a model'})
            self.assertEqual(memory.pending(), []); self.assertEqual(new.pending(), [])

    def test_large_history_is_preserved_windowed_and_invalidated_by_new_review(self):
        from lodestone_machinery.bezoar_learning import LearningMemory
        from lodestone_machinery.loom_unified import UnifiedLoom
        from lodestone_machinery.learning_context import build_adapter_request, expand_learning_projection
        from lodestone_machinery.yggdrasil_learning import validate_learning_records, learning_refs
        from lodestone_nurarihyon import learning
        from lodestone_nurarihyon.learning_conscience import assess
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            initialized = bootstrap.initialize(root/'preview', RUNTIME, Path(sys.executable))
            active = json.loads(Path(initialized['active_config']).read_text())
            memory = LearningMemory.from_active(active); loom = UnifiedLoom(memory, active['seed_handles'])
            returns = []
            for number in range(3):
                actual = loom.run(encounter='Scripted retained observation ' + str(number),
                    purpose='Source custody fixture.', reader='Scripted provider; no model', context='',
                    provider=lambda packet: {'status':'COMPLETED', 'response':'x' * 47000,
                        'observation': {'source':'Scripted fixture only', 'model_calls':0}})
                self.assertEqual(actual['status'], 'COMPLETED'); returns.append(actual)
                loom.correct(actual['outcome_handle'], correction='Scoped fixture correction ' + str(number),
                             author='Fixture reviewer', observation='Actual scripted result; no semantic learning claim.')
            original = {str(p.relative_to(memory.workspace)): hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in memory.workspace.glob('organs/*/records/*.json')}
            old_contexts = {r['run_handle']: memory.read_prepared(r['run_handle'])['context'] for r in returns}
            with learning.locked(memory), self.assertRaisesRegex(ValueError, 'exceeds bound'):
                memory.prepare(active['seed_handles'], encounter='Next fixture.', purpose='Source custody fixture.', reader='No provider requested')
            imported = bootstrap.import_memory(memory.workspace, root/'isolated-v2', RUNTIME,
                Path(sys.executable), seed_handles=active['seed_handles'], history_references=True)
            config = json.loads(Path(imported['active_config']).read_text())
            self.assertEqual(config['schema'], 'lodestone-learning-active/v2')
            new = LearningMemory.from_active(config)
            before = {str(p.relative_to(new.workspace)): hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in new.workspace.glob('organs/*/records/*.json')}
            self.assertEqual(before, original)
            prepared = learning.prepare(new, config['seed_handles'], encounter='Next fixture.',
                purpose='Source custody fixture.', reader='No model; bounded preparation inspection')
            snapshot = json.loads(prepared['context'])
            self.assertEqual(snapshot['schema'], 'bezoar-learning-context/v2')
            self.assertEqual(snapshot['history']['row_count'], 3)
            self.assertLess(len(prepared['context'].encode()), 131072)
            with self.assertRaisesRegex(ValueError, 'explicit reader window'):
                build_adapter_request(new, prepared, request_id=prepared['run_id'], context='',
                    encounter=prepared['binding']['encounter'], purpose=prepared['binding']['purpose'])
            packet = build_adapter_request(new, prepared, request_id=prepared['run_id'], context='',
                encounter=prepared['binding']['encounter'], purpose=prepared['binding']['purpose'], history_window=1)
            visible = expand_learning_projection(json.loads(packet['context'])['learning_context'])
            self.assertEqual(len(visible['content']['history']), 1)
            self.assertEqual(len(visible['projection']['departed_history_runs']), 2)
            self.assertEqual(visible['projection']['native_history_inventory_sha256'], snapshot['history']['inventory_sha256'])
            self.assertEqual(visible['content']['history'][0]['reviews'][0]['review']['review']['correction'], 'Scoped fixture correction 2')
            admitted = assess(new, prepared, packet)
            self.assertEqual(admitted['verdict'], 'ALLOW', admitted)
            retained_admission = learning.assess_prepared(new, prepared, packet)
            self.assertEqual(new.store.read(retained_admission['assessment_handle'])['kind'], 'learning-admission')
            self.assertEqual(assess(new, prepared, {'learning_context': prepared})['verdict'], 'REFUSE')
            embedded = build_adapter_request(new, prepared, request_id=prepared['run_id'],
                context=canonical(prepared).decode(), encounter=prepared['binding']['encounter'],
                purpose=prepared['binding']['purpose'], history_window=1)
            embedded_admission = assess(new, prepared, embedded)
            self.assertEqual(embedded_admission['verdict'], 'ALLOW', embedded_admission)
            self.assertEqual(embedded_admission['context_delivery'], 'RULE_DEPARTED_NATIVE_READER_PROJECTION')
            for mutation in ('window', 'inventory-digest', 'correction'):
                changed = copy.deepcopy(packet); body = json.loads(changed['context'])
                if mutation == 'window':
                    body['reader_history_window']['keep_latest_runs'] = 2
                else:
                    view = expand_learning_projection(body['learning_context'])
                    if mutation == 'inventory-digest':
                        view['projection']['native_history_inventory_sha256'] = '0' * 64
                    else:
                        view['content']['history'][0]['reviews'][0]['review']['review']['correction'] = 'Foreign advice'
                    body['learning_context'] = view
                changed['context'] = canonical(body).decode()
                changed['context_sha256'] = hashlib.sha256(changed['context'].encode()).hexdigest()
                with self.subTest(mutation=mutation):
                    self.assertEqual(assess(new, prepared, changed)['verdict'], 'REFUSE')
            with learning.locked(new):
                new.validate_for_dispatch(prepared)
            for run, context in old_contexts.items(): self.assertEqual(new.read_prepared(run)['context'], context)
            refs = learning_refs(new.store.read(prepared['run_handle']))
            self.assertIn(('learning_prior_outcome', returns[0]['outcome_handle']), refs)
            # Later feedback cannot rewrite the frozen snapshot, but must invalidate dispatch.
            UnifiedLoom(new, config['seed_handles']).correct(returns[0]['outcome_handle'],
                correction='A later explicit correction.', author='Later fixture reviewer', observation='Only actual scripted history.')
            self.assertEqual(new.read_prepared(prepared['run_handle']), prepared)
            with learning.locked(new), self.assertRaisesRegex(ValueError, 'newer actual outcome or correction'):
                new.validate_for_dispatch(prepared)
            terminal = learning.finish(new, prepared, response=None, status='ABSTAINED',
                observation={'source':'Actual fixture inspection ended without a provider', 'model_calls':0})
            self.assertEqual(terminal['status'], 'ABSTAINED')
            UnifiedLoom(new, config['seed_handles']).correct(terminal['outcome_handle'],
                correction='Preparation custody inspected; no model decision tested.',
                author='Operational fixture reviewer', observation='No provider dispatched.')
            validate_learning_records(new.store)
            with learning.locked(new): self.assertEqual(learning.status(new)['pending'], [])
            after = {str(p.relative_to(new.workspace)): hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in new.workspace.glob('organs/*/records/*.json')}
            self.assertTrue(all(after.get(k) == v for k,v in before.items()))
            self.assertEqual(original, {str(p.relative_to(memory.workspace)): hashlib.sha256(p.read_bytes()).hexdigest()
                                        for p in memory.workspace.glob('organs/*/records/*.json')})

    @unittest.skipUnless(os.environ.get('LODESTONE_OLD_RUNTIME'), 'Explicit prior release needed for real rollback compatibility check')
    def test_actual_old_runtime_refuses_import_and_restores_its_old_workspace(self):
        old = Path(os.environ['LODESTONE_OLD_RUNTIME']).resolve()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            old_spec = importlib.util.spec_from_file_location('prior_history_bootstrap', old.parent/'bootstrap_learning.py')
            old_bootstrap = importlib.util.module_from_spec(old_spec); old_spec.loader.exec_module(old_bootstrap)
            initialized = old_bootstrap.initialize(root/'preview', old, Path(sys.executable))
            active = json.loads(Path(initialized['active_config']).read_text())
            before = {str(p.relative_to(root/'preview')):p.read_bytes() for p in (root/'preview').rglob('*') if p.is_file()}
            imported = bootstrap.import_memory(Path(active['memory_workspace']), root/'v2', RUNTIME,
                Path(sys.executable), seed_handles=active['seed_handles'], history_references=True)
            script = """
import sys,json
sys.path.insert(0,sys.argv[1])
from lodestone_machinery.loom_runtime import _native
try: _native({'learning_config':sys.argv[2]})
except ValueError as error: print(str(error))
else: raise SystemExit('Old runtime unexpectedly accepted new configuration')
restored = _native({'learning_config':sys.argv[3]})
assert str(restored.memory.workspace) == sys.argv[4]
assert restored.memory.pending() == []
print('PAIRED_OLD_RUNTIME_AND_WORKSPACE_RESTORED')
"""
            result = subprocess.run([sys.executable,'-I','-B','-c',script,str(old/'src'),imported['active_config'],
                initialized['active_config'], active['memory_workspace']],
                capture_output=True,text=True,timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('configuration', result.stdout.lower())
            self.assertIn('PAIRED_OLD_RUNTIME_AND_WORKSPACE_RESTORED', result.stdout)
            self.assertEqual(before, {str(p.relative_to(root/'preview')):p.read_bytes() for p in (root/'preview').rglob('*') if p.is_file()})


if __name__ == '__main__': unittest.main()
