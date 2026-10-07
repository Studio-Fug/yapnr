"""Plan one declared fanout: lattice, obstacles, tasks, assignment, emitted copper.

:func:`plan` is pure (no pcbnew, no numpy): it reads the placed graph, the routing
rules and the compiled ``fanouts`` entry, and returns the ``fanout.json`` artifact
(a dict with deterministic content). It depends only on the fanned-out part (its
pads and pose), the rules, the layer plan and fixed copper, never on where other
parts sit, so the same plan serves placement (bottom sites) and every routing
round; :func:`cached_plan` keeps it per process by its ``inputs_sha256``.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import math
from collections import Counter
from typing import Dict, Iterable, List, Optional, Sequence

from .assign import Assigner, Task
from .geom import Land, Pose, compass, rect_polygon
from .lattice import PadLand, infer
from .sites import Model, Obstacles
from .spec import FanoutError, check, class_for

SCHEMA = "yapnr-fanout-v1"
_CACHE: Dict[str, Dict] = {}


def _matches(name: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


def plan_layers(spec: Dict, grid_layers: Sequence[str], side: str) -> List[str]:
    """The plan's copper layers: the part's surface first, then its escape layers."""
    surface = grid_layers[-1] if side == "bottom" else grid_layers[0]
    escape = spec.get("escape_layers")
    if escape is None:
        escape = [la for la in grid_layers if la != surface]
    return [surface] + [la for la in escape if la != surface]


def classify(graph, rules: Dict):
    """``(grid layers, drop nets, signal nets)`` as :func:`pnr.route.detail.router.
    route_board` sees them: the declared stack's routing layers (else the legacy
    heuristic), the plane nets whose surface pads drop a via (a declared stack
    only) and the grid-routed signal nets (two pads or more, no plane, not deferred
    to the native electrical flow)."""
    from pnr.stack import assess

    stack, _warnings = assess(rules, getattr(graph, "stack", None))
    planes = {
        n
        for nc in rules.get("net_classes", [])
        if nc.get("plane_layer")
        for n in nc.get("nets", [])
    }
    if stack is None:
        many = int(rules.get("layers", 2)) >= 4 and planes
        layers = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"] if many else ["F.Cu", "B.Cu"]
        drops = set()
    else:
        layers = list(stack.grid_layers)
        planes |= set(stack.plane_nets)
        drops = {
            pad.net
            for comp in graph.components
            for pad in comp.pads
            if pad.net in stack.plane_nets and not pad.through_hole
        }
    signals = {n.name for n in graph.nets if n.name not in planes and n.degree >= 2}
    if rules.get("electrical_fab"):
        from pnr.electrical import net_policy

        signals -= {n for n in signals if net_policy(n, rules)["mode"] in ("power", "pair")}
    return layers, drops, signals


def _inputs(graph, rules, spec, layers, plane_nets, signal_nets, fixed_copper):
    comp = graph.component(spec["ref"])
    keep = {
        k: rules.get(k)
        for k in (
            "fab",
            "net_classes",
            "copper_keepouts",
            "mounting_holes",
            "electrical_fab",
            "electrical_nets",
            "terminal_width_intents",
        )
    }
    outline = (
        [list(p) for p in graph.outline.polygon]
        if graph.outline and graph.outline.polygon
        else None
    )
    size = (graph.outline.width, graph.outline.height) if graph.outline else None
    keepout_refs = sorted(
        {k.get("ref") for k in rules.get("copper_keepouts") or [] if k.get("ref")}
    )
    poses = {
        ref: [list(graph.component(ref).pos), graph.component(ref).rot, graph.component(ref).side]
        for ref in keepout_refs
        if ref in graph.refs
    }
    sites = (spec.get("bottom_sites") or {}).get("parts") or []
    from pnr.fixed_block import footprint_keepouts

    # The footprints' own rule areas at their poses; only when there are any, so the
    # digest of a board without them is unchanged.
    extra = {}
    owned = footprint_keepouts(graph)
    if owned:
        extra["footprint_keepouts"] = owned
    return dict(
        **extra,
        schema=SCHEMA,
        spec=spec,
        site_parts=[
            [ref, graph.component(ref).side, list(graph.component(ref).courtyard)]
            + [
                [p.name, p.net, list(p.offset), list(p.size), p.land_corner]
                for p in graph.component(ref).pads
            ]
            for ref in sites
            if ref in graph.refs
        ],
        part=dict(
            ref=comp.ref,
            pos=list(comp.pos),
            rot=comp.rot,
            side=comp.side,
            pads=[
                [p.name, p.net, list(p.offset), list(p.size), p.land_corner, p.through_hole]
                + [list(p.drill_size)]
                for p in comp.pads
            ],
        ),
        rules=keep,
        layers=list(layers),
        plane_nets=sorted(plane_nets),
        signal_nets=sorted(signal_nets),
        fixed=fixed_copper or None,
        outline=outline,
        size=list(size) if size else None,
        keepout_poses=poses,
    )


def inputs_sha256(*args) -> str:
    text = json.dumps(_inputs(*args), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode()).hexdigest()


def cached_plan(graph, rules, spec, *, grid_layers, plane_nets, signal_nets, fixed_copper=None):
    """:func:`plan`, memoized per process by its inputs' digest."""
    comp = graph.component(spec["ref"])
    layers = plan_layers(spec, grid_layers, comp.side)
    key = inputs_sha256(graph, rules, spec, layers, plane_nets, signal_nets, fixed_copper)
    hit = _CACHE.get(key)
    if hit is None:
        hit = _CACHE[key] = plan(
            graph,
            rules,
            spec,
            grid_layers=grid_layers,
            plane_nets=plane_nets,
            signal_nets=signal_nets,
            fixed_copper=fixed_copper,
            digest=key,
        )
    return hit


