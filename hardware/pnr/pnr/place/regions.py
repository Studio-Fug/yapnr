"""Placement regions and alignments: the ``region`` and ``align`` constraints.

A **region** confines parts to an allowed area in board coordinates (the union of
rectangles and polygons): each ref's courtyard must lie inside it. An **align**
makes parts share one coordinate: the ``anchor`` of every ref (its origin, the
centre of its pad box, a named pad, or a courtyard edge) has the same ``x`` (or
``y``) within ``tol_mm``. Both are hard by default; a soft one is a weighted
penalty (protrusion squared, or the anchor's squared distance from the line).

Every placed ref carries *bodies* and an *anchor* in its own unrotated frame. A
plain part has one body, its body box: the courtyard widened to its pads and
silkscreen, as it lies about the origin (``Component.body``; the centred
``courtyard`` box when the ingest found it centred). A rigid macro
(:mod:`pnr.hier.macro`: a line group or an assembled block) carries the bodies and
anchor of the members it stands for (``params["bodies"]`` / an explicit
``{"point"}`` or ``{"box", "edge"}`` anchor), so the relation stays exact at every
macro rotation. Anchors are evaluated at the part's rotation and side: rotation
moves pad and edge anchors, never ``origin``.

Exactness: a rectangle, one polygon, and a union whose pieces have only
axis-parallel edges are tested exactly. Other unions are tested on a raster whose
grid lines are the pieces' own coordinates plus a :data:`REGION_CELL_MM` grid (a
cell counts when the union covers it); only cells cut by a sloped edge are
refused, so the test is conservative by at most one cell along sloped edges. The
legalizer uses that raster for every polygon and union, so its results always
pass the legality check.

:func:`check_feasible` refuses, before placement, a region or align no placement
can meet, naming it; :func:`snap_aligns` puts each hard align's anchors on one
line after legalization when that is legal.

Without a ``region`` or ``align`` in the design nothing here runs.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

from pnr.constraints import Enforcement

REGION_CELL_MM = 0.25
# The legalizer keeps an align band this much narrower than tol_mm (rounding).
ALIGN_SLACK_MM = 1e-4
# The legalizer's align band is at least this many slot pitches (grid_mm) wide on
# each side, so a second member always finds a slot; a narrower tol_mm (down to 0)
# is reached by snap_aligns after legalization.
ALIGN_BAND_FLOOR = 0.6
CHECK_EPS_MM = 1e-6
# Global-placement weights of the hard relations (a soft one uses its own weight).
GP_REGION_WEIGHT = 40.0
GP_ALIGN_WEIGHT = 20.0

Box = Tuple[float, float, float, float]  # (x0, y0, x1, y1)


def region_rules(constraints) -> list:
    return [c for c in constraints.constraints if c.kind == "region"]


def align_rules(constraints) -> list:
    return [c for c in constraints.constraints if c.kind == "align"]


def declared(constraints) -> bool:
    """True when the design declares a region or an align."""
    return any(c.kind in ("region", "align") for c in constraints.constraints)


# ------------------------------------------------------------------ local frame


def _quarter(rot: float) -> Optional[int]:
    q = rot / 90.0
    return int(round(q)) % 4 if abs(q - round(q)) < 1e-9 else None


def rotate_point(x: float, y: float, rot: float) -> Tuple[float, float]:
    """``(x, y)`` turned ``rot`` degrees CCW (exact at the cardinal angles)."""
    q = _quarter(rot)
    if q is not None:
        return ((x, y), (-y, x), (-x, -y), (y, -x))[q]
    t = math.radians(rot)
    c, s = math.cos(t), math.sin(t)
    return (x * c - y * s, x * s + y * c)


def rotate_box(box: Box, rot: float) -> Box:
    """The bounding box of ``box`` turned ``rot`` degrees CCW about the origin."""
    pts = [rotate_point(x, y, rot) for x in (box[0], box[2]) for y in (box[1], box[3])]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def courtyard_box(comp) -> Box:
    """``comp``'s body box in its unrotated frame: the ingested, possibly off-centre
    ``Component.body``, else the ``courtyard`` centred on the origin."""
    body = getattr(comp, "body", None)
    if body is not None:
        return tuple(float(v) for v in body)
    w, h = comp.courtyard
    return (-w / 2.0, -h / 2.0, w / 2.0, h / 2.0)


def bodies(comp, con) -> List[Box]:
    """``comp``'s bodies for ``con`` in its unrotated frame: its courtyard, or the
    member bodies a macro carries (``params["bodies"]``)."""
    override = (con.params.get("bodies") or {}).get(comp.ref)
    if override:
        return [tuple(float(v) for v in b) for b in override]
    return [courtyard_box(comp)]


def placed_boxes(comp, con, rot=None, pos=None) -> List[Box]:
    """Absolute body boxes at ``rot``/``pos`` (default: the current pose)."""
    rot = comp.rot if rot is None else rot
    x, y = comp.pos if pos is None else pos
    out = []
    for b in bodies(comp, con):
        r = rotate_box(b, rot)
        out.append((x + r[0], y + r[1], x + r[2], y + r[3]))
    return out


# ------------------------------------------------------------------ anchors


def anchor_spec(con, ref):
    return (con.params.get("anchors") or {}).get(ref, "origin")


def anchor_local(comp, spec):
    """``("point", (x, y))`` or ``("box", box, edge)`` in ``comp``'s unrotated frame."""
    if isinstance(spec, dict):
        if "point" in spec:
            return ("point", tuple(float(v) for v in spec["point"]))
        return ("box", tuple(float(v) for v in spec["box"]), spec["edge"])
    if spec == "origin":
        return ("point", (0.0, 0.0))
    if spec in ("centre", "center"):
        if not comp.pads:
            return ("point", (0.0, 0.0))
        xs = [p.offset[0] - p.size[0] / 2 for p in comp.pads]
        xs += [p.offset[0] + p.size[0] / 2 for p in comp.pads]
        ys = [p.offset[1] - p.size[1] / 2 for p in comp.pads]
        ys += [p.offset[1] + p.size[1] / 2 for p in comp.pads]
        return ("point", ((min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0))
    if spec == "pad1" or (isinstance(spec, str) and spec.startswith("pad:")):
        name = "1" if spec == "pad1" else spec[4:]
        for pad in comp.pads:
            if pad.name == name:
                return ("point", tuple(pad.offset))
        raise ValueError("align anchor %r: %s has no pad %r" % (spec, comp.ref, name))
    if spec in ("south", "north", "west", "east"):
        box = rotate_box(courtyard_box(comp), 0.0)
        return ("box", box, spec)
    raise ValueError("unknown align anchor %r" % (spec,))


def anchor_offset(comp, spec, axis: str, rot: Optional[float] = None) -> float:
    """The anchor's offset from ``comp.pos`` along ``axis`` at rotation ``rot``."""
    rot = comp.rot if rot is None else rot
    k = 0 if axis == "x" else 1
    local = anchor_local(comp, spec)
    if local[0] == "point":
        return rotate_point(local[1][0], local[1][1], rot)[k]
    box = rotate_box(local[1], rot)
    return {"west": box[0], "south": box[1], "east": box[2], "north": box[3]}[local[2]]


def anchor_value(comp, spec, axis: str, rot=None, pos=None) -> float:
    k = 0 if axis == "x" else 1
    p = comp.pos if pos is None else pos
    return p[k] + anchor_offset(comp, spec, axis, rot)


def macro_anchor(member, spec, frame_offset, member_rot):
    """A member's anchor as an explicit anchor of its macro: ``frame_offset`` is the
    member's position relative to the macro centre (macro unrotated) and
    ``member_rot`` its rotation in the macro frame. An edge anchor keeps its board
    direction: the macro's rotation turns the member box, whose edge is then taken."""
    local = anchor_local(member, spec)
    dx, dy = frame_offset
    if local[0] == "point":
        px, py = rotate_point(local[1][0], local[1][1], member_rot)
        return {"point": [dx + px, dy + py]}
    b = rotate_box(local[1], member_rot)
    return {"box": [dx + b[0], dy + b[1], dx + b[2], dy + b[3]], "edge": local[2]}


def macro_bodies(member, con, frame_offset, member_rot) -> List[List[float]]:
    """A member's bodies for ``con`` as bodies of its macro (macro frame, unrotated)."""
    dx, dy = frame_offset
    out = []
    for b in bodies(member, con):
        r = rotate_box(b, member_rot)
        out.append([dx + r[0], dy + r[1], dx + r[2], dy + r[3]])
    return out


# ------------------------------------------------------------------ areas


def _point_in_polygon(x, y, poly) -> bool:
    """Inside or on the boundary of a simple polygon."""
    inside = False
    n = len(poly)
    for i in range(n):
        (x1, y1), (x2, y2) = poly[i], poly[(i + 1) % n]
        # On the segment (boundary counts as inside).
        cross = (x2 - x1) * (y - y1) - (y2 - y1) * (x - x1)
        if (
            abs(cross) <= 1e-9 * max(1.0, abs(x2 - x1) + abs(y2 - y1))
            and min(x1, x2) - 1e-9 <= x <= max(x1, x2) + 1e-9
            and min(y1, y2) - 1e-9 <= y <= max(y1, y2) + 1e-9
        ):
            return True
        if (y1 > y) != (y2 > y):
            if x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
                inside = not inside
    return inside


def _segment_enters_open_box(a, b, box) -> bool:
    """True when segment a-b meets the open interior of ``box`` (Liang-Barsky)."""
    x0, y0, x1, y1 = box
    eps = 1e-9
    t0, t1 = 0.0, 1.0
    dx, dy = b[0] - a[0], b[1] - a[1]
    for p, q in ((-dx, a[0] - (x0 + eps)), (dx, (x1 - eps) - a[0])):
        if abs(p) < 1e-15:
            if q <= 0:
                return False
        else:
            t = q / p
            if p < 0:
                t0 = max(t0, t)
            else:
                t1 = min(t1, t)
    for p, q in ((-dy, a[1] - (y0 + eps)), (dy, (y1 - eps) - a[1])):
        if abs(p) < 1e-15:
            if q <= 0:
                return False
        else:
            t = q / p
            if p < 0:
                t0 = max(t0, t)
            else:
                t1 = min(t1, t)
    return t0 < t1


def _seg_dist2(px, py, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    ll = dx * dx + dy * dy
    t = 0.0 if ll == 0 else max(0.0, min(1.0, ((px - a[0]) * dx + (py - a[1]) * dy) / ll))
    ex, ey = a[0] + t * dx - px, a[1] + t * dy - py
    return ex * ex + ey * ey


class Area:
    """An allowed area: the union of rectangle and polygon pieces (board mm)."""

    def __init__(self, pieces: Sequence[Tuple[str, object]]):
        self.pieces = [
            (kind, tuple(float(v) for v in data) if kind == "rect" else tuple(map(tuple, data)))
            for kind, data in pieces
        ]
        xs, ys = [], []
        for kind, data in self.pieces:
            if kind == "rect":
                xs += [data[0], data[2]]
                ys += [data[1], data[3]]
            else:
                xs += [p[0] for p in data]
                ys += [p[1] for p in data]
        self.bbox = (min(xs), min(ys), max(xs), max(ys))
        self._raster = None

    @classmethod
    def from_params(cls, params) -> "Area":
        pieces = []
        for a in params["areas"]:
            if "rect" in a:
                pieces.append(("rect", a["rect"]))
            else:
                pieces.append(("polygon", [tuple(p) for p in a["polygon"]]))
        return cls(pieces)

    @property
    def rect(self) -> Optional[Box]:
        """The rectangle when the area is exactly one rectangle, else None."""
        if len(self.pieces) == 1 and self.pieces[0][0] == "rect":
            return self.pieces[0][1]
        return None

    def _piece_contains(self, piece, box) -> bool:
        kind, data = piece
        if kind == "rect":
            return (
                box[0] >= data[0] - CHECK_EPS_MM
                and box[1] >= data[1] - CHECK_EPS_MM
                and box[2] <= data[2] + CHECK_EPS_MM
                and box[3] <= data[3] + CHECK_EPS_MM
            )
        corners = [(box[i], box[j]) for i in (0, 2) for j in (1, 3)]
        if not all(_point_in_polygon(x, y, data) for x, y in corners):
            return False
        n = len(data)
        for i in range(n):
            vx, vy = data[i]
            if box[0] + 1e-9 < vx < box[2] - 1e-9 and box[1] + 1e-9 < vy < box[3] - 1e-9:
                return False
            if _segment_enters_open_box(data[i], data[(i + 1) % n], box):
                return False
        return True

    def contains(self, box: Box) -> bool:
        """``box`` lies inside the area: exact for one piece and for a union whose
        pieces have only axis-parallel edges; along a sloped edge of a union the
        raster refuses the cells the edge cuts."""
        if any(self._piece_contains(p, box) for p in self.pieces):
            return True
        if len(self.pieces) > 1:
            return bool(self.raster_contains([box[0]], [box[1]], [box[2]], [box[3]])[0])
        return False

    # ---- raster (polygons and unions in the legalizer; unions in the check)

    def raster(self):
        """``(xs, ys, inside)``: grid lines and the cells between them. The grid lines
        are every piece's own coordinates (rectangle edges, polygon vertices), plus a
        :data:`REGION_CELL_MM` grid when a polygon has a sloped edge, so a cell lies
        wholly inside or wholly outside each piece unless a sloped edge cuts it. A cell
        is inside when the union covers it; a cell a sloped edge cuts is outside."""
        if self._raster is None:
            import numpy as np

            xs, ys, sloped = set(), set(), False
            for kind, data in self.pieces:
                if kind == "rect":
                    xs.update((data[0], data[2]))
                    ys.update((data[1], data[3]))
                    continue
                for (x1, y1), (x2, y2) in zip(data, data[1:] + data[:1]):
                    xs.add(x1)
                    ys.add(y1)
                    sloped = sloped or (x1 != x2 and y1 != y2)
            if sloped:
                c = REGION_CELL_MM
                x0, y0, x1, y1 = self.bbox
                xs.update(np.arange(math.ceil(x0 / c) * c, x1, c).tolist())
                ys.update(np.arange(math.ceil(y0 / c) * c, y1, c).tolist())
            xs, ys = _grid_lines(xs), _grid_lines(ys)
            inside = np.zeros((len(ys) - 1, len(xs) - 1), dtype=bool)
            for piece in self.pieces:
                inside |= _piece_cells(piece, xs, ys)
            self._raster = (xs, ys, inside)
        return self._raster

    def raster_contains(self, x0s, y0s, x1s, y1s):
        """Vectorized: each box (arrays of edges, any shape) lies on inside cells only."""
        import numpy as np

        xs, ys, inside = self.raster()
        ny, nx = inside.shape
        eps = CHECK_EPS_MM
        x0s, y0s = np.asarray(x0s, dtype=float), np.asarray(y0s, dtype=float)
        x1s, y1s = np.asarray(x1s, dtype=float), np.asarray(y1s, dtype=float)
        # Cells [c0, c1) x [r0, r1) are the ones the box's interior meets.
        c0 = np.searchsorted(xs, x0s + eps, side="right") - 1
        c1 = np.searchsorted(xs, x1s - eps, side="left")
        r0 = np.searchsorted(ys, y0s + eps, side="right") - 1
        r1 = np.searchsorted(ys, y1s - eps, side="left")
        within = (c0 >= 0) & (r0 >= 0) & (c1 <= nx) & (r1 <= ny) & (c1 > c0) & (r1 > r0)
        integ = np.zeros((ny + 1, nx + 1), dtype=np.int64)
        integ[1:, 1:] = np.cumsum(np.cumsum(~inside, axis=0, dtype=np.int64), axis=1)
        c0c, c1c = np.clip(c0, 0, nx), np.clip(c1, 0, nx)
        r0c, r1c = np.clip(r0, 0, ny), np.clip(r1, 0, ny)
        bad = integ[r1c, c1c] - integ[r0c, c1c] - integ[r1c, c0c] + integ[r0c, c0c]
        return within & (bad == 0)

    # ---- distances (soft penalty, global-placement mirror)

    def dist2(self, xs, ys):
        """Vectorized squared distance of points to the area (0 inside)."""
        import numpy as np

        xs, ys = np.asarray(xs, dtype=float), np.asarray(ys, dtype=float)
        best = np.full(np.broadcast(xs, ys).shape, np.inf)
        for kind, data in self.pieces:
            if kind == "rect":
                dx = np.maximum(np.maximum(data[0] - xs, 0.0), xs - data[2])
                dy = np.maximum(np.maximum(data[1] - ys, 0.0), ys - data[3])
                d = dx * dx + dy * dy
            else:
                d = np.full(best.shape, np.inf)
                n = len(data)
                inside = np.zeros(best.shape, dtype=bool)
                for i in range(n):
                    (x1, y1), (x2, y2) = data[i], data[(i + 1) % n]
                    ex, ey = x2 - x1, y2 - y1
                    ll = ex * ex + ey * ey
                    t = np.clip(((xs - x1) * ex + (ys - y1) * ey) / (ll or 1.0), 0.0, 1.0)
                    d = np.minimum(d, (x1 + t * ex - xs) ** 2 + (y1 + t * ey - ys) ** 2)
                    crosses = (y1 > ys) != (y2 > ys)
                    with np.errstate(divide="ignore", invalid="ignore"):
                        xi = x1 + (ys - y1) * ex / np.where(ey == 0, 1.0, ey)
                    inside ^= crosses & (xs < xi)
                d = np.where(inside, 0.0, d)
            best = np.minimum(best, d)
        return best

    def protrusion2(self, box: Box) -> float:
        """Sum over the box corners of the squared distance to the area."""
        xs = [box[0], box[2], box[0], box[2]]
        ys = [box[1], box[1], box[3], box[3]]
        return float(self.dist2(xs, ys).sum())


def _grid_lines(values):
    """Sorted grid lines, values closer than CHECK_EPS_MM merged."""
    import numpy as np

    out = []
    for v in sorted(values):
        if not out or v - out[-1] > CHECK_EPS_MM:
            out.append(v)
    return np.asarray(out, dtype=float)


def _inside_polygon(px, py, poly):
    """Vectorized even-odd test of points strictly off the boundary."""
    import numpy as np

    inside = np.zeros(np.broadcast(px, py).shape, dtype=bool)
    for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1]):
        if y1 == y2:
            continue
        crosses = (y1 > py) != (y2 > py)
        inside ^= crosses & (px < x1 + (py - y1) * (x2 - x1) / (y2 - y1))
    return inside


