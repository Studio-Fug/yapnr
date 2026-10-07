"""Per-side occupancy hulls of hierarchical block macros (N-0001, ``PNR_MACRO_HULL=1``).

A macro built by :func:`pnr.hier.macro.collapse` with the hull flag carries
``Component.hull``: what its routed block really occupies on each copper side,
in the macro frame (origin at the courtyard centre, unrotated, top side up):

``shapes``  the physical primitives per side: ``rect`` [x0, y0, x1, y1] (member
            reservations and pads, exact) and ``cap`` [x0, y0, x1, y1, r] (outer-layer
            tracks and vias as capsules / discs of radius w/2 + c_cu - clearance,
            so that the courtyard spacing the legalizer keeps from them leaves
            the block copper clearance ``c_cu``; see pnr.hier.extent.build_hull);
``top``/``bottom``  the conservative cell cover of those shapes on the placement
            lattice (``grid_mm``, parity of the default legalizer slot), with
            enclosed pockets filled, merged into rectangles;
``inner``   the same cover of the block's inner-layer copper, via barrels and
            member drilled pads (hull v2). Only drilled parts (any PTH/NPTH pad,
            whose holes cross every inner layer) and other blocks reserve this
            plane, so SMD parts still nest over inner copper.

Consumers (all inert for components without a hull):

* :func:`hull_placement_rects` -> :func:`pnr.place.geometry.placement_rects`, so
  overlap checks (:mod:`pnr.place.metrics`) test hull rectangles per side and on
  the ``inner`` plane (:func:`inner_rects` puts drilled pads and solid blocks there);
* :func:`slot_masks` / :func:`free_map` -> :mod:`pnr.place.legalize`: a macro slot
  is legal when its per-side mask misses the occupancy of that side (FFT
  correlation) and the whole slot misses keep-outs; placing it marks only the
  mask, so later parts can nest into its free pockets;
* :func:`gp_bodies` -> the global placement overlap terms (:mod:`pnr.place.model`,
  :mod:`pnr.place.power_first`): a macro becomes a few per-side boxes.

Masks are the physical shapes grown by half the placement clearance, united with
the cell cover, so a part legalized next to a macro keeps the full clearance
from its copper and never intersects its hull rectangles (the zero-tolerance
overlap check). Mirrored (bottom-side) macros swap the sides and map y -> -y
before rotating, like :func:`pnr.place.geometry.set_component_side`; the
hierarchical flow only places macros on top.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections import OrderedDict
from typing import Dict, List, Optional, Tuple

import numpy as np

SIDES = ("top", "bottom")
PLANES = ("top", "bottom", "inner")  # legalizer occupancy planes when hulls are active
TOL = 1e-9
HULL_VERSION = 2
BODY_GRID_MM = 1.0  # global-placement cover resolution
BODY_MAX_PER_SIDE = 32
BODY_MIN_FILL = 0.5  # a cover block counts when at least this fraction is hull


def enabled() -> bool:
    return os.environ.get("PNR_MACRO_HULL") == "1"


def active(components) -> bool:
    """True when the hull flag is set and some component carries a hull."""
    return enabled() and any(getattr(c, "hull", None) for c in components)


# ------------------------------------------------------------------ transforms


def quarter(rot: float) -> int:
    k = rot / 90.0
    if abs(k - round(k)) > 1e-6:
        raise ValueError(f"hull macros rotate by quarter turns only, got {rot}")
    return int(round(k)) % 4


def _xf_point(x, y, k, mirror):
    if mirror:
        y = -y
    if k == 0:
        return x, y
    if k == 1:
        return -y, x
    if k == 2:
        return -x, -y
    return y, -x


def _side(side, mirror):
    if not mirror or side == "inner":
        return side
    return "bottom" if side == "top" else "top"


def xf_rect(r, k, mirror=False):
    ax, ay = _xf_point(r[0], r[1], k, mirror)
    bx, by = _xf_point(r[2], r[3], k, mirror)
    return [min(ax, bx), min(ay, by), max(ax, bx), max(ay, by)]


def xf_cap(c, k, mirror=False):
    ax, ay = _xf_point(c[0], c[1], k, mirror)
    bx, by = _xf_point(c[2], c[3], k, mirror)
    return [ax, ay, bx, by, c[4]]


def transformed(hull: dict, rot: float, mirror: bool = False) -> dict:
    """Hull shapes and cover rects rotated by ``rot`` (and mirrored): {side: {...}}."""
    k = quarter(rot)
    out = {s: dict(rect=[], cap=[], cover=[]) for s in PLANES}
    for side in PLANES:
        dst = out[_side(side, mirror)]
        shapes = hull.get("shapes", {}).get(side, {})
        dst["rect"] = [xf_rect(r, k, mirror) for r in shapes.get("rect", [])]
        dst["cap"] = [xf_cap(c, k, mirror) for c in shapes.get("cap", [])]
        dst["cover"] = [xf_rect(r, k, mirror) for r in hull.get(side, [])]
    return out


def hull_placement_rects(comp):
    """[(plane, Rect)] of the hull cover rectangles at the component's pose."""
    from .geometry import Rect

    mirror = comp.side == "bottom"
    k = quarter(comp.rot)
    out = []
    for side in PLANES:
        for r in comp.hull.get(side, []):
            x0, y0, x1, y1 = xf_rect(r, k, mirror)
            out.append(
                (
                    _side(side, mirror),
                    Rect(
                        comp.pos[0] + (x0 + x1) / 2, comp.pos[1] + (y0 + y1) / 2, x1 - x0, y1 - y0
                    ),
                )
            )
    return out


