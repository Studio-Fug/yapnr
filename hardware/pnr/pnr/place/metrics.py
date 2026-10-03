"""Placement quality + legality metrics (pure).

These score a :class:`pnr.graph.BoardGraph`'s current placement and back the
Phase 2 acceptance test: half-perimeter wirelength (the quality number), plus the
hard-legality checks (overlaps, outline containment, fixed poses, keep-outs).

PNR_PAIR_LANDING_RESERVE=1 (src13): overlap_pairs / hard_violations /
translation_checker see diff-pair via landing reserves through placement_rects
(a bottom part over a top terminal part's landing is an overlap).

On a double-sided board (:func:`pnr.place.sides.stack_refs`) two parts that fan out
stacked back to back on opposite sides are an overlap too (hard_violations,
translation_checker, pose_checker).

The incremental checkers (translation_checker, pose_checker) share one per-part
predicate, :func:`part_rules`, for every hard rule a re-posed part must meet on its
own, so a rule added there holds for every kind of move.
"""

from __future__ import annotations

import math
from typing import Dict, List, Tuple

from pnr.constraints import CompiledConstraints
from pnr.graph import BoardGraph

from .geometry import (
    Rect,
    courtyard_rect,
    edge_band_violations,
    edge_distance,
    hard_edge_bands,
    hard_group_edges,
    hard_group_limits,
    keepout_rects,
    outline_size,
    pin_positions,
    placement_rects,
    resolve_fixed_poses,
    resolve_hard_rotations,
    resolve_hard_sides,
)
from .sides import STACK_PLANE, stack_refs


def hpwl(graph: BoardGraph) -> float:
    """Total half-perimeter wirelength over all multi-pin nets (mm).

    HPWL is the standard placement wirelength proxy: for each net, the perimeter
    half of the bounding box of its pin positions. Single-pin nets contribute 0.
    """
    abs_pins: Dict[Tuple[str, str], Tuple[float, float]] = {}
    for comp in graph.components:
        for name, xy in pin_positions(comp):
            abs_pins[(comp.ref, name)] = xy

    total = 0.0
    for net in graph.nets:
        pts = [abs_pins[p] for p in net.pins if p in abs_pins]
        if len(pts) < 2:
            continue
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        total += (max(xs) - min(xs)) + (max(ys) - min(ys))
    return total


def overlap_pairs(graph: BoardGraph, clearance: float = 0.0) -> List[Tuple[str, str]]:
    """All unordered component pairs whose courtyards overlap (with clearance)."""
    rects = [(c.ref, placement_rects(c)) for c in graph.components]
    return [
        (ref, other)
        for i, (ref, areas) in enumerate(rects)
        for other, regions in rects[i + 1 :]
        if any(
            side == other_side and rect.overlaps(other_rect, gap=clearance)
            for side, rect in areas
            for other_side, other_rect in regions
        )
    ]


def stack_pairs(
    graph: BoardGraph, refs, clearance: float = 0.0, exempt=()
) -> List[Tuple[str, str]]:
    """Pairs of ``refs`` (:func:`pnr.place.sides.stack_refs`) on opposite sides whose
    courtyards overlap (with clearance): two parts that fan out stacked back to back.
    A pair of two ``exempt`` (fixed) parts is the designer's and is not reported."""
    comps = [c for c in graph.components if c.ref in refs]
    out = []
    for i, a in enumerate(comps):
        area = courtyard_rect(a)
        for b in comps[i + 1 :]:
            if a.side == b.side or (a.ref in exempt and b.ref in exempt):
                continue
            if area.overlaps(courtyard_rect(b), gap=clearance):
                out.append((a.ref, b.ref))
    return out


def _with_stack(comp, regions, stack):
    """``regions`` plus the part's courtyard on the stack plane when the no-stacking
    rule covers it (:func:`pnr.place.sides.stack_refs`)."""
    if comp.ref in stack:
        return list(regions) + [(STACK_PLANE, courtyard_rect(comp))]
    return regions


