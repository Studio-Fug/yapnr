"""The joint fit on synthetic sessions (design §8.5-§8.8, §10): the fast CI part of the
synthetic-recovery validation. The full study (50 draws per board, 12 GHz) is manual:
`python -m yapnr.rf.coupons study` (docs/rf-fab-coupons.md)."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

import numpy as np

from yapnr.rf.coupons import export, fit, models, session, stackups, synthetic

A = "JLC04161H-7628"
B = "JLC06161H-7628"


def _session(sid, out, seed, imp=None, f_max=6e9, step=20e6, **kw):
    st = stackups.get(sid)
    rng = np.random.default_rng(seed)
    truth = synthetic.draw_truth(st, rng)
    synthetic.simulate(sid, truth, out, synthetic.grid(f_max, step), rng, imp=imp, **kw)
    return truth


class NoiseFreeTest(unittest.TestCase):
    """Identical connectors and no noise: the fit must return the truth (the model is the
    simulator's, so only the optimizer is tested)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_board_a(self):
        truth = _session(A, self.dir, 3, imp=synthetic.ideal(), microsection=False)
        d = fit.prepare(A, session.load(self.dir))
        d.dc = []  # the synthetic DC readings carry noise
        res = fit.run_fit(fit.Predictor(d))
        for n in res.names:
            if n in ("mask.df", "pp.df", "L1.rough", "L1.enig"):
                continue  # loss parameters: weakly determined below 6 GHz
            # the MAP estimate is shrunk toward the prior by about (σ/σ_prior)² of the offset
            self.assertLess(abs(res.value[n] - truth[n]), 0.5 * res.sigma[n] + 1e-9, n)


class RecoveryTest(unittest.TestCase):
    """Realistic sessions (soldered connectors, SOLT residuals, noise, drift; design §10 steps
    3-4) at 6 GHz: the fitted values agree with the truth within the reported uncertainty
    (fit σ and a small parametric bootstrap)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _recover(self, sid, seeds):
        zs = []
        for seed in seeds:
            ses = os.path.join(self.dir, f"s{seed}")
            truth = _session(sid, ses, seed)
            res, d, p = fit.extract(sid, ses, with_systematics=False, boot=4)
            zs += [(res.value[n] - truth[n]) / res.sigma[n] for n in res.names]
            self.assertFalse(d.problems, d.problems)
            for name, block in res.chi2.items():
                self.assertLess(block["rms"], 3.0, name)
            # what the product sees: Z0 and εeff of the TRL families at 5.8 GHz
            m = models.Model(sid, stackups.with_values(stackups.get(sid), truth), np.array([5.8e9]))
            for set_id, t in d.board.trl.items():
                line = m.line(t["family"])
                z0, sz = res.derived[f"{t['family']}.z0_ohm_5g8"]
                ee, se = res.derived[f"{t['family']}.eps_eff_5g8"]
                self.assertLess(abs(z0 - abs(line.zc[0])), 3 * sz + 0.05, (seed, set_id))
                self.assertLess(abs(ee - line.eps_eff[0]), 3 * se + 0.002, (seed, set_id))
        zs = np.abs(np.array(zs))
        self.assertGreaterEqual(np.mean(zs <= 2.0), 0.8)
        return res, d, p

    def test_board_a(self):
        res, d, p = self._recover(A, (11, 12))
        self.assertLess(res.derived["P.eps_eff_5g8"][1], 0.01)
        self.assertLess(res.derived["P.z0_ohm_5g8"][1], 1.0)

    def test_board_b(self):
        self._recover(B, (21,))

    def test_mask_product_only(self):
        """Design §8.8: RF data identify the mask's Dk × thickness, not each; the fit reports
        their strong anticorrelation."""
        ses = os.path.join(self.dir, "m")
        _session(A, ses, 31, microsection=False)
        res = fit.run_fit(fit.Predictor(fit.prepare(A, session.load(ses))))
        c = res.corr()
        i, j = res.names.index("mask.scale"), res.names.index("mask.dk")
        self.assertLess(c[i, j], -0.85)
        self.assertTrue(
            any(w.startswith("mask.scale and mask.dk correlated") for w in res.warnings)
        )


class ExportTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_records(self):
        ses = os.path.join(self.dir, "ses")
        _session(A, ses, 41)
        res, d, p = fit.extract(A, ses, with_systematics=True)
        self.assertIn("roughness=groiss", res.systematic)
        paths = export.write_all(os.path.join(self.dir, "out"), res, d, p, ses)
        import json

        with open(paths["fit.json"], encoding="utf-8") as fh:
            rec = json.load(fh)
        self.assertEqual(rec["schema"], "yapnr-stackup-fit/1")
        self.assertEqual(rec["covariance"]["order"], res.names)
        self.assertEqual(len(rec["covariance"]["matrix"]), len(res.names))
        self.assertTrue(set(res.names) <= set(rec["parameters"]))
        self.assertNotIn("truth.json", rec["provenance"]["data_sha256"])
        ad = rec["rf_adapter"]
        self.assertTrue(3.5 < ad["er"] < 5.5 and 1e7 < ad["sigma_cu"] < 6e7, ad)
        with open(paths["stackup-overlay.json"], encoding="utf-8") as fh:
            ov = json.load(fh)
        self.assertEqual(ov["measured"]["source"], "coupon-fit")
        with open(paths["report.md"], encoding="utf-8") as fh:
            text = fh.read()
        for heading in ("Derived at 5.8 GHz", "Held-out structures", "Residual blocks"):
            self.assertIn(heading, text)


if __name__ == "__main__":
    unittest.main()
