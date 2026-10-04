"""A board's custom rules where they constrain routing (``board.dru_routing: true``).

The fixture is the rules file of the radar60 example board (a fab profile part and
the board's own rules, trimmed): a no-via crystal class, LVDS on the outer layers, two
switch-node pair clearances, a QSPI length limit, the hole-to-edge rule written for a
0.05 mm outline, area rules and sizes. The parser maps what it can state exactly and
lists the rest; route_board applies the effects.
"""

import unittest

from pnr.constraints import compile_constraints, compile_routing_rules
from pnr.dru_rules import Item, attach_dru, evaluate, parse, routing_rules
from pnr.graph import BoardGraph, Component, Net, Pad
from pnr.route.detail.router import route_board

DRU = """(version 1)
# ---- Part 1: profile
(rule "smd_pad_to_pad"
  (constraint clearance (min 0.1mm))
  (condition "A.Type == 'Pad' && B.Type == 'Pad' && A.Pad_Type == 'SMD' && B.Pad_Type == 'SMD'"))
(rule "via_to_smd_pad"
  (constraint physical_clearance (min 0.1mm))
  (condition "A.Type == 'Via' && B.Type == 'Pad' && B.Pad_Type == 'SMD'"))
(rule "hole_to_edge"
  (constraint physical_hole_clearance (min 0.475mm))
  (condition "A.Layer == 'Edge.Cuts' || B.Layer == 'Edge.Cuts'"))
# ---- Part 2: board rules
(rule "rf_region_tracks"
  (constraint disallow track)
  (condition "A.Type == 'Track' && A.intersectsArea('RF_REGION') && !A.hasNetclass('RF')"))
(rule "rf_fence_vias"
  (constraint hole_size (min 0.15mm))
  (constraint via_diameter (min 0.32mm))
  (condition "A.Type == 'Via' && A.intersectsArea('RF_REGION')"))
(rule "xtal_no_vias"
  (constraint disallow via)
  (condition "A.Type == 'Via' && A.hasNetclass('XTAL')"))
(rule "sw_to_xtal"
  (constraint clearance (min 8mm))
  (condition "A.hasNetclass('SW') && B.hasNetclass('XTAL')"))
(rule "clk_to_default"
  (constraint clearance (min 0.25mm))
  (condition "A.hasNetclass('CLK') && B.NetClass == 'Default'"))
(rule "lvds_pairs"
  (constraint track_width (min 0.15mm) (opt 0.17mm))
  (constraint diff_pair_gap (min 0.16mm) (opt 0.18mm) (max 0.22mm))
  (condition "A.hasNetclass('LVDS') && !A.intersectsCourtyard('U1')"))
(rule "lvds_outer_layers"
  (constraint disallow track)
  (condition "A.Type == 'Track' && A.hasNetclass('LVDS') && A.Layer != 'F.Cu' && A.Layer != 'B.Cu'"))
(rule "qspi_length"
  (constraint length (max 25mm))
  (condition "A.hasNetclass('QSPI')"))
(rule "plane_in1"
  (constraint disallow track)
  (condition "A.Type == 'Track' && A.Layer == 'In1.Cu'"))
"""

RULES = {
    "layers": 4,
    "fab": {"clearance_mm": 0.1, "via_drill_mm": 0.2, "hole_to_edge_mm": 0.5},
    "net_classes": [
        {"name": "XTAL", "nets": ["XTAL_P", "XTAL_N"], "clearance_mm": 0.15},
        {"name": "SW", "nets": ["SW1"], "clearance_mm": 0.2},
        {"name": "LVDS", "nets": ["TX_P", "TX_N"]},
        {"name": "QSPI", "nets": ["Q0", "Q1"]},
        {"name": "CLK", "nets": ["CLK"]},
    ],
    "diff_pairs": [{"name": "tx", "p": "TX_P", "n": "TX_N"}],
}
NETS = ["XTAL_P", "XTAL_N", "SW1", "TX_P", "TX_N", "Q0", "Q1", "CLK", "GPIO1", "GND"]