def outside_outline(graph: BoardGraph, width: float, height: float, exclude=()) -> List[str]:
    """Refs whose courtyard is not fully inside ``[0,width] x [0,height]``.

    ``exclude`` (e.g. fixed connectors placed with an intentional edge overhang)
    are skipped — their protrusion past the outline is by design, not a
    violation."""
    ex = set(exclude)
    return [
        c.ref
        for c in graph.components
        if c.ref not in ex and not courtyard_rect(c).inside(width, height)
    ]


def pad_edge_violations(
    graph: BoardGraph, width: float, height: float, pad_edge, exclude=()
) -> List[str]:
    """Refs (not in ``exclude``) whose pads come closer than ``pad_edge[0]`` or
    whose drills closer than ``pad_edge[1]`` to the ``[0,width] x [0,height]``
    outline (PNR_PAD_EDGE_CLEARANCE=1; see pnr.place.legalize.pad_edge_box)."""
    from .legalize import pad_edge_box

    ex = set(exclude)
    out = []
    for c in graph.components:
        if c.ref in ex or not c.pads:
            continue
        x_lo, x_hi, y_lo, y_hi = pad_edge_box(c, pad_edge, width, height)
        x, y = c.pos
        if not (x_lo - 1e-6 <= x <= x_hi + 1e-6 and y_lo - 1e-6 <= y <= y_hi + 1e-6):
            out.append(c.ref)
    return out


def in_keepout(graph: BoardGraph, keepouts: List[Rect], clearance: float = 0.0) -> List[str]:
    """Refs whose courtyard intrudes into any keep-out region."""
    out: List[str] = []
    for c in graph.components:
        cr = courtyard_rect(c)
        if any(cr.overlaps(k, gap=clearance) for k in keepouts):
            out.append(c.ref)
    return out


def misplaced_fixed(
    graph: BoardGraph,
    poses: Dict[str, Tuple[float, float]],
    tol: float = 1e-3,
) -> List[str]:
    """Fixed refs whose placed centre drifted from the resolved pose."""
    out: List[str] = []
    for ref, (px, py) in poses.items():
        try:
            comp = graph.component(ref)
        except KeyError:
            continue
        if abs(comp.pos[0] - px) > tol or abs(comp.pos[1] - py) > tol:
            out.append(ref)
    return out


def hard_violations(
    graph: BoardGraph, constraints: CompiledConstraints, clearance: float = 0.0
) -> Dict[str, List]:
    """All hard-constraint / legality violations, keyed by kind (empty = legal)."""
    width, height = outline_size(graph, constraints)
    poses = resolve_fixed_poses(graph, constraints)
    keepouts = keepout_rects(graph, constraints, poses)
    limits = hard_group_limits(constraints, {c.ref: c.pos for c in graph.components})
    from .line_group import violations as line_violations
    from .rows import violations as row_violations

    rows_bad = row_violations(graph, constraints)
    rows_bad = rows_bad + line_violations(graph, constraints)
    rows_bad = rows_bad + edge_band_violations(graph, constraints, width, height)
    overlaps = overlap_pairs(graph, clearance)
    stack = stack_refs(graph, constraints)
    if stack:
        exempt = (
            set(poses)
            | set(constraints.locked_refs)
            | {c.ref for c in graph.components if c.locked}
        )
        overlaps += [
            pair for pair in stack_pairs(graph, stack, clearance, exempt) if pair not in overlaps
        ]
    out = {
        "overlaps": overlaps,
        "outside_outline": outside_outline(graph, width, height, exclude=constraints.locked_refs),
        "fixed_misplaced": sorted(
            set(misplaced_fixed(graph, poses))
            | {
                ref
                for ref, angle in resolve_hard_rotations(constraints).items()
                if abs((graph.component(ref).rot - angle + 180) % 360 - 180) > 1e-6
            }
        ),
        "side_misplaced": [
            ref
            for ref, side in resolve_hard_sides(constraints).items()
            if graph.component(ref).side != side
        ],
        "keepout": in_keepout(graph, keepouts, clearance),
        "group_outside": sorted(
            set(rows_bad)
            | {
                member
                for anchor, member, radius in hard_group_edges(constraints)
                if math.dist(graph.component(anchor).pos, graph.component(member).pos)
                > radius + 1e-9
            }
        ),
    }
    # Hard region / align (pnr.place.regions): their own keys, present only when the
    # design declares one, so other designs report exactly the keys they always had.
    from .regions import align_offenders, declared, region_offenders

    if declared(constraints):
        out["region_outside"] = sorted(set(region_offenders(graph, constraints)))
        out["align_off"] = sorted(set(align_offenders(graph, constraints)))
    return out


