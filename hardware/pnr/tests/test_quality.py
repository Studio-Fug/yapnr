"""Phase 6 tests — routing-rule compilation + the post-route quality analysis.

Pure (no pcbnew): `compile_routing_rules` net-glob expansion, and `analyze` over
synthetic per-net length/via dicts for diff-pair skew, length-match spread, and
net-class totals.
"""

import unittest
from unittest.mock import Mock

from pnr.constraints import (
    ConstraintError,
    DiffPair,
    LengthMatch,
    NetClass,
    compile_constraints,
    compile_routing_rules,
)
from pnr.quality import analyze, unrouted_count


class ConnectivityFailureTest(unittest.TestCase):
    def test_failed_connectivity_query_cannot_pass_release(self):
        board = Mock()
        board.GetConnectivity.side_effect = RuntimeError("unsupported KiCad API")
        with self.assertRaisesRegex(RuntimeError, "Cannot verify"):
            unrouted_count(board)

    def test_unconnected_pads_are_reported(self):
        board = Mock()
        board.GetConnectivity.return_value.GetUnconnectedCount.return_value = 7
        self.assertEqual(unrouted_count(board), 7)


class RoutingRuleCompileTest(unittest.TestCase):
    def _compiled(self):
        doc = {
            "net_class": {"power": {"width_mm": 0.4, "clearance_mm": 0.3, "nets": ["lv", "*hv"]}},
            "diff_pair": [{"name": "usb", "p": "usb_dp", "n": "usb_dm", "skew_mm": 0.3}],
            "length_match": [{"name": "i2c", "nets": ["scl", "sda"], "tolerance_mm": 2.0}],
        }
        return compile_constraints(doc, [])

    def test_parses_into_typed_rules(self):
        c = self._compiled()
        self.assertEqual(len(c.net_classes), 1)
        self.assertIsInstance(c.net_classes[0], NetClass)
        self.assertEqual(c.net_classes[0].width_mm, 0.4)
        self.assertIsInstance(c.diff_pairs[0], DiffPair)
        self.assertEqual(c.diff_pairs[0].skew_mm, 0.3)
        self.assertIsInstance(c.length_matches[0], LengthMatch)

    def test_glob_expansion_against_netlist(self):
        c = self._compiled()
        nets = ["lv", "hv", "p3v3-hv", "vsys-hv", "sda", "scl", "usb_dp", "usb_dm", "misc"]
        rules = compile_routing_rules(c, nets)
        power = rules["net_classes"][0]["nets"]
        self.assertIn("lv", power)
        self.assertIn("p3v3-hv", power)  # *hv glob
        self.assertNotIn("misc", power)
        # diff pair kept only because both nets exist
        self.assertEqual(len(rules["diff_pairs"]), 1)
        self.assertEqual(set(rules["length_match"][0]["nets"]), {"sda", "scl"})

    def test_diff_pair_dropped_if_net_missing(self):
        c = self._compiled()
        rules = compile_routing_rules(c, ["usb_dp"])  # usb_dm absent
        self.assertEqual(rules["diff_pairs"], [])

    def test_diff_pair_requires_p_and_n(self):
        from pnr.constraints import ConstraintError

        with self.assertRaises(ConstraintError):
            compile_constraints({"diff_pair": [{"name": "x", "p": "a"}]}, [])


