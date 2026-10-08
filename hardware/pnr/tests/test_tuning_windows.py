import os
import unittest
from unittest.mock import patch

from pnr.route.detail.coupled import geometry_ok, length, tune


class TuningWindowsTest(unittest.TestCase):
    paths = {"P": [(0, 0), (20, 0)], "N": [(0, -2), (20, -2)]}

    def test_endpoint_site_when_midpoint_is_blocked(self):
        def clear(net, a, b, w):
            return net != "P" or not (
                max(a[1], b[1]) > 0.1 and min(a[0], b[0]) < 13 and max(a[0], b[0]) > 7
            )

        with patch.dict(os.environ, {"PNR_TUNE_WINDOW_SEARCH": "0"}):
            self.assertIsNone(tune(self.paths, 0.25, 0.2, 0.5, clear, {"N": 2}))
        with patch.dict(os.environ, {"PNR_TUNE_WINDOW_SEARCH": "1"}):
            result = tune(self.paths, 0.25, 0.2, 0.5, clear, {"N": 2})
        self.assertIsNotNone(result)
        self.assertTrue(geometry_ok(result, 0.25, 0.2, clear))
        self.assertLessEqual(abs(length(result["P"]) - length(result["N"]) - 2), 0.5)
        self.assertEqual(result["P"][0], self.paths["P"][0])
        self.assertEqual(result["P"][-1], self.paths["P"][-1])

    def test_several_small_bumps_when_one_large_bump_does_not_fit(self):
        def clear(net, a, b, w):
            return net != "P" or max(a[1], b[1]) <= 1.0

        with patch.dict(os.environ, {"PNR_TUNE_WINDOW_SEARCH": "1"}):
            result = tune(self.paths, 0.25, 0.2, 0.5, clear, {"N": 2})
        self.assertIsNotNone(result)
        self.assertTrue(geometry_ok(result, 0.25, 0.2, clear))
        self.assertLessEqual(abs(length(result["P"]) - 22), 0.5)

    def test_compact_window_fits_cap_that_excludes_centered_trombone(self):
        with patch.dict(os.environ, {"PNR_TUNE_WINDOW_SEARCH": "1"}):
            result = tune(
                self.paths, 0.25, 0.2, 0.5, lambda *args: True, {"N": 2}, max_tuning_length=2.0
            )
        self.assertIsNotNone(result)
        self.assertTrue(geometry_ok(result, 0.25, 0.2, lambda *args: True))

    def test_baseline_exemption_does_not_ignore_changed_copper(self):
        def blocked(*args):
            return False

        self.assertTrue(geometry_ok(self.paths, 0.25, 0.2, blocked, self.paths))
        changed = dict(self.paths, P=[(0, 0), (10, 1), (20, 0)])
        self.assertFalse(geometry_ok(changed, 0.25, 0.2, blocked, self.paths))

    def test_tuning_length_cap_is_not_bypassed_by_split_bumps(self):
        with patch.dict(os.environ, {"PNR_TUNE_WINDOW_SEARCH": "1"}):
            self.assertIsNone(
                tune(
                    self.paths,
                    0.25,
                    0.2,
                    0.5,
                    lambda *args: True,
                    {"N": 2},
                    max_tuning_length=0.2,
                )
            )


if __name__ == "__main__":
    unittest.main()
