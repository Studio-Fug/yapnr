"""Board models (design §26.1, `board`) and the far field of the FDTD (V2).

- The grid: the board's, the ground's and the keepouts' edges are nodes, the ground on z = 0
  and the copper on z = h, a CPML below the air under a free board and none over an infinite
  ground; the substrate's εr only inside the block; PEC on the ground plane's edges next to a
  ground pixel (none under the keepout); the Huygens box in air on every face.
- A short vertical current element (a lumped port's column, kL = 0.13) in free space: the
  pattern is sin²θ to 0.03 dB over 10–170°, the directivity at θ = 90° (from the box's flux)
  1.5 to 0.02 dB, and the quadrature's radiated power the box's flux to 1e-3 (measured 0.002
  dB, 0.0004 dB and 4e-4 with the box 5 cells off the board; 6e-3 two cells off, where the
  element's reactive near field, ten times its far field at 0.1 λ, must cancel on the box).
- The same element standing on an infinite ground (the image transform, upper half space):
  sin²θ, directivity 3.0 (4.77 dBi).
- The tiny board spec: a design runs, its problem describes the board, the far field and the
  warnings.
- Electric currents on Yee edges against the exact far field of the same discrete currents: a
  one-cell loop (E along φ̂ only) and a horizontal dipole λ/4 over an infinite ground (odd
  images).
"""

from __future__ import annotations

import math
import unittest

import numpy as np
import torch

from yapnr.rf.board import BoardDomain, BoardGeometry, LumpedPortSpec
from yapnr.rf.constants import C0, EPS0, ETA0
from yapnr.rf.farfield import FarField, intensity_factor
from yapnr.rf.fdtd.engine import Simulation
from yapnr.rf.fdtd.sources import GaussianPulse, PulseSource
from yapnr.rf.fdtd.stop import StopRule
from yapnr.rf.ports import source_dtft
from yapnr.rf.problem import Problem
from yapnr.rf.stackup import Stackup
from yapnr.rf.testing import tiny_board_spec

F0 = 3e9


class BoardGridTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.p = Problem(tiny_board_spec(ground="keepout"), exact=True)
        cls.inf = Problem(tiny_board_spec(ground="infinite"), exact=True)

    def test_nodes_and_planes(self):
        dom, g = self.p.domain, self.p.grid
        for v in (-2.4e-3, 7.2e-3, 2.4e-3, 4.8e-3):
            g.x.node(v)  # raises unless a node
        for v in (-4.0e-3, 4.0e-3, -2.4e-3, 2.4e-3):
            g.y.node(v)
        self.assertAlmostEqual(g.z.nodes[dom.k_g], 0.0, delta=1e-12)
        self.assertAlmostEqual(g.z.nodes[g.k_c], 0.8e-3, delta=1e-12)
        self.assertEqual(g.pml.z_lo, 6)
        self.assertEqual(self.inf.grid.pml.z_lo, 0)
        self.assertEqual(self.inf.domain.k_g, 0)

    def test_materials(self):
        dom, g = self.p.domain, self.p.grid
        st = self.p.sim.structure
        er, _ = dom.cells()
        self.assertTrue(np.all(er[~dom.board_mask] == 1.0))
        self.assertTrue(np.all(er[dom.board_mask][:, dom.k_b : g.k_c] == 3.0))
        self.assertTrue(np.all(er[:, :, g.k_c :] == 1.0))
        ez = st.eps("ez") / EPS0
        self.assertAlmostEqual(float(ez.max()), 3.0)
        self.assertAlmostEqual(float(ez[:, :, -1].max()), 1.0)
        # PEC on the ground plane next to a ground pixel; none under the keepout.
        pec = st.pec["ex"][:, :, dom.k_g]
        i0, i1 = g.x.node(2.4e-3), g.x.node(4.8e-3)
        j0, j1 = g.y.node(-2.4e-3), g.y.node(2.4e-3)
        self.assertFalse(pec[i0 + 1 : i1 - 1, j0 + 1 : j1].any())
        self.assertTrue(pec[g.x.node(0.0), g.y.node(0.0)])
        self.assertFalse(st.pec["ex"][:, :, dom.k_g + 1].any())

    def test_huygens_box_in_air(self):
        st = self.p.sim.structure
        box = self.p.box.box
        for face in box.faces:
            for pair in (0, 1):
                pr = face.probes[3 * pair]
                eps = st.eps(pr.comp).reshape(-1)[pr.index] / EPS0
                self.assertTrue(np.allclose(eps, 1.0), pr.name)
                self.assertFalse(st.pec[pr.comp].reshape(-1)[pr.index].any(), pr.name)
                self.assertFalse(np.any(st.sigma(pr.comp).reshape(-1)[pr.index]), pr.name)

    def test_design_runs(self):
        rng = np.random.default_rng(0)
        ev = self.p.evaluate(rng.uniform(0.2, 0.8, self.p.design_shape), gradients=False)
        self.assertTrue(ev.converged)
        self.assertTrue(np.all(np.isfinite(ev.values)))
        eta = ev.eta[1]
        self.assertTrue(np.all((eta > 0) & (eta < 1)))
        d = self.p.describe()
        self.assertEqual(d["board"]["model"], "free board")
        self.assertEqual(
            d["radiation_box"]["eta"], "total efficiency (radiated power over incident)"
        )
        self.assertEqual(d["port_extraction"], "lumped")

    def test_warning_without_a_gain_floor(self):
        spec = tiny_board_spec(
            requirements=[{"shape": 1, "target": "beam", "max_rms_db": 3.0, "band": "b"}]
        )
        codes = [a["code"] for a in Problem(spec, exact=True).assumptions()]
        self.assertIn("pattern_without_gain_floor", codes)
        self.assertNotIn("eta_nonguided", codes)


