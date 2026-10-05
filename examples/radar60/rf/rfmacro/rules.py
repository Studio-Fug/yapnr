"""RF-uniformity checks of the macro (G1-G6), shared with the board audit.

Each check appends a record to `Macro.checks` with `ok`; any `ok: false` makes `rfmacro build`
exit non-zero.

- G1 entry and band: every feed (and dummy run-in) reaches its cut-out only through its own
  entry, straight and perpendicular, with a 0.600 mm contact (line plus both gaps); before Pg
  its flat-cap corridor of half-width fence_offset (the via locus and the gap edge) keeps
  guard_band from every cut-out; inside the cut-outs grown by guard_band the only vias are run-in
  pairs and ring sites.
- G2 congruence: every active column's window (column frame, mirrored for TX: x within
  ±(2 d - W/2 - window_margin), symmetric, y from Pg to the cut-out top + 1.0 mm) has the same L1
  copper and L1 GND (XOR <= 1e-3 mm² at a 10 µm raster), the same L2 windows and the same vias
  (1 µm) as an interior column's; the one declared difference (the absent second-neighbour input
  at an open bank end, `allow_rects`) is masked and reported with its size.
- G3 unstitched GND: every L1 GND point of the RF region outside the package lies within
  stitch_reach (geodesic, through GND) of a GND via where it is an edge (within edge_band of a
  gap, cut-out or pour edge) and within stitch_dmax inside the pour (D15: A 0.45, B 0.75); every
  point of the L2-L3 plane pair outside the cut-outs, the package and the PA island within
  l23_dmax (straight line); pieces that are not are listed. `stitch_metrics` reports the
  maxima, the areas beyond 0.45/0.60/0.75 mm, the vias per role and the [D] lattice cut-offs.
- G4 fence continuity: on each feed side, from the fence start to Pg, every row point has a via
  within fence_pitch / 2 + 0.025 (a gap of at most 0.50 mm on the row); run-in pairs present.
- G5 via accounting: no lattice or row via was lost (conflicts), merges only within 0.10 mm,
  dropped optional vias counted by reason, measured row pitches <= the declared ones.
- G6 fits and D12: the equalizers come from the fit search (no fixed room); the other D12
  variants give the same layout outside the cut-outs (hash).
- G7 dummy load cells: the vias in each load's zone are exactly the declared load vias (the four
  terminations are one cell), and no via's pad comes within 0.10 mm of an SMD land.
- G8 PA feed (D14): clearances, the vias in the pocket, the L2 anti-pads, current per via and the
  IR drop [D] (`rfmacro.pa`).
- G9 L2-L3 cavity (K0/K2/K1): the posts and their clearances, the ring-bounded cavity per bank
  (`rfmacro.cavity`).
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Dict, List, Optional, Tuple

from .geom import Path, PointIndex, path_samples, rect_dist
from .params import D_LATTICE, RULES, STACK
from .raster import Grid

RULES_DRILL = RULES["via_fence"][0]

Pt = Tuple[float, float]
H_RASTER = 0.01
REACH_TOL = 0.01  # raster reach is exact to within [0.452, 0.459] mm for 0.46 mm in 3 steps


def _pts(path: Path, step: float = 0.01) -> List[Pt]:
    out = []
    for sg in path.segs:
        sm = sg.sample(step)
        out += [q for q, _ in (sm if not out else sm[1:])]
    return out


def _seg_runs(path: Path, step: float = 0.01) -> List[Tuple[List[Pt], float]]:
    """Per segment: sampled centreline and half-width (the 35 ohm section differs)."""
    return [([q for q, _ in sg.sample(step)], sg.width / 2) for sg in path.segs]


# ---- G3: stitch reach ----------------------------------------------------------------------


def gnd_mask(mc, ru, g: Grid, frame=None):
    """L1 GND of the macro as the fill would make it: pour minus cut-outs, channels, anti-pads,
    load channels, unstitched keepouts and the package body. `frame(pt)` maps U1 -> raster."""
    tf = frame or (lambda q: q)
    m = g.empty()
    zones = getattr(mc, "pour_zones", None) or mc.pour[:2]
    for poly in zones:
        g.polygon(m, [tf(q) for q in poly])
    cut = g.empty()
    for c in mc.cutouts.values():
        g.polygon(cut, [tf(q) for q in ((c[0], c[1]), (c[2], c[1]), (c[2], c[3]), (c[0], c[3]))])
    for poly in list(mc.antipads) + list(mc.unstitched) + list(mc.load_channels):
        g.polygon(cut, [tf(q) for q in poly])
    # a load's GND land is soldered copper, not pour: its own five vias stitch the pour round it,
    # and its far edge (0.5 mm from them) is not a sliver to be stitched or cut away
    for ld in getattr(mc, "loads", {}).values():
        g.polygon(cut, [tf(q) for q in ld.pad2])
    for path in list(mc.feeds.values()) + list(mc.runins.values()):
        g.capsules(cut, [tf(q) for q in _pts(path)], ru.lchan)
    if getattr(mc, "pa", None) is not None:  # D14: the PA copper and its clearance
        from .pa import keepouts

        for r in keepouts(mc.pa, float(mc.params["pa_clear"])):
            g.polygon(
                cut, [tf(q) for q in ((r[0], r[1]), (r[2], r[1]), (r[2], r[3]), (r[0], r[3]))]
            )
    h = ru.half
    g.polygon(cut, [tf(q) for q in ((-h, -h), (h, -h), (h, h), (-h, h))])
    return g.andnot(m, cut)


def _grid(mc, h: float = H_RASTER, margin: float = 0.5) -> Grid:
    """The region's raster, `margin` beyond it so its outer pour edge reads as an edge."""
    x0, x1 = mc.region["x"]
    y0, y1 = mc.region["y"]
    return Grid(x0 - margin, y0 - margin, x1 + margin, y1 + margin, h)


