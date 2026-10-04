"""Board A conventional RF macro (RFS-1 to RFS-6) in U1's frame.

Frame: mm, U1 centre at the origin, +y north. U1 is rotated so its RX edge (ball column 2)
faces north and its TX edge (ball row B) faces east [BD §5.1, §5.3]. Ball positions come from the
SWRS219F ball names and the 0.65 mm pitch [TI-DS Fig. 6-1, §6.2]; no TI layout file is used.

Structures:
- launch (RFS-1, x7): the 0.32 mm RF land, a GCPW trace to P0 (1.3 mm from the ball [TI-RF
  §2.1.2]), an L1 anti-pad, a circular L2 cut-out under the land and two interstitial GND vias
  (through, 0.15/0.35) on the inner diagonals, where the four neighbours are VSSA;
- feeds (RFS-2 RX, RFS-3 TX): 50 ohm GCPW on 4 mil over L2 with a GND via fence on both sides,
  equal length per bank P0 -> Pg (equalizers outside the guard band), then the same straight
  run-in for every column: `runin_out` of fenced GCPW to the cut-out entry E and
  `pour_clear_ant` of microstrip to P1;
- columns (RFS-4): one 2-patch corporate cell (two inset-fed patches on L2 windows, D4, fed at
  their facing edges by a T: 35.4 ohm lambda/4 + two 50 ohm arms, the south arm lambda_g/2 longer
  for the 180° of the facing feeds; the input runs up the column gap), instanced per column and
  mirrored for TX; terminated dummy cells at the bank ends (`dummies`) so that every active column
  sees the same copper and the same neighbours within the cut-out;
- isolation (RFS-5): the L1 GND strip between the bank cut-outs and a via wall along its middle;
- windows and margins (RFS-6): L2 windows `window_margin` beyond every patch; the L1 cut-out is
  the column cells grown by `pour_clear_ant`, so it does not depend on the D12 variant;
- L1 GND stitching: fence rows on the corridor boundary of every feed (shared rows between close
  corridors, a via at each U-turn centre), a fixed lattice of run-in pairs and ring sites round
  each cut-out, the isolation wall, the In2 boundary rows and a grid; GND that no via reaches
  within `stitch_reach` becomes gap (`rfmacro.vias`);
- L3 (In2.Cu) GND: only the RF region outside the package, the under-package ground of rows 1-3
  and A-C and each land's L2 cut-out plus `l3_cut_margin` (elsewhere In2 is the board's escape
  layer).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import dims as dims_mod
from .geom import (
    Path,
    Pt,
    Seg,
    SegIndex,
    circle,
    outline,
    path_samples,
    rect,
    rect_dist,
    serpentine,
)
from .params import D_LATTICE, PKG, RF_BALLS, RULES, length_scale, resolve

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

RX_NAMES = ["RX1", "RX2", "RX3", "RX4"]
TX_NAMES = ["TX1", "TX2", "TX3"]
# terminated dummy cells per `dummies` option: the bank ends beside RX1/RX4 and TX1/TX3
DUMMY_SETS = {
    "both": ("RXD0", "RXD5", "TXD0", "TXD4"),
    "outer": ("RXD0", "TXD4"),
    "none": (),
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
    dummy: bool = False


@dataclass
class Load:
    """Dummy-column termination: a 0201 50 ohm resistor on the run-in axis below Pg."""

    name: str  # the dummy column
    net: str
    ref: str
    centre: Pt
    pad1: List[Pt]  # signal pad (the dummy's net)
    pad2: List[Pt]  # GND pad
    vias: List[Pt]
    zone: Tuple[float, float, float, float]  # keep-clear box for the fit search
    via_zone: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)  # its own vias only
    mask: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)  # solder-mask island


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
    pour_keepouts: List[List[Pt]] = field(default_factory=list)  # the bank cut-outs
    mask_open: List[List[Pt]] = field(default_factory=list)
    l3_gnd: List[List[Pt]] = field(default_factory=list)  # In2.Cu GND outlines (macro-owned)
    ports: Dict[str, Dict[str, object]] = field(default_factory=dict)
    checks: List[Dict[str, object]] = field(default_factory=list)
    region: Dict[str, List[float]] = field(default_factory=dict)
    runins: Dict[str, Path] = field(default_factory=dict)  # dummy run-ins (load -> P1)
    loads: Dict[str, Load] = field(default_factory=dict)
    cutouts: Dict[str, Tuple[float, float, float, float]] = field(default_factory=dict)
    banks: Dict[str, Dict[str, object]] = field(default_factory=dict)
    unstitched: List[List[Pt]] = field(default_factory=list)  # GND made gap (cannot be stitched)
    load_channels: List[List[Pt]] = field(default_factory=list)
    via_log: Dict[str, object] = field(default_factory=dict)
    fit: Dict[str, object] = field(default_factory=dict)
    rows: Dict[str, object] = field(default_factory=dict)  # fence-row bookkeeping for G4


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


# ---- cell, cut-out and rules shared by the fit search and the checks ------------------------


class Rules:
    """Numbers of the RF-uniformity rules for one parameter set."""

    def __init__(self, p: Dict, d: Dict):
        self.w50, self.gap = float(p["w50"]), float(p["gap"])
        self.foff = float(p["fence_offset"])
        self.pitch = float(p["fence_pitch"])
        self.pad = RULES["via_fence"][1]
        self.site_min = self.w50 / 2 + self.gap + self.pad / 2  # 0.46: via centre to a line
        self.smin = 2 * self.site_min  # 0.92: corridors that can share one row
        self.B = float(p["guard_band"])
        self.lout = float(p["runin_out"])
        self.lin = float(p["pour_clear_ant"])
        self.rm = float(p["meander_r"])
        self.half = PKG["body"] / 2
        self.d = D_LATTICE
        self.in_x = float(p["in_x"])
        self.p1_y = float(p["p1_y"])
        W = d["patch"]["w"]
        s = float(p["spacing"])
        # the cell is variant-independent: its top is the longest D12 variant's upper patch
        l_max = d["patch"]["l"] / length_scale(p) * (1 + float(p["bracket_step"]))
        self.cell = (-W / 2, self.p1_y, self.in_x, s / 2 + l_max / 2)
        lz = dict(p["dummy_load"])
        self.load = lz
        self.lchan = self.w50 / 2 + self.gap
        self.fence_start = float(p["fence_start"])
        self.rx_tail = float(p["rx_tail"])
        self.window_margin = float(p["window_margin"])
        self.patch_w = W

    def cut(self, xs_in: List[float], e: float, mirror: bool) -> Tuple[float, float, float, float]:
        """Bank cut-out for column inputs `xs_in` and entry `e` (U1 frame)."""
        x0c, y0c, x1c, y1c = self.cell
        c = self.lin
        oy = e + c - self.p1_y
        xs = []
        for xi in xs_in:
            ox = xi + self.in_x if mirror else xi - self.in_x
            xs += [ox - x1c, ox - x0c] if mirror else [ox + x0c, ox + x1c]
        return (min(xs) - c, oy + y0c - c, max(xs) + c, oy + y1c + c)

    def load_zone(self, xin: float, e: float) -> Tuple[float, float, float, float]:
        zx, zy = self.load["zone"]
        pg = e - self.lout
        return (xin - zx, pg - zy, xin + zx, pg)


def cross_section_min(path: Path, upto: float, rects, ts=(0.0, 0.1, 0.2, 0.3, 0.4, 0.5)) -> float:
    """Smallest distance from the path's cross-sections (up to `ts` either side, i.e. the flat-cap
    corridor of half-width max(ts)) to any rectangle, for arc lengths <= `upto`."""
    best = 1e9
    for q, h, s in path_samples(path, 0.02):
        if s > upto + 1e-9:
            break
        nx, ny = -math.sin(h), math.cos(h)
        for t in ts:
            for sg in (1, -1) if t > 0 else (1,):
                pt = (q[0] + sg * t * nx, q[1] + sg * t * ny)
                for r in rects:
                    dd = rect_dist(pt, r)
                    if dd < best:
                        best = dd
    return best


def _index(paths: Dict[str, Path]) -> SegIndex:
    ix = SegIndex(1.0)
    for n, q in paths.items():
        ix.add_path(q, n)
    return ix


def feed_separation(paths: Dict[str, Path], skip: float = 1.6) -> Tuple[float, Optional[tuple]]:
    """Smallest centreline distance between different feeds, beyond `skip` from each ball."""
    worst: Tuple[float, Optional[tuple]] = (9.0, None)
    names = list(paths)
    ix = {n: _index({n: paths[n]}) for n in names}
    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            for q, _, s in path_samples(paths[a], 0.02):
                if s < skip:
                    continue
                dd = ix[b].dist(q, worst[0])
                if dd < worst[0]:
                    worst = (dd, (a, b, (round(q[0], 3), round(q[1], 3))))
    return worst


# ---- RX bank -------------------------------------------------------------------------------


def _bump_for(need: float, rb: float) -> Tuple[float, float]:
    lo, hi = 1e-4, math.pi / 2 + 0.6
    for _ in range(80):
        th = 0.5 * (lo + hi)
        lo, hi = (th, hi) if 4 * rb * (th - math.sin(th)) < need else (lo, th)
    return th, 4 * rb * math.sin(th)


def _rx_design(R_out: float, R_in: float, rb: float, rx_ball, rx_in, ru: Rules):
    """Outer lines one S-bend of R_out, inner lines R_in plus a symmetric bump of rb, then the
    straight to Pg; returns (rise P0 -> Pg, per-line spec) or None."""
    spec = {}
    for n in RX_NAMES:
        dx = rx_in[n] - rx_ball[n][0]
        R = R_out if n in ("RX1", "RX4") else R_in
        if abs(dx) > 2 * R:
            return None
        th, v, L = _s_bend_len(dx, R)
        spec[n] = dict(R=R, th=th, v=v, L=L, dx=dx)
    ex = {n: s["L"] - s["v"] for n, s in spec.items()}
    top = max(ex.values())
    H = 0.0
    for n, s in spec.items():
        need = top - ex[n]
        if need > 1e-6:
            if n in ("RX1", "RX4"):
                return None
            thb, vb = _bump_for(need, rb)
            if thb > math.pi / 2 + 0.5:
                return None
            s.update(thb=thb, vb=vb)
        else:
            s.update(thb=0.0, vb=0.0)
        # a last arc tighter than the fence offset bulges its concave-side via locus up to
        # (foff - R) past its end: keep that much straight before Pg; and at least `rx_tail` so
        # the fence rows meet the run-in pair on a straight, one fence pitch below it
        tail_min = ru.rx_tail
        s["tail"] = max(tail_min, ru.foff - rb) if s["vb"] > 0 else max(tail_min, ru.foff - R)
        H = max(H, s["v"] + s["vb"] + s["tail"])
    return H, spec


def _rx_paths(spec, rb: float, e: float, rx_ball, p0_y: float, ru: Rules) -> Dict[str, Path]:
    out = {}
    pg = e - ru.lout
    for n in RX_NAMES:
        s = spec[n]
        bx, by = rx_ball[n]
        path = Path((bx, by), math.pi / 2, ru.w50)
        path.straight(p0_y - by).mark("P0")
        sgn = 1 if s["dx"] < 0 else -1  # left turn first for a westward shift
        path.turn(s["R"], sgn * math.degrees(s["th"])).turn(s["R"], -sgn * math.degrees(s["th"]))
        if s["thb"] > 0:
            side = -1 if s["dx"] > 0 else 1
            t = math.degrees(s["thb"])
            path.turn(rb, side * t).turn(rb, -2 * side * t).turn(rb, side * t)
        path.straight(pg - path.pos[1]).mark("Pg")
        path.straight(ru.lout).mark("E").straight(ru.lin).mark("P1")
        out[n] = path
    return out


def _arange(lo: float, hi: float, step: float) -> List[float]:
    n = int(round((hi - lo) / step))
    return [round(lo + step * i, 6) for i in range(n + 1)]


def fit_rx(p: Dict, ru: Rules, rx_ball, rx_in, p0_y: float):
    """Lowest RX entry: S-bend radii and bump from the declared ranges, corridors >= smin."""
    r_lo, r_hi = (float(v) for v in p["rx_bend_r_range"])
    cands = []
    for Ro in _arange(max(r_lo, 0.8), r_hi, 0.05):
        for Ri in _arange(r_lo, r_hi, 0.05):
            for rb in (float(v) for v in p["rx_bump_r"]):
                r = _rx_design(Ro, Ri, rb, rx_ball, rx_in, ru)
                if r:
                    cands.append((r[0], Ro, Ri, rb, r[1]))
    cands.sort(key=lambda t: t[0])
    tried = 0
    for H, Ro, Ri, rb, spec in cands:
        tried += 1
        e = p0_y + H + ru.lout
        paths = _rx_paths(spec, rb, e, rx_ball, p0_y, ru)
        if feed_separation(paths)[0] >= ru.smin - 1e-6:
            return dict(H=H, R_out=Ro, R_in=Ri, rb=rb, spec=spec, tried=tried)
    raise ValueError("RX fit: no S-bend/bump set keeps the corridors apart")


# ---- TX bank -------------------------------------------------------------------------------

# Equalizer allocations tried by the fit, in order: (TX1 north-west fingers on its eastward leg,
# None = fewest; TX1 fingers in the TX1-TX2 lane; TX2 style)
TX_ALLOCS = [(None, 0, "lane"), (None, 1, "lane"), (2, 0, "lane"), (2, 1, "lane"), (3, 0, "lane")]


def _tx_paths(e: float, dx_bank: float, alloc, p: Dict, ru: Rules, tx_ball, p0_x: float):
    nnw, ne1, st2 = alloc
    d = ru.d
    xin = {n: float(p["tx_col_x0"]) + dx_bank + k * d for k, n in enumerate(TX_NAMES)}
    pg = e - ru.lout
    rtx = float(p["bend_r_tx"])
    rm = ru.rm
    manh = {n: (xin[n] - p0_x) + (pg - tx_ball[n][1]) for n in TX_NAMES}
    ref = max(manh.values())
    extra = {n: ref - manh[n] for n in TX_NAMES}
    out, info = {}, {}
    k = 2 * math.pi - 4
    lane = d - 2 * ru.foff  # finger reach in a lane between two legs (shared rows)
    e_lane = k * rm + 2 * (lane - 2 * rm)  # the most one lane finger adds
    for n in TX_NAMES:
        bx, by = tx_ball[n]
        path = Path((bx, by), 0.0, ru.w50)
        path.straight(p0_x - bx).mark("P0")
        if n == "TX1":
            e_east = min(ne1 * e_lane, extra[n])
            e_nw = extra[n] - e_east
            # first finger leg's fence row `fence_start` east of the package body
            xs = ru.half + ru.fence_start + ru.foff - rm
            span = (xin[n] - rtx) - xs
            nmax = int(span // (4 * rm) + 1e-9)
            info[n] = dict(lane_fingers=ne1)
            if e_nw > 1e-9:
                if nmax < 1:
                    return None
                nf = nnw
                if nf is None:
                    nf = next((m for m in range(1, nmax + 1) if (e_nw / m - k * rm) / 2 >= 0), None)
                if nf is None or nf > nmax or (e_nw / nf - k * rm) / 2 < 0:
                    return None
                a_ = (e_nw / nf - k * rm) / 2
                path.straight(xs - p0_x)
                for _ in range(nf):
                    path.turn(rm, 90).straight(a_).turn(rm, -180).straight(a_).turn(rm, 90)
                info[n].update(
                    nw_fingers=nf, nw_a=round(a_, 4), nw_apex_y=round(by + 2 * rm + a_, 4)
                )
            if xin[n] - rtx - path.pos[0] < -1e-9:
                return None
            path.straight(xin[n] - rtx - path.pos[0])
            path.turn(rtx, 90)
            if e_east > 1e-9 and not _lane_fingers(path, e_east, rm, lane, pg):
                return None
        elif n == "TX2":
            path.straight(xin[n] - rtx - p0_x).turn(rtx, 90)
            if st2 != "lane":
                return None
            nf = _lane_fingers(path, extra[n], rm, lane, pg)
            if not nf:
                return None
            info[n] = dict(lane_fingers=nf[0], a=round(nf[2], 4))
        else:
            path.straight(xin[n] - rtx - p0_x).turn(rtx, 90)
            info[n] = dict(lane_fingers=0)
        if path.pos[1] > pg + 1e-9:
            return None
        path.straight(pg - path.pos[1]).mark("Pg")
        path.straight(ru.lout).mark("E").straight(ru.lin).mark("P1")
        want = ref + (math.pi / 2 - 2) * rtx
        if abs((path.marks["Pg"][1] - path.marks["P0"][1]) - want) > 1e-6:
            return None
        out[n] = path
    return out, info, xin


def _lane_fingers(path: Path, extra: float, rm: float, lane: float, pg: float):
    """Fingers of radius rm into the lane east of a northward leg, ending exactly at Pg: the last
    arc's centre then coincides (within 0.05 mm) with the run-in pair at E - guard_band, so the
    inner fence via of that arc and the pair are one via."""
    try:
        n, r, a = serpentine(path, extra, rm, -1, lane, rm, draw=False)
    except ValueError:
        return None
    rise = pg - path.pos[1] - 4 * r * n
    if rise < -1e-9:
        return None
    path.straight(rise)
    serpentine(path, extra, rm, -1, lane, rm)
    return (n, r, a)


def _body_clear(path: Path, s_from: float, ru: Rules) -> float:
    """TX: fence rows east of the package body beyond the fence start."""
    best = 9.0
    for q, h, s in path_samples(path, 0.02):
        if s < s_from:
            continue
        if s > path.marks["Pg"][1]:
            break
        nx, ny = -math.sin(h), math.cos(h)
        for sg in (1, -1):
            x, y = q[0] + sg * ru.foff * nx, q[1] + sg * ru.foff * ny
            if -ru.half <= y <= ru.half:
                best = min(best, x - ru.half)
    return best


def _clear_of_loads(paths: Dict[str, Path], zones, r: float) -> bool:
    for q in paths.values():
        upto = q.marks["Pg"][1]
        for pt, _, s in path_samples(q, 0.02):
            if s > upto:
                break
            for z in zones:
                if rect_dist(pt, z) < r - 1e-9:
                    return False
    return True


def fit_tx(p: Dict, ru: Rules, tx_ball, p0_x: float, rx_cut, rx_paths, rx_loads, dummies):
    """Lowest TX entry (then the smallest eastward shift) at which every equalizer fits outside
    the guard bands, the corridors keep `smin`, TX1's fence clears the package and no feed passes
    a dummy load."""
    d = ru.d
    clear = ru.lin
    strip = float(p["bank_strip"])
    x0 = float(p["tx_col_x0"])
    # the TX cut-out's west edge (its westmost input - clear) >= RX cut-out east edge + strip
    west_in = 1 if "TXD0" in dummies else 0
    need = rx_cut[2] + strip + clear + west_in * d - x0
    dx_min = max(0.0, need)
    e_floor = float(p["tx_col_y"]) + ru.p1_y - clear
    why: Dict[str, int] = {}

    def fail(r):
        why[r] = why.get(r, 0) + 1
        return None

    def try1(e, dxb, alloc):
        r = _tx_paths(e, dxb, alloc, p, ru, tx_ball, p0_x)
        if r is None:
            return fail("paths")
        txp, info, xin = r
        names = dict(xin)
        if "TXD0" in dummies:
            names["TXD0"] = xin["TX1"] - d
        if "TXD4" in dummies:
            names["TXD4"] = xin["TX3"] + d
        cut = ru.cut(list(names.values()), e, True)
        gap = max(cut[0] - rx_cut[2], rx_cut[0] - cut[2], cut[1] - rx_cut[3], rx_cut[1] - cut[3])
        if gap < 2 * ru.B - 1e-9:
            return fail("cut-out to cut-out")
        cuts = [cut, rx_cut]
        for q in list(txp.values()) + list(rx_paths.values()):
            if cross_section_min(q, q.marks["Pg"][1], cuts) < ru.B - 1e-6:
                return fail("band")
        if feed_separation(txp)[0] < ru.smin - 1e-6:
            return fail("separation")
        s_f = ru.half + ru.fence_start - tx_ball["TX1"][0]
        if _body_clear(txp["TX1"], s_f, ru) < ru.fence_start - 1e-6:
            return fail("package")
        zones = [ru.load_zone(names[n], e) for n in ("TXD0", "TXD4") if n in names] + rx_loads
        if not _clear_of_loads({**txp, **rx_paths}, zones, ru.load["zone"][0]):
            return fail("load")
        info["alloc"] = [alloc[0], alloc[1], alloc[2]]
        return dict(dx=dxb, E=e, paths=txp, info=info, xin=xin, names=names, cut=cut)

    def try_e(e, dxb):
        for alloc in TX_ALLOCS:
            f = try1(e, dxb, alloc)
            if f:
                return f
        return None

    for i in range(300):
        dxb = round(dx_min + 0.01 * i, 6)
        hit = None
        for k in range(0, 241):
            e = round(e_floor + 0.05 * k, 6)
            if try_e(e, dxb):
                hit = e
                break
        if hit is None:
            continue
        lo = max(e_floor, hit - 0.05)
        for k in range(0, 11):
            e = round(lo + 0.005 * k, 6)
            f = try_e(e, dxb)
            if f:
                f["why_rejected"] = why
                return f
    raise ValueError(f"TX fit: nothing fits ({why})")


# ---- build ---------------------------------------------------------------------------------


def _load(name: str, net: str, xin: float, e: float, mirror: bool, ru: Rules, idx: int) -> Load:
    lz = ru.load
    pg = e - ru.lout
    plen, pw = (float(v) for v in lz["pad"])
    pitch = float(lz["pitch"])
    c1 = (xin, pg - float(lz["pad1_dy"]))
    c2 = (xin, c1[1] - pitch)
    cen = (xin, c1[1] - pitch / 2)
    p1 = rect(c1[0] - pw / 2, c1[1] - plen / 2, c1[0] + pw / 2, c1[1] + plen / 2)
    p2 = rect(c2[0] - pw / 2, c2[1] - plen / 2, c2[0] + pw / 2, c2[1] + plen / 2)
    vias = [(xin + float(dx), pg + float(dy)) for dx, dy in lz["vias"]]
    vx, vy = (float(v) for v in lz["via_zone"])
    mx, my0, my1 = (float(v) for v in lz["mask"])
    return Load(
        name,
        net,
        f"RT{idx}",  # RT: the board's RL1/RL2 are the radome lands
        cen,
        p1,
        p2,
        vias,
        ru.load_zone(xin, e),
        (xin - vx, pg - vy, xin + vx, pg),
        (xin - mx, pg - my0, xin + mx, pg - my1),
    )


def build(overrides: Dict | None = None) -> Macro:
    p = resolve(overrides)
    d = dims_mod.compute(p)
    mc = Macro(p, d)
    ru = Rules(p, d)
    mir = bool(p["mirror_x"])
    dummies = DUMMY_SETS[str(p["dummies"])]
    half_body = ru.half
    d_l = ru.d

    # ---- RX bank: inputs (east gaps) on the plan's column x ---------------------------------
    rx_ball = {n: ball_xy(RF_BALLS[n], mir) for n in RX_NAMES}
    xc = sum(b[0] for b in rx_ball.values()) / 4
    rx_in = {n: xc + (k - 1.5) * d_l for k, n in enumerate(RX_NAMES)}
    p0_y = rx_ball["RX1"][1] + float(p["launch_len"])
    rfit = fit_rx(p, ru, rx_ball, rx_in, p0_y)
    e_rx = max(p0_y + rfit["H"] + ru.lout, float(p["rx_col_y"]) + ru.p1_y - ru.lin)
    rx_paths = _rx_paths(rfit["spec"], rfit["rb"], e_rx, rx_ball, p0_y, ru)
    rx_cols_in = dict(rx_in)
    if "RXD0" in dummies:
        rx_cols_in["RXD0"] = rx_in["RX1"] - d_l
    if "RXD5" in dummies:
        rx_cols_in["RXD5"] = rx_in["RX4"] + d_l
    rx_cut = ru.cut(list(rx_cols_in.values()), e_rx, False)
    rx_loads = [ru.load_zone(rx_cols_in[n], e_rx) for n in ("RXD0", "RXD5") if n in rx_cols_in]

    # ---- TX bank: fit search (bank shift east, entry height, equalizer lanes) ---------------
    tx_ball = {n: ball_xy(RF_BALLS[n], mir) for n in TX_NAMES}
    p0_x = tx_ball["TX1"][0] + float(p["launch_len"])
    tfit = fit_tx(p, ru, tx_ball, p0_x, rx_cut, rx_paths, rx_loads, dummies)
    e_tx = tfit["E"]
    tx_cols_in = dict(tfit["names"])

    mc.feeds.update(rx_paths)
    mc.feeds.update(tfit["paths"])
    mc.cutouts = {"RX": rx_cut, "TX": tfit["cut"]}
    mc.pour_keepouts = [rect(*rx_cut), rect(*tfit["cut"])]
    order = ["RXD0"] + RX_NAMES + ["RXD5", "TXD0"] + TX_NAMES + ["TXD4"]
    li = 0
    for bank, cols_in, e, mirror in (
        ("RX", rx_cols_in, e_rx, False),
        ("TX", tx_cols_in, e_tx, True),
    ):
        oy = e + ru.lin - ru.p1_y
        for n in [q for q in order if q in cols_in]:
            xi = cols_in[n]
            org = (xi + ru.in_x, oy) if mirror else (xi - ru.in_x, oy)
            col = build_column(f"COL_{n}", n, org, mirror, p, d)
            col.dummy = n not in RX_NAMES + TX_NAMES
            mc.columns[n] = col
            if col.dummy:
                li += 1
                ld = _load(n, n, xi, e, mirror, ru, li)
                mc.loads[n] = ld
                r = Path(_c1(ld), math.pi / 2, ru.w50)
                r.straight(e - ru.lout - _c1(ld)[1]).mark("Pg")
                r.straight(ru.lout).mark("E").straight(ru.lin).mark("P1")
                mc.runins[n] = r
                pw, plen = float(ru.load["pad"][1]), float(ru.load["pad"][0])
                cx, cy = _c1(ld)
                g = ru.gap
                mc.load_channels.append(
                    rect(cx - pw / 2 - g, cy - plen / 2 - g, cx + pw / 2 + g, cy + plen / 2 + g)
                )
        mc.banks[bank] = dict(
            E=e,
            Pg=e - ru.lout,
            top=(rx_cut if bank == "RX" else tfit["cut"])[3],
            mirror=mirror,
            inputs={n: cols_in[n] for n in order if n in cols_in},
            origin_y=oy,
        )
    mc.fit = dict(
        rx=dict(
            R_out=rfit["R_out"],
            R_in=rfit["R_in"],
            bump_r=rfit["rb"],
            rise_p0_pg=rfit["H"],
            E=e_rx,
            candidates_tried=rfit["tried"],
            lines={
                n: dict(R=s["R"], rise=s["v"], bump_rise=s["vb"], tail=s["tail"])
                for n, s in rfit["spec"].items()
            },
        ),
        tx=dict(
            dx_bank=tfit["dx"],
            E=e_tx,
            equalizers=tfit["info"],
            rejected=tfit["why_rejected"],
        ),
        dummies=list(dummies),
    )

    # ---- launch details, ports -----------------------------------------------------------
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
        mc.ports[f"{n}.Pg"] = dict(at=list(path.marks["Pg"][0]), heading_deg=90.0, z0=50.0)
        mc.ports[f"{n}.P1"] = dict(at=list(path.marks["P1"][0]), heading_deg=90.0, z0=50.0)

    # ---- RF region, pour, keepouts, mask -------------------------------------------------
    cuts = list(mc.cutouts.values())
    x_lo = min(c[0] for c in cuts) - 4.0
    x_hi = max(c[2] for c in cuts) + 4.0
    y_hi = max(c[3] for c in cuts) + 4.0
    # the region starts where the east pour does (U1 y -1.3, board y 26.7): nothing of the
    # macro lies further south, and the strip below is the board's (review 2026-10-03)
    mc.region = dict(
        x=[round(x_lo, 4), round(x_hi, 4)], y=[round(-half_body + 3.9, 4), round(y_hi, 4)]
    )
    # L1 GND pour: north strip above the package and the east strip right of it (the macro's
    # own L1 copper; the pour stops at the package outline except around the RF lands)
    mc.pour.append(rect(x_lo, half_body + 0.0, x_hi, y_hi))
    mc.pour.append(rect(half_body, -half_body + 3.9, x_hi, half_body))
    # under-package ground around the RF lands: rows 1-3 (RX) and A-C (TX), as far as the RF
    # balls plus 0.70 mm of coplanar GND beside the outer launches (RX1, RX4, TX1, TX3); the
    # neighbouring foreign lands (P2, D2, A2/B2/C2, B10/C10) keep 0.14 mm
    rxs = [b[0] for b in rx_ball.values()]
    txs = [b[1] for b in tx_ball.values()]
    gw = ru.lchan + 0.70
    mc.pour.append(rect(min(rxs) - gw, rx_ball["RX1"][1] - 0.65 - 0.16, max(rxs) + gw, half_body))
    mc.pour.append(rect(tx_ball["TX1"][0] - 0.65 - 0.16, min(txs) - gw, half_body, max(txs) + gw))
    mc.l3_gnd = [list(q) for q in mc.pour]
    margin = float(p["l3_cut_margin"])
    for n in mc.feeds:
        b = rx_ball.get(n) or tx_ball.get(n)
        mc.l3_gnd.append(circle(b, float(p["l2_cut_r"]) + margin))
    # the mask opening over the RF copper, without the load cells (mask-defined GND lands)
    islands = [ld.mask for ld in mc.loads.values()]
    for r in rects_minus(
        [
            (x_lo, half_body + 0.05, x_hi, y_hi),
            (half_body + 0.05, -half_body + 3.9, x_hi, half_body + 0.05),
        ],
        islands,
    ):
        mc.mask_open.append(rect(*r))
    x_iso = 0.5 * (rx_cut[2] + tfit["cut"][0])
    mc.ports["iso_fence_x"] = dict(at=[x_iso, 0.0])

    # ---- vias: launch first, then the lattice, loads, fences, rings, rows, grid, fill --------
    from . import vias as vias_mod

    vias_mod.place(mc, ru)

    _pocket(mc, ru)

    from . import rules as rules_mod

    rules_mod.generator_checks(mc, ru, rx_ball, tx_ball)
    mc.dims["floorplan"] = dict(
        rx_entry_y=e_rx,
        tx_entry_y=e_tx,
        rx_col_y=mc.banks["RX"]["origin_y"],
        tx_col_y=mc.banks["TX"]["origin_y"],
        p0_rx_y=p0_y,
        p0_tx_x=p0_x,
        tx_dx_bank=tfit["dx"],
    )
    return mc


def rects_minus(rects, holes) -> List[Tuple[float, float, float, float]]:
    """Axis-aligned rectangles minus rectangular holes, as rectangles: each rectangle is cut on
    the holes' x and y lines and the free cells are merged along x, then along y."""
    out = []
    for r in rects:
        hs = [h for h in holes if h[0] < r[2] and h[2] > r[0] and h[1] < r[3] and h[3] > r[1]]
        if not hs:
            out.append(tuple(r))
            continue
        xs = sorted({r[0], r[2]} | {min(max(v, r[0]), r[2]) for h in hs for v in (h[0], h[2])})
        ys = sorted({r[1], r[3]} | {min(max(v, r[1]), r[3]) for h in hs for v in (h[1], h[3])})
        rows = []
        for y0, y1 in zip(ys, ys[1:]):
            row, cur = [], None
            for x0, x1 in zip(xs, xs[1:]):
                cx, cy = 0.5 * (x0 + x1), 0.5 * (y0 + y1)
                free = not any(h[0] < cx < h[2] and h[1] < cy < h[3] for h in hs)
                if free and cur is not None and abs(cur[1] - x0) < 1e-12:
                    cur[1] = x1
                elif free:
                    cur = [x0, x1]
                    row.append(cur)
                else:
                    cur = None
            rows.append((y0, y1, [tuple(c) for c in row]))
        merged = []  # (x0, x1, y0, y1): extend a run of equal x-intervals upward
        open_ = {}
        for y0, y1, row in rows:
            nxt = {}
            for iv in row:
                if iv in open_ and abs(open_[iv][3] - y0) < 1e-12:
                    open_[iv][3] = y1
                    nxt[iv] = open_.pop(iv)
                else:
                    nxt[iv] = [iv[0], iv[1], y0, y1]
            merged += open_.values()
            open_ = nxt
        merged += open_.values()
        out += [(a, c, b, d) for a, b, c, d in merged]
    return [tuple(round(v, 6) for v in r) for r in out]


