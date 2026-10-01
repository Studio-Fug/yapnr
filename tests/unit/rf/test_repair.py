"""The width and space repair before export (`export.repair`).

- a one-pixel hole is filled and a one-pixel nub removed at a two-pixel minimum;
- a copper corner contact is bridged;
- two-pixel lines and gaps, fixed pixels and copper continuing into the exterior ring (a feed)
  are kept; the result is a fixed point, passes the polygon width and space check, and a
  design without violations comes back unchanged.
"""

from __future__ import annotations

import unittest

import numpy as np

from yapnr.rf.export.contour import islands
from yapnr.rf.export.drc import check_width_space
from yapnr.rf.export.repair import pixels_for, repair

N = 16
RING = 3


def _ring(feed_rows=slice(7, 9)) -> np.ndarray:
    """An exterior ring with a 2-pixel feed entering the window from the west."""
    ring = np.zeros((N + 2 * RING, N + 2 * RING))
    ring[:RING, RING + feed_rows.start : RING + feed_rows.stop] = 1.0
    return ring


def _fix(*, pads=True):
    fixed = np.zeros((N, N), bool)
    value = np.zeros((N, N))
    if pads:
        fixed[0:2, 7:9] = True
        value[0:2, 7:9] = 1.0
    return fixed, value


def _run(m, fixed=None, value=None, ring=None):
    if fixed is None:
        fixed, value = _fix()
    ring = _ring() if ring is None else ring
    out, rounds = repair(m.astype(float), fixed, value, ring, RING, 2, 2)
    return out > 0.5, rounds


def _drc(m):
    polys = [i.polygon for i in islands(m)]
    return check_width_space(polys, (0.0, N, 0.0, N), 1.0, 2.0, 2.0)


class RepairTest(unittest.TestCase):
    def _line(self):
        m = np.zeros((N, N), bool)
        m[0:12, 7:9] = True  # the feed's line
        m[10:14, 4:12] = True  # a block at its end
        return m

    def test_clean_design_is_unchanged(self):
        m = self._line()
        out, rounds = _run(m)
        np.testing.assert_array_equal(out, m)
        self.assertEqual(rounds, 1)
        self.assertTrue(_drc(out).ok)

    def test_hole_nub_and_corner(self):
        m = self._line()
        m[11, 6] = False  # a one-pixel hole in the block
        m[14, 10] = True  # a one-pixel nub on its edge
        m[14:16, 12:14] = True  # a 2 × 2 block touching the main block at a corner only
        self.assertFalse(_drc(m).ok)
        out, _ = _run(m)
        self.assertTrue(out[11, 6])
        self.assertFalse(out[14, 10])
        self.assertTrue(out[13, 12] and out[14, 11])  # the corner contact bridged
        self.assertTrue(out[14:16, 12:14].all())
        self.assertTrue(_drc(out).ok, _drc(out).to_json())
        again, rounds = _run(out)
        np.testing.assert_array_equal(again, out)
        self.assertEqual(rounds, 1)

    def test_fixed_pixels_and_feeds_are_kept(self):
        m = np.zeros((N, N), bool)
        m[0, 7:9] = True  # one pixel of feed copper inside the window: kept by the ring
        fixed, value = _fix(pads=False)
        out, _ = _run(m, fixed, value)
        np.testing.assert_array_equal(out, m)
        fixed[5, 5], value[5, 5] = True, 1.0
        m[5, 5] = True  # an isolated fixed pixel stays
        out, _ = _run(m, fixed, value)
        self.assertTrue(out[5, 5])

    def test_two_pixel_gap_is_kept(self):
        m = np.zeros((N, N), bool)
        m[2:14, 2:5] = True
        m[2:14, 7:10] = True  # a 2-pixel gap between two 3-pixel lines
        fixed, value = _fix(pads=False)
        out, _ = _run(m, fixed, value, ring=np.zeros((N + 2 * RING, N + 2 * RING)))
        np.testing.assert_array_equal(out, m)

    def test_pixels_for(self):
        self.assertEqual(pixels_for(0.6, 0.3), 2)
        self.assertEqual(pixels_for(0.8, 0.4), 2)
        self.assertEqual(pixels_for(0.7, 0.3), 3)
        self.assertEqual(pixels_for(0.0, 0.3), 1)


if __name__ == "__main__":
    unittest.main()
