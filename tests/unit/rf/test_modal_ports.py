"""Modal port waves (design §25.1, `ports.ModalPlane`) on a straight two-port.

- The matched line: the modal reflection is below −45 dB (the V/I waves': about −40 dB), the
  transmission agrees with the V/I waves' to 0.05 dB, and the modal net power at the
  measurement plane equals the plane's Poynting flux (a pure mode carries all of it).
- The incident wave does not depend on the design: the straight line, a gray design and an
  empty design region (an open end, |Γ| ≈ 1) give the same incident power to 1e-3.
- The modal S-matrix of a gray design is reciprocal (|S21 − S12| ≤ 1e-3) and passive.
- The waves are unit-power: P_inc = ½|a|² exactly, with the phase of the strip voltage.
"""

from __future__ import annotations

import unittest
from dataclasses import replace

import numpy as np

from yapnr.rf import sparams
from yapnr.rf.problem import Problem
from yapnr.rf.testing import line_spec, straight_line

OMEGA = 2 * np.pi * np.array([8e9, 10e9, 12e9])


class ModalPortTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = line_spec()
        cls.p = Problem(spec)
        cls.vi = Problem(spec.replace(solver=replace(spec.solver, port_extraction="vi")))
        p = cls.p
        cls.line = straight_line(p)
        cls.gray = np.random.default_rng(5).uniform(0.2, 0.8, p.design_shape)
        cls.empty = np.zeros(p.design_shape)
        cls.sw = {}
        for name, rho in (("line", cls.line), ("gray", cls.gray), ("empty", cls.empty)):
            cls.sw[name] = p.sweep(rho, OMEGA / (2 * np.pi))
        cls.sw_vi = cls.vi.sweep(cls.line, OMEGA / (2 * np.pi))
        # The forward run of the line for the plane checks.
        p.set_design(cls.line)
        cls.fwd = p.forward(1, omega=OMEGA, design=False)

    def test_matched_line(self):
        s, s_vi = self.sw["line"]["s"], self.sw_vi["s"]
        self.assertLess(sparams.db(s[:, 0, 0]).max(), -45.0)
        np.testing.assert_allclose(sparams.db(s[:, 1, 0]), sparams.db(s_vi[:, 1, 0]), atol=0.05)

    def test_modal_power_is_the_plane_flux(self):
        mp = self.p.modal[1]
        face = mp.face
        fms = mp.face_modes(face, OMEGA)
        ap, am = mp.amplitudes(face, fms, self.fwd.dft)
        modal = mp.net_power(fms, ap, am)
        flux = mp.box.power(self.fwd.dft)  # along +x
        np.testing.assert_allclose(modal, flux, rtol=1e-3)
        # Unit-power waves: the incident power is ½|a|².
        np.testing.assert_allclose(modal, 0.5 * np.abs(ap) ** 2, rtol=2e-3)

    def test_incident_wave_is_design_independent(self):
        a = {}
        for name in ("line", "gray", "empty"):
            self.p.set_design({"line": self.line, "gray": self.gray, "empty": self.empty}[name])
            res = self.p.forward(1, omega=OMEGA, design=False)
            a[name] = self.p.port_waves(1, res.dft, OMEGA)[0]
        for name in ("gray", "empty"):
            err = np.abs(np.abs(a[name]) ** 2 / np.abs(a["line"]) ** 2 - 1)
            self.assertLess(err.max(), 1e-3, (name, err))
            # The phase too (the same incident wave).
            self.assertLess(np.abs(np.angle(a[name] / a["line"])).max(), 1e-3, name)

    def test_reciprocal_and_passive(self):
        s = self.sw["gray"]["s"]
        err = np.abs(s[:, 0, 1] - s[:, 1, 0])  # measured 2e-4 (|S21| 0.05)
        self.assertLess(err.max(), 1e-3, err)
        self.assertGreater(sparams.passivity_margin(s).min(), -1e-3)

    def test_voltage_phase(self):
        """The modal waves' phases follow the V/I convention (a ∝ V₊): on the matched line the
        two transmissions agree in phase to a degree (the V/I waves see the dispersive Z_c)."""
        s, s_vi = self.sw["line"]["s"], self.sw_vi["s"]
        d = np.degrees(np.abs(np.angle(s[:, 1, 0] / s_vi[:, 1, 0])))
        self.assertLess(d.max(), 1.0, d)


if __name__ == "__main__":
    unittest.main()
