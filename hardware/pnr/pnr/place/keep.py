"""Displacement-minimizing legalization: triage and order-preserving local spreading.

:func:`pnr.place.legalize.legalize` uses this (``PNR_LEGALIZE_KEEP``, on by default;
:mod:`pnr.legalize_flags`) before its slot packer, on boxes it builds from the global placement
(GP): each movable part's slot (its courtyard grown as the legalizer grows it, rounded up to the
slot grid) at its GP pose, and the obstacles (fixed parts, keep-outs, parts the spreading may not
push, the outline).

1. :func:`triage` sorts the movable parts by how badly they conflict at their GP poses. A part's
   *occlusion* is the fraction of its slot covered by other slots, obstacles or the space outside
   the outline. While some part has an occlusion of at least :data:`SEVERE` (half its slot or
   more), the worst one (ties: one free to take either side, then the smaller, then the later
   reference) is *severe*: it leaves the GP
   layout, and the occlusions of the rest are taken again without it. What is left is *clean*
   (occlusion 0: legal where it is) or *mild* (a small overlap).
2. :func:`spread` resolves the mild overlaps by pushing parts apart along one axis per pair
   (horizontal and vertical constraint graphs taken from the GP poses): a pair that overlaps is
   separated along the axis of its smaller penetration, in its GP order; a pair already side by
   side keeps its order on that axis. The new centres are the weighted least-squares projection
   of the GP centres (weight: slot area, so a small part gives way to a big one) onto those
   constraints and each slot's bounds (the outline and the obstacles it faces), computed per axis
   by Dykstra's alternating projections. Pairs that come to overlap get a constraint on the axis
   they were apart on at GP, and the solve repeats (:data:`ROUNDS`). A part pushed further than
   its cascade bound (:func:`reach`), or a solve that cannot meet its constraints, fails the
   spreading.
3. :func:`resolve` spreads each cluster of nearby parts in two steps. First the physical overlap
   alone (slots side by side): when that fails, the cluster's most occluded mild part is given up
   (``push_infeasible``) and the rest spread again. Then, where the legalizer's routing-channel
   model asks for an escape channel between two facing pad rows, the channel is a *soft* goal:
   the pair's distance grows by the largest share of the channel (:data:`SHARE_STEPS` bisection
   steps between none and all of it) that the push still meets within every part's reach. A
   channel never makes the push give up, and a part short of one at its GP pose is pushed like a
   mild overlap.

The legalizer then takes, for every part that is not severe, the slot nearest its (spread) pose
within a grid snap at its GP turn; a part that cannot have it (its slot is taken:
``slot_taken``) or that the push gave up (``push_infeasible``) takes the nearest free slot in
rings expanding from its GP pose (:data:`RING_GROWTH`), its GP turn first, never the packer's
full search. Only the severe parts are relocated wholesale, last, by the packer's own cost
(displacement, routing channels, wirelength) and turn search. The legalizer's motion record
names each relocated part's reason (:data:`REASONS`).
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

# Occlusion at or above which a part is relocated instead of spread around.
SEVERE = 0.5
# Constraint-adding rounds of :func:`spread`.
ROUNDS = 6
# Dykstra sweeps per axis solve, and the feasibility tolerance (mm).
SWEEPS = 3000
TOL = 1e-6
# The least cascade bound (mm), and the share of a slot's smaller side that bounds it otherwise.
REACH_MM = 1.5
REACH_SHARE = 0.5
# Bisection steps of :func:`resolve` on the share of the routing channels a push opens.
SHARE_STEPS = 5
# The nearest-first search of a part that keeps no slot at its pose: rings of radius
# ``snap * RING_GROWTH ** k`` around its GP pose (pnr.place.legalize).
RING_GROWTH = 2.0
# Why a part left its (pushed) GP pose: half or more of it occluded (relocated wholesale), the
# push could not clear it (nearest-first rings), or its slot was taken (nearest-first rings).
SEVERE_REASON, PUSH_REASON, SLOT_REASON = "severe", "push_infeasible", "slot_taken"
REASONS = (SEVERE_REASON, PUSH_REASON, SLOT_REASON)


class Box:
    """A slot or an obstacle: centre ``x``, ``y``, size ``w``, ``h`` (mm), the occupancy
    ``planes`` it is on, and for a movable slot its ``ref``, its ``weight`` and the bounds
    ``(xlo, xhi, ylo, yhi)`` of its centre."""

    __slots__ = ("ref", "x", "y", "w", "h", "planes", "weight", "bounds", "movable")

    def __init__(self, ref, x, y, w, h, planes, weight=1.0, bounds=None, movable=True):
        self.ref = ref
        self.x, self.y, self.w, self.h = float(x), float(y), float(w), float(h)
        self.planes = frozenset(planes)
        self.weight = float(weight)
        self.bounds = bounds
        self.movable = movable

    def overlap(self, other) -> float:
        dx = min(self.x + self.w / 2, other.x + other.w / 2) - max(
            self.x - self.w / 2, other.x - other.w / 2
        )
        dy = min(self.y + self.h / 2, other.y + other.h / 2) - max(
            self.y - self.h / 2, other.y - other.h / 2
        )
        return dx * dy if dx > 0 and dy > 0 else 0.0


def _shares(a: Box, b: Box) -> bool:
    return bool(a.planes & b.planes)


def occlusions(
    slots: Sequence[Box], obstacles: Sequence[Box], width: float, height: float, skip=()
) -> Dict[str, float]:
    """{ref: occluded fraction of its slot} for ``slots`` (refs in ``skip`` are neither measured
    nor obstacles): the area covered by the other slots and the obstacles on a shared plane,
    plus the area outside ``width x height``, over the slot area (capped at 1)."""
    skip = set(skip)
    board = Box(None, width / 2.0, height / 2.0, width, height, ())
    out = {}
    for s in slots:
        if s.ref in skip:
            continue
        area = max(s.w * s.h, 1e-12)
        covered = area - s.overlap(board)
        for o in obstacles:
            if _shares(s, o):
                covered += s.overlap(o)
        for t in slots:
            if t is not s and t.ref not in skip and _shares(s, t):
                covered += s.overlap(t)
        out[s.ref] = min(1.0, max(0.0, covered / area))
    return out


def triage(
    slots: Sequence[Box],
    obstacles: Sequence[Box],
    width: float,
    height: float,
    free: Optional[Dict[str, int]] = None,
) -> Tuple[List[str], Dict[str, float]]:
    """``(severe refs in the order they were taken, {ref: occlusion})``: the occlusion of every
    slot at the end (severe ones as they were when taken). Among equally occluded parts the one
    with more ``free`` ({ref: n}, e.g. 1 for a part that may take either side), then the smaller,
    then the later reference is taken first."""
    severe: List[str] = []
    final: Dict[str, float] = {}
    free = free or {}
    area = {s.ref: s.w * s.h for s in slots}
    while True:
        occ = occlusions(slots, obstacles, width, height, skip=severe)
        worst = [r for r, v in occ.items() if v >= SEVERE - 1e-12]
        if not worst:
            final.update(occ)
            return severe, final
        pick = max(worst, key=lambda r: (occ[r], free.get(r, 0), -area[r], str(r)))
        final[pick] = occ[pick]
        severe.append(pick)


def reach(box: Box) -> float:
    """The furthest :func:`spread` may push ``box`` (mm)."""
    return max(REACH_MM, REACH_SHARE * min(box.w, box.h))


def _axis_pairs(slots, obstacles, need=None, share=1.0):
    """Initial constraints: ``{(i, j): (axis, first, need)}`` over slot indices (obstacles
    indexed after the slots), ``first`` the index in front on ``axis`` (``need``, ``share``: see
    :func:`spread`)."""
    boxes = list(slots) + list(obstacles)
    n = len(slots)
    out = {}
    for i in range(n):
        a = boxes[i]
        for j in range(i + 1, len(boxes)):
            b = boxes[j]
            if not _shares(a, b):
                continue
            ox = (a.w + b.w) / 2.0 - abs(a.x - b.x)
            oy = (a.h + b.h) / 2.0 - abs(a.y - b.y)
            if ox > 0 and oy > 0:
                axis = 0 if ox <= oy else 1
            elif oy > 0 and -ox < reach(a) + (reach(b) if j < n else 0.0):
                axis = 0  # side by side along x: keep the order
            elif ox > 0 and -oy < reach(a) + (reach(b) if j < n else 0.0):
                axis = 1  # one above the other: keep the order
            else:
                continue
            out[(i, j)] = (axis,) + _order(a, b, i, j, axis, need, share)
    return out


def _order(a, b, i, j, axis, need=None, share=1.0):
    """``(first, need)``: which of ``a`` (index i) and ``b`` (j) is in front on ``axis`` at the
    GP poses (a tie: the lower index) and the centre distance the pair needs there: their slots
    side by side, or more where ``need(front, back, axis)`` (refs) asks more (a routing channel),
    by ``share`` (0 to 1) of the difference."""
    ca, cb = (a.x, b.x) if axis == 0 else (a.y, b.y)
    gap = (a.w + b.w) / 2.0 if axis == 0 else (a.h + b.h) / 2.0
    first = i if ca <= cb else j
    if need is not None and a.ref is not None and b.ref is not None:
        front, back = (a, b) if first == i else (b, a)
        extra = need(front.ref, back.ref, axis)
        if extra is not None and float(extra) > gap:
            gap += share * (float(extra) - gap)
    return first, gap


def _solve_axis(targets, weights, lo, hi, cons, sweeps=SWEEPS, tol=TOL):
    """Weighted least-squares projection of ``targets`` onto ``{lo <= x <= hi}`` and
    ``x[j] - x[i] >= d`` for every ``(i, j, d)`` in ``cons`` (Dykstra). Returns ``(x, ok)``,
    ``ok`` false when the constraints are not met within ``tol`` after ``sweeps`` sweeps."""
    x = list(targets)
    n = len(x)
    inv = [1.0 / w for w in weights]
    pc = [(0.0, 0.0)] * len(cons)
    pb = [0.0] * n
    for _ in range(sweeps):
        for k, (i, j, d) in enumerate(cons):
            pi, pj = pc[k]
            yi, yj = x[i] + pi, x[j] + pj
            viol = d - (yj - yi)
            if viol > 0:
                mu = viol / (inv[i] + inv[j])
                xi, xj = yi - mu * inv[i], yj + mu * inv[j]
            else:
                xi, xj = yi, yj
            pc[k] = (yi - xi, yj - xj)
            x[i], x[j] = xi, xj
        for i in range(n):
            y = x[i] + pb[i]
            v = min(max(y, lo[i]), hi[i])
            pb[i] = y - v
            x[i] = v
        worst = max((d - (x[j] - x[i]) for i, j, d in cons), default=0.0)
        if worst <= tol:
            break
    worst = max((d - (x[j] - x[i]) for i, j, d in cons), default=0.0)
    bad = any(x[i] < lo[i] - tol or x[i] > hi[i] + tol for i in range(n))
    return x, worst <= 10 * tol and not bad and all(lo[i] <= hi[i] + tol for i in range(n))


def spread(
    slots: Sequence[Box], obstacles: Sequence[Box], rounds: int = ROUNDS, need=None, share=1.0
) -> Tuple[Dict[str, Tuple[float, float]], List[str]]:
    """``({ref: (x, y)} new centres, [refs pushed past their reach or left in conflict])`` for
    the movable ``slots`` among the fixed ``obstacles`` (the module docstring). ``need(front, back,
    axis)`` (refs; None: none) is a larger centre distance a pair needs along ``axis``, such as
    the routing channel between their facing pad rows; the pair asks ``share`` (0 to 1) of the
    extra distance. An empty failure
    list means every slot is clear of every other slot and obstacle on a shared plane, inside its
    bounds and within its reach."""
    n = len(slots)
    if n == 0:
        return {}, []
    boxes = list(slots) + list(obstacles)
    pairs = _axis_pairs(slots, obstacles, need, share)
    xs = [s.x for s in slots]
    ys = [s.y for s in slots]
    failed: List[str] = []
    for _ in range(rounds):
        coords = []
        ok_all = True
        for axis in (0, 1):
            lo, hi = [], []
            for s in slots:
                b = s.bounds or (-math.inf, math.inf, -math.inf, math.inf)
                lo.append(b[2 * axis])
                hi.append(b[2 * axis + 1])
            cons = []
            for (i, j), (ax, first, dist) in pairs.items():
                if ax != axis:
                    continue
                if j >= n:  # an obstacle: a bound on the slot
                    o = boxes[j]
                    oc = o.x if axis == 0 else o.y
                    if first == i:
                        hi[i] = min(hi[i], oc - dist)
                    else:
                        lo[i] = max(lo[i], oc + dist)
                    continue
                cons.append((i, j, dist) if first == i else (j, i, dist))
            target = [s.x if axis == 0 else s.y for s in slots]
            x, ok = _solve_axis(target, [s.weight for s in slots], lo, hi, cons)
            ok_all = ok_all and ok
            coords.append(x)
        xs, ys = coords
        moved = [Box(s.ref, xs[k], ys[k], s.w, s.h, s.planes) for k, s in enumerate(slots)]
        added = False
        for i in range(n):
            for j in range(i + 1, len(boxes)):
                if (i, j) in pairs:
                    continue
                a = moved[i]
                b = moved[j] if j < n else boxes[j]
                if not _shares(a, b) or a.overlap(b) <= 1e-9:
                    continue
                # A new conflict: keep the GP order on the axis the pair was apart on.
                ga, gb = slots[i], boxes[j]
                gx = abs(ga.x - gb.x) - (ga.w + gb.w) / 2.0
                gy = abs(ga.y - gb.y) - (ga.h + gb.h) / 2.0
                axis = 0 if gx >= gy else 1
                pairs[(i, j)] = (axis,) + _order(ga, gb, i, j, axis, need, share)
                added = True
        if not added:
            failed = [] if ok_all else [s.ref for s in slots]
            break
    else:
        failed = [s.ref for s in slots]
    far = [
        s.ref for k, s in enumerate(slots) if math.hypot(xs[k] - s.x, ys[k] - s.y) > reach(s) + TOL
    ]
    out = {s.ref: (xs[k], ys[k]) for k, s in enumerate(slots)}
    return out, sorted(set(failed) | set(far))


def clusters(slots: Sequence[Box], obstacles: Sequence[Box] = ()) -> List[List[str]]:
    """Refs of ``slots`` grouped by touching or nearby slots (within both reaches) on a shared
    plane: the parts one push can reach."""
    n = len(slots)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        for j in range(i + 1, n):
            a, b = slots[i], slots[j]
            if not _shares(a, b):
                continue
            gap = reach(a) + reach(b)
            if (
                abs(a.x - b.x) < (a.w + b.w) / 2.0 + gap
                and abs(a.y - b.y) < (a.h + b.h) / 2.0 + gap
            ):
                parent[find(i)] = find(j)
    groups: Dict[int, List[str]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(slots[i].ref)
    return sorted((sorted(g) for g in groups.values()), key=lambda g: g[0])


def resolve(
    slots: Sequence[Box],
    obstacles: Sequence[Box],
    width: float,
    height: float,
    occlusion: Optional[Dict[str, float]] = None,
    need=None,
    short=(),
    shares: Optional[Dict[str, float]] = None,
) -> Tuple[Dict[str, Tuple[float, float]], List[str]]:
    """Spread ``slots`` (none severe) cluster by cluster (:func:`clusters`), the physical overlap
    first: while a cluster's push fails, its most occluded mild part (``occlusion``; ties: the
    smaller, then the later reference) joins the returned list of parts the push gave up and the
    cluster is spread again without it. ``need`` (see :func:`spread`) then widens the pairs'
    distances by their routing channels, as large a share of them (all, or the largest of
    :data:`SHARE_STEPS` bisection steps) as the push still meets: a channel is a soft goal and
    never gives a part up. A cluster with no overlap is left as it is unless it holds a part of
    ``short`` (refs short of a channel at their GP poses). Returns ``({ref: (x, y)}, [refs the
    push gave up])``; ``shares`` (a dict, when given) receives each spread part's channel share."""
    occlusion = dict(occlusion or occlusions(slots, obstacles, width, height))
    drop: List[str] = []
    out: Dict[str, Tuple[float, float]] = {}
    by_ref = {s.ref: s for s in slots}
    for group in clusters(slots, obstacles):
        live = [by_ref[r] for r in group]
        if not any(occlusion.get(s.ref, 0.0) > 0 or s.ref in short for s in live):
            out.update({s.ref: (s.x, s.y) for s in live})
            continue
        while live:
            centres, bad = spread(live, obstacles)
            if bad:
                mild = [s for s in live if occlusion.get(s.ref, 0.0) > 0]
                if not mild:
                    # Nothing overlaps any more but a bound fails: leave the cluster as it was.
                    out.update({s.ref: (s.x, s.y) for s in live})
                    break
                worst = max(mild, key=lambda s: (occlusion[s.ref], -s.w * s.h, str(s.ref)))
                drop.append(worst.ref)
                live = [s for s in live if s is not worst]
                occlusion = dict(occlusion)
                occlusion.update(occlusions(live, obstacles, width, height))
                continue
            share = 0.0
            if need is not None:
                got, bad = spread(live, obstacles, need=need)
                if not bad:
                    centres, share = got, 1.0
                else:
                    lo, hi = 0.0, 1.0
                    for _ in range(SHARE_STEPS):
                        mid = (lo + hi) / 2.0
                        got, bad = spread(live, obstacles, need=need, share=mid)
                        if bad:
                            hi = mid
                        else:
                            centres, lo = got, mid
                    share = lo
            out.update(centres)
            if shares is not None:
                shares.update({s.ref: share for s in live})
            break
    return out, drop
