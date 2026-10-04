"""GND via placement of the RF macro: deterministic; every merge, drop and repair is recorded.

Order (each via keeps the fab's 0.43 mm centre spacing to every earlier one):
1. launch vias (RFS-1);
2. the bank lattice, fixed and identical in every column's frame: run-in pairs at
   E - runin_inset and E - guard_band beside every input, two ring sites between neighbouring
   pairs (at E - ring_inset and E - guard_band), five sites per column pitch along the top
   (ring_inset and guard_band outside the cut-out). A lattice site that cannot be placed is a
   conflict and fails G5;
3. the dummy loads' GND vias; a via at the centre of every meander arc of radius fence_offset (the
   inner fence row collapses there); the via one fence pitch below each lower run-in pair;
4. fence rows: per feed and side, on the corridor boundary (fence_offset from the line) or, where
   another line or a facing leg comes within 2 x fence_offset + 0.43 mm, on the ridge midway (one
   shared row). Each row is covered by one chain of vias chosen on (or up to 0.12 mm beside) the
   row: fewest vias such that every row point is within the planned radius, spacing near
   fence_pitch;
5. the isolation wall along the middle of the inter-bank strip; the rings round each cut-out
   (ring_inset and guard_band, rounded corners); the In2 boundary rows (stitch_inset inside the
   boundary, its corners fixed);
6. repair: a row point left uncovered gets a via, after shifting up to two movable vias if needed;
   nudge: a pair of consecutive fence vias over 0.50 mm apart (G4) is closed by moving a movable via
   by up to 0.12 mm;
7. a stitch_grid grid in the open pour (optional: drops are counted by reason);
8. fill: GND that no via reaches within stitch_reach gets a via where a site exists; what is left
   becomes a pour keepout (gap), never an unstitched sliver.
Only lattice vias sit above Pg (E - runin_out), so every column's run-in sees the same vias.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

from .geom import Path, PointIndex, SegIndex, path_samples, rect, rect_dist
from .params import D_LATTICE, RULES

Pt = Tuple[float, float]
# via pad edge to an SMD land edge (GND land of a load): a solder-mask web, no wicking [D]
SMD_VIA_CLEAR = 0.10


class Placer:
    def __init__(self, mc, ru):
        from .macro import _place_paths

        self.mc, self.ru = mc, ru
        self.idx = PointIndex(0.5)
        self.vias: List[Tuple[Pt, float, float, str]] = []
        self.lim = RULES["fence_pitch_min"]
        self.drill, self.pad = RULES["via_fence"]
        self.log: Dict[str, object] = dict(merged=[], dropped={}, conflicts=[], row_gaps=[])
        self.fixed: set = set()  # indices of vias the repair may not move
        self.need: List[Tuple[Pt, float, str]] = []  # row points to cover: (point, radius, role)
        self.lines = SegIndex(1.0)
        for n, f in mc.feeds.items():
            self.lines.add_path(f, n)
        for n, r in mc.runins.items():
            self.lines.add_path(r, n)
        for n, c in mc.columns.items():
            for q in _place_paths(c):
                self.lines.add_path(q, n)
        self.cuts = list(mc.cutouts.values())
        self.pour = [_bbox(q) for q in mc.pour[:2]]
        self.pad1 = [_bbox(ld.pad1) for ld in mc.loads.values()]
        self.pad2 = [_bbox(ld.pad2) for ld in mc.loads.values()]
        self.load_zones = [ld.via_zone for ld in mc.loads.values()]

    # ---- site rules -------------------------------------------------------------------
    def in_pour(self, q: Pt, m: float) -> bool:
        for dx, dy in ((0, 0), (m, 0), (-m, 0), (0, m), (0, -m)):
            pt = (q[0] + dx, q[1] + dy)
            if not any(r[0] <= pt[0] <= r[2] and r[1] <= pt[1] <= r[3] for r in self.pour):
                return False
        return True

    def site(self, q: Pt, band: bool = False) -> Optional[str]:
        """None when a GND via may sit at `q`, else the reason it may not."""
        ru = self.ru
        pr = self.pad / 2
        if abs(q[0]) < ru.half + pr and abs(q[1]) < ru.half + pr:
            return "package"
        if not self.in_pour(q, pr):
            return "pour edge"
        for c in self.cuts:
            rd = rect_dist(q, c)
            if rd < pr + 0.05:
                return "cut-out"
            if not band and rd < ru.lout - 1e-6:
                return "guard band"  # only the lattice above Pg (= E - runin_out)
        for r in self.pad1:
            if rect_dist(q, r) < ru.gap + pr - 1e-9:
                return "load pad"
        for r in self.pad2:  # no via in or beside the GND land (solder wicking)
            if rect_dist(q, r) < SMD_VIA_CLEAR + pr - 1e-9:
                return "load pad"
        for z in self.load_zones:  # the load cell's own vias only (placed before this check)
            if z[0] < q[0] < z[2] and z[1] < q[1] < z[3]:
                return "load zone"
        if self.lines.dist(q, ru.site_min) < ru.site_min - 1e-6:
            return "line"
        return None

    def add(self, q: Pt, role: str, required: bool) -> str:
        near = self.idx.within(q, self.lim - 1e-6)
        if near:
            dd, j = min((math.dist(q, self.idx.pts[j]), j) for j in near)
            if dd <= 0.10 + 1e-9:
                self.log["merged"].append(
                    dict(role=role, at=_r(q), into=self.vias[j][3], d=round(dd, 4))
                )
                return "merged"
            if required:
                self.log["conflicts"].append(
                    dict(role=role, at=_r(q), near=self.vias[j][3], d=round(dd, 4))
                )
                return "conflict"
            self._drop(role, "spacing")
            return "dropped"
        self.idx.add(q)
        self.vias.append((q, self.drill, self.pad, role))
        if required:
            self.fixed.add(len(self.vias) - 1)
        return "ok"

    def _drop(self, role: str, why: str) -> None:
        dr = self.log["dropped"].setdefault(role, {})
        dr[why] = dr.get(why, 0) + 1

    def covered(self, q: Pt, r: float) -> bool:
        return self.idx.nearest(q, r)[1] >= 0

    def place_near(self, run: List[Pt], k: int, role: str, nudge: int = 4) -> bool:
        """Add a via at run[k], or at the nearest of run[k +- 1..nudge] that keeps the spacing."""
        for dk in [0] + [s * i for i in range(1, nudge + 1) for s in (1, -1)]:
            j = k + dk
            if 0 <= j < len(run) and not self.idx.within(run[j], self.lim - 1e-6):
                self.idx.add(run[j])
                self.vias.append((run[j], self.drill, self.pad, role))
                return True
        return False

    def repair(self, rounds: int = 3) -> Dict[str, int]:
        """Local search over every row point still uncovered: insert a via within its radius, or
        insert one after shifting up to two movable vias that block it (each by at most 0.25 mm,
        keeping every row point it alone covered)."""
        need = self.need
        nix = PointIndex(0.5)
        for q, _, _ in need:
            nix.add(q)
        stats = dict(inserted=0, shifted=0, left=0)

        def cov(q, r, skip=()):
            return any(j not in skip for j in self.idx.within(q, r))

        def site_ok(c, role):
            return self.site(c, band=(role == "ring")) is None

        for _ in range(rounds):
            todo = [k for k, (q, r, _) in enumerate(need) if not cov(q, r)]
            if not todo:
                break
            progress = False
            for k in todo:
                q, r, role = need[k]
                if cov(q, r):
                    continue
                cands = []
                n = int(r / 0.02)
                for i in range(-n, n + 1):
                    for j in range(-n, n + 1):
                        c = (q[0] + 0.02 * i, q[1] + 0.02 * j)
                        if math.dist(c, q) <= r and site_ok(c, role):
                            cands.append(c)
                cands.sort(key=lambda c: math.dist(c, q))
                done = False
                for c in cands:
                    if not self.idx.within(c, self.lim - 1e-6):
                        self._push(c, role)
                        stats["inserted"] += 1
                        done = True
                        break
                if not done:
                    for c in cands[:60]:
                        blk = self.idx.within(c, self.lim - 1e-6)
                        if len(blk) > 2 or any(b in self.fixed for b in blk):
                            continue
                        moves = self._relocate(blk, c, nix)
                        if moves is None:
                            continue
                        for b, nb in moves.items():
                            self.idx.move(b, nb)
                            v = self.vias[b]
                            self.vias[b] = (nb, v[1], v[2], v[3])
                        self._push(c, role)
                        stats["shifted"] += len(moves)
                        stats["inserted"] += 1
                        done = True
                        break
                progress = progress or done
            if not progress:
                break
        stats["left"] = sum(1 for q, r, _ in need if not cov(q, r))
        return stats

    def nudge(self) -> Dict[str, int]:
        """Fence pairs just over 0.50 mm: move one movable via of the pair by up to 0.08 mm (valid
        site, fab spacing kept) where that lowers the number of over-limit pairs and openings
        within 1 mm."""
        from .rules import fence_rows, fence_sequences

        stats = dict(moved=0, left=0)
        rows = fence_rows(self.mc, self.ru, self)

        def count(bb):
            sq = fence_sequences(self.mc, self.ru, self, bbox=bb, rows=rows)
            return sum(len(r["bad"]) + len(r["holes"]) for r in sq.values())

        for _ in range(5):
            sq = fence_sequences(self.mc, self.ru, self, rows=rows)
            pairs = [(i, j) for r in sq.values() for i, j, _ in r["bad"]]
            if not pairs:
                break
            moved = False
            for i, j in pairs:
                a, b = self.idx.pts[i], self.idx.pts[j]
                if math.dist(a, b) <= 0.50:
                    continue
                mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
                bb = (mid[0] - 1.0, mid[1] - 1.0, mid[0] + 1.0, mid[1] + 1.0)
                base = count(bb)
                best = None
                movers = [v for v in self.idx.within(mid, 0.6) if v not in self.fixed]
                for v in movers:
                    p0 = self.idx.pts[v]
                    for di in range(-6, 7):
                        for dj in range(-6, 7):
                            c = (p0[0] + 0.02 * di, p0[1] + 0.02 * dj)
                            if math.dist(c, p0) > 0.12 or (di == 0 and dj == 0):
                                continue
                            if any(x != v for x in self.idx.within(c, self.lim - 1e-6)):
                                continue
                            if self.site(c) is not None:
                                continue
                            self.idx.move(v, c)
                            k = count(bb)
                            self.idx.move(v, p0)
                            key = (k, math.dist(c, p0))
                            if k < base and (best is None or key < best[0]):
                                best = (key, v, c)
                if best is not None:
                    _, v, c = best
                    self.idx.move(v, c)
                    old = self.vias[v]
                    self.vias[v] = (c, old[1], old[2], old[3])
                    stats["moved"] += 1
                    moved = True
            if not moved:
                break
        sq = fence_sequences(self.mc, self.ru, self, rows=rows)
        stats["left"] = sum(len(r["bad"]) + len(r["holes"]) for r in sq.values())
        return stats

    def _push(self, c: Pt, role: str) -> None:
        self.idx.add(c)
        self.vias.append((c, self.drill, self.pad, role))

    def _relocate(self, blk: List[int], c: Pt, nix: PointIndex):
        """New positions for the vias `blk` so that a via fits at `c`, or None."""
        need = self.need
        new: Dict[int, Pt] = {}
        for b in blk:
            v = self.idx.pts[b]
            role = self.vias[b][3]
            # row points that only b covers now (c will cover some of them)
            mine = []
            for k in nix.within(v, 0.31):
                q, r, _ = need[k]
                if math.dist(q, v) > r:
                    continue
                others = [j for j in self.idx.within(q, r) if j != b and j not in new]
                if (
                    others
                    or math.dist(q, c) <= r
                    or any(math.dist(q, p) <= r for p in new.values())
                ):
                    continue
                mine.append((q, r))
            best = None
            for i in range(-12, 13):
                for j in range(-12, 13):
                    nb = (v[0] + 0.02 * i, v[1] + 0.02 * j)
                    dv = math.dist(nb, v)
                    if dv > 0.25 or math.dist(nb, c) < self.lim:
                        continue
                    if any(math.dist(nb, p) < self.lim for p in new.values()):
                        continue
                    if any(jj != b for jj in self.idx.within(nb, self.lim - 1e-6)):
                        continue
                    if not all(math.dist(nb, q) <= r for q, r in mine):
                        continue
                    if self.site(nb, band=(role == "ring")) is not None:
                        continue
                    if best is None or dv < best[0]:
                        best = (dv, nb)
            if best is None:
                return None
            new[b] = best[1]
        return new

    def _dp_row(self, run: List[Pt], unc: List[bool], r: float, p_t: float, role: str, depth=0):
        """Fewest vias that leave no uncovered row point farther than r from a via: candidates
        are every row point and points beside it (+-0.06, +-0.12 mm across the row, so a
        chain can zig-zag to fit between fixed vias), each a valid site >= the fab spacing from
        every via already placed and from the previous pick. Returns positions, or None."""
        n = len(run)
        U = [k for k in range(n) if unc[k]]
        if not U:
            return []
        lim = self.lim
        band = role == "ring"
        cands: List[Tuple[int, Pt]] = []
        for k in range(0, n):
            k0, k1 = max(0, k - 2), min(n - 1, k + 2)
            tx, ty = run[k1][0] - run[k0][0], run[k1][1] - run[k0][1]
            tl = math.hypot(tx, ty)
            nx, ny = (-ty / tl, tx / tl) if tl > 1e-9 else (0.0, 0.0)
            # rings and boundary rows stitch an edge: their vias stay on the row
            offs = (0.0, 0.06, -0.06, 0.12, -0.12) if role in ("fence", "isolation") else (0.0,)
            for dlt in offs:
                if dlt and tl <= 1e-9:
                    continue
                c = (run[k][0] + dlt * nx, run[k][1] + dlt * ny)
                if self.idx.within(c, lim - 1e-6):
                    continue
                if dlt and self.site(c, band=band) is not None:
                    continue
                cands.append((k, c))
        if not cands:
            return None
        # points no candidate can cover (fixed vias around them) are left to the repair
        cix = PointIndex(0.5)
        for _, c in cands:
            cix.add(c)
        for k in U:
            if not cix.within(run[k], r):
                unc[k] = False
        U = [k for k in range(n) if unc[k]]
        if not U:
            return []

        def reach_end(i):
            k, c = cands[i]
            for t in range(0, n):
                if unc[t] and t >= k - 40 and math.dist(run[t], c) > r and t > k:
                    return t
            return n

        by_k: Dict[int, List[int]] = {}
        for i, (k, _) in enumerate(cands):
            by_k.setdefault(k, []).append(i)
        best: Dict[int, Tuple[int, float]] = {}
        prev: Dict[int, Optional[int]] = {}
        first = U[0]
        # a free row start (the fence start at the package) takes its via on the first point, so
        # the GND at the package edge stays within reach
        r0 = 0.03 if first == 0 else r
        for i, (k, c) in enumerate(cands):
            if math.dist(c, run[first]) <= r0 and all(
                math.dist(run[t], c) <= r for t in U if t < k
            ):
                best[i] = (1, 0.0)
                prev[i] = None
        ends = []
        todo = sorted(best, key=lambda i: cands[i][0])
        seen = set()
        stuck = -1
        while todo:
            i = todo.pop(0)
            if i in seen:
                continue
            seen.add(i)
            k1, c1 = cands[i]
            m = reach_end(i)
            if m >= n:
                ends.append(i)
                continue
            stuck = max(stuck, m)
            for k2 in range(max(k1 + 1, m - 40), min(n, m + 40)):
                for j in by_k.get(k2, ()):
                    c2 = cands[j][1]
                    if math.dist(c2, run[m]) > r:
                        continue
                    dd = math.dist(c1, c2)
                    if dd < lim - 1e-9:
                        continue
                    if k2 > m and not all(
                        not unc[t] or math.dist(run[t], c2) <= r or math.dist(run[t], c1) <= r
                        for t in range(m, k2)
                    ):
                        continue
                    cst = (best[i][0] + 1, best[i][1] + (dd - p_t) ** 2)
                    if j not in best or cst < best[j]:
                        best[j] = cst
                        prev[j] = i
                        todo.append(j)
            todo.sort(key=lambda t: cands[t][0])
        if not ends:
            # every chain dies at some point: leave the farthest one to the repair and retry
            if stuck < 0 or depth > 12:
                return None
            unc = list(unc)
            unc[stuck if best else U[0]] = False
            return self._dp_row(run, unc, r, p_t, role, depth + 1)
        i = min(ends, key=lambda t: (best[t], cands[t][0]))
        out = []
        while i is not None:
            out.append(cands[i][1])
            i = prev[i]
        return out[::-1]

    def cover_row(
        self,
        pts: List[Tuple[float, Optional[Pt]]],
        r_cov: float,
        role: str,
        tag: str,
        p_max: Optional[float] = None,
    ):
        """Cover a row: every valid point within p_max / 2 of a via, vias >= the fab spacing
        apart. A U-turn's collapsed centre takes a via first; the rest of the row is covered by
        one chain of vias chosen on the row (`_dp_row`), with an even-spacing fallback."""
        p_t = 2 * r_cov
        p_max = p_max or p_t
        r = p_max / 2 - 0.005  # planned coverage, with margin for other rows sampled between
        self.need += [(q, p_max / 2 + 0.004, role) for _, q in pts if q is not None]
        run = [q for _, q in pts if q is not None]
        placed, skipped = 0, 0
        if run:
            u = [0.0]
            for k in range(1, len(run)):
                u.append(u[-1] + math.dist(run[k], run[k - 1]))
            for k in _collapses(u):
                if not self.covered(run[k], r) and self.place_near(run, k, role, nudge=1):
                    placed += 1
            unc = [not self.covered(q, r) for q in run]
            pos = self._dp_row(run, unc, r, p_t, role)
            picks = []
            if pos is not None:
                for c in pos:
                    if not self.idx.within(c, self.lim - 1e-6):
                        self._push(c, role)
                        placed += 1
                    else:
                        skipped += 1
            else:
                k = 0
                while k < len(run):
                    if not unc[k]:
                        k += 1
                        continue
                    a = k
                    while k < len(run) and unc[k]:
                        k += 1
                    b = k - 1
                    for up in _plan(u[a], u[b], a > 0, b < len(run) - 1, r, p_t, p_max, self.lim):
                        picks.append(min(range(a, b + 1), key=lambda t: (abs(u[t] - up), t)))
                self.log.setdefault("dp_fallback", []).append(tag)
            for j in picks:
                if self.place_near(run, j, role, nudge=2):
                    placed += 1
                else:
                    skipped += 1
        gaps = [_r(q) for _, q in pts if q is not None and not self.covered(q, p_max / 2 + 0.005)]
        if gaps or skipped:
            self.log["row_gaps"].append(
                dict(row=tag, points=len(gaps), skipped=skipped, first=gaps[0] if gaps else None)
            )
        return placed, gaps


def _collapses(u: List[float]) -> List[int]:
    """Middle indices of runs of three or more row points on one spot (a U-turn of radius
    fence_offset collapses its inner row onto the centre)."""
    out = []
    n = len(u)
    k = 0
    while k < n - 2:
        j = k
        while j + 1 < n and u[j + 1] - u[j] < 0.003:
            j += 1
        if j - k >= 2:
            out.append((k + j) // 2)
            k = j + 1
        else:
            k += 1
    return out


def _plan(
    ua: float, ub: float, left: bool, right: bool, r: float, p_t: float, p_max: float, lim: float
) -> List[float]:
    """Fallback: row positions (arc length) for an uncovered stretch [ua, ub]; `left`/`right`
    say whether a via already covers the point before/after it (it then sits about r beyond)."""
    lo = ua - r if left else ua
    hi = ub + r if right else ub
    L = hi - lo
    if L < lim and not (left or right):
        return [0.5 * (lo + hi)]
    n_lo = max(1, int(math.ceil(L / p_max - 1e-9)))
    n_hi = max(1, int(math.floor(L / lim + 1e-9)))
    n = min(max(int(round(L / p_t)), n_lo), n_hi) if n_lo <= n_hi else n_hi
    if left and right:
        return [lo + L * k / n for k in range(1, n)] or [0.5 * (lo + hi)]
    if left:
        return [lo + L * k / n for k in range(1, n + 1)]
    if right:
        return [lo + L * k / n for k in range(0, n)]
    return [lo + L * k / n for k in range(0, n + 1)]


def _bbox(poly) -> Tuple[float, float, float, float]:
    xs = [q[0] for q in poly]
    ys = [q[1] for q in poly]
    return (min(xs), min(ys), max(xs), max(ys))


def _r(q: Pt) -> List[float]:
    return [round(q[0], 4), round(q[1], 4)]


def fence_row(path: Path, side: int, s_from: float, s_to: float, pl: Placer, name: str = ""):
    """Row points of one feed side: (arc length, site or None). The row runs at fence_offset
    from the line unless another line, or a facing leg of the same line (more than 1.3 mm away
    along it), comes within 2 x fence_offset + 0.43 of the line: then the row runs on the ridge
    midway between the two (one shared row; a U-turn of radius fence_offset collapses onto its
    centre). A point that is not a via site is None."""
    ru = pl.ru
    foff = ru.foff
    hi = foff + pl.lim / 2
    out = []
    for q, h, s in path_samples(path, 0.02):
        if s < s_from - 1e-9:
            continue
        if s > s_to + 1e-9:
            break
        sx, sy = -math.sin(h) * side, math.cos(h) * side
        o = (q[0] + foff * sx, q[1] + foff * sy)

        def far(t):
            return pl.lines.dist_far((q[0] + t * sx, q[1] + t * sy), t + 0.05, name, s, 1.3)

        lo, up = 0.0, hi
        if far(hi) >= hi - 1e-6:
            lo = hi
        else:
            for _ in range(22):
                t = 0.5 * (lo + up)
                if far(t) >= t - 1e-6:
                    lo = t
                else:
                    up = t
        cand = None
        if lo >= hi - 1e-6:
            cand = o  # nothing facing within 2 x fence_offset + 0.43: the row at fence_offset
        elif lo >= ru.site_min - 1e-6:
            cand = (q[0] + lo * sx, q[1] + lo * sy)  # shared row midway
        if cand is not None and pl.site(cand) is not None:
            cand = None
        out.append((s, cand))
    return out


def fence_start(name: str, path: Path, ru) -> float:
    """Arc length where the fence starts: `fence_start` outside the package body."""
    b = path.start
    return (ru.half + ru.fence_start) - (b[1] if name.startswith("RX") else b[0])


def lattice_sites(mc, ru, bank: str) -> List[Tuple[Pt, str]]:
    """The fixed via sites of one bank (identical in every column's frame)."""
    b = mc.banks[bank]
    cut = mc.cutouts[bank]
    p = mc.params
    e = b["E"]
    top = cut[3]
    si, ri, B = float(p["runin_inset"]), float(p["ring_inset"]), ru.B
    d = D_LATTICE
    xs = sorted(b["inputs"].values())
    actual = {round(x, 6) for x in xs}
    a = (d - 2 * ru.foff) / 3  # two ring sites between neighbouring run-in pairs
    out: List[Tuple[Pt, str]] = []
    for k in range(-4, len(xs) + 4):
        xi = xs[0] + k * d
        role = "runin" if round(xi, 6) in actual else "ring"
        for x in (xi - ru.foff, xi + ru.foff):
            out.append(((x, e - si), role))
            out.append(((x, e - B), role))
        for x in (xi + ru.foff + a, xi + ru.foff + 2 * a):
            out.append(((x, e - ri), "ring"))
            out.append(((x, e - B), "ring"))
        for j in range(5):
            x = xi + j * d / 5
            out.append(((x, top + ri), "ring"))
            out.append(((x, top + B), "ring"))
    keep = []
    seen = set()
    for q, role in out:
        if not (cut[0] - 1e-9 <= q[0] <= cut[2] + 1e-9):
            continue
        key = (round(q[0], 5), round(q[1], 5))
        if key in seen:
            continue
        seen.add(key)
        keep.append((q, role))
    keep.sort(key=lambda t: (t[0][1], t[0][0]))
    return keep


def ring_points(cut, t: float, step: float = 0.02) -> List[Tuple[float, Pt]]:
    """Points on the cut-out grown by `t` with round corners, counter-clockwise from the
    south-west corner, with their arc length."""
    x0, y0, x1, y1 = cut
    pts: List[Pt] = []

    def line(a, b):
        n = max(1, int(math.ceil(math.dist(a, b) / step)))
        for k in range(n):
            pts.append((a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n))

    def arc(c, a0):
        n = max(1, int(math.ceil(t * math.pi / 2 / step)))
        for k in range(n):
            a = a0 + (math.pi / 2) * k / n
            pts.append((c[0] + t * math.cos(a), c[1] + t * math.sin(a)))

    line((x0, y0 - t), (x1, y0 - t))
    arc((x1, y0), -math.pi / 2)
    line((x1 + t, y0), (x1 + t, y1))
    arc((x1, y1), 0.0)
    line((x1, y1 + t), (x0, y1 + t))
    arc((x0, y1), math.pi / 2)
    line((x0 - t, y1), (x0 - t, y0))
    arc((x0, y0), math.pi)
    out, s = [], 0.0
    for i, q in enumerate(pts):
        if i:
            s += math.dist(q, pts[i - 1])
        out.append((s, q))
    return out


def straight_points(a: Pt, b: Pt, step: float = 0.02) -> List[Tuple[float, Pt]]:
    n = max(1, int(math.ceil(math.dist(a, b) / step)))
    L = math.dist(a, b)
    return [
        (L * k / n, (a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n))
        for k in range(n + 1)
    ]


def place(mc, ru) -> None:
    from . import rules as rules_mod

    p = mc.params
    pl = Placer(mc, ru)
    launch = [v for v in mc.vias if v[3] == "launch"]
    for v in launch:
        pl.idx.add(v[0])
        pl.vias.append(v)
        pl.fixed.add(len(pl.vias) - 1)

    # 2. lattice
    lattice = {}
    for bank in mc.banks:
        for q, role in lattice_sites(mc, ru, bank):
            why = pl.site(q, band=True)
            if why is not None:
                pl.log["conflicts"].append(dict(role=role, at=_r(q), site=why))
                continue
            pl.add(q, role, True)
        lattice[bank] = len(lattice_sites(mc, ru, bank))

    # 3. dummy loads: the load cell's own vias (its zone is closed to every other via)
    for ld in mc.loads.values():
        for q in ld.vias:
            why = pl.site(q)
            if why not in (None, "line", "load zone"):  # the load's own run-in ends at its pad
                pl.log["conflicts"].append(dict(role="load", at=_r(q), site=why))
                continue
            pl.add(q, "load", True)

    # 4. U-turn and tight-bend centres of the meanders (arcs of radius fence_offset): the inner
    # fence row collapses there, one via serves it (design: a via at each U-turn centre)
    for n, f in mc.feeds.items():
        s = 0.0
        for sg in f.segs:
            if (
                sg.kind == "arc"
                and abs(sg.radius - ru.foff) < 1e-6
                and s >= fence_start(n, f, ru)
                and s <= f.marks["Pg"][1]
            ):
                if pl.site(sg.center) is None:
                    pl.add(sg.center, "fence", True)
            s += sg.length

    # each fence row meets its run-in pair (E - guard_band) through a fixed via one fence pitch
    # before it: on the straight before Pg where the line has one (x_in +- fence_offset,
    # Pg - 0.40); where an RX bump's outer row reaches the pair obliquely instead, the row point
    # (walking back from Pg) nearest 0.46 mm from the pair. A finger ending at Pg needs none: its
    # last arc's centre via is the pair, and its outer row arrives along the run-in.
    for n, f in mc.feeds.items():
        xi = f.marks["Pg"][0][0]
        e = f.marks["E"][0][1]
        y_a = e - ru.B - ru.pitch
        seg_ok = any(
            sg.kind == "line"
            and abs(sg.p1[0] - sg.p0[0]) < 1e-9
            and sg.p0[1] <= y_a + 1e-6
            and sg.p1[1] >= y_a - 1e-6
            for sg in f.segs
        )
        for side in (1, -1):
            pair = (xi - side * ru.foff, e - ru.B)  # left side (+1) is west of a northward line
            q_a = (pair[0], y_a)
            if seg_ok and pl.site(q_a) is None:
                if not pl.idx.within(q_a, pl.lim - 1e-6):
                    pl.add(q_a, "fence", True)
                continue
            if not n.startswith("RX"):
                continue
            row = fence_row(f, side, fence_start(n, f, ru), f.marks["Pg"][1], pl, n)
            best = None
            for _, q in reversed(row):
                if q is None:
                    continue
                dq = math.dist(q, pair)
                if dq > 0.60:
                    break
                if 0.43 <= dq <= 0.50 and (best is None or abs(dq - 0.46) < best[0]):
                    best = (abs(dq - 0.46), q)
            if best is not None and not pl.idx.within(best[1], pl.lim - 1e-6):
                pl.add(best[1], "fence", True)

    # 5. fence rows
    r_fence = ru.pitch / 2
    rows = {}
    for n, f in mc.feeds.items():
        s0 = fence_start(n, f, ru)
        for side, tag in ((1, "L"), (-1, "R")):
            pts = fence_row(f, side, s0, f.marks["Pg"][1], pl, n)
            placed, gaps = pl.cover_row(pts, r_fence, "fence", f"{n}.{tag}", p_max=0.47)
            rows[f"{n}.{tag}"] = dict(
                points=sum(1 for _, q in pts if q), placed=placed, gaps=len(gaps)
            )

    # 5. isolation wall along the middle of the inter-bank strip
    x_iso = mc.ports["iso_fence_x"]["at"][0]
    y_top = max(c[3] for c in mc.cutouts.values()) + ru.B
    pts = [
        (s, q if pl.site(q) is None else None)
        for s, q in straight_points((x_iso, ru.half + 0.6), (x_iso, y_top))
    ]
    pl.cover_row(pts, r_fence, "isolation", "isolation", p_max=0.47)

    # 6. rings round the cut-outs, In2 boundary rows
    ri, B = float(p["ring_inset"]), ru.B
    sp, si = float(p["stitch_pitch"]), float(p["stitch_inset"])
    for bank, cut in mc.cutouts.items():
        for t, r_cov in ((ri, 0.25), (B, sp / 2)):
            pts = [
                (s, q if pl.site(q, band=True) is None else None) for s, q in ring_points(cut, t)
            ]
            pl.cover_row(pts, r_cov, "ring", f"ring {bank} {t}")
    x_lo, x_hi = mc.region["x"]
    y_s, y_hi = mc.region["y"]
    half = ru.half
    edges = [
        ((x_lo + si, half + si), (x_lo + si, y_hi - si)),  # west
        ((x_lo + si, y_hi - si), (x_hi - si, y_hi - si)),  # north (board edge)
        ((x_hi - si, y_hi - si), (x_hi - si, y_s + si)),  # east
        ((x_hi - si, y_s + si), (half + si, y_s + si)),  # east strip, south edge
        ((x_lo + si, half + si), (-half - si, half + si)),  # west of the package
    ]
    # the region's corners take a via each, so the GND corner stays within reach
    for q in ((x_lo + si, y_hi - si), (x_hi - si, y_hi - si), (x_hi - si, y_s + si)):
        if pl.site(q) is None:
            pl.add(q, "stitch", True)
    for k, (a, b) in enumerate(edges):
        pts = [(s, q if pl.site(q) is None else None) for s, q in straight_points(a, b)]
        pl.cover_row(pts, sp / 2, "stitch", f"boundary {k}")

    pl.log["repair"] = pl.repair()
    pl.log["nudge"] = pl.nudge()

    # 7. grid in the open pour
    g = float(p["stitch_grid"])
    for i in range(int(math.floor(x_lo / g)), int(math.ceil(x_hi / g)) + 1):
        for j in range(int(math.floor(y_s / g)), int(math.ceil(y_hi / g)) + 1):
            q = (round(i * g, 6), round(j * g, 6))
            why = pl.site(q)
            if why is not None:
                pl._drop("grid", why)
                continue
            pl.add(q, "grid", False)

    # 8. fill what the grid leaves unreached; the rest becomes gap
    fill_rounds = []
    for rnd in range(8):
        mc.vias = list(pl.vias)
        res = rules_mod.stitch_raster(mc, ru)
        pieces = res["pieces"]
        fill_rounds.append(len(pieces))
        if not pieces:
            break
        added = 0
        for pc in pieces:
            q = _fill_site(pc, pl)
            if q is not None and pl.add(q, "fill", False) == "ok":
                added += 1
        if not added:
            break
    mc.vias = list(pl.vias)
    res = rules_mod.stitch_raster(mc, ru)
    for pc in res["pieces"]:
        x0, y0, x1, y1 = pc["bbox"]
        m = 0.02
        mc.unstitched.append(rect(x0 - m, y0 - m, x1 + m, y1 + m))
    pl.log["fill_rounds"] = fill_rounds
    pl.log["made_gap"] = [
        dict(at=pc["at"], long_mm=pc["long_mm"], area_mm2=pc["area_mm2"]) for pc in res["pieces"]
    ]
    pl.log["lattice_sites"] = lattice
    mc.via_log = pl.log
    mc.rows = rows


def _fill_site(pc: Dict, pl: Placer) -> Optional[Pt]:
    """A via site that reaches the unreached piece `pc`: the valid point nearest the piece's
    pixel closest to its centroid, on a 0.02 mm lattice within stitch_reach of it."""
    ax, ay = pc["near"]
    reach = float(pl.mc.params["stitch_reach"]) - 0.02
    best = None
    n = int(reach / 0.02)
    for i in range(-n, n + 1):
        for j in range(-n, n + 1):
            q = (round(ax + i * 0.02, 5), round(ay + j * 0.02, 5))
            dd = math.dist(q, (ax, ay))
            if dd > reach:
                continue
            if best is not None and dd >= best[0]:
                continue
            if pl.site(q) is not None or pl.idx.within(q, pl.lim - 1e-6):
                continue
            best = (dd, q)
    return best[1] if best else None
