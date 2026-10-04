"""The conic filter, the tanh projection and the parameterization chain (design §7.2, §7.3)."""

from __future__ import annotations

import math
import unittest

import numpy as np
import torch

from yapnr.rf.design.filters import ConicFilter, conic_kernel, kernel_half_width, ring_width_for
from yapnr.rf.design.material_grid import MaterialGrid
from yapnr.rf.design.metrics import gray_measure
from yapnr.rf.design.parameterization import Parameterization
from yapnr.rf.design.projection import tanh_projection, tanh_projection_derivative


class ConicFilterTest(unittest.TestCase):
    def test_kernel(self):
        self.assertEqual(kernel_half_width(0.6e-3, 0.3e-3), 1)
        self.assertEqual(kernel_half_width(0.75e-3, 0.3e-3), 2)
        self.assertEqual(ring_width_for(0.6e-3, 0.3e-3), 3)
        k = conic_kernel(0.75e-3, 0.3e-3)
        self.assertEqual(k.shape, (5, 5))
        self.assertAlmostEqual(float(k.sum()), 1.0, places=14)
        np.testing.assert_allclose(k, k[::-1, ::-1])
        np.testing.assert_allclose(k, k.T)
        self.assertGreater(k[2, 2], k[2, 3])
        np.testing.assert_array_equal(conic_kernel(0.0, 1.0), [[1.0]])

    def test_keeps_constants(self):
        f = ConicFilter(2.5, 1.0)
        ext = np.full((12, 10), 0.7)
        out = f(ext)
        self.assertEqual(out.shape, (12 - 2 * f.half, 10 - 2 * f.half))
        np.testing.assert_allclose(out, 0.7, atol=1e-14)

    def test_numpy_matches_torch(self):
        f = ConicFilter(2.2, 1.0)
        ext = np.random.default_rng(0).random((15, 11))
        np.testing.assert_allclose(f(ext), f(torch.as_tensor(ext)).numpy(), atol=1e-14)

    def test_self_adjoint_with_zero_ring(self):
        f = ConicFilter(2.5, 1.0)
        n, m = 9, 7
        r = f.half

        def apply(x):
            ext = np.zeros((n + 2 * r, m + 2 * r))
            ext[r:-r, r:-r] = x
            return f(ext)

        rng = np.random.default_rng(1)
        x, y = rng.standard_normal((n, m)), rng.standard_normal((n, m))
        self.assertAlmostEqual(float(np.sum(apply(x) * y)), float(np.sum(x * apply(y))), places=12)

    def test_ring_feeds_the_filter(self):
        f = ConicFilter(2.0, 1.0)
        r = f.ring_width
        ring = np.zeros((6 + 2 * r, 6 + 2 * r))
        ring[:r, r + 2 : r + 4] = 1.0
        g = MaterialGrid((6, 6), ring=ring, ring_width=r)
        p = Parameterization(g, f)
        st = p.forward(np.zeros(g.n_dof), 8.0)
        rt = st["rho_tilde"].numpy()
        self.assertGreater(rt[0, 2], 0.1)  # next to the feed
        self.assertEqual(float(rt[-1].max()), 0.0)  # the far edge sees no copper
        self.assertEqual(p.margin, 2)


class ProjectionTest(unittest.TestCase):
    def test_fixed_points(self):
        for beta in (1.0, 8.0, 128.0):
            for eta in (0.3, 0.5, 0.75):
                v = tanh_projection(np.array([0.0, 1.0]), beta, eta)
                np.testing.assert_allclose(v, [0.0, 1.0], atol=1e-12)
            self.assertAlmostEqual(float(tanh_projection(np.array(0.5), beta, 0.5)), 0.5)

    def test_heaviside(self):
        x = np.array([0.0, 0.49, 0.5, 0.51, 1.0])
        np.testing.assert_array_equal(tanh_projection(x, math.inf, 0.5), [0, 0, 0, 1, 1])
        t = tanh_projection(torch.as_tensor(x), math.inf, 0.5).numpy()
        np.testing.assert_array_equal(t, [0, 0, 0, 1, 1])
        self.assertTrue(np.all(tanh_projection_derivative(x, math.inf) == 0))

    def test_derivative_and_monotone(self):
        x = np.linspace(0.01, 0.99, 37)
        for beta in (2.0, 16.0, 64.0):
            h = 1e-6
            fd = (tanh_projection(x + h, beta) - tanh_projection(x - h, beta)) / (2 * h)
            np.testing.assert_allclose(
                tanh_projection_derivative(x, beta), fd, rtol=1e-6, atol=1e-8
            )
            self.assertTrue(np.all(np.diff(tanh_projection(x, beta)) >= 0))
            self.assertTrue(np.all(np.diff(tanh_projection(np.linspace(0.45, 0.55, 11), beta)) > 0))
        xt = torch.as_tensor(x).requires_grad_(True)
        tanh_projection(xt, 16.0).sum().backward()
        np.testing.assert_allclose(
            xt.grad.numpy(), tanh_projection_derivative(x, 16.0), rtol=1e-9, atol=1e-12
        )

    def test_gray_measure(self):
        self.assertEqual(gray_measure(np.array([0.0, 1.0, 1.0])), 0.0)
        self.assertAlmostEqual(gray_measure(np.full(4, 0.5)), 1.0)
        mask = np.array([True, False])
        self.assertEqual(gray_measure(np.array([1.0, 0.5]), mask), 0.0)


class ParameterizationTest(unittest.TestCase):
    def _param(self, sym="mirror_y"):
        f = ConicFilter(1.6, 1.0)
        r = f.ring_width
        fixed = np.zeros((7, 6), bool)
        fixed[0:2, 2:4] = True
        ring = np.zeros((7 + 2 * r, 6 + 2 * r))
        ring[:r, r + 2 : r + 4] = 1.0
        g = MaterialGrid(
            (7, 6),
            fixed=fixed,
            fixed_value=fixed.astype(float),
            ring=ring,
            ring_width=r,
            symmetry=sym,
        )
        return Parameterization(g, f)

    def test_vjp_against_finite_differences(self):
        p = self._param()
        rng = np.random.default_rng(5)
        x = rng.uniform(0.2, 0.8, p.n_dof)
        cot = rng.standard_normal((2,) + p.grid.shape)
        g = p.vjp(x, 8.0, cot)
        v = rng.standard_normal(p.n_dof)
        h = 1e-6
        d = (p.rho_bar(x + h * v, 8.0) - p.rho_bar(x - h * v, 8.0)) / (2 * h)
        for k in range(2):
            self.assertAlmostEqual(float(g[k] @ v), float(np.sum(cot[k] * d)), delta=1e-7)

    def test_fixed_reimposed_and_symmetric(self):
        p = self._param()
        rb = p.rho_bar(np.zeros(p.n_dof), 32.0)
        np.testing.assert_array_equal(rb[p.grid.fixed], 1.0)
        np.testing.assert_allclose(rb, rb[:, ::-1])
        g = p.vjp(np.full(p.n_dof, 0.5), 8.0, np.ones(p.grid.shape))
        self.assertEqual(g.shape, (p.n_dof,))

    def test_ring_too_narrow(self):
        with self.assertRaises(ValueError):
            Parameterization(MaterialGrid((4, 4), ring_width=1), ConicFilter(2.5, 1.0))


if __name__ == "__main__":
    unittest.main()
