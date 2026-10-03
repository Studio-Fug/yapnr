"""Closed-form line, patch and column models at 60-64 GHz (stdlib only).

Every function names its source. None of this is full-wave; the 2D cross-section solver
(`xsec2d.py`) replaces the static line parameters, and openEMS (`../openems/`) checks the column.

Sources:
- [HJ80] E. Hammerstad, O. Jensen, "Accurate models for microstrip computer-aided design",
  IEEE MTT-S Int. Microwave Symp. Digest, 1980, pp. 407-409: static Z0 and εeff of microstrip,
  with the finite-thickness width correction.
- [KJ82] M. Kirschning, R. H. Jansen, "Accurate model for effective dielectric constant of
  microstrip with validity up to millimetre-wave frequencies", Electronics Letters 18(6), 1982,
  pp. 272-273: dispersion of εeff.
- [GN87] G. Ghione, C. Naldi, "Coplanar waveguides for MMIC applications: effect of upper
  shielding, conductor backing, finite-extent ground planes, and line-to-line coupling",
  IEEE Trans. MTT 35(3), 1987, pp. 260-267: conductor-backed CPW by conformal mapping
  (also B. C. Wadell, "Transmission Line Design Handbook", Artech House 1991, §3.4.3).
- [Bal16] C. A. Balanis, "Antenna Theory: Analysis and Design", 4th ed., Wiley 2016, §14.2:
  transmission-line model of the rectangular patch (W, εreff, ΔL, L; G1, G12; inset feed
  R(y0) = R(0) cos²(π y0 / L)), §14.2.4 directivity of the two-slot model.
- [JA91] D. R. Jackson, N. G. Alexopoulos, "Simple approximate formulas for input resistance,
  bandwidth, and efficiency of a resonant rectangular patch", IEEE Trans. AP 39(3), 1991,
  pp. 407-410: BW(VSWR 2) = 3.77 (εr-1)/εr² (W/L)(h/λ0).
- [Poz12] D. M. Pozar, "Microwave Engineering", 4th ed., Wiley 2012, §2.5 (quarter-wave
  transformer), §7.1 (T-junction divider).
- [Ham75] E. Hammerstad, "Computer-aided design of microstrip couplers with accurate
  discontinuity models", 1981 MTT-S Digest; roughness factor K = 1 + (2/π) atan(1.4 (Δ/δ)²)
  (also Pozar §2.7 for conductor loss of a quasi-TEM line).
"""

from __future__ import annotations

import math
from typing import Dict, Tuple

C0 = 299792458.0
ETA0 = 376.730313668
MU0 = 4e-7 * math.pi
SIGMA_CU = 5.8e7
SIGMA_AG = 6.1e7


# --- microstrip [HJ80], [KJ82] -------------------------------------------------------------


def _z01(u: float) -> float:
    fu = 6 + (2 * math.pi - 6) * math.exp(-((30.666 / u) ** 0.7528))
    return ETA0 / (2 * math.pi) * math.log(fu / u + math.sqrt(1 + 4 / u**2))


def _eeff0(u: float, er: float) -> float:
    a = (
        1
        + math.log((u**4 + (u / 52) ** 2) / (u**4 + 0.432)) / 49
        + math.log(1 + (u / 18.1) ** 3) / 18.7
    )
    b = 0.564 * ((er - 0.9) / (er + 3)) ** 0.053
    return (er + 1) / 2 + (er - 1) / 2 * (1 + 10 / u) ** (-a * b)


def ms_static(w: float, h: float, t: float, er: float) -> Tuple[float, float]:
    """Static (Z0 ohm, εeff) of microstrip, width w on height h, thickness t (mm) [HJ80]."""
    u, tn = w / h, t / h
    if tn > 0:
        du1 = tn / math.pi * math.log(1 + 4 * math.e / (tn / math.tanh(math.sqrt(6.517 * u)) ** 2))
        dur = 0.5 * (1 + 1 / math.cosh(math.sqrt(er - 1))) * du1
    else:
        du1 = dur = 0.0
    u1, ur = u + du1, u + dur
    ee = _eeff0(ur, er) * (_z01(u1) / _z01(ur)) ** 2
    return _z01(ur) / math.sqrt(_eeff0(ur, er)), ee


def kj_dispersion(e0: float, w: float, h: float, er: float, f_hz: float) -> float:
    """εeff(f) from the static e0 [KJ82] (valid to fn = f·h ≤ 25 GHz·mm)."""
    u = w / h
    fn = f_hz / 1e9 * h
    p1 = 0.27488 + (0.6315 + 0.525 / (1 + 0.0157 * fn) ** 20) * u - 0.065683 * math.exp(-8.7513 * u)
    p2 = 0.33622 * (1 - math.exp(-0.03442 * er))
    p3 = 0.0363 * math.exp(-4.6 * u) * (1 - math.exp(-((fn / 38.7) ** 4.97)))
    p4 = 1 + 2.751 * (1 - math.exp(-((er / 15.916) ** 8)))
    p = p1 * p2 * ((0.1844 + p3 * p4) * fn) ** 1.5763
    return er - (er - e0) / (1 + p)


