"""Pure millimetre copper clearance checks shared by grid and saved-board tuning."""

import math
from typing import Dict, List, Tuple

from pnr import length_model as lm


def _seg_dist(a, b, c, d) -> float:
    """Minimum distance between segments ab and cd (mm)."""

    def point_seg(p, s0, s1):
        vx, vy = s1[0] - s0[0], s1[1] - s0[1]
        ll = vx * vx + vy * vy
        if ll <= 0:
            return math.dist(p, s0)
        t = max(0.0, min(1.0, ((p[0] - s0[0]) * vx + (p[1] - s0[1]) * vy) / ll))
        return math.dist(p, (s0[0] + t * vx, s0[1] + t * vy))

    def cross(o, p, q):
        return (p[0] - o[0]) * (q[1] - o[1]) - (p[1] - o[1]) * (q[0] - o[0])

    d1, d2 = cross(c, d, a), cross(c, d, b)
    d3, d4 = cross(a, b, c), cross(a, b, d)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)) and d1 and d2 and d3 and d4:
        return 0.0
    return min(point_seg(a, c, d), point_seg(b, c, d), point_seg(c, a, b), point_seg(d, a, b))


def _seg_poly_dist(a, b, poly) -> float:
    if lm._inside_convex(poly, a) or lm._inside_convex(poly, b):
        return 0.0
    n = len(poly)
    return min(_seg_dist(a, b, poly[k], poly[(k + 1) % n]) for k in range(n))


class CopperIndex:
    """Other nets' copper in millimetres, bucketed for distance queries."""

    def __init__(self, bucket: float = 1.0):
        self.bucket = bucket
        self.items: Dict[Tuple[int, int], List[tuple]] = {}

    def _cells(self, x0, y0, x1, y1):
        b = self.bucket
        for i in range(math.floor(x0 / b), math.floor(x1 / b) + 1):
            for j in range(math.floor(y0 / b), math.floor(y1 / b) + 1):
                yield (i, j)

    def add(self, item, box):
        for key in self._cells(*box):
            self.items.setdefault(key, []).append(item)

    def add_track(self, net, layer, a, b, half):
        box = (
            min(a[0], b[0]) - half,
            min(a[1], b[1]) - half,
            max(a[0], b[0]) + half,
            max(a[1], b[1]) + half,
        )
        self.add(("track", net, layer, a, b, half), box)

    def add_via(self, net, centre, radius):
        x, y = centre
        self.add(
            ("via", net, None, centre, radius),
            (x - radius, y - radius, x + radius, y + radius),
        )

    def add_pad(self, net, layers, outline):
        xs = [p[0] for p in outline]
        ys = [p[1] for p in outline]
        self.add(("pad", net, layers, outline), (min(xs), min(ys), max(xs), max(ys)))

    def clear(self, net, layer, a, b, half, clearance_of) -> bool:
        """True when segment ab of ``net`` (half width ``half``) on ``layer`` keeps
        ``clearance_of(other_net)`` from every other net's copper."""
        reach = half + 1.0
        box = (
            min(a[0], b[0]) - reach,
            min(a[1], b[1]) - reach,
            max(a[0], b[0]) + reach,
            max(a[1], b[1]) + reach,
        )
        seen = set()
        for key in self._cells(*box):
            for item in self.items.get(key, ()):
                if id(item) in seen or item[1] == net:
                    continue
                seen.add(id(item))
                need = clearance_of(item[1]) - 1e-6
                kind = item[0]
                if kind == "track":
                    if item[2] != layer:
                        continue
                    if _seg_dist(a, b, item[3], item[4]) < half + item[5] + need:
                        return False
                elif kind == "via":
                    if _seg_dist(a, b, item[3], item[3]) < half + item[4] + need:
                        return False
                else:
                    if "*" not in item[2] and layer not in item[2]:
                        continue
                    if _seg_poly_dist(a, b, item[3]) < half + need:
                        return False
        return True
