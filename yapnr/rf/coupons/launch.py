"""Edge-SMA launches of the OSH Park Order 0 boards: the Cinch 142-0701-851 footprint and the
launch transitions of region M and region W (Order 0 design §5, work package WP4).

- **Region M**: L1 microstrip over In1.Cu (0.200 mm of FR408HR). The connector's tab sits on a
  grounded-coplanar pin pad whose In1.Cu is cut out, so the pad references In2.Cu 1.21 mm down;
  a short taper narrows the pad to the M line while the cut-out closes, so the reference moves
  from In2.Cu back to In1.Cu at a constant 2D impedance.
- **Region W**: L1 over B.Cu with In1.Cu and In2.Cu removed (1.39 mm): the same pin pad, then a
  horn-shaped coplanar taper up to the 3 mm W line.

Every number of the transition is a 2D quasi-static solve (`xsec`, scikit-fem + gmsh, the FEA
environment): `python -m yapnr.rf.coupons.launch design --region M` reruns them. The results are
shipped in `data/launch-<board>.json` (`load`, `design`), so the layout and the tests need numpy
only. Nothing here is 3D-tuned: a 3D (openEMS) check of the bare edge under the tab, the reference
change and the legs' return path is still to come.

Coordinates of a launch: x from the nominal milled edge into the board (mm), y across the stick
from the launch axis. The connector's flange face sits on the edge (x = 0).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

C_LIGHT = 299792458.0
IN = 25.4  # mm per inch


# --- the connector (manufacturer drawing) -------------------------------------------------------


@dataclass(frozen=True)
class EdgeSMA:
    """An end-launch SMA jack for a 0.062 in board, from the manufacturer's drawing [C-851]:
    the body and contact (product drawing) and the recommended launch (end-launch table)."""

    part: str
    source: str
    body_w: float  # flange width across the board edge
    flange_h: float  # flange height (board normal)
    flange_t: float  # flange thickness (along x)
    leg_len: float  # legs, from the flange face onto the board
    leg_w: float  # leg width (along the edge), legs flush with the flange sides
    leg_t_top: float  # top leg thickness
    slot: float  # opening between top and bottom legs (the board goes in)
    tab_w: float  # centre contact: a flat tab, width x thickness
    tab_t: float
    tab_len: float  # tab, from the flange face onto the board
    # end-launch table ("A".."E", in) and the plated through holes of the launch drawing
    lay_a: float
    lay_b: float
    lay_c: float  # inner distance between the two ground pads
    lay_d: float  # outer distance across the ground pads
    lay_e: float  # ground and signal pad length from the edge
    pth_drill: float
    pth_pad: float
    pth_dx: float  # PTH centre beyond the pad end
    pth_y: float  # PTH centre from the axis

    @property
    def leg_y(self) -> Tuple[float, float]:
        """|y| span of a leg: the legs are flush with the flange sides."""
        return (self.body_w / 2 - self.leg_w, self.body_w / 2)

    @property
    def gnd_pad_y(self) -> Tuple[float, float]:
        return (self.lay_c / 2, self.lay_d / 2)


# Cinch Connectivity Solutions (Johnson) 142-0701-851, "SMA 50 Ohm End Launch Jack Receptacle -
# Tab Contact", product information sheet (read 2026-10-02): body .375 (9.52) x .312 (7.92),
# flange .065 (1.65); legs 2X .040 (1.02) wide, .187 (4.75) long; slot "A" .068 (1.73) and bottom
# leg "B" .083 (2.11) for a .062 (1.57) board; centre contact "Ø.020 (0.51) X .010 (0.25) TAB
# CONTACT", .075 (1.91) to contact. End Launch Connectors page of the same sheet, row
# 142-0701-851/861 (.375 body, .062 board): A .103, B .090, C .250, D .440, E .200 in; 2X Ø.018
# plated through holes on Ø.038 pads, .035 beyond E and .170 from the centre line. The top leg's
# thickness (1.02 mm) is read from the side view (the 2X .040 there brackets the top leg).
CINCH_142_0701_851 = EdgeSMA(
    part="Cinch 142-0701-851",
    source="Cinch 142-0701-851 product information sheet (drawing and end-launch table), 2026-10-02",
    body_w=0.375 * IN,
    flange_h=0.312 * IN,
    flange_t=0.065 * IN,
    leg_len=0.187 * IN,
    leg_w=0.040 * IN,
    leg_t_top=0.040 * IN,
    slot=0.068 * IN,
    tab_w=0.020 * IN,
    tab_t=0.010 * IN,
    tab_len=0.075 * IN,
    lay_a=0.103 * IN,
    lay_b=0.090 * IN,
    lay_c=0.250 * IN,
    lay_d=0.440 * IN,
    lay_e=0.200 * IN,
    pth_drill=0.018 * IN,
    pth_pad=0.038 * IN,
    pth_dx=0.035 * IN,
    pth_y=0.170 * IN,
)


# --- the board cross-section ----------------------------------------------------------------------


@dataclass(frozen=True)
class Board4L:
    """A 4-layer cross-section at the design frequency (mm). FR408HR values: OSH Park's 4-layer
    page and construction drawing [O-4l, O-4l-stack]; the 5 GHz εr scale OSH Park's values by
    Isola's 1 -> 5 GHz trend, 3.69 -> 3.64 [O-fr408] (derived, as in the stackup's rf block)."""

    id: str
    t_out: float  # L1 and L4 finished copper
    t_in: float  # In1/In2
    h_pp: float  # each prepreg
    h_core: float
    er_pp: float
    er_core: float
    mask_t: float
    mask_er: float
    keepback: float  # copper from the nominal milled edge, every layer
    min_space: float
    f_ref_ghz: float = 5.0

    @property
    def h_m(self) -> float:
        """L1 to In1.Cu (region M)."""
        return self.h_pp

    @property
    def h_cut(self) -> float:
        """L1 to In2.Cu where In1.Cu is cut (resin fills the removed copper)."""
        return self.h_pp + self.t_in + self.h_core

    @property
    def h_w(self) -> float:
        """L1 to B.Cu with both inner layers removed (region W)."""
        return 2 * self.h_pp + self.h_core


OSH_FR408HR = Board4L(
    id="oshpark-4l-fr408hr",
    t_out=0.04318,
    t_in=0.01727,
    h_pp=0.1999,
    h_core=0.9906,
    er_pp=round(3.61 * 3.64 / 3.69, 3),
    er_core=round(3.87 * 3.64 / 3.69, 3),
    mask_t=0.01524,
    mask_er=3.90,
    keepback=0.381,
    min_space=0.127,
)
# The EM528 alternate [O-alt]: 7.67 mil 2 x 3313 prepreg, Dk 3.76 / core 4.11 at 1 GHz; scaled to
# 5 GHz by EM-528's 1 -> 10 GHz trend (about -2.5 % per decade, derived). Sensitivity only.
OSH_EM528 = Board4L(
    id="oshpark-4l-em528",
    t_out=0.04318,
    t_in=0.01727,
    h_pp=0.1948,
    h_core=0.9906,
    er_pp=3.71,
    er_core=4.06,
    mask_t=0.01524,
    mask_er=3.90,
    keepback=0.381,
    min_space=0.127,
)


def section(
    bd: Board4L,
    region: str,
    w: float,
    gap: Optional[float],
    cut: Optional[float] = None,
    tab: Optional[EdgeSMA] = None,
    fillet: float = 0.5,
    mask_gap: bool = False,
    inner: Optional[float] = None,
    gnd_to: Optional[float] = None,
    width: Optional[float] = None,
    air: Optional[float] = None,
    etch: float = 0.0,
) -> Tuple[dict, Dict[str, float]]:
    """The 2D cross-section of a launch station for `xsec.build`, and its εr per region.

    region "M": `cut` None is the M line over solid In1.Cu (z = 0 is In1.Cu). With `cut`, In1.Cu
    is removed for |y| < cut and z = 0 is In2.Cu (the core, then In1.Cu's copper outside the
    cut-out, then the prepreg, resin-filled inside it).
    region "W": z = 0 is B.Cu; prepreg, core, prepreg; In1/In2 only for |y| >= `inner` (the
    stitched perimeter ring).
    L1: the strip |y| <= w/2 and coplanar grounds from w/2 + gap to `gnd_to` (gap None: none).
    `tab`: the connector's tab on the strip, with a solder fillet wetting `fillet` of the tab
    height at its side and running out at the pad edge (a 4-step staircase).
    `mask_gap`: mask on the substrate in the gaps and over the grounds (not over the pad).
    `etch`: per copper edge on L1 (the strip narrower, the gaps wider)."""
    t = bd.t_out
    if region == "M":
        W = width or 12.0
        if cut is None:
            z1 = bd.h_pp
            slabs = [("pp", 0.0, z1)]
            conds = []
        else:
            zc = bd.h_core
            z1 = zc + bd.t_in + bd.h_pp
            slabs = [("core", 0.0, zc), ("pp", zc, z1)]
            conds = []
            if cut < W / 2:
                conds += [
                    ("In1a", -W / 2, -cut, zc, zc + bd.t_in),
                    ("In1b", cut, W / 2, zc, zc + bd.t_in),
                ]
    elif region == "W":
        W = width or 16.0
        z_a, z_b = bd.h_pp, bd.h_pp + bd.h_core
        z1 = 2 * bd.h_pp + bd.h_core
        slabs = [("pp", 0.0, z_a), ("core", z_a, z_b), ("pp", z_b, z1)]
        conds = []
        if inner is not None and inner < W / 2:
            for k, zz in enumerate((z_a, z_b)):
                h = bd.t_in / 2
                conds += [
                    (f"I{k}a", -W / 2, -inner, zz - h, zz + h),
                    (f"I{k}b", inner, W / 2, zz - h, zz + h),
                ]
    else:
        raise ValueError(region)
    air = air if air is not None else (5.0 if region == "M" else 10.0)
    zt = z1 + t
    slabs.append(("air", z1, zt + air))
    we = w - 2 * etch
    conds.append(("S", -we / 2, we / 2, z1, zt))
    blocks = []
    if gap is not None:
        g0 = we / 2 + gap + 2 * etch
        g1 = (gnd_to if gnd_to is not None else W / 2 - 0.3) - etch
        if g0 < g1:
            conds += [("G1", -g1, -g0, z1, zt), ("G2", g0, g1, z1, zt)]
            if mask_gap:
                m = bd.mask_t
                blocks += [
                    ("mask", -g0, -we / 2, z1, z1 + m),
                    ("mask", we / 2, g0, z1, z1 + m),
                    ("mask", -g1, -g0 + m, z1, zt + m),
                    ("mask", g0 - m, g1, z1, zt + m),
                ]
    if tab is not None:
        tw, tt = tab.tab_w / 2, tab.tab_t
        conds.append(("S", -tw, tw, zt, zt + tt))
        n = 4
        span = we / 2 - tw
        if fillet > 0 and span > 1e-4:
            for k in range(n):
                x0, x1 = tw + span * k / n, tw + span * (k + 1) / n
                hgt = fillet * tt * (1 - (k + 0.5) / n)
                conds += [("S", x0, x1, zt, zt + hgt), ("S", -x1, -x0, zt, zt + hgt)]
    er = {"core": bd.er_core, "pp": bd.er_pp, "air": 1.0, "mask": bd.mask_er}
    return dict(width=W, slabs=slabs, blocks=blocks, conductors=conds), er


def impedance(bd: Board4L, region: str, w: float, gap: Optional[float], mesh: str = "launch", **kw):
    """(Z0 in ohm, εeff) of one station: quasi-static, C and the vacuum C0 (FEA environment)."""
    from yapnr.rf.coupons import xsec

    spec, er = section(bd, region, w, gap, **kw)
    r = xsec.solve(xsec.build(spec, mesh), er, ["S"])
    c, c0 = float(r["C"][0, 0]), float(r["C0"][0, 0])
    return 1.0 / (C_LIGHT * math.sqrt(c * c0)), c / c0


def _bisect(f, lo, hi, target, n=14, increasing=True):
    """x in [lo, hi] with f(x) = target, f monotone; None outside the bracket."""
    flo, fhi = f(lo), f(hi)
    if not increasing:
        flo, fhi = -flo, -fhi
        target = -target
        g = f
        f = lambda x: -g(x)  # noqa: E731
    if not (flo <= target <= fhi):
        return None
    for _ in range(n):
        mid = 0.5 * (lo + hi)
        if f(mid) < target:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


# --- the 2D design of the transitions (FEA environment) ------------------------------------------

PAD_W = 0.80  # pin pad: the 0.51 mm tab plus 0.145 mm of fillet each side
PAD_TOE = 0.40  # pad beyond the tab tip: the toe fillet, and room for a connector set back a little
CUT_MARGIN = 0.25  # In1.Cu cut-out beyond the pad's coplanar gap (Order 0 design §5)
M_KEEPAWAY = 1.0  # L1 ground from M copper beyond the launch (5h, design §4.2)
W_KEEPAWAY = 4.2  # L1 ground from W copper (3h, design §4.3)
W_RING = 5.7  # |y| from which In1/In2 stay as the W stick's stitched perimeter ring (1.5 + 4.2)
M_TAPER = 1.2  # mm: pad to line (WP4: 1-1.5 mm)
M_FLARE = 2.0  # mm: the L1 ground opens from the taper's gap to M_KEEPAWAY
M_GAP_END = 0.40  # L1 gap at the taper end (2h: the line is microstrip-like from there)
W_TAPER = 3.6  # mm: pad to the W line
W_FLARE = 2.0
FENCE_OFF = 0.50  # via centre from the channel edge (via pad 0.55: 0.225 mm of ground copper)
FENCE_PITCH = 1.0  # <= 1.3 mm (λ/10 at 12 GHz in FR408HR)
FENCE_LINE = 4.0  # fence along the first 4 mm of line after the flare
EDGE_ROW_X = 0.70  # first via row: hole 0.55 mm and pad 0.425 mm from the nominal edge
VIA_D, VIA_DRILL = 0.55, 0.30  # the oshpark-4l default via class
DAM_W = 0.20  # solder-mask dam across the line at the pad end
N_TAPER = {"M": 9, "W": 9}  # 2D stations along a taper, both ends included


def solve_design(
    region: str,
    bd: Board4L = OSH_FR408HR,
    conn: EdgeSMA = CINCH_142_0701_851,
    pad_w: float = PAD_W,
    line_w: Optional[float] = None,
    mesh: str = "launch",
    log=print,
) -> dict:
    """The launch of one region from 2D solves: the pad's coplanar gaps with and without the tab,
    the taper stations, the flare, and the sensitivities. FEA environment, a few minutes."""
    line_w = line_w if line_w is not None else (0.40 if region == "M" else 3.0)
    Z = lambda w, g, **k: impedance(bd, region, w, g, mesh=mesh, **k)  # noqa: E731
    x0, x_tab = bd.keepback, round(conn.tab_len, 3)
    x_pe = round(x_tab + PAD_TOE, 3)
    ring = {} if region == "M" else {"inner": W_RING}
    out = dict(
        region=region,
        board=bd.id,
        connector=conn.part,
        line_w=line_w,
        pad_w=pad_w,
        x_pad=[x0, x_pe],
        x_tab=x_tab,
        mesh=mesh,
    )
    tab = dict(tab=conn, fillet=0.5, mask_gap=True)

    if region == "M":

        def zpad(g, cut=None, **k):
            c = cut if cut is not None else pad_w / 2 + g + CUT_MARGIN
            return Z(pad_w, g, cut=c, **k)[0]

        g_tab = _bisect(lambda g: zpad(g, **tab), bd.min_space, 1.0, 50.0)
        cut_pad = round(pad_w / 2 + g_tab + CUT_MARGIN, 4)
        g_bare = _bisect(lambda g: zpad(g, cut=cut_pad, mask_gap=True), bd.min_space, 1.0, 50.0)
        out.update(gap_tab=round(g_tab, 4), gap_bare=round(g_bare, 4), cut_pad=cut_pad)
        log(f"M pad {pad_w}: gap {g_tab:.4f} (tab), {g_bare:.4f} (bare), cut ±{cut_pad}")
        sens = {}
        for name, kw in (
            ("fillet 0", dict(tab=conn, fillet=0.0, mask_gap=True)),
            ("fillet 1", dict(tab=conn, fillet=1.0, mask_gap=True)),
            ("no mask in the gap", dict(tab=conn, fillet=0.5)),
            ("no tab", dict(mask_gap=True)),
            ("etch +0.025", dict(etch=0.025, **tab)),
        ):
            sens[name] = round(zpad(g_tab, cut=cut_pad, **kw), 2)
        for name, b2 in (
            ("h_pp -10 %", _scaled(bd, h_pp=0.9)),
            ("h_pp +10 %", _scaled(bd, h_pp=1.1)),
            ("EM528", OSH_EM528),
        ):
            sens[name] = round(
                impedance(b2, "M", pad_w, g_tab, mesh=mesh, cut=cut_pad, **tab)[0], 2
            )
        out["pad_sensitivity_ohm"] = sens
        log(f"  sensitivity {sens}")
        stations = []
        for k in range(N_TAPER[region]):
            f = k / (N_TAPER[region] - 1)
            x = x_pe + M_TAPER * f
            w = pad_w + (line_w - pad_w) * f
            g = g_bare + (M_GAP_END - g_bare) * f
            if k == 0:
                c = cut_pad
            else:
                m = dict(mask_gap=True) if x < x_pe + DAM_W - 1e-9 else {}
                zc = lambda c: Z(w, g, cut=c, **m)[0]  # noqa: E731
                c = _bisect(zc, 0.0, cut_pad, 50.0, n=12)
                if c is None:
                    c = 0.0 if zc(0.0) > 50.0 else cut_pad
                if k == N_TAPER[region] - 1 and c < w / 4:
                    c = 0.0  # a slot narrower than half the strip: close the cut-out here
            dam = dict(mask_gap=True) if x < x_pe + DAM_W - 1e-9 else {}  # under the dam
            z, e = Z(w, g, cut=c, **dam) if c > 0 else Z(w, g, **dam)
            stations.append(
                dict(
                    x=round(x, 4),
                    w=round(w, 4),
                    gap=round(g, 4),
                    cut=round(c, 4),
                    z0=round(z, 2),
                    eps_eff=round(e, 4),
                )
            )
            log(f"  taper x {x:.3f} w {w:.3f} gap {g:.3f} cut {c:.4f}: {z:.2f} ohm")
        out["taper"] = stations
        out["taper_mid"] = taper_midpoints(bd, region, stations, x_pe, mesh, log)
        keep = M_KEEPAWAY
        flare_len = M_FLARE
        g_end = M_GAP_END
    else:

        def zpad(g, **k):
            return Z(pad_w, g, **ring, **k)[0]

        g_tab = _bisect(lambda g: zpad(g, **tab), bd.min_space, 2.0, 50.0)
        g_bare = _bisect(lambda g: zpad(g, mask_gap=True), bd.min_space, 2.0, 50.0)
        out.update(gap_tab=round(g_tab, 4), gap_bare=round(g_bare, 4), cut_pad=0.0)
        log(f"W pad {pad_w}: gap {g_tab:.4f} (tab), {g_bare:.4f} (bare)")
        sens = {}
        for name, kw in (
            ("fillet 0", dict(tab=conn, fillet=0.0, mask_gap=True)),
            ("fillet 1", dict(tab=conn, fillet=1.0, mask_gap=True)),
            ("no mask in the gap", dict(tab=conn, fillet=0.5)),
            ("no tab", dict(mask_gap=True)),
            ("etch +0.025", dict(etch=0.025, **tab)),
        ):
            sens[name] = round(zpad(g_tab, **kw), 2)
        for name, b2 in (("h_core -10 %", _scaled(bd, h_core=0.9)), ("EM528", OSH_EM528)):
            sens[name] = round(impedance(b2, "W", pad_w, g_tab, mesh=mesh, **ring, **tab)[0], 2)
        out["pad_sensitivity_ohm"] = sens
        log(f"  sensitivity {sens}")
        stations = []
        gmax_leg = conn.lay_c / 2  # the ground pads bound the gap next to the legs
        for k in range(N_TAPER[region]):
            f = k / (N_TAPER[region] - 1)
            x = x_pe + W_TAPER * f
            w = pad_w + (line_w - pad_w) * f
            cap = (gmax_leg - w / 2) if x <= conn.lay_e + 0.3 else W_KEEPAWAY
            if k == 0:
                g = g_bare
            else:
                zg = lambda g: Z(w, g, **ring)[0]  # noqa: E731
                g = _bisect(zg, bd.min_space, cap, 50.0, n=12)
                if g is None:
                    g = cap if zg(cap) < 50.0 else bd.min_space
            dam = dict(mask_gap=True) if x < x_pe + DAM_W - 1e-9 else {}  # under the dam
            z, e = Z(w, g, **ring, **dam)
            stations.append(
                dict(
                    x=round(x, 4),
                    w=round(w, 4),
                    gap=round(g, 4),
                    cut=0.0,
                    z0=round(z, 2),
                    eps_eff=round(e, 4),
                )
            )
            log(f"  taper x {x:.3f} w {w:.3f} gap {g:.4f} (cap {cap:.3f}): {z:.2f} ohm")
        out["taper"] = stations
        out["taper_mid"] = taper_midpoints(bd, region, stations, x_pe, mesh, log)
        keep = W_KEEPAWAY
        flare_len = W_FLARE
        g_end = stations[-1]["gap"]
    # the flare: the L1 ground opens from the taper's last gap to the region's keep-away
    x_te = stations[-1]["x"]
    flare = []
    for k in range(1, 4) if g_end < keep - 1e-6 else ():
        f = k / 3
        g = g_end + (keep - g_end) * f
        z, e = Z(line_w, g, **ring)
        flare.append(
            dict(
                x=round(x_te + flare_len * f, 4),
                gap=round(g, 4),
                z0=round(z, 2),
                eps_eff=round(e, 4),
            )
        )
        log(f"  flare x {x_te + flare_len * f:.3f} gap {g:.3f}: {z:.2f} ohm")
    out["flare"] = flare
    z, e = Z(line_w, None, **ring) if region == "M" else Z(line_w, keep, **ring)
    out["line"] = dict(w=line_w, z0=round(z, 2), eps_eff=round(e, 4))
    out["keepaway"] = keep
    return out


def taper_midpoints(
    bd: Board4L, region: str, stations: Sequence[dict], x_pe: float, mesh: str = "launch", log=print
) -> List[dict]:
    """Z0 halfway between consecutive stations, where the layout interpolates the strip, the
    L1 ground edge and the cut-out linearly: the check that the stations are dense enough."""
    out = []
    for a, b in zip(stations, stations[1:]):
        x = (a["x"] + b["x"]) / 2
        w = (a["w"] + b["w"]) / 2
        g = (a["w"] / 2 + a["gap"] + b["w"] / 2 + b["gap"]) / 2 - w / 2
        kw = {} if region == "M" else {"inner": W_RING}
        c = (a["cut"] + b["cut"]) / 2
        if region == "M" and c > 0:
            kw["cut"] = c
        if x < x_pe + DAM_W - 1e-9:
            kw["mask_gap"] = True
        z, _ = impedance(bd, region, w, g, mesh=mesh, **kw)
        out.append(dict(x=round(x, 4), z0=round(z, 2)))
        log(f"  midway x {x:.3f}: {z:.2f} ohm")
    return out


def _scaled(bd: Board4L, **f) -> Board4L:
    d = asdict(bd)
    for k, v in f.items():
        d[k] = d[k] * v
    d["id"] = bd.id + "-" + "-".join(f"{k}x{v:g}" for k, v in f.items())
    return Board4L(**d)


# --- the shipped designs and their geometry (numpy only) ------------------------------------------

SCHEMA_LAUNCH = "yapnr-coupon-launch/1"
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def data_path(board_id: str, data_dir: Optional[str] = None) -> str:
    return os.path.join(data_dir or DATA_DIR, f"launch-{board_id}.json")


@dataclass(frozen=True)
class Via:
    x: float
    y: float
    role: str  # "edge", "fence", "leg"


@dataclass(frozen=True)
class LaunchDesign:
    """One region's launch, in launch coordinates (x from the milled edge, y across)."""

    region: str
    board: Board4L
    conn: EdgeSMA
    line_w: float
    pad_w: float
    x0: float  # pad start (the copper keep-back)
    x_tab: float  # tab tip
    x_pe: float  # pad end
    gap_tab: float
    gap_bare: float
    cut_pad: float  # In1.Cu cut-out half width under the pad (0: none, region W)
    taper: Tuple[dict, ...]
    flare: Tuple[dict, ...]
    keepaway: float
    line: dict = field(default_factory=dict)
    sensitivity: dict = field(default_factory=dict)
    taper_mid: Tuple[dict, ...] = ()  # Z0 halfway between stations (the interpolation check)

    @property
    def x_te(self) -> float:
        """Taper end: the line has its family width from here."""
        return self.taper[-1]["x"]

    @property
    def x_end(self) -> float:
        """Flare end: the L1 ground is at the region's keep-away from here."""
        return self.flare[-1]["x"] if self.flare else self.x_te

    @property
    def cut_layers(self) -> Tuple[int, ...]:
        """Copper layers the launch cuts (region W: the stick removes In1/In2 itself)."""
        return (2,) if self.region == "M" else ()

    def signal_outline(self) -> List[Tuple[float, float]]:
        """Pad and taper copper, a closed polygon (counter-clockwise in x right, y up)."""
        lo = [(self.x0, -self.pad_w / 2)] + [(s["x"], -s["w"] / 2) for s in self.taper]
        hi = [(s["x"], s["w"] / 2) for s in reversed(self.taper)] + [(self.x0, self.pad_w / 2)]
        return lo + hi

    def channel(self, x_to: Optional[float] = None) -> List[Tuple[float, float]]:
        """[(x, half width)] of the copper-free L1 channel from the edge (x = 0), piecewise
        linear, to the flare end (or on to `x_to` at the keep-away)."""
        hp = self.pad_w / 2
        prof = [(0.0, hp + self.gap_tab), (self.x_tab, hp + self.gap_tab)]
        if abs(self.gap_bare - self.gap_tab) > 1e-4:
            prof.append((self.x_tab, hp + self.gap_bare))
        prof += [(s["x"], s["w"] / 2 + s["gap"]) for s in self.taper]
        prof += [(f["x"], self.line_w / 2 + f["gap"]) for f in self.flare]
        if x_to is not None and x_to > prof[-1][0]:
            prof.append((x_to, self.line_w / 2 + self.keepaway))
        return prof

    def cut_profile(self) -> List[Tuple[float, float]]:
        """[(x, half width)] of the In1.Cu cut-out (region M), from the edge to where it closes;
        empty for region W."""
        if self.cut_pad <= 0:
            return []
        prof = [(0.0, self.cut_pad)]
        for s in self.taper:
            prof.append((s["x"], s["cut"]))
            if s["cut"] <= 0:
                break
        if prof[-1][1] > 0:  # still open at the taper end: close it with a step
            prof.append((prof[-1][0], 0.0))
        return prof

    def ground_pads(self) -> List[Tuple[float, float, float, float]]:
        """(x0, x1, y0, y1) of the leg pads, on F.Cu and B.Cu: the manufacturer's C, D and E,
        less the keep-back at the edge."""
        c, d = self.conn.gnd_pad_y
        return [(self.x0, self.conn.lay_e, c, d), (self.x0, self.conn.lay_e, -d, -c)]

    def channel_at(self, x: float, x_to: Optional[float] = None) -> float:
        prof = self.channel(x_to)
        xs = [p[0] for p in prof]
        if x <= xs[0]:
            return prof[0][1]
        if x >= xs[-1]:
            return prof[-1][1]
        for (xa, ha), (xb, hb) in zip(prof, prof[1:]):
            if xa <= x <= xb:
                if xb - xa < 1e-9:
                    return max(ha, hb)
                return ha + (hb - ha) * (x - xa) / (xb - xa)
        return prof[-1][1]

    def vias(self, half_w: float, x_max: Optional[float] = None) -> List[Via]:
        """Ground vias of one launch on a stick `2 half_w` wide: the edge row, the fences along
        both channel edges to FENCE_LINE past the flare (or to `x_max`, half a short stick),
        and the leg vias (inside and behind each ground pad). Candidates that break a rule are
        dropped (`_rule_ok`)."""
        conn = self.conn
        c, d = conn.gnd_pad_y
        y_leg_in = c - VIA_D / 2 - 0.15  # 0.15 mm mask web to the leg pad
        out: List[Via] = []
        x_stop = self.x_end + FENCE_LINE
        if x_max is not None:
            x_stop = min(x_stop, x_max)
        # fences: both sides, from the edge row on; near steps take the widest channel nearby
        xs = []
        x = EDGE_ROW_X
        while x <= x_stop + 1e-9:
            xs.append(round(x, 4))
            x += FENCE_PITCH
        for x in xs:
            hw = max(self.channel_at(x + dx) for dx in (-0.3, 0.0, 0.3))
            for sgn in (-1, 1):
                self._add(out, Via(x, sgn * (hw + FENCE_OFF), "fence"), half_w)
        # leg vias: a row inside each ground pad and three behind its end
        x = EDGE_ROW_X
        while x <= conn.lay_e - 0.2 + 1e-9:
            for sgn in (-1, 1):
                self._add(out, Via(round(x, 4), sgn * y_leg_in, "leg"), half_w)
            x += FENCE_PITCH
        xb = conn.lay_e + VIA_D / 2 + 0.15
        ya, yb = c + 0.3, d - 0.45
        for yy in (ya, (ya + yb) / 2, yb):
            for sgn in (-1, 1):
                self._add(out, Via(round(xb, 4), sgn * round(yy, 4), "leg"), half_w)
        # the edge row between the fence and the leg row, both sides
        y_f = self.channel_at(EDGE_ROW_X) + FENCE_OFF
        n = max(1, int(math.ceil((y_leg_in - y_f) / FENCE_PITCH)))
        for k in range(1, n):
            yy = y_f + (y_leg_in - y_f) * k / n
            for sgn in (-1, 1):
                self._add(out, Via(EDGE_ROW_X, sgn * round(yy, 4), "edge"), half_w)
        return out

    def _add(self, out: List[Via], v: Via, half_w: float):
        if self._rule_ok(v, out, half_w):
            out.append(v)

    def _rule_ok(self, v: Via, placed: Sequence[Via], half_w: float) -> bool:
        bd = self.board
        r = VIA_D / 2
        # copper keep-back and hole-to-edge (0.5 mm) from the launch edge and the long sides
        if v.x - r < bd.keepback - 1e-9 or v.x - VIA_DRILL / 2 < HOLE_TO_EDGE - 1e-9:
            return False
        if abs(v.y) + r > half_w - bd.keepback + 1e-9:
            return False
        if half_w - abs(v.y) - VIA_DRILL / 2 < HOLE_TO_EDGE - 1e-9:
            return False
        # outside the channel and the signal copper, by a pad radius plus the minimum space
        if abs(v.y) - r < self.channel_at(v.x) + bd.min_space - 1e-9:
            return False
        # off the leg pads (tented vias, 0.15 mm mask web)
        for x0, x1, y0, y1 in self.ground_pads():
            dx = max(x0 - v.x, 0.0, v.x - x1)
            dy = max(y0 - v.y, 0.0, v.y - y1)
            if math.hypot(dx, dy) < r + 0.15 - 1e-9:
                return False
        # hole to hole
        for p in placed:
            if math.hypot(p.x - v.x, p.y - v.y) < VIA_DRILL + HOLE_TO_HOLE - 1e-9:
                return False
        return True

    def to_json(self) -> dict:
        return dict(
            region=self.region,
            line_w=self.line_w,
            pad_w=self.pad_w,
            x_pad=[self.x0, self.x_pe],
            x_tab=self.x_tab,
            gap_tab=self.gap_tab,
            gap_bare=self.gap_bare,
            cut_pad=self.cut_pad,
            taper=list(self.taper),
            taper_mid=list(self.taper_mid),
            flare=list(self.flare),
            keepaway=self.keepaway,
            line=self.line,
            pad_sensitivity_ohm=self.sensitivity,
        )


