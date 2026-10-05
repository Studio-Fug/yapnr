"""Differentiable global placement (design doc §4/§8, orientation §9.3).

Relaxes a continuous placement by gradient descent on a smooth loss:

    L = WL(log-sum-exp HPWL)          # connected pins attract
      + w_spread * overlap            # courtyards repel (spreading)
      + w_bound  * outline_penalty    # stay inside the board
      + w_edge   * edge_align         # soft pull to a board edge
      + w_keep   * keepout_penalty    # keep movable parts out of keep-outs
      + w_group  * grouping           # cluster grouped parts near their anchor
      + w_match  * length_mismatch    # equal estimated lengths in pairs / groups
      + region / align                # allowed areas, shared coordinates (if declared)

Positions of ``fixed`` parts are held constant (they still anchor the wirelength);
everything else is an optimized parameter. This is the DREAMPlace reframing —
"placement is training a network" — in plain PyTorch on CPU, deterministic under a
fixed seed on one platform. It produces good *continuous* positions; :mod:`pnr.place.legalize`
removes the residual overlaps.

**Orientation** (``orient=True``): each movable part also carries a categorical
over the four 90° rotations, relaxed to a softmax whose temperature is annealed
toward one-hot (a deterministic Concrete/Gumbel-Softmax relaxation — Cypress §9.3).
Pin offsets and courtyard extents become the *expected* offset/extent under that
distribution, so orientation is differentiable and co-optimized with position; at
the end we snap to the arg-max angle. Fixed parts keep their constrained angle.

**Side** (a ``side_plan`` with free parts, :mod:`pnr.place.sides`): each free part
also carries a side logit, its probability ``q`` of the bottom side a sigmoid annealed
with the rotation temperature, initialised toward its start side. Pin offsets are
the expectation over both sides (the bottom mirrors them, as KiCad's Flip does) and
rotations; two parts repel with weight ``min(1, o_i . o_j)``, ``o = [1 - q, q]`` for a
surface part and ``[1, 1]`` for one that occupies both sides (the constant per-side
mask when nothing is free), and two parts that fan out with weight 1 whatever their
sides (no back-to-back stacking, :func:`pnr.place.sides.stack_refs`). Three terms
join the loss, in wirelength millimetres: the expected layer changes
``VIA_MM * sum P(net split)``, ``P = 1 - prod(1 - q) - prod(q)`` over each splittable
net's parts, the ``side_pref`` bias and a small cost per part off its source side.
Each free part snaps to the bottom when ``q > 0.5``.
Without free parts none of this runs and the result is unchanged.

**Compact placement** (PNR_COMPACT, :mod:`pnr.place.compact`, default off): ``start_box``
maps the seeded random starts into a cluster box instead of the whole board (``GP``);
the overlap and keep-out terms keep the courtyard gap instead of the routing clearance
(``LEGALIZE``); a part with an offset courtyard (``COURTYARD``) spreads, stays inside the
outline, aligns to an edge and avoids keep-outs with its body box, centred at ``pos``
plus the body offset expected under the rotation (and side) distribution, like the pin
offsets. Without the flag every term is computed exactly as before.
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional, Tuple

import torch

from pnr.constraints import CompiledConstraints
from pnr.graph import BoardGraph

from . import portable_math as pm
from .geometry import keepout_rects, occupied_sides, resolve_fixed_poses

# Reproducibility ("same inputs -> same board", design §10): run torch
# single-threaded so the float reductions don't vary with thread scheduling.
# Set at import, before any parallel work sizes the intra-op pool.
# Bitwise reproducible per platform (OS, architecture, torch build) only: the
# macOS and Linux torch wheels round exp, log and addcmul differently in the last
# bit, and the non-convex placement amplifies that (Studio-Fug/yapnr#6).
torch.set_num_threads(1)

# The discrete rotation set (degrees) the placer chooses from.
ANGLES = (0.0, 90.0, 180.0, 270.0)


PAIR_EPS2 = 0.01  # mm^2 inside the pad-pair distance sqrt (smooth at zero)


def pair_tensors(pair_weights, pin_key):
    """(a, b, w) index/weight tensors of the positive pad pairs present in
    ``pin_key`` ({(ref, pad): pin index}), or None when there are none."""
    if not pair_weights:
        return None
    rows = sorted(
        (pin_key[(ra, pa)], pin_key[(rb, pb)], float(w))
        for (ra, pa, rb, pb), w in pair_weights.items()
        if ra != rb and float(w) > 0 and (ra, pa) in pin_key and (rb, pb) in pin_key
    )
    if not rows:
        return None
    return (
        torch.tensor([r[0] for r in rows], dtype=torch.long),
        torch.tensor([r[1] for r in rows], dtype=torch.long),
        torch.tensor([r[2] for r in rows], dtype=torch.float32),
    )


def matched_pin_sets(graph: BoardGraph, constraints, pin_key) -> List[List[torch.Tensor]]:
    """Pin index tensors per member net of each declared diff pair and length-match
    group whose members all have two or more placed pins ([] when none, or with
    ``tuning: {placement: false}``)."""
    if not ((getattr(constraints, "tuning", None) or {}).get("placement", True)):
        return []
    pins_of = {
        net.name: [pin_key[p] for p in net.pins if p in pin_key]
        for net in getattr(graph, "nets", [])
    }
    sets = [(dp.p, dp.n) for dp in getattr(constraints, "diff_pairs", None) or []]
    sets += [tuple(lm.nets) for lm in getattr(constraints, "length_matches", None) or []]
    out = []
    for members in sets:
        members = list(dict.fromkeys(members))
        if len(members) >= 2 and all(len(pins_of.get(n, ())) >= 2 for n in members):
            out.append([torch.tensor(pins_of[n], dtype=torch.long) for n in members])
    return out


def length_mismatch(match_sets, pin_x, pin_y, gamma: float) -> torch.Tensor:
    """``sum |L_i - mean(L)|`` (smoothed) over each set's members, ``L`` the estimated
    routed length: the pin distance of a two-pin net, else the smooth HPWL."""
    total = pin_x.new_zeros(())
    for members in match_sets:
        lengths = []
        for pins in members:
            px, py = pin_x[pins], pin_y[pins]
            if len(pins) == 2:
                lengths.append(torch.sqrt((px[0] - px[1]) ** 2 + (py[0] - py[1]) ** 2 + PAIR_EPS2))
            else:
                lengths.append(
                    gamma
                    * (
                        pm.logsumexp(px / gamma, 0)
                        + pm.logsumexp(-px / gamma, 0)
                        + pm.logsumexp(py / gamma, 0)
                        + pm.logsumexp(-py / gamma, 0)
                    )
                )
        stacked = torch.stack(lengths)
        total = total + torch.sqrt((stacked - stacked.mean()) ** 2 + PAIR_EPS2).sum()
    return total


def _base_half_sizes(graph: BoardGraph) -> torch.Tensor:
    """Unrotated courtyard half-(w, h) per component (parts ingest at rot 0); the body
    box's half size for a PNR_COMPACT offset courtyard."""
    from .geometry import compact_body

    hs = []
    for c in graph.components:
        body = compact_body(c)
        if body is None:
            hs.append((c.courtyard[0] / 2.0, c.courtyard[1] / 2.0))
        else:
            hs.append(((body[2] - body[0]) / 2.0, (body[3] - body[1]) / 2.0))
    return torch.tensor(hs, dtype=torch.float32)


