"""IWR6843 FCBGA-161 (TI package ABL0161): ball map and a KiCad footprint, from the data sheet.

Every value comes from TI's public data sheet SWRS219F (IWR6843, IWR6443, rev. April 2025,
https://www.ti.com/lit/ds/symlink/iwr6843.pdf): the ball names from section 6.2 (Signal
Descriptions, digital and analog) and Table 6-1 (Pin Attributes, ABL0161), the package from the
mechanical pages (4223365/A: 10.4 x 10.4 mm body, 15 x 15 grid at 0.65 mm, 161 balls, 1.17 mm
maximum height) and the example board layout (0.32 mm non-solder-mask-defined land, mask
opening at most 0.05 mm larger per side, 0.125 mm stencil with 0.32 mm apertures). No TI design
file is used or committed.

Frames. The footprint is the usual top view: ball A1 at the top left, rows A..R (no I and O)
downwards, columns 1..15 to the right; KiCad's y grows down. :func:`ball_xy` returns the
y-up frame yapnr uses (``pnr.graph``). At the board's rotation of 270 degrees the RX balls face
north and the TX balls face east (radar60 plan section 5.3).

Run ``python3 fcbga161.py --out DIR`` to write ``IWR6843_FCBGA-161_ABL0161.kicad_mod``.
"""

from __future__ import annotations

import argparse
import math
import uuid
from pathlib import Path

PITCH_MM = 0.65
ROWS = "ABCDEFGHJKLMNPR"  # JEDEC rows: no I, O
COLS = 15
LAND_MM = 0.32  # NSMD land (SWRS219F example board layout)
MASK_MARGIN_MM = 0.05  # mask opening 0.42 mm: "0.05 MAX" around the land
BODY_MM = 10.4  # nominal (10.3-10.5)
BODY_MAX_MM = 10.5
HEIGHT_MAX_MM = 1.17
COURTYARD_MM = 0.5  # beyond the maximum body
NAME = "IWR6843_FCBGA-161_ABL0161"

# SWRS219F section 6.2.2: ground and supply balls.
_VSS = (
    "L5 L6 L8 L10 K7 K8 K9 K10 K11 J6 J7 J8 J10 H7 H9 H11 G6 G7 G8 G10 F9 F11 E5 E6 E8 E10 E11 R15"
)
_VSSA = (
    "A1 A3 A5 A7 A9 A13 A15 B1 B3 B5 B7 B9 B14 C1 C3 C4 C5 C6 C7 C8 C9 C14 E1 E2 E3 F3 G1 G2 G3 "
    "H3 J1 J2 J3 K3 L1 L2 L3 M3 N1 N2 N3 R1"
)
_POWER = {
    "VDDIN": "H15 N11 P15 R6",
    "VIN_SRAM": "G15",
    "VNWA": "P14",
    "VIOIN": "R10 F15",
    "VIOIN_18": "R9",
    "VIN_18CLK": "B11",
    "VIOIN_18DIFF": "D15",
    "VPP": "L13",
    "VIN_13RF1": "G5 H5 J5",
    "VIN_13RF2": "C2 D2",
    "VIN_18BB": "K5 F5",
    "VIN_18VCO": "B12",
    "VOUT_14APLL": "A10",
    "VOUT_14SYNTH": "B13",
    "VOUT_PA": "A2 B2",
    "VBGAP": "B10",
}
_RF = {"TX1": "B4", "TX2": "B6", "TX3": "B8", "RX1": "M2", "RX2": "K2", "RX3": "H2", "RX4": "F2"}
# Section 6.2.2 (clock, reset, GPADC) and Table 6-1 (digital balls, default signal name).
_SIGNAL = {
    "B15": "CLKP",
    "C15": "CLKM",
    "A14": "OSC_CLKOUT",
    "R3": "NRESET",
    "P1": "GPADC1",
    "P2": "GPADC2",
    "P3": "GPADC3",
    "R2": "GPADC4",
    "C13": "GPADC5",
    "D14": "GPADC6",
    "H13": "GPIO_0",
    "J13": "GPIO_1",
    "K13": "GPIO_2",
    "R4": "GPIO_31",
    "P5": "GPIO_32",
    "R5": "GPIO_33",
    "P6": "GPIO_34",
    "R7": "GPIO_35",
    "P7": "GPIO_36",
    "R8": "GPIO_37",
    "P8": "GPIO_38",
    "N15": "GPIO_47",
    "N14": "DMM_SYNC",
    "N8": "MCU_CLKOUT",
    "N7": "NERROR_IN",
    "N6": "NERROR_OUT",
    "P9": "PMIC_CLKOUT",
    "R13": "QSPI0",
    "N12": "QSPI1",
    "R14": "QSPI2",
    "P12": "QSPI3",
    "R12": "QSPI_CLK",
    "P11": "QSPI_CS_N",
    "N4": "RS232_RX",
    "N5": "RS232_TX",
    "E13": "SPIA_CLK",
    "E15": "SPIA_CS_N",
    "E14": "SPIA_MISO",
    "D13": "SPIA_MOSI",
    "F14": "SPIB_CLK",
    "H14": "SPIB_CS_N",
    "G14": "SPIB_MISO",
    "F13": "SPIB_MOSI",
    "P13": "SPI_HOST_INTR",
    "P4": "SYNC_IN",
    "G13": "SYNC_OUT",
    "P10": "TCK",
    "R11": "TDI",
    "N13": "TDO",
    "N10": "TMS",
    "N9": "WARM_RESET",
    "J14": "LVDS_TXP0",
    "J15": "LVDS_TXM0",
    "K14": "LVDS_TXP1",
    "K15": "LVDS_TXM1",
    "L14": "LVDS_CLKP",
    "L15": "LVDS_CLKM",
    "M14": "LVDS_FRCLKP",
    "M15": "LVDS_FRCLKM",
}


