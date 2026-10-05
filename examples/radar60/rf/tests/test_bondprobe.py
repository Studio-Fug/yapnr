"""Tests of the plane-pair probes of ant_sim.py `--probe-bond` (openems/bondprobe.py): the probe
sites (the same for every variant of a comparison, clear of vias, no duplicates) and the
ring-down decomposition (frequency and Q of synthetic damped sinusoids).

Run from examples/radar60/rf:  python3 -m unittest discover -s tests -v   (needs numpy)
"""

from __future__ import annotations

import math
import os
import sys
import unittest

try:
    import numpy as np
except ImportError:  # the generator's tests need the standard library only
    np = None

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "openems")
)


def bank_model(vias):
    """Two banks of five columns at 2.342 mm, the RX cut-out left of the TX one."""
    pc = {f"R{k}": [-4.0 + 2.342 * k, 12.0] for k in range(5)}
    pc.update({f"T{k}": [14.0 + 2.342 * k, 10.0] for k in range(5)})
    return dict(
        cutouts={"RX": [-5.5, 8.5, 6.5, 15.0], "TX": [12.5, 6.8, 24.5, 13.2]},
        phase_centres=pc,
        vias=vias,
    )


@unittest.skipUnless(np is not None, "needs numpy")
class ProbeSitesTest(unittest.TestCase):
    def test_sites_are_variant_independent_and_clear_of_vias(self):
        import bondprobe

        a = bondprobe.points(bank_model([]))
        names = [p["name"] for p in a]
        self.assertEqual(len(names), len(set(names)))
        self.assertIn("pb_between", names)
        self.assertEqual(sum(p["pair"] == "core" for p in a), 5)
        # a via on a site moves only that site, by less than 0.3 mm
        mid = next(p for p in a if p["name"] == "pb_RX_mid")
        b = bondprobe.points(bank_model([[mid["at"][0], mid["at"][1], 0.15]]))
        for pa, pb in zip(a, b):
            self.assertEqual(pa["name"], pb["name"])
            if pa["name"] == "pb_RX_mid":
                self.assertGreater(pb["at"][0] - pa["at"][0], 0.15)
                self.assertLess(pb["at"][0] - pa["at"][0], 0.3)
            else:
                self.assertEqual(pa["at"], pb["at"])

    def test_triplet_has_no_duplicate_site(self):
        import bondprobe

        m = dict(
            cutouts={"RX": [-5.2, 0.0, 3.3, 6.3]},
            phase_centres={"T0": [-3.513, 3.3], "T1": [-1.171, 3.3], "T2": [1.171, 3.3]},
            vias=[],
        )
        ats = [tuple(p["at"]) + (p["pair"],) for p in bondprobe.points(m)]
        self.assertEqual(len(ats), len(set(ats)))


@unittest.skipUnless(np is not None, "needs numpy")
class RingdownTest(unittest.TestCase):
    def test_frequency_and_q_of_damped_sinusoids(self):
        import bondprobe

        dt = 3e-14
        t = np.arange(0, 5e-9, dt)

        def ds(f, q, a, t0=0.3e-9):
            tt = np.maximum(t - t0, 0)
            return a * np.exp(-math.pi * f / q * tt) * np.cos(2 * math.pi * f * tt) * (t > t0)

        v = np.exp(-(((t - 0.16e-9) / 0.05e-9) ** 2)) * np.cos(2 * math.pi * 62e9 * t)
        v = v + ds(62.3e9, 30, 0.05) + ds(66e9, 8, 0.2)
        poles, late, _, window = bondprobe.ringdown(t, v, 0.42e-9)
        self.assertGreater(window, 4e-9)
        hi = [p for p in poles if p[2] > 1e-3 and p[1] >= 20]
        self.assertEqual(len(hi), 1)
        self.assertAlmostEqual(hi[0][0] / 1e9, 62.3, delta=0.05)
        self.assertAlmostEqual(hi[0][1], 30, delta=1.5)
        self.assertLess(late["1.0ns"], -40)


if __name__ == "__main__":
    unittest.main()
