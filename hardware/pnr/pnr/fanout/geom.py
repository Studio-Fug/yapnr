"""Exact planar geometry for the fanout generator (mm, stdlib only).

Every test here is an exact Euclidean distance between the shapes copper takes:
segments (track centrelines, a via is a zero-length one), circles and rounded
rectangles (lands) and polygons (keepouts, reserved corridors, the outline).
Nothing is rasterized: the fanout is judged on the same geometry KiCad's DRC
judges, and the router's grid only sees it afterwards.
"""

from __future__ import annotations

import math
from typing import List, Sequence, Tuple

Point = Tuple[float, float]


def point_segment_sq(p: Point, a: Point, b: Point) -> float:
    """Squared distance from ``p`` to the segment ``a``-``b`` (a point when equal)."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    den = dx * dx + dy * dy
    t = 0.0 if den == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / den))
    ex, ey = p[0] - a[0] - t * dx, p[1] - a[1] - t * dy
    return ex * ex + ey * ey


def _orient(a: Point, b: Point, c: Point) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def segments_cross(a: Point, b: Point, c: Point, d: Point) -> bool:
    """The closed segments ``a``-``b`` and ``c``-``d`` meet."""
    d1, d2 = _orient(c, d, a), _orient(c, d, b)
    d3, d4 = _orient(a, b, c), _orient(a, b, d)
    if ((d1 > 0 > d2) or (d1 < 0 < d2)) and ((d3 > 0 > d4) or (d3 < 0 < d4)):
        return True

    def on(p, q, r):  # r on p-q, given collinear
        return min(p[0], q[0]) <= r[0] <= max(p[0], q[0]) and min(p[1], q[1]) <= r[1] <= max(
            p[1], q[1]
        )

    return (
        (d1 == 0 and on(c, d, a))
        or (d2 == 0 and on(c, d, b))
        or (d3 == 0 and on(a, b, c))
        or (d4 == 0 and on(a, b, d))
    )


def segment_segment(a: Point, b: Point, c: Point, d: Point) -> float:
    """Distance between the segments ``a``-``b`` and ``c``-``d`` (0 if they meet)."""
    if a != b and c != d and segments_cross(a, b, c, d):
        return 0.0
    return math.sqrt(
        min(
            point_segment_sq(a, c, d),
            point_segment_sq(b, c, d),
            point_segment_sq(c, a, b),
            point_segment_sq(d, a, b),
        )
    )


def segment_box(a: Point, b: Point, x0: float, y0: float, x1: float, y1: float) -> float:
    """Distance from the segment ``a``-``b`` to the box ``[x0,x1] x [y0,y1]``."""
    if x0 <= a[0] <= x1 and y0 <= a[1] <= y1:
        return 0.0
    corners = ((x0, y0), (x1, y0), (x1, y1), (x0, y1))
    if a != b:
        for k in range(4):
            if segments_cross(a, b, corners[k], corners[(k + 1) % 4]):
                return 0.0
    best = min(segment_segment(a, b, corners[k], corners[(k + 1) % 4]) for k in range(4))
    return best


class Land:
    """A pad's copper: a rounded rectangle about ``centre`` with half extents
    ``hw``/``hh`` and corner radius ``corner`` (a circle: ``hw == hh == corner``),
    axis aligned in the frame it is given in."""

    __slots__ = ("centre", "hw", "hh", "corner", "net", "name")

    def __init__(self, centre, hw, hh, corner, net="", name=""):
        self.centre = (float(centre[0]), float(centre[1]))
        self.hw, self.hh = float(hw), float(hh)
        self.corner = max(0.0, min(float(corner), self.hw, self.hh))
        self.net = net
        self.name = name

    @property
    def radius(self) -> float:
        """The circumscribed radius (a bound for quick rejection)."""
        return math.hypot(self.hw, self.hh)

    def distance(self, a: Point, b: Point) -> float:
        """Distance from the segment ``a``-``b`` to this land's copper (0 inside)."""
        c = self.corner
        cx, cy = self.centre
        inner = segment_box(
            a, b, cx - self.hw + c, cy - self.hh + c, cx + self.hw - c, cy + self.hh - c
        )
        return max(0.0, inner - c)

    def contains(self, p: Point, margin: float = 0.0) -> bool:
        """``p`` lies inside the land shrunk by ``margin``."""
        return self.distance(p, p) == 0.0 and self.signed(p) <= -margin

    def signed(self, p: Point) -> float:
        """Signed distance (negative inside) from ``p`` to the land outline."""
        c = self.corner
        qx = abs(p[0] - self.centre[0]) - self.hw + c
        qy = abs(p[1] - self.centre[1]) - self.hh + c
        return math.hypot(max(qx, 0.0), max(qy, 0.0)) + min(max(qx, qy), 0.0) - c


def point_in_polygon(p: Point, poly: Sequence[Point]) -> bool:
    """Even-odd test (a point on the outline counts as inside)."""
    inside = False
    n = len(poly)
    for k in range(n):
        a, b = poly[k], poly[(k + 1) % n]
        if point_segment_sq(p, a, b) <= 1e-18:
            return True
        if (a[1] > p[1]) != (b[1] > p[1]):
            x = a[0] + (p[1] - a[1]) * (b[0] - a[0]) / (b[1] - a[1])
            if p[0] < x:
                inside = not inside
    return inside


def segment_polygon(a: Point, b: Point, poly: Sequence[Point]) -> float:
    """Distance from the segment ``a``-``b`` to the filled polygon (0 inside or across)."""
    if point_in_polygon(a, poly) or point_in_polygon(b, poly):
        return 0.0
    n = len(poly)
    return min(segment_segment(a, b, poly[k], poly[(k + 1) % n]) for k in range(n))


def polygon_bounds(poly: Sequence[Point]) -> Tuple[float, float, float, float]:
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    return (min(xs), min(ys), max(xs), max(ys))


def rect_polygon(rect: Sequence[float]) -> List[Point]:
    x0, y0, x1, y1 = (float(v) for v in rect)
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


class Pose:
    """A part's pose: local (the part's current-side unrotated frame, the frame of
    :attr:`pnr.graph.Pad.offset`) to board (engine mm, y up) and back. Quarter
    turns use exact cosines, so a lattice point maps to the same board point the
    router's pad rectangles use."""

    def __init__(self, pos, rot):
        self.pos = (float(pos[0]), float(pos[1]))
        self.rot = float(rot) % 360.0
        q = self.rot / 90.0
        if abs(q - round(q)) < 1e-9:
            self.c, self.s = ((1, 0), (0, 1), (-1, 0), (0, -1))[int(round(q)) % 4]
        else:
            r = math.radians(self.rot)
            self.c, self.s = math.cos(r), math.sin(r)

    @property
    def quarter(self) -> bool:
        return self.c in (0, 1, -1) and self.s in (0, 1, -1)

    def to_board(self, p: Point) -> Point:
        x, y = p
        return (self.pos[0] + self.c * x - self.s * y, self.pos[1] + self.s * x + self.c * y)

    def to_local(self, p: Point) -> Point:
        x, y = p[0] - self.pos[0], p[1] - self.pos[1]
        return (self.c * x + self.s * y, -self.s * x + self.c * y)

    def direction_to_board(self, v: Point) -> Point:
        return (self.c * v[0] - self.s * v[1], self.s * v[0] + self.c * v[1])


def compass(v: Point) -> str:
    """The board compass edge an outward unit vector points to (``x`` east, ``y`` north)."""
    if abs(v[0]) >= abs(v[1]):
        return "east" if v[0] > 0 else "west"
    return "north" if v[1] > 0 else "south"