def _quarter_turns(x: float, y: float) -> List[Tuple[float, float]]:
    """``(x, y)`` at 0, 90, 180 and 270 degrees CCW (exact)."""
    return [(x, y), (-y, x), (-x, -y), (y, -x)]


def body_offsets(graph: BoardGraph):
    """PNR_COMPACT offset courtyards: ``(off4, mirror4)``, the (n, 4, 2) offsets of each
    body box's centre from the origin at the four rotations, on the part's side and on
    the other one (mirrored in y, as the pads); None when no part has one."""
    from .geometry import compact_body

    bodies = [compact_body(c) for c in graph.components]
    if all(b is None for b in bodies):
        return None
    rows, mirrored = [], []
    for body in bodies:
        if body is None:
            rows.append([(0.0, 0.0)] * 4)
            mirrored.append([(0.0, 0.0)] * 4)
            continue
        cx, cy = (body[0] + body[2]) / 2.0, (body[1] + body[3]) / 2.0
        rows.append(_quarter_turns(cx, cy))
        mirrored.append(_quarter_turns(cx, -cy))
    return (
        torch.tensor(rows, dtype=torch.float32),
        torch.tensor(mirrored, dtype=torch.float32),
    )


def global_place(
    graph: BoardGraph,
    constraints: CompiledConstraints,
    width: float,
    height: float,
    *,
    seed: int = 0,
    iters: int = 800,
    lr: float = 0.3,
    gamma: float = 1.0,
    orient: bool = True,
    inflation: Optional[Dict[str, float]] = None,
    spread: float = 1.0,
    w_spread: float = 1.0,
    w_bound: float = 20.0,
    w_keep: float = 40.0,
    w_group: float = 0.5,
    w_plane: float = 0.05,
    w_plane_sep: float = 0.35,
    w_match: float = 1.0,
    initial_positions: Optional[Dict[str, Tuple[float, float]]] = None,
    initial_rotations: Optional[Dict[str, float]] = None,
    pair_weights: Optional[Dict[Tuple[str, str, str, str], float]] = None,
    side_plan=None,
    initial_sides: Optional[Dict[str, str]] = None,
    return_sides: bool = False,
    start_box: Optional[Tuple[float, float, float, float]] = None,
    polish=None,
):
    """Optimize continuous centres (+ orientation); return positions and angles.

    ``inflation`` optionally maps a ref to a spreading multiplier > 1 (RePlAce
    cell inflation, §6): the part's courtyard is scaled up *only in the density/
    spreading term*, so a component the router found in a congested region is
    pushed into lower-density space on the next placement round. Wirelength and
    the reported courtyard are unaffected.

    ``pair_weights`` ({(ref_a, pad_a, ref_b, pad_b): w}) adds
    ``sum w * sqrt(dx^2 + dy^2 + PAIR_EPS2)`` over those pad pairs (expected
    rotated pin offsets) beside the wirelength term; None skips it entirely.

    Each declared differential pair and length-match group (``constraints.diff_pairs``,
    ``constraints.length_matches``) adds ``w_match * sum |L_i - mean(L)|`` over its
    members' estimated lengths (a two-pin net: the pin distance; more pins: the
    smooth HPWL). A pair whose legs run through series parts (connector -> R1/R2 ->
    MCU) is otherwise free to place R1 and R2 at different distances along the way,
    which no meander can make up; a design without pairs or groups is unchanged.

    ``side_plan`` (:func:`pnr.place.sides.plan`) relaxes the side of each of its free
    parts too, starting toward ``initial_sides`` ({ref: side}, default the part's
    current side); without free parts it changes nothing.

    ``start_box`` ((x0, y0, w, h), PNR_COMPACT ``GP``) maps the seeded random starts into
    that box (:func:`pnr.place.compact.box_coordinate`, the same draws); None keeps them
    spread across the board.

    ``polish`` (:class:`pnr.place.gp_polish.Polish`, ``PNR_GP_POLISH``; None = off, and
    ignored with hull macros) runs ``polish.steps`` more iterations of this loop with the
    turns and free sides frozen at their arg-max, the overlap, outline and keep-out terms on
    the legalizer's slots, the optional channel term, the overlap weight ramped and a fresh
    positions-only Adam with a decaying step (:mod:`pnr.place.gp_polish`).

    Returns ``({ref: (x, y)}, {ref: angle_deg})`` for every component (angle is
    the arg-max of the relaxed rotation distribution, a legal 0/90/180/270), and
    with ``return_sides`` a third map ``{ref: side}`` (free parts snapped at
    ``q > 0.5``, every other part its current side)."""
    torch.manual_seed(seed)
    comps = graph.components
    n = len(comps)
    idx = {c.ref: i for i, c in enumerate(comps)}

    side_overlap = torch.tensor(
        [[bool(set(occupied_sides(a)) & set(occupied_sides(b))) for b in comps] for a in comps],
        dtype=torch.float32,
    )
    half = _base_half_sizes(graph)  # (n, 2), unrotated
    # Inflate the *spreading* footprint (not WL, not the reported courtyard): a
    # per-part ``inflation`` floor (congested parts, from the loop) OR a global
    # ``spread`` floor applied to EVERY part. The latter is what makes parts fill
    # the whole board — with each courtyard reserving `spread`× its area in the
    # overlap term, they distribute to a lower target density with routing channels,
    # instead of clumping toward the wirelength optimum and leaving the board empty.
    if inflation or spread > 1.0:
        scale = torch.tensor(
            [[max(1.0, spread, float((inflation or {}).get(c.ref, 1.0)))] for c in comps],
            dtype=torch.float32,
        )
        half = half * scale
    # Courtyard half-size per candidate angle: swap w/h at 90/270.
    swapped = half[:, [1, 0]]
    half4 = torch.stack([half, swapped, half, swapped], dim=1)  # (n, 4, 2)
    # Block macros with per-side hulls (PNR_MACRO_HULL=1): overlap over per-side
    # bodies instead of whole courtyards; None (the unchanged path) otherwise.
    from .hull import gp_bodies, gp_overlap

    bodies = gp_bodies(
        comps,
        (
            [max(1.0, spread, float((inflation or {}).get(c.ref, 1.0))) for c in comps]
            if (inflation or spread > 1.0)
            else None
        ),
    )

    poses = resolve_fixed_poses(graph, constraints)
    # Side relaxation only with free parts (and not over hull bodies, whose macros are
    # held anyway); otherwise every tensor below is the single-sided one.
    free_side = (
        [idx[r] for r in side_plan.free if r in idx and r not in poses]
        if side_plan is not None and bodies is None
        else []
    )
    for ref, side in (initial_sides or {}).items():
        if ref not in idx or side not in ("top", "bottom"):
            raise ValueError("invalid initial side %r for %s" % (side, ref))
    # A start may only propose a side the plan allows; any other start side is moot.
    is_fixed = torch.zeros(n, dtype=torch.bool)
    fixed_xy = torch.zeros(n, 2, dtype=torch.float32)
    fixed_angle_idx = torch.zeros(n, dtype=torch.long)
    fixed_rot = {}
    for con in constraints.constraints:
        if con.kind == "fixed":
            for ref in con.refs:
                fixed_rot[ref] = con.params.get("rot") or 0.0
    for ref, (px, py) in poses.items():
        if ref in idx:
            is_fixed[idx[ref]] = True
            fixed_xy[idx[ref]] = torch.tensor([px, py])
            fixed_angle_idx[idx[ref]] = int(round(fixed_rot.get(ref, 0.0) / 90.0)) % 4

    from .geometry import resolve_hard_rotations

    rotation_fixed = is_fixed.clone()
    for ref, angle in resolve_hard_rotations(constraints).items():
        if ref in idx:
            rotation_fixed[idx[ref]] = True
            fixed_angle_idx[idx[ref]] = int(round(angle / 90)) % 4

    # Init movable positions spread across the interior (seeded, deterministic).
    init = torch.rand(n, 2)
    if start_box is None:
        init[:, 0] = half[:, 0] + init[:, 0] * (width - 2 * half[:, 0])
        init[:, 1] = half[:, 1] + init[:, 1] * (height - 2 * half[:, 1])
    else:
        # PNR_COMPACT GP: the same draws, mapped into the cluster box.
        from .compact import box_coordinate

        x0, y0, bw, bh = (float(v) for v in start_box)
        init = torch.tensor(
            [
                [box_coordinate(u, hx, x0, bw, width), box_coordinate(v, hy, y0, bh, height)]
                for (u, v), (hx, hy) in zip(init.tolist(), half.tolist())
            ],
            dtype=torch.float32,
        ).reshape(n, 2)
    # Explicit global starts let the initial pool explore different arrangements
    # instead of replacing every supplied source pose with the same random path.
    if initial_positions is not None:
        for ref, xy in initial_positions.items():
            if ref not in idx or len(xy) != 2:
                raise ValueError("invalid initial placement reference/coordinate")
            point = torch.tensor(xy, dtype=torch.float32)
            if not bool(torch.isfinite(point).all()):
                raise ValueError("initial placement contains non-finite coordinates")
            init[idx[ref]] = point
    move = torch.nn.Parameter(init.clone())

    params = [move]
    rot_logits = None
    if orient:
        logits = torch.zeros(n, 4)
        for ref, angle in (initial_rotations or {}).items():
            if ref not in idx or not torch.isfinite(torch.tensor(float(angle))):
                raise ValueError("invalid initial rotation")
            logits[idx[ref], int(round(angle / 90.0)) % 4] = 2.0
        rot_logits = torch.nn.Parameter(logits)
        params.append(rot_logits)
    fixed_onehot = torch.nn.functional.one_hot(fixed_angle_idx, 4).float()

    def full_pos() -> torch.Tensor:
        return torch.where(is_fixed.unsqueeze(1), fixed_xy, move)

    def rot_probs(temp: float) -> torch.Tensor:
        """(n, 4) rotation distribution; fixed parts pinned one-hot."""
        if rot_logits is None:
            raw = torch.zeros(n, 4)
            raw[:, 0] = 1.0
        else:
            raw = pm.softmax(rot_logits / temp, dim=1)
        return torch.where(rotation_fixed.unsqueeze(1), fixed_onehot, raw)

    # Pins: component index + the four rotated offsets (rot 0/90/180/270).
    pin_comp: List[int] = []
    pin_off4: List[List[Tuple[float, float]]] = []
    pin_key: Dict[Tuple[str, str], int] = {}
    for c in comps:
        for pad in c.pads:
            ox, oy = pad.offset
            pin_key[(c.ref, pad.name)] = len(pin_comp)
            pin_comp.append(idx[c.ref])
            variants = []
            for ang in ANGLES:
                ct, st = pm.quarter_turn(ang)
                variants.append((ox * ct - oy * st, ox * st + oy * ct))
            pin_off4.append(variants)
    pin_comp_t = torch.tensor(pin_comp, dtype=torch.long)
    pin_off4_t = torch.tensor(pin_off4, dtype=torch.float32)  # (P, 4, 2)
    sided = None
    if free_side:
        sided = _side_terms(
            graph, constraints, comps, idx, side_plan, free_side, initial_sides, pin_off4_t
        )
        params.append(sided["logits"])
    net_pin_idx = [[pin_key[p] for p in net.pins if p in pin_key] for net in graph.nets]
    net_pin_idx = [pins for pins in net_pin_idx if len(pins) >= 2]
    pairs = pair_tensors(pair_weights, pin_key)
    match_sets = matched_pin_sets(graph, constraints, pin_key)
    batched_wl = None
    if os.environ.get("PNR_BATCHED_WIRELENGTH") == "1":
        from .batched_cost import BucketedWirelength

        batched_wl = BucketedWirelength(net_pin_idx)

    # Plane nets (power/ground poured as copper planes): the pins on each, used to
    # (a) minimise each plane's pad-bounding-box AREA and (b) keep different power
    # domains from overlapping — so the split planes end up compact and disjoint,
    # not sprawling stepped rectangles. A component with pads on two domains is a
    # soft compromise between them.
    import fnmatch as _fnmatch

    from pnr.stack import split_plane_patterns

    plane_patterns = split_plane_patterns(constraints, graph)
    plane_pin_idx: List[List[int]] = []
    for net in graph.nets:
        if any(_fnmatch.fnmatch(net.name, pat) for pat in plane_patterns):
            pins = [pin_key[p] for p in net.pins if p in pin_key]
            if len(pins) >= 2:
                plane_pin_idx.append(pins)

    # Edge-align targets (soft): (comp_idx, axis, edge, weight).
    edge_terms: List[Tuple[int, int, str, float]] = []
    for con in constraints.constraints:
        if con.kind != "edge_align":
            continue
        edge = con.params.get("edge")
        for ref in con.refs:
            if ref in idx:
                axis = 1 if edge in ("south", "north") else 0
                edge_terms.append((idx[ref], axis, edge, con.weight or 1.0))

    # Grouping (soft): pull members within radius of the anchor.
    group_terms: List[Tuple[List[int], int, float, float]] = []
    for con in constraints.constraints:
        if con.kind != "group":
            continue
        anchor = con.params.get("anchor")
        if anchor not in idx:
            continue
        members = [idx[r] for r in con.refs if r in idx and r != anchor]
        if members:
            group_terms.append(
                (members, idx[anchor], float(con.params.get("radius_mm") or 5.0), con.weight or 1.0)
            )

    # Regions and aligns (pnr.place.regions): None unless the design declares one,
    # so other designs build no extra tensors and keep their loss bit for bit.
    related = None
    from .regions import declared

    if declared(constraints):
        from .regions import GlobalTerms

        related = GlobalTerms(
            constraints, comps, idx, ANGLES, (~is_fixed).tolist(), flippable=free_side
        )

    keepouts = keepout_rects(graph, constraints, poses)
    keep_t = (
        torch.tensor([[k.cx, k.cy, k.w / 2, k.h / 2] for k in keepouts], dtype=torch.float32)
        if keepouts
        else None
    )

    movable_f = (~is_fixed).float()
    from .compact import placement_clearance

    # The board's default clearance; the courtyard gap with PNR_COMPACT LEGALIZE.
    clearance = placement_clearance(constraints)
    # PNR_COMPACT COURTYARD: body-centre offsets per rotation (None: every part centred).
    body_off = body_offsets(graph)
    opt = pm.Adam(params, lr=lr)
    from pnr.trace import placement_tracer

    # PNR_GP_POLISH (pnr.place.gp_polish): extra iterations after the main ones (none with
    # the switch off, or with hull macros, whose bodies the slot model does not know).
    extra = 0 if polish is None or bodies is not None else polish.steps
    if extra:
        from .gp_polish import channel_shortage, slot_bound, slot_keepout, slot_overlap
    # None unless PNR_TRACE_DIR is set (pnr.trace); the polish iterations are traced too.
    tracer = placement_tracer(comps, iters + extra)
    frozen = None

    for step in range(iters + extra):
        if step >= iters:
            # Polish: turns and sides frozen, the legalizer's slots, a decaying step.
            if frozen is None:
                frozen = _freeze(
                    graph, polish, rot_probs(0.2), sided, comps, width, height, side_overlap
                )
                opt = pm.Adam([move], lr=polish.lr(0.0))
            frac = (step - iters) / max(1, extra - 1)
            for group in opt.param_groups:
                group["lr"] = polish.lr(frac)
            opt.zero_grad()
            pos = full_pos()
            p = frozen["p"]
        else:
            temp = 2.0 - (2.0 - 0.2) * (step / max(1, iters - 1))  # anneal 2.0 -> 0.2
            opt.zero_grad()
            pos = full_pos()
            p = rot_probs(temp)  # (n, 4)

        # Expected pin offset under the rotation distribution.
        p_pin = p[pin_comp_t]  # (P, 4)
        exp_off = (p_pin.unsqueeze(-1) * pin_off4_t).sum(1)  # (P, 2)
        if sided is not None:
            # ... and under the side distribution: the far side mirrors the offsets.
            bottom = (
                _bottom_probability(sided, temp) if frozen is None else frozen["bottom"]
            )  # (n,)
            away = torch.where(sided["current_bottom"], 1.0 - bottom, bottom)
            mirrored = (p_pin.unsqueeze(-1) * sided["pin_off4_mirror"]).sum(1)
            away_pin = away[pin_comp_t].unsqueeze(-1)
            exp_off = (1.0 - away_pin) * exp_off + away_pin * mirrored
        pin_x = pos[pin_comp_t, 0] + exp_off[:, 0]
        pin_y = pos[pin_comp_t, 1] + exp_off[:, 1]

        if batched_wl is not None:
            wl = batched_wl(torch.stack((pin_x, pin_y), dim=-1), gamma)
        else:
            wl = pos.new_zeros(())
            for pins in net_pin_idx:
                px, py = pin_x[pins], pin_y[pins]
                # One portable logsumexp per net over the four signed coordinates.
                ext = pm.logsumexp(torch.stack((px, -px, py, -py)) / gamma, 1)
                wl = wl + gamma * (ext[0] + ext[1] + ext[2] + ext[3])

        # Expected courtyard half-size (rotation-aware).
        exp_half = (p.unsqueeze(-1) * half4).sum(1)  # (n, 2)
        hw, hh = exp_half[:, 0], exp_half[:, 1]
        # Courtyard centres: the origins, or (PNR_COMPACT offset courtyards) the origins
        # plus the expected body offsets, mixed over both sides for a side-free part.
        body = pos
        if body_off is not None:
            exp_body = (p.unsqueeze(-1) * body_off[0]).sum(1)
            if sided is not None:
                away_b = away.unsqueeze(-1)
                exp_body = (1.0 - away_b) * exp_body + away_b * (p.unsqueeze(-1) * body_off[1]).sum(
                    1
                )
            body = pos + exp_body

        # Pairwise smooth overlap (spreading), upper triangle only.
        if frozen is not None:
            # Polish: the legalizer's slots, the weight ramped (pnr.place.gp_polish).
            overlap = polish.ramp(frac) * slot_overlap(pos, frozen, frozen["mask"])
        elif bodies is None:
            dx = (body[:, 0].unsqueeze(1) - body[:, 0].unsqueeze(0)).abs()
            dy = (body[:, 1].unsqueeze(1) - body[:, 1].unsqueeze(0)).abs()
            sw = hw.unsqueeze(1) + hw.unsqueeze(0) + clearance
            sh = hh.unsqueeze(1) + hh.unsqueeze(0) + clearance
            ox = torch.clamp(sw - dx, min=0.0)
            oy = torch.clamp(sh - dy, min=0.0)
            mask = side_overlap if sided is None else _side_overlap(sided, bottom)
            overlap = torch.triu(ox * oy * mask, diagonal=1).sum()
        else:
            overlap = gp_overlap(bodies, pos, p, clearance)

        # Outline containment.
        cx, cy = body[:, 0], body[:, 1]
        bound = (
            torch.clamp(hw - cx, min=0.0) ** 2
            + torch.clamp(cx + hw - width, min=0.0) ** 2
            + torch.clamp(hh - cy, min=0.0) ** 2
            + torch.clamp(cy + hh - height, min=0.0) ** 2
        )
        bound = (bound * movable_f).sum()
        if frozen is not None:
            bound = slot_bound(pos, frozen, width, height, movable_f)

        loss = wl + w_spread * overlap + w_bound * bound
        if frozen is not None and frozen["need"] is not None:
            # PNR_GP_CHANNELS: the legalizer's channel cost, smooth (each pair from both ends).
            loss = loss + polish.channel_weight * 0.5 * channel_shortage(
                pos, frozen, frozen["mask"]
            )
        if sided is not None:
            loss = loss + _side_cost(sided, bottom)
        if pairs is not None:
            pa, pb, pw = pairs
            loss = (
                loss
                + (
                    pw
                    * torch.sqrt(
                        (pin_x[pa] - pin_x[pb]) ** 2 + (pin_y[pa] - pin_y[pb]) ** 2 + PAIR_EPS2
                    )
                ).sum()
            )
        if match_sets and w_match > 0.0:
            loss = loss + w_match * length_mismatch(match_sets, pin_x, pin_y, gamma)

        # Power-plane compactness + inter-domain separation. Each plane net gets a
        # smooth pad bbox; minimise its AREA (compact planes) and penalise overlap
        # between different planes' bboxes (disjoint domains).
        if plane_pin_idx and (w_plane > 0.0 or w_plane_sep > 0.0):
            boxes = []  # (minx, maxx, miny, maxy) per plane net
            for pins in plane_pin_idx:
                px, py = pin_x[pins], pin_y[pins]
                ext = pm.logsumexp(torch.stack((px, -px, py, -py)) / gamma, 1)
                maxx, minx = gamma * ext[0], -gamma * ext[1]
                maxy, miny = gamma * ext[2], -gamma * ext[3]
                boxes.append((minx, maxx, miny, maxy))
                loss = loss + w_plane * (maxx - minx) * (maxy - miny)  # bbox area
            for a in range(len(boxes)):
                axmin, axmax, aymin, aymax = boxes[a]
                for b in range(a + 1, len(boxes)):
                    bxmin, bxmax, bymin, bymax = boxes[b]
                    ox = torch.clamp(
                        torch.minimum(axmax, bxmax) - torch.maximum(axmin, bxmin), min=0.0
                    )
                    oy = torch.clamp(
                        torch.minimum(aymax, bymax) - torch.maximum(aymin, bymin), min=0.0
                    )
                    loss = loss + w_plane_sep * ox * oy

        for i, axis, edge, weight in edge_terms:
            extent = hh[i] if axis == 1 else hw[i]
            if edge in ("south", "west"):
                target = extent
            else:  # north / east
                target = (height if axis == 1 else width) - extent
            loss = loss + weight * (body[i, axis] - target) ** 2

        for members, anchor, radius, weight in group_terms:
            m = torch.tensor(members, dtype=torch.long)
            d = torch.linalg.vector_norm(pos[m] - pos[anchor], dim=1)
            loss = loss + weight * (torch.clamp(d - radius, min=0.0) ** 2).sum()

        if related is not None:
            # A side-free part's bodies and anchors are mixed over both sides, as its pins.
            term = related.loss(pos, p, away if sided is not None else None)
            if term is not None:
                loss = loss + term

        if keep_t is not None and frozen is not None:
            loss = loss + w_keep * slot_keepout(pos, frozen, keep_t, movable_f)
        elif keep_t is not None:
            kdx = (cx.unsqueeze(1) - keep_t[:, 0].unsqueeze(0)).abs()
            kdy = (cy.unsqueeze(1) - keep_t[:, 1].unsqueeze(0)).abs()
            kox = torch.clamp(
                hw.unsqueeze(1) + keep_t[:, 2].unsqueeze(0) + clearance - kdx, min=0.0
            )
            koy = torch.clamp(
                hh.unsqueeze(1) + keep_t[:, 3].unsqueeze(0) + clearance - kdy, min=0.0
            )
            loss = loss + w_keep * ((kox * koy) * movable_f.unsqueeze(1)).sum()

        if step == iters - 1 and os.environ.get("PNR_COST_CAPTURE_DIR"):
            from .cost_capture import record_global_loss

            record_global_loss(
                graph,
                constraints,
                pos.detach().tolist(),
                p.detach().tolist(),
                exp_off.detach().tolist(),
                exp_half.detach().tolist(),
                float(loss.detach()),
                dict(
                    gamma=gamma,
                    spread=spread,
                    w_spread=w_spread,
                    w_bound=w_bound,
                    w_keep=w_keep,
                    w_plane=w_plane,
                    w_plane_sep=w_plane_sep,
                ),
                inflation,
                step,
                # PNR_COMPACT offset courtyards: the expected body offsets (else none).
                **({} if body_off is None else dict(shift=(body - pos).detach().tolist())),
            )
        if tracer is not None and tracer.due(step):
            tracer.snapshot(step, pos, p)
        loss.backward()
        opt.step()

    pos = full_pos().detach()
    p = rot_probs(0.2).detach()
    angle_idx = torch.argmax(p, dim=1)
    positions = {c.ref: (float(pos[i, 0]), float(pos[i, 1])) for i, c in enumerate(comps)}
    rotations = {c.ref: float(ANGLES[int(angle_idx[i])]) for i, c in enumerate(comps)}
    if tracer is not None:
        tracer.finish(positions, rotations)
    if not return_sides:
        return positions, rotations
    sides = {c.ref: c.side for c in comps}
    if sided is not None:
        logits = sided["logits"].detach()
        for k, i in enumerate(sided["free"]):
            sides[comps[i].ref] = "bottom" if float(logits[k]) > 0.0 else "top"
    return positions, rotations, sides


