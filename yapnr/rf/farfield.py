"""Near-to-far-field transform on a closed Huygens box (design §26.2).

The surface equivalence principle replaces the sources inside a closed surface S by the
currents J = n̂ × H and M = −n̂ × E on it; outside, they radiate the same field. With the
internal e^{−iωt} convention (outgoing e^{ikr}/r), k = ω/c0 and r̂ = (u, v, w):

    N(r̂) = Σ_s A_s c_J,s(r̂) J_s e^{−ik r̂·r_s},     L(r̂) = Σ_s A_s M_s e^{−ik r̂·r_s}
    F = η0 N_⊥ − r̂ × L,   i.e.  F_θ = η0 N_θ + L_φ,   F_φ = η0 N_φ − L_θ
    E_far = ik e^{ikr}/(4πr) F,     U(r̂) = k²/(32π² η0) |F|²   (W/sr)

**Samples.** `fdtd.monitors.FluxBox` already pairs collocated samples on every face: on a
face normal to axis n with outward sign s and tangential axes (t1, t2) = (n+1, n+2) mod 3, the
tangential E on the face's node plane and H̄, the mean of the two half-cell H planes either
side, at the same Yee edge centre, with the flux's area weights (rims halved). So

    pair (E_t1, H̄_t2):  J_t1 = −s H̄_t2,  M_t2 = −s E_t1
    pair (E_t2, H̄_t1):  J_t2 = +s H̄_t1,  M_t1 = +s E_t2

and ½ Re Σ (E × H̄*)·n̂ A over the same samples is the box's flux, P_box. No probes are added.

**Averaging correction.** H̄ averages H over the normal offsets −Δ⁻/2 and +Δ⁺/2 (the cells
below and above the face's node). For the plane-wave component the far field in direction r̂
selects (normal wavenumber k_n = k r̂·n̂), that is H(0)·(e^{−ik_nΔ⁻/2} + e^{ik_nΔ⁺/2})/2, which
c_J = 2/(e^{−ik_nΔ⁻/2} + e^{ik_nΔ⁺/2}) undoes (uncorrected: a cos(k_nΔ/2) error, 2.2 % in
amplitude at λ/15 cells). What is left is the grid's dispersion, O((kΔ)²) in phase.

**Images.** With an infinite PEC ground at z = 0 (`image=True`) the box has no bottom face and
the currents radiate with their images (x, y, −z): J → (−J_x, −J_y, +J_z), M → (+M_x, +M_y,
−M_z). The factor e^{−ikwz} then becomes −2i sin(kwz) for J_x, J_y, M_z and 2 cos(kwz) for J_z,
M_x, M_y; the far field exists in the upper half space (w ≥ 0) only.

**Cost.** Each face pair's samples form a tensor grid over its two tangential axes, so the sum
over a face factors into two matrix products per frequency (torch complex128, differentiable:
`adjoint.wirtinger` turns the transform's Wirtinger gradient into surface sources on the box's
probes).

`Frame` sets the polar axis and φ = 0 of the reported angles; `sphere_quadrature` integrates
U over the sphere (Gauss–Legendre in cos θ, uniform in φ), which for sources inside a sphere of
radius a is exact to round-off once n_θ exceeds ka (`quadrature_order`).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from yapnr.rf.constants import C0, ETA0

AXES = {"+x": (1, 0, 0), "-x": (-1, 0, 0), "+y": (0, 1, 0), "-y": (0, -1, 0)}
AXES.update({"+z": (0, 0, 1), "-z": (0, 0, -1)})


def axis_vector(name) -> np.ndarray:
    """A unit vector from "+x" … "-z" or an (x, y, z) triple."""
    if isinstance(name, str):
        if name not in AXES:
            raise ValueError(f"unknown axis {name!r}: one of {sorted(AXES)}")
        return np.array(AXES[name], dtype=np.float64)
    v = np.asarray(name, dtype=np.float64)
    n = np.linalg.norm(v)
    if v.shape != (3,) or not n > 0:
        raise ValueError(f"not a direction: {name!r}")
    return v / n


@dataclass(frozen=True)
class Frame:
    """The angles' frame: θ from `axis` (the polar axis), φ from `zero` towards axis × zero
    (right-handed). Default: θ from +z, φ from +x (the usual spherical angles)."""

    axis: str | tuple = "+z"
    zero: str | tuple = "+x"

    def __post_init__(self) -> None:
        z, x = axis_vector(self.axis), axis_vector(self.zero)
        if abs(float(z @ x)) > 1e-9:
            raise ValueError(f"frame: zero {self.zero!r} must be normal to axis {self.axis!r}")

    @property
    def basis(self) -> np.ndarray:
        """Rows x', y', z' (global coordinates)."""
        z, x = axis_vector(self.axis), axis_vector(self.zero)
        return np.array([x, np.cross(z, x), z])

    def directions(self, theta, phi) -> np.ndarray:
        """Unit vectors (…, 3), global, of the frame's (θ, φ) in radians."""
        theta, phi = np.broadcast_arrays(np.asarray(theta, float), np.asarray(phi, float))
        st = np.sin(theta)
        local = np.stack([st * np.cos(phi), st * np.sin(phi), np.cos(theta)], axis=-1)
        return local @ self.basis

    def unit_vectors(self, theta, phi) -> tuple[np.ndarray, np.ndarray]:
        """(θ̂, φ̂) (…, 3), global, at the frame's (θ, φ)."""
        theta, phi = np.broadcast_arrays(np.asarray(theta, float), np.asarray(phi, float))
        ct, st, cp, sp = np.cos(theta), np.sin(theta), np.cos(phi), np.sin(phi)
        th = np.stack([ct * cp, ct * sp, -st], axis=-1) @ self.basis
        ph = np.stack([-sp, cp, np.zeros_like(cp)], axis=-1) @ self.basis
        return th, ph

    def angles(self, dirs) -> tuple[np.ndarray, np.ndarray]:
        """(θ, φ) in radians of global unit vectors (…, 3)."""
        local = np.asarray(dirs, float) @ self.basis.T
        theta = np.arccos(np.clip(local[..., 2], -1.0, 1.0))
        phi = np.arctan2(local[..., 1], local[..., 0])
        return theta, np.mod(phi, 2.0 * np.pi)


