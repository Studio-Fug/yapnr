"""Power balance and passivity (design §5.6, §5.7).

- Closed box around a lossy gray design (sides down to the ground): outward Poynting flux plus
  the power dissipated inside is zero. With the trapezoid weights of `FluxBox` and edge weights
  ½ on faces (¼ on box edges) for the dissipation, this is the scheme's exact discrete Poynting
  theorem, so it holds to rounding.
- The port-wave power ½ Re(V Î*) differs from the Poynting flux through the feed cross-section
  by a few per cent (2.7–6 % at 8–12 GHz, the V/I definition of a dispersive quasi-TEM line);
  the calibration's power factor brings it within 1.3 %.
- A radiated-power box with its feed windows (|y − y_p| ≤ w/2 + 2h, z ≤ 3h) sees 0.6–1.4 % of
  the power of a matched straight line; larger windows do not reduce it (design target 1 %).
- Random gray 3-ports are passive: eig(I − SᴴS) ≥ −1e-3.
"""

from __future__ import annotations

import unittest

import numpy as np

from yapnr.rf import sparams
from yapnr.rf.domain import Domain, DomainSpec, PortSpec
from yapnr.rf.fdtd.engine import Simulation
from yapnr.rf.fdtd.monitors import FluxBox, dissipated_power, region_probes
from yapnr.rf.fdtd.sources import GaussianPulse
from yapnr.rf.fdtd.stop import StopRule
from yapnr.rf.materials import sheet_conductance
from yapnr.rf.ports import LineCalibration, calibrate_line
from yapnr.rf.stackup import Stackup

S1 = Stackup(3.55, 0.0027, 0.813e-3, 10e9)
OMEGA = 2 * np.pi * np.array([8e9, 10e9, 12e9])


def two_port(width=4.8e-3):
    spec = DomainSpec(
        S1,
        0.3e-3,
        4,
        (0.0, width, -2.4e-3, 2.4e-3),
        (PortSpec(1, "W", 0.0, 6), PortSpec(2, "E", 0.0, 6)),
        f_max=12.5e9,
        air=5.5e-3,
        dz_max=0.8e-3,
        meas_cells=6,
        src_cells=13,
    )
    return Domain(spec)


def box_edge_weights(grid, probes, node_box):
    """½ per box-boundary coordinate of each edge (faces ½, box edges ¼)."""
    out = []
    for p in probes:
        idx = grid.unravel(p.comp, p.index)
        w = np.ones(p.index.size)
        own = "xyz".index(p.comp[1])
        for a in range(3):
            if a == own:
                continue
            lo, hi = node_box[a]
            w *= np.where((idx[a] == lo) | (idx[a] == hi), 0.5, 1.0)
        out.append(w)
    return out


class ClosedBoxTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dom = cls.dom = two_port()
        g = dom.grid
        rng = np.random.default_rng(7)
        rho = rng.uniform(0.2, 0.9, dom.design_shape)
        cls.st = dom.structure(sheet_conductance(rho, S1.g_min, S1.g_max))
        dt = 0.95 * g.courant_dt()
        sim = Simulation(g, cls.st, dt=dt, dtype=np.float64)
        i0, i1, j0, j1 = dom.window
        kc = g.k_c
        cls.node_box = ((i0 - 2, i1 + 2), (j0 - 3, j1 + 3), (0, kc + 6))
        # The bottom face is the ground plane (no tangential E, no flux).
        cls.box = FluxBox(g, "closed", cls.node_box, faces=("x-", "x+", "y-", "y+", "z+"))
        cls.inside = region_probes(g, "in", cls.node_box)
        p1, p2 = dom.ports
        # The feed's cross-section at port 1's measurement plane, ground to the top CPML.
        ic = p1.i_cell
        cls.section = FluxBox(
            g,
            "section",
            ((ic, ic + 1), (g.pml.y_lo + 1, g.y.n - g.pml.y_hi - 1), (0, g.z.n - g.pml.z_hi - 1)),
            faces=("x+",),
        )
        cls.res = sim.run(
            p1.mode_sources(GaussianPulse.for_band(8e9, 12e9), dt),
            p1.probes + p2.probes + cls.box.probes + cls.inside + cls.section.probes,
            OMEGA,
            StopRule(tol=1e-9, f_lo=8e9),
        )
        cls.dt = dt

    def test_closed_box_balance(self):
        flux = self.box.power(self.res.dft)
        w = box_edge_weights(self.dom.grid, self.inside, self.node_box)
        diss = dissipated_power(self.st, self.inside, self.res.dft, OMEGA, self.dt, weights=w)
        self.assertTrue(np.all(diss > 0))
        self.assertTrue(np.all(flux < 0))  # net power flows in through the feed
        err = np.abs(flux + diss) / diss
        self.assertLess(err.max(), 1e-6, err)

    def test_port_wave_power_equals_feed_flux(self):
        p1 = self.dom.ports[0]
        v, i = p1.voltage(self.res.dft), p1.current(self.res.dft)
        net = 0.5 * np.real(v * np.conj(i))  # = (|a|² − |b|²)/2 for any real reference
        flux = self.section.power(self.res.dft)
        # Uncorrected, V/I power and field power differ by a few per cent (dispersion).
        raw = np.abs(flux - net) / np.abs(net)
        self.assertLess(raw.max(), 0.1, raw)
        # The calibration's power factor (measured on a separate straight line) corrects it.
        cal = calibrate_line(self.dom.line_spec(6, self.dt), OMEGA, backend="auto")
        err = np.abs(flux - cal.power_at(OMEGA) * net) / np.abs(net)
        self.assertLess(err.max(), 0.02, err)  # measured 0.5 %, 0.15 %, 1.3 % (8, 10, 12 GHz)


