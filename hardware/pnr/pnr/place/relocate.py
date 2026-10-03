"""Board-wide translation proposals with a layered route-cost surrogate.

This is candidate ranking, not a clearance/connectivity certificate. The source
loop reroutes a proposal and retains its best completed routing round. No hard
pose, side, orientation, group or keepout is relaxed by this module.
"""

import heapq
import math
import os

import numpy as np

from pnr.graph import BoardGraph

from .geometry import outline_size, pin_positions, resolve_fixed_poses
from .metrics import hard_violations, hpwl, translation_checker


def distance_field(
    blocked, via_clear, crossing, seeds, pitch, via_cost=3.0, crossing_cost=100.0, *, terms=False
):
    """3D Dijkstra: 1 cost/mm, 3 mm/via, 100 additional cost/mm copper conflict.

    Foreign tracks may be crossed *only as an expensive rip-up estimate*.
    Static pad/keepout obstacles remain impassable. Through-vias require a clear
    aperture across every copper layer; planes can have antipads but not tracks.
    """
    layers, ny, nx = blocked.shape
    dist = np.full(blocked.shape, np.inf)
    queue = []
    detail = np.zeros((*blocked.shape, 3), dtype=float) if terms else None
    for la, j, i in seeds:
        if 0 <= la < layers and 0 <= j < ny and 0 <= i < nx and not blocked[la, j, i]:
            dist[la, j, i] = 0.0
            heapq.heappush(queue, (0.0, la, j, i))
    while queue:
        cost, la, j, i = heapq.heappop(queue)
        if cost != dist[la, j, i]:
            continue
        for y, x in ((j - 1, i), (j + 1, i), (j, i - 1), (j, i + 1)):
            if 0 <= y < ny and 0 <= x < nx and not blocked[la, y, x]:
                new = cost + pitch * (
                    1 + crossing_cost * max(crossing[la, j, i], crossing[la, y, x])
                )
                if new < dist[la, y, x]:
                    dist[la, y, x] = new
                    heapq.heappush(queue, (new, la, y, x))
                    if detail is not None:
                        detail[la, y, x] = detail[la, j, i]
                        detail[la, y, x, 0] += pitch
                        detail[la, y, x, 2] += pitch * max(crossing[la, j, i], crossing[la, y, x])
        if via_clear[j, i]:
            for other in range(layers):
                if other == la or blocked[other, j, i]:
                    continue
                new = cost + via_cost
                if new < dist[other, j, i]:
                    dist[other, j, i] = new
                    heapq.heappush(queue, (new, other, j, i))
                    if detail is not None:
                        detail[other, j, i] = detail[la, j, i]
                        detail[other, j, i, 1] += 1
    return (dist, detail) if terms else dist


def _near(grid, blocked, xy, layer, radius=1.5):
    i, j = grid.cell_of(*xy)
    cells = []
    steps = math.ceil(radius / grid.pitch)
    for y in range(max(0, j - steps), min(grid.ny, j + steps + 1)):
        for x in range(max(0, i - steps), min(grid.nx, i + steps + 1)):
            d = math.dist(xy, grid.center_of(x, y))
            if d <= radius and not blocked[layer, y, x]:
                cells.append((d, layer, y, x))
    return sorted(cells)


