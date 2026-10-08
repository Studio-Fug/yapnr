"""Detailed placement after legalization (``PNR_DETAIL_PLACE``): small, order-preserving moves
that shorten the wiring of a legal placement.

Design: docs/design/compact-placement.md section 13.H. Displacement-minimizing legalization
(:mod:`pnr.place.keep`, ``PNR_LEGALIZE_KEEP``) keeps every legal part at its global pose and
turn, so it gives up the wirelength the packer's ``WIRE`` slot cost found by re-siting every part
(7.5 % more copper on the ladder A/B). This pass wins it back with local moves under a movement
budget, after legalization (and the align snap and the matched-length pass) and before the side
moves (:mod:`pnr.place.detail_moves`) and routing.

First an order-preserving compaction (:func:`compact`): every movable part is drawn half way
toward the point that minimizes its wirelength, and the layout is projected (weighted least
squares) onto its present order: two parts side by side keep their order and their slots apart
(and as much of the routing channel between them as they have), every part stays in the outline
and within :data:`COMPACT_REACH_MM` of where it was; :data:`COMPACT_ROUNDS` rounds. It is kept
only when the legalizer's checker accepts every shifted part (a part it refuses is held where it
was and the compaction solved again) and the wiring shortens. Then the greedy moves:

``turn``
    the part at one of its other three quarter turns about its slot centre (the half turn is
    the flip); every movable part, not only the ones the legalizer moved (it takes over from
    ``TURN``, :mod:`pnr.place.reorient`, when on);
``slide``
    the part shifted along x or y, in slot-grid steps, at most :func:`reach` from where the
    legalizer put it (half its larger side, at least :data:`REACH_MM`): too short to jump a
    neighbour, so neighbour order is kept;
``swap``
    two adjacent parts of one size class (each courtyard side within :data:`SIZE_CLASS`, or
    one footprint) with no third part between them exchange slot centres, each at its best
    turn; at most one swap per part;
``wrong_side``
    a part one of whose connections crosses the body of the part it connects to (it sits on
    the far side of that part from the pin, the 11-buck-pour R1/U1 case) moves to a free spot
    just outside the edge that pin faces, nearest the pin first, within that part's extent plus
    its own plus two grid cells, and only where the other part's channel penalty does not rise
    (an escape channel beside the pin stays open).

The cost (mm) is the part's wiring estimate: the half-perimeter wirelength of its nets (plane
nets left out, :func:`pnr.place.legalize.plane_nets`) plus the crossing detour ``X``: each net
(up to :data:`MST_PINS` pins) is a Manhattan minimum spanning tree of pad-to-pad segments, and a
segment whose bounding box a same-side courtyard (shrunk by :data:`OBSTACLE_EPS`) cuts across
costs the shorter way around it, the endpoint parts included (behind the pin's own edge: a pin
whose route must go round its own part). Through-hole parts and parts on the other side do not
block. Plus the legalizer's routing-channel penalty (:class:`pnr.place.channels.ChannelModel`,
shortage squared) at the packer's ratio (``channel_weight / wire_weight``, 25 / 4) and global
placement's soft terms (edge pulls, soft groups, soft regions and aligns, as
:mod:`pnr.place.detail_moves`). A move must shorten the wiring (``HPWL + X``) by more than
:data:`GAIN_MM` and lower the whole cost by as much.

Never moved: fixed and locked parts, hard rotations, macros (``block:``, ``line:``, hulls), row
and line-group members, parts with landing reserves and the parts on a differential-pair or
length-match net (:func:`pnr.place.reorient.matched_refs`). Held parts (a hard group, align,
region or edge band) only turn and slide. Every move is legal by
:func:`pnr.place.metrics.pose_checker` with the legalizer's clearance, spreading, inflation,
margins and pad-edge rule; a final :func:`pnr.place.metrics.hard_violations` check returns the
legal input should anything have slipped.

Search: deterministic greedy, best gain first, as :mod:`pnr.place.reorient`: each part's best
move is costed (legality checked in gain order, only while a move still gains) and the best
of all is taken (ties: gain, kind, ref, candidate); the parts it may affect (its nets, and
everything within :data:`NEAR_MM`) are costed again. At most :data:`MOVES_PER_PART` moves per
part and ``MOVES_PER_MOVABLE`` times the movable parts in all. Pure float64 arithmetic in fixed
orders (sorted refs and nets), quarter turns exact, no random draws, no ``exp``/``log``/``hypot``:
the same input gives the same bits on every platform.
"""

from __future__ import annotations

import math
import time
from typing import Dict, List, Optional, Tuple

from pnr.graph import BoardGraph

from .geometry import courtyard_rect, edge_distance, pin_positions

# A move must shorten the wiring (and the whole cost) by more than this (mm).
GAIN_MM = 0.05
# The movement budget of a turn or slide: max(REACH_MM, REACH_SHARE * larger courtyard side).
REACH_MM = 1.5
REACH_SHARE = 1.0
# Accepted moves: per part, and per movable part in all.
MOVES_PER_PART = 3
MOVES_PER_MOVABLE = 3
# Swap partners: each courtyard side within this share, or the same footprint.
SIZE_CLASS = 0.25
# A blocked slide shifts the parts right in front of it (within this gap, mm) along, at most
# this many parts in all.
BAND_GAP_MM = 1.0
BAND_PARTS = 3
# A swap whose wiring at the present turns is this much worse (mm) is not turned further.
SWAP_SLACK_MM = 1.0
# Nets with more pins than this count their half-perimeter only (no crossing term).
MST_PINS = 24
# A pad within this share of its part's smaller side from an edge faces that edge; deeper pads
# (an exposed pad, a ball array's inner rows) have no own-part crossing.
EDGE_PIN_SHARE = 0.3
# A part with at least ARRAY_PADS pads on a grid, more than ARRAY_INNER of them deeper than its
# outer ring (ARRAY_RING_MM past its shallowest pad), is a pad array.
ARRAY_PADS = 16
ARRAY_INNER = 0.25
ARRAY_RING_MM = 0.4
ARRAY_LINES = 4  # and its pads on at least this many distinct rows and columns
# Near a pad array, a part keeps at least this gap (mm) to its courtyard, or what it has if less:
# a plane drop of its own may not land in the array's escape field (the BGA rungs' check that
# every plane via inside the array's courtyard is a fanout via failed when compaction drew the
# decoupling parts up to it).
ARRAY_ZONE_MM = 1.5
# A part within this distance (mm) of a pad array's courtyard does not move at all: the array's
# escape and fanout are the board's tightest routing, and the BGA -pairs rung's LVDS pair failed
# its gap rule in 4 of 16 seeds with the decoupling parts there moved (by 0.1 to 3.5 mm) against
# 0 of 8 with KEEP alone and 1 of 16 with KEEP off.
ARRAY_FREEZE_MM = 3.0
# Likewise a part keeps at least this gap (mm), or what it has if less, to the straight line
# between consecutive pins of a differential-pair or length-match net (its route's corridor;
# the BGA -pairs rung's coupled LVDS pair failed its gap rule more often when parts closed in).
CORRIDOR_MM = 1.0
# ... and moves no deeper into the box between those two pins (grown by this, mm): the route may
# take any monotone path in it.
CORRIDOR_BOX_MM = 0.5
# Courtyards shrink by this (mm) before a segment counts as crossing them.
OBSTACLE_EPS = 0.05
# Channel neighbours and the parts re-costed after a move: within this distance (mm).
NEAR_MM = 4.0
BUCKET_MM = 4.0
# Channel pair terms memoized (by both poses) before the memo is cleared.
PAIR_MEMO = 200000
# Order-preserving compaction before the greedy moves: rounds (0: none), and how far (mm, per
# axis) a part may move in it.
COMPACT_ROUNDS = 4
COMPACT_REACH_MM = 3.0
# Compaction keeps slots this much (mm, per side) further apart than they need.
SEPARATION_EPS = 1e-3
# The compaction solve's tolerance (mm; its result may miss a constraint by ten times this,
# well inside SEPARATION_EPS, and the legalizer's checker has the last word).
COMPACT_TOL = 5e-5
# Compactions tried, each holding back the parts the last one left illegal.
COMPACT_RETRIES = 4
# After a move, the parts within this distance (mm) of where it left or arrived are re-costed.
TOUCH_MM = 2.0
# The slide distances, in slot-grid steps.
SLIDE_STEPS = (1, 2, 3, 4, 6, 8, 12, 16, 24, 32)
# Wrong-side candidates: steps off the edge, and along it either side of the pin.
WRONG_OUT = (0, 1, 2, 3, 4, 6, 8)
WRONG_ALONG = (0, 1, -1, 2, -2, 4, -4, 8, -8)
# A channel penalty may not rise by more than this (mm^2).
PENALTY_EPS = 1e-9
KINDS = ("turn", "slide", "swap", "wrong_side", "compact")
_DIRS = ("east", "west", "north", "south")


