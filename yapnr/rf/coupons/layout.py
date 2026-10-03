"""KiCad board writer for the coupon panels (design §5, §6, §11.2).

Text generation of a KiCad 10 board (`.kicad_pcb`, nets by name) and its project file
(`.kicad_pro`, the design rules DRC checks against); zones are written unfilled and filled by
`kicad-cli pcb drc --refill-zones --save-board` (`yapnr.rf.coupons.fab`). numpy only.

Panel: rows of sticks of equal height, end to end with 2 mm milled slots between them, a 2 mm
slot above and below every row, 3 mm rails between rows and 5 mm rails around. Every stick hangs
on 5 mm mouse-bite tabs (six 0.5 mm holes at 0.8 mm pitch) on its long sides only, so its
connector ends are milled edges. Copper never crosses a slot or a tab.

Per stick (local x along the stick from its left end, y across from its centre line): per-stick
nets RF_<id> and GND_<id>, the edge SMA footprints with the launch taper as a copper-only pad, a
ground zone on every copper layer, rule areas (no copper pour) that shape the coplanar gaps, the
microstrip and stripline clearances and the cut-outs under the launch, ground via fences, and
silkscreen labels with reference-plane ticks. Rings are arc tracks of the stick's RF net;
footprints of netless copper carry the microsection lines.
"""

from __future__ import annotations

import math
import uuid as _uuid
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

import numpy as np

from yapnr.rf.coupons import catalog, families, launch, stackups

RAIL = 5.0
INNER_RAIL = 3.0
SLOT = 2.0
# JLC's capabilities page (read 2026-10-02): mouse-bite tabs at least 5 mm wide, bites 0.5-0.8 mm
# at 0.2-0.3 mm spacing, NPTH at least 0.5 mm
TAB_W = 5.0
BITE_D, BITE_PITCH, BITES = 0.5, 0.8, 6
EDGE_CLEAR = 0.30  # zone inset from every stick edge
VIA_D, VIA_DRILL = 0.5, 0.3
FENCE_OFF, FENCE_PITCH = 0.5, 0.8  # from the gap edge, along the line (design §6.2)
M_CLEAR = families.M_CLEAR
S_CLEAR = families.S_CLEAR
CONN_EDGE = 2.1  # connector origin to the board edge (Samtec SMA-J-P-H-ST-EM1 geometry)
ORIGIN = (20.0, 20.0)


def _u(*parts) -> str:
    return str(_uuid.uuid5(_uuid.NAMESPACE_URL, "yapnr-coupon/" + "/".join(map(str, parts))))


def _n(x: float) -> str:
    s = f"{x:.4f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


# --- geometry helpers ---------------------------------------------------------------------------


@dataclass
class Placed:
    stick: catalog.Stick
    x0: float  # panel coordinates of the stick's left end (rot 90: x of its centre line)
    yc: float  # panel y of the stick centre line (rot 90: y of its first end)
    rot: int = 0  # 90: the stick's x axis runs along panel +y (board O's A04R)

    def p(self, x: float, y: float) -> Tuple[float, float]:
        if self.rot == 90:
            return (self.x0 - y, self.yc + x)
        return (self.x0 + x, self.yc + y)

    @property
    def angle(self) -> float:
        """KiCad rotation (degrees, counter-clockwise on screen) of the stick's frame."""
        return -90.0 if self.rot == 90 else 0.0


class Frame:
    """Launch coordinates (x from the milled edge into the stick, y across the launch axis) of
    the launch on stick side "W", "E", "N" or "S", its axis at `at` (W, E: y from the stick's
    centre line; N, S: x from the stick's left end), mapped to the panel."""

    def __init__(self, pl: Placed, side: str, at: float = 0.0):
        self.pl, self.side, self.at = pl, side, at

    def s(self, x: float, y: float) -> Tuple[float, float]:
        """Launch to stick coordinates."""
        st, a = self.pl.stick, self.at
        if self.side == "W":
            return (x, a + y)
        if self.side == "E":
            return (st.length - x, a - y)
        if self.side == "N":
            return (a - y, -st.height / 2 + x)
        if self.side == "S":
            return (a + y, st.height / 2 - x)
        raise ValueError(self.side)

    def p(self, x: float, y: float) -> Tuple[float, float]:
        return self.pl.p(*self.s(x, y))

    @property
    def angle(self) -> float:
        base = {"W": 0.0, "E": 180.0, "N": -90.0, "S": 90.0}[self.side]
        a = base + self.pl.angle
        return a - 360.0 if a > 180.0 else a + 360.0 if a <= -180.0 else a


def line_start_extra(ld) -> float:
    """The full-width stub of the line in the launch's taper pad: half a line width plus
    0.05 mm, so a round-ended track starting there stays inside it."""
    return ld.line_w / 2 + 0.05


def line_start(ld) -> float:
    """Where the line's track starts, launch coordinates (inside the taper pad's stub)."""
    return ld.x_te + ld.line_w / 2


@dataclass
class Items:
    tracks: List[str] = field(default_factory=list)
    vias: List[str] = field(default_factory=list)
    zones: List[str] = field(default_factory=list)
    footprints: List[str] = field(default_factory=list)
    graphics: List[str] = field(default_factory=list)
    nets: List[str] = field(default_factory=list)

    def extend(self, o: "Items"):
        for k in ("tracks", "vias", "zones", "footprints", "graphics", "nets"):
            getattr(self, k).extend(getattr(o, k))


def _poly_pts(pts) -> str:
    return " ".join(f"(xy {_n(x)} {_n(y)})" for x, y in pts)


def _circle_pts(cx, cy, r, n=32):
    return [
        (cx + r * math.cos(2 * math.pi * k / n), cy + r * math.sin(2 * math.pi * k / n))
        for k in range(n)
    ]


