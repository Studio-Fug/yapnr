"""Footprint generators for the radar60 parts that KiCad's stock library does not carry.

Every land pattern is built from the manufacturer's published package drawing; the drawing is
named in each generator. No vendor CAD file is read or copied. Units are millimetres; KiCad's
footprint axes (x right, y down, top view).

Conventions follow the KiCad library (KLC) where they apply: pad 1 / ball A1 marked on silk and
fab, a 0.12 mm silk line, a 0.10 mm fab line, a courtyard on a 0.01 mm grid.
"""

from __future__ import annotations

import math
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

Rect = Tuple[float, float, float, float]  # x0, y0, x1, y1
FORMAT_VERSION = "20241229"  # KiCad 9 board/footprint format, which atopile 0.15.8 writes

# The models3d directory, as a KiCad path variable (same convention as KiCad's own
# KICAD10_3DMODEL_DIR, and as this project's YAPNR_KICAD_FOOTPRINTS env var, gen_parts.py
# stock_dir()): export RADAR60_3D=.../examples/radar60/models3d before running kicad-cli or
# pcbnew (KiCad resolves "${VAR}" in a (model ...) path against the OS environment when it is
# not a project text variable), or pass kicad-cli's -D RADAR60_3D=... / --define-var. A plain
# env var, not a project-relative "${KIPRJMOD}/..." text variable: the boards these footprints
# end up on are copied through out-of-tree work directories (examples/radar60/board/integrate.py)
# at depths "${KIPRJMOD}" cannot predict. See ../../models3d/README.md.
MODELS_VAR = "RADAR60_3D"


def _model_ref(filename: str) -> str:
    return f"${{{MODELS_VAR}}}/{filename}"


def _n(v: float) -> str:
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


