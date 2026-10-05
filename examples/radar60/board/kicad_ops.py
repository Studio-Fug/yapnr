"""KiCad-side steps of the radar60 Rev A integration (run under KiCad's own Python, pcbnew).

``integrate.py`` calls this module; nothing here decides a placement. Steps:

``source``  the board yapnr ingests: the floorplan board (outline, 6-layer stackup, GND planes,
            rule areas, radome standoff lands) with every footprint of the atopile build copied
            in, nets by name, except the RF macro placeholder (RFM1), any schematic dummy load
            ``rf_macro.rt[i]`` (the macro owns their pose and land) and the floorplan's stand-in
            holes H1-H4 (the build's own mounting-hole parts take their places). U1 is posed at
            its fixed pose and the RF macro is merged in U1's frame (:func:`merge_macro`): feeds,
            vias and zones bit for bit with nets renamed to the schematic's, the column patches
            (active and dummy) and the mask opening as one locked footprint RFM1, the dummy
            loads RT1-RT4 as locked, assembled footprints. Every macro item goes into the KiCad
            group RFM1_MACRO, the engine's ``fixed_block``. The board's In1 GND plane is cut out
            of the RF region, where the macro's own In1 ground is the reference.
``finish``  the placed board (pnr.writeback of the source): the floorplan's rounded outline
            restored, any copper outside the macro removed (nothing is routed at this stage),
            macro items, U1 and the macro's footprints locked, zones filled, and the macro
            digest R1 v2 (:func:`macro_digest`) compared with the RF track's board.
``poses``   footprint poses and pad positions of a board as JSON (for the audits).
``measure`` post-route checks (integrate.py check, R6) on a routed board: the macro group's
            copper digest unchanged by routing (R1 v2), no foreign copper next to the RF region
            (R4), and routed length/skew for the differential pairs and single nets named.

Usage: KICAD_PYTHON kicad_ops.py source FLOORPLAN_PCB ATOPILE_PCB OUT_PCB PARAMS_JSON
       KICAD_PYTHON kicad_ops.py finish PLACED_PCB FLOORPLAN_PCB MACRO_PCB OUT_PCB REPORT_JSON
       KICAD_PYTHON kicad_ops.py poses PCB OUT_JSON
       KICAD_PYTHON kicad_ops.py measure ROUTED_PCB FINISH_JSON PARAMS_JSON OUT_JSON

PARAMS_JSON (integrate.py writes it): ``macro_pcb``, ``u1`` (``address``, ``at_kicad``,
``orientation_deg``) and ``rf_region_kicad`` (the RF region's rectangles in KiCad mm).
``measure``'s own PARAMS_JSON is unrelated (see :func:`measure`'s docstring): it carries the
RF region rects again (the check step may run long after source, with no shared process), plus
``diff_pairs`` and ``nets``.

The text helpers (:func:`merge_macro`, :func:`macro_digest`, ...) need no pcbnew; the unit tests
in tests/unit/radar60 run them on the committed macro board.
"""

import hashlib
import json
import math
import os
import re
import sys

MACRO_ADDRESS = "rf_macro.rfm1"
MACRO_GROUP = "RFM1_MACRO"
# Schematic dummy loads (rf_macro.rt[i] -> RT(i+1)): the macro owns their pose and land.
LOAD_ADDRESS = re.compile(r"^rf_macro\.rt\[(\d+)\]$")
STAND_IN_HOLES = ("H1", "H2", "H3", "H4")
MACRO_CENTRE = (100.0, 100.0)  # the macro board's U1 centre (rfmacro.kicad)
U1_FOOTPRINT = "IWR6843_ABL0161_RF_EDGES"  # the macro board's U1 stand-in (the build has U1)
COLUMN_FOOTPRINTS = ("radar60:COL2_CORPORATE", "radar60:COL2_DUMMY")
DUMMY_FOOTPRINT = "radar60:COL2_DUMMY"
LOAD_FOOTPRINT = "radar60:R_0201_0603Metric_LOAD"
MASK_FOOTPRINT = "radar60:RFM1_MASK"
RFM1_FOOTPRINT = "radar60:RFM1_Macro"
# rfm1-m/-n/-p (RF uniformity, 2026-10-04): 7 active columns and 4 terminated dummies.
DEFAULT_COLUMNS = (
    "RXD0",
    "RX1",
    "RX2",
    "RX3",
    "RX4",
    "RXD5",
    "TXD0",
    "TX1",
    "TX2",
    "TX3",
    "TXD4",
)
NUM = r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?"