class Writer:
    def __init__(self, st: stackups.Stackup, b: catalog.Board):
        self.st, self.b = st, b
        self.n_cu = len(st.copper)
        self.layers = [st.kicad_layer(i) for i in range(1, self.n_cu + 1)]
        self.items = Items()
        self.ref = 0
        self.mask_rules: List[str] = []
        # a 2D-designed edge launch (the OSH Park boards), else the Samtec geometry
        self.ld = launch.design(b.launch.design) if b.launch.design else None
        self.lds = {r: launch.design(x.design) for r, x in b.launches.items()}
        self.edge_clear = self.ld.board.keepback if self.ld else EDGE_CLEAR
        self.via_size = (launch.VIA_D, launch.VIA_DRILL) if self.ld else (VIA_D, VIA_DRILL)

    # primitives -----------------------------------------------------------------------------

    def seg(self, a, b, w, layer, net, key):
        if math.hypot(b[0] - a[0], b[1] - a[1]) < 1e-6:
            return
        self.items.tracks.append(
            f"\t(segment (start {_n(a[0])} {_n(a[1])}) (end {_n(b[0])} {_n(b[1])}) (width {_n(w)})"
            f' (layer "{layer}") (net "{net}"))'
        )

    def via(self, p, net, key, remove_unused=False):
        extra = " (remove_unused_layers yes) (keep_end_layers yes)" if remove_unused else ""
        self.items.vias.append(
            f"\t(via (at {_n(p[0])} {_n(p[1])}) (size {_n(self.via_size[0])}) (drill {_n(self.via_size[1])})"
            f' (layers "F.Cu" "B.Cu"){extra} (net "{net}"))'
        )

    def zone(self, layer, net, pts, key, clearance=0.15):
        self.items.zones.append(
            f'\t(zone (net "{net}") (layer "{layer}") (uuid "{_u("zone", key)}") (hatch edge 0.5)'
            f" (connect_pads yes (clearance {_n(clearance)})) (min_thickness 0.15)"
            " (fill (thermal_gap 0.5) (thermal_bridge_width 0.5) (island_removal_mode 0))"
            f" (polygon (pts {_poly_pts(pts)})))"
        )

    def keepout(self, layers, pts, key):
        ls = " ".join(f'"{x}"' for x in layers)
        self.items.zones.append(
            f'\t(zone (layers {ls}) (uuid "{_u("keep", key)}") (hatch edge 0.5)'
            " (connect_pads (clearance 0)) (min_thickness 0.15)"
            " (keepout (tracks allowed) (vias allowed) (pads allowed) (copperpour not_allowed)"
            " (footprints allowed))"
            " (fill (thermal_gap 0.5) (thermal_bridge_width 0.5))"
            f" (polygon (pts {_poly_pts(pts)})))"
        )

    def mask_opening(self, pts, name):
        """An intentional solder-mask opening over a line and its ground (the mask-off sticks
        measure bare copper). It is a footprint (reference `name`) holding the F.Mask polygon,
        so a `bridged_mask` rule in the board's .kicad_dru can be scoped to it alone."""
        cx = sum(p[0] for p in pts) / len(pts)
        cy = sum(p[1] for p in pts) / len(pts)
        body = [
            f"\t\t(fp_poly (pts {_poly_pts([(x - cx, y - cy) for x, y in pts])})"
            f' (stroke (width 0) (type solid)) (fill yes) (layer "F.Mask") (uuid "{_u("mo", name)}"))'
        ]
        self.footprint("MaskOpening", (cx, cy), 0, body, ("mofp", name), ref=name)
        self.mask_rules.append(name)

    def gr_poly(self, layer, pts, key, fill=True, width=0.0):
        self.items.graphics.append(
            f"\t(gr_poly (pts {_poly_pts(pts)}) (stroke (width {_n(width)}) (type solid))"
            f' (fill {"yes" if fill else "no"}) (layer "{layer}") (uuid "{_u("poly", key)}"))'
        )

    def gr_line(self, a, b, layer, key, width=0.15):
        self.items.graphics.append(
            f"\t(gr_line (start {_n(a[0])} {_n(a[1])}) (end {_n(b[0])} {_n(b[1])})"
            f' (stroke (width {_n(width)}) (type solid)) (layer "{layer}") (uuid "{_u("line", key)}"))'
        )

    def text(self, s, p, key, size=1.0, layer="F.SilkS", angle=0.0, justify="left"):
        j = f" (justify {justify})" if justify != "center" else ""
        self.items.graphics.append(
            f'\t(gr_text "{s}" (at {_n(p[0])} {_n(p[1])} {_n(angle)}) (layer "{layer}")'
            f' (uuid "{_u("text", key)}") (effects (font (size {_n(size)} {_n(size)})'
            f" (thickness {_n(0.15 * size)})){j}))"
        )

    def net(self, name):
        if name not in self.items.nets:
            self.items.nets.append(name)
        return name

    def footprint(self, name, at, rot, body: List[str], key, ref=None, attrs="smd"):
        self.ref += 1
        r = ref or f"X{self.ref}"
        self.items.footprints.append(
            "\n".join(
                [
                    f'\t(footprint "{name}" (layer "F.Cu") (uuid "{_u("fp", key)}")'
                    f" (at {_n(at[0])} {_n(at[1])} {_n(rot)})",
                    f'\t\t(property "Reference" "{r}" (at 0 0 {_n(rot)}) (layer "F.Fab")'
                    f' (uuid "{_u("ref", key)}") (effects (font (size 0.8 0.8) (thickness 0.12))))',
                    f'\t\t(property "Value" "{name}" (at 0 0 {_n(rot)}) (layer "F.Fab") (hide yes)'
                    f' (uuid "{_u("val", key)}") (effects (font (size 0.8 0.8) (thickness 0.12))))',
                    f"\t\t(attr {attrs})",
                ]
                + body
                + ["\t)"]
            )
        )
        return r

    # stick building blocks ------------------------------------------------------------------

    def connector(self, pl: Placed, side: str, net_sig: str, net_gnd: str, w_line: float, y=0.0):
        """Edge SMA at the left ("L") or right ("R") end, with the launch taper to w_line."""
        if self.ld is not None:
            return self.edge_launch(pl, side, net_sig, net_gnd, y)
        L = self.b.launch
        s = pl.stick
        if side == "L":
            at, rot = pl.p(CONN_EDGE, y), 180.0
        else:
            at, rot = pl.p(s.length - CONN_EDGE, y), 0.0
        x_pad0 = CONN_EDGE - L.pad_x0  # footprint x of the pad end at the edge (+1.6)
        x_pad1 = x_pad0 - L.pad_len  # inner end (-1.6)
        x_tap = x_pad1 - L.taper_len
        hw0, hw1 = L.pad_w / 2, w_line / 2
        taper = [(x_pad1 + 0.05, -hw0), (x_pad1 + 0.05, hw0), (x_tap, hw1), (x_tap, -hw1)]
        body = [
            f'\t\t(pad "1" smd rect (at {_n((x_pad0 + x_pad1) / 2)} 0 {_n(rot)}) (size {_n(L.pad_len)} {_n(L.pad_w)})'
            f' (layers "F.Cu" "F.Mask") (net "{net_sig}") (uuid "{_u("p1", s.id, side)}"))',
            f'\t\t(pad "1" smd custom (at {_n(x_pad1)} 0 {_n(rot)}) (size 0.1 0.1) (layers "F.Cu")'
            f' (net "{net_sig}") (uuid "{_u("p1t", s.id, side)}")'
            " (options (clearance outline) (anchor rect))"
            f" (primitives (gr_poly (pts {_poly_pts([(x - x_pad1, yy) for x, yy in taper])})"
            " (width 0) (fill yes))))",
        ]
        for yy in (-2.825, 2.825):
            for lay in ('"F.Cu" "F.Mask"', '"B.Cu" "B.Mask"'):
                body.append(
                    f'\t\t(pad "2" smd rect (at -0.25 {_n(yy)} {_n(rot)}) (size 3.7 1.35) (layers {lay})'
                    f' (net "{net_gnd}") (uuid "{_u("p2", s.id, side, yy, lay)}"))'
                )
        # body outline (fab) and an on-board courtyard: the connector is fitted after break-out
        body.append(
            "\t\t(fp_rect (start 2.1 -3.2) (end 11.6 3.2) (stroke (width 0.1) (type solid))"
            f' (fill no) (layer "F.Fab") (uuid "{_u("fab", s.id, side)}"))'
        )
        body.append(
            "\t\t(fp_rect (start -2.35 -3.75) (end 2.1 3.75) (stroke (width 0.05) (type solid))"
            f' (fill no) (layer "F.CrtYd") (uuid "{_u("crt", s.id, side)}"))'
        )
        return self.footprint(
            "SMA_EdgeLaunch_SMA-J-P-H-ST-EM1",
            at,
            rot,
            body,
            ("conn", s.id, side),
            ref=f"J{s.id}{side}",
        )

    def edge_launch(
        self,
        pl: Placed,
        side: str,
        net_sig: str,
        net_gnd: str,
        y=0.0,
        x_max=None,
        ld=None,
        half_w=None,
        ref=None,
    ):
        """The 2D-designed launch (`launch.LaunchDesign`, `ld` or the board's) on one edge of a
        stick: "L"/"W" (left end), "R"/"E" (right end), "N" or "S" (the long edges of board O's
        3-port sticks), its axis at `y` across (W, E) or `y` along (N, S) the stick. It draws
        the Cinch 142-0701-851 footprint (pin pad; the taper, with a full-width stub of the line
        so the line's round end stays inside it, as a copper-only pad; leg pads on F.Cu and
        B.Cu), the In1.Cu cut-out (region M) and the launch's ground vias (`ld.vias` on a stick
        `2 half_w` wide, to `x_max` from the edge). The stick draws the L1 channel
        (`ld.channel`), the line from `line_start(ld)` and the mask opening from
        `ld.x_pe + launch.DAM_W`. Returns the frame (launch to panel coordinates)."""
        ld = ld or self.ld
        conn = ld.conn
        s = pl.stick
        fr = Frame(pl, {"L": "W", "R": "E"}.get(side, side), y)
        x0, x_pe = ld.x0, ld.x_pe
        taper = [(x, yy) for x, yy in ld.signal_outline() if x >= x_pe - 1e-9]
        # the taper's last station is the line width: run it on by half a line width
        hw, xe = ld.line_w / 2, ld.x_te + line_start_extra(ld)
        i = len(taper) // 2
        taper = taper[:i] + [(xe, -hw), (xe, hw)] + taper[i:]
        rot = fr.angle
        body = [
            f'\t\t(pad "1" smd rect (at {_n((x0 + x_pe) / 2)} 0 {_n(rot)}) (size {_n(x_pe - x0)} {_n(ld.pad_w)})'
            f' (layers "F.Cu" "F.Mask") (net "{net_sig}") (uuid "{_u("lp1", s.id, side, y)}"))',
            f'\t\t(pad "1" smd custom (at {_n(x_pe)} 0 {_n(rot)}) (size 0.1 0.1) (layers "F.Cu")'
            f' (net "{net_sig}") (uuid "{_u("lp1t", s.id, side, y)}")'
            " (options (clearance outline) (anchor rect))"
            f" (primitives (gr_poly (pts {_poly_pts([(x - x_pe, yy) for x, yy in taper])})"
            " (width 0) (fill yes))))",
        ]
        for k, (a, b_, c, d) in enumerate(ld.ground_pads()):
            for lay in ('"F.Cu" "F.Mask"', '"B.Cu" "B.Mask"'):
                body.append(
                    f'\t\t(pad "2" smd rect (at {_n((a + b_) / 2)} {_n((c + d) / 2)} {_n(rot)})'
                    f" (size {_n(b_ - a)} {_n(d - c)}) (layers {lay})"
                    f' (net "{net_gnd}") (uuid "{_u("lp2", s.id, side, y, k, lay)}"))'
                )
        # fab drawing: the flange off the board, the legs and the tab on it
        hb = conn.body_w / 2
        l0, l1 = conn.leg_y
        fab = [((-conn.flange_t, -hb), (0.0, hb))]
        fab += [((0.0, sg * l0), (conn.leg_len, sg * l1)) for sg in (-1, 1)]
        fab += [((0.0, -conn.tab_w / 2), (conn.tab_len, conn.tab_w / 2))]
        for k, (p, q) in enumerate(fab):
            body.append(
                f"\t\t(fp_rect (start {_n(p[0])} {_n(p[1])}) (end {_n(q[0])} {_n(q[1])})"
                f' (stroke (width 0.05) (type solid)) (fill no) (layer "F.Fab")'
                f' (uuid "{_u("lfab", s.id, side, y, k)}"))'
            )
        # courtyard on the board (the connector is fitted after break-out)
        cy = conn.lay_d / 2 + 0.05
        body.append(
            f"\t\t(fp_rect (start 0 {_n(-cy)}) (end {_n(conn.lay_e + 0.25)} {_n(cy)})"
            " (stroke (width 0.05) (type solid))"
            f' (fill no) (layer "F.CrtYd") (uuid "{_u("lcrt", s.id, side, y)}"))'
        )
        self.footprint(
            "SMA_EdgeLaunch_Cinch_142-0701-851",
            fr.p(0.0, 0.0),
            rot,
            body,
            ("lconn", s.id, side, y),
            ref=ref or f"J{s.id}{side}",
        )
        cut = ld.cut_profile()
        if cut:
            lays = [self.layers[k - 1] for k in ld.cut_layers]
            pts = [fr.p(x, -c) for x, c in cut] + [fr.p(x, c) for x, c in reversed(cut)]
            ded = [p for i, p in enumerate(pts) if i == 0 or math.dist(p, pts[i - 1]) > 1e-6]
            self.keepout(lays, ded, (s.id, "lcut", side, y))
        if x_max is None and s.ports == 2 and fr.side in ("W", "E"):
            x_max = s.length / 2 - 0.45  # the two launches' fences meet mid-stick
        for k, v in enumerate(ld.vias(half_w or s.height / 2, x_max)):
            self.via(fr.p(v.x, v.y), net_gnd, (s.id, "lvia", side, y, k))
        return fr

    def anchor(self, pl: Placed, p, w, net, key, layer="F.Cu"):
        """Ends an open track so it is not a dangling end: a copper-only pad on F.Cu, a small
        zone of the track's net on an inner layer (an inner-only SMD pad is a questionable
        padstack)."""
        if layer != "F.Cu":
            x, y = pl.p(*p)
            h = w / 2
            pts = [(x - h, y - h), (x + h, y - h), (x + h, y + h), (x - h, y + h)]
            self.zone(layer, net, pts, ("anc",) + key, clearance=0.15)
            return None
        body = [
            f'\t\t(pad "1" smd rect (at 0 0) (size {_n(w)} {_n(w)}) (layers "{layer}")'
            f' (net "{net}") (uuid "{_u("anc", key)}"))'
        ]
        return self.footprint("Anchor", pl.p(*p), 0, body, ("anchor",) + key)

    def stick_zones(self, pl: Placed, net, layers=None, x_inset=None):
        s = pl.stick
        x_inset = self.edge_clear if x_inset is None else x_inset
        hh = s.height / 2 - self.edge_clear
        pts = [
            pl.p(x_inset, -hh),
            pl.p(s.length - x_inset, -hh),
            pl.p(s.length - x_inset, hh),
            pl.p(x_inset, hh),
        ]
        for lay in layers or self.layers:
            self.zone(lay, net, pts, (s.id, lay))

    def channel(self, pl: Placed, prof: List[Tuple[float, float]], key, layers=("F.Cu",), y=0.0):
        """Rule area around a line: prof is [(x, half width)], piecewise linear in x."""
        top = [pl.p(x, y - hw) for x, hw in prof]
        bot = [pl.p(x, y + hw) for x, hw in reversed(prof)]
        self.keepout(list(layers), top + bot, key)

    def fence(
        self, pl: Placed, prof, net, key, y=0.0, pitch=FENCE_PITCH, off=FENCE_OFF, x_lim=None
    ):
        """Ground vias along both sides of a channel profile."""
        s = pl.stick
        xa = 0.9 if x_lim is None else x_lim[0]
        xb = s.length - 0.9 if x_lim is None else x_lim[1]
        xs = np.arange(xa, xb + 1e-9, pitch)
        px = np.array([p[0] for p in prof])
        ph = np.array([p[1] for p in prof])
        for i, x in enumerate(xs):
            if x < px[0] or x > px[-1]:
                continue
            # the channel is wider just around steps: use the max over ±0.3 mm
            hw = max(np.interp(x - 0.3, px, ph), np.interp(x, px, ph), np.interp(x + 0.3, px, ph))
            for sgn in (-1, 1):
                yy = y + sgn * (hw + off)
                if abs(yy) > s.height / 2 - 0.8:
                    continue
                self.via(pl.p(x, yy), net, key + (i, sgn))

    def stitch(
        self, pl: Placed, net, key, keep_out: List[Tuple[float, float, float, float]], pitch=3.0
    ):
        """Plane stitching vias on a grid, away from the given (x0, x1, y0, y1) boxes."""
        s = pl.stick
        hh = s.height / 2 - 1.0
        for i, x in enumerate(np.arange(1.5, s.length - 1.0, pitch)):
            for j, yy in enumerate(np.arange(-hh, hh + 1e-9, pitch)):
                if any(
                    a - 0.6 <= x <= b + 0.6 and c - 0.6 <= yy <= d + 0.6 for a, b, c, d in keep_out
                ):
                    continue
                self.via(pl.p(x, yy), net, key + (i, j))

    def labels(self, pl: Placed, rp=True, x=None, bottom=False):
        """The stick's label (id, family, ΔL: design §5.5) after its first reference plane, and
        the reference-plane ticks (0.5-1.3 mm from the long edge; the label sits below them,
        1.6-2.6 mm from the edge, so the short lines' second tick does not touch it). A label
        too long for the stick past the reference plane (the 20 mm thrus) ends 0.5 mm from the
        far edge instead."""
        s = pl.stick
        hh = s.height / 2
        text = s.label or s.id
        ticks = rp and s.ports == 2
        if x is None:
            x = s.rp[0] + 1.0 if ticks else 2.0
            x = max(min(x, s.length - 0.5 - label_width(text)), 0.5)
        dy = 2.1 if ticks else 1.4
        y = hh - dy if bottom else -hh + dy
        self.text(text, pl.p(x, y), ("lab", s.id), size=1.0)
        if ticks:
            for x in s.rp:
                for sgn in (-1, 1):
                    self.gr_line(
                        pl.p(x, sgn * (hh - 0.5)),
                        pl.p(x, sgn * (hh - 1.3)),
                        "F.SilkS",
                        ("rp", s.id, x, sgn),
                    )


