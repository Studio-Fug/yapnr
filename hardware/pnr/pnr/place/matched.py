"""Matched lengths after legalization: keep the legs of each pair and group even.

Global placement pulls the members of every declared differential pair and
length-match group toward equal estimated lengths (:func:`pnr.place.model.length_mismatch`),
but the legalizer then packs each part into the free slot nearest its target, so
two series resistors that sat side by side can end up at different distances from
the connector and the MCU. A meander adds only what fits beside the route; a leg
10-20 mm longer than its partner is out of reach. :func:`refine_matched` moves the
small parts on the matched nets (at most :data:`MAX_PADS` pads, not fixed or held by
another hard constraint) to the legal slot that best evens the estimated lengths, at
a price for the move and for the wirelength it adds, and keeps the legalized
placement whenever that would add a hard violation. A design without pairs or
groups never gets here.

The estimate is the 45-degree routing distance (octile) between the two pins of a
two-pin net, and the half perimeter of a larger net's pins.

PNR_COMPACT (:mod:`pnr.place.compact`, default off): a moved part with an offset
courtyard takes the slot of its body box (pose = slot centre less its shift), and
``margins`` grows every slot by the parts' copper margins, as in the legalizer.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from pnr.graph import BoardGraph

from .geometry import (
    Rect,
    body_shift,
    courtyard_rect,
    occupied_sides,
    pin_positions,
    placement_rects,
)
from .legalize import LegalizationError, _mark, _place_part, pad_edge_box

# Parts a pair's legs run through (series resistors, ESD arrays, common-mode chokes)
# have few pads; moving an IC or a connector would cost every other net.
MAX_PADS = 4
# Candidate price: per mm of estimated length mismatch, per mm of added wirelength;
# the move itself costs its squared displacement (mm^2), as in the legalizer.
W_MISMATCH = 25.0
W_WIRELENGTH = 5.0
# A move must even the estimate by at least this much (mm).
MIN_GAIN_MM = 0.25
PASSES = 3
SQRT2M1 = math.sqrt(2.0) - 1.0


def matched_sets(constraints, graph: BoardGraph) -> List[Tuple[str, ...]]:
    """Member nets of each declared pair and group, all present with >= 2 pins."""
    pins = {n.name: len(n.pins) for n in graph.nets}
    sets = [(dp.p, dp.n) for dp in getattr(constraints, "diff_pairs", None) or []]
    sets += [tuple(lm.nets) for lm in getattr(constraints, "length_matches", None) or []]
    out = []
    for members in sets:
        members = tuple(dict.fromkeys(members))
        if len(members) >= 2 and all(pins.get(n, 0) >= 2 for n in members):
            out.append(members)
    return out


def _length(xs: Sequence[np.ndarray], ys: Sequence[np.ndarray]) -> np.ndarray:
    """Estimated routed length of one net from its pins' coordinates (arrays over
    candidates, or scalars): octile for two pins, half perimeter for more."""
    if len(xs) == 2:
        dx, dy = np.abs(xs[0] - xs[1]), np.abs(ys[0] - ys[1])
        return np.maximum(dx, dy) + SQRT2M1 * np.minimum(dx, dy)
    return (
        np.max(np.stack(np.broadcast_arrays(*xs)), 0)
        - np.min(np.stack(np.broadcast_arrays(*xs)), 0)
        + np.max(np.stack(np.broadcast_arrays(*ys)), 0)
        - np.min(np.stack(np.broadcast_arrays(*ys)), 0)
    )


def _hpwl(xs, ys):
    return (
        np.max(np.stack(np.broadcast_arrays(*xs)), 0)
        - np.min(np.stack(np.broadcast_arrays(*xs)), 0)
        + np.max(np.stack(np.broadcast_arrays(*ys)), 0)
        - np.min(np.stack(np.broadcast_arrays(*ys)), 0)
    )


class _Pins:
    """Pin coordinates by net, with one component's pins movable by (dx, dy)."""

    def __init__(self, graph: BoardGraph):
        self.at: Dict[Tuple[str, str], Tuple[float, float]] = {}
        for comp in graph.components:
            for name, xy in pin_positions(comp):
                self.at[(comp.ref, name)] = xy
        self.nets = {n.name: [tuple(p) for p in n.pins if tuple(p) in self.at] for n in graph.nets}

    def coords(self, net: str, ref: Optional[str] = None, dx=0.0, dy=0.0):
        xs, ys = [], []
        for pin in self.nets[net]:
            x, y = self.at[pin]
            if pin[0] == ref:
                x, y = x + dx, y + dy
            xs.append(x)
            ys.append(y)
        return xs, ys

    def length(self, net: str, ref: Optional[str] = None, dx=0.0, dy=0.0):
        return _length(*self.coords(net, ref, dx, dy))


def mismatch(graph: BoardGraph, sets: Sequence[Tuple[str, ...]]) -> List[float]:
    """Estimated length spread (mm) of each set on ``graph``."""
    pins = _Pins(graph)
    out = []
    for members in sets:
        lengths = [float(pins.length(n)) for n in members]
        out.append(max(lengths) - min(lengths))
    return out


def _held_refs(constraints) -> set:
    """Parts another hard constraint holds in place (fixed poses, rows, line groups,
    hard edge bands, regions...). Group radii, sides and rotations stay honoured by
    the move itself."""
    from pnr.constraints import Enforcement

    held = set(getattr(constraints, "locked_refs", None) or ())
    for con in constraints.constraints:
        if con.enforcement is Enforcement.HARD and con.kind not in ("group", "side", "orientation"):
            held.update(con.refs)
    return held


