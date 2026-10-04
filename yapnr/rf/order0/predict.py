"""Order 0's pre-registered predictions: the loss-corrected demo and reference files, and the
held-out resonators' notches in our FDTD (order0-design §7 items 2 and 5).

The solver's loss is corrected per substrate with that substrate's own straight lines (run 0b's
method, `demos`): Δα(f) = α_coupon − α_solver of the 0.40 mm (and 0.70 mm) line on region M, of
the 3.0 mm line on region W, the coupon model on the board the substrate stands for (FR408HR for
*-eq and *-nom, EM528 for *-eq-em528).

- R1 and R1t: −Σ Δα·L along their path (R1: the 0.70 mm arm's Δα over the arm, the 0.40 mm
  line's over half the output line; R1t: the 3.0 mm line's Δα over both, region W having no
  5.0 mm line run);
- D1 and D2 (any geometry): the dissipated fraction scaled by α_coupon/α_solver of the port
  line (`demos.ratio_correction`).

Every corrected file scales |S21|, |S31| (and S12, S13) only, phases kept; reflections and the
output ports' own terms are left as simulated (a stated simplification: the correction is a
fraction of a dB on transmission and below 0.01 in |S11|).

    python -m yapnr.rf.order0 correct --out docs/rf/order0/predictions/corrected
"""

from __future__ import annotations

import json
import os
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np

from yapnr.rf.order0 import demos

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
PRED = os.path.join(ROOT, "docs", "rf", "order0", "predictions")
STACKUP = {"": "OSHPARK-4L-FR408HR", "-em528": "OSHPARK-4L-EM528"}
BAND = (demos.BAND[0] * 1e9, demos.BAND[1] * 1e9)


def stackup_of(sub: str) -> str:
    return STACKUP["-em528" if sub.endswith("-em528") else ""]


def line_ids(sub: str) -> Dict[str, Tuple[str, str, float, str]]:
    """{key: (short id, long id, ΔL mm, coupon family)} of a substrate's loss lines."""
    tail = "" if sub in ("M-eq", "W-eq") else "-" + sub.lower()
    if sub.startswith("M"):
        return {
            "line": (f"line-m-w040-l10{tail}", f"line-m-w040-l30{tail}", 20.0, "M"),
            "arm": (f"line-m-w070-l10{tail}", f"line-m-w070-l30{tail}", 20.0, "M1.4"),
        }
    return {"line": (f"line-w-w300-l20{tail}", f"line-w-w300-l60{tail}", 40.0, "W")}


def find_line(lid: str, roots: Iterable[str]) -> str:
    """A line's Touchstone: `<root>/<id>.s2p` (run 0b's layout) or `<root>/<id>/coarse_dense.s2p`."""
    for r in roots:
        for p in (os.path.join(r, lid + ".s2p"), os.path.join(r, lid, "coarse_dense.s2p")):
            if os.path.isfile(p):
                return p
    raise FileNotFoundError(f"loss line {lid} under {list(roots)}")


def line_roots() -> List[str]:
    return [
        os.path.join(PRED, "run0b"),
        os.path.join(PRED, "references"),
        os.path.join(PRED, "lines"),
    ]


def loss_model(sub: str, roots: Optional[Iterable[str]] = None) -> dict:
    """f (Hz) and per line key Δα (dB/mm) and the ratio α_coupon/α_solver, with their values
    at 5 GHz."""
    roots = list(roots or line_roots())
    out: dict = {"substrate": sub, "stackup": stackup_of(sub), "lines": {}}
    f_ref = None
    for key, (short, long, dl, fam) in line_ids(sub).items():
        f, a_sol = demos.solver_alpha(find_line(short, roots), find_line(long, roots), dl)
        a_cpn = demos.coupon_alpha(f, fam, stackup_of(sub))
        if f_ref is None:
            f_ref = f
        elif f.shape != f_ref.shape or np.max(np.abs(f - f_ref)) > 1.0:
            raise ValueError(f"{sub}: the loss lines' frequencies differ")
        at5 = int(np.argmin(np.abs(f - 5e9)))
        out["lines"][key] = dict(
            family=fam,
            ids=[short, long],
            d_alpha=a_cpn - a_sol,
            ratio=a_cpn / a_sol,
            solver_db_per_cm_5ghz=round(float(a_sol[at5]) * 10, 5),
            coupon_db_per_cm_5ghz=round(float(a_cpn[at5]) * 10, 5),
        )
    out["f"] = f_ref
    return out


def _on(f, model, key, what):
    return np.interp(f, model["f"], model["lines"][key][what])


