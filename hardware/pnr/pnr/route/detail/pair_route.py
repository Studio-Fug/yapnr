"""Coupled differential pairs inside :func:`pnr.route.detail.router.route_board`.

``route_pairs: coupled`` (``board.route_pairs``, opt-in) routes every declared
``diff_pair`` whose two nets each join exactly two terminals as ONE coupled pair
before the escape planner and the maze, instead of two independent legs that the
length tuner then matches:

* **Terminals.** At a ball a declared fanout (:mod:`.fanout`) escaped, the end of
  its escape copper (the exit, on the escape's last layer); the escape's planar
  length is uncoupled copper the pair already has. At any other pad, the pad centre
  on its own layer (any grid layer for a through-hole pad). The two ends of the pair
  are the terminals the two nets share a part on (else the nearest).
* **Layers.** The pair runs on one layer from its terminals: one both ends reach,
  the pair's ``layers`` (``diff_pair.layers``) and its nets' layer masks (the
  stack's current rating, ``dru_routing``'s ``disallow track``) allow, in stack
  order. A pair whose ends share no allowed layer is not routed coupled.
* **Search.** :func:`.coupled.solve_pair` (the native loop's coupled solver): a
  clearance envelope of width ``2 width + gap`` is routed and both legs are offset
  from it, with bounded pad-to-lane fanouts. The obstacles are exactly those the
  escape planner judges (:func:`.joint_escape._segment_clear`'s semantics: the
  blocked mask with keepouts, edge and rule areas, fixed-block copper, other nets'
  pads with their class clearances and pad keep-aways, the declared fanouts' escape
  copper and vias) plus the protected access cells of the fanouts and every pair
  routed before, bucketed for speed.
* **Uncoupled length.** The pair's ``max_uncoupled_mm`` (default
  :data:`DEFAULT_MAX_UNCOUPLED_MM`) bounds each leg's total uncoupled copper: its
  escape leads plus every uncoupled run of its routed path
  (:func:`.coupled.uncoupled_runs`, measured exactly on the accepted geometry).
* **Skew.** The shorter leg gets the solver's 45-degree trombone so the legs match
  to :data:`SKEW_SHARE` of the pair's budget (``skew_mm``, or ``skew_ps`` at the
  layer's delay), then both legs are measured as KiCad does
  (:func:`pnr.length_model.net_length`, escapes and pads included) and re-tuned
  once against that measure. A pair still over its budget is not routed coupled.
* **Groups.** A ``length_match`` group whose members are all legs of coupled
  pairs is tuned here: each pair shorter than the group's longest member gets
  coupled bumps (a 45-degree trombone in the pair's centre line, both legs offset
  from it, so both legs gain the same length and stay coupled) until the group is
  within :data:`GROUP_SHARE` of its tolerance. Groups with other members are left
  to the length tuner, which keeps the coupled legs as they are.
* **Commit.** The legs are committed as escape copper: their cells are owned by
  their nets (:func:`.fixed.reserve_fixed_copper`, ``own_net``) and their segments
  join ``grid.escape_segments``, so every later escape, drop and maze route keeps
  clear of them; the nets leave the maze and are emitted with their fanout escapes.
* **Fallback.** A pair that is not routed coupled (no common layer, no channel, an
  uncoupled or skew budget it cannot meet, a terminal count other than two per
  net, a fixed-block port) is routed as two legs, exactly as without the switch,
  and the report says why.

The report is ``escape_diagnostics["coupled_pairs"]``: per pair ``status``
(``coupled`` or ``legs``), ``reason``, ``layer``, the KiCad-model ``lengths_mm``,
``skew_mm``, ``uncoupled_mm`` per leg, the solver's ``attempts`` and ``failures``;
per group ``status`` (``ok``, ``tuned``, ``length_unmatched``), ``spread_mm`` and the
bumps added. Nothing here runs without ``route_pairs: coupled``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from pnr import length_model as lm
from pnr.place.geometry import pad_rects
from pnr.writeback import _segment_distance_sq

from . import coupled
from .grid import far_twins, pad_layer

# A pair's default bound on each leg's uncoupled copper (mm), solve_pair's own.
DEFAULT_MAX_UNCOUPLED_MM = 2.0
# The solver matches a pair's legs to this share of its skew budget; the KiCad
# measure afterwards must be within the whole budget.
SKEW_SHARE = 0.5
# A coupled group is tuned to this share of its tolerance.
GROUP_SHARE = 0.5
# Coupled bumps: the plateau in pair pitches (width + gap); the largest bump
# height in pair pitches; at most this many bumps per pair.
BUMP_PLATEAU_PITCHES = 2.0
BUMP_MAX_HEIGHT_PITCHES = 6.0
BUMP_LIMIT = 24
# Maze effort of one solve (solve_pair's own bounds).
MAX_EXPANSIONS = 20000
MAX_ATTEMPTS = 32
BUCKET_MM = 1.0
# Another net's fanout exit (its protected access cell) keeps a corridor this long
# in its escape's outward direction, and a via site at its end, free of pair copper.
EXIT_CORRIDOR_MM = 1.0


def enabled(rules: Optional[dict]) -> bool:
    """Whether the board routes its pairs coupled (``board.route_pairs: coupled``)."""
    return (rules or {}).get("route_pairs") == "coupled"


def pair_layer_masks(grid, rules: Optional[dict]) -> Dict[str, set]:
    """net -> the grid layer indices a declared ``diff_pair.layers`` allows its two
    nets (both legs, routed coupled or not). Empty when no pair declares layers."""
    out: Dict[str, set] = {}
    for dp in (rules or {}).get("diff_pairs") or []:
        names = dp.get("layers")
        if not names:
            continue
        allowed = {grid.layers.index(n) for n in names if n in grid.layers}
        for net in (dp["p"], dp["n"]):
            out[net] = out[net] & allowed if net in out else set(allowed)
    return out


# ------------------------------------------------------------------ obstacles


class Obstacles:
    """The static copper the escape planner judges, bucketed (module doc)."""

    def __init__(self, grid, fanouts=None):
        self.grid = grid
        self.classes = getattr(grid, "net_clearances", None) or {}
        self.keepaways = getattr(grid, "pad_keepaways", None) or {}
        self.protected = dict(getattr(fanouts, "protected", None) or {})
        self.pads: Dict[tuple, list] = {}
        self.segments: Dict[tuple, list] = {}
        self.vias: Dict[tuple, list] = {}
        self.cache: Dict[tuple, bool] = {}
        widths: Dict[str, float] = {}
        for esc in getattr(fanouts, "escapes", None) or []:
            for w in [esc.width or 0.0] + list(esc.widths or []):
                widths[esc.net] = max(widths.get(esc.net, 0.0), w)
        self.escape_width = widths
        sizes = getattr(fanouts, "via_sizes", None) or {}
        for la, owner, r in grid.pad_rectangles:
            keep = self.keepaways.get((la, owner, r))
            item = (owner, r, keep)
            for key in self._keys(la, r.left, r.bottom, r.right, r.top):
                self.pads.setdefault(key, []).append(item)
        for la, owner, a, b in grid.escape_segments:
            self.add_segment(owner, la, a, b, self._width(owner))
        for owner, p in grid.escape_vias:
            size = sizes.get((owner, p[0], p[1]))
            radius = size[0] / 2 if size else grid.via_radius
            for key in self._keys(None, p[0], p[1], p[0], p[1]):
                self.vias.setdefault(key, []).append((owner, tuple(p), radius))
        # A fanout's protected access cell is where its net's maze route starts: a
        # track of that net must fit at its centre and run on outward, in its
        # escape's direction, for EXIT_CORRIDOR_MM (route_pairs keeps the cell its
        # net's when the pair's copper is reserved), so a pair never seals an exit.
        heading = {}
        for esc in getattr(fanouts, "escapes", None) or []:
            if esc.fanout and esc.kind != "blocked" and esc.segments:
                end = esc.segments[-1][2]
                d = math.dist(esc.pad_xy, end)
                if d > 1e-9:
                    key = (esc.access.layer, esc.access.i, esc.access.j)
                    heading[key] = ((end[0] - esc.pad_xy[0]) / d, (end[1] - esc.pad_xy[1]) / d)
        # (owner, layer, cells, centres) per exit; their obstacles carry a room
        # level (1: the corridor's track, 2: its end via) that ``room`` switches.
        self.corridors = []
        for key, owner in sorted(self.protected.items()):
            la, i, j = key
            u = heading.get(key, (0.0, 0.0))
            cells = [(i, j)]
            for k in range(1, int(EXIT_CORRIDOR_MM / grid.pitch + 1e-9) + 1):
                x, y = grid.center_of(i, j)
                c = grid.cell_of(x + u[0] * k * grid.pitch, y + u[1] * k * grid.pitch)
                if grid.in_bounds(*c) and c != cells[-1]:
                    cells.append(c)
            centres = [grid.center_of(*c) for c in cells]
            for a, b in zip(centres, centres[1:] or centres):
                self.add_segment(owner, la, a, b, self._width(owner), room=1)
            # ... and may drop a via at its end.
            self.add_segment(owner, la, centres[-1], centres[-1], 2 * grid.via_radius, room=2)
            self.corridors.append((owner, la, cells, centres))
        self.room = 2

    def _width(self, owner):
        return max(
            self.grid.net_widths.get(owner, self.grid.track_width),
            self.escape_width.get(owner, 0.0),
        )

    @staticmethod
    def _keys(layer, x0, y0, x1, y1):
        for i in range(math.floor(x0 / BUCKET_MM), math.floor(x1 / BUCKET_MM) + 1):
            for j in range(math.floor(y0 / BUCKET_MM), math.floor(y1 / BUCKET_MM) + 1):
                yield (layer, i, j)

    def add_segment(self, owner, layer, a, b, width, room=0):
        item = (owner, tuple(a), tuple(b), width, room)
        box = (min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]))
        for key in self._keys(layer, *box):
            self.segments.setdefault(key, []).append(item)
        self.cache.clear()

    def remove_net(self, nets):
        """Forget the segments of ``nets`` (a pair whose legs are rebuilt)."""
        for key, items in self.segments.items():
            self.segments[key] = [item for item in items if item[0] not in nets]
        self.cache.clear()

    def clear(self, net, layer, a, b, width, ignore=()):
        """Segment ``a``-``b`` of ``width`` for ``net`` on grid layer ``layer``
        clears every obstacle of a net other than ``net`` and ``ignore``."""
        key = (
            net,
            layer,
            round(a[0], 6),
            round(a[1], 6),
            round(b[0], 6),
            round(b[1], 6),
            round(width, 6),
            tuple(sorted(ignore)),
        )
        hit = self.cache.get(key)
        if hit is None:
            hit = self._clear(net, layer, tuple(a), tuple(b), width, set(ignore) | {net})
            self.cache[key] = hit
        return hit

    def _reach(self, net, owner, width):
        return width / 2 + max(
            self.grid.clearance, self.classes.get(net, 0.0), self.classes.get(owner, 0.0)
        )

    def _clear(self, net, layer, a, b, width, skip):
        grid = self.grid
        radius = width / 2 + max(grid.clearance, self.classes.get(net, 0.0))
        # The blocked mask (keepouts, edge, rule areas, retained copper; grown by
        # half a track), the keepouts' allow lists, fixed-block cells and the
        # fanouts' protected access cells, sampled as joint_escape does.
        steps = max(1, math.ceil(math.dist(a, b) / (grid.pitch / 4)))
        keepouts = getattr(grid, "net_keepouts", None)
        owned = getattr(grid, "fixed_owned", None)
        for step in range(steps + 1):
            x = a[0] + (b[0] - a[0]) * step / steps
            y = a[1] + (b[1] - a[1]) * step / steps
            for dx, dy in ((0, 0), (radius, 0), (-radius, 0), (0, radius), (0, -radius)):
                if not (0 <= x + dx <= grid.width and 0 <= y + dy <= grid.height):
                    return False
                i, j = grid.cell_of(x + dx, y + dy)
                if grid.blocked[layer, j, i]:
                    return False
                if dx or dy:
                    continue
                if keepouts and grid.net_blocked(net, layer, i, j):
                    return False
                if owned:
                    holder = owned.get((layer, i, j))
                    if holder is not None and holder not in skip:
                        return False
                holder = self.protected.get((layer, i, j))
                if holder is not None and holder not in skip:
                    return False
        grow = radius + 1.0
        box = (
            min(a[0], b[0]) - grow,
            min(a[1], b[1]) - grow,
            max(a[0], b[0]) + grow,
            max(a[1], b[1]) + grow,
        )
        seen = set()
        for key in self._keys(layer, *box):
            for item in self.pads.get(key, ()):
                if id(item) in seen:
                    continue
                seen.add(id(item))
                owner, r, keep = item
                if owner in skip:
                    continue
                reach = self._reach(net, owner, width)
                if keep is not None:
                    reach = max(reach, width / 2 + keep)
                if (
                    max(a[0], b[0]) + reach < r.left
                    or min(a[0], b[0]) - reach > r.right
                    or max(a[1], b[1]) + reach < r.bottom
                    or min(a[1], b[1]) - reach > r.top
                ):
                    continue
                if any(r.left <= p[0] <= r.right and r.bottom <= p[1] <= r.top for p in (a, b)):
                    return False
                corners = [
                    (r.left, r.bottom),
                    (r.right, r.bottom),
                    (r.right, r.top),
                    (r.left, r.top),
                ]
                if any(
                    _segment_distance_sq(a, b, corners[k], corners[(k + 1) % 4]) < reach**2 - 1e-10
                    for k in range(4)
                ):
                    return False
            for item in self.segments.get(key, ()):
                if id(item) in seen:
                    continue
                seen.add(id(item))
                owner, c, d, other, room = item
                if owner in skip or room > self.room:
                    continue
                limit = (other / 2 + self._reach(net, owner, width)) ** 2 - 1e-10
                if _segment_distance_sq(a, b, c, d) < limit:
                    return False
        for key in self._keys(None, *box):
            for item in self.vias.get(key, ()):
                if id(item) in seen:
                    continue
                seen.add(id(item))
                owner, p, via_radius = item
                if owner in skip:
                    continue
                limit = (via_radius + self._reach(net, owner, width)) ** 2 - 1e-10
                if _segment_distance_sq(a, b, p, p) < limit:
                    return False
        return True


# ------------------------------------------------------------------ terminals


@dataclass
class Terminal:
    net: str
    ref: str
    pad: str
    point: Tuple[float, float]
    layers: Tuple[str, ...]  # grid layer names the pair may leave it on
    lead_mm: float = 0.0  # planar escape copper before it (uncoupled)
    escape: Optional[object] = None  # the fanout Escape ending here
    tracks: List[tuple] = field(default_factory=list)  # (layer, a, b, width) of the escape
    vias: List[tuple] = field(default_factory=list)  # (x, y, radius) of the escape


def _terminals(grid, graph, net, escapes, via_sizes):
    """The net's terminals (module doc), or a reason string."""
    try:
        pins = graph.net(net).pins
    except KeyError:
        return "no_net"
    out = []
    for ref, name in pins:
        comp = graph.component(ref)
        twins = far_twins(comp)
        for k, ((pad_name, pad_net, r), pad) in enumerate(zip(pad_rects(comp), comp.pads)):
            if pad_name != name or pad_net != net or k in twins:
                continue
            esc = escapes.get((net, round(r.cx, 6), round(r.cy, 6)))
            if esc is not None:
                if esc.kind == "blocked" or not esc.segments:
                    return "fanout_without_exit"
                tracks = []
                for index, (layer, a, b) in enumerate(esc.segments):
                    w = esc.widths[index] if esc.widths else esc.width
                    tracks.append((layer, tuple(a), tuple(b), w or grid.track_width))
                layer, _a, end = esc.segments[-1]
                vias = []
                if esc.via_xy is not None:
                    size = via_sizes.get((net, esc.via_xy[0], esc.via_xy[1]))
                    radius = size[0] / 2 if size else grid.via_radius
                    vias.append((esc.via_xy[0], esc.via_xy[1], radius))
                lead = sum(math.dist(a, b) for _la, a, b in esc.segments)
                out.append(Terminal(net, ref, name, tuple(end), (layer,), lead, esc, tracks, vias))
            elif pad.through_hole:
                out.append(Terminal(net, ref, name, (r.cx, r.cy), tuple(grid.layers)))
            else:
                la = grid.layers[pad_layer(grid, comp, pad)]
                out.append(Terminal(net, ref, name, (r.cx, r.cy), (la,)))
            break
    if len(out) != 2:
        return "terminals_%d" % len(out)
    return out


