"""Fanout exit bands and bottom-site constraint edits for the radar60 placement (stdlib only).

``integrate.py prepare`` plans U1's fanout before placement (U1 is fixed and the RF macro's
copper is known: :func:`pnr.fanout.planner.cached_plan`) and turns the plan into placement
inputs:

- :func:`exit_strips`: every ball that escapes on the surface (F.Cu) gets a strip ``width_mm``
  wide, from U1's courtyard edge ``beyond_courtyard_mm`` outwards, centred on the plan's exit
  point and square to the edge it leaves across. The router takes each escape from the first
  free grid cell beyond the exit and may turn within 2 mm (pnr.fanout), so a part in the strip
  is what made the stage 3a trial's balls lose their access cell.
- :func:`judge_slots`: a strip may hold only a ball-anchored slot (floorplan ``slot: true``)
  whose part sits on that ball's own net (the crystal's load caps at B15/C15, the QSPI clock
  resistor at R12): such a strip is not emitted, the slot is its net's own path. A slot that
  meets another net's strip is an input error, refused by name.
- :func:`merge_strips`: strips on one edge that touch or overlap become one placement
  ``keepout`` rectangle (a v0 polygon keepout is judged as its bounding box, so each piece stays
  a rectangle).
- :func:`drop_site_parts`: parts the plan gave a bottom site are fixed there by the engine
  (pnr.fanout.bottom.derive); their top-side regions and fixed rotations are removed.

Frames: board mm, origin at the lower-left corner, +y north (the plan's ``exit`` points are in
this frame). Rectangles are ``[x0, y0, x1, y1]``.
"""

from __future__ import annotations

import math
from typing import Dict, Iterable, List, Sequence, Tuple

EDGES = ("south", "west", "north", "east")


def _edge(outward: Sequence[float]) -> str:
    """The compass edge a direction leaves across (its dominant axis)."""
    dx, dy = outward
    if abs(dy) >= abs(dx):
        return "south" if dy < 0 else "north"
    return "west" if dx < 0 else "east"


def exit_strips(
    terminals: Dict[str, dict],
    courtyard: Sequence[float],
    width_mm: float,
    beyond_mm: float,
    layer: str = "F.Cu",
) -> List[dict]:
    """One strip per surface exit on ``layer``: ``{ball, net, edge, exit, rect}``.

    ``terminals`` is the plan's ``terminals`` (``kind``, ``layer``, ``exit``, ``outward``);
    ``courtyard`` the fanned-out part's courtyard rectangle. A strip runs from the courtyard
    edge (touching it: a keepout that only touches the fixed part is no violation) to
    ``beyond_mm`` past it, ``width_mm`` wide about the exit's coordinate along the edge."""
    x0, y0, x1, y1 = courtyard
    half = width_mm / 2.0
    out = []
    for ball, t in sorted(terminals.items()):
        if t.get("kind") not in ("surface", "dogbone") or t.get("layer") != layer:
            continue
        if not t.get("exit") or not t.get("outward"):
            continue
        ex, ey = t["exit"]
        edge = _edge(t["outward"])
        if edge == "south":
            rect = [ex - half, y0 - beyond_mm, ex + half, y0]
        elif edge == "north":
            rect = [ex - half, y1, ex + half, y1 + beyond_mm]
        elif edge == "west":
            rect = [x0 - beyond_mm, ey - half, x0, ey + half]
        else:
            rect = [x1, ey - half, x1 + beyond_mm, ey + half]
        out.append(
            dict(
                ball=ball,
                net=t.get("net"),
                edge=edge,
                exit=[round(ex, 4), round(ey, 4)],
                rect=[round(v, 4) for v in rect],
            )
        )
    return out


def overlap(a: Sequence[float], b: Sequence[float], eps: float = 1e-6) -> bool:
    """True when two rectangles share area (touching edges do not)."""
    return a[0] < b[2] - eps and b[0] < a[2] - eps and a[1] < b[3] - eps and b[1] < a[3] - eps