HOLE_TO_EDGE = 0.5  # oshpark-4l profile (derived there: not published by OSH Park)
HOLE_TO_HOLE = 0.5  # oshpark-4l pth_hole_to_hole (the stricter of its two values)

BOARDS = {b.id: b for b in (OSH_FR408HR, OSH_EM528)}


def load(board_id: str = OSH_FR408HR.id, data_dir: Optional[str] = None) -> Dict[str, LaunchDesign]:
    """The shipped launch designs of a board, by region."""
    with open(data_path(board_id, data_dir), encoding="utf-8") as f:
        doc = json.load(f)
    if doc.get("schema") != SCHEMA_LAUNCH:
        raise ValueError(f"not a {SCHEMA_LAUNCH} file")
    bd = BOARDS[doc["board"]]
    out = {}
    for reg, r in doc["regions"].items():
        out[reg] = LaunchDesign(
            region=reg,
            board=bd,
            conn=CINCH_142_0701_851,
            line_w=r["line_w"],
            pad_w=r["pad_w"],
            x0=r["x_pad"][0],
            x_tab=r["x_tab"],
            x_pe=r["x_pad"][1],
            gap_tab=r["gap_tab"],
            gap_bare=r["gap_bare"],
            cut_pad=r["cut_pad"],
            taper=tuple(r["taper"]),
            flare=tuple(r["flare"]),
            keepaway=r["keepaway"],
            line=r.get("line", {}),
            sensitivity=r.get("pad_sensitivity_ohm", {}),
            taper_mid=tuple(r.get("taper_mid", [])),
        )
    return out


