"""The optimization loop end to end on the tiny two-port spec (design §8.3, §10, §12).

- the epigraph value falls: from t = 0.31 (uniform gray start) to below 0 within the first
  epoch (measured: −0.22 after three iterations, the straight line continuing the feeds);
- two runs are bit-identical, and a run stopped and resumed from its checkpoint reproduces an
  uninterrupted run bit for bit;
- `design()` writes the footprint (which reads back and passes the width and space check), the
  Touchstone file and result.json, with the binary design meeting the spec.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from dataclasses import replace

import numpy as np

from yapnr.rf.driver import CHECKPOINT, Optimizer, design
from yapnr.rf.export.kicad import read_footprint
from yapnr.rf.export.touchstone import read_touchstone
from yapnr.rf.problem import Problem
from yapnr.rf.spec import RadiatedFraction
from yapnr.rf.testing import tiny_spec


class TinyDesignTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="rf-tiny-")
        cls.cache = os.path.join(cls.tmp, "cache")
        cls.spec = tiny_spec()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _optimizer(self, out=None):
        return Optimizer(Problem(self.spec, cache_dir=self.cache), out_dir=out)

    def test_progress_determinism_and_resume(self):
        a = self._optimizer()
        a.run(max_iterations=6)
        ts = [h["t"] for h in a.history]
        self.assertGreater(ts[0], 0.25)
        self.assertLess(min(ts), -0.1)
        b = self._optimizer()
        b.run(max_iterations=6)
        self.assertEqual([h["t"] for h in b.history], ts)
        np.testing.assert_array_equal(b.state.x, a.state.x)
        out = os.path.join(self.tmp, "resume")
        c = self._optimizer(out)
        c.run(max_iterations=3)
        self.assertTrue(os.path.exists(os.path.join(out, CHECKPOINT)))
        d = self._optimizer(out)
        d.run(max_iterations=3)  # resumes at iteration 3
        self.assertEqual(d.state.iteration, 6)
        self.assertEqual([h["t"] for h in d.history], ts)
        np.testing.assert_array_equal(d.state.x, a.state.x)
        self.assertEqual(d.state.epoch, a.state.epoch)
        for name in ("xold1", "xold2", "low", "upp"):
            np.testing.assert_array_equal(getattr(d.state.mma, name), getattr(a.state.mma, name))

    def test_design_outputs(self):
        out = os.path.join(self.tmp, "design")
        spec = self.spec.replace(
            optimizer=self.spec.optimizer.__class__(betas=(8, 32), iterations_per_beta=4)
        )
        res = design(spec, out, log=lambda *_: None)
        for name in (
            "spec.json",
            "history.json",
            "frames.npz",
            "footprint.kicad_mod",
            "coarse.s2p",
        ):
            self.assertTrue(os.path.exists(os.path.join(out, name)), name)
        with open(os.path.join(out, "result.json")) as fh:
            saved = json.load(fh)
        self.assertEqual(saved["schema"], "yapnr-rf-result/1")
        self.assertEqual(saved["spec_sha256"], spec.sha256())
        self.assertEqual(res["optimizer"]["iterations"], 8)
        self.assertTrue(res["drc"]["ok"])
        self.assertTrue(res["coarse_binary"]["all_met"], res["coarse_binary"])
        fp = read_footprint(os.path.join(out, "footprint.kicad_mod"))
        self.assertEqual(fp.net_tie_groups, ["1, 2"])
        self.assertEqual([p["number"] for p in fp.pads], ["1", "2"])
        f, s, z = read_touchstone(os.path.join(out, "coarse.s2p"))
        self.assertEqual(s.shape, (11, 2, 2))
        np.testing.assert_allclose(s[:, 0, 1], s[:, 1, 0], atol=0.02)  # reciprocal
        self.assertGreater(saved["sweep"]["passivity_margin_min"], -0.03)


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


if __name__ == "__main__":
    unittest.main()
