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
"""

from __future__ import annotations

import math
import unittest

import numpy as np
import torch

from yapnr.rf.board import BoardDomain, BoardGeometry, LumpedPortSpec
from yapnr.rf.constants import C0, EPS0
from yapnr.rf.farfield import FarField, intensity_factor
from yapnr.rf.fdtd.engine import Simulation
from yapnr.rf.fdtd.sources import GaussianPulse
from yapnr.rf.fdtd.stop import StopRule
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


if __name__ == "__main__":
    unittest.main()
