"""Generate the radar60 board-track files from floorplan.yaml.

Outputs, beside this script:

- ``constraints.yaml``: the yapnr constraint file (docs/hardware/pnr-inputs.md, schema v0, plus the
  ``region`` section of the region/align work and proposed sections the engine ignores with a
  warning today);
- ``radar60.kicad_pcb``: the floorplan board: outline with corner radii, the four plated mounting
  holes, the radome standoff lands and the named rule areas (``RF_REGION``, ``RF_POCKET``,
  ``RF_GUARD``) the custom rules test, plus the placement regions drawn on User.1;
- ``radar60.kicad_pro``: the design rules of the fab profile and the net classes;
- ``radar60.kicad_dru``: the profile's custom rules (from ``pnr.fab_profile.dru_text``) followed
  by the board's own rules: RF keepouts, fence vias, BGA escape classes, noise distances, LVDS.

Usage (from the repository root, with PyYAML)::

    PYTHONPATH=.:hardware/pnr python3 examples/radar60/board/gen_board.py [--check]

``--check`` regenerates in memory and exits 1 when a committed file differs (YAML and JSON are
compared as data, so formatters may restyle them).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import uuid
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
OFFSET = 30.0  # KiCad page offset of the board origin (pnr.writeback._PAGE_OFFSET_MM)
COPPER = ["F.Cu", "In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu", "B.Cu"]
RF_LAYERS = ["F.Cu", "In1.Cu", "In2.Cu"]
# R4 (no digital copper next to the RF region): the net classes it exempts (audit.py uses the same)
R4_EXEMPT = ("RF", "PWR", "GND", "ANALOG")


def _u(*parts):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "yapnr/examples/radar60/board/" + "/".join(parts)))


def _n(v):
    return ("%.4f" % v).rstrip("0").rstrip(".") if v != int(v) else str(int(v))


class Floorplan:
    def __init__(self, doc):
        self.doc = doc
        b = doc["board"]
        self.w, self.h = float(b["width"]), float(b["height"])

    def k(self, x, y):
        """Board (y-up, origin lower left) to KiCad page coordinates (y down)."""
        return (OFFSET + x, OFFSET + self.h - y)

    def part(self, role):
        return self.doc["parts"][role]

    def rect(self, spec):
        """A region's rectangle (the bounding box of its ``areas``); ``rf.pocket`` names the RF
        pocket."""
        if "areas" in spec:
            a = spec["areas"]
            return [
                min(r[0] for r in a),
                min(r[1] for r in a),
                max(r[2] for r in a),
                max(r[3] for r in a),
            ]
        r = spec["rect"]
        return self.doc["rf"]["pocket"] if r == "rf.pocket" else r

    def areas(self, spec):
        """A region's rectangles."""
        return spec["areas"] if "areas" in spec else [self.rect(spec)]


def load(path=HERE / "floorplan.yaml"):
    return Floorplan(yaml.safe_load(Path(path).read_text()))


def rect_subtract(r, hole):
    """Rectangle ``r`` minus rectangle ``hole``: up to four rectangles."""
    x0, y0, x1, y1 = r
    hx0, hy0, hx1, hy1 = max(hole[0], x0), max(hole[1], y0), min(hole[2], x1), min(hole[3], y1)
    if hx0 >= hx1 or hy0 >= hy1:
        return [list(r)]
    out = [
        [x0, y0, x1, hy0],  # below
        [x0, hy1, x1, y1],  # above
        [x0, hy0, hx0, hy1],  # left
        [hx1, hy0, x1, hy1],  # right
    ]
    return [[round(v, 4) for v in q] for q in out if q[2] - q[0] > 1e-6 and q[3] - q[1] > 1e-6]


def rect_pts(r):
    x0, y0, x1, y1 = r
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def circle_pts(cx, cy, radius, n=24):
    return [
        (cx + radius * math.cos(2 * math.pi * i / n), cy + radius * math.sin(2 * math.pi * i / n))
        for i in range(n)
    ]


# --------------------------------------------------------------------------- constraints.yaml


def _glob_literal(path):
    """An instance path as an fnmatch pattern that matches only itself (``mh[0]`` -> ``mh[[]0]``)."""
    return "".join("[%s]" % c if c in "[*?" else c for c in path)


