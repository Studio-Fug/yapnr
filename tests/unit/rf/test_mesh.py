"""Graded axes, Yee grid geometry and the copper-plane pixel ↔ edge mapping."""

from __future__ import annotations

import unittest

import numpy as np

from yapnr.rf.materials import edges_to_pixels, pixels_to_edges
from yapnr.rf.mesh import Axis, Grid, PMLCells, graded_axis, substrate_z_axis, uniform_nodes


class AxisTest(unittest.TestCase):
    def test_primary_and_dual_lengths(self):
        ax = Axis(np.array([0.0, 1.0, 3.0, 6.0]))
        np.testing.assert_allclose(ax.primary, [1.0, 2.0, 3.0])
        np.testing.assert_allclose(ax.dual, [0.5, 1.5, 2.5, 1.5])
        self.assertAlmostEqual(ax.dual.sum(), ax.hi - ax.lo)
        np.testing.assert_allclose(ax.centers, [0.5, 2.0, 4.5])

    def test_rejects_non_increasing(self):
        with self.assertRaises(ValueError):
            Axis(np.array([0.0, 1.0, 1.0]))

    def test_index_lookup(self):
        ax = Axis(uniform_nodes(-1e-3, 2e-3, 0.25e-3))
        self.assertEqual(ax.node(0.0), 4)
        self.assertEqual(ax.node(2e-3), 12)
        with self.assertRaises(ValueError):
            ax.node(0.1e-3)
        self.assertEqual(ax.cell(0.1e-3), 4)
        self.assertEqual(ax.cell(-5.0), 0)
        self.assertEqual(ax.cell(5.0), ax.n - 1)

    def test_graded_axis(self):
        ax = graded_axis(
            0.0, 3e-3, 0.3e-3, -4e-3, 5e-3, max_cell=1e-3, ratio=1.25, n_pml_lo=4, n_pml_hi=3
        )
        p = ax.primary
        core = (ax.nodes >= -1e-12) & (ax.nodes <= 3e-3 + 1e-12)
        self.assertEqual(int(core.sum()), 11)
        self.assertLessEqual(ax.max_ratio(), 1.25 + 1e-12)
        self.assertLessEqual(p.max(), 1e-3 + 1e-15)
        self.assertLessEqual(ax.lo, -4e-3)
        self.assertGreaterEqual(ax.hi, 5e-3)
        # CPML cells repeat the last interior cell.
        np.testing.assert_allclose(p[:4], p[4])
        np.testing.assert_allclose(p[-3:], p[-4])

    def test_ungraded_side_keeps_the_pitch(self):
        ax = graded_axis(0.0, 3e-3, 0.3e-3, -3e-3, 6e-3, max_cell=1e-3, grade_lo=False)
        left = ax.primary[ax.nodes[:-1] < 0.0]
        np.testing.assert_allclose(left, 0.3e-3)

    def test_substrate_axis(self):
        z, kc = substrate_z_axis(0.813e-3, 4, 5.5e-3, dz_max=0.8e-3, n_pml=8)
        self.assertEqual(kc, 4)
        self.assertAlmostEqual(z.nodes[kc], 0.813e-3)
        np.testing.assert_allclose(z.primary[:4], 0.813e-3 / 4)
        self.assertAlmostEqual(z.primary[4], 0.813e-3 / 4)
        self.assertLessEqual(z.max_ratio(), 1.25 + 1e-12)
        np.testing.assert_allclose(z.primary[-8:], z.primary[-9])