def _piece_cells(piece, xs, ys):
    """The raster cells (grid lines ``xs``, ``ys``) lying wholly inside one piece.

    A rectangle's edges and a polygon's vertices are grid lines, so only a sloped
    polygon edge can cross a cell's interior: such cells are refused, every other
    cell is wholly inside or wholly outside and is decided by its centre."""
    import numpy as np

    kind, data = piece
    eps = CHECK_EPS_MM
    if kind == "rect":
        cols = (xs[:-1] >= data[0] - eps) & (xs[1:] <= data[2] + eps)
        rows = (ys[:-1] >= data[1] - eps) & (ys[1:] <= data[3] + eps)
        return rows[:, None] & cols[None, :]
    cx = (xs[:-1] + xs[1:]) / 2.0
    cy = (ys[:-1] + ys[1:]) / 2.0
    cells = _inside_polygon(cx[None, :], cy[:, None], list(data))
    for (x1, y1), (x2, y2) in zip(data, data[1:] + data[:1]):
        if x1 == x2 or y1 == y2:
            continue  # on grid lines: never inside a cell
        # Rows whose open band the edge crosses, and the edge's x span in each.
        lo, hi = min(y1, y2), max(y1, y2)
        r0 = max(0, int(np.searchsorted(ys, lo, side="right")) - 1)
        r1 = min(len(ys) - 1, int(np.searchsorted(ys, hi, side="left")))
        if r1 <= r0:
            continue
        b0 = np.maximum(ys[r0:r1], lo)
        b1 = np.minimum(ys[r0 + 1 : r1 + 1], hi)
        keep = b1 - b0 > eps
        xa = x1 + (b0 - y1) * (x2 - x1) / (y2 - y1)
        xb = x1 + (b1 - y1) * (x2 - x1) / (y2 - y1)
        left, right = np.minimum(xa, xb), np.maximum(xa, xb)
        cut = (xs[None, :-1] < right[:, None] - eps) & (xs[None, 1:] > left[:, None] + eps)
        cells[r0:r1] &= ~(cut & keep[:, None])
    return cells


