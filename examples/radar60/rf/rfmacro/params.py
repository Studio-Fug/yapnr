"""Declared parameters of the Board A RF macro: stackup, rules, lattice and start values.

Every value carries its source: [BD] the radar60 board plan (board-design.md §4-6, its review
§14 and board-calc), [TI-DS] TI SWRS219F (IWR6843 data sheet, rev. April 2025), [TI-RF] TI
SPRACG5 (mmWave RF PCB design guide, May 2018), [R-4835] Rogers RO4835 data sheet (92-160),
[R-4450] Rogers RO4400 bondply data sheet, [PW] PCBWay capability pages, or [D] derived here.
The `variant` parameter is the D12 frequency bracketing: patch lengths scaled by 1 + 0.018·k,
k in {-1, 0, +1} (board-design.md §0.1 D12, §14.4 R1); nothing else changes between variants.
"""

from __future__ import annotations

import copy
import math
from typing import Dict

F_LO, F_HI = 60.3e9, 63.8e9  # ANT-02 band [BD]
F0 = 0.5 * (F_LO + F_HI)  # 62.05 GHz
LAM0 = 299792458.0 / F0 * 1e3  # mm

STACK = {
    # L1: 0.5 oz LoPro foil plated to <= 35 um finished, immersion silver, no mask over RF [BD §4.1]
    "t_l1": 0.035,
    "rq_l1_um": 0.4,  # RO4835 LoPro reverse-treated foil, Rq ~0.4 um [BD], [R-4835]
    # RO4835 LoPro core 4 mil: process Dk 3.33 (4 mil) / 3.48, design Dk 3.66 [R-4835];
    # 3.56 is the board plan's mid value, +-0.10 the corner [BD review_calc §2]
    "h_core": 0.1016,
    "dk_core": 3.56,
    "df_core": 0.0037,
    "t_l2": 0.0175,  # 0.5 oz [BD §4.1]
    # RO4450F bondply 4 mil, Dk 3.52, Df 0.004 [R-4450]; pressed 90-102 um and it fills the
    # removed L2 copper under a window [BD §4.1 review]
    "h_bond": 0.096,
    "dk_bond": 3.52,
    "df_bond": 0.004,
}
STACK["h_window"] = STACK["h_core"] + STACK["t_l2"] + STACK["h_bond"]  # 0.2151 mm [BD §4.1]

CORNERS = {  # RF-06 fab corners [BD §6.2]
    "dk_core": (-0.10, +0.10),
    "edge_bias": (-0.0125, +0.0125),  # per edge, mm (+-1 mil width class, TI LEVM fab note)
    "h_core_rel": (-0.175, +0.175),  # +-0.7 mil [R-4835]
    "h_bond_rel": (-0.10, +0.10),
}

RULES = {  # pcbway-adv-6l-rf draft [BD §4.2]
    "track_min": 0.10,
    "space_min": 0.10,
    # drill, pad: 0.32 mm keeps the advanced 3 mil (0.076 mm) via ring of pcbway-adv-6l-rf; the
    # plan's 0.30 pad is a 2.95 mil ring (board track, stage 2; integration DRC 2026-10-03)
    "via_fence": (0.15, 0.32),
    "via_bga": (0.15, 0.35),  # interstitial GND vias
    # via centre spacing: 11 mil (0.2794 mm) hole to hole [PW] + one 0.15 drill = 0.43 mm
    "fence_pitch_min": 0.43,
    "edge_clear": 0.30,
}

# U1 frame (mm): U1 centre at the origin, rotated so the RX edge faces north and the TX edge
# east [BD §5.1, §5.3]. Ball pitch 0.65 mm, 10.4 mm body [TI-DS §6, ABL0161].
PKG = {"pitch": 0.65, "body": 10.4, "land": 0.32, "mask_open": 0.42}
RF_BALLS = {  # SWRS219F §6.2.2 [TI-DS]
    "TX1": "B4",
    "TX2": "B6",
    "TX3": "B8",
    "RX1": "M2",
    "RX2": "K2",
    "RX3": "H2",
    "RX4": "F2",
}

