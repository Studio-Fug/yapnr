"""The copper sheet model (design §4.3, §7.4): a G_max sheet behaves like metal, conductance is
averaged onto edges, and G(ρ̄) is monotone and log-symmetric about 1/η0."""

from __future__ import annotations

import unittest

import numpy as np

from yapnr.rf import sparams
from yapnr.rf.constants import ETA0
from yapnr.rf.domain import Domain, DomainSpec, PortSpec
from yapnr.rf.fdtd.engine import Simulation
from yapnr.rf.fdtd.sources import GaussianPulse
from yapnr.rf.fdtd.stop import StopRule
from yapnr.rf.materials import (
    pixels_to_edges,
    sheet_conductance,
    sheet_conductance_derivative,
)
from yapnr.rf.ports import LineCalibration
from yapnr.rf.stackup import Stackup

S1 = Stackup(3.55, 0.0027, 0.813e-3, 10e9)


class InterpolationTest(unittest.TestCase):
    def test_endpoints_monotone_log_symmetric(self):
        rho = np.linspace(0, 1, 101)
        g = sheet_conductance(rho, S1.g_min, S1.g_max)
        self.assertAlmostEqual(g[0], S1.g_min)
        self.assertAlmostEqual(g[-1], S1.g_max)
        self.assertTrue(np.all(np.diff(g) > 0))
        np.testing.assert_allclose(g * g[::-1], 1.0 / ETA0**2, rtol=1e-12)
        self.assertAlmostEqual(g[50] * ETA0, 1.0)

    def test_copper_numbers(self):
        # R_s(10 GHz) = 26.1 mΩ, G_max η0 = 1.44e4 (design §4.3).
        self.assertAlmostEqual(S1.surface_resistance, 26.1e-3, delta=0.1e-3)
        self.assertAlmostEqual(S1.g_max * ETA0 / 1e4, 1.44, delta=0.01)

    def test_derivative(self):
        rho = np.linspace(0.05, 0.95, 7)
        for g_d in (0.0, 0.3):
            h = 1e-6
            fd = (
                sheet_conductance(rho + h, S1.g_min, S1.g_max, g_d)
                - sheet_conductance(rho - h, S1.g_min, S1.g_max, g_d)
            ) / (2 * h)
            an = sheet_conductance_derivative(rho, S1.g_min, S1.g_max, g_d)
            np.testing.assert_allclose(an, fd, rtol=1e-7)

    def test_torch_matches_numpy(self):
        import torch

        rho = np.linspace(0, 1, 11)
        got = sheet_conductance(torch.as_tensor(rho), S1.g_min, S1.g_max, 0.2).numpy()
        np.testing.assert_allclose(got, sheet_conductance(rho, S1.g_min, S1.g_max, 0.2))


class SheetVersusPecTest(unittest.TestCase):
    """A strip of G_max sheet against the same strip as hard PEC edges."""

    def run_line(self, pec: bool):
        spec = DomainSpec(
            S1,
            0.3e-3,
            4,
            (0.0, 4.8e-3, -2.4e-3, 2.4e-3),
            (PortSpec(1, "W", 0.0, 6), PortSpec(2, "E", 0.0, 6)),
            f_max=12.5e9,
            air=5.5e-3,
            dz_max=0.8e-3,
            meas_cells=6,
            src_cells=13,
        )
        dom = Domain(spec)
        grid = dom.grid
        p1, p2 = dom.ports
        i0, i1, j0, j1 = dom.window
        gd = np.zeros(dom.design_shape)
        gd[:, p1.ta - j0 : p1.tb - j0] = S1.g_max
        if pec:
            st = dom.structure()
            full = dom.pixels(gd)
            full[dom.feed] = 0.0
            gx, gy = pixels_to_edges(grid, full)
            kc = grid.k_c
            for comp, gg in (("ex", gx), ("ey", gy)):
                i, j = np.nonzero(gg > 0)
                st.set_pec(comp, grid.flat_index(comp, i, j, kc))
        else:
            st = dom.structure(gd)
        dt = 0.95 * grid.courant_dt()
        sim = Simulation(grid, st, dt=dt, dtype=np.float64)
        omega = 2 * np.pi * np.array([8e9, 10e9, 12e9])
        res = sim.run(
            p1.mode_sources(GaussianPulse.for_band(8e9, 12e9), dt),
            p1.probes + p2.probes,
            omega,
            StopRule(tol=1e-6, f_lo=8e9),
        )
        cal = LineCalibration(omega, np.full(3, 48.0 + 0j), omega * 1.7 / 3e8, dt)
        a1, _ = sparams.port_waves(p1, cal, res.dft, omega)
        _, b2 = sparams.port_waves(p2, cal, res.dft, omega)
        return b2 / a1

    def test_sheet_matches_pec(self):
        sheet = self.run_line(pec=False)
        pec = self.run_line(pec=True)
        ddb = np.abs(sparams.db(sheet) - sparams.db(pec))
        dphase = np.abs(np.angle(sheet / pec, deg=True))
        self.assertLess(ddb.max(), 0.02, ddb)
        self.assertLess(dphase.max(), 0.5, dphase)


if __name__ == "__main__":
    unittest.main()
