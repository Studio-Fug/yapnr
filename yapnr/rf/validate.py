"""Independent re-validation of an exported design (design §11.5).

The validator reads the run directory's `footprint.kicad_mod`, not the optimizer's arrays, and
re-simulates the copper it describes:

- **raster:** the footprint's copper (islands, custom-pad primitives and the rectangular port
  pads) is sampled at the centres of the pixels of a grid of pitch Δ/refine with the
  "inside or on" rule: a pixel is copper when at least two of four points at ±10⁻⁴ pixel
  around its centre (along x and y) are inside a polygon, so pixels that a 45° chamfer cuts
  in half count as copper and the rule is mirror symmetric;
- **ports:** the feed widths are the widths of the port pads; the lines are calibrated again
  on the new grid and the S-matrix (every port excited) is renormalized to 50 Ω;
- **grids:** "coarse" is the optimization grid (refine 1), which must reproduce the exported
  design pixel for pixel (the optimizer's binary design after the width and space repair) and
  the optimizer's binary S-parameters within 0.5 dB; "fine" halves the in-plane pitch and has
  1.5 times the substrate cells (6 on S1, 9 on S2), graded by the same rules.

`validate_case` applies the criteria of `cases.CRITERIA` to both and writes `validation.json`,
`coarse_dense.sNp` and `fine.sNp` (engineering e^{+jωt} convention, 50 Ω).
"""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import replace

import numpy as np

from yapnr.rf import sparams
from yapnr.rf.export.kicad import read_footprint
from yapnr.rf.export.raster import rasterize
from yapnr.rf.export.touchstone import write_touchstone
from yapnr.rf.spec import Spec

VALIDATION = "validation.json"
# The exported footprint, re-simulated on the optimization grid, against the optimizer's binary
# design at the objective frequencies: transmissions (|S| ≥ −10 dB) within 0.5 dB (design
# §11.2) and every |S_ij| within 0.05 (the width and space repair moves a −20 dB reflection by
# a few dB but its magnitude by a few hundredths).
EXPORT_DB = 0.5
EXPORT_ABS = 0.05
_EPS = 1e-4  # sample offset around pixel centres, in pixels


def load_spec(run_dir: str) -> Spec:
    with open(os.path.join(run_dir, "spec.json"), encoding="utf-8") as fh:
        return Spec.from_dict(json.load(fh))


def board_polygons(fp, spec: Spec) -> list[np.ndarray]:
    """The footprint's copper in board coordinates (mm, y up)."""
    x0, x1, y0, y1 = spec.design_region
    ox, oy = 0.5 * (x0 + x1), 0.5 * (y0 + y1)
    return [np.column_stack([ox + p[:, 0], oy - p[:, 1]]) for p in fp.copper()]


def footprint_mask(fp, spec: Spec, pitch_mm: float) -> np.ndarray:
    """Boolean (ni, nj) design-window pixels at `pitch_mm` that the footprint's copper covers
    ("inside or on", see the module doc)."""
    x0, x1, y0, y1 = spec.design_region
    ni = int(round((x1 - x0) / pitch_mm))
    nj = int(round((y1 - y0) / pitch_mm))
    xc = x0 + (np.arange(ni) + 0.5) * pitch_mm
    yc = y0 + (np.arange(nj) + 0.5) * pitch_mm
    polys = board_polygons(fp, spec)
    e = _EPS * pitch_mm
    votes = np.zeros((ni, nj), dtype=np.int8)
    for dx, dy in ((e, 0.0), (-e, 0.0), (0.0, e), (0.0, -e)):
        votes += rasterize(polys, xc + dx, yc + dy).astype(np.int8)
    return votes >= 2


