"""Fixed copper blocks and arcs: the geometry both interpreters share (stdlib only).

A **fixed block** (constraint ``fixed_block``) is a KiCad group on the source board
whose copper stays exactly as drawn: its tracks, arcs, vias, zones and rule areas, and
the pads of its footprints (an RF macro, a hand-drawn antenna feed, a matched
meander). The block's footprints are held out of the placement graph
(:func:`hold_out`); its copper is exported to ``fixed.json`` (schema 2, pnr.fixed_copper)
and reserved by the router as copper its own nets own (pnr.route.detail.fixed): other
nets keep their clearance from it, its own nets may join it.

``fixed.json`` schema 2 is schema 1 (``frame``, ``tracks``, ``vias``) plus, only when
present:

``arcs``
    ``[net, layer, start, mid, end, width]`` (engine mm, y up), outside any block;
``blocks``
    one record per declared block: ``name``, ``group``, ``anchor``, ``sha256`` (the
    copper digest in the anchor's frame), ``refs`` (its footprints), and its own
    ``tracks``, ``arcs``, ``vias`` and ``polygons``. A polygon is ``{net, layer,
    outline, holes, kind}``: ``pad`` (a block footprint's pad), ``zone`` (a copper zone
    on one of the block's ``solid_layers``) or ``rule_area`` (with the ``tracks`` /
    ``vias`` flags it forbids). Block items are always owned by their nets; the
    top-level items keep the caller's ownership flag.

Arcs are reserved as chords whose sagitta is at most :data:`ARC_EPS_MM`, each chord a
capsule ``width / 2 + ARC_EPS_MM`` wide: every point within ``width / 2`` of the arc is
inside it, so the reservation is conservative by at most 1 um.
"""

from __future__ import annotations

import math
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

ARC_EPS_MM = 0.001
SCHEMA = 2

Point = Tuple[float, float]


# --------------------------------------------------------------------- arcs


def arc_center(start: Sequence[float], mid: Sequence[float], end: Sequence[float]):
    """``(centre, radius, sweep)`` of the circular arc from ``start`` through ``mid``
    to ``end``; ``sweep`` is signed (radians, positive counter-clockwise in the frame
    of the points). None when the three points are collinear (a straight track)."""
    ax, ay = start
    bx, by = mid
    cx, cy = end
    d = 2.0 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    scale = max(abs(ax - bx), abs(ay - by), abs(cx - bx), abs(cy - by), 1e-12)
    if abs(d) <= 1e-12 * scale * scale:
        return None
    a2, b2, c2 = ax * ax + ay * ay, bx * bx + by * by, cx * cx + cy * cy
    ux = (a2 * (by - cy) + b2 * (cy - ay) + c2 * (ay - by)) / d
    uy = (a2 * (cx - bx) + b2 * (ax - cx) + c2 * (bx - ax)) / d
    radius = math.hypot(ax - ux, ay - uy)
    a0 = math.atan2(ay - uy, ax - ux)
    a1 = math.atan2(cy - uy, cx - ux)
    cross = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
    if cross > 0:  # counter-clockwise from start to end through mid
        sweep = (a1 - a0) % (2 * math.pi)
    else:
        sweep = -((a0 - a1) % (2 * math.pi))
    if sweep == 0.0:  # start == end: a full circle
        sweep = 2 * math.pi if cross > 0 else -2 * math.pi
    return (ux, uy), radius, sweep


def arc_segments(radius: float, sweep: float, eps: float = ARC_EPS_MM) -> int:
    """Chords needed so that none is more than ``eps`` from its arc piece:
    ``ceil(|sweep| / (2 acos(1 - eps / R)))``, at least one."""
    if radius <= eps:
        return 1
    step = 2.0 * math.acos(1.0 - eps / radius)
    return max(1, int(math.ceil(abs(sweep) / step - 1e-12)))


def arc_points(start, mid, end, eps: float = ARC_EPS_MM) -> List[Point]:
    """The chord vertices of an arc, ``start`` and ``end`` exactly (a collinear
    "arc" is its straight track)."""
    start = (float(start[0]), float(start[1]))
    end = (float(end[0]), float(end[1]))
    geometry = arc_center(start, mid, end)
    if geometry is None:
        return [start, end]
    (ux, uy), radius, sweep = geometry
    n = arc_segments(radius, sweep, eps)
    a0 = math.atan2(start[1] - uy, start[0] - ux)
    pts = [start]
    for k in range(1, n):
        a = a0 + sweep * k / n
        pts.append((ux + radius * math.cos(a), uy + radius * math.sin(a)))
    pts.append(end)
    return pts


