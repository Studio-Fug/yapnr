"""Joint assignment of every fanned-out pad: negotiated congestion on the model.

Each pad is a **task**. A signal task finds a path from its ball to an exit node
of an allowed side: on the surface layer (rings under ``surface_rings``), or a stub
to a via site of its class and a path on an escape layer (a dog-bone; a filled via
in the ball where the fab allows it). A drop task (a net with a dedicated plane)
finds a stub to a via site of its class; the via reaches the plane.

All tasks negotiate for the same objects (:mod:`.sites`): PathFinder rounds, where an
object another net's objects conflict with costs more each round it stays
contested (present and history costs), until no two nets conflict. Conflicts left
after the last round are removed by priority (the lexicographic objective: most
signals escaped, then most drops, then least length and vias): the task with the
lower priority loses its objects and is tried once more against what is left,
strictly. A task that still has no legal path is ``failed``, with its reason.

Deterministic: tasks, neighbours and heap ties are ordered by name and node; a
``variant`` above 0 permutes the order and adds a small seeded jitter to object
costs (a Monte-Carlo hook).
"""

from __future__ import annotations

import heapq
import math
import random
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .sites import DIRS, Model

STEPS = ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (-1, -1), (-1, 1), (1, -1))


@dataclass
class Task:
    pad: str
    net: str
    kind: str  # "signal" | "drop"
    node: Tuple[int, int]  # the ball's half-lattice node
    ring: int
    width: float
    via_class: int
    exit_layers: Tuple[int, ...] = ()  # plan layer indices where the task may exit
    via_layers: Tuple[int, ...] = ()  # escape layers a dog-bone may hand over to
    priority: int = 0  # 0 signals, 1 drops (lower wins)
    path: Optional[List[Tuple]] = None  # committed objects, in path order
    route: Optional[List[Tuple]] = None  # (layer, node) states, in order, then the exit
    failed: Optional[str] = None
    stats: Dict = field(default_factory=dict)


