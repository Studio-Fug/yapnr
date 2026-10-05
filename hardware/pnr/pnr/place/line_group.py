"""Line groups: ordered parts held in one rigid line (the ``line_group`` constraint).

A line group is placed as one rigid body. :func:`collapse` turns every group into a
macro component of :mod:`pnr.hier.macro` (courtyard = the group rectangle, pads = the
member pads in the group frame), so the unmodified global placer moves the whole line
and anneals its four-way rotation, and the unmodified legalizer packs and turns it as
one rectangle. :meth:`pnr.hier.macro.MacroPlan.expand` then poses the members rigidly.
:func:`pnr.place.placer.place` runs that sequence when a design declares a line group;
without one nothing here is imported.

The group frame: members in order along +x, centred on the group's axis, each turned
by the group's ``rot``; origin at the bottom-left of the group rectangle. The group
has one free position and one free cardinal rotation (a 180 degree turn reverses the
order on the board, which a human would accept too).
"""

from __future__ import annotations

import copy
import math
from types import SimpleNamespace

from pnr.constraints import DEFAULT_WEIGHTS, Constraint, Enforcement
from pnr.graph import BoardGraph
from pnr.stack import local_record

TOLERANCE_MM = 1e-5


def groups(constraints):
    """The ``line_group`` constraints of ``constraints``, in declaration order."""
    return [c for c in constraints.constraints if c.kind == "line_group"]


def _extent(comp, rot):
    """(along, across) courtyard extent of ``comp`` turned by ``rot`` in the group frame."""
    w, h = comp.courtyard
    return (h, w) if int(round(rot / 90.0)) % 2 else (w, h)


def layout(graph, con, clearance):
    """``(width, height, {ref: (x, y, rot)})``: the member poses in the group frame.

    With ``pitch_mm`` the centres are evenly pitched; with ``gap_mm`` neighbouring
    courtyards are that far apart. Raises ``ValueError`` when a pitch leaves two
    neighbours closer than ``clearance``.
    """
    rot = float(con.params.get("rot") or 0.0) % 360
    parts = [graph.component(ref) for ref in con.refs]
    extents = [_extent(c, rot) for c in parts]
    pitch, gap = con.params.get("pitch_mm"), con.params.get("gap_mm")
    centres = [0.0]
    for i in range(1, len(parts)):
        need = (extents[i - 1][0] + extents[i][0]) / 2.0
        if pitch is not None:
            if pitch < need + clearance - 1e-9:
                raise ValueError(
                    "line_group %r: pitch %.3f mm puts %s and %s closer than the %.3f mm "
                    "clearance (needs at least %.3f mm)"
                    % (con.name, pitch, con.refs[i - 1], con.refs[i], clearance, need + clearance)
                )
            centres.append(centres[-1] + float(pitch))
        else:
            centres.append(centres[-1] + need + float(gap))
    left = centres[0] - extents[0][0] / 2.0
    width = centres[-1] + extents[-1][0] / 2.0 - left
    height = max(e[1] for e in extents)
    return (
        width,
        height,
        {ref: (x - left, height / 2.0, rot) for ref, x in zip(con.refs, centres)},
    )


def _turned(x, y, deg):
    t = math.radians(deg)
    c, s = math.cos(t), math.sin(t)
    return x * c - y * s, x * s + y * c


def satellites(graph, constraints):
    """``PNR_LINE_SATELLITES``: ``{member ref: [(satellite ref, member pad, satellite pad)]}``.

    A satellite of a line member is a free two-pad part (no constraint names it, not locked,
    on the top side, on no diff-pair or length-match net) one of whose pads shares a two-pin
    net with one pad of that member: an LED's series resistor, a pull-up's resistor. A part
    that would be the satellite of two pads is nobody's. Empty on a double-sided board (a
    satellite there may want the other side)."""
    from .reorient import matched_refs
    from .sides import policy_of

    lines = groups(constraints)
    if not lines or policy_of(constraints) == "double":
        return {}
    members = {ref for con in lines for ref in con.refs}
    named = {ref for con in constraints.constraints for ref in con.refs}
    named |= set(constraints.locked_refs) | matched_refs(graph, constraints)
    pins = {n.name: list(n.pins) for n in graph.nets}
    found = {}
    for comp in graph.components:
        if (
            comp.ref in members
            or comp.ref in named
            or comp.locked
            or comp.side != "top"
            or len(comp.pads) != 2
            or not all(p.net for p in comp.pads)
        ):
            continue
        ties = []
        for pad in comp.pads:
            net = pins.get(pad.net) or []
            others = [(ref, name) for ref, name in net if ref != comp.ref]
            if len(net) == 2 and len(others) == 1 and others[0][0] in members:
                ties.append((others[0][0], others[0][1], pad.name))
        if len(ties) == 1:
            found.setdefault(ties[0][0], []).append((comp.ref,) + ties[0][1:])
    return found


