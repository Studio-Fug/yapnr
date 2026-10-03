"""Length tuning after detailed routing: differential pairs and length-match groups.

A design that declares ``diff_pair`` skew budgets or ``length_match`` groups gets its
matched nets tuned at the end of :func:`pnr.route.detail.router.route_board`, so every
caller (the initial pool, the place/route rounds, the Monte-Carlo and hierarchical
drivers) returns tuned copper. Nothing here runs for a design without such sets, or
with ``tuning: {meanders: false}``.

**Measure.** A net's length is what KiCad's DRC measures (:mod:`pnr.length_model`,
a transcription of KiCad's own length calculation on the geometry as the routed board
will hold it): merged track lines with their in-pad and in-via parts straightened,
plus each via's span through the board's stackup. A budget in ps compares delays
(per-layer propagation delay from the stack). Copper the route keeps as it is
(``fixed_copper``: a hierarchical block's, or pairs routed before the grid) counts in
its net's length and is never changed. A set with a member that leaves a hierarchical
block (``block_ports``) is ``partial``: only the whole board knows its length, and it
is tuned there.

**Tune.** The longest member of each set is the target. Every shorter member gets
meanders on straight runs of its own grid path, on the run's own layer, away from its
vias (and their copper), access cells and pad zones: U bumps of amplitude ``a`` cells
across ``w`` cells, one (``trombone``), several on one side (``serpentine``) or
alternating sides (``accordion``). A bump adds exactly ``2 a`` cells of track;
45-degree mitres on its corners take ``2 - sqrt(2)`` cells each, which brings the
residual under a tenth of a millimetre at the ladder's 0.25 mm pitch. Without a
``gap_mm`` rule the legs of a meander stand :data:`PREFERRED_GAP_WIDTHS` track widths
apart (edge to edge) where that is enough, and at the minimum gap (the clearance, at
least one width) where it is not. Each set is measured and tuned again until it
settles (:data:`PASSES`).

**Sets that share nets.** Pairs are tuned first, then groups, each in declared order.
Meanders only add length, so a group may lengthen a pair's legs (both, toward the
group's longest member); an earlier set that this puts out of its budget is tuned
again (:data:`FIXUP_ROUNDS`), and if it stays out the later set's tuning is undone.
Members of more than one set are never routed again (that could only shorten or move
them for one set at another's expense), and the legs of a pair are never routed
apart.

**When meanders are not enough.** The longest member is routed again around every
other net at a high via price and kept when it is shorter (``rerouted``). The members
of a group boxed in by each other are routed apart where the board has room, from the
route as it was before tuning, then tuned again; whichever attempt ends closer is kept
(``spaced``). A set still outside its budget keeps its tuned copper only when that
closed at least :data:`KEEP_PARTIAL` of its excess; otherwise it goes back to the
route as routed (``reverted``) and is reported ``length_unmatched``.

**Legality.** A bump uses the router's own predicate: each new cell passable for the
net, the net's footprint (cells, diagonal corners and track halo, as in
:func:`pnr.route.detail.maze._footprint`) disjoint from every other net's committed
footprint, no other net's copper inside the bump, and the bump clear of the net's own
copper elsewhere. A second, exact check measures each new segment against the other
nets' tracks, vias and pads (and fixed copper) in millimetres. Meanders stay on their
run's layer; the re-routing steps use the router's own search over the grid's signal
layers, so they may change layer and add vias, never on a plane layer.

**Report.** Per set: ``status`` (``ok``, ``tuned``, ``length_unmatched``, ``unrouted``,
``partial``), worst-case and nominal spread, margin, and per member its length, delay,
vias, layers, added length, bumps, mitres and meander gap. A pair also reports how
much of its P leg runs beside its N leg (within the pair's gap plus
:data:`COUPLED_TOL_MM`, same layer): the grid router routes a pair's legs as two nets,
so a pair is matched in length, not routed coupled.

Rules (``rules.json``) read here: ``diff_pairs`` (``skew_mm`` or ``skew_ps``, ``gap_mm``),
``length_match`` (``tolerance_mm`` or ``tolerance_ps``), the optional ``tuning`` block
(``gap_mm``, ``amplitude_max_mm``, ``min_segment_mm``, ``max_added_mm``, ``style``,
``mitre``, ``meanders``), ``fab``, ``layers``, ``stackup`` (else KiCad's default stack
for ``layers``), ``pad_lands`` (the exact lands of matched pads the graph does not
know, :func:`pnr.length_model.attach_board`) and ``block_ports``.

``PNR_TUNE_STRICT=1`` raises an error inside tuning instead of keeping the route
untuned (tests set it).
"""

from __future__ import annotations

import math
import os
import sys
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from pnr import length_model as lm

from .grid import Cell, RouteGrid

SQRT2 = math.sqrt(2.0)
STYLES = ("auto", "trombone", "serpentine", "accordion")
# Model margin kept inside a budget: the residual target is min(budget / 2,
# budget - MARGIN_MM) (in ps, MARGIN_MM at the slowest routed layer).
MARGIN_MM = 0.25
DEFAULT_AMPLITUDE_MM = 1.0
DEFAULT_AMPLITUDE_CAP_MM = 4.0
# Without a ``gap_mm`` rule, meander legs stand this many track widths apart (edge
# to edge) where that is enough: closer legs couple to each other and the signal
# arrives earlier than the length says. Where it is not enough, the minimum gap.
PREFERRED_GAP_WIDTHS = 3.0
# Measure-and-tune passes per set (each pass lengthens every member still short).
PASSES = 3
# Times an earlier set put out of its budget by a later one is tuned again.
FIXUP_ROUNDS = 2
# A set still outside its budget keeps its tuned copper only when that closed at
# least this share of its excess over the target.
KEEP_PARTIAL = 0.5
# Via price (mm of track per via) when the longest member of an unmatched set is
# routed again: the router's own is 3 mm; KiCad counts a via as its stack span.
REROUTE_VIA_MM = 12.0
# Price of a cell in another net's footprint (not its copper) on the second try.
REROUTE_FOOTPRINT_COST = 20.0
# Members of a set boxed in by each other are routed apart (Tuner.spread_set): a
# step within SPACING_CELLS of another member's footprint costs SPACING_COST more.
SPACING_CELLS = 2
SPACING_COST = 0.5
SPREAD_PASSES = 2
# A pair's P leg counts as coupled where its N leg is within the pair's gap plus
# this (centre lines width + gap + tolerance apart), on the same layer.
COUPLED_TOL_MM = 0.1
COUPLED_STEP_MM = 0.05


# ------------------------------------------------------------------ rules


@dataclass(frozen=True)
class MatchSet:
    name: str
    kind: str  # "diff_pair" | "length_match"
    nets: Tuple[str, ...]
    budget: float
    unit: str  # "mm" | "ps"


def match_sets(rules: Optional[dict]) -> List[MatchSet]:
    """The pairs and groups of ``rules`` with their budgets (ps when given)."""
    out: List[MatchSet] = []
    for dp in (rules or {}).get("diff_pairs") or []:
        if dp.get("skew_ps") is not None:
            out.append(
                MatchSet(dp["name"], "diff_pair", (dp["p"], dp["n"]), float(dp["skew_ps"]), "ps")
            )
        else:
            out.append(
                MatchSet(
                    dp["name"], "diff_pair", (dp["p"], dp["n"]), float(dp.get("skew_mm", 0.5)), "mm"
                )
            )
    for group in (rules or {}).get("length_match") or []:
        nets = tuple(dict.fromkeys(group.get("nets") or []))
        if len(nets) < 2:
            continue
        if group.get("tolerance_ps") is not None:
            out.append(
                MatchSet(group["name"], "length_match", nets, float(group["tolerance_ps"]), "ps")
            )
        else:
            out.append(
                MatchSet(
                    group["name"], "length_match", nets, float(group.get("tolerance_mm", 1.0)), "mm"
                )
            )
    return out


def meanders_enabled(rules: Optional[dict]) -> bool:
    """Whether the router tunes the declared sets (``tuning.meanders``, default on)."""
    return bool(((rules or {}).get("tuning") or {}).get("meanders", True))


