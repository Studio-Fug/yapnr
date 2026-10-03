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

import math
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

    @property
    def inner_routed(self) -> Tuple[str, ...]:
        """The routed (signal and split) inner layers, outer to outer."""
        return self.grid_layers[1:-1]

    def reference_plane(self, name: str) -> Optional[Layer]:
        """The dedicated plane nearest to layer ``name`` (the return path of a
        trace on it), the one toward the board's middle on a tie; None without
        planes."""
        index = self.names.index(name)
        middle = (len(self.layers) - 1) / 2.0
        planes = [
            (abs(i - index), abs(i - middle), i, x)
            for i, x in enumerate(self.layers)
            if x.role == PLANE and x.nets and i != index
        ]
        return min(planes, key=lambda t: t[:3])[3] if planes else None


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
    return assess(rules, record)[0]


def assess(
    rules: Optional[dict], record: Optional[dict]
) -> Tuple[Optional[Stack], Tuple[str, ...]]:
    """:func:`resolve` with the warnings that explain its decision.

    A stack record the engine cannot use as declared (one copper layer, an unknown
    layer type, an outer layer that cannot carry signals, a layer count other than
    the rules') falls back to the legacy heuristic with a warning instead of
    stopping the run; so does a stack whose only plane hints are zones drawn on
    signal-typed inner layers (KiCad types every layer ``signal`` by default, so
    such a board keeps its old outer-layer routing until it types its planes).
    Rules that name a plane layer the board does not have still raise."""
    if not record or not record.get("layers"):
        return None, ()
    rows = record["layers"]
    names = [str(r["name"]) for r in rows]
    if len(rows) > MAX_COPPER_LAYERS:
        raise ValueError("stack record must list at most %d copper layers" % MAX_COPPER_LAYERS)
    if len(set(names)) != len(names):
        raise ValueError("duplicate copper layer names in the stack record")
    kinds = [str(r.get("type", "signal")) for r in rows]
    described = ", ".join("%s %s" % (n, k) for n, k in zip(names, kinds))
    # The board's own rules may keep tracks off a layer typed signal or mixed (a
    # .kicad_dru "disallow track" rule): an inner one is a plane, as if typed power.
    no_tracks = set(record.get("no_track_layers", ()))
    retyped = []
    for i in range(1, len(rows) - 1):
        if names[i] in no_tracks and kinds[i] in ("signal", "mixed"):
            retyped.append("%s (typed %s)" % (names[i], kinds[i]))
            kinds[i] = "power"
    if len(rows) < 2:
        return None, (
            "the declared stack has one copper layer (%s): legacy layer heuristic" % described,
        )
    if rules and rules.get("layers") is not None and int(rules["layers"]) != len(rows):
        # The source file's stack does not describe the board the rules build (an
        # atopile layout saved two-layer for a four-layer build): legacy.
        return None, (
            "the declared stack has %d copper layers, the rules build %d: legacy layer "
            "heuristic" % (len(rows), int(rules["layers"])),
        )
    unknown = sorted(set(kinds) - set(KICAD_LAYER_TYPES.values()))
    if unknown:
        return None, (
            "unknown copper layer type(s) %s in the declared stack (%s): legacy layer "
            "heuristic" % (", ".join(unknown), described),
        )
    for index in (0, len(rows) - 1):
        if kinds[index] == "jumper":
            return None, (
                "outer layer %s is typed jumper, so it cannot carry signals (%s): legacy "
                "layer heuristic" % (names[index], described),
            )
    classes = _class_planes(rules)
    zoned = {
        names[i]: sorted({n for n in rows[i].get("zones", []) if n})
        for i in range(1, len(rows) - 1)
        if kinds[i] == "signal" and any(rows[i].get("zones", []))
    }
    if not any(k in ("power", "mixed") for k in kinds):
        if classes:
            return None, ()  # planes from the rules only: the legacy split planes
        if zoned:
            return None, (
                "inner layer(s) %s are typed signal but carry zones (%s); no layer is "
                "typed power or mixed, so the declared stack is not used (legacy layer "
                "heuristic): type each plane layer power (or mixed) to route this stack"
                % (
                    ", ".join(zoned),
                    "; ".join("%s: %s" % (k, ", ".join(v)) for k, v in zoned.items()),
                ),
            )
    missing = sorted(set(classes) - set(names))
    if missing:
        raise ValueError(
            "net class plane_layer %s is not a copper layer of this board (%s)"
            % (", ".join(missing), ", ".join(names))
        )
    warnings = []
    if retyped:
        warnings.append(
            "the board's rules disallow tracks on %s: routed as plane layers" % ", ".join(retyped)
        )
    for name in sorted(no_tracks & {names[0], names[-1]}):
        warnings.append(
            "the board's rules disallow tracks on outer layer %s, which carries pads: "
            "it is still routed" % name
        )
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
            if not nets:
                warnings.append(
                    "%s is typed power but no net class plane_layer or zone names its net: "
                    "no tracks and no plane on it" % name
                )
            elif len(nets) > 1:
                warnings.append(
                    "%s is a plane shared by %s: each net keeps its drops inside its own "
                    "region (source zones, else the outline for the net with most pads and "
                    "its pads' bounding box for the others)" % (name, ", ".join(nets))
                )
            layers.append(Layer(name, kind, PLANE, nets, copper))
        elif role_kind == "mixed" or (role_kind == "signal" and class_nets):
            layers.append(Layer(name, kind, SPLIT, tuple(class_nets), copper))
        elif role_kind == "jumper":
            layers.append(Layer(name, kind, UNUSED, (), copper))
        else:
            if zone_nets and not outer:
                warnings.append(
                    "%s is a signal layer with zones of %s: tracks route across them and "
                    "the zones refill around the tracks" % (name, ", ".join(sorted(set(zone_nets))))
                )
            layers.append(Layer(name, kind, SIGNAL, (), copper))
    return Stack(tuple(layers), tuple(warnings)), tuple(warnings)


