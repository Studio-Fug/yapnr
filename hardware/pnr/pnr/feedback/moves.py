"""Placement children from a parent's failed connections, and the matched control.

PULL (:func:`pull_children`): for the parent's failed cross-part connections in
order of weight ``min(5, 1 + 2 * lineage failures + [power] + [no_make_room])``
(then id), move ONE non-anchor end (the one with fewer pads; one child per
mover) on a 0.25 mm lattice within ``max_move`` (tier-1 power parts only for a
tier-1 to tier-1 connection, and no pad of theirs moves more than 1 mm), at any cardinal rotation the
constraints allow. The cost is the weighted length of the mover's failed
connections plus 0.5 x any growth of its other pads' nearest same-net peer
distance (plane nets excluded); the target connection must shorten by at least
0.25 mm, the cost must drop, and the mover stays within ``lineage_cap`` of its
generation-0 pose. Every other part keeps its exact parent pose, so a child is
local by construction (no re-legalization).

RAND (:func:`rand_child`): a seeded non-anchor unit moved by a given
displacement in a seeded direction (and seeded rotation when the sibling PULL
rotated), under the same legality rules: the control that decides whether
feedback beats perturbation.

A unit is a single part, or (hierarchical top level) a whole library block
moved rigidly by translation. Legality: the mover's courtyards (with plane-array
reservations) clear every other part by the board clearance, stay inside the
outline, out of keep-outs and within hard group radii; then the full
:func:`pnr.place.metrics.hard_violations` of the child may add nothing; then the
driver's ``extra_check`` and (PNR_POWER_FIRST=1) the power guard (no new power
crossings, q_band at most one band worse).
"""
from __future__ import annotations

import fnmatch
import hashlib
import math
import random
from dataclasses import dataclass, field
from typing import Callable, Dict, FrozenSet, Optional, Tuple

import numpy as np

from pnr.feedback.signals import POWER_MODES
from pnr.place.geometry import (courtyard_rect, hard_group_edges, keepout_rects, outline_size, placement_rects,
                                resolve_fixed_poses, resolve_hard_rotations)

MAX_MOVE_MM = 3.0
TIER1_MAX_MOVE_MM = 1.0
GRID_MM = 0.25
LINEAGE_CAP_MM = 4.0
MIN_SHORTEN_MM = 0.25
WEIGHT_CAP = 5.0
OTHER_PAD_PENALTY = 0.5
MAX_LEGAL_CHECKS = 400
ANGLES = (0.0, 90.0, 180.0, 270.0)


def seed_for(*parts) -> int:
    """Deterministic 31-bit seed from the lineage coordinates."""
    return int(hashlib.sha256(':'.join(map(str, parts)).encode()).hexdigest(), 16) % 2 ** 31


def conn_weight(conn, table=None, node_id=None) -> float:
    lineage = table.lineage_fails(conn['id'], node_id) if table is not None and node_id is not None else 1
    lineage = max(1, lineage)       # the parent's own failure always counts
    return min(WEIGHT_CAP, 1 + 2 * lineage + (conn.get('mode') in POWER_MODES) + bool(conn.get('no_room')))


def hubs(graph, blocks):
    """The part with the most pads in each block (ties by ref), as extract_blocks names blocks."""
    by_ref = {c.ref: c for c in graph.components}
    return {max((r for r in b.refs if r in by_ref), key=lambda r: (len(by_ref[r].pads), r))
            for b in blocks if any(r in by_ref for r in b.refs)}


def default_anchors(graph, constraints, hub_refs=()):
    """Parts no move may touch: hubs, fixed/locked/source-locked parts and board-level
    interface parts (rows, edge alignment)."""
    fixed = set(resolve_fixed_poses(graph, constraints)) | set(constraints.locked_refs)
    fixed |= {c.ref for c in graph.components if c.locked}
    fixed |= {r for con in constraints.constraints if con.kind in ('row', 'fixed', 'edge_align') for r in con.refs}
    return frozenset(fixed | set(hub_refs))


def plane_nets(graph, constraints):
    pats = [p for nc in constraints.net_classes if nc.plane_layer for p in nc.nets]
    return frozenset(n.name for n in graph.nets if any(fnmatch.fnmatch(n.name, p) for p in pats))


def _norm(angle):
    return float(round(float(angle)) % 360)


def _rot(x, y, deg):
    t = math.radians(deg)
    c, s = math.cos(t), math.sin(t)
    return x * c - y * s, x * s + y * c


