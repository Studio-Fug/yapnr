"""Hand declared fanouts (:mod:`pnr.fanout`) to the detailed router.

``rules["fanouts"]`` (compiled ``fanout:`` constraint entries) is planned once per
process (:func:`pnr.fanout.cached_plan`) and becomes, on this grid:

* **reserved copper**: every planned stub, dog-bone, escape track and via is
  recorded as escape copper (``grid.escape_segments`` / ``escape_vias``, judged
  exactly by every later escape and by the wide-net tables) and its cells are owned
  by its net, as a jointly planned escape's are;
* **terminals**: each escaped signal ball continues straight outward from its exit
  to the first grid cell its net may hold and leave (the *access* cell); that cell
  is where the maze routes the net from, and the short tail to its centre is part
  of the escape;
* **escapes**: ``Escape(kind="joint", fanout=name)`` per ball, emitted like any
  joint escape at the fanout's own track width; a plane ball's escape is its drop;
* **pads left alone**: the fanned-out balls are skipped by the escape and drop
  planner (``skip_pads``);
* **failures**: a ball the plan could not escape or drop, or whose exit has no free
  access cell here, blocks its net and is localized at the ball (failure sites);
* **partial fanouts** (the entry's ``partial``): such a ball no longer blocks its net.
  With ``bridge`` it is first joined by a surface stub to an adjacent ball of its net
  whose escape stands (judged exactly); otherwise it goes back to the board's escape
  planner (out of ``skip_pads``), and :func:`resolve_partial` records whether that
  planner escaped it or it stays open (a failure site, ``BoardRoute.partial_open``).
  The net routes among its other terminals; the report lists every such ball.

Nothing here runs without ``rules["fanouts"]``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Set, Tuple

from pnr.graph import footprint_point
from pnr.place.geometry import pad_rects

from .escape import Escape
from .grid import Cell
from .joint_escape import _occupied, _segment_clear


@dataclass
class FanoutRouting:
    escapes: List[Escape] = field(default_factory=list)
    skip_pads: Set[Tuple[str, str]] = field(default_factory=set)
    access: Dict[str, List[Cell]] = field(default_factory=dict)
    blocked_nets: Set[str] = field(default_factory=set)
    drop_failures: Dict[str, List[Tuple[float, float]]] = field(default_factory=dict)
    failure_sites: Dict[str, List[Tuple[float, float]]] = field(default_factory=dict)
    via_sizes: Dict[Tuple[str, float, float], Tuple[float, float]] = field(default_factory=dict)
    locked: Set[str] = field(default_factory=set)  # fanout names written locked
    protected: Dict[Tuple[int, int, int], str] = field(default_factory=dict)
    report: Dict = field(default_factory=dict)
    # Partial fanouts only (the entry's ``partial``), else empty. ``retry``: (ref, pad)
    # -> (net, fanout name, pad centre, reason) of the failed balls handed back to the
    # board's escape planner; ``partial_open``: net -> {"REF.PAD": reason} of the balls
    # left unconnected (resolve_partial completes it).
    retry: Dict[Tuple[str, str], Tuple[str, str, Tuple[float, float], str]] = field(
        default_factory=dict
    )
    partial_open: Dict[str, Dict[str, str]] = field(default_factory=dict)

    def note_partial(self, fanout, ball, net, reason, outcome):
        """Record one partial-fanout ball in its fanout's report (``partial``)."""
        self.report[fanout].setdefault("partial", {})[ball] = dict(
            net=net, reason=reason, outcome=outcome
        )


def _foreign_pads(grid, own_rects):
    """Pad rectangles of every other part (the fanout's own lands are in its plan)."""
    own = {(la, round(r.cx, 6), round(r.cy, 6)) for la, r in own_rects}
    return [
        (la, net, r)
        for la, net, r in grid.pad_rectangles
        if (la, round(r.cx, 6), round(r.cy, 6)) not in own
    ]


