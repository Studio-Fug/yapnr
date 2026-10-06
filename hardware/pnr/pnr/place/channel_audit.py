"""Measure :class:`pnr.place.channels.ChannelModel` against a routed board.

For every pair of facing pad rows the model prices (:meth:`ChannelModel.interactions`
with ``gap >= 0`` and a positive overlap), the channel is the rectangle between the two
pad envelopes over their overlap. The routed copper that really runs *along* that
channel is what the model's demand stands for: the tracks on the parts' own surface
layer that cross a cut taken across the gap (perpendicular to the faces). The
busiest cut over the channel's length gives the tracks that were actually stacked
side by side there; tracks that merely cross the channel straight from one row to
the other do not run along it and are not counted. Vias inside the channel are
counted the same way (a via is a short lengthwise obstacle of its own diameter).

``used_mm`` prices the busiest cut exactly as the model prices its own demand
(:meth:`ChannelModel.active_demand`: widths plus one clearance per gap), so
``required_mm / used_mm`` is the model's conservatism on that channel. For each net
the model put on the channel, the audit also records whether it stayed on the
surface through the channel, dropped through a via near its pad on that face, or
did neither (it left by another face or crossed straight over).

The audit is read-only: it never changes the graph, the rules or the routes.
"""

from __future__ import annotations

import math
from typing import Dict, Iterable, List, Sequence

from .channels import signal_layers  # noqa: F401  (re-exported for the audit script)
from .geometry import occupied_sides, pad_rects

# A pad's drop via is one whose centre lies within this distance (mm) of the pad's
# copper (a dogbone neck or a via in pad).
DROP_RADIUS_MM = 1.0
# Lengthwise extent (mm) below which a clipped track segment is a crossing, not a run
# along the channel.
_RUN_EPS = 1e-3


def surface_layer(side: str) -> str:
    return "B.Cu" if side == "bottom" else "F.Cu"


def _clip(p0, p1, x0, x1, y0, y1):
    """Liang-Barsky: the part of segment p0-p1 inside the box, or None."""
    (ax, ay), (bx, by) = p0, p1
    dx, dy = bx - ax, by - ay
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, ax - x0), (dx, x1 - ax), (-dy, ay - y0), (dy, y1 - ay)):
        if abs(p) < 1e-12:
            if q < 0:
                return None
            continue
        t = q / p
        if p < 0:
            t0 = max(t0, t)
        else:
            t1 = min(t1, t)
        if t0 > t1:
            return None
    return (ax + t0 * dx, ay + t0 * dy), (ax + t1 * dx, ay + t1 * dy)


def _busiest(intervals):
    """``intervals``: [(lo, hi, key, width)]. The cut position covered by the most
    distinct keys, and those keys with their widest width."""
    points = sorted({p for lo, hi, _, _ in intervals for p in (lo, hi)})
    best: Dict[str, float] = {}
    for a, b in zip(points, points[1:]):
        mid = (a + b) / 2
        here: Dict[str, float] = {}
        for lo, hi, key, width in intervals:
            if lo <= mid <= hi:
                here[key] = max(here.get(key, 0.0), width)
        if len(here) > len(best) or (
            len(here) == len(best) and sum(here.values()) > sum(best.values())
        ):
            best = here
    return best


def channel_box(model, comp, other, direction):
    """(x0, x1, y0, y1) of the channel between ``comp`` and ``other`` facing
    ``direction`` (one of the labels :meth:`ChannelModel.interactions` yields)."""
    a, b = model.shape(comp), model.shape(other)
    x, y = comp.pos
    ox, oy = other.pos
    ax0, ax1, ay0, ay1 = x + a[0], x + a[1], y + a[2], y + a[3]
    bx0, bx1, by0, by1 = ox + b[0], ox + b[1], oy + b[2], oy + b[3]
    ylo, yhi = max(ay0, by0), min(ay1, by1)
    xlo, xhi = max(ax0, bx0), min(ax1, bx1)
    return {
        "east": (ax1, bx0, ylo, yhi),
        "west": (bx1, ax0, ylo, yhi),
        "north": (xlo, xhi, ay1, by0),
        "south": (xlo, xhi, by1, ay0),
    }[direction]


def _drop_vias(comp, other, nets, vias, radius):
    """{net: True} for nets with a via within ``radius`` of one of its pads on
    ``comp`` or ``other``."""
    out = {}
    pads = [(net, r) for c in (comp, other) for _, net, r in pad_rects(c) if net in nets]
    for net, r in pads:
        for via in vias:
            if via[0] != net:
                continue
            vx, vy = float(via[1]), float(via[2])
            dx = max(r.left - vx, 0.0, vx - r.right)
            dy = max(r.bottom - vy, 0.0, vy - r.top)
            if math.hypot(dx, dy) <= radius:
                out[net] = True
                break
    return out