_AREAS: Dict[str, "Area"] = {}


def area_of(con) -> Area:
    """The :class:`Area` of a region constraint, cached by content (with its raster)
    across constraint copies and recompiles."""
    import json

    key = json.dumps(con.params["areas"], sort_keys=True)
    hit = _AREAS.get(key)
    if hit is None:
        if len(_AREAS) > 256:
            _AREAS.clear()
        hit = _AREAS[key] = Area.from_params(con.params)
    return hit


# ------------------------------------------------------------------ checks


def _by_ref(parts):
    """{ref: component} of a graph or of an iterable of components."""
    return {c.ref: c for c in getattr(parts, "components", parts)}


def region_offenders(parts, constraints, refs=None) -> List[str]:
    """Refs of hard regions whose bodies are not inside their area (``parts``: a graph
    or components; refs not among them are skipped)."""
    comps = _by_ref(parts)
    bad = []
    for con in region_rules(constraints):
        if con.enforcement is not Enforcement.HARD:
            continue
        area = area_of(con)
        for ref in con.refs:
            comp = comps.get(ref)
            if comp is None or (refs is not None and ref not in refs):
                continue
            if not all(area.contains(b) for b in placed_boxes(comp, con)):
                bad.append(ref)
    return bad


def align_spread(parts, con) -> Optional[float]:
    """The largest anchor spread of ``con`` (None with fewer than two present refs)."""
    comps = _by_ref(parts)
    values = [
        anchor_value(comps[r], anchor_spec(con, r), con.params["axis"])
        for r in con.refs
        if r in comps
    ]
    return max(values) - min(values) if len(values) >= 2 else None


