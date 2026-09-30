"""Router side of :mod:`pnr.trace`: the copper, pad groups and progress of each net event.

:func:`start` is called by :func:`pnr.route.detail.router.route_board` once the escape plan
exists. Inside a traced ``route`` scope it registers a :class:`RouteTrace` as the recorder's
route hook, which :mod:`pnr.route.detail.maze` calls for each net it adds, rips, commits or
drops; anywhere else (relocation probes, holdout routing, untraced runs) it returns None and
nothing is recorded. A net's copper is what writeback would receive for it at that moment:
grid segments, vias (plated pad transitions excluded, as ``route_board`` excludes them) and
the escapes of the pads its tree reaches. Observational only: it reads the router's state and
never changes it.
"""

from __future__ import annotations

import math
from types import SimpleNamespace

from pnr import trace

from ...place.geometry import pad_rects
from .grid import Cell


def start(graph, grid, plan, net_width, track_width, planes, deferred, max_iters):
    """A :class:`RouteTrace` for this route, or None unless a traced route scope is open."""
    recorder = trace.current()
    if recorder is None or not recorder.in_route_scope():
        return None
    try:
        hook = RouteTrace(recorder, graph, grid, plan, net_width, track_width, planes, deferred)
        recorder.route = hook
        recorder.event(
            "route_begin",
            pitch=trace.um(grid.pitch),
            layers=[hook.layer(name) for name in grid.layers],
            nets=len(plan.net_access),
            plane_nets=sorted(planes),
            deferred=sorted(deferred),
            blocked=sorted(plan.blocked_nets),
            max_iters=int(max_iters),
            progress=hook.progress(False),
            phase="negotiation",
        )
        return hook
    except Exception as error:  # noqa: BLE001 - diagnostics never fail the router
        recorder._disable("route_start", error)
        return None


def _find(parent, cell):
    parent.setdefault(cell, cell)
    root = cell
    while parent[root] != root:
        root = parent[root]
    while cell != root:
        parent[cell], cell = root, parent[cell]
    return root


