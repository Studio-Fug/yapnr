"""Filled via-in-pad policy (docs/fab-comparison.md 5A/5B): pure geometry, the
engine's barrel model, the generated KiCad rule and the detailed-grid rule.

Runs in the PnR runtime (no pcbnew). Native checks: test_via_in_pad_native.py.
"""

import json
import math
import unittest
from pathlib import Path

from pnr import fab_profile as fp
from pnr.plane_intent import array_capacity, size_array

SPLANC = Path(__file__).resolve().parents[2] / "splanc_dev"
U5_STRIP = (0.38, 1.43)  # TPS552882 pin 24/25/26 land (VQFN-HR), long axis = h


def jlc():
    return fp.geometry(name="jlc-pofv")


class PolicyTest(unittest.TestCase):
    def test_legacy_has_no_policy(self):
        for g in (fp.geometry(name="legacy"), fp.geometry({"fab": dict(fp.LEGACY_FAB)})):
            self.assertIsNone(g.in_pad)
            self.assertIsNone(g.via_to_smd_pad)
        self.assertIsNone(fp.dru_text(name="legacy"))

    def test_jlc_pofv_values_cite_5a_5b(self):
        g = jlc()
        # 5A "Vias in SMD pads": via copper to SMD pad 0.127.
        self.assertEqual(g.via_to_smd_pad, 0.127)
        ip = g.in_pad
        # 5B "Via-in-pad via" 0.20/0.35 (+ "Alternative" 0.20/0.45), hole edge 0.09,
        # 5B "Via-in-pad row pitch" 0.50 / 0.45, 5B "Inner layers" stagger 0.25.
        self.assertEqual(
            (ip.drill, ip.diameters, ip.hole_margin, ip.pitch, ip.min_pitch, ip.stagger),
            (0.20, (0.35, 0.45), 0.09, 0.50, 0.45, 0.25),
        )
        self.assertTrue(ip.remove_unused_inner_pads)
        self.assertTrue(ip.allows(0.35, 0.20) and ip.allows(0.45, 0.20))
        self.assertFalse(ip.allows(0.45, 0.30))  # the default via is not the in-pad class

    def test_rules_carry_the_policy(self):
        rules = fp.apply_rules({"fab": dict(fp.LEGACY_FAB), "net_classes": []}, "jlc-pofv")
        self.assertEqual(fp.geometry(rules).in_pad, jlc().in_pad)
        self.assertEqual(fp.geometry(rules).via_to_smd_pad, 0.127)


class GeometryTest(unittest.TestCase):
    def test_u5_strip(self):
        ip = jlc().in_pad
        fit = lambda off, d=0.35, drill=0.20, size=U5_STRIP: fp.in_pad_fit(
            ip, size, off, d, drill, 0.0
        )
        self.assertTrue(fit((0, 0)))
        self.assertTrue(fit((0, 0), 0.45))  # centred; bulges 0.035 per side (5B Alternative)
        # 0.38 strip: the 0.09 hole margin leaves no room across the strip.
        self.assertFalse(fit((0.002, 0)))
        self.assertTrue(fit((0, 0.525)))  # hole edge 0.09 from the strip end
        self.assertFalse(fit((0, 0.53)))
        self.assertFalse(fit((0, 0), size=(0.25, 0.6)))  # QFN signal land: too narrow

    def test_bulge_needs_centring(self):
        ip = jlc().in_pad
        size = (0.5, 1.0)
        self.assertTrue(fp.in_pad_fit(ip, size, (0.02, 0), 0.45, 0.20, 0.0))  # copper inside
        self.assertFalse(fp.in_pad_fit(ip, size, (0.04, 0), 0.45, 0.20, 0.0))  # bulges, off-centre
        self.assertTrue(fp.in_pad_fit(ip, size, (0.04, 0), 0.35, 0.20, 0.0))

    def test_unknown_rounding_is_conservative(self):
        ip = jlc().in_pad
        # 0.05/0.05 off a 0.5 square: fits the rectangle, not the round (stadium) land.
        self.assertTrue(fp.in_pad_fit(ip, (0.5, 0.5), (0.05, 0.05), 0.35, 0.20, 0.0))
        self.assertFalse(fp.in_pad_fit(ip, (0.5, 0.5), (0.05, 0.05), 0.35, 0.20, None))

    def test_u5_strip_sites(self):
        ip = jlc().in_pad
        self.assertEqual(fp.in_pad_sites(ip, U5_STRIP), [-0.5, 0.0, 0.5])
        self.assertEqual(fp.in_pad_sites(ip, U5_STRIP, pitch=ip.min_pitch), [-0.45, 0.0, 0.45])
        # Pin 24's footprint via sits at the strip centre: 5B stagger 0.25 leaves two.
        self.assertEqual(fp.in_pad_sites(ip, U5_STRIP, avoid=[0.0]), [-0.25, 0.25])
        self.assertEqual(fp.in_pad_sites(ip, (0.25, 0.6)), [])