def edge_mask(g: Grid, gnd, band: float):
    """GND pixels within `band` of a pixel that is not GND (a gap, cut-out or pour edge)."""
    return g.and_(gnd, g.dilate([~r & g.full_row for r in gnd], band / g.h))


def stitch_raster(mc, ru) -> Dict[str, object]:
    """Unreached L1 GND: an edge pixel beyond stitch_reach of every via (geodesic), an interior
    pixel beyond stitch_dmax (D15; equal for A)."""
    p = mc.params
    g = _grid(mc)
    gnd = gnd_mask(mc, ru, g)
    seeds = g.empty()
    g.points(seeds, [v[0] for v in mc.vias])
    reach = float(p["stitch_reach"]) + REACH_TOL
    d_int = float(p.get("stitch_dmax") or p["stitch_reach"])
    dmax = max(d_int, float(p["stitch_reach"])) + REACH_TOL
    got = g.geodesic_reach(seeds, gnd, reach, 3)
    if dmax > reach + 1e-9:
        edge = edge_mask(g, gnd, float(p.get("edge_band", 0.30)))
        got_d = g.geodesic_reach(seeds, gnd, dmax, 3)
        bad = g.or_(g.andnot(edge, got), g.andnot(g.andnot(gnd, edge), got_d))
    else:
        edge = None
        bad = g.andnot(gnd, got)
    pieces = []
    for comp in g.components(bad):
        if len(comp) < 4:  # under 4e-4 mm²: raster noise on a corner
            continue
        dsc = g.describe(comp)
        cx, cy = dsc["at"]
        i, j = min(comp, key=lambda t: (g.xc(t[0]) - cx) ** 2 + (g.yc(t[1]) - cy) ** 2)
        dsc["near"] = [g.xc(i), g.yc(j)]
        at_edge = edge is None or any((edge[jj] >> ii) & 1 for ii, jj in comp)
        dsc["limit_mm"] = float(p["stitch_reach"]) if at_edge else d_int
        pieces.append(dsc)
    pieces.sort(key=lambda t: -t["area_mm2"])
    return dict(
        gnd_area_mm2=round(g.area(gnd), 3), reached_mm2=round(g.area(got), 3), pieces=pieces
    )


def stitch_metrics(mc, ru) -> Dict[str, object]:
    """D15 [D]: the L1 GND's largest geodesic distance to a via (edges and interior, bisection on
    G3's 10 um raster), the GND area beyond 0.45 / 0.60 / 0.75 mm, the L2-L3 pair's largest
    via-free circle, the vias per role and the [D] cut-offs of the lattice and of the largest
    gap."""
    from . import pour as pour_mod

    p = mc.params
    g = _grid(mc, H_RASTER)
    gnd = gnd_mask(mc, ru, g)
    edge = edge_mask(g, gnd, float(p["edge_band"]))
    inner = g.andnot(gnd, edge)
    seeds = g.empty()
    g.points(seeds, [v[0] for v in mc.vias])

    def unreached(r, dom):
        un = g.andnot(dom, g.geodesic_reach(seeds, gnd, r, max(3, int(r / 0.15))))
        # pieces under 4 pixels are raster noise on a corner (as G3)
        out = g.empty()
        for comp in g.components(un):
            if len(comp) >= 4:
                for i, j in comp:
                    out[j] |= 1 << i
        return out

    beyond = {
        f"{r:.2f}": round(g.area(unreached(r + REACH_TOL, gnd)), 4) for r in (0.45, 0.60, 0.75)
    }

    def dmax(dom):
        if not g.count(dom):
            return 0.0
        lo, hi = 0.0, 2.0
        if g.count(unreached(hi, dom)):
            return hi
        for _ in range(8):
            r = 0.5 * (lo + hi)
            if g.count(unreached(r, dom)):
                lo = r
            else:
                hi = r
        return round(hi, 3)

    counts: Dict[str, int] = {}
    for v in mc.vias:
        counts[v[3]] = counts.get(v[3], 0) + 1
    gap = pour_mod.largest_gap(mc, ru)
    dr = RULES_DRILL
    return dict(
        mode=p["pour_mode"],
        preset=p["d15"],
        l1_gnd_mm2=round(g.area(gnd), 3),
        l1_edge_mm2=round(g.area(edge), 3),
        l1_dmax_edge_mm=dmax(edge),
        l1_dmax_interior_mm=dmax(inner),
        l1_area_beyond_mm2=beyond,
        l23_largest_gap_radius_mm=gap["radius_mm"],
        l23_largest_gap_at=gap["at"],
        vias_by_role=counts,
        vias_0p15_drill=sum(1 for v in mc.vias if abs(v[1] - 0.15) < 1e-6),
        cutoff_ghz=dict(
            lattice_l1l2=round(
                pour_mod.lattice_cutoff_ghz(float(p["stitch_grid"]), dr, STACK["dk_core"]), 1
            ),
            lattice_l2l3=round(
                pour_mod.lattice_cutoff_ghz(float(p["l23_pitch"]), dr, STACK["dk_bond"]), 1
            ),
            largest_gap_l2l3=round(
                pour_mod.span_cutoff_ghz(gap["radius_mm"], dr, STACK["dk_bond"]), 1
            ),
            note="[D] post-wall (SIW) equivalent width; the gap bound takes 2 r - d as the span",
        ),
    )