def _ends(tp, tn):
    """((p source, n source), (p target, n target)): the terminals sharing a part,
    else the nearer assignment; a fanned-out end is the source."""

    def score(a, b):
        return (
            -((tp[a].ref == tn[b].ref) + (tp[1 - a].ref == tn[1 - b].ref)),
            math.dist(tp[a].point, tn[b].point) + math.dist(tp[1 - a].point, tn[1 - b].point),
        )

    a, b = min(((0, 0), (0, 1)), key=lambda ab: score(*ab))
    ends = [(tp[a], tn[b]), (tp[1 - a], tn[1 - b])]
    if ends[1][0].escape is not None and ends[0][0].escape is None:
        ends.reverse()
    return ends


# ------------------------------------------------------------------ one pair


@dataclass
class Solved:
    pair: dict
    layer: str
    width: float
    gap: float
    paths: Dict[str, list]
    centerline: list
    ends: list
    lengths: Dict[str, float]
    uncoupled: Dict[str, float]
    budget: float
    max_uncoupled: float
    attempts: int = 0
    bumps: int = 0
    room: int = 2  # the exit room other nets' corridors kept (Obstacles.room)

    def tracks(self):
        for net in (self.pair["p"], self.pair["n"]):
            path = self.paths[net]
            for a, b in zip(path, path[1:]):
                if math.dist(a, b) >= 1e-6:
                    yield (net, self.layer, tuple(a), tuple(b), self.width)