def _c1(ld: Load) -> Pt:
    xs = [q[0] for q in ld.pad1]
    ys = [q[1] for q in ld.pad1]
    return (0.5 * (min(xs) + max(xs)), 0.5 * (min(ys) + max(ys)))


def _pocket(mc: Macro, ru: Rules) -> None:
    """VOUT_PA pocket: the largest free box north of the package corner between RX4's corridor,
    TX1's corridor, the RX band and any dummy load (fence corridor + 0.2 mm)."""
    half = ru.half
    y0p = half + 0.25
    y1p = min(half + 1.8, mc.cutouts["RX"][1] - ru.B)
    keep = ru.foff + 0.2

    def xs_of(path, ya, yb):
        return [
            q[0] for sg in path.segs for q, _ in sg.sample(0.05) if ya - keep <= q[1] <= yb + keep
        ]

    best = None
    for k in range(0, 31):
        yb = y1p - 0.05 * k
        if yb - y0p < 0.5:
            break
        xr = xs_of(mc.feeds["RX4"], y0p, yb)
        xt = xs_of(mc.feeds["TX1"], y0p, yb)
        px0 = max(xr) + keep if xr else half - 1.0
        px1 = (min(xt) - keep) if xt else half + 1.0
        for ld in mc.loads.values():
            z = ld.zone
            if z[1] - 0.2 < yb and z[3] + 0.2 > y0p:
                if z[0] - 0.2 >= px0:
                    px1 = min(px1, z[0] - 0.2)
                else:
                    px0 = max(px0, z[2] + 0.2)
        if px1 - px0 < 0.3:
            continue
        a = (px1 - px0) * (yb - y0p)
        if best is None or a > best[0] + 1e-9:
            best = (a, [round(px0, 3), round(y0p, 3), round(px1, 3), round(yb, 3)])
    mc.ports["vout_pa_pocket"] = dict(rect=best[1] if best else None)