def arc_length(start, mid, end) -> float:
    geometry = arc_center(start, mid, end)
    if geometry is None:
        return math.dist(start, end)
    _, radius, sweep = geometry
    return abs(radius * sweep)


def arc_chords(arc, eps: float = ARC_EPS_MM):
    """``[net, layer, a, b, width + 2 eps]`` chords of a schema-2 arc record: a
    straight-track view that covers the arc's copper (used by obstacle models that
    only know segments)."""
    net, layer, start, mid, end, width = arc
    pts = arc_points(start, mid, end, eps)
    return [[net, layer, list(a), list(b), width + 2 * eps] for a, b in zip(pts, pts[1:])]


# ------------------------------------------------------------ fixed.json views


def blocks(copper: Optional[dict]) -> List[dict]:
    return list((copper or {}).get("blocks") or [])


def flatten(copper: Optional[dict], eps: float = ARC_EPS_MM) -> Optional[dict]:
    """A schema-1 view (``tracks`` and ``vias`` only) of fixed copper: arcs as chords
    (their width grown by ``2 eps``) and the blocks' tracks, arcs and vias merged in.
    A schema-1 input is returned unchanged (the same object). For consumers that only
    model straight copper and do not need ownership (length tuning)."""
    if copper is None:
        return None
    if not copper.get("arcs") and not copper.get("blocks"):
        return copper
    tracks = [list(t) for t in copper.get("tracks", [])]
    vias = [dict(v) for v in copper.get("vias", [])]
    for arc in copper.get("arcs") or []:
        tracks.extend(arc_chords(arc, eps))
    for block in blocks(copper):
        tracks.extend(list(t) for t in block.get("tracks", []))
        for arc in block.get("arcs") or []:
            tracks.extend(arc_chords(arc, eps))
        vias.extend(dict(v) for v in block.get("vias", []))
    return dict(frame=copper.get("frame"), tracks=tracks, vias=vias)


def copper_length(copper: Optional[dict]) -> Dict[str, float]:
    """Net -> length (mm) of the fixed tracks and arcs (exact arc lengths)."""
    out: Dict[str, float] = {}

    def add(net, value):
        if net:
            out[net] = out.get(net, 0.0) + value

    for source in [copper or {}] + blocks(copper):
        for net, _layer, a, b, _w in source.get("tracks", []):
            add(net, math.dist(a, b))
        for net, _layer, start, mid, end, _w in source.get("arcs") or []:
            add(net, arc_length(start, mid, end))
    return out


# ------------------------------------------------------------- placement graph


def block_refs(fixed_blocks: Iterable[dict] = (), copper: Optional[dict] = None) -> List[str]:
    """Footprint refs of the fixed blocks: the ones the constraints declare
    (``fixed_block.refs``) and the ones the exported copper records (its group's
    footprints), sorted."""
    refs = set()
    for spec in fixed_blocks or ():
        refs.update(spec.get("refs") or ())
    for block in blocks(copper):
        refs.update(block.get("refs") or ())
    return sorted(refs)


def hold_out(graph, refs: Iterable[str]) -> List[str]:
    """Drop the fixed blocks' footprints (``refs``) from ``graph`` in place: the
    components and their pins (a net keeps its other pins; a net left with none is
    dropped). Their copper stays on the board, so KiCad still sees each net joined
    through it. Returns the refs actually removed, sorted. No refs: ``graph`` is
    unchanged."""
    refs = set(refs or ())
    if not refs:
        return []
    held = sorted(c.ref for c in graph.components if c.ref in refs)
    if not held:
        return []
    gone = set(held)
    graph.components = [c for c in graph.components if c.ref not in gone]
    nets = []
    for net in graph.nets:
        net.pins = [(ref, pad) for ref, pad in net.pins if ref not in gone]
        if net.pins:
            nets.append(net)
    graph.nets = nets
    return held


# -------------------------------------------------------------------- keepouts

KEEPOUT_ITEMS = ("tracks", "vias", "pours")
KEEPOUT_V1_KEYS = (
    "polygon",
    "rect",
    "layers",
    "items",
    "allow_classes",
    "allow_nets",
    "exempt_groups",
)


def keepout_is_v1(spec: dict) -> bool:
    """A ``copper_keepout`` entry in the v1 form (anything beyond ``{name, ref,
    rect_mm}``)."""
    return any(k in spec for k in KEEPOUT_V1_KEYS)


