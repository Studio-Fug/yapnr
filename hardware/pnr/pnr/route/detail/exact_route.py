"""Exact-separation detailed routing (``PNR_EXACT_SEPARATION``).

The negotiated router (:func:`pnr.route.detail.maze._route_impl`) reserves a halo
around every net's cells, sized for the full distance to another net's centreline,
and calls two nets in conflict when their reservations overlap. Both halos count,
so routed nets end up about twice as far apart as the rules ask: on the 0.25 mm
grid with 0.25 mm tracks and 0.2 mm clearance, signal centrelines sit 3 cells
(0.75 mm) apart where 0.45 mm is enough, and vias 7 cells apart where 0.8 mm is.

This router keeps the same passes (PathFinder negotiation, commit, reroute around
committed copper, negotiated rip-up with a best snapshot, partial-tree recovery)
and the same grid, pads, escapes, drills and kernels, but judges two nets with the
exact pairwise rule: the centrelines of two different nets keep at least
``r_a + r_b + clearance`` apart, with ``r`` half the track width, or half the via
diameter around a via (two vias also keep the via drill spacing).

A route's centreline is made of grid edges between cell centres: orthogonal steps,
45° steps and through-vias. The closest points of two such segments include an end
of one of them, and a cell centre's closest point on an orthogonal step is one of
its ends, on a 45° step one of its ends or its centre (the centre of its 2x2 block).
So the rule holds exactly when

* every centreline cell of each net is far enough from the other net's cell
  centres, 45° step centres and vias, and
* every 45° step centre of each net is far enough from the other net's cell
  centres, 45° step centres (two crossing steps share theirs) and vias.

Each placed net keeps, per core kind ``k``, a **zone**: the cell centres, and the
2x2 block centres, where a cell (or a 45° step) of kind ``k`` of another net would
be too close (exact Euclidean distances, a 1 nm margin). Occupancy per kind counts
the zones covering a cell, so pricing or blocking a cell for a core of kind ``k``
is one lookup, and a 45° step is blocked by its block. A step's corner cells only
need to be passable for pads and obstacles, as before. The search runs on dense
fields (:func:`.dense_maze.build_exact_field`), so the packed and native kernels
both serve it.
"""

from __future__ import annotations

import math
from collections import defaultdict, deque
from typing import Dict, List, Optional

import numpy as np

from .dense_maze import DenseSession, build_exact_field
from .grid import Cell

MODES = ("off", "recover", "full")


def exact_mode() -> str:
    """``PNR_EXACT_SEPARATION``: ``recover`` (the default: route again with the
    exact rule when the negotiated route leaves connections open, and keep the
    route with fewer open connections, so complete routes are unchanged), ``off``
    or ``full`` (route with the exact rule only)."""
    import os

    mode = os.environ.get("PNR_EXACT_SEPARATION", "recover")
    return mode if mode in MODES else "recover"


