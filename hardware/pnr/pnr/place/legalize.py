"""Legalization — snap a continuous placement to a non-overlapping one.

Global placement (:mod:`pnr.place.model`) gives good continuous positions but
with residual courtyard overlaps. This turns that into a strictly legal layout:
every movable part is snapped to a grid-aligned slot whose block is disjoint from
all others, from the fixed parts, from the keep-outs, and from the outline
border — so the result has **0 overlaps and is fully in-outline** by construction.

Algorithm (a nearest-free-fit shelf/grid packer): rasterize the outline at a fine
grid, mark fixed courtyards + keep-outs occupied, then place movable parts
biggest-first, each into the free block nearest its continuous target. Because
each part's block is ``ceil((size + clearance)/g)`` cells, disjoint blocks keep
courtyards at least ``clearance`` apart. Placing biggest-first avoids stranding
large parts once the board fills; the nearest-to-target rule preserves the
wirelength structure the global stage found.

PNR_PAIR_LANDING_RESERVE=1 (src13, default off; only when some component carries
pair_landing reserves): legalization keeps two more rasters per side (bodies
mounted there, landing reserves) so no part body is snapped onto a reserve of its
mount side and no terminal part is snapped where its own reserve lies under an
opposite-side body; refine_channels rejects moves that would do the same.

PNR_COMPACT (:mod:`pnr.place.compact`, default off): a part with an offset courtyard
(``COURTYARD``, :func:`pnr.place.geometry.body_shift`) takes the slot of its body box, so
the slot centre is ``pos + shift``: :func:`_place_part` compares the candidate centres
less the shift (``pos`` space) with the target, the hard group discs, the edge box and
the candidate cost, and every pose is set to the slot centre less the shift. ``margins``
(``LEGALIZE``) grows a part's slot by a copper margin on every side. Without either,
every slot and pose is computed exactly as before.

PNR_LEGALIZE_HPWL=<w> (:mod:`pnr.legalize_flags`, default off; the placer passes it as
``wire_weight``): every candidate slot also costs ``w`` times the part's wirelength there
(:func:`wire_cost`, plane nets left out), and the slot is chosen together with the turn: the
search runs once per quarter turn (:func:`wire_turns`, the global turn first) and keeps the
cheapest outcome. Parts on a matched-length net (``wire_exempt``) keep the plain search. With
``wire_weight`` 0 (the default) the cost and the turn search are exactly as before.
"""

from __future__ import annotations

import math
import os
from typing import Dict, List, Optional, Tuple

import numpy as np

from pnr.graph import BoardGraph, Component

from .geometry import (
    Rect,
    ReserveRect,
    body_shift,
    courtyard_rect,
    occupied_sides,
    pad_rects,
    placement_rects,
    set_component_side,
)
from .sides import STACK_PLANE
from .sides import opposite as opposite_side

# PNR_PAIR_LANDING_RESERVE=1 (pnr.place.pair_landing): besides the per-side body
# occupancy, legalize keeps two more rasters per side: ``mounted`` (bodies of parts
# MOUNTED on that side) and ``reserved`` (diff-pair via landing reserves). A part's
# body must avoid the reserves on its mount side(s); each of its own reserves must
# avoid the bodies mounted on the reserve's side. Reserves never exclude each other.
# The look-ahead (power-first) and slot-count heuristics ignore reserves; the final
# hard_violations check (metrics.overlap_pairs) sees them through placement_rects.


def _landing_blocks(comp, bw, bh, g, clearance, mounted, shift=None):
    """Reserve blocks of ``comp`` relative to its slot's top-left cell (r, c).

    Returns [(side, occupancy-to-avoid, dr0, dr1, dc0, dc1)]: rows r+dr0..r+dr1
    and cols c+dc0..c+dc1 (exclusive) with the slot centre at ((c+bw/2)g, (r+bh/2)g).
    ``shift`` (PNR_COMPACT offset courtyard) is the slot centre's offset from ``pos``.
    """
    from .pair_landing import reserve_rects

    out = []
    for side, rect in reserve_rects(comp):
        dx, dy = rect.cx - comp.pos[0], rect.cy - comp.pos[1]
        if shift is not None:
            dx, dy = dx - shift[0], dy - shift[1]
        w, h = rect.w + clearance, rect.h + clearance
        out.append(
            (
                side,
                mounted[side],
                int(math.floor(bh / 2 + (dy - h / 2) / g)),
                int(math.ceil(bh / 2 + (dy + h / 2) / g)),
                int(math.floor(bw / 2 + (dx - w / 2) / g)),
                int(math.ceil(bw / 2 + (dx + w / 2) / g)),
            )
        )
    return out


def pad_edge_rule(constraints=None, rules=None) -> Optional[Tuple[float, float]]:
    """(copper-to-edge, hole-to-edge) mm the legalizer keeps every movable part's
    pads and drills from the outline, or None (the default: courtyards only).

    ``PNR_PAD_EDGE_CLEARANCE=1`` turns it on. The values are the fab rules the
    router and DRC use: ``rules['fab']`` when given (already profile-applied),
    else the constraint file's fab block under the active fab profile. A missing
    hole-to-edge rule leaves holes to the copper rule of their pad. PNR_COMPACT
    ``LEGALIZE`` turns it on too (the compact legalizer keeps no clearance margin that
    would otherwise hold pads off the outline)."""
    if os.environ.get("PNR_PAD_EDGE_CLEARANCE") != "1":
        from pnr.compact_flags import enabled as compact_enabled

        if not compact_enabled("LEGALIZE"):
            return None
    fab = dict((rules or {}).get("fab") or {})
    if not fab and constraints is not None:
        from pnr.fab_profile import apply_fab

        fab = apply_fab(
            {
                "edge_clearance_mm": constraints.fab.edge_clearance_mm,
                "hole_to_edge_mm": constraints.fab.hole_to_edge_mm,
            }
        )
    edge = float(fab.get("edge_clearance_mm", 0.2))
    hole = fab.get("hole_to_edge_mm")
    return (edge, 0.0 if hole is None else float(hole))


def pad_edge_box(comp, pad_edge, width: float, height: float):
    """Centre box (x_lo, x_hi, y_lo, y_hi) at ``comp.rot`` in which every pad keeps
    ``pad_edge[0]`` and every drill ``pad_edge[1]`` from the outline rectangle
    (measured to the outline centreline, as the fab rules are)."""
    edge, hole = pad_edge
    px, py = comp.pos
    swap = int(round(comp.rot)) % 180 == 90
    x_lo = y_lo = -math.inf
    x_hi = y_hi = math.inf
    for pad, (_, _, r) in zip(comp.pads, pad_rects(comp)):
        boxes = []
        if r.w > 0 and r.h > 0:
            boxes.append((r.left - px, r.bottom - py, r.right - px, r.top - py, edge))
        if pad.through_hole and hole > 0 and min(pad.drill_size) > 0:
            dw, dh = (pad.drill_size[1], pad.drill_size[0]) if swap else pad.drill_size
            boxes.append(
                (
                    r.cx - px - dw / 2,
                    r.cy - py - dh / 2,
                    r.cx - px + dw / 2,
                    r.cy - py + dh / 2,
                    hole,
                )
            )
        for x0, y0, x1, y1, m in boxes:
            x_lo = max(x_lo, m - x0)
            x_hi = min(x_hi, width - m - x1)
            y_lo = max(y_lo, m - y0)
            y_hi = min(y_hi, height - m - y1)
    return (x_lo, x_hi, y_lo, y_hi)


def _band_box(box, rect, band, width, height, shift=None):
    """Narrow a centre box (x_lo, x_hi, y_lo, y_hi) so a courtyard ``rect`` (at the
    tried rotation) stays within ``band`` = (edge, tolerance) of its board edge.
    ``shift`` (PNR_COMPACT offset courtyard): the box bounds ``pos``, the rect's centre
    less that shift."""
    x_lo, x_hi, y_lo, y_hi = box
    edge, tolerance = band
    if shift is not None:
        sx, sy = shift
        if edge == "south":
            y_hi = min(y_hi, rect.h / 2 + tolerance - sy)
        elif edge == "north":
            y_lo = max(y_lo, height - rect.h / 2 - tolerance - sy)
        elif edge == "west":
            x_hi = min(x_hi, rect.w / 2 + tolerance - sx)
        else:
            x_lo = max(x_lo, width - rect.w / 2 - tolerance - sx)
        return (x_lo, x_hi, y_lo, y_hi)
    if edge == "south":
        y_hi = min(y_hi, rect.h / 2 + tolerance)
    elif edge == "north":
        y_lo = max(y_lo, height - rect.h / 2 - tolerance)
    elif edge == "west":
        x_hi = min(x_hi, rect.w / 2 + tolerance)
    else:
        x_lo = max(x_lo, width - rect.w / 2 - tolerance)
    return (x_lo, x_hi, y_lo, y_hi)


def legalize_constraint_kwargs(graph, constraints, poses, pad_edge=None) -> dict:
    """The constraint keyword arguments of :func:`legalize` for ``constraints``.

    ``poses`` are the resolved fixed poses and ``graph`` the graph keep-outs are
    resolved on. Optional relations add their keys only when the design declares
    them (``pad_edge`` when given, ``edge_bands`` for a hard ``edge_align``,
    ``regions``/``aligns`` for a ``region`` or ``align``), so every other design
    calls the legalizer exactly as before; so do the ``legalize:`` options
    (:func:`pnr.place.legal_options.legalize_kwargs`)."""
    from . import legal_options
    from .geometry import (
        hard_edge_bands,
        hard_group_edges,
        hard_group_limits,
        keepout_rects,
        resolve_hard_rotations,
    )
    from .regions import legalize_kwargs

    out = dict(
        fixed=poses,
        keepouts=keepout_rects(graph, constraints, poses),
        group_limits=hard_group_limits(constraints, poses, partial=True),
        group_edges=hard_group_edges(constraints),
        rotations=resolve_hard_rotations(constraints),
    )
    if pad_edge is not None:
        out["pad_edge"] = pad_edge
    bands = hard_edge_bands(constraints)
    if bands:
        out["edge_bands"] = bands
    out.update(legalize_kwargs(constraints, graph.components))
    # The opt-in ``legalize:`` options (pnr.place.legal_options): {} when undeclared.
    out.update(legal_options.legalize_kwargs(constraints, graph))
    return out


