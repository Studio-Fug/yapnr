"""Bounded witness-growth / reverse-cost track optimizer. Geometry units are mm.

Candidate geometry is never an electrical authority. A board adapter must atomically
refill and run its native/electrical gate before committing a proposed transaction.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import math
import time
from collections import deque
from dataclasses import dataclass, field, replace

from shapely.geometry import LineString, Point, Polygon
from shapely.ops import substring

Point2 = tuple[float, float]
Path = tuple[Point2, ...]
EPS = 1e-8
QUANTUM_MM = 1e-6  # 1 nm, not a routing grid


def snap(p):
    return tuple(round(float(v) / QUANTUM_MM) * QUANTUM_MM for v in p)


def normalize(points) -> Path:
    out = []
    for p in map(snap, points):
        if out and math.dist(out[-1], p) < EPS:
            continue
        while len(out) > 1:
            a, b = out[-2:]
            u, v = (b[0] - a[0], b[1] - a[1]), (p[0] - b[0], p[1] - b[1])
            if abs(u[0] * v[1] - u[1] * v[0]) > EPS or u[0] * v[0] + u[1] * v[1] < 0:
                break
            out.pop()
        out.append(p)
    return tuple(out)


def length(path):
    return sum(math.dist(a, b) for a, b in zip(path, path[1:]))


def turn(a, b, c):
    u, v = (b[0] - a[0], b[1] - a[1]), (c[0] - b[0], c[1] - b[1])
    return math.atan2(abs(u[0] * v[1] - u[1] * v[0]), u[0] * v[0] + u[1] * v[1])


def octilinear(a, b):
    x, y = abs(a[0] - b[0]), abs(a[1] - b[1])
    return min(x, y, abs(x - y)) < EPS


def elbows(a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    d = min(abs(dx), abs(dy))
    sx, sy = (1 if dx >= 0 else -1), (1 if dy >= 0 else -1)
    return (snap((a[0] + sx * d, a[1] + sy * d)), snap((b[0] - sx * d, b[1] - sy * d)))


@dataclass(frozen=True)
class Energy:
    length_weight: float = 1.0  # mm-equivalent / mm
    bend_mm: float = 0.15  # mm-equivalent / nonzero bend
    curvature_mm_per_rad2: float = 0.0

    def __post_init__(self):
        if any(
            not math.isfinite(x) or x < 0
            for x in (self.length_weight, self.bend_mm, self.curvature_mm_per_rad2)
        ):
            raise ValueError("Energy weights must be finite and nonnegative")

    def value(self, path):
        path = normalize(path)
        angles = [turn(a, b, c) for a, b, c in zip(path, path[1:], path[2:])]
        return (
            self.length_weight * length(path)
            + self.bend_mm * sum(a > EPS for a in angles)
            + self.curvature_mm_per_rad2 * sum(a * a for a in angles)
        )


@dataclass(frozen=True)
class Track:
    id: str
    net: str
    points: Path
    width_mm: float = 0.2
    clearance_mm: float = 0.2
    layer: str = "F.Cu"
    protected: bool = False
    pair: str | None = None
    min_length_mm: float = 0.0
    max_length_mm: float = math.inf
    no_length_increase: bool = True
    vias: tuple = ()

    def __post_init__(self):
        if not self.id or not self.net or not self.layer:
            raise ValueError("Track identity, net and layer are required")
        if (
            self.width_mm <= 0
            or not math.isfinite(self.width_mm)
            or self.clearance_mm < 0
            or not math.isfinite(self.clearance_mm)
        ):
            raise ValueError("Invalid width or clearance")
        if any(len(p) != 2 or not all(math.isfinite(v) for v in p) for p in self.points):
            raise ValueError("Finite 2D coordinates required")
        if (
            self.min_length_mm < 0
            or not math.isfinite(self.min_length_mm)
            or self.max_length_mm < self.min_length_mm
            or math.isnan(self.max_length_mm)
        ):
            raise ValueError("Invalid length contract")


@dataclass(frozen=True)
class Obstacle:
    id: str
    shape: object
    radius_mm: float = 0.0  # exact disc/capsule radius for point/line geometry
    clearance_mm: float = 0.2
    layer: str = "F.Cu"
    net: str | None = None
    dependent_plane: bool = False
    protected: bool = False

    def __post_init__(self):
        if not self.id or self.shape.is_empty or not self.shape.is_valid:
            raise ValueError("Valid nonempty obstacle geometry required")
        if any(not math.isfinite(v) or v < 0 for v in (self.radius_mm, self.clearance_mm)):
            raise ValueError("Invalid obstacle radius/clearance")


@dataclass(frozen=True)
class Config:
    growth_mm: float = 1.0
    growth_levels: tuple[float, ...] = (0.25, 0.5, 1.0)
    clearance_epsilon_mm: float = 1e-6
    hysteresis_mm: float = 1e-5
    max_nodes: int = 180
    max_edges_checked: int = 18000
    max_states: int = 12000
    max_transactions: int = 12
    max_attempts: int = 40
    max_group: int = 2
    seconds: float = 30.0
    octilinear_only: bool = True
    preserve_side: bool = True
    spatial_broadphase: bool = False
    spatial_cell_mm: float = 2.0
    spatial_max_cells: int = 4096

    def __post_init__(self):
        if (
            not math.isfinite(self.spatial_cell_mm)
            or self.spatial_cell_mm <= 0
            or self.spatial_max_cells < 1
        ):
            raise ValueError("Invalid spatial broadphase configuration")
        if not math.isfinite(self.seconds) or self.seconds <= 0:
            raise ValueError("A finite positive deadline is required")
        if any(
            not math.isfinite(v) or v < 0
            for v in (self.growth_mm, self.clearance_epsilon_mm, self.hysteresis_mm)
        ):
            raise ValueError("Invalid geometry/acceptance threshold")
        if not self.growth_levels or any(
            not math.isfinite(v) or not 0 < v <= 1 for v in self.growth_levels
        ):
            raise ValueError("Growth fractions must be in (0,1]")
        if tuple(sorted(set(self.growth_levels))) != self.growth_levels:
            raise ValueError("Growth fractions must increase deterministically")
        if any(
            v < 1
            for v in (
                self.max_nodes,
                self.max_edges_checked,
                self.max_states,
                self.max_transactions,
                self.max_attempts,
                self.max_group,
            )
        ):
            raise ValueError("Budgets must be positive")


@dataclass
class Plan:
    track_id: str
    before: Path
    after: Path
    energy_before: float
    energy_after: float
    reason: str
    diagnostics: dict = field(default_factory=dict)


class BudgetExceeded(Exception):
    pass


class Planner:
    def __init__(self, config=Config(), energy=Energy()):
        self.config, self.energy = config, energy

    def plan(
        self,
        track: Track,
        obstacles: list[Obstacle],
        *,
        allow_dependent=False,
        segment_gate=None,
        path_gate=None,
    ):
        cfg = self.config
        old = normalize(track.points)
        before = self.energy.value(old)
        diag = {
            "nodes": 0,
            "edges_checked": 0,
            "states": 0,
            "growth_levels": [],
            "isoline_spans": [],
            "reverse_field": [],
        }

        def result(p, why):
            return Plan(track.id, old, p, before, self.energy.value(p), why, diag)

        if track.protected or track.pair:
            return result(old, "protected_or_pair_requires_group_adapter")
        if len(old) < 2 or not LineString(old).is_simple:
            return result(old, "invalid_witness")
        if cfg.growth_mm <= 0:
            return result(old, "growth_disabled")
        if len({o.id for o in obstacles}) != len(obstacles):
            raise ValueError("Obstacle IDs must be unique")
        relevant = [o for o in obstacles if o.layer == track.layer]
        dependencies = [o for o in relevant if o.dependent_plane]
        if dependencies and not allow_dependent:
            return result(old, "dependent_plane_requires_atomic_gate")
        if any(o.protected for o in dependencies):
            return result(old, "protected_plane")
        fixed = [o for o in relevant if not o.dependent_plane]
        diag["dependent_plane_ids"] = sorted(o.id for o in dependencies)
        witness = LineString(old)
        deadline = time.monotonic() + cfg.seconds
        radii = {
            o.id: track.width_mm / 2 + o.radius_mm + max(track.clearance_mm, o.clearance_mm)
            for o in fixed
        }

        def check_budget():
            if time.monotonic() > deadline:
                raise BudgetExceeded("wall_time_budget")

        def clear(a, b, *, native=True):
            shape = Point(a) if a == b else LineString((a, b))
            if any(shape.distance(o.shape) + EPS < radii[o.id] for o in fixed):
                return False
            return segment_gate(a, b) if native and segment_gate and a != b else True

        if not all(clear(a, b) for a, b in zip(old, old[1:])):
            return result(old, "witness_not_legal")
        if length(old) + EPS < track.min_length_mm or length(old) > track.max_length_mm + EPS:
            return result(old, "witness_outside_length_contract")
        # Candidate contacts come from every relevant obstacle's clearance isoline.
        # Ring vertices are candidate sites only: the graph chooses a finite S->T
        # span, never commits a complete obstacle contour.
        contacts = []
        for o in sorted(fixed, key=lambda x: x.id):
            offset = radii[o.id] + cfg.clearance_epsilon_mm
            ring = o.shape.buffer(offset / math.cos(math.pi / 32), quad_segs=8, join_style=2)
            for poly in getattr(ring, "geoms", (ring,)):
                if isinstance(poly, Polygon):
                    contacts.extend((snap(p), o.id) for p in poly.exterior.coords[:-1])
                    for interior in poly.interiors:
                        contacts.extend((snap(p), o.id) for p in interior.coords[:-1])
        seed = set(old)
        for i, a in enumerate(old):
            for b in old[i + 1 :]:
                seed.update(elbows(a, b))
        # Contact-to-anchor elbows allow octilinear tangent connectors; generation
        # is capped deterministically before any expensive graph expansion.
        contact_seeds = []
        for p, oid in contacts:
            contact_seeds.append((p, oid))
            for a in (old[0], old[-1]):
                contact_seeds.extend((q, oid) for q in elbows(a, p))
        best, best_cost = old, before
        try:
            for fraction in cfg.growth_levels:
                check_budget()
                radius = cfg.growth_mm * fraction
                corridor = witness.buffer(radius, quad_segs=16)
                candidates = set(seed)
                labels = {}
                for p, oid in contact_seeds:
                    if witness.distance(Point(p)) <= radius + EPS:
                        candidates.add(p)
                        labels.setdefault(p, set()).add(oid)
                # Keep witness, then near-witness sites ordered by geometry. No
                # dependence on input obstacle order, Python hash or wall clock.
                ordered = sorted(
                    candidates,
                    key=lambda p: (
                        0 if p in old else 1 if p in seed else 2,
                        witness.distance(Point(p)),
                        p,
                    ),
                )
                nodes = []
                for p in ordered:
                    if not corridor.covers(Point(p)) or not clear(p, p):
                        continue
                    s = witness.project(Point(p))
                    foot = tuple(witness.interpolate(s).coords[0])
                    if not clear(p, foot, native=False):
                        continue
                    nodes.append((s, p))
                    if len(nodes) >= cfg.max_nodes:
                        diag["node_cap_hit"] = len(ordered) > cfg.max_nodes
                        break
                nodes.sort()
                if old[0] not in [p for _, p in nodes] or old[-1] not in [p for _, p in nodes]:
                    return result(best, "anchor_missing")
                diag["nodes"] = max(diag["nodes"], len(nodes))
                edges = [[] for _ in nodes]
                start = next(i for i, (_, p) in enumerate(nodes) if p == old[0])
                goal = next(i for i, (_, p) in enumerate(nodes) if p == old[-1])
                for i, (s, a) in enumerate(nodes):
                    check_budget()
                    for j in range(i + 1, len(nodes)):
                        t, b = nodes[j]
                        if t - s <= EPS or (cfg.octilinear_only and not octilinear(a, b)):
                            continue
                        diag["edges_checked"] += 1
                        if diag["edges_checked"] > cfg.max_edges_checked:
                            raise BudgetExceeded("edge_budget")
                        seg = LineString((a, b))
                        if not corridor.covers(seg) or not clear(a, b):
                            continue
                        if cfg.preserve_side:
                            # Growth spokes retract to a contiguous witness span.
                            # Its swept polygon must avoid all fixed copper. This
                            # is conservative, and disallows jumping obstacle sides.
                            reference = list(substring(witness, s, t).coords)
                            swept = Polygon(reference + [b, a]).buffer(0)
                            if not swept.is_empty and any(swept.intersects(o.shape) for o in fixed):
                                continue
                        edges[i].append((j, math.dist(a, b)))
                # Reverse goal-counting tree: shortest remaining LENGTH on this
                # directed finite roadmap. Admissible for nonnegative bend terms.
                reverse = [math.inf] * len(nodes)
                reverse[goal] = 0
                tree = {}
                for i in range(goal - 1, -1, -1):
                    options = [
                        (d + reverse[j], nodes[j][1], j)
                        for j, d in edges[i]
                        if math.isfinite(reverse[j])
                    ]
                    if options:
                        reverse[i], _, tree[i] = min(options)
                diag["growth_levels"].append(
                    {
                        "radius_mm": radius,
                        "nodes": len(nodes),
                        "reachable": math.isfinite(reverse[start]),
                    }
                )
                if not math.isfinite(reverse[start]):
                    continue
                # Incoming-edge state preserves bend/curvature costs. This is an
                # S->T path search, not a Steiner tree or a literal field PDE.
                heap = [(reverse[start] * self.energy.length_weight, 0.0, (start,), -1, start)]
                costs = {(-1, start): 0.0}
                chosen = None
                while heap:
                    check_budget()
                    _, cost, indices, prev, i = heapq.heappop(heap)
                    if cost > costs.get((prev, i), math.inf) + EPS:
                        continue
                    diag["states"] += 1
                    if diag["states"] > cfg.max_states:
                        raise BudgetExceeded("state_budget")
                    if i == goal:
                        candidate = normalize(nodes[k][1] for k in indices)
                        L = length(candidate)
                        if L + EPS < track.min_length_mm or L > track.max_length_mm + EPS:
                            continue
                        if track.no_length_increase and L > length(old) + EPS:
                            continue
                        if path_gate and not path_gate(candidate):
                            continue
                        chosen = candidate
                        break
                    for j, d in edges[i]:
                        if not math.isfinite(reverse[j]):
                            continue
                        angle = 0 if prev < 0 else turn(nodes[prev][1], nodes[i][1], nodes[j][1])
                        if angle > math.pi / 2 + EPS:
                            continue
                        add = (
                            self.energy.length_weight * d
                            + (self.energy.bend_mm if angle > EPS else 0)
                            + self.energy.curvature_mm_per_rad2 * angle * angle
                        )
                        nc = cost + add
                        key = (i, j)
                        if nc + EPS < costs.get(key, math.inf):
                            costs[key] = nc
                            heapq.heappush(
                                heap,
                                (
                                    nc + self.energy.length_weight * reverse[j],
                                    nc,
                                    indices + (j,),
                                    i,
                                    j,
                                ),
                            )
                if chosen and self.energy.value(chosen) < best_cost - cfg.hysteresis_mm:
                    best, best_cost = chosen, self.energy.value(chosen)
                    diag["reverse_field"] = [
                        {
                            "point": p,
                            "remaining_length_mm": reverse[i],
                            "next": nodes[tree[i]][1] if i in tree else None,
                        }
                        for i, (_, p) in enumerate(nodes)
                        if math.isfinite(reverse[i])
                    ]
                    diag["isoline_spans"] = [
                        {"point": p, "obstacle_ids": sorted(labels[p])} for p in best if p in labels
                    ]
        except BudgetExceeded as e:
            diag["budget_stop"] = str(e)
        return result(
            best, "improved" if best != old else diag.get("budget_stop", "no_improvement")
        )


@dataclass
class GateResult:
    accepted: bool
    reason: str = ""
    dirty_region: object | None = None
    checks: dict = field(default_factory=dict)
    dirty_layers: tuple[str, ...] | None = None
    updated_obstacles: tuple[Obstacle, ...] = ()


class Controller:
    """Bounded atomic groups with deterministic dirty-neighbor requeue.

    `gate(before, proposed, moved_ids)` must return ONLY after joint refill/native
    and electrical validation. Any refusal or exception leaves state unchanged.
    Pair/protected geometry remains locked in this first prototype.
    """

    def __init__(self, tracks, obstacles=(), config=Config(), energy=Energy(), gate=None):
        self.tracks = {t.id: replace(t, points=normalize(t.points)) for t in tracks}
        if len(self.tracks) != len(tracks):
            raise ValueError("Track IDs must be unique")
        self.obstacles, self.config, self.energy = list(obstacles), config, energy
        self.gate = gate
        self.events = []
        self.seen = {self.fingerprint(self.tracks)}
        self._track_shapes = {i: LineString(t.points) for i, t in self.tracks.items()}
        self._obstacle_by_id = {o.id: o for o in self.obstacles}
        if len(self._obstacle_by_id) != len(self.obstacles):
            raise ValueError("Obstacle IDs must be unique")
        self._max_dirty_margin = 2 * max(
            (t.clearance_mm + t.width_mm for t in self.tracks.values()), default=0
        )
        self._spatial = None
        if config.spatial_broadphase:
            from pnr.energy_track_spatial import LayerGrid

            self._spatial = LayerGrid(config.spatial_cell_mm, config.spatial_max_cells)
            for i, t in sorted(self.tracks.items()):
                self._index_track(i, t)
            for o in self.obstacles:
                self._index_obstacle(o)

    @staticmethod
    def fingerprint(tracks):
        return hashlib.sha256(
            json.dumps(
                [
                    (
                        i,
                        t.net,
                        t.layer,
                        round(t.width_mm / QUANTUM_MM),
                        t.vias,
                        [[round(v / QUANTUM_MM) for v in p] for p in t.points],
                    )
                    for i, t in sorted(tracks.items())
                ]
            ).encode()
        ).hexdigest()

    def _index_track(self, i, t):
        if self._spatial is None:
            return
        from pnr.energy_track_spatial import expand

        self._spatial.upsert(
            "track:" + i,
            t.layer,
            expand(self._track_shapes[i].bounds, t.width_mm / 2 + t.clearance_mm),
        )

    def _index_obstacle(self, o):
        if self._spatial is None:
            return
        from pnr.energy_track_spatial import expand

        self._spatial.upsert(
            "obstacle:" + o.id, o.layer, expand(o.shape.bounds, o.radius_mm + o.clearance_mm)
        )

    def environment(self, track_id, state=None, changed_ids=None):
        state = self.tracks if state is None else state
        t = state[track_id]
        if self._spatial is None:
            candidate_tracks = sorted(state)
            candidate_obstacles = self.obstacles
        else:
            from pnr.energy_track_spatial import expand

            # Every admitted route lies in this grown witness corridor. Width
            # plus sum of clearances conservatively bounds the exact max rule.
            query = expand(
                LineString(t.points).bounds, self.config.growth_mm + t.width_mm / 2 + t.clearance_mm
            )
            hits = self._spatial.query(query, (t.layer,))
            candidate_tracks = {h[6:] for h in hits if h.startswith("track:")}
            candidate_obstacles = [
                self._obstacle_by_id[h[9:]] for h in hits if h.startswith("obstacle:")
            ]
            if state is not self.tracks:
                # Atomic groups supply the small changed set. Arbitrary external
                # snapshots use an O(N) fallback rather than risk stale misses.
                if changed_ids is None:
                    changed_ids = [i for i, v in state.items() if v != self.tracks.get(i)]
                candidate_tracks.update(changed_ids)
            candidate_tracks = sorted(candidate_tracks)
        out = list(candidate_obstacles)
        for i in candidate_tracks:
            if i == track_id:
                continue
            neighbor = state[i]
            if self._spatial is not None and neighbor.layer != t.layer:
                continue
            geom = (
                self._track_shapes[i]
                if neighbor is self.tracks.get(i)
                else LineString(neighbor.points)
            )
            out.append(
                Obstacle(
                    i,
                    geom,
                    neighbor.width_mm / 2,
                    neighbor.clearance_mm,
                    neighbor.layer,
                    neighbor.net,
                    protected=neighbor.protected,
                )
            )
        return out

    def dirty_neighbors(self, changed_geometry, layer, verdict, exclude=(), queued=()):
        dirty = changed_geometry.buffer(self.config.growth_mm + self._max_dirty_margin)
        layers = (layer,)
        if verdict.dirty_region is not None:
            dirty = dirty.union(verdict.dirty_region)
            # Unknown refill-layer scope conservatively searches all layers.
            layers = (
                None
                if verdict.dirty_layers is None
                else tuple(sorted({layer} | set(verdict.dirty_layers)))
            )
        if self._spatial is None:
            ids = sorted(self.tracks)
        else:
            ids = [
                h[6:] for h in self._spatial.query(dirty.bounds, layers) if h.startswith("track:")
            ]
        forbidden = set(exclude) | set(queued)
        return [
            i
            for i in ids
            if i not in forbidden
            and (layers is None or self.tracks[i].layer in layers)
            and self._track_shapes[i].intersects(dirty)
        ]

    def transact(self, replacements: dict[str, Path]):
        cfg = self.config
        ids = sorted(replacements)
        if not ids or len(ids) > cfg.max_group:
            return GateResult(False, "group_budget")
        if len(ids) > 1 and not self.gate:
            return GateResult(False, "group_requires_atomic_gate")
        before = self.tracks
        trial = dict(before)
        for i in ids:
            if i not in before:
                return GateResult(False, "unknown_track")
            t = before[i]
            p = normalize(replacements[i])
            if t.protected or t.pair:
                return GateResult(False, "protected_or_pair_requires_group_adapter")
            if len(p) < 2 or p[0] != t.points[0] or p[-1] != t.points[-1]:
                return GateResult(False, "anchors")
            if not LineString(p).is_simple:
                return GateResult(False, "self_intersection")
            if any(not octilinear(a, b) for a, b in zip(p, p[1:])) and cfg.octilinear_only:
                return GateResult(False, "angles")
            if any(turn(a, b, c) > math.pi / 2 + EPS for a, b, c in zip(p, p[1:], p[2:])):
                return GateResult(False, "acute_turn")
            L = length(p)
            if (
                L + EPS < t.min_length_mm
                or L > t.max_length_mm + EPS
                or (t.no_length_increase and L > length(t.points) + EPS)
            ):
                return GateResult(False, "length_contract")
            trial[i] = replace(t, points=p)
        gain = sum(
            self.energy.value(before[i].points) - self.energy.value(trial[i].points) for i in ids
        )
        if gain <= cfg.hysteresis_mm:
            return GateResult(False, "objective_hysteresis")
        signature = self.fingerprint(trial)
        if signature in self.seen:
            return GateResult(False, "cycle")
        for i in ids:
            t = trial[i]
            path = LineString(t.points)
            if not LineString(before[i].points).buffer(cfg.growth_mm, quad_segs=16).covers(path):
                return GateResult(False, "growth_locality")
            for o in self.environment(i, trial, changed_ids=ids):
                if o.layer != t.layer or o.dependent_plane:
                    continue
                threshold = t.width_mm / 2 + o.radius_mm + max(t.clearance_mm, o.clearance_mm)
                if path.distance(o.shape) + EPS < threshold:
                    return GateResult(False, "clearance:" + o.id)
                if cfg.preserve_side and o.id not in ids:
                    sweep = Polygon(list(before[i].points) + list(reversed(t.points))).buffer(0)
                    if not sweep.is_empty and sweep.intersects(o.shape):
                        return GateResult(False, "fixed_obstacle_side:" + o.id)
        dependent = any(o.dependent_plane for o in self.obstacles)
        if dependent and not self.gate:
            return GateResult(False, "dependent_plane_requires_atomic_gate")
        if any(o.dependent_plane and o.protected for o in self.obstacles):
            return GateResult(False, "protected_plane")
        try:
            verdict = (
                self.gate(before, trial, ids)
                if self.gate
                else GateResult(True, "geometry_only", checks={"native": False})
            )
        except Exception as e:
            return GateResult(False, "gate_exception:" + type(e).__name__)
        if not verdict.accepted:
            return verdict
        if dependent:
            required = (
                "refill",
                "native_drc",
                "connectivity",
                "pair_skew",
                "length",
                "power_ir",
                "reference",
                "protected_macros",
            )
            if any(verdict.checks.get(k) is not True for k in required):
                return GateResult(False, "incomplete_dependent_plane_gate")
        if any(o.id not in self._obstacle_by_id for o in verdict.updated_obstacles):
            return GateResult(False, "unknown_obstacle_update")
        if len({o.id for o in verdict.updated_obstacles}) != len(verdict.updated_obstacles):
            return GateResult(False, "duplicate_obstacle_update")
        if any(self._obstacle_by_id[o.id].protected for o in verdict.updated_obstacles):
            return GateResult(False, "protected_obstacle_update")
        dirty = verdict.dirty_region
        dirty_layers = (
            None
            if dirty is not None and verdict.dirty_layers is None
            else set(verdict.dirty_layers or ())
        )
        for o in verdict.updated_obstacles:
            previous = self._obstacle_by_id[o.id]
            keys = ("layer", "net", "radius_mm", "clearance_mm", "dependent_plane", "protected")
            if any(getattr(previous, k) != getattr(o, k) for k in keys):
                return GateResult(False, "obstacle_update_metadata")
            affected = previous.shape.symmetric_difference(o.shape)
            if not affected.is_empty:
                affected = affected.buffer(o.radius_mm + o.clearance_mm + EPS)
                dirty = affected if dirty is None else dirty.union(affected)
                if dirty_layers is not None:
                    dirty_layers.add(o.layer)
        if verdict.updated_obstacles:
            verdict = replace(
                verdict,
                dirty_region=dirty,
                dirty_layers=None if dirty_layers is None else tuple(sorted(dirty_layers)),
            )
        self.tracks = trial
        self.seen.add(signature)
        for i in ids:
            self._track_shapes[i] = LineString(trial[i].points)
            self._index_track(i, trial[i])
        for o in verdict.updated_obstacles:
            self._obstacle_by_id[o.id] = o
            self._index_obstacle(o)
        if verdict.updated_obstacles:
            self.obstacles = [self._obstacle_by_id[o.id] for o in self.obstacles]

        self.events.append(
            {
                "kind": "accepted",
                "ids": ids,
                "gain_mm_equivalent": gain,
                "checks": verdict.checks,
                "before": {i: before[i].points for i in ids},
                "after": {i: trial[i].points for i in ids},
            }
        )
        return verdict

    def run(self):
        queue = deque(sorted(self.tracks))
        queued = set(queue)
        attempts = transactions = 0
        deadline = time.monotonic() + self.config.seconds
        while (
            queue
            and attempts < self.config.max_attempts
            and transactions < self.config.max_transactions
            and time.monotonic() < deadline
        ):
            i = queue.popleft()
            queued.remove(i)
            attempts += 1
            plan = Planner(
                replace(self.config, seconds=max(0.001, deadline - time.monotonic())), self.energy
            ).plan(self.tracks[i], self.environment(i), allow_dependent=self.gate is not None)
            self.events.append(
                {
                    "kind": "planned",
                    "id": i,
                    "reason": plan.reason,
                    "energy_before": plan.energy_before,
                    "energy_after": plan.energy_after,
                }
            )
            if plan.after == plan.before:
                continue
            verdict = self.transact({i: plan.after})
            if not verdict.accepted:
                self.events.append({"kind": "rejected", "id": i, "reason": verdict.reason})
                continue
            transactions += 1
            requeued = self.dirty_neighbors(
                LineString(plan.before).union(LineString(plan.after)),
                self.tracks[i].layer,
                verdict,
                exclude=(i,),
                queued=queued,
            )
            for j in requeued:
                queue.append(j)
                queued.add(j)
            self.events.append({"kind": "requeue", "after": i, "ids": requeued})
        stop = (
            "converged"
            if not queue
            else (
                "attempt_budget"
                if attempts >= self.config.max_attempts
                else (
                    "transaction_budget"
                    if transactions >= self.config.max_transactions
                    else "wall_time_budget"
                )
            )
        )
        return {
            "stop": stop,
            "attempts": attempts,
            "transactions": transactions,
            "events": self.events,
            "tracks": {i: t.points for i, t in sorted(self.tracks.items())},
            "spatial": dict(self._spatial.stats) if self._spatial is not None else None,
        }