class Assigner:
    def __init__(
        self,
        model: Model,
        tasks: Sequence[Task],
        *,
        exits: Dict[int, Dict[Tuple[int, int], Tuple[int, int]]],
        via_cost: float = 0.6,
        in_pad_cost: float = 1.5,
        drop_reach: int = 4,
        variant: int = 0,
        max_rounds: int = 40,
    ):
        self.m = model
        self.tasks = list(tasks)
        self.exits = exits  # layer -> {exit node: outward}
        self.via_cost = via_cost
        self.in_pad_cost = in_pad_cost
        self.drop_reach = drop_reach
        self.max_rounds = max_rounds
        self.press: Dict[Tuple, Counter] = defaultdict(Counter)
        self.same: Dict[Tuple, Counter] = defaultdict(Counter)
        self.history: Dict[Tuple, float] = defaultdict(float)
        self.pf = 0.5
        self.variant = variant
        self.rng = random.Random(variant) if variant else None
        self._jitter: Dict[Tuple, float] = {}
        self._cc: Dict[Tuple, Tuple] = {}
        hx, hy = model.lat.px / 2, model.lat.py / 2
        self.length = [math.hypot(dx * hx, dy * hy) for dx, dy in DIRS]
        a0, b0, a1, b1 = model.lat.hull
        k = model.exit_ring
        self.lines = {(-1, 0): a0 - k, (1, 0): a1 + k, (0, -1): b0 - k, (0, 1): b1 + k}
        self.report = dict(rounds=0, conflicts_per_round=[], legalized=[], repaired=[])

    # ------------------------------------------------------------ objects

    def _conflicts(self, obj):
        """Every object position an object ``obj`` excludes for other nets, and the
        via positions it excludes for its own net (drill spacing)."""
        m = self.m
        if obj[0] == "e":
            _, layer, node, d, wc = obj
            ta = ("e", d, wc)
        else:
            _, node, k = obj
            layer = None
            ta = ("v", k)
        other, same = [], []
        for (sa, tb), (offsets, same_offsets) in m.stencils.items():
            if sa != ta:
                continue
            for da, db in offsets:
                at = (node[0] + da, node[1] + db)
                if tb[0] == "v":
                    other.append(("v", at, tb[1]))
                elif layer is not None:
                    other.append(("e", layer, at, tb[1], tb[2]))
                else:
                    other.extend(("e", la, at, tb[1], tb[2]) for la in range(len(m.layers)))
            for da, db in same_offsets:
                same.append(("v", (node[0] + da, node[1] + db), tb[1]))
        return other, same

    def _apply(self, net, objs, sign):
        for obj in objs:
            other, same = self._cache_conflicts(obj)
            for o in other:
                c = self.press[o]
                c[net] += sign
                if c[net] <= 0:
                    del c[net]
            for o in same:
                c = self.same[o]
                c[net] += sign
                if c[net] <= 0:
                    del c[net]

    def _cache_conflicts(self, obj):
        hit = self._cc.get(obj)
        if hit is None:
            hit = self._cc[obj] = self._conflicts(obj)
        return hit

    def present(self, obj, net) -> int:
        c = self.press.get(obj)
        n = 0
        if c:
            n = len(c) - (1 if net in c else 0)
        s = self.same.get(obj)
        if s and s.get(net):
            n += 1
        return n

    def cost(self, obj, base, net, strict=False):
        p = self.present(obj, net)
        if strict and p:
            return None
        j = 1.0
        if self.rng is not None:
            j = self._jitter.get(obj)
            if j is None:
                j = self._jitter[obj] = 1.0 + 0.04 * (self.rng.random() - 0.5)
        return (base * j + self.history.get(obj, 0.0)) * (1.0 + self.pf * p)

    @staticmethod
    def edge(layer, node, step):
        """The canonical edge object for a move ``step`` from ``node``."""
        dx, dy = step
        for d, (ex, ey) in enumerate(DIRS):
            if (dx, dy) == (ex, ey):
                return node, d
            if (dx, dy) == (-ex, -ey):
                return (node[0] + dx, node[1] + dy), d
        raise ValueError(step)

    # ------------------------------------------------------------ search

    def _heuristic(self, task, node):
        if task.kind == "drop":
            return 0.0
        hx, hy = self.m.lat.px / 2, self.m.lat.py / 2
        best = math.inf
        for out, line in self.lines.items():
            d = (line - node[0]) * out[0] * hx if out[0] else (line - node[1]) * out[1] * hy
            best = min(best, max(0.0, d))
        return best

    def search(self, task: Task, strict=False):
        """Least-cost path for ``task``: ``(cost, objects, states)`` or None."""
        m = self.m
        net, w = task.net, task.width
        wc = m.widths.index(round(w, 6))
        start = (0, task.node)
        best = {start: 0.0}
        prev = {}
        heap = [(self._heuristic(task, task.node), 0.0, 0, start)]
        tie = 1
        goal = None
        while heap:
            f, g, _, state = heapq.heappop(heap)
            if g > best.get(state, math.inf) + 1e-12:
                continue
            if state == "GOAL":
                goal = state
                break
            layer, node = state
            moves = []
            # The goal: a via (drops), or an exit node's outward step (signals).
            if layer == 0 and (task.kind == "drop" or task.via_layers):
                via = ("v", node, task.via_class)
                if m.legal(m.via_blockers(node, task.via_class, net), net):
                    in_pad = node == task.node
                    base = self.via_cost + (self.in_pad_cost if in_pad else 0.0)
                    c = self.cost(via, base, net, strict)
                    if c is not None:
                        if task.kind == "drop":
                            moves.append(("GOAL", c, (via,)))
                        else:
                            for lj in task.via_layers:
                                moves.append(((lj, node), c, (via,)))
            if task.kind == "signal" and layer in task.exit_layers:
                out = self.exits.get(layer, {}).get(node)
                if out is not None:
                    o, d = self.edge(layer, node, out)
                    if m.legal(m.edge_blockers(layer, o, d, w), net):
                        obj = ("e", layer, o, d, wc)
                        c = self.cost(obj, self.length[d], net, strict)
                        if c is not None:
                            moves.append(("GOAL", c, (obj,)))
            ring = m.ring_of(node)
            for step in STEPS:
                nxt = (node[0] + step[0], node[1] + step[1])
                if not m.in_region(nxt):
                    continue
                nring = m.ring_of(nxt)
                if nring > m.exit_ring or (nring == m.exit_ring and ring == m.exit_ring):
                    continue  # beyond the exit ring only by an exit step; never along it
                if (
                    task.kind == "drop"
                    and max(abs(nxt[0] - task.node[0]), abs(nxt[1] - task.node[1]))
                    > self.drop_reach
                ):
                    continue
                if m.site_kind(nxt) == "ball" and nxt != task.node and layer == 0:
                    continue  # another ball's land (judged anyway; skip early)
                o, d = self.edge(layer, node, step)
                if not m.legal(m.edge_blockers(layer, o, d, w), net):
                    continue
                obj = ("e", layer, o, d, wc)
                c = self.cost(obj, self.length[d], net, strict)
                if c is None:
                    continue
                moves.append(((layer, nxt), c, (obj,)))
            for nstate, c, objs in moves:
                ng = g + c
                if ng < best.get(nstate, math.inf) - 1e-12:
                    best[nstate] = ng
                    prev[nstate] = (state, objs)
                    h = 0.0 if nstate == "GOAL" else self._heuristic(task, nstate[1])
                    heapq.heappush(heap, (ng + h, ng, tie, nstate))
                    tie += 1
        if goal is None:
            return None
        objs, states = [], []
        s = goal
        while s != start:
            p, o = prev[s]
            objs[:0] = list(o)
            if s != "GOAL":
                states.insert(0, s)
            s = p
        states.insert(0, start)
        return best[goal], objs, states

    # ------------------------------------------------------------ rounds

    def commit(self, task, found):
        if found is None:
            task.path = task.route = None
            return
        _, objs, states = found
        task.path, task.route = objs, states
        self._apply(task.net, objs, +1)

    def ripup(self, task):
        if task.path:
            self._apply(task.net, task.path, -1)
        task.path = task.route = None

    def conflicted(self) -> List[Task]:
        return [t for t in self.tasks if t.path and any(self.present(o, t.net) for o in t.path)]

    def order(self):
        tasks = sorted(self.tasks, key=lambda t: (t.priority, -t.ring, t.pad))
        if self.rng is not None:
            self.rng.shuffle(tasks)
            tasks.sort(key=lambda t: t.priority)
        return tasks

    def run(self):
        unreachable = set()
        for t in self.order():
            found = self.search(t)
            if found is None:
                t.failed = "no legal path or via site (static obstacles)"
                unreachable.add(t.pad)
            self.commit(t, found)
        rounds = 1
        while rounds < self.max_rounds:
            bad = self.conflicted()
            self.report["conflicts_per_round"].append(len(bad))
            if not bad:
                break
            for t in bad:
                for o in t.path:
                    if self.present(o, t.net):
                        self.history[o] += 0.2
            self.pf *= 1.5
            for t in self.order():
                if t in bad:
                    self.ripup(t)
                    self.commit(t, self.search(t))
            rounds += 1
        self.report["rounds"] = rounds
        self._legalize()
        return self.tasks

    def _legalize(self):
        """Drop conflicting tasks by priority, then retry each strictly."""
        while True:
            bad = self.conflicted()
            if not bad:
                break
            loser = max(
                bad,
                key=lambda t: (
                    t.priority,
                    sum(1 for o in t.path if self.present(o, t.net)),
                    t.ring,
                    t.pad,
                ),
            )
            self.ripup(loser)
            loser.failed = "lost its resources to higher-priority pads"
            self.report["legalized"].append(loser.pad)
        for t in sorted(
            (t for t in self.tasks if t.path is None), key=lambda t: (t.priority, -t.ring, t.pad)
        ):
            found = self.search(t, strict=True)
            if found is not None:
                self.commit(t, found)
                t.failed = None
                self.report["repaired"].append(t.pad)