class Separation:
    """Core kinds and their separations.

    A kind is a core radius and its net's copper clearance: the distinct
    (half track width, clearance) pairs of the nets' tracks, then a through-via
    (half the via diameter) at each distinct clearance. A net's clearance is its
    class clearance where that exceeds the fab clearance (``net_clearances``),
    else the fab clearance; with one clearance there is one via kind, ``via``.
    ``kind[net]`` and ``via_kind[net]`` are a net's track and via kinds.
    ``distance(e, k)`` is the least centre distance (mm) between an element of
    kind ``e`` of one net and a core of kind ``k`` of another: the two radii plus
    the larger of the two clearances (KiCad's rule between two net classes), and
    for two vias at least the via drill spacing. The stencils hold the grid
    offsets closer than that (with a 1 nm margin), between cell centres (``cc``),
    from a 45° step's centre to cell centres (``mc``) and from a cell centre to
    45° step centres (``cm``).
    """

    MARGIN = 1e-6

    def __init__(self, grid, nets):
        default = grid.track_width
        classes = getattr(grid, "net_clearances", {}) or {}

        def clearance(n):
            return max(grid.clearance, classes.get(n, grid.clearance))

        radius = {n: grid.net_widths.get(n, default) / 2 for n in nets}
        gaps = {n: clearance(n) for n in nets}
        tracks = sorted(set((radius[n], gaps[n]) for n in nets))
        vias = sorted(set(gaps.values()) | {grid.clearance})
        self.kind = {n: tracks.index((radius[n], gaps[n])) for n in nets}
        self.via = len(tracks) + vias.index(grid.clearance)
        self.via_kind = {n: len(tracks) + vias.index(gaps[n]) for n in nets}
        self.vias = set(range(len(tracks), len(tracks) + len(vias)))
        self.radii = [r for r, _ in tracks] + [grid.via_radius] * len(vias)
        self.gaps = [g for _, g in tracks] + vias
        self.pitch = grid.pitch
        kinds = range(len(self.radii))
        self.distance = [[self._distance(grid, e, k) for k in kinds] for e in kinds]
        self.cc = [[self._stencil(d, 0.0) for d in row] for row in self.distance]
        self.mc = [[self._stencil(d, -0.5) for d in row] for row in self.distance]
        self.cm = [[self._stencil(d, 0.5) for d in row] for row in self.distance]
        self.reach = max(math.ceil(d / self.pitch) for row in self.distance for d in row) + 2

    def _distance(self, grid, e, k):
        d = self.radii[e] + self.radii[k] + max(self.gaps[e], self.gaps[k])
        if e in self.vias and k in self.vias:
            d = max(d, grid.via_spacing)
        return d

    def _stencil(self, distance, offset):
        """``(dj, di)`` offsets whose centre distance is below ``distance``."""
        reach = math.ceil(distance / self.pitch) + 1
        limit = ((distance + self.MARGIN) / self.pitch) ** 2
        cells = [
            (dj, di)
            for dj in range(-reach, reach + 1)
            for di in range(-reach, reach + 1)
            if (di + offset) ** 2 + (dj + offset) ** 2 < limit
        ]
        if not cells:
            return (np.zeros(0, dtype=np.intp), np.zeros(0, dtype=np.intp))
        return tuple(np.array(v, dtype=np.intp) for v in zip(*cells))

    def __len__(self):
        return len(self.radii)


def _keys(grid, points, stencil=None, every_layer=False):
    """Flat ``[layer, j, i]`` keys of ``points`` (``(layer, j, i)`` arrays, or
    ``(j, i)`` on every layer) moved by each ``stencil`` offset, inside the grid."""
    if points is None:
        return np.zeros(0, dtype=np.int64)
    plane = grid.ny * grid.nx
    if every_layer:
        j, i = points
        layer = None
    else:
        layer, j, i = points
    if stencil is None:
        jj, ii = j.astype(np.int64), i.astype(np.int64)
        ll = None if layer is None else layer.astype(np.int64)
    else:
        dj, di = stencil
        jj = (j[:, None] + dj[None, :]).ravel().astype(np.int64)
        ii = (i[:, None] + di[None, :]).ravel().astype(np.int64)
        ll = None if layer is None else np.repeat(layer, len(dj)).astype(np.int64)
    keep = (jj >= 0) & (jj < grid.ny) & (ii >= 0) & (ii < grid.nx)
    flat = jj[keep] * grid.nx + ii[keep]
    if ll is None:
        return (np.arange(grid.nlayers, dtype=np.int64)[:, None] * plane + flat[None, :]).ravel()
    return ll[keep] * plane + flat


def _member(keys, table):
    """Which ``keys`` are in the sorted unique ``table``."""
    if not len(table) or not len(keys):
        return np.zeros(len(keys), dtype=bool)
    at = np.searchsorted(table, keys)
    at[at == len(table)] = 0
    return table[at] == keys


def _arrays(points):
    return tuple(np.array(v, dtype=np.intp) for v in zip(*points)) if points else None


