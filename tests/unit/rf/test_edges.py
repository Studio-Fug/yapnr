"""The copper-edge correction (`yapnr.rf.edges`): factors, pixel maps, gradients, time step,
and the convergence it buys.

The convergence check uses the discrete line mode (`yapnr.rf.modes`) of the S1 1.8 mm line
(εr 3.55, h 0.813 mm) at 0.3 mm with 4 substrate cells against 0.1 mm with 8 (a third of the
pitch, the validator's "finer" grid). Measured at 2, 4, 8, 10 and 12 GHz:

- uncorrected: ε_eff +0.55 to +0.76 %, the power–current impedance −3.8 to −4.0 %;
- corrected (first ring, next ring, corners): ε_eff +0.06 to +0.21 %, the impedance −0.13 to
  +0.13 % (first ring alone: +0.08 to +0.24 % and +0.11 to +0.36 %); at a sixth of the pitch
  (16 substrate cells) the corrected values move by at most 0.02 % (ε_eff) and 0.04 %
  (impedance) from the third, so the coarse grid is within about 0.2 % of converged.
"""

from __future__ import annotations

import math
import unittest

import numpy as np

from yapnr.rf.edges import (
    EdgeConstants,
    factor_maps,
    kappa_inplane,
    kappa_normal,
    kappa_slot,
    stable_dt,
    vjp,
)
from yapnr.rf.materials import Structure
from yapnr.rf.mesh import Axis, Grid, PMLCells, substrate_z_axis, uniform_nodes
from yapnr.rf.stackup import Stackup

S1 = Stackup(3.55, 0.0027, 0.813e-3, 10e9)


def _quad(fn, a, b, n=4001):
    """∫_a^b fn with the substitution u² = s − a (integrable s^(−½) singularity at a)."""
    u = np.linspace(0.0, math.sqrt(b - a), n)[1:]
    v = fn(a + u * u) * 2.0 * u
    v = np.concatenate([[2.0 * v[0] - v[1]], v])  # the finite limit at u = 0
    u = np.concatenate([[0.0], u])
    return float(np.sum(0.5 * (v[1:] + v[:-1]) * np.diff(u)))


class KappaTest(unittest.TestCase):
    def test_square_cells(self):
        # Δz = g: the classic values of the knife edge and the slot line.
        self.assertAlmostEqual(float(kappa_inplane(1.0, 1.0, 1.0)), 0.6436, places=4)
        self.assertAlmostEqual(float(kappa_normal(1.0, 1.0)), 0.6436, places=4)
        self.assertAlmostEqual(float(kappa_slot(1.0, 1.0, 1.0)), 2 * math.asinh(1) / math.pi)

    def test_inplane_against_quadrature(self):
        # E_s ∝ Re (s + iz)^(−½) of φ = Re √w; e over [0, g], d over z ∈ ±Δz/2 at s = g/2,
        # with a dielectric below.
        g, dz, er = 0.3, 0.2033, 3.55

        def es(s, z):
            return np.real((s + 1j * z + 0j) ** -0.5)

        e = _quad(lambda s: es(s, 0.0), 0.0, g)
        zz = np.linspace(-dz / 2, dz / 2, 20001)
        v = es(g / 2, zz) * np.where(zz < 0, er, 1.0)
        d = float(np.sum(0.5 * (v[1:] + v[:-1]) * np.diff(zz)))
        ref = d / (0.5 * (er + 1.0) * dz * e / g)
        self.assertAlmostEqual(float(kappa_inplane(g, dz, dz, er, 1.0)), ref, places=4)

    def test_normal_against_quadrature(self):
        g, dz = 0.3, 0.2033

        def ez(s, z):
            return np.imag((s + 1j * z + 0j) ** -0.5)

        e = _quad(lambda z: np.abs(ez(0.0, z)), 0.0, dz)
        ss = np.linspace(-g / 2, g / 2, 40001)
        v = np.abs(ez(ss, dz / 2))
        d = float(np.sum(0.5 * (v[1:] + v[:-1]) * np.diff(ss)))
        self.assertAlmostEqual(float(kappa_normal(g, dz)), (d / g) / (e / dz), places=5)