def all_paths(mc: Macro) -> List[Tuple[str, Path]]:
    out = [(n, f) for n, f in mc.feeds.items()]
    out += [(n, r) for n, r in mc.runins.items()]
    for n, c in mc.columns.items():
        out += [(n, q) for q in _place_paths(c)]
    return out


def channel_outlines(mc: Macro) -> List[List[Pt]]:
    """Pour keepouts that form the GCPW gaps along every feed and run-in, around the load's signal
    pad and around the column paths."""
    lchan = float(mc.params["w50"]) / 2 + float(mc.params["gap"])
    out = [outline(f, half=lchan) for f in mc.feeds.values()]
    out += [outline(r, half=lchan) for r in mc.runins.values()]
    out += [list(q) for q in mc.load_channels]
    for c in mc.columns.values():
        for q in _place_paths(c):
            out.append(outline(q, half=float(mc.params["pour_clear_feed"])))
    return out


def outside_cutouts(mc: Macro) -> Dict[str, object]:
    """Everything the macro draws outside its cut-outs (for the D12 hash, G6)."""

    def segs_upto(path: Path, s_end: float):
        out, s = [], 0.0
        for sg in path.segs:
            if s >= s_end - 1e-9:
                break
            out.append([sg.kind, list(sg.p0), list(sg.p1), sg.width, sg.radius])
            s += sg.length
        return out

    return dict(
        feeds={n: segs_upto(f, f.marks["E"][1]) for n, f in mc.feeds.items()},
        runins={n: segs_upto(r, r.marks["E"][1]) for n, r in mc.runins.items()},
        loads={n: [ld.pad1, ld.pad2, ld.vias] for n, ld in mc.loads.items()},
        cutouts=mc.cutouts,
        vias=sorted([list(v[0]) + [v[3]] for v in mc.vias]),
        pour=mc.pour,
        region=mc.region,
        unstitched=mc.unstitched,
    )
