"""Routed length and delay of a net, measured the way KiCad's DRC measures it.

The judge of a length or skew rule is KiCad's own length calculation (KiCad 10
``length`` / ``skew`` constraints; LENGTH_DELAY_CALCULATION::CalculateLengthDetails
with the DRC's optimisations), which is not the raw sum of a net's track lengths.
:func:`net_length` transcribes KiCad 10.0.6, in integer nanometres:

* **Vias** first. A via counts the stackup distance between the outermost copper
  layers it joins: the layers of the tracks that end at its centre or inside its
  copper, and the side of a pad it overlaps (an SMD pad's layer, a through-hole pad's
  footprint side). An outer copper layer at either end counts in full, an inner one
  by half, everything between in full (F.Cu to B.Cu on the default 1.6 mm build is
  1.58 mm; F.Cu to In1.Cu on the 4L-SGPS ladder stack 0.035 + 0.2104 + 0.0076 =
  0.2530 mm). A via joining one layer counts zero.
* **Lines.** Tracks merge into lines where, once the line being grown leaves the
  endpoint map, exactly one other track ends at the point, on the same layer (so a
  line runs through a via or pad centre where only two same-layer tracks meet).
* **Clipping in vias, then in pads.** A line whose first (else last) point lies in a
  via's round copper is cut at its first vertex outside the via: that part becomes
  via centre -> the crossing with the via's edge. The start need not be the via
  centre (a pad centre inside a via's copper is replaced by the via centre). Then a
  line ending at a pad's centre, on a layer the pad flashes, is cut the same way at
  the pad outline. A line wholly inside keeps its length. KiCad's inside tests are
  half-open (in the board's frame, y down: the lower and left edges are inside), so
  points are measured in the routed board's own frame (:func:`board_frame`).
* Each segment counts its rounded Euclidean length in nanometres.

Junctions of three or more track ends (branching multi-pin nets) are merged in
KiCad's item order, which is a memory order; for them the model is close, not exact.
Two-pin nets, which pairs and length-match groups are, measure exactly.

:func:`net_length` measures one net from geometry (tracks, vias, pad outlines) and a
stackup; :func:`board_route_lengths` measures nets of a detailed route
(:class:`pnr.route.detail.router.BoardRoute`) against the placed graph's pads.
``pnr.length_oracle`` asks KiCad itself.

Delay (ps) uses the stackup's per-layer propagation delay (:func:`pnr.si.physics.line`,
from the layer's effective permittivity) for track, and the dielectric's
``sqrt(er) / c`` for via barrels.

The stackup is the plain dict :mod:`pnr.si.physics` uses (``layers``: copper and
dielectric rows top to bottom with ``t_mm`` and ``er``; optional ``planes``).
:func:`read_stackup` takes it from a board's ``(stackup ...)`` block; a board without
one uses KiCad's default stack (:func:`default_stackup`). Pure stdlib.
"""

from __future__ import annotations

import math
import random
import re
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple

Point = Tuple[float, float]
NM = 1e6
C_MM_PER_PS = 0.299792458

# KiCad's default stack (BOARD_STACKUP::BuildDefaultStackupList): a 1.6 mm board,
# 35 um copper, 10 um solder mask on each side, the dielectric split evenly.
KICAD_BOARD_MM = 1.6
KICAD_COPPER_MM = 0.035
KICAD_MASK_MM = 0.01
KICAD_ER = 4.5


def _key(p: Point) -> Tuple[int, int]:
    return (int(round(p[0] * NM)), int(round(p[1] * NM)))


# ------------------------------------------------------------------ stackup


def copper_names(count: int) -> List[str]:
    """KiCad's copper layer names, outer to outer."""
    if count < 2:
        return ["F.Cu"][:count]
    return ["F.Cu"] + ["In%d.Cu" % i for i in range(1, count - 1)] + ["B.Cu"]