class TimeBudgetAndTuningRuleTest(unittest.TestCase):
    def test_constraints_carry_ps_budgets_and_tuning(self):
        doc = {
            "diff_pair": [{"name": "usb", "p": "DP", "n": "DN", "skew_ps": 2.5}],
            "length_match": [{"name": "bus", "nets": ["A", "B"], "tolerance_ps": 5}],
            "tuning": {"gap_mm": 0.3, "style": "serpentine", "mitre": False},
        }
        rules = compile_routing_rules(compile_constraints(doc, []), ["DP", "DN", "A", "B"])
        self.assertEqual(rules["diff_pairs"][0]["skew_ps"], 2.5)
        self.assertEqual(rules["length_match"][0]["tolerance_ps"], 5.0)
        self.assertEqual(rules["tuning"], {"gap_mm": 0.3, "style": "serpentine", "mitre": False})
        for bad in ({"style": "zigzag"}, {"gap_mm": -1}, {"mitre": "yes"}, {"pitch": 1}):
            with self.assertRaises(ConstraintError):
                compile_constraints({"tuning": bad}, [])
        with self.assertRaises(ConstraintError):
            compile_constraints({"diff_pair": [{"p": "a", "n": "b", "skew_ps": 0}]}, [])

    def test_a_budget_in_mm_and_ps_at_once_is_refused(self):
        with self.assertRaises(ConstraintError):
            compile_constraints(
                {"diff_pair": [{"p": "a", "n": "b", "skew_mm": 1.0, "skew_ps": 5}]}, []
            )
        with self.assertRaises(ConstraintError):
            compile_constraints(
                {"length_match": [{"nets": ["a", "b"], "tolerance_mm": 1, "tolerance_ps": 5}]},
                [],
            )

    def test_tuning_caps_and_switches(self):
        doc = {"tuning": {"max_added_mm": 4, "meanders": False, "placement": False}}
        cc = compile_constraints(doc, [])
        self.assertEqual(cc.tuning, {"max_added_mm": 4.0, "meanders": False, "placement": False})
        for bad in ({"max_added_mm": 0}, {"meanders": "no"}, {"placement": 1}):
            with self.assertRaises(ConstraintError):
                compile_constraints({"tuning": bad}, [])

    def test_rules_unchanged_without_the_new_keys(self):
        doc = {"diff_pair": [{"name": "usb", "p": "DP", "n": "DN", "skew_mm": 1.0}]}
        rules = compile_routing_rules(compile_constraints(doc, []), ["DP", "DN"])
        self.assertNotIn("tuning", rules)
        self.assertEqual(
            rules["diff_pairs"][0],
            dict(name="usb", p="DP", n="DN", width_mm=None, gap_mm=None, skew_mm=1.0),
        )

    def test_ps_budgets_judge_delay_and_report_margins(self):
        rules = {
            "diff_pairs": [{"name": "usb", "p": "dp", "n": "dm", "skew_mm": 9.0, "skew_ps": 2.0}],
            "length_match": [
                {"name": "bus", "nets": ["x", "y"], "tolerance_mm": 9.0, "tolerance_ps": 5.0}
            ],
        }
        lengths = {"dp": 10.0, "dm": 10.1, "x": 10.0, "y": 11.0}
        ok = analyze(lengths, {}, rules, delays={"dp": 60.0, "dm": 61.5, "x": 60.0, "y": 63.0})
        self.assertTrue(ok.ok)
        self.assertAlmostEqual(ok.diff_pairs[0].margin, 0.5)
        self.assertAlmostEqual(ok.length_matches[0].margin, 2.0)
        self.assertIn("ps", ok.summary())
        late = analyze(lengths, {}, rules, delays={"dp": 60.0, "dm": 62.5, "x": 60.0, "y": 63.0})
        self.assertFalse(late.diff_pairs[0].ok)
        self.assertFalse(analyze(lengths, {}, rules).diff_pairs[0].ok)  # no delays: unproven
        mm = analyze(lengths, {}, {"diff_pairs": [dict(rules["diff_pairs"][0], skew_ps=None)]})
        self.assertAlmostEqual(mm.diff_pairs[0].margin, 8.9)