def label_width(text: str, size: float = 1.0) -> float:
    """Upper estimate of a KiCad stroke-font text's length (mm): 0.92 em per character."""
    return 0.92 * size * len(text)


def _profile_launch(b: catalog.Board, x_end: float, w_line: float, gap: float):
    L = b.launch
    x1 = L.pad_x0 + L.pad_len
    x2 = x1 + L.taper_len
    hp = L.pad_w / 2 + L.pad_gap
    return [(0.0, hp), (x1, hp), (x2, w_line / 2 + gap), (x_end, w_line / 2 + gap)]


def _mirror(prof, length):
    return [(length - x, hw) for x, hw in reversed(prof)]


# --- the sticks ---------------------------------------------------------------------------------


def _hw(fam_id: str, stackup_id=None) -> float:
    return families.channel_halfwidth(fam_id, stackup_id)


def build_stick(wr: Writer, pl: Placed):
    s = pl.stick
    b = wr.b
    sig = wr.net(f"RF_{s.id}")
    gnd = wr.net(f"GND_{s.id}")
    kind = s.kind
    if wr.ld is not None:
        return launch_stick(wr, pl)
    if kind in ("dc",):
        return dc_stick(wr, pl)
    if kind == "xsec":
        return xsec_stick(wr, pl, gnd)
    fam_trl = b.trl[s.trl]["family"] if s.trl else "P"
    if kind == "reflect":
        sig = gnd
    if fam_trl == "S":
        return stripline_stick(wr, pl, sig, gnd)
    L = s.length
    wP = families.of(wr.st.id)["P"].w
    gP = families.of(wr.st.id)["P"].gap
    rp1, rp2 = s.rp
    wr.stick_zones(pl, gnd)
    # inner-layer cut-outs under the pad and taper (both ends)
    cut = [wr.layers[k - 1] for k in b.launch.cut_layers]
    hp = b.launch.pad_w / 2 + b.launch.pad_gap
    x_tap = b.launch.pad_x0 + b.launch.pad_len + b.launch.taper_len
    y2 = s.geometry.get("y_port2", 0.0)
    for side, x0, x1, yy in (("L", 0.0, x_tap, 0.0), ("R", L - x_tap, L, y2)):
        wr.keepout(
            cut,
            [pl.p(x0, yy - hp), pl.p(x1, yy - hp), pl.p(x1, yy + hp), pl.p(x0, yy + hp)],
            (s.id, "cut", side),
        )
    wr.connector(pl, "L", sig, gnd, wP)
    wr.connector(pl, "R", sig, gnd, wP, y=y2)
    keep_boxes = [(0, L, -max(hp, 2.2) - 1.0, max(hp, 2.2) + 1.0)]
    if kind in ("thru", "line", "verify"):
        prof = _profile_launch(b, L / 2, wP, gP)
        prof = prof + _mirror(prof, L)[1:]
        wr.channel(pl, prof, (s.id, "ch"))
        wr.fence(pl, prof, gnd, (s.id, "fence"))
        wr.seg(pl.p(x_tap - 0.05, 0), pl.p(L - x_tap + 0.05, 0), wP, "F.Cu", sig, (s.id, "line"))
    elif kind == "reflect":
        prof = _profile_launch(b, rp1, wP, gP)
        wr.channel(pl, prof, (s.id, "chL"))
        wr.channel(pl, _mirror(prof, L), (s.id, "chR"))
        wr.fence(pl, prof, gnd, (s.id, "fL"), x_lim=(0.9, rp1 - 0.6))
        wr.fence(pl, _mirror(prof, L), gnd, (s.id, "fR"), x_lim=(rp2 + 0.6, L - 0.9))
        wr.seg(pl.p(x_tap - 0.05, 0), pl.p(rp1, 0), wP, "F.Cu", sig, (s.id, "l1"))
        wr.seg(pl.p(rp2, 0), pl.p(L - x_tap + 0.05, 0), wP, "F.Cu", sig, (s.id, "l2"))
        for x, k in ((rp1 + 0.3, "a"), (rp2 - 0.3, "b")):
            for yy in (-0.55, 0.0, 0.55):
                wr.via(pl.p(x, yy), gnd, (s.id, "short", k, yy))
    elif kind == "variant":
        fam = s.family
        f = families.of(wr.st.id)[fam]
        prof = _profile_launch(b, rp1, wP, gP)
        hw = families.channel_halfwidth(fam, wr.st.id)
        prof = prof + [(rp1, hw), (rp2, hw)] + _mirror(prof, L)
        wr.channel(pl, prof, (s.id, "ch"))
        if f.gap is not None:
            wr.fence(pl, prof, gnd, (s.id, "fence"))
        else:
            wr.fence(pl, prof, gnd, (s.id, "fence1"), x_lim=(0.9, rp1 - 0.3))
            wr.fence(pl, prof, gnd, (s.id, "fence2"), x_lim=(rp2 + 0.3, L - 0.9))
            wr.fence(
                pl, prof, gnd, (s.id, "fenceM"), pitch=1.6, off=0.6, x_lim=(rp1 + 0.8, rp2 - 0.8)
            )
        wr.seg(pl.p(x_tap - 0.05, 0), pl.p(rp1, 0), wP, "F.Cu", sig, (s.id, "l1"))
        wr.seg(pl.p(rp1, 0), pl.p(rp2, 0), f.w, "F.Cu", sig, (s.id, "l2"))
        wr.seg(pl.p(rp2, 0), pl.p(L - x_tap + 0.05, 0), wP, "F.Cu", sig, (s.id, "l3"))
        if not f.mask:
            ho = hw + 1.5
            wr.mask_opening(
                [pl.p(rp1, -ho), pl.p(rp2, -ho), pl.p(rp2, ho), pl.p(rp1, ho)], f"MO_{s.id}"
            )
        keep_boxes = [(0, L, -hw - 1.5, hw + 1.5), (0, L, -hp - 1.0, hp + 1.0)]
    elif kind == "ring":
        ring_stick(wr, pl, sig, gnd, prof_launch=_profile_launch(b, rp1, wP, gP))
        keep_boxes = [(0, L, -s.height, s.height)]
    elif kind == "stub":
        keep_boxes = stub_stick(wr, pl, sig, gnd, _profile_launch(b, rp1, wP, gP))
    elif kind == "coupled":
        keep_boxes = coupled_stick(wr, pl, gnd, _profile_launch(b, rp1, wP, gP))
    wr.stitch(pl, gnd, (s.id, "st"), keep_boxes)
    wr.labels(pl)


