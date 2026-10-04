"""The discrete quasi-TEM mode of a feed line and the port source shaped like it.

A line port launches its feed's mode with a sheet of electric current J on the transverse E
edges of the source plane. J = n̂ × H_0 (H_0 the transverse magnetic field of the mode)
excites that mode only: the excitation of any other mode n is the cross-power
∫ (E_n × H_0)·n̂ dA, which vanishes by mode orthogonality. The earlier source took H_0 from
the static field of the strip in air (the quasi-static limit, `ports.quasi_tem_air`); at
8–12 GHz that profile is off by the line's dispersion and the source also launched the
substrate's TM0 surface wave, which reached the V/I samples and made the excited port's
incident wave read up to 2 % high.

`line_mode` solves for the mode of the scheme itself: the Yee discretization of the
cross-section (t, z) at frequency ω with fields ∝ e^{iKa} along the line (∂a → iK̃,
K̃ = (2/Δa) sin(KΔa/2)), the numerical frequency Ω and the Crank–Nicolson conductances c_ω σ
(design §4.6), the CPML as the exact stretch of its recursion (`cpml.stretch`), the copper
sheet's conductance and, when on, the copper-edge correction (`edges`). Eliminating H gives,
for X = (iK̃ E_a, E_t, E_z), a generalized eigenproblem A0 X = K̃² B X that is block
tridiagonal along t; inverse iteration with a block LU (no sparse solver needed), started
from the closed-form estimate of K, converges in a few steps to the line mode (the slowest
guided mode: every substrate or box mode has a smaller K).

The mode's profile changes with frequency (a dispersive line), so `ModeProfile` fits the
source profile per unit modal current as P(ω) = P0 + ω² P2 from solves at the edges of the
source's band and realizes it in the time domain as J(t) = P0 s(t) − P2 s''(t) for the
pulse s (ω² S(ω) is the transform of −s''). Other strips crossing the source plane are left
out of the solve (the source launches the isolated line's mode, as the static source did).

Orientation: the solve is written for a line along x (t = y). For a line along y (t = x) the
frame (y, x, z) is left-handed, which flips the sign of every curl; that is H → −H, and the
source J = n̂ × H takes the same form, J_t = −H_z and J_z = H_t, in both cases.
"""

from __future__ import annotations

import hashlib
import math
from collections import OrderedDict
from dataclasses import dataclass

import numpy as np

from yapnr.rf.constants import C0, EPS0, MU0
from yapnr.rf.fdtd.cpml import CPMLParams, profile, stretch
from yapnr.rf.fdtd.dtft import conductance_factor, numerical_omega
from yapnr.rf.mesh import Axis, Grid, PMLCells


@dataclass
class CrossSection:
    """Materials of a uniform strip's cross-section on the grid's (t, z) axes: permittivity
    and conductivity of E_a (nodes, (Nt+1, Nz+1)), E_t ((Nt, Nz+1)) and E_z ((Nt+1, Nz)),
    and 1/μr of H_a ((Nt, Nz)), H_t ((Nt+1, Nz)) and H_z ((Nt, Nz+1))."""

    grid: Grid
    axis: int
    ta: int
    tb: int
    pitch_a: float
    eps: dict
    sig: dict
    mu: dict


