"""Relative edge rows, explored as complete configurations across global starts.

A sampled row pose is a temporary search choice, never an authored absolute lock.
The ordinary global placer optimizes all other parts around it. Whole electrical
routing ranks the resulting placements; the sampler makes no routing claim.
"""

import copy
import math
import random

from pnr.constraints import Constraint, Enforcement
from pnr.graph import BoardGraph

from .geometry import (
    courtyard_rect,
    keepout_rects,
    outline_size,
    placement_rects,
    resolve_fixed_poses,
    resolve_hard_rotations,
)


def offsets(graph, row, rotation):
    """Centred row geometry derived from real courtyard widths plus stated gap."""
    gap = row.params["gap_mm"]
    positions = []
    cursor = 0.0
    for ref in row.refs:
        c = graph.component(ref)
        positions.append((ref, cursor + c.courtyard[0] / 2, 0.0))
        cursor += c.courtyard[0] + gap
    span = cursor - gap
    angle = math.radians(rotation)
    ct, st = math.cos(angle), math.sin(angle)
    return {
        ref: ((x - span / 2) * ct - y * st, (x - span / 2) * st + y * ct) for ref, x, y in positions
    }


def edges(row, rotation):
    q = int(round(rotation / 90)) % 4
    f = row.params.get("facing")
    if f == "south":
        return [("south", "east", "north", "west")[q]]
    if f == "north":
        return [("north", "west", "south", "east")[q]]
    return ["north", "south"] if q % 2 == 0 else ["west", "east"]


def bounds(graph, row, rotation):
    pts = offsets(graph, row, rotation)
    rs = []
    for ref, xy in pts.items():
        c = copy.copy(graph.component(ref))
        c.pos = xy
        c.rot = rotation
        rs.append(courtyard_rect(c))
    return (
        min(x.left for x in rs),
        min(x.bottom for x in rs),
        max(x.right for x in rs),
        max(x.top for x in rs),
    )


def pad_edge_along(graph, row, rotation, edge, lo, hi, pad_edge, width, height):
    """PNR_PAD_EDGE_CLEARANCE=1: narrow the along-edge interval [lo, hi] of the row
    centre so every member's pads and drills keep the fab edge rules from the two
    perpendicular edges (a row end at a corner). The facing edge stays flush, as
    the row relation requires."""
    from .legalize import pad_edge_box

    for ref, (dx, dy) in offsets(graph, row, rotation).items():
        c = copy.copy(graph.component(ref))
        c.pos = (dx, dy)
        c.rot = float(rotation)
        x_lo, x_hi, y_lo, y_hi = pad_edge_box(c, pad_edge, width, height)
        if edge in ("north", "south"):
            lo, hi = max(lo, x_lo - dx), min(hi, x_hi - dx)
        else:
            lo, hi = max(lo, y_lo - dy), min(hi, y_hi - dy)
    return lo, hi


def violations(graph, constraints, tolerance=0.250001):
    """Validate original source relationships after sampling, routing or moves."""
    width, height = outline_size(graph, constraints)
    bad = []
    for row in constraints.constraints:
        if row.kind != "row":
            continue
        parts = [graph.component(ref) for ref in row.refs]
        rotation = parts[0].rot
        if abs(rotation / 90 - round(rotation / 90)) > 1e-7 or any(
            abs((c.rot - rotation + 180) % 360 - 180) > 1e-7 or c.side != parts[0].side
            for c in parts
        ):
            bad.extend(row.refs)
            continue
        off = offsets(graph, row, rotation)
        center = (parts[0].pos[0] - off[parts[0].ref][0], parts[0].pos[1] - off[parts[0].ref][1])
        if any(
            math.dist(c.pos, (center[0] + off[c.ref][0], center[1] + off[c.ref][1])) > 1e-5
            for c in parts
        ):
            bad.extend(row.refs)
            continue
        left, bottom, right, top = bounds(graph, row, rotation)
        left += center[0]
        right += center[0]
        bottom += center[1]
        top += center[1]
        distances = {
            "west": abs(left),
            "east": abs(width - right),
            "south": abs(bottom),
            "north": abs(height - top),
        }
        if (
            min(left, bottom, width - right, height - top) < -tolerance
            or min(distances[e] for e in edges(row, rotation)) > tolerance
        ):
            bad.extend(row.refs)
    return sorted(set(bad))