def reach(comp) -> float:
    """The movement budget (mm) of a turn or slide of ``comp`` from its legal pose."""
    w, h = comp.courtyard
    return max(REACH_MM, REACH_SHARE * max(float(w), float(h)))


def _quarter(rot) -> Optional[int]:
    k = float(rot) / 90.0
    q = int(round(k))
    return q % 4 if abs(k - q) < 1e-9 else None


def pins_of(comp) -> List[Tuple[float, float]]:
    """Pad positions of ``comp``: exact at a quarter turn (no trigonometry)."""
    q = _quarter(comp.rot)
    if q is None:
        return [xy for _, xy in pin_positions(comp)]
    px, py = float(comp.pos[0]), float(comp.pos[1])
    out = []
    for pad in comp.pads:
        ox, oy = float(pad.offset[0]), float(pad.offset[1])
        if q == 0:
            rx, ry = ox, oy
        elif q == 1:
            rx, ry = -oy, ox
        elif q == 2:
            rx, ry = -ox, -oy
        else:
            rx, ry = oy, -ox
        out.append((px + rx, py + ry))
    return out


def _box(comp):
    r = courtyard_rect(comp)
    return (r.cx - r.w / 2.0, r.cx + r.w / 2.0, r.cy - r.h / 2.0, r.cy + r.h / 2.0)


def _centre(box):
    return ((box[0] + box[1]) / 2.0, (box[2] + box[3]) / 2.0)


def _dist(a, b):
    dx, dy = a[0] - b[0], a[1] - b[1]
    return math.sqrt(dx * dx + dy * dy)


def _gap(a, b) -> float:
    """The distance (mm) between two boxes (x0, x1, y0, y1); 0 when they touch or overlap."""
    dx = max(0.0, max(a[0], b[0]) - min(a[1], b[1]))
    dy = max(0.0, max(a[2], b[2]) - min(a[3], b[3]))
    return math.sqrt(dx * dx + dy * dy)


def _overlap_area(a, b) -> float:
    """The area (mm^2) two boxes (x0, x1, y0, y1) share."""
    dx = min(a[1], b[1]) - max(a[0], b[0])
    dy = min(a[3], b[3]) - max(a[2], b[2])
    return dx * dy if dx > 0 and dy > 0 else 0.0


def _segment_gap(box, a, b) -> float:
    """The distance (mm) between the box (x0, x1, y0, y1) and the segment ``a``-``b``; 0 when
    they meet."""
    x0, x1, y0, y1 = box
    dx, dy = b[0] - a[0], b[1] - a[1]
    lo, hi = 0.0, 1.0
    crosses = True
    for p, q in ((-dx, a[0] - x0), (dx, x1 - a[0]), (-dy, a[1] - y0), (dy, y1 - a[1])):
        if p == 0.0:
            if q < 0.0:
                crosses = False
                break
            continue
        t = q / p
        if p < 0.0:
            lo = max(lo, t)
        else:
            hi = min(hi, t)
        if lo > hi:
            crosses = False
            break
    if crosses:
        return 0.0

    def point_box(px, py):
        ex = max(0.0, x0 - px, px - x1)
        ey = max(0.0, y0 - py, py - y1)
        return math.sqrt(ex * ex + ey * ey)

    def point_segment(px, py):
        ll = dx * dx + dy * dy
        t = 0.0 if ll == 0.0 else max(0.0, min(1.0, ((px - a[0]) * dx + (py - a[1]) * dy) / ll))
        ex, ey = a[0] + t * dx - px, a[1] + t * dy - py
        return math.sqrt(ex * ex + ey * ey)

    corners = ((x0, y0), (x0, y1), (x1, y0), (x1, y1))
    return min([point_box(*a), point_box(*b)] + [point_segment(*c) for c in corners])


def matched_nets(constraints) -> set:
    """The differential-pair and length-match nets of ``constraints``."""
    out = set()
    for pair in getattr(constraints, "diff_pairs", None) or []:
        out |= {pair.p, pair.n}
    for group in getattr(constraints, "length_matches", None) or []:
        out |= set(group.nets)
    return out


def _open(r0, r1, b0, b1):
    """The open interval (r0, r1) meets [b0, b1] (a point when degenerate)."""
    if b1 - b0 > 1e-9:
        return min(r1, b1) - max(r0, b0) > 1e-9
    return r0 + 1e-9 < b0 < r1 - 1e-9


def detour(bbox, rect) -> float:
    """The extra Manhattan length (mm) a route inside ``bbox`` (x0, x1, y0, y1) needs around
    ``rect`` (x0, x1, y0, y1): zero unless the rectangle cuts the box from one side to the other,
    then twice the shorter overshoot round its nearer end."""
    bx0, bx1, by0, by1 = bbox
    rx0, rx1, ry0, ry1 = rect
    if rx1 - rx0 <= 0 or ry1 - ry0 <= 0:
        return 0.0
    if not (_open(rx0, rx1, bx0, bx1) and _open(ry0, ry1, by0, by1)):
        return 0.0
    best = None
    if ry0 <= by0 and ry1 >= by1:  # a wall across the box in y: round its top or bottom
        best = 2.0 * min(ry1 - by1, by0 - ry0)
    if rx0 <= bx0 and rx1 >= bx1:  # across in x: round its left or right end
        v = 2.0 * min(rx1 - bx1, bx0 - rx0)
        best = v if best is None else min(best, v)
    return 0.0 if best is None else max(0.0, best)


def facing(box, xy) -> Optional[int]:
    """The edge (index into east, west, north, south) a pad at ``xy`` of a part with courtyard
    ``box`` faces, or None for an inner pad (:data:`EDGE_PIN_SHARE`)."""
    x0, x1, y0, y1 = box
    d = (x1 - xy[0], xy[0] - x0, y1 - xy[1], xy[1] - y0)
    best = min(range(4), key=lambda i: (d[i], i))
    if d[best] > EDGE_PIN_SHARE * min(x1 - x0, y1 - y0):
        return None
    return best


def own_rect(box, xy, face) -> Tuple[float, float, float, float]:
    """The part of a courtyard ``box`` behind a pad at ``xy`` facing ``face``: a route from the
    pad that has to cross it goes round the part."""
    x0, x1, y0, y1 = box
    e = OBSTACLE_EPS
    if face == 0:
        return (x0 + e, xy[0] - e, y0 + e, y1 - e)
    if face == 1:
        return (xy[0] + e, x1 - e, y0 + e, y1 - e)
    if face == 2:
        return (x0 + e, x1 - e, y0 + e, xy[1] - e)
    return (x0 + e, x1 - e, xy[1] + e, y1 - e)


def mst(points) -> List[Tuple[int, int]]:
    """Prim's Manhattan minimum spanning tree over ``points``: index pairs (ties: lower index)."""
    n = len(points)
    if n < 2:
        return []
    inside = [False] * n
    best = [math.inf] * n
    parent = [-1] * n
    best[0] = 0.0
    out = []
    for _ in range(n):
        u = -1
        for i in range(n):
            if not inside[i] and (u < 0 or best[i] < best[u]):
                u = i
        inside[u] = True
        if parent[u] >= 0:
            out.append((parent[u], u))
        ux, uy = points[u]
        for v in range(n):
            if not inside[v]:
                d = abs(points[v][0] - ux) + abs(points[v][1] - uy)
                if d < best[v]:
                    best[v], parent[v] = d, u
    return out


