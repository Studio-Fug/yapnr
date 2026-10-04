"""Subcell correction of the zero-thickness copper's edges (static-field Hodge factors).

The copper is a sheet on the node plane z = h. At a sheet's edge the transverse fields are
singular, E, H ~ r^(−1/2) (a knife edge; the exponent stays ½ when the edge lies on the
substrate–air interface, since the sheet and the interface are coplanar), and the Yee scheme's
constitutive relations, which assume fields uniform over each edge and dual face, misjudge the
charge and the current there. Uncorrected, a strip acts about half a cell wider per edge on the
cases' grids (the line impedance is 6 % low on 6 cells, and resonators tuned on one grid
resonate 2 % higher on a grid three times finer).

The correction follows the static-field method of Shorthouse and Railton [1] (the singular
static field built into the update coefficients of the cells next to an edge; see also the
contour-path subcell models of Taflove and Hagness [2], ch. 10, and the edge-condition
exponents of Meixner [3]). Write the scheme with integral unknowns: e = ∫E·dl on primary
edges, b = ∫B·dA on primary faces, h = ∫H·dl on dual edges, d = ∫D·dA on dual faces. Faraday's
and Ampère's laws in these unknowns are exact; only the discrete constitutive relations

    d = ε (A_d / L_p) e,        b = μ (A_p / L_d) h

assume uniform fields. For the static edge field φ = Re √w (w = s + i z', s the distance from
the edge in the sheet's plane, z' the height above it; the metal lies on s < 0, z' = 0), the
ratio κ = d_true / d_uniform of the edge-adjacent unknowns follows in closed form, and the
correction multiplies ε of those E edges and 1/μ of the dual H edges by κ (the magnetic field
of the strip current has the same singular shape, H_t ∝ x̂ × ∇ψ with ψ = Re √w):

- **in-plane, across the edge** (the E edge from the edge into the void, its dual face a
  vertical segment at s = g/2 over ±Δz/2; and the H_z of that void cell):
  κ_t = [ε_lo I(Δz_lo/2) + ε_hi I(Δz_hi/2)] / [(ε_lo Δz_lo + ε_hi Δz_hi)/2 · 2/√g],
  I(a) = ∫₀^a Re (g/2 + iz)^(−½) dz = 2 Im √(g/2 + ia);
- **a one-cell slot** (both ends of the in-plane edge on copper, slot-line field
  ∝ 1/√(g²/4 − y²)): κ_slot = [ε_lo asinh(Δz_lo/g) + ε_hi asinh(Δz_hi/g)] / [(ε_lo Δz_lo +
  ε_hi Δz_hi)/2 · π/g];
- **normal, above and below the edge** (E_z on the edge's node line, its dual face a
  horizontal segment at Δz/2 across the edge; and the H tangential to the plane, normal to
  the edge, on the edge's segments): κ_n = [(Im √(g/2 + iΔz/2) − Im √(−g/2 + iΔz/2))/g] /
  [√(Δz/2)/Δz].

On the cases' grids (S1, 0.3 mm, 4 substrate cells) κ_t = 0.673 and κ_n = 0.600. The H
component along the edge and E along the edge (∝ r^(½)) need no correction. The factors depend
only on the cell sizes, not on the field's amplitude, so they are applied wherever a copper
edge is, and their effect vanishes with the cells (they act on a region of one cell).

**The next ring and corners.** The same static field gives the factors of E_z one cell from
an edge (1.21 on the void side, 1.03 under the copper on the cases' grids, and the H
tangential to the plane there): with them the line's ε_eff and impedance agree with a grid
three times finer to about 0.2 % (0.35 % with the first ring alone). At a corner node the field
is that of a flat sector, φ = R^ν F(θ, ϕ), F the lowest Dirichlet eigenfunction of the
Laplace–Beltrami operator on the unit sphere slit along the sector's arc (`_sector_field`;
ν = 0.297 for a convex 90° corner as in Morrison and Lewis [4], 0.81 for a concave one, and
the 180° slit reproduces the straight edge, ν = ½ and κ_n to 1 %). E_z on a convex (concave)
corner node gets κ_n times κ_sector/κ_180° (0.62 and 1.48 on the cases' grids); without that an
open stub and a patch still resonated 0.5–0.6 % low on the optimization grid, with it 0.13 %.

**Pixels.** The copper is a pixel field c ∈ [0, 1] on the plane (1 copper, 0 void, gray in
between during optimization). Every factor is a multilinear interpolation over the copper
configurations of the pixels around its edge (two pixels around at most), exact for binary
pixels and smooth for gray ones, so the gradient is exact (`vjp`):

- E_z (node; four pixels): mixed configurations (an edge) κ_n, one or three copper (a convex or
  concave corner) the corner values, and the next ring when the four pixels agree and a pair
  of pixels beyond them differs;
- E_x, E_y in the plane (an edge of two void pixels; the pixel pairs beyond its two ends):
  1 + v[(κ_t − 1)(e₀ + e₁) + (κ_slot − 2κ_t + 1) e₀ e₁], v = Π(1 − c) of its own pixels,
  e = 1 − Π(1 − c) of each end pair;
- H_z (a pixel): 1 + (1 − c)[(κ_t − 1)(1 − Π_sides(1 − c)) + (κ_slot − κ_t)(1 − (1 − c_W
  c_E)(1 − c_S c_N))] (one side: κ_t; a one-cell slot or more: κ_slot; a corner: κ_t);
- H_x, H_y above and below the plane (an edge's two pixels): 1 + (κ_n − 1)(c₁ + c₂ − 2c₁c₂),
  plus the next ring.

The scheme stays symmetric (diagonal changes of ε and μ), so the adjoint is still an ordinary
run; ε is lowered next to edges and corners, and the stable time step follows from
`stable_dt`.

[1] D. B. Shorthouse, C. J. Railton, "The incorporation of static field solutions into the
    finite difference time domain algorithm," IEEE Trans. Microw. Theory Techn. 40(5),
    986–994 (1992).
[2] A. Taflove, S. C. Hagness, Computational Electrodynamics: The Finite-Difference
    Time-Domain Method, 3rd ed., Artech House (2005), ch. 10.
[3] J. Meixner, "The behavior of electromagnetic fields at edges," IEEE Trans. Antennas
    Propag. 20(4), 442–446 (1972).
[4] D. L. Morrison, J. A. Lewis, "Charge singularity at the corner of a flat plate," SIAM J.
    Appl. Math. 31(2), 233–250 (1976).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from yapnr.rf.mesh import Grid

# The version of the factor model (in calibration cache keys): 1 the first ring, 2 with the
# next ring of E_z and the tangential H, 3 with the corner nodes.
MODEL = 3

# Components and the planes (z indices relative to the copper plane k_c) they are corrected on.
E_PLANES = {"ex": (0,), "ey": (0,), "ez": (-1, 0)}
H_PLANES = {"hx": (-1, 0), "hy": (-1, 0), "hz": (0,)}


def kappa_inplane(g, dz_lo: float, dz_hi: float, eps_lo: float = 1.0, eps_hi: float = 1.0):
    """κ_t of an in-plane edge of length g running from a copper edge into the void."""
    g = np.asarray(g, dtype=np.float64)

    def integral(a):
        return 2.0 * np.sqrt(0.5 * g + 1j * a).imag

    num = eps_lo * integral(0.5 * dz_lo) + eps_hi * integral(0.5 * dz_hi)
    den = 0.5 * (eps_lo * dz_lo + eps_hi * dz_hi) * 2.0 / np.sqrt(g)
    return num / den


def kappa_slot(g, dz_lo: float, dz_hi: float, eps_lo: float = 1.0, eps_hi: float = 1.0):
    """κ_slot of an in-plane edge of length g across a one-cell slot (copper at both ends)."""
    g = np.asarray(g, dtype=np.float64)
    num = eps_lo * np.arcsinh(dz_lo / g) + eps_hi * np.arcsinh(dz_hi / g)
    den = 0.5 * (eps_lo * dz_lo + eps_hi * dz_hi) * math.pi / g
    return num / den


def kappa_normal(g, dz: float):
    """κ_n of a vertical edge of height dz on a copper edge's node line (dual width g)."""
    g = np.asarray(g, dtype=np.float64)
    a = 0.5 * dz
    flux = np.abs(np.sqrt(0.5 * g + 1j * a).imag - np.sqrt(-0.5 * g + 1j * a).imag)
    return (flux / g) / (math.sqrt(0.5 * dz) / dz)


