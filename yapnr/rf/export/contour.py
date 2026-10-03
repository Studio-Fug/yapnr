"""Binary pixels → copper polygons (design §10.1).

The polygons follow the pixel boundaries exactly: the copper is the union of its closed pixels,
the copper the solver simulates (its sheet conductance sits on the edges of the copper pixels,
boundary edges included, `materials`), on the optimization grid and, subdivided, on every finer
grid. Each boundary edge between a copper and a void pixel is directed with the copper on its
left, so outer boundaries run counter-clockwise and holes clockwise; each hole belongs to the
smallest outer loop around it.

Saddles (two diagonal copper pixels, [[1, 0], [0, 1]]) are resolved as connected copper, as in
the solver, where the four edges at the shared node all conduct. A polygon cannot touch itself
at a point, so the connection is a bridge of the two void pixels' corners cut by
`SADDLE_CUT` (a quarter pixel) along the diagonal: no sample point of a grid up to three
times finer (sub-pixel centres at 1/6 of a pixel and more from the node) falls in the cut, so
the raster of the polygons at those grids is the subdivided pixels. The width and space repair
(`repair`) leaves no saddles in an exported design.

Collinear points merge; an optional Douglas–Peucker simplification (off by default, since
every vertex of a pixel boundary carries a pixel) falls back to the unsimplified loops when the
simplified polygons no longer reproduce the pixels (pixel-centre test). Each hole is joined to
its outer boundary by a zero-width keyhole cut along +x from its rightmost vertex, because
footprint polygons have no holes (KiCad fractures zones for Gerber the same way).

An earlier version traced the level-½ contour of the pixel centres (marching squares), which
cuts every convex pixel corner and fills every concave one by half a pixel: the same pixels on
the optimization grid, but other copper at a half and a third of the pitch, where the
re-validation resonated 1.2–1.7 % higher than the optimizer's copper (design §24).

Coordinates are in pixel units with pixel (i, j) the square [i, i+1] × [j, j+1]; i runs along
x, j along y (y up).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# The corners of the void pixels at a saddle are cut by this much (pixels) along the diagonal.
SADDLE_CUT = 0.25

# Unit steps of the four edge directions.
_DIRS = {(1, 0), (0, 1), (-1, 0), (0, -1)}


def _boundary_edges(m: np.ndarray) -> dict:
    """Directed boundary edges of the copper (copper on the left): start node → [direction]."""
    p = np.zeros((m.shape[0] + 2, m.shape[1] + 2), dtype=bool)
    p[1:-1, 1:-1] = m
    out: dict[tuple[int, int], list[tuple[int, int]]] = {}

    def add(nodes, d):
        for a, b in nodes:
            out.setdefault((int(a), int(b)), []).append(d)

    c = p[1:-1, 1:-1]
    ii, jj = np.nonzero(c & ~p[1:-1, :-2])  # void below: the bottom edge, +x
    add(zip(ii, jj), (1, 0))
    ii, jj = np.nonzero(c & ~p[2:, 1:-1])  # void to the right: the right edge, +y
    add(zip(ii + 1, jj), (0, 1))
    ii, jj = np.nonzero(c & ~p[1:-1, 2:])  # void above: the top edge, −x
    add(zip(ii + 1, jj + 1), (-1, 0))
    ii, jj = np.nonzero(c & ~p[:-2, 1:-1])  # void to the left: the left edge, −y
    add(zip(ii, jj + 1), (0, -1))
    return out


def trace_loops(mask: np.ndarray) -> list[np.ndarray]:
    """Closed loops (k, 2) in pixel units along the pixel boundaries of the copper of a binary
    mask (ni, nj), copper on the left; saddles connect the copper (module doc)."""
    m = np.asarray(mask, dtype=bool)
    edges = _boundary_edges(m)
    used: set = set()
    loops = []
    for start in sorted(edges):
        for d0 in sorted(edges[start]):
            if (start, d0) in used:
                continue
            pts: list[tuple[float, float]] = []
            node, d = start, d0
            while (node, d) not in used:
                used.add((node, d))
                nxt = (node[0] + d[0], node[1] + d[1])
                outs = edges[nxt]
                if len(outs) == 1:
                    dn = outs[0]
                    pts.append((float(nxt[0]), float(nxt[1])))
                else:
                    # A saddle: turn right (the copper on the left stays connected) and cut the
                    # void pixel's corner between the two edges.
                    dn = (d[1], -d[0])
                    if dn not in outs:
                        raise ValueError(f"inconsistent boundary at node {nxt}")
                    c = SADDLE_CUT
                    pts.append((nxt[0] - c * d[0], nxt[1] - c * d[1]))
                    pts.append((nxt[0] + c * dn[0], nxt[1] + c * dn[1]))
                node, d = nxt, dn
            loops.append(np.array(pts, dtype=np.float64))
    return loops


def signed_area(loop: np.ndarray) -> float:
    x, y = loop[:, 0], loop[:, 1]
    return 0.5 * float(np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y))


def point_in_loop(pt, loop: np.ndarray) -> bool:
    """Even-odd test (half-open rule: horizontal edges never count)."""
    x, y = pt
    x1, y1 = loop[:, 0], loop[:, 1]
    x2, y2 = np.roll(x1, -1), np.roll(y1, -1)
    cross = (y1 > y) != (y2 > y)
    with np.errstate(divide="ignore", invalid="ignore"):
        xi = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
    return bool(np.count_nonzero(cross & (x < xi)) % 2)


def merge_collinear(loop: np.ndarray, tol: float = 1e-9) -> np.ndarray:
    """Drop vertices on the straight line between their neighbours (repeated until none)."""
    pts = loop
    while pts.shape[0] > 3:
        prev = np.roll(pts, 1, axis=0)
        nxt = np.roll(pts, -1, axis=0)
        cross = (pts[:, 0] - prev[:, 0]) * (nxt[:, 1] - prev[:, 1]) - (pts[:, 1] - prev[:, 1]) * (
            nxt[:, 0] - prev[:, 0]
        )
        keep = np.abs(cross) > tol
        if keep.all():
            break
        # Remove every other removable vertex at a time so neighbours stay valid.
        drop = np.nonzero(~keep)[0][::2]
        pts = np.delete(pts, drop, axis=0)
    return pts


def _dp(pts: np.ndarray, tol: float) -> list[int]:
    """Douglas–Peucker on an open polyline: indices kept (first and last included)."""
    keep = [0, pts.shape[0] - 1]
    stack = [(0, pts.shape[0] - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        a, b = pts[i], pts[j]
        d = b - a
        seg = pts[i + 1 : j] - a
        norm = float(np.hypot(*d))
        if norm == 0.0:
            dist = np.hypot(seg[:, 0], seg[:, 1])
        else:
            dist = np.abs(seg[:, 0] * d[1] - seg[:, 1] * d[0]) / norm
        k = int(np.argmax(dist))
        if dist[k] > tol:
            mid = i + 1 + k
            keep.append(mid)
            stack.append((i, mid))
            stack.append((mid, j))
    return sorted(set(keep))


def simplify_loop(loop: np.ndarray, tol: float) -> np.ndarray:
    """Douglas–Peucker on a closed loop, anchored at vertex 0 and the vertex farthest from it."""
    if loop.shape[0] <= 4 or tol <= 0:
        return loop
    far = int(np.argmax(np.hypot(*(loop - loop[0]).T)))
    a = _dp(loop[: far + 1], tol)
    b = _dp(np.vstack([loop[far:], loop[:1]]), tol)
    idx = a + [far + k for k in b[1:-1]]
    out = loop[idx]
    return out if out.shape[0] >= 3 else loop


@dataclass
class Island:
    """One copper island: its outer loop (CCW), holes (CW) and the keyholed polygon."""

    outer: np.ndarray
    holes: list
    polygon: np.ndarray

    @property
    def area(self) -> float:
        return signed_area(self.outer) + sum(signed_area(h) for h in self.holes)


def nest(loops: list[np.ndarray]) -> list[tuple[np.ndarray, list[np.ndarray]]]:
    """Group loops into (outer, holes): holes (negative area) go to the smallest outer loop
    around them."""
    outers = [lp for lp in loops if signed_area(lp) > 0]
    holes = [lp for lp in loops if signed_area(lp) < 0]
    groups: list[tuple[np.ndarray, list]] = [(o, []) for o in outers]
    areas = [signed_area(o) for o in outers]
    for h in holes:
        probe = 0.5 * (h[0] + h[1])
        best = None
        for k, o in enumerate(outers):
            if point_in_loop(probe, o) and (best is None or areas[k] < areas[best]):
                best = k
        if best is None:
            raise ValueError("a hole without an outer boundary")
        groups[best][1].append(h)
    return groups


def _wedge_contains(prev_pt, v, next_pt, d) -> bool:
    """True if direction d from v points into the interior wedge (left of prev→v→next)."""
    u = next_pt - v
    w = prev_pt - v
    au = np.arctan2(u[1], u[0])
    aw = (np.arctan2(w[1], w[0]) - au) % (2 * np.pi)
    ad = (np.arctan2(d[1], d[0]) - au) % (2 * np.pi)
    return 0.0 < ad < aw


def keyhole(outer: np.ndarray, holes: list[np.ndarray]) -> np.ndarray:
    """One hole-free polygon: each hole joined to the boundary by a zero-width cut along +x
    from its rightmost vertex to the nearest boundary crossing (holes in order of decreasing
    rightmost x, so a cut never crosses an unprocessed hole)."""
    poly = [tuple(p) for p in outer]
    order = sorted(holes, key=lambda h: -float(h[:, 0].max()))
    for h in order:
        k0 = int(np.lexsort((-h[:, 1], -h[:, 0]))[0])  # rightmost, then topmost
        hole = np.roll(h, -k0, axis=0)
        m = hole[0]
        best = None
        n = len(poly)
        for k in range(n):
            p, q = np.array(poly[k]), np.array(poly[(k + 1) % n])
            if p[1] == q[1]:
                continue
            if not (min(p[1], q[1]) <= m[1] <= max(p[1], q[1])):
                continue
            t = (m[1] - p[1]) / (q[1] - p[1])
            xi = p[0] + t * (q[0] - p[0])
            if xi <= m[0]:
                continue
            if best is None or xi < best[0]:
                best = (xi, k, t)
        if best is None:
            raise ValueError("no boundary to the right of a hole")
        xi, k, t = best
        hole_seq = [tuple(p) for p in hole] + [tuple(m)]
        if t <= 1e-12 or t >= 1 - 1e-12:
            v = np.array([xi, m[1]])
            d = m - v
            occ = [i for i, p in enumerate(poly) if p[0] == v[0] and p[1] == v[1]]
            pick = occ[0]
            for i in occ:
                if _wedge_contains(np.array(poly[i - 1]), v, np.array(poly[(i + 1) % n]), d):
                    pick = i
                    break
            vt = poly[pick]
            poly = poly[: pick + 1] + hole_seq + [vt] + poly[pick + 1 :]
        else:
            ip = (float(xi), float(m[1]))
            poly = poly[: k + 1] + [ip] + hole_seq + [ip] + poly[k + 1 :]
    return np.array(poly, dtype=np.float64)


def islands(mask: np.ndarray, *, simplify_tol: float = 0.0) -> list[Island]:
    """Copper islands of a binary mask as keyholed polygons (pixel units), largest first;
    `simplify_tol` > 0 simplifies the loops (Douglas–Peucker, kept only if the polygons still
    reproduce the pixels)."""
    raw = trace_loops(mask)
    loops = [merge_collinear(lp) for lp in raw]
    simple = [simplify_loop(lp, simplify_tol) for lp in loops]
    groups = nest(simple)
    if simplify_tol > 0 and not _reproduces(mask, [keyhole(o, hs) for o, hs in groups]):
        groups = nest(loops)
    out = [Island(o, hs, keyhole(o, hs)) for o, hs in groups]
    out.sort(
        key=lambda isl: (-isl.area, float(isl.outer[:, 0].min()), float(isl.outer[:, 1].min()))
    )
    return out


def _reproduces(mask: np.ndarray, polygons: list[np.ndarray]) -> bool:
    from yapnr.rf.export.raster import rasterize

    ni, nj = mask.shape
    got = rasterize(polygons, np.arange(ni) + 0.5, np.arange(nj) + 0.5)
    return bool(np.array_equal(got, np.asarray(mask, bool)))