class Zone:
    """One net's cores and, per core kind ``k``, its separation zones as sorted
    flat ``[layer, j, i]`` keys (only the cells a zone covers are held, so a long
    net on a large board costs its own area, not its bounding box's):

    * ``cells[k]``: cell centres where a core of kind ``k`` of another net would
      be too close to this net's copper (its centreline cells, the centres of
      its 45° steps, its vias);
    * ``blocks[k]``: 2x2 blocks (by lower-left cell) whose centre is too close,
      for another net's 45° step through that block.

    ``box`` (``j0, j1, i0, i1``, half-open) bounds every zone; ``track_keys``,
    ``via_keys`` (every layer) and ``step_keys`` are the cores' own keys.
    """

    __slots__ = (
        "net",
        "kind",
        "via_kind",
        "box",
        "cells",
        "blocks",
        "track",
        "steps",
        "ends",
        "vias",
        "track_keys",
        "via_keys",
        "step_keys",
    )

    def __init__(self, grid, sep: Separation, net, route):
        self.net = net
        self.kind = kind = sep.kind[net]
        self.via_kind = via_kind = sep.via_kind[net]
        track = {(c.layer, c.j, c.i) for c in route.cells}
        steps = set()
        ends = set()
        for a, b in route.edges:
            if a.layer == b.layer and a.i != b.i and a.j != b.j:
                steps.add((a.layer, min(a.j, b.j), min(a.i, b.i)))
                ends.add((a.layer, a.j, a.i))
                ends.add((b.layer, b.j, b.i))
        from .maze import _new_via_sites

        vias = sorted((j, i) for i, j in _new_via_sites(grid, route.edges, net))
        self.track = _arrays(sorted(track))
        self.steps = _arrays(sorted(steps))
        self.ends = _arrays(sorted(ends))
        self.vias = _arrays(vias)
        self.track_keys = _keys(grid, self.track)
        self.via_keys = _keys(grid, self.vias, every_layer=True)
        self.step_keys = _keys(grid, self.steps)
        js = [j for _, j, _ in track] + [j + 1 for _, j, _ in steps] + [j for j, _ in vias]
        is_ = [i for _, _, i in track] + [i + 1 for _, _, i in steps] + [i for _, i in vias]
        if not js:
            self.box = self.cells = self.blocks = None
            return
        reach = sep.reach
        j0, j1 = max(0, min(js) - reach), min(grid.ny, max(js) + reach + 1)
        i0, i1 = max(0, min(is_) - reach), min(grid.nx, max(is_) + reach + 1)
        self.box = (j0, j1, i0, i1)
        self.cells, self.blocks = [], []
        for k in range(len(sep)):
            cells = [
                _keys(grid, self.track, sep.cc[kind][k]),
                _keys(grid, self.steps, sep.mc[kind][k]),
                _keys(grid, self.vias, sep.cc[via_kind][k], every_layer=True),
            ]
            blocks = [
                _keys(grid, self.track, sep.cm[kind][k]),
                _keys(grid, self.steps, sep.cc[kind][k]),
                _keys(grid, self.vias, sep.cm[via_kind][k], every_layer=True),
            ]
            self.cells.append(np.unique(np.concatenate(cells)))
            self.blocks.append(np.unique(np.concatenate(blocks)))