def check(d: LaunchDesign, half_w: float) -> List[str]:
    """Rule problems of a launch on a stick `2 half_w` wide (empty when clean): the keep-back,
    the minimum space, the taper length and impedances, the cut-out, the fence pitch and the
    leg vias."""
    bd, conn = d.board, d.conn
    p: List[str] = []
    if d.x0 < bd.keepback - 1e-9:
        p.append(f"pad starts {d.x0} mm from the edge (< {bd.keepback})")
    c, dd = conn.gnd_pad_y
    if dd > half_w - bd.keepback + 1e-9:
        p.append(f"ground pads reach |y| {dd:.3f} > {half_w - bd.keepback:.3f} (stick keep-back)")
    if d.pad_w < conn.tab_w + 0.2 - 1e-9:
        p.append(f"pad {d.pad_w} mm leaves less than 0.1 mm of fillet each side of the tab")
    if d.x_pe < conn.tab_len + 0.3:
        p.append("pad ends less than 0.3 mm past the tab tip")
    gaps = [d.gap_tab, d.gap_bare] + [s["gap"] for s in d.taper] + [f["gap"] for f in d.flare]
    if min(gaps) < bd.min_space - 1e-9:
        p.append(f"coplanar gap {min(gaps):.3f} below the minimum space {bd.min_space}")
    if d.region == "M":
        lt = d.x_te - d.x_pe
        if not (1.0 - 1e-9 <= lt <= 1.5 + 1e-9):
            p.append(f"M taper {lt:.2f} mm (1-1.5 mm)")
        if not (0.38 - 1e-9 <= d.line_w <= 0.42 + 1e-9):
            p.append(f"M line {d.line_w} mm (0.38-0.42 mm)")
        if d.cut_pad < d.pad_w / 2 + max(d.gap_tab, d.gap_bare) - 1e-9:
            p.append("In1.Cu cut-out narrower than the pad and its gap")
    for s in list(d.taper) + list(d.taper_mid):
        if abs(s["z0"] - 50.0) > 2.5:
            p.append(f"taper at x {s['x']}: {s['z0']} ohm (50 ± 2.5)")
    for x, hw in d.channel():
        if x <= conn.lay_e + 1e-9 and hw > c + 1e-9:
            p.append(f"channel half width {hw:.3f} at x {x:.2f} runs into the leg pads")
    vias = d.vias(half_w)
    for sgn in (-1, 1):
        fence = sorted(v.x for v in vias if v.role == "fence" and v.y * sgn > 0)
        # the wall along the channel edge: fence vias, and where the channel comes close to the
        # leg pads (region W), the leg vias that take their place
        wall = sorted(
            v.x
            for v in vias
            if v.y * sgn > 0 and abs(v.y) <= d.channel_at(v.x) + FENCE_OFF + 0.8 + 1e-9
        )
        steps = [b - a for a, b in zip(wall, wall[1:])]
        if not fence or fence[0] > EDGE_ROW_X + 1e-6:
            p.append("fence does not start at the edge row")
        if steps and max(steps) > 1.3 + 1e-9:
            p.append(f"fence pitch {max(steps):.2f} mm > 1.3 mm")
        if fence and fence[-1] < d.x_end + FENCE_LINE - FENCE_PITCH - 1e-6:
            p.append("fence stops before FENCE_LINE past the flare")
        legs = [v for v in vias if v.role == "leg" and v.y * sgn > 0]
        if len(legs) < 3:
            p.append(f"only {len(legs)} vias tie the {'upper' if sgn > 0 else 'lower'} legs")
    return p