def sample_constraints(graph, constraints, seed, attempts=96, pad_edge=None):
    """Sample a collision-free joint edge configuration; deterministic by seed.

    Covers every cardinal edge across consecutive seeds. Within an edge it samples
    the legal translation interval; different seeds supply different combinations.
    No design source is mutated and no exact source fixed pose is released here.
    ``pad_edge`` ((copper, hole) mm, PNR_PAD_EDGE_CLEARANCE=1; None = off) keeps
    row-end pads and drills off the perpendicular edges (:func:`pad_edge_along`).
    """
    from .legalize import LegalizationError

    rows = [c for c in constraints.constraints if c.kind == "row"]
    if not rows:
        return constraints
    width, height = outline_size(graph, constraints)
    clearance = constraints.board.default_clearance_mm
    ordered = sorted(
        rows,
        key=lambda c: (
            -sum(
                graph.component(ref).courtyard[0] * graph.component(ref).courtyard[1]
                for ref in c.refs
            ),
            c.name or "",
        ),
    )
    rng = random.Random(seed)
    base_fixed = resolve_fixed_poses(graph, constraints)
    base_rot = resolve_hard_rotations(constraints)
    from pnr.constraints import ConstraintError

    from .regions import check_feasible, declared

    related = declared(constraints)
    if related:
        # A region or align no row choice can rescue is the author's to fix: name it.
        check_feasible(graph, constraints, width, height)
    for attempt in range(attempts):
        work = BoardGraph.from_json(graph.to_json())
        cc = copy.deepcopy(constraints)
        placed = set(base_fixed)
        choices = []
        ok = True
        for ref, pos in base_fixed.items():
            work.component(ref).pos = pos
            work.component(ref).rot = base_rot.get(ref, 0.0)
        for ri, row in enumerate(ordered):
            rotation = 90 * ((seed + ri + attempt) % 4)
            if any(ref in base_rot and base_rot[ref] != rotation for ref in row.refs):
                ok = False
                break
            edge = rng.choice(edges(row, rotation))
            left, bottom, right, top = bounds(work, row, rotation)
            if right - left > width or top - bottom > height:
                ok = False
                break
            lo, hi = (
                (-left, width - right) if edge in ("north", "south") else (-bottom, height - top)
            )
            if pad_edge is not None:
                lo, hi = pad_edge_along(work, row, rotation, edge, lo, hi, pad_edge, width, height)
                if lo > hi + 1e-9:
                    ok = False
                    break
            along = lo + (hi - lo) * rng.random()
            center = (
                (along, -bottom if edge == "south" else height - top)
                if edge in ("north", "south")
                else (-left if edge == "west" else width - right, along)
            )
            off = offsets(work, row, rotation)
            new = []
            for ref, (dx, dy) in off.items():
                c = work.component(ref)
                c.pos = (center[0] + dx, center[1] + dy)
                c.rot = float(rotation)
                new.append(c)
            # Both sides and exact opposing thermal-hole reservations participate.
            previous = [work.component(ref) for ref in placed]
            if any(
                sa == sb and ra.overlaps(rb, gap=clearance)
                for c in new
                for other in previous
                for sa, ra in placement_rects(c)
                for sb, rb in placement_rects(other)
            ):
                ok = False
                break
            for c in new:
                cc.constraints.append(
                    Constraint(
                        "fixed",
                        Enforcement.HARD,
                        (c.ref,),
                        dict(
                            at=list(c.pos),
                            rot=c.rot,
                            side=c.side,
                            row_trial=dict(
                                name=row.name,
                                members=list(row.refs),
                                edge=edge,
                                rotation=rotation,
                                seed=seed,
                                attempt=attempt,
                                gap_mm=row.params["gap_mm"],
                                reason=row.params["reason"],
                            ),
                            reason="Temporary joint row search choice; no authored XY lock",
                        ),
                    )
                )
            placed.update(row.refs)
            choices.append(
                dict(
                    name=row.name,
                    edge=edge,
                    rotation=rotation,
                    center=center,
                    members=list(row.refs),
                )
            )
        if not ok:
            continue
        keepouts = keepout_rects(work, cc, resolve_fixed_poses(work, cc))
        if any(courtyard_rect(work.component(ref)).overlaps(k) for ref in placed for k in keepouts):
            continue
        if violations(work, cc):
            continue
        from .regions import violations as related_violations

        # A region or align among the parts placed so far (fixed poses, rows).
        if related_violations([work.component(ref) for ref in placed], cc):
            continue
        if related:
            # A trial that leaves an aligned or confined part nowhere to go.
            try:
                check_feasible(work, cc, width, height, trials_fixed=True)
            except ConstraintError:
                continue
        for row in cc.constraints:
            if row.kind == "row":
                row.params["trial_resolved"] = True
        from pnr.live import emit

        emit(
            "placement_row_choices",
            data=dict(
                seed=seed,
                attempt=attempt,
                choices=choices,
                scope="Provisional relative-row search choices; source contains no absolute origins",
            ),
        )
        return cc
    raise LegalizationError("No collision-free joint edge-row sample within bounded search")
