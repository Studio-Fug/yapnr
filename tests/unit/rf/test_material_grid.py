"""The material grid: fixed pixels, the exterior ring, mirror symmetry on the DOF (design §7.1)."""

from __future__ import annotations

import unittest

import numpy as np
import torch

from yapnr.rf.design.material_grid import MaterialGrid


def _grid(sym="none", shape=(6, 8), r=2):
    fixed = np.zeros(shape, bool)
    value = np.zeros(shape)
    fixed[0:2, 3:5] = True  # a "pad" on the W edge, symmetric about the y centre line
    value[0:2, 3:5] = 1.0
    fixed[2, 0] = fixed[2, -1] = True  # keepouts (mirror pair in y)
    ring = np.zeros((shape[0] + 2 * r, shape[1] + 2 * r))
    ring[:r, r + 3 : r + 5] = 1.0  # the feed entering the pad
    return MaterialGrid(
        shape, fixed=fixed, fixed_value=value, ring=ring, ring_width=r, symmetry=sym
    )


class MaterialGridTest(unittest.TestCase):
    def test_embed_and_fixed(self):
        g = _grid()
        self.assertEqual(g.n_dof, 6 * 8 - 6)
        x = np.arange(g.n_dof, dtype=float) / g.n_dof
        rho = g.embed(x)
        np.testing.assert_array_equal(rho[g.fixed], g.fixed_value[g.fixed])
        np.testing.assert_allclose(np.sort(rho[g.free]), np.sort(x))
        np.testing.assert_allclose(g.restrict(rho), x)

    def test_torch_embed_matches_numpy(self):
        for sym in ("none", "mirror_y", "mirror_x", "mirror_xy"):
            g = _grid(sym, shape=(6, 8)) if sym in ("none", "mirror_y") else _sym_grid(sym)
            x = np.random.default_rng(1).random(g.n_dof)
            np.testing.assert_array_equal(g.embed(torch.as_tensor(x)).numpy(), g.embed(x))

    def test_transpose(self):
        rng = np.random.default_rng(2)
        for sym in ("none", "mirror_y"):
            g = _grid(sym)
            x = rng.random(g.n_dof)
            y = rng.standard_normal(g.shape)
            lin = g.embed(x) - g.embed(np.zeros(g.n_dof))  # the linear part
            lhs = float(np.sum(lin * y))
            rhs = float(np.dot(x, g.embed_transpose(y)))
            self.assertAlmostEqual(lhs, rhs, delta=1e-12 * max(1.0, abs(lhs)))
            batch = g.embed_transpose(np.stack([y, 2 * y]))
            np.testing.assert_allclose(batch[1], 2 * batch[0])

    def test_ring(self):
        g = _grid()
        rho = np.full(g.shape, 0.5)
        ext = g.extend(rho)
        r = g.ring_width
        np.testing.assert_array_equal(ext[r:-r, r:-r], rho)
        np.testing.assert_array_equal(ext[:r, r + 3 : r + 5], 1.0)
        self.assertEqual(float(ext[:r].sum()), 2 * r)
        ext_t = g.extend(torch.as_tensor(rho)).numpy()
        np.testing.assert_array_equal(ext_t, ext)

    def test_mirror_y_shares_dof(self):
        g = _grid("mirror_y")
        ni, nj = g.shape
        self.assertEqual(g.n_dof, (ni * nj - 6) // 2)
        np.testing.assert_array_equal(g.orbit_size, 2.0)
        x = np.random.default_rng(3).random(g.n_dof)
        rho = g.embed(x)
        np.testing.assert_array_equal(rho, rho[:, ::-1])

    def test_mirror_xy(self):
        g = _sym_grid("mirror_xy")
        x = np.random.default_rng(4).random(g.n_dof)
        rho = g.embed(x)
        np.testing.assert_array_equal(rho, rho[::-1, :])
        np.testing.assert_array_equal(rho, rho[:, ::-1])
        self.assertEqual(g.n_dof, 4 * 4)

    def test_odd_axis_centre_line(self):
        g = MaterialGrid((5, 5), symmetry="mirror_y")
        self.assertEqual(g.n_dof, 5 * 3)
        self.assertEqual(sorted(set(g.orbit_size)), [1.0, 2.0])

    def test_asymmetric_fixed_rejected(self):
        fixed = np.zeros((4, 4), bool)
        fixed[0, 0] = True
        with self.assertRaises(ValueError):
            MaterialGrid((4, 4), fixed=fixed, symmetry="mirror_y")
        ring = np.zeros((8, 8))
        ring[0, 2] = 1.0
        with self.assertRaises(ValueError):
            MaterialGrid((4, 4), ring=ring, ring_width=2, symmetry="mirror_y")

    def test_reimpose(self):
        g = _grid()
        rb = np.full(g.shape, 0.3)
        out = g.reimpose(rb)
        np.testing.assert_array_equal(out[g.fixed], g.fixed_value[g.fixed])
        np.testing.assert_array_equal(out[g.free], 0.3)
        out_t = g.reimpose(torch.as_tensor(rb)).numpy()
        np.testing.assert_array_equal(out_t, out)


def _sym_grid(sym):
    return MaterialGrid((8, 8), ring_width=2, symmetry=sym)


if __name__ == "__main__":
    unittest.main()
