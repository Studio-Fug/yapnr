"""Pure geometry core of the PNR_GLOSS phase: normalize, dekink, gloss, corridor.

Design: docs/design/gloss.md (steps, legality L1-L8, constants).

Units are KiCad internal units (nm) held in Python ints. There is no pcbnew
dependency: every board-facing predicate (native shape clearance L1, same-net
contact L2, obstacle test points L3, proximity guards L7, strip blockers) is
injected as a callable or plain data, the way pnr.route.detail.keyhole takes
`clear`. The KiCad workers re-check every proposal on the applied board; nothing
here accepts an edit on its own.

Determinism: all iteration is over sorted keys, ties are broken
lexicographically on coordinates, there is no randomness and the output does
not depend on the order of the input segments.

Cost model (the router's own): octilinear legs, chain cost C = L + 0.15 mm * B,
and the keyhole `relax` rule R (shorter, or the same length with fewer bends).
Corridor moves get the `align_parallel` slack (dC <= 0.2 mm, no added bend).
"""

import hashlib
import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Optional, Tuple

Point = Tuple[int, int]

# ---------------------------------------------------------------- constants (Appendix A; global, never per board)
NM = 1_000_000
BEND_COST = 150_000  # keyhole.route bend_cost 0.15 mm
TOL = 1  # coordinate tolerance (nm)
LENGTH_TOL = 1_000  # rule R length tolerance (1 um)
MARGIN = 1_000  # Oracle clearance margin (+0.001 mm)
TUBE = 1_000_000  # gloss tube radius T
TUBE_SAMPLE = 50_000  # L8: new segments are sampled at <= 0.05 mm
TUBE_PITCHES = (250_000, 100_000)  # route_search_pitch ladder subset
TUBE_EXPANSIONS = 20_000
DP_MAX_LEGS = 24  # dekink lattice <= 25 x 25
DEKINK_RETRIES = 8
WINDOW_LEGS, WINDOW_STRIDE = 16, 8
WINDOW_MAX_LEGS, WINDOW_MAX_LENGTH = 64, 25 * NM
CORRIDOR_MIN_LEG = 2 * NM
CORRIDOR_MIN_OVERLAP = 2 * NM
CORRIDOR_MAX_GAP = 2 * NM
CORRIDOR_TIGHT = 100_000  # already-tight filter c_req + 0.1 mm
CORRIDOR_MAX_SHIFT = 2 * NM
CORRIDOR_SLACK = 200_000  # keyhole.align_parallel length_slack
CORRIDOR_MIN_SHIFT = 20_000
CORRIDOR_MIN_GAIN = 0.05 * NM * NM  # 0.05 mm^2 in nm^2
CORRIDOR_MAX_ANCHORS = 8
CORRIDOR_WINDOW_MARGIN = NM
SI_GUARD_CAP = CORRIDOR_MAX_GAP  # an SI leg facing a member within this gap is a boundary
BATCH_LIMIT = 16
INFLUENCE_MARGIN = 500_000
ANGLE_TOL = 0.5  # degrees
# Same-class adjacency: the sampled normal-ray gap metric X / T
# (direction-agnostic; no leg-length or overlap cut-offs) replaces E as the corridor
# acceptance and report metric, and its saturated per-chain form ("hug") breaks router-cost
# ties in dekink and gloss. Constants are global (never per board).
ADJ_STEP = 100_000  # samples every 0.1 mm along the copper
ADJ_REACH = CORRIDOR_MAX_GAP  # rays up to 2.0 mm from the copper edge
ADJ_PARALLEL = 30.0  # a hit counts as a same-class neighbour within +-30 deg of parallel
ADJ_TIGHT = CORRIDOR_TIGHT  # gap <= c_req + 0.1 mm counts as packed (tight length T)
ADJ_MIN_GAIN = CORRIDOR_MIN_GAIN  # a material adjacency change (mm^2 x NM^2)
RAY_TIE = 1e-6  # nm: two ray hits this close are at the same distance (a shared vertex)
HUG_SLACK = CORRIDOR_SLACK  # router-cost slack adjacency may buy (keyhole.align_parallel)
DP_MAX_LATTICE = 32  # dekink lattice values per axis incl. neighbour-snap lines
CHAIN_CALL_BUDGET = 6000  # deterministic per-chain planning budget (clear+contact calls)

DIRS = ((1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1))


# ---------------------------------------------------------------- primitives
def ipt(p):
    return (int(round(p[0])), int(round(p[1])))


def direction(a, b):
    """Octilinear direction index 0..7 of a->b (DIRS order, 1 nm tolerance), else None."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    ax, ay = abs(dx), abs(dy)
    if max(ax, ay) <= TOL:
        return None
    if ay <= TOL:
        return 0 if dx > 0 else 4
    if ax <= TOL:
        return 2 if dy > 0 else 6
    if abs(ax - ay) <= TOL:
        if dx > 0:
            return 1 if dy > 0 else 7
        return 3 if dy > 0 else 5
    return None


def octilinear(a, b):
    return direction(a, b) is not None


def _heading(v):
    return math.degrees(math.atan2(v[1], v[0]))


def _vdir(v):
    v = tuple(v)
    if max(abs(v[0]), abs(v[1])) <= 2 * TOL:  # small exact vectors such as DIRS entries
        unit = (_sgn(v[0]), _sgn(v[1]))
        exact = v[0] == 0 or v[1] == 0 or abs(v[0]) == abs(v[1])
        return DIRS.index(unit) if unit in DIRS and exact else None
    return direction((0, 0), v)


def vector_turn(v1, v2):
    """Deflection (degrees, 0..180) from heading v1 to heading v2."""
    d1, d2 = _vdir(v1), _vdir(v2)
    if d1 is not None and d2 is not None:
        k = (d2 - d1) % 8
        return 45 * min(k, 8 - k)
    return abs((_heading(v2) - _heading(v1) + 180) % 360 - 180)


def turn_angle(a, v, b):
    return vector_turn((v[0] - a[0], v[1] - a[1]), (b[0] - v[0], b[1] - v[1]))


def ray_angle(v1, v2):
    """Angle (degrees, 0..180) between two rays leaving the same vertex (< 90 is an acid trap)."""
    return vector_turn(v1, v2)


def turn_class(deg):
    for c in (0, 45, 90, 135, 180):
        if abs(deg - c) <= ANGLE_TOL:
            return c
    return "other"


def is_sharp(deg):
    """A turn the rule R guard counts (90/135/180 and any-angle turns over 67.5)."""
    return deg > 67.5


def _straight(a, v, b):
    d1, d2 = direction(a, v), direction(v, b)
    if d1 is not None and d1 == d2:
        return True
    if d1 is not None and d2 is not None:
        return False
    dx, dy = b[0] - a[0], b[1] - a[1]
    n = math.hypot(dx, dy)
    if n == 0:
        return False
    if abs(dx * (v[1] - a[1]) - dy * (v[0] - a[0])) / n > TOL:
        return False
    return (v[0] - a[0]) * dx + (v[1] - a[1]) * dy > 0 and (b[0] - v[0]) * dx + (
        b[1] - v[1]
    ) * dy > 0


def simplify(points):
    """Drop repeated vertices and merge collinear same-direction legs (copper unchanged)."""
    out = []
    for p in points:
        p = (int(p[0]), int(p[1]))
        if out and out[-1] == p:
            continue
        out.append(p)
        while len(out) >= 3 and _straight(out[-3], out[-2], out[-1]):
            del out[-2]
    return out


def length(points):
    return sum(math.dist(p, q) for p, q in zip(points, points[1:]))


def turns(points):
    pts = simplify(points)
    return [turn_angle(pts[i - 1], pts[i], pts[i + 1]) for i in range(1, len(pts) - 1)]


def bend_histogram(points):
    out = {45: 0, 90: 0, 135: 0, 180: 0, "other": 0}
    for t in turns(points):
        c = turn_class(t)
        if c != 0:
            out[c] += 1
    return out


def bends(points):
    return sum(1 for t in turns(points) if turn_class(t) != 0)


def sharp_turns(points):
    return sum(1 for t in turns(points) if turn_class(t) != 0 and is_sharp(t))


def cost(points):
    """Router path cost C = L + 0.15 mm per heading change (nm)."""
    return length(points) + BEND_COST * bends(points)


def octile(a, b):
    dx, dy = abs(b[0] - a[0]), abs(b[1] - a[1])
    return max(dx, dy) - min(dx, dy) + math.sqrt(2) * min(dx, dy)


def lower_bound(a, b):
    """Smallest C any octilinear path a->b can have."""
    if a == b:
        return 0.0
    return octile(a, b) + (0 if octilinear(a, b) else BEND_COST)


def end_turns(points, anchor_legs=None):
    """Turns (degrees) at the two chain-end anchors against the anchor's single outside leg.

    anchor_legs: {anchor: [vectors (other end - anchor) of the OTHER same-net legs there]}
    (chain_anchor_legs). Only an anchor with exactly one outside leg is a degree-2 copper
    vertex (pin point, frozen end, width change); a junction or a pad/via end has no turn.
    """
    if not anchor_legs:
        return []
    pts = simplify(points)
    if len(pts) < 2:
        return []
    out = []
    for end, nxt in ((pts[0], pts[1]), (pts[-1], pts[-2])):
        legs = anchor_legs.get(end, ())
        if len(legs) != 1:
            continue
        o = legs[0]
        out.append(vector_turn((-o[0], -o[1]), (nxt[0] - end[0], nxt[1] - end[1])))
    return out


def total_bends(points, anchor_legs=None):
    """Interior bends plus the turns at the end anchors (every degree-2 copper vertex)."""
    return bends(points) + sum(1 for t in end_turns(points, anchor_legs) if turn_class(t) != 0)


def total_sharp(points, anchor_legs=None):
    return sharp_turns(points) + sum(
        1 for t in end_turns(points, anchor_legs) if turn_class(t) != 0 and is_sharp(t)
    )


def total_cost(points, anchor_legs=None):
    """C = L + 0.15 mm per heading change, the turns at the end anchors included (nm)."""
    return length(points) + BEND_COST * total_bends(points, anchor_legs)


def rule_r(old, new, anchor_legs=None):
    """keyhole.relax acceptance plus its bend cost (design 3.1).

    Shorter by more than 1 um, or equal length (+-1 um) with fewer bends; the
    90/135 turn count never grows and C strictly drops (a shorter path that adds
    bends must still pay for them at 0.15 mm each). With anchor_legs the turns at
    the end anchors against their outside leg are charged too (every
    degree-2 copper vertex counts, not only chain-interior ones).
    """
    lo, ln = length(old), length(new)
    bo, bn = total_bends(old, anchor_legs), total_bends(new, anchor_legs)
    shorter = ln < lo - LENGTH_TOL
    simpler = abs(ln - lo) <= LENGTH_TOL and bn < bo
    return (
        (shorter or simpler)
        and total_sharp(new, anchor_legs) <= total_sharp(old, anchor_legs)
        and ln + BEND_COST * bn < lo + BEND_COST * bo - 1e-6
    )


def bbox(points, margin=0):
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs) - margin, min(ys) - margin, max(xs) + margin, max(ys) + margin)


def boxes_overlap(a, b):
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def point_segment_distance(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    n2 = dx * dx + dy * dy
    t = 0.0 if n2 == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / n2))
    return math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy)


def _cross(a, b, p):
    return (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])


def _sgn(x):
    return (x > 0) - (x < 0)


def _on_box(p, a, b):
    return min(a[0], b[0]) <= p[0] <= max(a[0], b[0]) and min(a[1], b[1]) <= p[1] <= max(a[1], b[1])


def segments_intersect(a, b, c, d):
    """Closed segments share a point (exact for ints)."""
    d1, d2 = _sgn(_cross(c, d, a)), _sgn(_cross(c, d, b))
    d3, d4 = _sgn(_cross(a, b, c)), _sgn(_cross(a, b, d))
    if d1 * d2 < 0 and d3 * d4 < 0:
        return True
    return (
        (d1 == 0 and _on_box(a, c, d))
        or (d2 == 0 and _on_box(b, c, d))
        or (d3 == 0 and _on_box(c, a, b))
        or (d4 == 0 and _on_box(d, a, b))
    )


def segment_distance(a, b, c, d):
    if segments_intersect(a, b, c, d):
        return 0.0
    return min(
        point_segment_distance(a, c, d),
        point_segment_distance(b, c, d),
        point_segment_distance(c, a, b),
        point_segment_distance(d, a, b),
    )


def polyline_distance(p, points):
    if len(points) == 1:
        return math.dist(p, points[0])
    return min(point_segment_distance(p, a, b) for a, b in zip(points, points[1:]))


def point_in_polygon(p, poly):
    return winding_number(poly, p) != 0


def winding_number(loop, p):
    """Winding number of closed polyline `loop` around p (exact for ints)."""
    wn = 0
    n = len(loop)
    for i in range(n):
        a, b = loop[i], loop[(i + 1) % n]
        if a[1] <= p[1]:
            if b[1] > p[1] and _cross(a, b, p) > 0:
                wn += 1
        elif b[1] <= p[1] and _cross(a, b, p) < 0:
            wn -= 1
    return wn


def _trim(old, new):
    i = 0
    while i + 1 < len(old) and i + 1 < len(new) and old[i + 1] == new[i + 1]:
        i += 1
    j = 0
    while j + 1 < len(old) - i and j + 1 < len(new) - i and old[-2 - j] == new[-2 - j]:
        j += 1
    return old[i : len(old) - j], new[i : len(new) - j]


def swept_points(old, new, points):
    """Test points that `new` would sweep across (L3).

    old and new share both ends. A point is swept when the closed polyline old +
    reversed(new) winds around it, or when it lies on exactly one of the two
    paths (conservative). Points on both (unchanged anchors) are ignored.
    """
    old, new = _trim(list(old), list(new))
    if old == new:
        return []
    loop = old + new[::-1][1:-1]
    box = bbox(loop, TOL)
    bad = []
    for p in points:
        if not (box[0] <= p[0] <= box[2] and box[1] <= p[1] <= box[3]):
            continue
        on_old = polyline_distance(p, old) <= TOL
        on_new = polyline_distance(p, new) <= TOL
        if on_old and on_new:
            continue
        if on_old or on_new or winding_number(loop, p):
            bad.append(p)
    return bad


def _line_intersection_x(a, b, c, d):
    rx, ry = b[0] - a[0], b[1] - a[1]
    sx, sy = d[0] - c[0], d[1] - c[1]
    det = rx * sy - ry * sx
    if det == 0:
        return None
    qx, qy = c[0] - a[0], c[1] - a[1]
    t, u = (qx * sy - qy * sx) / det, (qx * ry - qy * rx) / det
    if 0 <= t <= 1 and 0 <= u <= 1:
        return a[0] + t * rx
    return None


def swept_area(old, new):
    """|old (+) new|: area enclosed by old + reversed(new), weighted by |winding| (nm^2)."""
    old, new = _trim(list(old), list(new))
    if old == new:
        return 0.0
    loop = old + new[::-1][1:-1]
    n = len(loop)
    edges = [(loop[k], loop[(k + 1) % n]) for k in range(n)]
    xs = {p[0] for p in loop}
    for i in range(n):
        for j in range(i + 2, n):
            if i == 0 and j == n - 1:
                continue
            x = _line_intersection_x(*edges[i], *edges[j])
            if x is not None:
                xs.add(x)
    xs = sorted(xs)
    area = 0.0
    for x0, x1 in zip(xs, xs[1:]):
        if x1 - x0 <= 0:
            continue
        cuts = []
        for a, b in edges:
            if a[0] == b[0]:
                continue
            lo, hi = min(a[0], b[0]), max(a[0], b[0])
            if lo <= x0 and x1 <= hi:

                def f(x, a=a, b=b):
                    return a[1] + (b[1] - a[1]) * (x - a[0]) / (b[0] - a[0])

                y0, y1 = f(x0), f(x1)
                cuts.append(((y0 + y1) / 2, y0, y1, 1 if b[0] > a[0] else -1))
        cuts.sort()
        w = 0
        for (_, ya0, ya1, s), (_, yb0, yb1, _) in zip(cuts, cuts[1:]):
            w += s
            if w:
                area += abs(w) * ((yb0 - ya0) + (yb1 - ya1)) / 2 * (x1 - x0)
    return area


def elbows(a, b):
    """keyhole.elbows in integer nm: the four one-bend octilinear connections."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    d = min(abs(dx), abs(dy))
    sx, sy = (1 if dx >= 0 else -1), (1 if dy >= 0 else -1)
    out = []
    for p in (
        (a[0] + sx * d, a[1] + sy * d),
        (b[0] - sx * d, b[1] - sy * d),
        (a[0], b[1]),
        (b[0], a[1]),
    ):
        path = [a] + ([p] if p != a and p != b else []) + [b]
        if path not in out:
            out.append(path)
    if octilinear(a, b) and [a, b] not in out:
        out.append([a, b])
    return [p for p in out if all(octilinear(u, v) for u, v in zip(p, p[1:]))]