def kappa_second(g, dz: float, side: int):
    """κ of a vertical edge of height dz one cell (g) from a copper edge's node line, on the
    void side (`side` = +1) or under/over the copper (−1): the next ring of the static field."""
    g = np.asarray(g, dtype=np.float64)
    s0 = side * g
    e = np.abs(np.sqrt(s0 + 1j * dz).real - np.sqrt(s0 + 0j).real)
    a = 0.5 * dz
    flux = np.abs(np.sqrt(s0 + 0.5 * g + 1j * a).imag - np.sqrt(s0 - 0.5 * g + 1j * a).imag)
    return (flux / g) / (e / dz)


def _sector_field(sector_deg: float, nt: int = 80):
    """The singular field φ = R^ν F(θ, ϕ) of a flat sheet sector with its vertex at the origin
    (the sector of angle `sector_deg` in the plane z = 0, from azimuth π): F the lowest
    Dirichlet eigenfunction of the Laplace–Beltrami operator on the unit sphere slit along the
    sector's arc, ν(ν + 1) its eigenvalue. Finite volumes on an (nt × 2nt) (θ, ϕ) grid, the
    block-tridiagonal system along θ solved by block LU, inverse iteration. 180° is the
    straight edge (ν = ½), 90° a convex corner (ν ≈ 0.297, Morrison and Lewis), 270° a concave
    one; the sheet's field is even in z, so a dielectric below the sheet does not change it."""
    na = 2 * nt
    dth, dph = math.pi / nt, 2.0 * math.pi / na
    th = (np.arange(nt) + 0.5) * dth
    ph = (np.arange(na) + 0.5) * dph
    a0, a1 = math.pi, math.pi + math.radians(sector_deg)
    slit = ((ph >= a0) & (ph < a1)) | ((ph + 2 * math.pi >= a0) & (ph + 2 * math.pi < a1))
    area = np.sin(th)[:, None] * dth * dph * np.ones((1, na))
    g_th = np.sin(np.arange(1, nt) * dth) * dph / dth
    g_ph = dth / (np.sin(th) * dph)
    eye = np.eye(na)
    ring = np.roll(eye, 1, axis=1) + np.roll(eye, -1, axis=1)
    diag = np.stack([g * (2.0 * eye - ring) for g in g_ph])
    off = np.empty((nt - 1, na, na))
    eq = nt // 2 - 1
    for i in range(nt - 1):
        g = g_th[i]
        w = np.where(slit, 0.0, g) if i == eq else np.full(na, g)
        to_slit = np.where(slit, 2.0 * g, 0.0) if i == eq else np.zeros(na)
        diag[i] += np.diag(w + to_slit)
        diag[i + 1] += np.diag(w + to_slit)
        off[i] = -np.diag(w)
    inv = np.empty_like(diag)
    gmat = np.empty_like(off)
    d = diag[0]
    for i in range(nt):
        if i > 0:
            d = diag[i] - off[i - 1].T @ gmat[i - 1]
        inv[i] = np.linalg.inv(d)
        if i < nt - 1:
            gmat[i] = inv[i] @ off[i]

    def solve(b):
        y = np.empty_like(b)
        for i in range(nt):
            y[i] = inv[i] @ (b[i] if i == 0 else b[i] - off[i - 1].T @ y[i - 1])
        x = np.empty_like(b)
        x[-1] = y[-1]
        for i in range(nt - 2, -1, -1):
            x[i] = y[i] - gmat[i] @ x[i + 1]
        return x

    f = np.ones((nt, na))
    lam = 0.0
    for _ in range(30):
        y = solve(area * f)
        lam = float(np.sum(f * area * f) / np.sum(f * area * y))
        f = y / math.sqrt(float(np.sum(y * area * y)))
    nu = 0.5 * (-1.0 + math.sqrt(1.0 + 4.0 * lam))
    return nu, f


