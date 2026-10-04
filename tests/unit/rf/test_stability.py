"""Time-step bounds (design §4.7): the power-iteration λ_max against the analytic value of a
uniform PEC cavity, and the leapfrog's stability limit on a graded grid with a substrate."""

from __future__ import annotations

import math
import unittest

import numpy as np

from yapnr.rf.constants import C0
from yapnr.rf.fdtd.engine import Simulation
from yapnr.rf.fdtd.stability import max_eigenvalue
from yapnr.rf.fdtd.stop import StopRule
from yapnr.rf.materials import Structure
from yapnr.rf.mesh import Axis, Grid, PMLCells, graded_axis, substrate_z_axis
from yapnr.rf.stackup import Stackup

NO_PML = PMLCells(0, 0, 0, 0, 0)


class PowerIterationTest(unittest.TestCase):
    def test_uniform_cavity(self):
        n = (10, 8, 6)
        d = (0.3e-3, 0.25e-3, 0.2e-3)
        axes = [Axis(np.arange(n[a] + 1) * d[a]) for a in range(3)]
        grid = Grid(*axes, k_c=2, pml=NO_PML)
        lam, iters = max_eigenvalue(grid)
        exact = 4 * C0**2 * sum(math.cos(math.pi / (2 * n[a])) ** 2 / d[a] ** 2 for a in range(3))
        self.assertLessEqual(lam, exact * (1 + 1e-9))
        self.assertLess(abs(lam - exact) / exact, 0.01, f"{lam} vs {exact} after {iters}")


def graded_board():
    st = Stackup(3.55, 0.0027, 0.813e-3, 10e9)
    x = graded_axis(0, 1.2e-3, 0.3e-3, -1.0e-3, 2.2e-3, max_cell=0.7e-3)
    y = graded_axis(0, 0.9e-3, 0.3e-3, -1.0e-3, 1.9e-3, max_cell=0.7e-3)
    z, kc = substrate_z_axis(st.h, 3, 1.5e-3, dz_max=0.6e-3, n_pml=0)
    grid = Grid(x, y, z, kc, NO_PML)
    s = Structure(grid, st)
    s.set_pixels(np.where(np.random.default_rng(0).uniform(size=grid.n[:2]) > 0.5, 1.0, 0.0))
    return grid, s


class LeapfrogLimitTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.grid, cls.s = graded_board()
        lam, _ = max_eigenvalue(cls.grid, cls.s, tol=1e-9, max_iter=5000)
        cls.dt_lim = 2.0 / math.sqrt(lam)

    def growth(self, factor, steps=20000):
        sim = Simulation(self.grid, self.s, dt=factor * self.dt_lim)
        rng = np.random.default_rng(1)
        for c in ("ex", "ey", "ez"):
            sim.f[c][...] = rng.standard_normal(sim.f[c].shape)
        # Keep the PEC edges at zero as the stepper would.
        sim.f["ex"][:, [0, -1], :] = 0
        sim.f["ex"][:, :, [0, -1]] = 0
        sim.f["ey"][[0, -1], :, :] = 0
        sim.f["ey"][:, :, [0, -1]] = 0
        sim.f["ez"][[0, -1], :, :] = 0
        sim.f["ez"][:, [0, -1], :] = 0
        start = max(np.abs(sim.f[c]).max() for c in ("ex", "ey", "ez"))
        peak = start
        for _ in range(0, steps, 500):
            sim.advance(500)  # source-free steps on any backend
            peak = max(peak, max(float(np.abs(sim.f[c]).max()) for c in ("ex", "ey", "ez")))
            if peak > 1e8 * start:
                break
        return peak / start

    def test_cfl_is_conservative(self):
        self.assertLessEqual(self.grid.courant_dt(), self.dt_lim)

    def test_stable_below_the_limit(self):
        self.assertLess(self.growth(0.99), 10.0)

    def test_unstable_above_the_limit(self):
        self.assertGreater(self.growth(1.05), 1e6)

    def test_run_uses_cfl_by_default(self):
        sim = Simulation(self.grid, self.s)
        self.assertAlmostEqual(sim.dt, 0.95 * self.grid.courant_dt())
        res = sim.run([], [], [1e10], StopRule(max_steps=10))
        self.assertEqual(res.steps, 10)


if __name__ == "__main__":
    unittest.main()
