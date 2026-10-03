"""Lexicographic power-first placement (``PNR_POWER_FIRST=1``).

A human lays out a converter power stage first (shortest current paths,
ideally on one layer), then the controller to minimise control and sense
routing, then the incidental passives. Here that order is the *structure* of
the objective rather than a weight setting. With tiers, trunks and hot loops
from :func:`pnr.power_topology.derive`, stage k minimises

    F_k = J_k + sum_{j<k} Omega s_j softplus((J_j - (1+eps_j) J_j*) / s_j)
          + w_ov,k(t) overlap + 20 W bound + w_group W rho(t) group(r - margin)
          + plane area/separation + edge + keepout

    J1 = sum_P w_n HPWL(trunk_n) + sum_R HPWL(trunk_n) + sum_hot w_L Lambda_L
    J2 = HPWL(signal nets among tier-1/controller parts) + sum D_t(tap, trunk) of their taps
    J3 = HPWL(remaining signal nets) + sum D_t(tap, trunk) of tier-3 taps

where Lambda_L is a softmin over loop member choices of the summed pad-group
softmin gaps D_t along the loop, and J_j* is stage j's result: a later stage
may spend at most eps_j of an earlier stage's optimum. Parts of later tiers are
ghosts during earlier stages (area, outline, group and plane terms only).
Stage 1 runs K batched random starts and keeps the lowest F1 (plus the
runner-up for one bounded legalization retry).

``model.global_place`` is untouched; everything here runs only behind the flag.
"""

from __future__ import annotations

import fnmatch
import itertools
import logging
import math
import os

import numpy as np

log = logging.getLogger(__name__)

T_SOFT = 0.5  # mm: pad-pair / member-choice softmin temperature
GAP_EPS2 = 0.01  # mm^2 inside the pad distance sqrt
GAMMA = 1.0  # mm: LSE-HPWL smoothing
SPLIT = (0.3, 0.15, 0.55)
ITER_SCALE = 4.0 / 3.0
STARTS = 8
EPS = (0.03, 0.05)
OMEGA = 10.0
GUARD_SCALE = 0.01
OVERLAP_RAMP = ((0.02, 4.0), (1.0, 10.0), (1.0, 10.0))  # geometric within each stage
OVERLAP_TIMES_W = (True, False, False)  # stage 1 ramps 0.02W -> 4W; stages 2-3 1 -> 10
RHO_RAMP = (1.0, 20.0)  # linear within each stage
TEMP_HI = (2.0, 1.0, 1.0)
TEMP_LO = 0.2
GHOST_TEMP = 2.0
PRIOR_LOGIT = 2.0
RETRY_RATIO = (
    1.25  # legal/continuous growth of J1, or of any hot loop's Lambda, that triggers the retry
)
LOOK_AHEAD_TRIES = 40


def enabled() -> bool:
    from pnr.power_topology import enabled as flag

    return flag()


def roles_for(graph, constraints, channel_rules):
    """Power-first roles, or None (with a warning) when the default path must run."""
    from pnr.power_topology import PowerTopologyUnavailable, derive

    if not channel_rules:
        log.warning("PNR_POWER_FIRST=1 but place() received no channel_rules; default placement")
        return None
    if not channel_rules.get("electrical_nets"):
        log.warning("PNR_POWER_FIRST=1 but rules lack electrical_nets; default placement")
        return None
    from pnr.hier.blocks import extract_blocks

    # At top level a loop through parts of different blocks is a conduction path.
    block_of = {r: b.name for b in extract_blocks(graph, constraints) for r in b.refs}
    try:
        roles = derive(graph, constraints, channel_rules, block_of=block_of)
    except PowerTopologyUnavailable as exc:
        log.warning("PNR_POWER_FIRST=1 but %s; default placement", exc)
        return None
    if not any(e["stage"] == 1 for e in roles["elements"]):
        log.warning("PNR_POWER_FIRST=1 but no power stage was derived; default placement")
        return None
    return roles


# ----------------------------------------------------------------- cost elements