# ---- G2: congruence -----------------------------------------------------------------------


def column_frame(col) -> Tuple:
    ox, oy = col.origin
    m = -1.0 if col.mirror else 1.0
    return (lambda q: ((q[0] - ox) * m, q[1] - oy)), m


def window_bounds(ru) -> Tuple[float, float, float, float]:
    """Congruence window in the column frame (input on +x), symmetric (review 2026-10-04: it was
    clipped on the -x side): x within +-(2 d - W/2 - window_margin - 0.01), just short of the L2
    windows of the patches two columns away on both sides, so it holds both neighbours, both
    neighbouring inputs and the second-neighbour inputs at +-1.5 d; y from Pg to the cut-out top
    + 1.0 mm."""
    d = D_LATTICE
    x1 = 2 * d - ru.patch_w / 2 - ru.window_margin - 0.01
    y_pg = ru.p1_y - ru.lin - ru.lout
    y_top = ru.cell[3] + ru.lin + 1.0
    return (-x1, y_pg, x1, y_top)


def allow_rects(ru) -> List[Tuple[float, float, float, float]]:
    """The one declared difference (column frame): at an open bank end (no column two pitches
    away on -x) the second-neighbour input at -1.5 d is absent: its line and gap inside the
    cut-out (x -1.5 d +- the channel half-width) and its run-in with both fence pairs below the
    entry (x -1.5 d +- (fence_offset + pad radius)). Masked out of G2/A2 and reported apart."""
    if getattr(ru, "column", "corporate") == "series":
        return []  # no in-gap input: nothing differs at an open bank end
    d = D_LATTICE
    xa = -1.5 * d
    y_pg = ru.p1_y - ru.lin - ru.lout
    y_e = y_pg + ru.lout
    y_top = ru.cell[3] + ru.lin + 1.0
    wv = ru.foff + ru.pad / 2 + 0.01
    return [
        (xa - wv, y_pg - 0.01, xa + wv, y_e),
        (xa - ru.lchan - 0.01, y_e, xa + ru.lchan + 0.01, y_top),
    ]


def open_end(mc, col) -> bool:
    """True when no column (active or dummy) sits two pitches away on the column frame's -x."""
    tf, _ = column_frame(col)
    return not any(
        abs(tf(c.origin)[0] + 2 * D_LATTICE) < 0.01 and abs(tf(c.origin)[1]) < 0.01
        for c in mc.columns.values()
    )


def missing_neighbour(mc, col) -> bool:
    """True when the column has no neighbour (active or dummy) one pitch away on either side:
    with S1/S1.5 (no input-side dummy) RX4 and TX1 (S1) are edge columns by design."""
    tf, _ = column_frame(col)
    for s in (-1, 1):
        if not any(
            abs(tf(c.origin)[0] - s * D_LATTICE) < 0.01 and abs(tf(c.origin)[1]) < 0.01
            for c in mc.columns.values()
        ):
            return True
    return False


def _window_scene(mc, ru, col, wb):
    from .macro import _place_paths

    tf, _ = column_frame(col)
    g = Grid(wb[0], wb[1], wb[2], wb[3], H_RASTER)
    lo = (wb[0] - 1.0, wb[1] - 1.0, wb[2] + 1.0, wb[3] + 1.0)

    def near(pts):
        return any(lo[0] <= q[0] <= lo[2] and lo[1] <= q[1] <= lo[3] for q in pts)

    cu = g.empty()
    paths = list(mc.feeds.values()) + list(mc.runins.values())
    paths += [q for c in mc.columns.values() for q in _place_paths(c)]
    for path in paths:
        for pts, hw in _seg_runs(path):
            tp = [tf(q) for q in pts]
            if near(tp):
                g.capsules(cu, tp, hw)
    for c in mc.columns.values():
        for poly in c.patches:
            tp = [tf(q) for q in poly]
            if near(tp):
                g.polygon(cu, tp)
    for ld in mc.loads.values():
        for poly in (ld.pad1, ld.pad2):
            g.polygon(cu, [tf(q) for q in poly])
    gnd = gnd_mask(mc, ru, g, tf)
    vias = sorted(
        (round(tf(v[0])[1], 4), round(tf(v[0])[0], 4))
        for v in mc.vias
        if wb[0] <= tf(v[0])[0] <= wb[2] and wb[1] <= tf(v[0])[1] <= wb[3]
    )
    wins = []
    for c in mc.columns.values():
        for poly in c.windows:
            tp = [tf(q) for q in poly]
            xs, ys = [q[0] for q in tp], [q[1] for q in tp]
            if max(xs) < wb[0] or min(xs) > wb[2] or max(ys) < wb[1] or min(ys) > wb[3]:
                continue
            wins.append(tuple(round(v, 4) for v in (min(xs), min(ys), max(xs), max(ys))))
    return g, cu, gnd, vias, sorted(wins)