D_LATTICE = 2.342  # ANT-01 column pitch [BD §0]

DEFAULTS: Dict[str, object] = {
    "variant": 0,  # D12 bracketing: -1, 0, +1
    "bracket_step": 0.018,
    "mirror_x": False,  # ball map view; must equal the part-cache footprint (BD §7.2 review)
    # 50-ohm GCPW on 4 mil over L2 (w, g); w from the 2D solver at g = 0.20 [D], fences 0.15/0.30
    "w50": 0.200,
    "gap": 0.200,
    "fence_offset": 0.50,  # via centre from the line centre: w/2 + g + pad r + 0.05 [D]
    "fence_pitch": 0.45,  # >= 0.40 [BD §4.2]; 0.45 leaves lambda_g/10 openings [BD §14]
    # launch (RFS-1): land, L1 anti-pad and L2 cut-out radii, interstitial GND vias [TI-RF Fig. 2]
    "antipad_r": 0.30,
    "l2_cut_r": 0.38,
    "l3_cut_margin": 0.30,  # In2.Cu GND beyond each L2 cut-out (review 2026-10-03) [D]
    "launch_len": 1.30,  # land centre to P0 (TI: 50 ohm about 1.3 mm from the ball) [TI-RF §2.1.2]
    # column (RFS-4): patch on the L2 window, corporate divider on 4 mil over L2
    "patch_w": 1.45,  # W/L >= 1.2 and 0.2 mm from the gap line to the window [BD §14.4 R2]
    "patch_l": None,  # None: closed form for F0 on the window (set by dims)
    # Inset and drawn length from the first openEMS calibration (stage-2 C0a, 2026-10-03): the
    # closed-form patch (L 1.190, inset 0.445) resonated at 60.0 GHz with |S11| >= -8.4 dB; the
    # mechanical pick of the single-patch sweep L x0.967, inset {0.30, 0.38, 0.45} by |S11| at
    # 62.05 GHz is inset 0.30 (RL >= 10 dB 60.90-62.80 GHz, -26.9 dB at 61.9 GHz)
    # [results/openems/patch-c-i30]. None restores the closed form.
    # Macro v2 freeze (stage 3b, 2026-10-05): the embedded centre column's pick by the plan's C1
    # rule ("full band out of reach: maximize the minimum in-band RL with the band centred"),
    # point cell-c1b-6 (20 um, K0): L x1.025, inset 0.325, w35 0.42, t_y 0.25 -> worst in-band
    # |S11| -6.5 dB (openEMS frame) / -7.2 dB (Palace-referred), RL-10 band centre 62.63 GHz
    # Palace-referred; ANT-02's RL (and 60.3 GHz gain, 4.8 dBi) miss is preregistered
    # (stage3b-rf/freeze.md). The single-patch values above are history.
    "inset": 0.325,
    "fullwave_l_scale": 1.025,  # drawn L = closed-form L x this (1.0: closed form only)
    "fullwave_ref": "results/openems/patch-c-i30 (C0a single-patch sweep)",
    "notch": 0.10,  # inset slot width beside the feed [D]
    "spacing": 2.90,  # patch centre spacing along the column, ~0.6 lambda0 [BD §6.2]
    "window_margin": 0.15,  # L2 window beyond the patch outline [D], swept (RFS-6)
    "windows": True,  # D4: L2 windows under the radiators (False: patches over solid L2)
    "w35": 0.42,  # 35 ohm lambda/4 width; 0.353 (closed form) -> 0.42 by the C1 pick (cell-c1b-6)
    "t_y": 0.25,  # T-junction offset toward the upper patch; 0.37 -> 0.25 by the C1 pick
    "in_x": None,  # input line x in column frame; None: d/2 (centre of the east gap)
    "p1_y": -2.30,  # column input plane P1, column frame [BD §5.3: P1 2.3 mm below centre]
    # floorplan (U1 frame) [BD §5.3]
    "rx_col_y": 9.5,
    "tx_col_x0": 8.0,
    "tx_col_y": 6.1,
    "bend_r_tx": 0.6,
    # RX S-bends and the inner lines' equalizing bump: the fit takes the lowest bank whose lines
    # keep 0.92 mm corridors (shared fence row) from these radius ranges [D]
    "rx_bend_r_range": [0.4, 1.6],
    "rx_bump_r": [0.30, 0.40, 0.50],
    # straight before Pg on every RX line: the fence rows reach the run-in pair (E - guard_band)
    # along a straight, so the last fence via sits one pitch below it clear of the ring sites; a
    # bump ending closer meets the pair obliquely and leaves a fence opening [D]
    "rx_tail": 0.40,
    # RF uniformity (owner finding 2026-10-03: identical structures across the array within the
    # keepout) [D, rf-uniform design]:
    # guard band around each L1 cut-out: the stitch ring's 0.40 inset plus one 0.45 via spacing;
    # inside it only the straight run-ins, their fence pairs, the ring and plain GND
    "guard_band": 0.85,
    # straight run-in before the entry (two fence pitches); P1 is pour_clear_ant inside the entry
    "runin_out": 0.90,
    # meander radius: legs 2R = 1.0 mm apart share one fence row; a via sits at each U-turn centre
    "meander_r": 0.50,
    # terminated dummy columns: "both" (S2, all four bank ends), "outer" (S1, the open ends RXD0
    # and TXD4), "outer+txd0" (S1.5, S1 plus TXD0) or "none". Frozen 2026-10-04/05 (stage-3b E1
    # bank runs, s3b-bank-s1 vs s3b-bank-a/b/c): S1 beats S2 by ~1.5 dB isolation (38.2 vs 36.7
    # dB) and by less TX1 feed loss (-1.50 vs -1.78/-1.79 dB); recommend S1 [S].
    "dummies": "outer",
    # dummy load: 50 ohm thin-film 0201 (KiCad R_0201_0603Metric land), along the run-in axis.
    # Every load is the same cell (review 2026-10-04: fill vias had landed in three of the four
    # GND lands): its GND end is returned by its own five vias, given relative to (x_in, Pg), each
    # pad edge >= 0.14 mm from either land; no other via may sit in its via zone (x_in +-
    # via_zone[0], Pg - via_zone[1] .. Pg). `zone` is the fit search's keep-clear box. The mask
    # island (x_in +- mask[0], Pg - mask[1] .. Pg - mask[2]) is under solder mask, the lands
    # opening by their own size, so the GND land is mask-defined like the signal land instead
    # of wetting into the bare pour, and the load vias are tented [D]
    "dummy_load": {
        "value": "50R 0201 thin film",
        "pad": [0.46, 0.40],
        "pitch": 0.64,
        "pad1_dy": 0.45,
        "vias": [[-0.50, -1.09], [0.50, -1.09], [-0.58, -0.50], [0.58, -0.50], [0.0, -1.62]],
        "zone": [0.66, 1.35],
        "via_zone": [0.66, 1.65],
        "mask": [0.78, 1.82, 0.05],
    },
    "bank_strip": 2.0,  # cut-out to cut-out between the banks, the isolation wall in the middle [D]
    "pour_clear_ant": 1.0,  # L1 GND pour kept this far from patch copper [D]
    "pour_clear_feed": 0.45,  # and this far from the column's microstrip divider [D]
    # L2-L3 stitching (review 2026-10-03): GND through vias round each bank and along the macro's
    # In2.Cu GND boundary, so the L2-L3 parallel plate has no open edge. The pitch is a via-lattice
    # wall: 0.60 mm with 0.15 mm drills puts its parallel-plate cut-off near 120 GHz (about 35 dB/mm
    # at 62 GHz; review 2026-10-04, not the lambda_d/4 = 0.64 mm argument) [D]; a maximum, rows are
    # divided with ceil
    "stitch_pitch": 0.60,
    # via centre inside the In2 GND boundary: 0.30 keeps the boundary between two vias 0.60 apart
    # within stitch_reach (stage 2's 0.40 would leave 0.50 mm) [D]
    "stitch_inset": 0.30,
    "runin_inset": 0.40,  # first run-in fence pair below the entry; the second at guard_band [D]
    # fence rows start this far outside the package body (pad outside it, the GND at the body
    # edge within stitch_reach) [D]
    "fence_start": 0.25,
    # ring sites between the run-in pairs and round the cut-out sides: 0.35 keeps the cut-out
    # edge between two sites 0.447 apart within stitch_reach (0.40 would leave 0.46) [D]
    "ring_inset": 0.35,
    # L1 GND stitching: every GND point at an edge (within `edge_band` of a gap, cut-out or pour
    # edge) within lambda_g/10 (0.29 mm) + the 0.16 mm pad radius of a GND via, or the GND is
    # removed (stitch or remove; owner D15 keeps it) [D]
    "stitch_reach": 0.45,
    "edge_band": 0.30,
    # D15 ground-stitching study (owner 2026-10-04): the open-pour variant. None takes the value
    # of the `d15` preset (POUR_PRESETS below):
    # A "grid": today's 0.60 mm grid, every L1 GND point within 0.45 mm of a via;
    # B "sparse": a 1.00 mm grid, interior L1 GND within the hard maximum 0.75 mm of a via (fill
    #   vias where it is exceeded; closes stage 2's 2.9 mm unstitched corners), edges as A;
    # C "strips": no L1 pour in the antenna area except the GCPW ground strips (gap edge to the
    #   fence row's pads + strip_margin), the rings round the cut-outs (to the guard-band row's
    #   pads + strip_margin, which holds the isolation strip), the load cells and the launch
    #   ground under the package; the L2-L3 pair keeps its own lattice (vias with bare L1 pads).
    "d15": "A",
    "pour_mode": None,
    "stitch_grid": None,  # interior grid pitch (the open pour; in C the L2-L3 lattice)
    "stitch_dmax": None,  # hard maximum, interior L1 GND point to a via (geodesic)
    # L2-L3 plane pair (bondply): every point of it outside the cut-outs, the package and the PA
    # island within l23_dmax (straight line) of a GND through via, a grid of l23_pitch first. A
    # separate parameter so the L2-L3 stitching can be set apart from the L1 pour; the vias are
    # through vias, so the two lattices are one set of vias, placed as their union. A's 0.60 mm
    # holds under the GCPW (fence rows 1.0 mm apart at 0.45 mm: 0.55 mm) [D]
    "l23_pitch": None,
    "l23_dmax": None,
    "strip_margin": 0.10,
    # dummy-column termination: "load" (the fitted 0201, default) or "open" (land pattern, part not
    # fitted); "short" is refused (rf-uniform: a shorted dummy changes the pattern by 3 dB at
    # 60.3 GHz, an open one by 1.2 dB)
    "dummy_term": "load",
    # L2-L3 bondply cavity under each bank (rf-uniform: excited through the L2 windows, -10 to
    # -25 dB, contained by the ring): "K0" the ring only (today); "K2" L2-L3 posts (through vias)
    # round the window groups wherever L1 is free: pads >= post_clear from patch copper (stage 2
    # saw pads detune patches), pad edge >= post_line_clear from the column's line centrelines
    # (the pour's pour_clear_feed), every column the same (instanced per cell, G2); "K1" solid
    # L2 under the patches (sets windows False) [D]
    "l23_cavity": "K0",
    "post_clear": 0.60,
    "post_line_clear": 0.45,
    "post_pitch": 0.50,
    # C1 column retune: the divider's electrical lengths (the 35 ohm lambda/4 and the south arm's
    # lambda_g/2 surplus) scaled together [D]
    "div_l_scale": 1.0,
    # D5: "corporate" (RFS-4, the required topology) or "series" (RFS-4S, the coupon's series-fed
    # column in the bank: the lower patch inset-fed on its axis from P1, a lambda_g/2 link of
    # ser_link_w on the window to the upper patch, one L2 window per column; no in-gap input)
    "column": "corporate",
    "ser_link_scale": 1.0,
    "ser_link_w": 0.10,
    # TX feed options (stage 3b): "T0" fingers of meander_r (today); "T2" TX1's north-west
    # fingers at meander_r_nw 0.75 (lane fingers keep meander_r: a lane holds 2R + a <= 1.342
    # mm). tx_skew_budget_ps > 0 (T4, needs the owner's waiver of the 2 ps target): TX1's
    # north-west finger equalizes only to within that skew (TX1 has the longest meanders; TX2's
    # lane fingers keep their length). tx_order maps the balls to the columns west to east (T3):
    # only the nested order is planar on L1 for L-routes, any other is refused as crossing
    "tx_eq": "T0",
    "meander_r_nw": None,
    "tx_skew_budget_ps": 0.0,
    "tx_order": ["TX1", "TX2", "TX3"],
    # D14 (owner 2026-10-04): the macro owns the VOUT_PA (1V0_PA) feed. A2 and B2 are joined on
    # L1 by a bar between ball rows 1 and 3; the copper leaves east of row A between A1 and A3,
    # runs north inside the package outline to the pocket and ends on pa_vias through vias
    # (pa_via drill/pad: the board's 0.20/0.40 general class) that tie to the L4 1V0 pour and
    # carry the bottom-side caps. Board rules unchanged: PWR clearance pa_clear to every foreign
    # land, via and pour [BD constraints net_class PWR]; vias wholly inside the pocket, outside
    # the RF region [BD floorplan rf.pocket]. Sizing [D]: 2.5 A peak, 1.0 A RMS on the whole
    # 1.0 V rail [BD §3.3, ARCH-03; TI publishes no PA split], <= pa_via_i_max per via, IR drop
    # <= pa_ir_max, via barrels of pa_plating_um (IPC-6012 class 2 minimum average)
    "pa_feed": True,
    "pa_net": "1V0_PA",
    "pa_balls": ["A2", "B2"],
    "pa_i_peak": 2.5,
    "pa_i_rms": 1.0,
    "pa_via": [0.20, 0.40],
    "pa_vias": 4,
    "pa_via_i_max": 1.0,
    # IR drop budget of the feed [D]: §3.3's fix leaves 0.96 V nominal at the balls after 28 mV
    # of IR drop, so 10 mV more keeps the nominal at the 0.95 V minimum
    "pa_ir_max": 0.010,
    "pa_clear": 0.15,
    "pa_plating_um": 20.0,
    "pa_copper_margin": 0.05,  # PA copper beyond each via pad [D]
    # L2 anti-pad (via pad + pa_clear) edge to the nearest feed centreline [D]: 0.2 mm (2 h)
    # beyond the coplanar ground's 0.30 mm, so the GCPW reference under the line and its gaps is
    # whole
    "pa_antipad_feed_min": 0.50,
    # D14 PA ground-via fence (owner 2026-10-04, stage-3b E1/E2): the un-fenced PA island
    # couples TX1 -34.9 dB into the antenna bank at 62.05 GHz (port case, measured), short of
    # the plan's <= -40 dB rule by ~5 dB (RX4's corner already clears it unfenced, -42.2 dB,
    # despite an earlier note claiming otherwise). A model-only EM test of the FULL 5-6 via
    # ring geometry (em/models/pa-pa-*-fence.json, stage-3b E2) measured -45 to -47 dB with
    # the fence on, 10+ dB of margin past the rule [S] -- a real, not marginal, fix. What this
    # generator step actually places is NOT that ring: it is one row along the pocket edge
    # facing the bank, `pa_fence_offset` out, stepped by `pa_fence_pitch` -- at most
    # floor(pocket_dx / pa_fence_pitch) + 1 (~4) sites, fewer once a load pad or line
    # conflicts, and on the current default layout only ~2 are free. The -45..-47 dB number
    # is therefore evidence that a full fence works, not yet a measurement of what this
    # function builds; closing D14 on the built macro needs either more sites recovered here
    # (nudge the conflicting load/line, or a tighter `pa_fence_pitch`) or a fresh EM point run
    # on the as-placed 2-4 via geometry. The fence vias are plain GND fence vias
    # (RULES["via_fence"] drill/pad, like every other GND via the macro places); board rules
    # are unchanged.
    "pa_fence": True,
    "pa_fence_offset": 0.31,
    "pa_fence_pitch": 0.475,
}

