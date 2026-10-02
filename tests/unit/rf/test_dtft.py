"""The DTFT of a run satisfies the exact discrete frequency-domain equations (design §4.6).

    (−iΩ ε + c_ω σ) Ê = C_H Ĥ − Ĵ,     −iΩ μ0 Ĥ = −C_E Ê − K̂

with Ω = (2/Δt) sin(ωΔt/2) and c_ω = cos(ωΔt/2). This pins the time staggering, the DTFT
phases, the Crank–Nicolson conductivity and the source conventions that the adjoint relies on.
"""

from __future__ import annotations

import unittest

import numpy as np

from yapnr.rf.constants import MU0
from yapnr.rf.edges import planes
from yapnr.rf.fdtd.dtft import conductance_factor, decimation, numerical_omega
from yapnr.rf.fdtd.engine import Simulation
from yapnr.rf.fdtd.monitors import Probe, box_indices
from yapnr.rf.fdtd.sources import GaussianPulse, NuttallFit, PulseSource, SpectralSource
from yapnr.rf.fdtd.stop import StopRule
from yapnr.rf.materials import Structure
from yapnr.rf.mesh import (
    COMPONENTS,
    E_COMPONENTS,
    Grid,
    PMLCells,
    graded_axis,
    substrate_z_axis,
)
from yapnr.rf.stackup import Stackup


def small_grid():
    st = Stackup(3.0, 0.004, 0.6e-3, 10e9)
    x = graded_axis(0, 2.4e-3, 0.3e-3, -1.5e-3, 3.9e-3, max_cell=0.6e-3, n_pml_lo=4, n_pml_hi=4)
    y = graded_axis(0, 1.8e-3, 0.3e-3, -1.2e-3, 3.0e-3, max_cell=0.6e-3, n_pml_lo=4, n_pml_hi=4)
    z, kc = substrate_z_axis(st.h, 2, 1.5e-3, dz_max=0.6e-3, n_pml=4)
    return Grid(x, y, z, kc, PMLCells(4, 4, 4, 4, 4)), st


def full_probes(grid):
    return [Probe(c, c, np.arange(int(np.prod(grid.shape(c))))) for c in COMPONENTS]


def source_dtft(src, sim, omega, magnetic):
    n = np.arange(src.end_step + 1)
    vals = []
    for k in n:
        v = src.values(int(k))
        vals.append(np.zeros(src.index.size) if v is None else v)
    vals = np.array(vals)  # (T, P)
    t = (n + (0.0 if magnetic else 0.5)) * sim.dt
    return sim.dt * np.exp(1j * np.outer(omega, t)) @ vals  # (M, P)


class DiscreteFrequencyDomainTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        grid, st = small_grid()
        s = Structure(grid, st)
        rng = np.random.default_rng(3)
        # Random sheet conductance from void up to a Crank–Nicolson factor a = σΔt/2ε of about
        # 0.1 (copper itself, a ≈ 2e3, traps magnetic flux whose decay would make the run
        # long; the G_max sheet is covered by the gradient and sheet tests), and a resistor.
        g = st.g_min * (st.g_max / st.g_min) ** rng.uniform(0, 0.85, grid.n[:2])
        s.set_pixels(g)
        s.add_resistor("ez", grid.flat_index("ez", 8, 7, [0, 1]), 50.0, 2, 1)
        sim = Simulation(grid, s)
        cls.grid, cls.sim, cls.s = grid, sim, s
        cls.omega = 2 * np.pi * np.array([7.5e9, 9e9, 10.5e9])
        # Narrow enough (ω_c τ ≈ 11) to have no low-frequency content: a broadband pulse
        # excites the slow dielectric relaxation (ε/σ_sub) through its first time moment.
        pulse = GaussianPulse(9e9, 2.5e9)
        jsrc = PulseSource("ez", grid.flat_index("ez", 6, 6, [0, 1]), [1.0, -0.5], pulse, sim.dt)
        fit = NuttallFit.build(cls.omega, sim.dt, magnetic=True)
        req = np.array([[1.0 + 2.0j, -0.5j, 0.3], [0.2, 1.0, -1.0 + 1.0j]]) * 1e-3
        ksrc = SpectralSource("hy", grid.flat_index("hy", 9, 8, [3, 4]), fit.coefficients(req), fit)
        cls.sources = [jsrc, ksrc]
        cls.jhat = source_dtft(jsrc, sim, cls.omega, magnetic=False)
        cls.khat = source_dtft(ksrc, sim, cls.omega, magnetic=True)
        cls.res = sim.run(
            cls.sources, full_probes(grid), cls.omega, StopRule(tol=1e-12, f_lo=7.5e9)
        )

    def fields(self, m):
        return {c: self.res.dft[c][m].reshape(self.grid.shape(c)) for c in COMPONENTS}

    def test_converged(self):
        self.assertTrue(self.res.converged)

    def test_requested_adjoint_spectrum_is_realized(self):
        req = np.array([[1.0 + 2.0j, -0.5j, 0.3], [0.2, 1.0, -1.0 + 1.0j]]) * 1e-3
        np.testing.assert_allclose(self.khat.T, req, atol=1e-12 * np.abs(req).max())

    def test_residuals(self):
        g, sim = self.grid, self.sim
        big = numerical_omega(self.omega, sim.dt)
        cw = conductance_factor(self.omega, sim.dt)
        x, y, z = g.x, g.y, g.z

        def bc(v, a):
            s = [1, 1, 1]
            s[a] = v.size
            return v.reshape(s)

        # Interior region away from the CPML (one extra cell of margin).
        lo = [g.pml.along(a)[0] + 1 for a in range(3)]
        hi = [g.axis(a).n - g.pml.along(a)[1] - 1 for a in range(3)]
        worst_e = worst_h = 0.0
        for m in range(self.omega.size):
            f = self.fields(m)
            jfull = np.zeros(g.shape("ez"), complex)
            jfull.reshape(-1)[self.sources[0].index] = self.jhat[m]
            kfull = np.zeros(g.shape("hy"), complex)
            kfull.reshape(-1)[self.sources[1].index] = self.khat[m]
            # H equation: −iΩμ0 Ĥ + C_E Ê + K̂ = 0.
            ce = {
                "hx": np.diff(f["ez"], axis=1) / bc(y.primary, 1)
                - np.diff(f["ey"], axis=2) / bc(z.primary, 2),
                "hy": np.diff(f["ex"], axis=2) / bc(z.primary, 2)
                - np.diff(f["ez"], axis=0) / bc(x.primary, 0),
                "hz": np.diff(f["ey"], axis=0) / bc(x.primary, 0)
                - np.diff(f["ex"], axis=1) / bc(y.primary, 1),
            }
            for c in ("hx", "hy", "hz"):
                k = kfull if c == "hy" else 0.0
                mu = np.full(g.shape(c), MU0)
                if self.s.edge_correction:
                    ks = planes(g, c)
                    mu[:, :, ks[0] : ks[-1] + 1] = MU0 / self.s.mu_factor(c)
                r = -1j * big[m] * mu * f[c] + ce[c] + k
                sl = tuple(slice(lo[a], hi[a]) for a in range(3))
                scale = np.abs(ce[c][sl]).max()
                worst_h = max(worst_h, np.abs(r[sl]).max() / scale)
            # E equation: (−iΩε + c σ) Ê − C_H Ĥ + Ĵ = 0 (interior edges).
            ch = {
                "ex": (np.diff(f["hz"], axis=1) / bc(y.dual[1:-1], 1))[:, :, 1:-1]
                - (np.diff(f["hy"], axis=2) / bc(z.dual[1:-1], 2))[:, 1:-1, :],
                "ey": (np.diff(f["hx"], axis=2) / bc(z.dual[1:-1], 2))[1:-1, :, :]
                - (np.diff(f["hz"], axis=0) / bc(x.dual[1:-1], 0))[:, :, 1:-1],
                "ez": (np.diff(f["hy"], axis=0) / bc(x.dual[1:-1], 0))[:, 1:-1, :]
                - (np.diff(f["hx"], axis=1) / bc(y.dual[1:-1], 1))[1:-1, :, :],
            }
            inner = {
                "ex": (slice(None), slice(1, -1), slice(1, -1)),
                "ey": (slice(1, -1), slice(None), slice(1, -1)),
                "ez": (slice(1, -1), slice(1, -1), slice(None)),
            }
            for c in E_COMPONENTS:
                eps = self.s.eps(c)[inner[c]]
                sig = self.s.sigma(c)[inner[c]]
                j = jfull[inner[c]] if c == "ez" else 0.0
                r = (-1j * big[m] * eps + cw[m] * sig) * f[c][inner[c]] - ch[c] + j
                sl = tuple(slice(lo[a] - 1, hi[a] - 1) for a in range(3))
                scale = np.abs(ch[c][sl]).max()
                worst_e = max(worst_e, np.abs(r[sl]).max() / scale)
        self.assertLess(worst_h, 1e-10)
        self.assertLess(worst_e, 1e-10)


