import unittest

from pnr.route.detail.coupled import length, tune


class CompactPairTuningTest(unittest.TestCase):
    def test_tuning_size_depends_on_skew_not_host_segment_length(self):
        for distance in (10.0, 30.0):
            paths = {"p": [(0, 0), (distance, 0)], "n": [(0, 0.6), (distance, 0.6)]}
            tuned = tune(
                paths, 0.2, 0.15, 0.3, lambda *args: True, {"n": 0.65}, max_tuning_length=2
            )
            self.assertIsNotNone(tuned)
            self.assertLessEqual(length(tuned["p"][1:-1]), 2)
            self.assertLessEqual(abs(length(tuned["p"]) - length(tuned["n"]) - 0.65), 0.3 + 1e-6)

    def test_tuning_limit_rejects_excessive_uncoupled_excursion(self):
        paths = {"p": [(0, 0), (30, 0)], "n": [(0, 0.6), (30, 0.6)]}
        self.assertIsNone(
            tune(paths, 0.2, 0.15, 0.3, lambda *args: True, {"n": 4}, max_tuning_length=2)
        )


if __name__ == "__main__":
    unittest.main()
