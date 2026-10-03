"""Start dimensions of the conventional structures, from the closed forms and the 2D table.

`compute(p)` returns every dimension the geometry needs, with the predictions to compare
against the full-wave check: patch f0 and bandwidth, inset depth, column directivity and
efficiency, line loss per mm. The 2D table (`results/xsec.json`, written by `python -m rfmacro
xsec`) replaces the static line constants when present; otherwise Hammerstad-Jensen is used and
the record says so.
"""

from __future__ import annotations

import json
import math
import os
from typing import Dict

from . import closedform as cf
from .params import F0, F_HI, F_LO, LAM0, STACK, length_scale

HERE = os.path.dirname(os.path.abspath(__file__))
XSEC = os.path.join(HERE, "..", "results", "xsec.json")


def _xsec_rows():
    if not os.path.exists(XSEC):
        return None
    with open(XSEC) as fh:
        return json.load(fh)


def _line(rows, what: str):
    if rows:
        for r in rows:
            if r["what"] == what:
                return r
    return None


def compute(p: Dict[str, object]) -> Dict[str, object]:
    rows = _xsec_rows()
    h_w = STACK["h_window"]
    # composite permittivity of the window (series capacitors, thickness-weighted 1/εr)
    hb = STACK["h_bond"] + STACK["t_l2"]
    er_w = h_w / (hb / STACK["dk_bond"] + STACK["h_core"] / STACK["dk_core"])
    if not p["windows"]:  # D4 option A: patches on the 4 mil core over solid L2
        h_w, er_w = STACK["h_core"], STACK["dk_core"]
    W = float(p["patch_w"])
    L_nom = cf.patch_length(F0, W, h_w, er_w)
    L = float(p["patch_l"]) if p["patch_l"] is not None else L_nom
    L *= length_scale(p)
    f0 = cf.patch_f0(W, L, h_w, er_w)
    bw = cf.patch_bw(W, L, h_w, er_w, f0)
    ins = cf.inset_depth(W, L, f0)
    inset = float(p["inset"]) if p["inset"] is not None else ins["y0"]
    k_r = cf.roughness_factor(STACK["rq_l1_um"], F0)
    eta = cf.patch_efficiency(bw, 0.5 * (STACK["df_core"] + STACK["df_bond"]), h_w, F0, k_r)
    d_patch = cf.patch_directivity(W, L, h_w, er_w, F0)
    d_col = cf.column_directivity(W, L, h_w, er_w, F0, float(p["spacing"]))

    ms = _line(rows, "macro microstrip w 0.200")
    gc = _line(rows, "macro GCPW w 0.200 g 0.200")
    s35 = _line(rows, "macro 35 ohm w 0.353")
    src = "2D solver (yapnr.rf.coupons.xsec) + KJ82 dispersion" if ms else "HJ80 + KJ82"
    if ms is None:
        z, e = cf.ms_static(float(p["w50"]), STACK["h_core"], STACK["t_l1"], STACK["dk_core"])
        e = cf.kj_dispersion(e, float(p["w50"]), STACK["h_core"], STACK["dk_core"], F0)
        ms = dict(z0_static=z, eeff_62g=e, lambda_g=cf.guided_wavelength(F0, e), alpha_db_mm=None)
        gc = ms
        z, e = cf.ms_static(float(p["w35"]), STACK["h_core"], STACK["t_l1"], STACK["dk_core"])
        e = cf.kj_dispersion(e, float(p["w35"]), STACK["h_core"], STACK["dk_core"], F0)
        s35 = dict(z0_static=z, eeff_62g=e, lambda_g=cf.guided_wavelength(F0, e), alpha_db_mm=None)
    lg50 = ms["lambda_g"]
    lg35 = s35["lambda_g"]
    return dict(
        f0_target_ghz=F0 / 1e9,
        band_ghz=[F_LO / 1e9, F_HI / 1e9],
        lambda0_mm=LAM0,
        window=dict(h_mm=h_w, er_composite=er_w),
        patch=dict(
            w=W,
            l=L,
            l_nominal=L_nom,
            length_scale=length_scale(p),
            f0_ghz=f0 / 1e9,
            bw_vswr2=bw,
            bw_ghz=bw * f0 / 1e9,
            edge_r_ohm=ins["r_edge"],
            g1_s=ins["g1"],
            g12_s=ins["g12"],
            inset=inset,
            inset_closed_form=ins["y0"],
            w_over_l=W / L,
            efficiency=eta,
            directivity_dbi=10 * math.log10(d_patch),
        ),
        column=dict(
            spacing=float(p["spacing"]),
            spacing_lambda0=float(p["spacing"]) / LAM0,
            directivity_dbi=10 * math.log10(d_col),
            # realized gain = directivity x radiation efficiency x feed loss (divider, 2 arms)
            divider_loss_db=None,
        ),
        lines=dict(
            source=src,
            z50_ms=ms["z0_static"],
            z50_gcpw=gc["z0_static"],
            z35=s35["z0_static"],
            lambda_g50=lg50,
            lambda_g35=lg35,
            quarter35=lg35 / 4,
            half50=lg50 / 2,
            alpha50_db_mm=ms.get("alpha_db_mm"),
            alpha50_gcpw_db_mm=gc.get("alpha_db_mm"),
        ),
    )


def predictions(d: Dict[str, object], divider_len_mm: float) -> Dict[str, float]:
    """Closed-form column predictions to set against the full-wave run."""
    a = d["lines"]["alpha50_db_mm"] or 0.12
    loss = a * divider_len_mm
    g = d["column"]["directivity_dbi"] + 10 * math.log10(d["patch"]["efficiency"]) - loss
    return dict(
        f0_ghz=d["patch"]["f0_ghz"],
        bw_vswr2_pct=100 * d["patch"]["bw_vswr2"],
        rl10_bw_pct=100 * d["patch"]["bw_vswr2"] * 0.62,  # VSWR 1.92 ~ RL 10 dB: same order
        column_directivity_dbi=d["column"]["directivity_dbi"],
        efficiency_pct=100 * d["patch"]["efficiency"],
        feed_loss_db=loss,
        realized_gain_dbi=g,
    )
