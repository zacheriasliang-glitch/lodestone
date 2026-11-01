"""Synthetic prebuilt byte bundles only: no model preparation or execution."""
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


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def fixture_bundle():
    # Deliberately not a real model or preparation API. These bytes are charged
    # as a synthetic artifact; the existing integrity checker pins bytes only.
    bundle = object.__new__(sa.PreparedModel)
    model = b'synthetic-engineering-model-placeholder'
    table = bytes(8) + b'\xff' * 8 + bytes(codec.TABLE_BYTES - 16)
    for name, value in (('_model_bytes', model), ('_table_bytes', table),
                        ('_model_sha256', sha(model)), ('_table_sha256', sha(table)),
                        ('_preparation_wall_seconds', 0)):
        object.__setattr__(bundle, name, value)
    return bundle


def plan_for(sources, table, chunk_bytes, candidates=None):
    """Bind caller-supplied lists; this fixture does no ranking or search."""
    if candidates is None:
        candidates = [codec.coordinates(0)]
    result = dict(schema=ci.SCHEMA, corpus_sha256=ci.corpus_sha256(sources),
                  table_sha256=sha(table), sources=[])
    for source in sources:
        raw = source['data']
        chunks = []
        for offset in range(0, max(1, len(raw)), chunk_bytes):
            body = raw[offset:offset + chunk_bytes]
            chunks.append(dict(offset=offset, bytes=len(body), sha256=sha(body),
                               candidates=[copy.deepcopy(candidates) for _ in range(len(body) // 8)]))
        result['sources'].append(dict(id=source['id'], sha256=sha(raw), bytes=len(raw), chunks=chunks))
    return result


class SuppliedCandidatesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'synthetic.sqlite'
        self.store = CrystalStore(self.path, index_mode='scan')
        self.addCleanup(self.store.close)
        self.bundle = fixture_bundle()
        self.source = dict(id='source-a', data=bytes(8) + b'\x11' * 4 + bytes(4) + b'\x11' * 4 + b'\x10' + bytes(3) + b'tail', mandatory=False)
        self.guards = ExitStack()
        self.addCleanup(self.guards.close)
        for module, name in ((sa.PreparedModel, 'encode'), (sa, 'prepare_model'),
                             (latent_model, 'encode_block'), (latent_model, 'compile_decoder'),
                             (latent_model, 'initialize')):
            self.guards.enter_context(mock.patch.object(module, name, side_effect=AssertionError('model execution forbidden')))

    def store_call(self, plan, source=None):
        source = self.source if source is None else source
        return sa.store_source(self.store, source['id'], source['data'], mandatory=source['mandatory'],
                               prepared=(self.bundle,), supplied_candidates=plan)

    def catalog_call(self, plan, sources=None):
        return cp.build_catalog(self.store, [self.source] if sources is None else sources,
                                encoding='latent', prepared=(self.bundle,), supplied_candidates=plan)

    def assert_no_mutation(self, call):
        self.store.put('sentinel', b'preserve', {}, strategy='literal', report=False)
        before = self.path.read_bytes()
        before_inspect = self.store.inspect()
        with mock.patch.object(self.store, 'put', wraps=self.store.put) as put:
            with self.assertRaises(ValueError):
                call()
            put.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.store.inspect(), before_inspect)
        self.assertEqual(self.store.get('sentinel'), b'preserve')

    def test_store_and_catalog_end_to_end_accounting_and_original_recovery(self):
        for mode, chunk_bytes in (('store', sa.MAX_SOURCE_BYTES), ('catalog', layout.CHUNK_BYTES)):
            plan = plan_for([self.source], self.bundle.table_bytes, chunk_bytes)
            result = self.store_call(plan) if mode == 'store' else self.catalog_call(plan)
            if mode == 'store':
                pin = result['manifest']
                recovered = sa.restore_source(self.store, pin['id'], expected_manifest_sha256=pin['sha256'])
                accounting = result['layer_accounting'][0]
                selection = accounting['selection']
                self.assertEqual(accounting['encoder_calls'], 0)
                costs = result['logical_artifact_bytes_by_kind']
                self.assertEqual(sum(costs.values()), result['logical_artifact_bytes_total'])
                self.assertEqual(costs['packet'], accounting['packet_bytes'])
            else:
                recovered = cp.ReadSession(self.store, result['directory']).read(self.source['id'])['data']
                chunk = result['supplied_candidates']['chunks'][0]
                accounting, selection = chunk['accounting'], chunk['selection']
                self.assertEqual(result['encoder_calls'], 0)
                costs = result['artifact_bytes_by_kind']
                self.assertEqual(sum(costs.values()), result['artifact_bytes'])
                self.assertEqual(costs['packet'], accounting['packet_bytes'])
            self.assertEqual(recovered, self.source['data'])
            self.assertEqual(costs['table'], codec.TABLE_BYTES)
            self.assertEqual(costs['model'], len(self.bundle.model_bytes))
            self.assertEqual(accounting['payload_bits'], 29 + 61 + 65 + 32)
            self.assertEqual(selection['candidates_evaluated'], 3)
            self.assertEqual(selection['nibble_comparisons'], 48)
            self.assertEqual(selection['writer_nibble_comparisons'], 32)
            self.assertEqual(result['supplied_candidates']['plan_bytes'], len(ci._canonical(plan)))
            self.assertEqual(result['supplied_candidates']['plan_sha256'], sha(ci._canonical(plan)))
            self.assertEqual(len(self.source['data']), 28)

    def test_later_supplied_candidate_improves_both_callers(self):
        codes = [codec.coordinates(1), codec.coordinates(0), codec.coordinates(0)]
        baseline = codec.fold(self.source['data'], [codes[0]] * 3, self.bundle.table_bytes)
        self.assertEqual(baseline['accounting']['payload_bits'], 227)
        self.assertEqual(baseline['accounting']['packet_bytes'], 103)
        for call, width in ((self.store_call, sa.MAX_SOURCE_BYTES), (self.catalog_call, layout.CHUNK_BYTES)):
            plan = plan_for([self.source], self.bundle.table_bytes, width, candidates=codes)
            result = call(plan)
            if width == sa.MAX_SOURCE_BYTES:
                accounting = result['layer_accounting'][0]
                selection = accounting['selection']
            else:
                chunk = result['supplied_candidates']['chunks'][0]
                accounting, selection = chunk['accounting'], chunk['selection']
            self.assertEqual(accounting['packet_bytes'], 98)
            self.assertEqual(selection['candidates_evaluated'], 9)
            self.assertEqual(selection['nibble_comparisons'], 144)
            self.assertEqual([b['rank'] for b in selection['blocks']], [1, 1, None])

    def test_empty_input_and_empty_rankings_and_multichunk(self):
        for raw in (b'', b'tail', bytes(1033)):
            source = dict(id='empty-' + str(len(raw)), data=raw, mandatory=False)
            for call, width in ((self.store_call, sa.MAX_SOURCE_BYTES), (self.catalog_call, layout.CHUNK_BYTES)):
                plan = plan_for([source], self.bundle.table_bytes, width, candidates=[])
                if width == sa.MAX_SOURCE_BYTES:
                    result = call(plan, source)
                    recovered = sa.restore_source(self.store, result['manifest']['id'], expected_manifest_sha256=result['manifest']['sha256'])
                    self.assertEqual(result['layer_accounting'][0]['learned_blocks'], 0)
                else:
                    result = call(plan, [source])
                    recovered = cp.ReadSession(self.store, result['directory']).read(source['id'])['data']
                    self.assertTrue(all(c['accounting']['learned_blocks'] == 0 for c in result['supplied_candidates']['chunks']))
                self.assertEqual(recovered, raw)

    def test_binding_and_last_candidate_errors_do_not_write(self):
        sources = [dict(id='one', data=bytes(1032), mandatory=False),
                   dict(id='two', data=b'\x11' * 1032, mandatory=False)]
        good = plan_for(sources, self.bundle.table_bytes, layout.CHUNK_BYTES)
        variants = []
        def changed(edit):
            p = copy.deepcopy(good)
            edit(p)
            variants.append(p)
        changed(lambda p: p.update(corpus_sha256='0' * 64))
        changed(lambda p: p.update(table_sha256='0' * 64))
        changed(lambda p: p['sources'].reverse())
        changed(lambda p: p['sources'].pop())
        changed(lambda p: p['sources'][-1].update(id='one'))
        changed(lambda p: p['sources'][-1].update(sha256='0' * 64))
        changed(lambda p: p['sources'][-1].update(bytes=True))
        changed(lambda p: p['sources'][-1]['chunks'].reverse())
        changed(lambda p: p['sources'][-1]['chunks'][-1].update(offset=0))
        changed(lambda p: p['sources'][-1]['chunks'][-1].update(sha256='0' * 64))
        changed(lambda p: p['sources'][-1]['chunks'][-1].update(bytes=7))
        changed(lambda p: p['sources'][-1]['chunks'].pop())
        changed(lambda p: p['sources'][-1]['chunks'][-1].update(candidates=[]))
        changed(lambda p: p['sources'][-1]['chunks'][-1].update(candidates=[[(True, 0, 0)]]))
        changed(lambda p: p['sources'][-1]['chunks'][-1].update(candidates=[[(0, 0, 0)] * 65]))
        for plan in variants:
            with self.subTest(plan=variants.index(plan)):
                self.assert_no_mutation(lambda: self.catalog_call(plan, sources))
        adapter_plan = plan_for([self.source], self.bundle.table_bytes, sa.MAX_SOURCE_BYTES)
        adapter_plan['sources'][0]['chunks'][0]['candidates'][-1] = [(8, 0, 0)]
        self.assert_no_mutation(lambda: self.store_call(adapter_plan))
        wrong_source = {**self.source, 'id': 'other'}
        adapter_plan = plan_for([self.source], self.bundle.table_bytes, sa.MAX_SOURCE_BYTES)
        self.assert_no_mutation(lambda: self.store_call(adapter_plan, wrong_source))

    def test_changed_corpus_and_chunk_interchange_rejected(self):
        sources = [dict(id='one', data=bytes(2048), mandatory=False),
                   dict(id='two', data=b'\x11' * 8, mandatory=False)]
        plan = plan_for(sources, self.bundle.table_bytes, layout.CHUNK_BYTES)
        changed = copy.deepcopy(sources)
        changed[-1]['data'] = b'\x22' * 8
        self.assert_no_mutation(lambda: self.catalog_call(plan, changed))
        # Equal-byte chunks still cannot exchange their ordinal-bound records.
        plan['sources'][0]['chunks'].reverse()
        self.assert_no_mutation(lambda: self.catalog_call(plan, sources))

    def test_incompatible_modes_rejected_before_writes(self):
        plan = plan_for([self.source], self.bundle.table_bytes, layout.CHUNK_BYTES)
        for options in (dict(encoding='literal'), dict(encoding='lifting'),
                        dict(encoding='latent', prepared=(self.bundle,) * 2),
                        dict(encoding='latent', prepared=(self.bundle,), fixed_code=True)):
            self.assert_no_mutation(lambda: cp.build_catalog(self.store, [self.source], supplied_candidates=plan, **options))
        for mandatory, prepared in ((True, ()), (False, (self.bundle,) * 2)):
            self.assert_no_mutation(lambda: sa.store_source(self.store, 'source-a', b'', mandatory=mandatory,
                                                           prepared=prepared, supplied_candidates=plan))
        mandatory_source = {**self.source, 'mandatory': True}
        self.assert_no_mutation(lambda: self.catalog_call(plan, [mandatory_source]))

    def test_validation_preserves_deadline_exception_and_no_writes(self):
        for module, call, width, error in ((sa, self.store_call, sa.MAX_SOURCE_BYTES, sa.AdapterTimeout),
                                          (cp, self.catalog_call, layout.CHUNK_BYTES, cp.CheckpointTimeout)):
            plan = plan_for([self.source], self.bundle.table_bytes, width)
            before = self.path.read_bytes()
            with mock.patch.object(module._Deadline, 'check', side_effect=error('deadline')), \
                    mock.patch.object(self.store, 'put', wraps=self.store.put) as put:
                with self.assertRaises(error):
                    call(plan)
                put.assert_not_called()
            self.assertEqual(self.path.read_bytes(), before)

    def test_snapshot_is_independent_and_default_api_unchanged(self):
        plan = plan_for([self.source], self.bundle.table_bytes, sa.MAX_SOURCE_BYTES)
        snapshot, _ = ci.validate(plan, [self.source], self.bundle.table_bytes, sa.MAX_SOURCE_BYTES, lambda: None)
        plan['sources'][0]['chunks'][0]['candidates'][0].clear()
        self.assertEqual(snapshot[self.source['id'], 0][0], ((-8, -8, -8),))
        # A synthetic stand-in exercises legacy dispatch without model execution.
        with mock.patch.object(sa.PreparedModel, 'encode', return_value=(-8, -8, -8)) as encoder:
            a = self.store_call(None)
            self.assertEqual(encoder.call_count, 3)
            b = self.store_call(plan_for([self.source], self.bundle.table_bytes, sa.MAX_SOURCE_BYTES))
            self.assertEqual(a['manifest'], b['manifest'])
            self.assertNotIn('supplied_candidates', a)
            self.assertNotIn('selection', a['layer_accounting'][0])
            c = self.catalog_call(None)
            d = self.catalog_call(plan_for([self.source], self.bundle.table_bytes, layout.CHUNK_BYTES))
            self.assertEqual(c['directory'], d['directory'])
            self.assertNotIn('supplied_candidates', c)
            self.assertEqual(c['encoder_calls'], 3)
            self.assertEqual(d['encoder_calls'], 0)


if __name__ == '__main__':
    unittest.main()