class Compiled:
    """Power-first cost elements as index arrays over the global pad order.

    The global pad order is every pad of every component in graph order, the
    same order ``model.global_place`` and ``cost_inspect.Objective`` use.
    """

    def __init__(self, graph, roles):
        self.refs = [c.ref for c in graph.components]
        self.index = {r: i for i, r in enumerate(self.refs)}
        where, owner, k = {}, [], 0
        for i, c in enumerate(graph.components):
            for p in c.pads:
                where.setdefault((c.ref, p.name, p.net), []).append(k)
                owner.append(i)
                k += 1
        self.npads = k
        self.owner = np.asarray(owner, dtype=np.int64)
        self.W = float(roles["W"])

        def pads(pins, net):
            out = []
            for r, p in pins:
                for j in where.get((r, p, net), ()):
                    if j not in out:
                        out.append(j)
            return out

        self.bbox = {1: [], 2: [], 3: []}  # (pad idx, weight, name, role, owners)
        self.taps = {1: [], 2: [], 3: []}  # (A idx, B idx, weight, name, owners)
        self.loops = {
            1: [],
            2: [],
            3: [],
        }  # (weight, name, [[(A idx, B idx) per link] per combo], owners)
        for e in roles["elements"]:
            s = e["stage"]
            if e["kind"] == "bbox":
                idx = pads(e["pins"], e["net"])
                if len(idx) >= 2:
                    self.bbox[s].append(
                        (
                            idx,
                            float(e["weight"]),
                            e["name"],
                            e["role"],
                            sorted({self.index[r] for r, _ in e["pins"]}),
                        )
                    )
            elif e["kind"] == "tap":
                a, b = pads([e["pin"]], e["net"]), pads(e["targets"], e["net"])
                if a and b:
                    self.taps[s].append(
                        (
                            a,
                            b,
                            float(e["weight"]),
                            e["name"],
                            sorted(
                                {self.index[e["pin"][0]]} | {self.index[r] for r, _ in e["targets"]}
                            ),
                        )
                    )
            else:
                members, nets, pins = e["members"], e["nets"], e["pins"]
                k_ = len(members)

                def link(j, ra, rb):
                    n = nets[j]
                    return (
                        pads([[r, p] for r in ra for p in pins[r].get(n, [])], n),
                        pads([[r, p] for r in rb for p in pins[r].get(n, [])], n),
                    )

                if e["capped"]:
                    combos = [[link(j, members[j], members[(j + 1) % k_]) for j in range(k_)]]
                else:
                    combos = [
                        [link(j, [pick[j]], [pick[(j + 1) % k_]]) for j in range(k_)]
                        for pick in itertools.product(*members)
                    ]
                owners = sorted({self.index[r] for m in members for r in m})
                self.loops[s].append((float(e["weight"]), e["name"], combos, owners))

    # -------------------------------------------------------------- numpy side
    def numpy_terms(self, pp, gamma=GAMMA, t=T_SOFT):
        """Per-element values at pad positions ``pp`` of shape (B, P, 2) (float64)."""
        pp = np.asarray(pp, dtype=float)
        if pp.ndim == 2:
            pp = pp[None]

        def lse(x, axis):
            return np.logaddexp.reduce(x, axis=axis)

        def hpwl(idx):
            q = pp[:, idx, :]
            return gamma * (lse(q / gamma, 1) + lse(-q / gamma, 1)).sum(-1)

        def dt(a, b):
            d = np.sqrt(((pp[:, a, None, :] - pp[:, None, b, :]) ** 2).sum(-1) + GAP_EPS2)
            return -t * lse((-d / t).reshape(len(pp), -1), 1)

        out = []
        for s in (1, 2, 3):
            for idx, w, name, role, owners in self.bbox[s]:
                out.append(
                    dict(
                        stage=s,
                        kind="bbox",
                        role=role,
                        name=name,
                        weight=w,
                        owners=owners,
                        value=hpwl(idx),
                    )
                )
            for a, b, w, name, owners in self.taps[s]:
                out.append(
                    dict(
                        stage=s,
                        kind="tap",
                        role="tap",
                        name=name,
                        weight=w,
                        owners=owners,
                        value=dt(a, b),
                    )
                )
            for w, name, combos, owners in self.loops[s]:
                per = np.stack([sum(dt(a, b) for a, b in links) for links in combos], axis=1)
                out.append(
                    dict(
                        stage=s,
                        kind="loop",
                        role="loop",
                        name=name,
                        weight=w,
                        owners=owners,
                        value=-t * lse(-per / t, 1),
                    )
                )
        return out

    def numpy_j(self, pp, gamma=GAMMA):
        """(J1, J2, J3) arrays of shape (B,) at pad positions ``pp``."""
        terms = self.numpy_terms(pp, gamma)
        b = (
            terms[0]["value"].shape[0]
            if terms
            else np.asarray(pp).reshape((-1, self.npads, 2)).shape[0]
        )
        j = {s: np.zeros(b) for s in (1, 2, 3)}
        for e in terms:
            j[e["stage"]] = j[e["stage"]] + e["weight"] * e["value"]
        return j[1], j[2], j[3]


def rigid_pads(graph):
    """(P, 2) absolute pad centres of a posed graph, global pad order."""
    out = []
    for c in graph.components:
        th = math.radians(c.rot)
        ct, st = math.cos(th), math.sin(th)
        for p in c.pads:
            x, y = p.offset
            out.append((c.pos[0] + x * ct - y * st, c.pos[1] + x * st + y * ct))
    return np.asarray(out, dtype=float).reshape((-1, 2))


def rigid_j(graph, roles, compiled=None):
    compiled = compiled or Compiled(graph, roles)
    j1, j2, j3 = compiled.numpy_j(rigid_pads(graph))
    return float(j1[0]), float(j2[0]), float(j3[0])


# ----------------------------------------------------------------- torch side


def torch_gap(xy, a, am, b, bm, t=T_SOFT):
    """D_t per pad-group pair: -t log sum exp(-sqrt(d^2 + GAP_EPS2) / t); xy (K, P, 2) -> (K, Q)."""
    import torch

    pa, pb = xy[:, a, :], xy[:, b, :]  # (K, Q, ma, 2), (K, Q, mb, 2)
    d = torch.sqrt(((pa[:, :, :, None, :] - pb[:, :, None, :, :]) ** 2).sum(-1) + GAP_EPS2)
    z = (-d / t).masked_fill(~(am[:, :, None] & bm[:, None, :])[None], -math.inf)
    return -t * torch.logsumexp(z.flatten(2), dim=2)


def torch_guard(value, star, eps):
    """Lexicographic guard Omega s softplus((J - (1 + eps) J*) / s), s = GUARD_SCALE J*."""
    import torch

    s = GUARD_SCALE * star + 1e-6
    return OMEGA * s * torch.nn.functional.softplus((value - (1 + eps) * star) / s)


class _TorchCost:
    def __init__(self, compiled, gamma):
        import torch

        self.torch = torch
        self.gamma = gamma
        self.stage = {}
        for s in (1, 2, 3):
            bb = compiled.bbox[s]
            taps = compiled.taps[s]
            loops = compiled.loops[s]
            entry = dict(bbox=None, taps=None, loops=None)
            if bb:
                idx, mask = self._pad([e[0] for e in bb])
                entry["bbox"] = (idx, mask, torch.tensor([e[1] for e in bb], dtype=torch.float32))
            if taps:
                a, am = self._pad([e[0] for e in taps])
                b, bm = self._pad([e[1] for e in taps])
                entry["taps"] = (
                    a,
                    am,
                    b,
                    bm,
                    torch.tensor([e[2] for e in taps], dtype=torch.float32),
                )
            if loops:
                pairs, spec = [], []
                for w, _, combos, _ in loops:
                    rows = []
                    for links in combos:
                        rows.append([len(pairs) + j for j in range(len(links))])
                        pairs.extend(links)
                    spec.append((w, torch.tensor(rows, dtype=torch.long)))
                a, am = self._pad([p[0] for p in pairs])
                b, bm = self._pad([p[1] for p in pairs])
                entry["loops"] = (a, am, b, bm, spec)
            self.stage[s] = entry

    def _pad(self, groups):
        torch = self.torch
        m = max(1, max(len(g) for g in groups))
        idx = np.zeros((len(groups), m), dtype=np.int64)
        mask = np.zeros((len(groups), m), dtype=bool)
        for i, g in enumerate(groups):
            idx[i, : len(g)] = g
            mask[i, : len(g)] = True
        return torch.as_tensor(idx), torch.as_tensor(mask)

    def __call__(self, stage, xy):
        """J_stage for pin positions ``xy`` of shape (K, P, 2) -> (K,)."""
        torch = self.torch
        e = self.stage[stage]
        total = xy.new_zeros(xy.shape[0])
        if e["bbox"] is not None:
            idx, mask, w = e["bbox"]
            q = xy[:, idx, :] / self.gamma  # (K, E, M, 2)
            signed = torch.cat((q, -q), dim=-1).masked_fill(~mask[None, :, :, None], -math.inf)
            total = total + (self.gamma * torch.logsumexp(signed, dim=2).sum(-1) * w).sum(-1)
        if e["taps"] is not None:
            a, am, b, bm, w = e["taps"]
            total = total + (torch_gap(xy, a, am, b, bm) * w).sum(-1)
        if e["loops"] is not None:
            a, am, b, bm, spec = e["loops"]
            d = torch_gap(xy, a, am, b, bm)
            for w, rows in spec:
                per = d[:, rows].sum(-1)  # (K, C)
                total = total + w * (-T_SOFT * torch.logsumexp(-per / T_SOFT, dim=1))
        return total


