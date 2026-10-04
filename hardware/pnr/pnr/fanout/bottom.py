"""Bottom-side decoupling sites under an area array (``fanout.bottom_sites``).

After the fanout is planned, each listed part (in priority order) gets the pose on
the bottom side, inside the array's shadow, where every pad clears the fanout's
vias and bottom-layer copper and lies within ``max_stub_mm`` of a fanout via of its
own net (the router joins the two on the bottom layer). Candidates are the part's
footprint at each allowed rotation on a lattice an eighth of the ball pitch apart,
centred on the array; the least total stub (pad edge to via edge) wins, ties by
distance to the array centre. Chosen sites do not overlap each other's courtyards.

``zone: interior`` keeps sites inside the array's outermost fully vacant ring (else
inside the rings from 2 inwards), so the outer rings' bottom-layer escape lanes stay
free; ``shadow`` allows the whole array. The sites become ``fixed`` poses on the
bottom side (:func:`derive`); the rest of the shadow is reported as keep-out pieces
(placement keeps other bottom parts out of the array by the side policy).
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

from .geom import Land, Pose
from .sites import MARGIN


def _zone(lat, zone: str):
    """Half-lattice box ``(a0, b0, a1, b1)`` the sites must stay inside."""
    a0, b0, a1, b1 = lat.hull
    if zone == "shadow":
        return (a0 - 1, b0 - 1, a1 + 1, b1 + 1)
    vacant = set(lat.vacant())
    deepest = None
    for k in range(min(lat.cols, lat.rows) // 2):
        ring = [
            (c, r)
            for c in range(k, lat.cols - k)
            for r in range(k, lat.rows - k)
            if min(c - k, r - k, lat.cols - 1 - k - c, lat.rows - 1 - k - r) == 0
        ]
        if ring and all(p in vacant for p in ring):
            deepest = k
            break  # the outermost fully vacant ring
    k = deepest if deepest is not None else 2
    return (2 * k, 2 * k, 2 * (lat.cols - 1 - k), 2 * (lat.rows - 1 - k))


def sites(graph, spec: Dict, plan: Dict, lat, pose: Pose, rules: Dict) -> Dict:
    """``{"sites": {ref: {...}}, "unplaced": {ref: reason}, "keepouts": [...]}``."""
    from pnr.fab_profile import geometry

    bottom = spec["bottom_sites"]
    g = geometry(rules)
    clearance = float((rules.get("fab") or {}).get("clearance_mm", 0.13))
    keep_via = max(clearance, g.via_to_smd_pad or 0.0)
    # Two nets keep the larger of their class clearances (as the planner's Model).
    net_clearance: Dict[str, float] = {}
    for c in rules.get("net_classes", []):
        if c.get("clearance_mm"):
            for n in c.get("nets", []):
                net_clearance[n] = max(net_clearance.get(n, 0.0), float(c["clearance_mm"]))

    def cl(net):
        return max(clearance, net_clearance.get(net, 0.0))

    za0, zb0, za1, zb1 = _zone(lat, bottom["zone"])
    x0, y0 = lat.point(za0, zb0)
    x1, y1 = lat.point(za1, zb1)
    vias = [(v[0], pose.to_local((v[1], v[2])), v[3]) for v in plan["copper"]["vias"]]
    bottom_layer = plan["layers"][-1] if plan["layers"][0] != plan["layers"][-1] else None
    tracks = [
        (t[0], pose.to_local(tuple(t[2])), pose.to_local(tuple(t[3])), t[4])
        for t in plan["copper"]["tracks"]
        if t[1] == bottom_layer
    ]
    step = min(lat.px, lat.py) / 8.0
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    nx = int(math.floor((x1 - x0) / 2 / step))
    ny = int(math.floor((y1 - y0) / 2 / step))
    chosen: Dict[str, Dict] = {}
    unplaced: Dict[str, str] = {}
    boxes: List[Tuple[float, float, float, float]] = []
    comp = graph.component(spec["ref"])
    for ref in bottom["parts"]:
        part = graph.component(ref)
        # Pad offsets in the bottom side's frame (KiCad flips about x).
        sign = 1.0 if part.side == "bottom" else -1.0
        pads = [
            (p.name, p.net, (p.offset[0], sign * p.offset[1]), p.size, p.land_corner)
            for p in part.pads
        ]
        hw, hh = part.courtyard[0] / 2, part.courtyard[1] / 2
        best = None
        for rot in bottom["rotations"]:
            local_rot = (rot - comp.rot) % 360  # the site's rotation in the array's frame
            r = Pose((0.0, 0.0), local_rot)
            swap = int(round(local_rot)) % 180 == 90
            ex, ey = (hh, hw) if swap else (hw, hh)
            for i in range(-nx, nx + 1):
                for j in range(-ny, ny + 1):
                    at = (cx + i * step, cy + j * step)
                    if at[0] - ex < x0 - 1e-9 or at[0] + ex > x1 + 1e-9:
                        continue
                    if at[1] - ey < y0 - 1e-9 or at[1] + ey > y1 + 1e-9:
                        continue
                    box = (at[0] - ex, at[1] - ey, at[0] + ex, at[1] + ey)
                    if any(
                        not (box[2] <= b[0] or b[2] <= box[0] or box[3] <= b[1] or b[3] <= box[1])
                        for b in boxes
                    ):
                        continue
                    total = 0.0
                    stubs = {}
                    ok = True
                    for name, net, off, size, corner in pads:
                        p = r.to_board(off)
                        centre = (at[0] + p[0], at[1] + p[1])
                        w, h = (size[1], size[0]) if swap else size
                        land = Land(centre, w / 2, h / 2, corner or 0.0, net, name)
                        own = math.inf
                        for vnet, c, d in vias:
                            gap = land.distance(c, c) - d / 2
                            keep = keep_via if vnet == net else max(keep_via, cl(net), cl(vnet))
                            if gap < keep + MARGIN:
                                ok = False
                                break
                            if vnet == net and net:
                                own = min(own, gap)
                        if not ok:
                            break
                        for tnet, a, b, tw in tracks:
                            keep = max(cl(net), cl(tnet))
                            if tnet != net and land.distance(a, b) < tw / 2 + keep + MARGIN:
                                ok = False
                                break
                        if not ok:
                            break
                        if net and own > bottom["max_stub_mm"] + 1e-9:
                            ok = False
                            break
                        stubs[name] = round(own, 4) if net else None
                        total += own if net else 0.0
                    if not ok:
                        continue
                    key = (round(total, 6), round(math.hypot(at[0] - cx, at[1] - cy), 6), rot, i, j)
                    if best is None or key < best[0]:
                        best = (key, at, rot, box, stubs)
        if best is None:
            unplaced[ref] = "no pose clears the fanout with every pad within %.2f mm of its via" % (
                bottom["max_stub_mm"]
            )
            continue
        _key, at, rot, box, stubs = best
        boxes.append(box)
        xy = pose.to_board(at)
        chosen[ref] = dict(
            at=[round(xy[0], 4), round(xy[1], 4)],
            rot=rot % 360,
            side="bottom",
            stubs=stubs,
        )
    keepouts = _pieces((x0, y0, x1, y1), boxes, pose)
    return dict(sites=chosen, unplaced=unplaced, keepouts=keepouts)


def _pieces(zone, boxes, pose):
    """The zone minus the site boxes as board-frame rectangles (a horizontal split)."""
    x0, y0, x1, y1 = zone
    ys = sorted({y0, y1} | {b[1] for b in boxes} | {b[3] for b in boxes})
    out = []
    for ya, yb in zip(ys, ys[1:]):
        if yb - ya < 1e-6:
            continue
        cuts = sorted((b[0], b[2]) for b in boxes if b[1] < yb - 1e-9 and b[3] > ya + 1e-9)
        x = x0
        for c0, c1 in cuts:
            if c0 > x + 1e-6:
                out.append((x, ya, c0, yb))
            x = max(x, c1)
        if x1 > x + 1e-6:
            out.append((x, ya, x1, yb))
    rects = []
    for a0, b0, a1, b1 in out:
        p, q = pose.to_board((a0, b0)), pose.to_board((a1, b1))
        rects.append(
            [
                round(min(p[0], q[0]), 4),
                round(min(p[1], q[1]), 4),
                round(max(p[0], q[0]), 4),
                round(max(p[1], q[1]), 4),
            ]
        )
    return rects


def derive(graph, constraints, rules: Optional[Dict]):
    """``constraints`` with a ``fixed`` bottom pose for every site of every declared
    fanout's ``bottom_sites`` (the same object when none is declared)."""
    specs = [f for f in (rules or {}).get("fanouts") or [] if f.get("bottom_sites")]
    if not specs:
        return constraints
    import copy

    from pnr.constraints import Constraint, Enforcement

    from .planner import cached_plan, classify

    out = copy.copy(constraints)
    out.constraints = list(constraints.constraints)
    reports = {}
    posed = _posed(graph, constraints, [f["ref"] for f in specs])
    for spec in specs:
        layers, drops, signals = classify(posed, rules)
        plan = cached_plan(
            posed, rules, spec, grid_layers=layers, plane_nets=drops, signal_nets=signals
        )
        found = plan.get("bottom") or {}
        reports[spec["name"]] = found
        for ref, site in sorted(found.get("sites", {}).items()):
            out.constraints = [
                c for c in out.constraints if not (c.kind == "fixed" and ref in c.refs)
            ]
            out.constraints.append(
                Constraint(
                    kind="fixed",
                    enforcement=Enforcement.HARD,
                    refs=(ref,),
                    params=dict(
                        edge=None,
                        align=None,
                        rot=site["rot"],
                        side="bottom",
                        at=list(site["at"]),
                        overhang_mm=None,
                    ),
                    name="fanout-site:" + spec["name"],
                )
            )
    out.warnings = list(constraints.warnings) + [
        "fanout %s: bottom sites %s%s"
        % (
            name,
            ", ".join(
                "%s at (%.3f, %.3f) rot %g" % (r, *s["at"], s["rot"])
                for r, s in sorted(rep.get("sites", {}).items())
            )
            or "none",
            (
                "; unplaced %s" % ", ".join(sorted(rep.get("unplaced", {})))
                if rep.get("unplaced")
                else ""
            ),
        )
        for name, rep in sorted(reports.items())
    ]
    return out


def _posed(graph, constraints, refs):
    """A copy of ``graph`` with ``refs`` at their resolved fixed poses (placement has
    not run yet: the source row is not where the part will be)."""
    import copy

    from pnr.place.geometry import (
        resolve_fixed_poses,
        resolve_hard_rotations,
        resolve_hard_sides,
        set_component_side,
    )

    out = copy.deepcopy(graph)
    poses = resolve_fixed_poses(out, constraints)
    rotations = resolve_hard_rotations(constraints)
    sides = resolve_hard_sides(constraints)
    for ref in refs:
        comp = out.component(ref)
        if ref in sides:
            set_component_side(comp, sides[ref])
        if ref in rotations:
            comp.rot = rotations[ref]
        if ref in poses:
            comp.pos = poses[ref]
    return out
