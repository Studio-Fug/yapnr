"""Resistive lumped ports (design §5.5): a short strip between two 50 Ω ports is reciprocal
(exactly, by the symmetry of the discrete operator), passive and transmits."""

from __future__ import annotations

import unittest

import numpy as np

from yapnr.rf import sparams
from yapnr.rf.domain import Domain, DomainSpec
from yapnr.rf.fdtd.engine import Simulation
from yapnr.rf.fdtd.sources import GaussianPulse
from yapnr.rf.fdtd.stop import StopRule
from yapnr.rf.ports import LumpedPort, source_dtft
from yapnr.rf.stackup import Stackup

S1 = Stackup(3.55, 0.0027, 0.813e-3, 10e9)
OMEGA = 2 * np.pi * np.array([2e9, 4e9, 6e9])
PITCH = 0.3e-3


class LumpedPortTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = DomainSpec(S1, PITCH, 4, (0.0, 6.0e-3, -2.4e-3, 2.4e-3), f_max=8e9, air=5e-3)
        dom = Domain(spec)
        g = dom.grid
        i0, i1, j0, j1 = dom.window
        gd = np.zeros(dom.design_shape)
        jc = (j1 - j0) // 2
        gd[2:-2, jc - 3 : jc + 3] = S1.g_max  # a 6-cell strip from x = 0.6 to 5.4 mm
        ys = [g.y.nodes[j0 + jc + d] for d in range(-3, 4)]
        x_a, x_b = g.x.nodes[i0 + 2], g.x.nodes[i1 - 2]
        cls.ports = [
            LumpedPort(1, tuple((x_a, y) for y in ys)).on(g),
            LumpedPort(2, tuple((x_b, y) for y in ys)).on(g),
        ]
        st = dom.structure(gd)
        for p in cls.ports:
            p.apply(st)
        dt = 0.95 * g.courant_dt()
        sim = Simulation(g, st, dt=dt, backend="torch", dtype=np.float64)
        pulse = GaussianPulse.for_band(2e9, 6e9)
        vs = source_dtft(pulse, OMEGA, dt)
        probes = [pr for p in cls.ports for pr in p.probes]
        s = np.zeros((OMEGA.size, 2, 2), complex)
        for j, pj in enumerate(cls.ports):
            res = sim.run([pj.source(pulse, dt)], probes, OMEGA, StopRule(tol=1e-8, f_lo=2e9))
            for i, pi in enumerate(cls.ports):
                a, b = pi.waves(res.dft, vs if i == j else 0.0)
                if i == j:
                    a_j = a
                s[:, i, j] = b
            s[:, :, j] /= a_j[:, None]
        cls.s = s

    def test_reciprocal(self):
        s = self.s
        err = np.abs(s[:, 0, 1] - s[:, 1, 0]) / np.abs(s[:, 1, 0])
        self.assertLess(err.max(), 1e-6, err)

    def test_passive(self):
        self.assertGreater(sparams.passivity_margin(self.s).min(), -1e-6)

    def test_transmits(self):
        self.assertGreater(sparams.db(self.s[:, 1, 0]).min(), -1.0)
        self.assertLess(sparams.db(self.s[:, 0, 0]).max(), -10.0)


if __name__ == "__main__":
    unittest.main()