def correction(kind: str, f: np.ndarray, s: np.ndarray, model: dict) -> Tuple[np.ndarray, str]:
    """dB to add to |S21| and |S31| of a prediction: kind "r1", "r1t" (path) or "ratio"."""
    if kind == "r1":
        path = demos.r1_path("M")
        d = {k: _on(f, model, k, "d_alpha") for k in ("arm", "line")}
        return demos.path_correction(f, d, path), f"path {path}"
    if kind == "r1t":
        path = [("line", L) for _, L in demos.r1_path("W")]
        d = {"line": _on(f, model, "line", "d_alpha")}
        return demos.path_correction(f, d, path), f"path {path} (3.0 mm line's Δα)"
    ratio = _on(f, model, "line", "ratio")
    return demos.ratio_correction(s, ratio), "ratio (port line)"


def apply(s: np.ndarray, corr: np.ndarray) -> np.ndarray:
    g = 10 ** (corr / 20)
    out = np.array(s, dtype=complex)
    for i in range(1, s.shape[-1]):
        out[:, i, 0] *= g
        out[:, 0, i] *= g
    return out


def worst(f: np.ndarray, s: np.ndarray) -> dict:
    band = (f >= BAND[0] - 1) & (f <= BAND[1] + 1)
    db = 20 * np.log10(np.maximum(np.abs(s), 1e-300))
    out = dict(s11_max_db=round(float(np.max(db[band, 0, 0])), 3))
    if s.shape[-1] > 1:
        out["s21_min_db"] = round(float(np.min(db[band, 1, 0])), 4)
    if s.shape[-1] > 2:
        out["s31_min_db"] = round(float(np.min(db[band, 2, 0])), 4)
    return out


# (name, source file relative to predictions/, substrate, kind)
ITEMS: List[Tuple[str, str, str, str]] = [
    ("d1-m-eq-coarse", "D1/runs/d1-star/coarse_dense.s3p", "M-eq", "ratio"),
    ("d1-m-eq-fine", "D1/runs/d1-star/fine.s3p", "M-eq", "ratio"),
    ("d1-m-eq-finer", "D1/runs/d1-star/finer.s3p", "M-eq", "ratio"),
    ("d1-m-eq-wide-coarse", "D1/variants/d1-star-m-eq/coarse_dense.s3p", "M-eq", "ratio"),
    ("d1-m-eq-wide-fine", "D1/variants/d1-star-m-eq/fine.s3p", "M-eq", "ratio"),
    ("d1-m-eq-wide-finer", "D1/variants/d1-star-m-eq/finer.s3p", "M-eq", "ratio"),
    ("d1-m-nom-coarse", "D1/variants/d1-star-m-nom/coarse_dense.s3p", "M-nom", "ratio"),
    ("d1-m-nom-fine", "D1/variants/d1-star-m-nom/fine.s3p", "M-nom", "ratio"),
    ("d1-m-nom-finer", "D1/variants/d1-star-m-nom/finer.s3p", "M-nom", "ratio"),
    ("d1-m-eq-em528-coarse", "D1/variants/d1-star-m-eq-em528/coarse_dense.s3p", "M-eq-em528",
     "ratio"),
    ("d1-m-eq-em528-fine", "D1/variants/d1-star-m-eq-em528/fine.s3p", "M-eq-em528", "ratio"),
    ("d1-m-eq-em528-finer", "D1/variants/d1-star-m-eq-em528/finer.s3p", "M-eq-em528", "ratio"),
    ("r1-m-eq", "run0b/r1-m-eq.s3p", "M-eq", "r1"),
    ("r1-m-nom", "references/r1-m-nom/coarse_dense.s3p", "M-nom", "r1"),
    ("r1-m-eq-em528", "references/r1-m-eq-em528/coarse_dense.s3p", "M-eq-em528", "r1"),
    ("r1t-w-eq", "references/r1t-w-eq/coarse_dense.s3p", "W-eq", "r1t"),
    ("r1t-w-nom", "references/r1t-w-nom/coarse_dense.s3p", "W-nom", "r1t"),
    ("r1t-w-eq-em528", "references/r1t-w-eq-em528/coarse_dense.s3p", "W-eq-em528", "r1t"),
    ("d2-star-sched-w-eq-coarse", "D2/runs/d2-star-sched/coarse_dense.s3p", "W-eq", "ratio"),
    ("d2-star-sched-w-eq-fine", "D2/runs/d2-star-sched/fine.s3p", "W-eq", "ratio"),
    ("d2-star-sched-w-eq-finer", "D2/runs/d2-star-sched/finer.s3p", "W-eq", "ratio"),
    ("d2-star-sched-w-nom-coarse", "D2/variants/d2-star-sched-w-nom/coarse_dense.s3p", "W-nom",
     "ratio"),
    ("d2-star-sched-w-nom-fine", "D2/variants/d2-star-sched-w-nom/fine.s3p", "W-nom", "ratio"),
    ("d2-star-sched-w-nom-finer", "D2/variants/d2-star-sched-w-nom/finer.s3p", "W-nom", "ratio"),
    ("d2-star-sched-w-eq-em528-coarse", "D2/variants/d2-star-sched-w-eq-em528/coarse_dense.s3p",
     "W-eq-em528", "ratio"),
    ("d2-star-sched-w-eq-em528-fine", "D2/variants/d2-star-sched-w-eq-em528/fine.s3p",
     "W-eq-em528", "ratio"),
    ("d2-star-sched-w-eq-em528-finer", "D2/variants/d2-star-sched-w-eq-em528/finer.s3p",
     "W-eq-em528", "ratio"),
]  # fmt: skip