def _obstacles(graph, rules, spec, comp, pose, layers, fixed_copper):
    from pnr.graph import footprint_point

    obs = Obstacles()
    lidx = {name: i for i, name in enumerate(layers)}
    for pad in comp.pads:
        if pad.through_hole:
            if max(pad.drill_size) > 0:
                obs.vias.append((pad.net, tuple(pad.offset), max(pad.size), max(pad.drill_size)))
            continue
        w, h = pad.size
        corner = pad.land_corner if pad.land_corner is not None else 0.0
        obs.lands.append(Land(pad.offset, w / 2, h / 2, corner, pad.net, pad.name))
    tracks, vias, polygons = fixed_items(fixed_copper)
    for net, layer, a, b, width in tracks:
        if layer in lidx:
            obs.tracks.append(
                (net, lidx[layer], pose.to_local(tuple(a)), pose.to_local(tuple(b)), float(width))
            )
    for poly in polygons:
        if poly.get("kind") == "rule_area":
            # A block's rule area (pnr.fixed_copper exports its copper ``layers``): its
            # flags bar every net's tracks on those layers and, through vias spanning
            # them all, its vias wherever it has a layer, as the router reads it
            # (route.detail.fixed._reserve_polygons). No layer: it bars nothing.
            names = list(poly.get("layers") or ([poly["layer"]] if poly.get("layer") else []))
            layer_set = frozenset(lidx[n] for n in names if n in lidx)
            outline = [pose.to_local(tuple(p)) for p in poly["outline"]]
            bars_tracks = bool(poly.get("tracks")) and bool(layer_set)
            bars_vias = bool(poly.get("vias")) and bool(names)
            if bars_tracks or bars_vias:
                obs.areas.append(
                    (
                        "block-area:%d" % len(obs.areas),
                        outline,
                        layer_set if bars_tracks else frozenset(),
                        bars_vias,
                        frozenset(),
                    )
                )
            continue
        # A fixed block's pads and solid zones (fixed.json schema 2): other nets'
        # copper keeps clear; on the surface a ball of its net is joined by it.
        if poly.get("layer") not in lidx:
            continue
        obs.zones.append(
            (
                poly.get("net", ""),
                [pose.to_local(tuple(p)) for p in poly["outline"]],
                frozenset({lidx[poly["layer"]]}),
            )
        )
    for net, xy, diameter, drill in vias:
        obs.vias.append((net, pose.to_local(tuple(xy)), diameter, drill))
    from pnr.fixed_block import copper_keepouts

    # The declared keepouts, then the footprints' own rule areas (pnr.ingest).
    for k, spec_k in enumerate(copper_keepouts(graph, rules)):
        name = "keepout:" + str(spec_k.get("name") or k)
        if spec_k.get("ref") and spec_k.get("rect_mm"):
            if spec_k["ref"] not in graph.refs:
                continue
            owner = graph.component(spec_k["ref"])
            x0, y0, x1, y1 = spec_k["rect_mm"]
            poly = [
                pose.to_local(footprint_point(owner, x, y))
                for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))
            ]
        elif spec_k.get("polygon"):
            poly = [pose.to_local(tuple(p)) for p in spec_k["polygon"]]
        else:
            continue
        # A v1 keepout (pnr.fixed_block) bars its ``items`` on its ``layers`` for every
        # net outside ``allowed_nets`` (the allow lists resolved against the netlist,
        # rules.json); a v0 one bars tracks and vias on every layer for every net.
        names = spec_k.get("layers")
        layer_set = None if not names else frozenset(lidx[n] for n in names if n in lidx)
        items = spec_k.get("items")
        if items is not None and "tracks" not in items:
            layer_set = frozenset()
        bars_vias = items is None or "vias" in items
        allow = frozenset(
            spec_k["allowed_nets"] if "allowed_nets" in spec_k else spec_k.get("allow_nets") or ()
        )
        obs.areas.append((name, poly, layer_set, bars_vias, allow))
    for hole in rules.get("mounting_holes") or []:
        d = float(hole["clearance_diameter_mm"])
        obs.vias.append(("", pose.to_local(tuple(hole["at"])), d, float(hole.get("drill_mm", d))))
    for r in spec.get("reserved") or []:
        if r["frame"] == "board":
            poly = [pose.to_local(tuple(p)) for p in r["polygon"]]
        else:
            poly = [pose.to_local(footprint_point(comp, x, y)) for x, y in r["polygon"]]
        layer_set = (
            None if "*" in r["layers"] else frozenset(lidx[n] for n in r["layers"] if n in lidx)
        )
        obs.areas.append(("reserved:" + r["name"], poly, layer_set, True, frozenset()))
    if graph.outline and graph.outline.polygon:
        obs.outline = [pose.to_local(tuple(p)) for p in graph.outline.polygon]
    elif graph.outline:
        obs.outline = [
            pose.to_local(p)
            for p in rect_polygon((0, 0, graph.outline.width, graph.outline.height))
        ]
    return obs


