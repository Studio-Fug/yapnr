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
    "inset": 0.30,
    "fullwave_l_scale": 0.967,  # drawn L = closed-form L x this (1.0: closed form only)
    "fullwave_ref": "results/openems/patch-c-i30 (C0a single-patch sweep)",
    "notch": 0.10,  # inset slot width beside the feed [D]
    "spacing": 2.90,  # patch centre spacing along the column, ~0.6 lambda0 [BD §6.2]
    "window_margin": 0.15,  # L2 window beyond the patch outline [D], swept (RFS-6)
    "windows": True,  # D4: L2 windows under the radiators (False: patches over solid L2)
    "w35": 0.353,  # 35.4 ohm lambda/4 on 4 mil [BD board_calc]
    "t_y": 0.37,  # T-junction offset from the column centre toward the upper patch [D]
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
    # terminated dummy columns: "both" (all four bank ends), "outer" (RX0 and TX4) or "none"
    "dummies": "both",
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
    # L2-L3 stitching (review 2026-10-03): GND through vias at <= lambda_d/4 in RO4450F
    # (62 GHz: lambda0 4.83 mm / sqrt(3.52) = 2.58 mm -> 0.64 mm) round each bank and along the
    # macro's In2.Cu GND boundary, so the L2-L3 parallel plate has no open edge [D]; a maximum,
    # rows are divided with ceil
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
    # L1 GND stitching: every GND point within lambda_g/10 (0.29 mm) + the 0.16 mm pad radius of a
    # GND via, or the GND is removed; open pour gets this via grid [D]
    "stitch_reach": 0.45,
    "stitch_grid": 0.60,
}


def resolve(overrides: Dict[str, object] | None = None) -> Dict[str, object]:
    p = copy.deepcopy(DEFAULTS)
    if overrides:
        unknown = set(overrides) - set(p)
        if unknown:
            raise KeyError(f"unknown parameters: {sorted(unknown)}")
        p.update(overrides)
    if p["variant"] not in (-1, 0, 1):
        raise ValueError("variant must be -1, 0 or +1 (D12 bracketing)")
    if p["in_x"] is None:
        p["in_x"] = D_LATTICE / 2
    return p


def length_scale(p: Dict[str, object]) -> float:
    return 1.0 + float(p["bracket_step"]) * int(p["variant"])


def lambda_g(eeff: float, f_hz: float = F0) -> float:
    return 299792458.0 / f_hz / math.sqrt(eeff) * 1e3