def cross_section(
    grid: Grid, stackup, axis: int, ta: int, tb: int, *, edge_correction: bool = False
) -> CrossSection:
    """The cross-section of a strip on transverse nodes ta..tb running along `axis` (0: x,
    1: y), alone on the copper plane: the substrate, the sheet conductance averaged onto the
    edges as in 3D, and the edge factors of a uniform strip when `edge_correction`."""
    from yapnr.rf.edges import EdgeConstants, factor_maps
    from yapnr.rf.materials import _pair_weights

    tx, zx = grid.axis(1 - axis), grid.z
    nt, nz = tx.n, zx.n
    kc = grid.k_c
    eps_sub, sig_sub = EPS0 * stackup.er, stackup.sigma_sub
    dzp = zx.primary
    w_sub = dzp[kc - 1] / (dzp[kc - 1] + dzp[kc])
    t_eps, t_sig = np.full(nz + 1, EPS0), np.zeros(nz + 1)
    t_eps[:kc], t_sig[:kc] = eps_sub, sig_sub
    t_eps[kc] = w_sub * eps_sub + (1 - w_sub) * EPS0
    t_sig[kc] = w_sub * sig_sub
    z_eps, z_sig = np.full(nz, EPS0), np.zeros(nz)
    z_eps[:kc], z_sig[:kc] = eps_sub, sig_sub
    shapes = {"a": (nt + 1, nz + 1), "t": (nt, nz + 1), "z": (nt + 1, nz)}
    eps = {c: np.broadcast_to(z_eps if c == "z" else t_eps, s).copy() for c, s in shapes.items()}
    sig = {c: np.broadcast_to(z_sig if c == "z" else t_sig, s).copy() for c, s in shapes.items()}
    cop = np.zeros(nt)
    cop[ta:tb] = 1.0
    lo, hi = _pair_weights(tx.primary, tx.dual)
    ga = np.zeros(nt + 1)
    ga[1:] += lo[1:] * cop
    ga[:-1] += hi[:-1] * cop
    dz_d = float(zx.dual[kc])
    sig["a"][:, kc] += stackup.g_max * ga / dz_d
    sig["t"][:, kc] += stackup.g_max * cop / dz_d
    mu = {"a": np.ones((nt, nz)), "t": np.ones((nt + 1, nz)), "z": np.ones((nt, nz + 1))}
    pitch_a = float(grid.axis(axis).primary.min())
    if edge_correction:
        # A plane five pixels long along the strip; its middle row is the uniform strip (the
        # factors look two pixels around).
        along = Axis(np.arange(6) * pitch_a)
        pml = PMLCells(0, 0, 0, 0, 0)
        if axis == 0:
            sub = Grid(along, tx, zx, kc, pml)
            plane = np.zeros((5, nt))
            plane[:, ta:tb] = 1.0
        else:
            sub = Grid(tx, along, zx, kc, pml)
            plane = np.zeros((nt, 5))
            plane[ta:tb, :] = 1.0
        maps = factor_maps(plane, EdgeConstants.build(sub, stackup.er))

        def mid(name, n):
            m = maps[name]
            return m[2, :, n] if axis == 0 else m[:, 2, n]

        a, t = ("x", "y") if axis == 0 else ("y", "x")
        eps["a"][:, kc] *= mid("e" + a, 0)
        eps["t"][:, kc] *= mid("e" + t, 0)
        eps["z"][:, kc - 1] *= mid("ez", 0)
        eps["z"][:, kc] *= mid("ez", 1)
        for n, k in enumerate((kc - 1, kc)):
            mu["a"][:, k] = mid("h" + a, n)
            mu["t"][:, k] = mid("h" + t, n)
        mu["z"][:, kc] = mid("hz", 0)
    return CrossSection(grid, axis, ta, tb, pitch_a, eps, sig, mu)