def _sector_phi(field, x, y, z):
    nu, f = field
    nt, na = f.shape
    r = np.sqrt(x * x + y * y + z * z)
    ti = np.arccos(np.clip(z / r, -1.0, 1.0)) / (math.pi / nt) - 0.5
    ai = np.mod(np.arctan2(y, x), 2.0 * math.pi) / (2.0 * math.pi / na) - 0.5
    i0 = np.clip(np.floor(ti).astype(int), 0, nt - 2)
    fi = np.clip(ti - i0, 0.0, 1.0)
    j0 = np.floor(ai).astype(int) % na
    j1 = (j0 + 1) % na
    fa = ai - np.floor(ai)
    v = (f[i0, j0] * (1 - fi) + f[i0 + 1, j0] * fi) * (1 - fa)
    v = v + (f[i0, j1] * (1 - fi) + f[i0 + 1, j1] * fi) * fa
    return r**nu * v


def _sector_kappa(field, aspect: float, n: int = 200) -> float:
    """κ of the vertical edge (height 1) at the vertex, its dual face the square of side
    `aspect` at height ½ centred on it."""
    g = aspect
    e = abs(float(_sector_phi(field, np.array(1e-12), np.array(1e-12), np.array(1.0))))
    s = (np.arange(n) + 0.5) / n * g - 0.5 * g
    xx, yy = np.meshgrid(s, s, indexing="ij")
    h = 1e-4
    dphi = _sector_phi(field, xx, yy, 0.5 + h) - _sector_phi(field, xx, yy, 0.5 - h)
    d = abs(float(np.sum(dphi))) / (2 * h) * (g / n) ** 2
    return (d / g**2) / e


