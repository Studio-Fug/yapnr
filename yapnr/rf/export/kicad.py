"""The KiCad footprint of a design (design §10.3), and a minimal reader for round trips.

The `.kicad_mod` follows the format KiCad 10 writes for its own libraries (version 20260206):

- one rectangular SMD pad per port on F.Cu, numbered by port, the feed width × two pixels, at
  the footprint edge;
- each copper island touching two or more port pads: an `fp_poly` on F.Cu (filled, zero width),
  and the footprint lists those pads in `net_tie_pad_groups`, so the port nets may have
  different names (a net tie);
- an island touching one pad: that port's pad becomes a custom pad (anchor = the port pad)
  with the island as a `gr_poly` primitive;
- an island touching no pad: an `fp_poly` on F.Cu (netless);
- F.CrtYd and F.Fab rectangles on the design region, `(attr smd exclude_from_pos_files
  exclude_from_bom)`, and a description naming the stackup the design assumes and the spec
  sha256.

The origin is the design-region centre; KiCad's y axis points down, so y_kicad = −(y − y_c).
UUIDs are uuid5 of the spec hash and the item index, so the file is deterministic.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import numpy as np

FORMAT_VERSION = "20260206"
_NS = uuid.UUID("6f1c2d0e-8a51-5c43-9b7e-2f4c1e0a9d37")


@dataclass
class PortPad:
    """A port pad: number, centre (mm, board coordinates, y up) and size (mm)."""

    number: int
    center: tuple[float, float]
    size: tuple[float, float]


@dataclass
class Footprint:
    """What the writer needs (board coordinates in mm, y up)."""

    name: str
    origin: tuple[float, float]
    region: tuple[float, float, float, float]  # x0, x1, y0, y1
    pads: list
    islands: list  # [(polygon (k, 2) mm, sorted pad numbers touched)]
    description: str = ""
    tags: str = "rf microstrip inverse-design"
    seed: str = ""
    extra: dict = field(default_factory=dict)


def _fmt(v: float) -> str:
    s = f"{v:.6f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _q(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


class _Writer:
    def __init__(self, fp: Footprint):
        self.fp = fp
        self.lines: list[str] = []
        self.count = 0

    def uuid(self) -> str:
        self.count += 1
        return str(uuid.uuid5(_NS, f"{self.fp.seed}:{self.fp.name}:{self.count}"))

    def xy(self, x: float, y: float) -> str:
        ox, oy = self.fp.origin
        return f"{_fmt(x - ox)} {_fmt(-(y - oy))}"

    def emit(self, depth: int, text: str) -> None:
        self.lines.append("\t" * depth + text)

    def pts(self, depth: int, poly, rel=(0.0, 0.0)) -> None:
        self.emit(depth, "(pts")
        row = []
        for x, y in poly:
            if rel != (0.0, 0.0):
                row.append(f"(xy {_fmt(x - rel[0])} {_fmt(-(y - rel[1]))})")
            else:
                row.append(f"(xy {self.xy(x, y)})")
            if len(row) == 6:
                self.emit(depth + 1, " ".join(row))
                row = []
        if row:
            self.emit(depth + 1, " ".join(row))
        self.emit(depth, ")")

    def prop(self, name: str, value: str, at_y: float, layer: str, hide: bool) -> None:
        self.emit(1, f"(property {_q(name)} {_q(value)}")
        self.emit(2, f"(at 0 {_fmt(at_y)} 0)")
        if name in ("Datasheet", "Description"):
            self.emit(2, "(unlocked yes)")
        self.emit(2, f"(layer {_q(layer)})")
        if hide:
            self.emit(2, "(hide yes)")
        self.emit(2, f'(uuid "{self.uuid()}")')
        self.emit(2, "(effects")
        self.emit(3, "(font")
        self.emit(4, "(size 1 1)")
        self.emit(4, "(thickness 0.15)")
        self.emit(3, ")")
        self.emit(2, ")")
        self.emit(1, ")")

    def rect(self, layer: str, width: float) -> None:
        x0, x1, y0, y1 = self.fp.region
        self.emit(1, "(fp_rect")
        self.emit(2, f"(start {self.xy(x0, y1)})")
        self.emit(2, f"(end {self.xy(x1, y0)})")
        self.emit(2, "(stroke")
        self.emit(3, f"(width {_fmt(width)})")
        self.emit(3, "(type solid)")
        self.emit(2, ")")
        self.emit(2, "(fill no)")
        self.emit(2, f"(layer {_q(layer)})")
        self.emit(2, f'(uuid "{self.uuid()}")')
        self.emit(1, ")")


def write_footprint(fp: Footprint, path: str | None = None) -> str:
    """The `.kicad_mod` text of `fp` (also written to `path` when given)."""
    w = _Writer(fp)
    x0, x1, y0, y1 = fp.region
    half_h = 0.5 * (y1 - y0)
    single = {}  # pad number → island polygon (islands touching exactly one pad)
    ties, free = [], []
    for poly, touched in fp.islands:
        if len(touched) >= 2:
            ties.append((poly, touched))
        elif len(touched) == 1 and touched[0] not in single:
            single[touched[0]] = poly
        else:
            free.append(poly)
    w.emit(0, f"(footprint {_q(fp.name)}")
    w.emit(1, f"(version {FORMAT_VERSION})")
    w.emit(1, '(generator "yapnr")')
    w.emit(1, '(generator_version "rf")')
    w.emit(1, '(layer "F.Cu")')
    w.emit(1, f"(descr {_q(fp.description)})")
    w.emit(1, f"(tags {_q(fp.tags)})")
    w.prop("Reference", "REF**", -(half_h + 1.2), "F.SilkS", False)
    w.prop("Value", fp.name, half_h + 1.2, "F.Fab", False)
    w.prop("Datasheet", "", 0.0, "F.Fab", True)
    w.prop("Description", fp.description, 0.0, "F.Fab", True)
    w.emit(1, "(attr smd exclude_from_pos_files exclude_from_bom)")
    if ties:
        groups = " ".join(_q(", ".join(str(n) for n in t)) for _, t in ties)
        w.emit(1, f"(net_tie_pad_groups {groups})")
    w.emit(1, "(duplicate_pad_numbers_are_jumpers no)")
    for poly in [p for p, _ in ties] + free:
        w.emit(1, "(fp_poly")
        w.pts(2, poly)
        w.emit(2, "(stroke")
        w.emit(3, "(width 0)")
        w.emit(3, "(type solid)")
        w.emit(2, ")")
        w.emit(2, "(fill yes)")
        w.emit(2, '(layer "F.Cu")')
        w.emit(2, f'(uuid "{w.uuid()}")')
        w.emit(1, ")")
    w.rect("F.CrtYd", 0.05)
    w.rect("F.Fab", 0.1)
    for pad in sorted(fp.pads, key=lambda p: p.number):
        cx, cy = pad.center
        sx, sy = pad.size
        if pad.number in single:
            w.emit(1, f'(pad "{pad.number}" smd custom')
            w.emit(2, f"(at {w.xy(cx, cy)})")
            w.emit(2, f"(size {_fmt(min(sx, sy))} {_fmt(min(sx, sy))})")
            w.emit(2, '(layers "F.Cu")')
            w.emit(2, "(options")
            w.emit(3, "(clearance outline)")
            w.emit(3, "(anchor rect)")
            w.emit(2, ")")
            w.emit(2, "(primitives")
            w.emit(3, "(gr_poly")
            w.pts(4, single[pad.number], rel=(cx, cy))
            w.emit(4, "(width 0)")
            w.emit(4, "(fill yes)")
            w.emit(3, ")")
            w.emit(2, ")")
        else:
            w.emit(1, f'(pad "{pad.number}" smd rect')
            w.emit(2, f"(at {w.xy(cx, cy)})")
            w.emit(2, f"(size {_fmt(sx)} {_fmt(sy)})")
            w.emit(2, '(layers "F.Cu")')
        w.emit(2, f'(uuid "{w.uuid()}")')
        w.emit(1, ")")
    w.emit(1, "(embedded_fonts no)")
    w.emit(0, ")")
    text = "\n".join(w.lines) + "\n"
    if path:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    return text


# -- reader -----------------------------------------------------------------------------------


def parse_sexpr(text: str):
    """S-expression text → nested lists of strings (quoted strings unquoted)."""
    tokens = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in "()":
            tokens.append(c)
            i += 1
        elif c.isspace():
            i += 1
        elif c == '"':
            j = i + 1
            buf = []
            while text[j] != '"':
                if text[j] == "\\":
                    j += 1
                buf.append(text[j])
                j += 1
            tokens.append(("str", "".join(buf)))
            i = j + 1
        else:
            j = i
            while j < n and not text[j].isspace() and text[j] not in "()":
                j += 1
            tokens.append(text[i:j])
            i = j
    stack: list[list] = [[]]
    for t in tokens:
        if t == "(":
            stack.append([])
        elif t == ")":
            done = stack.pop()
            stack[-1].append(done)
        else:
            stack[-1].append(t[1] if isinstance(t, tuple) else t)
    if len(stack) != 1 or len(stack[0]) != 1:
        raise ValueError("unbalanced S-expression")
    return stack[0][0]


def _children(node, key):
    return [c for c in node if isinstance(c, list) and c and c[0] == key]


def _child(node, key):
    found = _children(node, key)
    return found[0] if found else None


def _points(node) -> np.ndarray:
    pts = _child(node, "pts")
    return np.array([[float(p[1]), float(p[2])] for p in pts[1:] if p[0] == "xy"])


@dataclass
class ReadFootprint:
    """A parsed footprint in KiCad coordinates (mm, y down, relative to the origin)."""

    name: str
    description: str
    net_tie_groups: list
    pads: list  # dicts: number, type, shape, at, size, primitives (polygons, absolute)
    polygons: list  # fp_poly on F.Cu: (k, 2) arrays

    def copper(self) -> list[np.ndarray]:
        """Every copper polygon (fp_poly and custom-pad primitives) plus rectangular pads."""
        out = list(self.polygons)
        for p in self.pads:
            out.extend(p["primitives"])
            if p["shape"] == "rect":
                (cx, cy), (sx, sy) = p["at"], p["size"]
                out.append(
                    np.array(
                        [
                            [cx - sx / 2, cy - sy / 2],
                            [cx + sx / 2, cy - sy / 2],
                            [cx + sx / 2, cy + sy / 2],
                            [cx - sx / 2, cy + sy / 2],
                        ]
                    )
                )
        return out


def read_footprint(path_or_text: str) -> ReadFootprint:
    text = path_or_text
    if "\n" not in path_or_text and not path_or_text.lstrip().startswith("("):
        with open(path_or_text, encoding="utf-8") as fh:
            text = fh.read()
    tree = parse_sexpr(text)
    if tree[0] != "footprint":
        raise ValueError("not a footprint")
    descr = _child(tree, "descr")
    ties = _child(tree, "net_tie_pad_groups")
    pads = []
    for p in _children(tree, "pad"):
        at = _child(p, "at")
        size = _child(p, "size")
        cx, cy = float(at[1]), float(at[2])
        prims = []
        pr = _child(p, "primitives")
        if pr is not None:
            for gp in _children(pr, "gr_poly"):
                prims.append(_points(gp) + np.array([cx, cy]))
        pads.append(
            {
                "number": p[1],
                "type": p[2],
                "shape": p[3],
                "at": (cx, cy),
                "size": (float(size[1]), float(size[2])),
                "primitives": prims,
            }
        )
    polys = []
    for fpoly in _children(tree, "fp_poly"):
        layer = _child(fpoly, "layer")
        if layer and layer[1] == "F.Cu":
            polys.append(_points(fpoly))
    return ReadFootprint(
        name=tree[1],
        description=descr[1] if descr else "",
        net_tie_groups=[g for g in ties[1:]] if ties else [],
        pads=pads,
        polygons=polys,
    )