class EdgeCorrectionTest(DiscreteFrequencyDomainTest):
    """The same identity with the copper-edge correction (`edges`) for a gray copper fraction:
    ε scaled on the corrected E edges (in `Structure.eps`) and μ = μ0/factor on the H edges
    of the planes around the copper (applied by the engine after each H update)."""

    @classmethod
    def setUpClass(cls):
        grid, st = small_grid()
        s = Structure(grid, st)
        rng = np.random.default_rng(7)
        g = st.g_min * (st.g_max / st.g_min) ** rng.uniform(0, 0.85, grid.n[:2])
        s.set_pixels(g)
        s.set_edge_correction(rng.uniform(0.0, 1.0, grid.n[:2]))
        s.add_resistor("ez", grid.flat_index("ez", 8, 7, [0, 1]), 50.0, 2, 1)
        sim = Simulation(grid, s)
        cls.grid, cls.sim, cls.s = grid, sim, s
        cls.omega = 2 * np.pi * np.array([7.5e9, 9e9, 10.5e9])
        pulse = GaussianPulse(9e9, 2.5e9)
        jsrc = PulseSource("ez", grid.flat_index("ez", 6, 6, [0, 1]), [1.0, -0.5], pulse, sim.dt)
        fit = NuttallFit.build(cls.omega, sim.dt, magnetic=True)
        req = np.array([[1.0 + 2.0j, -0.5j, 0.3], [0.2, 1.0, -1.0 + 1.0j]]) * 1e-3
        # The magnetic source sits on a corrected plane (k_c − 1, k_c of Hy).
        kk = [grid.k_c - 1, grid.k_c]
        ksrc = SpectralSource("hy", grid.flat_index("hy", 9, 8, kk), fit.coefficients(req), fit)
        cls.sources = [jsrc, ksrc]
        cls.jhat = source_dtft(jsrc, sim, cls.omega, magnetic=False)
        cls.khat = source_dtft(ksrc, sim, cls.omega, magnetic=True)
        cls.res = sim.run(
            cls.sources, full_probes(grid), cls.omega, StopRule(tol=1e-12, f_lo=7.5e9)
        )

    def test_factors_vary(self):
        self.assertTrue(self.s.edge_correction)
        for c in ("hx", "hy", "hz"):
            m = self.s.mu_factor(c)
            self.assertLess(m.min(), 0.9)
        self.assertLess(self.s.eps("ez").min() / self.s.eps_base("ez").min(), 0.95)


