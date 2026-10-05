"""The stage-3b macro variants (boards) behind the EM matrix, and the C1 / D5 design points.

Every board variant is the default macro (D15 A, S2 dummies with fitted loads, K0, corporate
column, T0 feeds, the D14 PA feed) with the overrides listed; `python -m rfmacro variants`
builds them (each must pass G1-G9 and KiCad's DRC). The C1/SER points change only the column
inside the cut-out; their EM models are built from the generator directly
(`openems/triplet.py`), and the DOE's corners are built as boards here so the DRC covers the
range.
"""

from __future__ import annotations

from typing import Dict, List

BOARDS: Dict[str, Dict[str, object]] = {
    # D15 open-pour study (E1-BANK, E1-PP), S2/K0
    "s3b-A": {"d15": "A"},
    "s3b-B": {"d15": "B"},
    "s3b-C": {"d15": "C"},
    # dummies: S1 (open ends only; E1-BANK), S1.5 (S1 plus TXD0), loads not fitted
    "s3b-S1": {"dummies": "outer"},
    "s3b-S15": {"dummies": "outer+txd0"},
    "s3b-open": {"dummy_term": "open"},
    # L2-L3 cavity options (E1-CAV models come from the generator; the boards for the DRC)
    "s3b-K2": {"l23_cavity": "K2"},
    "s3b-K1": {"l23_cavity": "K1"},
    # D5: series-fed columns in the bank
    "s3b-SER": {"column": "series"},
    # TX feed options (E1-TX): T2 (TX1's north-west finger R 0.75), T4 (7.9 ps, needs a waiver)
    "s3b-T2": {"tx_eq": "T2"},
    "s3b-T4": {"tx_skew_budget_ps": 7.9},
    # D14 reference: the corner without the PA feed (E1-PA's baseline)
    "s3b-pa0": {"pa_feed": False},
    # C1 DOE corners (the column only; the DRC over the swept range)
    "s3b-C1lo": {"fullwave_l_scale": 0.98, "inset": 0.25, "w35": 0.30, "t_y": 0.25},
    "s3b-C1hi": {
        "fullwave_l_scale": 1.04,
        "inset": 0.40,
        "w35": 0.45,
        "t_y": 0.49,
        "div_l_scale": 1.04,
    },
}

# C1 corporate DOE, first stage (E1-C1a): patch length scale x inset at today's w35 / t_y
C1_L = [0.98, 0.995, 1.01, 1.025, 1.04]
C1_INSET = [0.25, 0.325, 0.40]
# second stage, after the first: w35 x t_y round the best (L, inset)
C1_W35 = [0.30, 0.353, 0.42]
C1_TY = [0.25, 0.37, 0.49]
# D5 series-fed DOE (E1-SER): (fullwave_l_scale, ser_link_scale, inset)
SER = [
    (0.98, 1.00, 0.30),
    (1.00, 1.00, 0.30),
    (1.02, 1.00, 0.30),
    (1.00, 0.95, 0.30),
    (1.00, 1.05, 0.30),
    (1.00, 1.00, 0.20),
    (1.00, 1.00, 0.40),
    (1.02, 1.00, 0.40),
]


def c1a_points() -> List[Dict[str, object]]:
    return [{"fullwave_l_scale": L, "inset": i} for L in C1_L for i in C1_INSET]


def c1b_points(best: Dict[str, object]) -> List[Dict[str, object]]:
    return [dict(best, w35=w, t_y=t) for w in C1_W35 for t in C1_TY]


def ser_points() -> List[Dict[str, object]]:
    return [
        {"column": "series", "fullwave_l_scale": L, "ser_link_scale": k, "inset": i}
        for L, k, i in SER
    ]