class LegalizationError(RuntimeError):
    """Raised when a part cannot be placed (outline too small / too full)."""


def _mark(occ: np.ndarray, g: float, rect: Rect) -> None:
    """Mark every cell touched by ``rect`` (clamped to the grid) occupied."""
    ny, nx = occ.shape
    c0 = max(0, int(math.floor(rect.left / g)))
    c1 = min(nx, int(math.ceil(rect.right / g)))
    r0 = max(0, int(math.floor(rect.bottom / g)))
    r1 = min(ny, int(math.ceil(rect.top / g)))
    if c1 > c0 and r1 > r0:
        occ[r0:r1, c0:c1] = True


def _place_part(
    occ: np.ndarray,
    g: float,
    bw: int,
    bh: int,
    target: Tuple[float, float],
    limits=(),
    candidate_cost=None,
    forbidden=(),
    attached=(),
    free=None,
    box=None,
    mask=None,
    box_label="keeping pads clear of the board edge",
    shift=None,
    bound=None,
) -> Tuple[int, int]:
    """Find the free ``bh x bw`` block nearest ``target`` (returns top-left r, c).

    ``shift`` (PNR_COMPACT offset courtyard, :func:`pnr.place.geometry.body_shift`) is
    the block centre's offset from the part's ``pos``: the target, ``limits``, ``box``
    and ``candidate_cost`` are in ``pos`` space and are compared with the block centres
    less the shift. None: block centres are poses (unchanged).

    ``attached`` (landing reserves): [(side, occupancy, dr0, dr1, dc0, dc1)] blocks
    at fixed cell offsets from the slot that must be free as well (clamped to the
    grid; a block entirely off the board is trivially free).
    ``free`` (hull macros, :func:`pnr.place.hull.free_map`) replaces the
    whole-block test on ``occ`` with a precomputed map of valid top-lefts.
    ``box`` (``PNR_PAD_EDGE_CLEARANCE=1``, :func:`pad_edge_box`) bounds the
    block centre so the part's pads and drills keep the fab edge rules; it also carries
    a hard edge band, a rectangle region and an align band.
    ``mask`` (a polygon or union region, :meth:`pnr.place.regions.LegalizeRules.mask`)
    is a map of the top-lefts whose courtyard lies inside the region.
    ``bound`` (PNR_LEGALIZE_HPWL, :func:`_cheapest`): ``(rest, cheap)`` with
    ``candidate_cost(xs, ys) == rest(xs, ys) + cheap(xs, ys)`` element for element and
    ``rest >= 0``; the same slot is found evaluating ``rest`` on fewer candidates."""
    ny, nx = occ.shape
    if free is not None:
        if free is False:
            raise LegalizationError("part exceeds grid")
        free = free.copy()
    else:
        if bw > nx or bh > ny:
            raise LegalizationError(f"part {bw}x{bh} cells exceeds grid {nx}x{ny}")

        # Integral image → O(1) block-occupancy sum for every candidate top-left.
        integ = np.zeros((ny + 1, nx + 1), dtype=np.int32)
        integ[1:, 1:] = np.cumsum(np.cumsum(occ.astype(np.int32), axis=0), axis=1)
        block = integ[bh:, bw:] - integ[:-bh, bw:] - integ[bh:, :-bw] + integ[:-bh, :-bw]
        free = block == 0
    if attached:
        rr_ = np.arange(free.shape[0])[:, None]
        cc_ = np.arange(free.shape[1])[None, :]
        for _side, occ2, dr0, dr1, dc0, dc1 in attached:
            integ2 = np.zeros((ny + 1, nx + 1), dtype=np.int32)
            integ2[1:, 1:] = np.cumsum(np.cumsum(occ2.astype(np.int32), axis=0), axis=1)
            r0, r1 = np.clip(rr_ + dr0, 0, ny), np.clip(rr_ + dr1, 0, ny)
            c0, c1 = np.clip(cc_ + dc0, 0, nx), np.clip(cc_ + dc1, 0, nx)
            free &= (integ2[r1, c1] - integ2[r0, c1] - integ2[r1, c0] + integ2[r0, c0]) == 0
    if not free.any():
        raise LegalizationError("no free slot for part")
    if mask is not None:
        free &= mask
        if not free.any():
            raise LegalizationError("no free slot inside the part's region")

    # Centre of the block for each candidate top-left (r, c).
    rows = np.arange(free.shape[0])[:, None]
    cols = np.arange(free.shape[1])[None, :]
    cx = (cols + bw / 2.0) * g
    cy = (rows + bh / 2.0) * g
    if shift is not None:
        cx, cy = cx - shift[0], cy - shift[1]
    if box is not None:
        free &= (
            (cx >= box[0] - 1e-9)
            & (cx <= box[1] + 1e-9)
            & (cy >= box[2] - 1e-9)
            & (cy <= box[3] + 1e-9)
        )
        if not free.any():
            raise LegalizationError("no free slot " + box_label)
    for ax, ay, radius in limits:
        free &= (cx - ax) ** 2 + (cy - ay) ** 2 <= radius**2 + 1e-9
    for rr, cc in forbidden:
        # Branches must explore distinct packings, not thousands of adjacent
        # quarter-mm variants of the same obstructing pose. This is bounded
        # sampling, not an exhaustive infeasibility proof.
        radius = max(0.75, min(2.0, min(bw, bh) * g * 0.25)) / g
        free &= (rows - rr) ** 2 + (cols - cc) ** 2 > radius**2
    if not free.any():
        raise LegalizationError("no free slot inside hard group radius")
    dist2 = (cx - target[0]) ** 2 + (cy - target[1]) ** 2
    if bound is not None:
        return _cheapest(dist2, free, cx, cy, *bound)
    if candidate_cost is not None:
        dist2[free] += candidate_cost(
            np.broadcast_to(cx, free.shape)[free], np.broadcast_to(cy, free.shape)[free]
        )
    dist2 = np.where(free, dist2, np.inf)
    r, c = np.unravel_index(np.argmin(dist2), dist2.shape)
    return int(r), int(c)


# _cheapest: candidates scored in full first, before the lower-bound prune.
PRUNE_FIRST = 64


def _cheapest(dist2, free, cx, cy, rest, cheap):
    """:func:`_place_part`'s choice (the first cell of least ``dist2 + rest + cheap`` among the
    free ones, row-major) with ``rest`` (the channel and soft costs, never negative and the
    expensive part) evaluated only where it can matter (PNR_LEGALIZE_HPWL).

    ``dist2 + cheap`` bounds every candidate's cost from below. The :data:`PRUNE_FIRST` cells
    of least bound are scored in full; any cell whose bound exceeds the best cost so far cannot
    win or tie, and every other cell is scored in full too. The totals are summed in the order
    :func:`_place_part` sums them (``dist2 + (rest + cheap)``, element by element), so the
    chosen cell, ties included, is the unpruned one."""
    shape = free.shape
    flat = np.flatnonzero(free)
    xs = np.broadcast_to(cx, shape).ravel()[flat]
    ys = np.broadcast_to(cy, shape).ravel()[flat]
    d2 = np.broadcast_to(dist2, shape).ravel()[flat]
    extra = np.broadcast_to(np.asarray(cheap(xs, ys), dtype=float), flat.shape)
    lower = d2 + extra
    order = np.argsort(lower, kind="stable")
    total = np.full(flat.shape, np.inf)

    def score(pick):
        total[pick] = d2[pick] + (rest(xs[pick], ys[pick]) + extra[pick])

    first = order[:PRUNE_FIRST]
    score(first)
    later = order[PRUNE_FIRST:]
    later = later[lower[later] <= total[first].min()]
    if later.size:
        score(later)
    out = np.full(free.size, np.inf)
    out[flat] = total
    r, c = np.unravel_index(np.argmin(out), shape)
    return int(r), int(c)


def _exact_outline_box(static_edge_box, width, height):
    """``static_edge_box`` intersected with the courtyard-in-outline box
    (:func:`pnr.place.legal_options.outline_box`), cached per part, rotation and side
    as ``static_edge_box`` is (``legalize: {outline: exact}``)."""
    from .legal_options import intersect, outline_box

    cache = {}

    def box(comp):
        key = (comp.ref, round(comp.rot % 360, 6), comp.side)
        hit = cache.get(key)
        if hit is None:
            hit = cache[key] = intersect(static_edge_box(comp), outline_box(comp, width, height))
        return hit

    return box


def _cached_masks(region_mask):
    """``region_mask`` cached per part, rotation, side and slot size (a region mask is
    static within one legalization; callers never write into it)."""
    cache = {}

    def mask(comp, bw, bh):
        key = (comp.ref, round(comp.rot % 360, 6), comp.side, bw, bh)
        if key not in cache:
            cache[key] = region_mask(comp, bw, bh)
        return cache[key]

    return mask


def _scarcity_blocks(blocks, movable, rules, bands):
    """``legalize: {order: scarcity}``: ``blocks`` plus a block of its own for every
    movable part held by a hard region or edge band that is in no block yet."""
    from .legal_options import region_held

    out = dict(blocks)
    for ref in sorted(region_held(movable, rules, bands)):
        out.setdefault(ref, {ref})
    return out


def _legal_held(components, rules, bands, fixed):
    """The movable parts held by a hard region or edge band (``lookahead: regions``)."""
    from .legal_options import region_held

    return region_held([c for c in components if c.ref not in fixed], rules, bands)


def _pad_limits(ref, edges, neighbors, by_ref):
    """The pad-anchored hard group discs on ``ref`` against the placed ``neighbors``
    (:func:`pnr.place.legal_options.pad_group_limits`)."""
    from .legal_options import pad_group_limits

    return pad_group_limits(ref, edges, {c.ref: c for c in neighbors}, by_ref[ref])


def _assert_inside_outline(placed, width, height, fixed):
    """``legalize: {outline: exact}``: the legalized parts pass the hard outline check."""
    from .legal_options import outline_offenders

    bad = outline_offenders(
        placed, width, height, [c.ref for c in placed.components if c.ref not in fixed]
    )
    if bad:
        raise LegalizationError("outline: exact left %s outside the outline" % ", ".join(bad))


