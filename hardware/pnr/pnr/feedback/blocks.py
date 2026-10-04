"""Block-template side of the feedback loop (used by :mod:`pnr.hier.synth_native`).

Layouts are ``{local key: [x, y, rot, side]}`` in the block frame (origin at the
block rectangle's lower-left, no sub-board apron). A child differs from its
parent layout in one key's pose.
"""

from __future__ import annotations

import hashlib
import json
import math

from pnr.feedback.signals import merge, read_round


def block_key(block):
    """read_round key: a component's block-local path."""
    from pnr.hier.synth import local_key

    return lambda comp: local_key(block, comp.get("address") or "")


def layout_sha(layout, n=6):
    items = sorted(
        (k, round(v[0], 4), round(v[1], 4), float(v[2]) % 360, v[3]) for k, v in layout.items()
    )
    return hashlib.sha1(json.dumps(items).encode()).hexdigest()[:n]


def layout_key(rec):
    """Dedup key of a layout (the one synth_native's stage A uses)."""
    return json.dumps(
        [
            rec["width"],
            rec["height"],
            sorted((k, round(v[0], 2), round(v[1], 2), v[2]) for k, v in rec["layout"].items()),
        ]
    )


def base_tag(rec):
    name = rec["block"]
    return "%s-s%d-%gx%g" % (
        name.replace(":", "_").replace(".", "_"),
        rec["seed"],
        rec["width"],
        rec["height"],
    )


def tag_of(rec):
    """Layout tag (native dir name without the repeat suffix)."""
    return rec.get("tag") or base_tag(rec) + rec.get("tag_suffix", "")


def layout_fb(rec, blocks=None):
    """Merged feedback of a native layout record over its instances.

    Instances without a stored ``fb`` are read from their ``dir`` (imports of
    runs made before PNR_FEEDBACK existed) when ``blocks`` ({name: Block}) is given."""
    fbs = []
    for inst in rec.get("instances") or []:
        fb = inst.get("fb")
        if fb is None and blocks is not None and inst.get("dir") and inst.get("instance") in blocks:
            fb = read_round(inst["dir"], key=block_key(blocks[inst["instance"]]))
            inst["fb"] = fb
        fbs.append(fb or dict(missing=True, dir=inst.get("dir")))
    return merge(fbs)


def missing(rec):
    o = rec.get("objective")
    return (o[5] + o[0]) if o else None


def block_board(
    graph,
    constraints,
    rules,
    blocks,
    names,
    layout,
    w,
    h,
    *,
    origin_layout=None,
    q_ref=None,
    power_first=False,
):
    """MoveBoard of a template layout on its first instance's sub-board.

    Courtyards carry the plane-array reservations place() legalizes with; the
    extra check is the rigidity check accepted shove nudges pass
    (``synth_native._layout_violations``) over every instance of the template."""
    from pnr.feedback.moves import MoveBoard, default_anchors, hubs, plane_nets, power_guard
    from pnr.hier.synth import instance_board, local_key
    from pnr.hier.synth_native import _layout_violations

    rep = blocks[names[0]]
    g2, c2, r2 = instance_board(graph, constraints, rules, rep, layout, w, h)
    from pnr.place.pair_landing import enabled as landing_enabled

    if landing_enabled():
        # PNR_PAIR_LANDING_RESERVE=1: block moves keep diff-pair via landings clear.
        from pnr.place.pair_landing import attach

        attach(g2, r2)
    if r2.get("plane_access_intents"):
        from pnr.plane_intent import reserve_array_space

        reserve_array_space(
            g2,
            r2["plane_access_intents"],
            r2["plane_access_fab"],
            r2.get("fab", {}).get("edge_clearance_mm", 0.2),
        )
    key_of = {c.ref: local_key(rep, c.address) for c in g2.components}
    anchors = default_anchors(g2, c2, hubs(g2, [rep]))
    tier1, guard = frozenset(), None
    if power_first:
        from pnr.power_topology import PowerTopologyUnavailable, derive

        try:
            roles = derive(g2, c2, r2)
            tier1 = frozenset(roles["tier1"])
            guard = power_guard(g2, roles, q_ref=q_ref)
        except PowerTopologyUnavailable:
            pass
    origin = None
    if origin_layout:
        origin = {r: tuple(origin_layout[k][:2]) for r, k in key_of.items() if k in origin_layout}

    def extra(graph_, moved):
        after = dict(layout)
        for ref, pose in moved.items():
            after[key_of[ref]] = list(pose)
        bad = {}
        for name in names:
            for kind, values in _layout_violations(
                graph, constraints, rules, blocks[name], layout, after, w, h
            ).items():
                bad.setdefault(kind, []).extend(values)
        return sorted(bad)

    from pnr.place.compact import legalize_settings, margin_kwargs, placement_clearance

    return MoveBoard(
        graph=g2,
        constraints=c2,
        key_of=key_of,
        anchors=anchors,
        tier1=tier1,
        origin=origin,
        # The board clearance; the courtyard gap with PNR_COMPACT LEGALIZE, plus the
        # copper margins of parts whose box hugs their pads.
        clearance=placement_clearance(c2),
        **margin_kwargs(legalize_settings(g2, c2, r2)),
        plane=plane_nets(g2, c2),
        extra_check=extra,
        guard=guard,
    )


def child_layout(layout, board, poses):
    out = {k: list(v) for k, v in layout.items()}
    for ref, pose in poses.items():
        out[board.key_of[ref]] = [float(pose[0]), float(pose[1]), float(pose[2]) % 360, pose[3]]
    return out


def displacement(before, after):
    """{key: mm} for keys whose pose changed, and the max."""
    moved = {
        k: math.dist(before[k][:2], after[k][:2])
        for k in after
        if k in before
        and (
            math.dist(before[k][:2], after[k][:2]) > 1e-9
            or abs((float(before[k][2]) - float(after[k][2])) % 360) > 1e-9
        )
    }
    return moved