def _clear_of_pads(pads, net, layer, a, b, width, clearance, classes=None):
    """``a``-``b`` of ``width`` clears every pad of another net on ``layer`` by the
    larger of the two nets' clearances (``classes``: net -> class clearance; the
    fab ``clearance`` holds where larger or absent)."""
    from pnr.writeback import _segment_distance_sq

    classes = classes or {}
    base = clearance
    for la, owner, r in pads:
        if la != layer or owner == net:
            continue
        radius = width / 2 + max(base, classes.get(net, 0.0), classes.get(owner, 0.0))
        if (
            max(a[0], b[0]) + radius < r.left
            or min(a[0], b[0]) - radius > r.right
            or max(a[1], b[1]) + radius < r.bottom
            or min(a[1], b[1]) - radius > r.top
        ):
            continue
        if any(r.left <= p[0] <= r.right and r.bottom <= p[1] <= r.top for p in (a, b)):
            return False
        corners = [(r.left, r.bottom), (r.right, r.bottom), (r.right, r.top), (r.left, r.top)]
        if any(
            _segment_distance_sq(a, b, corners[k], corners[(k + 1) % 4]) < radius**2 - 1e-10
            for k in range(4)
        ):
            return False
    return True


def _via_halo(grid, segments, vias):
    """Cells whose centre a via of another net may not take: closer than via radius
    + clearance + half the copper to a segment (on its layer) or a via (every
    layer). Exact at cell centres, where the maze puts its vias. The clearance is
    the largest one a via there may need: any net class's (``grid.net_clearances``)
    where larger than the fab's."""
    from pnr.writeback import _segment_distance_sq

    out = set()
    classes = getattr(grid, "net_clearances", None) or {}
    reach = grid.via_radius + max([grid.clearance] + list(classes.values()))

    def mark(layers, a, b, grow):
        i0, j0 = grid.cell_of(min(a[0], b[0]) - grow, min(a[1], b[1]) - grow)
        i1, j1 = grid.cell_of(max(a[0], b[0]) + grow, max(a[1], b[1]) + grow)
        for j in range(j0, j1 + 1):
            for i in range(i0, i1 + 1):
                c = grid.center_of(i, j)
                if _segment_distance_sq(a, b, c, c) < grow * grow - 1e-10:
                    out.update((la, i, j) for la in layers)

    for la, a, b, w in segments:
        mark((la,), a, b, w / 2 + reach)
    for p, d in vias:
        mark(range(grid.nlayers), p, p, d / 2 + reach)
    return out


def _access(
    grid, net, layer, exit_xy, outward, width, taken, owners, reach_mm=2.0, pads=None, areas=()
):
    """The first cell along the exit's outward ray that ``net`` may hold and leave
    outward, with a clear tail from the exit to its centre: ``(cell, tail end)``.
    The tail keeps each foreign pad in ``pads`` at the larger of the two nets' class
    clearances (``grid.net_clearances``). When the ray finds none (a part placed
    against the array's edge), the tail may turn: the nearest cell within
    ``reach_mm`` and within 75 degrees of the outward direction that passes the same
    tests, leaving along the tail's own direction. ``areas`` (:func:`_keepout_areas`)
    are judged on the tail exactly, as the planner judged the exit, instead of by the
    grid's conservative keepout cells (the access cell itself still needs them)."""
    classes = getattr(grid, "net_clearances", None) or {}
    steps = max(1, int(math.ceil(reach_mm / (grid.pitch / 4))))
    seen = set()
    rays = []
    for k in range(steps + 1):
        t = k * grid.pitch / 4
        q = (exit_xy[0] + outward[0] * t, exit_xy[1] + outward[1] * t)
        if not (0 <= q[0] < grid.width and 0 <= q[1] < grid.height):
            break
        i, j = grid.cell_of(*q)
        if (i, j) in seen:
            continue
        seen.add((i, j))
        rays.append((i, j, int(round(outward[0])), int(round(outward[1]))))
    found = _first_access(
        grid, net, layer, exit_xy, width, taken, owners, pads, classes, rays, areas
    )
    if found is not None:
        return found
    ci, cj = grid.cell_of(*exit_xy)
    span = int(math.ceil(reach_mm / grid.pitch))
    fan = []
    for dj in range(-span, span + 1):
        for di in range(-span, span + 1):
            i, j = ci + di, cj + dj
            if (i, j) in seen or not grid.in_bounds(i, j):
                continue
            c = grid.center_of(i, j)
            vx, vy = c[0] - exit_xy[0], c[1] - exit_xy[1]
            d = math.hypot(vx, vy)
            if d < 1e-9 or d > reach_mm or vx * outward[0] + vy * outward[1] < 0.25 * d:
                continue
            fan.append((round(d, 9), i, j, int(round(vx / d)), int(round(vy / d))))
    fan.sort()
    found = _first_access(
        grid, net, layer, exit_xy, width, taken, owners, pads, classes, [f[1:] for f in fan], areas
    )
    if found is not None or not areas:
        return found
    # Last: the same cells with the tail judged as the planner judged the exit (rule
    # areas and keepouts exactly, the grid's grown obstacles on the centre line), for
    # an exit the grid's cells put too close to an area it legally clears.
    return _first_access(
        grid,
        net,
        layer,
        exit_xy,
        width,
        taken,
        owners,
        pads,
        classes,
        rays + [f[1:] for f in fan],
        areas,
        exact=True,
    )


