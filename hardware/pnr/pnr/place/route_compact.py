"""Route-then-compact: squeeze a routed placement by the copper actually routed.

``PNR_ROUTE_COMPACT`` (default off; unset, nothing here runs and no report key changes):
``1`` turns on every part, or a comma list of them:

``TOP``
    hierarchical top level (``regression/hier_case.py``): after the nets between the blocks
    are knitted, the gutters between block macros and top-level parts shrink to what the
    knitted copper uses, then those nets are ripped up and routed again;
``BLOCK``
    inside each hierarchical block template: its parts squeeze together by the block's own
    routed copper, the block outline shrinks to what is left (the macro gets smaller), and
    every instance routes again;
``FLAT``
    a flat board (:func:`pnr.route.feedback.route_and_place`): its parts squeeze together
    by the routed copper and the board routes again.

One pass is an order-preserving 1D compaction along x, a reroute, then the same along y
(``x then y``). The compaction is a longest-path solve on a constraint graph:

* every pair of items whose shapes face each other along the axis (they overlap across it,
  within the placement clearance, on a shared plane) keeps a gap. The gap is what the routed
  copper in that gutter uses: the union of the lanes (tracks running along the gutter and
  vias, each with half the copper clearance either side; tracks crossing the gutter need
  no width), per layer, the widest layer, plus a slack. Never below the placement
  clearance, never more than the gap the item pair already has;
* consecutive items in coordinate order keep their order (no topological upsets: every
  left/right relation of the x pass and every above/below one of the y pass survives);
* fixed parts (fixed poses, locked, hard edge bands and regions, parts carrying a keep-out)
  do not move, keep-outs are fixed obstacles, line groups, rows and aligns move rigidly;
* items stay inside the bounding box they already span: the outline is not touched
  (``PNR_SHRINK`` stays off); the outline shrink this frees is reported.

The items left of an anchor line move toward it as far as the gaps allow (their greatest
solution with the rest held), then the items right of it (their least solution with the
left ones held), then what slack the line left is closed (shapes dovetailing across it). Motion is in multiples of
the placement grid (``PNR_ROUTE_COMPACT_GRID_MM``, default 0.25 mm), so parts on the grid
stay on it.

A compacted placement that adds any hard violation is not routed. A reroute that is worse
(more missing connections, unresolved nets or unmatched pairs/groups, more vias than
``PNR_ROUTE_COMPACT_VIA_TOL`` or 5 % allow, or more copper than
``PNR_ROUTE_COMPACT_COPPER_TOL`` of the board's copper) is refused and the pass backs off:
the next attempt closes every gutter by half as much, and after
``PNR_ROUTE_COMPACT_ATTEMPTS`` (default 3) refusals the axis keeps the routed placement it
had.

Among anchor lines at eighths of the span the one whose moved placement has the least
half-perimeter wirelength is taken, so the items close up toward what they connect to
(a fixed connector at one edge pulls the others its way) rather than on a fixed centre.
``PNR_ROUTE_COMPACT_ROUNDS`` (default 1) repeats the x-then-y pass on the new route.

Block macros with a per-side hull (``PNR_MACRO_HULL=1``, :mod:`pnr.place.hull`) enter as
their hull cover rectangles per plane, so a block slides into another block's notch as far
as the two outlines dovetail.

The rip-up and reroute is a callback (the driver's own router), the seam where a
push-and-shove router can later move the copper instead.
"""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

PARTS = ("TOP", "BLOCK", "FLAT")
ALL = "all"  # a keep-out blocks every plane
EPS = 1e-6


# ------------------------------------------------------------------------ switches


def enabled(part: Optional[str] = None) -> bool:
    """True with ``PNR_ROUTE_COMPACT`` set (``1``: every part; else a comma list of
    :data:`PARTS`), for ``part`` when it is listed and not ``PNR_ROUTE_COMPACT_<PART>=0``."""
    value = os.environ.get("PNR_ROUTE_COMPACT", "").strip()
    if not value or value == "0":
        return False
    if value == "1":
        on = set(PARTS)
    else:
        on = {p.strip().upper() for p in value.split(",") if p.strip()}
        unknown = on - set(PARTS)
        if unknown:
            raise ValueError(
                "PNR_ROUTE_COMPACT names unknown parts %s (known: %s)"
                % (", ".join(sorted(unknown)), ", ".join(PARTS))
            )
    if part is None:
        return bool(on)
    return part in on and os.environ.get("PNR_ROUTE_COMPACT_" + part) != "0"


def _env_float(name, default):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return float(default)
    value = float(raw)
    if not math.isfinite(value) or value < 0:
        raise ValueError("%s takes a finite non-negative number, got %r" % (name, raw))
    return value


@dataclass
class Settings:
    rounds: int = 1
    attempts: int = 3
    slack_mm: float = 0.25
    grid_mm: float = 0.25
    copper_tol: float = 0.01
    via_tol: int = 2

    @classmethod
    def from_environment(cls):
        return cls(
            rounds=int(_env_float("PNR_ROUTE_COMPACT_ROUNDS", 1)),
            attempts=max(1, int(_env_float("PNR_ROUTE_COMPACT_ATTEMPTS", 3))),
            slack_mm=_env_float("PNR_ROUTE_COMPACT_SLACK_MM", 0.25),
            grid_mm=max(1e-3, _env_float("PNR_ROUTE_COMPACT_GRID_MM", 0.25)),
            copper_tol=_env_float("PNR_ROUTE_COMPACT_COPPER_TOL", 0.01),
            via_tol=int(_env_float("PNR_ROUTE_COMPACT_VIA_TOL", 2)),
        )

    def to_dict(self):
        return dict(self.__dict__)


