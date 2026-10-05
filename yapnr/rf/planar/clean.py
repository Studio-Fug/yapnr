"""Geometry cleaning for planar models: union, crop, snap, simplify and exact-arc refitting.

KiCad zone fills and buffered tracks arrive as polygons with thousands of vertices: clusters a
micrometre apart, long runs of collinear points and arcs sampled every few micrometres. A mesher
puts a node on every one of them, so a feed region would carry 5 um elements along every copper
edge. ``clean_geometry`` merges near-duplicate vertices, removes collinear ones and replaces runs
of short edges that lie on one circle with an exact arc (``{"mid": ...}`` ring items), within a
stated tolerance; ``xor_area`` measures what the cleaning changed.

Needs shapely (BSD-3-Clause), imported lazily so the document model stays stdlib only.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from yapnr.rf.planar.model import Point, circle_through, ring_points

# Defaults (mm): merge vertices closer than SNAP, drop collinear vertices (Douglas-Peucker at
# SIMPLIFY: only numerically collinear ones, so the tangent points where a straight edge meets an
# arc survive and the refit arc starts there), refit arcs whose vertices and chord midpoints are
# within ARC_TOL of one circle.
SNAP = 0.002
SIMPLIFY = 1e-5
ARC_TOL = 0.001
MIN_AREA = 1e-5


def _shapely():
    try:
        import shapely  # noqa: F401
        from shapely import geometry, ops
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "planar cleaning and the adapters need shapely (pip install shapely)"
        ) from exc
    return geometry, ops


def to_shapely(polygons: Iterable[Dict[str, Any]], chord: float = 0.002):
    """The union of polygon dicts (arcs sampled at ``chord``) as one shapely geometry."""
    geometry, ops = _shapely()
    parts = []
    for p in polygons:
        outer = ring_points(p["outer"], chord)
        holes = [ring_points(h, chord) for h in p.get("holes", [])]
        parts.append(geometry.Polygon(outer, holes).buffer(0))
    return ops.unary_union(parts) if parts else geometry.Polygon()


def parts(geom) -> List[Any]:
    """Polygons of a (multi)polygon or collection, empty ones dropped."""
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom]
    out = []
    for g in getattr(geom, "geoms", []):
        out.extend(parts(g))
    return out


def merge_close(points: Sequence[Point], snap: float) -> List[Point]:
    """Drop vertices closer than ``snap`` to the previous kept one (closed ring)."""
    out: List[Point] = []
    for p in points:
        if not out or math.hypot(p[0] - out[-1][0], p[1] - out[-1][1]) >= snap:
            out.append((float(p[0]), float(p[1])))
    while len(out) > 3 and math.hypot(out[0][0] - out[-1][0], out[0][1] - out[-1][1]) < snap:
        out.pop()
    return out


def _turn(a: Point, b: Point, c: Point) -> float:
    v1 = (b[0] - a[0], b[1] - a[1])
    v2 = (c[0] - b[0], c[1] - b[1])
    return math.atan2(v1[0] * v2[1] - v1[1] * v2[0], v1[0] * v2[0] + v1[1] * v2[1])


def refit_arcs(
    points: Sequence[Point],
    tol: float = ARC_TOL,
    min_edges: int = 3,
    max_chord: float = 0.3,
    max_turn_deg: float = 40.0,
    r_range: Tuple[float, float] = (0.01, 20.0),
) -> List[Any]:
    """A ring with every run of >= ``min_edges`` short edges on one circle replaced by an arc.

    A run qualifies when its edges are at most ``max_chord`` long, it turns one way at every
    inner vertex by at most ``max_turn_deg``, and every vertex and chord midpoint lies within
    ``tol`` of the circle through its ends and middle vertex. Sweeps over 0.9 pi become two arcs
    (an exact OCC arc through three points stays well conditioned).
    """
    pts = [(float(x), float(y)) for x, y in points]
    n = len(pts)
    if n < min_edges + 1:
        return [list(p) for p in pts]
    max_turn = math.radians(max_turn_deg)
    elen = [
        math.hypot(pts[(i + 1) % n][0] - pts[i][0], pts[(i + 1) % n][1] - pts[i][1])
        for i in range(n)
    ]
    turns = [_turn(pts[i - 1], pts[i], pts[(i + 1) % n]) for i in range(n)]

    def corner(i):
        return (
            abs(turns[i]) > max_turn
            or elen[i] > max_chord
            or elen[i - 1] > max_chord
            or abs(turns[i]) < 1e-6
        )

    start = next((i for i in range(n) if corner(i)), 0)
    pts = pts[start:] + pts[:start]
    elen = elen[start:] + elen[:start]
    turns = turns[start:] + turns[:start]

    def fits(i: int, j: int) -> Optional[Tuple[float, float, float, float]]:
        idx = [k % n for k in range(i, j + 1)]
        if any(elen[k] > max_chord for k in idx[:-1]):
            return None
        inner = [turns[k] for k in idx[1:-1]]
        if not inner or any(abs(t) > max_turn or abs(t) < 1e-6 for t in inner):
            return None
        if not (all(t > 0 for t in inner) or all(t < 0 for t in inner)):
            return None
        a, m, b = pts[idx[0]], pts[idx[len(idx) // 2]], pts[idx[-1]]
        try:
            cx, cy, r = circle_through(a, m, b)
        except ValueError:
            return None
        if not (r_range[0] <= r <= r_range[1]):
            return None
        for k in idx:
            if abs(math.hypot(pts[k][0] - cx, pts[k][1] - cy) - r) > tol:
                return None
        for k in idx[:-1]:
            q, s = pts[k], pts[(k + 1) % n]
            if abs(math.hypot(0.5 * (q[0] + s[0]) - cx, 0.5 * (q[1] + s[1]) - cy) - r) > tol:
                return None
        # the swept angle, vertex to vertex about the fitted centre
        sweep = 0.0
        t_prev = math.atan2(pts[idx[0]][1] - cy, pts[idx[0]][0] - cx)
        for k in idx[1:]:
            t = math.atan2(pts[k][1] - cy, pts[k][0] - cx)
            d = (t - t_prev + math.pi) % (2 * math.pi) - math.pi
            sweep += d
            t_prev = t
        if abs(sweep) > 1.9 * math.pi:
            return None
        return cx, cy, r, sweep

    out: List[Any] = []
    i = 0
    while i < n:
        best = None
        j = i + min_edges
        while j <= n:
            f = fits(i, j)
            if f is None:
                break
            best = (j, f)
            j += 1
        if best is None:
            out.append(list(pts[i]))
            i += 1
            continue
        j, (cx, cy, r, sweep) = best
        t0 = math.atan2(pts[i][1] - cy, pts[i][0] - cx)

        def on(frac):
            t = t0 + sweep * frac
            return [cx + r * math.cos(t), cy + r * math.sin(t)]

        out.append(list(pts[i]))
        if abs(sweep) > 0.9 * math.pi:
            out += [{"mid": on(0.25)}, on(0.5), {"mid": on(0.75)}]
        else:
            out.append({"mid": on(0.5)})
        i = j
    return out


def clean_polygon(
    poly,
    snap: float = SNAP,
    simplify: float = SIMPLIFY,
    refit: bool = True,
    arc_tol: float = ARC_TOL,
) -> Optional[Dict[str, Any]]:
    """One shapely polygon as a ring dict, cleaned; None when nothing is left."""
    geometry, _ = _shapely()
    from shapely.geometry.polygon import orient

    poly = orient(poly.simplify(simplify, preserve_topology=True), 1.0)
    rings = []
    for k, ring in enumerate([poly.exterior] + list(poly.interiors)):
        pts = merge_close(list(ring.coords)[:-1], snap)
        if len(pts) < 3:
            if k == 0:
                return None
            continue
        if abs(geometry.Polygon(pts).area) < MIN_AREA:
            if k == 0:
                return None
            continue
        rings.append(refit_arcs(pts, tol=arc_tol) if refit else [list(p) for p in pts])
    return dict(outer=rings[0], holes=rings[1:])


def clean_geometry(geom, **kw) -> List[Dict[str, Any]]:
    """Polygon dicts of a shapely geometry, every part cleaned, parts under MIN_AREA dropped."""
    out = []
    for p in parts(geom):
        if p.area < MIN_AREA:
            continue
        c = clean_polygon(p, **kw)
        if c is not None:
            out.append(c)
    return out


def clean_doc(doc: Dict[str, Any], **kw) -> Dict[str, Any]:
    """Every conductor of a document re-unioned and cleaned (``kw`` as ``clean_polygon``)."""
    for c in doc["conductors"]:
        c["polygons"] = clean_geometry(to_shapely(c["polygons"]), **kw)
    doc["conductors"] = [c for c in doc["conductors"] if c["polygons"]]
    return doc


def xor_area(a, b) -> float:
    """Area of the symmetric difference of two shapely geometries (mm^2)."""
    return float(a.symmetric_difference(b).area)


def cleaning_change(before, after_polys: List[Dict[str, Any]], chord: float = 0.0005) -> float:
    """What cleaning moved: XOR area between the input geometry and the cleaned polygons."""
    return xor_area(before, to_shapely(after_polys, chord=chord))


def overlaps(doc: Dict[str, Any], min_area: float = 1e-6) -> List[Tuple[str, str, str, float]]:
    """(layer, net_a, net_b, area) for every pair of nets whose copper overlaps on a layer."""
    by_layer: Dict[str, List[Tuple[str, Any]]] = {}
    for c in doc["conductors"]:
        by_layer.setdefault(c["layer"], []).append((c["net"], to_shapely(c["polygons"])))
    out = []
    for lay, items in by_layer.items():
        for i, (na, ga) in enumerate(items):
            for nb, gb in items[i + 1 :]:
                if na == nb:
                    continue
                area = ga.intersection(gb).area
                if area > min_area:
                    out.append((lay, na, nb, float(area)))
    return out


def thin_parts(geom, width: float, min_area: float = 0.02):
    """Pieces of ``geom`` narrower than ``width`` (an opening by width/2), as shapely polygons:
    the sliver finder of the radar60 feed models (prep.py), as a function."""
    core = geom.buffer(-width / 2, join_style="mitre").buffer(width / 2, join_style="mitre")
    thin = geom.difference(core.buffer(0.005))
    return [p for p in parts(thin) if p.area > min_area]
