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

import numpy as np

from yapnr.rf.driver import CHECKPOINT, Optimizer, design
from yapnr.rf.export.kicad import read_footprint
from yapnr.rf.export.touchstone import read_touchstone
from yapnr.rf.problem import Problem
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


if __name__ == "__main__":
    unittest.main()
