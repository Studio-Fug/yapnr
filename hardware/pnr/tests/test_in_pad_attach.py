"""Terminal in-pad via-array attach (run under KiCad Python).

A 5 A rms / 8 A peak terminal on a TPS552882-like 0.38 x 1.43 mm centre strip
(pins 24/25/26 at 0.62 pitch, pin 24's 0.20/0.45 footprint via) has no surface
neck/trunk within its budget. Under jlc-pofv the power plan attaches it by the
engine-sized row of filled 5B vias (3 x 0.20/0.35 at 0.50 pitch, 18 um plating)
and a full-width trunk on another layer; pad_entry / the via-in-pad audit qualify
the terminal only through pnr.via_in_pad.array_attach. Legacy never does.
"""

import math
import unittest

import pcbnew as k
from pnr import fab_profile as fp
from pnr.electrical import compile_policy
from test_via_in_pad_native import FAB, V, board, pad, u5, via

SW = dict(net="sw", scope="terminal", neck_max_length_mm=0.5, source={})


def rules(profile, branch=False):
    intents = [
        dict(SW, ref="U5", pads=["25"], rms_current_a=5, peak_current_a=8),
        dict(
            ref="L2",
            pads=["2"],
            net="sw",
            scope="net",
            rms_current_a=5,
            peak_current_a=8,
            source={},
        ),
    ]
    if branch:
        intents.append(dict(SW, ref="U5", pads=["21"], rms_current_a=0.5, peak_current_a=1))
    base = compile_policy(
        dict(fab=dict(fp.LEGACY_FAB), net_classes=[], diff_pairs=[]), intents, FAB
    )
    return base if profile == "legacy" else fp.apply_rules(base, profile)


def back_pad(b, ref, num, net, pos, size):
    p = pad(b, ref, num, net, pos, size)
    ls = k.LSET()
    ls.AddLayer(k.B_Cu)
    p.SetLayerSet(ls)
    return p


def track(b, net, layer, a, z, width):
    t = k.PCB_TRACK(b)
    t.SetStart(V(*a))
    t.SetEnd(V(*z))
    t.SetWidth(round(width * 1e6))
    t.SetLayer(layer)
    t.SetNetCode(b.FindNet(net).GetNetCode())
    b.Add(t)
    return t


def realize(b, net, plan, g):
    from pnr.native_electrical import add_bank, add_in_pad_vias, add_track

    for la, x, y, w in plan.get("tracks", []):
        add_track(b, net, la, x, y, w)
    for center, points, layers in plan.get("banks", []):
        add_bank(b, net, center, points, plan["policy"], layers)
    add_in_pad_vias(b, net, plan.get("in_pad_vias", []), g)
    b.BuildConnectivity()


class SizingTest(unittest.TestCase):
    def test_engine_model_counts_three_barrels_for_pin25(self):
        """3 x 0.20 mm at 18 um: 5.75 A rms / 11.0 A peak >= 5 A / 8 A; 2 are not enough."""
        from pnr.electrical import terminal_policy
        from pnr.plane_intent import array_capacity
        from pnr.via_in_pad import array_requirement

        r = rules("jlc-pofv")
        g = fp.geometry(r)
        policy = terminal_policy("U5", ["25"], "sw", r)
        self.assertEqual(array_requirement(policy, r, g)["count"], 3)
        three = array_capacity(r["electrical_fab"], 0.2, 3)
        self.assertAlmostEqual(three["max_rms_current_a"], 5.746, places=2)
        self.assertAlmostEqual(three["max_peak_current_a"], 11.007, places=2)
        two = array_capacity(r["electrical_fab"], 0.2, 2)
        self.assertLess(two["max_rms_current_a"], 5)

    def test_windows_prefer_stagger_then_5b_row(self):
        from pnr.via_in_pad import attach_windows

        b = board()
        strip = u5(b)
        g = fp.geometry(rules("jlc-pofv"))
        (window,) = attach_windows(b, strip, 3, g)  # pin 24's via at offset 0: stagger fits 2 only
        self.assertEqual(window["variant"], "row_pitch")
        self.assertEqual([p for p, _, _ in window["vias"]], [(10, 9.5), (10, 10), (10, 10.5)])
        self.assertTrue(all((d, h) == (0.35, 0.2) for _, d, h in window["vias"]))
        two = attach_windows(b, strip, 2, g)
        self.assertEqual(two[0]["variant"], "staggered")
        self.assertTrue(all(abs(u) >= 0.25 - 1e-9 for u in two[0]["offsets_mm"]))
        self.assertEqual(attach_windows(b, strip, 4, g), [])  # a 1.43 strip holds 3 at 0.50
        self.assertEqual(attach_windows(b, strip, 3, fp.geometry(rules("legacy"))), [])