def default_stackup(copper_layers: int = 2) -> dict:
    """KiCad's default stack for ``copper_layers`` layers (2 layers: 0.035 / 1.51 /
    0.035 mm, a 1.58 mm via)."""
    names = copper_names(copper_layers)
    gap = (KICAD_BOARD_MM - 2 * KICAD_MASK_MM - len(names) * KICAD_COPPER_MM) / max(
        1, len(names) - 1
    )
    layers: List[dict] = []
    for index, name in enumerate(names):
        layers.append(dict(name=name, kind="copper", t_mm=KICAD_COPPER_MM))
        if index < len(names) - 1:
            layers.append(
                dict(name="dielectric %d" % (index + 1), kind="dielectric", t_mm=gap, er=KICAD_ER)
            )
    return dict(name="kicad-default-%dL" % len(names), layers=layers)


def _blocks(text: str, head: str) -> Iterable[str]:
    """Each balanced ``(head ...)`` s-expression in ``text``."""
    start = text.find("(" + head)
    while start >= 0:
        depth = 0
        for end in range(start, len(text)):
            ch = text[end]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    yield text[start : end + 1]
                    break
        start = text.find("(" + head, start + 1)


def read_stackup(text: str) -> Optional[dict]:
    """The board's ``(stackup ...)`` block as a stackup dict, or None when the board
    declares none. Copper layers typed ``power`` in the board's layer table are the
    reference ``planes``."""
    stack = next(iter(_blocks(text, "stackup")), None)
    if stack is None:
        return None
    layers: List[dict] = []
    for row in _blocks(stack, "layer "):
        name = re.match(r'\(layer\s+"([^"]+)"', row)
        kind = re.search(r'\(type\s+"([^"]+)"\)', row)
        thick = re.search(r"\(thickness\s+([-\d.eE+]+)", row)
        if not name or not kind:
            continue
        kind = kind.group(1).lower()
        if kind == "copper":
            layers.append(
                dict(name=name.group(1), kind="copper", t_mm=float(thick.group(1)) if thick else 0)
            )
        elif kind in ("core", "prepreg") or name.group(1).startswith("dielectric"):
            er = re.search(r"\(epsilon_r\s+([-\d.eE+]+)", row)
            # A dielectric's sub-layers (``addsublayer``) all count.
            subs = re.findall(r"\(thickness\s+([-\d.eE+]+)", row)
            layers.append(
                dict(
                    name=name.group(1),
                    kind="dielectric",
                    t_mm=sum(float(x) for x in subs),
                    er=float(er.group(1)) if er else KICAD_ER,
                )
            )
    if not any(x["kind"] == "copper" for x in layers):
        return None
    out = dict(name="board", layers=layers)
    table = next(iter(_blocks(text, "layers")), "")
    planes = re.findall(r'\(\s*\d+\s+"([^"]+\.Cu)"\s+power', table)
    if planes:
        out["planes"] = planes
    return out


def attach_stackup(rules: dict, board_text: str) -> bool:
    """Give a design that declares pairs or groups its board's stackup
    (``rules["stackup"]``, read by the length tuner). Rules without pairs or groups,
    or of a board without a stackup block, are left as they are. Returns whether the
    rules changed."""
    if rules.get("stackup") or not (rules.get("diff_pairs") or rules.get("length_match")):
        return False
    st = read_stackup(board_text)
    if st is None:
        return False
    rules["stackup"] = st
    return True


def stackup_copper(st: dict) -> List[str]:
    return [x["name"] for x in st["layers"] if x["kind"] == "copper"]


def layer_distance(st: dict, a: str, b: str) -> float:
    """KiCad's via length between copper layers ``a`` and ``b``
    (BOARD_STACKUP::GetLayerDistance, in whole nanometres): an outer copper layer at
    either end in full, an inner one by half, everything between in full."""
    if a == b:
        return 0.0
    names = stackup_copper(st)
    ia, ib = names.index(a), names.index(b)
    if ia > ib:
        a, b = b, a
    outer = {names[0], names[-1]}
    total, inside = 0, False
    for row in st["layers"]:
        nm = int(round(float(row["t_mm"]) * NM))
        if row["kind"] == "copper" and row["name"] == a:
            inside = True
            total += nm if a in outer else nm // 2
            continue
        if not inside:
            continue
        if row["kind"] == "copper" and row["name"] == b:
            total += nm if b in outer else nm // 2
            break
        total += nm
    return total / NM


