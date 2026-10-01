import unittest

from pnr.route.detail.fixed import reserve_fixed_copper
from pnr.route.detail.grid import Cell, RouteGrid
from pnr.route.detail.maze import route


class FixedCopperTest(unittest.TestCase):
    def test_off_grid_capsule_and_layer_scope(self):
        g = RouteGrid(10, 10, 0.2, clearance=0.1, track_width=0.2)
        reserve_fixed_copper(
            g, dict(frame="engine-mm-y-up", tracks=[["usb", "F.Cu", [0.1, 5.03], [9.9, 5.03], 0.2]])
        )
        i, j = g.cell_of(5, 5)
        self.assertFalse(g.passable(0, i, j, "signal"))
        self.assertTrue(g.passable(1, i, j, "signal"))
        self.assertFalse(g.via_passable(0, i, j, "signal"))
        # No legal crossing on the fixed track's layer, even with negotiation/ripup.
        a = Cell(0, *g.cell_of(5, 2))
        b = Cell(0, *g.cell_of(5, 8))
        r = route(g, {"signal": [a, b]}, max_iters=2, rrr_rounds=1)
        self.assertTrue(r.nets["signal"].routed)
        self.assertTrue(r.nets["signal"].vias)

    def test_through_via_blocks_all_layers(self):
        g = RouteGrid(
            10, 10, 0.2, layers=("F.Cu", "In2.Cu", "B.Cu"), clearance=0.1, track_width=0.2
        )
        reserve_fixed_copper(
            g,
            dict(
                frame="engine-mm-y-up",
                vias=[dict(net="usb", xy=[5, 5], diameter_mm=0.6, drill_mm=0.3, type="through")],
            ),
        )
        for la in range(3):
            i, j = g.cell_of(5, 5)
            self.assertFalse(g.passable(la, i, j, "signal"))
            self.assertFalse(g.via_passable(la, i, j, "signal"))
            i, j = g.cell_of(8, 8)
            self.assertTrue(g.passable(la, i, j, "signal"))

    def test_own_net_mode_passes_its_own_net_only(self):
        """A pad of net A behind a fixed A track: reachable by A, walled off for B."""
        g = RouteGrid(10, 10, 0.2, clearance=0.1, track_width=0.2)
        # A vertical fixed A track at x=5 on both layers walls the pad at x=7 off from x=3.
        wall = [["A", layer, [5.0, 0.1], [5.0, 9.9], 0.2] for layer in ("F.Cu", "B.Cu")]
        reserve_fixed_copper(g, dict(frame="engine-mm-y-up", tracks=wall), own_net=True)
        i, j = g.cell_of(5, 5)
        self.assertTrue(g.passable(0, i, j, "A"))
        self.assertFalse(g.passable(0, i, j, "B"))
        self.assertTrue(g.via_passable(0, i, j, "A"))
        self.assertFalse(g.via_passable(0, i, j, "B"))
        self.assertFalse(g.blocked.any() or g.via_blocked.any())
        a = [Cell(0, *g.cell_of(3, 5)), Cell(0, *g.cell_of(7, 5))]
        r = route(g, {"A": a}, max_iters=2, rrr_rounds=1)
        self.assertTrue(r.nets["A"].routed)
        r = route(g, {"B": a}, max_iters=2, rrr_rounds=1)
        self.assertFalse(r.nets["B"].routed)
        # Cells two nets claim belong to neither.
        reserve_fixed_copper(
            g,
            dict(frame="engine-mm-y-up", tracks=[["C", "F.Cu", [4.8, 5.0], [5.2, 5.0], 0.2]]),
            own_net=True,
        )
        self.assertFalse(g.passable(0, i, j, "A"))
        self.assertFalse(g.passable(0, i, j, "C"))

    def test_own_net_vias_keep_hole_spacing_for_their_own_net(self):
        g = RouteGrid(10, 10, 0.2, clearance=0.1, track_width=0.2)
        reserve_fixed_copper(
            g,
            dict(
                frame="engine-mm-y-up",
                vias=[dict(net="A", xy=[5, 5], diameter_mm=0.6, drill_mm=0.3, type="through")],
            ),
            own_net=True,
        )
        for la in range(2):
            for x, y in ((5, 5), (5.4, 5), (5, 5.6)):
                i, j = g.cell_of(x, y)
                self.assertFalse(g.via_passable(la, i, j, "A"))
                self.assertTrue(g.passable(la, i, j, "A") or (x, y) != (5, 5))
            i, j = g.cell_of(5, 5)
            self.assertTrue(g.passable(la, i, j, "A"))
            self.assertFalse(g.passable(la, i, j, "B"))

    def test_default_mode_tables_are_unchanged(self):
        copper = dict(
            frame="engine-mm-y-up",
            tracks=[["A", "F.Cu", [1.0, 1.0], [8.0, 3.3], 0.3]],
            vias=[dict(net="A", xy=[4, 6], diameter_mm=0.6, drill_mm=0.3, type="through")],
        )
        plain, explicit = RouteGrid(10, 10, 0.25), RouteGrid(10, 10, 0.25)
        reserve_fixed_copper(plain, copper)
        reserve_fixed_copper(explicit, copper, own_net=False)
        self.assertTrue((plain.blocked == explicit.blocked).all())
        self.assertTrue((plain.via_blocked == explicit.via_blocked).all())
        self.assertEqual((plain.pad_net, plain.via_halo), ({}, {}))
        # The counts of the code before the own-net mode (same copper, same grid).
        self.assertEqual(int(plain.blocked.sum()), 193)
        self.assertEqual(int(plain.via_blocked.sum()), 254)

    def test_frame_and_blind_vias_are_not_silently_assumed(self):
        g = RouteGrid(10, 10, 0.2)
        with self.assertRaises(ValueError):
            reserve_fixed_copper(g, {})
        with self.assertRaises(ValueError):
            reserve_fixed_copper(
                g,
                dict(
                    frame="engine-mm-y-up",
                    vias=[dict(net="x", xy=[5, 5], diameter_mm=0.6, drill_mm=0.3, type="blind")],
                ),
            )


if __name__ == "__main__":
    unittest.main()
