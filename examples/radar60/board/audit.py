"""Placement audits for the radar60 Rev A integration (plan 7.5 R2, R4, R5 and the noise
distances, as far as they can be judged before routing), on yapnr's placement candidates and on
the final KiCad board. Board frame: mm, origin at the lower-left corner, +y north.

R4 and R5 have no engine constraint yet (yapnr's proposed ``noise_keepout`` and
``height_limit``): ``digital_region`` turns R4 into a placement region for the parts with a
guard-restricted pad, and the audit checks both on every candidate; selection only takes
candidates that pass. A net is guard-restricted when it is routed (two or more pads) and its net
class (floorplan ``nets``) is none of RF, PWR, GND and ANALOG: the same classes the board's
custom rule ``radar60_rf_guard_digital`` exempts (review 2026-10-03; before, a name list here
called some Default-class nets analog that the rule forbids).

``pin_distances`` measures, for the parts the review named, the distance from each of their
non-ground pads to the nearest U1 ball (or J1 pin) on the same net, and the order of the
protection parts along J1's lines.
"""

from __future__ import annotations

import fnmatch
import math

OFFSET = 30.0  # KiCad page offset of the board origin (pnr.writeback._PAGE_OFFSET_MM)
TAN30 = math.tan(math.radians(30.0))

# R4 exempts these net classes (gen_board.R4_EXEMPT, radar60_rf_guard_digital).
R4_EXEMPT = ("RF", "PWR", "GND", "ANALOG")

# Named digital nets, for reports only (R4 itself is class-based, see guard_restricted).
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
    ("R_0201", 0.30),  # the RF macro's dummy loads RT1-RT4
    ("C_0603", 0.95),
    ("LED_0603", 0.80),
    ("C_0805", 1.45),
    ("L_0805", 1.00),
    ("L_Vishay_IHLP-1616", 2.00),
    ("Vishay_WFCP0612", 1.00),  # the 1.0 V shunt: Vishay 30417, t 0.75 +/- 0.25 mm
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


def net_class(net, floorplan):
    """The net's class from floorplan ``nets`` (globs, LVDS pairs), or 'Default'."""
    for name, spec in floorplan["nets"].items():
        pats = (
            [n for pair in spec["pairs"].values() for n in pair]
            if "pairs" in spec
            else spec["globs"]
        )
        if any(fnmatch.fnmatchcase(net or "", q) for q in pats):
            return name
    return "Default"


def pad_counts(parts):
    out = {}
    for p in parts:
        for net, _ in p["pads"]:
            if net:
                out[net] = out.get(net, 0) + 1
    return out


def guard_restricted(net, floorplan, counts):
    """R4: a routed net (two or more pads) outside the RF, PWR, GND and ANALOG classes."""
    return bool(net) and counts.get(net, 0) >= 2 and net_class(net, floorplan) not in R4_EXEMPT


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
                pad_at={p["name"]: at(*p["offset"]) for p in c["pads"] if p.get("name")},
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
                pad_at={q["number"]: b(*q["at"]) for q in p["pads"] if q["number"]},
                at=b(*p["at"]),
                rot=p["rot"],
                dnp=p["dnp"],
                group=p.get("group"),
            )
        )
    return out


# ---------------------------------------------------------------- checks


def checks(parts, floorplan, footprint_of=None):
    d = floorplan
    rf, guard = d["rf"]["region"], d["rf"]["guard"]
    patches = list(d["rf"]["patches"].values())
    # U1 and the RF macro's own footprints (RFM1 and its dummy loads, the group RFM1_MACRO)
    exempt = {"U1", "RFM1"} | {
        p["ref"] for p in parts if p.get("group") == d["rf"].get("block", {}).get("group")
    }
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
    # R4: guard-restricted pads (class-based, as the custom rule) in the guard band or the RF
    # region.
    counts = pad_counts(parts)
    bad = []
    for p in parts:
        if p["ref"] in exempt:
            continue
        for net, xy in p["pads"]:
            if guard_restricted(net, floorplan, counts) and any(inside(xy, r) for r in guard + rf):
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
    res["pin_distance"] = pin_distances(parts, floorplan)
    return res


