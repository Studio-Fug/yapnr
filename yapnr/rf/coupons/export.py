"""Outputs of the extraction (design §8.9): `yapnr-stackup-fit/1`, the `rules['stackup']` overlay,
the `yapnr.rf` single-substrate adapter, and a Markdown report."""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import math
import os
from typing import Dict, List

import numpy as np

from yapnr.rf.coupons import SCHEMA_FIT, families, models, stackups
from yapnr.rf.coupons.fit import F_PRODUCT, Data, FitResult, Predictor


def _r(x, n=6):
    return float(f"{x:.{n}g}")


def dc_layers(d: Data) -> Dict[str, dict]:
    """Per-layer copper thickness and etch from the two meander widths alone (closed form):
    R_i = ρ/t (L_i/(w_i − 2e) + 0.56 c_i), two equations in (t, e)."""
    from yapnr.rf.coupons.fit import _meander

    by_layer: Dict[int, List[dict]] = {}
    for row in d.dc:
        by_layer.setdefault(row["layer"], []).append(row)
    out = {}
    for k, rows in sorted(by_layer.items()):
        if len(rows) < 2:
            continue
        a, b = sorted(rows, key=lambda r: r["width_mm"])[:2]
        ma, mb = _meander(d.board, a), _meander(d.board, b)
        rho = stackups.RHO_CU * (1 + stackups.ALPHA_CU * (a["temp_c"] - 20.0))

        def mismatch(e):
            ta = rho / a["r_ohm"] * (ma["length"] / (ma["w"] - 2 * e) + 0.56 * ma["corners"])
            tb = rho / b["r_ohm"] * (mb["length"] / (mb["w"] - 2 * e) + 0.56 * mb["corners"])
            return ta - tb, ta

        lo, hi = -0.05, 0.09
        for _ in range(80):
            mid = 0.5 * (lo + hi)
            if mismatch(lo)[0] * mismatch(mid)[0] <= 0:
                hi = mid
            else:
                lo = mid
        e = 0.5 * (lo + hi)
        t = mismatch(e)[1]
        out[f"L{k}"] = dict(t_um=round(t * 1e6, 3), etch_um=round(e * 1e3, 2))
    return out


def fit_record(res: FitResult, d: Data, p: Predictor, session_dir: str, serial=None) -> dict:
    st = stackups.get(d.stackup)
    data_hash = {}
    for name in sorted(os.listdir(session_dir)):
        if name.endswith((".s2p", ".s1p", ".csv", ".json", ".yaml")) and name != "truth.json":
            with open(os.path.join(session_dir, name), "rb") as fh:
                data_hash[name] = hashlib.sha256(fh.read()).hexdigest()[:16]
    tot = res.sigma_total
    params = {}
    for n in res.names:
        q = stackups.param(n, st)
        params[n] = dict(
            value=_r(res.value[n]),
            sigma=_r(tot[n], 3),
            sigma_stat=_r(res.sigma[n], 3),
            sigma_sys=_r(res.sigma_sys.get(n, 0.0), 3),
            sigma_boot=_r(res.sigma_boot.get(n, 0.0), 3),
            unit=q.unit,
            prior=[q.nominal if n not in res.prior else res.prior[n][0], res.prior[n][1]],
        )
    try:
        from yapnr import __version__ as yv
    except Exception:  # pragma: no cover
        yv = "unknown"
    return {
        "schema": SCHEMA_FIT,
        "stackup": {"id": st.id, "nominal_sha256": st.sha256()},
        "lot": {
            "boards": [serial] if serial else "all in the session",
            "fitted": _dt.date.today().isoformat(),
        },
        "models": {
            "dielectric": "djordjevic-sarkar(f1=1e3,f2=1e12,fref=1e9)",
            "roughness": f"{p.roughness}(radius={stackups.HURAY_RADIUS_UM}um)",
            "etch": "rectangular trace, width lost per edge",
            "mask": "conformal(30/15 um nominal) x mask.scale",
            "lines": "2D quasi-static RLGC surrogate, Wheeler conductor loss, KJ dispersion (L1)",
        },
        "parameters": params,
        "covariance": {"order": res.names, "matrix": [[_r(x, 4) for x in row] for row in res.cov]},
        "derived": {k: [_r(v[0]), _r(v[1], 3)] for k, v in res.derived.items()},
        "checks": {
            "blocks": res.chi2,
            "held_out": res.held_out,
            "warnings": res.warnings,
            "problems": d.problems,
            "dc_layers": dc_layers(d),
            "tdr_z0_ohm": {k: _r(v, 5) for k, v in d.tdr.items()},
        },
        "nuisance": {k: [_r(v, 4), _r(sd, 3)] for k, (v, sd) in res.nuisance.items()},
        "bootstrap": (
            {"draws": res.bootstrap["draws"], "imperfections": res.bootstrap["imperfections"]}
            if res.bootstrap
            else None
        ),
        "verification": d.verify,
        "repeatability": {
            k: {"n": v["n"], "phase_deg_10g": v.get("phase_deg_10g"), "mag_db": v.get("mag_db")}
            for k, v in d.repeat.items()
        },
        "systematic": {k: {n: _r(x, 3) for n, x in v.items()} for k, v in res.systematic.items()},
        "provenance": {
            "yapnr": yv,
            "tables": families.table_path(d.stackup).split(os.sep)[-1],
            "data_sha256": data_hash,
        },
    }


