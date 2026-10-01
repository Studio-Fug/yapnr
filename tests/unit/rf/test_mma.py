"""MMA on analytic problems (design §8.2): Svanberg's toy problem, a min-max of quadratics."""

from __future__ import annotations

import unittest

import numpy as np

from yapnr.rf.optim.epigraph import Epigraph
from yapnr.rf.optim.mma import MMA, MMASettings, MMAState
from yapnr.rf.optim.schedule import BetaSchedule


def toy(x):
    """Svanberg (2007): min x·x s.t. two spheres of radius 3, 0 ≤ x ≤ 5, from (4, 3, 2)."""
    f0 = float(x @ x)
    c1, c2 = np.array([5.0, 2.0, 1.0]), np.array([3.0, 4.0, 3.0])
    f = np.array([np.sum((x - c1) ** 2) - 9.0, np.sum((x - c2) ** 2) - 9.0])
    return f0, 2 * x, f, np.array([2 * (x - c1), 2 * (x - c2)])


CENTRES = 0.5 + 0.3 * np.array([[np.cos(a), np.sin(a)] for a in (0.3, 0.3 + 2.0944, 0.3 + 4.1888)])


def quads(x):
    d = x[None, :] - CENTRES
    return np.sum(d * d, axis=1), 2 * d


class MMAToyTest(unittest.TestCase):
    def _run(self, iterations=40):
        mma = MMA(3, 2, 0.0, 5.0, settings=MMASettings(move=1.0, epsimin=1e-9))
        x, st = np.array([4.0, 3.0, 2.0]), MMAState()
        trace = []
        for _ in range(iterations):
            f0, df0, f, df = toy(x)
            x, sol, st = mma.step(x, f0, df0, f, df, st)
            trace.append(x.copy())
        f0, df0, f, df = toy(x)
        return x, mma.kkt_residual(sol, df0, f, df), f, trace

    def test_converges_to_kkt_point(self):
        x, kkt, f, _ = self._run()
        self.assertLess(kkt, 1e-6)
        np.testing.assert_allclose(x, [2.017519, 1.780011, 1.237507], atol=1e-5)
        self.assertTrue(np.all(f <= 1e-6))  # both constraints active and met

    def test_deterministic(self):
        a = self._run(12)[3]
        b = self._run(12)[3]
        for u, v in zip(a, b):
            np.testing.assert_array_equal(u, v)

    def test_state_round_trip(self):
        mma = MMA(3, 2, 0.0, 5.0)
        x, st = np.array([4.0, 3.0, 2.0]), MMAState()
        for _ in range(3):
            f0, df0, f, df = toy(x)
            x, _, st = mma.step(x, f0, df0, f, df, st)
        back = MMAState.from_arrays(st.to_arrays())
        self.assertEqual(back.k, st.k)
        for name in ("xold1", "xold2", "low", "upp"):
            np.testing.assert_array_equal(getattr(back, name), getattr(st, name))


class EpigraphTest(unittest.TestCase):
    def _minimax(self, conservative, iterations=60):
        epi = Epigraph(2, settings=MMASettings(move=0.2, epsimin=1e-9))
        x, st = np.array([0.2, 0.7]), MMAState()
        ts = []
        for _ in range(iterations):
            f, df = quads(x)
            ev = (lambda xh: (quads(xh)[0], None)) if conservative else None
            step = epi.step(x, f, df, st, evaluate=ev)
            x, st = step.x, step.state
            ts.append(step.t)
        return x, ts

    def test_minimax_of_quadratics(self):
        x, ts = self._minimax(False)
        np.testing.assert_allclose(x, [0.5, 0.5], atol=1e-5)
        self.assertAlmostEqual(ts[-1], 0.09, delta=1e-8)

    def test_conservative_is_monotone(self):
        x, ts = self._minimax(True, 40)
        np.testing.assert_allclose(x, [0.5, 0.5], atol=1e-4)
        self.assertTrue(all(b <= a + 1e-12 for a, b in zip(ts, ts[1:])))

    def test_lengthscale_style_constraint(self):
        # min max_k |x − c_k|² subject to x_0 ≥ 0.6 (as g = 0.6 − x_0 ≤ 0).
        epi = Epigraph(2, settings=MMASettings(move=0.2, epsimin=1e-9))
        x, st = np.array([0.2, 0.7]), MMAState()
        for _ in range(80):
            f, df = quads(x)
            step = epi.step(x, f, df, st, np.array([0.6 - x[0]]), np.array([[-1.0, 0.0]]))
            x, st = step.x, step.state
        self.assertGreaterEqual(x[0], 0.6 - 1e-6)
        f, _ = quads(x)
        self.assertLess(f.max(), 0.15)


class ScheduleTest(unittest.TestCase):
    def test_epoch_rules(self):
        s = BetaSchedule(betas=(8, 16), iterations=(10, 20), min_iterations=8)
        self.assertEqual(s.cap(1), 20)
        self.assertFalse(s.epoch_done(0, [1.0] * 7, 0.0))
        self.assertTrue(s.epoch_done(0, [1.0] * 8, 0.0))
        self.assertFalse(s.epoch_done(0, [1.0] * 8, 0.5))
        self.assertTrue(s.epoch_done(0, list(np.linspace(2, 1, 10)), 0.5))
        self.assertEqual(s.move_for(0), 0.2)
        self.assertEqual(BetaSchedule(betas=(8, 64)).move_for(1), 0.1)
        with self.assertRaises(ValueError):
            BetaSchedule(betas=(8, 16), iterations=(3,))


if __name__ == "__main__":
    unittest.main()
