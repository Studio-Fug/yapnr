"""Extract a supply rail's copper from a KiCad board and report its IR drop.

Runs under KiCad's Python (``pcbnew``). The solve needs numpy: without it in this
Python (a KiCad build without numpy, as in the container image), the rails' copper
goes to the numeric Python named by ``PNR_PYTHON`` (``python -m pnr.ir_drop --jobs``),
which the regression runner and :mod:`pnr.staged_signal` set. For each ``ir_drop``
entry of the routing rules (:mod:`pnr.power_spec`) it collects the rail's copper in
the graph frame (:func:`pnr.ingest._board_frame`, mm, y up): the **filled** zone
polygons per copper layer (as the last refill left them), the tracks and arcs with
their width, the vias with drill and span, every pad of the net (its copper polygon
on each of its copper layers, the drill of a plated hole) and the stackup's copper
thickness and depth per layer (the board file's ``(stackup ...)`` block). Fixed-block
copper is ordinary copper here, so a macro's feed counts. Then
:func:`pnr.ir_drop.solve` gives the rail's report; ``ir.json`` holds every rail's.

    python -m pnr.ir_extract board.kicad_pcb --rules rules.json --out DIR [--heatmaps]

The exit status is 0 unless an entry with ``hard: true`` fails or is open.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional


def _nm(v) -> float:
    return v / 1e6


def stack_depths(board, path: Optional[str]) -> List[dict]:
    """``[{"name", "z_mm", "copper_mm"}]`` per copper layer, outer to outer: the copper
    centre depth below the top surface, from the stackup block (else the board
    thickness spread evenly, 35 um copper)."""
    import pcbnew

    from pnr.stack import stackup_rows

    cu = list(board.GetEnabledLayers().CuStack())
    names = [pcbnew.BOARD.GetStandardLayerName(lid) for lid in cu]
    rows = []
    if path:
        try:
            rows = stackup_rows(Path(path).read_text(encoding="utf-8"))
        except OSError:
            rows = []
    copper = [r for r in rows if r["type"] == "copper"]
    if [r["name"] for r in copper] == names and all(
        r["thickness_mm"] is not None for r in rows if r["type"] in ("copper", "core", "prepreg")
    ):
        out = []
        z = 0.0
        for r in rows:
            t = r["thickness_mm"] or 0.0
            if r["type"] == "copper":
                out.append(dict(name=r["name"], z_mm=round(z + t / 2, 6), copper_mm=t))
            if r["type"] in ("copper", "core", "prepreg") or r["name"].startswith("dielectric"):
                z += t
        return out
    thickness = _nm(board.GetDesignSettings().GetBoardThickness())
    n = len(names)
    return [
        dict(name=name, z_mm=round(thickness * k / max(1, n - 1), 6), copper_mm=0.035)
        for k, name in enumerate(names)
    ]


def _poly_rings(ps, frame) -> List[dict]:
    out = []
    for o in range(ps.OutlineCount()):
        chain = ps.Outline(o)
        outline = [
            frame.point(chain.CPoint(i).x, chain.CPoint(i).y) for i in range(chain.PointCount())
        ]
        holes = []
        for h in range(ps.HoleCount(o)):
            hc = ps.Hole(o, h)
            holes.append(
                [frame.point(hc.CPoint(i).x, hc.CPoint(i).y) for i in range(hc.PointCount())]
            )
        out.append(dict(outline=outline, holes=holes))
    return out


def _pad_polygon(pad, lid, frame):
    import pcbnew

    ps = pcbnew.SHAPE_POLY_SET()
    try:
        pad.TransformShapeToPolygon(ps, lid, 0, 1000, pcbnew.ERROR_INSIDE)
    except TypeError:  # pragma: no cover - version shim
        pad.TransformShapeToPolygon(ps, lid, 0, 1000, pcbnew.ERROR_INSIDE, False)
    rings = _poly_rings(ps, frame)
    return rings[0] if rings else None


def extract(board, net: str, path: Optional[str] = None) -> Dict:
    """The copper of ``net`` (the :mod:`pnr.ir_drop` input), graph frame."""
    import pcbnew

    from pnr.ingest import _atopile_address, _board_frame

    frame, _outline = _board_frame(board)
    layers = stack_depths(board, path if path is not None else board.GetFileName())
    names = {
        pcbnew.BOARD.GetStandardLayerName(lid): lid for lid in board.GetEnabledLayers().CuStack()
    }
    code = board.FindNet(net).GetNetCode() if board.FindNet(net) else None
    if code is None:
        raise ValueError("net %s is not on the board" % net)
    zones = []
    for z in board.Zones():
        if z.GetIsRuleArea() or z.GetNetCode() != code:
            continue
        for name, lid in names.items():
            if not z.IsOnLayer(lid):
                continue
            filled = z.GetFilledPolysList(lid)
            if filled is None or not filled.OutlineCount():
                continue
            zones.append(dict(layer=name, polygons=_poly_rings(filled, frame)))
    tracks, arcs, vias = [], [], []
    for t in board.GetTracks():
        if t.GetNetCode() != code:
            continue
        kind = t.GetClass()
        if kind == "PCB_VIA":
            v = pcbnew.Cast_to_PCB_VIA(t) if hasattr(pcbnew, "Cast_to_PCB_VIA") else t
            try:
                width = v.GetWidth(pcbnew.F_Cu)
            except TypeError:  # pragma: no cover - version shim
                width = v.GetWidth()
            p = v.GetPosition()
            vias.append(
                dict(
                    at=frame.point(p.x, p.y),
                    drill_mm=_nm(v.GetDrillValue()),
                    diameter_mm=_nm(width),
                    top=pcbnew.BOARD.GetStandardLayerName(v.TopLayer()),
                    bottom=pcbnew.BOARD.GetStandardLayerName(v.BottomLayer()),
                )
            )
            continue
        layer = pcbnew.BOARD.GetStandardLayerName(t.GetLayer())
        a, b = t.GetStart(), t.GetEnd()
        if kind == "PCB_ARC":
            m = t.GetMid()
            arcs.append(
                dict(
                    layer=layer,
                    start=frame.point(a.x, a.y),
                    mid=frame.point(m.x, m.y),
                    end=frame.point(b.x, b.y),
                    width_mm=_nm(t.GetWidth()),
                )
            )
        else:
            tracks.append(
                dict(
                    layer=layer,
                    a=frame.point(a.x, a.y),
                    b=frame.point(b.x, b.y),
                    width_mm=_nm(t.GetWidth()),
                )
            )
    pads = []
    for fp in board.GetFootprints():
        address = _atopile_address(fp)
        for pad in fp.Pads():
            if pad.GetNetCode() != code:
                continue
            on = [name for name, lid in names.items() if pad.IsOnLayer(lid)]
            if not on:
                continue
            poly = _pad_polygon(pad, names[on[0]], frame)
            if poly is None:
                continue
            p = pad.GetPosition()
            row = dict(
                ref=fp.GetReference(),
                pad=pad.GetNumber(),
                address=address,
                at=frame.point(p.x, p.y),
                layers=on,
                polygon=poly["outline"],
            )
            if pad.GetAttribute() == pcbnew.PAD_ATTRIB_PTH:
                row["drill_mm"] = _nm(min(pad.GetDrillSize().x, pad.GetDrillSize().y))
            pads.append(row)
    return dict(net=net, layers=layers, zones=zones, tracks=tracks, arcs=arcs, vias=vias, pads=pads)


def _terminals(copper, entry, key):
    """Indices of ``copper["pads"]`` named by ``entry[key]`` (see pnr.power_spec)."""
    from pnr.power_spec import resolve_part, resolve_terminal

    parts = {}
    for p in copper["pads"]:
        parts.setdefault(p["ref"], dict(address=p.get("address", ""), pads=[]))["pads"].append(
            p["pad"]
        )
    names = []
    value = entry[key]
    if isinstance(value, dict):  # {part: [pads]} (rules carry REF:PAD strings)
        for target, pads in value.items():
            for ref in resolve_part(target, parts):
                names.extend((ref, str(pad)) for pad in pads)
    else:
        for text in value:
            names.extend(resolve_terminal(text, parts))
    out = []
    for ref, pad in names:
        hits = [k for k, p in enumerate(copper["pads"]) if p["ref"] == ref and p["pad"] == pad]
        if not hits:
            raise ValueError("%s pad %s:%s is not on net %s" % (key, ref, pad, entry["net"]))
        out.extend(hits)
    return out


def report(board, rules: Dict, out_dir: Path, path: Optional[str] = None, heatmaps=False) -> Dict:
    """Every ``ir_drop`` rail of ``rules`` on ``board``: ``{net: report}`` (also
    written to ``out_dir/ir.json``)."""
    from pnr.power_spec import rail_current

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    result = {}
    jobs = []
    for entry in rules.get("ir_drop") or []:
        net = entry["net"]
        copper = extract(board, net, path)
        sources = _terminals(copper, entry, "sources")
        if entry.get("sinks", "all") == "all":
            sinks = [k for k in range(len(copper["pads"])) if k not in sources]
        else:
            sinks = _terminals(copper, entry, "sinks")
        current = rail_current(rules, net, entry.get("current_a"))
        if current is None:
            raise ValueError("ir_drop %s: no current_a and no @pnr-current or class current" % net)
        kwargs = dict(
            sources=sources,
            sinks=sinks,
            current_a=current,
            split=entry.get("split", "equal"),
            budget_mohm=entry.get("budget_mohm"),
            budget_mv=entry.get("budget_mv"),
            temperature_c=entry.get("temperature_c", 25.0),
            h=entry.get("h_mm", 0.1),
            two_point=entry.get("two_point", True),
            heatmap=str(out_dir / ("ir-" + net.replace("/", "_"))) if heatmaps else None,
        )
        jobs.append(dict(net=net, copper=copper, kwargs=kwargs, hard=bool(entry.get("hard"))))
    for job, rep in zip(jobs, solve_jobs(jobs, out_dir)):
        copper = job["copper"]
        rep["hard"] = job["hard"]
        rep["sources"] = [
            "%s.%s" % (copper["pads"][k]["ref"], copper["pads"][k]["pad"])
            for k in job["kwargs"]["sources"]
        ]
        result[job["net"]] = rep
    (out_dir / "ir.json").write_text(json.dumps(result, indent=2, sort_keys=True))
    return result


def solve_jobs(jobs: List[Dict], out_dir: Path) -> List[Dict]:
    """:func:`pnr.ir_drop.solve` of each job (``{copper, kwargs}``): here when numpy
    imports, else in the numeric Python ``PNR_PYTHON`` names (``-m pnr.ir_drop --jobs``)."""
    try:
        import numpy  # noqa: F401
    except ImportError:
        python = os.environ.get("PNR_PYTHON")
        if not python:
            raise RuntimeError(
                "pnr.ir_extract: this Python has no numpy; set PNR_PYTHON to a Python with "
                "numpy for the solve"
            )
        path = Path(out_dir) / "ir-jobs.json"
        solved = Path(out_dir) / "ir-solved.json"
        path.write_text(json.dumps(jobs))
        root = str(Path(__file__).resolve().parent.parent)
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            [root] + [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p]
        )
        subprocess.run(
            [python, "-m", "pnr.ir_drop", "--jobs", str(path), "--out", str(solved)],
            check=True,
            env=env,
        )
        reports = json.loads(solved.read_text())
        path.unlink()
        solved.unlink()
        return reports
    from pnr.ir_drop import solve

    return [solve(job["copper"], **job["kwargs"]) for job in jobs]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("pcb")
    ap.add_argument("--rules", required=True)
    ap.add_argument("--out", required=True, help="directory for ir.json (and heat maps)")
    ap.add_argument("--heatmaps", action="store_true")
    ap.add_argument("--copper", help="write the named net's copper JSON here instead")
    ap.add_argument("--net")
    args = ap.parse_args(argv)
    import pcbnew

    board = pcbnew.LoadBoard(args.pcb)
    rules = json.loads(Path(args.rules).read_text())
    if args.copper:
        Path(args.copper).write_text(json.dumps(extract(board, args.net, args.pcb), indent=1))
        return 0
    result = report(board, rules, Path(args.out), args.pcb, args.heatmaps)
    bad = 0
    for net, rep in sorted(result.items()):
        line = "ir_drop %s: %s" % (net, rep["status"])
        if rep.get("r_eff_mohm") is not None:
            line += " (%.3f mOhm, %.3f mV at %.3g A)" % (
                rep["r_eff_mohm"],
                rep["worst_drop_mv"],
                rep["current_a"],
            )
        print(line)
        if rep["hard"] and rep["status"] != "pass":
            bad += 1
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