def pad_widths(fp, spec: Spec) -> dict:
    """Port number → feed width in cells of the spec's pitch, from the port pads."""
    pitch = spec.grid.pitch_mm
    out = {}
    for p in spec.ports:
        pads = [q for q in fp.pads if str(q["number"]) == str(p.n)]
        if not pads:
            raise ValueError(f"the footprint has no pad for port {p.n}")
        sx, sy = pads[0]["size"]
        w = sy if p.side in ("W", "E") else sx
        cells = w / pitch
        if abs(cells - round(cells)) > 1e-3:
            raise ValueError(f"port {p.n}: pad width {w} mm is not a whole number of pitches")
        out[p.n] = int(round(cells))
    return out


def resimulate(
    run_dir: str,
    *,
    refine: int = 1,
    n_sub: int | None = None,
    freqs=None,
    backend: str | None = None,
    dtype=None,
    cache_dir: str | None = None,
    log=None,
) -> dict:
    """Re-simulate the run's footprint on a grid `refine` times finer in-plane (and `n_sub`
    substrate cells); returns freqs (Hz), s (F, N, N; 50 Ω, engineering convention, from the
    wave matrices S = B A⁻¹), s_ref (b_i/a_j referenced to each port's Z_c, internal convention:
    what the optimizer evaluates), eta, the largest idle-port incident wave, mask, the problem,
    steps and wall time."""
    from yapnr.rf.problem import Problem

    log = log or (lambda *_: None)
    t0 = time.perf_counter()
    spec = load_spec(run_dir)
    fp = read_footprint(os.path.join(run_dir, "footprint.kicad_mod"))
    widths = pad_widths(fp, spec)
    vspec = spec.replace(ports=tuple(replace(p, width_cells=widths[p.n]) for p in spec.ports))
    prob = Problem(
        vspec,
        refine=refine,
        n_sub=n_sub,
        backend=backend,
        dtype=dtype,
        cache_dir=cache_dir or os.path.join(run_dir, "cache"),
        interpolation="resistive",  # binary copper: the physical sheet
        log=log,
    )
    mask = footprint_mask(fp, spec, spec.grid.pitch_mm / refine)
    if mask.shape != prob.design_shape:
        raise ValueError(f"raster {mask.shape} does not match the window {prob.design_shape}")
    t1 = time.perf_counter()
    sw = prob.sweep(mask.astype(np.float64), freqs)
    s50 = sparams.renormalize(sw["s"], sw["zc"], 50.0)
    log(
        f"re-simulated {spec.name} at refine {refine} ({prob.grid.cells} cells): "
        f"{time.perf_counter() - t1:.1f} s"
    )
    return {
        "freqs": sw["freqs"],
        "s": sparams.to_engineering(s50),
        "s_ref": sw["s_naive"],
        "idle_incident": sw.get("idle_incident"),
        "zc": sw["zc"],
        "eta": sw["eta"],
        "mask": mask,
        "problem": prob,
        "steps": sw["steps"],
        "wall_s": time.perf_counter() - t0,
    }


# Power balance of radiators (design §11.3): the port's net input power against what leaves a
# box closed by the ground (side faces from the ground up, the top face, the feed windows left
# out), the power the other ports take and the dissipation inside the box.
BALANCE = 0.02