class _Operator:
    """A0 and B of the mode problem at one frequency on a `CrossSection`."""

    def __init__(self, cs: CrossSection, omega: float, dt: float, cpml: CPMLParams):
        g = cs.grid
        tx, zx = g.axis(1 - cs.axis), g.z
        self.nt, self.nz = tx.n, zx.n
        big = float(numerical_omega(omega, dt))
        c = float(conductance_factor(omega, dt))
        self.y = {k: -1j * big * cs.eps[k] + c * cs.sig[k] for k in cs.eps}
        self.al = {k: cs.mu[k] / (1j * big * MU0) for k in cs.mu}
        n_lo, n_hi = g.pml.along(1 - cs.axis)
        w = np.array([omega])

        def s(ax, lo, hi, pos):
            return stretch(profile(ax, lo, hi, pos, dt, cpml), w, dt)[0]

        st_e = np.ones(self.nt + 1, complex)
        st_e[1:-1] = s(tx, n_lo, n_hi, tx.nodes[1:-1])
        sz_e = np.ones(self.nz + 1, complex)
        sz_e[1:-1] = s(zx, 0, g.pml.z_hi, zx.nodes[1:-1])
        self.st_e, self.sz_e = st_e[:, None], sz_e[None, :]
        self.st_h = s(tx, n_lo, n_hi, tx.centers)[:, None]
        self.sz_h = s(zx, 0, g.pml.z_hi, zx.centers)[None, :]
        self.dtp, self.dzp = tx.primary[:, None], zx.primary[None, :]
        self.dtd, self.dzd = tx.dual[:, None], zx.dual[None, :]
        nt, nz = self.nt, self.nz
        # Free unknowns: tangential E vanishes on the outer walls, the ground and the top.
        self.free = {
            "a": np.zeros((nt + 1, nz + 1), bool),
            "t": np.zeros((nt, nz + 1), bool),
            "z": np.zeros((nt + 1, nz), bool),
        }
        self.free["a"][1:-1, 1:-1] = True
        self.free["t"][:, 1:-1] = True
        self.free["z"][1:-1, :] = True

    # field helpers ------------------------------------------------------------------------
    def _dt(self, f):  # nodes → t-edges (primary)
        return (f[1:] - f[:-1]) / self.dtp

    def _dz(self, f):  # nodes → z-edges (primary)
        return (f[:, 1:] - f[:, :-1]) / self.dzp

    def _dtd(self, f):  # t-edges → interior nodes (dual), zero at the ends
        out = np.zeros((f.shape[0] + 1,) + f.shape[1:], complex)
        out[1:-1] = (f[1:] - f[:-1]) / self.dtd[1:-1]
        return out

    def _dzd(self, f):
        out = np.zeros(f.shape[:1] + (f.shape[1] + 1,), complex)
        out[:, 1:-1] = (f[:, 1:] - f[:, :-1]) / self.dzd[:, 1:-1]
        return out

    def curl_a(self, et, ez):
        return self.st_h * self._dt(ez) - self.sz_h * self._dz(et)

    def a0(self, x):
        ea, et, ez = x
        ha = self.al["a"] * self.curl_a(et, ez)
        qz = self.al["z"] * self.st_h * self._dt(ea)  # (Nt, Nz+1)
        qt = self.al["t"] * self.sz_h * self._dz(ea)  # (Nt+1, Nz)
        ra = self.y["a"] * ea + self.st_e * self._dtd(qz) + self.sz_e * self._dzd(qt)
        rt = self.y["t"] * et - self.sz_e * self._dzd(ha) - qz
        rz = self.y["z"] * ez + self.st_e * self._dtd(ha) - qt
        return self._pin((ra, rt, rz), x, 1.0)

    def b(self, x):
        ea, et, ez = x
        ra = -(self.st_e * self._dtd(self.al["z"] * et) + self.sz_e * self._dzd(self.al["t"] * ez))
        rt = self.al["z"] * et
        rz = self.al["t"] * ez
        return self._pin((ra, rt, rz), x, 0.0)

    def _pin(self, r, x, diag):
        out = []
        for k, ri, xi in zip("atz", r, x):
            out.append(np.where(self.free[k], ri, diag * xi))
        return tuple(out)

    # packing: column j holds ea[j, :], ez[j, :], et[j, :] (j < Nt) --------------------------
    @property
    def block(self) -> int:
        return 3 * self.nz + 2

    def pack(self, x) -> np.ndarray:
        ea, et, ez = x
        return np.concatenate([ea[:-1], ez[:-1], et], axis=1)  # (Nt, B)

    def unpack(self, v: np.ndarray):
        nz = self.nz
        ea = np.zeros((self.nt + 1, nz + 1), complex)
        ez = np.zeros((self.nt + 1, nz), complex)
        ea[:-1] = v[:, : nz + 1]
        ez[:-1] = v[:, nz + 1 : 2 * nz + 1]
        et = np.array(v[:, 2 * nz + 1 :], dtype=complex)
        return ea, et, ez

    def pencil(self, sigma: complex):
        """Block-tridiagonal blocks (lower, diag, upper) of A0 − σB, by probing."""
        nt, nb = self.nt, self.block
        diag = np.zeros((nt, nb, nb), complex)
        lower = np.zeros((nt, nb, nb), complex)  # block (j, j−1)
        upper = np.zeros((nt, nb, nb), complex)  # block (j, j+1)
        for color in range(3):
            cols = np.arange(color, nt, 3)
            for q in range(nb):
                v = np.zeros((nt, nb), complex)
                v[cols, q] = 1.0
                x = self.unpack(v)
                ya, yb = self.pack(self.a0(x)), self.pack(self.b(x))
                y = ya - sigma * yb
                diag[cols, :, q] = y[cols]
                up = cols[cols > 0]
                upper[up - 1, :, q] = y[up - 1]
                lo = cols[cols < nt - 1]
                lower[lo + 1, :, q] = y[lo + 1]
        return lower, diag, upper


