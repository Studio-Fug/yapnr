"""Regression: an already-present, identical source power bank is kept, not re-planned.

Pure-python cases cover the via-matching and track-coverage predicates; native cases
(KiCad python) cover replace_power_array on a board whose bank neighbours copper legal
at the board clearance but inside the fab precheck gap, and banks that must not be
kept (vias without current-sized copper, shorts, board-rule violations).
"""

import importlib.util
import math
import os
import random
import unittest

from pnr.plane_access import covering_tracks, matching_array
from pnr.plane_intent import array_geometry

FAB = dict(
    via_drill_mm=0.3,
    via_diameter_mm=0.6,
    min_via_plating_um=20,
    board_thickness_mm=1.6,
    copper_resistivity_ohm_mm=0.000021,
    via_barrel_loss_budget_w=0.01,
    via_array_peak_drop_v=0.01,
    hole_clearance_mm=0.2,
    track_width_mm=0.2,
    outer_copper_oz=1,
    plane_access_delta_t_c=40,
    power_bus_min_width_mm=1.5,
)
INTENT = dict(
    ref="Q987",
    address="any.converter.low_fet",
    pads=["1", "2", "3"],
    kind="power_array",
    surface="F.Cu",
    rms_current_a=5,
    peak_current_a=16,
    max_array_span_mm=3,
)
PITCH = max(FAB["via_diameter_mm"] + 0.2, FAB["via_drill_mm"] + FAB["hole_clearance_mm"])


def plan(key="vias"):
    # Q2-like cardinal row of three pads right of the footprint centre (mm).
    pads = [((6, y), (0.7, 0.5)) for y in (4, 5, 6)]
    return array_geometry(pads, (5, 5), INTENT, FAB)[key]


def existing(vias, through=True):
    return [(i, p, d, h, through) for i, (p, d, h) in enumerate(vias)]


class MatchingPredicateTests(unittest.TestCase):
    def test_exact_bank_matches_in_any_order(self):
        planned = plan()
        self.assertEqual(len(planned), 3)
        ex = existing(planned)
        random.Random(1).shuffle(ex)
        keys = matching_array(planned, ex, PITCH)
        self.assertEqual(sorted(keys), [0, 1, 2])
        self.assertTrue(
            all(
                math.dist(dict((e[0], e[1]) for e in ex)[k], p) < 1e-9
                for k, (p, _, _) in zip(keys, planned)
            )
        )

    def test_assembly_rounding_is_within_tolerance(self):
        planned = plan()
        ex = existing([((x + 0.002, y - 0.002), d + 1e-4, h) for (x, y), d, h in planned])
        self.assertIsNotNone(matching_array(planned, ex, PITCH))

    def test_distant_same_net_via_is_not_part_of_the_bank(self):
        planned = plan()
        ex = existing(planned) + [(9, (planned[0][0][0] + 3, planned[0][0][1]), 0.6, 0.3, True)]
        self.assertEqual(sorted(matching_array(planned, ex, PITCH)), [0, 1, 2])

    def test_different_banks_do_not_match(self):
        planned = plan()
        (x, y), d, h = planned[0]
        cases = dict(
            shifted=[((x + 0.05, y), d, h)] + planned[1:],
            missing=planned[1:],
            resized_drill=[((x, y), d, 0.35)] + planned[1:],
            resized_diameter=[((x, y), 0.5, h)] + planned[1:],
            duplicate=planned + [planned[0]],
            extra_inline=planned + [((2 * x - planned[1][0][0], 2 * y - planned[1][0][1]), d, h)],
            extra_lateral=planned + [((x + PITCH, y), d, h)],
        )
        for name, vias in cases.items():
            with self.subTest(name):
                self.assertIsNone(matching_array(planned, existing(vias), PITCH))
        with self.subTest("not_through"):
            self.assertIsNone(matching_array(planned, existing(planned, through=False), PITCH))
        with self.subTest("nothing_planned"):
            self.assertIsNone(matching_array([], existing(planned), PITCH))


