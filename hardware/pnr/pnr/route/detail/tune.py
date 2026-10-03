"""Length tuning after detailed routing: differential pairs and length-match groups.

A design that declares ``diff_pair`` skew budgets or ``length_match`` groups gets its
matched nets tuned at the end of :func:`pnr.route.detail.router.route_board`, so every
caller (the initial pool, the place/route rounds, the Monte-Carlo and hierarchical
drivers) returns tuned copper. Nothing here runs for a design without such sets.

**Measure.** A net's length is what KiCad's DRC measures (:mod:`pnr.length_model`,
a transcription of KiCad's own length calculation on the geometry as the routed board
will hold it): merged track lines with their in-pad and in-via parts straightened,
plus each via's span through the board's stackup. A budget in ps compares delays
(per-layer propagation delay from the stack).

**Tune.** The longest member of each set is the target. Every shorter member gets
meanders on straight runs of its own grid path, on the run's own layer, away from its
vias (and their copper), access cells and pad zones: U bumps of amplitude ``a`` cells
across ``w`` cells, one (``trombone``), several on one side (``serpentine``) or
alternating sides (``accordion``). A bump adds exactly ``2 a`` cells of track;
45-degree mitres on its corners take ``2 - sqrt(2)`` cells each, which brings the
residual under a tenth of a millimetre at the ladder's 0.25 mm pitch. Each set is
measured and tuned again until it settles (:data:`PASSES`); a set still out of its
target is reported ``length_unmatched``.

**Legality.** A bump uses the router's own predicate: each new cell passable for the
net, the net's footprint (cells, diagonal corners and track halo, as in
:func:`pnr.route.detail.maze._footprint`) disjoint from every other net's committed
footprint, no other net's copper inside the bump, and the bump at least the meander
gap from the net's own copper elsewhere. A second, exact check measures each new
segment against the other nets' tracks, vias and pads in millimetres. The tuner never
changes layer, so it never puts copper on a plane layer.

Rules (``rules.json``) read here: ``diff_pairs`` (``skew_mm`` or ``skew_ps``),
``length_match`` (``tolerance_mm`` or ``tolerance_ps``), the optional ``tuning`` block
(``gap_mm``, ``amplitude_max_mm``, ``min_segment_mm``, ``style``, ``mitre``), ``fab``,
``layers``, ``stackup`` (else KiCad's default stack) and ``pad_lands`` (the exact
lands of matched pads the graph does not know, :func:`pnr.length_model.attach_board`).
"""

from __future__ import annotations

import math
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
# Measure-and-tune passes per set (each pass lengthens every member still short).
PASSES = 3
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


@dataclass(frozen=True)
class Shape:
    """Meander geometry in grid cells (from the ``tuning`` rules and the fab)."""

    gap: int  # leg spacing, centre to centre, in cells
    amp_min: int
    amp_max: int
    style: str = "auto"
    mitre: bool = True
    # The largest amplitude a member may escalate to when ``amp_max`` leaves its need
    # unmet: ``amp_max`` itself when the rules set ``amplitude_max_mm``.
    amp_cap: int = 0
    # The shortest straight piece a meander may have (mm); mitres keep to it.
    min_seg_mm: float = 0.0