def _keepout_areas(graph, rules, fixed_copper=None):
    """``[(polygon, track layer names, allowed nets)]`` of the v1 copper keepouts and
    the fixed blocks' rule areas that bar tracks, in the board frame: what the fanout
    planner judges exactly."""
    from pnr.fanout.planner import fixed_items
    from pnr.fixed_block import keepout_polygon

    out = []
    for spec in (rules or {}).get("copper_keepouts") or []:
        if "items" not in spec or "tracks" not in spec["items"]:
            continue
        allowed = frozenset(spec.get("allowed_nets") or ())
        out.append((keepout_polygon(graph, spec), frozenset(spec.get("layers") or ()), allowed))
    for poly in fixed_items(fixed_copper)[2] if fixed_copper else ():
        if poly.get("kind") == "rule_area" and poly.get("tracks") and poly.get("layers"):
            outline = [tuple(p) for p in poly["outline"]]
            out.append((outline, frozenset(poly["layers"]), frozenset()))
    return out


def _first_access(
    grid,
    net,
    layer,
    exit_xy,
    width,
    taken,
    owners,
    pads,
    classes,
    candidates,
    areas=(),
    exact=False,
):
    """The first ``(i, j, di, dj)`` candidate whose cell and its ``(di, dj)`` neighbour the
    net may hold and whose tail from the exit is clear (see :func:`_access`)."""
    for i, j, di, dj in candidates:
        ni, nj = i + di, j + dj
        ok = True
        for ci, cj in ((i, j), (ni, nj)):
            key = (layer, ci, cj)
            if not grid.in_bounds(ci, cj) or not grid.passable(layer, ci, cj, net):
                ok = False
                break
            if taken.get(key, net) != net or owners.get(key, {net}) - {net}:
                ok = False
                break
        if not ok:
            continue
        centre = grid.center_of(i, j)
        if math.dist(centre, exit_xy) > 1e-9 and not _tail_clear(
            grid, net, layer, exit_xy, centre, width, areas, exact
        ):
            continue
        if (
            classes
            and pads
            and math.dist(centre, exit_xy) > 1e-9
            and not _clear_of_pads(
                pads, net, layer, exit_xy, centre, width, grid.clearance, classes
            )
        ):
            continue
        return Cell(layer, i, j), centre
    return None


def _tail_clear(grid, net, layer, a, b, width, areas, exact=False):
    """The access tail ``a``-``b`` clears the grid's obstacles, the copper keepouts
    judged exactly (``areas``) when there are any, else by the grid's cells.
    ``exact``: the rule areas in ``areas`` too, and the grid's blocked cells (grown
    obstacles) on the tail's centre line only."""
    if not areas:
        return _segment_clear(grid, net, layer, a, b, width)
    from pnr.fanout.geom import segment_polygon
    from pnr.fanout.sites import MARGIN

    name = grid.layers[layer]
    for poly, layers, allowed in areas:
        if net in allowed or (layers and name not in layers):
            continue
        if segment_polygon(a, b, poly) < width / 2 + MARGIN:
            return False
    return _segment_clear(grid, net, layer, a, b, width, net_keepouts=False, offsets=not exact)


