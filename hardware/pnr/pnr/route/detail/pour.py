"""Outer-layer pours of a plane partition with a ``region`` (:mod:`pnr.plane_partition`).

A partition entry with a ``region`` divides a routed layer inside that region among
its rails (a power stage's hot-rod lands, its switch nodes and inductor pads): each
rail's territory becomes a zone that owns its lands (``connect: solid``). The router
then treats it like copper the net already has:

- :func:`reserve` claims each territory for its net on its layer (track and via
  halos), so no other net's track or via is planned through the pour; a cell an other
  net's pad already owns is left to that pad (the zone fill keeps its clearance);
- the pads inside their net's territory on that layer (``covered``) need no escape
  and no plane drop: the pour joins them;
- :func:`ports` gives a net whose other pads still route one access cell inside its
  territory (the maze joins them to the pour there);
- :func:`stitch` drills ``stitch_vias`` through vias inside each territory of a net
  with a dedicated plane, at sites where a drop of that net may land, so the pour
  reaches its plane.
"""

from __future__ import annotations

import math
from typing import Dict, List, Tuple

from pnr.fixed_block import point_in_polygon


def _inside(region, p) -> bool:
    if region.outline is None:
        return True
    return point_in_polygon(p, region.outline) and not any(
        point_in_polygon(p, h) for h in region.holes
    )


def reserve(grid, graph, parts) -> Dict[str, dict]:
    """Claim the outer partitions' territories on ``grid``; returns ``{net: {layer,
    regions, pads}}`` with the (ref, pad) pairs each territory covers."""
    from pnr.place.geometry import pad_rects

    from .grid import pad_layer

    cell_radius = grid.pitch / math.sqrt(2)
    track_grow = grid.clearance + grid.track_width / 2 + cell_radius
    via_grow = grid.clearance + grid.via_radius + cell_radius
    # The exact escape tests sample centre lines (grid.fixed_owned): there the pour's
    # own reach, without the cell margin the maze's halo needs.
    exact_grow = grid.clearance + grid.track_width / 2
    out: Dict[str, dict] = {}
    for part in parts:
        la = grid.layers.index(part.layer)
        for r in part.regions:
            if r.outline is None:
                continue
            row = out.setdefault(r.net, dict(layer=part.layer, regions=[], pads=[]))
            row["regions"].append(r)
            for table, grow in ((grid.pad_net, track_grow), (grid.via_halo, via_grow)):
                cells = grid.polygon_cells(r.outline, r.holes, grow)
                for j, i in zip(*cells.nonzero()):
                    key = (la, int(i), int(j))
                    if table.get(key) is None:
                        table[key] = r.net
            exact = grid.polygon_cells(r.outline, r.holes, exact_grow)
            for j, i in zip(*exact.nonzero()):
                key = (la, int(i), int(j))
                if grid.pad_net.get(key) == r.net and grid.fixed_owned.get(key) is None:
                    grid.fixed_owned[key] = r.net
    for comp in graph.components:
        for (name, net, rect), pad in zip(pad_rects(comp), comp.pads):
            row = out.get(net)
            if row is None or pad.through_hole:
                continue
            if grid.layers[pad_layer(grid, comp, pad)] != row["layer"]:
                continue
            if any(_inside(r, (rect.cx, rect.cy)) for r in row["regions"]):
                row["pads"].append((comp.ref, name))
    for row in out.values():
        row["pads"].sort()
    return out


def covered(poured) -> set:
    """Every (ref, pad) a pour joins."""
    return {p for row in poured.values() for p in row["pads"]}


def _cells(grid, row, net):
    la = grid.layers.index(row["layer"])
    found = []
    for r in row["regions"]:
        mask = grid.polygon_cells(r.outline, r.holes, 0.0)
        for j, i in zip(*mask.nonzero()):
            if grid.passable(la, int(i), int(j), net):
                found.append((la, int(i), int(j)))
    return found


def ports(grid, poured, net_access) -> Dict[str, list]:
    """``{net: [Cell]}``: for each poured net that still has other access cells, the
    territory cell nearest their centroid (where the maze joins them to the pour)."""
    from .grid import Cell

    out = {}
    for net, row in sorted(poured.items()):
        others = net_access.get(net) or []
        if not row["pads"] or not others:
            continue
        cx = sum(grid.center_of(c.i, c.j)[0] for c in others) / len(others)
        cy = sum(grid.center_of(c.i, c.j)[1] for c in others) / len(others)
        cells = _cells(grid, row, net)
        if not cells:
            continue
        la, i, j = min(
            cells,
            key=lambda c: (math.dist(grid.center_of(c[1], c[2]), (cx, cy)), c),
        )
        out[net] = [Cell(la, i, j)]
    return out


def stitch(grid, poured, plane_nets, counts, plane_access=None, via_keepout=1, spacing=1.0):
    """Through vias inside each territory of a net with a dedicated plane: up to
    ``counts[net]`` sites (greedy, nearest the covered pads' centroid first, at least
    ``spacing`` mm apart) where a drop of the net may land (exact clearance tests of
    the drop planner, and the net's plane region). Returns ``{net: [(x, y)]}``; the
    vias are reserved on the grid and listed in ``grid.escape_vias``."""
    from .escape import _reserve_via
    from .joint_escape import _drop_via_clear

    out: Dict[str, List[Tuple[float, float]]] = {}
    for net, row in sorted(poured.items()):
        want = int(counts.get(net, 0) or 0)
        if net not in plane_nets or want <= 0 or not row["pads"]:
            continue
        centres = []
        for r in row["regions"]:
            xs = [p[0] for p in r.outline]
            ys = [p[1] for p in r.outline]
            centres.append(((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2))
        cx = sum(c[0] for c in centres) / len(centres)
        cy = sum(c[1] for c in centres) / len(centres)
        cells = sorted(
            _cells(grid, row, net),
            key=lambda c: (math.dist(grid.center_of(c[1], c[2]), (cx, cy)), c),
        )
        chosen: List[Tuple[float, float]] = []
        for la, i, j in cells:
            if len(chosen) >= want:
                break
            q = grid.center_of(i, j)
            if any(math.dist(q, p) < spacing - 1e-9 for p in chosen):
                continue
            if plane_access is not None and not plane_access.site_ok(net, q):
                continue
            if not _drop_via_clear(grid, net, q):
                continue
            chosen.append(q)
            _reserve_via(grid, i, j, net, via_keepout)
            grid.escape_vias.append((net, q))
        if chosen:
            out[net] = chosen
    return out