def launch_stick(wr: Writer, pl: Placed):
    """A thru or line stick of a board with a 2D-designed launch (`Writer.ld`, the OSH Park
    Order 0 boards): both launches, the line of the launch's family between them, the L1
    channel at the region's keep-away, the mask opened over the line and its channel between
    the dams, a sparse fence (every 3 mm) along the line past the launch fences, and, on W
    sticks, In1/In2 removed except a perimeter ring. The other Order 0 structures belong to
    board O's catalogue."""
    s = pl.stick
    ld = wr.ld
    if s.kind not in ("thru", "line", "verify"):
        raise NotImplementedError(f"stick kind {s.kind!r} on a designed-launch board")
    sig = wr.net(f"RF_{s.id}")
    gnd = wr.net(f"GND_{s.id}")
    L, half = s.length, s.height / 2
    wr.stick_zones(pl, gnd)
    if ld.region == "W":
        inner = wr.layers[1:-1]
        r = min(launch.W_RING, half - 1.0)
        wr.keepout(inner, [pl.p(0, -r), pl.p(L, -r), pl.p(L, r), pl.p(0, r)], (s.id, "wring"))
    wr.connector(pl, "L", sig, gnd, ld.line_w)
    wr.connector(pl, "R", sig, gnd, ld.line_w)
    prof = ld.channel(x_to=L / 2)
    prof = prof + _mirror(prof, L)[1:]
    wr.channel(pl, prof, (s.id, "ch"))
    xs = line_start(ld)
    wr.seg(pl.p(xs, 0), pl.p(L - xs, 0), ld.line_w, "F.Cu", sig, (s.id, "line"))
    # mask open over the line and its channel, from dam to dam
    xm = ld.x_pe + launch.DAM_W
    inside = [(x, hw) for x, hw in prof if xm < x < L - xm]
    mprof = [(xm, ld.channel_at(xm))] + inside + [(L - xm, ld.channel_at(xm))]
    top = [pl.p(x, -hw) for x, hw in mprof]
    bot = [pl.p(x, hw) for x, hw in reversed(mprof)]
    wr.mask_opening(top + bot, f"MO_{s.id}")
    # the line's own fence: every 3 mm past the launch fences
    xf = ld.x_end + launch.FENCE_LINE + 1.5
    if L - 2 * xf > 0:
        wr.fence(pl, prof, gnd, (s.id, "lf"), pitch=3.0, off=launch.FENCE_OFF, x_lim=(xf, L - xf))
    xl = ld.conn.lay_e + 1.2
    hw_max = max(hw for _, hw in prof)
    keep = [(0, xl, -half, half), (L - xl, L, -half, half), (0, L, -hw_max - 0.8, hw_max + 0.8)]
    wr.stitch(pl, gnd, (s.id, "st"), keep)
    launch_labels(wr, pl, xl)


def launch_labels(wr: Writer, pl: Placed, x_free: float):
    """Label and reference-plane ticks of a designed-launch stick: the text sits 0.9-1.9 mm
    from the long edge (outside the W mask opening and the M leg pads' x range), between
    `x_free` and the next reference plane, shortened to the stick id when the full label does
    not fit; the ticks are 0.5-1.3 mm from both long edges at each reference plane."""
    s = pl.stick
    hh = s.height / 2
    rp1, rp2 = s.rp
    text = s.label or s.id
    x0 = x_free if x_free < rp1 - 0.6 else rp1 + 0.6
    room = (rp1 if x0 < rp1 else rp2) - 0.3 - x0
    if label_width(text) > room:
        text = s.id
    wr.text(text, pl.p(x0, -hh + 1.4), ("lab", s.id), size=1.0)
    for x in s.rp:
        for sgn in (-1, 1):
            wr.gr_line(
                pl.p(x, sgn * (hh - 0.5)),
                pl.p(x, sgn * (hh - 1.3)),
                "F.SilkS",
                ("rp", s.id, x, sgn),
            )


def ring_points(s: catalog.Stick):
    """Centre, radius and the feed points of a directly fed ring (stick coordinates): the
    feeds run along y = 0 and meet the ring a `arc` turn apart, symmetric about x = L/2."""
    g = s.geometry
    r, arc = g["radius"], g["arc"]
    cx = s.length / 2
    yc = -r * math.cos(math.pi * arc)
    half = r * math.sin(math.pi * arc)
    return (cx, yc), r, (cx - half, 0.0), (cx + half, 0.0)


def ring_arcs(wr: "Writer", pl: Placed, s: catalog.Stick, width: float, layer: str, net: str):
    """The ring as arc tracks of its net, split at the feed points (so the feeds end on track
    ends): the short arc between the feeds and the long arc in three parts."""
    (cx, yc), r, _, _ = ring_points(s)
    a0 = 90.0 - 180.0 * s.geometry["arc"]  # angle of the right feed point (degrees, math sense)
    a1 = 180.0 - a0
    spans = [(a0, a1)]
    step = (360.0 - (a1 - a0)) / 3
    spans += [(a1 + k * step, a1 + (k + 1) * step) for k in range(3)]

    def pt(ang):
        t = math.radians(ang)
        return pl.p(cx + r * math.cos(t), yc + r * math.sin(t))

    for k, (u, v) in enumerate(spans):
        st, md, en = pt(u), pt(0.5 * (u + v)), pt(v)
        wr.items.tracks.append(
            f"\t(arc (start {_n(st[0])} {_n(st[1])}) (mid {_n(md[0])} {_n(md[1])})"
            f' (end {_n(en[0])} {_n(en[1])}) (width {_n(width)}) (layer "{layer}")'
            f' (net "{net}"))'
        )


def ring_stick(wr: Writer, pl: Placed, sig, gnd, prof_launch):
    """L1 microstrip ring, fed directly from both ends (catalog._ring)."""
    s = pl.stick
    L = s.length
    rp1, rp2 = s.rp
    fam = families.of(wr.st.id)["M"]
    wP = families.of(wr.st.id)["P"].w
    x_tap = prof_launch[2][0]
    (cx, yc), r, f1, f2 = ring_points(s)
    hw = families.channel_halfwidth("M", wr.st.id)
    prof1 = prof_launch + [(rp1, hw), (f1[0], hw)]
    wr.channel(pl, prof1, (s.id, "ch1"))
    wr.channel(pl, _mirror(prof1, L), (s.id, "ch2"))
    disc = _circle_pts(*pl.p(cx, yc), r + fam.w / 2 + M_CLEAR, 64)
    wr.keepout(["F.Cu"], disc, (s.id, "disc"))
    wr.fence(pl, prof_launch, gnd, (s.id, "fL"), x_lim=(0.9, rp1 - 0.3))
    wr.fence(pl, _mirror(prof_launch, L), gnd, (s.id, "fR"), x_lim=(rp2 + 0.3, L - 0.9))
    for k, (x, y) in enumerate(_circle_pts(cx, yc, r + fam.w / 2 + M_CLEAR + 0.6, 56)):
        if abs(y) < hw + 0.6 and min(abs(x - f1[0]), abs(x - f2[0])) < 12.0:
            continue
        if abs(y) > s.height / 2 - 0.8:
            continue
        wr.via(pl.p(x, y), gnd, (s.id, "ringfence", k))
    wr.seg(pl.p(x_tap - 0.05, 0), pl.p(rp1, 0), wP, "F.Cu", sig, (s.id, "p", "a"))
    wr.seg(pl.p(rp1, 0), pl.p(*f1), fam.w, "F.Cu", sig, (s.id, "m", "a"))
    wr.seg(pl.p(*f2), pl.p(rp2, 0), fam.w, "F.Cu", sig, (s.id, "m", "b"))
    wr.seg(pl.p(rp2, 0), pl.p(L - x_tap + 0.05, 0), wP, "F.Cu", sig, (s.id, "p", "b"))
    ring_arcs(wr, pl, s, fam.w, "F.Cu", sig)


