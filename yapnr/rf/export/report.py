"""The run directory's exports: polygons, footprint, Touchstone file and result JSON (§10).

`export_design` takes the exported design of an optimization (the best binarized design of the
loop, `driver.Optimizer.finish`, with its minimum width and space repaired on the pixel grid by
`repair`; the result records the pixels it changed and the iteration it came from) and writes

- `footprint.kicad_mod`: the copper islands (`contour`), checked for minimum width and space
  (`drc`), with one pad per port (`kicad`);
- `coarse.sNp`: the full S-matrix of the exported design on the optimization grid (every port
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
from yapnr.rf.export.kicad import Footprint, PortPad, RuleArea, write_footprint
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


def exported_binary(problem, binary: np.ndarray) -> tuple[np.ndarray, dict]:
    """The binary design as exported: `repair.repair` at the spec's minimum width and space
    (in pixels of the problem's grid), and what it changed."""
    from yapnr.rf.export.repair import pixels_for, repair

    spec = problem.spec
    b = (np.asarray(binary) > 0.5).astype(np.float64)
    info = {"changed_pixels": 0, "added": 0, "removed": 0, "rounds": 0}
    if not spec.rules.active:
        return b, info
    pitch = spec.grid.pitch_mm / problem.refine
    mg = problem.material
    out, rounds = repair(
        b,
        mg.fixed,
        mg.fixed_value,
        mg.ring,
        mg.ring_width,
        pixels_for(spec.rules.min_width_mm, pitch),
        pixels_for(spec.rules.min_space_mm, pitch),
    )
    info = {
        "changed_pixels": int(np.sum(out != b)),
        "added": int(np.sum((out > 0.5) & (b < 0.5))),
        "removed": int(np.sum((out < 0.5) & (b > 0.5))),
        "rounds": int(rounds),
    }
    return out, info


# Half-width added to each port's corridor through the track keepout beyond its pad (mm): the
# feed track of the pad's width fits with this much to spare on each side, other copper does not.
CORRIDOR_SPARE_MM = 0.05


def rule_areas(spec, pads: list, margin_mm: float) -> list[RuleArea]:
    """The keepouts of the environment the simulation assumed (board coordinates, mm):

    - the design region grown by the simulated margin: no copper pour, vias or other
      footprints (the footprint's own copper and pads are inside it; KiCad does not test a
      footprint's items against its own rule areas);
    - the margin around the design region, as one strip per side: no tracks either, except in
      a corridor for each port's feed (the pad's width plus `CORRIDOR_SPARE_MM` per side) from
      the region's edge outward. The design region itself is left out of the track keepout so
      that a feed track ending on its pad (round end cap) is not flagged; other tracks there
      would violate the clearance to the footprint's copper anyway.
    """
    x0, x1, y0, y1 = spec.design_region
    m = margin_mm
    X0, X1, Y0, Y1 = x0 - m, x1 + m, y0 - m, y1 + m
    corridors = {"W": [], "E": [], "S": [], "N": []}
    sides = {p.n: p.side for p in spec.ports}
    for pad in pads:
        side = sides[pad.number]
        cx, cy = pad.center
        sx, sy = pad.size
        half = 0.5 * (sy if side in ("W", "E") else sx) + CORRIDOR_SPARE_MM
        c = cy if side in ("W", "E") else cx
        corridors[side].append((c - half, c + half))
    text = f"no other F.Cu within {_mm(m)} mm, a solid ground on the next copper layer"
    out = [
        RuleArea(
            f"yapnr RF keepout: simulated with {text}",
            np.array([(X0, Y0), (X1, Y0), (X1, Y1), (X0, Y1)], dtype=np.float64),
            ("vias", "copperpour", "footprints"),
        )
    ]
    # Strips: (side, along-edge span, the strip's rectangle as a function of a span (a, b)).
    strips = {
        "W": ((Y0, Y1), lambda a, b: [(X0, a), (x0, a), (x0, b), (X0, b)]),
        "E": ((Y0, Y1), lambda a, b: [(x1, a), (X1, a), (X1, b), (x1, b)]),
        "S": ((x0, x1), lambda a, b: [(a, Y0), (b, Y0), (b, y0), (a, y0)]),
        "N": ((x0, x1), lambda a, b: [(a, y1), (b, y1), (b, Y1), (a, Y1)]),
    }
    for side, ((lo, hi), rect) in strips.items():
        cuts = sorted(corridors[side])
        edges = [lo] + [v for c in cuts for v in c] + [hi]
        for a, b in zip(edges[::2], edges[1::2]):
            if b - a > 1e-9:
                out.append(
                    RuleArea(
                        f"yapnr RF track keepout ({side}): {text}",
                        np.array(rect(a, b), dtype=np.float64),
                        ("tracks", "vias", "copperpour", "footprints"),
                    )
                )
    return out


def _mm(v: float) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".")