def align_offenders(parts, constraints, refs=None) -> List[str]:
    """Refs of hard aligns whose anchor spread exceeds ``tol_mm``."""
    comps = _by_ref(parts)
    bad = []
    for con in align_rules(constraints):
        if con.enforcement is not Enforcement.HARD:
            continue
        if refs is not None and not set(refs) & set(con.refs):
            continue
        spread = align_spread(comps.values(), con)
        if spread is not None and spread > con.params["tol_mm"] + CHECK_EPS_MM:
            bad.extend(r for r in con.refs if r in comps)
    return bad


def violations(parts, constraints) -> List[str]:
    """Refs violating a hard region or a hard align (empty without either). With a
    subset of the parts (a row sample) only the relations among them count."""
    if not declared(constraints):
        return []
    return sorted(
        set(region_offenders(parts, constraints)) | set(align_offenders(parts, constraints))
    )


def ref_ok(parts, constraints, ref) -> bool:
    """``ref``'s hard regions and the hard aligns it belongs to hold."""
    return not region_offenders(parts, constraints, {ref}) and not align_offenders(
        parts, constraints, {ref}
    )


def align_snap(parts, constraints, comp) -> Dict[int, float]:
    """``{axis index: centre coordinate}`` putting ``comp``'s anchor on the line of each
    hard align it belongs to (the middle of the other members' anchors)."""
    comps = _by_ref(parts)
    out = {}
    for con in align_rules(constraints):
        if con.enforcement is not Enforcement.HARD or comp.ref not in con.refs:
            continue
        axis = con.params["axis"]
        values = [
            anchor_value(comps[r], anchor_spec(con, r), axis)
            for r in con.refs
            if r != comp.ref and r in comps
        ]
        if values:
            off = anchor_offset(comp, anchor_spec(con, comp.ref), axis)
            out[0 if axis == "x" else 1] = (max(values) + min(values)) / 2.0 - off
    return out


def soft_penalty(parts, constraints, comp, pos) -> float:
    """The soft regions' and soft aligns' penalty of ``comp`` at ``pos`` (others as
    they are): weight x squared corner protrusion, weight x squared distance of the
    anchor from the other members' mean."""
    comps = _by_ref(parts)
    total = 0.0
    for con in region_rules(constraints):
        if con.enforcement is Enforcement.HARD or comp.ref not in con.refs:
            continue
        area = area_of(con)
        total += (con.weight or 1.0) * sum(
            area.protrusion2(b) for b in placed_boxes(comp, con, pos=pos)
        )
    for con in align_rules(constraints):
        if con.enforcement is Enforcement.HARD or comp.ref not in con.refs:
            continue
        axis = con.params["axis"]
        values = [
            anchor_value(comps[r], anchor_spec(con, r), axis)
            for r in con.refs
            if r != comp.ref and r in comps
        ]
        if values:
            mine = anchor_value(comp, anchor_spec(con, comp.ref), axis, pos=pos)
            total += (con.weight or 1.0) * (mine - sum(values) / len(values)) ** 2
    return total


def soft_refs(constraints) -> frozenset:
    """Refs in a soft region or a soft align (empty without either)."""
    if not declared(constraints):
        return frozenset()
    return frozenset(
        r
        for con in region_rules(constraints) + align_rules(constraints)
        if con.enforcement is not Enforcement.HARD
        for r in con.refs
    )


def soft_total(parts, constraints) -> float:
    """The whole board's soft region and soft align penalty, for the movers that rank
    whole placements (batch relocation, the elastic mesh, feedback children, native
    loop trials): weight x squared corner protrusion of each member, weight x squared
    deviation of each member's anchor from the members' mean. 0 without either."""
    if not soft_refs(constraints):
        return 0.0
    comps = _by_ref(parts)
    total = 0.0
    for con in region_rules(constraints):
        if con.enforcement is Enforcement.HARD:
            continue
        area = area_of(con)
        for ref in con.refs:
            if ref in comps:
                total += (con.weight or 1.0) * sum(
                    area.protrusion2(b) for b in placed_boxes(comps[ref], con)
                )
    for con in align_rules(constraints):
        if con.enforcement is Enforcement.HARD:
            continue
        axis = con.params["axis"]
        values = [anchor_value(comps[r], anchor_spec(con, r), axis) for r in con.refs if r in comps]
        if len(values) >= 2:
            mean = sum(values) / len(values)
            total += (con.weight or 1.0) * sum((v - mean) ** 2 for v in values)
    return total


# ------------------------------------------------------------------ feasibility

CARDINAL = (0.0, 90.0, 180.0, 270.0)


def _fmt_interval(lo, hi):
    return "[%.3f, %.3f]" % (lo, hi)