def judge_slots(
    strips: List[dict], slots: Iterable[dict]
) -> Tuple[List[dict], List[dict], List[str]]:
    """``(kept strips, own-path strips, errors)``.

    ``slots``: ``{name, rect, nets}`` (``nets``: the nets of the slot's parts' pads). A strip
    whose ball's net is a slot's net and that meets the slot is the slot's own path (dropped,
    reported). A slot that meets a strip of another net is an error naming both."""
    slots = list(slots)
    kept, own, errors = [], [], []
    for s in strips:
        hits = [q for q in slots if overlap(s["rect"], q["rect"])]
        mine = [q["name"] for q in hits if s["net"] in q["nets"]]
        foreign = [q["name"] for q in hits if s["net"] not in q["nets"]]
        for name in foreign:
            errors.append(
                "slot %s meets the exit band of %s (%s) at %s"
                % (name, s["ball"], s["net"], s["rect"])
            )
        if mine:
            own.append(dict(s, slots=mine))
        else:
            kept.append(s)
    return kept, own, errors


def merge_strips(
    strips: List[dict], name_prefix: str = "exit", join_gap: float = 0.5
) -> List[dict]:
    """Strips on one edge whose spans along the edge come within ``join_gap`` (default: a strip
    width; no part fits such a gap) become one rectangle: ``{name, edge, balls, rect}``,
    ordered by edge and position."""
    out = []
    for edge in EDGES:
        mine = [s for s in strips if s["edge"] == edge]
        axis = 0 if edge in ("south", "north") else 1
        mine.sort(key=lambda s: s["rect"][axis])
        groups: List[List[dict]] = []
        for s in mine:
            reach = max(q["rect"][axis + 2] for q in groups[-1]) if groups else None
            if groups and s["rect"][axis] < reach + join_gap - 1e-9:
                groups[-1].append(s)
            else:
                groups.append([s])
        for k, g in enumerate(groups, 1):
            rect = [
                min(s["rect"][0] for s in g),
                min(s["rect"][1] for s in g),
                max(s["rect"][2] for s in g),
                max(s["rect"][3] for s in g),
            ]
            out.append(
                dict(
                    name="%s_%s_%d" % (name_prefix, edge, k),
                    edge=edge,
                    balls=sorted(s["ball"] for s in g),
                    rect=[round(v, 4) for v in rect],
                )
            )
    return out


def keepout_entries(merged: List[dict]) -> List[dict]:
    """The placement ``keepout`` entries (yapnr constraints) of merged strips."""
    return [
        {
            "name": m["name"],
            "polygon": [
                [m["rect"][0], m["rect"][1]],
                [m["rect"][2], m["rect"][1]],
                [m["rect"][2], m["rect"][3]],
                [m["rect"][0], m["rect"][3]],
            ],
        }
        for m in merged
    ]


def glob_literal(path: str) -> str:
    """An instance path as an fnmatch pattern that matches only itself (``c[0]`` -> ``c[[]0]``)."""
    return "".join("[%s]" % c if c in "[*?" else c for c in path)


def drop_site_parts(doc: dict, addresses: Iterable[str]) -> Dict[str, List[str]]:
    """Remove the parts at ``addresses`` from ``doc``'s regions (a region left empty goes),
    ``side`` lists and ``orientation`` keys, in place: the fanout's bottom sites fix them.
    Returns what was removed, by section."""
    keys = {"@" + glob_literal(a) for a in addresses} | {"@" + a for a in addresses}
    removed: Dict[str, List[str]] = {"region": [], "orientation": [], "side": []}
    regions = []
    for r in doc.get("region") or []:
        refs = [x for x in r.get("refs", []) if x not in keys]
        gone = [x for x in r.get("refs", []) if x in keys]
        if gone:
            removed["region"] += ["%s:%s" % (r["name"], x) for x in gone]
        if refs:
            regions.append(dict(r, refs=refs))
        elif not gone:
            regions.append(r)
    if "region" in doc:
        doc["region"] = regions
    for k in list((doc.get("orientation") or {}).keys()):
        if k in keys:
            del doc["orientation"][k]
            removed["orientation"].append(k)
    for side, refs in list((doc.get("side") or {}).items()):
        left = [x for x in refs if x not in keys]
        removed["side"] += [x for x in refs if x in keys]
        doc["side"][side] = left
    return removed


def rect_area(r: Sequence[float]) -> float:
    return max(0.0, r[2] - r[0]) * max(0.0, r[3] - r[1])


def distance_to_rect(p: Sequence[float], r: Sequence[float]) -> float:
    dx = max(r[0] - p[0], 0.0, p[0] - r[2])
    dy = max(r[1] - p[1], 0.0, p[1] - r[3])
    return math.hypot(dx, dy)
