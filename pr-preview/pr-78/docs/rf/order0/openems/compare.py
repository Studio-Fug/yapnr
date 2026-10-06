#!/usr/bin/env python3
"""openEMS (43 µm copper, nominal FR408HR) against yapnr.rf (zero-thickness sheet on the
thickness-equivalent substrate) and the coupon model: the pre-registered comparison of
D-O0-13a. Run with yapnr on the path, at the repository root, after `post.py`:

    python3 docs/rf/order0/openems/compare.py --em DIR --out docs/rf/order0/predictions/openems

DIR holds post.py's Touchstone files (<model>-r05.s3p, <model>-r025.s3p, line*-r*.s2p,
a01.s2p, a04.s2p). Written to --out:

- the lines: openEMS's εeff, α (copper lossless: the dielectric's and radiation) and Zc of the
  0.40 mm line at each mesh, against the coupon model (lines.csv, FR408HR) and yapnr.rf's run 0b
  line on M-eq;
- D1 and R1: openEMS at each mesh, raw and loss-corrected with the same method as yapnr.rf's
  predictions (`yapnr.rf.order0.predict`; Δα and the ratio against openEMS's own line), and the
  differences from yapnr.rf's (finer grid for D1, refine 2 for R1) in the terms of the "agrees
  with prediction" criteria: max |ΔS11| (vector), the in-band |S11| minima, |S21| and the
  |S21| − |S31| imbalance;
- A01 and A04: the launch-to-launch thru and line as tier-1 data would show them (coax ports at
  the flange), and A04's line γ by the thru-line method (A04 against A01), against the coupon
  model;
- comparison.json with every number, and the corrected Touchstone files.
"""

from __future__ import annotations

import argparse
import json
import math
import os

import numpy as np

from yapnr.rf.order0 import demos, predict

C0 = 299792458.0
ROOT = predict.ROOT
PRED = predict.PRED
BAND = (4.25e9, 5.75e9)


def read(path):
    return demos.read_s(path)


def on(f_to, f, x):
    """Complex or real arrays interpolated onto f_to (per element of the trailing dims)."""
    x = np.asarray(x)
    shape = x.shape[1:]
    flat = x.reshape(x.shape[0], -1)
    out = np.empty((f_to.size, flat.shape[1]), dtype=x.dtype)
    for k in range(flat.shape[1]):
        if np.iscomplexobj(flat):
            out[:, k] = np.interp(f_to, f, flat[:, k].real) + 1j * np.interp(
                f_to, f, flat[:, k].imag
            )
        else:
            out[:, k] = np.interp(f_to, f, flat[:, k])
    return out.reshape((f_to.size,) + shape)


def line_params(short, long, dl_mm):
    """εeff (from the phase of S21 between two lengths), α (dB/mm) and the frequencies."""
    f1, s1 = read(short)
    f2, s2 = read(long)
    s2 = on(f1, f2, s2)
    alpha = (demos.line_loss_db(s2) - demos.line_loss_db(s1)) / dl_mm
    dphi = -np.unwrap(np.angle(s2[:, 1, 0])) + np.unwrap(np.angle(s1[:, 1, 0]))
    beta = dphi / (dl_mm * 1e-3)
    eps = (beta * C0 / (2 * math.pi * f1)) ** 2
    return f1, eps, alpha


