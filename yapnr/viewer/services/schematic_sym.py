"""KiCad .kicad_sym (s-expression) -> browser-drawable symbol JSON.

Schema ``schematic-symbol-v1`` (one entry per library symbol):

  {
    "schema": "schematic-symbol-v1",
    "lib": "<part folder>",            # == footprint library prefix in graph.json
    "name": "<symbol name>",
    "ref_prefix": "U",                 # property Reference
    "value": "...",                    # property Value
    "lcsc": "C3188898" | null,
    "pin_numbers_hidden": bool, "pin_names_hidden": bool, "pin_name_offset": mm,
    "units": [                         # body style 1 only; unit 0 (common) merged in
      {"unit": 1,
       "bbox": [x0, y0, x1, y1],       # graphics + pins, mm, y DOWN (SVG frame)
       "graphics": [
         {"t": "rect",   "a": [x,y], "b": [x,y], "fill": "none|outline|background", "w": stroke_mm},
         {"t": "poly",   "pts": [[x,y],...], "fill": ..., "w": ...},
         {"t": "circle", "c": [x,y], "r": r, "fill": ..., "w": ...},
         {"t": "arc",    "s": [x,y], "m": [x,y], "e": [x,y], "fill": ..., "w": ...},
         {"t": "bezier", "pts": [[x,y]x4], "fill": ..., "w": ...},
         {"t": "text",   "at": [x,y], "rot": deg, "text": "...", "size": mm}
       ],
       "pins": [
         {"number": "1", "name": "DR1L", "etype": "unspecified", "shape": "line",
          "at": [x,y],                 # electrical connection point (wire end)
          "tip": [x,y],                # body end of the pin line
          "len": 2.54, "rot": 0,       # KiCad angle, symbol frame (y up)
          "side": "W|E|N|S",           # side of the body the pin leaves from (ELK port side)
          "hidden": false}
       ]}
    ]
  }

Coordinates are converted from KiCad's symbol frame (y up) to y down so the
browser can draw them into SVG without a flip. A pin at KiCad angle 0 points from
its connection point towards +x (into the body), so it leaves the body on the
WEST side; 180 -> EAST; 90 (towards +y, i.e. up in KiCad) -> SOUTH; 270 -> NORTH.
"""
from __future__ import annotations

import math
import os
import re
from pathlib import Path

_TOKEN = re.compile(r'\s*(?:(\()|(\))|"((?:[^"\\]|\\.)*)"|([^\s()"]+))')


def parse_sexpr(text: str):
    """Minimal s-expression reader: lists, bare atoms and quoted strings.

    Quoted strings are returned as ``Q(str)`` so ``(name "1")`` is not confused
    with numbers; bare atoms are plain ``str``.
    """
    stack, cur, pos = [], [], 0
    n = len(text)
    while pos < n:
        m = _TOKEN.match(text, pos)
        if not m or m.end() == pos:
            if text[pos:].strip() == "":
                break
            raise ValueError("s-expr syntax error at %d: %r" % (pos, text[pos:pos + 40]))
        pos = m.end()
        if m.group(1):
            stack.append(cur)
            cur = []
        elif m.group(2):
            done, cur = cur, stack.pop()
            cur.append(done)
        elif m.group(3) is not None:
            cur.append(Q(re.sub(r'\\(.)', r'\1', m.group(3))))
        else:
            cur.append(m.group(4))
    if stack:
        raise ValueError("unbalanced s-expression")
    return cur[0] if len(cur) == 1 else cur


class Q(str):
    """Quoted string atom."""


def _head(node):
    return node[0] if isinstance(node, list) and node else None


def _children(node, name):
    return [c for c in node[1:] if isinstance(c, list) and c and c[0] == name]


def _child(node, name):
    for c in node[1:]:
        if isinstance(c, list) and c and c[0] == name:
            return c
    return None


def _flag(node, name):
    """True for a bare ``hide`` atom or ``(hide yes)``."""
    for c in node[1:]:
        if c == name:
            return True
        if isinstance(c, list) and c and c[0] == name:
            return len(c) == 1 or c[1] in ("yes", "true")
    return False


def _xy(node):
    return [float(node[1]), -float(node[2])]  # y up -> y down


def _stroke_fill(node):
    fill = _child(node, "fill")
    ft = _child(fill, "type")[1] if fill is not None and _child(fill, "type") else "none"
    stroke = _child(node, "stroke")
    w = float(_child(stroke, "width")[1]) if stroke is not None and _child(stroke, "width") else 0.0
    return dict(fill=str(ft), w=w)


