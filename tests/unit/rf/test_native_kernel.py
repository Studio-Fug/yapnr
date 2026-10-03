"""The native FDTD stepper (`fdtd.native_kernel`, backend "native") against the numpy reference.

The C kernel computes every value with numpy's operations in numpy's order and no fused
multiply-add, so the stated bound is 0 ulp: fields, DTFTs, S-parameters, objective values and
adjoint gradients are compared with `assert_array_equal`, float64 and float32 alike, for
several thread counts. Covered: random fields stepped through every kernel (CPML on all sides,
graded axes, the copper-edge μ planes, the inductive sheet, a lumped resistor and PEC edges),
runs with sources, probes and decimation, the optimization pipeline's forward and adjoint runs
(exact and production settings), the divider and the antenna (smoke grids), float32 against
float64 (S within 1e-4), the source tables and backend selection and fallback; both schedules
of the C side, the sweeps and the wavefront passes (YAPNR_RF_TBLOCK).

The native cases need the library: the Bazel-built one (the manual `test_native_kernel_native`
target), else this test compiles it with the host compiler (``cc``/``$CC``); without either they
skip.
"""

from __future__ import annotations

import io
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr
from dataclasses import replace
from unittest.mock import patch

import numpy as np

from yapnr.rf.domain import Domain, DomainSpec, PortSpec
from yapnr.rf.fdtd import native_kernel
from yapnr.rf.fdtd.engine import Simulation
from yapnr.rf.fdtd.sources import GaussianPulse, NuttallFit, SpectralSource, combine
from yapnr.rf.fdtd.stop import StopRule
from yapnr.rf.mesh import COMPONENTS
from yapnr.rf.stackup import Stackup

_BUILT: dict = {}


def kernel_or_skip(test):
    """The native kernel: the provided library, else one compiled with the host compiler."""
    kernel = native_kernel.load()
    if (
        kernel is None
        and "dir" not in _BUILT
        and native_kernel.SOURCE.is_file()
        and shutil.which(os.environ.get("CC", "cc"))
    ):
        out = _BUILT["dir"] = tempfile.mkdtemp(prefix="yapnr-fdtd-")
        library = native_kernel.build_library(out)
        native_kernel.reset()
        with patch.dict(os.environ, {native_kernel.ENV_LIB: str(library)}):
            kernel = native_kernel.load()
    if kernel is None:
        test.skipTest("native FDTD library unavailable: " + native_kernel.status()["reason"])
    return kernel


def tearDownModule():
    if "dir" in _BUILT:
        shutil.rmtree(_BUILT.pop("dir"), True)


def domain(edge_pml=8):
    st = Stackup(3.55, 0.0027, 0.813e-3, 10e9)
    spec = DomainSpec(
        st,
        0.4e-3,
        2,
        (0.0, 3.2e-3, -1.6e-3, 1.6e-3),
        (PortSpec(1, "W", 0.0, 4), PortSpec(2, "E", 0.0, 4)),
        f_max=14e9,
        meas_cells=3,
        src_cells=7,
        pml_gap=2,
        margin=1.6e-3,
        air=3.0e-3,
        n_pml=edge_pml,
        n_pml_top=6,
    )
    return Domain(spec)


def structures(dom, dt):
    """Structures that exercise every kernel path."""
    st = dom.grid
    rng = np.random.default_rng(5)
    stack = dom.spec.stackup
    g = stack.g_min * (stack.g_max / stack.g_min) ** rng.uniform(0, 1, dom.design_shape)
    yield "resistive", dom.structure(g)
    yield "edges", dom.structure(g, copper=rng.uniform(0, 1, dom.design_shape))
    ind = dom.structure(g)
    pix = dom.pixels(g)
    ind.set_sheet(pix, np.where(pix > 0, 2e-10, 0.0), dt=dt)
    yield "inductive", ind
    lump = dom.structure(g, copper=(g > np.median(g)).astype(float))
    idx = st.flat_index("ez", 20, 15, np.arange(0, st.k_c))
    lump.add_resistor("ez", idx, 50.0, idx.size, 1)
    lump.set_pec("ey", st.flat_index("ey", 21, np.arange(10, 14), st.k_c))
    yield "lumped+pec", lump