class Occupancy:
    """How many placed nets' zones cover each cell (``cells[k]``) and each 2x2
    block (``blocks[k]``), per core kind ``k``."""

    def __init__(self, grid, sep: Separation):
        self.grid = grid
        self.sep = sep
        shape = (len(sep), grid.nlayers, grid.ny, grid.nx)
        self.cells = np.zeros(shape, dtype=np.int16)
        self.blocks = np.zeros(shape, dtype=np.int16)
        # Flat views of the same counts, per kind.
        self._flat = {
            "cells": self.cells.reshape(len(sep), -1),
            "blocks": self.blocks.reshape(len(sep), -1),
        }
        self.zones: Dict[str, Zone] = {}

    def _count(self, zone: Zone, step: int):
        flat_cells, flat_blocks = self._flat["cells"], self._flat["blocks"]
        for k in range(len(self.sep)):
            # A zone's keys are unique: a fancy-indexed add counts each once.
            flat_cells[k, zone.cells[k]] += step
            flat_blocks[k, zone.blocks[k]] += step

    def add(self, zone: Zone):
        self.remove(zone.net)
        self.zones[zone.net] = zone
        if zone.box is not None:
            self._count(zone, 1)

    def remove(self, net):
        zone = self.zones.pop(net, None)
        if zone is not None and zone.box is not None:
            self._count(zone, -1)

    def _others(self, table, net, kind):
        counts = getattr(self, table)[kind]
        zone = self.zones.get(net)
        if zone is None or zone.box is None:
            return counts
        out = counts.copy()
        out.reshape(-1)[getattr(zone, table)[kind]] -= 1
        return out

    def blocked(self, net):
        """``(track, via, step)`` masks of every other placed net's zones."""
        kind = self.sep.kind[net]
        return (
            self._others("cells", net, kind) > 0,
            (self._others("cells", net, self.sep.via_kind[net]) > 0).any(axis=0),
            self._others("blocks", net, kind) > 0,
        )

    @staticmethod
    def _groups(zone: Zone):
        """The zone's core keys with the table and kind each is judged in."""
        return (
            ("cells", zone.kind, zone.track_keys),
            ("cells", zone.via_kind, zone.via_keys),
            ("blocks", zone.kind, zone.step_keys),
        )

    def conflicts(self, zone: Zone) -> bool:
        """A core of ``zone`` lies in another placed net's zone."""
        own = self.zones.get(zone.net)
        if own is not None and own.box is None:
            own = None
        for table, kind, keys in self._groups(zone):
            if not len(keys):
                continue
            counts = self._flat[table][kind][keys].astype(np.int64)
            if own is not None:
                counts -= _member(keys, getattr(own, table)[kind])
            if (counts > 0).any():
                return True
        return False

    def crossed(self, zone: Zone) -> List[str]:
        """Placed nets other than ``zone.net`` whose zones hold a core of ``zone``."""
        groups = self._groups(zone)
        return sorted(
            name
            for name, other in self.zones.items()
            if name != zone.net
            and other.box is not None
            and any(
                _member(keys, getattr(other, table)[kind]).any()
                for table, kind, keys in groups
                if len(keys)
            )
        )

    def overuse(self):
        """History increment: at each placed core (a 45° step at its two end
        cells), how many other nets' zones hold it."""
        increment = np.zeros(self.cells.shape[1:])
        for zone in self.zones.values():
            if zone.track is not None:
                over = self.cells[zone.kind][zone.track].astype(np.int64) - 1
                hit = over > 0
                if hit.any():
                    np.add.at(increment, tuple(a[hit] for a in zone.track), over[hit])
            if zone.vias is not None:
                j, i = zone.vias
                for layer in range(self.cells.shape[1]):
                    over = self.cells[zone.via_kind][layer, j, i].astype(np.int64) - 1
                    hit = over > 0
                    if hit.any():
                        np.add.at(increment, (layer, j[hit], i[hit]), over[hit])
            if zone.steps is not None:
                layer, j, i = zone.steps
                over = self.blocks[zone.kind][zone.steps].astype(np.int64) - 1
                hit = over > 0
                if hit.any():
                    # Both cells of the step's diagonal: the lower-left/upper-right
                    # pair and the other pair; only cells of the route are cores.
                    for dj, di in ((0, 0), (1, 1), (0, 1), (1, 0)):
                        np.add.at(
                            increment, (layer[hit], j[hit] + dj, i[hit] + di), 0.5 * over[hit]
                        )
        return increment