_SIDE = {0: "W", 180: "E", 90: "S", 270: "N"}


def _graphic(node):
    h = _head(node)
    sf = _stroke_fill(node)
    if h == "rectangle":
        return dict(t="rect", a=_xy(_child(node, "start")), b=_xy(_child(node, "end")), **sf)
    if h == "polyline":
        return dict(t="poly", pts=[_xy(p) for p in _children(_child(node, "pts"), "xy")], **sf)
    if h == "bezier":
        return dict(t="bezier", pts=[_xy(p) for p in _children(_child(node, "pts"), "xy")], **sf)
    if h == "circle":
        c = _xy(_child(node, "center"))
        if _child(node, "radius") is not None:
            r = float(_child(node, "radius")[1])
        else:  # faebryk_convert writes (end x y): a point on the circumference
            e = _xy(_child(node, "end"))
            r = math.hypot(e[0] - c[0], e[1] - c[1])
        return dict(t="circle", c=c, r=r, **sf)
    if h == "arc":
        s, e = _xy(_child(node, "start")), _xy(_child(node, "end"))
        mid = _child(node, "mid")
        if mid is not None:
            m = _xy(mid)
        else:  # legacy (radius (at) (length) (angles a0 a1))
            rad = _child(node, "radius")
            c = _xy(_child(rad, "at"))
            r = float(_child(rad, "length")[1])
            a0, a1 = (float(v) for v in _child(rad, "angles")[1:3])
            am = math.radians((a0 + a1) / 2)
            m = [c[0] + r * math.cos(am), c[1] - r * math.sin(am)]
        return dict(t="arc", s=s, m=m, e=e, **sf)
    if h == "text":
        at = _child(node, "at")
        eff = _child(node, "effects")
        size = 1.27
        if eff is not None and _child(eff, "font") is not None and _child(_child(eff, "font"), "size"):
            size = float(_child(_child(eff, "font"), "size")[1])
        return dict(t="text", at=[float(at[1]), -float(at[2])], rot=float(at[3]) if len(at) > 3 else 0.0,
                    text=str(node[1]), size=size)
    return None


def _pin(node):
    at = _child(node, "at")
    x, y = float(at[1]), float(at[2])
    rot = int(round(float(at[3]))) % 360 if len(at) > 3 else 0
    length = float(_child(node, "length")[1]) if _child(node, "length") else 2.54
    tip_k = (x + length * math.cos(math.radians(rot)), y + length * math.sin(math.radians(rot)))
    name = _child(node, "name")
    number = _child(node, "number")
    return dict(number=str(number[1]) if number else "", name=str(name[1]) if name else "",
                etype=str(node[1]) if len(node) > 1 and not isinstance(node[1], list) else "unspecified",
                shape=str(node[2]) if len(node) > 2 and not isinstance(node[2], list) else "line",
                at=[x, -y], tip=[round(tip_k[0], 4), round(-tip_k[1], 4)], len=length, rot=rot,
                side=_SIDE.get(rot, "W"), hidden=_flag(node, "hide"))


def _bbox(graphics, pins):
    xs, ys = [], []
    for g in graphics:
        if g["t"] == "rect":
            xs += [g["a"][0], g["b"][0]]; ys += [g["a"][1], g["b"][1]]
        elif g["t"] in ("poly", "bezier"):
            xs += [p[0] for p in g["pts"]]; ys += [p[1] for p in g["pts"]]
        elif g["t"] == "circle":
            xs += [g["c"][0] - g["r"], g["c"][0] + g["r"]]; ys += [g["c"][1] - g["r"], g["c"][1] + g["r"]]
        elif g["t"] == "arc":
            xs += [g["s"][0], g["m"][0], g["e"][0]]; ys += [g["s"][1], g["m"][1], g["e"][1]]
        elif g["t"] == "text":
            xs.append(g["at"][0]); ys.append(g["at"][1])
    for p in pins:
        xs += [p["at"][0], p["tip"][0]]; ys += [p["at"][1], p["tip"][1]]
    if not xs:
        return [0.0, 0.0, 0.0, 0.0]
    return [round(min(xs), 4), round(min(ys), 4), round(max(xs), 4), round(max(ys), 4)]


def _prop(sym, key):
    for p in _children(sym, "property"):
        if len(p) > 2 and p[1] == key:
            return str(p[2])
    return None