def plane_nets(channel_model) -> frozenset:
    """The nets a channel model routes as planes (``plane_layer`` classes): their pads drop
    to the plane, so :func:`wire_cost` leaves them out. Empty without a model."""
    if channel_model is None:
        return frozenset()
    return frozenset(net for net, spec in channel_model.classes.items() if spec[2])


def wire_turns(rot: float, free: bool) -> List[float]:
    """The quarter turns ``PNR_LEGALIZE_HPWL`` tries, ``rot`` first; ``[rot]`` when the part
    may not turn (``free`` false: no rotation search, or a hard rotation)."""
    if not free:
        return [rot]
    return [(rot + 90.0 * k) % 360 for k in range(4)]


def banned_cells(entries, rotation, side, half_turns=False):
    """The banned top-left cells (``(rot, r, c, side)`` backtracking entries) that apply to a
    search at ``rotation`` on ``side``. ``half_turns`` (PNR_LEGALIZE_HPWL, parts other than hull
    macros): a slot banned at one turn is banned at the half turn too, which occupies the same
    block of cells."""
    if half_turns:
        return [
            (rr, cc)
            for rot, rr, cc, s in entries
            if s == side and abs((rot - rotation) % 180.0) < 1e-9
        ]
    return [(rr, cc) for rot, rr, cc, s in entries if rot == rotation and s == side]


def _wire_column(fields, xs, ys, wire):
    """Cost capture under PNR_LEGALIZE_HPWL: the recorded candidate field of one turn
    (columns x, y, displacement², channel, local loop) with the raw wirelength appended as a
    sixth column; without a recorded field (no channel model) the other columns are zero."""
    xs, ys, wire = np.broadcast_arrays(np.asarray(xs, float), np.asarray(ys, float), wire)
    if fields is None or len(fields) != xs.size or np.shape(fields)[1] != 5:
        zeros = np.zeros(xs.size)
        fields = np.column_stack((xs.ravel(), ys.ravel(), zeros, zeros, zeros))
    return np.column_stack((fields, wire.ravel()))


def wire_cost(comp, neighbors, movable, xs, ys, skip=frozenset()):
    """Half-perimeter wirelength (mm) of ``comp``'s nets with ``comp`` (at its current turn and
    side) posed at each candidate ``(xs, ys)``, vectorised over the candidates.

    The other pins of each net are those of ``neighbors`` (placed and fixed parts, at their
    legal poses) and ``movable`` (parts still to place, at their global targets); ``comp`` itself
    is skipped in both. Nets in ``skip`` (plane nets, :func:`plane_nets`) are left out, and a net
    with no other pin and a single pad of ``comp`` adds nothing (metrics.hpwl's definition)."""
    from .geometry import pin_positions

    xs = np.asarray(xs, dtype=float)
    ys = np.asarray(ys, dtype=float)
    total = np.zeros(np.broadcast(xs, ys).shape)
    px, py = comp.pos
    mine = {}
    for pad, (_, (x, y)) in zip(comp.pads, pin_positions(comp)):
        if pad.net and pad.net not in skip:
            mine.setdefault(pad.net, []).append((x - px, y - py))
    if not mine:
        return total
    bounds = {}
    for group in (neighbors, movable):
        for other in group:
            if other is comp:
                continue
            for pad, (_, (x, y)) in zip(other.pads, pin_positions(other)):
                if pad.net not in mine:
                    continue
                b = bounds.get(pad.net)
                bounds[pad.net] = (
                    (x, x, y, y)
                    if b is None
                    else (min(b[0], x), max(b[1], x), min(b[2], y), max(b[3], y))
                )
    for net in sorted(mine):
        offsets = mine[net]
        b = bounds.get(net)
        if b is None and len(offsets) < 2:
            continue
        ox = [o[0] for o in offsets]
        oy = [o[1] for o in offsets]
        lo_x, hi_x = xs + min(ox), xs + max(ox)
        lo_y, hi_y = ys + min(oy), ys + max(oy)
        if b is not None:
            lo_x, hi_x = np.minimum(lo_x, b[0]), np.maximum(hi_x, b[1])
            lo_y, hi_y = np.minimum(lo_y, b[2]), np.maximum(hi_y, b[3])
        total = total + (hi_x - lo_x) + (hi_y - lo_y)
    return total