@dataclass
class MoveBoard:
    """A parent pose plus what may move and how legality is judged.

    The graph is mutated in place while candidates are checked and always
    restored; children are returned as ``{ref: [x, y, rot, side]}`` for the moved
    refs only.
    """
    graph: object
    constraints: object
    key_of: Dict[str, str]
    anchors: FrozenSet[str]
    units: Optional[Dict[str, Tuple[str, ...]]] = None
    tier1: FrozenSet[str] = frozenset()
    origin: Optional[Dict[str, Tuple[float, float]]] = None
    clearance: float = 0.0
    plane: FrozenSet[str] = frozenset()
    extra_check: Optional[Callable] = None      # f(graph, moved {ref: pose}) -> [problems]
    guard: Optional[Callable] = None            # f(graph) -> reason or None
    grid: float = GRID_MM
    stats: dict = field(default_factory=dict)

    def __post_init__(self):
        g = self.graph
        self.by_ref = {c.ref: c for c in g.components}
        if self.units is None:
            self.units = {r: (r,) for r in sorted(self.by_ref)}
        self.unit_of = {r: u for u, refs in self.units.items() for r in refs}
        for r in self.by_ref:
            if r not in self.unit_of:
                self.units[r] = (r,)
                self.unit_of[r] = r
        self.ref_of = {k: r for r, k in self.key_of.items()}
        self.origin = dict(self.origin or {})
        for r, c in self.by_ref.items():
            self.origin.setdefault(r, tuple(c.pos))
        self.width, self.height = outline_size(g, self.constraints)
        poses = resolve_fixed_poses(g, self.constraints)
        self.keepouts = keepout_rects(g, self.constraints, poses)
        self.rot_req = resolve_hard_rotations(self.constraints)
        self.edges = hard_group_edges(self.constraints)
        self.locked = set(self.constraints.locked_refs)
        self.rects = {r: placement_rects(c) for r, c in self.by_ref.items()}
        from pnr.place.metrics import hard_violations
        self._hard = hard_violations
        self.baseline = self._violations()
        self.pads_by_net = {}
        for c in g.components:
            for p in c.pads:
                if p.net:
                    self.pads_by_net.setdefault(p.net, []).append((c.ref, p.name))

    # ------------------------------------------------------------ helpers
    def _violations(self):
        return {k: {repr(v) for v in vs} for k, vs in self._hard(self.graph, self.constraints).items()}

    def movable(self, unit):
        return not (set(self.units[unit]) & self.anchors)

    def n_pads(self, unit):
        return sum(len(self.by_ref[r].pads) for r in self.units[unit])

    def is_tier1(self, unit):
        return any(r in self.tier1 for r in self.units[unit])

    def pad_xy(self, ref, name):
        c = self.by_ref[ref]
        for p in c.pads:
            if p.name == name:
                ox, oy = _rot(p.offset[0], p.offset[1], c.rot)
                return c.pos[0] + ox, c.pos[1] + oy
        return None

    def end_ref(self, end):
        key, _ = end
        return self.ref_of.get(key, key if key in self.by_ref else None)

    def pose(self, ref):
        c = self.by_ref[ref]
        return [c.pos[0], c.pos[1], _norm(c.rot), c.side]

    # ------------------------------------------------------------ legality
    def _local(self, refs):
        inside = set(refs)
        for ref in refs:
            comp = self.by_ref[ref]
            want = self.rot_req.get(ref)
            if want is not None and abs((comp.rot - want + 180) % 360 - 180) > 1e-6:
                return 'rotation'
            rect = courtyard_rect(comp)
            if ref not in self.locked and not rect.inside(self.width, self.height):
                return 'outline'
            if any(rect.overlaps(k, gap=self.clearance) for k in self.keepouts):
                return 'keepout'
            for anchor, member, radius in self.edges:
                if ref in (anchor, member):
                    other = self.by_ref.get(member if ref == anchor else anchor)
                    if other is not None and math.dist(comp.pos, other.pos) > radius + 1e-9:
                        return 'group'
            for side, area in placement_rects(comp):
                for other, regions in self.rects.items():
                    if other in inside:
                        continue
                    for other_side, rect2 in regions:
                        if other_side == side and area.overlaps(rect2, gap=self.clearance):
                            return 'overlap'
        return None

    def _apply(self, refs, dx, dy, rot):
        saved = {r: (self.by_ref[r].pos, self.by_ref[r].rot) for r in refs}
        for r in refs:
            c = self.by_ref[r]
            c.pos = (c.pos[0] + dx, c.pos[1] + dy)
            if rot is not None:
                c.rot = rot
        return saved

    def _restore(self, saved):
        for r, (pos, rot) in saved.items():
            self.by_ref[r].pos, self.by_ref[r].rot = pos, rot

    def check(self, refs, dx, dy, rot):
        """None when the unit moved by (dx, dy[, to rot]) is legal, else the reason."""
        saved = self._apply(refs, dx, dy, rot)
        try:
            why = self._local(refs)
            if why:
                return why
            now = self._violations()
            added = {k: v - self.baseline.get(k, set()) for k, v in now.items()}
            added = {k: v for k, v in added.items() if v}
            if added:
                return 'hard:' + ','.join(sorted(added))
            moved = {r: self.pose(r) for r in refs}
            if self.extra_check is not None:
                problems = self.extra_check(self.graph, moved)
                if problems:
                    return 'extra:' + str(problems)[:200]
            if self.guard is not None:
                why = self.guard(self.graph)
                if why:
                    return 'guard:' + why
            return None
        finally:
            self._restore(saved)

    def within_lineage(self, refs, dx, dy, cap):
        return all(math.dist(self.origin[r], (self.by_ref[r].pos[0] + dx, self.by_ref[r].pos[1] + dy)) <= cap + 1e-9
                   for r in refs)

    def rotations(self, unit):
        refs = self.units[unit]
        if len(refs) != 1:
            return [None]
        ref = refs[0]
        if ref in self.rot_req:
            return [None]
        cur = _norm(self.by_ref[ref].rot)
        return [None] + [a for a in ANGLES if abs(a - cur) > 1e-6]

    def lattice(self, max_move):
        n = int(math.floor(max_move / self.grid + 1e-9))
        pts = [(i * self.grid, j * self.grid) for i in range(-n, n + 1) for j in range(-n, n + 1)
               if math.hypot(i, j) * self.grid <= max_move + 1e-9]
        return np.array(pts, dtype=float).reshape(-1, 2)

    # ------------------------------------------------------------ PULL
    def _unit_pad_table(self, unit):
        """[(ref, pad name, net, base xy, local offset)] for the unit's pads."""
        out = []
        for r in self.units[unit]:
            c = self.by_ref[r]
            for p in c.pads:
                ox, oy = _rot(p.offset[0], p.offset[1], c.rot)
                out.append((r, p.name, p.net, (c.pos[0] + ox, c.pos[1] + oy), tuple(p.offset)))
        return out

    def _positions(self, unit, pads, cand):
        """(M, P, 2) pad positions for candidates [(dx, dy, rot or None)]."""
        refs = self.units[unit]
        base = np.array([p[3] for p in pads], dtype=float)                   # (P, 2)
        d = np.array([[c[0], c[1]] for c in cand], dtype=float)              # (M, 2)
        pos = base[None, :, :] + d[:, None, :]
        if len(refs) == 1:
            comp = self.by_ref[refs[0]]
            off = np.array([p[4] for p in pads], dtype=float)
            cur = np.array([_rot(o[0], o[1], comp.rot) for o in off]).reshape(-1, 2)
            for m, c in enumerate(cand):
                if c[2] is not None:
                    new = np.array([_rot(o[0], o[1], c[2]) for o in off]).reshape(-1, 2)
                    pos[m] += new - cur
        return pos

    def failed_for(self, unit, conns):
        """[(conn, own end, peer end)] of the connections with exactly one end in the unit."""
        inside = set(self.units[unit])
        out = []
        for c in conns:
            ra, rb = self.end_ref(c['a']), self.end_ref(c['b'])
            if ra is None or rb is None:
                continue
            if (ra in inside) == (rb in inside):
                continue
            mine, peer = (c['a'], c['b']) if ra in inside else (c['b'], c['a'])
            out.append((c, mine, peer))
        return out

    def search(self, unit, conns_w, target, max_move, lineage_cap, skip=None, pad_limit=None):
        """Best legal PULL pose of ``unit`` for ``target``; None when there is none.

        ``skip(poses)`` true for a child already produced (an earlier generation
        of the same parent) moves on to the next-best legal pose. ``pad_limit``
        bounds every pad's displacement (tier-1 power parts: rotations included)."""
        pads = self._unit_pad_table(unit)
        index = {(r, n): i for i, (r, n, _, _, _) in enumerate(pads)}
        terms, target_i = [], None
        for c, w in conns_w:
            mine = c['_mine']
            peer = self.pad_xy(self.end_ref(c['_peer']), c['_peer'][1])
            i = index.get((self.end_ref(mine), mine[1]))
            if peer is None or i is None:
                continue
            if c['id'] == target['id']:
                target_i = len(terms)
            terms.append((i, np.array(peer), w))
        if target_i is None:
            return None
        failed_pads = {t[0] for t in terms}
        inside = set(self.units[unit])
        others = []
        for i, (r, name, net, xy, _) in enumerate(pads):
            if i in failed_pads or not net or net in self.plane:
                continue
            peers = [self.pad_xy(pr, pn) for pr, pn in self.pads_by_net.get(net, []) if pr not in inside]
            peers = [q for q in peers if q is not None]
            if peers:
                others.append((i, np.array(peers, dtype=float)))
        lat = self.lattice(max_move)
        cand = [(float(dx), float(dy), rot) for rot in self.rotations(unit) for dx, dy in lat
                if not (rot is None and abs(dx) < 1e-9 and abs(dy) < 1e-9)]
        if not cand:
            return None
        ident = self._positions(unit, pads, [(0.0, 0.0, None)])[0]
        pos = self._positions(unit, pads, cand)

        def fail_cost(p):
            per = [np.linalg.norm(p[..., i, :] - q, axis=-1) for i, q, _ in terms]
            return sum(w * d for (_, _, w), d in zip(terms, per)), per[target_i]

        def other_len(p):
            total = 0.0
            for i, q in others:
                total = total + np.min(np.linalg.norm(p[..., i, None, :] - q, axis=-1), axis=-1)
            return total

        c0, t0 = fail_cost(ident)
        o0 = other_len(ident)
        c1, t1 = fail_cost(pos)
        o1 = other_len(pos)
        cost = c1 + OTHER_PAD_PENALTY * np.maximum(0.0, o1 - o0)
        shorten = t0 - t1
        moves = np.hypot([c[0] for c in cand], [c[1] for c in cand])
        pad_move = np.max(np.linalg.norm(pos - ident[None], axis=-1), axis=-1)
        order = sorted(range(len(cand)), key=lambda m: (round(float(cost[m]), 9), round(float(moves[m]), 9),
                                                       cand[m][0], cand[m][1],
                                                       -1.0 if cand[m][2] is None else cand[m][2]))
        refs = self.units[unit]
        checked = 0
        for m in order:
            if cost[m] >= c0 - 1e-9:
                break
            if shorten[m] < MIN_SHORTEN_MM - 1e-9:
                continue
            if pad_limit is not None and pad_move[m] > pad_limit + 1e-9:
                continue
            dx, dy, rot = cand[m]
            if not self.within_lineage(refs, dx, dy, lineage_cap):
                continue
            checked += 1
            if checked > MAX_LEGAL_CHECKS:
                break
            why = self.check(refs, dx, dy, rot)
            tally = (why or 'legal').split(':')[0]
            self.stats[tally] = self.stats.get(tally, 0) + 1
            if why is None:
                poses = self.poses_after(unit, dx, dy, rot)
                if skip is not None and skip(poses):
                    self.stats['seen'] = self.stats.get('seen', 0) + 1
                    continue
                old_rot = _norm(self.by_ref[refs[0]].rot) if len(refs) == 1 else 0.0
                return dict(poses=poses, dx=dx, dy=dy, rot=rot, move_mm=float(moves[m]),
                            pad_move_mm=float(pad_move[m]), shortening_mm=float(shorten[m]),
                            cost_before=float(c0), cost_after=float(cost[m]),
                            drot=0.0 if rot is None else _norm(rot - old_rot), checked=checked)
        return None

    def poses_after(self, unit, dx, dy, rot):
        saved = self._apply(self.units[unit], dx, dy, rot)
        try:
            return {r: self.pose(r) for r in self.units[unit]}
        finally:
            self._restore(saved)