def _barrel_ps_per_mm(st: dict) -> float:
    rows = [x for x in st["layers"] if x["kind"] == "dielectric"]
    thick = sum(x["t_mm"] for x in rows) or 1.0
    er = sum(x["t_mm"] * float(x.get("er", KICAD_ER)) for x in rows) / thick
    return math.sqrt(er) / C_MM_PER_PS


class DelayModel:
    """Per-layer track delay (ps/mm) for a width, and the via barrel's ps/mm."""

    def __init__(self, st: dict):
        from pnr.si import physics

        self._st = dict(st)
        self._st.setdefault("planes", [])
        self._physics = physics
        self._cache: Dict[Tuple[str, float], float] = {}
        self.barrel_ps_per_mm = _barrel_ps_per_mm(st)

    def track(self, layer: str, width: float) -> float:
        key = (layer, round(width, 6))
        if key not in self._cache:
            self._cache[key] = self._physics.line(self._st, layer, width)["td_ps_per_mm"]
        return self._cache[key]


# ------------------------------------------------------------------ pads


@dataclass(frozen=True)
class PadCopper:
    """A pad's copper: its centre, copper layers and convex outline (engine mm).
    ``side`` is the layer a via in the pad joins: an SMD pad's own layer, a
    through-hole pad's footprint side."""

    net: str
    centre: Point
    layers: FrozenSet[str]
    outline: Tuple[Point, ...]
    side: str = "F.Cu"

    def contains(self, p: Point) -> bool:
        return _inside_convex(self.outline, p)


def rounded_rect(
    centre: Point, size: Tuple[float, float], corner: float = 0.0, rot_deg: float = 0.0, arc=8
) -> Tuple[Point, ...]:
    """Counter-clockwise outline of a ``size`` rectangle with ``corner`` radius,
    turned by ``rot_deg`` about ``centre``."""
    w, h = size
    r = max(0.0, min(corner, w / 2, h / 2))
    pts: List[Point] = []
    if r <= 1e-9:
        pts = [(w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2), (-w / 2, -h / 2)]
    else:
        for cx, cy, start in (
            (w / 2 - r, -h / 2 + r, -90.0),
            (w / 2 - r, h / 2 - r, 0.0),
            (-w / 2 + r, h / 2 - r, 90.0),
            (-w / 2 + r, -h / 2 + r, 180.0),
        ):
            for k in range(arc + 1):
                t = math.radians(start + 90.0 * k / arc)
                pts.append((cx + r * math.cos(t), cy + r * math.sin(t)))
    th = math.radians(rot_deg)
    co, si = math.cos(th), math.sin(th)
    return tuple((centre[0] + x * co - y * si, centre[1] + x * si + y * co) for x, y in pts)


def _cross(o: Point, a: Point, b: Point) -> float:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _inside_convex(poly: Sequence[Point], p: Point) -> bool:
    """Inside or on a convex counter-clockwise outline."""
    n = len(poly)
    for k in range(n):
        if _cross(poly[k], poly[(k + 1) % n], p) < -1e-9:
            return False
    return True