def _fields(graph, comp, rules, tracks, vias, pitch, *, terms=False, without=()):
    from pnr.route.detail.grid import RouteGrid
    from pnr.route.detail.router import (
        _fab,
        _mark_copper_keepouts,
        _mark_plane_regions,
        _mark_source_arrays,
        _net_widths,
        _plane_nets,
        _signal_layers,
    )

    # Remove only this component's pads from the static substrate. Keep its body
    # and parent-relative rule areas in graph for conservative keepout handling.
    static = BoardGraph.from_json(graph.to_json())
    static.component(comp.ref).pads = []
    for ref in without:  # a swap partner leaves its pose too
        static.component(ref).pads = []
    fab = _fab(rules)
    layers = _signal_layers(rules)
    names = {p.net for p in comp.pads if p.net} - _plane_nets(rules)
    widths = _net_widths(rules, fab["track_width_mm"])
    fields = {}
    for net in sorted(names):
        g = RouteGrid.from_graph(
            static,
            graph.outline.width,
            graph.outline.height,
            pitch=pitch,
            layers=layers,
            clearance=fab["clearance_mm"],
            track_width=widths.get(net, fab["track_width_mm"]),
            via_radius=fab["via_diameter_mm"] / 2,
        )
        _mark_plane_regions(g, static, rules, 2.0)
        _mark_copper_keepouts(g, graph, rules)
        # Source arrays on the moving footprint need candidate-specific rebuild;
        # those footprints are excluded by the proposal generator.
        _mark_source_arrays(g, static, rules)
        blocked = g.blocked.copy()
        via_clear = ~np.any(g.via_blocked, axis=0)
        for (la, i, j), owner in g.pad_net.items():
            if owner != net:
                blocked[la, j, i] = True
        for (la, i, j), owner in g.via_halo.items():
            if owner != net:
                via_clear[j, i] = False
        copper = np.zeros_like(blocked, dtype=float)

        def segment(a, b, radius, visit):
            dx, dy = b[0] - a[0], b[1] - a[1]
            ll = dx * dx + dy * dy
            ix, jy = g.cell_of(min(a[0], b[0]) - radius, min(a[1], b[1]) - radius)
            ex, ey = g.cell_of(max(a[0], b[0]) + radius, max(a[1], b[1]) + radius)
            for y in range(max(0, jy), min(g.ny, ey + 1)):
                for x in range(max(0, ix), min(g.nx, ex + 1)):
                    px, py = g.center_of(x, y)
                    t = max(0, min(1, ((px - a[0]) * dx + (py - a[1]) * dy) / (ll or 1)))
                    if math.hypot(px - a[0] - t * dx, py - a[1] - t * dy) <= radius:
                        visit(y, x)

        # Conservative raster half-cell diagonal avoids missing a crossing
        # simply because two centre-lines happen between coarse grid nodes.
        guard = pitch / math.sqrt(2)
        for owner, layer, a, b, width in tracks:
            if owner == net or layer not in layers:
                continue
            la = layers.index(layer)
            segment(
                a,
                b,
                width / 2 + g.clearance + g.track_width / 2 + guard,
                lambda y, x: copper.__setitem__((la, y, x), 1.0),
            )
            segment(
                a,
                b,
                width / 2 + g.clearance + g.via_radius + guard,
                lambda y, x: via_clear.__setitem__((y, x), False),
            )
        for owner, x, y in vias:
            if owner == net:
                continue
            segment(
                (x, y),
                (x, y),
                g.via_radius + g.clearance + g.track_width / 2 + guard,
                lambda j, i: copper.__setitem__((slice(None), j, i), 1.0),
            )
            segment(
                (x, y),
                (x, y),
                2 * g.via_radius + g.clearance + guard,
                lambda j, i: via_clear.__setitem__((j, i), False),
            )
        seeds = []
        for other in static.components:
            for pad, (_, xy) in zip(other.pads, pin_positions(other)):
                if pad.net != net:
                    continue
                side = 0 if other.side == "top" else len(layers) - 1
                for la in range(len(layers)) if pad.through_hole else [side]:
                    near = _near(g, blocked, xy, la)
                    if near:
                        seeds.append(near[0][1:])
        if seeds:
            result = distance_field(blocked, via_clear, copper, seeds, pitch, terms=terms)
            fields[net] = (g, blocked, *result) if terms else (g, blocked, result)
    return fields


PROBE_TERMS = (
    ("routing_length", "Layered path length", "mm", 1.0),
    ("via_transitions", "Through-layer transitions", "count", 3.0),
    ("foreign_copper", "Foreign copper crossing estimate", "mm", 100.0),
    ("terminal_approach", "Pad-to-grid approach", "mm", 1.0),
    ("unreachable_penalty", "Unreachable terminals", "count", 10000.0),
)


