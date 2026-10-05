"""Test helpers.

- `GradientChecks`: the shared body of the pipeline gradient tests (tests/unit/rf).
- `tiny_spec`: a tiny two-port spec for the optimizer tests (about 1e4 cells, 6 × 6 pixels):
  S-parameter requirements on a 2.4 mm square region between two 2-cell (0.8 mm) feeds on a
  0.8 mm substrate, 8–12 GHz; the best binary design is the straight line continuing the
  feeds. A run of the optimization on it takes about 1.5 s per iteration (numpy, float64; the
  default backend, native where its library loads, is faster).
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
    RadiationBox,
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


def tiny_spec(*, radiated: bool = False, extraction: str = "vi", **optimizer) -> Spec:
    """The tiny two-port (see the module doc). Its ports keep the V/I waves (`extraction`):
    the measurement planes lie two cells from the region and three from the source, and the
    6-cell CPML cuts the feed mode's tail, which the modal projection needs (its straight line
    reads −15 to −18 dB modal reflection there against −40 dB on a physics-grade grid,
    `line_spec`); the gradient tests run both."""
    reqs = [S(2, 1).at_least_db(-0.3, band="b"), S(1, 1).at_most_db(-15, band="b")]
    if radiated:
        reqs.append(RadiatedFraction(1).at_most(0.1, band="b"))
    opt = dict(betas=(8, 16, 32), iterations_per_beta=4)
    opt.update(optimizer)
    # The closed radiation box one cell outside the 2.4 mm region (the 1.2 mm margin leaves no
    # room for the default two cells between the box and the CPML).
    rad = RadiationBox(clearance_cells=1) if radiated else None
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
        solver=SolverSpec(sweep_points=11, port_extraction=extraction),
        radiation=rad,
    )


def line_spec(*, port_source: str = "mode", **solver) -> Spec:
    """A two-port on S1 with a physics-grade grid (0.3 mm pitch, the default 6h feeds, an 8 mm
    margin), for the radiated-power and port-wave tests: about 1.4e5 cells, 16 × 16 pixels,
    8–12 GHz, the closed radiation box at its default clearance (`straight_line`: the design
    that continues the feed). The margin holds the feed mode's lateral tail: with the default
    4h the modal planes miss enough of it in the CPML that an open end's reflection moves the
    incident power by 2e-3, with 8 mm by 4e-4."""
    return Spec(
        name="line",
        stackup=StackupSpec(3.55, 0.0027, 0.813, 10.0),
        grid=GridSpec(
            pitch_mm=0.3,
            substrate_cells=4,
            air_mm=5.5,
            dz_max_mm=0.8,
            margin_mm=8.0,
            f_max_ghz=12.5,
        ),
        design_region=(0.0, 4.8, -2.4, 2.4),
        ports=(Port(1, "W", 0.0, 6), Port(2, "E", 0.0, 6)),
        bands={"b": Band(8.0, 12.0, 3)},
        requirements=(
            S(2, 1).at_least_db(-0.3, band="b"),
            RadiatedFraction(1).at_most(0.1, band="b"),
        ),
        solver=SolverSpec(sweep_points=11, port_source=port_source, **solver),
    )


def tiny_board_spec(*, ground="board", requirements=None, **optimizer) -> Spec:
    """A tiny board model (design §26) for the gradient and parity tests: a 6 × 6-pixel design
    region (0.8 mm pitch) on a 9.6 × 8 mm board of εr 3, h 0.8 mm, fed by a lumped port at its
    west edge through a fixed 0.8 mm stub; ground under the whole board ("board"), under all but
    a keepout below the design region ("keepout"), or infinite ("infinite"); 9–11 GHz. The
    requirements cover every pattern form (or `requirements`)."""
    from yapnr.rf.spec import BoardSpec, GroundSpec, Rect

    if ground == "infinite":
        gspec = "infinite"
    elif ground == "keepout":
        gspec = GroundSpec(keepout=(Rect((2.4, 4.8), (-2.4, 2.4)),))
    else:
        gspec = GroundSpec()
    board = BoardSpec(
        x_mm=(-2.4, 7.2),
        y_mm=(-4.0, 4.0),
        ground=gspec,
        air_mm=4.8,
        max_cell_mm=1.6,
        copper=(Rect((-1.6, 0.0), (-0.4, 0.4)),),
        pml_cells=6,
    )
    if requirements is None:
        cut = {"cut": {"theta_deg": 60, "points": 4}}
        requirements = [
            {"s": [1, 1], "max_db": -10, "band": "b"},
            {"gain": 1, "min_dbi": 3.0, "directions": {"point": {"theta_deg": 0}}, "band": "b"},
            {
                "gain": 1,
                "kind": "directivity",
                "pol": "co",
                "max_dbi": 9.0,
                "directions": cut,
                "band": "b",
            },
            {"ripple": 1, "max_db": 3.0, "directions": cut, "band": "b"},
            {"hpbw": 1, "between_deg": [40, 120], "cut_phi_deg": 90, "band": "b"},
            {
                "front_to_back": 1,
                "min_db": 6,
                "front": {"point": {"theta_deg": 0}},
                "back": (
                    {"cut": {"theta_deg": 80, "points": 3}}
                    if ground == "infinite"
                    else {"cone": {"toward": "-z", "half_angle_deg": 30, "step_deg": 30}}
                ),
                "band": "b",
            },
            # Off the symmetry plane y = 0 (there the cross-polar field vanishes).
            {
                "cross_pol": 1,
                "max_db": -10,
                "directions": {"point": {"theta_deg": 30, "phi_deg": 45}},
                "band": "b",
            },
            {"shape": 1, "target": "beam", "max_rms_db": 3.0, "band": "b"},
            {
                "shape": 1,
                "target": "beam",
                "max_rms_db": 3.0,
                "form": "log_l2",
                "weight": "target",
                "band": "b",
            },
            {"efficiency": 1, "kind": "radiation", "min": 0.5, "band": "b"},
            {"radiated": 1, "min": 0.3, "band": "b"},
        ]
    from yapnr.rf.spec import Requirement

    reqs = []
    for r in requirements:
        reqs.extend(Requirement.from_dict(r) if isinstance(r, dict) else [r])
    opt = dict(betas=(8, 16), iterations_per_beta=2)
    opt.update(optimizer)
    from yapnr.rf.patterns import parse_target

    return Spec(
        name="tiny-board",
        stackup=StackupSpec(3.0, 0.002, 0.8, 10.0),
        grid=GridSpec(pitch_mm=0.8, substrate_cells=2, core_cells=1, f_max_ghz=13.0),
        design_region=(0.0, 4.8, -2.4, 2.4),
        ports=(Port(1, kind="lumped", x_mm=(-1.6, -1.6), y_mm=(-0.4, 0.4), ohms=50.0),),
        bands={"b": Band(9.0, 11.0, 3)},
        requirements=tuple(reqs),
        symmetry="mirror_y",
        rules=Rules(0.8, 0.8),
        optimizer=OptimizerSpec(**opt),
        solver=SolverSpec(sweep_points=11),
        board=board,
        patterns={"beam": parse_target({"preset": "beam", "toward": "+z", "hpbw_deg": 80})},
    )


def straight_line(problem) -> np.ndarray:
    """The design that continues port 1's feed straight across the window."""
    ni, nj = problem.design_shape
    rho = np.zeros((ni, nj))
    pg = problem.ports[1]
    j0 = problem.domain.window[2]
    rho[:, pg.ta - j0 : pg.tb - j0] = 1.0
    return rho