def _retarget_right_connector(wr: Writer, sid: str, old: str, new: str):
    for i, fp in enumerate(wr.items.footprints):
        if f'"J{sid}R"' in fp:
            wr.items.footprints[i] = fp.replace(f'(net "{old}")', f'(net "{new}")')


def stub_stick(wr: Writer, pl: Placed, sig, gnd, prof_launch):
    s = pl.stick
    L = s.length
    wP = families.of(wr.st.id)["P"].w
    gP = families.of(wr.st.id)["P"].gap
    x_tap = prof_launch[2][0]
    end = s.geometry["end"]
    ls = s.geometry["stub_mm"]
    cx = L / 2
    prof = prof_launch[:-1] + [(cx, wP / 2 + gP)]
    prof = prof + _mirror(prof, L)[1:]
    wr.channel(pl, prof, (s.id, "ch"))
    y_end = -(wP / 2 + ls)  # the stub goes toward −y
    hws = wP / 2 + gP
    top = y_end - (gP if end == "open" else 0.0)
    wr.keepout(
        ["F.Cu"],
        [pl.p(cx - hws, 0), pl.p(cx - hws, top), pl.p(cx + hws, top), pl.p(cx + hws, 0)],
        (s.id, "stubch"),
    )
    wr.fence(pl, prof, gnd, (s.id, "f1"), x_lim=(0.9, cx - hws - FENCE_OFF - 0.6))
    wr.fence(pl, prof, gnd, (s.id, "f2"), x_lim=(cx + hws + FENCE_OFF + 0.6, L - 0.9))
    # fence along the stub, both sides
    for i, yy in enumerate(np.arange(-(wP / 2 + gP + FENCE_OFF + 0.6), top + 0.1, -FENCE_PITCH)):
        for sgn in (-1, 1):
            wr.via(pl.p(cx + sgn * (hws + FENCE_OFF), yy), gnd, (s.id, "sf", i, sgn))
    wr.seg(pl.p(x_tap - 0.05, 0), pl.p(L - x_tap + 0.05, 0), wP, "F.Cu", sig, (s.id, "line"))
    wr.seg(pl.p(cx, 0), pl.p(cx, y_end), wP, "F.Cu", sig, (s.id, "stub"))
    if end == "open":
        wr.anchor(pl, (cx, y_end + wP / 2), wP, sig, (s.id, "open"))
    else:
        # net tie at the stub end: pad 1 (signal) -> pad 2 (ground), three vias
        body = [
            f'\t\t(pad "1" smd rect (at 0 0.15) (size {_n(wP)} 0.3) (layers "F.Cu") (net "{sig}")'
            f' (uuid "{_u("nt1", s.id)}"))',
            f'\t\t(pad "2" smd rect (at 0 -0.35) (size 1.4 0.7) (layers "F.Cu") (net "{gnd}")'
            f' (uuid "{_u("nt2", s.id)}"))',
            f"\t\t(fp_poly (pts (xy {_n(-wP / 2)} 0.05) (xy {_n(wP / 2)} 0.05) (xy {_n(wP / 2)} -0.05)"
            f' (xy {_n(-wP / 2)} -0.05)) (stroke (width 0) (type solid)) (fill yes) (layer "F.Cu")'
            f' (uuid "{_u("ntc", s.id)}"))',
        ]
        wr.footprint("NetTie_Short", pl.p(cx, y_end), 0, body, ("nettie", s.id))
        wr.items.footprints[-1] = wr.items.footprints[-1].replace(
            "\t\t(attr smd)", '\t\t(attr smd)\n\t\t(net_tie_pad_groups "1,2")'
        )
        for k, dx in enumerate((-0.55, 0.0, 0.55)):
            wr.via(pl.p(cx + dx, y_end - 0.75), gnd, (s.id, "sv", k))
    return [(0, L, -1.5, 1.5), (cx - 2.0, cx + 2.0, top - 1.0, 0.0), (0, L, -3.0, 3.0)]


def coupled_stick(wr: Writer, pl: Placed, gnd, prof_launch):
    s = pl.stick
    L = s.length
    rp1, rp2 = s.rp
    g = s.geometry
    fam = families.of(wr.st.id)["M"]
    wP = families.of(wr.st.id)["P"].w
    x_tap = prof_launch[2][0]
    a, lc, pg = g["feed"], g["length"], g["pair_gap"]
    dy = -(fam.w + pg)  # line 2 sits above line 1
    n1, n2 = wr.net(f"RF_{s.id}"), wr.net(f"RF_{s.id}_2")
    hw = families.channel_halfwidth("M", wr.st.id)
    # one channel around both M lines between the reference planes
    top = [pl.p(x, -hw0) for x, hw0 in prof_launch] + [pl.p(rp1, dy - hw), pl.p(rp2, dy - hw)]
    right = [pl.p(L - x, dy - hw0) for x, hw0 in reversed(prof_launch)]
    right += [pl.p(L - x, dy + hw0) for x, hw0 in prof_launch]
    bottom = [pl.p(rp2, hw), pl.p(rp1, hw)] + [pl.p(x, hw0) for x, hw0 in reversed(prof_launch)]
    wr.keepout(["F.Cu"], top + right + bottom, (s.id, "ch"))
    wr.fence(pl, prof_launch, gnd, (s.id, "fL"), x_lim=(0.9, rp1 - 0.3))
    wr.fence(pl, _mirror(prof_launch, L), gnd, (s.id, "fR"), y=dy, x_lim=(rp2 + 0.3, L - 0.9))
    x1e = rp1 + a + lc
    x2s = rp1 + a
    wr.seg(pl.p(x_tap - 0.05, 0), pl.p(rp1, 0), wP, "F.Cu", n1, (s.id, "p1"))
    wr.seg(pl.p(rp1, 0), pl.p(x1e, 0), fam.w, "F.Cu", n1, (s.id, "m1"))
    wr.anchor(pl, (x1e - fam.w / 2, 0), fam.w, n1, (s.id, "e1"))
    wr.seg(pl.p(x2s, dy), pl.p(rp2, dy), fam.w, "F.Cu", n2, (s.id, "m2"))
    wr.seg(pl.p(rp2, dy), pl.p(L - x_tap + 0.05, dy), wP, "F.Cu", n2, (s.id, "p2"))
    wr.anchor(pl, (x2s + fam.w / 2, dy), fam.w, n2, (s.id, "e2"))
    _retarget_right_connector(wr, s.id, f"RF_{s.id}", n2)
    return [(0, L, dy - hw - 1.2, hw + 1.2), (0, L, -2.4, 2.4), (0, L, dy - 2.4, dy + 2.4)]


