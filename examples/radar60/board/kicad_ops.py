"""KiCad-side steps of the radar60 Rev A integration (run under KiCad's own Python, pcbnew).

``integrate.py`` calls this module; nothing here decides a placement. Steps:

``source``  the board yapnr ingests: the floorplan board (outline, 6-layer stackup, GND planes,
            rule areas, radome standoff lands) with every footprint of the atopile build copied
            in, nets by name, except the RF macro placeholder (RFM1) and the floorplan's stand-in
            holes H1-H4 (the build's own mounting-hole parts take their places).
``finish``  the placed board: the floorplan's rounded outline restored, the engine's all-layer
            copper-keepout rule areas checked against the floorplan's RF region and dropped
            (they would forbid the macro's own copper), the RF macro merged in U1's frame
            (feeds, vias and zones bit for bit, nets renamed to the schematic's; the column
            patches and the mask opening as one locked footprint RFM1), macro items locked,
            zones filled.
``poses``   footprint poses and pad positions of a board as JSON (for the audits).

Usage: KICAD_PYTHON kicad_ops.py source FLOORPLAN_PCB ATOPILE_PCB OUT_PCB
       KICAD_PYTHON kicad_ops.py finish PLACED_PCB FLOORPLAN_PCB MACRO_PCB OUT_PCB REPORT_JSON
       KICAD_PYTHON kicad_ops.py poses PCB OUT_JSON
"""

import hashlib
import json
import re
import sys

MACRO_ADDRESS = "rf_macro.rfm1"
STAND_IN_HOLES = ("H1", "H2", "H3", "H4")
# Macro net -> schematic net (examples/radar60/schematic, rf_macro.ato and radio_iwr6843.ato).
MACRO_NETS = {
    "GND": "GND",
    **{"%s%d" % (b, i): "RF_%s%d" % (b, i) for b in ("RX",) for i in (1, 2, 3, 4)},
}
MACRO_NETS.update({"TX%d" % i: "RF_TX%d" % i for i in (1, 2, 3)})
NUM = r"-?\d+(?:\.\d+)?(?:[eE]-?\d+)?"


def _address(fp):
    for field in fp.GetFields():
        if field.GetName() == "atopile_address":
            return field.GetText()
    return ""


# ---------------------------------------------------------------- source


def source(floorplan_pcb, atopile_pcb, out_pcb):
    import pcbnew

    # Load both boards before changing either: after a Remove(), KiCad 10's Python returns an
    # unwrapped object from the next LoadBoard().
    board = pcbnew.LoadBoard(floorplan_pcb)
    ato = pcbnew.LoadBoard(atopile_pcb)
    for fp in list(board.GetFootprints()):
        if fp.GetReference() in STAND_IN_HOLES:
            board.Delete(fp)
    nets = {}

    def net(name):
        if name not in nets:
            found = board.FindNet(name)
            if found is None:
                found = pcbnew.NETINFO_ITEM(board, name)
                board.Add(found)
            nets[name] = found
        return nets[name]

    copied, skipped = [], []
    for fp in list(ato.GetFootprints()):
        if _address(fp) == MACRO_ADDRESS:
            skipped.append(fp.GetReference())
            continue
        names = {
            pad.GetNumber() + "@" + str(i): pad.GetNetname() for i, pad in enumerate(fp.Pads())
        }
        ato.Remove(fp)
        for i, pad in enumerate(fp.Pads()):
            name = names[pad.GetNumber() + "@" + str(i)]
            pad.SetNet(net(name) if name else board.FindNet(""))
        board.Add(fp)
        copied.append(fp.GetReference())
    pcbnew.SaveBoard(out_pcb, board)
    print(json.dumps({"copied": len(copied), "skipped": skipped, "nets": len(nets)}))


# ---------------------------------------------------------------- s-expression helpers


def top_items(text, depth=2):
    """``[(start, end)]`` spans of the items at ``depth`` (2: the top-level items of a
    ``(kicad_pcb ...)`` file; 2 within one item: its children)."""
    spans, level, start, i, n = [], 0, None, 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif c == "(":
            level += 1
            if level == depth:
                start = i
        elif c == ")":
            if level == depth:
                spans.append((start, i + 1))
            level -= 1
        i += 1
    return spans


def children(item, kind):
    """The direct children ``(kind ...)`` of one item."""
    out = [item[a:b] for a, b in top_items(item)]
    return [c for c in out if _kind(c) == kind]


def _kind(item):
    return re.match(r"\(\s*([a-z_]+)", item).group(1)


def _shift_all(item, dx, dy):
    def sub(m):
        return "(%s %s %s" % (
            m.group(1),
            _fmt(float(m.group(2)) + dx),
            _fmt(float(m.group(3)) + dy),
        )

    return re.sub(r"\((at|start|end|mid|xy|center) (%s) (%s)" % (NUM, NUM), sub, item)