# Pad-to-ball limits (review 2026-10-03) [D]: the radio's internal-LDO outputs, VBGAP, the
# crystal load caps, the VOUT_PA corner parts and the QSPI clock resistor are failures beyond
# these; the rest is reported (decoupling: limit; bulk: the PA/RF2 bulk sits on the bottom side
# east of the package, the nearest area outside the BGA shadow, see floorplan pa_bulk).
PIN_LIMITS = [
    # (role, limit mm, hard)
    # pa_decoupling split (stage 3c R7): the two 220 nF caps still sit in their tight corner
    # slots (pa_cap, rf2_cap; floorplan regions), so their 3 mm hard limit is unchanged. R46
    # (pa_r) does not: the owner's Q1 default (WFCP0612, ~3.8 x 4.4 mm) does not fit inside 3 mm
    # of C2/D2 at all (nor anywhere in the BGA shadow -- see floorplan `groups.r46`), so its
    # limit widens to the group's own radius and is reported, not hard, pending the R2
    # power-chain track's re-derived 1V0_RF2 -> 1V0_PA resistance against the 4 mOhm budget.
    ("pa_cap", 3.0, True),
    ("rf2_cap", 3.0, True),
    ("pa_r", 11.0, False),
    ("crystal_caps", 3.0, True),
    # stage 3b: west of the R12 neighbours' exit bands (floorplan qspi_series), about 4.5 mm
    ("qspi_series", 5.0, True),
    ("radio_east", 3.5, True),
    ("radio_west", 8.0, False),
    ("pa_bulk", 8.0, False),
    ("radio_decoupling_hf", 7.0, False),
    ("radio_decoupling_bulk", 12.0, False),
]
# B10/B13 are one ball in from the edge: with U1's 0.5 mm courtyard and the 0.25 mm grid the
# nearest slot puts the VBGAP and SYNTH pads 3.1-3.4 mm from their balls (floorplan regions)
LDO_PARTS = ("radio.c_apll", "radio.c_synth", "radio.c_vbgap")  # 3.5 mm, hard


def _role_parts(parts, floorplan, role):
    pats = floorplan["parts"][role]
    pats = pats if isinstance(pats, list) else [pats]
    return [
        p for p in parts if p["address"] and any(fnmatch.fnmatchcase(p["address"], q) for q in pats)
    ]


def pin_distances(parts, floorplan):
    """Pad-to-ball distances of the pin-anchored parts and the protection order at J1."""
    by_ref = {p["ref"]: p for p in parts}
    u1 = by_ref.get("U1")
    out = {"parts": {}, "failures": [], "protection": {}}
    if u1 is None:
        return out
    balls = {}
    for net, xy in u1["pads"]:
        balls.setdefault(net, []).append(xy)
    seen = set()
    for role, limit, hard in PIN_LIMITS:
        for p in _role_parts(parts, floorplan, role):
            if p["ref"] in seen:
                continue
            seen.add(p["ref"])
            # each non-ground pad to the nearest ball of its own net; a part's distance is its
            # worst pad (a series part such as the PA 0 ohm has a ball net on both pads)
            ds = [
                min(math.dist(xy, b) for b in balls[net])
                for net, xy in p["pads"]
                if net and net != "GND" and net in balls
            ]
            if not ds:
                continue
            d = round(max(ds), 2)
            lim = 3.5 if p["address"] in LDO_PARTS else limit
            out["parts"][p["address"]] = {"ref": p["ref"], "mm": d, "limit_mm": lim, "role": role}
            if d > lim + 1e-6 and (hard or p["address"] in LDO_PARTS):
                out["failures"].append("%s (%s) %.2f mm > %.1f" % (p["ref"], p["address"], d, lim))
    # Protection at J1: on every line from J1, the ESD/TVS pad nearest J1's pin must be nearer than
    # any IC's pad on that line (review High 1: the CAN ESD sat behind the transceiver).
    j1 = next((p for p in parts if p["address"] == floorplan["parts"]["connector"]), None)
    prot = set(
        p["ref"]
        for r in ("connector_esd", "connector_tvs")
        if r in floorplan["parts"]
        for p in _role_parts(parts, floorplan, r)
    )
    if j1:
        for net, pin in j1["pads"]:
            if not net or net == "GND":
                continue
            others = [
                (math.dist(pin, xy), q["ref"])
                for q in parts
                if q is not j1
                for n, xy in q["pads"]
                if n == net
            ]
            if not others:
                continue
            others.sort()
            esd = [o for o in others if o[1] in prot]
            ics = [o for o in others if o[1].startswith("U")]
            row = {"nearest": others[0][1], "nearest_mm": round(others[0][0], 2)}
            if esd:
                row.update(esd=esd[0][1], esd_mm=round(esd[0][0], 2))
            if ics:
                row.update(ic=ics[0][1], ic_mm=round(ics[0][0], 2))
            out["protection"][net] = row
            if esd and ics and ics[0][0] < esd[0][0]:
                out["failures"].append(
                    "J1 %s: %s (%.1f mm) before the protection %s (%.1f mm)"
                    % (net, ics[0][1], ics[0][0], esd[0][1], esd[0][0])
                )
    return out


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
    out += ["pin distance: " + f for f in res["pin_distance"]["failures"]]
    return out