_SECTORS: dict = {}
_RATIOS: dict = {}


def corner_ratios(g, dz: float):
    """(convex, concave) corner factors relative to the straight edge's κ_n for E_z at a corner
    node of dual width g and height dz: κ_sector/κ_180° from the sector fields (the ratio
    cancels the sphere grid's error; 0.62 and 1.47 on the cases' grids)."""
    g = np.asarray(g, dtype=np.float64)
    if not _SECTORS:
        for sector in (90.0, 180.0, 270.0):
            _SECTORS[sector] = _sector_field(sector)
    out_x, out_v = np.empty(g.shape), np.empty(g.shape)
    for val in np.unique(np.round(g / dz, 9)):
        if val not in _RATIOS:
            ref = _sector_kappa(_SECTORS[180.0], val)
            _RATIOS[val] = (
                _sector_kappa(_SECTORS[90.0], val) / ref,
                _sector_kappa(_SECTORS[270.0], val) / ref,
            )
        sel = np.round(g / dz, 9) == val
        out_x[sel], out_v[sel] = _RATIOS[val]
    return out_x, out_v


@dataclass
class EdgeConstants:
    """The κ arrays of a grid (electric with the substrate, magnetic air-filled)."""

    ez: tuple  # (κ_n below, κ_n above) on the nodes (Nx+1, Ny+1)
    ex: tuple  # (κ_t, κ_slot) on the copper-plane Ex edges (Nx, 1)
    ey: tuple  # (κ_t, κ_slot) on the Ey edges (1, Ny)
    hz: tuple  # (κ_t, κ_slot) on the pixels (Nx, Ny), air-filled
    hx: tuple  # (κ_n below, κ_n above) on the Hx edges (Nx+1, 1)
    hy: tuple  # (κ_n below, κ_n above) on the Hy edges (1, Ny+1)
    # The next ring (E_z and the H tangential to the plane one cell from a straight edge), per
    # plane (below, above) and side (void, copper); None: first ring only.
    ez2: tuple | None = None
    hx2: tuple | None = None
    hy2: tuple | None = None
    # E_z on corner nodes, per plane (below, above): (convex, concave); None: the edge's κ_n.
    ez_corner: tuple | None = None

    @classmethod
    def build(
        cls, grid: Grid, er: float, *, second_ring: bool = True, corners: bool = True
    ) -> "EdgeConstants":
        kc = grid.k_c
        dz_lo = float(grid.z.primary[kc - 1])
        dz_hi = float(grid.z.primary[kc])
        xp, yp = grid.x.primary, grid.y.primary
        xd, yd = grid.x.dual, grid.y.dual
        gn = 0.5 * (xd[:, None] + yd[None, :])
        gp = 0.5 * (xp[:, None] + yp[None, :])
        k = cls(
            ez=(kappa_normal(gn, dz_lo), kappa_normal(gn, dz_hi)),
            ex=(
                kappa_inplane(xp[:, None], dz_lo, dz_hi, er, 1.0),
                kappa_slot(xp[:, None], dz_lo, dz_hi, er, 1.0),
            ),
            ey=(
                kappa_inplane(yp[None, :], dz_lo, dz_hi, er, 1.0),
                kappa_slot(yp[None, :], dz_lo, dz_hi, er, 1.0),
            ),
            hz=(kappa_inplane(gp, dz_lo, dz_hi), kappa_slot(gp, dz_lo, dz_hi)),
            hx=(kappa_normal(xd[:, None], dz_lo), kappa_normal(xd[:, None], dz_hi)),
            hy=(kappa_normal(yd[None, :], dz_lo), kappa_normal(yd[None, :], dz_hi)),
        )
        if corners:
            k.ez_corner = tuple(
                tuple(kn * r for r in corner_ratios(gn, dz)) for kn, dz in zip(k.ez, (dz_lo, dz_hi))
            )
        if second_ring:

            def ring(gg):
                return tuple(
                    (kappa_second(gg, dz, +1), kappa_second(gg, dz, -1)) for dz in (dz_lo, dz_hi)
                )

            k.ez2 = ring(gn)
            k.hx2 = ring(xd[:, None])
            k.hy2 = ring(yd[None, :])
        return k

    def worst(self) -> dict:
        """The extreme factors over every configuration, for the time step bound: the smallest
        ε factor of each E component (κ_n on E_z, min(κ_t, κ_slot) in the plane; the next ring
        raises ε) and the largest 1/μ factor of each H component (the next ring's, above 1, on
        H_x and H_y; H_z's are at most 1)."""
        out = {
            "ez": (self.ez[0], self.ez[1]),
            "ex": (np.minimum(*self.ex),),
            "ey": (np.minimum(*self.ey),),
            "hz": (1.0,),
        }
        for name, k2 in (("hx", self.hx2), ("hy", self.hy2)):
            if k2 is None:
                out[name] = (1.0, 1.0)
            else:
                out[name] = tuple(np.maximum(1.0, np.maximum(kv, km)) for kv, km in k2)
        return out


