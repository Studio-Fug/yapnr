"""Placement audits for the radar60 Rev A integration (plan 7.5 R2, R4, R5 and the noise
distances, as far as they can be judged before routing), on yapnr's placement candidates and on
the final KiCad board. Board frame: mm, origin at the lower-left corner, +y north.

R4 and R5 have no engine constraint yet (yapnr's proposed ``noise_keepout`` and
``height_limit``): ``digital_region`` turns R4 into a placement region for the parts with a
digital pad, and the audit checks both on every candidate; selection only takes candidates that
pass.
"""

from __future__ import annotations

import fnmatch
import math

OFFSET = 30.0  # KiCad page offset of the board origin (pnr.writeback._PAGE_OFFSET_MM)
TAN30 = math.tan(math.radians(30.0))

# Digital nets for R4 (no digital copper within 5 mm of RF copper on F.Cu-In2.Cu). Supply,
# ground, RF, crystal, the radio's internal LDO and band-gap capacitor nets, the switch nodes
# and the unnamed passive-to-passive nets are not digital [D].
DIGITAL = [
    "UART_*",
    "SPIA_*",
    "SPI_*",
    "CAN_*",
    "CANH",
    "CANL",
    "I2C_*",
    "JTAG_*",
    "QSPI_*",
    "LVDS_*",
    "GPIO_*",
    "NRESET",
    "NERROR_*",
    "WARM_RESET",
    "SOP*",
    "SYNC_IN",
    "FRAME_START",
    "PMIC_NINT",
    "PMIC_PGOOD",
    "EFUSE_PG",
    "DCA_RST_N",
    "OSC_CLKOUT",
    "DMM_SYNC",
    "STB",
    "led_*",
]

# Part heights for R5 [E]: maximum package heights from typical data sheets, by footprint name.
HEIGHTS = [
    ("C_0402", 0.55),
    ("R_0402", 0.40),
    ("C_0603", 0.95),
    ("LED_0603", 0.80),
    ("C_0805", 1.45),
    ("L_0805", 1.00),
    ("R_0612", 0.65),
    ("SOT-23-6", 1.45),
    ("SOT-23", 1.12),
    ("SOT-323", 1.10),
    ("D_SMF", 1.08),
    ("Crystal_SMD_3225", 0.80),
    ("WSON-8", 0.80),
    ("VQFN", 1.00),
    ("DRB0008A", 1.00),
    ("FCBGA", 1.20),
    ("JST_GH", 4.25),
    ("QTH-030", 4.30),
    ("PinHeader_2x05_P1.27mm", 5.00),
    ("TestPoint", 0.05),
    ("KelvinSense", 0.05),
    ("Fiducial", 0.0),
    ("MountingHole", 0.0),
    ("RadomeStandoff", 0.0),
]


def is_digital(net):
    return bool(net) and any(fnmatch.fnmatchcase(net, p) for p in DIGITAL)


def height(footprint):
    for key, h in HEIGHTS:
        if key in (footprint or ""):
            return h
    return None


def rect_gap(a, b):
    dx = max(a[0] - b[2], b[0] - a[2], 0.0)
    dy = max(a[1] - b[3], b[1] - a[3], 0.0)
    return math.hypot(dx, dy)


def inside(p, r, eps=1e-6):
    return r[0] - eps <= p[0] <= r[2] + eps and r[1] - eps <= p[1] <= r[3] + eps


def overlap(a, b, eps=1e-6):
    return a[0] < b[2] - eps and b[0] < a[2] - eps and a[1] < b[3] - eps and b[1] < a[3] - eps


def subtract(base, holes):
    """``base`` minus the union of ``holes``, as disjoint rectangles (vertical slabs, merged)."""
    xs = sorted(
        {base[0], base[2]} | {v for h in holes for v in (h[0], h[2]) if base[0] < v < base[2]}
    )
    out = []
    for x0, x1 in zip(xs, xs[1:]):
        cut = sorted((h[1], h[3]) for h in holes if h[0] <= x0 and h[2] >= x1)
        y = base[1]
        for y0, y1 in cut:
            if y0 > y:
                out.append([x0, y, x1, min(y0, base[3])])
            y = max(y, y1)
        if y < base[3]:
            out.append([x0, y, x1, base[3]])
    merged = []
    for r in out:  # join slabs with the same y-span that touch
        for m in merged:
            if abs(m[1] - r[1]) < 1e-9 and abs(m[3] - r[3]) < 1e-9 and abs(m[2] - r[0]) < 1e-9:
                m[2] = r[2]
                break
        else:
            merged.append(list(r))
    return [[round(v, 4) for v in r] for r in merged if r[2] - r[0] > 1e-6 and r[3] - r[1] > 1e-6]


