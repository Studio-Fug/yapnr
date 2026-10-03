"""Board-level detailed routing driver (detailed router, R5 integration).

Ties R2 (grid) + R4 (maze) together into one call over a *placed*
:class:`pnr.graph.BoardGraph`: build the grid, split nets into **plane** nets
(poured on inner layers — connected by a via drop, handled by
:mod:`pnr.writeback`) and **signal** nets, and negotiated-route the signals to a
DRC-clean result. The output is grid geometry + mm-space tracks/vias that
:mod:`pnr.writeback` emits (replacing FreeRouting), and the per-net routed/unrouted
status the place↔route loop consumes as ground truth (design §6/§R5).

Pure Python on the graph — no pcbnew. Deterministic under the grid's fixed order.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import List, Optional, Set, Tuple

from pnr.constraints import CompiledConstraints
from pnr.graph import BoardGraph, footprint_point

from ...place.geometry import Rect, outline_size, pad_rects
from .escape import plan_escapes, trapped_access_sites
from .grid import DEFAULT_SIGNAL_LAYERS, RouteGrid
from .maze import RouteResult, route

# Full copper stack for a 4-layer board, outer→inner→outer.
_FOUR_LAYER = ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")

# Fallback fab geometry when the rules carry no ``fab:`` block (older rules.json).
_FAB_DEFAULT = {
    "track_width_mm": 0.15,
    "clearance_mm": 0.13,
    "via_diameter_mm": 0.45,
    "via_drill_mm": 0.25,
    "hole_clearance_mm": 0.2,
}


def _fab(rules: Optional[dict]) -> dict:
    """The fab geometry from the rules (``fab:`` block), defaults filled in."""
    out = dict(_FAB_DEFAULT)
    if rules and isinstance(rules.get("fab"), dict):
        out.update({k: float(v) for k, v in rules["fab"].items() if k in out})
    return out


def _net_widths(rules: Optional[dict], default_mm: float) -> dict:
    """net name -> track width (mm) from the rules' net classes (which resolve
    width from an explicit value or an IPC-2221 current). Nets not in a width class
    route at ``default_mm``. Plane nets are skipped (they pour, not route)."""
    out: dict = {}
    if not rules:
        return out
    for nc in rules.get("net_classes", []):
        w = nc.get("width_mm")
        if not w or nc.get("plane_layer"):
            continue
        for n in nc.get("nets", []):
            out[n] = float(w)
    return out


def _net_clearances(rules: Optional[dict]) -> dict:
    """net name -> the largest clearance (mm) of the net classes that list it.
    Writeback stamps each class's clearance into the project
    (:func:`pnr.writeback.patch_project_rules`), KiCad judges two nets at the
    larger of their clearances, and a net outside every class keeps the fab
    clearance (the grid's)."""
    out: dict = {}
    for nc in (rules or {}).get("net_classes", []):
        value = nc.get("clearance_mm")
        if not value:
            continue
        for n in nc.get("nets", []):
            out[n] = max(out.get(n, 0.0), float(value))
    return out


def _late_copper(
    graph: BoardGraph, planes: Set[str], deferred: Set[str], escapes=()
) -> Optional[str]:
    """What later stages add to the board without the signal grid holding it, or
    None: the via drops of plane nets' surface pads (writeback dog-bones them after
    routing, :func:`pnr.writeback._dogbone_fanout_net`) and the deferred power and
    pair nets (routed natively after the grid). A pad whose drop the escape plan
    already holds (an escape of its net at its centre, ``escapes``) is not late.
    :func:`.maze.route` skips its exact-separation recovery when there is any."""
    planned = {
        (esc.net, round(esc.pad_xy[0], 6), round(esc.pad_xy[1], 6))
        for esc in escapes
        if esc.net in planes and esc.kind != "blocked"
    }
    pads = sorted(
        "%s.%s" % (comp.ref, name)
        for comp in graph.components
        for (name, net, r), pad in zip(pad_rects(comp), comp.pads)
        if net in planes
        and not pad.through_hole
        and (net, round(r.cx, 6), round(r.cy, 6)) not in planned
    )
    parts = []
    if pads:
        parts.append(
            "%d plane-net surface pad%s (%s%s)"
            % (
                len(pads),
                "" if len(pads) == 1 else "s",
                ", ".join(pads[:6]),
                ", ..." if len(pads) > 6 else "",
            )
        )
    if deferred:
        parts.append(
            "%d deferred net%s (%s)"
            % (len(deferred), "" if len(deferred) == 1 else "s", ", ".join(sorted(deferred)[:6]))
        )
    return "; ".join(parts) or None


def _track_halo(width: float, signal_width: float, clearance: float, pitch: float) -> int:
    """Cells to reserve so even a fine grid preserves copper separation."""
    return max(0, math.ceil((width / 2 + clearance + signal_width / 2) / pitch) - 1)


@dataclass
class BoardRoute:
    """Detailed-route result in board (mm) space, ready for write-back."""

    result: RouteResult
    grid: RouteGrid
    # mm-space geometry with the owning net:
    #   tracks (net, layer_name, (x0,y0), (x1,y1), width_mm), vias (net, x, y).
    tracks: List[Tuple[str, str, Tuple[float, float], Tuple[float, float], float]] = field(
        default_factory=list
    )
    vias: List[Tuple[str, float, float]] = field(default_factory=list)
    # The vias above that are not through vias (a board whose via policy allows
    # blind, buried or micro vias, pnr.via_policy): (net, x, y, top layer, bottom
    # layer, kind), one per barrel; a via without an entry is through. Two entries
    # with one (net, x, y) are two holes whose spans do not overlap.
    via_spans: List[Tuple[str, float, float, str, str, str]] = field(default_factory=list)
    plane_nets: Set[str] = field(default_factory=set)
    # net -> localized static escape failures in engine mm
    failure_sites: dict = field(default_factory=dict)
    pressure_events: Optional[list] = None
    deferred_nets: Set[str] = field(default_factory=set)
    escape_diagnostics: dict = field(default_factory=dict)
    # The declared copper stack's warnings (pnr.stack.assess); also in
    # escape_diagnostics["stack_warnings"], which the run's PnR report keeps.
    stack_warnings: List[str] = field(default_factory=list)
    # Pair / group length tuning report (pnr.route.detail.tune); None without sets.
    length_report: Optional[list] = None

    @property
    def fully_routed(self) -> bool:
        return self.result.fully_routed

    def summary(self) -> str:
        return "%s; %d track segs, %d vias (planes: %d nets)" % (
            self.result.summary(),
            len(self.tracks),
            len(self.vias),
            len(self.plane_nets),
        )


def _plane_nets(rules: Optional[dict]) -> Set[str]:
    out: Set[str] = set()
    if not rules:
        return out
    for nc in rules.get("net_classes", []):
        if nc.get("plane_layer"):
            out.update(nc.get("nets", []))
    return out


def _net_plane_layer(rules: Optional[dict]) -> dict:
    """plane-net name -> its plane layer name (from the routing rules)."""
    out: dict = {}
    if rules:
        for nc in rules.get("net_classes", []):
            pl = nc.get("plane_layer")
            if pl:
                for n in nc.get("nets", []):
                    out[n] = pl
    return out


def _signal_layers(rules: Optional[dict]) -> Tuple[str, ...]:
    """The copper layers the detailed router routes signals on. A 4-layer board
    with split planes on the inners routes on **all four** — F/B plus the inner-
    layer *gaps* between the split planes — which is the routing resource a dense
    2-signal-layer board lacks. Otherwise the two outer layers. (The legacy
    heuristic: a board that declares its stack uses :func:`layer_plan`.)"""
    if rules and int(rules.get("layers", 2)) >= 4 and _plane_nets(rules):
        return _FOUR_LAYER
    return DEFAULT_SIGNAL_LAYERS


def layer_plan(graph: BoardGraph, rules: Optional[dict]):
    """``(grid layers, plane nets, stack)`` for routing ``graph``.

    A board that declares its copper stack (:mod:`pnr.stack`) routes every signal
    and split-plane layer in stack order and keeps every net with a dedicated plane
    out of the maze; otherwise (``stack`` None) the legacy heuristic, unchanged."""
    from pnr.stack import assess

    stack, warnings = assess(rules, getattr(graph, "stack", None))
    _warn_once(warnings)
    planes = _plane_nets(rules)
    if stack is None:
        return _signal_layers(rules), planes, None
    return stack.grid_layers, planes | set(stack.plane_nets), stack


_WARNED: Set[str] = set()


def _warn_once(warnings) -> None:
    """Each stack warning (pnr.stack.assess) once per process, on stderr, where the
    run's place-and-route log keeps it; the route result also carries them."""
    import sys

    for text in warnings:
        if text not in _WARNED:
            _WARNED.add(text)
            sys.stderr.write("pnr.stack: warning: %s\n" % text)


# Copper thickness of one ounce per square foot (mm), the IPC-2221 unit.
_OZ_MM = 0.035


def current_layer_mask(stack, layers, rules, net_width, default_width):
    """net -> the grid layers a current-rated net's tracks may use.

    A net class with ``current_a`` sizes its width for outer copper (IPC-2221,
    ``copper_oz``). An inner layer is open to such a net only if the IPC-2221
    internal width at that layer's declared copper thickness fits the net's routed
    width; the outer layers carry its pads and stay open. Nets that fit everywhere,
    and layers of unknown thickness, are not restricted.
    """
    from pnr.electrical import current_width

    out = {}
    for nc in (rules or {}).get("net_classes", []):
        current = nc.get("current_a")
        if not current or nc.get("plane_layer"):
            continue
        for net in nc.get("nets", []):
            width = net_width.get(net, default_width)
            allowed = []
            for index, name in enumerate(layers):
                copper = stack.layer(name).copper_mm
                inner = 0 < index < len(layers) - 1
                if inner and copper:
                    need = current_width(
                        current, copper / _OZ_MM, nc.get("delta_t_c") or 10.0, external=False
                    )
                    if need > width + 1e-9:
                        continue
                allowed.append(index)
            if len(allowed) < len(layers):
                out[net] = frozenset(allowed)
    return out


def _mark_plane_regions(
    grid: RouteGrid, graph: BoardGraph, rules: Optional[dict], margin: float, stack=None
) -> None:
    """Block the poured split-plane regions on the inner layers so signals + their
    through-vias avoid the plane copper (they route the gaps). Each plane net's
    region is the bbox of its pads + ``margin`` (matching the writeback pour) +
    clearance — the same split-plane geometry :func:`pnr.writeback.apply_planes`
    lays down, so the grid model and the emitted copper agree. With a declared
    ``stack`` only its split planes apply (dedicated planes are not grid layers)."""
    layer_idx = {name: i for i, name in enumerate(grid.layers)}
    net_layer = _net_plane_layer(rules) if stack is None else stack.split_nets
    if not net_layer:
        return
    rects: dict = {}
    for comp in graph.components:
        for _name, net, r in pad_rects(comp):
            if net in net_layer:
                rects.setdefault(net, []).append(r)
    for net, rs in rects.items():
        la = layer_idx.get(net_layer[net])
        if la is None:
            continue
        x0 = min(r.left for r in rs)
        x1 = max(r.right for r in rs)
        y0 = min(r.bottom for r in rs)
        y1 = max(r.top for r in rs)
        region = Rect((x0 + x1) / 2.0, (y0 + y1) / 2.0, x1 - x0, y1 - y0)
        grid.block_region(region, layers=[la], grow=margin + grid.clearance, block_vias=False)


def _mark_copper_keepouts(grid: RouteGrid, graph: BoardGraph, rules: Optional[dict]) -> None:
    """Apply the same physical copper exclusions as KiCad writeback.

    Reserve a conservative bounding box for rotated rule areas and round screw
    clearances. Grow by via radius so both tracks and through-vias stay clear.
    """
    if not rules:
        return
    for spec in rules.get("mounting_holes", []):
        x, y = spec["at"]
        diameter = spec["clearance_diameter_mm"]
        grid.block_region(Rect(x, y, diameter, diameter), grow=grid.via_radius)
    for spec in rules.get("copper_keepouts", []):
        if "items" in spec:  # v1: per layer, per item, optional allow lists
            _mark_keepout_v1(grid, graph, spec)
            continue
        comp = graph.component(spec["ref"])
        x0, y0, x1, y1 = spec["rect_mm"]
        # The same transform as writeback's rule area (mirrored with the pads).
        points = [footprint_point(comp, x, y) for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
        xs, ys = zip(*points)
        grid.block_region(
            Rect(
                (min(xs) + max(xs)) / 2,
                (min(ys) + max(ys)) / 2,
                max(xs) - min(xs),
                max(ys) - min(ys),
            ),
            grow=grid.via_radius,
        )


def _mark_keepout_v1(grid: RouteGrid, graph: BoardGraph, spec: dict) -> None:
    """A v1 keepout on the grid. Tracks are barred on the listed layers the grid
    routes; vias (through: every layer) wherever any listed layer bars them, as
    KiCad's rule areas do. A cell is barred when its centre comes within half the
    widest track (or the via radius) plus half a cell diagonal of the polygon.
    Without allow lists every net is barred (static obstacles); with them only the
    nets outside ``allowed_nets`` (:meth:`RouteGrid.add_net_keepout`). Pours are
    writeback's (rule area flags or custom rules)."""
    import numpy as np

    from pnr.fixed_block import keepout_polygon

    poly = keepout_polygon(graph, spec)
    names = list(spec.get("layers") or grid.layers)
    items = spec.get("items") or ("tracks", "vias", "pours")
    layers = [grid.layers.index(n) for n in names if n in grid.layers]
    cell_radius = grid.pitch / math.sqrt(2)
    widest = max([grid.track_width] + list((grid.net_widths or {}).values()))
    shape = (grid.nlayers, grid.ny, grid.nx)
    track = via = None
    if "tracks" in items and layers:
        cells = grid.polygon_cells(poly, (), widest / 2 + cell_radius)
        track = np.zeros(shape, dtype=bool)
        track[layers] = cells
    if "vias" in items and names:
        cells = grid.polygon_cells(poly, (), grid.via_radius + cell_radius)
        via = np.zeros(shape, dtype=bool)
        via[:] = cells
    if spec.get("allow_classes") or spec.get("allow_nets"):
        if track is not None or via is not None:
            grid.add_net_keepout(track, via, spec.get("allowed_nets") or ())
        return
    if track is not None:
        grid.blocked |= track
    if via is not None:
        grid.via_blocked |= via


def block_ports(grid: RouteGrid, graph: BoardGraph, copper: Optional[dict], skip=()):
    """``{net: [(Cell, point, layer)]}``: where the router joins each fixed block's
    copper (pnr.fixed_block.pick_ports): for every net with pads in ``graph`` (and
    not in ``skip``, the plane nets), one free end per connected piece of its block
    copper that no pad already reaches, on a routed layer, at a cell the net may
    enter. ``{}`` without blocks."""
    from pnr.fixed_block import components_touching, pick_ports

    from .grid import Cell

    if not copper or not copper.get("blocks"):
        return {}
    pads = {}
    for comp in graph.components:
        side = grid.layers[grid.side_layer(comp.side)]
        for (_name, net, r), pad in zip(pad_rects(comp), comp.pads):
            if not net:
                continue
            for layer in grid.layers if pad.through_hole else (side,):
                pads.setdefault(net, []).append((layer, r.left, r.bottom, r.right, r.top))
    out = {}
    for block in copper["blocks"]:
        nets = {t[0] for t in block.get("tracks", [])} | {a[0] for a in block.get("arcs") or []}
        nets |= {v.get("net") for v in block.get("vias", [])}
        for net in sorted(n for n in nets if n and n in pads and n not in skip):
            joined = components_touching(block, net, pads[net])
            centres = [((x0 + x1) / 2, (y0 + y1) / 2) for _, x0, y0, x1, y1 in pads[net]]
            for point, layer in pick_ports(block, net, centres, joined):
                if layer not in grid.layers:
                    continue
                la = grid.layers.index(layer)
                i, j = grid.cell_of(*point)
                if grid.passable(la, i, j, net):
                    out.setdefault(net, []).append((Cell(la, i, j), point, layer))
    return out


def _mark_source_arrays(grid, graph, rules):
    """Reserve copper that the following native source-array stage will emit."""
    if not rules or not rules.get("plane_access_intents"):
        return
    from pnr.plane_intent import array_geometry

    for intent in rules["plane_access_intents"]:
        if intent["kind"] != "power_array":
            continue
        comp = graph.component(intent["ref"])
        pads = [
            ((r.cx, r.cy), (r.w, r.h))
            for number, net, r in pad_rects(comp)
            if number in intent["pads"]
        ]
        plan = array_geometry(pads, comp.pos, intent, rules["plane_access_fab"])
        surface = grid.layers.index(intent["surface"])
        extra = max(0.0, rules["plane_access_fab"].get("clearance_mm", 0.2) - grid.clearance)
        for a, z, width in plan["tracks"]:
            r = Rect(
                (a[0] + z[0]) / 2,
                (a[1] + z[1]) / 2,
                abs(a[0] - z[0]) + width + 2 * extra,
                abs(a[1] - z[1]) + width + 2 * extra,
            )
            grid.add_pad(surface, intent["net"], r)
        for point, diameter, drill in plan["vias"]:
            r = Rect(*point, diameter + 2 * extra, diameter + 2 * extra)
            for layer in range(grid.nlayers):
                grid.add_pad(layer, intent["net"], r)
            grid.escape_vias.append((intent["net"], point))


def _diag_unrouted(grid, net_access, unrouted, via_keepout):
    """Diagnostic: re-route each unrouted net ALONE (fresh occupancy, only the pad
    obstacles) and report how many find a path. Routable-alone ⇒ the net's path
    exists and the router's *negotiation* failed to deconflict it (a router-quality
    gap); not-routable-alone ⇒ genuinely blocked by pads/planes (a resource gap)."""
    import sys

    alone_ok = 0
    for net in unrouted:
        # The halo model alone, as documented: what negotiation could not separate.
        r = route(grid, {net: net_access[net]}, max_iters=6, via_keepout=via_keepout, exact="off")
        if not r.unrouted:
            alone_ok += 1
    sys.stderr.write(
        "DIAG unrouted=%d: routable-alone=%d (negotiation-limited), "
        "blocked-alone=%d (resource-limited)\n"
        % (len(unrouted), alone_ok, len(unrouted) - alone_ok)
    )


def _via_model(rules, layers, grid, fab, via_keepout, graph=None):
    """The grid's via model (pnr.via_policy.GridVias) from the rules' via policy,
    or None (through vias only: the router's legacy model, unchanged). A policy
    without a build (pnr.via_policy.select_build) gets one from ``graph`` here."""
    policy = (rules or {}).get("via_policy")
    if not policy:
        return None
    from pnr.via_policy import GridVias, board_needs, select_build

    if "build" not in policy and graph is not None:
        build = select_build(
            policy,
            board_needs(graph, rules, policy["layers"]),
            (fab["via_diameter_mm"], fab["via_drill_mm"]),
        )
        _warn_once(["via policy: no build given; chosen from this board: %s" % build["spans"]])
        if not build["spans"]:
            return None
        policy = dict(policy, build=build)

    missing = [name for name in layers if name not in policy["layers"]]
    if missing:
        _warn_once(
            [
                "the via policy's copper layers (%s) lack routed layer(s) %s: through vias "
                "only" % (", ".join(policy["layers"]), ", ".join(missing))
            ]
        )
        return None
    _warn_once(["via policy: %s" % text for text in policy.get("warnings", [])])
    return GridVias(
        policy,
        layers,
        grid.pitch,
        grid.clearance,
        (fab["via_diameter_mm"], fab["via_drill_mm"]),
        via_keepout,
    )


def _drop_span(vm, stack):
    """``drop_span(net, side)``: a plane drop from grid layer ``side`` is the via to
    the net's dedicated plane nearest that layer (the shortest span), or None with
    no via model or no stack (a through drop)."""
    if vm is None or stack is None:
        return None
    names = vm.model.layers

    def drop_span(net, side):
        planes = stack.net_planes(net)
        if not planes:
            return None
        here = vm.stack_index[side]
        nearest = min(planes, key=lambda la: (abs(names.index(la) - here), names.index(la)))
        return vm.to_layer(side, nearest)

    return drop_span


def tie_plane_layers(grid, graph, stack, plan, result, plane_access, via_keepout, net_halo, rule):
    """Tie the plane layers of a net with several (two ground planes), whose blind
    or micro drops reach only the plane nearest each pad (pnr.via_policy).

    Return ties: a signal via whose ends are referenced (the plane nearest above
    and below each end, pnr.via_policy.reference_planes) to two plane layers of
    one net needs a via of that net joining both within ``rule["max_mm"]`` (the
    policy's ``return_tie``) of it. A plated pad, a drop or an earlier tie there
    serves; else the nearest drop of the net within reach is extended to the
    deeper span, else a tie via of the build's span joining the two planes goes
    at the clear site nearest the signal via. Floor: every plane layer of such a
    net is joined at least once (a drop extended, else a tie beside one). Every
    new via is checked like a drop (exact pads, escape copper, its own net's
    lands, the net's plane fill on each plane it must join), on its grid layers
    clear of every routed net's reserved cells, and clear of every other net's
    drill by the hole spacing (never on one, whatever the spans).

    Returns ``(ties, report)``: ties ``[(net, (x, y), span)]``; the report gives
    ``plane_layer_connections`` (every dedicated plane layer: the plated pads
    and vias joining it) and ``return_ties`` (the rule, the signal vias that
    need a tie, those met, the ties added and drops extended, each unmet one
    with its nearest tie, and the signal vias whose two references are planes of
    different nets, which only decoupling can bridge)."""
    from collections import defaultdict

    from pnr.via_policy import reference_planes, return_tie_rule

    from .joint_escape import _drop_via_clear, _outside_own_lands
    from .maze import _footprint

    vm = grid.via_model
    rule = rule or return_tie_rule()
    names = vm.model.layers
    last = len(names) - 1
    dedicated = list(stack.dedicated)
    plane_nets = set(stack.plane_nets)
    multi = {net: stack.net_planes(net) for net in sorted(plane_nets)}
    multi = {net: planes for net, planes in multi.items() if len(planes) > 1}
    limit = float((rule or {}).get("max_mm") or 0.0)

    # Every via joining each plane net's layers: [xy, top, bottom, drop escape].
    ties = defaultdict(list)
    for e in plan.escapes:
        if e.net in plane_nets and e.kind == "joint" and e.via_xy is not None:
            span = e.via_span or vm.full
            ties[e.net].append([tuple(e.via_xy), span.t, span.b, e])
    for _layers, net, centre, _radius, plated in grid.drilled_pads:
        if plated and net in plane_nets:
            ties[net].append([tuple(centre), 0, last, None])

    occupied = {}
    holes = []
    for name, rn in result.nets.items():
        if rn.cells:
            for c in _footprint(grid, rn.cells, via_keepout, net_halo.get(name, 0)):
                occupied.setdefault((c.layer, c.i, c.j), set()).add(name)
        holes.extend((name, grid.center_of(i, j)) for i, j in rn.vias)

    def clear(net, p, span, layers):
        # Each plane layer ``layers`` the via must join has the net's fill at p.
        for layer in layers:
            if plane_access is not None and plane_access.constrained(net, (layer,)):
                if not plane_access.site_ok(net, p, (layer,)):
                    return False
        if not _drop_via_clear(grid, net, p, span):
            return False
        if not _outside_own_lands(grid, net, p, None, span.radius):
            return False
        if not grid.hole_site_clear(p, net=net):
            return False
        for owner, q in holes:
            gap = math.dist(p, q)
            if gap < grid.via_spacing - 1e-7 and (owner != net or gap >= 1e-7):
                return False
        i, j = grid.cell_of(*p)
        k = span.keepout
        for la in span.layers():
            for di in range(-k, k + 1):
                for dj in range(-k, k + 1):
                    if occupied.get((la, i + di, j + dj), set()) - {net}:
                        return False
        return True

    added = []

    def place(net, q, span):
        grid.escape_vias.append((net, q))
        grid.escape_via_spans[(net, q)] = span
        holes.append((net, q))
        ties[net].append([q, span.t, span.b, None])
        added.append((net, q, span))

    def extend(net, tie, lo, hi):
        e = tie[3]
        span = e.via_span or vm.full
        deeper = vm.stack_span(min(span.t, lo), max(span.b, hi))
        new = [la for la in multi[net] if vm.reaches(deeper, la) and not vm.reaches(span, la)]
        if not new or not clear(net, tie[0], deeper, new):
            return False
        e.via_span = deeper
        grid.escape_via_spans[(net, tie[0])] = deeper
        tie[1], tie[2] = deeper.t, deeper.b
        return True

    # The signal vias, with the grid layers each joins (plated pads excluded).
    grid_names = list(grid.layers)
    signal = {}
    for name, rn in result.nets.items():
        if name in plane_nets or not rn.vias:
            continue
        columns = defaultdict(set)
        for c in rn.cells:
            columns[(c.i, c.j)].add(c.layer)
        for i, j in rn.vias:
            if grid.plated_transition(name, i, j) is not None:
                continue
            used = columns.get((i, j), set())
            if len(used) > 1:
                signal.setdefault((name, grid.center_of(i, j)), set()).update(used)
    for e in plan.escapes:
        if e.net in plane_nets or e.via_xy is None or e.access is None:
            continue
        used = {e.access.layer}
        if e.side_layer in grid_names:
            used.add(grid_names.index(e.side_layer))
        if len(used) > 1:
            signal.setdefault((e.net, tuple(e.via_xy)), set()).update(used)

    needs = []
    cross = 0
    for (net, p), used in sorted(signal.items()):
        refs = set()
        for la in used:
            refs.update(reference_planes(names, dedicated, grid_names[la]))
        if len({n for _, n in refs}) > 1:
            cross += 1
        for plane_net in sorted({n for _, n in refs} & set(multi)):
            joined = sorted({names.index(la) for la, n in refs if n == plane_net})
            if len(joined) > 1:
                needs.append((plane_net, p, joined[0], joined[-1], net))

    reach = max(1, int(limit / grid.pitch))
    offsets = sorted(
        ((di, dj) for di in range(-reach, reach + 1) for dj in range(-reach, reach + 1)),
        key=lambda d: (d[0] ** 2 + d[1] ** 2, d),
    )
    met = extended = 0
    unmet = []
    for plane_net, p, lo, hi, net in needs:
        covering = [t[0] for t in ties[plane_net] if t[1] <= lo and t[2] >= hi]
        near = min((math.dist(p, q) for q in covering), default=None)
        if near is not None and near <= limit + 1e-9:
            met += 1
            continue
        done = False
        drops = sorted(
            (t for t in ties[plane_net] if t[3] is not None and math.dist(p, t[0]) <= limit),
            key=lambda t: (math.dist(p, t[0]), t[0]),
        )
        for tie in drops:
            if extend(plane_net, tie, lo, hi):
                extended += 1
                done = True
                break
        if not done:
            span = vm.stack_span(lo, hi)
            joins = [la for la in multi[plane_net] if vm.reaches(span, la)]
            ci, cj = grid.cell_of(*p)
            for di, dj in offsets:
                if not grid.in_bounds(ci + di, cj + dj):
                    continue
                q = grid.center_of(ci + di, cj + dj)
                if math.dist(p, q) > limit + 1e-9:
                    continue
                if clear(plane_net, q, span, joins):
                    place(plane_net, q, span)
                    done = True
                    break
        if done:
            met += 1
        else:
            unmet.append(
                dict(
                    net=plane_net,
                    signal=net,
                    xy=[round(p[0], 4), round(p[1], 4)],
                    nearest_mm=None if near is None else round(near, 3),
                )
            )

    # Floor: every plane layer of a net with several is joined at least once.
    beside = sorted(
        ((di, dj) for di in range(-12, 13) for dj in range(-12, 13)),
        key=lambda d: (d[0] ** 2 + d[1] ** 2, d),
    )
    for net, planes in multi.items():
        for layer in planes:
            index = names.index(layer)
            if any(t[1] <= index <= t[2] for t in ties[net]):
                continue
            drops = sorted((t for t in ties[net] if t[3] is not None), key=lambda t: t[0])
            if any(extend(net, tie, index, index) for tie in drops):
                extended += 1
                continue
            reached = [
                la
                for la in planes
                if la != layer and any(t[1] <= names.index(la) <= t[2] for t in ties[net])
            ]
            if not reached:
                continue
            other = min(reached, key=lambda la: (abs(names.index(la) - index), la))
            span = vm.stack_span(names.index(other), index)
            done = False
            for tie in drops:
                ci, cj = grid.cell_of(*tie[0])
                for di, dj in beside:
                    if not grid.in_bounds(ci + di, cj + dj):
                        continue
                    q = grid.center_of(ci + di, cj + dj)
                    if clear(net, q, span, (other, layer)):
                        place(net, q, span)
                        done = True
                        break
                if done:
                    break

    counts = {}
    for layer, net in dedicated:
        index = names.index(layer)
        counts.setdefault(net, {})[layer] = sum(t[1] <= index <= t[2] for t in ties[net])
    report = dict(
        plane_layer_connections=counts,
        return_ties=dict(
            rule=dict(rule or {}),
            required=len(needs),
            met=met,
            ties_added=len(added),
            drops_extended=extended,
            unmet=unmet,
            reference_net_changes=cross,
        ),
    )
    floating = sorted(
        "%s %s" % (layer, net)
        for net, layers in counts.items()
        for layer, n in layers.items()
        if not n
    )
    if floating:
        report["floating_planes"] = floating
        _warn_once(
            ["plane layer %s has no via or plated pad: it floats" % text for text in floating]
        )
    return added, report


def detail_pitch(explicit, track_width_mm, clearance_mm):
    """Resolve one pitch for source screening and fixed-copper signal handoff.

    The grid changes sampling only. Copper widths, clearance halos and native
    validation remain unchanged. An explicit function/CLI value takes precedence.
    """
    value = explicit
    if value is None:
        configured = os.environ.get("PNR_DETAIL_PITCH_MM")
        if configured is not None:
            value = float(configured)
    if value is None or value == 0:
        value = round((track_width_mm + clearance_mm + 0.02) / 0.05) * 0.05
    if not math.isfinite(value) or value <= 0:
        raise ValueError("detail grid pitch must be positive and finite, or 0 for auto")
    return value


def escape_reach_cells(pitch: float, track_width_mm: float, clearance_mm: float) -> int:
    """Preserve the nominal physical fanout search reach on finer routing grids."""
    if pitch <= 0 or track_width_mm <= 0 or clearance_mm < 0:
        raise ValueError("positive pitch/width and nonnegative clearance required")
    nominal_pitch = round((track_width_mm + clearance_mm + 0.02) / 0.05) * 0.05
    return max(4, math.ceil(4 * nominal_pitch / pitch - 1e-9))


def route_board(
    graph: BoardGraph,
    constraints: CompiledConstraints,
    rules: Optional[dict] = None,
    *,
    pitch: Optional[float] = None,
    track_width_mm: Optional[float] = None,
    max_iters: int = 12,
    ripup_rounds: int = 12,
    escape_via_in_pad: bool = True,
    escape_dogbone: bool = True,
    fixed_copper: Optional[dict] = None,
    fixed_copper_own_net: bool = False,
) -> BoardRoute:
    """Detailed-route the signal nets of a placed ``graph``.

    ``rules`` (the routing rules dict) names the plane nets, which are left to the
    plane pour + fanout (they connect via a via drop, not signal routing), and
    carries the **fab profile** (track/clearance/via geometry — local DRC
    relaxation). The rest are negotiated-routed on the grid. ``pitch``/
    ``track_width_mm`` default to the fab profile: the grid pitch is the DRC-clean
    floor ``track + clearance`` (a tighter fab ⇒ finer pitch ⇒ better escape).
    Returns a :class:`BoardRoute` with grid + mm geometry.

    ``fixed_copper`` is existing copper kept as it is (:mod:`.fixed`); with
    ``fixed_copper_own_net`` its own net may reach and pass it (the hierarchical
    knit joins pads that block copper already connects), other nets may not.
    """
    if fixed_copper is None and rules and rules.get("fixed_copper"):
        # Fixed blocks' copper carried in the rules (pnr.fixed_block): every route of
        # the board, the placement loop's included, keeps it.
        fixed_copper = rules["fixed_copper"]
    fab = _fab(rules)
    track_width_mm = fab["track_width_mm"] if track_width_mm is None else track_width_mm
    clearance_mm = fab["clearance_mm"]
    via_radius_mm = fab["via_diameter_mm"] / 2.0
    # Per-net track width from the net classes (type/amperage), default = fab width.
    net_width = _net_widths(rules, track_width_mm)
    pitch = detail_pitch(pitch, track_width_mm, clearance_mm)
    # Every net reserves enough track halo for this pitch, including signals:
    # other-net centre must be ≥ width/2 + clearance + ½signal from this net's cells.
    net_halo = {
        n: _track_halo(w, track_width_mm, clearance_mm, pitch)
        for n in (net.name for net in graph.nets)
        for w in (net_width.get(n, track_width_mm),)
    }

    # Via keep-out radius derived from the fab geometry, NOT hardcoded: two vias
    # must clear by ``via_diameter + clearance`` centre-to-centre, so the nearest
    # allowed other-net via sits ⌈(via_d+clr)/pitch⌉ cells away ⇒ keep-out radius one
    # less. (Default 0.45/0.13/0.30 ⇒ 1; a tighter fab needs a wider halo.)
    via_keepout = max(1, math.ceil((2 * via_radius_mm + clearance_mm) / pitch) - 1)

    width, height = outline_size(graph, constraints)
    layers, planes, stack = layer_plan(graph, rules)
    grid = RouteGrid.from_graph(
        graph,
        width,
        height,
        pitch=pitch,
        layers=layers,
        clearance=clearance_mm,
        track_width=track_width_mm,
        via_radius=via_radius_mm,
    )
    grid.net_widths = net_width
    grid.via_model = vm = _via_model(rules, layers, grid, fab, via_keepout, graph)
    if stack is not None:
        grid.layer_mask = (
            current_layer_mask(stack, layers, rules, net_width, track_width_mm) or None
        )
    grid.net_clearances = _net_clearances(rules)
    grid.reserve_wide_pad_clearance()
    # Fab-profile per-hole-kind rules ride in rules['fab'] beside the 5 keys
    # _fab() keeps; absent (legacy rules) they leave the original model intact.
    extra = dict((rules or {}).get("fab") or {})
    grid.via_spacing = fab["via_drill_mm"] + extra.get(
        "hole_to_hole_mm", fab.get("hole_clearance_mm", 0.2)
    )
    grid.via_drill_radius = fab["via_drill_mm"] / 2
    grid.hole_clearance = fab.get("hole_clearance_mm", 0.2)
    if extra.get("component_pth_min_drill_mm") is not None:
        # Footprint via-class drills (under 5A Component PTH hole 0.30) get via rules.
        grid.component_pth_min_drill = float(extra["component_pth_min_drill_mm"])
        grid.via_hole_gap = float(extra.get("hole_to_hole_mm", grid.hole_clearance))
    if extra.get("pth_hole_to_hole_mm") is not None:
        grid.pth_hole_gap = float(extra["pth_hole_to_hole_mm"])
        grid.npth_hole_gap = float(
            extra.get("filled_via_hole_to_hole_mm", extra["pth_hole_to_hole_mm"])
        )
    if extra.get("pth_hole_clearance_mm") is not None:
        grid.mark_pth_hole_keepouts(float(extra["pth_hole_clearance_mm"]), grid.hole_clearance)
    if extra.get("via_to_smd_pad_mm") is not None:
        # Fab profile: 5A via copper 0.127 from any SMD pad; inside an own-net pad
        # only a 5B filled in-pad via (sized at emission, pnr.fixed_copper).
        from pnr.fab_profile import in_pad_policy

        grid.restrict_smd_vias(in_pad_policy(extra), float(extra["via_to_smd_pad_mm"]))
    if extra.get("hole_to_edge_mm") is not None:
        edge = float(extra.get("edge_clearance_mm", 0.2))
        grid.block_edge_inset_split(
            edge + track_width_mm / 2,
            max(edge + via_radius_mm, float(extra["hole_to_edge_mm"]) + fab["via_drill_mm"] / 2),
        )
    # Split planes on the inner layers become obstacles the signals route around
    # (matching the 2 mm writeback pour margin).
    _mark_plane_regions(grid, graph, rules, margin=2.0, stack=stack)
    _mark_copper_keepouts(grid, graph, rules)
    _mark_source_arrays(grid, graph, rules)

    # Plan a pin escape per pad (E2 via-in-pad / E3 dog-bone) — the access cell the
    # maze routes each net from, plus the escape geometry that bonds pad→access.
    signal_nets = {net.name for net in graph.nets if net.name not in planes and net.degree >= 2}
    deferred = set()
    if rules and rules.get("electrical_fab"):
        from pnr.electrical import net_policy

        deferred = {n for n in signal_nets if net_policy(n, rules)["mode"] in ("power", "pair")}
        # These jobs require the native capacity/coupling adapters. The signal
        # grid must not emit undersized vias or independent differential legs.
        signal_nets -= deferred
    if fixed_copper:
        from .fixed import reserve_fixed_copper

        reserve_fixed_copper(
            grid,
            fixed_copper,
            max([track_width_mm] + [net_width.get(n, track_width_mm) for n in signal_nets]),
            **({"own_net": True} if fixed_copper_own_net else {}),
        )
    # Fixed blocks join their nets at free ends of their copper (route_board's
    # targets besides the pads); a net with one pad and a port is routed too.
    ports = block_ports(grid, graph, fixed_copper, planes | deferred)
    for net in ports:
        signal_nets.add(net)
    # Stack-aware: every surface pad of a net with a dedicated plane drops a through
    # via to it, planned jointly with the signal exits (no drop is left to after
    # routing). Each stub carries its own pad's required entry width, and its via
    # must land where the net's own plane fills (a layer shared by several nets
    # splits into regions, pnr.stack.plane_regions).
    drop_widths = {}
    pad_drop_width = {}
    plane_access = None
    if stack is not None:
        from pnr.pad_entry import terminal_required_width
        from pnr.stack import PlaneAccess, pad_points, plane_regions

        for comp in graph.components:
            for pad in comp.pads:
                if pad.net in stack.plane_nets and not pad.through_hole:
                    w = terminal_required_width(comp.ref, pad.name, pad.net, rules or {})
                    pad_drop_width[(comp.ref, pad.name)] = w
                    drop_widths[pad.net] = max(drop_widths.get(pad.net, 0.0), w)
        for n, w in drop_widths.items():
            net_width.setdefault(n, w)
        plane_access = PlaneAccess(
            stack,
            plane_regions(stack, graph.stack, pad_points(graph), width, height),
            inset=via_radius_mm,
            outset=via_radius_mm + clearance_mm + fab["track_width_mm"],
        )
    drop_span = _drop_span(vm, stack) if drop_widths else None
    plan = plan_escapes(
        grid,
        graph,
        signal_nets,
        via_keepout=via_keepout,
        allow_via_in_pad=escape_via_in_pad,
        allow_dogbone=escape_dogbone,
        dogbone_reach=escape_reach_cells(grid.pitch, track_width_mm, clearance_mm),
        joint=os.environ.get("PNR_JOINT_ACCESS", "1") != "0",
        joint_max_options=int(os.environ.get("PNR_JOINT_ACCESS_OPTIONS", "16")),
        joint_max_states=int(os.environ.get("PNR_JOINT_ACCESS_STATES", "20000")),
        joint_max_cluster_size=int(os.environ.get("PNR_JOINT_ACCESS_CLUSTER", "24")),
        drop_widths=drop_widths or None,
        drop_in_pad=grid.in_pad is not None,
        drop_pad_width=pad_drop_width,
        plane_access=plane_access,
        drop_span=drop_span,
    )
    for net, cells in sorted(ports.items()):
        if net not in plan.blocked_nets:
            plan.net_access.setdefault(net, []).extend(cell for cell, _, _ in cells)
    if ports:
        plan.diagnostics["block_ports"] = {
            net: [[layer, round(p[0], 6), round(p[1], 6)] for _, p, layer in cells]
            for net, cells in sorted(ports.items())
        }
    from pnr.stack import assess

    stack_warnings = list(assess(rules, getattr(graph, "stack", None))[1])
    if stack_warnings:
        plan.diagnostics["stack_warnings"] = stack_warnings
    net_access = {
        n: cells
        for n, cells in plan.net_access.items()
        if len(cells) >= 2 and n not in plan.blocked_nets and n not in drop_widths
    }
    # PNR_TRACE_DIR only: records this route inside a traced route scope (pnr.trace).
    from .trace_route import start as trace_start

    route_trace = trace_start(
        graph, grid, plan, net_width, track_width_mm, planes, deferred, max_iters
    )

    # Price a layer transition in physical distance so finer grids do not
    # accidentally make short via excursions cheaper than surface detours.
    result = route(
        grid,
        net_access,
        max_iters=max_iters,
        via_keepout=via_keepout,
        net_halo=net_halo,
        rrr_rounds=ripup_rounds,
        via_cost=3.0 / grid.pitch,
        late_copper=_late_copper(graph, planes, deferred, plan.escapes),
    )

    if deferred or plan.blocked_nets:
        from .maze import RoutedNet

        for name in sorted(deferred | plan.blocked_nets):
            result.nets[name] = RoutedNet(name)
        result.unrouted = sorted(set(result.unrouted) | deferred | plan.blocked_nets)

    if os.environ.get("PNR_DIAG_UNROUTED"):
        _diag_unrouted(grid, net_access, result.unrouted, via_keepout)

    # Blind and micro drops reach only the nearest plane of their net: a net with
    # several plane layers (two ground planes) gets return ties (and every plane
    # layer at least one via); each plane layer's joins are reported.
    stitches = []
    if vm is not None:
        plan.diagnostics["via_build"] = vm.model.policy.get("build")
    if drop_span is not None:
        stitches, report = tie_plane_layers(
            grid,
            graph,
            stack,
            plan,
            result,
            plane_access,
            via_keepout,
            net_halo,
            vm.model.policy.get("return_tie"),
        )
        plan.diagnostics.update(report)

    board = BoardRoute(
        result=result,
        grid=grid,
        plane_nets=planes,
        failure_sites=trapped_access_sites(grid, plan.net_access, result.unrouted),
        escape_diagnostics=plan.diagnostics,
        stack_warnings=stack_warnings,
    )
    for net, sites in plan.drop_failures.items():
        # A plane pad without a drop is localized at the pad for the placement loop.
        board.failure_sites[net] = sorted(set(board.failure_sites.get(net, [])) | set(sites))
    if os.environ.get("PNR_LOCAL_PRESSURE") == "1":
        from .pressure import localized_pressure

        board.pressure_events = localized_pressure(grid, net_access, result, deferred)
    layer_names = grid.layers
    routed_names = {n for n, rn in result.nets.items() if rn.routed}
    # (net, x, y) -> the spans of the vias there (via model only), merged below.
    span_at = {} if vm is not None else None
    for name, rn in result.nets.items():
        w = net_width.get(name, track_width_mm)  # per-net (type/amperage) width
        for layer, (i0, j0), (i1, j1) in rn.segments:
            x0, y0 = grid.center_of(i0, j0)
            x1, y1 = grid.center_of(i1, j1)
            board.tracks.append((name, layer_names[layer], (x0, y0), (x1, y1), w))
        new_vias = []
        for i, j in rn.vias:
            x, y = grid.center_of(i, j)
            existing_pad = grid.plated_transition(name, i, j)
            if existing_pad is not None:
                # This layer transition uses native PTH plating. Bond the exact
                # terminal centre on every visited layer, without a second hole.
                for layer in sorted({c.layer for c in rn.cells if (c.i, c.j) == (i, j)}):
                    if math.dist(existing_pad, (x, y)) > 1e-7:
                        board.tracks.append((name, layer_names[layer], existing_pad, (x, y), w))
            else:
                board.vias.append((name, x, y))
                new_vias.append((i, j))
                if span_at is not None:
                    span_at.setdefault((name, x, y), []).extend(
                        rn.via_spans.get((i, j)) or [vm.full]
                    )
        rn.vias = new_vias
    for net, (x, y), span in stitches:
        board.vias.append((net, x, y))
        span_at.setdefault((net, x, y), []).append(span)

    board.deferred_nets = deferred

    # Emit each routed pad's escape geometry (on-layer stub, via-in-pad, or dog-bone
    # stub + via) so the net is electrically whole from the real pad centre.
    for esc in plan.escapes:
        if esc.net in drop_widths:
            # A plane drop needs no maze route: its via reaches the plane(s).
            _emit_escape(board, esc, grid, esc.width or drop_widths[esc.net], span_at)
            continue
        rn = result.nets.get(esc.net)
        if rn is None or esc.access not in set(rn.cells):
            continue
        _emit_escape(board, esc, grid, net_width.get(esc.net, track_width_mm), span_at)
    for net, cells in sorted(ports.items()):
        # The block port's exact end to the cell centre where the route begins.
        rn = result.nets.get(net)
        reached = set(rn.cells) if rn is not None else set()
        for cell, point, layer in cells:
            if cell in reached:
                centre = grid.center_of(cell.i, cell.j)
                w = net_width.get(net, track_width_mm)
                board.tracks.append((net, layer, tuple(point), centre, w))
    # Zero-length pad-to-grid stubs add no connection and become dangling items.
    board.tracks = [t for t in board.tracks if math.dist(t[2], t[3]) >= 1e-6]
    board.vias = list(dict.fromkeys(board.vias))
    if vm is not None:
        # One barrel per same-net site whose spans share a copper layer (two holes
        # there would be co-located); a through via needs no entry.
        for key in board.vias:
            for span in vm.merged(span_at.get(key) or [vm.full]):
                if not span.through:
                    board.via_spans.append((*key, span.top, span.bottom, span.kind))
    if rules and (rules.get("diff_pairs") or rules.get("length_match")):
        # Length-match the declared pairs and groups on the finished route (fixed
        # copper counts in its net's length, unchanged).
        from .tune import meanders_enabled, tune_board

        if meanders_enabled(rules):
            board.length_report = tune_board(
                board,
                graph,
                grid,
                rules,
                net_width=net_width,
                default_width=track_width_mm,
                net_halo=net_halo,
                via_keepout=via_keepout,
                access=net_access,
                via_radius=via_radius_mm,
                fixed_copper=_flat(fixed_copper),
            )
    if route_trace is not None:
        route_trace.end(board)
    return board


def _flat(copper):
    """Fixed copper as straight tracks and vias (arcs as chords, blocks merged) for
    the length tuner; schema-1 copper unchanged."""
    from pnr.fixed_block import flatten

    return flatten(copper)


def _emit_escape(board: BoardRoute, esc, grid: RouteGrid, w: float, span_at=None) -> None:
    """Append the mm-space geometry that bonds a pad to its maze access cell.
    ``span_at`` ((net, x, y) -> spans, a grid with a via model) gets each via's
    span (a planned escape's own; the legacy planner's vias are through)."""
    access_ctr = grid.center_of(esc.access.i, esc.access.j)
    access_layer = grid.layers[esc.access.layer]
    if esc.kind == "blocked":
        return
    if span_at is not None and esc.via_xy is not None:
        span = esc.via_span if esc.kind == "joint" and esc.via_span else grid.via_model.full
        span_at.setdefault((esc.net, *esc.via_xy), []).append(span)
    if esc.kind == "joint":
        for layer, a, b in esc.segments:
            board.tracks.append((esc.net, layer, a, b, w))
        if esc.via_xy is not None:
            board.vias.append((esc.net, *esc.via_xy))
    elif esc.kind == "offgrid":
        for a, b in zip(esc.stub_path, esc.stub_path[1:]):
            board.tracks.append((esc.net, esc.side_layer, a, b, w))
    elif esc.kind == "via_in_pad":
        # Via in the pad (side ↔ access layer); short stub on the access layer to the
        # cell centre where the maze route begins.
        board.vias.append((esc.net, esc.via_xy[0], esc.via_xy[1]))
        if esc.pad_xy != access_ctr:
            board.tracks.append((esc.net, access_layer, esc.pad_xy, access_ctr, w))
    elif esc.kind == "dogbone":
        # Stub outward on the pad's layer to the offset cell; via there if the maze
        # route continues on another layer.
        board.tracks.append((esc.net, esc.side_layer, esc.pad_xy, esc.stub_to, w))
        if esc.via_xy is not None:
            board.vias.append((esc.net, esc.via_xy[0], esc.via_xy[1]))
    else:  # onlayer — the classic pin-access stub (pad centre → its cell centre)
        if esc.pad_xy != access_ctr:
            board.tracks.append((esc.net, esc.side_layer, esc.pad_xy, access_ctr, w))