def audit(
    graph,
    model,
    tracks: Sequence,
    vias: Sequence = (),
    *,
    drop_radius: float = DROP_RADIUS_MM,
) -> List[dict]:
    """One record per facing channel ``model`` prices on ``graph``'s placement.

    ``tracks``: ``[net, layer, (x0, y0), (x1, y1), width]`` (``routes.json``);
    ``vias``: ``[net, x, y, ...]``. Records carry the predicted demand and nets, the
    gap, the measured busiest cut (``used_tracks``, ``used_vias``, ``used_mm``,
    ``used_nets``) and the fate of each predicted net (``surface``, ``dropped``,
    ``elsewhere``)."""
    from itertools import combinations

    by_layer: Dict[str, list] = {}
    for net, layer, p0, p1, width in (t[:5] for t in tracks):
        by_layer.setdefault(layer, []).append((net, tuple(p0), tuple(p1), float(width)))
    via_d = model.via
    out = []
    for comp, other in combinations(graph.components, 2):
        shared = set(occupied_sides(comp)) & set(occupied_sides(other))
        for direction, gap, overlap, required, nets in model.interactions(comp, other):
            if not (gap >= 0 and overlap > 0):
                continue
            predicted = sorted(n for n, active in nets.items() if bool(active))
            x0, x1, y0, y1 = channel_box(model, comp, other, direction)
            lengthwise = 1 if direction in ("east", "west") else 0
            intervals = []
            for side in sorted(shared):
                for net, p0, p1, width in by_layer.get(surface_layer(side), ()):
                    clipped = _clip(p0, p1, x0, x1, y0, y1)
                    if clipped is None:
                        continue
                    lo, hi = sorted((clipped[0][lengthwise], clipped[1][lengthwise]))
                    if hi - lo > _RUN_EPS:
                        intervals.append((lo, hi, "t:" + net, width))
            for via in vias:
                vx, vy = float(via[1]), float(via[2])
                if x0 <= vx <= x1 and y0 <= vy <= y1:
                    c = (vx, vy)[lengthwise]
                    intervals.append(
                        (c - via_d / 2, c + via_d / 2, "v:%s@%.3f,%.3f" % (via[0], vx, vy), via_d)
                    )
            busiest = _busiest(intervals)
            used = {}
            for key, width in busiest.items():
                kind, name = key.split(":", 1)
                used[key] = (kind, name.split("@")[0], width)
            used_mm = _price(model, used.values())
            surface_nets = {name for kind, name, _ in used.values() if kind == "t"}
            run_nets = {key[2:] for _, _, key, _ in intervals if key.startswith("t:")}
            drops = _drop_vias(comp, other, set(predicted), vias, drop_radius)
            fate = {}
            for net in predicted:
                if net in run_nets:
                    fate[net] = "surface"
                elif drops.get(net):
                    fate[net] = "dropped"
                else:
                    fate[net] = "elsewhere"
            out.append(
                dict(
                    refs=[comp.ref, other.ref],
                    direction=direction,
                    gap_mm=float(gap),
                    overlap_mm=float(overlap),
                    required_mm=float(required),
                    predicted_nets=predicted,
                    predicted_short=bool(required - gap > 1e-6),
                    used_tracks=sum(1 for kind, _, _ in used.values() if kind == "t"),
                    used_vias=sum(1 for kind, _, _ in used.values() if kind == "v"),
                    used_mm=float(used_mm),
                    used_nets=sorted(surface_nets),
                    fate=fate,
                )
            )
    return out


def _price(model, used: Iterable) -> float:
    """The model's own pricing of a cut: widths plus one clearance per gap, at the
    widest clearance present (0 for an empty cut)."""
    used = list(used)
    if not used:
        return 0.0
    clearance = max(
        model.classes.get(name, (model.width, model.clearance, False))[1] for _, name, _ in used
    )
    return sum(width for _, _, width in used) + (len(used) + 1) * clearance


def summarize(records: Sequence[dict]) -> dict:
    """Totals over audit records: channels, predicted-short channels, predicted vs
    used mm and the fate of predicted nets."""
    fates = {"surface": 0, "dropped": 0, "elsewhere": 0}
    for record in records:
        for fate in record["fate"].values():
            fates[fate] += 1
    priced = [r for r in records if r["predicted_nets"]]
    required = sum(r["required_mm"] for r in priced)
    used = sum(r["used_mm"] for r in priced)
    return dict(
        channels=len(records),
        priced=len(priced),
        predicted_short=sum(r["predicted_short"] for r in records),
        required_mm=required,
        used_mm=used,
        used_tracks=sum(r["used_tracks"] for r in priced),
        predicted_tracks=sum(len(r["predicted_nets"]) for r in priced),
        over_predicted=sum(1 for r in priced if r["required_mm"] > r["used_mm"] + 1e-6),
        under_predicted=sum(1 for r in priced if r["used_mm"] > r["required_mm"] + 1e-6),
        fates=fates,
    )
