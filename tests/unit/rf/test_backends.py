"""numpy and torch backends agree (design §12): float64 fields to 1e-12, float32 S-parameters to
1e-4; repeated runs are bit-identical."""

from __future__ import annotations

import unittest

import numpy as np

from yapnr.rf import sparams
from yapnr.rf.domain import Domain, DomainSpec, PortSpec
from yapnr.rf.fdtd.engine import Simulation
from yapnr.rf.fdtd.sources import GaussianPulse
from yapnr.rf.fdtd.stop import StopRule
from yapnr.rf.ports import LineCalibration
from yapnr.rf.stackup import Stackup


def problem():
    st = Stackup(3.55, 0.0027, 0.813e-3, 10e9)
    spec = DomainSpec(
        st,
        0.4e-3,
        2,
        (0.0, 3.2e-3, -1.6e-3, 1.6e-3),
        (PortSpec(1, "W", 0.0, 4), PortSpec(2, "E", 0.0, 4)),
        f_max=14e9,
        meas_cells=3,
        src_cells=7,
        pml_gap=2,
        margin=1.6e-3,
        air=3.0e-3,
        n_pml=8,
        n_pml_top=6,
    )
    dom = Domain(spec)
    rng = np.random.default_rng(5)
    g = st.g_min * (st.g_max / st.g_min) ** rng.uniform(0, 1, dom.design_shape)
    return dom, dom.structure(g)


class BackendTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dom, cls.st = problem()
        cls.dt = 0.95 * cls.dom.grid.courant_dt()
        cls.omega = 2 * np.pi * np.array([8e9, 10e9, 12e9])
        cls.pulse = GaussianPulse.for_band(8e9, 12e9)

    def sim(self, backend, dtype):
        return Simulation(self.dom.grid, self.st, dt=self.dt, backend=backend, dtype=dtype)

    def run_fields(self, backend, dtype, steps=400):
        sim = self.sim(backend, dtype)
        p1 = self.dom.ports[0]
        sim.run(
            p1.mode_sources(self.pulse, self.dt),
            [],
            self.omega,
            StopRule(max_steps=steps),
        )
        return {c: sim.field(c) for c in ("ex", "ey", "ez", "hx", "hy", "hz")}

    def run_s(self, backend, dtype):
        sim = self.sim(backend, dtype)
        p1, p2 = self.dom.ports
        res = sim.run(
            p1.mode_sources(self.pulse, self.dt),
            p1.probes + p2.probes,
            self.omega,
            StopRule(tol=1e-6, f_lo=8e9),
        )
        cal = LineCalibration(
            omega=self.omega, zc=np.full(3, 50.0 + 0j), k=self.omega * 1.7 / 3e8, dt=self.dt
        )
        a1, b1 = sparams.port_waves(p1, cal, res.dft, self.omega)
        a2, b2 = sparams.port_waves(p2, cal, res.dft, self.omega)
        return np.array([b1 / a1, b2 / a1]), res

    def test_float64_fields_agree(self):
        ref = self.run_fields("numpy", np.float64)
        got = self.run_fields("torch", np.float64)
        for c in ref:
            scale = np.abs(ref[c]).max()
            self.assertGreater(scale, 0.0)
            self.assertLess(np.abs(got[c] - ref[c]).max() / scale, 1e-12, c)

    def test_float32_sparameters_agree(self):
        ref, _ = self.run_s("numpy", np.float64)
        got, _ = self.run_s("torch", np.float32)
        self.assertLess(np.abs(got - ref).max(), 1e-4)

    def test_repeat_is_bit_identical(self):
        a, ra = self.run_s("torch", np.float32)
        b, rb = self.run_s("torch", np.float32)
        self.assertEqual(ra.steps, rb.steps)
        np.testing.assert_array_equal(a, b)


if __name__ == "__main__":
    unittest.main()
