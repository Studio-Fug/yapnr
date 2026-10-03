"""``yapnr fab preview``: render a gerber zip (RS-274X and Excellon) to SVG, without KiCad.

A look at what the vendor will receive, read back from the files themselves rather than from the
board: one SVG per layer and a top and a bottom composite (outline, copper, mask openings, silk,
drills). It reads the subset of RS-274X that KiCad writes (standard apertures, aperture macros
with primitives 1, 4, 5, 20 and 21, linear and circular interpolation, regions, polarity) and
Excellon drill files (tools, holes, G85 slots, inch or metric). It is a preview, not a CAM tool:
vendors render the files with their own viewers, and their previews are the check before paying.
Stdlib only.
"""

from __future__ import annotations

import math
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from yapnr.fab.export import layer_of_function

Point = Tuple[float, float]


class PreviewError(ValueError):
    """A gerber or drill file this reader cannot render."""


# ------------------------------------------------------------------------------- shapes


@dataclass
class Layer:
    """Rendered shapes of one file, in mm with y up: (dark, svg element) in drawing order."""

    name: str
    function: Optional[str]
    items: List[Tuple[bool, str]] = field(default_factory=list)
    bbox: List[float] = field(default_factory=lambda: [math.inf, math.inf, -math.inf, -math.inf])

    def grow(self, x: float, y: float, r: float = 0.0) -> None:
        b = self.bbox
        b[0], b[1] = min(b[0], x - r), min(b[1], y - r)
        b[2], b[3] = max(b[2], x + r), max(b[3], y + r)


def _f(v: float) -> str:
    return f"{v:.4f}".rstrip("0").rstrip(".")


def _poly(points: Sequence[Point]) -> str:
    return "M" + " L".join(f"{_f(x)},{_f(-y)}" for x, y in points) + " Z"


def _rot(p: Point, deg: float) -> Point:
    if not deg:
        return p
    a = math.radians(deg)
    return (p[0] * math.cos(a) - p[1] * math.sin(a), p[0] * math.sin(a) + p[1] * math.cos(a))


# ------------------------------------------------------------------------------ apertures


def _eval(expr: str, env: Dict[int, float]) -> float:
    expr = expr.strip().replace("x", "*").replace("X", "*")
    expr = re.sub(r"\$(\d+)", lambda m: repr(env.get(int(m.group(1)), 0.0)), expr)
    if not re.fullmatch(r"[0-9.+\-*/() eE]*", expr):
        raise PreviewError(f"unsupported macro expression {expr!r}")
    return float(eval(expr, {"__builtins__": {}}, {}))  # noqa: S307 (digits and operators only)