class _BlockLU:
    """LU of a block-tridiagonal matrix (block Thomas algorithm)."""

    def __init__(self, lower, diag, upper):
        n = diag.shape[0]
        self.inv = np.empty_like(diag)
        self.g = np.empty_like(diag)  # D'_j⁻¹ U_j
        self.lower = lower
        d = diag[0]
        for j in range(n):
            if j > 0:
                d = diag[j] - lower[j] @ self.g[j - 1]
            self.inv[j] = np.linalg.inv(d)
            self.g[j] = self.inv[j] @ upper[j]

    def solve(self, r: np.ndarray) -> np.ndarray:
        n = r.shape[0]
        y = np.empty_like(r)
        prev = None
        for j in range(n):
            rhs = r[j] if prev is None else r[j] - self.lower[j] @ prev
            y[j] = self.inv[j] @ rhs
            prev = y[j]
        x = np.empty_like(r)
        x[-1] = y[-1]
        for j in range(n - 2, -1, -1):
            x[j] = y[j] - self.g[j] @ x[j + 1]
        return x


@dataclass
class LineMode:
    """A solved line mode at one frequency: K (rad/m), K̃, and the transverse fields of the
    solver's frame (E_a, E_t, E_z, H_t, H_z; see the module doc for the orientation)."""

    omega: float
    k: complex
    k_tilde: complex
    ea: np.ndarray
    et: np.ndarray
    ez: np.ndarray
    ht: np.ndarray
    hz: np.ndarray
    iterations: int
    residual: float

    def eps_eff(self) -> float:
        return float((self.k.real * C0 / self.omega) ** 2)


def _k_estimate(cs: CrossSection, stackup, omega: float) -> float:
    from yapnr.rf.stackup import kirschning_jansen_eps_eff

    tx = cs.grid.axis(1 - cs.axis)
    w = float(tx.nodes[cs.tb] - tx.nodes[cs.ta])
    ee = kirschning_jansen_eps_eff(w, stackup.h, stackup.er, omega / (2 * math.pi))
    return omega * math.sqrt(ee) / C0