def quadrature_order(k_max: float, radius: float) -> int:
    """n_θ = ⌈k_max a⌉ + 10 Gauss–Legendre nodes (and 2 n_θ in φ): the far field of sources in
    a sphere of radius a is band-limited to degree about ka, so U (degree about 2ka) is
    integrated to round-off (design §26.2)."""
    return int(math.ceil(k_max * radius)) + 10


def sphere_quadrature(n_theta: int, *, upper: bool = False, frame: Frame = Frame()):
    """(directions (D, 3), weights (D,), θ (D,), φ (D,)): Gauss–Legendre in cos θ (over [−1, 1],
    or [0, 1] for the upper hemisphere of `frame`) times 2 n_θ uniform φ; the weights sum to 4π
    (2π)."""
    x, w = np.polynomial.legendre.leggauss(int(n_theta))
    if upper:
        x, w = 0.5 * (x + 1.0), 0.5 * w
    n_phi = 2 * int(n_theta)
    phi = 2.0 * np.pi * np.arange(n_phi) / n_phi
    theta = np.arccos(x)
    tt, pp = np.meshgrid(theta, phi, indexing="ij")
    ww = np.repeat(w, n_phi) * (2.0 * np.pi / n_phi)
    return frame.directions(tt.ravel(), pp.ravel()), ww, tt.ravel(), pp.ravel()


@dataclass
class _Channel:
    """One current component of a face pair: J or M along `comp` (0, 1, 2), the sign that
    makes it from the pair's E or H̄, and whether it comes from H̄ (J, takes c_J)."""

    comp: int
    magnetic: bool
    sign: float


@dataclass
class _Pair:
    e_name: str
    h_names: tuple
    shape: tuple  # (A, B) samples along the face's two tangential axes in array order
    axes: tuple  # (a, b) the tangential axes in array order
    coords: tuple  # (x_a (A,), x_b (B,))
    normal: int
    node_coord: float
    weights: np.ndarray  # (A, B)
    d_lo: float
    d_hi: float
    channels: list