def probe_cost(comp, fields, pos):
    score = 0.0
    raw = [0.0] * 5
    omitted = []
    for pad, (_, xy) in zip(comp.pads, pin_positions(comp)):
        if pad.net not in fields:
            if pad.net:
                omitted.append(
                    dict(pad=pad.name, net=pad.net, reason="plane or no stationary terminal seeds")
                )
            continue
        field = fields[pad.net]
        grid, blocked, dist = field[:3]
        point = (xy[0] + pos[0] - comp.pos[0], xy[1] + pos[1] - comp.pos[1])
        side = 0 if comp.side == "top" else len(grid.layers) - 1
        near = _near(grid, blocked, point, side)
        values = [dist[la, j, i] + d for d, la, j, i in near]
        best = min(range(len(values)), key=lambda i: values[i]) if values else None
        cost = values[best] if best is not None else math.inf
        if not math.isfinite(cost):
            cost = 10000.0
            raw[4] += 1
        elif len(field) > 3:
            d, la, j, i = near[best]
            detail = field[3][la, j, i]
            for k in range(3):
                raw[k] += float(detail[k])
            raw[3] += d
        score += cost
    terms = [
        dict(
            key=key,
            label=label,
            raw_share=value,
            weights=[weight],
            weighted=value * weight,
            unit=unit,
        )
        for (key, label, unit, weight), value in zip(PROBE_TERMS, raw)
    ]
    return float(score), int(raw[4]), terms, omitted


def _score(comp, fields, target, terminals):
    """Layered field cost of ``comp``'s pads with its centre at ``target`` plus 0.1 of
    the endpoint Manhattan screen (the translation proposals' heavy cost)."""
    dx, dy = target[0] - comp.pos[0], target[1] - comp.pos[1]
    score, air = 0.0, 0.0
    for pad, (_, xy) in zip(comp.pads, pin_positions(comp)):
        pt = (xy[0] + dx, xy[1] + dy)
        peers = [q for ref, q in terminals.get(pad.net, []) if ref != comp.ref]
        if peers:
            air += min(abs(pt[0] - q[0]) + abs(pt[1] - q[1]) for q in peers)
        if pad.net not in fields:
            continue
        grid, blocked, dist = fields[pad.net][:3]
        side = 0 if comp.side == "top" else len(grid.layers) - 1
        values = [dist[la, j, i] + d for d, la, j, i in _near(grid, blocked, pt, side)]
        best = min(values, default=np.inf)
        score += best if math.isfinite(best) else 10000.0
    return score + 0.1 * air


def _shifted(terminals, shifts):
    """``terminals`` ({net: [(ref, xy)]}) with the pins of each ref in ``shifts``
    ({ref: (dx, dy)}) moved by its shift."""
    return {
        net: [
            (ref, (xy[0] + shifts[ref][0], xy[1] + shifts[ref][1]) if ref in shifts else xy)
            for ref, xy in pins
        ]
        for net, pins in terminals.items()
    }


