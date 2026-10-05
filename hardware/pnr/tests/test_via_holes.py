"""Vias of two nets never share a site under a via model (pnr.via_policy): an
escape via of another net is no longer passed by ``RouteGrid.hole_site_clear``, and
two escape options whose vias sit at one point conflict whatever their spans (review
finding: a span with no routed layer, e.g. a tie between two adjacent ground planes,
was checked on no layer and could land on another net's via)."""

import unittest

from pnr.route.detail.grid import Cell, RouteGrid
from pnr.stack import copper_names
from pnr.via_policy import BLIND, BURIED, MICRO, THROUGH, GridVias, resolve

SIX = copper_names(6)
GAPS6 = [0.09, 0.55, 0.2, 0.55, 0.09]
HDI = dict(allowed=[THROUGH, BLIND, BURIED, MICRO], microvia=dict(diameter_mm=0.3, drill_mm=0.1))
GRID_LAYERS = ("F.Cu", "In2.Cu", "B.Cu")  # the routed layers of SGSGPS


def grid(n=8):
    g = RouteGrid(n, n, 1.0, layers=GRID_LAYERS, clearance=0.2, via_radius=0.3)
    g.via_spacing = 0.55
    g.via_model = GridVias(resolve(HDI, SIX, gaps=GAPS6), GRID_LAYERS, 1.0, 0.2, (0.6, 0.3), 1)
    return g


class Holes(unittest.TestCase):
    def test_no_foreign_hole_on_a_site(self):
        """Under a via model a via never shares its site with another net's via,
        whatever the spans (review finding: two plane-only spans could)."""
        g = grid(4)
        g.escape_vias.append(("A", g.center_of(1, 1)))
        p = g.center_of(1, 1)
        self.assertTrue(g.hole_site_clear(p))  # legacy reading: a co-located via passes
        self.assertTrue(g.hole_site_clear(p, net="A"))  # same net: one barrel
        self.assertFalse(g.hole_site_clear(p, net="B"))

    def test_span_options_never_share_a_site(self):
        from pnr.route.detail.escape import Escape
        from pnr.route.detail.joint_escape import AccessOption, options_conflict

        g = grid(8)
        vm = g.via_model
        micro = vm.to_layer(0, "In1.Cu")  # F-In1, grid layer F only
        lower = vm.to_layer(2, "In4.Cu")  # In4-B, grid layer B only
        p = g.center_of(4, 4)

        def option(net, span, at=p, segments=()):
            esc = Escape(net=net, kind="joint", access=Cell(0, 4, 4), pad_xy=at)
            return AccessOption(
                esc, 1.0, tuple(segments), (at,), frozenset(), (net,), (0, 0, 8, 8), (span,)
            )

        self.assertTrue(options_conflict(g, option("A", micro), option("B", lower)))
        self.assertFalse(options_conflict(g, option("A", micro), option("A", lower)))
        # Disjoint spans a little apart: no shared layer, no copper conflict.
        q = g.center_of(7, 4)
        self.assertFalse(options_conflict(g, option("A", micro), option("B", lower, q)))
        # A track on B through the In4-B via's site conflicts; the same on F does not.
        near = g.center_of(5, 4)
        on = lambda la: ((la, (near[0], near[1] - 1.0), (near[0], near[1] + 1.0), 0.25),)  # noqa
        self.assertTrue(options_conflict(g, option("A", lower, near), option("B", micro, q, on(2))))
        self.assertFalse(
            options_conflict(g, option("A", lower, near), option("B", micro, q, on(0)))
        )


if __name__ == "__main__":
    unittest.main()
