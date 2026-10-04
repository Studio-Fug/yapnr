"""Board facts for the fab checks and bundles, from the ``.kicad_pcb`` file itself.

A small stdlib S-expression reader (no ``pcbnew``) gives the copper layers, the board's own KiCad
stackup, the footprints with their fields and attributes, castellated pads, RF footprints and the
outline; ``kicad-cli pcb export stats`` (``yapnr.fab.kicad``) adds KiCad's own measurements.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

_TOKEN = re.compile(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()"]+')
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
NIL_UUID = "00000000-0000-0000-0000-000000000000"

# The description yapnr.rf.export writes on an RF footprint (design §6.6).
RF_DESCRIPTION = re.compile(
    r"yapnr RF inverse design '(?P<name>[^']*)': microstrip copper on er (?P<er>[\d.eE+-]+), "
    r"tan_delta (?P<td>[\d.eE+-]+), h (?P<h>[\d.eE+-]+) mm.*?spec sha256 (?P<sha>[0-9a-f]{64})",
    re.DOTALL,
)


class BoardError(ValueError):
    """The board file cannot be read."""


class Sym(str):
    """An unquoted atom (``smd``, ``yes``); quoted strings are plain ``str``."""


def parse(text: str):
    """The S-expression tree of a KiCad file: lists of ``str`` / ``Sym`` / nested lists."""
    stack: List[list] = [[]]
    for match in _TOKEN.finditer(text):
        tok = match.group(0)
        if tok == "(":
            stack.append([])
        elif tok == ")":
            if len(stack) < 2:
                raise BoardError("unbalanced ')'")
            done = stack.pop()
            stack[-1].append(done)
        elif tok[0] == '"':
            stack[-1].append(tok[1:-1].replace('\\"', '"').replace("\\\\", "\\"))
        else:
            stack[-1].append(Sym(tok))
    if len(stack) != 1 or len(stack[0]) != 1:
        raise BoardError("unbalanced S-expression")
    return stack[0][0]


def children(node, name: str) -> Iterable[list]:
    for item in node[1:] if isinstance(node, list) else ():
        if isinstance(item, list) and item and item[0] == name:
            yield item


def child(node, name: str) -> Optional[list]:
    return next(iter(children(node, name)), None)


def value(node, name: str, default=None):
    found = child(node, name)
    return found[1] if found is not None and len(found) > 1 else default


def _float(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def input_id(text: str) -> str:
    """sha256 of the board text with every UUID replaced by the nil UUID (as atopile builds do:
    yapnr.frontends.atopile.runner.input_id)."""
    return hashlib.sha256(_UUID.sub(NIL_UUID, text).encode("utf-8")).hexdigest()


@dataclass
class Pad:
    number: str
    kind: str  # smd, thru_hole, np_thru_hole, connect
    shape: str
    drill: Tuple[float, float] = (0.0, 0.0)
    castellated: bool = False
    at: Tuple[float, float, float] = (0.0, 0.0, 0.0)  # board x, y (mm) and absolute angle
    size: Tuple[float, float] = (0.0, 0.0)
    layers: Tuple[str, ...] = ()

    def contains(self, x: float, y: float) -> bool:
        """Whether the board point lies on the pad's copper (its rectangle, rotated)."""
        a = math.radians(self.at[2])
        dx, dy = x - self.at[0], y - self.at[1]
        # KiCad's angles are counter-clockwise with y down: undo the pad's rotation.
        u = dx * math.cos(a) - dy * math.sin(a)
        v = dx * math.sin(a) + dy * math.cos(a)
        return abs(u) <= self.size[0] / 2 + 1e-9 and abs(v) <= self.size[1] / 2 + 1e-9


@dataclass
class Via:
    x: float
    y: float
    diameter: float
    drill: float


