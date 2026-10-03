"""KiCad layout of board O, the OSH Park 4-layer Order 0 uploads (Order 0 design §3-§6).

Every structure is a stick with the Cinch 142-0701-851 launch of its region (`launch.py`) on each
port's milled edge. Region M is L1 microstrip over In1.Cu (12 mm sticks), region W L1 over B.Cu
with In1.Cu and In2.Cu removed but for a stitched perimeter ring (16 mm sticks). The L1 ground
stays 1.0 mm (M) or 4.2 mm (W) from all RF copper, the mask is open over the RF copper and that
keep-away (the masked line A10 aside), with 0.2 mm dams at the pin pads.

The panel (`pack`) is OSH Park's frameless form: one outline, the sticks separated by 2.54 mm
milled slots (OSH Park's 0.1 in between outlines) and held by OSH Park's suggested tab (0.1 in
wide, three 0.020 in holes at 0.040 in on each edge [O-panel, the tab drawing]) on edges away
from every launch (at least 6.1 mm from a launch end: past the leg pads). numpy only.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from yapnr.rf.coupons import catalog, families, launch, qr
from yapnr.rf.coupons.layout import (
    Frame,
    Placed,
    Writer,
    _circle_pts,
    _n,
    _u,
    label_width,
    line_start,
)

SLOT = 2.54  # OSH Park: 0.1 in between outlines [O-panel]
TAB_W = 2.54  # OSH Park's suggested tab: 0.10 in wide [O-panel, tab drawing]
BITE_D, BITE_PITCH, BITES = 0.508, 1.016, 3  # 0.020 in holes at 0.040 in, three per edge
TAB_CLEAR = 6.1  # tab edge from a launch end: past the leg pads (Cinch E = 5.08 mm) + 1 mm
TAB_EVERY = 30.0  # mm of long edge per tab
STITCH = 3.0  # stitching via pitch (design §4.2: <= 3 mm)
ORIGIN = (20.0, 20.0)
COPY_BOX = 3.0  # the copy-number box (design §6)
SILK_RF = 1.5  # no silkscreen within 1.5 mm of RF copper (design §6)
FLEX = ("thru", "line", "verify", "reflect", "variant", "switch", "cpad")
FLEX_MAX = 6.0  # at most this much wider
SEARCH_ORDERS, SEARCH_SEED = 24, 7  # random placement orders tried by `pack` (deterministic)


# --- sticks --------------------------------------------------------------------------------


def region(s: catalog.Stick) -> str:
    return s.geometry.get("region", "M")


def _ld(wr: Writer, s: catalog.Stick):
    return wr.lds[region(s)]


def _nets(wr: Writer, s: catalog.Stick):
    return wr.net(f"RF_{s.id}"), wr.net(f"GND_{s.id}")


def _poly_prof(fr: Frame, prof: Sequence[Tuple[float, float]]) -> List[Tuple[float, float]]:
    """A channel polygon from a launch-coordinate profile [(x, half width)]."""
    top = [fr.p(x, -hw) for x, hw in prof]
    bot = [fr.p(x, hw) for x, hw in reversed(prof)]
    pts = top + bot
    return [p for i, p in enumerate(pts) if i == 0 or math.dist(p, pts[i - 1]) > 1e-6]


def _rect(pl: Placed, x0, x1, y0, y1) -> List[Tuple[float, float]]:
    return [pl.p(x0, y0), pl.p(x1, y0), pl.p(x1, y1), pl.p(x0, y1)]


def _w_inner(wr: Writer, pl: Placed, rects: Sequence[Tuple[float, float, float, float]], key):
    """Region W: In1.Cu and In2.Cu removed over the given stick rectangles."""
    inner = wr.layers[1:-1]
    for k, (x0, x1, y0, y1) in enumerate(rects):
        wr.keepout(inner, _rect(pl, x0, x1, y0, y1), key + (k,))


def _open_end(wr: Writer, pl: Placed, fr: Frame, x_end: float, w: float, net: str, key):
    """An open line end exactly at x_end (launch coordinates): the round-ended track stops half
    a width short and a square copper pad squares it off."""
    wr.footprint(
        "Anchor",
        fr.p(x_end - w / 2, 0.0),
        fr.angle,
        [
            f'\t\t(pad "1" smd rect (at 0 0 {_n(fr.angle)}) (size {_n(w)} {_n(w)}) (layers "F.Cu")'
            f' (net "{net}") (uuid "{_u("oend", key)}"))'
        ],
        ("oend",) + key,
    )


def _line(wr, fr: Frame, x0, x1, w, net, key):
    wr.seg(fr.p(x0, 0.0), fr.p(x1, 0.0), w, "F.Cu", net, key)


def _mask(wr: Writer, pts, name: str):
    wr.mask_opening(pts, name)


def _stitch(wr: Writer, pl: Placed, net: str, keep, key, margin=0.9):
    """Ground stitching on a STITCH grid over the stick, away from the given stick-coordinate
    keep-out boxes (x0, x1, y0, y1) and the stick edges."""
    s = pl.stick
    hh = s.height / 2 - margin
    xs = np.arange(margin + 0.6, s.length - margin, STITCH)
    ys = np.arange(-hh, hh + 1e-9, STITCH)
    for i, x in enumerate(xs):
        for j, y in enumerate(ys):
            if any(a - 0.6 <= x <= b + 0.6 and c - 0.6 <= y <= d + 0.6 for a, b, c, d in keep):
                continue
            wr.via(pl.p(float(x), float(y)), net, key + (i, j))


def _ticks(wr: Writer, pl: Placed, xs: Sequence[float], key, sides=(-1, 1), inner=1.3):
    """Reference-plane ticks on the long edges (0.5 to `inner` mm in) at stick x positions."""
    hh = pl.stick.height / 2
    for x in xs:
        for sgn in sides:
            wr.gr_line(
                pl.p(x, sgn * (hh - 0.5)), pl.p(x, sgn * (hh - inner)), "F.SilkS", key + (x, sgn)
            )


def _ticks_y(wr: Writer, pl: Placed, ys: Sequence[float], key):
    """Reference-plane ticks on the short edges (W and E) at stick y positions (the N and S
    ports of a 3-port stick)."""
    L = pl.stick.length
    for y in ys:
        for x0, x1 in ((0.5, 1.3), (L - 0.5, L - 1.3)):
            wr.gr_line(pl.p(x0, y), pl.p(x1, y), "F.SilkS", key + (x0, y))


def _text(wr: Writer, pl: Placed, text: str, x: float, y: float, key, vertical=False, size=1.0):
    ang = pl.angle + (90.0 if vertical else 0.0)
    wr.text(text, pl.p(x, y), key, size=size, angle=ang)


def _copy_box(wr: Writer, pl: Placed, x: float, y: float, key, sid: str = ""):
    """A 3 x 3 mm box for the copy number on the back silkscreen (the back is ground under
    mask: no RF field, no mask opening), its top-left corner at stick (x, y), the stick id
    beside it."""
    c = COPY_BOX
    pts = [(x, y), (x + c, y), (x + c, y + c), (x, y + c)]
    for k in range(4):
        wr.gr_line(pl.p(*pts[k]), pl.p(*pts[(k + 1) % 4]), "B.SilkS", key + (k,), width=0.15)
    if sid:
        wr.text(
            sid,
            pl.p(x + c + 0.6 + label_width(sid), y + c / 2),
            key + ("id",),
            size=1.0,
            layer="B.SilkS",
            angle=pl.angle,
            justify="left mirror",
        )


def _label_x(s: catalog.Stick, x_free: float, ticks: Sequence[float]) -> Tuple[str, float]:
    """The longest form of the stick's label (full; without the upload prefix; the id) that
    fits between the leg pads (`x_free` from each end) and the reference-plane ticks, and
    where it starts."""
    xs = sorted(
        set([x_free] + [t for t in ticks if x_free < t < s.length - x_free] + [s.length - x_free])
    )
    segs = [
        (a + (0.3 if a != x_free else 0.0), b - (0.3 if b != s.length - x_free else 0.0))
        for a, b in zip(xs, xs[1:])
    ]
    label = s.label or s.id
    forms = [label, label.split(" ", 1)[1] if label.startswith("O0-") else label, s.id]
    for t in forms:
        for a, b in sorted(segs, key=lambda ab: -(ab[1] - ab[0])):
            if label_width(t) <= b - a:
                return t, a
    return s.id, segs[0][0] if segs else x_free


def _label_2port(wr: Writer, pl: Placed, x_free: float):
    """Label (the longest form that fits between the ticks), RP ticks on both long edges, and
    the copy box on the back."""
    s = pl.stick
    hh = s.height / 2
    rp1, rp2 = s.rp
    text, x0 = _label_x(s, x_free, [rp1, rp2])
    _text(wr, pl, text, x0, -hh + 1.4, ("lab", s.id))
    _ticks(wr, pl, sorted(set(s.rp)), ("rp", s.id))
    _copy_box(wr, pl, s.length / 2 - 4.0, -COPY_BOX / 2, ("box", s.id), s.id)


def _launch_pair(
    wr: Writer, pl: Placed, sig: str, gnd: str, ld, axis=0.0, half_w=None, x_max=None, sig_r=None
):
    fl = wr.edge_launch(pl, "W", sig, gnd, axis, x_max=x_max, ld=ld, half_w=half_w)
    fr = wr.edge_launch(pl, "E", sig_r or sig, gnd, axis, x_max=x_max, ld=ld, half_w=half_w)
    return fl, fr


def line_stick(wr: Writer, pl: Placed):
    """Thru, line, verify (and A04R, rotated in the panel): both launches, the region's line
    between them, the channel at the keep-away, the mask open from dam to dam, a sparse fence
    past the launch fences, W's inner-layer void, stitching and labels."""
    s = pl.stick
    ld = _ld(wr, s)
    sig, gnd = _nets(wr, s)
    L, half = s.length, s.height / 2
    wr.stick_zones(pl, gnd)
    if ld.region == "W":
        r = min(launch.W_RING, half - 1.0)
        _w_inner(wr, pl, [(0, L, -r, r)], (s.id, "wring"))
    fl, fr = _launch_pair(wr, pl, sig, gnd, ld)
    prof = ld.channel(x_to=L / 2)
    wr.keepout(["F.Cu"], _poly_prof(fl, prof), (s.id, "chL"))
    wr.keepout(["F.Cu"], _poly_prof(fr, prof), (s.id, "chR"))
    xs = line_start(ld)
    _line(wr, fl, xs, L - xs, ld.line_w, sig, (s.id, "line"))
    xm = ld.x_pe + launch.DAM_W
    mprof = [(xm, ld.channel_at(xm))] + [(x, hw) for x, hw in prof if xm < x < L / 2]
    mprof += [(L / 2, ld.line_w / 2 + ld.keepaway)]
    full = mprof + [(L - x, hw) for x, hw in reversed(mprof)]
    _mask(wr, _poly_prof(fl, full), f"MO_{s.id}")
    xf = ld.x_end + launch.FENCE_LINE + 1.5
    if L - 2 * xf > 0:
        fence_line(wr, pl, ld, gnd, xf, L - xf, (s.id, "lf"))
    xl = ld.conn.lay_e + 1.2
    hw_max = ld.line_w / 2 + ld.keepaway
    keep = [(0, xl, -half, half), (L - xl, L, -half, half), (0, L, -hw_max - 0.8, hw_max + 0.8)]
    _stitch(wr, pl, gnd, keep, (s.id, "st"))
    _label_2port(wr, pl, xl)