class Footprint:
    """A minimal KiCad footprint writer (only the constructs these generators need)."""

    def __init__(self, name: str, descr: str, tags: str, attr: str = "smd") -> None:
        self.ref_y = -1.5
        self.name = name
        self.descr = descr
        self.tags = tags
        self.attr = attr
        self.props: List[Tuple[str, str]] = []
        self.items: List[str] = []
        self.model_node: Optional[str] = None

    def model(
        self,
        path: str,
        offset: Tuple[float, float, float] = (0, 0, 0),
        scale: Tuple[float, float, float] = (1, 1, 1),
        rotate: Tuple[float, float, float] = (0, 0, 0),
    ) -> None:
        """Attach a 3D model (one per footprint; KiCad ignores a second ``(model ...)``)."""
        self.model_node = (
            f'\t(model "{path}"\n\t\t(offset\n\t\t\t(xyz {_n(offset[0])} {_n(offset[1])} {_n(offset[2])})\n'
            f"\t\t)\n\t\t(scale\n\t\t\t(xyz {_n(scale[0])} {_n(scale[1])} {_n(scale[2])})\n\t\t)\n"
            f"\t\t(rotate\n\t\t\t(xyz {_n(rotate[0])} {_n(rotate[1])} {_n(rotate[2])})\n\t\t)\n\t)"
        )

    def prop(self, key: str, value: str) -> None:
        self.props.append((key, value))

    def line(
        self, a: Tuple[float, float], b: Tuple[float, float], layer: str, width: float
    ) -> None:
        self.items.append(
            f"\t(fp_line\n\t\t(start {_n(a[0])} {_n(a[1])})\n\t\t(end {_n(b[0])} {_n(b[1])})\n"
            f'\t\t(stroke\n\t\t\t(width {_n(width)})\n\t\t\t(type solid)\n\t\t)\n\t\t(layer "{layer}")\n\t)'
        )

    def poly(
        self, pts: Sequence[Tuple[float, float]], layer: str, width: float, fill: bool = False
    ) -> None:
        xy = " ".join(f"(xy {_n(x)} {_n(y)})" for x, y in pts)
        self.items.append(
            f"\t(fp_poly\n\t\t(pts {xy})\n\t\t(stroke\n\t\t\t(width {_n(width)})\n\t\t\t(type solid)\n\t\t)\n"
            f'\t\t(fill {"yes" if fill else "no"})\n\t\t(layer "{layer}")\n\t)'
        )

    def rect(self, r: Rect, layer: str, width: float) -> None:
        x0, y0, x1, y1 = r
        self.poly([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], layer, width)

    def text(self, s: str, at: Tuple[float, float], layer: str, size: float = 1.0) -> None:
        th = round(size * 0.15, 3)
        self.items.append(
            f'\t(fp_text user "{s}"\n\t\t(at {_n(at[0])} {_n(at[1])} 0)\n\t\t(layer "{layer}")\n'
            f"\t\t(effects\n\t\t\t(font\n\t\t\t\t(size {_n(size)} {_n(size)})\n\t\t\t\t(thickness {_n(th)})\n"
            f"\t\t\t)\n\t\t)\n\t)"
        )

    def pad(
        self,
        name: str,
        shape: str,
        at: Tuple[float, float],
        size: Tuple[float, float],
        layers: str = '"F.Cu" "F.Paste" "F.Mask"',
        extra: str = "",
        kind: str = "smd",
        drill: Optional[float] = None,
    ) -> None:
        d = f"\n\t\t(drill {_n(drill)})" if drill is not None else ""
        self.items.append(
            f'\t(pad "{name}" {kind} {shape}\n\t\t(at {_n(at[0])} {_n(at[1])})\n'
            f"\t\t(size {_n(size[0])} {_n(size[1])}){d}\n\t\t(layers {layers}){extra}\n\t)"
        )

    def custom_pad(
        self,
        name: str,
        anchor: Rect,
        polys: Iterable[Sequence[Tuple[float, float]]],
        layers: str = '"F.Cu" "F.Mask"',
        extra: str = "",
    ) -> None:
        """A custom pad: a rectangular anchor plus filled polygon primitives (pad-relative)."""
        x0, y0, x1, y1 = anchor
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        prims = []
        for pts in polys:
            xy = " ".join(f"(xy {_n(x - cx)} {_n(y - cy)})" for x, y in pts)
            prims.append(
                f"\t\t\t(gr_poly\n\t\t\t\t(pts {xy})\n\t\t\t\t(width 0)\n\t\t\t\t(fill yes)\n\t\t\t)"
            )
        self.items.append(
            f'\t(pad "{name}" smd custom\n\t\t(at {_n(cx)} {_n(cy)})\n'
            f"\t\t(size {_n(x1 - x0)} {_n(y1 - y0)})\n\t\t(layers {layers}){extra}\n"
            "\t\t(options\n\t\t\t(clearance outline)\n\t\t\t(anchor rect)\n\t\t)\n"
            "\t\t(primitives\n" + "\n".join(prims) + "\n\t\t)\n\t)"
        )

    def render(self) -> str:
        out = [
            f'(footprint "{self.name}"',
            f"\t(version {FORMAT_VERSION})",
            '\t(generator "radar60-gen-parts")',
            '\t(layer "F.Cu")',
            f'\t(descr "{self.descr}")',
            f'\t(tags "{self.tags}")',
        ]
        out.append(
            f'\t(property "Reference" "REF**"\n\t\t(at 0 {_n(self.ref_y)} 0)\n\t\t(layer "F.SilkS")\n'
            "\t\t(effects\n\t\t\t(font\n\t\t\t\t(size 1 1)\n\t\t\t\t(thickness 0.15)\n\t\t\t)\n\t\t)\n\t)"
        )
        out.append(
            f'\t(property "Value" "{self.name}"\n\t\t(at 0 0 0)\n\t\t(layer "F.Fab")\n'
            "\t\t(effects\n\t\t\t(font\n\t\t\t\t(size 1 1)\n\t\t\t\t(thickness 0.15)\n\t\t\t)\n\t\t)\n\t)"
        )
        for k, v in self.props:
            out.append(
                f'\t(property "{k}" "{v}"\n\t\t(at 0 0 0)\n\t\t(layer "F.Fab")\n\t\t(hide yes)\n'
                "\t\t(effects\n\t\t\t(font\n\t\t\t\t(size 1 1)\n\t\t\t\t(thickness 0.15)\n\t\t\t)\n\t\t)\n\t)"
            )
        out.append(f"\t(attr {self.attr})")
        out.extend(self.items)
        if self.model_node:
            out.append(self.model_node)
        out.append(")")
        return "\n".join(out) + "\n"