def constraints(fp: Floorplan):
    """The yapnr constraint document (a dict; written with comments by :func:`constraints_text`)."""
    d = fp.doc
    radio = "@" + fp.part("radio")
    u1 = d["u1"]

    def refs(role):
        paths = fp.doc["parts"][role]
        return ["@" + x for x in ([paths] if isinstance(paths, str) else paths)]

    net_class = {}
    for name, spec in d["nets"].items():
        if name == "LVDS":
            continue
        entry = {"nets": list(spec["globs"])}
        if name == "GND":
            entry["plane_layer"] = [k for k, v in d["board"]["planes"].items() if v == "GND"][0]
        if name == "PWR":
            entry["width_mm"] = 0.25
            entry["clearance_mm"] = 0.15
        if name == "SW":
            entry["width_mm"] = 0.6
            entry["clearance_mm"] = 0.2
        if name == "XTAL":
            entry["clearance_mm"] = 0.15
        net_class[name] = entry
    lv = d["lvds"]
    diff_pair = [
        {
            "name": name,
            "p": p,
            "n": n,
            "width_mm": lv["width"],
            "gap_mm": lv["gap"],
            "skew_mm": lv["skew_mm"],
        }
        for name, (p, n) in d["nets"]["LVDS"]["pairs"].items()
    ]
    lvds_nets = [x for pair in d["nets"]["LVDS"]["pairs"].values() for x in pair]
    net_class["LVDS"] = {"nets": lvds_nets, "width_mm": lv["width"], "clearance_mm": 0.1}
    region, side = [], {}
    for name, spec in d["regions"].items():
        area = (
            {"areas": [{"rect": r} for r in spec["areas"]]}
            if "areas" in spec
            else {"rect": fp.rect(spec)}
        )
        region.append(
            {
                "name": name,
                "refs": [r for p in spec["parts"] for r in refs(p)],
                **area,
                "hard": True,
                "reason": spec["reason"],
            }
        )
        if spec.get("side"):  # a region on the other side holds its parts there (hard)
            side.setdefault(spec["side"], []).extend(r for p in spec["parts"] for r in refs(p))
    group = [
        {
            "members": [r for m in spec["members"] for r in refs(m)],
            "anchor": refs(spec["anchor"])[0],
            "radius_mm": spec["radius_mm"],
            "hard": True,
        }
        for spec in d["groups"].values()
    ]
    # The four M2.5 holes are the schematic's own mounting-hole parts (mech.mh[i], plated, GND),
    # fixed at their positions; the radome standoff lands are board-only footprints of the
    # floorplan board (RL1, RL2), fixed too. A `mounting_hole` entry would make yapnr drill an
    # extra unplated hole (pnr.writeback.apply_mounting_holes), so none is emitted.
    holes = d["mounting_holes"]
    lands = d["radome_lands"]
    hole_parts = fp.doc["parts"]["holes"]
    if len(hole_parts) != len(holes["at"]):
        raise SystemExit("floorplan: parts.holes and mounting_holes.at differ in length")
    fixed_holes = {
        "@" + _glob_literal(path): {"at": at, "rot": 0, "side": "top"}
        for path, at in zip(hole_parts, holes["at"])
    }
    fixed_lands = {
        "RL%d" % (i + 1): {"at": at, "rot": 0, "side": "top"} for i, at in enumerate(lands["at"])
    }
    # Copper keepouts relative to U1 (rect_mm in U1's own y-up frame): the router blocks every
    # layer there and writeback draws a KiCad rule area. Inverse of pnr.graph.footprint_point.
    cx, cy = u1["at"]
    a = math.radians(u1["rot"])
    co, si = math.cos(a), math.sin(a)

    def local(x, y):
        dx, dy = x - cx, y - cy
        return (round(co * dx + si * dy, 4), round(-si * dx + co * dy, 4))

    copper_keepout = []
    for i, r in enumerate(d["rf"]["region"]):
        p0, p1 = local(r[0], r[1]), local(r[2], r[3])
        copper_keepout.append(
            {
                "name": "rf_region_%d" % (i + 1),
                "ref": radio,
                "rect_mm": [
                    min(p0[0], p1[0]),
                    min(p0[1], p1[1]),
                    max(p0[0], p1[0]),
                    max(p0[1], p1[1]),
                ],
            }
        )
    # Placement keepout: the RF region less U1's courtyard (the macro is drawn around U1, whose
    # courtyard reaches into it; yapnr would count the fixed U1 as a keepout violation).
    half = u1["courtyard_mm"] / 2
    u1_box = [cx - half, cy - half, cx + half, cy + half]
    pieces = [q for r in d["rf"]["region"] for q in rect_subtract(r, u1_box)]
    keepout = [
        {"name": "rf_region_%d" % (i + 1), "polygon": [list(p) for p in rect_pts(r)]}
        for i, r in enumerate(pieces)
    ]
    doc = {
        "schema": "v0",
        "board": {
            "outline": {"w": fp.w, "h": fp.h},
            "layers": d["board"]["copper_layers"],
            "default_clearance_mm": 0.2,
            "references_on_fab": True,
        },
        "fab": {"track_width_mm": 0.15},
        "fixed": {
            radio: {"at": u1["at"], "rot": u1["rot"], "side": "top"},
            "@"
            + fp.part("connector"): {
                "edge": "south",
                "align": "center",
                "rot": d["connector"]["rot"],
                "side": "top",
            },
            **fixed_holes,
            **fixed_lands,
            **(
                {
                    "@"
                    + fp.part("lvds_header"): {
                        "at": d["lvds_header_fixed"]["at"],
                        "rot": d["lvds_header_fixed"]["rot"],
                        "side": d["lvds_header_fixed"]["side"],
                    }
                }
                if d.get("lvds_header_fixed")
                else {}
            ),
            **(
                {
                    "@"
                    + fp.part("jtag"): {
                        "at": d["jtag_fixed"]["at"],
                        "rot": d["jtag_fixed"]["rot"],
                        "side": d["jtag_fixed"]["side"],
                    }
                }
                if d.get("jtag_fixed")
                else {}
            ),
        },
        **(
            {"orientation": {r: rot for role, rot in d["orientations"].items() for r in refs(role)}}
            if d.get("orientations")
            else {}
        ),
        "keepout": keepout,
        "copper_keepout": copper_keepout,
        "region": region,
        **({"side": side} if side else {}),
        "group": group,
        "net_class": net_class,
        "diff_pair": diff_pair,
        "length_match": [
            {"name": "lvds", "nets": lvds_nets, "tolerance_mm": lv["group_skew_mm"]},
        ],
        # Proposed sections (plan 3.4, 7.3): the current engine warns and ignores them.
        "rf_macro": {
            "ref": "@" + fp.part("rf_macro"),
            "frame": radio,
            "lock": "fixed",
            "layers": RF_LAYERS,
            "owns_vias": True,
            "region": d["rf"]["region"],
            "pocket": d["rf"]["pocket"],
        },
        "noise_keepout": [
            {
                "name": "sw_to_crystal",
                "nets": d["nets"]["SW"]["globs"],
                "from": "@" + fp.part("crystal"),
                "min_distance_mm": d["noise"]["sw_to_crystal_mm"],
            },
            {
                "name": "sw_to_rf",
                "nets": d["nets"]["SW"]["globs"],
                "from_region": "rf_macro",
                "min_distance_mm": d["noise"]["sw_to_rf_mm"],
            },
            {
                "name": "digital_to_rf",
                "layers": RF_LAYERS,
                "from_region": "rf_macro",
                "min_distance_mm": d["rf"]["guard_mm"],
                "except_classes": list(R4_EXEMPT),
            },
        ],
        "height_limit": {
            "rule": "radome_visibility",
            "angle_deg": d["radome"]["angle_deg"],
            "from_copper": [d["rf"]["patches"]["rx"], d["rf"]["patches"]["tx"]],
        },
    }
    return doc


HEADER = """\
# radar60 Rev A: yapnr constraints. Generated by gen_board.py from floorplan.yaml: edit that file.
#
# Frame: mm, origin at the board's lower-left corner, +y north (towards the antennas), rot CCW.
# Parts are named by atopile instance path (@...), nets by the schematic's net names (globs);
# floorplan.yaml lists both. Route under
# PNR_FAB_PROFILE=pcbway-adv-6l-rf (stackup pcbway-6l-ro4835-ro4450f); radar60.kicad_dru beside
# the board carries the profile's custom rules and the board's own.
#
# Sections: fixed (U1 in the RF macro's frame, J1 on the south edge, the four mounting-hole
# parts, the floorplan board's two radome standoff lands), keepout / copper_keepout (the RF
# region minus the VOUT_PA pocket), region (J2, J3, PMIC, switch nodes, Y1, the pocket; hard),
# group (decoupling, Y1, buck loops; hard),
# net_class / diff_pair / length_match (routing). rf_macro, noise_keepout and height_limit are
# proposed (radar60 plan 3.4, 7.3, 7.5): the current engine warns and ignores them, and the
# custom rules and the RF audit enforce them meanwhile.
"""