def graph_pads(
    graph, nets: Optional[Iterable[str]] = None, conservative: bool = False
) -> List[PadCopper]:
    """The pad copper of ``nets`` (every connected pad when None) on a placed graph:
    the pad rectangle with its land corner, at the part's rotation, on the part's side
    (every copper layer for a through-hole pad: :data:`ALL_LAYERS`). A land the graph
    does not know exactly gets KiCad's 25 % roundrect corner, or with ``conservative``
    its full bounding rectangle; ``conservative`` also includes unconnected pads (for
    clearance, not length)."""
    from pnr.place.geometry import pin_positions

    wanted = None if nets is None else set(nets)
    out: List[PadCopper] = []
    for comp in graph.components:
        side = "B.Cu" if comp.side == "bottom" else "F.Cu"
        for (_name, centre), pad in zip(pin_positions(comp), comp.pads):
            if (not pad.net and not conservative) or (wanted is not None and pad.net not in wanted):
                continue
            w, h = pad.size
            if w <= 0 or h <= 0:
                continue
            if pad.land_corner is not None:
                corner = pad.land_corner
            else:
                corner = 0.0 if conservative else 0.25 * min(w, h)
            layers = ALL_LAYERS if pad.through_hole else frozenset((side,))
            out.append(
                PadCopper(
                    pad.net, centre, layers, rounded_rect(centre, (w, h), corner, comp.rot), side
                )
            )
    return out


ALL_LAYERS = frozenset(("*",))


def _pad_on(pad: PadCopper, layer: str) -> bool:
    return "*" in pad.layers or layer in pad.layers


# ------------------------------------------------------------------ the model
#
# A transcription of KiCad 10.0.6's LENGTH_DELAY_CALCULATION::CalculateLengthDetails
# with the DRC's optimisations (OptimiseVias, MergeTracks, OptimiseTracesInPads), in
# integer nanometres like KiCad.


Track = Tuple[str, Point, Point, float]  # layer, start, end, width
IPoint = Tuple[int, int]


@dataclass
class NetLength:
    net: str
    track_mm: float = 0.0
    via_mm: float = 0.0
    delay_ps: Optional[float] = None
    vias: int = 0
    lines: List[Tuple[str, List[Point]]] = field(default_factory=list)
    # The lengths KiCad may report when the net branches (three or more track ends at
    # one point): KiCad merges such junctions in its items' memory order, so the
    # result depends on it. Equal to the total for a net without a branch.
    low_mm: Optional[float] = None
    high_mm: Optional[float] = None
    low_ps: Optional[float] = None
    high_ps: Optional[float] = None

    @property
    def total_mm(self) -> float:
        return self.track_mm + self.via_mm

    def span(self, unit: str = "mm") -> Tuple[float, float]:
        """(lowest, highest) length (``mm``) or delay (``ps``) KiCad may report."""
        if unit == "ps":
            return (self.low_ps, self.high_ps)
        return (self.low_mm, self.high_mm)


def _seg_nm(a: IPoint, b: IPoint) -> int:
    """KiCad's SEG::Length(): the rounded Euclidean norm."""
    return int(round(math.hypot(b[0] - a[0], b[1] - a[1])))


def _chain_nm(pts: Sequence[IPoint]) -> int:
    return sum(_seg_nm(a, b) for a, b in zip(pts, pts[1:]))


def _append(chain: List[IPoint], p: IPoint) -> None:
    """SHAPE_LINE_CHAIN::Append: a point equal to the last one is dropped."""
    if not chain or chain[-1] != p:
        chain.append(p)


def _rescale(a: int, b: int, c: int) -> int:
    """KiCad's rescale(a, b, c): a * b / c rounded half away from zero."""
    num = a * b
    q, r = divmod(abs(num), abs(c))
    if 2 * r >= abs(c):
        q += 1
    return q if (num >= 0) == (c > 0) else -q


def _point_inside(poly: Sequence[IPoint], p: IPoint) -> bool:
    """SHAPE_LINE_CHAIN_BASE::PointInside (ray casting, accuracy 0)."""
    inside = False
    n = len(poly)
    for k in range(n):
        p1, p2 = poly[k], poly[(k + 1) % n]
        dy = p2[1] - p1[1]
        if dy == 0:
            continue
        d = _rescale(p2[0] - p1[0], p[1] - p1[1], dy)
        if (p1[1] >= p[1]) != (p2[1] >= p[1]) and p[0] - p1[0] < d:
            inside = not inside
    return inside


