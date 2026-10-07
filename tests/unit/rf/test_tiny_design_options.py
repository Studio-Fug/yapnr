"""The optimizer's options on the tiny two-port spec (design §8.3, §21.3, §23.1, §24.3).

- the export is the best binarized design of the loop, not the last iterate;
- adaptive moves keep t within the slack of the current t (`trust_reference: current`) or of
  the epoch's best t (`best`), resume their move from the checkpoint and apply from
  `adaptive_from_beta` on;
- robust variants (eroded and dilated designs) join the epigraph from `robust_from_beta` on,
  their forward runs are reused, and the radiation objective and frequency continuation apply
  per epoch while binarized designs are judged by the spec at its own frequencies.

Split from test_tiny_design.py so that the two run side by side (one thread each).
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from dataclasses import replace

import numpy as np

from yapnr.rf import cases
from yapnr.rf.driver import Optimizer, design, write_json
from yapnr.rf.problem import Problem
from yapnr.rf.spec import RadiatedFraction, Spec
from yapnr.rf.testing import tiny_spec


class BestDesignTest(unittest.TestCase):
    """The export is the best binarized design of the loop, not the last iterate, and robust
    variants (eroded and dilated designs) join the epigraph."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="rf-best-")
        cls.cache = os.path.join(cls.tmp, "cache")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_exports_the_best_binary_design(self):
        spec = tiny_spec(binary_every=1)
        opt = Optimizer(Problem(spec, cache_dir=self.cache))
        opt.run(max_iterations=5)
        tb = [h["t_binary"] for h in opt.history]
        self.assertEqual(len(tb), 5)  # evaluated at every iteration
        # Make the last iterate bad: the export must still be the best tracked design.
        best_it = int(np.argmin(tb))
        opt.state.x = np.zeros_like(opt.state.x)
        fin = opt.finish()
        self.assertGreater(fin["t_binary_last"], min(tb))
        self.assertEqual(fin["export_iteration"], best_it)
        self.assertAlmostEqual(float(np.max(fin["evaluation"].values)), min(tb), places=9)
        np.testing.assert_array_equal(opt.state.export_x, fin["x"])

    def test_adaptive_move(self):
        # A 0.3 move at β = 8: plain MMA jumps to t = 17 at the third iteration on this spec
        # (round-2 measurement); the adaptive move refuses such steps, keeps t within the slack
        # and resumes the move from the checkpoint.
        spec = tiny_spec(betas=(8,), move=0.3, adaptive_move=True)
        out = os.path.join(self.tmp, "adaptive")
        opt = Optimizer(Problem(spec, cache_dir=self.cache), out_dir=out)
        opt.run(max_iterations=5)
        ts = [h["t"] for h in opt.history]
        for a, b in zip(ts, ts[1:]):
            self.assertLessEqual(b, a + 0.05 * max(1.0, abs(a)) + 1e-12)
        self.assertLess(ts[-1], -0.1)
        self.assertTrue(all(h["move"] <= 0.3 for h in opt.history))
        self.assertTrue(all(len(h["t_trials"]) >= 1 for h in opt.history))
        again = Optimizer(Problem(spec, cache_dir=self.cache), out_dir=out)
        self.assertTrue(again.load_checkpoint())
        self.assertEqual(again.state.move, opt.state.move)

    def test_adaptive_move_from_the_best(self):
        # `trust_reference: best`: every accepted point stays within the slack of the best t
        # of the epoch so far (with "current" each step may add a slack: t can creep).
        spec = tiny_spec(betas=(8,), move=0.3, adaptive_move=True, trust_reference="best")
        opt = Optimizer(Problem(spec, cache_dir=self.cache))
        opt.run(max_iterations=5)
        ts = [h["t"] for h in opt.history]
        for k in range(1, len(ts)):
            best = min(ts[:k])
            self.assertLessEqual(ts[k], best + 0.05 * max(1.0, abs(best)) + 1e-12)
        self.assertLess(ts[-1], -0.1)
        self.assertEqual(spec.to_dict()["optimizer"]["trust_reference"], "best")
        self.assertNotIn("trust_reference", tiny_spec().to_dict()["optimizer"])

    def test_adaptive_from_beta(self):
        # Plain MMA steps below `adaptive_from_beta`, adaptive ones from it on.
        spec = tiny_spec(
            betas=(8, 16), iterations_per_beta=2, adaptive_move=True, adaptive_from_beta=16.0
        )
        opt = Optimizer(Problem(spec, cache_dir=self.cache))
        opt.run(max_iterations=4)
        h = opt.history
        self.assertEqual([h_["beta"] for h_ in h], [8.0, 8.0, 16.0, 16.0])
        self.assertEqual([bool(h_["t_trials"]) for h_ in h], [False, False, True, True])
        self.assertNotIn("adaptive_from_beta", tiny_spec().to_dict()["optimizer"])

    def test_robust_from_beta(self):
        # The nominal design alone below `robust_from_beta`, the variants from it on; binarized
        # designs are judged with every variant throughout.
        spec = tiny_spec(
            betas=(8, 16), iterations_per_beta=2, eta_variants=(0.45, 0.55), robust_from_beta=16.0
        )
        opt = Optimizer(Problem(spec, cache_dir=self.cache))
        opt.run(max_iterations=4)
        h = opt.history
        self.assertEqual([len(h_.get("t_variants", [0])) for h_ in h], [1, 1, 3, 3])
        self.assertEqual(len(h[0]["keys"]), len(h[0]["f"]))
        self.assertEqual(len(h[2]["f"]), 3 * len(h[0]["f"]))
        self.assertTrue(np.isfinite(h[0]["t_binary"]))  # judged with every variant
        self.assertNotIn("robust_from_beta", tiny_spec().to_dict()["optimizer"])

    def test_epoch_objectives(self):
        # The radiation objective in the first epoch (one value per lower bound on a radiated
        # fraction), the spec's epigraph in the second; binary designs are always judged by
        # the spec.
        spec = tiny_spec(radiated=True, betas=(8, 16), iterations_per_beta=2)
        reqs = spec.requirements[:-1] + (RadiatedFraction(1).at_least(0.3, band="b"),)
        spec = spec.replace(
            requirements=reqs,
            optimizer=replace(spec.optimizer, epoch_objectives=("radiation", "spec")),
        )
        opt = Optimizer(Problem(spec, cache_dir=self.cache))
        opt.run(max_iterations=4)
        h = opt.history
        self.assertEqual([r.get("objective", "spec") for r in h], ["radiation"] * 2 + ["spec"] * 2)
        self.assertEqual(len(h[0]["f"]), 1)
        self.assertEqual(h[0]["keys"][0][0], "radiation1")
        n = len(opt.problem.groups[0].frequencies_active.nonzero()[0])
        self.assertEqual(len(h[2]["f"]), n)
        t_bin = opt.evaluate_binary(opt.state.x)[0]
        exported = opt.evaluate_binary(opt.state.x)[2]
        self.assertAlmostEqual(t_bin, opt.problem.evaluate(exported, gradients=False).t, places=12)

    def test_frequency_scale(self):
        # Frequency continuation: an epoch solves at its scaled frequencies (the same S as a
        # sweep there) and records the factor; binary designs are judged at the nominal ones.
        spec = tiny_spec(betas=(8, 16), iterations_per_beta=1)
        spec = spec.replace(optimizer=replace(spec.optimizer, epoch_frequency_scale=(0.9, 1.0)))
        prob = Problem(spec, cache_dir=self.cache)
        rho = prob.param.rho_bar(prob.param.grid.initial(0.6), 8.0)
        ev = prob.evaluate(rho, gradients=False, frequency_scale=0.9)
        sw = prob.sweep(rho, freqs=0.9 * prob.freqs, ports=[1])
        np.testing.assert_allclose(ev.s[:, 1, 0], sw["s_naive"][:, 1, 0], rtol=1e-9, atol=1e-12)
        nominal = prob.evaluate(rho, gradients=False)
        self.assertGreater(np.max(np.abs(nominal.s[:, 1, 0] - ev.s[:, 1, 0])), 1e-3)
        opt = Optimizer(prob)
        opt.run(max_iterations=2)
        self.assertEqual([h.get("frequency_scale", 1.0) for h in opt.history], [0.9, 1.0])
        exported = opt.evaluate_binary(opt.state.x)[2]
        t_bin = opt.evaluate_binary(opt.state.x)[0]
        self.assertAlmostEqual(t_bin, prob.evaluate(exported, gradients=False).t, places=12)

    def test_forward_runs_of_several_designs_are_reused(self):
        # An adaptive step evaluates the trial point's nominal and variant designs; the next
        # iteration evaluates the same designs with gradients and reuses their forward runs
        # (the cache keeps two designs per variant), with the same values and gradients.
        spec = tiny_spec(eta_variants=(0.6, 0.4))
        prob = Problem(spec, cache_dir=self.cache)
        self.assertEqual(prob.fwd_cache_size, 6)
        x = prob.param.grid.initial(0.6)
        rhos = [prob.param.rho_bar(x, 8.0, e) for e in (None, 0.6, 0.4)]
        first = prob.evaluate(rhos[0])
        for r in rhos:
            prob.evaluate(r, gradients=False)
        again = prob.evaluate(rhos[0])
        self.assertTrue(all(v == 0 for v in again.steps["forward"].values()))
        self.assertTrue(all(v > 0 for v in first.steps["forward"].values()))
        np.testing.assert_array_equal(again.values, first.values)
        np.testing.assert_allclose(again.grads, first.grads, rtol=1e-12, atol=1e-15)

    def test_robust_variants(self):
        spec = tiny_spec(eta_variants=(0.6, 0.4))
        opt = Optimizer(Problem(spec, cache_dir=self.cache))
        opt.run(max_iterations=2)
        rec = opt.history[0]
        n = len(opt.problem.groups[0].frequencies_active.nonzero()[0])
        self.assertEqual(len(rec["f"]), 3 * n * len(opt.problem.groups))
        self.assertEqual(len(rec["t_variants"]), 3)
        self.assertAlmostEqual(rec["t"], max(rec["t_variants"]), places=12)
        self.assertEqual(len(rec["keys"]), len(rec["f"]))


