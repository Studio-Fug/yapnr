"""Stackup physics for SI decks: per-layer trace L'/C', via L/C, pad C (pure Python).

One stackup feeds every SI number. Copper thicknesses, board thickness and via
geometry come from the fab profile (:mod:`pnr.fab_profile`, ``jlc-pofv``: 35 um outer,
15.2 um inner, 0.45/0.30 mm default via). Dielectric thicknesses and permittivities
of JLC04161H-7628 come from ``docs/fab-comparison.md`` (the "Default stackup" row, the
source the profile's qualification text cites): F.Cu 35 um / 7628 prepreg 0.2104 mm
er 4.4 / In1 15.2 um / core 1.065 mm er 4.6 / In2 15.2 um / 0.2104 mm er 4.4 / B.Cu 35 um.
``rules['stackup']`` (design WP0) replaces the table when present. Reference planes
are the ``plane_layer`` of rules net classes (In1.Cu on splanc-mini).

Formulas, implemented from the publications (not copied from any GPL code):

* surface microstrip: Hammerstad & Jensen, "Accurate models for microstrip
  computer-aided design", IEEE MTT-S 1980 (static Z0 and eps_eff, with the
  thickness correction) - anchors: 0.20 mm on F.Cu over In1 -> 67.9 ohm, eps_eff 2.99.
* inner layer with one reference plane (embedded microstrip): IPC-2141A,
  ``Z0 = 60/sqrt(er') ln(5.98 h/(0.8 w + t))``, ``er' = er (1 - exp(-1.55 H1/h))``
  with H1 the dielectric height from the plane to the covering surface.
* inner layer between two planes: IPC-2141A asymmetric stripline.
* via inductance: Johnson & Graham, High-Speed Digital Design, eq. 7.9,
  ``L = 5.08 h (ln(4 h/d) + 1)`` nH with h, d in inches (sigcon.com/Pubs/news/6_04.htm):
  1.6 mm barrel / 0.3 mm drill -> 1.30 nH, F.Cu->In1 (0.245 mm) -> 0.107 nH.
* via capacitance: HSDD p. 258, ``C = 1.41 er T D1/(D2 - D1)`` pF (inches).
* pad capacitance: parallel plate over the nearest plane (no fringing).

These are ranking-grade estimates for a lumped model; they are not a field solve.
"""

from __future__ import annotations

import math

C0 = 299792458.0  # m/s
EPS0 = 8.8541878128e-12  # F/m
MM_PER_IN = 25.4

# JLC04161H-7628 (docs/fab-comparison.md, "Default stackup"); copper thicknesses are
# overwritten from the fab profile's copper model when available.
JLC04161H_7628 = dict(
    name="JLC04161H-7628",
    source='docs/fab-comparison.md "Default stackup (JLC04161H-7628)"; https://jlcpcb.com/impedance',
    layers=[
        dict(name="F.Cu", kind="copper", t_mm=0.035),
        dict(name="prepreg-1", kind="dielectric", t_mm=0.2104, er=4.4, material="7628 prepreg"),
        dict(name="In1.Cu", kind="copper", t_mm=0.0152),
        dict(name="core", kind="dielectric", t_mm=1.065, er=4.6, material="FR-4 core"),
        dict(name="In2.Cu", kind="copper", t_mm=0.0152),
        dict(name="prepreg-2", kind="dielectric", t_mm=0.2104, er=4.4, material="7628 prepreg"),
        dict(name="B.Cu", kind="copper", t_mm=0.035),
    ],
)

DEFAULT_ANTIPAD_CLEARANCE_MM = (
    0.2  # plane clearance around a via pad (assumed, fab 'hole_clearance_mm')
)


def _copper_from_profile():
    """Outer/inner copper thickness (mm) and the default via of the active fab profile."""
    try:
        from pnr import fab_profile

        name = fab_profile.active_name()
        spec = fab_profile.PROFILES.get(name) or fab_profile.PROFILES["jlc-pofv"]
    except Exception:  # pragma: no cover - profile module unavailable (KiCad python)
        return None
    cu = spec["copper"]
    return dict(
        outer_mm=cu["outer_copper_um"] / 1000.0,
        inner_mm=cu["inner_copper_um"] / 1000.0,
        via_diameter_mm=cu["via_diameter_mm"],
        via_drill_mm=cu["via_drill_mm"],
        board_thickness_mm=cu["board_thickness_mm"],
        profile=name,
    )


