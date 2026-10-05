"""Placement orchestrator: global placement → legalization → report.

PNR_PAIR_LANDING_RESERVE=1 (src13, default off): place() attaches the diff-pair
via landing reserves of pnr.place.pair_landing (from ``channel_rules``) before
legalization; legality/report then include them through placement_rects.

PNR_COMPACT=1 (default off; :mod:`pnr.place.compact`, docs/design/compact-placement.md):
spread 1.0 and starts drawn in a cluster box (``GP``), the compact legalizer settings
(``LEGALIZE``: courtyard gap, copper margins, 0.125 mm slots, pads off the outline) and
offset courtyards (``COURTYARD``) wherever a part has an off-centre body.

Legalizer and global-placement switches (:mod:`pnr.legalize_flags`, default off, with or
without PNR_COMPACT; docs/design/compact-placement.md section 11): ``PNR_GP_POLISH`` /
``PNR_GP_CHANNELS`` end global placement with a phase on the legalizer's slots (and its
channel cost, :mod:`pnr.place.gp_polish`), ``PNR_LEGALIZE_HPWL`` gives the legalizer a
wirelength term and the choice of all four turns, and ``PNR_LEGALIZE_REORIENT`` turns parts in
place after legalization (:mod:`pnr.place.reorient`).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from pnr import legalize_flags
from pnr.constraints import CompiledConstraints
from pnr.graph import BoardGraph, BoardOutline

from . import compact, metrics
from .geometry import (
    apply_hard_sides,
    hard_group_limits,
    keepout_rects,
    outline_size,
    resolve_fixed_poses,
    set_component_side,
)
from .legalize import legalize, legalize_constraint_kwargs, pad_edge_rule
from .model import global_place


@dataclass
class PlacementReport:
    """Outcome of a placement run — the numbers the acceptance test checks."""

    width: float
    height: float
    hpwl_baseline: float
    hpwl_placed: float
    overlaps: List[Tuple[str, str]] = field(default_factory=list)
    outside_outline: List[str] = field(default_factory=list)
    fixed_misplaced: List[str] = field(default_factory=list)
    keepout: List[str] = field(default_factory=list)
    group_outside: List[str] = field(default_factory=list)
    rotated: int = 0
    side_misplaced: List[str] = field(default_factory=list)
    # Hard region / align breaches (pnr.place.regions); always empty without them.
    region_outside: List[str] = field(default_factory=list)
    align_off: List[str] = field(default_factory=list)

    @property
    def legal(self) -> bool:
        return not (
            self.overlaps
            or self.outside_outline
            or self.fixed_misplaced
            or self.keepout
            or self.group_outside
            or self.side_misplaced
            or self.region_outside
            or self.align_off
        )

    @property
    def hpwl_improvement(self) -> float:
        if self.hpwl_baseline <= 0:
            return 0.0
        return 1.0 - self.hpwl_placed / self.hpwl_baseline

    def summary(self) -> str:
        return (
            f"placement {self.width:.0f}x{self.height:.0f} mm: "
            f"HPWL {self.hpwl_baseline:.0f} -> {self.hpwl_placed:.0f} mm "
            f"({self.hpwl_improvement * 100:.0f}% shorter); "
            f"rotated={self.rotated}; "
            f"legal={self.legal} "
            f"(overlaps={len(self.overlaps)}, "
            f"outside={len(self.outside_outline)}, "
            f"fixed_off={len(self.fixed_misplaced)}, "
            f"keepout={len(self.keepout)}, group_outside={len(self.group_outside)}, side_off={len(self.side_misplaced)}"
            + (
                f", region_outside={len(self.region_outside)}, align_off={len(self.align_off)}"
                if self.region_outside or self.align_off
                else ""
            )
            + ")"
        )


# Cap on the legalizer's per-part footprint inflation (the global spread does the
# board-filling; legalization just needs channel slack that still fits the outline).
_LEGALIZE_SPREAD_CAP = 1.3


def place(
    graph: BoardGraph,
    constraints: CompiledConstraints,
    *,
    seed: int = 0,
    iters: int = 800,
    grid_mm: float = 0.25,
    orient: bool = True,
    inflation: Optional[Dict[str, float]] = None,
    spread: float = 1.0,
    channel_rules: Optional[dict] = None,
    initial_positions: Optional[Dict[str, Tuple[float, float]]] = None,
    initial_rotations: Optional[Dict[str, float]] = None,
    pair_weights: Optional[Dict[Tuple[str, str, str, str], float]] = None,
    initial_sides: Optional[Dict[str, str]] = None,
) -> Tuple[BoardGraph, PlacementReport]:
    """Place ``graph`` under ``constraints``; return the placed graph + report.

    ``hpwl_baseline`` is the wirelength of the incoming (atopile row) placement,
    so the report shows the improvement. With ``orient`` the placer also picks a
    90° rotation per movable part (Phase 3). ``inflation`` (ref → spreading
    multiplier) is the routing-feedback hook (§6): the place↔route loop grows the
    footprint of congested parts so the next round spreads them.
    ``pair_weights`` ({(ref_a, pad_a, ref_b, pad_b): w}) adds a pad-pair
    attraction ``w * |pad_a - pad_b|`` to the global objective (pnr.feedback:
    connections that often fail routing); None leaves the objective untouched.
    Deterministic under a fixed ``seed`` on one platform (OS, architecture and
    torch build); another platform gives a different placement of the same
    quality on average (pnr.place.model).

    Sides (:mod:`pnr.place.sides`): on a double-sided board (``board.sides: double``)
    a held part is put on its one side first (an ``edge_align`` side), global
    placement relaxes the side of every free part too (starting toward
    ``initial_sides``, {ref: side}), the legalizer may take the slot on its other side,
    and a detail pass (:mod:`pnr.place.detail_moves`) tries flips and pairwise swaps; a
    held part never changes side (checked), and two parts that fan out never stack back
    to back (:func:`pnr.place.sides.stack_refs`). With nothing free the flow is the
    single-sided one, as it is under power-first placement (``PNR_POWER_FIRST=1``),
    which keeps every side.

    PNR_COMPACT=1 (:mod:`pnr.place.compact`): the spread floor is 1.0 and the global
    starts are drawn in a cluster box (``GP``), the legalizer keeps the courtyard gap,
    copper margins and a finer grid (``LEGALIZE``), and offset courtyards hold their body
    box (``COURTYARD``). Under power-first placement every part but ``WIRE`` and ``TURN``
    applies (:func:`pnr.place.power_first.staged_place`).
    """
    spread = compact.spread(spread)  # PNR_COMPACT GP: 1.0 (unchanged otherwise)
    if any(c.kind == "line_group" for c in constraints.constraints):
        # Line groups (pnr.place.line_group): each group is one rigid macro here.
        return _place_line_groups(
            graph,
            constraints,
            seed=seed,
            iters=iters,
            grid_mm=grid_mm,
            orient=orient,
            inflation=inflation,
            spread=spread,
            channel_rules=channel_rules,
            initial_positions=initial_positions,
            initial_rotations=initial_rotations,
            pair_weights=pair_weights,
            initial_sides=initial_sides,
        )
    # Edge rows are optimized across complete global starts. These temporary
    # search choices are distinct from authored absolute locks.
    # PNR_PAD_EDGE_CLEARANCE=1: pads/drills keep the fab edge rules (None = off).
    pad_edge = pad_edge_rule(constraints, channel_rules)
    if any(c.kind == "row" and not c.params.get("trial_resolved") for c in constraints.constraints):
        from .rows import sample_constraints

        constraints = sample_constraints(
            graph, constraints, seed, **({} if pad_edge is None else dict(pad_edge=pad_edge))
        )
    # The source compiler emits every footprint on top. Apply physical side
    # constraints before any obstacle/HPWL calculations, including pad mirroring.
    graph = BoardGraph.from_json(graph.to_json())
    apply_hard_sides(graph, constraints)
    from .pair_landing import enabled as landing_enabled

    if channel_rules and landing_enabled():
        # PNR_PAIR_LANDING_RESERVE=1: diff-pair via landings become placement
        # reservations (pnr.place.pair_landing); legalize and the report honour them.
        from .pair_landing import attach

        attach(graph, channel_rules)
    if channel_rules and channel_rules.get("plane_access_intents"):
        from pnr.plane_intent import reserve_array_space

        reserve_array_space(
            graph,
            channel_rules["plane_access_intents"],
            channel_rules["plane_access_fab"],
            channel_rules.get("fab", {}).get("edge_clearance_mm", 0.2),
        )
    width, height = outline_size(graph, constraints)
    baseline = metrics.hpwl(graph)
    from .regions import check_feasible, declared

    related = declared(constraints)
    if related:
        # Region / align: refuse an impossible one by name before placing.
        check_feasible(graph, constraints, width, height, orient=orient)

    poses = resolve_fixed_poses(graph, constraints)
    keepouts = keepout_rects(graph, constraints, poses)
    clearance = float(constraints.board.default_clearance_mm)
    from pnr.constraints import compile_routing_rules

    # PNR_COMPACT LEGALIZE: courtyard gap, slot grid and copper margins (None: unchanged).
    tight = None
    if compact.enabled("LEGALIZE"):
        tight = compact.legalize_settings(
            graph,
            constraints,
            channel_rules or compile_routing_rules(constraints, [n.name for n in graph.nets]),
        )
        clearance, grid_mm = tight.gap, tight.grid_mm
    from .sides import apply_held
    from .sides import plan as side_plan_of
    from .sides import stack_refs

    side_plan = side_plan_of(graph, constraints, channel_rules)
    # A held part's one side (an edge_align side under board.sides: double).
    apply_held(graph, side_plan)
    # On a double-sided board parts that fan out may not stack back to back.
    stack = stack_refs(graph, constraints)

    roles = None
    if os.environ.get("PNR_POWER_FIRST") == "1":
        if related:
            raise ValueError("region and align constraints do not support PNR_POWER_FIRST=1")
        # Power-first placement: derive tiers/loops, staged lexicographic global
        # placement, then power-first legalization (pnr.place.power_first).
        from .power_first import roles_for, staged_place

        roles = roles_for(graph, constraints, channel_rules)
    if roles is not None:
        placed = staged_place(
            graph,
            constraints,
            roles,
            width,
            height,
            seed=seed,
            iters=iters,
            orient=orient,
            inflation=inflation,
            spread=spread,
            channel_rules=channel_rules,
            initial_positions=initial_positions,
            initial_rotations=initial_rotations,
            poses=poses,
            keepouts=keepouts,
            clearance=clearance,
            grid_mm=grid_mm,
            legalize_spread=min(spread, _LEGALIZE_SPREAD_CAP),
            pair_weights=pair_weights,
            # PNR_COMPACT LEGALIZE margins and GP cluster box (pnr.place.power_first).
            **({} if not (tight and tight.margins) else dict(margins=tight.margins)),
            **(
                dict(start_box=compact.cluster_box(graph, constraints, width, height))
                if compact.enabled("GP")
                else {}
            ),
            mobility={
                ref: dict(
                    source_fixed=not bool(c.params.get("row_trial")),
                    row_trial=c.params.get("row_trial"),
                )
                for c in constraints.constraints
                if c.kind == "fixed"
                for ref in c.refs
            },
            **({} if pad_edge is None else dict(pad_edge=pad_edge)),
        )
        return _finish(placed, graph, constraints, width, height, baseline, pad_edge)

    # 1. Global placement (continuous position + orientation, and side when free).
    sided = side_plan.active
    polish = _polish(
        graph,
        constraints,
        channel_rules,
        clearance=clearance,
        grid_mm=grid_mm,
        spread=spread,
        inflation=inflation,
        pad_edge=pad_edge,
        margins=tight.margins if tight else None,
    )
    placement = global_place(
        graph,
        constraints,
        width,
        height,
        seed=seed,
        iters=iters,
        orient=orient,
        inflation=inflation,
        spread=spread,
        initial_positions=initial_positions,
        initial_rotations=initial_rotations,
        pair_weights=pair_weights,
        **(
            dict(side_plan=side_plan, initial_sides=initial_sides, return_sides=True)
            if sided
            else {}
        ),
        # PNR_COMPACT GP: the random starts are drawn in the cluster box.
        **(
            dict(start_box=compact.cluster_box(graph, constraints, width, height))
            if compact.enabled("GP")
            else {}
        ),
        # PNR_GP_POLISH: a final phase on the legalizer's slots (pnr.place.gp_polish).
        **({} if polish is None else dict(polish=polish)),
    )
    positions, rotations = placement[:2]
    cont = BoardGraph.from_json(graph.to_json())
    for comp in cont.components:
        comp.pos = positions[comp.ref]
        comp.rot = rotations[comp.ref]
    if sided:
        from .sides import assign

        assign(cont, placement[2], side_plan)

    # Directional copper escape demand is part of production legalization.
    from .channels import ChannelModel

    routing_rules = channel_rules or compile_routing_rules(
        constraints, [n.name for n in graph.nets]
    )
    # PNR_LEGALIZE_CHANNEL_CLEARANCE=fab: unclassed nets at the fab clearance.
    channels = ChannelModel(cont, routing_rules, **_channel_kwargs(routing_rules))
    # 2. Legalization (snap to a non-overlapping, in-outline layout).
    placed = legalize(
        cont,
        width,
        height,
        allow_rotation=orient,
        channel_model=channels,
        mobility={
            ref: dict(
                source_fixed=not bool(c.params.get("row_trial")),
                row_trial=c.params.get("row_trial"),
            )
            for c in constraints.constraints
            if c.kind == "fixed"
            for ref in c.refs
        },
        clearance=clearance,
        grid_mm=grid_mm,
        inflation=inflation,
        # The global spread already distributes parts across the board; the
        # legalizer only needs enough per-part slack to keep routing channels — a
        # full spread here would over-reserve and fail to fit on a tight outline
        # (grow the outline via the rubber-band instead).
        spread=min(spread, _LEGALIZE_SPREAD_CAP),
        # Fixed poses, keep-outs, hard groups and rotations, plus the optional
        # relations (pad-edge rule, hard edge_align, region, align) only when declared.
        **legalize_constraint_kwargs(graph, constraints, poses, pad_edge),
        **(_side_legalization(side_plan) if sided else {}),
        **({} if not stack else dict(stack=stack)),
        **({} if not (tight and tight.margins) else dict(margins=tight.margins)),
        # PNR_LEGALIZE_HPWL: the wirelength term and the four-turn search (not for matched parts).
        **_wire_kwargs(cont, constraints),
    )
    if related:
        # Hard aligns onto one exact line where that is legal (pnr.place.regions).
        from .regions import snap_aligns

        snap_aligns(
            placed,
            constraints,
            clearance,
            pad_edge,
            (width, height),
            **compact.margin_kwargs(tight),
        )
    if (constraints.diff_pairs or constraints.length_matches) and (
        (getattr(constraints, "tuning", None) or {}).get("placement", True)
    ):
        # Even the legs of each pair / group the packer pulled apart (pnr.place.matched).
        from .matched import refine_matched

        placed = refine_matched(
            placed,
            constraints,
            width,
            height,
            fixed=poses,
            keepouts=keepouts,
            group_limits=hard_group_limits(constraints, poses, partial=True),
            clearance=clearance,
            grid_mm=grid_mm,
            spread=min(spread, _LEGALIZE_SPREAD_CAP),
            inflation=inflation,
            outline=_legal_outline(constraints),
            pad_edge=pad_edge,
            **({} if not (tight and tight.margins) else dict(margins=tight.margins)),
        )
    reorienting = legalize_flags.legalize_reorient()
    if reorienting and orient:
        # PNR_LEGALIZE_REORIENT: greedy in-place turns that shorten wires (pnr.place.reorient);
        # ``wire`` drops the channel guard.
        from .reorient import reorient

        placed, _turned = reorient(
            placed,
            constraints,
            clearance=clearance,
            spread=min(spread, _LEGALIZE_SPREAD_CAP),
            inflation=inflation,
            pad_edge=pad_edge,
            channel_model=channels,
            channel_guard=reorienting != "wire",
            **({} if not (tight and tight.margins) else dict(margins=tight.margins)),
        )
    if sided:
        from .detail_moves import improve
        from .sides import check_held

        placed = improve(
            placed,
            constraints,
            side_plan,
            seed=seed,
            spread=min(spread, _LEGALIZE_SPREAD_CAP),
            inflation=inflation,
            allow_rotation=orient,
            **({} if pad_edge is None else dict(pad_edge=pad_edge)),
            **({} if not (tight and tight.margins) else dict(margins=tight.margins)),
        )
        check_held(placed, side_plan)
    return _finish(placed, graph, constraints, width, height, baseline, pad_edge)


def _channel_kwargs(rules) -> dict:
    """``ChannelModel`` keywords of ``PNR_LEGALIZE_CHANNEL_CLEARANCE`` (empty when off)."""
    if legalize_flags.legalize_channel_clearance() != "fab":
        return {}
    fab = rules.get("fab") or {}
    if fab.get("clearance_mm") is None:
        return {}
    return dict(clearance=float(fab["clearance_mm"]))


def _wire_kwargs(graph, constraints) -> dict:
    """``legalize`` keywords of ``PNR_LEGALIZE_HPWL`` (empty when off): its weight, and the
    parts on a diff-pair or length-match net, which keep the plain slot search
    (:func:`pnr.place.reorient.matched_refs`)."""
    weight = legalize_flags.legalize_hpwl()
    if weight is None:
        return {}
    from .reorient import matched_refs

    return dict(wire_weight=weight, wire_exempt=frozenset(matched_refs(graph, constraints)))


def _polish(graph, constraints, channel_rules, **legal):
    """The global-placement polish of ``PNR_GP_POLISH`` / ``PNR_GP_CHANNELS``
    (:class:`pnr.place.gp_polish.Polish`) with the legalizer's ``clearance``, ``grid_mm``,
    ``inflation``, ``pad_edge`` rule, copper ``margins`` and spreading floor (``spread``
    capped as the legalizer's), and with ``PNR_GP_CHANNELS`` its weight and the routing rules;
    None when off."""
    if not legalize_flags.gp_polish():
        return None
    from pnr.constraints import compile_routing_rules

    from .gp_polish import Polish

    weight = legalize_flags.gp_channels()
    rules = None
    if weight is not None:
        rules = channel_rules or compile_routing_rules(constraints, [n.name for n in graph.nets])
    legal["spread"] = min(legal["spread"], _LEGALIZE_SPREAD_CAP)
    return Polish(channel_weight=weight, rules=rules, **legal)


def _legal_outline(constraints):
    """``legalize: {outline: ...}`` of the constraints (None when undeclared)."""
    from .legal_options import options

    return options(constraints).get("outline")


def _side_legalization(side_plan):
    """Legalizer arguments for a board with side-free parts (pnr.place.sides)."""
    from .sides import side_cost

    return dict(
        side_options={r: o for r, o in side_plan.options.items() if len(o) > 1},
        side_cost=lambda board: side_cost(board, side_plan),
    )


def _place_line_groups(
    graph,
    constraints,
    *,
    seed,
    iters,
    grid_mm,
    orient,
    inflation,
    spread,
    channel_rules,
    initial_positions,
    initial_rotations,
    pair_weights,
    initial_sides=None,
):
    """:func:`place` for a design with line groups: collapse each group into a rigid
    macro (:func:`pnr.place.line_group.collapse`), place the macro graph as usual (rows,
    sides, global placement, legalization), expand the members rigidly and report the
    flat board against the original constraints."""
    from pnr import trace as _trace
    from pnr.hier.macro import macro_pair_weights

    from . import line_group

    if os.environ.get("PNR_POWER_FIRST") == "1":
        raise ValueError("line groups do not support PNR_POWER_FIRST=1")
    pad_edge = pad_edge_rule(constraints, channel_rules)
    flat = BoardGraph.from_json(graph.to_json())
    apply_hard_sides(flat, constraints)
    width, height = outline_size(flat, constraints)
    baseline = metrics.hpwl(flat)
    mgraph, mcon, mrules, plan = line_group.collapse(graph, constraints, channel_rules)
    positions, rotations = line_group.map_starts(plan, initial_positions, initial_rotations)
    # Tracing only: snapshots and the legalization order name the members, not LG00.
    with _trace.pose_expansion(plan.trace_rows):
        placed, _ = place(
            mgraph,
            mcon,
            seed=seed,
            iters=iters,
            grid_mm=grid_mm,
            orient=orient,
            inflation=line_group.map_inflation(plan, inflation),
            spread=spread,
            channel_rules=None if channel_rules is None else mrules,
            initial_positions=positions,
            initial_rotations=rotations,
            pair_weights=macro_pair_weights(pair_weights, plan),
            initial_sides=(
                None
                if initial_sides is None
                else {r: v for r, v in initial_sides.items() if r not in plan.member_of}
            ),
        )
    return _finish(plan.expand(placed, flat), graph, constraints, width, height, baseline, pad_edge)


def _finish(placed, graph, constraints, width, height, baseline, pad_edge=None):
    # Stamp the *placement region* as the placed board's outline, so downstream
    # steps (writeback framing, route SVG) use the constraint-resolved region
    # rather than the incoming atopile-framed one.
    placed.outline = BoardOutline(width=width, height=height)

    # 3. Score (hard checks at zero tolerance — strict no-overlap / in-outline).
    v = metrics.hard_violations(placed, constraints, clearance=0.0)
    if pad_edge is not None:
        # PNR_PAD_EDGE_CLEARANCE=1: copper/drills of a movable part too close to
        # the outline count as outside it (the legalizer never produces this).
        exempt = set(constraints.locked_refs) | set(resolve_fixed_poses(placed, constraints))
        bad = metrics.pad_edge_violations(placed, width, height, pad_edge, exclude=exempt)
        if bad:
            v["outside_outline"] = sorted(set(v["outside_outline"]) | set(bad))
    report = PlacementReport(
        width=width,
        height=height,
        hpwl_baseline=baseline,
        hpwl_placed=metrics.hpwl(placed),
        overlaps=v["overlaps"],
        outside_outline=v["outside_outline"],
        fixed_misplaced=v["fixed_misplaced"],
        side_misplaced=v["side_misplaced"],
        keepout=v["keepout"],
        group_outside=v["group_outside"],
        region_outside=v.get("region_outside", []),
        align_off=v.get("align_off", []),
        rotated=sum(1 for c in placed.components if int(round(c.rot)) % 360 != 0),
    )
    return placed, report
