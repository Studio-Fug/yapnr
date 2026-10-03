"""Compact placement (``PNR_COMPACT``) and shrink-to-fit (``PNR_SHRINK``), default off.

Design: docs/design/compact-placement.md. The switches are read by :mod:`pnr.compact_flags`
(stdlib only); every call site guards on them, so with the flags unset the engine takes
exactly its previous paths and writes no new keys into its JSON. This module is stdlib-only.

Parts of ``PNR_COMPACT=1`` (``PNR_COMPACT_<PART>=0`` drops one):

``GP``
    Global placement at spread 1.0 (:func:`spread`, overriding the ladder's board-filling
    1.3) in the placer, the initial pool and the place-route loop; the random starts of
    the global stage and the initial pool are drawn in a cluster box (:func:`cluster_box`)
    around the fixed parts instead of the whole board. Space then grows only where the
    router measured congestion (:func:`pnr.route.feedback.derive_inflation`, unchanged).
``RANK``
    A compactness tie-break in the Monte Carlo selections, always after every completion
    key (:func:`metrics`; the orders are listed there).
``LEGALIZE``
    Legalizer settings from :func:`legalize_settings`: the courtyard gap (authored
    ``board.courtyard_clearance_mm``, else :data:`GAP_MM`) instead of the routing
    clearance, a per-part copper margin, a :data:`GRID_MM` slot grid and the fab edge
    rules for pads and drills. Copper clearance stays with the router and native DRC.
``COURTYARD``
    Offset courtyards: a part occupies its ingested body box (``Component.body``: the
    courtyard, or the body, united with the pads and silk, as it lies about the origin),
    turned with the part (:func:`pnr.place.geometry.courtyard_rect`,
    :func:`pnr.place.geometry.body_shift`); ``pos`` stays the footprint origin.
``DROPS``
    A ``plane_layer`` net without a declared stack gets its plane drops from the detailed
    router, planned with the signal escapes (:func:`pnr.route.detail.router.route_board`),
    instead of writeback's dog-bones after routing.

``PNR_SHRINK=1`` (:func:`shrink_search`, flat driver only) treats the outline as an
envelope and keeps the smallest probed outline whose place-route loop converges.
"""

from __future__ import annotations

import copy
import math
from typing import Callable, Dict, List, NamedTuple, Optional, Tuple

from pnr.compact_flags import PARTS, active, enabled, shrink_enabled

__all__ = [
    "PARTS",
    "active",
    "enabled",
    "shrink_enabled",
    "spread",
    "body_box",
    "body_rect",
    "part_area",
    "metrics",
    "rank_bucket",
    "cluster_box",
    "box_coordinate",
    "courtyard_gap",
    "copper_clearance",
    "pad_inset",
    "margins",
    "LegalizeSettings",
    "legalize_settings",
    "margin_kwargs",
    "placement_clearance",
    "shrink_skip_reason",
    "shrink_lower_bound",
    "scaled_constraints",
    "moved_fixed",
    "probe_size",
    "shrink_search",
]

# Courtyard-to-courtyard gap (mm) when the board block authors none: nominally zero
# (touching KiCad courtyards are legal; they already carry about 0.25 mm around the
# pads), with 10 um against KiCad reporting exactly touching courtyards after nm rounding.
GAP_MM = 0.01
# Slot grid (mm) of the compact legalizer (the placer's default is 0.25 mm).
GRID_MM = 0.125
# Cluster box area as a multiple of the summed body area.
CLUSTER_AREA_FACTOR = 2.0
# Bucket count of the compactness tie-break: the bbox in 5 % steps of the outline.
BUCKETS = 20
# Extra block utilisations of the hierarchical driver's block trials.
UTILISATIONS = (0.5, 0.6)
# Shrink search: utilisation of the area lower bound, probe count, size rounding (mm), the
# band (fraction of the outline) next to the far (north, east) edges within which a fixed
# ``at`` keeps its distance to that edge (anywhere else it keeps its absolute position; the
# outline keeps its origin), and the margin (mm) added around the envelope run's bounding
# box (twice the edge clearance plus this, per axis) for the first probe.
SHRINK_UTILISATION = 0.7
SHRINK_PROBES = 4
SHRINK_ROUND_MM = 0.5
SHRINK_EDGE_BAND = 0.25
SHRINK_MARGIN_MM = 0.5