@dataclass
class Aperture:
    kind: str  # C, R, O, P, or a macro name
    params: List[float]
    macro: Optional[List[str]] = None

    def width(self) -> float:
        return self.params[0] if self.params else 0.0

    def flash(self, x: float, y: float) -> List[Tuple[bool, str, float]]:
        """(dark, svg, radius for the bbox) elements of a flash at (x, y)."""
        p = self.params
        if self.kind == "C":
            return [(True, f'<circle cx="{_f(x)}" cy="{_f(-y)}" r="{_f(p[0] / 2)}"/>', p[0] / 2)]
        if self.kind in ("R", "O"):
            w, h = p[0], p[1] if len(p) > 1 else p[0]
            rx = min(w, h) / 2 if self.kind == "O" else 0.0
            return [
                (
                    True,
                    f'<rect x="{_f(x - w / 2)}" y="{_f(-y - h / 2)}" width="{_f(w)}" '
                    f'height="{_f(h)}" rx="{_f(rx)}"/>',
                    max(w, h) / 2,
                )
            ]
        if self.kind == "P":
            d, n = p[0], int(p[1])
            rot = p[2] if len(p) > 2 else 0.0
            pts = [
                (
                    x + d / 2 * math.cos(math.radians(rot + 360.0 * i / n)),
                    y + d / 2 * math.sin(math.radians(rot + 360.0 * i / n)),
                )
                for i in range(n)
            ]
            return [(True, f'<path d="{_poly(pts)}"/>', d / 2)]
        return self._macro(x, y)

    def _macro(self, x: float, y: float) -> List[Tuple[bool, str, float]]:
        env = {i + 1: v for i, v in enumerate(self.params)}
        out = []
        for block in self.macro or []:
            block = block.strip()
            if not block or block.startswith("0"):
                continue
            if block.startswith("$") and "=" in block:
                var, expr = block.split("=", 1)
                env[int(var[1:])] = _eval(expr, env)
                continue
            fields = [_eval(v, env) for v in block.split(",")]
            code, dark = int(fields[0]), bool(fields[1])
            if code == 1:
                dia, cx, cy = fields[2], fields[3], fields[4]
                rot = fields[5] if len(fields) > 5 else 0.0
                cx, cy = _rot((cx, cy), rot)
                svg = f'<circle cx="{_f(x + cx)}" cy="{_f(-(y + cy))}" r="{_f(dia / 2)}"/>'
                out.append((dark, svg, math.hypot(cx, cy) + dia / 2))
            elif code == 4:
                n = int(fields[2])
                pts = [(fields[3 + 2 * i], fields[4 + 2 * i]) for i in range(n + 1)]
                rot = fields[3 + 2 * (n + 1)] if len(fields) > 3 + 2 * (n + 1) else 0.0
                pts = [_rot(q, rot) for q in pts]
                out.append(
                    (
                        dark,
                        f'<path d="{_poly([(x + a, y + b) for a, b in pts])}"/>',
                        max(math.hypot(a, b) for a, b in pts),
                    )
                )
            elif code == 5:
                n, cx, cy, dia = int(fields[2]), fields[3], fields[4], fields[5]
                rot = fields[6] if len(fields) > 6 else 0.0
                pts = [
                    _rot(
                        (
                            cx + dia / 2 * math.cos(2 * math.pi * i / n),
                            cy + dia / 2 * math.sin(2 * math.pi * i / n),
                        ),
                        rot,
                    )
                    for i in range(n)
                ]
                out.append(
                    (
                        dark,
                        f'<path d="{_poly([(x + a, y + b) for a, b in pts])}"/>',
                        dia / 2 + math.hypot(cx, cy),
                    )
                )
            elif code == 20:
                w, x1, y1, x2, y2 = fields[2:7]
                rot = fields[7] if len(fields) > 7 else 0.0
                dx, dy = x2 - x1, y2 - y1
                length = math.hypot(dx, dy) or 1.0
                nx, ny = -dy / length * w / 2, dx / length * w / 2
                pts = [
                    (x1 + nx, y1 + ny),
                    (x2 + nx, y2 + ny),
                    (x2 - nx, y2 - ny),
                    (x1 - nx, y1 - ny),
                ]
                pts = [_rot(q, rot) for q in pts]
                out.append(
                    (
                        dark,
                        f'<path d="{_poly([(x + a, y + b) for a, b in pts])}"/>',
                        max(math.hypot(a, b) for a, b in pts),
                    )
                )
            elif code == 21:
                w, h, cx, cy = fields[2:6]
                rot = fields[6] if len(fields) > 6 else 0.0
                pts = [
                    _rot((cx + sx * w / 2, cy + sy * h / 2), rot)
                    for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))
                ]
                out.append(
                    (
                        dark,
                        f'<path d="{_poly([(x + a, y + b) for a, b in pts])}"/>',
                        max(math.hypot(a, b) for a, b in pts),
                    )
                )
            else:
                raise PreviewError(f"aperture macro primitive {code} is not supported")
        return out


# --------------------------------------------------------------------------------- gerber

_COORD = re.compile(r"([XYIJ])([+-]?\d+)")