def _fmt(v):
    s = ("%.6f" % v).rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _rename_nets(item):
    def sub(m):
        name = m.group(1)
        if name.startswith("EXT_"):
            raise ValueError("macro item outside U1's footprint on net " + name)
        return '(net "%s")' % MACRO_NETS.get(name, name)

    return re.sub(r'\(net "([^"]*)"\)', sub, item)


def _uuid(seed):
    h = hashlib.sha256(("radar60-integrate:" + seed).encode()).hexdigest()
    return "%s-%s-5%s-8%s-%s" % (h[:8], h[8:12], h[13:16], h[17:20], h[20:32])


def macro_footprint(columns, mask, record, x, y):
    """One locked footprint RFM1 at U1's centre: every column's patch pads (numbered by port,
    as the schematic placeholder's pads are) and the mask opening, in the macro's U1 frame."""
    mx, my = 100.0, 100.0  # the macro board's U1 centre (rfmacro.kicad)
    body = []
    for item in columns:
        at = re.search(r"\(at (%s) (%s)\)" % (NUM, NUM), item)
        cx, cy = float(at.group(1)), float(at.group(2))
        ref = re.search(r'\(property "Reference" "ANT_([A-Z]+\d)"', item).group(1)
        port = ref.lower()
        for pad in children(item, "pad"):
            m = re.match(r'\(pad "[^"]*" smd custom \(at (%s) (%s)\)' % (NUM, NUM), pad)
            px, py = float(m.group(1)) + cx - mx, float(m.group(2)) + cy - my
            pad = re.sub(
                r'^\(pad "[^"]*" smd custom \(at %s %s\)' % (NUM, NUM),
                '(pad "%s" smd custom (at %s %s)' % (port, _fmt(px), _fmt(py)),
                pad,
            )
            pad = re.sub(
                r'\(uuid "[^"]*"\)', '(uuid "%s")' % _uuid("pad%s%s%s" % (port, px, py)), pad
            )
            body.append("\t\t" + _rename_nets(pad))
    for poly in children(mask, "fp_poly"):
        body.append("\t\t" + poly)
    sha = record["geometry_sha256"]
    head = [
        '\t(footprint "radar60:RFM1_Macro" (locked yes) (layer "F.Cu") (uuid "%s") (at %s %s)'
        % (_uuid("rfm1"), _fmt(x), _fmt(y)),
        '\t\t(property "Reference" "RFM1" (at 0 6.2) (layer "F.Fab") (uuid "%s") '
        "(effects (font (size 0.8 0.8) (thickness 0.12))))" % _uuid("rfm1-ref"),
        '\t\t(property "Value" "RF macro rfm1-%s" (at 0 7.4) (layer "F.Fab") (hide yes) (uuid "%s") '
        "(effects (font (size 0.8 0.8) (thickness 0.12))))"
        % (_variant(record), _uuid("rfm1-value")),
        '\t\t(property "atopile_address" "%s" (at 0 0) (layer "F.Fab") (hide yes) (uuid "%s") '
        "(effects (font (size 0.8 0.8) (thickness 0.12))))" % (MACRO_ADDRESS, _uuid("rfm1-addr")),
        '\t\t(property "geometry_sha256" "%s" (at 0 0) (layer "F.Fab") (hide yes) (uuid "%s") '
        "(effects (font (size 0.8 0.8) (thickness 0.12))))" % (sha, _uuid("rfm1-sha")),
        '\t\t(property "Datasheet" "" (at 0 0) (layer "F.Fab") (hide yes) (uuid "%s") '
        "(effects (font (size 0.8 0.8) (thickness 0.12))))" % _uuid("rfm1-ds"),
        '\t\t(property "Description" "radar60 RF macro (examples/radar60/rf)" (at 0 0) '
        '(layer "F.Fab") (hide yes) (uuid "%s") (effects (font (size 0.8 0.8) (thickness 0.12))))'
        % _uuid("rfm1-descr"),
        "\t\t(attr smd board_only exclude_from_pos_files exclude_from_bom)",
    ]
    return "\n".join(head + body + ["\t)"])


def _variant(record):
    v = int(record["params"]["variant"])
    return {-1: "m", 0: "n", 1: "p"}[v]


