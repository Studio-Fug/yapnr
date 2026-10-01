"""Microstrip line against closed forms, and a matched line through the design region
(design §5.3, §12).

Stackup S1 (εr 3.55, h 0.813 mm), 0.3 mm cells, 4 substrate cells, a 6-cell (1.8 mm) strip:
Hammerstad–Jensen gives Z0 = 50.3 Ω, ε_eff = 2.784.

Measured on this grid (torch float64), the basis of the tolerances (measurement + 50 %):

- Z_c is 6.4 % (2 GHz) to 5.8 % (4 GHz) below Hammerstad–Jensen: the zero-thickness strip on
  6 cells is electrically about half a cell wider than its pixels (edge singularity);
- ε_eff is within 0.5 % of Kirschning–Jansen from 2 to 12 GHz; Im Z_c is up to 2.2 % of Z0;
- a straight line through a 9.6 mm design region: |S11| ≤ −41.8 dB, |S21| ≥ −0.13 dB (the line
  loss plus the de-embedding error of the calibrated Im k near the source), ∠S21 within 1.8° of
  −Re(k) L (engineering convention).

The attenuation of this line rises from 0.55 Np/m at 2 GHz to about 2 Np/m at 10 GHz on both
this grid and one twice as fine: with a constant sheet conductance G_max = 1/R_s(10 GHz) the
current crowds towards the strip edges as frequency rises, so the zero-thickness sheet has
several times the textbook conductor loss of a thick strip.
"""

from __future__ import annotations

import os
import unittest

import numpy as np

from yapnr.rf import sparams
from yapnr.rf.domain import Domain, DomainSpec, PortSpec
from yapnr.rf.fdtd.engine import Simulation
from yapnr.rf.fdtd.sources import GaussianPulse
from yapnr.rf.fdtd.stop import StopRule
from yapnr.rf.ports import calibrate_line
from yapnr.rf.stackup import Stackup, hammerstad_jensen, kirschning_jansen_eps_eff

S1 = Stackup(3.55, 0.0027, 0.813e-3, 10e9)
FREQS = np.array([2, 3, 4, 6, 8, 10, 12]) * 1e9
LENGTH = 9.6e-3


def line_domain(cells_per_width: int, n_sub: int) -> Domain:
    pitch = 1.8e-3 / cells_per_width
    spec = DomainSpec(
        S1,
        pitch,
        n_sub,
        (0.0, LENGTH, -3.0e-3, 3.0e-3),
        (PortSpec(1, "W", 0.0, cells_per_width), PortSpec(2, "E", 0.0, cells_per_width)),
        f_max=12.5e9,
        air=5.5e-3,
        dz_max=0.8e-3,
        meas_cells=int(round(2.7e-3 / pitch)),
        src_cells=int(round(5.1e-3 / pitch)),
    )
    return Domain(spec)


def measure(cells_per_width: int, n_sub: int):
    dom = line_domain(cells_per_width, n_sub)
    grid = dom.grid
    dt = 0.95 * grid.courant_dt()
    omega = 2 * np.pi * FREQS
    cal = calibrate_line(dom.line_spec(cells_per_width, dt), omega, backend="torch")
    p1, p2 = dom.ports
    gd = np.zeros(dom.design_shape)
    i0, i1, j0, j1 = dom.window
    gd[:, p1.ta - j0 : p1.tb - j0] = S1.g_max
    sim = Simulation(grid, dom.structure(gd), dt=dt, backend="torch", dtype=np.float64)
    res = sim.run(
        p1.mode_sources(GaussianPulse.for_band(2e9, 12e9), dt),
        p1.probes + p2.probes,
        omega,
        StopRule(tol=1e-5, f_lo=2e9),
    )
    a1, b1 = sparams.port_waves(p1, cal, res.dft, omega)
    _, b2 = sparams.port_waves(p2, cal, res.dft, omega)
    return cal, b1 / a1, b2 / a1


class MicrostripTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cal, cls.s11, cls.s21 = measure(6, 4)

    def test_impedance_vs_hammerstad_jensen(self):
        z0, _ = hammerstad_jensen(1.8e-3, S1.h, S1.er)
        low = FREQS <= 4e9
        err = np.abs(self.cal.zc.real[low] / z0 - 1)
        self.assertLess(err.max(), 0.095, err)
        self.assertLess(np.abs(self.cal.zc.imag).max() / z0, 0.035)  # measured 2.2 %

    def test_eps_eff_vs_kirschning_jansen(self):
        kj = np.array([kirschning_jansen_eps_eff(1.8e-3, S1.h, S1.er, f) for f in FREQS])
        err = np.abs(self.cal.eps_eff() / kj - 1)
        self.assertLess(err.max(), 0.0075, err)

    def test_matched_line(self):
        self.assertLess(sparams.db(self.s11).max(), -35.0)
        self.assertGreater(sparams.db(self.s21).min(), -0.2)
        self.assertLess(sparams.db(self.s21).max(), 0.01)
        phase = np.angle(sparams.to_engineering(self.s21), deg=True)
        expect = np.rad2deg(-self.cal.k.real * LENGTH)
        err = (phase - expect + 180.0) % 360.0 - 180.0
        self.assertLess(np.abs(err).max(), 2.6, err)


@unittest.skipUnless(os.environ.get("YAPNR_RF_SLOW"), "slow: the :test_microstrip_slow target")
class MicrostripRefinementTest(unittest.TestCase):
    """The impedance error drops with resolution.

    Measured: Z_c − Z_HJ at 2–4 GHz is −6.4 % at 6 cells per width and 4 substrate cells and
    −3.3 % at 12 cells and 8 substrate cells (first order: the strip-edge singularity); ε_eff
    stays within 0.4 % of Kirschning–Jansen.
    """

    def test_error_drops_with_resolution(self):
        z0, _ = hammerstad_jensen(1.8e-3, S1.h, S1.er)
        low = FREQS <= 4e9
        coarse, _, _ = measure(6, 4)
        fine, _, _ = measure(12, 8)
        e_coarse = np.abs(coarse.zc.real[low] / z0 - 1).max()
        e_fine = np.abs(fine.zc.real[low] / z0 - 1).max()
        self.assertLess(e_fine, 0.7 * e_coarse)
        kj = np.array([kirschning_jansen_eps_eff(1.8e-3, S1.h, S1.er, f) for f in FREQS])
        self.assertLess(np.abs(fine.eps_eff() / kj - 1).max(), 0.006)


if __name__ == "__main__":
    unittest.main()
