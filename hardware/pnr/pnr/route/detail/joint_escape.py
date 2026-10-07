"""Geometry adapter for joint terminal-access assignment on the detailed grid.

Candidates start at the exact pad centre and retain their compiled net width.
This grid/rectangle model is conservative, not a replacement for native KiCad
DRC or the existing power, differential-pair and pad-entry acceptance gates.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from pnr.place.geometry import pad_rects
from pnr.writeback import _segment_distance_sq

from .grid import Cell, far_twins, pad_layer
from .joint_access import select_joint
from .keyhole import elbows, length
from .obstacle_index import near


@dataclass
class AccessOption:
    escape: object
    cost: float
    segments: tuple  # layer index, start, end, width
    vias: tuple  # only newly emitted via centres
    occupied: frozenset
    access_key: tuple
    bounds: tuple
    # Per via (aligned with ``vias``): its span under the grid's via model, or None
    # for a through via. Empty: every via is through.
    spans: tuple = ()


def _segment_clear(grid, net, layer, a, b, width, own=None, net_keepouts=True, offsets=True):
    """``a``-``b`` of ``width`` on ``layer`` clears the grid's obstacles for ``net``.
    ``net_keepouts=False`` leaves the copper keepouts' cell masks to the caller (a
    fanout's hand-over judges them exactly, as its planner does). ``offsets=False``
    reads the blocked mask on the centre line only: every obstacle in it is already
    grown by at least half a track (the caller judges rule areas exactly)."""
    radius = width / 2 + grid.clearance
    # Net class clearances (route_board's _net_clearances): two nets keep the larger
    # of theirs, as KiCad's DRC judges them, against pads and escape copper. A board
    # without class clearances keeps the fab clearance (the grid's) exactly.
    classes = getattr(grid, "net_clearances", None) or {}

    def reach(owner):
        if net not in classes and owner not in classes:
            return radius
        return width / 2 + max(grid.clearance, classes.get(net, 0.0), classes.get(owner, 0.0))

    # The blocked mask includes absolute keepouts, no-net pads, the edge inset
    # and retained copper. Exact foreign-pad checks below may relax pad *halos*,
    # but they may never relax this mask, except ``own``: the cells of the net's own
    # legacy plane region (PNR_COMPACT DROPS, RouteGrid.own_plane_cells; None: none).
    steps = max(1, math.ceil(math.dist(a, b) / (grid.pitch / 4)))
    # Copper keepouts with allow lists and fixed-block copper (both centreline
    # reservations, like the maze's): judged at the centre samples. Absent, nothing.
    keepouts = getattr(grid, "net_keepouts", None) if net_keepouts else None
    owned = getattr(grid, "fixed_owned", None)
    for step in range(steps + 1):
        x = a[0] + (b[0] - a[0]) * step / steps
        y = a[1] + (b[1] - a[1]) * step / steps
        for dx, dy in (
            ((0, 0), (radius, 0), (-radius, 0), (0, radius), (0, -radius)) if offsets else ((0, 0),)
        ):
            if not (0 <= x + dx <= grid.width and 0 <= y + dy <= grid.height):
                return False
            i, j = grid.cell_of(x + dx, y + dy)
            if grid.blocked[layer, j, i] and (own is None or (layer, j, i) not in own):
                return False
            if dx or dy:
                continue
            if keepouts and grid.net_blocked(net, layer, i, j):
                return False
            if owned:
                holder = owned.get((layer, i, j))
                if holder is not None and holder != net:
                    return False
    # A pad that sets its own clearance or mask margin (RouteGrid.pad_keepaways).
    keepaways = getattr(grid, "pad_keepaways", None) or {}
    # Only the obstacles that can come within reach of a-b (.obstacle_index): the
    # largest clearance any owner could be judged at, past the widest copper.
    widest = max(grid.clearance, max(classes.values(), default=0.0))
    pad_reach = width / 2 + max(widest, max(keepaways.values(), default=0.0))
    copper_reach = (
        width / 2
        + widest
        + max(grid.via_radius, max(grid.net_widths.values(), default=0.0) / 2, grid.track_width / 2)
    )
    pads = grid.pad_rectangles
    hits = near(pads, "rect", layer, a, b, pad_reach)
    for k in range(len(pads)) if hits is None else hits:
        la, owner, r = pads[k]
        if la != layer or owner == net:
            continue
        grow = reach(owner) if classes else radius
        if keepaways:
            keep = keepaways.get((la, owner, r))
            if keep is not None:
                grow = max(grow, width / 2 + keep)
        if (
            max(a[0], b[0]) + grow < r.left
            or min(a[0], b[0]) - grow > r.right
            or max(a[1], b[1]) + grow < r.bottom
            or min(a[1], b[1]) - grow > r.top
        ):
            continue
        if any(r.left <= p[0] <= r.right and r.bottom <= p[1] <= r.top for p in (a, b)):
            return False
        corners = [(r.left, r.bottom), (r.right, r.bottom), (r.right, r.top), (r.left, r.top)]
        if any(
            _segment_distance_sq(a, b, corners[k], corners[(k + 1) % 4]) < grow**2 - 1e-10
            for k in range(4)
        ):
            return False
    segments = grid.escape_segments
    hits = near(segments, "segment", layer, a, b, copper_reach)
    for k in range(len(segments)) if hits is None else hits:
        la, owner, c, d = segments[k]
        if la != layer or owner == net:
            continue
        other = grid.net_widths.get(owner, grid.track_width)
        grow = reach(owner) if classes else radius
        if grow is radius:
            limit = ((width + other) / 2 + grid.clearance) ** 2 - 1e-10
        else:
            limit = (other / 2 + grow) ** 2 - 1e-10
        if _segment_distance_sq(a, b, c, d) < limit:
            return False
    vias = grid.escape_vias
    hits = near(vias, "via", layer, a, b, copper_reach)
    for k in range(len(vias)) if hits is None else hits:
        owner, p = vias[k]
        if (
            owner != net
            and _segment_distance_sq(a, b, p, p)
            < (grid.via_radius + (reach(owner) if classes else radius)) ** 2 - 1e-10
        ):
            return False
    return True


def _via_clear(grid, net, p, via_keepout, span=None):
    """A new via of ``net`` at ``p`` clears foreign copper and holes on every grid
    layer, or only on ``span``'s layers (pnr.via_policy.Span) with its own size."""
    from .escape import _via_clean

    i, j = grid.cell_of(*p)
    layers = range(grid.nlayers) if span is None else span.layers()
    keepout = via_keepout if span is None else span.keepout
    radius = grid.via_radius if span is None else span.radius
    # The via sits exactly at p (a pad centre for via-in-pad): the fab profile's
    # via-to-SMD-pad rule is judged there, not at the cell centre.
    owner = net if getattr(grid, "via_model", None) is not None else None  # no shared site
    if not _via_clean(grid, i, j, net, keepout, p, layers) or not grid.hole_site_clear(
        p, net=owner
    ):
        return False
    # Checking a point with the via diameter reuses the exact foreign copper test
    # in every layer; via-blocked applies even when tracks may use an inner gap.
    keepouts = getattr(grid, "net_keepouts", None)
    return all(
        not grid.via_blocked[la, j, i]
        and not (keepouts and grid.net_blocked(net, la, i, j, via=True))
        and _segment_clear(grid, net, la, p, p, 2 * radius)
        for la in layers
    )