class SelectionTest(unittest.TestCase):
    """Backend choice and the fallback; no library needed."""

    def test_choose(self):
        choose = native_kernel.choose
        f32, f64 = np.dtype(np.float32), np.dtype(np.float64)
        with patch.dict(os.environ, {}, clear=False):
            for k in (native_kernel.ENV_BACKEND, native_kernel.ENV_DTYPE):
                os.environ.pop(k, None)
            self.assertEqual(choose(None, None, "torch", "float32"), ("torch", f32))
            self.assertEqual(choose(None, None, "torch", "float32", exact=True), ("numpy", f64))
            os.environ[native_kernel.ENV_BACKEND] = "native"
            # A backend switched by the environment runs float64 unless asked.
            self.assertEqual(choose(None, None, "torch", "float32"), ("native", f64))
            self.assertEqual(choose(None, None, "native", "float32"), ("native", f32))
            self.assertEqual(choose(None, None, "torch", "float32", exact=True), ("native", f64))
            self.assertEqual(choose("numpy", None, "torch", "float32"), ("numpy", f32))
            os.environ[native_kernel.ENV_DTYPE] = "f32"
            self.assertEqual(choose(None, None, "torch", "float64"), ("native", f32))
            os.environ[native_kernel.ENV_BACKEND] = "fortran"
            with self.assertRaises(ValueError):
                choose(None, None, "torch", "float32")

    def test_missing_library_falls_back_to_numpy(self):
        native_kernel.reset()
        self.addCleanup(native_kernel.reset)
        dom = domain()
        err = io.StringIO()
        with patch.dict(
            os.environ, {native_kernel.ENV_LIB: "/nonexistent/libyapnr_fdtd.so"}
        ), patch.object(native_kernel, "_candidates", return_value=iter(())), redirect_stderr(err):
            sim = Simulation(dom.grid, dom.structure(), backend="native")
            Simulation(dom.grid, dom.structure(), backend="native")
        self.assertEqual(sim.backend, "numpy")
        self.assertIsNone(sim.threads)
        self.assertEqual(err.getvalue().count("numpy backend used"), 1)
        self.assertFalse(native_kernel.status()["loaded"])


class SourceTableTest(unittest.TestCase):
    """`separable()` tabulates exactly what values(n) returns."""

    def check(self, src, n_max):
        amp, weights = src.separable()
        for n0, n1 in ((0, 7), (5, 300), (n_max - 3, n_max + 4)):
            w, on = weights(n0, n1)
            table = combine(amp, w)
            for b, n in enumerate(range(n0, n1)):
                v = src.values(n)
                self.assertEqual(v is not None, bool(on[b]), n)
                if v is not None:
                    np.testing.assert_array_equal(table[b], v)

    def test_port_sources(self):
        dom = domain()
        dt = 0.95 * dom.grid.courant_dt()
        pulse = GaussianPulse.for_band(8e9, 12e9)
        p1 = dom.ports[0]
        for src in p1.mode_sources(pulse, dt):
            self.check(src, src.end_step)
        for src in p1.sources(pulse, dt, dom.spec.stackup, kind="mode"):
            self.check(src, src.end_step)

    def test_spectral(self):
        omega = 2 * np.pi * np.array([8e9, 10e9, 12e9])
        fit = NuttallFit.build(omega, 1e-12, False)
        rng = np.random.default_rng(2)
        coef = fit.coefficients(rng.standard_normal((9, 3)) + 1j * rng.standard_normal((9, 3)))
        self.check(SpectralSource("ex", np.arange(9), coef, fit), fit.n_window)


