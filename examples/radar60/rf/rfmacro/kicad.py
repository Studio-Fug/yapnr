"""KiCad 10 output of the RF macro: a self-contained preview board that KiCad's DRC checks.

The board holds the macro exactly as the PnR will import it (board-design.md §7.4 E2 fallback):
tracks and arcs on F.Cu for the feeds and dividers (nets RX1..TX3), one footprint per column
whose custom pads are the patches, GND vias (fences, launch, isolation), the L1 GND pour as F.Cu
zones shaped by rule areas (GCPW gaps, anti-pads, antenna field), the L2 GND plane with the
radiator windows and land cut-outs as rule areas, a solid L3 GND plane, and the F.Mask opening
over the RF copper. Around it: the U1 balls of the two RF edges (rings 0-2, from SWRS219F Fig.
6-1) so the DRC sees the real neighbours. Zones are written unfilled; `kicad-cli pcb drc
--refill-zones --save-board` fills them. KiCad coordinates: x = X0 + x_U1, y = Y0 - y_U1.
"""

from __future__ import annotations

import json
import uuid
from typing import Dict, List, Sequence, Tuple

from .geom import Path, Pt
from .macro import (
    OTHER_EDGE_BALLS,
    RF_BALLS,
    ROWS,
    VSSA,
    Macro,
    _place_paths,
    ball_xy,
    channel_outlines,
)
from .params import PKG, RULES, STACK

X0, Y0 = 100.0, 100.0
NS = uuid.UUID("6f1c3c52-6f62-4d0e-9d3a-7a6d2a2b0c60")


def _u(*key) -> str:
    return str(uuid.uuid5(NS, "/".join(str(k) for k in key)))


def _n(v: float) -> str:
    s = f"{v:.5f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _k(p: Pt) -> Tuple[float, float]:
    return (X0 + p[0], Y0 - p[1])


def _pts(poly: Sequence[Pt], local: Pt | None = None) -> str:
    if local is None:
        return " ".join(f"(xy {_n(_k(q)[0])} {_n(_k(q)[1])})" for q in poly)
    return " ".join(f"(xy {_n(q[0] - local[0])} {_n(-(q[1] - local[1]))})" for q in poly)


def _path_items(path: Path, net: str, key: str) -> List[str]:
    out = []
    for i, sg in enumerate(path.segs):
        a, b = _k(sg.p0), _k(sg.p1)
        if sg.kind == "line":
            out.append(
                f"\t(segment (start {_n(a[0])} {_n(a[1])}) (end {_n(b[0])} {_n(b[1])}) (width {_n(sg.width)})"
                f' (layer "F.Cu") (net "{net}") (uuid "{_u(key, i)}"))'
            )
        else:
            m = _k(sg.mid())
            out.append(
                f"\t(arc (start {_n(a[0])} {_n(a[1])}) (mid {_n(m[0])} {_n(m[1])}) (end {_n(b[0])} {_n(b[1])})"
                f' (width {_n(sg.width)}) (layer "F.Cu") (net "{net}") (uuid "{_u(key, i)}"))'
            )
    return out


def _zone(net: str, layer: str, poly, key: str, clearance: float = 0.10, prio: int = 0) -> str:
    return (
        f'\t(zone (net "{net}") (layer "{layer}") (uuid "{_u("zone", key)}") (hatch edge 0.5)'
        f" (priority {prio}) (connect_pads yes (clearance {_n(clearance)})) (min_thickness 0.1)"
        " (fill (thermal_gap 0.3) (thermal_bridge_width 0.3) (island_removal_mode 0))"
        f" (polygon (pts {_pts(poly)})))"
    )


def _keepout(layers: List[str], poly, key: str) -> str:
    ls = " ".join(f'"{x}"' for x in layers)
    return (
        f'\t(zone (layers {ls}) (uuid "{_u("keep", key)}") (hatch edge 0.5)'
        " (connect_pads (clearance 0)) (min_thickness 0.1)"
        " (keepout (tracks allowed) (vias allowed) (pads allowed) (copperpour not_allowed) (footprints allowed))"
        " (fill (thermal_gap 0.5) (thermal_bridge_width 0.5))"
        f" (polygon (pts {_pts(poly)})))"
    )