# ------------------------------------------------------------------------ items


@dataclass
class Item:
    """A rigid placement body: ``refs`` move together, ``shapes`` are ``(plane, x0, y0, x1,
    y1)`` relative to ``pos`` (board frame). ``kind`` is part, compound, macro or keepout."""

    key: str
    refs: Tuple[str, ...]
    pos: Tuple[float, float]
    shapes: List[Tuple[str, float, float, float, float]]
    fixed: bool = False
    kind: str = "part"

    def extent(self):
        """(x0, y0, x1, y1) of the shapes in the board frame."""
        x0 = min(s[1] for s in self.shapes) + self.pos[0]
        y0 = min(s[2] for s in self.shapes) + self.pos[1]
        x1 = max(s[3] for s in self.shapes) + self.pos[0]
        y1 = max(s[4] for s in self.shapes) + self.pos[1]
        return x0, y0, x1, y1


def make_item(key, refs, shapes_abs, fixed=False, kind="part"):
    """An :class:`Item` anchored at the centre of its board-frame ``shapes_abs``."""
    x0 = min(s[1] for s in shapes_abs)
    y0 = min(s[2] for s in shapes_abs)
    x1 = max(s[3] for s in shapes_abs)
    y1 = max(s[4] for s in shapes_abs)
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    rel = [(p, a - cx, b - cy, c - cx, d - cy) for p, a, b, c, d in shapes_abs]
    return Item(key, tuple(refs), (cx, cy), rel, fixed, kind)


def _rect_shape(plane, r):
    return (plane, r.left, r.bottom, r.right, r.top)


RIGID_KINDS = ("line_group", "row", "align")


def fixed_refs(graph, constraints) -> set:
    """Refs compaction may not move: fixed poses, locked parts, hard edge bands and
    regions, and the refs keep-outs are measured from."""
    from pnr.constraints import Enforcement
    from pnr.place.geometry import resolve_fixed_poses

    out = set(resolve_fixed_poses(graph, constraints)) | set(constraints.locked_refs)
    out |= {c.ref for c in graph.components if c.locked}
    for con in constraints.constraints:
        if con.kind in ("fixed", "keepout"):
            out.update(con.refs)
        elif con.kind in ("edge_align", "region") and con.enforcement == Enforcement.HARD:
            out.update(con.refs)
    return out


def graph_items(graph, constraints, *, rigid=(), shapes=None, keepouts=True) -> List[Item]:
    """The compaction items of a placed graph.

    ``rigid`` is ``[(key, refs)]`` of bodies that must move as one (block macros);
    ``shapes`` (``{key: [(plane, x0, y0, x1, y1)] board frame}``) replaces their members'
    own rectangles (a macro's outline or hull). Line groups, rows and aligns join their
    members into one body; a body with a fixed member is fixed."""
    from pnr.place.geometry import keepout_rects, placement_rects

    refs = [c.ref for c in graph.components]
    parent = {r: r for r in refs}

    def find(r):
        while parent[r] != r:
            parent[r] = parent[parent[r]]
            r = parent[r]
        return r

    def union(group):
        group = [r for r in group if r in parent]
        for r in group[1:]:
            a, b = find(group[0]), find(r)
            if a != b:
                parent[b] = a

    names = {}
    for key, members in rigid:
        union(list(members))
        for r in members:
            if r in parent:
                names[r] = key
    for con in constraints.constraints:
        if con.kind in RIGID_KINDS and len(con.refs) > 1:
            union(list(con.refs))
    fixed = fixed_refs(graph, constraints)
    sets: Dict[str, List[str]] = {}
    for r in refs:
        sets.setdefault(find(r), []).append(r)
    by_ref = {c.ref: c for c in graph.components}
    shapes = shapes or {}
    items = []
    for root, members in sorted(sets.items(), key=lambda kv: min(kv[1])):
        keys = sorted({names[r] for r in members if r in names})
        body = []
        covered = set()
        for key in keys:
            if key in shapes:
                body += list(shapes[key])
                covered |= {r for r in members if names.get(r) == key}
        for r in members:
            if r in covered:
                continue
            body += [_rect_shape(side, rect) for side, rect in placement_rects(by_ref[r])]
        if not body:
            continue
        if len(keys) == 1:
            key = keys[0]
        else:
            key = members[0] if len(members) == 1 else "+".join(sorted(members))
        kind = "macro" if keys else ("part" if len(members) == 1 else "compound")
        items.append(
            make_item(key, sorted(members), body, fixed=bool(fixed & set(members)), kind=kind)
        )
    if keepouts:
        poses = {c.ref: c.pos for c in graph.components}
        for k, rect in enumerate(keepout_rects(graph, constraints, poses)):
            items.append(make_item("keepout%d" % k, (), [_rect_shape(ALL, rect)], True, "keepout"))
    return items


# ------------------------------------------------------------------------ copper


