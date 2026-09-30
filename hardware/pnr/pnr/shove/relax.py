"""Wish plans: the production power planner against a relaxed obstacle model.

Every candidate comes from :func:`pnr.native_electrical.power_plan`, so contract
widths, via banks, necks, landings and in-pad arrays are built in. Relaxation
only changes which *existing* items are obstacles:

* :func:`relaxed_oracle` (rungs L1/L2): soft items may be penetrated by at most
  ``delta`` (their displacement budget): the hard world is the exact Oracle
  without the soft items, the soft world is a second copy of the board holding
  only the soft items, each shrunk by ``delta`` per side, with exact clearances.
* :func:`ignoring_oracle` (rung L4): rip candidates are not obstacles at all.

The make-room QP, the native worker and the whole-transaction gate decide
whether the plan is actually realised.
"""

import math
import os
import time


def soft_pads(board, parts):
    """UUIDs of the simple-shape lands of nudgeable ``parts`` (rect, rounded
    rect, oval, circle): these may be penetrated by delta like soft copper."""
    import pcbnew as k
    from pnr.native_electrical import uid

    shapes = (k.PAD_SHAPE_RECT, k.PAD_SHAPE_ROUNDRECT, k.PAD_SHAPE_OVAL, k.PAD_SHAPE_CIRCLE)
    return {
        uid(p)
        for f in board.GetFootprints()
        if f.GetReference() in set(parts)
        for p in f.Pads()
        if p.GetShape() in shapes and max(p.GetDrillSize().x, p.GetDrillSize().y) == 0
    }


def relaxed_oracle(board, board_path, rules, soft, delta, deadline, parts=()):
    import pcbnew as k
    from pnr.native_electrical import Oracle, uid
    from pnr.fab_profile import load_board

    pads = soft_pads(board, parts) if parts else set()
    soft = set(soft) | pads
    hard = Oracle(board, rules, ignored=soft, deadline=deadline)
    copy = load_board(board_path)
    for t in copy.GetTracks():
        if uid(t) not in soft:
            continue
        if t.GetClass() == "PCB_VIA":
            t.SetFrontWidth(max(10000, t.GetWidth(k.F_Cu) - round(2 * delta * 1e6)))
            t.SetDrill(max(10000, t.GetDrillValue() - round(2 * delta * 1e6)))
        else:
            t.SetWidth(max(10000, t.GetWidth() - round(2 * delta * 1e6)))
    for f in copy.GetFootprints():
        for p in f.Pads():
            if uid(p) in pads:
                size = p.GetSize()
                p.SetSize(
                    k.VECTOR2I(
                        max(10000, size.x - round(2 * delta * 1e6)),
                        max(10000, size.y - round(2 * delta * 1e6)),
                    )
                )
    ids = {uid(p) for f in copy.GetFootprints() for p in f.Pads()} | {
        uid(t) for t in copy.GetTracks()
    }
    softer = Oracle(copy, rules, ignored=ids - set(soft), deadline=deadline)
    softer.board_copy = copy  # keep the SWIG owner alive with the oracle
    hard_clear, hard_via = hard.clear, hard.via

    def clear(net, layer, a, z, width, ignore_nets=(), pair=None):
        return hard_clear(net, layer, a, z, width, ignore_nets, pair) and softer.clear(
            net, layer, a, z, width, ignore_nets, pair
        )

    def via(net, p, diameter, drill):
        return hard_via(net, p, diameter, drill) and softer.via(net, p, diameter, drill)

    hard.clear = clear
    hard.via = via
    hard.soft_oracle = softer
    return hard


def ignoring_oracle(board, rules, ignored, deadline):
    from pnr.native_electrical import Oracle

    return Oracle(board, rules, ignored=ignored, deadline=deadline)


def plan_route(board, rules, oracle, net, source, target, bounds, pitch):
    """power_plan with the PNR_SHOVE forced in-pad retry (as native_electrical.main)."""
    from pnr.native_electrical import power_plan

    try:
        plan = power_plan(board, net, source, target, rules, oracle, bounds, pitch)
    except TimeoutError:
        plan = dict(status="time_budget")
    if (
        plan.get("status") != "routed"
        and oracle.__dict__.pop("in_pad_skipped", False)
        and time.monotonic() < oracle.deadline
        and os.environ.get("PNR_SHOVE_IN_PAD", "1") != "0"
    ):
        try:
            plan = power_plan(
                board, net, source, target, rules, oracle, bounds, pitch, _force_in_pad=True
            )
            plan["forced_in_pad"] = True
        except TimeoutError:
            plan = dict(status="time_budget", forced_in_pad=True)
    return plan


