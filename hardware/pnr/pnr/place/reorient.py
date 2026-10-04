"""In-place quarter turns after legalization (``PNR_LEGALIZE_REORIENT=1``, default off).

Design: docs/design/compact-placement.md section 11 (``C2``). The legalizer keeps the global
placer's turn (or, with ``PNR_LEGALIZE_HPWL``, chooses it with the slot); once every part is
placed, a turn that was right for the global target can be wrong at the legal pose. This pass
turns parts about their slot centre (the centre of the occupied box, which is where the
legalizer centred the slot) where that shortens their wirelength:

- greedy, best gain first: each round evaluates every candidate part at its three other turns
  and takes the single turn with the largest wirelength gain, ties broken by the reference and
  then the turn; to a fixed point, at most four turns per candidate in all;
- a turn is taken only if the part's half-perimeter wirelength (plane nets left out, as the
  legalizer's term, :func:`pnr.place.legalize.wire_cost`) drops by more than :data:`GAIN_MM`,
  it is legal in place (:func:`pnr.place.metrics.pose_checker` with the legalizer's clearance,
  spreading factor or inflation, copper margins and pad-edge rule: hard rotation and side,
  outline, keep-outs, regions, aligns, edge bands, group radii and every other part), and its
  routing-channel penalty against all other parts (:class:`pnr.place.channels.ChannelModel`)
  does not rise (the channel guard; ``channel_guard=False``, ``PNR_LEGALIZE_REORIENT=wire``,
  drops it: wirelength and legality only);
- never turned: fixed and locked parts, hard rotations, row and line-group members, macros
  (``block:``, ``line:``) and the parts on a differential pair or length-match net (the
  matched-length pass evened them).

Ties keep the turn: a two-pad part whose nets both leave on one side has the same wirelength in
every correctly polarised turn, so this pass alone does not make such a part "flush" with a row;
the legalizer's wirelength term (``PNR_LEGALIZE_HPWL``) does, by choosing the slot and turn
together. Deterministic: no random draws, fixed orders, float64 arithmetic.
"""

from __future__ import annotations

from typing import Dict, Optional, Set, Tuple

from pnr.graph import BoardGraph

from .geometry import body_shift, courtyard_rect, resolve_fixed_poses, resolve_hard_rotations
from .legalize import plane_nets, wire_cost

# A turn must shorten the part's wirelength by more than this (mm).
GAIN_MM = 1e-6
# A turn may not raise the part's channel penalty by more than this (mm^2).
PENALTY_EPS = 1e-9


def frozen_refs(graph, constraints, poses=None) -> Set[str]:
    """The parts the pass never turns (see the module docstring)."""
    out = set(resolve_fixed_poses(graph, constraints) if poses is None else poses)
    out |= set(constraints.locked_refs) | set(resolve_hard_rotations(constraints))
    for con in constraints.constraints:
        if con.kind in ("fixed", "row", "line_group"):
            out |= set(con.refs)
    matched = set()
    for pair in getattr(constraints, "diff_pairs", None) or []:
        matched |= {pair.p, pair.n}
    for group in getattr(constraints, "length_matches", None) or []:
        matched |= set(group.nets)
    for comp in graph.components:
        if comp.locked or str(comp.footprint).startswith(("block:", "line:")):
            out.add(comp.ref)
        elif matched and any(pad.net in matched for pad in comp.pads):
            out.add(comp.ref)
    return out


def _set_turn(comp, centre, rot):
    """Turn ``comp`` to ``rot`` keeping its occupied box centred on ``centre``."""
    comp.rot = rot
    shift = body_shift(comp)
    comp.pos = centre if shift is None else (centre[0] - shift[0], centre[1] - shift[1])


def reorient(
    graph: BoardGraph,
    constraints,
    *,
    clearance: float,
    spread: float = 1.0,
    inflation: Optional[Dict[str, float]] = None,
    margins: Optional[Dict[str, float]] = None,
    pad_edge: Optional[Tuple[float, float]] = None,
    channel_model=None,
    channel_guard: bool = True,
) -> Tuple[BoardGraph, Dict[str, float]]:
    """``(board, {ref: new turn})``: a copy of the legal ``graph`` with the greedy in-place
    turns applied (the module docstring). ``clearance``, ``spread``, ``inflation``, ``margins``
    and ``pad_edge`` are the legalizer's; ``channel_model`` supplies the channel penalty (with
    ``channel_guard``) and the plane nets (None: neither). A board that is not legal to begin
    with is returned unchanged (the pose checker needs a legal baseline)."""
    from .metrics import pose_checker

    out = BoardGraph.from_json(graph.to_json())
    try:
        legal = pose_checker(
            out,
            constraints,
            clearance=clearance,
            spread=spread,
            pad_edge=pad_edge,
            inflation=inflation,
            margins=margins or None,
        )
    except ValueError:
        return out, {}
    skip = plane_nets(channel_model)
    frozen = frozen_refs(out, constraints)
    by_ref = {c.ref: c for c in out.components}
    candidates = sorted(
        c.ref
        for c in out.components
        if c.ref not in frozen and any(p.net and p.net not in skip for p in c.pads)
    )
    others = {ref: [c for c in out.components if c.ref != ref] for ref in candidates}

    def wire(comp):
        return float(wire_cost(comp, others[comp.ref], (), comp.pos[0], comp.pos[1], skip))

    guarded = channel_guard and channel_model is not None

    def penalty(comp):
        if not guarded:
            return 0.0
        return float(channel_model.penalty(comp, others[comp.ref], comp.pos[0], comp.pos[1]))

    turned = {}
    for _ in range(4 * len(candidates)):
        best = None  # (gain, ref, k, rot, pos)
        for ref in candidates:
            comp = by_ref[ref]
            pos0, rot0 = comp.pos, comp.rot
            rect = courtyard_rect(comp)
            centre = (rect.cx, rect.cy)
            base = wire(comp)
            before = None
            for k in (1, 2, 3):
                rot = (rot0 + 90.0 * k) % 360
                _set_turn(comp, centre, rot)
                gain = base - wire(comp)
                if gain <= GAIN_MM:
                    continue
                if best is not None and (-gain, ref, k) >= (-best[0], best[1], best[2]):
                    continue
                if not legal([comp]):
                    continue
                if guarded:
                    if before is None:
                        comp.pos, comp.rot = pos0, rot0
                        before = penalty(comp)
                        _set_turn(comp, centre, rot)
                    if penalty(comp) > before + PENALTY_EPS:
                        continue
                best = (gain, ref, k, rot, comp.pos)
            comp.pos, comp.rot = pos0, rot0
        if best is None:
            break
        _, ref, _, rot, pos = best
        comp = by_ref[ref]
        comp.rot, comp.pos = rot, pos
        legal.update([ref])
        turned[ref] = rot
    return out, turned