@dataclass
class Copper:
    """Routed copper for gutter measurement: segments (x0, y0, x1, y1, half width) with a
    layer id each, vias (x, y, radius), and the copper clearance."""

    seg: np.ndarray
    layer: np.ndarray
    via: np.ndarray
    clearance: float

    @classmethod
    def from_routes(cls, tracks, vias, via_diameter, clearance):
        """``tracks`` ``[net, layer, (x, y), (x, y), width]`` and ``vias`` ``[net, x, y]``
        (or ``[net, x, y, diameter]``) as in routes.json."""
        layers = {}
        seg, lay = [], []
        for t in tracks:
            _net, la, a, b, w = t[:5]
            seg.append((float(a[0]), float(a[1]), float(b[0]), float(b[1]), float(w) / 2.0))
            lay.append(layers.setdefault(la, len(layers)))
        via = []
        for v in vias:
            d = float(v[3]) if len(v) > 3 and v[3] else float(via_diameter)
            via.append((float(v[1]), float(v[2]), d / 2.0))
        return cls(
            np.array(seg, dtype=np.float64).reshape(-1, 5),
            np.array(lay, dtype=np.int64),
            np.array(via, dtype=np.float64).reshape(-1, 3),
            float(clearance),
        )

    def along(self, axis):
        """Per-axis arrays: (axis lo, axis hi, perp lo, perp hi, half width, lane?)."""
        s = self.seg
        a0, a1 = s[:, axis], s[:, axis + 2]
        p0, p1 = s[:, 1 - axis], s[:, 3 - axis]
        alo, ahi = np.minimum(a0, a1), np.maximum(a0, a1)
        plo, phi = np.minimum(p0, p1), np.maximum(p0, p1)
        # A track running along the gutter (more across the axis than along it) holds a
        # lane; one crossing it, diagonals included, stretches or shrinks with it.
        lane = (ahi - alo) < (phi - plo) - EPS
        return alo, ahi, plo, phi, s[:, 4], lane


def _union_length(intervals):
    total, end = 0.0, -math.inf
    for lo, hi in sorted(intervals):
        if hi <= end:
            continue
        total += hi - max(lo, end)
        end = hi
    return total


class GutterMeter:
    """Measures the copper a gutter (axis interval x perp interval) uses along one axis."""

    def __init__(self, copper: Optional[Copper], axis: int):
        self.copper = copper
        self.axis = axis
        if copper is not None:
            self.arr = copper.along(axis)

    def need(self, g0, g1, p0, p1) -> float:
        """Width the copper inside [g0, g1] (along the axis) x [p0, p1] holds: the union of
        its lanes (each widened by half the clearance on both sides), widest layer."""
        if self.copper is None or g1 - g0 <= EPS or p1 - p0 <= EPS:
            return 0.0
        c2 = self.copper.clearance / 2.0
        alo, ahi, plo, phi, hw, lane = self.arr
        pad = hw + c2
        sel = lane & (phi + hw > p0) & (plo - hw < p1) & (ahi + pad > g0) & (alo - pad < g1)
        per_layer: Dict[int, list] = {}
        for k in np.nonzero(sel)[0]:
            lo, hi = max(g0, alo[k] - pad[k]), min(g1, ahi[k] + pad[k])
            if hi > lo:
                per_layer.setdefault(int(self.copper.layer[k]), []).append((lo, hi))
        v = self.copper.via
        if len(v):
            a, p, r = v[:, self.axis], v[:, 1 - self.axis], v[:, 2]
            vs = (p + r > p0) & (p - r < p1) & (a + r + c2 > g0) & (a - r - c2 < g1)
            spans = [
                (max(g0, a[k] - r[k] - c2), min(g1, a[k] + r[k] + c2)) for k in np.nonzero(vs)[0]
            ]
            spans = [s for s in spans if s[1] > s[0]]
            if spans:  # through vias block every layer
                if not per_layer:
                    per_layer[-1] = []
                for key in per_layer:
                    per_layer[key] = per_layer[key] + spans
        return max((_union_length(v) for v in per_layer.values()), default=0.0)


# ------------------------------------------------------------------------ the 1D solve


def _planes_meet(a, b):
    return a == b or a == ALL or b == ALL


@dataclass
class AxisPlan:
    """One axis's compaction: per item delta (grid multiples), the gutters measured and the
    edges solved."""

    axis: int
    delta: List[float]
    gutters: List[dict] = field(default_factory=list)
    edges: int = 0
    span_before: float = 0.0
    span_after: float = 0.0
    anchor: float = 0.5

    @property
    def moved(self):
        return sum(1 for d in self.delta if abs(d) > EPS)

    @property
    def max_move(self):
        return max((abs(d) for d in self.delta), default=0.0)

    def summary(self):
        gaps = [g["gap"] for g in self.gutters]
        needs = [g["need"] for g in self.gutters]
        after = [g["after"] for g in self.gutters if g.get("after") is not None]
        return dict(
            axis="xy"[self.axis],
            moved=self.moved,
            max_move_mm=round(self.max_move, 4),
            sum_move_mm=round(sum(abs(d) for d in self.delta), 4),
            edges=self.edges,
            gutters=len(self.gutters),
            gutter_mean_mm=round(float(np.mean(gaps)), 4) if gaps else 0.0,
            gutter_need_mean_mm=round(float(np.mean(needs)), 4) if needs else 0.0,
            gutter_after_mean_mm=round(float(np.mean(after)), 4) if after else 0.0,
            gutter_used=round(sum(needs) / sum(gaps), 4) if gaps and sum(gaps) > 0 else 0.0,
            span_before_mm=round(self.span_before, 4),
            span_after_mm=round(self.span_after, 4),
            anchor=round(self.anchor, 3),
        )


