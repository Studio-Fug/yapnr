"""The gradient of the whole optimization pipeline against finite differences (design §6.7).

x (half the DOF, mirror symmetry) → material grid with fixed pads and the feed ring → conic
filter → tanh projection → log-interpolated sheet conductance with damping → FDTD → port waves
with de-embedding and the radiated fraction → log-sum-exp group per frequency; one adjoint run
gives every per-frequency gradient, pulled back through the parameterization. Compared with
Richardson-extrapolated central differences along a random direction (measured: 1e-10
relative); also the length-scale constraint gradients at β = 128.
"""

from __future__ import annotations

import unittest

import numpy as np

from yapnr.rf.problem import Problem
from yapnr.rf.spec import OptimizerSpec
from yapnr.rf.testing import nominal_calibration, tiny_spec


class PipelineGradientTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = tiny_spec(radiated=True)
        spec = spec.replace(optimizer=OptimizerSpec(damping=0.5))
        cls.p = p = Problem(spec, exact=True, calibrations=nominal_calibration())
        rng = np.random.default_rng(3)
        cls.x = rng.uniform(0.3, 0.7, p.param.n_dof)
        cls.beta = 8.0
        ev = p.evaluate(p.param.rho_bar(cls.x, cls.beta))
        cls.ev = ev
        cls.grad = p.param.vjp(cls.x, cls.beta, ev.grads)
        cls.v = rng.standard_normal(cls.x.size)

    def _f(self, x):
        return self.p.evaluate(self.p.param.rho_bar(x, self.beta), gradients=False).values

    def test_runs_converged(self):
        self.assertTrue(self.ev.converged)
        self.assertEqual(len(self.ev.keys), 3)

    def test_directional_derivative(self):
        h = 1e-4
        x, v = self.x, self.v
        d1 = (self._f(x + h * v) - self._f(x - h * v)) / (2 * h)
        d2 = (self._f(x + 0.5 * h * v) - self._f(x - 0.5 * h * v)) / h
        fd = (4 * d2 - d1) / 3
        adj = self.grad @ v
        np.testing.assert_allclose(adj, fd, rtol=1e-6)

    def test_lengthscale_gradient(self):
        ls = self.p.lengthscale
        self.assertIsNotNone(ls)
        h = 1e-5
        g, dg = self.p.param.lengthscale(self.x, 128.0, ls)
        gp, _ = self.p.param.lengthscale(self.x + h * self.v, 128.0, ls)
        gm, _ = self.p.param.lengthscale(self.x - h * self.v, 128.0, ls)
        np.testing.assert_allclose(dg @ self.v, (gp - gm) / (2 * h), rtol=1e-5)


if __name__ == "__main__":
    unittest.main()