def grid_layers(rules: Optional[dict], record: Optional[dict]) -> Tuple[str, ...]:
    """The detailed router's layers: the stack's, else the legacy heuristic's."""
    stack = resolve(rules, record)
    return stack.grid_layers if stack is not None else legacy_layers(rules)


def plane_nets(rules: Optional[dict], record: Optional[dict]) -> frozenset:
    """Nets kept out of signal routing: the class plane nets, plus (stack) every
    net with a dedicated plane."""
    return plane_nets_of(rules, resolve(rules, record))


def plane_nets_of(rules: Optional[dict], stack: Optional[Stack]) -> frozenset:
    """:func:`plane_nets` for an already resolved ``stack``."""
    nets = {n for ns in _class_planes(rules).values() for n in ns}
    if stack is not None:
        nets |= stack.plane_nets
    return frozenset(nets)


def split_plane_patterns(constraints, graph) -> List[str]:
    """Net patterns whose pads the placement's plane terms keep compact and apart
    (pnr.place.model ``w_plane``/``w_plane_sep``): the class plane nets, which the
    legacy heuristic pours as split planes over their pads' bounding box. On a
    declared stack only its split-plane nets: a dedicated plane covers the whole
    board, so its pads need neither a compact box nor one apart from other nets."""
    import fnmatch
    import glob

    pats = [p for nc in constraints.net_classes if nc.plane_layer for p in nc.nets]
    record = getattr(graph, "stack", None)
    if not record:
        return pats
    names = [n.name for n in graph.nets]
    rules = dict(
        layers=getattr(constraints.board, "layers", None),
        net_classes=[
            dict(
                nets=[n for n in names if any(fnmatch.fnmatch(n, p) for p in nc.nets)],
                plane_layer=nc.plane_layer,
            )
            for nc in constraints.net_classes
            if nc.plane_layer
        ],
    )
    stack = resolve(rules, record)
    if stack is None:
        return pats
    return [glob.escape(n) for n in sorted(stack.split_nets)]


def stackup_rows(text: str) -> List[dict]:
    """The physical rows of a board file's ``(stackup ...)`` block, top to bottom:
    ``{"name", "type", "thickness_mm", "epsilon_r"}`` (thickness and epsilon_r None
    when the row does not state them). Copper rows have type ``copper``."""
    import re

    start = text.find("(stackup")
    if start < 0:
        return []
    depth = 0
    rows = []
    row_start = None
    for index in range(start, len(text)):
        ch = text[index]
        if ch == "(":
            depth += 1
            if depth == 2 and text.startswith("(layer", index):
                row_start = index
        elif ch == ")":
            if depth == 2 and row_start is not None:
                row = text[row_start : index + 1]
                name = re.match(r'\(layer\s+"([^"]+)"', row)
                kind = re.search(r'\(type\s+"([^"]+)"\)', row)
                thick = re.search(r"\(thickness\s+([0-9.eE+-]+)", row)
                er = re.search(r"\(epsilon_r\s+([0-9.eE+-]+)\)", row)
                if name and kind:
                    rows.append(
                        dict(
                            name=name.group(1),
                            type=kind.group(1),
                            thickness_mm=float(thick.group(1)) if thick else None,
                            epsilon_r=float(er.group(1)) if er else None,
                        )
                    )
                row_start = None
            depth -= 1
            if depth == 0:
                break
    return rows


