"""The radiated-power checks of the validator and its convergence trend (§11.5, §25.5).

- On a straight two-port (`testing.line_spec`) with its closed radiation box, a gray (lossy)
  design and the straight line: the closed-box identity (η + |Γ|² + the other port's power +
  the dissipation inside the box, all on the faces, = 1) holds (measured: 1e-7 for the line,
  4–6e-4 for the gray sheet, the modal waves' cross terms), the dissipation is positive and
  grows with the gray sheet, and the port's incident power does not depend on the design
  (modal extraction against an empty design region: measured 0.1–5e-4).
- `convergence` lists each check's worst value per grid and the last change; checks of kind
  "balance" and "incident" are judged and reported per grid, not trended.
"""

from __future__ import annotations

import unittest

import numpy as np

from yapnr.rf import cases
from yapnr.rf.problem import Problem
from yapnr.rf.testing import line_spec, straight_line
from yapnr.rf.validate import convergence, power_balance


class PowerBalanceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.p = Problem(line_spec())
        line = straight_line(cls.p)
        freqs = np.array([9e9, 11e9])
        cls.line = power_balance(cls.p, line, freqs)
        cls.gray = power_balance(cls.p, np.full(cls.p.design_shape, 0.5), freqs)

    def test_line_balances(self):
        err = np.abs(self.line["error"])
        self.assertLess(err.max(), 1e-5, self.line)
        self.assertGreater(min(self.line["ports"]), 0.9)  # most of it reaches port 2

    def test_gray_sheet_dissipates(self):
        self.assertGreater(min(self.gray["diss"]), max(self.line["diss"]))
        self.assertLess(np.abs(self.gray["error"]).max(), 1e-3, self.gray)

    def test_incident_power_is_design_independent(self):
        for b in (self.line, self.gray):
            self.assertLess(np.abs(b["incident_error"]).max(), 1e-3, b)

    def test_fractions_are_consistent(self):
        for b in (self.line, self.gray):
            total = (
                np.array(b["eta"])
                + np.array(b["reflected"])
                + np.array(b["diss"])
                + np.array(b["ports"])
                - 1.0
            )
            np.testing.assert_allclose(total, b["error"], atol=1e-12)
            self.assertEqual(sorted(b["faces"]), ["x+", "x-", "y+", "y-", "z+"])


class ConvergenceTest(unittest.TestCase):
    def test_trend(self):
        checks = [
            cases.Check("a", "s_max", (1, 1), -10.0),
            cases.Check("b", "balance", (1,), 0.005, at_ghz=(10.0,)),
            cases.Check("c", "incident", (1,), 1e-3, at_ghz=(10.0,)),
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