def ms_width(z0: float, h: float, t: float, er: float) -> float:
    lo, hi = 0.005 * h, 20 * h
    for _ in range(100):
        m = 0.5 * (lo + hi)
        lo, hi = (m, hi) if ms_static(m, h, t, er)[0] > z0 else (lo, m)
    return 0.5 * (lo + hi)


# --- conductor-backed CPW [GN87] -----------------------------------------------------------


def _k_ratio(k: float) -> float:
    """K(k)/K(k') by the arithmetic-geometric mean (exact to double precision)."""

    def agm_k(m: float) -> float:
        a, b = 1.0, math.sqrt(1 - m * m)
        for _ in range(60):
            a, b = (a + b) / 2, math.sqrt(a * b)
        return math.pi / (2 * a)

    kp = math.sqrt(1 - k * k)
    return agm_k(k) / agm_k(kp)


def gcpw_static(w: float, g: float, h: float, er: float) -> Tuple[float, float]:
    """Zero-thickness conductor-backed CPW with infinite side grounds (Z0, εeff) [GN87]."""
    a, b = w / 2, w / 2 + g
    k = a / b
    k3 = math.tanh(math.pi * a / (2 * h)) / math.tanh(math.pi * b / (2 * h))
    r1, r3 = _k_ratio(k), _k_ratio(k3)
    ee = (1 + er * r3 / r1) / (1 + r3 / r1)
    z = 60 * math.pi / math.sqrt(ee) / (r1 + r3)
    return z, ee


# --- loss ----------------------------------------------------------------------------------


def surface_resistance(f_hz: float, sigma: float = SIGMA_CU) -> float:
    return math.sqrt(math.pi * f_hz * MU0 / sigma)


def roughness_factor(rq_um: float, f_hz: float, sigma: float = SIGMA_CU) -> float:
    """Hammerstad-Bekkadal: K = 1 + (2/π) atan(1.4 (Rq/δ)²) [Ham75]."""
    delta_um = 1 / math.sqrt(math.pi * f_hz * MU0 * sigma) * 1e6
    return 1 + 2 / math.pi * math.atan(1.4 * (rq_um / delta_um) ** 2)


def line_loss_db_per_mm(
    z0: float, eeff: float, er: float, tand: float, w: float, f_hz: float, k_rough: float, g_m=None
) -> Tuple[float, float]:
    """(dielectric, conductor) dB/mm of a quasi-TEM line. Dielectric: filling-factor form
    [Poz12 §3.8]. Conductor: Wheeler's R = Rs·g when the 2D solver supplied g (1/m), else
    Rs/(Z0 w) x 1.3 for the strip with its current crowding and the ground (±30 %)."""
    lam0_mm = C0 / f_hz * 1e3
    ad = 27.287 * er * (eeff - 1) * tand / (math.sqrt(eeff) * (er - 1) * lam0_mm)
    rs = surface_resistance(f_hz) * k_rough
    if g_m is not None:
        r_per_m = rs * g_m
    else:
        r_per_m = rs / (w * 1e-3) * 1.3
    ac = 8.686 * r_per_m / (2 * z0) / 1e3
    return ad, ac


# --- patch [Bal16], [JA91] -----------------------------------------------------------------


def patch_width(f_hz: float, er: float) -> float:
    return C0 / (2 * f_hz) * math.sqrt(2 / (er + 1)) * 1e3


def patch_ereff(w: float, h: float, er: float) -> float:
    return (er + 1) / 2 + (er - 1) / 2 / math.sqrt(1 + 12 * h / w)


def patch_dl(w: float, h: float, er: float) -> float:
    e = patch_ereff(w, h, er)
    return 0.412 * h * (e + 0.3) * (w / h + 0.264) / ((e - 0.258) * (w / h + 0.8))


def patch_length(f_hz: float, w: float, h: float, er: float) -> float:
    """Resonant length L (mm) for width w [Bal16 (14-1)-(14-7)]."""
    e = patch_ereff(w, h, er)
    return C0 / (2 * f_hz * math.sqrt(e)) * 1e3 - 2 * patch_dl(w, h, er)


def patch_f0(w: float, length: float, h: float, er: float) -> float:
    e = patch_ereff(w, h, er)
    return C0 / (2 * (length + 2 * patch_dl(w, h, er)) * 1e-3 * math.sqrt(e))


def patch_bw(w: float, length: float, h: float, er: float, f_hz: float) -> float:
    """Fractional VSWR-2 bandwidth [JA91]."""
    lam0 = C0 / f_hz * 1e3
    return 3.77 * (er - 1) / er**2 * (w / length) * (h / lam0)


def _integrate(fn, a: float, b: float, n: int = 400) -> float:
    hstep = (b - a) / n
    s = fn(a) + fn(b)
    for i in range(1, n):
        s += (4 if i % 2 else 2) * fn(a + i * hstep)
    return s * hstep / 3


