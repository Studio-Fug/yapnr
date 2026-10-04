"""Net class clearances after the maze route (``board.class_clearance: maze``).

The halo router (:func:`pnr.route.detail.maze.route`) keeps nets apart by reserved
cells. With ``class_clearance: maze`` those reservations are sized per net from its
class clearance (route_board: track halos, via keep-outs; RouteGrid class tables for
the static copper), which covers every pair of nets on a grid no coarser than one
signal track and clearance. The checks here make the guarantee exact:

* :func:`repair` judges every pair of routed nets with the exact pairwise rule of
  :mod:`.exact_route` (:class:`~.exact_route.Separation`: half widths plus the
  larger of the two class clearances, via drill spacing; exact centre distances on
  the grid). Two nets too close are a conflict: the fewest nets that leave none
  are ripped and routed again, one at a time, strictly around every other net's
  exact zone; a net that cannot be routed whole keeps the branches that fit, and
  the rest stays open (an open is reported, a violation would not be).
* :func:`copper_audit` measures, in mm, all the copper route_board emits (maze
  routes, escapes, fanouts, block ports, at their own widths and via sizes)
  against other nets' copper and every pad (with its own clearance) at the larger
  of the two class clearances, and lists every shortfall. It changes nothing: it
  is the report that says whether the tables and the repair held.

Both run on the grid's geometry only (no pcbnew); route_board puts their reports in
the escape diagnostics (``class_clearance``) the run's PnR report keeps.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Dict, List

from .grid import Cell


def route_of(rn):
    """The :class:`~.maze._Route` behind a :class:`~.maze.RoutedNet` (its cells,
    its same-layer segments and a layer move at each via column), as the exact
    zones read it."""
    from .maze import _Route

    edges = [(Cell(la, *p), Cell(la, *q)) for la, p, q in rn.segments]
    layers = defaultdict(set)
    for c in rn.cells:
        layers[(c.i, c.j)].add(c.layer)
    for i, j in rn.vias:
        stack = sorted(layers.get((i, j), ()))
        edges.extend((Cell(a, i, j), Cell(b, i, j)) for a, b in zip(stack, stack[1:]))
    return _Route(list(rn.cells), edges)


def _cover(pairs, size):
    """The nets to rip so that no pair is left: the net in most pairs first (fewer
    routed cells, then the name, break ties)."""
    pending = {tuple(sorted(p)) for p in pairs}
    ripped = []
    while pending:
        degree = defaultdict(int)
        for a, b in pending:
            degree[a] += 1
            degree[b] += 1
        net = min(degree, key=lambda n: (-degree[n], size.get(n, 0), n))
        ripped.append(net)
        pending = {p for p in pending if net not in p}
    return ripped


def repair(grid, net_access, result, *, via_cost, session=None, also=()):
    """Rip and reroute the nets of ``result`` (a :class:`~.maze.RouteResult`, changed
    in place) that the exact pairwise rule finds too close to another (module doc),
    and the nets ``also`` names (too close to static copper: :func:`static_offenders`).
    Returns the report: ``pairs`` (the conflicting pairs, at most 20), ``static``
    (``also``), ``ripped``, ``rerouted`` (whole again), ``partial`` (branches kept,
    connections open) and ``open`` (nothing kept). A grid outside the dense model
    (the reference kernel, a via model) is checked but not rerouted: its offenders
    are ripped to open."""
    from .dense_maze import DenseSession, build_exact_field
    from .exact_route import Occupancy, Separation, Zone, supported
    from .maze import _Route, _route_one, _to_geometry, remaining_connections

    class AtTheRule(Separation):
        # Two nets exactly at their clearance are legal (KiCad's DRC, and the halo
        # grid's pitch of one track plus clearance): only a centre distance short of
        # the rule by more than 1 nm is too close, here and in the reroute.
        MARGIN = -1e-6

    nets = sorted(n for n, cells in net_access.items() if len(cells) >= 2)
    routes = {
        n: route_of(result.nets[n]) for n in nets if n in result.nets and result.nets[n].cells
    }
    report = {"pairs": [], "static": [], "ripped": [], "rerouted": [], "partial": [], "open": []}
    if not routes:
        return report
    sep = AtTheRule(grid, nets)
    zones = {n: Zone(grid, sep, n, r) for n, r in routes.items()}
    occ = Occupancy(grid, sep)
    for zone in zones.values():
        occ.add(zone)
    pairs = set()
    for name, zone in sorted(zones.items()):
        if occ.conflicts(zone):
            pairs.update(tuple(sorted((name, other))) for other in occ.crossed(zone))
    also = also if isinstance(also, dict) else {n: [] for n in also}
    static = sorted(n for n in set(also) if n in routes)
    if not pairs and not static:
        return report
    report["pairs"] = [list(p) for p in sorted(pairs)[:20]]
    report["pair_count"] = len(pairs)
    report["static"] = static
    # The static offenders first: ripping them may already part a pair.
    pairs = {p for p in pairs if not set(p) & set(static)}
    ripped = static + _cover(pairs, {n: len(r.cells) for n, r in routes.items()})
    report["ripped"] = sorted(ripped)
    committed = Occupancy(grid, sep)
    for name, zone in zones.items():
        if name not in ripped:
            committed.add(zone)
    exact = supported(grid)
    if exact:
        session = session or DenseSession(grid)
        exact = session.static is not None

    def span(net):
        cs = net_access[net]
        return (max(c.i for c in cs) - min(c.i for c in cs)) + (
            max(c.j for c in cs) - min(c.j for c in cs)
        )

    def blocked_field(net):
        track, via, step = committed.blocked(net)
        return build_exact_field(
            session.static, net, track_block=track, via_block=via, diag_block=step
        )

    for net in sorted(ripped, key=lambda n: (span(n), n)):
        forest = _Route()
        if exact:
            field = blocked_field(net)
            route = _route_one(
                grid, net_access[net], net, {}, {}, via_cost, 0.0, _session=session, _field=field
            )
            if route and not committed.conflicts(Zone(grid, sep, net, route)):
                forest = route
            else:
                # Keep what fits: the old route less its cells too close to the rest
                # (or to static copper), its pieces joined again around everything.
                bad = _bad_cells(grid, zones[net], committed) | _row_cells(
                    grid, routes[net], also.get(net) or ()
                )
                forest = _join(
                    grid,
                    net,
                    _prune(routes[net], bad),
                    net_access[net],
                    lambda trial: not committed.conflicts(Zone(grid, sep, net, trial)),
                    via_cost,
                    field,
                )
        rn = _to_geometry(forest, grid)
        rn.name = net
        rn.remaining_connections = remaining_connections(net_access[net], forest.edges)
        rn.routed = rn.remaining_connections == 0
        result.nets[net] = rn
        if forest.cells:
            committed.add(Zone(grid, sep, net, forest))
        bucket = "rerouted" if rn.routed else ("partial" if forest.edges else "open")
        report[bucket].append(net)
    result.unrouted = sorted(
        set(result.unrouted) - set(report["rerouted"]) | set(report["partial"] + report["open"])
    )
    report["model"] = "exact" if exact else "ripped (grid outside the dense model)"
    return report


def _order(c):
    return (c.layer, c.i, c.j)


def _cell_of_key(grid, key):
    plane = grid.ny * grid.nx
    layer, rest = divmod(int(key), plane)
    j, i = divmod(rest, grid.nx)
    return Cell(layer, i, j)


def _bad_cells(grid, zone, committed):
    """Cells of ``zone``'s route that lie in another committed net's zone: its track
    cells, the cells of its 45° steps' blocks, every layer of its via columns."""
    flat = committed._flat
    out = set()
    if len(zone.track_keys):
        hit = flat["cells"][zone.kind][zone.track_keys] > 0
        out.update(_cell_of_key(grid, k) for k in zone.track_keys[hit])
    if len(zone.step_keys):
        hit = flat["blocks"][zone.kind][zone.step_keys] > 0
        for k in zone.step_keys[hit]:
            c = _cell_of_key(grid, k)
            out.update(Cell(c.layer, c.i + di, c.j + dj) for di in (0, 1) for dj in (0, 1))
    if len(zone.via_keys):
        hit = flat["cells"][zone.via_kind][zone.via_keys] > 0
        for k in zone.via_keys[hit]:
            c = _cell_of_key(grid, k)
            out.update(Cell(la, c.i, c.j) for la in range(grid.nlayers))
    return out