def _occupied(grid, segments, vias, via_keepout, spans=()):
    cells = set()
    pitch = grid.pitch
    for layer, a, b, width in segments:
        radius = (width + grid.track_width) / 2 + grid.clearance
        limit = radius**2 - 1e-10
        i0, j0 = grid.cell_of(min(a[0], b[0]) - radius, min(a[1], b[1]) - radius)
        i1, j1 = grid.cell_of(max(a[0], b[0]) + radius, max(a[1], b[1]) + radius)
        # _segment_distance_sq(a, b, p, p) with the same arithmetic, inlined: a
        # point never straddles the segment, so it is the least of the squared
        # distances to a, to b and to the segment.
        dx, dy = b[0] - a[0], b[1] - a[1]
        den = dx * dx + dy * dy
        for j in range(j0, j1 + 1):
            py = (j + 0.5) * pitch
            for i in range(i0, i1 + 1):
                px = (i + 0.5) * pitch
                t = max(0.0, min(1.0, ((px - a[0]) * dx + (py - a[1]) * dy) / den)) if den else 0.0
                if (
                    min(
                        (a[0] - px) ** 2 + (a[1] - py) ** 2,
                        (b[0] - px) ** 2 + (b[1] - py) ** 2,
                        (px - a[0] - t * dx) ** 2 + (py - a[1] - t * dy) ** 2,
                    )
                    < limit
                ):
                    cells.add((layer, i, j))
    for k, p in enumerate(vias):
        span = spans[k] if k < len(spans) else None
        keep = via_keepout if span is None else span.keepout
        i, j = grid.cell_of(*p)
        for la in range(grid.nlayers) if span is None else span.layers():
            for di in range(-keep, keep + 1):
                for dj in range(-keep, keep + 1):
                    if grid.in_bounds(i + di, j + dj):
                        cells.add((la, i + di, j + dj))
    return frozenset(cells)


def _make_option(grid, net, pad_xy, side, access, segments, vias, kind, via_keepout, spans=()):
    from .escape import Escape

    segments = tuple(segments)
    spans = tuple(spans)
    points = [p for _, a, b, _ in segments for p in (a, b)] + list(vias) + [pad_xy]
    margin = max(
        [grid.via_radius + grid.clearance] + [w / 2 + grid.clearance for _, _, _, w in segments]
    )
    bounds = (
        min(p[0] for p in points) - margin,
        min(p[1] for p in points) - margin,
        max(p[0] for p in points) + margin,
        max(p[1] for p in points) + margin,
    )
    escape = Escape(
        net=net,
        kind="joint",
        access=access,
        pad_xy=pad_xy,
        side_layer=grid.layers[side],
        via_xy=vias[0] if vias else None,
        segments=[(grid.layers[la], a, b) for la, a, b, _ in segments],
        via_span=spans[0] if spans else None,
    )
    # Via cost in millimetres, matching route_board's transition cost (a span's
    # multiplier under a via model). Surface exits remain cheaper; a shared
    # existing via is not charged as a new drill.
    if any(s is not None for s in spans):
        drills = sum(1.0 if s is None else s.cost for s in spans)
    else:
        drills = len(vias)
    cost = sum(math.dist(a, b) for _, a, b, _ in segments) + 3.0 * drills
    if kind == "reuse":
        cost += 0.05
    return AccessOption(
        escape,
        cost,
        segments,
        tuple(vias),
        _occupied(grid, segments, vias, via_keepout, spans),
        (access.layer, access.i, access.j),
        bounds,
        spans if any(s is not None for s in spans) else (),
    )