def _arc_chords(net, layer, start, mid, end, width, eps=0.001):
    """An arc as chords whose sagitta is at most ``eps``, each ``2 eps`` wider: every
    point of the arc's copper lies inside them (fixed.json schema 2 ``arcs``)."""
    ax, ay = start
    bx, by = mid
    cx, cy = end
    d = 2.0 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:
        return [[net, layer, list(start), list(end), width]]
    a2, b2, c2 = ax * ax + ay * ay, bx * bx + by * by, cx * cx + cy * cy
    ux = (a2 * (by - cy) + b2 * (cy - ay) + c2 * (ay - by)) / d
    uy = (a2 * (cx - bx) + b2 * (ax - cx) + c2 * (bx - ax)) / d
    r = math.hypot(ax - ux, ay - uy)
    a0, am, a1 = (math.atan2(y - uy, x - ux) for x, y in (start, mid, end))

    def ccw(a, b):
        return (b - a) % (2 * math.pi)

    sweep = ccw(a0, a1) if ccw(a0, am) <= ccw(a0, a1) else -ccw(a1, a0)
    step = 2 * math.acos(max(-1.0, 1 - eps / max(r, eps)))
    n = max(1, int(math.ceil(abs(sweep) / step)))
    pts = [
        (ux + r * math.cos(a0 + sweep * k / n), uy + r * math.sin(a0 + sweep * k / n))
        for k in range(n + 1)
    ]
    return [[net, layer, list(p), list(q), width + 2 * eps] for p, q in zip(pts, pts[1:])]


