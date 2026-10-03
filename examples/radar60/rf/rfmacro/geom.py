"""Turtle paths (straights and arcs) with exact lengths, offsets and fence-via placement."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

Pt = Tuple[float, float]


@dataclass
class Seg:
    kind: str  # "line" or "arc"
    p0: Pt
    p1: Pt
    width: float
    center: Optional[Pt] = None
    radius: float = 0.0
    a0: float = 0.0  # start angle of the arc (rad, from the centre)
    sweep: float = 0.0  # signed sweep (rad), + = counter-clockwise

    @property
    def length(self) -> float:
        if self.kind == "line":
            return math.dist(self.p0, self.p1)
        return abs(self.sweep) * self.radius

    def mid(self) -> Pt:
        if self.kind == "line":
            return ((self.p0[0] + self.p1[0]) / 2, (self.p0[1] + self.p1[1]) / 2)
        a = self.a0 + self.sweep / 2
        return (
            self.center[0] + self.radius * math.cos(a),
            self.center[1] + self.radius * math.sin(a),
        )

    def sample(self, step: float) -> List[Tuple[Pt, float]]:
        """Points along the centreline with their heading (rad), about `step` apart."""
        n = max(1, int(math.ceil(self.length / step)))
        out = []
        for i in range(n + 1):
            t = i / n
            if self.kind == "line":
                x = self.p0[0] + t * (self.p1[0] - self.p0[0])
                y = self.p0[1] + t * (self.p1[1] - self.p0[1])
                h = math.atan2(self.p1[1] - self.p0[1], self.p1[0] - self.p0[0])
            else:
                a = self.a0 + t * self.sweep
                x = self.center[0] + self.radius * math.cos(a)
                y = self.center[1] + self.radius * math.sin(a)
                h = a + (math.pi / 2 if self.sweep > 0 else -math.pi / 2)
            out.append(((x, y), h))
        return out


@dataclass
class Path:
    start: Pt
    heading: float  # rad
    width: float
    segs: List[Seg] = field(default_factory=list)
    marks: dict = field(default_factory=dict)

    @property
    def pos(self) -> Pt:
        return self.segs[-1].p1 if self.segs else self.start

    @property
    def head(self) -> float:
        return self._head if hasattr(self, "_head") else self.heading

    def _set_head(self, h: float) -> None:
        self._head = h

    def straight(self, length: float, width: Optional[float] = None) -> "Path":
        if length < -1e-9:
            raise ValueError(f"negative straight {length:.4f}")
        if length < 1e-9:
            return self
        x, y = self.pos
        h = self.head
        p1 = (x + length * math.cos(h), y + length * math.sin(h))
        self.segs.append(Seg("line", (x, y), p1, width or self.width))
        return self

    def turn(self, radius: float, angle_deg: float, width: Optional[float] = None) -> "Path":
        """Arc of `angle_deg` (+ left / counter-clockwise) at centreline radius `radius`."""
        if abs(angle_deg) < 1e-12:
            return self
        x, y = self.pos
        h = self.head
        s = 1 if angle_deg > 0 else -1
        cx, cy = x + radius * math.cos(h + s * math.pi / 2), y + radius * math.sin(
            h + s * math.pi / 2
        )
        a0 = math.atan2(y - cy, x - cx)
        sweep = math.radians(angle_deg)
        a1 = a0 + sweep
        p1 = (cx + radius * math.cos(a1), cy + radius * math.sin(a1))
        self.segs.append(Seg("arc", (x, y), p1, width or self.width, (cx, cy), radius, a0, sweep))
        self._set_head(h + sweep)
        return self

    def mark(self, name: str) -> "Path":
        self.marks[name] = (self.pos, self.length)
        return self

    @property
    def length(self) -> float:
        return sum(s.length for s in self.segs)

    def fence(
        self, offset: float, pitch: float, skip_from: float = 0.0, skip_to: float = 0.0
    ) -> List[Pt]:
        """Via centres on both sides at `offset` from the centreline, `pitch` apart along the
        centreline, between `skip_from` from the start and `skip_to` before the end."""
        pts: List[Pt] = []
        tot = self.length
        s_acc = 0.0
        nxt = skip_from
        for seg in self.segs:
            L = seg.length
            while nxt <= s_acc + L + 1e-9 and nxt <= tot - skip_to + 1e-9:
                t = (nxt - s_acc) / L if L > 0 else 0.0
                (x, y), h = _at(seg, t)
                for side in (+1, -1):
                    pts.append((x + side * offset * -math.sin(h), y + side * offset * math.cos(h)))
                nxt += pitch
            s_acc += L
        return pts


def _at(seg: Seg, t: float) -> Tuple[Pt, float]:
    if seg.kind == "line":
        x = seg.p0[0] + t * (seg.p1[0] - seg.p0[0])
        y = seg.p0[1] + t * (seg.p1[1] - seg.p0[1])
        return (x, y), math.atan2(seg.p1[1] - seg.p0[1], seg.p1[0] - seg.p0[0])
    a = seg.a0 + t * seg.sweep
    return (
        (seg.center[0] + seg.radius * math.cos(a), seg.center[1] + seg.radius * math.sin(a)),
        a + (math.pi / 2 if seg.sweep > 0 else -math.pi / 2),
    )


def outline(path: Path, half: Optional[float] = None, step: float = 0.02) -> List[Pt]:
    """Closed polygon of the path's copper (or of a corridor of half-width `half`)."""
    left, right = [], []
    for seg in path.segs:
        hw = half if half is not None else seg.width / 2
        for (x, y), h in seg.sample(step):
            nx, ny = -math.sin(h), math.cos(h)
            left.append((x + hw * nx, y + hw * ny))
            right.append((x - hw * nx, y - hw * ny))
    return left + right[::-1]


