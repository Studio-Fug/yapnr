"""Detail moves on a legal placement with side-free parts: flips and pairwise swaps.

Global placement relaxes sides and the legalizer snaps them, but neither can move a
part across a crowded side nor exchange two parts. This seeded pass can. Each
proposal picks a movable part (not fixed, locked, a macro, or a row or line member)
and draws one candidate from

``flip``
    the part on its other side (when the side plan frees it): the cheapest of its
    allowed quarter turns there that is legal at the same centre or, when that is
    taken, at the nearest legal centre within :data:`FLIP_RADIUS_MM`;
``swap``
    the part and one of its :data:`SWAP_PARTNERS` nearest partners with a similar
    courtyard (area within a factor of two) that share a net with it or sit within
    :data:`SWAP_RADIUS_MM`, each at the other's centre, each re-picking its turn and
    side (the cheapest legal pair);
``stay``.

The cost is the half-perimeter wirelength plus the side cost
(:func:`pnr.place.sides.side_cost`: layer changes, side preferences, departures from
the source side), in millimetres, plus global placement's soft terms
(:mod:`pnr.place.model`): ``weight * d**2`` for an ``edge_align`` part's courtyard at
``d`` mm from its edge, and ``weight * max(0, d - radius)**2`` for a ``group`` member
at ``d`` mm from its anchor. Candidates are drawn by
:func:`pnr.place.anneal.choose_cost` at a temperature that falls linearly to zero over
the first 70 % of the budget (greedy after), and the cheapest board seen is
returned. Options are costed before they are checked, cheapest first, and one that
cannot come within :data:`PRUNE` temperatures of staying is never checked (it would
practically never be drawn). Legality is :func:`pnr.place.metrics.pose_checker` with
the legalizer's clearance, spread and per-part ``inflation``, so a move keeps the
same slack as a legalized slot. The legalizer's escape-channel model
(:class:`pnr.place.channels.ChannelModel`) is not charged here: it scores only
same-side pad rows, so charging it favours parking parts back to back on the other
side, where vias cannot land (on the double-sided chaser rung the mandatory
source-start finalist then failed to route on both seeds, after ~380 s each). Without
``allow_rotation`` (the placer's ``orient=False``) no part turns. The result is
checked with :func:`pnr.place.metrics.hard_violations`; should a move have broken a
rule the incremental checker does not know, the legal input is returned instead.
The pass runs only when the side plan frees a part; it is deterministic under
``seed``.
"""

from __future__ import annotations

import math
import random
from typing import Dict, List, Tuple

from pnr.graph import BoardGraph

from .anneal import choose_cost
from .geometry import (
    courtyard_rect,
    edge_distance,
    outline_size,
    pin_positions,
    resolve_fixed_poses,
    resolve_hard_rotations,
    set_component_side,
)
from .metrics import hard_violations, pose_checker
from .sides import FLIP_MM, SIDE_PREF_MM, VIA_MM, macro, opposite

BUDGET_PER_PART = 30
FLIP_RADIUS_MM = 2.0
SWAP_RADIUS_MM = 6.0
SWAP_PARTNERS = 4
GRID_MM = 0.5
START_TEMPERATURE_MM = 1.0
# A candidate more than PRUNE x the current temperature (mm) worse than staying even
# before its legality check is skipped: its draw probability is below exp(-PRUNE)
# (at zero temperature only improving candidates are checked).
PRUNE = 5.0
SALT = 0x51DE5


# Offsets (grid steps) within FLIP_RADIUS_MM, nearest first.
RING = [
    (dx, dy)
    for _, dx, dy in sorted(
        (dx * dx + dy * dy, dx, dy)
        for dx in range(-int(FLIP_RADIUS_MM / GRID_MM), int(FLIP_RADIUS_MM / GRID_MM) + 1)
        for dy in range(-int(FLIP_RADIUS_MM / GRID_MM), int(FLIP_RADIUS_MM / GRID_MM) + 1)
        if (dx * dx + dy * dy) * GRID_MM**2 <= FLIP_RADIUS_MM**2 + 1e-9
    )
]