def macro_nets(record=None):
    """Macro net -> board net: GND stays, every column's net gets the ``RF_`` prefix (the
    schematic's RF_RX1 ... RF_TX3; the dummy columns' RF_RXD0 ... RF_TXD4 are board-only nets of
    the RF class)."""
    names = list((record or {}).get("columns") or DEFAULT_COLUMNS)
    return {"GND": "GND", **{n: "RF_" + n for n in names}}


MACRO_NETS = macro_nets()


def _field(fp, name):
    for field in fp.GetFields():
        if field.GetName() == name:
            return field.GetText()
    return None


def _address(fp):
    return _field(fp, "atopile_address") or ""


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


def _rename_nets(item, nets=None):
    nets = MACRO_NETS if nets is None else nets

    def sub(m):
        name = m.group(1)
        if name.startswith("EXT_"):
            raise ValueError("macro item outside U1's footprint on net " + name)
        return '(net "%s")' % nets.get(name, name)

    return re.sub(r'\(net "([^"]*)"\)', sub, item)


def _uuid(seed):
    h = hashlib.sha256(("radar60-integrate:" + seed).encode()).hexdigest()
    return "%s-%s-5%s-8%s-%s" % (h[:8], h[8:12], h[13:16], h[17:20], h[20:32])


def _fpid(item):
    return re.match(r'\(footprint\s+"([^"]*)"', item).group(1)


def _reference(item):
    m = re.search(r'\(property\s+"Reference"\s+"([^"]*)"', item)
    return m.group(1) if m else ""


def _at(item):
    """``(x, y, rot)`` of an item's own ``(at ...)`` child."""
    at = children(item, "at")[0]
    m = re.match(r"\(at\s+(%s)\s+(%s)(?:\s+(%s))?\s*\)" % (NUM, NUM, NUM), at)
    return float(m.group(1)), float(m.group(2)), float(m.group(3) or 0.0)


def _rotate(x, y, deg):
    """A footprint-local point turned by the footprint's orientation, as KiCad's RotatePoint
    turns it (KiCad mm, y down; a positive angle is counter-clockwise on screen)."""
    if not deg:
        return x, y
    a = math.radians(deg)
    return x * math.cos(a) + y * math.sin(a), -x * math.sin(a) + y * math.cos(a)


def column_name(item):
    """A column footprint's column name (``ANT_RXD0`` -> ``RXD0``)."""
    return re.search(r'\(property\s+"Reference"\s+"ANT_([A-Z]+\d)"', item).group(1)


# ---------------------------------------------------------------- the macro merge


