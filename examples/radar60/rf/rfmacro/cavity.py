"""The L2-L3 bondply cavity under each bank (rf-uniform: excited through the L2 windows, -10 to
-25 dB across the bank, contained by the ring vias) and its options:

- K0: the ring only (today);
- K2: L2-L3 posts (GND through vias) round the window groups wherever L1 is free. The posts are
  laid out once in the column frame (one column pitch either side of the axis, so every column
  and dummy gets the same posts and G2 holds) on a `post_pitch` lattice: inside the cut-out,
  outside every L2 window, pads at least `post_clear` from patch copper (stage 2 saw via pads
  detune patches) and at least `post_line_clear` from the column's line centrelines;
- K1: solid L2 under the patches (`windows` False): no L2 aperture under the bank, so nothing
  excites the bondply there; the column's match on 4 mil alone is the question (C1).

`check` is G9: the posts, their clearances, and per bank the ring-bounded cavity: the largest
via-free circle, the [D] TM mode count of the rectangle in 57-70 GHz.
"""

from __future__ import annotations

import math
from typing import Dict, List, Tuple

from .geom import PointIndex, seg_point_dist
from .params import D_LATTICE, RULES, STACK

Pt = Tuple[float, float]
C0 = 299792458.0


def _poly_dist(q: Pt, poly: List[Pt]) -> float:
    """Distance from a point to a polygon's outline (0 inside)."""
    from .geom import poly_contains, seg_dist

    if poly_contains(poly, q):
        return 0.0
    n = len(poly)
    return min(seg_dist(q, poly[i], poly[(i + 1) % n]) for i in range(n))


def cell_posts(mc, ru) -> List[Pt]:
    """K2 post sites in the column frame (input on +x, unmirrored)."""
    from .macro import _place_paths, make_column

    p = mc.params
    d = D_LATTICE
    gpr = RULES["via_fence"][1] / 2
    pc = float(p["post_clear"]) + gpr
    lc = float(p["post_line_clear"]) + gpr
    pitch = max(float(p["post_pitch"]), RULES["fence_pitch_min"])
    # this column and its neighbours, built in the column frame
    cols = [make_column("c", "c", (k * d, 0.0), False, p, mc.dims) for k in (-2, -1, 0, 1, 2)]
    patches = [poly for c in cols for poly in c.patches]
    wins = [w for c in cols for w in c.windows]
    segs = [sg for c in cols for q in _place_paths(c) for sg in q.segs]
    # the run-in below P1 (to the entry) of this column and the neighbours
    y_e = ru.p1_y - ru.lin
    y1c = ru.cell[3]
    y_lo, y_hi = y_e + 0.26, y1c + ru.lin - 0.26
    out: List[Pt] = []
    ix = PointIndex(0.5)
    step = 0.02
    # G2: a post of the column two pitches away must stay outside every column's congruence
    # window (+-(2 d - W/2 - window_margin - 0.01)), whether that column exists or not
    x_lim = mc.dims["patch"]["w"] / 2 + float(p["window_margin"])
    nx = int(round(d / step))
    ny = int(round((y_hi - y_lo) / step))
    cands = []
    for i in range(nx):
        x = -d / 2 + (i + 0.5) * step
        if abs(x) > x_lim:
            continue
        for j in range(ny + 1):
            y = y_lo + j * step
            q = (x, y)
            if any(
                w[0][0] - 0.05 <= x <= w[2][0] + 0.05 and w[0][1] - 0.05 <= y <= w[2][1] + 0.05
                for w in wins
            ):
                continue
            if min(_poly_dist(q, poly) for poly in patches) < pc - 1e-9:
                continue
            if min(seg_point_dist(q, sg) for sg in segs) < lc - 1e-9:
                continue
            for k in (-1, 0, 1):  # run-ins: x = in_x + k d from the entry to P1
                xi = ru.in_x + k * d
                if abs(x - xi) < lc and y_e - 1e-9 <= y <= ru.p1_y + 1e-9:
                    break
            else:
                cands.append(q)
    # greedy on rows, bottom to top, left to right: a lattice of about post_pitch
    cands.sort(key=lambda q: (round(q[1] / pitch), q[0]))
    for q in cands:
        if ix.within(q, pitch - 1e-9):
            continue
        # one period: a post near the +x edge would meet its copy from the next column
        if any(math.dist(q, (o[0] + s * d, o[1])) < pitch - 1e-9 for o in out for s in (-1, 1)):
            continue
        ix.add(q)
        out.append(q)
    return out