def held_refs(constraints) -> set:
    """Parts held by a hard group, region, align or edge band: they only turn and slide."""
    from pnr.constraints import Enforcement

    out = set()
    for con in constraints.constraints:
        hard = con.enforcement == Enforcement.HARD
        if con.kind in ("region", "align", "edge_align", "group") and hard:
            out |= set(con.refs)
            anchor = (con.params or {}).get("anchor")
            if anchor:
                out.add(anchor)
    return out


class _State:
    """The board, its nets' segments and crossings, and spatial indexes (incremental)."""

    def __init__(self, out, constraints, skip, channel_model, channel_ratio, matched=()):
        from .geometry import occupied_sides, outline_size
        from .regions import soft_refs

        self.graph = out
        self.comps = {c.ref: c for c in out.components}
        self.channels = channel_model
        self.ratio = channel_ratio
        self.constraints = constraints
        self.size = outline_size(out, constraints)
        self.soft = soft_refs(constraints)
        self.edges: Dict[str, List[Tuple[str, float]]] = {}
        self.groups: Dict[str, List[Tuple[str, str, float, float]]] = {}
        from pnr.constraints import Enforcement

        for con in constraints.constraints:
            soft = con.enforcement != Enforcement.HARD
            if con.kind == "edge_align" and con.params.get("edge") and soft:
                for ref in con.refs:
                    self.edges.setdefault(ref, []).append(
                        (con.params["edge"], float(con.weight or 1.0))
                    )
            elif con.kind == "group" and con.params.get("anchor") and soft:
                anchor = con.params["anchor"]
                radius = float(con.params.get("radius_mm") or 5.0)
                for ref in con.refs:
                    if ref != anchor:
                        term = (anchor, ref, radius, float(con.weight or 1.0))
                        self.groups.setdefault(anchor, []).append(term)
                        self.groups.setdefault(ref, []).append(term)
        self.th = {}
        self.sides = {}
        for c in out.components:
            block = str(c.footprint).startswith("block:")
            self.th[c.ref] = (
                (not block) and (not c.smd_body) and any(p.through_hole for p in c.pads)
            )
            self.sides[c.ref] = frozenset(occupied_sides(c))
        nets: Dict[str, List[Tuple[str, int]]] = {}
        for c in sorted(out.components, key=lambda c: c.ref):
            for i, pad in enumerate(c.pads):
                if pad.net and (pad.net not in skip or pad.net in matched):
                    nets.setdefault(pad.net, []).append((c.ref, i))
        self.nets = {
            n: p for n, p in sorted(nets.items()) if len({r for r, _ in p}) >= 2 and n not in skip
        }
        self.part_nets: Dict[str, List[str]] = {}
        for n, pins in self.nets.items():
            for r in sorted({r for r, _ in pins}):
                self.part_nets.setdefault(r, []).append(n)
        self.pins = {c.ref: pins_of(c) for c in out.components}
        self.box = {c.ref: _box(c) for c in out.components}
        # Pad arrays (a ball grid): their routes drop through their fanout vias, so nothing moves
        # to their other side (their crossing still counts: a route across the array's escape
        # field costs as one round it would).
        self.array = {}
        for c in out.components:
            pins = self.pins[c.ref]
            x0, x1, y0, y1 = self.box[c.ref]
            depth = [min(x1 - x, x - x0, y1 - y, y - y0) for x, y in pins]
            outer = min(depth, default=0.0) + ARRAY_RING_MM
            inner = sum(1 for d in depth if d > outer)
            # A grid, not two rows (an SOIC-16's end pads sit nearer its short edges).
            cols = len({round(float(p.offset[0]), 2) for p in c.pads})
            rows = len({round(float(p.offset[1]), 2) for p in c.pads})
            self.array[c.ref] = (
                len(pins) >= ARRAY_PADS
                and min(cols, rows) >= ARRAY_LINES
                and inner > ARRAY_INNER * len(pins)
            )
        self.arrays = sorted(r for r, v in self.array.items() if v)
        # The straight lines between consecutive pins (a spanning tree) of every matched net.
        self.corridors = []
        for net in sorted(matched or ()):
            pins = [(r, i) for r, i in nets.get(net, ())]
            pts = [self.pins[r][i] for r, i in pins]
            for i, j in mst(pts):
                self.corridors.append((frozenset((pins[i][0], pins[j][0])), pts[i], pts[j]))
        self.cells: Dict[Tuple[int, int], set] = {}
        self.cells_of: Dict[str, list] = {}
        for ref in sorted(self.comps):
            self._index(ref)
        self.seg_cells: Dict[Tuple[int, int], set] = {}
        self.segs: Dict[str, list] = {}
        self.seg_known: Dict[str, dict] = {}
        self.pairs: Dict[tuple, float] = {}
        self.hp: Dict[str, float] = {}
        self.xc: Dict[str, float] = {}
        for n in self.nets:
            self._net(n)

    # -- indexes ---------------------------------------------------------------------------
    @staticmethod
    def _cells(box, grow=0.0):
        return [
            (i, j)
            for i in range(
                math.floor((box[0] - grow) / BUCKET_MM), math.floor((box[1] + grow) / BUCKET_MM) + 1
            )
            for j in range(
                math.floor((box[2] - grow) / BUCKET_MM), math.floor((box[3] + grow) / BUCKET_MM) + 1
            )
        ]

    def _index(self, ref):
        for cell in self.cells_of.get(ref, ()):
            self.cells[cell].discard(ref)
        self.cells_of[ref] = self._cells(self.box[ref])
        for cell in self.cells_of[ref]:
            self.cells.setdefault(cell, set()).add(ref)

    def by_array(self, ref) -> bool:
        """``ref`` (not an array) lies within :data:`ARRAY_FREEZE_MM` of a pad array: it stays
        where the legalizer put it."""
        if self.array.get(ref):
            return False
        return any(_gap(self.box[ref], self.box[a]) < ARRAY_FREEZE_MM - 1e-9 for a in self.arrays)

    def zone_ok(self, ref, new_box) -> bool:
        """Moving ``ref`` from its present box to ``new_box`` keeps its gap to every pad array
        (:data:`ARRAY_ZONE_MM`, or what it has if less) and to every matched net's corridor
        (:data:`CORRIDOR_MM`)."""
        old = self.box[ref]
        for a in self.arrays:
            if a == ref:
                continue
            gap = _gap(new_box, self.box[a])
            if gap < ARRAY_ZONE_MM - 1e-9 and gap < _gap(old, self.box[a]) - 1e-9:
                return False
        for refs, pa, pb in self.corridors:
            if ref in refs:
                continue
            gap = _segment_gap(new_box, pa, pb)
            if gap < CORRIDOR_MM - 1e-9 and gap < _segment_gap(old, pa, pb) - 1e-9:
                return False
            # Nor deeper into the box the pair's route may take (between its two pins).
            area = (
                min(pa[0], pb[0]) - CORRIDOR_BOX_MM,
                max(pa[0], pb[0]) + CORRIDOR_BOX_MM,
                min(pa[1], pb[1]) - CORRIDOR_BOX_MM,
                max(pa[1], pb[1]) + CORRIDOR_BOX_MM,
            )
            if _overlap_area(new_box, area) > _overlap_area(old, area) + 1e-9:
                return False
        return True

    def near(self, box, grow=0.0) -> List[str]:
        found = set()
        for cell in self._cells(box, grow):
            found |= self.cells.get(cell, set())
        x0, x1, y0, y1 = box[0] - grow, box[1] + grow, box[2] - grow, box[3] + grow
        return sorted(
            r
            for r in found
            if self.box[r][0] <= x1
            and self.box[r][1] >= x0
            and self.box[r][2] <= y1
            and self.box[r][3] >= y0
        )

    def _seg_index(self, net, add):
        for k, seg in enumerate(self.segs.get(net, ())):
            for cell in self._cells(seg[0]):
                if add:
                    self.seg_cells.setdefault(cell, set()).add((net, k))
                else:
                    self.seg_cells[cell].discard((net, k))

    def segs_near(self, box) -> List[Tuple[str, int]]:
        found = set()
        for cell in self._cells(box):
            found |= self.seg_cells.get(cell, set())
        return sorted(found)

    # -- costs -----------------------------------------------------------------------------
    def _layer(self, ra, rb):
        ta, tb = self.th[ra], self.th[rb]
        if ta and tb:
            return None
        sa, sb = self.comps[ra].side, self.comps[rb].side
        if ta:
            return sb
        if tb:
            return sa
        return sa if sa == sb else None

    def _blocks(self, ref, layer):
        return (not self.th[ref]) and layer in self.sides[ref]

    def seg_cost(self, a, b, pa, pb, trial=None) -> float:
        """The crossing detour of the segment from pin ``a`` (ref, index) at ``pa`` to pin
        ``b`` at ``pb``; ``trial`` ({ref: box}) overrides the boxes of moving parts."""
        ra, rb = a[0], b[0]
        if ra == rb:
            return 0.0
        layer = self._layer(ra, rb)
        if layer is None:
            return 0.0
        bbox = (min(pa[0], pb[0]), max(pa[0], pb[0]), min(pa[1], pb[1]), max(pa[1], pb[1]))
        trial = trial or {}
        total = 0.0
        e = OBSTACLE_EPS
        for ref in self.near(bbox):
            if ref in trial or ref == ra or ref == rb or not self._blocks(ref, layer):
                continue
            x0, x1, y0, y1 = self.box[ref]
            total += detour(bbox, (x0 + e, x1 - e, y0 + e, y1 - e))
        for ref in sorted(trial):
            if ref == ra or ref == rb or not self._blocks(ref, layer):
                continue
            x0, x1, y0, y1 = trial[ref]
            total += detour(bbox, (x0 + e, x1 - e, y0 + e, y1 - e))
        for ref, xy in ((ra, pa), (rb, pb)):
            if self.th[ref]:
                continue
            box = trial.get(ref, self.box[ref])
            face = facing(box, xy)
            if face is not None:
                total += detour(bbox, own_rect(box, xy, face))
        return total

    def net_cost(self, net, pins=None, trial=None):
        """``(half-perimeter, crossing, segments)`` of ``net`` (``pins``: {ref: positions}
        overrides; ``trial``: {ref: box})."""
        pins = pins or {}
        pts = []
        for ref, i in self.nets[net]:
            got = pins.get(ref)
            pts.append((got if got is not None else self.pins[ref])[i])
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        hp = (max(xs) - min(xs)) + (max(ys) - min(ys))
        segs = []
        x = 0.0
        if len(pts) <= MST_PINS:
            members = self.nets[net]
            trial = trial or {}
            known = self.seg_known.get(net, {}) if trial else {}
            e = OBSTACLE_EPS
            for i, j in mst(pts):
                pa, pb = pts[i], pts[j]
                a, b = members[i], members[j]
                bbox = (
                    min(pa[0], pb[0]),
                    max(pa[0], pb[0]),
                    min(pa[1], pb[1]),
                    max(pa[1], pb[1]),
                )
                c = known.get((a, b)) if a[0] not in trial and b[0] not in trial else None
                if c is None:
                    c = self.seg_cost(a, b, pa, pb, trial)
                else:
                    # A segment of the present tree whose ends stay: only the moving parts'
                    # blocking changes.
                    layer = self._layer(a[0], b[0])
                    for r in sorted(trial):
                        if layer is None or not self._blocks(r, layer):
                            continue
                        o, t = self.box[r], trial[r]
                        c -= detour(bbox, (o[0] + e, o[1] - e, o[2] + e, o[3] - e))
                        c += detour(bbox, (t[0] + e, t[1] - e, t[2] + e, t[3] - e))
                segs.append((bbox, a, b, pa, pb, c))
                x += c
        return hp, x, segs

    def _net(self, net):
        if net in self.segs:
            self._seg_index(net, False)
        self.hp[net], self.xc[net], self.segs[net] = self.net_cost(net)
        self.seg_known[net] = {(a, b): c for _bb, a, b, _pa, _pb, c in self.segs[net]}
        self._seg_index(net, True)

    def penalty(self, comp, others) -> float:
        """The channel penalty (mm^2) of ``comp`` against ``others`` at their present poses, a
        sum of pair terms in ``others``' order, each memoized by the two poses."""
        if self.channels is None:
            return 0.0
        total = 0.0
        a = (comp.ref, comp.pos[0], comp.pos[1], comp.rot, comp.side)
        for other in others:
            key = (a, (other.ref, other.pos[0], other.pos[1], other.rot, other.side))
            got = self.pairs.get(key)
            if got is None:
                if len(self.pairs) > PAIR_MEMO:
                    self.pairs.clear()
                got = float(self.channels.penalty(comp, [other], comp.pos[0], comp.pos[1]))
                self.pairs[key] = got
            total += got
        return total

    def soft_cost(self, comp) -> float:
        cost = 0.0
        for edge, weight in self.edges.get(comp.ref, ()):
            cost += weight * edge_distance(comp, edge, *self.size) ** 2
        if comp.ref in self.soft:
            from .regions import soft_penalty

            cost += soft_penalty(self.graph.components, self.constraints, comp, comp.pos)
        return cost

    def group_cost(self, refs) -> float:
        terms = sorted({t for r in refs for t in self.groups.get(r, ())})
        cost = 0.0
        for anchor, member, radius, weight in terms:
            d = _dist(self.comps[anchor].pos, self.comps[member].pos)
            cost += weight * max(0.0, d - radius) ** 2
        return cost

    def wire_of(self, refs) -> float:
        """Current half-perimeter plus crossing of the nets of ``refs``."""
        nets = sorted({n for r in refs for n in self.part_nets.get(r, ())})
        return sum(self.hp[n] + self.xc[n] for n in nets)

    def evaluate(self, moving) -> float:
        """The wiring gain (mm: half-perimeter plus crossing, before minus after) of the parts
        ``moving`` (components at their trial poses; ``self.box`` and ``self.pins`` still hold
        the current ones): their nets, and the other nets' segments they leave or block."""
        refs = [c.ref for c in moving]
        trial_box = {c.ref: _box(c) for c in moving}
        trial_pins = {c.ref: pins_of(c) for c in moving}
        nets = sorted({n for r in refs for n in self.part_nets.get(r, ())})
        before = after = 0.0
        for n in nets:
            hp, x, _ = self.net_cost(n, trial_pins, trial_box)
            before += self.hp[n] + self.xc[n]
            after += hp + x
        # Other nets' segments the moving parts leave or come to block.
        mine = set(nets)
        seen = set()
        for r in refs:
            for box in (self.box[r], trial_box[r]):
                for key in self.segs_near(box):
                    if key[0] not in mine:
                        seen.add(key)
        if seen:
            e = OBSTACLE_EPS
            for net, k in sorted(seen):
                bbox, a, b, _pa, _pb, _c = self.segs[net][k]
                layer = self._layer(a[0], b[0])
                if layer is None:
                    continue
                for r in refs:
                    if r in (a[0], b[0]) or not self._blocks(r, layer):
                        continue
                    o, t = self.box[r], trial_box[r]
                    before += detour(bbox, (o[0] + e, o[1] - e, o[2] + e, o[3] - e))
                    after += detour(bbox, (t[0] + e, t[1] - e, t[2] + e, t[3] - e))
        return before - after

    def accept(self, refs) -> set:
        """Take the components ``refs`` at their (new) poses: refresh their pins, boxes and
        indexes, and every net whose cost may change (theirs, and those with a segment where
        the parts left or arrived). Returns those nets."""
        nets = {n for r in refs for n in self.part_nets.get(r, ())}
        for r in refs:
            nets |= {net for net, _k in self.segs_near(self.box[r])}
        for r in refs:
            comp = self.comps[r]
            self.pins[r] = pins_of(comp)
            self.box[r] = _box(comp)
            self._index(r)
        for r in refs:
            nets |= {net for net, _k in self.segs_near(self.box[r])}
        for n in sorted(nets):
            self._net(n)
        return nets

    def totals(self):
        return (
            sum(self.hp[n] for n in self.nets),
            sum(self.xc[n] for n in self.nets),
        )