@dataclass(frozen=True)
class Shape:
    """Meander geometry in grid cells (from the ``tuning`` rules and the fab)."""

    gap: int  # leg spacing, centre to centre, in cells (the preferred one)
    amp_min: int
    amp_max: int
    style: str = "auto"
    mitre: bool = True
    # The largest amplitude a member may escalate to when ``amp_max`` leaves its need
    # unmet: ``amp_max`` itself when the rules set ``amplitude_max_mm``.
    amp_cap: int = 0
    # The shortest straight piece a meander may have (mm); mitres keep to it.
    min_seg_mm: float = 0.0
    # The smallest leg spacing (cells) when the preferred one cannot add enough.
    gap_min: int = 0
    # Meander length one member may gain in all (mm); None: no limit.
    max_added_mm: Optional[float] = None


def shape_rules(rules: Optional[dict], pitch: float, width: float, clearance: float) -> Shape:
    """Gap at least max(clearance, width) edge to edge, preferably
    :data:`PREFERRED_GAP_WIDTHS` widths when the rules give no ``gap_mm``; amplitude
    from one cell (and the minimum segment) up to ``amplitude_max_mm``. Without that
    key the amplitude starts at 1 mm and may double up to
    :data:`DEFAULT_AMPLITUDE_CAP_MM` for a member the 1 mm meanders cannot lengthen
    enough."""
    spec = dict((rules or {}).get("tuning") or {})
    min_gap_mm = max(float(spec.get("gap_mm") or 0.0), clearance, width)
    gap_mm = (
        min_gap_mm
        if spec.get("gap_mm") is not None
        else max(min_gap_mm, PREFERRED_GAP_WIDTHS * width)
    )
    min_seg = max(float(spec.get("min_segment_mm") or 0.0), width)
    explicit = spec.get("amplitude_max_mm") is not None
    amp_max_mm = float(spec["amplitude_max_mm"]) if explicit else DEFAULT_AMPLITUDE_MM
    style = str(spec.get("style") or "auto")
    if style not in STYLES:
        raise ValueError("tuning.style must be one of %s" % ", ".join(STYLES))

    def gap_cells(mm: float) -> int:
        return max(2, math.ceil((mm + width) / pitch - 1e-9), math.ceil(min_seg / pitch - 1e-9))

    amp_min = max(1, math.ceil(min_seg / pitch - 1e-9))
    amp_max = max(amp_min, int(math.floor(amp_max_mm / pitch + 1e-9)))
    cap = (
        amp_max
        if explicit
        else max(amp_max, int(math.floor(DEFAULT_AMPLITUDE_CAP_MM / pitch + 1e-9)))
    )
    added = spec.get("max_added_mm")
    return Shape(
        gap_cells(gap_mm),
        amp_min,
        amp_max,
        style,
        bool(spec.get("mitre", True)),
        cap,
        min_seg,
        gap_cells(min_gap_mm),
        None if added is None else float(added),
    )


def residual_target(budget: float, margin: float) -> float:
    """How close each member must end to the target: min(budget/2, budget - margin),
    never under a quarter of the budget."""
    return max(budget / 4.0, min(budget / 2.0, budget - margin))


# ------------------------------------------------------------------ exact clearance


def _seg_dist(a, b, c, d) -> float:
    """Minimum distance between segments ab and cd (mm)."""

    def point_seg(p, s0, s1):
        vx, vy = s1[0] - s0[0], s1[1] - s0[1]
        ll = vx * vx + vy * vy
        if ll <= 0:
            return math.dist(p, s0)
        t = max(0.0, min(1.0, ((p[0] - s0[0]) * vx + (p[1] - s0[1]) * vy) / ll))
        return math.dist(p, (s0[0] + t * vx, s0[1] + t * vy))

    def cross(o, p, q):
        return (p[0] - o[0]) * (q[1] - o[1]) - (p[1] - o[1]) * (q[0] - o[0])

    d1, d2 = cross(c, d, a), cross(c, d, b)
    d3, d4 = cross(a, b, c), cross(a, b, d)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)) and d1 and d2 and d3 and d4:
        return 0.0
    return min(point_seg(a, c, d), point_seg(b, c, d), point_seg(c, a, b), point_seg(d, a, b))


def _seg_poly_dist(a, b, poly) -> float:
    if lm._inside_convex(poly, a) or lm._inside_convex(poly, b):
        return 0.0
    n = len(poly)
    return min(_seg_dist(a, b, poly[k], poly[(k + 1) % n]) for k in range(n))


class CopperIndex:
    """Other nets' copper in millimetres, bucketed for distance queries."""

    def __init__(self, bucket: float = 1.0):
        self.bucket = bucket
        self.items: Dict[Tuple[int, int], List[tuple]] = {}

    def _cells(self, x0, y0, x1, y1):
        b = self.bucket
        for i in range(math.floor(x0 / b), math.floor(x1 / b) + 1):
            for j in range(math.floor(y0 / b), math.floor(y1 / b) + 1):
                yield (i, j)

    def add(self, item, box):
        for key in self._cells(*box):
            self.items.setdefault(key, []).append(item)

    def add_track(self, net, layer, a, b, half):
        box = (
            min(a[0], b[0]) - half,
            min(a[1], b[1]) - half,
            max(a[0], b[0]) + half,
            max(a[1], b[1]) + half,
        )
        self.add(("track", net, layer, a, b, half), box)

    def add_via(self, net, centre, radius):
        x, y = centre
        self.add(
            ("via", net, None, centre, radius), (x - radius, y - radius, x + radius, y + radius)
        )

    def add_pad(self, net, layers, outline):
        xs = [p[0] for p in outline]
        ys = [p[1] for p in outline]
        self.add(("pad", net, layers, outline), (min(xs), min(ys), max(xs), max(ys)))

    def clear(self, net, layer, a, b, half, clearance_of) -> bool:
        """True when segment ab of ``net`` (half width ``half``) on ``layer`` keeps
        ``clearance_of(other_net)`` from every other net's copper."""
        reach = half + 1.0
        box = (
            min(a[0], b[0]) - reach,
            min(a[1], b[1]) - reach,
            max(a[0], b[0]) + reach,
            max(a[1], b[1]) + reach,
        )
        seen = set()
        for key in self._cells(*box):
            for item in self.items.get(key, ()):
                if id(item) in seen or item[1] == net:
                    continue
                seen.add(id(item))
                need = clearance_of(item[1]) - 1e-6
                kind = item[0]
                if kind == "track":
                    if item[2] != layer:
                        continue
                    if _seg_dist(a, b, item[3], item[4]) < half + item[5] + need:
                        return False
                elif kind == "via":
                    if _seg_dist(a, b, item[3], item[3]) < half + item[4] + need:
                        return False
                else:
                    if "*" not in item[2] and layer not in item[2]:
                        continue
                    if _seg_poly_dist(a, b, item[3]) < half + need:
                        return False
        return True


# ------------------------------------------------------------------ grid paths


def _edges(rn) -> List[Tuple[Cell, Cell]]:
    return [(Cell(layer, *a), Cell(layer, *b)) for layer, a, b in rn.segments]


def net_footprint(grid: RouteGrid, net: str, rn, via_keepout: int, halo: int) -> Set[Cell]:
    """The router's footprint of a routed net (:func:`maze._footprint`)."""
    from .maze import _footprint

    edges = _edges(rn)
    layers = sorted({c.layer for c in rn.cells})
    for i, j in rn.vias:
        cols = sorted({c.layer for c in rn.cells if (c.i, c.j) == (i, j)}) or layers
        if len(cols) >= 2:
            edges.append((Cell(cols[0], i, j), Cell(cols[-1], i, j)))
    return _footprint(grid, list(rn.cells), via_keepout, halo, edges=edges, net=net)


def straight_runs(rn, breaking: Set[Cell], diagonal: bool = True) -> List[List[Cell]]:
    """Maximal straight chains of the net's unit track edges on one layer (axis
    aligned, and with ``diagonal`` the 45-degree ones too) whose inner vertices have
    degree two and are not in ``breaking``."""
    adj: Dict[Cell, Set[Cell]] = {}
    for a, b in _edges(rn):
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)
    runs: List[List[Cell]] = []
    directions = ((1, 0), (0, 1)) + (((1, 1), (1, -1)) if diagonal else ())
    for di, dj in directions:

        def nxt(c):
            n = Cell(c.layer, c.i + di, c.j + dj)
            return n if n in adj.get(c, ()) else None

        def prv(c):
            n = Cell(c.layer, c.i - di, c.j - dj)
            return n if n in adj.get(c, ()) else None

        def inner(c):
            return len(adj.get(c, ())) == 2 and c not in breaking

        for c in sorted(adj, key=lambda x: (x.layer, x.i, x.j)):
            if nxt(c) is None or (prv(c) is not None and inner(c)):
                continue
            run = [c]
            cur = c
            while True:
                n = nxt(cur)
                if n is None:
                    break
                run.append(n)
                cur = n
                if not inner(cur):
                    break
            if len(run) >= 3:
                runs.append(run)
    return runs