def u1_balls(mc: Macro) -> List[Tuple[str, Pt, str]]:
    """Balls of rings 0-2 on the two RF edges with their nets; GND balls only where the
    macro's under-package pour reaches them (others would be unconnected in a fragment)."""
    mir = bool(mc.params["mirror_x"])
    nets = {b: "GND" for b in VSSA}
    nets.update({b: n for n, b in RF_BALLS.items()})
    # nets the macro does not own get one name per ball: the PnR connects them, not the macro
    nets.update({b: f"EXT_{n}_{b}" for b, n in OTHER_EDGE_BALLS.items()})
    out = []
    for name, net in sorted(nets.items()):
        r = ROWS.index(name[0])
        c = int(name[1:])
        if not (r <= 2 or c <= 3):
            continue
        xy = ball_xy(name, mir)
        if net == "GND" and not any(
            p[0][0] - 1e-6 <= xy[0] <= p[2][0] + 1e-6 and p[0][1] - 1e-6 <= xy[1] <= p[2][1] + 1e-6
            for p in mc.pour[2:]
        ):
            continue
        out.append((name, xy, net))
    return out


def board_text(mc: Macro, title: str) -> str:
    items: List[str] = []
    nets = ["GND"] + list(RF_BALLS)
    balls = u1_balls(mc)
    for _, _, n in balls:
        if n not in nets:
            nets.append(n)
    # --- U1 rings 0-2 of the RF edges (land pattern from the data sheet, not a TI file)
    pads = []
    land, mo = PKG["land"], (PKG["mask_open"] - PKG["land"]) / 2
    for name, xy, net in balls:
        pads.append(
            f'\t\t(pad "{name}" smd circle (at {_n(xy[0])} {_n(-xy[1])}) (size {_n(land)} {_n(land)})'
            f' (layers "F.Cu" "F.Mask" "F.Paste") (net "{net}") (solder_mask_margin {_n(mo)}) (uuid '
            f'"{_u("u1", name)}"))'
        )
    b = PKG["body"] / 2
    items.append(
        f'\t(footprint "radar60:IWR6843_ABL0161_RF_EDGES" (layer "F.Cu") (uuid "{_u("fp", "U1")}") (at '
        f"{_n(X0)} {_n(Y0)})\n"
        f'\t\t(property "Reference" "U1" (at 0 {_n(b + 0.8)}) (layer "F.Fab") (uuid "{_u("u1ref")}") '
        "(effects (font (size 0.8 0.8) (thickness 0.12))))\n"
        '\t\t(property "Value" "IWR6843AQGABLR (rings 0-2 of the RF edges)" (at 0 0) (layer "F.Fab") (hide '
        f'yes) (uuid "{_u("u1val")}") (effects (font (size 0.8 0.8) (thickness 0.12))))\n'
        "\t\t(attr smd)\n"
        f"\t\t(fp_rect (start {_n(-b)} {_n(-b)}) (end {_n(b)} {_n(b)}) (stroke (width 0.1) (type solid)) "
        f'(fill no) (layer "F.Fab") (uuid "{_u("u1fab")}"))\n' + "\n".join(pads) + "\n\t)"
    )
    # --- feeds and column paths (tracks), patches (footprint custom pads)
    for n, f in mc.feeds.items():
        items += _path_items(f, n, f"feed/{n}")
    for n, c in mc.columns.items():
        for i, q in enumerate(_place_paths(c)):
            items += _path_items(q, n, f"col/{n}/{i}")
        o = c.origin
        pd = []
        for i, poly in enumerate(c.patches):
            cx = 0.5 * (min(q[0] for q in poly) + max(q[0] for q in poly))
            cy = 0.5 * (min(q[1] for q in poly) + max(q[1] for q in poly))
            # anchor at the patch centre (inside the copper), primitives relative to it
            prim = " ".join(f"(xy {_n(q[0] - cx)} {_n(-(q[1] - cy))})" for q in poly)
            pd.append(
                f'\t\t(pad "1" smd custom (at {_n(cx - o[0])} {_n(-(cy - o[1]))}) (size 0.2 0.2) (layers '
                f'"F.Cu") (net "{n}")'
                f' (uuid "{_u("pad", n, i)}") (options (clearance outline) (anchor rect))'
                f" (primitives (gr_poly (pts {prim}) (width 0) (fill yes))))"
            )
        kx, ky = _k(o)
        items.append(
            f'\t(footprint "radar60:COL2_CORPORATE" (layer "F.Cu") (uuid "{_u("fp", n)}") (at {_n(kx)} {_n(ky)})\n'
            f'\t\t(property "Reference" "ANT_{n}" (at 0 {_n(-3.2)}) (layer "F.Fab") (uuid "{_u("ref", n)}") '
            "(effects (font (size 0.5 0.5) (thickness 0.08))))\n"
            '\t\t(property "Value" "2-patch corporate column" (at 0 0) (layer "F.Fab") (hide yes) (uuid '
            f'"{_u("val", n)}") (effects (font (size 0.5 0.5) (thickness 0.08))))\n'
            "\t\t(attr smd exclude_from_pos_files exclude_from_bom)\n" + "\n".join(pd) + "\n\t)"
        )
    # --- vias
    for i, (xy, drill, pad, role) in enumerate(mc.vias):
        k = _k(xy)
        items.append(
            f'\t(via (at {_n(k[0])} {_n(k[1])}) (size {_n(pad)}) (drill {_n(drill)}) (layers "F.Cu" "B.Cu") '
            f'(net "GND") (uuid "{_u("via", i)}"))'
        )
    # --- zones and rule areas
    for i, poly in enumerate(mc.pour):
        items.append(_zone("GND", "F.Cu", poly, f"pour{i}", prio=i))
    x0, x1 = mc.region["x"]
    y0, y1 = mc.region["y"]
    plane = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    items.append(_zone("GND", "In1.Cu", plane, "l2"))
    items.append(_zone("GND", "In2.Cu", plane, "l3"))
    for i, poly in enumerate(channel_outlines(mc)):
        items.append(_keepout(["F.Cu"], poly, f"chan{i}"))
    for i, poly in enumerate(mc.antipads):
        items.append(_keepout(["F.Cu"], poly, f"anti{i}"))
    for i, poly in enumerate(mc.pour_keepouts):
        items.append(_keepout(["F.Cu"], poly, f"field{i}"))
    for i, poly in enumerate(mc.l2_cuts):
        items.append(_keepout(["In1.Cu"], poly, f"cut{i}"))
    for n, c in mc.columns.items():
        for i, poly in enumerate(c.windows):
            items.append(_keepout(["In1.Cu"], poly, f"win/{n}/{i}"))
    # --- mask opening over the RF copper (one footprint, so a custom rule can name it)
    polys = "\n".join(
        f"\t\t(fp_poly (pts {_pts(poly, (0.0, 0.0))}) (stroke (width 0) (type solid)) (fill yes) (layer "
        f'"F.Mask") (uuid "{_u("mask", i)}"))'
        for i, poly in enumerate(mc.mask_open)
    )
    items.append(
        f'\t(footprint "radar60:RFM1_MASK" (layer "F.Cu") (uuid "{_u("fp", "mask")}") (at {_n(X0)} {_n(Y0)})\n'
        f'\t\t(property "Reference" "MO1" (at 0 0) (layer "F.Fab") (hide yes) (uuid "{_u("moref")}") '
        "(effects (font (size 0.5 0.5) (thickness 0.08))))\n"
        '\t\t(property "Value" "RF mask opening" (at 0 0) (layer "F.Fab") (hide yes) (uuid '
        f'"{_u("moval")}") (effects (font (size 0.5 0.5) (thickness 0.08))))\n'
        "\t\t(attr smd exclude_from_pos_files exclude_from_bom)\n" + polys + "\n\t)"
    )
    # --- outline
    m = 0.6
    e0, e1 = _k((x0 - m, y1 + m)), _k((x1 + m, y0 - m))
    items.append(
        f"\t(gr_rect (start {_n(e0[0])} {_n(e0[1])}) (end {_n(e1[0])} {_n(e1[1])}) (stroke (width 0.05) "
        f'(type solid)) (fill no) (layer "Edge.Cuts") (uuid "{_u("edge")}"))'
    )
    head = _header(title, nets)
    return head + "\n".join(items) + "\n)\n"


