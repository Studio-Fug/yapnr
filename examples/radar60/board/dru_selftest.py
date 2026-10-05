"""Check that radar60.kicad_dru does what it says: plant items on the floorplan board and run
KiCad's DRC (headless kicad-cli) on it.

Every planted item either must be flagged by one named board rule or must pass every board rule.
The test board is the generated floorplan plus U1 (fcbga161.py) at its pose, a stand-in RF macro
footprint (RFM1), a part in the VOUT_PA pocket, and planted tracks, vias and parts. Net classes
come from name patterns in the test project (RF_*, XTAL_*, ...).

Usage: python3 dru_selftest.py --kicad-cli PATH [--keep DIR]   (exit 1 on a failed case)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fcbga161  # noqa: E402
import gen_board  # noqa: E402

PATTERNS = {
    "RF": "RF_*",
    "GND": "GND",
    "PWR": "PWR_*",
    "XTAL": "XTAL_*",
    "SW": "SW_*",
    "LVDS": "LVDS_*",
    "QSPI": "QSPI_*",
    "ANALOG": "ANA_*",
}
NETS = ["SIG1", "RF_TX1", "PWR_1V8", "XTAL_P", "SW_B0", "LVDS_TXP0", "ANA_VBGAP"]

# (case, kind, spec, expected board rule or None). Coordinates: board frame (y-up, mm).
CASES = [
    (
        "track_in_rf_region_L1",
        "track",
        dict(a=(18, 44), b=(20, 44), layer="F.Cu", net="SIG1"),
        "rf_region_tracks",
    ),
    (
        "track_in_rf_region_L4",
        "track",
        dict(a=(18, 42), b=(20, 42), layer="In3.Cu", net="SIG1"),
        None,
    ),
    (
        "rf_track_in_rf_region",
        "track",
        dict(a=(36, 44), b=(38, 44), layer="F.Cu", net="RF_TX1"),
        None,
    ),
    ("fence_via_in_rf_region", "via", dict(at=(40, 44), drill=0.15, size=0.32, net="GND"), None),
    (
        "signal_via_in_rf_region",
        "via",
        dict(at=(42, 44), drill=0.2, size=0.4, net="SIG1"),
        "rf_region_vias",
    ),
    ("rf_net_via", "via", dict(at=(52, 14), drill=0.2, size=0.4, net="RF_TX1"), "rf_no_vias"),
    ("part_in_rf_region", "part", dict(at=(18, 40), ref="C90"), "rf_region_parts"),
    # the RF-uniformity pocket is 1.69 x 1.0 mm (y 33.2-34.2): an 0402 courtyard just fits in it
    ("part_in_pocket", "part", dict(at=(30.4, 33.7), ref="C91"), None),
    ("macro_in_rf_region", "part", dict(at=(24, 42), ref="RFM1"), None),
    (
        "digital_track_in_guard",
        "track",
        dict(a=(17, 31), b=(19, 31), layer="F.Cu", net="SIG1"),
        "rf_guard_digital",
    ),
    (
        "digital_track_in_guard_L6",
        "track",
        dict(a=(17, 29.5), b=(19, 29.5), layer="B.Cu", net="SIG1"),
        None,
    ),
    (
        "power_track_in_guard",
        "track",
        dict(a=(53, 30), b=(53, 32), layer="F.Cu", net="PWR_1V8"),  # east of the TX bank
        None,
    ),
    (
        "analog_track_in_guard",
        "track",
        dict(a=(33, 24), b=(35, 24), layer="F.Cu", net="ANA_VBGAP"),
        None,
    ),
    (
        "signal_015_via_under_u1",
        "via",
        dict(at=(26.325, 28.325), drill=0.15, size=0.35, net="SIG1"),
        "bga_signal_vias",
    ),
    (
        "gnd_015_via_under_u1",
        "via",
        dict(at=(25.675, 27.675), drill=0.15, size=0.35, net="GND"),
        None,
    ),
    # the RF macro's GND stitching in the VOUT_PA pocket, outside the RF region (rfm1-n's own
    # positions): clear of U1's courtyard, and where it meets it (the GND rule there asks 0.35)
    ("fence_via_in_pocket", "via", dict(at=(30.2, 34.0), drill=0.15, size=0.32, net="GND"), None),
    (
        "fence_via_in_pocket_at_u1",
        "via",
        dict(at=(30.8, 33.4), drill=0.15, size=0.32, net="GND"),
        None,
    ),
    (
        "signal_015_via_elsewhere",
        "via",
        dict(at=(8, 12), drill=0.15, size=0.35, net="SIG1"),
        "general_vias",
    ),
    ("xtal_via", "via", dict(at=(12, 12), drill=0.2, size=0.4, net="XTAL_P"), "xtal_no_vias"),
    ("sw_near_xtal", "track", dict(a=(20, 6), b=(22, 6), layer="F.Cu", net="SW_B0"), "sw_to_xtal"),
    ("xtal_track", "track", dict(a=(20, 10), b=(22, 10), layer="F.Cu", net="XTAL_P"), "sw_to_xtal"),
    (
        "lvds_on_inner_layer",
        "track",
        dict(a=(8, 20), b=(10, 20), layer="In2.Cu", net="LVDS_TXP0"),
        "lvds_outer_layers",
    ),
    (
        # plan R4: a coupled LVDS pair needs one shared layer for both legs, so B.Cu is no
        # longer an allowed leg even though it is an outer layer (only F.Cu is).
        "lvds_on_bcu",
        "track",
        dict(a=(8, 21), b=(10, 21), layer="B.Cu", net="LVDS_TXP0"),
        "lvds_outer_layers",
    ),
    (
        "track_on_gnd_plane",
        "track",
        dict(a=(8, 24), b=(10, 24), layer="In1.Cu", net="SIG1"),
        "plane_in1",
    ),
]


def _part(fp, ref, at):
    """A 0402-sized two-pad part (no nets)."""
    x, y = fp.k(*at)
    uid = gen_board._u("selftest", ref)
    return (
        '\t(footprint "C_0402" (layer "F.Cu") (uuid "%s") (at %s %s 0)\n'
        '\t\t(property "Reference" "%s" (at 0 -1 0) (layer "F.Fab") (uuid "%s-r")'
        " (effects (font (size 0.5 0.5) (thickness 0.08))))\n"
        "\t\t(fp_rect (start -0.8 -0.45) (end 0.8 0.45) (stroke (width 0.05) (type solid)) (fill no)"
        ' (layer "F.CrtYd") (uuid "%s-c"))\n'
        '\t\t(pad "1" smd roundrect (at -0.48 0) (size 0.56 0.62) (layers "F.Cu" "F.Mask" "F.Paste")'
        ' (roundrect_rratio 0.25) (uuid "%s-1"))\n'
        '\t\t(pad "2" smd roundrect (at 0.48 0) (size 0.56 0.62) (layers "F.Cu" "F.Mask" "F.Paste")'
        ' (roundrect_rratio 0.25) (uuid "%s-2"))\n\t)'
        % (uid, gen_board._n(x), gen_board._n(y), ref, uid[:30], uid[:30], uid[:30], uid[:30])
    )


def _u1(fp):
    """U1 from fcbga161.py at its pose; its pads carry no nets (planted copper keeps clear)."""
    text = fcbga161.footprint_text()
    x, y = fp.k(*fp.doc["u1"]["at"])
    rot = fp.doc["u1"]["rot"]
    body = text.split("\n", 1)[1].rsplit(")", 1)[0]
    # Place: KiCad stores pad positions in the footprint frame and rotates them with (at x y rot).
    body = re.sub(r"\(at ([-\d.]+) ([-\d.]+)\)", r"(at \1 \2 %d)" % rot, body)
    return '\t(footprint "%s" (layer "F.Cu") (uuid "%s") (at %s %s %d)\n%s\t)' % (
        fcbga161.NAME,
        gen_board._u("selftest", "U1"),
        gen_board._n(x),
        gen_board._n(y),
        rot,
        body,
    )


def build(fp, out: Path):
    files = gen_board.outputs(fp)
    pcb = files["radar60.kicad_pcb"]
    nets = "".join('\t(net %d "%s")\n' % (i + 2, n) for i, n in enumerate(NETS))
    pcb = pcb.replace('\t(net 1 "GND")\n', '\t(net 1 "GND")\n' + nets)
    items = [_u1(fp)]
    for name, kind, spec, _ in CASES:
        if kind == "track":
            (ax, ay), (bx, by) = fp.k(*spec["a"]), fp.k(*spec["b"])
            items.append(
                '\t(segment (start %s %s) (end %s %s) (width 0.15) (layer "%s") (net "%s") (uuid "%s"))'
                % (ax, ay, bx, by, spec["layer"], spec["net"], gen_board._u("selftest", name))
            )
        elif kind == "via":
            x, y = fp.k(*spec["at"])
            items.append(
                '\t(via (at %s %s) (size %s) (drill %s) (layers "F.Cu" "B.Cu") (net "%s") (uuid "%s"))'
                % (x, y, spec["size"], spec["drill"], spec["net"], gen_board._u("selftest", name))
            )
        else:
            items.append(_part(fp, spec["ref"], spec["at"]))
    pcb = pcb.rstrip()[:-1] + "\n".join(items) + "\n)\n"
    pro = json.loads(files["radar60.kicad_pro"])
    pro["net_settings"]["netclass_patterns"] = [
        {"netclass": c, "pattern": p} for c, p in PATTERNS.items()
    ]
    out.mkdir(parents=True, exist_ok=True)
    (out / "radar60.kicad_pcb").write_text(pcb)
    (out / "radar60.kicad_pro").write_text(json.dumps(pro, indent=2))
    (out / "radar60.kicad_dru").write_text(files["radar60.kicad_dru"])
    return out / "radar60.kicad_pcb"


def run(kicad_cli, board: Path):
    report = board.with_suffix(".drc.json")
    subprocess.run(
        [
            kicad_cli,
            "pcb",
            "drc",
            str(board),
            "--format",
            "json",
            "--refill-zones",
            "--severity-all",
            "--output",
            str(report),
        ],
        check=True,
        capture_output=True,
        timeout=600,
    )
    return json.loads(report.read_text())


def check(fp, drc):
    """``[(case, expected, found board rules, ok)]``: a case is matched to violations by its
    items' positions."""
    found = {}
    for v in drc["violations"]:
        rule = re.search(r"radar60_(\w+)", v["description"])
        for item in v.get("items", []):
            pos = item.get("pos") or {}
            if rule and "x" in pos:
                found.setdefault((round(pos["x"], 2), round(pos["y"], 2)), set()).add(rule.group(1))
    rows = []
    for name, kind, spec, expected in CASES:
        if kind == "track":
            a, b = fp.k(*spec["a"]), fp.k(*spec["b"])
        else:
            a = b = fp.k(*spec["at"])
        reach = 1.0 if kind == "part" else 0.3
        hits = set()
        for (px, py), rules in found.items():
            if _dist((px, py), a, b) < reach:
                hits |= rules
        ok = (expected in hits) if expected else not hits
        rows.append((name, expected, sorted(hits), ok))
    return rows


def _dist(p, a, b):
    """Distance from point ``p`` to segment ``a``-``b``."""
    (px, py), (ax, ay), (bx, by) = p, a, b
    dx, dy = bx - ax, by - ay
    t = (
        0.0
        if dx == dy == 0
        else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    )
    return ((px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2) ** 0.5


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--kicad-cli", default=os.environ.get("PNR_KICAD_CLI", "kicad-cli"))
    ap.add_argument("--keep", type=Path, help="write the test board here and keep it")
    args = ap.parse_args(argv)
    fp = gen_board.load()
    with tempfile.TemporaryDirectory() as tmp:
        board = build(fp, args.keep or Path(tmp))
        drc = run(args.kicad_cli, board)
    rows = check(fp, drc)
    for name, expected, hits, ok in rows:
        print("%-4s %-28s expected %-20s found %s" % ("ok" if ok else "FAIL", name, expected, hits))
    return 0 if all(r[3] for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