def _lib(c):
    if isinstance(c, np.ndarray):
        return np
    import torch

    return torch


_PAD = 2


def _pad(c):
    """Pad the last two axes of `c` with two void pixels on every side."""
    if isinstance(c, np.ndarray):
        width = [(0, 0)] * (c.ndim - 2) + [(_PAD, _PAD), (_PAD, _PAD)]
        return np.pad(c, width)
    import torch

    return torch.nn.functional.pad(c, (_PAD,) * 4)


def _const(a, like):
    if isinstance(like, np.ndarray):
        return np.asarray(a)
    import torch

    return torch.as_tensor(np.asarray(a), dtype=like.dtype)


def factor_maps(c, k: EdgeConstants) -> dict:
    """The correction factors from the pixel copper fraction `c` (..., Nx, Ny), numpy or torch.

    Returns {comp: (..., n1, n2, planes)}: ε multipliers of "ex", "ey" (copper plane) and "ez"
    (below, above), 1/μ multipliers of "hx", "hy" (below, above) and "hz" (copper plane)."""
    lib = _lib(c)
    cp = _pad(c)
    nx, ny = c.shape[-2], c.shape[-1]

    def px(shape, di, dj):
        """Pixel (i + di, j + dj) for every element (i, j) of a target of `shape` (ox, oy
        more than the pixels)."""
        ox, oy = shape
        i0, j0 = _PAD + di, _PAD + dj
        return cp[..., i0 : i0 + nx + ox, j0 : j0 + ny + oy]

    def stack(*maps):
        return lib.stack(maps, -1)

    def both(p, q):
        return p * q

    def none(p, q):
        return (1.0 - p) * (1.0 - q)

    # E_z on the nodes: the four pixels around each node.
    sn = (1, 1)
    a, b, cc, d = px(sn, -1, -1), px(sn, 0, -1), px(sn, -1, 0), px(sn, 0, 0)
    all_c = a * b * cc * d
    all_v = (1.0 - a) * (1.0 - b) * (1.0 - cc) * (1.0 - d)
    mixed = 1.0 - all_c - all_v
    ez = [1.0 + (_const(kn, c) - 1.0) * mixed for kn in k.ez]
    if k.ez_corner is not None:
        # Convex (one copper pixel of four) and concave (three) corner nodes.
        one = a * (1 - b) * (1 - cc) * (1 - d) + (1 - a) * b * (1 - cc) * (1 - d)
        one = one + (1 - a) * (1 - b) * cc * (1 - d) + (1 - a) * (1 - b) * (1 - cc) * d
        three = (1 - a) * b * cc * d + a * (1 - b) * cc * d + a * b * (1 - cc) * d
        three = three + a * b * cc * (1 - d)
        ez = [
            e + (_const(kx, c) - _const(kn, c)) * one + (_const(kv, c) - _const(kn, c)) * three
            for e, kn, (kx, kv) in zip(ez, k.ez, k.ez_corner)
        ]
    if k.ez2 is not None:
        pairs = (
            (px(sn, -1, -2), px(sn, 0, -2)),
            (px(sn, -1, 1), px(sn, 0, 1)),
            (px(sn, -2, -1), px(sn, -2, 0)),
            (px(sn, 1, -1), px(sn, 1, 0)),
        )
        near_c, near_v = 1.0, 1.0
        for p, q in pairs:
            near_c = near_c * (1.0 - both(p, q))
            near_v = near_v * (1.0 - none(p, q))
        f_v = all_v * (1.0 - near_c)  # void node, a straight copper edge one cell away
        f_m = all_c * (1.0 - near_v)  # copper node, a straight copper edge one cell away
        ez = [
            e + (_const(kv, c) - 1.0) * f_v + (_const(km, c) - 1.0) * f_m
            for e, (kv, km) in zip(ez, k.ez2)
        ]
    ez = stack(*ez)

    def inplane(shape, own0, own1, e00, e01, e10, e11, kt, ks):
        v = (1.0 - px(shape, *own0)) * (1.0 - px(shape, *own1))
        e0 = 1.0 - none(px(shape, *e00), px(shape, *e01))
        e1 = 1.0 - none(px(shape, *e10), px(shape, *e11))
        kt, ks = _const(kt, c), _const(ks, c)
        return 1.0 + v * ((kt - 1.0) * (e0 + e1) + (ks - 2.0 * kt + 1.0) * e0 * e1)

    ex = inplane((0, 1), (0, -1), (0, 0), (-1, -1), (-1, 0), (1, -1), (1, 0), *k.ex)
    ey = inplane((1, 0), (-1, 0), (0, 0), (-1, -1), (0, -1), (-1, 1), (0, 1), *k.ey)
    # H_z on the pixels.
    sp = (0, 0)
    self_ = px(sp, 0, 0)
    w, e, s, n = px(sp, -1, 0), px(sp, 1, 0), px(sp, 0, -1), px(sp, 0, 1)
    kt, ks = _const(k.hz[0], c), _const(k.hz[1], c)
    any_side = 1.0 - (1.0 - w) * (1.0 - e) * (1.0 - s) * (1.0 - n)
    pair = 1.0 - (1.0 - w * e) * (1.0 - s * n)
    hz = 1.0 + (1.0 - self_) * ((kt - 1.0) * any_side + (ks - kt) * pair)

    def tangential(shape, own0, own1, out0, out1, kn, k2):
        p, q = px(shape, *own0), px(shape, *own1)
        x = p + q - 2.0 * p * q
        maps = [1.0 + (_const(kk, c) - 1.0) * x for kk in kn]
        if k2 is not None:
            lo, hi = px(shape, *out0), px(shape, *out1)
            f_v = none(p, q) * (1.0 - (1.0 - lo) * (1.0 - hi))
            f_m = both(p, q) * (1.0 - lo * hi)
            maps = [
                mp + (_const(kv, c) - 1.0) * f_v + (_const(km, c) - 1.0) * f_m
                for mp, (kv, km) in zip(maps, k2)
            ]
        return stack(*maps)

    return {
        "ex": stack(ex),
        "ey": stack(ey),
        "ez": ez,
        "hx": tangential((1, 0), (-1, 0), (0, 0), (-2, 0), (1, 0), k.hx, k.hx2),
        "hy": tangential((0, 1), (0, -1), (0, 0), (0, -2), (0, 1), k.hy, k.hy2),
        "hz": stack(hz),
    }


