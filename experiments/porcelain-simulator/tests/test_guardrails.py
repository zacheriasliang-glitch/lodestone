"""Guard checks with synthetic samples only: no mesh assembly or time steps."""
from fractions import Fraction
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np

from porcelain import core


class MeshSetupReached(Exception):
    """Stop a valid constructor before finite-element setup."""


class VolumeLimitTests(unittest.TestCase):
    def test_invalid_limits_fail_before_mesh_setup(self):
        limits = (float("nan"), float("inf"), float("-inf"), -.001,
                  True, None, "0.005", [.005], np.array(.005), np.array([.005]))
        for limit in limits:
            with self.subTest(limit=repr(limit)):
                with patch.object(core, "bases", side_effect=MeshSetupReached) as setup:
                    with self.assertRaisesRegex(ValueError, "volume drift"):
                        core.Solver(None, max_volume_drift=limit)
                    setup.assert_not_called()

    def test_finite_nonnegative_limits_are_preserved(self):
        for limit in (0, .005, .1, 1.5, np.float64(.02), np.int64(2), 10**400, Fraction(1, 3)):
            with self.subTest(limit=repr(limit)):
                solver = core.Solver.__new__(core.Solver)
                with patch.object(core, "bases", side_effect=MeshSetupReached) as setup:
                    with self.assertRaises(MeshSetupReached):
                        solver.__init__(None, max_volume_drift=limit)
                    setup.assert_called_once_with(None)
                self.assertIs(solver.max_volume_drift, limit)


class MeshQualityTests(unittest.TestCase):
    def quality(self, integration=(2., 3.), probe=(4., 5.), weights=(1., 2.), sign=1.):
        mapping = SimpleNamespace(detDF=Mock(side_effect=[np.array([integration]),
                                                         np.array([probe])]))
        basis = SimpleNamespace(mapping=mapping, X=object(), dx=np.array([weights]))
        with patch.object(core, "Basis", return_value=basis):
            return core.mesh_quality(object(), np.array([sign]), quadrature=None)

    def test_nonfinite_samples_are_rejected_in_both_sample_sets(self):
        for location in ("integration", "probe"):
            for value in (float("nan"), float("inf"), float("-inf")):
                with self.subTest(location=location, value=value):
                    with self.assertRaises(RuntimeError):
                        self.quality(**{location: (2., value)})

    def test_nonfinite_integrated_volume_is_rejected(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                with self.assertRaises(RuntimeError):
                    self.quality(weights=(value,))

    def test_zero_or_inverted_samples_still_fail(self):
        for location in ("integration", "probe"):
            for value in (0., -1.):
                with self.subTest(location=location, value=value):
                    with self.assertRaises(RuntimeError):
                        self.quality(**{location: (2., value)})

    def test_finite_samples_preserve_volume_minimum_and_reference_orientation(self):
        self.assertEqual(self.quality(), (3., 2.))
        self.assertEqual(self.quality(integration=(-2., -3.), probe=(-4., -5.), sign=-1.),
                         (3., 2.))


if __name__ == "__main__":
    unittest.main()