def check_feasible(graph, constraints, width, height, orient=True, trials_fixed=False):
    """Refuse, before placement, a hard region or align that no placement can meet:
    raise :class:`pnr.constraints.ConstraintError` naming the constraint, the part
    and what it conflicts with (the outline, a keep-out, an edge band, a fixed
    part, another member's region). Only necessary conditions are tested (each part
    alone, rectangle bounds of polygons, rotation ranges merged), so a design this
    passes can still fail in the legalizer, never the other way round. Also refuses
    an align anchor naming a pad the part does not have. Row trials (temporary
    ``row_trial`` poses) count as movable, unless ``trials_fixed`` (the row sampler
    screening a trial: :func:`pnr.place.rows.sample_constraints`)."""
    from pnr.constraints import ConstraintError

    from .geometry import hard_edge_bands, resolve_fixed_poses, resolve_hard_rotations

    if not declared(constraints):
        return
    comps = {c.ref: c for c in graph.components}
    for con in align_rules(constraints):
        for ref in con.refs:
            if ref in comps:
                try:
                    anchor_local(comps[ref], anchor_spec(con, ref))
                except ValueError as error:
                    raise ConstraintError("align %r: %s" % (con.name, error)) from None
    trial = set()
    if not trials_fixed:
        trial = {
            r
            for c in constraints.constraints
            if c.kind == "fixed" and c.params.get("row_trial")
            for r in c.refs
        }
    poses = {r: p for r, p in resolve_fixed_poses(graph, constraints).items() if r not in trial}
    locked = set(constraints.locked_refs) - trial
    hard_rot = resolve_hard_rotations(constraints)
    bands = hard_edge_bands(constraints)
    keepouts = []
    for c in constraints.constraints:
        if c.kind == "keepout" and c.params.get("polygon"):
            xs = [float(p[0]) for p in c.params["polygon"]]
            ys = [float(p[1]) for p in c.params["polygon"]]
            keepouts.append((c.name or "keepout", (min(xs), min(ys), max(xs), max(ys))))
    hard_regions = [c for c in region_rules(constraints) if c.enforcement is Enforcement.HARD]
    hard_aligns = [c for c in align_rules(constraints) if c.enforcement is Enforcement.HARD]

    def pose(ref):
        """(pos, rot) of a part that cannot move, else None."""
        comp = comps[ref]
        if ref in poses:
            return poses[ref], hard_rot.get(ref, comp.rot)
        if ref in locked:
            return comp.pos, comp.rot
        return None

    def rotations(ref):
        if ref in hard_rot:
            return [hard_rot[ref]]
        return list(CARDINAL) if orient else [comps[ref].rot]

    def centres(ref, rot):
        """``((x_lo, x_hi, y_lo, y_hi), None)``: the centres at which ``ref`` (at ``rot``)
        lies in the outline, its hard regions' bounds and its edge band, or ``(None,
        reason)`` naming what empties that box."""
        comp = comps[ref]
        w, h = comp.courtyard
        if int(round(rot)) % 180 == 90:
            w, h = h, w
        box = [w / 2, width - w / 2, h / 2, height - h / 2]
        if box[0] > box[1] + CHECK_EPS_MM or box[2] > box[3] + CHECK_EPS_MM:
            return None, "the board outline"
        for con in hard_regions:
            if ref not in con.refs:
                continue
            area = area_of(con)
            x0, y0, x1, y1 = area.rect or area.bbox
            for b in bodies(comp, con):
                r = rotate_box(b, rot)
                box = [
                    max(box[0], x0 - r[0]),
                    min(box[1], x1 - r[2]),
                    max(box[2], y0 - r[1]),
                    min(box[3], y1 - r[3]),
                ]
            if box[0] > box[1] + CHECK_EPS_MM or box[2] > box[3] + CHECK_EPS_MM:
                return None, "region %r within the board outline" % con.name
        if ref in bands:
            edge, tol = bands[ref]
            k, lo, hi = {
                "south": (2, -math.inf, tol + h / 2),
                "north": (2, height - tol - h / 2, math.inf),
                "west": (0, -math.inf, tol + w / 2),
                "east": (0, width - tol - w / 2, math.inf),
            }[edge]
            box[k], box[k + 1] = max(box[k], lo), min(box[k + 1], hi)
            if box[k] > box[k + 1] + CHECK_EPS_MM:
                return None, "its hard edge_align (%s, %g mm)" % (edge, tol)
        if keepouts and any(ref in c.refs for c in hard_regions):
            # The centres are lost when the keep-outs, grown by half the courtyard
            # (less a hair: touching is legal), cover all of them.
            grown = [
                (
                    "rect",
                    (
                        k[0] - w / 2 + 1e-4,
                        k[1] - h / 2 + 1e-4,
                        k[2] + w / 2 - 1e-4,
                        k[3] + h / 2 - 1e-4,
                    ),
                )
                for _, k in keepouts
                if k[2] - k[0] + w > 2e-4 and k[3] - k[1] + h > 2e-4
            ]
            if grown and Area(grown).contains((box[0], box[2], box[1], box[3])):
                names = ", ".join(sorted({repr(n) for n, _ in keepouts}))
                return None, "keep-out %s" % names
        return (box[0], box[1], box[2], box[3]), None

    reach = {}  # ref -> [(rot, centre box)] of a movable part with a hard relation
    involved = {r for c in hard_regions + hard_aligns for r in c.refs if r in comps}
    for ref in sorted(involved):
        fixed = pose(ref)
        if fixed is not None:
            (x, y), rot = fixed
            for con in hard_regions:
                if ref not in con.refs:
                    continue
                area = area_of(con)
                for b in placed_boxes(comps[ref], con, rot=rot, pos=(x, y)):
                    if not area.contains(b) and area.protrusion2(b) > 1e-9:
                        raise ConstraintError(
                            "region %r: %s is fixed at (%.3f, %.3f) and lies outside it"
                            % (con.name, ref, x, y)
                        )
            continue
        options, reasons = [], []
        for rot in rotations(ref):
            box, reason = centres(ref, rot)
            if box is None:
                reasons.append(reason)
            else:
                options.append((rot, box))
        if not options:
            names = ["region %r" % c.name for c in hard_regions if ref in c.refs]
            names += ["align %r" % c.name for c in hard_aligns if ref in c.refs]
            what = names[0]
            raise ConstraintError(
                "%s: %s (%.2f x %.2f mm) fits nowhere at any allowed rotation; it conflicts "
                "with %s"
                % (what, ref, comps[ref].courtyard[0], comps[ref].courtyard[1], reasons[0])
            )
        reach[ref] = options

    for con in hard_aligns:
        axis = con.params["axis"]
        k = 0 if axis == "x" else 1
        tol = con.params["tol_mm"]
        spans = {}
        for ref in con.refs:
            if ref not in comps:
                continue
            spec = anchor_spec(con, ref)
            fixed = pose(ref)
            if fixed is not None:
                value = anchor_value(comps[ref], spec, axis, rot=fixed[1], pos=fixed[0])
                spans[ref] = (value, value, True)
                continue
            lo = min(b[2 * k] + anchor_offset(comps[ref], spec, axis, rot) for rot, b in reach[ref])
            hi = max(
                b[2 * k + 1] + anchor_offset(comps[ref], spec, axis, rot) for rot, b in reach[ref]
            )
            spans[ref] = (lo, hi, False)
        if len(spans) < 2:
            continue
        fixed = {r: v[0] for r, v in spans.items() if v[2]}
        if len(fixed) > 1 and max(fixed.values()) - min(fixed.values()) > tol + CHECK_EPS_MM:
            ends = sorted(fixed, key=fixed.get)
            raise ConstraintError(
                "align %r: its fixed members %s and %s are %.3f mm apart on %s, more than "
                "tol_mm %g"
                % (con.name, ends[0], ends[-1], fixed[ends[-1]] - fixed[ends[0]], axis, tol)
            )
        high = max(spans, key=lambda r: spans[r][0])  # the member whose span starts last
        low = min(spans, key=lambda r: spans[r][1])  # the member whose span ends first
        gap = spans[high][0] - spans[low][1]
        if gap > tol + CHECK_EPS_MM:

            def why(ref):
                if spans[ref][2]:
                    return "%s is fixed with its anchor at %s = %.3f" % (ref, axis, spans[ref][0])
                limits = [c.name for c in hard_regions if ref in c.refs]
                return "%s reaches %s in %s%s" % (
                    ref,
                    axis,
                    _fmt_interval(spans[ref][0], spans[ref][1]),
                    (" (region %r)" % limits[0]) if limits else "",
                )

            raise ConstraintError(
                "align %r: no line within tol_mm %g reaches every member: %s, %s"
                % (con.name, tol, why(low), why(high))
            )