def _courtyard(fp: Footprint, r: Rect) -> None:
    fp.ref_y = r[1] - 0.8  # reference text above the courtyard, clear of the lands
    g = (
        lambda v, up: (math.ceil(v * 100 - 1e-9) if up else math.floor(v * 100 + 1e-9)) / 100
    )  # noqa: E731
    fp.rect((g(r[0], False), g(r[1], False), g(r[2], True), g(r[3], True)), "F.CrtYd", 0.05)


def _fab_body(fp: Footprint, half_w: float, half_h: float, chamfer: float) -> None:
    x0, y0, x1, y1 = -half_w, -half_h, half_w, half_h
    fp.poly([(x0 + chamfer, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0 + chamfer)], "F.Fab", 0.1)
    fp.text("${REFERENCE}", (0, 0), "F.Fab", size=min(1.0, half_w * 0.4))


def _silk_corners(
    fp: Footprint,
    half_w: float,
    half_h: float,
    arm: float,
    pin1: bool = True,
    skip_edges: Tuple[str, ...] = (),
) -> None:
    """Silk corner marks just outside the body (no full outline: the RF feeds leave the package
    edges). ``skip_edges`` ('left', 'top', 'right', 'bottom', footprint frame) get no arms."""
    o = 0.11
    xw, yh = half_w + o, half_h + o
    for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
        cx, cy = sx * xw, sy * yh
        horizontal, vertical = ("top" if sy < 0 else "bottom"), ("left" if sx < 0 else "right")
        # an arm at a corner of a skipped edge starts 0.3 mm short of that edge
        gx = 0.3 if vertical in skip_edges else 0.0
        gy = 0.3 if horizontal in skip_edges else 0.0
        if horizontal not in skip_edges:
            fp.line((cx - sx * gx, cy), (cx - sx * arm, cy), "F.SilkS", 0.12)
        if vertical not in skip_edges:
            fp.line((cx, cy - sy * gy), (cx, cy - sy * arm), "F.SilkS", 0.12)
    if pin1:
        fp.poly(
            [(-xw - 0.1, -yh - 0.1), (-xw - 0.6, -yh - 0.1), (-xw - 0.1, -yh - 0.6)],
            "F.SilkS",
            0.12,
            fill=True,
        )


# --- IWR6843 ABL0161B ------------------------------------------------------------------------

BGA_ROWS = "ABCDEFGHJKLMNPR"  # JEDEC rows: no I, O, Q