def stackup_overlay(res: FitResult, d: Data) -> dict:
    """The `rules['stackup']` block with a `measured` overlay (FEA design §7.4)."""
    st = stackups.get(d.stackup)
    v = stackups.with_values(st, res.value)
    tot = res.sigma_total
    return {
        "name": st.id,
        "source": st.source,
        "layers": stackups.physical_layers(st, {}),
        "measured": {
            "source": "coupon-fit",
            "schema": SCHEMA_FIT,
            "layers": stackups.physical_layers(st, v),
            "etch_mm": {k: _r(v[k], 4) for k in v if k.endswith(".etch")},
            "roughness": {k: _r(v[k], 4) for k in v if k.endswith(".rough")},
            "mask": {
                "t_over_copper_mm": _r(stackups.MASK_CU_MM * v.get("mask.scale", 1.0), 4),
                "t_over_substrate_mm": _r(stackups.MASK_SUB_MM * v.get("mask.scale", 1.0), 4),
                "er": _r(v.get("mask.dk", 3.8), 4),
                "tand": _r(v.get("mask.df", 0.025), 4),
            },
            "sigma": {k: _r(x, 3) for k, x in tot.items()},
        },
    }


def rf_adapter(res: FitResult, d: Data, p: Predictor) -> dict:
    """Single-substrate stand-in for `yapnr.rf.stackup.Stackup(er, tan_delta, h, f_ref,
    sigma_cu)`: the εr that gives the fitted microstrip (or P) its measured εeff at 5.8 GHz by
    Hammerstad-Jensen + Kirschning-Jansen, the dielectric loss tangent, and σ/K² carrying the
    roughness factor K into the sheet resistance (design §8.9 item 3)."""
    st = stackups.get(d.stackup)
    v = stackups.with_values(st, res.value)
    fam = "M" if "M" in p.tables else "P"
    f = np.array([F_PRODUCT])
    m = p.model(v, f)
    eps = float(m.line(fam).eps_eff[0])
    famd = families.get(fam, d.stackup)
    h = v["pp1.h"]
    w = famd.w - 2 * v["L1.etch"]

    def eps_of(er):
        return _hj_eps(w, h, er) * float(models.kj_ratio(w, h, er, f)[0])

    lo, hi = 1.5, 8.0
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if eps_of(mid) < eps:
            lo = mid
        else:
            hi = mid
    er = 0.5 * (lo + hi)
    k = float(models.huray(f, v.get("L1.rough", 0.0))[0])
    return {
        "family": fam,
        "er": _r(er, 5),
        "tan_delta": _r(v["pp.df"], 4),
        "h_m": _r(h * 1e-3, 5),
        "f_ref_hz": F_PRODUCT,
        "sigma_cu": _r(1 / stackups.RHO_CU / k**2, 5),
        "eps_eff_measured": _r(eps, 5),
        "warning": "; ".join(
            x
            for x in (
                (
                    "equivalent substrate: adequate for lines and resonators, not for coupled"
                    " structures (the mask moves the odd mode more)"
                    if famd.mask
                    else ""
                ),
                (
                    "matched to the coplanar family P with the microstrip formulas (no microstrip"
                    " on this board): approximate"
                    if famd.gap is not None
                    else ""
                ),
            )
            if x
        ),
    }