class PairRouter:
    def __init__(self, grid, graph, rules, fanouts, net_width, track_width):
        self.grid = grid
        self.graph = graph
        self.rules = rules
        self.net_width = net_width
        self.track_width = track_width
        self.obstacles = Obstacles(grid, fanouts)
        self.escapes = {}
        for esc in getattr(fanouts, "escapes", None) or []:
            if esc.fanout and esc.kind != "blocked":
                self.escapes[(esc.net, round(esc.pad_xy[0], 6), round(esc.pad_xy[1], 6))] = esc
        self.via_sizes = dict(getattr(fanouts, "via_sizes", None) or {})
        layers = int(rules.get("layers", 2) or 2)
        self.st = rules.get("stackup") or lm.default_stackup(layers)
        self.delay = None
        self.pads = lm.graph_pads(
            graph,
            [n for dp in rules.get("diff_pairs") or [] for n in (dp["p"], dp["n"])],
            lands=rules.get("pad_lands"),
        )
        outline = getattr(graph, "outline", None)
        self.frame = lm.board_frame(outline.height) if outline is not None else None
        self.via_radius = grid.via_radius

    # -- measures --------------------------------------------------------------

    def measure(self, net, path, layer, width, ends):
        """KiCad-model length of ``net``: its escapes, then ``path``."""
        tracks, vias = [], []
        for end in ends:
            term = end[0] if end[0].net == net else end[1]
            tracks.extend(term.tracks)
            vias.extend(term.vias)
        tracks += [(layer, tuple(a), tuple(b), width) for a, b in zip(path, path[1:])]
        return lm.net_length(
            net, tracks, vias, self.pads, self.st, None, self.via_radius, self.frame
        ).total_mm

    def budget_mm(self, pair, layer, width):
        if pair.get("skew_ps") is not None:
            if self.delay is None:
                self.delay = lm.DelayModel(self.st)
            return float(pair["skew_ps"]) / self.delay.track(layer, width)
        return float(pair.get("skew_mm", 0.5))

    def uncoupled(self, paths, layer, width, gap, leads):
        steps = {
            net: [(layer, a, b) for a, b in zip(path, path[1:])] for net, path in paths.items()
        }
        runs = coupled.uncoupled_runs(steps, width, gap)
        return {net: sum(r["length_mm"] for r in runs[net]["runs"]) + leads[net] for net in paths}

    # -- solve -----------------------------------------------------------------

    def width_gap(self, pair):
        p, n = pair["p"], pair["n"]
        width = pair.get("width_mm") or max(
            self.net_width.get(p, self.track_width), self.net_width.get(n, self.track_width)
        )
        classes = self.obstacles.classes
        gap = pair.get("gap_mm") or max(
            self.grid.clearance, classes.get(p, 0.0), classes.get(n, 0.0)
        )
        return float(width), float(gap)

    def allowed_layers(self, pair):
        names = list(self.grid.layers)
        if pair.get("layers"):
            names = [n for n in names if n in pair["layers"]]
        mask = getattr(self.grid, "layer_mask", None) or {}
        for net in (pair["p"], pair["n"]):
            if mask.get(net) is not None:
                names = [n for n in names if self.grid.layers.index(n) in mask[net]]
        return names

    def solve(self, pair):
        """A :class:`Solved` pair, or ``(reason, details)``."""
        p, n = pair["p"], pair["n"]
        terms = {}
        for net in (p, n):
            t = _terminals(self.grid, self.graph, net, self.escapes, self.via_sizes)
            if isinstance(t, str):
                return t, {}
            terms[net] = t
        ends = _ends(terms[p], terms[n])
        common = set(self.allowed_layers(pair))
        for end in ends:
            for term in end:
                common &= set(term.layers)
        layers = [name for name in self.grid.layers if name in common]
        if not layers:
            return "no_common_layer", {}
        width, gap = self.width_gap(pair)
        cap = float(pair.get("max_uncoupled_mm") or DEFAULT_MAX_UNCOUPLED_MM)
        lead = {
            p: ends[0][0].lead_mm + ends[1][0].lead_mm,
            n: ends[0][1].lead_mm + ends[1][1].lead_mm,
        }
        remaining = cap - max(lead.values())
        if remaining <= 1e-6:
            return "uncoupled_budget", dict(leads_mm=lead, max_uncoupled_mm=cap)
        failures: Dict[str, int] = {}
        attempts = 0
        terminals = {
            p: (ends[0][0].point, ends[1][0].point),
            n: (ends[0][1].point, ends[1][1].point),
        }
        heavy = p if self.obstacles.classes.get(p, 0.0) >= self.obstacles.classes.get(n, 0.0) else n
        # Exits of other nets keep their corridor and its end via where the pair
        # can leave them that room, else their corridor, else only their access.
        for room in (2, 1, 0):
            self.obstacles.room = room
            self.obstacles.cache.clear()
            for layer in layers:
                la = self.grid.layers.index(layer)
                budget = self.budget_mm(pair, layer, width)

                def clear(net, x, y, w, la=la):
                    return self.obstacles.clear(net, la, x, y, w)

                def envelope(x, y, w, la=la):
                    return self.obstacles.clear(heavy, la, x, y, w, ignore=(p, n))

                def accept(paths, layer=layer):
                    unc = self.uncoupled(paths, layer, width, gap, lead)
                    return all(v <= cap + 1e-6 for v in unc.values())

                rr = coupled.solve_pair(
                    p,
                    n,
                    terminals,
                    (0.0, 0.0, self.grid.width, self.grid.height),
                    clear,
                    envelope,
                    width,
                    gap,
                    budget * SKEW_SHARE,
                    pitch=self.grid.pitch,
                    max_expansions=MAX_EXPANSIONS,
                    max_uncoupled=remaining,
                    max_attempts=MAX_ATTEMPTS,
                    offsets=lead,
                    accept_paths=accept,
                    max_tuning_length=remaining,
                )
                attempts += rr.get("attempts", 0)
                for k, v in (rr.get("failures") or {}).items():
                    failures[k] = failures.get(k, 0) + v
                if rr["status"] != "routed":
                    continue
                paths = rr["paths"]
                lengths = {net: self.measure(net, paths[net], layer, width, ends) for net in (p, n)}
                if abs(lengths[p] - lengths[n]) > budget * SKEW_SHARE + 1e-9:
                    # The solver matched planar lengths; match KiCad's measure once.
                    offsets = {net: lengths[net] - coupled.length(paths[net]) for net in (p, n)}
                    tuned = coupled.tune(
                        {net: list(paths[net]) for net in (p, n)},
                        width,
                        gap,
                        budget * SKEW_SHARE,
                        lambda net, x, y, w: clear(net, x, y, w),
                        offsets,
                        max_tuning_length=remaining,
                    )
                    if tuned and accept(tuned):
                        paths = tuned
                        lengths = {
                            net: self.measure(net, paths[net], layer, width, ends) for net in (p, n)
                        }
                if abs(lengths[p] - lengths[n]) > budget + 1e-9:
                    failures["kicad_skew"] = failures.get("kicad_skew", 0) + 1
                    continue
                return Solved(
                    pair,
                    layer,
                    width,
                    gap,
                    {net: [tuple(q) for q in paths[net]] for net in (p, n)},
                    [tuple(q) for q in rr["centerline"]],
                    ends,
                    lengths,
                    self.uncoupled(paths, layer, width, gap, lead),
                    budget,
                    cap,
                    attempts,
                    room=room,
                )
        self.obstacles.room = 2
        self.obstacles.cache.clear()
        return "no_coupled_channel", dict(attempts=attempts, failures=failures)

    def commit(self, solved):
        la = self.grid.layers.index(solved.layer)
        for net, _layer, a, b, w in solved.tracks():
            self.obstacles.add_segment(net, la, a, b, w)

    # -- coupled group tuning ---------------------------------------------------

    def bump(self, solved, need):
        """Lengthen both legs of ``solved`` by up to ``need`` mm with one coupled
        bump; returns the length added (0: no room)."""
        p, n = solved.pair["p"], solved.pair["n"]
        width, gap = solved.width, solved.gap
        pitch = width + gap
        o = pitch / 2
        plateau = BUMP_PLATEAU_PITCHES * pitch
        h_min = 0.5 * o / math.sqrt(2) + 2 * o * math.tan(math.pi / 8)
        h_need = need / (2 * (math.sqrt(2) - 1))
        heights = []
        h = min(h_need, BUMP_MAX_HEIGHT_PITCHES * pitch)
        while h >= h_min and len(heights) < 6:
            heights.append(h)
            h *= 0.6
        if not heights:
            return 0.0
        la = self.grid.layers.index(solved.layer)
        self.obstacles.remove_net({p, n})
        self.obstacles.room = solved.room
        self.obstacles.cache.clear()

        def clear(net, x, y, w):
            return self.obstacles.clear(net, la, x, y, w)

        centre = solved.centerline
        order = sorted(range(len(centre) - 1), key=lambda k: -math.dist(centre[k], centre[k + 1]))
        try:
            for h in heights:
                span = 2 * h + plateau
                for k in order:
                    c0, c1 = centre[k], centre[k + 1]
                    d = math.dist(c0, c1)
                    lead = pitch
                    if d < span + 2 * lead + 2 * pitch:
                        continue
                    u = ((c1[0] - c0[0]) / d, (c1[1] - c0[1]) / d)
                    room = d - span - 2 * lead
                    for frac in (0.5, 0.25, 0.75, 0.1, 0.9):
                        s = lead + room * frac
                        for side in (1, -1):
                            v = (-u[1] * side, u[0] * side)
                            local = [
                                (c0[0] + x * u[0] + y * v[0], c0[1] + x * u[1] + y * v[1])
                                for x, y in (
                                    (s - lead, 0),
                                    (s, 0),
                                    (s + h, h),
                                    (s + h + plateau, h),
                                    (s + 2 * h + plateau, 0),
                                    (s + 2 * h + plateau + lead, 0),
                                )
                            ]
                            new = self._splice(solved, k, local)
                            if new is None:
                                continue
                            if not coupled.geometry_ok(new, width, gap, clear):
                                continue
                            leads = {
                                p: solved.uncoupled[p] - self._path_uncoupled(solved, p),
                                n: solved.uncoupled[n] - self._path_uncoupled(solved, n),
                            }
                            unc = self.uncoupled(new, solved.layer, width, gap, leads)
                            if any(v > solved.max_uncoupled + 1e-6 for v in unc.values()):
                                continue
                            lengths = {
                                net: self.measure(net, new[net], solved.layer, width, solved.ends)
                                for net in (p, n)
                            }
                            if abs(lengths[p] - lengths[n]) > solved.budget + 1e-9:
                                continue
                            added = min(lengths.values()) - min(solved.lengths.values())
                            if added <= 1e-6:
                                continue
                            solved.paths = new
                            solved.centerline = centre[: k + 1] + local[1:-1] + centre[k + 1 :]
                            solved.lengths = lengths
                            solved.uncoupled = unc
                            solved.bumps += 1
                            return added
            return 0.0
        finally:
            self.obstacles.room = 2
            self.obstacles.cache.clear()
            self.commit(solved)

    def _path_uncoupled(self, solved, net):
        steps = {
            m: [(solved.layer, a, b) for a, b in zip(path, path[1:])]
            for m, path in solved.paths.items()
        }
        runs = coupled.uncoupled_runs(steps, solved.width, solved.gap)
        return sum(r["length_mm"] for r in runs[net]["runs"])

    @staticmethod
    def _splice(solved, k, local):
        """Both legs with the offset of ``local`` (a bump on centre-line segment
        ``k``) in place of the straight lane piece it replaces, or None."""
        c0, c1 = solved.centerline[k], solved.centerline[k + 1]
        d = math.dist(c0, c1)
        u = ((c1[0] - c0[0]) / d, (c1[1] - c0[1]) / d)
        out = {}
        for net, path in solved.paths.items():
            # The leg's side of the centre line, from its points beside segment k.
            side = None
            for q in path:
                t = (q[0] - c0[0]) * u[0] + (q[1] - c0[1]) * u[1]
                if -1e-6 <= t <= d + 1e-6:
                    cross = (q[1] - c0[1]) * u[0] - (q[0] - c0[0]) * u[1]
                    if abs(abs(cross) - (solved.width + solved.gap) / 2) < 1e-6:
                        side = 1 if cross > 0 else -1
                        break
            if side is None:
                return None
            try:
                lane = coupled.offset_path(local, side * (solved.width + solved.gap) / 2)
            except ValueError:
                return None
            start, end = lane[0], lane[-1]
            for i, (a, b) in enumerate(zip(path, path[1:])):
                if _on_segment(start, a, b) and _on_segment(end, a, b):
                    if math.dist(a, start) < math.dist(a, end):
                        out[net] = coupled.simplify(path[: i + 1] + lane + path[i + 1 :])
                        break
            else:
                return None
        return out

    def tune_groups(self, solved_by_net):
        reports = []
        for group in self.rules.get("length_match") or []:
            nets = list(dict.fromkeys(group.get("nets") or []))
            if len(nets) < 2 or not all(net in solved_by_net for net in nets):
                continue
            pairs = []
            for net in nets:
                s = solved_by_net[net]
                if s not in pairs:
                    pairs.append(s)
            if any(not {s.pair["p"], s.pair["n"]} <= set(nets) for s in pairs):
                continue
            if group.get("tolerance_ps") is not None:
                layer = pairs[0].layer
                if self.delay is None:
                    self.delay = lm.DelayModel(self.st)
                tolerance = float(group["tolerance_ps"]) / self.delay.track(layer, pairs[0].width)
            else:
                tolerance = float(group.get("tolerance_mm", 1.0))
            target_share = GROUP_SHARE * tolerance

            def spread():
                values = [v for s in pairs for v in s.lengths.values()]
                return max(values) - min(values)

            before = spread()
            for _ in range(BUMP_LIMIT):
                longest = max(v for s in pairs for v in s.lengths.values())
                short = [s for s in pairs if longest - min(s.lengths.values()) > target_share]
                if not short:
                    break
                progressed = False
                for s in short:
                    need = longest - min(s.lengths.values()) - target_share / 2
                    if need > 1e-6 and self.bump(s, need) > 0:
                        progressed = True
                if not progressed:
                    break
            after = spread()
            reports.append(
                dict(
                    name=group.get("name"),
                    tolerance_mm=round(tolerance, 6),
                    spread_before_mm=round(before, 6),
                    spread_mm=round(after, 6),
                    bumps={s.pair["name"]: s.bumps for s in pairs},
                    status=(
                        "length_unmatched"
                        if after > tolerance + 1e-9
                        else ("tuned" if any(s.bumps for s in pairs) else "ok")
                    ),
                )
            )
        return reports