def placement_audit(graph, poses, floorplan):
    parts = parts_from_engine(graph, poses)
    res = checks(parts, floorplan)
    fail = failures(res, floorplan)
    return {"pass": not fail, "failures": fail, "checks": res}


# ---------------------------------------------------------------- final board


def _constraint_checks(parts, floorplan, sited=()):
    """The hard placement constraints again, on the written board. ``sited``: the addresses
    of the parts the fanout fixed at bottom sites (integrate.py prepare took them out of their
    floorplan regions)."""
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
        members = [p for r in spec["parts"] for p in role(r) if p["address"] not in sited]
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
        # a pad-anchored group measures from that pad of the anchor (yapnr anchor_pad)
        centre = (
            anchor["pad_at"][str(spec["anchor_pad"])] if spec.get("anchor_pad") else anchor["at"]
        )
        members = [p for r in spec["members"] for p in role(r)]
        far = {
            p["ref"]: round(math.dist(p["at"], centre), 2)
            for p in members
            if math.dist(p["at"], centre) > spec["radius_mm"] + 1e-3
        }
        worst = max((math.dist(p["at"], centre) for p in members), default=0.0)
        out["groups"][name] = {
            "members": len(members),
            "radius_mm": spec["radius_mm"],
            "anchor_pad": spec.get("anchor_pad"),
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


def fanout_checks(parts, fanout, macro_group=None):
    """The fanout inputs on the placed board (integrate.py prepare's plan): no part's courtyard
    in an exit band (any side; U1 and the RF macro's footprints, which are copper and a mask
    opening rather than placed parts, aside), and every bottom-site part at its site on the
    bottom side."""
    out = {"bands": {}, "sites": {}}
    if not fanout:
        return out
    placed = [
        p for p in parts if p["ref"] != "U1" and not (macro_group and p.get("group") == macro_group)
    ]
    for band in fanout.get("bands") or []:
        inside = sorted(p["ref"] for p in placed if overlap(p["box"], band["rect"]))
        out["bands"][band["name"]] = {"balls": band["balls"], "rect": band["rect"], "parts": inside}
    by_address = {p["address"]: p for p in parts if p["address"]}
    for ref, site in (fanout.get("bottom_sites") or {}).items():
        p = by_address.get(site.get("address"))
        if p is None:
            out["sites"][ref] = {"missing": True}
            continue
        err = math.dist(p["at"], site["at"])
        out["sites"][ref] = {
            "board_ref": p["ref"],
            "side": p["side"],
            "error_mm": round(err, 4),
            "ok": p["side"] == "bottom" and err < 1e-3,
        }
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


def board_audit(
    poses, drc, floorplan, macro_record, finish, selection, rf_audit=None, fanout=None, prepare=None
):
    h = floorplan["board"]["height"]
    parts = parts_from_board(poses, h)
    res = checks(parts, floorplan)
    fail = failures(res, floorplan)
    sited = {s.get("address") for s in ((fanout or {}).get("bottom_sites") or {}).values()}
    cons = _constraint_checks(parts, floorplan, sited)
    macro = _macro_checks(parts, macro_record, finish)
    fan = fanout_checks(parts, fanout, floorplan["rf"].get("block", {}).get("group"))
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
        "pin_distance_failures": res["pin_distance"]["failures"],
        "protection_at_j1": res["pin_distance"]["protection"],
        "rf_ball_vs_macro_worst_um": macro["worst_um"],
        "r1_macro_digest_v2_equal": (finish.get("r1_macro_copper") or {}).get("equal"),
        "macro_loads": (finish.get("macro") or {}).get("loads"),
        "rf_audit": (
            {k: v.get("ok") for k, v in rf_audit.items() if k.startswith("A")} if rf_audit else None
        ),
        "rf_audit_ok": rf_audit.get("ok") if rf_audit else None,
        "exit_bands_clear": all(not v["parts"] for v in fan["bands"].values()),
        "exit_bands": len(fan["bands"]),
        "bottom_sites_ok": all(v.get("ok") for v in fan["sites"].values()),
        "bottom_sites": {k: v.get("board_ref") for k, v in fan["sites"].items()},
        "fanout_plan": (prepare or {}).get("fanout", {}).get("diagnostics"),
        "macro_variant": finish["macro"].get("variant"),
        "macro_status": macro_record.get("status"),
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
        "fanout": fan,
        "rf_audit": rf_audit,
        "drc": drc_s,
        "finish": finish,
    }
