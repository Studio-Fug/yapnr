"""Legalization motion: how far legalization moves parts from their global-placement poses.

Global placement (:mod:`pnr.place.model`) leaves a continuous layout whose relative order (who
sits left of, above, next to whom) carries most of its wirelength structure; the legalizer
should keep that and move only what conflicts. This module measures it (stdlib only, besides
the placement geometry):

- :func:`slot_rect`: a part's reserved slot, as the legalizer sizes it (courtyard times its
  inflation, plus the clearance and twice its copper margin), at any pose;
- :func:`occlusion`: per part, the fraction of its slot covered by other slots, by fixed parts
  and keep-outs, or lying outside the outline (0: legal where it is; 1: fully occluded);
- :func:`motion`: per part, the displacement and rotation change from one pose set to
  another, the moved count, and the topology kept (the fraction of pairwise left/right and
  above/below relations of the first pose set that the second keeps).

:func:`pnr.place.legalize.legalize` records :func:`motion` of each call as its ``motion``
(the placement report, the trace's ``legal`` event and the run results carry it).
"""

from __future__ import annotations

import math
from typing import Dict, Iterable, List, Optional, Tuple

from pnr.place.geometry import Rect, courtyard_rect, occupied_sides

# A part moved by at most this (mm) only snapped to the slot grid (``grid_mm`` 0.125 to 0.5:
# half a cell diagonal is at most 0.36 mm).
SNAP_MM = 0.4
# Pairs whose relation is this close to a tie (mm) at the first pose set are not counted.
TIE_MM = 0.05


def slot_rect(comp, clearance: float, infl: float = 1.0, margin: float = 0.0) -> Rect:
    """The slot ``comp`` reserves at its pose: its courtyard scaled by ``infl`` and grown by
    ``clearance`` and twice ``margin`` (two disjoint slots keep the courtyards ``clearance``
    apart)."""
    cr = courtyard_rect(comp)
    return Rect(
        cr.cx,
        cr.cy,
        cr.w * infl + clearance + 2 * margin,
        cr.h * infl + clearance + 2 * margin,
    )


def _overlap(a: Rect, b: Rect) -> float:
    dx = min(a.right, b.right) - max(a.left, b.left)
    dy = min(a.top, b.top) - max(a.bottom, b.bottom)
    return dx * dy if dx > 0 and dy > 0 else 0.0


def planes(comp) -> Tuple[str, ...]:
    """The occupancy planes a part tests (both sides for a drilled part or a block)."""
    if any(p.through_hole for p in comp.pads):
        return ("top", "bottom")
    return tuple(occupied_sides(comp))


def occlusion(
    components,
    width: float,
    height: float,
    *,
    clearance: float,
    movable: Iterable[str],
    keepouts=(),
    inflation: Optional[Dict[str, float]] = None,
    spread: float = 1.0,
    margins: Optional[Dict[str, float]] = None,
    ignore: Iterable[str] = (),
) -> Dict[str, float]:
    """{ref: occluded fraction of its slot} for the ``movable`` parts at their poses: the area
    of its slot covered by the other parts' slots (on a shared plane), the keep-outs and the
    space outside the outline, over its slot area (capped at 1). ``ignore`` (refs) leaves those
    parts out as obstacles. Fixed parts are obstacles at their poses."""
    inflation = inflation or {}
    margins = margins or {}
    movable = set(movable)
    ignore = set(ignore)
    slots = {}
    for c in components:
        infl = max(1.0, spread, float(inflation.get(c.ref, 1.0))) if c.ref in movable else 1.0
        slots[c.ref] = (slot_rect(c, clearance, infl, margins.get(c.ref, 0.0)), set(planes(c)))
    out = {}
    board = Rect(width / 2.0, height / 2.0, width, height)
    for c in components:
        if c.ref not in movable:
            continue
        rect, sides = slots[c.ref]
        area = max(rect.w * rect.h, 1e-12)
        covered = area - _overlap(rect, board)
        for k in keepouts:
            covered += _overlap(rect, k)
        for other in components:
            if other is c or other.ref in ignore:
                continue
            orect, osides = slots[other.ref]
            if sides & osides:
                covered += _overlap(rect, orect)
        out[c.ref] = min(1.0, max(0.0, covered / area))
    return out