class ParseTest(unittest.TestCase):
    def test_rules_and_constraints(self):
        rules = {r["name"]: r for r in parse(DRU)}
        self.assertEqual(len(rules), 12)
        (c,) = rules["sw_to_xtal"]["constraints"]
        self.assertEqual((c["type"], c["min"]), ("clearance", 8.0))
        self.assertEqual(rules["xtal_no_vias"]["constraints"][0]["items"], ["via"])
        self.assertEqual(len(rules["lvds_pairs"]["constraints"]), 2)

    def test_three_valued_conditions(self):
        via = Item("Via", "XTAL_P", {"XTAL"}, None, 0.2)
        self.assertIs(evaluate("A.Type == 'Via' && A.hasNetclass('XTAL')", via), True)
        self.assertIs(evaluate("A.Type == 'Track' && A.intersectsArea('X')", via), False)
        unknown = evaluate("A.Type == 'Via' && A.intersectsArea('X')", via)
        self.assertNotIn(unknown, (True, False))
        self.assertIs(evaluate("A.Hole <= 0.155mm", via), False)
        self.assertIs(evaluate("!(A.NetClass == 'SW') || A.NetName == 'X'", via), True)


class MappingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dru = routing_rules(DRU, RULES, NETS)

    def test_no_via_and_layers(self):
        self.assertEqual(self.dru["no_via"], ["XTAL_N", "XTAL_P"])
        self.assertEqual(self.dru["track_layers"]["TX_P"], ["F.Cu", "B.Cu"])
        # The In1.Cu ban holds for every net.
        self.assertEqual(self.dru["track_layers"]["GPIO1"], ["F.Cu", "In2.Cu", "B.Cu"])

    def test_pairs_lengths_and_edge(self):
        pairs = {p["rule"]: p for p in self.dru["pair_clearances"]}
        self.assertEqual(pairs["sw_to_xtal"]["a"], ["SW1"])
        self.assertEqual(pairs["sw_to_xtal"]["b"], ["XTAL_N", "XTAL_P"])
        self.assertEqual(pairs["sw_to_xtal"]["clearance_mm"], 8.0)
        # 'Default' is the class of a net without one.
        self.assertEqual(pairs["clk_to_default"]["b"], ["GND", "GPIO1"])
        self.assertEqual(self.dru["length_max"], {"Q0": 25.0, "Q1": 25.0})
        self.assertEqual(self.dru["hole_to_edge_mm"], 0.475)

    def test_the_rest_is_listed(self):
        listed = {(u["rule"], u["constraint"]) for u in self.dru["unmodelled"]}
        for key in [
            ("smd_pad_to_pad", "clearance"),
            ("via_to_smd_pad", "physical_clearance"),
            ("rf_region_tracks", "disallow"),
            ("rf_fence_vias", "hole_size"),
            ("rf_fence_vias", "via_diameter"),
            ("lvds_pairs", "track_width"),
            ("lvds_pairs", "diff_pair_gap"),
        ]:
            self.assertIn(key, listed)
        reasons = {u["rule"]: u["reason"] for u in self.dru["unmodelled"]}
        self.assertIn("intersectsArea", reasons["rf_region_tracks"])

    def test_unreadable_condition_is_listed_not_applied(self):
        text = '(version 1)\n(rule "odd" (constraint disallow via) (condition "A.Type ~~ 3"))\n'
        dru = routing_rules(text, RULES, NETS)
        self.assertEqual(dru["no_via"], [])
        self.assertIn("unreadable", dru["unmodelled"][0]["reason"])

    def test_attach_only_when_asked(self):
        rules = dict(RULES)
        attach_dru(rules, DRU, NETS)
        self.assertNotIn("dru", rules)
        rules["dru_routing"] = True
        attach_dru(rules, DRU, NETS)
        self.assertEqual(rules["dru"]["source"], "kicad_dru")
        rules = dict(RULES, dru_routing=True)
        attach_dru(rules, None, NETS)
        self.assertEqual(rules["dru"]["source"], "none")


# ----------------------------------------------------------------- route_board

FAB = dict(
    track_width_mm=0.15,
    clearance_mm=0.1,
    via_diameter_mm=0.4,
    via_drill_mm=0.2,
    hole_clearance_mm=0.15,
)