@dataclass
class Footprint:
    reference: str
    value: str
    lib_id: str
    layer: str  # F.Cu or B.Cu
    at: Tuple[float, float, float]
    properties: Dict[str, str]
    attrs: List[str]
    description: str
    pads: List[Pad] = field(default_factory=list)

    @property
    def side(self) -> str:
        return "bottom" if self.layer == "B.Cu" else "top"

    @property
    def dnp(self) -> bool:
        return "dnp" in self.attrs

    @property
    def excluded_from_bom(self) -> bool:
        return "exclude_from_bom" in self.attrs or "board_only" in self.attrs

    @property
    def excluded_from_pos(self) -> bool:
        return "exclude_from_pos_files" in self.attrs or "board_only" in self.attrs

    @property
    def mount(self) -> str:
        if "smd" in self.attrs:
            return "SMD"
        if "through_hole" in self.attrs:
            return "THT"
        return "other"

    def field(self, *names: str) -> str:
        """The first non-empty footprint field among ``names`` (case-insensitive)."""
        lower = {k.lower(): v for k, v in self.properties.items()}
        for name in names:
            v = lower.get(name.lower(), "").strip()
            if v:
                return v
        return ""

    @property
    def rf(self) -> Optional[Dict[str, Any]]:
        """The RF design a yapnr.rf footprint assumes (name, er, tan_delta, h_mm, spec sha256)."""
        match = RF_DESCRIPTION.search(self.description or "")
        if not match:
            return None
        return {
            "name": match["name"],
            "er": float(match["er"]),
            "tan_delta": float(match["td"]),
            "h_mm": float(match["h"]),
            "spec_sha256": match["sha"],
        }


@dataclass
class Outline:
    loops: List[Tuple[float, float, float, float]]  # bounding box of every closed loop
    open_chains: int
    stroke_mm: float

    @property
    def closed_loops(self) -> int:
        return len(self.loops)

    def outer_loops(self) -> List[Tuple[float, float, float, float]]:
        """Loops inside no other loop: the board outlines (more than one is a panel); the
        others are cutouts."""

        def inside(a, b):
            return a != b and b[0] <= a[0] and b[1] <= a[1] and a[2] <= b[2] and a[3] <= b[3]

        return [a for a in self.loops if not any(inside(a, b) for b in self.loops)]

    @property
    def bbox(self) -> Optional[Tuple[float, float, float, float]]:
        outer = self.outer_loops()
        if not outer:
            return None
        return (
            min(b[0] for b in outer),
            min(b[1] for b in outer),
            max(b[2] for b in outer),
            max(b[3] for b in outer),
        )

    @property
    def size_mm(self) -> Optional[Tuple[float, float]]:
        box = self.bbox
        if box is None:
            return None
        return (round(box[2] - box[0], 6), round(box[3] - box[1], 6))


@dataclass
class Board:
    path: Path
    name: str
    sha256: str
    input_id: str
    thickness_mm: float
    copper_layers: List[str]
    stackup: List[Dict[str, Any]]
    footprints: List[Footprint]
    outline: Outline
    via_count: int
    netclasses: List[Dict[str, Any]] = field(default_factory=list)
    vias: List[Via] = field(default_factory=list)
    rules_profile: Optional[str] = None

    @property
    def layer_count(self) -> int:
        return len(self.copper_layers)

    def vias_in_smd_pads(self) -> List[str]:
        """``REF.pad`` of every SMD pad with a via on its copper (via in pad: filled vias)."""
        found = []
        for fp in self.footprints:
            for pad in fp.pads:
                if pad.kind != "smd" or not any(layer.endswith(".Cu") for layer in pad.layers):
                    continue
                if any(pad.contains(v.x, v.y) for v in self.vias):
                    found.append(f"{fp.reference}.{pad.number}")
        return sorted(set(found))

    def rf_footprints(self) -> List[Footprint]:
        return [fp for fp in self.footprints if fp.rf is not None]

    def castellated(self) -> List[Tuple[str, Pad]]:
        return [(fp.reference, p) for fp in self.footprints for p in fp.pads if p.castellated]


def _xy(node) -> Tuple[float, float]:
    return (_float(node[1]), _float(node[2])) if node is not None and len(node) >= 3 else (0.0, 0.0)


def _transform(pt, at):
    """Footprint-local point to board coordinates (KiCad: rotation counter-clockwise, y down)."""
    x, y = pt
    a = math.radians(at[2])
    return (at[0] + x * math.cos(a) + y * math.sin(a), at[1] - x * math.sin(a) + y * math.cos(a))