def slot_g1_g12(w: float, length: float, f_hz: float) -> Tuple[float, float]:
    """Radiating-slot self conductance G1 and mutual G12 (S) [Bal16 (14-10), (14-18a)]."""
    k0 = 2 * math.pi * f_hz / C0 * 1e-3  # 1/mm

    def base(th):
        c = math.cos(th)
        return (math.sin(k0 * w / 2 * c) / c) ** 2 if abs(c) > 1e-9 else (k0 * w / 2) ** 2

    def i1(th):
        return base(th) * math.sin(th) ** 3

    def i12(th):
        return base(th) * _j0(k0 * length * math.sin(th)) * math.sin(th) ** 3

    g1 = _integrate(i1, 0, math.pi) / (120 * math.pi**2)
    g12 = _integrate(i12, 0, math.pi) / (120 * math.pi**2)
    return g1, g12


def _j0(x: float) -> float:
    # Bessel J0 by its integral form (adequate for x ≤ 10)
    return _integrate(lambda t: math.cos(x * math.sin(t)), 0, math.pi, 200) / math.pi


def inset_depth(w: float, length: float, f_hz: float, r_target: float = 50.0) -> Dict[str, float]:
    """Edge resistance Rin(0) = 1 / (2 (G1 + G12)) and the inset y0 for r_target
    [Bal16 (14-17), (14-20a)]."""
    g1, g12 = slot_g1_g12(w, length, f_hz)
    r0 = 1 / (2 * (g1 + g12))
    c = math.sqrt(min(1.0, r_target / r0))
    return dict(g1=g1, g12=g12, r_edge=r0, y0=length / math.pi * math.acos(c))


def patch_directivity(w: float, length: float, h: float, er: float, f_hz: float) -> float:
    """Directivity (linear) of the two-slot model over an infinite ground, numerically
    integrated over the upper half-space [Bal16 §14.2.4, (14-53)]."""
    k0 = 2 * math.pi * f_hz / C0 * 1e-3
    leff = length + 2 * patch_dl(w, h, er)

    def u(th, ph):
        st, ct, sp, cp = math.sin(th), math.cos(th), math.sin(ph), math.cos(ph)
        x = k0 * h / 2 * st * cp
        z = k0 * w / 2 * ct
        a = math.sin(x) / x if abs(x) > 1e-12 else 1.0
        b = math.sin(z) / z if abs(z) > 1e-12 else 1.0
        return (a * b * st) ** 2 * math.cos(k0 * leff / 2 * st * sp) ** 2

    # Balanis' frame: slots along z (width w), separated along y; broadside is +x (θ=90°, φ=0).
    n = 120
    tot = 0.0
    for i in range(n):
        th = (i + 0.5) * math.pi / n
        row = 0.0
        for j in range(n):
            ph = -math.pi / 2 + (j + 0.5) * math.pi / n
            row += u(th, ph)
        tot += row * math.sin(th) * (math.pi / n) * (math.pi / n)
    return 4 * math.pi * u(math.pi / 2, 0.0) / tot


def column_directivity(
    w: float, length: float, h: float, er: float, f_hz: float, spacing: float
) -> float:
    """Two patches in the E-plane, in phase: the two-slot pattern times 2cos(k0 s sinθ' / 2)
    with θ' the angle from broadside in the E-plane, integrated over the half-space."""
    k0 = 2 * math.pi * f_hz / C0 * 1e-3
    leff = length + 2 * patch_dl(w, h, er)

    def u(th, ph):
        st, ct, sp, cp = math.sin(th), math.cos(th), math.sin(ph), math.cos(ph)
        x = k0 * h / 2 * st * cp
        z = k0 * w / 2 * ct
        a = math.sin(x) / x if abs(x) > 1e-12 else 1.0
        b = math.sin(z) / z if abs(z) > 1e-12 else 1.0
        af = math.cos(k0 * spacing / 2 * st * sp) ** 2
        return (a * b * st) ** 2 * math.cos(k0 * leff / 2 * st * sp) ** 2 * af

    n = 140
    tot = 0.0
    for i in range(n):
        th = (i + 0.5) * math.pi / n
        row = 0.0
        for j in range(n):
            ph = -math.pi / 2 + (j + 0.5) * math.pi / n
            row += u(th, ph)
        tot += row * math.sin(th) * (math.pi / n) * (math.pi / n)
    return 4 * math.pi * u(math.pi / 2, 0.0) / tot


def patch_efficiency(bw: float, tand: float, h_mm: float, f_hz: float, k_rough: float) -> float:
    """Radiation efficiency from Q: 1/Q = 1/Qrad + tanδ + K δ/h, Qrad = 1/(√2 BW)
    [JA91; Pozar-style Q bookkeeping]."""
    delta_mm = 1 / math.sqrt(math.pi * f_hz * MU0 * SIGMA_CU) * 1e3
    qr = 1 / (math.sqrt(2) * bw)
    return (1 / qr) / (1 / qr + tand + k_rough * delta_mm / h_mm)


def qw_transformer(z_load: float, z_in: float = 50.0) -> float:
    """Impedance of a quarter-wave section matching z_load to z_in [Poz12 §2.5]."""
    return math.sqrt(z_load * z_in)


def guided_wavelength(f_hz: float, eeff: float) -> float:
    return C0 / f_hz / math.sqrt(eeff) * 1e3
