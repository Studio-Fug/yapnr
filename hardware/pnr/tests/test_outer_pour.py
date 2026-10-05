"""Outer-layer pours of a plane partition with a region (stage 3c E1).

A power stage whose hot-rod lands (0.25 x 1.82 mm at 0.5 mm pitch) no track can
enter at its class clearance: ``plane_partition`` with ``region``, ``terminals: pad``
and ``connect: solid`` gives each rail a pour on F.Cu that owns its whole lands. The
router claims the pours, plans no escape or drop for the pads they join, joins a
rail's other pads to its pour, and stitches a plane net's pour to its plane.
"""

import math
import unittest

from pnr.constraints import ConstraintError, compile_constraints, compile_routing_rules
from pnr.graph import BoardGraph, BoardOutline, Component, Net, Pad
from pnr.place.geometry import pad_rects
from pnr.plane_partition import region_polygon, regions_from_rows
from pnr.power_spec import PowerSpecError, parse_partition

HOT = (0.25, 1.82)


def hotrod(ref, at, nets):
    """A VQFN-HR-like row of hot-rod lands, 0.5 mm apart, plus a small signal pin."""
    pads = [
        Pad(str(k + 1), net, ((k - (len(nets) - 1) / 2) * 0.5, 0.0), HOT, land_corner=0.0)
        for k, net in enumerate(nets)
    ]
    pads.append(Pad("FB", "FB", (2.2, -0.6), (0.3, 0.5), land_corner=0.0))
    return Component(ref, "VQFN-HR", at, 0.0, "top", (5.4, 2.6), (5.4, 2.6), pads=pads)


def two_pad(ref, at, a, b, size=(0.9, 0.95), pitch=1.6):
    pads = [
        Pad("1", a, (-pitch / 2, 0.0), size, land_corner=0.0),
        Pad("2", b, (pitch / 2, 0.0), size, land_corner=0.0),
    ]
    return Component(ref, "2pad", at, 0.0, "top", (pitch + size[0] + 0.4, 1.6), (1, 1), pads=pads)


def board():
    comps = [
        hotrod("U2", (8.0, 8.0), ["SW", "SW", "GND", "GND", "VIN", "VIN"]),
        two_pad("L1", (7.1, 5.0), "SW", "VOUT", size=(1.2, 1.6), pitch=2.4),
        two_pad("C1", (8.5, 11.0), "GND", "VIN"),
        two_pad("J1", (18.0, 8.0), "VIN", "GND"),
        two_pad("R1", (16.0, 3.0), "VOUT", "FB"),
    ]
    pins = {}
    for c in comps:
        for p in c.pads:
            pins.setdefault(p.net, []).append((c.ref, p.name))
    nets = [Net(n, k + 1, p) for k, (n, p) in enumerate(sorted(pins.items()))]
    g = BoardGraph("pour", comps, nets, BoardOutline(22, 14))
    g.stack = {
        "layers": [
            {"name": "F.Cu", "type": "signal", "zones": [], "copper_mm": 0.035},
            {"name": "In1.Cu", "type": "power", "zones": [], "copper_mm": 0.035},
            {"name": "In2.Cu", "type": "signal", "zones": [], "copper_mm": 0.035},
            {"name": "B.Cu", "type": "signal", "zones": [], "copper_mm": 0.035},
        ]
    }
    return g


POUR = {
    "layer": "F.Cu",
    "nets": ["SW", "GND", "VIN"],
    "split_gap_mm": 0.2,
    "min_width_mm": 0.25,
    "region": {"refs": ["U2", "L1", "C1"], "margin_mm": 0.4},
    "terminals": "pad",
    "connect": "solid",
    "stitch_vias": 2,
}


def setup(pour=True):
    g = board()
    doc = {
        "schema": "v0",
        "board": {"outline": {"w": 22, "h": 14}, "layers": 4},
        "fab": dict(
            track_width_mm=0.15,
            clearance_mm=0.15,
            via_diameter_mm=0.45,
            via_drill_mm=0.2,
            edge_clearance_mm=0.3,
        ),
        "fixed": {c.ref: {"at": list(c.pos), "rot": 0, "side": "top"} for c in g.components},
        "net_class": {"gnd": {"nets": ["GND"], "plane_layer": "In1.Cu"}},
    }
    if pour:
        doc["plane_partition"] = [dict(POUR)]
    c = compile_constraints(doc, g.refs)
    return g, c, compile_routing_rules(c, [n.name for n in g.nets])