def line_mode(
    cs: CrossSection,
    stackup,
    omega: float,
    dt: float,
    cpml: CPMLParams = CPMLParams(),
    *,
    tol: float = 1e-10,
    max_iter: int = 30,
) -> LineMode:
    """The quasi-TEM mode of the cross-section at ω (see the module doc)."""
    op = _Operator(cs, omega, dt, cpml)
    k0 = _k_estimate(cs, stackup, omega)
    da = cs.pitch_a
    kt = 2.0 / da * math.sin(0.5 * k0 * da)
    sigma = kt * kt
    lu = _BlockLU(*op.pencil(sigma))
    # Start: E_z under the strip (the quasi-TEM field), unit elsewhere small.
    ea, et, ez = (np.zeros_like(op.y[k]) for k in "atz")
    ez[cs.ta : cs.tb + 1, : cs.grid.k_c] = 1.0
    x = op.pack((ea, et, ez))
    lam = sigma
    it = 0
    res = math.inf
    for it in range(1, max_iter + 1):
        bx = op.pack(op.b(op.unpack(x)))
        y = lu.solve(bx)
        nu = np.vdot(x, y) / np.vdot(x, x)
        lam_new = sigma + 1.0 / nu
        x = y / np.linalg.norm(y)
        if it >= 3 and abs(lam_new - lam) <= tol * abs(lam_new):
            lam = lam_new
            break
        lam = lam_new
    xa = op.unpack(x)
    r = op.pack(op.a0(xa)) - lam * op.pack(op.b(xa))
    res = float(np.linalg.norm(r) / max(np.linalg.norm(op.pack(op.a0(xa))), 1e-300))
    kt = np.sqrt(lam)
    k = 2.0 / da * np.arcsin(0.5 * kt * da)
    ea_s, et, ez = xa
    ea = ea_s / (1j * kt)
    ht = op.al["t"] * (op.sz_h * op._dz(ea) - 1j * kt * ez)
    hz = op.al["z"] * (1j * kt * et - op.st_h * op._dt(ea))
    return LineMode(omega, complex(k), complex(kt), ea, et, ez, ht, hz, it, res)


@dataclass
class ModeProfile:
    """The source profile per unit modal current, P(ω) = P0 + ω² P2, on the transverse E edges
    of the source plane: J_t on the (Nt, Nz+1) t-edges and J_z on the (Nt+1, Nz) z-edges,
    scaled so that max |P0| = 1. `fit_error` is the largest relative error of the fit at the
    band centre against a solve there."""

    jt0: np.ndarray
    jz0: np.ndarray
    jt2: np.ndarray
    jz2: np.ndarray
    f_ghz: tuple
    eps_eff: tuple
    fit_error: float


def _ring_current(cs: CrossSection, m: LineMode) -> complex:
    """∮ H·dl around the strip in the solver's frame (the port's current ring)."""
    tx, zx = cs.grid.axis(1 - cs.axis), cs.grid.z
    kc = cs.grid.k_c
    j = np.arange(cs.ta, cs.tb + 1)
    ring = np.sum((m.ht[j, kc - 1] - m.ht[j, kc]) * tx.dual[j])
    ring += (m.hz[cs.tb, kc] - m.hz[cs.ta - 1, kc]) * zx.dual[kc]
    return complex(ring)


# Solved profiles by the content of their arguments (`_profile_key`), so that every problem of
# a process (the optimizer's, the calibrations', the validation's; numpy, native, float32 or
# float64) drives its ports with the same bits, and the solve runs once. The solve's numpy
# complex arithmetic is not reproducible to the last bit between processes everywhere: on the
# development Mac (macOS 27 beta, M4, numpy 1.26.4) the same complex multiplication of fixed
# arrays rounded unfused (a·b − c·d) in the first calls of some processes started side by side
# and fused (an FMA) afterwards, cause not established; a profile then moved by an ulp and a
# line calibration by up to 5e-11 (docs/rf-solver-backends.md, Exactness). Read-only.
_PROFILES: OrderedDict = OrderedDict()
_PROFILES_MAX = 64


def _profile_key(cs: CrossSection, stackup, f_lo, f_hi, dt, cpml) -> str:
    h = hashlib.sha256()
    g = cs.grid
    for a in (g.x.nodes, g.y.nodes, g.z.nodes):
        h.update(np.ascontiguousarray(a, dtype=np.float64).tobytes())
    for d in (cs.eps, cs.sig, cs.mu):
        for k in sorted(d):
            v = np.ascontiguousarray(d[k])
            h.update(f"{k}{v.dtype}{v.shape}".encode())
            h.update(v.tobytes())
    h.update(
        repr(
            (g.k_c, g.pml, cs.axis, cs.ta, cs.tb, float(cs.pitch_a), stackup)
            + (float(f_lo), float(f_hi), float(dt), cpml)
        ).encode()
    )
    return h.hexdigest()