def element(image: bool, clearance: int = 5):
    """A short vertical current element (a lumped port's 2 mm column) on a board of air, in
    free space or on an infinite ground; returns (directions' U / P_box per frequency, the
    directions, P_ff/P_box)."""
    lam = C0 / F0
    st = Stackup(1.0, 0.0, 2e-3, F0)
    side = ((-4e-3, 4e-3), (-4e-3, 4e-3))
    geom = BoardGeometry(
        stackup=st,
        pitch=1e-3,
        n_sub=2,
        design=(-2e-3, 2e-3, -2e-3, 2e-3),
        outline=side,
        thickness=2e-3,
        ground="infinite" if image else side,
        keepouts=() if image else (side,),
        ports=(LumpedPortSpec(1, (0.0, 0.0), (0.0, 0.0), 50.0),),
        f_max=4e9,
        air=lam / 4,
        max_cell=2.5e-3,
        require_ground=False,
    )
    dom = BoardDomain(geom)
    g = dom.grid
    box = dom.huygens_box(clearance)
    ff = FarField(box, image=dom.infinite)
    s = dom.structure()
    port = dom.ports[0]
    port.apply(s)
    sim = Simulation(g, s, dt=0.95 * g.courant_dt())
    pulse = GaussianPulse.for_band(2e9, 4e9)
    om = np.array([2 * math.pi * F0])
    res = sim.run(
        [port.source(pulse, sim.dt)], port.probes + box.probes, om, StopRule(tol=1e-6, f_lo=2e9)
    )
    p_box = float(np.asarray(box.power(res.dft)).ravel()[0])
    dft = {k: torch.as_tensor(v) for k, v in res.dft.items()}
    qd, qw, _, _ = ff.quadrature(om)
    dirs = np.concatenate([qd, [[1.0, 0.0, 0.0], [0.0, -1.0, 0.0]]])
    f = ff.vectors(dft, om, dirs)[0].numpy()
    u = intensity_factor(om)[0] * (np.abs(f) ** 2).sum(1)
    return u / p_box, dirs, float(u[: qw.size] @ qw) / p_box


