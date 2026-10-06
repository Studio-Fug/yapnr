"""The engine adapts a design to the selected fab profile (pnr.fab_profile.apply_rules).

A design's fab block may ask for tracks finer than the profile's fab makes (the buck
and BGA rungs' 0.10 mm default track under jlc-pofv's 0.127 mm minimum): every width the
router would draw below the minimum is raised to it and recorded, so the judge's board
setup never sees a track the fab cannot make. Pure python (no splanc_dev data).
"""

import copy
import unittest

from pnr import fab_profile as fp

RULES = {
    "fab": dict(fp.LEGACY_FAB, track_width_mm=0.1),
    "net_classes": [
        {"name": "power", "width_mm": 1.5, "nets": ["rail"], "plane_layer": None},
        {"name": "fine", "width_mm": 0.1, "nets": ["fb"], "plane_layer": None},
        {"name": "plain", "width_mm": None, "nets": ["lv"], "plane_layer": None},
    ],
    "diff_pairs": [{"name": "usb", "p": "dp", "n": "dn", "width_mm": 0.1, "gap_mm": 0.15}],
}


class FloorTest(unittest.TestCase):
    def test_widths_under_the_minimum_are_raised_and_recorded(self):
        out = fp.apply_rules(copy.deepcopy(RULES), "jlc-pofv")
        self.assertEqual(out["fab"]["track_width_mm"], 0.127)
        self.assertEqual([c["width_mm"] for c in out["net_classes"]], [1.5, 0.127, None])
        self.assertEqual(out["diff_pairs"][0]["width_mm"], 0.127)
        self.assertEqual(out["diff_pairs"][0]["gap_mm"], 0.15)  # the gap is the design's
        self.assertEqual(
            out["fab_adaptations"],
            [
                "fab.track_width_mm 0.1 -> 0.127 mm (fab min_track_width)",
                "net_class fine width 0.1 -> 0.127 mm (fab min_track_width)",
                "diff_pair usb width 0.1 -> 0.127 mm (fab min_track_width)",
            ],
        )

    def test_a_design_the_fab_makes_keeps_its_bytes(self):
        rules = copy.deepcopy(RULES)
        rules["fab"]["track_width_mm"] = 0.2
        rules["net_classes"][1]["width_mm"] = 0.2
        rules["diff_pairs"][0]["width_mm"] = 0.2
        out = fp.apply_rules(rules, "jlc-pofv")
        self.assertNotIn("fab_adaptations", out)
        self.assertEqual(out["fab"]["track_width_mm"], 0.2)

    def test_legacy_and_a_finer_profile_change_nothing(self):
        rules = copy.deepcopy(RULES)
        self.assertIs(fp.apply_rules(rules, "legacy"), rules)
        self.assertEqual(fp.floor_track_widths(copy.deepcopy(RULES), "legacy"), [])
        # jlc-6l-hdi makes 0.09 mm tracks: the design's 0.10 stays.
        out = fp.apply_rules(copy.deepcopy(RULES), "jlc-6l-hdi")
        self.assertNotIn("fab_adaptations", out)
        self.assertEqual(out["fab"]["track_width_mm"], 0.1)


class ViaClearRadiusTest(unittest.TestCase):
    def test_the_hole_clearance_widens_a_thin_ringed_via_only(self):
        from pnr.route.detail.router import via_clear_radius

        # jlc-6l-hdi: 0.45/0.30 via, 0.09 copper and 0.20 hole clearance: the hole
        # binds (0.15 + 0.20 - 0.09 = 0.26 > 0.225).
        self.assertAlmostEqual(via_clear_radius(fp.apply_fab(fp.LEGACY_FAB, "jlc-6l-hdi")), 0.26)
        # legacy and jlc-pofv: the ring covers it, the copper radius as before.
        self.assertEqual(via_clear_radius(fp.apply_fab(fp.LEGACY_FAB, "legacy")), 0.3)
        self.assertEqual(via_clear_radius(fp.apply_fab(fp.LEGACY_FAB, "jlc-pofv")), 0.225)
        self.assertEqual(via_clear_radius({"via_diameter_mm": 0.6, "via_drill_mm": 0.3}), 0.3)


class HdiProfileTest(unittest.TestCase):
    def test_jlc_6l_hdi_makes_the_bga_rungs_vias_and_tracks(self):
        fab = fp.profile_fab("jlc-6l-hdi")
        self.assertEqual((fab["min_through_drill_mm"], fab["min_via_diameter_mm"]), (0.15, 0.25))
        self.assertEqual((fab["min_track_width_mm"], fab["clearance_mm"]), (0.09, 0.09))
        self.assertIsNotNone(fp.in_pad_policy(fab))  # filled vias (free on 6+ layers)
        for cls in fab["via_classes"].values():
            self.assertGreaterEqual(cls["drill_mm"], fab["min_through_drill_mm"])
            self.assertGreaterEqual(cls["diameter_mm"], fab["min_via_diameter_mm"])
            ring = (cls["diameter_mm"] - cls["drill_mm"]) / 2
            self.assertGreaterEqual(round(ring, 6), fab["via_annular_mm"])


if __name__ == "__main__":
    unittest.main()
