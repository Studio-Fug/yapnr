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


# board-design.md R4: no digital net within 5 mm of RF copper on L1-L3 (the region holds the RF
# copper with 4 mm of GND round the cut-outs, so 5 mm beyond it keeps the rule with margin)
R4_GUARD = 5.0
# today's 46.3 mm board: the region's top (45.975) plus 0.325 mm of edge clearance [BD §5.1]
BOARD_TOP_MARGIN = 0.325


def _board_frame(mc: Macro) -> Dict[str, object]:
    """Macro extents in the board plan's frame (origin lower-left, +y north)."""
    ux, uy = U1_AT

    def bb(polys):
        xs = [q[0] + ux for pp in polys for q in pp]
        ys = [q[1] + uy for pp in polys for q in pp]
        return [min(xs), min(ys), max(xs), max(ys)]

    def rect(r):
        return [r[0] + ux, r[1] + uy, r[2] + ux, r[3] + uy]

    act = {n: c for n, c in mc.columns.items() if not c.dummy}
    rx = [pp for n, c in act.items() if n.startswith("RX") for pp in c.patches]
    tx = [pp for n, c in act.items() if n.startswith("TX") for pp in c.patches]
    rx_all = [pp for n, c in mc.columns.items() if n.startswith("RX") for pp in c.patches]
    tx_all = [pp for n, c in mc.columns.items() if n.startswith("TX") for pp in c.patches]
    pk = mc.ports["vout_pa_pocket"]["rect"]
    region = [
        mc.region["x"][0] + ux,
        mc.region["y"][0] + uy,
        mc.region["x"][1] + ux,
        mc.region["y"][1] + uy,
    ]
    pcs = {n: [c.origin[0] + ux, c.origin[1] + uy] for n, c in act.items()}
    rxn = [n for n in act if n.startswith("RX")]
    txn = [n for n in act if n.startswith("TX")]
    return dict(
        u1_at=list(U1_AT),
        phase_centres=pcs,
        dummy_phase_centres={
            n: [c.origin[0] + ux, c.origin[1] + uy] for n, c in mc.columns.items() if c.dummy
        },
        rx_patch_bbox=bb(rx),
        tx_patch_bbox=bb(tx),
        rx_patch_bbox_with_dummies=bb(rx_all),
        tx_patch_bbox_with_dummies=bb(tx_all),
        l1_cutouts={b: rect(r) for b, r in mc.cutouts.items()},
        entries_y={b: v["E"] + uy for b, v in mc.banks.items()},
        region_bbox=region,
        r4_guard_x=[region[0] - R4_GUARD, region[2] + R4_GUARD],
        board_height_min=region[3] + BOARD_TOP_MARGIN,
        rx_tx_nearest_phase_centres=min(
            ((pcs[a][0] - pcs[b][0]) ** 2 + (pcs[a][1] - pcs[b][1]) ** 2) ** 0.5
            for a in rxn
            for b in txn
        ),
        dummy_loads={
            ld.ref: dict(
                column=n,
                centre=[ld.centre[0] + ux, ld.centre[1] + uy],
                axis="y",
                # no via but the load's own five in the via zone; solder mask over the island
                via_zone=[round(v + o, 4) for v, o in zip(ld.via_zone, (ux, uy, ux, uy))],
                mask_island=[round(v + o, 4) for v, o in zip(ld.mask, (ux, uy, ux, uy))],
            )
            for n, ld in mc.loads.items()
        },
        vout_pa_pocket=None if pk is None else [pk[0] + ux, pk[1] + uy, pk[2] + ux, pk[3] + uy],
        # D14: the macro's PA feed, for merge_macro (stage 3b hand-off)
        pa_feed=None if mc.pa is None else _pa_frame(mc, ux, uy),
        options=dict(
            d15=mc.params["d15"],
            dummies=mc.params["dummies"],
            dummy_term=mc.params["dummy_term"],
            l23_cavity=mc.params["l23_cavity"],
            column=mc.params["column"],
            tx_eq=mc.params["tx_eq"],
        ),
        # In2.Cu GND the macro owns (its L3 reference): a keepout for the BGA fanout (E4) and
        # the escape probe, which lose In2 there (review 2026-10-03)
        in2_gnd_polygons=[[[q[0] + ux, q[1] + uy] for q in poly] for poly in mc.l3_gnd],
    )