def congruence(mc, ru) -> Dict[str, object]:
    wb = window_bounds(ru)
    act = [n for n, c in mc.columns.items() if not c.dummy]
    # the reference is an interior column (both second neighbours present), not RX1
    ref_name = next((n for n in act if not open_end(mc, mc.columns[n])), act[0])
    ref = _window_scene(mc, ru, mc.columns[ref_name], wb)
    allow = allow_rects(ru)
    out = {}
    worst = dict(copper=0.0, gnd=0.0, vias=0, windows=0)
    declared_edges = {}
    for n in act:
        if n == ref_name:
            continue
        if missing_neighbour(mc, mc.columns[n]):
            # S1 / S1.5: no dummy beside this edge column; its window differs by design (the
            # trade the owner's S1 option accepts), reported, not gated
            g, cu, gnd, vias, wins = _window_scene(mc, ru, mc.columns[n], wb)
            declared_edges[n] = dict(
                what="edge column without a neighbour (dummies option)",
                copper_xor_mm2=round(g.area(g.xor(cu, ref[1])), 4),
                gnd_xor_mm2=round(g.area(g.xor(gnd, ref[2])), 4),
                via_mismatch=_via_diff(vias, ref[3]),
            )
            continue
        g, cu, gnd, vias, wins = _window_scene(mc, ru, mc.columns[n], wb)
        masked = open_end(mc, mc.columns[n])
        am = g.empty()
        if masked:
            for a in allow:
                g.rect(am, *a)
        x_cu, x_g = g.xor(cu, ref[1]), g.xor(gnd, ref[2])
        a_cu, a_g = g.area(g.andnot(x_cu, am)), g.area(g.andnot(x_g, am))

        def outside(vs):
            return [
                v
                for v in vs
                if not (
                    masked and any(a[0] <= v[1] <= a[2] and a[1] <= v[0] <= a[3] for a in allow)
                )
            ]

        vd = _via_diff(outside(vias), outside(ref[3]))
        wd = 0 if wins == ref[4] else max(1, abs(len(wins) - len(ref[4])))
        out[n] = dict(
            copper_xor_mm2=round(a_cu, 5),
            gnd_xor_mm2=round(a_g, 5),
            via_mismatch=vd,
            l2_window_mismatch=wd,
            open_end=masked,
        )
        if masked:
            out[n]["declared_difference"] = dict(
                what="second-neighbour input at -1.5 d absent (open bank end)",
                copper_xor_mm2=round(g.area(g.and_(x_cu, am)), 4),
                gnd_xor_mm2=round(g.area(g.and_(x_g, am)), 4),
                via_mismatch=_via_diff(vias, ref[3]) - vd,
            )
        worst["copper"] = max(worst["copper"], a_cu)
        worst["gnd"] = max(worst["gnd"], a_g)
        worst["vias"] = max(worst["vias"], vd)
        worst["windows"] = max(worst["windows"], wd)
    return dict(
        reference=ref_name,
        window_column_frame=[round(v, 4) for v in wb],
        declared_difference_column_frame=[[round(v, 4) for v in a] for a in allow],
        vias_in_window=len(ref[3]),
        columns=out,
        edge_columns_declared=declared_edges,
        worst=dict(
            copper_xor_mm2=round(worst["copper"], 5),
            gnd_xor_mm2=round(worst["gnd"], 5),
            via_mismatch=worst["vias"],
            l2_window_mismatch=worst["windows"],
        ),
    )


def _via_diff(a: List[Tuple[float, float]], b: List[Tuple[float, float]], tol: float = 1e-3) -> int:
    """Number of vias of either list without a partner within `tol` in the other."""
    ib = PointIndex(0.5)
    for y, x in b:
        ib.add((x, y))
    used = set()
    miss = 0
    for y, x in a:
        d, j = ib.nearest((x, y), tol)
        if j < 0 or j in used:
            miss += 1
        else:
            used.add(j)
    return miss + (len(b) - len(used))


# ---- G1: entry and band --------------------------------------------------------------------


def _cross_min(path: Path, upto: float, rects, half: float) -> Tuple[float, Optional[Pt]]:
    best, at = 1e9, None
    ts = [half * k / 5 for k in range(6)]
    for q, h, s in path_samples(path, 0.02):
        if s > upto + 1e-9:
            break
        nx, ny = -math.sin(h), math.cos(h)
        for t in ts:
            for sg in (1, -1):
                pt = (q[0] + sg * t * nx, q[1] + sg * t * ny)
                for r in rects:
                    dd = rect_dist(pt, r)
                    if dd < best:
                        best, at = dd, pt
    return best, at