def _header(title: str, nets: List[str]) -> str:
    s = STACK
    fr4 = '(material "FR-4 (IT180A class)") (epsilon_r 4.3) (loss_tangent 0.015)'
    return (
        '(kicad_pcb\n\t(version 20241229)\n\t(generator "radar60_rfmacro")\n\t(generator_version "10.0")\n'
        '\t(general (thickness 1.17) (legacy_teardrops no))\n\t(paper "A3")\n'
        f'\t(title_block (title "{title}") (rev "draft") (comment 1 "generated by python -m rfmacro board; '
        'do not edit"))\n'
        "\t(layers\n"
        '\t\t(0 "F.Cu" signal)\n\t\t(4 "In1.Cu" signal)\n\t\t(6 "In2.Cu" signal)\n\t\t(8 "In3.Cu" signal)\n'
        '\t\t(10 "In4.Cu" signal)\n\t\t(2 "B.Cu" signal)\n'
        '\t\t(13 "F.Paste" user)\n\t\t(15 "B.Paste" user)\n\t\t(5 "F.SilkS" user "F.Silkscreen")\n'
        '\t\t(7 "B.SilkS" user "B.Silkscreen")\n\t\t(1 "F.Mask" user)\n\t\t(3 "B.Mask" user)\n'
        '\t\t(17 "Dwgs.User" user "User.Drawings")\n\t\t(19 "Cmts.User" user "User.Comments")\n'
        '\t\t(25 "Edge.Cuts" user)\n\t\t(27 "Margin" user)\n\t\t(31 "F.CrtYd" user "F.Courtyard")\n'
        '\t\t(29 "B.CrtYd" user "B.Courtyard")\n\t\t(35 "F.Fab" user)\n\t\t(33 "B.Fab" user)\n\t)\n'
        "\t(setup\n\t\t(stackup\n"
        '\t\t\t(layer "F.SilkS" (type "Top Silk Screen"))\n\t\t\t(layer "F.Paste" (type "Top Solder Paste"))\n'
        '\t\t\t(layer "F.Mask" (type "Top Solder Mask") (thickness 0.015))\n'
        f'\t\t\t(layer "F.Cu" (type "copper") (thickness {s["t_l1"]}))\n'
        f'\t\t\t(layer "dielectric 1" (type "core") (thickness {s["h_core"]}) (material "RO4835 LoPro") '
        f'(epsilon_r {s["dk_core"]}) (loss_tangent {s["df_core"]}))\n'
        f'\t\t\t(layer "In1.Cu" (type "copper") (thickness {s["t_l2"]}))\n'
        f'\t\t\t(layer "dielectric 2" (type "prepreg") (thickness {s["h_bond"]}) (material "RO4450F") '
        f'(epsilon_r {s["dk_bond"]}) (loss_tangent {s["df_bond"]}))\n'
        '\t\t\t(layer "In2.Cu" (type "copper") (thickness 0.0175))\n'
        f'\t\t\t(layer "dielectric 3" (type "core") (thickness 0.51) {fr4})\n'
        '\t\t\t(layer "In3.Cu" (type "copper") (thickness 0.035))\n'
        f'\t\t\t(layer "dielectric 4" (type "prepreg") (thickness 0.10) {fr4})\n'
        '\t\t\t(layer "In4.Cu" (type "copper") (thickness 0.0175))\n'
        f'\t\t\t(layer "dielectric 5" (type "core") (thickness 0.20) {fr4})\n'
        '\t\t\t(layer "B.Cu" (type "copper") (thickness 0.035))\n'
        '\t\t\t(layer "B.Mask" (type "Bottom Solder Mask") (thickness 0.015))\n'
        '\t\t\t(layer "B.Paste" (type "Bottom Solder Paste"))\n\t\t\t(layer "B.SilkS" (type "Bottom Silk Screen"))\n'
        '\t\t\t(copper_finish "Immersion silver")\n\t\t\t(dielectric_constraints yes)\n\t\t)\n'
        "\t\t(pad_to_mask_clearance 0)\n\t\t(allow_soldermask_bridges_in_footprints no)\n\t\t(tenting "
        "front back)\n\t)\n"
        '\t(net 0 "")\n' + "".join(f'\t(net {i + 1} "{n}")\n' for i, n in enumerate(nets))
    )