def plane_nets_of(channel_model) -> frozenset:
    """The plane nets of ``channel_model`` (:func:`pnr.place.legalize.plane_nets`): left out of
    the wiring."""
    from .legalize import plane_nets

    return plane_nets(channel_model)


def _size_class(a, b) -> bool:
    """Swap partners: the same footprint, or each courtyard side within :data:`SIZE_CLASS`."""
    if a.footprint and a.footprint == b.footprint:
        return True
    da, db = sorted(map(float, a.courtyard)), sorted(map(float, b.courtyard))
    return all(abs(x - y) <= SIZE_CLASS * max(x, y) for x, y in zip(da, db))


def _pose(comp):
    return (float(comp.pos[0]), float(comp.pos[1]), float(comp.rot))


def _set(comp, pose):
    comp.pos, comp.rot = (pose[0], pose[1]), pose[2]


def channel_ratio() -> float:
    """The packer's channel weight over its wirelength weight (``PNR_LEGALIZE_HPWL``, the
    ``WIRE`` part's 4 when unset): DP prices a pose as the packer that gave the best copper."""
    from pnr import legalize_flags

    wire = legalize_flags.legalize_hpwl() or legalize_flags.COMPACT_WIRE_WEIGHT
    return CHANNEL_WEIGHT / float(wire)