class CornerTest(unittest.TestCase):
    def test_sector_exponents_and_factors(self):
        from yapnr.rf.edges import _sector_field, _sector_kappa, corner_ratios

        nu = {s: _sector_field(s)[0] for s in (90.0, 180.0, 270.0)}
        self.assertAlmostEqual(nu[180.0], 0.5, delta=0.006)  # the straight edge
        self.assertAlmostEqual(nu[90.0], 0.2966, delta=0.006)  # Morrison and Lewis
        self.assertGreater(nu[270.0], 0.75)
        # The 180° sector reproduces the closed-form κ_n within the sphere grid's error.
        k180 = _sector_kappa(_sector_field(180.0), 0.3 / 0.2033)
        self.assertAlmostEqual(k180 / float(kappa_normal(0.3, 0.2033)), 1.0, delta=0.015)
        cx, cc = corner_ratios(np.array([0.3]), 0.2033)
        self.assertAlmostEqual(float(cx[0]), 0.624, delta=0.01)
        self.assertAlmostEqual(float(cc[0]), 1.478, delta=0.02)

    def test_corner_nodes(self):
        grid = plane_grid()
        k = EdgeConstants.build(grid, S1.er)
        c = np.zeros((8, 8))
        c[2:6, 2:6] = 1.0  # a square: convex corners at nodes (2, 2) … (6, 6)
        m = factor_maps(c, k)
        np.testing.assert_allclose(m["ez"][2, 2, 1], k.ez_corner[1][0][2, 2])
        np.testing.assert_allclose(m["ez"][6, 6, 0], k.ez_corner[0][0][6, 6])
        np.testing.assert_allclose(m["ez"][4, 2, 1], k.ez[1][4, 2])  # an edge node
        h = np.ones((8, 8))
        h[2:6, 2:6] = 0.0  # a hole: concave corners
        m = factor_maps(h, k)
        np.testing.assert_allclose(m["ez"][2, 2, 1], k.ez_corner[1][1][2, 2])


def plane_grid(n=8, pitch=0.3e-3, n_sub=4):
    ax = Axis(uniform_nodes(0.0, n * pitch, pitch))
    z, kc = substrate_z_axis(S1.h, n_sub, 4 * S1.h, dz_max=1e-3, n_pml=0)
    return Grid(ax, ax, z, kc, PMLCells(0, 0, 0, 0, 0))


class FactorMapTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.grid = plane_grid()
        cls.k = EdgeConstants.build(cls.grid, S1.er)

    def test_straight_edge(self):
        c = np.zeros((8, 8))
        c[:, :4] = 1.0  # copper for y < y_4: an edge along x on node line j = 4
        m = factor_maps(c, self.k)
        kn = float(self.k.ez[1][3, 4])
        kt = float(self.k.ey[0][0, 4])
        kv, km = (float(v[3, 4]) for v in self.k.ez2[1])
        # E_z on the edge's nodes (interior ones), the next ring one cell into the void and
        # under the copper, 1 further away.
        np.testing.assert_allclose(m["ez"][2:6, 4, 1], kn)
        np.testing.assert_allclose(m["ez"][2:6, 5, 1], kv)
        np.testing.assert_allclose(m["ez"][2:6, 3, 1], km)
        self.assertGreater(kv, 1.1)  # 1.21 here
        np.testing.assert_allclose(m["ez"][2:6, 2, 1], 1.0)
        np.testing.assert_allclose(m["ez"][2:6, 6, 1], 1.0)
        # E_y of the void cell next to the edge; E_x is along the edge (no correction).
        np.testing.assert_allclose(m["ey"][2:6, 4, 0], kt)
        np.testing.assert_allclose(m["ey"][2:6, 5, 0], 1.0)
        np.testing.assert_allclose(m["ex"][:, :, 0], 1.0)
        # H_z of the void pixel next to the edge, H_y above and below the edge, H_x none.
        np.testing.assert_allclose(m["hz"][2:6, 4, 0], float(self.k.hz[0][3, 4]))
        np.testing.assert_allclose(m["hz"][2:6, 5, 0], 1.0)
        np.testing.assert_allclose(m["hy"][:, 4, :], float(self.k.hy[0][0, 4]))
        np.testing.assert_allclose(m["hy"][:, 5, :], float(self.k.hy2[0][0][0, 5]))
        np.testing.assert_allclose(m["hy"][:, 3, :], float(self.k.hy2[0][1][0, 3]))
        np.testing.assert_allclose(m["hy"][:, 6, :], 1.0)
        np.testing.assert_allclose(m["hx"][2:-2], 1.0)  # (the plane's rim is an edge too)

    def test_slot_and_uniform(self):
        c = np.ones((8, 8))
        c[:, 4] = 0.0  # a one-pixel slot
        m = factor_maps(c, self.k)
        np.testing.assert_allclose(m["ey"][2:6, 4, 0], float(self.k.ey[1][0, 4]))
        np.testing.assert_allclose(m["hz"][2:6, 4, 0], float(self.k.hz[1][3, 4]))
        for v in factor_maps(np.zeros((8, 8)), self.k).values():
            np.testing.assert_allclose(v, 1.0)
        for name, v in factor_maps(np.ones((8, 8)), self.k).items():
            inner = v[2:-2, 2:-2] if name != "hz" else v  # (the plane's rim is an edge)
            np.testing.assert_allclose(inner, 1.0, err_msg=name)

    def test_vjp_against_finite_differences(self):
        rng = np.random.default_rng(1)
        c = rng.uniform(0, 1, (8, 8))
        kern = {k: rng.standard_normal((2,) + v.shape) for k, v in factor_maps(c, self.k).items()}
        g = vjp(c, self.k, kern)
        dc = rng.standard_normal(c.shape)
        h = 1e-6

        def total(cc):
            maps = factor_maps(cc, self.k)
            return np.array([sum(np.sum(kern[k][m] * maps[k]) for k in maps) for m in range(2)])

        fd = (total(c + h * dc) - total(c - h * dc)) / (2 * h)
        np.testing.assert_allclose(np.sum(g * dc, axis=(1, 2)), fd, rtol=1e-7)


