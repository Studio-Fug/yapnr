"""Placement orchestrator: global placement → legalization → report.

PNR_PAIR_LANDING_RESERVE=1 (src13, default off): place() attaches the diff-pair
via landing reserves of pnr.place.pair_landing (from ``channel_rules``) before
legalization; legality/report then include them through placement_rects.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from pnr.constraints import CompiledConstraints
from pnr.graph import BoardGraph, BoardOutline

from . import metrics
from .geometry import (
    apply_hard_sides,
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

    @property
    def legal(self) -> bool:
        return not (
            self.overlaps
            or self.outside_outline
            or self.fixed_misplaced
            or self.keepout
            or self.group_outside
            or self.side_misplaced
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
            f"keepout={len(self.keepout)}, group_outside={len(self.group_outside)}, side_off={len(self.side_misplaced)})"
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
    """
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

    poses = resolve_fixed_poses(graph, constraints)
    keepouts = keepout_rects(graph, constraints, poses)
    clearance = float(constraints.board.default_clearance_mm)

    roles = None
    if os.environ.get("PNR_POWER_FIRST") == "1":
        from .regions import declared

        if declared(constraints):
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

    # 1. Global placement (continuous position + orientation).
    positions, rotations = global_place(
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
    )
    cont = BoardGraph.from_json(graph.to_json())
    for comp in cont.components:
        comp.pos = positions[comp.ref]
        comp.rot = rotations[comp.ref]

    # Directional copper escape demand is part of production legalization.
    from pnr.constraints import compile_routing_rules

    from .channels import ChannelModel

    channels = ChannelModel(
        cont, channel_rules or compile_routing_rules(constraints, [n.name for n in graph.nets])
    )
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
    )
    return _finish(placed, graph, constraints, width, height, baseline, pad_edge)


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
        rotated=sum(1 for c in placed.components if int(round(c.rot)) % 360 != 0),
    )
    return placed, report
