#!/usr/bin/env python3
"""Parametric STEP models for the radar60 parts KiCad's stock library has no 3D model for.

Built with CadQuery (https://cadquery.readthedocs.io/) straight from each part's datasheet
dimensions (see the docstring of the matching generator in ``../schematic/tools/footprints.py``,
which this script imports so the lead/ball positions always match the footprint's pads exactly).
Every model is a coarse, flat-colour approximation: a body block with a chamfered pin-1 corner,
plus a lead, ball or contact at every pad position. It is not a vendor CAD file -- no TI,
Samtec, SnapEDA or Ultra Librarian data was read -- see ``README.md`` next to this file for the
limits of that approximation and where an exact vendor model can be kept privately instead.

Usage::

    tools/../models3d/gen_models3d.py [--out DIR]

Requires CadQuery (``pip install cadquery``); none of the rest of yapnr needs it, so it is not a
project dependency -- see README.md for a throwaway venv.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, List

HERE = Path(__file__).resolve().parent
TOOLS = HERE.parent / "schematic" / "tools"
sys.path.insert(0, str(TOOLS))
import cadquery as cq  # noqa: E402
import footprints as fp  # noqa: E402

PAD = re.compile(
    r'\(pad "(?P<name>[^"]*)" (?P<kind>\w+) (?P<shape>\w+)\n\t\t\(at (?P<x>[-\d.]+) (?P<y>[-\d.]+)'
    r"(?: (?P<rot>[-\d.]+))?\)\n\t\t\(size (?P<w>[\d.]+) (?P<h>[\d.]+)\)"
)

BODY_COLOR = cq.Color(0.15, 0.15, 0.17)  # matte black mould compound (BGA/QFN)
LEAD_COLOR = cq.Color(0.75, 0.75, 0.75)  # tin/NiPdAu leads and balls
HOUSING_COLOR = cq.Color(0.08, 0.08, 0.1)  # connector housing (black LCP)
CONTACT_COLOR = cq.Color(0.82, 0.72, 0.3)  # gold-flash connector contacts


def pads(text: str) -> List[Dict]:
    """Pad name/kind/position/size straight out of a rendered Footprint (tests/unit/radar60's
    own parser): the 3D model's leads and balls are built from these, so they can't drift from
    the footprint's own pads."""
    out = []
    for m in PAD.finditer(text):
        d = m.groupdict()
        out.append(
            {
                "name": d["name"],
                "kind": d["kind"],
                "shape": d["shape"],
                "x": float(d["x"]),
                "y": float(d["y"]),
                "rot": float(d["rot"]) if d["rot"] else 0.0,
                "w": float(d["w"]),
                "h": float(d["h"]),
            }
        )
    return out


def chamfered_body(
    half_w: float, half_h: float, chamfer: float, z0: float, z1: float
) -> cq.Workplane:
    """A rectangular body, chamfered at its (-x,-y) corner (this codebase's pin-1 corner
    convention throughout footprints.py: see ``_fab_body``/``_silk_corners`` and every generated
    footprint's pin 1, always at that corner), from z0 to z1."""
    x0, y0, x1, y1 = -half_w, -half_h, half_w, half_h
    pts = [(x0 + chamfer, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0 + chamfer)]
    return cq.Workplane("XY").workplane(offset=z0).polyline(pts).close().extrude(z1 - z0)


def lead_block(
    x: float, y: float, w: float, h: float, rot: float, z0: float, z1: float
) -> cq.Workplane:
    wp = cq.Workplane("XY").workplane(offset=z0).center(x, y)
    if rot:
        wp = wp.transformed(rotate=(0, 0, rot))
    return wp.rect(w, h).extrude(z1 - z0)


def assembly(body, leads, body_color=BODY_COLOR, lead_color=LEAD_COLOR) -> cq.Assembly:
    """One coloured solid for the body, one for every lead/ball/contact combined (not a boolean
    union, just one compound): STEP's per-shape product/instance bookkeeping otherwise makes a
    161-ball BGA or a 26-lead QFN many times its geometry's actual size (the ``check-added-
    large-files`` hook caught this at 662 KB for the BGA; one compound per colour keeps every
    model under 250 KB)."""
    asy = cq.Assembly()
    asy.add(body, color=body_color, name="body")
    if leads:
        shapes = [ld.val() if isinstance(ld, cq.Workplane) else ld for ld in leads]
        asy.add(cq.Compound.makeCompound(shapes), color=lead_color, name="leads")
    return asy


# --- U1 IWR6843AQGABLR: TI ABL0161B FCBGA-161 ------------------------------------------------