def merge_macro(text, macro_text, record, u1_xy):
    """``text`` with the macro's items added in U1's frame; returns (text, summary)."""
    dx, dy = u1_xy[0] - 100.0, u1_xy[1] - 100.0
    items = [macro_text[a:b] for a, b in top_items(macro_text)]
    out, columns, mask, counts = [], [], None, {}
    for item in items:
        kind = _kind(item)
        if kind == "footprint":
            if "IWR6843_ABL0161_RF_EDGES" in item:
                continue  # U1 is the build's own footprint
            if "radar60:COL2_CORPORATE" in item:
                columns.append(item)
            elif "radar60:RFM1_MASK" in item:
                mask = item
            else:
                raise ValueError("unexpected macro footprint: " + item[:80])
            continue
        if kind in ("segment", "arc", "via", "zone"):
            out.append("\t" + _rename_nets(_shift_all(item, dx, dy)))
            counts[kind] = counts.get(kind, 0) + 1
    if mask is None or len(columns) != 7:
        raise ValueError("macro: expected 7 columns and a mask footprint")
    out.append(macro_footprint(columns, mask, record, u1_xy[0], u1_xy[1]))
    idx = text.rstrip().rfind(")")
    merged = text[:idx] + "\n".join(out) + "\n" + text[idx:]
    counts["columns"] = len(columns)
    return merged, counts


def replace_outline(text, floorplan_text):
    """Drop every Edge.Cuts graphic of ``text``; insert the floorplan's outline items."""
    spans = top_items(text)
    keep, last = [], 0
    for a, b in spans:
        item = text[a:b]
        if _kind(item).startswith("gr_") and '"Edge.Cuts"' in item:
            keep.append(text[last:a])
            last = b
    keep.append(text[last:])
    text = "".join(keep)
    outline = [
        floorplan_text[a:b]
        for a, b in top_items(floorplan_text)
        if _kind(floorplan_text[a:b]).startswith("gr_") and '"Edge.Cuts"' in floorplan_text[a:b]
    ]
    idx = text.rstrip().rfind(")")
    return text[:idx] + "\n".join("\t" + o for o in outline) + "\n" + text[idx:], len(outline)


def copper_digest(text, origin, rename=None):
    """sha256 of every track, arc and via of a board in a frame centred on ``origin`` (KiCad
    mm): kind, points to 0.1 um, width or size and drill, layers, net (renamed by ``rename``).
    Equal digests mean the same copper, whatever the file's formatting (plan 7.5 R1)."""
    rows = []
    for a, b in top_items(text):
        item = text[a:b]
        kind = _kind(item)
        if kind not in ("segment", "arc", "via"):
            continue
        pts = [
            "%s %d %d"
            % (
                m.group(1),
                round((float(m.group(2)) - origin[0]) * 1e6),
                round((float(m.group(3)) - origin[1]) * 1e6),
            )
            for m in re.finditer(r"\((start|mid|end|at) (%s) (%s)" % (NUM, NUM), item)
        ]
        dims = re.findall(r"\((width|size|drill) (%s)\)" % NUM, item)
        layers = re.search(r'\(layers? ((?:"[^"]*"\s*)+)\)', item).group(1).split()
        net = re.search(r'\(net (?:\d+ )?"([^"]*)"\)', item).group(1)
        net = (rename or {}).get(net, net)
        rows.append(
            "|".join([kind] + pts + ["%s %.4f" % (k, float(v)) for k, v in dims] + layers + [net])
        )
    rows.sort()
    return hashlib.sha256("\n".join(rows).encode()).hexdigest(), len(rows)


# ---------------------------------------------------------------- finish


def _rect_of_zone(zone):
    box = zone.Outline().BBox()
    return [box.GetLeft() / 1e6, box.GetTop() / 1e6, box.GetRight() / 1e6, box.GetBottom() / 1e6]