def project_json(name: str) -> Dict:
    rules = {
        "allow_blind_buried_vias": False,
        "allow_microvias": False,
        "max_error": 0.002,
        "min_clearance": RULES["space_min"],
        "min_connection": 0.0,
        "min_copper_edge_clearance": RULES["edge_clear"],
        "min_groove_width": 0.0,
        "min_hole_clearance": 0.15,
        "min_hole_to_hole": 0.25,
        "min_microvia_diameter": 0.2,
        "min_microvia_drill": 0.1,
        "min_resolved_spokes": 2,
        "min_silk_clearance": 0.0,
        "min_text_height": 0.5,
        "min_text_thickness": 0.08,
        "min_through_hole_diameter": RULES["via_fence"][0],
        "min_track_width": RULES["track_min"],
        "min_via_annular_width": 0.075,
        "min_via_diameter": RULES["via_fence"][1],
        "solder_mask_to_copper_clearance": 0.0,
        "use_height_for_length_calcs": True,
    }
    cls = {
        "bus_width": 12,
        "clearance": RULES["space_min"],
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
        "via_diameter": 0.3,
        "via_drill": 0.15,
        "wire_width": 6,
    }
    return {
        "board": {
            "design_settings": {
                "defaults": {},
                "diff_pair_dimensions": [],
                "drc_exclusions": [],
                "meta": {"version": 2},
                "rule_severities": {
                    "missing_courtyard": "ignore",
                    "lib_footprint_issues": "ignore",
                    "lib_footprint_mismatch": "ignore",
                },
                "rules": rules,
                "track_widths": [],
                "via_dimensions": [],
                "zones_allow_external_fillets": False,
            }
        },
        "boards": [],
        "meta": {"filename": f"{name}.kicad_pro", "version": 3},
        "net_settings": {
            "classes": [cls],
            "meta": {"version": 4},
            "net_colors": None,
            "netclass_assignments": None,
            "netclass_patterns": [],
        },
        "pcbnew": {"page_layout_descr_file": ""},
        "sheets": [],
        "text_variables": {},
    }