def abl0161b(balls: Dict[str, str]) -> Footprint:
    """TI ABL0161B FCBGA-161 (IWR6843), from SWRS219F section 13 drawing 4223365/A (10/2016).

    Body 10.3-10.5 mm square (10.4 nominal), 1.17 mm maximum height, 15 x 15 grid at 0.65 mm
    (9.1 mm ball span), balls 0.35-0.45 mm. Land pattern example: 161 x 0.32 mm round
    non-solder-mask-defined lands (preferred), solder mask opening 0.05 mm maximum all around;
    stencil example 161 x 0.32 mm round apertures for a 0.125 mm stencil.
    """
    fp = Footprint(
        "TI_ABL0161B_FCBGA-161_10.4x10.4mm_Layout15x15_P0.65mm",
        "TI ABL0161B FCBGA-161, 10.4x10.4 mm, 15x15 grid, 0.65 mm pitch, 0.32 mm NSMD lands; "
        "TI SWRS219F drawing 4223365/A (https://www.ti.com/lit/ds/symlink/iwr6843.pdf)",
        "BGA 161 0.65 FCBGA ABL0161 IWR6843",
    )
    fp.prop("Source", "TI SWRS219F section 13, ABL0161B drawing 4223365/A, land pattern example")
    pitch = 0.65
    for ball in balls:
        row, col = ball[0], int(ball[1:])
        x = (col - 8) * pitch
        y = (BGA_ROWS.index(row) - 7) * pitch
        fp.pad(ball, "circle", (x, y), (0.32, 0.32), extra="\n\t\t(solder_mask_margin 0.05)")
    half = 5.2
    _fab_body(fp, half, half, 1.0)
    # No silk on the two RF edges (columns 1-2 on the left, rows A-B on top): the RF macro's mask
    # opening starts 0.05 mm outside them (no mask over RF copper, TI SPRACG5), where silk would
    # be clipped (6 DRC findings at integration). Pin 1 (A1, the RF corner) is marked on F.Fab
    # and by the two remaining corner marks' asymmetry; the courtyard is unchanged.
    _silk_corners(fp, half, half, 1.0, pin1=False, skip_edges=("left", "top"))
    # Courtyard 0.5 mm beyond the body: IPC-7351B least-density BGA excess. The radio's
    # ball-anchored decoupling (internal-LDO outputs, VBGAP, crystal load caps) sits right at the
    # package edge; with the nominal 1.0 mm the B-row caps' pads cannot come within 3.5 mm of
    # their balls (radar60 review 2026-10-03).
    _courtyard(fp, (-half - 0.5, -half - 0.5, half + 0.5, half + 0.5))
    # Parametric CadQuery model (../../models3d/gen_models3d.py:abl0161b_model), built from this
    # same function's ball map: real 161-ball BGA, chamfered pin-1 corner, TI's 1.17 mm max
    # height. Approximate (no TI CAD data); see ../../models3d/README.md.
    fp.model(_model_ref("ti_abl0161b_fcbga161.step"))
    return fp


# --- LP87524J RNF0026C ------------------------------------------------------------------------


def _l_pad(bar: Rect, leg: Rect) -> Tuple[Rect, List[List[Tuple[float, float]]]]:
    """An L-shaped land as the union of two rectangles (anchor = the bar)."""
    polys = []
    for r in (bar, leg):
        x0, y0, x1, y1 = r
        polys.append([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])
    return bar, polys


def _mirror(r: Rect, mx: bool, my: bool) -> Rect:
    x0, y0, x1, y1 = r
    if mx:
        x0, x1 = -x1, -x0
    if my:
        y0, y1 = -y1, -y0
    return (x0, y0, x1, y1)