def keepout_class_mode(spec: dict) -> bool:
    """The keepout exempts some copper (allow lists or exempt groups): written as
    custom DRC rules over a rule area that forbids nothing itself, instead of
    rule-area flags."""
    return bool(spec.get("allow_classes") or spec.get("allow_nets") or spec.get("exempt_groups"))


# -------------------------------------------------------------------- polygons


def footprint_keepouts(graph) -> List[dict]:
    """The rule areas built into the footprints (``Component.rule_areas``, pnr.ingest)
    as v1 ``copper_keepout`` specs at the parts' current poses: ``polygon`` in the
    board frame (:func:`pnr.graph.footprint_point`), the copper ``layers`` of the
    part's current side, the ``items`` they bar, every net barred (no allow list).
    ``owner`` names the part; there is no ``ref``, so placement does not treat the
    part as tied to a declared keep-out. Pads of the owner itself are KiCad's
    business (a footprint's rule area never bars its own pads)."""
    from pnr.graph import SIDE_BOTTOM, footprint_point

    out = []
    for comp in graph.components:
        for k, area in enumerate(getattr(comp, "rule_areas", None) or ()):
            layers = area.get("layers_bottom" if comp.side == SIDE_BOTTOM else "layers")
            out.append(
                dict(
                    name="%s:%d" % (comp.ref, k),
                    owner=comp.ref,
                    source="footprint",
                    polygon=[footprint_point(comp, float(x), float(y)) for x, y in area["outline"]],
                    layers=list(layers or ()),
                    items=list(area.get("items") or ()),
                )
            )
    return out


def copper_keepouts(graph, rules) -> List[dict]:
    """Every copper keep-out a copper producer must respect: the declared ones
    (``rules["copper_keepouts"]``) and the footprints' own rule areas
    (:func:`footprint_keepouts`)."""
    return list((rules or {}).get("copper_keepouts") or []) + footprint_keepouts(graph)


def keepout_polygon(graph, spec: dict) -> List[Point]:
    """A v1 ``copper_keepout``'s polygon in the board frame: its ``polygon``, or
    ``rect_mm`` in its part's frame (mirrored with the pads, as writeback's rule
    area is)."""
    from pnr.graph import footprint_point

    if spec.get("polygon") is not None:
        return [(float(x), float(y)) for x, y in spec["polygon"]]
    comp = graph.component(spec["ref"])
    x0, y0, x1, y1 = spec["rect_mm"]
    return [footprint_point(comp, x, y) for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]


def point_in_polygon(p: Point, outline: Sequence[Sequence[float]]) -> bool:
    """Even-odd test (a point on the boundary may go either way)."""
    x, y = p
    inside = False
    n = len(outline)
    for k in range(n):
        x0, y0 = outline[k]
        x1, y1 = outline[(k + 1) % n]
        if (y0 > y) != (y1 > y):
            t = (y - y0) / (y1 - y0)
            if x < x0 + t * (x1 - x0):
                inside = not inside
    return inside


def _point_segment(p, a, b) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    den = dx * dx + dy * dy
    t = 0.0 if den == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / den))
    return math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy)


def polygon_distance(p: Point, polygon: dict) -> float:
    """Distance from ``p`` to a polygon record's copper (0 inside it)."""
    outline = polygon["outline"]
    holes = polygon.get("holes") or []
    if point_in_polygon(p, outline) and not any(point_in_polygon(p, h) for h in holes):
        return 0.0
    best = math.inf
    for ring in [outline] + list(holes):
        for k in range(len(ring)):
            best = min(best, _point_segment(p, ring[k], ring[(k + 1) % len(ring)]))
    return best


# ----------------------------------------------------------------------- ports


def _items(block):
    """``(kind, net, layers, geometry, half_width)`` of a block's copper items, for
    contact tests. A track or arc lists its chord points; a via its centre; a pad or
    zone polygon its record."""
    out = []
    for net, layer, a, b, w in block.get("tracks", []):
        out.append(("track", net, (layer,), [tuple(a), tuple(b)], w / 2))
    for net, layer, start, mid, end, w in block.get("arcs") or []:
        out.append(("arc", net, (layer,), arc_points(start, mid, end), w / 2))
    for via in block.get("vias", []):
        out.append(("via", via.get("net", ""), None, [tuple(via["xy"])], via["diameter_mm"] / 2))
    for poly in block.get("polygons") or []:
        if poly.get("kind") in ("pad", "zone"):
            out.append(("polygon", poly.get("net", ""), (poly["layer"],), poly, 0.0))
    return out