def si_stackup(rows: Sequence[dict], stack: Stack) -> Optional[dict]:
    """The SI model's stack (:func:`pnr.si.physics.stackup`, ``rules['stackup']``)
    from a declared stackup block's ``rows`` (:func:`stackup_rows`): its copper and
    dielectric layers, top to bottom, and the stack's dedicated planes as reference
    planes. None when the block does not state every copper and dielectric
    thickness, or its copper is not the stack's."""
    layers = []
    for row in rows:
        if row["type"] == "copper":
            kind = "copper"
        elif row["type"] in ("core", "prepreg") or row["name"].startswith("dielectric"):
            kind = "dielectric"
        else:
            continue  # mask, paste, silk: outside the copper
        if row["thickness_mm"] is None:
            return None
        layer = dict(name=row["name"], kind=kind, t_mm=row["thickness_mm"])
        if kind == "dielectric":
            layer["er"] = row["epsilon_r"] if row["epsilon_r"] is not None else 4.5
            layer["material"] = row["type"]
        layers.append(layer)
    if [x["name"] for x in layers if x["kind"] == "copper"] != list(stack.names):
        return None
    planes = [x.name for x in stack.layers if x.role == PLANE and x.nets]
    return dict(
        name="declared stackup", source="the board file's stackup", layers=layers, planes=planes
    )


# ------------------------------------------------- the native loop's copper layers
# The native KiCad loop (pnr.native_electrical, pnr.native_loop) predates declared
# stacks: it routes power paths on F/B plus In2 and takes In1 as a pair's reference
# plane. These give it the declared stack's layers instead, and its legacy layers
# exactly when ``stack`` is None.


def power_layer_names(stack: Optional[Stack]) -> List[str]:
    """Layers a current-rated power path may use: the outer layers, then the routed
    inner layers (legacy: F, B, In2)."""
    if stack is None:
        return ["F.Cu", "B.Cu", "In2.Cu"]
    return ["F.Cu", "B.Cu"] + list(stack.inner_routed)


def bridge_layer_names(stack: Optional[Stack]) -> List[str]:
    """Bridge layers of a power path's compact via banks (legacy: B, In2, F)."""
    if stack is None:
        return ["B.Cu", "In2.Cu", "F.Cu"]
    return ["B.Cu"] + list(stack.inner_routed) + ["F.Cu"]


def contact_layer_names(stack: Optional[Stack]) -> List[str]:
    """Layers a pad's track contact is looked for on, in order (legacy: F, In2, B)."""
    if stack is None:
        return ["F.Cu", "In2.Cu", "B.Cu"]
    return ["F.Cu"] + list(stack.inner_routed) + ["B.Cu"]


def reference_layer(stack: Optional[Stack], pair: Optional[dict] = None) -> str:
    """A pair's reference plane: its own ``reference_layer``, else the dedicated
    plane nearest F.Cu (legacy: In1.Cu)."""
    if pair and pair.get("reference_layer"):
        return pair["reference_layer"]
    plane = stack.reference_plane("F.Cu") if stack is not None else None
    return plane.name if plane is not None else "In1.Cu"


def reference_nets(stack: Optional[Stack], rules: Optional[dict], layer: str) -> set:
    """The nets whose copper on ``layer`` is a reference plane: the class planes
    naming it, plus (declared stack) the nets of a dedicated plane there."""
    nets = set(_class_planes(rules).get(layer, []))
    if stack is not None and layer in stack.names and stack.layer(layer).role == PLANE:
        nets |= set(stack.layer(layer).nets)
    return nets