def _row_cells(grid, route, rows):
    """Cells of ``route`` at the copper the static check found too close (``rows``
    of :func:`static_offenders`: a track's two ends on its layer, a via's column)."""
    out = set()
    index = {name: k for k, name in enumerate(grid.layers)}
    for row in rows:
        points = [row["at"], row.get("to") or row["at"]]
        if points[0] == points[1]:  # the route's via: its column
            layers = range(grid.nlayers)
        else:  # the route's track, on its layer
            layers = [index[row["layer"]]] if row.get("layer") in index else range(grid.nlayers)
        for x, y in points:
            i, j = grid.cell_of(x, y)
            out.update(Cell(la, i, j) for la in layers)
    cells = set(route.cells)
    return {c for c in out if c in cells}


def _prune(route, bad):
    """``route`` without the ``bad`` cells and the edges that touch them."""
    from .maze import _Route

    edges = [(a, b) for a, b in route.edges if a not in bad and b not in bad]
    cells = {c for c in route.cells if c not in bad}
    return _Route(sorted(cells, key=lambda c: (c.layer, c.i, c.j)), edges)


def _join(grid, net, forest, terminals, clean, via_cost, field):
    """``forest`` (pieces of a route) and the net's ``terminals`` joined by A* paths
    on ``field``, the largest piece first, each path kept only when the trial tree
    stays ``clean``. What cannot be joined stays apart (open connections)."""
    from .maze import _astar, _new_via_sites, _Route

    parent = {}

    def find(c):
        parent.setdefault(c, c)
        while parent[c] != c:
            parent[c] = parent[parent[c]]
            c = parent[c]
        return c

    for a, b in forest.edges:
        parent[find(a)] = find(b)
    for c in forest.cells:
        find(c)
    for t in terminals:
        find(t)
    pieces = {}
    for c in parent:
        pieces.setdefault(find(c), set()).add(c)
    # Pieces worth joining: those holding a terminal.
    terms = set(terminals)
    pieces = [cells for cells in pieces.values() if cells & terms]
    pieces.sort(key=lambda cells: (-len(cells & terms), -len(cells), min(map(_order, cells))))
    if not pieces:
        return _Route()
    keep = set().union(*pieces)
    loose = sorted(terms - set(forest.cells), key=_order)
    forest = _Route(
        [c for c in forest.cells if c in keep] + loose,
        [(a, b) for a, b in forest.edges if a in keep],
    )
    tree, others = set(pieces[0]), [set(p) for p in pieces[1:]]
    while others:
        targets = set().union(*others)
        path = _astar(
            grid,
            tree,
            targets,
            net,
            {},
            {},
            via_cost,
            0.0,
            drill_sites=tuple(
                grid.center_of(i, j) for i, j in _new_via_sites(grid, forest.edges, net)
            ),
            _field=field,
        )
        if not path:
            break
        trial = _Route(
            list(dict.fromkeys(list(forest.cells) + path)), forest.edges + list(zip(path, path[1:]))
        )
        if not clean(trial):
            break
        forest = trial
        tree.update(path)
        joined = [p for p in others if path[-1] in p]
        for p in joined:
            tree |= p
            others.remove(p)
    return forest


