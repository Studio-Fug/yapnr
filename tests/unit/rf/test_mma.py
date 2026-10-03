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


def wavy(x):
    """Two non-convex functions whose separable MMA approximations miss their ripples."""
    f = np.array(
        [np.cos(9.0 * x[0]) + 2.0 * (x[1] - 0.3) ** 2, np.sin(7.0 * x[1]) + (x[0] - 0.6) ** 2]
    )
    df = np.array(
        [
            [-9.0 * np.sin(9.0 * x[0]), 4.0 * (x[1] - 0.3)],
            [2.0 * (x[0] - 0.6), 7.0 * np.cos(7.0 * x[1])],
        ]
    )
    return f, df


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

    def test_conservative_rejects_after_max_inner(self):
        # One subproblem per call: most calls end without a conservative approximation. Those
        # steps must be rejected (x unchanged, t unchanged), and the raised curvature carried in
        # the state must let a later call at the same point succeed.
        epi = Epigraph(2, settings=MMASettings(move=0.5, epsimin=1e-9))
        x, st = np.array([0.1, 0.9]), MMAState()
        ts, rejected = [], 0
        for _ in range(25):
            f, df = wavy(x)
            step = epi.step(
                x, f, df, st, evaluate=lambda xh: (wavy(xh)[0], None), max_inner=1, move=0.5
            )
            self.assertLessEqual(step.t_new, step.t + 1e-9)  # the conservative test tolerance
            if not step.accepted:
                rejected += 1
                np.testing.assert_array_equal(step.x, x)
                self.assertIsNotNone(step.state.rho)
            else:
                self.assertIsNone(step.state.rho)
            x, st = step.x, step.state
            ts.append(step.t)
        self.assertGreater(rejected, 5)
        self.assertTrue(all(b <= a + 1e-9 for a, b in zip(ts, ts[1:])))
        self.assertLess(ts[-1], -0.8)
        back = MMAState.from_arrays(st.to_arrays())
        np.testing.assert_array_equal(back.rho, st.rho)

    def test_adaptive_move_is_monotone(self):
        # Plain MMA with a 0.5 move oscillates on the ripples (t: 0.28 → 0.49, −0.78 → −0.06);
        # the adaptive move refuses every step that raises t and halves the move, and
        # reaches the same optimum.
        epi = Epigraph(2, settings=MMASettings(move=0.5, epsimin=1e-9))
        x, st, move = np.array([0.1, 0.9]), MMAState(), 0.5
        ts, refused = [], 0
        for _ in range(25):
            f, df = wavy(x)
            tr = epi.trust_step(
                x, f, df, st, move=move, evaluate=lambda xh: (wavy(xh)[0], None), slack=0.0
            )
            step = tr.step
            self.assertEqual(len(tr.t_trials), tr.trials)
            if step.accepted:
                self.assertLessEqual(step.t_new, step.t + 1e-12)
                move = min(0.5, 1.5 * tr.move) if step.t_new < step.t else tr.move
            else:
                refused += 1
                np.testing.assert_array_equal(step.x, x)
                move = max(1e-3, 0.5 * tr.move)
            refused += tr.trials - 1
            x, st = step.x, step.state
            ts.append(step.t)
        self.assertTrue(all(b <= a + 1e-12 for a, b in zip(ts, ts[1:])))
        self.assertLess(ts[-1], -0.81)
        self.assertGreater(refused, 3)

    def test_adaptive_slack_from_the_best(self):
        # A trial point 0.04 above t_k: within the slack (0.05) of t_k, so accepted when the
        # slack is measured from t_k, and refused (every trial, x kept) when it is measured
        # from an epoch's best t 0.5 lower (`optimizer.trust_reference: best`).
        epi = Epigraph(2, settings=MMASettings(move=0.2, epsimin=1e-9))
        x = np.array([0.1, 0.9])
        f, df = quads(x)
        up = float(np.max(f)) + 0.04
        kw = dict(move=0.2, evaluate=lambda xh: (np.array([up]), None), slack=0.05)
        tr = epi.trust_step(x, f, df, MMAState(), **kw)
        self.assertTrue(tr.step.accepted)
        self.assertEqual(tr.trials, 1)
        tr = epi.trust_step(x, f, df, MMAState(), reference=float(np.max(f)) - 0.5, **kw)
        self.assertFalse(tr.step.accepted)
        np.testing.assert_array_equal(tr.step.x, x)
        self.assertGreater(tr.trials, 1)

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
