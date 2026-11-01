"""Small synthetic engineering cases; no model execution or research panels."""
import hashlib
import random
import unittest
from unittest import mock

from lodestone_machinery.latent import codec


def row(mismatches):
    values = [1] * mismatches + [0] * (16 - mismatches)
    return bytes((values[i] << 4) | values[i + 1] for i in range(0, 16, 2))


class CandidateSelectionTests(unittest.TestCase):
    def setUp(self):
        self.table = bytes(codec.TABLE_BYTES)
        self.zero = codec.coordinates(0)

    def check_packet(self, raw, sets, table=None, level=0):
        table = self.table if table is None else table
        result = codec.fold_candidates(raw, sets, table, level=level)
        self.assertEqual(codec.unfold(result['packet'], table,
                         expected_sha256=hashlib.sha256(raw).hexdigest(),
                         expected_size=len(raw), expected_level=level), raw)
        stats = result['accounting']
        self.assertEqual(stats['payload_bits'], sum(s['payload_bits']
                         for s in result['selection']['blocks']) + stats['tail_bits'])
        self.assertEqual(stats['payload_bits'], sum(stats[k] for k in
                         ('mode_bits', 'latent_bits', 'residual_mask_bits',
                          'residual_value_bits', 'literal_bits', 'tail_bits')))
        self.assertEqual(len(result['packet']) * 8,
                         codec.HEADER.size * 8 + stats['payload_bits'] + stats['padding_bits'])
        return result

    def test_all_mismatch_costs_singleton_compatibility(self):
        for m in range(17):
            with self.subTest(m=m):
                result = self.check_packet(row(m), [[self.zero]])
                selected = result['selection']['blocks'][0]
                self.assertEqual(selected['payload_bits'], min(65, 29 + 4 * m))
                self.assertEqual(selected['index'], 0 if m < 9 else None)
                old = codec.fold(row(m), [self.zero], self.table)
                self.assertEqual(result['packet'], old['packet'])
                self.assertEqual(result['accounting'], old['accounting'])

    def test_lower_cost_beats_rank_and_equal_cost_preserves_rank(self):
        table = row(10) + row(8) + row(8) + bytes(codec.TABLE_BYTES - 24)
        codes = [codec.coordinates(i) for i in (0, 2, 1)]
        result = self.check_packet(bytes(8), [codes], table)
        selected = result['selection']['blocks'][0]
        self.assertEqual((selected['index'], selected['rank'], selected['payload_bits']), (2, 1, 61))
        self.assertEqual(result['packet'], codec.fold(bytes(8), [codes[1]], table)['packet'])

    def test_empty_candidates_force_literal_even_with_exact_table_row(self):
        result = self.check_packet(bytes(8), [[]])
        self.assertEqual(result['selection']['blocks'][0], dict(
            index=None, rank=None, payload_bits=65, candidates_evaluated=0, nibble_comparisons=0))
        self.assertEqual(result['accounting']['literal_blocks'], 1)
        self.assertEqual(result['selection']['writer_nibble_comparisons'], 0)

    def test_only_supplied_rows_evaluated_and_duplicates_counted(self):
        # Unsupplied row 0 is exact; row 1 has nine mismatches (literal tie).
        table = bytes(8) + row(9) + bytes(codec.TABLE_BYTES - 16)
        codes = [codec.coordinates(1)] * 3
        with mock.patch.object(codec, '_residual', wraps=codec._residual) as residual:
            result = self.check_packet(bytes(8), [codes], table)
        self.assertEqual(residual.call_count, 3)
        self.assertTrue(all(call.args[1] == 1 for call in residual.call_args_list))
        self.assertEqual(result['selection']['candidates_evaluated'], 3)
        self.assertEqual(result['selection']['nibble_comparisons'], 48)
        self.assertIsNone(result['selection']['blocks'][0]['index'])

    def test_mixed_modes_tails_padding_and_empty_source(self):
        for tail in range(8):
            raw = row(0) + row(8) + row(9) + bytes(tail)
            result = self.check_packet(raw, [[self.zero], [self.zero], []])
            self.assertEqual(result['accounting']['payload_bits'], 29 + 61 + 65 + 8 * tail)
            self.assertEqual(result['selection']['writer_nibble_comparisons'], 32)
        self.check_packet(b'', [])

    def test_seeded_ranked_sets_at_cap_and_level_one(self):
        rng = random.Random(20261023)
        for case in range(24):
            block = rng.randbytes(8)
            rows = []
            for m in range(17):
                candidate = bytearray(block)
                for nibble in rng.sample(range(16), m):
                    candidate[nibble // 2] ^= rng.randrange(1, 16) << (4 if nibble % 2 == 0 else 0)
                rows.append(bytes(candidate))
            table = b''.join(rows) + bytes(codec.TABLE_BYTES - 8 * len(rows))
            # Mix cap-sized lists, singleton lists, duplicates and empty lists.
            size = (0, 1, 7, 64)[case % 4]
            indices = [rng.randrange(17) for _ in range(size)]
            rng.shuffle(indices)
            candidates = [codec.coordinates(i) for i in indices]
            expected = (65, -1, None)  # Literal precedes any equal cost.
            for rank, index in enumerate(indices):
                differences = [a ^ b for a, b in zip(block, rows[index])]
                mismatch_count = sum(int(x >> 4 != 0) + int(x & 15 != 0) for x in differences)
                expected = min(expected, (29 + 4 * mismatch_count, rank, index))
            raw = block + rng.randbytes(case % 8)
            result = self.check_packet(raw, [candidates], table, level=1)
            selected = result['selection']['blocks'][0]
            self.assertEqual((selected['payload_bits'], selected['rank'], selected['index']),
                             (expected[0], None if expected[1] == -1 else expected[1], expected[2]))
            self.assertEqual(result['selection']['candidates_evaluated'], size)
            self.assertEqual(result['selection']['nibble_comparisons'], size * 16)
            if indices:
                singleton = self.check_packet(raw, [[candidates[0]]], table, level=1)
                legacy = codec.fold(raw, [candidates[0]], table, level=1)
                self.assertEqual(singleton['packet'], legacy['packet'])
                self.assertEqual(singleton['accounting'], legacy['accounting'])

    def test_bounds_and_invalid_inputs_fail_closed(self):
        for block, codes, table in ((b'', [], self.table), (bytes(9), [], self.table),
                (bytes(8), [], b''), (bytes(8), [self.zero] * 65, self.table),
                (bytes(8), iter([self.zero]), self.table),
                (bytes(8), [self.zero, (True, 0, 0)], self.table)):
            with self.assertRaises(ValueError):
                codec.select_candidate(block, codes, table)
        for raw, codes, level in ((b'', [], True), (bytes(8), [], 0),
                (bytes(codec.MAX_INPUT + 1), [], 0), ('bad', [], 0),
                (bytes(8), [None], 0)):
            with self.assertRaises(ValueError):
                codec.fold_candidates(raw, codes, self.table, level=level)
        result = self.check_packet(bytes(8), [[self.zero] * codec.MAX_CANDIDATES])
        self.assertEqual(result['selection']['candidates_evaluated'], 64)
        self.assertEqual(result['selection']['blocks'][0]['rank'], 0)


if __name__ == '__main__':
    unittest.main()