def refine_matched(
    graph: BoardGraph,
    constraints,
    width: float,
    height: float,
    *,
    fixed: Dict[str, Tuple[float, float]],
    keepouts: List[Rect],
    group_limits=None,
    clearance: float = 0.2,
    grid_mm: float = 0.25,
    spread: float = 1.0,
    inflation: Optional[Dict[str, float]] = None,
    pad_edge: Optional[Tuple[float, float]] = None,
    margins: Optional[Dict[str, float]] = None,
) -> BoardGraph:
    """Move the small parts on matched nets to even each set's estimated lengths
    (module docstring). Returns a new graph; ``graph`` itself when nothing moves.
    ``margins`` ({ref: mm}, PNR_COMPACT ``LEGALIZE``) grows each part's slot."""
    sets = matched_sets(constraints, graph)
    if not sets:
        return graph
    from .metrics import hard_violations

    placed = BoardGraph.from_json(graph.to_json())
    held = _held_refs(constraints) | set(fixed)
    limits = group_limits or {}
    inflation = inflation or {}
    member_of: Dict[str, List[int]] = {}
    for k, members in enumerate(sets):
        for net in members:
            member_of.setdefault(net, []).append(k)
    g = grid_mm
    moved = False
    for _ in range(PASSES):
        changed = False
        for comp in sorted(placed.components, key=lambda c: c.ref):
            if comp.ref in held or len(comp.pads) > MAX_PADS:
                continue
            if any(p.through_hole for p in comp.pads):
                continue
            touched = sorted({k for p in comp.pads for k in member_of.get(p.net, ())})
            if not touched:
                continue
            pins = _Pins(placed)
            before = sum(
                max(float(pins.length(n)) for n in sets[k])
                - min(float(pins.length(n)) for n in sets[k])
                for k in touched
            )
            if before <= MIN_GAIN_MM:
                continue
            others = [c for c in placed.components if c is not comp]
            ny, nx = int(math.ceil(height / g)), int(math.ceil(width / g))
            occ = np.zeros((ny, nx), dtype=bool)
            for keepout in keepouts:
                _mark(occ, g, keepout)
            sides = set(occupied_sides(comp))
            for other in others:
                infl = max(1.0, spread, float(inflation.get(other.ref, 1.0)))
                grow = (
                    clearance + 2 * margins[other.ref]
                    if margins and other.ref in margins
                    else clearance
                )
                for side, cr in placement_rects(other):
                    if side in sides:
                        _mark(
                            occ,
                            g,
                            Rect(cr.cx, cr.cy, cr.w * infl + grow, cr.h * infl + grow),
                        )
            cr = courtyard_rect(comp)
            infl = max(1.0, spread, float(inflation.get(comp.ref, 1.0)))
            grow = (
                clearance + 2 * margins[comp.ref] if margins and comp.ref in margins else clearance
            )
            bw, bh = (int(math.ceil((size * infl + grow) / g)) for size in (cr.w, cr.h))
            shift = body_shift(comp)  # PNR_COMPACT offset courtyard (None: centred)
            x0, y0 = comp.pos
            nets = sorted({p.net for p in comp.pads if p.net and p.net in pins.nets})

            def cost(xs, ys, comp=comp, touched=touched, nets=nets, pins=pins, x0=x0, y0=y0):
                dx, dy = xs - x0, ys - y0
                spread_mm = np.zeros_like(xs)
                for k in touched:
                    lengths = np.stack(
                        np.broadcast_arrays(*[pins.length(n, comp.ref, dx, dy) for n in sets[k]])
                    )
                    spread_mm = spread_mm + lengths.max(0) - lengths.min(0)
                added = np.zeros_like(xs)
                for net in nets:
                    if len(pins.nets[net]) >= 2:
                        added = (
                            added
                            + _hpwl(*pins.coords(net, comp.ref, dx, dy))
                            - _hpwl(*pins.coords(net))
                        )
                return W_MISMATCH * spread_mm + W_WIRELENGTH * added

            try:
                row, col = _place_part(
                    occ,
                    g,
                    bw,
                    bh,
                    (x0, y0),
                    list(limits.get(comp.ref, ())),
                    candidate_cost=cost,
                    box=None if pad_edge is None else pad_edge_box(comp, pad_edge, width, height),
                    **({} if shift is None else dict(shift=shift)),
                )
            except LegalizationError:
                continue
            candidate = ((col + bw / 2) * g, (row + bh / 2) * g)
            if shift is not None:
                candidate = (candidate[0] - shift[0], candidate[1] - shift[1])
            cx, cy = np.array([candidate[0]]), np.array([candidate[1]])
            after_cost = float(cost(cx, cy)[0]) + math.dist(candidate, (x0, y0)) ** 2
            dx, dy = candidate[0] - x0, candidate[1] - y0
            after = sum(
                max(float(pins.length(n, comp.ref, dx, dy)) for n in sets[k])
                - min(float(pins.length(n, comp.ref, dx, dy)) for n in sets[k])
                for k in touched
            )
            if after <= before - MIN_GAIN_MM and after_cost < W_MISMATCH * before - 1e-9:
                comp.pos = candidate
                changed = moved = True
        if not changed:
            break
    if not moved:
        return graph
    base = hard_violations(graph, constraints, clearance=0.0)
    now = hard_violations(placed, constraints, clearance=0.0)
    if any(len(now.get(k) or []) > len(base.get(k) or []) for k in now):
        return graph
    return placed