class StagedPlacer:
    """Staged lexicographic global placement; mirrors ``model.global_place`` set-up."""

    def __init__(
        self,
        graph,
        constraints,
        width,
        height,
        roles,
        *,
        seed=0,
        iters=800,
        lr=0.3,
        gamma=GAMMA,
        orient=True,
        inflation=None,
        spread=1.0,
        w_spread=1.0,
        w_bound=20.0,
        w_keep=40.0,
        w_group=0.5,
        w_plane=0.05,
        w_plane_sep=0.35,
        initial_positions=None,
        initial_rotations=None,
        starts=STARTS,
        grid_mm=0.0,
        pair_weights=None,
    ):
        import torch

        from pnr.constraints import Enforcement

        from .geometry import (
            keepout_rects,
            occupied_sides,
            resolve_fixed_poses,
            resolve_hard_rotations,
        )
        from .model import ANGLES, _base_half_sizes

        self.torch = torch
        self.ANGLES = ANGLES
        torch.manual_seed(seed)
        self.graph, self.constraints, self.roles = graph, constraints, roles
        self.width, self.height = width, height
        self.seed, self.lr, self.gamma, self.orient = seed, lr, gamma, orient
        self.inflation, self.spread = inflation, spread
        self.w = dict(
            w_spread=w_spread,
            w_bound=w_bound,
            w_keep=w_keep,
            w_group=w_group,
            w_plane=w_plane,
            w_plane_sep=w_plane_sep,
        )
        comps = graph.components
        n = self.n = len(comps)
        idx = {c.ref: i for i, c in enumerate(comps)}
        self.W = float(roles["W"])
        total = max(3, int(math.ceil(iters * ITER_SCALE)))
        s1, s2 = max(1, int(round(total * SPLIT[0]))), max(1, int(round(total * SPLIT[1])))
        self.steps = (s1, s2, max(1, total - s1 - s2))
        mixed = set(roles["mixed"])
        tier = [roles["tier"].get(c.ref, 3) for c in comps]
        self.tier = torch.tensor(tier)
        self.mixed = torch.tensor([c.ref in mixed for c in comps])
        self.side_overlap = torch.tensor(
            [[bool(set(occupied_sides(a)) & set(occupied_sides(b))) for b in comps] for a in comps],
            dtype=torch.float32,
        )
        half = _base_half_sizes(graph)
        if inflation or spread > 1.0:
            scale = torch.tensor(
                [[max(1.0, spread, float((inflation or {}).get(c.ref, 1.0)))] for c in comps],
                dtype=torch.float32,
            )
            half = half * scale
        self.half = half
        self.half4 = torch.stack([half, half[:, [1, 0]], half, half[:, [1, 0]]], dim=1)
        # Hull macros (PNR_MACRO_HULL=1): per-side overlap bodies; None otherwise.
        from .hull import gp_bodies

        self.bodies = gp_bodies(
            comps,
            (
                [max(1.0, spread, float((inflation or {}).get(c.ref, 1.0))) for c in comps]
                if (inflation or spread > 1.0)
                else None
            ),
        )
        poses = resolve_fixed_poses(graph, constraints)
        self.is_fixed = torch.zeros(n, dtype=torch.bool)
        self.fixed_xy = torch.zeros(n, 2)
        fixed_angle = torch.zeros(n, dtype=torch.long)
        fixed_rot = {}
        for con in constraints.constraints:
            if con.kind == "fixed":
                for ref in con.refs:
                    fixed_rot[ref] = con.params.get("rot") or 0.0
        for ref, (px, py) in poses.items():
            if ref in idx:
                self.is_fixed[idx[ref]] = True
                self.fixed_xy[idx[ref]] = torch.tensor([px, py])
                fixed_angle[idx[ref]] = int(round(fixed_rot.get(ref, 0.0) / 90.0)) % 4
        self.rotation_fixed = self.is_fixed.clone()
        for ref, angle in resolve_hard_rotations(constraints).items():
            if ref in idx:
                self.rotation_fixed[idx[ref]] = True
                fixed_angle[idx[ref]] = int(round(angle / 90)) % 4
        if not orient:
            self.rotation_fixed[:] = True
        self.fixed_onehot = torch.nn.functional.one_hot(fixed_angle, 4).float()

        # Same seeded start as model.global_place (start 0), then K-1 extra starts.
        init = torch.rand(n, 2)
        init[:, 0] = half[:, 0] + init[:, 0] * (width - 2 * half[:, 0])
        init[:, 1] = half[:, 1] + init[:, 1] * (height - 2 * half[:, 1])
        if initial_positions is not None:
            for ref, xy in initial_positions.items():
                if ref not in idx or len(xy) != 2:
                    raise ValueError("invalid initial placement reference/coordinate")
                point = torch.tensor(xy, dtype=torch.float32)
                if not bool(torch.isfinite(point).all()):
                    raise ValueError("initial placement contains non-finite coordinates")
                init[idx[ref]] = point
        gen = torch.Generator().manual_seed(1000003 * (seed + 1))
        inits = [init]
        for _ in range(1, max(1, starts)):
            r = torch.rand(n, 2, generator=gen)
            inits.append(
                torch.stack(
                    [
                        half[:, 0] + r[:, 0] * (width - 2 * half[:, 0]),
                        half[:, 1] + r[:, 1] * (height - 2 * half[:, 1]),
                    ],
                    dim=1,
                )
            )
        self.inits = torch.stack(inits)
        self.init_logits = torch.zeros(len(inits), n, 4)
        for ref, angle in (initial_rotations or {}).items():
            if ref not in idx or not math.isfinite(float(angle)):
                raise ValueError("invalid initial rotation")
            self.init_logits[0, idx[ref], int(round(angle / 90.0)) % 4] = PRIOR_LOGIT

        pin_comp, off4, pin_key = [], [], {}
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
                off4.append(variants)
        self.pin_comp = torch.tensor(pin_comp, dtype=torch.long)
        self.pin_off4 = torch.tensor(off4, dtype=torch.float32).reshape((-1, 4, 2))
        from .model import pair_tensors

        self.pairs = pair_tensors(pair_weights, pin_key)
        from pnr.stack import split_plane_patterns

        pats = split_plane_patterns(constraints, graph)
        self.plane_pins = []
        for net in graph.nets:
            if any(fnmatch.fnmatch(net.name, p) for p in pats):
                pins = [pin_key[p] for p in net.pins if p in pin_key]
                if len(pins) >= 2:
                    self.plane_pins.append(torch.tensor(pins, dtype=torch.long))
        self.edges = []
        for con in constraints.constraints:
            if con.kind == "edge_align":
                edge = con.params.get("edge")
                for ref in con.refs:
                    if ref in idx:
                        self.edges.append(
                            (
                                idx[ref],
                                1 if edge in ("south", "north") else 0,
                                edge,
                                con.weight or 1.0,
                            )
                        )
        self.groups = []
        for con in constraints.constraints:
            if con.kind != "group" or con.params.get("anchor") not in idx:
                continue
            members = [idx[r] for r in con.refs if r in idx and r != con.params["anchor"]]
            if members:
                radius = float(con.params.get("radius_mm") or 5.0)
                margin = min(0.5, radius / 4) if con.enforcement is Enforcement.HARD else 0.0
                self.groups.append(
                    (
                        torch.tensor(members, dtype=torch.long),
                        idx[con.params["anchor"]],
                        radius - margin,
                        con.weight or 1.0,
                    )
                )
        keep = keepout_rects(graph, constraints, poses)
        self.keep = (
            torch.tensor([[k.cx, k.cy, k.w / 2, k.h / 2] for k in keep], dtype=torch.float32)
            if keep
            else None
        )
        self.movable = (~self.is_fixed).float()
        # Courtyards separate by the board clearance plus one legalizer grid cell:
        # the legalizer rounds every block up to whole cells, so a continuous
        # layout packed at exactly the clearance would not fit its grid.
        self.clearance = float(constraints.board.default_clearance_mm)
        self.overlap_clearance = self.clearance + float(grid_mm)
        self.compiled = Compiled(graph, roles)
        self.cost = _TorchCost(self.compiled, gamma)

    # ------------------------------------------------------------- helpers
    def _temps(self, stage, frac, active):
        hi = TEMP_HI[stage - 1]
        t = self.torch.full((self.n,), GHOST_TEMP)
        t[active] = hi - (hi - TEMP_LO) * frac
        return t

    def _probs(self, logits, temps, frozen, frozen_oh):
        raw = self.torch.softmax(logits / temps[None, :, None], dim=2)
        return self.torch.where(frozen[None, :, None], frozen_oh, raw)

    def _pins(self, pos, p):
        off = (p[:, self.pin_comp, :, None] * self.pin_off4[None]).sum(2)  # (K, P, 2)
        return pos[:, self.pin_comp, :] + off, off

    def objective(self, stage, pos, p, frac, stars):
        """Stage loss per start (K,), plus the stage-3 factors for cost capture."""
        torch = self.torch
        w = self.w
        W = self.W
        xy, off = self._pins(pos, p)
        J = self.cost(stage, xy)
        guard = torch.zeros_like(J)
        for j in range(1, stage):
            guard = guard + torch_guard(self.cost(j, xy), stars[j], EPS[j - 1])
        exp_half = (p.unsqueeze(-1) * self.half4[None]).sum(2)  # (K, n, 2)
        hw, hh = exp_half[..., 0], exp_half[..., 1]
        if self.bodies is None:
            dx = (pos[:, :, None, 0] - pos[:, None, :, 0]).abs()
            dy = (pos[:, :, None, 1] - pos[:, None, :, 1]).abs()
            ox = torch.clamp(hw[:, :, None] + hw[:, None, :] + self.overlap_clearance - dx, min=0.0)
            oy = torch.clamp(hh[:, :, None] + hh[:, None, :] + self.overlap_clearance - dy, min=0.0)
            overlap = torch.triu(ox * oy * self.side_overlap, diagonal=1).sum((1, 2))
        else:
            from .hull import gp_overlap

            overlap = gp_overlap(self.bodies, pos, p, self.overlap_clearance)
        a, b = OVERLAP_RAMP[stage - 1]
        w_ov = w["w_spread"] * (W if OVERLAP_TIMES_W[stage - 1] else 1.0) * a * (b / a) ** frac
        cx, cy = pos[..., 0], pos[..., 1]
        bound = (
            torch.clamp(hw - cx, min=0.0) ** 2
            + torch.clamp(cx + hw - self.width, min=0.0) ** 2
            + torch.clamp(hh - cy, min=0.0) ** 2
            + torch.clamp(cy + hh - self.height, min=0.0) ** 2
        )
        bound = (bound * self.movable).sum(-1)
        rho = RHO_RAMP[0] + (RHO_RAMP[1] - RHO_RAMP[0]) * frac
        group = torch.zeros_like(J)
        for members, anchor, reach, weight in self.groups:
            d = torch.linalg.vector_norm(pos[:, members] - pos[:, anchor : anchor + 1], dim=-1)
            group = group + weight * (torch.clamp(d - reach, min=0.0) ** 2).sum(-1)
        loss = (
            J + guard + w_ov * overlap + w["w_bound"] * W * bound + w["w_group"] * W * rho * group
        )
        if self.pairs is not None:
            from .model import PAIR_EPS2

            pa, pb, pw = self.pairs
            d = torch.sqrt(((xy[:, pa] - xy[:, pb]) ** 2).sum(-1) + PAIR_EPS2)  # (K, pairs)
            loss = loss + W * (pw[None] * d).sum(-1)
        if self.plane_pins and (w["w_plane"] > 0.0 or w["w_plane_sep"] > 0.0):
            boxes = []
            g = self.gamma
            for pins in self.plane_pins:
                px, py = xy[:, pins, 0], xy[:, pins, 1]
                box = (
                    -g * torch.logsumexp(-px / g, 1),
                    g * torch.logsumexp(px / g, 1),
                    -g * torch.logsumexp(-py / g, 1),
                    g * torch.logsumexp(py / g, 1),
                )
                boxes.append(box)
                loss = loss + w["w_plane"] * (box[1] - box[0]) * (box[3] - box[2])
            for i in range(len(boxes)):
                for k in range(i + 1, len(boxes)):
                    A, B = boxes[i], boxes[k]
                    ox_ = torch.clamp(
                        torch.minimum(A[1], B[1]) - torch.maximum(A[0], B[0]), min=0.0
                    )
                    oy_ = torch.clamp(
                        torch.minimum(A[3], B[3]) - torch.maximum(A[2], B[2]), min=0.0
                    )
                    loss = loss + w["w_plane_sep"] * ox_ * oy_
        for i, axis, edge, weight in self.edges:
            extent = hh[:, i] if axis == 1 else hw[:, i]
            target = (
                extent
                if edge in ("south", "west")
                else (self.height if axis == 1 else self.width) - extent
            )
            loss = loss + weight * (pos[:, i, axis] - target) ** 2
        if self.keep is not None:
            k = self.keep
            kdx = (cx[:, :, None] - k[None, None, :, 0]).abs()
            kdy = (cy[:, :, None] - k[None, None, :, 1]).abs()
            kox = torch.clamp(hw[:, :, None] + k[None, None, :, 2] + self.clearance - kdx, min=0.0)
            koy = torch.clamp(hh[:, :, None] + k[None, None, :, 3] + self.clearance - kdy, min=0.0)
            loss = loss + w["w_keep"] * (kox * koy * self.movable[None, :, None]).sum((1, 2))
        factors = dict(
            w_ov=float(w_ov), bound=float(w["w_bound"] * W), group=float(w["w_group"] * W * rho)
        )
        return loss, J, dict(off=off, exp_half=exp_half, factors=factors)

    def _pos(self, move):
        return self.torch.where(self.is_fixed[None, :, None], self.fixed_xy[None], move)

    def _snap(self, logits, frozen, frozen_oh, parts):
        """Freeze ``parts`` (bool mask) at their argmax rotation."""
        torch = self.torch
        am = torch.argmax(logits, dim=2)
        newly = parts & ~frozen
        oh = torch.nn.functional.one_hot(am, 4).float()
        frozen_oh = torch.where(newly[None, :, None], oh, frozen_oh)
        return frozen | newly, frozen_oh

    def _rigid_j(self, stage, move, logits, frozen, frozen_oh, snap):
        with self.torch.no_grad():
            f, foh = self._snap(logits, frozen, frozen_oh, snap)
            p = self._probs(logits, self.torch.full((self.n,), TEMP_LO), f, foh)
            xy, _ = self._pins(self._pos(move), p)
            return self.cost(stage, xy)

    def _run(self, stage, move, logits, frozen, frozen_oh, active, stars, capture=False):
        torch = self.torch
        opt = torch.optim.Adam([move, logits], lr=self.lr)
        steps = self.steps[stage - 1]
        last = None
        for t in range(steps):
            frac = t / max(1, steps - 1)
            opt.zero_grad()
            pos = self._pos(move)
            p = self._probs(logits, self._temps(stage, frac, active), frozen, frozen_oh)
            loss, J, extra = self.objective(stage, pos, p, frac, stars)
            if capture and t == steps - 1 and os.environ.get("PNR_COST_CAPTURE_DIR"):
                from .cost_capture import record_global_loss

                record_global_loss(
                    self.graph,
                    self.constraints,
                    pos[0].detach().tolist(),
                    p[0].detach().tolist(),
                    extra["off"][0].detach().tolist(),
                    extra["exp_half"][0].detach().tolist(),
                    float(loss[0].detach()),
                    dict(
                        gamma=self.gamma,
                        spread=self.spread,
                        w_spread=self.w["w_spread"],
                        w_bound=self.w["w_bound"],
                        w_keep=self.w["w_keep"],
                        w_plane=self.w["w_plane"],
                        w_plane_sep=self.w["w_plane_sep"],
                    ),
                    self.inflation,
                    sum(self.steps) - 1,
                    roles=self.roles,
                    pf_state=dict(
                        stage=3,
                        stars={str(k): v for k, v in stars.items()},
                        eps=list(EPS),
                        omega=OMEGA,
                        guard_scale=GUARD_SCALE,
                        t=T_SOFT,
                        gap_eps2=GAP_EPS2,
                        overlap_clearance=self.overlap_clearance,
                        **extra["factors"],
                    ),
                )
            loss.sum().backward()
            opt.step()
            last = frac
        return last

    # ------------------------------------------------------------- stages
    def stage1(self):
        """K batched starts; returns (best, runner_up) stage-1 states (runner_up may be None)."""
        torch = self.torch
        K = self.inits.shape[0]
        move = torch.nn.Parameter(self.inits.clone())
        logits = torch.nn.Parameter(self.init_logits.clone())
        frozen = self.rotation_fixed.clone()
        frozen_oh = self.fixed_onehot[None].expand(K, -1, -1).clone()
        active = (self.tier == 1) & ~frozen
        self._run(1, move, logits, frozen, frozen_oh, active, {})
        with torch.no_grad():
            p = self._probs(logits, self._temps(1, 1.0, active), frozen, frozen_oh)
            f1, j1, _ = self.objective(1, self._pos(move), p, 1.0, {})
        order = sorted(range(K), key=lambda k: (float(f1[k]), k))
        states = [
            dict(
                start=k,
                F1=float(f1[k]),
                J1_soft=float(j1[k]),
                move=move[k].detach().clone(),
                logits=logits[k].detach().clone(),
            )
            for k in order[:2]
        ]
        self.start_scores = [dict(start=k, F1=float(f1[k]), J1=float(j1[k])) for k in range(K)]
        return states[0], (states[1] if len(states) > 1 else None)

    def finish(self, state, capture=True):
        """Stages 2 and 3 from a stage-1 state; returns (positions, rotations, info)."""
        torch = self.torch
        move = torch.nn.Parameter(state["move"].clone()[None])
        logits = torch.nn.Parameter(state["logits"].clone()[None])
        frozen = self.rotation_fixed.clone()
        frozen_oh = self.fixed_onehot[None].clone()
        tier1, mixed = self.tier == 1, self.mixed
        stars = {1: float(self._rigid_j(1, move, logits, frozen, frozen_oh, tier1)[0])}
        with torch.no_grad():
            frozen, frozen_oh = self._snap(logits, frozen, frozen_oh, tier1 & ~mixed)
            am = torch.argmax(logits, dim=2)
            reopen = mixed & ~frozen
            logits[0, reopen] = 0.0
            logits[0, reopen, am[0, reopen]] = PRIOR_LOGIT
            fresh = (self.tier == 2) & ~frozen
            logits[0, fresh] = 0.0
        self._run(2, move, logits, frozen, frozen_oh, reopen | fresh, stars)
        early = mixed | (self.tier == 2)
        stars[2] = float(self._rigid_j(2, move, logits, frozen, frozen_oh, early)[0])
        with torch.no_grad():
            frozen, frozen_oh = self._snap(logits, frozen, frozen_oh, early)
            late = (self.tier == 3) & ~frozen
            logits[0, late] = 0.0
        self._run(3, move, logits, frozen, frozen_oh, late, stars, capture=capture)
        stars[3] = float(self._rigid_j(3, move, logits, frozen, frozen_oh, late)[0])
        with torch.no_grad():
            pos = self._pos(move)[0]
            p = self._probs(logits, torch.full((self.n,), TEMP_LO), frozen, frozen_oh)[0]
            ai = torch.argmax(p, dim=1)
        comps = self.graph.components
        positions = {c.ref: (float(pos[i, 0]), float(pos[i, 1])) for i, c in enumerate(comps)}
        rotations = {c.ref: float(self.ANGLES[int(ai[i])]) for i, c in enumerate(comps)}
        return positions, rotations, dict(start=state["start"], stars=stars)