def power_balance(prob, rho: np.ndarray, freqs, port: int = 1) -> dict:
    """The power balance of the design `rho` with `port` excited, at `freqs` (Hz).

    Returns per frequency (lists): `p_in` (the port's net input power, power factor × (|a|² −
    |b|²)/2, at the reference plane), the fractions of it that leave the closed box
    (`closed`), leave the radiation box the objectives use (`eta`, from the copper plane up),
    are dissipated inside the closed box (`diss`) and are taken by the other ports (`ports`),
    and `error` = closed + diss + ports − 1. A non-zero error is the inconsistency of the port
    power (the calibration's power factor, |S|) with the fields: the radiated fraction the
    criteria judge is only as good as this balance.
    """
    from yapnr.rf.fdtd.monitors import dissipated_power, region_probes
    from yapnr.rf.fdtd.stop import StopRule

    if prob.box is None:
        raise ValueError("the power balance needs a spec with a radiation box")
    rb = prob.spec.radiation
    dom, g = prob.domain, prob.grid
    closed = dom.radiation_box(
        (rb.offset_mm * 1e-3) if rb.offset_mm is not None else 0.5 * dom.spec.margin,
        (rb.height_mm * 1e-3) if rb.height_mm is not None else 0.6 * dom.spec.air,
        name="closed",
        window_margin=None if rb.window_margin_mm is None else rb.window_margin_mm * 1e-3,
        window_height=None if rb.window_height_mm is None else rb.window_height_mm * 1e-3,
        from_ground=True,
    )
    (i0, i1), (j0, j1), _ = closed.node_box
    # Lossy edges only: the substrate and the copper plane (σ = 0 in the air above).
    zbox = (0, g.k_c + 1)
    region = region_probes(g, "diss", ((i0, i1), (j0, j1), zbox))
    weights = []
    for p in region:
        ii, jj, _ = g.unravel(p.comp, p.index)
        w = np.ones(p.index.size)
        if p.comp != "ex":
            w = np.where((ii == i0) | (ii == i1), 0.5 * w, w)
        if p.comp != "ey":
            w = np.where((jj == j0) | (jj == j1), 0.5 * w, w)
        weights.append(w)
    freqs = np.asarray(freqs, dtype=np.float64)
    omega = 2.0 * np.pi * freqs
    prob.set_design(rho)
    probes = prob.port_probes + prob.box_probes + closed.probes + region
    stop = StopRule(
        tol=prob.tol,
        f_lo=float(freqs.min()),
        max_steps=prob.spec.solver.max_steps,
        probes=tuple(prob.watched),
    )
    sources = prob.ports[port].mode_sources(prob.pulse, prob.dt)
    res = prob.sim.run(sources, probes, omega, stop, decimation=prob._decimation())
    q = prob.quantities(res.dft, port, omega)
    a, b = q["waves"][port]
    p_in = prob.cal[port].power_at(omega) * 0.5 * (np.abs(a) ** 2 - np.abs(b) ** 2)
    others = np.zeros(freqs.size)
    for n, (_, bn) in q["waves"].items():
        if n != port:
            others += sparams.incident_power(bn, prob.cal[n], omega)
    p_closed = np.asarray(closed.power(res.dft), dtype=np.float64)
    p_diss = dissipated_power(prob.sim.structure, region, res.dft, omega, prob.dt, weights)
    eta = np.asarray(q["eta"][port], dtype=np.float64) * sparams.incident_power(
        a, prob.cal[port], omega
    )
    frac = {
        "closed": p_closed / p_in,
        "eta": eta / p_in,
        "diss": p_diss / p_in,
        "ports": others / p_in,
    }
    err = frac["closed"] + frac["diss"] + frac["ports"] - 1.0
    out = {"ghz": (freqs / 1e9).tolist(), "p_in": p_in.tolist(), "steps": int(res.steps)}
    out.update({k: np.asarray(v, dtype=np.float64).tolist() for k, v in frac.items()})
    out["error"] = err.tolist()
    return out


def reciprocity_error(s: np.ndarray) -> float:
    """max |S_ij − S_ji| over the sweep: the exact discrete system is reciprocal, so this
    measures the error of the port-wave extraction."""
    return float(np.max(np.abs(s - np.swapaxes(s, -1, -2))))


def _table(freqs, s, eta) -> dict:
    """|S_ij| in dB (and η) per frequency, for the report."""
    out = {"ghz": [float(f) / 1e9 for f in freqs]}
    n = s.shape[-1]
    for j in range(n):
        for i in range(n):
            out[f"S{i + 1}{j + 1}"] = [round(float(v), 4) for v in sparams.db(s[:, i, j])]
    for k, v in (eta or {}).items():
        out[f"eta{k}"] = [round(float(x), 5) for x in np.asarray(v)]
    return out