def drilled(comp) -> bool:
    """A part with a PTH/NPTH pad: its holes cross every inner layer."""
    return any(p.through_hole for p in comp.pads)


def inner_rects(comp):
    """[("inner", Rect)] of an ordinary component on the inner plane (hull flag on).

    A block macro without a hull (a solid rectangle) reserves its courtyard, a
    drilled part its drilled pads (pad or drill extent, whichever is larger);
    every other part reserves nothing there."""
    from .geometry import Rect, courtyard_rect, pad_rects

    if getattr(comp, "hull", None):
        return []
    if (comp.footprint or "").startswith("block:"):
        return [("inner", courtyard_rect(comp))]
    swap = int(round(comp.rot)) % 180 == 90
    out = []
    for pad, (_, _, r) in zip(comp.pads, pad_rects(comp)):
        if not pad.through_hole:
            continue
        dw, dh = (pad.drill_size[1], pad.drill_size[0]) if swap else pad.drill_size
        w, h = max(r.w, dw), max(r.h, dh)
        if w > 0 and h > 0:
            out.append(("inner", Rect(r.cx, r.cy, w, h)))
    return out


# ------------------------------------------------------------------ rasterization


def lattice(n: int, g: float) -> np.ndarray:
    """Cell edges of an ``n``-cell slot axis, relative to the slot centre."""
    return np.arange(n + 1, dtype=np.float64) * g - n * g / 2.0


def _window(lo, hi, edges):
    """Cell index range [i0, i1) whose cells may meet [lo, hi]."""
    n = len(edges) - 1
    g = edges[1] - edges[0]
    i0 = max(0, int(math.floor((lo - edges[0]) / g)) - 1)
    i1 = min(n, int(math.ceil((hi - edges[0]) / g)) + 1)
    return i0, i1


def raster_rects(mask, rects, grow, xe, ye):
    """Mark cells meeting any rect grown by ``grow`` (rounded corners; 0 = the rect)."""
    for x0, y0, x1, y1 in rects:
        c0, c1 = _window(x0 - grow, x1 + grow, xe)
        r0, r1 = _window(y0 - grow, y1 + grow, ye)
        if c1 <= c0 or r1 <= r0:
            continue
        cl, ch = xe[c0:c1], xe[c0 + 1 : c1 + 1]
        rl, rh = ye[r0:r1], ye[r0 + 1 : r1 + 1]
        okx = (x0 - grow < ch - TOL) & (x1 + grow > cl + TOL)
        oky = (y0 - grow < rh - TOL) & (y1 + grow > rl + TOL)
        hit = oky[:, None] & okx[None, :]
        if grow > 0:
            dx = np.maximum(0.0, np.maximum(cl - x1, x0 - ch))
            dy = np.maximum(0.0, np.maximum(rl - y1, y0 - rh))
            hit &= (dx[None, :] ** 2 + dy[:, None] ** 2) < (grow - TOL) ** 2
        mask[r0:r1, c0:c1] |= hit


