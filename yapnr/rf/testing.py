"""Test helpers.

- `tiny_spec`: a tiny two-port spec for the optimizer tests (about 1e4 cells, 6 × 6 pixels):
  S-parameter requirements on a 2.4 mm square region between two 2-cell (0.8 mm) feeds on a
  0.8 mm substrate, 8–12 GHz; the best binary design is the straight line continuing the
  feeds. A run of the optimization on it takes about 1.5 s per iteration (numpy, float64).
- `CaseChecks`: the shared body of the end-to-end case tests (tests/e2e/rf, design §11). Each
  case test optimizes its preset (`yapnr.rf.cases`), exports it, re-validates the exported
  footprint on the optimization grid and on a finer grid, and asserts the case's criteria.
  A full run (Bazel `:test_<case>`, tagged `manual`) takes minutes to an hour on 4 threads; its
  run directory is `$YAPNR_RF_RUN_DIR/<case>` when that is set (a finished or interrupted run
  there resumes from its checkpoint), else the test's undeclared outputs. The smoke run
  (`:test_<case>_smoke`, `YAPNR_RF_SMOKE=1`, in CI) is the same topology on a tiny grid for
  four iterations; it asserts the pipeline (progress, export, a pixel-exact re-simulation of
  the footprint, a finite and roughly passive fine re-simulation), not the RF targets.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import tempfile

import numpy as np

from yapnr.rf import cases
from yapnr.rf.export.kicad import read_footprint
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


# -- end-to-end cases ---------------------------------------------------------------------------

SMOKE = os.environ.get("YAPNR_RF_SMOKE") == "1"


def _summary(report: dict) -> str:
    return json.dumps(cases.validate_summary(report), indent=1)


class CaseChecks:
    """Mixin of the e2e case tests (tests/e2e/rf): set `CASE` and mix with TestCase."""

    CASE: str = ""

    def setUp(self):
        self._tmp = None
        base = os.environ.get("YAPNR_RF_RUN_DIR") or os.environ.get("TEST_UNDECLARED_OUTPUTS_DIR")
        if not base:
            self._tmp = base = tempfile.mkdtemp(prefix="rf-e2e-")
        suffix = "-smoke" if SMOKE else ""
        self.out = os.path.join(base, self.CASE + suffix)

    def tearDown(self):
        if self._tmp:
            shutil.rmtree(self._tmp, ignore_errors=True)

    def test_case(self):
        report = cases.run(self.CASE, self.out, scale="smoke" if SMOKE else "full")
        for name in ("footprint.kicad_mod", "result.json", "validation.json", "history.json"):
            self.assertTrue(os.path.exists(os.path.join(self.out, name)), name)
        spec = cases.spec_for(self.CASE, "smoke" if SMOKE else "full")
        n = len(spec.ports)
        for name in (f"coarse.s{n}p", f"coarse_dense.s{n}p", f"fine.s{n}p"):
            self.assertTrue(os.path.exists(os.path.join(self.out, name)), name)
        fp = read_footprint(os.path.join(self.out, "footprint.kicad_mod"))
        n_pads = n + 2 * len(spec.lumped)  # the ports, then two pads per lumped part
        self.assertEqual(sorted(int(p["number"]) for p in fp.pads), list(range(1, n_pads + 1)))
        # The footprint reproduces the exported design on its grid, pixel for pixel.
        same = report["same_grid"]
        self.assertEqual(same["pixel_xor"], 0)
        if SMOKE:
            self._smoke_checks(report)
            return
        # The width and space check passes, every port is joined, and the exported design
        # stays close to the optimizer's binary design (validate.EXPORT_DB, EXPORT_ABS).
        self.assertTrue(report["export_ok"], (same, report.get("drc"), report["footprint"]))
        self.assertTrue(report["coarse"]["ok"], _summary(report))
        self.assertTrue(report["fine"]["ok"], _summary(report))
        if "finer" in report:
            self.assertTrue(report["finer"]["ok"], _summary(report))

    def _smoke_checks(self, report):
        with open(os.path.join(self.out, "history.json"), encoding="utf-8") as fh:
            hist = json.load(fh)["iterations"]
        self.assertEqual(len(hist), 4)
        ts = [h["t"] for h in hist]
        self.assertLess(min(ts[1:]), ts[0])  # the epigraph value fell at least once
        for level in ("coarse", "fine"):
            table = report[level]["table"]
            for key, vals in table.items():
                if key.startswith("S"):
                    self.assertTrue(all(math.isfinite(v) for v in vals), (level, key))
                    self.assertLessEqual(max(vals), 0.5, (level, key))  # roughly passive
            pas = [c for c in report[level]["checks"] if c["kind"] == "passivity"][0]
            self.assertGreater(pas["worst"], -0.2, level)
        fine_cells = report["fine"]["grid"]["cells"]
        self.assertGreater(fine_cells, report["coarse"]["grid"]["cells"])
        self.assertTrue(np.isfinite(report["coarse"]["checks"][0]["worst"]))