def _yaml_scalar(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return _n(float(v)) if isinstance(v, float) else str(v)
    s = str(v)
    if s and all(c.isalnum() or c in "._-/" for c in s) and not s[0].isdigit():
        return s
    return json.dumps(s)


def _yaml(v, indent=0):
    """Block YAML (flow lists of scalars), stable and close to prettier's style."""
    pad = "  " * indent
    if isinstance(v, dict):
        out = []
        for key, val in v.items():
            k = _yaml_scalar(key)
            if isinstance(val, (dict, list)) and val and not _flat_list(val):
                out.append("%s%s:\n%s" % (pad, k, _yaml(val, indent + 1)))
            else:
                out.append("%s%s: %s" % (pad, k, _inline(val)))
        return "\n".join(out)
    if isinstance(v, list):
        out = []
        for item in v:
            if isinstance(item, dict):
                body = _yaml(item, indent + 1).lstrip()
                out.append("%s- %s" % (pad, body))
            else:
                out.append("%s- %s" % (pad, _inline(item)))
        return "\n".join(out)
    return pad + _inline(v)


def _flat_list(v):
    return isinstance(v, list) and all(
        not isinstance(x, (dict, list)) or (isinstance(x, list) and _flat_list(x)) for x in v
    )


def _inline(v):
    if isinstance(v, tuple):
        raise TypeError("write points as [x, y] lists, not tuples: %r" % (v,))
    if isinstance(v, list):
        return "[" + ", ".join(_inline(x) for x in v) + "]"
    if isinstance(v, dict):
        return (
            "{ " + ", ".join("%s: %s" % (_yaml_scalar(k), _inline(x)) for k, x in v.items()) + " }"
        )
    return _yaml_scalar(v)


def constraints_text(fp):
    return HEADER + "\n" + _yaml(constraints(fp)) + "\n"


# --------------------------------------------------------------------------- KiCad files


def profile_spec(name):
    sys.path[:0] = [str(REPO), str(REPO / "hardware/pnr")]
    from yapnr.fab import capability

    return capability.profile(name), capability.engine_profile(name)


def netclasses(fp):
    """KiCad net classes: name -> (clearance, track width, diff pair width, gap)."""
    lv = fp.doc["lvds"]
    return {
        "Default": dict(clearance=0.1, track_width=0.15),
        "GND": dict(clearance=0.1, track_width=0.25),
        "PWR": dict(clearance=0.15, track_width=0.25),
        "SW": dict(clearance=0.2, track_width=0.6),
        "RF": dict(clearance=0.1, track_width=0.2),
        "ANALOG": dict(clearance=0.1, track_width=0.15),
        "XTAL": dict(clearance=0.15, track_width=0.15),
        "QSPI": dict(clearance=0.1, track_width=0.12),
        "LVDS": dict(
            clearance=0.1,
            track_width=lv["width"],
            diff_pair_width=lv["width"],
            diff_pair_gap=lv["gap"],
        ),
    }


def netclass_patterns(fp):
    """Net-name patterns -> class (floorplan ``nets``), so the custom rules' hasNetclass() sees
    the board's real nets wherever this project file is used."""
    out = []
    for name, spec in fp.doc["nets"].items():
        if name == "LVDS":
            pats = [n for pair in spec["pairs"].values() for n in pair]
        else:
            pats = spec["globs"]
        out.extend({"netclass": name, "pattern": p} for p in pats)
    return out


def project(fp):
    _, eng = profile_spec(fp.doc["board"]["profile"])
    f = eng["fab"]
    classes = []
    for prio, (name, c) in enumerate(netclasses(fp).items()):
        classes.append(
            {
                "bus_width": 12,
                "clearance": c["clearance"],
                "diff_pair_gap": c.get("diff_pair_gap", 0.2),
                "diff_pair_via_gap": 0.25,
                "diff_pair_width": c.get("diff_pair_width", 0.2),
                "line_style": 0,
                "microvia_diameter": 0.3,
                "microvia_drill": 0.1,
                "name": name,
                "pcb_color": "rgba(0, 0, 0, 0.000)",
                "priority": 2147483647 if name == "Default" else prio,
                "schematic_color": "rgba(0, 0, 0, 0.000)",
                "track_width": c["track_width"],
                "via_diameter": f["via_diameter_mm"],
                "via_drill": f["via_drill_mm"],
                "wire_width": 6,
            }
        )
    return {
        "board": {
            "design_settings": {
                "defaults": {},
                "diff_pair_dimensions": [],
                "drc_exclusions": [],
                "meta": {"version": 2},
                "rules": {
                    "allow_blind_buried_vias": False,
                    "allow_microvias": False,
                    "max_error": 0.005,
                    "min_clearance": f["clearance_mm"],
                    "min_connection": 0.0,
                    "min_copper_edge_clearance": f["edge_clearance_mm"],
                    "min_groove_width": 0.0,
                    "min_hole_clearance": f["hole_clearance_mm"],
                    "min_hole_to_hole": f["hole_to_hole_mm"],
                    "min_microvia_diameter": 0.2,
                    "min_microvia_drill": 0.1,
                    "min_resolved_spokes": 2,
                    "min_silk_clearance": 0.0,
                    "min_text_height": 0.8,
                    "min_text_thickness": 0.08,
                    "min_through_hole_diameter": f["min_through_drill_mm"],
                    "min_track_width": f["min_track_width_mm"],
                    "min_via_annular_width": f["via_annular_mm"],
                    "min_via_diameter": f["min_via_diameter_mm"],
                    "solder_mask_to_copper_clearance": 0.0,
                    "use_height_for_length_calcs": True,
                },
                "track_widths": [],
                "via_dimensions": [
                    {"diameter": v["diameter_mm"], "drill": v["drill_mm"]}
                    for v in f["via_classes"].values()
                ],
                "zones_allow_external_fillets": False,
            }
        },
        "boards": [],
        "meta": {"filename": "radar60.kicad_pro", "version": 3},
        "net_settings": {
            "classes": classes,
            "meta": {"version": 4},
            "net_colors": None,
            "netclass_assignments": None,
            "netclass_patterns": netclass_patterns(fp),
        },
        "pcbnew": {"page_layout_descr_file": ""},
        "sheets": [],
        "text_variables": {},
    }


def board_rules(fp):
    """The board's own custom rules (KiCad 10 syntax), after the profile's."""
    d = fp.doc
    u1 = d["u1"]["ref"]
    macro = d["rf"]["macro_ref"]
    fence = d["rf"]["fence_via"]
    lv = d["lvds"]
    rf_layers = " || ".join("A.Layer == '%s'" % layer for layer in RF_LAYERS)
    # yapnr's writeback puts a diff pair's nets in class dp_<pair> (pnr.writeback), so the LVDS
    # rules name those classes too.
    lvds = "(%s)" % " || ".join(
        ["A.hasNetclass('LVDS')"]
        + ["A.hasNetclass('dp_%s')" % name for name in d["nets"]["LVDS"]["pairs"]]
    )
    planes = [layer for layer, net in d["board"]["planes"].items()]
    rules = [
        (
            "R2 RF region: no foreign tracks on F.Cu-In2.Cu (macro copper only; plan 5.2, 7.5)",
            "rf_region_tracks",
            "A.Type == 'Track' && (%s) && A.intersectsArea('RF_REGION') && !A.hasNetclass('RF')"
            % rf_layers,
            ["(constraint disallow track)"],
        ),
        (
            "R2 RF region: no vias but the macro's GND fence vias (RFS-5)",
            "rf_region_vias",
            "A.Type == 'Via' && A.intersectsArea('RF_REGION') && "
            "!(A.hasNetclass('GND') && A.Hole <= %smm)" % _n(fence["drill"] + 0.005),
            ["(constraint disallow via)"],
        ),
        (
            "RFS-5 fence vias: %s/%s mm; pitch >= %s mm follows from the profile's hole to hole"
            % (_n(fence["drill"]), _n(fence["diameter"]), _n(fence["min_pitch"])),
            "rf_fence_vias",
            "A.Type == 'Via' && A.intersectsArea('RF_REGION')",
            [
                "(constraint hole_size (min %smm))" % _n(fence["drill"]),
                "(constraint via_diameter (min %smm))" % _n(fence["diameter"]),
            ],
        ),
        (
            "R2 RF region: no top-side parts but the macro, U1 and the pocket parts",
            "rf_region_parts",
            "A.Type == 'Footprint' && A.Layer == 'F.Cu' && A.intersectsArea('RF_REGION') && "
            "A.Reference != '%s' && A.Reference != '%s' && !A.enclosedByArea('RF_POCKET')"
            % (macro, u1),
            ["(constraint disallow footprint)"],
        ),
        (
            "No solder mask over the RF copper (TI SPRACG5): the macro's F.Mask opening bridges "
            "its copper by design, inside the RF region or the macro footprint only",
            "rf_mask_opening",
            "A.intersectsArea('RF_REGION') || B.intersectsArea('RF_REGION') || "
            "A.memberOfFootprint('%s') || B.memberOfFootprint('%s') || "
            "A.memberOfFootprint('*:%s*') || B.memberOfFootprint('*:%s*')" % ((macro,) * 4),
            ["(constraint bridged_mask)", "(severity ignore)"],
        ),
        (
            "The 60 GHz nets never change layer (the RF balls have no vias; plan 7.2)",
            "rf_no_vias",
            "A.Type == 'Via' && A.hasNetclass('RF')",
            ["(constraint disallow via)"],
        ),
        (
            "R4 no digital copper within %s mm of the RF region on F.Cu-In2.Cu (rectangular band; "
            "the package body is exempt)" % _n(d["rf"]["guard_mm"]),
            "rf_guard_digital",
            "(A.Type == 'Via' || (A.Type == 'Track' && (%s))) && A.intersectsArea('RF_GUARD') && "
            "A.NetName != '' && %s"
            % (rf_layers, " && ".join("!A.hasNetclass('%s')" % c for c in R4_EXEMPT)),
            ["(constraint disallow track via)"],
        ),
        (
            "BGA escape: signal and power vias under U1 are the 0.20/0.40 escape class (dog-bones);"
            " the RF region's vias are the macro's own class",
            "bga_signal_vias",
            "A.Type == 'Via' && A.intersectsCourtyard('%s') && !A.hasNetclass('GND') && "
            "!A.intersectsArea('RF_REGION')" % u1,
            [
                "(constraint hole_size (min 0.2mm))",
                "(constraint via_diameter (min 0.4mm))",
            ],
        ),
        (
            "BGA escape: 0.15/0.35 vias under U1 are GND only (interstitial sites, same net); the"
            " macro's 0.15/0.32 fence vias at the package edge are the RF region's class",
            "bga_gnd_vias",
            "A.Type == 'Via' && A.intersectsCourtyard('%s') && A.hasNetclass('GND') && "
            "!A.intersectsArea('RF_REGION')" % u1,
            [
                "(constraint hole_size (min 0.15mm))",
                "(constraint via_diameter (min 0.35mm))",
            ],
        ),
        (
            "Vias away from U1 and the RF region: 0.20/0.40 or larger",
            "general_vias",
            "A.Type == 'Via' && !A.intersectsCourtyard('%s') && !A.intersectsArea('RF_REGION')"
            % u1,
            [
                "(constraint hole_size (min 0.2mm))",
                "(constraint via_diameter (min 0.4mm))",
            ],
        ),
        (
            "Crystal nets stay on F.Cu over the solid In1.Cu (no vias; plan 7.1)",
            "xtal_no_vias",
            "A.Type == 'Via' && A.hasNetclass('XTAL')",
            ["(constraint disallow via)"],
        ),
        (
            "Switch nodes >= %s mm from the crystal nets (noise budget; same-layer copper)"
            % _n(d["noise"]["sw_to_crystal_mm"]),
            "sw_to_xtal",
            "A.hasNetclass('SW') && B.hasNetclass('XTAL')",
            ["(constraint clearance (min %smm))" % _n(d["noise"]["sw_to_crystal_mm"])],
        ),
        (
            "Switch nodes >= %s mm from RF copper (R4)" % _n(d["noise"]["sw_to_rf_mm"]),
            "sw_to_rf",
            "A.hasNetclass('SW') && B.hasNetclass('RF')",
            ["(constraint clearance (min %smm))" % _n(d["noise"]["sw_to_rf_mm"])],
        ),
        (
            "LVDS pairs: %s/%s mm (100 ohm on F.Cu over In1.Cu), %s mm intra-pair skew; the "
            "breakout under U1 may neck down" % (_n(lv["width"]), _n(lv["gap"]), _n(lv["skew_mm"])),
            "lvds_pairs",
            "%s && !A.intersectsCourtyard('%s')" % (lvds, u1),
            [
                "(constraint track_width (min %smm) (opt %smm))"
                % (_n(lv["width"] - 0.02), _n(lv["width"])),
                "(constraint diff_pair_gap (min %smm) (opt %smm) (max %smm))"
                % (_n(lv["gap"] - 0.02), _n(lv["gap"]), _n(lv["gap"] + 0.04)),
                "(constraint diff_pair_uncoupled (max 3mm))",
                "(constraint skew (max %smm) (within_diff_pairs))" % _n(lv["skew_mm"]),
            ],
        ),
        (
            "LVDS on the outer layers only (plan 7.2: F.Cu or B.Cu, next to J2)",
            "lvds_outer_layers",
            "A.Type == 'Track' && %s && A.Layer != 'F.Cu' && A.Layer != 'B.Cu'" % lvds,
            ["(constraint disallow track)"],
        ),
        (
            "QSPI at 80 MHz: <= %s mm routed (plan 3.4 @pnr-si)" % _n(d["qspi_max_length_mm"]),
            "qspi_length",
            "A.hasNetclass('QSPI')",
            ["(constraint length (max %smm))" % _n(d["qspi_max_length_mm"])],
        ),
    ]
    for layer in planes:
        rules.append(
            (
                "%s is a GND plane%s: no tracks"
                % (layer, " (the RF reference)" if layer == "In1.Cu" else ""),
                "plane_%s" % layer.split(".")[0].lower(),
                "A.Type == 'Track' && A.Layer == '%s'" % layer,
                ["(constraint disallow track)"],
            )
        )
    lines = []
    for comment, name, condition, body in rules:
        lines += ["# " + comment, '(rule "radar60_%s"' % name]
        lines += ["  " + b for b in body]
        lines.append('  (condition "%s"))' % condition)
    return "\n".join(lines) + "\n"


DRU_HEAD = """\
(version 1)
# radar60 Rev A custom rules. Generated by examples/radar60/board/gen_board.py from floorplan.yaml
# and the fab profile; do not edit. Part 1 is the {profile} profile's rules, copied from
# pnr.fab_profile.dru_text (yapnr writes those alone beside a board that has no rules file;
# this file carries them and the board's rules, so yapnr leaves it as it is). Part 2 is the
# board's own rules: RF keepouts (rule areas RF_REGION, RF_POCKET, RF_GUARD in radar60.kicad_pcb),
# fence vias, BGA escape classes, noise distances, LVDS and QSPI. Of several matching rules of
# one constraint type, the later wins.
"""


def dru(fp):
    sys.path[:0] = [str(REPO), str(REPO / "hardware/pnr")]
    from pnr import fab_profile

    name = fp.doc["board"]["profile"]
    clear = {n: c["clearance"] for n, c in netclasses(fp).items()}
    text = fab_profile.dru_text(None, name, clear, 0.05)
    body = [line for line in text.splitlines()[3:]]  # drop the version and generated header
    return (
        DRU_HEAD.format(profile=name)
        + "\n# ---- Part 1: profile %s\n" % name
        + "\n".join(body)
        + "\n\n# ---- Part 2: board rules (floorplan.yaml)\n"
        + board_rules(fp)
    )


def _zone_rule_area(name, layers, pts, fp, keepout=None):
    ko = keepout or {}
    flags = " ".join(
        "(%s %s)" % (k, "not_allowed" if ko.get(k) else "allowed")
        for k in ("tracks", "vias", "pads", "copperpour", "footprints")
    )
    ls = " ".join('"%s"' % layer for layer in layers)
    kp = " ".join("(xy %s %s)" % tuple(_n(c) for c in fp.k(*p)) for p in pts)
    return (
        '\t(zone (net 0) (net_name "") (layers %s) (uuid "%s") (name "%s") (hatch edge 0.5)'
        " (connect_pads (clearance 0)) (min_thickness 0.25) (filled_areas_thickness no)"
        ' (keepout %s) (placement (enabled no) (sheetname ""))'
        " (fill (thermal_gap 0.5) (thermal_bridge_width 0.5)) (polygon (pts %s)))"
        % (ls, _u("zone", name, *[str(p) for p in pts[:1]]), name, flags, kp)
    )


def _gr_line(fp, a, b, layer, key, width=0.1):
    (ax, ay), (bx, by) = fp.k(*a), fp.k(*b)
    return (
        '\t(gr_line (start %s %s) (end %s %s) (stroke (width %s) (type solid)) (layer "%s")'
        ' (uuid "%s"))' % (_n(ax), _n(ay), _n(bx), _n(by), _n(width), layer, _u("line", key))
    )


def _gr_rect(fp, r, layer, key, width=0.1):
    (x0, y1), (x1, y0) = fp.k(r[0], r[1]), fp.k(r[2], r[3])
    return (
        "\t(gr_rect (start %s %s) (end %s %s) (stroke (width %s) (type dash)) (fill no)"
        ' (layer "%s") (uuid "%s"))'
        % (_n(x0), _n(y0), _n(x1), _n(y1), _n(width), layer, _u("rect", key))
    )


def _gr_text(fp, s, p, layer, key, size=0.8):
    x, y = fp.k(*p)
    return (
        '\t(gr_text "%s" (at %s %s 0) (layer "%s") (uuid "%s")'
        " (effects (font (size %s %s) (thickness 0.12)) (justify left)))"
        % (s, _n(x), _n(y), layer, _u("text", key), _n(size), _n(size))
    )


def _hole_fp(fp, ref, at, drill, pad, keepout, net="GND"):
    x, y = fp.k(*at)
    r = keepout / 2
    return "\n".join(
        [
            '\t(footprint "MountingHole_%smm_M2.5_Pad" (layer "F.Cu") (uuid "%s") (at %s %s 0)'
            % (_n(drill), _u("fp", ref), _n(x), _n(y)),
            '\t\t(property "Reference" "%s" (at 0 %s 0) (layer "F.Fab") (uuid "%s")'
            " (effects (font (size 0.8 0.8) (thickness 0.12))))"
            % (ref, _n(-r - 0.6), _u("ref", ref)),
            '\t\t(property "Value" "M2.5" (at 0 %s 0) (layer "F.Fab") (hide yes) (uuid "%s")'
            " (effects (font (size 0.8 0.8) (thickness 0.12))))" % (_n(r + 0.6), _u("val", ref)),
            "\t\t(attr exclude_from_pos_files exclude_from_bom)",
            "\t\t(fp_circle (center 0 0) (end %s 0) (stroke (width 0.05) (type solid)) (fill no)"
            ' (layer "F.CrtYd") (uuid "%s"))' % (_n(r), _u("crt", ref)),
            '\t\t(pad "1" thru_hole circle (at 0 0) (size %s %s) (drill %s) (layers "*.Cu" "*.Mask")'
            ' (net "%s") (uuid "%s"))' % (_n(pad), _n(pad), _n(drill), net, _u("pad", ref)),
            "\t)",
        ]
    )


def _land_fp(fp, ref, at, diameter, keepout):
    x, y = fp.k(*at)
    r = keepout / 2
    return "\n".join(
        [
            '\t(footprint "RadomeStandoffLand_%smm" (layer "F.Cu") (uuid "%s") (at %s %s 0)'
            % (_n(diameter), _u("fp", ref), _n(x), _n(y)),
            '\t\t(property "Reference" "%s" (at 0 %s 0) (layer "F.Fab") (uuid "%s")'
            " (effects (font (size 0.8 0.8) (thickness 0.12))))"
            % (ref, _n(-r - 0.6), _u("ref", ref)),
            '\t\t(property "Value" "radome standoff" (at 0 %s 0) (layer "F.Fab") (hide yes) (uuid "%s")'
            " (effects (font (size 0.8 0.8) (thickness 0.12))))" % (_n(r + 0.6), _u("val", ref)),
            "\t\t(attr smd exclude_from_pos_files exclude_from_bom)",
            "\t\t(fp_circle (center 0 0) (end %s 0) (stroke (width 0.05) (type solid)) (fill no)"
            ' (layer "F.CrtYd") (uuid "%s"))' % (_n(r), _u("crt", ref)),
            '\t\t(pad "1" smd circle (at 0 0) (size %s %s) (layers "F.Cu" "F.Mask")'
            ' (uuid "%s"))' % (_n(diameter), _n(diameter), _u("pad", ref)),
            "\t)",
        ]
    )


def _stackup_block():
    sys.path[:0] = [str(REPO)]
    from yapnr.fab import capability

    st = capability.stackup("pcbway-6l-ro4835-ro4450f")
    rows = [
        '(layer "F.SilkS" (type "Top Silk Screen"))',
        '(layer "F.Paste" (type "Top Solder Paste"))',
        '(layer "F.Mask" (type "Top Solder Mask") (thickness 0.015))',
    ]
    n = 0
    for layer in st["layers"]:
        if layer["kind"] == "copper":
            rows.append(
                '(layer "%s" (type "copper") (thickness %s))'
                % (layer["name"], _n(layer["thickness_mm"]))
            )
        elif layer["kind"] == "dielectric":
            n += 1
            er = layer.get("er") or layer.get("er_prior")
            df = layer.get("df") or layer.get("df_prior")
            rows.append(
                '(layer "dielectric %d" (type "%s") (thickness %s) (material "%s") (epsilon_r %s)'
                " (loss_tangent %s))"
                % (n, layer["type"], _n(layer["thickness_mm"]), layer["material"], _n(er), _n(df))
            )
    rows += [
        '(layer "B.Mask" (type "Bottom Solder Mask") (thickness 0.015))',
        '(layer "B.Paste" (type "Bottom Solder Paste"))',
        '(layer "B.SilkS" (type "Bottom Silk Screen"))',
        '(copper_finish "Immersion silver")',
        "(dielectric_constraints no)",
    ]
    return "\t\t(stackup\n" + "".join("\t\t\t%s\n" % r for r in rows) + "\t\t)"


def board(fp):
    d = fp.doc
    w, h, rc = fp.w, fp.h, float(d["board"]["corner_radius"])
    items = []
    # Outline with corner arcs.
    corners = [(rc, rc, 180), (w - rc, rc, 270), (w - rc, h - rc, 0), (rc, h - rc, 90)]
    edges = [
        ((rc, 0), (w - rc, 0)),
        ((w, rc), (w, h - rc)),
        ((w - rc, h), (rc, h)),
        ((0, h - rc), (0, rc)),
    ]
    for i, (a, b) in enumerate(edges):
        items.append(_gr_line(fp, a, b, "Edge.Cuts", "edge%d" % i, 0.05))
    for i, (cx, cy, a0) in enumerate(corners):
        start = (cx + rc * math.cos(math.radians(a0)), cy + rc * math.sin(math.radians(a0)))
        mid = (cx + rc * math.cos(math.radians(a0 + 45)), cy + rc * math.sin(math.radians(a0 + 45)))
        end = (cx + rc * math.cos(math.radians(a0 + 90)), cy + rc * math.sin(math.radians(a0 + 90)))
        s, m, e = fp.k(*start), fp.k(*mid), fp.k(*end)
        items.append(
            "\t(gr_arc (start %s %s) (mid %s %s) (end %s %s) (stroke (width 0.05) (type solid))"
            ' (layer "Edge.Cuts") (uuid "%s"))'
            % (_n(s[0]), _n(s[1]), _n(m[0]), _n(m[1]), _n(e[0]), _n(e[1]), _u("arc", str(i)))
        )
    # The GND planes (In1.Cu, In4.Cu), unfilled: KiCad fills them on DRC and plot.
    inset = 0.3
    for layer, net in d["board"]["planes"].items():
        pts = rect_pts([inset, inset, w - inset, h - inset])
        kp = " ".join("(xy %s %s)" % tuple(_n(c) for c in fp.k(*p)) for p in pts)
        items.append(
            '\t(zone (net "%s") (layer "%s") (uuid "%s") (name "PLANE_%s") (hatch edge 0.5)'
            " (connect_pads yes (clearance 0.2)) (min_thickness 0.2) (filled_areas_thickness no)"
            " (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3) (island_removal_mode 0))"
            " (polygon (pts %s)))" % (net, layer, _u("plane", layer), layer.split(".")[0], kp)
        )
    # Rule areas the custom rules name (no keepout flags: the .kicad_dru decides).
    for r in d["rf"]["region"]:
        items.append(
            _zone_rule_area("RF_REGION", RF_LAYERS + ["In3.Cu", "In4.Cu", "B.Cu"], rect_pts(r), fp)
        )
    items.append(_zone_rule_area("RF_POCKET", ["F.Cu"], rect_pts(d["rf"]["pocket"]), fp))
    for r in d["rf"]["guard"]:
        items.append(_zone_rule_area("RF_GUARD", COPPER, rect_pts(r), fp))
    # Mounting-hole and radome-land keepouts: no tracks or vias of any net.
    holes, lands = d["mounting_holes"], d["radome_lands"]
    for i, at in enumerate(holes["at"]):
        items.append(
            _zone_rule_area(
                "MH%d_KEEPOUT" % (i + 1),
                COPPER,
                circle_pts(at[0], at[1], holes["keepout_diameter"] / 2),
                fp,
                {"tracks": True, "vias": True},
            )
        )
    for i, at in enumerate(lands["at"]):
        items.append(
            _zone_rule_area(
                "RADOME_LAND%d_KEEPOUT" % (i + 1),
                COPPER,
                circle_pts(at[0], at[1], lands["keepout_diameter"] / 2),
                fp,
                {"tracks": True, "vias": True},
            )
        )
    # Placement regions and the patch extents, for review (User.1 / User.2).
    for name, spec in d["regions"].items():
        for i, r in enumerate(fp.areas(spec)):
            items.append(_gr_rect(fp, r, "User.1", "reg-%s-%d" % (name, i) if i else "reg-" + name))
        r = fp.rect(spec)
        items.append(_gr_text(fp, name, (r[0] + 0.3, r[3] - 0.9), "User.1", "reg-" + name, 0.6))
    for name, r in d["rf"]["patches"].items():
        items.append(_gr_rect(fp, r, "User.2", "patch-" + name))
        items.append(
            _gr_text(
                fp, "%s patches" % name.upper(), (r[0], r[3] + 0.3), "User.2", "patch-" + name, 0.6
            )
        )
    cx, cy = d["u1"]["at"]
    half = d["u1"]["body_mm"] / 2
    items.append(_gr_rect(fp, [cx - half, cy - half, cx + half, cy + half], "User.2", "u1-body"))
    items.append(
        _gr_text(fp, "U1 rot %d" % d["u1"]["rot"], (cx - half + 0.3, cy), "User.2", "u1", 0.8)
    )
    y = d["radome"]["test_radome_south_y"]
    items.append(_gr_line(fp, (0, y), (w, y), "User.2", "radome", 0.1))
    items.append(
        _gr_text(fp, "test radome covers y >= %s" % _n(y), (0.5, y + 0.4), "User.2", "radome", 0.6)
    )
    footprints = [
        _hole_fp(fp, "H%d" % (i + 1), at, holes["drill"], holes["pad"], holes["keepout_diameter"])
        for i, at in enumerate(holes["at"])
    ] + [
        _land_fp(fp, "RL%d" % (i + 1), at, lands["diameter"], lands["keepout_diameter"])
        for i, at in enumerate(lands["at"])
    ]
    head = (
        """(kicad_pcb
\t(version 20241229)
\t(generator "radar60_gen_board")
\t(generator_version "10.0")
\t(general (thickness 1.17) (legacy_teardrops no))
\t(paper "A4")
\t(title_block (title "radar60 Rev A floorplan (not routed)") (rev "A")
\t\t(comment 1 "generated by examples/radar60/board/gen_board.py from floorplan.yaml; do not edit"))
\t(layers
\t\t(0 "F.Cu" signal)
\t\t(4 "In1.Cu" power)
\t\t(6 "In2.Cu" signal)
\t\t(8 "In3.Cu" mixed)
\t\t(10 "In4.Cu" power)
\t\t(2 "B.Cu" signal)
\t\t(9 "F.Adhes" user "F.Adhesive")
\t\t(11 "B.Adhes" user "B.Adhesive")
\t\t(13 "F.Paste" user)
\t\t(15 "B.Paste" user)
\t\t(5 "F.SilkS" user "F.Silkscreen")
\t\t(7 "B.SilkS" user "B.Silkscreen")
\t\t(1 "F.Mask" user)
\t\t(3 "B.Mask" user)
\t\t(17 "Dwgs.User" user "User.Drawings")
\t\t(19 "Cmts.User" user "User.Comments")
\t\t(25 "Edge.Cuts" user)
\t\t(27 "Margin" user)
\t\t(31 "F.CrtYd" user "F.Courtyard")
\t\t(29 "B.CrtYd" user "B.Courtyard")
\t\t(35 "F.Fab" user)
\t\t(33 "B.Fab" user)
\t\t(39 "User.1" user)
\t\t(41 "User.2" user)
\t)
\t(setup
%s
\t\t(pad_to_mask_clearance 0)
\t\t(allow_soldermask_bridges_in_footprints no)
\t\t(tenting front back)
\t)
\t(net 0 "")
\t(net 1 "GND")
"""
        % _stackup_block()
    )
    return head + "\n".join(footprints + items) + "\n)\n"


# --------------------------------------------------------------------------- main


def outputs(fp):
    return {
        "constraints.yaml": constraints_text(fp),
        "radar60.kicad_pcb": board(fp),
        "radar60.kicad_pro": json.dumps(project(fp), indent=2, sort_keys=True) + "\n",
        "radar60.kicad_dru": dru(fp),
    }


def _rect_gap(a, b):
    """Shortest distance between two axis-aligned rectangles [x0, y0, x1, y1] (0 if they meet)."""
    dx = max(a[0] - b[2], b[0] - a[2], 0.0)
    dy = max(a[1] - b[3], b[1] - a[3], 0.0)
    return math.hypot(dx, dy)


def radome_report(fp):
    """The radome visibility rule (plan 5.2, R5) against the placement regions: for each part
    with a height in floorplan.yaml, the shortest distance from its region to the patch copper,
    the distance its height needs (h / tan(angle)) and the tallest part the region allows."""
    d = fp.doc
    t = math.tan(math.radians(d["radome"]["angle_deg"]))
    patches = list(d["rf"]["patches"].values())
    rows = []
    places = [(name, spec["parts"], fp.rect(spec)) for name, spec in d["regions"].items()]
    for key, role in (("lvds_header_fixed", "lvds_header"), ("jtag_fixed", "jtag")):
        fx = d.get(key)
        if fx:  # fixed: its courtyard at the pose
            (x, y), (w, h_) = fx["at"], fx["courtyard_mm"]
            places.append(("fixed", [role], [x - w / 2, y - h_ / 2, x + w / 2, y + h_ / 2]))
    for name, parts, box in places:
        for part in parts:
            h = d["radome"]["heights"].get(part)
            if h is None:
                continue
            gap = min(_rect_gap(box, p) for p in patches)
            rows.append(
                dict(
                    part=part,
                    region=name,
                    height_mm=h,
                    gap_mm=round(gap, 2),
                    need_mm=round(h / t, 2),
                    max_height_mm=round(gap * t, 2),
                    ok=gap >= h / t,
                )
            )
    return rows


def macro_check(fp):
    """Compare the floorplan with the RF macro's records: the integrated variant
    (rf.macro_record) and its D12 bracketing siblings (rfm1-m/-n/-p beside it, review
    2026-10-03). Every variant: pocket covered, every via and every F.Cu copper point inside the
    RF region, the pocket or the package body. The integrated variant's patch extents must equal
    the floorplan's; the siblings' differ by the bracketing step and are reported. Returns
    ``(lines, ok)``."""
    path = (HERE / fp.doc["rf"]["macro_record"]).resolve()
    if not path.is_file():
        return ["macro record %s not found: skipped" % fp.doc["rf"]["macro_record"]], True
    out, ok = [], True
    stem = path.parent.name  # rfm1-n
    for tag in ("m", "n", "p"):
        sib = path.parent.parent / (stem[:-1] + tag) / (stem[:-1] + tag + ".json")
        if not sib.is_file():
            out.append("variant %s: record %s missing" % (tag, sib.name))
            ok = False
            continue
        lines, good = _macro_check_one(fp, sib, exact=(sib == path))
        out += ["%s: %s" % (sib.parent.name, line) for line in lines]
        ok &= good
    return out, ok


def _macro_check_one(fp, path, exact):
    rec = json.loads(path.read_text())
    cx, cy = fp.doc["u1"]["at"]
    out, ok = [], True
    pk = rec["ports"]["vout_pa_pocket"]["rect"]
    pocket = [pk[0] + cx, pk[1] + cy, pk[2] + cx, pk[3] + cy]
    mine = fp.doc["rf"]["pocket"]
    inside = (
        mine[0] <= pocket[0] + 1e-3 and mine[2] >= pocket[2] - 1e-3 and mine[3] >= pocket[3] - 1e-3
    )
    ok &= inside
    out.append(
        "pocket: macro %s, floorplan %s: %s" % (pocket, mine, "covered" if inside else "DIFFERS")
    )
    w, length = rec["dims"]["patch"]["w"], rec["dims"]["patch"]["l"]
    half_col = rec["params"]["spacing"] / 2 + length / 2
    for bank in ("rx", "tx"):
        cols = [c["phase_centre"] for n, c in rec["columns"].items() if n.lower().startswith(bank)]
        box = [
            round(min(c[0] for c in cols) - w / 2 + cx, 3),
            round(min(c[1] for c in cols) - half_col + cy, 3),
            round(max(c[0] for c in cols) + w / 2 + cx, 3),
            round(max(c[1] for c in cols) + half_col + cy, 3),
        ]
        delta = max(abs(a - b) for a, b in zip(box, fp.doc["rf"]["patches"][bank]))
        same = delta < 0.01
        if exact:
            ok &= same
        out.append(
            "%s patches: macro %s: %s"
            % (
                bank,
                box,
                "same" if same else "differs by %.3f mm%s" % (delta, "" if exact else " (D12)"),
            )
        )
    regions = fp.doc["rf"]["region"] + [fp.doc["rf"]["pocket"]]
    half = fp.doc["u1"]["body_mm"] / 2
    regions.append([cx - half, cy - half, cx + half, cy + half])
    board = (path.parent / rec["files"]["pcb"]).read_text()
    import re

    m = re.search(r'\(footprint "[^"]*ABL0161[^"]*".*?\(at ([-\d.]+) ([-\d.]+)', board, re.S)
    ux, uy = (float(m.group(1)), float(m.group(2))) if m else (0.0, 0.0)
    vias = [
        (float(a) - ux + cx, -(float(b) - uy) + cy)
        for a, b in re.findall(r"\(via \(at ([-\d.]+) ([-\d.]+)\)", board)
    ]
    outside = [
        (round(x, 2), round(y, 2))
        for x, y in vias
        if not any(r[0] <= x <= r[2] and r[1] <= y <= r[3] for r in regions)
    ]
    ok &= not outside
    out.append(
        "macro vias outside the RF region and the package: %d of %d %s"
        % (len(outside), len(vias), outside[:6])
    )
    # Top-layer copper: track and arc points and F.Cu zone vertices (the In1/In2 GND zones are
    # the macro's reference planes and may extend past the region; the board's In1 is GND too).
    points = [
        (float(a) - ux + cx, -(float(b) - uy) + cy)
        for line in board.splitlines()
        if line.lstrip().startswith(("(segment", "(arc"))
        or (line.lstrip().startswith("(zone") and '"F.Cu"' in line and "(net " in line)
        for a, b in re.findall(r"\((?:start|mid|end|xy) ([-\d.]+) ([-\d.]+)\)", line)
    ]
    eps = 1e-3
    outside = [
        (round(x, 2), round(y, 2))
        for x, y in points
        if not any(r[0] - eps <= x <= r[2] + eps and r[1] - eps <= y <= r[3] + eps for r in regions)
    ]
    ok &= not outside
    out.append(
        "macro F.Cu copper points outside the RF region and the package: %d of %d %s"
        % (len(outside), len(points), outside[:6])
    )
    return out, ok


def compile_check(fp, engine=None):
    """Compile constraints.yaml with yapnr's constraint compiler against a stand-in netlist
    (one part per instance path of floorplan.yaml). ``engine``: a
    checkout whose hardware/pnr to use (default: this one). Returns the compiler's warnings."""
    root = Path(engine) if engine else REPO
    sys.path.insert(0, str(root / "hardware/pnr"))
    for name in [m for m in sys.modules if m == "pnr" or m.startswith("pnr.")]:
        del sys.modules[name]
    from pnr.constraints import compile_constraints

    doc = constraints(fp)
    addresses, refs = {}, []
    for role, paths in sorted(fp.doc["parts"].items()):
        for path in [paths] if isinstance(paths, str) else paths:
            ref = "U1" if role == "radio" else "P%d" % len(refs)
            addresses[path.replace("*", "x")] = ref
            refs.append(ref)
    compiled = compile_constraints(doc, refs, addresses=addresses)
    kinds = sorted({c.kind for c in compiled.constraints})
    return compiled.warnings, kinds, compiled


def _same(name, a, b):
    if name.endswith(".yaml"):
        return yaml.safe_load(a) == yaml.safe_load(b)
    if name.endswith(".kicad_pro"):
        return json.loads(a) == json.loads(b)
    return a == b


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="fail if a generated file is stale")
    ap.add_argument("--out", type=Path, default=HERE)
    ap.add_argument("--radome", action="store_true", help="print the radome visibility check")
    ap.add_argument("--macro", action="store_true", help="compare with the RF macro's record")
    ap.add_argument(
        "--compile",
        nargs="?",
        const="",
        metavar="CHECKOUT",
        help="also compile constraints.yaml with yapnr's compiler (optionally another checkout's)",
    )
    args = ap.parse_args(argv)
    fp = load()
    stale = []
    for name, text in outputs(fp).items():
        path = args.out / name
        if args.check:
            if not path.is_file() or not _same(name, path.read_text(), text):
                stale.append(name)
        else:
            path.write_text(text)
            print(path.relative_to(args.out.parent) if args.out == HERE else path)
    if args.radome:
        for row in radome_report(fp):
            print(
                "radome: %(part)-15s region %(region)-15s h %(height_mm)4.2f mm, needs %(need_mm)5.2f,"
                " has %(gap_mm)5.2f (max h %(max_height_mm)4.2f) %(ok)s" % row
            )
    if args.macro:
        lines, ok = macro_check(fp)
        for line in lines:
            print("macro: " + line)
        if not ok:
            stale.append("floorplan.yaml (differs from the RF macro)")
    if args.compile is not None:
        warnings, kinds, _ = compile_check(fp, args.compile or None)
        print("compiled constraint kinds: " + ", ".join(kinds))
        for w in warnings:
            print("warning: " + w)
    if stale:
        print("stale: " + ", ".join(stale) + " (run gen_board.py)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