# D15 open-pour presets (owner 2026-10-04): a parameter left None takes its preset's value
POUR_PRESETS = {
    "A": dict(pour_mode="grid", stitch_grid=0.60, stitch_dmax=0.45, l23_pitch=0.60, l23_dmax=0.60),
    "B": dict(
        pour_mode="sparse", stitch_grid=1.00, stitch_dmax=0.75, l23_pitch=1.00, l23_dmax=0.75
    ),
    "C": dict(
        pour_mode="strips", stitch_grid=1.00, stitch_dmax=0.45, l23_pitch=1.00, l23_dmax=0.75
    ),
}
DUMMY_OPTIONS = ("both", "outer", "outer+txd0", "none")


def resolve(overrides: Dict[str, object] | None = None) -> Dict[str, object]:
    p = copy.deepcopy(DEFAULTS)
    if overrides:
        unknown = set(overrides) - set(p)
        if unknown:
            raise KeyError(f"unknown parameters: {sorted(unknown)}")
        p.update(overrides)
    if p["variant"] not in (-1, 0, 1):
        raise ValueError("variant must be -1, 0 or +1 (D12 bracketing)")
    if p["d15"] not in POUR_PRESETS:
        raise ValueError(f"d15 must be one of {sorted(POUR_PRESETS)}")
    for k, v in POUR_PRESETS[str(p["d15"])].items():
        if p[k] is None:
            p[k] = v
    if p["pour_mode"] not in ("grid", "sparse", "strips"):
        raise ValueError("pour_mode must be grid, sparse or strips")
    if p["dummies"] not in DUMMY_OPTIONS:
        raise ValueError(f"dummies must be one of {DUMMY_OPTIONS}")
    if p["dummy_term"] == "short":
        raise ValueError(
            "dummy_term 'short' refused: a shorted dummy changes the pattern by 3 dB at 60.3 GHz "
            "(rf-uniform); use 'load' (fitted 0201) or 'open'"
        )
    if p["dummy_term"] not in ("load", "open"):
        raise ValueError("dummy_term must be load or open")
    if p["l23_cavity"] not in ("K0", "K1", "K2"):
        raise ValueError("l23_cavity must be K0, K1 or K2")
    if p["l23_cavity"] == "K1":
        p["windows"] = False  # solid L2 under the patches
    if p["column"] not in ("corporate", "series"):
        raise ValueError("column must be corporate or series")
    if p["tx_eq"] not in ("T0", "T2"):
        raise ValueError("tx_eq must be T0 or T2 (T1, the accordion: see the README)")
    if p["meander_r_nw"] is None:
        p["meander_r_nw"] = 0.75 if p["tx_eq"] == "T2" else p["meander_r"]
    if sorted(p["tx_order"]) != ["TX1", "TX2", "TX3"]:
        raise ValueError("tx_order must be a permutation of TX1, TX2, TX3")
    if p["in_x"] is None:
        # corporate: the input runs up the column's east gap; series: on the column's axis
        p["in_x"] = D_LATTICE / 2 if p["column"] == "corporate" else 0.0
    return p


def length_scale(p: Dict[str, object]) -> float:
    return 1.0 + float(p["bracket_step"]) * int(p["variant"])


def lambda_g(eeff: float, f_hz: float = F0) -> float:
    return 299792458.0 / f_hz / math.sqrt(eeff) * 1e3
