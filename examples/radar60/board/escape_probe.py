"""BGA escape feasibility probe for U1 (IWR6843 FCBGA-161) on the radar60 stackup.

A synthetic board holds only U1 at its Rev A pose (moved by less than half a routing cell so
the balls and the channels between them lie on cell centres) and its fanout targets: one 0.3 mm
pad per escaping net, in line with its ball on the nearest side the escape may use (west or
south for signals; east too, below the TX region, for the band-gap and LDO capacitors), one
target row per ball row of depth (1.1 mm beyond the package edge, then every 1.3 mm). Reaching
its target is escaping. yapnr places nothing (every part is fixed) and routes everything
through the regression runner (a design JSON for ``hardware/pnr/regression/run.py
--design-json``) on the declared 6-layer stack: F signal, In1 GND plane, In2 signal, In3 supply
plane, In4 GND plane, B signal; through vias only; the pcbway-adv-6l-rf rules as the design's
own fab block (0.10/0.10 mm, 0.40/0.20 mm vias).

The 60 GHz balls are the RF macro's: the probe leaves them unconnected and reserves their
exits (the depopulated site in front of each, to the package edge) and the RF region beyond.
Every supply ball but the three capacitor balls is one net on the In3 plane, so a supply ball
escapes when it gets its via drop (the per-rail pours on In3 are a placement matter).

``build`` writes the design JSON and a footprint library (U1 from fcbga161.py, the target);
``analyze`` reads a finished run and reports, ball by ball, which balls escaped (their net is
complete in KiCad's DRC), on which layers, with how many vias, and which board rules of
radar60.kicad_dru their copper breaks (the probe board gets the floorplan's rule areas).

Variants: ``reva`` connects the balls the Rev A pin plan uses (plan 3.2); ``all`` connects every
signal ball (a worst case).
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from collections import Counter, defaultdict
from pathlib import Path

import fcbga161
import gen_board

HERE = Path(__file__).resolve().parent
LIB = "Radar60Probe"
TARGET = "FanoutTarget_D0.3mm"
PITCH = 0.325  # routing grid aligned with the 0.65 mm ball pitch

# Rev A pin plan (radar60 plan 3.2): balls whose nets leave the package.
REVA_SIGNALS = {
    "N4": "UART_RX",
    "N5": "UART_TX",
    "H14": "CAN_TX",
    "F14": "CAN_RX",
    "E13": "SPIA_CLK",
    "E15": "SPIA_CS_N",
    "E14": "SPIA_MISO",
    "D13": "SPIA_MOSI",
    "G14": "I2C_SCL",
    "F13": "I2C_SDA",
    "R12": "QSPI_CLK",
    "P11": "QSPI_CS_N",
    "R13": "QSPI_D0",
    "N12": "QSPI_D1",
    "R14": "QSPI_D2",
    "P12": "QSPI_D3",
    "J14": "LVDS_TXP0",
    "J15": "LVDS_TXM0",
    "K14": "LVDS_TXP1",
    "K15": "LVDS_TXM1",
    "L14": "LVDS_CLKP",
    "L15": "LVDS_CLKM",
    "M14": "LVDS_FRCLKP",
    "M15": "LVDS_FRCLKM",
    "P10": "JTAG_TCK",
    "N10": "JTAG_TMS",
    "R11": "JTAG_TDI",
    "N13": "JTAG_TDO_SOP0",
    "P9": "SOP2_PMIC_CLKOUT",
    "G13": "SOP1_SYNC_OUT",
    "R3": "NRESET",
    "N6": "NERROR_OUT",
    "N9": "WARM_RESET",
    "N8": "MCU_CLKOUT",
    "N7": "NERROR_IN",
    "B15": "XTAL_P",
    "C15": "XTAL_M",
}
# The internal LDO outputs and the band gap need a capacitor next to the ball, not a rail.
CAPS = {"B10": "PWR_VBGAP", "A10": "PWR_APLL", "B13": "PWR_SYNTH"}
# Out-of-plane side of each ball edge after the 270 degree rotation: the RX edge (column 1) is
# north, the TX edge (row A) east, column 15 south, row R west.
SIDES = ("west", "south", "east", "north")
SIGNAL_SIDES = ("west", "south")  # the escape may not cross the RF edges (plan 7.2)
CAP_SIDES = ("west", "south", "east")  # caps may sit below the TX region (PWR is allowed in R4)


VARIANTS = (
    "reva",  # the Rev A pin plan
    "all",  # every signal ball
    "reva-via035",  # every via 0.35/0.15 (GND drops fit the interstitial sites)
    "all-via035",
    "reva-signals",  # GND and supply balls left unconnected: signal escape alone
    "all-signals",
)


def nets_for(variant):
    """``{ball: net}`` for U1 and ``{net: kind}`` (signal, cap, plane, gnd, rf)."""
    base, _, option = variant.partition("-")
    variant = base
    balls = fcbga161.ball_map()
    net, kind = {}, {"GND": "gnd", "PWR_PLANE": "plane"}
    signals = (
        REVA_SIGNALS
        if variant == "reva"
        else {b: "SIG_" + n for b, (n, k) in balls.items() if k == "signal"}
    )
    for ball, (name, k) in balls.items():
        if k == "gnd":
            net[ball] = "GND"
        elif k == "rf":
            net[ball] = "RF_" + name
            kind[net[ball]] = "rf"
        elif ball in CAPS:
            net[ball] = CAPS[ball]
            kind[net[ball]] = "cap"
        elif k == "power" and name != "VPP":
            net[ball] = "PWR_PLANE"  # every supply ball drops a via to the In3 pours
        elif ball in signals:
            net[ball] = signals[ball]
            kind[net[ball]] = "signal"
    if option == "signals":
        net = {b: n for b, n in net.items() if n not in ("GND", "PWR_PLANE")}
    return net, kind


def target_footprint():
    return """(footprint "%s"
\t(version 20241229)
\t(generator "radar60_escape_probe")
\t(layer "F.Cu")
\t(descr "Fanout target for the radar60 BGA escape probe: one 0.3 mm SMD pad")
\t(attr smd)
\t(property "Reference" "T" (at 0 -0.5 0) (layer "F.Fab") (uuid "%s")
\t\t(effects (font (size 0.2 0.2) (thickness 0.03))))
\t(property "Value" "target" (at 0 0.5 0) (layer "F.Fab") (uuid "%s")
\t\t(effects (font (size 0.2 0.2) (thickness 0.03))))
\t(fp_rect (start -0.2 -0.2) (end 0.2 0.2) (stroke (width 0.05) (type solid)) (fill no)
\t\t(layer "F.CrtYd") (uuid "%s"))
\t(pad "1" smd circle (at 0 0) (size 0.3 0.3) (layers "F.Cu" "F.Mask") (uuid "%s"))
)
""" % (
        TARGET,
        gen_board._u("probe", "t", "ref"),
        gen_board._u("probe", "t", "val"),
        gen_board._u("probe", "t", "crt"),
        gen_board._u("probe", "t", "pad"),
    )


def u1_pose(fp, pitch=PITCH):
    """U1's probe pose: the Rev A pose moved (by less than half a cell) so its balls and the
    channels between them lie on routing-cell centres ((i + 0.5) * pitch)."""
    x, y = fp.doc["u1"]["at"]
    snap = lambda v: round((round(v / pitch - 0.5) + 0.5) * pitch, 4)  # noqa: E731
    return (snap(x), snap(y)), fp.doc["u1"]["rot"]


def edge_depths(ball, rot):
    """``{side: depth}``: how many ball rows lie between the ball and each package edge."""
    x, y = fcbga161.ball_xy(ball, rot)
    edge = 4.55  # outer ball row
    return {
        "west": round((x + edge) / fcbga161.PITCH_MM),
        "east": round((edge - x) / fcbga161.PITCH_MM),
        "south": round((y + edge) / fcbga161.PITCH_MM),
        "north": round((edge - y) / fcbga161.PITCH_MM),
    }


def target_for(ball, rot, centre, sides, first=1.1, step=1.3):
    """The ball's fanout target: on its nearest allowed side, in line with the ball, one target
    row per ball row of depth (``first`` mm beyond the body edge, then every ``step`` mm)."""
    depth = edge_depths(ball, rot)
    side = min(sides, key=lambda s: (depth[s], SIDES.index(s)))
    x, y = fcbga161.ball_xy(ball, rot)
    out = 5.2 + first + depth[side] * step
    cx, cy = centre
    at = {
        "west": (cx - out, cy + y),
        "east": (cx + out, cy + min(y, -2.2)),  # below the TX region (y >= cy - 1.2)
        "south": (cx + x, cy - out),
        "north": (cx + x, cy + out),
    }[side]
    return side, depth[side], (round(at[0], 4), round(at[1], 4))


def rf_exit_keepouts(net, kind):
    """A 0.1 mm wide reservation (the router grows it by a via radius) from each 60 GHz ball's
    outer depopulated site to the package edge, in U1's frame: the RF macro's exits."""
    out = []
    for ball in sorted(b for b, x in net.items() if kind.get(x) == "rf"):
        r, c = fcbga161.grid_index(ball)
        if r == 1:  # TX: row B, the exit is row A (local +y)
            x, _ = fcbga161.ball_xy(ball)
            rect = [x - 0.05, 4.45, x + 0.05, 5.4]
        else:  # RX: column 2, the exit is column 1 (local -x)
            _, y = fcbga161.ball_xy(ball)
            rect = [-5.4, y - 0.05, -4.45, y + 0.05]
        out.append({"name": "rf_exit_" + ball, "ref": "U1", "rect_mm": [round(v, 4) for v in rect]})
    return out


def design(variant, fp):
    net, kind = nets_for(variant)
    centre, rot = u1_pose(fp)
    pins = {b: net.get(b, "") for b in fcbga161.ball_map()}
    # The 60 GHz balls belong to the RF macro (RFM1), which routes them on F.Cu through the
    # depopulated outer site in front of each; the probe leaves them unconnected and reserves
    # those exits (copper_keepout below), as the macro's fixed copper will.
    for ball, n in net.items():
        if kind.get(n) == "rf":
            pins[ball] = ""
    parts = [dict(ref="U1", footprint="%s:%s" % (LIB, fcbga161.NAME), value="IWR6843", pins=pins)]
    fixed = {"U1": {"at": list(centre), "rot": rot, "side": "top"}}
    targets = {}
    for ball, n in sorted(net.items()):
        if kind.get(n) not in ("signal", "cap"):
            continue
        sides = SIGNAL_SIDES if kind[n] == "signal" else CAP_SIDES
        targets[n] = target_for(ball, rot, centre, sides)
    for i, (n, (side, depth, at)) in enumerate(sorted(targets.items(), key=lambda kv: kv[1][2])):
        ref = "TP%d" % (i + 1)
        parts.append(dict(ref=ref, footprint="%s:%s" % (LIB, TARGET), value=n, pins={"1": n}))
        fixed[ref] = {"at": list(at), "rot": 0, "side": "top"}
    if variant.endswith("-signals"):
        # The plane nets still need a pad on the board: one far from U1 each.
        for i, n in enumerate(("GND", "PWR_PLANE")):
            ref = "TPP%d" % (i + 1)
            parts.append(dict(ref=ref, footprint="%s:%s" % (LIB, TARGET), value=n, pins={"1": n}))
            fixed[ref] = {"at": [5.0 + 3.0 * i, 10.0], "rot": 0, "side": "top"}
    half = fp.doc["u1"]["courtyard_mm"] / 2
    connected = sum(1 for p in parts for v in p["pins"].values() if v)
    via = (0.35, 0.15) if variant.endswith("-via035") else (0.4, 0.2)
    planes = {
        "plane_gnd": {"nets": ["GND"], "plane_layer": "In1.Cu", "width_mm": 0.15},
        "plane_pwr": {"nets": ["PWR_PLANE"], "plane_layer": "In3.Cu", "width_mm": 0.15},
    }
    spec = {
        "name": "radar60-escape-" + variant,
        "description": "radar60 U1 (IWR6843 FCBGA-161) escape probe, %s nets" % variant,
        "parts": parts,
        "constraints": {
            "schema": "v0",
            "board": {
                "outline": {"w": fp.w, "h": fp.h},
                "layers": 6,
                "default_clearance_mm": 0.2,
                "references_on_fab": True,
            },
            "fab": {
                "track_width_mm": 0.1,
                "clearance_mm": 0.1,
                "via_diameter_mm": via[0],
                "via_drill_mm": via[1],
                "hole_clearance_mm": 0.1524,
                "edge_clearance_mm": 0.3,
                "min_through_drill_mm": 0.15,
                "via_annular_mm": 0.0762,
                "min_track_width_mm": 0.1,
                "smd_pad_clearance_mm": 0.1,
                "hole_to_hole_mm": 0.28,
                "via_to_smd_pad_mm": 0.1,
                "min_via_diameter_mm": 0.31,
            },
            "fixed": fixed,
            "net_class": planes,
            # The RF region beyond the macro's launches; the router grows it by a via radius.
            "copper_keepout": rf_exit_keepouts(net, kind)
            + [
                {
                    "name": "rf_north",
                    "ref": "U1",
                    "rect_mm": [-18.0, -10.0, -(half + 1.6), half + 0.2],
                },
                {"name": "rf_east", "ref": "U1", "rect_mm": [-18.0, half + 1.6, 1.2, 18.5]},
            ],
        },
        "expected_components": len(parts),
        "expected_connected_pads": connected,
        "tier": "hard",
        "stackup": {
            "name": "6L-SGSPGS",
            "copper_layers": 6,
            "layers": [
                {"name": "F.Cu", "role": "signal"},
                {"name": "In1.Cu", "role": "plane", "net": "GND"},
                {"name": "In2.Cu", "role": "signal"},
                {"name": "In3.Cu", "role": "plane", "net": "PWR_PLANE"},
                {"name": "In4.Cu", "role": "plane", "net": "GND"},
                {"name": "B.Cu", "role": "signal"},
            ],
            "dielectrics": [
                {"kind": "core", "thickness_mm": 0.1016},
                {"kind": "prepreg", "thickness_mm": 0.1016},
                {"kind": "core", "thickness_mm": 0.51},
                {"kind": "prepreg", "thickness_mm": 0.1},
                {"kind": "core", "thickness_mm": 0.2},
            ],
            "outer_copper_mm": 0.035,
            "inner_copper_mm": 0.0175,
            "board_thickness_mm": 1.17,
        },
        "via_policy": {"name": "through", "allowed": ["through"]},
        "sides": "single",
        "checks": [
            {"id": "inside-board", "kind": "inside_board", "refs": "*", "engine": "native"},
            {"id": "single-sided", "kind": "side", "refs": "*", "side": "top", "engine": "native"},
        ],
        "dims": {
            "stackup": "6L-SGSPGS",
            "via_policy": "through",
            "sides": "single",
            "probe": variant,
        },
        "ci": {"lane": "manual", "minutes": 90},
        "layer_mode": "planes",
    }
    return spec, targets


def build(out: Path, variants):
    fp = gen_board.load()
    lib = out / "lib" / (LIB + ".pretty")
    lib.mkdir(parents=True, exist_ok=True)
    (lib / (fcbga161.NAME + ".kicad_mod")).write_text(fcbga161.footprint_text())
    (lib / (TARGET + ".kicad_mod")).write_text(target_footprint())
    specs = [design(v, fp)[0] for v in variants]
    (out / "designs.json").write_text(json.dumps(specs, indent=1))
    for s in specs:
        print(s["name"], len(s["parts"]), "parts", s["expected_connected_pads"], "connected pads")


# ----------------------------------------------------------------------------- analysis


def _pad_of(description):
    m = re.search(r"Pad (\w+) \[([^\]]*)\] of (\w+)", description)
    return (m.group(3), m.group(1), m.group(2)) if m else None


def inject_rules(board_text, fp, shift):
    """The probe's routed board plus the floorplan's rule areas (RF_REGION, RF_POCKET,
    RF_GUARD), moved with U1's probe pose (``shift``), so radar60.kicad_dru can judge it."""
    import copy

    moved = gen_board.Floorplan(copy.deepcopy(fp.doc))
    dx, dy = shift
    rf = moved.doc["rf"]
    for key in ("region", "guard"):
        rf[key] = [[r[0] + dx, r[1] + dy, r[2] + dx, r[3] + dy] for r in rf[key]]
    p = rf["pocket"]
    rf["pocket"] = [p[0] + dx, p[1] + dy, p[2] + dx, p[3] + dy]
    zones = [
        line
        for line in gen_board.board(moved).splitlines()
        if line.startswith("\t(zone (net 0)") and ('"RF_' in line)
    ]
    idx = board_text.rstrip().rfind(")")
    return board_text[:idx] + "\n".join(zones) + "\n)\n"