class FarField:
    """The transform of a closed `FluxBox`'s samples (see the module doc). `image` mirrors the
    currents in an infinite PEC ground at z = 0 (the box then has no z− face)."""

    def __init__(self, box, *, image: bool = False):
        grid = box.grid
        self.box = box
        self.image = bool(image)
        tags = {(f.axis, f.sign) for f in box.faces}
        if self.image:
            if (2, -1) in tags:
                raise ValueError("with an image ground the box has no z− face")
            if box.node_box[2][0] != 0 or abs(float(grid.z.nodes[0])) > 1e-12:
                raise ValueError("with an image ground the box must stand on z = 0")
        need = {(a, s) for a in range(3) for s in (-1, 1)} - ({(2, -1)} if self.image else set())
        if not need <= tags:
            raise ValueError("the far-field transform needs a closed box")
        self.pairs: list[_Pair] = []
        for face in box.faces:
            n, s = face.axis, face.sign
            t1, t2 = (n + 1) % 3, (n + 2) % 3
            ax_n = grid.axis(n)
            for pair, w in enumerate(face.weights):
                ep = face.probes[3 * pair]
                hl, hh = face.probes[3 * pair + 1], face.probes[3 * pair + 2]
                ii = grid.unravel(ep.comp, ep.index)
                a_ax, b_ax = sorted([t1, t2])
                ua = np.unique(ii[a_ax])
                ub = np.unique(ii[b_ax])
                shape = (ua.size, ub.size)
                if ua.size * ub.size != ep.index.size:
                    raise ValueError("face samples are not a tensor grid")
                pos = [grid.positions(ep.comp, a) for a in range(3)]
                # J_t1 = −s H̄_t2, M_t2 = −s E_t1 (pair 0); J_t2 = +s H̄_t1, M_t1 = +s E_t2.
                if pair == 0:
                    chans = [_Channel(t1, False, -s), _Channel(t2, True, -s)]
                else:
                    chans = [_Channel(t2, False, +s), _Channel(t1, True, +s)]
                self.pairs.append(
                    _Pair(
                        e_name=ep.name,
                        h_names=(hl.name, hh.name),
                        shape=shape,
                        axes=(a_ax, b_ax),
                        coords=(pos[a_ax][ua], pos[b_ax][ub]),
                        normal=n,
                        node_coord=float(ax_n.nodes[face.node]),
                        weights=np.asarray(w, dtype=np.float64).reshape(shape),
                        d_lo=float(ax_n.primary[face.node - 1]),
                        d_hi=float(ax_n.primary[face.node]),
                        channels=chans,
                    )
                )
        lo = np.array([grid.axis(a).nodes[box.node_box[a][0]] for a in range(3)])
        hi = np.array([grid.axis(a).nodes[box.node_box[a][1]] for a in range(3)])
        if self.image:
            lo[2] = -hi[2]
        self.centre = 0.5 * (lo + hi)
        self.radius = float(0.5 * np.linalg.norm(hi - lo))
        self._phases: dict = {}

    @property
    def probe_names(self) -> list[str]:
        out = []
        for p in self.pairs:
            out += [p.e_name, *p.h_names]
        return out

    def quadrature(self, omega, *, frame: Frame = Frame()):
        """`sphere_quadrature` of order `quadrature_order(k_max, radius)` (the upper
        hemisphere about +z with an image ground)."""
        k_max = float(np.max(omega)) / C0
        n = quadrature_order(k_max, self.radius)
        if self.image:
            frame = Frame("+z", "+x")
        return sphere_quadrature(n, upper=self.image, frame=frame)

    def _factors(self, omega, dirs):
        """Per pair: the phase matrices along its two tangential axes and its normal, per
        channel (the image's sin/cos factors on the z coordinate), and c_J; numpy complex."""
        key = (np.asarray(omega).tobytes(), np.asarray(dirs).tobytes())
        if key in self._phases:
            return self._phases[key]
        k = np.asarray(omega, dtype=np.float64)[:, None] / C0  # (M, 1)
        dirs = np.asarray(dirs, dtype=np.float64)

        def phase(axis, x, kind):
            """(M, D, P) e^{−ik u_axis x} ("plain"), 2cos ("even") or −2i sin ("odd")."""
            arg = (k * dirs[None, :, axis])[..., None] * np.asarray(x, float)[None, None, :]
            if kind == "plain":
                return np.exp(-1j * arg)
            if kind == "even":
                return (2.0 * np.cos(arg)).astype(np.complex128)
            return -2j * np.sin(arg)

        out = []
        for p in self.pairs:
            kn = k * dirs[None, :, p.normal]  # (M, D)

            def c_j(kn, p=p):
                return 2.0 / (np.exp(-0.5j * kn * p.d_lo) + np.exp(0.5j * kn * p.d_hi))

            per = []
            for ch in p.channels:
                kinds = ["plain", "plain", "plain"]
                odd = (not ch.magnetic and ch.comp in (0, 1)) or (ch.magnetic and ch.comp == 2)
                if self.image:
                    kinds[2] = "odd" if odd else "even"
                fa = phase(p.axes[0], p.coords[0], kinds[p.axes[0]])
                fb = phase(p.axes[1], p.coords[1], kinds[p.axes[1]])
                one = np.ones_like(kn)
                if self.image and p.normal == 2:
                    # The image's normal offsets are mirrored: its averaging factor is c_J(−k_n).
                    z = p.node_coord
                    c_plus, c_minus = (one, one) if ch.magnetic else (c_j(kn), c_j(-kn))
                    fn = (
                        np.exp(-1j * kn * z) * c_plus
                        + (-1.0 if odd else 1.0) * np.exp(1j * kn * z) * c_minus
                    )
                else:
                    fn = phase(p.normal, [p.node_coord], "plain")[..., 0]
                    if not ch.magnetic:
                        fn = fn * c_j(kn)
                per.append((fa, fb, fn))
            out.append(per)
        self._phases[key] = out
        return out

    def potentials(self, dft, omega, dirs):
        """(N, L) (M, D, 3) torch complex128 from probe DTFTs (torch tensors (M, P))."""
        import torch

        omega = np.asarray(omega, dtype=np.float64)
        dirs = np.asarray(dirs, dtype=np.float64)
        factors = self._factors(omega, dirs)
        m, d = omega.size, dirs.shape[0]
        n_acc = [torch.zeros((m, d), dtype=torch.complex128) for _ in range(3)]
        l_acc = [torch.zeros((m, d), dtype=torch.complex128) for _ in range(3)]
        for p, per in zip(self.pairs, factors):
            e = torch.as_tensor(dft[p.e_name])
            h = (torch.as_tensor(dft[p.h_names[0]]) + torch.as_tensor(dft[p.h_names[1]])) / 2
            w = torch.as_tensor(p.weights.reshape(-1), dtype=torch.float64)
            for ch, (fa, fb, fn) in zip(p.channels, per):
                v = (e if ch.magnetic else h) * (ch.sign * w)  # (M, A·B)
                v = v.reshape((m,) + p.shape)
                fa_t, fb_t, fn_t = (torch.as_tensor(f) for f in (fa, fb, fn))
                t = torch.einsum("mab,mdb->mda", v, fb_t)
                s = torch.einsum("mda,mda->md", t, fa_t) * fn_t
                (l_acc if ch.magnetic else n_acc)[ch.comp] = (l_acc if ch.magnetic else n_acc)[
                    ch.comp
                ] + s
        return torch.stack(n_acc, dim=-1), torch.stack(l_acc, dim=-1)

    def vectors(self, dft, omega, dirs):
        """F = η0 N_⊥ − r̂ × L (M, D, 3) (torch complex128): E_far = ik e^{ikr}/(4πr) F."""
        import torch

        n, ell = self.potentials(dft, omega, dirs)
        r = torch.as_tensor(np.asarray(dirs, dtype=np.float64), dtype=torch.complex128)[None]
        n_perp = n - (n * r).sum(-1, keepdim=True) * r
        return ETA0 * n_perp - torch.linalg.cross(r.expand_as(ell), ell, dim=-1)


