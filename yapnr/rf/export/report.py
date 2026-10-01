"""The run directory's exports: polygons, footprint, Touchstone file and result JSON (§10).

`export_design` takes the binarized design of an optimization and writes

- `footprint.kicad_mod`: the copper islands (`contour`), checked for minimum width and space
  (`drc`), with one pad per port (`kicad`);
- `coarse.sNp`: the full S-matrix of the binary design on the optimization grid (every port
  excited), renormalized to 50 Ω, engineering convention, over the sweep grid;
- the `yapnr-rf-result/1` dictionary (the driver writes it as `result.json`): spec hash, solver
  settings, optimizer summary, achieved values, the width and space check and provenance.
"""

from __future__ import annotations

import os
import platform
import time

import numpy as np

from yapnr.rf import sparams
from yapnr.rf.export.contour import islands, point_in_loop
from yapnr.rf.export.drc import check_width_space
from yapnr.rf.export.kicad import Footprint, PortPad, write_footprint
from yapnr.rf.export.touchstone import write_touchstone

RESULT = "result.json"
FOOTPRINT = "footprint.kicad_mod"
RESULT_SCHEMA = "yapnr-rf-result/1"


def port_pads(problem) -> list[tuple[int, slice, slice]]:
    """(port, pixel slice along x, pixel slice along y) of each port pad in window pixels."""
    from yapnr.rf.problem import PAD_DEPTH

    dom = problem.domain
    i0, _, j0, _ = dom.window
    depth = PAD_DEPTH * problem.refine
    out = []
    for pg in dom.ports:
        along = (
            slice(pg.i_ref, pg.i_ref + depth) if pg.sign > 0 else slice(pg.i_ref - depth, pg.i_ref)
        )
        across = slice(pg.ta, pg.tb)
        sx, sy = (along, across) if pg.axis == 0 else (across, along)
        out.append(
            (
                pg.port.number,
                slice(sx.start - i0, sx.stop - i0),
                slice(sy.start - j0, sy.stop - j0),
            )
        )
    return out


def footprint_of(problem, binary: np.ndarray, *, name: str | None = None) -> tuple:
    """(Footprint, island polygons in mm) of a binary window design."""
    spec = problem.spec
    pitch = spec.grid.pitch_mm / problem.refine
    x0, x1, y0, y1 = spec.design_region
    isl = islands(np.asarray(binary) > 0.5, simplify_tol=0.125)
    pads = []
    pad_px = port_pads(problem)
    for n, sx, sy in pad_px:
        cx = x0 + 0.5 * (sx.start + sx.stop) * pitch
        cy = y0 + 0.5 * (sy.start + sy.stop) * pitch
        pads.append(
            PortPad(n, (cx, cy), ((sx.stop - sx.start) * pitch, (sy.stop - sy.start) * pitch))
        )
    shapes = []
    for island in isl:
        touched = sorted(
            n
            for n, sx, sy in pad_px
            if point_in_loop(
                (0.5 * (sx.start + sx.stop), 0.5 * (sy.start + sy.stop)), island.polygon
            )
        )
        poly_mm = np.column_stack(
            [x0 + island.polygon[:, 0] * pitch, y0 + island.polygon[:, 1] * pitch]
        )
        shapes.append((poly_mm, touched))
    st = spec.stackup
    sha = spec.sha256()
    descr = (
        f"yapnr RF inverse design '{spec.name}': microstrip copper on er {st.er:g}, "
        f"tan_delta {st.tan_delta:g}, h {st.h_mm:g} mm with a solid ground on the next copper "
        f"layer; spec sha256 {sha}"
    )
    fp = Footprint(
        name=name or f"RF_{spec.name}",
        origin=(0.5 * (x0 + x1), 0.5 * (y0 + y1)),
        region=(x0, x1, y0, y1),
        pads=pads,
        islands=shapes,
        description=descr,
        seed=sha,
    )
    return fp, [p for p, _ in shapes]