def _swaps(
    g, posed, scored, terminals, rules, tracks, vias, pitch, tried, temperature, limit=2, held=()
):
    """Swap proposals among the parts :func:`propose` scored (except ``held`` ones:
    row and line members, macros): pairs with courtyard areas within a factor of two,
    legal when exchanged (``posed``), ranked by the endpoint screen; the best
    ``limit`` are scored with fields rebuilt without both. Before and after are
    costed alike: each part by :func:`_score` in the fields without its partner, its
    peers' pins where they are (after: the partner's at its new centre)."""
    if posed is None or len(scored) < 2:
        return []
    from .geometry import courtyard_rect

    refs = sorted(r for r in scored if r not in held)

    def swapped(ca, cb):
        pa, pb = ca.pos, cb.pos
        return _shifted(
            terminals,
            {ca.ref: (pb[0] - pa[0], pb[1] - pa[1]), cb.ref: (pa[0] - pb[0], pa[1] - pb[1])},
        )

    pairs = []
    for i, a in enumerate(refs):
        for b in refs[i + 1 :]:
            ca, cb = g.component(a), g.component(b)
            ra, rb = courtyard_rect(ca), courtyard_rect(cb)
            if not 0.5 <= (rb.w * rb.h) / max(ra.w * ra.h, 1e-9) <= 2.0:
                continue
            if (a, b, "swap") in tried:
                continue
            pa, pb = ca.pos, cb.pos
            after = swapped(ca, cb)
            gain = sum(
                _score(c, {}, here, terminals) - _score(c, {}, there, after)
                for c, here, there in ((ca, pa, pb), (cb, pb, pa))
            )
            ca.pos, cb.pos = pb, pa
            ok = posed([ca, cb])
            ca.pos, cb.pos = pa, pb
            if ok:
                pairs.append((-gain, a, b))
    out = []
    for _, a, b in sorted(pairs)[:limit]:
        ca, cb = g.component(a), g.component(b)
        pa, pb = ca.pos, cb.pos
        after_terminals = swapped(ca, cb)
        before = after = 0.0
        for comp, here, target, partner in ((ca, pa, pb, b), (cb, pb, pa, a)):
            fields = _fields(g, comp, rules, tracks, vias, pitch, without=(partner,))
            before += _score(comp, fields, here, terminals)
            after += _score(comp, fields, target, after_terminals)
        if after < before - 1e-6 or temperature > 0:
            out.append(
                (
                    before - after,
                    a,
                    pb,
                    before,
                    after,
                    (a, b, "swap"),
                    [(a, pb, None), (b, pa, None)],
                )
            )
    return out


