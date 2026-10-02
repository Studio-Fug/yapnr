"""Rasters of polygons: point sampling, connected components, the Euclidean distance transform.

All in numpy (no scipy): `rasterize` samples polygons on a tensor grid of points with the
even-odd rule (half-open in y, so zero-width keyhole cuts, which are horizontal, never count);
`label` finds 4-connected components by union-find over row runs; `edt` is the exact squared
Euclidean distance transform, computed separably (rows, then columns) as the lower envelope of
parabolas min_k (f_k + (i − k)²), which is what the Felzenszwalb–Huttenlocher algorithm
evaluates in linear time; here it is evaluated in vectorized blocks.
"""

from __future__ import annotations

import numpy as np


def rasterize(polygons, xs, ys) -> np.ndarray:
    """Boolean (len(xs), len(ys)): True where the point (xs[i], ys[j]) is inside any polygon."""
    xs = np.asarray(xs, dtype=np.float64)
    ys = np.asarray(ys, dtype=np.float64)
    out = np.zeros((xs.size, ys.size), dtype=bool)
    for poly in polygons:
        p = np.asarray(poly, dtype=np.float64)
        inside = np.zeros_like(out)
        x1, y1 = p[:, 0], p[:, 1]
        x2, y2 = np.roll(x1, -1), np.roll(y1, -1)
        for a, b, c, d in zip(x1, y1, x2, y2):
            if b == d:
                continue
            rows = np.nonzero((b > ys) != (d > ys))[0]
            if rows.size == 0:
                continue
            xi = a + (ys[rows] - b) * (c - a) / (d - b)
            inside[:, rows] ^= xs[:, None] < xi[None, :]
        out |= inside
    return out


def label(mask: np.ndarray) -> tuple[np.ndarray, int]:
    """4-connected component labels (0 = background, 1..n) and n."""
    m = np.asarray(mask, dtype=bool)
    ni, nj = m.shape
    parent: list[int] = []

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    runs = []  # (row, start, stop, id)
    prev: list[tuple[int, int, int]] = []
    for i in range(ni):
        row = m[i]
        d = np.diff(np.concatenate([[0], row.view(np.int8), [0]]))
        starts = np.nonzero(d == 1)[0]
        stops = np.nonzero(d == -1)[0]
        cur = []
        for s, e in zip(starts, stops):
            rid = len(parent)
            parent.append(rid)
            for ps, pe, pid in prev:
                if ps < e and s < pe:
                    ra, rb = find(rid), find(pid)
                    if ra != rb:
                        parent[max(ra, rb)] = min(ra, rb)
            cur.append((int(s), int(e), rid))
            runs.append((i, int(s), int(e), rid))
        prev = cur
    out = np.zeros((ni, nj), dtype=np.int64)
    roots: dict[int, int] = {}
    for i, s, e, rid in runs:
        r = find(rid)
        if r not in roots:
            roots[r] = len(roots) + 1
        out[i, s:e] = roots[r]
    return out, len(roots)


def _edt_1d(f: np.ndarray) -> np.ndarray:
    """Row-wise min_k (f[:, k] + (i − k)²) for a (rows, n) array."""
    n = f.shape[1]
    block = max(1, 2_000_000 // max(1, n * n))
    idx = np.arange(n, dtype=np.float64)
    out = np.empty_like(f)
    sq = (idx[:, None] - idx[None, :]) ** 2  # (i, k)
    for r0 in range(0, f.shape[0], block):
        fr = f[r0 : r0 + block]
        out[r0 : r0 + block] = np.min(fr[:, None, :] + sq[None, :, :], axis=2)
    return out


def edt(mask: np.ndarray) -> np.ndarray:
    """Euclidean distance (in samples) from every sample to the nearest True sample of `mask`
    (inf everywhere when `mask` is empty)."""
    m = np.asarray(mask, dtype=bool)
    if not m.any():
        return np.full(m.shape, np.inf)
    big = float(m.shape[0] ** 2 + m.shape[1] ** 2 + 1)
    f = np.where(m, 0.0, big)
    f = _edt_1d(f)  # along axis 1
    f = _edt_1d(f.T).T  # along axis 0
    return np.sqrt(f)
