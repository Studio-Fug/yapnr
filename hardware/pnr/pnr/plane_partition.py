"""Divide one dedicated plane layer among several supply rails (``plane_partition``).

Where several nets share a power layer, :func:`pnr.stack.plane_regions` gives every
net but the one with most pads its pads' bounding box plus 2 mm: boxes of rails
whose balls spread over the same package overlap, and the smaller box wins. A
partition instead gives each rail a **connected territory** built from its own
terminals:

1. the layer is rasterized at ``h_mm`` (0.1 mm) inside the outline less the edge
   clearance; foreign through copper is blocked with its clearance (other nets'
   planned vias, fixed vias, plated holes, mounting holes), and so are keepouts
   that bar pours on the layer (except for the nets they allow);
2. a rail's terminals are its planned drop vias (the fanout's, fixed ones: their
   lands) and, for a surface pad without one yet, the disc within
   ``terminal_reach_mm`` of the pad where the drop planner puts its via;
3. rails are taken in order (peak current, then terminal count, then name; or as
   listed); each gets a Steiner tree over its terminals (shortest-path heuristic:
   Dijkstra from the tree to the nearest remaining terminal; a cell costs more where
   the trunk would be narrower than it should be, and inside another rail's
   terminal disc), claimed with its width ``w`` = the largest of ``min_width_mm``,
   the IPC-2221 internal width for the rail's current, and ``R_sq L / R_share``
   for its IR budget (``L`` the root's path to its farthest terminal, ``R_share``
   the budget less two via barrels, at least a quarter of it; at most 10 mm),
   keeping ``split_gap_mm`` of copper from every other rail. ``min_width_mm`` is a
   hard limit: the tree passes only where a zone that wide fits, except inside the
   rail's own terminal discs (and within ``neck_mm``, default 0, of them). A
   terminal no such tree reaches is joined where a zone of the board's minimum
   width (the fab track width writeback gives the zones) still fills, and reported
   **necked** with the narrowest copper on its way (step 6). Foreign copper keeps
   the larger class clearance of the pair, as KiCad's fill does;
4. a terminal no path reaches at all is reported unreached (the pad's drop then
   fails in the drop planner, as a pad outside its region does);
5. the territories grow into the free cells round the board (a breadth-first
   competition), and grown copper is carved back to keep the split gap;
6. each territory becomes polygons (cell boundaries, holes kept where another rail
   or free copper sits in them), checked for one connected piece per rail, for
   the narrowest width along its trunk, and per terminal for the widest way to the
   root (the largest, over all paths in the drawn copper, of the narrowest copper
   along the path, the terminal discs exempt): a terminal behind copper narrower
   than ``min_width_mm`` is **necked** (reported with the neck's width and place,
   a failure site of the route, and a warning), as is a trunk narrower than its
   IPC-2221 width (``ipc_neck``). ``fill`` takes what is left (a zone of that net
   over the whole outline at priority 0, under the rails);
7. with ``core_no_vias`` other nets' vias may not land on a trunk's claimed copper
   (its width ``w`` about the centre line, where the trunk could take it: the
   router's net keepouts), so a row of vias cannot cut a rail's trunk.

The result is cached by the digest of its inputs. :func:`for_route` gathers the
inputs on the detailed router's grid; :class:`Partition` gives the regions
(:class:`pnr.stack.Region`, which :class:`pnr.stack.PlaneAccess` and the writeback
use) and the report. numpy only.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import math
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

_CACHE: Dict[str, "Partition"] = {}
MAX_WIDTH_MM = 10.0  # a trunk's widest target (its room decides below that)


@dataclass
class Terminal:
    """One terminal of a rail: a via land (``kind="via"``, exact) or a pad's reach
    disc (``kind="pad"``, where its drop will land)."""

    name: str
    kind: str
    at: Tuple[float, float]
    radius: float


@dataclass
class Partition:
    layer: str
    regions: list  # pnr.stack.Region
    report: dict
    # net -> (centre-line points mm, keepout radius mm) for core_no_vias
    cores: Dict[str, Tuple[List[Tuple[float, float]], float]] = field(default_factory=dict)

    def rows(self) -> List[dict]:
        """The regions as JSON (routes.json ``plane_regions``)."""
        return [
            dict(
                layer=r.layer,
                net=r.net,
                priority=r.priority,
                outline=None if r.outline is None else [list(p) for p in r.outline],
                holes=[[list(p) for p in h] for h in r.holes],
            )
            for r in self.regions
        ]


def regions_from_rows(rows) -> list:
    """:class:`pnr.stack.Region` objects from :meth:`Partition.rows`."""
    from pnr.stack import Region

    return [
        Region(
            r["layer"],
            r["net"],
            int(r["priority"]),
            None if r["outline"] is None else tuple(tuple(p) for p in r["outline"]),
            False,
            tuple(tuple(tuple(p) for p in h) for h in r.get("holes") or ()),
        )
        for r in rows
    ]


# --------------------------------------------------------------- raster tools


def edt(mask: np.ndarray, cap: float) -> np.ndarray:
    """Euclidean distance (cells) from each cell centre to the nearest ``mask`` cell,
    exact up to ``cap`` cells (larger values are only lower bounds above it)."""
    ny, nx = mask.shape
    big = float(cap) + 2.0
    if not mask.any():
        return np.full(mask.shape, big)
    idx = np.arange(nx)
    left = np.maximum.accumulate(np.where(mask, idx, -(10**9)), axis=1)
    right = np.minimum.accumulate(np.where(mask, idx, 10**9)[:, ::-1], axis=1)[:, ::-1]
    g = np.minimum(np.minimum(idx - left, right - idx).astype(float), big)
    g2 = g * g
    out = g2.copy()
    for dy in range(1, int(math.ceil(cap)) + 2):
        if dy >= ny:
            break
        dd = float(dy * dy)
        np.minimum(out[dy:], g2[:-dy] + dd, out=out[dy:])
        np.minimum(out[:-dy], g2[dy:] + dd, out=out[:-dy])
    return np.sqrt(out)


def dilate(mask: np.ndarray, radius_cells: float) -> np.ndarray:
    """Cells within ``radius_cells`` (centre to centre) of ``mask``."""
    if radius_cells <= 0:
        return mask.copy()
    return edt(mask, radius_cells) <= radius_cells + 1e-9


def _label(mask: np.ndarray) -> Tuple[np.ndarray, int]:
    """4-connected components of ``mask``: (labels, count), -1 off the mask."""
    ny, nx = mask.shape
    lab = np.full(mask.shape, -1, dtype=np.int64)
    flat = mask.ravel()
    labf = lab.ravel()
    count = 0
    for start in np.flatnonzero(flat):
        if labf[start] >= 0:
            continue
        labf[start] = count
        q = deque([int(start)])
        while q:
            k = q.popleft()
            j, i = divmod(k, nx)
            for nb, ok in (
                (k - 1, i > 0),
                (k + 1, i < nx - 1),
                (k - nx, j > 0),
                (k + nx, j < ny - 1),
            ):
                if ok and flat[nb] and labf[nb] < 0:
                    labf[nb] = count
                    q.append(nb)
        count += 1
    return lab, count


class _Grid:
    def __init__(self, width, height, h):
        self.h = float(h)
        self.nx = max(1, int(math.ceil(width / h)))
        self.ny = max(1, int(math.ceil(height / h)))
        self.xs = (np.arange(self.nx) + 0.5) * h
        self.ys = (np.arange(self.ny) + 0.5) * h

    def zeros(self):
        return np.zeros((self.ny, self.nx), dtype=bool)

    def cell(self, p):
        i = min(max(int(p[0] / self.h), 0), self.nx - 1)
        j = min(max(int(p[1] / self.h), 0), self.ny - 1)
        return i, j

    def disc(self, c, r):
        out = self.zeros()
        i0, j0 = self.cell((c[0] - r - self.h, c[1] - r - self.h))
        i1, j1 = self.cell((c[0] + r + self.h, c[1] + r + self.h))
        X, Y = np.meshgrid(self.xs[i0 : i1 + 1], self.ys[j0 : j1 + 1])
        out[j0 : j1 + 1, i0 : i1 + 1] = (X - c[0]) ** 2 + (Y - c[1]) ** 2 <= r * r + 1e-12
        if not out.any():
            i, j = self.cell(c)
            out[j, i] = True
        return out

    def polygon(self, rings, grow=0.0):
        """Cells whose centre is inside ``rings`` (even-odd) or within ``grow`` of
        their boundary."""
        out = self.zeros()
        rings = [np.asarray(r, dtype=float) for r in rings if len(r) >= 3]
        if not rings:
            return out
        e = np.vstack([np.hstack([r, np.roll(r, -1, axis=0)]) for r in rings])
        xs, ys = self.xs, self.ys
        X, Y = np.meshgrid(xs, ys)
        hit = np.zeros(out.shape, dtype=bool)
        for x0, y0, x1, y1 in e:
            if y0 == y1:
                continue
            crosses = (y0 > Y) != (y1 > Y)
            xc = x0 + (Y - y0) / (y1 - y0) * (x1 - x0)
            hit ^= crosses & (X < xc)
        if grow > 0:
            for x0, y0, x1, y1 in e:
                dx, dy = x1 - x0, y1 - y0
                den = dx * dx + dy * dy
                t = 0.0 if den == 0 else np.clip(((X - x0) * dx + (Y - y0) * dy) / den, 0, 1)
                hit |= np.hypot(X - x0 - t * dx, Y - y0 - t * dy) <= grow
        return hit


# ----------------------------------------------------------------- Steiner tree

_STEPS = ((1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0))
_DIAG = ((1, 1), (1, -1), (-1, 1), (-1, -1))


def _steiner(cost: np.ndarray, terminals: List[np.ndarray], root: int):
    """Shortest-path-heuristic Steiner tree on the cell ``cost`` grid (inf: blocked)
    joining ``terminals`` (flat cell index arrays) from ``root``. A diagonal step keeps
    an orthogonal corner cell, so the tree is 4-connected (copper touching at a corner
    is not joined). Returns (tree path cells as a flat index array, reached terminal
    indices, tree length in cells, the longest root-to-terminal path in cells)."""
    ny, nx = cost.shape
    flat = cost.ravel()
    owner = {}
    for t, cells in enumerate(terminals):
        for c in cells.tolist():
            owner.setdefault(c, []).append(t)
    tree = set(int(c) for c in terminals[root].tolist() if math.isfinite(flat[c]))
    path_cells = set()
    reached = [root] if tree else []
    remaining = set(range(len(terminals))) - {root}

    def covered(cells):
        """Terminals the new tree cells already touch (overlapping discs) are reached."""
        for c in cells:
            for t in owner.get(c, ()):
                if t in remaining:
                    remaining.discard(t)
                    reached.append(t)

    rdist = {c: 0.0 for c in tree}  # distance from the root along the tree
    covered(tree)
    length = 0.0
    sq2 = math.sqrt(2.0)
    while remaining and tree:
        dist = {c: 0.0 for c in tree}
        prev = {}
        heap = [(0.0, c) for c in sorted(tree)]
        heapq.heapify(heap)
        found = None
        while heap:
            d, k = heapq.heappop(heap)
            if d > dist.get(k, math.inf):
                continue
            hit = [t for t in owner.get(k, ()) if t in remaining]
            if hit:
                found = (k, hit[0])
                break
            j, i = divmod(k, nx)
            ck = flat[k]
            for di, dj, step in _STEPS:
                ii, jj = i + di, j + dj
                if not (0 <= ii < nx and 0 <= jj < ny):
                    continue
                nb = jj * nx + ii
                cn = flat[nb]
                if not math.isfinite(cn):
                    continue
                nd = d + step * (ck + cn) / 2
                if nd < dist.get(nb, math.inf):
                    dist[nb] = nd
                    prev[nb] = k
                    heapq.heappush(heap, (nd, nb))
            for di, dj in _DIAG:
                ii, jj = i + di, j + dj
                if not (0 <= ii < nx and 0 <= jj < ny):
                    continue
                if not (math.isfinite(flat[j * nx + ii]) and math.isfinite(flat[jj * nx + i])):
                    continue
                nb = jj * nx + ii
                cn = flat[nb]
                if not math.isfinite(cn):
                    continue
                nd = d + sq2 * (ck + cn) / 2
                if nd < dist.get(nb, math.inf):
                    dist[nb] = nd
                    prev[nb] = k
                    heapq.heappush(heap, (nd, nb))
        if found is None:
            break
        k, t = found
        walk = []
        while k not in tree:
            walk.append(k)
            k = prev[k]
        walk.append(k)  # the attach cell, on the tree
        walk.reverse()
        new = []
        d = rdist.get(walk[0], 0.0)
        for a, b in zip(walk, walk[1:]):
            (ja, ia), (jb, ib) = divmod(a, nx), divmod(b, nx)
            if ia != ib and ja != jb:  # diagonal: keep the cheaper orthogonal corner
                c1, c2 = ja * nx + ib, jb * nx + ia
                corner = c1 if flat[c1] <= flat[c2] else c2
                if corner not in tree:
                    new.append(corner)
                    rdist.setdefault(corner, d)
            step = math.hypot(ia - ib, ja - jb)
            d += step
            length += step
            new.append(b)
            rdist[b] = d
        path_cells.update(new)
        tree.update(new)
        added = set(int(c) for c in terminals[t].tolist() if math.isfinite(flat[c])) - tree
        for c in added:
            rdist[c] = d
        tree |= added
        reached.append(t)
        remaining.discard(t)
        covered(set(new) | added)
    reach = max(
        (rdist.get(int(c), 0.0) for t in reached for c in terminals[t].tolist()), default=0.0
    )
    return np.array(sorted(path_cells), dtype=np.int64), reached, length, reach


# ----------------------------------------------------------------- polygons


def _loops(mask: np.ndarray) -> List[List[Tuple[int, int]]]:
    """Closed boundary loops of ``mask`` in cell-corner coordinates (region on the
    left: outer loops counter-clockwise, holes clockwise, y up)."""
    m = np.pad(mask, 1)
    inner = m[1:-1, 1:-1]
    out_edges = {}
    jj, ii = np.nonzero(inner & ~m[:-2, 1:-1])  # bottom
    for j, i in zip(jj.tolist(), ii.tolist()):
        out_edges.setdefault((i, j), []).append(((i + 1, j), (1, 0)))
    jj, ii = np.nonzero(inner & ~m[1:-1, 2:])  # right
    for j, i in zip(jj.tolist(), ii.tolist()):
        out_edges.setdefault((i + 1, j), []).append(((i + 1, j + 1), (0, 1)))
    jj, ii = np.nonzero(inner & ~m[2:, 1:-1])  # top
    for j, i in zip(jj.tolist(), ii.tolist()):
        out_edges.setdefault((i + 1, j + 1), []).append(((i, j + 1), (-1, 0)))
    jj, ii = np.nonzero(inner & ~m[1:-1, :-2])  # left
    for j, i in zip(jj.tolist(), ii.tolist()):
        out_edges.setdefault((i, j + 1), []).append(((i, j), (0, -1)))
    loops = []
    for start in sorted(out_edges):
        while out_edges.get(start):
            v = start
            end, d = out_edges[v].pop(0)
            loop = [v]
            v = end
            while v != start or loop[-1] == v:
                loop.append(v)
                options = out_edges.get(v) or []
                if not options:
                    break
                if len(options) > 1:  # a pinch: turn left (keeps 4-connected pieces apart)
                    left = (-d[1], d[0])
                    pick = next((k for k, o in enumerate(options) if o[1] == left), 0)
                else:
                    pick = 0
                end, d = options.pop(pick)
                v = end
            loops.append(_collinear(loop))
    return loops


def _collinear(loop):
    pts = loop[:-1] if len(loop) > 1 and loop[0] == loop[-1] else loop
    out = []
    n = len(pts)
    for k in range(n):
        a, b, c = pts[k - 1], pts[k], pts[(k + 1) % n]
        if (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0]) != 0:
            out.append(b)
    return out


def _area(loop) -> float:
    return 0.5 * sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(loop, loop[1:] + loop[:1]))


# ----------------------------------------------------------------- the partition


def _digest(payload) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def partition(
    entry: Dict,
    *,
    width: float,
    height: float,
    terminals: Dict[str, List[Terminal]],
    blocked: Sequence[Tuple[Tuple[float, float], float]],
    blocked_polygons: Sequence[Tuple[list, frozenset]] = (),
    currents: Optional[Dict[str, float]] = None,
    budgets_mohm: Optional[Dict[str, float]] = None,
    sources: Optional[Dict[str, str]] = None,
    copper_mm: float = 0.035,
    edge_mm: float = 0.3,
    via_drill_mm: float = 0.2,
    temperature_c: float = 25.0,
    fill_min_mm: float = 0.0,
) -> Partition:
    """Partition ``entry["layer"]`` (see the module doc). ``terminals``: net ->
    :class:`Terminal` list (in the nets' order of ``entry["nets"]``); ``blocked``:
    foreign copper discs ``(centre, radius incl. clearance)``; ``blocked_polygons``:
    ``(rings, allowed nets)`` keepouts barring pours on the layer; ``fill_min_mm``:
    the zones' minimum width (a tree passes only where a zone that wide fills)."""
    payload = dict(
        entry=entry,
        width=width,
        height=height,
        terminals={n: [t.__dict__ for t in ts] for n, ts in terminals.items()},
        blocked=[[list(c), r] for c, r in blocked],
        polygons=[[r, sorted(a)] for r, a in blocked_polygons],
        currents=currents,
        budgets=budgets_mohm,
        sources=sources,
        copper=copper_mm,
        edge=edge_mm,
        drill=via_drill_mm,
        t=temperature_c,
    )
    if fill_min_mm:
        payload["fill_min"] = fill_min_mm
    key = _digest(payload)
    if key in _CACHE:
        return _CACHE[key]
    result = _partition(
        entry,
        width,
        height,
        terminals,
        blocked,
        blocked_polygons,
        currents or {},
        budgets_mohm or {},
        sources or {},
        copper_mm,
        edge_mm,
        via_drill_mm,
        temperature_c,
        fill_min_mm,
    )
    result.report["inputs_sha256"] = key
    if len(_CACHE) > 8:
        _CACHE.clear()
    _CACHE[key] = result
    return result


def _partition(
    entry,
    width,
    height,
    terminals,
    blocked,
    blocked_polygons,
    currents,
    budgets,
    sources,
    copper_mm,
    edge_mm,
    via_drill_mm,
    temperature_c,
    fill_min_mm=0.0,
):
    from pnr.electrical import current_width
    from pnr.ir_drop import barrel_ohm, resistivity
    from pnr.stack import Region

    layer = entry["layer"]
    h = float(entry.get("h_mm", 0.1))
    gap = float(entry["split_gap_mm"])
    min_w = float(entry["min_width_mm"])
    g = _Grid(width, height, h)
    nets = [n for n in entry["nets"] if n in terminals]
    # 1. Free cells: inside the outline less the edge clearance, off foreign copper.
    free = g.zeros()
    e = int(math.ceil(edge_mm / h - 1e-9))
    free[e : g.ny - e, e : g.nx - e] = True
    hard = g.zeros()
    for c, r in blocked:
        hard |= g.disc(c, r)
    per_net_block = {n: g.zeros() for n in nets}
    for rings, allowed in blocked_polygons:
        cells = g.polygon(rings, grow=h / 2)
        for n in nets:
            if n not in allowed:
                per_net_block[n] |= cells
    free &= ~hard
    # 2. Terminals: via lands (exact, pre-claimed) and pad reach discs.
    label = np.full((g.ny, g.nx), -1, dtype=np.int64)  # claimed copper per rail
    cells_of = {}
    via_land = {}
    for k, n in enumerate(nets):
        via_land[n] = g.zeros()
        cells_of[n] = []
        for t in terminals[n]:
            disc = g.disc(t.at, t.radius)
            if t.kind == "via":
                via_land[n] |= disc
            cells_of[n].append(disc)
    for k, n in enumerate(nets):
        label[via_land[n] & (label < 0)] = k
    # 3. Order: current, terminal count, name (or as listed).
    if entry.get("order", "current") == "current":
        order = sorted(
            range(len(nets)),
            key=lambda k: (-(currents.get(nets[k]) or 0.0), -len(terminals[nets[k]]), nets[k]),
        )
    else:
        order = list(range(len(nets)))
    rho = resistivity(temperature_c)
    r_sq = rho / copper_mm
    report = dict(layer=layer, h_mm=h, split_gap_mm=gap, nets={})
    gap_cells = (gap + h) / h
    pad_discs = {}
    neck_mm = float(entry.get("neck_mm", 0.0) or 0.0)
    exempt = {}  # a rail's own terminal discs (plus neck_mm): the hard width's exception
    for n in nets:
        pad_discs[n] = g.zeros()
        own = g.zeros()
        for t, disc in zip(terminals[n], cells_of[n]):
            own |= disc
            if t.kind == "pad":
                pad_discs[n] |= disc
        exempt[n] = dilate(own, neck_mm / h) if neck_mm > 0 and own.any() else own
    wants = {}
    w_ipcs = {}
    for n in nets:
        current = currents.get(n) or 0.0
        w_ipcs[n] = (
            current_width(current, copper_mm / 0.035, 10.0, external=False) if current else 0.0
        )
        wants[n] = max(min_w, w_ipcs[n])
    ctx = dict(
        g=g,
        nets=nets,
        free=free,
        per_net_block=per_net_block,
        cells_of=cells_of,
        pad_discs=pad_discs,
        terminals=terminals,
        sources=sources,
        wants=wants,
        min_w=min_w,
        gap_cells=gap_cells,
        fill_min=fill_min_mm,
        exempt=exempt,
    )
    # 3a. Connect every rail at its minimum width first, in order; a rail left with
    # an unreached terminal is tried first in turn, and the order with the fewest
    # unreached terminals wins (the first such order on a tie).
    best = _connect(ctx, label, order)
    best_order = order
    tried = [order]
    for k in [k for k in order if best[2][nets[k]]]:
        variant = [k] + [m for m in order if m != k]
        if variant in tried:
            continue
        tried.append(variant)
        attempt = _connect(ctx, label, variant)
        if sum(map(len, attempt[2].values())) < sum(map(len, best[2].values())):
            best, best_order = attempt, variant
    label, spines, unreached, lengths, reached_of = best
    report["orders_tried"] = len(tried)
    report["connect_order"] = [nets[k] for k in best_order]
    # 3b. Widen each trunk to its width, the higher current first, where no other
    # rail's copper (with the gap) or pad disc is.
    widths = {}
    for k in order:
        n = nets[k]
        tree_mm = lengths[n][0] * h
        path_mm = lengths[n][1] * h  # the root to its farthest terminal
        budget = budgets.get(n)
        w_budget = 0.0
        if budget:
            share = max(0.25 * budget, budget - 2e3 * barrel_ohm(0.6, via_drill_mm, rho=rho))
            w_budget = r_sq * path_mm / (share * 1e-3)
        w = min(max(wants[n], w_budget), MAX_WIDTH_MM)
        widths[n] = w
        spine = spines[n]
        if spine.any() and w > min_w:
            others = (label >= 0) & (label != k)
            near = dilate(others, gap_cells - 1e-9) if others.any() else g.zeros()
            protect = g.zeros()
            for m, nm in enumerate(nets):
                if m != k:
                    protect |= pad_discs[nm]
            protect = dilate(protect, gap_cells - 1e-9) if protect.any() else protect
            room = free & ~per_net_block[n] & ~near & ~protect
            label[dilate(spine, w / h / 2) & room & (label < 0)] = k
        missing = unreached[n]
        report["nets"][n] = dict(
            current_a=currents.get(n) or 0.0,
            width_mm=round(w, 4),
            width_ipc_mm=round(w_ipcs[n], 4),
            width_budget_mm=round(w_budget, 4),
            budget_mohm=budget,
            tree_mm=round(tree_mm, 3),
            path_mm=round(path_mm, 3),
            terminals=len(terminals[n]),
            reached=reached_of[n]["count"],
            unreached=[
                dict(name=t.name, at=[round(t.at[0], 4), round(t.at[1], 4)]) for t in missing
            ],
        )
    trunks = spines
    claimed = label.copy()
    # The trunks' claimed copper at their widths (core_no_vias protects all of it).
    core_masks = {}
    for k, n in enumerate(nets):
        if spines[n].any():
            core_masks[n] = (claimed == k) & dilate(spines[n], widths[n] / h / 2)
    # 5. Growth: a breadth-first competition into the free cells, then carving.
    grown = _grow(label, free, per_net_block, nets, order)
    # A grown cell keeps gap + h (centre to centre) from another rail's claimed copper
    # and gap / 2 + h from another rail's grown copper (which carves the other half).
    carves = []
    for k, n in enumerate(nets):
        mine = grown == k
        other_claim = (claimed >= 0) & (claimed != k)
        other_grown = (grown >= 0) & (grown != k) & (claimed < 0)
        d_c = edt(other_claim, gap_cells + 1)
        d_g = edt(other_grown, gap_cells + 1)
        near = (d_c < gap_cells - 1e-9) | (d_g < gap / 2 / h + 1 - 1e-9)
        carves.append(mine & (claimed != k) & near)
    for carve in carves:
        grown[carve] = -1
    # 6. Polygons, connectivity and the narrowest width along each trunk.
    regions = []
    shapes = []
    occupied = grown >= 0
    for k, n in enumerate(nets):
        mine = grown == k
        lab, count = _label(mine)
        # The piece holding the trunk (or, without one, the most claimed copper); a
        # rail's other pieces (lands of terminals no tree reached) get no region.
        anchor = trunks[n] & mine
        if anchor.any():
            keep = sorted(set(lab[anchor].tolist()) - {-1})
        else:
            sizes = [int((claimed[lab == c] == k).sum()) for c in range(count)]
            keep = [int(np.argmax(sizes))] if sizes and max(sizes) else []
        mine = np.isin(lab, keep) & mine
        grown[(grown == k) & ~mine] = -1
        info = report["nets"][n]
        info["components"] = len(keep)
        info["area_mm2"] = round(float(mine.sum()) * h * h, 3)
        spine = trunks[n] & mine
        if spine.any():
            inside = edt(~mine, 4 * widths[n] / h + 4)
            info["core_min_mm"] = round(float(2 * inside[spine].min() * h - h), 4)
        # The widest way from each terminal to the root through the drawn copper.
        ways = _width_check(
            g, mine, n, terminals[n], cells_of[n], exempt[n], reached_of[n]["root"], min_w, h
        )
        narrowest = [row["width_mm"] for row in ways if row["width_mm"] is not None]
        if narrowest:
            info["way_min_mm"] = min(narrowest)
        # One raster cell of tolerance (a strip of 2m cells reads (2m - 1) h wide).
        necked = [
            row
            for row in ways
            if row["width_mm"] is not None and row["width_mm"] < min_w - h - 1e-9
        ]
        # Joined by the tree only at the fill width (the hard width found no way).
        info["joined_narrow"] = sorted(terminals[n][t].name for t in reached_of[n]["relaxed"])
        info["necked"] = necked
        info["reached_at_width"] = info["reached"] - len(necked)
        warnings = []
        if necked:
            worst = min(necked, key=lambda row: (row["width_mm"], row["name"]))
            warnings.append(
                "%s: %d terminal(s) (%s) reach the root only through %.2f mm of copper at "
                "(%.2f, %.2f), under min_width_mm %.2f"
                % (
                    n,
                    len(necked),
                    ", ".join(row["name"] for row in necked[:6]),
                    worst["width_mm"],
                    worst["neck_at"][0],
                    worst["neck_at"][1],
                    min_w,
                )
            )
        if w_ipcs[n] and narrowest and info["way_min_mm"] < w_ipcs[n] - h - 1e-9:
            info["ipc_neck"] = True
            warnings.append(
                "%s: a terminal's widest way to the root narrows to %.2f mm, under the %.2f mm "
                "IPC-2221 internal width for %.3g A"
                % (n, info["way_min_mm"], w_ipcs[n], currents.get(n) or 0.0)
            )
        info["status"] = "unreached" if info["unreached"] else ("necked" if necked else "ok")
        if warnings:
            info["warnings"] = warnings
        for c in keep:
            piece = lab == c
            piece = _fill_dead_holes(piece, grown, free, k)
            loops = _loops(piece)
            outer = [lp for lp in loops if _area(lp) > 0]
            holes = [lp for lp in loops if _area(lp) < 0]
            for lp in outer:
                shapes.append((abs(_area(lp)), n, lp, holes if len(outer) == 1 else []))
    if occupied.any():
        report["gap_min_mm"] = _gap_min(grown, len(nets), h)
    shapes.sort(key=lambda s: (-s[0], s[1]))
    base = 1 if entry.get("fill") else 0
    for rank, (_a, n, lp, holes) in enumerate(shapes):
        regions.append(
            Region(
                layer,
                n,
                base + rank,
                tuple((round(x * h, 6), round(y * h, 6)) for x, y in lp),
                False,
                tuple(tuple((round(x * h, 6), round(y * h, 6)) for x, y in hole) for hole in holes),
            )
        )
    if entry.get("fill"):
        regions.insert(0, Region(layer, entry["fill"], 0, None))
    cores = {}
    if entry.get("core_no_vias", True):
        # The trunk's claimed copper at its full width (not only the minimum width's
        # core), as cell centres: a via keeps half a cell plus its reach from them.
        for n in nets:
            mask = core_masks.get(n)
            if mask is None:
                continue
            jj, ii = np.nonzero(mask)
            cores[n] = (list(zip((ii + 0.5) * h, (jj + 0.5) * h)), h / 2)
    report["order"] = [nets[k] for k in order]
    return Partition(layer, regions, report, cores)


def _connect(ctx, label0, order):
    """Phase 1: every rail's Steiner tree, claimed at its minimum width (see
    :func:`_partition`). Returns (labels, spines, unreached terminals, tree lengths in
    cells, reached counts)."""
    g = ctx["g"]
    nets = ctx["nets"]
    gap_cells = ctx["gap_cells"]
    label = label0.copy()
    spines, unreached, lengths, reached_of = {}, {}, {}, {}
    pending = set(order)
    for k in order:
        n = nets[k]
        pending.discard(k)
        # The pad discs of rails still to come keep their landing ground (with the
        # gap): a trunk may cross one on its centre line, never widen there.
        later = g.zeros()
        for m in pending:
            later |= ctx["pad_discs"][nets[m]]
        later = dilate(later, gap_cells - 1e-9) if later.any() else later
        others = (label >= 0) & (label != k)
        near = dilate(others, gap_cells - 1e-9) if others.any() else g.zeros()
        allowed = ctx["free"] & ~ctx["per_net_block"][n] & ~near
        allowed |= label == k
        want = ctx["wants"][n]
        # Cost: 1 per cell, more where a trunk would be narrower than it should be, and
        # inside another rail's pad discs (its drop lands there).
        clear = edt(~allowed, want / g.h / 2 + 2)
        cost = 1.0 + 2.0 * np.clip((want / 2 - clear * g.h) / g.h, 0, None)
        for m, nm in enumerate(nets):
            if m != k:
                cost[ctx["pad_discs"][nm]] += 4.0

        def fits(w):
            # A zone of width w centred on the cell fits: half of it from the cell's
            # centre to the nearest barred cell's edge.
            return (clear - 0.5) * g.h >= w / 2 - 1e-9

        passable = allowed
        if ctx.get("fill_min"):
            # A zone fills no neck narrower than its minimum width: the tree passes
            # only where that width fits, or on this rail's own lands.
            passable = allowed & fits(ctx["fill_min"])
            passable |= label == k
        # min_width_mm is hard: outside the rail's own terminal discs (and neck_mm
        # round them) the tree passes only where a zone that wide fits, and not
        # through the landing ground of a rail still to come (it would not widen there).
        need = max(ctx["min_w"], ctx.get("fill_min") or 0.0)
        room = allowed & ~later
        wide = (edt(~room, need / g.h / 2 + 2) - 0.5) * g.h >= need / 2 - 1e-9
        strict = passable & (wide | ctx["exempt"][n]) | (label == k)
        flat_terms = [np.flatnonzero(disc & passable) for disc in ctx["cells_of"][n]]
        strict_terms = [np.flatnonzero(disc & strict) for disc in ctx["cells_of"][n]]
        root = _root(n, ctx["terminals"][n], flat_terms, ctx["sources"])
        if flat_terms:
            path, reached, length, reach = _steiner(
                np.where(strict, cost, np.inf), strict_terms, root
            )
        else:
            path, reached, length, reach = np.zeros(0, dtype=np.int64), [], 0.0, 0.0
        missing = [t for t in range(len(flat_terms)) if t not in set(reached)]
        relaxed = []
        if missing and reached and (passable & ~strict).any():
            # The terminals the hard width leaves out, joined where a zone of the
            # minimum fill width still fills: necked (reported, never silent).
            tree = set(path.tolist())
            for t in reached:
                tree.update(int(c) for c in strict_terms[t].tolist())
            more = [np.array(sorted(tree), dtype=np.int64)] + [flat_terms[t] for t in missing]
            path2, reached2, length2, _reach2 = _steiner(np.where(passable, cost, np.inf), more, 0)
            relaxed = [missing[t - 1] for t in reached2 if t > 0]
            if relaxed:
                path = np.union1d(path, path2)
                reached = list(reached) + relaxed
                length += length2
                reach = _tree_reach(g, path, flat_terms, root, reached)
        spine = g.zeros()
        spine.ravel()[path] = True
        half = ctx["min_w"] / g.h / 2
        trunk = (dilate(spine, half) & ~later | spine) & allowed if spine.any() else spine.copy()
        for t in reached:  # a via's land, a pad's reach disc (its drop's landing ground)
            trunk |= ctx["cells_of"][n][t] & allowed
        # Only copper joined to the tree: a disc that blocked copper cuts in two keeps
        # the part the tree reaches (a drop in the other part would land on an island).
        anchor = spine if spine.any() else _cells(g, flat_terms[root] if flat_terms else [])
        trunk = _joined(trunk | (label == k), anchor) & trunk
        label[trunk & (label < 0)] = k
        spines[n] = spine
        lengths[n] = (length, reach)
        reached_of[n] = dict(count=len(reached), relaxed=sorted(relaxed), root=root)
        unreached[n] = [
            ctx["terminals"][n][t] for t in range(len(ctx["terminals"][n])) if t not in set(reached)
        ]
    return label, spines, unreached, lengths, reached_of


def _tree_reach(g, path, flat_terms, root, reached):
    """The longest way (cells) from the root's cells along the tree (``path`` plus the
    reached terminals' cells, 8-neighbour steps) to a reached terminal's cell."""
    cells = set(path.tolist())
    for t in reached:
        cells.update(int(c) for c in flat_terms[t].tolist())
    nx = g.nx
    start = [int(c) for c in flat_terms[root].tolist() if int(c) in cells] if flat_terms else []
    dist = {c: 0.0 for c in start}
    heap = [(0.0, c) for c in start]
    heapq.heapify(heap)
    sq2 = math.sqrt(2.0)
    while heap:
        d, c = heapq.heappop(heap)
        if d > dist.get(c, math.inf):
            continue
        j, i = divmod(c, nx)
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                if not (di or dj):
                    continue
                nb = (j + dj) * nx + (i + di)
                if nb not in cells or not (0 <= i + di < nx):
                    continue
                nd = d + (sq2 if di and dj else 1.0)
                if nd < dist.get(nb, math.inf):
                    dist[nb] = nd
                    heapq.heappush(heap, (nd, nb))
    return max(
        (dist.get(int(c), 0.0) for t in reached for c in flat_terms[t].tolist()), default=0.0
    )


def _widest(mine, width, sources, nx):
    """Widest-path search over the 4-connected cells of ``mine``: for every cell, the
    largest (over paths from ``sources``) of the narrowest ``width`` along the path,
    the shortest such path on a tie, and the predecessor map to walk it back.
    Returns (best, prev) as flat arrays (best -1 off the reached copper)."""
    flat = mine.ravel()
    wf = width.ravel()
    ny = mine.shape[0]
    best = np.full(flat.size, -1.0)
    steps = np.full(flat.size, np.iinfo(np.int64).max, dtype=np.int64)
    prev = np.full(flat.size, -1, dtype=np.int64)
    heap = []
    for c in sources:
        c = int(c)
        if flat[c] and wf[c] > best[c]:
            best[c] = wf[c]
            steps[c] = 0
            heap.append((-wf[c], 0, c))
    heapq.heapify(heap)
    while heap:
        nb_, d, c = heapq.heappop(heap)
        b = -nb_
        if b < best[c] or (b == best[c] and d > steps[c]):
            continue
        j, i = divmod(c, nx)
        for nb, ok in ((c - 1, i > 0), (c + 1, i < nx - 1), (c - nx, j > 0), (c + nx, j < ny - 1)):
            if not ok or not flat[nb]:
                continue
            v = min(b, wf[nb])
            if v > best[nb] or (v == best[nb] and d + 1 < steps[nb]):
                best[nb] = v
                steps[nb] = d + 1
                prev[nb] = c
                heapq.heappush(heap, (-v, d + 1, nb))
    return best, prev


def _width_check(g, mine, n, terms, cells_of, exempt, root, min_w, h):
    """Per terminal of rail ``n``: the widest way through its drawn copper ``mine`` to
    the root's cells (the narrowest copper on it; own terminal discs exempt), and
    where that neck is. Returns ``[{name, width_mm, at, neck_at}]`` for every
    terminal joined to the root (inf: no copper narrower than the disc exemption)."""
    if not mine.any() or root is None:
        return []
    inside = edt(~mine, 4 * min_w / h + 4)
    width = np.where(mine, 2 * inside * h - h, 0.0)
    width = np.where(exempt & mine, np.inf, width)
    sources = np.flatnonzero(cells_of[root] & mine)
    if not len(sources):
        return []
    best, prev = _widest(mine, width, sources, g.nx)
    wf = width.ravel()
    out = []
    for t, term in enumerate(terms):
        if t == root:
            continue
        cells = np.flatnonzero(cells_of[t] & mine)
        if not len(cells):
            continue
        k = int(cells[np.argmax(best[cells])])
        b = float(best[k])
        if b < 0:
            continue  # not joined here (the connectivity check reports it)
        neck, c, steps = k, k, 0
        while c >= 0 and steps < mine.size:
            if wf[c] < wf[neck]:
                neck = c
            c = int(prev[c])
            steps += 1
        j, i = divmod(neck, g.nx)
        out.append(
            dict(
                name=term.name,
                at=[round(term.at[0], 4), round(term.at[1], 4)],
                width_mm=round(b, 4) if math.isfinite(b) else None,
                neck_at=[round((i + 0.5) * h, 4), round((j + 0.5) * h, 4)],
            )
        )
    return out


def _cells(g, flat):
    out = g.zeros()
    out.ravel()[np.asarray(flat, dtype=np.int64)] = True
    return out


def _joined(mask, anchor):
    """The 4-connected pieces of ``mask`` that hold a cell of ``anchor``."""
    if not anchor.any():
        return np.zeros_like(mask)
    lab, _count = _label(mask)
    keep = sorted(set(lab[anchor & mask].tolist()) - {-1})
    return np.isin(lab, keep) & mask


def _root(net, terms, flat_terms, sources):
    """The tree's root: the declared source's terminal, else the one nearest the
    centroid of the rail's terminals (that the raster can hold)."""
    usable = [k for k, cells in enumerate(flat_terms) if len(cells)]
    if not usable:
        return 0
    src = sources.get(net)
    if src:
        for k in usable:
            if terms[k].name == src:
                return k
    cx = sum(terms[k].at[0] for k in usable) / len(usable)
    cy = sum(terms[k].at[1] for k in usable) / len(usable)
    return min(usable, key=lambda k: (math.dist(terms[k].at, (cx, cy)), terms[k].name))


def _grow(label, free, per_net_block, nets, order):
    """Breadth-first competition of the claimed territories into the free cells
    (4-neighbour, rails in ``order`` within a ring)."""
    ny, nx = label.shape
    out = label.copy()
    flat = out.ravel()
    ok = {k: (free & ~per_net_block[nets[k]]).ravel() for k in range(len(nets))}
    q = deque()
    for k in order:
        for c in np.flatnonzero(flat == k).tolist():
            q.append(c)
    while q:
        c = q.popleft()
        k = flat[c]
        j, i = divmod(c, nx)
        for nb, good in (
            (c - 1, i > 0),
            (c + 1, i < nx - 1),
            (c - nx, j > 0),
            (c + nx, j < ny - 1),
        ):
            if good and flat[nb] < 0 and ok[k][nb]:
                flat[nb] = k
                q.append(nb)
    return out


def _fill_dead_holes(piece, grown, free, k):
    """``piece`` with the holes filled that hold only blocked copper (another net's
    antipad, a hole): KiCad's fill clears those itself. A hole holding another rail
    or free copper stays."""
    jj, ii = np.nonzero(piece)
    j0, j1 = max(int(jj.min()) - 1, 0), min(int(jj.max()) + 2, piece.shape[0])
    i0, i1 = max(int(ii.min()) - 1, 0), min(int(ii.max()) + 2, piece.shape[1])
    crop = piece[j0:j1, i0:i1]
    outside, count = _label(~crop)
    border = set(outside[0, :].tolist()) | set(outside[-1, :].tolist())
    border |= set(outside[:, 0].tolist()) | set(outside[:, -1].tolist())
    out = piece.copy()
    g = grown[j0:j1, i0:i1]
    f = free[j0:j1, i0:i1]
    for c in range(count):
        if c in border:
            continue
        hole = outside == c
        if ((g[hole] >= 0) & (g[hole] != k)).any() or (f[hole] & (g[hole] < 0)).any():
            continue
        out[j0:j1, i0:i1] |= hole
    return out


def _gap_min(grown, count, h):
    best = math.inf
    for k in range(count):
        mine = grown == k
        if not mine.any():
            continue
        other = (grown >= 0) & (grown != k)
        if not other.any():
            continue
        d = edt(other, 20)
        best = min(best, float(d[mine].min()) * h - h)
    return round(best, 4) if math.isfinite(best) else None


# --------------------------------------------------------- the router's inputs


def for_route(grid, graph, rules, stack, width, height, *, fixed_copper=None, fanouts=None):
    """Every declared partition of ``rules`` on this route: its inputs from the grid
    after the fanouts are planned (their vias are terminals or foreign copper), the
    partition, and its trunk cores as net keepouts on ``grid``. Returns the
    :class:`Partition` list."""
    from pnr.fanout.planner import fixed_items
    from pnr.fixed_block import keepout_polygon
    from pnr.place.geometry import pad_rects
    from pnr.power_spec import rail_current

    fab = dict(rules.get("fab") or {})
    clearance = float(fab.get("clearance_mm", grid.clearance))
    # Foreign copper keeps the larger class clearance of the pair (KiCad's fill
    # does), and the zones writeback draws fill no neck under the track width.
    classes = dict(getattr(grid, "net_clearances", None) or {})
    fill_min = float(fab.get("track_width_mm", grid.track_width))
    via_d = float(fab.get("via_diameter_mm", 2 * grid.via_radius))
    via_h = float(fab.get("via_drill_mm", via_d / 2))
    edge = float(fab.get("edge_clearance_mm", 0.3))
    out = []
    for entry in rules.get("plane_partition") or []:
        layer = entry["layer"]
        if layer not in stack.names:
            raise ValueError("plane_partition: %s is not a copper layer of the board" % layer)
        lay = stack.layer(layer)
        if lay.role != "plane":
            # Only a dedicated plane (typed power) takes drops into regions; a split or
            # signal layer keeps the legacy model. Reported, nothing partitioned.
            import sys

            sys.stderr.write(
                "pnr.plane_partition: warning: %s is typed %s, not power: not partitioned\n"
                % (layer, lay.kind)
            )
            continue
        nets = [n for n in entry["nets"] if n in lay.nets]
        skipped = [n for n in entry["nets"] if n not in lay.nets]
        rail_clear = max([clearance] + [classes.get(n, 0.0) for n in nets])

        def gap_to(net):
            return max(rail_clear, classes.get(net, 0.0))

        terms: Dict[str, List[Terminal]] = {n: [] for n in nets}
        blocked = []
        sizes = getattr(fanouts, "via_sizes", {}) if fanouts is not None else {}
        for net, p in grid.escape_vias:
            d = sizes.get((net, p[0], p[1]), (via_d, via_h))[0]
            if net in terms:
                terms[net].append(Terminal("via %.3f,%.3f" % p, "via", tuple(p), d / 2))
            else:
                blocked.append((tuple(p), d / 2 + gap_to(net)))
        _t, fixed_vias, polygons = fixed_items(fixed_copper) if fixed_copper else ((), [], [])
        for net, xy, d, _drill in fixed_vias:
            if net in terms:
                terms[net].append(
                    Terminal("fixed via %.3f,%.3f" % tuple(xy), "via", tuple(xy), d / 2)
                )
            else:
                blocked.append((tuple(xy), d / 2 + gap_to(net)))
        skip = getattr(fanouts, "skip_pads", set()) if fanouts is not None else set()
        reach = float(entry.get("terminal_reach_mm", 0.8))
        for comp in graph.components:
            for (name, net, r), pad in zip(pad_rects(comp), comp.pads):
                half = max(r.w, r.h) / 2
                if pad.through_hole:
                    if net in terms:
                        terms[net].append(
                            Terminal("%s.%s" % (comp.ref, name), "via", (r.cx, r.cy), half)
                        )
                    else:
                        blocked.append(((r.cx, r.cy), half + gap_to(net)))
                    continue
                if net in terms and (comp.ref, name) not in skip:
                    terms[net].append(
                        Terminal("%s.%s" % (comp.ref, name), "pad", (r.cx, r.cy), reach)
                    )
        for hole in rules.get("mounting_holes") or []:
            d = float(hole.get("clearance_diameter_mm", hole.get("drill_mm", 3.0)))
            blocked.append((tuple(hole["at"]), d / 2))
        keepouts = []
        for poly in polygons:  # fixed copper of other nets on the layer
            if poly.get("kind") in ("pad", "zone") and poly.get("layer") == layer:
                if poly.get("net") not in terms:
                    rings = [poly["outline"]] + list(poly.get("holes") or [])
                    keepouts.append(([[tuple(p) for p in ring] for ring in rings], frozenset()))
        for spec in rules.get("copper_keepouts") or []:
            layers = spec.get("layers")
            items = spec.get("items") or ("tracks", "vias", "pours")
            if layers and layer not in layers or "pours" not in items:
                continue
            if spec.get("polygon") is not None or spec.get("rect_mm") is not None:
                poly = keepout_polygon(graph, spec)
            elif spec.get("rect") is not None:
                x0, y0, x1, y1 = spec["rect"]
                poly = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
            else:
                continue
            keepouts.append(([poly], frozenset(spec.get("allowed_nets") or ())))
        currents = {n: rail_current(rules, n, (entry.get("currents") or {}).get(n)) for n in nets}
        budgets = dict(entry.get("budgets_mohm") or {})
        for ir in rules.get("ir_drop") or []:
            n = ir["net"]
            if n in terms and n not in budgets:
                amps = currents.get(n) or ir.get("current_a")
                if ir.get("budget_mohm"):
                    budgets[n] = ir["budget_mohm"]
                elif ir.get("budget_mv") and amps:
                    budgets[n] = ir["budget_mv"] / amps
        sources = {}
        for n, text in (entry.get("sources") or {}).items():
            sources[n] = text.replace(":", ".", 1)
        part = partition(
            entry,
            width=width,
            height=height,
            terminals=terms,
            blocked=blocked,
            blocked_polygons=keepouts,
            currents={n: c for n, c in currents.items() if c},
            budgets_mohm=budgets,
            sources=sources,
            copper_mm=lay.copper_mm or 0.035,
            edge_mm=edge,
            via_drill_mm=via_h,
            fill_min_mm=fill_min,
        )
        if skipped:
            part.report["not_plane_nets"] = skipped
        if entry.get("core_no_vias", True):
            # Other nets' surface pads keep their drop sites (within the terminal reach):
            # a maze via may not cut a trunk, a pad's own drop may land beside it.
            spare = [
                (r.cx, r.cy)
                for comp in graph.components
                for (_name, net, r), pad in zip(pad_rects(comp), comp.pads)
                if net and net not in terms and not pad.through_hole
            ]
            extra = ()
            if entry.get("protect_fanouts") and fanouts is not None:
                # The fanouts' planned access cells keep a via site: a ball's tail may
                # need its via right there (the trunk's copper keeps its gap anyway).
                extra = [
                    (grid.center_of(i, j), grid.via_radius + clearance + grid.pitch)
                    for (_la, i, j) in sorted(fanouts.protected)
                ]
            _core_keepouts(grid, part, grid.via_radius + clearance, spare, reach, extra)
        out.append(part)
    return out


def _core_keepouts(grid, part, via_reach, spare=(), spare_reach=0.0, spare_sites=()):
    """Other nets' vias stay ``via_reach`` (via radius + clearance) beyond each trunk's
    claimed copper (the router's net keepouts, vias only), except within
    ``spare_reach`` of a ``spare`` point (another net's pad: its drop site) and within
    each ``(point, reach)`` of ``spare_sites`` (a fanout's access cell)."""
    if not part.cores:
        return
    xs = (np.arange(grid.nx) + 0.5) * grid.pitch
    ys = (np.arange(grid.ny) + 0.5) * grid.pitch
    h = part.report["h_mm"]
    g = _Grid(grid.width, grid.height, h)
    keep = g.zeros()
    for p in spare:
        keep |= g.disc(p, spare_reach)
    for p, r in spare_sites:
        keep |= g.disc(p, r)
    for net, (points, half) in sorted(part.cores.items()):
        if not points:
            continue
        spine = g.zeros()
        for x, y in points:
            i, j = g.cell((x, y))
            spine[j, i] = True
        near = edt(spine, (half + via_reach) / h + 1) * h <= half + via_reach
        near &= ~keep
        ii = np.minimum((xs / h).astype(int), g.nx - 1)
        jj = np.minimum((ys / h).astype(int), g.ny - 1)
        cells = near[np.ix_(jj, ii)]
        mask = np.broadcast_to(cells, (grid.nlayers,) + cells.shape).copy()
        grid.add_net_keepout(None, mask, {net})