# The legalizer's channel weight (pnr.place.legalize.legalize ``channel_weight``).
CHANNEL_WEIGHT = 25.0


def empty_record() -> dict:
    return dict(
        moves={k: 0 for k in KINDS},
        movable=0,
        moved=0,
        sum_mm=0.0,
        max_mm=0.0,
        median_mm=0.0,
        hpwl_before=0.0,
        hpwl_after=0.0,
        crossing_before=0.0,
        crossing_after=0.0,
        channel_delta=0.0,
        wire_saved=0.0,
        topology=1.0,
        seconds=0.0,
    )


def improve_detail(
    graph: BoardGraph,
    constraints,
    *,
    clearance: float,
    spread: float = 1.0,
    inflation: Optional[Dict[str, float]] = None,
    margins: Optional[Dict[str, float]] = None,
    pad_edge: Optional[Tuple[float, float]] = None,
    channel_model=None,
    grid_mm: float = 0.25,
    allow_rotation: bool = True,
    ratio: Optional[float] = None,
    global_graph=None,
    compact_rounds: Optional[int] = None,
) -> Tuple[BoardGraph, dict]:
    """``(board, record)``: a copy of the legal ``graph`` after the detailed-placement pass
    (the module docstring) and its record (:func:`summary` drops the per-move log). The
    legalizer's ``clearance``, ``spread``, ``inflation``, ``margins``, ``pad_edge`` and
    ``grid_mm`` apply; ``channel_model`` gives the channel penalty and the plane nets; ``ratio``
    (None: :func:`channel_ratio`) weighs the penalty; ``global_graph`` (the global placement)
    adds the topology kept from it. A board that is not legal to begin with comes back
    unchanged."""
    from .metrics import hard_violations, pose_checker
    from .motion import motion as motion_of
    from .reorient import _set_turn, frozen_refs
    from .sides import macro

    started = time.monotonic()
    out = BoardGraph.from_json(graph.to_json())
    record = empty_record()
    legal_args = dict(
        clearance=clearance,
        spread=spread,
        pad_edge=pad_edge,
        inflation=inflation,
        margins=margins or None,
    )
    try:
        legal = pose_checker(out, constraints, **legal_args)
    except ValueError:
        record["skipped"] = "illegal_input"
        return out, record
    skip = plane_nets_of(channel_model)
    frozen = frozen_refs(out, constraints)
    held = held_refs(constraints)
    st = _State(
        out,
        constraints,
        skip,
        channel_model,
        channel_ratio() if ratio is None else ratio,
        matched=matched_nets(constraints),
    )
    movable = [
        c.ref
        for c in sorted(out.components, key=lambda c: c.ref)
        if c.ref not in frozen
        and not macro(c)
        and not c.reserves
        and c.ref in st.part_nets
        and not st.by_array(c.ref)
    ]
    record["movable"] = len(movable)
    hp0, x0 = st.totals()
    record.update(hpwl_before=round(hp0, 4), crossing_before=round(x0, 4))
    if not movable:
        record["hpwl_after"], record["crossing_after"] = record["hpwl_before"], x0
        record["seconds"] = round(time.monotonic() - started, 3)
        return out, record
    movable_set = frozenset(movable)
    g = float(grid_mm)
    anchor = {r: _centre(st.box[r]) for r in movable}
    moves_of = {r: 0 for r in movable}
    swapped = set()
    rank = {k: i for i, k in enumerate(KINDS)}

    def turns(comp):
        if not allow_rotation:
            return []
        return [(comp.rot + 90.0 * k) % 360.0 for k in (1, 2, 3)]

    def neighbours(refs, boxes):
        """Channel partners of ``boxes``: the other parts within :data:`NEAR_MM` that face one
        of them (their courtyards overlap in x or in y), sorted."""
        found = set()
        for box in boxes:
            for r in st.near(box, NEAR_MM):
                o = st.box[r]
                if (o[0] < box[1] and o[1] > box[0]) or (o[2] < box[3] and o[3] > box[2]):
                    found.add(r)
        return [st.comps[r] for r in sorted(found - set(refs))]

    def extra(moving, poses):
        """Channel-penalty and soft-term increase (mm) of ``moving`` from the current poses to
        ``poses`` (each component is left at its current pose)."""
        refs = [c.ref for c in moving]
        current = [_pose(c) for c in moving]
        boxes = [st.box[r] for r in refs]
        for c, p in zip(moving, poses):
            _set(c, p)
        boxes += [_box(c) for c in moving]
        others = neighbours(refs, boxes)
        after = sum(st.penalty(c, others) for c in moving)
        if len(moving) == 2:
            after += st.penalty(moving[0], [moving[1]])
        soft_after = sum(st.soft_cost(c) for c in moving) + st.group_cost(refs)
        for c, p in zip(moving, current):
            _set(c, p)
        before = sum(st.penalty(c, others) for c in moving)
        if len(moving) == 2:
            before += st.penalty(moving[0], [moving[1]])
        soft_before = sum(st.soft_cost(c) for c in moving) + st.group_cost(refs)
        return st.ratio * (after - before) + (soft_after - soft_before), after - before

    def wire_gain(moving, poses):
        current = [_pose(c) for c in moving]
        for c, p in zip(moving, poses):
            _set(c, p)
        got = st.evaluate(moving)
        for c, p in zip(moving, current):
            _set(c, p)
        return got

    def posed_at(comp, centre, rot):
        """The pose of ``comp`` turned to ``rot`` with its courtyard centred on ``centre``."""
        keep = _pose(comp)
        _set_turn(comp, centre, rot)
        pose = _pose(comp)
        _set(comp, keep)
        return pose

    def legal_at(moving):
        """``moving`` ([(component, pose)]) is legal (each component left where it was)."""
        comps = [c for c, _ in moving]
        current = [_pose(c) for c in comps]
        for c, p in moving:
            _set(c, p)
        ok = legal(comps)
        for c, p in zip(comps, current):
            _set(c, p)
        return ok

    def blockers(ref, axis, sign, shift):
        """The parts right in front of ``ref`` (within :data:`BAND_GAP_MM`, facing it across
        the slide ``axis`` in direction ``sign``) shifted by ``shift`` too: ``[(component,
        pose)]``, or None when one of them may not slide that far (frozen, out of moves or past
        its budget) or there are more than :data:`BAND_PARTS` - 1."""
        box = st.box[ref]
        lo, hi = (2, 3) if axis == 0 else (0, 1)
        front = 1 if sign > 0 else 0
        edge = box[2 * axis + front]
        out = []
        for r in st.near(box, BAND_GAP_MM + abs(shift[0]) + abs(shift[1])):
            if r == ref:
                continue
            o = st.box[r]
            if not (o[lo] < box[hi] and o[hi] > box[lo]):
                continue  # not in the part's lane
            near_edge = o[2 * axis + (0 if sign > 0 else 1)]
            gap = (near_edge - edge) * sign
            if gap < -1e-9 or gap > BAND_GAP_MM + abs(shift[axis]):
                continue
            if r not in movable_set or moves_of[r] >= MOVES_PER_PART:
                return None
            oc = _centre(o)
            if _dist((oc[0] + shift[0], oc[1] + shift[1]), anchor[r]) > reach(st.comps[r]) + 1e-9:
                return None
            p = _pose(st.comps[r])
            out.append((st.comps[r], (p[0] + shift[0], p[1] + shift[1], p[2])))
        if not out or len(out) >= BAND_PARTS:
            return None
        return out

    def candidates(ref):
        """``[(kind, index, [(component, pose)], other part, wiring gain or None)]`` of
        ``ref``'s moves, generated in a fixed order; slides stop where the wiring stops
        improving."""
        comp = st.comps[ref]
        out_moves = []
        centre = _centre(st.box[ref])
        here = _pose(comp)
        for k, rot in enumerate(turns(comp)):
            out_moves.append(("turn", k, [(comp, posed_at(comp, centre, rot))], None, None))
        budget = reach(comp)
        idx = 0
        for axis in (0, 1):
            for sign in (1.0, -1.0):
                last = 0.0
                for step in SLIDE_STEPS:
                    d = sign * step * g
                    shift = (d, 0.0) if axis == 0 else (0.0, d)
                    c = (centre[0] + shift[0], centre[1] + shift[1])
                    if _dist(c, anchor[ref]) > budget + 1e-9:
                        break
                    moving = [(comp, (here[0] + shift[0], here[1] + shift[1], here[2]))]
                    if not legal_at(moving):
                        # Blocked: shift the parts in front along too (a band, order kept).
                        band = blockers(ref, axis, sign, shift)
                        if band is None:
                            break
                        moving += band
                        if not legal_at(moving):
                            break
                    gain = wire_gain([m[0] for m in moving], [m[1] for m in moving])
                    if gain < last - 1e-9:
                        break
                    last = gain
                    out_moves.append(("slide", idx, moving, None, gain))
                    idx += 1
        if ref in held:
            return out_moves
        if ref not in swapped:
            box = st.box[ref]
            w, h = box[1] - box[0], box[3] - box[2]
            idx = 0
            for other in st.near(box, w + h):
                if other == ref or other not in movable_set or other in held or other in swapped:
                    continue
                if moves_of[other] >= MOVES_PER_PART:
                    continue
                partner = st.comps[other]
                if not _size_class(comp, partner):
                    continue
                oc = _centre(st.box[other])
                if _dist(centre, oc) > w + h + 1e-9:
                    continue
                span = (
                    min(centre[0], oc[0]),
                    max(centre[0], oc[0]),
                    min(centre[1], oc[1]),
                    max(centre[1], oc[1]),
                )
                between = [
                    r
                    for r in st.near(span)
                    if r not in (ref, other)
                    and _open(st.box[r][0], st.box[r][1], span[0], span[1])
                    and _open(st.box[r][2], st.box[r][3], span[2], span[3])
                ]
                if between:
                    continue
                # Each picks its turn: this part's best with the partner at its present turn,
                # then the partner's best with this part's choice.
                theirs = posed_at(partner, centre, partner.rot)
                mine = posed_at(comp, oc, comp.rot)
                if wire_gain([comp, partner], [mine, theirs]) <= -SWAP_SLACK_MM:
                    continue  # far from paying off even before the turns are re-picked
                gain, mine = max(
                    (wire_gain([comp, partner], [p, theirs]), -k, p)
                    for k, p in enumerate(posed_at(comp, oc, r) for r in [comp.rot] + turns(comp))
                )[::2]
                gain, theirs = max(
                    (wire_gain([comp, partner], [mine, p]), -k, p)
                    for k, p in enumerate(
                        posed_at(partner, centre, r) for r in [partner.rot] + turns(partner)
                    )
                )[::2]
                out_moves.append(("swap", idx, [(comp, mine), (partner, theirs)], None, gain))
                idx += 1
        idx = 0
        area = (st.box[ref][1] - st.box[ref][0]) * (st.box[ref][3] - st.box[ref][2])
        seen = set()
        for net in st.part_nets.get(ref, ()):
            for bbox, a, b, pa, pb, _c in st.segs.get(net, ()):
                if a[0] == ref and b[0] != ref:
                    other, pin = b[0], pb
                elif b[0] == ref and a[0] != ref:
                    other, pin = a[0], pa
                else:
                    continue
                if st.th[other] or st.array[other] or (other, pin) in seen:
                    continue
                obox = st.box[other]
                if (obox[1] - obox[0]) * (obox[3] - obox[2]) <= area:
                    continue
                face = facing(obox, pin)
                if face is None or detour(bbox, own_rect(obox, pin, face)) <= 0.0:
                    continue
                seen.add((other, pin))
                # The budget: across the other part (and the gap to it) plus its own extent and
                # two grid cells.
                mine = st.box[ref]
                if face in (0, 1):
                    span = obox[1] - obox[0]
                    gap = max(0.0, obox[0] - mine[1], mine[0] - obox[1])
                else:
                    span = obox[3] - obox[2]
                    gap = max(0.0, obox[2] - mine[3], mine[2] - obox[3])
                own = max(float(comp.courtyard[0]), float(comp.courtyard[1]))
                if gap > max(1.0, own):
                    continue  # not beside the other part: a slide's job, not a wrong-side move
                limit = span + gap + own + 2.0 * g
                for rot in [comp.rot] + turns(comp):
                    probe = posed_at(comp, centre, rot)
                    keep_pose = _pose(comp)
                    _set(comp, probe)
                    pb_ = _box(comp)
                    _set(comp, keep_pose)
                    hx, hy = (pb_[1] - pb_[0]) / 2.0, (pb_[3] - pb_[2]) / 2.0
                    for k in WRONG_OUT:
                        for j in WRONG_ALONG:
                            if face == 0:
                                c = (obox[1] + hx + k * g, pin[1] + j * g)
                            elif face == 1:
                                c = (obox[0] - hx - k * g, pin[1] + j * g)
                            elif face == 2:
                                c = (pin[0] + j * g, obox[3] + hy + k * g)
                            else:
                                c = (pin[0] + j * g, obox[2] - hy - k * g)
                            if _dist(c, anchor[ref]) > limit + 1e-9:
                                continue
                            pose = posed_at(comp, c, rot)
                            out_moves.append(("wrong_side", idx, [(comp, pose)], other, None))
                            idx += 1
        return out_moves

    def best_move(ref):
        """The best legal move of ``ref``: ``(key, kind, [(ref, pose)], wire, channel)`` or
        None. Candidates are taken in order of wiring gain; each legal one is costed in full
        (channel penalty, soft terms), until no later one can beat the best (its wiring gain
        plus the moving parts' present penalty and soft cost, the most it could recover)."""
        if moves_of[ref] >= MOVES_PER_PART:
            return None
        scored = []
        for cand in candidates(ref):
            kind, idx, moving = cand[0], cand[1], cand[2]
            wire = cand[4]
            if wire is None:
                wire = wire_gain([c for c, _ in moving], [p for _, p in moving])
            if wire > GAIN_MM:
                scored.append((-wire, rank[kind], idx, cand, wire))
        if not scored:
            return None
        scored.sort(key=lambda s: s[:3])
        slack = {}

        def recoverable(comps):
            refs = tuple(sorted(c.ref for c in comps))
            if refs not in slack:
                others = neighbours(refs, [st.box[r] for r in refs])
                pen = sum(st.penalty(c, others) for c in comps)
                if len(comps) == 2:
                    pen += st.penalty(comps[0], [comps[1]])
                soft = sum(st.soft_cost(c) for c in comps) + st.group_cost(refs)
                slack[refs] = st.ratio * pen + soft
            return slack[refs]

        found = None
        for _neg, kr, idx, cand, wire in scored:
            kind, moving = cand[0], cand[2]
            comps = [c for c, _ in moving]
            poses = [p for _, p in moving]
            if found is not None and wire + recoverable(comps) <= -found[0][0]:
                break
            current = [_pose(c) for c in comps]
            for c, p in moving:
                _set(c, p)
            ok = all(st.zone_ok(c.ref, _box(c)) for c in comps) and legal(comps)
            if ok and kind == "wrong_side":
                # The other part's escape channels beside the pin must not close.
                other = st.comps[cand[3]]
                after = st.penalty(other, comps)
                for c, p in zip(comps, current):
                    _set(c, p)
                ok = after <= st.penalty(other, comps) + PENALTY_EPS
            for c, p in zip(comps, current):
                _set(c, p)
            if not ok:
                continue
            more, chan = extra(comps, poses)
            total = wire - more
            if total <= GAIN_MM:
                continue
            key = (-total, kr, ref, idx)
            if found is None or key < found[0]:
                found = (key, kind, [(c.ref, p) for c, p in moving], wire, chan)
        return found

    log = []
    rounds = COMPACT_ROUNDS if compact_rounds is None else compact_rounds
    if rounds:
        compacted = _compact_stage(
            st, out, constraints, legal_args, movable, clearance, log, record, rounds
        )
        if compacted:
            legal = pose_checker(out, constraints, **legal_args)
    best = {r: best_move(r) for r in movable}
    chan_total = 0.0
    applied = 0
    refreshed = True  # the first costing covered every part
    while applied < MOVES_PER_MOVABLE * len(movable):
        live = [b for b in best.values() if b is not None]
        if not live:
            # A fixed point of the re-costed parts: cost every part once more (a move far
            # away may have opened one) and stop when none moves.
            if refreshed:
                break
            refreshed = True
            best = {r: best_move(r) for r in movable}
            continue
        pick = min(live, key=lambda b: b[0])
        ref = pick[0][2]
        # The cached move may be stale (a neighbour beyond the re-costed set moved): cost the
        # part again and take the move only when it still is the best.
        fresh = best_move(ref)
        if fresh is None or fresh[0] != pick[0]:
            best[ref] = fresh
            continue
        applied += 1
        refreshed = False
        _key, kind, moving, wire, chan = fresh
        refs = [r for r, _ in moving]
        old_boxes = [st.box[r] for r in refs]
        for r, pose in moving:
            comp = st.comps[r]
            log.append([r, kind, list(_pose(comp)), list(pose), round(-_key[0], 4)])
            _set(comp, pose)
            moves_of[r] += 1
            if kind in ("swap", "wrong_side"):
                anchor[r] = _centre(_box(comp))
            if kind == "swap":
                swapped.add(r)
        legal.update(refs)
        nets = st.accept(refs)
        record["moves"][kind] += 1
        chan_total += chan
        # Re-cost the parts on the nets whose cost changed and those near enough that their
        # legality or channels may have (a stale one is caught when picked).
        touched = {r for n in nets for r, _i in st.nets[n]}
        for box in old_boxes + [st.box[r] for r in refs]:
            touched |= set(st.near(box, TOUCH_MM))
        for r in sorted(touched & movable_set):
            best[r] = best_move(r)
    if any(hard_violations(out, constraints).values()):
        fallback = BoardGraph.from_json(graph.to_json())
        record.update(fallback=True, seconds=round(time.monotonic() - started, 3))
        record["hpwl_after"], record["crossing_after"] = record["hpwl_before"], x0
        return fallback, record
    hp1, x1 = st.totals()
    m = motion_of(graph, out, movable, snap_mm=1e-6)
    dists = sorted(d for d, t in m["parts"].values() if d > 1e-6 or t > 1e-6)
    record.update(
        moved=m["moved"],
        turned=m["turned"],
        sum_mm=m["sum_mm"],
        max_mm=m["max_mm"],
        median_mm=round(_median(dists), 4),
        hpwl_after=round(hp1, 4),
        crossing_after=round(x1, 4),
        channel_delta=round(chan_total, 6),
        # The estimated wiring (half-perimeter plus crossing) the whole pass saved, compaction
        # included.
        wire_saved=round((hp0 + x0) - (hp1 + x1), 4),
        topology=m["topology"],
        # The neighbour order kept from the legal poses (pnr.place.motion.neighbour_order).
        neighbour_order=m["neighbour_order"],
        neighbour_relations=m["neighbour_relations"],
        log=log,
        parts={r: v for r, v in m["parts"].items() if v[0] > 1e-6 or v[1] > 1e-6},
    )
    if global_graph is not None:
        g = motion_of(global_graph, out, movable)
        record["topology_global"] = g["topology"]
        # ... and from the global poses (global placement to detailed placement).
        record["neighbour_order_global"] = g["neighbour_order"]
        record["neighbour_relations_global"] = g["neighbour_relations"]
    record["seconds"] = round(time.monotonic() - started, 3)
    return out, record