def enumerate_access(
    grid,
    net,
    pad_xy,
    side,
    *,
    through_hole=False,
    via_keepout=1,
    allow_via_in_pad=True,
    allow_dogbone=True,
    reach=4,
    max_options=16,
):
    """Enumerate qualified surface, plated-pad, existing-via and new-via exits.

    A surface candidate includes an actual centre-to-grid branch, never a trace
    that only grazes the pad. Hard source policy still decides whether this net
    reaches this signal adapter and whether new via-in-pad/dogbone access is legal.
    """
    from .escape import _has_free_neighbor

    width = grid.net_widths.get(net, grid.track_width)
    ci, cj = grid.cell_of(*pad_xy)
    layers = list(range(grid.nlayers)) if through_hole else [side]
    surface = []
    reuse = []
    new_vias = []
    portal_offsets = sorted(
        ((di, dj) for di in range(-reach, reach + 1) for dj in range(-reach, reach + 1)),
        key=lambda p: (p[0] ** 2 + p[1] ** 2, p),
    )
    if not allow_dogbone:
        portal_offsets = [(0, 0)]

    def paths(a, b, la):
        for path in sorted(elbows(a, b), key=lambda p: (length(p), len(p), p)):
            if all(_segment_clear(grid, net, la, x, y, width) for x, y in zip(path, path[1:])):
                yield tuple((la, x, y, width) for x, y in zip(path, path[1:]))

    def portals(la, origin=pad_xy, offsets=portal_offsets):
        oi, oj = grid.cell_of(*origin)
        for di, dj in offsets:
            i, j = oi + di, oj + dj
            c = Cell(la, i, j)
            if grid.passable(la, i, j, net) and _has_free_neighbor(grid, c, net):
                yield c, grid.center_of(i, j)

    # Retain a directionally diverse small set. Without this, twelve equal-cost
    # tiny elbow variants toward one side can erase the only useful opposite exit.
    def diverse(values, limit):
        values.sort(key=lambda c: (c.cost, c.access_key, c.segments))
        selected = []
        seen = set()
        directions = set()
        for c in values:
            key = (c.access_key, c.segments, c.vias)
            if key in seen:
                continue
            p = grid.center_of(c.escape.access.i, c.escape.access.j)
            dx, dy = p[0] - pad_xy[0], p[1] - pad_xy[1]
            direction = (c.escape.access.layer, round(math.atan2(dy, dx) / (math.pi / 4)))
            if direction not in directions:
                selected.append(c)
                seen.add(key)
                directions.add(direction)
                if len(selected) >= limit:
                    return selected
        for c in values:
            key = (c.access_key, c.segments, c.vias)
            if key not in seen:
                selected.append(c)
                seen.add(key)
                if len(selected) >= limit:
                    break
        return selected

    for la in layers:
        for c, q in portals(la):
            for segments in paths(pad_xy, q, la):
                surface.append(
                    _make_option(grid, net, pad_xy, side, c, segments, (), "surface", via_keepout)
                )
            # Broad open areas need only the nearby directional portals. Continue
            # farther when nearer portals are blocked, as in a fine-pitch fanout.
            if len(surface) >= 4 * max_options:
                break
    # A PTH is already an inter-layer terminal: never add a via on top of its drill.
    if not through_hole:
        for owner, p in grid.escape_vias:
            if owner != net or math.dist(p, pad_xy) > (reach + 1) * grid.pitch:
                continue
            # An existing via joins only the layers of its span (through: all).
            existing = grid.escape_via_spans.get((owner, p))
            if existing is not None and not existing.lo <= side <= existing.hi:
                continue
            for trunk in paths(pad_xy, p, side):
                for la in range(grid.nlayers):
                    if la == side or (
                        existing is not None and not existing.lo <= la <= existing.hi
                    ):
                        continue
                    for c, q in portals(la, p, [(0, 0), (1, 0), (-1, 0), (0, 1), (0, -1)]):
                        for tail in paths(p, q, la):
                            reuse.append(
                                _make_option(
                                    grid,
                                    net,
                                    pad_xy,
                                    side,
                                    c,
                                    trunk + tail,
                                    (),
                                    "reuse",
                                    via_keepout,
                                )
                            )
        # New via candidates are only generated when allowed, with full-stack
        # copper/hole checks. Surface alternatives stay in the same joint problem.
        sites = []
        if allow_via_in_pad:
            sites.append((pad_xy, ()))
        if allow_dogbone:
            for c, q in portals(side):
                if math.dist(q, pad_xy) < grid.pitch * 0.25:
                    continue
                for trunk in paths(pad_xy, q, side):
                    sites.append((q, trunk))
                    break
                if len(sites) >= max_options:
                    break
        vm = grid.via_model
        for p, trunk in sites:
            if vm is None and not _via_clear(grid, net, p, via_keepout):
                continue
            for la in range(grid.nlayers):
                if la == side:
                    continue
                # Under a via model the via to ``la`` is that span (checked on its
                # own layers); otherwise one through via serves every layer.
                span = None if vm is None else vm.span(side, la)
                if span is not None and not _via_clear(grid, net, p, via_keepout, span):
                    continue
                for c, q in portals(la, p, [(0, 0)]):
                    for tail in paths(p, q, la):
                        new_vias.append(
                            _make_option(
                                grid,
                                net,
                                pad_xy,
                                side,
                                c,
                                trunk + tail,
                                (p,),
                                "new_via",
                                via_keepout,
                                () if span is None else (span,),
                            )
                        )
    # Explicit quotas keep lower-cost surface choices while retaining a layer
    # alternative. No empty quota is wasted; total bounded by max_options.
    groups = [diverse(surface, max(1, max_options - 4)), diverse(reuse, 2), diverse(new_vias, 2)]
    selected = [c for group in groups for c in group]
    if len(selected) < max_options:
        for c in diverse(surface + reuse + new_vias, max_options):
            if c not in selected:
                selected.append(c)
                if len(selected) >= max_options:
                    break
    return sorted(selected[:max_options], key=lambda c: (c.cost, c.access_key, c.segments))