# --------------------------------------------------------------------- copper audit


def _point_rect(p, r):
    dx = max(r.left - p[0], 0.0, p[0] - r.right)
    dy = max(r.bottom - p[1], 0.0, p[1] - r.top)
    return math.hypot(dx, dy)


def _segment_rect(a, b, r):
    from pnr.writeback import _segment_distance_sq

    if any(r.left <= p[0] <= r.right and r.bottom <= p[1] <= r.top for p in (a, b)):
        return 0.0
    corners = [(r.left, r.bottom), (r.right, r.bottom), (r.right, r.top), (r.left, r.top)]
    return math.sqrt(
        min(_segment_distance_sq(a, b, corners[k], corners[(k + 1) % 4]) for k in range(4))
    )


def copper_audit(grid, tracks, vias, via_sizes=(), *, pairs=None, classes=None, limit=20):
    """Clearance shortfalls between different nets' copper, in mm: ``tracks``
    ``(net, layer name, a, b, width)`` and ``vias`` ``(net, x, y)`` (a route_board
    result's emitted copper: maze routes, escapes, fanouts, block ports) against
    each other and against the grid's pads (every layer a through via crosses).
    Two nets keep the larger of their class clearances (``grid.net_clearances``)
    and the fab's; a pad that sets its own (``grid.pad_keepaways``) keeps that where
    larger; a pad whose land is exact (``grid.smd_pads``) is judged with its corner
    radius. ``via_sizes`` gives a via's diameter where it is not the fab's
    (``[net, x, y, diameter, drill]``). ``classes`` (net -> class clearance) stands
    in for ``grid.net_clearances`` (which may hold clearances raised for routing);
    ``pairs`` (``frozenset({a, b})`` -> mm, a board's pair rules: pnr.dru_rules) adds
    pair clearances, judged out to their own distance. Returns ``{"count": n,
    "items": [...]}`` with at most ``limit`` items ``{net, other, kind, layer,
    gap_mm, need_mm, at}``. Same-net copper and drill spacing are not judged here."""
    items = _copper_items(grid, tracks, vias, via_sizes)
    rows = _shortfalls(grid, items, classes=classes, pairs=pairs)
    return {"count": len(rows), "items": rows[:limit]}