class CapacityTest(unittest.TestCase):
    """U5 SW2 (pins 21+25, 5 A rms / 16 A peak, no implicit sharing) on pin 25 alone."""

    def fab(self):
        model = json.loads((SPLANC / "mini-routing-electrical-fab.json").read_text())
        return fp.apply_fab_model(model, "jlc-pofv")

    def test_array_capacity_inverts_size_array(self):
        fab = self.fab()
        self.assertEqual(fab["min_via_plating_um"], 18)
        contract = dict(rms_current_a=5, peak_current_a=16)
        need = size_array(contract, dict(fab, via_drill_mm=0.20, via_diameter_mm=0.35))["count"]
        self.assertEqual(need, 5)  # drop-limited: ceil(16 A x 2.73 mOhm / 10 mV)
        ok = array_capacity(fab, 0.20, need)
        short = array_capacity(fab, 0.20, need - 1)
        self.assertTrue(ok["max_rms_current_a"] >= 5 and ok["max_peak_current_a"] >= 16)
        self.assertLess(short["max_peak_current_a"], 16)

    def test_pin25_strip_cannot_carry_the_terminal_budget(self):
        fab = self.fab()
        three = array_capacity(fab, 0.20, 3)
        self.assertAlmostEqual(three["barrel_resistance_ohm"], 0.0027256, places=6)
        self.assertAlmostEqual(three["max_rms_current_a"], 5.746, places=3)
        self.assertAlmostEqual(three["max_peak_current_a"], 11.007, places=3)
        two = array_capacity(fab, 0.20, 2)
        self.assertAlmostEqual(two["max_peak_current_a"], 7.338, places=3)


class KiCadRuleTest(unittest.TestCase):
    def test_via_to_smd_pad_rule(self):
        text = fp.dru_text(name="jlc-pofv")
        block = text.split('(rule "jlc-pofv_via_to_smd_pad"', 1)[1].split("(rule", 1)[0]
        self.assertIn("(constraint physical_clearance (min 0.127mm))", block)
        # Only the 0.20-drill in-pad class may enter an SMD pad.
        self.assertIn(
            "A.Type == 'Via' && A.Hole > 0.2mm && B.Type == 'Pad' && B.Pad_Type == 'SMD'", block
        )