def _on_copper(p, layer, item, slack=1e-6) -> bool:
    kind, _net, layers, geometry, half = item
    if layers is not None and layer not in layers:
        return False
    if kind == "polygon":
        return polygon_distance(p, geometry) <= slack
    if kind == "via":
        return math.dist(p, geometry[0]) <= half + slack
    return any(_point_segment(p, a, b) <= half + slack for a, b in zip(geometry, geometry[1:]))


def _connectivity(block: dict, net: str):
    """``(items, component, ends)`` of ``net``'s copper in a block: ``component[k]``
    numbers item ``k``'s connected copper (items whose copper meets), ``ends`` lists
    every track and arc end as ``(item, point, layer, free)``; an end is free when no
    other item of the net covers it."""
    items = [it for it in _items(block) if it[1] == net]
    parent = list(range(len(items)))

    def find(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    ends = []
    for k, item in enumerate(items):
        if item[0] not in ("track", "arc"):
            continue
        layer = item[2][0]
        for p in (item[3][0], item[3][-1]):
            touching = [
                m for m, other in enumerate(items) if m != k and _on_copper(p, layer, other)
            ]
            for m in touching:
                parent[find(m)] = find(k)
            ends.append((k, (float(p[0]), float(p[1])), layer, not touching))
    # Copper that meets away from the ends (a via or a pad on a track's middle).
    for k, item in enumerate(items):
        if item[0] not in ("via", "polygon"):
            continue
        for m, other in enumerate(items):
            if m == k or other[0] not in ("track", "arc"):
                continue
            layer = other[2][0]
            if item[0] == "via":
                hit = _on_copper(item[3][0], layer, other, item[4])
            else:
                hit = any(_on_copper(p, layer, item) for p in other[3])
            if hit:
                parent[find(m)] = find(k)
    roots: Dict[int, int] = {}
    component = [roots.setdefault(find(k), len(roots)) for k in range(len(items))]
    return items, component, ends


def port_candidates(block: dict, net: str) -> List[Tuple[Point, str, int]]:
    """The free ends of ``net``'s tracks and arcs in a block: ``(point, layer,
    component)`` (see :func:`_connectivity`)."""
    _items_, component, ends = _connectivity(block, net)
    return [(p, layer, component[k]) for k, p, layer, free in ends if free]


def components_touching(block: dict, net: str, pads) -> set:
    """Component numbers of ``net``'s block copper that already reach a circuit pad:
    a track or arc end inside one of ``pads`` (``(layer, left, bottom, right, top)``
    rectangles on that copper layer)."""
    _items_, component, ends = _connectivity(block, net)
    hits = set()
    for k, (x, y), layer, _free in ends:
        for pad_layer, x0, y0, x1, y1 in pads:
            if pad_layer == layer and x0 - 1e-6 <= x <= x1 + 1e-6 and y0 - 1e-6 <= y <= y1 + 1e-6:
                hits.add(component[k])
    return hits


def pick_ports(
    block: dict,
    net: str,
    pad_points: Sequence[Point],
    joined: Optional[Iterable[int]] = None,
    via_layer: str = "F.Cu",
) -> List[Tuple[Point, str]]:
    """One port per connected component of ``net``'s block copper that no pad of the
    circuit already reaches (``joined``: those components' numbers), nearest the
    centroid of the net's pads (``pad_points``), ties by position: a free end of its
    tracks and arcs; for a component without one (a ground rail joining fence vias),
    one of its vias, entered on ``via_layer`` (its copper is on every layer); else an
    end of its tracks. A component of pads or zones alone gets none."""
    joined = set(joined or ())
    if pad_points:
        cx = sum(p[0] for p in pad_points) / len(pad_points)
        cy = sum(p[1] for p in pad_points) / len(pad_points)
    else:
        cx = cy = 0.0
    items, component, ends = _connectivity(block, net)
    candidates: Dict[int, List[tuple]] = {}
    for k, point, layer, free in ends:
        candidates.setdefault(component[k], []).append((0 if free else 2, point, layer))
    for k, item in enumerate(items):
        if item[0] == "via":
            candidates.setdefault(component[k], []).append((1, item[3][0], via_layer))
    out = []
    for comp in sorted(candidates):
        if comp in joined:
            continue
        rank = min(r for r, _, _ in candidates[comp])
        best = min(
            (math.hypot(p[0] - cx, p[1] - cy), float(p[0]), float(p[1]), layer)
            for r, p, layer in candidates[comp]
            if r == rank
        )
        out.append(((best[1], best[2]), best[3]))
    return out