def part_rules(graph, constraints, *, clearance=0.0, pad_edge=None):
    """The hard rules one re-posed part of ``graph`` must meet on its own: its hard
    rotation and side, a fixed pose, the outline (locked parts exempt) and, with
    ``pad_edge``, the pad-edge rule, a hard edge band, a hard region or align
    (:mod:`pnr.place.regions`, against the other parts' current poses), keep-outs (with
    ``clearance``) and hard group radii against the other part's current centre. Everything but
    the overlap with other parts and row/line membership, which the checkers test
    themselves. Returns ``ok(comp)``; the board must be otherwise unchanged."""
    width, height = outline_size(graph, constraints)
    poses = resolve_fixed_poses(graph, constraints)
    keepouts = keepout_rects(graph, constraints, poses)
    edges = {}
    for anchor, member, radius in hard_group_edges(constraints):
        edges.setdefault(anchor, []).append((member, radius))
        edges.setdefault(member, []).append((anchor, radius))
    sides_required = resolve_hard_sides(constraints)
    rotations_required = resolve_hard_rotations(constraints)
    bands = hard_edge_bands(constraints)
    locked = set(constraints.locked_refs)
    from .regions import declared, ref_ok

    related = declared(constraints)

    def ok(comp):
        if (
            comp.ref in rotations_required
            and abs((comp.rot - rotations_required[comp.ref] + 180) % 360 - 180) > 1e-6
        ):
            return False
        if comp.ref in sides_required and comp.side != sides_required[comp.ref]:
            return False
        if comp.ref in poses and any(abs(a - b) > 1e-3 for a, b in zip(comp.pos, poses[comp.ref])):
            return False
        rect = courtyard_rect(comp)
        if comp.ref not in locked and not rect.inside(width, height):
            return False
        if pad_edge is not None and comp.ref not in locked and comp.ref not in poses:
            from .legalize import pad_edge_box

            x_lo, x_hi, y_lo, y_hi = pad_edge_box(comp, pad_edge, width, height)
            x, y = comp.pos
            if not (x_lo - 1e-6 <= x <= x_hi + 1e-6 and y_lo - 1e-6 <= y <= y_hi + 1e-6):
                return False
        if (
            comp.ref in bands
            and edge_distance(comp, bands[comp.ref][0], width, height) > bands[comp.ref][1] + 1e-6
        ):
            return False
        if related and not ref_ok(
            [comp if c.ref == comp.ref else c for c in graph.components], constraints, comp.ref
        ):
            return False
        if any(rect.overlaps(k, gap=clearance) for k in keepouts):
            return False
        for other, radius in edges.get(comp.ref, ()):
            if math.dist(comp.pos, graph.component(other).pos) > radius + 1e-9:
                return False
        return True

    return ok


def translation_checker(graph, constraints, clearance=0.0):
    """Check one translated part against an unchanged, initially legal board.

    Cache stationary courtyards and resolved hard constraints. The caller must
    restore each trial before translating another part; rotations, side swaps,
    or accepted moves require constructing a fresh checker.
    """
    bad = hard_violations(graph, constraints, clearance)
    if any(bad.values()):
        raise ValueError("translation checker requires a legal baseline")
    stack = stack_refs(graph, constraints)
    geometry = {c.ref: _with_stack(c, placement_rects(c), stack) for c in graph.components}
    alone = part_rules(graph, constraints, clearance=clearance)

    def legal(comp):
        from .line_group import violations as line_violations
        from .rows import violations as row_violations

        if row_violations(graph, constraints) or line_violations(graph, constraints):
            return False
        if not alone(comp):
            return False
        return not any(
            ref != comp.ref and side == other_side and area.overlaps(other, gap=clearance)
            for ref, regions in geometry.items()
            for other_side, other in regions
            for side, area in _with_stack(comp, placement_rects(comp), stack)
        )

    return legal


