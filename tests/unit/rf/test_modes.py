"""The discrete line mode and the modal port source (`yapnr.rf.modes`).

S1 (εr 3.55, h 0.813 mm), a 6-cell (1.8 mm) strip at 0.3 mm with 4 substrate cells. Measured
on a matched line through a 9.6 mm window (both ports' feeds, 2 × 5.1 mm de-embedded in phase
only), torch float64, at 8.5, 10 and 11.5 GHz:

- static source (the strip's field in air): |S21| −0.208, −0.208, −0.191 dB against the mode's
  own loss −0.118, −0.119, −0.119 dB (the excited port's incident wave reads about 1 % high);
  |S11| −40 dB (the surface wave the source launches, read as a reflection);
- modal source: |S21| −0.118, −0.119, −0.119 dB (within 0.001 dB of the mode's loss) and
  |S11| −63 to −68 dB; with the copper-edge correction the same agreement (Z_c 52.3–53.4 Ω).
"""

from __future__ import annotations

import unittest

import numpy as np

from yapnr.rf import sparams
from yapnr.rf.modes import _ring_current, cross_section, line_mode, mode_profile
from yapnr.rf.ports import LinePort, LineSpec, calibration_grid
from yapnr.rf.stackup import Stackup, kirschning_jansen_eps_eff

S1 = Stackup(3.55, 0.0027, 0.813e-3, 10e9)


def line_grid(edge_correction=False):
    spec = LineSpec(S1, 0.3e-3, 4, 6, air=7 * S1.h, dz_max=1.6e-3, dt=1.0)
    grid, centre = calibration_grid(spec, 40)
    dt = 0.95 * grid.courant_dt()
    p = LinePort(1, "W", centre, 6, grid.x.nodes[30], 2, 5).on(grid)
    return grid, dt, cross_section(grid, S1, 0, p.ta, p.tb, edge_correction=edge_correction)


class LineModeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.grid, cls.dt, cls.cs = line_grid()
        cls.mode = line_mode(cls.cs, S1, 2 * np.pi * 10e9, cls.dt)

    def test_converged_and_close_to_closed_forms(self):
        m = self.mode
        self.assertLess(m.residual, 1e-9)
        kj = kirschning_jansen_eps_eff(1.8e-3, S1.h, S1.er, 10e9)
        # Uncorrected the strip acts wider: ε_eff +4.5 % against Kirschning–Jansen here.
        self.assertLess(abs(m.eps_eff() / kj - 1), 0.06)
        self.assertGreater(m.k.imag, 0.3)  # dielectric and copper loss, 0.68 Np/m measured
        self.assertLess(m.k.imag, 1.2)

    def test_orientation(self):
        # The same strip along y: the frame flips handedness (H → −H) but not the mode.
        from yapnr.rf.mesh import Grid

        g = self.grid
        gy = Grid(
            g.y,
            g.x,
            g.z,
            g.k_c,
            g.pml.__class__(g.pml.y_lo, g.pml.y_hi, g.pml.x_lo, g.pml.x_hi, g.pml.z_hi),
        )
        cs = cross_section(gy, S1, 1, self.cs.ta, self.cs.tb)
        m = line_mode(cs, S1, 2 * np.pi * 10e9, self.dt)
        self.assertAlmostEqual(m.eps_eff(), self.mode.eps_eff(), places=10)
        i0, i1 = _ring_current(self.cs, self.mode), _ring_current(cs, m)
        np.testing.assert_allclose(np.abs(m.ht / i1), np.abs(self.mode.ht / i0), atol=1e-9)

    def test_profile_fit(self):
        prof = mode_profile(self.cs, S1, 8e9, 12e9, self.dt)
        self.assertLess(prof.fit_error, 0.01)
        self.assertAlmostEqual(
            np.abs(np.concatenate([prof.jt0.ravel(), prof.jz0.ravel()])).max(), 1.0
        )
        kc, c = self.grid.k_c, self.cs.ta + 3
        self.assertLess(prof.jz0[c, kc - 1], 0.0)  # the static source's sign


class ModalSourceTest(unittest.TestCase):
    """A matched line through a 9.6 mm window: |S21| against the mode's loss (module doc)."""

    @staticmethod
    def thru(port_source, edge_correction=False):
        from yapnr.rf import cases
        from yapnr.rf.problem import Problem
        from yapnr.rf.spec import Band, GridSpec, Port, S, SolverSpec, Spec

        spec = Spec(
            name="thru",
            stackup=cases.S1,
            grid=GridSpec(pitch_mm=0.3, substrate_cells=4),
            design_region=(0.0, 9.6, -3.0, 3.0),
            ports=(Port(1, "W", 0.0, 6), Port(2, "E", 0.0, 6)),
            bands={"b": Band(8.5, 11.5, 3)},
            requirements=(S(2, 1).at_least_db(-1, band="b"),),
            solver=SolverSpec(
                backend="torch",
                dtype="float64",
                tol=1e-5,
                edge_correction=edge_correction,
                port_source=port_source,
            ),
        )
        p = Problem(spec)
        pg = p.ports[1]
        mask = np.zeros(p.design_shape)
        j0 = p.domain.window[2]
        mask[:, pg.ta - j0 : pg.tb - j0] = 1.0
        f = np.array([8.5e9, 10e9, 11.5e9])
        sw = p.sweep(mask, f, ports=[1])
        cs = cross_section(p.grid, p.stackup, 0, pg.ta, pg.tb, edge_correction=edge_correction)
        alpha = np.array([line_mode(cs, p.stackup, 2 * np.pi * x, p.dt).k.imag for x in f])
        loss_db = -20.0 * np.log10(np.e) * alpha * (9.6e-3 + 2 * pg.d_m)
        return sw["s"], loss_db

    def test_modal_source_has_no_incident_bias(self):
        s, loss = self.thru("mode")
        np.testing.assert_allclose(sparams.db(s[:, 1, 0]), loss, atol=0.005)
        self.assertLess(sparams.db(s[:, 0, 0]).max(), -55.0)

    def test_static_source_reads_low(self):
        s, loss = self.thru("static")
        self.assertLess((sparams.db(s[:, 1, 0]) - loss).max(), -0.05)  # measured −0.07 to −0.09


if __name__ == "__main__":
    unittest.main()