# ----------------------------------------------------------------- legalization


class PourChannels:
    """Channel model where a power trunk shared by two parts pours across their gap.

    Copper can pour across a gap only on a net both facing parts carry, so only
    the trunk nets common to both parts lose their escape demand. A trunk net of
    just one part (a switch-node row facing an input or output capacitor, say)
    still needs its corridor, as do gate drive, tap and signal nets. Trunk pins
    exist only on tier-1 parts, so every exempted pair is a tier-1 pair.
    ``pnr.place.channels`` is unchanged.
    """

    def __init__(self, base, roles):
        self.base = base
        trunk = {}
        for e in roles["elements"]:
            if e["kind"] == "bbox" and e["role"] == "trunk":
                for r, _ in e["pins"]:
                    trunk.setdefault(r, set()).add(e["net"])
        self.trunk = trunk

    def __getattr__(self, name):
        return getattr(self.base, name)

    def poured(self, a, b):
        """Trunk nets both parts carry: realised as a pour across the gap between them."""
        return self.trunk.get(a, set()) & self.trunk.get(b, set())

    def penalty(self, comp, others, xs, ys):
        score = np.zeros(np.broadcast_shapes(np.shape(xs), np.shape(ys)))
        for other in others:
            shared = self.poured(comp.ref, other.ref)
            for _, gap, overlap, required, nets in self.base.interactions(comp, other, xs, ys):
                if shared:
                    required = self.base.active_demand(
                        {k: v for k, v in nets.items() if k not in shared}
                    )
                shortage = np.maximum(required - gap, 0)
                score += np.where((gap >= 0) & (overlap > 0), shortage**2, 0)
        return score


