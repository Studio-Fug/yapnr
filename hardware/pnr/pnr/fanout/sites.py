"""The fanout's resources: half-lattice nodes, track edges and via sites, judged exactly.

A copper object is a **track edge** between two neighbouring half-lattice nodes on
one layer (orthogonal or 45 degrees, half a pitch or its diagonal) or a **via** of a
class at one node (through every layer). :class:`Model` decides, on exact geometry
(:mod:`.geom`), which objects a net may use at all (*static*: lands, the part's
other copper, fixed copper, keepouts, reserved corridors, the outline, the site
kinds of the via class) and which pairs of objects of two nets cannot coexist
(*conflicts*: track to track, track to via, via to via copper clearance and drill
spacing). Conflicts depend only on the two objects' kinds and their offset on the
lattice, so they are computed once per board as **stencils**.

Every number comes from the routing rules: the fab block (clearance, SMD pad
clearance, via-to-SMD-pad, hole to hole, copper to edge, hole to edge), the net
classes (width, clearance) and the fanout's via classes. The clearance between two
fanout objects is the largest clearance any fanned-out net has (conservative).
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Dict, List, Optional, Sequence, Tuple

from .geom import Land, point_in_polygon, segment_polygon, segment_segment
from .lattice import Lattice

# Track edge directions on the half lattice: east, north, north-east, north-west.
DIRS = ((1, 0), (0, 1), (1, 1), (-1, 1))
MARGIN = 1e-6  # mm: every clearance is judged with this margin (KiCad's nm rounding)
AREA = "\0area:"  # a blocker token: an area that exempts some nets


class Obstacles:
    """Copper and areas the fanout must keep clear of, in the part's frame."""

    def __init__(self):
        self.lands: List[Land] = []  # surface lands (the part's own, every pad)
        self.tracks: List[Tuple[str, int, Tuple, Tuple, float]] = []  # net, layer, a, b, width
        self.vias: List[Tuple[str, Tuple, float, float]] = []  # net, centre, diameter, drill
        # (name, polygon, layer indices or None for every layer, applies to vias,
        # nets exempt from it)
        self.areas: List[Tuple[str, list, Optional[frozenset], bool, frozenset]] = []
        self.outline: Optional[list] = None  # board outline polygon (local frame)