def _seg_intersection(a: IPoint, b: IPoint, c: IPoint, d: IPoint) -> Optional[IPoint]:
    """SEG::Intersect: the crossing of segments ab and cd (rounded), or None."""
    rx, ry = b[0] - a[0], b[1] - a[1]
    sx, sy = d[0] - c[0], d[1] - c[1]
    den = rx * sy - ry * sx
    if den == 0:
        return None
    qx, qy = c[0] - a[0], c[1] - a[1]
    t = qx * sy - qy * sx
    u = qx * ry - qy * rx
    if den < 0:
        den, t, u = -den, -t, -u
    if t < 0 or t > den or u < 0 or u > den:
        return None
    return (a[0] + int(round(rx * t / den)), a[1] + int(round(ry * t / den)))


def _poly_crossing(poly: Sequence[IPoint], outside: IPoint, inside: IPoint) -> Optional[IPoint]:
    """SHAPE_POLY_SET::Collide(SEG(outside, inside)) location: the crossing with the
    first outline edge the segment meets."""
    n = len(poly)
    for k in range(n):
        hit = _seg_intersection(outside, inside, poly[k], poly[(k + 1) % n])
        if hit is not None:
            return hit
    return None


def _circle_crossing(centre: IPoint, r: int, outside: IPoint, inside: IPoint) -> Optional[IPoint]:
    """CIRCLE::Intersect(SEG): the intersection of the segment with the circle that is
    closest to the outside vertex."""
    ax, ay = outside[0] - centre[0], outside[1] - centre[1]
    dx, dy = inside[0] - outside[0], inside[1] - outside[1]
    qa = dx * dx + dy * dy
    if qa == 0:
        return None
    qb = 2 * (ax * dx + ay * dy)
    qc = ax * ax + ay * ay - r * r
    disc = qb * qb - 4 * qa * qc
    if disc < 0:
        return None
    root = math.sqrt(disc)
    best = None
    for t in ((-qb - root) / (2 * qa), (-qb + root) / (2 * qa)):
        if -1e-9 <= t <= 1 + 1e-9:
            p = (outside[0] + int(round(dx * t)), outside[1] + int(round(dy * t)))
            if best is None or _d2(p, outside) < _d2(best, outside):
                best = p
    return best


def _d2(a: IPoint, b: IPoint) -> int:
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2


def _in_circle(centre: IPoint, r: int, p: IPoint) -> bool:
    """SHAPE_CIRCLE::Collide(point, 0)."""
    d2 = _d2(centre, p)
    return d2 == 0 or d2 < r * r


def _clip(line: List[IPoint], forward: bool, inside, crossing, centre: IPoint) -> List[IPoint]:
    """clipLineToPad / clipLineToVia (KiCad 10.0.6): the part from the line's first
    (``forward``) or last point up to the first vertex outside becomes a straight
    run from ``centre`` to where the line leaves the shape."""
    n = len(line)
    order = range(1, n) if forward else range(n - 2, -1, -1)
    delta = 1 if forward else -1
    first_out = None
    hit = None
    for vertex in order:
        if not inside(line[vertex]):
            first_out = vertex
            hit = crossing(line[vertex], line[vertex - delta])
            break
    if first_out is None:
        return line  # every point inside: nothing to clip
    out: List[IPoint] = []
    if forward:
        _append(out, centre)
        if hit is not None:
            _append(out, hit)
        for p in line[first_out:]:
            _append(out, p)
    else:
        for p in line[: first_out + 1]:
            _append(out, p)
        if hit is not None:
            _append(out, hit)
        _append(out, centre)
    return out


@dataclass
class _Line:
    layer: str
    width: float
    pts: List[IPoint]
    status: int = 0  # 0 unmerged, 1 merged in use, 2 merged retired