class LegalizeAid:
    """Tier order and relative targets for :func:`pnr.place.legalize.legalize`.

    target_i = x_i + sum_j b_ij D_j / sum_j b_ij over already placed parts j,
    D_j = legal - continuous position of j, b_ij = sum over cost elements shared
    by i and j of w_e / (|e| - 1).
    """

    def __init__(self, roles, components):
        self.tiers = dict(roles["tier"])
        self.origin = {c.ref: tuple(c.pos) for c in components}
        bonds = {}
        for e in roles["elements"]:
            if e["kind"] == "bbox":
                refs = [r for r, _ in e["pins"]]
            elif e["kind"] == "tap":
                refs = [e["pin"][0]] + [r for r, _ in e["targets"]]
            else:
                refs = [r for m in e["members"] for r in m]
            refs = sorted(set(refs))
            if len(refs) < 2:
                continue
            w = float(e["weight"]) / (len(refs) - 1)
            for a in refs:
                for b in refs:
                    if a != b:
                        bonds.setdefault(a, {})
                        bonds[a][b] = bonds[a].get(b, 0.0) + w
        self.bonds = bonds

    def tier(self, ref):
        return self.tiers.get(ref, 3)

    def target(self, ref, placed):
        x0, y0 = self.origin[ref]
        mine = self.bonds.get(ref)
        if not mine:
            return (x0, y0)
        num_x = num_y = den = 0.0
        for c in placed:
            b = mine.get(c.ref)
            if not b or c.ref not in self.origin:
                continue
            ox, oy = self.origin[c.ref]
            num_x += b * (c.pos[0] - ox)
            num_y += b * (c.pos[1] - oy)
            den += b
        if den <= 0:
            return (x0, y0)
        return (x0 + num_x / den, y0 + num_y / den)