class RandomFieldTest(unittest.TestCase):
    """Random fields stepped through every kernel: equal to numpy after 1, 7 and 47 steps."""

    def compare(self, dom, st, dt, dtype, threads):
        ref = Simulation(dom.grid, st, dt=dt, backend="numpy", dtype=dtype)
        nat = Simulation(dom.grid, st, dt=dt, backend="native", dtype=dtype, threads=threads)
        self.assertEqual(nat.backend, "native")
        rng = np.random.default_rng(11)
        for c in COMPONENTS:
            v = rng.standard_normal(ref.f[c].shape).astype(dtype)
            ref.f[c][...] = v
            nat.f[c][...] = v
        for steps in (1, 6, 40):
            ref.advance(steps)
            nat.advance(steps)
            for c in COMPONENTS:
                np.testing.assert_array_equal(nat.field(c), ref.field(c), c)

    def test_structures(self):
        kernel_or_skip(self)
        dom = domain()
        dt = 0.95 * dom.grid.courant_dt()
        for name, st in structures(dom, dt):
            for dtype, threads in ((np.float64, 1), (np.float64, 3), (np.float32, 2)):
                with self.subTest(structure=name, dtype=np.dtype(dtype).name, threads=threads):
                    self.compare(dom, st, dt, dtype, threads)

    def test_wavefront_schedule(self):
        """Several steps per pass over the planes (YAPNR_RF_TBLOCK): the same values."""
        kernel_or_skip(self)
        dom = domain()
        dt = 0.95 * dom.grid.courant_dt()
        for name, st in structures(dom, dt):
            for tblock in ("1", "3", "8"):
                with self.subTest(structure=name, tblock=tblock), patch.dict(
                    os.environ, {native_kernel.ENV_TBLOCK: tblock}
                ):
                    self.compare(dom, st, dt, np.float64, 3)

    def test_uneven_cpml(self):
        """CPML slabs of unequal length (separate ψ runs) and none along one side."""
        kernel_or_skip(self)
        from yapnr.rf.materials import Structure
        from yapnr.rf.mesh import Axis, Grid, PMLCells

        x = Axis(np.cumsum(np.r_[0.0, np.linspace(0.3e-3, 0.5e-3, 30)]))
        y = Axis(np.linspace(0.0, 9e-3, 26))
        z = Axis(np.r_[np.linspace(0, 0.8e-3, 3), 0.8e-3 + np.cumsum(np.full(12, 0.4e-3))])
        grid = Grid(x, y, z, 2, PMLCells(5, 7, 0, 6, 4))
        st = Structure(grid, Stackup(3.0, 0.002, 0.8e-3, 10e9))
        rng = np.random.default_rng(1)
        st.set_pixels(rng.uniform(0, 1e3, grid.n[:2]))
        dt = 0.95 * grid.courant_dt()
        ref = Simulation(grid, st, dt=dt, backend="numpy")
        nat = Simulation(grid, st, dt=dt, backend="native", threads=4)
        for c in COMPONENTS:
            v = rng.standard_normal(ref.f[c].shape)
            ref.f[c][...] = v
            nat.f[c][...] = v
        ref.advance(25)
        nat.advance(25)
        for c in COMPONENTS:
            np.testing.assert_array_equal(nat.field(c), ref.field(c), c)


