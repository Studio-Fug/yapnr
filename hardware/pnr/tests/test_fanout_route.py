"""Declared fanouts in the detailed router (pnr.route.detail.fanout, route_board)."""

import math
import unittest

from pnr.constraints import compile_constraints, compile_routing_rules
from pnr.graph import BoardGraph, BoardOutline, Component, Net, Pad
from pnr.route.detail.router import _late_copper, route_board

PITCH = 0.65
FAB = {
    "track_width_mm": 0.1,
    "clearance_mm": 0.1,
    "via_diameter_mm": 0.4,
    "via_drill_mm": 0.2,
    "hole_clearance_mm": 0.1524,
    "edge_clearance_mm": 0.3,
    "min_through_drill_mm": 0.15,
    "via_annular_mm": 0.0762,
    "smd_pad_clearance_mm": 0.1,
    "hole_to_hole_mm": 0.28,
    "via_to_smd_pad_mm": 0.1,
}
STACK = {
    "layers": [
        {"name": "F.Cu", "type": "signal", "zones": []},
        {"name": "In1.Cu", "type": "power", "zones": ["GND"]},
        {"name": "In2.Cu", "type": "signal", "zones": []},
        {"name": "B.Cu", "type": "signal", "zones": []},
    ]
}


def board(n=6):
    """An n x n 0.65 mm array at (10, 10): the outer two rings signals S_*, the rest
    GND; each signal has a target pad on the board's edges."""
    half = (n - 1) / 2
    pads, pins = [], {}
    for r in range(n):
        for c in range(n):
            name = "%s%d" % ("ABCDEFGH"[r], c + 1)
            ring = min(r, c, n - 1 - r, n - 1 - c)
            net = ("S_" + name) if ring < 2 and (r + c) % 2 == 0 else "GND"
            pads.append(
                Pad(
                    name,
                    net,
                    ((c - half) * PITCH, (half - r) * PITCH),
                    (0.32, 0.32),
                    land_corner=0.16,
                )
            )
            pins.setdefault(net, []).append(("U1", name))
    comps = [Component("U1", "bga", (10.0, 10.0), 0.0, "top", (5.0, 5.0), (5.0, 5.0), True, pads)]
    signals = sorted(n for n in pins if n.startswith("S_"))
    for k, net in enumerate(signals):
        side = k % 4
        t = 2.0 + 16.0 * (k // 4 + 0.5) / ((len(signals) + 3) // 4)
        at = [(t, 1.0), (t, 19.0), (1.0, t), (19.0, t)][side]
        ref = "T%d" % k
        comps.append(
            Component(
                ref,
                "t",
                at,
                0.0,
                "top",
                (0.6, 0.6),
                (0.6, 0.6),
                pads=[Pad("1", net, (0, 0), (0.3, 0.3), land_corner=0.15)],
            )
        )
        pins[net].append((ref, "1"))
    comps.append(
        Component(
            "TG",
            "t",
            (19.0, 1.0),
            0.0,
            "top",
            (0.6, 0.6),
            (0.6, 0.6),
            pads=[Pad("1", "GND", (0, 0), (0.3, 0.3), land_corner=0.15)],
        )
    )
    pins["GND"].append(("TG", "1"))
    nets = [Net(name, i + 1, p) for i, (name, p) in enumerate(sorted(pins.items()))]
    g = BoardGraph("fanout-route", comps, nets, BoardOutline(20, 20))
    g.stack = STACK
    return g


def setup(fanout=True):
    g = board()
    doc = {
        "schema": "v0",
        "board": {"outline": {"w": 20, "h": 20}, "layers": 4},
        "fab": FAB,
        "fixed": {"U1": {"at": [10, 10], "rot": 0, "side": "top"}},
        "net_class": {"gnd": {"nets": ["GND"], "plane_layer": "In1.Cu"}},
    }
    if fanout:
        doc["fanout"] = [
            {
                "ref": "U1",
                "via_classes": {
                    "ground": {
                        "diameter_mm": 0.35,
                        "drill_mm": 0.15,
                        "nets": ["GND"],
                        "sites": ["interstitial"],
                    },
                    "default": {
                        "diameter_mm": 0.4,
                        "drill_mm": 0.2,
                        "sites": ["vacant", "outside", "interstitial"],
                    },
                },
            }
        ]
    c = compile_constraints(doc, g.refs)
    return g, c, compile_routing_rules(c, [n.name for n in g.nets])


class FanoutRouteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.g, cls.c, cls.rules = setup()
        cls.route = route_board(cls.g, cls.c, cls.rules, pitch=0.25, max_iters=6)

    def test_every_signal_routes_through_its_fanout(self):
        r = self.route
        self.assertEqual(r.result.unrouted, [])
        report = r.escape_diagnostics["fanout"]["U1"]
        self.assertEqual(report["signals_escaped"], report["signals"])
        self.assertEqual(report["drops_placed"], report["drops"])
        self.assertEqual(report["no_access"], [])
        self.assertEqual(report["conflicts_on_board"], [])

    def test_drop_vias_keep_their_class_and_the_copper_is_locked(self):
        r = self.route
        sizes = {(n, x, y): (d, h) for n, x, y, d, h in r.via_sizes}
        drops = [v for v in r.vias if v[0] == "GND" and math.dist(v[1:], (10, 10)) < 3]
        self.assertTrue(drops)
        for v in drops:
            self.assertEqual(sizes.get(v), (0.35, 0.15))
        self.assertTrue(r.extras()["locked"]["tracks"])
        locked = {tuple(v) for v in r.locked["vias"]}
        self.assertTrue({tuple(v) for v in drops} <= locked)

    def test_fanned_out_pads_are_not_late_copper(self):
        from pnr.route.detail.fanout import plan_fanouts
        from pnr.route.detail.grid import RouteGrid

        grid = RouteGrid.from_graph(
            self.g,
            20,
            20,
            pitch=0.25,
            layers=("F.Cu", "In2.Cu", "B.Cu"),
            clearance=0.1,
            track_width=0.1,
            via_radius=0.2,
        )
        signals = {n.name for n in self.g.nets if n.name != "GND"}
        fo = plan_fanouts(
            grid, self.g, self.rules, plane_nets={"GND"}, signal_nets=signals, via_keepout=1
        )
        self.assertIsNone(_late_copper(self.g, {"GND"}, set(), fo.escapes + self._target_drop()))
        self.assertEqual(len(fo.skip_pads), 36)

    def _target_drop(self):
        from pnr.route.detail.escape import Escape
        from pnr.route.detail.grid import Cell

        return [Escape("GND", "joint", Cell(0, 0, 0), (19.0, 1.0))]

    def test_fanout_copper_is_where_the_plan_put_it(self):
        from pnr.fanout import cached_plan, classify

        layers, drops, signals = classify(self.g, self.rules)
        plan = cached_plan(
            self.g,
            self.rules,
            self.rules["fanouts"][0],
            grid_layers=layers,
            plane_nets=drops,
            signal_nets=signals,
        )
        tracks = {(t[0], t[1], tuple(map(tuple, t[2:4]))) for t in self.route.tracks}
        for net, layer, a, b, _w in plan["copper"]["tracks"]:
            self.assertIn((net, layer, (tuple(a), tuple(b))), tracks)

    def test_no_extras_without_a_fanout(self):
        g, c, rules = setup(fanout=False)
        self.assertNotIn("fanouts", rules)
        r = route_board(g, c, rules, pitch=0.25, max_iters=2)
        self.assertEqual(r.extras(), {})
        self.assertNotIn("fanout", r.escape_diagnostics)
        self.assertTrue(all(math.isfinite(v[1]) for v in r.vias))


if __name__ == "__main__":
    unittest.main()
