"""Plane-partition quality (pnr.plane_quality): the metrics a partition reports per
rail and per layer, the penalty and warnings built on them, and the plain-Python judge
the ladder's ``plane_quality`` check runs on a routed board's filled zones."""

import math
import unittest

from pnr.plane_partition import _CACHE, Terminal, finish_quality, partition
from pnr.plane_quality import LIMITS, emst, judge, penalty, raster_polygons, warnings

W, H = 30.0, 20.0
ENTRY = dict(
    layer="In4.Cu",
    order="current",
    split_gap_mm=0.3,
    min_width_mm=1.0,
    fill=None,
    core_no_vias=True,
    terminal_reach_mm=0.8,
    currents={},
    budgets_mohm={},
    sources={},
    h_mm=0.2,
)


def via(name, at):
    return Terminal(name, "via", at, 0.2)


def pad(name, at):
    return Terminal(name, "pad", at, 0.8)


# The main rail: a mesh of terminals over the middle of the board.
MAIN = [via("M%d" % k, (8.0 + 2.0 * (k % 7), 6.0 + 4.0 * (k // 7))) for k in range(14)]
# A trickle rail: a header in a corner and one far terminal.
TRICKLE = [via("T1", (2.0, 18.0)), via("T2", (26.0, 3.0))]


def run(nets, fill=None, terms=None, blocked=(), currents=None):
    _CACHE.clear()
    terms = terms or {"MAIN": MAIN, "TRICKLE": TRICKLE}
    entry = dict(ENTRY, nets=list(nets), fill=fill)
    return partition(
        entry,
        width=W,
        height=H,
        terminals={n: terms[n] for n in nets},
        blocked=list(blocked),
        currents=currents or {"MAIN": 0.5, "TRICKLE": 0.001},
    )


class MetricsTest(unittest.TestCase):
    def test_emst_of_a_unit_square_is_three(self):
        self.assertAlmostEqual(emst([(0, 0), (1, 0), (1, 1), (0, 1)]), 3.0)
        self.assertEqual(emst([(5, 5)]), 0.0)

    def test_every_rail_reports_every_metric(self):
        part = run(["MAIN", "TRICKLE"])
        q = part.report["quality"]
        for net in ("MAIN", "TRICKLE"):
            row = q["nets"][net]
            for key in (
                "area_mm2",
                "need_mm2",
                "area_ratio",
                "compactness",
                "trunk_mm",
                "steiner_mm",
                "detour",
                "blocked",
                "dead_mm2",
                "neck_mm",
                "ir_drop_mv",
                "frag_pieces",
                "frag_cut_mm2",
                "split_mm",
            ):
                self.assertIn(key, row)
            self.assertGreater(row["need_mm2"], 0.0)
            self.assertLessEqual(row["compactness"], 1.0)
        self.assertEqual(q["layer"]["rails"], ["MAIN", "TRICKLE"])

    def test_competing_rails_give_a_trickle_rail_far_more_than_it_needs(self):
        # Without an owner the rails compete for the leftover: the 1 mA rail ends up
        # with many times its need, which the metrics and warnings flag.
        part = run(["MAIN", "TRICKLE"])
        q = part.report["quality"]
        row = q["nets"]["TRICKLE"]
        self.assertFalse(row["owner"])
        self.assertTrue(q["nets"]["MAIN"]["owner"])  # the most current
        self.assertGreater(row["area_ratio"], LIMITS["area_ratio_max"])
        self.assertGreater(row["dead_mm2"], 50.0)
        finish_quality(part.report, "In4.Cu")
        codes = {(w["code"], w["net"]) for w in part.report["quality"]["warnings"]}
        self.assertIn(("area_beyond_need", "TRICKLE"), codes)

    def test_a_leftover_owner_keeps_the_others_to_their_trunk_and_apron(self):
        shared = run(["MAIN", "TRICKLE"])
        owned = run(["MAIN", "TRICKLE"], fill="MAIN")
        a = shared.report["quality"]["nets"]["TRICKLE"]
        b = owned.report["quality"]["nets"]["TRICKLE"]
        self.assertLess(b["area_mm2"], a["area_mm2"] / 3)
        self.assertLess(b["area_ratio"], LIMITS["area_ratio_max"])
        self.assertEqual(owned.report["leftover"], "MAIN")
        # No fill zone of its own: the owner's grown territory is the leftover.
        self.assertEqual({r.net for r in owned.regions}, {"MAIN", "TRICKLE"})
        self.assertTrue(all(r.outline is not None for r in owned.regions))

    def test_one_rail_owns_the_layer_with_nothing_dead_or_split(self):
        part = run(["MAIN"])
        q = part.report["quality"]
        self.assertTrue(q["nets"]["MAIN"]["owner"])
        finish_quality(part.report, "In4.Cu")
        total, parts = penalty(part.report["quality"])
        self.assertEqual(parts.get("dead", 0.0), 0.0)
        self.assertEqual(parts.get("split", 0.0), 0.0)
        self.assertEqual(total, sum(parts.values()))

    def test_a_pad_terminal_walled_in_by_foreign_copper_is_blocked(self):
        walled = [pad("B1", (15.0, 10.0)), via("B2", (25.0, 10.0))]
        ring = [
            (
                (15.0 + 1.2 * math.cos(a / 8 * math.tau), 10.0 + 1.2 * math.sin(a / 8 * math.tau)),
                1.0,
            )
            for a in range(8)
        ]
        ring.append(((15.0, 10.0), 1.0))
        part = run(["MAIN", "B"], terms={"MAIN": MAIN[:3], "B": walled}, blocked=ring)
        row = part.report["quality"]["nets"]["B"]
        self.assertEqual(row["blocked"], ["B1"])
        finish_quality(part.report, "In4.Cu", via_blocked={"B": ["B1"]})
        _total, parts = penalty(part.report["quality"])
        self.assertGreaterEqual(parts["unreached"], 100.0)  # blocked on the layer and the grid
        codes = [w["code"] for w in part.report["quality"]["warnings"]]
        self.assertIn("unreachable_terminal", codes)

    def test_a_fill_net_with_its_own_plane_elsewhere_is_redundant(self):
        part = run(["MAIN"], fill="GND")
        finish_quality(part.report, "In4.Cu", planes_elsewhere={"GND": ["In1.Cu", "In3.Cu"]})
        q = part.report["quality"]
        self.assertEqual(q["layer"]["redundant_fill"]["net"], "GND")
        self.assertEqual(q["layer"]["redundant_fill"]["planes"], ["In1.Cu", "In3.Cu"])
        self.assertIn("redundant_fill", q["penalty"])
        self.assertIn("redundant_fill", [w["code"] for w in q["warnings"]])
        # Without planes elsewhere the same fill is no redundancy.
        finish_quality(part.report, "In4.Cu", planes_elsewhere={"GND": []})
        self.assertNotIn("redundant_fill", part.report["quality"]["layer"])

    def test_ir_margin_is_the_budget_less_the_trunk_drop(self):
        _CACHE.clear()
        entry = dict(ENTRY, nets=["MAIN"])
        part = partition(
            entry,
            width=W,
            height=H,
            terminals={"MAIN": MAIN},
            blocked=[],
            currents={"MAIN": 0.5},
            budgets_mohm={"MAIN": 100.0},
        )
        row = part.report["quality"]["nets"]["MAIN"]
        drop_mohm = row["ir_drop_mv"] / 0.5
        self.assertAlmostEqual(row["ir_margin_mv"], (100.0 - drop_mohm) * 0.5, places=2)


def square(x0, y0, x1, y1):
    return [[(x0, y0), (x1, y0), (x1, y1), (x0, y1)]]


class JudgeTest(unittest.TestCase):
    """The plain-Python judge on hand-made filled zones (mm)."""

    def layer(self, zones, terminals, **extra):
        out = dict(
            outline=[0.0, 0.0, 30.0, 20.0],
            zones=zones,
            terminals=terminals,
            candidates=["VDD", "VBAT"],
            currents={"VDD": 0.15, "VBAT": 0.001},
            min_width_mm=1.0,
            planes_elsewhere={},
        )
        out.update(extra)
        return out

    def test_raster_counts_cell_centres_inside(self):
        cells = raster_polygons([square(0.0, 0.0, 1.0, 1.0)], 0.0, 0.0, 10, 10, 0.2)
        self.assertEqual(len(cells), 25)
        holed = [square(0.0, 0.0, 2.0, 2.0)[0], [(0.6, 0.6), (1.4, 0.6), (1.4, 1.4), (0.6, 1.4)]]
        self.assertEqual(len(raster_polygons([holed], 0.0, 0.0, 10, 10, 0.2)), 100 - 16)

    def test_one_rail_owning_the_layer_passes(self):
        vdd = [(5.0 + 2 * k, 10.0, 0.2) for k in range(8)]
        ok, measured, _ = judge(
            self.layer({"VDD": [square(0.5, 0.5, 29.5, 19.5)]}, {"VDD": vdd}, candidates=["VDD"])
        )
        self.assertTrue(ok, measured["failures"])
        self.assertEqual(measured["owner"], "VDD")

    def test_the_n0001_layout_fails_on_area_detour_and_redundant_fill(self):
        # VDD over the middle, VBAT holding a third of the layer for a 1 mA rail whose
        # trunk winds round VDD, the rest a GND fill although GND has its own planes.
        # VBAT: an upturned U from its header (2, 2) round the top to its ball (12, 2).
        vdd = [(16.0 + 2 * k, 8.0, 0.2) for k in range(5)]
        vbat_zone = [
            [
                (0.5, 0.5),
                (3.5, 0.5),
                (3.5, 15.0),
                (10.5, 15.0),
                (10.5, 0.5),
                (13.5, 0.5),
                (13.5, 19.5),
                (0.5, 19.5),
            ]
        ]
        zones = {
            "VDD": [square(15.0, 2.0, 26.0, 13.0)],
            "VBAT": [vbat_zone],
            "GND": [square(26.5, 0.5, 29.5, 13.0)],
        }
        terms = {"VDD": vdd, "VBAT": [(2.0, 2.0, 0.5), (12.0, 2.0, 0.2)]}
        ok, measured, limits = judge(
            self.layer(zones, terms, planes_elsewhere={"GND": ["In1.Cu", "In3.Cu"]})
        )
        self.assertFalse(ok)
        text = " ".join(measured["failures"])
        self.assertIn("GND: fill GND is redundant", text)
        self.assertIn("VBAT", text)
        (piece,) = measured["nets"]["VBAT"]["pieces"]
        self.assertGreater(piece["area_ratio"], limits["area_ratio_max"])
        self.assertGreater(piece["detour"], limits["detour_max"])

    def test_a_region_serving_one_terminal_fails_on_a_plane_not_a_pour(self):
        zones = {"VDD": [square(0.5, 0.5, 29.5, 19.5)], "VBAT": [square(1.0, 1.0, 3.0, 3.0)]}
        vdd = [(10.0, 10.0, 0.2), (20.0, 10.0, 0.2)]
        layer = self.layer(zones, {"VDD": vdd, "VBAT": [(2.0, 2.0, 0.3)]})
        ok, measured, _ = judge(layer)
        self.assertFalse(ok)
        self.assertIn("serves 1 terminal", " ".join(measured["failures"]))
        _ok, measured, _ = judge(dict(layer, lands=True))
        self.assertNotIn("serves 1 terminal", " ".join(measured["failures"]))

    def test_slivers_under_a_square_millimetre_are_counted_not_judged(self):
        zones = {"VDD": [square(0.5, 0.5, 29.5, 19.5), square(0.0, 19.6, 0.4, 20.0)]}
        vdd = [(10.0, 10.0, 0.2), (20.0, 10.0, 0.2)]
        ok, measured, _ = judge(self.layer(zones, {"VDD": vdd}, candidates=["VDD"]))
        self.assertTrue(ok, measured["failures"])
        self.assertEqual(measured["nets"]["VDD"]["slivers"], 1)

    def test_limits_may_be_tightened_per_check(self):
        vdd = [(10.0, 10.0, 0.2), (20.0, 10.0, 0.2)]
        zones = {"VDD": [square(0.5, 0.5, 29.5, 19.5)], "VBAT": [square(1.0, 1.0, 9.0, 3.0)]}
        terms = {"VDD": vdd, "VBAT": [(1.5, 2.0, 0.2), (8.5, 2.0, 0.2)]}
        ok, _m, _l = judge(self.layer(zones, terms))
        self.assertTrue(ok)
        ok, measured, limits = judge(self.layer(zones, terms), limits=dict(area_ratio_max=1.2))
        self.assertFalse(ok)
        self.assertEqual(limits["area_ratio_max"], 1.2)

    def test_warnings_carry_value_limit_and_message(self):
        part = run(["MAIN", "TRICKLE"])
        finish_quality(part.report, "In4.Cu")
        for w in warnings(part.report["quality"], "In4.Cu"):
            self.assertEqual(set(w), {"code", "layer", "net", "value", "limit", "message"})


if __name__ == "__main__":
    unittest.main()
