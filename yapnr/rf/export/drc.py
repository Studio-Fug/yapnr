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
        if not (opened & in_island).any():
            out.append(Violation(kind, "island", at, reach * step))
            continue
        if reach > corner:
            out.append(Violation(kind, "strip", at, reach * step))
            continue
        ring = np.zeros_like(sel)
        ring[1:, :] |= sel[:-1, :]
        ring[:-1, :] |= sel[1:, :]
        ring[:, 1:] |= sel[:, :-1]
        ring[:, :-1] |= sel[:, 1:]
        touching = np.unique(open_lab[ring & opened])
        if touching.size >= 2:
            out.append(Violation(kind, "neck", at, reach * step))
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
    return result