@dataclass
class Bump:
    run: int
    k: int  # index of the first leg's base on the run
    side: int
    amp: int
    w: int


def _perp(run: List[Cell]) -> Tuple[int, int]:
    a, b = run[0], run[1]
    di, dj = b.i - a.i, b.j - a.j
    return (-dj, di)


def bump_path(run: List[Cell], bumps: Sequence[Bump]) -> List[Cell]:
    """The run's vertex list with the bumps (sorted by ``k``) inserted."""
    ni, nj = _perp(run)
    out: List[Cell] = []
    index = 0
    for bump in sorted(bumps, key=lambda b: b.k):
        out.extend(run[index : bump.k + 1])
        base0, base1 = run[bump.k], run[bump.k + bump.w]
        s = bump.side
        for t in range(1, bump.amp + 1):
            out.append(Cell(base0.layer, base0.i + s * t * ni, base0.j + s * t * nj))
        for u in range(1, bump.w):
            c = run[bump.k + u]
            out.append(Cell(c.layer, c.i + s * bump.amp * ni, c.j + s * bump.amp * nj))
        for t in range(bump.amp, 0, -1):
            out.append(Cell(base1.layer, base1.i + s * t * ni, base1.j + s * t * nj))
        index = bump.k + bump.w
    out.extend(run[index:])
    return out


def _corner(path: List[Cell], index: int) -> Optional[Cell]:
    """The opposite unit-square corner when ``path[index]`` is a 90-degree corner of
    unit orthogonal steps (else None)."""
    if index <= 0 or index >= len(path) - 1:
        return None
    u, v, x = path[index - 1], path[index], path[index + 1]
    d1 = (v.i - u.i, v.j - u.j)
    d2 = (x.i - v.i, x.j - v.j)
    if abs(d1[0]) + abs(d1[1]) != 1 or abs(d2[0]) + abs(d2[1]) != 1:
        return None
    if d1[0] * d2[0] + d1[1] * d2[1] != 0:
        return None
    return Cell(v.layer, u.i + x.i - v.i, u.j + x.j - v.j)


def _mitre(path: List[Cell], index: int) -> Optional[Tuple[List[Cell], Cell, float]]:
    """A 45-degree cut of the 90-degree corner at ``path[index]``: ``(replacement,
    new cell, cells saved)``. Orthogonal unit steps lose the corner vertex (one
    diagonal step, ``2 - sqrt(2)`` cells shorter; the new cell is the square's
    opposite corner the diagonal passes); diagonal unit steps turn into two
    orthogonal ones through their midpoint (``2 sqrt(2) - 2`` cells shorter)."""
    o = _corner(path, index)
    if o is not None:
        return [], o, 2 - SQRT2
    if index <= 0 or index >= len(path) - 1:
        return None
    u, v, x = path[index - 1], path[index], path[index + 1]
    d1 = (v.i - u.i, v.j - u.j)
    d2 = (x.i - v.i, x.j - v.j)
    if abs(d1[0]) != 1 or abs(d1[1]) != 1 or abs(d2[0]) != 1 or abs(d2[1]) != 1:
        return None
    if d1[0] * d2[0] + d1[1] * d2[1] != 0:
        return None
    m = Cell(v.layer, (u.i + x.i) // 2, (u.j + x.j) // 2)
    return [m], m, 2 * SQRT2 - 2


def _piece_cells(path: Sequence[Cell], index: int) -> List[float]:
    """Lengths (cells) of the maximal straight pieces of ``path`` that meet at or
    pass through ``path[index]``."""

    def step(a, b):
        return (b.i - a.i, b.j - a.j)

    out = []
    for lo, hi in ((index - 1, index), (index, index + 1)):
        if lo < 0 or hi >= len(path):
            continue
        d = step(path[lo], path[hi])
        a, b = lo, hi
        while a > 0 and step(path[a - 1], path[a]) == d:
            a -= 1
        while b < len(path) - 1 and step(path[b], path[b + 1]) == d:
            b += 1
        out.append((b - a) * (SQRT2 if d[0] and d[1] else 1.0))
    return out


def path_cells_mm(path: Sequence[Cell], pitch: float) -> float:
    return sum(
        pitch * (SQRT2 if (a.i != b.i and a.j != b.j) else 1.0) for a, b in zip(path, path[1:])
    )


def coupled_length(
    p_tracks: Sequence[tuple], n_tracks: Sequence[tuple], reach: float
) -> Tuple[float, float]:
    """``(coupled, total)`` track length (mm) of the P leg ``p_tracks`` (``(layer, a,
    b)``): the parts whose centre line has an N track (``n_tracks``) on the same layer
    within ``reach``, sampled every :data:`COUPLED_STEP_MM`."""
    by_layer: Dict[str, List[tuple]] = {}
    for layer, a, b in n_tracks:
        by_layer.setdefault(layer, []).append((a, b))
    coupled = total = 0.0
    for layer, a, b in p_tracks:
        length = math.dist(a, b)
        total += length
        others = by_layer.get(layer)
        if not others or length <= 0:
            continue
        steps = max(1, math.ceil(length / COUPLED_STEP_MM))
        for k in range(steps):
            t = (k + 0.5) / steps
            p = (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))
            if any(_seg_dist(p, p, c, d) <= reach + 1e-9 for c, d in others):
                coupled += length / steps
    return coupled, total


# ------------------------------------------------------------------ the tuner


@dataclass
class MemberReport:
    net: str
    length_mm: float
    delay_ps: Optional[float]
    vias: int
    added_mm: float = 0.0
    bumps: int = 0
    mitres: int = 0
    # The lengths KiCad may report for a branching member (its merge order).
    low_mm: Optional[float] = None
    high_mm: Optional[float] = None
    layers: Tuple[str, ...] = ()
    # The narrowest meander gap (edge to edge, mm) of the member's bumps.
    gap_mm: Optional[float] = None
    # Copper the route kept as it was (not tuned here) is part of the length.
    fixed: bool = False


@dataclass
class SetReport:
    name: str
    kind: str
    unit: str
    budget: float
    target_residual: float
    members: List[MemberReport] = field(default_factory=list)
    # Worst case over the members' possible KiCad lengths; nominal in KiCad's file order.
    spread: Optional[float] = None
    nominal_spread: Optional[float] = None
    status: str = "unrouted"  # ok | tuned | length_unmatched | unrouted | partial
    # Members routed again (shorter) because the others had no room to catch up.
    rerouted: List[str] = field(default_factory=list)
    # Members routed apart to make room for meanders (Tuner.spread_set).
    spaced: List[str] = field(default_factory=list)
    # The set's tuning was undone: it ended outside its budget without closing enough
    # of the gap, or it put an earlier set (``conflicts``) out of its budget.
    reverted: bool = False
    conflicts: List[str] = field(default_factory=list)
    # A pair: its P leg's track length beside the N leg, and in all (mm).
    coupled_mm: Optional[float] = None
    p_track_mm: Optional[float] = None

    def to_json(self) -> dict:
        return dict(
            name=self.name,
            kind=self.kind,
            unit=self.unit,
            budget=self.budget,
            target_residual=round(self.target_residual, 6),
            spread=None if self.spread is None else round(self.spread, 6),
            margin=None if self.spread is None else round(self.budget - self.spread, 6),
            **(
                {"nominal_spread": round(self.nominal_spread, 6)}
                if self.nominal_spread is not None and self.nominal_spread != self.spread
                else {}
            ),
            status=self.status,
            **({"rerouted": list(self.rerouted)} if self.rerouted else {}),
            **({"spaced": list(self.spaced)} if self.spaced else {}),
            **({"reverted": True} if self.reverted else {}),
            **({"conflicts": list(self.conflicts)} if self.conflicts else {}),
            **(
                {
                    "coupled_mm": round(self.coupled_mm, 3),
                    "coupled_share": (
                        round(self.coupled_mm / self.p_track_mm, 3) if self.p_track_mm else None
                    ),
                }
                if self.coupled_mm is not None
                else {}
            ),
            members=[
                dict(
                    net=m.net,
                    length_mm=round(m.length_mm, 6),
                    delay_ps=None if m.delay_ps is None else round(m.delay_ps, 3),
                    vias=m.vias,
                    layers=list(m.layers),
                    added_mm=round(m.added_mm, 6),
                    bumps=m.bumps,
                    mitres=m.mitres,
                    **({"gap_mm": round(m.gap_mm, 4)} if m.gap_mm is not None else {}),
                    **({"fixed_copper": True} if m.fixed else {}),
                    **(
                        {"length_range_mm": [round(m.low_mm, 6), round(m.high_mm, 6)]}
                        if m.low_mm is not None and m.high_mm - m.low_mm > 1e-6
                        else {}
                    ),
                )
                for m in self.members
            ],
        )


