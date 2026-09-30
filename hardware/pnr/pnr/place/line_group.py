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


def collapse(graph, constraints, rules):
    """``(macro graph, macro constraints, macro rules, plan)`` with every line group a macro.

    The macros are ``LG00``, ``LG01``, ... in declaration order, with no courtyard
    margin (the legalizer keeps the usual clearance around them). The ``line_group``
    constraints are dropped from the macro constraints (the inner placement must not
    collapse again); a group with an ``edge`` gets a soft ``edge_align`` on its macro.
    """
    from pnr.hier.macro import collapse as collapse_macros

    clearance = float(constraints.board.default_clearance_mm)
    lines = groups(constraints)
    members = {ref for con in lines for ref in con.refs}
    for intent in (rules or {}).get("plane_access_intents", []) or []:
        if intent.get("ref") in members:
            raise ValueError(
                "line_group member %s carries a plane-access intent; line groups do not "
                "support them" % intent.get("ref")
            )
    layouts = []
    for con in lines:
        width, height, poses = layout(graph, con, clearance)
        sub = BoardGraph(name=graph.name + ":line:" + con.name)
        for ref in con.refs:
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