def _merge_lines(lines: List[_Line]) -> None:
    """LENGTH_DELAY_CALCULATION::mergeLines (10.0.6): two lines join where, once the
    current line leaves the endpoint map, exactly one other line ends, on its layer."""
    where: Dict[IPoint, Dict[int, None]] = {}
    for k, line in enumerate(lines):
        where.setdefault(line.pts[0], {})[k] = None
        where.setdefault(line.pts[-1], {})[k] = None

    def remove(k):
        where.get(lines[k].pts[0], {}).pop(k, None)
        where.get(lines[k].pts[-1], {}).pop(k, None)

    def try_merge(at_start: bool, k: int) -> bool:
        primary = lines[k]
        pos = primary.pts[0] if at_start else primary.pts[-1]
        bucket = where.get(pos)
        if bucket is None or len(bucket) != 1:
            return False
        other_k = next(iter(bucket))
        other = lines[other_k]
        if other.layer != primary.layer:
            return False
        other.status = 2
        sec = other.pts
        if at_start:
            merged = list(reversed(sec)) if sec[0] == primary.pts[0] else list(sec)
            for p in primary.pts:
                _append(merged, p)
            primary.pts = merged
        else:
            tail = sec if sec[0] == primary.pts[-1] else list(reversed(sec))
            for p in tail:
                _append(primary.pts, p)
        # KiCad removes the merged line by its own (pre-merge) endpoints.
        where.get(sec[0], {}).pop(other_k, None)
        where.get(sec[-1], {}).pop(other_k, None)
        return True

    for k, line in enumerate(lines):
        if line.status != 0:
            continue
        remove(k)
        line.status = 1
        while True:
            started = try_merge(True, k)
            ended = try_merge(False, k)
            if not started and not ended:
                break


def _via_layers(
    via: IPoint,
    radius: int,
    tracks: Sequence[Tuple[str, IPoint, IPoint]],
    pad: Optional["_Pad"],
    order: Dict[str, int],
) -> Tuple[str, ...]:
    """optimiseVias: the layers of the tracks ending at or within the via's copper,
    and the side of a pad the via connects to."""
    layers = set()
    r2 = radius * radius
    for layer, a, b in tracks:
        for p in (a, b):
            d2 = _d2(p, via)
            if d2 == 0 or d2 < r2:
                layers.add(layer)
    if pad is not None:
        layers.add(pad.side)
    return tuple(sorted((x for x in layers if x in order), key=order.get))


@dataclass
class _Pad:
    centre: IPoint
    poly: List[IPoint]
    layers: FrozenSet[str]
    side: str

    def flashes(self, layer: str) -> bool:
        return "*" in self.layers or layer in self.layers


def board_frame(height_mm: float, offset_mm: float = 30.0):
    """Engine millimetres (y up, origin at the outline's lower left) to the routed
    board's nanometres (y down, page offset), exactly as :func:`pnr.writeback.to_pcb_nm`
    writes them. KiCad's inside tests are half-open, so they depend on the frame."""

    def frame(p: Point) -> IPoint:
        return (
            int(round((offset_mm + p[0]) * NM)),
            int(round((offset_mm + (height_mm - p[1])) * NM)),
        )

    return frame