def fence_line(wr: Writer, pl: Placed, ld, gnd, x0, x1, key, axis=0.0, pitch=STITCH):
    """Ground vias along both sides of a line's keep-away channel, stick x from x0 to x1."""
    off = ld.line_w / 2 + ld.keepaway + launch.FENCE_OFF
    for i, x in enumerate(np.arange(x0, x1 + 1e-9, pitch)):
        for sgn in (-1, 1):
            y = axis + sgn * off
            if abs(y) > pl.stick.height / 2 - 0.9:
                continue
            wr.via(pl.p(float(x), y), gnd, key + (i, sgn))


def reflect_stick(wr: Writer, pl: Placed):
    """The mTRL reflect: the line open at both reference planes (design §4.2 A06, §4.3 B05)."""
    s = pl.stick
    ld = _ld(wr, s)
    sig, gnd = _nets(wr, s)
    L, half = s.length, s.height / 2
    rp1, _ = s.rp
    sig2 = wr.net(f"RF_{s.id}_2")
    wr.stick_zones(pl, gnd)
    if ld.region == "W":
        r = min(launch.W_RING, half - 1.0)
        _w_inner(wr, pl, [(0, L, -r, r)], (s.id, "wring"))
    x_end = rp1 + ld.keepaway
    fl, fr = _launch_pair(wr, pl, sig, gnd, ld, x_max=min(x_end, L / 2 - 0.45), sig_r=sig2)
    prof = ld.channel(x_to=x_end)
    xm = ld.x_pe + launch.DAM_W
    mprof = [(xm, ld.channel_at(xm))] + [(x, hw) for x, hw in prof if x > xm]
    for fr_, net, k in ((fl, sig, "L"), (fr, sig2, "R")):
        wr.keepout(["F.Cu"], _poly_prof(fr_, prof), (s.id, "ch", k))
        _mask(wr, _poly_prof(fr_, mprof), f"MO_{s.id}{k}")
        _line(wr, fr_, line_start(ld), rp1 - ld.line_w / 2, ld.line_w, net, (s.id, "l", k))
        _open_end(wr, pl, fr_, rp1, ld.line_w, net, (s.id, k))
    xl = ld.conn.lay_e + 1.2
    hw_max = ld.line_w / 2 + ld.keepaway
    keep = [
        (0, xl, -half, half),
        (L - xl, L, -half, half),
        (0, x_end, -hw_max - 0.8, hw_max + 0.8),
        (L - x_end, L, -hw_max - 0.8, hw_max + 0.8),
    ]
    _stitch(wr, pl, gnd, keep, (s.id, "st"))
    _label_2port(wr, pl, xl)


def variant_stick(wr: Writer, pl: Placed):
    """The width set and the masked line: M at the launches, the variant family between the
    reference planes; the masked line keeps its mask between the reference planes."""
    s = pl.stick
    ld = _ld(wr, s)
    sig, gnd = _nets(wr, s)
    L, half = s.length, s.height / 2
    rp1, rp2 = s.rp
    fam = families.get(s.family, wr.st.id)
    wr.stick_zones(pl, gnd)
    fl, fr = _launch_pair(wr, pl, sig, gnd, ld)
    hv = fam.w / 2 + ld.keepaway
    prof = ld.channel(x_to=rp1) + [(rp1, hv)]
    full = prof + [(L - x, hw) for x, hw in reversed(prof)]
    wr.keepout(["F.Cu"], _poly_prof(fl, full), (s.id, "ch"))
    xs = line_start(ld)
    _line(wr, fl, xs, rp1, ld.line_w, sig, (s.id, "l1"))
    _line(wr, fl, rp1, rp2, fam.w, sig, (s.id, "l2"))
    _line(wr, fl, rp2, L - xs, ld.line_w, sig, (s.id, "l3"))
    xm = ld.x_pe + launch.DAM_W
    mprof = [(xm, ld.channel_at(xm))] + [(x, hw) for x, hw in prof if x > xm]
    if fam.mask:
        _mask(wr, _poly_prof(fl, mprof), f"MO_{s.id}L")
        _mask(wr, _poly_prof(fr, mprof), f"MO_{s.id}R")
    else:
        mfull = mprof + [(L - x, hw) for x, hw in reversed(mprof)]
        _mask(wr, _poly_prof(fl, mfull), f"MO_{s.id}")
    xf = ld.x_end + launch.FENCE_LINE + 1.5
    fence_line(wr, pl, _Wide(ld, fam.w), gnd, xf, L - xf, (s.id, "lf"))
    xl = ld.conn.lay_e + 1.2
    hw_max = max(ld.line_w / 2 + ld.keepaway, hv)
    keep = [(0, xl, -half, half), (L - xl, L, -half, half), (0, L, -hw_max - 0.8, hw_max + 0.8)]
    _stitch(wr, pl, gnd, keep, (s.id, "st"))
    _label_2port(wr, pl, xl)