def _outside_own_lands(grid, net, p, rect=None, radius=None):
    """A drop via at ``p`` (of ``radius``, the grid's via by default) keeps its copper
    clear of every surface land of its own net (``via_to_smd_pad`` under a fab
    profile, else the clearance): an unfilled via in or touching a land wicks
    solder. Foreign lands are ``_via_clear``'s."""
    keep = (grid.via_radius if radius is None else radius) + (
        grid.via_to_smd_pad if grid.via_to_smd_pad is not None else grid.clearance
    )
    lands = [r for _, owner, r, _ in grid.smd_pads if owner == net]
    if rect is not None:
        lands.append(rect)
    for r in lands:
        dx = max(r.left - p[0], 0.0, p[0] - r.right)
        dy = max(r.bottom - p[1], 0.0, p[1] - r.top)
        if math.hypot(dx, dy) < keep - 1e-9:
            return False
    return True


def _drop_via_clear(grid, net, p, span=None):
    """A plane-drop via site judged on exact geometry: the drill's hole spacing,
    every grid layer's via mask (only ``span``'s layers for a blind or micro drop,
    pnr.via_policy.Span, with its own diameter), and the via disk against foreign
    pad rectangles, escape copper and the blocked mask (``_segment_clear``).

    Unlike ``_via_clear`` it does not reject a site for a foreign track-halo cell
    inside the square keep-out: that square is the maze's reservation (still
    claimed for the chosen drop), not a copper-to-copper rule, and it shuts a
    fine-pitch row's middle pins out of every outward site. Nor does it reject one
    for a foreign pad's cell-rounded halo (``RouteGrid.pad_track_halo`` /
    ``pad_via_halo``), which overstates the pad's clearance by up to a cell: the
    pad rectangle itself is judged exactly. A halo cell any other reservation
    wrote (a plated hole, fixed copper) still rejects the site, as does the fab
    profile's via-to-SMD-pad rule."""
    i, j = grid.cell_of(*p)
    owner = net if getattr(grid, "via_model", None) is not None else None  # no shared site
    if not grid.in_bounds(i, j) or not grid.hole_site_clear(p, net=owner):
        return False
    if grid.smd_via_blocked is not None and not grid.smd_via_ok(p, net):
        return False
    halos = (
        (grid.pad_net, getattr(grid, "pad_track_halo", {})),
        (grid.via_halo, getattr(grid, "pad_via_halo", {})),
    )
    radius = grid.via_radius if span is None else span.radius
    # PNR_COMPACT DROPS: a legacy plane drop crosses its own net's plane region.
    own = getattr(grid, "own_plane_cells", {}).get(net)
    keepouts = getattr(grid, "net_keepouts", None)
    for la in range(grid.nlayers) if span is None else span.layers():
        if grid.via_blocked[la, j, i]:
            return False
        if keepouts and grid.net_blocked(net, la, i, j, via=True):
            return False
        key = (la, i, j)
        for table, pads_only in halos:
            owner = table.get(key)
            if owner is not None and owner != net and owner != pads_only.get(key):
                return False
        if not _segment_clear(grid, net, la, p, p, 2 * radius, own=own):
            return False
    return True


