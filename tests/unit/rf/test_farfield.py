"""The near-to-far-field transform against analytic fields (design §26.2, V1; `farfield`).

Exact near fields of point current moments (electric p and magnetic m, e^{−iωt}) are sampled
on a `FluxBox`'s probes (E on the face's node plane, H on both half-cell planes, as the solver
records them) on a box of ±λ/4 at λ/40 cells, and transformed:

- a z dipole: F = η0 p_⊥ (to 1e-3 of its peak), U ∝ sin²θ, D = 1.5 and the quadrature's
  radiated power equal to the closed form and to the box's Poynting flux (to 1e-3);
- a small loop (a z magnetic dipole): the field is φ̂-polarized, F_φ = −sinθ m (M and J signs);
- crossed dipoles x̂ + iŷ: right-hand circular towards +z and left-hand towards −z (the IEEE
  sense of `farfield.components` in the e^{−iωt} convention), axial ratio 0 dB on the axis;
- images: a vertical and a horizontal dipole λ/20 over a PEC ground, the box on the ground,
  against the two-element array of the dipole and its image; the vertical one's directivity
  (3.0 for a dipole on the ground);
- the error falls as (kΔ)² with the cells;
- the transform's Wirtinger gradient against finite differences of the probe values.
"""

from __future__ import annotations

import math
import unittest

import numpy as np
import torch

from yapnr.rf.adjoint import wirtinger
from yapnr.rf.constants import C0, ETA0
from yapnr.rf.farfield import FarField, Frame, components, intensity_factor
from yapnr.rf.fdtd.monitors import FluxBox
from yapnr.rf.mesh import Axis, Grid, PMLCells

F0 = 3e9
LAM = C0 / F0
K = 2 * math.pi / LAM
OMEGA = np.array([2 * math.pi * F0])


def fields(points, sources):
    """(E, H) (P, 3) of point current moments [(r0, p, m)] (A·m, V·m) at `points` (P, 3)."""
    e = np.zeros(points.shape, complex)
    h = np.zeros(points.shape, complex)
    w = K * C0
    for r0, p, m in sources:
        d = points - np.asarray(r0, float)[None]
        r = np.linalg.norm(d, axis=1)[:, None]
        n = d / r
        g = np.exp(1j * K * r) / (4 * math.pi * r)
        kr = K * r
        a = 1 + 1j / kr - 1 / kr**2
        b = 1 + 3j / kr - 3 / kr**2
        grad = (1j * K - 1 / r) * g  # ∇G = grad · n

        def ge(v):
            v = np.asarray(v, complex)[None]
            return g * (a * v - b * (n * v).sum(1, keepdims=True) * n)

        def cross(u, v):
            return np.cross(np.broadcast_to(u, n.shape), np.broadcast_to(v, n.shape))

        p, m = np.asarray(p, complex), np.asarray(m, complex)
        mu, eps = ETA0 / C0, 1 / (ETA0 * C0)
        e += 1j * w * mu * ge(p) - grad * cross(n, m[None])
        h += grad * cross(n, p[None]) + 1j * w * eps * ge(m)
    return e, h


def far_vector(dirs, sources):
    """The exact F (D, 3) of the moments: Σ e^{−ik r̂·r0} (η0 p_⊥ − r̂ × m)."""
    out = np.zeros(dirs.shape, complex)
    for r0, p, m in sources:
        ph = np.exp(-1j * K * dirs @ np.asarray(r0, float))[:, None]
        p, m = np.asarray(p, complex)[None], np.asarray(m, complex)[None]
        p_perp = p - (dirs * p).sum(1, keepdims=True) * dirs
        out += ph * (ETA0 * p_perp - np.cross(dirs, np.broadcast_to(m, dirs.shape)))
    return out


def box_dft(box, grid, sources):
    out = {}
    for pr in box.probes:
        ijk = grid.unravel(pr.comp, pr.index)
        pts = np.stack([grid.positions(pr.comp, a)[ijk[a]] for a in range(3)], axis=1)
        e, h = fields(pts, sources)
        out[pr.name] = (e if pr.comp[0] == "e" else h)[:, "xyz".index(pr.comp[1])][None]
    return out