def stackup(rules=None):
    """The stack used by every SI calculation (a plain dict, JSON-serialisable).

    ``rules['stackup']`` wins when present (a dict with ``layers`` and optional
    ``planes``). Otherwise JLC04161H-7628 with the profile's copper thicknesses;
    planes are the net-class ``plane_layer`` values of ``rules`` (default In1.Cu).
    """
    rules = rules or {}
    if isinstance(rules.get("stackup"), dict) and rules["stackup"].get("layers"):
        st = dict(rules["stackup"])
        st["layers"] = [dict(x) for x in st["layers"]]
    else:
        st = dict(JLC04161H_7628)
        st["layers"] = [dict(x) for x in JLC04161H_7628["layers"]]
        cu = _copper_from_profile()
        if cu:
            coppers = [x for x in st["layers"] if x["kind"] == "copper"]
            for i, x in enumerate(coppers):
                x["t_mm"] = cu["outer_mm"] if i in (0, len(coppers) - 1) else cu["inner_mm"]
            st["fab_profile"] = cu["profile"]
            st["via"] = dict(diameter_mm=cu["via_diameter_mm"], drill_mm=cu["via_drill_mm"])
    if "planes" not in st:
        planes = sorted(
            {c["plane_layer"] for c in rules.get("net_classes", []) if c.get("plane_layer")}
        )
        st["planes"] = planes or ["In1.Cu"]
    st.setdefault("via", dict(diameter_mm=0.45, drill_mm=0.30))
    st.setdefault("antipad_clearance_mm", DEFAULT_ANTIPAD_CLEARANCE_MM)
    return st


def copper_layers(st):
    """Copper layer names top to bottom."""
    return [x["name"] for x in st["layers"] if x["kind"] == "copper"]


def z_table(st):
    """{copper layer: (z_top, z_bottom)} in mm from the board top surface."""
    z = 0.0
    out = {}
    for x in st["layers"]:
        if x["kind"] == "copper":
            out[x["name"]] = (z, z + x["t_mm"])
        z += x["t_mm"]
    return out


def thickness(st):
    return sum(x["t_mm"] for x in st["layers"])


def via_span_mm(st, a, b):
    """Barrel length a current traverses between copper layers ``a`` and ``b``.

    Measured between the copper surfaces the current enters/leaves: top surface of
    the upper layer to the top surface of the lower one, except that B.Cu counts to
    the board's bottom surface (so F.Cu->B.Cu is the whole board, F.Cu->In1.Cu is
    35 um + 0.2104 mm = 0.245 mm on JLC04161H-7628, the HSDD anchor).
    """
    z = z_table(st)
    names = copper_layers(st)
    ia, ib = names.index(a), names.index(b)
    if ia > ib:
        a, b, ia, ib = b, a, ib, ia
    lo = z[b][1] if ib == len(names) - 1 else z[b][0]
    return abs(lo - z[a][0])


def _dielectric_between(st, za, zb):
    """(height zb - za, thickness-weighted er of the dielectric in it) for depths za < zb.

    Non-plane copper in between (an unpoured signal layer) counts as height but not
    as permittivity: it is mostly etched away.
    """
    diel = wer = 0.0
    for z0, z1, x in _spans(st):
        seg = max(0.0, min(z1, zb) - max(z0, za))
        if seg and x["kind"] == "dielectric":
            diel += seg
            wer += seg * float(x["er"])
    return max(0.0, zb - za), (wer / diel if diel > 0 else 1.0)


def _spans(st):
    z = 0.0
    for x in st["layers"]:
        yield z, z + x["t_mm"], x
        z += x["t_mm"]


# ------------------------------------------------------------------ line models