def parse_library(text: str, lib: str = ""):
    """All top-level symbols of one .kicad_sym file -> list of symbol-v1 dicts."""
    root = parse_sexpr(text)
    if _head(root) != "kicad_symbol_lib":
        raise ValueError("not a kicad_symbol_lib")
    raw = {str(s[1]): s for s in _children(root, "symbol")}
    out = []
    for name, sym in raw.items():
        base = sym
        ext = _child(sym, "extends")
        if ext is not None and str(ext[1]) in raw:  # derived symbol: graphics from parent
            base = raw[str(ext[1])]
        units = {}
        for sub in _children(base, "symbol"):
            m = re.match(r"^(.*)_(\d+)_(\d+)$", str(sub[1]))
            if not m:
                continue
            unit, style = int(m.group(2)), int(m.group(3))
            if style not in (0, 1):  # skip De Morgan alternate bodies
                continue
            u = units.setdefault(unit, dict(graphics=[], pins=[]))
            for c in sub[1:]:
                if not isinstance(c, list):
                    continue
                if c[0] == "pin":
                    u["pins"].append(_pin(c))
                else:
                    g = _graphic(c)
                    if g:
                        u["graphics"].append(g)
        common = units.pop(0, dict(graphics=[], pins=[]))
        if not units:
            units = {1: dict(graphics=[], pins=[])}
        unit_list = []
        for k in sorted(units):
            g = common["graphics"] + units[k]["graphics"]
            p = common["pins"] + units[k]["pins"]
            unit_list.append(dict(unit=k, bbox=_bbox(g, p), graphics=g, pins=p))
        pn = _child(sym, "pin_names") or _child(base, "pin_names")
        pnum = _child(sym, "pin_numbers") or _child(base, "pin_numbers")
        offset = 0.508
        if pn is not None and _child(pn, "offset") is not None:
            offset = float(_child(pn, "offset")[1])
        out.append(dict(schema="schematic-symbol-v1", lib=lib, name=name,
                        ref_prefix=_prop(sym, "Reference") or "", value=_prop(sym, "Value") or name,
                        lcsc=_prop(sym, "LCSC Part"),
                        pin_numbers_hidden=bool(pnum is not None and _flag(pnum, "hide")),
                        pin_names_hidden=bool(pn is not None and _flag(pn, "hide")),
                        pin_name_offset=offset, units=unit_list))
    return out


def load_part_symbol(part_dir: Path):
    """The (single) symbol of an atopile part folder.

    The .kicad_sym is named after the MPN, not the folder; the part .ato's
    ``is_atomic_part<... symbol="X.kicad_sym">`` trait is authoritative, else the
    only .kicad_sym in the folder.
    """
    part_dir = Path(part_dir)
    sym_file = None
    for ato in part_dir.glob("*.ato"):
        m = re.search(r'is_atomic_part<[^>]*symbol="([^"]+)"', ato.read_text())
        if m and (part_dir / m.group(1)).exists():
            sym_file = part_dir / m.group(1)
            break
    if sym_file is None:
        files = sorted(part_dir.glob("*.kicad_sym"))
        if len(files) != 1:
            raise FileNotFoundError("%s: %d .kicad_sym files" % (part_dir, len(files)))
        sym_file = files[0]
    syms = parse_library(sym_file.read_text(), lib=part_dir.name)
    if len(syms) != 1:
        raise ValueError("%s: %d symbols" % (sym_file, len(syms)))
    s = syms[0]
    s["file"] = sym_file.name
    return s


def load_part_signals(part_dir: Path):
    """atopile signal names per pad number from the part .ato (``signal X ~ pin N`` / ``X ~ pin N``)."""
    out = {}
    for ato in Path(part_dir).glob("*.ato"):
        for m in re.finditer(r"^\s*(?:signal\s+)?([A-Za-z_][\w]*)\s*~\s*pin\s+(\S+)", ato.read_text(), re.M):
            out.setdefault(m.group(2), m.group(1))
    return out


# --------------------------------------------------------------------------------
# Viewer additions: generic fallback symbols and a per-library parse cache.

PARSER_VERSION = "schematic-sym-2"
GRID = 2.54


def _natural(s):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", str(s))]