def entry_and_band(mc, ru) -> Dict[str, object]:
    cuts = list(mc.cutouts.values())
    res = {}
    ok = True
    lines = dict(mc.feeds)
    lines.update(mc.runins)
    for n, f in lines.items():
        bank = "RX" if n.startswith("RX") else "TX"
        cut = mc.cutouts[bank]
        s_pg, s_e = f.marks["Pg"][1], f.marks["E"][1]
        band, at = _cross_min(f, s_pg, cuts, ru.foff) if n in mc.feeds else (ru.lout, None)
        # run-in: straight and north from Pg through E
        straight = all(
            sg.kind == "line" and abs(sg.p1[0] - sg.p0[0]) < 1e-9 and sg.p1[1] > sg.p0[1]
            for sg, s0 in _segs_between(f, s_pg, f.length)
        )
        xin = mc.banks[bank]["inputs"][n]
        on_axis = abs(f.marks["Pg"][0][0] - xin) < 1e-9 and abs(f.marks["E"][0][1] - cut[1]) < 1e-9
        span = _contact_span(f, s_e, cut, ru.lchan)
        ok_n = band >= ru.B - 1e-6 and straight and on_axis and abs(span - 2 * ru.lchan) <= 0.005
        ok = ok and ok_n
        res[n] = dict(
            band_min_mm=round(band, 4),
            at=None if at is None else [round(at[0], 3), round(at[1], 3)],
            runin_straight=straight,
            on_input_axis=on_axis,
            contact_span_mm=round(span, 4),
            ok=ok_n,
        )
    # vias inside the cut-outs grown by the band: run-in pairs and ring sites only
    inside = [
        v
        for v in mc.vias
        if any(rect_dist(v[0], c) < ru.B - 1e-6 for c in cuts)
        and v[3] not in ("runin", "ring", "post")
    ]
    # K2 posts are the only vias inside a cut-out (G9 checks them)
    in_cut = [
        v for v in mc.vias if v[3] != "post" and any(rect_dist(v[0], c) <= 1e-9 for c in cuts)
    ]
    ok = ok and not inside and not in_cut
    return dict(
        lines=res,
        foreign_vias_in_band=[[round(v[0][0], 3), round(v[0][1], 3), v[3]] for v in inside],
        vias_in_cutouts=len(in_cut),
        ok=ok,
    )


def _segs_between(path: Path, s_a: float, s_b: float):
    s = 0.0
    out = []
    for sg in path.segs:
        if s >= s_a - 1e-9 and s + sg.length <= s_b + 1e-9:
            out.append((sg, s))
        s += sg.length
    return out


def _contact_span(path: Path, s_e: float, cut, lchan: float) -> float:
    """Length of the cut-out boundary within lchan of the line before the entry (its copper plus
    gap touching the cut-out), measured on all four edges."""
    pts = [q for q, _, s in path_samples(path, 0.005) if s <= s_e + 1e-9]
    x0, y0, x1, y1 = cut
    tot = 0.0
    step = 0.001
    for a, b in (
        ((x0, y0), (x1, y0)),
        ((x1, y0), (x1, y1)),
        ((x1, y1), (x0, y1)),
        ((x0, y1), (x0, y0)),
    ):
        L = math.dist(a, b)
        # coarse filter: only edge stretches near the path's bounding box
        bx = (
            min(q[0] for q in pts) - lchan,
            min(q[1] for q in pts) - lchan,
            max(q[0] for q in pts) + lchan,
            max(q[1] for q in pts) + lchan,
        )
        n = int(L / step)
        for k in range(n + 1):
            t = k / n
            e = (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))
            if not (bx[0] <= e[0] <= bx[2] and bx[1] <= e[1] <= bx[3]):
                continue
            if min(math.dist(e, q) for q in pts) <= lchan + 1e-9:
                tot += step
    return tot


# ---- G4: fence continuity ------------------------------------------------------------------


def fence_rows(mc, ru, pl) -> Dict[str, List]:
    """Fence row points per feed side (they depend on the lines only, not on the vias)."""
    from .vias import fence_row, fence_start

    out = {}
    for n, f in mc.feeds.items():
        s0 = fence_start(n, f, ru)
        for side, tag in ((1, "L"), (-1, "R")):
            out[f"{n}.{tag}"] = fence_row(f, side, s0, f.marks["Pg"][1], pl, n)
    return out


def fence_sequences(mc, ru, pl, bbox=None, rows=None) -> Dict[str, Dict[str, object]]:
    """Per feed side, from the fence start to Pg: walk the fence row and take, at every row point,
    the nearest GND via within 0.60 mm (on the row's side); consecutive distinct vias in that walk
    are the fence's consecutive vias. Returns per row the worst spacing and every pair over the
    limit (0.50 mm), and row points with no via within 0.60 mm (an opening)."""
    lim = 0.50
    out = {}
    rows = rows or fence_rows(mc, ru, pl)
    for key, pts in rows.items():
        if bbox and not any(
            q is not None and bbox[0] <= q[0] <= bbox[2] and bbox[1] <= q[1] <= bbox[3]
            for _, q in pts
        ):
            continue
        if True:
            prev = None
            worst, bad, holes = 0.0, [], []
            first = True
            for _, q in pts:
                if q is None:
                    prev = None
                    continue
                if bbox and not (bbox[0] <= q[0] <= bbox[2] and bbox[1] <= q[1] <= bbox[3]):
                    prev = None
                    continue
                d, j = pl.idx.nearest(q, 0.60)
                if j < 0:
                    holes.append([round(q[0], 3), round(q[1], 3)])
                    prev = None
                    continue
                if first and d > ru.pitch / 2 + 0.025:
                    holes.append([round(q[0], 3), round(q[1], 3)])
                first = False
                if prev is not None and j != prev:
                    dd = math.dist(pl.idx.pts[prev], pl.idx.pts[j])
                    worst = max(worst, dd)
                    if dd > lim + 1e-9:
                        bad.append((prev, j, round(dd, 4)))
                prev = j
            out[key] = dict(worst=worst, bad=bad, holes=holes)
    return out