def _targets(st, refs):
    """{ref: (x, y)}: where each part's courtyard centre would minimize its half-perimeter
    wirelength with the other parts where they are (per axis, the median of the ends of its
    nets' spans of the other pins, shifted by its pad's offset from the centre)."""
    out = {}
    for ref in refs:
        cx, cy = _centre(st.box[ref])
        ends = ([], [])
        for net in st.part_nets.get(ref, ()):
            mine = [st.pins[ref][i] for r, i in st.nets[net] if r == ref]
            others = [st.pins[r][i] for r, i in st.nets[net] if r != ref]
            if not others or not mine:
                continue
            for axis in (0, 1):
                lo = min(p[axis] for p in others)
                hi = max(p[axis] for p in others)
                off = sum(p[axis] for p in mine) / len(mine) - (cx if axis == 0 else cy)
                ends[axis].extend((lo - off, hi - off))
        target = []
        for axis, c in ((0, cx), (1, cy)):
            v = sorted(ends[axis])
            target.append(_median(v) if v else c)
        out[ref] = (target[0], target[1])
    return out


def compact(st, movable, obstacles, half, budget, width, height, rounds=COMPACT_ROUNDS, need=None):
    """Order-preserving compaction: ``{ref: (dx, dy)}`` shifts of the ``movable`` parts toward
    their wirelength targets (:func:`_targets`, half way each round), projected (weighted least
    squares, :func:`pnr.place.keep._solve_axis`) onto their present order: two parts side by side
    keep their order and slot distance along that axis, every part stays inside the outline and
    within ``budget`` ({ref: mm} per axis) of where it was. ``half`` ({ref: (hx, hy)}) are the
    slot half-sizes (clearance included) and ``obstacles`` the refs that do not move.
    ``need(front, back, axis)`` (refs; None: none) is the centre distance at which the routing
    channel between two facing parts is open: a pair keeps as much of it as it has now (an open
    channel is never closed, a short one never narrowed)."""
    from .keep import _solve_axis

    refs = list(movable)
    if not refs:
        return {}
    index = {r: k for k, r in enumerate(refs)}
    every = refs + [r for r in obstacles if r in half]
    pos = {r: _centre(st.box[r]) for r in every}
    start = {r: pos[r] for r in refs}
    weight = [max(4.0 * half[r][0] * half[r][1], 1e-6) for r in refs]

    def sides(r):
        return st.sides[r]

    # The order constraints, from the present layout: pairs that face each other across x (their
    # y spans overlap) or across y, within reach of each other.
    pairs = []
    for a_i, a in enumerate(every):
        for b in every[a_i + 1 :]:
            if a not in index and b not in index:
                continue
            if not (sides(a) & sides(b)):
                continue
            (ax, ay), (bx, by) = pos[a], pos[b]
            need_x = half[a][0] + half[b][0]
            need_y = half[a][1] + half[b][1]
            gx = abs(ax - bx) - need_x
            gy = abs(ay - by) - need_y
            reach_ab = budget.get(a, 0.0) + budget.get(b, 0.0)
            if gx >= gy and gy < 0 and gx < reach_ab:
                pairs.append((0, a, b) if ax <= bx else (0, b, a))
            elif gy > gx and gx < 0 and gy < reach_ab:
                pairs.append((1, a, b) if ay <= by else (1, b, a))
    # A pair nearer now than its slots (legal by the legalizer's own, finer rule) keeps at least
    # the distance it has.
    nearer = {}
    for ax, a, b in pairs:
        now = pos[b][ax] - pos[a][ax]
        if now < half[a][ax] + half[b][ax]:
            nearer[(ax, a, b)] = now
    # The distance each facing pair keeps: its slots side by side, or the channel it has now.
    keep = dict(nearer)
    if need is not None:
        for ax, a, b in pairs:
            want = need(a, b, ax)
            if want is not None:
                now = pos[b][ax] - pos[a][ax]
                floor = nearer.get((ax, a, b), half[a][ax] + half[b][ax])
                keep[(ax, a, b)] = max(floor, min(now, float(want)))
    current = dict(pos)
    known = {(a, b) for _ax, a, b in pairs} | {(b, a) for _ax, a, b in pairs}

    def solve(goal):
        new = {}
        for axis in (0, 1):
            lo, hi, target = [], [], []
            for r in refs:
                h = half[r][axis]
                limit = width if axis == 0 else height
                lo.append(max(h, start[r][axis] - budget[r]))
                hi.append(min(limit - h, start[r][axis] + budget[r]))
                c = current[r][axis]
                target.append(c + 0.5 * (goal[r][axis] - c))
            cons = []
            for ax, a, b in pairs:
                if ax != axis:
                    continue
                d = keep.get((ax, a, b), half[a][axis] + half[b][axis])
                if a in index and b in index:
                    cons.append((index[a], index[b], d))
                elif a in index:  # b an obstacle ahead of a
                    k = index[a]
                    hi[k] = min(hi[k], pos[b][axis] - d)
                else:
                    k = index[b]
                    lo[k] = max(lo[k], pos[a][axis] + d)
            if any(lo[k] > hi[k] + 1e-9 for k in range(len(refs))):
                return {}
            x, ok = _solve_axis(target, weight, lo, hi, cons, tol=COMPACT_TOL)
            if not ok:
                return None
            new[axis] = x
        return {r: (new[0][index[r]], new[1][index[r]]) for r in refs}

    for _ in range(rounds):
        goal = _targets(st, refs)
        for _attempt in range(6):
            got = solve(goal)
            if got is None:
                return {}
            # Pairs that come to overlap keep the order they had on the axis they were apart on.
            added = False
            at = dict(pos, **got)
            for a in refs:
                for b in every:
                    if a == b or (a, b) in known or not (sides(a) & sides(b)):
                        continue
                    dx = abs(at[a][0] - at[b][0]) - (half[a][0] + half[b][0])
                    dy = abs(at[a][1] - at[b][1]) - (half[a][1] + half[b][1])
                    if dx >= -1e-9 or dy >= -1e-9:
                        continue
                    (ax, ay), (bx, by) = current[a], current[b]
                    gx = abs(ax - bx) - (half[a][0] + half[b][0])
                    gy = abs(ay - by) - (half[a][1] + half[b][1])
                    if gx >= gy:
                        pairs.append((0, a, b) if ax <= bx else (0, b, a))
                    else:
                        pairs.append((1, a, b) if ay <= by else (1, b, a))
                    known.update(((a, b), (b, a)))
                    added = True
            if not added:
                break
        else:
            return {}
        current.update(got)
    return {
        r: (current[r][0] - start[r][0], current[r][1] - start[r][1])
        for r in refs
        if abs(current[r][0] - start[r][0]) > 1e-9 or abs(current[r][1] - start[r][1]) > 1e-9
    }


