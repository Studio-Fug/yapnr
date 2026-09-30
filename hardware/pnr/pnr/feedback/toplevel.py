"""Top-level (halving) side of the feedback loop.

A parent is a candidate evaluated at the native stage. Its pose is the
candidate's ``placed.json`` (courtyards include the plane-array reservations the
placer legalized with) with positions and rotations taken from the stage's
``evaluated-placed.json`` (accepted shove nudges, the early USB-pair rescue),
so a child starts from what was actually routed. If that pose breaks a hard
rule the placed pose is used instead.

Flat mode: every part is its own unit; hubs (the most-padded part of each
extracted block), fixed, locked and interface parts are anchors. Library mode
(``--library``): each library block is one rigid unit moved by translation only
(its routed copper, when assembled, follows the footprint poses), block hubs are
not anchors, top-level glue parts move alone.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from pnr.graph import BoardGraph


def _norm(rot):
    return float(round(float(rot)) % 360)


def parent_pose(placed_path, evaluated_path=None):
    """(BoardGraph, pose_source): placed.json with evaluated positions/rotations by ref."""
    graph = BoardGraph.from_json(Path(placed_path).read_text())
    if evaluated_path is None or not Path(evaluated_path).exists():
        return graph, "placed"
    try:
        evaluated = {
            c["ref"]: c for c in json.loads(Path(evaluated_path).read_text())["components"]
        }
    except (OSError, ValueError, KeyError):
        return graph, "placed"
    changed = 0
    for comp in graph.components:
        e = evaluated.get(comp.ref)
        if e is None:
            continue
        pos, rot = (float(e["pos"][0]), float(e["pos"][1])), _norm(e["rot"])
        if math.dist(pos, comp.pos) > 1e-9 or abs((rot - _norm(comp.rot)) % 360) > 1e-9:
            comp.pos, comp.rot = pos, rot
            changed += 1
    return graph, "evaluated" if changed else "placed"


def prepared(graph, constraints, rules):
    """(constraints with source locks, prepared source) exactly as halving stage 0 checks them."""
    from pnr.place.initial_pool import _prepared_source, preserve_source_locks

    con = preserve_source_locks(graph, constraints)
    return con, _prepared_source(graph, con, rules)


def source_errors(candidate, source, constraints):
    from pnr.place.initial_pool import _hard_and_source_errors

    return {k: v for k, v in _hard_and_source_errors(candidate, source, constraints).items() if v}


def top_board(
    parent, graph, constraints, rules, *, library_blocks=(), origin=None, power_first=False
):
    """MoveBoard over a parent candidate pose (``parent`` BoardGraph, mutated and restored)."""
    from pnr.feedback.moves import MoveBoard, default_anchors, hubs, plane_nets, power_guard
    from pnr.hier.blocks import extract_blocks

    con, source = prepared(graph, constraints, rules)
    from pnr.place.pair_landing import enabled as landing_enabled

    if landing_enabled():
        # PNR_PAIR_LANDING_RESERVE=1: moves keep diff-pair via landings clear (a
        # parent placed without the reserve keeps its existing violations; the
        # MoveBoard only forbids new ones).
        from pnr.place.pair_landing import attach

        attach(parent, rules)
    blocks = extract_blocks(source, con)
    lib = set(library_blocks or ())
    units = {b.name: tuple(sorted(b.refs)) for b in blocks if b.name in lib}
    anchors = default_anchors(parent, con, hubs(parent, [b for b in blocks if b.name not in lib]))
    tier1, guard = frozenset(), None
    if power_first:
        from pnr.place.power_first import roles_for

        roles = roles_for(parent, con, rules)
        if roles is not None:
            tier1 = frozenset(roles["tier1"])
            guard = power_guard(parent, roles)

    def extra(graph_, moved):
        return sorted(source_errors(graph_, source, con))

    return MoveBoard(
        graph=parent,
        constraints=con,
        key_of={c.ref: c.ref for c in parent.components},
        anchors=anchors,
        units=units or None,
        tier1=tier1,
        origin=origin,
        clearance=float(con.board.default_clearance_mm),
        plane=plane_nets(parent, con),
        extra_check=extra,
        guard=guard,
    )


def child_graph(parent, poses):
    out = BoardGraph.from_json(parent.to_json())
    for ref, pose in poses.items():
        c = out.component(ref)
        c.pos, c.rot = (float(pose[0]), float(pose[1])), _norm(pose[2])
    return out


def poses_sha(graph, n=10):
    import hashlib

    items = sorted(
        (c.ref, round(c.pos[0], 4), round(c.pos[1], 4), _norm(c.rot), c.side)
        for c in graph.components
    )
    return hashlib.sha1(json.dumps(items).encode()).hexdigest()[:n]