def net_length(
    net: str,
    tracks: Sequence[Track],
    vias: Sequence[Sequence[float]],
    pads: Sequence[PadCopper],
    st: dict,
    delay: Optional[DelayModel] = None,
    via_radius: Optional[float] = None,
    frame=None,
    orders: int = 16,
) -> NetLength:
    """KiCad-equivalent length of one net from its ``tracks``, vias (``(x, y)`` with
    ``via_radius``, or ``(x, y, radius)``) and pads (those of other nets are ignored):
    the DRC's length of the net's items (KiCad 10.0.6, module docstring). ``frame``
    maps a point to the board's nanometres (:func:`board_frame` for engine
    geometry); by default the points already are the board's, in millimetres.

    The total takes the tracks layer by layer (F.Cu first, then by KiCad layer id),
    each layer in the given order, the order KiCad's file holds them in. A net that
    branches is measured again in ``orders - 1`` other orders (reversed, then seeded
    shuffles) for the range KiCad's own order may give (``low_mm`` .. ``high_mm``)."""
    _nm = frame or _key
    mine = [p for p in pads if p.net == net]
    pad_items = [_Pad(_nm(p.centre), [_nm(q) for q in p.outline], p.layers, p.side) for p in mine]
    raw = [(t[0], _nm(t[1]), _nm(t[2]), float(t[3])) for t in tracks]
    via_items = []
    for v in vias:
        radius = v[2] if len(v) > 2 else via_radius
        via_items.append((_nm((v[0], v[1])), int(round(float(radius or 0.0) * NM))))
    names = stackup_copper(st)
    order = {n: k for k, n in enumerate(names)}
    result = NetLength(net)
    via_ps = 0.0

    # optimiseVias (on the tracks before merging).
    plain = [(layer, a, b) for layer, a, b, _w in raw]
    for centre, radius in via_items:
        pad = next((p for p in pad_items if _via_touches_pad(centre, radius, p)), None)
        span = _via_layers(centre, radius, plain, pad, order)
        mm = layer_distance(st, span[0], span[-1]) if len(span) >= 2 else 0.0
        result.via_mm += mm
        result.vias += 1
        if delay is not None:
            via_ps += mm * delay.barrel_ps_per_mm

    # KiCad saves a net's tracks layer by layer, F.Cu first: measure in that order.
    raw = sorted(raw, key=lambda t: _kicad_layer_id(t[0]))
    track_mm, track_ps, result.lines = _track_length(raw, via_items, pad_items, delay)
    result.track_mm = track_mm
    totals = [(result.via_mm + track_mm, via_ps + track_ps)]
    if orders > 1 and _branches(raw):
        # KiCad saves a net's tracks layer by layer (F.Cu first, then in layer id
        # order) and in random UUID order within a layer, and measures them in its
        # items' memory order, which mostly follows the file: half the samples keep
        # the layers in that order, half shuffle freely.
        rng = random.Random(len(raw))
        index = list(range(len(raw)))
        by_layer: Dict[str, List[int]] = {}
        for k, (layer, _a, _b, _w) in enumerate(raw):
            by_layer.setdefault(layer, []).append(k)
        layers = sorted(by_layer, key=_kicad_layer_id)
        perms = [index[::-1]]
        for n in range(orders - 2):
            if n % 2 == 0:
                perms.append(
                    [k for la in layers for k in rng.sample(by_layer[la], len(by_layer[la]))]
                )
            else:
                perms.append(rng.sample(index, len(index)))
        for perm in perms:
            mm, ps, _lines = _track_length([raw[k] for k in perm], via_items, pad_items, delay)
            totals.append((result.via_mm + mm, via_ps + ps))
    result.low_mm = min(t[0] for t in totals)
    result.high_mm = max(t[0] for t in totals)
    if delay is not None:
        result.delay_ps = via_ps + track_ps
        result.low_ps = min(t[1] for t in totals)
        result.high_ps = max(t[1] for t in totals)
    return result


def _kicad_layer_id(name: str) -> int:
    """KiCad 9+ copper layer ids: F.Cu 0, B.Cu 2, InN.Cu 2 N + 2."""
    if name == "F.Cu":
        return 0
    if name == "B.Cu":
        return 2
    m = re.match(r"In(\d+)\.Cu$", name)
    return 2 * int(m.group(1)) + 2 if m else 1000


def _branches(raw) -> bool:
    """Three or more track ends at one point: KiCad's merge there depends on order."""
    ends: Dict[IPoint, int] = {}
    for _layer, a, b, _w in raw:
        for p in (a, b):
            ends[p] = ends.get(p, 0) + 1
    return any(n >= 3 for n in ends.values())


