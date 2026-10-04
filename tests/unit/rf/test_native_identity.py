"""Native float64 is numpy float64, bit for bit, with every round-2 solver option on and off.

One evaluation with gradients (forward and adjoint runs, objective values, design gradients,
S-parameters) of a fixed gray design on the smoke grids of the divider and the antenna, and of
a diagonal-pattern design on the divider's grid (one-pixel diagonal lines every three pixels:
the copper-edge correction's worst pattern for the time step, `edges._patterns`). Each with the
copper-edge correction (its ε and μ factors, the extra gradient probes and the diagonal-pattern
time-step bound) off and on and the static and modal port sources; also the Wilkinson-type
combiner (a lumped resistor and its absorbed-power requirement) and the reactive copper sheet.
The sha256 of (steps, values, gradients, S) is compared: numpy float64 against native float64
on one thread with the sweeps and on three threads with 3-step wavefront passes; numpy float32
against native float32 (equal too), and float32 against float64 within the documented bounds
(objective values 1e-5, gradients 2e-5 relative; docs/rf-solver-backends.md).

Needs the native library (Bazel: the one //yapnr/rf carries; a direct run compiles it with the
host compiler, `yapnr.rf.testing.native_kernel_or_skip`).
"""

from __future__ import annotations

import hashlib
import json
import os
import unittest
from dataclasses import replace
from unittest.mock import patch

import numpy as np

from yapnr.rf.fdtd import native_kernel
from yapnr.rf.testing import native_kernel_or_skip

OPTIONS = ((False, "static"), (True, "static"), (False, "mode"), (True, "mode"))
F32_VALUES, F32_GRADS = 1e-5, 2e-5
# Bazel's test sharding (the target's shard_count): test method k runs on shard k mod N.
SHARDS = int(os.environ.get("TEST_TOTAL_SHARDS", "1"))
SHARD = int(os.environ.get("TEST_SHARD_INDEX", "0"))
if os.environ.get("TEST_SHARD_STATUS_FILE"):
    open(os.environ["TEST_SHARD_STATUS_FILE"], "a").close()


def digest(ev) -> str:
    h = hashlib.sha256()
    h.update(json.dumps(ev.steps, sort_keys=True, default=str).encode())
    for a in (ev.values, ev.grads, ev.s):
        h.update(np.ascontiguousarray(a).tobytes())
    return h.hexdigest()


def design(kind: str, shape) -> np.ndarray:
    if kind == "diagonal":
        i, j = np.meshgrid(np.arange(shape[0]), np.arange(shape[1]), indexing="ij")
        return 0.15 + 0.7 * ((i + j) % 3 == 0)
    return np.random.default_rng(1).uniform(0.2, 0.8, shape)


class NativeIdentityTest(unittest.TestCase):
    ORDER = (
        "test_divider",
        "test_antenna",
        "test_diagonal_pattern",
        "test_wilkinson_lumped_resistor",
        "test_reactive_sheet",
    )

    def setUp(self):
        if self.ORDER.index(self._testMethodName) % SHARDS != SHARD:
            self.skipTest(f"on shard {self.ORDER.index(self._testMethodName) % SHARDS}")

    def spec(self, case, edge=None, source=None, **optimizer):
        from yapnr.rf import cases

        spec = cases.spec_for(case, "smoke")
        solver = spec.solver
        if edge is not None:
            solver = replace(solver, edge_correction=edge, port_source=source)
        opt = replace(spec.optimizer, **optimizer) if optimizer else spec.optimizer
        return spec.replace(solver=solver, optimizer=opt)

    def evaluate(self, spec, kind, backend, dtype, threads, tblock):
        from yapnr.rf.problem import Problem

        with patch.dict(os.environ, {native_kernel.ENV_TBLOCK: str(tblock)}):
            p = Problem(spec, backend=backend, dtype=dtype, threads=threads)
            self.assertEqual(p.sim.backend, backend)
            ev = p.evaluate(design(kind, p.design_shape), gradients=True)
        self.assertTrue(np.all(np.isfinite(ev.grads)))
        return p, ev

    def check(self, spec, kind):
        native_kernel_or_skip(self)
        ref_p, ref = self.evaluate(spec, kind, "numpy", np.float64, 1, 0)
        want = digest(ref)
        for threads, tblock in ((1, 0), (3, 3)):
            with self.subTest(threads=threads, tblock=tblock):
                p, ev = self.evaluate(spec, kind, "native", np.float64, threads, tblock)
                self.assertEqual(p.sim.dt, ref_p.sim.dt)
                self.assertEqual(digest(ev), want)
        _, n32 = self.evaluate(spec, kind, "numpy", np.float32, 1, 0)
        _, c32 = self.evaluate(spec, kind, "native", np.float32, 3, "auto")
        self.assertEqual(digest(c32), digest(n32))
        scale = np.max(np.abs(ref.values))
        self.assertLess(np.max(np.abs(c32.values - ref.values)) / scale, F32_VALUES)
        for k, (g32, g64) in enumerate(zip(c32.grads, ref.grads)):
            self.assertLess(np.linalg.norm(g32 - g64) / np.linalg.norm(g64), F32_GRADS, k)

    def options(self, case, kind):
        for edge, source in OPTIONS:
            with self.subTest(case=case, kind=kind, edge_correction=edge, port_source=source):
                self.check(self.spec(case, edge, source), kind)

    def test_divider(self):
        self.options("divider", "gray")

    def test_antenna(self):
        self.options("antenna", "gray")

    def test_diagonal_pattern(self):
        self.options("divider", "diagonal")

    def test_wilkinson_lumped_resistor(self):
        """The combiner: a lumped resistor and the absorbed-power requirement (the case's own
        round-2 settings)."""
        self.check(self.spec("wilkinson"), "gray")

    def test_reactive_sheet(self):
        """Gray copper as an inductive sheet (the sheet's branch currents)."""
        self.check(self.spec("divider", True, "mode", interpolation="reactive"), "gray")


if __name__ == "__main__":
    unittest.main()