def digital_region(floorplan):
    """R4 as a placement region: the board minus the RF region and its guard band."""
    d = floorplan
    board = [0.0, 0.0, d["board"]["width"], d["board"]["height"]]
    return subtract(board, d["rf"]["region"] + d["rf"]["guard"] + [d["rf"]["pocket"]])


# ---------------------------------------------------------------- part models


def parts_from_engine(graph, poses):
    """Courtyard rectangles and pad points of an engine placement (``poses``: ref -> [x, y,
    rot, side], the halving record's), from the ingested graph."""
    out = []
    for c in graph["components"]:
        if c["ref"] not in poses:
            continue
        x, y, rot, side = poses[c["ref"]]
        a = math.radians(rot)
        co, si = math.cos(a), math.sin(a)

        def at(dx, dy, x=x, y=y, co=co, si=si, side=side):
            if side == "bottom":
                dy = -dy
            return (x + co * dx - si * dy, y + si * dx + co * dy)

        w, h = c["courtyard"]
        corners = [at(sx * w / 2, sy * h / 2) for sx in (-1, 1) for sy in (-1, 1)]
        box = [
            min(p[0] for p in corners),
            min(p[1] for p in corners),
            max(p[0] for p in corners),
            max(p[1] for p in corners),
        ]
        pads = [(p["net"], at(*p["offset"])) for p in c["pads"]]
        out.append(
            dict(
                ref=c["ref"],
                address=c.get("address") or "",
                footprint=c.get("footprint") or "",
                side=side,
                box=box,
                pads=pads,
                at=(x, y),
                rot=rot,
            )
        )
    return out


def parts_from_board(poses, height_mm):
    """The same from ``kicad_ops.py poses`` (KiCad coordinates)."""

    def b(x, y):
        return (x - OFFSET, OFFSET + height_mm - y)

    out = []
    for p in poses["parts"]:
        x0, y0 = b(p["courtyard"][0], p["courtyard"][3])
        x1, y1 = b(p["courtyard"][2], p["courtyard"][1])
        out.append(
            dict(
                ref=p["ref"],
                address=p["address"],
                footprint=p["fpid"],
                side=p["side"],
                box=[x0, y0, x1, y1],
                pads=[(q["net"], b(*q["at"])) for q in p["pads"]],
                at=b(*p["at"]),
                rot=p["rot"],
                dnp=p["dnp"],
            )
        )
    return out


# ---------------------------------------------------------------- checks


def checks(parts, floorplan, footprint_of=None):
    d = floorplan
    rf, guard = d["rf"]["region"], d["rf"]["guard"]
    patches = list(d["rf"]["patches"].values())
    exempt = {"U1", "RFM1"}
    res = {}
    # R2: no part in the RF region (top side), the pocket aside; U1 and the macro are its own.
    res["r2_parts_in_rf_region"] = sorted(
        p["ref"]
        for p in parts
        if p["ref"] not in exempt and p["side"] == "top" and any(overlap(p["box"], r) for r in rf)
    )
    res["r2_bottom_parts_under_rf_region"] = sorted(
        p["ref"] for p in parts if p["side"] == "bottom" and any(overlap(p["box"], r) for r in rf)
    )
    # R4: digital pads (top-side copper) in the guard band or the RF region.
    bad = []
    for p in parts:
        if p["ref"] in exempt:
            continue
        for net, xy in p["pads"]:
            if is_digital(net) and any(inside(xy, r) for r in guard + rf):
                bad.append("%s:%s" % (p["ref"], net))
    res["r4_digital_pads_in_guard"] = sorted(set(bad))
    # R5: radome visibility, every part with a known height.
    r5, unknown = [], []
    for p in parts:
        fpn = footprint_of(p) if footprint_of else p["footprint"]
        h = height(fpn)
        if h is None:
            unknown.append(p["ref"])
            continue
        if p["ref"] in exempt or h <= 0:
            continue
        gap = min(rect_gap(p["box"], q) for q in patches)
        if gap < h / TAN30 - 1e-6:
            r5.append(
                {
                    "ref": p["ref"],
                    "height_mm": h,
                    "gap_mm": round(gap, 2),
                    "need_mm": round(h / TAN30, 2),
                }
            )
    res["r5_radome_violations"] = r5
    res["r5_unknown_heights"] = sorted(unknown)
    # Noise (plan 3.4, R4): switch-node pads >= 8 mm from the crystal pads, >= 5 mm from RF.
    sw = [
        xy for p in parts for net, xy in p["pads"] if fnmatch.fnmatchcase(net or "", "PMIC_SW_B*")
    ]
    xt = [
        xy
        for p in parts
        for net, xy in p["pads"]
        if fnmatch.fnmatchcase(net or "", "XTAL_*") and p["ref"] not in exempt
    ]
    res["sw_to_xtal_mm"] = round(
        min((math.dist(a, b) for a in sw for b in xt), default=math.inf), 2
    )
    res["sw_to_rf_region_mm"] = round(
        min((rect_gap([x, y, x, y], r) for x, y in sw for r in rf), default=math.inf), 2
    )
    # Sides: only the declared bottom-side parts are on the bottom.
    declared = {
        r for spec in d["regions"].values() if spec.get("side") == "bottom" for r in spec["parts"]
    }
    res["bottom_side_parts"] = sorted(p["ref"] for p in parts if p["side"] == "bottom")
    res["declared_bottom_roles"] = sorted(declared)
    return res