def read_gerber(text: str, name: str = "") -> Layer:
    """Shapes of one RS-274X file."""
    layer = Layer(name, None)
    macros: Dict[str, List[str]] = {}
    apertures: Dict[int, Aperture] = {}
    scale = 1.0  # file unit -> mm
    fmt = (4, 6)
    current: Optional[Aperture] = None
    x = y = 0.0
    mode = "G01"
    dark = True
    region: Optional[List[List[str]]] = None
    contour: List[str] = []

    for match in re.finditer(r"%([^%]*)%|([^%*]*)\*", text, re.DOTALL):
        if match.group(1) is not None:
            for block in [b for b in match.group(1).split("*") if b.strip()]:
                block = block.strip()
                if block.startswith("FS"):
                    m = re.search(r"X(\d)(\d)", block)
                    fmt = (int(m.group(1)), int(m.group(2)))
                elif block.startswith("MO"):
                    scale = 25.4 if block[2:4] == "IN" else 1.0
                elif block.startswith("LP"):
                    dark = block[2] == "D"
                elif block.startswith("AM"):
                    body = match.group(1).split("*")
                    macros[body[0][2:].strip()] = [b for b in body[1:] if b.strip()]
                    break
                elif block.startswith("AD"):
                    m = re.match(r"ADD(\d+)([A-Za-z_.$][\w.$-]*?)(?:,(.*))?$", block)
                    if not m:
                        raise PreviewError(f"{name}: bad aperture {block!r}")
                    params = [float(v) * scale for v in (m.group(3) or "").split("X") if v]
                    kind = m.group(2)
                    if kind == "P" and len(params) > 1:
                        params[1] /= scale  # vertex count
                        if len(params) > 2:
                            params[2] /= scale  # rotation
                    apertures[int(m.group(1))] = Aperture(kind, params, macros.get(kind))
                    if kind not in ("C", "R", "O", "P") and kind not in macros:
                        raise PreviewError(f"{name}: unknown aperture macro {kind}")
                elif block.startswith("TF.FileFunction"):
                    layer.function = block.split(",", 1)[1]
            continue
        word = match.group(2).strip()
        if not word or word.startswith("G04"):
            continue
        for g in re.findall(r"G0?([0-9]+)", word.split("X")[0].split("Y")[0].split("D")[0]):
            code = f"G{int(g):02d}"
            if code in ("G01", "G02", "G03"):
                mode = code
            elif code == "G36":
                region, contour = [], []
            elif code == "G37":
                if contour:
                    region.append(contour)
                if region:
                    d = " ".join(" ".join(cont) + " Z" for cont in region)
                    layer.items.append((dark, f'<path d="{d}" fill-rule="nonzero"/>'))
                region, contour = None, []
        if word.startswith("M02"):
            break
        dcode = re.search(r"D(\d+)$", word)
        coords = dict((k, int(v)) for k, v in _COORD.findall(word))
        if dcode and int(dcode.group(1)) >= 10 and not coords:
            current = apertures.get(int(dcode.group(1)))
            if current is None:
                raise PreviewError(f"{name}: undefined aperture D{dcode.group(1)}")
            continue
        if not dcode and not coords:
            continue
        unit = 10.0 ** fmt[1]
        nx = coords["X"] / unit * scale if "X" in coords else x
        ny = coords["Y"] / unit * scale if "Y" in coords else y
        op = int(dcode.group(1)) if dcode else 1
        if op == 2:
            if region is not None:
                if contour:
                    region.append(contour)
                contour = [f"M{_f(nx)},{_f(-ny)}"]
            x, y = nx, ny
            continue
        if op == 3:
            if current is None:
                raise PreviewError(f"{name}: flash without an aperture")
            for d, svg, r in current.flash(nx, ny):
                layer.items.append((d if dark else not d, svg))
                layer.grow(nx, ny, r)
            x, y = nx, ny
            continue
        # D01: interpolate.
        if mode == "G01":
            seg = f"L{_f(nx)},{_f(-ny)}"
        else:
            cx = x + coords.get("I", 0) / unit * scale
            cy = y + coords.get("J", 0) / unit * scale
            r = math.hypot(x - cx, y - cy)
            a0, a1 = math.atan2(y - cy, x - cx), math.atan2(ny - cy, nx - cx)
            sweep = (a1 - a0) % (2 * math.pi) if mode == "G03" else (a0 - a1) % (2 * math.pi)
            if sweep == 0:
                sweep = 2 * math.pi
            # SVG y is flipped: counter-clockwise in gerber is clockwise on screen (sweep 0).
            flag = 0 if mode == "G03" else 1
            if abs(sweep - 2 * math.pi) < 1e-9:
                mx, my = 2 * cx - x, 2 * cy - y
                seg = (
                    f"A{_f(r)},{_f(r)} 0 0,{flag} {_f(mx)},{_f(-my)} "
                    f"A{_f(r)},{_f(r)} 0 0,{flag} {_f(nx)},{_f(-ny)}"
                )
            else:
                large = 1 if sweep > math.pi else 0
                seg = f"A{_f(r)},{_f(r)} 0 {large},{flag} {_f(nx)},{_f(-ny)}"
            layer.grow(cx, cy, r)
        if region is not None:
            if not contour:
                contour = [f"M{_f(x)},{_f(-y)}"]
            contour.append(seg)
        else:
            if current is None:
                raise PreviewError(f"{name}: draw without an aperture")
            w = current.width()
            layer.items.append(
                (
                    dark,
                    f'<path d="M{_f(x)},{_f(-y)} {seg}" fill="none" stroke-width="{_f(w)}" '
                    'stroke-linecap="round" stroke-linejoin="round" class="s"/>',
                )
            )
            layer.grow(x, y, w / 2)
            layer.grow(nx, ny, w / 2)
        if region is not None:
            layer.grow(nx, ny)
        x, y = nx, ny
    return layer