@dataclass
class _Wide:
    """A launch design seen with another line width (for fence_line)."""

    ld: object
    line_w: float

    @property
    def keepaway(self):
        return self.ld.keepaway


def switch_stick(wr: Writer, pl: Placed):
    """A14: the M line with a shunt 0402 100 Ω resistor 5 mm from RP1, its far pad on two vias
    to the ground planes (design §4.2)."""
    line_stick(wr, pl)
    s = pl.stick
    _, gnd = _nets(wr, s)
    sig = f"RF_{s.id}"
    ld = _ld(wr, s)
    x = s.rp[0] + s.geometry["x_shunt"]
    # KiCad's R_0402_1005Metric pads (0.54 x 0.64 mm at ±0.51 mm), the resistor across the
    # line: pad 1 overlaps the line edge by 0.10 mm
    y1 = ld.line_w / 2 + 0.27 - 0.10
    y2 = y1 + 1.02
    ang = pl.angle - 90.0  # pad 1 toward the line, pad 2 away from it
    body = [
        f'\t\t(pad "1" smd roundrect (at -0.51 0 {_n(ang)}) (size 0.54 0.64) (layers "F.Cu" "F.Mask" "F.Paste")'
        f' (roundrect_rratio 0.25) (net "{sig}") (uuid "{_u("r1", s.id)}"))',
        f'\t\t(pad "2" smd roundrect (at 0.51 0 {_n(ang)}) (size 0.54 0.64) (layers "F.Cu" "F.Mask" "F.Paste")'
        f' (roundrect_rratio 0.25) (net "{gnd}") (uuid "{_u("r2", s.id)}"))',
        f"\t\t(fp_rect (start -0.5 -0.25) (end 0.5 0.25) (stroke (width 0.1) (type solid)) (fill no)"
        f' (layer "F.Fab") (uuid "{_u("rfab", s.id)}"))',
        f"\t\t(fp_rect (start -0.93 -0.47) (end 0.93 0.47) (stroke (width 0.05) (type solid))"
        f' (fill no) (layer "F.CrtYd") (uuid "{_u("rcrt", s.id)}"))',
    ]
    wr.footprint(
        "R_0402_1005Metric", pl.p(x, (y1 + y2) / 2), ang, body, ("res", s.id), ref=f"R{s.id}"
    )
    wr.items.footprints[-1] = wr.items.footprints[-1].replace(
        '(property "Value" "R_0402_1005Metric"', '(property "Value" "100R 1% 0402"'
    )
    yv = y2 + 0.32 + 0.127 + launch.VIA_D / 2 + 0.01
    wr.seg(pl.p(x, y2), pl.p(x, yv), 0.4, "F.Cu", gnd, (s.id, "rg"))
    wr.seg(pl.p(x - 0.45, yv), pl.p(x + 0.45, yv), 0.4, "F.Cu", gnd, (s.id, "rg2"))
    for k, dx in enumerate((-0.45, 0.45)):
        wr.via(pl.p(x + dx, yv), gnd, (s.id, "rv", k))


def cpad_stick(wr: Writer, pl: Placed):
    """A16: a 6 mm and a 12 mm square pad on L1 over solid In1.Cu, each at the reference plane
    of a standard launch half (two 1-ports)."""
    s = pl.stick
    ld = _ld(wr, s)
    _, gnd = _nets(wr, s)
    n1, n2 = wr.net(f"RF_{s.id}_1"), wr.net(f"RF_{s.id}_2")
    L, half = s.length, s.height / 2
    rp1, rp2 = s.rp
    a, b = s.geometry["pads"]
    wr.stick_zones(pl, gnd)
    fl, fr = _launch_pair(wr, pl, n1, gnd, ld, x_max=rp1 - 0.3, sig_r=n2)
    keep_boxes = []
    for fr_, net, size, k in ((fl, n1, a, "1"), (fr, n2, b, "2")):
        prof = ld.channel(x_to=rp1) + [
            (rp1, size / 2 + ld.keepaway),
            (rp1 + size + ld.keepaway, size / 2 + ld.keepaway),
        ]
        wr.keepout(["F.Cu"], _poly_prof(fr_, prof), (s.id, "ch", k))
        xm = ld.x_pe + launch.DAM_W
        mprof = [(xm, ld.channel_at(xm))] + [(x, hw) for x, hw in prof if x > xm]
        _mask(wr, _poly_prof(fr_, mprof), f"MO_{s.id}_{k}")
        _line(wr, fr_, line_start(ld), rp1 + ld.line_w / 2, ld.line_w, net, (s.id, "l", k))
        wr.footprint(
            "CPad",
            fr_.p(rp1 + size / 2, 0.0),
            fr_.angle,
            [
                f'\t\t(pad "1" smd rect (at 0 0 {_n(fr_.angle)}) (size {_n(size)} {_n(size)})'
                f' (layers "F.Cu") (net "{net}") (uuid "{_u("cpad", s.id, k)}"))'
            ],
            ("cpad", s.id, k),
            ref=f"P{s.id}_{k}",
        )
        x0, x1 = (0.0, rp1 + size + ld.keepaway) if k == "1" else (L - rp1 - size - ld.keepaway, L)
        keep_boxes.append((x0, x1, -size / 2 - ld.keepaway, size / 2 + ld.keepaway))
    xl = ld.conn.lay_e + 1.2
    keep = keep_boxes + [(0, xl, -half, half), (L - xl, L, -half, half)]
    _stitch(wr, pl, gnd, keep, (s.id, "st"))
    _text(wr, pl, s.label or s.id, rp1 + a + ld.keepaway + 0.8, -half + 1.4, ("lab", s.id))
    _ticks(wr, pl, [rp1, rp2], ("rp", s.id), inner=half - b / 2 - ld.keepaway - 0.2)
    _copy_box(wr, pl, L / 2 - 4.0, -COPY_BOX / 2, ("box", s.id), s.id)


def ring_stick(wr: Writer, pl: Placed):
    """A11: the directly fed M ring, the feeds a quarter turn apart, the port axis off the
    stick centre (catalog._o_ring_geometry)."""
    s = pl.stick
    g = s.geometry
    ld = _ld(wr, s)
    sig, gnd = _nets(wr, s)
    L, half = s.length, s.height / 2
    ay, r, arc = g["axis_y"], g["radius"], g["arc"]
    rp1, rp2 = s.rp
    w = ld.line_w
    hw_top = half + ay  # axis to the north edge
    wr.stick_zones(pl, gnd)
    fl, fr = _launch_pair(wr, pl, sig, gnd, ld, axis=ay, half_w=hw_top, x_max=rp1)
    cx, cy = L / 2, ay + r * math.cos(math.pi * arc)
    half_chord = r * math.sin(math.pi * arc)
    f1, f2 = cx - half_chord, cx + half_chord
    for fr_, k in ((fl, "L"), (fr, "R")):
        prof = ld.channel(x_to=f1)
        wr.keepout(["F.Cu"], _poly_prof(fr_, prof), (s.id, "ch", k))
        xm = ld.x_pe + launch.DAM_W
        mprof = [(xm, ld.channel_at(xm))] + [(x, hw) for x, hw in prof if x > xm]
        _mask(wr, _poly_prof(fr_, mprof), f"MO_{s.id}{k}")
        _line(wr, fr_, line_start(ld), f1, w, sig, (s.id, "f", k))
    rk = r + w / 2 + ld.keepaway
    disc = _circle_pts(*pl.p(cx, cy), rk, 72)
    wr.keepout(["F.Cu"], disc, (s.id, "disc"))
    _mask(wr, disc, f"MO_{s.id}D")
    # the ring as arcs split at the feed points (angles counter-clockwise from east, y up):
    # the short arc through the north between the feeds, the long arc in three parts
    a0 = 90.0 - 180.0 * arc  # the east feed
    a1 = 180.0 - a0  # the west feed
    spans = [(a0, a1)]
    step = (360.0 - (a1 - a0)) / 3
    spans += [(a1 + k * step, a1 + (k + 1) * step) for k in range(3)]

    def pt(ang):
        t = math.radians(ang)
        return pl.p(cx + r * math.cos(t), cy - r * math.sin(t))

    for k, (u, v) in enumerate(spans):
        st, md, en = pt(u), pt(0.5 * (u + v)), pt(v)
        wr.items.tracks.append(
            f"\t(arc (start {_n(st[0])} {_n(st[1])}) (mid {_n(md[0])} {_n(md[1])})"
            f' (end {_n(en[0])} {_n(en[1])}) (width {_n(w)}) (layer "F.Cu") (net "{sig}"))'
        )
    # ring fence: vias around the disc, off the feeds and inside the stick
    rf = rk + launch.FENCE_OFF
    for k, (x, y) in enumerate(_circle_pts(cx, cy, rf, 64)):
        if abs(y - ay) < w / 2 + ld.keepaway + 0.9 and min(abs(x - f1), abs(x - f2)) < 6.0:
            continue
        if abs(y) > half - 0.9 or x < 0.9 or x > L - 0.9:
            continue
        wr.via(pl.p(x, y), gnd, (s.id, "rf", k))
    xl = ld.conn.lay_e + 1.2
    keep = [
        (0, xl, ay - hw_top, ay + hw_top),
        (L - xl, L, ay - hw_top, ay + hw_top),
        (0, L, ay - w / 2 - ld.keepaway - 0.8, ay + w / 2 + ld.keepaway + 0.8),
        (cx - rf - 0.3, cx + rf + 0.3, cy - rf - 0.3, cy + rf + 0.3),
    ]
    _stitch(wr, pl, gnd, keep, (s.id, "st"))
    _text(wr, pl, s.label or s.id, 0.9, half - 2.4, ("lab", s.id))
    _ticks(wr, pl, [rp1, rp2], ("rp", s.id), sides=(-1,))
    _copy_box(wr, pl, L / 2 - 4.0, -COPY_BOX / 2, ("box", s.id), s.id)


