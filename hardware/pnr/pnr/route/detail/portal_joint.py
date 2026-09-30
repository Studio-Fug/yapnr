"""Experimental exact-geometry portal assignment before regional body routing.

Enumerated, bounded access choices are coordinated before expensive trunk search.
A failed assignment is not proof of unroutability. Never emit partial geometry.
"""

import math
import time
from dataclasses import dataclass
from functools import cached_property, lru_cache

from pnr.fab_profile import active_geometry

from .joint import conflict, conflicts, solve_joint_region
from .joint_access import select_joint
from .keyhole import route
from .layered import SearchTimeout, escape_frontier, primitives
from .regional import RegionalResult, Request
from .spatial_conflicts import PrimitiveIndex


@dataclass
class Portal:
    request: object
    side: int
    path: list
    layers: tuple
    via: bool
    cost: float

    @property
    def point(self):
        return self.path[-1][:2]

    @cached_property
    def copper(self):
        out = list(primitives(self.path))
        if self.via:
            out.append(("via", None, self.point, self.point))
        return out


def portal_conflict(a, b):
    return any(conflict(a.request, x, b.request, y) for x in a.copper for y in b.copper)


def solve_portal_region(
    requests,
    bounds,
    static_clear,
    static_via_clear,
    *,
    pitch=0.1,
    max_orders=16,
    max_expansions=10000,
    layers=3,
    terminal_layers=lambda r, p: (0,),
    first_via_allowed=lambda r, p: True,
    on_event=None,
    max_seconds=120.0,
    route_seconds=None,
    max_total_vias=5,
):
    if (
        not requests
        or layers < 2
        or pitch <= 0
        or not 0 < max_seconds < math.inf
        or not 0 <= max_total_vias <= 5
    ):
        raise ValueError("nonempty requests and positive finite geometry/budgets required")
    deadline = time.monotonic() + max_seconds
    events = []
    terminal_index = PrimitiveIndex(
        [
            (r, ("track", la, p, p))
            for r in requests
            for p in r.sources + r.targets
            for la in terminal_layers(r, p)
        ],
        conflict,
    )
    via_terminal_index = PrimitiveIndex(
        [(r, ("track", 0, p, p)) for r in requests for p in r.sources + r.targets], conflict
    )

    def emit(event):
        events.append(event)
        if on_event:
            on_event(event)

    def check():
        if time.monotonic() >= deadline:
            raise SearchTimeout

    def clear(r, la, a, b):
        check()
        edge = ("track", la, a, b)
        return not terminal_index.collides(r, edge) and static_clear(r, la, a, b)

    def via(r, p):
        check()
        edge = ("via", None, p, p)
        return not via_terminal_index.collides(r, edge) and static_via_clear(r, p)

    try:
        options = {}
        for r in requests:
            for side, (points, opposite) in enumerate(
                ((r.sources, r.targets), (r.targets, r.sources))
            ):
                candidates = []
                for surface in sorted({la for p in points for la in terminal_layers(r, p)}):
                    source = [p for p in points if surface in terminal_layers(r, p)]
                    # Existing inner-layer copper needs no new escape via.
                    if surface != 0:
                        for p in source:
                            candidates.append(
                                Portal(
                                    r,
                                    side,
                                    [(*p, surface)],
                                    (surface,),
                                    False,
                                    0.05 * min(math.dist(p, q) for q in opposite),
                                )
                            )
                    found, expanded = escape_frontier(
                        source,
                        opposite,
                        bounds,
                        lambda a, b: clear(r, surface, a, b),
                        lambda p: via(r, p) and (side != 0 or first_via_allowed(r, p)),
                        pitch,
                        min(max_expansions, 12000),
                        port_spacing=pitch,
                        max_ports=128,
                    )
                    for p, path in found.items():
                        cost = (
                            sum(math.dist(a, b) for a, b in zip(path, path[1:]))
                            + 2
                            + 0.05 * min(math.dist(p, q) for q in opposite)
                        )
                        candidates.append(
                            Portal(
                                r,
                                side,
                                [(*q, surface) for q in path],
                                tuple(range(layers)),
                                True,
                                cost,
                            )
                        )
                    emit(
                        dict(
                            stage="portal_enumeration",
                            request=r.name,
                            side=side,
                            surface=surface,
                            expanded=expanded,
                            count=len(found),
                        )
                    )
                options[f"{r.name}/{side}"] = candidates

        def clash(a, b):
            check()
            return portal_conflict(a, b)

        selection = select_joint(options, clash, max_states=20000)
        # Shortest escape prefixes are not the only possible access geometry.
        # For a two-terminal bottleneck, reserve separated via lands first, then
        # regenerate surface paths in both orders with exact foreign-copper tests.
        for cluster in selection.clusters:
            keys = cluster["terminals"]
            if cluster["selected_count"] == len(keys) or len(keys) != 2:
                continue
            ka, kb = keys
            pairs = []
            for a in options[ka]:
                for b in options[kb]:
                    check()
                    if a.request.net == b.request.net or not a.via or not b.via:
                        continue
                    if a.path[-1][2] != b.path[-1][2]:
                        continue
                    if conflict(
                        a.request,
                        ("via", None, a.point, a.point),
                        b.request,
                        ("via", None, b.point, b.point),
                    ):
                        continue
                    pairs.append((a.cost + b.cost, a, b))
            pairs.sort(key=lambda x: x[0])
            repaired = False
            for trial, (_, a, b) in enumerate(pairs[:64]):
                for first, second in ((a, b), (b, a)):
                    check()
                    other_via = ("via", None, second.point, second.point)
                    surface = first.path[-1][2]

                    def first_clear(x, y):
                        return not conflict(
                            first.request, ("track", surface, x, y), second.request, other_via
                        ) and clear(first.request, surface, x, y)

                    ps = first.request.sources if first.side == 0 else first.request.targets
                    ps = [p for p in ps if surface in terminal_layers(first.request, p)]
                    f = route(
                        ps,
                        [first.point],
                        bounds,
                        first_clear,
                        pitch=pitch,
                        max_expansions=min(5000, max_expansions),
                    )
                    if not f.path:
                        continue
                    fp = Portal(
                        first.request,
                        first.side,
                        [(*p, surface) for p in f.path],
                        first.layers,
                        True,
                        first.cost,
                    )

                    def second_clear(x, y):
                        edge = ("track", surface, x, y)
                        return not any(
                            conflict(second.request, edge, fp.request, copper)
                            for copper in fp.copper
                        ) and clear(second.request, surface, x, y)

                    ps = second.request.sources if second.side == 0 else second.request.targets
                    ps = [p for p in ps if surface in terminal_layers(second.request, p)]
                    z = route(
                        ps,
                        [second.point],
                        bounds,
                        second_clear,
                        pitch=pitch,
                        max_expansions=min(5000, max_expansions),
                    )
                    if not z.path:
                        continue
                    zp = Portal(
                        second.request,
                        second.side,
                        [(*p, surface) for p in z.path],
                        second.layers,
                        True,
                        second.cost,
                    )
                    if portal_conflict(fp, zp):
                        continue
                    for option in (fp, zp):
                        options[f"{option.request.name}/{option.side}"].append(option)
                    repaired = True
                    emit(
                        dict(
                            stage="portal_prefix_repair",
                            terminals=keys,
                            pairs=len(pairs),
                            trial=trial,
                            choices=[
                                dict(request=p.request.name, side=p.side, path=p.path)
                                for p in (fp, zp)
                            ],
                        )
                    )
                    break
                if repaired:
                    break
            if not repaired:
                emit(
                    dict(
                        stage="portal_prefix_repair_failed",
                        terminals=keys,
                        pairs=len(pairs),
                        tried=min(64, len(pairs)),
                    )
                )
        selection = select_joint(options, clash, max_states=20000)
        emit(
            dict(
                stage="portal_assignment",
                **selection.report(),
                choices={
                    k: dict(
                        point=options[k][i].point, path=options[k][i].path, via=options[k][i].via
                    )
                    for k, i in selection.selected.items()
                },
            )
        )
        if not selection.complete:
            return RegionalResult("portal_assignment_incomplete", {}, events)
        chosen = {k: options[k][i] for k, i in selection.selected.items()}
        body_requests = []
        for r in requests:
            a, z = chosen[f"{r.name}/0"], chosen[f"{r.name}/1"]
            body_requests.append(Request(r.name, r.net, [a.point], [z.point], r.width, r.clearance))
        reservation_index = PrimitiveIndex(
            [(p.request, copper) for p in chosen.values() for copper in p.copper], conflict
        )

        def reserved(r, primitive):
            return reservation_index.collides(r, primitive)

        body_byname = {r.name: r for r in body_requests}

        @lru_cache(maxsize=400000)
        def cached_body_clear(name, la, a, b):
            r = body_byname[name]
            return not reserved(r, ("track", la, a, b)) and clear(r, la, a, b)

        @lru_cache(maxsize=40000)
        def cached_body_via(name, p):
            r = body_byname[name]
            return not reserved(r, ("via", None, p, p)) and via(r, p)

        def body_clear(r, la, a, b):
            check()
            a, b = sorted((tuple(a), tuple(b)))
            return cached_body_clear(r.name, la, a, b)

        def body_via(r, p):
            check()
            return cached_body_via(r.name, tuple(p))

        def body_layers(r, p):
            a, z = chosen[f"{r.name}/0"], chosen[f"{r.name}/1"]
            if a.point == z.point:
                return sorted(set(a.layers) & set(z.layers))
            return a.layers if p == a.point else z.layers

        result = solve_joint_region(
            body_requests,
            bounds,
            body_clear,
            body_via,
            pitch=pitch,
            max_orders=max_orders,
            max_expansions=max_expansions,
            layers=layers,
            terminal_layers=body_layers,
            on_event=on_event,
            max_seconds=max(0.000001, deadline - time.monotonic()),
            route_seconds=route_seconds,
        )
        emit(
            dict(
                stage="portal_cache",
                tracks=cached_body_clear.cache_info()._asdict(),
                vias=cached_body_via.cache_info()._asdict(),
            )
        )
        attempts = events + result.attempts
        if result.status != "routed":
            return RegionalResult(result.status, {}, attempts)
        paths = {}
        via_pitch = active_geometry().same_net_via_pitch  # distinct drills (legacy 0.501)
        for r in requests:
            path = (
                chosen[f"{r.name}/0"].path
                + result.paths[r.name]
                + list(reversed(chosen[f"{r.name}/1"].path))
            )
            paths[r.name] = [p for i, p in enumerate(path) if not i or p != path[i - 1]]
            holes = [a for kind, la, a, b in primitives(paths[r.name]) if kind == "via"]
            # Count physical barrels, consistent with native deduplicating writeback.
            holes = list(dict.fromkeys(tuple(p) for p in holes))
            emit(
                dict(
                    stage="composed_vias",
                    request=r.name,
                    count=len(holes),
                    limit=max_total_vias,
                    holes=holes,
                    path=paths[r.name],
                )
            )
            if (
                len(holes) > max_total_vias
                or (holes and not first_via_allowed(r, holes[0]))
                or any(
                    1e-8 < math.dist(a, b) < via_pitch - 1e-9
                    for i, a in enumerate(holes)
                    for b in holes[i + 1 :]
                )
            ):
                return RegionalResult("portal_transition_rejected", {}, events + result.attempts)
        if conflicts(requests, paths):
            return RegionalResult("portal_composition_conflict", {}, events + result.attempts)
        # Each source/target prefix is original, exact-oracle-qualified geometry.
        return RegionalResult("routed", paths, events + result.attempts)
    except SearchTimeout:
        return RegionalResult("time_budget", {}, events)
