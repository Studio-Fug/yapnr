"""Board A conventional RF macro (RFS-1 to RFS-6) in U1's frame.

Frame: mm, U1 centre at the origin, +y north. U1 is rotated so its RX edge (ball column 2)
faces north and its TX edge (ball row B) faces east [BD §5.1, §5.3]. Ball positions come from the
SWRS219F ball names and the 0.65 mm pitch [TI-DS Fig. 6-1, §6.2]; no TI layout file is used.

Structures:
- launch (RFS-1, x7): the 0.32 mm RF land, a GCPW trace to P0 (1.3 mm from the ball [TI-RF
  §2.1.2]), an L1 anti-pad, a circular L2 cut-out under the land and two interstitial GND vias
  (through, 0.15/0.35) on the inner diagonals, where the four neighbours are VSSA;
- feeds (RFS-2 RX, RFS-3 TX): 50 ohm GCPW on 4 mil over L2 with a GND via fence on both sides,
  equal electrical length per bank (serpentines where needed), P0 -> P1;
- columns (RFS-4, x7): two inset-fed patches on L2 windows (D4: windows under the radiators
  only) fed at their facing edges by a corporate T (35.4 ohm lambda/4 + two 50 ohm arms, the south
  arm lambda_g/2 longer for the 180° of the facing feeds); the input runs up the column gap;
- isolation (RFS-5): the L1 GND strip and a fence between the RX and TX banks;
- windows and margins (RFS-6): L2 windows `window_margin` beyond every patch; the L1 pour is kept
  `pour_clear_ant` from the radiators.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

from . import dims as dims_mod
from .geom import Path, Pt, circle, outline, path_dist, rect, serpentine
from .params import D_LATTICE, PKG, RF_BALLS, RULES, STACK, resolve

ROWS = "ABCDEFGHJKLMNPR"

# VSSA balls [TI-DS §6.2.2 "VSSA Ground Analog ground"] and the other balls next to the RF edges
# [TI-DS Fig. 6-1]; used for the launch ground and the clearance check against foreign lands.
VSSA = (
    "A1 A3 A5 A7 A9 A13 A15 B1 B3 B5 B7 B9 B14 C1 C3 C4 C5 C6 C7 C8 C9 C14 E1 E2 E3 F3 G1 G2 "
    "G3 H3 J1 J2 J3 K3 L1 L2 L3 M3 N1 N2 N3 R1"
).split()
OTHER_EDGE_BALLS = {
    "A2": "VOUT_PA",
    "B2": "VOUT_PA",
    "C2": "VIN_13RF2",
    "D2": "VIN_13RF2",
    "A10": "VOUT_14APLL",
    "B10": "VBGAP",
    "B11": "VIN_18CLK",
    "B12": "VIN_18VCO",
    "B13": "VOUT_14SYNTH",
    "E5": "VSS",
    "F5": "VIN_18BB",
    "G5": "VIN_13RF1",
    "H5": "VIN_13RF1",
    "J5": "VIN_13RF1",
    "K5": "VIN_18BB",
}


def ball_xy(name: str, mirror_x: bool = False) -> Pt:
    """Ball centre in the U1 frame. Data-sheet figure: row A at the top, column 1 at the left
    [TI-DS Fig. 6-1]; U1 is then rotated by -90° (RX edge north). `mirror_x` flips the column
    axis, for a figure drawn as a bottom view (to be settled against the part-cache footprint,
    board-design.md §7.2)."""
    r = ROWS.index(name[0]) + 1
    c = int(name[1:])
    p = PKG["pitch"]
    x, y = (c - 8) * p, (8 - r) * p
    if mirror_x:
        x = -x
    return (y, -x)


@dataclass
class Column:
    name: str
    net: str
    origin: Pt  # phase centre (U1 frame)
    mirror: bool  # True: input in the west gap
    patches: List[List[Pt]] = field(default_factory=list)
    windows: List[List[Pt]] = field(default_factory=list)
    paths: List[Path] = field(default_factory=list)  # divider and input (column frame -> U1 frame)
    p1: Pt = (0.0, 0.0)
    arm_lengths: Dict[str, float] = field(default_factory=dict)


@dataclass
class Macro:
    params: Dict[str, object]
    dims: Dict[str, object]
    feeds: Dict[str, Path] = field(default_factory=dict)
    columns: Dict[str, Column] = field(default_factory=dict)
    vias: List[Tuple[Pt, float, float, str]] = field(default_factory=list)  # (xy, drill, pad, role)
    l2_cuts: List[List[Pt]] = field(default_factory=list)
    antipads: List[List[Pt]] = field(default_factory=list)
    pour: List[List[Pt]] = field(default_factory=list)  # L1 GND pour outlines (regions)
    pour_keepouts: List[List[Pt]] = field(default_factory=list)
    mask_open: List[List[Pt]] = field(default_factory=list)
    ports: Dict[str, Dict[str, object]] = field(default_factory=dict)
    checks: List[Dict[str, object]] = field(default_factory=list)
    region: Dict[str, List[float]] = field(default_factory=dict)


def _xf(pt: Pt, origin: Pt, mirror: bool) -> Pt:
    x, y = pt
    if mirror:
        x = -x
    return (origin[0] + x, origin[1] + y)


def build_column(name: str, net: str, origin: Pt, mirror: bool, p: Dict, d: Dict) -> Column:
    """One 2-patch corporate column in its frame (phase centre at 0,0; +y along the column;
    the input runs up the +x gap), then placed at `origin` (mirrored for a -x gap input)."""
    col = Column(name, net, origin, mirror)
    W, L = d["patch"]["w"], d["patch"]["l"]
    s = float(p["spacing"])
    ins = d["patch"]["inset"]
    notch = float(p["notch"])
    w50, w35 = float(p["w50"]), float(p["w35"])
    m = float(p["window_margin"])
    lg50 = d["lines"]["lambda_g50"]
    q35 = d["lines"]["quarter35"]
    ty = float(p["t_y"])
    xin = float(p["in_x"])
    edge = s / 2 - L / 2  # facing radiating edges at y = +-edge
    loc = []
    for k, yc in (("lo", -s / 2), ("hi", +s / 2)):
        x0, x1 = -W / 2, W / 2
        y0, y1 = yc - L / 2, yc + L / 2
        nw = w50 / 2 + notch
        if k == "lo":  # feed at the north (facing) edge
            poly = [
                (x0, y0),
                (x1, y0),
                (x1, y1),
                (nw, y1),
                (nw, y1 - ins),
                (-nw, y1 - ins),
                (-nw, y1),
                (x0, y1),
            ]
        else:  # feed at the south (facing) edge
            poly = [
                (x0, y0),
                (-nw, y0),
                (-nw, y0 + ins),
                (nw, y0 + ins),
                (nw, y0),
                (x1, y0),
                (x1, y1),
                (x0, y1),
            ]
        loc.append(poly)
        if p["windows"]:
            col.windows.append(
                [_xf(q, origin, mirror) for q in rect(x0 - m, y0 - m, x1 + m, y1 + m)]
            )
    col.patches = [[_xf(q, origin, mirror) for q in poly] for poly in loc]

    # north arm: T (0, ty) -> upper patch notch bottom (0, edge + ins)
    north = Path((0.0, ty), math.pi / 2, w50)
    north.straight(edge + ins - ty)
    # south arm: lambda_g/2 longer (electrically) to undo the 180° of the facing feeds; the
    # surplus over the straight run is a jog away from the input side
    south = Path((0.0, ty), -math.pi / 2, w50)
    extra = lg50 / 2 - 2 * ty
    rj = 0.20
    b = (extra - (2 * math.pi - 4) * rj) / 2
    if b < 0:
        raise ValueError(f"t_y {ty} too large for the lambda/2 jog")
    a1 = 0.10
    south.straight(a1).turn(rj, -90).straight(b).turn(rj, 90)
    south.turn(rj, 90).straight(b).turn(rj, -90)
    rest = (ty + edge + ins) - (a1 + 4 * rj)
    if rest < 0:
        raise ValueError("no room for the south arm jog")
    south.straight(rest)
    col.arm_lengths = dict(
        north=north.length, south=south.length, diff=south.length - north.length, target=lg50 / 2
    )
    # input: P1 (xin, p1_y) north up the gap, turn west, 50 ohm stub, then the 35 ohm lambda/4 to T
    rin = 0.30
    inp = Path((xin, float(p["p1_y"])), math.pi / 2, w50)
    inp.straight(ty - rin - float(p["p1_y"])).turn(rin, 90)
    l50 = xin - rin - q35
    if l50 < 0:
        raise ValueError("gap input too close for the lambda/4 section")
    inp.straight(l50).mark("t35").straight(q35, width=w35)
    col.paths = [north, south, inp]
    col.p1 = _xf((xin, float(p["p1_y"])), origin, mirror)
    return col


def _place_paths(col: Column) -> List[Path]:
    """Column paths in the U1 frame (rebuilt with transformed geometry)."""
    out = []
    for pth in col.paths:
        q = Path(_xf(pth.start, col.origin, col.mirror), 0.0, pth.width)
        for sg in pth.segs:
            from .geom import Seg

            if sg.kind == "line":
                q.segs.append(
                    Seg(
                        "line",
                        _xf(sg.p0, col.origin, col.mirror),
                        _xf(sg.p1, col.origin, col.mirror),
                        sg.width,
                    )
                )
            else:
                c = _xf(sg.center, col.origin, col.mirror)
                a0 = (math.pi - sg.a0) if col.mirror else sg.a0
                sw = -sg.sweep if col.mirror else sg.sweep
                q.segs.append(
                    Seg(
                        "arc",
                        _xf(sg.p0, col.origin, col.mirror),
                        _xf(sg.p1, col.origin, col.mirror),
                        sg.width,
                        c,
                        sg.radius,
                        a0,
                        sw,
                    )
                )
        out.append(q)
    return out


def _s_bend_len(dx: float, r: float) -> Tuple[float, float, float]:
    th = math.acos(1 - abs(dx) / (2 * r))
    return th, 2 * r * math.sin(th), 2 * r * th


def build(overrides: Dict | None = None) -> Macro:
    p = resolve(overrides)
    d = dims_mod.compute(p)
    mc = Macro(p, d)
    mir = bool(p["mirror_x"])
    w50, g = float(p["w50"]), float(p["gap"])
    pitch = float(p["fence_pitch"])
    foff = float(p["fence_offset"])
    half_body = PKG["body"] / 2
    lchan = w50 / 2 + g  # GCPW channel half-width in the pour

    # ---- RX bank: inputs (east gaps) on the plan's column x, bank shifted west by d/2 ------
    rx_names = ["RX1", "RX2", "RX3", "RX4"]
    rx_ball = {n: ball_xy(RF_BALLS[n], mir) for n in rx_names}
    xc = sum(b[0] for b in rx_ball.values()) / 4
    rx_in_x = {n: xc + (k - 1.5) * D_LATTICE for k, n in enumerate(rx_names)}
    p0_y = rx_ball["RX1"][1] + float(p["launch_len"])
    r_rx = float(p["bend_r_rx"])
    bends = {n: _s_bend_len(rx_in_x[n] - rx_ball[n][0], r_rx) for n in rx_names}
    l_bend = {n: bends[n][2] + 0.0 for n in rx_names}
    v_bend = {n: bends[n][1] for n in rx_names}
    l_max = max(l_bend[n] + (max(v_bend.values()) - v_bend[n]) for n in rx_names)
    # inner lines: a symmetric bump (+th, -2th, +th) of radius rb after the S-bend
    rb = float(p["bump_r_rx"])
    bump = {}
    for n in rx_names:
        need = l_max - (l_bend[n] + (max(v_bend.values()) - v_bend[n]))
        if need < 1e-6:
            bump[n] = (0.0, 0.0)
            continue
        lo, hi = 1e-4, math.pi / 2
        for _ in range(80):
            th = 0.5 * (lo + hi)
            lo, hi = (th, hi) if 4 * rb * (th - math.sin(th)) < need else (lo, th)
        bump[n] = (th, 4 * rb * math.sin(th))
    v_need = max(v_bend.values()) + max(v for _, v in bump.values()) + 0.3
    p1_rx = max(p0_y + v_need, float(p["rx_col_y"]) + float(p["p1_y"]))
    rx_col_y = p1_rx - float(p["p1_y"])
    for k, n in enumerate(rx_names):
        bx, by = rx_ball[n]
        path = Path((bx, by), math.pi / 2, w50)
        path.straight(p0_y - by).mark("P0")
        dx = rx_in_x[n] - bx
        th, vb, _ = bends[n]
        sgn = 1 if dx < 0 else -1  # left turn first for a westward shift
        path.turn(r_rx, sgn * math.degrees(th)).turn(r_rx, -sgn * math.degrees(th))
        thb, vbb = bump[n]
        if thb > 0:
            side = -1 if dx > 0 else 1  # bump away from the bank centre? keep it toward the shift
            path.turn(rb, side * math.degrees(thb)).turn(rb, -2 * side * math.degrees(thb)).turn(
                rb, side * math.degrees(thb)
            )
        path.straight(p1_rx - path.pos[1]).mark("P1")
        mc.feeds[n] = path
        org = (rx_in_x[n] - float(p["in_x"]), rx_col_y)
        mc.columns[n] = build_column(f"COL_{n}", n, org, False, p, d)

    # ---- TX bank: inputs (west gaps, mirrored columns) on the plan's column x -----------------
    tx_names = ["TX1", "TX2", "TX3"]
    tx_ball = {n: ball_xy(RF_BALLS[n], mir) for n in tx_names}
    tx_in_x = {n: float(p["tx_col_x0"]) + k * D_LATTICE for k, n in enumerate(tx_names)}
    r_tx = float(p["bend_r_tx"])
    p0_x = tx_ball["TX1"][0] + float(p["launch_len"])
    manh = {n: (tx_in_x[n] - p0_x) + (0 - tx_ball[n][1]) for n in tx_names}  # relative
    ref = max(manh.values())
    extra = {n: ref - manh[n] for n in tx_names}
    # finger amplitude: TX1 to the west, bounded by the package corner; others by the leg before
    corridor = 2 * foff
    west_bound = {"TX1": half_body + 0.25 + foff}
    west_bound["TX2"] = tx_in_x["TX1"] + corridor
    west_bound["TX3"] = tx_in_x["TX2"] + corridor
    tx_paths = {}
    tops = []
    rmin = float(p["meander_r_min"])
    for n in tx_names:
        bx, by = tx_ball[n]
        path = Path((bx, by), 0.0, w50)
        path.straight(p0_x - bx).mark("P0")
        where = dict(p["tx_meander"]).get(n, "v")
        if where == "h" and extra[n] > 1e-9:
            lead = max(0.0, half_body + 0.25 + foff - rmin - p0_x)
            path.straight(lead)
            room = 9.0  # north of the eastward leg only the pocket and RX4 bound it (checked)
            nf = serpentine(path, extra[n], r_tx, +1, room, rmin)
            path.straight(tx_in_x[n] - r_tx - path.pos[0])
            path.turn(r_tx, 90).straight(0.6)
        else:
            path.straight(tx_in_x[n] - r_tx - p0_x)
            path.turn(r_tx, 90).straight(0.6)
            room = tx_in_x[n] - west_bound[n]
            nf = serpentine(path, extra[n], r_tx, +1, room, rmin)
        tops.append(max(q[1] for sg in path.segs for q in (sg.p0, sg.p1)) + 0.3)
        tx_paths[n] = (path, nf)
    p1_tx = max(max(tops), float(p["tx_col_y"]) + float(p["p1_y"]))
    tx_col_y = p1_tx - float(p["p1_y"])
    for n in tx_names:
        path, nf = tx_paths[n]
        path.straight(p1_tx - path.pos[1]).mark("P1")
        mc.feeds[n] = path
        org = (tx_in_x[n] + float(p["in_x"]), tx_col_y)
        mc.columns[n] = build_column(f"COL_{n}", n, org, True, p, d)

    # ---- launch details, ports, fences ---------------------------------------------------
    via_f = RULES["via_fence"]
    via_b = RULES["via_bga"]
    for n, path in mc.feeds.items():
        b = rx_ball.get(n) or tx_ball.get(n)
        mc.antipads.append(circle(b, float(p["antipad_r"])))
        mc.l2_cuts.append(circle(b, float(p["l2_cut_r"])))
        ang = math.pi / 2 if n.startswith("RX") else 0.0
        inward = (-math.cos(ang), -math.sin(ang))
        side = (-math.sin(ang), math.cos(ang))
        q = PKG["pitch"] / 2
        for sgn in (+1, -1):
            v = (b[0] + q * inward[0] + sgn * q * side[0], b[1] + q * inward[1] + sgn * q * side[1])
            mc.vias.append((v, via_b[0], via_b[1], "launch"))
        p0 = path.marks["P0"][0]
        mc.ports[f"{n}.Pb"] = dict(at=list(b), kind="ball land", note="vendor boundary")
        mc.ports[f"{n}.P0"] = dict(at=list(p0), heading_deg=math.degrees(ang), z0=50.0)
        mc.ports[f"{n}.P1"] = dict(at=list(path.marks["P1"][0]), heading_deg=90.0, z0=50.0)
        # fence from just outside the package to where the pour stops below the column
        start = (half_body + 0.30) - (b[1] if n.startswith("RX") else b[0])
        stop_y = (
            mc.columns[n].origin[1]
            - float(p["spacing"]) / 2
            - mc.dims["patch"]["l"] / 2
            - float(p["pour_clear_ant"])
        )
        tail = max(0.0, path.marks["P1"][0][1] - stop_y) + 0.2
        for v in path.fence(foff, pitch, skip_from=start, skip_to=tail):
            mc.vias.append((v, via_f[0], via_f[1], "fence"))

    # ---- RF region, pour, keepouts, mask -------------------------------------------------
    all_cols = list(mc.columns.values())
    pxs = [q[0] for c in all_cols for poly in c.patches for q in poly]
    pys = [q[1] for c in all_cols for poly in c.patches for q in poly]
    clear = float(p["pour_clear_ant"])
    rx_cols = [mc.columns[n] for n in rx_names]
    tx_cols = [mc.columns[n] for n in tx_names]

    def bank_box(cols):
        xs = [q[0] for c in cols for poly in c.patches for q in poly] + [c.p1[0] for c in cols]
        ys = [q[1] for c in cols for poly in c.patches for q in poly] + [c.p1[1] for c in cols]
        return (min(xs) - clear, min(ys) - clear, max(xs) + clear, max(ys) + clear)

    for box in (bank_box(rx_cols), bank_box(tx_cols)):
        mc.pour_keepouts.append(rect(*box))
    x_lo = min(pxs) - 5.0
    x_hi = max(pxs) + 5.0
    y_hi = max(pys) + 5.0
    mc.region = dict(
        x=[round(x_lo, 3), round(x_hi, 3)], y=[round(-half_body + 1.3, 3), round(y_hi, 3)]
    )
    # L1 GND pour: north strip above the package and the east strip right of it (the macro's
    # own L1 copper; the pour stops at the package outline except around the RF lands)
    mc.pour.append(rect(x_lo, half_body + 0.0, x_hi, y_hi))
    mc.pour.append(rect(half_body, -half_body + 3.9, x_hi, half_body))
    # under-package ground around the RF lands: rows 1-3 (RX) and A-C (TX), as far as the RF balls
    rxs = [b[0] for b in rx_ball.values()]
    txs = [b[1] for b in tx_ball.values()]
    mc.pour.append(
        rect(min(rxs) - 0.65, rx_ball["RX1"][1] - 0.65 - 0.16, max(rxs) + 0.65 + 0.26, half_body)
    )
    mc.pour.append(
        rect(tx_ball["TX1"][0] - 0.65 - 0.16, min(txs) - 0.65, half_body, max(txs) + 0.65)
    )
    mc.mask_open.append(rect(x_lo, half_body + 0.05, x_hi, y_hi))
    mc.mask_open.append(rect(half_body + 0.05, -half_body + 3.9, x_hi, half_body + 0.05))

    # ---- RFS-5: isolation fence between the banks ------------------------------------------
    rx_e = max(q[0] for c in rx_cols for poly in c.patches for q in poly)
    tx_w = min(q[0] for c in tx_cols for poly in c.patches for q in poly)
    x_iso = 0.5 * (max(rx_e, max(c.p1[0] for c in rx_cols) + 0.1) + tx_w)
    y0_iso = half_body + 0.6
    y1_iso = max(pys) + 1.0
    y = y0_iso
    while y <= y1_iso:
        ok = all(
            path_dist((x_iso, y), f) > w50 / 2 + g + via_f[1] / 2 + 0.05 for f in mc.feeds.values()
        )
        if ok:
            mc.vias.append(((x_iso, y), via_f[0], via_f[1], "isolation"))
        y += pitch
    mc.ports["iso_fence_x"] = dict(at=[x_iso, 0.0])

    _filter_vias(mc)

    # ---- VOUT_PA pocket: the free box north of the package corner between RX4 and TX1 -------
    y0p, y1p = half_body + 0.25, half_body + 1.8
    xs_rx = [
        q[0]
        for sg in mc.feeds["RX4"].segs
        for q, _ in sg.sample(0.05)
        if y0p - 0.5 <= q[1] <= y1p + 0.5
    ]
    xs_tx = [
        q[0]
        for sg in mc.feeds["TX1"].segs
        for q, _ in sg.sample(0.05)
        if y0p - 0.5 <= q[1] <= y1p + 0.5
    ]
    px0 = max(xs_rx) + foff + 0.2
    px1 = (min(xs_tx) - foff - 0.2) if xs_tx else half_body + 1.0
    mc.ports["vout_pa_pocket"] = dict(
        rect=[round(px0, 3), round(y0p, 3), round(px1, 3), round(y1p, 3)]
    )

    # ---- checks ----------------------------------------------------------------------------
    _checks(mc, rx_ball, tx_ball, lchan)
    mc.dims["floorplan"] = dict(rx_col_y=rx_col_y, tx_col_y=tx_col_y, p0_rx_y=p0_y, p0_tx_x=p0_x)
    return mc


def _filter_vias(mc: Macro) -> None:
    """Drop GND vias that would sit in a GCPW gap or on another line's copper, in the antenna
    field or a window, or closer than the fab's hole spacing to a via already kept (launch vias
    first, then fences in path order, then the isolation row)."""
    from .geom import poly_contains

    p = mc.params
    w50, g = float(p["w50"]), float(p["gap"])
    paths = [f for f in mc.feeds.values()] + [
        q for c in mc.columns.values() for q in _place_paths(c)
    ]
    lim_min = RULES["fence_pitch_min"]
    kept: List[Tuple[Pt, float, float, str]] = []
    order = {"launch": 0, "fence": 1, "isolation": 2}
    zones = (
        list(mc.pour_keepouts)
        + [w for c in mc.columns.values() for w in c.windows]
        + list(mc.l2_cuts)
    )
    for v in sorted(mc.vias, key=lambda t: order[t[3]]):
        xy, drill, pad, role = v
        if role != "launch":
            if any(path_dist(xy, q, 0.05) < w50 / 2 + g + pad / 2 - 1e-6 for q in paths):
                continue
            if any(poly_contains(z, xy) for z in zones):
                continue
        if any(math.dist(xy, k[0]) < lim_min - 1e-6 for k in kept):
            continue
        kept.append(v)
    mc.vias = kept


def _checks(mc: Macro, rx_ball, tx_ball, lchan: float) -> None:
    p = mc.params
    L = {n: f.length for n, f in mc.feeds.items()}
    lp0 = {n: f.length - f.marks["P0"][1] for n, f in mc.feeds.items()}
    for bank in (("RX1", "RX2", "RX3", "RX4"), ("TX1", "TX2", "TX3")):
        vals = [lp0[n] for n in bank]
        mc.checks.append(
            dict(
                check=f"equal length P0->P1 {bank[0][:2]}",
                lengths_mm={n: round(lp0[n], 4) for n in bank},
                spread_mm=round(max(vals) - min(vals), 4),
                limit_mm=0.036,  # 2 ps design target (0.36 mm) / 10, geometric [BD §6.2]
                ok=max(vals) - min(vals) <= 0.036,
            )
        )
    mc.checks.append(
        dict(check="ball to P1 lengths", lengths_mm={n: round(v, 3) for n, v in L.items()})
    )
    # different-net corridor separation (centrelines >= 2 x fence offset - shared row)
    names = list(mc.feeds)
    worst = (9.0, None)
    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            for sg in mc.feeds[a].segs:
                for q, _ in sg.sample(0.1):
                    if (
                        math.dist(q, mc.feeds[a].start) < 1.0
                        and math.dist(mc.feeds[a].start, mc.feeds[b].start) < 1.4
                    ):
                        continue  # inside the ball field the launch geometry governs
                    dd = path_dist(q, mc.feeds[b], 0.1)
                    if dd < worst[0]:
                        worst = (dd, (a, b, q))
    mc.checks.append(
        dict(
            check="feed corridor separation",
            min_mm=round(worst[0], 3),
            pair=worst[1][:2] if worst[1] else None,
            limit_mm=round(2 * float(p["fence_offset"]), 3),
            ok=worst[0] >= 2 * float(p["fence_offset"]) - 1e-6,
        )
    )
    # column: input line to the window edges of its own and the neighbour column
    c = mc.columns["RX2"]
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
    mc.checks.append(
        dict(check="divider arms", **{k: round(v, 4) for k, v in c.arm_lengths.items()})
    )


def all_paths(mc: Macro) -> List[Tuple[str, Path]]:
    out = [(n, f) for n, f in mc.feeds.items()]
    for n, c in mc.columns.items():
        out += [(n, q) for q in _place_paths(c)]
    return out


def channel_outlines(mc: Macro) -> List[List[Pt]]:
    """Pour keepouts that form the GCPW gaps along every feed and around the column paths."""
    lchan = float(mc.params["w50"]) / 2 + float(mc.params["gap"])
    out = [outline(f, half=lchan) for f in mc.feeds.values()]
    for c in mc.columns.values():
        for q in _place_paths(c):
            out.append(outline(q, half=float(mc.params["pour_clear_feed"])))
    return out