def _pt_seg(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 <= 0:
        return np.hypot(px - ax, py - ay)
    t = np.clip(((px - ax) * dx + (py - ay) * dy) / L2, 0.0, 1.0)
    return np.hypot(px - (ax + t * dx), py - (ay + t * dy))


def raster_caps(mask, caps, grow, xe, ye):
    """Mark cells whose square lies closer than r + ``grow`` to a capsule's segment.

    Exact for convex sets: the segment-square distance is 0 when they intersect
    (separating-axis test), else the least vertex-to-other-set distance."""
    for ax, ay, bx, by, r in caps:
        R = r + grow
        if R <= 0:
            continue
        c0, c1 = _window(min(ax, bx) - R, max(ax, bx) + R, xe)
        r0, r1 = _window(min(ay, by) - R, max(ay, by) + R, ye)
        if c1 <= c0 or r1 <= r0:
            continue
        X0 = xe[c0:c1][None, :]
        X1 = xe[c0 + 1 : c1 + 1][None, :]
        Y0 = ye[r0:r1][:, None]
        Y1 = ye[r0 + 1 : r1 + 1][:, None]
        X0, X1, Y0, Y1 = np.broadcast_arrays(X0, X1, Y0, Y1)

        def pt_box(px, py):
            return np.hypot(
                np.maximum(0.0, np.maximum(X0 - px, px - X1)),
                np.maximum(0.0, np.maximum(Y0 - py, py - Y1)),
            )

        d = np.minimum(pt_box(ax, ay), pt_box(bx, by))
        for cx, cy in ((X0, Y0), (X1, Y0), (X0, Y1), (X1, Y1)):
            d = np.minimum(d, _pt_seg(cx, cy, ax, ay, bx, by))
        # separating axes: x, y and the segment normal
        inter = (
            (np.minimum(ax, bx) <= X1)
            & (np.maximum(ax, bx) >= X0)
            & (np.minimum(ay, by) <= Y1)
            & (np.maximum(ay, by) >= Y0)
        )
        nx, ny = -(by - ay), (bx - ax)
        if nx or ny:
            s = nx * ax + ny * ay
            proj = [nx * cx + ny * cy for cx, cy in ((X0, Y0), (X1, Y0), (X0, Y1), (X1, Y1))]
            lo = np.minimum(np.minimum(proj[0], proj[1]), np.minimum(proj[2], proj[3]))
            hi = np.maximum(np.maximum(proj[0], proj[1]), np.maximum(proj[2], proj[3]))
            inter &= (lo <= s) & (s <= hi)
        d = np.where(inter, 0.0, d)
        mask[r0:r1, c0:c1] |= d < R - TOL


def raster(shapes: dict, grow: float, bw: int, bh: int, g: float) -> np.ndarray:
    """(bh, bw) bool mask (row 0 = bottom) of ``shapes`` grown by ``grow``, slot-centred."""
    xe, ye = lattice(bw, g), lattice(bh, g)
    mask = np.zeros((bh, bw), dtype=bool)
    raster_rects(mask, shapes.get("rect", []), grow, xe, ye)
    raster_caps(mask, shapes.get("cap", []), grow, xe, ye)
    if shapes.get("cover"):
        raster_rects(mask, shapes["cover"], 0.0, xe, ye)
    return mask


def fill_pockets(mask: np.ndarray, erode_cells: int = 0) -> Tuple[np.ndarray, int]:
    """Fill free cells that no 4-connected free path links to the slot boundary.

    ``erode_cells`` closes channels narrower than ``2*erode_cells+1`` cells first
    (a pocket reachable only through them cannot be routed out on this side).
    Returns (filled mask, number of cells filled)."""
    free = ~mask
    core = free
    if erode_cells > 0:
        k = erode_cells
        pad = np.pad(free, k, constant_values=True)
        core = np.ones_like(free)
        for dy in range(-k, k + 1):
            for dx in range(-k, k + 1):
                core &= pad[k + dy : k + dy + free.shape[0], k + dx : k + dx + free.shape[1]]
    reach = np.zeros_like(free)
    reach[0, :] = core[0, :]
    reach[-1, :] = core[-1, :]
    reach[:, 0] |= core[:, 0]
    reach[:, -1] |= core[:, -1]
    while True:
        grown = reach.copy()
        grown[1:, :] |= reach[:-1, :]
        grown[:-1, :] |= reach[1:, :]
        grown[:, 1:] |= reach[:, :-1]
        grown[:, :-1] |= reach[:, 1:]
        grown &= core
        if np.array_equal(grown, reach):
            break
        reach = grown
    if erode_cells > 0:
        k = erode_cells
        pad = np.pad(reach, k, constant_values=False)
        grown = np.zeros_like(reach)
        for dy in range(-k, k + 1):
            for dx in range(-k, k + 1):
                grown |= pad[k + dy : k + dy + reach.shape[0], k + dx : k + dx + reach.shape[1]]
        reach = grown & free
    filled = free & ~reach
    return mask | filled, int(filled.sum())


def merge_cells(mask: np.ndarray, xe: np.ndarray, ye: np.ndarray) -> List[List[float]]:
    """Rectangles covering exactly the set cells: row runs, then equal runs merged upward."""
    rects = []
    open_runs: Dict[Tuple[int, int], int] = {}
    for r in range(mask.shape[0] + 1):
        runs = set()
        if r < mask.shape[0]:
            row = mask[r]
            c = 0
            n = len(row)
            while c < n:
                if row[c]:
                    c0 = c
                    while c < n and row[c]:
                        c += 1
                    runs.add((c0, c))
                else:
                    c += 1
        for run in list(open_runs):
            if run not in runs:
                r0 = open_runs.pop(run)
                rects.append([float(xe[run[0]]), float(ye[r0]), float(xe[run[1]]), float(ye[r])])
        for run in runs:
            open_runs.setdefault(run, r)
    rects.sort(key=lambda q: (q[1], q[0]))
    return rects


# ------------------------------------------------------------------ legalizer masks

_MASKS: "OrderedDict[tuple, Dict[str, np.ndarray]]" = OrderedDict()
_MASK_CACHE = 256


def hull_key(hull: dict) -> str:
    key = hull.get("key")
    if key:
        return key
    return hashlib.sha256(json.dumps(hull, sort_keys=True).encode()).hexdigest()[:16]


def slot_masks(comp, g: float, clearance: float, bw: int, bh: int) -> Dict[str, np.ndarray]:
    """Per-plane (bh, bw) masks of ``comp``'s hull in its legalizer slot at ``comp.rot``.

    Physical shapes grown by clearance/2 (so a neighbour block, which holds its
    own courtyard clearance/2 inside, ends up a full clearance away) united with
    the hull cover, so the cover rectangles never meet a neighbour courtyard.
    A v1 hull (no ``inner`` plane) gets an empty inner mask."""
    mirror = comp.side == "bottom"
    key = (hull_key(comp.hull), quarter(comp.rot), mirror, bw, bh, round(g, 9), round(clearance, 9))
    hit = _MASKS.get(key)
    if hit is not None:
        _MASKS.move_to_end(key)
        return hit
    shapes = transformed(comp.hull, comp.rot, mirror)
    masks = {side: raster(shapes[side], clearance / 2.0, bw, bh, g) for side in PLANES}
    for m in masks.values():
        m.setflags(write=False)
    _MASKS[key] = masks
    if len(_MASKS) > _MASK_CACHE:
        _MASKS.popitem(last=False)
    return masks


def block_free(occ: np.ndarray, bw: int, bh: int) -> np.ndarray:
    """True where the whole ``bh x bw`` block at (r, c) is free (integral image)."""
    ny, nx = occ.shape
    integ = np.zeros((ny + 1, nx + 1), dtype=np.int32)
    integ[1:, 1:] = np.cumsum(np.cumsum(occ.astype(np.int32), axis=0), axis=1)
    return (integ[bh:, bw:] - integ[:-bh, bw:] - integ[bh:, :-bw] + integ[:-bh, :-bw]) == 0


def correlate(occ: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """count[r, c] = sum(occ[r:r+bh, c:c+bw] & mask) for every valid top-left (FFT)."""
    ny, nx = occ.shape
    bh, bw = mask.shape
    F = np.fft.rfft2(occ.astype(np.float64), s=(ny, nx))
    M = np.fft.rfft2(mask.astype(np.float64), s=(ny, nx))
    corr = np.fft.irfft2(F * np.conj(M), s=(ny, nx))
    return corr[: ny - bh + 1, : nx - bw + 1]


def correlate_brute(occ: np.ndarray, mask: np.ndarray) -> np.ndarray:
    ny, nx = occ.shape
    bh, bw = mask.shape
    out = np.zeros((ny - bh + 1, nx - bw + 1))
    for i, j in zip(*np.nonzero(mask)):
        out += occ[i : i + ny - bh + 1, j : j + nx - bw + 1]
    return out


def free_map(
    occupancy: Dict[str, np.ndarray],
    keep_occ: np.ndarray,
    masks: Dict[str, np.ndarray],
    bw: int,
    bh: int,
) -> Optional[np.ndarray]:
    """Valid top-lefts (r, c) of a hull slot: per-plane masks miss that plane's
    occupancy (``inner`` only when the legalizer keeps one) and the whole slot
    misses the keep-outs. None when it cannot fit."""
    ny, nx = keep_occ.shape
    if bw > nx or bh > ny:
        return None
    free = block_free(keep_occ, bw, bh)
    for side in PLANES:
        m = masks.get(side)
        if m is None or side not in occupancy:
            continue
        if not m.any() or not occupancy[side].any():
            continue
        free &= correlate(occupancy[side], m) < 0.5
    return free


# ------------------------------------------------------------------ global placement bodies


def cover_boxes(hull: dict, side: str, grid: float = BODY_GRID_MM, limit: int = BODY_MAX_PER_SIDE):
    """(boxes [x0, y0, x1, y1] in the macro frame, covered fraction) for one side.

    The hull cover is resampled on a ``grid`` lattice (a block counts when at
    least BODY_MIN_FILL of it is hull), merged into rectangles and capped at the
    ``limit`` largest."""
    cover = hull.get(side) or []
    if not cover:
        return [], 1.0
    g = float(hull["grid_mm"])
    bw, bh = hull["cells"]
    xe, ye = lattice(bw, g), lattice(bh, g)
    fine = np.zeros((bh, bw), dtype=bool)
    raster_rects(fine, cover, 0.0, xe, ye)
    step = max(1, int(round(grid / g)))
    nbx, nby = int(math.ceil(bw / step)), int(math.ceil(bh / step))
    padded = np.zeros((nby * step, nbx * step), dtype=bool)
    padded[:bh, :bw] = fine
    frac = padded.reshape(nby, step, nbx, step).mean(axis=(1, 3))
    coarse = frac >= BODY_MIN_FILL
    cxe = xe[0] + np.arange(nbx + 1) * step * g
    cye = ye[0] + np.arange(nby + 1) * step * g
    boxes = merge_cells(coarse, cxe, cye)
    boxes.sort(key=lambda b: -(b[2] - b[0]) * (b[3] - b[1]))
    boxes = boxes[:limit]
    kept = np.zeros((nby, nbx), dtype=bool)
    raster_rects(kept, boxes, 0.0, cxe, cye)
    covered = np.kron(kept, np.ones((step, step), dtype=bool))[:bh, :bw] & fine
    return boxes, float(covered.sum()) / max(1, int(fine.sum()))


def gp_bodies(components, scale=None):
    """Overlap bodies for global placement, or None when no component has a hull.

    Returns dict(owner (B,), off4 (B, 4, 2), half4 (B, 4, 2), pair (B, B) float,
    upper triangle of "same side and different owner"). An ordinary part is one
    body at offset 0 with its courtyard half-size (times ``scale``) and its
    occupied sides; a hull macro is its per-side cover boxes, each rotating with
    the macro like a pin (offset) and a courtyard (half-size swap)."""
    import torch

    from .geometry import occupied_sides
    from .model import ANGLES

    if not active(components):
        return None
    owner, off4, half4, sides = [], [], [], []
    for i, c in enumerate(components):
        s = 1.0 if scale is None else float(scale[i])
        if getattr(c, "hull", None):
            mirror = c.side == "bottom"
            for side in SIDES:
                boxes, _ = cover_boxes(c.hull, side)
                for x0, y0, x1, y1 in boxes:
                    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
                    hx, hy = (x1 - x0) / 2 * s, (y1 - y0) / 2 * s
                    if mirror:
                        cy = -cy
                    offs, halves = [], []
                    for ang in ANGLES:
                        th = math.radians(ang)
                        ct, st = math.cos(th), math.sin(th)
                        offs.append((cx * ct - cy * st, cx * st + cy * ct))
                        halves.append((hx, hy) if int(round(ang / 90)) % 2 == 0 else (hy, hx))
                    owner.append(i)
                    off4.append(offs)
                    half4.append(halves)
                    sides.append({_side(side, mirror)})
            continue
        hx, hy = c.courtyard[0] / 2.0 * s, c.courtyard[1] / 2.0 * s
        owner.append(i)
        from .geometry import compact_body

        body = compact_body(c)
        if body is None:
            off4.append([(0.0, 0.0)] * 4)
        else:
            # PNR_COMPACT offset courtyard: one body box at its turning offset.
            cx, cy = (body[0] + body[2]) / 2.0, (body[1] + body[3]) / 2.0
            hx, hy = (body[2] - body[0]) / 2.0 * s, (body[3] - body[1]) / 2.0 * s
            off4.append([(cx, cy), (-cy, cx), (-cx, -cy), (cy, -cx)])
        half4.append([(hx, hy), (hy, hx), (hx, hy), (hy, hx)])
        sides.append(set(occupied_sides(c)))
    bits = torch.tensor([(1 if "top" in s else 0) | (2 if "bottom" in s else 0) for s in sides])
    own = torch.tensor(owner)
    pair = ((own[:, None] != own[None, :]) & ((bits[:, None] & bits[None, :]) != 0)).float()
    pair = torch.triu(pair, diagonal=1)
    return dict(
        owner=torch.tensor(owner, dtype=torch.long),
        off4=torch.tensor(off4, dtype=torch.float32),
        half4=torch.tensor(half4, dtype=torch.float32),
        pair=pair,
    )


def gp_overlap(bodies, pos, p, clearance):
    """Smooth pairwise overlap over bodies; ``pos`` (..., n, 2), ``p`` (..., n, 4)."""
    import torch

    own = bodies["owner"]
    pb = p[..., own, :]  # (..., B, 4)
    off = (pb.unsqueeze(-1) * bodies["off4"]).sum(-2)  # (..., B, 2)
    half = (pb.unsqueeze(-1) * bodies["half4"]).sum(-2)  # (..., B, 2)
    xy = pos[..., own, :] + off
    dx = (xy[..., :, None, 0] - xy[..., None, :, 0]).abs()
    dy = (xy[..., :, None, 1] - xy[..., None, :, 1]).abs()
    ox = torch.clamp(half[..., :, None, 0] + half[..., None, :, 0] + clearance - dx, min=0.0)
    oy = torch.clamp(half[..., :, None, 1] + half[..., None, :, 1] + clearance - dy, min=0.0)
    return (ox * oy * bodies["pair"]).sum((-1, -2))


def dovetail_weight() -> float:
    """``PNR_HULL_DOVETAIL`` (default 0: off): the weight, in wirelength millimetres per
    millimetre, of :func:`gp_pack` in global placement with hull macros."""
    raw = os.environ.get("PNR_HULL_DOVETAIL")
    if not raw:
        return 0.0
    value = float(raw)
    if not math.isfinite(value) or value < 0:
        raise ValueError("PNR_HULL_DOVETAIL takes a finite non-negative weight, got %r" % raw)
    return value


def macro_overlap_area_mm2(components) -> float:
    """Summed overlap area of hull-bearing macros' *plain* courtyard rectangles (the
    simple box every stage but the hull-aware ones still sees) at their placed poses.

    Two macros' simple boxes overlapping is illegal for an ordinary part -- it is only
    legal here because their real, hull-shaped copper does not actually collide in that
    shared square millimetre. A placement where this is 0 may still look "closer
    together" (smaller gutters, a smaller bounding box) without a single block having
    moved into a neighbour's notch; this is the number that tells the two apart."""
    from .geometry import courtyard_rect

    macros = [c for c in components if getattr(c, "hull", None)]
    total = 0.0
    for i, a in enumerate(macros):
        ra = courtyard_rect(a)
        for b in macros[i + 1 :]:
            rb = courtyard_rect(b)
            dx = min(ra.right, rb.right) - max(ra.left, rb.left)
            dy = min(ra.top, rb.top) - max(ra.bottom, rb.bottom)
            if dx > 0.0 and dy > 0.0:
                total += dx * dy
    return total


def gp_pack(bodies, pos, p, gamma):
    """Smooth half-perimeter of the box around every overlap body (log-sum-exp extremes).

    With the hull-shaped overlap of :func:`gp_overlap` this is a packing pressure: the
    cluster shrinks where the bodies can interlock, so a block slides into a notch of
    another (``PNR_HULL_DOVETAIL``), not only where wires pull it."""
    import torch

    from . import portable_math as pm

    own = bodies["owner"]
    pb = p[..., own, :]
    off = (pb.unsqueeze(-1) * bodies["off4"]).sum(-2)
    half = (pb.unsqueeze(-1) * bodies["half4"]).sum(-2)
    xy = pos[..., own, :] + off
    lo, hi = xy - half, xy + half
    ext = pm.logsumexp(torch.stack((hi[..., 0], -lo[..., 0], hi[..., 1], -lo[..., 1])) / gamma, -1)
    return gamma * ext.sum()


# ------------------------------------------------------------------ nesting (PNR_HULL_NEST)

NEST_ROUNDS = 4  # passes over the movable hull macros
NEST_RADIUS_MM = 6.0  # how far one nest move may take a macro from its pose


def nest_enabled() -> bool:
    """On for hull macros unless ``PNR_HULL_NEST=0``: :func:`nest` slides them into
    each other's notches, and the hierarchical driver keeps a rectangle-placement fallback
    when a hull placement does not route (``regression/hier_case.py``). Inert without
    ``PNR_MACRO_HULL=1``. (The global-placement polish, ``PNR_GP_POLISH``, measures overlap
    between hull bodies whenever hull macros are present: :func:`polish_bodies`.)"""
    return enabled() and os.environ.get("PNR_HULL_NEST", "1") == "1"


def _area(a, b) -> float:
    """Intersection area of two (x0, y0, x1, y1) boxes."""
    dx = min(a[2], b[2]) - max(a[0], b[0])
    dy = min(a[3], b[3]) - max(a[1], b[1])
    return dx * dy if dx > 0.0 and dy > 0.0 else 0.0


def _box(rects):
    return (
        min(r[0] for r in rects),
        min(r[1] for r in rects),
        max(r[2] for r in rects),
        max(r[3] for r in rects),
    )


def nesting_metrics(outlines, shapes) -> dict:
    """How far placed block macros nest into each other, from the board-frame geometry.

    ``outlines`` {name: (x0, y0, x1, y1)}: each block's plain rectangle; ``shapes``
    {name: [(plane, x0, y0, x1, y1)]}: its hull cover rectangles per plane.

    ``macro_overlap_mm2``  summed pairwise overlap of the plain rectangles (what a block
                           without a hull could never do);
    ``hull_interlock_mm2`` summed pairwise overlap of the boxes around each hull: one
                           block's routed extent reaching into another's, the dovetail
                           proper (an overlap of empty margins only does not count);
    ``hull_collision_mm2`` summed same-plane overlap of the hull rectangles themselves
                           (legal placements keep it 0).
    """
    names = sorted(outlines)
    over = inter = clash = 0.0
    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            over += _area(outlines[a], outlines[b])
            sa, sb = shapes.get(a) or [], shapes.get(b) or []
            if not sa or not sb:
                continue
            inter += _area(_box([s[1:] for s in sa]), _box([s[1:] for s in sb]))
            for pa, *ra in sa:
                for pb, *rb in sb:
                    if pa == pb or "all" in (pa, pb):
                        clash += _area(ra, rb)
    return dict(
        macro_overlap_mm2=round(over, 4),
        hull_interlock_mm2=round(inter, 4),
        hull_collision_mm2=round(clash, 4),
    )


def component_nesting(components) -> dict:
    """:func:`nesting_metrics` of the hull macros among placed ``components``."""
    from .geometry import courtyard_rect

    outlines, shapes = {}, {}
    for c in components:
        if not getattr(c, "hull", None):
            continue
        r = courtyard_rect(c)
        outlines[c.ref] = (r.left, r.bottom, r.right, r.top)
        shapes[c.ref] = [
            (plane, q.left, q.bottom, q.right, q.top) for plane, q in hull_placement_rects(c)
        ]
    return nesting_metrics(outlines, shapes)


def _grown(comp, grow):
    """[(plane, (x0, y0, x1, y1))] of ``comp``'s placement rectangles grown by ``grow``."""
    from .geometry import placement_rects

    return [
        (plane, (r.left - grow, r.bottom - grow, r.right + grow, r.top + grow))
        for plane, r in placement_rects(comp)
    ]


def keep_overlap(a, b, clearance: float) -> Optional[float]:
    """The overlap a displacement-keeping legalizer should see between components ``a`` and
    ``b`` at their poses: None when neither carries a hull (the caller's rectangle test
    stands), else the summed same-plane intersection of their placement rectangles (hull
    cover rectangles for a hull macro, the courtyard per occupied side otherwise), each
    grown by half the clearance. Two blocks whose plain rectangles overlap but whose hulls
    keep the clearance give 0: an interlocked pose is legal where it is."""
    if not (getattr(a, "hull", None) or getattr(b, "hull", None)):
        return None
    total = 0.0
    for pa, ra in _grown(a, clearance / 2.0):
        for pb, rb in _grown(b, clearance / 2.0):
            if pa == pb:
                total += _area(ra, rb)
    return total


def polish_bodies(bodies, comps, frozen, grid: float, clearance: float):
    """The overlap bodies of the global-placement polish phase with hull macros
    (``PNR_GP_POLISH`` / ``PNR_HULL_NEST``, :mod:`pnr.place.gp_polish`).

    An ordinary part becomes its legalizer slot at the frozen turn (``frozen["centre"]`` /
    ``frozen["half"]``: courtyard plus clearance rounded up to whole cells); a hull macro
    keeps its per-side cover boxes, grown by half the clearance and half a cell (the
    legalizer's snap). The pairwise overlap of these bodies is then taken with no further
    clearance (:func:`gp_overlap` with ``clearance=0``), so two macros' rectangles may
    overlap wherever their hull boxes miss each other."""
    import torch

    own = bodies["owner"]
    hull_body = torch.tensor([bool(getattr(comps[int(i)], "hull", None)) for i in own])
    grow = clearance / 2.0 + grid / 2.0
    off4 = bodies["off4"].clone()
    half4 = bodies["half4"].clone()
    slot_off = frozen["centre"][own].unsqueeze(1).expand(-1, 4, -1)
    slot_half = frozen["half"][own].unsqueeze(1).expand(-1, 4, -1)
    mask = hull_body.view(-1, 1, 1)
    off4 = torch.where(mask, off4, slot_off)
    half4 = torch.where(mask, half4 + grow, slot_half)
    return dict(bodies, off4=off4, half4=half4)


def _slot_of(comp, g, clearance):
    """(r, c, bw, bh) of ``comp``'s legalizer slot at its pose, None when it is off the
    lattice (pnr.place.legalize: the slot is courtyard plus clearance in whole cells,
    centred on the pose)."""
    from .geometry import body_shift, courtyard_rect

    cr = courtyard_rect(comp)
    bw = int(math.ceil((cr.w + clearance) / g - 1e-9))
    bh = int(math.ceil((cr.h + clearance) / g - 1e-9))
    shift = body_shift(comp) or (0.0, 0.0)
    cx, cy = comp.pos[0] + shift[0], comp.pos[1] + shift[1]
    c = cx / g - bw / 2.0
    r = cy / g - bh / 2.0
    if abs(c - round(c)) > 1e-6 or abs(r - round(r)) > 1e-6:
        return None
    return int(round(r)), int(round(c)), bw, bh


def _planes_of(comp):
    from .geometry import occupied_sides

    sides = ("top", "bottom") if any(p.through_hole for p in comp.pads) else occupied_sides(comp)
    if drilled(comp) or str(comp.footprint).startswith("block:"):
        sides = tuple(sides) + ("inner",)
    return tuple(sides)


def nest(
    graph, constraints, width, height, *, clearance, grid, keepouts=(), fixed=(), pad_edge=None
):
    """Slide the legalized hull macros of ``graph`` (in place) into each other's notches.

    The legalizer's own occupancy (:func:`slot_masks` of every other macro, the slots of
    the other parts on their planes, the keep-outs) gives every hull-legal top-left of a
    macro at each quarter turn (:func:`free_map`); among those within
    :data:`NEST_RADIUS_MM` the one with the least cost -- the half-perimeter wirelength of
    the macro's nets plus the half-perimeter of the layout's bounding box, weighted by
    :func:`dovetail_weight` (1 when unset) -- replaces the pose when it is cheaper. Rounds
    repeat (:data:`NEST_ROUNDS`) while some macro moves. A result with a hard violation the
    input did not have is undone; with the pad-edge rule (``pad_edge``,
    :func:`pnr.place.legalize.pad_edge_rule`) a pose must also keep the macro's pads inside
    :func:`pnr.place.legalize.pad_edge_box`. Returns a report dict: ``moved``, ``turned``,
    ``rounds``, the ``nesting`` (:func:`component_nesting`) after the moves, ``skipped`` (why
    nothing was tried) or ``undone``, and with moves the ``before`` poses ({ref: [x, y,
    rot]}) they started from and ``nesting_before``."""
    from .geometry import courtyard_rect, pin_positions
    from .metrics import hard_violations

    comps = list(graph.components)
    g = float(grid)
    macros = [c for c in comps if getattr(c, "hull", None) and c.ref not in fixed]
    report = dict(moved=0, turned=0, rounds=0)
    if not macros:
        report["skipped"] = "no movable hull macro"
        return report
    slots = {c.ref: _slot_of(c, g, clearance) for c in comps if c.ref not in fixed}
    off = sorted(ref for ref, s in slots.items() if s is None)
    if off:
        report["skipped"] = "off the placement lattice: %s" % ", ".join(off[:4])
        return report
    nx, ny = int(math.ceil(width / g)), int(math.ceil(height / g))
    keep_occ = np.zeros((ny, nx), dtype=bool)
    for k in keepouts:
        c0, c1 = int(math.floor(k.left / g)), int(math.ceil(k.right / g))
        r0, r1 = int(math.floor(k.bottom / g)), int(math.ceil(k.top / g))
        keep_occ[max(0, r0) : max(0, r1), max(0, c0) : max(0, c1)] = True
    weight = dovetail_weight() or 1.0
    nets = {}
    for net in graph.nets:
        for ref, _pad in net.pins:
            nets.setdefault(ref, set()).add(net.name)
    by_net = {n.name: n for n in graph.nets}
    pins = {c.ref: dict(pin_positions(c)) for c in comps}

    def occupancy(skip):
        occ = {p: np.zeros((ny, nx), dtype=bool) for p in PLANES}
        for c in comps:
            if c.ref == skip:
                continue
            if c.ref in fixed:
                # A fixed part's placement rectangles grown by the clearance (as the
                # legalizer marks them).
                from .geometry import placement_rects

                for p, q in placement_rects(c):
                    if p not in occ:
                        continue
                    a0 = max(0, int(math.floor((q.left - clearance / 2) / g)))
                    a1 = min(nx, int(math.ceil((q.right + clearance / 2) / g)))
                    b0 = max(0, int(math.floor((q.bottom - clearance / 2) / g)))
                    b1 = min(ny, int(math.ceil((q.top + clearance / 2) / g)))
                    if a1 > a0 and b1 > b0:
                        occ[p][b0:b1, a0:a1] = True
                continue
            r, cc, bw, bh = slots[c.ref]
            r0, c0 = max(0, r), max(0, cc)
            r1, c1 = min(ny, r + bh), min(nx, cc + bw)
            if r1 <= r0 or c1 <= c0:
                continue
            if getattr(c, "hull", None):
                masks = slot_masks(c, g, clearance, bw, bh)
                for p in PLANES:
                    occ[p][r0:r1, c0:c1] |= masks[p][r0 - r : r1 - r, c0 - cc : c1 - cc]
            else:
                for p in _planes_of(c):
                    occ[p][r0:r1, c0:c1] = True
        return occ

    def boxes_except(skip):
        rs = [courtyard_rect(c) for c in comps if c.ref != skip]
        if not rs:
            return None
        return (
            min(r.left for r in rs),
            min(r.bottom for r in rs),
            max(r.right for r in rs),
            max(r.top for r in rs),
        )

    def net_points(ref):
        """{net: (xs, ys) of its pins on other components} for ``ref``'s nets."""
        out = {}
        for name in nets.get(ref, ()):
            xs, ys = [], []
            for r2, pad in by_net[name].pins:
                if r2 != ref and (pad in pins.get(r2, {})):
                    x, y = pins[r2][pad]
                    xs.append(x)
                    ys.append(y)
            out[name] = (xs, ys)
        return out

    def cost_grid(m, rot, cand_xy, others_box):
        """Cost of macro ``m`` at turn ``rot`` with its slot centre at each of ``cand_xy`` (K, 2)."""
        saved = (m.pos, m.rot)
        m.pos, m.rot = (0.0, 0.0), rot
        local = dict(pin_positions(m))
        cr = courtyard_rect(m)
        m.pos, m.rot = saved
        K = len(cand_xy)
        wl = np.zeros(K)
        for name, (xs, ys) in net_points(m.ref).items():
            mine = [local[p] for r2, p in by_net[name].pins if r2 == m.ref and p in local]
            if not mine:
                continue
            mx = np.array([q[0] for q in mine])[None, :] + cand_xy[:, :1]
            my = np.array([q[1] for q in mine])[None, :] + cand_xy[:, 1:]
            lox, hix = mx.min(1), mx.max(1)
            loy, hiy = my.min(1), my.max(1)
            if xs:
                lox = np.minimum(lox, min(xs))
                hix = np.maximum(hix, max(xs))
                loy = np.minimum(loy, min(ys))
                hiy = np.maximum(hiy, max(ys))
            wl += (hix - lox) + (hiy - loy)
        x0 = cand_xy[:, 0] + cr.left
        x1 = cand_xy[:, 0] + cr.right
        y0 = cand_xy[:, 1] + cr.bottom
        y1 = cand_xy[:, 1] + cr.top
        if others_box is not None:
            x0 = np.minimum(x0, others_box[0])
            y0 = np.minimum(y0, others_box[1])
            x1 = np.maximum(x1, others_box[2])
            y1 = np.maximum(y1, others_box[3])
        return wl + weight * ((x1 - x0) + (y1 - y0))

    before_bad = {k: v for k, v in hard_violations(graph, constraints).items() if v}
    saved = {c.ref: (c.pos, c.rot) for c in macros}
    nesting_before = component_nesting(comps)
    total_before = None
    radius = max(1, int(round(NEST_RADIUS_MM / g)))
    for rnd in range(NEST_ROUNDS):
        report["rounds"] = rnd + 1
        moved = False
        for m in macros:
            occ = occupancy(m.ref)
            others = boxes_except(m.ref)
            current = cost_grid(m, m.rot, np.array([m.pos], dtype=float), others)[0]
            if total_before is None:
                total_before = current
            best = (current - 1e-6, None)
            r_now, c_now, _, _ = slots[m.ref]
            for k in range(4):
                rot = (m.rot + 90.0 * k) % 360.0
                saved_rot = m.rot
                m.rot = rot
                slot = _slot_of(m, g, clearance)
                m.rot = saved_rot
                if slot is None:
                    continue
                _, _, bw, bh = slot
                if bw > nx or bh > ny:
                    continue
                masks = slot_masks(_turned(m, rot), g, clearance, bw, bh)
                free = free_map(occ, keep_occ, masks, bw, bh)
                if free is None:
                    continue
                # The window around the current slot centre (same centre, new slot size).
                rc = r_now + slots[m.ref][3] / 2.0 - bh / 2.0
                cc = c_now + slots[m.ref][2] / 2.0 - bw / 2.0
                r0 = max(0, int(math.floor(rc)) - radius)
                c0 = max(0, int(math.floor(cc)) - radius)
                win = free[
                    r0 : int(math.ceil(rc)) + radius + 1, c0 : int(math.ceil(cc)) + radius + 1
                ]
                rr, cc_ = np.nonzero(win)
                if not len(rr):
                    continue
                rr = rr + r0
                cc_ = cc_ + c0
                xy = np.stack(((cc_ + bw / 2.0) * g, (rr + bh / 2.0) * g), axis=1)
                shift = _shift_of(m, rot)
                xy = xy - np.array(shift)[None, :]
                if pad_edge is not None:
                    from .legalize import pad_edge_box

                    bx0, bx1, by0, by1 = pad_edge_box(_turned(m, rot), pad_edge, width, height)
                    keep = (
                        (xy[:, 0] >= bx0 - 1e-9)
                        & (xy[:, 0] <= bx1 + 1e-9)
                        & (xy[:, 1] >= by0 - 1e-9)
                        & (xy[:, 1] <= by1 + 1e-9)
                    )
                    xy, rr, cc_ = xy[keep], rr[keep], cc_[keep]
                    if not len(xy):
                        continue
                costs = cost_grid(m, rot, xy, others)
                j = int(np.argmin(costs))
                if costs[j] < best[0]:
                    best = (
                        float(costs[j]),
                        (rot, (float(xy[j, 0]), float(xy[j, 1])), int(rr[j]), int(cc_[j]), bw, bh),
                    )
            if best[1] is not None:
                rot, pos, r, c, bw, bh = best[1]
                report["turned"] += int(abs(((rot - m.rot + 180) % 360) - 180) > 1e-6)
                m.rot, m.pos = rot, pos
                slots[m.ref] = (r, c, bw, bh)
                pins[m.ref] = dict(pin_positions(m))
                report["moved"] += 1
                moved = True
                trace_move(graph, report["moved"])
        if not moved:
            break
    after_bad = {k: v for k, v in hard_violations(graph, constraints).items() if v}
    new = {k: v for k, v in after_bad.items() if len(v) > len(before_bad.get(k, ()))}
    if new:
        for c in macros:
            c.pos, c.rot = saved[c.ref]
        report.update(moved=0, turned=0, undone=sorted(new))
    if new:
        trace_move(graph, report.get("moved", 0) + 1)  # the undo, for the animation
    report["nesting"] = component_nesting(comps)
    if report["moved"]:
        # The legalized poses the moves started from (the driver's fallback, pnr.hier.top).
        report["before"] = {
            ref: [pos[0], pos[1], rot]
            for ref, (pos, rot) in sorted(saved.items())
            if (pos, rot) != (graph.component(ref).pos, graph.component(ref).rot)
        }
        report["nesting_before"] = nesting_before
    return report


def trace_move(graph, step):
    """A ``poses`` event (stage ``hull-nest``) of ``graph`` when tracing (``PNR_TRACE_DIR``):
    the animation slides the blocks into their notches (:mod:`pnr.animate.hier`)."""
    from pnr import trace

    recorder = trace.current()
    if recorder is not None:
        recorder.poses("hull-nest", graph, iter=step, phase="placement")


def _turned(comp, rot):
    """A shallow stand-in of ``comp`` at turn ``rot`` (for :func:`slot_masks`)."""
    import copy

    t = copy.copy(comp)
    t.rot = rot
    return t


def _shift_of(comp, rot):
    from .geometry import body_shift

    t = _turned(comp, rot)
    return body_shift(t) or (0.0, 0.0)
