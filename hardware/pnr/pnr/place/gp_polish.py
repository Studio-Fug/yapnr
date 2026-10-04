"""Global-placement polish: GP and the legalizer agree on spacing (``PNR_GP_POLISH=1``,
``PNR_GP_CHANNELS=<lambda>``; default off).

Design: docs/design/compact-placement.md section 11 (``A``). :func:`pnr.place.model.global_place`
runs :data:`pnr.legalize_flags.POLISH_STEPS` more iterations of its own loop after the main
ones, so every loss term (wirelength, regions, aligns, groups, keep-outs, edge aligns, planes,
matched lengths, side terms) stays active. During them:

- **Turns and free sides are frozen** at the arg-max the placer returns (one-hot), so every
  footprint is exact.
- **The overlap term uses the legalizer's own slot** (:class:`Polish`): per frozen turn and side
  ``ceil((w * s + clearance + 2 m) / g) * g`` per axis, centred where
  :func:`pnr.place.legalize.legalize` centres it (the occupied box centre, offset courtyards
  included), with the spreading factor ``s``, ``clearance``, copper margin ``m`` and grid ``g``
  exactly as the legalizer gets them. Slots of whole cells that do not overlap in the plane
  stay disjoint after the legalizer's nearest-cell snapping, so the grid no longer turns a
  tight packing into overlaps. The outline and keep-out terms use the same slot, and with the
  pad-edge rule the slot centre is held inside :func:`pnr.place.legalize.pad_edge_box`.
- **With** ``PNR_GP_CHANNELS`` **the legalizer's channel cost joins, made smooth**:
  ``lambda / 2 * sum shortage^2`` over ordered pairs and facing directions whose pad envelopes
  face each other (gap >= 0, projections overlapping), the required gap being
  :meth:`pnr.place.channels.ChannelModel.active_demand` over the facing rows' nets that reach a
  third part (pair-local nets reserve nothing), precomputed once for the frozen turns.
- **Schedule:** the overlap weight ramps geometrically from 1 to :data:`OVERLAP_RAMP`, and a fresh
  Adam (positions only) decays its step exponentially from :data:`LR_START` to :data:`LR_END` mm.

Fixed step counts, no random draws: deterministic per platform like the rest of the global stage.
Not with hull macros (``PNR_MACRO_HULL=1``: the switch is ignored there) or power-first placement
(which never calls it).
"""

from __future__ import annotations

import copy
import math
from typing import Dict, Optional, Tuple

import torch

from pnr.legalize_flags import POLISH_STEPS

# Adam's step (mm) at the first and the last polish iteration (exponential decay in between).
LR_START = 0.3
LR_END = 0.005
# The overlap weight grows geometrically from 1 to this over the polish.
OVERLAP_RAMP = 100.0