def analyze(case_dir: Path, kicad_cli: str, variant: str):
    fp = gen_board.load()
    net, kind = nets_for(variant)
    _, targets = design(variant, fp)
    drc = json.loads((case_dir / "drc.json").read_text())
    routes = json.loads((case_dir / "routes.json").read_text())
    open_pads, open_nets = set(), set()
    for item in drc["unconnected_items"]:
        for it in item.get("items", []):
            text = it.get("description", "")
            p = _pad_of(text)
            if p and p[0] == "U1":
                open_pads.add(p[1])
            m = re.search(r"\[([^\]]+)\]", text)
            if m:
                open_nets.add(m.group(1))
    layers = defaultdict(Counter)
    for t in routes.get("tracks", []):  # [net, layer, start, end, width]
        layers[t[0]][t[1]] += 1
    vias = Counter(v[0] for v in routes.get("vias", []))  # [net, x, y]
    # Board rules on the routed copper: rule areas injected, net classes by name.
    judged = case_dir / "radar60-judge"
    if judged.exists():
        shutil.rmtree(judged)
    judged.mkdir()
    board = judged / "radar60.kicad_pcb"
    centre, _ = u1_pose(fp)
    shift = (centre[0] - fp.doc["u1"]["at"][0], centre[1] - fp.doc["u1"]["at"][1])
    board.write_text(inject_rules((case_dir / "routed.kicad_pcb").read_text(), fp, shift))
    pro = json.loads((case_dir / "routed.kicad_pro").read_text())
    patterns = [
        {"netclass": "RF", "pattern": "RF_*"},
        {"netclass": "GND", "pattern": "GND"},
        {"netclass": "PWR", "pattern": "PWR_*"},
        {"netclass": "XTAL", "pattern": "XTAL_*"},
        {"netclass": "LVDS", "pattern": "LVDS_*"},
        {"netclass": "QSPI", "pattern": "QSPI_*"},
    ]
    classes = pro.setdefault("net_settings", {}).setdefault("classes", [])
    default = next(c for c in classes if c["name"] == "Default")
    for cname in ("RF", "GND", "PWR", "XTAL", "LVDS", "QSPI"):
        if not any(c["name"] == cname for c in classes):
            classes.append(dict(default, name=cname, priority=len(classes)))
    pro["net_settings"]["netclass_patterns"] = patterns
    (judged / "radar60.kicad_pro").write_text(json.dumps(pro, indent=2))
    (judged / "radar60.kicad_dru").write_text(gen_board.dru(fp))
    subprocess.run(
        [
            kicad_cli,
            "pcb",
            "drc",
            str(board),
            "--format",
            "json",
            "--output",
            str(judged / "drc.json"),
        ],
        check=True,
        capture_output=True,
        timeout=900,
    )
    jd = json.loads((judged / "drc.json").read_text())
    rule_hits = defaultdict(Counter)
    for v in jd["violations"]:
        m = re.search(r"radar60_(\w+)", v["description"])
        if not m:
            continue
        for it in v.get("items", []):
            nm = re.search(r"\[([^\]]+)\]", it.get("description", ""))
            if nm:
                rule_hits[nm.group(1)][m.group(1)] += 1
    rows = []
    for ball in sorted(net, key=lambda b: (fcbga161.ring(b), b)):
        n = net[ball]
        k = kind.get(n, "signal")
        if k == "rf":
            k = "rf_macro"  # left to the RF macro: not routed by the probe
        rows.append(
            dict(
                ball=ball,
                ring=fcbga161.ring(ball),
                net=n,
                kind=k,
                escaped=ball not in open_pads
                and (k in ("gnd", "plane", "rf_macro") or n not in open_nets),
                layers=sorted(layers[n]) if k not in ("gnd", "plane") else [],
                vias=vias[n] if k not in ("gnd", "plane") else None,
                rules=dict(rule_hits.get(n, {})) if k not in ("gnd", "plane") else {},
                side=targets[n][0] if n in targets else None,
            )
        )
    summary = Counter((r["kind"], r["escaped"]) for r in rows)
    out = dict(
        variant=variant,
        balls=len(rows),
        summary={"%s_%s" % (a, "ok" if b else "open"): c for (a, b), c in sorted(summary.items())},
        drc_violations=Counter(v["type"] for v in drc["violations"]),
        board_rule_violations=Counter(
            re.search(r"radar60_(\w+)", v["description"]).group(1)
            for v in jd["violations"]
            if "radar60_" in v["description"]
        ),
        rows=rows,
    )
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--out", type=Path, required=True)
    b.add_argument("--variant", action="append", choices=VARIANTS)
    a = sub.add_parser("analyze")
    a.add_argument("case_dir", type=Path)
    a.add_argument("--variant", choices=VARIANTS, required=True)
    a.add_argument("--kicad-cli", required=True)
    a.add_argument("--json", type=Path)
    args = ap.parse_args(argv)
    if args.cmd == "build":
        build(args.out, args.variant or ["reva", "all"])
    else:
        res = analyze(args.case_dir, args.kicad_cli, args.variant)
        text = json.dumps(res, indent=1, default=dict)
        if args.json:
            args.json.write_text(text)
        print(json.dumps({k: v for k, v in res.items() if k != "rows"}, indent=1, default=dict))


if __name__ == "__main__":
    main()