class PowerPlanTest(unittest.TestCase):
    def plan(self, profile, target_layer=k.B_Cu):
        from pnr.native_electrical import Oracle, power_plan

        b = board()
        strip = u5(b)
        if target_layer == k.B_Cu:
            target = back_pad(b, "L2", "2", "sw", (15, 15), (2, 2))
        else:
            target = pad(b, "L2", "2", "sw", (15, 15), (2, 2))
        b.BuildConnectivity()
        r = rules(profile)
        o = Oracle(b, r)
        return b, strip, r, o, power_plan(b, "sw", strip, target, r, o, (1, 1, 19, 19), 0.2)

    def check_attach(self, b, strip, r, plan, layers=("B.Cu",)):
        from pnr.pad_entry import snapshot
        from pnr.via_in_pad import array_attach, audit, new_forbidden

        g = fp.geometry(r)
        self.assertEqual(plan["status"], "routed", plan.get("status"))
        self.assertEqual([p for p, _, _ in plan["in_pad_vias"]], [(10, 9.5), (10, 10), (10, 10.5)])
        report = plan["in_pad_array"]
        self.assertEqual(
            (report["pad"], report["variant"], report["required"]), ("U5.25", "row_pitch", 3)
        )
        self.assertIn(report["layer"], layers)
        # The collector (first trunk segment) runs along the row at the full width of
        # its layer (5 A: 1.19 mm on 1 oz B.Cu, 7.14 mm on 15.2 um In2) and covers
        # every via disk.
        layer = b.GetLayerID(report["layer"])
        width = {"B.Cu": 1.192, "In2.Cu": 7.142}[report["layer"]]
        s = report["collector_offset_mm"]
        self.assertLessEqual(abs(s), (width - 0.35) / 2 + 1e-6)
        ends = {(round(10 + s, 6), 9.5), (round(10 + s, 6), 10.5)}
        (collector,) = [
            t for t in plan["tracks"] if t[0] == layer and {tuple(t[1]), tuple(t[2])} == ends
        ]
        self.assertAlmostEqual(collector[3], width, places=3)
        self.assertTrue(all(abs(w - width) < 1e-3 for la, _, _, w in plan["tracks"] if la == layer))
        before = board()
        u5(before)
        realize(b, "sw", plan, g)
        attach = array_attach(b, strip, r)
        self.assertTrue(attach["qualified"], attach)
        self.assertEqual(
            (attach["connected"], attach["required"], attach["layer"]), (3, 3, report["layer"])
        )
        self.assertTrue(snapshot(b, r)[strip.m_Uuid.AsString() + ":" + str(k.F_Cu)])
        found = audit(b, r)
        self.assertEqual((found["forbidden"], found["in_pad_terminal_attaches"]), (0, ["U5.25"]))
        vias = [
            t for t in b.GetTracks() if t.GetClass() == "PCB_VIA" and t.GetDrillValue() == 200000
        ]
        self.assertEqual(len(vias), 3)
        self.assertTrue(
            all(
                v.Padstack().UnconnectedLayerMode()
                == k.UNCONNECTED_LAYER_MODE_REMOVE_EXCEPT_START_AND_END
                for v in vias
            )
        )
        # Full native connectivity from the terminal to the target.
        cn = b.GetConnectivity()
        target = next(p for f in b.GetFootprints() for p in f.Pads() if f.GetReference() == "L2")
        self.assertIn(
            target.m_Uuid.AsString(), {t.m_Uuid.AsString() for t in cn.GetConnectedItems(strip)}
        )
        self.assertEqual(new_forbidden(before, b, r), [])

    def test_blocked_terminal_attaches_by_in_pad_array_to_back_trunk(self):
        b, strip, r, o, plan = self.plan("jlc-pofv")
        self.check_attach(b, strip, r, plan)

    def test_array_trunk_changes_layer_through_a_bank(self):
        b, strip, r, o, plan = self.plan("jlc-pofv", k.F_Cu)
        self.check_attach(b, strip, r, plan, layers=("B.Cu", "In2.Cu"))
        self.assertTrue(plan["banks"])  # the trunk returns to F.Cu through a source-sized bank
        ((center, points, ports),) = plan["banks"]
        self.assertEqual(len(points), plan["policy"]["via_array"]["count"])

    def test_retries_reoffer_the_reserved_array(self):
        """Review repair: a later root strategy re-validated the barrels against their
        own reservation in the shared Oracle and lost the attach (via_blocked)."""
        from pnr.native_electrical import Oracle, power_plan

        b = board()
        strip = u5(b)
        target = back_pad(b, "L2", "2", "sw", (17, 17), (2, 2))  # outside the search bounds
        b.BuildConnectivity()
        r = rules("jlc-pofv")
        o = Oracle(b, r)
        plan = power_plan(b, "sw", strip, target, r, o, (1, 1, 14, 14), 0.2)
        self.assertNotEqual(plan["status"], "routed")
        attempts = o.in_pad_attempts
        self.assertGreaterEqual(len(attempts), 2)
        self.assertTrue(all(a["ports"] > 0 and a["via_blocked"] == 0 for a in attempts), attempts)
        self.assertEqual(len(o.in_pad_reserved), 3)

    def test_legacy_never_places_vias_in_pads(self):
        b, strip, r, o, plan = self.plan("legacy")
        self.assertNotIn("in_pad_vias", plan)
        self.assertNotEqual(plan["status"], "routed")

    def test_surface_attach_is_preferred(self):
        """A land with a budgeted surface attach never gets an in-pad array."""
        from pnr.native_electrical import Oracle, power_plan

        b = board()
        strip = pad(b, "U5", "25", "sw", (10, 10), (0.38, 1.43))  # no neighbours
        target = pad(b, "L2", "2", "sw", (15, 10), (2, 2))
        b.BuildConnectivity()
        r = rules("jlc-pofv")
        plan = power_plan(b, "sw", strip, target, r, Oracle(b, r), (1, 1, 19, 19), 0.2)
        self.assertEqual(plan["status"], "routed")
        self.assertNotIn("in_pad_vias", plan)