def plan_fanouts(grid, graph, rules, *, plane_nets, signal_nets, via_keepout, fixed_copper=None):
    """Plan, reserve and translate every declared fanout on ``grid`` (see module doc)."""
    from pnr.fanout import cached_plan

    # Fixed copper given to route_board, else carried in the rules (a fixed block's).
    fixed_copper = fixed_copper or (rules or {}).get("fixed_copper")
    out = FanoutRouting()
    layer_index = {name: i for i, name in enumerate(grid.layers)}
    clearance = grid.clearance
    classes = getattr(grid, "net_clearances", None) or {}
    areas = _keepout_areas(graph, rules, fixed_copper)
    planned = []
    for spec in rules.get("fanouts") or []:
        plan = cached_plan(
            graph,
            rules,
            spec,
            grid_layers=list(grid.layers),
            plane_nets=plane_nets,
            signal_nets=signal_nets,
            fixed_copper=fixed_copper,
        )
        comp = graph.component(spec["ref"])
        centres = {name: (r.cx, r.cy) for name, _net, r in pad_rects(comp)}
        side = grid.side_layer(comp.side)
        own_rects = [(side, r) for _n, _net, r in pad_rects(comp)]
        if plan.get("lock", True):
            out.locked.add(spec["name"])
        planned.append((spec, plan, comp, centres, side, own_rects))
    # 1. Every planned copper item is exact escape copper before any access is chosen.
    rows = []
    foreign_of = {}
    ball_escape = {}  # (fanout, ball) -> its emitted joint Escape (partial bridges)
    pending = []  # partial fanouts: failed balls, bridged or handed back below
    access_ball = {}  # protected access cell -> (spec, ref, ball, pad centre)
    for spec, plan, comp, centres, side, own_rects in planned:
        foreign = _foreign_pads(grid, own_rects)
        foreign_of[spec["name"]] = foreign
        summary = dict(plan["diagnostics"])
        summary.update(inputs_sha256=plan["inputs_sha256"], conflicts_on_board=[], no_access=[])
        for name, row in plan["terminals"].items():
            if row["kind"] == "skipped":
                continue
            out.skip_pads.add((comp.ref, name))
            pad_xy = centres[name]
            if row["kind"] == "fixed":
                # A plane ball the fixed copper of its net already joins: planned,
                # with nothing to emit (so it is not late copper either).
                out.escapes.append(
                    Escape(
                        net=row["net"],
                        kind="joint",
                        access=Cell(side, *grid.cell_of(*pad_xy)),
                        pad_xy=pad_xy,
                        side_layer=grid.layers[side],
                        segments=[],
                        fanout=spec["name"],
                    )
                )
                ball_escape[spec["name"], name] = out.escapes[-1]
                continue
            if row["kind"] == "failed":
                rows.append((spec, comp, name, row, pad_xy, side, None, None))
                continue
            segments = [(layer_index[la], tuple(a), tuple(b)) for la, a, b in row["segments"]]
            via = tuple(row["via"][:2]) if row.get("via") else None
            width = row["width_mm"]
            clash = any(
                not _clear_of_pads(foreign, row["net"], la, a, b, width, clearance, classes)
                for la, a, b in segments
            ) or (
                via is not None
                and any(
                    not _clear_of_pads(
                        foreign, row["net"], la, via, via, row["via"][2], clearance, classes
                    )
                    for la in range(grid.nlayers)
                )
            )
            if clash:
                summary["conflicts_on_board"].append(name)
                rows.append(
                    (spec, comp, name, dict(row, kind="failed", intended=row["kind"]), pad_xy, side)
                    + (None, None)
                )
                continue
            for la, a, b in segments:
                grid.escape_segments.append((la, row["net"], a, b))
            if via is not None:
                grid.escape_vias.append((row["net"], via))
                out.via_sizes[(row["net"], via[0], via[1])] = (row["via"][2], row["via"][3])
            rows.append((spec, comp, name, row, pad_xy, side, segments, via))
        out.report[spec["name"]] = summary
    # 2. Cells every planned item occupies (owners per cell), without access tails.
    owners: Dict[Tuple[int, int, int], Set[str]] = {}
    claims: Dict[Tuple[Tuple[int, int, int], str], int] = {}  # (cell, net): items
    row_cells = {}
    for k, (spec, comp, name, row, pad_xy, side, segments, via) in enumerate(rows):
        if segments is None:
            continue
        cells = _occupied(
            grid,
            [(la, a, b, row["width_mm"]) for la, a, b in segments],
            [via] if via is not None else [],
            via_keepout,
        )
        row_cells[k] = cells
        for cell in cells:
            owners.setdefault(cell, set()).add(row["net"])
            claims[cell, row["net"]] = claims.get((cell, row["net"]), 0) + 1

    def release(k, row, segments, via):
        """A planned ball whose exit found no access is never emitted: its copper
        leaves the escape tables and its cells go back to the maze."""
        net = row["net"]
        for la, a, b in segments:
            grid.escape_segments.remove((la, net, a, b))
        if via is not None:
            grid.escape_vias.remove((net, via))
            out.via_sizes.pop((net, via[0], via[1]), None)
        for cell in row_cells.get(k, ()):
            claims[cell, net] -= 1
            if not claims[cell, net]:
                owners[cell].discard(net)

    # 3. Access cells, then the escapes; each owned cell goes to its net (or nobody).
    taken: Dict[Tuple[int, int, int], str] = {}
    for k, (spec, comp, name, row, pad_xy, side, segments, via) in enumerate(rows):
        net = row["net"]
        if segments is None:
            if spec.get("partial"):
                reason = (
                    "conflicts with another part's pad on the board"
                    if row.get("intended") and "reason" not in row
                    else "plan: %s" % row.get("reason", "unresolved")
                )
                pending.append((spec, comp, name, row, pad_xy, side, reason))
                continue
            site = (min(pad_xy[0], grid.width - 1e-9), min(pad_xy[1], grid.height - 1e-9))
            out.failure_sites.setdefault(net, []).append(site)
            if row.get("intended") == "drop":
                out.drop_failures.setdefault(net, []).append(pad_xy)
            out.blocked_nets.add(net)
            out.escapes.append(
                Escape(
                    net=net,
                    kind="blocked",
                    access=Cell(side, *grid.cell_of(*pad_xy)),
                    pad_xy=pad_xy,
                    side_layer=grid.layers[side],
                )
            )
            continue
        tail = []
        tail_width = row["width_mm"]
        if (row["kind"] == "drop" or (row["kind"] == "via_in_pad" and "exit" not in row)) and (
            net in signal_nets
        ):
            # A drop net's ball (fanout drop_nets): its via is the net's terminal,
            # reached on the layer opposite the part (decoupling under the array).
            far = grid.nlayers - 1 if side == 0 else 0
            access = Cell(far, *grid.cell_of(*via))
            out.access.setdefault(net, []).append(access)
            out.protected[(access.layer, access.i, access.j)] = net
            access_ball[(access.layer, access.i, access.j)] = (spec, comp.ref, name, pad_xy)
        elif row["kind"] == "drop" or (row["kind"] == "via_in_pad" and "exit" not in row):
            access = Cell(side, *grid.cell_of(*pad_xy))
        else:
            layer = layer_index[row["layer"]]
            # The tail runs at the router's width for the net (the class width): the
            # fanout's own width (a neck) ends at its exit.
            tail_width = grid.net_widths.get(net, grid.track_width)
            found = _access(
                grid,
                net,
                layer,
                tuple(row["exit"]),
                tuple(row["outward"]),
                tail_width,
                taken,
                owners,
                pads=foreign_of[spec["name"]],
                areas=areas,
            )
            if found is None:
                out.report[spec["name"]]["no_access"].append(name)
                if spec.get("partial"):
                    release(k, row, segments, via)
                    pending.append(
                        (spec, comp, name, row, pad_xy, side, "no access cell beyond its exit")
                    )
                    continue
                out.failure_sites.setdefault(net, []).append(tuple(row["exit"]))
                out.blocked_nets.add(net)
                release(k, row, segments, via)
                out.escapes.append(
                    Escape(
                        net=net,
                        kind="blocked",
                        access=Cell(side, *grid.cell_of(*pad_xy)),
                        pad_xy=pad_xy,
                        side_layer=grid.layers[side],
                    )
                )
                continue
            access, centre = found
            if math.dist(centre, tuple(row["exit"])) > 1e-9:
                tail = [(layer, tuple(row["exit"]), centre)]
                grid.escape_segments.append((layer, net, tuple(row["exit"]), centre))
            out.access.setdefault(net, []).append(access)
            out.protected[(access.layer, access.i, access.j)] = net
            access_ball[(access.layer, access.i, access.j)] = (spec, comp.ref, name, pad_xy)
        widths = [row["width_mm"]] * len(segments) + [tail_width] * len(tail)
        cells = _occupied(
            grid,
            [(la, a, b, w) for (la, a, b), w in zip(segments + tail, widths)],
            [via] if via is not None else [],
            via_keepout,
        )
        for cell in cells:
            taken.setdefault(cell, net)
            if taken[cell] != net:
                taken[cell] = "\0conflict"
            owner = grid.pad_net.get(cell)
            grid.pad_net[cell] = net if owner is None or owner == net else "\0conflict"
        # A maze via sits on a cell centre: keep every other net's via centre at
        # least via radius + clearance + half the copper from this copper, exactly.
        for cell in _via_halo(
            grid,
            [(la, a, b, w) for (la, a, b), w in zip(segments + tail, widths)],
            [(via, row["via"][2])] if via is not None else [],
        ):
            owner = grid.via_halo.get(cell)
            grid.via_halo[cell] = net if owner is None or owner == net else "\0conflict"
        esc = Escape(
            net=net,
            kind="joint",
            access=access,
            pad_xy=pad_xy,
            side_layer=grid.layers[side],
            # A reused fixed via (the plan's via_existing) is already on the board.
            via_xy=None if row.get("via_existing") else via,
            segments=[(grid.layers[la], a, b) for la, a, b in segments + tail],
            width=row["width_mm"],
            widths=widths,
            fanout=spec["name"],
        )
        out.escapes.append(esc)
        ball_escape[spec["name"], name] = esc
    # An access cell another fanout net's tail took is no longer its net's.
    lost = set()
    for key, net in sorted(out.protected.items()):
        if grid.pad_net.get(key) != net:
            del out.protected[key]
            out.access[net] = [c for c in out.access.get(net, []) if (c.layer, c.i, c.j) != key]
            owner = access_ball.get(key)
            if owner is not None and owner[0].get("partial"):
                spec, ref, name, pad_xy = owner
                lost.add((spec["name"], name))
                reason = "its access cell went to another fanout's tail"
                out.partial_open.setdefault(net, {})["%s.%s" % (ref, name)] = reason
                out.note_partial(spec["name"], name, net, reason, "open")
                out.failure_sites.setdefault(net, []).append(grid.center_of(key[1], key[2]))
                continue
            out.blocked_nets.add(net)
            out.failure_sites.setdefault(net, []).append(grid.center_of(key[1], key[2]))
    # Partial fanouts: each failed ball is bridged to a standing neighbour of its net,
    # or handed back to the board's escape planner (resolve_partial judges it).
    for spec, comp, name, row, pad_xy, side, reason in pending:
        net = row["net"]
        if (spec.get("partial") or {}).get("bridge"):
            other = _bridge(
                grid, spec, comp, name, row, pad_xy, side, ball_escape, lost, areas, via_keepout
            )
            if other is not None:
                out.note_partial(spec["name"], name, net, reason, "bridged to %s" % other)
                # Its connection is the neighbour's escape (which carries the stub);
                # this empty one says the pad is held (not late copper).
                out.escapes.append(
                    Escape(
                        net=net,
                        kind="joint",
                        access=Cell(side, *grid.cell_of(*pad_xy)),
                        pad_xy=pad_xy,
                        side_layer=grid.layers[side],
                        segments=[],
                        fanout=spec["name"],
                    )
                )
                continue
        if spec["partial"].get("retry", True) is False:
            out.note_partial(spec["name"], name, net, reason, "open")
            out.partial_open.setdefault(net, {})["%s.%s" % (comp.ref, name)] = reason
            out.failure_sites.setdefault(net, []).append(
                (min(pad_xy[0], grid.width - 1e-9), min(pad_xy[1], grid.height - 1e-9))
            )
            out.escapes.append(
                Escape(
                    net=net,
                    kind="blocked",
                    access=Cell(side, *grid.cell_of(*pad_xy)),
                    pad_xy=pad_xy,
                    side_layer=grid.layers[side],
                )
            )
            continue
        out.skip_pads.discard((comp.ref, name))
        out.retry[(comp.ref, name)] = (net, spec["name"], pad_xy, reason)
    return out