def _compact_stage(st, out, constraints, legal_args, movable, clearance, log, rec, rounds):
    """Run :func:`compact` on the board ``out`` (``st`` its state) and keep it when the board stays
    legal and the wiring (half-perimeter plus crossing) shortens; True when kept."""
    from .geometry import outline_size
    from .metrics import hard_violations, pose_checker

    width, height = outline_size(out, constraints)
    spread = float(legal_args.get("spread") or 1.0)
    inflation = legal_args.get("inflation") or {}
    margins = legal_args.get("margins") or {}
    half = {}
    for c in out.components:
        f = max(1.0, spread, float(inflation.get(c.ref, 1.0)))
        x0, x1, y0, y1 = st.box[c.ref]
        m = float(margins.get(c.ref, 0.0))
        half[c.ref] = (
            ((x1 - x0) * f + clearance) / 2.0 + m + SEPARATION_EPS,
            ((y1 - y0) * f + clearance) / 2.0 + m + SEPARATION_EPS,
        )
    need = None
    if st.channels is not None:
        from .geometry import body_shift
        from .legalize import _channel_need

        need, _short = _channel_need(st.channels, st.comps, lambda c: body_shift(c) or (0.0, 0.0))
    # The legalizer's own rules (clearance, spreading, margins, pad-edge rule, regions, reserves)
    # have the last word: a part that breaks one is held where it was and the compaction solved
    # again without it (a few times), else dropped.
    held_back = set()
    shifts, saved = {}, {}
    for _ in range(COMPACT_RETRIES):
        free = [r for r in movable if r not in held_back]
        budget = {r: COMPACT_REACH_MM for r in free}
        obstacles = sorted(set(st.comps) - set(free))
        shifts = compact(st, free, obstacles, half, budget, width, height, rounds, need)
        if not shifts:
            break
        saved = {r: _pose(st.comps[r]) for r in shifts}
        for r, (dx, dy) in sorted(shifts.items()):
            p = saved[r]
            _set(st.comps[r], (p[0] + dx, p[1] + dy, p[2]))
        bad = set()
        hard = hard_violations(out, constraints)
        if any(hard.values()):
            for a, b in hard.get("overlaps", ()):
                bad |= {a, b} & set(shifts)
            for key, refs in hard.items():
                if key != "overlaps":
                    bad |= set(refs) & set(shifts)
            bad = bad or set(shifts)
        else:
            try:
                check = pose_checker(out, constraints, **legal_args)
                bad = {r for r in shifts if not check([st.comps[r]])}
                bad |= {r for r in shifts if not st.zone_ok(r, _box(st.comps[r]))}
                if not bad and not check([st.comps[r] for r in sorted(shifts)]):
                    bad = set(shifts)
            except ValueError:
                bad = set(shifts)
        if not bad:
            break
        for r, p in saved.items():
            _set(st.comps[r], p)
        held_back |= bad
        shifts = {}
    if not shifts:
        rec["compact"] = "illegal" if held_back else "none"
        return False
    before = st.totals()
    st.accept(sorted(saved))
    after = st.totals()
    if after[0] + after[1] >= before[0] + before[1] - GAIN_MM:
        for r, p in saved.items():
            _set(st.comps[r], p)
        st.accept(sorted(saved))
        rec["compact"] = "no_gain"
        return False
    saved = {r: saved[r] for r in shifts}
    for r in sorted(shifts):
        log.append([r, "compact", list(saved[r]), list(_pose(st.comps[r])), 0.0])
    rec["compact"] = len(shifts)
    rec["moves"]["compact"] = len(shifts)
    return True


