"""Binary pixels → copper polygons (design §10.1).

Marching squares on the pixel-centre samples of the binary field at level ½: on its bilinear
interpolant the contour crosses each segment between a copper and a void centre at its
midpoint, which is on the pixel boundary (the node line the solver used), and cuts pixel
corners by half a pixel. Saddles (two diagonal copper pixels) are resolved as connected copper,
as in the solver, where the four edges at the shared node all conduct (`materials`).

Loops are directed with the copper on the left, so outer boundaries run counter-clockwise and
holes clockwise; each hole belongs to the smallest outer loop around it. Collinear points merge,
Douglas–Peucker simplification (tolerance Δ/8 by default) follows, and each hole is joined to
its outer boundary by a zero-width keyhole cut along +x from its rightmost vertex, because
footprint polygons have no holes (KiCad fractures zones for Gerber the same way). A polygon
from simplification that no longer reproduces the pixels (pixel-centre test) falls back to the
unsimplified loops.

Coordinates are in pixel units with pixel (i, j) the square [i, i+1] × [j, j+1]; i runs along
x, j along y (y up).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# Segments per case (corner bits bl=1, br=2, tr=4, tl=8), copper on the left. Edge midpoints
# of the marching cell between samples (a, b) .. (a+1, b+1), in half-pixel units:
# B = (2a, 2b−1), R = (2a+1, 2b), T = (2a, 2b+1), L = (2a−1, 2b).
_CASES = {
    1: [("B", "L")],
    2: [("R", "B")],
    3: [("R", "L")],
    4: [("T", "R")],
    5: [("B", "R"), ("T", "L")],
    6: [("T", "B")],
    7: [("T", "L")],
    8: [("L", "T")],
    9: [("B", "T")],
    10: [("L", "B"), ("R", "T")],
    11: [("R", "T")],
    12: [("L", "R")],
    13: [("B", "R")],
    14: [("L", "B")],
}


def _point(edge: str, a: int, b: int) -> tuple[int, int]:
    return {
        "B": (2 * a, 2 * b - 1),
        "R": (2 * a + 1, 2 * b),
        "T": (2 * a, 2 * b + 1),
        "L": (2 * a - 1, 2 * b),
    }[edge]


def trace_loops(mask: np.ndarray) -> list[np.ndarray]:
    """Closed loops (k, 2) in pixel units around the copper of a binary mask (ni, nj)."""
    m = np.asarray(mask, dtype=bool)
    p = np.zeros((m.shape[0] + 2, m.shape[1] + 2), dtype=np.int64)
    p[1:-1, 1:-1] = m
    # Cell (a, b) has corners at padded samples (a, b), (a+1, b), (a+1, b+1), (a, b+1); padded
    # sample (a, b) is pixel (a−1, b−1) with centre (a − ½, b − ½).
    code = p[:-1, :-1] + 2 * p[1:, :-1] + 4 * p[1:, 1:] + 8 * p[:-1, 1:]
    nxt: dict[tuple[int, int], tuple[int, int]] = {}
    for a, b in zip(*np.nonzero((code > 0) & (code < 15))):
        for e0, e1 in _CASES[int(code[a, b])]:
            nxt[_point(e0, int(a), int(b))] = _point(e1, int(a), int(b))
    loops = []
    seen: set = set()
    for start in sorted(nxt):
        if start in seen:
            continue
        pts = []
        q = start
        while q not in seen:
            seen.add(q)
            pts.append(q)
            q = nxt[q]
        loops.append(np.array(pts, dtype=np.float64) / 2.0)
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


def islands(mask: np.ndarray, *, simplify_tol: float = 0.125) -> list[Island]:
    """Copper islands of a binary mask as keyholed polygons (pixel units), largest first."""
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