class _Cost:
    """Incremental HPWL + side cost over the nets of the parts a move touches.

    Per net and set of moving parts, the bounding box and sides of the parts that do
    not move are cached (:meth:`invalidate` after an accepted move), so costing a
    trial pose only merges the moving parts' pins."""

    def __init__(self, graph, side_plan, constraints=None):
        self.graph = graph
        self.plan = side_plan
        # Global placement's soft terms (pnr.place.model): edge pulls and group radii.
        self.edges: Dict[str, List[Tuple[str, float]]] = {}
        self.groups: Dict[str, List[Tuple[str, str, float, float]]] = {}
        self.size = (0.0, 0.0)
        if constraints is not None:
            self.size = outline_size(graph, constraints)
            for con in constraints.constraints:
                if con.kind == "edge_align" and con.params.get("edge"):
                    for ref in con.refs:
                        self.edges.setdefault(ref, []).append(
                            (con.params["edge"], float(con.weight or 1.0))
                        )
                elif con.kind == "group" and con.params.get("anchor"):
                    anchor = con.params["anchor"]
                    radius = float(con.params.get("radius_mm") or 5.0)
                    for ref in con.refs:
                        if ref == anchor:
                            continue
                        term = (anchor, ref, radius, float(con.weight or 1.0))
                        self.groups.setdefault(anchor, []).append(term)
                        self.groups.setdefault(ref, []).append(term)
        self.nets = {n.name: n.pins for n in graph.nets if n.name and len(n.pins) >= 2}
        self.splittable = {name for name, _ in side_plan.nets(graph)}
        self.net_refs = {name: sorted({r for r, _ in pins}) for name, pins in self.nets.items()}
        self.by_ref: Dict[str, List[str]] = {}
        for name, refs in self.net_refs.items():
            for ref in refs:
                self.by_ref.setdefault(ref, []).append(name)
        self.comps = {c.ref: c for c in graph.components}
        self.pins_of: Dict[Tuple[str, str], List[Tuple[str, str]]] = {}
        for name, pins in self.nets.items():
            for pin in pins:
                self.pins_of.setdefault((name, pin[0]), []).append(pin)
        self.pins: Dict[Tuple[str, str], Tuple[float, float]] = {}
        self.static = {}
        for comp in graph.components:
            self.refresh(comp)

    def refresh(self, comp):
        for name, xy in pin_positions(comp):
            self.pins[(comp.ref, name)] = xy

    def invalidate(self):
        self.static.clear()

    def nets_of(self, refs):
        return sorted({n for r in refs for n in self.by_ref.get(r, ())})

    def _fixed_part(self, name, moving):
        key = (name, moving)
        hit = self.static.get(key)
        if hit is None:
            pts = [self.pins[p] for p in self.nets[name] if p[0] not in moving and p in self.pins]
            box = (
                (
                    min(p[0] for p in pts),
                    max(p[0] for p in pts),
                    min(p[1] for p in pts),
                    max(p[1] for p in pts),
                )
                if pts
                else None
            )
            sides = frozenset(self.comps[r].side for r in self.net_refs[name] if r not in moving)
            hit = self.static[key] = (box, len(pts), sides)
        return hit

    def net_cost(self, name, moving=frozenset()):
        box, count, sides = self._fixed_part(name, moving)
        mine = [r for r in moving if (name, r) in self.pins_of]
        pts = [self.pins[p] for r in mine for p in self.pins_of[(name, r)] if p in self.pins]
        if count + len(pts) < 2:
            return 0.0
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        if box is not None:
            xs += [box[0], box[1]]
            ys += [box[2], box[3]]
        cost = (max(xs) - min(xs)) + (max(ys) - min(ys))
        if name in self.splittable:
            if len(sides | {self.comps[r].side for r in mine}) > 1:
                cost += VIA_MM
        return cost

    def part_cost(self, comp):
        cost = 0.0
        for edge, weight in self.edges.get(comp.ref, ()):
            cost += weight * edge_distance(comp, edge, *self.size) ** 2
        pref = self.plan.preferred.get(comp.ref)
        if pref is not None and comp.side != pref[0]:
            cost += SIDE_PREF_MM * pref[1]
        if len(self.plan.options.get(comp.ref, ())) > 1 and comp.side != self.plan.source.get(
            comp.ref, comp.side
        ):
            cost += FLIP_MM
        return cost

    def local(self, comps):
        """Cost of the nets and part terms ``comps`` touch, at their current poses."""
        moving = frozenset(c.ref for c in comps)
        for comp in comps:
            self.refresh(comp)
        groups = {term for ref in moving for term in self.groups.get(ref, ())}
        return (
            sum(self.net_cost(n, moving) for n in self.nets_of(moving))
            + sum(self.part_cost(c) for c in comps)
            + sum(
                weight
                * max(
                    0.0,
                    math.dist(self.comps[anchor].pos, self.comps[member].pos) - radius,
                )
                ** 2
                for anchor, member, radius, weight in sorted(groups)
            )
        )


