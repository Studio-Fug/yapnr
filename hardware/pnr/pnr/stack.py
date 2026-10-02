"""The board's own copper stack: which layers route signals and which are planes.

A board that declares a physical stackup (KiCad's ``(stackup ...)`` block) says, per
copper layer, what the layer is for through its KiCad layer type: ``signal``,
``power`` (a dedicated plane), ``mixed`` (a split plane that also carries signals)
or ``jumper``. :mod:`pnr.ingest` records that, with the nets of the zones already
drawn on each layer, as ``BoardGraph.stack``:

    {"layers": [{"name": "F.Cu", "type": "signal", "copper_mm": 0.035,
                 "zones": []}, ...]}            # copper layers, outer to outer

:func:`resolve` combines the record with the routing rules into a :class:`Stack`,
or returns None, which keeps the legacy layer heuristic (the two outer layers, or
F/In1/In2/B on a board with four or more layers and a ``plane_layer`` class). The
stack applies only when the board declares a stackup with the rules' copper layer
count **and** it either types some layer ``power``/``mixed`` or no net class
declares a ``plane_layer``: a board whose planes come only from the rules, or whose
source file was saved with another layer count, keeps its old behaviour exactly.

Layer roles in a :class:`Stack`:

``plane``
    a dedicated plane (``power`` type): no tracks. Its nets are the ``plane_layer``
    class nets naming it plus the nets of zones already on it. Every surface pad of
    such a net drops a through via to it (:mod:`pnr.route.detail.joint_escape`).
``split``
    a ``mixed`` layer, or a ``signal`` layer named by a class ``plane_layer``: the
    legacy split plane (the bounding box of the class nets' pads), whose gaps carry
    signals.
``signal``
    a routed layer.
``unused``
    a ``jumper`` layer.

Layers are referred to by name, for up to KiCad's 32 copper layers. Pure stdlib.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

# KiCad layer types (pcbnew LT_SIGNAL, LT_POWER, LT_MIXED, LT_JUMPER) by value.
KICAD_LAYER_TYPES = {0: "signal", 1: "power", 2: "mixed", 3: "jumper"}
MAX_COPPER_LAYERS = 32

SIGNAL, SPLIT, PLANE, UNUSED = "signal", "split", "plane", "unused"

# The legacy heuristic's copper stack on a board with 4+ layers and plane classes.
LEGACY_FOUR = ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")
LEGACY_TWO = ("F.Cu", "B.Cu")


def copper_names(count: int) -> List[str]:
    """KiCad's standard copper layer names, outer to outer."""
    if not 1 <= count <= MAX_COPPER_LAYERS:
        raise ValueError("copper layer count must be 1..%d" % MAX_COPPER_LAYERS)
    if count == 1:
        return ["F.Cu"]
    return ["F.Cu"] + ["In%d.Cu" % i for i in range(1, count - 1)] + ["B.Cu"]


@dataclass(frozen=True)
class Layer:
    name: str
    kind: str  # the board's KiCad layer type
    role: str  # SIGNAL / SPLIT / PLANE / UNUSED
    nets: Tuple[str, ...] = ()  # plane nets (PLANE and SPLIT layers)
    copper_mm: Optional[float] = None


@dataclass(frozen=True)
class Stack:
    layers: Tuple[Layer, ...]
    warnings: Tuple[str, ...] = field(default=())

    @property
    def names(self) -> Tuple[str, ...]:
        return tuple(x.name for x in self.layers)

    @property
    def grid_layers(self) -> Tuple[str, ...]:
        """Layers the detailed router routes, in stack order (signal and split)."""
        return tuple(x.name for x in self.layers if x.role in (SIGNAL, SPLIT))

    @property
    def dedicated(self) -> Tuple[Tuple[str, str], ...]:
        """``(layer, net)`` of every dedicated plane, outer to outer."""
        return tuple((x.name, n) for x in self.layers if x.role == PLANE for n in x.nets)

    @property
    def plane_nets(self) -> frozenset:
        """Nets with at least one dedicated plane (connected by through-via drops)."""
        return frozenset(n for _, n in self.dedicated)

    @property
    def split_nets(self) -> Dict[str, str]:
        """net -> split-plane layer (the legacy bounding-box pour)."""
        out: Dict[str, str] = {}
        for x in self.layers:
            if x.role == SPLIT:
                for n in x.nets:
                    out.setdefault(n, x.name)
        return out

    def net_planes(self, net: str) -> List[str]:
        """The dedicated plane layers of ``net``, outer to outer."""
        return [layer for layer, n in self.dedicated if n == net]

    def layer(self, name: str) -> Layer:
        for x in self.layers:
            if x.name == name:
                return x
        raise KeyError(name)