def _edge_items(tree):
    """(kind, points, stroke) for every Edge.Cuts graphic, footprint graphics included."""
    out = []

    def collect(node, prefix, at=None):
        for item in node[1:]:
            if not isinstance(item, list) or not item or not str(item[0]).startswith(prefix):
                continue
            if value(item, "layer") != "Edge.Cuts":
                continue
            kind = str(item[0])[len(prefix) :]
            stroke = (
                _float(value(child(item, "stroke"), "width", 0.0))
                if child(item, "stroke")
                else _float(value(item, "width", 0.0))
            )
            if kind in ("line", "arc"):
                pts = [_xy(child(item, "start")), _xy(child(item, "end"))]
                if kind == "arc":
                    pts.insert(1, _xy(child(item, "mid")))
            elif kind == "rect":
                (x0, y0), (x1, y1) = _xy(child(item, "start")), _xy(child(item, "end"))
                pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
            elif kind == "circle":
                (cx, cy), (ex, ey) = _xy(child(item, "center")), _xy(child(item, "end"))
                r = math.hypot(ex - cx, ey - cy)
                pts = [(cx - r, cy - r), (cx + r, cy + r)]
            elif kind in ("poly", "curve"):
                pts = [_xy(p) for p in children(child(item, "pts") or [], "xy")]
            else:
                continue
            if at is not None:
                pts = [_transform(p, at) for p in pts]
            out.append((kind, pts, stroke))

    collect(tree, "gr_")
    for fp in children(tree, "footprint"):
        at_node = child(fp, "at")
        at = (
            _float(at_node[1]),
            _float(at_node[2]),
            _float(at_node[3]) if len(at_node) > 3 else 0.0,
        )
        collect(fp, "fp_", at)
    return out


def _box(points) -> Tuple[float, float, float, float]:
    return (
        min(p[0] for p in points),
        min(p[1] for p in points),
        max(p[0] for p in points),
        max(p[1] for p in points),
    )


def outline(tree) -> Outline:
    """Closed loops (with their bounding boxes), open chains and the widest stroke."""
    items = _edge_items(tree)
    if not items:
        return Outline([], 0, 0.0)
    stroke = max(s for _, _, s in items)
    loops = [_box(pts) for kind, pts, _ in items if kind in ("rect", "circle", "poly")]
    key = lambda p: (round(p[0], 3), round(p[1], 3))  # noqa: E731
    graph: Dict[tuple, List[tuple]] = {}
    points: Dict[tuple, List[tuple]] = {}
    for kind, pts, _ in items:
        if kind in ("rect", "circle", "poly"):
            continue
        a, b = key(pts[0]), key(pts[-1])
        graph.setdefault(a, []).append(b)
        graph.setdefault(b, []).append(a)
        points.setdefault(a, []).extend(pts)
    seen = set()
    open_chains = 0
    for start in graph:
        if start in seen:
            continue
        comp, stack = [], [start]
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            comp.append(node)
            stack.extend(graph[node])
        if all(len(graph[n]) == 2 for n in comp):
            loops.append(_box([p for n in comp for p in points.get(n, [n])] + comp))
        else:
            open_chains += 1
    return Outline(sorted(loops), open_chains, stroke)


def _footprints(tree) -> List[Footprint]:
    out = []
    for fp in children(tree, "footprint"):
        props = {}
        for prop in children(fp, "property"):
            if len(prop) >= 3:
                props[str(prop[1])] = str(prop[2])
        # KiCad 6/7 boards keep the reference and value as fp_text.
        for text in children(fp, "fp_text"):
            if len(text) >= 3 and str(text[1]) in ("reference", "value"):
                props.setdefault(str(text[1]).capitalize(), str(text[2]))
        at_node = child(fp, "at")
        at = (
            _float(at_node[1]),
            _float(at_node[2]),
            _float(at_node[3]) if at_node is not None and len(at_node) > 3 else 0.0,
        )
        attr = child(fp, "attr")
        attrs = [str(a) for a in attr[1:] if not isinstance(a, list)] if attr else []
        for flag in ("dnp", "exclude_from_bom", "exclude_from_pos_files", "board_only"):
            node = child(fp, flag)
            if node is not None and (len(node) == 1 or str(node[1]) == "yes"):
                attrs.append(flag)
        pads = []
        for pad in children(fp, "pad"):
            drill = child(pad, "drill")
            hole: Tuple[float, float] = (0.0, 0.0)
            if drill is not None:
                nums = [_float(x) for x in drill[1:] if not isinstance(x, list) and x != "oval"]
                if nums:
                    hole = (nums[0], nums[1] if len(nums) > 1 else nums[0])
            prop = child(pad, "property")
            castellated = prop is not None and any(str(x) == "pad_prop_castellated" for x in prop)
            # A pad's (at x y angle): footprint-relative position, absolute orientation, omitted
            # when 0 (KiCad's file format), so only the position turns with the footprint.
            pad_at = child(pad, "at")
            px, py = _transform(_xy(pad_at), at)
            angle = _float(pad_at[3]) if pad_at is not None and len(pad_at) > 3 else 0.0
            size_node = child(pad, "size")
            layers_node = child(pad, "layers")
            pads.append(
                Pad(
                    number=str(pad[1]) if len(pad) > 1 else "",
                    kind=str(pad[2]) if len(pad) > 2 else "",
                    shape=str(pad[3]) if len(pad) > 3 else "",
                    drill=hole,
                    castellated=castellated,
                    at=(px, py, angle),
                    size=_xy(size_node),
                    layers=tuple(str(x) for x in layers_node[1:]) if layers_node else (),
                )
            )
        out.append(
            Footprint(
                reference=props.get("Reference", ""),
                value=props.get("Value", ""),
                lib_id=str(fp[1]) if len(fp) > 1 else "",
                layer=str(value(fp, "layer", "F.Cu")),
                at=at,
                properties=props,
                attrs=sorted(set(attrs)),
                description=str(value(fp, "descr", "") or ""),
                pads=pads,
            )
        )
    return out