class ShortElementTest(unittest.TestCase):
    def check(self, image: bool, d0: float):
        u, dirs, p_ff = element(image)
        theta = np.arccos(np.clip(dirs[:, 2], -1, 1))
        sel = (theta > math.radians(10)) & (theta < math.radians(170))
        d = 4 * math.pi * u
        err = 10 * np.log10(d[sel] / (d0 * np.sin(theta[sel]) ** 2))
        self.assertLess(np.abs(err).max(), 0.03)
        for k in (-2, -1):  # θ = 90° along x and −y
            self.assertAlmostEqual(10 * math.log10(d[k] / d0), 0.0, delta=0.02)
        self.assertLess(abs(p_ff - 1.0), 1e-3)

    def test_free_space_dipole(self):
        self.check(False, 1.5)

    def test_monopole_on_an_infinite_ground(self):
        self.check(True, 3.0)

    def test_near_field_box(self):
        """Two cells off the board the box sits at 0.1 λ, where the element's reactive near
        field dominates: the quadrature's power is still within the 1 % check."""
        _, _, p_ff = element(False, clearance=2)
        self.assertLess(abs(p_ff - 1.0), 0.01)


def current_sources(image: bool, build, height: int = 2, pitch: float = 5e-3):
    """The FDTD far field of electric currents J on Yee edges (`build(grid, domain)` → [(comp,
    (i, j, k), sign)]) in a uniform grid of air at `pitch` (λ/20 at 3 GHz), in free space or
    over an infinite PEC ground, against the exact far field of the same discrete currents:
    point elements of moment J(ω) A_dual l at the edges' centres (and their images, (−J_x, −J_y,
    +J_z) at −z). Returns U/P_box and U_exact/P_exact on a θ–φ grid (θ 10–170°, or 0–85° over
    the ground), F there (D, 3), the grid's directions and P_ff/P_box."""
    lam = C0 / F0
    half = 3 * pitch
    side = ((-half, half), (-half, half))
    geom = BoardGeometry(
        stackup=Stackup(1.0, 0.0, height * pitch, F0),
        pitch=pitch,
        n_sub=height,
        design=(-pitch, pitch, -pitch, pitch),
        outline=side,
        thickness=height * pitch,
        ground="infinite" if image else side,
        keepouts=() if image else (side,),
        f_max=4e9,
        air=0.3 * lam,
        max_cell=pitch,
        core_cells=1,
        require_ground=False,
    )
    dom = BoardDomain(geom)
    g = dom.grid
    box = dom.huygens_box(2)
    ff = FarField(box, image=dom.infinite)
    sim = Simulation(g, dom.structure(), dt=0.95 * g.courant_dt())
    pulse = GaussianPulse.for_band(2e9, 4e9)
    om = np.array([2 * math.pi * F0])
    jw = source_dtft(pulse, om, sim.dt)[0]
    sources, elements = [], []
    for comp, (i, j, k), sign in build(g, dom):
        i, j, k = (np.atleast_1d(v) for v in (i, j, k))
        a = "xyz".index(comp[1])
        idx = g.flat_index(comp, i, j, k)
        sources.append(PulseSource(comp, idx, sign, pulse, sim.dt))
        moment = sign * jw * np.ones(idx.size)
        pos = np.stack([g.positions(comp, b)[v] for b, v in enumerate((i, j, k))], axis=-1)
        for b, v in enumerate((i, j, k)):
            moment = moment * (g.axis(b).primary[v] if b == a else g.axis(b).dual[v])
        u = np.zeros(3)
        u[a] = 1.0
        elements.append((pos, u, moment))
        if image:
            elements.append((pos * [1, 1, -1], u * [-1, -1, 1], moment))
    res = sim.run(sources, box.probes, om, StopRule(tol=1e-6, f_lo=2e9))
    p_box = float(np.asarray(box.power(res.dft)).ravel()[0])
    qd, qw, _, _ = ff.quadrature(om)
    tt, pp = np.meshgrid(
        np.radians(np.arange(0.0, 85.1, 5.0) if image else np.arange(10.0, 170.1, 5.0)),
        np.radians(np.arange(0.0, 359.0, 15.0)),
        indexing="ij",
    )
    st = np.sin(tt)
    grid = np.stack([st * np.cos(pp), st * np.sin(pp), np.cos(tt)], -1).reshape(-1, 3)
    dirs = np.concatenate([qd, grid])
    dft = {k: torch.as_tensor(v) for k, v in res.dft.items()}
    f = ff.vectors(dft, om, dirs)[0].numpy()
    k0 = om[0] / C0
    n = np.zeros((dirs.shape[0], 3), complex)
    for pos, u, moment in elements:
        n += (np.exp(-1j * k0 * dirs @ pos.T) @ moment)[:, None] * u[None]
    fa = ETA0 * (n - (n * dirs).sum(-1, keepdims=True) * dirs)
    fac = intensity_factor(om)[0]
    u = fac * (np.abs(f) ** 2).sum(-1)
    ua = fac * (np.abs(fa) ** 2).sum(-1)
    nq = qw.size
    p_exact = float(ua[:nq] @ qw)
    return u[nq:] / p_box, ua[nq:] / p_exact, f[nq:], grid, float(u[:nq] @ qw) / p_box


