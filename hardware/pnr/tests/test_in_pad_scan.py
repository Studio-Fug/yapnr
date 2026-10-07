"""PNR_IN_PAD_SCAN: the sites a plane pad's filled in-pad via tries (pure geometry)."""

import math
import os
import unittest
from unittest.mock import patch

from pnr.via_in_pad import local
from pnr.writeback import IN_PAD_SCAN_STEP_MM, in_pad_scan_points


class InPadScanTest(unittest.TestCase):
    FRAME = ((48.25, 59.625), (2.6, 1.0), 30.0, 0.0)

    def test_the_land_centre_only_by_default(self):
        with patch.dict(os.environ, {"PNR_IN_PAD_SCAN": ""}):
            self.assertEqual(in_pad_scan_points(self.FRAME), [(48.25, 59.625)])

    def test_the_whole_land_nearest_the_centre_first(self):
        with patch.dict(os.environ, {"PNR_IN_PAD_SCAN": "1"}):
            points = in_pad_scan_points(self.FRAME)
        centre, (w, h), angle, _ = self.FRAME
        self.assertEqual(points[0], centre)
        nx, ny = int(w / 2 / IN_PAD_SCAN_STEP_MM), int(h / 2 / IN_PAD_SCAN_STEP_MM)
        self.assertEqual(len(points), (2 * nx + 1) * (2 * ny + 1))
        self.assertEqual(len(set((round(x, 9), round(y, 9)) for x, y in points)), len(points))
        distances = [round(math.dist(p, centre), 9) for p in points]
        self.assertEqual(distances, sorted(distances))
        # Every point lies on the land in its own (rotated) frame, on the lattice.
        for p in points:
            u, v = local(p, centre, angle)
            self.assertLessEqual(abs(u), w / 2 + 1e-9)
            self.assertLessEqual(abs(v), h / 2 + 1e-9)
            for t in (u, v):
                self.assertAlmostEqual(t / IN_PAD_SCAN_STEP_MM, round(t / IN_PAD_SCAN_STEP_MM))


if __name__ == "__main__":
    unittest.main()