def _turn(a: float, b: float) -> float:
    d = abs((float(b) - float(a)) % 360.0)
    return min(d, 360.0 - d)


def motion(before, after, refs: Optional[Iterable[str]] = None, snap_mm: float = SNAP_MM) -> dict:
    """Displacement from ``before`` to ``after`` (graphs, or {ref: (x, y, rot)}) over ``refs``
    (default: every part in both): ``moved`` (parts displaced more than ``snap_mm`` or
    turned), ``sum_mm``, ``max_mm``, ``mean_mm``, ``turned``, ``topology`` (the fraction of
    pairwise x and y orders of ``before`` kept by ``after``; 1.0 with fewer than two parts)
    and ``parts`` ({ref: [distance mm, turn degrees]})."""
    a, b = _poses(before), _poses(after)
    keys = sorted((set(a) & set(b)) if refs is None else (set(refs) & set(a) & set(b)))
    parts = {}
    for ref in keys:
        (x0, y0, r0), (x1, y1, r1) = a[ref], b[ref]
        parts[ref] = [round(math.hypot(x1 - x0, y1 - y0), 4), round(_turn(r0, r1), 3)]
    dist = [d for d, _ in parts.values()]
    moved = sum(1 for d, t in parts.values() if d > snap_mm or t > 1e-6)
    kept = total = 0
    for i, p in enumerate(keys):
        for q in keys[i + 1 :]:
            for k in (0, 1):
                d0 = a[q][k] - a[p][k]
                if abs(d0) <= TIE_MM:
                    continue
                total += 1
                if (b[q][k] - b[p][k]) * d0 > 0:
                    kept += 1
    return dict(
        parts=parts,
        count=len(keys),
        moved=moved,
        turned=sum(1 for _, t in parts.values() if t > 1e-6),
        sum_mm=round(sum(dist), 3),
        max_mm=round(max(dist, default=0.0), 3),
        mean_mm=round(sum(dist) / len(dist), 3) if dist else 0.0,
        topology=round(kept / total, 4) if total else 1.0,
    )


def _poses(value) -> Dict[str, Tuple[float, float, float]]:
    if hasattr(value, "components"):
        return {c.ref: (float(c.pos[0]), float(c.pos[1]), float(c.rot)) for c in value.components}
    return {k: (float(v[0]), float(v[1]), float(v[2])) for k, v in dict(value).items()}


def summary(record: dict) -> dict:
    """``record`` (a :func:`motion` result) without its per-part rows."""
    return {k: v for k, v in record.items() if k != "parts"}


# The PNR_LEGALIZE_KEEP triage counts a legalizer record carries (pnr.place.legalize).
TRIAGE = (
    "clean",
    "mild",
    "severe",
    "pushed",
    "relocated",
    "anchored",
    "channel_short",
    "channel_unopened",
    "nudged",
    "relocated_severe",
    "relocated_push_infeasible",
    "relocated_slot_taken",
)


def combine(records: List[dict]) -> dict:
    """Totals over several :func:`motion` records (stages or calls): counts and sums add (the
    triage counts too, when every record has them), ``max_mm`` is the largest and ``topology``
    the pair-weighted mean (by part count)."""
    if not records:
        return dict(count=0, moved=0, turned=0, sum_mm=0.0, max_mm=0.0, mean_mm=0.0, topology=1.0)
    count = sum(r["count"] for r in records)
    total = sum(r["sum_mm"] for r in records)
    pairs = [(r["count"] * (r["count"] - 1) / 2.0, r["topology"]) for r in records]
    weight = sum(w for w, _ in pairs)
    triage = {
        k: sum(r[k] for r in records)
        for k in TRIAGE
        if all(isinstance(r.get(k), (int, float)) for r in records)
    }
    return dict(
        **triage,
        count=count,
        moved=sum(r["moved"] for r in records),
        turned=sum(r["turned"] for r in records),
        sum_mm=round(total, 3),
        max_mm=round(max(r["max_mm"] for r in records), 3),
        mean_mm=round(total / count, 3) if count else 0.0,
        topology=round(sum(w * t for w, t in pairs) / weight, 4) if weight else 1.0,
    )
