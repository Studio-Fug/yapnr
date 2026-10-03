"""Quasi-static 2D cross-sections of the macro's lines with yapnr's coupon solver.

`yapnr.rf.coupons.xsec` (scikit-fem P2 energy method on a gmsh mesh, Wheeler's incremental
inductance for the conductor loss) gives Z0, the static εeff and the loss factor g of each
line with its real copper thickness; Kirschning-Jansen [KJ82] then adds the dispersion to
62 GHz. Needs scikit-fem and gmsh (the FEA worker environment) and the yapnr checkout on
PYTHONPATH; the results are written to `results/xsec.json` and read by `dims.py`.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

from . import closedform as cf
from .params import F0, STACK

RECEDE_MM = 0.002  # Wheeler wall recession, as yapnr.rf.coupons.families


def _two_layer_spec(w: float, t: float, gap: Optional[float], recede: float = 0.0):
    """Line on L1 over the L2 window: RO4450F (filling the removed L2 copper) then RO4835,
    referenced to L3 at z = 0."""
    d = recede
    hb = STACK["h_bond"] + STACK["t_l2"]
    h = hb + STACK["h_core"]
    strips = [("S", -w / 2, w / 2)]
    if gap is not None:
        strips += [("G1", -w / 2 - gap - 1.5, -w / 2 - gap), ("G2", w / 2 + gap, w / 2 + gap + 1.5)]
    conds = [(n, a + d, b - d, h + d, h + t - d) for n, a, b in strips]
    slabs = [("bond", -d, hb), ("core", hb, h), ("air", h, h + t + 5.0)]
    blocks = [("air", a, b, h, h + t) for _, a, b in strips] if d > 0 else []
    return dict(width=10.0, slabs=slabs, blocks=blocks, conductors=conds), ["S"]


def solve_line(kind: str, w: float, gap: Optional[float] = None) -> Dict[str, float]:
    from yapnr.rf.coupons import xsec

    t = STACK["t_l1"]
    if kind == "l1_over_l2":
        er = {"sub": STACK["dk_core"]}

        def spec(rec):
            return xsec.outer_spec(w, STACK["h_core"], t, 0.0, gap, False, 1.0, recede=rec)

        h_eff, er_eff_sub = STACK["h_core"], STACK["dk_core"]
    elif kind == "l1_over_window":
        er = {"bond": STACK["dk_bond"], "core": STACK["dk_core"]}

        def spec(rec):
            return _two_layer_spec(w, t, gap, rec)

        h_eff = STACK["h_window"]
        er_eff_sub = (
            STACK["dk_bond"] * (STACK["h_bond"] + STACK["t_l2"])
            + STACK["dk_core"] * STACK["h_core"]
        ) / h_eff
    else:
        raise ValueError(kind)
    s0, sig = spec(0.0)
    m = xsec.build(s0, "fit")
    r = xsec.solve(m, er, sig)
    z0, e0 = xsec.z_eps(float(r["C"][0, 0]), float(r["C0"][0, 0]))
    s1, _ = spec(RECEDE_MM)
    r1 = xsec.solve(xsec.build(s1, "fit"), er, sig, vacuum_only=True)
    g = float(xsec.wheeler_g(r["C0"][0, 0], r1["C0"][0, 0], RECEDE_MM))
    ef = cf.kj_dispersion(e0, w, h_eff, er_eff_sub, F0)
    # dispersion moves Z0 roughly as sqrt(e0/ef) for a quasi-TEM line (first-order bookkeeping)
    k = cf.roughness_factor(STACK["rq_l1_um"], F0)
    df = STACK["df_core"] if kind == "l1_over_l2" else 0.5 * (STACK["df_core"] + STACK["df_bond"])
    ad, ac = cf.line_loss_db_per_mm(z0, ef, er_eff_sub, df, w, F0, k, g_m=g)
    return dict(
        kind=kind,
        w=w,
        gap=gap,
        z0_static=z0,
        eeff_static=e0,
        eeff_62g=ef,
        lambda_g=cf.guided_wavelength(F0, ef),
        g_wheeler_per_m=g,
        k_rough=k,
        alpha_d_db_mm=ad,
        alpha_c_db_mm=ac,
        alpha_db_mm=ad + ac,
        ndof=int(r["ndof"]),
    )


def width_for(
    kind: str, z_target: float, gap: Optional[float], lo: float, hi: float
) -> Tuple[float, Dict]:
    """Bisect the width for z_target (the solver's Z0 falls monotonically with w)."""
    best = None
    for _ in range(14):
        mid = 0.5 * (lo + hi)
        r = solve_line(kind, mid, gap)
        best = r
        if r["z0_static"] > z_target:
            lo = mid
        else:
            hi = mid
        if hi - lo < 0.0005:
            break
    return round(0.5 * (lo + hi), 4), best


def table() -> List[Dict[str, float]]:
    rows = []
    w50_ms, _ = width_for("l1_over_l2", 50.0, None, 0.12, 0.30)
    w50_gc, _ = width_for("l1_over_l2", 50.0, 0.20, 0.12, 0.30)
    w35_ms, _ = width_for("l1_over_l2", math.sqrt(25 * 50), None, 0.25, 0.50)
    w50_win, _ = width_for("l1_over_window", 50.0, None, 0.25, 0.60)
    for kind, w, g, tag in (
        ("l1_over_l2", w50_ms, None, "50 ohm microstrip, 4 mil"),
        ("l1_over_l2", w50_gc, 0.20, "50 ohm GCPW g 0.20, 4 mil"),
        ("l1_over_l2", 0.200, 0.20, "macro GCPW w 0.200 g 0.200"),
        ("l1_over_l2", 0.200, None, "macro microstrip w 0.200"),
        ("l1_over_l2", w35_ms, None, "35.4 ohm lambda/4, 4 mil"),
        ("l1_over_l2", 0.353, None, "macro 35 ohm w 0.353"),
        ("l1_over_window", w50_win, None, "50 ohm microstrip over the L2 window"),
        ("l1_over_window", 0.200, None, "w 0.200 over the L2 window (inset feed)"),
    ):
        r = solve_line(kind, w, g)
        r["what"] = tag
        hj = cf.ms_static(
            w,
            STACK["h_core"] if kind == "l1_over_l2" else STACK["h_window"],
            STACK["t_l1"],
            STACK["dk_core"] if kind == "l1_over_l2" else 3.54,
        )
        r["z0_hj80"] = hj[0]
        r["eeff_hj80"] = hj[1]
        if g is not None:
            gz = cf.gcpw_static(w, g, STACK["h_core"], STACK["dk_core"])
            r["z0_gn87_t0"] = gz[0]
            r["eeff_gn87_t0"] = gz[1]
        rows.append(r)
    return rows
