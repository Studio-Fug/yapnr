"""Write Order 0's RF inputs (`demos`): specs, criteria, forward-run directories and the
`tools/exp/rf_stage_plan.py` job files of run 0b and of the compute stage; and read run 0b back.

    python -m yapnr.rf.order0 write --out DIR [--s21 -3.4] [--offset 0.10]
    python -m yapnr.rf.order0 loss --fetched DIR [--out FILE]

`write` lays out DIR as::

    specs/      d1-<formulation>.json, d2-<formulation>.json (yapnr-rf-spec/1)
    criteria/   d1.json, d2.json, ref-m.json, ref-w.json, line.json (rf_job --criteria)
    runs/       forward-run directories: r1-<substrate>, r1t-<substrate>, line-*
    run0b.toml  the R1 forward and the loss lines (C4D, one family for the comparison)
    compute-m.toml, compute-w.toml   D1's and D2's formulations and the references' predictions
    manifest.json

`--s21` is the |S21| criterion of D-O0-11 (on the loss-corrected prediction) and `--offset`
R1's loss correction from run 0b: the optimizer's requirement is their sum on the raw solver.
"""

from __future__ import annotations

import json
import os
from typing import Dict, List, Optional

import numpy as np

from yapnr.rf.order0 import demos

REF_SUBSTRATES = {"M": ("M-eq", "M-nom", "M-eq-em528"), "W": ("W-eq", "W-nom", "W-eq-em528")}
LINES = (("M", 0.40, 10.0), ("M", 0.40, 30.0), ("M", 0.70, 10.0), ("M", 0.70, 30.0))
# Resources of one RF task: 16 threads of the native kernel on 8 cores (a c4d-/c4-highcpu-16;
# memory-bound beyond 8 threads, PR #30's benchmark), with room for the refine-3 grid.
# An optimization fits one 3-hour attempt (about 1.5-2 h: 70 iterations at 16-75 s, then the
# export and three validation grids); a resumable run goes on in the next attempt otherwise.
TASK = dict(threads=16, cpus=8, memory_gb=24, disk_gb=10, max_wall_s=10800)
# A loss line (about 2 M cells, under 2 GB) or the diagnostic: 8 threads on 4 cores, two to a VM
# (a boot disk holds two tasks of 4 GB of disk).
SMALL = dict(threads=8, cpus=4, memory_gb=6, disk_gb=4)
# Region W's grids are small (D2 about 0.2 M cells at the optimization grid, 3 M at refine 3):
# 8 threads on 4 cores, two to a VM; D2 ends well within 2 hours.
W_TASK = dict(threads=8, cpus=4, memory_gb=8, disk_gb=4, max_wall_s=7200)
# One machine family per comparison: region M's track (R1, the loss lines, D1 and R1's other
# substrates) on C4 in northamerica-northeast1, region W's (D2, R1t) on C4D in us-west4 (on
# 2026-10-04 another track's 250-task campaign held us-west4's 64 Spot vCPUs for hours).
SHAPES = {"M": "c4-highcpu-16", "W": "c4d-highcpu-16"}
# A forward run (one grid, every port excited): R1 on the 0.05 mm grid is about 4 M cells and
# 0.1 M steps per excitation, 5-10 minutes each at 16 threads (estimated from PR #30's 0.8 ns per
# cell-step); the limit keeps a stuck task's cost, and the submission's ceiling, small.
FORWARD_WALL_S = 4800


def _dump(path: str, obj) -> None:
    from yapnr.rf.coupons import jsonfmt

    os.makedirs(os.path.dirname(path), exist_ok=True)
    jsonfmt.dump(obj, path)


def _toml(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml(v) for v in value) + "]"
    raise TypeError(value)


def jobs_toml(name: str, comment: str, shape: str, jobs: List[dict]) -> str:
    lines = [f"# {line}" for line in comment.splitlines()]
    lines += [f"name = {_toml(name)}", 'image = "edge"', "", "[defaults]"]
    lines += [f"{k} = {_toml(v)}" for k, v in TASK.items()]
    lines += ["require_native = true", "", "[placement]", f"shape = {_toml(shape)}"]
    for job in jobs:
        lines += ["", "[[jobs]]"] + [f"{k} = {_toml(v)}" for k, v in job.items()]
    return "\n".join(lines) + "\n"