class Model:
    """Nodes, objects, static legality and conflict stencils for one fanout."""

    def __init__(
        self,
        lattice: Lattice,
        layers: Sequence[str],
        obstacles: Obstacles,
        *,
        clearance: float,
        pad_clearance: float,
        via_to_pad: Optional[float],
        hole_to_hole: float,
        edge_clearance: float,
        hole_to_edge: Optional[float],
        via_classes: Sequence[Dict],
        widths: Sequence[float],
        margin: int = 2,
        in_pad=None,
    ):
        self.lat = lattice
        self.layers = list(layers)
        self.obs = obstacles
        self.clearance = clearance
        self.pad_clearance = max(pad_clearance, clearance)
        self.via_to_pad = via_to_pad
        self.hole_to_hole = hole_to_hole
        self.edge_clearance = edge_clearance
        self.hole_to_edge = hole_to_edge
        self.via_classes = list(via_classes)
        self.widths = sorted(set(round(w, 6) for w in widths))
        self.in_pad = in_pad
        a0, b0, a1, b1 = lattice.hull
        self.margin = margin
        # The exit ring sits margin + 1 half steps beyond the hull; the ring after
        # it holds only the first outward step of each exit (its extension).
        self.exit_ring = margin + 1
        self.bounds = (a0 - margin - 2, b0 - margin - 2, a1 + margin + 2, b1 + margin + 2)
        self._lands_by_bucket = self._bucket_lands()
        self._allow = {name: allow for name, _p, _l, _v, allow in obstacles.areas}
        self._edge_cache: Dict[Tuple, Optional[frozenset]] = {}
        self._via_cache: Dict[Tuple, Optional[frozenset]] = {}
        self.stencils = self._stencils()

    def legal(self, blockers: Optional[frozenset], net: str) -> bool:
        """``net`` may use an object whose blockers are ``blockers``."""
        if blockers is None:
            return False
        for b in blockers:
            if b == net:
                continue
            if b.startswith(AREA) and net in self._allow.get(b[len(AREA) :], ()):
                continue
            return False
        return True

    # ------------------------------------------------------------ nodes

    def point(self, node) -> Tuple[float, float]:
        return self.lat.point(*node)

    def in_region(self, node) -> bool:
        a0, b0, a1, b1 = self.bounds
        return a0 <= node[0] <= a1 and b0 <= node[1] <= b1

    def ring_of(self, node) -> int:
        """Half steps beyond the hull (0 inside it)."""
        a0, b0, a1, b1 = self.lat.hull
        a, b = node
        return max(a0 - a, a - a1, b0 - b, b - b1, 0)

    def outward(self, node) -> Optional[Tuple[int, int]]:
        """The outward axis of an exit-ring node (None off the ring and at corners)."""
        a0, b0, a1, b1 = self.lat.hull
        k = self.exit_ring
        a, b = node
        sides = []
        if a == a0 - k and b0 - k < b < b1 + k:
            sides.append((-1, 0))
        if a == a1 + k and b0 - k < b < b1 + k:
            sides.append((1, 0))
        if b == b0 - k and a0 - k < a < a1 + k:
            sides.append((0, -1))
        if b == b1 + k and a0 - k < a < a1 + k:
            sides.append((0, 1))
        return sides[0] if len(sides) == 1 else None

    def site_kind(self, node) -> str:
        kind = self.lat.kind(*node)
        return "outside" if kind == "outside" else kind

    # ------------------------------------------------------------ static legality

    def _bucket_lands(self):
        buckets = defaultdict(list)
        size = max(self.lat.px, self.lat.py)
        for k, land in enumerate(self.obs.lands):
            r = land.radius + 1.0
            for bx in range(
                math.floor((land.centre[0] - r) / size), math.floor((land.centre[0] + r) / size) + 1
            ):
                for by in range(
                    math.floor((land.centre[1] - r) / size),
                    math.floor((land.centre[1] + r) / size) + 1,
                ):
                    buckets[(bx, by)].append(k)
        self._bucket = size
        return buckets

    def _near_lands(self, a, b):
        size = self._bucket
        seen = set()
        for p in (a, b, ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)):
            for k in self._lands_by_bucket.get(
                (math.floor(p[0] / size), math.floor(p[1] / size)), ()
            ):
                if k not in seen:
                    seen.add(k)
                    yield self.obs.lands[k]

    def _outline_clear(self, a, b, keep) -> bool:
        poly = self.obs.outline
        if poly is None:
            return True
        if not (point_in_polygon(a, poly) and point_in_polygon(b, poly)):
            return False
        n = len(poly)
        return all(
            segment_segment(a, b, poly[k], poly[(k + 1) % n]) >= keep - MARGIN for k in range(n)
        )

    def edge_blockers(self, layer: int, node, d: int, width: float) -> Optional[frozenset]:
        """Nets whose copper a track edge of ``width`` from ``node`` in direction
        ``DIRS[d]`` on ``layer`` would violate (empty: free for every net), or None
        when nothing may use it (an area, the outline, a no-net land)."""
        key = (layer, node, d, width)
        if key in self._edge_cache:
            return self._edge_cache[key]
        other = (node[0] + DIRS[d][0], node[1] + DIRS[d][1])
        result = self._edge_blockers(layer, self.point(node), self.point(other), width)
        self._edge_cache[key] = result
        return result

    def segment_blockers(self, layer, a, b, width) -> Optional[frozenset]:
        """As :meth:`edge_blockers` for any segment (a stub from a ball's exact centre)."""
        return self._edge_blockers(layer, a, b, width)

    def _edge_blockers(self, layer, a, b, width):
        nets = set()
        half = width / 2
        if layer == 0:
            for land in self._near_lands(a, b):
                if land.distance(a, b) < half + self.pad_clearance - MARGIN:
                    if not land.net:
                        return None
                    nets.add(land.net)
        for net, la, p, q, w in self.obs.tracks:
            if la == layer and segment_segment(a, b, p, q) < half + w / 2 + self.clearance - MARGIN:
                if not net:
                    return None
                nets.add(net)
        for net, c, dia, _drill in self.obs.vias:
            if segment_segment(a, b, c, c) < half + dia / 2 + self.clearance - MARGIN:
                if not net:
                    return None
                nets.add(net)
        for name, poly, layers, _vias, allow in self.obs.areas:
            if (layers is None or layer in layers) and segment_polygon(a, b, poly) < half + MARGIN:
                if not allow:
                    return None
                nets.add(AREA + name)
        if not self._outline_clear(a, b, half + self.edge_clearance):
            return None
        return frozenset(nets)

    def via_blockers(self, node, k: int, net: str, at=None) -> Optional[frozenset]:
        """Nets a via of class ``k`` at ``node`` would violate, or None when no net may
        place it (wrong site kind, an area, the outline, a no-net land, too close to
        ``net``'s own land: the via-to-SMD-pad keep-away). ``at`` overrides the
        position (a ball's exact centre for an in-pad via)."""
        key = (node, k, net)
        if key in self._via_cache:
            return self._via_cache[key]
        result = self._via_blockers(node, k, net, at)
        self._via_cache[key] = result
        return result

    def _via_blockers(self, node, k, net, at):
        cls = self.via_classes[k]
        kind = self.site_kind(node)
        if kind == "ball":
            if "in_pad" not in cls["sites"] or self.in_pad is None:
                return None
        elif kind not in cls["sites"] or kind == "channel":
            return None
        if self.ring_of(node) >= self.exit_ring:
            return None  # the exit ring and beyond carry exits only
        p = at or self.point(node)
        d, h = cls["diameter_mm"], cls["drill_mm"]
        r = d / 2
        nets = set()
        keep_own = self.via_to_pad if self.via_to_pad is not None else self.clearance
        keep_foreign = max(self.pad_clearance, self.via_to_pad or 0.0)
        for land in self._near_lands(p, p):
            dist = land.distance(p, p)
            if land.net and land.net == net:
                if kind == "ball":
                    if not self._in_pad_fits(land, p, d, h):
                        return None
                elif dist < r + keep_own - MARGIN:
                    return None
                continue
            if dist < r + keep_foreign - MARGIN:
                if not land.net:
                    return None
                nets.add(land.net)
        for other, _la, a, b, w in self.obs.tracks:
            if segment_segment(p, p, a, b) < r + w / 2 + self.clearance - MARGIN:
                if not other:
                    return None
                nets.add(other)
        for other, c, dia, drill in self.obs.vias:
            gap = math.dist(p, c)
            if gap < (h + drill) / 2 + self.hole_to_hole - MARGIN and gap > 1e-6:
                return None  # drills too close, whatever the nets
            if gap < r + dia / 2 + self.clearance - MARGIN:
                if not other:
                    return None
                nets.add(other)
        for name, poly, _layers, vias, allow in self.obs.areas:
            if vias and segment_polygon(p, p, poly) < r + MARGIN:
                if not allow:
                    return None
                nets.add(AREA + name)
        keep = r + self.edge_clearance
        if self.hole_to_edge is not None:
            keep = max(keep, h / 2 + self.hole_to_edge)
        if not self._outline_clear(p, p, keep):
            return None
        return frozenset(nets)

    def _in_pad_fits(self, land, p, d, h) -> bool:
        from pnr.fab_profile import in_pad_fit

        if self.in_pad is None or not self.in_pad.allows(d, h):
            return False
        offset = (p[0] - land.centre[0], p[1] - land.centre[1])
        return in_pad_fit(self.in_pad, (2 * land.hw, 2 * land.hh), offset, d, h, land.corner)

    # ------------------------------------------------------------ conflicts

    def edge_type(self, d: int, width: float) -> Tuple:
        return ("e", d, self.widths.index(round(width, 6)))

    def via_type(self, k: int) -> Tuple:
        return ("v", k)

    def _types(self):
        return [("e", d, w) for d in range(4) for w in range(len(self.widths))] + [
            ("v", k) for k in range(len(self.via_classes))
        ]

    def _shape(self, t, origin=(0, 0)):
        """(segment start, end, radius, drill or None) of an object of type ``t`` at
        half-lattice ``origin`` (its position relative to the stencil's origin)."""
        hx, hy = self.lat.px / 2, self.lat.py / 2
        p = (origin[0] * hx, origin[1] * hy)
        if t[0] == "e":
            dx, dy = DIRS[t[1]]
            return p, (p[0] + dx * hx, p[1] + dy * hy), self.widths[t[2]] / 2, None
        cls = self.via_classes[t[1]]
        return p, p, cls["diameter_mm"] / 2, cls["drill_mm"]

    def _stencils(self):
        """``{(type a, type b): (other-net offsets, same-net offsets)}``: the offsets
        ``(da, db)`` at which an object of type b conflicts with one of type a at the
        origin. Same-net conflicts are drill spacing only (two vias of one net)."""
        reach = 2 + int(
            math.ceil(
                (
                    max([w for w in self.widths] + [c["diameter_mm"] for c in self.via_classes])
                    + self.clearance
                    + self.hole_to_hole
                )
                / (min(self.lat.px, self.lat.py) / 2)
            )
        )
        out = {}
        types = self._types()
        for ta in types:
            a0, a1, ra, ha = self._shape(ta)
            for tb in types:
                other, same = [], []
                for da in range(-reach, reach + 1):
                    for db in range(-reach, reach + 1):
                        b0, b1, rb, hb = self._shape(tb, (da, db))
                        gap = segment_segment(a0, a1, b0, b1)
                        if gap < ra + rb + self.clearance - MARGIN:
                            other.append((da, db))
                        if ha is not None and hb is not None and (da, db) != (0, 0):
                            if gap < (ha + hb) / 2 + self.hole_to_hole - MARGIN:
                                other.append((da, db)) if (da, db) not in other else None
                                same.append((da, db))
                out[(ta, tb)] = (tuple(sorted(set(other))), tuple(sorted(same)))
        return out