def _median(values) -> float:
    if not values:
        return 0.0
    n = len(values)
    mid = n // 2
    return values[mid] if n % 2 else (values[mid - 1] + values[mid]) / 2.0


def summary(record: dict) -> dict:
    """``record`` without its per-move log and per-part rows (the report's form)."""
    return {k: v for k, v in record.items() if k not in ("log", "parts")}


def combine(records: List[dict]) -> dict:
    """Totals over several records (blocks and top): counts, sums and the largest move;
    the median is the largest of the medians (a bound), the topology the mean and the
    neighbour orders the means weighted by their relation counts."""
    out = empty_record()
    if not records:
        return out
    for r in records:
        for k in KINDS:
            out["moves"][k] += r.get("moves", {}).get(k, 0)
        for k in (
            "movable",
            "moved",
            "sum_mm",
            "hpwl_before",
            "hpwl_after",
            "crossing_before",
            "crossing_after",
            "channel_delta",
            "wire_saved",
            "seconds",
        ):
            out[k] = round(out[k] + r.get(k, 0), 4)
        out["max_mm"] = max(out["max_mm"], r.get("max_mm", 0.0))
        out["median_mm"] = max(out["median_mm"], r.get("median_mm", 0.0))
    out["topology"] = round(sum(r.get("topology", 1.0) for r in records) / len(records), 4)
    from .motion import weighted_order

    for suffix in ("", "_global"):
        kept, total = weighted_order(
            records, "neighbour_order" + suffix, "neighbour_relations" + suffix
        )
        if total:
            out["neighbour_order" + suffix] = round(kept / total, 4)
            out["neighbour_relations" + suffix] = total
    return out