class RouteTrace:
    """Per-route state: pin indices of access cells, escapes and per-net connection counts."""

    def __init__(self, recorder, graph, grid, plan, net_width, track_width, planes, deferred):
        self.recorder = recorder
        self.grid = grid
        self.widths = dict(net_width)
        self.track_width = float(track_width)
        self.via = (
            trace.um(2 * grid.via_radius),
            trace.um(2 * getattr(grid, "via_drill_radius", 0)),
        )
        self.layer_names = list(recorder.layers or grid.layers)
        centres = {}
        for comp in graph.components:
            for name, _net, rect in pad_rects(comp):
                centres[(comp.ref, name)] = (rect.cx, rect.cy)
        self.pins = {}
        for net in graph.nets:
            pins = recorder.pins.get(net.name) or [tuple(p) for p in net.pins]
            self.pins[net.name] = [tuple(p) for p in pins]
        self.total = sum(max(0, len(p) - 1) for p in self.pins.values())
        self.access = {}
        self.escapes = {}
        for esc in plan.escapes:
            pins = self.pins.get(esc.net, [])
            known = [(i, centres[p]) for i, p in enumerate(pins) if p in centres]
            if not known:
                continue
            index = min(known, key=lambda item: (math.dist(item[1], esc.pad_xy), item[0]))[0]
            self.access.setdefault(esc.net, {}).setdefault(esc.access, []).append(index)
            self.escapes.setdefault(esc.net, []).append(esc)
        self.done = {False: {}, True: {}}

    def layer(self, name):
        return self.layer_names.index(name) if name in self.layer_names else 0

    def width(self, net):
        return self.widths.get(net, self.track_width)

    def progress(self, provisional):
        done = sum(self.done[provisional].values())
        source = "router-provisional" if provisional else "router"
        return dict(done=done, total=self.total, source=source)

    def groups(self, net, edges, extra_unions=()):
        """Pin-index groups of ``net`` joined by the route ``edges`` (and ``extra_unions``)."""
        count = len(self.pins.get(net, []))
        access = self.access.get(net, {})
        parent = {}
        for a, b in list(edges) + list(extra_unions):
            ra, rb = _find(parent, a), _find(parent, b)
            if ra != rb:
                parent[ra] = rb
        joined = {}
        for cell, pins in access.items():
            joined.setdefault(_find(parent, cell), set()).update(pins)
        covered = set().union(*joined.values()) if joined else set()
        groups = [sorted(g) for g in joined.values()]
        groups += [[i] for i in range(count) if i not in covered]
        return sorted(groups)

    def copper(self, net, cells, segments, vias):
        """Copper blob of one net: grid segments, new vias and the escapes its cells reach."""
        grid, width = self.grid, self.width(net)
        tracks = []
        for layer, a, b in segments:
            (x0, y0), (x1, y1) = grid.center_of(*a), grid.center_of(*b)
            tracks.append([self.layer(grid.layers[layer]), x0, y0, x1, y1, width])
        points = []
        for i, j in vias:
            if grid.plated_transition(net, i, j) is None:
                points.append(grid.center_of(i, j))
        scratch = SimpleNamespace(tracks=[], vias=[])
        present = set(cells)
        from .router import _emit_escape

        for esc in self.escapes.get(net, []):
            if esc.access in present:
                _emit_escape(scratch, esc, grid, width)
        for _net, layer, a, b, w in scratch.tracks:
            if math.dist(a, b) >= 1e-6:
                tracks.append([self.layer(layer), a[0], a[1], b[0], b[1], w])
        points += [(x, y) for _net, x, y in scratch.vias]
        return self._blob(net, tracks, points)

    def _blob(self, net, tracks, points):
        um = trace.um
        rows = [
            [layer, um(x0), um(y0), um(x1), um(y1), um(w)] for layer, x0, y0, x1, y1, w in tracks
        ]
        vias = sorted({(um(x), um(y)) for x, y in points})
        return self.recorder.blob(
            dict(
                tracks=sorted(rows),
                vias=[[x, y, self.via[0], self.via[1]] for x, y in vias],
                zones=[],
            )
        )

    def net(self, net, route, op, provisional, pass_index):
        """One router event: ``op`` in add, rip (provisional or real), commit, drop."""
        if not self.recorder.active:
            return
        try:
            self._net(net, route, op, provisional, pass_index)
        except Exception as error:  # noqa: BLE001
            self.recorder._disable("route_net", error)

    def _net(self, net, route, op, provisional, pass_index):
        if not self.recorder._budget("net", provisional):
            return
        if route is not None and op in ("add", "commit"):
            from .maze import _to_geometry

            rn = _to_geometry(route)
            copper = self.copper(net, route.cells, rn.segments, rn.vias)
            groups = self.groups(net, route.edges)
        else:
            copper = None
            groups = [[i] for i in range(len(self.pins.get(net, [])))]
        self.done[provisional][net] = len(self.pins.get(net, [])) - len(groups)
        self.recorder._emit(
            "net",
            provisional,
            net=net,
            op=op,
            provisional=bool(provisional),
            copper=copper,
            groups=groups,
            progress=self.progress(provisional),
            phase="negotiation" if provisional else ("commit" if op == "commit" else "rip-up"),
            **{"pass": int(pass_index)},
        )

    def end(self, board):
        """``route_end``: the final copper per net (escapes included) and its groups."""
        if not self.recorder.active or self.recorder.route is not self:
            return
        try:
            self._end(board)
        except Exception as error:  # noqa: BLE001
            self.recorder._disable("route_end", error)
        finally:
            self.recorder.route = None

    def _end(self, board):
        grid = self.grid
        per_net = {}
        for net, layer, a, b, w in board.tracks:
            per_net.setdefault(net, ([], []))[0].append(
                [self.layer(layer), a[0], a[1], b[0], b[1], w]
            )
        for net, x, y in board.vias:
            per_net.setdefault(net, ([], []))[1].append((x, y))
        nets, groups, done = {}, {}, {}
        for net, (tracks, points) in sorted(per_net.items()):
            nets[net] = self._blob(net, tracks, points)
        for net, rn in sorted(board.result.nets.items()):
            edges = [(Cell(la, *p), Cell(la, *q)) for la, p, q in rn.segments]
            by_column = {}
            for cell in rn.cells:
                by_column.setdefault((cell.i, cell.j), []).append(cell)
            unions = []
            for (i, j), cells in by_column.items():
                if len(cells) > 1 and (
                    (i, j) in set(rn.vias) or grid.plated_transition(net, i, j) is not None
                ):
                    unions += [(cells[0], c) for c in cells[1:]]
            groups[net] = self.groups(net, edges, unions)
            done[net] = len(self.pins.get(net, [])) - len(groups[net])
        self.done[False] = done
        self.recorder._emit(
            "route_end",
            nets=nets,
            groups=groups,
            unrouted=sorted(board.result.unrouted),
            deferred=sorted(board.deferred_nets),
            vias=len(board.vias),
            copper_length_mm=round(sum(math.dist(t[2], t[3]) for t in board.tracks), 3),
            progress=self.progress(False),
            phase="routed",
        )