def supported(grid) -> bool:
    from .maze import maze_kernel

    if maze_kernel() == "reference":
        return False
    static = DenseSession(grid).static
    return static is not None and static.stencil() is not None


def route_exact(
    grid,
    net_access: Dict[str, List[Cell]],
    *,
    max_iters: int = 12,
    via_cost: float = 3.0,
    pres_fac0: float = 0.5,
    pres_inc: float = 0.6,
    hist_fac: float = 1.0,
    rrr_rounds: int = 12,
    rip_penalty: float = 4.0,
    max_rip: int = 8,
    session: Optional[DenseSession] = None,
    **_unused,
):
    """Negotiated detailed route of ``net_access`` under the exact separation
    model. The passes mirror :func:`pnr.route.detail.maze._route_impl`."""
    from .maze import (
        RoutedNet,
        RouteResult,
        _astar,
        _new_via_sites,
        _Route,
        _route_one,
        _to_geometry,
        remaining_connections,
    )

    session = session or DenseSession(grid)
    static = session.static
    nets = sorted(n for n, cells in net_access.items() if len(cells) >= 2)
    sep = Separation(grid, nets)
    history = np.zeros((grid.nlayers, grid.ny, grid.nx))
    pres_fac = pres_fac0

    def zone_of(net, route):
        return Zone(grid, sep, net, route)

    def route_net(net, field):
        return _route_one(
            grid, net_access[net], net, {}, {}, via_cost, 0.0, _session=session, _field=field
        )

    def blocked_field(net, committed):
        track, via, step = committed.blocked(net)
        return build_exact_field(static, net, track_block=track, via_block=via, diag_block=step)

    # PathFinder negotiation: rip and reroute every net against the others'
    # zones, raising the present price and the history of contested cores.
    occ = Occupancy(grid, sep)
    routed: Dict[str, Optional[_Route]] = {}

    def negotiate(net):
        occ.remove(net)
        kind = sep.kind[net]
        field = build_exact_field(
            static,
            net,
            track_count=occ.cells[kind],
            via_count=occ.cells[sep.via_kind[net]],
            history=history,
            pres_fac=pres_fac,
        )
        route = route_net(net, field)
        routed[net] = route
        if route:
            occ.add(zone_of(net, route))

    for net in nets:
        negotiate(net)
    iters = 0
    # The exact model resolves its last few contested cores late: negotiate up
    # to twice as long (stopping at convergence) before the commit passes.
    for it in range(2 * max_iters):
        iters = it + 1
        for net in nets:
            negotiate(net)
        increment = occ.overuse()
        if not increment.any():
            break
        history += hist_fac * increment
        pres_fac += pres_inc

    # Pass 1: commit the negotiated routes that conflict with nothing committed.
    committed = Occupancy(grid, sep)
    routes: Dict[str, _Route] = {}
    result_nets: Dict[str, RoutedNet] = {}

    def commit(net, route, zone=None):
        committed.add(zone or zone_of(net, route))
        routes[net] = route
        rn = _to_geometry(route)
        rn.name = net
        rn.routed = True
        result_nets[net] = rn

    def drop(net):
        committed.remove(net)
        routes.pop(net, None)
        rn = _to_geometry(_Route())
        rn.name = net
        rn.routed = False
        result_nets[net] = rn

    leftover = []
    for net in nets:
        route = routed.get(net)
        if route:
            zone = zone_of(net, route)
            if not committed.conflicts(zone):
                commit(net, route, zone)
                continue
        leftover.append(net)

    def span(net):
        cs = net_access[net]
        return (max(c.i for c in cs) - min(c.i for c in cs)) + (
            max(c.j for c in cs) - min(c.j for c in cs)
        )

    # Pass 2: reroute the leftovers around the committed zones, shortest first.
    unrouted = []
    for net in sorted(leftover, key=lambda n: (span(n), n)):
        proposal = route_net(net, blocked_field(net, committed))
        if proposal:
            zone = zone_of(net, proposal)
            if not committed.conflicts(zone):
                commit(net, proposal, zone)
                continue
        unrouted.append(net)
        drop(net)

    # Pass 3: negotiated rip-up with a rising per-net crossing price; keep the
    # best (most connected terminal branches) state seen.
    def routed_count():
        return sum(
            max(
                0,
                len(set(net_access[n])) - 1 - remaining_connections(net_access[n], routes[n].edges),
            )
            for n in nets
            if n in routes
        )

    def snapshot():
        return {n: routes[n] for n in nets if n in routes and result_nets[n].routed}

    best_snap = snapshot()
    best_count = routed_count()
    rip_count: Dict[str, int] = defaultdict(int)
    queue: deque = deque(sorted(unrouted, key=lambda n: (span(n), n)))
    queued = set(queue)
    budget = len(nets) * rrr_rounds
    while queue and budget > 0:
        budget -= 1
        net = queue.popleft()
        queued.discard(net)
        pen = rip_penalty * (1 + rip_count[net])
        track, via, _ = committed.blocked(net)
        route = route_net(
            net,
            build_exact_field(
                static,
                net,
                track_soft=np.where(track, pen, 0.0),
                via_soft=np.where(via, pen, 0.0),
            ),
        )
        if not route:
            continue
        zone = zone_of(net, route)
        crossed = committed.crossed(zone)
        if len(crossed) > max_rip:
            # Too disruptive at this price: route strictly around instead.
            around = route_net(net, blocked_field(net, committed))
            if around:
                around_zone = zone_of(net, around)
                if not committed.conflicts(around_zone):
                    commit(net, around, around_zone)
            continue
        for other in crossed:
            drop(other)
            rip_count[other] += 1
            if other not in queued:
                queue.append(other)
                queued.add(other)
        commit(net, route, zone)
        count = routed_count()
        if count > best_count:
            best_count = count
            best_snap = snapshot()

    # Restore the best snapshot; then keep useful branches of incomplete nets
    # and connect what still can be, around everything else.
    unrouted = []
    for net in nets:
        route = best_snap.get(net)
        rn = _to_geometry(route) if route else _to_geometry(_Route())
        rn.name = net
        rn.remaining_connections = remaining_connections(
            net_access[net], route.edges if route else []
        )
        rn.routed = rn.remaining_connections == 0
        result_nets[net] = rn
        if not rn.routed:
            unrouted.append(net)
    committed = Occupancy(grid, sep)
    for name, saved in best_snap.items():
        committed.add(zone_of(name, saved))
    for net in sorted(unrouted):
        saved = best_snap.get(net)
        forest = _Route(list(saved.cells), list(saved.edges)) if saved else _Route()
        committed.remove(net)
        remaining = set(net_access[net]) - set(forest.cells)
        first_tree = set(forest.cells)
        field = None
        while remaining:
            if first_tree:
                tree = first_tree
                first_tree = set()
            else:
                seed = min(remaining, key=lambda c: (c.layer, c.i, c.j))
                remaining.remove(seed)
                tree = {seed}
            while remaining:
                if field is None:
                    field = blocked_field(net, committed)
                path = _astar(
                    grid,
                    tree,
                    remaining,
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
                    list(set(forest.cells) | set(path)), forest.edges + list(zip(path, path[1:]))
                )
                if committed.conflicts(zone_of(net, trial)):
                    break
                forest = trial
                tree.update(path)
                remaining.difference_update(path)
        rn = _to_geometry(forest)
        rn.name = net
        rn.remaining_connections = remaining_connections(net_access[net], forest.edges)
        rn.routed = rn.remaining_connections == 0
        result_nets[net] = rn
        committed.add(zone_of(net, forest))
    unrouted = [net for net in nets if not result_nets[net].routed]
    return RouteResult(nets=result_nets, unrouted=sorted(unrouted), iterations=iters)


def missing_connections(result) -> int:
    return sum(rn.remaining_connections for rn in result.nets.values() if not rn.routed)
