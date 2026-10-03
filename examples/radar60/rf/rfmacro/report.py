"""Machine-readable record of one macro build (dimensions, ports, lengths, checks, loss)."""

from __future__ import annotations

import hashlib
import json
from typing import Dict

from .macro import Macro


def _r(x, n=4):
    if isinstance(x, float):
        return round(x, n)
    if isinstance(x, dict):
        return {k: _r(v, n) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_r(v, n) for v in x]
    return x


U1_AT = (26.0, 28.0)  # board-design.md §5.1: U1 at (26.0, 28.0), rot 270 (RX edge north)


def _board_frame(mc: Macro) -> Dict[str, object]:
    """Macro extents in the board plan's frame (origin lower-left, +y north)."""

    def bb(polys):
        xs = [q[0] + U1_AT[0] for pp in polys for q in pp]
        ys = [q[1] + U1_AT[1] for pp in polys for q in pp]
        return [min(xs), min(ys), max(xs), max(ys)]

    rx = [pp for n, c in mc.columns.items() if n.startswith("RX") for pp in c.patches]
    tx = [pp for n, c in mc.columns.items() if n.startswith("TX") for pp in c.patches]
    pk = mc.ports["vout_pa_pocket"]["rect"]
    return dict(
        u1_at=list(U1_AT),
        rx_patch_bbox=bb(rx),
        tx_patch_bbox=bb(tx),
        region_bbox=[
            mc.region["x"][0] + U1_AT[0],
            mc.region["y"][0] + U1_AT[1],
            mc.region["x"][1] + U1_AT[0],
            mc.region["y"][1] + U1_AT[1],
        ],
        vout_pa_pocket=[pk[0] + U1_AT[0], pk[1] + U1_AT[1], pk[2] + U1_AT[0], pk[3] + U1_AT[1]],
        phase_centres={
            n: [c.origin[0] + U1_AT[0], c.origin[1] + U1_AT[1]] for n, c in mc.columns.items()
        },
    )


def record(mc: Macro) -> Dict[str, object]:
    d = mc.dims
    a50 = d["lines"]["alpha50_gcpw_db_mm"] or d["lines"]["alpha50_db_mm"]
    feeds = {}
    for n, f in mc.feeds.items():
        lp0 = f.length - f.marks["P0"][1]
        feeds[n] = dict(
            ball_to_p1_mm=f.length,
            p0_to_p1_mm=lp0,
            p0_to_p1_line_loss_db=None if a50 is None else lp0 * a50,
            ball_to_p0_mm=f.marks["P0"][1],
        )
    cols = {
        n: dict(
            phase_centre=list(c.origin),
            p1=list(c.p1),
            input_side="west" if c.mirror else "east",
            arms=c.arm_lengths,
        )
        for n, c in mc.columns.items()
    }
    geo = json.dumps(
        _r(
            dict(
                feeds={n: [s.p0 for s in f.segs] for n, f in mc.feeds.items()},
                cols=cols,
                vias=mc.vias,
            ),
            5,
        ),
        sort_keys=True,
    ).encode()
    return _r(
        dict(
            params={k: v for k, v in mc.params.items()},
            dims=d,
            feeds=feeds,
            columns=cols,
            ports=mc.ports,
            region_u1_mm=mc.region,
            vias=dict(
                fence=sum(1 for v in mc.vias if v[3] == "fence"),
                launch=sum(1 for v in mc.vias if v[3] == "launch"),
                isolation=sum(1 for v in mc.vias if v[3] == "isolation"),
            ),
            checks=mc.checks,
            geometry_sha256=hashlib.sha256(geo).hexdigest(),
            board_frame=_board_frame(mc),
        )
    )