def static_offenders(grid, result, widths, tracks, vias, via_sizes=(), *, classes=None):
    """The nets of ``result`` (a RouteResult, grid geometry) whose routed copper is too
    close to other nets' static copper (``tracks``/``vias``: escapes, fanouts,
    ports, as route_board emits them; the grid's pads), by :func:`copper_audit`'s
    rule. ``widths``: net -> track width. Returns ``{net: [shortfall rows]}``."""
    route_tracks, route_vias = [], []
    plated = getattr(grid, "plated_transition", lambda *a: None)
    for net, rn in result.nets.items():
        w = widths.get(net, grid.track_width)
        for la, p, q in rn.segments:
            route_tracks.append((net, grid.layers[la], grid.center_of(*p), grid.center_of(*q), w))
        for i, j in rn.vias:
            if plated(net, i, j) is None:
                route_vias.append((net, *grid.center_of(i, j)))
    items = [it + ("route",) for it in _copper_items(grid, route_tracks, route_vias, ())]
    items += [it + ("static",) for it in _copper_items(grid, tracks, vias, via_sizes)]
    out: Dict[str, list] = {}
    for row in _shortfalls(grid, items, classes=classes, only="route"):
        out.setdefault(row["net"], []).append(row)
    return out


def _copper_items(grid, tracks, vias, via_sizes):
    """``(kind, layer index or None for every layer, net, (a, b), half width)``."""
    layer_index = {name: k for k, name in enumerate(grid.layers)}
    radius = {(n, round(x, 6), round(y, 6)): d / 2 for n, x, y, d, *_ in via_sizes}
    items = []
    for net, layer, a, b, width in tracks:
        if layer in layer_index:
            items.append(("track", layer_index[layer], net, (tuple(a), tuple(b)), width / 2))
    for net, x, y in vias:
        r = radius.get((net, round(x, 6), round(y, 6)), grid.via_radius)
        items.append(("via", None, net, ((x, y), (x, y)), r))
    return items


