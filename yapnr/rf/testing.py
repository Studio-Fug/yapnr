"""Test helpers: a tiny two-port spec for the optimizer tests (about 1e4 cells, 6 × 6 pixels).

S-parameter requirements on a 2.4 mm square region between two 2-cell (0.8 mm) feeds on a
0.8 mm substrate, 8–12 GHz; the best binary design is the straight line continuing the feeds.
A run of the optimization on it takes about 1.5 s per iteration (numpy, float64).
"""

from __future__ import annotations

import numpy as np

from yapnr.rf.ports import LineCalibration
from yapnr.rf.spec import (
    Band,
    GridSpec,
    OptimizerSpec,
    Port,
    RadiatedFraction,
    Rules,
    S,
    SolverSpec,
    Spec,
    StackupSpec,
)

GRID = GridSpec(
    pitch_mm=0.4,
    substrate_cells=2,
    meas_cells=2,
    src_cells=5,
    pml_gap=2,
    core_cells=1,
    margin_mm=1.2,
    air_mm=2.4,
    pml_cells=6,
    pml_top_cells=6,
    f_max_ghz=15.0,
)


def tiny_spec(*, radiated: bool = False, **optimizer) -> Spec:
    reqs = [S(2, 1).at_least_db(-0.3, band="b"), S(1, 1).at_most_db(-15, band="b")]
    if radiated:
        reqs.append(RadiatedFraction(1).at_most(0.1, band="b"))
    opt = dict(betas=(8, 16, 32), iterations_per_beta=4)
    opt.update(optimizer)
    return Spec(
        name="tiny",
        stackup=StackupSpec(3.0, 0.002, 0.8, 10.0),
        grid=GRID,
        design_region=(0.0, 2.4, -1.2, 1.2),
        ports=(Port(1, "W", 0.0, 2), Port(2, "E", 0.0, 2)),
        bands={"b": Band(8.0, 12.0, 3)},
        requirements=tuple(reqs),
        rules=Rules(0.8, 0.8),
        symmetry="mirror_y",
        optimizer=OptimizerSpec(**opt),
        solver=SolverSpec(backend="numpy", dtype="float64", sweep_points=11),
    )


def nominal_calibration() -> dict:
    """A fixed line calibration (skips the calibration run in gradient tests)."""
    om = 2 * np.pi * np.array([8e9, 10e9, 12e9])
    zc = np.array([72 + 1j, 72.5 + 0.8j, 73 + 0.6j])
    return {"*": LineCalibration(om, zc, om * np.sqrt(2.3) / 3e8 + 0.5j, 0.0)}