def fence_continuity(mc, ru) -> Dict[str, object]:
    from .vias import Placer

    pl = Placer(mc, ru)
    for v in mc.vias:
        pl.idx.add(v[0])
    seqs = fence_sequences(mc, ru, pl)
    res = {}
    ok = True
    for k, r in seqs.items():
        pairs = [
            dict(
                a=[round(c, 3) for c in pl.idx.pts[i]], b=[round(c, 3) for c in pl.idx.pts[j]], mm=d
            )
            for i, j, d in r["bad"]
        ]
        res[k] = dict(
            max_spacing_mm=round(r["worst"], 3), over_limit=pairs[:5], openings=r["holes"][:5]
        )
        if pairs or r["holes"]:
            ok = False
    # run-in pairs at E - runin_inset and E - guard_band on both sides of every input
    si = float(mc.params["runin_inset"])
    missing = []
    for bank, b in mc.banks.items():
        for n, xi in b["inputs"].items():
            for y in (b["E"] - si, b["E"] - ru.B):
                for x in (xi - ru.foff, xi + ru.foff):
                    if pl.idx.nearest((x, y), 0.10)[1] < 0:
                        missing.append([n, round(x, 3), round(y, 3)])
    ok = ok and not missing
    return dict(limit_mm=0.50, rows=res, runin_pairs_missing=missing, ok=ok)


# ---- G5: via accounting --------------------------------------------------------------------


def _row_pitch(vias: List[Pt], a: Pt, b: Pt, band: float = 0.13) -> float:
    """Largest spacing between consecutive vias within `band` of the segment a-b."""
    L = math.dist(a, b)
    ux, uy = (b[0] - a[0]) / L, (b[1] - a[1]) / L
    ts = sorted(
        (q[0] - a[0]) * ux + (q[1] - a[1]) * uy
        for q in vias
        if abs(-(q[0] - a[0]) * uy + (q[1] - a[1]) * ux) <= band
        and -0.01 <= (q[0] - a[0]) * ux + (q[1] - a[1]) * uy <= L + 0.01
    )
    return max((t1 - t0 for t0, t1 in zip(ts, ts[1:])), default=0.0)


def via_accounting(mc, ru) -> Dict[str, object]:
    lg = mc.via_log
    counts: Dict[str, int] = {}
    for v in mc.vias:
        counts[v[3]] = counts.get(v[3], 0) + 1
    merged_far = [m for m in lg.get("merged", []) if m["d"] > 0.10 + 1e-9]
    sp, si = float(mc.params["stitch_pitch"]), float(mc.params["stitch_inset"])
    x_lo, x_hi = mc.region["x"]
    y_s, y_hi = mc.region["y"]
    pts = [v[0] for v in mc.vias]
    pitches = dict(
        west=_row_pitch(pts, (x_lo + si, ru.half + si), (x_lo + si, y_hi - si)),
        north=_row_pitch(pts, (x_lo + si, y_hi - si), (x_hi - si, y_hi - si)),
        east=_row_pitch(pts, (x_hi - si, y_s + si), (x_hi - si, y_hi - si)),
    )
    ok = not lg.get("conflicts") and not merged_far and max(pitches.values()) <= sp + 1e-6
    return dict(
        counts=counts,
        total=len(mc.vias),
        merged=len(lg.get("merged", [])),
        merged_detail=lg.get("merged", [])[:20],
        conflicts=lg.get("conflicts", []),
        row_gaps=lg.get("row_gaps", []),
        dropped_optional=lg.get("dropped", {}),
        boundary_row_pitch_mm={k: round(v, 4) for k, v in pitches.items()},
        stitch_pitch_max_mm=sp,
        fill_rounds=lg.get("fill_rounds"),
        made_gap=lg.get("made_gap"),
        lattice_sites=lg.get("lattice_sites"),
        ok=ok,
    )


# ---- G7: dummy load cells ------------------------------------------------------------------


def load_cells(mc, ru) -> Dict[str, object]:
    """Every dummy load is the same cell: the vias in its zone are exactly its declared vias
    (load frame: x from the run-in axis, y from Pg, within 1 um), and no via's pad comes within
    SMD_VIA_CLEAR of either land (no via in or beside an SMD land)."""
    from .vias import SMD_VIA_CLEAR

    pr = ru.pad / 2
    want = sorted(
        (round(float(dy), 4), round(float(dx), 4)) for dx, dy in mc.params["dummy_load"]["vias"]
    )
    res, ok = {}, True
    for n, ld in mc.loads.items():
        z = ld.via_zone
        xi, pg = 0.5 * (z[0] + z[2]), z[3]
        got = sorted(
            (round(v[0][1] - pg, 4), round(v[0][0] - xi, 4))
            for v in mc.vias
            if z[0] - 1e-9 <= v[0][0] <= z[2] + 1e-9 and z[1] - 1e-9 <= v[0][1] <= z[3] + 1e-9
        )
        diff = _via_diff(got, want)
        lands = [_bbox4(ld.pad1), _bbox4(ld.pad2)]
        near = [
            [
                round(v[0][0], 3),
                round(v[0][1], 3),
                v[3],
                round(min(rect_dist(v[0], b) for b in lands) - pr, 3),
            ]
            for v in mc.vias
            if min(rect_dist(v[0], b) for b in lands) < pr + SMD_VIA_CLEAR - 1e-9
        ]
        under = []  # line copper under the mask island (only the load's own run-in may be)
        for nm, path in list(mc.feeds.items()) + list(mc.runins.items()):
            if nm == n:
                continue
            for q, _, _ in path_samples(path, 0.02):
                if rect_dist(q, ld.mask) < ru.lchan - 1e-9:
                    under.append(nm)
                    break
        good = diff == 0 and not near and not under
        ok = ok and good
        res[n] = dict(
            ref=ld.ref,
            vias_in_zone=len(got),
            mismatch=diff,
            vias_at_lands=near,
            lines_under_mask=under,
            ok=good,
        )
    return dict(declared_vias=len(want), smd_via_clear_mm=SMD_VIA_CLEAR, loads=res, ok=ok)