class AttachQualificationTest(unittest.TestCase):
    """array_attach: 5B vias in the pad, one full-width trunk on another layer, capacity."""

    def setUp(self):
        self.b = board()
        self.strip = u5(self.b)
        self.r = rules("jlc-pofv")
        self.width = self.r["electrical_nets"]["sw"]["outer_width_mm"]

    def attach(self):
        from pnr.via_in_pad import array_attach

        self.b.BuildConnectivity()
        return array_attach(self.b, self.strip, self.r)

    def entry(self):
        from pnr.pad_entry import snapshot

        self.b.BuildConnectivity()
        return snapshot(self.b, self.r).get(self.strip.m_Uuid.AsString() + ":" + str(k.F_Cu))

    def test_dangling_or_thin_or_partial_trunks_do_not_qualify(self):
        for y in (9.5, 10, 10.5):
            via(self.b, "sw", (10, y))
        report = self.attach()
        self.assertEqual(
            (report["in_pad_vias"], report["connected"], report["qualified"]), (3, 0, False)
        )
        self.assertIsNone(self.entry())  # no record: barrels alone are never an entry
        thin = track(
            self.b, "sw", k.B_Cu, (10, 9.5), (10, 14), 0.2
        )  # through every centre, 0.2 wide
        self.assertFalse(self.attach()["qualified"])
        self.b.Remove(thin)
        s = 0.4  # full width, covers only the two lower vias (via disks inside the copper)
        track(self.b, "sw", k.B_Cu, (10 + s, 10), (10 + s, 14), self.width)
        report = self.attach()
        self.assertEqual((report["connected"], report["qualified"]), (2, False))
        self.assertIsNone(self.entry())

    def test_full_width_collector_qualifies_and_grazing_group_does_not(self):
        for y in (9.5, 10, 10.5):
            via(self.b, "sw", (10, y))
        s = 0.4
        track(self.b, "sw", k.B_Cu, (10 + s, 9.5), (10 + s, 10.5), self.width)
        track(self.b, "sw", k.B_Cu, (10 + s, 10.5), (15, 15), self.width)
        report = self.attach()
        self.assertEqual(
            (report["connected"], report["layer"], report["qualified"]), (3, "B.Cu", True)
        )
        self.assertGreaterEqual(report["capacity"]["max_peak_current_a"], 8)
        self.assertTrue(self.entry())
        # Legacy rules: the same copper is not an entry (no via-in-pad policy).
        from pnr.pad_entry import snapshot

        self.assertNotIn(
            self.strip.m_Uuid.AsString() + ":" + str(k.F_Cu), snapshot(self.b, rules("legacy"))
        )

    def test_covering_tracks_must_be_one_full_width_group(self):
        for y in (9.5, 10, 10.5):
            via(self.b, "sw", (10, y))
        # Two separate full-width stubs (1.19 wide) cover 2 + 1 vias but do not overlap
        # by a full-width contact: neither group holds the 3 barrels the budget needs.
        track(self.b, "sw", k.B_Cu, (10.4, 9.5), (10.4, 10), self.width)
        track(self.b, "sw", k.B_Cu, (10.4, 10.5), (11.9, 10.5 + 1.8), self.width)
        stubs = self.attach()
        self.assertLess(stubs["connected"], 3)
        self.assertFalse(stubs["qualified"])

    def test_unqualified_in_pad_via_counts_for_nothing(self):
        for y in (9.5, 10):
            via(self.b, "sw", (10, y))
        via(self.b, "sw", (10.03, 10.5))  # off the strip centreline: not 5B
        track(self.b, "sw", k.B_Cu, (10.4, 9.5), (10.4, 14), self.width)
        report = self.attach()
        self.assertEqual(
            (report["in_pad_vias"], report["connected"], report["qualified"]), (2, 2, False)
        )