def _hj_eps(w, h, er):
    u = w / h
    a = (
        1
        + math.log((u**4 + (u / 52) ** 2) / (u**4 + 0.432)) / 49
        + math.log(1 + (u / 18.1) ** 3) / 18.7
    )
    b = 0.564 * ((er - 0.9) / (er + 3)) ** 0.053
    return (er + 1) / 2 + (er - 1) / 2 * (1 + 10 / u) ** (-a * b)


def report_md(rec: dict, overlay: dict, adapter: dict) -> str:
    L = [f"# Coupon fit: {rec['stackup']['id']}", ""]
    L += ["| Parameter | Value | σ (total) | σ stat | σ sys | Prior |", "|---|---|---|---|---|---|"]
    for n, p in rec["parameters"].items():
        L.append(
            f"| {n} | {p['value']:.5g} {p['unit']} | {p['sigma']:.3g} | {p['sigma_stat']:.3g} "
            f"| {p['sigma_sys']:.3g} | {p['prior'][0]:.4g} ± {p['prior'][1]:.3g} |"
        )
    L += ["", "Derived at 5.8 GHz:", "", "| Quantity | Value | σ |", "|---|---|---|"]
    for k, (v, s) in rec["derived"].items():
        L.append(f"| {k} | {v:.5g} | {s:.2g} |")
    ho = rec["checks"]["held_out"]
    if ho:
        L += [
            "",
            "Held-out structures (design §8.7):",
            "",
            "| Stick | Measured (GHz) | Predicted | Rel. error | Pass |",
            "|---|---|---|---|---|",
        ]
        for sid, h in ho.items():
            L.append(
                f"| {sid} {h['kind']} | {h['measured_ghz']} | {h['predicted_ghz']} | {h['rel_err']} "
                f"| {'yes' if h['passed'] else 'NO'} |"
            )
    L += [
        "",
        "Residual blocks (rms in units of the re-estimated σ; n_eff from the AR(1) model):",
        "",
    ]
    L += ["| Block | n | n_eff | rms |", "|---|---|---|---|"]
    for k, b in rec["checks"]["blocks"].items():
        L.append(f"| {k} | {b['n']} | {b['n_eff']} | {b['rms']:.3g} |")
    if rec["checks"]["dc_layers"]:
        L += [
            "",
            "DC meanders alone (per layer):",
            "",
            "| Layer | t (µm) | etch (µm) |",
            "|---|---|---|",
        ]
        for k, x in rec["checks"]["dc_layers"].items():
            L.append(f"| {k} | {x['t_um']} | {x['etch_um']} |")
    for title, items in (
        ("Warnings", rec["checks"]["warnings"]),
        ("Problems", rec["checks"]["problems"]),
    ):
        if items:
            L += ["", f"{title}:", ""] + [f"- {x}" for x in items]
    L += [
        "",
        "yapnr.rf adapter (single substrate):",
        "",
        "```json",
        json.dumps(adapter, indent=2),
        "```",
    ]
    L += [
        "",
        "Stackup overlay (`rules['stackup']`):",
        "",
        "```json",
        json.dumps(overlay, indent=2),
        "```",
        "",
    ]
    return "\n".join(L)


def write_all(
    out_dir: str, res: FitResult, d: Data, p: Predictor, session_dir: str, serial=None
) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    rec = fit_record(res, d, p, session_dir, serial)
    ov = stackup_overlay(res, d)
    ad = rf_adapter(res, d, p)
    rec["rf_adapter"] = ad
    paths = {}
    for name, obj in (("fit.json", rec), ("stackup-overlay.json", ov)):
        paths[name] = os.path.join(out_dir, name)
        with open(paths[name], "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=2)
            fh.write("\n")
    paths["report.md"] = os.path.join(out_dir, "report.md")
    with open(paths["report.md"], "w", encoding="utf-8") as fh:
        fh.write(report_md(rec, ov, ad))
    return paths
