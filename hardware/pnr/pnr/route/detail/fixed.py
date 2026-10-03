"""Immutable existing copper reservations in the graph's millimetre frame.

These are obstacles only: they do not assert that any net is complete. Native
connectivity remains authoritative. Layer-specific tracks reserve only that
layer, while through-vias reserve the entire stack including drill spacing. A
blind, buried or micro via (``type`` with ``layers``: its top and bottom copper)
reserves its copper only on the grid layers inside its span; its drill spacing
still applies on every layer (conservative).

``own_net=True`` (the hierarchical knit, ``route_board(fixed_copper_own_net=True)``):
a fixed track's clearance cells are owned by its net instead of blocked for every
net, with the pad rule of :meth:`pnr.route.detail.grid.RouteGrid.add_pad`: foreign
nets are kept out, the own net may pass (to reach a pad the fixed copper already
joins), and cells two nets claim block both. Fixed vias stay hard via obstacles for
every net (hole spacing applies within a net too); their copper is owned the same way.

Schema 2 (:mod:`pnr.fixed_block`) adds arcs, reserved as chords whose sagitta is at most
1 um, each a capsule ``width / 2 + 1 um`` wide (conservative by at most 1 um), and fixed
blocks. A block's items are always owned by their nets, whatever ``own_net`` says (it
applies to the top-level items only); its pad and solid-zone polygons are owned the same
way (a polygon on a layer the grid does not route keeps foreign vias out of it on every
layer), and its rule areas block what their flags forbid on their layers. Block claims
are mirrored in ``grid.fixed_owned`` for the exact escape tests.
"""

import math
import re

from pnr.writeback import _segment_distance_sq


def reserve_fixed_copper(grid, copper, route_width=None, own_net=False):
    if copper.get("frame") != "engine-mm-y-up":
        raise ValueError("fixed copper requires explicit engine frame")
    _reserve(grid, copper, route_width, own_net)
    for block in copper.get("blocks") or []:
        _reserve(grid, dict(block, frame=copper["frame"]), route_width, True, block=True)


def _reserve(grid, copper, route_width, own_net, block=False):
    from pnr.fixed_block import ARC_EPS_MM, arc_chords

    # Every cell intersecting the forbidden capsule is blocked. The half-cell
    # diagonal covers continuous grid steps as well as their endpoint centres.
    cell_radius = grid.pitch / math.sqrt(2)
    track_radius = (grid.track_width if route_width is None else route_width) / 2

    def reserve(layer, a, b, radius, table):
        grow = radius + cell_radius
        imin = max(0, int(math.floor((min(a[0], b[0]) - grow) / grid.pitch)))
        imax = min(grid.nx - 1, int(math.floor((max(a[0], b[0]) + grow) / grid.pitch)))
        jmin = max(0, int(math.floor((min(a[1], b[1]) - grow) / grid.pitch)))
        jmax = min(grid.ny - 1, int(math.floor((max(a[1], b[1]) + grow) / grid.pitch)))
        for j in range(jmin, jmax + 1):
            for i in range(imin, imax + 1):
                c = grid.center_of(i, j)
                if _segment_distance_sq(a, b, c, c) <= grow * grow:
                    if callable(table):
                        table(layer, i, j)
                    else:
                        table[layer, j, i] = True

    def owned(table, net):
        """A cell setter that gives the cell to ``net`` (two nets: nobody's)."""
        mirror_owned = block and table is grid.pad_net

        def claim(layer, i, j):
            key = (layer, i, j)
            owner = table.get(key)
            table[key] = net if owner is None or owner == net else "\0conflict"
            # No longer a pad-only halo: exact pad checks do not see fixed copper.
            for mirror in ("pad_track_halo", "pad_via_halo"):
                getattr(grid, mirror, {}).pop(key, None)
            if mirror_owned:
                held = grid.fixed_owned.get(key)
                grid.fixed_owned[key] = net if held is None or held == net else "\0conflict"

        return claim

    tracks = list(copper.get("tracks", []))
    for arc in copper.get("arcs") or []:
        if len(arc) != 6 or not all(math.isfinite(v) for v in [*arc[2], *arc[3], *arc[4], arc[5]]):
            raise ValueError("invalid fixed arc geometry")
        # Chords within ARC_EPS_MM of the arc, each ARC_EPS_MM wider on either side.
        tracks.extend(arc_chords(arc, ARC_EPS_MM))
    for net, layer, a, b, width in tracks:
        if not all(math.isfinite(v) for v in [*a, *b, width]) or width <= 0:
            raise ValueError("invalid fixed track geometry")
        if layer not in grid.layers:
            continue  # non-signal layer not traversed by this grid
        la = grid.layers.index(layer)
        if own_net and net:
            track_clear = width / 2 + grid.clearance + track_radius
            via_clear = width / 2 + grid.clearance + grid.via_radius
            reserve(la, a, b, track_clear, owned(grid.pad_net, net))
            reserve(la, a, b, via_clear, owned(grid.via_halo, net))
            continue
        reserve(la, a, b, width / 2 + grid.clearance + track_radius, grid.blocked)
        reserve(la, a, b, width / 2 + grid.clearance + grid.via_radius, grid.via_blocked)
    for via in copper.get("vias", []):
        p = via["xy"]
        diameter = via["diameter_mm"]
        drill = via["drill_mm"]
        if not all(math.isfinite(v) for v in [*p, diameter, drill]) or not 0 < drill < diameter:
            raise ValueError("invalid fixed via geometry")
        kind = via.get("type")
        if kind not in ("through", "blind", "buried", "micro"):
            raise ValueError("unsupported fixed via type %r" % kind)
        span = _span_layers(grid, via) if kind != "through" else range(grid.nlayers)
        net = via.get("net") if own_net else None
        for la in range(grid.nlayers):
            copper_table = owned(grid.pad_net, net) if net else grid.blocked
            if la in span:
                reserve(la, p, p, diameter / 2 + grid.clearance + track_radius, copper_table)
            drill_clear = (drill + 2 * grid.via_radius) / 2 + grid.clearance
            reserve(
                la,
                p,
                p,
                max(diameter / 2 + grid.clearance + grid.via_radius, drill_clear),
                grid.via_blocked,
            )
    if block:
        _reserve_polygons(grid, copper.get("polygons") or [], track_radius, cell_radius, owned)


