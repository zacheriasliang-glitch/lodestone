"""Wire compatibility against an immutable pre-selector codec; invented bytes only."""
import hashlib
import importlib.util
import inspect
from pathlib import Path
import unittest

from lodestone_machinery.latent import codec
from lodestone_machinery.latent import compact_checkpoint as cp
from lodestone_machinery.latent import store_adapter as sa


ORIGINAL_SHA256 = '64b5f0bea5683191a8914f0cde36ce3ec9885ae3b951168edea8460e4e342068'
ORIGINAL_BLOB = 'c6c8416aea232e3bef28943627f74c10557b8521'
ORIGINAL_RESTORE_SOURCE_SHA256 = '526f12ba6c08a7e7bc8cc3113650b08ea2b33804f8bec1857a2913e8e2c9394f'
ORIGINAL_READ_SESSION_SHA256 = 'afcae4627ca3f18e8edb60a401241e2d598d382a9bfbef2d67ffcb33c77733a1'


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class OriginalCodecCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).parent / 'fixtures' / 'latent' / 'codec_4386273.py'
        raw = path.read_bytes()
        if (len(raw) != 9990 or sha(raw) != ORIGINAL_SHA256 or
                hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest() != ORIGINAL_BLOB):
            raise AssertionError('Original codec fixture no longer matches its recorded Git bytes')
        spec = importlib.util.spec_from_file_location('original_codec_4386273', path)
        cls.original = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.original)
        cls.table = bytes(8) + b'\xff' * 8 + bytes(codec.TABLE_BYTES - 24) + b'abcdefgh'

    def test_default_packets_and_accounting_match_original_writer(self):
        cases = [b'', b'tail', bytes(8), b'abcdefgh!', bytes(range(32)) + b'xyz',
                 b'\xff' * 1033]
        for raw in cases:
            for level in (0, 1):
                with self.subTest(bytes=len(raw), level=level):
                    codes = [codec.coordinates((0, 1, 4095)[i % 3]) for i in range(len(raw) // 8)]
                    old = self.original.fold(raw, codes, self.table, level=level)
                    new = codec.fold(raw, codes, self.table, level=level)
                    singleton = codec.fold_candidates(raw, [[q] for q in codes], self.table, level=level)
                    self.assertEqual(new, old)
                    self.assertEqual(singleton['packet'], old['packet'])
                    self.assertEqual(singleton['accounting'], old['accounting'])

    def test_storage_readers_match_original_5cd_bodies(self):
        # Integration cases execute these exact pinned bodies on newly created stores.
        self.assertEqual(sha(inspect.getsource(sa.restore_source).encode()), ORIGINAL_RESTORE_SOURCE_SHA256)
        self.assertEqual(sha(inspect.getsource(cp.ReadSession).encode()), ORIGINAL_READ_SESSION_SHA256)

    def test_original_reader_accepts_new_ranked_mixed_mode_packet(self):
        raw = bytes(8) + b'\x11' * 4 + bytes(4) + b'\x11' * 4 + b'\x10' + bytes(3) + b'tail'
        zero, wrong = codec.coordinates(0), codec.coordinates(1)
        for level in (0, 1):
            new = codec.fold_candidates(raw, [[wrong, zero, zero], [wrong, zero], [zero]],
                                        self.table, level=level)
            self.assertEqual(new['accounting']['payload_bits'], 187)
            self.assertEqual(new['accounting']['packet_bytes'], 98)
            self.assertEqual(self.original.unfold(new['packet'], self.table,
                             expected_sha256=sha(raw), expected_size=len(raw), expected_level=level), raw)

    def test_equal_cost_rank_changes_row_not_wire_recoverability(self):
        zero, other_zero = codec.coordinates(0), codec.coordinates(2)
        packets = []
        for ranked, selected in (([other_zero, zero], 2), ([zero, other_zero], 0)):
            new = codec.fold_candidates(bytes(8), [ranked], self.table)
            self.assertEqual(new['selection']['blocks'][0]['index'], selected)
            self.assertEqual(new['selection']['blocks'][0]['rank'], 0)
            self.assertEqual(new['accounting']['payload_bits'], 29)
            self.assertEqual(self.original.unfold(new['packet'], self.table,
                             expected_sha256=sha(bytes(8)), expected_size=8), bytes(8))
            packets.append(new['packet'])
        self.assertNotEqual(*packets)

    def test_default_layer_packets_match_original_without_models(self):
        raw = b'invented layering bytes' * 4 + b'x'
        encoders = [lambda _: (-8, -8, -8)] * 2
        old = self.original.fold_layers(raw, encoders, [self.table] * 2)
        new = codec.fold_layers(raw, encoders, [self.table] * 2)
        self.assertEqual(new, old)
        self.assertEqual(self.original.unfold_layers(new['packet'], [self.table] * 2, new['layers']), raw)


if __name__ == '__main__':
    unittest.main()
