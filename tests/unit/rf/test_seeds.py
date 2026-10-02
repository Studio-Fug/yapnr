"""Closed-form starting designs (`seeds`).

- the patch dimensions reproduce Balanis' worked example (Antenna Theory, 4th ed., Example
  14.1: εr 2.2, h 1.588 mm, 10 GHz → W 11.86 mm, L 9.06 mm) within 1 %;
- the patch reference's seed (`cases.antenna_patch_reference`) is a mirror-symmetric inset
  patch joined to the port's feed, with slots of 1.5 times the minimum space beside the inset
  feed, on both scales; a port on the east side mirrors it;
- the star seed joins every port of the divider and the diplexer in one copper island;
- the stub seed adds the diplexer's two quarter-wave stubs, one island, the minimum space kept.
"""

from __future__ import annotations

import unittest
from dataclasses import replace
from types import SimpleNamespace

import numpy as np

from yapnr.rf.cases import antenna_patch_reference, spec_for
from yapnr.rf.seeds import PATCH_FAMILY, patch_dimensions, patch_mask, star_mask


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
        spec = antenna_patch_reference()
        m = patch_mask(_problem(spec, (45, 45), 9, 0.4e-3))
        np.testing.assert_array_equal(m, m[:, ::-1])
        # The 9-pixel feed runs from the port (x = 0) into the patch.
        self.assertTrue(m[0:31, 18:27].all())
        self.assertFalse(m[31:].any())
        # The patch: 25 pixels (10 mm) wide, 18 (7.2 mm) long, from pixel 13.
        self.assertEqual(int(m[20].sum()), 25)
        self.assertEqual(int(m[:, 11].sum()), 18)
        # The inset: 6 pixels (2.4 mm) deep, slots of 3 pixels (1.5 × 0.8 mm) beside the feed.
        self.assertFalse(m[13:19, 15:18].any())
        self.assertFalse(m[13:19, 27:30].any())
        self.assertTrue(m[13:19, 14].all() and m[13:19, 30].all())
        self.assertTrue(m[19, 15:18].all())
        smoke = antenna_patch_reference("smoke")
        ms = patch_mask(_problem(smoke, (10, 10), 4, 0.8e-3))
        np.testing.assert_array_equal(ms, ms[:, ::-1])
        self.assertTrue(ms[0, 3:7].all())

    def test_whole_pixel_neighbours(self):
        spec = antenna_patch_reference()
        prob = _problem(spec, (45, 45), 9, 0.4e-3)
        base = patch_mask(prob)
        longer = patch_mask(prob, dl=1)
        wider = patch_mask(prob, dw=2)
        deeper = patch_mask(prob, di=1)
        self.assertEqual(int(longer[:, 11].sum()), int(base[:, 11].sum()) + 1)
        self.assertEqual(int(wider[20].sum()), int(base[20].sum()) + 2)
        np.testing.assert_array_equal(wider, wider[:, ::-1])
        self.assertEqual(int(deeper.sum()), int(base.sum()) - 6)  # one row of both 3-px slots
        self.assertEqual(len(PATCH_FAMILY), 27)

    def test_east_port_mirrors(self):
        spec = antenna_patch_reference()
        east = spec.replace(ports=(replace(spec.ports[0], side="E"),))
        mw = patch_mask(_problem(spec, (45, 45), 9, 0.4e-3))
        me = patch_mask(_problem(east, (45, 45), 9, 0.4e-3))
        np.testing.assert_array_equal(me, mw[::-1])

    def test_star_joins_every_port(self):
        from yapnr.rf.export.raster import label

        for case, shape in (("divider", (32, 40)), ("diplexer", (50, 50))):
            spec = spec_for(case)
            prob = _problem(spec, shape, 6, 0.3e-3)
            prob.widths = {1: 6, 2: 6, 3: 6}
            m = star_mask(prob) > 0.5
            lab, n = label(m)
            self.assertEqual(n, 1, case)
            # Each port's pad pixels (the first two along its axis) are copper.
            self.assertTrue(m[0:2, shape[1] // 2 - 3 : shape[1] // 2 + 3].all(), case)
            self.assertTrue(m[-2:].any(axis=1).all(), case)
        np.testing.assert_array_equal(m, m[:, ::-1])  # the diplexer's ports are symmetric

    def test_stub_seed(self):
        from yapnr.rf.export.raster import label
        from yapnr.rf.seeds import channel_plan, stub_mask

        spec = spec_for("diplexer")
        self.assertEqual(
            {k: (f, tuple(o)) for k, (f, o) in channel_plan(spec).items()},
            {2: (8e9, (12e9,)), 3: (12e9, (8e9,))},
        )
        prob = _problem(spec, (50, 50), 6, 0.3e-3)
        prob.widths = {1: 6, 2: 6, 3: 6}
        star = star_mask(prob) > 0.5
        m = stub_mask(prob) > 0.5
        self.assertTrue(m[star].all())
        added = int(m.sum() - star.sum())
        # Two quarter-wave stubs of the feed width: 12 GHz (11 px) and 8 GHz (17 px) long.
        self.assertEqual(added, 6 * (11 + 17))
        lab, n = label(m)
        self.assertEqual(n, 1)
        # The minimum space (two pixels) holds between the stubs and the rest of the copper:
        # no void gap of one pixel between copper pixels, along x or y.
        for arr in (m, m.T):
            gap = arr[:-2] & ~arr[1:-1] & arr[2:]
            self.assertFalse(gap.any())

    def test_needs_one_port(self):
        with self.assertRaises(ValueError):
            patch_mask(_problem(spec_for("divider"), (32, 40), 6, 0.3e-3))


if __name__ == "__main__":
    unittest.main()