def spread(value: float) -> float:
    """``GP``: the global spread floor is 1.0; otherwise ``value`` unchanged."""
    return 1.0 if enabled("GP") else value


# ------------------------------------------------------------------- metrics


def body_box(comp) -> Tuple[float, float, float, float]:
    """``comp``'s body box ``(x0, y0, x1, y1)`` in its unrotated frame, whatever the flags:
    the ingested ``Component.body``, else the ``courtyard`` centred on the origin
    (:func:`pnr.place.regions.courtyard_box`)."""
    from .regions import courtyard_box

    return courtyard_box(comp)


def body_rect(comp) -> Tuple[float, float, float, float]:
    """``(x0, y0, x1, y1)`` of ``comp``'s body box at its pose (board frame)."""
    from .regions import rotate_box

    x0, y0, x1, y1 = rotate_box(body_box(comp), comp.rot)
    x, y = comp.pos
    return (x + x0, y + y0, x + x1, y + y1)


def part_area(comp) -> float:
    """The body box area (mm^2) of ``comp``."""
    x0, y0, x1, y1 = body_box(comp)
    return (x1 - x0) * (y1 - y0)


def metrics(graph, width: float, height: float) -> Dict[str, object]:
    """Compactness of a placed graph on a ``width`` x ``height`` outline, measured on the
    body boxes (:func:`body_rect`) of every part, fixed ones included, whatever the flags
    (so every A/B arm is measured alike).

    ``bbox_mm2`` is the bounding box of the bodies, ``area_mm2`` their summed area,
    ``utilization`` = area / bbox, ``occupancy`` = area / outline area and ``bucket`` =
    floor(:data:`BUCKETS` * bbox / outline area), the bbox in 5 % steps of the outline.
    Floats are rounded to 1e-3 so rankings are reproducible.

    Selection orders with ``RANK`` (completion keys always first):

    - routed initial-pool finalists, the halving screen and the hierarchical top seed
      (:func:`pnr.place.initial_pool.route_rank`): ``missing, unresolved,
      length_unmatched, vias, bucket, length``, then the id (``bucket`` before ``vias``
      under ``PNR_SHRINK``, where the outline follows the bounding box);
    - halving native and deep stages: ``bbox_mm2`` after every completion key, before
      the id;
    - the place-route loop's best round: ``(overflow, unfinished, bbox_mm2)``;
    - hierarchical blocks: :func:`pnr.hier.synth.rank_key` already prefers less area;
      the driver adds the block utilisations :data:`UTILISATIONS`.
    """
    rects = [body_rect(c) for c in graph.components]
    board = float(width) * float(height)
    if not rects or board <= 0:
        return dict(
            bbox_mm2=0.0,
            bbox_mm=[0.0, 0.0],
            area_mm2=0.0,
            utilization=0.0,
            occupancy=0.0,
            bucket=0,
        )
    left = min(r[0] for r in rects)
    bottom = min(r[1] for r in rects)
    right = max(r[2] for r in rects)
    top = max(r[3] for r in rects)
    bw, bh = right - left, top - bottom
    bbox = bw * bh
    area = sum((r[2] - r[0]) * (r[3] - r[1]) for r in rects)
    return dict(
        bbox_mm2=round(bbox, 3),
        bbox_mm=[round(bw, 3), round(bh, 3)],
        area_mm2=round(area, 3),
        utilization=round(area / bbox, 3) if bbox > 0 else 0.0,
        occupancy=round(area / board, 3),
        bucket=int(math.floor(BUCKETS * bbox / board + 1e-9)),
    )


def rank_bucket(graph, width: float, height: float) -> int:
    """``RANK``: the compactness bucket of a placed graph (:func:`metrics`)."""
    return int(metrics(graph, width, height)["bucket"])


# ------------------------------------------------------------- global stage


