"""What the gloss stage changed on a saved board, for its before/after frames.

The gloss stage (``run.py --gloss``, docs/design/gloss.md) rewrites the routed copper: it
dekinks corners, takes shortcuts, coalesces corridors and normalizes the tracks, merging
collinear pieces into one. Normalization changes the track list but not the copper, so the
frames compare geometry rather than track rows: a piece of a track is *changed* when no track
of the other board on the same layer, of the same width and running the same way covers it.
Tracks are sampled every :data:`STEP_UM`; runs of uncovered samples become the pieces drawn
(red where copper went, mint where it came), and pieces shorter than :data:`MIN_UM` (a
sample's rounding at a joint) are dropped. A via is changed when the other board has none at
its centre. Units are the trace's integer micrometres (docs/design/animations.md, 3.3).
"""

from __future__ import annotations

import math

STEP_UM = 100.0  # sample pitch along a track
EPS_UM = 10.0  # a sample this close to a covering track is on it
MIN_UM = 150.0  # shorter changed pieces are dropped
CELL_UM = 1000.0  # the spatial index's cell
SINE_TOL = 0.02  # |sin| of the angle between two tracks that still run the same way (~1.1 deg)


def copper_length_mm(copper):
    """Summed track length (mm) of a copper blob."""
    total = 0.0
    for _layer, x0, y0, x1, y1, _width in (copper or {}).get("tracks", []):
        total += math.hypot(x1 - x0, y1 - y0)
    return total / 1000.0


def _span(a, b, pad):
    lo, hi = min(a, b) - pad, max(a, b) + pad
    return range(int(math.floor(lo / CELL_UM)), int(math.floor(hi / CELL_UM)) + 1)


def _cells(x0, y0, x1, y1, pad):
    for cx in _span(x0, x1, pad):
        for cy in _span(y0, y1, pad):
            yield cx, cy


def _index(tracks):
    grid = {}
    for i, (_layer, x0, y0, x1, y1, _width) in enumerate(tracks):
        for cell in _cells(x0, y0, x1, y1, EPS_UM):
            grid.setdefault(cell, []).append(i)
    return grid


def _covered(px, py, dx, dy, layer, width, tracks, grid):
    """True when ``(px, py)`` lies on a track of ``layer`` and ``width`` running along the unit
    direction ``(dx, dy)`` (either way)."""
    cell = (int(math.floor(px / CELL_UM)), int(math.floor(py / CELL_UM)))
    for i in grid.get(cell, ()):
        other_layer, x0, y0, x1, y1, other_width = tracks[i]
        if other_layer != layer or other_width != width:
            continue
        length = math.hypot(x1 - x0, y1 - y0)
        if length < 1e-9:
            continue
        ux, uy = (x1 - x0) / length, (y1 - y0) / length
        if abs(ux * dy - uy * dx) > SINE_TOL:
            continue
        along = (px - x0) * ux + (py - y0) * uy
        if along < -EPS_UM or along > length + EPS_UM:
            continue
        if abs((px - x0) * uy - (py - y0) * ux) <= EPS_UM:
            return True
    return False


def _uncovered(tracks, other):
    """The pieces of ``tracks`` that no track of ``other`` covers, as track rows."""
    grid = _index(other)
    out = []
    for layer, x0, y0, x1, y1, width in tracks:
        length = math.hypot(x1 - x0, y1 - y0)
        if length < 1e-9:
            continue
        dx, dy = (x1 - x0) / length, (y1 - y0) / length
        k = max(1, int(math.ceil(length / STEP_UM)))
        flags = []
        for i in range(k):
            t = (i + 0.5) / k
            px, py = x0 + (x1 - x0) * t, y0 + (y1 - y0) * t
            flags.append(not _covered(px, py, dx, dy, layer, width, other, grid))
        i = 0
        while i < k:
            if not flags[i]:
                i += 1
                continue
            j = i
            while j < k and flags[j]:
                j += 1
            a, b = i / float(k), j / float(k)
            if (b - a) * length >= MIN_UM:
                piece = [layer]
                piece += [int(round(x0 + (x1 - x0) * a)), int(round(y0 + (y1 - y0) * a))]
                piece += [int(round(x0 + (x1 - x0) * b)), int(round(y0 + (y1 - y0) * b))]
                out.append(piece + [width])
            i = j
    return out


def _lone_vias(vias, other):
    centres = [(v[0], v[1]) for v in other]
    return [
        list(v)
        for v in vias
        if not any(abs(v[0] - x) <= EPS_UM and abs(v[1] - y) <= EPS_UM for x, y in centres)
    ]


def copper_change(before, after):
    """``(removed, added)``: copper blobs of what ``before`` has and ``after`` lacks, and the
    reverse (tracks as changed pieces, vias whole, no zones)."""
    before, after = before or {}, after or {}
    tb, ta = before.get("tracks", []), after.get("tracks", [])
    vb, va = before.get("vias", []), after.get("vias", [])
    removed = dict(tracks=_uncovered(tb, ta), vias=_lone_vias(vb, va), zones=[])
    added = dict(tracks=_uncovered(ta, tb), vias=_lone_vias(va, vb), zones=[])
    return removed, added


__all__ = ["copper_change", "copper_length_mm"]
