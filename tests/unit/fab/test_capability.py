"""The vendor data (yapnr/fab/data): schemas, provenance, sources and cross-references."""

from __future__ import annotations

import copy
import datetime
import unittest

from yapnr.fab import capability


class DataTest(unittest.TestCase):
    def test_every_file_is_valid_and_cross_references_resolve(self):
        self.assertEqual(capability.check_all(), [])

    def test_the_planned_profiles_and_stackups_exist(self):
        self.assertEqual(
            capability.names("profiles"),
            [
                "jlc-4l",
                "jlc-6l",
                "jlc-pofv",
                "oshpark-2l",
                "oshpark-4l",
                "oshpark-6l",
                "pcbway-adv-6l-rf",
                "pcbway-hf-2l",
                "pcbway-std",
            ],
        )
        self.assertEqual(capability.names("vendors"), ["jlcpcb", "oshpark", "pcbway"])
        self.assertEqual(len(capability.names("stackups")), 18)
        drafts = [
            n for n in capability.names("profiles") if capability.profile(n)["status"] == "draft"
        ]
        self.assertEqual(drafts, ["jlc-6l", "pcbway-adv-6l-rf", "pcbway-hf-2l", "pcbway-std"])

    def test_every_source_has_an_https_url_and_an_access_date(self):
        sources = capability.all_sources()
        self.assertTrue(sources)
        for src in sources:
            self.assertTrue(src["url"].startswith("https://"), src)
            datetime.date.fromisoformat(src["accessed"])

    def test_every_engine_value_has_provenance(self):
        for name in capability.engine_profile_names():
            spec = capability.engine_profile(name)
            for block in ("fab", "copper"):
                self.assertEqual(set(spec[block]), set(spec["provenance"][block]), (name, block))

    def test_builtin_and_unknown_names_are_not_data_engine_profiles(self):
        with self.assertRaises(KeyError):
            capability.engine_profile("jlc-pofv")
        with self.assertRaises(KeyError):
            capability.engine_profile("no-such-profile")
        with self.assertRaises(KeyError):
            capability.engine_profile("../vendors/oshpark")

    def test_jlc_4l_is_jlc_pofv_without_the_filled_via_rows(self):
        from yapnr.fab.profiles import fab_profile_module

        fp = fab_profile_module()
        pofv = copy.deepcopy(fp.PROFILES["jlc-pofv"]["fab"])
        jlc4 = capability.engine_profile("jlc-4l")["fab"]
        del pofv["filled_via_hole_to_hole_mm"]
        del pofv["via_classes"]["in_pad"]
        # 5B's 0.35 in-pad via sets jlc-pofv's minimum; without it 5A's 0.45 is the smallest pad.
        self.assertEqual(pofv.pop("min_via_diameter_mm"), 0.35)
        self.assertEqual(jlc4.pop("min_via_diameter_mm"), 0.45)
        self.assertEqual(jlc4, pofv)
        copper = dict(fp.PROFILES["jlc-pofv"]["copper"])
        del copper["filled_via_hole_to_hole_mm"]
        self.assertEqual(capability.engine_profile("jlc-4l")["copper"], copper)

    def test_osh_park_values_match_the_published_rules(self):
        fab = {
            n: capability.engine_profile(n)["fab"]
            for n in ("oshpark-2l", "oshpark-4l", "oshpark-6l")
        }
        self.assertEqual(fab["oshpark-2l"]["min_track_width_mm"], 0.1524)  # 6 mil
        self.assertEqual(fab["oshpark-4l"]["min_track_width_mm"], 0.127)  # 5 mil
        self.assertEqual(fab["oshpark-2l"]["via_annular_mm"], 0.127)  # 5 mil
        self.assertEqual(fab["oshpark-4l"]["via_annular_mm"], 0.1016)  # 4 mil
        self.assertEqual(fab["oshpark-6l"]["min_through_drill_mm"], 0.2032)  # 8 mil
        for f in fab.values():
            self.assertEqual(f["edge_clearance_mm"], 0.381)  # 15 mil keepout
            self.assertNotIn("in_pad", f["via_classes"])
            self.assertNotIn("filled_via_hole_to_hole_mm", f)
            self.assertAlmostEqual(
                f["min_via_diameter_mm"], f["min_through_drill_mm"] + 2 * f["via_annular_mm"], 9
            )