def loop_ratio(loops_cont, loops_legal, links, gap_floor):
    """Worst hot-loop legalization growth: max_L Lambda_L(legal) / max(Lambda_L(continuous), floor_L).

    floor_L = links_L x gap_floor, the closest a loop's links can sit when
    courtyards keep the clearance plus one legalizer cell. It keeps the ratio
    meaningful when the softmin gap Lambda is near or below zero.
    """
    worst = 0.0
    for name, cont in loops_cont.items():
        worst = max(worst, loops_legal[name] / max(cont, links[name] * gap_floor))
    return worst


def better_attempt(a, b):
    """True when legalization attempt ``a`` should be kept over ``b``.

    Lexicographic: a legalized, hard-legal pose first; then the hot-loop cost
    sum_L w_L Lambda_L, where values within EPS[0] of each other tie (the
    tolerance stage 2 gets on J1); then J1. A pure hot-loop key would trade a
    1-3% loop gain for 15% more trunk length; J1 alone, which the trunks
    dominate, keeps a legalization that doubled the switching loop.
    """
    ka, kb = (a["placed"] is None, not a["legal"]), (b["placed"] is None, not b["legal"])
    if ka != kb:
        return ka < kb
    if a["placed"] is None:
        return False
    ha, hb = a["hot_legal"], b["hot_legal"]
    if abs(ha - hb) > EPS[0] * max(abs(ha), abs(hb)):
        return ha < hb
    return a["j_legal"] < b["j_legal"]


