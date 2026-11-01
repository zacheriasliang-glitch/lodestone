"""Invented fixtures only; no corpus, original files, models or private stores."""
import copy
import hashlib
import random
import unittest
from unittest import mock

from lodestone_machinery.latent import codec


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class CodecTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.table = bytes(codec.TABLE_BYTES)

    def roundtrip(self, raw, q=(-8, -8, -8), table=None):
        table = self.table if table is None else table
        folded = codec.fold(raw, [q] * (len(raw) // 8), table)
        self.assertEqual(codec.unfold(folded['packet'], table,
                         expected_sha256=sha(raw), expected_size=len(raw)), raw)
        stats = folded['accounting']
        bits = sum(stats[k] for k in ('mode_bits', 'latent_bits',
                   'residual_mask_bits', 'residual_value_bits', 'literal_bits', 'tail_bits'))
        self.assertEqual(stats['payload_bits'], bits)
        self.assertEqual(len(folded['packet']) * 8,
                         codec.HEADER.size * 8 + bits + stats['padding_bits'])
        return folded

    def test_all_coordinate_codes_are_bijective(self):
        for code in range(4096):
            self.assertEqual(codec.code_index(codec.coordinates(code)), code)

    def test_tails_arbitrary_bytes_and_empty(self):
        rng = random.Random(712)
        for n in list(range(33)) + [127, 128, 129, 1003]:
            self.roundtrip(bytes(rng.randrange(256) for _ in range(n)))

    def test_learned_mask_order_threshold_and_literal_tie(self):
        for mismatches in range(17):
            nibs = [1] * mismatches + [0] * (16 - mismatches)
            raw = bytes((nibs[i] << 4) | nibs[i+1] for i in range(0, 16, 2))
            stats = self.roundtrip(raw)['accounting']
            self.assertEqual(stats['learned_blocks'], int(mismatches <= 8))
            self.assertEqual(stats['payload_bits'],
                             29 + 4 * mismatches if mismatches <= 8 else 65)

    def test_nonzero_decoder_row_is_used(self):
        table = bytearray(self.table)
        table[4095*8:] = b'abcdefgh'
        stats = self.roundtrip(b'abcdefgh', (7, 7, 7), bytes(table))['accounting']
        self.assertEqual(stats['residual_value_bits'], 0)
        self.assertEqual(stats['latent_bits'], 12)

    def test_pins_and_header_corruption(self):
        raw = b'abcdefgh'
        packet = self.roundtrip(raw)['packet']
        for kwargs in ({'expected_sha256': '0'*64}, {'expected_size': 7},
                       {'expected_level': 1}):
            args = dict(expected_sha256=sha(raw), expected_size=len(raw))
            args.update(kwargs)
            with self.assertRaises(ValueError):
                codec.unfold(packet, self.table, **args)
        for pos in (0, 4, 5, 9, 10, 42, len(packet)-2):
            bad = bytearray(packet)
            bad[pos] ^= 1
            with self.assertRaises(ValueError):
                codec.unfold(bytes(bad), self.table, expected_sha256=sha(raw), expected_size=8)
        with self.assertRaises(ValueError):
            codec.unfold(packet, b'1' * codec.TABLE_BYTES,
                         expected_sha256=sha(raw), expected_size=8)

    def test_truncation_trailing_and_nonzero_padding(self):
        packet = self.roundtrip(bytes(8))['packet']  # 29 bits, three padding bits
        for bad in (packet[:-1], packet+b'\0', packet[:-1]+bytes([packet[-1] | 1])):
            with self.assertRaises(ValueError):
                codec.unfold(bad, self.table, expected_sha256=sha(bytes(8)), expected_size=8)

    def test_redundant_and_literal_should_win_masks_rejected(self):
        for mask, values in ((0x8000, [0]), (0xff80, [1]*9)):
            writer = codec._Writer()
            writer.put(1, 1)
            writer.put(0, 12)
            writer.put(mask, 16)
            for value in values:
                writer.put(value, 4)
            packet = codec.HEADER.pack(codec.MAGIC, 0, 0, 8,
                     bytes.fromhex(sha(self.table)), bytes.fromhex(sha(bytes(8)))) + writer.finish()
            with self.assertRaises(ValueError):
                codec.unfold(packet, self.table, expected_sha256=sha(bytes(8)), expected_size=8)

    def test_input_and_coordinate_bounds(self):
        for q in ((True, 0, 0), (-9, 0, 0), (8, 0, 0), (0, 0), (0.0, 0, 0)):
            with self.assertRaises(ValueError):
                codec.fold(bytes(8), [q], self.table)
        for raw, codes in ((bytes(8), []), ('bad', []), (bytes(codec.MAX_INPUT+1), [])):
            with self.assertRaises(ValueError):
                codec.fold(raw, codes, self.table)
        with self.assertRaises(ValueError):
            codec.fold(b'', [], self.table[:-1])

    def test_maximum_source_and_packet_bound(self):
        packet = self.roundtrip(b'\xff' * codec.MAX_INPUT)['packet']
        self.assertEqual(len(packet), codec.HEADER.size + codec.MAX_INPUT + codec.MAX_INPUT // 64)
        self.assertLessEqual(len(packet), codec.MAX_PACKET)

    def test_two_layers_transform_the_child_packet_and_decode_without_encoder(self):
        raw, observed = b'abcdefgh' * 16 + b'xyz', []
        def encoder(block):
            observed.append(block)
            return (-8, -8, -8)
        first = codec.fold_layers(raw, [encoder], [self.table])
        observed.clear()
        both = codec.fold_layers(raw, [encoder, encoder], [self.table, self.table])
        n = len(raw)//8
        self.assertEqual(observed[n:], [first['packet'][i:i+8]
                         for i in range(0, len(first['packet'])-7, 8)])
        self.assertEqual(codec.unfold_layers(both['packet'], [self.table]*2, both['layers']), raw)
        for key, value in (('input_sha256', '0'*64), ('packet_bytes', 1),
                           ('decoder_sha256', '0'*64), ('level', 1)):
            layers = copy.deepcopy(both['layers'])
            layers[0][key] = value
            with self.assertRaises(ValueError):
                codec.unfold_layers(both['packet'], [self.table]*2, layers)

    def test_layered_ingress_bounded_before_hash_and_manifest_validation(self):
        for packet in (bytes(codec.MAX_PACKET+1), b'', 'bad'):
            with mock.patch.object(codec, '_sha', side_effect=AssertionError('hashed')):
                with self.assertRaises(ValueError):
                    codec.unfold_layers(packet, [self.table], [{}])
        result = codec.fold_layers(b'12345678', [lambda _: (-8,-8,-8)], [self.table])
        for entry in ({}, None, dict(result['layers'][0], level=True)):
            with self.assertRaises(ValueError):
                codec.unfold_layers(result['packet'], [self.table], [entry])


if __name__ == '__main__':
    unittest.main()