def wish_plan(
    board,
    board_path,
    rules,
    net,
    source,
    target,
    soft,
    deltas,
    bounds,
    pitch,
    seconds,
    names=None,
    parts=(),
):
    """First delta tier whose relaxed plan routes: ``(plan or None, tiers)``."""
    tiers = []
    names = names or {}
    # One deadline shared by every delta tier: the caller's budget is for the whole plan.
    deadline = time.monotonic() + seconds
    for delta in deltas:
        started = time.monotonic()
        if started >= deadline:
            tiers.append(dict(delta=delta, status="time_budget", seconds=0.0))
            continue
        oracle = relaxed_oracle(board, board_path, rules, soft, delta, deadline, parts)
        plan = plan_route(board, rules, oracle, net, source, target, bounds, pitch)
        tiers.append(
            dict(
                delta=delta,
                status=plan.get("status"),
                seconds=round(time.monotonic() - started, 2),
                forced_in_pad=plan.get("forced_in_pad", False),
                hard_hits=[(names.get(u, u), c) for u, c in oracle.hits.most_common(8)],
                soft_hits=[(names.get(u, u), c) for u, c in oracle.soft_oracle.hits.most_common(8)],
                soft_via_hits=[
                    (names.get(u, u), c) for u, c in oracle.soft_oracle.via_hits.most_common(8)
                ],
            )
        )
        if plan.get("status") == "routed":
            plan["delta"] = delta
            return plan, tiers
    return None, tiers


def plan_shapes(board, plan):
    """``[(layer or None, shape, width_or_diameter)]`` for every copper element of
    a routed plan (tracks, bank vias, in-pad vias) as native probe shapes."""
    import pcbnew as k
    from pnr.native_electrical import vec

    shapes = []
    for la, a, z, w in plan.get("tracks", []):
        t = k.PCB_TRACK(board)
        t.SetLayer(la)
        t.SetStart(vec(a))
        t.SetEnd(vec(z))
        t.SetWidth(round(w * 1e6))
        shapes.append((la, t))
    sizing = (plan.get("policy") or {}).get("via_array") or {}
    vias = [
        (p, sizing.get("diameter_mm", 0.45), sizing.get("drill_mm", 0.3))
        for _, pts, _ in plan.get("banks", [])
        for p in pts
    ]
    vias += [(p, d, h) for p, d, h in plan.get("in_pad_vias") or []]
    for p, d, h in vias:
        v = k.PCB_VIA(board)
        v.SetPosition(vec(p))
        v.SetViaType(k.VIATYPE_THROUGH)
        v.SetLayerPair(k.F_Cu, k.B_Cu)
        v.SetFrontWidth(round(d * 1e6))
        v.SetDrill(round(h * 1e6))
        shapes.append((None, v))
    return shapes


def plan_collisions(board, rules, plan, candidates, net):
    """Existing items (``candidates`` = {uid: item}) whose copper the plan's copper
    would violate at the exact net-class clearance: ``{uid: hits}``."""
    from collections import Counter
    from pnr.electrical import net_policy

    layers = list(board.GetEnabledLayers().CuStack())
    probes = plan_shapes(board, plan)
    own = net_policy(net, rules)["clearance_mm"]
    hits = Counter()
    for identity, item in candidates.items():
        gap = max(own, net_policy(item.GetNetname(), rules)["clearance_mm"]) + 0.001
        for la, probe in probes:
            for layer in [la] if la is not None else layers:
                if not (item.IsOnLayer(layer) and probe.IsOnLayer(layer)):
                    continue
                if item.GetEffectiveShape(layer).Collide(
                    probe.GetEffectiveShape(layer), round(gap * 1e6)
                ):
                    hits[identity] += 1
                    break
    return hits


def crossing_nets(plan, board, candidates):
    """Nets whose same-layer centreline the plan's centreline properly crosses:
    a topological crossing no displacement can undo (L4 rip candidates)."""
    from pnr.shove.geom import seg_intersect
    from pnr.native_electrical import xy

    out = set()
    for la, a, z, w in plan.get("tracks", []):
        for item in candidates.values():
            if item.GetClass() != "PCB_TRACK" or item.GetLayer() != la:
                continue
            if seg_intersect(tuple(a), tuple(z), xy(item.GetStart()), xy(item.GetEnd())):
                out.add(item.GetNetname())
    return out


def serialise(plan):
    """JSON-safe copy of a routed power plan (layer ids stay KiCad integers)."""
    import json

    keep = (
        "status",
        "mode",
        "policy",
        "tracks",
        "banks",
        "in_pad_vias",
        "in_pad_array",
        "neck",
        "root_landing",
        "root_strategy",
        "delta",
        "forced_in_pad",
        "reversed_branch_retry",
    )
    out = {key: plan[key] for key in keep if key in plan}
    return json.loads(
        json.dumps(out, default=lambda v: list(v) if isinstance(v, tuple) else str(v))
    )


def distance_to(points, item):
    from pnr.native_electrical import xy
    from pnr.shove.geom import closest_on_seg

    if item.GetClass() == "PCB_VIA":
        return min(math.dist(p, xy(item.GetPosition())) for p in points)
    a, z = xy(item.GetStart()), xy(item.GetEnd())
    return min(math.dist(p, closest_on_seg(p, a, z)[1]) for p in points)
