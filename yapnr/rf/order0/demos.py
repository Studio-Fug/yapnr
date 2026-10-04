"""Order 0's RF demos: the D1/D2 specs and formulations, the references' and the loss lines'
forward runs, their criteria, and run 0b's loss correction (order0-design §4.4, §7, §10.1).

Substrates (`stackup`): the optimizer's single uniform substrate per region and board,
`yapnr.rf.coupons.equivalent`'s thickness-equivalent ones (`*-eq`, the coupon model's line at
5 GHz: D-O0-9) and the nominal ones (`*-nom`, the stackup rf block with zero-thickness copper,
the raw view whose bias the equivalent substrate removes).

Formulations (`FORMULATIONS`): the plan's baseline (round 2's passing divider formulation, V3,
at the plan's schedule) and variants that each change one thing against the main risk, that D1
does not pass: a wider robust set from the start, a junction start, and a longer plain epoch
with the epoch-best trust reference. The shipped run is chosen by `SELECTION`, fixed before any
run.

Forward runs (`forward_dir`): a fixed-copper design as a run directory (the spec and the
footprint the exporter writes), which `python -m yapnr.rf.cases validate` re-simulates exactly as
it re-validates an optimized design: R1/R1t (the closed-form references, `coupons.catalog.
o_reference`, on the refine-2 grid) and straight lines for run 0b.

Run 0b (`loss_correction`): the solver models copper as one smooth sheet over a PEC ground, so
its line loss is below the coupon model's (ground-plane loss, roughness, and the 1/f of its
substrate conductivity). Δα(f) = α_coupon − α_solver from straight lines on the solver's own grid,
applied along R1's path, gives R1's corrected |S21|, and D-O0-11's rule sets the |S21|
requirement: −3.4 dB if the corrected R1 clears it by 0.1 dB, else −3.5 dB, for D1 and R1 alike.
The optimizer works on the raw solver, so its requirement is the criterion plus R1's correction.

    python -m yapnr.rf.order0 write --out DIR [--s21 -3.4 --offset 0.1]   # specs, criteria, runs
    python -m yapnr.rf.order0 loss --runs DIR                             # after run 0b
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import replace
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from yapnr.rf.spec import (
    Band,
    GridSpec,
    OptimizerSpec,
    Port,
    Rules,
    S,
    SolverSpec,
    Spec,
    StackupSpec,
)

BAND = (4.25, 5.75)  # GHz, D1 and D2 (the round-1 divider spec at half scale, §4.4)
BAND_POINTS = 7
# The forward runs' second band widens the source pulse (its −20 dB band then spans about
# 2.4–7.6 GHz) so the predictions cover 3–7 GHz; a requirement that is always met puts it in.
WIDE = (3.0, 7.0)
F_REF_GHZ = 5.0
EQ_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "coupons",
    "data",
    "equivalent-oshpark-4l.json",
)
# The stackup rf block's tan δ at 5 GHz (order0-design §3.1); its εr and h per region (W: the
# series combination of prepreg, core and prepreg) are the equivalence file's "nominal".
NOMINAL_TAN_DELTA = 0.0095
BOARDS = {"": "oshpark-4l-fr408hr", "-em528": "oshpark-4l-em528"}

# The optimizer's windows: pitch, substrate cells, the 50 Ω port line in cells (M 0.40 mm, W
# 3.0 mm), the minimum width and space (M 0.20 mm, W 1.0 mm); the window and the ports are the
# board's (`coupons.catalog.O_WINDOWS`: D1 12 x 15 mm, P2/P3 at 6.0 mm; D2 20 x 24 mm, at 10 mm).
GRIDS = {
    "M": dict(pitch=0.10, sub=4, line_cells=4, rule=0.20),
    "W": dict(pitch=0.50, sub=4, line_cells=6, rule=1.00),
}
DEMOS = {"M": "D1", "W": "D2"}
# Round 2's passing divider (design §23, V3): eroded and dilated designs from β = 16, plain MMA at
# β = 8 and adaptive moves from β = 16; the plan's schedule (25, 15, 15, 15). The budget is wall
# minutes of the loop: native float64 at 16 threads takes about 16 s per D1 evaluation, and the
# task's attempts (rf_job --attempt-s) end long before it.
BASE = OptimizerSpec(
    betas=(8.0, 16.0, 32.0, 64.0),
    iterations_per_beta=(25, 15, 15, 15),
    budget_min=720.0,
    move=0.2,
    move_late=0.05,
    init=0.3,
    eta_variants=(0.45, 0.55),
    robust_from_beta=16.0,
    adaptive_move=True,
    adaptive_from_beta=16.0,
)
FORMULATIONS: Dict[str, Tuple[OptimizerSpec, str]] = {
    "base": (BASE, "the plan's §4.4 formulation (round 2's passing divider, V3)"),
    "robust": (
        replace(BASE, eta_variants=(0.40, 0.60), robust_from_beta=8.0),
        "a wider robust set (thresholds 0.40/0.60) from β = 8: against designs that pass the"
        " optimization grid and miss the finer ones, and against the narrow features and gaps"
        " the thickness-equivalent substrate models least well (review R15)",
    ),
    "star": (
        replace(BASE, seed="star"),
        "a junction start (`seed: star`, the ports' lines joined at the window centre, a 50 Ω"
        " T) instead of the uniform 0.3: another basin",
    ),
    "sched": (
        replace(BASE, iterations_per_beta=(35, 15, 15, 10), trust_reference="best"),
        "a longer plain epoch at β = 8 (35, as the combiner W11, whose t still fell at its"
        " 25th) and adaptive steps measured from the epoch's best t (design §24.3: the"
        " current-t rule let t creep)",
    ),
}
SELECTION = (
    "Among the formulations whose exported design passes every validation criterion on all"
    " three grids (coarse; fine and finer), ship the one with the largest worst-case margin"
    " over the fine and finer grids, the margin of a check being (limit - worst) in dB for"
    " |S11| and (worst - limit) for |S21| and |S31|; on a tie (within 0.05 dB) the one whose"
    " export changed the fewest pixels in the width and space repair (validation.json"
    " `repaired_pixels`), then the earlier one in the order base, robust, star, sched. Every"
    " attempt and its validation is published (§7 items 1 and 6)."
)
ROUND2_SOLVER = dict(edge_correction=True, port_source="mode")


def eq_data(path: str = EQ_FILE) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def stackup(key: str, data: Optional[dict] = None) -> StackupSpec:
    """The solver substrate `key`: region M or W, "-eq" (thickness-equivalent, coupon target) or
    "-nom" (the rf block, zero thickness), "-em528" for that board ("M-eq", "W-eq-em528")."""
    parts = key.split("-")
    region, kind = parts[0], parts[1]
    board = BOARDS["-" + parts[2] if len(parts) > 2 else ""]
    data = data or eq_data()
    if kind == "nom":
        if board != BOARDS[""]:
            raise ValueError("the nominal substrates are FR408HR's (the rf block)")
        nom = data["substrates"][f"{board}:{region}"]["nominal"]
        return StackupSpec(
            er=round(nom["er"], 4),
            tan_delta=NOMINAL_TAN_DELTA,
            h_mm=round(nom["h_mm"], 4),
            f_ref_ghz=F_REF_GHZ,
        )
    if kind != "eq":
        raise ValueError(f"substrate {key!r}: M or W, then eq or nom, then em528 or nothing")
    entry = data["substrates"][f"{board}:{region}"][data.get("use", "coupon")]["equivalent"]
    return StackupSpec(
        er=round(entry["er"], 4),
        tan_delta=round(entry["tan_delta"], 5),
        h_mm=round(entry["h_mm"], 4),
        f_ref_ghz=F_REF_GHZ,
    )


def _ports(window: dict, cells: int) -> Tuple[Port, ...]:
    out = []
    for n, (side, at) in enumerate(window["ports"], start=1):
        out.append(Port(n, side, float(at), cells))
    return tuple(out)


def _requirements(s21_min: float, wide: bool) -> tuple:
    reqs = (
        S(1, 1).at_most_db(-20.0, band="pass"),
        S(2, 1).at_least_db(s21_min, band="pass"),
        S(3, 1).at_least_db(s21_min, band="pass"),
    )
    if wide:
        reqs += (S(1, 1).at_most_db(0.0, band="wide"),)
    return reqs


def _bands(wide: bool) -> dict:
    bands = {"pass": Band(BAND[0], BAND[1], BAND_POINTS)}
    if wide:
        bands["wide"] = Band(WIDE[0], WIDE[1], 9)
    return bands


def demo_spec(
    region: str,
    formulation: str = "base",
    substrate: Optional[str] = None,
    s21_min: float = -3.4,
    threads: int = 16,
    data: Optional[dict] = None,
) -> Spec:
    """D1 (region M) or D2 (W): the divider in the board's window, ports W/N/S, mirror symmetric,
    on `substrate` (default the region's equivalent one), with `formulation`'s optimizer."""
    from yapnr.rf.coupons import catalog

    g = GRIDS[region]
    win = catalog.O_WINDOWS[DEMOS[region]]
    opt, _ = FORMULATIONS[formulation]
    sub = substrate or f"{region}-eq"
    return Spec(
        name=f"divider-osh-{region.lower()}" + ("" if formulation == "base" else f"-{formulation}"),
        stackup=stackup(sub, data),
        grid=GridSpec(pitch_mm=g["pitch"], substrate_cells=g["sub"]),
        design_region=(0.0, win["w"], -win["h"] / 2, win["h"] / 2),
        symmetry="mirror_y",
        rules=Rules(g["rule"], g["rule"]),
        ports=_ports(win, g["line_cells"]),
        bands=_bands(False),
        requirements=_requirements(s21_min, False),
        optimizer=opt,
        solver=SolverSpec(threads=threads, **ROUND2_SOLVER),
    )


def criteria(s21_target: float, dense: Sequence = ((4.0, 6.0, 81),)) -> dict:
    """The validation criteria (rf_job's `--criteria` format) for a divider whose optimizer
    target is `s21_target` (raw solver): round 2's divider criteria at the Order 0 band, |S21|
    and |S31| 0.05 dB (coarse) and 0.2 dB (fine and finer) below the target, as the divider's
    −3.45 / −3.6 dB under its −3.4 dB."""
    band = list(BAND)

    def s(name, kind, ports, limit):
        return dict(name=name, kind=kind, ports=list(ports), limit=round(limit, 4), ghz=band)

    pas = dict(name="passivity", kind="passivity", limit=-0.001)
    return {
        "dense_ghz": [list(d) for d in dense],
        "coarse": [
            s("|S11| max dB", "s_max", (1, 1), -17.0),
            s("|S21| min dB", "s_min", (2, 1), s21_target - 0.05),
            s("|S31| min dB", "s_min", (3, 1), s21_target - 0.05),
            pas,
        ],
        "fine": [
            s("|S11| max dB", "s_max", (1, 1), -15.0),
            s("|S21| min dB", "s_min", (2, 1), s21_target - 0.2),
            s("|S31| min dB", "s_min", (3, 1), s21_target - 0.2),
            s("||S21|-|S31|| dB", "imbalance", (2, 1, 3, 1), 0.25),
            pas,
        ],
    }


def line_criteria(dense: Sequence = ((3.0, 7.0, 161),)) -> dict:
    """A straight line's sanity checks (the run is a measurement of α, not a pass/fail)."""
    band = list(BAND)
    checks = [
        dict(name="|S21| min dB", kind="s_min", ports=[2, 1], limit=-3.0, ghz=band),
        dict(name="passivity", kind="passivity", limit=-0.001),
    ]  # no |S11| check: the files are in 50 Ω, a 0.70 mm line is 35 Ω
    return {"dense_ghz": [list(d) for d in dense], "coarse": checks, "fine": checks}


# --- forward runs ---------------------------------------------------------------------------


def reference_spec(
    region: str, substrate: Optional[str] = None, s21_min: float = -3.4, data=None
) -> Tuple[Spec, List[tuple]]:
    """R1 (M) or R1t (W) as a spec on the refine-2 grid (M 0.05, W 0.25 mm; 1.5 times the
    substrate cells, as the fine re-validation) and its copper rectangles (x0, x1, y0, y1)."""
    from yapnr.rf.coupons import catalog

    ref = catalog.o_reference(region)
    pitch = ref["fdtd_pitch_mm"]
    cells = int(round(catalog.O_REF_2D[region]["w_out"] / pitch))
    sub = substrate or f"{region}-eq"
    spec = Spec(
        name=("r1-osh-m" if region == "M" else "r1t-osh-w") + "-" + sub.lower(),
        stackup=stackup(sub, data),
        grid=GridSpec(pitch_mm=pitch, substrate_cells=6),
        design_region=(0.0, ref["w"], -ref["h"] / 2, ref["h"] / 2),
        symmetry="mirror_y",
        ports=tuple(
            Port(n, side, float(at), cells) for n, (side, at) in enumerate(ref["ports"], 1)
        ),
        bands=_bands(True),
        requirements=_requirements(s21_min, True),
        solver=SolverSpec(threads=16, **ROUND2_SOLVER),
    )
    return spec, [tuple(c) for c in ref["copper"]]


def line_spec(
    region: str, w_mm: float, length_mm: float, substrate: Optional[str] = None, data=None
) -> Tuple[Spec, List[tuple]]:
    """A straight line of width `w_mm` across a window `length_mm` long, ports W and E, on the
    references' grid: run 0b's loss lines."""
    from yapnr.rf.coupons import catalog

    pitch = catalog.O_REF_PITCH[region]
    cells = int(round(w_mm / pitch))
    if abs(cells * pitch - w_mm) > 1e-9:
        raise ValueError(f"{w_mm} mm is not a whole number of {pitch} mm cells")
    half = (
        (w_mm / 2 + 1.25) if region == "M" else (w_mm / 2 + 6.0)
    )  # about 7h and 4h of substrate beside the strip
    sub = substrate or f"{region}-eq"
    spec = Spec(
        name=f"line-osh-{region.lower()}-w{w_mm:.2f}-l{length_mm:g}-{sub.lower()}".replace(
            ".", "p"
        ),
        stackup=stackup(sub, data),
        grid=GridSpec(pitch_mm=pitch, substrate_cells=6),
        design_region=(0.0, float(length_mm), -half, half),
        symmetry="mirror_y",
        ports=(Port(1, "W", 0.0, cells), Port(2, "E", 0.0, cells)),
        bands=_bands(True),
        requirements=(
            S(2, 1).at_least_db(-3.0, band="pass"),
            S(1, 1).at_most_db(0.0, band="wide"),
        ),
        solver=SolverSpec(threads=16, **ROUND2_SOLVER),
    )
    return spec, [(0.0, float(length_mm), -w_mm / 2, w_mm / 2)]


def raster(spec: Spec, rects: Sequence[tuple]) -> np.ndarray:
    """The window's pixels (ni, nj) that the rectangles cover; every edge must lie on the grid."""
    x0, x1, y0, y1 = spec.design_region
    p = spec.grid.pitch_mm
    ni, nj = int(round((x1 - x0) / p)), int(round((y1 - y0) / p))
    m = np.zeros((ni, nj), dtype=np.float64)
    for a, b, c, d in rects:
        idx = [(a - x0) / p, (b - x0) / p, (c - y0) / p, (d - y0) / p]
        if any(abs(v - round(v)) > 1e-6 for v in idx):
            raise ValueError(f"copper {a, b, c, d} is not on the {p} mm grid")
        i0, i1, j0, j1 = (int(round(v)) for v in idx)
        m[max(i0, 0) : min(i1, ni), max(j0, 0) : min(j1, nj)] = 1.0
    return m


def forward_dir(spec: Spec, rects: Sequence[tuple], out: str, meta: Optional[dict] = None) -> str:
    """A run directory holding `spec` and the footprint of the fixed copper `rects` (written by
    the optimizer's exporter, so the validator re-simulates exactly this copper), plus
    `forward.json` (what it is). No FDTD runs here: the problem is built with placeholder line
    calibrations, which only the simulation would use."""
    from yapnr.rf.driver import write_json
    from yapnr.rf.export.kicad import write_footprint
    from yapnr.rf.export.report import footprint_of
    from yapnr.rf.problem import Problem

    prob = Problem(spec, calibrations={"*": None})
    mask = raster(spec, rects)
    if mask.shape != prob.design_shape:
        raise ValueError(f"raster {mask.shape} is not the window {prob.design_shape}")
    fp, _ = footprint_of(prob, mask, name=f"RF_{spec.name}")
    os.makedirs(out, exist_ok=True)
    write_json(os.path.join(out, "spec.json"), spec.to_dict())
    write_footprint(fp, os.path.join(out, "footprint.kicad_mod"))
    info = dict(
        schema="yapnr-rf-forward/1",
        spec=spec.name,
        spec_sha256=spec.sha256(),
        copper_mm=[list(r) for r in rects],
        pixels=int(mask.sum()),
    )
    info.update(meta or {})
    write_json(os.path.join(out, "forward.json"), info)
    return out


def forward_variant(
    run_dir: str, out: str, substrate: Optional[str] = None, wide: bool = True, data=None
) -> str:
    """A forward-run directory of an optimized design (its footprint, unchanged) on another
    substrate and/or with the wide band (the source pulse then covers 3-7 GHz): for the
    pre-registered predictions of the shipped D1/D2 on the nominal and EM528 substrates. The
    checkpoint and result are left out, so the validator does not compare against the optimizer's
    own (other-substrate) numbers."""
    import shutil

    from yapnr.rf.driver import write_json

    with open(os.path.join(run_dir, "spec.json"), encoding="utf-8") as fh:
        spec = Spec.from_dict(json.load(fh))
    if substrate:
        spec = replace(spec, stackup=stackup(substrate, data))
    if wide and "wide" not in spec.bands:
        reqs = spec.requirements + (S(1, 1).at_most_db(0.0, band="wide"),)
        spec = replace(spec, bands=dict(spec.bands, **_bands(True)), requirements=reqs)
    spec = replace(spec, name=spec.name + (f"-{substrate.lower()}" if substrate else "") + "-fwd")
    os.makedirs(out, exist_ok=True)
    write_json(os.path.join(out, "spec.json"), spec.to_dict())
    shutil.copyfile(
        os.path.join(run_dir, "footprint.kicad_mod"), os.path.join(out, "footprint.kicad_mod")
    )
    write_json(
        os.path.join(out, "forward.json"),
        dict(
            schema="yapnr-rf-forward/1",
            spec=spec.name,
            spec_sha256=spec.sha256(),
            from_run=os.path.basename(os.path.normpath(run_dir)),
            substrate=substrate or "as optimized",
            wide=wide,
        ),
    )
    return out


# --- run 0b: the loss correction --------------------------------------------------------------


def read_s(path: str) -> Tuple[np.ndarray, np.ndarray]:
    from yapnr.rf.export.touchstone import read_touchstone

    f, s = read_touchstone(path)[:2]
    return np.asarray(f, float), np.asarray(s)


def line_loss_db(s: np.ndarray) -> np.ndarray:
    """The attenuation α·L (dB) of a uniform line section from its 2-port S-parameters in any
    real reference: A = cosh(γL) of its ABCD matrix does not depend on the reference impedance
    (a 0.70 mm line, 35 Ω, renormalized to 50 Ω has |S11| near −9 dB, which a |S21| reading
    would count as loss), and Re acosh(A) = αL is free of the branch of βL."""
    s11, s12, s21, s22 = s[:, 0, 0], s[:, 0, 1], s[:, 1, 0], s[:, 1, 1]
    a = ((1 + s11) * (1 - s22) + s12 * s21) / (2 * s21)
    return 20 * np.log10(math.e) * np.abs(np.real(np.arccosh(a.astype(complex))))


def solver_alpha(short: str, long: str, dl_mm: float) -> Tuple[np.ndarray, np.ndarray]:
    """(f in Hz, α in dB/mm) of the solver's line from two lengths (`line_loss_db`; their
    difference also cancels what the port planes add)."""
    f1, s1 = read_s(short)
    f2, s2 = read_s(long)
    if f1.shape != f2.shape or np.max(np.abs(f1 - f2)) > 1.0:
        raise ValueError("the two lines' frequencies differ")
    return f1, (line_loss_db(s2) - line_loss_db(s1)) / dl_mm


def coupon_alpha(f_hz: np.ndarray, family: str, stackup_id: str = "OSHPARK-4L-FR408HR"):
    """α (dB/mm) of a coupon line family at nominal parameters (the shipped coupon model; the
    lines' L1 ground at 1.0 mm is part of it)."""
    from yapnr.rf.coupons import families, models, stackups

    st = stackups.get(stackup_id)
    v = stackups.with_values(st, {})
    line = models.family_line(
        family, v, np.asarray(f_hz, float), families.load(stackup_id), stackup_id=stackup_id
    )
    return 20 * np.log10(math.e) * np.real(line.gamma) * 1e-3


def path_correction(
    f_hz: np.ndarray, d_alpha: Dict[str, np.ndarray], path: Sequence[Tuple[str, float]]
) -> np.ndarray:
    """dB to add to the solver's |S21| (negative: more loss): −Σ Δα_w(f) · L_w along `path`
    [(line key, mm)]."""
    total = np.zeros_like(np.asarray(f_hz, float))
    for key, length in path:
        total -= d_alpha[key] * length
    return total


def ratio_correction(s: np.ndarray, ratio: np.ndarray, port: int = 1) -> np.ndarray:
    """dB to add to every |S_i,port| of a passive design (F, N, N), for any geometry: the
    fraction of the incident power it dissipates (1 − Σ_i |S_i,port|², the solver's copper,
    substrate and the little it radiates) scaled by `ratio`(f) = α_coupon/α_solver of the port
    line, the outputs' powers scaled by the same factor. A path-free check of `path_correction`
    (radiation scaled too: conservative)."""
    j = port - 1
    kept = np.sum(np.abs(s[:, :, j]) ** 2, axis=1)
    lost = np.clip(1.0 - kept, 0.0, 1.0)
    return 10.0 * np.log10((1.0 - lost * ratio) / (1.0 - lost))


def r1_path(region: str = "M") -> List[Tuple[str, float]]:
    """R1's path from port 1 to port 2: the λ/4 arm (0.70 mm) to the output line's centre, then
    half the output line (0.40 mm) to its port (`coupons.catalog.o_reference`)."""
    from yapnr.rf.coupons import catalog

    ref = catalog.o_reference(region)
    (ax0, ax1, _, _), (_, _, oy0, oy1) = ref["copper"]
    w_out = catalog.O_REF_2D[region]["w_out"]
    return [("arm", ax1 - ax0 + w_out / 2), ("line", (oy1 - oy0) / 2)]


def d_o0_11(worst_corrected_db: float) -> float:
    """D-O0-11: keep −3.4 dB if the corrected R1 clears it by at least 0.1 dB, else −3.5 dB."""
    return -3.4 if worst_corrected_db >= -3.4 + 0.1 - 1e-9 else -3.5