# Solved line modes by content (`_profile_key` and ω), for the modal port waves
# (`ports.ModalPlane`): every problem of a process projects on the same bits (see `_PROFILES`).
_MODES: OrderedDict = OrderedDict()
_MODES_MAX = 4096


def cached_line_mode(cs: CrossSection, stackup, omega: float, dt: float, cpml=CPMLParams()):
    """`line_mode` once per process for the same cross-section, ω, Δt and CPML (read-only)."""
    key = _profile_key(cs, stackup, omega, omega, dt, cpml)
    m = _MODES.get(key)
    if m is None:
        m = line_mode(cs, stackup, float(omega), dt, cpml)
        for a in (m.ea, m.et, m.ez, m.ht, m.hz):
            a.setflags(write=False)
        m.key = key
        _MODES[key] = m
        while len(_MODES) > _MODES_MAX:
            _MODES.popitem(last=False)
    else:
        _MODES.move_to_end(key)
    return m


def mode_profile(
    cs: CrossSection,
    stackup,
    f_lo: float,
    f_hi: float,
    dt: float,
    cpml: CPMLParams = CPMLParams(),
) -> ModeProfile:
    """Solve the mode at f_lo, f_hi and their mean and fit P0 + ω² P2 through the outer two
    (once per process for the same arguments: `_PROFILES`)."""
    key = _profile_key(cs, stackup, f_lo, f_hi, dt, cpml)
    prof = _PROFILES.get(key)
    if prof is None:
        prof = _solve_profile(cs, stackup, f_lo, f_hi, dt, cpml)
        for a in (prof.jt0, prof.jz0, prof.jt2, prof.jz2):
            a.setflags(write=False)
        _PROFILES[key] = prof
        while len(_PROFILES) > _PROFILES_MAX:
            _PROFILES.popitem(last=False)
    else:
        _PROFILES.move_to_end(key)
    return prof


def _solve_profile(cs, stackup, f_lo, f_hi, dt, cpml) -> ModeProfile:
    kc = cs.grid.k_c
    fs = (f_lo, 0.5 * (f_lo + f_hi), f_hi)
    prof, ees = [], []
    for f in fs:
        m = line_mode(cs, stackup, 2 * math.pi * f, dt, cpml)
        cur = _ring_current(cs, m)
        jt, jz = -m.hz / cur, m.ht / cur
        prof.append((jt, jz))
        ees.append(m.eps_eff())
    w = [(2 * math.pi * f) ** 2 for f in fs]
    jt2 = (prof[2][0] - prof[0][0]) / (w[2] - w[0])
    jz2 = (prof[2][1] - prof[0][1]) / (w[2] - w[0])
    jt0 = prof[0][0] - w[0] * jt2
    jz0 = prof[0][1] - w[0] * jz2
    # Real profiles (the lossy line's are real to about tan δ); the sign of J_z under the
    # strip negative, as the static source's.
    c = cs.ta + (cs.tb - cs.ta) // 2
    sgn = -1.0 if jz0[c, kc - 1].real > 0 else 1.0
    scale = sgn / max(np.abs(jt0.real).max(), np.abs(jz0.real).max())
    jt0, jz0, jt2, jz2 = (scale * a.real for a in (jt0, jz0, jt2, jz2))
    pt = scale * prof[1][0].real
    pz = scale * prof[1][1].real
    err = max(np.abs(jt0 + w[1] * jt2 - pt).max(), np.abs(jz0 + w[1] * jz2 - pz).max()) / max(
        np.abs(pt).max(), np.abs(pz).max()
    )
    return ModeProfile(jt0, jz0, jt2, jz2, tuple(f / 1e9 for f in fs), tuple(ees), float(err))