def make_box(image=False, cells=40, half=0.25):
    n = int(round(2 * half * cells))
    d = LAM / cells
    ax = Axis((np.arange(n + 1) - n / 2) * d)
    z = Axis(np.arange(n // 2 + 2) * d) if image else ax
    grid = Grid(ax, ax, z, 1, PMLCells(0, 0, 0, 0, 0))
    nz = z.n
    node_box = ((1, n - 1), (1, n - 1), (0 if image else 1, nz - 1))
    faces = ("x-", "x+", "y-", "y+", "z+") if image else None
    return grid, FluxBox(grid, "nf", node_box, faces=faces)


def directions(n=400, upper=False):
    rng = np.random.default_rng(0)
    v = rng.standard_normal((n, 3))
    v /= np.linalg.norm(v, axis=1, keepdims=True)
    if upper:
        v[:, 2] = np.abs(v[:, 2])
    return v


class FreeSpaceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.grid, cls.box = make_box()
        cls.ff = FarField(cls.box)
        cls.dirs = directions()

    def transform(self, sources, dirs=None):
        dirs = self.dirs if dirs is None else dirs
        dft = {k: torch.as_tensor(v) for k, v in box_dft(self.box, self.grid, sources).items()}
        return dft, self.ff.vectors(dft, OMEGA, dirs)[0].numpy()

    def check_vector(self, sources, tol=1e-3):
        _, f = self.transform(sources)
        want = far_vector(self.dirs, sources)
        err = np.abs(f - want).max() / np.abs(want).max()
        self.assertLess(err, tol)
        return f

    def test_z_dipole(self):
        src = [((0.002, -0.001, 0.003), (0, 0, 1e-3), (0, 0, 0))]
        f = self.check_vector(src)
        theta = np.arccos(self.dirs[:, 2])
        u = intensity_factor(OMEGA)[0] * (np.abs(f) ** 2).sum(1)
        u_max = intensity_factor(OMEGA)[0] * (ETA0 * 1e-3) ** 2
        np.testing.assert_allclose(u / u_max, np.sin(theta) ** 2, atol=2e-3)
        # Directivity and radiated power by the quadrature, against the closed form and the
        # box's flux over the same samples.
        dft, _ = self.transform(src)
        q_dirs, q_w, _, _ = self.ff.quadrature(OMEGA)
        fq = self.ff.vectors(dft, OMEGA, q_dirs)[0].numpy()
        uq = intensity_factor(OMEGA)[0] * (np.abs(fq) ** 2).sum(1)
        p_ff = float(uq @ q_w)
        p_exact = ETA0 * K**2 * 1e-6 / (12 * math.pi)
        p_box = float(np.asarray(self.box.power({k: v.numpy() for k, v in dft.items()}))[0])
        self.assertAlmostEqual(p_ff / p_exact, 1.0, delta=2e-3)
        self.assertAlmostEqual(p_box / p_exact, 1.0, delta=2e-3)
        self.assertAlmostEqual(4 * math.pi * uq.max() / p_ff, 1.5, delta=3e-3)

    def test_loop(self):
        src = [((0.0, 0.001, -0.002), (0, 0, 0), (0, 0, 2.0))]
        f = self.check_vector(src)
        fr = Frame()
        th, ph = fr.unit_vectors(*fr.angles(self.dirs))
        f_t, f_p = (f * th).sum(1), (f * ph).sum(1)
        self.assertLess(np.abs(f_t).max() / np.abs(f_p).max(), 2e-3)

    def test_crossed_dipoles_circular(self):
        src = [((0, 0, 0), (1e-3, 1e-3j, 0), (0, 0, 0))]
        self.check_vector(src)
        axis = np.array([[0, 0, 1.0], [0, 0, -1.0]])
        _, f = self.transform(src, axis)
        f = torch.as_tensor(f)[None]
        r = components(f, axis, Frame(), "rhcp")[0].numpy()
        left = components(f, axis, Frame(), "lhcp")[0].numpy()
        self.assertGreater(r[0] / left[0], 1e4)  # +z: right-hand
        self.assertGreater(left[1] / r[1], 1e4)  # −z: left-hand

    def test_mixed_sources(self):
        src = [
            ((0.01, 0.0, 0.0), (1e-3, 0, 5e-4j), (0, 0.3, 0)),
            ((-0.005, 0.008, 0.004), (0, 2e-4, 0), (0.1j, 0, 0.2)),
        ]
        self.check_vector(src)

    def test_second_order(self):
        """The transform's error falls as (kΔ)² (the faces' quadrature at their rims; the
        averaging correction c_J removes the H̄ smoothing's cos(k_nΔ/2)): measured 3.2e-3 at
        λ/20 and 7.9e-4 at λ/40 cells for a dipole."""
        src = [((0, 0, 0), (0, 0, 1e-3), (0, 0, 0))]
        want = far_vector(self.dirs, src)
        err = []
        for cells in (20, 40):
            grid, box = make_box(cells=cells)
            dft = {k: torch.as_tensor(v) for k, v in box_dft(box, grid, src).items()}
            f = FarField(box).vectors(dft, OMEGA, self.dirs)[0].numpy()
            err.append(np.abs(f - want).max() / np.abs(want).max())
        self.assertLess(err[0], 5e-3)
        self.assertLess(err[1] / err[0], 0.3)

    def test_wirtinger_gradient(self):
        src = [((0.002, 0, 0), (2e-4, 0, 1e-3), (0, 0.1, 0))]
        dft = box_dft(self.box, self.grid, src)
        dirs = self.dirs[:5]
        names = self.ff.probe_names

        def fn(q):
            f = self.ff.vectors(q, OMEGA, dirs)
            u = (f.real**2 + f.imag**2).sum(-1)
            return torch.log(u[:, 0] / u.sum(-1))

        _, g = wirtinger(fn, dft, names)
        rng = np.random.default_rng(2)
        dirn = {
            n: rng.standard_normal(dft[n].shape) + 1j * rng.standard_normal(dft[n].shape)
            for n in names
        }
        scale = {n: np.abs(dft[n]).max() for n in names}
        h = 1e-6

        def val(t):
            q = {n: torch.as_tensor(dft[n] + t * scale[n] * dirn[n]) for n in names}
            return float(fn(q)[0])

        fd = (val(h) - val(-h)) / (2 * h)
        adj = sum(2 * np.real(np.sum(g[n] * scale[n] * dirn[n])) for n in names)
        self.assertAlmostEqual(adj, fd, delta=1e-6 * max(1.0, abs(fd)))


class ImageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.grid, cls.box = make_box(image=True)
        cls.ff = FarField(cls.box, image=True)
        cls.dirs = directions(upper=True)

    def check(self, src, image_src):
        dft = {
            k: torch.as_tensor(v) for k, v in box_dft(self.box, self.grid, src + image_src).items()
        }
        f = self.ff.vectors(dft, OMEGA, self.dirs)[0].numpy()
        want = far_vector(self.dirs, src + image_src)
        err = np.abs(f - want).max() / np.abs(want).max()
        self.assertLess(err, 2e-3)
        return dft

    def test_vertical_dipole_over_ground(self):
        z0 = LAM / 20
        src = [((0.0, 0.0, z0), (0, 0, 1e-3), (0, 0, 0))]
        dft = self.check(src, [((0.0, 0.0, -z0), (0, 0, 1e-3), (0, 0, 0))])
        q_dirs, q_w, theta, _ = self.ff.quadrature(OMEGA)
        f = self.ff.vectors(dft, OMEGA, q_dirs)[0].numpy()
        u = (np.abs(f) ** 2).sum(1)
        d = 4 * math.pi * u.max() / float(u @ q_w)
        # Two in-phase dipoles 2 z0 apart, over the upper hemisphere.
        x, w = np.polynomial.legendre.leggauss(64)
        ct = 0.5 * (x + 1)
        pat = (1 - ct**2) * np.cos(K * z0 * ct) ** 2
        want = 2 * pat.max() / float(0.5 * w @ pat)
        self.assertAlmostEqual(d, want, delta=3e-3)
        self.assertAlmostEqual(d, 3.0, delta=0.1)  # a dipole on the ground

    def test_horizontal_dipole_over_ground(self):
        z0 = LAM / 20
        self.check(
            [((0.004, 0.0, z0), (1e-3, 0, 0), (0, 0, 0))],
            [((0.004, 0.0, -z0), (-1e-3, 0, 0), (0, 0, 0))],
        )

    def test_magnetic_current_over_ground(self):
        z0 = LAM / 25
        self.check(
            [((0.0, 0.003, z0), (0, 0, 0), (0.5, 0, 0.3))],
            [((0.0, 0.003, -z0), (0, 0, 0), (0.5, 0, -0.3))],
        )


if __name__ == "__main__":
    unittest.main()
