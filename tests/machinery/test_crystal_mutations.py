import random
import unittest

from lodestone_machinery.crystal_mutations import mutate, orbit, a3_features, reverse_trace


class MutationTests(unittest.TestCase):
    def test_exact_rule_direction_and_roundtrip(self):
        # Two incoming edges 0 -> 1, no reverse edge.
        self.assertEqual(mutate((3, 4), 1, ((0, 2), (0, 0))), (3, 2))
        rng = random.Random(731)
        for _ in range(100):
            start = tuple(rng.randrange(-1000, 1001) for _ in range(3))
            trace = [rng.randrange(3) for _ in range(40)]
            current = start
            for vertex in trace:
                self.assertEqual(mutate(mutate(current, vertex), vertex), current)
                current = mutate(current, vertex)
            self.assertEqual(reverse_trace(current, trace), start)

    def test_a3_known_orbit_and_relations(self):
        self.assertEqual(orbit((0, 0, 0)), ((0, 0, 0),))
        self.assertEqual(len(orbit((1, 4, 9))), 24)
        x = (3, -7, 11)
        self.assertEqual(mutate(mutate(x, 0), 2), mutate(mutate(x, 2), 0))
        self.assertEqual(mutate(mutate(mutate(x, 0), 1), 0), mutate(mutate(mutate(x, 1), 0), 1))
        self.assertEqual(a3_features(x)[:3], x)
        self.assertEqual(a3_features(x)[3:6], mutate(x, 0))

    def test_nonfinite_graph_and_invalid_values_fail_explicitly(self):
        with self.assertRaisesRegex(ValueError, "finiteness"):
            orbit((1, 2), ((0, 3), (3, 0)), max_states=20)
        for vector in ((True, 0, 0), (1.0, 2, 3), (2**257, 0, 0)):
            with self.assertRaises(ValueError):
                mutate(vector, 0)
        with self.assertRaisesRegex(ValueError, "zero diagonal"):
            mutate((1,), 0, ((1,),))


if __name__ == "__main__":
    unittest.main()