def _bridge(grid, spec, comp, name, row, pad_xy, side, ball_escape, lost, areas, via_keepout):
    """Join failed ball ``name`` by a straight surface stub (its planned width) to the
    nearest adjacent ball of its net whose escape stands (orthogonal, then diagonal
    neighbours on the array's lattice), judged exactly: foreign pads at the larger
    class clearance, escape copper, escape vias, keepouts and rule areas. The stub
    joins that ball's escape (emitted with it) and is reserved like fanout copper.
    Returns the neighbour's name, or None."""
    net = row["net"]
    width = row.get("width_mm") or grid.net_widths.get(net, grid.track_width)
    centres = {n: (r.cx, r.cy) for n, _net, r in pad_rects(comp)}
    nets = {n: owner for n, owner, _r in pad_rects(comp)}
    pitch = min(
        (math.dist(a, b) for a in centres.values() for b in centres.values() if a != b),
        default=0.0,
    )
    if pitch <= 0:
        return None
    candidates = []
    for other, xy in centres.items():
        if other == name or nets.get(other) != net or (spec["name"], other) in lost:
            continue
        esc = ball_escape.get((spec["name"], other))
        d = math.dist(pad_xy, xy)
        if esc is None or esc.kind != "joint" or d > pitch * math.sqrt(2) * 1.01:
            continue
        candidates.append((round(d, 9), other, xy, esc))
    # The fanout's own reserved corridors bar its copper (as in its planner).
    reserved = []
    for r in spec.get("reserved") or []:
        if r["frame"] == "board":
            poly = [tuple(p) for p in r["polygon"]]
        else:
            poly = [footprint_point(comp, x, y) for x, y in r["polygon"]]
        layers = frozenset() if "*" in r["layers"] else frozenset(r["layers"])
        reserved.append((poly, layers, frozenset()))
    for _d, other, xy, esc in sorted(candidates):
        if not _tail_clear(grid, net, side, pad_xy, xy, width, list(areas) + reserved, exact=True):
            continue
        layer = grid.layers[side]
        esc.segments = list(esc.segments) + [(layer, pad_xy, xy)]
        base = esc.widths if esc.widths is not None else [esc.width] * (len(esc.segments) - 1)
        esc.widths = list(base) + [width]
        grid.escape_segments.append((side, net, pad_xy, xy))
        for cell in _occupied(grid, [(side, pad_xy, xy, width)], [], via_keepout):
            owner = grid.pad_net.get(cell)
            grid.pad_net[cell] = net if owner is None or owner == net else "\0conflict"
        for cell in _via_halo(grid, [(side, pad_xy, xy, width)], []):
            owner = grid.via_halo.get(cell)
            grid.via_halo[cell] = net if owner is None or owner == net else "\0conflict"
        return other
    return None


