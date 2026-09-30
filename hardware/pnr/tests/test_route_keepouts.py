"""Real-board routing must preserve the same enclosure exclusions as writeback."""

import unittest

from pnr.graph import BoardGraph, Component
from pnr.route.detail.grid import RouteGrid
from pnr.route.detail.router import _mark_copper_keepouts, _track_halo


class CopperKeepoutTest(unittest.TestCase):
    def test_mount_blocks_all_layers_and_via_radius(self):
        g = RouteGrid(20, 20, 0.25, layers=("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"), via_radius=0.3)
        _mark_copper_keepouts(
            g, BoardGraph("t"), {"mounting_holes": [{"at": [5, 5], "clearance_diameter_mm": 7}]}
        )
        for la in range(4):
            self.assertFalse(g.passable(la, *g.cell_of(5, 5)))
            self.assertFalse(g.via_passable(la, *g.cell_of(8.6, 5)))
            self.assertTrue(g.passable(la, *g.cell_of(12, 12)))

    def test_rotated_case_keepout_follows_component(self):
        comp = Component("SW1", "switch", (10, 10), 90, "top", (4, 4), (4, 4))
        g = RouteGrid(20, 20, 0.25)
        _mark_copper_keepouts(
            g,
            BoardGraph("t", [comp]),
            {"copper_keepouts": [{"ref": "SW1", "rect_mm": [-1, -3, 1, -2]}]},
        )
        for la in range(g.nlayers):
            self.assertFalse(g.passable(la, *g.cell_of(12.5, 10)))
            self.assertTrue(g.passable(la, *g.cell_of(7.5, 10)))


class TrackSpacingTest(unittest.TestCase):
    def test_nearest_unreserved_cell_preserves_signal_and_power_clearance(self):
        for pitch in (0.25, 0.35, 0.5):
            for width in (0.2, 0.5, 1.5):
                with self.subTest(pitch=pitch, width=width):
                    halo = _track_halo(width, 0.2, 0.15, pitch)
                    copper_gap = (halo + 1) * pitch - width / 2 - 0.2 / 2
                    self.assertGreaterEqual(copper_gap + 1e-9, 0.15)


if __name__ == "__main__":
    unittest.main()
