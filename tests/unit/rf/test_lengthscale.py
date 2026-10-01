"""Minimum width and space: the indicator constraints (design §7.5).

On a uniform grid with the minimum length b = 4 pixels (R = b, η_e = 0.75, c = 64R², ε = 1e-6)
at β = 128, measured: lines and gaps of width b, 1.25b, 1.5b and 2b give g/ε ≤ 1e-4; width
0.75b gives g/ε = 136 (a solid line whose skeleton stays below η_e, or the mirror-image gap);
width 0.5b gives 37 (the line vanishes in ρ̄ and is caught as gray void by the space
constraint). The tests require ≤ 1 to pass and ≥ 10 to fail.
"""

from __future__ import annotations

import unittest

import numpy as np

from yapnr.rf.design.filters import ConicFilter
from yapnr.rf.design.lengthscale import (
    LengthScale,
    conic_radius,
    eta_for_length,
    indicator_values,
)
from yapnr.rf.design.material_grid import MaterialGrid
from yapnr.rf.design.parameterization import Parameterization

B = 4  # pixels


def _setup(b_px=B, n=36):
    ls = LengthScale.from_rules(float(b_px), float(b_px), 1.0)
    f = ConicFilter(ls.radius, 1.0)
    g = MaterialGrid((n, n), ring_width=f.ring_width)
    return ls, Parameterization(g, f)


def _measure(ls, p, rho, beta=128.0):
    st = p.forward(p.grid.restrict(rho), beta)
    v = indicator_values(st["rho_tilde_m"], beta, 0.5, ls, p.margin).numpy()
    return v / ls.eps


def _line(n, w, solid=True):
    rho = np.zeros((n, n)) if solid else np.ones((n, n))
    lo = n // 2 - w // 2
    rho[lo : lo + w, 8 : n - 8] = 1.0 if solid else 0.0
    return rho


class RelationsTest(unittest.TestCase):
    def test_qian_sigmund(self):
        self.assertAlmostEqual(conic_radius(0.6, 0.75), 0.6)
        self.assertAlmostEqual(eta_for_length(0.6, 0.6), 0.75)
        for b in (0.3, 0.6, 1.1):
            for eta in (0.55, 0.7, 0.75, 0.9):
                self.assertAlmostEqual(eta_for_length(b, conic_radius(b, eta)), eta, places=12)

    def test_from_rules(self):
        ls = LengthScale.from_rules(0.6e-3, 0.6e-3, 0.3e-3)
        self.assertAlmostEqual(ls.radius, 0.6e-3)
        self.assertAlmostEqual(ls.eta_e, 0.75)
        self.assertAlmostEqual(ls.eta_d, 0.25)
        self.assertAlmostEqual(ls.c_value, 64 * 0.36e-6)
        ls = LengthScale.from_rules(0.6e-3, 0.9e-3, 0.3e-3)
        self.assertAlmostEqual(ls.radius, 0.9e-3)
        self.assertLess(ls.eta_e, 0.75)  # the narrower width under the larger radius
        self.assertAlmostEqual(ls.eta_d, 0.25)


class IndicatorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ls, cls.p = _setup()

    def test_wide_lines_and_gaps_pass(self):
        n = self.p.grid.shape[0]
        for w in (B, 5, 6, 2 * B):
            for solid in (True, False):
                v = _measure(self.ls, self.p, _line(n, w, solid))
                self.assertLessEqual(v.max(), 1.0, f"width {w} solid={solid}: {v}")

    def test_narrow_lines_and_gaps_fail(self):
        n = self.p.grid.shape[0]
        for w in (3, 2):  # 0.75 b and 0.5 b
            for solid in (True, False):
                v = _measure(self.ls, self.p, _line(n, w, solid))
                self.assertGreaterEqual(v.max(), 10.0, f"width {w} solid={solid}: {v}")
        # The right constraint flags the line: width for 0.75 b solid, space for its gap.
        self.assertGreater(_measure(self.ls, self.p, _line(n, 3, True))[0], 10.0)
        self.assertGreater(_measure(self.ls, self.p, _line(n, 3, False))[1], 10.0)

    def test_gradient(self):
        rng = np.random.default_rng(7)
        x = rng.uniform(0.2, 0.8, self.p.n_dof)
        vals, grads = self.p.lengthscale(x, 16.0, self.ls)
        v = rng.standard_normal(self.p.n_dof)
        h = 1e-6
        vp, _ = self.p.lengthscale(x + h * v, 16.0, self.ls)
        vm, _ = self.p.lengthscale(x - h * v, 16.0, self.ls)
        fd = (vp - vm) / (2 * h)
        np.testing.assert_allclose(grads @ v, fd, rtol=1e-5, atol=1e-6 * np.abs(fd).max())


if __name__ == "__main__":
    unittest.main()