def footprint_of(problem, binary: np.ndarray, *, name: str | None = None) -> tuple:
    """(Footprint, island polygons in mm) of a binary window design."""
    spec = problem.spec
    pitch = spec.grid.pitch_mm / problem.refine
    x0, x1, y0, y1 = spec.design_region
    isl = islands(np.asarray(binary) > 0.5)
    pads = []
    pad_px = port_pads(problem)
    for n, sx, sy in pad_px:
        cx = x0 + 0.5 * (sx.start + sx.stop) * pitch
        cy = y0 + 0.5 * (sy.start + sy.stop) * pitch
        pads.append(
            PortPad(n, (cx, cy), ((sx.stop - sx.start) * pitch, (sy.stop - sy.start) * pitch))
        )
    port_only = list(pads)
    fab = []
    centres = [(n, 0.5 * (sx.start + sx.stop), 0.5 * (sy.start + sy.stop)) for n, sx, sy in pad_px]
    number = len(pad_px)
    for el in spec.lumped:
        (bx0, bx1), (by0, by1) = sorted(el.x_mm), sorted(el.y_mm)
        fab.append((bx0, bx1, by0, by1))
        for (px0, px1), (py0, py1) in el.pads():
            number += 1
            cx, cy = 0.5 * (px0 + px1), 0.5 * (py0 + py1)
            pads.append(PortPad(number, (cx, cy), (px1 - px0, py1 - py0)))
            centres.append((number, (cx - x0) / pitch, (cy - y0) / pitch))
    shapes = []
    for island in isl:
        touched = sorted(n for n, u, v in centres if point_in_loop((u, v), island.polygon))
        poly_mm = np.column_stack(
            [x0 + island.polygon[:, 0] * pitch, y0 + island.polygon[:, 1] * pitch]
        )
        shapes.append((poly_mm, touched))
    st = spec.stackup
    sha = spec.sha256()
    parts = "".join(
        f"; {el.name}: {el.ohms:g} ohm between pads {len(pad_px) + 2 * k + 1} and "
        f"{len(pad_px) + 2 * k + 2}"
        for k, el in enumerate(spec.lumped)
    )
    descr = (
        f"yapnr RF inverse design '{spec.name}': microstrip copper on er {st.er:g}, "
        f"tan_delta {st.tan_delta:g}, h {st.h_mm:g} mm with a solid ground on the next copper "
        f"layer{parts}; spec sha256 {sha}"
    )
    fp = Footprint(
        name=name or f"RF_{spec.name}",
        origin=(0.5 * (x0 + x1), 0.5 * (y0 + y1)),
        region=(x0, x1, y0, y1),
        pads=pads,
        islands=shapes,
        description=descr,
        seed=sha,
        rule_areas=rule_areas(spec, port_only, problem.domain.spec.margin * 1e3),
        fab_rects=fab,
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
    """The exported binary design's values at the objective frequencies."""
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
    if "exported" in final:
        binary, repaired = final["exported"], final["repair"]
    else:
        binary, repaired = exported_binary(problem, final["binary"])
    if repaired["changed_pixels"]:
        log(f"width/space repair changed {repaired['changed_pixels']} pixels")
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
            # The exported design: the best binarized design of the loop (driver.finish).
            "export_iteration": final.get("export_iteration"),
            "t_binary_exported": float(np.max(final["evaluation"].values)),
            "t_binary_last": final.get("t_binary_last"),
            "best_iteration_tracked": final.get("best_iteration_tracked"),
        },
        "coarse_binary": achieved(problem, final["evaluation"]),
        "sweep": sweep_json,
        "footprint": {
            "file": FOOTPRINT,
            "islands": len(polys),
            "net_ties": sum(1 for _, t in fp.islands if len(t) >= 2),
            "repair": repaired,
        },
        "drc": drc.to_json(),
        "provenance": provenance(problem),
        "wall_s": {"optimization": st.wall_s, "export": time.perf_counter() - t0},
    }
    return result