def write(out: str, s21: float = -3.4, offset: float = 0.10, data: Optional[dict] = None) -> dict:
    data = data or demos.eq_data()
    target = round(s21 + offset, 4)  # the raw solver's |S21| requirement
    manifest: Dict[str, object] = {
        "schema": "yapnr-order0-rf/1",
        "s21_criterion_db": s21,
        "loss_offset_db": offset,
        "s21_optimizer_db": target,
        "band_ghz": list(demos.BAND),
        "selection": demos.SELECTION,
        "formulations": {k: v[1] for k, v in demos.FORMULATIONS.items()},
        "substrates": {},
        "specs": {},
        "runs": {},
    }
    for region in ("M", "W"):
        for key in REF_SUBSTRATES[region]:
            st = demos.stackup(key, data)
            manifest["substrates"][key] = dict(
                er=st.er, tan_delta=st.tan_delta, h_mm=st.h_mm, f_ref_ghz=st.f_ref_ghz
            )
    # D1 and D2: every formulation on the equivalent substrate
    for region, demo in (("M", "d1"), ("W", "d2")):
        for form in demos.FORMULATIONS:
            spec = demos.demo_spec(region, form, s21_min=target, data=data)
            path = os.path.join(out, "specs", f"{demo}-{form}.json")
            _dump(path, spec.to_dict())
            manifest["specs"][f"{demo}-{form}"] = dict(name=spec.name, sha256=spec.sha256())
        _dump(os.path.join(out, "criteria", f"{demo}.json"), demos.criteria(target))
    # the references, judged as the demos; the loss lines
    for region in ("M", "W"):
        _dump(
            os.path.join(out, "criteria", f"ref-{region.lower()}.json"),
            demos.criteria(target, dense=((3.0, 7.0, 161),)),
        )
        for key in REF_SUBSTRATES[region]:
            spec, rects = demos.reference_spec(region, key, s21_min=target, data=data)
            rid = ("r1-" if region == "M" else "r1t-") + key.lower()
            demos.forward_dir(
                spec,
                rects,
                os.path.join(out, "runs", rid),
                dict(what=f"{'R1' if region == 'M' else 'R1t'} on {key}", substrate=key),
            )
            manifest["runs"][rid] = dict(spec=spec.name, sha256=spec.sha256())
    _dump(os.path.join(out, "criteria", "line.json"), demos.line_criteria())
    for region, w, length in LINES:
        spec, rects = demos.line_spec(region, w, length, data=data)
        lid = f"line-{region.lower()}-w{int(round(w * 100)):03d}-l{length:g}"
        demos.forward_dir(
            spec,
            rects,
            os.path.join(out, "runs", lid),
            dict(what=f"straight {w} mm line, {length:g} mm, run 0b", substrate=f"{region}-eq"),
        )
        manifest["runs"][lid] = dict(spec=spec.name, sha256=spec.sha256())

    def fwd(rid: str, crit: str) -> dict:
        return dict(
            id=rid,
            case="divider",
            validate=f"runs/{rid}",
            criteria=f"criteria/{crit}.json",
            args=["--no-fine"],
            max_wall_s=FORWARD_WALL_S,
        )

    run0b = [dict(id="diag", diagnostic=True, **SMALL), fwd("r1-m-eq", "ref-m")]
    run0b += [
        dict(fwd(f"line-m-w{int(round(w * 100)):03d}-l{length:g}", "line"), **SMALL)
        for _, w, length in LINES
    ]
    with open(os.path.join(out, "run0b.toml"), "w", encoding="utf-8") as fh:
        fh.write(
            jobs_toml(
                "order0-run0b",
                "Order 0 run 0b (D-O0-11): R1 forward on the M equivalent substrate and four\n"
                "straight lines (0.40 and 0.70 mm, 10 and 30 mm) for the solver's loss. One\n"
                f"family ({SHAPES['M']}) for the comparison. Written by python -m yapnr.rf.order0"
                " write.",
                SHAPES["M"],
                run0b,
            )
        )
    for region, demo in (("M", "d1"), ("W", "d2")):
        jobs = [
            dict(
                id=f"{demo}-{form}",
                case="divider",
                spec=f"specs/{demo}-{form}.json",
                criteria=f"criteria/{demo}.json",
            )
            for form in demos.FORMULATIONS
        ]
        refs = REF_SUBSTRATES[region][1:] if region == "M" else REF_SUBSTRATES[region]
        prefix = "r1-" if region == "M" else "r1t-"
        jobs += [fwd(prefix + key.lower(), f"ref-{region.lower()}") for key in refs]
        if region == "W":
            jobs = [
                dict(j, **dict(W_TASK, max_wall_s=j.get("max_wall_s", W_TASK["max_wall_s"])))
                for j in jobs
            ]
        with open(os.path.join(out, f"compute-{region.lower()}.toml"), "w", encoding="utf-8") as fh:
            fh.write(
                jobs_toml(
                    f"order0-{demo}",
                    f"Order 0 {demo.upper()}: every formulation in parallel, and the references'\n"
                    f"forward predictions on the other substrates. One family ({SHAPES[region]})\n"
                    "for the comparison. Written by python -m yapnr.rf.order0 write.",
                    SHAPES[region],
                    jobs,
                )
            )
    _dump(os.path.join(out, "manifest.json"), manifest)
    return manifest