def _class_planes(rules: Optional[dict]) -> Dict[str, List[str]]:
    """layer -> the nets of the net classes that name it as ``plane_layer``."""
    out: Dict[str, List[str]] = {}
    for nc in (rules or {}).get("net_classes", []):
        layer = nc.get("plane_layer")
        if layer:
            out.setdefault(layer, []).extend(nc.get("nets", []))
    return out


def legacy_layers(rules: Optional[dict]) -> Tuple[str, ...]:
    """The legacy heuristic: F/In1/In2/B on a board with 4+ layers and plane
    classes, otherwise the two outer layers."""
    if rules and int(rules.get("layers", 2)) >= 4 and any(_class_planes(rules).values()):
        return LEGACY_FOUR
    return LEGACY_TWO


def resolve(rules: Optional[dict], record: Optional[dict]) -> Optional[Stack]:
    """The board's :class:`Stack`, or None for the legacy heuristic (module doc)."""
    if not record or not record.get("layers"):
        return None
    rows = record["layers"]
    if not 2 <= len(rows) <= MAX_COPPER_LAYERS:
        raise ValueError("stack record must list 2..%d copper layers" % MAX_COPPER_LAYERS)
    if rules and rules.get("layers") is not None and int(rules["layers"]) != len(rows):
        # The source file's stack does not describe the board the rules build (an
        # atopile layout saved two-layer for a four-layer build): legacy.
        return None
    kinds = [str(r.get("type", "signal")) for r in rows]
    unknown = sorted(set(kinds) - set(KICAD_LAYER_TYPES.values()))
    if unknown:
        raise ValueError("unknown copper layer type(s): %s" % ", ".join(unknown))
    classes = _class_planes(rules)
    if not (any(k in ("power", "mixed") for k in kinds) or not classes):
        return None
    names = [str(r["name"]) for r in rows]
    if len(set(names)) != len(names):
        raise ValueError("duplicate copper layer names in the stack record")
    missing = sorted(set(classes) - set(names))
    if missing:
        raise ValueError(
            "net class plane_layer %s is not a copper layer of this board (%s)"
            % (", ".join(missing), ", ".join(names))
        )
    warnings = []
    layers = []
    for index, (row, kind) in enumerate(zip(rows, kinds)):
        name = names[index]
        outer = index in (0, len(rows) - 1)
        class_nets = list(dict.fromkeys(classes.get(name, [])))
        zone_nets = [n for n in row.get("zones", []) if n]
        copper = row.get("copper_mm")
        copper = None if copper is None else float(copper)
        role_kind = kind
        if kind == "power" and outer:
            # Pads and parts sit on an outer layer: it cannot be a track-free plane.
            warnings.append("%s is typed power but is an outer layer: treated as mixed" % name)
            role_kind = "mixed"
        if role_kind == "power":
            nets = tuple(dict.fromkeys(class_nets + sorted(set(zone_nets) - set(class_nets))))
            layers.append(Layer(name, kind, PLANE, nets, copper))
        elif role_kind == "mixed" or (role_kind == "signal" and class_nets):
            layers.append(Layer(name, kind, SPLIT, tuple(class_nets), copper))
        elif role_kind == "jumper":
            layers.append(Layer(name, kind, UNUSED, (), copper))
        else:
            layers.append(Layer(name, kind, SIGNAL, (), copper))
    stack = Stack(tuple(layers), tuple(warnings))
    grid = stack.grid_layers
    if len(grid) < 1 or grid[0] != names[0] or grid[-1] != names[-1]:
        raise ValueError("both outer copper layers must carry signals")
    return stack


def grid_layers(rules: Optional[dict], record: Optional[dict]) -> Tuple[str, ...]:
    """The detailed router's layers: the stack's, else the legacy heuristic's."""
    stack = resolve(rules, record)
    return stack.grid_layers if stack is not None else legacy_layers(rules)


def plane_nets(rules: Optional[dict], record: Optional[dict]) -> frozenset:
    """Nets kept out of signal routing: the class plane nets, plus (stack) every
    net with a dedicated plane."""
    nets = {n for ns in _class_planes(rules).values() for n in ns}
    stack = resolve(rules, record)
    if stack is not None:
        nets |= stack.plane_nets
    return frozenset(nets)


def record_from_rows(rows: Sequence[dict]) -> dict:
    """A normalized stack record (names, types, copper thickness, zone nets)."""
    return {
        "layers": [
            {
                "name": str(r["name"]),
                "type": str(r.get("type", "signal")),
                "copper_mm": None if r.get("copper_mm") is None else float(r["copper_mm"]),
                "zones": sorted({str(n) for n in r.get("zones", []) if n}),
            }
            for r in rows
        ]
    }