class TimeStepTest(unittest.TestCase):
    def test_bound_holds_for_dense_patterns(self):
        from yapnr.rf.fdtd.stability import max_eigenvalue

        grid = plane_grid(16)
        dt = stable_dt(grid, S1.er, 0.95)
        self.assertLessEqual(dt, 0.95 * grid.courant_dt() * (1 + 1e-12))
        for pattern in (
            (np.arange(16)[:, None] % 2) * np.ones((1, 16)),
            ((np.arange(16)[:, None] + np.arange(16)[None, :]) % 2).astype(float),
            np.random.default_rng(2).uniform(0, 1, (16, 16)),
        ):
            st = Structure(grid, S1)
            st.set_edge_correction(pattern)
            lam, _ = max_eigenvalue(grid, st, tol=1e-7, max_iter=2000)
            self.assertLess(dt, 0.95 * 2.0 / math.sqrt(lam) * 1.0001)


class ConvergenceTest(unittest.TestCase):
    """ε_eff and the power–current impedance of the discrete line mode, coarse against a third
    of the pitch (see the module doc for the measured numbers)."""

    @staticmethod
    def mode(ref, n_sub, corr, freqs):
        from yapnr.rf.modes import _ring_current, cross_section, line_mode
        from yapnr.rf.ports import LinePort, LineSpec, calibration_grid

        spec = LineSpec(S1, 0.3e-3 / ref, n_sub, 6 * ref, air=7 * S1.h, dz_max=1.6e-3, dt=1.0)
        grid, centre = calibration_grid(spec, 40)
        dt = 0.95 * grid.courant_dt()
        p = LinePort(1, "W", centre, 6 * ref, grid.x.nodes[30], 2, 5).on(grid)
        cs = cross_section(grid, S1, 0, p.ta, p.tb, edge_correction=corr)
        ee, zz = [], []
        for f in freqs:
            m = line_mode(cs, S1, 2 * np.pi * f, dt)
            pt = np.sum(m.et * np.conj(m.hz) * grid.y.primary[:, None] * grid.z.dual[None, :])
            pz = np.sum(m.ez * np.conj(m.ht) * grid.y.dual[:, None] * grid.z.primary[None, :])
            power = 0.5 * np.real(pt - pz) * np.cos(0.5 * m.k.real * cs.pitch_a)
            ee.append(m.eps_eff())
            zz.append(2 * power / abs(_ring_current(cs, m)) ** 2)
        return np.array(ee), np.array(zz)

    def test_coarse_against_a_third_of_the_pitch(self):
        freqs = np.array([2e9, 10e9, 12e9])
        out = {}
        for corr in (False, True):
            e1, z1 = self.mode(1, 4, corr, freqs)
            e3, z3 = self.mode(3, 8, corr, freqs)
            out[corr] = (np.abs(e1 / e3 - 1), np.abs(z1 / z3 - 1))
        self.assertGreater(out[False][1].min(), 0.035)  # measured 3.8–4.0 %
        self.assertGreater(out[False][0].min(), 0.005)  # measured 0.55–0.76 %
        self.assertLess(out[True][1].max(), 0.0025)  # measured 0.02–0.13 %
        self.assertLess(out[True][0].max(), 0.0035)  # measured 0.06–0.21 %


if __name__ == "__main__":
    unittest.main()