def nominal_calibration() -> dict:
    """A fixed line calibration (skips the calibration run in gradient tests)."""
    om = 2 * np.pi * np.array([8e9, 10e9, 12e9])
    zc = np.array([72 + 1j, 72.5 + 0.8j, 73 + 0.6j])
    return {"*": LineCalibration(om, zc, om * np.sqrt(2.3) / 3e8 + 0.5j, 0.0)}


# -- the native kernel --------------------------------------------------------------------------

_NATIVE_BUILT: dict = {}


def native_kernel_or_skip(test):
    """The native FDTD kernel for a test: the library the loader finds (Bazel: the one
    //yapnr/rf carries), else one compiled once per process with the host compiler (``cc`` or
    ``$CC``, into a temporary directory removed at exit); skips the test without either, and
    fails it instead when ``YAPNR_RF_REQUIRE_NATIVE`` is set."""
    import atexit
    import shutil
    import tempfile
    from unittest.mock import patch

    from yapnr.rf.fdtd import native_kernel

    kernel = native_kernel.load()
    if (
        kernel is None
        and "dir" not in _NATIVE_BUILT
        and native_kernel.SOURCE.is_file()
        and shutil.which(os.environ.get("CC", "cc"))
    ):
        out = _NATIVE_BUILT["dir"] = tempfile.mkdtemp(prefix="yapnr-fdtd-")
        atexit.register(shutil.rmtree, out, True)
        _NATIVE_BUILT["library"] = native_kernel.build_library(out)
    if kernel is None and "library" in _NATIVE_BUILT:
        # (again after a test that made the loader forget it)
        native_kernel.reset()
        with patch.dict(os.environ, {native_kernel.ENV_LIB: str(_NATIVE_BUILT["library"])}):
            kernel = native_kernel.load()
    if kernel is None:
        reason = "native FDTD library unavailable: " + native_kernel.status()["reason"]
        if native_kernel.required():  # the library is the point
            test.fail(reason)
        test.skipTest(reason)
    return kernel