def _pose(comp):
    return (comp.pos, comp.rot, comp.side)


def _set(comp, pose):
    comp.pos, comp.rot = pose[0], pose[1]
    set_component_side(comp, pose[2])


def improve(
    graph,
    constraints,
    side_plan,
    *,
    seed=0,
    spread=1.0,
    pad_edge=None,
    budget=None,
    inflation=None,
    allow_rotation=True,
):
    """Return a copy of legal ``graph`` after the seeded flip/swap pass (see module)."""
    placed = BoardGraph.from_json(graph.to_json())
    if not side_plan.active:
        return placed
    clearance = float(constraints.board.default_clearance_mm)
    try:
        legal = pose_checker(
            placed,
            constraints,
            clearance=clearance,
            spread=spread,
            pad_edge=pad_edge,
            inflation=inflation,
        )
    except ValueError:
        return placed  # not a legal baseline: nothing to improve safely
    fixed = set(resolve_fixed_poses(placed, constraints)) | set(constraints.locked_refs)
    lined = {
        r for con in constraints.constraints if con.kind in ("line_group", "row") for r in con.refs
    }
    rotations = resolve_hard_rotations(constraints)
    movable = sorted(
        c.ref
        for c in placed.components
        if c.ref not in fixed and c.ref not in lined and not c.locked and not macro(c)
    )
    if not movable:
        return placed
    cost = _Cost(placed, side_plan, constraints)
    rng = random.Random(seed ^ SALT)
    budget = BUDGET_PER_PART * len(movable) if budget is None else budget
    hot = max(1, int(0.7 * budget))
    best_total = total = cost.local(placed.components)
    best = {ref: _pose(placed.component(ref)) for ref in movable}
    stats = dict(proposals=0, flips=0, swaps=0, budget=budget)

    def turns(comp):
        if comp.ref in rotations:
            return [rotations[comp.ref]]
        if not allow_rotation:
            return [comp.rot]
        return [(comp.rot + 90 * k) % 360 for k in range(4)]

    def sides_of(comp):
        return list(side_plan.options.get(comp.ref, (comp.side,)))

    def nearest_legal(comp, centre):
        """Nearest legal centre to ``centre`` within FLIP_RADIUS_MM (comp's turn/side)."""
        for dx, dy in RING:
            comp.pos = (centre[0] + dx * GRID_MM, centre[1] + dy * GRID_MM)
            if legal([comp]):
                return comp.pos
        return None

    def flip_candidates(comp, limit):
        """The cheapest legal flip of ``comp`` (at most one candidate): its turns on
        the other side in cost order, each at the same centre or, when that is
        taken, at the nearest legal centre within FLIP_RADIUS_MM."""
        if len(sides_of(comp)) < 2:
            return []
        start = _pose(comp)
        before = cost.local([comp])
        side = opposite(comp.side)
        options = []
        for rot in turns(comp):
            _set(comp, (start[0], rot, side))
            options.append((cost.local([comp]), rot))
        if min(options)[0] - before > limit:  # never drawn; not worth a legality check
            _set(comp, start)
            cost.refresh(comp)
            return []
        out = []
        searched = set()
        for _, rot in sorted(options):
            _set(comp, (start[0], rot, side))
            if not legal([comp]):
                key = int(round(rot)) % 180
                if key in searched or nearest_legal(comp, start[0]) is None:
                    searched.add(key)
                    continue
            out = [("flip", [(comp, _pose(comp))], cost.local([comp]) - before)]
            break
        _set(comp, start)
        cost.refresh(comp)
        return out

    def best_pose(comp, centre, others, ignore=(), check=True):
        """Cheapest legal (turn, side) for ``comp`` at ``centre`` with ``others`` moved
        and the parts in ``ignore`` left out of the overlap test: options are costed
        first and tested for legality cheapest first (``check=False``: the cheapest,
        legal or not, a lower bound)."""
        options = []
        here = comp.side
        for side in sides_of(comp):
            for rot in turns(comp):
                _set(comp, (centre, rot, side))
                options.append((cost.local([comp] + others), side != here, rot, side))
        for option in sorted(options):
            _set(comp, (centre, option[2], option[3]))
            if not check or legal([comp] + others, ignore=ignore):
                return option
        return None

    def swap_candidates(comp, limit):
        a = courtyard_rect(comp)
        area = a.w * a.h
        peers = set()
        for name in cost.by_ref.get(comp.ref, ()):
            peers.update(cost.net_refs[name])
        partners = []
        for ref in movable:
            if ref == comp.ref:
                continue
            other = placed.component(ref)
            b = courtyard_rect(other)
            if not 0.5 <= (b.w * b.h) / max(area, 1e-9) <= 2.0:
                continue
            distance = math.dist(comp.pos, other.pos)
            if ref in peers or distance <= SWAP_RADIUS_MM:
                partners.append((distance, ref))
        out = []
        for _, ref in sorted(partners)[:SWAP_PARTNERS]:
            other = placed.component(ref)
            first, second = _pose(comp), _pose(other)
            before = cost.local([comp, other])
            # A lower bound first (cheapest poses, legality ignored): a swap that cannot
            # come within ``limit`` of staying is never drawn, so it is not checked.
            pick = best_pose(comp, second[0], [], check=False)
            _set(comp, (second[0], pick[2], pick[3]))
            bound = best_pose(other, first[0], [comp], check=False)[0] - before
            _set(comp, first)
            _set(other, second)
            pick = None if bound > limit else best_pose(comp, second[0], [], ignore={other.ref})
            if pick is not None:
                _set(comp, (second[0], pick[2], pick[3]))
                other_pick = best_pose(other, first[0], [comp])
                if other_pick is not None:
                    _set(other, (first[0], other_pick[2], other_pick[3]))
                    after = cost.local([comp, other])
                    out.append(
                        ("swap", [(comp, _pose(comp)), (other, _pose(other))], after - before)
                    )
            _set(comp, first)
            _set(other, second)
            cost.refresh(comp)
            cost.refresh(other)
        return out

    for step in range(budget):
        stats["proposals"] += 1
        comp = placed.component(movable[rng.randrange(len(movable))])
        temperature = START_TEMPERATURE_MM * max(0.0, 1.0 - step / hot)
        limit = PRUNE * temperature + 1e-9
        candidates = (
            [("stay", [], 0.0)] + flip_candidates(comp, limit) + swap_candidates(comp, limit)
        )
        chosen = choose_cost([c[2] for c in candidates], temperature, rng)
        kind, moves, delta = candidates[chosen]
        if kind == "stay":
            continue
        for part, pose in moves:
            _set(part, pose)
            cost.refresh(part)
        cost.invalidate()
        legal.update([part.ref for part, _ in moves])
        total += delta
        stats[kind + "s"] += 1
        if total < best_total - 1e-9:
            best_total = total
            best = {ref: _pose(placed.component(ref)) for ref in movable}
    for ref, pose in best.items():
        _set(placed.component(ref), pose)
    stats.update(best_mm=best_total, reverted=False)
    if any(hard_violations(placed, constraints).values()):
        # A rule pose_checker does not know: keep the legal input.
        placed = BoardGraph.from_json(graph.to_json())
        stats.update(reverted=True)
    LAST_STATS.clear()
    LAST_STATS.update(stats)
    return placed


# The last pass's counters (proposals, accepted flips and swaps, best cost), for
# diagnostics and tests.
LAST_STATS: Dict[str, float] = {}