# --------------------------------------------------------------------------------- drills


def read_drill(text: str, name: str = "") -> Layer:
    """Holes (and G85 slots) of one Excellon file, as dark shapes."""
    layer = Layer(name, "Drill")
    scale = 1.0
    tools: Dict[str, float] = {}
    tool = None
    header = True
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(";"):
            continue
        if header:
            if line.startswith("INCH"):
                scale = 25.4
            elif line.startswith("METRIC"):
                scale = 1.0
            m = re.match(r"T(\d+)C([\d.]+)", line)
            if m:
                tools[f"T{int(m.group(1))}"] = float(m.group(2)) * scale
            if line == "%":
                header = False
            continue
        m = re.match(r"^T(\d+)$", line)
        if m:
            tool = f"T{int(m.group(1))}"
            continue
        m = re.match(r"^X(-?[\d.]+)Y(-?[\d.]+)(?:G85X(-?[\d.]+)Y(-?[\d.]+))?$", line)
        if m and tool:
            d = tools[tool]
            x0, y0 = float(m.group(1)) * scale, float(m.group(2)) * scale
            if m.group(3):
                x1, y1 = float(m.group(3)) * scale, float(m.group(4)) * scale
                layer.items.append(
                    (
                        True,
                        f'<path d="M{_f(x0)},{_f(-y0)} L{_f(x1)},{_f(-y1)}" fill="none" '
                        f'stroke-width="{_f(d)}" stroke-linecap="round" class="s"/>',
                    )
                )
                layer.grow(x1, y1, d / 2)
            else:
                layer.items.append(
                    (True, f'<circle cx="{_f(x0)}" cy="{_f(-y0)}" r="{_f(d / 2)}"/>')
                )
            layer.grow(x0, y0, d / 2)
    return layer


# ---------------------------------------------------------------------------------- render

COLOURS = {
    "board": "#0b3d2e",
    "Edge.Cuts": "#e8e8e8",
    "Cu": "#c88a3a",
    "In": "#7a5cc4",
    "Mask": "#3a1f5c",
    "Silkscreen": "#f4f4f4",
    "Paste": "#9aa0a6",
    "Drill": "#101010",
}


def _group(layer: Layer, colour: str, ident: str, box) -> str:
    """The layer as a masked fill: dark shapes white in the mask, clear shapes black."""
    x0, y0, x1, y1 = box
    body = []
    for dark, svg in layer.items:
        fill = "#fff" if dark else "#000"
        svg = svg.replace('class="s"', f'stroke="{fill}"')
        if 'fill="none"' not in svg:
            svg = svg.replace("<", f'<g fill="{fill}"><', 1) + "</g>"
        body.append(svg)
    return (
        f'<mask id="{ident}" maskUnits="userSpaceOnUse" x="{_f(x0)}" y="{_f(-y1)}" '
        f'width="{_f(x1 - x0)}" height="{_f(y1 - y0)}"><rect x="{_f(x0)}" y="{_f(-y1)}" '
        f'width="{_f(x1 - x0)}" height="{_f(y1 - y0)}" fill="#000"/>{"".join(body)}</mask>'
        f'<rect x="{_f(x0)}" y="{_f(-y1)}" width="{_f(x1 - x0)}" height="{_f(y1 - y0)}" '
        f'fill="{colour}" mask="url(#{ident})"/>'
    )


