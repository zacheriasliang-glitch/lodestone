"""Custody, failure detection, and actual Genepool consumption of new organs."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from lodestone_machinery.bezoar import Plumber
from lodestone_machinery.organs.ingest import read_ingest
from lodestone_machinery.organs._kernel import strict_load

ROOT = Path(__file__).resolve().parents[2]


class BezoarTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='kernel organs ')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.sources = self.base / 'sources'
        self.sources.mkdir()
        self.source = self.sources / 'kernel.txt'
        self.original = b'Probe score = effort + gain.\nFunction travels; identity does not.\n'
        self.source.write_bytes(self.original)
        self.plumber = Plumber(self.base / 'work')
        self.store = self.plumber.records
        self.ingestion = self.plumber.ingest(self.sources, ['kernel.txt'])
        self.request = self.plumber.eat_request(self.ingestion, 'Extract the declared probe operator.')
        self.proposal = {'schema': 'kernel-digestion/v1', 'request_handle': self.request,
                         'id': 'test-parent', 'actor': 'explicit-test-fixture',
                         'operators': {'probe_gain_sign': {'value': 1, 'source': 'kernel.txt',
                                      'quote': 'Probe score = effort + gain.', 'rationale': 'Gain has positive sign.'}},
                         'instructions': [], 'rejected': [], 'uncertainties': ['Toy operator only.']}
        self.parent = self.plumber.eat(self.request, self.proposal)
        self.mutagen = {'schema': 'kernel-mutagen/v1', 'id': 'flip-sign', 'new_id': 'test-child',
                        'operator': 'probe_gain_sign', 'expected': 1, 'replacement': -1,
                        'rationale': 'Test preference for higher gain on a fresh board.'}

    def payload(self, handle):
        return self.store.read(handle)['payload']

    def test_composition_preserves_exact_bytes_parent_and_one_locus(self):
        parent_bytes = self.store.path(self.parent).read_bytes()
        child = self.plumber.mutate(self.parent, self.mutagen)
        profile = self.payload(child)['profile']
        self.assertEqual(read_ingest(self.store, self.ingestion, 'eat')['sources'][0]['text'].encode(), self.original)
        self.assertEqual(self.store.path(self.parent).read_bytes(), parent_bytes)
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertEqual(profile['operators'], {'probe_gain_sign': -1})
        self.assertEqual(profile['lineage']['parent_record'], self.parent)
        self.assertEqual(self.payload(child)['fitness'], 'UNMEASURED')
        self.assertEqual(self.store.verify_links([child])['records_checked'], 4)

    @unittest.skipUnless(
        (ROOT / 'experiments/001-rule30/Genepool/genepool.py').is_file(),
        'Optional historical Genepool consumer is absent from this checkout')
    def test_mutated_profile_is_consumed_by_existing_genepool_assay(self):
        spec = importlib.util.spec_from_file_location('kernel_genepool', ROOT / 'experiments/001-rule30/Genepool/genepool.py')
        genepool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(genepool)
        board = self.base / 'fresh-board.json'
        board.write_text(json.dumps({'schema': 'probe-board/v1', 'id': 'new-organ-fixture',
            'question': 'Choose useful gain at equal effort.', 'probes': [
                {'id': 'low', 'effort': 3, 'gain': 1}, {'id': 'high', 'effort': 3, 'gain': 4}],
            'expected_choice': 'high'}))
        choices = []
        for handle in (self.parent, self.plumber.mutate(self.parent, self.mutagen)):
            path = self.base / (handle.split(':')[0] + '.json')
            path.write_text(json.dumps(self.payload(handle)['profile']))
            choices.append(genepool.assay(path, board)['choice'])
        self.assertEqual(choices, ['low', 'high'])

    def test_unknown_quote_wrong_request_and_extra_authority_reject(self):
        for field, value in [('request_handle', self.ingestion), ('authority', 'EXECUTE')]:
            proposal = copy.deepcopy(self.proposal)
            proposal[field] = value
            with self.assertRaises(ValueError):
                self.plumber.eat(self.request, proposal)
        proposal = copy.deepcopy(self.proposal)
        proposal['operators']['probe_gain_sign']['quote'] = 'Invented evidence'
        with self.assertRaisesRegex(ValueError, 'quotation'):
            self.plumber.eat(self.request, proposal)

    def test_mutation_requires_exact_type_locus_and_real_change(self):
        for key, value in [('expected', True), ('expected', -1), ('replacement', 1),
                           ('operator', 'missing'), ('new_id', 'test-parent')]:
            mutagen = {**self.mutagen, key: value}
            with self.assertRaises(ValueError):
                self.plumber.mutate(self.parent, mutagen)

    def test_mutation_of_variant_retains_parent_chain(self):
        child = self.plumber.mutate(self.parent, self.mutagen)
        grandchild = self.plumber.mutate(child, {**self.mutagen, 'new_id': 'grandchild', 'expected': -1, 'replacement': 1})
        lineage = self.payload(grandchild)['profile']['lineage']
        self.assertEqual(lineage['parent_record'], child)
        self.assertEqual(lineage['parent_lineage']['parent_record'], self.parent)

    def test_oversize_child_is_rejected_before_publication(self):
        before = self.store.catalog()
        with self.assertRaisesRegex(ValueError, 'exceeds 262144 bytes'):
            self.plumber.mutate(self.parent, {**self.mutagen, 'replacement': 'x' * 132000})
        self.assertEqual(self.store.catalog(), before)

    def test_capture_rejects_traversal_symlink_escape_duplicates_oversize(self):
        external = self.base / 'outside.txt'
        external.write_text('outside')
        (self.sources / 'escape.txt').symlink_to(external)
        for paths in [['../outside.txt'], ['escape.txt'], ['kernel.txt', 'kernel.txt']]:
            with self.assertRaises(ValueError):
                self.plumber.ingest(self.sources, paths)
        (self.sources / 'large.txt').write_bytes(b'x' * (2 * 1024 * 1024 + 1))
        with self.assertRaises(ValueError):
            self.plumber.ingest(self.sources, ['large.txt'])

    def test_drift_routes_to_auditor_and_new_clean_observation_cannot_clear_hold(self):
        child = self.plumber.mutate(self.parent, self.mutagen)
        clean = self.plumber.hold(self.plumber.watch([child]))
        self.assertEqual(self.payload(clean)['next_role'], 'ORCHESTRATOR')
        self.source.write_text('changed source')
        report = self.plumber.watch([child])
        self.assertEqual(self.payload(report)['issues'][0]['code'], 'SOURCE_DRIFT')
        held = self.plumber.hold(report)
        held_bytes = self.store.path(held).read_bytes()
        self.assertEqual(self.payload(held)['next_role'], 'AUDITOR')
        self.assertEqual(self.payload(held)['dispatch'], 'PROPOSAL_ONLY')
        self.source.write_bytes(self.original)
        self.plumber.hold(self.plumber.watch([child]))
        self.assertEqual(self.store.path(held).read_bytes(), held_bytes)
        self.assertEqual(self.payload(held)['held_findings'][0]['state'], 'HELD')

    def test_missing_source_and_corrupt_record_remain_reportable(self):
        self.source.unlink()
        report = self.plumber.watch([self.parent])
        self.assertEqual(self.payload(report)['issues'][0]['code'], 'SOURCE_UNAVAILABLE')
        self.store.path(self.ingestion).write_bytes(b'corrupted')
        report = self.plumber.watch([self.parent])
        self.assertEqual(self.payload(report)['issues'][0]['code'], 'RECORD_UNAVAILABLE')
        held = self.plumber.hold(report)
        self.assertEqual(self.store.verify_links([held])['status'], 'INTACT')
        with self.assertRaises(ValueError):
            self.plumber.mutate(self.parent, self.mutagen)

    def test_unknown_producer_cannot_claim_observation(self):
        wrong = self.store.publish('builder', 'observation', {'schema': 'watcher-observation/v1'})
        with self.assertRaises(ValueError):
            self.plumber.hold(wrong)

    def test_growing_sources_cannot_evade_fresh_read_budget(self):
        paths = [f'growing-{i}.txt' for i in range(8)]
        for path in paths:
            (self.sources / path).write_bytes(b'')
        handle = self.plumber.ingest(self.sources, paths)
        for path in paths:
            (self.sources / path).write_bytes(b'x' * (2 * 1024 * 1024))
        report = self.payload(self.plumber.watch([handle]))
        self.assertEqual(report['source_read_bytes_charged'], 8 * 1024 * 1024)
        self.assertIn('COVERAGE_LIMIT', [issue['code'] for issue in report['issues']])
        self.assertEqual(report['status'], 'ATTENTION')

    def test_malformed_capture_and_strict_json_fail_closed(self):
        payload = copy.deepcopy(self.payload(self.ingestion))
        payload['sources'][0]['text'] = 123
        forged = self.store.publish('ingest', 'ingested_sources', payload)
        with self.assertRaises(ValueError):
            read_ingest(self.store, forged, 'eat')
        report = self.plumber.watch([forged])
        self.assertEqual(self.payload(report)['issues'][0]['code'], 'CAPTURE_INVALID')
        for raw in ['{"key":1,"key":2}', '{"key":NaN}']:
            p = self.base / 'bad.json'
            p.write_text(raw)
            with self.assertRaises(ValueError):
                strict_load(p)

    def test_real_toolkit_cli_routes_and_export_does_not_overwrite(self):
        command = [sys.executable, '-B', str(ROOT / 'tools/ecosystem/lodestone'),
                   '--repo', str(ROOT), '--workspace', str(self.store.workspace)]
        observed = subprocess.run([*command, 'watcher', 'observe', '--handle', self.parent],
                                  cwd=self.base, capture_output=True, text=True, timeout=15)
        self.assertEqual(observed.returncode, 0, observed.stderr)
        self.assertEqual(json.loads(observed.stdout)['observations'][0]['report']['status'], 'INTACT')
        export = [*command, 'bezoar', 'export-profile', '--handle', self.parent, '--output', 'profile.json']
        first = subprocess.run(export, cwd=self.base, capture_output=True, text=True, timeout=15)
        self.assertEqual(first.returncode, 0, first.stderr)
        raw = (self.base / 'profile.json').read_bytes()
        second = subprocess.run(export, cwd=self.base, capture_output=True, text=True, timeout=15)
        self.assertEqual(second.returncode, 2)
        self.assertEqual((self.base / 'profile.json').read_bytes(), raw)


if __name__ == '__main__':
    unittest.main()
