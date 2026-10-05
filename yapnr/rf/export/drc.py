"""Minimum width and space check of the exported copper (design §10.2).

The polygons are rasterized at Δ/`sub` (Δ/8 by default) over the footprint with a void margin.
A morphological opening with a disk of diameter D = L_min − 2·(Δ/sub) (the slack absorbs the
raster) removes copper narrower than D; a closing fills gaps narrower than D. Not every removed
sample is a violation: an opening also trims convex corners, by at most (√2 − 1)·D/2. A
connected part of the removed copper (the residue) is a width violation when

- its island has no copper left after the opening (the whole island is too thin), or
- it reaches farther than (√2 − 1)·D/2 + 2 samples from the opened copper (a thin strip,
  spike or line), or
- it joins two parts of the opened copper (a neck, such as a diagonal pixel connection).

Space violations are the same tests on the void (gaps, slots and enclosed holes). Clearance to
copper outside the footprint is KiCad's job.

The raster test alone misses one shape of space violation: two separate copper regions whose
closest approach is a corner pinched against a corner (or an edge), diagonally. Point sampling
insets the rasterized boundary of each shape toward its interior by up to half a raster step in
each axis, so near a diagonal corner the two insets compound and the morphological opening sees
a gap that looks wider than it is — wide enough, in the minimal case, that a real sub-minimum
gap survives the opening with no residue at all, and the check reports no violation. A dedicated
exact check closes this: for every pair of distinct input polygons, the true (resolution
independent) minimum distance between their boundaries is computed by segment-segment distance
over all edge pairs, and a "corner" violation is raised when that distance is positive but under
`min_space_mm`. Two polygons meant to touch or overlap (distance 0, e.g. a net merged with
itself) are left alone — they are not a gap at all.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from yapnr.rf.export.raster import edt, label, rasterize


@dataclass
class Violation:
    kind: str  # "width" or "space"
    reason: str  # "island", "strip" or "neck"
    at_mm: tuple[float, float]  # a point of the residue
    extent_mm: float  # largest distance of the residue from the opened set
    box_mm: tuple[float, float, float, float] | None = None  # the residue's x0, x1, y0, y1


@dataclass
class DRCResult:
    min_width_mm: float
    min_space_mm: float
    resolution_mm: float
    violations: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations

    def to_json(self) -> dict:
        return {
            "min_width_mm": self.min_width_mm,
            "min_space_mm": self.min_space_mm,
            "resolution_mm": self.resolution_mm,
            "ok": self.ok,
            "violations": [
                {
                    "kind": v.kind,
                    "reason": v.reason,
                    "at_mm": list(v.at_mm),
                    "extent_mm": v.extent_mm,
                }
                for v in self.violations
            ],
        }


def _opening(mask: np.ndarray, radius: float) -> np.ndarray:
    """Morphological opening by a disk of `radius` samples (exact, through the EDT)."""
    eroded = edt(~mask) > radius
    return edt(eroded) <= radius


def _residue_violations(mask, opened, radius, kind, xs, ys) -> list[Violation]:
    residue = mask & ~opened
    if not residue.any():
        return []
    res_lab, n_res = label(residue)
    mask_lab, _ = label(mask)
    open_lab, _ = label(opened)
    dist = edt(opened)
    corner = (math.sqrt(2.0) - 1.0) * radius + 2.0
    step = float(xs[1] - xs[0]) if xs.size > 1 else 1.0
    out = []
    for r in range(1, n_res + 1):
        sel = res_lab == r
        isl = np.unique(mask_lab[sel])
        isl = isl[isl > 0]
        in_island = np.isin(mask_lab, isl)
        reach = float(dist[sel].max())
        i, j = np.argwhere(sel)[np.argmax(dist[sel])]
        at = (float(xs[i]), float(ys[j]))
        si, sj = np.nonzero(sel)
        box = (float(xs[si.min()]), float(xs[si.max()]), float(ys[sj.min()]), float(ys[sj.max()]))
        if not (opened & in_island).any():
            out.append(Violation(kind, "island", at, reach * step, box))
            continue
        if reach > corner:
            out.append(Violation(kind, "strip", at, reach * step, box))
            continue
        ring = np.zeros_like(sel)
        ring[1:, :] |= sel[:-1, :]
        ring[:-1, :] |= sel[1:, :]
        ring[:, 1:] |= sel[:, :-1]
        ring[:, :-1] |= sel[:, 1:]
        touching = np.unique(open_lab[ring & opened])
        if touching.size >= 2:
            out.append(Violation(kind, "neck", at, reach * step, box))
    return out


def _point_seg_dist(px, py, ax, ay, bx, by) -> tuple[float, float, float]:
    """Distance from point `p` to segment `ab`, and the closest point on `ab`."""
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 == 0.0 else ((px - ax) * dx + (py - ay) * dy) / length2
    t = max(0.0, min(1.0, t))
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(px - cx, py - cy), cx, cy


def _segments_cross(ax, ay, bx, by, cx, cy, dx, dy) -> bool:
    """True when open segments `ab` and `cd` intersect or touch (orientation test)."""

    def cross(ox, oy, px, py, qx, qy):
        return (px - ox) * (qy - oy) - (py - oy) * (qx - ox)

    d1 = cross(cx, cy, dx, dy, ax, ay)
    d2 = cross(cx, cy, dx, dy, bx, by)
    d3 = cross(ax, ay, bx, by, cx, cy)
    d4 = cross(ax, ay, bx, by, dx, dy)
    if ((d1 > 0) != (d2 > 0) or d1 == 0 or d2 == 0) and (
        (d3 > 0) != (d4 > 0) or d3 == 0 or d4 == 0
    ):
        # Full general-position crossing, plus the collinear/touching edge cases.
        if d1 == 0 and d2 == 0 and d3 == 0 and d4 == 0:
            return (
                min(ax, bx) <= max(cx, dx)
                and min(cx, dx) <= max(ax, bx)
                and min(ay, by) <= max(cy, dy)
                and min(cy, dy) <= max(ay, by)
            )
        return True
    return False


def _seg_seg_dist(a, b, c, d) -> tuple[float, float, float, float, float]:
    """Exact minimum distance between segments `ab` and `cd`, and the midpoint of the closest
    points (0.0 when they cross or touch): for non-crossing segments the minimum is always
    attained at an endpoint of one against the other segment, so the four endpoint-to-segment
    distances suffice."""
    ax, ay = a
    bx, by = b
    cx, cy = c
    dx, dy = d
    if _segments_cross(ax, ay, bx, by, cx, cy, dx, dy):
        return 0.0, ax, ay, ax, ay
    candidates = [
        _point_seg_dist(ax, ay, cx, cy, dx, dy) + (ax, ay),
        _point_seg_dist(bx, by, cx, cy, dx, dy) + (bx, by),
        _point_seg_dist(cx, cy, ax, ay, bx, by) + (cx, cy),
        _point_seg_dist(dx, dy, ax, ay, bx, by) + (dx, dy),
    ]
    dist, px, py, qx, qy = min(candidates, key=lambda t: t[0])
    return dist, px, py, qx, qy


def _corner_violations(polygons, min_space_mm: float) -> list[Violation]:
    """Exact (non-raster) space violations the morphological opening can miss: two distinct
    polygons whose boundaries come closer than `min_space_mm` at a point, such as a diagonally
    offset corner-to-corner pinch (see module docstring)."""
    out = []
    n = len(polygons)
    eps = 1e-9
    for i in range(n):
        pi = [tuple(p) for p in polygons[i]]
        for j in range(i + 1, n):
            pj = [tuple(p) for p in polygons[j]]
            best = None
            for ai in range(len(pi)):
                a, b = pi[ai], pi[(ai + 1) % len(pi)]
                for aj in range(len(pj)):
                    c, d = pj[aj], pj[(aj + 1) % len(pj)]
                    dist, px, py, qx, qy = _seg_seg_dist(a, b, c, d)
                    if best is None or dist < best[0]:
                        best = (dist, px, py, qx, qy)
                    if dist <= eps:
                        break
                if best is not None and best[0] <= eps:
                    break
            if best is None:
                continue
            dist, px, py, qx, qy = best
            if eps < dist < min_space_mm:
                at = (0.5 * (px + qx), 0.5 * (py + qy))
                box = (min(px, qx), max(px, qx), min(py, qy), max(py, qy))
                out.append(Violation("space", "corner", at, dist, box))
    return out


def check_width_space(
    polygons,
    bbox_mm: tuple[float, float, float, float],
    pitch_mm: float,
    min_width_mm: float,
    min_space_mm: float,
    sub: int = 8,
) -> DRCResult:
    """Check copper `polygons` (lists of (x, y) mm) inside `bbox_mm` (x0, x1, y0, y1)."""
    res = pitch_mm / sub
    result = DRCResult(min_width_mm, min_space_mm, res)
    lmax = max(min_width_mm, min_space_mm)
    if lmax <= 0:
        return result
    x0, x1, y0, y1 = bbox_mm
    pad = lmax + 2 * res
    xs = np.arange(x0 - pad + 0.5 * res, x1 + pad, res)
    ys = np.arange(y0 - pad + 0.5 * res, y1 + pad, res)
    copper = rasterize(polygons, xs, ys)
    if min_width_mm > 0:
        radius = 0.5 * (min_width_mm - 2 * res) / res
        opened = _opening(copper, radius)
        result.violations += _residue_violations(copper, opened, radius, "width", xs, ys)
    if min_space_mm > 0:
        radius = 0.5 * (min_space_mm - 2 * res) / res
        void = ~copper
        opened = _opening(void, radius)
        result.violations += _residue_violations(void, opened, radius, "space", xs, ys)
        result.violations += _corner_violations(polygons, min_space_mm)
    return result