class Polish:
    """The legalizer settings the polish phase reproduces, and its channel weight.

    ``clearance``, ``grid_mm``, ``spread`` (the legalizer's floor, ``min(spread, 1.3)``),
    ``inflation`` and ``margins`` are the arguments :func:`pnr.place.legalize.legalize` gets;
    ``pad_edge`` the pad-edge rule (None = off); ``channel_weight`` lambda (None = no channel
    term) with ``rules`` the routing rules the :class:`pnr.place.channels.ChannelModel` reads."""

    def __init__(
        self,
        *,
        clearance: float,
        grid_mm: float,
        spread: float = 1.0,
        inflation: Optional[Dict[str, float]] = None,
        margins: Optional[Dict[str, float]] = None,
        pad_edge: Optional[Tuple[float, float]] = None,
        channel_weight: Optional[float] = None,
        rules: Optional[dict] = None,
        steps: int = POLISH_STEPS,
    ):
        self.clearance = float(clearance)
        self.grid = float(grid_mm)
        self.spread = float(spread)
        self.inflation = dict(inflation or {})
        self.margins = dict(margins or {})
        self.pad_edge = pad_edge
        self.channel_weight = channel_weight
        self.rules = rules
        self.steps = int(steps)
        if channel_weight is not None and rules is None:
            raise ValueError("the polish channel term needs the routing rules")

    def lr(self, frac: float) -> float:
        """Adam's step (mm) at ``frac`` (0 at the first polish iteration, 1 at the last)."""
        return LR_START * (LR_END / LR_START) ** frac

    def ramp(self, frac: float) -> float:
        """The overlap weight at ``frac``."""
        return OVERLAP_RAMP**frac

    def slot_cells(self, comp, rect) -> Tuple[int, int]:
        """The legalizer's slot (cells) of ``comp`` whose occupied box at its turn is ``rect``
        (``legalize.slot_dims`` with the legalizer's spreading factor)."""
        infl = max(1.0, self.spread, float(self.inflation.get(comp.ref, 1.0)))
        g, c = self.grid, self.clearance
        m = self.margins.get(comp.ref)
        if m:
            return (
                int(math.ceil((rect.w * infl + c + 2 * m) / g)),
                int(math.ceil((rect.h * infl + c + 2 * m) / g)),
            )
        return (int(math.ceil((rect.w * infl + c) / g)), int(math.ceil((rect.h * infl + c) / g)))

    def prepare(self, graph, angles, sides, width: float, height: float):
        """Constant tensors of the polish for the frozen ``angles`` (degrees) and ``sides`` of
        ``graph``'s components (in order): a dict of ``centre`` (n, 2) slot-centre offsets from the
        origin, ``half`` (n, 2) slot half sizes, ``pad_box`` (n, 4) pad-edge bounds on the
        origin (x_lo, x_hi, y_lo, y_hi; None without the rule), and with the channel term
        ``env`` (n, 4) pad envelopes about the origin (left, right, bottom, top) and ``need``
        (n, n, 4) required gaps (west, east, south, north of the first part)."""
        from .geometry import courtyard_rect, set_component_side
        from .legalize import pad_edge_box

        posed = []
        for comp, angle, side in zip(graph.components, angles, sides):
            t = copy.deepcopy(comp)
            if t.side != side:
                set_component_side(t, side)
            t.pos, t.rot = (0.0, 0.0), float(angle)
            posed.append(t)
        centre, half, boxes = [], [], []
        for t in posed:
            rect = courtyard_rect(t)
            bw, bh = self.slot_cells(t, rect)
            centre.append((rect.cx, rect.cy))
            half.append((bw * self.grid / 2.0, bh * self.grid / 2.0))
            if self.pad_edge is not None:
                boxes.append(pad_edge_box(t, self.pad_edge, width, height))
        out = dict(
            centre=torch.tensor(centre, dtype=torch.float32),
            half=torch.tensor(half, dtype=torch.float32),
            pad_box=None,
            env=None,
            need=None,
        )
        if self.pad_edge is not None:
            # An unbounded side (a part without sized pads) stays unbounded.
            out["pad_box"] = torch.tensor(
                [
                    [max(-1e9, v) if k % 2 == 0 else min(1e9, v) for k, v in enumerate(b)]
                    for b in boxes
                ],
                dtype=torch.float32,
            )
        if self.channel_weight is not None:
            out["env"], out["need"] = self._channels(graph, posed)
        return out

    def _channels(self, graph, posed):
        """``(env, need)`` of :meth:`prepare` from the legalizer's channel model."""
        from .channels import ChannelModel

        n = len(posed)
        model = ChannelModel(graph, self.rules)
        shapes = [model.shape(t) for t in posed]
        env = []
        for t, shape in zip(posed, shapes):
            if shape is None:
                from .geometry import courtyard_rect

                r = courtyard_rect(t)
                env.append((r.left, r.right, r.bottom, r.top))
            else:
                env.append(tuple(shape[:4]))
        need = torch.zeros((n, n, 4), dtype=torch.float32)
        opposite = (1, 0, 3, 2)
        for i in range(n):
            if shapes[i] is None:
                continue
            for j in range(n):
                if i == j or shapes[j] is None:
                    continue
                pair = {posed[i].ref, posed[j].ref}
                for f in range(4):
                    nets = {}
                    for row in (shapes[i][4][f], shapes[j][4][opposite[f]]):
                        for net in row:
                            if model.refs.get(net, set()) - pair:
                                nets[net] = True
                    if nets:
                        need[i, j, f] = float(model.active_demand(nets))
        return torch.tensor(env, dtype=torch.float32), need