def legalize(
    graph: BoardGraph,
    width: float,
    height: float,
    *,
    fixed: Dict[str, Tuple[float, float]],
    keepouts: List[Rect],
    clearance: float = 0.2,
    grid_mm: float = 0.5,
    inflation: Optional[Dict[str, float]] = None,
    spread: float = 1.0,
    group_limits: Optional[Dict[str, List[Tuple[float, float, float]]]] = None,
    channel_model=None,
    channel_weight: float = 25.0,
    allow_rotation: bool = False,
    group_edges=(),
    rotations=None,
    backtrack_budget=500,
    mobility=None,
    roles=None,
    pad_edge: Optional[Tuple[float, float]] = None,
    edge_bands: Optional[Dict[str, Tuple[str, float]]] = None,
    side_options: Optional[Dict[str, Tuple[str, ...]]] = None,
    side_cost=None,
    side_weight: float = 3.0,
    side_retry_mm: float = 1.0,
    stack: Optional[frozenset] = None,
    outline: Optional[str] = None,
    order: Optional[str] = None,
    lookahead: Optional[str] = None,
    pad_group_edges=None,
    regions=None,
    aligns=None,
    margins: Optional[Dict[str, float]] = None,
    wire_weight: Optional[float] = None,
    wire_exempt=frozenset(),
) -> BoardGraph:
    """Return a copy of ``graph`` with movable parts snapped to a legal layout.

    ``fixed`` maps refs to their held centres (placed as-is, marked as obstacles);
    ``keepouts`` are blocked regions. Movable parts are read at their current
    (continuous) ``pos`` as the placement target. ``inflation`` optionally scales
    the *reserved* footprint of a part (RePlAce cell inflation, §6): a factor > 1
    grows the slot a congested part claims so the packer spreads it into lower-
    density space — the part's real courtyard (used for the legality check) is
    unchanged. ``spread`` is a *floor* on that factor applied to **every** movable
    part, so legalization leaves routing channels between all footprints (HPWL
    global placement otherwise packs parts shoulder-to-shoulder with no room for
    tracks). ``group_limits`` intersects centre-distance discs (anchor x/y,
    radius) for each constrained part. Raises :class:`LegalizationError` if a
    part cannot fit without violating one of those discs.

    ``channel_model`` adds directional pad-escape demand to candidate costs.
    This is a soft routing estimate, not an additional legality guarantee.

    ``roles`` (power-first placement, :mod:`pnr.place.power_first`; inert when
    None) orders parts by tier inside the parent-first/hard-block order, moves
    each target by the weighted displacement of already placed cost neighbours,
    and accepts a slot only if a greedy trial pack of every unplaced
    hard-limited part still succeeds (ban and retry, then normal backtracking).

    ``pad_edge`` ((copper, hole) mm, :func:`pad_edge_rule`; None = off) keeps
    every movable part's pads and drills that far from the outline, not only its
    courtyard ``clearance / 2`` inside it.

    ``edge_bands`` ({ref: (edge, tolerance mm)}, hard ``edge_align``; None = off)
    keeps each listed part's courtyard within the tolerance of its edge: its slot
    centre is bounded like ``pad_edge`` does, per tried rotation, and its spreading
    factor is capped so the reserved slot still fits the band.

    ``side_options`` ({ref: allowed sides}, :func:`pnr.place.sides.plan`; None = off)
    lets a part with two options take the slot nearest its target on either side:
    when its current side has no slot, or only one more than ``side_retry_mm`` from
    its target, the other side is tried too and the cheaper slot wins, its cost the
    slot cost (squared displacement plus channel demand) plus ``side_weight`` (mm)
    times ``side_cost(graph)`` (mm, :func:`pnr.place.sides.side_cost` of the board
    with that choice). A part with one option keeps its side.

    ``stack`` (refs, :func:`pnr.place.sides.stack_refs`; None = off) adds a ``stack``
    occupancy plane that each of those parts tests and marks whatever its side, so two
    of them never overlap back to back on opposite sides.

    ``regions`` / ``aligns`` (the ``region`` / ``align`` constraints,
    :func:`legalize_constraint_kwargs`; None = off): a hard region bounds the slot
    centre per tried rotation (a rectangle as a box, a polygon or union as a raster
    mask), a hard align bounds it to the band the members already placed leave
    (``tol_mm``), and each aligned member's target moves onto the line. Soft ones add
    their penalty to the candidate cost. Aligned members are ordered as one block,
    and a slot that strands a later member is backtracked like a hard group's.

    ``outline`` (``legalize: {outline: exact}``, :mod:`pnr.place.legal_options`; None =
    the raster alone) bounds every slot centre so the part's courtyard stays inside the
    ``width x height`` outline by the hard check's own test, which the raster's partial
    last row and column do not.

    ``order`` (``legalize: {order: scarcity}``; None = blocks first) makes every part
    held by a hard region or edge band and in no block a block of its own, so it is
    ordered with the hard-group blocks by remaining slots per area instead of after all
    of them, and goes ahead of the active block once it has fewer free slots left than
    every part of that block. ``lookahead`` (``legalize: {lookahead: regions}``; None = off) refuses a
    slot that strands an unplaced part held by a hard group, region or edge band with
    few slots left (:func:`starves` without power-first; the nearest slot is kept when
    every tried one strands).

    ``pad_group_edges`` ([(anchor, pad, member, radius)], hard groups with
    ``anchor_pad``, :func:`pnr.place.legal_options.hard_pad_group_edges`; None = none)
    bounds each member's centre to the radius about the placed anchor's pad (the anchor
    is placed first) and orders the group as one block, like ``group_edges``.

    With hull macros (``PNR_MACRO_HULL=1``) the occupancy gains an ``inner`` plane:
    hull macros mark their inner-layer mask there, drilled parts and solid block
    macros their whole slot, so a hole never lands on block inner copper and two
    blocks' inner copper never meet.

    ``margins`` ({ref: mm}, PNR_COMPACT ``LEGALIZE``: :func:`pnr.place.compact.margins`;
    None = off) grows each listed part's slot, and a fixed part's marked area, by that
    copper margin on every side. A part with an offset courtyard (PNR_COMPACT
    ``COURTYARD``) takes the slot of its body box and keeps ``pos`` its origin.

    ``wire_weight`` (mm² per mm, ``PNR_LEGALIZE_HPWL`` as :func:`pnr.place.placer.place`
    passes it; None or 0 = off) adds ``wire_weight * wire_cost`` to every candidate's cost and to
    :func:`slot_cost`, and a part free to turn (``allow_rotation``, no hard rotation) has its
    slot searched at each of its four quarter turns, global turn first: the cheapest outcome
    wins, a tie keeps the earlier turn, and under ``lookahead: regions`` a turn whose every slot
    strands a part counts only when every turn does. Line and block macros turn as one body;
    the side retry compares the two sides' best turns; a backtracking ban covers the banned
    slot's half turn as well. ``wire_exempt`` (refs) keeps the plain search for those parts
    (the placer passes the parts on a diff-pair or length-match net:
    :func:`pnr.place.reorient.matched_refs`).
    """
    wire_weight = float(wire_weight or 0.0)
    wire_exempt = frozenset(wire_exempt or ())

    def wired(comp):
        """PNR_LEGALIZE_HPWL acts on ``comp``: the term is on and ``comp`` is not exempt."""
        return wire_weight > 0 and comp.ref not in wire_exempt

    # Plane nets drop to their plane: no wirelength term (computed only when the term is on).
    wire_skip = plane_nets(channel_model) if wire_weight > 0 else frozenset()
    inflation = inflation or {}
    margins = margins or {}
    group_limits = group_limits or {}
    rotations = rotations or {}
    g = grid_mm
    nx = int(math.ceil(width / g))
    ny = int(math.ceil(height / g))
    occupancy = {side: np.zeros((ny, nx), dtype=bool) for side in ("top", "bottom")}

    for k in keepouts:
        for occ in occupancy.values():
            _mark(occ, g, k)

    placed = BoardGraph.from_json(graph.to_json())  # deep copy
    by_ref = {c.ref: c for c in placed.components}
    from .pair_landing import enabled as landing_enabled

    landing = landing_enabled() and any(c.reserves for c in placed.components)
    mounted = (
        {side: np.zeros((ny, nx), dtype=bool) for side in ("top", "bottom")} if landing else None
    )
    reserved = (
        {side: np.zeros((ny, nx), dtype=bool) for side in ("top", "bottom")} if landing else None
    )

    def mounts_of(comp):
        if str(comp.footprint).startswith("block:"):
            from .pair_landing import macro_mount

            mount = macro_mount(comp)
            return ("top", "bottom") if mount == "both" else (mount,)
        return (comp.side,)

    # Per-side hull masks of block macros (PNR_MACRO_HULL=1, pnr.place.hull):
    # inert, and every path below unchanged, unless a component carries a hull.
    from . import hull as hullmod

    hulls = hullmod.active(placed.components)
    if hulls:
        keep_occ = np.zeros((ny, nx), dtype=bool)
        for k in keepouts:
            _mark(keep_occ, g, k)
        occupancy["inner"] = np.zeros((ny, nx), dtype=bool)

    def is_hull(comp):
        return hulls and bool(comp.hull)

    stacked = frozenset(stack or ())
    if stacked:
        occupancy[STACK_PLANE] = np.zeros((ny, nx), dtype=bool)

    def part_sides(comp):
        """Occupancy planes a slot tests and marks (drilled parts: both sides)."""
        sides = (
            ("top", "bottom") if any(p.through_hole for p in comp.pads) else occupied_sides(comp)
        )
        if (
            hulls
            and not comp.hull
            and (hullmod.drilled(comp) or str(comp.footprint).startswith("block:"))
        ):
            sides = tuple(sides) + ("inner",)
        if comp.ref in stacked:
            sides = tuple(sides) + (STACK_PLANE,)
        return sides

    def slot_dims(comp, infl, cr=None):
        """Slot (bw, bh) cells of ``comp`` at its rotation: its courtyard grown by
        ``infl``, the clearance and (PNR_COMPACT ``LEGALIZE``) its copper margin."""
        cr = courtyard_rect(comp) if cr is None else cr
        m = margins.get(comp.ref)
        if m:
            return (
                int(math.ceil((cr.w * infl + clearance + 2 * m) / g)),
                int(math.ceil((cr.h * infl + clearance + 2 * m) / g)),
            )
        return (
            int(math.ceil((cr.w * infl + clearance) / g)),
            int(math.ceil((cr.h * infl + clearance) / g)),
        )

    boxes = {}
    bands = edge_bands or {}
    # Regions and aligns (pnr.place.regions): None unless the design declares one;
    # built once the hard rotations and fixed poses are applied (below).
    rules = None
    if regions or aligns:
        from .regions import _intersect as intersect_boxes

    def constrained(comp):
        return rules is not None and rules.applies(comp.ref)

    def static_edge_box(comp):
        """pad_edge_box at comp.rot, narrowed to the part's hard edge band and its hard
        rectangle regions (None when none applies)."""
        band = bands.get(comp.ref)
        if pad_edge is None and band is None and not constrained(comp):
            return None
        # Per side too: a flipped part's pads and off-centre body are mirrored.
        key = (comp.ref, round(comp.rot % 360, 6), comp.side)
        hit = boxes.get(key)
        if hit is None:
            if pad_edge is None:
                hit = (-math.inf, math.inf, -math.inf, math.inf)
            else:
                hit = pad_edge_box(comp, pad_edge, width, height)
            if band is not None:
                hit = _band_box(
                    hit, courtyard_rect(comp), band, width, height, shift=body_shift(comp)
                )
            if constrained(comp):
                region = rules.static_box(comp)
                if region is not None:
                    hit = intersect_boxes(hit, region)
            boxes[key] = hit
        return hit

    if outline == "exact":
        # legalize: {outline: exact}: every box also keeps the courtyard in the outline.
        static_edge_box = _exact_outline_box(static_edge_box, width, height)

    def edge_box(comp):
        """static_edge_box, narrowed to the part's hard align band (None when none
        applies)."""
        hit = static_edge_box(comp)
        if constrained(comp):
            dynamic = rules.align_box(comp, {c.ref: c for c in neighbors}, by_ref)
            if dynamic is not None:
                hit = intersect_boxes(hit, dynamic)
        return hit

    def centre_limits(comp, rot):
        """The slot centres ``comp`` could take at ``rot`` on an empty board: its block
        on the grid, inside static_edge_box; None when there is none. The align bands
        of the other members of its aligns are built from these
        (pnr.place.regions.LegalizeRules.reach). A part with a hard edge band has the
        block its band cap gives it, and its bounds move inward onto its slot lattice
        (exact: a partner placed first then leaves it a slot); any other part could
        have its block shrunk to its courtyard by the align cap (spreading), so its
        bounds are those of that smallest block (never too narrow)."""
        saved = comp.rot
        comp.rot = rot
        try:
            cr = courtyard_rect(comp)
            banded = comp.ref in bands
            infl = spreading(comp, aligned=False) if banded else 1.0
            bw, bh = slot_dims(comp, infl, cr)
            if bw > nx or bh > ny:
                return None
            box = ((bw / 2.0) * g, (nx - bw / 2.0) * g, (bh / 2.0) * g, (ny - bh / 2.0) * g)
            static = static_edge_box(comp)
            shift = body_shift(comp)
            if shift is not None:
                # Offset courtyard: work in slot centres, return poses (centre - shift).
                if static is not None:
                    static = (
                        static[0] + shift[0],
                        static[1] + shift[0],
                        static[2] + shift[1],
                        static[3] + shift[1],
                    )
            if static is not None:
                box = intersect_boxes(box, static)
            if not banded:
                ok = box[0] <= box[1] + 1e-9 and box[2] <= box[3] + 1e-9
                if ok and shift is not None:
                    box = (
                        box[0] - shift[0],
                        box[1] - shift[0],
                        box[2] - shift[1],
                        box[3] - shift[1],
                    )
                return box if ok else None
            lo_c = math.ceil(box[0] / g - bw / 2.0 - 1e-9)
            hi_c = math.floor(box[1] / g - bw / 2.0 + 1e-9)
            lo_r = math.ceil(box[2] / g - bh / 2.0 - 1e-9)
            hi_r = math.floor(box[3] / g - bh / 2.0 + 1e-9)
            if lo_c > hi_c or lo_r > hi_r:
                return None
            if shift is not None:
                return (
                    (lo_c + bw / 2.0) * g - shift[0],
                    (hi_c + bw / 2.0) * g - shift[0],
                    (lo_r + bh / 2.0) * g - shift[1],
                    (hi_r + bh / 2.0) * g - shift[1],
                )
            return (
                (lo_c + bw / 2.0) * g,
                (hi_c + bw / 2.0) * g,
                (lo_r + bh / 2.0) * g,
                (hi_r + bh / 2.0) * g,
            )
        finally:
            comp.rot = saved

    def region_mask(comp, bw, bh):
        """The hard polygon/union region mask of comp at comp.rot (None when none)."""
        if not constrained(comp) or bw > nx or bh > ny:
            return None
        shift = body_shift(comp)
        return rules.mask(
            comp,
            bw,
            bh,
            g,
            (ny - bh + 1, nx - bw + 1),
            **({} if shift is None else dict(shift=shift)),
        )

    if lookahead == "regions":
        # The look-ahead counts the slots of many region parts per slot: cache the masks.
        region_mask = _cached_masks(region_mask)

    def box_label_of(comp):
        if not constrained(comp):
            return {}
        return dict(box_label="inside its edge band, region and align band")

    def target_of(comp):
        """The slot target: comp.pos, with an aligned axis moved onto its line."""
        if not constrained(comp):
            return comp.pos
        return rules.target(comp, {c.ref: c for c in neighbors}, comp.pos)

    def spreading(comp, aligned=True):
        """The part's slot inflation: the spread floor or its feedback inflation, capped
        for a hard edge part so the slot fits its band, and (``aligned``) for a
        hard-aligned part so the slot fits beside the board edge its align band runs
        along."""
        infl = max(1.0, spread, float(inflation.get(comp.ref, 1.0)))
        band = bands.get(comp.ref)
        m2 = 2 * margins.get(comp.ref, 0.0) if margins else 0.0
        if band is not None:
            cr = courtyard_rect(comp)
            normal = cr.h if band[0] in ("south", "north") else cr.w
            infl = min(infl, max(1.0, 1.0 + (2 * band[1] - clearance - m2 - g) / normal))
        if aligned and constrained(comp):
            bound = rules.align_box(comp, {c.ref: c for c in neighbors}, by_ref)
            if bound is not None:
                cr = courtyard_rect(comp)
                for k, normal, size in ((0, cr.w, width), (1, cr.h, height)):
                    # The widest block centred in [lo, hi] that stays on the board.
                    lo, hi = bound[2 * k], bound[2 * k + 1]
                    room = 2 * min(hi, size - lo, size / 2)
                    infl = min(infl, max(1.0, (room - clearance - m2 - g) / normal))
        return infl

    free_cache = {}

    def hull_free(comp, bw, bh, reserves=True):
        """Valid top-lefts of a hull slot at comp.rot (False when it cannot fit the grid).

        Cached on the occupancy content: selection keys and the look-ahead ask
        again and again while nothing changed. Callers copy before mutating.
        ``reserves=False``: ignore the landing reserves, as the look-ahead and the
        slot-count heuristic do for every other part (src13 design; src15 review fix:
        they saw the reserves for hull macros only)."""
        if bw > nx or bh > ny:
            return False
        import hashlib

        digest = hashlib.blake2b(occupancy["top"].tobytes(), digest_size=16)
        digest.update(occupancy["bottom"].tobytes())
        digest.update(occupancy["inner"].tobytes())
        avoid = ()
        if landing and reserves:
            # PNR_PAIR_LANDING_RESERVE with hull macros (src15 merge): the hull mask
            # on every side the macro is mounted on must also miss the landing
            # reserves there (as a solid macro's whole slot must, below).
            avoid = mounts_of(comp)
            for m in avoid:
                digest.update(reserved[m].tobytes())
        key = (comp.ref, round(comp.rot % 360, 6), comp.side, bw, bh, avoid, digest.digest())
        hit = free_cache.get(key)
        if hit is None:
            masks = hullmod.slot_masks(comp, g, clearance, bw, bh)
            occ_in = (
                occupancy
                if not avoid
                else dict(occupancy, **{m: occupancy[m] | reserved[m] for m in avoid})
            )
            hit = hullmod.free_map(occ_in, keep_occ, masks, bw, bh)
            hit.setflags(write=False)
            if len(free_cache) > 512:
                free_cache.clear()
            free_cache[key] = hit
        return hit

    def hull_turns(comp, base):
        """A rectangle only needs {rot, rot+90} (rot+180 has the same footprint);
        a hull does not, so a free hull macro tries every quarter turn."""
        return [base] + [(base + 90 * k) % 360 for k in (1, 2, 3)]

    def mark_slot(comp, r, c, bw, bh, sides):
        if is_hull(comp):
            masks = hullmod.slot_masks(comp, g, clearance, bw, bh)
            for side in hullmod.PLANES:
                occupancy[side][r : r + bh, c : c + bw] |= masks[side]
        else:
            for side in sides:
                occupancy[side][r : r + bh, c : c + bw] = True

    def slot_pose(comp, r, c, bw, bh):
        """The pose of ``comp`` (at its rotation and side) in the slot at (r, c): the slot
        centre, less the offset courtyard's shift (PNR_COMPACT)."""
        shift = body_shift(comp)
        if shift is None:
            return ((c + bw / 2.0) * g, (r + bh / 2.0) * g)
        return ((c + bw / 2.0) * g - shift[0], (r + bh / 2.0) * g - shift[1])

    def displaced(comp, r, c, bw, bh):
        return math.dist(target_of(comp), slot_pose(comp, r, c, bw, bh))

    def slot_cost(comp, outcome, neighbors):
        """Cost of a :func:`search` outcome (its side and turn set on ``comp``), measured
        from the slot target (an aligned axis on its line, :func:`target_of`)."""
        if outcome[2] is not None:
            return math.inf
        _, _, _, r, c, bw, bh = outcome[:7]
        x, y = slot_pose(comp, r, c, bw, bh)
        tx, ty = target_of(comp)
        cost = (x - tx) ** 2 + (y - ty) ** 2
        if channel_model is not None:
            cost += channel_weight * float(channel_model.penalty(comp, neighbors, x, y))
        if rules is not None and rules.has_soft(comp.ref):
            extra = rules.soft_cost(comp, {q.ref: q for q in neighbors}, x, y)
            cost += 0.0 if extra is None else float(extra)
        if side_cost is not None:
            cost += side_weight * float(side_cost(placed))
        if wired(comp):  # PNR_LEGALIZE_HPWL
            cost += wire_weight * float(wire_cost(comp, neighbors, movable, x, y, wire_skip))
        return cost

    aid = None
    if roles is not None:
        from .power_first import LOOK_AHEAD_TRIES, LegalizeAid

        aid = LegalizeAid(roles, placed.components)
    neighbors = []
    cost_records = []
    # Bounds follow the real legalized neighbour positions, in both directions.
    parents = {}
    for anchor, member, radius in group_edges:
        parents.setdefault(member, set()).add(anchor)
    for anchor, _pad, member, _radius in pad_group_edges or ():
        parents.setdefault(member, set()).add(anchor)  # pad-anchored hard groups

    def limits_for(ref):
        points = {c.ref: c.pos for c in neighbors}
        limits = list(group_limits.get(ref, ()))
        for anchor, member, radius in group_edges:
            if member == ref and anchor in points:
                limits.append((*points[anchor], radius))
            if anchor == ref and member in points:
                limits.append((*points[member], radius))
        if pad_group_edges:
            limits += _pad_limits(ref, pad_group_edges, neighbors, by_ref)
        return limits

    for ref, angle in rotations.items():
        if ref in by_ref:
            by_ref[ref].rot = angle

    # Fixed parts: pin at their pose, mark occupied.
    for ref, (px, py) in fixed.items():
        comp = by_ref.get(ref)
        if comp is None:
            continue
        comp.pos = (px, py)
        neighbors.append(comp)
        if any(math.hypot(px - ax, py - ay) > radius + 1e-9 for ax, ay, radius in limits_for(ref)):
            raise LegalizationError(f"fixed part {ref} lies outside hard group radius")
        # PNR_COMPACT LEGALIZE: a fixed part's body area also carries its copper margin.
        grow = clearance + 2 * margins[ref] if ref in margins else clearance
        for side, rect in placement_rects(comp):
            if landing and isinstance(rect, ReserveRect):
                _mark(
                    reserved[side],
                    g,
                    Rect(rect.cx, rect.cy, rect.w + clearance, rect.h + clearance),
                )
                continue
            if side in occupancy:
                _mark(
                    occupancy[side],
                    g,
                    Rect(rect.cx, rect.cy, rect.w + grow, rect.h + grow),
                )
            if landing and side in mounted and getattr(rect, "mount", None) in (side, "both"):
                _mark(
                    mounted[side], g, Rect(rect.cx, rect.cy, rect.w + clearance, rect.h + clearance)
                )
        if comp.ref in stacked:
            cr = courtyard_rect(comp)
            _mark(occupancy[STACK_PLANE], g, Rect(cr.cx, cr.cy, cr.w + clearance, cr.h + clearance))

    if regions or aligns:
        from .regions import LegalizeRules

        # Align lines start at the median of the global anchors (fixed parts at their pose).
        rules = LegalizeRules(
            regions,
            aligns,
            placed.components,
            grid_mm=g,
            limits=centre_limits,
            # The rotations the slot search tries per part (below).
            turns=lambda m: (
                wire_turns(m.rot, allow_rotation and m.ref not in rotations)
                if wired(m)
                else [m.rot]
                + ([(m.rot + 90) % 360] if allow_rotation and m.ref not in rotations else [])
            ),
        )

    # Minimum-remaining-slots ordering accounts for actual fixed obstacles and
    # intersections of group discs. Radius alone can let a flexible neighbor
    # consume the only legal site of another equally constrained component.
    movable = [c for c in placed.components if c.ref not in fixed]

    def available_pose(comp):
        cr = courtyard_rect(comp)
        infl = (
            spreading(comp)
            if bands or rules is not None
            else max(1.0, spread, float(inflation.get(comp.ref, 1.0)))
        )
        bw, bh = slot_dims(comp, infl, cr)
        occ = np.logical_or.reduce([occupancy[side] for side in part_sides(comp)])
        if bw > nx or bh > ny:
            return 0
        if is_hull(comp):
            free = hull_free(comp, bw, bh, reserves=False).copy()
        else:
            integ = np.zeros((ny + 1, nx + 1), dtype=np.int32)
            integ[1:, 1:] = np.cumsum(np.cumsum(occ.astype(np.int32), axis=0), axis=1)
            free = (integ[bh:, bw:] - integ[:-bh, bw:] - integ[bh:, :-bw] + integ[:-bh, :-bw]) == 0
        cx = (np.arange(free.shape[1])[None, :] + bw / 2) * g
        cy = (np.arange(free.shape[0])[:, None] + bh / 2) * g
        shift = body_shift(comp)
        if shift is not None:  # offset courtyard: compare poses, not slot centres
            cx, cy = cx - shift[0], cy - shift[1]
        for ax, ay, radius in limits_for(comp.ref):
            free &= (cx - ax) ** 2 + (cy - ay) ** 2 <= radius**2 + 1e-9
        box = edge_box(comp)
        if box is not None:
            free &= (
                (cx >= box[0] - 1e-9)
                & (cx <= box[1] + 1e-9)
                & (cy >= box[2] - 1e-9)
                & (cy <= box[3] + 1e-9)
            )
        mask = region_mask(comp, bw, bh)
        if mask is not None:
            free &= mask
        return int(free.sum())

    def available(comp):
        count = available_pose(comp)
        if count or not allow_rotation or comp.ref in rotations:
            return count
        if is_hull(comp):
            previous = comp.rot
            for turn in hull_turns(comp, previous)[1:]:
                comp.rot = turn
                count = available_pose(comp)
                if count:
                    break
            comp.rot = previous
            return count
        previous = comp.rot
        comp.rot = (previous + 90) % 360
        count = available_pose(comp)
        comp.rot = previous
        return count

    def starves(comp, r, c, bw, bh, sides):
        """Power-first look-ahead: does this slot strand an unplaced hard-limited part?

        Greedy trial pack on a copy of the occupancy: every unplaced part with a
        hard limit, in minimum-remaining-slots order, at its nearest relative
        target, trying both rotations. Everything is restored afterwards.
        """
        saved = {side: occ_.copy() for side, occ_ in occupancy.items()}
        poses = [(m, m.pos, m.rot) for m in movable] + [(comp, comp.pos, comp.rot)]
        count = len(neighbors)
        try:
            mark_slot(comp, r, c, bw, bh, sides)
            comp.pos = slot_pose(comp, r, c, bw, bh)
            neighbors.append(comp)
            pending = [m for m in movable if limits_for(m.ref)]
            pending.sort(
                key=lambda m: (available(m), -courtyard_rect(m).w * courtyard_rect(m).h, m.ref)
            )
            for m in pending:
                sides_m = part_sides(m)
                occ_m = np.logical_or.reduce([occupancy[side] for side in sides_m])
                infl_m = (
                    spreading(m)
                    if bands or rules is not None
                    else max(1.0, spread, float(inflation.get(m.ref, 1.0)))
                )
                target = aid.target(m.ref, neighbors)
                turns = (
                    ([m.rot] if not allow_rotation or m.ref in rotations else hull_turns(m, m.rot))
                    if is_hull(m)
                    else [m.rot]
                    + ([(m.rot + 90) % 360] if allow_rotation and m.ref not in rotations else [])
                )
                for rot in turns:
                    m.rot = rot
                    bw_, bh_ = slot_dims(m, infl_m)
                    shift_m = body_shift(m)
                    try:
                        rr, cc = _place_part(
                            occ_m,
                            g,
                            bw_,
                            bh_,
                            target,
                            limits_for(m.ref),
                            free=hull_free(m, bw_, bh_, reserves=False) if is_hull(m) else None,
                            box=edge_box(m),
                            **({} if shift_m is None else dict(shift=shift_m)),
                        )
                    except LegalizationError:
                        continue
                    mark_slot(m, rr, cc, bw_, bh_, sides_m)
                    m.pos = slot_pose(m, rr, cc, bw_, bh_)
                    neighbors.append(m)
                    break
                else:
                    return True
            return False
        finally:
            del neighbors[count:]
            for m, pos, rot in poses:
                m.pos = pos
                m.rot = rot
            for side in occupancy:
                occupancy[side][:] = saved[side]

    # legalize: {lookahead: regions} (pnr.place.legal_options): the parts a slot may
    # strand are those held by a hard region or edge band, besides the hard-group ones.
    held = _legal_held(placed.components, rules, bands, fixed) if lookahead == "regions" else set()

    def strands(comp, r, c, bw, bh, sides, skip=(), only=None):
        """``legalize: {lookahead: regions}``: the first unplaced part this slot strands
        (None when it strands none).

        :func:`starves` without power-first: a greedy trial pack, on a copy of the
        occupancy, of the unplaced parts held by a hard group, region or edge band whose
        reach meets this slot and that keep at most ``SCARCE_SLOTS`` free slots once it
        is taken (but those in ``skip``), in minimum-remaining-slots order, each at its
        slot target inside its edge box and region mask, trying its turns. Everything is
        restored afterwards. ``only`` (a part): pack it alone without taking the slot,
        to tell whether it is stranded anyway."""
        from .legal_options import SCARCE_SLOTS, reaches

        saved = {side: occ_.copy() for side, occ_ in occupancy.items()}
        poses = [(m, m.pos, m.rot) for m in movable] + [(comp, comp.pos, comp.rot)]
        count = len(neighbors)
        try:
            pending = []
            if only is not None:
                pending.append((0, 0.0, only.ref, only))
            else:
                mark_slot(comp, r, c, bw, bh, sides)
                comp.pos = slot_pose(comp, r, c, bw, bh)
                neighbors.append(comp)
                slot = (c * g, (c + bw) * g, r * g, (r + bh) * g)
                for m in movable:
                    limits = limits_for(m.ref)
                    if m.ref in skip or (not limits and m.ref not in held):
                        continue
                    if not reaches(m, slot, limits, static_edge_box(m), rules, width, height):
                        continue
                    n = available(m)
                    if n <= SCARCE_SLOTS:
                        area = courtyard_rect(m).w * courtyard_rect(m).h
                        pending.append((n, -area, m.ref, m))
                pending.sort(key=lambda item: item[:3])
            for _, _, _, m in pending:
                sides_m = part_sides(m)
                occ_m = np.logical_or.reduce([occupancy[side] for side in sides_m])
                infl_m = (
                    spreading(m)
                    if bands or rules is not None
                    else max(1.0, spread, float(inflation.get(m.ref, 1.0)))
                )
                target = target_of(m)
                turns = (
                    ([m.rot] if not allow_rotation or m.ref in rotations else hull_turns(m, m.rot))
                    if is_hull(m)
                    else [m.rot]
                    + ([(m.rot + 90) % 360] if allow_rotation and m.ref not in rotations else [])
                )
                for rot in turns:
                    m.rot = rot
                    # PNR_COMPACT: the slot carries the copper margin and an offset
                    # courtyard's shift, as in :func:`starves` (none with the flags off).
                    bw_, bh_ = slot_dims(m, infl_m)
                    shift_m = body_shift(m)
                    try:
                        rr, cc = _place_part(
                            occ_m,
                            g,
                            bw_,
                            bh_,
                            target,
                            limits_for(m.ref),
                            free=hull_free(m, bw_, bh_, reserves=False) if is_hull(m) else None,
                            box=edge_box(m),
                            mask=region_mask(m, bw_, bh_),
                            **({} if shift_m is None else dict(shift=shift_m)),
                        )
                    except LegalizationError:
                        continue
                    mark_slot(m, rr, cc, bw_, bh_, sides_m)
                    m.pos = slot_pose(m, rr, cc, bw_, bh_)
                    neighbors.append(m)
                    break
                else:
                    return m
            return None
        finally:
            del neighbors[count:]
            for m, pos, rot in poses:
                m.pos = pos
                m.rot = rot
            for side in occupancy:
                occupancy[side][:] = saved[side]

    def lookahead_slot(comp, r, c, bw, bh, sides, rotation, place_again):
        """``legalize: {lookahead: regions}``: ``(r, c, True)`` for the first of up to
        LOOK_AHEAD_TRIES slots at this turn (``place_again(tried)`` gives the next nearest
        one) that strands no part; ``(r, c, False)`` for the nearest slot itself when every
        tried one does (the search then tries the part's next turn, and keeps this slot,
        as without the look-ahead, when no turn does better). A part that cannot be placed
        even without this part's slot is stranded anyway (no slot of ``comp`` helps it;
        backtracking will) and is left out of the check."""
        from .power_first import LOOK_AHEAD_TRIES

        tried = [
            (rr, cc)
            for rot, rr, cc, side in banned.get(comp.ref, ())
            if rot == rotation and side == comp.side
        ]
        first = (r, c)
        stuck, attempts = set(), 0
        while attempts < LOOK_AHEAD_TRIES:
            culprit = strands(comp, r, c, bw, bh, sides, skip=stuck)
            if culprit is None:
                return r, c, True
            if strands(comp, r, c, bw, bh, sides, only=culprit) is not None:
                stuck.add(culprit.ref)  # stranded anyway: check this slot again without it
                continue
            attempts += 1
            tried.append((r, c))
            try:
                r, c = place_again(tried)
            except LegalizationError:
                break
        return first + (False,)

    # Finish each electrically constrained connected block before unrelated
    # footprints consume its local escape/decoupling space. Source radii remain
    # exact; this changes ordering, never legality or fixed poses.
    adjacency = {}
    for anchor, member, _ in group_edges:
        adjacency.setdefault(anchor, set()).add(member)
        adjacency.setdefault(member, set()).add(anchor)
    for anchor, _pad, member, _radius in pad_group_edges or ():
        adjacency.setdefault(anchor, set()).add(member)
        adjacency.setdefault(member, set()).add(anchor)
    if rules is not None:
        # Aligned members share one band: place them as one block.
        for a, b in rules.edges():
            if a in by_ref and b in by_ref:
                adjacency.setdefault(a, set()).add(b)
                adjacency.setdefault(b, set()).add(a)
    blocks = {}
    for ref in sorted(adjacency):
        if ref in blocks:
            continue
        pending = [ref]
        members = set()
        while pending:
            v = pending.pop()
            if v in members:
                continue
            members.add(v)
            pending.extend(adjacency.get(v, ()))
        for v in members:
            blocks[v] = members
    if order == "scarcity":
        blocks = _scarcity_blocks(blocks, movable, rules, bands)

    def scarce_first(active, eligible):
        """``order: scarcity``: a part in a block of its own (held by a region or an edge
        band) with fewer free slots left than every eligible part of the active block goes
        ahead of the block (minimum remaining slots across blocks); else ``active``."""
        singles = [
            c for c in eligible if len(blocks.get(c.ref, ())) == 1 and c.ref not in active_block
        ]
        if not singles:
            return active
        counts = {c.ref: available(c) for c in singles}
        best = min(
            singles,
            key=lambda c: (counts[c.ref], -courtyard_rect(c).w * courtyard_rect(c).h, c.ref),
        )
        return [best] if counts[best.ref] < min(available(c) for c in active) else active

    active_block = set()
    trail = []  # backtracking: (state, ref, chosen slot)
    banned = {}
    backtracks = 0
    while movable:
        placed_refs = {c.ref for c in neighbors}
        ready = [c for c in movable if parents.get(c.ref, set()) <= placed_refs]
        # A cycle is still checked symmetrically as its vertices become placed.
        eligible = ready or movable
        active = [c for c in eligible if c.ref in active_block]
        if not active:
            grouped = [c for c in eligible if c.ref in blocks]
            if grouped:

                def block_rank(c):
                    area = sum(
                        courtyard_rect(by_ref[v]).w * courtyard_rect(by_ref[v]).h
                        for v in blocks[c.ref]
                        if v not in placed_refs
                    )
                    return (available(c) / max(area, 0.01), -area, c.ref)

                root = min(grouped, key=block_rank)
                active_block = blocks[root.ref]
                active = [c for c in eligible if c.ref in active_block]
        elif order == "scarcity":
            active = scarce_first(active, eligible)
        if aid is None:
            comp = min(
                active or eligible,
                key=lambda c: (available(c), -courtyard_rect(c).w * courtyard_rect(c).h, c.ref),
            )
        else:
            # Mixed-size legalization: block macros (pnr.hier.macro, footprint 'block:*') take their
            # slots before any tier, as their members' tiers are already fixed inside the layout.
            comp = min(
                active or eligible,
                key=lambda c: (
                    0 if str(c.footprint).startswith("block:") else aid.tier(c.ref),
                    available(c),
                    -courtyard_rect(c).w * courtyard_rect(c).h,
                    c.ref,
                ),
            )
            comp.pos = aid.target(comp.ref, neighbors)
        state = dict(
            occupancy={s: a.copy() for s, a in occupancy.items()},
            mounted={s: a.copy() for s, a in mounted.items()} if landing else None,
            reserved={s: a.copy() for s, a in reserved.items()} if landing else None,
            neighbors=list(neighbors),
            movable=list(movable),
            active=set(active_block),
            poses={c.ref: (c.pos, c.rot) for c in placed.components},
            sides={c.ref: c.side for c in placed.components} if side_options else None,
            records=list(cost_records),
            banned={k: set(v) for k, v in banned.items()},
        )
        movable.remove(comp)
        original_rotation = comp.rot
        from .cost_capture import folder as cost_folder
        from .cost_capture import legalizer_decision

        capturing = cost_folder() is not None
        # search_scored (PNR_LEGALIZE_HPWL): set when search() fell back to a stranding slot.
        strand_note = [False]

        def search(only=None):
            """The nearest legal slot for ``comp`` on its current side, trying its turns:
            ``(error, r, c, bw, bh, sides, attached, captured_fields)``. ``only`` (a turn,
            :func:`search_scored`): try that turn alone."""
            r = c = None
            bw = bh = 0
            infl = max(1.0, spread, float(inflation.get(comp.ref, 1.0)))
            # A hard edge band, or a hard align: the inflation is capped per tried rotation.
            banded = comp.ref in bands or (rules is not None and comp.ref in rules.align_of)
            sides = part_sides(comp)
            occ = np.logical_or.reduce([occupancy[side] for side in sides])
            if landing:
                occ = occ | np.logical_or.reduce([reserved[m] for m in mounts_of(comp)])
            attached = ()
            error = None
            captured_fields = {}
            # PNR_LEGALIZE_HPWL: a ban covers the half turn (same block), hull macros aside.
            half_turns = wired(comp) and not is_hull(comp)
            turns = [original_rotation] + (
                [(original_rotation + 90) % 360]
                if allow_rotation and comp.ref not in rotations
                else []
            )
            if is_hull(comp) and allow_rotation and comp.ref not in rotations:
                turns = hull_turns(comp, original_rotation)
            if only is not None:
                turns = [only]
            stranded = None  # lookahead: regions: the first turn's slot if every one strands
            for rotation in turns:
                comp.rot = rotation
                cr = courtyard_rect(comp)
                if banded:
                    infl = spreading(comp)  # capped per tried rotation (edge band, align band)
                bw, bh = slot_dims(comp, infl, cr)
                # PNR_COMPACT offset courtyard at this turn and side (None: centred part).
                shift = body_shift(comp)
                shifted = {} if shift is None else dict(shift=shift)
                attached = (
                    _landing_blocks(comp, bw, bh, g, clearance, mounted, **shifted)
                    if landing
                    else ()
                )
                target = target_of(comp)
                try:
                    candidate_cost = (
                        None
                        if channel_model is None
                        else (
                            lambda xs, ys: channel_weight
                            * channel_model.penalty(comp, neighbors, xs, ys)
                        )
                    )
                    if capturing:

                        def candidate_cost(xs, ys):
                            channel = (
                                0.0
                                if channel_model is None
                                else channel_model.penalty(comp, neighbors, xs, ys)
                            )
                            if np.asarray(xs).ndim:
                                xx, yy, ch = np.broadcast_arrays(xs, ys, channel)
                                captured_fields[rotation] = np.column_stack(
                                    (
                                        xx,
                                        yy,
                                        (xx - comp.pos[0]) ** 2 + (yy - comp.pos[1]) ** 2,
                                        ch,
                                        np.zeros(xx.shape),
                                    )
                                )
                            return channel_weight * channel

                    if rules is not None and rules.has_soft(comp.ref):
                        # Soft region / align: their penalty joins the candidate cost.
                        def candidate_cost(xs, ys, _base=candidate_cost):
                            extra = rules.soft_cost(comp, {c.ref: c for c in neighbors}, xs, ys)
                            value = 0.0 if _base is None else _base(xs, ys)
                            return value + (0.0 if extra is None else extra)

                    bound = None
                    if wired(comp):
                        rest = candidate_cost  # the channel and soft costs (or None)
                        if not capturing and not (rules is not None and rules.has_soft(comp.ref)):
                            # The same slot, scoring the channel cost on fewer candidates.
                            bound = (
                                (lambda xs, ys: 0.0) if rest is None else rest,
                                lambda xs, ys: wire_weight
                                * wire_cost(comp, neighbors, movable, xs, ys, wire_skip),
                            )

                        # PNR_LEGALIZE_HPWL: w times the part's wirelength at each candidate.
                        def candidate_cost(xs, ys, _base=candidate_cost, _turn=rotation):
                            value = 0.0 if _base is None else _base(xs, ys)
                            wire = wire_cost(comp, neighbors, movable, xs, ys, wire_skip)
                            if capturing:
                                captured_fields[_turn] = _wire_column(
                                    captured_fields.get(_turn), xs, ys, wire
                                )
                            return value + wire_weight * wire

                    slot_free = hull_free(comp, bw, bh) if is_hull(comp) else None
                    slot_mask = region_mask(comp, bw, bh)
                    r, c = _place_part(
                        occ,
                        g,
                        bw,
                        bh,
                        target,
                        limits_for(comp.ref),
                        candidate_cost=candidate_cost,
                        forbidden=banned_cells(
                            banned.get(comp.ref, ()), rotation, comp.side, half_turns
                        ),
                        attached=attached,
                        free=slot_free,
                        box=edge_box(comp),
                        mask=slot_mask,
                        **box_label_of(comp),
                        **shifted,
                        **({} if bound is None else dict(bound=bound)),
                    )
                    if aid is not None:
                        tried = banned_cells(
                            banned.get(comp.ref, ()), rotation, comp.side, half_turns
                        )
                        for attempt in range(LOOK_AHEAD_TRIES):
                            if not starves(comp, r, c, bw, bh, sides):
                                break
                            tried.append((r, c))
                            if attempt == LOOK_AHEAD_TRIES - 1:
                                raise LegalizationError(
                                    "look-ahead: every tried slot strands a hard-limited part"
                                )
                            r, c = _place_part(
                                occ,
                                g,
                                bw,
                                bh,
                                target,
                                limits_for(comp.ref),
                                candidate_cost=candidate_cost,
                                forbidden=tried,
                                attached=attached,
                                free=slot_free,
                                box=edge_box(comp),
                                mask=slot_mask,
                                **box_label_of(comp),
                                **shifted,
                            )
                    elif lookahead == "regions":
                        r, c, clear = lookahead_slot(
                            comp,
                            r,
                            c,
                            bw,
                            bh,
                            sides,
                            rotation,
                            lambda tried: _place_part(
                                occ,
                                g,
                                bw,
                                bh,
                                target,
                                limits_for(comp.ref),
                                candidate_cost=candidate_cost,
                                forbidden=tried,
                                attached=attached,
                                free=slot_free,
                                box=edge_box(comp),
                                mask=slot_mask,
                                **box_label_of(comp),
                                **({} if bound is None else dict(bound=bound)),
                            ),
                        )
                        if not clear:  # every tried slot strands a part: the next turn first
                            stranded = stranded or (rotation, r, c, bw, bh, attached)
                            raise LegalizationError("look-ahead: every tried slot strands a part")
                    error = None
                    break
                except LegalizationError as exc:
                    error = exc
            if error is not None and stranded is not None:
                # No turn has a slot that strands nothing: the first one's nearest slot.
                (comp.rot, r, c, bw, bh, attached), error = stranded, None
                strand_note[0] = True
            return error, r, c, bw, bh, sides, attached, captured_fields

        def search_scored():
            """PNR_LEGALIZE_HPWL: :func:`search` once per turn of :func:`wire_turns` (the
            global turn first), keeping the outcome of least :func:`slot_cost` (displacement
            squared, channel demand, soft rules, side cost and the wirelength term); a tie
            (within 1e-9) keeps the earlier turn, and a turn whose every slot strands a part
            (``lookahead: regions``) counts only when every turn does. Returns
            :func:`search`'s tuple with ``comp.rot`` at the chosen turn (the global turn and
            the first error when no turn has a slot)."""
            best = best_key = first = None
            fields = {}
            for rotation in wire_turns(
                original_rotation, allow_rotation and comp.ref not in rotations
            ):
                strand_note[0] = False
                outcome = search(only=rotation)
                fields.update(outcome[7])
                if outcome[0] is not None:
                    first = first or (original_rotation, outcome)
                    continue
                key = (strand_note[0], slot_cost(comp, (comp.side, comp.rot) + outcome, neighbors))
                if (
                    best is None
                    or key[0] < best_key[0]
                    or (key[0] == best_key[0] and key[1] < best_key[1] - 1e-9)
                ):
                    best, best_key = (comp.rot, outcome), key
            rotation, outcome = best or first
            comp.rot = rotation
            return outcome[:7] + (fields,)

        # PNR_LEGALIZE_HPWL chooses the turn with the slot; otherwise the first turn that fits.
        run_search = search_scored if wired(comp) else search
        error, r, c, bw, bh, sides, attached, captured_fields = run_search()
        if (
            side_options
            and len(side_options.get(comp.ref, ())) > 1
            and (error is not None or displaced(comp, r, c, bw, bh) > side_retry_mm)
        ):
            # A part free to take either side (pnr.place.sides) also tries the other
            # side, and keeps whichever slot is cheaper including the side cost.
            here = (comp.side, comp.rot, error, r, c, bw, bh, sides, attached, captured_fields)
            set_component_side(comp, opposite_side(comp.side))
            comp.rot = original_rotation
            there = (comp.side, None) + run_search()
            there = (there[0], comp.rot) + there[2:]
            far = slot_cost(comp, there, neighbors)
            set_component_side(comp, here[0])
            comp.rot = here[1]
            near = slot_cost(comp, here, neighbors)
            if far < near - 1e-9:
                set_component_side(comp, there[0])
                comp.rot = there[1]
                _, _, error, r, c, bw, bh, sides, attached, captured_fields = there
        if (
            error is not None
            and trail
            and (
                group_edges or group_limits or pad_group_edges or (rules is not None and rules.hard)
            )
            and backtracks < backtrack_budget
        ):
            previous, chosen_ref, chosen_pose = trail.pop()
            occupancy = previous["occupancy"]
            neighbors = previous["neighbors"]
            if landing:
                mounted = previous["mounted"]
                reserved = previous["reserved"]
            movable = previous["movable"]
            active_block = previous["active"]
            for ref, (pos, rot) in previous["poses"].items():
                by_ref[ref].pos = pos
                by_ref[ref].rot = rot
            for ref, side in (previous["sides"] or {}).items():
                set_component_side(by_ref[ref], side)
            cost_records = previous["records"]
            banned = previous["banned"]
            banned.setdefault(chosen_ref, set()).add(chosen_pose)
            backtracks += 1
            continue
        if error is not None:
            comp.rot = original_rotation
            import json
            import os
            from pathlib import Path

            debug = os.environ.get("PNR_PLACEMENT_DIAGNOSTICS")
            if debug:
                Path(debug).write_text(
                    json.dumps(
                        dict(
                            failed=comp.ref,
                            remaining=[c.ref for c in movable],
                            placed=[c.ref for c in neighbors],
                            graph=json.loads(placed.to_json()),
                            limits=group_limits,
                            grid_mm=g,
                            backtracks=backtracks,
                            keepouts=[vars(k) for k in keepouts],
                        ),
                        indent=2,
                    )
                )
            raise LegalizationError(f"{comp.ref}: {error}") from error
        if capturing:
            target = comp.pos
            position = ((c + bw / 2) * g, (r + bh / 2) * g)
            if body_shift(comp) is not None:  # offset courtyard: the pose, not the slot
                position = slot_pose(comp, r, c, bw, bh)
            channel = (
                0.0
                if channel_model is None
                else float(channel_model.penalty(comp, neighbors, *position))
            )
            # Keep only the accepted search path. Rejected branches must not
            # serialize full graphs or become displayed legalization decisions.
            cost_records.append(
                dict(
                    ref=comp.ref,
                    target=target,
                    position=position,
                    rotation=comp.rot,
                    neighbors=[n.ref for n in neighbors],
                    fields=captured_fields,
                    chosen=(
                        (position[0] - target[0]) ** 2 + (position[1] - target[1]) ** 2,
                        channel,
                        0.0,
                    )
                    + (
                        # PNR_LEGALIZE_HPWL: the wirelength term of the chosen slot.
                        (
                            float(
                                wire_cost(
                                    comp,
                                    neighbors,
                                    movable,
                                    *slot_pose(comp, r, c, bw, bh),
                                    wire_skip,
                                )
                            ),
                        )
                        if wired(comp)
                        else ()
                    ),
                    poses={q.ref: (q.pos, q.rot) for q in placed.components},
                    sides={q.ref: q.side for q in placed.components},
                )
            )
        # A slot is banned on the side it was taken (the other side's cell is free).
        trail.append((state, comp.ref, (comp.rot, r, c, comp.side)))
        banned.pop(comp.ref, None)
        if is_hull(comp):
            mark_slot(comp, r, c, bw, bh, sides)
        else:
            for side in sides:
                occupancy[side][r : r + bh, c : c + bw] = True
        if landing:
            if is_hull(comp):
                # A hull macro's body on each mount side is its hull mask there.
                masks = hullmod.slot_masks(comp, g, clearance, bw, bh)
                for m in mounts_of(comp):
                    mounted[m][r : r + bh, c : c + bw] |= masks[m]
            else:
                for m in mounts_of(comp):
                    mounted[m][r : r + bh, c : c + bw] = True
            for side, _occ, dr0, dr1, dc0, dc1 in attached:
                reserved[side][
                    max(0, r + dr0) : max(0, r + dr1), max(0, c + dc0) : max(0, c + dc1)
                ] = True
        comp.pos = slot_pose(comp, r, c, bw, bh)
        neighbors.append(comp)

    if outline == "exact":
        _assert_inside_outline(placed, width, height, fixed)

    from pnr import trace as _trace

    if _trace.current() is not None:  # PNR_TRACE_DIR only
        _trace.legal([ref for _state, ref, _pose in trail], placed, backtracks)
    if cost_records:
        import hashlib
        import json
        from pathlib import Path

        from .cost_capture import save

        records = []
        replay = BoardGraph.from_json(placed.to_json())
        replay_refs = {c.ref: c for c in replay.components}
        for v in cost_records:
            for ref, (pos, rot) in v["poses"].items():
                replay_refs[ref].pos = pos
                replay_refs[ref].rot = rot
            for ref, side in v["sides"].items():
                set_component_side(replay_refs[ref], side)
            records.append(
                legalizer_decision(
                    replay,
                    v["ref"],
                    v["target"],
                    v["position"],
                    v["rotation"],
                    [replay_refs[ref] for ref in v["neighbors"]],
                    v["fields"],
                    v["chosen"],
                    g,
                    channel_weight,
                    # PNR_LEGALIZE_HPWL: the wirelength term of a part it acted on.
                    **({} if len(v["chosen"]) < 4 else dict(wire_weight=wire_weight)),
                )
            )
        save(
            "legalizer-complete",
            dict(
                graph=json.loads(placed.to_json()),
                decisions=[
                    dict(path=p, sha256=hashlib.sha256(Path(p).read_bytes()).hexdigest())
                    for p in records
                ],
                mobility=mobility or {},
                fixed_refs=sorted(fixed),
                hard_group_edges=list(group_edges),
                hard_rotations=rotations,
                backtracks=backtracks,
                scope="Actual per-component sequential legalizer decisions; each field retains its own previously occupied neighbors",
            ),
        )
    return placed


