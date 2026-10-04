"""Standard-library raster for the copper checks: one Python int per pixel row (a bitset).

Used by the generator's checks (G2 congruence, G3 stitch reach) and by the board audit, which runs
under KiCad's Python 3.9, so this module stays 3.9-compatible and imports nothing outside the
standard library. Pixel centres sit at x0 + (i + phase)·h; the off-centre phase keeps exact
geometry edges (multiples of the pitch) off the pixel centres, so congruent copies rasterize to the
same pixels.
"""

from __future__ import annotations

import math
from collections import deque
from typing import Iterable, List, Sequence, Tuple

Pt = Tuple[float, float]
Mask = List[int]


def popcount(x: int) -> int:
    return bin(x).count("1")


class Grid:
    def __init__(self, x0: float, y0: float, x1: float, y1: float, h: float = 0.01, phase=0.3711):
        self.h = h
        self.x0, self.y0 = x0, y0
        self.ph = phase
        self.nx = max(1, int(math.ceil((x1 - x0) / h)))
        self.ny = max(1, int(math.ceil((y1 - y0) / h)))
        self.full_row = (1 << self.nx) - 1

    # pixel <-> coordinates
    def xc(self, i: int) -> float:
        return self.x0 + (i + self.ph) * self.h

    def yc(self, j: int) -> float:
        return self.y0 + (j + self.ph) * self.h

    def cols(self, xa: float, xb: float) -> Tuple[int, int]:
        i0 = int(math.ceil((xa - self.x0) / self.h - self.ph))
        i1 = int(math.floor((xb - self.x0) / self.h - self.ph))
        return max(0, i0), min(self.nx - 1, i1)

    def rows(self, ya: float, yb: float) -> Tuple[int, int]:
        j0 = int(math.ceil((ya - self.y0) / self.h - self.ph))
        j1 = int(math.floor((yb - self.y0) / self.h - self.ph))
        return max(0, j0), min(self.ny - 1, j1)

    def empty(self) -> Mask:
        return [0] * self.ny

    def full(self) -> Mask:
        return [self.full_row] * self.ny

    @staticmethod
    def span(i0: int, i1: int) -> int:
        return ((1 << (i1 - i0 + 1)) - 1) << i0 if i1 >= i0 else 0

    # drawing (OR into `m`)
    def rect(self, m: Mask, x0: float, y0: float, x1: float, y1: float) -> None:
        i0, i1 = self.cols(x0, x1)
        if i1 < i0:
            return
        s = self.span(i0, i1)
        j0, j1 = self.rows(y0, y1)
        for j in range(j0, j1 + 1):
            m[j] |= s

    def disks(self, m: Mask, centres: Iterable[Pt], r: float) -> None:
        r2 = r * r
        for cx, cy in centres:
            j0, j1 = self.rows(cy - r, cy + r)
            for j in range(j0, j1 + 1):
                dy = self.yc(j) - cy
                hw2 = r2 - dy * dy
                if hw2 < 0:
                    continue
                hw = math.sqrt(hw2)
                i0, i1 = self.cols(cx - hw, cx + hw)
                if i1 >= i0:
                    m[j] |= self.span(i0, i1)

    def points(self, m: Mask, pts: Iterable[Pt]) -> None:
        """Set the pixel nearest each point (via centres as reach seeds)."""
        for x, y in pts:
            i = int(round((x - self.x0) / self.h - self.ph))
            j = int(round((y - self.y0) / self.h - self.ph))
            if 0 <= i < self.nx and 0 <= j < self.ny:
                m[j] |= 1 << i

    def capsules(self, m: Mask, pts: Sequence[Pt], r: float, step: float = 0.01) -> None:
        """Union of disks of radius `r` along a polyline, resampled to `step` (scallop below
        r - sqrt(r^2 - step^2/4), 0.13 um for r 0.1 mm and 10 um steps)."""
        out: List[Pt] = []
        for a, b in zip(pts, pts[1:]):
            n = max(1, int(math.ceil(math.dist(a, b) / step)))
            for k in range(n):
                t = k / n
                out.append((a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])))
        if pts:
            out.append(pts[-1])
        self.disks(m, out, r)

    def polygon(self, m: Mask, poly: Sequence[Pt]) -> None:
        """Non-zero winding scanline fill."""
        if len(poly) < 3:
            return
        ys = [q[1] for q in poly]
        j0, j1 = self.rows(min(ys), max(ys))
        edges = []
        n = len(poly)
        for k in range(n):
            a, b = poly[k], poly[(k + 1) % n]
            if a[1] == b[1]:
                continue
            edges.append((a, b, 1 if b[1] > a[1] else -1))
        for j in range(j0, j1 + 1):
            y = self.yc(j)
            xs = []
            for a, b, w in edges:
                lo, hi = (a[1], b[1]) if w > 0 else (b[1], a[1])
                if lo <= y < hi:
                    xs.append((a[0] + (y - a[1]) * (b[0] - a[0]) / (b[1] - a[1]), w))
            if not xs:
                continue
            xs.sort()
            wind = 0
            row = 0
            for k, (x, w) in enumerate(xs):
                prev = wind
                wind += w
                if prev == 0 and wind != 0:
                    start = x
                elif prev != 0 and wind == 0:
                    i0, i1 = self.cols(start, x)
                    row |= self.span(i0, i1)
            m[j] |= row

    # set operations
    @staticmethod
    def and_(a: Mask, b: Mask) -> Mask:
        return [x & y for x, y in zip(a, b)]

    @staticmethod
    def or_(a: Mask, b: Mask) -> Mask:
        return [x | y for x, y in zip(a, b)]

    @staticmethod
    def andnot(a: Mask, b: Mask) -> Mask:
        return [x & ~y for x, y in zip(a, b)]

    @staticmethod
    def xor(a: Mask, b: Mask) -> Mask:
        return [x ^ y for x, y in zip(a, b)]

    @staticmethod
    def count(a: Mask) -> int:
        return sum(popcount(x) for x in a)

    def area(self, a: Mask) -> float:
        return self.count(a) * self.h * self.h

    # morphology
    def dilate(self, a: Mask, r_px: float) -> Mask:
        """Dilation by a disk of radius `r_px` pixels (may be fractional)."""
        n = int(math.floor(r_px))
        widths = {
            dy: int(math.floor(math.sqrt(r_px * r_px - dy * dy) + 1e-9)) for dy in range(-n, n + 1)
        }
        out = [0] * self.ny
        for j, row in enumerate(a):
            if not row:
                continue
            cache = {}
            for dy, w in widths.items():
                jj = j + dy
                if jj < 0 or jj >= self.ny:
                    continue
                s = cache.get(w)
                if s is None:
                    s = _smear(row, w)
                    cache[w] = s
                out[jj] |= s
        fr = self.full_row
        return [x & fr for x in out]

    def geodesic_reach(self, seeds: Mask, domain: Mask, r: float, steps: int = 5) -> Mask:
        """Pixels of `domain` within geodesic distance about `r` of `seeds` (steps of r/steps)."""
        r_px = max(1.0, r / self.h / steps)
        reach = self.and_(seeds, domain)
        for _ in range(steps):
            reach = self.and_(self.dilate(reach, r_px), domain)
        return reach

    def components(self, a: Mask, limit: int = 2_000_000) -> List[List[Tuple[int, int]]]:
        """8-connected pieces of a sparse mask as pixel lists."""
        pix = set()
        for j, row in enumerate(a):
            x = row
            while x:
                low = x & -x
                pix.add((low.bit_length() - 1, j))
                x ^= low
                if len(pix) > limit:
                    raise ValueError("components: mask too large")
        out = []
        while pix:
            seed = pix.pop()
            comp = [seed]
            dq = deque([seed])
            while dq:
                i, j = dq.popleft()
                for di in (-1, 0, 1):
                    for dj in (-1, 0, 1):
                        q = (i + di, j + dj)
                        if q in pix:
                            pix.remove(q)
                            comp.append(q)
                            dq.append(q)
            out.append(comp)
        return out

    def describe(self, comp: List[Tuple[int, int]]) -> dict:
        xs = [self.xc(i) for i, _ in comp]
        ys = [self.yc(j) for _, j in comp]
        return dict(
            at=[round(sum(xs) / len(xs), 3), round(sum(ys) / len(ys), 3)],
            bbox=[round(min(xs), 3), round(min(ys), 3), round(max(xs), 3), round(max(ys), 3)],
            long_mm=round(max(max(xs) - min(xs), max(ys) - min(ys)) + self.h, 3),
            area_mm2=round(len(comp) * self.h * self.h, 5),
        )

    def window(self, a: Mask, i0: int, j0: int, nx: int, ny: int) -> Mask:
        """Sub-mask of `nx` x `ny` pixels from pixel (i0, j0)."""
        m = (1 << nx) - 1
        return [(a[j] >> i0) & m if 0 <= j < self.ny else 0 for j in range(j0, j0 + ny)]


def _smear(x: int, w: int) -> int:
    """OR of x shifted by -w..+w bits."""
    y = x << w
    n = 2 * w + 1
    s = y
    c = 1
    while c < n:
        st = min(c, n - c)
        s |= s >> st
        c += st
    return s