class CoveragePredicateTests(unittest.TestCase):
    def segs(self, tracks):
        return [(i, a, b, w) for i, (a, b, w) in enumerate(tracks)]

    def test_exact_reversed_split_and_wider_copper_covers(self):
        planned = plan("tracks")
        self.assertEqual(len(planned), 7)
        self.assertEqual(sorted(covering_tracks(planned, self.segs(planned))), list(range(7)))
        self.assertIsNotNone(
            covering_tracks(planned, self.segs([(b, a, w + 0.3) for a, b, w in planned]))
        )
        (a, b, w), rest = planned[0], planned[1:]
        mid = ((a[0] + b[0]) / 2 + 0.003, (a[1] + b[1]) / 2)  # router split, 3 um jog
        self.assertIsNotNone(covering_tracks(planned, self.segs([(a, mid, w), (mid, b, w)] + rest)))

    def test_missing_narrow_short_gapped_or_offset_copper_does_not_cover(self):
        planned = plan("tracks")
        (a, b, w), rest = planned[0], planned[1:]
        u = ((b[0] - a[0]) / math.dist(a, b), (b[1] - a[1]) / math.dist(a, b))

        def at(t, lateral=0):
            return (a[0] + u[0] * t - u[1] * lateral, a[1] + u[1] * t + u[0] * lateral)

        L = math.dist(a, b)
        cases = dict(
            missing=rest,
            narrow=[(a, b, w - 0.01)] + rest,
            all_narrow=[(s, e, 0.15) for s, e, _ in planned],
            short=[(a, at(L - 0.05), w)] + rest,
            gapped=[(a, at(L / 2 - 0.03), w), (at(L / 2 + 0.03), b, w)] + rest,
            offset=[(at(0, 0.05), at(L, 0.05), w)] + rest,
            vias_only=[],
        )
        for name, tracks in cases.items():
            with self.subTest(name):
                self.assertIsNone(covering_tracks(planned, self.segs(tracks)))