# ------------------------------------------------------------------ exact snap


def snap_aligns(placed, constraints, clearance=0.0, pad_edge=None, size=None):
    """After legalization, put the anchors of each hard align whose spread exceeds its
    ``tol_mm`` (one under the legalizer's band floor, e.g. 0) on one line, best effort.
    An align already within ``tol_mm`` is left alone: a snap would move parts off the
    legalizer's grid, which the router's grid follows.

    Members move along the aligned axis only, each by at most the align's current
    spread; the line is the members' anchor (a fixed member's, if any) that needs
    the smallest largest move, then the others in turn. A line is kept only when
    every move passes :func:`pnr.place.metrics.translation_checker` (overlap,
    outline, keep-outs, regions, edge bands, groups, the other aligns), brings no
    other part within ``clearance`` that was not within it before and, with
    ``pad_edge`` (PNR_PAD_EDGE_CLEARANCE=1, ``size`` = (width, height)), keeps the
    pads' edge rule. Returns the refs moved; an align that cannot be snapped keeps
    its legal spread (within ``max(tol_mm, the legalizer's band floor)``)."""
    import copy

    from pnr.constraints import Constraint

    from .geometry import placement_rects, resolve_fixed_poses
    from .metrics import pad_edge_violations, translation_checker

    rules = [c for c in align_rules(constraints) if c.enforcement is Enforcement.HARD]
    if not rules:
        return []
    comps = {c.ref: c for c in placed.components}
    pinned = set(resolve_fixed_poses(placed, constraints)) | set(constraints.locked_refs)
    # The checker holds every hard align to its current spread (the legalizer's band
    # may exceed a tol_mm under its floor): a move may only narrow an align.
    relaxed = copy.copy(constraints)
    relaxed.constraints = [
        (
            c
            if c.kind != "align" or c.enforcement is not Enforcement.HARD
            else Constraint(
                c.kind,
                c.enforcement,
                c.refs,
                dict(
                    c.params,
                    tol_mm=max(c.params["tol_mm"], (align_spread(comps.values(), c) or 0.0)),
                ),
                c.weight,
                c.name,
            )
        )
        for c in constraints.constraints
    ]

    def near(comp):
        mine = placement_rects(comp)
        return {
            other.ref
            for other in placed.components
            if other.ref != comp.ref
            for other_side, other_rect in placement_rects(other)
            for side, rect in mine
            if side == other_side and rect.overlaps(other_rect, gap=clearance)
        }

    moved = []
    for con in rules:
        axis = con.params["axis"]
        k = 0 if axis == "x" else 1
        members = [r for r in con.refs if r in comps]
        values = {r: anchor_value(comps[r], anchor_spec(con, r), axis) for r in members}
        if len(values) < 2:
            continue
        if max(values.values()) - min(values.values()) <= con.params["tol_mm"] + CHECK_EPS_MM:
            continue  # within its tolerance: no snap needed
        fixed = sorted({round(values[r], 9) for r in members if r in pinned})
        if len(fixed) > 1:
            continue
        lines = fixed or sorted(
            set(values.values()), key=lambda v: (max(abs(v - u) for u in values.values()), v)
        )
        for line in lines:
            saved = {r: comps[r].pos for r in members}
            done = []
            for ref in members:
                delta = line - values[ref]
                if abs(delta) <= 1e-9:
                    continue
                try:
                    legal = translation_checker(placed, relaxed)
                except ValueError:  # the legalized board is not legal: leave it
                    return moved
                comp = comps[ref]
                before = near(comp) if clearance > 0 else set()
                point = list(comp.pos)
                point[k] += delta
                comp.pos = tuple(point)
                ok = legal(comp) and (clearance <= 0 or near(comp) <= before)
                if ok and pad_edge is not None:
                    ok = not pad_edge_violations(
                        placed, size[0], size[1], pad_edge, exclude=set(comps) - {ref}
                    )
                if not ok:
                    break
                done.append(ref)
            else:
                moved.extend(done)
                break
            for r, pos in saved.items():
                comps[r].pos = pos
    return moved


# ------------------------------------------------------------------ legalizer