def macro_footprint(columns, mask, record, x, y, nets=None):
    """One locked footprint RFM1 at U1's centre: every column's patch pads, active and dummy,
    numbered by port (as the schematic placeholder's pads are: ``rx1`` ... ``tx3``; the dummy
    columns ``rxd0``, ``rxd5``, ``txd0``, ``txd4`` on their board-only nets), and the mask
    opening's polygons as drawn (the dummy loads' mask islands are its gaps), in the macro's
    U1 frame."""
    mx, my = MACRO_CENTRE
    body = []
    for item in columns:
        cx, cy, _rot = _at(item)
        port = column_name(item).lower()
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
            body.append("\t\t" + _rename_nets(pad, nets))
    for poly in children(mask, "fp_poly"):
        body.append("\t\t" + poly)
    sha = record["geometry_sha256"]
    head = [
        '\t(footprint "%s" (locked yes) (layer "F.Cu") (uuid "%s") (at %s %s)'
        % (RFM1_FOOTPRINT, _uuid("rfm1"), _fmt(x), _fmt(y)),
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


def load_footprint(item, dx, dy, nets=None, fields=None):
    """A dummy column's 0201 load as a board footprint: the macro's own footprint (pads, land,
    courtyard) moved by ``(dx, dy)`` into the board's U1 frame, locked, nets renamed. It stays
    an assembled part (``attr smd``: in the BOM and the position files). ``fields`` (from the
    schematic's ``rf_macro.rt[i]``, when it has one) are added as hidden properties, so the
    schematic owns the part's address and BOM data while the macro owns its pose and land."""
    spans = top_items(item)
    at_span = next((a, b) for a, b in spans if _kind(item[a:b]) == "at")
    at = item[at_span[0] : at_span[1]]
    m = re.match(r"\(at\s+(%s)\s+(%s)((?:\s+%s)?)\s*\)" % (NUM, NUM, NUM), at)
    moved = "(at %s %s%s)" % (
        _fmt(float(m.group(1)) + dx),
        _fmt(float(m.group(2)) + dy),
        m.group(3),
    )
    item = item[: at_span[0]] + moved + item[at_span[1] :]
    if "(locked yes)" not in item[: item.find("(layer")]:
        item = re.sub(
            r'^\(footprint\s+("[^"]*")', lambda q: "(footprint %s (locked yes)" % q.group(1), item
        )
    ref = _reference(item)
    extra = []
    for name, value in sorted((fields or {}).items()):
        if name in ("Reference",):
            continue
        if name == "Value":
            item = re.sub(
                r'(\(property\s+"Value"\s+)"[^"]*"',
                lambda q: '%s"%s"' % (q.group(1), value.replace('"', "'")),
                item,
                count=1,
            )
            continue
        extra.append(
            '\t\t(property "%s" "%s" (at 0 0) (layer "F.Fab") (hide yes) (uuid "%s") '
            "(effects (font (size 0.4 0.4) (thickness 0.06))))"
            % (name, str(value).replace('"', "'"), _uuid("%s-%s" % (ref, name)))
        )
    if extra:
        cut = item.rfind(")")
        item = item[:cut].rstrip() + "\n" + "\n".join(extra) + "\n\t)"
    return "\t" + _rename_nets(item, nets)


def merge_macro(text, macro_text, record, u1_xy, held=None):
    """``text`` with the macro's items added in U1's frame; returns (text, summary).

    The macro board must carry exactly the record's columns (active ``COL2_CORPORATE`` and,
    for ``dummy: true``, ``COL2_DUMMY``), the record's loads (``R_0201_0603Metric_LOAD``, one
    per dummy column) and one mask footprint; any other footprint is refused. ``held`` maps a
    load reference (``RT1``) to the schematic fields :func:`load_footprint` adds."""
    nets = macro_nets(record)
    dx, dy = u1_xy[0] - MACRO_CENTRE[0], u1_xy[1] - MACRO_CENTRE[1]
    items = [macro_text[a:b] for a, b in top_items(macro_text)]
    out, columns, loads, masks, counts = [], [], [], [], {}
    for item in items:
        kind = _kind(item)
        if kind == "footprint":
            fpid = _fpid(item)
            if U1_FOOTPRINT in fpid:
                continue  # U1 is the build's own footprint
            if fpid in COLUMN_FOOTPRINTS:
                columns.append(item)
            elif fpid == LOAD_FOOTPRINT:
                loads.append(item)
            elif fpid == MASK_FOOTPRINT:
                masks.append(item)
            else:
                raise ValueError("unexpected macro footprint: " + item[:80])
            continue
        if kind in ("segment", "arc", "via", "zone"):
            out.append("\t" + _rename_nets(_shift_all(item, dx, dy), nets))
            counts[kind] = counts.get(kind, 0) + 1
    want = record.get("columns") or {}
    got = {column_name(c): c for c in columns}
    if sorted(got) != sorted(want) or len(got) != len(columns):
        raise ValueError(
            "macro columns %s differ from the record's %s" % (sorted(got), sorted(want))
        )
    for name, item in got.items():
        dummy = bool(want[name].get("dummy"))
        if dummy != (_fpid(item) == DUMMY_FOOTPRINT):
            raise ValueError(
                "macro column %s: dummy %s but footprint %s" % (name, dummy, _fpid(item))
            )
    want_loads = sorted(spec["ref"] for spec in (record.get("loads") or {}).values())
    got_loads = sorted(_reference(item) for item in loads)
    if got_loads != want_loads:
        raise ValueError("macro loads %s differ from the record's %s" % (got_loads, want_loads))
    if len(masks) != 1:
        raise ValueError("macro: expected one mask footprint, found %d" % len(masks))
    ordered = [got[n] for n in want]  # the record's column order
    out.append(macro_footprint(ordered, masks[0], record, u1_xy[0], u1_xy[1], nets))
    for item in loads:
        out.append(load_footprint(item, dx, dy, nets, (held or {}).get(_reference(item))))
    idx = text.rstrip().rfind(")")
    merged = text[:idx] + "\n".join(out) + "\n" + text[idx:]
    counts["columns"] = len(columns)
    counts["dummy_columns"] = sum(1 for n in want if want[n].get("dummy"))
    counts["loads"] = len(loads)
    counts["mask_polygons"] = len(children(masks[0], "fp_poly"))
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


# ---------------------------------------------------------------- digests (R1)


def _copper_rows(text, origin, rename=None):
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
    return rows


def copper_digest(text, origin, rename=None):
    """sha256 of every track, arc and via of a board in a frame centred on ``origin`` (KiCad
    mm): kind, points to 0.1 um, width or size and drill, layers, net (renamed by ``rename``).
    Equal digests mean the same copper, whatever the file's formatting (plan 7.5 R1)."""
    rows = sorted(_copper_rows(text, origin, rename))
    return hashlib.sha256("\n".join(rows).encode()).hexdigest(), len(rows)


def _footprints(text):
    return [text[a:b] for a, b in top_items(text) if _kind(text[a:b]) == "footprint"]


def _load_pad_rows(item, origin, rename=None):
    fx, fy, frot = _at(item)
    ref = _reference(item)
    rows = []
    for pad in children(item, "pad"):
        head = re.match(r'\(pad\s+"([^"]*)"\s+(\w+)\s+(\w+)', pad)
        px, py, prot = _at(pad)
        ox, oy = _rotate(px, py, frot)
        size = re.search(r"\(size\s+(%s)\s+(%s)\)" % (NUM, NUM), pad)
        layers = re.search(r'\(layers\s+((?:"[^"]*"\s*)+)\)', pad).group(1).split()
        net = re.search(r'\(net\s+(?:\d+\s+)?"([^"]*)"\)', pad)
        net = net.group(1) if net else ""
        rows.append(
            "|".join(
                [
                    "load_pad",
                    ref,
                    head.group(1),
                    head.group(2),
                    head.group(3),
                    "%d %d"
                    % (round((fx + ox - origin[0]) * 1e6), round((fy + oy - origin[1]) * 1e6)),
                    "%.3f" % (prot % 360.0),
                    "%.4f %.4f" % (float(size.group(1)), float(size.group(2))),
                ]
                + sorted(layers)
                + [(rename or {}).get(net, net)]
            )
        )
    return rows


def _mask_rows(item, origin):
    fx, fy, frot = _at(item)
    rows = []
    for poly in children(item, "fp_poly"):
        layer = re.search(r'\(layer\s+"([^"]*)"\)', poly).group(1)
        pts = []
        for m in re.finditer(r"\(xy\s+(%s)\s+(%s)\)" % (NUM, NUM), poly):
            ox, oy = _rotate(float(m.group(1)), float(m.group(2)), frot)
            pts.append(
                "%d %d" % (round((fx + ox - origin[0]) * 1e6), round((fy + oy - origin[1]) * 1e6))
            )
        rows.append("|".join(["mask", layer] + pts))
    return rows


def macro_digest(text, origin, rename=None, loads=()):
    """R1 v2: sha256 of the macro's tracks, arcs and vias (as :func:`copper_digest`), its dummy
    loads' pads (footprint references ``loads``: position, orientation, size, shape, layers and
    net) and its mask polygons (the macro board's RFM1_MASK footprint or the board's RFM1),
    all in a frame centred on ``origin`` (U1's centre, KiCad mm). Returns ``(sha, counts)``."""
    rows = _copper_rows(text, origin, rename)
    n_copper = len(rows)
    n_pads = n_mask = 0
    loads = set(loads)
    for item in _footprints(text):
        fpid = _fpid(item)
        if loads and _reference(item) in loads:
            got = _load_pad_rows(item, origin, rename)
            rows += got
            n_pads += len(got)
        if fpid in (MASK_FOOTPRINT, RFM1_FOOTPRINT):
            got = _mask_rows(item, origin)
            rows += got
            n_mask += len(got)
    rows.sort()
    sha = hashlib.sha256(("radar60-macro-digest/2\n" + "\n".join(rows)).encode()).hexdigest()
    return sha, dict(copper=n_copper, load_pads=n_pads, mask_polygons=n_mask)


# ---------------------------------------------------------------- source


def _rect_poly(pcbnew, rects):
    cut = pcbnew.SHAPE_POLY_SET()
    for x0, y0, x1, y1 in rects:
        cut.NewOutline()
        for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
            cut.Append(int(round(x * 1e6)), int(round(y * 1e6)))
    cut.Simplify()
    return cut


def source(floorplan_pcb, atopile_pcb, out_pcb, params_json):
    import pcbnew

    params = json.load(open(params_json, encoding="utf-8"))
    macro_pcb = params["macro_pcb"]
    record = json.load(open(macro_pcb[: -len(".kicad_pcb")] + ".json", encoding="utf-8"))
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

    copied, skipped, held = [], [], {}
    for fp in list(ato.GetFootprints()):
        address = _address(fp)
        load = LOAD_ADDRESS.match(address)
        if address == MACRO_ADDRESS or load:
            skipped.append(fp.GetReference())
            if load:  # the macro's RT(i+1) takes the schematic's address and BOM fields
                fields = {f.GetName(): f.GetText() for f in fp.GetFields()}
                fields["Value"] = fp.GetValue()
                held["RT%d" % (int(load.group(1)) + 1)] = fields
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
    # The macro's nets exist before its items come in as text (the file declares them).
    for name in macro_nets(record).values():
        net(name)
    u1 = next(fp for fp in board.GetFootprints() if _address(fp) == params["u1"]["address"])
    ux, uy = params["u1"]["at_kicad"]
    u1.SetPosition(pcbnew.VECTOR2I(int(round(ux * 1e6)), int(round(uy * 1e6))))
    u1.SetOrientationDegrees(float(params["u1"]["orientation_deg"]))
    pcbnew.SaveBoard(out_pcb, board)

    text = open(out_pcb, encoding="utf-8").read()
    macro_text = open(macro_pcb, encoding="utf-8").read()
    text, counts = merge_macro(text, macro_text, record, (ux, uy), held)
    open(out_pcb, "w", encoding="utf-8").write(text)

    zone_ids = set()
    for a, b in top_items(macro_text):
        if _kind(macro_text[a:b]) == "zone":
            m = re.search(r'\(uuid "([^"]+)"\)', macro_text[a:b])
            if m:
                zone_ids.add(m.group(1))
    board = pcbnew.LoadBoard(out_pcb)
    group = pcbnew.PCB_GROUP(board)
    group.SetName(MACRO_GROUP)
    board.Add(group)
    grouped = dict(tracks=0, zones=0, footprints=[])
    for t in list(board.GetTracks()):  # the macro's: neither source board has copper
        t.SetLocked(True)
        group.AddItem(t)
        grouped["tracks"] += 1
    for z in list(board.Zones()):
        if z.m_Uuid.AsString() in zone_ids:
            z.SetLocked(True)
            group.AddItem(z)
            grouped["zones"] += 1
    refs = ["RFM1"] + sorted(spec["ref"] for spec in (record.get("loads") or {}).values())
    for ref in refs:
        fp = board.FindFootprintByReference(ref)
        fp.SetLocked(True)
        group.AddItem(fp)
        grouped["footprints"].append(ref)
    # The board's In1 GND plane stops at the RF region (the macro's own In1 ground is the RF
    # reference there; the region's keepout bars every other pour): its rectangles are cut out.
    cut = _rect_poly(pcbnew, params["rf_region_kicad"])
    plane_cut = {}
    for z in board.Zones():
        if z.GetZoneName() == "PLANE_In1" and not z.GetIsRuleArea():
            before = z.Outline().Area()
            z.Outline().BooleanSubtract(cut)
            z.SetAssignedPriority(1)  # distinct from the macro's In1 zone, which it may touch
            plane_cut[z.GetZoneName()] = round((before - z.Outline().Area()) / 1e12, 3)
    pcbnew.SaveBoard(out_pcb, board)
    report = {
        "copied": len(copied),
        "skipped": skipped,
        "held_schematic_loads": sorted(held),
        "nets": len(nets),
        "u1": {"at_kicad": [ux, uy], "orientation_deg": params["u1"]["orientation_deg"]},
        "macro": dict(counts, variant=_variant(record), geometry_sha256=record["geometry_sha256"]),
        "group": dict(grouped, name=MACRO_GROUP),
        "plane_in1_cut_mm2": plane_cut,
    }
    print(json.dumps(report, sort_keys=True))


# ---------------------------------------------------------------- finish


def _group_name(item):
    group = item.GetParentGroup()
    name = None
    while group is not None:
        name = group.GetName()
        owner = group.AsEdaItem() if hasattr(group, "AsEdaItem") else group
        group = owner.GetParentGroup()
    return name


def finish(placed_pcb, floorplan_pcb, macro_pcb, out_pcb, report_json):
    import pcbnew

    report = {}
    record = json.load(open(macro_pcb[: -len(".kicad_pcb")] + ".json", encoding="utf-8"))
    load_refs = sorted(spec["ref"] for spec in (record.get("loads") or {}).values())
    board = pcbnew.LoadBoard(placed_pcb)
    u1 = board.FindFootprintByReference("U1")
    pos = u1.GetPosition()
    u1_xy = (pos.x / 1e6, pos.y / 1e6)
    report["u1"] = {"at_kicad": u1_xy, "orientation_deg": u1.GetOrientationDegrees()}
    # Nothing is routed at this stage: copper outside the macro group (writeback's plane drops,
    # which plane_fallback_drops: false already stops) is removed.
    removed = {"tracks": 0, "vias": 0}
    kept = 0
    for item in list(board.GetTracks()):
        if _group_name(item) == MACRO_GROUP:
            kept += 1
            continue
        removed["vias" if item.GetClass() == "PCB_VIA" else "tracks"] += 1
        board.Delete(item)
    report["copper_outside_macro_removed"] = removed
    report["macro_items"] = kept
    report["engine_keepout_rule_areas"] = sorted(
        z.GetZoneName() for z in board.Zones() if z.GetZoneName().startswith("PNR keepout:")
    )
    # Writeback draws those rule areas with fresh UUIDs; named ones get UUIDs from their names, so
    # the board rebuilt from the placement record (finish --placement) is the same file.
    for z in board.Zones():
        if z.GetZoneName().startswith("PNR keepout:"):
            z.m_Uuid.Clone(pcbnew.KIID(_uuid("zone:" + z.GetZoneName())))
    for ref in ["U1", "RFM1"] + load_refs:
        board.FindFootprintByReference(ref).SetLocked(True)
    pcbnew.SaveBoard(out_pcb, board)

    text = open(out_pcb, encoding="utf-8").read()
    floorplan_text = open(floorplan_pcb, encoding="utf-8").read()
    text, n_outline = replace_outline(text, floorplan_text)
    report["outline_items"] = n_outline
    open(out_pcb, "w", encoding="utf-8").write(text)

    board = pcbnew.LoadBoard(out_pcb)
    filler = pcbnew.ZONE_FILLER(board)
    filler.Fill(board.Zones())
    pcbnew.SaveBoard(out_pcb, board)
    rfm1 = board.FindFootprintByReference("RFM1")
    field = _field(rfm1, "geometry_sha256") if rfm1 else None
    report["macro"] = dict(
        geometry_sha256=record["geometry_sha256"],
        rfm1_field=field,
        rfm1_field_equal=field == record["geometry_sha256"],
        variant=_variant(record),
        status=record.get("status"),
        columns=len(record.get("columns") or {}),
        dummy_columns=sum(1 for c in (record.get("columns") or {}).values() if c.get("dummy")),
        loads=load_refs,
    )
    # R1 v2: the macro's copper, its loads' pads and its mask polygons on the board against the
    # RF track's macro board, both in U1's frame.
    macro_text = open(macro_pcb, encoding="utf-8").read()
    want = macro_digest(macro_text, MACRO_CENTRE, macro_nets(record), load_refs)
    got = macro_digest(open(out_pcb, encoding="utf-8").read(), u1_xy, None, load_refs)
    v1 = (
        copper_digest(macro_text, MACRO_CENTRE, macro_nets(record)),
        copper_digest(open(out_pcb, encoding="utf-8").read(), u1_xy),
    )
    report["r1_macro_copper"] = {
        "macro_sha256": want[0],
        "board_sha256": got[0],
        "items": [want[1], got[1]],
        "equal": want == got and report["macro"]["rfm1_field_equal"],
        "copper_only_equal": v1[0] == v1[1],
    }
    with open(report_json, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=1, sort_keys=True)
    print(json.dumps(report, sort_keys=True))


# ---------------------------------------------------------------- measure (post-route checks)


def _foreign_copper(pcbnew, board, group, rects_kicad_mm, layers):
    """Copper on ``layers`` inside ``rects_kicad_mm`` (KiCad page mm) that is not ``group``'s:
    R4 (no digital copper next to the RF region), checked on the routed board."""
    area = pcbnew.SHAPE_POLY_SET()
    for x0, y0, x1, y1 in rects_kicad_mm:
        area.NewOutline()
        for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
            area.Append(round(x * 1e6), round(y * 1e6))
    area.Simplify()
    foreign, zone_fill = [], {}
    for lname in layers:
        la = board.GetLayerID(lname)
        for t in board.GetTracks():
            if _group_name(t) == group or not t.IsOnLayer(la):
                continue
            shape = pcbnew.SHAPE_POLY_SET()
            t.TransformShapeToPolygon(shape, la, 0, 1000, pcbnew.ERROR_INSIDE)
            shape.BooleanIntersection(area)
            if shape.OutlineCount() and shape.Area() > 0:
                foreign.append([t.GetNetname(), t.GetClass(), lname])
        for z in board.Zones():
            if z.GetIsRuleArea() or _group_name(z) == group or not z.IsOnLayer(la):
                continue
            fill = pcbnew.SHAPE_POLY_SET(z.GetFilledPolysList(la))
            fill.BooleanIntersection(area)
            if fill.OutlineCount() and fill.Area() > 0:
                key = "%s %s %s" % (z.GetZoneName() or "zone", z.GetNetname(), lname)
                zone_fill[key] = round(fill.Area() / 1e12, 3)
    return {"items": foreign, "zone_fill_mm2": zone_fill}


def _net_lengths(board, prefixes):
    """Per-net routed length (mm; vias add none) and via count, for nets starting with one of
    ``prefixes`` (e.g. ``LVDS_``, ``QSPI_``)."""
    length, vias, layers = {}, {}, {}
    for t in board.GetTracks():
        n = t.GetNetname()
        if not any(n.startswith(p) for p in prefixes):
            continue
        if t.GetClass() == "PCB_VIA":
            vias[n] = vias.get(n, 0) + 1
            continue
        length[n] = length.get(n, 0.0) + t.GetLength() / 1e6
        layers.setdefault(n, set()).add(board.GetLayerName(t.GetLayer()))
    return length, vias, layers


def measure(routed_pcb, finish_json, params_json, out_json):
    """Post-route checks (integrate.py check, R6): the macro's copper digest unchanged by
    routing (R1 v2), no foreign copper next to the RF region (R4), and routed length/skew for
    the differential pairs and single nets ``params_json`` names.

    ``params_json``: ``{"rf_region_kicad": [[x0,y0,x1,y1], ...], "diff_pairs": {"TX0": ["LVDS_TX0_P",
    "LVDS_TX0_N"], ...}, "nets": ["QSPI_CLK", ...]}`` (KiCad page mm; integrate.py writes it from
    floorplan.yaml, so this module stays floorplan-ignorant)."""
    import pcbnew

    finish_report = json.load(open(finish_json, encoding="utf-8"))
    params = json.load(open(params_json, encoding="utf-8"))
    out = {}

    # R1 v2: the macro group's own copper, untouched by routing.
    board = pcbnew.LoadBoard(routed_pcb)
    u1 = board.FindFootprintByReference("U1").GetPosition()
    u1_xy = (u1.x / 1e6, u1.y / 1e6)
    for item in list(board.GetTracks()):
        if _group_name(item) != MACRO_GROUP:
            board.Delete(item)
    macro_only = out_json[: -len(".json")] + ".macro-only.kicad_pcb"
    pcbnew.SaveBoard(macro_only, board)
    text = open(macro_only, encoding="utf-8").read()
    os.remove(macro_only)
    load_refs = (finish_report.get("macro") or {}).get("loads") or []
    sha, counts = macro_digest(text, u1_xy, None, load_refs)
    want = (finish_report.get("r1_macro_copper") or {}).get("macro_sha256")
    out["r1_macro"] = {"board_sha256": sha, "counts": counts, "want": want, "equal": sha == want}

    # R4 and the diff pairs / single nets: a fresh load (the one above lost everything outside
    # the macro group).
    board = pcbnew.LoadBoard(routed_pcb)
    out["r4_foreign_copper"] = _foreign_copper(
        pcbnew,
        board,
        MACRO_GROUP,
        params.get("rf_region_kicad") or [],
        ("F.Cu", "In1.Cu", "In2.Cu"),
    )
    pairs = params.get("diff_pairs") or {}
    singles = params.get("nets") or []
    prefixes = tuple(sorted({n[: n.rfind("_")] + "_" for pair in pairs.values() for n in pair}))
    length, vias, layers = _net_lengths(board, prefixes)
    out["diff_pairs"] = {}
    for name, (p, n) in pairs.items():
        lp, ln = length.get(p, 0.0), length.get(n, 0.0)
        out["diff_pairs"][name] = {
            "p": round(lp, 3),
            "n": round(ln, 3),
            "skew_mm": round(abs(lp - ln), 3),
            "vias": {p: vias.get(p, 0), n: vias.get(n, 0)},
            "layers": {p: sorted(layers.get(p, [])), n: sorted(layers.get(n, []))},
        }
    routed = [v["p"] for v in out["diff_pairs"].values() if v["p"] > 0]
    out["diff_pairs_group_skew_mm"] = round(max(routed) - min(routed), 3) if routed else None
    length2, vias2, layers2 = _net_lengths(board, tuple(sorted({n for n in singles})))
    out["nets"] = {
        n: {
            "length_mm": round(length2.get(n, 0.0), 3),
            "vias": vias2.get(n, 0),
            "layers": sorted(layers2.get(n, [])),
        }
        for n in singles
    }
    with open(out_json, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1, sort_keys=True)
    print(json.dumps({k: out[k] for k in ("r1_macro", "diff_pairs_group_skew_mm")}, indent=1))


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
                "group": _group_name(fp),
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
        source(*argv[2:6])
    elif cmd == "finish":
        finish(*argv[2:7])
    elif cmd == "poses":
        poses(*argv[2:4])
    elif cmd == "measure":
        measure(*argv[2:6])
    else:
        raise SystemExit(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