def _pa_frame(mc: Macro, ux: float, uy: float) -> Dict[str, object]:
    pa = mc.pa
    clr = float(mc.params["pa_clear"])

    def r(q):
        return [round(q[0] + ux, 4), round(q[1] + uy, 4), round(q[2] + ux, 4), round(q[3] + uy, 4)]

    return dict(
        net=pa.net,
        skip_pads=sorted(pa.balls),  # the fanout leaves A2/B2 to the macro
        vias=[[round(c[0] + ux, 4), round(c[1] + uy, 4)] for c in pa.vias],
        via_drill_pad=[pa.drill, pa.pad],
        l1_copper=[r(q) for q in pa.rects],
        pocket=r(pa.pocket),
        mask_island=r(pa.mask_island),
        antipad_radius_l2_l3_l5=round(pa.pad / 2 + clr, 4),
        l4_tie=(
            "In3.Cu 1V0_PA pour over the vias (integration); the macro preview draws the pocket "
            "+ 0.5 mm as a stand-in"
        ),
        bottom_caps="VOUT_PA caps on B.Cu at the vias, pads by the integration's decoupling audit",
    )


# Not a sign-off: the column frozen here fails ANT-02's return loss at P1 (col-c, openEMS
# 2026-10-03: RL 5.9 / 9.1 / 11.4 dB at 60.3 / 62.05 / 63.8 GHz, best match at 64.0 GHz), the L
# x0.967 calibration came from one patch, and the bank (C2) is not solved.
STATUS = (
    "placeholder: geometry for integration only; the column's match is not converged and sits "
    "high (rf-uniform: RL-10 64.35-66 GHz at 15 um against 60.3-63.8); stage 3b's C1 retune, the "
    "D15 pour and S1/S2 choice, the L2-L3 cavity option and the PA-feed EM come before it is "
    "frozen"
)


def record(mc: Macro) -> Dict[str, object]:
    from .rules import outside_digest

    d = mc.dims
    a50 = d["lines"]["alpha50_gcpw_db_mm"] or d["lines"]["alpha50_db_mm"]
    feeds = {}
    for n, f in mc.feeds.items():
        lp0 = f.length - f.marks["P0"][1]
        feeds[n] = dict(
            ball_to_p1_mm=f.length,
            p0_to_p1_mm=lp0,
            p0_to_pg_mm=f.marks["Pg"][1] - f.marks["P0"][1],
            p0_to_p1_line_loss_db=None if a50 is None else lp0 * a50,
            ball_to_p0_mm=f.marks["P0"][1],
        )
    cols = {
        n: dict(
            phase_centre=list(c.origin),
            p1=list(c.p1),
            input_side="west" if c.mirror else "east",
            dummy=c.dummy,
            arms=c.arm_lengths,
        )
        for n, c in mc.columns.items()
    }
    geo = json.dumps(
        _r(
            dict(
                feeds={n: [s.p0 for s in f.segs] for n, f in mc.feeds.items()},
                runins={n: [s.p0 for s in f.segs] for n, f in mc.runins.items()},
                cols=cols,
                vias=mc.vias,
                unstitched=mc.unstitched,
            ),
            5,
        ),
        sort_keys=True,
    ).encode()
    counts: Dict[str, int] = {}
    for v in mc.vias:
        counts[v[3]] = counts.get(v[3], 0) + 1
    return _r(
        dict(
            params={k: v for k, v in mc.params.items()},
            dims=d,
            feeds=feeds,
            columns=cols,
            loads={
                n: dict(ref=ld.ref, centre=list(ld.centre), vias=[list(q) for q in ld.vias])
                for n, ld in mc.loads.items()
            },
            cutouts_u1_mm={b: list(r) for b, r in mc.cutouts.items()},
            banks={b: {k: v for k, v in bk.items()} for b, bk in mc.banks.items()},
            fit=mc.fit,
            ports=mc.ports,
            region_u1_mm=mc.region,
            status=STATUS,
            vias=dict(counts, total=len(mc.vias)),
            checks=mc.checks,
            geometry_sha256=hashlib.sha256(geo).hexdigest(),
            outside_cutouts_sha256=outside_digest(mc),
            board_frame=_board_frame(mc),
        )
    )