_CACHE: Dict[str, Dict[str, LaunchDesign]] = {}


def design(key: str) -> LaunchDesign:
    """A shipped design by key, "<board id>:<region>" (`catalog.Launch.design`)."""
    board_id, region = key.split(":")
    if board_id not in _CACHE:
        _CACHE[board_id] = load(board_id)
    return _CACHE[board_id][region]


def write(board_id: str, regions: Dict[str, dict], data_dir: Optional[str] = None) -> str:
    """Write (or update, region by region) a board's launch file."""
    from yapnr.rf.coupons import jsonfmt

    path = data_path(board_id, data_dir)
    doc = {"schema": SCHEMA_LAUNCH, "board": board_id, "regions": {}}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    bd = BOARDS[board_id]
    doc.update(
        connector=CINCH_142_0701_851.part,
        connector_source=CINCH_142_0701_851.source,
        tool="python -m yapnr.rf.coupons.launch design",
        solver="2D quasi-static (xsec: scikit-fem P2 on gmsh), Z0 = 1 / (c sqrt(C C0))",
        cross_section=asdict(bd),
        rules=dict(
            pad_toe_mm=PAD_TOE,
            cut_margin_mm=CUT_MARGIN,
            fence_off_mm=FENCE_OFF,
            fence_pitch_mm=FENCE_PITCH,
            fence_line_mm=FENCE_LINE,
            edge_row_x_mm=EDGE_ROW_X,
            via_mm=[VIA_D, VIA_DRILL],
            dam_mm=DAM_W,
        ),
    )
    for reg, r in regions.items():
        r = dict(r)
        r.pop("region", None)
        r.pop("board", None)
        r.pop("connector", None)
        doc["regions"][reg] = r
    doc["regions"] = {k: doc["regions"][k] for k in sorted(doc["regions"])}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    jsonfmt.dump(doc, path)
    _CACHE.pop(board_id, None)
    return path