def stub_stick(wr: Writer, pl: Placed):
    """A12: the open λ/4 stub, shunt at the middle of the 15 mm M line, toward the wide side."""
    s = pl.stick
    g = s.geometry
    ld = _ld(wr, s)
    sig, gnd = _nets(wr, s)
    L, half = s.length, s.height / 2
    ay, ls = g["axis_y"], g["stub_mm"]
    w = ld.line_w
    hw_top = half + ay
    wr.stick_zones(pl, gnd)
    fl, fr = _launch_pair(wr, pl, sig, gnd, ld, axis=ay, half_w=hw_top)
    prof = ld.channel(x_to=L / 2)
    for fr_, k in ((fl, "L"), (fr, "R")):
        wr.keepout(["F.Cu"], _poly_prof(fr_, prof), (s.id, "ch", k))
    xs = line_start(ld)
    _line(wr, fl, xs, L - xs, w, sig, (s.id, "line"))
    cx = L / 2
    y_end = ay + w / 2 + ls  # the stub's open end
    k_ = ld.keepaway
    stub_ch = [
        (cx - w / 2 - k_, ay),
        (cx + w / 2 + k_, ay),
        (cx + w / 2 + k_, y_end + k_),
        (cx - w / 2 - k_, y_end + k_),
    ]
    wr.keepout(["F.Cu"], [pl.p(*p) for p in stub_ch], (s.id, "stubch"))
    wr.seg(pl.p(cx, ay), pl.p(cx, y_end - w / 2), w, "F.Cu", sig, (s.id, "stub"))
    wr.footprint(
        "Anchor",
        pl.p(cx, y_end - w / 2),
        pl.angle,
        [
            f'\t\t(pad "1" smd rect (at 0 0 {_n(pl.angle)}) (size {_n(w)} {_n(w)}) (layers "F.Cu")'
            f' (net "{sig}") (uuid "{_u("send", s.id)}"))'
        ],
        ("send", s.id),
    )
    xm = ld.x_pe + launch.DAM_W
    mprof = [(xm, ld.channel_at(xm))] + [(x, hw) for x, hw in prof if xm < x < L / 2]
    mprof += [(L / 2, w / 2 + k_)]
    full = mprof + [(L - x, hw) for x, hw in reversed(mprof)]
    _mask(wr, _poly_prof(fl, full), f"MO_{s.id}")
    _mask(wr, [pl.p(*p) for p in stub_ch], f"MO_{s.id}S")
    # fence along the stub, both sides
    off = w / 2 + k_ + launch.FENCE_OFF
    for i, y in enumerate(np.arange(ay + w / 2 + k_ + 0.8, y_end + k_ + 0.6, 1.0)):
        for sgn in (-1, 1):
            wr.via(pl.p(cx + sgn * off, float(y)), gnd, (s.id, "sf", i, sgn))
    xl = ld.conn.lay_e + 1.2
    hw = w / 2 + k_
    keep = [
        (0, xl, ay - hw_top, ay + hw_top),
        (L - xl, L, ay - hw_top, ay + hw_top),
        (0, L, ay - hw - 0.8, ay + hw + 0.8),
        (cx - hw - 0.8, cx + hw + 0.8, ay, y_end + k_ + 0.8),
    ]
    _stitch(wr, pl, gnd, keep, (s.id, "st"))
    _text(wr, pl, s.label or s.id, 0.9, half - 1.4, ("lab", s.id))
    _ticks(wr, pl, list(s.rp), ("rp", s.id), sides=(-1,))
    _copy_box(wr, pl, L / 2 - 4.0, half - 1.2 - COPY_BOX, ("box", s.id), s.id)