def stripline_stick(wr: Writer, pl: Placed, sig, gnd):
    """Board B: L1 GCPW launch, via to L3 at launch.via_x, stripline between the planes."""
    s = pl.stick
    b = wr.b
    L = s.length
    rp1, rp2 = s.rp
    wP = families.of(wr.st.id)["P"].w
    gP = families.of(wr.st.id)["P"].gap
    lau = b.launch
    xv = lau.via_x
    hp = lau.pad_w / 2 + lau.pad_gap
    x_tap = lau.pad_x0 + lau.pad_len + lau.taper_len
    l1, l2, l3 = wr.layers[0], wr.layers[1], wr.layers[2]
    rest = wr.layers[3:]
    # the ring stick has no L3 pour (the ring and its feeds fill the stick); the planes are L2
    # and L4
    ring = s.kind == "ring"
    wr.stick_zones(pl, gnd, layers=[x for x in wr.layers if not (ring and x == l3)])
    cut = [wr.layers[k - 1] for k in lau.cut_layers if not (ring and wr.layers[k - 1] == l3)]
    for side, x0, x1 in (("L", 0.0, x_tap), ("R", L - x_tap, L)):
        wr.keepout(
            cut, [pl.p(x0, -hp), pl.p(x1, -hp), pl.p(x1, hp), pl.p(x0, hp)], (s.id, "cut", side)
        )
    sig_r = sig
    wr.connector(pl, "L", sig, gnd, wP)
    wr.connector(pl, "R", sig_r, gnd, wP)
    prof = _profile_launch(b, xv, wP, gP) + [(xv + 0.45, 0.45)]
    wr.channel(pl, prof, (s.id, "c1L"))
    wr.channel(pl, _mirror(prof, L), (s.id, "c1R"))
    wr.fence(pl, prof, gnd, (s.id, "fL"), x_lim=(0.9, xv - 1.2))
    wr.fence(pl, _mirror(prof, L), gnd, (s.id, "fR"), x_lim=(L - xv + 1.2, L - 0.9))
    # antipads of the signal vias on the planes the via passes
    for side, xx, net in (("L", xv, sig), ("R", L - xv, sig_r)):
        wr.keepout([l2] + rest, _circle_pts(*pl.p(xx, 0), 0.5, 16), (s.id, "anti", side))
        for k, ang in enumerate((45, 135, 225, 315)):
            a = math.radians(ang)
            wr.via(pl.p(xx + 0.9 * math.cos(a), 0.9 * math.sin(a)), gnd, (s.id, "gv", side, k))
        wr.via(pl.p(xx, 0), net, (s.id, "sv", side), remove_unused=True)
    wr.seg(pl.p(x_tap - 0.05, 0), pl.p(xv, 0), wP, l1, sig, (s.id, "pL"))
    wr.seg(pl.p(L - xv, 0), pl.p(L - x_tap + 0.05, 0), wP, l1, sig_r, (s.id, "pR"))
    fam = families.of(wr.st.id)[s.family] if s.kind == "variant" else families.of(wr.st.id)["S"]
    wS = families.of(wr.st.id)["S"].w
    hwS = families.channel_halfwidth("S", wr.st.id)
    if s.kind == "reflect":
        for side, xa, xb in (("L", xv, rp1), ("R", rp2, L - xv)):
            wr.seg(pl.p(xa, 0), pl.p(xb, 0), wS, l3, sig, (s.id, "s", side))
        wr.keepout(
            [l3],
            [pl.p(xv - 0.6, -hwS), pl.p(rp1, -hwS), pl.p(rp1, hwS), pl.p(xv - 0.6, hwS)],
            (s.id, "c3L"),
        )
        wr.keepout(
            [l3],
            [pl.p(rp2, -hwS), pl.p(L - xv + 0.6, -hwS), pl.p(L - xv + 0.6, hwS), pl.p(rp2, hwS)],
            (s.id, "c3R"),
        )
        for x, k in ((rp1 + 0.3, "a"), (rp2 - 0.3, "b")):
            for yy in (-0.55, 0.0, 0.55):
                wr.via(pl.p(x, yy), gnd, (s.id, "short", k, yy))
    elif s.kind == "ring":
        (cx, yc), r, f1, f2 = ring_points(s)
        wr.seg(pl.p(xv, 0), pl.p(*f1), wS, l3, sig, (s.id, "s1"))
        wr.seg(pl.p(*f2), pl.p(L - xv, 0), wS, l3, sig_r, (s.id, "s2"))
        ring_arcs(wr, pl, s, wS, l3, sig)
        for k, (x, y) in enumerate(_circle_pts(cx, yc, r + wS / 2 + S_CLEAR + 0.6, 56)):
            if abs(y) < hwS + 0.6 and min(abs(x - f1[0]), abs(x - f2[0])) < 12.0:
                continue
            if abs(y) > s.height / 2 - 0.8:
                continue
            wr.via(pl.p(x, y), gnd, (s.id, "rf", k))
    else:
        w_mid = fam.w
        wr.seg(pl.p(xv, 0), pl.p(rp1, 0), wS, l3, sig, (s.id, "s1"))
        wr.seg(pl.p(rp1, 0), pl.p(rp2, 0), w_mid, l3, sig, (s.id, "s2"))
        wr.seg(pl.p(rp2, 0), pl.p(L - xv, 0), wS, l3, sig, (s.id, "s3"))
        wr.keepout(
            [l3],
            [
                pl.p(xv - 0.6, -hwS),
                pl.p(L - xv + 0.6, -hwS),
                pl.p(L - xv + 0.6, hwS),
                pl.p(xv - 0.6, hwS),
            ],
            (s.id, "c3"),
        )
        # plane stitching along the stripline, outside its clearance
        prof = [(xv + 1.0, hwS), (L - xv - 1.0, hwS)]
        wr.fence(
            pl, prof, gnd, (s.id, "sfence"), pitch=1.2, off=0.6, x_lim=(xv + 1.0, L - xv - 1.0)
        )
    wr.stitch(
        pl,
        gnd,
        (s.id, "st"),
        [(0, L, -hwS - 1.4, hwS + 1.4), (0, L, -hp - 1.0, hp + 1.0)]
        + ([(0, L, -s.height, s.height)] if s.kind == "ring" else []),
    )
    wr.labels(pl)


def dc_stick(wr: Writer, pl: Placed):
    s = pl.stick
    g = s.geometry
    layers = sorted({m["layer"] for m in g["meanders"]})
    delta = 4.0
    x_base = 3.0
    pads_y = {0.2: -s.height / 2 + 2.5, 0.5: None}
    y_a0 = -s.height / 2 + 5.5
    mA = [m for m in g["meanders"] if m["w"] == 0.2]
    mB = [m for m in g["meanders"] if m["w"] == 0.5]
    y_b0 = y_a0 + mA[0]["run"] + 2.5
    pads_y[0.5] = y_b0 + mB[0]["run"] + 3.0
    tp = []

    def pth(p, net, key):
        body = [
            f'\t\t(pad "1" thru_hole circle (at 0 0) (size 1.5 1.5) (drill 0.8) (layers "*.Cu" "*.Mask")'
            f' (net "{net}") (uuid "{_u("tp", key)}"))'
        ]
        tp.append(p)
        return wr.footprint("TestPoint_PTH", pl.p(*p), 0, body, ("tp",) + key, attrs="through_hole")

    for grp, y0, up in ((mA, y_a0, True), (mB, y_b0, False)):
        for i, m in enumerate(grp):
            k = layers.index(m["layer"])
            lay = wr.layers[m["layer"] - 1]
            net = wr.net(f"DC_{s.id}_L{m['layer']}_W{int(m['w'] * 1000)}")
            x0 = x_base + k * delta
            cols, pitch, run = m["columns"], m["pitch"], m["run"]
            # serpentine: columns of length `run`; ends at the pad side
            yt, yb = (y0, y0 + run)
            pts = []
            for c in range(cols):
                x = x0 + c * pitch
                a, b_ = (yt, yb) if (c % 2 == 0) == up else (yb, yt)
                pts += [(x, a), (x, b_)]
            for j in range(len(pts) - 1):
                wr.seg(pl.p(*pts[j]), pl.p(*pts[j + 1]), m["w"], lay, net, (s.id, net, j))
            ends = [pts[0], pts[-1]]
            yp = pads_y[m["w"]]
            for e_i, (ex, ey) in enumerate(ends):
                jn = (ex, yp + (1.5 if up else -1.5))
                wr.seg(pl.p(ex, ey), pl.p(*jn), m["w"], lay, net, (s.id, net, "esc", e_i))
                for t_i, dx in enumerate((-1.0, 1.0)):
                    q = (ex + dx, yp)
                    wr.seg(
                        pl.p(*jn), pl.p(*q), min(m["w"], 0.3), lay, net, (s.id, net, "k", e_i, t_i)
                    )
                    pth(q, net, (s.id, net, e_i, t_i))
    # via chain: 50 vias, alternating F.Cu / B.Cu links, Kelvin ends
    n = g["via_chain"]
    net = wr.net(f"DC_{s.id}_CHAIN")
    yc = s.height / 2 - 3.0
    xs = [x_base + 1.0 + 0.8 * i for i in range(n)]
    for i, x in enumerate(xs):
        wr.via(pl.p(x, yc), net, (s.id, "chain", i))
        if i:
            lay = "B.Cu" if i % 2 else "F.Cu"
            wr.seg(pl.p(xs[i - 1], yc), pl.p(x, yc), 0.3, lay, net, (s.id, "cl", i))
    for e_i, x in enumerate((xs[0], xs[-1])):
        jn = (x, yc - 1.0)
        wr.seg(pl.p(x, yc), pl.p(*jn), 0.3, "F.Cu", net, (s.id, "ce", e_i))
        for t_i, dx in enumerate((-1.0, 1.0)):
            q = (x + dx * (1 if e_i else -1) * 0 + dx, yc - 2.2)
            wr.seg(pl.p(*jn), pl.p(*q), 0.3, "F.Cu", net, (s.id, "ck", e_i, t_i))
            pth(q, net, (s.id, "chain", e_i, t_i))
    wr.labels(pl, rp=False, x=2.0, bottom=True)
    return tp