def _via_xy(v) -> Tuple[str, float, float]:
    """``(net, x, y)`` of a via record ``(net, x, y, ...)``."""
    return v[0], float(v[1]), float(v[2])


class Tuner:
    def __init__(
        self,
        board,
        graph,
        grid: RouteGrid,
        rules: dict,
        *,
        net_width: Dict[str, float],
        default_width: float,
        net_halo: Dict[str, int],
        via_keepout: int,
        access: Dict[str, Iterable[Cell]],
        via_radius: float,
        fixed_copper: Optional[dict] = None,
    ):
        self.board = board
        self.graph = graph
        self.grid = grid
        self.rules = rules
        self.net_width = net_width
        self.default_width = default_width
        self.net_halo = net_halo
        self.via_keepout = via_keepout
        self.access = {n: set(cells) for n, cells in access.items()}
        self.via_radius = via_radius
        fab = rules.get("fab") or {}
        self.clearance = float(fab.get("clearance_mm", grid.clearance))
        self.class_clearance: Dict[str, float] = {}
        for nc in rules.get("net_classes") or []:
            if nc.get("clearance_mm"):
                for n in nc.get("nets", []):
                    self.class_clearance[n] = max(
                        self.class_clearance.get(n, 0.0), nc["clearance_mm"]
                    )
        layers = int(rules.get("layers", 2) or 2)
        self.st = rules.get("stackup") or lm.default_stackup(layers)
        lm.check_stackup(self.st, layers)
        self.sets = match_sets(rules)
        self.delay = lm.DelayModel(self.st) if any(s.unit == "ps" for s in self.sets) else None
        self.members = sorted({n for s in self.sets for n in s.nets})
        # Sets per member: a member of more than one is never routed again.
        self.set_count: Dict[str, int] = {}
        for s in self.sets:
            for n in s.nets:
                self.set_count[n] = self.set_count.get(n, 0) + 1
        self.pair_legs = {n for s in self.sets if s.kind == "diff_pair" for n in s.nets}
        self.pair_gap = {
            dp["name"]: dp.get("gap_mm") for dp in rules.get("diff_pairs") or [] if dp.get("name")
        }
        # Nets that leave a hierarchical block (its sub-board's rules).
        self.ports = set(rules.get("block_ports") or [])
        # Copper kept as it is: in the length, never changed.
        self.fixed_tracks: Dict[str, List[tuple]] = {}
        self.fixed_vias: Dict[str, List[tuple]] = {}
        for net, layer, a, b, w in (fixed_copper or {}).get("tracks") or []:
            if net:
                self.fixed_tracks.setdefault(net, []).append((layer, tuple(a), tuple(b), float(w)))
        for via in (fixed_copper or {}).get("vias") or []:
            if via.get("net"):
                x, y = via["xy"]
                self.fixed_vias.setdefault(via["net"], []).append(
                    (float(x), float(y), float(via["diameter_mm"]) / 2)
                )
        # Exact lands for the length model; bounding rectangles (no corner) where the
        # graph does not know the land, for clearance.
        self.member_pads = lm.graph_pads(graph, self.members, lands=rules.get("pad_lands"))
        self.pads = lm.graph_pads(graph, conservative=True)
        # Lengths are measured in the routed board's own frame (KiCad's inside tests
        # are half-open, so the y flip matters).
        outline = getattr(graph, "outline", None)
        self.frame = lm.board_frame(outline.height) if outline is not None else None
        self.owner: Dict[Cell, Set[str]] = {}
        self.index: Optional[CopperIndex] = None
        self.tuned: Dict[str, list] = {}  # net -> [added mm, bumps, mitres, gap mm]
        self.routed: Set[str] = set()
        self.tunable: Set[str] = set()
        self.present: Set[str] = set()

    # -- helpers -------------------------------------------------------------

    def width(self, net: str) -> float:
        return self.net_width.get(net, self.default_width)

    def clearance_of(self, net: str, other: str) -> float:
        return max(
            self.clearance, self.class_clearance.get(net, 0.0), self.class_clearance.get(other, 0.0)
        )

    def net_tracks(self, net: str) -> List[tuple]:
        """``(layer, a, b, width)`` of the net: the route's, then its fixed copper."""
        own = [(t[1], tuple(t[2]), tuple(t[3]), t[4]) for t in self.board.tracks if t[0] == net]
        return own + self.fixed_tracks.get(net, [])

    def net_vias(self, net: str) -> List[tuple]:
        """``(x, y, radius)`` of the net's vias, the route's and its fixed ones."""
        own = []
        for v in self.board.vias:
            n, x, y = _via_xy(v)
            if n == net:
                own.append((x, y, self.via_radius))
        return own + self.fixed_vias.get(net, [])

    def measure(self, net: str) -> lm.NetLength:
        return lm.net_length(
            net,
            self.net_tracks(net),
            self.net_vias(net),
            self.member_pads,
            self.st,
            self.delay,
            self.via_radius,
            self.frame,
        )

    def value(self, length: lm.NetLength, unit: str) -> float:
        return length.delay_ps if unit == "ps" else length.total_mm

    def spread(self, nets: Sequence[str], unit: str) -> Tuple[float, float]:
        """(nominal, worst-case) spread of ``nets``: the worst case takes each
        branching member's whole range of lengths KiCad may report."""
        lengths = {n: self.measure(n) for n in nets}
        nominal = [self.value(x, unit) for x in lengths.values()]
        spans = [x.span(unit) for x in lengths.values()]
        return (
            max(nominal) - min(nominal),
            max(hi for _lo, hi in spans) - min(lo for lo, _hi in spans),
        )

    def copper_index(self) -> CopperIndex:
        index = CopperIndex()
        for net, layer, a, b, w in self.board.tracks:
            index.add_track(net, layer, tuple(a), tuple(b), w / 2)
        for v in self.board.vias:
            net, x, y = _via_xy(v)
            index.add_via(net, (x, y), self.via_radius)
        for net, tracks in self.fixed_tracks.items():
            for layer, a, b, w in tracks:
                index.add_track(net, layer, a, b, w / 2)
        for net, vias in self.fixed_vias.items():
            for x, y, r in vias:
                index.add_via(net, (x, y), r)
        for pad in self.pads:
            index.add_pad(pad.net, pad.layers, pad.outline)
        return index

    def prepare(self) -> None:
        """The routes' state for tuning: routed members, the owner map, the copper."""
        nets = self.board.result.nets
        self.routed = {n for n in self.members if n in nets and nets[n].routed}
        self.tunable = {n for n in self.routed if nets[n].cells}
        self.present = self.routed | {
            n for n in self.members if self.fixed_tracks.get(n) or self.fixed_vias.get(n)
        }
        self.index = self.copper_index()
        self.owner = self._owner_map() if self.routed else {}
        self.tuned = {}

    # -- grid tuning -------------------------------------------------------------

    def _owner_map(self) -> Dict[Cell, Set[str]]:
        owner: Dict[Cell, Set[str]] = {}
        for name, rn in self.board.result.nets.items():
            if not rn.cells:
                continue
            for c in net_footprint(
                self.grid, name, rn, self.via_keepout, self.net_halo.get(name, 0)
            ):
                owner.setdefault(c, set()).add(name)
        return owner

    def _footprint_of(self, net: str) -> Set[Cell]:
        rn = self.board.result.nets[net]
        if not rn.cells:
            return set()
        return net_footprint(self.grid, net, rn, self.via_keepout, self.net_halo.get(net, 0))

    def _unclaim(self, net: str) -> None:
        for c in self._footprint_of(net):
            nets = self.owner.get(c)
            if nets:
                nets.discard(net)

    def _claim_route(self, net: str) -> None:
        for c in self._footprint_of(net):
            self.owner.setdefault(c, set()).add(net)

    def _plated(self, net: str) -> bool:
        """The net changes layer through a plated pad (the router emits that as
        pad-to-cell stubs, not a via, so its grid route alone does not say so)."""
        plated = getattr(self.grid, "plated_transition", None)
        if plated is None:
            return False
        rn = self.board.result.nets[net]
        cols: Dict[Tuple[int, int], Set[int]] = {}
        for c in rn.cells:
            cols.setdefault((c.i, c.j), set()).add(c.layer)
        return any(
            len(layers) > 1 and plated(net, i, j) is not None for (i, j), layers in cols.items()
        )

    def _breaking(self, net: str, rn) -> Set[Cell]:
        """Vertices a meander must not touch: via columns, access cells, cells in the
        net's own pad zones and cells inside its vias' copper (KiCad measures a line
        from a via straight out of the via's copper, so a bump starting there would
        count short)."""
        cols: Dict[Tuple[int, int], Set[int]] = {}
        for c in rn.cells:
            cols.setdefault((c.i, c.j), set()).add(c.layer)
        out = {c for c in rn.cells if len(cols[(c.i, c.j)]) > 1}
        out |= set(self.access.get(net, ()))
        vias = self.net_vias(net)
        for c in rn.cells:
            if self.grid.pad_net.get((c.layer, c.i, c.j)) == net:
                out.add(c)
            elif vias:
                x, y = self.grid.center_of(c.i, c.j)
                if any(math.dist((x, y), (vx, vy)) < r + 1e-9 for vx, vy, r in vias):
                    out.add(c)
        return out

    def tune_member(
        self, net: str, need: float, unit: str, shape: Shape, limit_mm: Optional[float] = None
    ) -> Tuple[float, int, int, Optional[float]]:
        """Add ``need`` (mm or ps) to ``net``, at most ``limit_mm`` of track. Returns
        (added mm, bumps, mitres, meander gap edge to edge in mm)."""
        grid = self.grid
        owner, index = self.owner, self.index
        rn = self.board.result.nets[net]
        halo = self.net_halo.get(net, 0)
        width = self.width(net)
        pitch = grid.pitch
        foreign = {c for c, nets in owner.items() if nets - {net}}
        own_cols: Set[Tuple[int, int, int]] = set()
        cols: Dict[Tuple[int, int], Set[int]] = {}
        for c in rn.cells:
            cols.setdefault((c.i, c.j), set()).add(c.layer)
        for c in rn.cells:
            if len(cols[(c.i, c.j)]) > 1:  # a via column is copper on every layer
                for la in range(grid.nlayers):
                    own_cols.add((la, c.i, c.j))
            own_cols.add((c.layer, c.i, c.j))
        runs = straight_runs(rn, self._breaking(net, rn))
        diag = [run[0].i != run[1].i and run[0].j != run[1].j for run in runs]

        def in_units(mm: float, layer: int) -> float:
            if unit == "ps":
                return mm * self.delay.track(grid.layers[layer], width)
            return mm

        def step_mm(r: int) -> float:
            """Track one cell of amplitude adds on run ``r`` (two legs of one step)."""
            return 2 * pitch * (SQRT2 if diag[r] else 1.0)

        def unit_value(r: int) -> float:
            return in_units(step_mm(r), runs[r][0].layer)

        run_cells = [set(r) for r in runs]
        run_edges = [set(zip(r, r[1:])) for r in runs]

        def corners_ok(a: Cell, b: Cell) -> bool:
            """A diagonal step's two side cells (part of the router's footprint) stay
            out of the other nets' footprints."""
            if a.i == b.i or a.j == b.j:
                return True
            return Cell(a.layer, a.i, b.j) not in foreign and Cell(a.layer, b.i, a.j) not in foreign

        def legal(c: Cell, run_index: int) -> bool:
            if not grid.in_bounds(c.i, c.j) or not grid.passable(c.layer, c.i, c.j, net):
                return False
            if grid.pad_net.get((c.layer, c.i, c.j)) == net:
                return False
            for di in range(-halo, halo + 1):
                for dj in range(-halo, halo + 1):
                    if Cell(c.layer, c.i + di, c.j + dj) in foreign:
                        return False
            mine = run_cells[run_index]
            for di in (-1, 0, 1):
                for dj in (-1, 0, 1):
                    o = (c.layer, c.i + di, c.j + dj)
                    if o in own_cols and Cell(*o) not in mine:
                        return False
            return (c.layer, c.i, c.j) not in own_cols

        # Free height per run vertex and side, up to the largest amplitude allowed.
        heights: Dict[Tuple[int, int, int], int] = {}
        for r, run in enumerate(runs):
            ni, nj = _perp(run)
            for side in (1, -1):
                for u in range(1, len(run) - 1):
                    base = run[u]
                    h = 0
                    prev = base
                    while h < shape.amp_cap:
                        c = Cell(
                            base.layer, base.i + side * (h + 1) * ni, base.j + side * (h + 1) * nj
                        )
                        if not legal(c, r) or not corners_ok(prev, c):
                            break
                        prev = c
                        h += 1
                    heights[(r, side, u)] = h

        def select(amp_max: int, w: int):
            """Greedy bumps ``w`` cells wide (and apart) for ``need`` with amplitudes
            up to ``amp_max`` cells."""
            candidates = []
            for r, run in enumerate(runs):
                for side in (1, -1):
                    for k in range(1, len(run) - 1 - w):
                        amax = min(amp_max, min(heights[(r, side, u)] for u in range(k, k + w + 1)))
                        if amax >= shape.amp_min:
                            centre = abs(k + w / 2 - (len(run) - 1) / 2)
                            candidates.append((-amax, centre, r, k, side, amax))
            candidates.sort()
            chosen: List[Bump] = []
            claimed: Set[Cell] = set()
            remaining = need
            added_mm = 0.0
            for _neg, _c, r, k, side, amax in candidates:
                if remaining <= 1e-9:
                    break
                if shape.style == "trombone" and chosen:
                    break
                if shape.style in ("serpentine", "accordion") and chosen and chosen[0].run != r:
                    continue
                if shape.style == "serpentine" and chosen[:1] and chosen[0].side != side:
                    continue
                # Same side: the meander gap between neighbouring legs. Opposite
                # sides may share a leg base (the accordion's straight-through leg).
                if any(
                    b.run == r
                    and not (
                        k >= b.k + b.w + (w if b.side == side else 0)
                        or b.k >= k + w + (w if b.side == side else 0)
                    )
                    for b in chosen
                ):
                    continue
                uv = unit_value(r)
                want = min(amax, max(shape.amp_min, math.ceil(remaining / uv - 1e-9)))
                if limit_mm is not None:
                    want = min(want, int(math.floor((limit_mm - added_mm) / step_mm(r) + 1e-9)))
                # The tallest legal bump up to ``want``: a lower one may clear a corner
                # (a diagonal step's side cell) or a neighbour the taller one meets.
                for amp in range(want, shape.amp_min - 1, -1):
                    bump = Bump(r, k, side, amp, w)
                    path = bump_path(runs[r], [bump])
                    new_cells = set(path) - set(runs[r])
                    # New steps only: the run's own steps are routed copper already.
                    if not all(
                        corners_ok(a, b)
                        for a, b in zip(path, path[1:])
                        if (a, b) not in run_edges[r]
                    ):
                        continue
                    # Bumps keep the meander gap from each other's new copper.
                    if any(
                        Cell(c.layer, c.i + di, c.j + dj) in claimed
                        for c in new_cells
                        for di in (-1, 0, 1)
                        for dj in (-1, 0, 1)
                    ):
                        continue
                    # Exact millimetre check of the new segments.
                    if not self._path_clear(net, runs[r], path, index):
                        continue
                    chosen.append(bump)
                    claimed |= new_cells
                    remaining -= amp * uv
                    added_mm += amp * step_mm(r)
                    break
            return chosen, remaining

        best = None
        for w in dict.fromkeys((shape.gap, shape.gap_min or shape.gap)):
            amp_max = shape.amp_max
            chosen, remaining = select(amp_max, w)
            while remaining > 1e-9 and amp_max < shape.amp_cap:
                amp_max = min(shape.amp_cap, 2 * amp_max)
                chosen, remaining = select(amp_max, w)
            if best is None or remaining < best[1] - 1e-9:
                best = (chosen, remaining, w)
            if remaining <= 1e-9:
                break
        chosen, remaining, w = best
        if shape.style == "accordion" and len(chosen) > 1:
            ordered = sorted(chosen, key=lambda b: b.k)
            for n, b in enumerate(ordered):
                want = ordered[0].side * (-1) ** n
                if b.side != want:
                    flipped = Bump(b.run, b.k, want, b.amp, b.w)
                    ok = min(heights[(b.run, want, u)] for u in range(b.k, b.k + b.w + 1)) >= b.amp
                    path = bump_path(runs[b.run], [flipped])
                    ok = ok and all(
                        corners_ok(x, y)
                        for x, y in zip(path, path[1:])
                        if (x, y) not in run_edges[b.run]
                    )
                    if ok and self._path_clear(net, runs[b.run], path, index):
                        chosen[chosen.index(b)] = flipped
        if not chosen:
            return 0.0, 0, 0, None

        # Build each touched run's new path, then trim the overshoot with mitres.
        by_run: Dict[int, List[Bump]] = {}
        for b in chosen:
            by_run.setdefault(b.run, []).append(b)
        paths = {r: bump_path(runs[r], bs) for r, bs in by_run.items()}
        overshoot = -remaining
        mitres = 0
        if shape.mitre and overshoot > 0:
            for r in sorted(paths):
                path = paths[r]
                bumped = set(path) - set(runs[r])
                index_ = 1
                while index_ < len(path) - 1 and overshoot > 0:
                    v = path[index_]
                    cut = _mitre(path, index_)
                    near = v in bumped or path[index_ - 1] in bumped or path[index_ + 1] in bumped
                    if cut is not None and near:
                        replacement, new_cell, saved = cut
                        mv = in_units(saved * pitch, runs[r][0].layer)
                        trial = path[:index_] + replacement + path[index_ + 1 :]
                        # The pieces either side of the cut keep the minimum segment.
                        short = min(_piece_cells(trial, index_ - 1) + _piece_cells(trial, index_))
                        if short * pitch < shape.min_seg_mm - 1e-9:
                            index_ += 1
                            continue
                        if overshoot > mv / 2 and self._mitre_ok(
                            new_cell, r, legal, path, trial, net, index
                        ):
                            path = trial
                            overshoot -= mv
                            mitres += 1
                            continue
                    index_ += 1
                paths[r] = path
        added = 0.0
        for r, path in paths.items():
            added += path_cells_mm(path, pitch) - path_cells_mm(runs[r], pitch)
            self._apply(net, runs[r], path)
        return added, len(chosen), mitres, w * pitch - width

    def _mitre_ok(self, o: Cell, r: int, legal, path, trial, net, index) -> bool:
        """A mitre is legal when the cell its new copper crosses is free for the net
        (or already the path's own) and the new segments clear the other nets."""
        if o not in set(path) and not legal(o, r):
            return False
        old = set(zip(path, path[1:]))
        layer = self.grid.layers[o.layer]
        for u, x in zip(trial, trial[1:]):
            if (u, x) in old:
                continue
            if not index.clear(
                net,
                layer,
                self.grid.center_of(u.i, u.j),
                self.grid.center_of(x.i, x.j),
                self.width(net) / 2,
                lambda n: self.clearance_of(net, n),
            ):
                return False
        return True

    def _path_clear(self, net: str, run: List[Cell], path: List[Cell], index: CopperIndex) -> bool:
        old = set(zip(run, run[1:]))
        layer = self.grid.layers[run[0].layer]
        half = self.width(net) / 2
        for a, b in zip(path, path[1:]):
            if (a, b) in old:
                continue
            if not index.clear(
                net,
                layer,
                self.grid.center_of(a.i, a.j),
                self.grid.center_of(b.i, b.j),
                half,
                lambda n: self.clearance_of(net, n),
            ):
                return False
        return True

    def _apply(self, net: str, run: List[Cell], path: List[Cell]) -> None:
        """Replace ``run`` by ``path`` in the routed net, its mm tracks and the owner
        map. Only edges that change are touched."""
        grid = self.grid
        rn = self.board.result.nets[net]
        self._unclaim(net)
        layer = run[0].layer

        def undirected(seq):
            return {frozenset(((a.i, a.j), (b.i, b.j))) for a, b in zip(seq, seq[1:])}

        before, after = undirected(run), undirected(path)
        removed, added = before - after, after - before
        rn.segments = [
            s for s in rn.segments if not (s[0] == layer and frozenset((s[1], s[2])) in removed)
        ]
        rn.segments += [
            (layer, (a.i, a.j), (b.i, b.j))
            for a, b in zip(path, path[1:])
            if frozenset(((a.i, a.j), (b.i, b.j))) in added
        ]
        dropped = set(run[1:-1]) - set(path)
        cells = [c for c in rn.cells if c not in dropped]
        seen = set(cells)
        for c in path:
            if c not in seen:
                cells.append(c)
                seen.add(c)
        rn.cells = cells
        name = grid.layers[layer]
        removed_mm = {
            frozenset((lm._key(grid.center_of(*a)), lm._key(grid.center_of(*b))))
            for a, b in (tuple(e) for e in removed)
        }
        self.board.tracks = [
            t
            for t in self.board.tracks
            if not (
                t[0] == net
                and t[1] == name
                and frozenset((lm._key(tuple(t[2])), lm._key(tuple(t[3])))) in removed_mm
            )
        ]
        w = self.width(net)
        for a, b in zip(path, path[1:]):
            if frozenset(((a.i, a.j), (b.i, b.j))) in added:
                self.board.tracks.append(
                    (net, name, grid.center_of(a.i, a.j), grid.center_of(b.i, b.j), w)
                )
        self._claim_route(net)

    # -- routing a member again ------------------------------------------------

    def _search(self, net: str, access: List[Cell], blocked, soft=None):
        """The router's own A* over ``access`` (one tree), vias priced at
        :data:`REROUTE_VIA_MM`; None unless every access cell is connected."""
        from collections import defaultdict

        from .maze import _route_one, remaining_connections

        route = _route_one(
            self.grid,
            access,
            net,
            defaultdict(int),
            defaultdict(float),
            REROUTE_VIA_MM / self.grid.pitch,
            0.0,
            blocked=blocked,
            soft=soft,
        )
        if route is None or remaining_connections(access, route.edges):
            return None
        return route

    def _swap_in(self, net: str, new, index: CopperIndex):
        """Replace ``net``'s grid route (cells, segments, vias and their millimetre
        copper; the pad escapes stay) by ``new`` when every piece of copper the old
        route did not have clears the other nets in millimetres. Returns the undo
        record, or None (nothing changed) when the new copper is not clear."""
        grid = self.grid
        rn = self.board.result.nets[net]
        plated = getattr(grid, "plated_transition", lambda *a: None)
        if any(plated(net, i, j) is not None for i, j in new.vias):
            return None
        width = self.width(net)
        old_segments = {(layer, frozenset((a, b))) for layer, a, b in rn.segments}
        tracks = [
            (net, grid.layers[layer], grid.center_of(*a), grid.center_of(*b), width)
            for layer, a, b in new.segments
        ]
        vias = [(net,) + tuple(grid.center_of(i, j)) for i, j in new.vias]

        def clear_of(n):
            return self.clearance_of(net, n)

        for (layer, a, b), (_n, name, pa, pb, w) in zip(new.segments, tracks):
            if (layer, frozenset((a, b))) in old_segments:
                continue
            if not index.clear(net, name, pa, pb, w / 2, clear_of):
                return None
        old_vias = set(rn.vias)
        for (i, j), (_n, x, y) in zip(new.vias, vias):
            if (i, j) in old_vias:
                continue
            if not all(
                index.clear(net, layer, (x, y), (x, y), self.via_radius, clear_of)
                for layer in grid.layers
            ):
                return None
        undo = (
            (list(rn.cells), list(rn.segments), list(rn.vias)),
            (list(self.board.tracks), list(self.board.vias)),
        )
        grid_keys = {
            frozenset(
                (lm._key(grid.center_of(*a)), lm._key(grid.center_of(*b)), grid.layers[layer])
            )
            for layer, a, b in rn.segments
        }
        via_keys = {lm._key(grid.center_of(i, j)) for i, j in rn.vias}
        self.board.tracks = [
            t
            for t in self.board.tracks
            if not (
                t[0] == net
                and frozenset((lm._key(tuple(t[2])), lm._key(tuple(t[3])), t[1])) in grid_keys
            )
        ] + tracks
        self.board.vias = [
            v
            for v in self.board.vias
            if not (v[0] == net and lm._key((float(v[1]), float(v[2]))) in via_keys)
        ] + vias
        rn.cells, rn.segments, rn.vias = list(new.cells), list(new.segments), list(new.vias)
        return undo

    def _undo(self, net: str, undo) -> None:
        rn = self.board.result.nets[net]
        (rn.cells, rn.segments, rn.vias), (self.board.tracks, self.board.vias) = undo

    def _claim(self, net: str, old_fp: Set[Cell]) -> None:
        """Move ``net``'s entries in the owner map from ``old_fp`` to its new route."""
        for c in old_fp:
            nets = self.owner.get(c)
            if nets:
                nets.discard(net)
        self._claim_route(net)

    def may_reroute(self, net: str) -> bool:
        """A member the re-routing steps may move: routed on the grid, in no other
        set, with no fixed copper and no layer change through a plated pad."""
        return (
            net in self.tunable
            and self.set_count.get(net, 0) == 1
            and not self.fixed_tracks.get(net)
            and not self.fixed_vias.get(net)
            and not self._plated(net)
        )

    def reroute_shorter(self, net: str, unit: str) -> bool:
        """Route ``net`` again from its access cells around every other net's
        footprint (as the router's recovery pass does), with vias priced at
        :data:`REROUTE_VIA_MM`, and keep the new route only when it connects every
        access cell, its footprint and its millimetre geometry clear the other nets,
        and it measures shorter. Returns True when the net was replaced."""
        from .maze import _footprint, _to_geometry

        grid = self.grid
        rn = self.board.result.nets[net]
        access = sorted(self.access.get(net, ()), key=lambda c: (c.layer, c.i, c.j))
        if len(access) < 2 or not self.may_reroute(net):
            return False
        halo = self.net_halo.get(net, 0)
        foreign = {c for c, nets in self.owner.items() if nets - {net}}
        # The net's own cells stay usable even where they sit in another net's
        # footprint (a route the router completed at the rules' exact spacing).
        old_fp = net_footprint(grid, net, rn, self.via_keepout, halo)
        own = set(rn.cells)
        # First around the other nets' footprints (the router's own model); failing
        # that, around their copper only, with their footprints priced, and the
        # millimetre check below as the judge.
        route = self._search(net, access, foreign - own)
        if route is not None:
            fp = _footprint(grid, route.cells, self.via_keepout, halo, edges=route.edges, net=net)
            if (fp & foreign) - (old_fp & foreign):
                route = None
        if route is None:
            copper = set()
            for name, other in self.board.result.nets.items():
                if name == net or not other.cells:
                    continue
                copper.update(other.cells)
                for i, j in other.vias:
                    copper.update(Cell(la, i, j) for la in range(grid.nlayers))
            route = self._search(
                net, access, copper - own, {c: REROUTE_FOOTPRINT_COST for c in foreign - own}
            )
        if route is None:
            return False
        before = self.value(self.measure(net), unit)
        undo = self._swap_in(net, _to_geometry(route), self.index)
        if undo is None:
            return False
        if self.value(self.measure(net), unit) < before - 1e-6:
            self._claim(net, old_fp)
            self.index = self.copper_index()
            return True
        self._undo(net, undo)
        return False

    # -- room for meanders: routing a set's members apart --------------------

    def spread_set(self, s: MatchSet, movable: Optional[Sequence[str]] = None) -> List[str]:
        """Give a set's members room for meanders: route each of ``movable`` (by
        default every member :meth:`may_reroute` allows that is not a pair's leg)
        again, the others in place, around every other net's footprint as the router
        does, but with each step within :data:`SPACING_CELLS` of another member's
        footprint priced :data:`SPACING_COST` more, so a bus packed at the pins'
        pitch fans out where the board has room (forward, then backward through the
        members). A member keeps its new route when it connects, clears the other
        nets (footprint and millimetres) and does not measure longer than the set's
        longest member. Returns the members moved."""
        from .maze import _footprint, _to_geometry

        grid = self.grid
        rns = self.board.result.nets
        moved: List[str] = []
        members = set(s.nets)
        if movable is None:
            movable = [n for n in s.nets if self.may_reroute(n) and n not in self.pair_legs]
        order = list(movable)
        for k in range(SPREAD_PASSES):
            for net in order if k % 2 == 0 else order[::-1]:
                rn = rns[net]
                access = sorted(self.access.get(net, ()), key=lambda c: (c.layer, c.i, c.j))
                if len(access) < 2 or not rn.cells:
                    continue
                ceiling = max(self.value(self.measure(n), s.unit) for n in s.nets)
                halo = self.net_halo.get(net, 0)
                foreign = {c for c, nets in self.owner.items() if nets - {net}}
                mates = {c for c, nets in self.owner.items() if (nets & members) - {net}}
                soft: Dict[Cell, float] = {}
                for c in mates:
                    for di in range(-SPACING_CELLS, SPACING_CELLS + 1):
                        for dj in range(-SPACING_CELLS, SPACING_CELLS + 1):
                            x = Cell(c.layer, c.i + di, c.j + dj)
                            if x not in foreign:
                                soft[x] = SPACING_COST
                old_fp = net_footprint(grid, net, rn, self.via_keepout, halo)
                own = set(rn.cells)
                route = self._search(net, access, foreign - own, soft)
                if route is None:
                    continue
                fp = _footprint(
                    grid, route.cells, self.via_keepout, halo, edges=route.edges, net=net
                )
                if (fp & foreign) - (old_fp & foreign):
                    continue
                new = _to_geometry(route)
                if {(la, frozenset((a, b))) for la, a, b in new.segments} == {
                    (la, frozenset((a, b))) for la, a, b in rn.segments
                }:
                    continue
                undo = self._swap_in(net, new, self.index)
                if undo is None:
                    continue
                if self.value(self.measure(net), s.unit) > ceiling + 1e-6:
                    self._undo(net, undo)
                    continue
                self._claim(net, old_fp)
                self.index = self.copper_index()
                if net not in moved:
                    moved.append(net)
        return moved

    # -- state -------------------------------------------------------------------

    def _state(self, report: SetReport) -> tuple:
        """The board's copper, the routed members' grid routes and the tuning record,
        for :meth:`_restore`."""
        rns = self.board.result.nets
        return (
            list(self.board.tracks),
            list(self.board.vias),
            {
                n: (list(rns[n].cells), list(rns[n].segments), list(rns[n].vias))
                for n in self.routed
            },
            {n: list(v) for n, v in self.tuned.items()},
            list(report.rerouted),
            list(report.spaced),
        )

    def _restore(self, state: tuple, report: SetReport) -> None:
        """Back to a :meth:`_state` (the owner map follows the routes that change)."""
        tracks, vias, routes, tuned, rerouted, spaced = state
        rns = self.board.result.nets
        for n, (cells, segments, net_vias) in routes.items():
            rn = rns[n]
            if (rn.cells, rn.segments, rn.vias) == (cells, segments, net_vias):
                continue
            self._unclaim(n)
            rn.cells, rn.segments, rn.vias = list(cells), list(segments), list(net_vias)
            self._claim_route(n)
        self.board.tracks, self.board.vias = list(tracks), list(vias)
        self.tuned = {n: list(v) for n, v in tuned.items()}
        report.rerouted, report.spaced = list(rerouted), list(spaced)
        self.index = self.copper_index()

    # -- driver -------------------------------------------------------------

    def _report(self, s: MatchSet) -> SetReport:
        margin = MARGIN_MM
        if s.unit == "ps":
            slow = max(self.delay.track(layer, self.default_width) for layer in self.grid.layers)
            margin = MARGIN_MM * slow
        return SetReport(s.name, s.kind, s.unit, s.budget, residual_target(s.budget, margin))

    def _needs_tuning(self, s: MatchSet, report: SetReport) -> bool:
        """Outside the tuning target: the nominal spread (KiCad's file order) over the
        residual target, or any merge order KiCad might take over the budget."""
        report.nominal_spread, report.spread = self.spread(s.nets, s.unit)
        return (
            report.nominal_spread > report.target_residual + 1e-9
            or report.spread > report.budget + 1e-9
        )

    def _over_budget(self, s: MatchSet, report: SetReport) -> bool:
        report.nominal_spread, report.spread = self.spread(s.nets, s.unit)
        return report.spread > report.budget + 1e-9

    def tune_passes(self, s: MatchSet, report: SetReport) -> None:
        """Measure, lengthen every member short of the longest, measure again: a
        bump's exact effect is what KiCad measures, so a few passes settle it."""
        for _ in range(PASSES):
            values = {n: self.value(self.measure(n), s.unit) for n in s.nets}
            target = max(values.values())
            if target - min(values.values()) <= report.target_residual:
                break  # the set is within its target
            progressed = False
            for net in s.nets:
                if net not in self.tunable:
                    continue
                need = target - values[net]
                if need <= report.target_residual / 4:
                    continue
                # Aim a little short of the longest: an overshoot the mitres cannot
                # trim would make this member every other one's target.
                aim = need - report.target_residual / 4
                shape = shape_rules(self.rules, self.grid.pitch, self.width(net), self.clearance)
                limit = None
                if shape.max_added_mm is not None:
                    limit = shape.max_added_mm - self.tuned.get(net, [0.0])[0]
                    if limit < 2 * self.grid.pitch * shape.amp_min - 1e-9:
                        continue
                added, bumps, mitres, gap = self.tune_member(net, aim, s.unit, shape, limit)
                if not bumps:
                    continue
                record = self.tuned.setdefault(net, [0.0, 0, 0, None])
                record[0] += added
                record[1] += bumps
                record[2] += mitres
                record[3] = gap if record[3] is None else min(record[3], gap)
                self.index = self.copper_index()
                progressed = True
            if not progressed:
                break

    def _attempt(self, s: MatchSet, report: SetReport, start: tuple) -> None:
        """Meanders; then the longest member routed again; then (a group) its members
        routed apart and tuned again from ``start``, whichever ends closer."""
        self.tune_passes(s, report)
        if not self._needs_tuning(s, report):
            return
        # No room to lengthen the short members enough: route the longest one again
        # around everything else at a high via price (a via is 1.6 mm on two
        # layers), keep it if it is shorter, and tune once more.
        values = {n: self.value(self.measure(n), s.unit) for n in s.nets}
        longest = max(s.nets, key=lambda n: (values[n], n))
        if self.reroute_shorter(longest, s.unit):
            report.rerouted.append(longest)
            self.tuned.pop(longest, None)  # its meanders went with the old route
            self.tune_passes(s, report)
            if not self._needs_tuning(s, report):
                return
        movable = [n for n in s.nets if self.may_reroute(n) and n not in self.pair_legs]
        if s.kind != "length_match" or len(movable) < 1:
            return
        # A member still short has no room for meanders where it runs: its neighbours
        # box it in (a bus routed at its pins' pitch). From the route as it was before
        # tuning, route the members apart where the board has room, tune again, and
        # keep whichever attempt ends closer. A pair's legs never move apart.
        first = self._state(report)
        first_spread = report.nominal_spread
        self._restore(start, report)
        moved = self.spread_set(s, movable)
        if moved:
            self.tune_passes(s, report)
            self._needs_tuning(s, report)
        if moved and report.nominal_spread < first_spread - 1e-9:
            report.spaced = moved
        else:
            self._restore(first, report)
            self._needs_tuning(s, report)

    def _tune_set(self, k: int, reports: List[SetReport], matched: List[int]) -> bool:
        """Tune set ``k`` without putting an earlier matched set (``matched``) out of
        its budget. Returns whether set ``k`` ends within its budget."""
        s, report = self.sets[k], reports[k]
        if any(n in self.ports for n in s.nets):
            report.status = "partial"
            return False
        if not all(n in self.present for n in s.nets):
            report.status = "unrouted"
            return False
        report.status = "ok"
        if not self._needs_tuning(s, report):
            return True
        start = self._state(report)
        start_spread = report.nominal_spread
        self._attempt(s, report, start)
        changed = report.rerouted or report.spaced or any(n in self.tuned for n in s.nets)
        if changed and self._over_budget(s, report):
            excess = start_spread - report.target_residual
            if report.nominal_spread - report.target_residual > KEEP_PARTIAL * excess:
                # Not meaningfully closer: back to the route as routed.
                self._restore(start, report)
                report.reverted = True
                self._over_budget(s, report)
                return False
        # Earlier sets this one put out of their budget (shared members it
        # lengthened) are tuned again; if that fails this set's tuning is undone.
        shares = [e for e in matched if set(self.sets[e].nets) & set(s.nets)]
        for _ in range(FIXUP_ROUNDS):
            broken = [e for e in shares if self._over_budget(self.sets[e], reports[e])]
            if not broken:
                break
            for e in broken:
                self.tune_passes(self.sets[e], reports[e])
            if self._needs_tuning(s, report):
                self.tune_passes(s, report)
        broken = [e for e in shares if self._over_budget(self.sets[e], reports[e])]
        if broken:
            self._restore(start, report)
            report.reverted = True
            report.conflicts = [self.sets[e].name for e in broken]
            for e in shares:
                self._over_budget(self.sets[e], reports[e])
            self._over_budget(s, report)
            return False
        return not self._over_budget(s, report)

    def _finish(self, s: MatchSet, report: SetReport) -> None:
        """Members, spreads and status from the board as it ends (a later set may
        have lengthened a shared member)."""
        if report.status in ("partial", "unrouted"):
            return
        for net in s.nets:
            length = self.measure(net)
            added, bumps, mitres, gap = self.tuned.get(net, (0.0, 0, 0, None))
            layers = sorted(
                {t[0] for t in self.net_tracks(net)}, key=lambda name: lm._kicad_layer_id(name)
            )
            report.members.append(
                MemberReport(
                    net,
                    length.total_mm,
                    length.delay_ps,
                    length.vias,
                    added,
                    bumps,
                    mitres,
                    length.low_mm,
                    length.high_mm,
                    tuple(layers),
                    gap,
                    bool(self.fixed_tracks.get(net) or self.fixed_vias.get(net)),
                )
            )
        report.nominal_spread, report.spread = self.spread(s.nets, s.unit)
        if report.spread > report.budget + 1e-9:
            report.status = "length_unmatched"
        elif any(n in self.tuned for n in s.nets) or report.rerouted or report.spaced:
            report.status = "tuned"
        else:
            report.status = "ok"
        if s.kind == "diff_pair":
            p, n = s.nets
            gap = self.pair_gap.get(s.name) or self.clearance
            reach = (self.width(p) + self.width(n)) / 2 + gap + COUPLED_TOL_MM
            report.coupled_mm, report.p_track_mm = coupled_length(
                [t[:3] for t in self.net_tracks(p)], [t[:3] for t in self.net_tracks(n)], reach
            )

    def run(self) -> List[SetReport]:
        self.prepare()
        reports = [self._report(s) for s in self.sets]
        # Pairs first, then groups: a group then lengthens a pair's legs together.
        order = sorted(range(len(self.sets)), key=lambda k: (self.sets[k].kind != "diff_pair", k))
        matched: List[int] = []
        for k in order:
            if self._tune_set(k, reports, matched):
                matched.append(k)
        for s, report in zip(self.sets, reports):
            self._finish(s, report)
        return reports