@unittest.skipUnless(importlib.util.find_spec("pcbnew"), "requires native KiCad")
class ReuseExistingBankTests(unittest.TestCase):
    def fixture(self, foreign=True):
        import pcbnew as k

        from pnr.plane_access import replace_power_array

        b = k.BOARD()
        b.SetCopperLayerCount(4)
        b.GetDesignSettings().m_MinClearance = 150000
        n = k.NETINFO_ITEM(b, "lv")
        b.Add(n)
        other = k.NETINFO_ITEM(b, "hv")
        b.Add(other)
        f = k.FOOTPRINT(b)
        f.SetReference("Q987")
        f.SetPosition(k.VECTOR2I(5000000, 5000000))
        b.Add(f)
        for i, y in enumerate([4, 5, 6], 1):
            p = k.PAD(f)
            p.SetNumber(str(i))
            p.SetPosition(k.VECTOR2I(6000000, y * 1000000))
            p.SetSize(k.VECTOR2I(700000, 500000))
            p.SetAttribute(k.PAD_ATTRIB_SMD)
            ls = k.LSET()
            ls.AddLayer(k.F_Cu)
            p.SetLayerSet(ls)
            p.SetNetCode(n.GetNetCode())
            f.Add(p)
        first = replace_power_array(b, dict(INTENT), FAB)
        self.assertNotIn("existing_array", first)
        via = max(
            (t for t in b.GetTracks() if t.GetClass() == "PCB_VIA"), key=lambda t: t.GetPosition().y
        )
        if not foreign:
            return b, via
        # Routed foreign copper 0.16 mm away: legal at the 0.15 mm board clearance but
        # inside the 0.2 mm fab precheck gap.
        t = k.PCB_TRACK(b)
        t.SetLayer(k.B_Cu)
        t.SetWidth(200000)
        t.SetNetCode(other.GetNetCode())
        x = via.GetPosition().x + 300000 + 160000 + 100000
        t.SetStart(k.VECTOR2I(x, via.GetPosition().y - 1000000))
        t.SetEnd(k.VECTOR2I(x, via.GetPosition().y + 1000000))
        b.Add(t)
        t.thisown = False
        return b, via

    @staticmethod
    def copper(b):
        return sorted(
            (
                t.m_Uuid.AsString(),
                t.GetClass(),
                t.GetStart().x,
                t.GetStart().y,
                t.GetEnd().x,
                t.GetEnd().y,
            )
            for t in b.GetTracks()
        )

    def test_present_bank_is_kept_despite_close_foreign_copper(self):
        from pnr.plane_access import replace_power_array

        b, _ = self.fixture()
        before = self.copper(b)
        r = replace_power_array(b, dict(INTENT), FAB)
        self.assertTrue(r["existing_array"])
        self.assertEqual((r["previous_vias"], r["count"]), (3, 3))
        self.assertEqual(self.copper(b), before)  # no mutation, uuids included

    def test_legacy_flag_and_mismatched_bank_keep_replacement_diagnostics(self):
        import pcbnew as k

        from pnr.plane_access import replace_power_array

        b, via = self.fixture()
        before = self.copper(b)
        os.environ["PNR_PLANE_ACCESS_REUSE_EXISTING"] = "0"
        try:
            with self.assertRaisesRegex(ValueError, "foreign copper"):
                replace_power_array(b, dict(INTENT), FAB)
        finally:
            del os.environ["PNR_PLANE_ACCESS_REUSE_EXISTING"]
        self.assertEqual(self.copper(b), before)
        via.SetPosition(via.GetPosition() + k.VECTOR2I(50000, 0))
        before = self.copper(b)
        with self.assertRaisesRegex(ValueError, "foreign copper"):
            replace_power_array(b, dict(INTENT), FAB)
        self.assertEqual(self.copper(b), before)

    def test_bank_without_current_sized_copper_is_not_kept(self):
        from pnr.plane_access import replace_power_array

        # Vias only: pads no longer reach the bank; the replacement path's diagnostic stands.
        b, _ = self.fixture(foreign=False)
        for t in list(b.GetTracks()):
            if t.GetClass() != "PCB_VIA":
                b.Remove(t)
        before = self.copper(b)
        with self.assertRaisesRegex(ValueError, "hole spacing"):
            replace_power_array(b, dict(INTENT), FAB)
        self.assertEqual(self.copper(b), before)
        # Undersized bus/feeds: rebuilt at current-carrying widths, as without reuse.
        b, _ = self.fixture(foreign=False)
        planned = sorted(t.GetWidth() for t in b.GetTracks() if t.GetClass() == "PCB_TRACK")
        for t in b.GetTracks():
            if t.GetClass() == "PCB_TRACK":
                t.SetWidth(150000)
        r = replace_power_array(b, dict(INTENT), FAB)
        self.assertNotIn("existing_array", r)
        self.assertEqual(
            sorted(t.GetWidth() for t in b.GetTracks() if t.GetClass() == "PCB_TRACK"), planned
        )

    def test_bank_violating_board_rules_is_not_kept(self):
        import pcbnew as k

        from pnr.plane_access import replace_power_array

        def via_at(b, net, pos):
            v = k.PCB_VIA(b)
            v.SetPosition(pos)
            v.SetViaType(k.VIATYPE_THROUGH)
            v.SetLayerPair(k.F_Cu, k.B_Cu)
            v.SetFrontWidth(600000)
            v.SetDrill(300000)
            v.SetNetCode(net)
            b.Add(v)
            v.thisown = False

        def hv(b):
            return b.FindNet("hv").GetNetCode()

        def short(b, via):
            t = k.PCB_TRACK(b)
            t.SetLayer(k.B_Cu)
            t.SetWidth(200000)
            t.SetNetCode(hv(b))
            t.SetStart(via.GetPosition())
            t.SetEnd(via.GetPosition() + k.VECTOR2I(0, 1000000))
            b.Add(t)
            t.thisown = False

        def overlap(b, via):
            via_at(b, hv(b), via.GetPosition() + k.VECTOR2I(100000, 0))

        def clearance(b, via):
            b.GetDesignSettings().m_MinClearance = 200000  # 0.16 mm neighbour now illegal

        for name, mutate, expected in (
            ("short", short, "external layer port"),
            ("overlap", overlap, "foreign copper"),
            ("clearance", clearance, "foreign copper"),
        ):
            with self.subTest(name):
                b, via = self.fixture()
                mutate(b, via)
                before = self.copper(b)
                with self.assertRaisesRegex(ValueError, expected):
                    replace_power_array(b, dict(INTENT), FAB)
                self.assertEqual(self.copper(b), before)
        # Hole-to-hole: a foreign via 0.3 mm (copper) / 0.6 mm (holes) away passes the fab
        # prechecks, so a 0.7 mm board hole rule sends the bank to the replacement path.
        b, via = self.fixture(foreign=False)
        via_at(b, hv(b), via.GetPosition() + k.VECTOR2I(900000, 0))
        self.assertTrue(replace_power_array(b, dict(INTENT), FAB)["existing_array"])
        b.GetDesignSettings().m_HoleToHoleMin = 700000
        self.assertNotIn("existing_array", replace_power_array(b, dict(INTENT), FAB))


if __name__ == "__main__":
    unittest.main()