def microstrip_hj(w, h, t, er):
    """Hammerstad-Jensen surface microstrip: (Z0 ohm, eps_eff). Units consistent (mm)."""

    def z0_air(u):
        f = 6 + (2 * math.pi - 6) * math.exp(-((30.666 / u) ** 0.7528))
        return 60 * math.log(f / u + math.sqrt(1 + (2 / u) ** 2))

    def eeff0(u):
        a = (
            1
            + math.log((u**4 + (u / 52) ** 2) / (u**4 + 0.432)) / 49
            + math.log(1 + (u / 18.1) ** 3) / 18.7
        )
        b = 0.564 * ((er - 0.9) / (er + 3)) ** 0.053
        return (er + 1) / 2 + (er - 1) / 2 * (1 + 10 / u) ** (-a * b)

    u = w / h
    if t > 0:
        tn = t / h
        du1 = (
            tn
            / math.pi
            * math.log(1 + 4 * math.e / (tn * (1 / math.tanh(math.sqrt(6.517 * u))) ** 2))
        )
        dur = 0.5 * (1 + 1 / math.cosh(math.sqrt(max(er - 1, 0.0)))) * du1
    else:
        du1 = dur = 0.0
    u1, ur = u + du1, u + dur
    ee = eeff0(ur) * (z0_air(u1) / z0_air(ur)) ** 2
    z0 = z0_air(ur) / math.sqrt(eeff0(ur))
    return z0, ee


def embedded_microstrip_ipc(w, h, t, er, h_total):
    """IPC-2141A embedded microstrip: (Z0, er'). ``h_total`` = plane to covering surface."""
    er_eff = er * (1 - math.exp(-1.55 * h_total / h))
    z0 = 60 / math.sqrt(er_eff) * math.log(5.98 * h / (0.8 * w + t))
    return z0, er_eff


def stripline_asym_ipc(w, t, h1, h2, er):
    """IPC-2141A asymmetric stripline (h1 <= h2 to the nearer/farther plane): (Z0, er)."""
    h1, h2 = min(h1, h2), max(h1, h2)
    z0 = (
        80
        / math.sqrt(er)
        * math.log(1.9 * (2 * h1 + t) / (0.8 * w + t))
        * (1 - h1 / (4 * (h1 + h2 + t)))
    )
    return z0, er


def line(st, layer, width_mm):
    """Per-length parameters of a ``width_mm`` trace on copper ``layer``.

    Returns dict(z0_ohm, eps_eff, l_nh_per_mm, c_pf_per_mm, td_ps_per_mm, model,
    reference, h_mm, er). The reference is the nearest plane above and/or below.
    """
    z = z_table(st)
    names = copper_layers(st)
    if layer not in z:
        raise ValueError("layer %r not in stackup %s" % (layer, names))
    top, bot = z[layer]
    t = bot - top
    planes = [p for p in st.get("planes", []) if p in z and p != layer]
    above = [p for p in planes if z[p][1] <= top]
    below = [p for p in planes if z[p][0] >= bot]
    pa = max(above, key=lambda p: z[p][1]) if above else None
    pb = min(below, key=lambda p: z[p][0]) if below else None
    outer = names.index(layer) in (0, len(names) - 1)
    total = thickness(st)
    if pa and pb:
        h1, er1 = _dielectric_between(st, z[pa][1], top)
        h2, er2 = _dielectric_between(st, bot, z[pb][0])
        er = (er1 * h1 + er2 * h2) / (h1 + h2)
        z0, ee = stripline_asym_ipc(width_mm, t, h1, h2, er)
        model, ref, h = "stripline_asym_ipc2141a", [pa, pb], min(h1, h2)
    elif pa or pb:
        if pb:
            h, er = _dielectric_between(st, bot, z[pb][0])
            cover_lo, cover_hi = 0.0, top  # dielectric above the trace up to the top surface
            ref = [pb]
        else:
            h, er = _dielectric_between(st, z[pa][1], top)
            cover_lo, cover_hi = bot, total
            ref = [pa]
        if outer:
            z0, ee = microstrip_hj(width_mm, h, t, er)
            model = "microstrip_hammerstad_jensen"
        else:
            # IPC H1: plane to the covering surface through dielectric only (etched signal copper excluded)
            cover = sum(
                max(0.0, min(z1, cover_hi) - max(z0, cover_lo))
                for z0, z1, x in _spans(st)
                if x["kind"] == "dielectric"
            )
            z0, ee = embedded_microstrip_ipc(width_mm, h, t, er, h + t + cover)
            model = "embedded_microstrip_ipc2141a"
    else:
        # No plane in the stack: pessimistic microstrip over the far surface.
        h, er = total - t, 4.5
        z0, ee = microstrip_hj(width_mm, h, t, er)
        model, ref = "no_plane_microstrip_pessimistic", []
    v = C0 / math.sqrt(ee)  # m/s
    l_per_m = z0 / v
    c_per_m = 1.0 / (z0 * v)
    return dict(
        z0_ohm=z0,
        eps_eff=ee,
        l_nh_per_mm=l_per_m * 1e6,
        c_pf_per_mm=c_per_m * 1e9,
        td_ps_per_mm=1e9 / v,
        model=model,
        reference=ref,
        h_mm=h,
        er=er,
    )


