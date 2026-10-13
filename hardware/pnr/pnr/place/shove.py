"""Deterministic transactional legalization repair (PNR_LEGALIZE_SHOVE=1).

Revisit the legal packer's decisions against the original global targets. A
proposal may translate a cascade of neighbours; no partial cascade is committed.
The complete placement and every affected hard relation are checked before an
aggregate improvement is accepted. This is placement, not copper push/shove.
"""

from __future__ import annotations

import json
import math
import os
from functools import lru_cache

from .geometry import Rect, courtyard_rect, placement_rects, resolve_fixed_poses
from .metrics import hard_violations, pose_checker

# Deterministic work limits, independent of machine speed.
PASSES = 3
BEAM = 12
DEPTH = 8
MAX_NEIGHBOR_MOVE_MM = 4.0


def enabled():
    return os.environ.get("PNR_LEGALIZE_SHOVE", "1") == "1"


def _memoized_channels(model, comps):
    """Cache scalar pair geometry locally, retaining the model's summation order.

    A cascade changes only poses; pads, nets and fabrication rules are fixed for
    this repair. Keep caches bounded and private to it. Copy the built-in channel
    adapters so callers and other searches never see a replaced method; unknown
    adapters retain their original behavior.
    """
    from .channels import ChannelModel
    from .power_first import PourChannels

    if type(model) not in (ChannelModel, PourChannels):
        return model
    cached = object.__new__(type(model))
    cached.__dict__ = model.__dict__.copy()
    if type(model) is PourChannels:
        cached.base = _memoized_channels(model.base, comps)

        def pour_penalty(comp, others, xs, ys):
            if os.environ.get("PNR_POWER_CHANNELS", "1") == "1":
                return cached.base.penalty(comp, others, xs, ys)
            return PourChannels.penalty(cached, comp, others, xs, ys)

        cached.penalty = pour_penalty
        return cached

    @lru_cache(maxsize=16384)
    def interactions(a, axy, arot, aside, b, bxy, brot, bside, x, y):
        # Pose arguments are the immutable cache key; the live components have
        # precisely those poses when the cache is populated.
        return tuple(model.interactions(comps[a], comps[b], x, y))

    def key(comp, other, xs=None, ys=None):
        x, y = comp.pos if xs is None else (xs, ys)
        return (
            comp.ref,
            tuple(comp.pos),
            comp.rot,
            comp.side,
            other.ref,
            tuple(other.pos),
            other.rot,
            other.side,
            x,
            y,
        )

    def at(comp, other, xs=None, ys=None):
        return interactions(*key(comp, other, xs, ys))

    @lru_cache(maxsize=16384)
    def contributions(*pose):
        import numpy as np

        from .geometry import occupied_sides

        a, b = comps[pose[0]], comps[pose[4]]
        if not set(occupied_sides(a)) & set(occupied_sides(b)):
            return ()
        sa, sb = model.shape(a), model.shape(b)
        if sa is None or sb is None:
            return ()
        x, y = pose[-2:]
        ox, oy = b.pos
        xover = min(x + sa[1], ox + sb[1]) - max(x + sa[0], ox + sb[0])
        yover = min(y + sa[3], oy + sb[3]) - max(y + sa[2], oy + sb[2])
        # No facing projection means every original directional contribution
        # is zero, independent of demand. Avoid computing distant rows' nets.
        if xover <= 0 and yover <= 0:
            return ()

        # Keep each directional contribution separate: combining a pair first
        # changes floating-point summation order and can change search decisions.
        return tuple(
            float(np.where((gap >= 0) & (overlap > 0), np.maximum(required - gap, 0) ** 2, 0))
            for _, gap, overlap, required, _ in interactions(*pose)
        )

    def penalty(comp, others, xs, ys):
        # Shove evaluates scalar poses only. Replay the original directional
        # additions in the same order, avoiding thousands of zero-dimensional
        # NumPy allocations for unchanged pairs on larger boards.
        score = 0.0
        for other in others:
            for value in contributions(*key(comp, other, xs, ys)):
                score += value
        return score

    cached.interactions = at
    cached.penalty = penalty
    return cached