class GridTest(unittest.TestCase):
    def setUp(self):
        x = Axis(np.array([0.0, 1.0, 2.5, 4.0, 5.0]))
        y = Axis(np.array([0.0, 0.5, 1.5, 2.0]))
        z = Axis(np.array([0.0, 0.2, 0.4, 0.9, 1.6]))
        self.grid = Grid(x, y, z, 2, PMLCells(1, 1, 1, 1, 1))

    def test_shapes(self):
        g = self.grid
        self.assertEqual(g.shape("ex"), (4, 4, 5))
        self.assertEqual(g.shape("ey"), (5, 3, 5))
        self.assertEqual(g.shape("ez"), (5, 4, 4))
        self.assertEqual(g.shape("hx"), (5, 3, 4))
        self.assertEqual(g.shape("hy"), (4, 4, 4))
        self.assertEqual(g.shape("hz"), (4, 3, 5))

    def test_volumes_tile_the_domain(self):
        g = self.grid
        total = (g.x.hi - g.x.lo) * (g.y.hi - g.y.lo) * (g.z.hi - g.z.lo)
        for comp in ("ex", "ey", "ez", "hx", "hy", "hz"):
            self.assertAlmostEqual(g.volume(comp).sum(), total, places=12, msg=comp)

    def test_flat_index_round_trip(self):
        g = self.grid
        idx = g.flat_index("ey", [1, 2], [0, 2], [3, 4])
        i, j, k = g.unravel("ey", idx)
        np.testing.assert_array_equal(i, [1, 2])
        np.testing.assert_array_equal(j, [0, 2])
        np.testing.assert_array_equal(k, [3, 4])

    def test_interior_box(self):
        (x0, x1), (y0, y1), (z0, z1) = self.grid.interior_box()
        self.assertEqual((x0, x1, y0, y1, z0, z1), (1.0, 4.0, 0.5, 1.5, 0.0, 0.9))


class PixelEdgeMapTest(unittest.TestCase):
    def setUp(self):
        x = Axis(np.cumsum(np.r_[0.0, [1.0, 1.0, 1.5, 2.0, 1.0, 1.0]]))
        y = Axis(np.cumsum(np.r_[0.0, [1.0, 0.7, 1.0, 1.0, 1.3]]))
        z = Axis(np.array([0.0, 0.5, 1.0, 2.0]))
        self.grid = Grid(x, y, z, 1, PMLCells(0, 0, 0, 0, 0))

    def test_constant_maps_to_constant(self):
        gx, gy = pixels_to_edges(self.grid, np.full(self.grid.n[:2], 3.0))
        np.testing.assert_allclose(gx, 3.0)
        np.testing.assert_allclose(gy, 3.0)

    def test_boundary_edges_get_half_the_copper(self):
        g = np.zeros(self.grid.n[:2])
        g[2:4, 1:3] = 1.0  # copper on x cells 2-3, y cells 1-2 (graded y: length weights)
        gx, gy = pixels_to_edges(self.grid, g)
        # Ex edge on the inner node j = 2 between two copper pixels: full copper.
        self.assertAlmostEqual(gx[2, 2], 1.0)
        # Ex edges on the strip's y boundaries carry the part of their dual width on copper.
        dy = self.grid.y.primary
        self.assertAlmostEqual(gx[2, 1], dy[1] / (dy[0] + dy[1]))
        self.assertAlmostEqual(gx[2, 3], dy[2] / (dy[2] + dy[3]))
        self.assertEqual(gx[2, 0], 0.0)

    def test_transpose(self):
        rng = np.random.default_rng(0)
        g = rng.standard_normal(self.grid.n[:2])
        gx, gy = pixels_to_edges(self.grid, g)
        ax = rng.standard_normal(gx.shape)
        ay = rng.standard_normal(gy.shape)
        lhs = float(np.sum(gx * ax) + np.sum(gy * ay))
        rhs = float(np.sum(g * edges_to_pixels(self.grid, ax, ay)))
        self.assertAlmostEqual(lhs, rhs, delta=1e-12 * abs(lhs))
        stacked = edges_to_pixels(self.grid, np.stack([ax, 2 * ax]), np.stack([ay, 2 * ay]))
        np.testing.assert_allclose(stacked[1], 2 * stacked[0])


if __name__ == "__main__":
    unittest.main()
