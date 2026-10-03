"""Verify planned fanouts on a scratch copy of a board (KiCad's Python, pcbnew).

The plan (:mod:`.planner`) is exact by construction; this checks it with the tools
that judge the board. Every planned item is added to a copy of the board, as
writeback would (the fanout's via classes, locked), then:

* the native :class:`pnr.native_electrical.Oracle` judges each track and via
  against the board's own copper, rule areas, drills and edges, item by item (its
  copper gaps carry a 1 um margin, so an item planned at exactly the clearance is
  reported there: ``oracle_exact`` counts them);
* KiCad's DRC (``kicad-cli pcb drc``, the board's own rules beside it) runs on the
  copy; unconnected items are ignored (the router has not run), every other
  finding is counted by type, and those touching the fanned-out part's window (its
  courtyard grown by 2 mm) are listed.

The original board is never written.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
from collections import Counter
from pathlib import Path
from typing import Dict, Optional


def _frame_point(k, frame, p):
    return k.VECTOR2I(round(frame._left + p[0] * 1e6), round(frame._bottom - p[1] * 1e6))


def apply(board, plans, rules):
    """Add every planned track and via to ``board`` (pcbnew); returns the items."""
    import pcbnew as k

    from pnr.ingest import _board_frame

    frame, _ = _board_frame(board)
    codes = {}
    for name, net in board.GetNetsByName().items():
        codes[str(name)] = net.GetNetCode()
    added = []
    for plan in plans:
        lock = plan.get("lock", True)
        for net, layer, a, b, w in plan["copper"]["tracks"]:
            t = k.PCB_TRACK(board)
            t.SetLayer(board.GetLayerID(layer))
            t.SetStart(_frame_point(k, frame, a))
            t.SetEnd(_frame_point(k, frame, b))
            t.SetWidth(round(w * 1e6))
            t.SetNetCode(codes.get(net, 0))
            t.SetLocked(lock)
            board.Add(t)
            added.append(("track", net, layer, a, b, w, t))
        seen = set()
        for net, x, y, d, h in plan["copper"]["vias"]:
            if (net, x, y) in seen:
                continue  # two balls of one net share the via
            seen.add((net, x, y))
            v = k.PCB_VIA(board)
            v.SetPosition(_frame_point(k, frame, (x, y)))
            v.SetViaType(k.VIATYPE_THROUGH)
            v.SetLayerPair(k.F_Cu, k.B_Cu)
            v.SetFrontWidth(round(d * 1e6))
            v.SetDrill(round(h * 1e6))
            v.SetNetCode(codes.get(net, 0))
            v.SetLocked(lock)
            board.Add(v)
            added.append(("via", net, None, (x, y), None, (d, h), v))
    board.BuildConnectivity()
    return added


def oracle_check(source_board, plans, rules):
    """Judge each planned item with the native Oracle on ``source_board`` (before
    any is added), reserving it once judged: ``{checked, rejected: [...]}``."""
    import pcbnew as k

    from pnr.ingest import _board_frame
    from pnr.native_electrical import Oracle

    frame, _ = _board_frame(source_board)
    oracle = Oracle(source_board, rules)

    def mm(p):
        q = _frame_point(k, frame, p)
        return (q.x / 1e6, q.y / 1e6)

    rejected = []
    checked = 0
    for plan in plans:
        for net, layer, a, b, w in plan["copper"]["tracks"]:
            la = source_board.GetLayerID(layer)
            checked += 1
            if not oracle.clear(net, la, mm(a), mm(b), w):
                rejected.append(dict(item="track", net=net, layer=layer, a=a, b=b))
        for net, x, y, d, h in plan["copper"]["vias"]:
            checked += 1
            if not oracle.via(net, mm((x, y)), d, h):
                rejected.append(dict(item="via", net=net, at=[x, y], size=[d, h]))
        for net, layer, a, b, w in plan["copper"]["tracks"]:
            oracle.reserve_track(net, source_board.GetLayerID(layer), mm(a), mm(b), w)
        for net, x, y, d, h in plan["copper"]["vias"]:
            oracle.reserve_via(net, mm((x, y)), d, h)
    return dict(checked=checked, rejected=rejected)


def drc(board_path: Path, kicad_cli: str, window, timeout=900) -> Dict:
    """KiCad DRC of ``board_path``: counts by type (unconnected items ignored) and
    the findings with an item inside ``window`` (pcbnew mm box)."""
    out = board_path.with_suffix(".drc.json")
    subprocess.run(
        [
            kicad_cli,
            "pcb",
            "drc",
            str(board_path),
            "--format",
            "json",
            "--severity-all",
            "--output",
            str(out),
        ],
        check=True,
        capture_output=True,
        timeout=timeout,
    )
    report = json.loads(out.read_text())
    x0, y0, x1, y1 = window
    types = Counter()
    inside = []
    for v in report.get("violations", []):
        types[v["type"]] += 1
        for item in v.get("items", []):
            pos = item.get("pos") or {}
            if x0 <= pos.get("x", math.inf) <= x1 and y0 <= pos.get("y", math.inf) <= y1:
                inside.append(dict(type=v["type"], description=v.get("description", "")[:200]))
                break
    return dict(
        violations=dict(sorted(types.items())),
        in_window=len(inside),
        in_window_types=dict(sorted(Counter(v["type"] for v in inside).items())),
        in_window_items=inside[:50],
        unconnected_ignored=len(report.get("unconnected_items", [])),
    )


def verify(board_path, rules: Dict, out_dir, *, kicad_cli: Optional[str] = None, fixed=None):
    """Plan every declared fanout on ``board_path``'s graph, add it to a copy in
    ``out_dir`` and judge it (see the module docstring). Returns the report."""
    import pcbnew as k

    from pnr.ingest import build_graph

    from .planner import classify, plan

    board_path, out_dir = Path(board_path), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    source = k.LoadBoard(str(board_path))
    graph = build_graph(source)
    layers, drops, signals = classify(graph, rules)
    plans = [
        plan(
            graph,
            rules,
            spec,
            grid_layers=layers,
            plane_nets=drops,
            signal_nets=signals,
            fixed_copper=fixed,
        )
        for spec in rules.get("fanouts") or []
    ]
    for p in plans:
        (out_dir / ("fanout-%s.json" % p["name"])).write_text(json.dumps(p, indent=1))
    report = dict(plans={p["name"]: p["diagnostics"] for p in plans})
    report["oracle"] = oracle_check(source, plans, rules)
    scratch = out_dir / (board_path.stem + "-fanout.kicad_pcb")
    for suffix in (".kicad_pro", ".kicad_dru"):
        if board_path.with_suffix(suffix).exists():
            shutil.copyfile(board_path.with_suffix(suffix), scratch.with_suffix(suffix))
    board = k.LoadBoard(str(board_path))
    added = apply(board, plans, rules)
    k.SaveBoard(str(scratch), board)
    report["items_added"] = len(added)
    if kicad_cli:
        boxes = []
        for p in plans:
            fp = board.FindFootprintByReference(p["ref"])
            box = fp.GetCourtyard(k.F_CrtYd if not fp.IsFlipped() else k.B_CrtYd).BBox()
            if box.GetWidth() <= 0:
                box = fp.GetBoundingBox(False)
            boxes.append(
                (
                    box.GetLeft() / 1e6 - 2.0,
                    box.GetTop() / 1e6 - 2.0,
                    box.GetRight() / 1e6 + 2.0,
                    box.GetBottom() / 1e6 + 2.0,
                )
            )
        window = (
            (
                min(b[0] for b in boxes),
                min(b[1] for b in boxes),
                max(b[2] for b in boxes),
                max(b[3] for b in boxes),
            )
            if boxes
            else (0, 0, 0, 0)
        )
        report["drc"] = drc(scratch, kicad_cli, window)
    (out_dir / "verify.json").write_text(json.dumps(report, indent=1, default=str))
    return report
