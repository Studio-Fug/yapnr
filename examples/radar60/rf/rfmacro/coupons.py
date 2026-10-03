"""Rev A coupon strip (60 x 25 mm) for the conventional set (board-design.md §8).

Left part, probe-ready 60 GHz structures (GSG 250 µm pads [BD §8 review]):
- CP-60: GCPW thru and mTRL lines (+0.25, +0.5, +1.0, +2.0, +4.0 mm), open and short (1-port),
  and a gap-coupled ring (3 λg at 62.05 GHz);
- CP-L: launch half-replica (ball land probed as a 650 µm GSG site on its two GND neighbours,
  RFS-1 launch, line, GSG) and a land-to-land back-to-back launch;
- CP-D: the corporate divider back-to-back (λ/4 35 Ω, T, two 50 Ω arms, T, λ/4);
- CP-T: the TX1 feed (P0 -> P1, serpentine included) between GSG pads;
- CP-A: 1-port antennas: the corporate column (RFS-4), a series-fed column (RFS-4S, D5
  fallback), and single patches at L-25 µm, L, L+25 µm;
- CP-M: line/gap combs 75-150 µm for optical CD measurement.
Right part, DC-6 GHz for the LibreVNA (labelled "measured coupon (≤ 6 GHz)" only): SMA
edge-launch thru and line, a gap-coupled ring near 5 GHz, and 4-wire copper bars on L1 and L3.

The SMA land is the Samtec SMA-J-P-H-ST-EM1 edge-mount pattern used by yapnr's own coupon boards
(`yapnr.rf.coupons.layout`); everything else comes from this package's parameters.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

from . import dims as dims_mod
from . import kicad as kc
from .geom import Path, Pt, circle, outline, rect
from .macro import _place_paths, build, build_column
from .params import PKG, RULES, resolve

STRIP = (60.0, 25.0)
GSG_PITCH = 0.25
GSG_S_W, GSG_LEN, TAPER = 0.10, 0.20, 0.15


@dataclass
class Strip:
    paths: List[Tuple[str, Path]] = field(default_factory=list)
    pads: List[Tuple[str, List[Pt], str]] = field(default_factory=list)  # (net, polygon, key)
    vias: List[Tuple[Pt, float, float]] = field(default_factory=list)
    chan: List[List[Pt]] = field(default_factory=list)  # F.Cu pour keepouts
    l2_keep: List[List[Pt]] = field(default_factory=list)
    l3_keep: List[List[Pt]] = field(default_factory=list)
    l3_tracks: List[Tuple[str, Path]] = field(default_factory=list)
    sma: List[Tuple[Pt, float, str, str]] = field(default_factory=list)  # (xy, angle, net, ref)
    catalog: List[Dict[str, object]] = field(default_factory=list)
    nets: List[str] = field(default_factory=list)

    def net(self, n: str) -> str:
        if n not in self.nets:
            self.nets.append(n)
        return n


def _gsg(st: Strip, net: str, tip: Pt, heading: float, key: str) -> Pt:
    """GSG landing at `tip` pointing back along `heading` into the structure: signal pad
    0.10 x 0.20 mm, a 0.15 mm taper to the 50 Ω width, ground pads formed by the pour (channel
    half-width 0.20 at the pad), two GND vias behind the ground pads. Returns the line start."""
    c, s = math.cos(heading), math.sin(heading)
    w50 = 0.2

    def at(u, v):
        return (tip[0] + u * c - v * s, tip[1] + u * s + v * c)

    poly = [
        at(0, -GSG_S_W / 2),
        at(GSG_LEN, -GSG_S_W / 2),
        at(GSG_LEN + TAPER, -w50 / 2),
        at(GSG_LEN + TAPER, w50 / 2),
        at(GSG_LEN, GSG_S_W / 2),
        at(0, GSG_S_W / 2),
    ]
    st.pads.append((net, poly, key))
    g = GSG_PITCH - 0.05  # pour edge = inner edge of the ground pads
    st.chan.append(
        [
            at(-0.05, -g),
            at(GSG_LEN, -g),
            at(GSG_LEN + TAPER, -0.30),
            at(GSG_LEN + TAPER, 0.30),
            at(GSG_LEN, g),
            at(-0.05, g),
        ]
    )
    for v in (-0.55, 0.55):
        st.vias.append((at(0.10, v), *RULES["via_fence"]))
    return at(GSG_LEN + TAPER, 0.0)


def _line(
    st: Strip, net: str, start: Pt, heading: float, length: float, key: str, fence: bool = True
) -> Path:
    p = Path(start, heading, 0.2)
    p.straight(length)
    st.paths.append((net, p))
    st.chan.append(outline(p, half=0.30))
    if fence:
        for v in p.fence(0.50, 0.45, skip_from=0.10, skip_to=0.10):
            st.vias.append((v, *RULES["via_fence"]))
    return p


def cp60(st: Strip, x0: float, y0: float, d: Dict) -> float:
    """mTRL set: rows along +x, GSG at both ends."""
    y = y0
    base = 0.50
    for k, extra in enumerate((0.0, 0.25, 0.5, 1.0, 2.0, 4.0)):
        net = st.net(f"CP60_L{k}")
        a = _gsg(st, net, (x0, y), 0.0, f"cp60/{k}/a")
        L = base + extra
        _line(st, net, a, 0.0, L, f"cp60/{k}")
        _gsg(st, net, (a[0] + L + GSG_LEN + TAPER, y), math.pi, f"cp60/{k}/b")
        st.catalog.append(
            dict(
                id=f"CP60-L{k}",
                kind="GCPW line, GSG both ends",
                line_mm=L,
                pad_to_pad_mm=L + 2 * (GSG_LEN + TAPER),
                y=y,
            )
        )
        y += 1.4
    net = st.net("CP60_OPEN")
    a = _gsg(st, net, (x0, y), 0.0, "cp60/open")
    po = _line(st, net, a, 0.0, 1.0, "cp60/open")
    st.pads.append((net, rect(po.pos[0] - 0.1, y - 0.1, po.pos[0] + 0.1, y + 0.1), "cp60/open/end"))
    st.catalog.append(dict(id="CP60-OPEN", kind="1-port open, 1.0 mm", y=y))
    y += 1.4
    a = _gsg(st, "GND", (x0, y), 0.0, "cp60/short")
    p = _line(st, "GND", a, 0.0, 1.0, "cp60/short", fence=False)
    st.vias.append((p.pos, *RULES["via_fence"], "GND", True))
    st.catalog.append(dict(id="CP60-SHORT", kind="1-port short (via at 1.0 mm)", y=y))
    return y + 1.4


def ring60(st: Strip, cx: float, cy: float, d: Dict) -> None:
    lg = d["lines"]["lambda_g50"]
    r = 3 * lg / (2 * math.pi)
    gap = 0.10
    net = st.net("CP60_RING")
    ring = Path((cx + r, cy), math.pi / 2, 0.2)
    ring.turn(r, 180).turn(r, 180)
    st.paths.append((net, ring))
    for sx in (-1, 1):
        st.pads.append(
            (
                net,
                rect(cx + sx * r - 0.1, cy - 0.1, cx + sx * r + 0.1, cy + 0.1),
                f"ring60/anchor{sx}",
            )
        )
    st.chan.append(circle((cx, cy), r + 0.6, 64))
    for side, key in ((-1, "a"), (+1, "b")):
        n2 = st.net(f"CP60_RING_{key.upper()}")
        x_end = cx + side * (r + 0.1 + gap + 0.1)  # track caps are round (w/2)
        tip = (x_end + side * 1.2, cy)
        a = _gsg(st, n2, tip, math.pi if side > 0 else 0.0, f"ring/{key}")
        p = Path(a, math.pi if side > 0 else 0.0, 0.2)
        p.straight(abs(a[0] - x_end))
        st.paths.append((n2, p))
        st.pads.append((n2, rect(x_end - 0.1, cy - 0.1, x_end + 0.1, cy + 0.1), f"ring/{key}/end"))
        st.chan.append(outline(p, half=0.30))
    st.catalog.append(
        dict(
            id="CP60-RING",
            kind="gap-coupled ring, 3 λg at 62.05 GHz",
            radius_mm=round(r, 4),
            gap_mm=gap,
            centre=[cx, cy],
        )
    )


def divider_b2b(st: Strip, x0: float, y0: float, d: Dict, p: Dict) -> None:
    net = st.net("CPD_CONV")
    q35 = d["lines"]["quarter35"]
    a = _gsg(st, net, (x0, y0), 0.0, "cpd/a")
    ln = Path(a, 0.0, 0.2)
    ln.straight(0.5).straight(q35, width=float(p["w35"]))
    st.paths.append((net, ln))
    t1 = ln.pos
    sep = 0.6
    arms = []
    for sgn in (1, -1):
        arm = Path(t1, sgn * math.pi / 2, 0.2)
        arm.straight(sep - 0.3).turn(0.3, -sgn * 90).straight(1.0).turn(0.3, -sgn * 90).straight(
            sep - 0.3
        )
        st.paths.append((net, arm))
        arms.append(arm)
    t2 = arms[0].pos
    back = Path(t2, 0.0, float(p["w35"]))
    back.straight(q35, width=float(p["w35"])).straight(0.5, width=0.2)
    st.paths.append((net, back))
    _gsg(st, net, (back.pos[0] + GSG_LEN + TAPER, y0), math.pi, "cpd/b")
    box = rect(a[0] - 0.1, y0 - sep - 0.6, back.pos[0] + 0.1, y0 + sep + 0.6)
    st.chan.append(box)
    st.catalog.append(
        dict(
            id="CP-D-CONV",
            kind="corporate divider back-to-back",
            quarter35_mm=q35,
            arm_sep_mm=2 * sep,
            y=y0,
        )
    )


def tx_replica(st: Strip, origin: Pt) -> None:
    mc = build()
    f = mc.feeds["TX1"]
    p0, s0 = f.marks["P0"]
    net = st.net("CPT_TX1")
    # copy the P0 -> P1 part, translated so P0 lands at `origin`
    dx, dy = origin[0] - p0[0], origin[1] - p0[1]
    q = Path((origin[0], origin[1]), 0.0, 0.2)
    acc = 0.0
    for sg in f.segs:
        if acc + 1e-9 >= s0:
            from .geom import Seg

            q.segs.append(
                Seg(
                    sg.kind,
                    (sg.p0[0] + dx, sg.p0[1] + dy),
                    (sg.p1[0] + dx, sg.p1[1] + dy),
                    sg.width,
                    None if sg.center is None else (sg.center[0] + dx, sg.center[1] + dy),
                    sg.radius,
                    sg.a0,
                    sg.sweep,
                )
            )
        acc += sg.length
    tip_a = (origin[0] - GSG_LEN - TAPER, origin[1])
    _gsg(st, net, tip_a, 0.0, "cpt/a")
    end = q.pos
    _gsg(st, net, (end[0], end[1] + GSG_LEN + TAPER), -math.pi / 2, "cpt/b")
    st.paths.append((net, q))
    st.chan.append(outline(q, half=0.30))
    for v in q.fence(0.50, 0.45, skip_from=0.1, skip_to=0.1):
        st.vias.append((v, *RULES["via_fence"]))
    st.catalog.append(
        dict(id="CP-T-TX1", kind="TX1 feed replica P0->P1", length_mm=round(q.length, 4))
    )


def launch_replicas(st: Strip, x0: float, y0: float, p: Dict) -> None:
    """CP-L1: land (GSG 650 µm on the land and its two GND neighbours) -> launch -> line -> GSG
    250 µm. CP-L2: land -> launch -> line -> launch -> land."""
    pitch = PKG["pitch"]
    for k, two in ((1, False), (2, True)):
        y = y0 + (k - 1) * 2.2
        net = st.net(f"CPL{k}")
        land = (x0, y)
        lands = [land]
        L = float(p["launch_len"]) + (1.0 if not two else float(p["launch_len"]))
        end = (x0 + L, y)
        if two:
            lands.append(end)
        for c in lands:
            st.pads.append((net, circle(c, PKG["land"] / 2, 24), f"cpl{k}/land{c[0]:.2f}"))
            st.chan.append(circle(c, float(p["antipad_r"]), 32))
            st.l2_keep.append(circle(c, float(p["l2_cut_r"]), 32))
            for sg in (-1, 1):
                st.pads.append(
                    (
                        "GND",
                        circle((c[0], c[1] + sg * pitch), PKG["land"] / 2, 24),
                        f"cpl{k}/g{c[0]:.2f}{sg}",
                    )
                )
            inward = -1 if c is land else 1
            for sg in (-1, 1):
                st.vias.append(((c[0] + inward * pitch / 2, c[1] + sg * pitch / 2), 0.15, 0.35))
        p_ = _line(st, net, land, 0.0, L, f"cpl{k}", fence=False)
        for v in p_.fence(0.50, 0.45, skip_from=0.9, skip_to=0.9 if two else 0.1):
            st.vias.append((v, *RULES["via_fence"]))
        if not two:
            _gsg(st, net, (end[0] + GSG_LEN + TAPER, y), math.pi, f"cpl{k}/b")
        st.catalog.append(
            dict(id=f"CP-L{k}", kind="launch back-to-back" if two else "launch half-replica", y=y)
        )


def antennas(st: Strip, x0: float, y_base: float, p: Dict, d: Dict) -> None:
    """1-port antennas; each fed from a GSG pad at the bottom through its P1 line."""
    W, L = d["patch"]["w"], d["patch"]["l"]
    m = float(p["window_margin"])
    pitch = 3.6
    x = x0
    # corporate column (the Board A column)
    net = st.net("CPA_CORP")
    org = (x, y_base + 2.0 + 2.3 + GSG_LEN + TAPER)
    col = build_column("CPA_CORP", net, org, False, p, d)
    for i, poly in enumerate(col.patches):
        st.pads.append((net, poly, f"cpa/corp/{i}"))
    for q in _place_paths(col):
        st.paths.append((net, q))
    st.l2_keep += col.windows
    tip = (col.p1[0], col.p1[1] - 2.0 - GSG_LEN - TAPER)
    a = _gsg(st, net, tip, math.pi / 2, "cpa/corp")
    _line(st, net, a, math.pi / 2, col.p1[1] - a[1], "cpa/corp/feed")
    st.chan.append(
        rect(
            x - W / 2 - 1.0,
            col.p1[1] - 0.05,
            x + 1.171 + 0.5,
            org[1] + float(p["spacing"]) / 2 + L / 2 + 1.0,
        )
    )
    st.catalog.append(
        dict(id="CP-A-CORP", kind="corporate 2-patch column (RFS-4)", phase_centre=list(org))
    )
    x += pitch + 0.8
    # series-fed column: feed at the south edge of the lower patch, 0.10 mm link of λg/2
    net = st.net("CPA_SER")
    h = d["window"]["h_mm"]
    from . import closedform as cf

    z, e = cf.ms_static(0.10, h, 0.035, d["window"]["er_composite"])
    lg_link = cf.guided_wavelength(
        62.05e9, cf.kj_dispersion(e, 0.10, h, d["window"]["er_composite"], 62.05e9)
    )
    link = lg_link / 2
    yb = y_base + 2.0 + GSG_LEN + TAPER
    ins = d["patch"]["inset"]
    nw = 0.1 + float(p["notch"])
    y_lo0 = yb + 0.6
    lo = [
        (x - W / 2, y_lo0),
        (x - nw, y_lo0),
        (x - nw, y_lo0 + ins),
        (x + nw, y_lo0 + ins),
        (x + nw, y_lo0),
        (x + W / 2, y_lo0),
        (x + W / 2, y_lo0 + L),
        (x - W / 2, y_lo0 + L),
    ]
    y_hi0 = y_lo0 + L + link
    hi = rect(x - W / 2, y_hi0, x + W / 2, y_hi0 + L)
    st.pads.append((net, lo, "cpa/ser/lo"))
    st.pads.append((net, hi, "cpa/ser/hi"))
    st.pads.append((net, rect(x - 0.05, y_lo0 + L - 0.01, x + 0.05, y_hi0 + 0.01), "cpa/ser/link"))
    st.l2_keep.append(rect(x - W / 2 - m, y_lo0 - m, x + W / 2 + m, y_hi0 + L + m))
    a = _gsg(st, net, (x, y_base), math.pi / 2, "cpa/ser")
    fl = Path(a, math.pi / 2, 0.2)
    fl.straight(y_lo0 + ins - a[1])
    st.paths.append((net, fl))
    st.chan.append(rect(x - W / 2 - 1.0, y_lo0 - 0.6, x + W / 2 + 1.0, y_hi0 + L + 1.0))
    st.chan.append(outline(fl, half=0.30))
    st.catalog.append(
        dict(
            id="CP-A-SER",
            kind="series-fed 2-patch column (RFS-4S, D5 fallback)",
            link_mm=round(link, 4),
            centre_spacing_mm=round(L + link, 4),
        )
    )
    x += pitch
    for k, dl in (("M25", -0.025), ("NOM", 0.0), ("P25", 0.025)):
        net = st.net(f"CPA_P{k}")
        Lk = L + dl
        y0p = yb + 0.6
        poly = [
            (x - W / 2, y0p),
            (x - nw, y0p),
            (x - nw, y0p + ins),
            (x + nw, y0p + ins),
            (x + nw, y0p),
            (x + W / 2, y0p),
            (x + W / 2, y0p + Lk),
            (x - W / 2, y0p + Lk),
        ]
        st.pads.append((net, poly, f"cpa/p{k}"))
        st.l2_keep.append(rect(x - W / 2 - m, y0p - m, x + W / 2 + m, y0p + Lk + m))
        a = _gsg(st, net, (x, y_base), math.pi / 2, f"cpa/p{k}")
        fl = Path(a, math.pi / 2, 0.2)
        fl.straight(y0p + ins - a[1])
        st.paths.append((net, fl))
        st.chan.append(outline(fl, half=0.30))
        st.chan.append(rect(x - W / 2 - 1.0, y0p - 0.6, x + W / 2 + 1.0, y0p + Lk + 1.0))
        st.catalog.append(dict(id=f"CP-A-PATCH-{k}", kind="single inset patch", l_mm=round(Lk, 4)))
        x += pitch


def combs(st: Strip, x0: float, y0: float) -> None:
    y = y0
    # 75 µm is below the 4/4 mil rule [BD §4.2]: it needs the fab's consent before it goes on
    for k, wv in enumerate((0.100, 0.125, 0.150)):
        for i in range(5):
            net = st.net(f"CPM_{k}_{i}")
            xx = x0 + i * 2 * wv
            st.pads.append((net, rect(xx, y, xx + wv, y + 1.0), f"cpm/{k}/{i}"))
        st.chan.append(rect(x0 - 0.3, y - 0.3, x0 + 10 * wv + 0.3, y + 1.3))
        st.catalog.append(dict(id=f"CP-M-{int(wv * 1000)}", kind="line/gap comb", width_mm=wv))
        y += 1.55


def librevna(st: Strip, x0: float) -> None:
    """DC-6 GHz set: SMA edge launches on the bottom (y=0) and top (y=25) edges."""
    H = STRIP[1]
    pin = 1.6  # SMA pad inner end from the edge (coupon writer's pad, 3.2 mm long)
    xs = (x0 + 3.5, x0 + 11.5)
    for k, (x, kind) in enumerate(zip(xs, ("thru", "line"))):
        net = st.net(f"CP6_{kind.upper()}")
        st.sma.append(((x, 0.0), 90.0, net, f"J6{k}A"))
        st.sma.append(((x, H), -90.0, net, f"J6{k}B"))
        p = Path((x, 2 * pin), math.pi / 2, 0.2)
        if kind == "thru":
            p.straight(H - 4 * pin)
        else:  # S-bend out by 2.5 mm and back: about 2 mm longer than the thru
            r = 2.0
            th = math.acos(1 - 2.5 / (2 * r))
            p.straight(2.0).turn(r, math.degrees(th)).turn(r, -math.degrees(th))
            mid = (H - 4 * pin) - 2 * 2.0 - 4 * r * math.sin(th)
            p.straight(mid).turn(r, -math.degrees(th)).turn(r, math.degrees(th)).straight(2.0)
        st.paths.append((net, p))
        st.chan.append(outline(p, half=0.30))
        for v in p.fence(0.50, 0.9, skip_from=0.5, skip_to=0.5):
            st.vias.append((v, *RULES["via_fence"]))
        st.catalog.append(
            dict(
                id=f"CP6-{kind.upper()}",
                kind=f"SMA {kind} (DC-6 GHz)",
                line_mm=round(p.length, 3),
                x=x,
            )
        )
    # ring near 5 GHz: circumference one λg (εeff from the 2D table)
    d = dims_mod.compute(resolve())
    lg5 = d["lines"]["lambda_g50"] * 62.05 / 5.0
    r = lg5 / (2 * math.pi)
    cx, cy = x0 + 11.5 + 4.0 + r + 0.5, H / 2
    net = st.net("CP6_RING")
    ring = Path((cx + r, cy), math.pi / 2, 0.2)
    ring.turn(r, 180).turn(r, 180)
    st.paths.append((net, ring))
    for sx in (-1, 1):
        st.pads.append(
            (
                net,
                rect(cx + sx * r - 0.1, cy - 0.1, cx + sx * r + 0.1, cy + 0.1),
                f"ring6/anchor{sx}",
            )
        )
    st.chan.append(circle((cx, cy), r + 0.5, 96))
    for side, y_edge, ang, key in ((-1, 0.0, 90.0, "A"), (1, H, -90.0, "B")):
        n2 = st.net(f"CP6_RING_{key}")
        st.sma.append(((cx, y_edge), ang, n2, f"J6R{key}"))
        y_end = cy + side * (r + 0.1 + 0.15 + 0.1)
        y_start = 2 * pin if side < 0 else H - 2 * pin
        f = Path((cx, y_start), math.pi / 2 if side < 0 else -math.pi / 2, 0.2)
        f.straight(abs(y_end - y_start))
        st.paths.append((n2, f))
        st.pads.append((n2, rect(cx - 0.1, y_end - 0.1, cx + 0.1, y_end + 0.1), f"ring6/{key}/end"))
        st.chan.append(outline(f, half=0.30))
    st.catalog.append(
        dict(
            id="CP6-RING",
            kind="gap-coupled ring, 1 λg near 5 GHz",
            radius_mm=round(r, 3),
            gap_mm=0.15,
        )
    )
    # 4-wire copper bars: L1 (F.Cu) and L3 (In2.Cu), 0.5 x 20 mm, sense taps 15 mm apart
    xb = cx + r + 1.6
    for k, layer in enumerate(("F.Cu", "In2.Cu")):
        net = st.net(f"CP6_R_{'L1' if k == 0 else 'L3'}")
        xx = xb + k * 2.2
        bar = Path((xx, 2.5), math.pi / 2, 0.5)
        bar.straight(20.0)
        if layer == "F.Cu":
            st.paths.append((net, bar))
            st.chan.append(outline(bar, half=0.6))
        else:
            st.l3_tracks.append((net, bar))
            st.l3_keep.append(outline(bar, half=0.6))
            st.chan.append(outline(bar, half=0.6))
        for i, yy in enumerate((2.5, 5.0, 20.0, 22.5)):
            st.pads.append((net, rect(xx - 0.5, yy - 0.5, xx + 0.5, yy + 0.5), f"bar{k}/{i}"))
            if layer != "F.Cu":
                st.vias.append(((xx, yy), 0.3, 0.6, net, True))
        st.catalog.append(
            dict(
                id=f"CP6-R-{'L1' if k == 0 else 'L3'}",
                kind="4-wire copper bar 0.5 x 20 mm",
                layer=layer,
            )
        )


def _filter(st: Strip):
    """Keep pinned vias; drop GND vias in a line's gap or closer than RULES["fence_pitch_min"] to a kept via."""
    from .geom import path_dist

    kept = []
    for v in st.vias:
        if len(v) > 4 and v[4]:
            kept.append(v)
    for v in st.vias:
        if len(v) > 4 and v[4]:
            continue
        xy, pad = v[0], v[2]
        if any(math.dist(xy, k[0]) < RULES["fence_pitch_min"] - 1e-6 for k in kept):
            continue
        if any(path_dist(xy, p_, 0.05) < p_.width / 2 + 0.2 + pad / 2 - 1e-6 for _, p_ in st.paths):
            continue
        kept.append(v)
    return kept