def correct_all(out_dir: str, items=ITEMS, pred: str = PRED, roots=None) -> dict:
    """Write `<name>-loss-corrected.s3p` for every item whose source exists, and summary.json
    (raw and corrected worst cases over 4.25-5.75 GHz, the Δα and ratio at 5 GHz)."""
    from yapnr.rf.export.touchstone import write_touchstone

    os.makedirs(out_dir, exist_ok=True)
    models: Dict[str, dict] = {}
    summary: dict = {
        "schema": "yapnr-order0-corrected/1",
        "band_ghz": list(demos.BAND),
        "substrates": {},
        "items": {},
    }
    for name, rel, sub, kind in items:
        src = os.path.join(pred, rel)
        if not os.path.isfile(src):
            summary["items"][name] = dict(source=rel, missing=True)
            continue
        if sub not in models:
            try:
                models[sub] = loss_model(sub, roots)
            except FileNotFoundError as e:
                models[sub] = None
                summary["substrates"][sub] = dict(missing=str(e))
        if models[sub] is None:
            summary["items"][name] = dict(source=rel, substrate=sub, lines_missing=True)
            continue
        if sub not in summary["substrates"]:
            summary["substrates"][sub] = {
                k: {
                    kk: v[kk]
                    for kk in ("family", "ids", "solver_db_per_cm_5ghz", "coupon_db_per_cm_5ghz")
                }
                for k, v in models[sub]["lines"].items()
            }
        f, s = demos.read_s(src)
        corr, how = correction(kind, f, s, models[sub])
        sc = apply(s, corr)
        path = os.path.join(out_dir, f"{name}-loss-corrected.s{s.shape[-1]}p")
        write_touchstone(
            path,
            f,
            sc,
            comments=[
                f"yapnr Order 0 prediction {name}: predictions/{rel}, loss-corrected",
                f"substrate {sub}; coupon model {stackup_of(sub)}; correction: {how}",
                "|S21|, |S31| (and S12, S13) scaled, phases kept; 50 ohm, e^(+jwt)",
            ],
        )
        band = (f >= BAND[0] - 1) & (f <= BAND[1] + 1)
        summary["items"][name] = dict(
            source=rel,
            substrate=sub,
            method=how,
            file=os.path.basename(path),
            correction_db_band=[
                round(float(np.min(corr[band])), 4),
                round(float(np.max(corr[band])), 4),
            ],
            raw=worst(f, s),
            corrected=worst(f, sc),
        )
    from yapnr.rf.coupons import jsonfmt

    jsonfmt.dump(summary, os.path.join(out_dir, "summary.json"))
    return summary


# --- the resonators' notches -------------------------------------------------------------------


def notch(f: np.ndarray, s21: np.ndarray, near: float, span: float = 0.25e9) -> float:
    """The |S21| minimum near `near` (Hz), refined by a parabola through |S21|² at the three
    samples around the lowest one (a notch's power is quadratic about its minimum)."""
    m = (f >= near - span) & (f <= near + span)
    idx = np.flatnonzero(m)
    p = np.abs(s21[idx]) ** 2
    k = int(np.argmin(p))
    if 0 < k < idx.size - 1:
        y0, y1, y2 = p[k - 1], p[k], p[k + 1]
        den = y0 - 2 * y1 + y2
        off = 0.5 * (y0 - y2) / den if den > 0 else 0.0
        step = f[idx[k + 1]] - f[idx[k]]
        return float(f[idx[k]] + off * step)
    return float(f[idx[k]])


def resonators(fetched: Dict[str, str]) -> dict:
    """The FDTD notches of A11 (n = 1 and 3) and A12 against the coupon model's scalars:
    `fetched` maps a run id (a11-ring-m-eq...) to its coarse_dense.s2p."""
    out: dict = {"schema": "yapnr-order0-resonators/1", "runs": {}}
    scal = {}
    for sub, board in (("m-eq", "fr408hr"), ("m-eq-em528", "em528")):
        p = os.path.join(PRED, "coupons", "O0-M", board, "scalars.json")
        with open(p, encoding="utf-8") as fh:
            scal[sub] = json.load(fh)["notches_ghz"]
    for rid, path in sorted(fetched.items()):
        f, s = demos.read_s(path)
        sub = rid.split("-", 2)[2]
        ref = scal[sub]["A11" if rid.startswith("a11") else "A12"]
        got = [notch(f, s[:, 1, 0], g * 1e9) / 1e9 for g in ref]
        out["runs"][rid] = dict(
            fdtd_ghz=[round(v, 5) for v in got],
            coupon_model_ghz=ref,
            difference_pct=[round(100 * (a / b - 1), 3) for a, b in zip(got, ref)],
        )
    return out
