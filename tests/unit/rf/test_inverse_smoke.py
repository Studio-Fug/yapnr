"""One real native forward/adjoint/MMA step, repeated to check determinism."""

from __future__ import annotations

import tempfile
import unittest

import numpy as np

from yapnr.rf.driver import Optimizer
from yapnr.rf.problem import Problem
from yapnr.rf.testing import native_kernel_or_skip, tiny_spec


class InverseSmokeTest(unittest.TestCase):
    def test_native_step_changes_design_and_repeats_exactly(self):
        native_kernel_or_skip(self)
        runs = []
        for _ in range(2):
            # Independent calibration caches: the second run cannot reuse an evaluation.
            with tempfile.TemporaryDirectory() as cache:
                problem = Problem(tiny_spec(), cache_dir=cache, backend="native", threads=1)
                self.assertEqual(problem.backend, "native")
                optimizer = Optimizer(problem)
                initial = optimizer.state.x.copy()
                optimizer.run(max_iterations=1)
                self.assertEqual(optimizer.state.iteration, 1)
                self.assertEqual(len(optimizer.history), 1)
                record = optimizer.history[0]
                self.assertTrue(record["accepted"])
                self.assertFalse(record["reused_evaluation"])
                self.assertTrue(np.isfinite(record["f"]).all())
                self.assertTrue(np.isfinite(record["t"]).all())
                self.assertGreater(record["change"], 0)
                self.assertFalse(np.array_equal(initial, optimizer.state.x))
                for phase in ("forward", "adjoint"):
                    self.assertTrue(record["steps"][phase])
                    self.assertTrue(all(n > 0 for n in record["steps"][phase].values()))
                runs.append((optimizer.state.x.copy(), record["f"], record["t"]))
        for first, second in zip(*runs):
            np.testing.assert_array_equal(first, second)


if __name__ == "__main__":
    unittest.main()