def cluster_box(graph, constraints, width: float, height: float):
    """``(x0, y0, w, h)``: where ``GP`` draws the random starts.

    A rectangle of :data:`CLUSTER_AREA_FACTOR` times the summed body area with the
    outline's aspect, centred on the fixed parts' centroid (the outline centre without
    fixed parts) and clamped inside the outline."""
    from .geometry import resolve_fixed_poses

    total = sum(part_area(c) for c in graph.components)
    width, height = float(width), float(height)
    area = min(width * height, CLUSTER_AREA_FACTOR * total)
    aspect = width / height
    bw = min(width, math.sqrt(area * aspect))
    bh = min(height, math.sqrt(area / aspect))
    poses = resolve_fixed_poses(graph, constraints)
    points = [poses[ref] for ref in sorted(poses)]
    if points:
        cx = sum(p[0] for p in points) / len(points)
        cy = sum(p[1] for p in points) / len(points)
    else:
        cx, cy = width / 2.0, height / 2.0
    cx = min(max(cx, bw / 2.0), width - bw / 2.0)
    cy = min(max(cy, bh / 2.0), height - bh / 2.0)
    return (cx - bw / 2.0, cy - bh / 2.0, bw, bh)


def box_coordinate(u: float, half: float, lo: float, size: float, limit: float) -> float:
    """A unit draw ``u`` mapped into ``[lo, lo + size]`` for a part of half extent
    ``half`` (its centre stays ``half`` inside the box, or at the box centre when it does
    not fit), then kept inside ``[half, limit - half]`` of the outline."""
    if size >= 2.0 * half:
        value = lo + half + u * (size - 2.0 * half)
    else:
        value = lo + size / 2.0
    return min(max(value, half), max(half, limit - half))


# ----------------------------------------------------------------- legalizer


def courtyard_gap(constraints) -> float:
    """``LEGALIZE`` courtyard-to-courtyard gap: authored ``board.courtyard_clearance_mm``,
    else :data:`GAP_MM`."""
    authored = getattr(constraints.board, "courtyard_clearance_mm", None)
    return GAP_MM if authored is None else float(authored)


def copper_clearance(rules, constraints=None) -> float:
    """The routing copper clearance (mm): the rules' fab clearance, else their default
    clearance, else the constraint file's fab clearance (0.2 when nothing says)."""
    rules = rules or {}
    fab = rules.get("fab") or {}
    if fab.get("clearance_mm") is not None:
        return float(fab["clearance_mm"])
    if rules.get("default_clearance_mm") is not None:
        return float(rules["default_clearance_mm"])
    fabspec = getattr(constraints, "fab", None)
    if fabspec is not None and getattr(fabspec, "clearance_mm", None) is not None:
        return float(fabspec.clearance_mm)
    return 0.2


def occupied_box(comp) -> Tuple[float, float, float, float]:
    """The box a part occupies in the placer (unrotated frame): its offset body with
    ``COURTYARD``, else the centred ``courtyard``."""
    from .geometry import compact_body

    body = compact_body(comp)
    if body is not None:
        return body
    w, h = comp.courtyard
    return (-w / 2.0, -h / 2.0, w / 2.0, h / 2.0)


def pad_inset(comp) -> float:
    """Smallest distance (mm) from a pad's copper to the edge of the part's occupied box
    (:func:`occupied_box`, unrotated frame); inf for a part without sized pads."""
    x0, y0, x1, y1 = occupied_box(comp)
    inset = math.inf
    for pad in comp.pads:
        pw, ph = pad.size
        if pw <= 0 or ph <= 0:
            continue
        x, y = pad.offset
        inset = min(
            inset,
            (x - pw / 2.0) - x0,
            x1 - (x + pw / 2.0),
            (y - ph / 2.0) - y0,
            y1 - (y + ph / 2.0),
        )
    return inset


def margins(graph, copper: float) -> Dict[str, float]:
    """Per-part slot margin ``m = max(0, copper / 2 - pad inset)``: two neighbours' pads
    then keep at least the copper clearance even when a box hugs its pads (zero for
    KiCad library courtyards, 0.25 mm or more outside the pads). Macros take none. Only
    non-zero margins are listed."""
    out = {}
    for comp in graph.components:
        if str(comp.footprint).startswith(("block:", "line:")):
            continue
        m = max(0.0, copper / 2.0 - pad_inset(comp))
        if m > 0 and math.isfinite(m):
            out[comp.ref] = round(m, 6)
    return out


class LegalizeSettings(NamedTuple):
    """``LEGALIZE`` legalizer settings: the courtyard ``gap`` (mm, the legalizer's
    ``clearance``), the slot ``grid_mm`` and the per-part copper ``margins``."""

    gap: float
    grid_mm: float
    margins: Dict[str, float]