def summary(d: LaunchDesign, half_w: float) -> str:
    lines = [
        f"region {d.region} ({d.board.id}, {d.conn.part}): pad {d.pad_w} mm from x {d.x0} to"
        f" {d.x_pe} (tab to {d.x_tab}); gap {d.gap_tab} (tab) / {d.gap_bare} (bare)"
        + (f"; In1.Cu cut ±{d.cut_pad}" if d.cut_pad else "")
    ]
    for s in d.taper:
        lines.append(
            f"  taper x {s['x']:.3f}: w {s['w']:.3f} gap {s['gap']:.3f}"
            + (f" cut {s['cut']:.3f}" if d.region == "M" else "")
            + f" -> {s['z0']:.2f} ohm, eps_eff {s['eps_eff']:.3f}"
        )
    if d.taper_mid:
        zs = [m["z0"] for m in d.taper_mid]
        lines.append(f"  halfway between stations: {min(zs):.2f} to {max(zs):.2f} ohm")
    for f in d.flare:
        lines.append(f"  flare x {f['x']:.3f}: gap {f['gap']:.3f} -> {f['z0']:.2f} ohm")
    if d.line:
        lines.append(f"  line {d.line['w']} mm: {d.line['z0']} ohm, eps_eff {d.line['eps_eff']}")
    if d.sensitivity:
        lines.append(
            "  pad (tab section) sensitivity: "
            + ", ".join(f"{k} {v}" for k, v in d.sensitivity.items())
        )
    vias = d.vias(half_w)
    roles = {r: sum(1 for v in vias if v.role == r) for r in ("edge", "fence", "leg")}
    lines.append(f"  vias on a {2 * half_w:g} mm stick: {roles}")
    probs = check(d, half_w)
    lines.append("  rule check: " + ("clean" if not probs else "; ".join(probs)))
    return "\n".join(lines)


