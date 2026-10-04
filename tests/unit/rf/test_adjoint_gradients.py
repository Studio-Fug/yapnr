"""The adjoint gradient of the full pipeline against finite differences (design §6.7).

A tiny two-port problem (about 1e4 cells) with a random gray 6×6-pixel design between line
ports. For every DM type (|S21|² and |S11|² with de-embedding, the phase of S21, the radiated
fraction through a flux box, a design-plane field, a log-sum-exp group), the per-frequency
gradients from one adjoint run per type are compared with Richardson-extrapolated central
differences along random directions and single pixels. The conductance interpolation includes
the damping term, so its derivative is covered too.
"""

from __future__ import annotations

import unittest

import numpy as np
import torch

from yapnr.rf import sparams
from yapnr.rf.adjoint import gradient, wirtinger
from yapnr.rf.domain import Domain, DomainSpec, PortSpec
from yapnr.rf.fdtd.engine import Simulation
from yapnr.rf.fdtd.monitors import FluxBox
from yapnr.rf.fdtd.sources import GaussianPulse
from yapnr.rf.fdtd.stop import StopRule
from yapnr.rf.materials import sheet_conductance, sheet_conductance_derivative
from yapnr.rf.stackup import Stackup

torch.set_num_threads(1)

NAMES = ("s21", "s11", "phase", "eta", "field", "lse")


class TinyProblem:
    def __init__(self):
        st = Stackup(3.0, 0.002, 0.8e-3, 10e9)
        spec = DomainSpec(
            st,
            0.4e-3,
            2,
            (0.0, 2.4e-3, -1.2e-3, 1.2e-3),
            (PortSpec(1, "W", 0.0, 2), PortSpec(2, "E", 0.0, 2)),
            f_max=15e9,
            meas_cells=2,
            src_cells=5,
            pml_gap=2,
            core_cells=1,
            margin=1.2e-3,
            air=2.4e-3,
            n_pml=6,
            n_pml_top=6,
        )
        self.st = st
        self.dom = dom = Domain(spec)
        g = dom.grid
        self.grid = g
        self.dt = 0.95 * g.courant_dt()
        self.omega = 2 * np.pi * np.array([8e9, 10e9, 12e9])
        self.pulse = GaussianPulse.for_band(8e9, 12e9)
        self.p1, self.p2 = dom.ports
        i0, i1, j0, j1 = dom.window
        kc = g.k_c
        window = ((-1.0, 1.0), (-1.0e-3, 1.0e-3), (0.0, 2.1e-3))
        self.box = FluxBox(
            g,
            "flux",
            ((i0 - 1, i1 + 1), (j0 - 2, j1 + 2), (kc, kc + 3)),
            faces=("x-", "x+", "y-", "y+", "z+"),
            windows=[window],
        )
        self.dprobes = dom.design_probes()
        self.probes = self.p1.probes + self.p2.probes + self.box.probes + self.dprobes
        self.g_d = 0.05  # damping term on, so its derivative is tested
        self.zc = np.array([50.0 + 1.0j, 49.0 + 0.5j, 48.0 + 0.2j])
        self.k = self.omega * 1.6 / 3e8 + 0.5j

    def conductance(self, rho):
        return sheet_conductance(rho, self.st.g_min, self.st.g_max, self.g_d)

    def forward(self, rho, tol=1e-12):
        sim = Simulation(self.grid, self.dom.structure(self.conductance(rho)), dt=self.dt)
        res = sim.run(
            self.p1.mode_sources(self.pulse, self.dt),
            self.probes,
            self.omega,
            StopRule(tol=tol, f_lo=8e9),
        )
        return sim, res

    def objectives(self, q):
        p1, p2 = self.p1, self.p2
        a1, b1 = sparams.waves(p1.voltage(q), p1.current(q), self.zc, self.k, p1.d_m)
        a2, b2 = sparams.waves(p2.voltage(q), p2.current(q), self.zc, self.k, p2.d_m)
        s21 = b2 / a1
        s11 = b1 / a1
        eta = self.box.power(q) / (0.5 * torch.abs(a1) ** 2)
        th0 = torch.tensor([0.3, 1.0, 2.0], dtype=torch.float64)
        phase = 1 - torch.real(s21 * torch.exp(-1j * th0)) / torch.abs(s21)
        field = (torch.abs(q["design_ex"][:, :5]) ** 2).sum(dim=1) / torch.abs(a1) ** 2
        phis = torch.stack(
            [
                10 * torch.log10(torch.abs(s11) ** 2 + 1e-10) / 10,
                (-3 - 10 * torch.log10(torch.abs(s21) ** 2)) / 1.0,
                (0.3 - eta) / 0.1,
            ]
        )
        lse = 0.05 * torch.logsumexp(phis / 0.05, dim=0)
        return {
            "s21": torch.abs(s21) ** 2,
            "s11": torch.abs(s11) ** 2,
            "phase": phase,
            "eta": eta,
            "field": field,
            "lse": lse,
        }

    def values(self, res):
        q = {n: torch.as_tensor(v) for n, v in res.dft.items()}
        return {k: v.detach().numpy() for k, v in self.objectives(q).items()}


class AdjointGradientTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        prob = cls.prob = TinyProblem()
        rng = np.random.default_rng(1)
        rho = rng.uniform(0.3, 0.7, prob.dom.design_shape)
        sim, res = prob.forward(rho)
        cls.converged = [res.converged]
        dg = sheet_conductance_derivative(rho, prob.st.g_min, prob.st.g_max, prob.g_d)
        watched = [p.name for p in prob.p1.probes + prob.p2.probes + prob.box.probes]
        watched.append("design_ex")
        cls.grads = {}
        cls.adjoint_steps = {}
        for name in NAMES:
            _, wg = wirtinger(lambda q: prob.objectives(q)[name], res.dft, watched)
            gr = gradient(sim, res, prob.probes, wg, prob.dprobes, StopRule(tol=1e-10, f_lo=8e9))
            cls.converged.append(gr.adjoint.converged)
            cls.adjoint_steps[name] = gr.adjoint.steps
            cls.grads[name] = prob.dom.window_pixels(gr.pixels(prob.grid)) * dg[None]
        dirs = [rng.standard_normal(rho.shape) for _ in range(2)]
        for i, j in [(1, 2), (3, 3), (4, 1)]:
            e = np.zeros_like(rho)
            e[i, j] = 1.0
            dirs.append(e)
        cls.dirs = dirs
        h = 1e-4
        cls.fd = []
        for v in dirs:
            vals = [prob.values(prob.forward(rho + s * h * v)[1]) for s in (1, -1, 0.5, -0.5)]
            d1 = {n: (vals[0][n] - vals[1][n]) / (2 * h) for n in NAMES}
            d2 = {n: (vals[2][n] - vals[3][n]) / h for n in NAMES}
            cls.fd.append({n: (4 * d2[n] - d1[n]) / 3 for n in NAMES})

    def test_runs_converged(self):
        self.assertTrue(all(self.converged))

    def _check(self, name):
        for v, fd in zip(self.dirs, self.fd):
            adj = np.tensordot(self.grads[name], v, axes=([1, 2], [0, 1]))
            scale = np.maximum(np.abs(fd[name]), 1e-3 * np.abs(self.grads[name]).max())
            err = np.abs(fd[name] - adj) / scale
            self.assertLess(err.max(), 1e-5, f"{name}: fd {fd[name]} adjoint {adj}")

    def test_s21_power(self):
        self._check("s21")

    def test_s11_power(self):
        self._check("s11")

    def test_s21_phase(self):
        self._check("phase")

    def test_radiated_fraction(self):
        self._check("eta")

    def test_design_plane_field(self):
        self._check("field")

    def test_lse_group(self):
        self._check("lse")


if __name__ == "__main__":
    unittest.main()