def _bbox4(poly) -> Tuple[float, float, float, float]:
    xs, ys = [q[0] for q in poly], [q[1] for q in poly]
    return (min(xs), min(ys), max(xs), max(ys))


# ---- G6: fits and D12 ----------------------------------------------------------------------


def layout_digest(mc) -> str:
    from .macro import outside_cutouts

    o = outside_cutouts(mc)
    o = dict(o)
    o.pop("vias")
    o.pop("unstitched")
    return _sha(o)


def outside_digest(mc) -> str:
    from .macro import outside_cutouts

    return _sha(outside_cutouts(mc))


def _sha(o) -> str:
    def rnd(x):
        if isinstance(x, float):
            return round(x, 5)
        if isinstance(x, dict):
            return {str(k): rnd(v) for k, v in x.items()}
        if isinstance(x, (list, tuple)):
            return [rnd(v) for v in x]
        return x

    return hashlib.sha256(json.dumps(rnd(o), sort_keys=True).encode()).hexdigest()


def d12_layouts(mc) -> Dict[str, str]:
    """Layout hash (feeds to the entries, cut-outs, loads, pour, region) of every D12 variant,
    from the fit search alone (no via placement)."""
    from . import macro as M

    out = {}
    for v in (-1, 0, 1):
        ov = {k: mc.params[k] for k in mc.params if k != "variant"}
        ov["variant"] = v
        out[str(v)] = layout_only_digest(M, ov)
    return out


def layout_only_digest(M, ov) -> str:
    from . import dims as dims_mod
    from .params import resolve

    p = resolve(ov)
    d = dims_mod.compute(p)
    ru = M.Rules(p, d)
    mir = bool(p["mirror_x"])
    dummies = M.DUMMY_SETS[str(p["dummies"])]
    rx_ball = {n: M.ball_xy(M.RF_BALLS[n], mir) for n in M.RX_NAMES}
    xc = sum(b[0] for b in rx_ball.values()) / 4
    rx_in = {n: xc + (k - 1.5) * ru.d for k, n in enumerate(M.RX_NAMES)}
    p0_y = rx_ball["RX1"][1] + float(p["launch_len"])
    rfit = M.fit_rx(p, ru, rx_ball, rx_in, p0_y)
    e_rx = max(p0_y + rfit["H"] + ru.lout, float(p["rx_col_y"]) + ru.p1_y - ru.lin)
    rx_paths = M._rx_paths(rfit["spec"], rfit["rb"], e_rx, rx_ball, p0_y, ru)
    cols = dict(rx_in)
    if "RXD0" in dummies:
        cols["RXD0"] = rx_in["RX1"] - ru.d
    if "RXD5" in dummies:
        cols["RXD5"] = rx_in["RX4"] + ru.d
    rx_cut = ru.cut(list(cols.values()), e_rx, False)
    rx_loads = [ru.load_zone(cols[n], e_rx) for n in ("RXD0", "RXD5") if n in cols]
    rx_masks = [ru.load_mask(cols[n], e_rx) for n in ("RXD0", "RXD5") if n in cols]
    tx_ball = {n: M.ball_xy(M.RF_BALLS[n], mir) for n in M.TX_NAMES}
    p0_x = tx_ball["TX1"][0] + float(p["launch_len"])
    t = M.fit_tx(p, ru, tx_ball, p0_x, rx_cut, rx_paths, rx_loads, dummies, rx_masks)
    o = dict(
        rx=[[s.kind, list(s.p0), list(s.p1)] for q in rx_paths.values() for s in q.segs],
        tx=[[s.kind, list(s.p0), list(s.p1)] for q in t["paths"].values() for s in q.segs],
        cuts=[rx_cut, t["cut"]],
        cols=[cols, t["names"]],
    )
    return _sha(o)


# ---- all generator checks ------------------------------------------------------------------


