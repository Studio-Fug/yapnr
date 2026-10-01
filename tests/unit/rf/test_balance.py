"""The power balance of radiators and the convergence trend of the validator (§11.3, §11.5).

- On the tiny two-port spec with its radiation box, a gray (lossy) design and the straight
  line: the port's net input power equals what leaves the box closed by the ground plus the
  other port's power plus the dissipation inside, within the port model's error (measured:
  below 1 % for the line, a few per cent with a lossy gray sheet); the dissipation is positive
  and grows with the gray sheet.
- `convergence` lists each check's worst value per grid and the last change; checks of kind
  "balance" are judged and reported per grid, not trended.
"""

from __future__ import annotations

import unittest

import numpy as np

from yapnr.rf import cases
from yapnr.rf.problem import Problem
from yapnr.rf.testing import tiny_spec
from yapnr.rf.validate import convergence, power_balance


class PowerBalanceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = tiny_spec(radiated=True)
        cls.p = Problem(spec, exact=True)
        ni, nj = cls.p.design_shape
        line = np.zeros((ni, nj))
        pg = cls.p.ports[1]
        j0 = cls.p.domain.window[2]
        line[:, pg.ta - j0 : pg.tb - j0] = 1.0
        freqs = np.array([9e9, 11e9])
        cls.line = power_balance(cls.p, line, freqs)
        cls.gray = power_balance(cls.p, np.full((ni, nj), 0.5), freqs)

    def test_line_balances(self):
        err = np.abs(self.line["error"])
        self.assertLess(err.max(), 0.02, self.line)
        self.assertGreater(min(self.line["ports"]), 0.9)  # most of it reaches port 2

    def test_gray_sheet_dissipates(self):
        self.assertGreater(min(self.gray["diss"]), max(self.line["diss"]))
        self.assertLess(np.abs(self.gray["error"]).max(), 0.05, self.gray)

    def test_fractions_are_consistent(self):
        for b in (self.line, self.gray):
            total = np.array(b["closed"]) + np.array(b["diss"]) + np.array(b["ports"]) - 1.0
            np.testing.assert_allclose(total, b["error"], atol=1e-12)
            # The open-bottom box of the objectives sees at most the closed box's flux.
            self.assertTrue(np.all(np.array(b["eta"]) <= np.array(b["closed"]) + 0.02))


class ConvergenceTest(unittest.TestCase):
    def test_trend(self):
        checks = [
            cases.Check("a", "s_max", (1, 1), -10.0),
            cases.Check("b", "balance", (1,), 0.02, at_ghz=(10.0,)),
        ]
        levels = {
            1: {"checks": [{"name": "a", "worst": -20.0}, {"name": "b", "worst": 0.01}]},
            2: {"checks": [{"name": "a", "worst": -18.0}]},
            3: {"checks": [{"name": "a", "worst": -16.5}]},
        }
        out = convergence(levels, checks)
        self.assertEqual(out["refine"], [1, 2, 3])
        self.assertEqual(out["checks"], {"a": [-20.0, -18.0, -16.5]})
        self.assertAlmostEqual(out["last_change"]["a"], 1.5)


if __name__ == "__main__":
    unittest.main()