def _ceil_grid(v, g):
    return math.ceil(v / g - 1e-9) * g


def _floor_grid(v, g):
    return math.floor(v / g + 1e-9) * g


def plan_axis(
    items: Sequence[Item],
    axis: int,
    copper: Optional[Copper],
    *,
    min_gap: float,
    slack: float = 0.25,
    close: float = 1.0,
    grid: float = 0.25,
    bounds: Optional[Tuple[float, float]] = None,
    anchors: Sequence[float] = (0.5,),
    score: Optional[Callable] = None,
) -> AxisPlan:
    """The compaction of ``items`` along ``axis`` (0: x, 1: y); see the module doc.

    ``bounds`` (lo, hi) on the axis limits the movable items (default: the extent they span
    now). Each gutter closes by the share ``close`` of what it may give up (1: down to
    its copper's need; the back-off steps take less). The items close up on an anchor line,
    at each fraction of ``anchors`` across that span (0: everything packs toward lo, 0.5: on
    the centre); with ``score(delta)`` (lower
    is better, e.g. the wirelength of the moved placement) the best anchor wins, ties to
    the one nearest the centre. Deltas are multiples of ``grid``; all zero when nothing can
    close up."""
    n = len(items)
    perp = 1 - axis
    o = [it.pos[axis] for it in items]
    op = [it.pos[perp] for it in items]
    meter = GutterMeter(copper, axis)
    # Separation edges, strongest per ordered pair: p_j - p_i >= d.
    edge: Dict[Tuple[int, int], float] = {}
    nearest: Dict[Tuple[int, int], dict] = {}  # per shape, its facing gutter (reporting)

    def add(i, j, d):
        if d > edge.get((i, j), -math.inf):
            edge[(i, j)] = d

    shp = []
    for k, it in enumerate(items):
        rows = []
        for plane, x0, y0, x1, y1 in it.shapes:
            r = (x0, y0, x1, y1)
            rows.append(
                (
                    plane,
                    r[axis],  # axis lo, relative
                    r[axis + 2],  # axis hi, relative
                    r[perp] + op[k],  # perp lo, absolute (does not move this pass)
                    r[perp + 2] + op[k],
                )
            )
        shp.append(rows)
    for i in range(n):
        for j in range(i + 1, n):
            if items[i].fixed and items[j].fixed:
                continue
            for pa, alo, ahi, aplo, aphi in shp[i]:
                for pb, blo, bhi, bplo, bphi in shp[j]:
                    if not _planes_meet(pa, pb):
                        continue
                    if not (aplo < bphi + min_gap - EPS and bplo < aphi + min_gap - EPS):
                        continue
                    ai0, ai1 = o[i] + alo, o[i] + ahi
                    bj0, bj1 = o[j] + blo, o[j] + bhi
                    q0, q1 = max(aplo, bplo), min(aphi, bphi)
                    if ai1 <= bj0 + EPS:
                        first, second, gap = (i, ahi, (pa, ai1)), (j, blo, (pb, bj0)), bj0 - ai1
                    elif bj1 <= ai0 + EPS:
                        first, second, gap = (j, bhi, (pb, bj1)), (i, alo, (pa, ai0)), ai0 - bj1
                    else:  # overlapping already (stacked or locked): keep the offset
                        add(i, j, o[j] - o[i])
                        add(j, i, o[i] - o[j])
                        continue
                    f, fhi, (_, g0) = first
                    s, slo, (_, g1) = second
                    if gap <= min_gap + EPS:
                        target = gap
                        need = 0.0
                    else:
                        obstacle = "keepout" in (items[f].kind, items[s].kind)
                        need = 0.0 if obstacle else meter.need(g0, g1, q0, q1)
                        room = need + (slack if need > 0 else 0.0)
                        target = min(gap, max(min_gap, room))
                        target = gap - close * (gap - target)
                    add(f, s, fhi - slo + target)
                    key = (f, s)
                    rec = nearest.get(key)
                    if rec is None or gap < rec["gap"]:
                        nearest[key] = dict(
                            a=items[f].key,
                            b=items[s].key,
                            gap=round(gap, 4),
                            need=round(need, 4),
                            target=round(target, 4),
                            at=[round(v, 3) for v in (g0, g1, q0, q1)],
                        )
    # Order: consecutive non-keepout items keep their order along the axis.
    order = sorted(
        (k for k in range(n) if items[k].kind != "keepout"), key=lambda k: (o[k], items[k].key)
    )
    for a, b in zip(order, order[1:]):
        add(a, b, 0.0)
    # Delta form, rounded to the grid (e <= 0: the current placement satisfies every edge).
    edges = []
    for (i, j), d in edge.items():
        e = d - (o[j] - o[i])
        e = min(0.0, _ceil_grid(e, grid)) if e <= EPS else e
        edges.append((i, j, e))
    movable = [k for k in range(n) if not items[k].fixed]
    plan = AxisPlan(axis=axis, delta=[0.0] * n, edges=len(edges))
    if not movable:
        return plan
    ext = [(min(s[1] for s in shp[k]) + o[k], max(s[2] for s in shp[k]) + o[k]) for k in range(n)]
    lo_b = min(ext[k][0] for k in movable) if bounds is None else bounds[0]
    hi_b = max(ext[k][1] for k in movable) if bounds is None else bounds[1]
    lo = [min(0.0, _ceil_grid(lo_b - ext[k][0], grid)) for k in range(n)]
    hi = [max(0.0, _floor_grid(hi_b - ext[k][1], grid)) for k in range(n)]
    best = None
    for fraction in anchors:
        centre = lo_b + (hi_b - lo_b) * float(fraction)
        delta = _close_on(n, edges, ext, movable, lo, hi, centre, grid)
        key = (score(delta) if score is not None else 0.0, abs(float(fraction) - 0.5))
        if best is None or key < best[0]:
            best = (key, delta, float(fraction))
    delta = best[1]
    plan.anchor = best[2]
    plan.delta = [round(d / grid) * grid for d in delta]
    for (f, s), g in nearest.items():
        g["after"] = round(g["gap"] + plan.delta[s] - plan.delta[f], 4)
    plan.gutters = sorted(nearest.values(), key=lambda g: (g["a"], g["b"]))
    span = [ext[k] for k in range(n) if items[k].kind != "keepout"]
    if span:
        plan.span_before = max(s[1] for s in span) - min(s[0] for s in span)
        moved = [
            (ext[k][0] + plan.delta[k], ext[k][1] + plan.delta[k])
            for k in range(n)
            if items[k].kind != "keepout"
        ]
        plan.span_after = max(s[1] for s in moved) - min(s[0] for s in moved)
    return plan