class LegalizeRules:
    """Regions and aligns for :func:`pnr.place.legalize.legalize`.

    Built from the hard and soft ``region``/``align`` constraints and the incoming
    (global-placement) poses. Hard relations bound the slot centre: a static box per
    rotation for a rectangle region, a raster mask for a polygon or union, and a
    dynamic band from the members already placed for an align (``tol_mm``, at least
    :data:`ALIGN_BAND_FLOOR` slot pitches), narrowed to where the members not yet
    placed can still reach. Soft relations add a candidate cost. Each aligned
    member's target on the aligned axis is the line: the centre of the placed
    members' anchors, else the median of the global anchors (moved onto the stretch
    every member can reach).
    """

    def __init__(self, regions, aligns, components, grid_mm=0.25, limits=None, turns=None):
        # A tol_mm under the band floor is met by snap_aligns after legalization.
        self.band_floor = ALIGN_BAND_FLOOR * grid_mm
        # ``limits(comp, rot)``: the slot centres a part can take on an empty board
        # (the legalizer's grid, edge band and rectangle regions), over ``turns(comp)``,
        # the rotations it tries: where an align member not yet placed can reach.
        self.limits = limits
        self.turns = turns or (lambda comp: [comp.rot])
        self._reach = {}
        self.regions = [(con, area_of(con)) for con in regions or ()]
        self.aligns = list(aligns or ())
        self.region_of: Dict[str, list] = {}
        self.align_of: Dict[str, list] = {}
        for con, area in self.regions:
            for ref in con.refs:
                self.region_of.setdefault(ref, []).append((con, area))
        for con in self.aligns:
            for ref in con.refs:
                self.align_of.setdefault(ref, []).append(con)
        self._by_ref = {c.ref: c for c in components}
        self._static = {}
        # Each align's start line: the median of the incoming anchors (lazy: line()).
        self.lines = {}
        for index, con in enumerate(self.aligns):
            values = sorted(
                anchor_value(self._by_ref[r], anchor_spec(con, r), con.params["axis"])
                for r in con.refs
                if r in self._by_ref
            )
            if values:
                mid = len(values) // 2
                self.lines[index] = (
                    values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2.0
                )
        self._clamped = set()

    def line(self, index) -> Optional[float]:
        """Align ``index``'s start line, moved onto the stretch every member can reach
        (an edge band, a region) when there is one."""
        if index not in self.lines:
            return None
        if index not in self._clamped:
            self._clamped.add(index)
            con = self.aligns[index]
            spans = [self.reach(self._by_ref[r], con) for r in con.refs if r in self._by_ref]
            spans = [x for x in spans if x is not None]
            if spans:
                lo, hi = max(a for a, _ in spans), min(b for _, b in spans)
                if lo <= hi:
                    self.lines[index] = min(max(self.lines[index], lo), hi)
        return self.lines[index]

    @property
    def hard(self) -> bool:
        return any(c.enforcement is Enforcement.HARD for c, _ in self.regions) or any(
            c.enforcement is Enforcement.HARD for c in self.aligns
        )

    def applies(self, ref) -> bool:
        return ref in self.region_of or ref in self.align_of

    def edges(self):
        """Chain pairs of every align, for the legalizer's block ordering."""
        return [(a, b) for con in self.aligns for a, b in zip(con.refs, con.refs[1:])]

    def static_box(self, comp, rot=None, bounds=False) -> Optional[Box]:
        """Slot-centre box (x_lo, x_hi, y_lo, y_hi) of the hard rectangle regions at
        ``rot`` (default ``comp.rot``; None when none applies). With ``bounds`` a
        polygon or union region counts by its bounding box."""
        rot = comp.rot if rot is None else rot
        key = (comp.ref, round(rot % 360, 6), bounds)
        if key in self._static:
            return self._static[key]
        box = None
        for con, area in self.region_of.get(comp.ref, ()):
            if con.enforcement is not Enforcement.HARD or (area.rect is None and not bounds):
                continue
            x0, y0, x1, y1 = area.rect or area.bbox
            for b in bodies(comp, con):
                r = rotate_box(b, rot)
                part = (x0 - r[0], x1 - r[2], y0 - r[1], y1 - r[3])
                box = part if box is None else _intersect(box, part)
        self._static[key] = box
        return box

    def reach(self, comp, con) -> Optional[Tuple[float, float]]:
        """The anchor values on ``con``'s axis that ``comp`` can take before any other
        part is placed (``limits``), over the rotations the legalizer tries; None
        without ``limits`` or when nothing fits."""
        if self.limits is None:
            return None
        axis = con.params["axis"]
        key = (comp.ref, round(comp.rot % 360, 6), con.name, axis)
        if key in self._reach:
            return self._reach[key]
        k = 0 if axis == "x" else 1
        out = None
        for rot in self.turns(comp):
            box = self.limits(comp, rot)
            if box is None:
                continue
            off = anchor_offset(comp, anchor_spec(con, comp.ref), axis, rot)
            lo, hi = box[2 * k] + off, box[2 * k + 1] + off
            out = (lo, hi) if out is None else (min(out[0], lo), max(out[1], hi))
        self._reach[key] = out
        return out

    def mask(self, comp, bw, bh, g, shape):
        """Valid slot top-lefts (array of ``shape``) for the hard polygon/union regions
        of ``comp`` at its rotation, or None."""
        import numpy as np

        out = None
        for con, area in self.region_of.get(comp.ref, ()):
            if con.enforcement is not Enforcement.HARD or area.rect is not None:
                continue
            rows = np.arange(shape[0])[:, None]
            cols = np.arange(shape[1])[None, :]
            cx = np.broadcast_to((cols + bw / 2.0) * g, shape)
            cy = np.broadcast_to((rows + bh / 2.0) * g, shape)
            ok = np.ones(shape, dtype=bool)
            for b in bodies(comp, con):
                r = rotate_box(b, comp.rot)
                ok &= area.raster_contains(cx + r[0], cy + r[1], cx + r[2], cy + r[3])
            out = ok if out is None else out & ok
        return out

    def _placed_anchors(self, con, comp, placed):
        axis = con.params["axis"]
        return [
            anchor_value(placed[r], anchor_spec(con, r), axis)
            for r in con.refs
            if r != comp.ref and r in placed
        ]

    def align_box(self, comp, placed, members=None) -> Optional[Box]:
        """Slot-centre box from the hard aligns of ``comp``: within ``tol_mm`` of the
        members in ``placed`` ({ref: component}), and of the stretch each member not
        yet placed (``members``: {ref: component}) can still reach (reach()); None
        when neither bounds it."""
        box = None
        for con in self.align_of.get(comp.ref, ()):
            if con.enforcement is not Enforcement.HARD:
                continue
            values = self._placed_anchors(con, comp, placed)
            tol = max(con.params["tol_mm"], self.band_floor) - ALIGN_SLACK_MM
            off = anchor_offset(comp, anchor_spec(con, comp.ref), con.params["axis"])
            lo, hi = -math.inf, math.inf
            if values:
                lo, hi = max(values) - tol - off, min(values) + tol - off
            for ref in con.refs:
                other = (members or {}).get(ref)
                if other is None or ref == comp.ref or ref in placed:
                    continue
                span = self.reach(other, con)
                if span is not None:
                    lo, hi = max(lo, span[0] - tol - off), min(hi, span[1] + tol - off)
            if lo == -math.inf and hi == math.inf:
                continue
            inf = math.inf
            part = (lo, hi, -inf, inf) if con.params["axis"] == "x" else (-inf, inf, lo, hi)
            box = part if box is None else _intersect(box, part)
        return box

    def target(self, comp, placed, default):
        """The legalizer target of ``comp`` at its rotation: ``default`` with each
        aligned axis moved onto the line."""
        target = list(default)
        sums = {}
        for con in self.align_of.get(comp.ref, ()):
            axis = con.params["axis"]
            values = self._placed_anchors(con, comp, placed)
            if values:
                line = (max(values) + min(values)) / 2.0
            else:
                line = self.line(self.aligns.index(con))
                if line is None:
                    continue
            off = anchor_offset(comp, anchor_spec(con, comp.ref), axis)
            sums.setdefault(axis, []).append(line - off)
        for axis, values in sums.items():
            target[0 if axis == "x" else 1] = sum(values) / len(values)
        return tuple(target)

    def soft_cost(self, comp, placed, xs, ys):
        """Soft-relation cost at candidate centres (arrays), or None."""
        import numpy as np

        total = None
        for con, area in self.region_of.get(comp.ref, ()):
            if con.enforcement is Enforcement.HARD:
                continue
            cost = 0.0
            for b in bodies(comp, con):
                r = rotate_box(b, comp.rot)
                for dx in (r[0], r[2]):
                    for dy in (r[1], r[3]):
                        cost = cost + area.dist2(np.asarray(xs) + dx, np.asarray(ys) + dy)
            cost = (con.weight or 1.0) * cost
            total = cost if total is None else total + cost
        for con in self.align_of.get(comp.ref, ()):
            if con.enforcement is Enforcement.HARD:
                continue
            axis = con.params["axis"]
            values = self._placed_anchors(con, comp, placed)
            if values:
                line = sum(values) / len(values)
            else:
                line = self.line(self.aligns.index(con))
                if line is None:
                    continue
            off = anchor_offset(comp, anchor_spec(con, comp.ref), axis)
            coord = np.asarray(xs if axis == "x" else ys, dtype=float)
            cost = (con.weight or 1.0) * (coord + off - line) ** 2
            total = cost if total is None else total + cost
        return total

    def has_soft(self, ref) -> bool:
        return any(
            c.enforcement is not Enforcement.HARD for c, _ in self.region_of.get(ref, ())
        ) or any(c.enforcement is not Enforcement.HARD for c in self.align_of.get(ref, ()))