def vjp(c: np.ndarray, k: EdgeConstants, kernels: dict) -> np.ndarray:
    """Σ_comp ⟨kernels[comp], ∂ factor_maps(c)[comp]/∂c⟩ per leading row: `kernels[comp]` has
    shape (M, n1, n2, planes) (missing components count as zero); returns (M, Nx, Ny)."""
    import torch

    m = next(iter(kernels.values())).shape[0]
    ct = torch.tensor(np.broadcast_to(c, (m,) + c.shape).copy(), dtype=torch.float64)
    ct.requires_grad_(True)
    maps = factor_maps(ct, k)
    outs, grads = [], []
    for comp, ker in kernels.items():
        outs.append(maps[comp])
        grads.append(torch.as_tensor(ker, dtype=torch.float64))
    (g,) = torch.autograd.grad(outs, [ct], grads)
    return g.numpy()


def planes(grid: Grid, comp: str) -> tuple[int, ...]:
    """Absolute z indices of the corrected planes of `comp`."""
    table = E_PLANES if comp[0] == "e" else H_PLANES
    return tuple(grid.k_c + d for d in table[comp])


_DT_CACHE: dict = {}
# The margin on the largest eigenvalue found over the dense test patterns (`stable_dt`).
DT_MARGIN = 1.05