def staged_place(
    graph,
    constraints,
    roles,
    width,
    height,
    *,
    seed,
    iters,
    orient,
    inflation,
    spread,
    channel_rules,
    initial_positions,
    initial_rotations,
    poses,
    keepouts,
    clearance,
    grid_mm,
    legalize_spread,
    mobility,
    pair_weights=None,
    pad_edge=None,
):
    """Staged global placement, power-first legalization, one bounded retry.

    Returns the legal graph. Legalization can undo a hot loop while barely
    moving J1, which the power trunks dominate, so each hot loop is checked on
    its own. Stages 2-3 plus legalization are rerun from the stage-1 runner-up
    when legalization fails, when the rigid J1 of the legal pose exceeds
    RETRY_RATIO x the continuous J1, or when any hot loop's Lambda exceeds
    RETRY_RATIO x its continuous value (:func:`loop_ratio`). The kept result is
    chosen by :func:`better_attempt`: legal, then hot-loop cost
    sum_L w_L Lambda_L at the legal pose (EPS[0] tie band), then J1.
    """
    from pnr.constraints import compile_routing_rules
    from pnr.graph import BoardGraph

    from . import metrics
    from .channels import ChannelModel
    from .geometry import hard_group_edges, hard_group_limits, resolve_hard_rotations
    from .legalize import LegalizationError, legalize

    sp = StagedPlacer(
        graph,
        constraints,
        width,
        height,
        roles,
        seed=seed,
        iters=iters,
        orient=orient,
        inflation=inflation,
        spread=spread,
        initial_positions=initial_positions,
        initial_rotations=initial_rotations,
        grid_mm=grid_mm,
        pair_weights=pair_weights,
    )
    best, runner = sp.stage1()
    compiled = sp.compiled
    links = {name: len(combos[0]) for _, name, combos, _ in compiled.loops[1]}

    def rigid(g):
        """(J1, {hot loop: Lambda}, sum_L w_L Lambda_L) at a rigid pose."""
        terms = compiled.numpy_terms(rigid_pads(g))
        j1 = sum(e["weight"] * float(e["value"][0]) for e in terms if e["stage"] == 1)
        loops = {e["name"]: float(e["value"][0]) for e in terms if e["kind"] == "loop"}
        hot = sum(e["weight"] * float(e["value"][0]) for e in terms if e["kind"] == "loop")
        return j1, loops, hot

    def attempt(state):
        positions, rotations, info = sp.finish(state)
        cont = BoardGraph.from_json(graph.to_json())
        for comp in cont.components:
            comp.pos = positions[comp.ref]
            comp.rot = rotations[comp.ref]
        channels = PourChannels(
            ChannelModel(
                cont,
                channel_rules or compile_routing_rules(constraints, [n.name for n in graph.nets]),
            ),
            roles,
        )
        j_cont, loops_cont, hot_cont = rigid(cont)
        out = dict(
            info=info,
            j_cont=j_cont,
            loops_cont=loops_cont,
            hot_cont=hot_cont,
            placed=None,
            error=None,
            legal=False,
            continuous={c.ref: [c.pos[0], c.pos[1], c.rot] for c in cont.components},
        )
        try:
            out["placed"] = legalize(
                cont,
                width,
                height,
                fixed=poses,
                allow_rotation=orient,
                channel_model=channels,
                group_limits=hard_group_limits(constraints, poses, partial=True),
                group_edges=hard_group_edges(constraints),
                rotations=resolve_hard_rotations(constraints),
                mobility=mobility,
                keepouts=keepouts,
                clearance=clearance,
                grid_mm=grid_mm,
                inflation=inflation,
                spread=legalize_spread,
                roles=roles,
                **({} if pad_edge is None else dict(pad_edge=pad_edge)),
            )
        except LegalizationError as exc:
            out["error"] = exc
            return out
        out["j_legal"], out["loops_legal"], out["hot_legal"] = rigid(out["placed"])
        out["loop_ratio"] = loop_ratio(loops_cont, out["loops_legal"], links, sp.overlap_clearance)
        out["legal"] = not any(
            metrics.hard_violations(out["placed"], constraints, clearance=0.0).values()
        )
        return out

    def summary(a):
        return dict(
            start=a["info"]["start"],
            legal=a["legal"],
            error=None if a["error"] is None else str(a["error"]),
            j1_continuous=a["j_cont"],
            j1_legal=a.get("j_legal"),
            hot_continuous=a["hot_cont"],
            hot_legal=a.get("hot_legal"),
            loop_ratio=a.get("loop_ratio"),
            loops={k: [v, (a.get("loops_legal") or {}).get(k)] for k, v in a["loops_cont"].items()},
        )

    first = attempt(best)
    chosen, attempts, reason = first, [first], None
    if first["placed"] is None:
        reason = "legalization failed"
    elif first["j_legal"] > RETRY_RATIO * first["j_cont"]:
        reason = "J1 grew %.3fx in legalization" % (first["j_legal"] / first["j_cont"])
    elif first["loop_ratio"] > RETRY_RATIO:
        reason = "hot loop grew %.3fx in legalization" % first["loop_ratio"]
    if runner is not None and reason is not None:
        second = attempt(runner)
        attempts.append(second)
        if better_attempt(second, first):
            chosen = second
    if chosen["placed"] is None:
        raise first["error"]
    placed = chosen["placed"]
    placed.power_first = dict(
        start=chosen["info"]["start"],
        stars=chosen["info"]["stars"],
        j1_continuous=chosen["j_cont"],
        j1_legal=chosen["j_legal"],
        hot_continuous=chosen["hot_cont"],
        hot_legal=chosen["hot_legal"],
        loop_ratio=chosen["loop_ratio"],
        retried=len(attempts) > 1,
        retry_reason=reason,
        attempts=[summary(a) for a in attempts],
        chosen=attempts.index(chosen),
        starts=sp.start_scores,
        steps=list(sp.steps),
        continuous=chosen["continuous"],
    )
    return placed


# ----------------------------------------------------------------- placement metrics


def _centroid(pts):
    return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))