def provenance(problem) -> dict:
    import torch

    try:
        from yapnr import __version__ as yv
    except Exception:  # pragma: no cover - the package marker always has it
        yv = ""
    return {
        "yapnr": yv,
        "python": platform.python_version(),
        "numpy": np.__version__,
        "torch": torch.__version__,
        "system": platform.system(),
        "machine": platform.machine(),
        "backend": problem.backend,
        "dtype": str(problem.dtype),
        "threads": problem.threads,
    }


def achieved(problem, ev) -> dict:
    """The binary design's values at the objective frequencies."""
    spec = problem.spec
    reqs = []
    for k, r in enumerate(spec.requirements):
        key = f"{k + 1}: {r.label}"
        phi = ev.phi.get(key)
        worst = float(np.nanmax(phi)) if phi is not None else None
        reqs.append(
            {"requirement": r.label, "worst_phi": worst, "met": worst is not None and worst <= 0}
        )
    s_db = {}
    n = ev.s.shape[-1]
    for j in range(n):
        if np.all(np.isnan(ev.s[:, 0, j])):
            continue
        for i in range(n):
            s_db[f"S{i + 1}{j + 1}"] = sparams.db(ev.s[:, i, j]).tolist()
    return {
        "frequencies_ghz": (problem.freqs / 1e9).tolist(),
        "t": float(np.max(ev.values)),
        "f": ev.values.tolist(),
        "s_db": s_db,
        "eta": {str(k): v.tolist() for k, v in ev.eta.items()},
        "requirements": reqs,
        "all_met": all(r["met"] for r in reqs),
    }


def export_design(problem, opt, final: dict, out_dir: str, *, sweep: bool = True, log=None) -> dict:
    """Write the footprint and the Touchstone file; return the result dictionary."""
    log = log or (lambda *_: None)
    t0 = time.perf_counter()
    spec = problem.spec
    binary = final["binary"]
    fp, polys = footprint_of(problem, binary)
    write_footprint(fp, os.path.join(out_dir, FOOTPRINT))
    pitch = spec.grid.pitch_mm / problem.refine
    rules = spec.rules
    drc = check_width_space(
        polys, spec.design_region, pitch, rules.min_width_mm, rules.min_space_mm
    )
    n = len(problem.ports)
    sweep_json = None
    if sweep:
        sw = problem.sweep(binary)
        s50 = sparams.renormalize(sw["s"], sw["zc"], 50.0)
        eng = sparams.to_engineering(s50)
        write_touchstone(
            os.path.join(out_dir, f"coarse.s{n}p"),
            sw["freqs"],
            eng,
            comments=[
                f"yapnr rf: {spec.name}, binary design on the optimization grid",
                f"spec sha256 {spec.sha256()}",
                "renormalized to 50 ohm, e^(+jwt) convention",
            ],
        )
        sweep_json = {
            "file": f"coarse.s{n}p",
            "points": int(sw["freqs"].size),
            "ghz": [float(sw["freqs"][0] / 1e9), float(sw["freqs"][-1] / 1e9)],
            "passivity_margin_min": float(np.min(sparams.passivity_margin(s50))),
            "steps": {str(k): v for k, v in sw["steps"].items()},
        }
        log(f"coarse sweep: passivity margin {sweep_json['passivity_margin_min']:.2e}")
    st = opt.state
    result = {
        "schema": RESULT_SCHEMA,
        "spec": spec.name,
        "spec_sha256": spec.sha256(),
        "solver": problem.describe(),
        "optimizer": {
            "schedule": opt.schedule.to_json(),
            "iterations": st.iteration,
            "stop_reason": st.stop_reason,
            "final_beta": final["beta_last"],
            "gray": final["gray"],
            "gray_ok": final["gray"] <= 0.01,
            "t_last": opt.history[-1]["t"] if opt.history else None,
        },
        "coarse_binary": achieved(problem, final["evaluation"]),
        "sweep": sweep_json,
        "footprint": {
            "file": FOOTPRINT,
            "islands": len(polys),
            "net_ties": sum(1 for _, t in fp.islands if len(t) >= 2),
        },
        "drc": drc.to_json(),
        "provenance": provenance(problem),
        "wall_s": {"optimization": st.wall_s, "export": time.perf_counter() - t0},
    }
    return result