def enumerate_drops(
    grid,
    net,
    pad_xy,
    side,
    *,
    rect=None,
    width,
    via_keepout=1,
    allow_in_pad=False,
    reach=4,
    max_options=8,
    site_ok=None,
    span=None,
    reuse=None,
):
    """Plane-drop exits for a surface pad of a net with a dedicated plane.

    Each option is a stub on the pad's layer from the pad centre to a through via
    whose site clears every grid layer (``_via_clear``) and the pad's own lands;
    the via reaches every plane of the net, so no maze tail follows. A filled via
    in the pad is an option only under the fab profile's in-pad policy
    (``allow_in_pad``), as for the native plane fanout. ``width`` is the pad's
    required entry width (pnr.pad_entry). ``site_ok(point)``, when given, admits
    only sites where the via reaches its net's own plane fill (a plane layer
    shared by several nets, a partial plane zone: pnr.stack.PlaneAccess).
    ``span`` (pnr.via_policy.Span): the drop is that blind or micro via, checked
    and reserved on its own grid layers; None: a through via.

    ``reuse`` ``(vias, reach)``: the pad may instead join an existing via of its net
    (a declared fanout's, which reaches the planes) by a stub on its own layer, where
    the via's edge is within ``reach`` of the land (a fanout's ``bottom_sites`` part
    and its ``max_stub_mm``). Those options come first: they drill nothing.
    """
    options = []
    if reuse:
        vias, within = reuse
        found = []
        for q in sorted(set(tuple(v) for v in vias)):
            if rect is not None:
                gap = math.hypot(
                    max(abs(q[0] - rect.cx) - rect.w / 2, 0.0),
                    max(abs(q[1] - rect.cy) - rect.h / 2, 0.0),
                )
            else:
                gap = math.dist(q, pad_xy)
            if gap - grid.via_radius > within + 1e-9:
                continue
            i, j = grid.cell_of(*q)
            for path in sorted(elbows(pad_xy, q), key=lambda p: (length(p), len(p), p)):
                if all(
                    _segment_clear(grid, net, side, a, b, width) for a, b in zip(path, path[1:])
                ):
                    trunk = tuple((side, a, b, width) for a, b in zip(path, path[1:]))
                    found.append(
                        _make_option(
                            grid,
                            net,
                            pad_xy,
                            side,
                            Cell(side, i, j),
                            trunk,
                            (),
                            "reuse",
                            via_keepout,
                        )
                    )
                    break
        options += sorted(found, key=lambda c: (c.cost, c.access_key, c.segments))[:max_options]
    spans = () if span is None else (span,)
    radius = None if span is None else span.radius
    ci, cj = grid.cell_of(*pad_xy)
    if (
        allow_in_pad
        and (site_ok is None or site_ok(pad_xy))
        and _via_clear(grid, net, pad_xy, via_keepout, span)
    ):
        options.append(
            _make_option(
                grid,
                net,
                pad_xy,
                side,
                Cell(side, ci, cj),
                (),
                (pad_xy,),
                "drop",
                via_keepout,
                spans,
            )
        )
    offsets = sorted(
        ((di, dj) for di in range(-reach, reach + 1) for dj in range(-reach, reach + 1)),
        key=lambda p: (p[0] ** 2 + p[1] ** 2, p),
    )
    found = []
    for di, dj in offsets:
        i, j = ci + di, cj + dj
        if not grid.in_bounds(i, j):
            continue
        q = grid.center_of(i, j)
        if site_ok is not None and not site_ok(q):
            continue
        if not _outside_own_lands(grid, net, q, rect, radius) or not _drop_via_clear(
            grid, net, q, span
        ):
            continue
        for path in sorted(elbows(pad_xy, q), key=lambda p: (length(p), len(p), p)):
            if all(_segment_clear(grid, net, side, a, b, width) for a, b in zip(path, path[1:])):
                trunk = tuple((side, a, b, width) for a, b in zip(path, path[1:]))
                found.append(
                    _make_option(
                        grid,
                        net,
                        pad_xy,
                        side,
                        Cell(side, i, j),
                        trunk,
                        (q,),
                        "drop",
                        via_keepout,
                        spans,
                    )
                )
                break
        if len(found) >= 4 * max_options:
            break
    for c in found:
        if _grazes_own_lands(grid, net, side, c.segments, rect):
            # The stub crosses another land of its net without entering it at full
            # width: that land is touched but not entered (pnr.pad_entry judges it
            # an unqualified entry). Taken only when no clean site is left.
            c.cost += GRAZE_COST
    # Directionally diverse: equal-cost sites on one side must not crowd out the
    # only exit on the other side of a dense pad row.
    found.sort(key=lambda c: (c.cost, c.access_key, c.segments))
    chosen, directions = [], set()
    for c in found:
        p = c.vias[0]
        direction = round(math.atan2(p[1] - pad_xy[1], p[0] - pad_xy[0]) / (math.pi / 4))
        if direction not in directions:
            directions.add(direction)
            chosen.append(c)
    for c in found:
        if len(chosen) >= max_options:
            break
        if c not in chosen:
            chosen.append(c)
    options += chosen[: max(0, max_options - len(options))]
    return sorted(options, key=lambda c: (c.cost, c.access_key, c.segments))


# The cost (mm, as _make_option prices a via at 3.0) added to a drop whose stub grazes
# another land of its own net.
GRAZE_COST = 5.0


def _rect_gap(p, r):
    return math.hypot(
        max(r.left - p[0], 0.0, p[0] - r.right), max(r.bottom - p[1], 0.0, p[1] - r.top)
    )


def _segment_rect_gap(a, b, r):
    """The distance between segment ``a``-``b`` and the rectangle ``r`` (0 if they meet)."""
    if _rect_gap(a, r) == 0.0 or _rect_gap(b, r) == 0.0:
        return 0.0
    corners = ((r.left, r.bottom), (r.right, r.bottom), (r.right, r.top), (r.left, r.top))
    for k in range(4):
        c, d = corners[k], corners[(k + 1) % 4]
        if _segment_distance_sq(a, b, c, d) <= 1e-18:
            return 0.0
    return min(math.sqrt(_segment_distance_sq(a, b, c, c)) for c in corners)


def _grazes_own_lands(grid, net, layer, segments, rect):
    """A stub of ``segments`` on ``layer`` overlaps a surface land of ``net`` other
    than its own (``rect``) with its copper."""
    pads = grid.smd_pads
    lands = [r for la, owner, r, _ in pads if owner == net and la == layer and r != rect]
    if not lands:
        return False
    for la, a, b, width in segments:
        if la != layer:
            continue
        # Only the lands that can come within half the stub's width (.obstacle_index):
        # _segment_rect_gap is never below the gap between the boxes.
        hits = near(pads, "rect", layer, a, b, width / 2)
        candidates = (
            lands
            if hits is None
            else [
                pads[k][2]
                for k in hits
                if pads[k][1] == net and pads[k][0] == layer and pads[k][2] != rect
            ]
        )
        for r in candidates:
            if _segment_rect_gap(a, b, r) < width / 2 - 1e-9:
                return True
    return False


