"""Closed-form starting designs (`seeds`).

- the patch dimensions reproduce Balanis' worked example (Antenna Theory, 4th ed., Example
  14.1: εr 2.2, h 1.588 mm, 10 GHz → W 11.86 mm, L 9.06 mm) within 1 %;
- the antenna preset's seed is a mirror-symmetric inset patch joined to the port's feed, with
  gaps of the minimum space beside the inset feed, on both scales; a port on the east side
  mirrors it.
"""

from __future__ import annotations

import unittest
from dataclasses import replace
from types import SimpleNamespace

import numpy as np

from yapnr.rf.cases import spec_for
from yapnr.rf.seeds import patch_dimensions, patch_mask


def _problem(spec, shape, width, pitch_m):
    return SimpleNamespace(spec=spec, pitch=pitch_m, design_shape=shape, widths={1: width})


class PatchSeedTest(unittest.TestCase):
    def test_balanis_example(self):
        d = patch_dimensions(2.2, 1.588e-3, 10e9)
        self.assertAlmostEqual(d["w"] * 1e3, 11.86, delta=0.12)
        self.assertAlmostEqual(d["l"] * 1e3, 9.06, delta=0.09)
        self.assertGreater(d["inset"], 0.0)
        self.assertLess(d["inset"], 0.5 * d["l"])

    def test_antenna_seed(self):
        spec = spec_for("antenna")
        m = patch_mask(_problem(spec, (45, 45), 9, 0.4e-3))
        np.testing.assert_array_equal(m, m[:, ::-1])
        # The 9-pixel feed runs from the port (x = 0) into the patch.
        self.assertTrue(m[0:31, 18:27].all())
        self.assertFalse(m[31:].any())
        # The patch: 25 pixels (10 mm) wide, 18 (7.2 mm) long, from pixel 13.
        self.assertEqual(int(m[20].sum()), 25)
        self.assertEqual(int(m[:, 11].sum()), 18)
        # The inset: 6 pixels (2.4 mm) deep, two-pixel (0.8 mm) gaps beside the feed.
        self.assertFalse(m[13:19, 16:18].any())
        self.assertFalse(m[13:19, 27:29].any())
        self.assertTrue(m[19, 16:18].all())
        smoke = spec_for("antenna", "smoke")
        ms = patch_mask(_problem(smoke, (10, 10), 4, 0.8e-3))
        np.testing.assert_array_equal(ms, ms[:, ::-1])
        self.assertTrue(ms[0, 3:7].all())

    def test_east_port_mirrors(self):
        spec = spec_for("antenna")
        east = spec.replace(ports=(replace(spec.ports[0], side="E"),))
        mw = patch_mask(_problem(spec, (45, 45), 9, 0.4e-3))
        me = patch_mask(_problem(east, (45, 45), 9, 0.4e-3))
        np.testing.assert_array_equal(me, mw[::-1])

    def test_needs_one_port(self):
        with self.assertRaises(ValueError):
            patch_mask(_problem(spec_for("divider"), (32, 40), 6, 0.3e-3))


if __name__ == "__main__":
    unittest.main()
