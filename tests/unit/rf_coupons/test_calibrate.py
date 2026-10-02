"""Multiline TRL, the TDR impedance and the verification line (design §8.3, §8.4)."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

import numpy as np

from yapnr.rf.coupons import calibrate, catalog, fit, models, session, stackups, synthetic

A = "JLC04161H-7628"
B = "JLC06161H-7628"


class MultilineTRLTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _exact(self, sid):
        st = stackups.get(sid)
        truth = synthetic.draw_truth(st, np.random.default_rng(5))
        f = synthetic.grid(12e9, 50e6)
        synthetic.simulate(sid, truth, self.dir, f, np.random.default_rng(5), imp=synthetic.ideal())
        d = fit.prepare(sid, session.load(self.dir))
        p = fit.Predictor(d)
        m = p.model(stackups.with_values(st, truth), d.f)
        for set_id, g in d.gamma.items():
            g_true = p.gamma(m, set_id)
            np.testing.assert_allclose(g, g_true, rtol=1e-6, err_msg=set_id)
        k = np.isin(d.f_grid, d.f)
        for sid_, s in d.corrected.items():
            if d.board.stick(sid_).elements:
                np.testing.assert_allclose(s[k], p.stick_s(m, sid_), atol=1e-6, err_msg=sid_)
        for v in d.verify.values():
            self.assertLess(v["max_dev"], 1e-6)
        return d

    def test_board_a_exact(self):
        d = self._exact(A)
        self.assertEqual(set(d.gamma), {"P"})
        self.assertGreater(d.trl["P"].conditioning[d.f_grid > 1e9].min(), 0.9)

    def test_board_b_exact(self):
        d = self._exact(B)
        self.assertEqual(set(d.gamma), {"S", "PB"})

    def test_scikit_rf_agrees(self):
        """scikit-rf's NISTMultilineTRL (Marks 1991) as the cross-check, when installed."""
        try:
            import skrf  # noqa: F401
        except ImportError:
            self.skipTest("scikit-rf not installed (optional)")
        st = stackups.get(A)
        f = synthetic.grid(6e9, 50e6)
        synthetic.simulate(A, {}, self.dir, f, np.random.default_rng(2), imp=synthetic.ideal())
        ses = session.load(self.dir)
        b = catalog.board(A)
        t = b.trl["P"]
        sticks = {b.stick(s).dl: s for s in t["sticks"]}
        lines = [(dl, ses.mean(s)) for dl, s in sticks.items() if dl > 0]
        refl = ses.mean(t["reflect"])
        g = calibrate.skrf_gamma(f, ses.mean(sticks[0.0]), lines, refl, 3.2)
        mine = calibrate.multiline_trl(f, ses.mean(sticks[0.0]), lines, refl, 3.2).gamma
        k = f > 0.5e9
        np.testing.assert_allclose(g[k].imag, mine[k].imag, rtol=1e-4)
        self.assertLess(np.max(abs(g[k].real - mine[k].real)), 0.02)
        del st


class TDRTest(unittest.TestCase):
    def test_constant_line_plateau(self):
        f = synthetic.grid(6e9, 20e6)
        for z in (40.0, 50.7, 62.0):
            line = synthetic._const_line(f, 3.15, z)
            s = models.abcd_to_s(models.abcd_line(line, 120.0), 50.0)
            z_tdr = calibrate.tdr_z0(f, s[:, 0, 0], 3.15, 10.0, 100.0)
            self.assertAlmostEqual(z_tdr, z, delta=0.02)

    def test_launch_does_not_bias(self):
        """A 47 Ω pad before the line leaves the plateau alone (the step is integrated from
        before t = 0)."""
        f = synthetic.grid(6e9, 20e6)
        line = synthetic._const_line(f, 3.15, 50.7)
        pad = synthetic._const_line(f, 2.8, 47.0)
        s = models.abcd_to_s(
            models.cascade(models.abcd_line(pad, 3.2), models.abcd_line(line, 120.0)), 50.0
        )
        self.assertAlmostEqual(calibrate.tdr_z0(f, s[:, 0, 0], 3.15, 10.0, 100.0), 50.7, delta=0.05)


class QualityTest(unittest.TestCase):
    def test_passivity(self):
        f = np.linspace(1e9, 6e9, 20)
        s = np.zeros((len(f), 2, 2), complex)
        s[:, 0, 1] = s[:, 1, 0] = 0.9
        self.assertTrue(calibrate.quality(s)["passive"])
        s[:, 0, 1] = s[:, 1, 0] = 1.1
        self.assertFalse(calibrate.quality(s)["passive"])


if __name__ == "__main__":
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    unittest.main()