def record_from_rows(rows: Sequence[dict]) -> dict:
    """A normalized stack record (names, types, copper thickness, zone nets, and the
    outlines of the zones on dedicated planes when given: ``zone_shapes``, a list of
    ``{"net", "priority", "outline": [[x, y], ...]}`` in the graph frame)."""
    layers = []
    for r in rows:
        row = {
            "name": str(r["name"]),
            "type": str(r.get("type", "signal")),
            "copper_mm": None if r.get("copper_mm") is None else float(r["copper_mm"]),
            "zones": sorted({str(n) for n in r.get("zones", []) if n}),
        }
        shapes = [
            {
                "net": str(z["net"]),
                "priority": int(z.get("priority", 0)),
                "outline": [[round(float(x), 6), round(float(y), 6)] for x, y in z["outline"]],
            }
            for z in r.get("zone_shapes", [])
            if z.get("net") and len(z.get("outline", ())) >= 3
        ]
        if shapes:
            row["zone_shapes"] = sorted(
                shapes, key=lambda z: (z["net"], z["priority"], z["outline"])
            )
        layers.append(row)
    return {"layers": layers}


def local_record(record: Optional[dict]) -> Optional[dict]:
    """``record`` for a sub-board in a frame of its own (a hierarchical block, a line
    group): the zone outlines are in the whole board's frame, so they are left out
    (each such zone then counts as covering the sub-board)."""
    if not record:
        return record
    return {
        **record,
        "layers": [
            {k: v for k, v in row.items() if k != "zone_shapes"} for row in record["layers"]
        ],
    }


# ------------------------------------------------------------------ plane regions


@dataclass(frozen=True)
class Region:
    """Where ``net``'s copper fills on dedicated plane ``layer``: a polygon in the
    graph frame (mm), or None for the whole board. ``priority`` is the zone's fill
    priority (a higher one carves out of a lower one); ``source`` marks a zone
    already drawn on the board."""

    layer: str
    net: str
    priority: int
    outline: Optional[Tuple[Tuple[float, float], ...]]
    source: bool = False


SPLIT_MARGIN_MM = 2.0  # a split region's margin around its net's pads (pnr.writeback)


def _covers(outline, width, height, tol=1e-3) -> bool:
    xs = [x for x, _ in outline]
    ys = [y for _, y in outline]
    if min(xs) > tol or min(ys) > tol or max(xs) < width - tol or max(ys) < height - tol:
        return False
    return abs(_area(outline)) >= width * height - tol * (width + height)


def _area(outline) -> float:
    return 0.5 * sum(
        x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(outline, outline[1:] + outline[:1])
    )


def pad_points(graph) -> Dict[str, List[Tuple[float, float]]]:
    """net -> the absolute pad centres of ``graph`` (graph frame), as
    :func:`pnr.place.geometry.pin_positions` computes them (pure: the KiCad-side
    writeback forms the planes from the same points the router drops to)."""
    out: Dict[str, List[Tuple[float, float]]] = {}
    for comp in graph.components:
        th = math.radians(comp.rot)
        ct, st = math.cos(th), math.sin(th)
        for pad in comp.pads:
            if not pad.net:
                continue
            ox, oy = pad.offset
            out.setdefault(pad.net, []).append(
                (comp.pos[0] + (ox * ct - oy * st), comp.pos[1] + (ox * st + oy * ct))
            )
    return out