def tune_board(
    board, graph, grid: RouteGrid, rules: Optional[dict], **kwargs
) -> Optional[List[dict]]:
    """Tune the matched nets of a routed ``board`` in place (module docstring).
    Returns the per-set report, or None when the rules declare no pair or group.

    Tuning only adds length to a finished route, so a failure inside it must not
    cost the route: the board is restored as routed, the report names the error
    (``status: "tuning_error"`` on every set) and stderr says so. With
    ``PNR_TUNE_STRICT=1`` the error is raised instead."""
    sets = match_sets(rules)
    if not sets:
        return None
    members = {n for s in sets for n in s.nets}
    nets = board.result.nets
    saved_tracks, saved_vias = list(board.tracks), list(board.vias)
    saved = {
        n: (list(nets[n].segments), list(nets[n].cells), list(nets[n].vias))
        for n in members
        if n in nets
    }
    try:
        tuner = Tuner(board, graph, grid, rules, **kwargs)
        return [r.to_json() for r in tuner.run()]
    except Exception as error:  # noqa: BLE001 - the route stands without tuning
        if os.environ.get("PNR_TUNE_STRICT") == "1":
            raise
        board.tracks, board.vias = saved_tracks, saved_vias
        for n, (segments, cells, vias) in saved.items():
            nets[n].segments, nets[n].cells, nets[n].vias = segments, cells, vias
        message = "%s: %s" % (type(error).__name__, error)
        sys.stderr.write("pnr.tune: length tuning failed, route kept untuned (%s)\n" % message)
        return [
            dict(
                name=s.name,
                kind=s.kind,
                unit=s.unit,
                budget=s.budget,
                status="tuning_error",
                error=message,
                members=[],
            )
            for s in sets
        ]
