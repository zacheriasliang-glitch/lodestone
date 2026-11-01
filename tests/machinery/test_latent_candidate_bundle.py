"""Pinned opaque bundles and synthetic storage boundaries; neural calls forbidden."""
import copy
from contextlib import ExitStack
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from lodestone_machinery.crystals import CrystalStore
from lodestone_machinery.latent import candidate_inputs as ci
from lodestone_machinery.latent import codec
from lodestone_machinery.latent import compact_checkpoint as cp
from lodestone_machinery.latent import compact_layout as layout
from lodestone_machinery.latent import latent_model
from lodestone_machinery.latent import store_adapter as sa

from test_latent_supplied_candidates import fixture_bundle, plan_for


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class CandidateBundleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'synthetic.sqlite'
        self.store = CrystalStore(self.path, index_mode='scan')
        self.addCleanup(self.store.close)
        guards = ExitStack()
        self.addCleanup(guards.close)
        for name in ('initialize', 'validate', 'encode_block', 'decoder_block', 'compile_decoder',
                     'loss_and_gradients', 'apply_gradients'):
            guards.enter_context(mock.patch.object(latent_model, name,
                                 side_effect=AssertionError('neural work forbidden')))
        guards.enter_context(mock.patch.object(sa, 'prepare_model',
                             side_effect=AssertionError('model preparation forbidden')))
        self.model = b'opaque synthetic provenance, deliberately not JSON'
        self.table = bytes(8) + b'\xff' * 8 + bytes(codec.TABLE_BYTES - 16)
        self.bundle = self.load(self.model, self.table)
        self.source = dict(id='invented', data=bytes(8) + b'tail', mandatory=False)

    def load(self, model, table, **pins):
        return sa.load_candidate_bundle(model, table,
            expected_model_sha256=pins.get('expected_model_sha256', sha(model)),
            expected_table_sha256=pins.get('expected_table_sha256', sha(table)))

    def assert_no_writes(self, exception, call):
        self.store.put('sentinel', b'preserved', {}, strategy='literal', report=False)
        before = self.path.read_bytes()
        with mock.patch.object(self.store, 'put', wraps=self.store.put) as put:
            with self.assertRaises(exception):
                call()
            put.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.store.get('sentinel'), b'preserved')

    def test_opaque_loader_pins_bytes_without_authenticating_or_running_model(self):
        self.assertTrue(self.bundle.candidate_only)
        self.assertEqual(self.bundle.preparation_provenance, 'CALLER_PINNED_BYTES_NOT_COMPILE_AUTHENTICATED')
        self.assertEqual(self.bundle.model_bytes, self.model)
        self.assertEqual(self.bundle.table_bytes, self.table)
        self.assertEqual(self.bundle.model_sha256, sha(self.model))
        self.assertEqual(self.bundle.table_sha256, sha(self.table))
        with self.assertRaises(sa.AdapterError):
            self.bundle.encode(bytes(8))
        with self.assertRaises(sa.AdapterError):
            self.bundle._candidate_only = False

    def test_loader_rejects_wrong_pins_types_empty_and_oversized_bytes(self):
        variants = [(b'', self.table, {}), (b'x' * (latent_model.MAX_MODEL_BYTES + 1), self.table, {}),
                    (bytearray(self.model), self.table, {}), (self.model, bytearray(self.table), {}),
                    (self.model, self.table[:-1], {}),
                    (self.model, self.table, dict(expected_model_sha256='0' * 64)),
                    (self.model, self.table, dict(expected_table_sha256='0' * 64)),
                    (self.model, self.table, dict(expected_model_sha256=True)),
                    (self.model, self.table, dict(expected_table_sha256='A' * 64))]
        for model, table, pins in variants:
            with self.subTest(model_type=type(model).__name__, model_bytes=len(model), pins=pins):
                with self.assertRaises(sa.AdapterError):
                    self.load(model, table, **pins)

    def test_candidate_only_default_paths_reject_before_writes_even_empty(self):
        for raw in (b'', b'tail', bytes(8)):
            source = {**self.source, 'data': raw}
            for bundles in ((self.bundle,), (fixture_bundle(), self.bundle)):
                with self.subTest(bytes=len(raw), layers=len(bundles)):
                    self.assert_no_writes(sa.AdapterError, lambda: sa.store_source(
                        self.store, source['id'], raw, mandatory=False, prepared=bundles))
                    self.assert_no_writes(cp.CheckpointError, lambda: cp.build_catalog(
                        self.store, [source], encoding='latent', prepared=bundles))

    def test_non_boolean_candidate_marker_is_rejected_before_writes(self):
        plan = plan_for([self.source], self.table, sa.MAX_SOURCE_BYTES)
        for marker in (1, 0, 'true', None):
            object.__setattr__(self.bundle, '_candidate_only', marker)
            self.assert_no_writes(sa.AdapterError, lambda: sa.store_source(
                self.store, self.source['id'], self.source['data'], mandatory=False,
                prepared=(self.bundle,), supplied_candidates=plan))
            self.assert_no_writes(sa.AdapterError, lambda: cp.build_catalog(
                self.store, [self.source], encoding='latent', prepared=(self.bundle,),
                supplied_candidates=plan))

    def test_numeric_boolean_and_late_invalid_plan_fail_before_writes(self):
        source = {**self.source, 'data': bytes(16)}
        good = plan_for([source], self.table, sa.MAX_SOURCE_BYTES,
                        candidates=[codec.coordinates(0)] * codec.MAX_CANDIDATES)
        edits = [lambda p: p['sources'][0].update(bytes=True),
                 lambda p: p['sources'][0]['chunks'][0].update(offset=False),
                 lambda p: p['sources'][0]['chunks'][0].update(bytes=True),
                 lambda p: p['sources'][0]['chunks'][0]['candidates'][-1].__setitem__(-1, (True, 0, 0)),
                 lambda p: p['sources'][0]['chunks'][0]['candidates'][-1].__setitem__(-1, (8, 0, 0))]
        for edit in edits:
            plan = copy.deepcopy(good)
            edit(plan)
            self.assert_no_writes(sa.AdapterError, lambda: sa.store_source(
                self.store, source['id'], source['data'], mandatory=False,
                prepared=(self.bundle,), supplied_candidates=plan))
            self.assert_no_writes(cp.CheckpointError, lambda: cp.build_catalog(
                self.store, [source], encoding='latent', prepared=(self.bundle,),
                supplied_candidates=plan))

    def test_correctly_bound_mandatory_plan_is_rejected_as_mode_not_mixup(self):
        mandatory = dict(id='mandatory', data=b'explicit required context', mandatory=True)
        plan = plan_for([mandatory], self.table, layout.CHUNK_BYTES)
        # The plan itself is valid, so a failed corpus binding cannot mask the mode guard.
        ci.validate(plan, [mandatory], self.table, layout.CHUNK_BYTES, lambda: None)
        with mock.patch.object(ci, 'validate', side_effect=AssertionError('must reject mode first')):
            self.assert_no_writes(cp.CheckpointError, lambda: cp.build_catalog(
                self.store, [mandatory], encoding='latent', prepared=(self.bundle,), supplied_candidates=plan))

    def test_append_supplied_source_preserves_prior_mandatory_recovery(self):
        required = dict(id='required-control', data=b'preserve this invented condition', mandatory=True)
        previous = cp.build_catalog(self.store, [required])
        plan = plan_for([self.source], self.table, layout.CHUNK_BYTES)
        added = cp.build_catalog(self.store, [self.source], encoding='latent', prepared=(self.bundle,),
                                 previous_pin=previous['directory'], supplied_candidates=plan)
        reader = cp.ReadSession(self.store, added['directory'])
        empty = reader.read(self.source['id'], 0, 0)
        self.assertEqual(empty['data'], b'')
        self.assertEqual(empty['mandatory'], {required['id']: required['data']})
        whole = reader.read(self.source['id'])
        self.assertEqual(whole['data'], self.source['data'])
        self.assertEqual(whole['mandatory'], empty['mandatory'])
        self.assertEqual(added['sources'], 2)
        self.assertEqual(added['encoder_calls'], 0)
        self.assertEqual(added['supplied_candidates']['plan_sha256'], sha(ci._canonical(plan)))

    def test_candidate_order_changes_returned_provenance_not_same_selected_packet(self):
        reports = []
        for codes in ([codec.coordinates(1), codec.coordinates(0)],
                      [codec.coordinates(0), codec.coordinates(1)]):
            plan = plan_for([self.source], self.table, sa.MAX_SOURCE_BYTES, candidates=codes)
            reports.append(sa.store_source(self.store, self.source['id'], self.source['data'], mandatory=False,
                           prepared=(self.bundle,), supplied_candidates=plan))
        self.assertEqual(reports[0]['manifest'], reports[1]['manifest'])
        self.assertNotEqual(reports[0]['supplied_candidates']['plan_sha256'],
                            reports[1]['supplied_candidates']['plan_sha256'])
        for report, rank in zip(reports, (1, 0)):
            selection = report['layer_accounting'][0]['selection']
            self.assertEqual(selection['blocks'][0]['rank'], rank)
            self.assertEqual(selection['candidates_evaluated'], 2)
            self.assertEqual(report['supplied_candidates']['plan_storage'], 'CALLER_OWNED_NOT_RETAINED')

    def test_full_adapter_source_and_rank_cap_charge_duplicates_in_both_paths(self):
        source = dict(id='at-adapter-bound', data=bytes(sa.MAX_SOURCE_BYTES), mandatory=False)
        ranks = [codec.coordinates(0)] * codec.MAX_CANDIDATES
        adapter_plan = plan_for([source], self.table, sa.MAX_SOURCE_BYTES, candidates=ranks)
        stored = sa.store_source(self.store, source['id'], source['data'], mandatory=False,
                                 prepared=(self.bundle,), supplied_candidates=adapter_plan)
        self.assertEqual(stored['bundle_provenance'], 'CALLER_PINNED_BYTES_NOT_COMPILE_AUTHENTICATED')
        selection = stored['layer_accounting'][0]['selection']
        self.assertEqual(selection['candidates_evaluated'], 32768)
        self.assertEqual(selection['nibble_comparisons'] + selection['writer_nibble_comparisons'], 532480)
        self.assertEqual(sa.restore_source(self.store, stored['manifest']['id'],
                         expected_manifest_sha256=stored['manifest']['sha256']), source['data'])
        catalog_plan = plan_for([source], self.table, layout.CHUNK_BYTES, candidates=ranks)
        built = cp.build_catalog(self.store, [source], encoding='latent', prepared=(self.bundle,),
                                 supplied_candidates=catalog_plan)
        chunks = built['supplied_candidates']['chunks']
        self.assertEqual(len(chunks), 4)
        self.assertEqual(sum(c['selection']['candidates_evaluated'] for c in chunks), 32768)
        self.assertTrue(all(c['selection']['nibble_comparisons'] +
                            c['selection']['writer_nibble_comparisons'] == 133120 for c in chunks))
        self.assertEqual(cp.ReadSession(self.store, built['directory']).read(source['id'])['data'], source['data'])


if __name__ == '__main__':
    unittest.main()
