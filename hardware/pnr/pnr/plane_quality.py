"""How well a partition of one plane layer serves its rails (``plane_partition``).

A partition (:mod:`pnr.plane_partition`) can be legal and connected and still make no
sense: a rail of a milliamp holding a third of the layer, a region whose only BGA ball
has no via site, a fill net that already has two planes of its own elsewhere in the
stack. This module puts numbers on that, per rail region and per layer:

``area_mm2`` / ``need_mm2`` / ``area_ratio``
    the copper a region holds against the copper its terminals need: its trunk at its
    width plus its terminal lands (``need``);
``compactness``
    the isoperimetric quotient ``4 pi A / P^2`` of the region (1 for a disc);
``trunk_mm`` / ``steiner_mm`` / ``detour``
    the trunk the partition drew against the Euclidean minimum spanning tree of the
    terminals (an upper bound on, and within 15 % of, their Steiner length on an empty
    layer): how far other rails pushed the trunk round;
``unreached`` / ``blocked`` / ``via_blocked`` / ``needs_via``
    terminal reachability: terminals no trunk reached, pad terminals with no free cell
    on the layer within their reach, pad terminals with no free through-via site on the
    router's grid (an inner BGA ball whose neighbours' vias wall it in), and pad
    terminals that still need a new via at all;
``dead_mm2``
    copper farther than the rail's width (at least 1 mm) from its trunk and terminals:
    area serving no terminal;
``neck_mm``
    the narrowest copper on the trunk or on a terminal's widest way to the root;
``ir_drop_mv`` / ``ir_margin_mv``
    the trunk's estimated DC drop (sheet resistance over the root's path to its
    farthest terminal at the trunk width, plus two via barrels) and its margin to the
    rail's budget;
``frag_pieces`` / ``frag_cut_mm2``
    how much the region fragments its neighbours: the extra pieces the rest of the
    layer falls into without it, and the area cut off from the largest of them;
``split_mm`` (per region and layer)
    the region's boundary against other copper of the layer: the slots it cuts into the
    plane (a return path for the adjacent signal layer);
``redundant_fill`` (layer)
    the fill net already has a dedicated plane elsewhere in the stack.

:func:`penalty` folds them into one number in millimetres of track (the unit the
allocation search, :mod:`pnr.rail_alloc`, compares a plane against a trace in) and
:func:`warnings` into quantified ERC-style records, both against the module's limits.
:func:`judge` is the same measurement on a routed board's filled zones, in plain Python
(KiCad's own Python may lack numpy), for the ladder's ``plane_quality`` check.

The limits (``LIMITS``) were derived from measured layouts: the ladder's partitioned
rungs whose layouts are good (``11-buck-vqfnhr-4L-SGPS-pour``, every seed; the
``-rails`` rung under the allocation search) against the one an owner review found
nonsense (the ``-rails`` rung's In4 split into VDD, VDDA and VBAT with a GND fill,
2026-10-06): each limit sits above the worst good value measured and below the bad
one, with the margin that leaves -- about 2x for ``area_ratio_max`` (3.24 measured),
closer for ``detour_max`` (1.34 measured, the worse bad case 3.97). ``docs/plane-
partition.md`` gives the table.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

# The judge's limits (a region failing one makes no sense) and the engine's warning
# limits (the same numbers). Derivation: the module doc and docs/plane-partition.md.
LIMITS = dict(
    # A region other than the layer's owner (the rail with the most current, or the
    # declared leftover) may hold at most this many times the copper its terminals
    # need. Good layouts measured at most 3.24 (the -pour rung's VIN); the nonsense
    # ones 3.6 to 18 (VBAT) and 8.3 up (VDDA).
    area_ratio_max=6.0,
    # A trunk (or a region's geodesic tree, judged) at most this many times the
    # Euclidean MST of its terminals. Good layouts measured at most 1.34; the
    # nonsense ones up to 3.97.
    detour_max=1.6,
    # Every region serves at least this many terminals (one terminal carries no
    # current anywhere).
    terminals_min=2,
    # ... and a piece of an outer pour (``lands``), where a land's own piece is
    # stitched to its plane by itself (``pieces``): measured down to one.
    terminals_min_lands=1,
    # A filled piece under this area is a sliver of the fill (a thermal spoke's
    # corner), not a region: counted, not judged.
    piece_min_mm2=1.0,
)

# Penalty weights, millimetres of track per unit (pnr.rail_alloc compares a plane's
# penalty with a traced rail's estimated length in the same unit). An open is worth
# more than any copper; a slot in a reference plane about its own length of track;
# area serving no terminal one millimetre per 10 mm^2.
WEIGHTS = dict(
    unreached=50.0,  # per terminal no trunk reaches, or with no via site
    necked=10.0,  # per terminal behind copper under min_width_mm
    ir_short=50.0,  # a rail over its IR budget (+1 per mV over)
    dead=0.1,  # per mm^2 of a non-owner region serving no terminal
    detour=0.5,  # per mm of trunk beyond 1.2x its terminals' MST
    frag_cut=0.1,  # per mm^2 a region cuts off its neighbours
    frag_piece=5.0,  # per extra piece a region splits its neighbours into
    split=0.5,  # per mm of split line (each side counted half)
    redundant=0.1,  # per mm^2 of a fill net with a dedicated plane elsewhere
    redundant_flat=25.0,  # and once for the fill zone itself (it pours every pocket)
)
TERMINAL_SLACK_MM = 0.3  # a terminal joins a filled piece within this of its land
# (a thermal relief's spokes reach out from an antipad that wide)
DETOUR_FREE = 1.2  # a trunk this much longer than its terminals' MST costs nothing
COARSE_MM = 0.4  # the raster the fragmentation is measured on


# ------------------------------------------------------------------ geometry


def emst(points: Sequence[Tuple[float, float]]) -> float:
    """Length of the Euclidean minimum spanning tree of ``points`` (Prim, O(n^2))."""
    pts = list(points)
    if len(pts) < 2:
        return 0.0
    inf = math.inf
    best = [inf] * len(pts)
    used = [False] * len(pts)
    best[0] = 0.0
    total = 0.0
    for _ in range(len(pts)):
        k = min((i for i in range(len(pts)) if not used[i]), key=lambda i: (best[i], i))
        used[k] = True
        total += best[k]
        for i in range(len(pts)):
            if not used[i]:
                d = math.dist(pts[k], pts[i])
                if d < best[i]:
                    best[i] = d
    return total


def _edges(mask, other):
    """Count of 4-neighbour cell edges from ``mask`` cells to ``other`` cells."""
    import numpy as np

    m = np.pad(mask, 1)
    o = np.pad(other, 1)
    inner = m[1:-1, 1:-1]
    return int(
        (inner & o[:-2, 1:-1]).sum()
        + (inner & o[2:, 1:-1]).sum()
        + (inner & o[1:-1, :-2]).sum()
        + (inner & o[1:-1, 2:]).sum()
    )


def _coarse(mask, f):
    """``mask`` on a raster ``f`` times coarser (a cell set when half its cells are)."""
    import numpy as np

    if f <= 1:
        return mask.copy()
    ny, nx = mask.shape
    cy, cx = -(-ny // f), -(-nx // f)
    pad = np.zeros((cy * f, cx * f), dtype=float)
    pad[:ny, :nx] = mask
    return pad.reshape(cy, f, cx, f).mean(axis=(1, 3)) >= 0.5


def _pieces(mask, cell_mm2, min_mm2=1.0):
    """Areas (mm^2) of the 4-connected pieces of ``mask`` of at least ``min_mm2``."""
    import numpy as np

    from pnr.plane_partition import _label

    lab, count = _label(mask)
    if not count:
        return []
    sizes = np.bincount(lab[lab >= 0].ravel(), minlength=count) * cell_mm2
    return sorted((float(s) for s in sizes if s >= min_mm2), reverse=True)


# ------------------------------------------------------------- the partition


def measure(
    *,
    h,
    nets,
    terminals,
    cells_of,
    free,
    per_net_block,
    grown,
    spines,
    widths,
    report,
    currents,
    budgets,
    owner,
    r_sq,
    barrel_mohm,
    gap_mm,
):
    """The metrics of one partition (see the module doc) from
    :func:`pnr.plane_partition._partition`'s rasters: ``grown`` the final territory
    labels (index into ``nets``), ``spines`` each rail's trunk cells, ``report`` its
    per-net report rows. ``owner``: the rail that takes the leftover (the entry's
    ``fill`` when it names a rail; with one rail, that rail), else None."""
    import numpy as np

    from pnr.plane_partition import dilate

    cell = h * h
    rows = {}
    if owner is None and nets:
        # Competing rails (or a non-rail fill): the one with the most current (then
        # area) is the layer's main rail, whose extra copper is no waste.
        owner = max(
            nets, key=lambda n: (currents.get(n) or 0.0, int((grown == nets.index(n)).sum()), n)
        )
    f = max(1, int(round(COARSE_MM / h)))
    free_c = _coarse(free, f)
    cell_c = (f * h) ** 2
    base = _pieces(free_c, cell_c)
    base_cut = sum(base[1:]) if base else 0.0
    split_total = 0.0
    assigned = grown >= 0
    for k, n in enumerate(nets):
        info = report["nets"][n]
        mine = grown == k
        area = float(mine.sum()) * cell
        own = np.zeros_like(free)
        for disc in cells_of[n]:
            own |= disc
        w = widths.get(n, 0.0)
        core = own.copy()
        if spines[n].any():
            core |= dilate(spines[n], w / h / 2)
        need = float((core & (free | own)).sum()) * cell
        perim = _edges(mine, ~mine) * h
        serve = dilate(core, max(w, 1.0) / h)
        dead = float((mine & ~serve).sum()) * cell
        steiner = emst([t.at for t in terminals[n]])
        trunk = float(info.get("tree_mm") or 0.0)
        pad_terms = [(t, d) for t, d in zip(terminals[n], cells_of[n]) if t.kind == "pad"]
        room = free & ~per_net_block[n]
        blocked = sorted(t.name for t, d in pad_terms if not (d & room).any())
        necks = [v for v in (info.get("core_min_mm"), info.get("way_min_mm")) if v is not None]
        current = currents.get(n) or 0.0
        drop_mohm = None
        if current and info.get("path_mm") is not None and w > 0:
            drop_mohm = r_sq * float(info["path_mm"]) / w * 1e3 + 2 * barrel_mohm
        budget = budgets.get(n)
        split = _edges(mine, free & ~mine) * h
        split_total += split / 2
        rest = _coarse(free & ~dilate(mine, gap_mm / h + 1), f) if mine.any() else free_c
        pieces = _pieces(rest, cell_c)
        rows[n] = dict(
            area_mm2=round(area, 3),
            need_mm2=round(need, 3),
            area_ratio=round(area / need, 3) if need > 0 else None,
            compactness=round(min(1.0, 4 * math.pi * area / perim**2), 4) if perim else None,
            trunk_mm=round(trunk, 3),
            steiner_mm=round(steiner, 3),
            detour=round(trunk / steiner, 3) if steiner > 0 and trunk > 0 else None,
            terminals=len(terminals[n]),
            unreached=len(info.get("unreached") or ()),
            blocked=blocked,
            needs_via=len(pad_terms),
            necked=len(info.get("necked") or ()),
            dead_mm2=round(dead, 3),
            neck_mm=round(min(necks), 4) if necks else None,
            ir_drop_mv=round(drop_mohm * current, 3) if drop_mohm is not None else None,
            ir_margin_mv=(
                round((budget - drop_mohm) * current, 3)
                if budget and drop_mohm is not None
                else None
            ),
            frag_pieces=max(0, len(pieces) - len(base)),
            frag_cut_mm2=round(max(0.0, (sum(pieces[1:]) if pieces else 0.0) - base_cut), 3),
            split_mm=round(split, 3),
            owner=n == owner,
        )
    free_area = float(free.sum()) * cell
    layer = dict(
        free_mm2=round(free_area, 3),
        assigned_mm2=round(float(assigned.sum()) * cell, 3),
        unassigned_mm2=round(float((free & ~assigned).sum()) * cell, 3),
        split_mm=round(split_total, 3),
        owner=owner,
        rails=list(nets),
    )
    return dict(nets=rows, layer=layer)


def owner_of(nets, fill, currents) -> Optional[str]:
    """The rail that takes the leftover: ``fill`` when it is one of ``nets``; the
    only rail; else None (the rails compete for it, or a non-rail fill takes it:
    :func:`measure` then treats the rail with the most current as the owner, as
    :func:`judge` does)."""
    if fill and fill in nets:
        return fill
    if len(nets) == 1:
        return nets[0]
    return None


def penalty(quality) -> Tuple[float, Dict[str, float]]:
    """The partition's cost in millimetres of track (:data:`WEIGHTS`) and its
    breakdown by term."""
    w = WEIGHTS
    parts: Dict[str, float] = {}

    def add(key, value):
        if value:
            parts[key] = round(parts.get(key, 0.0) + value, 3)

    for _n, row in sorted(quality["nets"].items()):
        add("unreached", w["unreached"] * (row["unreached"] + len(row["blocked"])))
        add("unreached", w["unreached"] * len(row.get("via_blocked") or ()))
        add("necked", w["necked"] * row["necked"])
        if row.get("ir_margin_mv") is not None and row["ir_margin_mv"] < 0:
            add("ir_short", w["ir_short"] - row["ir_margin_mv"])
        if not row["owner"]:
            add("dead", w["dead"] * row["dead_mm2"])
            add("fragments", w["frag_cut"] * row["frag_cut_mm2"])
            add("fragments", w["frag_piece"] * row["frag_pieces"])
        if row["steiner_mm"] > 0:
            add("detour", w["detour"] * max(0.0, row["trunk_mm"] - DETOUR_FREE * row["steiner_mm"]))
    layer = quality["layer"]
    add("split", w["split"] * layer["split_mm"])
    red = layer.get("redundant_fill")
    if red:
        add("redundant_fill", w["redundant_flat"] + w["redundant"] * red["area_mm2"])
    return round(sum(parts.values()), 3), parts


def warnings(quality, layer_name) -> List[dict]:
    """Quantified ERC-style warnings (code, net, value, limit, message) for the
    partition's regions that fail :data:`LIMITS` or leave a terminal without a way
    onto the layer."""
    lim = LIMITS
    out = []

    def warn(code, net, value, limit, message):
        out.append(
            dict(code=code, layer=layer_name, net=net, value=value, limit=limit, message=message)
        )

    layer = quality["layer"]
    red = layer.get("redundant_fill")
    if red:
        warn(
            "redundant_fill",
            red["net"],
            red["area_mm2"],
            0,
            "%s: fill %s takes %.0f mm^2 although it has a dedicated plane on %s"
            % (layer_name, red["net"], red["area_mm2"], ", ".join(red["planes"])),
        )
    for n, row in sorted(quality["nets"].items()):
        lost = row["unreached"] + len(row["blocked"]) + len(row.get("via_blocked") or ())
        if lost:
            warn(
                "unreachable_terminal",
                n,
                lost,
                0,
                "%s on %s: %d terminal(s) cannot reach the region (%s)"
                % (
                    n,
                    layer_name,
                    lost,
                    ", ".join((row["blocked"] + list(row.get("via_blocked") or ()))[:6])
                    or "no trunk",
                ),
            )
        if row["terminals"] < lim["terminals_min"]:
            warn(
                "too_few_terminals",
                n,
                row["terminals"],
                lim["terminals_min"],
                "%s on %s: the region serves %d terminal(s)" % (n, layer_name, row["terminals"]),
            )
        if (
            not row["owner"]
            and not layer.get("outer")
            and row["area_ratio"]
            and row["area_ratio"] > lim["area_ratio_max"]
        ):
            warn(
                "area_beyond_need",
                n,
                row["area_ratio"],
                lim["area_ratio_max"],
                "%s on %s: %.0f mm^2 for %.0f mm^2 of need (%.1fx)"
                % (n, layer_name, row["area_mm2"], row["need_mm2"], row["area_ratio"]),
            )
        if row["detour"] and row["detour"] > lim["detour_max"]:
            warn(
                "trunk_detour",
                n,
                row["detour"],
                lim["detour_max"],
                "%s on %s: trunk %.1f mm for a %.1f mm terminal tree (%.2fx)"
                % (n, layer_name, row["trunk_mm"], row["steiner_mm"], row["detour"]),
            )
        if row.get("ir_margin_mv") is not None and row["ir_margin_mv"] < 0:
            warn(
                "ir_margin",
                n,
                row["ir_margin_mv"],
                0,
                "%s on %s: estimated drop %.1f mV, %.1f mV over budget"
                % (n, layer_name, row["ir_drop_mv"], -row["ir_margin_mv"]),
            )
    return out


# ---------------------------------------------------------------- the judge
#
# Plain Python (no numpy): KiCad's own Python runs it in check_constraints.py.


def raster_polygons(polys, x0, y0, nx, ny, h):
    """Cells (i, j) whose centre lies inside ``polys`` (each a list of rings, the
    first the outline and the rest holes; even-odd), by scanlines."""
    out = set()
    for rings in polys:
        edges = []
        for ring in rings:
            for a, b in zip(ring, ring[1:] + ring[:1]):
                if a[1] != b[1]:
                    edges.append((a, b))
        for j in range(ny):
            y = y0 + (j + 0.5) * h
            xs = []
            for (ax, ay), (bx, by) in edges:
                if (ay > y) != (by > y):
                    xs.append(ax + (y - ay) / (by - ay) * (bx - ax))
            xs.sort()
            for xa, xb in zip(xs[::2], xs[1::2]):
                i0 = max(0, int(math.ceil((xa - x0) / h - 0.5)))
                i1 = min(nx - 1, int(math.floor((xb - x0) / h - 0.5)))
                for i in range(i0, i1 + 1):
                    out.add((i, j))
    return out


def _components(cells):
    """4-connected pieces of a cell set."""
    left = set(cells)
    pieces = []
    while left:
        seed = left.pop()
        piece = {seed}
        stack = [seed]
        while stack:
            i, j = stack.pop()
            for nb in ((i + 1, j), (i - 1, j), (i, j + 1), (i, j - 1)):
                if nb in left:
                    left.discard(nb)
                    piece.add(nb)
                    stack.append(nb)
        pieces.append(piece)
    return pieces


def _geodesic(cells, sources, targets):
    """BFS distance (cells, 8-neighbour at 1 / sqrt 2) in ``cells`` from ``sources``
    to each of ``targets`` (None where unreachable)."""
    import heapq

    dist = {s: 0.0 for s in sources}
    heap = [(0.0, s) for s in sorted(sources)]
    want = set(targets)
    found = {}
    sq2 = math.sqrt(2.0)
    while heap and want:
        d, c = heapq.heappop(heap)
        if d > dist.get(c, math.inf):
            continue
        if c in want:
            found[c] = d
            want.discard(c)
        i, j = c
        for di, dj, step in (
            (1, 0, 1.0),
            (-1, 0, 1.0),
            (0, 1, 1.0),
            (0, -1, 1.0),
            (1, 1, sq2),
            (1, -1, sq2),
            (-1, 1, sq2),
            (-1, -1, sq2),
        ):
            nb = (i + di, j + dj)
            if nb in cells and d + step < dist.get(nb, math.inf):
                dist[nb] = d + step
                heapq.heappush(heap, (d + step, nb))
    return [found.get(t) for t in targets]


def _geodesic_mst(cells, terms):
    """Prim's tree over ``terms`` (cells) with geodesic distances inside ``cells``;
    None when a terminal is unreachable."""
    if len(terms) < 2:
        return 0.0
    inside = [terms[0]]
    rest = list(terms[1:])
    total = 0.0
    while rest:
        d = _geodesic(cells, inside, rest)
        if all(x is None for x in d):
            return None
        k = min((i for i in range(len(rest)) if d[i] is not None), key=lambda i: (d[i], i))
        total += d[k]
        inside.append(rest.pop(k))
    return total


def judge(layer, *, h=0.2, limits=None):
    """The ``plane_quality`` verdict of one layer's filled zones (plain Python).

    ``layer``: ``outline`` [x0, y0, x1, y1] mm; ``zones`` {net: [rings, ...]} (each
    piece's filled outline then holes, mm); ``terminals`` {net: [(x, y, r_mm), ...]}
    (the net's vias and through-hole pads on the layer, and with ``lands`` its
    surface pads there); ``candidates`` the rails the partition may give a region;
    ``currents`` {net: A}; ``min_width_mm``; ``planes_elsewhere`` {net: [layers]} (the
    net's dedicated planes on other layers). Returns ``(ok, measured, limits)``."""
    lim = dict(LIMITS, **(limits or {}))
    x0, y0, x1, y1 = layer["outline"]
    nx = max(1, int(math.ceil((x1 - x0) / h)))
    ny = max(1, int(math.ceil((y1 - y0) / h)))
    cand = set(layer.get("candidates") or ())
    currents = layer.get("currents") or {}
    min_w = float(layer.get("min_width_mm") or 0.0)
    nets = sorted(layer["zones"])
    area = {}
    cells_of = {}
    for n in nets:
        cells_of[n] = raster_polygons(layer["zones"][n], x0, y0, nx, ny, h)
        area[n] = len(cells_of[n]) * h * h
    # The owner is the highest-current rail of every *candidate* (not only the ones
    # with a zone here): a rail traced away keeps its claim on the layer, so a
    # milliamp rail left holding it all is still judged a non-owner, not waved
    # through as the owner by default.
    owner = None
    if cand:
        owner = max(cand, key=lambda n: (currents.get(n) or 0.0, area.get(n, 0.0), n))
    elif nets:
        owner = max(nets, key=lambda n: (currents.get(n) or 0.0, area[n], n))
    failures = []
    rows = {}
    for n in nets:
        row = dict(area_mm2=round(area[n], 2), owner=n == owner, pieces=[])
        if n not in cand:
            planes = (layer.get("planes_elsewhere") or {}).get(n) or []
            row["fill"] = True
            if planes:
                row["redundant_with"] = planes
                failures.append(
                    "%s: fill %s is redundant (planes on %s)" % (n, n, ", ".join(planes))
                )
            rows[n] = row
            continue
        terms = layer["terminals"].get(n) or []
        row["slivers"] = 0
        matched = set()
        for piece in _components(cells_of[n]):
            if len(piece) * h * h < lim["piece_min_mm2"]:
                row["slivers"] += 1
                continue
            mine = []
            for x, y, r in terms:
                c = (int((x - x0) / h), int((y - y0) / h))
                rc = max(0, int(math.ceil((r + TERMINAL_SLACK_MM) / h)))
                hit = next(
                    (
                        (c[0] + di, c[1] + dj)
                        for di in range(-rc, rc + 1)
                        for dj in range(-rc, rc + 1)
                        if (c[0] + di, c[1] + dj) in piece
                    ),
                    None,
                )
                if hit is not None:
                    mine.append(((x, y, r), hit))
                    matched.add((x, y, r))
            parea = len(piece) * h * h
            pts = [(t[0], t[1]) for t, _ in mine]
            steiner = emst(pts)
            need = steiner * max(min_w, h) + sum(math.pi * t[2] ** 2 for t, _ in mine)
            ratio = parea / need if need > 0 else None
            geo = _geodesic_mst(piece, sorted(set(c for _, c in mine)))
            detour = (geo * h) / steiner if geo is not None and steiner > 1.0 else None
            prow = dict(
                area_mm2=round(parea, 2),
                terminals=len(mine),
                need_mm2=round(need, 2),
                area_ratio=round(ratio, 2) if ratio else None,
                steiner_mm=round(steiner, 2),
                tree_mm=round(geo * h, 2) if geo is not None else None,
                detour=round(detour, 3) if detour else None,
            )
            row["pieces"].append(prow)
            tmin = lim["terminals_min_lands"] if layer.get("lands") else lim["terminals_min"]
            if len(mine) < tmin:
                failures.append(
                    "%s: a %.0f mm^2 region serves %d terminal(s)" % (n, parea, len(mine))
                )
            if n != owner and ratio and ratio > lim["area_ratio_max"]:
                failures.append(
                    "%s: %.0f mm^2 for %.0f mm^2 of need (%.1fx > %.1fx)"
                    % (n, parea, need, ratio, lim["area_ratio_max"])
                )
            if detour and detour > lim["detour_max"]:
                failures.append(
                    "%s: tree %.1f mm for %.1f mm of terminal MST (%.2fx > %.2fx)"
                    % (n, geo * h, steiner, detour, lim["detour_max"])
                )
        unreached = [t for t in terms if t not in matched]
        if unreached:
            row["unreached"] = len(unreached)
            failures.append(
                "%s: %d terminal(s) cannot reach the region (no via site, or walled off "
                "by a neighbour's copper)" % (n, len(unreached))
            )
        rows[n] = row
    measured = dict(nets=rows, owner=owner, failures=failures)
    return not failures, measured, dict(lim)