def _drop_site_test(grid, plane_access, net, span, through_test):
    """The plane-fill site test of a drop: ``through_test`` for a through via; for
    a blind or micro drop (``span``) only the net's planes it reaches count."""
    if span is None or plane_access is None:
        return through_test
    reach = tuple(
        la for la in plane_access.stack.net_planes(net) if grid.via_model.reaches(span, la)
    )
    if not plane_access.constrained(net, reach):
        return None
    return lambda q: plane_access.site_ok(net, q, reach)


def _overlap(a, b):
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def options_conflict(grid, a, b):
    if not _overlap(a.bounds, b.bounds):
        return False
    # board.class_clearance (route_board sets class_escapes): two options of
    # different nets keep the larger of their class clearances, as the maze does.
    clearance = grid.clearance
    if getattr(grid, "class_escapes", False) and a.escape.net != b.escape.net:
        classes = getattr(grid, "net_clearances", None) or {}
        clearance = max(clearance, classes.get(a.escape.net, 0.0), classes.get(b.escape.net, 0.0))
    # Even same-net vias cannot have overlapping distinct drill holes. Co-located
    # same-net holes can be fused by the normal emitted-via deduplication.
    for p in a.vias:
        for q in b.vias:
            distance = math.dist(p, q)
            if 1e-7 < distance < grid.via_spacing - 1e-7:
                return True
    if a.escape.net == b.escape.net:
        return False
    if a.access_key in b.occupied or b.access_key in a.occupied:
        return True
    if a.spans or b.spans:
        # Two nets' vias never share a site, even where their spans share no layer.
        if any(math.dist(p, q) < 1e-7 for p in a.vias for q in b.vias):
            return True
        return _span_copper_conflict(grid, a, b)
    for la, p, q, wa in a.segments:
        for lb, r, s, wb in b.segments:
            if (
                la == lb
                and _segment_distance_sq(p, q, r, s) < ((wa + wb) / 2 + clearance) ** 2 - 1e-10
            ):
                return True
        for r in b.vias:
            if (
                _segment_distance_sq(p, q, r, r)
                < (wa / 2 + grid.via_radius + clearance) ** 2 - 1e-10
            ):
                return True
    for lb, p, q, wb in b.segments:
        for r in a.vias:
            if (
                _segment_distance_sq(p, q, r, r)
                < (wb / 2 + grid.via_radius + clearance) ** 2 - 1e-10
            ):
                return True
    return any(
        math.dist(p, q) < 2 * grid.via_radius + clearance - 1e-10 for p in a.vias for q in b.vias
    )


def _span_copper_conflict(grid, a, b):
    """:func:`options_conflict`'s copper test when a via has a span: a via meets a
    segment or another via only on the grid layers it occupies, at its own size."""

    def vias(option):
        return [
            (
                p,
                range(grid.nlayers) if s is None else s.layers(),
                grid.via_radius if s is None else s.radius,
            )
            for p, s in zip(option.vias, option.spans or [None] * len(option.vias))
        ]

    va, vb = vias(a), vias(b)
    for la, p, q, wa in a.segments:
        for lb, r, s, wb in b.segments:
            if (
                la == lb
                and _segment_distance_sq(p, q, r, s) < ((wa + wb) / 2 + grid.clearance) ** 2 - 1e-10
            ):
                return True
        for r, layers, radius in vb:
            if (
                la in layers
                and _segment_distance_sq(p, q, r, r)
                < (wa / 2 + radius + grid.clearance) ** 2 - 1e-10
            ):
                return True
    for lb, p, q, wb in b.segments:
        for r, layers, radius in va:
            if (
                lb in layers
                and _segment_distance_sq(p, q, r, r)
                < (wb / 2 + radius + grid.clearance) ** 2 - 1e-10
            ):
                return True
    return any(
        set(la) & set(lb) and math.dist(p, q) < ra + rb + grid.clearance - 1e-10
        for p, la, ra in va
        for q, lb, rb in vb
    )


