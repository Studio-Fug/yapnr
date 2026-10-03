"""Collapse blocks into rigid macro components for top-level placement, and expand back.

A macro is an ordinary :class:`Component` whose courtyard is the block outline
and whose pads are the member pads in the block frame, so the unmodified global
placer and legalizer can place it. Internal nets become intra-component and
exert no pull; external nets pull the macro toward its neighbours.

PNR_PAIR_LANDING_RESERVE (src13): a member's pair_landing reserves move onto its
macro (pads '<ref>.<pad>', side relative to the macro), so macro placement keeps
them clear; they are only present when the source graph carries them. With the
flag set the macro also records the sides its members are mounted on
(pair_landing.macro_mount), so another part's landing treats it like those parts.
The reserves are pad-name recipes, so they follow a shrunk/hull macro's re-centred
pads unchanged (src15 merge).

N-0001 (flags default off): with PNR_MACRO_SHRINK=1 the courtyard is the block's
used extent (members, pads and routed copper plus the margin) and the macro frame
is centred there; with PNR_MACRO_HULL=1 the macro also carries a per-side
occupancy hull (``Component.hull``, :mod:`pnr.place.hull`) so parts and other
blocks can nest into its free space. :meth:`MacroPlan.expand` stays rigid in
both cases (members are posed from the recorded frame origin).
"""

from __future__ import annotations

import copy
import math
import os
from typing import Dict, List, Optional, Tuple

from pnr.constraints import CompiledConstraints, Constraint, Enforcement
from pnr.graph import BoardGraph, Component, Net, Pad


def _rot(x, y, deg):
    t = math.radians(deg)
    c, s = math.cos(t), math.sin(t)
    return x * c - y * s, x * s + y * c


def _rot_size(size, deg):
    return (size[1], size[0]) if int(round(deg / 90)) % 2 else size


def _member_offset(macro, x, y, deg):
    """A member's offset from its macro's centre, for a member at (x, y) in the block
    frame and the macro turned by ``deg``."""
    if "origin" in macro:
        return _rot(x - macro["origin"][0], y - macro["origin"][1], deg)
    return _rot(x - macro["width"] / 2, y - macro["height"] / 2, deg)


def macro_pair_weights(pair_weights, plan):
    """Flat pad-pair weights {(ref_a, pad_a, ref_b, pad_b): w} on the macro graph.

    A block member's pad is the macro pad ``'<ref>.<pad>'`` of its macro; pairs
    inside one macro are rigid there and dropped; duplicates add up."""
    out = {}
    for (ra, pa, rb, pb), w in (pair_weights or {}).items():
        ma, mb = plan.member_of.get(ra), plan.member_of.get(rb)
        if ma is not None and ma == mb:
            continue
        a = (ma, "%s.%s" % (ra, pa)) if ma else (ra, pa)
        b = (mb, "%s.%s" % (rb, pb)) if mb else (rb, pb)
        out[(a[0], a[1], b[0], b[1])] = out.get((a[0], a[1], b[0], b[1]), 0.0) + float(w)
    return out or None


def shrink_enabled() -> bool:
    """PNR_MACRO_SHRINK=1: a macro is its used extent (N-0001), not its sub-board."""
    return os.environ.get("PNR_MACRO_SHRINK") == "1"


def hull_enabled() -> bool:
    """PNR_MACRO_HULL=1: a macro carries a per-side occupancy hull (N-0001)."""
    return os.environ.get("PNR_MACRO_HULL") == "1"


