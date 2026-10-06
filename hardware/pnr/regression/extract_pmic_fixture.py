"""Extract the real pad and courtyard geometry of a plane_partition region into a frozen JSON
fixture that ``pnr.plane_partition.partition`` can replay without KiCad or torch
(``docs/plane-partition.md``'s example and its tests load the fixture this writes, not this
script). Run under KiCad's Python (needs ``pcbnew`` and ``numpy``, not available from this
repository's own hermetic interpreter):

    KPY=/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/3.9/bin/python3.9
    "$KPY" hardware/pnr/regression/extract_pmic_fixture.py \\
        --candidate PATH/TO/cand/34c7ed35931c --out hardware/pnr/regression/fixtures/pmic_pour_34c7ed35931c.json

``--candidate`` is a hierarchical-synthesis library candidate directory (power_block.py's
``eval`` output: ``board.kicad_pcb`` -- the block placed, routed and poured on its own small
board -- and ``rules.json``, the rules that routed it, including its own ``plane_partition``
entry). ``board.kicad_pcb`` is used only for its placement (components, pads, nets): this
script reads no copper from it, and its own route is not replayed, only re-derived from the
candidate's own rules the way ``pnr.plane_partition._outer`` would build the inputs. Clearances
approximate the detailed router's class resolution from the candidate's own rules.json; the
corridor pitch stands in for the detailed router's fanout grid, which is not available outside
a full route -- see the written fixture's own ``meta.note`` and docs/plane-partition.md.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))


def pin_positions(comp):
    """Absolute (x, y) of each pad: component pose + rotated pad offset (as
    ``pnr.place.geometry.pin_positions``, reimplemented here since that module's package
    pulls in torch, which this script's interpreter -- KiCad's own -- does not have)."""
    th = math.radians(comp.rot)
    ct, st = math.cos(th), math.sin(th)
    out = []
    for pad in comp.pads:
        ox, oy = pad.offset
        rx = ox * ct - oy * st
        ry = ox * st + oy * ct
        out.append((comp.pos[0] + rx, comp.pos[1] + ry))
    return out


def extract(board_path: Path, rules: dict) -> dict:
    from pnr.fixed_block import point_in_polygon
    from pnr.ingest import load
    from pnr.plane_partition import region_polygon
    from pnr.power_spec import rail_current

    graph = load(str(board_path))
    entry = rules["plane_partition"][0]
    nets = entry["nets"]
    region = region_polygon(graph, entry["region"])

    def inside(p):
        return point_in_polygon(p, region)

    default_clear = float(rules.get("default_clearance_mm") or 0.2)
    fab = rules.get("fab") or {}
    fab_clear = float(fab.get("clearance_mm", default_clear))
    classes = {}
    for c in rules.get("net_classes") or []:
        if c.get("clearance_mm") is not None:
            for n in c.get("nets") or []:
                classes[n] = float(c["clearance_mm"])
    rail_clear = max([fab_clear] + [classes.get(n, 0.0) for n in nets])

    def gap_to(net):
        return max(rail_clear, classes.get(net, 0.0))

    terms = {n: [] for n in nets}
    blocked = []
    keepouts = []
    foreign = []
    bodies = []

    for comp in graph.components:
        if inside(comp.pos):
            w, h = comp.courtyard
            if int(round(comp.rot)) % 180 == 90:
                w, h = h, w
            x, y = comp.pos
            bodies.append(
                [
                    [x - w / 2, y - h / 2],
                    [x + w / 2, y - h / 2],
                    [x + w / 2, y + h / 2],
                    [x - w / 2, y + h / 2],
                ]
            )
        swap = int(round(comp.rot)) % 180 == 90
        for pad, (px, py) in zip(comp.pads, pin_positions(comp)):
            net = pad.net
            w, h = pad.size
            if swap:
                w, h = h, w
            half = max(w, h) / 2
            pad_id = "%s.%s" % (comp.ref, pad.name)
            if pad.through_hole:
                if net in terms and inside((px, py)):
                    terms[net].append(dict(name=pad_id, kind="via", at=[px, py], radius=half))
                elif net not in terms:
                    blocked.append([[px, py], half + gap_to(net)])
                continue
            if net in terms:
                if inside((px, py)):
                    terms[net].append(
                        dict(name=pad_id, kind="land", at=[px, py], radius=half, size=[w, h])
                    )
                continue
            g = gap_to(net)
            ring = [
                [px - w / 2 - g, py - h / 2 - g],
                [px + w / 2 + g, py - h / 2 - g],
                [px + w / 2 + g, py + h / 2 + g],
                [px - w / 2 - g, py + h / 2 + g],
            ]
            keepouts.append([[ring], []])  # blocked_polygons rows: (rings, allowed nets)
            if net and inside((px, py)):
                foreign.append(ring)

    currents = {}
    for n in nets:
        c = rail_current(rules, n, (entry.get("currents") or {}).get(n))
        if c:
            currents[n] = c
    budgets = dict(entry.get("budgets_mohm") or {})
    for ir in rules.get("ir_drop") or []:
        n = ir["net"]
        if n in terms and n not in budgets:
            amps = currents.get(n) or ir.get("current_a")
            if ir.get("budget_mohm"):
                budgets[n] = ir["budget_mohm"]
            elif ir.get("budget_mv") and amps:
                budgets[n] = ir["budget_mv"] / amps

    sources = {n: text.replace(":", ".", 1) for n, text in (entry.get("sources") or {}).items()}
    track_w = float(fab.get("track_width_mm", 0.15))
    return dict(
        width=graph.outline.width,
        height=graph.outline.height,
        entry=dict(entry, region=[list(p) for p in region]),
        terms=terms,
        blocked=blocked,
        blocked_polygons=keepouts,
        currents=currents,
        budgets_mohm=budgets,
        sources=sources,
        copper_mm=0.035,
        edge_mm=float(fab.get("edge_clearance_mm", 0.3)),
        via_drill_mm=float(fab.get("via_drill_mm", 0.2)),
        fill_min_mm=track_w,
        foreign_lands=foreign,
        corridor_mm=track_w + 2 * rail_clear + (math.sqrt(2) + 2) * 0.1,
        corridor_keep_mm=track_w / 2 + rail_clear,
        bodies=bodies,
    )


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--candidate", required=True, type=Path, help="a library candidate directory")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--label", default="", help="meta.source text (what this candidate is)")
    ap.add_argument("--refs", nargs="*", default=[], help="meta.block_refs")
    args = ap.parse_args(argv)

    rules = json.loads((args.candidate / "rules.json").read_text())
    fixture = extract(args.candidate / "board.kicad_pcb", rules)
    fixture["meta"] = dict(
        source=args.label or ("extracted from %s" % args.candidate.name),
        block_refs=sorted(args.refs),
        note=(
            "Pad and courtyard geometry read from the candidate's board.kicad_pcb (placed and "
            "routed, read here for its placement only -- no copper is read from it). "
            "Clearances use this candidate's own rules.json (fab.clearance_mm, "
            "default_clearance_mm, net_classes); the corridor pitch (0.1 mm) stands in for the "
            "detailed router's fanout grid, which this standalone extraction does not have -- "
            "see docs/plane-partition.md."
        ),
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(fixture, indent=1, sort_keys=True) + "\n")
    print(
        "wrote",
        args.out,
        "\nterms",
        {k: len(v) for k, v in fixture["terms"].items()},
        "\nkeepouts",
        len(fixture["blocked_polygons"]),
        "bodies",
        len(fixture["bodies"]),
        "foreign",
        len(fixture["foreign_lands"]),
        "\ncurrents",
        fixture["currents"],
        "\nbudgets",
        fixture["budgets_mohm"],
    )


if __name__ == "__main__":
    main()