def pull_children(board: MoveBoard, fb, table=None, node_id=None, n=2, *, max_move=MAX_MOVE_MM,
                  lineage_cap=LINEAGE_CAP_MM, tier1_max_move=TIER1_MAX_MOVE_MM, skip=None):
    """Up to ``n`` PULL children (distinct movers), in target order.

    ``skip(poses)``: children already evaluated elsewhere are passed over.
    Returns [dict(poses={ref: [x, y, rot, side]}, detail={...})]."""
    conns = [c for c in (fb or {}).get('conns') or [] if table is None or not table.is_floor(c['id'])]
    weighted = sorted(((conn_weight(c, table, node_id), c) for c in conns), key=lambda x: (-x[0], x[1]['id']))
    used, out = set(), []
    for w, target in weighted:
        if len(out) >= n:
            break
        ra, rb = board.end_ref(target['a']), board.end_ref(target['b'])
        if ra is None or rb is None:
            continue
        ua, ub = board.unit_of[ra], board.unit_of[rb]
        if ua == ub:
            continue
        both_tier1 = board.is_tier1(ua) and board.is_tier1(ub)
        ends = []
        for u in (ua, ub):
            if not board.movable(u) or u in used:
                continue
            if board.is_tier1(u) and not both_tier1:
                continue
            ends.append(u)
        for unit in sorted(ends, key=lambda u: (board.n_pads(u), u)):
            conns_w = []
            for c, mine, peer in board.failed_for(unit, conns):
                cw = w if c['id'] == target['id'] else conn_weight(c, table, node_id)
                conns_w.append((dict(c, _mine=mine, _peer=peer), cw))
            tier1 = board.is_tier1(unit)
            limit = min(max_move, tier1_max_move) if tier1 else max_move
            res = board.search(unit, conns_w, target, limit, lineage_cap, skip=skip,
                               pad_limit=limit if tier1 else None)
            if res is None:
                continue
            poses = res.pop('poses')
            used.add(unit)
            detail = dict(arm='pull', conn=target['id'], net=target.get('net'), mode=target.get('mode'),
                          weight=w, mover=unit, mover_refs=list(board.units[unit]),
                          mover_keys=[board.key_of.get(r, r) for r in board.units[unit]],
                          max_move_mm=limit, **res)
            out.append(dict(poses=poses, detail=detail))
            break
    return out