def fixed_items(copper):
    """``(tracks, vias, polygons)`` of fixed copper, schema 1 or 2: top-level tracks,
    arcs (as chords) and vias, and every block's tracks, arcs, vias and polygons
    (pads, solid zones, rule areas). Vias are ``(net, xy, diameter, drill)``."""
    tracks, vias, polygons = [], [], []
    for source in [copper or {}] + list((copper or {}).get("blocks") or []):
        tracks += [list(t) for t in source.get("tracks", [])]
        for arc in source.get("arcs") or []:
            tracks += _arc_chords(*arc)
        vias += [
            (v.get("net", ""), v["xy"], v["diameter_mm"], v["drill_mm"])
            for v in source.get("vias", [])
        ]
        polygons += list(source.get("polygons") or [])
    return tracks, vias, polygons


def _exits(model, pose, layers, spec):
    forbidden = spec.get("forbidden_exits") or {}
    out = {}
    a0, b0, a1, b1 = model.bounds
    for li, layer in enumerate(layers):
        banned = set(forbidden.get("*", ())) | set(forbidden.get(layer, ()))
        nodes = {}
        for a in range(a0, a1 + 1):
            for b in range(b0, b1 + 1):
                o = model.outward((a, b))
                if o is None:
                    continue
                if compass(pose.direction_to_board(o)) in banned:
                    continue
                nodes[(a, b)] = o
        out[li] = nodes
    return out