def finish(placed_pcb, floorplan_pcb, macro_pcb, out_pcb, report_json):
    import pcbnew

    report = {}
    board = pcbnew.LoadBoard(placed_pcb)
    u1 = board.FindFootprintByReference("U1")
    pos = u1.GetPosition()
    u1_xy = (pos.x / 1e6, pos.y / 1e6)
    report["u1"] = {"at_kicad": u1_xy, "orientation_deg": u1.GetOrientationDegrees()}
    # The engine's all-layer copper keepouts (constraints copper_keepout, in U1's frame) must
    # coincide with the floorplan's RF_REGION pieces: that checks the frame transform. They are
    # dropped because they forbid the macro's own copper on every layer (yapnr gap E5).
    regions = sorted(
        _rect_of_zone(z)
        for z in board.Zones()
        if z.GetIsRuleArea() and z.GetZoneName() == "RF_REGION"
    )
    keepouts = [z for z in board.Zones() if z.GetZoneName().startswith("PNR keepout:")]
    rects = sorted(_rect_of_zone(z) for z in keepouts)
    worst = max(
        (abs(a - b) for r, k in zip(regions, rects) for a, b in zip(r, k)), default=float("inf")
    )
    report["copper_keepouts"] = {
        "engine_rule_areas": len(keepouts),
        "rf_region_pieces": len(regions),
        "max_corner_error_mm": round(worst, 6),
        "dropped": True,
    }
    for z in keepouts:
        board.Remove(z)
    # Writeback forms the declared stack's planes and drops a via from every plane pad it can
    # reach (pnr.writeback form_planes, "fallback_vias") although no route was asked for. That
    # is routing, it ignores the RF macro (some drops land on the RF launches), and the BGA
    # fanout belongs to the E4 generator: this stage keeps no copper but the macro's.
    drops = {"tracks": 0, "vias": 0}
    for item in list(board.GetTracks()):
        drops["vias" if item.GetClass() == "PCB_VIA" else "tracks"] += 1
        board.Delete(item)
    report["writeback_plane_drops_removed"] = drops
    pcbnew.SaveBoard(out_pcb, board)

    text = open(out_pcb, encoding="utf-8").read()
    floorplan_text = open(floorplan_pcb, encoding="utf-8").read()
    text, n_outline = replace_outline(text, floorplan_text)
    report["outline_items"] = n_outline
    macro_text = open(macro_pcb, encoding="utf-8").read()
    record = json.load(open(macro_pcb[: -len(".kicad_pcb")] + ".json", encoding="utf-8"))
    text, counts = merge_macro(text, macro_text, record, u1_xy)
    report["macro"] = dict(
        counts,
        geometry_sha256=record["geometry_sha256"],
        variant=_variant(record),
        status=record.get("status"),
    )
    open(out_pcb, "w", encoding="utf-8").write(text)

    board = pcbnew.LoadBoard(out_pcb)
    # The macro's In1.Cu GND zone overlaps the board's In1 plane (same net); KiCad wants
    # distinct priorities for intersecting zones, so the board plane goes one above.
    for zone in board.Zones():
        if zone.GetZoneName() == "PLANE_In1":
            zone.SetAssignedPriority(1)
    locked = 0
    for item in list(board.GetTracks()):
        item.SetLocked(True)  # the only copper so far is the macro's
        locked += 1
    for zone in board.Zones():
        if not zone.GetIsRuleArea() and zone.GetLayerSet().Contains(pcbnew.F_Cu):
            zone.SetLocked(True)
    for ref in ("U1", "RFM1"):
        board.FindFootprintByReference(ref).SetLocked(True)
    report["locked_tracks_and_vias"] = locked
    filler = pcbnew.ZONE_FILLER(board)
    filler.Fill(board.Zones())
    pcbnew.SaveBoard(out_pcb, board)
    # R1: the board's copper (tracks, arcs, vias: the macro's only, at this stage) against the
    # RF track's macro board, both in U1's frame.
    want = copper_digest(macro_text, (100.0, 100.0), MACRO_NETS)
    got = copper_digest(open(out_pcb, encoding="utf-8").read(), u1_xy)
    report["r1_macro_copper"] = {
        "macro_sha256": want[0],
        "board_sha256": got[0],
        "items": [want[1], got[1]],
        "equal": want == got,
    }
    with open(report_json, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=1, sort_keys=True)
    print(json.dumps(report, sort_keys=True))


# ---------------------------------------------------------------- poses


def poses(pcb, out_json):
    import pcbnew

    board = pcbnew.LoadBoard(pcb)
    parts = []
    for fp in board.GetFootprints():
        p = fp.GetPosition()
        cy = fp.GetCourtyard(pcbnew.B_CrtYd if fp.IsFlipped() else pcbnew.F_CrtYd)
        box = cy.BBox() if cy.OutlineCount() else fp.GetBoundingBox(False)
        parts.append(
            {
                "ref": fp.GetReference(),
                "address": _address(fp),
                "value": fp.GetValue(),
                "fpid": fp.GetFPIDAsString(),
                "dnp": bool(fp.IsDNP()),
                "at": [p.x / 1e6, p.y / 1e6],
                "rot": fp.GetOrientationDegrees(),
                "side": "bottom" if fp.IsFlipped() else "top",
                "courtyard": [
                    box.GetLeft() / 1e6,
                    box.GetTop() / 1e6,
                    box.GetRight() / 1e6,
                    box.GetBottom() / 1e6,
                ],
                "pads": [
                    {
                        "number": pad.GetNumber(),
                        "net": pad.GetNetname(),
                        "at": [pad.GetPosition().x / 1e6, pad.GetPosition().y / 1e6],
                        "layers": "F" if pad.IsOnLayer(pcbnew.F_Cu) else "B",
                    }
                    for pad in fp.Pads()
                ],
            }
        )
    tracks = len(board.GetTracks())
    with open(out_json, "w", encoding="utf-8") as fh:
        json.dump({"parts": parts, "tracks_and_vias": tracks}, fh, indent=1)


def main(argv):
    cmd = argv[1]
    if cmd == "source":
        source(*argv[2:5])
    elif cmd == "finish":
        finish(*argv[2:7])
    elif cmd == "poses":
        poses(*argv[2:4])
    else:
        raise SystemExit(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