def _intersect(a: Box, b: Box) -> Box:
    """Intersection of two centre boxes (x_lo, x_hi, y_lo, y_hi)."""
    return (max(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), min(a[3], b[3]))


def legalize_kwargs(constraints, components) -> dict:
    """``legalize`` keyword arguments for the declared regions and aligns (empty
    without them, so other designs call the legalizer unchanged)."""
    regions, aligns = region_rules(constraints), align_rules(constraints)
    if not regions and not aligns:
        return {}
    return dict(regions=regions, aligns=aligns)


# ------------------------------------------------------------------ starts


def project_start(comp, rules_for_ref, xy, rot):
    """Project a sampled start ``xy`` (at ``rot``) into the hard regions of the ref
    (``rules_for_ref``: its region constraints). Draws no random numbers."""
    x, y = xy
    for con in rules_for_ref:
        if con.enforcement is not Enforcement.HARD:
            continue
        area = area_of(con)
        x0, y0, x1, y1 = area.rect or area.bbox
        for b in bodies(comp, con):
            r = rotate_box(b, rot)
            lo_x, hi_x = x0 - r[0], x1 - r[2]
            lo_y, hi_y = y0 - r[1], y1 - r[3]
            x = (lo_x + hi_x) / 2.0 if lo_x > hi_x else min(max(x, lo_x), hi_x)
            y = (lo_y + hi_y) / 2.0 if lo_y > hi_y else min(max(y, lo_y), hi_y)
    return [x, y]


# ------------------------------------------------------------------ global placement


def torch_dist2(area: Area, pts):
    """Squared distance of points ``pts`` (torch, ``(..., 2)``) to ``area``, 0 inside:
    differentiable outside; the inside test of a polygon is taken on the detached
    values."""
    import torch

    x, y = pts[..., 0], pts[..., 1]
    best = None
    for kind, data in area.pieces:
        if kind == "rect":
            dx = torch.clamp(data[0] - x, min=0.0) + torch.clamp(x - data[2], min=0.0)
            dy = torch.clamp(data[1] - y, min=0.0) + torch.clamp(y - data[3], min=0.0)
            d = dx * dx + dy * dy
        else:
            piece = Area([(kind, data)])
            outside = torch.as_tensor(
                piece.dist2(x.detach().numpy(), y.detach().numpy()) > 0, dtype=x.dtype
            )
            d = None
            n = len(data)
            for i in range(n):
                (x1, y1), (x2, y2) = data[i], data[(i + 1) % n]
                ex, ey = x2 - x1, y2 - y1
                ll = ex * ex + ey * ey or 1.0
                t = torch.clamp(((x - x1) * ex + (y - y1) * ey) / ll, 0.0, 1.0)
                s = (x1 + t * ex - x) ** 2 + (y1 + t * ey - y) ** 2
                d = s if d is None else torch.minimum(d, s)
            d = d * outside
        best = d if best is None else torch.minimum(best, d)
    return best


class GlobalTerms:
    """The global placer's region and align terms (:mod:`pnr.place.model`).

    Region: for each movable ref, the expectation over its rotation distribution of
    the summed squared distance of its body corners to the area. Align: the squared
    deviation of each member's expected anchor from the members' mean. Hard
    relations weigh :data:`GP_REGION_WEIGHT` / :data:`GP_ALIGN_WEIGHT`, soft ones
    their own weight. Built only when a design declares a region or an align."""

    def __init__(self, constraints, comps, idx, angles, movable):
        import torch

        self.regions = []
        self.aligns = []
        for con in region_rules(constraints):
            area = area_of(con)
            weight = GP_REGION_WEIGHT if con.enforcement is Enforcement.HARD else con.weight
            for ref in con.refs:
                if ref not in idx or not movable[idx[ref]]:
                    continue
                comp = comps[idx[ref]]
                corners = []
                for angle in angles:
                    pts = []
                    for b in bodies(comp, con):
                        r = rotate_box(b, angle)
                        pts += [(r[0], r[1]), (r[2], r[1]), (r[0], r[3]), (r[2], r[3])]
                    corners.append(pts)
                self.regions.append(
                    (idx[ref], torch.tensor(corners, dtype=torch.float32), area, float(weight))
                )
        for con in align_rules(constraints):
            members = [r for r in con.refs if r in idx]
            if len(members) < 2:
                continue
            axis = con.params["axis"]
            offsets = [
                [anchor_offset(comps[idx[r]], anchor_spec(con, r), axis, a) for a in angles]
                for r in members
            ]
            weight = GP_ALIGN_WEIGHT if con.enforcement is Enforcement.HARD else con.weight
            self.aligns.append(
                (
                    torch.tensor([idx[r] for r in members], dtype=torch.long),
                    torch.tensor(offsets, dtype=torch.float32),
                    0 if axis == "x" else 1,
                    float(weight),
                )
            )

    def loss(self, pos, probs):
        total = None
        for i, corners, area, weight in self.regions:
            d2 = torch_dist2(area, pos[i] + corners).sum(1)  # (4,)
            term = weight * (probs[i] * d2).sum()
            total = term if total is None else total + term
        for members, offsets, k, weight in self.aligns:
            anchors = pos[members, k] + (probs[members] * offsets).sum(1)
            term = weight * ((anchors - anchors.mean()) ** 2).sum()
            total = term if total is None else total + term
        return total
