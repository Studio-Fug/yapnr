"""Pure geometry helpers shared by the placer, legalizer, and metrics.

Everything here is stdlib-only (math + dataclasses) and works off the neutral
:class:`pnr.graph.BoardGraph`. Frame: mm, y-up, origin at the outline's
bottom-left (see :mod:`pnr.graph`).

PNR_PAIR_LANDING_RESERVE=1 (src13, default off): :func:`placement_rects` also
returns the component's diff-pair via landing reserves (:class:`ReserveRect`, see
:mod:`pnr.place.pair_landing`) and tags body rects with their mount side
(:class:`MountedRect`); unset, it returns exactly the plain rects as before.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from pnr.constraints import CompiledConstraints, Enforcement
from pnr.graph import BoardGraph, Component


def hard_group_edges(constraints):
    """Relative distance bounds, independent of absolute XY anchoring."""
    return [
        (con.params["anchor"], ref, con.params["radius_mm"])
        for con in constraints.constraints
        if con.kind == "group" and con.enforcement == Enforcement.HARD
        for ref in con.refs
        if ref != con.params["anchor"]
    ]


def hard_group_limits(constraints, poses, *, partial=False):
    """Relative bounds against current positions; reciprocal when both are known.

    Partial mode is only for held-out batches; complete joint validation follows.
    """
    limits = {}
    for anchor, ref, radius in hard_group_edges(constraints):
        if anchor not in poses and not partial:
            raise ValueError(f"hard group anchor {anchor} has no current pose")
        if anchor in poses:
            limits.setdefault(ref, []).append((*poses[anchor], radius))
        if ref in poses:
            limits.setdefault(anchor, []).append((*poses[ref], radius))
    return limits


def resolve_hard_rotations(constraints):
    rotations = {}
    for con in constraints.hard:
        if con.kind not in ("fixed", "orientation"):
            continue
        for ref in con.refs:
            value = float(con.params.get("rot") or 0) % 360
            if ref in rotations and rotations[ref] != value:
                raise ValueError("conflicting hard rotations")
            rotations[ref] = value
    return rotations


@dataclass(frozen=True)
class Rect:
    """An axis-aligned rectangle by centre + size (mm)."""

    cx: float
    cy: float
    w: float
    h: float

    @property
    def left(self) -> float:
        return self.cx - self.w / 2

    @property
    def right(self) -> float:
        return self.cx + self.w / 2

    @property
    def bottom(self) -> float:
        return self.cy - self.h / 2

    @property
    def top(self) -> float:
        return self.cy + self.h / 2

    def overlaps(self, other: "Rect", gap: float = 0.0) -> bool:
        """True if the two rectangles overlap when each is grown by ``gap/2``.

        A :class:`ReserveRect` decides its own conflicts (see there)."""
        if isinstance(other, ReserveRect) and not isinstance(self, ReserveRect):
            return other.overlaps(self, gap)
        return (
            self.left - gap / 2 < other.right + gap / 2
            and self.right + gap / 2 > other.left - gap / 2
            and self.bottom - gap / 2 < other.top + gap / 2
            and self.top + gap / 2 > other.bottom - gap / 2
        )

    def inside(self, width: float, height: float, eps: float = 1e-6) -> bool:
        """True if the rectangle lies within ``[0,width] x [0,height]``."""
        return (
            self.left >= -eps
            and self.bottom >= -eps
            and self.right <= width + eps
            and self.top <= height + eps
        )


@dataclass(frozen=True)
class MountedRect(Rect):
    """A body/hole reservation that knows the side its owner is mounted on.

    Only produced by :func:`placement_rects` under PNR_PAIR_LANDING_RESERVE=1
    (an assembled block macro: the sides its members are mounted on,
    pair_landing.macro_mount, 'both' when unrecorded); plain :class:`Rect` otherwise."""

    mount: str = ""


@dataclass(frozen=True)
class ReserveRect(Rect):
    """A routing reservation (diff-pair via landing, :mod:`pnr.place.pair_landing`).

    It excludes the bodies of parts MOUNTED on its side (an opposite-side part
    such as a bottom test-point array), never another reservation, and never a
    part that is only present on that side through its plated holes (a top THT
    connector's pins; the landing is routing space of the pair itself). A
    plain Rect of unknown owner is treated as mounted there (conservative).
    """

    side: str = ""
    owner: str = ""
    label: str = ""

    def overlaps(self, other: "Rect", gap: float = 0.0) -> bool:
        if isinstance(other, ReserveRect):
            return False
        mount = getattr(other, "mount", None)
        if mount is not None and mount not in (self.side, "both"):
            return False
        return Rect.overlaps(
            Rect(self.cx, self.cy, self.w, self.h), Rect(other.cx, other.cy, other.w, other.h), gap
        )


def set_component_side(comp: Component, side: str):
    """Flip local pad offsets with the physical footprint (KiCad Flip(..., False)).

    Ingestion stores offsets in the current side's unrotated frame. Merely
    changing the side label would leave the router targeting mirrored pads.
    """
    if side not in ("top", "bottom"):
        raise ValueError(f"Invalid placement side: {side}")
    if comp.side != side:
        for pad in comp.pads:
            pad.offset = (pad.offset[0], -pad.offset[1])
        comp.side = side


def resolve_hard_sides(constraints):
    """Resolve physical side rules without adding a position lock."""
    sides = {}
    for con in constraints.hard:
        if con.kind not in ("fixed", "side") or not con.params.get("side"):
            continue
        for ref in con.refs:
            if ref in sides and sides[ref] != con.params["side"]:
                raise ValueError(f"conflicting hard side rules for {ref}")
            sides[ref] = con.params["side"]
    return sides


def apply_hard_sides(graph, constraints):
    for ref, side in resolve_hard_sides(constraints).items():
        set_component_side(graph.component(ref), side)


def occupied_sides(comp: Component):
    """Reserve through-hole component bodies on both sides, conservatively.

    Assembled hierarchical blocks (``block:`` macros from :mod:`pnr.hier.macro`)
    carry their routed through vias and possibly bottom-side members, so their
    whole outline is reserved on both sides as well.
    """
    if (comp.footprint or "").startswith("block:"):
        return ("top", "bottom")
    return (
        ("top", "bottom")
        if not comp.smd_body and any(p.through_hole for p in comp.pads)
        else (comp.side,)
    )


def courtyard_rect(comp: Component) -> Rect:
    """The component's courtyard as a placed :class:`Rect`.

    Courtyard dimensions are recorded orientation-agnostic; for a 90/270° part we
    swap w/h so the placed extent is correct (no orientation *search* here — this
    just honors the ingested angle)."""
    w, h = comp.courtyard
    if int(round(comp.rot)) % 180 == 90:
        w, h = h, w
    return Rect(comp.pos[0], comp.pos[1], w, h)


def pin_positions(comp: Component) -> List[Tuple[str, Tuple[float, float]]]:
    """Absolute (x, y) of each pad: component pose + rotated pad offset."""
    th = math.radians(comp.rot)
    ct, st = math.cos(th), math.sin(th)
    out = []
    for pad in comp.pads:
        ox, oy = pad.offset
        rx = ox * ct - oy * st
        ry = ox * st + oy * ct
        out.append((pad.name, (comp.pos[0] + rx, comp.pos[1] + ry)))
    return out


def pad_rects(comp: Component) -> List[Tuple[str, str, Rect]]:
    """``(pad_name, net, Rect)`` for each pad — absolute centre + rotation-aware
    (w, h). For a 90/270° part the pad's w/h swap (like the courtyard). Used by the
    detailed router for pad obstacle / access geometry."""
    swap = int(round(comp.rot)) % 180 == 90
    out: List[Tuple[str, str, Rect]] = []
    positions = pin_positions(comp)
    for (name, (x, y)), pad in zip(positions, comp.pads):
        w, h = pad.size
        if swap:
            w, h = h, w
        out.append((name, pad.net, Rect(x, y, w, h)))
    return out


# --- constraint resolution (edge poses, keep-out regions) ------------------


def outline_size(graph: BoardGraph, constraints: CompiledConstraints) -> Tuple[float, float]:
    """The placement region: the constraint outline if given, else the ingested
    board's bounding box."""
    b = constraints.board
    if b.width and b.height:
        return (float(b.width), float(b.height))
    if graph.outline:
        return (graph.outline.width, graph.outline.height)
    raise ValueError("no board outline in constraints or graph")


def _edge_pose(
    edge: Optional[str],
    align: Optional[str],
    w: float,
    h: float,
    width: float,
    height: float,
    overhang: float = 0.0,
) -> Tuple[float, float]:
    """Resolve an edge+align hint to a concrete centre.

    By default the courtyard sits flush against ``edge`` (align controls the free
    axis; default = centre). ``overhang`` (mm) shifts the part *past* the edge by
    that much — for an edge connector (USB-C) whose mating face must protrude
    through an enclosure wall so a cable seats fully. Negative insets it inward.
    """
    cx, cy = width / 2, height / 2
    if edge == "south":
        cy = h / 2 - overhang
    elif edge == "north":
        cy = height - h / 2 + overhang
    elif edge == "west":
        cx = w / 2 - overhang
    elif edge == "east":
        cx = width - w / 2 + overhang
    if align == "left":
        cx = w / 2
    elif align == "right":
        cx = width - w / 2
    return (cx, cy)


def resolve_fixed_poses(
    graph: BoardGraph, constraints: CompiledConstraints
) -> Dict[str, Tuple[float, float]]:
    """Map each `fixed` component ref to its resolved centre (mm)."""
    width, height = outline_size(graph, constraints)
    poses: Dict[str, Tuple[float, float]] = {}
    for c in constraints.constraints:
        if c.kind != "fixed":
            continue
        for ref in c.refs:
            try:
                comp = graph.component(ref)
            except KeyError:
                continue
            w, h = courtyard_rect(comp).w, courtyard_rect(comp).h
            at = c.params.get("at")
            if at:
                poses[ref] = (float(at[0]), float(at[1]))
            else:
                poses[ref] = _edge_pose(
                    c.params.get("edge"),
                    c.params.get("align"),
                    w,
                    h,
                    width,
                    height,
                    overhang=float(c.params.get("overhang_mm") or 0.0),
                )
    return poses


def keepout_rects(
    graph: BoardGraph,
    constraints: CompiledConstraints,
    placed: Dict[str, Tuple[float, float]],
) -> List[Rect]:
    """Resolve keep-out constraints to absolute rectangles.

    A ``ref``-relative keep-out (``extent: {edge, depth_mm}``) sits against the
    named component's courtyard edge and extends ``depth_mm`` outward; the
    component's centre is read from ``placed`` (its resolved/current pose). An
    absolute ``polygon`` keep-out is taken as its bounding box.
    """
    rects: List[Rect] = []
    for c in constraints.constraints:
        if c.kind != "keepout":
            continue
        poly = c.params.get("polygon")
        if poly:
            xs = [float(p[0]) for p in poly]
            ys = [float(p[1]) for p in poly]
            rects.append(
                Rect(
                    (min(xs) + max(xs)) / 2,
                    (min(ys) + max(ys)) / 2,
                    max(xs) - min(xs),
                    max(ys) - min(ys),
                )
            )
            continue
        extent = c.params.get("extent") or {}
        depth = float(extent.get("depth_mm", 0))
        for ref in c.refs:
            try:
                comp = graph.component(ref)
            except KeyError:
                continue
            cx, cy = placed.get(ref, comp.pos)
            w, h = comp.courtyard
            local = {
                "north": (0, (h + depth) / 2, w, depth),
                "south": (0, -(h + depth) / 2, w, depth),
                "east": ((w + depth) / 2, 0, depth, h),
                "west": (-(w + depth) / 2, 0, depth, h),
            }.get(extent.get("edge"))
            if local is None:
                continue
            angle = resolve_hard_rotations(constraints).get(ref, comp.rot)
            rad = math.radians(angle)
            ct, st = math.cos(rad), math.sin(rad)
            x, y, kw, kh = local
            rects.append(
                Rect(
                    cx + x * ct - y * st,
                    cy + x * st + y * ct,
                    abs(kw * ct) + abs(kh * st),
                    abs(kw * st) + abs(kh * ct),
                )
            )
    return rects


def placement_rects(comp):
    """Physical reservations: body courtyard plus opposite-side plated holes.

    KiCad's explicit SMD attribute distinguishes a surface body containing
    thermal holes from a through-hole body/connector. Holes still exclude
    opposite components at their actual pad extents, not the whole body.

    A block macro with a per-side hull (PNR_MACRO_HULL=1, :mod:`pnr.place.hull`)
    reserves only its hull cover rectangles, each on its own side, plus its
    inner-layer cover on the ``inner`` plane; with that flag drilled pads and
    solid block macros also reserve the ``inner`` plane
    (:func:`pnr.place.hull.inner_rects`), so drilled parts clear block inner copper.

    With both PNR_MACRO_HULL=1 and PNR_PAIR_LANDING_RESERVE=1 (src15 merge) the
    hull/inner rects are tagged with the macro's mount side like any body rect and
    the component's landing reserves are appended; each flag alone returns exactly
    what its own line (src12n / src13) returned.
    """
    result = None
    if getattr(comp, "hull", None):
        from .hull import enabled, hull_placement_rects

        if enabled():
            result = hull_placement_rects(comp)
    if result is None:
        result = [(side, courtyard_rect(comp)) for side in occupied_sides(comp)]
        if comp.smd_body:
            opposite = "bottom" if comp.side == "top" else "top"
            for pad, (_, _, rect) in zip(comp.pads, pad_rects(comp)):
                if pad.through_hole:
                    result.append((opposite, rect))
        if os.environ.get("PNR_MACRO_HULL") == "1":
            from .hull import inner_rects

            result += inner_rects(comp)
    if _landing.enabled():
        # PNR_PAIR_LANDING_RESERVE=1: bodies carry their mount side and the
        # component's diff-pair via landings are added as ReserveRects.
        mount = (
            _landing.macro_mount(comp) if (comp.footprint or "").startswith("block:") else comp.side
        )
        result = [
            (side, MountedRect(r.cx, r.cy, r.w, r.h, mount=mount)) for side, r in result
        ] + _landing.reserve_rects(comp)
    return result


from . import pair_landing as _landing  # noqa: E402  (stdlib-only; imports this module)