def loss(fetched: str, data: Optional[dict] = None) -> dict:
    """Run 0b: the solver's α of the 0.40 and 0.70 mm lines (two lengths each), the coupon
    model's (M and M1.4, the nearest family to 0.70 mm: its α is within 1 % of M's), Δα, R1's raw
    and corrected |S21| over the band, and D-O0-11's criterion."""

    def ts(job: str, n: int) -> str:
        """A job's Touchstone: `fetched/<job>/` or a `yapnr exp fetch --full` campaign
        directory's `tasks/mc~<job>/result/out/<job>/`."""
        name = f"coarse_dense.s{n}p"
        for d in (
            os.path.join(fetched, job),
            os.path.join(fetched, "tasks", f"mc~{job}", "result", "out", job),
        ):
            if os.path.isfile(os.path.join(d, name)):
                return os.path.join(d, name)
        raise FileNotFoundError(f"{name} of {job} under {fetched}")

    out: dict = {"schema": "yapnr-order0-run0b/1", "lines": {}}
    d_alpha, ratio = {}, {}
    for key, w, fam in (("line", 40, "M"), ("arm", 70, "M1.4")):
        f, a_sol = demos.solver_alpha(ts(f"line-m-w0{w}-l10", 2), ts(f"line-m-w0{w}-l30", 2), 20.0)
        a_cpn = demos.coupon_alpha(f, fam)
        d_alpha[key] = a_cpn - a_sol
        ratio[key] = a_cpn / a_sol
        band = (f >= demos.BAND[0] * 1e9 - 1) & (f <= demos.BAND[1] * 1e9 + 1)
        at5 = int(np.argmin(np.abs(f - 5e9)))
        out["lines"][key] = dict(
            width_mm=w / 100,
            coupon_family=fam,
            solver_db_per_cm_5ghz=round(float(a_sol[at5]) * 10, 5),
            coupon_db_per_cm_5ghz=round(float(a_cpn[at5]) * 10, 5),
            delta_db_per_cm_band=[
                round(float(np.min(d_alpha[key][band])) * 10, 5),
                round(float(np.max(d_alpha[key][band])) * 10, 5),
            ],
        )
    fr, s = demos.read_s(ts("r1-m-eq", 3))
    if fr.shape != f.shape or np.max(np.abs(fr - f)) > 1.0:
        d_alpha = {k: np.interp(fr, f, v) for k, v in d_alpha.items()}
        ratio = {k: np.interp(fr, f, v) for k, v in ratio.items()}
    path = demos.r1_path("M")
    corr = demos.path_correction(fr, d_alpha, path)
    band = (fr >= demos.BAND[0] * 1e9 - 1) & (fr <= demos.BAND[1] * 1e9 + 1)
    raw = {f"s{i}1": 20 * np.log10(np.abs(s[:, i - 1, 0])) for i in (1, 2, 3)}
    worst_raw = float(min(np.min(raw["s21"][band]), np.min(raw["s31"][band])))
    worst_corr = float(min(np.min((raw["s21"] + corr)[band]), np.min((raw["s31"] + corr)[band])))
    out["r1"] = dict(
        path_mm=[[k, round(v, 3)] for k, v in path],
        correction_db_band=[
            round(float(np.min(corr[band])), 4),
            round(float(np.max(corr[band])), 4),
        ],
        worst_s21_raw_db=round(worst_raw, 4),
        worst_s21_corrected_db=round(worst_corr, 4),
        worst_s11_db=round(float(np.max(raw["s11"][band])), 3),
    )
    by_ratio = demos.ratio_correction(s, ratio["line"])
    worst_ratio = float(
        min(np.min((raw["s21"] + by_ratio)[band]), np.min((raw["s31"] + by_ratio)[band]))
    )
    out["r1"]["ratio_check"] = dict(
        method="the dissipated fraction scaled by alpha_coupon/alpha_solver of the 0.40 mm line",
        correction_db_band=[
            round(float(np.min(by_ratio[band])), 4),
            round(float(np.max(by_ratio[band])), 4),
        ],
        worst_s21_corrected_db=round(worst_ratio, 4),
    )
    out["ratio_5ghz"] = {k: round(float(np.interp(5e9, fr, v)), 4) for k, v in ratio.items()}
    crit = demos.d_o0_11(worst_corr)
    out["d_o0_11"] = dict(
        rule="keep -3.4 dB if the corrected R1 clears it by >= 0.1 dB, else -3.5 dB",
        criterion_db=crit,
        offset_db=round(-float(np.min(corr[band])), 3),
    )
    return out