def _close_on(n, edges, ext, movable, lo, hi, centre, grid):
    """Deltas closing the movable items up on the line ``centre``."""
    left = {k for k in movable if (ext[k][0] + ext[k][1]) / 2.0 < centre}
    right = set(movable) - left
    # First neither side passes the line, so each closes up on it ...
    hi_c = [min(hi[k], max(0.0, _floor_grid(centre - ext[k][1], grid))) for k in range(n)]
    lo_c = [max(lo[k], min(0.0, _ceil_grid(centre - ext[k][0], grid))) for k in range(n)]
    delta = [0.0] * n
    # (the left items as far right as they may go with the rest held: their greatest
    # solution; then the right ones as far left: their least solution) ...
    delta = _greatest(n, edges, delta, hi_c, left)
    delta = _least(n, edges, delta, lo_c, right)
    # ... then the slack the line left: shaped bodies that dovetail across it.
    delta = _least(n, edges, delta, [min(a, b) for a, b in zip(lo, delta)], right)
    return _greatest(n, edges, delta, [max(a, b) for a, b in zip(hi, delta)], left)


def _greatest(n, edges, delta, hi, free):
    """Greatest delta of the ``free`` items with p_j - p_i >= e kept, others held."""
    d = list(delta)
    for k in free:
        d[k] = hi[k]
    for _ in range(n + 2):
        changed = False
        for i, j, e in edges:
            if i in free and d[i] > d[j] - e + EPS:
                d[i] = d[j] - e
                changed = True
        if not changed:
            return d
    raise RuntimeError("route-compact: constraint graph has a positive cycle")


def _least(n, edges, delta, lo, free):
    """Least delta of the ``free`` items with p_j - p_i >= e kept, others held."""
    if delta is None:
        return None
    d = list(delta)
    for k in free:
        d[k] = lo[k]
    for _ in range(n + 2):
        changed = False
        for i, j, e in edges:
            if j in free and d[j] < d[i] + e - EPS:
                d[j] = d[i] + e
                changed = True
        if not changed:
            return d
    raise RuntimeError("route-compact: constraint graph has a positive cycle")


def check_plan(items, plan):
    """True when ``plan`` keeps every item inside its order (no swap of consecutive items)."""
    axis = plan.axis
    order = sorted(
        (k for k, it in enumerate(items) if it.kind != "keepout"),
        key=lambda k: (items[k].pos[axis], items[k].key),
    )
    new = [items[k].pos[axis] + plan.delta[k] for k in order]
    return all(b >= a - EPS for a, b in zip(new, new[1:]))


ANCHORS = (0.0, 0.125, 0.25, 0.375, 0.5, 0.625, 0.75, 0.875, 1.0)


def wirelength(graph, items, axis, delta):
    """Half-perimeter wirelength of ``graph`` with ``items`` moved by ``delta`` along
    ``axis`` (the anchor score: close up toward what the parts connect to)."""
    from pnr.place.geometry import pin_positions

    shift = {}
    for it, d in zip(items, delta):
        for r in it.refs:
            shift[r] = d
    pins = {}
    for c in graph.components:
        d = shift.get(c.ref, 0.0)
        for name, (x, y) in pin_positions(c):
            p = [x, y]
            p[axis] += d
            pins[(c.ref, name)] = p
    total = 0.0
    for net in graph.nets:
        pts = [pins[tuple(p)] for p in net.pins if tuple(p) in pins]
        if len(pts) > 1:
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            total += max(xs) - min(xs) + max(ys) - min(ys)
    return round(total, 6)