def refine_channels(
    graph,
    width,
    height,
    *,
    fixed,
    keepouts,
    channel_model,
    group_limits=None,
    channel_weight=5.0,
    max_move_mm=2.0,
    passes=3,
    clearance=0.2,
    grid_mm=0.25,
):
    """Improve an already legal checkpoint without snapping every footprint.

    Each accepted move reduces local channel pressure and its combined movement
    cost, while fitting the outline, other courtyards, keepouts and hard groups.
    A bounded move cannot establish electrical quality; native routing/DRC and
    power-layout review remain necessary. Fixed poses are never changed.
    """
    placed = BoardGraph.from_json(graph.to_json())
    targets = {c.ref: tuple(c.pos) for c in graph.components}
    limits = group_limits or {}
    g = grid_mm
    for _ in range(passes):
        changed = False
        for comp in placed.components:
            if comp.ref in fixed or any(p.through_hole for p in comp.pads):
                continue
            others = [c for c in placed.components if c is not comp]
            before = float(channel_model.penalty(comp, others, *comp.pos))
            if before <= 1e-9:
                continue
            occ = np.zeros((int(math.ceil(height / g)), int(math.ceil(width / g))), dtype=bool)
            for keepout in keepouts:
                _mark(occ, g, keepout)
            sides = set(occupied_sides(comp))
            for other in others:
                for side, cr in placement_rects(other):
                    if side in sides:
                        _mark(occ, g, Rect(cr.cx, cr.cy, cr.w + clearance, cr.h + clearance))
            cr = courtyard_rect(comp)
            bw, bh = (int(math.ceil((size + clearance) / g)) for size in (cr.w, cr.h))
            target = targets[comp.ref]
            shift = body_shift(comp)  # PNR_COMPACT offset courtyard (None: centred)
            try:
                row, col = _place_part(
                    occ,
                    g,
                    bw,
                    bh,
                    target,
                    list(limits.get(comp.ref, ())) + [(*target, max_move_mm)],
                    candidate_cost=lambda xs, ys: channel_weight
                    * channel_model.penalty(comp, others, xs, ys),
                    **({} if shift is None else dict(shift=shift)),
                )
            except LegalizationError:
                continue  # Keep a valid current pose if no improved legal slot exists.
            candidate = ((col + bw / 2) * g, (row + bh / 2) * g)
            if shift is not None:
                candidate = (candidate[0] - shift[0], candidate[1] - shift[1])
            if comp.reserves:
                from .pair_landing import enabled as landing_enabled

                if landing_enabled():
                    # PNR_PAIR_LANDING_RESERVE: the moved part's own landing
                    # reserves must stay clear of opposite-side bodies too.
                    saved, comp.pos = comp.pos, candidate
                    blocked = any(
                        side == other_side and rect.overlaps(other_rect)
                        for side, rect in placement_rects(comp)
                        if isinstance(rect, ReserveRect)
                        for other in others
                        for other_side, other_rect in placement_rects(other)
                    )
                    comp.pos = saved
                    if blocked:
                        continue
            after = float(channel_model.penalty(comp, others, *candidate))
            cost_before = math.dist(comp.pos, target) ** 2 + channel_weight * before
            cost_after = math.dist(candidate, target) ** 2 + channel_weight * after
            if after < before - 1e-9 and cost_after < cost_before - 1e-9:
                comp.pos = candidate
                changed = True
        if not changed:
            break
    return placed