def build_strip() -> Strip:
    p = resolve()
    d = dims_mod.compute(p)
    st = Strip()
    st.net("GND")
    y = cp60(st, 1.0, 1.2, d)
    ring60(st, 12.5, 3.2, d)
    divider_b2b(st, 9.5, 7.6, d, p)
    launch_replicas(st, 18.0, 1.4, p)
    combs(st, 18.5, 5.9)
    tx_replica(st, (2.0, 14.0))
    antennas(st, 9.5, 12.8, p, d)
    librevna(st, 27.0)
    st.catalog.append(dict(id="_layout", next_free_y_left=y))
    return st


def write(st: Strip, out_dir: str) -> Dict[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    name = "coupons"
    items: List[str] = []
    for i, (net, p) in enumerate(st.paths):
        items += kc._path_items(p, net, f"cp/{i}")
    for i, (net, p) in enumerate(st.l3_tracks):
        items += [
            t.replace('(layer "F.Cu")', '(layer "In2.Cu")')
            for t in kc._path_items(p, net, f"cp3/{i}")
        ]
    for i, (net, poly, key) in enumerate(st.pads):
        cx = 0.5 * (min(q[0] for q in poly) + max(q[0] for q in poly))
        cy = 0.5 * (min(q[1] for q in poly) + max(q[1] for q in poly))
        k = kc._k((cx, cy))
        prim = " ".join(f"(xy {kc._n(q[0] - cx)} {kc._n(-(q[1] - cy))})" for q in poly)
        items.append(
            f'\t(footprint "radar60:CP_PAD" (layer "F.Cu") (uuid "{kc._u("cpfp", key)}") (at {kc._n(k[0])} '
            f"{kc._n(k[1])})\n"
            f'\t\t(property "Reference" "CP{i}" (at 0 0) (layer "F.Fab") (hide yes) (uuid '
            f'"{kc._u("cpref", key)}") (effects (font (size 0.3 0.3) (thickness 0.05))))\n'
            f'\t\t(property "Value" "{key}" (at 0 0) (layer "F.Fab") (hide yes) (uuid '
            f'"{kc._u("cpval", key)}") (effects (font (size 0.3 0.3) (thickness 0.05))))\n'
            "\t\t(attr smd exclude_from_pos_files exclude_from_bom)\n"
            f'\t\t(pad "1" smd custom (at 0 0) (size 0.05 0.05) (layers "F.Cu") (net "{net}") (uuid '
            f'"{kc._u("cppad", key)}")'
            f" (options (clearance outline) (anchor circle)) (primitives (gr_poly (pts {prim}) (width 0) "
            "(fill yes))))\n\t)"
        )
    for i, (xy, ang, net, ref) in enumerate(st.sma):
        k = kc._k(xy)
        rot = ang  # footprint +x points into the board
        items.append(
            '\t(footprint "radar60:SMA_EdgeLaunch_SMA-J-P-H-ST-EM1" (layer "F.Cu") (uuid '
            f'"{kc._u("sma", ref)}") (at {kc._n(k[0])} {kc._n(k[1])} {kc._n(rot)})\n'
            f'\t\t(property "Reference" "{ref}" (at 0 0 {kc._n(rot)}) (layer "F.Fab") (hide yes) (uuid '
            f'"{kc._u("smaref", ref)}") (effects (font (size 0.5 0.5) (thickness 0.08))))\n'
            f'\t\t(property "Value" "SMA-J-P-H-ST-EM1" (at 0 0 {kc._n(rot)}) (layer "F.Fab") (hide yes) '
            f'(uuid "{kc._u("smaval", ref)}") (effects (font (size 0.5 0.5) (thickness 0.08))))\n'
            "\t\t(attr smd)\n"
            f'\t\t(pad "1" smd custom (at 1.6 0 {kc._n(rot)}) (size 0.2 0.2) (layers "F.Cu") (net "{net}") '
            f'(uuid "{kc._u("smap", ref)}") (options (clearance outline) (anchor rect))'
            " (primitives (gr_poly (pts (xy -1.6 -0.635) (xy 0 -0.635) (xy 1.6 -0.1) (xy 1.6 0.1) (xy 0 "
            "0.635) (xy -1.6 0.635)) (width 0) (fill yes))))\n"
            + "".join(
                f'\t\t(pad "2" smd rect (at 1.6 {kc._n(sy)} {kc._n(rot)}) (size 3.2 1.35) (layers "{lay}" '
                f'"{lay[0]}.Mask") (net "GND") (uuid "{kc._u("smag", ref, sy, lay)}"))\n'
                for sy in (-2.825, 2.825)
                for lay in ("F.Cu", "B.Cu")
            )
            + "\t)"
        )
        a = math.radians(ang)
        c, s = math.cos(a), math.sin(a)

        def at(u, v, xy=xy, c=c, s=s):
            return (xy[0] + u * c - v * s, xy[1] + u * s + v * c)

        st.chan.append(
            [
                at(-0.5, -0.95),
                at(1.6, -0.95),
                at(3.6, -0.30),
                at(3.6, 0.30),
                at(1.6, 0.95),
                at(-0.5, 0.95),
            ]
        )
        for u in (3.3, 4.0):
            for v in (-1.2, 1.2):
                st.vias.insert(0, (at(u, v), 0.3, 0.6, "GND", True))
    for i, v in enumerate(_filter(st)):
        xy, drill, pad = v[0], v[1], v[2]
        net = v[3] if len(v) > 3 else "GND"
        k = kc._k(xy)
        items.append(
            f"\t(via (at {kc._n(k[0])} {kc._n(k[1])}) (size {kc._n(pad)}) (drill {kc._n(drill)}) (layers "
            f'"F.Cu" "B.Cu") (net "{net}") (uuid "{kc._u("cpvia", i)}"))'
        )
    W, H = STRIP
    e = RULES["edge_clear"]
    plane = [(e, e), (W - e, e), (W - e, H - e), (e, H - e)]
    for lay, key in (("F.Cu", "f"), ("In1.Cu", "l2"), ("In2.Cu", "l3"), ("B.Cu", "b")):
        items.append(kc._zone("GND", lay, plane, f"cp/{key}"))
    for i, poly in enumerate(st.chan):
        items.append(kc._keepout(["F.Cu"], poly, f"cpch{i}"))
    for i, poly in enumerate(st.l2_keep):
        items.append(kc._keepout(["In1.Cu"], poly, f"cpl2{i}"))
    for i, poly in enumerate(st.l3_keep):
        items.append(kc._keepout(["In2.Cu"], poly, f"cpl3{i}"))
    mask = "\n".join(
        f"\t\t(fp_poly (pts {kc._pts(rect(0.4, 0.4, W - 0.4, H - 0.4), (0.0, 0.0))}) (stroke (width 0) "
        f'(type solid)) (fill yes) (layer "F.Mask") (uuid "{kc._u("cpmask")}"))'
        for _ in (0,)
    )
    items.append(
        f'\t(footprint "radar60:RFM1_MASK" (layer "F.Cu") (uuid "{kc._u("cpmaskfp")}") (at {kc._n(kc.X0)} '
        f"{kc._n(kc.Y0)})\n"
        f'\t\t(property "Reference" "MO1" (at 0 0) (layer "F.Fab") (hide yes) (uuid "{kc._u("cpmo")}") '
        "(effects (font (size 0.5 0.5) (thickness 0.08))))\n"
        '\t\t(property "Value" "coupon mask opening" (at 0 0) (layer "F.Fab") (hide yes) (uuid '
        f'"{kc._u("cpmov")}") (effects (font (size 0.5 0.5) (thickness 0.08))))\n'
        "\t\t(attr smd exclude_from_pos_files exclude_from_bom)\n" + mask + "\n\t)"
    )
    e0, e1 = kc._k((0.0, H)), kc._k((W, 0.0))
    items.append(
        f"\t(gr_rect (start {kc._n(e0[0])} {kc._n(e0[1])}) (end {kc._n(e1[0])} {kc._n(e1[1])}) (stroke "
        f'(width 0.05) (type solid)) (fill no) (layer "Edge.Cuts") (uuid "{kc._u("cpedge")}"))'
    )
    text = (
        kc._header("radar60 Rev A coupon strip (conventional set)", st.nets)
        + "\n".join(items)
        + "\n)\n"
    )
    paths = {}
    fn = os.path.join(out_dir, f"{name}.kicad_pcb")
    with open(fn, "w", encoding="utf-8") as fh:
        fh.write(text)
    paths["pcb"] = fn
    pro = kc.project_json(name)
    pro["board"]["design_settings"]["rules"]["min_copper_edge_clearance"] = 0.0  # edge launches
    fn = os.path.join(out_dir, f"{name}.kicad_pro")
    with open(fn, "w", encoding="utf-8") as fh:
        json.dump(pro, fh, indent=2)
        fh.write("\n")
    paths["pro"] = fn
    fn = os.path.join(out_dir, f"{name}.kicad_dru")
    with open(fn, "w", encoding="utf-8") as fh:
        fh.write(kc.dru_text())
    paths["dru"] = fn
    fn = os.path.join(out_dir, "coupons.json")
    with open(fn, "w", encoding="utf-8") as fh:
        json.dump(dict(strip_mm=list(STRIP), structures=st.catalog), fh, indent=1, default=float)
        fh.write("\n")
    paths["catalog"] = fn
    return paths


def build_and_write(out_dir: str) -> Dict[str, str]:
    return write(build_strip(), out_dir)