# ------------------------------------------------------------------ vias and pads


def via_l_nh(h_mm, d_mm):
    """Johnson HSDD eq. 7.9 via inductance (nH) of a barrel ``h_mm`` long, ``d_mm`` diameter."""
    if h_mm <= 0:
        return 0.0
    h, d = h_mm / MM_PER_IN, d_mm / MM_PER_IN
    return 5.08 * h * (math.log(4 * h / d) + 1)


def via_c_pf(t_mm, pad_d_mm, antipad_d_mm, er):
    """HSDD p. 258 via pad-to-plane capacitance (pF): 1.41 er T D1/(D2 - D1), inches."""
    if antipad_d_mm <= pad_d_mm:
        raise ValueError("antipad must exceed the via pad")
    return (
        1.41
        * er
        * (t_mm / MM_PER_IN)
        * (pad_d_mm / MM_PER_IN)
        / ((antipad_d_mm - pad_d_mm) / MM_PER_IN)
    )


def via(st, a, b, drill_mm=None, diameter_mm=None):
    """Lumped via between copper layers a and b: dict(l_nh, c_pf, span_mm, ...).

    L is the HSDD inductance of the traversed span. C is the HSDD capacitance of the
    whole barrel (the unused stub included), split C/2 at each end by the deck.
    Uncalibrated: overshoot numbers that depend on it are report-only.
    """
    drill = drill_mm or st["via"]["drill_mm"]
    dia = diameter_mm or st["via"]["diameter_mm"]
    span = via_span_mm(st, a, b)
    total = thickness(st)
    er = sum(x["t_mm"] * x.get("er", 0) for x in st["layers"] if x["kind"] == "dielectric") / max(
        1e-9, sum(x["t_mm"] for x in st["layers"] if x["kind"] == "dielectric")
    )
    antipad = dia + 2 * st.get("antipad_clearance_mm", DEFAULT_ANTIPAD_CLEARANCE_MM)
    return dict(
        l_nh=via_l_nh(span, drill),
        c_pf=via_c_pf(total, dia, antipad, er),
        span_mm=span,
        drill_mm=drill,
        diameter_mm=dia,
        antipad_mm=antipad,
        er=er,
        model="HSDD eq.7.9 (L of traversed span) + HSDD p.258 (C of full barrel); uncalibrated",
    )


def pad_c_pf(st, layer, area_mm2):
    """Parallel-plate capacitance (pF) of a surface land of ``area_mm2`` to the nearest plane."""
    z = z_table(st)
    if layer not in z or area_mm2 <= 0:
        return 0.0
    top, bot = z[layer]
    planes = [p for p in st.get("planes", []) if p in z and p != layer]
    best = None
    for p in planes:
        if z[p][0] >= bot:
            h, er = _dielectric_between(st, bot, z[p][0])
        else:
            h, er = _dielectric_between(st, z[p][1], top)
        if h > 0 and (best is None or h < best[0]):
            best = (h, er)
    if best is None:
        return 0.0
    h, er = best
    return EPS0 * er * (area_mm2 * 1e-6) / (h * 1e-3) * 1e12


def cable_lc(z0_ohm, nvp):
    """Per-metre L (nH/m), C (pF/m) and delay (ns/m) of a TEM line."""
    v = nvp * C0
    return dict(
        l_nh_per_m=z0_ohm / v * 1e9, c_pf_per_m=1.0 / (z0_ohm * v) * 1e12, td_ns_per_m=1e9 / v
    )


MU0 = 4e-7 * math.pi  # H/m


def skin_depth_m(rho_ohm_m, f_hz):
    """Skin depth (m) of a non-magnetic conductor: sqrt(rho / (pi f mu0))."""
    return math.sqrt(rho_ohm_m / (math.pi * f_hz * MU0))