BUCKET_MM = 4.0  # pose_checker's spatial index cell


def pose_checker(graph, constraints, *, clearance=0.0, spread=1.0, pad_edge=None, inflation=None):
    """Check a few re-posed parts (position, rotation and side) against an otherwise
    unchanged, initially legal board.

    ``legal(moved, ignore=())`` takes the moved components of ``graph`` (already set
    to their trial poses, pads mirrored for a side change) and tests each against
    :func:`part_rules` (with the pad-edge rule) and against every other part's cached
    geometry (except the refs in ``ignore``) and each other: same-side overlap with
    ``clearance``, the moved part's rectangles grown by ``spread`` or its
    ``inflation`` ({ref: factor}, the routing feedback's), whichever is larger, as the
    legalizer grows its slot (so a move does not close routing channels). Rows and
    line groups are not re-checked: their members must not move.
    After accepting a move call ``legal.update(refs)`` to refresh the cache.
    """
    bad = hard_violations(graph, constraints)
    if any(bad.values()):
        raise ValueError("pose checker requires a legal baseline")
    alone = part_rules(graph, constraints, clearance=clearance, pad_edge=pad_edge)
    inflation = inflation or {}
    geometry = {}
    buckets = {}  # (i, j) cell of BUCKET_MM -> refs whose rectangles touch it
    cells_of = {}

    def cells(regions, grow=0.0):
        if not regions:
            return []
        x0 = min(r.left for _, r in regions) - grow
        x1 = max(r.right for _, r in regions) + grow
        y0 = min(r.bottom for _, r in regions) - grow
        y1 = max(r.top for _, r in regions) + grow
        return [
            (i, j)
            for i in range(math.floor(x0 / BUCKET_MM), math.floor(x1 / BUCKET_MM) + 1)
            for j in range(math.floor(y0 / BUCKET_MM), math.floor(y1 / BUCKET_MM) + 1)
        ]

    def index(ref, regions):
        for cell in cells_of.get(ref, ()):
            buckets[cell].discard(ref)
        geometry[ref] = regions
        cells_of[ref] = cells(regions)
        for cell in cells_of[ref]:
            buckets.setdefault(cell, set()).add(ref)

    stack = stack_refs(graph, constraints)
    for c in graph.components:
        index(c.ref, _with_stack(c, placement_rects(c), stack))

    def grown(comp):
        from .geometry import ReserveRect

        factor = max(1.0, spread, float(inflation.get(comp.ref, 1.0)))
        out = []
        for side, rect in _with_stack(comp, placement_rects(comp), stack):
            if factor > 1.0 and not isinstance(rect, ReserveRect):
                rect = Rect(rect.cx, rect.cy, rect.w * factor, rect.h * factor)
            out.append((side, rect))
        return out

    def legal(moved, ignore=()):
        names = {c.ref for c in moved} | set(ignore)
        mine = {c.ref: grown(c) for c in moved}
        for comp in moved:
            if not alone(comp):
                return False
            near = set()
            for cell in cells(mine[comp.ref], clearance):
                near |= buckets.get(cell, set())
            for ref in sorted(near - names):
                regions = geometry[ref]
                if any(
                    side == other_side and area.overlaps(other, gap=clearance)
                    for side, area in mine[comp.ref]
                    for other_side, other in regions
                ):
                    return False
        for i, a in enumerate(moved):
            for b in moved[i + 1 :]:
                if any(
                    side == other_side and area.overlaps(other, gap=clearance)
                    for side, area in mine[a.ref]
                    for other_side, other in _with_stack(b, placement_rects(b), stack)
                ):
                    return False
        return True

    def update(refs):
        for ref in refs:
            comp = graph.component(ref)
            index(ref, _with_stack(comp, placement_rects(comp), stack))

    legal.update = update
    return legal