def generic_symbol(lib, pads, signals=None, reason=""):
    """A box symbol drawn mechanically from the part's pads.

    Used when a part folder has no usable .kicad_sym. ``pads`` is a list of pad
    names (duplicates collapse); pin names come from the atopile ``signal X ~ pin N``
    lines when available, else the pad number. Two-pin parts become a small
    two-terminal body so passives still read as passives.
    """
    signals = signals or {}
    numbers = sorted({str(p) for p in pads if str(p) != ""}, key=_natural)
    pins = []
    if len(numbers) <= 2:
        graphics = [dict(t="rect", a=[-1.27, -0.762], b=[1.27, 0.762], fill="background", w=0.254)]
        for i, n in enumerate(numbers):
            x = -GRID if i == 0 else GRID
            side = "W" if i == 0 else "E"
            tip = [-1.27, 0.0] if i == 0 else [1.27, 0.0]
            pins.append(dict(number=n, name=signals.get(n, ""), etype="passive", shape="line",
                             at=[x * 1.5, 0.0], tip=tip, len=abs(x * 1.5 - tip[0]), rot=0 if i == 0 else 180,
                             side=side, hidden=False))
        names_hidden = True
    else:
        half = (len(numbers) + 1) // 2
        left, right = numbers[:half], numbers[half:]
        label = max([len(signals.get(n, n)) for n in numbers] + [2])
        w = max(7.62, round((label * 1.1 * 2 + 2.54) / GRID) * GRID)
        h = GRID * (max(len(left), len(right)) + 1)
        x0, x1, y0 = -w / 2, w / 2, -h / 2
        graphics = [dict(t="rect", a=[x0, y0], b=[x1, y0 + h], fill="background", w=0.254)]
        for col, side, xs in ((left, "W", x0), (right, "E", x1)):
            for i, n in enumerate(col):
                y = y0 + GRID * (i + 1)
                at = [xs - GRID, y] if side == "W" else [xs + GRID, y]
                pins.append(dict(number=n, name=signals.get(n, n), etype="unspecified", shape="line",
                                 at=at, tip=[xs, y], len=GRID, rot=0 if side == "W" else 180, side=side,
                                 hidden=False))
        names_hidden = False
    unit = dict(unit=1, bbox=_bbox(graphics, pins), graphics=graphics, pins=pins)
    return dict(schema="schematic-symbol-v1", lib=lib, name=lib, ref_prefix="", value=lib, lcsc=None,
                pin_numbers_hidden=False, pin_names_hidden=names_hidden, pin_name_offset=0.508,
                units=[unit], file=None, generic=True, generic_reason=reason)


def _symbol_file(part_dir):
    part_dir = Path(part_dir)
    for ato in sorted(part_dir.glob("*.ato")):
        m = re.search(r'is_atomic_part<[^>]*symbol="([^"]+)"', ato.read_text(errors="replace"))
        if m and (part_dir / m.group(1)).is_file():
            return part_dir / m.group(1)
    files = sorted(part_dir.glob("*.kicad_sym"))
    return files[0] if len(files) == 1 else None


def cached_part_symbol(parts_dir, lib, cache_dir=None):
    """(symbol, signals) for part folder ``lib``, parse results cached per library.

    The cache entry is keyed by the symbol file's content hash and the parser
    version, so edited libraries re-parse and unchanged ones never do. Returns
    ``(None, signals, reason)`` when no symbol can be parsed; callers then draw a
    generic box from the pads.
    """
    import hashlib
    import json
    part_dir = Path(parts_dir) / lib
    if not lib or "/" in lib or lib.startswith(".") or not part_dir.is_dir():
        return None, {}, "no part folder %r" % lib
    try:
        signals = load_part_signals(part_dir)
    except OSError:
        signals = {}
    sym_file = _symbol_file(part_dir)
    if sym_file is None:
        return None, signals, "no unique .kicad_sym in %s" % lib
    raw = sym_file.read_bytes()
    key = hashlib.sha256(raw + PARSER_VERSION.encode()).hexdigest()[:24]
    entry = Path(cache_dir) / ("%s-%s.json" % (lib, key)) if cache_dir else None
    if entry is not None and entry.is_file():
        try:
            return json.loads(entry.read_text()), signals, ""
        except ValueError:
            pass
    try:
        syms = parse_library(raw.decode("utf-8", errors="replace"), lib=lib)
        if len(syms) != 1:
            raise ValueError("%d symbols in %s" % (len(syms), sym_file.name))
        sym = syms[0]
        sym["file"] = sym_file.name
        if not any(u["pins"] for u in sym["units"]):
            raise ValueError("symbol has no pins")
    except Exception as ex:  # malformed library: fall back to a generic box
        return None, signals, "parse failed: %s" % ex
    if entry is not None:
        entry.parent.mkdir(parents=True, exist_ok=True)
        tmp = entry.with_name(entry.name + ".tmp%d" % os.getpid())
        tmp.write_text(json.dumps(sym, separators=(",", ":")))
        tmp.replace(entry)
    return sym, signals, ""