def _stackup(tree) -> List[Dict[str, Any]]:
    setup = child(tree, "setup")
    st = child(setup, "stackup") if setup is not None else None
    out = []
    for layer in children(st, "layer") if st is not None else ():
        entry: Dict[str, Any] = {"name": str(layer[1]), "type": str(value(layer, "type", ""))}
        for key in ("thickness", "epsilon_r", "loss_tangent"):
            v = value(layer, key)
            if v is not None:
                entry[key] = _float(v)
        material = value(layer, "material")
        if material is not None:
            entry["material"] = str(material)
        out.append(entry)
    return out


_GENERATED_RULES = re.compile(
    r"^# Generated by pnr\.fab_profile \(profile ([^)\s]+)\)", re.MULTILINE
)


def rules_profile(dru_text: Optional[str]) -> Optional[str]:
    """The profile a generated ``.kicad_dru`` names (``pnr.fab_profile`` writes it when it routes
    or judges a board), "hand-written rules" for any other rules file, None without one."""
    if not dru_text:
        return None
    match = _GENERATED_RULES.search(dru_text)
    return match.group(1) if match else "hand-written rules"


def _vias(tree) -> List[Via]:
    out = []
    for via in children(tree, "via"):
        x, y = _xy(child(via, "at"))
        out.append(Via(x, y, _float(value(via, "size")), _float(value(via, "drill"))))
    return out


def read(path, name: Optional[str] = None) -> Board:
    """The facts of one board file (and its ``.kicad_pro`` netclasses, when present)."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as err:
        raise BoardError(f"cannot read {path}: {err}") from None
    tree = parse(text)
    if not tree or tree[0] != "kicad_pcb":
        raise BoardError(f"{path.name} is not a KiCad board")
    copper = []
    for layer in child(tree, "layers")[1:] if child(tree, "layers") is not None else ():
        if isinstance(layer, list) and len(layer) >= 3 and str(layer[1]).endswith(".Cu"):
            copper.append((int(_float(layer[0])), str(layer[1])))
    order = {"F.Cu": -1, "B.Cu": 10**6}
    names = [
        n
        for _, n in sorted(
            copper, key=lambda x: order.get(x[1], int(x[1][2:-3]) if x[1].startswith("In") else 0)
        )
    ]
    dru = path.with_suffix(".kicad_dru")
    try:
        dru_text = dru.read_text(encoding="utf-8") if dru.is_file() else None
    except OSError:
        dru_text = None
    pro = path.with_suffix(".kicad_pro")
    classes: List[Dict[str, Any]] = []
    if pro.is_file():
        try:
            classes = (json.loads(pro.read_text()).get("net_settings") or {}).get("classes") or []
        except ValueError:
            classes = []
    return Board(
        path=path,
        name=name or path.stem,
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        input_id=input_id(text),
        thickness_mm=_float(value(child(tree, "general"), "thickness", 1.6), 1.6),
        copper_layers=names,
        stackup=_stackup(tree),
        footprints=_footprints(tree),
        outline=outline(tree),
        via_count=sum(1 for _ in children(tree, "via")),
        netclasses=[c for c in classes if isinstance(c, dict)],
        vias=_vias(tree),
        rules_profile=rules_profile(dru_text),
    )


def stats_value(text: Any) -> Optional[float]:
    """A number of ``kicad-cli pcb export stats`` (``"26.0000 mm"`` -> 26.0)."""
    if isinstance(text, (int, float)):
        return float(text)
    match = re.match(r"\s*(-?[\d.]+)", str(text or ""))
    return float(match.group(1)) if match else None