def rnf0026c() -> Footprint:
    """TI RNF0026C VQFN-HR-26 (LP87524B/J/P-Q1), from SNVSAW2B drawing 4223207/B (04/2018).

    Land pattern example (exposed metal, NSMD preferred, mask 0.05 mm max all around):
    12 side lands 0.6 x 0.25 mm at 0.5 mm pitch on columns 3.8 mm apart (2-7, 15-20);
    10 power lands 0.25 x 1.82 mm at 0.5 mm pitch on rows 3.08 mm apart (9-13, 22-26);
    4 L-shaped corner lands (1, 8, 14, 21): 0.775 mm wide and 0.825 mm tall overall, a 0.4 mm
    foot on the 3.65 mm rows and a 0.35 mm leg; thermal pad 27 of 2.24 x 0.66 mm.
    """
    fp = Footprint(
        "TI_RNF0026C_VQFN-HR-26_4.5x4mm_P0.5mm",
        "TI RNF0026C VQFN-HR-26 4.5x4.0 mm (LP87524-Q1); land pattern from TI SNVSAW2B drawing "
        "4223207/B (https://www.ti.com/lit/ds/symlink/lp87524j-q1.pdf)",
        "VQFN-HR 26 RNF0026C LP87524",
    )
    fp.prop("Source", "TI SNVSAW2B, RNF0026C drawing 4223207/B, example board layout and stencil")
    mask = "\n\t\t(solder_mask_margin 0.05)"
    # side lands: left pins 2..7 top to bottom, right pins 15..20 bottom to top
    ys = [-1.25 + 0.5 * i for i in range(6)]
    for i, y in enumerate(ys):
        fp.pad(
            str(2 + i),
            "roundrect",
            (-1.9, y),
            (0.6, 0.25),
            extra="\n\t\t(roundrect_rratio 0.2)" + mask,
        )
        fp.pad(
            str(20 - i),
            "roundrect",
            (1.9, y),
            (0.6, 0.25),
            extra="\n\t\t(roundrect_rratio 0.2)" + mask,
        )
    # power lands: bottom 9..13 left to right, top 26..22 left to right
    xs = [-1.0 + 0.5 * i for i in range(5)]
    paste_long = "\n\t\t(solder_paste_margin_ratio -0.05)"
    for i, x in enumerate(xs):
        fp.pad(
            str(9 + i),
            "roundrect",
            (x, 1.54),
            (0.25, 1.82),
            extra="\n\t\t(roundrect_rratio 0.2)" + mask + paste_long,
        )
        fp.pad(
            str(26 - i),
            "roundrect",
            (x, -1.54),
            (0.25, 1.82),
            extra="\n\t\t(roundrect_rratio 0.2)" + mask + paste_long,
        )
    # corner L lands (pin 1 top-left; 8 bottom-left; 14 bottom-right; 21 top-right)
    bar1: Rect = (-2.2, -2.025, -1.425, -1.625)
    leg1: Rect = (-1.775, -2.45, -1.425, -1.625)
    for pin, mx, my in (
        ("1", False, False),
        ("8", False, True),
        ("14", True, True),
        ("21", True, False),
    ):
        anchor, polys = _l_pad(_mirror(bar1, mx, my), _mirror(leg1, mx, my))
        fp.custom_pad(pin, anchor, polys, layers='"F.Cu" "F.Paste" "F.Mask"', extra=mask)
    # thermal pad: copper and mask 2.24 x 0.66; paste as two 0.98 x 0.59 apertures (87 %, TI stencil)
    fp.pad("27", "rect", (0, 0), (2.24, 0.66), layers='"F.Cu" "F.Mask"', extra=mask)
    for x in (-0.535, 0.535):
        fp.pad("", "rect", (x, 0), (0.98, 0.59), layers='"F.Paste"')
    _fab_body(fp, 2.0, 2.25, 0.6)
    # silk: two short ticks per side outside the lands plus the pin-1 mark
    _silk_corners(fp, 2.3, 2.55, 0.3)
    _courtyard(fp, (-2.75, -2.95, 2.75, 2.95))
    # Parametric CadQuery model (../../models3d/gen_models3d.py:rnf0026c_model): a lead at every
    # pad of this same function, 0.80 mm max body (TI SNVSAW2B drawing 4223207/B). Approximate
    # (no TI CAD data); see ../../models3d/README.md.
    fp.model(_model_ref("ti_rnf0026c_vqfnhr26.step"))
    return fp