class ResumeTest(unittest.TestCase):
    """A run directory keeps its spec: a run started with other solver execution settings (the
    case presets said torch float32 before the native kernel became the default) resumes with
    its own spec, and a checkpoint of another design problem stops `design` before spec.json is
    replaced."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="rf-resume-")
        cls.cache = os.path.join(cls.tmp, "cache")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def run_dir(self, name, spec, checkpoint=True):
        d = os.path.join(self.tmp, name)
        os.makedirs(d)
        write_json(os.path.join(d, "spec.json"), spec.to_dict())
        if checkpoint:
            open(os.path.join(d, "checkpoint.npz"), "wb").close()
        return d

    def test_resume_spec(self):
        quiet = lambda *_: None  # noqa: E731
        preset = tiny_spec()
        old = preset.replace(
            solver=replace(preset.solver, backend="torch", dtype="float32", threads=3)
        )
        self.assertNotEqual(old.sha256(), preset.sha256())
        got = cases.resume_spec(preset, self.run_dir("old", old), log=quiet)
        self.assertEqual(got.sha256(), old.sha256())
        # Another design problem, or no checkpoint: the preset (the checkpoint check refuses
        # the former).
        other = old.replace(optimizer=replace(old.optimizer, move=0.3))
        self.assertIs(cases.resume_spec(preset, self.run_dir("other", other), log=quiet), preset)
        fresh = self.run_dir("fresh", old, checkpoint=False)
        self.assertIs(cases.resume_spec(preset, fresh, log=quiet), preset)
        self.assertIs(cases.resume_spec(preset, os.path.join(self.tmp, "none")), preset)

    def test_design_keeps_the_spec_of_a_foreign_checkpoint(self):
        spec = tiny_spec(betas=(8,), iterations_per_beta=1)
        out = os.path.join(self.tmp, "foreign")
        Optimizer(Problem(spec, cache_dir=self.cache), out_dir=out).run(max_iterations=1)
        write_json(os.path.join(out, "spec.json"), spec.to_dict())
        other = tiny_spec(betas=(8,), iterations_per_beta=1, move=0.3)
        with self.assertRaisesRegex(ValueError, "different spec"):
            design(other, out, problem=Problem(other, cache_dir=self.cache), log=None)
        with open(os.path.join(out, "spec.json"), encoding="utf-8") as fh:
            self.assertEqual(Spec.from_dict(json.load(fh)).sha256(), spec.sha256())


class UntilEpochTest(unittest.TestCase):
    """`run(until_epoch=)` (design `multistart.md` §2): pauses at an epoch boundary without
    changing state, so resuming from there is bit-identical to an uninterrupted run."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="rf-until-epoch-")
        cls.cache = os.path.join(cls.tmp, "cache")
        cls.spec = tiny_spec()  # betas=(8, 16, 32), iterations_per_beta=4: 3 epochs

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _optimizer(self, out=None):
        return Optimizer(Problem(self.spec, cache_dir=self.cache), out_dir=out)

    def test_pauses_at_the_epoch_boundary(self):
        opt = self._optimizer()
        opt.run(until_epoch=1)
        self.assertEqual(opt.state.epoch, 1)
        self.assertFalse(opt.done)  # the schedule has two more epochs
        # A no-op: already at or past the rung, so no further iterations happen.
        before = opt.state.iteration
        opt.run(until_epoch=1)
        self.assertEqual(opt.state.iteration, before)

    def test_resume_past_a_rung_matches_an_uninterrupted_run(self):
        full = self._optimizer()
        full.run()  # the whole schedule, uninterrupted
        ts_full = [h["t"] for h in full.history]

        out = os.path.join(self.tmp, "paused")
        a = self._optimizer(out)
        a.run(until_epoch=1)
        self.assertEqual(a.state.epoch, 1)
        b = self._optimizer(out)
        b.run(until_epoch=2)
        self.assertEqual(b.state.epoch, 2)
        c = self._optimizer(out)
        c.run()  # no until_epoch: runs to completion
        self.assertTrue(c.done)
        self.assertEqual([h["t"] for h in c.history], ts_full)
        np.testing.assert_array_equal(c.state.x, full.state.x)
        for name in ("xold1", "xold2", "low", "upp"):
            np.testing.assert_array_equal(getattr(c.state.mma, name), getattr(full.state.mma, name))

    def test_until_epoch_beyond_the_schedule_runs_to_completion(self):
        opt = self._optimizer()
        opt.run(until_epoch=100)
        self.assertTrue(opt.done)


if __name__ == "__main__":
    unittest.main()