def satellite_layout(graph, con, width, height, poses, found, clearance):
    """``(width, height, poses)``: :func:`layout`'s line with its members' satellites
    (:func:`satellites`) added, each flush beside its member across the line: turned so its
    pads lie across the line with the shared pad facing the member's, the shared pads in line,
    the courtyards ``clearance`` apart. A member pad that lies along the line (its offset not
    mostly across it) takes no satellite, nor does one that would come closer than
    ``clearance`` to an earlier satellite. The frame grows to hold them (origin bottom-left)."""
    boxes = {}
    for ref in con.refs:
        x, y, rot = poses[ref]
        along, across = _extent(graph.component(ref), rot)
        boxes[ref] = (x - along / 2.0, y - across / 2.0, x + along / 2.0, y + across / 2.0)
    placed = dict(poses)
    for ref in con.refs:
        member = graph.component(ref)
        mx, my, mrot = poses[ref]
        for sref, mpad, spad in found.get(ref, ()):
            pad = next(p for p in member.pads if p.name == mpad)
            px, py = _turned(pad.offset[0], pad.offset[1], mrot)
            if abs(py) <= abs(px) + 1e-9:
                continue  # the pad lies along the line: no room beside it
            side = 1.0 if py > 0 else -1.0
            sat = graph.component(sref)
            mine = next(p for p in sat.pads if p.name == spad)
            # The turn that points the shared pad at the member (across the line).
            srot = max(
                (0.0, 90.0, 180.0, 270.0),
                key=lambda a: -side * _turned(mine.offset[0], mine.offset[1], a)[1],
            )
            qx, _ = _turned(mine.offset[0], mine.offset[1], srot)
            along, across = _extent(sat, srot)
            mbox = boxes[ref]
            edge = mbox[3] if side > 0 else mbox[1]
            sx = mx + px - qx
            sy = edge + side * (clearance + across / 2.0)
            box = (sx - along / 2.0, sy - across / 2.0, sx + along / 2.0, sy + across / 2.0)
            if any(
                box[0] < b[2] + clearance - 1e-9
                and b[0] < box[2] + clearance - 1e-9
                and box[1] < b[3] + clearance - 1e-9
                and b[1] < box[3] + clearance - 1e-9
                for other, b in boxes.items()
                if other != ref
            ):
                continue
            boxes[sref] = box
            placed[sref] = (sx, sy, srot)
    left = min(b[0] for b in boxes.values())
    bottom = min(b[1] for b in boxes.values())
    width = max(width, max(b[2] for b in boxes.values()) - left)
    height = max(b[3] for b in boxes.values()) - bottom
    return (
        width,
        height,
        {ref: (x - left, y - bottom, rot) for ref, (x, y, rot) in placed.items()},
    )


def collapse(graph, constraints, rules):
    """``(macro graph, macro constraints, macro rules, plan)`` with every line group a macro.

    The macros are ``LG00``, ``LG01``, ... in declaration order, with no courtyard
    margin (the legalizer keeps the usual clearance around them) and the footprint
    ``line:<name>``: unlike an assembled ``block:`` macro a line carries no copper, so
    it occupies only its members' sides (:func:`pnr.place.geometry.occupied_sides`:
    top for SMD members, both sides with a drilled one) and an SMD line may sit above a
    bottom-side part. The ``line_group`` constraints are dropped from the macro
    constraints (the inner placement must not collapse again); a group with an
    ``edge`` gets a soft ``edge_align`` on its macro.

    Raises ``ValueError`` when a member is locked in place (a ``fixed`` constraint,
    including one :func:`pnr.place.initial_pool.preserve_source_locks` adds for a
    source-locked part): one member's lock would pin the whole line, and the parser
    rejects an authored one for that reason.
    """
    from pnr.hier.macro import collapse as collapse_macros

    clearance = float(constraints.board.default_clearance_mm)
    lines = groups(constraints)
    members = {ref for con in lines for ref in con.refs}
    locked = set(constraints.locked_refs) | {
        c.ref for c in graph.components if c.locked and c.ref in members
    }
    for con in lines:
        for ref in con.refs:
            if ref in locked:
                raise ValueError(
                    "line_group %r: member %s also has a fixed constraint (an authored pose "
                    "or a source lock), which a rigid line cannot honour" % (con.name, ref)
                )
    for intent in (rules or {}).get("plane_access_intents", []) or []:
        if intent.get("ref") in members:
            raise ValueError(
                "line_group member %s carries a plane-access intent; line groups do not "
                "support them" % intent.get("ref")
            )
    from pnr.legalize_flags import line_satellites

    # PNR_LINE_SATELLITES: each member's satellites ride in the rigid line.
    found = satellites(graph, constraints) if line_satellites() else {}
    layouts = []
    for con in lines:
        width, height, poses = layout(graph, con, clearance)
        if found:
            width, height, poses = satellite_layout(
                graph, con, width, height, poses, found, clearance
            )
        sub = BoardGraph(
            name=graph.name + ":line:" + con.name, stack=local_record(copy.deepcopy(graph.stack))
        )
        for ref in list(con.refs) + [r for r in poses if r not in con.refs]:
            comp = copy.deepcopy(graph.component(ref))
            if comp.side != "top":
                raise ValueError(
                    "line_group %r: member %s is not on the top side" % (con.name, ref)
                )
            x, y, rot = poses[ref]
            comp.pos, comp.rot = (x, y), rot
            sub.components.append(comp)
        layouts.append((SimpleNamespace(name="line_group:" + con.name), sub, width, height))
    mgraph, mcon, mrules, plan = collapse_macros(
        graph, constraints, rules or {}, layouts, prefix="LG", margin=0.0
    )
    for index, (con, (_, sub, _, _)) in enumerate(zip(lines, layouts)):
        macro = mgraph.component("LG%02d" % index)
        macro.footprint = macro.address = "line:" + con.name
        macro.smd_body = all(
            m.smd_body or not any(p.through_hole for p in m.pads) for m in sub.components
        )
    kept = [c for c in mcon.constraints if c.kind != "line_group"]
    for index, con in enumerate(lines):
        edge = con.params.get("edge") or "none"
        if edge != "none":
            kept.append(
                Constraint(
                    "edge_align",
                    Enforcement.SOFT,
                    ("LG%02d" % index,),
                    {"edge": edge, "side": None},
                    weight=DEFAULT_WEIGHTS["edge_align"],
                )
            )
    mcon.constraints = kept
    return mgraph, mcon, mrules, plan