def _freeze(graph, polish, probs, sided, comps, width, height, side_overlap):
    """The constants of the polish phase (PNR_GP_POLISH, :mod:`pnr.place.gp_polish`): the
    one-hot arg-max turns ``p`` (the turns :func:`global_place` returns), the frozen free sides
    ``bottom`` (0/1 per part; None without free sides) and their same-side ``mask``, plus the
    slot, pad-edge and channel tensors of :meth:`pnr.place.gp_polish.Polish.prepare`."""
    idx = torch.argmax(probs.detach(), dim=1)
    p = torch.nn.functional.one_hot(idx, 4).float()
    angles = [ANGLES[int(k)] for k in idx]
    sides = [c.side for c in comps]
    bottom, mask = None, side_overlap
    if sided is not None:
        logits = sided["logits"].detach()
        bottom = sided["current_bottom"].float().clone()
        for k, i in enumerate(sided["free"]):
            down = float(logits[k]) > 0.0
            bottom[i] = 1.0 if down else 0.0
            sides[i] = "bottom" if down else "top"
        mask = _side_overlap(sided, bottom)
    out = polish.prepare(graph, angles, sides, width, height)
    out.update(p=p, bottom=bottom, mask=mask)
    return out


# Initial side logit: toward the start side, like a start rotation's logit.
SIDE_LOGIT = 2.0