def cable_r_ohm_per_m(loss):
    """Loop series resistance (ohm/m) of a two-wire cable at its reference frequency.

    ``loss``: dict(conductor_d_mm, conductors (2 for signal + return), rho_ohm_m,
    f_ref_mhz, optional spacing_mm). Per conductor, the larger of the DC resistance
    ``rho / (pi d^2 / 4)`` and the skin-effect resistance ``rho / (pi d delta)``
    (Wheeler's thin-skin approximation, valid for d >> delta), times the two-wire
    proximity factor ``(D/d) / sqrt((D/d)^2 - 1)`` when the centre spacing D is given.
    One frequency-flat value: the CMOS receiver draws no DC current, so the (too high)
    low-frequency resistance moves no DC level; it slightly over-attenuates the
    mid band (slower edges, a little less ringing than a true skin-effect line).
    """
    d = float(loss["conductor_d_mm"]) * 1e-3
    rho = float(loss.get("rho_ohm_m", 1.72e-8))
    r_dc = rho / (math.pi * d * d / 4)
    r = r_dc
    if loss.get("f_ref_mhz"):
        delta = skin_depth_m(rho, float(loss["f_ref_mhz"]) * 1e6)
        r_ac = rho / (math.pi * d * delta)
        if loss.get("spacing_mm"):
            ratio = float(loss["spacing_mm"]) / float(loss["conductor_d_mm"])
            if ratio > 1:
                r_ac *= ratio / math.sqrt(ratio * ratio - 1)
        r = max(r_dc, r_ac)
    return r * float(loss.get("conductors", 2))


def _solve(a, b):
    """Gaussian elimination with partial pivoting (small dense systems)."""
    n = len(b)
    m = [list(row) + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        m[c], m[p] = m[p], m[c]
        if abs(m[c][c]) < 1e-300:
            raise ValueError("singular system")
        for r in range(n):
            if r != c:
                f = m[r][c] / m[c][c]
                m[r] = [x - f * y for x, y in zip(m[r], m[c])]
    return [m[i][n] / m[i][i] for i in range(n)]


def skin_ladder(loss, decades=(-2, -1, 0, 1)):
    """Frequency-dependent series resistance of a two-wire cable as an R-L ladder (per metre).

    Returns dict(r_dc, stages=[(R_k, L_k)], fit) for ``Z(f) = r_dc + sum_k (R_k || j w L_k)``:
    each stage adds ``R_k (f/f_k)^2 / (1 + (f/f_k)^2)`` of resistance above its pole
    ``f_k = R_k / (2 pi L_k)``; poles sit one decade apart around ``f_ref``
    (``decades``). The R_k are a non-negative least-squares fit of the skin-effect
    excess ``R_ac(f) - r_dc`` with ``R_ac(f) = R_ac(f_ref) sqrt(f / f_ref)`` (same
    physics as :func:`cable_r_ohm_per_m`, which is the value at ``f_ref``) on
    log-spaced frequencies from f_ref/300 to 10 f_ref. ``fit`` reports the worst
    relative error of the total resistance over f_ref/30 .. 3 f_ref.
    """
    f_ref = float(loss["f_ref_mhz"]) * 1e6
    r_ref = cable_r_ohm_per_m(loss)
    r_dc = cable_r_ohm_per_m(dict(loss, f_ref_mhz=None))
    poles = [f_ref * 10.0**d for d in decades]
    freqs = [f_ref * 10.0 ** (x / 8.0) for x in range(-20, 9)]

    def target(f):
        return max(r_dc, r_ref * math.sqrt(f / f_ref)) - r_dc

    def basis(f, fk):
        u = (f / fk) ** 2
        return u / (1 + u)

    active = list(range(len(poles)))
    while True:
        a = [
            [sum(basis(f, poles[i]) * basis(f, poles[j]) for f in freqs) for j in active]
            for i in active
        ]
        b = [sum(basis(f, poles[i]) * target(f) for f in freqs) for i in active]
        sol = _solve(a, b)
        if all(x >= 0 for x in sol) or len(active) == 1:
            break
        active = [k for k, x in zip(active, sol) if x > 0]
    rk = {k: max(0.0, x) for k, x in zip(active, sol)}
    stages = [(rk[k], rk[k] / (2 * math.pi * poles[k])) for k in sorted(rk) if rk[k] > 0]

    def model(f):
        return r_dc + sum(r * basis(f, r / (2 * math.pi * l)) for r, l in stages)

    worst = max(
        abs(model(f) - (target(f) + r_dc)) / (target(f) + r_dc)
        for f in (f_ref * 10.0 ** (x / 8.0) for x in range(-12, 5))
    )
    return dict(r_dc=r_dc, stages=stages, r_ref=r_ref, f_ref=f_ref, fit=round(worst, 4))