def _touchstone(path, sim, label, spec) -> None:
    n = sim["s"].shape[-1]
    write_touchstone(
        path,
        sim["freqs"],
        sim["s"],
        comments=[
            f"yapnr rf: {spec.name}, footprint re-simulated ({label})",
            f"spec sha256 {spec.sha256()}",
            f"{n} ports renormalized to 50 ohm, e^(+jwt) convention",
        ],
    )


def connectivity(fp, spec: Spec) -> dict:
    """Whether one copper island joins every port (a net tie of all pads; for one port, a
    custom pad carrying the copper), and the island counts."""
    ports = sorted(p.n for p in spec.ports)
    groups = [sorted(int(x) for x in str(g).split(",")) for g in fp.net_tie_groups]
    custom = [p for p in fp.pads if p["shape"] == "custom"]
    if len(ports) >= 2:
        joined = any(set(ports) <= set(g) for g in groups)
    else:
        joined = bool(custom)
    n_islands = len(fp.polygons) + sum(len(p["primitives"]) for p in custom)
    return {
        "ports_joined": bool(joined),
        "net_tie_groups": [str(g) for g in fp.net_tie_groups],
        "islands": int(n_islands),
        "islands_without_port": int(len(fp.polygons) - len(groups)),
    }


def binary_design(run_dir: str, problem) -> np.ndarray | None:
    """The optimizer's binary design (β = ∞ of the exported x in the checkpoint, `export_x`,
    else its last x) on `problem`'s grid; None without a checkpoint."""
    path = os.path.join(run_dir, "checkpoint.npz")
    if not os.path.exists(path):
        return None
    with np.load(path, allow_pickle=False) as z:
        x = np.array(z["export_x"] if "export_x" in z else z["x"])
    return problem.param.rho_bar(x, math.inf) > 0.5


def substrate_cells_at(spec: Spec, refine: int) -> int:
    """Substrate cells of the re-validation grid `refine` times finer in-plane: 1.5 times the
    spec's at refine 2, twice at refine 3 (1 + (refine − 1)/2 times)."""
    return int(math.ceil(spec.grid.substrate_cells * (1.0 + 0.5 * (refine - 1)) - 1e-9))


def convergence(levels: dict, checks) -> dict:
    """The worst value of each check (the fine criteria) on every re-validation grid, coarse to
    finest: {"refine": [...], "checks": {name: [worst, ...]}, "last_change": {name: Δ}}. The
    trend shows whether the fine grid's verdict is converged: the zero-thickness copper's edge
    and the staircase of diagonal edges make the response depend on the pitch."""
    names = [c.name for c in checks if c.kind != "balance"]
    out = {"refine": sorted(levels), "checks": {}, "last_change": {}}
    for name in names:
        vals = []
        for r in sorted(levels):
            res = [c for c in levels[r]["checks"] if c["name"] == name]
            vals.append(res[0]["worst"] if res else None)
        out["checks"][name] = vals
        if len(vals) >= 2 and None not in vals[-2:]:
            out["last_change"][name] = vals[-1] - vals[-2]
    return out