def slot_overlap(pos, frozen, mask):
    """Summed overlap area of the slots (upper triangle, ``mask`` the same-side weights)."""
    body = pos + frozen["centre"]
    half = frozen["half"]
    dx = (body[:, 0].unsqueeze(1) - body[:, 0].unsqueeze(0)).abs()
    dy = (body[:, 1].unsqueeze(1) - body[:, 1].unsqueeze(0)).abs()
    ox = torch.clamp(half[:, 0].unsqueeze(1) + half[:, 0].unsqueeze(0) - dx, min=0.0)
    oy = torch.clamp(half[:, 1].unsqueeze(1) + half[:, 1].unsqueeze(0) - dy, min=0.0)
    return torch.triu(ox * oy * mask, diagonal=1).sum()


def slot_bound(pos, frozen, width, height, movable_f):
    """The outline term on the slots, plus the pad-edge box when the rule is on."""
    body = pos + frozen["centre"]
    hw, hh = frozen["half"][:, 0], frozen["half"][:, 1]
    cx, cy = body[:, 0], body[:, 1]
    bound = (
        torch.clamp(hw - cx, min=0.0) ** 2
        + torch.clamp(cx + hw - width, min=0.0) ** 2
        + torch.clamp(hh - cy, min=0.0) ** 2
        + torch.clamp(cy + hh - height, min=0.0) ** 2
    )
    box = frozen["pad_box"]
    if box is not None:
        x, y = pos[:, 0], pos[:, 1]
        bound = (
            bound
            + torch.clamp(box[:, 0] - x, min=0.0) ** 2
            + torch.clamp(x - box[:, 1], min=0.0) ** 2
            + torch.clamp(box[:, 2] - y, min=0.0) ** 2
            + torch.clamp(y - box[:, 3], min=0.0) ** 2
        )
    return (bound * movable_f).sum()


def slot_keepout(pos, frozen, keep_t, movable_f):
    """Overlap of the slots with the keep-outs (the legalizer marks keep-outs as they are)."""
    body = pos + frozen["centre"]
    hw, hh = frozen["half"][:, 0], frozen["half"][:, 1]
    kdx = (body[:, 0].unsqueeze(1) - keep_t[:, 0].unsqueeze(0)).abs()
    kdy = (body[:, 1].unsqueeze(1) - keep_t[:, 1].unsqueeze(0)).abs()
    kox = torch.clamp(hw.unsqueeze(1) + keep_t[:, 2].unsqueeze(0) - kdx, min=0.0)
    koy = torch.clamp(hh.unsqueeze(1) + keep_t[:, 3].unsqueeze(0) - kdy, min=0.0)
    return ((kox * koy) * movable_f.unsqueeze(1)).sum()


def channel_shortage(pos, frozen, mask):
    """``sum shortage^2`` over ordered pairs (i, j) and the four directions in which part j's
    pad envelope faces part i's (gap >= 0, projections overlapping) and the facing rows need a
    gap (``ChannelModel.penalty``'s definition, counted from both ends)."""
    env, need = frozen["env"], frozen["need"]
    dx = pos[:, 0].unsqueeze(0) - pos[:, 0].unsqueeze(1)  # [i, j] = x_j - x_i
    dy = pos[:, 1].unsqueeze(0) - pos[:, 1].unsqueeze(1)
    l_i, r_i = env[:, 0].unsqueeze(1), env[:, 1].unsqueeze(1)
    b_i, t_i = env[:, 2].unsqueeze(1), env[:, 3].unsqueeze(1)
    l_j, r_j = env[:, 0].unsqueeze(0), env[:, 1].unsqueeze(0)
    b_j, t_j = env[:, 2].unsqueeze(0), env[:, 3].unsqueeze(0)
    yover = torch.minimum(t_i, dy + t_j) - torch.maximum(b_i, dy + b_j)
    xover = torch.minimum(r_i, dx + r_j) - torch.maximum(l_i, dx + l_j)
    total = pos.new_zeros(())
    for gap, over, f in (
        (dx + l_j - r_i, yover, 1),  # j east of i
        (-dx + l_i - r_j, yover, 0),  # j west of i
        (dy + b_j - t_i, xover, 3),  # j north of i
        (-dy + b_i - t_j, xover, 2),  # j south of i
    ):
        short = torch.clamp(need[:, :, f] - gap, min=0.0)
        live = (gap >= 0) & (over > 0) & (need[:, :, f] > 0)
        total = total + (torch.where(live, short**2, torch.zeros_like(short)) * mask).sum()
    return total