def abl0161b_model(balls: Dict[str, str]) -> cq.Assembly:
    text = fp.abl0161b(balls).render()
    ball_pads = [p for p in pads(text) if p["shape"] == "circle"]
    half = 5.2  # footprints.py:abl0161b
    ball_h = 0.30  # collapsed solder-ball height (nominal 0.35-0.45 mm ball, reflowed)
    total_h = 1.17  # TI SWRS219F drawing 4223365/A: 1.17 mm max
    body = chamfered_body(half, half, 1.0, ball_h, total_h)
    balls_cq = []
    for p in ball_pads:
        d = p["w"] * 1.1  # the land (0.32 mm) undersizes the ball itself slightly
        balls_cq.append(
            cq.Workplane("XY")
            .workplane(offset=0)
            .center(p["x"], p["y"])
            .sphere(d / 2)
            .translate((0, 0, ball_h / 2))
        )
    return assembly(body, balls_cq)


# --- U2 LP87524JRNFRQ1: TI RNF0026C VQFN-HR-26 -----------------------------------------------


def rnf0026c_model() -> cq.Assembly:
    text = fp.rnf0026c().render()
    all_pads = pads(text)
    half_w, half_h = 2.0, 2.25  # footprints.py:rnf0026c _fab_body
    lead_h = 0.05
    total_h = 0.80  # TI SNVSAW2B drawing 4223207/B: 0.80 mm max body thickness
    body = chamfered_body(half_w, half_h, 0.6, lead_h, total_h)
    leads = []
    for p in all_pads:
        if p["name"] == "27":  # the thermal pad: part of the body underside, not a lead
            continue
        leads.append(lead_block(p["x"], p["y"], p["w"], p["h"], p["rot"], 0, lead_h))
    return assembly(body, leads)


# --- U5 TPS259474ARPWR: TI RPW0010A VQFN-HR-10 -----------------------------------------------


def rpw0010a_model() -> cq.Assembly:
    text = fp.rpw0010a().render()
    all_pads = pads(text)
    half = 1.0  # footprints.py:rpw0010a _fab_body
    lead_h = 0.05
    total_h = 0.80  # TI SLVSFC9C drawing 4225183/A: 0.80 mm max body thickness (RPW family)
    body = chamfered_body(half, half, 0.3, lead_h, total_h)
    leads = [lead_block(p["x"], p["y"], p["w"], p["h"], p["rot"], 0, lead_h) for p in all_pads]
    return assembly(body, leads)


# --- U1's buddy, radar60's header: Samtec QTH-030-01-L-D-A -----------------------------------


def qth030_01_a_model() -> cq.Assembly:
    """Approximate housing: a black LCP body the footprint's full envelope, a gold contact strip
    over each signal row (one box, not 30 separate springs: a coarse stand-in, see README), the
    four ground-plane lands as low ribs, and the two alignment-pin bosses. No Samtec CAD data."""
    bw, bh = fp.QTH_BODY[0] / 2, fp.QTH_BODY[1] / 2
    stand_off = 0.40  # the mated height above the PCB before the contacts (typical -D option)
    body_h = 5.00  # Samtec part drawing QTH-XXX-XX-X-D-XXX envelope "C" (vertical body height)
    housing = chamfered_body(bw, bh, 1.0, stand_off, stand_off + body_h)
    land_w, land_h = fp.QTH_LAND
    contact_h = 0.15
    contacts = []
    for sy in (-1, 1):
        y = sy * fp.QTH_ROW_Y
        contacts.append(
            lead_block(0.0, y, (30 - 1) * fp.QTH_PITCH + land_w, land_h, 0, 0, contact_h)
        )
    for x, length in fp.QTH_GND_LANDS:
        for sx in (-1, 1):
            contacts.append(lead_block(sx * x, 0.0, fp.QTH_GND_LAND_H, length, 0, 0, contact_h))
    pins = []
    for x in (-fp.QTH_ALIGN_X, fp.QTH_ALIGN_X):
        pins.append(
            cq.Workplane("XY")
            .workplane(offset=0)
            .center(x, -fp.QTH_ALIGN_Y)
            .circle(fp.QTH_ALIGN_DRILL * 0.9 / 2)
            .extrude(stand_off)
        )
    asy = cq.Assembly()
    asy.add(housing, color=HOUSING_COLOR, name="housing")
    for i, c in enumerate(contacts):
        asy.add(c, color=CONTACT_COLOR, name=f"contact{i}")
    for i, p in enumerate(pins):
        asy.add(p, color=LEAD_COLOR, name=f"alignpin{i}")
    return asy


MODELS = {
    "ti_abl0161b_fcbga161": lambda: abl0161b_model(
        json.loads((TOOLS / "data" / "abl0161_ballmap.json").read_text())["balls"]
    ),
    "ti_rnf0026c_vqfnhr26": rnf0026c_model,
    "ti_rpw0010a_vqfnhr10": rpw0010a_model,
    "samtec_qth030_01_l_d_a": qth030_01_a_model,
}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default=str(HERE))
    args = ap.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for name, build in MODELS.items():
        asy = build()
        step_path = out / f"{name}.step"
        asy.export(str(step_path), exportType="STEP")
        print(f"{name} -> {step_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