class CurrentSourcesTest(unittest.TestCase):
    """The transform of FDTD runs against the exact far field of the same discrete currents at
    3 GHz: a one-cell loop at λ/20 (a magnetic dipole: E along φ̂ only, the M/J signs of every
    face; measured 0.06 dB) and a horizontal dipole λ/4 above an infinite ground at λ/40 (the
    odd images of J_x and M_z; measured 0.019 dB, 0.13 dB at λ/20: the grid's dispersion along
    the λ/4 path, second order)."""

    def check(self, d, d_exact, tol_db):
        sel = d_exact > 1e-2 * d_exact.max()  # within 20 dB of the peak
        err = np.abs(10 * np.log10(d[sel] / d_exact[sel]))
        self.assertLess(err.max(), tol_db)
        self.assertAlmostEqual(10 * math.log10(d.max() / d_exact.max()), 0.0, delta=0.05)

    def test_small_loop(self):
        def loop(g, dom):
            i, j, k = g.x.node(0.0), g.y.node(0.0), dom.k_g + 1
            return [
                ("ex", (i, j, k), 1.0),
                ("ey", (i + 1, j, k), 1.0),
                ("ex", (i, j + 1, k), -1.0),
                ("ey", (i, j, k), -1.0),
            ]

        u, ua, f, dirs, p_ff = current_sources(False, loop)
        self.check(4 * math.pi * u, 4 * math.pi * ua, 0.1)
        self.assertAlmostEqual(4 * math.pi * u.max(), 1.5, delta=0.01)
        self.assertLess(abs(p_ff - 1.0), 2e-3)
        # E along φ̂ only: the θ̂ component 40 dB below the peak
        theta = np.arccos(np.clip(dirs[:, 2], -1, 1))
        phi = np.arctan2(dirs[:, 1], dirs[:, 0])
        ct, sp, cp = np.cos(theta), np.sin(phi), np.cos(phi)
        that = np.stack([ct * cp, ct * sp, -np.sin(theta)], -1)
        f_t = np.abs((f * that).sum(-1)) ** 2
        self.assertLess(10 * math.log10(f_t.max() / (np.abs(f) ** 2).sum(-1).max()), -40.0)

    def test_horizontal_dipole_over_ground(self):
        def dipole(g, dom):
            return [("ex", (g.x.node(0.0), g.y.node(0.0), 10), 1.0)]

        u, ua, _, _, p_ff = current_sources(True, dipole, height=12, pitch=2.5e-3)
        self.check(4 * math.pi * u, 4 * math.pi * ua, 0.05)
        self.assertLess(abs(p_ff - 1.0), 2e-3)


if __name__ == "__main__":
    unittest.main()