def plane_regions(
    stack: Stack,
    record: Optional[dict],
    pad_points: Dict[str, List[Tuple[float, float]]],
    width: float,
    height: float,
    margin: float = SPLIT_MARGIN_MM,
) -> List[Region]:
    """The fill region of every dedicated ``(layer, net)`` of ``stack``.

    A zone already on the board (the record's ``zone_shapes``) is its net's region
    as drawn. Otherwise, as :func:`pnr.writeback.form_planes` pours it: the net
    with the most pads (``pad_points``: net -> pad centres, graph frame) takes the
    whole outline at priority 0, and every other net on that layer the bounding box
    of its pads plus ``margin``, clamped to the outline, smaller boxes at higher
    priorities (1, 2, ...). A region covering the whole outline is None."""
    shapes: Dict[Tuple[str, str], List[dict]] = {}
    drawn_nets = set()
    for row in (record or {}).get("layers", []):
        drawn_nets |= {(row["name"], n) for n in row.get("zones", [])}
        for z in row.get("zone_shapes", []):
            shapes.setdefault((row["name"], z["net"]), []).append(z)
    by_layer: Dict[str, List[str]] = {}
    for layer, net in stack.dedicated:
        by_layer.setdefault(layer, []).append(net)
    out: List[Region] = []
    for layer, nets in by_layer.items():
        order = sorted(nets, key=lambda n: (-len(pad_points.get(n, [])), n))
        split = []
        for rank, net in enumerate(order):
            drawn = shapes.get((layer, net))
            if drawn:
                for z in drawn:
                    outline = tuple((float(x), float(y)) for x, y in z["outline"])
                    full = _covers(list(outline), width, height)
                    out.append(Region(layer, net, z["priority"], None if full else outline, True))
                continue
            if (layer, net) in drawn_nets:
                # A zone on the board whose outline the record does not carry (a
                # record from before zone shapes): taken as the whole board.
                out.append(Region(layer, net, 0, None, True))
                continue
            if rank == 0:
                out.append(Region(layer, net, 0, None))
                continue
            pts = pad_points.get(net) or []
            if not pts:
                continue
            x0 = max(0.0, min(x for x, _ in pts) - margin)
            x1 = min(width, max(x for x, _ in pts) + margin)
            y0 = max(0.0, min(y for _, y in pts) - margin)
            y1 = min(height, max(y for _, y in pts) + margin)
            split.append((net, ((x0, y0), (x1, y0), (x1, y1), (x0, y1))))
        ranked = sorted(split, key=lambda t: (-abs(_area(list(t[1]))), t[0]))
        for priority, (net, outline) in enumerate(ranked, 1):
            full = _covers(list(outline), width, height)
            out.append(Region(layer, net, priority, None if full else outline))
    return out


def _inside(outline, p) -> bool:
    x, y = p
    hit = False
    for (x0, y0), (x1, y1) in zip(outline, outline[1:] + outline[:1]):
        if (y0 > y) != (y1 > y) and x < x0 + (y - y0) * (x1 - x0) / (y1 - y0):
            hit = not hit
    return hit


def _edge_distance(outline, p) -> float:
    best = float("inf")
    px, py = p
    for (x0, y0), (x1, y1) in zip(outline, outline[1:] + outline[:1]):
        dx, dy = x1 - x0, y1 - y0
        t = (
            0.0
            if dx == dy == 0
            else max(0.0, min(1.0, ((px - x0) * dx + (py - y0) * dy) / (dx * dx + dy * dy)))
        )
        best = min(best, math.hypot(px - (x0 + t * dx), py - (y0 + t * dy)))
    return best


class PlaneAccess:
    """Which through-via sites reach a net's own plane fill (the drop planner's
    test): a via of ``net`` at ``p`` connects when, on at least one dedicated plane
    of the net, it lies inside one of the net's regions by ``inset`` and no other
    net's region of equal or higher priority on that layer comes within
    ``outset`` (that region's fill takes the site, and the via would only touch an
    island of its own net)."""

    def __init__(self, stack: Stack, regions: Sequence[Region], inset: float, outset: float):
        self.stack = stack
        self.inset = inset
        self.outset = outset
        self.by_layer: Dict[str, List[Region]] = {}
        for r in regions:
            self.by_layer.setdefault(r.layer, []).append(r)

    def _free(self, net: str, layer: str) -> bool:
        rows = self.by_layer.get(layer, [])
        own = [r for r in rows if r.net == net and r.outline is None]
        return any(not any(f.net != net and f.priority >= r.priority for f in rows) for r in own)

    def constrained(self, net: str) -> bool:
        """False when every site of ``net`` reaches one of its planes (a plane of
        its own over the whole board): the test can be skipped."""
        layers = self.stack.net_planes(net)
        return bool(layers) and not any(self._free(net, la) for la in layers)

    def site_ok(self, net: str, p: Tuple[float, float]) -> bool:
        for layer in self.stack.net_planes(net):
            rows = self.by_layer.get(layer, [])
            for r in rows:
                if r.net != net:
                    continue
                if r.outline is not None and not (
                    _inside(r.outline, p) and _edge_distance(r.outline, p) >= self.inset
                ):
                    continue
                if any(
                    f.net != net
                    and f.priority >= r.priority
                    and (
                        f.outline is None
                        or _inside(f.outline, p)
                        or _edge_distance(f.outline, p) < self.outset
                    )
                    for f in rows
                ):
                    continue
                return True
        return False