def validate_case(
    run_dir: str,
    *,
    case: str | None = None,
    refine: int = 2,
    fine: bool = True,
    finer: int | None = None,
    criteria: dict | None = None,
    log=print,
) -> dict:
    """Re-validate a run directory on the optimization grid, (when `fine`) on the finer grid
    and (when `finer`, e.g. 3) on a third grid `finer` times finer in-plane, and judge the
    case's criteria (`cases.CRITERIA`: the fine criteria on the fine and the finest grids);
    returns the report, with the trend of the fine checks over the grids (`convergence`)."""
    from yapnr.rf import cases

    log = log or (lambda *_: None)
    spec = load_spec(run_dir)
    case = case or cases.case_of(spec)
    crit = criteria or cases.CRITERIA[case]
    smoke = spec.name.endswith("-smoke")
    report: dict = {"schema": "yapnr-rf-validation/1", "case": case, "spec": spec.name}
    report["spec_sha256"] = spec.sha256()
    report["wall_s"] = {}
    freqs = cases.sweep_frequencies(case, spec)
    # Same grid, from the export.
    co = resimulate(run_dir, refine=1, freqs=freqs, log=log)
    prob = co["problem"]
    report["wall_s"]["coarse"] = co["wall_s"]
    n = co["s"].shape[-1]
    _touchstone(os.path.join(run_dir, f"coarse_dense.s{n}p"), co, "optimization grid", spec)
    same: dict = {"pixels": int(co["mask"].size)}
    binary = binary_design(run_dir, prob)
    if binary is not None:
        from yapnr.rf.export.report import exported_binary

        exported, info = exported_binary(prob, binary)
        # The footprint against the design the export wrote (pixel exact), and what the width
        # and space repair changed against the optimizer's binary design.
        same["pixel_xor"] = int(np.sum((exported > 0.5) != co["mask"]))
        same["repaired_pixels"] = info["changed_pixels"]
    res_path = os.path.join(run_dir, "result.json")
    if os.path.exists(res_path):
        with open(res_path, encoding="utf-8") as fh:
            res = json.load(fh)
        opt_ghz = np.asarray(res["coarse_binary"]["frequencies_ghz"])
        idx = [int(np.argmin(np.abs(co["freqs"] / 1e9 - f))) for f in opt_ghz]
        # The re-simulated footprint against the optimizer's binary design (both referenced to
        # the feeds' Z_c): in dB where |S| ≥ −10 dB (transmissions), and as a difference of
        # magnitudes everywhere (reflections near −20 dB move by many dB for a small change).
        db_diff, abs_diff = [], []
        for key, vals in res["coarse_binary"]["s_db"].items():
            i, j = int(key[1]) - 1, int(key[2]) - 1
            again = sparams.db(co["s_ref"][idx, i, j])
            opt = np.asarray(vals, dtype=np.float64)
            big = opt >= -10.0
            if big.any():
                db_diff.append(np.max(np.abs(again - opt)[big]))
            abs_diff.append(np.max(np.abs(10 ** (again / 20) - 10 ** (opt / 20))))
        same["max_db_diff_transmission"] = float(max(db_diff)) if db_diff else 0.0
        same["max_abs_diff"] = float(max(abs_diff)) if abs_diff else 0.0
        report["drc"] = res.get("drc")
    report["same_grid"] = same
    report["footprint"] = connectivity(
        read_footprint(os.path.join(run_dir, "footprint.kicad_mod")), spec
    )
    balance_at = [c for c in crit["coarse"] if c.kind == "balance"]
    if balance_at:
        bal = power_balance(
            prob, co["mask"].astype(np.float64), np.asarray(balance_at[0].at_ghz) * 1e9
        )
        co["balance"] = {1: bal}
    report["coarse"] = cases.judge(
        crit["coarse"], co["freqs"], co["s"], co["eta"], balance=co.get("balance")
    )
    if "balance" in co:
        report["coarse"]["balance"] = co["balance"][1]
    report["coarse"]["grid"] = prob.describe()
    report["coarse"]["table"] = _table(co["freqs"], co["s"], co["eta"])
    report["coarse"]["reciprocity_error"] = reciprocity_error(co["s"])
    if co.get("idle_incident") is not None:
        report["coarse"]["idle_incident_max"] = float(np.max(co["idle_incident"]))
    ok = report["coarse"]["ok"]
    # The export check: True or False when every part of it ran, None ("not checked") when the
    # run directory lacks the checkpoint or the result it compares against (a published case
    # directory without its checkpoint), so it never reads as passed by default.
    parts = [
        None if "pixel_xor" not in same else same["pixel_xor"] == 0,
        (
            None
            if "max_db_diff_transmission" not in same
            else same["max_db_diff_transmission"] <= EXPORT_DB
            and same["max_abs_diff"] <= EXPORT_ABS
        ),
        None if report.get("drc") is None else bool(report["drc"]["ok"]),
        bool(report["footprint"]["ports_joined"]),
    ]
    if any(p is False for p in parts):
        export_ok = False
    elif any(p is None for p in parts):
        export_ok = None
    else:
        export_ok = True
    report["export_ok"] = export_ok
    ok = ok and export_ok is not False
    levels = {}
    if fine:
        # The coarse sweep judged by the fine criteria, for the trend over the grids.
        levels[1] = cases.judge(
            [c for c in crit["fine"] if c.kind != "balance"], co["freqs"], co["s"], co["eta"]
        )
        n_sub = substrate_cells_at(spec, refine)
        fi = resimulate(run_dir, refine=refine, n_sub=n_sub, freqs=freqs, log=log)
        report["wall_s"]["fine"] = fi["wall_s"]
        _touchstone(os.path.join(run_dir, f"fine.s{n}p"), fi, f"refine {refine}", spec)
        balance_at = [c for c in crit["fine"] if c.kind == "balance"]
        if balance_at:
            bal = power_balance(
                fi["problem"],
                fi["mask"].astype(np.float64),
                np.asarray(balance_at[0].at_ghz) * 1e9,
            )
            fi["balance"] = {1: bal}
        report["fine"] = cases.judge(
            crit["fine"], fi["freqs"], fi["s"], fi["eta"], balance=fi.get("balance")
        )
        if "balance" in fi:
            report["fine"]["balance"] = fi["balance"][1]
        report["fine"]["grid"] = fi["problem"].describe()
        report["fine"]["table"] = _table(fi["freqs"], fi["s"], fi["eta"])
        report["fine"]["reciprocity_error"] = reciprocity_error(fi["s"])
        if fi.get("idle_incident") is not None:
            report["fine"]["idle_incident_max"] = float(np.max(fi["idle_incident"]))
        ok = ok and report["fine"]["ok"]
        levels[refine] = report["fine"]
        del fi
    if fine and finer:
        n_sub = substrate_cells_at(spec, finer)
        fr = resimulate(run_dir, refine=finer, n_sub=n_sub, freqs=freqs, log=log)
        report["wall_s"]["finer"] = fr["wall_s"]
        _touchstone(os.path.join(run_dir, f"finer.s{n}p"), fr, f"refine {finer}", spec)
        balance_at = [c for c in crit["fine"] if c.kind == "balance"]
        if balance_at:
            bal = power_balance(
                fr["problem"],
                fr["mask"].astype(np.float64),
                np.asarray(balance_at[0].at_ghz) * 1e9,
            )
            fr["balance"] = {1: bal}
        report["finer"] = cases.judge(
            crit["fine"], fr["freqs"], fr["s"], fr["eta"], balance=fr.get("balance")
        )
        if "balance" in fr:
            report["finer"]["balance"] = fr["balance"][1]
        report["finer"]["grid"] = fr["problem"].describe()
        report["finer"]["table"] = _table(fr["freqs"], fr["s"], fr["eta"])
        report["finer"]["reciprocity_error"] = reciprocity_error(fr["s"])
        if fr.get("idle_incident") is not None:
            report["finer"]["idle_incident_max"] = float(np.max(fr["idle_incident"]))
        ok = ok and report["finer"]["ok"]
        levels[finer] = report["finer"]
    if len(levels) >= 2:
        report["convergence"] = convergence(levels, crit["fine"])
    report["smoke"] = smoke
    report["ok"] = bool(ok)
    return report


def write_report(run_dir: str, report: dict) -> str:
    from yapnr.rf.driver import write_json

    path = os.path.join(run_dir, VALIDATION)
    write_json(path, report)
    return path