class InductiveSheetTest(unittest.TestCase):
    """The same identity with an inductive copper sheet (`Structure.set_sheet`): on the
    copper-plane edges c_ω σ becomes the complex Σ w c_ω Y_d/Δz of the branch currents."""

    @classmethod
    def setUpClass(cls):
        grid, st = small_grid()
        s = Structure(grid, st)
        rng = np.random.default_rng(5)
        g = st.g_min * (st.g_max / st.g_min) ** rng.uniform(0, 0.85, grid.n[:2])
        sim0 = Simulation(grid, s)
        dt = sim0.dt
        # Inductances with Ω L G from about 0.01 to 30 at 9 GHz: resistive to reactive branches.
        lg = 10.0 ** rng.uniform(-2.0, 1.5, grid.n[:2]) / (2 * np.pi * 9e9)
        s.set_sheet(g, lg / g, dt=dt)
        sim = Simulation(grid, s, dt=dt)
        cls.grid, cls.sim, cls.s = grid, sim, s
        cls.omega = 2 * np.pi * np.array([7.5e9, 9e9, 10.5e9])
        pulse = GaussianPulse(9e9, 2.5e9)
        src = PulseSource("ez", grid.flat_index("ez", 6, 6, [0, 1]), [1.0, -0.5], pulse, sim.dt)
        cls.src = src
        cls.jhat = source_dtft(src, sim, cls.omega, magnetic=False)
        cls.res = sim.run([src], full_probes(grid), cls.omega, StopRule(tol=1e-12, f_lo=7.5e9))

    def test_converged(self):
        self.assertTrue(self.res.converged)
        self.assertTrue(self.s.inductive)

    def test_e_residual_on_the_sheet(self):
        g, sim, s = self.grid, self.sim, self.s
        big = numerical_omega(self.omega, sim.dt)
        cw = conductance_factor(self.omega, sim.dt)
        sheet = s.sheet_coefficient(self.omega, sim.dt)
        kc = g.k_c
        lo = [g.pml.along(a)[0] + 1 for a in range(3)]
        hi = [g.axis(a).n - g.pml.along(a)[1] - 1 for a in range(3)]
        worst = 0.0
        for m in range(self.omega.size):
            f = {c: self.res.dft[c][m].reshape(g.shape(c)) for c in COMPONENTS}

            def bc(v, a):
                sh = [1, 1, 1]
                sh[a] = v.size
                return v.reshape(sh)

            ch = {
                "ex": (np.diff(f["hz"], axis=1) / bc(g.y.dual[1:-1], 1))[:, :, 1:-1]
                - (np.diff(f["hy"], axis=2) / bc(g.z.dual[1:-1], 2))[:, 1:-1, :],
                "ey": (np.diff(f["hx"], axis=2) / bc(g.z.dual[1:-1], 2))[1:-1, :, :]
                - (np.diff(f["hz"], axis=0) / bc(g.x.dual[1:-1], 0))[:, :, 1:-1],
            }
            inner = {
                "ex": (slice(None), slice(1, -1), slice(1, -1)),
                "ey": (slice(1, -1), slice(None), slice(1, -1)),
            }
            for c in ("ex", "ey"):
                eps = s.eps(c)[inner[c]]
                sig = s.sigma(c).copy()
                sig[:, :, kc] -= s.sheet[c] / s.sheet_dz  # the instantaneous sheet part
                a = cw[m] * sig.astype(complex)
                a[:, :, kc] += sheet[c][0][m] + 1j * sheet[c][1][m]
                r = (-1j * big[m] * eps + a[inner[c]]) * f[c][inner[c]] - ch[c]
                plane = r[:, :, kc - 1]
                sl = tuple(slice(lo[ax] - 1, hi[ax] - 1) for ax in range(2))
                scale = np.abs(ch[c][sl + (slice(None),)]).max()
                worst = max(worst, np.abs(plane[sl]).max() / scale)
        self.assertLess(worst, 1e-10)


class DecimationTest(unittest.TestCase):
    def test_decimated_dtft_matches(self):
        grid, st = small_grid()
        s = Structure(grid, st)
        sim = Simulation(grid, s)
        omega = 2 * np.pi * np.array([8e9, 10e9, 12e9])
        pulse = GaussianPulse(10e9, 3e9)
        src = PulseSource("ez", grid.flat_index("ez", 6, 6, [0, 1]), 1.0, pulse, sim.dt)
        probe = Probe("p", "ey", box_indices(grid, "ey", [(5, 9), (5, 8), (2, 3)]))
        hprobe = Probe("q", "hx", box_indices(grid, "hx", [(5, 9), (5, 8), (2, 3)]))
        stop = StopRule(tol=1e-12, f_lo=8e9)
        ref = sim.run([src], [probe, hprobe], omega, stop)
        # f_top where the pulse spectrum falls below −100 dB of its peak.
        f_top = 10e9 + 3e9 * np.sqrt(np.log(1e5) / np.log(10.0))
        d = decimation(f_top, sim.dt)
        self.assertGreater(d, 1)
        dec = sim.run([src], [probe, hprobe], omega, stop, decimation=d)
        for name in ("p", "q"):
            err = np.abs(dec.dft[name] - ref.dft[name]).max() / np.abs(ref.dft[name]).max()
            self.assertLess(err, 1e-6, name)


if __name__ == "__main__":
    unittest.main()