def repair(
    graph,
    targets,
    constraints,
    *,
    grid=0.25,
    clearance=0.2,
    spread=1.0,
    inflation=None,
    margins=None,
    pad_edge=None,
    locality=None,
    channels=None,
    channel_weight=25.0,
    allow_rotation=True,
    banks=(),
):
    """Mutate a legal graph, returning immutable accepted transaction records.

    Neighbour shoves preserve rotation and side. An incoming part can recover its
    original rotation (when allowed). Banks are explicit authored member lists;
    their already-legal internal arrangement translates as a unit.
    """
    comps = {c.ref: c for c in graph.components}
    target = {c.ref: c for c in targets.components}
    channels = _memoized_channels(channels, comps)
    fixed = set(resolve_fixed_poses(graph, constraints))
    held = fixed | {
        c.ref
        for c in graph.components
        if c.locked
        or c.reserves
        or str(c.footprint).startswith(("block:", "line:"))
        or getattr(c, "hull", None)
    }
    rotations = set()
    for con in constraints.constraints:
        if con.kind in ("row", "line_group", "align"):
            held.update(con.refs)
        if con.kind == "orientation" and con.enforcement.name == "HARD":
            rotations.update(con.refs)
    units = {r: (r,) for r in comps}
    for bank in banks:
        members = tuple(sorted(bank))
        if not members or any(r not in comps for r in members):
            raise ValueError("Shove bank contains an unknown component")
        if any(len(units[r]) > 1 for r in members):
            raise ValueError("Shove banks must not overlap")
        for r in members:
            units[r] = members
        if held.intersection(members):
            held.update(members)
    legal = pose_checker(
        graph,
        constraints,
        clearance=clearance,
        spread=spread,
        inflation=inflation,
        margins=margins,
        pad_edge=pad_edge,
    )
    inflation, margins = inflation or {}, margins or {}
    from .sides import STACK_PLANE, stack_refs

    stacked = stack_refs(graph, constraints)

    def regions(c):
        factor = max(1.0, spread, inflation.get(c.ref, 1.0))
        margin = margins.get(c.ref, 0.0)
        rects = list(placement_rects(c))
        if c.ref in stacked:
            rects.append((STACK_PLANE, courtyard_rect(c)))
        return [
            (s, Rect(r.cx, r.cy, r.w * factor + 2 * margin, r.h * factor + 2 * margin))
            for s, r in rects
        ]

    @lru_cache(maxsize=4096)
    def cached_regions(ref, position, rotation):
        # These are the only component attributes modified by this search.
        return tuple(regions(comps[ref]))

    def poses():
        return {r: (tuple(c.pos), c.rot) for r, c in comps.items()}

    def restore(state):
        for r, (xy, rot) in state.items():
            comps[r].pos, comps[r].rot = xy, rot

    def terms():
        from pnr import placement_banks

        from .regions import soft_total

        displacement = sum(
            math.dist(c.pos, target[r].pos) ** 2 for r, c in comps.items() if r not in held
        )
        rotation = sum(
            (min((c.rot - target[r].rot) % 360, (target[r].rot - c.rot) % 360) / 90)
            * max(c.courtyard) ** 2
            for r, c in comps.items()
            if r not in held
        )
        local = channel = 0.0
        for r, c in comps.items():
            others = [o for k, o in comps.items() if k != r]
            if locality is not None:
                local += float(locality(c, others, *c.pos))
            if channels is not None:
                channel += channel_weight * float(channels.penalty(c, others, *c.pos))
        authored = soft_total(list(comps.values()), constraints)
        for con in constraints.constraints:
            if con.kind != "group" or con.enforcement.name != "SOFT":
                continue
            if con.params.get("placement_bank") and not placement_banks.enabled():
                continue
            anchor = con.params.get("anchor")
            if anchor not in comps:
                continue
            for ref in con.refs:
                if ref in comps:
                    authored += (con.weight or 1.0) * max(
                        0.0,
                        math.dist(comps[ref].pos, comps[anchor].pos)
                        - float(con.params.get("radius_mm") or 5.0),
                    ) ** 2
        return dict(
            target_displacement=displacement,
            rotation_change=rotation,
            pad_locality=local,
            channel_shortage=channel,
            source_constraints=authored,
        )

    def distance_cost(state):
        return sum(math.dist(xy, target[r].pos) ** 2 for r, (xy, _) in state.items())

    def collision(moved):
        geometry = {r: cached_regions(r, tuple(c.pos), c.rot) for r, c in comps.items()}
        for r in sorted(moved):
            for other in sorted(comps):
                if other == r or (other in moved and other < r):
                    continue
                for s, a in geometry[r]:
                    for t, b in geometry[other]:
                        if s == t and a.overlaps(b, gap=clearance):
                            return r, other, a, b
        return None

    def cascade(base, root, position, angle):
        restore(base)
        # A child may repair its own bank's internal packing. An incoming part
        # outside the bank must shove that bank collectively. Freezing the first
        # packer's scattered result would preserve exactly the defect we repair.
        local_units = dict(units)
        for r in units[root]:
            local_units[r] = (r,)
        dx, dy = position[0] - comps[root].pos[0], position[1] - comps[root].pos[1]
        protected = {root}
        for r in protected:
            c = comps[r]
            c.pos = (c.pos[0] + dx, c.pos[1] + dy)
        comps[root].rot = angle
        frontier = [(poses(), protected)]
        visited = set()
        for _ in range(DEPTH + 1):
            children = []
            for state, moved in frontier:
                restore(state)
                hit = collision(moved)
                if hit is None:
                    changed = [comps[r] for r in sorted(moved)]
                    if legal(changed) and not any(
                        hard_violations(graph, constraints, clearance).values()
                    ):
                        yield poses(), terms()
                    continue
                a_ref, b_ref, a, b = hit
                for ref, me, obstacle in ((b_ref, b, a), (a_ref, a, b)):
                    group = set(local_units[ref])
                    if group & (held | protected) or local_units[a_ref] == local_units[b_ref]:
                        continue
                    shifts = (
                        (obstacle.left - clearance - me.right, 0),
                        (obstacle.right + clearance - me.left, 0),
                        (0, obstacle.bottom - clearance - me.top),
                        (0, obstacle.top + clearance - me.bottom),
                    )
                    for x, y in shifts:
                        # Round away from the obstacle to avoid floating overlap.
                        x = math.copysign(math.ceil(abs(x) / grid - 1e-9) * grid, x)
                        y = math.copysign(math.ceil(abs(y) / grid - 1e-9) * grid, y)
                        trial = dict(state)
                        for r in group:
                            xy, rot = state[r]
                            trial[r] = ((xy[0] + x, xy[1] + y), rot)
                        if any(
                            math.dist(trial[r][0], base[r][0]) > MAX_NEIGHBOR_MOVE_MM for r in group
                        ):
                            continue
                        key = tuple((r, trial[r]) for r in sorted(moved | group))
                        if key in visited:
                            continue
                        visited.add(key)
                        children.append((trial, moved | group))
            frontier = sorted(children, key=lambda s: distance_cost(s[0]))[:BEAM]
            if not frontier:
                break
        restore(base)

    records = []
    for _ in range(PASSES):
        progress = False
        order = sorted(comps, key=lambda r: (-math.dist(comps[r].pos, target[r].pos), r))
        for ref in order:
            if ref in held:
                continue
            base = poses()
            before = terms()
            best, best_terms = base, before
            t = target[ref]
            # Include intermediate sites so a blocked target can still improve.
            sites = []
            for fraction in (1.0, 0.75, 0.5, 0.25):
                sites.append(
                    tuple(
                        round((base[ref][0][i] + fraction * (t.pos[i] - base[ref][0][i])) / grid)
                        * grid
                        for i in (0, 1)
                    )
                )
            turns = [base[ref][1]]
            if allow_rotation and ref not in rotations and len(units[ref]) == 1:
                turns = list(dict.fromkeys([t.rot, *turns]))
            for pos in dict.fromkeys(sites):
                for angle in turns:
                    for state, cost in cascade(base, ref, pos, angle):
                        if sum(cost.values()) < sum(best_terms.values()) - 1e-7:
                            best, best_terms = state, cost
            restore(base)
            if best is base:
                continue
            before_graph = json.loads(graph.to_json())
            restore(best)
            changed = [r for r in comps if best[r] != base[r]]
            # A second full check immediately before committing; no partial moves.
            if any(hard_violations(graph, constraints, clearance).values()):
                restore(base)
                continue
            legal.update(changed)
            records.append(
                dict(
                    ref=ref,
                    graph=before_graph,
                    target=list(t.pos),
                    original_position=list(base[ref][0]),
                    original_rotation=base[ref][1],
                    position=list(best[ref][0]),
                    rotation=best[ref][1],
                    before_terms=before,
                    after_terms=best_terms,
                    moves=[
                        dict(
                            ref=r,
                            before=list(base[r][0]),
                            before_rotation=base[r][1],
                            after=list(best[r][0]),
                            rotation=best[r][1],
                        )
                        for r in sorted(changed)
                    ],
                )
            )
            progress = True
        if not progress:
            break
    return records