class QualityAnalyzeTest(unittest.TestCase):
    def test_current_aware_report_requires_electrical_evidence(self):
        report = analyze({"rail": 10}, {}, {"electrical_fab": {"outer_copper_oz": 1}})
        self.assertFalse(report.ok)
        self.assertIn("NOT QUALIFIED", report.summary())
        report.electrical = {"qualified": False}
        self.assertFalse(report.ok)

    def test_totals(self):
        r = analyze({"a": 10.0, "b": 5.0}, {"a": 2, "b": 1}, {})
        self.assertEqual(r.total_length_mm, 15.0)
        self.assertEqual(r.total_vias, 3)
        self.assertEqual(r.routed_nets, 2)
        self.assertTrue(r.ok)  # no rules -> trivially ok

    def test_diff_pair_skew_pass_fail(self):
        rules = {"diff_pairs": [{"name": "usb", "p": "dp", "n": "dm", "skew_mm": 0.5}]}
        ok = analyze({"dp": 10.0, "dm": 10.3}, {}, rules)
        self.assertTrue(ok.diff_pairs[0].ok)
        self.assertAlmostEqual(ok.diff_pairs[0].skew_mm, 0.3, places=6)
        bad = analyze({"dp": 10.0, "dm": 12.0}, {}, rules)
        self.assertFalse(bad.diff_pairs[0].ok)
        self.assertFalse(bad.ok)

    def test_diff_pair_unrouted_is_not_ok(self):
        rules = {"diff_pairs": [{"name": "usb", "p": "dp", "n": "dm", "skew_mm": 5.0}]}
        r = analyze({"dp": 10.0}, {}, rules)  # dm has no copper
        self.assertFalse(r.diff_pairs[0].routed)
        self.assertFalse(r.diff_pairs[0].ok)

    def test_length_match_spread(self):
        rules = {"length_match": [{"name": "bus", "nets": ["x", "y", "z"], "tolerance_mm": 1.0}]}
        ok = analyze({"x": 10.0, "y": 10.5, "z": 10.8}, {}, rules)
        self.assertAlmostEqual(ok.length_matches[0].spread_mm, 0.8, places=6)
        self.assertTrue(ok.length_matches[0].ok)
        bad = analyze({"x": 10.0, "y": 12.0, "z": 10.8}, {}, rules)
        self.assertFalse(bad.length_matches[0].ok)

    def test_pairs_and_groups_use_kicads_lengths_totals_the_raw_sums(self):
        rules = {
            "diff_pairs": [{"name": "usb", "p": "dp", "n": "dm", "skew_mm": 0.5}],
            "length_match": [{"name": "bus", "nets": ["dp", "x"], "tolerance_mm": 0.5}],
        }
        raw = {"dp": 10.0, "dm": 10.9, "x": 11.0, "gnd": 30.0}
        r = analyze(raw, {}, rules, matched_lengths={"dp": 10.6, "dm": 10.9, "x": 11.0})
        self.assertTrue(r.diff_pairs[0].ok)
        self.assertAlmostEqual(r.diff_pairs[0].skew_mm, 0.3)
        self.assertAlmostEqual(r.length_matches[0].spread_mm, 0.4)
        self.assertEqual(r.total_length_mm, 61.9)

    def test_net_class_length_rollup(self):
        rules = {"net_classes": [{"name": "power", "nets": ["gnd", "vcc"]}]}
        r = analyze({"gnd": 40.0, "vcc": 20.0, "sig": 5.0}, {}, rules)
        self.assertEqual(r.net_class_length_mm["power"], 60.0)

    def test_summary_renders(self):
        rules = {"diff_pairs": [{"name": "usb", "p": "dp", "n": "dm", "skew_mm": 0.5}]}
        text = analyze({"dp": 10.0, "dm": 10.2}, {"dp": 1}, rules).summary()
        self.assertIn("diff-pair usb", text)
        self.assertIn("quality: PASS", text)

    def test_fully_routed_default(self):
        r = analyze({"a": 10.0}, {}, {})
        self.assertTrue(r.fully_routed)
        self.assertTrue(r.ok)

    def test_unrouted_fails_and_is_not_ok(self):
        r = analyze({"a": 10.0}, {}, {}, unrouted=7)
        self.assertFalse(r.fully_routed)
        self.assertFalse(r.ok)  # incomplete routing is never ok
        self.assertIn("7 UNROUTED", r.summary())
        self.assertIn("quality: FAIL", r.summary())


if __name__ == "__main__":
    unittest.main()