def rand_child(board: MoveBoard, seed, disp, rotate=False, *, lineage_cap=LINEAGE_CAP_MM,
               tier1_max_move=TIER1_MAX_MOVE_MM, tries=64, skip=None):
    """Seeded random-move control with displacement ``disp`` (snapped to the grid)."""
    rng = random.Random(seed)
    units = sorted(u for u in board.units if board.movable(u))
    if not units:
        return None
    for attempt in range(tries):
        unit = rng.choice(units)
        theta = rng.uniform(0.0, 2 * math.pi)
        rot_choice = rng.choice(ANGLES)
        dx = round(disp * math.cos(theta) / board.grid) * board.grid
        dy = round(disp * math.sin(theta) / board.grid) * board.grid
        refs = board.units[unit]
        rot = None
        if rotate and len(refs) == 1 and refs[0] not in board.rot_req and not board.is_tier1(unit):
            cur = _norm(board.by_ref[refs[0]].rot)
            rot = None if abs(rot_choice - cur) < 1e-6 else rot_choice
        if abs(dx) < 1e-9 and abs(dy) < 1e-9 and rot is None:
            continue
        move = math.hypot(dx, dy)
        if board.is_tier1(unit) and move > tier1_max_move + 1e-9:
            continue
        if not board.within_lineage(refs, dx, dy, lineage_cap):
            continue
        why = board.check(refs, dx, dy, rot)
        tally = 'rand:' + (why or 'legal').split(':')[0]
        board.stats[tally] = board.stats.get(tally, 0) + 1
        if why is None:
            poses = board.poses_after(unit, dx, dy, rot)
            if skip is not None and skip(poses):
                continue
            detail = dict(arm='rand', mover=unit, mover_refs=list(refs),
                          mover_keys=[board.key_of.get(r, r) for r in refs], dx=dx, dy=dy, rot=rot,
                          move_mm=move, attempts=attempt + 1, seed=seed)
            return dict(poses=poses, detail=detail)
    return None


def power_guard(graph, roles, q_ref=None, band=0.05):
    """f(child graph) -> reason or None: no new power crossings, q_band at most one band worse.

    ``graph`` is the parent pose (the object the MoveBoard mutates)."""
    from pnr.place.power_first import Compiled, placement_quality
    compiled = Compiled(graph, roles)
    q0 = placement_quality(graph, roles, compiled)
    ref = q_ref if q_ref else q0['q_place']

    def bandof(q):
        return math.floor(q / (band * ref)) if ref and ref > 0 else 0

    def guard(child):
        q = placement_quality(child, roles, compiled)
        if q['crossings'] > q0['crossings']:
            return 'power crossings %d > %d' % (q['crossings'], q0['crossings'])
        if bandof(q['q_place']) - bandof(q0['q_place']) > 1:
            return 'q_band %d > %d + 1' % (bandof(q['q_place']), bandof(q0['q_place']))
        return None
    guard.parent_quality = dict(q_place=q0['q_place'], crossings=q0['crossings'], q_ref=ref)
    return guard