def intensity_factor(omega) -> np.ndarray:
    """k²/(32π² η0) per frequency: U = factor · |F|²."""
    k = np.asarray(omega, dtype=np.float64) / C0
    return k * k / (32.0 * math.pi**2 * ETA0)


def components(f, dirs, frame: Frame, pol: str, reference_phi: float = 0.0):
    """|F·ê_p*|² (M, D) of far-field vectors F (M, D, 3) (torch) for the polarization `pol`:
    "total", "theta", "phi", "co" and "cross" (Ludwig-3 about `reference_phi`, radians, in
    `frame`), "rhcp", "lhcp" (IEEE; internally ê_R,L = (θ̂ ± iφ̂)/√2 for e^{−iωt})."""
    import torch

    if pol == "total":
        return (f.real**2 + f.imag**2).sum(-1)
    theta, phi = frame.angles(dirs)
    th, ph = frame.unit_vectors(theta, phi)
    f_t = (f * torch.as_tensor(th, dtype=torch.complex128)[None]).sum(-1)
    f_p = (f * torch.as_tensor(ph, dtype=torch.complex128)[None]).sum(-1)
    if pol == "theta":
        g = f_t
    elif pol == "phi":
        g = f_p
    elif pol in ("co", "cross"):
        c = torch.as_tensor(np.cos(phi - reference_phi), dtype=torch.complex128)[None]
        s = torch.as_tensor(np.sin(phi - reference_phi), dtype=torch.complex128)[None]
        g = c * f_t - s * f_p if pol == "co" else s * f_t + c * f_p
    elif pol == "rhcp":
        g = (f_t - 1j * f_p) / math.sqrt(2.0)
    elif pol == "lhcp":
        g = (f_t + 1j * f_p) / math.sqrt(2.0)
    else:
        raise ValueError(f"unknown polarization {pol!r}")
    return g.real**2 + g.imag**2