def _on_segment(q, a, b, eps=1e-6):
    d = math.dist(a, b)
    if d < 1e-12:
        return False
    cross = abs((b[0] - a[0]) * (q[1] - a[1]) - (b[1] - a[1]) * (q[0] - a[0])) / d
    t = ((q[0] - a[0]) * (b[0] - a[0]) + (q[1] - a[1]) * (b[1] - a[1])) / (d * d)
    return cross < eps and -1e-9 <= t <= 1 + 1e-9


# ------------------------------------------------------------------ route_board hook


@dataclass
class CoupledRoute:
    nets: set = field(default_factory=set)  # nets routed coupled (both legs)
    tracks: List[tuple] = field(default_factory=list)  # (net, layer, a, b, width)
    report: dict = field(default_factory=dict)


def route_pairs(grid, graph, rules, *, signal_nets, fanouts, net_width, track_width, blocked=()):
    """Route the declared pairs coupled on ``grid`` (module doc), reserve their
    copper, and return what :func:`route_board` emits and reports."""
    from .fixed import reserve_fixed_copper

    out = CoupledRoute()
    router = PairRouter(grid, graph, rules, fanouts, net_width, track_width)
    pairs_report = {}
    solved_by_net = {}
    solved_list: List[Solved] = []
    for pair in rules.get("diff_pairs") or []:
        p, n = pair["p"], pair["n"]
        row = dict(status="legs")
        if p not in signal_nets or n not in signal_nets:
            row["reason"] = "not_signal_nets"
        elif p in blocked or n in blocked:
            row["reason"] = "fixed_block_port"
        elif p in out.nets or n in out.nets:
            row["reason"] = "net_in_another_pair"
        else:
            result = router.solve(pair)
            if isinstance(result, Solved):
                router.commit(result)
                out.nets |= {p, n}
                solved_list.append(result)
                solved_by_net[p] = solved_by_net[n] = result
                row = dict(status="coupled")
            else:
                reason, details = result
                row["reason"] = reason
                row.update(details)
        pairs_report[pair["name"]] = row
    groups = router.tune_groups(solved_by_net) if solved_list else []
    for s in solved_list:
        row = pairs_report[s.pair["name"]]
        p, n = s.pair["p"], s.pair["n"]
        row.update(
            layer=s.layer,
            width_mm=s.width,
            gap_mm=s.gap,
            lengths_mm={k: round(v, 6) for k, v in s.lengths.items()},
            skew_mm=round(abs(s.lengths[p] - s.lengths[n]), 6),
            skew_budget_mm=round(s.budget, 6),
            uncoupled_mm={k: round(v, 6) for k, v in s.uncoupled.items()},
            max_uncoupled_mm=s.max_uncoupled,
            attempts=s.attempts,
            bumps=s.bumps,
            exit_room=("via", "track", "access")[2 - s.room],
        )
        out.tracks.extend(s.tracks())
    if out.tracks:
        for net, layer, a, b, w in out.tracks:
            grid.escape_segments.append((grid.layers.index(layer), net, a, b))
            if w > grid.net_widths.get(net, grid.track_width):
                grid.net_widths[net] = w
        # The fanouts' protected access cells stay their nets' (the obstacles kept a
        # track of that net legal at each centre): the cell-rounded reservation
        # below would otherwise hand a cell beside a pair leg to the pair.
        obstacles = router.obstacles
        held = {}
        for owner, la, cells, _centres in obstacles.corridors:
            for c in cells:
                held[(la, *c)] = (grid.pad_net.get((la, *c)), grid.via_halo.get((la, *c)))
        reserve_fixed_copper(
            grid,
            dict(frame="engine-mm-y-up", tracks=[list(t) for t in out.tracks], vias=[]),
            max([track_width] + list(net_width.values())),
            own_net=True,
        )
        # An exit corridor the pairs' copper keeps clear exactly (a track of its net
        # along it, a via at its end) stays its net's: the cell-rounded reservation
        # would otherwise hand those cells beside a pair leg to the pair.
        restored = 0
        for owner, la, cells, centres in obstacles.corridors:
            if owner in out.nets:
                continue
            ow = obstacles._width(owner)
            near = [t for t in out.tracks if grid.layers.index(t[1]) == la]

            def keeps(a, b, w):
                return all(
                    _segment_distance_sq(a, b, t[2], t[3])
                    >= (w / 2 + obstacles._reach(owner, t[0], t[4])) ** 2 - 1e-10
                    for t in near
                )

            track_ok = all(keeps(a, b, ow) for a, b in zip(centres, centres[1:] or centres))
            via_ok = track_ok and keeps(centres[-1], centres[-1], 2 * grid.via_radius)
            for k, c in enumerate(cells):
                key = (la, *c)
                tables = []
                if track_ok:
                    tables.append((grid.pad_net, held[key][0]))
                if via_ok and k == len(cells) - 1:
                    tables.append((grid.via_halo, held[key][1]))
                for table, before in tables:
                    if table.get(key) != before:
                        restored += 1
                        if before is None:
                            table.pop(key, None)
                        else:
                            table[key] = before
        pairs_report_extra = dict(exit_cells_restored=restored)
    else:
        pairs_report_extra = {}
    out.report = dict(pairs=pairs_report, groups=groups, **pairs_report_extra)
    return out
