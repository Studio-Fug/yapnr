"""Placement regions and alignments: the ``region`` and ``align`` constraints.

A **region** confines parts to an allowed area in board coordinates (the union of
rectangles and polygons): each ref's courtyard must lie inside it. An **align**
makes parts share one coordinate: the ``anchor`` of every ref (its origin, the
centre of its pad box, a named pad, or a courtyard edge) has the same ``x`` (or
``y``) within ``tol_mm``. Both are hard by default; a soft one is a weighted
penalty (protrusion squared, or the anchor's squared distance from the line).

Every placed ref carries *bodies* and an *anchor* in its own unrotated frame. A
plain part has one body, its courtyard (centred on the origin, as everywhere in
the placer). A rigid macro (:mod:`pnr.hier.macro`: a line group or an assembled
block) carries the bodies and anchor of the members it stands for
(``params["bodies"]`` / an explicit ``{"point"}`` or ``{"box", "edge"}`` anchor),
so the relation stays exact at every macro rotation. Anchors are evaluated at the
part's rotation and side: rotation moves pad and edge anchors, never ``origin``.

Exactness: a rectangle, or one polygon, is tested exactly. A union of several
pieces is tested on a raster of :data:`REGION_CELL_MM` cells (a cell counts when
it lies wholly inside one piece), which is conservative by up to one cell. The
legalizer uses that raster for every polygon and union, so its results always
pass the legality check.

Without a ``region`` or ``align`` in the design nothing here runs.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

from pnr.constraints import Enforcement

REGION_CELL_MM = 0.25
# The legalizer keeps an align band this much narrower than tol_mm (rounding).
ALIGN_SLACK_MM = 1e-4
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
        """``box`` lies inside the area (exact for one piece; a union also accepts
        what its raster accepts)."""
        if any(self._piece_contains(p, box) for p in self.pieces):
            return True
        if len(self.pieces) > 1:
            return bool(self.raster_contains([box[0]], [box[1]], [box[2]], [box[3]])[0])
        return False

    # ---- raster (polygons and unions in the legalizer; unions in the check)

    def raster(self):
        """``(origin_x, origin_y, inside)``: cells of REGION_CELL_MM on a grid anchored
        at the board origin; a cell is inside when it lies wholly in one piece."""
        if self._raster is None:
            import numpy as np

            c = REGION_CELL_MM
            ox = math.floor(self.bbox[0] / c) * c
            oy = math.floor(self.bbox[1] / c) * c
            nx = max(1, int(math.ceil((self.bbox[2] - ox) / c - 1e-9)))
            ny = max(1, int(math.ceil((self.bbox[3] - oy) / c - 1e-9)))
            inside = np.zeros((ny, nx), dtype=bool)
            for r in range(ny):
                y0 = oy + r * c
                for col in range(nx):
                    x0 = ox + col * c
                    cell = (x0, y0, x0 + c, y0 + c)
                    inside[r, col] = any(self._piece_contains(p, cell) for p in self.pieces)
            self._raster = (ox, oy, inside)
        return self._raster

    def raster_contains(self, x0s, y0s, x1s, y1s):
        """Vectorized: each box (arrays of edges) lies on inside raster cells only."""
        import numpy as np

        ox, oy, inside = self.raster()
        c = REGION_CELL_MM
        ny, nx = inside.shape
        x0s, y0s = np.asarray(x0s, dtype=float), np.asarray(y0s, dtype=float)
        x1s, y1s = np.asarray(x1s, dtype=float), np.asarray(y1s, dtype=float)
        c0 = np.floor((x0s - ox) / c + 1e-9).astype(int)
        c1 = np.ceil((x1s - ox) / c - 1e-9).astype(int)
        r0 = np.floor((y0s - oy) / c + 1e-9).astype(int)
        r1 = np.ceil((y1s - oy) / c - 1e-9).astype(int)
        within = (c0 >= 0) & (r0 >= 0) & (c1 <= nx) & (r1 <= ny) & (c1 > c0) & (r1 > r0)
        outside = (~inside).astype(np.int64)
        integ = np.zeros((ny + 1, nx + 1), dtype=np.int64)
        integ[1:, 1:] = np.cumsum(np.cumsum(outside, axis=0), axis=1)
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


_AREAS: Dict[int, tuple] = {}


def area_of(con) -> Area:
    """The (cached) :class:`Area` of a region constraint."""
    areas = con.params["areas"]
    hit = _AREAS.get(id(areas))
    if hit is None or hit[0] is not areas:
        if len(_AREAS) > 256:
            _AREAS.clear()
        hit = _AREAS[id(areas)] = (areas, Area.from_params(con.params))
    return hit[1]


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


# ------------------------------------------------------------------ legalizer


class LegalizeRules:
    """Regions and aligns for :func:`pnr.place.legalize.legalize`.

    Built from the hard and soft ``region``/``align`` constraints and the incoming
    (global-placement) poses. Hard relations bound the slot centre: a static box per
    rotation for a rectangle region, a raster mask for a polygon or union, and a
    dynamic band from the members already placed for an align. Soft relations add a
    candidate cost. Each aligned member's target on the aligned axis is the line:
    the centre of the placed members' anchors, else the median of the global
    anchors.
    """

    def __init__(self, regions, aligns, components):
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
        by_ref = {c.ref: c for c in components}
        self.lines = {}
        for index, con in enumerate(self.aligns):
            values = sorted(
                anchor_value(by_ref[r], anchor_spec(con, r), con.params["axis"])
                for r in con.refs
                if r in by_ref
            )
            if values:
                mid = len(values) // 2
                self.lines[index] = (
                    values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2.0
                )
        self._static = {}

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

    def static_box(self, comp) -> Optional[Box]:
        """Slot-centre box (x_lo, x_hi, y_lo, y_hi) of the hard rectangle regions at
        ``comp.rot`` (None when none applies)."""
        key = (comp.ref, round(comp.rot % 360, 6))
        if key in self._static:
            return self._static[key]
        box = None
        for con, area in self.region_of.get(comp.ref, ()):
            if con.enforcement is not Enforcement.HARD or area.rect is None:
                continue
            x0, y0, x1, y1 = area.rect
            for b in bodies(comp, con):
                r = rotate_box(b, comp.rot)
                part = (x0 - r[0], x1 - r[2], y0 - r[1], y1 - r[3])
                box = part if box is None else _intersect(box, part)
        self._static[key] = box
        return box

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

    def align_box(self, comp, placed) -> Optional[Box]:
        """Slot-centre box from the hard aligns of ``comp`` and the members in
        ``placed`` ({ref: component}); None when no member is placed yet."""
        box = None
        for con in self.align_of.get(comp.ref, ()):
            if con.enforcement is not Enforcement.HARD:
                continue
            values = self._placed_anchors(con, comp, placed)
            if not values:
                continue
            tol = con.params["tol_mm"] - ALIGN_SLACK_MM
            off = anchor_offset(comp, anchor_spec(con, comp.ref), con.params["axis"])
            lo, hi = max(values) - tol - off, min(values) + tol - off
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
                index = self.aligns.index(con)
                if index not in self.lines:
                    continue
                line = self.lines[index]
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
            elif self.aligns.index(con) in self.lines:
                line = self.lines[self.aligns.index(con)]
            else:
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
