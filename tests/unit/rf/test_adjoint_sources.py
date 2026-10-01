"""Nuttall-basis adjoint sources (design §6.5): exact at the objective frequencies, band-limited,
free of low-frequency content, with a conditioning fallback."""

from __future__ import annotations

import unittest

import numpy as np

from yapnr.rf.fdtd.sources import NuttallFit, SpectralSource, nuttall, window_length

DT = 0.465e-12
OMEGA = 2 * np.pi * np.array([8.5e9, 9.0e9, 9.5e9, 10.0e9, 10.5e9, 11.0e9, 11.5e9])


class NuttallFitTest(unittest.TestCase):
    def test_window(self):
        w = nuttall(100)
        self.assertEqual(w.size, 101)
        self.assertAlmostEqual(w[0], 0.0, places=12)
        self.assertAlmostEqual(w[-1], 0.0, places=12)
        self.assertAlmostEqual(w[50], 1.0, places=5)
        self.assertEqual(window_length(OMEGA, DT), int(np.ceil(1.0 / (0.5e9 * DT))))

    def _check_exact(self, magnetic):
        fit = NuttallFit.build(OMEGA, DT, magnetic)
        rng = np.random.default_rng(0)
        req = rng.standard_normal((5, OMEGA.size)) + 1j * rng.standard_normal((5, OMEGA.size))
        coef = fit.coefficients(req)
        got = fit.realized(coef, OMEGA)
        self.assertLess(np.abs(got - req).max() / np.abs(req).max(), 1e-10)
        # The series from `series(n)` is the one the DTFT was taken of.
        src = SpectralSource("hx" if magnetic else "ex", np.arange(5), coef, fit)
        s = np.array([src.values(n) for n in range(fit.n_window + 1)])
        self.assertIsNone(src.values(fit.n_window + 1))
        t = (np.arange(fit.n_window + 1) + (0.0 if magnetic else 0.5)) * DT
        dtft = DT * np.exp(1j * np.outer(OMEGA, t)) @ s
        np.testing.assert_allclose(dtft.T, req, atol=1e-10 * np.abs(req).max())
        return fit, coef, s, t

    def test_exact_half_step(self):
        self._check_exact(magnetic=False)

    def test_exact_integer_step(self):
        self._check_exact(magnetic=True)

    def test_low_frequency_moments_vanish(self):
        _, _, s, t = self._check_exact(magnetic=False)
        u = (t - t.mean()) / (t[-1] - t[0])
        scale = np.abs(s).sum()
        for p in range(3):
            self.assertLess(abs(np.sum(s * u[:, None] ** p, axis=0)).max() / scale, 1e-12)

    def test_out_of_band(self):
        # Beyond the window's main lobe (4 bins of 0.5 GHz) past the band edges the realized
        # spectrum sits at the Nuttall sidelobe level (measured: -87 dB at 6 GHz, -88 dB at
        # 14 GHz, -96 dB at 20 GHz for unit requests).
        fit = NuttallFit.build(OMEGA, DT, magnetic=False)
        coef = fit.coefficients(np.ones((1, OMEGA.size)))
        far = 2 * np.pi * np.array([4e9, 6e9, 14e9, 20e9, 30e9])
        band = np.abs(fit.realized(coef, OMEGA)).max()
        level = 20 * np.log10(np.abs(fit.realized(coef, far)).max() / band)
        self.assertLess(level, -85.0)

    def test_condition_fallback_doubles_the_window(self):
        n0 = window_length(OMEGA, DT)
        fit = NuttallFit.build(OMEGA, DT, False, n_window=n0 // 4, max_cond=1e3)
        self.assertEqual(fit.n_window, 4 * (n0 // 4))  # 1/4 and 1/2 of N are ill-conditioned
        self.assertLessEqual(fit.cond, 1e3)
        with self.assertRaises(RuntimeError):
            NuttallFit.build(OMEGA, DT, False, n_window=n0 // 4, max_cond=1e3, max_doublings=1)


if __name__ == "__main__":
    unittest.main()