def _shortfalls(grid, items, *, classes=None, pairs=None, only=None):
    """Rows (``{net, other, kind, layer, gap_mm, need_mm, at}``, sorted) of every
    pair of different nets' copper ``items`` (and items against the grid's pads)
    closer than their rule. With ``only``, items carry a sixth field, a tag, and a
    pair counts only when exactly one of its items is tagged ``only`` (or the item
    is and the other is a pad): that item's net is then the row's ``net``."""
    from pnr.place.geometry import Rect
    from pnr.writeback import _segment_distance_sq

    if classes is None:
        classes = getattr(grid, "net_clearances", None) or {}
    pairs = pairs or {}
    # The farthest a net's pair rules reach (the search window of its copper).
    far = {}
    for key, d in pairs.items():
        for n in key:
            far[n] = max(far.get(n, 0.0), d)
    keepaways = getattr(grid, "pad_keepaways", None) or {}
    corners = {(la, owner, r): c for la, owner, r, c in getattr(grid, "smd_pads", ()) or ()}

    def clearance(net):
        return max(grid.clearance, classes.get(net, 0.0))

    pads = []
    for la, owner, r in grid.pad_rectangles:
        corner = corners.get((la, owner, r)) or 0.0
        core = r
        if corner > 0:  # an exact rounded land: its core rectangle less the radius
            core = Rect(r.cx, r.cy, max(0.0, r.w - 2 * corner), max(0.0, r.h - 2 * corner))
        pads.append((la, owner, r, core, corner, keepaways.get((la, owner, r))))
    reach = 2.0
    buckets = defaultdict(list)

    def boxes(x0, y0, x1, y1):
        for bx in range(math.floor(x0), math.floor(x1) + 1):
            for by in range(math.floor(y0), math.floor(y1) + 1):
                yield bx, by

    for k, item in enumerate(items):
        (a, b) = item[3]
        for key in boxes(min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])):
            buckets[key].append(("copper", k))
    for k, (_, _, r, _, _, _) in enumerate(pads):
        for key in boxes(r.left, r.bottom, r.right, r.top):
            buckets[key].append(("pad", k))
    found = {}
    judged = set()  # copper pairs (lower index, higher), each judged once
    for k, item in enumerate(items):
        kind, layer, net, (a, b), half = item[:5]
        tag = item[5] if len(item) > 5 else None
        seen = set()
        window = max(reach, far.get(net, 0.0) + 1.0)
        x0, y0 = min(a[0], b[0]) - window, min(a[1], b[1]) - window
        x1, y1 = max(a[0], b[0]) + window, max(a[1], b[1]) + window
        for key in boxes(x0, y0, x1, y1):
            for what, m in buckets.get(key, ()):
                if (what, m) in seen or (what == "copper" and m == k):
                    continue
                seen.add((what, m))
                if what == "copper":
                    other_item = items[m]
                    okind, olayer, other, (c, d), ohalf = other_item[:5]
                    if other == net:
                        continue
                    if layer is not None and olayer is not None and layer != olayer:
                        continue
                    if only is not None:
                        if tag != only:
                            continue  # a tagged item judges its pairs from its own side
                        if len(other_item) > 5 and other_item[5] == only:
                            continue  # two tagged items: not this check's pair
                    key2 = (min(k, m), max(k, m))
                    if key2 in judged:
                        continue
                    judged.add(key2)
                    gap = math.sqrt(_segment_distance_sq(a, b, c, d)) - half - ohalf
                    need = max(clearance(net), clearance(other))
                    need = max(need, pairs.get(frozenset((net, other)), 0.0))
                    pair = "-".join(sorted((kind, okind)))
                else:
                    if only is not None and tag != only:
                        continue
                    olayer, other, rect, core, corner, keep = pads[m]
                    if other == net or (layer is not None and olayer != layer):
                        continue
                    if a != b:
                        gap = _segment_rect(a, b, core) - corner - half
                    else:
                        gap = _point_rect(a, core) - corner - half
                    need = max(clearance(net), clearance(other), keep or 0.0)
                    need = max(need, pairs.get(frozenset((net, other)), 0.0))
                    pair = kind + "-pad"
                if gap < need - 1e-6:
                    where = layer if layer is not None else olayer
                    name = grid.layers[where] if where is not None else None
                    found[(min(k, m), max(k, m)) if what == "copper" else (k, what, m)] = dict(
                        net=net,
                        other=other,
                        kind=pair,
                        layer=name,
                        gap_mm=round(gap, 4),
                        need_mm=round(need, 4),
                        at=[round(v, 4) for v in a],
                        to=[round(v, 4) for v in b],
                    )
    return sorted(found.values(), key=lambda v: (v["net"], v["other"], v["kind"], v["at"]))


def summarize(report: Dict) -> List[str]:
    """One line per non-empty part of a :func:`repair` report, for the log."""
    out = []
    if report.get("pairs") or report.get("static"):
        out.append(
            "class clearance: %d pair(s) too close, %d net(s) too close to static copper; "
            "ripped %s; rerouted %d, partial %d, open %d"
            % (
                report.get("pair_count", len(report["pairs"])),
                len(report.get("static") or ()),
                ", ".join(report["ripped"]),
                len(report["rerouted"]),
                len(report["partial"]),
                len(report["open"]),
            )
        )
    audit = report.get("audit") or {}
    if audit.get("count"):
        out.append("class clearance: %d copper shortfall(s) left (audit)" % audit["count"])
    return out