def _track_length(raw, via_items, pad_items, delay):
    """Track length (mm), its delay (ps) and the measured lines, for the tracks in
    this order: merge, clip in vias, clip in pads (module docstring)."""
    lines = [_Line(layer, w, [a, b]) for layer, a, b, w in raw]
    _merge_lines(lines)
    live = [line for line in lines if line.status == 1]

    # Clip traces inside via pads, then inside pads.
    for centre, radius in via_items:
        if radius <= 0:
            continue

        def inside(p, c=centre, r=radius):
            return _in_circle(c, r, p)

        def crossing(outside, inner, c=centre, r=radius):
            return _circle_crossing(c, r, outside, inner)

        for line in live:
            if len(line.pts) < 2:
                continue
            if inside(line.pts[0]):
                line.pts = _clip(line.pts, True, inside, crossing, centre)
            elif inside(line.pts[-1]):
                line.pts = _clip(line.pts, False, inside, crossing, centre)
    for pad in pad_items:

        def inside(p, poly=pad.poly):
            return _point_inside(poly, p)

        def crossing(outside, inner, poly=pad.poly):
            return _poly_crossing(poly, outside, inner)

        for line in live:
            if len(line.pts) < 2:
                continue
            if line.pts[0] != pad.centre and line.pts[-1] != pad.centre:
                continue
            if not pad.flashes(line.layer):
                continue
            if inside(line.pts[0]):
                line.pts = _clip(line.pts, True, inside, crossing, pad.centre)
            elif inside(line.pts[-1]):
                line.pts = _clip(line.pts, False, inside, crossing, pad.centre)

    track_mm = track_ps = 0.0
    out = []
    for line in live:
        mm = _chain_nm(line.pts) / NM
        track_mm += mm
        out.append((line.layer, [(x / NM, y / NM) for x, y in line.pts]))
        if delay is not None:
            track_ps += mm * delay.track(line.layer, line.width)
    return track_mm, track_ps, out


def _via_touches_pad(centre: IPoint, radius: int, pad: _Pad) -> bool:
    """The via's copper overlaps the pad's (KiCad connects them)."""
    if _point_inside(pad.poly, centre):
        return True
    n = len(pad.poly)
    r2 = radius * radius
    for k in range(n):
        a, b = pad.poly[k], pad.poly[(k + 1) % n]
        vx, vy = b[0] - a[0], b[1] - a[1]
        ll = vx * vx + vy * vy
        t = (
            0.0
            if ll == 0
            else max(0.0, min(1.0, ((centre[0] - a[0]) * vx + (centre[1] - a[1]) * vy) / ll))
        )
        px, py = a[0] + t * vx, a[1] + t * vy
        if (px - centre[0]) ** 2 + (py - centre[1]) ** 2 < r2:
            return True
    return False


def board_route_lengths(
    board_route,
    graph,
    nets: Iterable[str],
    st: dict,
    delay: Optional[DelayModel] = None,
    via_radius: Optional[float] = None,
) -> Dict[str, NetLength]:
    """KiCad-equivalent lengths of ``nets`` in a detailed route (``tracks`` as
    ``(net, layer, a, b, width)``, ``vias`` as ``(net, x, y)`` of radius
    ``via_radius``), in the frame the routed board is written in."""
    frame = board_frame(graph.outline.height) if graph.outline is not None else None
    nets = list(dict.fromkeys(nets))
    wanted = set(nets)
    pads = graph_pads(graph, wanted)
    tracks: Dict[str, List[Track]] = {n: [] for n in nets}
    vias: Dict[str, List[Point]] = {n: [] for n in nets}
    for net, layer, a, b, width in board_route.tracks:
        if net in wanted:
            tracks[net].append((layer, tuple(a), tuple(b), width))
    for net, x, y in board_route.vias:
        if net in wanted:
            vias[net].append((x, y))
    return {n: net_length(n, tracks[n], vias[n], pads, st, delay, via_radius, frame) for n in nets}
