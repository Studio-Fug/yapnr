"""Ball lattice inference for an area-array part (BGA, LGA, WLCSP).

The lattice is read from the pads alone, in the part's own frame (pad offsets):
the lands of the most common size give the pitch along each axis and an origin;
every land on a lattice point (1 um tolerance) is a ball, any other land (an
exposed pad, a mounting land) stays an obstacle. Sites follow from it:

``ball``         a lattice point with a land;
``vacant``       a lattice point inside the array's hull without one;
``interstitial`` the centre of a lattice cell inside the hull;
``outside``      any half-lattice point beyond the hull.

Positions are in **half-lattice** units ``(a, b)``: the point ``origin + (a * px / 2,
b * py / 2)``. A ball sits at even ``a`` and ``b``, an interstitial site at odd ones,
the midpoint of two neighbouring balls (a channel) at one odd and one even. Ring
``k`` is the k-th row of balls in from the hull's edge (0 = the outer row).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

TOL = 1e-3  # mm: a land is on the lattice when within this of a lattice point


@dataclass
class PadLand:
    """One surface pad of the part in its own frame."""

    name: str
    net: str
    centre: Tuple[float, float]
    size: Tuple[float, float]
    corner: Optional[float]  # exact land corner radius (pnr.graph.Pad.land_corner), else None


@dataclass
class Lattice:
    px: float
    py: float
    origin: Tuple[float, float]
    cols: int
    rows: int
    balls: Dict[Tuple[int, int], PadLand] = field(default_factory=dict)  # (col, row) -> land
    others: List[PadLand] = field(default_factory=list)  # lands off the lattice

    def point(self, a: int, b: int) -> Tuple[float, float]:
        """The local point of half-lattice node ``(a, b)``."""
        return (self.origin[0] + a * self.px / 2.0, self.origin[1] + b * self.py / 2.0)

    def node_of(self, col: int, row: int) -> Tuple[int, int]:
        return (2 * col, 2 * row)

    @property
    def hull(self) -> Tuple[int, int, int, int]:
        """Half-lattice bounds of the ball hull: ``(a0, b0, a1, b1)``."""
        return (0, 0, 2 * (self.cols - 1), 2 * (self.rows - 1))

    def ring(self, col: int, row: int) -> int:
        return min(col, row, self.cols - 1 - col, self.rows - 1 - row)

    def inside(self, a: int, b: int) -> bool:
        a0, b0, a1, b1 = self.hull
        return a0 <= a <= a1 and b0 <= b <= b1

    def kind(self, a: int, b: int) -> str:
        """The site kind of node ``(a, b)``: ball, vacant, interstitial, channel or outside."""
        if not self.inside(a, b):
            return "outside"
        if a % 2 == 0 and b % 2 == 0:
            return "ball" if (a // 2, b // 2) in self.balls else "vacant"
        if a % 2 and b % 2:
            return "interstitial"
        return "channel"

    def vacant(self) -> List[Tuple[int, int]]:
        return sorted(
            (c, r) for c in range(self.cols) for r in range(self.rows) if (c, r) not in self.balls
        )

    def summary(self) -> Dict:
        rings = Counter(self.ring(c, r) for c, r in self.balls)
        return dict(
            pitch_mm=[round(self.px, 6), round(self.py, 6)],
            origin=[round(self.origin[0], 6), round(self.origin[1], 6)],
            cols=self.cols,
            rows=self.rows,
            balls=len(self.balls),
            vacant=[[c, r] for c, r in self.vacant()],
            balls_per_ring={str(k): rings[k] for k in sorted(rings)},
            off_lattice=sorted(p.name for p in self.others),
        )


def _pitch(values: Sequence[float]) -> Optional[float]:
    """The modal (then smallest) gap between neighbouring distinct coordinates."""
    distinct = sorted({round(v, 4) for v in values})
    gaps = Counter(round(b - a, 3) for a, b in zip(distinct, distinct[1:]) if b - a > TOL)
    if not gaps:
        return None
    return min(gaps, key=lambda g: (-gaps[g], g))


def infer(pads: Sequence[PadLand]) -> Lattice:
    """The lattice of ``pads`` (raise ValueError when they form no array)."""
    if len(pads) < 4:
        raise ValueError("an area array needs at least four lands")
    sizes = Counter(tuple(sorted((round(p.size[0], 3), round(p.size[1], 3)))) for p in pads)
    common = max(sizes, key=lambda s: (sizes[s], s))
    balls = [p for p in pads if tuple(sorted((round(p.size[0], 3), round(p.size[1], 3)))) == common]
    px = _pitch([p.centre[0] for p in balls])
    py = _pitch([p.centre[1] for p in balls])
    if px is None and py is None:
        raise ValueError("the lands do not form an array")
    px = px or py
    py = py or px
    # Refine the pitch on the whole extent (rounding of a 0.65 mm pitch to 0.001 is exact).
    x0 = min(p.centre[0] for p in balls)
    y0 = min(p.centre[1] for p in balls)
    on, off = {}, []
    for p in pads:
        c = (p.centre[0] - x0) / px
        r = (p.centre[1] - y0) / py
        if (
            p in balls
            and abs(c - round(c)) * px <= TOL
            and abs(r - round(r)) * py <= TOL
            and (round(c), round(r)) not in on
        ):
            on[(int(round(c)), int(round(r)))] = p
        else:
            off.append(p)
    if len(on) < 4:
        raise ValueError("fewer than four lands on one lattice")
    cmin = min(c for c, _ in on)
    rmin = min(r for _, r in on)
    balls_at = {(c - cmin, r - rmin): p for (c, r), p in on.items()}
    cols = max(c for c, _ in balls_at) + 1
    rows = max(r for _, r in balls_at) + 1
    origin = (x0 + cmin * px, y0 + rmin * py)
    if cols < 2 or rows < 2:
        raise ValueError("a single row of lands is not an area array")
    return Lattice(px=px, py=py, origin=origin, cols=cols, rows=rows, balls=balls_at, others=off)