def rpw0010a() -> Footprint:
    """TI RPW0010A VQFN-HR-10 2 x 2 mm (TPS25947xx), from SLVSFC9C drawing 4225183/A (08/2019).

    Land pattern example (NSMD, mask 0.05 mm max): power lands 5 and 6 of 0.3 x 2.4 mm at
    x = -/+0.25 mm; side lands 2, 3, 8, 9 of 0.6 x 0.25 mm on columns 1.8 mm apart at
    y = -/+0.225 mm; L-shaped corner lands 1, 4, 7, 10: a 0.6 x 0.3 mm bar centred 0.7 mm off the
    axis plus a 0.25 mm leg reaching y = -/+1.2 mm (0.65 mm overall).
    """
    fp = Footprint(
        "TI_RPW0010A_VQFN-HR-10_2x2mm_P0.45mm",
        "TI RPW0010A VQFN-HR-10 2x2 mm (TPS25947); land pattern from TI SLVSFC9C drawing "
        "4225183/A (https://www.ti.com/lit/ds/symlink/tps25947.pdf)",
        "VQFN-HR 10 RPW0010A TPS25947",
    )
    fp.prop("Source", "TI SLVSFC9C, RPW0010A drawing 4225183/A, example board layout")
    mask = "\n\t\t(solder_mask_margin 0.05)"
    rr = "\n\t\t(roundrect_rratio 0.15)"
    fp.pad("2", "roundrect", (-0.9, -0.225), (0.6, 0.25), extra=rr + mask)
    fp.pad("3", "roundrect", (-0.9, 0.225), (0.6, 0.25), extra=rr + mask)
    fp.pad("8", "roundrect", (0.9, 0.225), (0.6, 0.25), extra=rr + mask)
    fp.pad("9", "roundrect", (0.9, -0.225), (0.6, 0.25), extra=rr + mask)
    fp.pad("5", "roundrect", (-0.25, 0), (0.3, 2.4), extra=rr + mask)
    fp.pad("6", "roundrect", (0.25, 0), (0.3, 2.4), extra=rr + mask)
    bar1: Rect = (-1.2, -0.85, -0.6, -0.55)
    leg1: Rect = (-0.85, -1.2, -0.6, -0.55)
    for pin, mx, my in (
        ("1", False, False),
        ("4", False, True),
        ("7", True, True),
        ("10", True, False),
    ):
        anchor, polys = _l_pad(_mirror(bar1, mx, my), _mirror(leg1, mx, my))
        fp.custom_pad(pin, anchor, polys, layers='"F.Cu" "F.Paste" "F.Mask"', extra=mask)
    _fab_body(fp, 1.0, 1.0, 0.3)
    _silk_corners(fp, 1.3, 1.3, 0.2)
    _courtyard(fp, (-1.5, -1.5, 1.5, 1.5))
    # Parametric CadQuery model (../../models3d/gen_models3d.py:rpw0010a_model): a lead at every
    # pad of this same function, 0.80 mm max body (TI SLVSFC9C drawing 4225183/A). Approximate
    # (no TI CAD data); see ../../models3d/README.md.
    fp.model(_model_ref("ti_rpw0010a_vqfnhr10.step"))
    return fp


# --- Samtec QTH-030-01-X-D-A ---------------------------------------------------------------

# Samtec, "Recommended PCB layout for QTH-XXX-XX-X-D-XXX" (qth-xxx-xx-x-d-xxx-footprint.pdf,
# revision M, 2024-01-26, ECN-474970), Fig. 1 and Tables 1-3, and the part drawing
# QTH-XXX-XX-X-D-XXX (qth-xxx-xx-x-d-xxx-mkt.pdf, revision BL), both at
# https://suddendocs.samtec.com/prints/ (read 2026-10-03). One bank of 30 positions per row.
QTH_PITCH = 0.500  # .0197
QTH_ROW_Y = 3.086  # .1215, connector centreline to each signal row's land centre
QTH_LAND = (0.305, 1.45)  # .0120 x .057
QTH_GND_LANDS = ((8.445, 2.54), (3.175, 4.70))  # (x, length): .665/2 [.100]; .250/2 [.185]
QTH_GND_LAND_H = 0.64  # .025
QTH_ALIGN_X = 9.24  # Table 1 "B" for -030, -A option: .728 [18.48] between the holes
QTH_ALIGN_Y = 2.03  # .080, towards the pin-1 row
QTH_ALIGN_DRILL = 1.02  # Table 3 "F" for -A: NPTH .040 [1.02] (alignment pins 0.89 mm)
QTH_BODY = (20.00, 5.97)  # part drawing Table 2 "A" for -030; footprint Table 2 "E" for -01