def _side_terms(graph, constraints, comps, idx, side_plan, free, initial_sides, pin_off4_t):
    """Constant tensors of the side relaxation (see the module docstring)."""
    from .sides import FLIP_MM, SIDE_PREF_MM, VIA_MM, stack_refs

    n = len(comps)
    current_bottom = torch.tensor([c.side == "bottom" for c in comps], dtype=torch.bool)
    both = torch.tensor([len(set(occupied_sides(c))) > 1 for c in comps], dtype=torch.bool)
    stack = stack_refs(graph, constraints)
    fan = torch.tensor([c.ref in stack for c in comps], dtype=torch.float32) if stack else None
    init = []
    for i in free:
        start = (initial_sides or {}).get(comps[i].ref, comps[i].side)
        init.append(SIDE_LOGIT if start == "bottom" else -SIDE_LOGIT)
    mirror = pin_off4_t.clone()
    for k, ang in enumerate(ANGLES):
        # Rotating the y-mirrored offset (ox, -oy): (ox c + oy s, ox s - oy c).
        ct, st = pm.quarter_turn(ang)
        r0 = pin_off4_t[:, 0, :]  # rot-0 offsets (ox, oy)
        mirror[:, k, 0] = r0[:, 0] * ct + r0[:, 1] * st
        mirror[:, k, 1] = r0[:, 0] * st - r0[:, 1] * ct
    nets = side_plan.nets(graph)
    member = torch.zeros(len(nets), n)
    for k, (_, refs) in enumerate(nets):
        for ref in refs:
            if ref in idx:
                member[k, idx[ref]] = 1.0
    pref = [
        (idx[ref], side == "bottom", SIDE_PREF_MM * weight)
        for ref, (side, weight) in sorted(side_plan.preferred.items())
        if ref in idx and idx[ref] in free
    ]
    source_bottom = torch.tensor(
        [side_plan.source.get(c.ref, c.side) == "bottom" for c in comps], dtype=torch.bool
    )
    free_t = torch.tensor(free, dtype=torch.long)
    return dict(
        free=free,
        free_t=free_t,
        logits=torch.nn.Parameter(torch.tensor(init, dtype=torch.float32)),
        current_bottom=current_bottom,
        both=both,
        fan=fan,
        pin_off4_mirror=mirror,
        member=member if nets else None,
        pref=pref,
        source_bottom=source_bottom,
        via_mm=VIA_MM,
        flip_mm=FLIP_MM,
    )


