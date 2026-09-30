"""A stdlib reader for saved ``.kicad_pcb`` boards, for :mod:`pnr.trace` and :mod:`pnr.animate`.

:func:`read` extracts what a picture of a board needs, in the engine frame of
:mod:`pnr.graph` (integer micrometres, y up, origin at the bottom-left corner of the
``Edge.Cuts`` extent, as :func:`pnr.ingest._board_frame` uses the physical contour): the copper
layers, ``segment`` and ``arc`` tracks (arcs tessellated), vias, the ``filled_polygon`` of
zones, and footprints with their pads. Nets may be written as codes (KiCad 9 and older, with a
net table) or as names (KiCad 10). No KiCad process is involved, so it runs in the controller
and in CI outside the KiCad image. Stdlib only and parseable by Python 3.9.

Not a general KiCad parser: footprint graphics, text, custom pad primitives and keep-out areas
are ignored; a custom pad is drawn as its anchor rectangle.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

_TOKEN = re.compile(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()"]+')
_ESCAPE = re.compile(r"\\(.)")
ARC_SEGMENTS = 8


class Atom(str):
    """An unquoted token (keyword or number), as opposed to a quoted string."""


def parse(text):
    """Nested lists of an S-expression; quoted strings are ``str``, bare tokens :class:`Atom`."""
    stack, current = [], []
    for match in _TOKEN.finditer(text):
        token = match.group(0)
        if token == "(":
            stack.append(current)
            current = []
        elif token == ")":
            if not stack:
                raise ValueError("unbalanced S-expression")
            done, current = current, stack.pop()
            current.append(done)
        elif token[0] == '"':
            current.append(_ESCAPE.sub(r"\1", token[1:-1]))
        else:
            current.append(Atom(token))
    if stack:
        raise ValueError("unbalanced S-expression")
    if len(current) != 1:
        raise ValueError("expected one top-level expression")
    return current[0]


def _head(node):
    return node[0] if isinstance(node, list) and node else None


def _children(node, name):
    return [c for c in node[1:] if isinstance(c, list) and c and c[0] == name]


def _child(node, name):
    for c in node[1:]:
        if isinstance(c, list) and c and c[0] == name:
            return c
    return None


def _value(node, name, default=None):
    c = _child(node, name)
    return c[1] if c is not None and len(c) > 1 else default


def _floats(node, name):
    c = _child(node, name)
    return [float(v) for v in c[1:] if not isinstance(v, list)] if c is not None else None


def _layers(node):
    c = _child(node, "layers")
    if c is not None:
        return [str(v) for v in c[1:] if not isinstance(v, list)]
    layer = _value(node, "layer")
    return [str(layer)] if layer is not None else []


def _circle(a, b, c):
    """Centre and radius of the circle through three points, or None if collinear."""
    (ax, ay), (bx, by), (cx, cy) = a, b, c
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:
        return None
    ux = (
        (ax * ax + ay * ay) * (by - cy)
        + (bx * bx + by * by) * (cy - ay)
        + (cx * cx + cy * cy) * (ay - by)
    ) / d
    uy = (
        (ax * ax + ay * ay) * (cx - bx)
        + (bx * bx + by * by) * (ax - cx)
        + (cx * cx + cy * cy) * (bx - ax)
    ) / d
    return (ux, uy), math.hypot(ax - ux, ay - uy)


def arc_points(start, mid, end, segments=ARC_SEGMENTS):
    """Points along the arc start -> mid -> end (a straight line if they are collinear)."""
    circle = _circle(start, mid, end)
    if circle is None:
        return [start, end]
    (ux, uy), r = circle
    a0 = math.atan2(start[1] - uy, start[0] - ux)
    am = math.atan2(mid[1] - uy, mid[0] - ux)
    a1 = math.atan2(end[1] - uy, end[0] - ux)

    def sweep(a, b):
        return (b - a) % (2 * math.pi)

    # Go the direction that passes through mid.
    if sweep(a0, am) <= sweep(a0, a1):
        total = sweep(a0, a1)
    else:
        total = sweep(a0, a1) - 2 * math.pi
    return [
        (ux + r * math.cos(a0 + total * k / segments), uy + r * math.sin(a0 + total * k / segments))
        for k in range(segments + 1)
    ]


def _edge_points(tree):
    points = []
    for item in tree[1:]:
        head = _head(item)
        if head not in ("gr_line", "gr_rect", "gr_arc", "gr_circle", "gr_poly", "gr_curve"):
            continue
        if "Edge.Cuts" not in _layers(item):
            continue
        if head == "gr_circle":
            (cx, cy), (ex, ey) = _floats(item, "center"), _floats(item, "end")
            r = math.hypot(ex - cx, ey - cy)
            points += [(cx - r, cy - r), (cx + r, cy + r)]
            continue
        for key in ("start", "mid", "end"):
            xy = _floats(item, key)
            if xy:
                points.append((xy[0], xy[1]))
        pts = _child(item, "pts")
        if pts is not None:
            points += [(float(p[1]), float(p[2])) for p in _children(pts, "xy")]
    return points


def copper_layers(tree):
    """Copper layer names of the board, outer to outer (``F.Cu``, ``In1.Cu``..., ``B.Cu``)."""
    names = []
    layers = _child(tree, "layers")
    for entry in layers[1:] if layers is not None else []:
        if isinstance(entry, list) and len(entry) > 1 and str(entry[1]).endswith(".Cu"):
            names.append(str(entry[1]))

    def order(name):
        if name == "F.Cu":
            return (0, 0)
        if name == "B.Cu":
            return (2, 0)
        digits = re.findall(r"\d+", name)
        return (1, int(digits[0]) if digits else 0)

    return sorted(set(names), key=order) or ["F.Cu", "B.Cu"]


class Board:
    """Frame conversion and net lookup for one parsed board."""

    def __init__(self, tree):
        if _head(tree) != "kicad_pcb":
            raise ValueError("not a kicad_pcb file")
        self.tree = tree
        self.layers = copper_layers(tree)
        self.net_names = {}
        for net in _children(tree, "net"):
            if len(net) >= 3:
                self.net_names[str(net[1])] = str(net[2])
        points = _edge_points(tree)
        if not points:
            points = [tuple(_floats(fp, "at")[:2]) for fp in _children(tree, "footprint")]
        if not points:
            points = [(0.0, 0.0)]
        self.left = min(p[0] for p in points)
        self.right = max(p[0] for p in points)
        self.top = min(p[1] for p in points)
        self.bottom = max(p[1] for p in points)

    def xy(self, x, y):
        """KiCad millimetres (y down) to engine micrometres (y up)."""
        return int(round((x - self.left) * 1000.0)), int(round((self.bottom - y) * 1000.0))

    def net(self, node):
        value = _value(node, "net")
        if value is None:
            return ""
        if isinstance(value, Atom) and str(value) in self.net_names:
            return self.net_names[str(value)]
        return "" if isinstance(value, Atom) and str(value) == "0" else str(value)

    def layer(self, name):
        return self.layers.index(name) if name in self.layers else None


def _rotate(x, y, degrees):
    """KiCad rotation (counter-clockwise on screen, y down) of a local offset."""
    a = math.radians(degrees)
    c, s = math.cos(a), math.sin(a)
    return x * c + y * s, -x * s + y * c


def _footprint(board, fp):
    at = _floats(fp, "at") or [0.0, 0.0]
    fx, fy, fa = at[0], at[1], at[2] if len(at) > 2 else 0.0
    side = "bottom" if _value(fp, "layer") == "B.Cu" else "top"
    ref, value = "", ""
    for prop in _children(fp, "property"):
        if len(prop) > 2 and prop[1] == "Reference":
            ref = str(prop[2])
        elif len(prop) > 2 and prop[1] == "Value":
            value = str(prop[2])
    for text in _children(fp, "fp_text"):
        if len(text) > 2 and text[1] == "reference" and not ref:
            ref = str(text[2])
        elif len(text) > 2 and text[1] == "value" and not value:
            value = str(text[2])
    pads = []
    for pad in _children(fp, "pad"):
        if len(pad) < 4:
            continue
        pat = _floats(pad, "at") or [0.0, 0.0]
        px, py, pa = pat[0], pat[1], pat[2] if len(pat) > 2 else 0.0
        dx, dy = _rotate(px, py, fa)
        x, y = board.xy(fx + dx, fy + dy)
        size = _floats(pad, "size") or [0.0, 0.0]
        drill = _child(pad, "drill")
        drill_size = None
        if drill is not None:
            numbers = [float(v) for v in drill[1:] if not isinstance(v, list) and v != "oval"]
            if numbers:
                drill_size = [numbers[0], numbers[1] if len(numbers) > 1 else numbers[0]]
        shape = str(pad[3])
        ratio = float(_value(pad, "roundrect_rratio", 0.25 if shape == "roundrect" else 0.0))
        layers = _layers(pad)
        pads.append(
            dict(
                name=str(pad[1]),
                kind=str(pad[2]),
                shape=shape,
                x=x,
                y=y,
                size=[int(round(size[0] * 1000)), int(round(size[1] * 1000))],
                angle=round(pa % 360.0, 3),
                local_angle=round((pa - fa) % 360.0, 3),
                corner=int(round(ratio * min(size) * 1000)) if shape == "roundrect" else None,
                drill=[int(round(d * 1000)) for d in drill_size] if drill_size else None,
                net=board.net(pad),
                layers=layers,
            )
        )
    x, y = board.xy(fx, fy)
    return dict(ref=ref, value=value, x=x, y=y, rot=round(fa % 360.0, 3), side=side, pads=pads)


def read_tree(tree):
    """The contents of a parsed board (see :func:`read`)."""
    board = Board(tree)
    tracks, vias, zones = [], [], []
    for item in tree[1:]:
        head = _head(item)
        if head == "segment" or head == "arc":
            layer = board.layer(_value(item, "layer"))
            if layer is None:
                continue
            width = int(round(float(_value(item, "width", 0.0)) * 1000))
            start, end = _floats(item, "start"), _floats(item, "end")
            if head == "arc":
                points = arc_points(tuple(start), tuple(_floats(item, "mid")), tuple(end))
            else:
                points = [tuple(start), tuple(end)]
            points = [board.xy(*p) for p in points]
            for a, b in zip(points, points[1:]):
                tracks.append([layer, a[0], a[1], b[0], b[1], width])
        elif head == "via":
            at = _floats(item, "at")
            x, y = board.xy(at[0], at[1])
            size = int(round(float(_value(item, "size", 0.0)) * 1000))
            drill = int(round(float(_value(item, "drill", 0.0)) * 1000))
            vias.append([x, y, size, drill])
        elif head == "zone":
            net = board.net(item) or str(_value(item, "net_name", ""))
            for filled in _children(item, "filled_polygon"):
                layer = board.layer(_value(filled, "layer"))
                pts = _child(filled, "pts")
                if layer is None or pts is None:
                    continue
                ring = [list(board.xy(float(p[1]), float(p[2]))) for p in _children(pts, "xy")]
                if len(ring) >= 3:
                    zones.append([layer, net, [ring]])
    footprints = [_footprint(board, fp) for fp in _children(tree, "footprint")]
    poses = sorted([f["ref"], f["x"], f["y"], f["rot"], f["side"]] for f in footprints if f["ref"])
    return dict(
        layers=board.layers,
        frame=(board.left, board.bottom),
        size=[
            int(round((board.right - board.left) * 1000)),
            int(round((board.bottom - board.top) * 1000)),
        ],
        copper=dict(tracks=sorted(tracks), vias=sorted(vias), zones=zones),
        footprints=footprints,
        poses=poses,
    )


def read(path):
    """Parse a ``.kicad_pcb`` file: ``layers``, ``frame`` (KiCad mm of the engine origin: left
    and bottom), ``size`` (µm), ``copper`` (a ``pnr.trace`` copper blob), ``footprints`` (pads
    at absolute positions) and ``poses`` (``[[ref, x, y, rot, side]]``)."""
    return read_tree(parse(Path(path).read_text(encoding="utf-8")))


def refine_header(header, parsed):
    """Pad shapes, corner radii, local pad angles and part values of ``header`` (a trace header
    dict) from a parsed source board with the same parts; returns the header."""
    by_ref = {f["ref"]: f for f in parsed["footprints"]}
    for comp in header.get("components", []):
        fp = by_ref.get(comp["ref"])
        if fp is None:
            continue
        if fp["value"]:
            comp["value"] = fp["value"]
        pads = {}
        for pad in fp["pads"]:
            pads.setdefault(pad["name"], pad)
        for pad in comp.get("pads", []):
            src = pads.get(pad["name"])
            if src is None:
                continue
            shape = (
                src["shape"] if src["shape"] in ("rect", "roundrect", "circle", "oval") else "rect"
            )
            pad["shape"] = shape
            pad["corner"] = src["corner"]
            pad["angle"] = src["local_angle"]
            pad["size"] = list(src["size"])
            if src["drill"]:
                pad["drill"] = list(src["drill"])
    return header