def xsec_stick(wr: Writer, pl: Placed, gnd):
    """Microsection stick: the families side by side along the stick, a via, a cut line."""
    s = pl.stick
    L = s.length
    wr.stick_zones(pl, gnd)
    fams = s.geometry["lines"]
    y = -s.height / 2 + 2.0
    body = []
    for f_id in fams:
        f = families.of(wr.st.id)[f_id]
        hw = families.channel_halfwidth(f_id, wr.st.id)
        y += hw
        lay = "F.Cu" if f.kind == "outer" else wr.layers[2]
        body.append(
            f"\t\t(fp_rect (start {_n(-L / 2 + 2.0)} {_n(y - f.w / 2)}) (end {_n(L / 2 - 2.0)} {_n(y + f.w / 2)})"
            f' (stroke (width 0) (type solid)) (fill yes) (layer "{lay}") (uuid "{_u("xs", s.id, f_id)}"))'
        )
        wr.keepout(
            [lay],
            [pl.p(1.5, y - hw), pl.p(L - 1.5, y - hw), pl.p(L - 1.5, y + hw), pl.p(1.5, y + hw)],
            (s.id, "k", f_id),
        )
        if not f.mask and f.kind == "outer":
            ho = hw + 0.8
            pts = [
                pl.p(1.5, y - ho),
                pl.p(L - 1.5, y - ho),
                pl.p(L - 1.5, y + ho),
                pl.p(1.5, y + ho),
            ]
            wr.mask_opening(pts, f"MO_{s.id}_{f_id.replace('-', '')}")
        y += hw + 0.9
    wr.footprint("Microsection", pl.p(L / 2, 0), 0, body, ("xsec", s.id))
    # the via of the microsection, on the cut line, and ground stitching at both ends
    wr.via(pl.p(L / 2, s.height / 2 - 1.2), gnd, (s.id, "via"))
    for k, x in enumerate((1.0, L - 1.0)):
        for j, yy in enumerate(np.arange(-s.height / 2 + 1.2, s.height / 2 - 1.0, 1.0)):
            wr.via(pl.p(x, yy), gnd, (s.id, "end", k, j))
    for k, yy in enumerate((-s.height / 2 + 0.5, s.height / 2 - 0.5)):
        wr.gr_line(
            pl.p(L / 2, yy),
            pl.p(L / 2, yy + (0.6 if k == 0 else -0.6)),
            "F.SilkS",
            (s.id, "cut", k),
        )
    wr.anchor(pl, (L / 2 - 4.0, s.height / 2 - 1.2), 0.8, gnd, (s.id, "gnd"))
    wr.labels(pl, rp=False, x=L / 2 + 1.0)


# --- panel ------------------------------------------------------------------------------------


@dataclass
class Panel:
    width: float
    height: float
    placed: List[Placed]
    removed: List[Tuple[float, float, float, float]]  # (x0, x1, y0, y1) slot rectangles
    tabs: List[Tuple[float, float, float, float]]
    bites: List[Tuple[float, float]]
    mask_rules: List[str] = field(default_factory=list)


def _rows(sticks: Sequence[catalog.Stick], row_max: float):
    """Shelf packing with stacking: a row starts with the tallest stick left; its columns are
    filled left to right by decreasing length, and a column holds a stack of sticks when their
    heights (and the slots between them) fit the row height. Returns [(height, [[sticks]])]."""
    todo = sorted(sticks, key=lambda s: (-s.height, -s.length, s.id))
    rows = []
    while todo:
        first = todo.pop(0)
        if first.length > row_max:
            raise ValueError(f"stick {first.id} longer than the row ({row_max} mm)")
        h_row = first.height
        cols: List[List[catalog.Stick]] = []
        used = -SLOT  # row length used so far, before the next column's leading slot
        nxt: Optional[catalog.Stick] = first
        while nxt is not None:
            col = [nxt]
            room = row_max - used - SLOT  # longest column that still fits the row
            h = nxt.height
            for s in sorted(todo, key=lambda s: (-s.length, s.id)):
                if s.length <= room and h + SLOT + s.height <= h_row:
                    col.append(s)
                    todo.remove(s)
                    h += SLOT + s.height
            col.sort(key=lambda s: (-s.length, s.id))
            cols.append(col)
            used += SLOT + max(s.length for s in col)
            cand = [s for s in todo if used + SLOT + s.length <= row_max]
            nxt = sorted(cand, key=lambda s: (-s.height, -s.length, s.id))[0] if cand else None
            if nxt is not None:
                todo.remove(nxt)
        rows.append((h_row, cols))
    return rows


def pack(sticks: Sequence[catalog.Stick], row_max: Optional[float] = None) -> Panel:
    """Pack the sticks into a frame. Each column of a row fills the row height (a single stick,
    or the last of a stack, is widened: more ground around the same structure). Every stick
    hangs on tabs with mouse bites on its long sides only, so its connector ends are milled.
    Without `row_max`, the row length that gives the smallest panel area is chosen."""
    import dataclasses

    if row_max is None:
        best = None
        lmin = max(s.length for s in sticks)
        for rm in np.arange(math.ceil(lmin), 221.0, 2.0):
            pnl = pack(sticks, float(rm))
            key = pnl.width * pnl.height
            if best is None or key < best[0] - 1e-6:
                best = (key, pnl)
        return best[1]
    rows = _rows(sticks, row_max)
    x_left = ORIGIN[0] + RAIL
    y = ORIGIN[1] + RAIL
    placed, removed, tabs, bites = [], [], [], []
    row_end_max = 0.0
    for r, (h, cols) in enumerate(rows):
        y_top = y + SLOT
        removed.append((x_left, x_left + SLOT, y_top, y_top + h))
        x = x_left + SLOT
        edges = []  # (stick x0, x1, edge y, slot y0, slot y1)
        for col in cols:
            w = max(s.length for s in col)
            yy = y_top
            for k, s in enumerate(col):
                hs = s.height
                if k == len(col) - 1:
                    hs = y_top + h - yy  # the last of the stack absorbs the spare height
                s2 = dataclasses.replace(s, height=hs)
                placed.append(Placed(s2, x, yy + hs / 2))
                if s.length < w:
                    removed.append((x + s.length, x + w, yy, yy + hs))
                edges.append((x, x + s.length, yy, yy - SLOT, yy))
                edges.append((x, x + s.length, yy + hs, yy + hs, yy + hs + SLOT))
                if k < len(col) - 1:
                    removed.append((x, x + w, yy + hs, yy + hs + SLOT))
                yy += hs + SLOT
            x += w
            removed.append((x, x + SLOT, y_top, y_top + h))
            x += SLOT
        x_end = x
        row_end_max = max(row_end_max, x_end)
        removed.append((x_left, x_end, y, y_top))
        removed.append((x_left, x_end, y_top + h, y_top + h + SLOT))
        seen = set()
        for x0, x1, ye, s0, s1 in edges:
            length = x1 - x0
            xs = [length / 2] if length < 35 else [length / 4, 3 * length / 4]
            for xt in xs:
                cx = round(x0 + xt, 3)
                tab = (cx - TAB_W / 2, cx + TAB_W / 2, s0, s1)
                if tab not in seen:
                    seen.add(tab)
                    tabs.append(tab)
                for k in range(BITES):
                    bites.append((cx + (k - (BITES - 1) / 2) * BITE_PITCH, ye))
        y = y_top + h + SLOT + (INNER_RAIL if r < len(rows) - 1 else 0.0)
    width = (row_end_max - ORIGIN[0]) + RAIL
    height = (y - ORIGIN[1]) + RAIL
    return Panel(width, height, placed, removed, tabs, bites)


def outline_loops(panel: Panel) -> List[List[Tuple[float, float]]]:
    """Boundary loops of (slots minus tabs) on a compressed grid, collinear points merged."""
    xs = sorted({v for r in panel.removed + panel.tabs for v in r[:2]})
    ys = sorted({v for r in panel.removed + panel.tabs for v in r[2:]})
    xs, ys = np.array(xs), np.array(ys)
    nx, ny = len(xs) - 1, len(ys) - 1
    cx, cy = 0.5 * (xs[:-1] + xs[1:]), 0.5 * (ys[:-1] + ys[1:])
    grid = np.zeros((nx, ny), bool)
    for x0, x1, y0, y1 in panel.removed:
        grid[np.ix_((cx > x0) & (cx < x1), (cy > y0) & (cy < y1))] = True
    for x0, x1, y0, y1 in panel.tabs:
        grid[np.ix_((cx > x0) & (cx < x1), (cy > y0) & (cy < y1))] = False

    def filled(i, j):
        return 0 <= i < nx and 0 <= j < ny and grid[i, j]

    # directed boundary edges with the removed region on the left (grid index space)
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


# --- files --------------------------------------------------------------------------------------


def _layers_block(n_cu: int) -> str:
    cu = ['\t\t(0 "F.Cu" signal)'] + [
        f'\t\t({4 + 2 * i} "In{i + 1}.Cu" signal)' for i in range(n_cu - 2)
    ]
    cu += ['\t\t(2 "B.Cu" signal)']
    user = [
        '\t\t(9 "F.Adhes" user "F.Adhesive")',
        '\t\t(11 "B.Adhes" user "B.Adhesive")',
        '\t\t(13 "F.Paste" user)',
        '\t\t(15 "B.Paste" user)',
        '\t\t(5 "F.SilkS" user "F.Silkscreen")',
        '\t\t(7 "B.SilkS" user "B.Silkscreen")',
        '\t\t(1 "F.Mask" user)',
        '\t\t(3 "B.Mask" user)',
        '\t\t(17 "Dwgs.User" user "User.Drawings")',
        '\t\t(19 "Cmts.User" user "User.Comments")',
        '\t\t(25 "Edge.Cuts" user)',
        '\t\t(27 "Margin" user)',
        '\t\t(31 "F.CrtYd" user "F.Courtyard")',
        '\t\t(29 "B.CrtYd" user "B.Courtyard")',
        '\t\t(35 "F.Fab" user)',
        '\t\t(33 "B.Fab" user)',
    ]
    return "\t(layers\n" + "\n".join(cu + user) + "\n\t)"