def apply(graph, items, plan):
    """A copy of ``graph`` with every item's refs moved by its delta along the plan's axis."""
    from pnr.graph import BoardGraph

    out = BoardGraph.from_json(graph.to_json())
    by_ref = {c.ref: c for c in out.components}
    for it, d in zip(items, plan.delta):
        if abs(d) <= EPS:
            continue
        for r in it.refs:
            c = by_ref[r]
            pos = list(c.pos)
            pos[plan.axis] += d
            c.pos = (pos[0], pos[1])
    return out


def new_violations(before, after, constraints) -> Dict[str, list]:
    """Hard violations ``after`` has that ``before`` did not (empty: no worse)."""
    from pnr.place.metrics import hard_violations

    a = hard_violations(before, constraints)
    b = hard_violations(after, constraints)
    out = {}
    for key, found in b.items():
        old = {repr(x) for x in a.get(key, [])}
        added = [x for x in found if repr(x) not in old]
        if added:
            out[key] = added
    return out


# ------------------------------------------------------------------------ metrics


def area_metrics(graph, width, height, items=None) -> dict:
    """Bounding box of the placed bodies, the outline and the outline shrink it allows."""
    from pnr.place import compact

    m = compact.metrics(graph, width, height)
    bw, bh = m["bbox_mm"]
    return dict(
        bbox_mm=[round(bw, 3), round(bh, 3)],
        bbox_mm2=round(bw * bh, 3),
        outline_mm=[round(float(width), 3), round(float(height), 3)],
        outline_shrink_possible=(
            round(1.0 - (bw * bh) / (float(width) * float(height)), 4) if width and height else 0.0
        ),
    )


def plane_blocked(graph, tracks, vias, rules) -> int:
    """SMD pads of a ``plane_layer`` net (the legacy plane path, whose pads writeback drops
    to the plane after routing) with no clear dog-bone site: a via ring around the pad
    (16 directions, writeback's base distance plus up to 1.5 mm) clear of other nets'
    tracks, vias and pads, its stub clear of other nets' surface copper. Compaction may
    not raise this count: a router that leaves a plane pad no room is a native open."""
    plane = {
        n
        for nc in rules.get("net_classes") or []
        if isinstance(nc, dict) and nc.get("plane_layer")
        for n in nc.get("nets") or []
    }
    if not plane:
        return 0
    from pnr.place.geometry import pad_rects

    fab = rules.get("fab") or {}
    via_r = float(fab.get("via_diameter_mm", 0.6)) / 2.0
    clr = max(0.2, float(fab.get("clearance_mm", 0.2)))
    stub = float(fab.get("track_width_mm", 0.25)) / 2.0
    seg = np.array(
        [(a[0], a[1], b[0], b[1], w / 2.0) for _n, _la, a, b, w in tracks], dtype=np.float64
    ).reshape(-1, 5)
    seg_net = np.array([t[0] for t in tracks], dtype=object)
    seg_layer = np.array([t[1] for t in tracks], dtype=object)
    via = np.array([(v[1], v[2]) for v in vias], dtype=np.float64).reshape(-1, 2)
    via_net = np.array([v[0] for v in vias], dtype=object)
    pads = []  # (net, rect, side, smd)
    for c in graph.components:
        for (_name, net, r), pad in zip(pad_rects(c), c.pads):
            pads.append((net, r, c.side, not pad.through_hole))

    def seg_dist(px, py, k):
        x0, y0, x1, y1 = seg[k, 0], seg[k, 1], seg[k, 2], seg[k, 3]
        dx, dy = x1 - x0, y1 - y0
        L2 = dx * dx + dy * dy
        safe = np.where(L2 > 0, L2, 1.0)
        t = np.clip(np.where(L2 > 0, ((px - x0) * dx + (py - y0) * dy) / safe, 0.0), 0.0, 1.0)
        return np.hypot(px - (x0 + t * dx), py - (y0 + t * dy))

    def rect_dist(px, py, r):
        return math.hypot(max(r.left - px, 0.0, px - r.right), max(r.bottom - py, 0.0, py - r.top))

    def clear(px, py, net, side, mids):
        idx = np.nonzero(seg_net != net)[0]
        if len(idx) and np.any(seg_dist(px, py, idx) < via_r + clr + seg[idx, 4] - 1e-9):
            return False
        vi = np.nonzero(via_net != net)[0]
        if len(vi) and np.any(np.hypot(via[vi, 0] - px, via[vi, 1] - py) < 2 * via_r + clr):
            return False
        if any(n2 != net and rect_dist(px, py, r2) < via_r + clr for n2, r2, _s, _m in pads):
            return False
        surface = "F.Cu" if side == "top" else "B.Cu"
        si = np.nonzero((seg_net != net) & (seg_layer == surface))[0]
        for mx, my in mids:
            if len(si) and np.any(seg_dist(mx, my, si) < stub + clr + seg[si, 4] - 1e-9):
                return False
            if any(
                n2 != net and s2 == side and rect_dist(mx, my, r2) < stub + clr
                for n2, r2, s2, _m in pads
            ):
                return False
        return True

    blocked = 0
    for net, r, side, smd in pads:
        if net not in plane or not smd:
            continue
        base = math.hypot(r.w, r.h) / 2.0 + via_r + clr
        ok = False
        for k in range(7):
            for a in range(16):
                th = 2 * math.pi * a / 16
                dist = base + 0.25 * k
                px, py = r.cx + dist * math.cos(th), r.cy + dist * math.sin(th)
                mids = [(r.cx + (px - r.cx) * f, r.cy + (py - r.cy) * f) for f in (0.5, 0.8)]
                if clear(px, py, net, side, mids):
                    ok = True
                    break
            if ok:
                break
        blocked += 0 if ok else 1
    return blocked


