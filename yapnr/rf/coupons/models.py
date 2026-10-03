"""Forward models (design §8.5): materials, conductor loss, lines, network algebra, structures.

Conventions: time dependence e^{jωt}, so a lossy permittivity is ε' − jε''. Two-port arrays have
shape (F, 2, 2) over the frequency vector. S-parameters use pseudo-waves with a (possibly complex)
reference impedance [MarksWilliams92: R. B. Marks, D. F. Williams, "A general waveguide circuit
theory", J. Res. NIST 97(5), 533-562, 1992], so a line referenced to its own Zc is matched.
T-parameters: [b1, a1]ᵀ = T [a2, b2]ᵀ, so cascades multiply and a matched line is
diag(e^{−γl}, e^{+γl}).

Materials:
- dielectric: the wideband Debye model of Djordjevic et al. 2001 and Svensson and Dermer 2001,
  fixed corners 1 kHz and 1 THz, set by Dk and Df at F_REF;
- conductor: R = R_s g K(f) (Wheeler's incremental inductance, g from the 2D tables) applied to
  the complex surface impedance (1+j) R_s so roughness adds internal inductance too [Shlepnev11],
  blended with the DC resistance; roughness by Huray's sphere model [Huray07] at a fixed radius
  (Simonovich's cannonball parameters [Simonovich16] reduce to the same form), or Groiss
  [Groiss96] as the alternative of the systematic refit.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Sequence, Tuple

import numpy as np

from yapnr.rf.coupons import families, stackups

C_LIGHT = 299792458.0
MU0 = 4.0e-7 * math.pi
EPS0 = 8.8541878128e-12
ETA0 = MU0 * C_LIGHT


# --- materials ----------------------------------------------------------------------------------


def ds_eps(dk: float, df: float, f: np.ndarray, f_ref: float = stackups.F_REF) -> np.ndarray:
    """Djordjevic-Sarkar εr(f) (complex, ε' − jε'') with ε(f_ref) = dk (1 − j df)."""
    w1, w2 = 2 * math.pi * stackups.DS_F1, 2 * math.pi * stackups.DS_F2
    span = math.log10(w2 / w1)

    def k(freq):
        w = 2 * math.pi * np.asarray(freq, float)
        return np.log10((w2 + 1j * w) / (w1 + 1j * w)) / span

    kr = k(f_ref)
    d_eps = -dk * df / kr.imag
    eps_inf = dk - d_eps * kr.real
    return eps_inf + d_eps * k(f)


def surface_resistance(f: np.ndarray, rho: float = stackups.RHO_CU) -> np.ndarray:
    return np.sqrt(math.pi * np.asarray(f, float) * MU0 * rho)


def skin_depth(f: np.ndarray, rho: float = stackups.RHO_CU) -> np.ndarray:
    return np.sqrt(rho / (math.pi * np.asarray(f, float) * MU0))


def huray(f: np.ndarray, ratio: float, radius_um: float = stackups.HURAY_RADIUS_UM) -> np.ndarray:
    """Huray roughness factor 1 + SR / (1 + δ/a + δ²/2a²): SR is the high-frequency excess
    (3/2 · N·4πa²/A_flat in Huray's notation)."""
    d = skin_depth(f) / (radius_um * 1e-6)
    return 1.0 + max(ratio, 0.0) / (1.0 + d + 0.5 * d * d)


def groiss(f: np.ndarray, rq_um: float) -> np.ndarray:
    """Groiss roughness factor 1 + exp(−(δ / 2Rq)^1.6)."""
    if rq_um <= 0:
        return np.ones_like(np.asarray(f, float))
    return 1.0 + np.exp(-((skin_depth(f) / (2e-6 * rq_um)) ** 1.6))


def kj_ratio(w: float, h: float, er: float, f: np.ndarray) -> np.ndarray:
    """Kirschning-Jansen dispersion εeff(f)/εeff(0) of a zero-thickness microstrip [KJ82]; used
    for the L1 families (for GCPW an estimate, design §4.3). w, h in mm."""
    f = np.asarray(f, float)
    u = max(w, 1e-4) / h
    a = (
        1.0
        + math.log((u**4 + (u / 52.0) ** 2) / (u**4 + 0.432)) / 49.0
        + math.log(1.0 + (u / 18.1) ** 3) / 18.7
    )
    b = 0.564 * ((er - 0.9) / (er + 3.0)) ** 0.053
    e0 = (er + 1.0) / 2.0 + (er - 1.0) / 2.0 * (1.0 + 10.0 / u) ** (-a * b)
    fn = f * 1e-9 * h
    p1 = (
        0.27488
        + (0.6315 + 0.525 / (1.0 + 0.0157 * fn) ** 20) * u
        - 0.065683 * math.exp(-8.7513 * u)
    )
    p2 = 0.33622 * (1.0 - math.exp(-0.03442 * er))
    p3 = 0.0363 * math.exp(-4.6 * u) * (1.0 - np.exp(-((fn / 38.7) ** 4.97)))
    p4 = 1.0 + 2.751 * (1.0 - math.exp(-((er / 15.916) ** 8)))
    p = p1 * p2 * ((0.1844 + p3 * p4) * fn) ** 1.5763
    return (er - (er - e0) / (1.0 + p)) / e0


# --- lines --------------------------------------------------------------------------------------


@dataclass
class Line:
    """Per-frequency propagation constant (1/m) and characteristic impedance (Ω) of one mode."""

    gamma: np.ndarray
    zc: np.ndarray

    @property
    def eps_eff(self) -> np.ndarray:
        """Effective permittivity from β (real part of (γ c / jω)²)."""
        return np.real((self.gamma * C_LIGHT / (1j * self._w)) ** 2)

    _w: np.ndarray = None  # angular frequency, set by family_line


def _rough(f, layer: str, v: Dict[str, float], model: str) -> np.ndarray:
    r = v.get(f"{layer}.rough", 0.0)
    if model == "groiss":
        return groiss(f, r)
    return huray(f, r)


def family_line(
    fam_id: str,
    v: Dict[str, float],
    f: np.ndarray,
    tables: Dict[str, families.Surrogate],
    mode: str = "",
    roughness: str = "huray",
    dispersion: bool = True,
) -> Line:
    """γ(f) and Zc(f) of a family (or one mode of a pair) at parameter values v.

    C(f) = C(F_REF) [1 + Σ_r q_r (ε_r(f)/Dk_r − 1)] is the first-order filling-factor expansion
    around the 2D solve (exact at F_REF); L = μ0 ε0 / C0. Geometric dispersion of the L1 lines
    scales L and C by √D(f) each (εeff by D, Zc unchanged), D from Kirschning-Jansen."""
    fam = families.FAMILIES[fam_id]
    f = np.asarray(f, float)
    w = 2 * math.pi * f
    out = tables[fam_id](v)
    p = f"{mode}:" if mode else ""
    c_ref, c0 = math.exp(out[p + "lnC"]), math.exp(out[p + "lnC0"])
    g = math.exp(out[p + "lng"])
    cf = np.ones_like(f, dtype=complex)
    for r, (dk_name, df_name) in fam.regions.items():
        dk, df = v[dk_name], v[df_name]
        cf += out[p + "q:" + r] * (ds_eps(dk, df, f) / dk - 1.0)
    C = c_ref * cf
    L = MU0 * EPS0 / c0
    if dispersion and fam.kind == "outer":
        w_e = fam.w - 2 * v["L1.etch"]
        d = np.sqrt(kj_ratio(w_e, v["pp1.h"], v["pp.dk"], f))
    else:
        d = np.ones_like(f)
    # conductor: skin-effect surface impedance with roughness, blended with DC
    zs = (1 + 1j) * surface_resistance(f) * g * _rough(f, fam.layer, v, roughness)
    if not fam.mask and fam.kind == "outer":
        zs = zs * v.get("L1.enig", 1.0)
    w_e = max(fam.w - 2 * v[fam.etch_param], 1e-3)
    r_dc = stackups.RHO_CU / (w_e * 1e-3 * v[fam.t_param] * 1e-3)
    zcond = np.sqrt(r_dc**2 + zs**2)
    Z = 1j * w * L * d + zcond
    Y = 1j * w * C * d
    gamma = np.sqrt(Z * Y)
    gamma = np.where(gamma.real < 0, -gamma, gamma)
    zc = np.sqrt(Z / Y)
    zc = np.where(zc.real < 0, -zc, zc)
    line = Line(gamma, zc)
    line._w = w
    return line


# --- network algebra (arrays of 2x2 over frequency) ---------------------------------------------


def _eye(n):
    m = np.zeros((n, 2, 2), complex)
    m[:, 0, 0] = m[:, 1, 1] = 1.0
    return m


def abcd_line(line: Line, length_mm: float) -> np.ndarray:
    gl = line.gamma * length_mm * 1e-3
    ch, sh = np.cosh(gl), np.sinh(gl)
    m = np.empty((len(gl), 2, 2), complex)
    m[:, 0, 0] = ch
    m[:, 0, 1] = line.zc * sh
    m[:, 1, 0] = sh / line.zc
    m[:, 1, 1] = ch
    return m


def abcd_shunt(y: np.ndarray) -> np.ndarray:
    m = _eye(len(y))
    m[:, 1, 0] = y
    return m


def abcd_series(z: np.ndarray) -> np.ndarray:
    m = _eye(len(z))
    m[:, 0, 1] = z
    return m


def cascade(*mats: np.ndarray) -> np.ndarray:
    out = mats[0]
    for m in mats[1:]:
        out = out @ m
    return out


def abcd_to_s(m: np.ndarray, z: np.ndarray) -> np.ndarray:
    """ABCD -> S with the same (complex) reference impedance at both ports (pseudo-waves)."""
    z = np.broadcast_to(np.asarray(z, complex), (m.shape[0],))
    A, B, C, D = m[:, 0, 0], m[:, 0, 1], m[:, 1, 0], m[:, 1, 1]
    den = A + B / z + C * z + D
    s = np.empty_like(m)
    s[:, 0, 0] = (A + B / z - C * z - D) / den
    s[:, 0, 1] = 2 * (A * D - B * C) / den
    s[:, 1, 0] = 2 / den
    s[:, 1, 1] = (-A + B / z - C * z + D) / den
    return s


def s_to_abcd(s: np.ndarray, z: np.ndarray) -> np.ndarray:
    z = np.broadcast_to(np.asarray(z, complex), (s.shape[0],))
    s11, s12, s21, s22 = s[:, 0, 0], s[:, 0, 1], s[:, 1, 0], s[:, 1, 1]
    m = np.empty_like(s)
    d = 2 * s21
    m[:, 0, 0] = ((1 + s11) * (1 - s22) + s12 * s21) / d
    m[:, 0, 1] = z * ((1 + s11) * (1 + s22) - s12 * s21) / d
    m[:, 1, 0] = ((1 - s11) * (1 - s22) - s12 * s21) / (z * d)
    m[:, 1, 1] = ((1 - s11) * (1 + s22) + s12 * s21) / d
    return m


def renormalize(s: np.ndarray, z_old, z_new) -> np.ndarray:
    return abcd_to_s(s_to_abcd(s, z_old), z_new)


def s_to_t(s: np.ndarray) -> np.ndarray:
    s11, s12, s21, s22 = s[:, 0, 0], s[:, 0, 1], s[:, 1, 0], s[:, 1, 1]
    t = np.empty_like(s)
    t[:, 0, 0] = (s12 * s21 - s11 * s22) / s21
    t[:, 0, 1] = s11 / s21
    t[:, 1, 0] = -s22 / s21
    t[:, 1, 1] = 1 / s21
    return t


def t_to_s(t: np.ndarray) -> np.ndarray:
    t11, t12, t21, t22 = t[:, 0, 0], t[:, 0, 1], t[:, 1, 0], t[:, 1, 1]
    s = np.empty_like(t)
    s[:, 0, 0] = t12 / t22
    s[:, 0, 1] = (t11 * t22 - t12 * t21) / t22
    s[:, 1, 0] = 1 / t22
    s[:, 1, 1] = -t21 / t22
    return s


def star(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Cascade of two-port S-parameters (Redheffer star product), a's port 2 to b's port 1, the
    same reference at the junction; works when a transmission is zero (a reflect standard)."""
    den = 1 - a[:, 1, 1] * b[:, 0, 0]
    s = np.empty_like(a)
    s[:, 0, 0] = a[:, 0, 0] + a[:, 0, 1] * b[:, 0, 0] * a[:, 1, 0] / den
    s[:, 0, 1] = a[:, 0, 1] * b[:, 0, 1] / den
    s[:, 1, 0] = a[:, 1, 0] * b[:, 1, 0] / den
    s[:, 1, 1] = b[:, 1, 1] + b[:, 1, 0] * a[:, 1, 1] * b[:, 0, 1] / den
    return s


def flip(s: np.ndarray) -> np.ndarray:
    """Swap the ports of a two-port."""
    return s[:, ::-1, ::-1].copy()


def gamma_in(s: np.ndarray, gl: np.ndarray) -> np.ndarray:
    """Reflection at port 1 with port 2 terminated in gl."""
    return s[:, 0, 0] + s[:, 0, 1] * s[:, 1, 0] * gl / (1 - s[:, 1, 1] * gl)


# --- discontinuities (closed forms, est.) -------------------------------------------------------


def open_end_extension(w: float, h: float, er: float, eps_eff: float) -> float:
    """Kirschning-Jansen-Koster open-end length extension Δl (mm) of a microstrip [KJK81]; for
    GCPW and stripline ends an estimate."""
    u = w / h
    e81 = eps_eff**0.81
    x1 = 0.434907 * (e81 + 0.26) / (e81 - 0.189) * (u**0.8544 + 0.236) / (u**0.8544 + 0.87)
    x2 = 1.0 + u**0.371 / (2.358 * er + 1.0)
    x3 = 1.0 + 0.5274 * math.atan(0.084 * u ** (1.9413 / x2)) / eps_eff**0.9236
    x4 = 1.0 + 0.0377 * math.atan(0.067 * u**1.456) * (6.0 - 5.0 * math.exp(0.036 * (1.0 - er)))
    x5 = 1.0 - 0.218 * math.exp(-7.5 * u)
    return h * x1 * x3 * x5 / x4


def via_inductance(h_mm: float, d_mm: float) -> float:
    """Goldfarb-Pucel inductance (H) of a via of diameter d through height h [Goldfarb91]."""
    h, r = h_mm * 1e-3, 0.5 * d_mm * 1e-3
    s = math.sqrt(r * r + h * h)
    return MU0 / (2 * math.pi) * (h * math.log((h + s) / r) + 1.5 * (r - s))


def gap_capacitances(w: float, h: float, s: float, er: float) -> Tuple[float, float]:
    """Series and shunt capacitance (F) of a microstrip end-to-end gap, Garg and Bahl (1978) as
    given in Gupta96 §3.4; est. outside 0.5 ≤ w/h ≤ 2, 0.1 ≤ s/w ≤ 1."""
    u = w / h
    sw = min(max(s / w, 0.1), 1.0)
    mo = u * (0.619 * math.log10(u) - 0.3853)
    ko = 4.26 - 1.453 * math.log10(u)
    if sw <= 0.3:
        me, ke = 0.8675, 2.043 * u**0.12
    else:
        me, ke = 1.565 / u**0.16 - 1.0, 1.97 - 0.03 / u
    w_m = w * 1e-3
    c_odd = sw**mo * math.exp(ko) * (er / 9.6) ** 0.8 * w_m * 1e-12  # pF/m * m
    c_even = 12.0 * sw**me * math.exp(ke) * (er / 9.6) ** 0.9 * w_m * 1e-12
    return 0.5 * c_odd - 0.25 * c_even, 0.5 * c_even


# --- elements of a structure --------------------------------------------------------------------


class Model:
    """Line and element evaluation of one stackup at parameter values v."""

    def __init__(
        self,
        stackup_id: str,
        v: Dict[str, float],
        f: np.ndarray,
        tables=None,
        roughness: str = "huray",
    ):
        self.st = stackups.get(stackup_id)
        self.v = dict(v)
        self.f = np.asarray(f, float)
        self.w = 2 * math.pi * self.f
        self.tables = tables if tables is not None else families.load(stackup_id)
        self.roughness = roughness
        self._lines: Dict[Tuple[str, str], Line] = {}

    def line(self, fam: str, mode: str = "") -> Line:
        key = (fam, mode)
        if key not in self._lines:
            self._lines[key] = family_line(
                fam, self.v, self.f, self.tables, mode, roughness=self.roughness
            )
        return self._lines[key]

    def abcd(self, el: Sequence) -> np.ndarray:
        """ABCD of one element: ("line", fam, mm), ("shunt_c", farads), ("series_l", henries),
        ("junction", name) (a shunt C nuisance parameter in pF, 0 if absent), ("stub", fam,
        mm, "open"|"short"), ("coupled", fam, mm), ("ring", fam, radius_mm, gap_mm) (gap
        coupled), ("ring2", fam, radius_mm, arc_fraction) (directly fed)."""
        kind = el[0]
        n = len(self.f)
        if kind == "line":
            return abcd_line(self.line(el[1]), el[2])
        if kind == "shunt_c":
            return abcd_shunt(1j * self.w * el[1])
        if kind == "series_l":
            return abcd_series(1j * self.w * el[1])
        if kind == "conn":
            # ("conn", fam, stick, port): a connection nuisance of the fit (fit.Predictor)
            dl = self.v.get(f"conn.{el[2]}.{el[3]}.dl", 0.0)
            c = abcd_shunt(1j * self.w * self.v.get(f"conn.{el[2]}.{el[3]}.c", 0.0) * 1e-12)
            line = abcd_line(self.line(el[1]), dl)
            return cascade(c, line) if el[3] == 1 else cascade(line, c)
        if kind == "junction":
            c = self.v.get(f"{el[1]}.c", 0.0) * 1e-12
            return abcd_shunt(1j * self.w * c) if c else _eye(n)
        if kind == "stub":
            return abcd_shunt(self.stub_admittance(el[1], el[2], el[3]))
        if kind == "coupled":
            return self.coupled_abcd(el[1], el[2])
        if kind == "ring":
            return self.ring_abcd(el[1], el[2], el[3])
        if kind == "ring2":
            return self.ring2_abcd(el[1], el[2], el[3])
        raise ValueError(f"unknown element {el!r}")

    def stub_admittance(self, fam: str, length_mm: float, end: str) -> np.ndarray:
        line = self.line(fam)
        t = np.tanh(line.gamma * length_mm * 1e-3)
        if end == "open":
            return t / line.zc
        # shorted by three vias through the L1-L2 prepreg (design §5.1), Goldfarb-Pucel
        zl = 1j * self.w * via_inductance(self.v["pp1.h"], 0.3) / 3.0
        zin = line.zc * (zl + line.zc * t) / (line.zc + zl * t)
        return 1.0 / zin

    def coupled_abcd(self, fam: str, length_mm: float) -> np.ndarray:
        """Coupled-line section, ports at opposite ends, other ends open (Z-matrix of the even
        and odd modes)."""
        le, lo = self.line(fam, "e"), self.line(fam, "o")
        x = length_mm * 1e-3
        ce = 1 / np.tanh(le.gamma * x)
        co = 1 / np.tanh(lo.gamma * x)
        se = 1 / np.sinh(le.gamma * x)
        so = 1 / np.sinh(lo.gamma * x)
        z11 = 0.5 * (le.zc * ce + lo.zc * co)
        z21 = 0.5 * (le.zc * se - lo.zc * so)
        m = np.empty((len(self.f), 2, 2), complex)
        m[:, 0, 0] = z11 / z21
        m[:, 0, 1] = (z11 * z11 - z21 * z21) / z21
        m[:, 1, 0] = 1 / z21
        m[:, 1, 1] = z11 / z21
        return m

    def gap_abcd(self, fam: str, gap_mm: float) -> np.ndarray:
        f = families.FAMILIES[fam]
        h = self.v["pp1.h"] if f.kind == "outer" else 0.5 * (self.v["pp3.h"] + self.v["core.h"])
        er = self.v["pp.dk"]
        cs, cp = gap_capacitances(f.w, h, gap_mm, er)
        y_s = 1j * self.w * cs
        return cascade(
            abcd_shunt(1j * self.w * cp), abcd_series(1 / y_s), abcd_shunt(1j * self.w * cp)
        )

    def ring2_abcd(self, fam: str, radius_mm: float, arc_frac: float = 0.25) -> np.ndarray:
        """Directly fed ring: two arcs of the ring's centre line in parallel (Y-parameters
        added), the shorter `arc_frac` of the circumference. With the feeds a quarter turn apart
        the transmission vanishes where the circumference is a whole number of wavelengths, so
        the notches sit at the ring resonances [Wolff71, Chang04] however lossy the ring; the
        two T-junctions are not modelled (est.)."""
        c = 2 * math.pi * radius_mm
        line = self.line(fam)
        y = abcd_to_y(abcd_line(line, arc_frac * c)) + abcd_to_y(
            abcd_line(line, (1 - arc_frac) * c)
        )
        return y_to_abcd(y)

    def ring_abcd(self, fam: str, radius_mm: float, gap_mm: float) -> np.ndarray:
        """Gap-coupled ring, feeds at 0° and 180°: two half rings in parallel between gaps."""
        half = abcd_line(self.line(fam), math.pi * radius_mm)
        y = abcd_to_y(half) * 2.0
        both = y_to_abcd(y)
        g = self.gap_abcd(fam, gap_mm)
        return cascade(g, both, g)


def abcd_to_y(m: np.ndarray) -> np.ndarray:
    A, B, C, D = m[:, 0, 0], m[:, 0, 1], m[:, 1, 0], m[:, 1, 1]
    y = np.empty_like(m)
    y[:, 0, 0] = D / B
    y[:, 0, 1] = -(A * D - B * C) / B
    y[:, 1, 0] = -1 / B
    y[:, 1, 1] = A / B
    return y


def y_to_abcd(y: np.ndarray) -> np.ndarray:
    y11, y12, y21, y22 = y[:, 0, 0], y[:, 0, 1], y[:, 1, 0], y[:, 1, 1]
    m = np.empty_like(y)
    m[:, 0, 0] = -y22 / y21
    m[:, 0, 1] = -1 / y21
    m[:, 1, 0] = -(y11 * y22 - y12 * y21) / y21
    m[:, 1, 1] = -y11 / y21
    return m


def meander_resistance(
    w_mm: float, t_mm: float, etch_mm: float, length_mm: float, corners: int, temp_c: float = 20.0
) -> float:
    """DC resistance (Ω) of a meander: straight squares plus 0.56 squares per 90° corner."""
    we = w_mm - 2 * etch_mm
    rho = stackups.RHO_CU * (1 + stackups.ALPHA_CU * (temp_c - 20.0))
    return rho / (t_mm * 1e-3) * (length_mm / we + 0.56 * corners)


def structure_s(model: Model, elements: Sequence, z_ref) -> np.ndarray:
    """S-parameters of a cascade of elements referenced to z_ref (array or scalar)."""
    return abcd_to_s(cascade(*[model.abcd(e) for e in elements]), z_ref)
