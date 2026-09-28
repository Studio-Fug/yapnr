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
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

import numpy as np
from pnr.graph import BoardGraph, Component

from .geometry import Rect, courtyard_rect, occupied_sides, placement_rects


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
    occ: np.ndarray, g: float, bw: int, bh: int, target: Tuple[float, float],
    limits=(), candidate_cost=None, forbidden=(),
) -> Tuple[int, int]:
    """Find the free ``bh x bw`` block nearest ``target`` (returns top-left r, c)."""
    ny, nx = occ.shape
    if bw > nx or bh > ny:
        raise LegalizationError(f"part {bw}x{bh} cells exceeds grid {nx}x{ny}")

    # Integral image → O(1) block-occupancy sum for every candidate top-left.
    integ = np.zeros((ny + 1, nx + 1), dtype=np.int32)
    integ[1:, 1:] = np.cumsum(np.cumsum(occ.astype(np.int32), axis=0), axis=1)
    block = integ[bh:, bw:] - integ[:-bh, bw:] - integ[bh:, :-bw] + integ[:-bh, :-bw]
    free = block == 0
    if not free.any():
        raise LegalizationError("no free slot for part")

    # Centre of the block for each candidate top-left (r, c).
    rows = np.arange(free.shape[0])[:, None]
    cols = np.arange(free.shape[1])[None, :]
    cx = (cols + bw / 2.0) * g
    cy = (rows + bh / 2.0) * g
    for ax, ay, radius in limits:
        free &= (cx - ax) ** 2 + (cy - ay) ** 2 <= radius ** 2 + 1e-9
    for rr, cc in forbidden:
        # Branches must explore distinct packings, not thousands of adjacent
        # quarter-mm variants of the same obstructing pose. This is bounded
        # sampling, not an exhaustive infeasibility proof.
        radius=max(.75, min(2., min(bw,bh)*g*.25)) / g
        free &= (rows-rr)**2+(cols-cc)**2 > radius**2
    if not free.any():
        raise LegalizationError("no free slot inside hard group radius")
    dist2 = (cx - target[0]) ** 2 + (cy - target[1]) ** 2
    if candidate_cost is not None:
        dist2[free] += candidate_cost(np.broadcast_to(cx, free.shape)[free],
                                     np.broadcast_to(cy, free.shape)[free])
    dist2 = np.where(free, dist2, np.inf)
    r, c = np.unravel_index(np.argmin(dist2), dist2.shape)
    return int(r), int(c)


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
    """
    inflation = inflation or {}
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
    def limits_for(ref):
        points = {c.ref:c.pos for c in neighbors}
        limits = list(group_limits.get(ref, ()))
        for anchor, member, radius in group_edges:
            if member == ref and anchor in points:limits.append((*points[anchor], radius))
            if anchor == ref and member in points:limits.append((*points[member], radius))
        return limits
    for ref, angle in rotations.items():
        if ref in by_ref:by_ref[ref].rot = angle

    # Fixed parts: pin at their pose, mark occupied.
    for ref, (px, py) in fixed.items():
        comp = by_ref.get(ref)
        if comp is None:
            continue
        comp.pos = (px, py)
        neighbors.append(comp)
        if any(math.hypot(px - ax, py - ay) > radius + 1e-9
               for ax, ay, radius in limits_for(ref)):
            raise LegalizationError(f"fixed part {ref} lies outside hard group radius")
        for side, rect in placement_rects(comp):
            _mark(occupancy[side], g, Rect(rect.cx, rect.cy, rect.w + clearance, rect.h + clearance))

    # Minimum-remaining-slots ordering accounts for actual fixed obstacles and
    # intersections of group discs. Radius alone can let a flexible neighbor
    # consume the only legal site of another equally constrained component.
    movable = [c for c in placed.components if c.ref not in fixed]
    def available_pose(comp):
        cr = courtyard_rect(comp)
        infl = max(1.0, spread, float(inflation.get(comp.ref, 1.0)))
        bw = int(math.ceil((cr.w * infl + clearance) / g))
        bh = int(math.ceil((cr.h * infl + clearance) / g))
        occ = np.logical_or.reduce([occupancy[side] for side in (('top','bottom') if any(p.through_hole for p in comp.pads) else occupied_sides(comp))])
        if bw > nx or bh > ny:return 0
        integ = np.zeros((ny+1,nx+1),dtype=np.int32)
        integ[1:,1:] = np.cumsum(np.cumsum(occ.astype(np.int32),axis=0),axis=1)
        free = (integ[bh:,bw:]-integ[:-bh,bw:]-integ[bh:,:-bw]+integ[:-bh,:-bw]) == 0
        cx=(np.arange(free.shape[1])[None,:]+bw/2)*g
        cy=(np.arange(free.shape[0])[:,None]+bh/2)*g
        for ax,ay,radius in limits_for(comp.ref):
            free &= (cx-ax)**2+(cy-ay)**2 <= radius**2+1e-9
        return int(free.sum())
    def available(comp):
        count=available_pose(comp)
        if count or not allow_rotation or comp.ref in rotations:return count
        previous=comp.rot;comp.rot=(previous+90)%360
        count=available_pose(comp);comp.rot=previous
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
            for side in sides:
                occupancy[side][r:r + bh, c:c + bw] = True
            comp.pos = ((c + bw / 2.0) * g, (r + bh / 2.0) * g)
            neighbors.append(comp)
            pending = [m for m in movable if limits_for(m.ref)]
            pending.sort(key=lambda m: (available(m), -courtyard_rect(m).w * courtyard_rect(m).h, m.ref))
            for m in pending:
                sides_m = ('top','bottom') if any(p.through_hole for p in m.pads) else occupied_sides(m)
                occ_m = np.logical_or.reduce([occupancy[side] for side in sides_m])
                infl_m = max(1.0, spread, float(inflation.get(m.ref, 1.0)))
                target = aid.target(m.ref, neighbors)
                for rot in [m.rot] + ([(m.rot + 90) % 360] if allow_rotation and m.ref not in rotations else []):
                    m.rot = rot
                    cr = courtyard_rect(m)
                    bw_ = int(math.ceil((cr.w * infl_m + clearance) / g))
                    bh_ = int(math.ceil((cr.h * infl_m + clearance) / g))
                    try:
                        rr, cc = _place_part(occ_m, g, bw_, bh_, target, limits_for(m.ref))
                    except LegalizationError:
                        continue
                    for side in sides_m:
                        occupancy[side][rr:rr + bh_, cc:cc + bw_] = True
                    m.pos = ((cc + bw_ / 2.0) * g, (rr + bh_ / 2.0) * g)
                    neighbors.append(m)
                    break
                else:
                    return True
            return False
        finally:
            del neighbors[count:]
            for m, pos, rot in poses:
                m.pos = pos; m.rot = rot
            for side in occupancy:
                occupancy[side][:] = saved[side]
    # Finish each electrically constrained connected block before unrelated
    # footprints consume its local escape/decoupling space. Source radii remain
    # exact; this changes ordering, never legality or fixed poses.
    adjacency = {}
    for anchor, member, _ in group_edges:
        adjacency.setdefault(anchor, set()).add(member)
        adjacency.setdefault(member, set()).add(anchor)
    blocks = {}
    for ref in sorted(adjacency):
        if ref in blocks: continue
        pending = [ref]; members = set()
        while pending:
            v = pending.pop()
            if v in members: continue
            members.add(v); pending.extend(adjacency.get(v, ()))
        for v in members: blocks[v] = members
    active_block = set()
    stack = []
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
                    area = sum(courtyard_rect(by_ref[v]).w * courtyard_rect(by_ref[v]).h
                               for v in blocks[c.ref] if v not in placed_refs)
                    return (available(c) / max(area, .01), -area, c.ref)
                root = min(grouped, key=block_rank)
                active_block = blocks[root.ref]
                active = [c for c in eligible if c.ref in active_block]
        if aid is None:
            comp = min(active or eligible,key=lambda c:(available(c),-courtyard_rect(c).w*courtyard_rect(c).h,c.ref))
        else:
            comp = min(active or eligible,key=lambda c:(aid.tier(c.ref),available(c),-courtyard_rect(c).w*courtyard_rect(c).h,c.ref))
            comp.pos = aid.target(comp.ref, neighbors)
        state = dict(occupancy={s:a.copy() for s,a in occupancy.items()},
                     neighbors=list(neighbors), movable=list(movable), active=set(active_block),
                     poses={c.ref:(c.pos,c.rot) for c in placed.components},
                     records=list(cost_records), banned={k:set(v) for k,v in banned.items()})
        movable.remove(comp)
        infl = max(1.0, spread, float(inflation.get(comp.ref, 1.0)))
        sides = ('top','bottom') if any(p.through_hole for p in comp.pads) else occupied_sides(comp)
        occ = np.logical_or.reduce([occupancy[side] for side in sides])
        original_rotation=comp.rot
        error=None
        from .cost_capture import folder as cost_folder, legalizer_decision
        capturing=cost_folder() is not None
        captured_fields={}
        for rotation in [original_rotation]+([(original_rotation+90)%360] if allow_rotation and comp.ref not in rotations else []):
            comp.rot=rotation
            cr=courtyard_rect(comp)
            bw=int(math.ceil((cr.w*infl+clearance)/g))
            bh=int(math.ceil((cr.h*infl+clearance)/g))
            try:
                candidate_cost = None if channel_model is None else (
                    lambda xs, ys: channel_weight * channel_model.penalty(comp, neighbors, xs, ys))
                if capturing:
                    def candidate_cost(xs, ys):
                        channel=0. if channel_model is None else channel_model.penalty(comp,neighbors,xs,ys)
                        if np.asarray(xs).ndim:
                            xx,yy,ch=np.broadcast_arrays(xs,ys,channel)
                            captured_fields[rotation]=np.column_stack((xx,yy,(xx-comp.pos[0])**2+(yy-comp.pos[1])**2,ch,np.zeros(xx.shape)))
                        return channel_weight*channel
                r,c=_place_part(occ,g,bw,bh,comp.pos,limits_for(comp.ref),candidate_cost=candidate_cost,
                                forbidden=[(rr,cc) for rot,rr,cc in banned.get(comp.ref,()) if rot==rotation])
                if aid is not None:
                    tried=[(rr,cc) for rot,rr,cc in banned.get(comp.ref,()) if rot==rotation]
                    for attempt in range(LOOK_AHEAD_TRIES):
                        if not starves(comp,r,c,bw,bh,sides):
                            break
                        tried.append((r,c))
                        if attempt==LOOK_AHEAD_TRIES-1:
                            raise LegalizationError("look-ahead: every tried slot strands a hard-limited part")
                        r,c=_place_part(occ,g,bw,bh,comp.pos,limits_for(comp.ref),candidate_cost=candidate_cost,forbidden=tried)
                error=None
                break
            except LegalizationError as exc:error=exc
        if error is not None and stack and (group_edges or group_limits) and backtracks < backtrack_budget:
            previous, chosen_ref, chosen_pose = stack.pop()
            occupancy = previous['occupancy']; neighbors = previous['neighbors']
            movable = previous['movable']; active_block = previous['active']
            for ref,(pos,rot) in previous['poses'].items():by_ref[ref].pos=pos;by_ref[ref].rot=rot
            cost_records=previous['records'];banned=previous['banned']
            banned.setdefault(chosen_ref,set()).add(chosen_pose)
            backtracks += 1
            continue
        if error is not None:
            comp.rot=original_rotation
            import os, json
            from pathlib import Path
            debug=os.environ.get('PNR_PLACEMENT_DIAGNOSTICS')
            if debug:
                Path(debug).write_text(json.dumps(dict(failed=comp.ref,remaining=[c.ref for c in movable],placed=[c.ref for c in neighbors],graph=json.loads(placed.to_json()),limits=group_limits,grid_mm=g,backtracks=backtracks,keepouts=[vars(k) for k in keepouts]),indent=2))
            raise LegalizationError(f"{comp.ref}: {error}") from error
        if capturing:
            target=comp.pos;position=((c+bw/2)*g,(r+bh/2)*g)
            channel=0. if channel_model is None else float(channel_model.penalty(comp,neighbors,*position))
            # Keep only the accepted search path. Rejected branches must not
            # serialize full graphs or become displayed legalization decisions.
            cost_records.append(dict(ref=comp.ref,target=target,position=position,
                rotation=comp.rot,neighbors=[n.ref for n in neighbors],fields=captured_fields,
                chosen=((position[0]-target[0])**2+(position[1]-target[1])**2,channel,0.),
                poses={q.ref:(q.pos,q.rot) for q in placed.components}))
        stack.append((state,comp.ref,(comp.rot,r,c)))
        banned.pop(comp.ref,None)
        for side in sides:
            occupancy[side][r : r + bh, c : c + bw] = True
        comp.pos = ((c + bw / 2.0) * g, (r + bh / 2.0) * g)
        neighbors.append(comp)

    if cost_records:
        import hashlib,json
        from pathlib import Path
        from .cost_capture import save
        records=[]
        replay=BoardGraph.from_json(placed.to_json());replay_refs={c.ref:c for c in replay.components}
        for v in cost_records:
            for ref,(pos,rot) in v['poses'].items():replay_refs[ref].pos=pos;replay_refs[ref].rot=rot
            records.append(legalizer_decision(replay,v['ref'],v['target'],v['position'],v['rotation'],
                [replay_refs[ref] for ref in v['neighbors']],v['fields'],v['chosen'],g,channel_weight))
        save('legalizer-complete',dict(graph=json.loads(placed.to_json()),decisions=[dict(path=p,sha256=hashlib.sha256(Path(p).read_bytes()).hexdigest()) for p in records],mobility=mobility or {},fixed_refs=sorted(fixed),hard_group_edges=list(group_edges),hard_rotations=rotations,backtracks=backtracks,scope='Actual per-component sequential legalizer decisions; each field retains its own previously occupied neighbors'))
    return placed


def refine_channels(graph, width, height, *, fixed, keepouts, channel_model,
                    group_limits=None, channel_weight=5., max_move_mm=2.,
                    passes=3, clearance=.2, grid_mm=.25):
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
            occ = np.zeros((int(math.ceil(height/g)), int(math.ceil(width/g))), dtype=bool)
            for keepout in keepouts:
                _mark(occ, g, keepout)
            sides = set(occupied_sides(comp))
            for other in others:
                for side, cr in placement_rects(other):
                    if side in sides:
                        _mark(occ, g, Rect(cr.cx, cr.cy, cr.w+clearance, cr.h+clearance))
            cr = courtyard_rect(comp)
            bw, bh = (int(math.ceil((size+clearance)/g)) for size in (cr.w, cr.h))
            target = targets[comp.ref]
            try:
                row, col = _place_part(occ, g, bw, bh, target,
                    list(limits.get(comp.ref, ())) + [(*target, max_move_mm)],
                    candidate_cost=lambda xs, ys: channel_weight*channel_model.penalty(comp, others, xs, ys))
            except LegalizationError:
                continue  # Keep a valid current pose if no improved legal slot exists.
            candidate = ((col+bw/2)*g, (row+bh/2)*g)
            after = float(channel_model.penalty(comp, others, *candidate))
            cost_before = math.dist(comp.pos, target)**2 + channel_weight*before
            cost_after = math.dist(candidate, target)**2 + channel_weight*after
            if after < before-1e-9 and cost_after < cost_before-1e-9:
                comp.pos = candidate
                changed = True
        if not changed:
            break
    return placed
