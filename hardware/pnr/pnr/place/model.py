"""Differentiable global placement (design doc §4/§8, orientation §9.3).

Relaxes a continuous placement by gradient descent on a smooth loss:

    L = WL(log-sum-exp HPWL)          # connected pins attract
      + w_spread * overlap            # courtyards repel (spreading)
      + w_bound  * outline_penalty    # stay inside the board
      + w_edge   * edge_align         # soft pull to a board edge
      + w_keep   * keepout_penalty    # keep movable parts out of keep-outs
      + w_group  * grouping           # cluster grouped parts near their anchor

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
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional, Tuple

import torch

from pnr.constraints import CompiledConstraints
from pnr.graph import BoardGraph

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


def _base_half_sizes(graph: BoardGraph) -> torch.Tensor:
    """Unrotated courtyard half-(w, h) per component (parts ingest at rot 0)."""
    hs = [(c.courtyard[0] / 2.0, c.courtyard[1] / 2.0) for c in graph.components]
    return torch.tensor(hs, dtype=torch.float32)


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
    initial_positions: Optional[Dict[str, Tuple[float, float]]] = None,
    initial_rotations: Optional[Dict[str, float]] = None,
    pair_weights: Optional[Dict[Tuple[str, str, str, str], float]] = None,
    side_plan=None,
    initial_sides: Optional[Dict[str, str]] = None,
    return_sides: bool = False,
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

    ``side_plan`` (:func:`pnr.place.sides.plan`) relaxes the side of each of its free
    parts too, starting toward ``initial_sides`` ({ref: side}, default the part's
    current side); without free parts it changes nothing.

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
    init[:, 0] = half[:, 0] + init[:, 0] * (width - 2 * half[:, 0])
    init[:, 1] = half[:, 1] + init[:, 1] * (height - 2 * half[:, 1])
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
            raw = torch.softmax(rot_logits / temp, dim=1)
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
                th = torch.deg2rad(torch.tensor(ang))
                ct, st = float(torch.cos(th)), float(torch.sin(th))
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

    plane_patterns = [pat for nc in constraints.net_classes if nc.plane_layer for pat in nc.nets]
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

    keepouts = keepout_rects(graph, constraints, poses)
    keep_t = (
        torch.tensor([[k.cx, k.cy, k.w / 2, k.h / 2] for k in keepouts], dtype=torch.float32)
        if keepouts
        else None
    )

    movable_f = (~is_fixed).float()
    clearance = float(constraints.board.default_clearance_mm)
    opt = torch.optim.Adam(params, lr=lr)
    from pnr.trace import placement_tracer

    tracer = placement_tracer(comps, iters)  # None unless PNR_TRACE_DIR is set (pnr.trace)

    for step in range(iters):
        temp = 2.0 - (2.0 - 0.2) * (step / max(1, iters - 1))  # anneal 2.0 -> 0.2
        opt.zero_grad()
        pos = full_pos()
        p = rot_probs(temp)  # (n, 4)

        # Expected pin offset under the rotation distribution.
        p_pin = p[pin_comp_t]  # (P, 4)
        exp_off = (p_pin.unsqueeze(-1) * pin_off4_t).sum(1)  # (P, 2)
        if sided is not None:
            # ... and under the side distribution: the far side mirrors the offsets.
            bottom = _bottom_probability(sided, temp)  # (n,)
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
                wl = wl + gamma * (
                    torch.logsumexp(px / gamma, 0)
                    + torch.logsumexp(-px / gamma, 0)
                    + torch.logsumexp(py / gamma, 0)
                    + torch.logsumexp(-py / gamma, 0)
                )

        # Expected courtyard half-size (rotation-aware).
        exp_half = (p.unsqueeze(-1) * half4).sum(1)  # (n, 2)
        hw, hh = exp_half[:, 0], exp_half[:, 1]

        # Pairwise smooth overlap (spreading), upper triangle only.
        if bodies is None:
            dx = (pos[:, 0].unsqueeze(1) - pos[:, 0].unsqueeze(0)).abs()
            dy = (pos[:, 1].unsqueeze(1) - pos[:, 1].unsqueeze(0)).abs()
            sw = hw.unsqueeze(1) + hw.unsqueeze(0) + clearance
            sh = hh.unsqueeze(1) + hh.unsqueeze(0) + clearance
            ox = torch.clamp(sw - dx, min=0.0)
            oy = torch.clamp(sh - dy, min=0.0)
            mask = side_overlap if sided is None else _side_overlap(sided, bottom)
            overlap = torch.triu(ox * oy * mask, diagonal=1).sum()
        else:
            overlap = gp_overlap(bodies, pos, p, clearance)

        # Outline containment.
        cx, cy = pos[:, 0], pos[:, 1]
        bound = (
            torch.clamp(hw - cx, min=0.0) ** 2
            + torch.clamp(cx + hw - width, min=0.0) ** 2
            + torch.clamp(hh - cy, min=0.0) ** 2
            + torch.clamp(cy + hh - height, min=0.0) ** 2
        )
        bound = (bound * movable_f).sum()

        loss = wl + w_spread * overlap + w_bound * bound
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

        # Power-plane compactness + inter-domain separation. Each plane net gets a
        # smooth pad bbox; minimise its AREA (compact planes) and penalise overlap
        # between different planes' bboxes (disjoint domains).
        if plane_pin_idx and (w_plane > 0.0 or w_plane_sep > 0.0):
            boxes = []  # (minx, maxx, miny, maxy) per plane net
            for pins in plane_pin_idx:
                px, py = pin_x[pins], pin_y[pins]
                maxx = gamma * torch.logsumexp(px / gamma, 0)
                minx = -gamma * torch.logsumexp(-px / gamma, 0)
                maxy = gamma * torch.logsumexp(py / gamma, 0)
                miny = -gamma * torch.logsumexp(-py / gamma, 0)
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
            loss = loss + weight * (pos[i, axis] - target) ** 2

        for members, anchor, radius, weight in group_terms:
            m = torch.tensor(members, dtype=torch.long)
            d = torch.linalg.vector_norm(pos[m] - pos[anchor], dim=1)
            loss = loss + weight * (torch.clamp(d - radius, min=0.0) ** 2).sum()

        if keep_t is not None:
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
        th = torch.deg2rad(torch.tensor(ang))
        # Rotating the y-mirrored offset (ox, -oy): (ox c + oy s, ox s - oy c).
        ct, st = float(torch.cos(th)), float(torch.sin(th))
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
        (sided["free_t"],), torch.sigmoid(sided["logits"] / temp), accumulate=False
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
        log_top = torch.log(torch.clamp(1.0 - bottom, min=1e-9))
        log_bottom = torch.log(torch.clamp(bottom, min=1e-9))
        member = sided["member"]
        split = 1.0 - torch.exp(member @ log_top) - torch.exp(member @ log_bottom)
        cost = cost + sided["via_mm"] * torch.clamp(split, min=0.0).sum()
    for i, want_bottom, weight in sided["pref"]:
        cost = cost + weight * ((1.0 - bottom[i]) if want_bottom else bottom[i])
    off_source = torch.where(sided["source_bottom"], 1.0 - bottom, bottom)
    cost = cost + sided["flip_mm"] * off_source[sided["free_t"]].sum()
    return cost