class GridRuleTest(unittest.TestCase):
    """Detailed grid: profile-only via-to-SMD-pad rule; legacy grid untouched."""

    def grid(self):
        from pnr.place.geometry import Rect
        from pnr.route.detail.grid import RouteGrid

        g = RouteGrid(10, 10, 0.1, clearance=0.127, track_width=0.2, via_radius=0.225)
        pads = [
            ("a", Rect(3.0, 3.0, 0.54, 0.6), 0.0),  # 0402-class land: in-pad possible
            ("a", Rect(6.0, 3.0, 0.25, 0.6), 0.0),  # QFN land: too narrow
            ("b", Rect(3.0, 6.0, 0.6, 0.6), 0.0),
            # A custom land's anchor-centred bounding box (graph land_corner None):
            # roomy as a rectangle, but the copper inside it is unknown.
            ("a", Rect(6.0, 6.0, 1.6, 1.6), None),
        ]
        for net, r, corner in pads:
            g.add_pad(0, net, r)
            g.smd_pads.append((0, net, r, corner))
        return g

    def test_legacy_grid_keeps_old_behaviour(self):
        g = self.grid()
        i, j = g.cell_of(3.0, 3.0)
        self.assertIsNone(g.smd_via_blocked)
        self.assertTrue(g.via_passable(0, i, j, "a"))
        # A via grazing its own pad edge was legal before profiles.
        i, j = g.cell_of(3.0 + 0.27 + 0.2, 3.0)
        self.assertTrue(g.via_passable(0, i, j, "a"))

    def test_profile_rule(self):
        g = self.grid()
        g.restrict_smd_vias(jlc().in_pad, 0.127)
        # Exact pad centre (escape E2): a 5B in-pad via of its own net only.
        self.assertTrue(g.smd_via_ok((3.0, 3.0), "a"))
        self.assertFalse(g.smd_via_ok((3.0, 3.0), "b"))
        self.assertFalse(g.smd_via_ok((6.0, 3.0), "a"))  # 0.25 land
        # Outside: the default via keeps 0.127 from the pad copper.
        edge = 3.0 + 0.27
        self.assertFalse(g.smd_via_ok((edge + 0.225 + 0.10, 3.0), "a"))
        self.assertTrue(g.smd_via_ok((edge + 0.225 + 0.13, 3.0), "a"))
        i, j = g.cell_of(3.0, 3.0)
        self.assertTrue(g.via_passable(0, i, j, "a", point=(3.0, 3.0)))
        self.assertFalse(g.via_passable(0, i, j, "b", point=(3.0, 3.0)))
        # Cell-centre verdicts follow the same rule.
        for x, y in [(edge + 0.225 + 0.05, 3.0), (6.0, 3.0)]:
            ci, cj = g.cell_of(x, y)
            self.assertEqual(bool(g.smd_via_blocked[cj, ci]), not g.smd_via_ok(g.center_of(ci, cj)))

    def test_bounding_box_lands_admit_no_in_pad_via(self):
        """Review repair: the grid judged custom lands on their bounding box, so
        emission placed in-pad vias the native land rejects (or default vias over
        no copper). A land whose rectangle only bounds its copper admits none."""
        g = self.grid()
        g.restrict_smd_vias(jlc().in_pad, 0.127)
        for point in [(6.0, 6.0), (6.2, 6.0), (6.0, 5.6)]:
            self.assertFalse(g.smd_via_ok(point, "a"), point)
            ci, cj = g.cell_of(*point)
            self.assertTrue(g.smd_via_blocked[cj, ci] or not g.smd_via_ok(g.center_of(ci, cj)))
        # Outside it the usual 5A keep-away still applies to the bounding box.
        self.assertFalse(g.smd_via_ok((6.8 + 0.225 + 0.10, 6.0), "a"))
        self.assertTrue(g.smd_via_ok((6.8 + 0.225 + 0.13, 6.0), "a"))

    def test_land_corner_is_used(self):
        from pnr.place.geometry import Rect
        from pnr.route.detail.grid import RouteGrid

        ip = jlc().in_pad
        # 0.05/0.05 off a 0.5 square: fits the square land, not a round one (5B margin).
        for corner, ok in [(0.0, True), (0.25, False)]:
            g = RouteGrid(10, 10, 0.1, clearance=0.127, track_width=0.2, via_radius=0.225)
            r = Rect(3.0, 3.0, 0.5, 0.5)
            g.add_pad(0, "a", r)
            g.smd_pads.append((0, "a", r, corner))
            g.restrict_smd_vias(ip, 0.127)
            self.assertEqual(g.smd_via_ok((3.05, 3.05), "a"), ok, corner)

    def test_graph_lands(self):
        """Pad.land_corner survives serialization; legacy graphs have none; the grid
        drops it at non-quarter-turn part rotations."""
        from pnr.graph import BoardGraph, BoardOutline, Component, Net, Pad
        from pnr.route.detail.grid import RouteGrid

        exact = Pad("1", "a", (0, 0), (0.54, 0.6), land_corner=0.0)
        loose = Pad("2", "a", (2, 0), (1.6, 1.6))
        self.assertIsNone(loose.land_corner)
        graph = BoardGraph(
            "t",
            [
                Component("U", "fp", (5, 5), 0, "top", (4, 2), (4, 2), pads=[exact, loose]),
                Component(
                    "V",
                    "fp",
                    (5, 8),
                    30,
                    "top",
                    (2, 2),
                    (2, 2),
                    pads=[Pad("1", "a", (0, 0), (0.54, 0.6), land_corner=0.0)],
                ),
            ],
            [Net("a", 1, [("U", "1"), ("U", "2"), ("V", "1")])],
            BoardOutline(10, 10),
        )
        again = BoardGraph.from_json(graph.to_json())
        self.assertEqual([p.land_corner for p in again.components[0].pads], [0.0, None])
        legacy = graph.to_dict()
        for c in legacy["components"]:
            for p in c["pads"]:
                del p["land_corner"]
        self.assertTrue(
            all(
                p.land_corner is None
                for c in BoardGraph.from_dict(legacy).components
                for p in c.pads
            )
        )
        g = RouteGrid.from_graph(
            again, 10, 10, pitch=0.1, clearance=0.127, track_width=0.2, via_radius=0.225
        )
        self.assertEqual(
            sorted(corner is None for _, _, _, corner in g.smd_pads), [False, True, True]
        )
        g.restrict_smd_vias(jlc().in_pad, 0.127)
        self.assertTrue(g.smd_via_ok((5, 5), "a"))
        self.assertFalse(g.smd_via_ok((7, 5), "a"))
        self.assertFalse(g.smd_via_ok((5, 8), "a"))  # 30 degree part: the Rect is not the land

    def test_router_enables_it_only_under_a_profile(self):
        import inspect
        from pnr.route.detail import router

        source = inspect.getsource(router.route_board)
        self.assertIn("extra.get('via_to_smd_pad_mm') is not None", source)


if __name__ == "__main__":
    unittest.main()