def _stackup_block(st: stackups.Stackup) -> str:
    ps = stackups.params_of(st)
    mask_t = 0.01524 if st.board == "O" else stackups.MASK_CU_MM  # OSH Park: 0.6 mil
    mask = (
        f'(color "{st.mask_color}") (thickness {_n(mask_t)})'
        f' (material "LPI") (epsilon_r {_n(ps["mask.dk"].nominal)})'
        f' (loss_tangent {_n(ps["mask.df"].nominal)}))'
    )
    out = [
        "\t\t(stackup",
        '\t\t\t(layer "F.SilkS" (type "Top Silk Screen"))',
        '\t\t\t(layer "F.Paste" (type "Top Solder Paste"))',
        f'\t\t\t(layer "F.Mask" (type "Top Solder Mask") {mask}',
    ]
    d = 0
    for x in st.layers:
        if x.kind == "copper":
            out.append(
                f'\t\t\t(layer "{st.kicad_layer(int(x.name[1:]))}" (type "copper") (thickness {_n(x.t_mm)}))'
            )
        else:
            d += 1
            df = ps["pp.df" if x.kind == "prepreg" else "core.df"].nominal
            out.append(
                f'\t\t\t(layer "dielectric {d}" (type "{x.kind}") (thickness {_n(x.t_mm)})'
                f' (material "{x.material}") (epsilon_r {_n(x.er)}) (loss_tangent {_n(df)}))'
            )
    out += [
        f'\t\t\t(layer "B.Mask" (type "Bottom Solder Mask") {mask}',
        '\t\t\t(layer "B.Paste" (type "Bottom Solder Paste"))',
        '\t\t\t(layer "B.SilkS" (type "Bottom Silk Screen"))',
        f'\t\t\t(copper_finish "{st.finish}")',
        "\t\t\t(dielectric_constraints yes)",
        "\t\t)",
    ]
    return "\n".join(out)


def board_text(
    stackup_id: str,
    b: Optional[catalog.Board] = None,
    revision: str = "1",
    info: Optional[dict] = None,
) -> Tuple[str, Panel]:
    st = stackups.get(stackup_id)
    b = b or catalog.board(stackup_id)
    wr = Writer(st, b)
    sticks = [s for s in b.sticks if s.generated]
    if b.panel == "osh":
        return _board_text_osh(st, b, wr, sticks, revision, info or {})
    panel = pack(sticks)
    for pl in panel.placed:
        build_stick(wr, pl)
    # edge cuts: frame and slot loops
    x0, y0 = ORIGIN
    x1, y1 = x0 + panel.width, y0 + panel.height
    frame = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    wr.gr_poly("Edge.Cuts", frame, ("frame",), fill=False, width=0.05)
    for k, loop in enumerate(outline_loops(panel)):
        wr.gr_poly("Edge.Cuts", loop, ("slot", k), fill=False, width=0.05)
    # mouse bites
    body = []
    for k, (bx, by) in enumerate(panel.bites):
        body.append(
            f'\t\t(pad "" np_thru_hole circle (at {_n(bx - x0)} {_n(by - y0)}) (size {_n(BITE_D)} {_n(BITE_D)})'
            f' (drill {_n(BITE_D)}) (layers "*.Cu" "*.Mask") (uuid "{_u("bite", k)}"))'
        )
    wr.footprint(
        "MouseBites",
        (x0, y0),
        0,
        body,
        ("bites",),
        ref="MB1",
        attrs="board_only exclude_from_pos_files exclude_from_bom",
    )
    # frame labels and the fab's order-number location
    wr.text(f"yapnr RF coupon {b.letter} rev {revision}", (x0 + 6, y0 + 3.2), ("title",), size=1.5)
    wr.text(f"{st.id}", (x0 + 70, y0 + 3.2), ("stackup",), size=1.5)
    wr.text("LOT ____  SN ____", (x0 + 110, y0 + 3.2), ("lot",), size=1.2)
    if wr.ld is None:
        wr.text("JLCJLCJLCJLC", (x1 - 40, y1 - 1.8), ("orderno",), size=1.0)
    text = _file_text(st, b, wr, revision)
    panel.mask_rules = list(wr.mask_rules)
    return text, panel


def _file_text(st, b, wr, revision) -> str:
    nets = "\n".join(f'\t(net {i} "{n}")' for i, n in enumerate([""] + wr.items.nets))
    head = "\n".join(
        [
            "(kicad_pcb",
            "\t(version 20241229)",
            '\t(generator "yapnr_rf_coupons")',
            '\t(generator_version "10.0")',
            f"\t(general (thickness {_n(st.thickness_mm)}) (legacy_teardrops no))",
            '\t(paper "A3")',
            f'\t(title_block (title "yapnr RF coupon {b.name if b.upload else b.letter} ({st.id})")'
            f' (rev "{revision}")'
            ' (comment 1 "generated by python -m yapnr.rf.coupons generate; do not edit"))',
            _layers_block(len(st.copper)),
            "\t(setup",
            _stackup_block(st),
            "\t\t(pad_to_mask_clearance 0)",
            "\t\t(allow_soldermask_bridges_in_footprints no)",
            "\t\t(tenting front back)",
            "\t)",
        ]
    )
    body = (
        [nets]
        + wr.items.footprints
        + wr.items.graphics
        + wr.items.tracks
        + wr.items.vias
        + wr.items.zones
    )
    return head + "\n" + "\n".join(body) + "\n)\n"


def _board_text_osh(st, b, wr, sticks, revision, info):
    """Board O: the frameless OSH Park panel (layout_o); its text and the tag on the A15
    stick, nothing on a frame."""
    from yapnr.rf.coupons import layout_o

    panel = layout_o.pack(sticks)
    layout_o.build(wr, panel, info)
    text = _file_text(st, b, wr, revision)
    panel.mask_rules = list(wr.mask_rules)
    return text, panel


def dru_text(mask_rules: Sequence[str], bites: bool = False) -> str:
    """Custom rules: each intentional mask opening may bridge its line and ground (the
    mask-off sticks measure bare copper); with `bites`, the mouse-bite holes (footprint MB1)
    sit on the break-off edge (OSH Park's suggested tab pattern), so the hole-to-edge rule does
    not apply to them. Nothing else is relaxed."""
    out = ["(version 1)"]
    if bites:
        out.append(
            '(rule "MB1: mouse-bite holes on the break-off edge (OSH Park tab pattern)"\n'
            "\t(constraint physical_hole_clearance)\n"
            "\t(condition \"A.memberOfFootprint('MB1') && B.Layer == 'Edge.Cuts'\")\n"
            "\t(severity ignore))"
        )
    for n in mask_rules:
        out.append(
            f'(rule "{n}: intentional mask opening over a test line"\n'
            f"\t(constraint bridged_mask)\n\t(condition \"A.memberOfFootprint('{n}')\")\n"
            "\t(severity ignore))"
        )
    return "\n".join(out) + "\n"


# OSH Park 4-layer (the oshpark-4l fab profile): 5/5 mil, 10 mil drill, 4 mil ring, 10 mil drill
# to inner copper, copper 15 mil from every milled edge
RULES_OSHPARK_4L = {
    "min_clearance": 0.127,
    "min_copper_edge_clearance": 0.381,
    "min_hole_clearance": 0.254,
    "min_hole_to_hole": 0.254,
    "min_through_hole_diameter": 0.254,
    "min_track_width": 0.127,
    "min_via_annular_width": 0.1016,
    "min_via_diameter": 0.4572,
}


def project_json(name: str, rules_override: Optional[dict] = None) -> dict:
    """The .kicad_pro with the design rules DRC checks (JLC multilayer capabilities, conservative;
    see the fab notes), or with `rules_override` (RULES_OSHPARK_4L)."""
    rules = {
        "allow_blind_buried_vias": False,
        "allow_microvias": False,
        "max_error": 0.005,
        "min_clearance": 0.127,
        "min_connection": 0.0,
        "min_copper_edge_clearance": 0.25,
        "min_groove_width": 0.0,
        "min_hole_clearance": 0.2,
        "min_hole_to_hole": 0.25,
        "min_microvia_diameter": 0.2,
        "min_microvia_drill": 0.1,
        "min_resolved_spokes": 2,
        "min_silk_clearance": 0.0,
        "min_text_height": 0.8,
        "min_text_thickness": 0.08,
        "min_through_hole_diameter": 0.3,
        "min_track_width": 0.127,
        "min_via_annular_width": 0.1,
        "min_via_diameter": 0.45,
        "solder_mask_to_copper_clearance": 0.0,
        "use_height_for_length_calcs": True,
    }
    rules.update(rules_override or {})
    return {
        "board": {
            "design_settings": {
                "defaults": {},
                "diff_pair_dimensions": [],
                "drc_exclusions": [],
                "meta": {"version": 2},
                "rules": rules,
                "track_widths": [],
                "via_dimensions": [],
                "zones_allow_external_fillets": False,
            },
        },
        "boards": [],
        "meta": {"filename": f"{name}.kicad_pro", "version": 3},
        "net_settings": {
            "classes": [
                {
                    "bus_width": 12,
                    "clearance": 0.127,
                    "diff_pair_gap": 0.2,
                    "diff_pair_via_gap": 0.25,
                    "diff_pair_width": 0.2,
                    "line_style": 0,
                    "microvia_diameter": 0.3,
                    "microvia_drill": 0.1,
                    "name": "Default",
                    "pcb_color": "rgba(0, 0, 0, 0.000)",
                    "priority": 2147483647,
                    "schematic_color": "rgba(0, 0, 0, 0.000)",
                    "track_width": 0.2,
                    "via_diameter": 0.5,
                    "via_drill": 0.3,
                    "wire_width": 6,
                }
            ],
            "meta": {"version": 4},
            "net_colors": None,
            "netclass_assignments": None,
            "netclass_patterns": [],
        },
        "pcbnew": {"page_layout_descr_file": ""},
        "sheets": [],
        "text_variables": {},
    }