# ---------------------------------------------------------------- signal class (design 1.1)
def class_key(layer, netclass, width, clearance):
    """Corridor grouping key K = (layer, netclass, width_nm, clearance_nm)."""
    return (layer, netclass, int(width), int(clearance))


def eligibility(
    net,
    *,
    mode,
    protected=(),
    si_nets=(),
    netclass="Default",
    arc_layers=(),
    layer=None,
    gloss_si=False,
):
    """E1-E5 over precomputed engine facts. Returns (eligible, si, reason)."""
    si = net in si_nets
    if mode != "signal":
        return False, si, "E1:" + str(mode)
    if net in protected:
        return False, si, "E2:protected"
    if si and not gloss_si:
        return False, si, "E3:si"
    if netclass != "Default":
        return False, si, "E4:netclass"
    if layer is not None and layer in arc_layers:
        return False, si, "E5:arc"
    return True, si, None


def net_record(
    net,
    rules,
    *,
    protected=(),
    si_nets=(),
    netclass="Default",
    arc_layers=(),
    layer=None,
    gloss_si=False,
):
    """Eligibility from the engine's own net_policy (pure; pnr.electrical has no KiCad import)."""
    from pnr.electrical import net_policy

    policy = net_policy(net, rules)
    ok, si, reason = eligibility(
        net,
        mode=policy["mode"],
        protected=protected,
        si_nets=si_nets,
        netclass=netclass,
        arc_layers=arc_layers,
        layer=layer,
        gloss_si=gloss_si,
    )
    return dict(
        net=net,
        eligible=ok,
        si=si,
        reason=reason,
        mode=policy["mode"],
        clearance=round(policy["clearance_mm"] * NM),
    )


def pitch(w1, c1, w2, c2):
    """Minimum legal centre pitch p(i,j) = (wi + wj)/2 + max(ci, cj) + 0.001 mm (nm)."""
    return (w1 + w2) / 2 + max(c1, c2) + MARGIN


def groupable(a, b):
    """Two corridor legs/records may share a stack: eligible, not SI, other nets, equal K."""
    return (
        a["eligible"]
        and b["eligible"]
        and not a["si"]
        and not b["si"]
        and a["net"] != b["net"]
        and a["key"] is not None
        and a["key"] == b["key"]
    )


# ---------------------------------------------------------------- segments, anchors and chains (design 1.2)
@dataclass(frozen=True)
class Seg:
    id: str
    a: Point
    b: Point
    width: int
    locked: bool = False
    frozen: bool = False  # F2/F3 decided by the caller (neck/terminal zones, DRC report)

    @property
    def editable(self):
        return not (self.locked or self.frozen)

    @property
    def zero(self):
        return max(abs(self.a[0] - self.b[0]), abs(self.a[1] - self.b[1])) <= TOL


def _seg_key(s):
    a, b = (s.a, s.b) if s.a <= s.b else (s.b, s.a)
    return (a, b, s.width, s.id)


def _on_interior(p, a, b):
    if p == a or p == b:
        return False
    dx, dy = b[0] - a[0], b[1] - a[1]
    n2 = dx * dx + dy * dy
    if n2 == 0:
        return False
    t = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / n2
    if t <= 0 or t >= 1:
        return False
    return math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy) <= TOL