def plan(
    graph,
    rules: Dict,
    spec: Dict,
    *,
    grid_layers: Sequence[str],
    plane_nets: Iterable[str],
    signal_nets: Iterable[str],
    fixed_copper: Optional[Dict] = None,
    digest: Optional[str] = None,
) -> Dict:
    """The fanout of ``spec['ref']`` (see the module docstring); raises
    :class:`FanoutError` for an input it cannot honour."""
    from pnr.fab_profile import geometry
    from pnr.pad_entry import fanout_neck, terminal_required_width

    plane_nets, signal_nets = set(plane_nets), set(signal_nets)
    try:
        comp = graph.component(spec["ref"])
    except KeyError:
        raise FanoutError("fanout %s: part %s is not on the board" % (spec["name"], spec["ref"]))
    layers = plan_layers(spec, grid_layers, comp.side)
    spec = json.loads(json.dumps(spec))  # check() may fill the default class
    warnings = check(spec, rules, list(grid_layers))
    digest = digest or inputs_sha256(
        graph, rules, spec, layers, plane_nets, signal_nets, fixed_copper
    )
    pose = Pose(comp.pos, comp.rot)
    lands = [
        PadLand(
            p.name,
            p.net,
            tuple(p.offset),
            tuple(p.size),
            p.land_corner,
        )
        for p in comp.pads
        if not p.through_hole and p.size[0] > 0 and p.size[1] > 0
    ]
    try:
        lat = infer(lands)
    except ValueError as error:
        raise FanoutError("fanout %s: %s" % (spec["name"], error)) from None
    g = geometry(rules)
    fab = rules.get("fab") or {}
    clearance = float(fab.get("clearance_mm", 0.13))
    # Each net keeps its class's clearance where larger (route_board's
    # _net_clearances): two nets keep the larger of theirs, as KiCad's DRC judges.
    net_clearance = {}
    for c in rules.get("net_classes", []):
        if c.get("clearance_mm"):
            for n in c.get("nets", []):
                net_clearance[n] = max(net_clearance.get(n, 0.0), float(c["clearance_mm"]))
    if rules.get("dru_routing") and rules.get("dru"):
        # board.dru_routing: a custom rule's small pair clearance raises one side's, as
        # route_board does for the maze (a CLK class 0.25 mm from unclassed nets).
        from pnr.route.detail.dru_apply import raised_clearances

        for n, value in raised_clearances(rules["dru"]).items():
            net_clearance[n] = max(net_clearance.get(n, 0.0), value)
    # The pads this fanout covers and their tasks.
    fanned = {}
    skipped = {}
    for (col, row), land in sorted(lat.balls.items()):
        name = land.name
        if not _matches(name, spec["pads"]) or _matches(name, spec["skip_pads"]):
            skipped[name] = "skip_pads" if _matches(name, spec["skip_pads"]) else "not in pads"
            continue
        if not land.net:
            skipped[name] = "no net"
            continue
        if land.net in plane_nets:
            kind = "drop"
        elif land.net in signal_nets:
            # A drop net (``drop_nets``) takes a via beside the ball, as a plane ball
            # does, instead of an exit: the router continues from the via.
            kind = "drop" if _matches(land.net, spec.get("drop_nets") or ()) else "signal"
        else:
            skipped[name] = "net not routed by the grid (single pad, deferred or held out)"
            continue
        fanned[name] = (kind, col, row, land)
    widths = {}
    necked = {}
    for name, (kind, col, row, land) in fanned.items():
        w = terminal_required_width(comp.ref, name, land.net, rules, neck=False)
        if kind == "signal":
            # A declared neck narrows a signal's escape, never below the net's own
            # class or current minimum unless its class is authorized (fanout_neck).
            narrow = fanout_neck(comp.ref, name, land.net, rules, required=w, spec=spec)
            if narrow is not None:
                necked[name] = (round(float(w), 6), round(float(narrow), 6))
                w = narrow
        widths[name] = round(float(w), 6)
    obs = _obstacles(graph, rules, spec, comp, pose, layers, fixed_copper)
    model = Model(
        lat,
        layers,
        obs,
        clearance=clearance,
        net_clearance=net_clearance,
        clearances=sorted(
            {
                max(clearance, net_clearance.get(land.net, 0.0))
                for _k, (_kind, _c, _r, land) in fanned.items()
            }
        ),
        via_to_pad=g.via_to_smd_pad,
        hole_to_hole=g.hole_to_hole,
        hole_clearance=g.hole_clearance,
        edge_clearance=g.edge_clearance,
        hole_to_edge=g.hole_to_edge,
        via_classes=spec["via_classes"],
        widths=sorted(set(widths.values())) or [float(fab.get("track_width_mm", 0.15))],
        in_pad=g.in_pad,
    )
    exits = _exits(model, pose, layers, spec)
    escape_idx = list(range(1, len(layers)))
    tasks = []
    joined = {}
    for name, (kind, col, row, land) in sorted(fanned.items()):
        ring = lat.ring(col, row)
        cls = class_for(spec, land.net)
        k = spec["via_classes"].index(cls) if cls is not None else -1
        exit_layers = tuple(escape_idx)
        via_layers = tuple(escape_idx)
        named = spec["ring_layers"].get(str(ring))
        if named is not None:
            allowed = {layers.index(n) for n in named if n in layers}
            exit_layers = tuple(sorted(allowed))
            via_layers = tuple(sorted(allowed - {0}))
        elif ring < spec["surface_rings"]:
            exit_layers = (0,) + exit_layers
        if k < 0:
            via_layers = ()
        elif cls.get("layers") is not None:
            keep = {layers.index(n) for n in cls["layers"] if n in layers}
            exit_layers = tuple(la for la in exit_layers if la in keep)
            via_layers = tuple(la for la in via_layers if la in keep)
        task = Task(
            pad=name,
            net=land.net,
            kind=kind,
            node=lat.node_of(col, row),
            ring=ring,
            width=widths[name],
            via_class=max(k, 0),
            exit_layers=exit_layers if kind == "signal" else (),
            via_layers=via_layers if kind == "signal" else (),
            priority=0 if kind == "signal" else 1,
        )
        if k < 0 and (kind == "drop" or 0 not in exit_layers):
            task.failed = "no via class for net %s" % land.net
        if (
            kind == "drop"
            and land.net in plane_nets
            and model.joined(
                Land(land.centre, land.size[0] / 2, land.size[1] / 2, land.corner or 0.0, land.net)
            )
        ):
            joined[name] = land.net  # its fixed copper is its connection: no drop
            continue
        tasks.append(task)
    assigner = Assigner(
        model,
        [t for t in tasks if not t.failed],
        exits=exits,
        variant=spec.get("variant", 0),
    )
    assigner.run()
    for name, net in joined.items():
        skipped[name] = ("fixed", net)
    result = _emit(spec, comp, pose, lat, model, tasks, skipped, warnings, assigner, digest, layers)
    if necked:
        # Declared necks (pad: [width without the neck, neck width]): the escapes the
        # validator accepts at the neck width (pnr.pad_entry.fanout_neck).
        result["diagnostics"]["necks"] = {k: list(necked[k]) for k in sorted(necked)}
    if spec.get("bottom_sites"):
        from .bottom import sites

        result["bottom"] = sites(graph, spec, result, lat, pose, rules, fixed=obs)
    return result


