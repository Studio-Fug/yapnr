"""Line-port conventions (design §5.2–§5.4).

- Signs: a short to the ground at the reference plane reflects with S11 ≈ −1 and an open strip
  end with S11 ≈ +1 (engineering convention, de-embedded to the reference plane). Measured at
  1.5 and 2.5 GHz: short 0.998∠179.3°, 0.998∠178.8°; open 0.999∠−3.9°, 0.999∠−6.4°.
- Orientation: ports on the S and N edges give the same S-parameters as on the W and E edges of
  the same problem rotated by 90° (the Yee scheme is symmetric under x ↔ y).
- Calibration results round-trip through their JSON cache.
"""

from __future__ import annotations

import math
import os
import tempfile
import unittest

import numpy as np

from yapnr.rf import sparams
from yapnr.rf.constants import C0
from yapnr.rf.domain import Domain, DomainSpec, PortSpec
from yapnr.rf.fdtd.engine import Simulation
from yapnr.rf.fdtd.sources import GaussianPulse
from yapnr.rf.fdtd.stop import StopRule
from yapnr.rf.ports import LineCalibration, LineSpec, calibrate_line
from yapnr.rf.stackup import Stackup, hammerstad_jensen

S1 = Stackup(3.55, 0.0027, 0.813e-3, 10e9)
PITCH = 0.3e-3


def hj_calibration(omega, width_cells, dt):
    z0, e_eff = hammerstad_jensen(width_cells * PITCH, S1.h, S1.er)
    return LineCalibration(omega, np.full(omega.size, z0 + 0j), omega * math.sqrt(e_eff) / C0, dt)


def spec(ports, design):
    return DomainSpec(
        S1,
        PITCH,
        4,
        design,
        ports,
        f_max=8e9,
        air=4.0e-3,
        meas_cells=6,
        src_cells=13,
        margin=2.4e-3,
    )


class TerminationSignTest(unittest.TestCase):
    """Short and open at the reference plane of a W port."""

    def reflect(self, short: bool):
        dom = Domain(spec((PortSpec(1, "W", 0.0, 6),), (0.0, 2.4e-3, -2.4e-3, 2.4e-3)))
        g = dom.grid
        p1 = dom.ports[0]
        st = dom.structure()
        if short:
            ks = np.arange(g.k_c)
            for j in range(p1.ta, p1.tb + 1):
                st.set_pec("ez", g.flat_index("ez", p1.i_ref, j, ks))
        dt = 0.95 * g.courant_dt()
        sim = Simulation(g, st, dt=dt, dtype=np.float64)
        omega = 2 * np.pi * np.array([1.5e9, 2.5e9])
        res = sim.run(
            p1.mode_sources(GaussianPulse(2e9, 1.0e9), dt),
            p1.probes,
            omega,
            StopRule(tol=1e-6, f_lo=1.5e9),
        )
        a, b = sparams.port_waves(p1, hj_calibration(omega, 6, dt), res.dft, omega)
        return sparams.to_engineering(b / a)

    def test_short_reflects_minus_one(self):
        s11 = self.reflect(short=True)
        self.assertGreater(np.abs(s11).min(), 0.95)
        phase = np.angle(-s11, deg=True)  # 0° for an ideal short
        self.assertLess(np.abs(phase).max(), 20.0, phase)

    def test_open_reflects_plus_one(self):
        s11 = self.reflect(short=False)
        self.assertGreater(np.abs(s11).min(), 0.9)
        phase = np.angle(s11, deg=True)  # slightly negative: the end's fringe capacitance
        self.assertLess(np.abs(phase).max(), 20.0, phase)
        self.assertTrue(np.all(phase < 0.0))


class OrientationTest(unittest.TestCase):
    def two_port(self, along_y: bool):
        # A 4.8 × 3.6 mm region with an offset 4-cell stub on the line, so S11 is not small.
        if along_y:
            ports = (PortSpec(1, "S", 0.0, 6), PortSpec(2, "N", 0.0, 6))
            design = (-1.8e-3, 1.8e-3, 0.0, 4.8e-3)
        else:
            ports = (PortSpec(1, "W", 0.0, 6), PortSpec(2, "E", 0.0, 6))
            design = (0.0, 4.8e-3, -1.8e-3, 1.8e-3)
        dom = Domain(spec(ports, design))
        g = dom.grid
        p1, p2 = dom.ports
        gd = np.zeros((16, 12))  # (along, across)
        gd[:, 3:9] = S1.g_max
        gd[6:10, 9:12] = S1.g_max
        st = dom.structure(gd.T if along_y else gd)
        dt = 0.95 * g.courant_dt()
        sim = Simulation(g, st, dt=dt, dtype=np.float64)
        omega = 2 * np.pi * np.array([4e9, 6e9, 8e9])
        res = sim.run(
            p1.mode_sources(GaussianPulse.for_band(4e9, 8e9), dt),
            p1.probes + p2.probes,
            omega,
            StopRule(tol=1e-4, f_lo=4e9),
        )
        cal = hj_calibration(omega, 6, dt)
        a1, b1 = sparams.port_waves(p1, cal, res.dft, omega)
        _, b2 = sparams.port_waves(p2, cal, res.dft, omega)
        return np.array([b1 / a1, b2 / a1])

    def test_rotation_invariance(self):
        xy = self.two_port(along_y=False)
        yx = self.two_port(along_y=True)
        self.assertGreater(np.abs(xy[0]).min(), 0.05)  # the stub reflects
        # Mirror-image arithmetic: equal to rounding whatever the run length (measured 2e-15).
        np.testing.assert_allclose(yx, xy, rtol=0, atol=1e-12)


class CalibrationCacheTest(unittest.TestCase):
    def test_round_trip(self):
        dt = 0.6e-12
        line = LineSpec(S1, 0.4e-3, 2, 4, air=2.4e-3, dz_max=0.8e-3, dt=dt, n_pml=6, n_pml_top=6)
        omega = 2 * np.pi * np.array([6e9, 9e9])
        with tempfile.TemporaryDirectory() as tmp:
            cal = calibrate_line(line, omega, tol=1e-4, cache_dir=tmp)
            self.assertEqual(len(os.listdir(tmp)), 1)
            again = calibrate_line(line, omega, tol=1e-4, cache_dir=tmp)
        np.testing.assert_array_equal(again.zc, cal.zc)
        np.testing.assert_array_equal(again.k, cal.k)
        np.testing.assert_array_equal(again.power, cal.power)
        self.assertGreater(cal.zc.real.min(), 30.0)
        self.assertTrue(np.all(cal.k.real > 0))
        zc, k = cal.at(np.array([7.5e9 * 2 * np.pi]))
        self.assertTrue(cal.zc.real.min() <= zc.real[0] <= cal.zc.real.max())


if __name__ == "__main__":
    unittest.main()