def demo_stick(wr: Writer, pl: Placed):
    """A 3-port demo stick (R1, R1t: the reference's copper; D1, D2: a placeholder window):
    launches on the W, N and S edges, straight feeds L_f from each reference plane to the
    window, the window and its keep-away free of other L1 copper, the mask open over all of it,
    and on W the inner layers removed under the window, the feeds and the launches."""
    s = pl.stick
    g = s.geometry
    reg = region(s)
    ld = wr.lds[reg]
    _, gnd = _nets(wr, s)
    win = g["window"]
    L, H = s.length, s.height
    xw, lf = win["x0"], win["feed"]
    ww, wh = win["w"], win["h"]
    k_ = ld.keepaway
    placeholder = s.kind == "window"
    nets = {}
    for p in g["ports"]:
        nets[p["n"]] = wr.net(f"RF_{s.id}_{p['n']}") if placeholder else wr.net(f"RF_{s.id}")
    wr.stick_zones(pl, gnd)
    x_win = _window_x(lf)
    hw_stick = g["stick_w"] / 2
    if reg == "W":
        r = launch.W_RING
        rects = [(0, xw, -r, r), (xw - k_, xw + ww + k_, -wh / 2 - k_, wh / 2 + k_)]
        for p in g["ports"]:
            if p["side"] in ("N", "S"):
                y0, y1 = (-H / 2, -wh / 2) if p["side"] == "N" else (wh / 2, H / 2)
                rects.append((p["at"] - r, p["at"] + r, y0, y1))
        _w_inner(wr, pl, rects, (s.id, "void"))
    frames = []
    for p in g["ports"]:
        side = p["side"]
        at = 0.0 if side == "W" else p["at"]
        fr = wr.edge_launch(
            pl,
            side,
            nets[p["n"]],
            gnd,
            at,
            x_max=x_win,
            ld=ld,
            half_w=hw_stick,
            ref=f"J{s.id}_{p['n']}",
        )
        frames.append((p, fr))
        prof = ld.channel(x_to=x_win)
        wr.keepout(["F.Cu"], _poly_prof(fr, prof), (s.id, "ch", p["n"]))
        xm = ld.x_pe + launch.DAM_W
        mprof = [(xm, ld.channel_at(xm))] + [(x, hw) for x, hw in prof if x > xm]
        _mask(wr, _poly_prof(fr, mprof), f"MO_{s.id}_{p['n']}")
        x_end = x_win
        if placeholder:
            _line(
                wr,
                fr,
                line_start(ld),
                x_end - ld.line_w / 2,
                ld.line_w,
                nets[p["n"]],
                (s.id, "f", p["n"]),
            )
            _open_end(wr, pl, fr, x_end, ld.line_w, nets[p["n"]], (s.id, p["n"]))
        else:
            _line(wr, fr, line_start(ld), x_end, ld.line_w, nets[p["n"]], (s.id, "f", p["n"]))
    wk = _rect(pl, xw - k_, xw + ww + k_, -wh / 2 - k_, wh / 2 + k_)
    wr.keepout(["F.Cu"], wk, (s.id, "win"))
    _mask(wr, wk, f"MO_{s.id}_W")
    # the window outline on the fabrication and drawing layers
    wo = _rect(pl, xw, xw + ww, -wh / 2, wh / 2)
    for k in range(4):
        wr.gr_line(wo[k], wo[(k + 1) % 4], "Dwgs.User", (s.id, "wo", k), width=0.05)
    if placeholder:
        wr.text(
            f"{s.id} window {ww:g} x {wh:g}",
            pl.p(xw + 0.5, -wh / 2 + 1.5),
            (s.id, "wtxt"),
            size=0.8,
            layer="Dwgs.User",
            angle=pl.angle,
        )
    else:
        copper_pad(wr, pl, s, xw, nets[1])
    # fences beside the feeds were placed by the launches (to x_win); stitch the rest
    keep = [(xw - k_ - 0.3, xw + ww + k_ + 0.3, -wh / 2 - k_ - 0.3, wh / 2 + k_ + 0.3)]
    for p, fr in frames:
        hwc = ld.line_w / 2 + k_ + 0.8
        a, b = fr.s(0.0, -hw_stick), fr.s(x_win, hw_stick)
        keep.append((min(a[0], b[0]), max(a[0], b[0]), min(a[1], b[1]), max(a[1], b[1])))
        a, b = fr.s(0.0, -hwc), fr.s(x_win + 0.5, hwc)
        keep.append((min(a[0], b[0]), max(a[0], b[0]), min(a[1], b[1]), max(a[1], b[1])))
    if reg == "W":  # vias only in the inner-layer ring: away from the voids
        r = launch.W_RING
        keep += [(0, xw + 0.3, -r - 0.3, r + 0.3)]
        for p in g["ports"]:
            if p["side"] in ("N", "S"):
                keep.append((p["at"] - r - 0.3, p["at"] + r + 0.3, -H / 2, H / 2))
    _stitch(wr, pl, gnd, keep, (s.id, "st"))
    # the label (without the upload prefix): M along the east edge, W in the north-west corner
    text = (s.label or s.id).split(" ", 1)[1] if (s.label or "").startswith("O0-") else s.id
    if reg == "M":
        _text(wr, pl, text, L - 1.4, label_width(text) / 2, ("lab", s.id), vertical=True)
    else:
        _text(wr, pl, text, 2.4, -launch.W_RING - 1.0, ("lab", s.id), vertical=True)
    _ticks(wr, pl, [catalog.LAUNCH_MM], ("rpW", s.id))
    _ticks_y(wr, pl, [-H / 2 + catalog.LAUNCH_MM, H / 2 - catalog.LAUNCH_MM], ("rpNS", s.id))
    _copy_box(wr, pl, xw + ww / 2 - 4.0, -COPY_BOX / 2, ("box", s.id), s.id)


def _window_x(lf: float) -> float:
    """Launch coordinate of the window edge: the reference plane plus the feed."""
    return catalog.LAUNCH_MM + lf


def copper_pad(wr: Writer, pl: Placed, s: catalog.Stick, xw: float, net: str):
    """The reference's fixed copper (R1, R1t) as one custom pad: the λ/4 arm from the window
    edge to the 50 Ω output line that runs through the junction to the N and S window edges."""
    win = s.geometry["window"]
    (ax0, ax1, ay0, ay1), (ox0, ox1, oy0, oy1) = win["copper"]
    pts = [
        (ax0, ay0),
        (ax1, ay0),
        (ox0, oy0),
        (ox1, oy0),
        (ox1, oy1),
        (ox0, oy1),
        (ax1, ay1),
        (ax0, ay1),
    ]
    pts = [(xw + x, y) for x, y in pts]
    cx, cy = xw + (ox0 + ox1) / 2, 0.0
    ang = pl.angle
    local = []
    for x, y in pts:
        dx, dy = x - cx, y - cy
        local.append((dx, dy))
    body = [
        f'\t\t(pad "1" smd custom (at 0 0 {_n(ang)}) (size 0.1 0.1) (layers "F.Cu")'
        f' (net "{net}") (uuid "{_u("ref", s.id)}")'
        " (options (clearance outline) (anchor rect))"
        f" (primitives (gr_poly (pts {' '.join(f'(xy {_n(x)} {_n(y)})' for x, y in local)})"
        " (width 0) (fill yes))))"
    ]
    wr.footprint(f"Reference_{s.id}", pl.p(cx, cy), ang, body, ("refcu", s.id), ref=f"U{s.id}")


# --- the tag stick (A15) -------------------------------------------------------------------


def tag_stick(wr: Writer, pl: Placed, info: Dict[str, str]):
    """A15: the QR (light modules and quiet zone in silkscreen, dark modules bare purple mask
    over laminate: design §6) with the tag text under it; the microsection lines (M, M0.7,
    M1.4, M-MK side by side with their L1 ground at the keep-away, a cut mark across); two
    4-wire meanders on L1 (0.20 x 200 mm, 0.50 x 250 mm) with test-point holes."""
    s = pl.stick
    g = s.geometry
    _, gnd = _nets(wr, s)
    L, H = s.length, s.height
    hh = H / 2
    wr.stick_zones(pl, gnd, layers=wr.layers[1:])
    # 1. the QR
    mods = qr.matrix(g["url"])
    n = len(mods)
    m = 0.40  # module (design §6)
    quiet = 4 * m
    side = n * m + 2 * quiet
    x0, y0 = 1.0, -hh + 1.0
    qkey = (s.id, "qr")
    q1 = (x0 + side, y0 + side)
    frame = [
        (x0, q1[0], y0, y0 + quiet),
        (x0, q1[0], q1[1] - quiet, q1[1]),
        (x0, x0 + quiet, y0 + quiet, q1[1] - quiet),
        (q1[0] - quiet, q1[0], y0 + quiet, q1[1] - quiet),
    ]
    for k, (a_, b_, c_, d_) in enumerate(frame):
        wr.gr_poly("F.SilkS", _rect(pl, a_, b_, c_, d_), qkey + ("f", k))
    for r, row in enumerate(mods):
        c = 0
        while c < n:
            if row[c]:
                c += 1
                continue
            c1 = c
            while c1 < n and not row[c1]:
                c1 += 1
            xa, xb = x0 + quiet + c * m, x0 + quiet + c1 * m
            ya = y0 + quiet + r * m
            wr.gr_poly("F.SilkS", _rect(pl, xa, xb, ya, ya + m), qkey + (r, c))
            c = c1
    wr.keepout(["F.Cu"], _rect(pl, x0 - 0.3, x0 + side + 0.3, y0 - 0.3, y0 + side + 0.3), qkey)
    lines = [
        info.get("title", ""),
        info.get("stackup", ""),
        info.get("git", ""),
        info.get("designs", ""),
    ]
    for k, t in enumerate(x for x in lines if x):
        _text(wr, pl, t, x0, y0 + side + 1.0 + 1.25 * k, (s.id, "tag", k), size=0.8)
    # 2. the microsection lines
    xs0 = x0 + side + 2.0
    xlen = 16.0
    ld = wr.lds["M"]
    body = []
    yy = -hh + 1.2
    rows = []
    for f_id in g["lines"]:
        f = families.get(f_id, wr.st.id)
        hw = f.w / 2 + ld.keepaway
        rows.append((f, yy, yy + 2 * hw))
        yc = yy + hw
        body.append(
            f"\t\t(fp_rect (start {_n(-xlen / 2)} {_n(yc - f.w / 2)}) (end {_n(xlen / 2)} {_n(yc + f.w / 2)})"
            f' (stroke (width 0) (type solid)) (fill yes) (layer "F.Cu") (uuid "{_u("xs", s.id, f_id)}"))'
        )
        wr.keepout(
            ["F.Cu"], _rect(pl, xs0 - 0.6, xs0 + xlen + 0.6, yy, yy + 2 * hw), (s.id, "xk", f_id)
        )
        if not f.mask:
            _mask(
                wr,
                _rect(pl, xs0 - 0.6, xs0 + xlen + 0.6, yy, yy + 2 * hw),
                f"MO_{s.id}_{f_id.replace('.', '').replace('-', '')}",
            )
        yy += 2 * hw + 0.6
    y_x1 = yy
    wr.footprint(
        "Microsection", pl.p(xs0 + xlen / 2, 0.0), pl.angle, body, ("xsec", s.id), ref=f"XS{s.id}"
    )
    wr.zone("F.Cu", gnd, _rect(pl, xs0 - 1.6, xs0 + xlen + 1.6, -hh + 0.381, y_x1), (s.id, "xz"))
    wr.anchor(pl, (xs0 + 2.0, y_x1 - 0.3), 0.4, gnd, (s.id, "gnd"))  # the net's one pad
    for k, x in enumerate((xs0 - 1.1, xs0 + xlen + 1.1)):
        for j, y in enumerate(np.arange(-hh + 1.3, y_x1 - 0.4, 1.0)):
            wr.via(pl.p(x, float(y)), gnd, (s.id, "xv", k, j))
    xc = xs0 + xlen / 2
    for k, (ya, yb) in enumerate(((-hh + 0.45, -hh + 1.0), (y_x1 + 0.1, y_x1 + 0.7))):
        wr.gr_line(pl.p(xc, ya), pl.p(xc, yb), "F.SilkS", (s.id, "cut", k))
    _text(
        wr,
        pl,
        (s.label or s.id).replace(" DC XSEC TAG", ""),
        xs0,
        y_x1 + 2.0,
        ("lab", s.id),
        size=1.0,
    )
    _text(wr, pl, "XSEC M M0.7 M1.4 MK", xs0, y_x1 + 3.6, ("xl", s.id), size=0.8)
    # 3. the meanders, right of the microsection lines
    x_m = xs0 + xlen + 4.8
    for k, mm in enumerate(g["meanders"]):
        x_m = _meander(wr, pl, s, x_m, -hh + 1.6, hh - 4.2, mm, k) + 3.2
    _copy_box(wr, pl, L / 2 - 4.0, -COPY_BOX / 2, ("box", s.id), s.id)