def circle(c: Pt, r: float, n: int = 32) -> List[Pt]:
    return [
        (c[0] + r * math.cos(2 * math.pi * i / n), c[1] + r * math.sin(2 * math.pi * i / n))
        for i in range(n)
    ]


def rect(x0: float, y0: float, x1: float, y1: float) -> List[Pt]:
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def seg_dist(p: Pt, a: Pt, b: Pt) -> float:
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / L2))
    return math.dist(p, (ax + t * dx, ay + t * dy))


def path_dist(p: Pt, path: Path, step: float = 0.01) -> float:
    """Distance from a point to a path's centreline."""
    best = 1e9
    for seg in path.segs:
        pts = [q for q, _ in seg.sample(step)]
        for a, b in zip(pts, pts[1:]):
            best = min(best, seg_dist(p, a, b))
    return best


def poly_contains(poly: Sequence[Pt], p: Pt) -> bool:
    x, y = p
    inside = False
    n = len(poly)
    for i in range(n):
        x0, y0 = poly[i]
        x1, y1 = poly[(i + 1) % n]
        if (y0 > y) != (y1 > y):
            xi = x0 + (y - y0) * (x1 - x0) / (y1 - y0)
            if xi > x:
                inside = not inside
    return inside


def serpentine(
    path: Path, extra: float, radius: float, side: int, room: float, r_min: float = 0.4
) -> Tuple[int, float, float]:
    """Append fingers that add `extra` mm while the path keeps its heading. A finger turns 90°
    toward `side` (+1 left, -1 right), runs `a`, U-turns, runs back `a` and turns 90° back: it
    advances 4R, reaches 2R + a sideways and adds (2π − 4)R + 2a. `room` bounds 2R + a. The
    fewest fingers are used, each with the smallest radius in [r_min, radius] that fits (the
    shortest advance). Returns (fingers, R, a)."""
    if extra <= 1e-9:
        return 0, 0.0, 0.0
    k = 2 * math.pi - 4
    best = None
    for n in range(1, 12):
        e = extra / n
        for r in [r_min + i * 0.01 for i in range(int(round((radius - r_min) / 0.01)) + 1)]:
            a = (e - k * r) / 2
            if a >= 0 and 2 * r + a <= room + 1e-9:
                best = (n, r, a)
                break
        if best:
            break
    if best is None:
        raise ValueError(f"serpentine: {extra:.3f} mm does not fit in room {room:.3f} mm")
    n, r, a = best
    for _ in range(n):
        path.turn(r, 90 * side)
        path.straight(a)
        path.turn(r, -180 * side)
        path.straight(a)
        path.turn(r, 90 * side)
    return best