def ball_map():
    """``{ball: (signal, kind)}`` for all 161 balls; kind is gnd, power, rf or signal."""
    out = {}
    for ball in (_VSS + " " + _VSSA).split():
        out[ball] = ("VSS" if ball in _VSS.split() else "VSSA", "gnd")
    for name, balls in _POWER.items():
        for ball in balls.split():
            out[ball] = (name, "power")
    for name, ball in _RF.items():
        out[ball] = (name, "rf")
    for ball, name in _SIGNAL.items():
        out[ball] = (name, "signal")
    if len(out) != 161:
        raise AssertionError("ball map has %d balls, not 161" % len(out))
    return out


def grid_index(ball):
    """``(row index 0..14, column index 0..14)`` of a ball name such as ``M2``."""
    return ROWS.index(ball[0]), int(ball[1:]) - 1


def ring(ball):
    """Depth from the package edge: 0 for the outer row and column, 7 at the centre."""
    r, c = grid_index(ball)
    return min(r, c, 14 - r, 14 - c)


def ball_xy(ball, rot_deg=0.0):
    """The ball centre in the footprint's y-up frame (mm), turned ``rot_deg`` CCW."""
    r, c = grid_index(ball)
    x, y = (c - 7) * PITCH_MM, -(r - 7) * PITCH_MM
    a = math.radians(rot_deg)
    return (
        round(x * math.cos(a) - y * math.sin(a), 6),
        round(x * math.sin(a) + y * math.cos(a), 6),
    )


def sites():
    """Every grid site name (225), populated or not."""
    return [row + str(col) for row in ROWS for col in range(1, COLS + 1)]


def _uuid(*parts):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "yapnr/examples/radar60/" + "/".join(parts)))


def footprint_text():
    """The KiCad 10 footprint (``.kicad_mod``) text, deterministic."""
    balls = ball_map()
    half = BODY_MM / 2
    cy = BODY_MAX_MM / 2 + COURTYARD_MM
    silk = BODY_MAX_MM / 2 + 0.11
    lines = [
        '(footprint "%s"' % NAME,
        "\t(version 20241229)",
        '\t(generator "radar60_fcbga161")',
        '\t(layer "F.Cu")',
        '\t(descr "TI IWR6843 FCBGA-161 (ABL0161), 10.4 x 10.4 mm, 0.65 mm pitch, 15 x 15 grid, '
        "0.32 mm NSMD lands; from SWRS219F (4223365/A) and its example board layout; "
        'generated by examples/radar60/board/fcbga161.py")',
        '\t(tags "BGA 161 0.65 ABL0161 IWR6843 mmWave")',
        "\t(attr smd)",
        '\t(property "Reference" "U1" (at 0 %.3f 0) (layer "F.SilkS") (uuid "%s")'
        % (-cy - 1.0, _uuid("ref"))
        + " (effects (font (size 1 1) (thickness 0.15))))",
        '\t(property "Value" "IWR6843AQGABLR" (at 0 %.3f 0) (layer "F.Fab") (uuid "%s")'
        % (cy + 1.0, _uuid("value"))
        + " (effects (font (size 1 1) (thickness 0.15))))",
    ]

    def line(layer, a, b, width, key):
        lines.append(
            "\t(fp_line (start %.4f %.4f) (end %.4f %.4f) (stroke (width %g) (type solid)) "
            '(layer "%s") (uuid "%s"))' % (a[0], a[1], b[0], b[1], width, layer, _uuid(key))
        )

    # Fab body with a 1 mm chamfer at A1, courtyard, silk corners and an A1 mark.
    ch = 1.0
    fab = [(-half + ch, -half), (half, -half), (half, half), (-half, half), (-half, -half + ch)]
    for i, (a, b) in enumerate(zip(fab, fab[1:] + fab[:1])):
        line("F.Fab", a, b, 0.1, "fab%d" % i)
    crt = [(-cy, -cy), (cy, -cy), (cy, cy), (-cy, cy)]
    for i, (a, b) in enumerate(zip(crt, crt[1:] + crt[:1])):
        line("F.CrtYd", a, b, 0.05, "crt%d" % i)
    for i, (sx, sy) in enumerate(((-1, -1), (1, -1), (1, 1), (-1, 1))):
        corner = (sx * silk, sy * silk)
        line("F.SilkS", corner, (sx * (silk - 1.0), sy * silk), 0.12, "silkh%d" % i)
        line("F.SilkS", corner, (sx * silk, sy * (silk - 1.0)), 0.12, "silkv%d" % i)
    line("F.SilkS", (-silk - 0.4, -silk), (-silk, -silk - 0.4), 0.12, "a1")
    for ball in sites():
        if ball not in balls:
            continue
        r, c = grid_index(ball)
        x, y = (c - 7) * PITCH_MM, (r - 7) * PITCH_MM  # KiCad y down
        lines.append(
            '\t(pad "%s" smd circle (at %.4f %.4f) (size %g %g) (layers "F.Cu" "F.Paste" "F.Mask") '
            '(solder_mask_margin %g) (uuid "%s"))'
            % (ball, x, y, LAND_MM, LAND_MM, MASK_MARGIN_MM, _uuid("pad", ball))
        )
    lines.append(")")
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, required=True, help="a .pretty directory")
    args = ap.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / (NAME + ".kicad_mod")
    path.write_text(footprint_text())
    print(path)


if __name__ == "__main__":
    main()