def _bottom_probability(sided, temp):
    """(n,) probability of the bottom side: free parts relaxed, the rest 0 or 1."""
    bottom = sided["current_bottom"].float()
    return bottom.index_put(
        (sided["free_t"],), pm.sigmoid(sided["logits"] / temp), accumulate=False
    )


def _side_overlap(sided, bottom):
    """(n, n) expected same-side weight ``min(1, o_i . o_j)``; two parts that fan out
    (:func:`pnr.place.sides.stack_refs`) also share the stack plane, weight 1."""
    top_o = torch.where(sided["both"], 1.0, 1.0 - bottom)
    bottom_o = torch.where(sided["both"], 1.0, bottom)
    weight = top_o.unsqueeze(1) * top_o.unsqueeze(0) + bottom_o.unsqueeze(1) * bottom_o.unsqueeze(0)
    if sided["fan"] is not None:
        weight = weight + sided["fan"].unsqueeze(1) * sided["fan"].unsqueeze(0)
    return torch.clamp(weight, max=1.0)


def _side_cost(sided, bottom):
    """Expected layer changes, side preferences and source-side departures (mm)."""
    cost = bottom.new_zeros(())
    if sided["member"] is not None:
        log_top = pm.log(torch.clamp(1.0 - bottom, min=1e-9))
        log_bottom = pm.log(torch.clamp(bottom, min=1e-9))
        member = sided["member"]
        split = 1.0 - pm.exp(member @ log_top) - pm.exp(member @ log_bottom)
        cost = cost + sided["via_mm"] * torch.clamp(split, min=0.0).sum()
    for i, want_bottom, weight in sided["pref"]:
        cost = cost + weight * ((1.0 - bottom[i]) if want_bottom else bottom[i])
    off_source = torch.where(sided["source_bottom"], 1.0 - bottom, bottom)
    cost = cost + sided["flip_mm"] * off_source[sided["free_t"]].sum()
    return cost