def failures(res, floorplan):
    out = []
    if res["r2_parts_in_rf_region"]:
        out.append("R2 parts in the RF region: %s" % res["r2_parts_in_rf_region"])
    if res["r4_digital_pads_in_guard"]:
        out.append("R4 digital pads in the guard band: %s" % res["r4_digital_pads_in_guard"][:8])
    if res["r5_radome_violations"]:
        out.append("R5 radome: %s" % [v["ref"] for v in res["r5_radome_violations"]])
    if res["sw_to_xtal_mm"] < floorplan["noise"]["sw_to_crystal_mm"]:
        out.append("switch node %.2f mm from the crystal" % res["sw_to_xtal_mm"])
    if res["sw_to_rf_region_mm"] < floorplan["noise"]["sw_to_rf_mm"]:
        out.append("switch node %.2f mm from the RF region" % res["sw_to_rf_region_mm"])
    return out


def placement_audit(graph, poses, floorplan):
    parts = parts_from_engine(graph, poses)
    res = checks(parts, floorplan)
    fail = failures(res, floorplan)
    return {"pass": not fail, "failures": fail, "checks": res}


# ---------------------------------------------------------------- final board


def _constraint_checks(parts, floorplan):
    """The hard placement constraints again, on the written board."""
    d = floorplan
    by_addr = {p["address"]: p for p in parts if p["address"]}
    by_ref = {p["ref"]: p for p in parts}

    def role(r):
        pats = d["parts"][r]
        pats = pats if isinstance(pats, list) else [pats]
        return [
            p for a, p in sorted(by_addr.items()) if any(fnmatch.fnmatchcase(a, q) for q in pats)
        ]

    out = {"regions": {}, "groups": {}, "fixed": {}}
    for name, spec in d["regions"].items():
        rects = spec.get("areas") or [
            d["rf"]["pocket"] if spec["rect"] == "rf.pocket" else spec["rect"]
        ]
        members = [p for r in spec["parts"] for p in role(r)]
        outside = [
            p["ref"]
            for p in members
            if not any(
                inside(p["box"][:2], q, 1e-3) and inside(p["box"][2:], q, 1e-3) for q in rects
            )
        ]
        wrong_side = [p["ref"] for p in members if spec.get("side") and p["side"] != spec["side"]]
        out["regions"][name] = {
            "members": len(members),
            "outside": outside,
            "wrong_side": wrong_side,
        }
    pending = {"pending_" + k: v for k, v in (d.get("blocks_pending") or {}).items()}
    for name, spec in list(d["groups"].items()) + list(pending.items()):
        anchor = role(spec["anchor"])[0]
        members = [p for r in spec["members"] for p in role(r)]
        far = {
            p["ref"]: round(math.dist(p["at"], anchor["at"]), 2)
            for p in members
            if math.dist(p["at"], anchor["at"]) > spec["radius_mm"] + 1e-3
        }
        worst = max((math.dist(p["at"], anchor["at"]) for p in members), default=0.0)
        out["groups"][name] = {
            "members": len(members),
            "radius_mm": spec["radius_mm"],
            "max_distance_mm": round(worst, 2),
            "beyond": far,
        }
    u1 = by_ref.get("U1")
    out["fixed"]["U1"] = {"at": [round(v, 4) for v in u1["at"]], "rot": u1["rot"]}
    for i, at in enumerate(d["mounting_holes"]["at"]):
        p = by_addr.get(d["parts"]["holes"][i])
        out["fixed"][p["ref"]] = round(math.dist(p["at"], at), 4)
    j1 = role("connector")[0]
    out["fixed"]["J1"] = {"x": round(j1["at"][0], 3), "courtyard_y0": round(j1["box"][1], 3)}
    return out