def _meander(wr, pl, s, x0, y_top, y_bot, m, k) -> float:
    """A serpentine of columns from y_top to y_bot (an even number, so both ends are at
    y_top). Force and sense part at each end of the meander itself (4-wire: the measured
    resistance is the meander's alone) and run round the outside to test-point holes below.
    Returns the x of the right-hand pads' outer edge."""
    w = m["w"]
    pitch = 0.5 if w <= 0.25 else 0.9
    run = y_bot - y_top
    cols = int(math.ceil((m["length"] + pitch) / (run + pitch)))
    cols += cols % 2
    net = wr.net(f"DC_{s.id}_W{int(round(w * 1000))}")
    pts = []
    for c in range(cols):
        x = x0 + c * pitch
        a, b = (y_top, y_bot) if c % 2 == 0 else (y_bot, y_top)
        pts += [(x, a), (x, b)]
    for j in range(len(pts) - 1):
        wr.seg(pl.p(*pts[j]), pl.p(*pts[j + 1]), w, "F.Cu", net, (s.id, "m", k, j))
    m["columns"], m["run"], m["pitch"] = cols, round(run, 3), pitch
    m["drawn_length"] = round(cols * run + (cols - 1) * pitch, 3)
    m["corners"] = 2 * (cols - 1)
    yp = s.height / 2 - 1.5
    yj = y_bot + 0.9
    x_last = x0 + (cols - 1) * pitch
    tw = 0.3
    for e, ex in enumerate((x0, x_last)):
        sg = -1 if e == 0 else 1  # outward
        xf, xs = ex + sg * 1.2, ex + sg * 0.6
        force = [(ex, y_top), (ex, y_top - 0.55), (xf, y_top - 0.55), (xf, yj), (ex + sg * 2.2, yp)]
        sense = [(ex, y_top), (xs, y_top), (xs, yj), (ex - sg * 0.2, yp)]
        for t, path in enumerate((force, sense)):
            for j in range(len(path) - 1):
                wr.seg(
                    pl.p(*path[j]), pl.p(*path[j + 1]), tw, "F.Cu", net, (s.id, "kel", k, e, t, j)
                )
            body = [
                f'\t\t(pad "1" thru_hole circle (at 0 0) (size 1.4 1.4) (drill 0.8)'
                f' (layers "*.Cu" "*.Mask") (net "{net}") (uuid "{_u("tp", s.id, k, e, t)}"))'
            ]
            wr.footprint(
                "TestPoint_PTH",
                pl.p(*path[-1]),
                0,
                body,
                ("tp", s.id, k, e, t),
                attrs="through_hole",
                ref=f"TP{s.id}_{k}{e}{t}",
            )
    return x_last + 2.2 + 0.7


# --- panel (frameless, OSH Park) -------------------------------------------------------------


@dataclass
class Item:
    stick: catalog.Stick
    w: float  # panel x extent
    h: float  # panel y extent
    rot: int = 0
    x: float = 0.0  # panel position of the top-left corner
    y: float = 0.0

    @property
    def flexible(self) -> bool:
        """A stick that may take spare row height as wider ground (its structure is centred
        and has no port on a long side)."""
        return self.rot == 0 and self.stick.kind in FLEX

    def placed(self) -> Placed:
        import dataclasses

        if self.rot == 90:
            return Placed(self.stick, self.x + self.w / 2, self.y, rot=90)
        s = (
            self.stick
            if abs(self.stick.height - self.h) < 1e-9
            else dataclasses.replace(self.stick, height=self.h)
        )
        return Placed(s, self.x, self.y + self.h / 2)


@dataclass
class OPanel:
    width: float
    height: float
    items: List[Item]
    tabs: List[Tuple[float, float, float, float]]
    bites: List[Tuple[float, float]]
    mask_rules: List[str] = field(default_factory=list)

    @property
    def placed(self) -> List[Placed]:
        return [it.placed() for it in self.items]

    @property
    def sq_in(self) -> float:
        return self.width * self.height / 645.16


def allowed(s: catalog.Stick) -> Dict[str, List[Tuple[float, float]]]:
    """Where a tab may sit on each edge of a stick (stick coordinates along the edge: x for N
    and S, y for W and E): away from every launch (TAB_CLEAR from a launch end, and the
    connector's ground pads plus 3 mm around a port on its own edge)."""
    L, H = s.length, s.height
    leg = launch.CINCH_142_0701_851.gnd_pad_y[1]
    if s.kind in ("demo", "window"):
        out = {"N": [(0.0, L)], "S": [(0.0, L)], "W": [(-H / 2, H / 2)], "E": [(-H / 2, H / 2)]}
        for p in s.geometry["ports"]:
            side = p["side"]
            at = 0.0 if side == "W" else p["at"]
            band = (at - leg - 3.0, at + leg + 3.0)
            out[side] = _minus(out[side], band)
            # launches near a corner also forbid the start of the adjacent edges
            if side == "W":
                for e in ("N", "S"):
                    out[e] = _minus(out[e], (-1e9, TAB_CLEAR))
            else:
                e = side
                adj = ("W", "E")
                for a in adj:
                    lim = (
                        (-H / 2 - 1, -H / 2 + TAB_CLEAR)
                        if side == "N"
                        else (H / 2 - TAB_CLEAR, H / 2 + 1)
                    )
                    out[a] = _minus(out[a], lim)
        return out
    if s.ports == 0:
        return {"N": [(1.0, L - 1.0)], "S": [(1.0, L - 1.0)], "W": [], "E": []}
    return {"N": [(TAB_CLEAR, L - TAB_CLEAR)], "S": [(TAB_CLEAR, L - TAB_CLEAR)], "W": [], "E": []}