STICK_W = {"M": 12.0, "W": 16.0}  # Order 0 design §3.2 (review): M 12 mm, W 16 mm


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m yapnr.rf.coupons.launch")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("design", help="2D-solve the launches (FEA environment) and ship them")
    d.add_argument("--board", default=OSH_FR408HR.id, choices=sorted(BOARDS))
    d.add_argument("--region", action="append", choices=["M", "W"])
    d.add_argument("--pad-w", type=float, default=PAD_W)
    d.add_argument("--mesh", default="launch")
    d.add_argument("--data-dir", default=None)
    s = sub.add_parser("show", help="the shipped launches and their rule check (numpy only)")
    s.add_argument("--board", default=OSH_FR408HR.id)
    b = sub.add_parser(
        "board",
        help="the launch check board of a region (KiCad DRC when a"
        " headless kicad-cli is in YAPNR_KICAD_CLI)",
    )
    b.add_argument("--region", required=True, choices=["M", "W"])
    b.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "board":
        from yapnr.rf.coupons import fab

        out = fab.generate_launch_check(a.region, a.out)
        print(f"board: {out['board']} (panel {out['panel'][0]:.1f} x {out['panel'][1]:.1f} mm)")
        drc = out.get("drc")
        if drc is None:
            print("no headless kicad-cli in YAPNR_KICAD_CLI: DRC skipped")
            return 0
        n = sum(drc["violations"].values())
        print(f"DRC: {n} violations, {drc['unconnected']} unconnected ({drc['kicad_version']})")
        for v in drc["details"][:10]:
            print(f"  {v['severity']} {v['type']}: {v['description']}")
        return 0 if n == 0 and drc["unconnected"] == 0 else 1
    if a.cmd == "design":
        import logging

        logging.getLogger("skfem").setLevel(logging.ERROR)  # mesh-conversion chatter
        bd = BOARDS[a.board]
        regs = {}
        for reg in a.region or ["M", "W"]:
            regs[reg] = solve_design(
                reg, bd, pad_w=a.pad_w, mesh=a.mesh, log=lambda m: print(m, flush=True)
            )
        print(write(a.board, regs, a.data_dir))
    else:
        for reg, ld in load(a.board).items():
            print(summary(ld, STICK_W[reg] / 2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