def _part(ref, x, y, net, w=0.4, h=0.4, side="top"):
    return Component(
        ref=ref,
        footprint="test:" + ref,
        pos=(x, y),
        rot=0.0,
        side=side,
        courtyard=(w + 0.2, h + 0.2),
        bbox=(w + 0.2, h + 0.2),
        pads=[Pad(name="1", net=net, offset=(0.0, 0.0), size=(w, h))],
    )


def wall_board():
    """A 14 x 16 mm two-layer board: X (class XTAL) between two top pads with a top
    wall of no-net copper between them that only a gap 9 mm away lets a top track
    past (two vias under it are cheaper); S (class SW) between two pads 4.2 mm from
    X's."""
    parts = [
        _part("X1", 2.0, 5.0, "X"),
        _part("X2", 12.0, 5.0, "X"),
        _part("W1", 7.0, 6.75, "", 0.6, 13.5),  # y 0-13.5, top only: a via would cross
        _part("S1", 2.0, 0.8, "S"),
        _part("S2", 12.0, 0.8, "S"),
    ]
    nets = [
        Net(name="X", code=1, pins=[("X1", "1"), ("X2", "1")]),
        Net(name="S", code=2, pins=[("S1", "1"), ("S2", "1")]),
    ]
    return BoardGraph(name="wall", components=parts, nets=nets)


BOARD_DRU = """(version 1)
(rule "no vias on X" (constraint disallow via) (condition "A.Type == 'Via' && A.hasNetclass('XTAL')"))
(rule "S far from X" (constraint clearance (min 3mm)) (condition "A.hasNetclass('SW') && B.hasNetclass('XTAL')"))
(rule "X short" (constraint length (max 12mm)) (condition "A.hasNetclass('XTAL')"))
"""


def _route(dru_on):
    graph = wall_board()
    board = {"outline": {"w": 14, "h": 16}}
    if dru_on:
        board["dru_routing"] = True
    doc = {
        "board": board,
        "net_class": {"XTAL": {"nets": ["X"]}, "SW": {"nets": ["S"]}},
    }
    compiled = compile_constraints(doc, graph.refs)
    rules = compile_routing_rules(compiled, ["X", "S"])
    rules["fab"] = dict(FAB)
    attach_dru(rules, BOARD_DRU, ["X", "S"])
    return route_board(graph, compiled, rules, pitch=0.25)


class RouteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.off = _route(False)
        cls.on = _route(True)

    def test_off_vias_under_the_wall(self):
        self.assertTrue([v for v in self.off.vias if v[0] == "X"])
        self.assertNotIn("dru", self.off.escape_diagnostics)

    def test_on_no_via_net_stays_on_its_layer(self):
        self.assertEqual([v for v in self.on.vias if v[0] == "X"], [])
        self.assertEqual({t[1] for t in self.on.tracks if t[0] == "X"}, {"F.Cu"})
        report = self.on.escape_diagnostics["dru"]
        self.assertEqual(report["no_via"], ["X"])

    def test_on_pair_keepout_and_length(self):
        from pnr.place.geometry import Rect
        from pnr.route.detail.class_check import _segment_rect

        report = self.on.escape_diagnostics["dru"]
        self.assertTrue(report["pair_keepouts"][0]["cells"] > 0)
        # S keeps 3 mm from X's pads (its track centre 3 mm + half width).
        pads = [Rect(2.0, 5.0, 0.4, 0.4), Rect(12.0, 5.0, 0.4, 0.4)]
        s_tracks = [t for t in self.on.tracks if t[0] == "S"]
        self.assertTrue(s_tracks)
        for _net, _layer, a, b, w in s_tracks:
            for pad in pads:
                self.assertGreaterEqual(_segment_rect(a, b, pad), 3.0 + w / 2 - 1e-6)
        length = report["length"]["X"]
        self.assertEqual(length["max_mm"], 12.0)
        self.assertEqual(length["ok"], length["length_mm"] <= 12.0)
        self.assertEqual(report["audit"]["count"], 0)


if __name__ == "__main__":
    unittest.main()