def coupon_line(f):
    import csv

    path = os.path.join(PRED, "coupons", "O0-M", "fr408hr", "lines.csv")
    with open(path, encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    fg = np.array([float(r["f_ghz"]) for r in rows]) * 1e9
    keys = ("M.eps_eff", "M.alpha_db_per_cm", "M.zc_re")
    get = {k: np.array([float(r[k]) for r in rows]) for k in keys}
    return {k: np.interp(f, fg, v) for k, v in get.items()}


def at(f, x, f0=5e9):
    return float(np.interp(f0, f, x))


def minima(f, s11):
    return predict.s11_minima(f, s11)


def worst(f, s):
    return predict.worst(f, s)


def diff(f_a, s_a, f_b, s_b):
    """The criteria's terms of b against a, over the band, on a's frequencies."""
    sb = on(f_a, f_b, s_b)
    m = (f_a >= BAND[0] - 1) & (f_a <= BAND[1] + 1)
    d11 = np.abs(s_a[m, 0, 0] - sb[m, 0, 0])
    db = lambda x: 20 * np.log10(np.abs(x))  # noqa: E731
    out = dict(
        max_abs_ds11=round(float(np.max(d11)), 4),
        max_abs_ds21_db=round(float(np.max(np.abs(db(s_a[m, 1, 0]) - db(sb[m, 1, 0])))), 4),
        s11_minima_ghz=[minima(f_a, s_a[:, 0, 0]), minima(f_a, sb[:, 0, 0])],
    )
    if s_a.shape[-1] > 2:
        imb_a = db(s_a[m, 1, 0]) - db(s_a[m, 2, 0])
        imb_b = db(sb[m, 1, 0]) - db(sb[m, 2, 0])
        out["max_imbalance_db"] = [round(float(np.max(np.abs(imb_a))), 4),
                                   round(float(np.max(np.abs(imb_b))), 4)]  # fmt: skip
    return out


def main(argv=None) -> int:
    from yapnr.rf.export.touchstone import write_touchstone

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--em", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    os.makedirs(a.out, exist_ok=True)
    res: dict = {"schema": "yapnr-order0-openems-compare/1", "lines": {}, "demos": {},
                 "sticks": {}}  # fmt: skip
    meshes = [m for m in ("r05", "r025") if os.path.isfile(os.path.join(a.em, f"line10-{m}.s2p"))]
    em_line = {}
    for mesh in meshes:
        f, eps, alpha = line_params(os.path.join(a.em, f"line10-{mesh}.s2p"),
                                    os.path.join(a.em, f"line30-{mesh}.s2p"), 20.0)  # fmt: skip
        summ = json.load(open(os.path.join(a.em, "summary.json"), encoding="utf-8"))
        zc = summ.get(f"line30-{mesh}", {}).get("z_at_5ghz_ohm", {}).get("1")
        em_line[mesh] = (f, alpha)
        cp = coupon_line(f)
        res["lines"][mesh] = dict(
            eps_eff_5ghz=round(at(f, eps), 4),
            alpha_db_per_cm_5ghz=round(at(f, alpha) * 10, 5),
            zc_ohm_5ghz=zc,
            coupon_eps_eff_5ghz=round(at(f, cp["M.eps_eff"]), 4),
            coupon_alpha_db_per_cm_5ghz=round(at(f, cp["M.alpha_db_per_cm"]), 5),
            coupon_zc_ohm_5ghz=round(at(f, cp["M.zc_re"]), 3),
        )
    # yapnr.rf's own line on M-eq (run 0b)
    f, eps, alpha = line_params(os.path.join(PRED, "run0b", "line-m-w040-l10.s2p"),
                                os.path.join(PRED, "run0b", "line-m-w040-l30.s2p"), 20.0)  # fmt: skip
    res["lines"]["yapnr-m-eq"] = dict(eps_eff_5ghz=round(at(f, eps), 4),
                                      alpha_db_per_cm_5ghz=round(at(f, alpha) * 10, 5),
                                      zc_ohm_5ghz=50.788)  # fmt: skip
    # D1 and R1
    ref = {
        "d1": ("D1/runs/d1-star/finer.s3p", "d1-m-eq-finer-loss-corrected.s3p", "ratio"),
        "r1": ("run0b/r1-m-eq.s3p", "r1-m-eq-loss-corrected.s3p", "r1"),
    }
    corr_dir = os.path.join(PRED, "corrected")
    for model, (raw_rel, corr_name, kind) in ref.items():
        fy, sy = read(os.path.join(PRED, raw_rel))
        cpath = os.path.join(corr_dir, corr_name)
        fyc, syc = read(cpath) if os.path.isfile(cpath) else (fy, sy)
        res["demos"][model] = {"yapnr": dict(raw=worst(fy, sy), corrected=worst(fyc, syc),
                                             s11_minima_ghz=minima(fy, sy[:, 0, 0]))}  # fmt: skip
        for mesh in meshes:
            p = os.path.join(a.em, f"{model}-{mesh}.s3p")
            if not os.path.isfile(p):
                continue
            fe, se = read(p)
            fl, al = em_line[mesh]
            cp = coupon_line(fl)
            a_cpn = cp["M.alpha_db_per_cm"] / 10
            model_loss = dict(f=fl, lines={"line": dict(d_alpha=a_cpn - al, ratio=a_cpn / al)})
            if kind == "r1":  # openEMS has the 0.40 mm line only: its Δα along the whole path
                model_loss["lines"]["arm"] = model_loss["lines"]["line"]
            c, how = predict.correction(kind, fe, se, model_loss)
            sec = predict.apply(se, c)
            out = os.path.join(a.out, f"{model}-openems-{mesh}-loss-corrected.s3p")
            write_touchstone(
                out,
                fe,
                sec,
                comments=[
                    f"Order 0 openEMS {model} ({mesh}), 43 um PEC copper, nominal FR408HR;"
                    f" loss-corrected ({how}) against openEMS's own 0.40 mm line",
                    "50 ohm, e^(+jwt)",
                ],
            )
            res["demos"][model][f"openems-{mesh}"] = dict(
                raw=worst(fe, se),
                corrected=worst(fe, sec),
                s11_minima_ghz=minima(fe, se[:, 0, 0]),
                vs_yapnr_corrected=diff(fyc, syc, fe, sec),
            )
    # the sticks
    for sid in ("a01", "a04"):
        p = os.path.join(a.em, f"{sid}.s2p")
        if not os.path.isfile(p):
            continue
        fs, ss = read(p)
        db = 20 * np.log10(np.abs(ss))
        band6 = fs <= 6.0e9 + 1
        res["sticks"][sid] = dict(
            s11_max_db_to_6ghz=round(float(np.max(db[band6, 0, 0])), 2),
            s11_db_at=[[g, round(float(np.interp(g * 1e9, fs, db[:, 0, 0])), 2)]
                       for g in (2, 3, 4, 5, 5.8, 6)],  # fmt: skip
            s21_db_at=[[g, round(float(np.interp(g * 1e9, fs, db[:, 1, 0])), 3)]
                       for g in (2, 3, 4, 5, 5.8, 6)],  # fmt: skip
        )
    if os.path.isfile(os.path.join(a.em, "a01.s2p")) and os.path.isfile(
        os.path.join(a.em, "a04.s2p")
    ):
        f1, t = read(os.path.join(a.em, "a01.s2p"))
        f2, l4 = read(os.path.join(a.em, "a04.s2p"))
        l4 = on(f1, f2, l4)

        def to_t(s):
            t_ = np.zeros_like(s)
            s11, s12, s21, s22 = s[:, 0, 0], s[:, 0, 1], s[:, 1, 0], s[:, 1, 1]
            t_[:, 0, 0] = -(s11 * s22 - s12 * s21) / s21
            t_[:, 0, 1] = s11 / s21
            t_[:, 1, 0] = -s22 / s21
            t_[:, 1, 1] = 1 / s21
            return t_

        m = to_t(l4) @ np.linalg.inv(to_t(t))
        ev = np.linalg.eigvals(m)
        # the eigenvalue e^{-γ ΔL} with |.| < 1 (forward wave decays)
        e = np.where(np.abs(ev[:, 0]) < np.abs(ev[:, 1]), ev[:, 0], ev[:, 1])
        dl = 30.0e-3
        gam = -np.log(e) / dl
        alpha = np.real(gam) * 20 * np.log10(math.e) / 100  # dB/cm
        beta = np.unwrap(np.imag(gam) * dl) / dl
        beta = np.abs(beta)
        # resolve the branch: βΔL near the coupon model's
        cp = coupon_line(f1)
        b_cp = 2 * math.pi * f1 / C0 * np.sqrt(cp["M.eps_eff"])
        k = np.round((b_cp - beta) * dl / (2 * math.pi))
        beta = beta + 2 * math.pi * k / dl
        eps = (beta * C0 / (2 * math.pi * f1)) ** 2
        res["sticks"]["a04_vs_a01_line"] = dict(
            method="thru-line: eigenvalues of T(A04) T(A01)^-1 (the launches cancel)",
            eps_eff=[
                [g, round(at(f1, eps, g * 1e9), 4), round(at(f1, cp["M.eps_eff"], g * 1e9), 4)]
                for g in (2, 3, 4, 5, 5.8)
            ],  # fmt: skip
            alpha_db_per_cm=[
                [
                    g,
                    round(at(f1, alpha, g * 1e9), 4),
                    round(at(f1, cp["M.alpha_db_per_cm"], g * 1e9), 4),
                ]
                for g in (2, 3, 4, 5, 5.8)
            ],  # fmt: skip
            note="openEMS copper is lossless: its α is the dielectric's (and radiation); the"
            " coupon model's includes rough copper and the ground",
        )
    with open(os.path.join(a.out, "comparison.json"), "w", encoding="utf-8") as fh:
        json.dump(res, fh, indent=1)
        fh.write("\n")
    print(json.dumps(res, indent=1)[:6000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