def svg(layers: Sequence[Tuple[Layer, str]], box, title: str, scale_px: float = 20.0) -> str:
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{_f(x0)} {_f(-y1)} {_f(w)} {_f(h)}" '
        f'width="{int(w * scale_px)}" height="{int(h * scale_px)}">',
        f"<title>{title}</title>",
        f'<rect x="{_f(x0)}" y="{_f(-y1)}" width="{_f(w)}" height="{_f(h)}" fill="#1b1b1b"/>',
    ]
    for i, (layer, colour) in enumerate(layers):
        parts.append(_group(layer, colour, f"m{i}", box))
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def _union(boxes) -> Tuple[float, float, float, float]:
    boxes = [b for b in boxes if b[0] < b[2]]
    if not boxes:
        raise PreviewError("nothing to render")
    pad = 1.0
    return (
        min(b[0] for b in boxes) - pad,
        min(b[1] for b in boxes) - pad,
        max(b[2] for b in boxes) + pad,
        max(b[3] for b in boxes) + pad,
    )


def read_zip(path: Path) -> Dict[str, Layer]:
    """Every gerber and drill file of a zip, by KiCad layer (``Drill`` for every drill file)."""
    out: Dict[str, Layer] = {}
    with zipfile.ZipFile(path) as zf:
        for name in sorted(zf.namelist()):
            text = zf.read(name).decode("utf-8", errors="replace")
            if text.lstrip().startswith("M48"):
                layer = read_drill(text, name)
                key = "Drill" if "Drill" not in out else f"Drill:{name}"
            elif "%FS" in text[:2000]:
                layer = read_gerber(text, name)
                key = (
                    layer_of_function(layer.function or "")
                    or f"{layer.function or 'Gerber'}:{name}"
                )
            else:
                continue
            out[key] = layer
    return out


def render_zip(path: Path, out_dir: Path) -> List[Path]:
    """Per-layer SVGs and top/bottom composites of a gerber zip; returns the files written."""
    layers = read_zip(path)
    if "Edge.Cuts" not in layers:
        raise PreviewError(f"{Path(path).name}: no board outline (Profile) layer")
    # The board's own layers set the frame (a drill map's legend lies outside the board).
    board_layers = [
        layer for key, layer in layers.items() if ":" not in key or key.startswith("Drill:")
    ]
    box = _union([layer.bbox for layer in board_layers])
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    drills = [layer for key, layer in layers.items() if key == "Drill" or key.startswith("Drill:")]
    for key, layer in sorted(layers.items()):
        colour = "#f0f0f0"
        target = out_dir / f"layer-{re.sub(r'[^A-Za-z0-9]+', '_', key)}.svg"
        target.write_text(svg([(layer, colour)], box, key))
        written.append(target)
    for side, prefix in (("top", "F"), ("bottom", "B")):
        stack: List[Tuple[Layer, str]] = []
        if "Edge.Cuts" in layers:
            stack.append((layers["Edge.Cuts"], COLOURS["Edge.Cuts"]))
        cu = layers.get(f"{prefix}.Cu")
        if cu:
            stack.append((cu, COLOURS["Cu"]))
        mask = layers.get(f"{prefix}.Mask")
        if mask:
            stack.append((mask, "#8fd18f"))
        silk = layers.get(f"{prefix}.Silkscreen")
        if silk:
            stack.append((silk, COLOURS["Silkscreen"]))
        stack += [(d, COLOURS["Drill"]) for d in drills]
        target = out_dir / f"composite-{side}.svg"
        target.write_text(svg(stack, box, f"{Path(path).name} {side}"))
        written.append(target)
    return written