def generator_checks(mc, ru, rx_ball, tx_ball) -> None:
    p = mc.params
    lp0 = {n: f.marks["Pg"][1] - f.marks["P0"][1] for n, f in mc.feeds.items()}
    lp1 = {n: f.length - f.marks["P0"][1] for n, f in mc.feeds.items()}
    for bank in (("RX1", "RX2", "RX3", "RX4"), ("TX1", "TX2", "TX3")):
        vals = [lp1[n] for n in bank]
        # 2 ps design target (0.36 mm) / 10, geometric [BD §6.2]; T4 adds the owner-waived skew
        lim = 0.036 + (ru.skew_mm if bank[0].startswith("TX") else 0.0)
        mc.checks.append(
            dict(
                check=f"equal length P0->P1 {bank[0][:2]}",
                lengths_mm={n: round(lp1[n], 4) for n in bank},
                p0_to_pg_mm={n: round(lp0[n], 4) for n in bank},
                spread_mm=round(max(vals) - min(vals), 4),
                limit_mm=round(lim, 4),
                skew_budget_ps=float(p["tx_skew_budget_ps"]) if bank[0].startswith("TX") else 0.0,
                ok=max(vals) - min(vals) <= lim + 1e-9,
            )
        )
    mc.checks.append(
        dict(
            check="ball to P1 lengths",
            lengths_mm={n: round(f.length, 3) for n, f in mc.feeds.items()},
        )
    )
    from .macro import feed_separation

    sep = feed_separation(mc.feeds)
    mc.checks.append(
        dict(
            check="feed corridor separation",
            min_mm=round(sep[0], 3),
            pair=list(sep[1][:2]) if sep[1] else None,
            limit_mm=round(ru.smin, 3),
            ok=sep[0] >= ru.smin - 1e-6,
        )
    )
    W = mc.dims["patch"]["w"]
    m = float(p["window_margin"])
    gap_edge = D_LATTICE / 2 - W / 2 - m - float(p["w50"]) / 2
    mc.checks.append(
        dict(
            check="column input line to L2 window edge (both sides)",
            clearance_mm=round(gap_edge, 4),
            limit_mm=round(STACK["h_core"] * 1.5, 4),
            ok=gap_edge >= STACK["h_core"] * 1.5,
        )
    )
    c = mc.columns["RX2"]
    mc.checks.append(
        dict(check="divider arms", **{k: round(v, 4) for k, v in c.arm_lengths.items()})
    )
    # patch clearance to the pour (the cut-out is the variant-independent cell)
    worst = 9.0
    for col in mc.columns.values():
        cut = mc.cutouts["RX" if col.name.startswith("COL_RX") else "TX"]
        for poly in col.patches:
            for q in poly:
                worst = min(worst, q[0] - cut[0], cut[2] - q[0], q[1] - cut[1], cut[3] - q[1])
    mc.checks.append(
        dict(
            check="patch to L1 pour (cut-out edge)",
            min_mm=round(worst, 4),
            limit_mm=float(p["pour_clear_ant"]),
            ok=worst >= float(p["pour_clear_ant"]) - 1e-6,
        )
    )

    g1 = entry_and_band(mc, ru)
    mc.checks.append(dict(check="G1 entry contact and guard band", **g1))
    g2 = congruence(mc, ru)
    w = g2["worst"]
    ok2 = (
        w["copper_xor_mm2"] <= 1e-3
        and w["gnd_xor_mm2"] <= 1e-3
        and w["via_mismatch"] == 0
        and w["l2_window_mismatch"] == 0
    )
    mc.checks.append(dict(check="G2 column congruence", **g2, ok=ok2))
    g3 = stitch_raster(mc, ru)
    from . import pour as pour_mod

    l23 = pour_mod.l23_raster(mc, ru)
    mc.checks.append(
        dict(
            check="G3 unstitched GND",
            d15=p["d15"],
            pour_mode=p["pour_mode"],
            reach_mm=float(p["stitch_reach"]),
            edge_band_mm=float(p["edge_band"]),
            interior_dmax_mm=float(p["stitch_dmax"]),
            l23_dmax_mm=float(p["l23_dmax"]),
            raster_mm=H_RASTER,
            gnd_area_mm2=g3["gnd_area_mm2"],
            unreached_pieces=[{k: v for k, v in pc.items() if k != "near"} for pc in g3["pieces"]],
            made_gap=mc.via_log.get("made_gap"),
            l23_domain_mm2=l23["domain_mm2"],
            l23_unstitched=[{k: v for k, v in pc.items() if k != "near"} for pc in l23["pieces"]],
            ok=not g3["pieces"] and not l23["pieces"],
        )
    )
    mc.checks.append(dict(check="stitch metrics (D15)", **stitch_metrics(mc, ru)))
    g4 = fence_continuity(mc, ru)
    mc.checks.append(dict(check="G4 fence continuity", **g4))
    g5 = via_accounting(mc, ru)
    mc.checks.append(dict(check="G5 via accounting", **g5))
    g7 = load_cells(mc, ru)
    mc.checks.append(dict(check="G7 dummy load cells", **g7))
    if getattr(mc, "pa", None) is not None:
        from . import pa as pa_mod

        mc.checks.append(dict(check="G8 PA feed (D14)", **pa_mod.check(mc, ru)))
    from . import cavity as cavity_mod

    mc.checks.append(dict(check="G9 L2-L3 cavity", **cavity_mod.check(mc, ru)))
    t3 = mc.fit["tx"]["order_planarity"]
    mc.checks.append(
        dict(
            check="T3 TX ball-to-column orders (L-routes on L1) [D]",
            planar=t3["planar"],
            crossing={k: v["crossing"] for k, v in t3["orders"].items() if v["crossing"]},
            used="-".join(p["tx_order"]),
        )
    )
    d12 = d12_layouts(mc)
    same = len(set(d12.values())) == 1
    mc.checks.append(
        dict(
            check="G6 fits and D12",
            equalizers=mc.fit,
            band_margin_mm=min(v["band_min_mm"] for v in g1["lines"].values()),
            layout_sha256_per_variant=d12,
            outside_cutouts_sha256=outside_digest(mc),
            ok=same,
        )
    )