class _Buckets:
    def __init__(self, size=NM):
        self.size = size
        self.cells = defaultdict(list)

    def add_box(self, box, item):
        s = self.size
        for x in range(box[0] // s, box[2] // s + 1):
            for y in range(box[1] // s, box[3] // s + 1):
                self.cells[x, y].append(item)

    def query(self, box):
        s = self.size
        out = set()
        for x in range(box[0] // s, box[2] // s + 1):
            for y in range(box[1] // s, box[3] // s + 1):
                out.update(self.cells.get((x, y), ()))
        return out


@dataclass
class Chain:
    net: str
    layer: str
    width: int
    points: list  # node sequence, anchor .. anchor
    pieces: list  # [(a, b, seg_id)] in walk order
    frozen: bool = False

    @property
    def legs(self):
        return simplify(self.points)

    @property
    def seg_ids(self):
        return tuple(sorted({p[2] for p in self.pieces}))

    @property
    def key(self):
        return (self.layer, self.net, self.points[0], self.points[-1], self.pieces[0][2])


@dataclass
class ChainSet:
    chains: list
    anchors: set
    loops: int = 0  # anchor-free closed loops skipped
    closed: int = 0  # chains that start and end at one anchor (skipped)
    off_width: int = 0  # chains with a width other than the class width (skipped)
    frozen: int = 0  # chains containing frozen/locked copper
    anchor_legs: dict = field(
        default_factory=dict
    )  # anchor -> sorted (other end - anchor) of incident pieces


def split_pieces(segs, pin_points=()):
    """Split segments at T points and pin points (track_graph.on_segment semantics)."""
    segs = sorted(segs, key=_seg_key)
    nodes = sorted({p for s in segs for p in (s.a, s.b)} | {ipt(p) for p in pin_points})
    index = _Buckets()
    for p in nodes:
        index.add_box((p[0], p[1], p[0], p[1]), p)
    pieces = []
    for s in segs:
        if s.zero:
            continue
        box = bbox((s.a, s.b), TOL)
        inner = sorted(
            (p for p in index.query(box) if _on_interior(p, s.a, s.b)),
            key=lambda p: (math.dist(p, s.a), p),
        )
        pts = [s.a] + inner + [s.b]
        for p, q in zip(pts, pts[1:]):
            if p != q:
                pieces.append((p, q, s))
    return pieces


def build_chains(
    segs, *, net="", layer="", pinned=None, pin_points=(), extra_anchors=(), class_width=None
):
    """Chains between anchors (design 1.2) for one (net, layer).

    pinned(p) -> bool marks vertices inside a same-net pad, via pad or filled zone
    (A1/A2/A6); pin_points are projections of pad centres onto touching segments
    (A1) and split segments like T points. Anchors also include degree != 2 (A3),
    width changes (A4), endpoints of frozen or locked segments (A5/A7, F1-F3) and
    zero-length dots. Anchor-free loops and chains that close on one anchor are
    skipped; so are chains whose width differs from class_width (when given).
    """
    pieces = split_pieces(segs, pin_points)
    incident = defaultdict(list)
    for k, (a, b, s) in enumerate(pieces):
        incident[a].append(k)
        incident[b].append(k)
    pins = {ipt(p) for p in pin_points}
    anchors = set(ipt(p) for p in extra_anchors) & set(incident)
    for s in segs:
        if s.zero:
            anchors.add(s.a)
    for node, ks in incident.items():
        widths = {pieces[k][2].width for k in ks}
        if (
            len(ks) != 2
            or len(widths) > 1
            or node in pins
            or any(not pieces[k][2].editable for k in ks)
            or (pinned is not None and pinned(node))
        ):
            anchors.add(node)
    by_id = {s.id: s for s in segs}
    used = set()
    result = ChainSet([], anchors)
    for anchor in sorted(anchors):
        if anchor not in incident:
            continue
        for k0 in sorted(
            incident[anchor],
            key=lambda k: (
                pieces[k][1] if pieces[k][0] == anchor else pieces[k][0],
                pieces[k][2].id,
            ),
        ):
            if k0 in used:
                continue
            pts, walk, at, k = [anchor], [], anchor, k0
            while True:
                used.add(k)
                a, b, s = pieces[k]
                nxt = b if at == a else a
                walk.append((at, nxt, s.id))
                pts.append(nxt)
                at = nxt
                if at in anchors:
                    break
                others = [x for x in incident[at] if x != k and x not in used]
                if not others:
                    break
                k = others[0]
            frozen = any(not by_id[w[2]].editable for w in walk)
            width = pieces[k0][2].width
            chain = Chain(net, layer, width, pts, walk, frozen)
            if pts[0] == pts[-1]:
                result.closed += 1
                continue
            if class_width is not None and width != class_width:
                result.off_width += 1
                continue
            if frozen:
                result.frozen += 1
            result.chains.append(chain)
    result.loops = sum(1 for k in range(len(pieces)) if k not in used)
    for anchor in sorted(anchors):
        legs = []
        for k in incident.get(anchor, ()):
            a, b, _ = pieces[k]
            other = b if a == anchor else a
            legs.append((other[0] - anchor[0], other[1] - anchor[1]))
        result.anchor_legs[anchor] = sorted(legs)
    return result


def chain_anchor_legs(chainset, chain):
    """L5 input: directions of the other same-net legs at each end of `chain`."""
    out = {}
    for end, nxt in ((chain.points[0], chain.points[1]), (chain.points[-1], chain.points[-2])):
        own = (nxt[0] - end[0], nxt[1] - end[1])
        legs = list(chainset.anchor_legs.get(end, ()))
        if own in legs:
            legs.remove(own)
        out[end] = legs
    return out


def chain_ops(chain, new_points, segs_by_id):
    """Express replacing `chain` by new_points as segment operations.

    remove: every original segment id the chain touches; keep: [(seg_id, a, b, width)]
    the parts of those segments outside the chain (a segment split at a T or pad
    projection), re-added as their own segments; add: the new chain legs at the
    chain width.
    """
    keep = []
    for sid in chain.seg_ids:
        s = segs_by_id[sid]
        n = math.dist(s.a, s.b)

        def t(p, s=s, n=n):
            return math.dist(s.a, p) / n

        covered = sorted(tuple(sorted((t(a), t(b)))) for a, b, x in chain.pieces if x == sid)
        at, cuts = 0.0, []
        for lo, hi in covered:
            if lo > at + 1e-12:
                cuts.append((at, lo))
            at = max(at, hi)
        if at < 1 - 1e-12:
            cuts.append((at, 1.0))
        pieces = {p for a, b, x in chain.pieces if x == sid for p in (a, b)}
        for lo, hi in cuts:
            pa = min(pieces | {s.a, s.b}, key=lambda p: abs(t(p) - lo))
            pb = min(pieces | {s.a, s.b}, key=lambda p: abs(t(p) - hi))
            if pa != pb:
                keep.append((sid, pa, pb, s.width))
    return dict(
        remove=list(chain.seg_ids),
        keep=keep,
        width=chain.width,
        add=[(p, q) for p, q in zip(new_points, new_points[1:])],
    )


# ---------------------------------------------------------------- normalize (design 2.0)
@dataclass
class Normalized:
    segments: list
    delete: list
    modify: dict
    counts: dict


def _covers(t, p, r):
    """Track t's copper contains the disc (p, r)."""
    return point_segment_distance(p, t.a, t.b) + r <= t.width / 2 + TOL


def normalize(segs, *, pinned=None, pin_points=(), covers_disc=None):
    """N1 zero-length, N2 duplicate, N3 collinear merge. The copper union is unchanged.

    Only editable (unlocked, unfrozen) segments are deleted or modified. N3 keeps
    the uuid of the smaller id and never merges across an anchor (pinned vertex,
    pin point, width change, degree != 2, frozen endpoint) or a T point.
    covers_disc(p, r) -> bool reports non-track same-net copper (pads, vias).
    """
    live = {s.id: s for s in sorted(segs, key=_seg_key)}
    delete, counts = [], dict(zero=0, duplicate=0, merged=0, duplicate_joint=0)
    for s in sorted((s for s in live.values() if s.zero and s.editable), key=_seg_key):
        r = s.width / 2
        if any(t.id != s.id and _covers(t, s.a, r) for t in live.values()) or (
            covers_disc is not None and covers_disc(s.a, r)
        ):
            del live[s.id]
            delete.append(s.id)
            counts["zero"] += 1
    order = sorted(
        (s for s in live.values() if not s.zero and s.editable),
        key=lambda s: (s.width, math.dist(s.a, s.b), _seg_key(s)),
    )
    pin_set = {ipt(p) for p in pin_points}

    def joint(e, s, t):
        # Another segment ending at e is attached through s's endpoint. Without s it would end
        # on t's interior: a T without a vertex, which KiCad DRC reports as track_dangling.
        # Keep s unless e is also t's endpoint or lies in a pad/via (pinned) or pin point.
        if e in (t.a, t.b) or e in pin_set or (pinned is not None and pinned(e)):
            return False
        return any(u.id not in (s.id, t.id) and e in (u.a, u.b) for u in live.values())

    for s in order:
        if s.id not in live:
            continue
        for t in sorted(live.values(), key=_seg_key):
            if t.id == s.id or t.zero or t.width < s.width:
                continue
            if (
                point_segment_distance(s.a, t.a, t.b) <= TOL
                and point_segment_distance(s.b, t.a, t.b) <= TOL
            ):
                if joint(s.a, s, t) or joint(s.b, s, t):
                    counts["duplicate_joint"] += 1
                    break
                del live[s.id]
                delete.append(s.id)
                counts["duplicate"] += 1
                break
    pins = {ipt(p) for p in pin_points}
    ends = defaultdict(list)
    for s in live.values():
        if not s.zero:
            ends[s.a].append(s.id)
            ends[s.b].append(s.id)
    index = _Buckets()
    for s in live.values():
        index.add_box(bbox((s.a, s.b), TOL), s.id)
    dots = {s.a for s in live.values() if s.zero}
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    merge_at = set()
    for v in sorted(ends):
        ids = sorted(ends[v])
        if len(ids) != 2 or v in pins or v in dots or (pinned is not None and pinned(v)):
            continue
        s1, s2 = live[ids[0]], live[ids[1]]
        if not (s1.editable and s2.editable) or s1.width != s2.width:
            continue
        if any(
            _on_interior(v, live[i].a, live[i].b)
            for i in index.query((v[0], v[1], v[0], v[1]))
            if i not in ids
        ):
            continue
        a = s1.b if s1.a == v else s1.a
        b = s2.b if s2.a == v else s2.a
        if a == b or not _straight(a, v, b):
            continue
        merge_at.add(v)
        ra, rb = find(s1.id), find(s2.id)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    groups = defaultdict(list)
    for sid in parent:
        groups[find(sid)].append(sid)
    modify = {}
    for root, members in sorted(groups.items()):
        if len(members) < 2:
            continue
        endpoints = defaultdict(int)
        for sid in members:
            endpoints[live[sid].a] += 1
            endpoints[live[sid].b] += 1
        far = sorted(p for p, n in endpoints.items() if n == 1)
        interior = sorted(p for p, n in endpoints.items() if n == 2)
        if (
            len(far) != 2
            or any(p not in merge_at for p in interior)
            or any(point_segment_distance(p, far[0], far[1]) > TOL for p in interior)
        ):
            continue
        # far is sorted (two elements), so (far[0], far[1]) is already the canonical,
        # geometry-only direction: picking it does not depend on which member survives as
        # `keep`. (`keep` itself is chosen by _seg_key, not by member id: KiCad assigns each
        # segment a fresh random uuid per process, and `s.id` is that uuid, so choosing `keep`
        # by `min(members)` or orienting a/b from `keep.a` -- as this used to -- made the
        # surviving segment's drawn direction depend on it, differing between two processes
        # given the same copper.)
        keep = live[min(members, key=lambda sid: _seg_key(live[sid]))]
        a, b = far[0], far[1]
        live[keep.id] = Seg(keep.id, a, b, keep.width, keep.locked, keep.frozen)
        modify[keep.id] = (a, b)
        for sid in sorted(members):
            if sid != keep.id:
                del live[sid]
                delete.append(sid)
                counts["merged"] += 1
    return Normalized(sorted(live.values(), key=_seg_key), sorted(delete), modify, counts)


# ---------------------------------------------------------------- legality context (design 1.4)
@dataclass(frozen=True)
class Guard:
    """L7: keep min(d_new, cap) >= min(d_old, cap) to these shapes ((a, b, half_width), ...)."""

    name: str
    shapes: tuple
    cap: float


def guard_distance(segments, width, guard):
    best = math.inf
    for a, b in segments:
        for c, d, hw in guard.shapes:
            best = min(best, segment_distance(a, b, c, d) - width / 2 - hw)
    return best


class Context:
    """Injected legality predicates for one chain (net, layer, width).

    clear(a, b) -> bool        L1, the native shape checker (Oracle.clear) for a new segment
    contact(a, b) -> bool      L2, True when a new segment touches same-net copper other
                               than the chain's own anchors' items (exact shapes, zero clearance)
    obstacles                  L3 test points of every other item on the layer (pad polygon
                               vertices and centres, via centres, other track endpoints,
                               keepout and outline vertices, hole centres), excluding the
                               items at this chain's anchors
    guards                     L7 Guard records (pairs G1, open terminals G2, SI 3W G3)
    anchor_legs                L5: {anchor: [direction vectors of other same-net legs]}; also the
                               outside leg whose turn rule R charges at a degree-2 anchor
    bounds                     board box clip for the tube search
    hug(a, b) -> nm^2          saturated same-class adjacency of a new segment (hug_segment);
                               None: no adjacency tie-breaks (router cost only)
    snap(box) -> [RayItem]     same-class tracks of other nets near box: dekink lattice lines at
                               minimum pitch from them (equal-length hugging alternatives)
    clearance                  the chain's clearance (pitch of the snap lines)
    search_budget              deterministic cap on tube-search shape checks per chain
    cap(new_points) -> reason  functional-group cap (PNR_GLOSS_CLASSES): None when the chain at
                               new_points keeps every cross-group parallel run within its
                               allowance (allowed_parallel_mm), else 'cap:<a>|<b>'; checked last
                               by check_edit. None: no cap (no groups file)
    hug_group, snap_group      hug/snap restricted to neighbours of the chain's own functional
                               group: the fallback when the cap refuses a hugging dekink
    """

    def __init__(
        self,
        width,
        clear=None,
        contact=None,
        obstacles=(),
        guards=(),
        anchor_legs=None,
        bounds=None,
        tube=TUBE,
        hug=None,
        snap=None,
        clearance=None,
        search_budget=None,
        cap=None,
        hug_group=None,
        snap_group=None,
    ):
        self.width = int(width)
        self._clear = clear
        self._contact = contact
        self.obstacles = sorted({ipt(p) for p in obstacles})
        self._points = _Buckets()
        for p in self.obstacles:
            self._points.add_box((p[0], p[1], p[0], p[1]), p)
        self.guards = tuple(guards)
        self.anchor_legs = dict(anchor_legs or {})
        self.bounds = bounds
        self.tube = tube
        self._hug, self.snap, self.clearance = hug, snap, clearance
        self._levels = {"all": (hug, snap), "group": (hug_group, snap_group), "none": (None, None)}
        self.level = "all"
        self._cap = cap
        self.search_budget = search_budget
        self._cc, self._kc, self._tubes, self._capc = {}, {}, {}, {}
        self._hcs = {"all": {}, "group": {}, "none": {}}
        self._hc = self._hcs["all"]
        self.calls = defaultdict(int)
        self.why = None  # first failing check of this chain (planner diagnostics)

    def note(self, reason):
        if self.why is None and reason:
            self.why = reason

    @property
    def adjacency(self):
        return self._hug is not None

    def adjacency_levels(self):
        """Hug levels to try in order: every same-class neighbour, own group only, none."""
        out = ["all"]
        if self._levels["group"][0] is not None:
            out.append("group")
        return out + ["none"]

    def set_level(self, level):
        self.level = level
        self._hug, self.snap = self._levels[level]
        self._hc = self._hcs[level]

    def cap(self, points):
        """None or 'cap:<a>|<b>' for the chain at `points` (functional-group cap)."""
        if self._cap is None:
            return None
        key = tuple(simplify(points))
        if key not in self._capc:
            self.calls["cap"] += 1
            self._capc[key] = self._cap(list(key))
            if self._capc[key]:
                self.calls["cap_refused"] += 1
        return self._capc[key]

    def hug_seg(self, a, b):
        key = (a, b) if a <= b else (b, a)
        if key not in self._hc:
            self.calls["hug"] += 1
            self._hc[key] = 0.0 if self._hug is None else float(self._hug(key[0], key[1]))
        return self._hc[key]

    def hug(self, points):
        """Saturated same-class adjacency of a polyline (sum over its legs; lower hugs tighter)."""
        pts = simplify(points)
        return sum(self.hug_seg(a, b) for a, b in zip(pts, pts[1:]))

    def tube_of(self, reference, radius):
        key = (tuple(reference), radius)
        if key not in self._tubes:
            self._tubes[key] = _Tube(reference, radius)
        return self._tubes[key]

    def clear(self, a, b):
        key = (a, b) if a <= b else (b, a)
        if key not in self._cc:
            self.calls["clear"] += 1
            self._cc[key] = True if self._clear is None else bool(self._clear(key[0], key[1]))
        return self._cc[key]

    def contact(self, a, b):
        key = (a, b) if a <= b else (b, a)
        if key not in self._kc:
            self.calls["contact"] += 1
            self._kc[key] = False if self._contact is None else bool(self._contact(key[0], key[1]))
        return self._kc[key]

    def points_in(self, box):
        return sorted(
            p
            for p in self._points.query(box)
            if box[0] <= p[0] <= box[2] and box[1] <= p[1] <= box[3]
        )

    def thresholds(self, reference):
        segs = list(zip(reference, reference[1:]))
        return [(g, min(guard_distance(segs, self.width, g), g.cap)) for g in self.guards]

    def segment_guarded(self, a, b, thresholds):
        return all(guard_distance([(a, b)], self.width, g) >= t - TOL for g, t in thresholds)


def turns_ok(old, new, anchor_legs=None):
    """L5: no new turn sharper than 90 deg; no new acid trap (< 90 deg) at an anchor."""
    old, new = simplify(old), simplify(new)
    old_turn = {old[i]: turn_angle(old[i - 1], old[i], old[i + 1]) for i in range(1, len(old) - 1)}
    for i in range(1, len(new) - 1):
        t = turn_angle(new[i - 1], new[i], new[i + 1])
        if t > 90 + ANGLE_TOL and (new[i] not in old_turn or old_turn[new[i]] + ANGLE_TOL < t):
            return False
    for o_end, o_next, n_next in ((old[0], old[1], new[1]), (old[-1], old[-2], new[-2])):
        ov = (o_next[0] - o_end[0], o_next[1] - o_end[1])
        nv = (n_next[0] - o_end[0], n_next[1] - o_end[1])
        if vector_turn(ov, nv) <= ANGLE_TOL:
            continue
        for other in (anchor_legs or {}).get(o_end, ()):
            if ray_angle(nv, other) + ANGLE_TOL < min(90, ray_angle(ov, other)):
                return False
    return True


def _legs(points):
    return list(zip(points, points[1:]))


def check_edit(old, new, ctx, *, reference=None, tube=None):
    """None when `new` may replace `old` (same anchors), else the failing check (design 1.4).

    Cheap checks first: L4 invariants, L5 turns, L8 tube, then L1 shape check and
    L2 contact for every new segment, L7 guards, L3 homotopy and last the functional-group
    cap on cross-group parallel runs (ctx.cap; new is always the full chain polyline).
    """
    old, new = simplify(old), simplify(new)
    if len(new) < 2 or new[0] != old[0] or new[-1] != old[-1]:
        return "L4:anchors"
    old_set = {frozenset(s) for s in _legs(old)}
    new_set = {frozenset(s) for s in _legs(new)}
    added = [s for s in _legs(new) if frozenset(s) not in old_set]
    removed = [s for s in _legs(old) if frozenset(s) not in new_set]
    if not added and not removed:
        return "L4:unchanged"
    if any(direction(a, b) is None for a, b in added):
        return "L4:octilinear"
    if not turns_ok(old, new, ctx.anchor_legs):
        return "L5:turn"
    if tube is not None:
        inside = ctx.tube_of(reference or old, tube)
        if any(not inside.covers(a, b) for a, b in added):
            return "L8:tube"
    for a, b in added:
        if not ctx.clear(a, b):
            return "L1:clear"
    for a, b in added:
        if ctx.contact(a, b):
            return "L2:contact"
    for g in ctx.guards:
        d_old = guard_distance(removed, ctx.width, g)
        d_new = guard_distance(added, ctx.width, g)
        if min(d_new, g.cap) < min(d_old, g.cap) - TOL:
            return "L7:" + g.name
    if swept_points(old, new, ctx.points_in(bbox(old + new, TOL))):
        return "L3:swept"
    return ctx.cap(new)


# ---------------------------------------------------------------- dekink (design 2.1)
def monotone_windows(points, max_legs=DP_MAX_LEGS):
    """Maximal runs of >= 3 octilinear legs inside one cone {k, k+1} or {k, k+2}.

    Returns (start_vertex, end_vertex, k, step) sorted longest first, then leftmost.
    Runs longer than max_legs are cut into consecutive windows of max_legs legs.
    """
    pts = simplify(points)
    dirs = [direction(a, b) for a, b in _legs(pts)]
    found = set()
    for step in (1, 2):
        for k in range(8):
            cone = {k, (k + step) % 8}
            i = 0
            while i < len(dirs):
                if dirs[i] not in cone:
                    i += 1
                    continue
                j = i
                while j + 1 < len(dirs) and dirs[j + 1] in cone:
                    j += 1
                if j - i + 1 >= 3 and len({dirs[x] for x in range(i, j + 1)}) == 2:
                    s = i
                    while s <= j:
                        e = min(j, s + max_legs - 1)
                        if e - s + 1 >= 3:
                            found.add((s, e + 1, k, step))
                        s = e + 1
                i = j + 1
    return sorted(found, key=lambda w: (-(w[1] - w[0]), w[0], w[2], w[3]))


def _outside_vec(ctx, anchor, incoming):
    """Heading of the single outside leg at a degree-2 chain-end anchor (None otherwise):
    incoming=True gives the heading INTO the anchor (prev leg), else OUT of it (next leg)."""
    legs = (ctx.anchor_legs or {}).get(anchor, ())
    if len(legs) != 1:
        return None
    o = legs[0]
    return (-o[0], -o[1]) if incoming else (o[0], o[1])


def _snap_values(ctx, run, v0, U, V, det, A, B):
    """Extra lattice coefficients putting a run leg at minimum pitch from a same-class
    parallel track of another net (equal-length hugging alternatives)."""
    extra_a, extra_b = set(), set()
    if ctx.snap is None or ctx.clearance is None or not ctx.adjacency:
        return extra_a, extra_b
    box = bbox(run, ADJ_REACH + ctx.width)
    for it in ctx.snap(box):
        d = direction(it.a, it.b)
        if d is None:
            continue
        p = pitch(ctx.width, ctx.clearance, 2 * it.r, it.clearance)
        for along, other, sign, values, lo, hi in (
            (U, V, 1, extra_b, B[0], B[-1]),
            (V, U, -1, extra_a, A[0], A[-1]),
        ):
            if d % 4 != _vdir(along) % 4:
                continue
            n_along = math.hypot(*along)
            c0 = along[0] * (v0[1] - it.a[1]) - along[1] * (v0[0] - it.a[0])
            coef = sign * det  # d cross(along, P - it.a) / d(coefficient of `other`)
            for sigma in (1, -1):
                target = (sigma * p * n_along - c0) / coef
                for cand in (math.floor(target), math.ceil(target)):
                    if sigma * (c0 + cand * coef) >= p * n_along - 1e-6 and lo < cand < hi:
                        values.add(Fraction(cand))
                        break
    return extra_a, extra_b


def _dekink_window(path, s, e, k, step, ctx, mode="dekink"):
    U, V = DIRS[k], DIRS[(k + step) % 8]
    det = U[0] * V[1] - U[1] * V[0]
    v0 = path[s]

    def coords(p):
        dx, dy = p[0] - v0[0], p[1] - v0[1]
        return Fraction(dx * V[1] - dy * V[0], det), Fraction(U[0] * dy - U[1] * dx, det)

    run = path[s : e + 1]
    ab = [coords(p) for p in run]
    if any(ab[i + 1][0] < ab[i][0] or ab[i + 1][1] < ab[i][1] for i in range(len(ab) - 1)):
        ctx.note("dekink:not_monotone")
        return None
    A = sorted({a for a, _ in ab})
    B = sorted({b for _, b in ab})
    snap_a, snap_b = _snap_values(ctx, run, v0, U, V, det, A, B)
    if snap_a:
        A = sorted(set(A) | set(sorted(snap_a)[: max(0, DP_MAX_LATTICE - len(A))]))
    if snap_b:
        B = sorted(set(B) | set(sorted(snap_b)[: max(0, DP_MAX_LATTICE - len(B))]))
    nA, nB = len(A), len(B)
    index_a = {a: i for i, a in enumerate(A)}
    index_b = {b: j for j, b in enumerate(B)}

    def exact(i, j):
        x = v0[0] + A[i] * U[0] + B[j] * V[0]
        y = v0[1] + A[i] * U[1] + B[j] * V[1]
        return (x, y)

    def node(i, j):
        x, y = exact(i, j)
        if x.denominator == 1 and y.denominator == 1:
            return (int(x), int(y))
        return None

    def rounded(i, j):
        x, y = exact(i, j)
        return (round(x), round(y))

    # old staircase level over each elementary alpha interval
    level = [None] * (nA - 1)
    for (a0, b0), (a1, b1) in zip(ab, ab[1:]):
        if a1 > a0:
            for m in range(index_a[a0], index_a[a1]):
                level[m] = index_b[b0]
    if any(x is None for x in level):
        ctx.note("dekink:level")
        return None
    prev_vec = (
        (path[s][0] - path[s - 1][0], path[s][1] - path[s - 1][1])
        if s > 0
        else _outside_vec(ctx, path[0], True)
    )
    next_vec = (
        (path[e + 1][0] - path[e][0], path[e + 1][1] - path[e][1])
        if e + 1 < len(path)
        else _outside_vec(ctx, path[-1], False)
    )
    first_vec = (run[1][0] - run[0][0], run[1][1] - run[0][1])
    last_vec = (run[-1][0] - run[-2][0], run[-1][1] - run[-2][1])
    old_start = vector_turn(prev_vec, first_vec) if prev_vec else 0
    old_end = vector_turn(last_vec, next_vec) if next_vec else 0
    vec = {"u": U, "v": V}

    def anchor_ok(point, old_ray, new_ray):
        # L5 at a chain-end anchor: no new acid trap against the anchor's other same-net legs
        if vector_turn(old_ray, new_ray) <= ANGLE_TOL:
            return True
        return all(
            ray_angle(new_ray, o) + ANGLE_TOL >= min(90, ray_angle(old_ray, o))
            for o in ctx.anchor_legs.get(point, ())
        )

    forbidden = set()
    # L3 precompute: at the obstacle's alpha the new staircase must stay on the old side.
    box = bbox(run, TOL)
    for p in ctx.points_in(box):
        ap, bp = coords(p)
        if not (A[0] <= ap <= A[-1] and B[0] <= bp <= B[-1]):
            continue
        span = [b for (a, b) in ab if a == ap]
        for m in range(nA - 1):
            if A[m] < ap < A[m + 1]:
                span.append(B[level[m]])
        if not span:
            continue
        lo, hi = min(span), max(span)
        if lo <= bp <= hi:
            continue
        side = 1 if bp < lo else -1
        for m in range(nA - 1):
            if A[m] <= ap <= A[m + 1]:
                for j in range(nB):
                    if (B[j] - bp) * side <= 0:
                        forbidden.add(("u", m, j))
        if ap in index_a:
            i = index_a[ap]
            for j in range(nB - 1):
                if (side > 0 and B[j] <= bp) or (side < 0 and B[j + 1] >= bp):
                    forbidden.add(("v", i, j))
    link_cache = {}
    hug_cache = {}

    def link_ok(kind, i, j):
        key = (kind, i, j)
        if key in forbidden:
            return False
        if key not in link_cache:
            p = rounded(i, j)
            q = rounded(i + 1, j) if kind == "u" else rounded(i, j + 1)
            link_cache[key] = ctx.clear(p, q) and not ctx.contact(p, q)
        return link_cache[key]

    def link_hug(kind, i, j):
        # saturated same-class adjacency of one elementary lattice link (0 without adjacency)
        if not ctx.adjacency:
            return 0
        key = (kind, i, j)
        if key not in hug_cache:
            p = rounded(i, j)
            q = rounded(i + 1, j) if kind == "u" else rounded(i, j + 1)
            hug_cache[key] = int(round(ctx.hug_seg(p, q)))
        return hug_cache[key]

    def area(i, i2, j):
        return sum(abs(B[j] - B[level[m]]) * (A[m + 1] - A[m]) for m in range(i, i2)) * abs(det)

    for _attempt in range(DEKINK_RETRIES + 1):
        best = {}
        start = (0, 0, None)
        best[start] = ((0, 0, 0, Fraction(0)), None)
        order = [(i, j) for i in range(nA) for j in range(nB)]
        for i, j in order:
            for d in (None, "u", "v"):
                st = (i, j, d)
                if st not in best:
                    continue
                c = best[st][0]
                for d2 in ("u", "v"):
                    if d2 == d:
                        continue
                    if d is None:
                        if (i, j) != (0, 0):
                            continue
                        t = vector_turn(prev_vec, vec[d2]) if prev_vec else 0
                        if t > 90 + ANGLE_TOL and t > old_start + ANGLE_TOL:
                            continue
                        if s == 0 and not anchor_ok(path[0], first_vec, vec[d2]):
                            continue
                        db = 1 if prev_vec and turn_class(t) != 0 else 0
                        ds = 1 if prev_vec and turn_class(t) != 0 and is_sharp(t) else 0
                    else:
                        db, ds = 1, 1 if step == 2 else 0
                    if d2 == "u":
                        h = 0
                        for i2 in range(i + 1, nA):
                            if not link_ok("u", i2 - 1, j):
                                break
                            h += link_hug("u", i2 - 1, j)
                            if node(i2, j) is None:
                                continue
                            nc = (c[0] + db, c[1] + ds, c[2] + h, c[3] + area(i, i2, j))
                            ns = (i2, j, "u")
                            if ns not in best or nc < best[ns][0]:
                                best[ns] = (nc, st)
                    else:
                        h = 0
                        for j2 in range(j + 1, nB):
                            if not link_ok("v", i, j2 - 1):
                                break
                            h += link_hug("v", i, j2 - 1)
                            if node(i, j2) is None:
                                continue
                            nc = (c[0] + db, c[1] + ds, c[2] + h, c[3])
                            ns = (i, j2, "v")
                            if ns not in best or nc < best[ns][0]:
                                best[ns] = (nc, st)
        finals = []
        for d in ("u", "v"):
            st = (nA - 1, nB - 1, d)
            if st not in best:
                continue
            c = best[st][0]
            t = vector_turn(vec[d], next_vec) if next_vec else 0
            if t > 90 + ANGLE_TOL and t > old_end + ANGLE_TOL:
                continue
            back = (-vec[d][0], -vec[d][1])
            if e == len(path) - 1 and not anchor_ok(path[-1], (-last_vec[0], -last_vec[1]), back):
                continue
            extra = (
                1 if next_vec and turn_class(t) != 0 else 0,
                1 if next_vec and turn_class(t) != 0 and is_sharp(t) else 0,
            )
            finals.append(((c[0] + extra[0], c[1] + extra[1], c[2], c[3]), d, st))
        if not finals:
            ctx.note("dekink:no_path")
            return None
        finals.sort(key=lambda f: (f[0], f[1]))
        st = finals[0][2]
        nodes = []
        while st is not None:
            nodes.append((st[0], st[1]))
            st = best[st][1]
        nodes.reverse()
        new_run = [node(i, j) for i, j in nodes]
        new_path = simplify(path[:s] + new_run + path[e + 1 :])
        if mode == "dekink":
            if not rule_r(path, new_path, ctx.anchor_legs):
                ctx.note("rule:R")
                return None
        elif not _rebalanced(path, new_path, ctx):
            return None
        reason = check_edit(path, new_path, ctx)
        if reason is None:
            return new_path
        if reason != "L3:swept":
            ctx.note(reason)
            return None
        bad = swept_points(path, new_path, ctx.points_in(bbox(path + new_path, TOL)))
        links = []
        for (i0, j0), (i1, j1) in zip(nodes, nodes[1:]):
            if j0 == j1:
                links += [("u", m, j0) for m in range(i0, i1)]
            else:
                links += [("v", i0, m) for m in range(j0, j1)]
        links = [link for link in links if link not in forbidden]
        if not links or not bad:
            ctx.note(reason)
            return None

        def gap(link):
            p = rounded(link[1], link[2])
            q = rounded(link[1] + 1, link[2]) if link[0] == "u" else rounded(link[1], link[2] + 1)
            return (min(point_segment_distance(b, p, q) for b in bad), link)

        forbidden.add(min(links, key=gap)[1])
    ctx.note("L3:swept")
    return None


def _rebalanced(old, new, ctx):
    """Rebalance acceptance: equal length, no more bends or sharp turns (end anchors
    included), and a material same-class adjacency gain (hug lower by >= ADJ_MIN_GAIN)."""
    return (
        abs(length(new) - length(old)) <= LENGTH_TOL
        and total_bends(new, ctx.anchor_legs) <= total_bends(old, ctx.anchor_legs)
        and total_sharp(new, ctx.anchor_legs) <= total_sharp(old, ctx.anchor_legs)
        and ctx.hug(new) < ctx.hug(old) - ADJ_MIN_GAIN
    )


def _declined(old, new, ctx):
    """Router cost bought with adjacency is capped by the align_parallel slack: an edit whose
    cost gain is below HUG_SLACK is declined when it makes the chain hug materially less."""
    if not ctx.adjacency:
        return False
    gain = total_cost(old, ctx.anchor_legs) - total_cost(new, ctx.anchor_legs)
    return gain < HUG_SLACK - 1e-6 and ctx.hug(new) > ctx.hug(old) + ADJ_MIN_GAIN


def dekink(points, ctx, max_legs=DP_MAX_LEGS, mode="dekink"):
    """Fewest-bend equal-length replacement of monotone runs (design 2.1), or None.

    Ties between fewest-bend paths go to the one hugging same-class neighbours tightest
    (lattice lines at minimum pitch from them are added), then the least swept area.
    mode='rebalance': keep the bend count, only move runs to hug tighter (used on gloss
    output; equal length by construction).
    Functional-group cap (ctx.cap): when the cap refuses the result of a hugging level and
    nothing is left, the DP runs again hugging only the chain's own group, then not at all.
    """
    if ctx._cap is None or not ctx.adjacency:
        return _dekink(points, ctx, max_legs, mode)
    entry = ctx.level
    try:
        for level in ctx.adjacency_levels():
            ctx.set_level(level)
            refused = ctx.calls["cap_refused"]
            out = _dekink(points, ctx, max_legs, mode)
            if out is not None or ctx.calls["cap_refused"] == refused:
                return out
        return None
    finally:
        ctx.set_level(entry)


def _dekink(points, ctx, max_legs=DP_MAX_LEGS, mode="dekink"):
    original = simplify(points)
    path = list(original)
    tried = set()
    while True:
        for s, e, k, step in monotone_windows(path, max_legs):
            key = (tuple(path[max(0, s - 1) : e + 2]), k, step)
            if key in tried:
                continue
            tried.add(key)
            new = _dekink_window(path, s, e, k, step, ctx, mode)
            if new is not None:
                path = new
                break
        else:
            break
    if path == original:
        return None
    if mode == "dekink":
        if not rule_r(original, path, ctx.anchor_legs):
            ctx.note("rule:R")
            return None
    elif not _rebalanced(original, path, ctx):
        return None
    reason = check_edit(original, path, ctx)
    if reason is not None:
        ctx.note(reason)
        return None
    if mode == "dekink" and _declined(original, path, ctx):
        ctx.note("hug:declined")
        return None
    return path


# ---------------------------------------------------------------- gloss (design 2.2)
def _choose_key(path, base, anchor_legs=None):
    return (
        round(total_cost(path, anchor_legs)),
        round(length(path)),
        total_bends(path, anchor_legs),
        total_sharp(path, anchor_legs),
        round(swept_area(base, path)),
        tuple(path),
    )


def string_pull(path, ctx, reference=None, lo=0, hi=None):
    """keyhole.relax over vertices lo..hi of path, extended with LEGAL and rule R.

    Equal length-and-bend options are ordered by same-class adjacency (hug), then by
    the swept area (the least copper moved)."""
    path = simplify(path)
    reference = reference or path
    hi = len(path) - 1 if hi is None else hi
    legs = ctx.anchor_legs
    changed = True
    while changed:
        changed = False
        for i in range(lo, hi - 1):
            for j in range(hi, i + 1, -1):
                options = []
                for c in elbows(path[i], path[j]):
                    new = simplify(path[:i] + c + path[j + 1 :])
                    if new == path or not rule_r(path, new, legs):
                        continue
                    options.append((round(length(new)), total_bends(new, legs), c, new))
                if not options:
                    continue
                options.sort(key=lambda o: (o[0], o[1], tuple(o[2])))
                legal = []
                for o in options:
                    why = check_edit(path, o[3], ctx, reference=reference, tube=ctx.tube)
                    if why is None:
                        legal.append(o)
                    else:
                        ctx.note(why)
                if not legal:
                    continue
                top = [o for o in legal if o[:2] == legal[0][:2]]
                if len(top) > 1:
                    top.sort(
                        key=lambda o: (
                            round(ctx.hug(o[2])) if ctx.adjacency else 0,
                            round(swept_area(path[i : j + 1], o[2])),
                            tuple(o[2]),
                        )
                    )
                new = top[0][3]
                hi -= len(path) - len(new)
                path = new
                changed = True
                break
            if changed:
                break
    return path


def tube_search(
    path, ctx, reference=None, lo=0, hi=None, pitches=TUBE_PITCHES, max_expansions=TUBE_EXPANSIONS
):
    """The repair router (keyhole.route, bend cost 0.15) inside the tube around reference.

    Returns candidate full paths (vertices lo..hi replaced); lattice origin on path[lo].
    ctx.search_budget caps the shape checks the search may make for this chain
    (deterministic; beyond it every further move is refused, so the search ends).
    """
    from pnr.route.detail import keyhole

    path = simplify(path)
    reference = reference or path
    hi = len(path) - 1 if hi is None else hi
    v0, vm = path[lo], path[hi]
    box = bbox(reference, ctx.tube)
    if ctx.bounds is not None:
        box = (
            max(box[0], ctx.bounds[0]),
            max(box[1], ctx.bounds[1]),
            min(box[2], ctx.bounds[2]),
            min(box[3], ctx.bounds[3]),
        )
    if box[0] > v0[0] or box[1] > v0[1] or box[2] < v0[0] or box[3] < v0[1]:
        return []
    thresholds = ctx.thresholds(reference)

    def to_nm(p):
        return (round(p[0] * NM), round(p[1] * NM))

    inside = ctx.tube_of(reference, ctx.tube)

    out = []
    for p in pitches:
        # keyhole.route returns any legal one-bend elbow v0->vm without searching; refuse the
        # long elbow legs here so its lattice A* (bend cost 0.15) runs. The elbows themselves
        # stay candidates through string_pull.
        banned = {
            frozenset(leg)
            for e in elbows(v0, vm)
            if len(e) == 3
            for leg in zip(e, e[1:])
            if math.dist(*leg) > 2 * p
        }

        def clear_mm(a, z):
            a, z = to_nm(a), to_nm(z)
            if frozenset((a, z)) in banned or not inside.covers(a, z):
                return False
            if a == z:
                return True
            key = (a, z) if a <= z else (z, a)
            if key not in ctx._cc and ctx.search_budget is not None:
                if ctx.calls["search"] >= ctx.search_budget:
                    ctx.calls["search_refused"] += 1
                    return False
                ctx.calls["search"] += 1
            return (
                ctx.clear(a, z) and not ctx.contact(a, z) and ctx.segment_guarded(a, z, thresholds)
            )

        x0 = v0[0] - ((v0[0] - box[0]) // p) * p
        y0 = v0[1] - ((v0[1] - box[1]) // p) * p
        bounds = (x0 / NM, y0 / NM, box[2] / NM, box[3] / NM)
        rep = keyhole.route(
            [(v0[0] / NM, v0[1] / NM)],
            [(vm[0] / NM, vm[1] / NM)],
            bounds,
            clear_mm,
            pitch=p / NM,
            bend_cost=BEND_COST / NM,
            max_expansions=max_expansions,
        )
        if rep.status != "routed" or not rep.path:
            continue
        sub = [to_nm(q) for q in rep.path]
        sub[0], sub[-1] = v0, vm
        sub = simplify(sub)
        if all(octilinear(a, b) for a, b in _legs(sub)):
            out.append(simplify(path[:lo] + sub + path[hi + 1 :]))
    if ctx.calls.get("search_refused"):
        ctx.note("search_budget")
    return out


class _Tube:
    """Membership in the radius-T tube around a polyline (bucketed, cached)."""

    def __init__(self, reference, radius):
        self.radius = radius
        self.legs = _legs(reference) or [(reference[0], reference[0])]
        self.index = _Buckets()
        for k, (a, b) in enumerate(self.legs):
            self.index.add_box(bbox((a, b), radius + 1), k)
        self.cache = {}

    def __call__(self, p):
        if p not in self.cache:
            ks = self.index.query((p[0], p[1], p[0], p[1]))
            self.cache[p] = any(point_segment_distance(p, *self.legs[k]) <= self.radius for k in ks)
        return self.cache[p]

    def covers(self, a, b):
        """Every sample of segment a-b (spacing <= TUBE_SAMPLE, ends included) is in the tube."""
        n = max(1, math.ceil(math.dist(a, b) / TUBE_SAMPLE))
        return all(
            self((round(a[0] + (b[0] - a[0]) * i / n), round(a[1] + (b[1] - a[1]) * i / n)))
            for i in range(n + 1)
        )


def _pick(ok, base, ctx):
    """Cheapest candidate by the router's key; another within the align_parallel slack of
    it wins only when it hugs same-class neighbours materially tighter (design 3.3)."""
    legs = ctx.anchor_legs
    ok = sorted(ok, key=lambda c: _choose_key(c, base, legs))
    best = ok[0]
    if not ctx.adjacency or len(ok) == 1:
        return best
    limit = total_cost(best, legs) + HUG_SLACK
    pool = [c for c in ok if total_cost(c, legs) <= limit + 1e-6]
    h0 = ctx.hug(best)
    alt = min(pool, key=lambda c: (round(ctx.hug(c)), _choose_key(c, base, legs)))
    return alt if ctx.hug(alt) < h0 - ADJ_MIN_GAIN else best


def _gloss_range(path, ctx, reference, lo, hi, use_tube):
    base = path
    legs = ctx.anchor_legs
    pulled = string_pull(path, ctx, reference, lo, hi)
    hi2 = hi - (len(path) - len(pulled))
    candidates = [pulled] if pulled != path else []
    if use_tube and cost(pulled[lo : hi2 + 1]) > lower_bound(pulled[lo], pulled[hi2]) + LENGTH_TOL:
        candidates += tube_search(pulled, ctx, reference, lo, hi2)
    ok = []
    for c in candidates:
        if c == base:
            continue
        if not rule_r(base, c, legs):
            ctx.note("rule:R")
            continue
        why = check_edit(base, c, ctx, reference=reference, tube=ctx.tube)
        if why is None:
            ok.append(c)
        else:
            ctx.note(why)
    if not ok:
        if not candidates:
            ctx.note("gloss:no_candidate")
        return None
    return _pick(ok, base, ctx)


def gloss(points, ctx, *, use_tube=True):
    """Shortest homotopic octilinear path in the tube (design 2.2), or None.

    Chains over 25 mm or 64 legs are processed in windows of 16 legs (stride 8)
    whose ends act as temporary anchors. With an adjacency context the result is
    rebalanced (equal length and bends, runs moved to hug same-class neighbours) and an
    improvement below the align_parallel slack that loosens adjacency is declined.
    """
    original = simplify(points)
    if len(original) < 3:
        ctx.note("gloss:short")
        return None
    path = list(original)
    if len(original) - 1 > WINDOW_MAX_LEGS or length(original) > WINDOW_MAX_LENGTH:
        s = 0
        while s < len(path) - 2:
            e = min(s + WINDOW_LEGS, len(path) - 1)
            new = _gloss_range(path, ctx, original, s, e, use_tube)
            if new is not None:
                path = new
            s += WINDOW_STRIDE
    else:
        new = _gloss_range(path, ctx, original, 0, len(path) - 1, use_tube)
        if new is not None:
            path = new
    if path != original and ctx.adjacency:
        balanced = dekink(path, ctx, mode="rebalance")
        if (
            balanced is not None
            and check_edit(original, balanced, ctx, reference=original, tube=ctx.tube) is None
        ):
            path = balanced
    if path == original:
        return None
    if not rule_r(original, path, ctx.anchor_legs):
        ctx.note("rule:R")
        return None
    why = check_edit(original, path, ctx, reference=original, tube=ctx.tube)
    if why is not None:
        ctx.note(why)
        return None
    if _declined(original, path, ctx):
        ctx.note("hug:declined")
        return None
    return path


# ---------------------------------------------------------------- edits and batching (design 3.4, Appendix B)
def _mm(p):
    return [p[0] / NM, p[1] / NM]


def segments_sha256(segments):
    """Hash guard over (id, a, b, width) tuples; order-independent."""
    rows = sorted([str(i), list(a), list(b), int(w)] for i, a, b, w in segments)
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()


@dataclass
class Edit:
    step: str
    net: str
    layer: str
    width: int
    old: list  # full old chain vertices (raw)
    new: list  # full new chain vertices (simplified)
    old_segments: tuple = ()  # ((id, a, b, width), ...) the chain's original segments
    members: Optional[list] = None
    extra: dict = field(default_factory=dict)
    anchor_legs: Optional[dict] = None  # outside legs at the end anchors (turns charged there)

    @property
    def nets(self):
        if self.members:
            return tuple(sorted({m["net"] for m in self.members}))
        return (self.net,)

    def metrics(self):
        if self.members:
            return dict(self.extra)
        o, n = simplify(self.old), self.new
        legs = self.anchor_legs
        return dict(
            L=length(o) / NM,
            L_new=length(n) / NM,
            B=bends(o),
            B_new=bends(n),
            B_all=total_bends(o, legs),
            B_all_new=total_bends(n, legs),
            turns90=sharp_turns(o),
            turns90_new=sharp_turns(n),
            swept_mm2=swept_area(o, n) / NM / NM,
            C=total_cost(o, legs) / NM,
            C_new=total_cost(n, legs) / NM,
        )

    def gain(self):
        if self.members:
            return -self.extra.get("dX_mm2", self.extra.get("dE_mm2", 0.0))
        return (
            total_cost(simplify(self.old), self.anchor_legs)
            - total_cost(self.new, self.anchor_legs)
        ) / NM

    def box(self, margin=INFLUENCE_MARGIN):
        pts = list(self.old) + list(self.new)
        for m in self.members or ():
            pts += list(m["old"]) + list(m["new"])
        return bbox(pts, margin)

    def to_spec(self):
        spec = dict(
            step=self.step,
            net=self.net,
            layer=self.layer,
            width_mm=self.width / NM,
            anchors=[_mm(self.old[0]), _mm(self.old[-1])] if self.old else [],
            old=dict(
                uuids=sorted({s[0] for s in self.old_segments}),
                segments=[[_mm(s[1]), _mm(s[2])] for s in self.old_segments],
                sha256=segments_sha256(self.old_segments),
            ),
            new=[_mm(p) for p in self.new],
            members=None,
            metrics=self.metrics(),
        )
        if self.members:
            spec["members"] = [
                dict(
                    net=m["net"],
                    chain=m["chain"],
                    uuids=sorted({s[0] for s in m["old_segments"]}),
                    sha256=segments_sha256(m["old_segments"]),
                    old=[_mm(p) for p in m["old"]],
                    new=[_mm(p) for p in m["new"]],
                    shift_mm=m["shift"] / NM,
                    dL=m["dL"] / NM,
                    dB=m["dB"],
                    seq=m.get("seq"),
                    partial=bool(m.get("partial")),
                )
                for m in self.members
            ]
        spec.update({k: v for k, v in self.extra.items() if k not in spec})
        return spec


def make_edit(step, chain, new_points, segs_by_id=None, anchor_legs=None):
    old_segments = ()
    if segs_by_id is not None:
        old_segments = tuple(
            sorted((s.id, s.a, s.b, s.width) for s in (segs_by_id[i] for i in chain.seg_ids))
        )
    return Edit(
        step,
        chain.net,
        chain.layer,
        chain.width,
        list(chain.points),
        simplify(new_points),
        old_segments,
        anchor_legs=anchor_legs,
    )


def batches(edits, limit=BATCH_LIMIT, margin=INFLUENCE_MARGIN):
    """Greedy packing by gain into batches of distinct nets and disjoint influence boxes."""
    order = sorted(
        edits, key=lambda e: (-e.gain(), e.layer, e.nets, tuple(e.old[:1]), tuple(e.new[:1]))
    )
    out = []
    for e in order:
        box = e.box(margin)
        for b in out:
            if len(b) < limit and all(
                not (set(e.nets) & set(x.nets))
                and not (x.layer == e.layer and boxes_overlap(box, x.box(margin)))
                for x in b
            ):
                b.append(e)
                break
        else:
            out.append([e])
    return out


# ---------------------------------------------------------------- same-class adjacency X / T and hug
@dataclass(frozen=True)
class RayItem:
    """Copper as a capsule for the ray metric: a track (a, b, half width), a via disc
    (a == b) or a pad stadium. key: the class key of an eligible same-class track (a ray
    hit on it counts as adjacency), None for every other item (it only stops the ray)."""

    a: Point
    b: Point
    r: float
    net: str
    key: Optional[tuple] = None
    clearance: int = 0
    owner: str = ""


def _ray_item_key(it):
    return (it.a, it.b, it.r, it.net, it.owner)


class RayIndex:
    """1 mm buckets of RayItems (deterministic order)."""

    def __init__(self, items=(), cell=NM):
        self.items = sorted(items, key=_ray_item_key)
        self.buckets = _Buckets(cell)
        for k, it in enumerate(self.items):
            self.buckets.add_box(bbox((it.a, it.b), int(math.ceil(it.r)) + 1), k)

    def near(self, box):
        box = (
            int(math.floor(box[0])),
            int(math.floor(box[1])),
            int(math.ceil(box[2])),
            int(math.ceil(box[3])),
        )
        return [self.items[k] for k in sorted(self.buckets.query(box))]


def _ray_circle(o, d, c, r):
    fx, fy = o[0] - c[0], o[1] - c[1]
    b = fx * d[0] + fy * d[1]
    disc = b * b - (fx * fx + fy * fy - r * r)
    if disc < 0:
        return None
    s = math.sqrt(disc)
    for t in (-b - s, -b + s):
        if t >= 0:
            return t
    return None


def _ray_segment(o, d, a, b):
    ex, ey = b[0] - a[0], b[1] - a[1]
    den = d[0] * ey - d[1] * ex
    if abs(den) < 1e-12:
        return None
    wx, wy = a[0] - o[0], a[1] - o[1]
    t = (wx * ey - wy * ex) / den
    u = (wx * d[1] - wy * d[0]) / den
    return t if t >= 0 and 0 <= u <= 1 else None


def _ray_hit(o, d, it):
    """Distance along the unit ray o + t d to the capsule's copper, 0 when o is inside, or None."""
    if point_segment_distance(o, it.a, it.b) <= it.r:
        return 0.0
    best = None
    ends = (it.a,) if it.a == it.b else (it.a, it.b)
    cands = [_ray_circle(o, d, c, it.r) for c in ends]
    if it.a != it.b:
        L = math.dist(it.a, it.b)
        nx, ny = -(it.b[1] - it.a[1]) / L * it.r, (it.b[0] - it.a[0]) / L * it.r
        cands.append(_ray_segment(o, d, (it.a[0] + nx, it.a[1] + ny), (it.b[0] + nx, it.b[1] + ny)))
        cands.append(_ray_segment(o, d, (it.a[0] - nx, it.a[1] - ny), (it.b[0] - nx, it.b[1] - ny)))
    for t in cands:
        if t is not None and (best is None or t < best):
            best = t
    return best


def _canonical(a, b):
    """A segment's ends in sorted order. The ray metric samples and sides a segment from its
    canonical first end, so it never depends on which way the segment is drawn (normalize keeps
    whichever end a merge meets first, and that can follow a random uuid)."""
    return (a, b) if a <= b else (b, a)


def _side_hits(
    a,
    b,
    half,
    net,
    index,
    side,
    extra=(),
    exclude=(),
    step=ADJ_STEP,
    reach=ADJ_REACH,
    prefer=None,
):
    """Per sample of segment a-b (every ~step, centred): (s, w, gap, item) of the first copper
    of another net hit by the normal ray on one side within reach (gap None: no hit).

    Several items hit at the same distance (a ray through a neighbour's vertex meets both of
    its segments' end caps): the one ``prefer(item)`` accepts wins, else the first in the
    candidate order. Callers pass their same-class test, so the sample counts the neighbour
    whichever segment of it the order meets first."""
    L = math.dist(a, b)
    if L <= 0:
        return []
    ux, uy = (b[0] - a[0]) / L, (b[1] - a[1]) / L
    nx, ny = -uy * side, ux * side
    far = half + reach
    xs = (a[0], b[0], a[0] + nx * far, b[0] + nx * far)
    ys = (a[1], b[1], a[1] + ny * far, b[1] + ny * far)
    box = (min(xs), min(ys), max(xs), max(ys))
    pool = (
        [it for it in index.near(box) if not (exclude and it.owner in exclude)]
        if index is not None
        else []
    )
    pool += [it for it in extra if boxes_overlap(bbox((it.a, it.b), it.r), box)]
    cands = []
    for it in pool:
        if it.net == net:
            continue
        ta = (it.a[0] - a[0]) * ux + (it.a[1] - a[1]) * uy
        tb = (it.b[0] - a[0]) * ux + (it.b[1] - a[1]) * uy
        na = (it.a[0] - a[0]) * nx + (it.a[1] - a[1]) * ny
        nb = (it.b[0] - a[0]) * nx + (it.b[1] - a[1]) * ny
        tlo, thi = min(ta, tb) - it.r, max(ta, tb) + it.r
        nlo, nhi = min(na, nb) - it.r, max(na, nb) + it.r
        if thi < 0 or tlo > L or nhi < half or nlo > far:
            continue
        cands.append((nlo, tlo, thi, _ray_item_key(it), it))
    cands.sort(key=lambda c: c[:4])
    n = max(1, int(round(L / step)))
    w = L / n
    out = []
    d = (nx, ny)
    for i in range(n):
        s = (i + 0.5) * w
        o = (a[0] + ux * s + nx * (half + 1), a[1] + uy * s + ny * (half + 1))
        best, hit = None, None
        for nlo, tlo, thi, _, it in cands:
            if best is not None and nlo - half - 1 > best + RAY_TIE:
                break
            if s < tlo or s > thi:
                continue
            t = _ray_hit(o, d, it)
            if t is None or t > reach:
                continue
            if best is None or t < best - RAY_TIE:
                best, hit = t, it
            elif t <= best + RAY_TIE and prefer is not None and prefer(it) and not prefer(hit):
                best, hit = min(best, t), it
        out.append((s, w, best, hit))
    return out


def _parallel_hit(a, b, it, tol=ADJ_PARALLEL):
    if it.a == it.b:
        return False
    ux, uy = b[0] - a[0], b[1] - a[1]
    hx, hy = it.b[0] - it.a[0], it.b[1] - it.a[1]
    ang = abs(math.degrees(math.atan2(ux * hy - uy * hx, ux * hx + uy * hy)))
    return min(ang, 180 - ang) <= tol


def _class_hit(sub_key, a, b, it):
    return it is not None and it.key is not None and it.key == sub_key and _parallel_hit(a, b, it)


def adjacency(
    subjects, index, *, extra=(), exclude=(), window=None, step=ADJ_STEP, reach=ADJ_REACH
):
    """Same-class adjacency of subject tracks (RayItems with a key), both sides sampled.

    X: sum over samples whose first hit within reach is a same-class parallel (+-30 deg) track
       of another net, of (gap - c_req) x sample length, for gaps above c_req + 0.1 mm (nm^2);
    T: sampled length whose such neighbour is packed (gap <= c_req + 0.1 mm) (nm);
    pairs: tight length per net pair (coupled length at minimum pitch, both sides counted).
    window: count only samples whose centreline point lies in the box. No leg-length or
    overlap cut-offs (replaces E for acceptance); a direction-agnostic dead-space proxy: each
    subject is sampled from its canonical first end, and a ray meeting a same-class parallel
    track and other copper at the same distance (a neighbour's vertex) counts the neighbour.
    """
    X = T = 0.0
    pairs = defaultdict(float)
    for sub in sorted(subjects, key=_ray_item_key):
        if sub.key is None or sub.a == sub.b:
            continue
        a, b = _canonical(sub.a, sub.b)
        L = math.dist(a, b)
        ux, uy = (b[0] - a[0]) / L, (b[1] - a[1]) / L

        def same_class(it, a=a, b=b, key=sub.key):
            return _class_hit(key, a, b, it)

        for side in (1, -1):
            for s, w, gap, hit in _side_hits(
                a, b, sub.r, sub.net, index, side, extra, exclude, step, reach, same_class
            ):
                if window is not None:
                    px, py = a[0] + ux * s, a[1] + uy * s
                    if not (window[0] <= px <= window[2] and window[1] <= py <= window[3]):
                        continue
                if not same_class(hit):
                    continue
                creq = max(sub.clearance, hit.clearance) + MARGIN
                if gap <= creq + ADJ_TIGHT:
                    T += w
                    pairs[tuple(sorted((sub.net, hit.net)))] += w
                else:
                    X += (gap - creq) * w
    return dict(X=X, T=T, pairs=dict(pairs))


def hug_segment(
    a,
    b,
    half,
    net,
    key,
    clearance,
    index,
    extra=(),
    exclude=(),
    step=ADJ_STEP,
    reach=ADJ_REACH,
    accept=None,
):
    """Saturated same-class adjacency of one new segment (nm^2, lower = tighter): per sample,
    min over both sides of (gap to a same-class parallel track of another net, else reach)
    minus c_req, times the sample length. Ties of router cost are broken by it.
    accept(item) -> bool narrows the neighbours that count (functional groups: same group only)."""
    if a == b:
        return 0.0
    a, b = _canonical(a, b)
    creq = clearance + MARGIN

    def counts(it):
        return _class_hit(key, a, b, it) and (accept is None or accept(it))

    left = _side_hits(a, b, half, net, index, 1, extra, exclude, step, reach, counts)
    right = _side_hits(a, b, half, net, index, -1, extra, exclude, step, reach, counts)
    score = 0.0
    for (s, w, g1, h1), (_, _, g2, h2) in zip(left, right):
        best = reach
        for g_, h_ in ((g1, h1), (g2, h2)):
            if counts(h_):
                best = min(best, g_)
        score += (max(best, creq) - creq) * w
    return score


# ---------------------------------------------------------------- functional groups: cross-group parallel-run cap
# Owner decision (functional groups with a cross-group cap): packing at minimum pitch is unlimited
# between nets of one functional group and capped at CROSS_GROUP_MM of parallel run between nets of
# different groups (an undeclared net is a group of its own). The measure, exactly:
#
#   For tracks s (net a) and t (net b != a) on the same copper layer whose directions differ by at
#   most CAP_PARALLEL (30 deg), I(s, t) is the set of points x of s's centreline whose perpendicular
#   foot on t's line lies on t (the overlapping projection) and whose centre distance to t's line is
#   <= p(s, t) + CAP_BAND, with p(s, t) = (w_s + w_t)/2 + max(c_a, c_b) + 0.001 mm the minimum legal
#   pitch (c: the nets' clearances). dir(a -> b) = sum over tracks s of a of |union over t of b of
#   I(s, t)|; the parallel run is C(a, b) = (dir(a -> b) + dir(b -> a)) / 2 summed over the layers.
#   For two parallel tracks at <= p + 0.1 mm, C is their common projection; the union makes it
#   independent of how either net is split into segments. Pads, vias and zones are not runs.
#
# An edit (dekink, gloss, corridor) is legal only if after it, for every pair (a, b) of different
# groups with a finite allowance A(a, b): C_after(a, b) <= max(A(a, b), C_before(a, b)) + CAP_TOL.
# By induction over the accepted edits of a pass, C_end <= max(A, C_B0): a pair that already
# exceeded A before the phase is never increased. Normalize does not change copper.
CROSS_GROUP_MM = 10.0  # PNR_GLOSS_CROSS_GROUP_MM default
CAP_BAND = ADJ_TIGHT  # 0.1 mm above minimum pitch still counts as "at minimum pitch"
CAP_PARALLEL = ADJ_PARALLEL  # +-30 deg counts as a parallel run
CAP_TOL = 10  # nm: float noise of the interval arithmetic
_CAP_COS = math.cos(math.radians(CAP_PARALLEL))


def group_of(net, tags):
    """Functional group of a net: its declared tag, else a singleton group of its own."""
    tag = (tags or {}).get(net)
    return ("group", tag) if tag is not None else ("net", net)


def allowed_parallel_mm(net_a, net_b, tags, cross_group_mm=CROSS_GROUP_MM, budget=None):
    """THE HOOK: allowed total parallel run (mm) at minimum pitch between two nets; None = unlimited.

    Owner decision: unlimited within one functional group, `cross_group_mm` (10 mm) between
    different groups and between a grouped and an undeclared net. A noise-budget model (built
    separately) may replace the fixed cap: pass `budget(net_a, net_b) -> mm or None`, the
    per-pair allowance derived from the victims' noise budgets; it is consulted for every pair of
    different groups and its answer is final. Within a group the run stays unlimited."""
    if net_a == net_b or group_of(net_a, tags) == group_of(net_b, tags):
        return None
    if budget is not None:
        return budget(net_a, net_b)
    return float(cross_group_mm)


@dataclass(frozen=True)
class CTrack:
    """A track centreline for the parallel-run measure (nm): width and its net's clearance."""

    net: str
    a: Point
    b: Point
    width: int
    clearance: int
    owner: str = ""


def _ct_key(t):
    return (t.net, t.a, t.b, t.width, t.clearance, t.owner)


def coupled_span(s, t, band=CAP_BAND):
    """(lo, hi) in nm along s from s.a: the points of s at minimum pitch from t (see above), or None."""
    if s.a == s.b or t.a == t.b or s.net == t.net:
        return None
    L, M = math.dist(s.a, s.b), math.dist(t.a, t.b)
    ux, uy = (s.b[0] - s.a[0]) / L, (s.b[1] - s.a[1]) / L
    vx, vy = (t.b[0] - t.a[0]) / M, (t.b[1] - t.a[1]) / M
    dot = ux * vx + uy * vy
    if abs(dot) < _CAP_COS - 1e-12:
        return None
    cross = ux * vy - uy * vx
    thr = (s.width + t.width) / 2 + max(s.clearance, t.clearance) + MARGIN + band
    wx, wy = s.a[0] - t.a[0], s.a[1] - t.a[1]
    lo, hi = 0.0, L
    # foot of s(x) on t's line: wx.v + x dot in [0, M]; signed distance: w x v + x cross in [-thr, thr]
    for g0, g1, a, b in ((wx * vx + wy * vy, dot, 0.0, M), (wx * vy - wy * vx, cross, -thr, thr)):
        if abs(g1) < 1e-12:
            if not (a - 1e-6 <= g0 <= b + 1e-6):
                return None
            continue
        x1, x2 = (a - g0) / g1, (b - g0) / g1
        lo, hi = max(lo, min(x1, x2)), min(hi, max(x1, x2))
        if hi <= lo:
            return None
    return (lo, hi)


def union_length(spans):
    total, lo, hi = 0.0, None, None
    for a, b in sorted(spans):
        if hi is None or a > hi:
            if hi is not None:
                total += hi - lo
            lo, hi = a, b
        else:
            hi = max(hi, b)
    return total + (hi - lo if hi is not None else 0.0)


def directed_runs(s, partners, wanted=None):
    """{net: |union of I(s, t)|} over partner tracks t of other nets (wanted(net) filters)."""
    spans = defaultdict(list)
    for t in partners:
        if t.net == s.net or (wanted is not None and not wanted(t.net)):
            continue
        sp = coupled_span(s, t)
        if sp is not None:
            spans[t.net].append(sp)
    return {n: union_length(v) for n, v in spans.items()}


class CouplingLayer:
    """All tracks of one copper layer (CTracks), bucketed, for the parallel-run measure."""

    def __init__(self, tracks, cell=NM):
        self.tracks = sorted({t for t in tracks if t.a != t.b}, key=_ct_key)
        self.buckets = _Buckets(cell)
        self.by_owner = defaultdict(list)
        self.by_net = defaultdict(list)
        for k, t in enumerate(self.tracks):
            self.buckets.add_box(bbox((t.a, t.b)), k)
            self.by_owner[t.owner].append(t)
            self.by_net[t.net].append(t)
        self.wmax = max([t.width for t in self.tracks] or [0])
        self.cmax = max([t.clearance for t in self.tracks] or [0])

    def reach(self, t, extra=()):
        """Centre distance beyond which t has no run with any track here (or in extra)."""
        w = max([self.wmax] + [x.width for x in extra])
        c = max([self.cmax] + [x.clearance for x in extra])
        return int(math.ceil((t.width + w) / 2 + max(t.clearance, c) + MARGIN + CAP_BAND)) + 2

    def near(self, box, remove=frozenset(), add=()):
        box = tuple(
            int(v)
            for v in (math.floor(box[0]), math.floor(box[1]), math.ceil(box[2]), math.ceil(box[3]))
        )
        out = [
            self.tracks[k]
            for k in sorted(self.buckets.query(box))
            if self.tracks[k].owner not in remove
            and boxes_overlap(bbox((self.tracks[k].a, self.tracks[k].b)), box)
        ]
        out += [t for t in add if boxes_overlap(bbox((t.a, t.b)), box)]
        return out

    def runs(self, s, remove=frozenset(), add=(), wanted=None):
        """{net: dir(s -> net)} in the state (this layer - owners `remove` + tracks `add`)."""
        return directed_runs(
            s, self.near(bbox((s.a, s.b), self.reach(s, add)), remove, add), wanted
        )


def pair_table(layer):
    """{(a, b) sorted: C_layer(a, b)} (nm) over every pair of nets with a run on this layer."""
    d = defaultdict(float)
    for s in layer.tracks:
        for n, v in layer.runs(s).items():
            d[s.net, n] += v
    out = {}
    for (a, b), v in d.items():
        key = (a, b) if a < b else (b, a)
        out[key] = out.get(key, 0.0) + v / 2
    return out


def net_runs(layer, net):
    """{other: C_layer(net, other)} (nm) of one net on this layer."""
    mine = layer.by_net.get(net, [])
    if not mine:
        return {}
    d = defaultdict(float)
    for s in mine:
        for n, v in layer.runs(s).items():
            d[n] += v
    seen = set()
    for s in mine:
        for t in layer.near(bbox((s.a, s.b), layer.reach(s))):
            if t.net == net or t in seen:
                continue
            seen.add(t)
            for n, v in layer.runs(t, wanted=lambda x: x == net).items():
                d[t.net] += v
    return {n: v / 2 for n, v in d.items()}


class EditCoupling:
    """Change of C(a, b) on one layer when the tracks owned by `remove` leave, `keep` tracks (the
    remainders of split segments, and copper of other edits applied together) arrive, and a
    candidate's tracks are added. Only tracks within reach of the changed region can change; the
    region's state without the candidate is cached, so a candidate costs its own tracks and their
    neighbours. Pairs between two unchanged nets never change and are not tallied."""

    def __init__(self, layer, remove=(), keep=(), region=None, margin=0):
        self.layer = layer
        self.remove = frozenset(remove)
        self.keep = list(keep)
        removed = [t for o in sorted(self.remove) for t in layer.by_owner.get(o, ())]
        self.changed = {t.net for t in removed} | {t.net for t in self.keep}
        pts = [p for t in removed + self.keep for p in (t.a, t.b)]
        if region is None and not pts:
            region = (0, 0, 0, 0)
        box = bbox(pts, margin) if region is None else region
        self.inner = box
        reach = max([layer.reach(t, self.keep) for t in removed + self.keep] or [0])
        reach = max(reach, int(math.ceil(layer.wmax + layer.cmax + MARGIN + CAP_BAND)) + 2)
        self.region = (box[0] - reach, box[1] - reach, box[2] + reach, box[3] + reach)
        self.before = _Runs()
        self.base = _Runs()
        self.base_by = {}
        for s in layer.near(self.region):
            self.before.add(s.net, layer.runs(s, wanted=self._wanted(s.net)))
        for s in layer.near(self.region, self.remove, self.keep):
            r = layer.runs(s, self.remove, self.keep, self._wanted(s.net))
            self.base_by[s] = r
            self.base.add(s.net, r)

    def _wanted(self, net, extra=frozenset()):
        if net in self.changed or net in extra:
            return None
        nets = self.changed | extra
        return lambda n: n in nets

    def delta(self, add=()):
        """{(a, b) sorted: change of C_layer(a, b)} (nm) for the candidate tracks `add`."""
        add = list(add)
        box = self.inner
        if any(
            not (box[0] <= p[0] <= box[2] and box[1] <= p[1] <= box[3])
            for t in add
            for p in (t.a, t.b)
        ):
            pts = [p for t in add for p in (t.a, t.b)] + [box[:2], box[2:]]
            wide = EditCoupling(self.layer, self.remove, self.keep, region=bbox(pts))
            return wide.delta(add)
        nets = {t.net for t in add} - self.changed
        if nets:  # a candidate of another net: start from scratch for it
            return EditCoupling(self.layer, self.remove, self.keep + add, region=self.inner).delta()
        after = _Runs()
        after.merge(self.base)
        state = self.keep + add
        for t in add:
            after.add(t.net, self.layer.runs(t, self.remove, state))
        touched = set()
        for t in add:
            for s in self.layer.near(
                bbox((t.a, t.b), self.layer.reach(t, state)), self.remove, self.keep
            ):
                if s in touched or s.net == t.net:
                    continue
                touched.add(s)
                old = self.base_by.get(s)
                if old is None:
                    old = self.layer.runs(s, self.remove, self.keep, self._wanted(s.net))
                after.sub(s.net, old)
                after.add(s.net, self.layer.runs(s, self.remove, state, self._wanted(s.net)))
        out = defaultdict(float)
        for (a, b), v in after.items():
            out[(a, b) if a < b else (b, a)] += v / 2
        for (a, b), v in self.before.items():
            out[(a, b) if a < b else (b, a)] -= v / 2
        return {k: v for k, v in out.items() if abs(v) > 1e-6}


class _Runs(dict):
    """{(subject net, partner net): nm} with add/sub of directed_runs results."""

    def add(self, net, runs):
        for n, v in runs.items():
            self[net, n] = self.get((net, n), 0.0) + v

    def sub(self, net, runs):
        for n, v in runs.items():
            self[net, n] = self.get((net, n), 0.0) - v

    def merge(self, other):
        for k, v in other.items():
            self[k] = self.get(k, 0.0) + v


def cap_violations(delta, totals, allowed, tol=CAP_TOL):
    """Pairs breaking the cap after an edit: [(a, b, before_nm, after_nm, allowed_nm)].

    delta: {(a, b): change of C (nm)}; totals(a, b) -> C before the edit (all layers, nm);
    allowed(a, b) -> nm or None (unlimited: same group)."""
    out = []
    for (a, b), d in sorted(delta.items()):
        if d <= tol:
            continue
        limit = allowed(a, b)
        if limit is None:
            continue
        before = totals(a, b)
        if before + d > max(limit, before) + tol:
            out.append((a, b, before, before + d, limit))
    return out


# ---------------------------------------------------------------- corridor coalescing (design 2.3) and metric E (2.4)
@dataclass
class CorridorChain:
    """One chain on the layer as the corridor planner sees it."""

    id: str
    net: str
    width: int
    clearance: int
    points: list
    key: Optional[tuple] = None  # class key K; None = never grouped (ineligible)
    movable: bool = True  # eligible, unfrozen, not SI
    si: bool = False
    old_segments: tuple = ()


@dataclass(frozen=True)
class Leg:
    chain: str
    net: str
    key: Optional[tuple]
    width: int
    clearance: int
    index: int  # leg index in the chain's simplified points
    a: Point
    b: Point
    d4: Optional[int]  # direction mod 4 (parallel class), None = any-angle
    si: bool
    movable: bool

    @property
    def length(self):
        return math.dist(self.a, self.b)


def _axes(d4):
    U = DIRS[d4]
    N = DIRS[(d4 + 2) % 8]
    return U, N, math.hypot(*U)


def _dot(p, v):
    return p[0] * v[0] + p[1] * v[1]


def chain_legs(chain, min_length=0):
    pts = simplify(chain.points)
    out = []
    n = len(pts) - 1
    for i, (a, b) in enumerate(_legs(pts)):
        d = direction(a, b)
        leg = Leg(
            chain.id,
            chain.net,
            chain.key,
            chain.width,
            chain.clearance,
            i,
            a,
            b,
            None if d is None else d % 4,
            chain.si,
            chain.movable and 0 < i < n - 1,
        )
        if leg.length >= min_length:
            out.append(leg)
    return out


def _capsule_hits_polygon(a, b, r, poly):
    if point_in_polygon(a, poly) or point_in_polygon(b, poly):
        return True
    for k in range(len(poly)):
        c, d = poly[k], poly[(k + 1) % len(poly)]
        if segment_distance(a, b, c, d) < r - 1e-6:
            return True
    return False


def _poly_box(poly):
    return (
        math.floor(min(p[0] for p in poly)),
        math.floor(min(p[1] for p in poly)),
        math.ceil(max(p[0] for p in poly)),
        math.ceil(max(p[1] for p in poly)),
    )


class _TrackIndex:
    def __init__(self, tracks):
        self.tracks = list(tracks)  # (net, a, b, width, owner)
        self.buckets = _Buckets()
        for k, (_, a, b, w, _) in enumerate(self.tracks):
            self.buckets.add_box(bbox((a, b), int(w // 2) + 1), k)

    def near(self, box):
        return [self.tracks[k] for k in sorted(self.buckets.query(box))]


def neighbour(s, t, tracks, strip_blocked=None):
    """Design 2.3 step 2 for two legs; returns the pair record or None (exact mode)."""
    if s.net == t.net or s.key is None or s.key != t.key or s.d4 is None or s.d4 != t.d4:
        return None
    U, N, k = _axes(s.d4)
    slo, shi = sorted((_dot(s.a, U), _dot(s.b, U)))
    tlo, thi = sorted((_dot(t.a, U), _dot(t.b, U)))
    lo, hi = max(slo, tlo), min(shi, thi)
    overlap = (hi - lo) / k
    if overlap < CORRIDOR_MIN_OVERLAP:
        return None
    os_, ot = _dot(s.a, N), _dot(t.a, N)
    gap = abs(ot - os_) / k - (s.width + t.width) / 2
    creq = max(s.clearance, t.clearance) + MARGIN
    if not (creq + CORRIDOR_TIGHT < gap <= CORRIDOR_MAX_GAP):
        return None
    sign = 1 if ot > os_ else -1
    e1 = os_ + sign * s.width / 2 * k
    e2 = ot - sign * t.width / 2 * k

    def xy(u, v):
        return ((u * U[0] + v * N[0]) / (k * k), (u * U[1] + v * N[1]) / (k * k))

    quad = [xy(lo, e1), xy(hi, e1), xy(hi, e2), xy(lo, e2)]
    if _strip_blocked(quad, (s, t), tracks, strip_blocked):
        return None
    return dict(
        s=s, t=t, overlap=overlap, gap=gap, creq=creq, excess=overlap * (gap - creq), quad=quad
    )


def _strip_blocked(quad, pair, tracks, strip_blocked):
    nets = {pair[0].net, pair[1].net}
    for net, a, b, w, owner in tracks.near(_poly_box(quad)):
        if net in nets:
            continue
        if _capsule_hits_polygon(a, b, w / 2, quad):
            return True
    return bool(strip_blocked is not None and strip_blocked(quad, frozenset(nets)))


def _survey_pair(s, t, tracks, strip_blocked):
    """The bounding-box corridor definition of the design survey (E_bbox, a diagnostic)."""
    if s.net == t.net or s.key is None or s.key != t.key:
        return None
    L = s.length
    if L < CORRIDOR_MIN_LEG or t.length < CORRIDOR_MIN_LEG:
        return None
    hs = math.degrees(math.atan2(s.b[1] - s.a[1], s.b[0] - s.a[0])) % 180
    ht = math.degrees(math.atan2(t.b[1] - t.a[1], t.b[0] - t.a[0])) % 180
    if min(abs(hs - ht), 180 - abs(hs - ht)) > 15:
        return None
    ux, uy = (s.b[0] - s.a[0]) / L, (s.b[1] - s.a[1]) / L
    pr = sorted((q[0] - s.a[0]) * ux + (q[1] - s.a[1]) * uy for q in (t.a, t.b))
    lo, hi = max(0, pr[0]), min(L, pr[1])
    if hi - lo <= CORRIDOR_MIN_OVERLAP:
        return None
    sa = (s.a[0] + ux * lo, s.a[1] + uy * lo)
    sb = (s.a[0] + ux * hi, s.a[1] + uy * hi)
    tv = (t.b[0] - t.a[0], t.b[1] - t.a[1])
    tdot = tv[0] * ux + tv[1] * uy

    def tat(u):
        f = ((u - ((t.a[0] - s.a[0]) * ux + (t.a[1] - s.a[1]) * uy)) / tdot) if tdot else 0
        return (t.a[0] + tv[0] * f, t.a[1] + tv[1] * f)

    ta, tb = tat(lo), tat(hi)
    edge = segment_distance(sa, sb, ta, tb) - (s.width + t.width) / 2
    req = max(s.clearance, t.clearance)
    if edge <= req + CORRIDOR_TIGHT or edge > CORRIDOR_MAX_GAP:
        return None
    quad = [sa, sb, tb, ta]
    if _strip_blocked(quad, (s, t), tracks, strip_blocked):
        return None
    return dict(
        s=s, t=t, overlap=hi - lo, gap=edge, creq=req, excess=(hi - lo) * (edge - req), quad=quad
    )


def corridor_pairs(legs, tracks, strip_blocked=None, window=None, mode="exact"):
    """Corridor neighbour pairs among `legs` (design 2.3 step 2 / survey for mode='bbox').

    tracks: every copper centreline on the layer as (net, a, b, width[, owner]) - the
    strip blockers; strip_blocked(quad, nets) covers pads, vias, keepouts and holes.
    """
    index = (
        tracks
        if isinstance(tracks, _TrackIndex)
        else _TrackIndex([tuple(t) + (None,) * (5 - len(t)) for t in tracks])
    )
    legs = sorted(legs, key=lambda g: (g.chain, g.index))
    inside = None
    if window is not None:
        # pairs with at least one leg in the window; partners lie within the gap cap of it
        reach = int(CORRIDOR_MAX_GAP + max([g.width for g in legs] or [0])) + 1
        wide = (window[0] - reach, window[1] - reach, window[2] + reach, window[3] + reach)
        inside = {(g.chain, g.index) for g in legs if boxes_overlap(bbox((g.a, g.b)), window)}
        legs = [g for g in legs if boxes_overlap(bbox((g.a, g.b)), wide)]
    out = []
    if mode == "exact":
        groups = defaultdict(list)
        for g in legs:
            if g.d4 is not None and g.key is not None and g.length >= CORRIDOR_MIN_LEG:
                groups[g.key, g.d4].append(g)
        for (key, d4), gl in sorted(groups.items(), key=lambda kv: (str(kv[0][0]), kv[0][1])):
            _, N, k = _axes(d4)
            gl.sort(key=lambda g: (_dot(g.a, N), g.chain, g.index))
            span = (CORRIDOR_MAX_GAP + max(g.width for g in gl)) * k + 2
            for i, s in enumerate(gl):
                for t in gl[i + 1 :]:
                    if _dot(t.a, N) - _dot(s.a, N) > span:
                        break
                    p = neighbour(s, t, index, strip_blocked)
                    if p:
                        out.append(p)
    else:
        for i, s in enumerate(legs):
            for t in legs[i + 1 :]:
                p = _survey_pair(s, t, index, strip_blocked)
                if p:
                    out.append(p)
    if inside is not None:
        out = [
            p
            for p in out
            if (p["s"].chain, p["s"].index) in inside or (p["t"].chain, p["t"].index) in inside
        ]
    return out


def corridor_excess(legs, tracks, strip_blocked=None, window=None, mode="exact"):
    """Metric E (nm^2): sum over neighbour pairs of common projection x (g - c_req)."""
    return sum(p["excess"] for p in corridor_pairs(legs, tracks, strip_blocked, window, mode))


class Corridor:
    """Corridor coalescing planner for one layer (design 2.3).

    chains: every chain on the layer the planner may move or must respect (all
    CorridorChain records, eligible or not). The injected clear(net, width, a, b)
    must IGNORE the copper of every chain in `chains` (an Oracle fork without
    them); the planner itself checks new copper against the current copper of
    those chains with the Oracle rule (edge gap >= max clearance + 0.001 mm).
    contact(net, width, a, b, chain_id) is L2 against same-net copper outside
    the chain's anchors; obstacles are L3 test points of non-chain items;
    guards(chain_id) -> [Guard] is L7 (pairs, open terminals); tracks are the
    other copper centrelines (net, a, b, width) for strip blocking;
    strip_blocked(quad, nets) covers non-track items; window_ok(box, before,
    after) is the dead-space hook (dDS <= 0), checked by the KiCad worker when None.
    cap(chain_id, new_points, state) -> None | 'cap:<a>|<b>' is the functional-group cap on
    cross-group parallel runs with every chain at its `state` geometry (None: no groups file).
    A member the cap stops stays where it is (no partial shift is sought against the cap) and
    becomes the anchor for the members beyond it.
    """

    def __init__(
        self,
        chains,
        *,
        layer="",
        clear=None,
        contact=None,
        obstacles=(),
        guards=None,
        tracks=(),
        strip_blocked=None,
        window_ok=None,
        ray_items=None,
        cap=None,
    ):
        self.layer = layer
        self.cap = cap
        self.chains = {c.id: c for c in sorted(chains, key=lambda c: c.id)}
        self._clear = clear
        self._contact = contact
        self.obstacles = sorted({ipt(p) for p in obstacles})
        self._points = _Buckets()
        for p in self.obstacles:
            self._points.add_box((p[0], p[1], p[0], p[1]), p)
        self.guards = guards
        self.tracks = [tuple(t[:4]) for t in tracks]
        self.strip_blocked = strip_blocked
        self.window_ok = window_ok
        self.stats = defaultdict(int)
        # Ray metric X: static copper (ray_items: pads, vias, other tracks as RayItems; by default
        # the `tracks` as blockers) plus every chain at its base geometry (same-class keys unless SI).
        static = (
            list(ray_items)
            if ray_items is not None
            else [RayItem(a, b, w / 2, n, None, 0, "track") for n, a, b, w in self.tracks]
        )
        base = []
        for cid, c in self.chains.items():
            base += self._chain_rays(cid, simplify(c.points))
        self._rays = RayIndex(static + base)

    def _chain_rays(self, cid, points):
        c = self.chains[cid]
        key = None if (c.si or c.key is None) else c.key
        return [
            RayItem(a, b, c.width / 2, c.net, key, c.clearance, cid)
            for a, b in _legs(simplify(points))
        ]

    def adjacency(self, state, window, moved=()):
        """X / T over the window with the chains in `moved` at their `state` geometry."""
        moved = sorted(set(moved))
        extra = [r for cid in moved for r in self._chain_rays(cid, state[cid])]
        reach = int(CORRIDOR_MAX_GAP + max([c.width for c in self.chains.values()] or [0])) + 1
        wide = (window[0] - reach, window[1] - reach, window[2] + reach, window[3] + reach)
        subjects = [r for r in self._rays.near(wide) if r.key is not None and r.owner not in moved]
        subjects += [
            r for r in extra if r.key is not None and boxes_overlap(bbox((r.a, r.b)), wide)
        ]
        return adjacency(subjects, self._rays, extra=extra, exclude=set(moved), window=window)

    # -- geometry state
    def _legs(self, state, min_length=0):
        out = []
        for cid in sorted(state):
            c = self.chains[cid]
            out += chain_legs(
                CorridorChain(
                    c.id, c.net, c.width, c.clearance, state[cid], c.key, c.movable, c.si
                ),
                min_length,
            )
        return out

    def _track_index(self, state):
        rows = [(n, a, b, w, None) for n, a, b, w in self.tracks]
        for cid in sorted(state):
            c = self.chains[cid]
            rows += [(c.net, a, b, c.width, cid) for a, b in _legs(simplify(state[cid]))]
        return _TrackIndex(rows)

    def excess(self, state=None, window=None, mode="exact"):
        state = state or {cid: simplify(c.points) for cid, c in self.chains.items()}
        return corridor_excess(
            self._legs(state), self._track_index(state), self.strip_blocked, window, mode
        )

    def stacks(self):
        """Stacks of movable-class legs (non-SI, eligible): sorted by normal offset, cut at gaps."""
        state = {cid: simplify(c.points) for cid, c in self.chains.items()}
        legs = [
            g
            for g in self._legs(state, CORRIDOR_MIN_LEG)
            if g.key is not None and not g.si and g.d4 is not None
        ]
        pairs = corridor_pairs(legs, self._track_index(state), self.strip_blocked)
        adj = defaultdict(set)

        def ident(g):
            return (g.chain, g.index)

        for p in pairs:
            adj[ident(p["s"])].add(ident(p["t"]))
            adj[ident(p["t"])].add(ident(p["s"]))
        bylegs = {ident(g): g for g in legs}
        seen, out = set(), []
        for start in sorted(adj):
            if start in seen:
                continue
            comp, todo = [], [start]
            seen.add(start)
            while todo:
                x = todo.pop()
                comp.append(x)
                for y in sorted(adj[x]):
                    if y not in seen:
                        seen.add(y)
                        todo.append(y)
            members = [bylegs[x] for x in comp]
            _, N, _ = _axes(members[0].d4)
            members.sort(
                key=lambda g: (
                    _dot(g.a, N),
                    min(_dot(g.a, DIRS[g.d4]), _dot(g.b, DIRS[g.d4])),
                    g.net,
                    g.chain,
                    g.index,
                )
            )
            cur = [members[0]]
            for g in members[1:]:
                if (
                    ident(g) in adj[ident(cur[-1])]
                    and g.chain not in {m.chain for m in cur}
                    and g.net not in {m.net for m in cur}
                ):
                    cur.append(g)
                else:
                    if len(cur) >= 2:
                        out.append(cur)
                    cur = [g]
            if len(cur) >= 2:
                out.append(cur)
        out.sort(key=lambda st: (st[0].key, st[0].d4, [(m.chain, m.index) for m in st]))
        return out

    # -- one member drag (design 2.3 step 4)
    def _drag(self, leg, pts, D, N):
        i = leg.index
        if i == 0 or i + 2 >= len(pts):
            return None, "anchored"
        vp, vs, ve, vn = pts[i - 1], pts[i], pts[i + 1], pts[i + 2]
        dp, dn = direction(vp, vs), direction(ve, vn)
        if dp is None or dn is None:
            return None, "any_angle"
        Up, Un = DIRS[dp], DIRS[dn]
        kp, kn = _dot(Up, N), _dot(Un, N)
        if kp == 0 or kn == 0:
            return None, "parallel"
        step = abs(kp) * abs(kn) // math.gcd(abs(kp), abs(kn))
        Dq = (1 if D > 0 else -1) * (abs(D) // step) * step
        if Dq == 0:
            return None, "small"
        ts, te = Dq // kp, Dq // kn
        vs2 = (vs[0] + ts * Up[0], vs[1] + ts * Up[1])
        ve2 = (ve[0] + te * Un[0], ve[1] + te * Un[1])
        if (
            direction(vp, vs2) != dp
            or direction(ve2, vn) != dn
            or direction(vs2, ve2) != direction(vs, ve)
            or math.dist(vp, vs2) <= 2 * TOL
            or math.dist(ve2, vn) <= 2 * TOL
        ):
            return None, "reverse"
        return pts[:i] + [vs2, ve2] + pts[i + 2 :], Dq

    def _member_legal(self, cid, old, new, state):
        c = self.chains[cid]
        old_s, new_s = simplify(old), simplify(new)
        old_set = {frozenset(s) for s in _legs(old_s)}
        new_set = {frozenset(s) for s in _legs(new_s)}
        added = [s for s in _legs(new_s) if frozenset(s) not in old_set]
        removed = [s for s in _legs(old_s) if frozenset(s) not in new_set]
        if any(direction(a, b) is None for a, b in added):
            return "L4:octilinear"
        if bends(new_s) > bends(old_s) or sharp_turns(new_s) > sharp_turns(old_s):
            return "dB"
        box = bbox([p for s in added + removed for p in s], int(c.width) + 1)
        for a, b in added:
            if self._clear is not None and not self._clear(c.net, c.width, a, b):
                return "L1:clear"
        for oid in sorted(state):
            if oid == cid:
                continue
            o = self.chains[oid]
            if o.net == c.net:
                continue
            need = (c.width + o.width) / 2 + max(c.clearance, o.clearance) + MARGIN
            obox = bbox(state[oid], int(need) + 1)
            if not boxes_overlap(box, obox):
                continue
            for a, b in added:
                for p, q in _legs(state[oid]):
                    if segment_distance(a, b, p, q) < need - 1e-6:
                        return "L1:corridor"
        for a, b in added:
            if self._contact is not None and self._contact(c.net, c.width, a, b, cid):
                return "L2:contact"
        guards = list(self.guards(cid)) if self.guards is not None else []
        for g in guards:
            d_old = guard_distance(removed, c.width, g)
            d_new = guard_distance(added, c.width, g)
            if min(d_new, g.cap) < min(d_old, g.cap) - TOL:
                return "L7:" + g.name
        sbox = bbox(old_s + new_s, TOL)
        pts = [
            p
            for p in self._points.query(sbox)
            if sbox[0] <= p[0] <= sbox[2] and sbox[1] <= p[1] <= sbox[3]
        ]
        for oid in sorted(state):
            if oid != cid:
                pts += [
                    p
                    for p in state[oid]
                    if sbox[0] <= p[0] <= sbox[2] and sbox[1] <= p[1] <= sbox[3]
                ]
        if swept_points(old_s, new_s, sorted(set(pts))):
            return "L3:swept"
        return None

    PARTIAL = ("reverse", "L1:clear", "L1:corridor", "L3:swept", "slack")

    def _try_drag(self, m, state, D, N, k, cap=True):
        """Drag member m by D (normal units); (new, Dq, dL, None) or (None, None, None, reason)."""
        new, Dq = self._drag(m, state[m.chain], D, N)
        if new is None:
            return None, None, None, Dq
        if abs(Dq) / k < CORRIDOR_MIN_SHIFT:
            return None, None, None, "small"
        dL = length(new) - length(state[m.chain])
        if dL > CORRIDOR_SLACK:
            return None, None, None, "slack"
        why = self._member_legal(m.chain, state[m.chain], new, state)
        if why is None and cap:
            why = self._cap(m.chain, new, state)
        if why is not None:
            return None, None, None, why
        return new, Dq, dL, None

    def _cap(self, cid, new, state):
        if self.cap is None:
            return None
        self.stats["cap_checks"] += 1
        why = self.cap(cid, new, state)
        if why:
            self.stats["cap_refused"] += 1
            return "cap"
        return None

    def _partial(self, m, state, D, N, k):
        """Largest legal shift toward the target on the drag lattice, down to the minimum shift
        (a member whose full target is blocked still packs as far as it legally can)."""
        i = m.index
        pts = state[m.chain]
        if i == 0 or i + 2 >= len(pts):
            return None
        dp, dn = direction(pts[i - 1], pts[i]), direction(pts[i + 1], pts[i + 2])
        if dp is None or dn is None:
            return None
        kp, kn = _dot(DIRS[dp], N), _dot(DIRS[dn], N)
        if kp == 0 or kn == 0:
            return None
        step = abs(kp) * abs(kn) // math.gcd(abs(kp), abs(kn))
        sign = 1 if D > 0 else -1
        n_full = abs(D) // step
        n_min = max(1, math.ceil(CORRIDOR_MIN_SHIFT * k / step))
        lo, hi = n_min, n_full - 1
        if lo > hi:
            return None
        # the shift is bisected against the legality checks only; the functional-group cap is
        # checked once on the result (a cap is never "packed up to" by bisection)
        got = self._try_drag(m, state, sign * lo * step, N, k, cap=False)
        if got[0] is None:
            return None
        while lo < hi:
            mid = (lo + hi + 1) // 2
            trial = self._try_drag(m, state, sign * mid * step, N, k, cap=False)
            if trial[0] is not None:
                lo, got = mid, trial
            else:
                hi = mid - 1
        if self._cap(m.chain, got[0], state):
            return "cap"
        return got

    def _pack(self, stack, anchor, base):
        """Pack the stack toward member `anchor`; returns (state, moves, reasons)."""
        d4 = stack[0].d4
        _, N, k = _axes(d4)
        state = dict(base)
        offset = {m: _dot(m.a, N) for m in stack}
        moves = {}
        reasons = {}
        for side in (1, -1):
            order = stack[anchor + 1 :] if side > 0 else stack[:anchor][::-1]
            prev = stack[anchor]
            for m in order:
                p = pitch(prev.width, prev.clearance, m.width, m.clearance)
                P = math.ceil(p * k - 1e-9)
                target = offset[prev] + side * P
                D = target - offset[m]
                c = self.chains[m.chain]
                why = None
                if D * side >= 0:
                    why = "tight"
                elif abs(D) / k < CORRIDOR_MIN_SHIFT:
                    why = "small"
                elif abs(D) / k > CORRIDOR_MAX_SHIFT:
                    why = "L8:shift"
                elif not m.movable or not c.movable:
                    why = "anchored"
                elif any((_dot(f.a, N) - offset[m]) * D > 0 for f in self._facing_si(m, state)):
                    why = "si_boundary"
                if why is None:
                    new, Dq, dL, why = self._try_drag(m, state, D, N, k)
                    partial = False
                    if new is None and why in self.PARTIAL:
                        got = self._partial(m, state, D, N, k)
                        if got == "cap":
                            why = "cap"
                        elif got is not None:
                            new, Dq, dL, _ = got
                            partial, why = True, None
                    if new is not None:
                        moves[m] = dict(
                            net=c.net,
                            chain=c.id,
                            old=list(state[m.chain]),
                            new=new,
                            shift=Dq / k,
                            dL=dL,
                            dB=bends(new) - bends(state[m.chain]),
                            old_segments=c.old_segments,
                            seq=len(moves),
                            partial=partial,
                        )
                        state[m.chain] = new
                        offset[m] = offset[m] + Dq
                if why is not None:
                    reasons[m] = why
                prev = m
        return state, moves, reasons

    def _facing_si(self, leg, state):
        """SI legs parallel to `leg`, overlapping it, within the gap cap, with an empty strip between."""
        _, N, k = _axes(leg.d4)
        if not any(self.chains[oid].si for oid in state):
            return []
        index = self._track_index(state)
        out = []
        for oid in sorted(state):
            o = self.chains[oid]
            if not o.si or o.net == leg.net:
                continue
            for f in chain_legs(
                CorridorChain(o.id, o.net, o.width, o.clearance, state[oid], leg.key, False, True)
            ):
                if f.d4 != leg.d4:
                    continue
                U = DIRS[leg.d4]
                lo = max(min(_dot(f.a, U), _dot(f.b, U)), min(_dot(leg.a, U), _dot(leg.b, U)))
                hi = min(max(_dot(f.a, U), _dot(f.b, U)), max(_dot(leg.a, U), _dot(leg.b, U)))
                gap = abs(_dot(f.a, N) - _dot(leg.a, N)) / k - (f.width + leg.width) / 2
                if hi <= lo or gap > SI_GUARD_CAP:
                    continue
                sign = 1 if _dot(f.a, N) > _dot(leg.a, N) else -1
                e1 = _dot(leg.a, N) + sign * leg.width / 2 * k
                e2 = _dot(f.a, N) - sign * f.width / 2 * k

                def xy(u, v, U=U, N=N, k=k):
                    return ((u * U[0] + v * N[0]) / (k * k), (u * U[1] + v * N[1]) / (k * k))

                quad = [xy(lo, e1), xy(hi, e1), xy(hi, e2), xy(lo, e2)]
                if not _strip_blocked(quad, (leg, f), index, self.strip_blocked):
                    out.append(f)
        return out

    def plan_stack(self, stack, base=None):
        base = base or {cid: simplify(c.points) for cid, c in self.chains.items()}
        n = len(stack)
        immovable = [i for i, m in enumerate(stack) if not m.movable]
        rest = sorted(
            (i for i in range(n) if i not in immovable), key=lambda i: (abs(2 * i - (n - 1)), i)
        )
        anchors = (immovable + rest)[:CORRIDOR_MAX_ANCHORS]
        best = None
        first_reasons = None
        outcome = "no_moves"
        for a in anchors:
            state, moves, reasons = self._pack(stack, a, base)
            self.stats["anchors_tried"] += 1
            if first_reasons is None:
                first_reasons = reasons
            if not moves:
                continue
            pts = [p for mv in moves.values() for p in mv["old"] + mv["new"]]
            window = bbox(pts, CORRIDOR_WINDOW_MARGIN)
            moved = sorted({mv["chain"] for mv in moves.values()})
            x0 = self.adjacency(base, window)
            x1 = self.adjacency(state, window, moved)
            dX = x1["X"] - x0["X"]
            if dX > -ADJ_MIN_GAIN:
                self.stats["small_gain"] += 1
                outcome = "small_gain"
                continue
            if any(mv["dB"] > 0 or mv["dL"] > CORRIDOR_SLACK for mv in moves.values()):
                continue
            if self.window_ok is not None and not self.window_ok(window, base, state):
                self.stats["dead_space"] += 1
                outcome = "dead_space"
                continue
            key = (
                round(dX),
                len(moves),
                sum(abs(mv["shift"]) for mv in moves.values()),
                stack[a].net,
                stack[a].chain,
            )
            if best is None or key < best[0]:
                best = (key, a, moves, x0, x1, window, reasons, state)
        if best is None:
            self.stats["stack:" + outcome] += 1
            for r in (first_reasons or {}).values():
                self.stats["stay:" + str(r)] += 1
            return None
        key, a, moves, x0, x1, window, reasons, state = best
        self.stats["stack:proposed"] += 1
        for r in reasons.values():
            self.stats["stay:" + str(r)] += 1
        self.stats["partial"] += sum(1 for mv in moves.values() if mv["partial"])
        before = self.excess(base, window)
        after = self.excess(state, window)
        members = [moves[m] for m in stack if m in moves]
        anchor = stack[a]
        c = self.chains[anchor.chain]
        return Edit(
            "corridor",
            c.net,
            self.layer,
            c.width,
            [],
            [],
            members=members,
            extra=dict(
                anchor=dict(net=anchor.net, chain=anchor.chain),
                X_mm2=x0["X"] / NM / NM,
                X_new_mm2=x1["X"] / NM / NM,
                dX_mm2=(x1["X"] - x0["X"]) / NM / NM,
                T_mm=x0["T"] / NM,
                T_new_mm=x1["T"] / NM,
                E_mm2=before / NM / NM,
                E_new_mm2=after / NM / NM,
                dE_mm2=(after - before) / NM / NM,
                window_mm=[_mm(window[:2]), _mm(window[2:])],
                stack=[dict(net=m.net, chain=m.chain, leg=m.index) for m in stack],
                stayed={m.net: r for m, r in sorted(reasons.items(), key=lambda kv: kv[0].net)},
            ),
        )

    def plan(self):
        """Best edit per stack, each planned on the base geometry; sorted by gain."""
        base = {cid: simplify(c.points) for cid, c in self.chains.items()}
        out = []
        for st in self.stacks():
            self.stats["stacks"] += 1
            if not any(m.movable for m in st):
                self.stats["stacks_pinned"] += 1
                continue
            e = self.plan_stack(st, base)
            if e is not None:
                out.append(e)
        return sorted(out, key=lambda e: (-e.gain(), e.nets))


def apply_corridor(chains, edit):
    """Chain points after a corridor edit ({chain_id: points})."""
    state = {c.id: simplify(c.points) for c in chains}
    for m in edit.members:
        state[m["chain"]] = list(m["new"])
    return state


# ---------------------------------------------------------------- metrics (design 2.4; DS/US is the KiCad worker's)
def measure(groups, *, info=None, pinned=None, strip_blocked=None, other_tracks=()):
    """L, chain L, B by angle class, S and E (exact and survey/bbox) per layer and class.

    groups: {(net, layer): [Seg]}; info(net, layer, width) -> dict(key=K, si=bool,
    eligible=bool, clearance=nm) or None (then the net's legs are blockers only).
    pinned(net, layer) -> callable(p) for pad/via anchors; other_tracks: extra
    blockers (net, layer, a, b, width).
    """
    out = defaultdict(
        lambda: dict(
            length_mm=0.0,
            chain_length_mm=0.0,
            segments=0,
            bends={45: 0, 90: 0, 135: 0, 180: 0, "other": 0},
            excess_mm2=0.0,
            excess_bbox_mm2=0.0,
        )
    )
    per_layer = defaultdict(list)
    tracks = defaultdict(list)
    for (net, layer), segs in sorted(groups.items()):
        for s in segs:
            tracks[layer].append((net, s.a, s.b, s.width))
        cs = build_chains(segs, net=net, layer=layer, pinned=pinned(net, layer) if pinned else None)
        for ch in cs.chains:
            meta = info(net, layer, ch.width) if info else None
            key = meta["key"] if meta else None
            row = out[layer, key]
            row["chain_length_mm"] += length(ch.points) / NM
            for c, n in bend_histogram(ch.points).items():
                row["bends"][c] += n
            per_layer[layer].append(
                CorridorChain(
                    "%s|%s|%s" % (net, ch.pieces[0][2], ch.points[0]),
                    net,
                    ch.width,
                    meta["clearance"] if meta else 0,
                    ch.points,
                    key,
                    bool(meta and meta["eligible"]),
                    bool(meta and meta["si"]),
                )
            )
        by_width = defaultdict(list)
        for s in segs:
            by_width[s.width].append(s)
        for w, ss in by_width.items():
            meta = info(net, layer, w) if info else None
            row = out[layer, meta["key"] if meta else None]
            row["length_mm"] += sum(math.dist(s.a, s.b) for s in ss) / NM
            row["segments"] += len(ss)
    for net, layer, a, b, w in other_tracks:
        tracks[layer].append((net, a, b, w))
    for layer, chains in sorted(per_layer.items()):
        legs = []
        for c in chains:
            legs += chain_legs(c)
        index = _TrackIndex([t + (None,) for t in tracks[layer]])
        for mode, col in (("exact", "excess_mm2"), ("bbox", "excess_bbox_mm2")):
            for p in corridor_pairs(
                legs, index, strip_blocked(layer) if strip_blocked else None, mode=mode
            ):
                out[layer, p["s"].key][col] += p["excess"] / NM / NM
    return {
        ("%s|%s" % (layer, key[1:] if key else "other")): v
        for (layer, key), v in sorted(out.items(), key=lambda kv: (kv[0][0], str(kv[0][1])))
    }