class BranchLandingTest(unittest.TestCase):
    def test_declared_branch_joins_attached_terminal_land(self):
        """U5.21 (0.5 A / 1 A) lands on the array-attached U5.25 at its own width."""
        from pnr.native_electrical import Oracle, power_plan
        from pnr.pad_entry import snapshot

        b = board()
        strip = u5(b)
        branch = pad(b, "U5", "21", "sw", (10, 11.6), (0.25, 0.6))
        target = back_pad(b, "L2", "2", "sw", (15, 15), (2, 2))
        b.BuildConnectivity()
        r = rules("jlc-pofv", branch=True)
        plan = power_plan(b, "sw", strip, target, r, Oracle(b, r), (1, 1, 19, 19), 0.2)
        realize(b, "sw", plan, fp.geometry(r))
        entries = snapshot(b, r)
        plan = power_plan(b, "sw", branch, strip, r, Oracle(b, r), (1, 1, 19, 19), 0.2)
        self.assertEqual(plan["status"], "routed")
        self.assertNotIn("in_pad_vias", plan)
        self.assertTrue(
            all(la == k.F_Cu and abs(w - 0.2) < 1e-9 for la, _, _, w in plan["tracks"]),
            plan["tracks"],
        )
        realize(b, "sw", plan, fp.geometry(r))
        after = snapshot(b, r)
        self.assertEqual([key for key, good in entries.items() if good and not after.get(key)], [])
        self.assertEqual([key for key, good in after.items() if not good], [])


if __name__ == "__main__":
    unittest.main()