def not_worse(base: dict, new: dict, settings: Settings) -> Tuple[bool, str]:
    """``new`` route metrics against ``base``: completion first, then vias and copper."""
    for key in ("missing", "unresolved", "unmatched", "plane_blocked"):
        if new.get(key, 0) > base.get(key, 0):
            return False, "%s %s > %s" % (key, new.get(key, 0), base.get(key, 0))
    via_tol = max(settings.via_tol, int(0.05 * base.get("vias", 0)))
    if new.get("vias", 0) > base.get("vias", 0) + via_tol:
        return False, "vias %s > %s + %d" % (new["vias"], base["vias"], via_tol)
    # The tolerance is a share of the board's copper (``copper_ref_mm``: a hierarchical
    # top level's knit is only part of it), else of the routed copper compared.
    ref = base.get("copper_ref_mm", base.get("copper_mm", 0.0))
    limit = base.get("copper_mm", 0.0) + settings.copper_tol * ref + 1e-6
    if new.get("copper_mm", 0.0) > limit:
        return False, "copper %.1f > %.1f" % (new["copper_mm"], limit)
    return True, ""


def route_summary(route) -> dict:
    """missing / unresolved / unmatched / vias / copper of a detail route result."""
    from pnr.place.initial_pool import _route_metrics

    m = _route_metrics(route)
    return dict(
        missing=m["missing_connections"],
        unresolved=len(m["unresolved_nets"]),
        unmatched=m.get("length_unmatched", 0),
        vias=m["vias"],
        copper_mm=round(m["copper_length_mm"], 3),
    )


# ------------------------------------------------------------------------ the loop


PIN_RETRIES = 3  # re-plans per axis that hold the parts a hard violation named


def _violation_refs(bad) -> set:
    """Component refs named in a violations dict (refs, or pairs/tuples of refs)."""
    out = set()

    def walk(x):
        if isinstance(x, str):
            out.add(x)
        elif isinstance(x, (list, tuple, set)):
            for y in x:
                walk(y)

    for found in bad.values():
        walk(found)
    return out


def _related(refs, constraints) -> set:
    """``refs`` plus the anchors and members of the hard groups they belong to (a group
    member out of its radius is fixed by holding both ends)."""
    out = set(refs)
    for con in getattr(constraints, "constraints", None) or []:
        if con.kind == "group" and out & (set(con.refs) | {con.params.get("anchor")}):
            out |= {con.params.get("anchor")} - {None}
    return out


def guarded(label, fallback, run):
    """``run()``, or ``fallback`` with the error recorded when it raises: route-then-compact
    is a post-pass on a finished route, which it may only improve, never lose."""
    import traceback

    try:
        return run()
    except Exception as error:  # noqa: BLE001 - the routed result stands
        placed, route = fallback
        return (
            placed,
            route,
            dict(
                label=label,
                error="%s: %s" % (type(error).__name__, error),
                traceback=traceback.format_exc()[-2000:],
                accepted=0,
            ),
        )