def _macro_checks(parts, macro_record, finish):
    """R6 geometry: the U1 RF balls where the macro's ports start, the phase centres."""
    u1 = next(p for p in parts if p["ref"] == "U1")
    pads = {}
    for net, xy in u1["pads"]:
        if net.startswith("RF_"):
            pads[net] = xy
    cx, cy = macro_record["board_frame"]["u1_at"]
    worst = 0.0
    rows = {}
    for port, spec in macro_record["ports"].items():
        if not port.endswith(".Pb"):
            continue
        chain = port.split(".")[0]
        want = (spec["at"][0] + cx, spec["at"][1] + cy)
        got = pads.get("RF_" + chain)
        err = math.dist(want, got) if got else math.inf
        worst = max(worst, err)
        rows[chain] = round(err * 1000, 3)
    rfm1 = next((p for p in parts if p["ref"] == "RFM1"), None)
    return {
        "rf_ball_vs_macro_port_um": rows,
        "worst_um": round(worst * 1000, 3),
        "rfm1_at": [round(v, 4) for v in rfm1["at"]] if rfm1 else None,
        "u1_at": [round(v, 4) for v in u1["at"]],
        "macro_frame_u1_at": [cx, cy],
        "geometry_sha256": finish["macro"]["geometry_sha256"],
        "record_sha256": macro_record["geometry_sha256"],
    }


def _drc_summary(drc):
    by_type, items = {}, {}
    for v in drc.get("violations", []):
        t = v.get("type")
        by_type[t] = by_type.get(t, 0) + 1
        items.setdefault(t, []).append(
            {
                "severity": v.get("severity"),
                "description": v.get("description"),
                "items": [i.get("description") for i in v.get("items", [])][:2],
            }
        )
    return {
        "violations": sum(by_type.values()),
        "by_type": dict(sorted(by_type.items(), key=lambda kv: -kv[1])),
        "unconnected_items": len(drc.get("unconnected_items", [])),
        "schematic_parity": len(drc.get("schematic_parity", [])),
        "examples": {t: v[:6] for t, v in items.items()},
    }


def board_audit(poses, drc, floorplan, macro_record, finish, selection):
    h = floorplan["board"]["height"]
    parts = parts_from_board(poses, h)
    res = checks(parts, floorplan)
    fail = failures(res, floorplan)
    cons = _constraint_checks(parts, floorplan)
    macro = _macro_checks(parts, macro_record, finish)
    drc_s = _drc_summary(drc)
    placement_types = {
        "courtyards_overlap",
        "items_not_allowed",
        "footprint",
        "malformed_courtyard",
        "missing_courtyard",
        "pth_inside_courtyard",
        "npth_inside_courtyard",
    }
    summary = {
        "parts": len(parts),
        "bottom_side": res["bottom_side_parts"],
        "winner": selection["winner"],
        "starts": selection["starts"],
        "legal": selection["legal"],
        "audit_pass_candidates": selection["audit_pass"],
        "audit_failures": fail,
        "regions_ok": all(
            not v["outside"] and not v["wrong_side"] for v in cons["regions"].values()
        ),
        "groups_ok": all(
            not v["beyond"] for k, v in cons["groups"].items() if not k.startswith("pending_")
        ),
        "pending_block_spread_mm": {
            k[len("pending_") :]: v["max_distance_mm"]
            for k, v in cons["groups"].items()
            if k.startswith("pending_")
        },
        "rf_ball_vs_macro_worst_um": macro["worst_um"],
        "drc_violations": drc_s["violations"],
        "drc_by_type": drc_s["by_type"],
        "drc_placement_types": {t: n for t, n in drc_s["by_type"].items() if t in placement_types},
        "unconnected_items": drc_s["unconnected_items"],
    }
    return {
        "summary": summary,
        "checks": res,
        "constraints": cons,
        "macro": macro,
        "drc": drc_s,
        "finish": finish,
    }