def _patterns(n: int) -> list:
    """Dense copper patterns for the time-step bound: one- to three-pixel stripes both ways,
    checkerboards, isolated dots and holes, random binary (two densities) and random gray, and
    the diagonal families (stripes two and three pixels wide, one-pixel lines touching at
    corners every three and four pixels, a knight's-move lattice, random diagonal stripes).

    The diagonal families raise λ most: on the cases' grids one-pixel diagonal lines every three
    pixels reach 1.66 (0.3 mm, 4 substrate cells) to 1.74 (0.1 mm, 8 cells) times the plain
    grid's λ, two-pixel diagonal stripes 1.62–1.69, against 1.58–1.64 for the earlier library
    (random binary); a random search of single and diagonal flips from the worst of them found
    nothing larger (design §24)."""
    i, j = np.meshgrid(np.arange(n), np.arange(n), indexing="ij")
    rng = np.random.default_rng(0)
    out = []
    for w in (1, 2, 3):
        out += [((i // w) % 2).astype(float), ((j // w) % 2).astype(float)]
    for w in (1, 2):
        out.append((((i // w) + (j // w)) % 2).astype(float))
    dots = ((i % 3 == 1) & (j % 3 == 1)).astype(float)
    out += [dots, 1.0 - dots]
    out += [(rng.uniform(size=(n, n)) > q).astype(float) for q in (0.5, 0.3)]
    out.append(rng.uniform(size=(n, n)))
    for w in (2, 3):
        out.append((((i + j) // w) % 2).astype(float))
    for k in (3, 4):
        out.append(((i + j) % k == 0).astype(float))
    out.append(((i + 2 * j) % 5 == 0).astype(float))
    out.append((rng.uniform(size=2 * n) < 0.5)[i + j].astype(float))
    return out


def stable_dt(grid: Grid, er: float, courant: float = 0.95) -> float:
    """The time step with the copper-edge correction: courant · min(Δt_CFL, 2/√(1.01 λ)), λ
    the largest eigenvalue of ε⁻¹ C_H μ⁻¹ C_E (`fdtd.stability`) on a proxy of the grid's
    densest region (20 × 20 cells at its smallest in-plane pitch, its own z axis, no CPML),
    taken as `DT_MARGIN` times the largest over dense copper patterns (`_patterns`).

    The correction lowers ε next to edges and raises 1/μ one cell away, so λ depends on the
    pattern: on the cases' grids the worst patterns of the library (one-pixel diagonal lines
    touching at corners) raise it to 1.66–1.74 times the plain grid's. A bound that takes
    every factor at its extreme at once (no pattern can) would cost more steps; this one is
    not a proof, so `Simulation.run` stops a diverging run with an error."""
    from yapnr.rf.fdtd.stability import max_eigenvalue
    from yapnr.rf.materials import Structure
    from yapnr.rf.mesh import Axis, PMLCells, uniform_nodes
    from yapnr.rf.stackup import Stackup

    p = float(min(grid.x.primary.min(), grid.y.primary.min()))
    key = (round(p, 15), tuple(np.round(grid.z.nodes, 15)), grid.k_c, float(er))
    if key not in _DT_CACHE:
        n = 20
        ax = Axis(uniform_nodes(0.0, n * p, p))
        proxy = Grid(ax, ax, grid.z, grid.k_c, PMLCells(0, 0, 0, 0, 0))
        stack = Stackup(er, 0.0, float(grid.z.nodes[grid.k_c]), 1e10)
        lam = 0.0
        for c in [None] + _patterns(n):
            st = Structure(proxy, stack)
            if c is not None:
                st.set_edge_correction(c)
            lam = max(lam, max_eigenvalue(proxy, st, tol=1e-6, max_iter=2000)[0])
        _DT_CACHE[key] = 2.0 / math.sqrt(1.01 * DT_MARGIN * lam)
    return courant * min(_DT_CACHE[key], grid.courant_dt())


def design_probes(grid: Grid, window, prefix: str = "edges") -> list:
    """Probes on every corrected edge whose factor depends on a pixel of the design window
    (i0, i1, j0, j1): the factors look two pixels around, so two cells beyond the window on
    every side, every corrected plane."""
    from yapnr.rf.fdtd.monitors import Probe, box_indices

    i0, i1, j0, j1 = window
    out = []
    for comp in ("ex", "ey", "ez", "hx", "hy", "hz"):
        shape = grid.shape(comp)
        ks = planes(grid, comp)
        ranges = [
            (max(0, i0 - 2), min(shape[0], i1 + 3)),
            (max(0, j0 - 2), min(shape[1], j1 + 3)),
            (ks[0], ks[-1] + 1),
        ]
        out.append(Probe(f"{prefix}_{comp}", comp, box_indices(grid, comp, ranges)))
    return out


def kernels(structure, probes, forward: dict, adjoint: dict, omega, dt: float) -> dict:
    """∂F_m/∂(factor) on every probed edge, as arrays shaped like `factor_maps` with a leading
    frequency axis (zero off the probes).

    With the adjoint field Ê^adj of the run driven by the Wirtinger gradients (`adjoint`),
    the volume-weighted operator A = C_H μ⁻¹ C_E − Ω² ε − iΩ c σ gives (design §6.2)

        ∂F/∂ε_e  = 2 Re[Ω² V_e Ê^adj_e Ê_e]                (∂A/∂ε_e = −Ω²)
        ∂F/∂μ⁻¹_h = 2 Re[Ω² μ_h² V_h Ĥ^adj_h Ĥ_h]          (C_E Ê = iΩ μ Ĥ off the sources)

    and the factors f (ε = ε_base f, 1/μ = f/μ0) chain as ε_base and μ0/f²."""
    from yapnr.rf.constants import MU0
    from yapnr.rf.fdtd.dtft import numerical_omega

    grid = structure.grid
    big = numerical_omega(omega, dt)[:, None]
    maps = structure.edge_maps
    out = {}
    for p in probes:
        comp = p.comp
        ks = planes(grid, comp)
        vol = grid.volume(comp).reshape(-1)[p.index][None, :]
        fw = np.asarray(forward[p.name])
        ad = np.asarray(adjoint[p.name])
        prod = (ad * fw).real
        i, j, k = grid.unravel(comp, p.index)
        n = k - ks[0]
        if comp[0] == "e":
            base = structure.eps_base(comp).reshape(-1)[p.index][None, :]
            ker = 2.0 * big**2 * vol * prod * base
        else:
            f = maps[comp][i, j, n][None, :]
            ker = 2.0 * big**2 * MU0 * vol * prod / (f * f)
        arr = np.zeros((omega.size,) + maps[comp].shape)
        arr[:, i, j, n] = ker
        out[comp] = arr
    return out