def compact_loop(
    placed,
    route,
    *,
    constraints,
    items_of: Callable,
    copper_of: Callable,
    reroute: Callable,
    metrics_of: Callable,
    min_gap: float,
    outline,
    label: str,
    settings: Optional[Settings] = None,
    observe: Optional[Callable] = None,
    check: Optional[Callable] = None,
    announce: Optional[Callable] = None,
):
    """Route-then-compact on one routed placement.

    ``items_of(placed)`` -> items, ``copper_of(route)`` -> :class:`Copper`,
    ``reroute(candidate, axis)`` -> ``(placed, route)`` (rip-up and route again; the
    placed graph may come back in a new frame, as a block's shrunk outline does; a None
    route refuses the step, with ``placed`` the reason when it is a string),
    ``metrics_of(route)`` -> the dict :func:`not_worse` compares. ``outline`` is the
    (width, height) or a callable ``outline(route)``. ``observe(stage, placed, route,
    record)`` sees each accepted step, ``announce(candidate, axis, record)`` each
    candidate about to be routed (tracing, live view). ``check(before, after)`` ->
    violations dict (default :func:`new_violations`). Returns ``(placed, route, report)``."""
    from pnr.profile import span

    settings = settings or Settings.from_environment()
    started = time.monotonic()

    def size(r):
        return outline(r) if callable(outline) else outline

    base = metrics_of(route)
    report = dict(
        label=label,
        settings=settings.to_dict(),
        before=dict(metrics=base, **area_metrics(placed, *size(route))),
        attempts=[],
        accepted=0,
        seconds=dict(measure=0.0, reroute=0.0, check=0.0),
    )
    check = check or (lambda a, b: new_violations(a, b, constraints))
    for rnd in range(max(0, settings.rounds)):
        progressed = False
        for axis in (0, 1):
            pinned = set()  # refs a hard violation named: held in the next attempt
            back_off = 0
            for _ in range(settings.attempts + PIN_RETRIES):
                if back_off >= settings.attempts:
                    break
                attempt = back_off
                t0 = time.monotonic()
                with span("route_compact.measure"):
                    items = items_of(placed)
                    for it in items:
                        if pinned & set(it.refs):
                            it.fixed = True
                    plan = plan_axis(
                        items,
                        axis,
                        copper_of(route),
                        min_gap=min_gap,
                        slack=settings.slack_mm,
                        close=0.5**attempt,
                        grid=settings.grid_mm,
                        anchors=ANCHORS,
                        score=lambda delta: wirelength(placed, items, axis, delta),
                    )
                t1 = time.monotonic()
                report["seconds"]["measure"] += t1 - t0
                rec = dict(round=rnd + 1, attempt=attempt + 1, **plan.summary())
                rec["gutter_list"] = plan.gutters[:64]
                rec["items"] = [
                    [it.key, it.kind, it.fixed, [round(v, 3) for v in it.extent()], round(d, 4)]
                    for it, d in zip(items, plan.delta)
                ][:200]
                report["attempts"].append(rec)
                if plan.max_move < settings.grid_mm / 2:
                    rec["result"] = "nothing to close"
                    break
                if not check_plan(items, plan):
                    rec["result"] = "order broken (refused)"
                    break
                candidate = apply(placed, items, plan)
                with span("route_compact.check"):
                    bad = check(placed, candidate)
                t2 = time.monotonic()
                report["seconds"]["check"] += t2 - t1
                if bad:
                    rec["result"] = "hard violation"
                    rec["violations"] = {k: [str(x) for x in v][:8] for k, v in bad.items()}
                    named = _related(_violation_refs(bad), constraints)
                    named &= {r for it in items for r in it.refs}
                    if named - pinned and len(pinned) < 64:
                        # Hold the parts it names (their items) and plan the axis again.
                        pinned |= named
                        rec["pinned"] = sorted(named)
                    else:
                        back_off += 1
                    continue
                if announce is not None:
                    announce(candidate, axis, rec)
                with span("route_compact.reroute"):
                    new_placed, new_route = reroute(candidate, axis)
                t3 = time.monotonic()
                report["seconds"]["reroute"] += t3 - t2
                rec["reroute_seconds"] = round(t3 - t2, 3)
                if new_route is None:
                    rec["result"] = new_placed if isinstance(new_placed, str) else "reroute failed"
                    back_off += 1
                    continue
                metrics = metrics_of(new_route)
                rec["metrics"] = metrics
                ok, why = not_worse(base, metrics, settings)
                if not ok:
                    rec["result"] = "back off: " + why
                    back_off += 1
                    continue
                rec["result"] = "accepted"
                rec.update(area_metrics(new_placed, *size(new_route)))
                placed, route, base = new_placed, new_route, metrics
                report["accepted"] += 1
                progressed = True
                if observe is not None:
                    observe("%s-r%d-%s" % (label, rnd + 1, "xy"[axis]), placed, route, rec)
                break
        if not progressed:
            break
    report["after"] = dict(metrics=base, **area_metrics(placed, *size(route)))
    report["seconds"] = {k: round(v, 3) for k, v in report["seconds"].items()}
    report["seconds"]["total"] = round(time.monotonic() - started, 3)
    return placed, route, report


def flat_pass(placed, route, constraints, rules, *, pitch, iters, outline):
    """PNR_ROUTE_COMPACT FLAT: :func:`compact_loop` on a flat board's final route with the
    production detail router (:func:`pnr.route.detail.router.route_board`) as the reroute.
    Returns ``(placed, route, report)``."""
    from pnr.hier.extent import copper_clearance
    from pnr.live import emit
    from pnr.route.detail.router import route_board

    fab = rules.get("fab") or {}
    via_d = float(fab.get("via_diameter_mm", 0.6))
    clearance = copper_clearance(rules)

    from pnr import trace

    def announce(candidate, axis, record):
        stats = {k: record.get(k) for k in ("moved", "gutter_mean_mm", "gutter_after_mean_mm")}
        recorder = trace.current()
        if recorder is not None:
            recorder.poses("compaction-" + "xy"[axis], candidate, phase="placement", **stats)
        emit(
            "route_compact",
            layout=json.loads(candidate.to_json()),
            data=dict(phase="route-compact %s (flat)" % "xy"[axis], **stats),
        )

    graphs = {id(route): placed}  # id(route) -> its placed graph (the plane-pad check)

    def reroute(candidate, axis):
        with trace.suspended():  # the flat trace's rounds stay as they were
            out = route_board(candidate, constraints, rules, pitch=pitch, max_iters=iters)
        graphs[id(out)] = candidate
        return candidate, out

    def metrics_of(r):
        m = route_summary(r)
        m["plane_blocked"] = plane_blocked(graphs[id(r)], r.tracks, r.vias, rules)
        return m

    return guarded(
        "flat",
        (placed, route),
        lambda: compact_loop(
            placed,
            route,
            constraints=constraints,
            items_of=lambda p: graph_items(p, constraints),
            copper_of=lambda r: Copper.from_routes(r.tracks, r.vias, via_d, clearance),
            reroute=reroute,
            metrics_of=metrics_of,
            min_gap=float(constraints.board.default_clearance_mm),
            outline=outline,
            label="flat",
            announce=announce,
        ),
    )