def propose(
    graph,
    constraints,
    rules,
    tracks,
    vias=(),
    *,
    pressure=None,
    tried=None,
    refs=None,
    pitch=0.8,
    candidate_pitch=2.0,
    shortlist=32,
    max_parts=6,
    temperature=0.0,
    rng=None
):
    """Search the full legal board for translations, preserving physical rules.

    Shortlist by endpoint Manhattan distance plus spatially distributed samples;
    heavy scoring uses per-net layered distance fields from stationary terminals.
    Declines unsupported nonrectangular outlines and moving source-array/keepout
    owners until their candidate-dependent obstacles can be rebuilt correctly.

    With side-free parts (:mod:`pnr.place.sides`) two more proposals join the
    translations: a *flip* (the part at its centre on its other side, scored by the
    same fields: its pads mirror and move to the far layer) and a *swap* of two
    evaluated parts with similar courtyards (each at the other's centre, fields
    rebuilt without both), each checked by :func:`pnr.place.metrics.pose_checker`.
    """
    import random

    from .anneal import choose_cost
    from .placer import PlacementReport

    sample_parts = rng is not None
    rng = rng or random.Random(0)
    g = BoardGraph.from_json(graph.to_json())
    pressure = pressure or {}
    tried = tried if tried is not None else set()
    width, height = outline_size(g, constraints)
    if g.outline.polygon and any(
        x not in (0, width) or y not in (0, height) for x, y in g.outline.polygon
    ):
        raise ValueError("relocation currently requires a rectangular outline")
    fixed = resolve_fixed_poses(g, constraints)
    unsupported = {
        v["ref"] for v in rules.get("plane_access_intents", []) if v["kind"] == "power_array"
    } | {v["ref"] for v in rules.get("copper_keepouts", [])}
    legal = translation_checker(g, constraints)
    from .geometry import set_component_side
    from .metrics import pose_checker
    from .sides import macro, opposite
    from .sides import plan as side_plan

    plan = side_plan(g, constraints, rules)
    posed = pose_checker(g, constraints) if plan.active else None
    # Swaps move two parts by pose_checker, which does not re-check rows and lines.
    unswappable = {
        r for con in constraints.constraints if con.kind in ("line_group", "row") for r in con.refs
    } | {c.ref for c in g.components if macro(c)}
    scored = {}  # ref -> (original centre, baseline heavy cost), for swaps
    terminals = {}
    for c in g.components:
        for p, (_, xy) in zip(c.pads, pin_positions(c)):
            terminals.setdefault(p.net, []).append((c.ref, xy))
    eligible = [
        c
        for c in g.components
        if c.ref not in fixed
        and not c.locked
        and c.ref not in unsupported
        and (refs is None or c.ref in refs)
    ]
    eligible.sort(
        key=lambda c: (-(1 + pressure.get(c.ref, 0)) * len({p.net for p in c.pads if p.net}), c.ref)
    )
    # Warm exploration considers different components, not only the same high-pin
    # components every cycle. Explicit refs still constrain the eligible pool.
    if sample_parts:
        rng.shuffle(eligible)
    choices = []
    audit = []
    for comp in eligible[:max_parts]:
        original = comp.pos

        def light(pos):
            dx, dy = pos[0] - original[0], pos[1] - original[1]
            cost = 0
            for pad, (_, xy) in zip(comp.pads, pin_positions(comp)):
                peers = [q for ref, q in terminals.get(pad.net, []) if ref != comp.ref]
                if peers:
                    cost += min(abs(xy[0] + dx - q[0]) + abs(xy[1] + dy - q[1]) for q in peers)
            return cost

        # Compute light cost with component at original pose.
        points = []
        for x in np.arange(candidate_pitch / 2, width, candidate_pitch):
            for y in np.arange(candidate_pitch / 2, height, candidate_pitch):
                comp.pos = (float(x), float(y))
                ok = legal(comp)
                comp.pos = original
                if ok:
                    points.append((light((x, y)), (float(x), float(y))))
        points.sort()
        selected = [p for _, p in points[:shortlist]]
        # Do not let a short-airwire basin hide empty distant regions.
        bins = {}
        for cost, p in points:
            bins.setdefault((int(p[0] / (width / 6)), int(p[1] / (height / 6))), p)
        selected = list(dict.fromkeys([original, *selected, *bins.values()]))
        if len(selected) < 2:
            continue
        capture = bool(os.environ.get("PNR_COST_CAPTURE_DIR"))
        fields = _fields(
            g, comp, rules, tracks, vias, pitch, **({"terms": True} if capture else {})
        )
        evaluated = {}

        def heavy(pos):
            if capture:
                score, missing, terms, omitted = probe_cost(comp, fields, pos)
                air = light(pos)
                terms.append(
                    dict(
                        key="airwire_screen",
                        label="Endpoint Manhattan screen",
                        raw_share=air,
                        weights=[0.1],
                        weighted=0.1 * air,
                        unit="mm",
                    )
                )
                total = score + 0.1 * air
                evaluated[tuple(pos)] = dict(
                    position=list(pos),
                    cost=total,
                    unreachable=missing,
                    terms=terms,
                    omitted=omitted,
                )
                return total, missing
            score = 0.0
            missing = 0
            for pad, (_, xy) in zip(comp.pads, pin_positions(comp)):
                if pad.net not in fields:
                    continue
                grid, blocked, dist = fields[pad.net]
                pt = (xy[0] + pos[0] - original[0], xy[1] + pos[1] - original[1])
                side = 0 if comp.side == "top" else len(grid.layers) - 1
                values = [dist[la, j, i] + d for d, la, j, i in _near(grid, blocked, pt, side)]
                best = min(values, default=np.inf)
                if not math.isfinite(best):
                    missing += 1
                    best = 10000.0
                score += best
            return score + 0.1 * light(pos), missing

        baseline, _ = heavy(original)
        ranked = []
        for pos in selected:
            identity = (comp.ref, *pos)
            if pos == original or identity in tried:
                continue
            cost, missing = heavy(pos)
            ranked.append(dict(position=list(pos), cost=cost, unreachable=missing))
            if cost < baseline - 1e-6 or temperature > 0:
                choices.append(
                    (
                        baseline - cost,
                        comp.ref,
                        pos,
                        baseline,
                        cost,
                        identity,
                        [(comp.ref, pos, None)],
                    )
                )
        if posed is not None:
            scored[comp.ref] = (original, baseline)
            other = opposite(comp.side)
            identity = (comp.ref, *original, other)
            if plan.allows(comp.ref, other) and identity not in tried:
                here = comp.side
                set_component_side(comp, other)
                if posed([comp]):
                    cost, missing = heavy(original)
                    ranked.append(dict(position=list(original), side=other, cost=cost))
                    if cost < baseline - 1e-6 or temperature > 0:
                        choices.append(
                            (
                                baseline - cost,
                                comp.ref,
                                original,
                                baseline,
                                cost,
                                identity,
                                [(comp.ref, original, other)],
                            )
                        )
                set_component_side(comp, here)
        audit.append(
            dict(
                ref=comp.ref,
                baseline_cost=baseline,
                legal_candidates=len(points),
                heavy_candidates=len(selected),
                best_candidates=sorted(ranked, key=lambda d: d["cost"])[:8],
            )
        )
        if capture:
            from .cost_capture import routing_probe

            routing_probe(
                g,
                comp,
                evaluated[tuple(original)],
                list(evaluated.values()),
                dict(
                    model="global-layered-relocation-v2",
                    held_out=[comp.ref],
                    grid_pitch_mm=pitch,
                    candidate_pitch_mm=candidate_pitch,
                    unscored_nets=evaluated[tuple(original)]["omitted"],
                    screen="Layered routing proxy plus .1 endpoint Manhattan distance; fixed face/orientation",
                ),
            )
    choices += _swaps(
        g,
        posed,
        scored,
        terminals,
        rules,
        tracks,
        vias,
        pitch,
        tried,
        temperature,
        held=unswappable,
    )
    while True:
        if not choices:
            return None
        # Compare relative improvements across components with different pin counts.
        # Zero temperature retains the original deterministic greedy behavior.
        chosen = (
            choose_cost([(v[4] - v[3]) / max(1.0, v[3]) for v in choices], temperature, rng)
            if temperature > 0
            else max(
                range(len(choices)), key=lambda i: (choices[i][0], choices[i][1], choices[i][2])
            )
        )
        gain, ref, pos, before, after, identity, moves = choices[chosen]
        tried.add(identity)
        olds = {r: (g.component(r).pos, g.component(r).side) for r, _, _ in moves}
        for r, xy, side in moves:
            g.component(r).pos = xy
            if side is not None:
                set_component_side(g.component(r), side)
        bad = hard_violations(g, constraints)
        if not any(bad.values()):
            break
        if len(moves) == 1 and moves[0][2] is None:
            raise AssertionError(bad)  # a translation: translation_checker disagrees
        # A flip or swap that breaks a rule pose_checker does not know: undo, drop it.
        for r, (xy, side) in olds.items():
            g.component(r).pos = xy
            set_component_side(g.component(r), side)
        choices.pop(chosen)
    report = PlacementReport(width, height, hpwl(graph), hpwl(g), **bad)
    return (
        g,
        report,
        dict(
            model="global-layered-relocation-v2",
            temperature=temperature,
            sampled_candidates=len(choices),
            moves=[
                dict(
                    ref=r,
                    original=list(olds[r][0]),
                    position=list(xy),
                    distance_mm=math.dist(olds[r][0], xy),
                    **({} if side is None else dict(side=side, original_side=olds[r][1])),
                )
                for r, xy, side in moves
            ],
            cost_before=before,
            cost_after=after,
            via_cost_mm=3.0,
            crossing_cost_per_mm=100.0,
            grid_pitch_mm=pitch,
            candidates=audit,
            fixed_refs=sorted(fixed),
            unsupported_refs=sorted(unsupported),
            routing_validation="pending",
        ),
    )