# -- pipeline gradients -------------------------------------------------------------------------


class GradientChecks:
    """Mixin of the pipeline gradient tests (tests/unit/rf/test_pipeline_gradient*.py): the
    whole pipeline's gradient against Richardson-extrapolated central differences along a
    random direction, and the length-scale constraints' at β = 128. A subclass calls
    `setup_gradient(spec, seed)` in its setUpClass and mixes with TestCase."""

    OBJECTIVE = "spec"

    @classmethod
    def setup_gradient(cls, spec, seed: int) -> None:
        from yapnr.rf.problem import Problem

        cls.p = p = Problem(spec, exact=True, calibrations=nominal_calibration())
        rng = np.random.default_rng(seed)
        cls.x = rng.uniform(0.3, 0.7, p.param.n_dof)
        cls.beta = 8.0
        ev = p.evaluate(p.param.rho_bar(cls.x, cls.beta), objective=cls.OBJECTIVE)
        cls.ev = ev
        cls.grad = p.param.vjp(cls.x, cls.beta, ev.grads)
        cls.v = rng.standard_normal(cls.x.size)

    def _f(self, x):
        rho = self.p.param.rho_bar(x, self.beta)
        return self.p.evaluate(rho, gradients=False, objective=self.OBJECTIVE).values

    def test_runs_converged(self):
        self.assertTrue(self.ev.converged)
        self.assertEqual(len(self.ev.keys), 3)

    def test_directional_derivative(self):
        h = 1e-4
        x, v = self.x, self.v
        d1 = (self._f(x + h * v) - self._f(x - h * v)) / (2 * h)
        d2 = (self._f(x + 0.5 * h * v) - self._f(x - 0.5 * h * v)) / h
        fd = (4 * d2 - d1) / 3
        adj = self.grad @ v
        np.testing.assert_allclose(adj, fd, rtol=1e-6)

    def test_lengthscale_gradient(self):
        ls = self.p.lengthscale
        self.assertIsNotNone(ls)
        h = 1e-5
        g, dg = self.p.param.lengthscale(self.x, 128.0, ls)
        gp, _ = self.p.param.lengthscale(self.x + h * self.v, 128.0, ls)
        gm, _ = self.p.param.lengthscale(self.x - h * self.v, 128.0, ls)
        np.testing.assert_allclose(dg @ self.v, (gp - gm) / (2 * h), rtol=1e-5)


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