def dru_text() -> str:
    """Two intentional exceptions: no mask over RF copper (TI SPRACG5 §2.3), so the opening
    bridges RF and GND copper; SMA edge launches reach the board edge (coupon strip only)."""
    return (
        "(version 1)\n"
        '(rule "RF mask opening, no mask over RF copper per SPRACG5"\n'
        "\t(constraint bridged_mask)\n"
        "\t(condition \"A.memberOfFootprint('MO1') || B.memberOfFootprint('MO1')\")\n"
        "\t(severity ignore))\n"
        '(rule "SMA edge launches reach the board edge"\n'
        "\t(constraint edge_clearance)\n"
        "\t(condition \"A.memberOfFootprint('J*')\")\n"
        "\t(severity ignore))\n"
    )


def write(mc: Macro, out_dir: str, name: str, title: str) -> Dict[str, str]:
    import os

    os.makedirs(out_dir, exist_ok=True)
    paths = {}
    p = os.path.join(out_dir, f"{name}.kicad_pcb")
    with open(p, "w", encoding="utf-8") as f:
        f.write(board_text(mc, title))
    paths["pcb"] = p
    p = os.path.join(out_dir, f"{name}.kicad_pro")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(project_json(name), f, indent=2)
        f.write("\n")
    paths["pro"] = p
    p = os.path.join(out_dir, f"{name}.kicad_dru")
    with open(p, "w", encoding="utf-8") as f:
        f.write(dru_text())
    paths["dru"] = p
    return paths