def legalize_settings(graph, constraints, rules) -> Optional[LegalizeSettings]:
    """The compact legalizer settings, or None with ``LEGALIZE`` off. The pad and drill
    edge rule is switched on by :func:`pnr.place.legalize.pad_edge_rule`."""
    if not enabled("LEGALIZE"):
        return None
    return LegalizeSettings(
        gap=courtyard_gap(constraints),
        grid_mm=GRID_MM,
        margins=margins(graph, copper_clearance(rules, constraints)),
    )


def margin_kwargs(settings: Optional[LegalizeSettings]) -> dict:
    """``{"margins": ...}`` for a placement check of :func:`legalize_settings`'s copper
    margins, ``{}`` without any (the flag off, or no part hugging its pads)."""
    return {} if not (settings and settings.margins) else dict(margins=settings.margins)


def placement_clearance(constraints) -> float:
    """The clearance placement keeps between courtyards: the courtyard gap with
    ``LEGALIZE``, else the board's ``default_clearance_mm`` (unchanged)."""
    if enabled("LEGALIZE"):
        return courtyard_gap(constraints)
    return float(constraints.board.default_clearance_mm)


# -------------------------------------------------------------- shrink to fit


def _round_size(value: float) -> float:
    return max(SHRINK_ROUND_MM, math.floor(value / SHRINK_ROUND_MM + 0.5) * SHRINK_ROUND_MM)


def probe_size(scale: float, width: float, height: float) -> Tuple[float, float]:
    """The outline of a probe at ``scale``, rounded to :data:`SHRINK_ROUND_MM` (never
    larger than the envelope)."""
    return (
        min(float(width), _round_size(scale * width)),
        min(float(height), _round_size(scale * height)),
    )


def _scaled_coordinate(value: float, size: float, new: float) -> float:
    """A fixed ``at`` coordinate on a ``size`` axis shrunk to ``new`` (the outline keeps
    its origin and loses its far edge): within :data:`SHRINK_EDGE_BAND` of the far edge it
    keeps its distance to that edge (a part on the north or east edge stays on it);
    anywhere else it is absolute and never moves."""
    if value >= (1.0 - SHRINK_EDGE_BAND) * size:
        return float(new - (size - value))
    return float(value)


def shrink_skip_reason(constraints, auto_outline=False) -> Optional[str]:
    """None when the shrink search applies to a flat run, else why it is skipped: the
    rubber-band outline, keep-outs (absolute polygons do not scale) and regions."""
    if auto_outline:
        return "auto_outline"
    kinds = {c.kind for c in constraints.constraints}
    if "keepout" in kinds:
        return "keep-outs"
    if "region" in kinds:
        return "regions"
    return None


def scaled_constraints(constraints, width: float, height: float, new_w: float, new_h: float):
    """A copy of ``constraints`` on a ``new_w`` x ``new_h`` outline: a fixed ``at`` near
    the far edge keeps its distance to it, any other stays put (:func:`_scaled_coordinate`);
    edge rules, groups, rows and line groups are relative and follow the outline by
    themselves."""
    out = copy.deepcopy(constraints)
    out.board.width = float(new_w)
    out.board.height = float(new_h)
    for con in out.constraints:
        if con.kind == "fixed" and con.params.get("at"):
            x, y = con.params["at"]
            con.params["at"] = [
                _scaled_coordinate(float(x), width, new_w),
                _scaled_coordinate(float(y), height, new_h),
            ]
    return out


def moved_fixed(constraints, scaled) -> List[dict]:
    """The fixed ``at`` poses ``scaled`` (:func:`scaled_constraints` of ``constraints``)
    moved: ``[{"refs", "at", "to"}]``, only parts on a far edge's band."""
    before = [c for c in constraints.constraints if c.kind == "fixed"]
    after = [c for c in scaled.constraints if c.kind == "fixed"]
    out = []
    for a, b in zip(before, after):
        at, to = a.params.get("at"), b.params.get("at")
        if at and to and [float(v) for v in at] != [float(v) for v in to]:
            out.append(dict(refs=list(a.refs), at=[float(v) for v in at], to=list(to)))
    return out