class MacroPlan:
    """Mapping between the flat graph and its macro-collapsed counterpart."""

    def __init__(self):
        self.macros: Dict[str, dict] = (
            {}
        )  # macro ref -> {block, width, height, members:{ref:(x,y,rot,side)}}
        self.member_of: Dict[str, str] = {}

    def expand(self, placed: BoardGraph, flat: BoardGraph) -> BoardGraph:
        """Return ``flat`` with members posed from their placed macros."""
        out = BoardGraph.from_json(flat.to_json())
        by_ref = {c.ref: c for c in placed.components}
        for comp in out.components:
            if comp.ref in self.member_of:
                m = self.macros[self.member_of[comp.ref]]
                mc = by_ref[self.member_of[comp.ref]]
                x, y, rot, side = m["members"][comp.ref]
                if "origin" in m and mc.side != "top":
                    # Shrunk/hull macro (N-0001): the frame centre is the used-extent centre.
                    raise ValueError(
                        f"macro {self.member_of[comp.ref]} placed on {mc.side}: "
                        f"mirrored block macros are not supported"
                    )
                dx, dy = _member_offset(m, x, y, mc.rot)
                comp.pos = (mc.pos[0] + dx, mc.pos[1] + dy)
                comp.rot = float((rot + mc.rot) % 360)
                comp.side = side
            else:
                src = by_ref[comp.ref]
                comp.pos, comp.rot, comp.side = src.pos, src.rot, src.side
                comp.pads = copy.deepcopy(src.pads)  # keep any side mirroring the placer applied
        out.outline = copy.deepcopy(placed.outline)
        return out

    def trace_rows(self, rows):
        """Recording only (:func:`pnr.trace.pose_expansion`): expand trace pose rows.

        ``rows`` are ``[[ref, x_um, y_um, rot, side]]``. Each macro row becomes its
        members' rows, posed as :meth:`expand` poses them (macro pose plus the rotated
        member offset, member rotation plus the macro rotation), in member order.
        Returns ``(rows, groups, members)``: the expanded rows, the macros' own rows
        and ``{macro ref: [member refs]}`` of the macros present."""
        from pnr.trace import angle as _angle
        from pnr.trace import um as _um

        out, groups, members = [], [], {}
        for row in rows:
            m = self.macros.get(row[0])
            if m is None:
                out.append(list(row))
                continue
            groups.append(list(row))
            members[row[0]] = list(m["members"])
            cx, cy, turn = row[1] / 1000.0, row[2] / 1000.0, float(row[3])
            for ref, (x, y, rot, side) in m["members"].items():
                dx, dy = _member_offset(m, x, y, turn)
                out.append([ref, _um(cx + dx), _um(cy + dy), _angle(rot + turn), side])
        return out, groups, members


def _macro_frame(geo, w, h, margin, shrink):
    """Frame of a shaped macro: {origin (block frame), courtyard, shape, reason, geometry}.

    With shrink and a measured geometry the courtyard is the used extent; else it
    is the full rectangle plus margin, centred on the rectangle. The hull (if any)
    is always built on the chosen courtyard."""
    full = dict(origin=(w / 2, h / 2), courtyard=(w + 2 * margin, h + 2 * margin), shape="rect")
    if geo is None:
        return dict(full, reason="no geometry for this block", geometry=None)
    if not geo.ok:
        return dict(full, reason=geo.reason, geometry=None)
    if not shrink:
        return dict(full, reason="", geometry=geo)
    x0, y0, x1, y1 = geo.extent
    return dict(
        origin=((x0 + x1) / 2, (y0 + y1) / 2),
        courtyard=(x1 - x0, y1 - y0),
        shape="used",
        reason="",
        geometry=geo,
    )