def shape_rules(rules: Optional[dict], pitch: float, width: float, clearance: float) -> Shape:
    """Gap at least max(clearance, width) edge to edge; amplitude from one cell (and
    the minimum segment) up to ``amplitude_max_mm``. Without that key the amplitude
    starts at 1 mm and may double up to :data:`DEFAULT_AMPLITUDE_CAP_MM` for a member
    the 1 mm meanders cannot lengthen enough."""
    spec = dict((rules or {}).get("tuning") or {})
    gap_mm = max(float(spec.get("gap_mm") or 0.0), clearance, width)
    min_seg = max(float(spec.get("min_segment_mm") or 0.0), width)
    explicit = spec.get("amplitude_max_mm") is not None
    amp_max_mm = float(spec["amplitude_max_mm"]) if explicit else DEFAULT_AMPLITUDE_MM
    style = str(spec.get("style") or "auto")
    if style not in STYLES:
        raise ValueError("tuning.style must be one of %s" % ", ".join(STYLES))
    gap = max(2, math.ceil((gap_mm + width) / pitch - 1e-9), math.ceil(min_seg / pitch - 1e-9))
    amp_min = max(1, math.ceil(min_seg / pitch - 1e-9))
    amp_max = max(amp_min, int(math.floor(amp_max_mm / pitch + 1e-9)))
    cap = (
        amp_max
        if explicit
        else max(amp_max, int(math.floor(DEFAULT_AMPLITUDE_CAP_MM / pitch + 1e-9)))
    )
    return Shape(gap, amp_min, amp_max, style, bool(spec.get("mitre", True)), cap, min_seg)


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
    status: str = "unrouted"  # ok | tuned | length_unmatched | unrouted
    # Members routed again (shorter) because the others had no room to catch up.
    rerouted: List[str] = field(default_factory=list)
    # Members routed apart to make room for meanders (Tuner.spread_set).
    spaced: List[str] = field(default_factory=list)

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
            members=[
                dict(
                    net=m.net,
                    length_mm=round(m.length_mm, 6),
                    delay_ps=None if m.delay_ps is None else round(m.delay_ps, 3),
                    vias=m.vias,
                    added_mm=round(m.added_mm, 6),
                    bumps=m.bumps,
                    mitres=m.mitres,
                    **(
                        {"length_range_mm": [round(m.low_mm, 6), round(m.high_mm, 6)]}
                        if m.low_mm is not None and m.high_mm - m.low_mm > 1e-6
                        else {}
                    ),
                )
                for m in self.members
            ],
        )


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
        self.st = rules.get("stackup") or lm.default_stackup(int(rules.get("layers", 2) or 2))
        self.sets = match_sets(rules)
        self.delay = lm.DelayModel(self.st) if any(s.unit == "ps" for s in self.sets) else None
        self.members = sorted({n for s in self.sets for n in s.nets})
        # Exact lands for the length model; bounding rectangles (no corner) where the
        # graph does not know the land, for clearance.
        self.member_pads = lm.graph_pads(graph, self.members, lands=rules.get("pad_lands"))
        self.pads = lm.graph_pads(graph, conservative=True)
        # Lengths are measured in the routed board's own frame (KiCad's inside tests
        # are half-open, so the y flip matters).
        outline = getattr(graph, "outline", None)
        self.frame = lm.board_frame(outline.height) if outline is not None else None

    # -- helpers -------------------------------------------------------------

    def width(self, net: str) -> float:
        return self.net_width.get(net, self.default_width)

    def clearance_of(self, net: str, other: str) -> float:
        return max(
            self.clearance, self.class_clearance.get(net, 0.0), self.class_clearance.get(other, 0.0)
        )

    def measure(self, net: str) -> lm.NetLength:
        tracks = [(t[1], tuple(t[2]), tuple(t[3]), t[4]) for t in self.board.tracks if t[0] == net]
        vias = [(v[1], v[2]) for v in self.board.vias if v[0] == net]
        return lm.net_length(
            net, tracks, vias, self.member_pads, self.st, self.delay, self.via_radius, self.frame
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
        for net, x, y in self.board.vias:
            index.add_via(net, (x, y), self.via_radius)
        for pad in self.pads:
            index.add_pad(pad.net, pad.layers, pad.outline)
        return index

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
        vias = [(v[1], v[2]) for v in self.board.vias if v[0] == net]
        for c in rn.cells:
            if self.grid.pad_net.get((c.layer, c.i, c.j)) == net:
                out.add(c)
            elif vias:
                x, y = self.grid.center_of(c.i, c.j)
                if any(math.dist((x, y), v) < self.via_radius + 1e-9 for v in vias):
                    out.add(c)
        return out

    def tune_member(
        self, net: str, need: float, unit: str, shape: Shape, owner, index
    ) -> Tuple[float, int, int]:
        """Add ``need`` (mm or ps) to ``net``. Returns (added mm, bumps, mitres)."""
        grid = self.grid
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

        def unit_value(r: int) -> float:
            """What one cell of amplitude adds on run ``r`` (two legs of one step)."""
            return in_units(2 * pitch * (SQRT2 if diag[r] else 1.0), runs[r][0].layer)

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

        def select(amp_max: int):
            """Greedy bumps for ``need`` with amplitudes up to ``amp_max`` cells."""
            w = shape.gap
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
                        k >= b.k + b.w + (shape.gap if b.side == side else 0)
                        or b.k >= k + w + (shape.gap if b.side == side else 0)
                    )
                    for b in chosen
                ):
                    continue
                uv = unit_value(r)
                want = min(amax, max(shape.amp_min, math.ceil(remaining / uv - 1e-9)))
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
                    break
            return chosen, remaining

        amp_max = shape.amp_max
        chosen, remaining = select(amp_max)
        while remaining > 1e-9 and amp_max < shape.amp_cap:
            amp_max = min(shape.amp_cap, 2 * amp_max)
            chosen, remaining = select(amp_max)
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
            return 0.0, 0, 0

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
            self._apply(net, runs[r], path, owner)
        return added, len(chosen), mitres

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

    def _apply(self, net: str, run: List[Cell], path: List[Cell], owner) -> None:
        """Replace ``run`` by ``path`` in the routed net, its mm tracks and the owner
        map. Only edges that change are touched."""
        grid = self.grid
        rn = self.board.result.nets[net]
        halo = self.net_halo.get(net, 0)
        for c in net_footprint(grid, net, rn, self.via_keepout, halo):
            nets = owner.get(c)
            if nets:
                nets.discard(net)
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
        for c in net_footprint(grid, net, rn, self.via_keepout, halo):
            owner.setdefault(c, set()).add(net)

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
            v for v in self.board.vias if not (v[0] == net and lm._key((v[1], v[2])) in via_keys)
        ] + vias
        rn.cells, rn.segments, rn.vias = list(new.cells), list(new.segments), list(new.vias)
        return undo

    def _undo(self, net: str, undo) -> None:
        rn = self.board.result.nets[net]
        (rn.cells, rn.segments, rn.vias), (self.board.tracks, self.board.vias) = undo

    def _claim(self, net: str, old_fp: Set[Cell], owner) -> None:
        """Move ``net``'s entries in the owner map from ``old_fp`` to its new route."""
        for c in old_fp:
            nets = owner.get(c)
            if nets:
                nets.discard(net)
        rn = self.board.result.nets[net]
        halo = self.net_halo.get(net, 0)
        for c in net_footprint(self.grid, net, rn, self.via_keepout, halo):
            owner.setdefault(c, set()).add(net)

    def reroute_shorter(self, net: str, unit: str, owner, index: CopperIndex) -> bool:
        """Route ``net`` again from its access cells around every other net's
        footprint (as the router's recovery pass does), with vias priced at
        :data:`REROUTE_VIA_MM`, and keep the new route only when it connects every
        access cell, its footprint and its millimetre geometry clear the other nets,
        and it measures shorter. Routes through plated pad transitions are left as
        they are. Returns True when the net was replaced."""
        from .maze import _footprint, _to_geometry

        grid = self.grid
        rn = self.board.result.nets[net]
        access = sorted(self.access.get(net, ()), key=lambda c: (c.layer, c.i, c.j))
        if len(access) < 2:
            return False
        plated = getattr(grid, "plated_transition", lambda *a: None)
        if any(plated(net, i, j) is not None for i, j in rn.vias):
            return False
        halo = self.net_halo.get(net, 0)
        foreign = {c for c, nets in owner.items() if nets - {net}}
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
        undo = self._swap_in(net, _to_geometry(route), index)
        if undo is None:
            return False
        if self.value(self.measure(net), unit) < before - 1e-6:
            self._claim(net, old_fp, owner)
            return True
        self._undo(net, undo)
        return False

    # -- room for meanders: routing a set's members apart --------------------

    def _snapshot(self, nets: Sequence[str]) -> tuple:
        """The board's copper and ``nets``' grid routes, for :meth:`_restore`."""
        rns = self.board.result.nets
        return (
            list(self.board.tracks),
            list(self.board.vias),
            {n: (list(rns[n].cells), list(rns[n].segments), list(rns[n].vias)) for n in nets},
        )

    def _restore(self, snap, owner) -> None:
        """Back to a :meth:`_snapshot`; the owner map is rebuilt from the routes."""
        tracks, vias, routes = snap
        self.board.tracks, self.board.vias = list(tracks), list(vias)
        rns = self.board.result.nets
        for n, (cells, segments, net_vias) in routes.items():
            rns[n].cells, rns[n].segments, rns[n].vias = list(cells), list(segments), list(net_vias)
        owner.clear()
        owner.update(self._owner_map())

    def spread_set(self, s: MatchSet, owner, index, owner_set=None) -> List[str]:
        """Give a set's members room for meanders: route each again, the others in
        place, around every other net's footprint as the router does, but with each
        step within :data:`SPACING_CELLS` of another member's footprint priced
        :data:`SPACING_COST` more, so a bus packed at the pins' pitch fans out where
        the board has room (forward, then backward through the members). A member
        keeps its new route when it connects, clears the other nets (footprint and
        millimetres) and does not measure longer than the set's longest member.
        Returns the members moved."""
        from .maze import _footprint, _to_geometry

        grid = self.grid
        rns = self.board.result.nets
        plated = getattr(grid, "plated_transition", lambda *a: None)
        moved: List[str] = []
        members = set(s.nets)
        # Members matched in an earlier set keep their routes.
        order = [n for n in s.nets if (owner_set or {}).get(n, s.name) == s.name]
        for k in range(SPREAD_PASSES):
            for net in order if k % 2 == 0 else order[::-1]:
                rn = rns[net]
                access = sorted(self.access.get(net, ()), key=lambda c: (c.layer, c.i, c.j))
                if len(access) < 2 or not rn.cells:
                    continue
                if any(plated(net, i, j) is not None for i, j in rn.vias):
                    continue
                ceiling = max(self.value(self.measure(n), s.unit) for n in s.nets)
                halo = self.net_halo.get(net, 0)
                foreign = {c for c, nets in owner.items() if nets - {net}}
                mates = {c for c, nets in owner.items() if (nets & members) - {net}}
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
                undo = self._swap_in(net, new, index)
                if undo is None:
                    continue
                if self.value(self.measure(net), s.unit) > ceiling + 1e-6:
                    self._undo(net, undo)
                    continue
                self._claim(net, old_fp, owner)
                index = self.copper_index()
                if net not in moved:
                    moved.append(net)
        return moved

    # -- driver -------------------------------------------------------------

    def run(self) -> List[SetReport]:
        nets = self.board.result.nets
        routed = {n for n in self.members if n in nets and nets[n].routed}
        index = self.copper_index()
        owner = self._owner_map() if routed else {}
        reports: List[SetReport] = []
        tuned: Dict[str, Tuple[float, int, int]] = {}
        owner_set: Dict[str, str] = {}
        for s in self.sets:
            margin = MARGIN_MM
            if s.unit == "ps":
                slow = max(
                    self.delay.track(layer, self.default_width) for layer in self.grid.layers
                )
                margin = MARGIN_MM * slow
            report = SetReport(s.name, s.kind, s.unit, s.budget, residual_target(s.budget, margin))
            reports.append(report)
            if not all(n in routed for n in s.nets):
                report.status = "unrouted"
                continue
            status = "ok"

            def tune_passes():
                """Measure, lengthen every member short of the longest, measure again:
                a bump's exact effect is what KiCad measures, so a few passes settle it."""
                nonlocal index, status
                for _ in range(PASSES):
                    values = {n: self.value(self.measure(n), s.unit) for n in s.nets}
                    target = max(values.values())
                    if target - min(values.values()) <= report.target_residual:
                        break  # the set is within its target
                    progressed = False
                    for net in s.nets:
                        need = target - values[net]
                        if need <= report.target_residual / 4:
                            continue
                        if owner_set.get(net, s.name) != s.name:
                            continue  # matched in an earlier set: left as it is
                        # Aim a little short of the longest: an overshoot the mitres
                        # cannot trim would make this member every other one's target.
                        aim = need - report.target_residual / 4
                        shape = shape_rules(
                            self.rules, self.grid.pitch, self.width(net), self.clearance
                        )
                        added, bumps, mitres = self.tune_member(
                            net, aim, s.unit, shape, owner, index
                        )
                        if not bumps:
                            continue
                        before = tuned.get(net, (0.0, 0, 0))
                        tuned[net] = (before[0] + added, before[1] + bumps, before[2] + mitres)
                        owner_set[net] = s.name
                        index = self.copper_index()
                        progressed = True
                        status = "tuned"
                    if not progressed:
                        break

            def unmatched() -> bool:
                # Matched: the nominal spread (KiCad's file order) within the target
                # and every merge order KiCad might take within the budget.
                report.nominal_spread, report.spread = self.spread(s.nets, s.unit)
                return (
                    report.nominal_spread > report.target_residual + 1e-9
                    or report.spread > report.budget + 1e-9
                )

            start = (None, self._snapshot(s.nets), dict(tuned), dict(owner_set), status, [])
            tune_passes()
            if unmatched():
                # No room to lengthen the short members enough: route the longest one
                # again around everything else at a high via price (a via is 1.6 mm on
                # two layers), keep it if it is shorter, and tune once more.
                values = {n: self.value(self.measure(n), s.unit) for n in s.nets}
                longest = max(s.nets, key=lambda n: (values[n], n))
                if owner_set.get(longest, s.name) == s.name and self.reroute_shorter(
                    longest, s.unit, owner, index
                ):
                    report.rerouted.append(longest)
                    tuned.pop(longest, None)  # its meanders went with the old route
                    index = self.copper_index()
                    status = "tuned"
                    tune_passes()
            if unmatched() and len(s.nets) > 1:
                # A member still short has no room for meanders where it runs: its
                # neighbours box it in (a bus routed at its pins' pitch). From the
                # route as it was before tuning, route the members apart where the
                # board has room, tune again, and keep whichever attempt ends closer.
                first = (
                    report.nominal_spread,
                    self._snapshot(s.nets),
                    dict(tuned),
                    dict(owner_set),
                    status,
                    list(report.rerouted),
                )

                def back_to(snap):
                    nonlocal index, status
                    self._restore(snap[1], owner)
                    tuned.clear()
                    tuned.update(snap[2])
                    owner_set.clear()
                    owner_set.update(snap[3])
                    status = snap[4]
                    report.rerouted = list(snap[5])
                    index = self.copper_index()

                back_to(start)
                moved = self.spread_set(s, owner, index, owner_set)
                if moved:
                    index = self.copper_index()
                    status = "tuned"
                    tune_passes()
                    unmatched()
                if moved and report.nominal_spread < first[0] - 1e-9:
                    report.spaced = moved
                else:
                    back_to(first)
            if unmatched():
                status = "length_unmatched"
            report.status = status
        for s, report in zip(self.sets, reports):
            for net in s.nets:
                if net not in routed:
                    continue
                length = self.measure(net)
                added, bumps, mitres = tuned.get(net, (0.0, 0, 0))
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
                    )
                )
            if len(report.members) == len(s.nets):
                report.nominal_spread, report.spread = self.spread(s.nets, s.unit)
        return reports


def tune_board(
    board, graph, grid: RouteGrid, rules: Optional[dict], **kwargs
) -> Optional[List[dict]]:
    """Tune the matched nets of a routed ``board`` in place (module docstring).
    Returns the per-set report, or None when the rules declare no pair or group.

    Tuning only adds length to a finished route, so a failure inside it must not
    cost the route: the board is restored as routed and the report names the error
    (``status: "tuning_error"`` on every set)."""
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
        board.tracks, board.vias = saved_tracks, saved_vias
        for n, (segments, cells, vias) in saved.items():
            nets[n].segments, nets[n].cells, nets[n].vias = segments, cells, vias
        return [
            dict(
                name=s.name,
                kind=s.kind,
                unit=s.unit,
                budget=s.budget,
                status="tuning_error",
                error="%s: %s" % (type(error).__name__, error),
                members=[],
            )
            for s in sets
        ]