def _minus(ivs, band):
    out = []
    for a, b in ivs:
        if band[1] <= a or band[0] >= b:
            out.append((a, b))
            continue
        if band[0] > a:
            out.append((a, band[0]))
        if band[1] < b:
            out.append((band[1], b))
    return [(a, b) for a, b in out if b - a > 1e-6]


def _panel_edges(it: Item):
    """The item's edges in panel coordinates with their allowed tab intervals:
    [(orientation, fixed coordinate, [(lo, hi)])], "h" edges along x, "v" along y."""
    s = it.stick
    al = allowed(s)
    x0, y0, x1, y1 = it.x, it.y, it.x + it.w, it.y + it.h
    if it.rot == 90:
        # stick x runs along panel +y from y0; stick y runs along panel -x (N = +x side)
        def tr_x(iv):
            return [(y0 + a, y0 + b) for a, b in iv]

        def tr_y(iv):
            xc = (x0 + x1) / 2
            return [(xc - b, xc - a) for a, b in iv]

        return [
            ("v", x1, tr_x(al["N"])),
            ("v", x0, tr_x(al["S"])),
            ("h", y0, tr_y(al["W"])),
            ("h", y1, tr_y(al["E"])),
        ]
    yc = (y0 + y1) / 2
    return [
        ("h", y0, [(x0 + a, x0 + b) for a, b in al["N"]]),
        ("h", y1, [(x0 + a, x0 + b) for a, b in al["S"]]),
        ("v", x0, [(yc + a, yc + b) for a, b in al["W"]]),
        ("v", x1, [(yc + a, yc + b) for a, b in al["E"]]),
    ]


def _rows(items: List[Item], row_max: float):
    """Shelf packing with stacks (layout._rows), with OSH Park's 2.54 mm slots."""
    todo = sorted(items, key=lambda i: (-i.h, -i.w, i.stick.id))
    rows = []
    while todo:
        first = todo.pop(0)
        if first.w > row_max:
            raise ValueError(f"stick {first.stick.id} longer than the row ({row_max} mm)")
        h_row = first.h
        cols: List[List[Item]] = []
        used = -SLOT
        nxt: Optional[Item] = first
        while nxt is not None:
            col = [nxt]
            room = row_max - used - SLOT
            h = nxt.h
            for it in sorted(todo, key=lambda i: (-i.w, i.stick.id)):
                if it.w <= room and h + SLOT + it.h <= h_row:
                    col.append(it)
                    todo.remove(it)
                    h += SLOT + it.h
            col.sort(key=lambda i: (-i.w, i.stick.id))
            cols.append(col)
            used += SLOT + max(i.w for i in col)
            cand = [i for i in todo if used + SLOT + i.w <= row_max]
            nxt = sorted(cand, key=lambda i: (-i.h, -i.w, i.stick.id))[0] if cand else None
            if nxt is not None:
                todo.remove(nxt)
        rows.append((h_row, cols))
    return rows


def _layout(items: List[Item], row_max: float) -> Tuple[float, float]:
    y = ORIGIN[1]
    width = 0.0
    for r, (h, cols) in enumerate(_rows(items, row_max)):
        x = ORIGIN[0]
        for col in cols:
            yy = y
            for it in col:
                it.x, it.y = x, yy
                yy += it.h + SLOT
            spare = y + h - (yy - SLOT)
            last = col[-1]
            if 1e-6 < spare <= FLEX_MAX and last.flexible:
                last.h += spare
            x += max(i.w for i in col) + SLOT
        width = max(width, x - SLOT - ORIGIN[0])
        y += h + SLOT
    return width, y - SLOT - ORIGIN[1]