class ValidationTest(unittest.TestCase):
    """Planted problems are reported (the data test above would then fail)."""

    def setUp(self):
        self.profile = capability.profile("oshpark-4l")
        self.stackup = capability.stackup("oshpark-4l-fr408hr")

    def test_a_value_without_provenance(self):
        doc = copy.deepcopy(self.profile)
        doc["engine"]["fab"]["new_rule_mm"] = 0.2
        problems = capability.validate("profiles", "oshpark-4l", doc)
        self.assertIn("engine.fab.new_rule_mm: no provenance", problems)

    def test_a_source_key_that_is_not_listed(self):
        doc = copy.deepcopy(self.profile)
        doc["engine"]["provenance"]["fab"]["clearance_mm"] = "X-unknown"
        problems = capability.validate("profiles", "oshpark-4l", doc)
        self.assertTrue(any("cites no source" in p for p in problems), problems)

    def test_a_source_without_date_or_https(self):
        doc = copy.deepcopy(self.profile)
        doc["sources"]["O-4l"] = {"url": "http://example.com", "accessed": "yesterday"}
        problems = capability.validate("profiles", "oshpark-4l", doc)
        self.assertIn("source O-4l: url must be https", problems)
        self.assertIn("source O-4l: accessed must be YYYY-MM-DD", problems)

    def test_a_via_class_under_the_minimum_ring(self):
        doc = copy.deepcopy(self.profile)
        doc["engine"]["fab"]["via_classes"]["default"] = {"diameter_mm": 0.45, "drill_mm": 0.30}
        problems = capability.validate("profiles", "oshpark-4l", doc)
        self.assertTrue(any("ring 0.0750 under the minimum" in p for p in problems), problems)

    def test_stackup_copper_order_and_priors(self):
        doc = copy.deepcopy(self.stackup)
        doc["layers"][1]["name"] = "In1.Cu"
        del doc["layers"][2]["df_prior_src"]
        problems = capability.validate("stackups", "oshpark-4l-fr408hr", doc)
        self.assertTrue(any("must be F.Cu, In1.Cu.. B.Cu" in p for p in problems), problems)
        self.assertTrue(any("df_prior" in p for p in problems), problems)

    def test_the_wrong_schema_and_name(self):
        doc = copy.deepcopy(self.profile)
        doc["schema"] = "v0"
        problems = capability.validate("profiles", "other", doc)
        self.assertIn("schema must be yapnr-fab-profile-v1", problems)
        self.assertIn("name must equal the file name", problems)

    def test_provenance_forms(self):
        sources = {"O-4l": {}, "J-imp": {}}
        self.assertEqual(capability.source_key("O-4l", sources), "O-4l")
        self.assertEqual(capability.source_key("O-4l (10 mil drill)", sources), "O-4l")
        self.assertEqual(capability.source_key("derived: a reason here", sources), "derived")
        self.assertEqual(capability.source_key("as fab.clearance_mm", sources), "derived")
        self.assertIsNone(capability.source_key("derived:", sources))
        self.assertIsNone(capability.source_key("O-2l", sources))
        self.assertIsNone(capability.source_key(None, sources))

    def test_stale_sources(self):
        doc = {"sources": {"A": {"url": "https://example.com", "accessed": "2026-01-01"}}}
        self.assertEqual(
            capability.stale_sources([doc], today=datetime.date(2026, 10, 2)), ["A (2026-01-01)"]
        )
        self.assertEqual(capability.stale_sources([doc], today=datetime.date(2026, 3, 1)), [])


if __name__ == "__main__":
    unittest.main()
