"""Immutable existing copper reservations in the graph's millimetre frame.

These are obstacles only: they do not assert that any net is complete. Native
connectivity remains authoritative. Layer-specific tracks reserve only that
layer, while through-vias reserve the entire stack including drill spacing.

``own_net=True`` (the hierarchical knit, ``route_board(fixed_copper_own_net=True)``):
a fixed track's clearance cells are owned by its net instead of blocked for every
net, with the pad rule of :meth:`pnr.route.detail.grid.RouteGrid.add_pad`: foreign
nets are kept out, the own net may pass (to reach a pad the fixed copper already
joins), and cells two nets claim block both. Fixed vias stay hard via obstacles for
every net (hole spacing applies within a net too); their copper is owned the same way.
"""

import math

from pnr.writeback import _segment_distance_sq


def reserve_fixed_copper(grid, copper, route_width=None, own_net=False):
    if copper.get("frame") != "engine-mm-y-up":
        raise ValueError("fixed copper requires explicit engine frame")
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

        def claim(layer, i, j):
            key = (layer, i, j)
            owner = table.get(key)
            table[key] = net if owner is None or owner == net else "\0conflict"

        return claim

    for net, layer, a, b, width in copper.get("tracks", []):
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
        if via.get("type") != "through":
            raise ValueError("only through fixed vias supported")
        net = via.get("net") if own_net else None
        for la in range(grid.nlayers):
            copper_table = owned(grid.pad_net, net) if net else grid.blocked
            reserve(la, p, p, diameter / 2 + grid.clearance + track_radius, copper_table)
            drill_clear = (drill + 2 * grid.via_radius) / 2 + grid.clearance
            reserve(
                la,
                p,
                p,
                max(diameter / 2 + grid.clearance + grid.via_radius, drill_clear),
                grid.via_blocked,
            )