def resolve_partial(grid, fanouts, plan):
    """After the board's escape planner (``plan``, which got the retried balls): each
    retried ball is escaped or open. A net the planner blocked only through retried
    balls that stayed open is unblocked (it routes among its other terminals)."""
    retried_open = {}
    for (ref, name), (net, fanout, xy, reason) in sorted(fanouts.retry.items()):
        esc = next(
            (e for e in plan.escapes if e.net == net and math.dist(e.pad_xy, xy) < 1e-6), None
        )
        if esc is not None and esc.kind != "blocked":
            fanouts.note_partial(fanout, name, net, reason, "escaped by the board's escapes")
            continue
        fanouts.note_partial(fanout, name, net, reason, "open")
        fanouts.partial_open.setdefault(net, {})["%s.%s" % (ref, name)] = reason
        if esc is not None and esc.access in plan.net_access.get(net, []):
            plan.net_access[net].remove(esc.access)  # no terminal for the maze there
        fanouts.failure_sites.setdefault(net, []).append(
            (min(xy[0], grid.width - 1e-9), min(xy[1], grid.height - 1e-9))
        )
        retried_open.setdefault(net, []).append(xy)
    for net in sorted(plan.blocked_nets):
        blocked = [e.pad_xy for e in plan.escapes if e.net == net and e.kind == "blocked"]
        mine = retried_open.get(net, [])
        if blocked and all(any(math.dist(p, q) < 1e-6 for q in mine) for p in blocked):
            plan.blocked_nets.discard(net)