class RunTest(unittest.TestCase):
    """`run` with port sources, probes, decimation and the stop rule."""

    @classmethod
    def setUpClass(cls):
        cls.dom = domain()
        cls.dt = 0.95 * cls.dom.grid.courant_dt()
        cls.omega = 2 * np.pi * np.array([8e9, 10e9, 12e9])
        cls.pulse = GaussianPulse.for_band(8e9, 12e9)
        cls.st = dict(structures(cls.dom, cls.dt))["edges"]

    def run_sim(self, backend, dtype=np.float64, threads=1, decimation=1, callback=None):
        sim = Simulation(
            self.dom.grid, self.st, dt=self.dt, backend=backend, dtype=dtype, threads=threads
        )
        p1, p2 = self.dom.ports
        res = sim.run(
            p1.sources(self.pulse, self.dt, self.dom.spec.stackup, kind="mode"),
            p1.probes + p2.probes + self.dom.design_probes(),
            self.omega,
            StopRule(tol=1e-6, f_lo=8e9),
            decimation=decimation,
            callback=callback,
        )
        return sim, res

    def assert_same(self, a, b):
        self.assertEqual(a.steps, b.steps)
        self.assertEqual(a.converged, b.converged)
        self.assertEqual(a.history, b.history)
        self.assertEqual(sorted(a.dft), sorted(b.dft))
        for k in a.dft:
            np.testing.assert_array_equal(a.dft[k], b.dft[k], k)

    def test_runs_equal_numpy(self):
        kernel_or_skip(self)
        for dtype in (np.float64, np.float32):
            for dec in (1, 3):
                _, ref = self.run_sim("numpy", dtype, decimation=dec)
                self.assertTrue(ref.converged)
                for threads in (1, 4):
                    with self.subTest(dtype=np.dtype(dtype).name, decimation=dec, threads=threads):
                        sim, got = self.run_sim("native", dtype, threads, dec)
                        self.assertEqual(sim.backend, "native")
                        self.assert_same(got, ref)

    def test_wavefront_runs_equal_numpy(self):
        """Sources, mu blend, sheet and probes row by row inside the passes."""
        kernel_or_skip(self)
        for dec in (1, 3):
            _, ref = self.run_sim("numpy", decimation=dec)
            for tblock in ("2", "5", "auto"):
                with self.subTest(decimation=dec, tblock=tblock), patch.dict(
                    os.environ, {native_kernel.ENV_TBLOCK: tblock, native_kernel.ENV_CACHE: "1"}
                ):
                    sim, got = self.run_sim("native", threads=3, decimation=dec)
                    if tblock != "auto":
                        self.assertEqual(sim._native.tblock, int(tblock))
                    self.assert_same(got, ref)

    def test_callback_steps_one_at_a_time(self):
        """With a callback the native backend runs one-step blocks: the same run."""
        kernel_or_skip(self)
        seen = []
        _, ref = self.run_sim("native", threads=2)
        _, got = self.run_sim("native", threads=2, callback=lambda sim, n: seen.append(n))
        self.assertEqual(seen, list(range(1, ref.steps + 1)))
        self.assert_same(got, ref)

    def test_forked_child_gets_its_own_pool(self):
        """A forked process inherits the pool's handle but not its threads: it makes its own."""
        kernel_or_skip(self)
        if not hasattr(os, "fork"):
            self.skipTest("no fork")
        sim, ref = self.run_sim("native", threads=3)
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:  # child: run again on the inherited simulation, report equality
            ok = 0
            try:
                p1, p2 = self.dom.ports
                got = sim.run(
                    p1.sources(self.pulse, self.dt, self.dom.spec.stackup, kind="mode"),
                    p1.probes + p2.probes + self.dom.design_probes(),
                    self.omega,
                    StopRule(tol=1e-6, f_lo=8e9),
                )
                ok = int(all(np.array_equal(got.dft[k], ref.dft[k]) for k in ref.dft))
            finally:
                os.write(w, bytes([ok]))
                os._exit(0)
        os.close(w)
        result = os.read(r, 1)
        os.waitpid(pid, 0)
        self.assertEqual(result, bytes([1]))

    def test_float32_sparameters(self):
        """float32 (opt-in) against float64: |ΔS| < 1e-4, as for torch (test_backends)."""
        kernel_or_skip(self)
        from yapnr.rf import sparams
        from yapnr.rf.ports import LineCalibration

        cal = LineCalibration(
            omega=self.omega, zc=np.full(3, 50.0 + 0j), k=self.omega * 1.7 / 3e8, dt=self.dt
        )
        s = {}
        for dtype in (np.float64, np.float32):
            _, res = self.run_sim("native", dtype, threads=2)
            p1, p2 = self.dom.ports
            a1, b1 = sparams.port_waves(p1, cal, res.dft, self.omega)
            a2, b2 = sparams.port_waves(p2, cal, res.dft, self.omega)
            s[dtype] = np.array([b1 / a1, b2 / a1])
        self.assertLess(np.abs(s[np.float32] - s[np.float64]).max(), 1e-4)

    def test_repeat_is_bit_identical(self):
        kernel_or_skip(self)
        _, a = self.run_sim("native", threads=4)
        _, b = self.run_sim("native", threads=4)
        _, c = self.run_sim("native", threads=1)
        self.assert_same(a, b)
        self.assert_same(a, c)