def _tabs(items: List[Item]):
    """Tabs between facing edges one slot apart, inside both edges' allowed intervals, about
    one per TAB_EVERY mm of shared edge; returns the tabs and the stick graph's edges."""
    tabs, links = [], []
    edges = [(k, e) for k, it in enumerate(items) for e in _panel_edges(it)]
    for i, (ka, (oa, ca, iva)) in enumerate(edges):
        for kb, (ob, cb, ivb) in edges[i + 1 :]:
            if ka == kb or oa != ob or abs(abs(ca - cb) - SLOT) > 1e-6:
                continue
            lo_c, hi_c = min(ca, cb), max(ca, cb)
            # the two items must face each other across this slot
            ia, ib = items[ka], items[kb]
            if oa == "h":
                if not (
                    (ia.y + ia.h <= lo_c + 1e-6 and ib.y >= hi_c - 1e-6)
                    or (ib.y + ib.h <= lo_c + 1e-6 and ia.y >= hi_c - 1e-6)
                ):
                    continue
            else:
                if not (
                    (ia.x + ia.w <= lo_c + 1e-6 and ib.x >= hi_c - 1e-6)
                    or (ib.x + ib.w <= lo_c + 1e-6 and ia.x >= hi_c - 1e-6)
                ):
                    continue
            common = []
            for a0, a1 in iva:
                for b0, b1 in ivb:
                    lo, hi = max(a0, b0), min(a1, b1)
                    if hi - lo >= TAB_W - 1e-6:
                        common.append((lo, hi))
            placed = []
            for lo, hi in common:
                n = max(1, int((hi - lo) // TAB_EVERY))
                for j in range(n):
                    c = lo + (hi - lo) * (j + 0.5) / n
                    c = min(max(c, lo + TAB_W / 2), hi - TAB_W / 2)
                    placed.append(c)
            for c in placed:
                if oa == "h":
                    tabs.append((c - TAB_W / 2, c + TAB_W / 2, lo_c, hi_c))
                else:
                    tabs.append((lo_c, hi_c, c - TAB_W / 2, c + TAB_W / 2))
            if placed:
                links.append((ka, kb))
    return tabs, links


def _connected(n: int, links) -> bool:
    parent = list(range(n))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for a, b in links:
        parent[find(a)] = find(b)
    return len({find(k) for k in range(n)}) == 1


def _facing(it: Item, orient: str, c: float):
    """The allowed intervals of `it`'s edge with orientation `orient` at coordinate c."""
    for o, cc, ivs in _panel_edges(it):
        if o == orient and abs(cc - c) < 1e-6:
            return ivs
    return []


def _overlap(a: Sequence[Tuple[float, float]], b: Sequence[Tuple[float, float]]) -> float:
    best = 0.0
    for a0, a1 in a:
        for b0, b1 in b:
            best = max(best, min(a1, b1) - max(a0, b0))
    return best


def _clear(it: Item, placed: Sequence[Item]) -> bool:
    """At least a slot between `it` and every placed item."""
    for q in placed:
        if (
            it.x < q.x + q.w + SLOT - 1e-6
            and q.x < it.x + it.w + SLOT - 1e-6
            and it.y < q.y + q.h + SLOT - 1e-6
            and q.y < it.y + it.h + SLOT - 1e-6
        ):
            return False
    return True


def _grow(items: List[Item], wmax: float, order: Optional[Sequence[int]] = None):
    """Place the sticks in `order` (default: largest first), each where it hangs on a tab from
    a placed stick (one of its allowed edges facing an allowed edge across a slot, TAB_W or
    more in common), at the position that keeps the bounding rectangle smallest within `wmax`
    wide."""
    if order is None:
        order = sorted(items, key=lambda i: (-(i.w * i.h), -i.w, i.stick.id))
    else:
        order = [items[k] for k in order]
    first = order[0]
    first.x, first.y = ORIGIN
    placed = [first]
    for it in order[1:]:
        best = None
        xs_c = sorted(
            {
                v
                for q in placed
                for v in (q.x, q.x + q.w + SLOT, q.x + q.w - it.w, q.x - SLOT - it.w)
            }
        )
        ys_c = sorted(
            {
                v
                for q in placed
                for v in (q.y, q.y + q.h + SLOT, q.y + q.h - it.h, q.y - SLOT - it.h)
            }
        )
        for q in placed:
            for orient, c, ivq in _panel_edges(q):
                if not ivq:
                    continue
                if orient == "h":
                    y = c - SLOT - it.h if abs(c - q.y) < 1e-6 else c + SLOT
                    face_c = y + it.h if abs(c - q.y) < 1e-6 else y
                    cands = [(x, y) for x in xs_c]
                else:
                    x = c - SLOT - it.w if abs(c - q.x) < 1e-6 else c + SLOT
                    face_c = x + it.w if abs(c - q.x) < 1e-6 else x
                    cands = [(x, y) for y in ys_c]
                for x, y in cands:
                    it.x, it.y = x, y
                    if _overlap(_facing(it, orient, face_c), ivq) < TAB_W - 1e-6:
                        continue
                    if not _clear(it, placed):
                        continue
                    x0 = min(min(p.x for p in placed), x)
                    x1 = max(max(p.x + p.w for p in placed), x + it.w)
                    y0 = min(min(p.y for p in placed), y)
                    y1 = max(max(p.y + p.h for p in placed), y + it.h)
                    if x1 - x0 > wmax + 1e-6:
                        continue
                    key = ((x1 - x0) * (y1 - y0), y1 - y0, y, x)
                    if best is None or key < best[0]:
                        best = (key, x, y)
        if best is None:
            return None
        it.x, it.y = best[1], best[2]
        placed.append(it)
    # move the panel to ORIGIN
    x0 = min(p.x for p in placed)
    y0 = min(p.y for p in placed)
    for p in placed:
        p.x += ORIGIN[0] - x0
        p.y += ORIGIN[1] - y0
    width = max(p.x + p.w for p in placed) - ORIGIN[0]
    height = max(p.y + p.h for p in placed) - ORIGIN[1]
    return width, height


def pack(sticks: Sequence[catalog.Stick], wmax: Optional[float] = None) -> OPanel:
    """The frameless panel: every stick hangs on tabs from the others (one outline), the billed
    rectangle as small as the constructive search finds (widths from the longest stick to
    250 mm in 2 mm steps, or `wmax`)."""

    def items():
        out = []
        for s in sticks:
            rot = int(s.geometry.get("rot", 0))
            w, h = (s.height, s.length) if rot == 90 else (s.length, s.height)
            out.append(Item(s, w, h, rot))
        return out

    widths = (
        [wmax]
        if wmax
        else list(
            np.arange(
                math.ceil(max((s.height if s.geometry.get("rot") else s.length) for s in sticks)),
                251.0,
                4.0,
            )
        )
    )
    n = len(sticks)
    base = items()
    by_area = sorted(
        range(n), key=lambda k: (-(base[k].w * base[k].h), -base[k].w, base[k].stick.id)
    )
    orders = [by_area]
    orders.append(sorted(range(n), key=lambda k: (-base[k].h, -base[k].w, base[k].stick.id)))
    orders.append(sorted(range(n), key=lambda k: (-base[k].w, -base[k].h, base[k].stick.id)))
    rng = np.random.default_rng(SEARCH_SEED)
    big = [k for k in by_area if base[k].h > 13.0 or base[k].rot]
    rest = [k for k in by_area if k not in big]
    for _ in range(SEARCH_ORDERS):
        orders.append(list(rng.permutation(big)) + list(rng.permutation(rest)))
    best = None
    for order in orders:
        for wm in widths:
            its = items()
            res = _grow(its, float(wm), order)
            if res is None:
                continue
            width, height = res
            if best is not None and width * height > best[0][0] + 1e-6:
                continue
            tabs, links = _tabs(its)
            if not _connected(len(its), links):
                continue
            key = (round(width * height, 3), abs(width - height))
            if best is None or key < best[0]:
                best = (key, its, width, height, tabs)
    if best is None:
        raise ValueError("no arrangement lets the sticks hang together on tabs")
    _, its, width, height, tabs = best
    return _finish(its, width, height, tabs)


def _finish(its, width, height, tabs) -> OPanel:
    bites = []
    for x0, x1, y0, y1 in tabs:
        if x1 - x0 < y1 - y0 + 1e-9 and abs((y1 - y0) - SLOT) < 1e-6:  # vertical bridge
            cx = (x0 + x1) / 2
            for k in range(BITES):
                bx = cx + (k - (BITES - 1) / 2) * BITE_PITCH
                bites += [(bx, y0), (bx, y1)]
        else:
            cy = (y0 + y1) / 2
            for k in range(BITES):
                by = cy + (k - (BITES - 1) / 2) * BITE_PITCH
                bites += [(x0, by), (x1, by)]
    return OPanel(width, height, its, tabs, bites)


def outline_loops(panel: OPanel) -> List[List[Tuple[float, float]]]:
    """Boundary loops of the board region (sticks and tabs) on a compressed grid, collinear
    points merged: the outer outline and the internal cut-outs."""
    rects = [(it.x, it.x + it.w, it.y, it.y + it.h) for it in panel.items] + list(panel.tabs)
    xs = np.array(sorted({v for r in rects for v in r[:2]}))
    ys = np.array(sorted({v for r in rects for v in r[2:]}))
    nx, ny = len(xs) - 1, len(ys) - 1
    cx, cy = 0.5 * (xs[:-1] + xs[1:]), 0.5 * (ys[:-1] + ys[1:])
    grid = np.zeros((nx, ny), bool)
    for x0, x1, y0, y1 in rects:
        grid[np.ix_((cx > x0) & (cx < x1), (cy > y0) & (cy < y1))] = True

    def filled(i, j):
        return 0 <= i < nx and 0 <= j < ny and grid[i, j]

    edges = {}
    for i in range(nx):
        for j in range(ny):
            if not grid[i, j]:
                continue
            if not filled(i, j - 1):
                edges[(i, j)] = (i + 1, j)
            if not filled(i + 1, j):
                edges[(i + 1, j)] = (i + 1, j + 1)
            if not filled(i, j + 1):
                edges[(i + 1, j + 1)] = (i, j + 1)
            if not filled(i - 1, j):
                edges[(i, j + 1)] = (i, j)
    loops = []
    while edges:
        start, nxt = next(iter(edges.items()))
        loop = [start]
        del edges[start]
        cur = nxt
        while cur != start:
            loop.append(cur)
            cur = edges.pop(cur)
        pts = [(float(xs[i]), float(ys[j])) for i, j in loop]
        merged = []
        n = len(pts)
        for k in range(n):
            a, b_, c = pts[k - 1], pts[k], pts[(k + 1) % n]
            if (a[0] == b_[0] == c[0]) or (a[1] == b_[1] == c[1]):
                continue
            merged.append(b_)
        loops.append(merged)
    return loops


# --- the board ------------------------------------------------------------------------------


def build(wr: Writer, panel: OPanel, info: Dict[str, str]):
    for pl in panel.placed:
        s = pl.stick
        k = s.kind
        if k in ("thru", "line", "verify"):
            line_stick(wr, pl)
        elif k == "reflect":
            reflect_stick(wr, pl)
        elif k == "variant":
            variant_stick(wr, pl)
        elif k == "switch":
            switch_stick(wr, pl)
        elif k == "cpad":
            cpad_stick(wr, pl)
        elif k == "ring":
            ring_stick(wr, pl)
        elif k == "stub":
            stub_stick(wr, pl)
        elif k in ("demo", "window"):
            demo_stick(wr, pl)
        elif k == "tag":
            tag_stick(wr, pl, info)
        else:
            raise NotImplementedError(f"board O stick kind {k!r}")
    # outline and mouse bites
    for k, loop in enumerate(outline_loops(panel)):
        wr.gr_poly("Edge.Cuts", loop, ("oloop", k), fill=False, width=0.05)
    body = []
    x0, y0 = ORIGIN
    for k, (bx, by) in enumerate(panel.bites):
        body.append(
            f'\t\t(pad "" np_thru_hole circle (at {_n(bx - x0)} {_n(by - y0)}) (size {_n(BITE_D)} {_n(BITE_D)})'
            f' (drill {_n(BITE_D)}) (layers "*.Cu" "*.Mask") (uuid "{_u("obite", k)}"))'
        )
    if body:
        wr.footprint(
            "MouseBites",
            (x0, y0),
            0,
            body,
            ("obites",),
            ref="MB1",
            attrs="board_only exclude_from_pos_files exclude_from_bom",
        )