def _mst(points):
    """Prim MST over points; returns (length, [(i, j)])."""
    n = len(points)
    if n < 2:
        return 0.0, []
    inside, best, parent, total, edges = [False] * n, [math.inf] * n, [-1] * n, 0.0, []
    best[0] = 0.0
    for _ in range(n):
        i = min((k for k in range(n) if not inside[k]), key=lambda k: (best[k], k))
        inside[i] = True
        total += best[i]
        if parent[i] >= 0:
            edges.append((parent[i], i))
        for k in range(n):
            d = math.dist(points[i], points[k])
            if not inside[k] and d < best[k]:
                best[k], parent[k] = d, i
    return total, edges


def _hull_area(points):
    """Convex-hull area (monotone chain); robust where the loop polygon self-crosses."""
    pts = sorted(set(points))
    if len(pts) < 3:
        return 0.0
    cross = lambda o, a, b: (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    hull = lower[:-1] + upper[:-1]
    return abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(hull, hull[1:] + hull[:1]))) / 2


def _crosses(a, b, c, d):
    o = lambda p, q, r: (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
    return o(a, b, c) * o(a, b, d) < 0 and o(c, d, a) * o(c, d, b) < 0


def placement_quality(graph, roles, compiled=None):
    """Placement-level power metrics at a rigid pose (recorded per trial, used for ranking).

    q_place = rigid J1; per-loop perimeter/area/gaps over carrying pad-group
    centroids (best member choice); crossings between straight MST edges of
    different power nets; series-path link gaps; stage wiring proxies; power-net
    MST; tier-1 part spread; the hard-group diagnostic.
    """
    compiled = compiled or Compiled(graph, roles)
    pp = rigid_pads(graph)
    j1, j2, j3 = (float(v[0]) for v in compiled.numpy_j(pp))
    at = {}
    k = 0
    for c in graph.components:
        for p in c.pads:
            at.setdefault((c.ref, p.name, p.net), []).append(tuple(pp[k]))
            k += 1
    carrying = roles["carrying"]

    def pads(ref, net):
        return [
            xy for name in carrying.get(ref, {}).get(net, []) for xy in at.get((ref, name, net), [])
        ]

    def gap(a, b, net):
        pa, pb = pads(a, net), pads(b, net)
        return min(math.dist(p, q) for p in pa for q in pb) if pa and pb else math.inf

    classes = roles["classes"]
    loops = []
    for l in roles["loops"]:
        members = [classes[i]["members"] for i in l["classes"]]
        nets = l["nets"]
        m = len(members)
        best = None
        choices = (
            itertools.product(*members)
            if math.prod(map(len, members)) <= 4096
            else [tuple(x[0] for x in members)]
        )
        for pick in choices:
            pts = []
            for i, ref in enumerate(pick):
                pts.append(_centroid(pads(ref, nets[i - 1])))
                pts.append(_centroid(pads(ref, nets[i])))
            per = sum(math.dist(pts[i], pts[(i + 1) % len(pts)]) for i in range(len(pts)))
            if best is None or per < best[0]:
                area = (
                    abs(
                        sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]))
                    )
                    / 2
                )
                gaps = [gap(pick[i], pick[(i + 1) % m], nets[i]) for i in range(m)]
                spans = sum(math.dist(pts[2 * i], pts[2 * i + 1]) for i in range(m))
                best = (per, area, gaps, spans, pick, _hull_area(pts), pts)
        per, area, gaps, spans, pick, hull, pts = best
        loops.append(
            dict(
                labels=l["labels"],
                nets=nets,
                hot=l["hot"],
                weight=l["weight"],
                parts=list(pick),
                perimeter_mm=per,
                area_mm2=area,
                hull_area_mm2=hull,
                gaps_mm=sum(gaps),
                link_gaps_mm=gaps,
                spans_mm=spans,
                polygon=[list(p) for p in pts],
            )
        )
    tier1 = roles["tier1"]
    power_mst, segments = 0.0, []
    for net in roles["power_nets"]:
        pts = [_centroid(pads(r, net)) for r in tier1 if pads(r, net)]
        length, edges = _mst(pts)
        power_mst += length
        segments += [(net, pts[i], pts[j]) for i, j in edges]
    crossings = sum(
        1
        for a, b in itertools.combinations(segments, 2)
        if a[0] != b[0] and _crosses(a[1], a[2], b[1], b[2])
    )
    series = None
    if roles.get("series"):
        s = roles["series"]
        chosen = []
        links = []
        for i, ci in enumerate(s["classes"]):
            cand = classes[ci]["members"]
            if not chosen:
                chosen.append(cand[0])
                continue
            net = s["nets"][i]
            ref = min(cand, key=lambda r: (gap(chosen[-1], r, net), cand.index(r)))
            links.append(dict(net=net, a=chosen[-1], b=ref, gap_mm=gap(chosen[-1], ref, net)))
            chosen.append(ref)
        series = dict(parts=chosen, links=links, gaps_mm=sum(x["gap_mm"] for x in links))
    stage_len = {2: 0.0, 3: 0.0}
    for s in (2, 3):
        for idx, w, *_ in compiled.bbox[s]:
            q = pp[idx]
            stage_len[s] += float((q.max(0) - q.min(0)).sum())
        for a, b, w, *_ in compiled.taps[s]:
            stage_len[s] += float(min(math.dist(pp[i], pp[k]) for i in a for k in b))
    centres = np.array([graph.component(r).pos for r in tier1]) if tier1 else np.zeros((0, 2))
    spread = dict(
        rms_mm=(
            float(np.sqrt(((centres - centres.mean(0)) ** 2).sum(1).mean()))
            if len(centres)
            else 0.0
        ),
        bbox_area_mm2=float(np.ptp(centres[:, 0]) * np.ptp(centres[:, 1])) if len(centres) else 0.0,
    )
    return dict(
        schema="pnr-power-quality-place-v1",
        q_place=j1,
        J1=j1,
        J2=j2,
        J3=j3,
        loops=loops,
        hot_loop_perimeter_mm=sum(l["perimeter_mm"] for l in loops if l["hot"]),
        hot_loop_area_mm2=sum(l["area_mm2"] for l in loops if l["hot"]),
        hot_loop_hull_area_mm2=sum(l["hull_area_mm2"] for l in loops if l["hot"]),
        crossings=crossings,
        power_mst_mm=power_mst,
        series=series,
        stage_len=dict(ctrl_sense=stage_len[2], passive=stage_len[3]),
        power_part_spread=spread,
        hard_group_diagnostic=roles["hard_group_diagnostic"],
    )