class PipelineTest(unittest.TestCase):
    """The optimizer's evaluation (forward and adjoint runs, gradients): equal to numpy."""

    def evaluate(self, spec, backend, dtype, threads, exact):
        from yapnr.rf.problem import Problem
        from yapnr.rf.testing import nominal_calibration

        p = Problem(
            spec,
            exact=exact,
            backend=backend,
            dtype=dtype,
            threads=threads,
            calibrations=nominal_calibration(),
        )
        self.assertEqual(p.sim.backend, backend)
        rng = np.random.default_rng(3)
        x = rng.uniform(0.3, 0.7, p.param.n_dof)
        return p, p.evaluate(p.param.rho_bar(x, 8.0))

    def check(self, spec, exact):
        kernel_or_skip(self)
        for dtype in (np.float64,) if exact else (np.float64, np.float32):
            _, ref = self.evaluate(spec, "numpy", dtype, 1, exact)
            for threads, tblock in ((1, "0"), (3, "0"), (3, "4")):
                with self.subTest(
                    dtype=np.dtype(dtype).name, threads=threads, tblock=tblock
                ), patch.dict(os.environ, {native_kernel.ENV_TBLOCK: tblock}):
                    p, got = self.evaluate(spec, "native", dtype, threads, exact)
                    self.assertEqual(got.steps, ref.steps)
                    np.testing.assert_array_equal(got.values, ref.values)
                    np.testing.assert_array_equal(got.grads, ref.grads)
                    np.testing.assert_array_equal(got.s, ref.s)
                    self.assertEqual(p.describe()["native"]["threads"], threads)

    def test_reactive_with_edges_exact(self):
        from yapnr.rf.spec import OptimizerSpec
        from yapnr.rf.testing import tiny_spec

        spec = tiny_spec(radiated=True)
        spec = spec.replace(
            optimizer=OptimizerSpec(interpolation="reactive"),
            solver=replace(spec.solver, edge_correction=True, port_source="mode"),
        )
        self.check(spec, exact=True)

    def test_lumped_production_settings(self):
        from yapnr.rf.spec import Absorbed, Lumped, OptimizerSpec
        from yapnr.rf.testing import tiny_spec

        spec = tiny_spec()
        spec = spec.replace(
            lumped=(Lumped("R1", (0.8, 1.6), (-0.4, 0.4), "y", 50.0, 0.4),),
            requirements=spec.requirements + (Absorbed("R1", 1).at_least(0.3, band="b"),),
            optimizer=OptimizerSpec(damping=0.5),
            solver=replace(spec.solver, edge_correction=True, port_source="mode"),
        )
        self.check(spec, exact=False)


class CaseTest(unittest.TestCase):
    """The divider and the antenna (smoke grids, the cases' topology and solver settings):
    S-parameters and objective values of a gray design equal to numpy's."""

    def check(self, case):
        kernel_or_skip(self)
        from yapnr.rf import cases
        from yapnr.rf.problem import Problem

        spec = cases.spec_for(case, "smoke")
        ref = Problem(spec, backend="numpy", dtype=np.float64)
        got = Problem(spec, backend="native", dtype=np.float64, threads=3)
        for w, cal in ref._cal_by_width.items():
            # The line calibrations ran on each backend: equal too.
            np.testing.assert_array_equal(got._cal_by_width[w].zc, cal.zc)
            np.testing.assert_array_equal(got._cal_by_width[w].k, cal.k)
        rho = np.random.default_rng(7).uniform(0.2, 0.8, ref.design_shape)
        a = ref.evaluate(rho, gradients=False)
        b = got.evaluate(rho, gradients=False)
        self.assertEqual(a.steps, b.steps)
        np.testing.assert_array_equal(b.s, a.s)
        np.testing.assert_array_equal(b.values, a.values)

    def test_divider(self):
        self.check("divider")

    def test_antenna(self):
        self.check("antenna")


if __name__ == "__main__":
    unittest.main()