def _reserve_polygons(grid, polygons, track_radius, cell_radius, owned):
    """A block's pad and solid-zone polygons (owned by their nets; no net: blocked
    for every net) and its rule areas (blocked as their flags say)."""
    for poly in polygons:
        kind = poly.get("kind")
        outline, holes = poly["outline"], poly.get("holes") or []
        if kind == "rule_area":
            names = poly.get("layers") or []
            layers = [grid.layers.index(n) for n in names if n in grid.layers]
            if poly.get("tracks") and layers:
                grid.block_polygon(
                    outline, holes, layers, track_radius + cell_radius, block_vias=False
                )
            if poly.get("vias") and names:
                grid.block_polygon(
                    outline, holes, None, grid.via_radius + cell_radius, block_tracks=False
                )
            continue
        if kind not in ("pad", "zone"):
            raise ValueError("unsupported fixed polygon kind %r" % kind)
        net = poly.get("net") or ""
        layer = poly["layer"]
        track_grow = grid.clearance + track_radius + cell_radius
        via_grow = grid.clearance + grid.via_radius + cell_radius
        if layer in grid.layers:
            la = grid.layers.index(layer)
            track_cells = grid.polygon_cells(outline, holes, track_grow)
            if net:
                claim = owned(grid.pad_net, net)
                for j, i in zip(*track_cells.nonzero()):
                    claim(la, int(i), int(j))
            else:
                grid.blocked[la] |= track_cells
            via_layers = [la]
        else:
            # A plane-layer copper polygon: foreign through vias stay out of it.
            via_layers = list(range(grid.nlayers))
        via_cells = grid.polygon_cells(outline, holes, via_grow)
        for la in via_layers:
            if net:
                claim = owned(grid.via_halo, net)
                for j, i in zip(*via_cells.nonzero()):
                    claim(la, int(i), int(j))
            else:
                grid.via_blocked[la] |= via_cells


def _copper_order(name):
    """Stack position of a copper layer by KiCad's standard names (F.Cu, In1.Cu ..
    InN.Cu, B.Cu)."""
    if name == "F.Cu":
        return 0
    if name == "B.Cu":
        return 10**6
    match = re.fullmatch(r"In(\d+)\.Cu", name)
    if not match:
        raise ValueError("not a copper layer name: %r" % name)
    return int(match.group(1))


def _span_layers(grid, via):
    """The grid layer indices inside a blind, buried or micro fixed via's span (its
    ``layers``: top and bottom copper names)."""
    layers = via.get("layers")
    if not layers or len(layers) != 2:
        raise ValueError("a fixed %s via needs its two copper layers" % via.get("type"))
    top, bottom = sorted(_copper_order(n) for n in layers)
    return {i for i, name in enumerate(grid.layers) if top <= _copper_order(name) <= bottom}
