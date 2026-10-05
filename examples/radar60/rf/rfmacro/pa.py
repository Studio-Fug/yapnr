"""D14: the VOUT_PA (1V0_PA) feed of the macro (owner 2026-10-04).

The board's rules leave balls A2/B2 no via site (stage 3a §5.3): an interstitial 0.20/0.40 via
misses the 0.10 mm via-to-land rule, the PWR clearance is 0.15 mm, and the RF region bars every
site outside the package. The owner's option 1: the macro draws a short L1 feed from A2/B2 into
the VOUT_PA pocket (the one place beside the corner outside the RF region) and ends it on
through vias there, which tie to the L4 1V0 pour and carry the bottom-side PA caps. The board
rules stay as they are; the feed is part of the macro the RF sign-off simulates.

Geometry (U1 frame, `mirror_x` False; derived, not drawn by hand):
- bar: A2 and B2 joined on L1 between ball rows 1 and 3, `pa_clear` from the row-1 and row-3
  lands and from the top of TX1's 0.70 mm launch ground (the TX under-package pour);
- neck: from the bar north to the package edge, east of row A (`pa_clear` beyond A1's land)
  and `pa_clear` from TX1's west fence row (the row's via pads), inside the package outline
  (east of it is the RF region);
- vias: `pa_vias` through vias of `pa_via` on a lattice of hole-to-hole 0.28 mm (the board's
  profile) anchored at the neck's top, each pad wholly inside the pocket, `pa_clear` from every
  fence-row via pad and fixed GND via, the L2 anti-pad (pad + `pa_clear`) at least
  `pa_antipad_feed_min` from every feed centreline, chosen nearest the neck first and joined
  through their copper squares (pad + `pa_copper_margin`).

The copper is axis-aligned rectangles (their union); the KiCad board writes them as pads of one
footprint on the PA net, so the DRC checks every clearance. `check` is G8.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .geom import SegIndex, path_samples, rect_dist
from .params import PKG, RULES, STACK

Pt = Tuple[float, float]
Rect = Tuple[float, float, float, float]

RHO_CU = 1.72e-8  # ohm m at 20 C [D]
HOLE_TO_HOLE = 0.28  # via to via, the profile's pcbway-adv-6l-rf_via_hole_to_hole [BD dru]
VIA_TO_LAND = 0.10  # via copper to an SMD pad (the profile) [BD dru]
H_FR4_L3_L4 = 0.51  # In2-In3 core [BD stackup, kicad header]
T_L3 = 0.0175


@dataclass
class PAFeed:
    net: str
    balls: Dict[str, Pt]
    bar: Rect
    neck: Rect
    squares: List[Rect]
    bridges: List[Rect]
    vias: List[Pt]
    drill: float
    pad: float
    pocket: Rect  # the board pocket: the macro's pocket down to the package edge
    mask_island: Rect
    info: Dict[str, object] = field(default_factory=dict)

    @property
    def rects(self) -> List[Rect]:
        return [self.bar, self.neck] + list(self.squares) + list(self.bridges)


def _lands(mir: bool, skip) -> List[Tuple[str, Pt]]:
    from .macro import OTHER_EDGE_BALLS, RF_BALLS, VSSA, ball_xy

    names = set(VSSA) | set(OTHER_EDGE_BALLS) | set(RF_BALLS.values())
    return [(b, ball_xy(b, mir)) for b in sorted(names) if b not in skip]


def _row_loci(mc, ru) -> List[Pt]:
    """Fence-row loci of every feed: points at fence_offset either side of the centreline from
    the fence start to Pg (where the vias of the fence rows can sit)."""
    from .vias import fence_start

    out = []
    for n, f in mc.feeds.items():
        s0, s1 = fence_start(n, f, ru), f.marks["Pg"][1]
        for q, h, s in path_samples(f, 0.02):
            if s < s0 or s > s1:
                continue
            nx, ny = -math.sin(h), math.cos(h)
            for sg in (1, -1):
                out.append((q[0] + sg * ru.foff * nx, q[1] + sg * ru.foff * ny))
    return out


def _rect_pts_min(r: Rect, pts: List[Pt]) -> float:
    return min((rect_dist(q, r) for q in pts), default=9.0)


def _rect_rect(a: Rect, b: Rect) -> float:
    dx = max(b[0] - a[2], 0.0, a[0] - b[2])
    dy = max(b[1] - a[3], 0.0, a[1] - b[3])
    return math.hypot(dx, dy)


def build(mc, ru) -> PAFeed:
    from .macro import ball_xy

    p = mc.params
    mir = bool(p["mirror_x"])
    if mir:
        raise NotImplementedError("pa_feed: drawn for mirror_x False (the TX1/RX4 corner)")
    clr = float(p["pa_clear"])
    lr = PKG["land"] / 2
    half = ru.half
    gpr = RULES["via_fence"][1] / 2  # GND fence via pad radius
    drill, pad = (float(v) for v in p["pa_via"])
    pr = pad / 2
    h = pr + float(p["pa_copper_margin"])
    balls = {b: ball_xy(b, mir) for b in p["pa_balls"]}
    outer = max(balls, key=lambda b: balls[b][0])  # A2 (row A, the package's east row)
    inner = min(balls, key=lambda b: balls[b][0])  # B2
    yb = balls[outer][1]
    assert abs(balls[inner][1] - yb) < 1e-9, "PA balls in one package column"
    lands = _lands(mir, set(p["pa_balls"]))

    # bar: between the lands north and south of the ball column, and the TX launch ground
    bx0 = balls[inner][0] - lr
    near = [q for _, q in lands if bx0 - lr - clr - 0.5 <= q[0] <= half]
    by1 = min(q[1] - lr - clr for q in near if q[1] > yb + 1e-6)
    by0 = max(q[1] + lr + clr for q in near if q[1] < yb - 1e-6)
    tx_pour = mc.pour[3]  # TX under-package pour: TX1's 0.70 mm launch ground
    tp = (tx_pour[0][0], tx_pour[0][1], tx_pour[2][0], tx_pour[2][1])
    by0 = max(by0, tp[3] + clr)
    west = [q for _, q in lands if abs(q[1] - yb) < 1e-6 and q[0] < balls[inner][0]]
    assert all(bx0 - (q[0] + lr) >= clr - 1e-9 for q in west), "bar to the land west of B2"

    # neck: east of the outer row's lands, west of TX1's fence-row pads, inside the package
    nx0 = balls[outer][0] + lr + clr
    loci = _row_loci(mc, ru)
    fixed = [v[0] for v in mc.vias] + [q for ld in mc.loads.values() for q in ld.vias]
    ny1 = half + h
    nx1 = half
    while nx1 > nx0 and (
        _rect_pts_min((nx0, by0, nx1, ny1), loci) < gpr + clr - 1e-9
        or _rect_pts_min((nx0, by0, nx1, ny1), fixed) < gpr + clr - 1e-9
    ):
        nx1 = round(nx1 - 0.005, 6)
    neck = (nx0, by0, nx1, ny1)
    bar = (bx0, by0, nx1, by1)

    # vias: lattice from the neck's top corner, wholly inside the pocket
    pk = mc.ports["vout_pa_pocket"]["rect"]
    pocket = (pk[0], half, pk[2], pk[3])
    step = max(drill + HOLE_TO_HOLE, pad + VIA_TO_LAND)
    step = math.ceil(step * 100 - 1e-9) / 100
    v0 = (nx1 - h, half + h)
    lines = SegIndex(1.0)
    for n, f in list(mc.feeds.items()) + list(mc.runins.items()):
        lines.add_path(f, n)
    islands = [ld.mask for ld in mc.loads.values()]
    lpads = [_bb(ld.pad1) for ld in mc.loads.values()] + [_bb(ld.pad2) for ld in mc.loads.values()]
    amin = pr + clr + float(p["pa_antipad_feed_min"])

    def why_not(c: Pt) -> Optional[str]:
        sq = (c[0] - h, c[1] - h, c[0] + h, c[1] + h)
        if not (
            c[0] - pr >= pocket[0] - 1e-9
            and c[0] + pr <= pocket[2] + 1e-9
            and c[1] - pr >= pocket[1] - 1e-9
            and c[1] + pr <= pocket[3] + 1e-9
        ):
            return "pocket"
        if sq[2] > nx1 + 1e-9:
            return "neck"
        if _rect_pts_min(sq, loci) < gpr + clr - 1e-9:
            return "fence row"
        if _rect_pts_min(sq, fixed) < gpr + clr - 1e-9:
            return "fixed via"
        if any(_rect_rect(sq, r) < clr - 1e-9 for r in lpads):
            return "load land"
        if any(_rect_rect(sq, r) < 0.05 - 1e-9 for r in islands):
            return "load mask island"
        if lines.dist(c, amin + 0.5) < amin - 1e-9:
            return "anti-pad to feed"
        return None

    cands = []
    for i in range(0, 8):
        for j in range(0, 6):
            c = (round(v0[0] - i * step, 6), round(v0[1] + j * step, 6))
            cands.append(((i, j), c))
    cands.sort(key=lambda t: (math.dist(t[1], v0), t[0]))
    chosen: Dict[Tuple[int, int], Pt] = {}
    rejected: Dict[str, int] = {}
    want = int(p["pa_vias"])
    while len(chosen) < want:
        added = False
        for ij, c in cands:
            if ij in chosen:
                continue
            if chosen and not any(
                abs(ij[0] - a) + abs(ij[1] - b) == 1 for a, b in chosen
            ):  # joined to the copper already chosen
                continue
            w = why_not(c)
            if w:
                rejected[w] = rejected.get(w, 0) + 1
                continue
            chosen[ij] = c
            added = True
            break
        if not added:
            break
    squares = [(c[0] - h, c[1] - h, c[0] + h, c[1] + h) for c in chosen.values()]
    bridges = []
    keys = sorted(chosen)
    for a in keys:
        for b in keys:
            if b <= a or abs(a[0] - b[0]) + abs(a[1] - b[1]) != 1:
                continue
            ca, cb = chosen[a], chosen[b]
            bridges.append(
                (
                    min(ca[0], cb[0]) - h,
                    min(ca[1], cb[1]) - h,
                    max(ca[0], cb[0]) + h,
                    max(ca[1], cb[1]) + h,
                )
            )
    allr = squares + bridges
    xs0 = min(r[0] for r in allr + [neck])
    xs1 = max(r[2] for r in allr + [neck])
    ys1 = max(r[3] for r in allr)
    island = (round(xs0 - 0.05, 4), round(half, 4), round(xs1 + 0.05, 4), round(ys1 + 0.05, 4))
    feed = PAFeed(
        net=str(p["pa_net"]),
        balls=balls,
        bar=tuple(round(v, 4) for v in bar),
        neck=tuple(round(v, 4) for v in neck),
        squares=[tuple(round(v, 4) for v in r) for r in squares],
        bridges=[tuple(round(v, 4) for v in r) for r in bridges],
        vias=[(round(c[0], 4), round(c[1], 4)) for _, c in sorted(chosen.items())],
        drill=drill,
        pad=pad,
        pocket=tuple(round(v, 4) for v in pocket),
        mask_island=island,
        info=dict(lattice_step_mm=step, rejected_sites=rejected, wanted=want),
    )
    return feed


def _bb(poly) -> Rect:
    xs, ys = [q[0] for q in poly], [q[1] for q in poly]
    return (min(xs), min(ys), max(xs), max(ys))


def keepouts(pa: PAFeed, clr: float) -> List[Rect]:
    """The PA copper grown by `clr`: no GND copper or GND via pad inside (pour gap, site rule)."""
    return [(r[0] - clr, r[1] - clr, r[2] + clr, r[3] + clr) for r in pa.rects]


# ---- G8 ------------------------------------------------------------------------------------


def electrical(pa: PAFeed, p: Dict) -> Dict[str, object]:
    """[D] IR drop, current per via, loop inductance of the feed for the 1.0 V rail's peak and
    RMS current (sized for the whole rail: TI publishes no PA split)."""
    i_pk, i_rms = float(p["pa_i_peak"]), float(p["pa_i_rms"])
    t = STACK["t_l1"] * 1e-3
    rs = RHO_CU / t  # ohm per square
    n = len(pa.vias)
    r_in = pa.drill / 2 * 1e-3
    tp = float(p["pa_plating_um"]) * 1e-6
    # via barrel from L1 to the L4 1V0 pour (In3's top face) [BD stackup]
    l_l1_l4 = (STACK["h_core"] + STACK["t_l2"] + STACK["h_bond"] + T_L3 + H_FR4_L3_L4) * 1e-3
    a_barrel = math.pi * ((r_in + tp) ** 2 - r_in**2)
    r_via = RHO_CU * l_l1_l4 / a_barrel if n else float("inf")
    nx0, ny0, nx1, ny1 = pa.neck
    bx0, by0, bx1, by1 = pa.bar
    w_neck, w_bar = nx1 - nx0, by1 - by0
    y_bar = 0.5 * (by0 + by1)
    sq_neck = (pa.pocket[1] - y_bar) / w_neck  # bar centre to the package edge (via patch)
    xn = 0.5 * (nx0 + nx1)
    bs = sorted(pa.balls.items(), key=lambda kv: -kv[1][0])  # outer (A2) first
    sq_outer = (xn - bs[0][1][0]) / w_bar
    sq_between = (bs[0][1][0] - bs[-1][1][0]) / w_bar
    # the via array and its patch as a resistor ladder: each via from the L4 pour (R_via), the
    # squares joined to their lattice neighbours (one step of copper 2 h wide), the current
    # leaving at the square on the neck
    h = (pa.squares[0][2] - pa.squares[0][0]) / 2 if pa.squares else 0.25
    v0 = (nx1 - h, ny1 - h)
    r_patch_array = _ladder(pa.vias, v0, r_via, rs, 2 * h)
    r_neck = rs * sq_neck
    drop_outer = i_pk * (r_patch_array + r_neck + rs * sq_outer)
    drop_inner = drop_outer + (i_pk / 2) * rs * sq_between  # equal split between the balls
    # IPC-2221 external conductor, 10 C rise: I = 0.048 dT^0.44 A^0.725 (A in mil^2) [D]
    a_mil2 = (w_neck / 0.0254) * (STACK["t_l1"] / 0.0254)
    i_ipc = 0.048 * 10**0.44 * a_mil2**0.725
    # partial self-inductance of one via, L1 to the bottom caps (L6), 0.2 h (ln(4h/d) + 1) nH
    hb = 1.17
    l_via = 0.2 * hb * (math.log(4 * hb / pa.drill) + 1)
    return dict(
        sheet_mohm_per_sq=round(rs * 1e3, 4),
        squares=dict(
            neck=round(sq_neck, 3),
            bar_to_outer=round(sq_outer, 3),
            outer_to_inner=round(sq_between, 3),
        ),
        via_mohm_each_l1_l4=round(r_via * 1e3, 3),
        via_array_and_patch_mohm=round(r_patch_array * 1e3, 3),
        vias=n,
        i_peak_per_via_a=round(i_pk / max(n, 1), 3),
        i_rms_per_via_a=round(i_rms / max(n, 1), 3),
        ir_drop_mv=dict(outer=round(drop_outer * 1e3, 2), inner=round(drop_inner * 1e3, 2)),
        neck_width_mm=round(w_neck, 4),
        bar_width_mm=round(w_bar, 4),
        neck_ipc2221_10c_a=round(i_ipc, 3),
        via_self_inductance_nh_each=round(l_via, 3),
        via_inductance_parallel_nh_no_mutual=round(l_via / max(n, 1), 3),
        note=(
            "copper at 20 C, 35 um L1, equal split between the balls; the via barrels to the L4 "
            "pour; sized for the whole 1.0 V rail (2.5 A peak, 1.0 A RMS)"
        ),
    )


def _ladder(vias: List[Pt], v0: Pt, r_via: float, rs: float, width: float) -> float:
    """Resistance from the L4 pour to the square at `v0` through the vias and the squares
    joined to their lattice neighbours (nodal analysis, Gaussian elimination)."""
    n = len(vias)
    if not n:
        return float("inf")
    k0 = min(range(n), key=lambda k: math.dist(vias[k], v0))
    step = min((math.dist(a, b) for i, a in enumerate(vias) for b in vias[i + 1 :]), default=1.0)
    G = [[0.0] * n for _ in range(n)]
    for k in range(n):
        G[k][k] += 1.0 / r_via
        for j in range(k + 1, n):
            dd = math.dist(vias[k], vias[j])
            if dd <= step + 1e-6:
                g = width / (rs * dd)
                G[k][k] += g
                G[j][j] += g
                G[k][j] -= g
                G[j][k] -= g
    b = [0.0] * n
    b[k0] = 1.0  # 1 A drawn at the neck's square: V = R
    for c in range(n):  # Gaussian elimination (symmetric positive definite)
        piv = G[c][c]
        for r_ in range(c + 1, n):
            f = G[r_][c] / piv
            if f:
                for cc in range(c, n):
                    G[r_][cc] -= f * G[c][cc]
                b[r_] -= f * b[c]
    v = [0.0] * n
    for r_ in range(n - 1, -1, -1):
        v[r_] = (b[r_] - sum(G[r_][cc] * v[cc] for cc in range(r_ + 1, n))) / G[r_][r_]
    return v[k0]


def check(mc, ru) -> Dict[str, object]:
    pa: PAFeed = mc.pa
    p = mc.params
    clr = float(p["pa_clear"])
    lr = PKG["land"] / 2
    half = ru.half
    gpr = RULES["via_fence"][1] / 2
    pr = pa.pad / 2
    res: Dict[str, object] = dict(net=pa.net, balls=sorted(pa.balls), vias=len(pa.vias))
    bad: List[str] = []
    rects = pa.rects
    # every PA ball inside the copper
    for b, q in pa.balls.items():
        if not any(rect_dist(q, r) <= 1e-9 for r in rects):
            bad.append(f"ball {b} not on the copper")
    # foreign lands
    lands = _lands(False, set(pa.balls))
    d_land = min(min(rect_dist(q, r) for r in rects) - lr for _, q in lands)
    res["min_to_foreign_land_mm"] = round(d_land, 4)
    if d_land < clr - 1e-6:
        bad.append("foreign land")
    # GND vias (pads) and the PA vias' holes
    d_via = min((min(rect_dist(v[0], r) for r in rects) - v[2] / 2 for v in mc.vias), default=9.0)
    res["min_to_gnd_via_pad_mm"] = round(d_via, 4)
    if d_via < clr - 1e-6:
        bad.append("GND via")
    h2h = min(
        (math.dist(c, v[0]) - pa.drill / 2 - v[1] / 2 for c in pa.vias for v in mc.vias),
        default=9.0,
    )
    res["min_hole_to_hole_mm"] = round(h2h, 4)
    if h2h < HOLE_TO_HOLE - 1e-6:
        bad.append("hole to hole")
    # RF and load copper
    d_rf = 9.0
    for n, f in list(mc.feeds.items()) + list(mc.runins.items()):
        for q, _, _ in path_samples(f, 0.02):
            d_rf = min(d_rf, min(rect_dist(q, r) for r in rects) - f.width / 2)
    res["min_to_rf_copper_mm"] = round(d_rf, 4)
    if d_rf < clr - 1e-6:
        bad.append("RF copper")
    # the TX launch ground keeps its 0.70 mm: the pour's top stays clear of the bar
    tp = mc.pour[3]
    d_tx = pa.bar[1] - tp[2][1]
    res["bar_to_tx_launch_ground_mm"] = round(d_tx, 4)
    if d_tx < clr - 1e-6:
        bad.append("TX launch ground")
    # copper inside the package outline or the pocket (east of the package edge below it is the
    # RF region); vias wholly inside the pocket
    pk = pa.pocket

    def placed_ok(r: Rect) -> bool:
        if r[2] <= half + 1e-9 and r[3] <= half + 1e-9:
            return True
        return (
            r[0] >= pk[0] - 1e-9
            and r[2] <= pk[2] + 1e-9
            and r[3] <= pk[3] + 1e-9
            and (r[2] <= half + 1e-9 or r[1] >= half - 1e-9)
        )

    outside = [r for r in rects if not placed_ok(r)]
    res["copper_outside_pocket_or_package"] = [list(r) for r in outside]
    if outside:
        bad.append("copper outside the pocket and the package")
    v_out = [
        list(c)
        for c in pa.vias
        if not (
            c[0] - pr >= pk[0] - 1e-9
            and c[0] + pr <= pk[2] + 1e-9
            and c[1] - pr >= pk[1] - 1e-9
            and c[1] + pr <= pk[3] + 1e-9
        )
    ]
    res["vias_outside_pocket"] = v_out
    if v_out:
        bad.append("via outside the pocket")
    # L2 anti-pads to the feed centrelines
    ix = SegIndex(1.0)
    for n, f in list(mc.feeds.items()) + list(mc.runins.items()):
        ix.add_path(f, n)
    d_ap = min((ix.dist(c, 5.0) for c in pa.vias), default=9.0) - pr - clr
    res["l2_antipad_to_feed_centreline_mm"] = round(d_ap, 4)
    res["l2_antipad_radius_mm"] = round(pr + clr, 4)
    if d_ap < float(p["pa_antipad_feed_min"]) - 1e-6:
        bad.append("L2 anti-pad near a feed")
    # widths and current
    el = electrical(pa, p)
    res["electrical"] = el
    if el["neck_width_mm"] < 0.25 - 1e-9:
        bad.append("neck narrower than the PWR class width 0.25")
    if len(pa.vias) < int(p["pa_vias"]):
        bad.append(f"only {len(pa.vias)} of {p['pa_vias']} vias fit")
    if el["i_peak_per_via_a"] > float(p["pa_via_i_max"]) + 1e-9:
        bad.append("current per via")
    if max(el["ir_drop_mv"].values()) > float(p["pa_ir_max"]) * 1e3 + 1e-9:
        bad.append("IR drop")
    res["limits"] = dict(
        clearance_mm=clr,
        via_i_max_a=float(p["pa_via_i_max"]),
        ir_max_mv=float(p["pa_ir_max"]) * 1e3,
        antipad_feed_min_mm=float(p["pa_antipad_feed_min"]),
        hole_to_hole_mm=HOLE_TO_HOLE,
        gnd_via_pad_r=gpr,
    )
    res["geometry"] = dict(
        bar=list(pa.bar),
        neck=list(pa.neck),
        vias=[list(c) for c in pa.vias],
        via=[pa.drill, pa.pad],
        pocket=list(pa.pocket),
        lattice=pa.info,
    )
    res["failed"] = bad
    res["ok"] = not bad
    return res