def qth030_01_a() -> Footprint:
    """Samtec QTH-030-01-X-D-A (60 positions, lead style -01, alignment pins -A), from Samtec's
    recommended PCB layout for QTH-XXX-XX-X-D-XXX (revision M, Fig. 1, Tables 1-3).

    Two rows of 30 lands of 0.305 x 1.45 mm at 0.50 mm pitch, 6.172 mm apart (pin 1 and the odd
    pins on one row, TI SPRUIJ4A Table 5 numbering); four ground-plane lands on the centreline
    (2.54 and 4.70 mm long, 0.64 mm wide: MP1-MP4, the ground bus of the QSH mate); two
    1.02 mm NPTH holes for the -A alignment pins, 18.48 mm apart and 2.03 mm off the centreline
    towards the pin-1 row. Samtec's stencil drawing (sheet 2) opens every land 1:1 for a
    0.152 mm stencil, so the paste layer equals the copper. Body 20.00 x 5.97 mm (envelope "A",
    "E"); the body's polarising chamfer is at the pin-2 end.
    """
    fp = Footprint(
        "Samtec_QTH-030-01-L-D-A_2x30_P0.5mm_Vertical",
        "Samtec QTH-030-01-L-D-A 60-pin 0.5 mm high-speed terminal strip, lead style -01, "
        "alignment pins; Samtec recommended PCB layout QTH-XXX-XX-X-D-XXX revision M "
        "(https://suddendocs.samtec.com/prints/qth-xxx-xx-x-d-xxx-footprint.pdf)",
        "Samtec QTH 0.5mm 60 DCA1000",
    )
    fp.prop(
        "Source",
        "Samtec qth-xxx-xx-x-d-xxx-footprint.pdf revision M (2024-01-26) Fig. 1, Tables 1-3; "
        "read 2026-10-03",
    )
    w, h = QTH_LAND
    for i in range(30):
        x = (i - 14.5) * QTH_PITCH
        fp.pad(str(2 * i + 1), "rect", (x, -QTH_ROW_Y), (w, h))
        fp.pad(str(2 * i + 2), "rect", (x, QTH_ROW_Y), (w, h))
    gnd = [(-QTH_GND_LANDS[0][0], QTH_GND_LANDS[0][1]), (-QTH_GND_LANDS[1][0], QTH_GND_LANDS[1][1])]
    gnd += [(QTH_GND_LANDS[1][0], QTH_GND_LANDS[1][1]), (QTH_GND_LANDS[0][0], QTH_GND_LANDS[0][1])]
    for k, (x, length) in enumerate(gnd):
        fp.pad(f"MP{k + 1}", "rect", (x, 0), (length, QTH_GND_LAND_H))
    for x in (-QTH_ALIGN_X, QTH_ALIGN_X):
        fp.pad(
            "",
            "circle",
            (x, -QTH_ALIGN_Y),
            (QTH_ALIGN_DRILL, QTH_ALIGN_DRILL),
            layers='"*.Cu" "*.Mask"',
            kind="np_thru_hole",
            drill=QTH_ALIGN_DRILL,
        )
    bw, bh = QTH_BODY[0] / 2, QTH_BODY[1] / 2
    _fab_body(fp, bw, bh, 1.0)
    # silk: the two short ends (the long edges run under the signal lands) and a pin-1 bar
    # beyond pin 1's land
    for sx in (-1, 1):
        fp.line((sx * (bw + 0.11), -bh), (sx * (bw + 0.11), bh), "F.SilkS", 0.12)
    y1 = -QTH_ROW_Y - h / 2 - 0.25
    fp.line((-7.25 - 0.4, y1), (-7.25 + 0.15, y1), "F.SilkS", 0.12)
    land_y = QTH_ROW_Y + h / 2
    _courtyard(fp, (-bw - 0.25, -land_y - 0.25, bw + 0.25, land_y + 0.25))
    # Parametric CadQuery model (../../models3d/gen_models3d.py:qth030_01_a_model): an
    # approximate housing the footprint's own envelope, one contact-strip box per land row (not
    # 60 separate springs) and the two alignment-pin bosses. No Samtec CAD data; see
    # ../../models3d/README.md.
    fp.model(_model_ref("samtec_qth030_01_l_d_a.step"))
    return fp


# --- Vishay WFCP0612 ------------------------------------------------------------------------