def collapse(
    flat: BoardGraph,
    constraints: CompiledConstraints,
    rules: dict,
    layouts: List[Tuple[object, BoardGraph, float, float]],
    geometry: Optional[dict] = None,
    prefix: str = "MB",
    margin: Optional[float] = None,
):
    """Build the macro graph.

    ``layouts`` holds (block, placed_sub_graph, width, height) for every block to
    collapse; the sub-graph poses are in the block frame (origin bottom-left).
    Returns (macro_graph, macro_constraints, placement_rules, plan).

    ``geometry`` ({block name: pnr.hier.extent.BlockGeometry}, N-0001) is read only
    with PNR_MACRO_SHRINK=1 (the macro courtyard becomes the used extent, its frame
    centred there) or PNR_MACRO_HULL=1 (the macro carries a per-side occupancy
    hull). A block without a measured geometry keeps the full rectangle; the plan
    records why.

    ``prefix`` names the macros (``<prefix>00``, ...); ``margin`` (mm) is the
    courtyard margin around each layout rectangle, None for the fab edge clearance
    (routed block copper may reach the rectangle). Line groups
    (:mod:`pnr.place.line_group`) collapse with ``prefix="LG", margin=0.0``.

    Raises ``ValueError`` when a relation cannot be expressed on the rigid macro:
    a HARD group, a multi-ref orientation or a ref-relative keepout that mixes
    members of one macro with parts outside it (straddling), or members of one
    macro whose orientation locks imply different macro rotations.
    """
    plan = MacroPlan()
    by_ref = {c.ref: c for c in flat.components}
    macro_graph = BoardGraph(
        name=flat.name + ":macro",
        outline=copy.deepcopy(flat.outline),
        stack=copy.deepcopy(flat.stack),
    )
    pad_alias: Dict[Tuple[str, str], Tuple[str, str]] = {}
    # Orientation is a per-ref absolute axis: every ref of a lock counts, not just refs[0].
    orient_locks = {
        r: c for c in constraints.constraints if c.kind == "orientation" for r in c.refs
    }
    macro_orientation = {}
    # Routed block copper may reach the block rectangle (sub-board apron equals the
    # edge clearance), so the macro reserves that clearance on every side: it then
    # also holds from the board edge, not only from neighbouring courtyards.
    if margin is None:
        margin = float(rules.get("fab", {}).get("edge_clearance_mm", 0.2))
    margin = float(margin)
    from pnr.place.pair_landing import enabled as landing_enabled
    from pnr.place.pair_landing import (
        macro_mount_record,
        macro_reserves,
    )

    shrink, with_hull = shrink_enabled(), hull_enabled()
    shaped = geometry is not None and (shrink or with_hull)
    for index, (block, sub, w, h) in enumerate(layouts):
        mref = "%s%02d" % (prefix, index)
        members = {}
        pads: List[Pad] = []
        reserves: List[dict] = []
        frame = None
        if shaped:
            frame = _macro_frame(geometry.get(block.name), w, h, margin, shrink)
            fx, fy = frame["origin"]
        for c in sub.components:
            # PNR_PAIR_LANDING_RESERVE: a member's diff-pair landing reserve moves
            # onto the macro (pads '<ref>.<pad>', side relative to the macro).
            reserves.extend(macro_reserves(c))
            members[c.ref] = (c.pos[0], c.pos[1], c.rot, c.side)
            plan.member_of[c.ref] = mref
            for p in c.pads:
                ox, oy = _rot(p.offset[0], p.offset[1], c.rot)
                q = copy.deepcopy(p)
                q.name = f"{c.ref}.{p.name}"
                if frame is not None:
                    q.offset = (c.pos[0] - fx + ox, c.pos[1] - fy + oy)
                else:
                    q.offset = (c.pos[0] - w / 2 + ox, c.pos[1] - h / 2 + oy)
                q.size = _rot_size(p.size, c.rot)
                q.drill_size = _rot_size(p.drill_size, c.rot)
                pads.append(q)
                pad_alias[(c.ref, p.name)] = (mref, q.name)
            if c.ref in orient_locks:
                # The member's absolute axis is authored; the macro may not rotate it.
                lock = orient_locks[c.ref]
                rot = (float(lock.params.get("rot") or 0) - c.rot) % 360
                macro_orientation.setdefault(mref, []).append((rot, lock.enforcement, c.ref))
        if landing_enabled():
            # PNR_PAIR_LANDING_RESERVE: the macro counts as mounted where its
            # members are (a landing excludes bodies mounted on its side).
            reserves.append(macro_mount_record(sub.components))
        plan.macros[mref] = dict(block=block.name, width=w, height=h, members=members)
        if frame is not None:
            cw, ch = frame["courtyard"]
            hull = None
            if with_hull and frame["geometry"] is not None:
                from pnr.hier.extent import build_hull

                try:
                    hull = build_hull(
                        frame["geometry"],
                        frame["origin"],
                        (cw, ch),
                        float(constraints.board.default_clearance_mm),
                    )
                except ValueError as error:  # the macro keeps its solid courtyard
                    frame["reason"] = "no hull: %s" % error
            plan.macros[mref].update(
                origin=frame["origin"],
                courtyard=(cw, ch),
                shape=frame["shape"],
                reason=frame["reason"],
                hull=None if hull is None else hull["stats"],
            )
            macro_graph.components.append(
                Component(
                    ref=mref,
                    footprint="block:" + block.name,
                    pos=(0.0, 0.0),
                    rot=0.0,
                    side="top",
                    courtyard=(cw, ch),
                    bbox=(cw, ch),
                    pads=pads,
                    address="block:" + block.name,
                    reserves=reserves,
                    hull=hull,
                )
            )
            continue
        macro_graph.components.append(
            Component(
                ref=mref,
                footprint="block:" + block.name,
                pos=(0.0, 0.0),
                rot=0.0,
                side="top",
                courtyard=(w + 2 * margin, h + 2 * margin),
                bbox=(w + 2 * margin, h + 2 * margin),
                pads=pads,
                address="block:" + block.name,
                reserves=reserves,
            )
        )
    for c in flat.components:
        if c.ref not in plan.member_of:
            macro_graph.components.append(copy.deepcopy(c))
    for n in flat.nets:
        pins = [pad_alias.get(tuple(p), tuple(p)) for p in n.pins]
        macro_graph.nets.append(Net(name=n.name, code=n.code, pins=pins))

    def straddled(refs):
        """First macro whose members share ``refs`` with parts outside it, else None."""
        for m in dict.fromkeys(plan.member_of[r] for r in refs if r in plan.member_of):
            if any(plan.member_of.get(r) != m for r in refs):
                return m
        return None

    con = copy.deepcopy(constraints)
    kept = []
    for c in con.constraints:
        # Relations the rigid macro cannot carry: a hard radius is measured from the
        # member, not the macro centre; orientation of outside refs must not move
        # with (or be dropped along with) a member; a keepout is relative to the
        # member's own courtyard edge. Refuse instead of emitting a wrong constraint.
        checked = (
            list(c.refs) + [c.params.get("anchor")]
            if c.kind == "group" and c.enforcement == Enforcement.HARD
            else (
                list(c.refs)
                if c.kind == "orientation" and len(c.refs) > 1
                else (
                    list(c.refs)
                    if c.kind == "keepout"
                    and c.params.get("extent")
                    and not c.params.get("polygon")
                    else []
                )
            )
        )
        m = straddled([r for r in checked if r])
        if m:
            inside = sorted({r for r in checked if r and plan.member_of.get(r) == m})
            outside = sorted({r for r in checked if r and plan.member_of.get(r) != m})
            label = ("hard group" if c.kind == "group" else c.kind) + (
                " %r" % c.name if c.name else ""
            )
            raise ValueError(
                f'{label} constraint straddles macro {m} (block {plan.macros[m]["block"]}): '
                f"members {inside} vs outside {outside}; the block extraction must keep "
                f"these parts together or leave the block flat"
            )
        refs = tuple(dict.fromkeys(plan.member_of.get(r, r) for r in c.refs))
        if c.kind == "orientation" and c.refs[0] in plan.member_of:
            continue
        if c.kind == "side" and all(r in plan.member_of for r in c.refs):
            continue
        params = dict(c.params)
        anchor = params.get("anchor")
        if anchor:
            params["anchor"] = plan.member_of.get(anchor, anchor)
        if c.kind == "group":
            others = [r for r in refs if r != params.get("anchor")]
            if not others:
                continue
        kept.append(Constraint(c.kind, c.enforcement, refs, params, c.weight, c.name))
    for mref, locks in macro_orientation.items():
        rot, enforcement, _ = locks[0]
        if any(abs((rot - other + 180) % 360 - 180) > 1e-6 for other, _, _ in locks):
            raise ValueError(
                f'macro {mref} ({plan.macros[mref]["block"]}): orientation locks of its members '
                f"need macro rotations {sorted((ref, r) for r, _, ref in locks)}; the layout "
                f"does not honour the authored axes"
            )
        kept.append(
            Constraint(
                "orientation",
                enforcement,
                (mref,),
                {"rot": rot, "reason": "member axis convention"},
            )
        )
    con.constraints = kept
    con.copper_keepouts = [k for k in con.copper_keepouts if k.get("ref") not in plan.member_of]

    place_rules = copy.deepcopy(rules)
    place_rules["plane_access_intents"] = [
        i for i in rules.get("plane_access_intents", []) if i.get("ref") not in plan.member_of
    ]
    return macro_graph, con, place_rules, plan
