"""Torch's complex autograd convention as used by `adjoint.wirtinger` (design §6.2).

For a real F, torch returns grad = 2 conj(∂F/∂q) (Wirtinger derivative, conjugate held fixed),
so ∂F/∂q = conj(grad)/2 and dF = 2 Re(∂F/∂q dq).
"""

from __future__ import annotations

import unittest

import numpy as np
import torch

from yapnr.rf.adjoint import wirtinger


class TorchConventionTest(unittest.TestCase):
    def test_abs_squared(self):
        q = np.array([[1.0 + 2.0j, -0.5 + 0.1j]])
        f, g = wirtinger(lambda d: (torch.abs(d["q"]) ** 2).sum(dim=1), {"q": q}, ["q"])
        np.testing.assert_allclose(f, [np.sum(np.abs(q) ** 2)])
        np.testing.assert_allclose(g["q"], np.conj(q))

    def test_real_part_of_linear(self):
        c = np.array([0.3 - 1.1j, 2.0 + 0.5j])
        q = np.array([[1.0 + 2.0j, -0.5 + 0.1j]])
        ct = torch.as_tensor(c)
        _, g = wirtinger(lambda d: torch.real(d["q"] @ ct), {"q": q}, ["q"])
        np.testing.assert_allclose(g["q"][0], c / 2)

    def test_first_order_change(self):
        rng = np.random.default_rng(0)
        q = rng.standard_normal((2, 3)) + 1j * rng.standard_normal((2, 3))
        dq = 1e-7 * (rng.standard_normal((2, 3)) + 1j * rng.standard_normal((2, 3)))

        def fn(d):
            x = d["q"]
            return torch.abs(x[:, 0] * x[:, 1] + x[:, 2] ** 2) ** 2 + torch.real(x[:, 0])

        f0, g = wirtinger(fn, {"q": q}, ["q"])
        f1, _ = wirtinger(fn, {"q": q + dq}, ["q"])
        predicted = 2 * np.real(np.sum(g["q"] * dq, axis=1))
        np.testing.assert_allclose(f1 - f0, predicted, rtol=1e-5)


if __name__ == "__main__":
    unittest.main()