def wfcp0612() -> Footprint:
    """Vishay Dale WFCP0612 (1 to 5 mOhm) wide-terminal metal-foil current-sense resistor, from
    Vishay document 30417 (WFCP, revision 20-Nov-2023), "Recommended solder pad layout".

    Two lands b x c = 1.30 x 3.80 mm with a = 0.60 mm between them (centres 1.90 mm apart);
    body 1.60 x 3.20 mm (L x W, Fig. 2), terminals on the long sides. The 1.0 V shunt R_SH1 uses
    it on development builds (2 mOhm) and on product builds (1 mOhm), so both fits share it.
    """
    fp = Footprint(
        "Vishay_WFCP0612_1632Metric",
        "Vishay WFCP0612 wide-terminal current-sense resistor (1-5 mOhm), 1.6x3.2 mm; "
        "Vishay document 30417 recommended solder pad layout "
        "(https://www.vishay.com/docs/30417/wfcp.pdf)",
        "resistor shunt wide terminal 0612 1632 WFCP",
    )
    fp.prop("Source", "Vishay document 30417 rev. 20-Nov-2023, pad layout a/b/c; read 2026-10-03")
    a, b, c = 0.60, 1.30, 3.80
    x = a / 2 + b / 2
    fp.pad("1", "rect", (-x, 0), (b, c))
    fp.pad("2", "rect", (x, 0), (b, c))
    fp.rect((-0.8, -1.6, 0.8, 1.6), "F.Fab", 0.1)
    fp.text("${REFERENCE}", (0, 0), "F.Fab", size=0.4)
    # silk: short bars beyond the body's long ends, between the lands
    for sy in (-1, 1):
        fp.line((-0.18, sy * (c / 2 + 0.2)), (0.18, sy * (c / 2 + 0.2)), "F.SilkS", 0.12)
    _courtyard(fp, (-x - b / 2 - 0.25, -c / 2 - 0.25, x + b / 2 + 0.25, c / 2 + 0.25))
    # No Vishay WFCP CAD data: KiCad's stock Resistor_SMD.3dshapes/R_0612_1632Metric.step has the
    # same 1.6x3.2 mm body and the same two-pad-on-the-long-sides layout as this footprint (its
    # 1x3.4 mm pads bracket our 1.30x3.80 mm lands); Resistor_SMD.3dshapes/
    # R_Shunt_Vishay_WSKW0612.step was considered and rejected, it is a different, 4-pad Kelvin
    # part with no geometric match here.
    fp.model("${KICAD10_3DMODEL_DIR}/Resistor_SMD.3dshapes/R_0612_1632Metric.step")
    return fp


def rfm1_placeholder() -> Footprint:
    """Placeholder for the yapnr RF macro RFM1 (7 RF ports + ground), replaced by the rf track's export.

    It carries one 0.2 mm port pad per RF chain so the U1 RF balls are not single-pad nets, and
    a ground pad. Board-only: no BOM line, no placement file line.
    """
    fp = Footprint(
        "Radar60_RFM1_Placeholder",
        "Placeholder for the radar60 RF macro RFM1 (yapnr-generated antenna and feed copper); "
        "replaced by the macro export of examples/radar60/rf",
        "radar60 RF macro placeholder",
        attr="smd board_only exclude_from_pos_files exclude_from_bom",
    )
    fp.prop("Unverified", "placeholder: RF copper comes from the pinned RF macro result")
    names = ["rx1", "rx2", "rx3", "rx4", "tx1", "tx2", "tx3"]
    for i, n in enumerate(names):
        fp.pad(n, "rect", (-3.0 + i * 1.0, 0), (0.2, 0.2), layers='"F.Cu"')
    fp.pad("gnd", "rect", (0, 1.0), (0.4, 0.2), layers='"F.Cu"')
    fp.rect((-3.5, -0.5, 3.5, 1.5), "F.Fab", 0.1)
    fp.text("RFM1 placeholder", (0, -1.0), "F.Fab", size=0.5)
    _courtyard(fp, (-3.6, -0.6, 3.6, 1.6))
    return fp