class FeedWindowTest(unittest.TestCase):
    def test_matched_line_leaks_little_through_the_box(self):
        dom = two_port(width=6.0e-3)
        g = dom.grid
        p1, p2 = dom.ports
        gd = np.zeros(dom.design_shape)
        i0, i1, j0, j1 = dom.window
        gd[:, p1.ta - j0 : p1.tb - j0] = S1.g_max
        dt = 0.95 * g.courant_dt()
        sim = Simulation(g, dom.structure(gd), dt=dt, dtype=np.float64)
        kc = g.k_c
        box = dom.radiation_box(0.0, float(g.z.nodes[kc + 8] - S1.h))
        self.assertEqual(box.node_box, ((i0, i1), (j0, j1), (kc, kc + 8)))
        res = sim.run(
            p1.mode_sources(GaussianPulse.for_band(8e9, 12e9), dt),
            p1.probes + box.probes,
            OMEGA,
            StopRule(tol=1e-6, f_lo=8e9),
        )
        v, i = p1.voltage(res.dft), p1.current(res.dft)
        net = 0.5 * np.real(v * np.conj(i))
        leak = np.abs(box.power(res.dft)) / net
        self.assertLess(leak.max(), 0.02, leak)  # measured 0.6 %, 1.0 %, 1.4 % (8, 10, 12 GHz)


class PassivityTest(unittest.TestCase):
    def test_random_gray_three_ports(self):
        spec = DomainSpec(
            S1,
            0.4e-3,
            2,
            (0.0, 4.0e-3, -2.4e-3, 2.4e-3),
            (
                PortSpec(1, "W", 0.0, 4),
                PortSpec(2, "E", 1.2e-3, 4),
                PortSpec(3, "E", -1.2e-3, 4),
            ),
            f_max=12.5e9,
            meas_cells=4,
            src_cells=9,
            margin=1.6e-3,
            air=3.2e-3,
            n_pml=8,
            n_pml_top=6,
        )
        dom = Domain(spec)
        g = dom.grid
        dt = 0.95 * g.courant_dt()
        rng = np.random.default_rng(11)
        worst = np.inf
        for trial in range(2):
            rho = rng.uniform(0, 1, dom.design_shape)
            st = dom.structure(sheet_conductance(rho, S1.g_min, S1.g_max))
            sim = Simulation(g, st, dt=dt, dtype=np.float64)
            probes = [pr for p in dom.ports for pr in p.probes]
            # Real reference impedance and phase-only shift: exact power waves.
            cal = LineCalibration(OMEGA, np.full(3, 48.0 + 0j), OMEGA * 1.7 / 3e8, dt)
            s = np.zeros((OMEGA.size, 3, 3), complex)
            for j, pj in enumerate(dom.ports):
                res = sim.run(
                    pj.mode_sources(GaussianPulse.for_band(8e9, 12e9), dt),
                    probes,
                    OMEGA,
                    StopRule(tol=1e-6, f_lo=8e9),
                )
                waves = [sparams.port_waves(p, cal, res.dft, OMEGA) for p in dom.ports]
                for i in range(3):
                    s[:, i, j] = waves[i][1] / waves[j][0]
            worst = min(worst, float(sparams.passivity_margin(s).min()))
        self.assertGreater(worst, -1e-3)


if __name__ == "__main__":
    unittest.main()