class SpecTest(unittest.TestCase):
    def test_outer_keys_only_with_a_region(self):
        (entry,) = parse_partition([dict(POUR)])
        self.assertEqual(entry["region"], {"refs": ["U2", "L1", "C1"], "margin_mm": 0.4})
        self.assertEqual(
            (entry["terminals"], entry["connect"], entry["stitch_vias"]), ("pad", "solid", 2)
        )
        (plain,) = parse_partition([dict(layer="In2.Cu", nets=["A"])])
        for key in ("region", "terminals", "connect", "stitch_vias"):
            self.assertNotIn(key, plain)
        with self.assertRaisesRegex(PowerSpecError, "go with a region"):
            parse_partition([dict(layer="In2.Cu", nets=["A"], connect="solid")])
        with self.assertRaisesRegex(PowerSpecError, "takes no fill"):
            parse_partition([dict(POUR, fill="GND")])
        with self.assertRaisesRegex(PowerSpecError, "terminals must be"):
            parse_partition([dict(POUR, terminals="land")])
        with self.assertRaisesRegex(PowerSpecError, "polygon"):
            parse_partition([dict(POUR, region=[[0, 0], [1, 1]])])
        (poly,) = parse_partition([dict(POUR, region=[[0, 0], [4, 0], [4, 3]])])
        self.assertEqual(poly["region"], [[0.0, 0.0], [4.0, 0.0], [4.0, 3.0]])

    def test_the_compiler_reports_a_bad_entry(self):
        g = board()
        doc = {"schema": "v0", "board": {"outline": {"w": 22, "h": 14}}}
        doc["plane_partition"] = [dict(POUR, connect="glued")]
        with self.assertRaises(ConstraintError):
            compile_constraints(doc, g.refs)

    def test_region_from_parts(self):
        g = board()
        x0, y0 = region_polygon(g, {"refs": ["U2"], "margin_mm": 0.5})[0]
        self.assertAlmostEqual(x0, 8.0 - 2.7 - 0.5)
        self.assertAlmostEqual(y0, 8.0 - 1.3 - 0.5)


class RouteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from pnr.route.detail.router import route_board

        cls.g, _c, cls.rules = setup()
        cls.r = route_board(cls.g, _c, cls.rules, max_iters=6)
        cls.rows = cls.r.extras()["plane_regions"]
        cls.regions = regions_from_rows(cls.rows)

    def inside(self, net, p):
        from pnr.route.detail.pour import _inside

        return any(_inside(r, p) for r in self.regions if r.net == net)

    def test_the_pours_own_every_land(self):
        pours = self.r.escape_diagnostics["pours"]
        self.assertEqual(pours["SW"]["pads"], ["L1.1", "U2.1", "U2.2"])
        self.assertEqual(pours["GND"]["pads"], ["C1.1", "U2.3", "U2.4"])
        self.assertEqual(pours["VIN"]["pads"], ["C1.2", "U2.5", "U2.6"])
        for row in self.rows:
            self.assertEqual((row["layer"], row["connect"], row["pour"]), ("F.Cu", "solid", True))
            self.assertGreaterEqual(row["priority"], 100)
        report = self.r.escape_diagnostics["plane_partition"][0]
        for net in ("SW", "GND", "VIN"):
            self.assertEqual(report["nets"][net]["unreached"], [], net)
            self.assertEqual(report["nets"][net]["components"], 1, net)

    def test_every_net_is_routed_and_no_other_copper_enters_a_pour(self):
        self.assertEqual(self.r.result.unrouted, [])
        self.assertFalse(self.r.failure_sites)
        # Another net's copper is in a pour only within its own pad's halo (the cells
        # the pad owned before the pour was claimed: KiCad's fill keeps clearance).
        pads = {}
        for comp in self.g.components:
            for _name, net, r in pad_rects(comp):
                pads.setdefault(net, []).append(r)

        def near_own_pad(net, p):
            return any(
                math.hypot(
                    max(r.left - p[0], 0, p[0] - r.right), max(r.bottom - p[1], 0, p[1] - r.top)
                )
                <= 0.15 + 0.15 + 0.3
                for r in pads.get(net, [])
            )

        points = [(n, p) for n, layer, a, b, _w in self.r.tracks if layer == "F.Cu" for p in (a, b)]
        points += [(n, (x, y)) for n, x, y in self.r.vias]
        for net, p in points:
            for other in ("SW", "GND", "VIN"):
                if other != net and self.inside(other, p):
                    self.assertTrue(near_own_pad(net, p), (net, other, p))

    def test_vin_joins_its_pour_and_gnd_is_stitched(self):
        vin = [t for t in self.r.tracks if t[0] == "VIN"]
        self.assertTrue(vin)  # J1.1 to the VIN pour
        self.assertTrue(any(self.inside("VIN", p) for t in vin for p in (t[2], t[3])))
        stitches = self.r.escape_diagnostics["pours"]["GND"]["stitches"]
        self.assertEqual(len(stitches), 2)
        vias = {(round(x, 4), round(y, 4)) for n, x, y in self.r.vias if n == "GND"}
        for p in stitches:
            self.assertIn(tuple(p), vias)
            self.assertTrue(self.inside("GND", p))
        self.assertEqual(self.r.escape_diagnostics["pours"]["SW"]["stitches"], [])  # no plane
        # The switch node has nothing else to join: no track at all.
        self.assertFalse([t for t in self.r.tracks if t[0] == "SW"])

    def test_no_escape_into_the_hot_rod_lands(self):
        # The pours join the hot-rod lands: no track starts or ends on one.
        lands = [r for name, net, r in pad_rects(self.g.component("U2")) if name != "FB"]
        for net, layer, a, b, _w in self.r.tracks:
            for r in lands:
                for p in (a, b):
                    within = r.left <= p[0] <= r.right and r.bottom <= p[1] <= r.top
                    self.assertFalse(within and layer == "F.Cu", (net, p))


class IdentityTest(unittest.TestCase):
    def test_without_the_section_nothing_changes(self):
        from pnr.route.detail.router import route_board

        g, c, rules = setup(pour=False)
        self.assertNotIn("plane_partition", rules)
        r = route_board(g, c, rules, max_iters=2)
        self.assertNotIn("plane_regions", r.extras())
        self.assertNotIn("pours", r.escape_diagnostics)


if __name__ == "__main__":
    unittest.main()