def map_starts(plan, positions, rotations):
    """Global-start maps on the macro graph: a group starts at the mean of its members'
    starts, turned by its first member's start rotation minus the group's ``rot``."""
    if positions is not None:
        out = {r: xy for r, xy in positions.items() if r not in plan.member_of}
        for mref, m in plan.macros.items():
            points = [positions[r] for r in m["members"] if r in positions]
            if points:
                out[mref] = [
                    sum(p[0] for p in points) / len(points),
                    sum(p[1] for p in points) / len(points),
                ]
        positions = out
    if rotations is not None:
        out = {r: a for r, a in rotations.items() if r not in plan.member_of}
        for mref, m in plan.macros.items():
            first = next(iter(m["members"]))
            if first in rotations:
                out[mref] = (float(rotations[first]) - m["members"][first][2]) % 360
        rotations = out
    return positions, rotations


def map_inflation(plan, inflation):
    """Per-part spreading factors on the macro graph: a group takes its members' maximum."""
    if not inflation:
        return inflation
    out = {r: v for r, v in inflation.items() if r not in plan.member_of}
    for mref, m in plan.macros.items():
        values = [inflation[r] for r in m["members"] if r in inflation]
        if values:
            out[mref] = max(values)
    return out


def violations(graph, constraints):
    """Refs of every line group whose members are not rigidly in line.

    A group holds when its members sit at the :func:`layout` offsets under one common
    pose (to 1e-5 mm), each member's rotation is the group's ``rot`` plus the common
    cardinal angle, and all members are on one side."""
    lines = groups(constraints)
    if not lines:
        return []
    clearance = float(constraints.board.default_clearance_mm)
    bad = []
    for con in lines:
        try:
            parts = [graph.component(ref) for ref in con.refs]
            width, height, poses = layout(graph, con, clearance)
        except (KeyError, ValueError):
            bad.extend(con.refs)
            continue
        turn = (parts[0].rot - poses[parts[0].ref][2]) % 360
        if abs(turn / 90 - round(turn / 90)) > 1e-7 or any(
            abs((c.rot - poses[c.ref][2] - turn + 180) % 360 - 180) > 1e-6
            or c.side != parts[0].side
            for c in parts
        ):
            bad.extend(con.refs)
            continue
        theta = math.radians(round(turn / 90) * 90)
        ct, st = math.cos(theta), math.sin(theta)

        def offset(ref):
            x, y = poses[ref][0] - width / 2.0, poses[ref][1] - height / 2.0
            return x * ct - y * st, x * st + y * ct

        dx, dy = offset(parts[0].ref)
        centre = (parts[0].pos[0] - dx, parts[0].pos[1] - dy)
        for c in parts[1:]:
            ox, oy = offset(c.ref)
            if math.dist(c.pos, (centre[0] + ox, centre[1] + oy)) > TOLERANCE_MM:
                bad.extend(con.refs)
                break
    return sorted(set(bad))