def plan_joint_escapes(
    grid,
    graph,
    net_names,
    *,
    via_keepout,
    allow_via_in_pad=True,
    allow_dogbone=True,
    dogbone_reach=4,
    max_options=16,
    max_states=20000,
    max_cluster_size=24,
    drop_widths=None,
    drop_in_pad=False,
    drop_pad_width=None,
    plane_access=None,
    drop_span=None,
    skip_pads=None,
    drop_reuse=None,
):
    """Choose every terminal's exit jointly. ``drop_widths`` (net -> entry width)
    names the nets with a dedicated plane: each of their surface pads becomes a
    plane-drop terminal (:func:`enumerate_drops`) planned together with the signal
    exits, kept out of ``net_access``; one without a drop is reported in
    ``plan.blocked_nets`` and ``plan.drop_failures`` (net -> pad centres). Each
    drop's stub is ``drop_pad_width[(ref, pad)]`` wide (else its net's width), and
    ``plane_access`` (:class:`pnr.stack.PlaneAccess`) keeps its via inside the
    net's own plane fill. ``drop_span(net, side)``, when given, is the drop via's
    span from the pad's grid layer (pnr.via_policy.Span; None: through).
    ``skip_pads`` ((ref, pad) pairs) get no exit here: a declared fanout holds them.
    ``drop_reuse`` ((ref, pad) -> reach mm): those plane pads may also drop by a stub
    to a planned via of their net (``enumerate_drops`` ``reuse``)."""
    from .escape import Escape, EscapePlan

    drop_widths = drop_widths or {}
    drop_pad_width = drop_pad_width or {}
    options = {}
    terminals = {}
    drops = set()
    drop_width = {}
    site_tests = {}
    for net in drop_widths:
        if plane_access is not None and plane_access.constrained(net):
            site_tests[net] = lambda q, net=net: plane_access.site_ok(net, q)
    for comp in sorted(graph.components, key=lambda c: c.ref):
        twins = far_twins(comp)
        for index, ((name, net, rect), pad) in enumerate(zip(pad_rects(comp), comp.pads)):
            if skip_pads and (comp.ref, name) in skip_pads:
                continue
            if index in twins:
                continue  # a far-side land beside a near one: that one is the access
            # The pad's own layer: a far-side land escapes on the opposite outer layer.
            side = pad_layer(grid, comp, pad)
            if net in drop_widths and net not in net_names:
                if pad.through_hole:
                    continue  # the plated barrel already reaches every plane
                key = "%s.%s#%d" % (comp.ref, name, index)
                point = (rect.cx, rect.cy)
                terminals[key] = (net, point, side)
                drops.add(key)
                span = drop_span(net, side) if drop_span else None
                drop_width[key] = drop_pad_width.get((comp.ref, pad.name), drop_widths[net])
                options[key] = enumerate_drops(
                    grid,
                    net,
                    point,
                    side,
                    rect=rect,
                    width=drop_width[key],
                    via_keepout=via_keepout,
                    allow_in_pad=drop_in_pad,
                    reach=dogbone_reach,
                    max_options=max(1, max_options // 2),
                    site_ok=_drop_site_test(grid, plane_access, net, span, site_tests.get(net)),
                    span=span,
                    **(
                        {
                            "reuse": (
                                [v for n, v in grid.escape_vias if n == net],
                                drop_reuse[(comp.ref, name)],
                            )
                        }
                        if drop_reuse and (comp.ref, name) in drop_reuse
                        else {}
                    ),
                )
                continue
            if net not in net_names:
                continue
            key = "%s.%s#%d" % (comp.ref, name, index)
            point = (rect.cx, rect.cy)
            terminals[key] = (net, point, side)
            options[key] = enumerate_access(
                grid,
                net,
                point,
                side,
                through_hole=pad.through_hole,
                via_keepout=via_keepout,
                allow_via_in_pad=allow_via_in_pad,
                allow_dogbone=allow_dogbone,
                reach=dogbone_reach,
                max_options=max_options,
            )
    guard = getattr(grid, "guard_access", None)
    if guard:
        # plane_partition protect_fanouts: a declared fanout's planned access cells
        # (route_board sets grid.guard_access for this plan) stay free of every other
        # net's exit and drop, whose via keep-out would close them.
        for key, cs in options.items():
            net = terminals[key][0]
            options[key] = [
                c for c in cs if not any(guard.get(cell, net) != net for cell in c.occupied)
            ]
    bounds = {
        k: (
            min(c.bounds[0] for c in cs),
            min(c.bounds[1] for c in cs),
            max(c.bounds[2] for c in cs),
            max(c.bounds[3] for c in cs),
        )
        for k, cs in options.items()
        if cs
    }
    selection, seal_report = _select_unsealed(
        grid,
        options,
        terminals,
        drops,
        bounds,
        max_states=max_states,
        max_cluster_size=max_cluster_size,
    )
    plan = EscapePlan()
    plan.diagnostics = selection.report()
    if seal_report:
        plan.diagnostics["sealed_access"] = seal_report
    plan.diagnostics["candidate_counts"] = {k: len(cs) for k, cs in options.items()}
    plan.diagnostics["model"] = "joint-grid-pad-rectangles-v1"
    plan.blocked_nets = {terminals[key][0] for key in selection.unresolved}
    plan.drop_failures = {}
    for key in sorted(selection.unresolved):
        if key in drops:
            plan.drop_failures.setdefault(terminals[key][0], []).append(terminals[key][1])
    grid.protected_escape_access = {}
    for key in sorted(terminals):
        net, point, side = terminals[key]
        if key in selection.selected:
            choice = options[key][selection.selected[key]]
            esc = choice.escape
            if key in drops:
                esc.width = drop_width[key]
            # The complete legal assignment is known before any reservation is
            # committed. Foreign pad halos may still overlap the coarse corridor;
            # retain that conflict rather than erasing an unrelated obstacle.
            for cell in choice.occupied:
                owner = grid.pad_net.get(cell)
                grid.pad_net[cell] = net if owner is None or owner == net else "\0conflict"
            grid.protected_escape_access[choice.access_key] = net
            grid.escape_segments.extend((la, net, a, b) for la, a, b, _ in choice.segments)
            grid.escape_vias.extend((net, p) for p in choice.vias)
            vm = grid.via_model
            for p, span in zip(choice.vias, choice.spans or (None,) * len(choice.vias)):
                if vm is None:
                    continue
                # One barrel per (net, site): spans of one net there merge.
                span = span or vm.full
                old = grid.escape_via_spans.get((net, p))
                if old is not None:
                    span = max(vm.merged([old, span]), key=lambda s: (s.b - s.t, s.t))
                grid.escape_via_spans[(net, p)] = span
        else:
            # Do not emit an unqualified centre stub or pretend a missing access
            # is connected. route_board reports this net unresolved explicitly.
            esc = Escape(
                net=net,
                kind="blocked",
                access=Cell(side, *grid.cell_of(*point)),
                pad_xy=point,
                side_layer=grid.layers[side],
            )
        if key not in drops:
            plan.net_access.setdefault(net, []).append(esc.access)
        plan.escapes.append(esc)
    return plan


# A selected exit whose access cell can reach fewer than this many grid cells on its
# layer, no via site and no other terminal of its net is sealed (see _sealed_access).
SEAL_REACH_CELLS = 400
# Re-selections after a sealed exit (each bans the sealing options for that exit).
SEAL_ROUNDS = 6


def _select_unsealed(grid, options, terminals, drops, bounds, *, max_states, max_cluster_size):
    """:func:`select_joint`, then re-selected while a chosen option walls in another
    terminal's exit.

    The joint search only knows pairwise geometric conflicts: it can choose a plane
    drop whose via and halo close the last gap out of a neighbouring signal pad (a
    connector's ground drop sealing the middle pin of a 0.65 mm row, say), leaving
    that pad's net open for every router round however the rest is routed. After
    each selection the exits are checked against the selection's own occupancy
    (:func:`_sealed_access`); the options that wall a sealed exit in are declared in
    conflict with that exit's option and the search runs again. A re-selection is
    kept only if it seals fewer exits and resolves no fewer terminals, so a board
    whose selection seals nothing is planned exactly as before. Returns the
    selection and a report of the sealed exits ({} when none was found)."""
    extra = set()
    extra_keys = set()

    def conflict(a, b):
        return (id(a), id(b)) in extra or options_conflict(grid, a, b)

    def may_interact(a, b):
        if (a, b) in extra_keys:
            return True
        return a in bounds and b in bounds and _overlap(bounds[a], bounds[b])

    def run():
        return select_joint(
            options,
            conflict,
            max_states=max_states,
            max_cluster_size=max_cluster_size,
            may_interact=may_interact,
        )

    best = run()
    sealed = _sealed_access(grid, options, terminals, drops, best.selected)
    if not sealed:
        return best, {}
    report = {
        "found": {k: walls for k, (walls, _) in sorted(sealed.items())},
        "rounds": 0,
        "left": sorted(sealed),
    }
    current, current_sealed = best, sealed
    for round_ in range(SEAL_ROUNDS):
        added = False
        for key, (walls, border) in sorted(current_sealed.items()):
            a = options[key][current.selected[key]]
            for other in walls:
                # Every option of a walling terminal that would stand on this
                # border, not only the chosen one: the next search moves it off.
                for b in options[other]:
                    if b.occupied.isdisjoint(border):
                        continue
                    for pair in ((id(a), id(b)), (id(b), id(a))):
                        if pair not in extra:
                            extra.add(pair)
                            added = True
                extra_keys.update(((key, other), (other, key)))
        if not added:
            break
        current = run()
        current_sealed = _sealed_access(grid, options, terminals, drops, current.selected)
        report["rounds"] = round_ + 1
        report.setdefault("walls", []).append(
            {k: walls for k, (walls, _) in sorted(current_sealed.items())}
        )
        if len(current.unresolved) <= len(best.unresolved) and len(current_sealed) < len(
            report["left"]
        ):
            best = current
            report["left"] = sorted(current_sealed)
        if not current_sealed:
            break
    return best, report


def _sealed_access(grid, options, terminals, drops, selected, reach=SEAL_REACH_CELLS):
    """{terminal key: ([keys of the selected options walling it in], border cells)}
    for every selected signal exit that the selection itself seals.

    An exit is sealed when the cells its net's track may use on the access layer,
    counting the selection's own exits and drops as copper (each with its halo, as
    :func:`_occupied` reserves them), reach fewer than ``reach`` cells from its
    access cell and none of them is a via site or another terminal of the net. Exits
    that already change layer (an option with a via) are not checked. The walling
    options are the selected options of other terminals that occupy a cell on the
    sealed region's border; an exit sealed by static copper alone names none."""
    owner = {}
    holders = {}
    access = {}
    for key, i in selected.items():
        net = terminals[key][0]
        option = options[key][i]
        for cell in option.occupied:
            held = owner.get(cell)
            owner[cell] = net if held is None or held == net else "\0conflict"
            holders.setdefault(cell, []).append(key)
        if key not in drops:
            access.setdefault(net, set()).add(option.access_key)
    out = {}
    steps = ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1))
    for key in sorted(selected):
        if key in drops:
            continue
        net = terminals[key][0]
        option = options[key][selected[key]]
        if option.vias:
            continue
        layer, i0, j0 = option.access_key
        others = access.get(net, set()) - {option.access_key}

        def free(i, j):
            held = owner.get((layer, i, j))
            return (held is None or held == net) and grid.passable(layer, i, j, net)

        def via_site(i, j):
            if not grid.via_passable(layer, i, j, net):
                return False
            for la in range(grid.nlayers):
                held = owner.get((la, i, j))
                if held is not None and held != net:
                    return False
            return True

        seen = {(i0, j0)}
        todo = [(i0, j0)]
        border = set()
        escaped = False
        while todo and not escaped:
            i, j = todo.pop()
            if (layer, i, j) in others or via_site(i, j):
                escaped = True
                break
            for di, dj in steps:
                n = (i + di, j + dj)
                if n in seen:
                    continue
                if free(*n):
                    seen.add(n)
                    todo.append(n)
                    if len(seen) >= reach:
                        escaped = True
                        break
                elif grid.in_bounds(*n):
                    border.add(n)
        if escaped:
            continue
        walls = set()
        for i, j in border:
            for other in holders.get((layer, i, j), ()):
                if terminals[other][0] != net:
                    walls.add(other)
        out[key] = (sorted(walls), frozenset((layer, i, j) for i, j in border))
    return out