def posts(mc, ru) -> List[Pt]:
    """K2 posts of every column (dummies included), U1 frame."""
    from .macro import _xf

    loc = cell_posts(mc, ru)
    out = []
    seen = set()
    for c in mc.columns.values():
        for q in loc:
            u = _xf(q, c.origin, c.mirror)
            key = (round(u[0], 4), round(u[1], 4))
            if key not in seen:
                seen.add(key)
                out.append((round(u[0], 6), round(u[1], 6)))
    return out


def tm_modes(a: float, b: float, er: float, f_lo: float, f_hi: float) -> Dict[str, object]:
    """[D] TM_mn0 modes (E across the plane pair) of an a x b rectangle with PEC side walls in
    [f_lo, f_hi] GHz: f = c / (2 sqrt(er)) sqrt((m/a)^2 + (n/b)^2)."""
    k = C0 / (2 * math.sqrt(er)) / 1e9 * 1e3  # GHz mm
    out = []
    for m_ in range(0, 60):
        for n_ in range(0, 60):
            if m_ == 0 and n_ == 0:
                continue
            f = k * math.hypot(m_ / a, n_ / b)
            if f_lo <= f <= f_hi:
                out.append((m_, n_, round(f, 2)))
    f1 = min(k * math.hypot(1 / a, 0), k * math.hypot(0, 1 / b))
    return dict(count=len(out), lowest_ghz=round(f1, 2), first=sorted(out, key=lambda t: t[2])[:6])


def check(mc, ru) -> Dict[str, object]:
    """G9: the L2-L3 cavity cells under the banks."""
    from .geom import rect_dist

    p = mc.params
    gpr = RULES["via_fence"][1] / 2
    er = STACK["dk_bond"]
    res: Dict[str, object] = dict(option=p["l23_cavity"], windows=bool(p["windows"]))
    bad = []
    pts = list(mc.posts)
    if pts:
        d_patch = min(
            _poly_dist(q, poly) for q in pts for c in mc.columns.values() for poly in c.patches
        )
        res["posts"] = len(pts)
        res["post_pad_to_patch_mm"] = round(d_patch - gpr, 4)
        if d_patch - gpr < float(p["post_clear"]) - 1e-6:
            bad.append("post near a patch")
        from .macro import _place_paths

        segs = [sg for c in mc.columns.values() for q in _place_paths(c) for sg in q.segs]
        segs += [sg for f in list(mc.feeds.values()) + list(mc.runins.values()) for sg in f.segs]
        d_line = min(seg_point_dist(q, sg) for q in pts for sg in segs)
        res["post_pad_to_line_centreline_mm"] = round(d_line - gpr, 4)
        if d_line - gpr < float(p["post_line_clear"]) - 1e-6:
            bad.append("post near a line")
        placed = {(round(v[0][0], 4), round(v[0][1], 4)) for v in mc.vias if v[3] == "post"}
        lost = [q for q in pts if (round(q[0], 4), round(q[1], 4)) not in placed]
        res["posts_lost"] = len(lost)
        if lost:
            bad.append("post not placed")
    else:
        res["posts"] = 0
    from .raster import Grid

    banks = {}
    for bank, cut in mc.cutouts.items():
        ri = float(p["ring_inset"])
        cav = (cut[0] - ri, cut[1] - ri, cut[2] + ri, cut[3] + ri)  # bounded by the ring rows
        g = Grid(cav[0], cav[1], cav[2], cav[3], 0.02)
        dom = g.full()
        vs = [v[0] for v in mc.vias if rect_dist(v[0], cav) <= 0.5]
        lo, hi = 0.0, 4.0
        for _ in range(9):
            r = 0.5 * (lo + hi)
            cov = g.empty()
            g.disks(cov, vs, r)
            if g.count(g.andnot(dom, cov)):
                lo = r
            else:
                hi = r
        a, b = cav[2] - cav[0], cav[3] - cav[1]
        banks[bank] = dict(
            cavity_mm=[round(a, 3), round(b, 3)],
            largest_via_free_radius_mm=round(hi, 3),
            tm_modes_57_70ghz=tm_modes(a, b, er, 57.0, 70.0),
            posts_inside=sum(1 for q in pts if rect_dist(q, cut) <= 1e-9),
            excited=(
                "no L2 windows: nothing excites the bondply under the bank"
                if not p["windows"]
                else "through the L2 windows (rf-uniform: -10 to -25 dB)"
            ),
        )
    res["banks"] = banks
    res["failed"] = bad
    res["ok"] = not bad
    return res