def shrink_lower_bound(graph, constraints, width: float, height: float) -> float:
    """Smallest outline scale worth probing: the summed body area at
    :data:`SHRINK_UTILISATION`, every part fitting the outline at its better rotation, and
    every fixed part's box inside the scaled outline (an ``at`` in a far edge's band keeps
    its distance to that edge, any other keeps its position)."""
    from .geometry import courtyard_rect

    area = sum(part_area(c) for c in graph.components)
    bound = math.sqrt(area / (SHRINK_UTILISATION * width * height)) if area > 0 else 0.0
    long_side, short_side = max(width, height), min(width, height)
    for comp in graph.components:
        x0, y0, x1, y1 = occupied_box(comp)
        w, h = x1 - x0, y1 - y0
        bound = max(bound, max(w, h) / long_side, min(w, h) / short_side)
    held = {}
    for con in constraints.constraints:
        if con.kind == "fixed":
            for ref in con.refs:
                held[ref] = con
    for ref, con in sorted(held.items()):
        try:
            comp = copy.deepcopy(graph.component(ref))
        except KeyError:
            continue
        comp.rot = float(con.params.get("rot") or 0.0)
        at = con.params.get("at")
        if not at:
            # An edge-resolved part needs only its own extent along the outline.
            r = courtyard_rect(comp)
            bound = max(bound, r.w / width, r.h / height)
            continue
        comp.pos = (float(at[0]), float(at[1]))
        r = courtyard_rect(comp)
        for lo, hi, x, size in (
            (r.left, r.right, float(at[0]), width),
            (r.bottom, r.top, float(at[1]), height),
        ):
            if x >= (1.0 - SHRINK_EDGE_BAND) * size:  # keeps its distance to the far edge
                bound = max(bound, (size - lo) / size)
            else:  # stays put: its box must end inside the scaled outline
                bound = max(bound, hi / size)
    return min(1.0, bound)


def first_probe_scale(bbox_mm, edge_clearance: float, width: float, height: float) -> float:
    """The scale of the probe after the envelope run: the run's body bounding box plus
    ``2 * (edge_clearance + SHRINK_MARGIN_MM)`` per axis, as a fraction of the larger
    relative size (1.0 when that does not shrink)."""
    pad = 2.0 * (float(edge_clearance) + SHRINK_MARGIN_MM)
    return min(1.0, max((bbox_mm[0] + pad) / width, (bbox_mm[1] + pad) / height))


def shrink_search(
    probe: Callable[[float, float], Tuple[object, bool]],
    width: float,
    height: float,
    lower: float,
    first: Optional[float] = None,
    probes: int = SHRINK_PROBES,
) -> Tuple[Optional[Tuple[float, float]], Optional[object], List[dict]]:
    """Up to ``probes`` outline scales in ``[lower, 1)``, in a fixed order: ``first`` (the
    envelope run's bounding box, when given and above ``lower``), then bisection between
    the largest failed scale (``lower`` at first) and the smallest converged one (1.0, the
    envelope, at first).

    ``probe(w, h)`` runs one complete place-route loop on that outline and returns
    ``(result, ok)``; ``ok`` means converged and legal; it may raise
    :class:`pnr.place.legalize.LegalizationError` (a failed probe). Returns the smallest
    converged size and its result (None, None when no probe converged) and the probe
    records, in probe order. Sizes are rounded to :data:`SHRINK_ROUND_MM`; the search
    stops when the rounded size repeats or reaches the envelope."""
    from .legalize import LegalizationError

    lo, hi = float(lower), 1.0
    best_size, best = None, None
    records = []
    tried = set()
    envelope = probe_size(1.0, width, height)
    for index in range(int(probes)):
        if index == 0 and first is not None and lo < first < hi:
            scale = float(first)
        else:
            scale = (lo + hi) / 2.0
        size = probe_size(scale, width, height)
        if size in tried or size == envelope:
            break  # the rounded sizes no longer move
        tried.add(size)
        record = dict(index=index, scale=round(scale, 6), width=size[0], height=size[1])
        try:
            result, ok = probe(*size)
        except LegalizationError as error:
            record.update(converged=False, error="legalization: %s" % error)
            result, ok = None, False
        else:
            record["converged"] = bool(ok)
        records.append(record)
        if ok:
            hi = scale
            if best_size is None or size[0] * size[1] < best_size[0] * best_size[1]:
                best_size, best = size, result
        else:
            lo = scale
    return best_size, best, records