def _emit(spec, comp, pose, lat, model, tasks, skipped, warnings, assigner, digest, layers):
    tracks, vias, terminals = [], [], {}
    via_size = {k: (c["diameter_mm"], c["drill_mm"]) for k, c in enumerate(spec["via_classes"])}

    def xy(p):
        q = pose.to_board(p)
        return [round(q[0], 6), round(q[1], 6)]

    for t in tasks:
        land = lat.balls[(t.node[0] // 2, t.node[1] // 2)]
        row = dict(net=t.net, kind=t.kind, ring=t.ring, width_mm=t.width, pad_xy=xy(land.centre))
        if t.path is None:
            row.update(kind="failed", intended=t.kind, reason=t.failed or "unresolved")
            terminals[t.pad] = row
            continue
        segments = []  # (layer index, a, b) in local frame
        via = None
        states = t.route
        for (la, na), (lb, nb) in zip(states, states[1:]):
            if la != lb:
                via = na
                continue
            a = land.centre if na == t.node else model.point(na)
            segments.append((la, a, model.point(nb)))
        last_layer, last = states[-1]
        exit_obj = None
        if t.kind == "drop":
            via = last
        else:
            exit_obj = t.path[-1]
            o = exit_obj
            other = (o[2][0] + _dir(o[3])[0], o[2][1] + _dir(o[3])[1])
            end = other if o[2] == last else o[2]
            segments.append((last_layer, model.point(last), model.point(end)))
        segments = _merge(segments)
        cls = spec["via_classes"][t.via_class]
        via_xy = None
        existing = False
        if via is not None:
            at = land.centre if via == t.node else model.point(via)
            d, h = via_size[t.via_class]
            via_xy = xy(at)
            # A fixed via of the same net already on the site (a block's stitching via,
            # which the site check lets a via of its net share): reuse it, do not drill
            # it again (KiCad: holes co-located).
            existing = any(
                net == t.net and math.dist(c, at) <= 1e-6 for net, c, _d, _h in model.obs.vias
            )
            if not existing:
                vias.append([t.net, via_xy[0], via_xy[1], d, h])
        out_tracks = [[t.net, layers[la], xy(a), xy(b), t.width] for la, a, b in segments]
        tracks.extend(out_tracks)
        if t.kind == "drop":
            kind = "via_in_pad" if via == t.node else "drop"
        elif via is None:
            kind = "surface"
        else:
            kind = "via_in_pad" if via == t.node else "dogbone"
        row.update(
            kind=kind,
            layer=layers[last_layer],
            segments=[[layers[la], xy(a), xy(b)] for la, a, b in segments],
            via=None if via is None else via_xy + list(via_size[t.via_class]),
            via_class=cls["name"] if via is not None else None,
            via_site=None if via is None else model.site_kind(via),
            length_mm=round(sum(math.dist(a, b) for _, a, b in segments), 4),
        )
        if existing:
            row["via_existing"] = True  # the fixed via is the connection (no new hole)
        if t.kind == "signal":
            ext = segments[-1]
            direction = pose.direction_to_board(
                (
                    (ext[2][0] - ext[1][0]) / max(1e-12, math.dist(ext[1], ext[2])),
                    (ext[2][1] - ext[1][1]) / max(1e-12, math.dist(ext[1], ext[2])),
                )
            )
            row.update(exit=xy(ext[2]), outward=[round(direction[0], 6), round(direction[1], 6)])
        terminals[t.pad] = row
    for name, reason in sorted(skipped.items()):
        if isinstance(reason, tuple):  # a plane ball its fixed copper already joins
            land = next(b for b in lat.balls.values() if b.name == name)
            terminals[name] = dict(
                kind="fixed",
                net=reason[1],
                pad_xy=xy(land.centre),
                reason="joined by fixed copper of its net (no drop)",
            )
            continue
        terminals[name] = dict(kind="skipped", reason=reason)
    counts = Counter()
    for name, row in terminals.items():
        counts[row["kind"]] += 1
    rings = {}
    for t in tasks:
        key = "%s_ring%d" % (t.kind, t.ring)
        rings.setdefault(key, [0, 0])
        rings[key][1] += 1
        if t.path is not None:
            rings[key][0] += 1
    sites = Counter(
        (r.get("via_class"), r.get("via_site")) for r in terminals.values() if r.get("via")
    )
    reused = sum(1 for r in terminals.values() if r.get("via_existing"))
    signals = [t for t in tasks if t.kind == "signal"]
    drops = [t for t in tasks if t.kind == "drop"]
    return dict(
        schema=SCHEMA,
        name=spec["name"],
        ref=comp.ref,
        inputs_sha256=digest,
        layers=list(layers),
        lock=bool(spec.get("lock", True)),
        lattice=lat.summary(),
        copper=dict(frame="engine-mm-y-up", tracks=tracks, vias=vias),
        terminals={k: terminals[k] for k in sorted(terminals)},
        diagnostics=dict(
            signals_escaped=sum(t.path is not None for t in signals),
            signals=len(signals),
            drops_placed=sum(t.path is not None for t in drops),
            drops=len(drops),
            kinds=dict(sorted(counts.items())),
            per_ring={k: rings[k] for k in sorted(rings)},
            via_sites={"%s@%s" % k: n for k, n in sorted(sites.items(), key=str)},
            failed={
                name: row["reason"] for name, row in terminals.items() if row["kind"] == "failed"
            },
            search=dict(
                rounds=assigner.report["rounds"],
                conflicts_per_round=assigner.report["conflicts_per_round"],
                legalized=assigner.report["legalized"],
                repaired=assigner.report["repaired"],
            ),
            warnings=warnings,
            # Only with a reused fixed via, so other plans keep their bytes.
            **({"vias_reused": reused} if reused else {}),
        ),
    )


def _dir(d):
    from .sites import DIRS

    return DIRS[d]


def _merge(segments):
    """Join consecutive collinear segments of one layer."""
    out = []
    for la, a, b in segments:
        if out:
            pl, pa, pb = out[-1]
            if pl == la and math.dist(pb, a) < 1e-9:
                ux, uy = pb[0] - pa[0], pb[1] - pa[1]
                vx, vy = b[0] - a[0], b[1] - a[1]
                if (
                    abs(ux * vy - uy * vx)
                    < 1e-9 * max(1e-9, math.hypot(ux, uy) * math.hypot(vx, vy))
                    and (ux * vx + uy * vy) > 0
                ):
                    out[-1] = (la, pa, b)
                    continue
        out.append((la, a, b))
    return out
