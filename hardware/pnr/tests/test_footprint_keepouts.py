"""Rule areas built into footprints (a microSD socket's keep-outs) bind every copper
producer, and a via's drill keeps the board's hole-to-hole minimum from footprint
drills (a connector's shield hole) under one-gap fab rules.

Footprint rule areas ride on the graph (``Component.rule_areas``, pnr.ingest) in the
footprint's own frame and reach the producers as v1 copper keep-outs at the part's
current pose (pnr.fixed_block.copper_keepouts): the detailed router's grid, the
access-tail checks of the declared fanouts, the BGA fanout planner and the plane
partition. KiCad-side producers (the native Oracle, the shove world) are covered by
tests/test_footprint_keepouts_kicad.py.
"""

import json
import math
import unittest
from unittest import mock

from pnr.constraints import compile_constraints
from pnr.fixed_block import copper_keepouts, footprint_keepouts, point_in_polygon
from pnr.graph import BoardGraph, Component, Net, Pad
from pnr.route.detail.grid import RouteGrid
from pnr.route.detail.router import _mark_copper_keepouts, route_board
from pnr.writeback import _segment_distance_sq

FAB = dict(
    track_width_mm=0.15,
    clearance_mm=0.1,
    via_diameter_mm=0.4,
    via_drill_mm=0.2,
    hole_clearance_mm=0.15,
)
# A socket's keep-out as KiCad's microSD footprint draws it: F.Cu, no tracks, vias,
# pads, pours or footprints, in the footprint frame (top side, y up).
AREA = dict(
    outline=[[-1.0, -2.6], [1.0, -2.6], [1.0, 2.6], [-1.0, 2.6]],
    layers=["F.Cu"],
    layers_bottom=["B.Cu"],
    items=["tracks", "vias", "pads", "pours", "footprints"],
)


def _tp(ref, x, y, net):
    return Component(
        ref=ref,
        footprint="test:" + ref,
        pos=(x, y),
        rot=0.0,
        side="top",
        courtyard=(0.8, 0.8),
        bbox=(0.8, 0.8),
        pads=[Pad(name="1", net=net, offset=(0.0, 0.0), size=(0.6, 0.6))],
    )


def socket(pos=(10.0, 3.0), rot=0.0, side="top", areas=(AREA,)):
    return Component(
        ref="J3",
        footprint="test:socket",
        pos=pos,
        rot=rot,
        side=side,
        courtyard=(2.4, 5.6),
        bbox=(2.4, 5.6),
        rule_areas=[dict(a) for a in areas],
    )


def keepout_board(**kw):
    """Net A from TP1 to TP2 straight across J3's keep-out, on a 20 x 6 mm board."""
    parts = [_tp("TP1", 3.0, 3.0, "A"), _tp("TP2", 17.0, 3.0, "A"), socket(**kw)]
    nets = [Net(name="A", code=1, pins=[("TP1", "1"), ("TP2", "1")])]
    return BoardGraph(name="keepout", components=parts, nets=nets)


def _constraints(refs=("TP1", "TP2", "J3"), w=20, h=6):
    return compile_constraints({"board": {"outline": {"w": w, "h": h}}}, list(refs))


def _rules(**fab):
    return {"layers": 2, "fab": dict(FAB, **fab), "net_classes": []}


def _segment_polygon(a, b, poly):
    """Least distance (mm) from segment a-b to polygon ``poly`` (0 when they meet)."""
    if point_in_polygon(a, poly) or point_in_polygon(b, poly):
        return 0.0
    best = math.inf
    for k in range(len(poly)):
        p, q = poly[k], poly[(k + 1) % len(poly)]
        best = min(best, math.sqrt(_segment_distance_sq(a, b, p, q)))
    return best


class GraphTest(unittest.TestCase):
    def test_rule_areas_round_trip_and_are_omitted_when_empty(self):
        graph = keepout_board()
        back = BoardGraph.from_json(graph.to_json())
        self.assertEqual(back.component("J3").rule_areas, [AREA])
        self.assertEqual(back.to_json(), graph.to_json())
        plain = json.loads(graph.to_json())["components"]
        self.assertNotIn("rule_areas", [c for c in plain if c["ref"] == "TP1"][0])

    def test_keepouts_follow_pose_and_side(self):
        for rot in (0.0, 90.0, 180.0, 270.0):
            for side in ("top", "bottom"):
                with self.subTest(rot=rot, side=side):
                    graph = keepout_board(pos=(10.0, 3.0), rot=rot, side=side)
                    (spec,) = footprint_keepouts(graph)
                    self.assertEqual(spec["owner"], "J3")
                    self.assertNotIn("ref", spec)  # placement never ties the part
                    self.assertEqual(spec["layers"], ["F.Cu"] if side == "top" else ["B.Cu"])
                    xs = [p[0] for p in spec["polygon"]]
                    ys = [p[1] for p in spec["polygon"]]
                    # Long side along y at 0/180, along x at 90/270; the bottom
                    # mirror keeps this symmetric box in place.
                    span = (max(xs) - min(xs), max(ys) - min(ys))
                    want = (2.0, 5.2) if rot in (0.0, 180.0) else (5.2, 2.0)
                    self.assertAlmostEqual(span[0], want[0], places=9)
                    self.assertAlmostEqual(span[1], want[1], places=9)
        graph = keepout_board()
        self.assertEqual(
            copper_keepouts(graph, {"copper_keepouts": [dict(name="x")]})[0]["name"], "x"
        )
        self.assertEqual(len(copper_keepouts(graph, None)), 1)

    def test_offset_area_mirrors_with_the_part(self):
        # An off-centre area at +y: on the bottom at rot 0 it sits at -y (KiCad Flip
        # mirrors the footprint in y, as the pads), never at +y.
        area = dict(AREA, outline=[[-0.5, 1.0], [0.5, 1.0], [0.5, 2.0], [-0.5, 2.0]])
        top = footprint_keepouts(keepout_board(areas=[area]))[0]["polygon"]
        bottom = footprint_keepouts(keepout_board(side="bottom", areas=[area]))[0]["polygon"]
        self.assertGreater(min(y for _x, y in top), 3.0)
        self.assertLess(max(y for _x, y in bottom), 3.0)


class GridTest(unittest.TestCase):
    def test_tracks_barred_on_their_layer_vias_on_every_layer(self):
        for side, barred, free in (("top", 0, 1), ("bottom", 1, 0)):
            with self.subTest(side=side):
                graph = keepout_board(side=side)
                g = RouteGrid(20, 6, 0.25, layers=("F.Cu", "B.Cu"), via_radius=0.2)
                _mark_copper_keepouts(g, graph, None)  # no rules: the parts' own areas
                i, j = g.cell_of(10.0, 3.0)
                self.assertFalse(g.passable(barred, i, j, "A"))
                self.assertTrue(g.passable(free, i, j, "A"))
                self.assertFalse(g.via_passable(0, i, j, "A"))
                self.assertFalse(g.via_passable(1, i, j, "A"))
                far = g.cell_of(15.0, 3.0)
                self.assertTrue(g.passable(barred, *far, "A"))

    def test_an_area_that_bars_only_pads_bars_no_copper(self):
        graph = keepout_board(areas=[dict(AREA, items=["pads", "footprints"])])
        g = RouteGrid(20, 6, 0.25, layers=("F.Cu", "B.Cu"), via_radius=0.2)
        _mark_copper_keepouts(g, graph, None)
        i, j = g.cell_of(10.0, 3.0)
        self.assertTrue(g.passable(0, i, j, "A"))
        self.assertTrue(g.via_passable(0, i, j, "A"))


class RouteTest(unittest.TestCase):
    def test_route_keeps_tracks_and_vias_out_of_the_area(self):
        graph = keepout_board()
        board = route_board(graph, _constraints(), _rules(), pitch=0.25)
        self.assertEqual(board.result.unrouted, [])
        poly = footprint_keepouts(graph)[0]["polygon"]
        for net, layer, a, b, w in board.tracks:
            if layer == "F.Cu":
                self.assertGreaterEqual(_segment_polygon(a, b, poly), w / 2 - 1e-6, (a, b))
        for _net, x, y in board.vias:
            self.assertGreaterEqual(_segment_polygon((x, y), (x, y), poly), 0.2 - 1e-6)
        # Without the area the route crosses it on F.Cu: the test can tell.
        plain = keepout_board(areas=())
        legacy = route_board(plain, _constraints(), _rules(), pitch=0.25)
        self.assertTrue(
            any(
                _segment_polygon(a, b, poly) < w / 2
                for _n, layer, a, b, w in legacy.tracks
                if layer == "F.Cu"
            )
        )

    def test_bottom_part_bars_the_bottom_layer(self):
        graph = keepout_board(side="bottom")
        board = route_board(graph, _constraints(), _rules(), pitch=0.25)
        self.assertEqual(board.result.unrouted, [])
        poly = footprint_keepouts(graph)[0]["polygon"]
        for _net, layer, a, b, w in board.tracks:
            if layer == "B.Cu":
                self.assertGreaterEqual(_segment_polygon(a, b, poly), w / 2 - 1e-6)


def pth_board(plated=True):
    """Net A pads TP1/TP2 and a 0.85 mm shield hole (J1.SH, GND; NPTH when not
    ``plated``) between them."""
    shield = Component(
        ref="J1",
        footprint="test:usb",
        pos=(10.0, 3.0),
        rot=0.0,
        side="top",
        courtyard=(1.6, 1.6),
        bbox=(1.6, 1.6),
        pads=[
            Pad(
                name="SH",
                net="GND" if plated else "",
                offset=(0.0, 0.0),
                size=(1.45, 1.45) if plated else (0.85, 0.85),
                through_hole=True,
                drill_size=(0.85, 0.85),
                plated=plated,
                plated_land_radius=0.725 if plated else 0.0,
            )
        ],
    )
    parts = [_tp("TP1", 3.0, 3.0, "A"), _tp("TP2", 17.0, 3.0, "A"), shield]
    nets = [Net(name="A", code=1, pins=[("TP1", "1"), ("TP2", "1")])]
    if plated:
        nets.append(Net(name="GND", code=2, pins=[("J1", "SH")]))
    return BoardGraph(name="pth", components=parts, nets=nets)


class HoleToHoleTest(unittest.TestCase):
    """KiCad judges a via's drill against a PTH or NPTH drill by the board's
    hole-to-hole minimum (0.25 mm here), whatever the nets; the grid used the hole
    clearance (0.15 mm), so a via sat 0.19 mm from a USB shield hole."""

    def test_via_sites_keep_hole_to_hole_from_pth_and_npth_drills(self):
        for plated in (True, False):
            with self.subTest(plated=plated):
                board = route_board(
                    pth_board(plated),
                    _constraints(("TP1", "TP2", "J1")),
                    _rules(hole_to_hole_mm=0.25),
                    pitch=0.25,
                )
                g = board.grid
                # drill radius 0.425 + via drill radius 0.1 + 0.25 = 0.775 from the centre
                self.assertFalse(g.hole_site_clear((10.71, 3.0)))
                self.assertFalse(g.hole_site_clear((10.0, 3.76), net="A"))
                self.assertTrue(g.hole_site_clear((10.78, 3.0)))
                for _net, x, y in board.vias:
                    gap = math.dist((x, y), (10.0, 3.0)) - 0.425 - 0.1
                    self.assertGreaterEqual(gap, 0.25 - 1e-6)

    def test_rules_without_hole_to_hole_keep_the_hole_clearance(self):
        board = route_board(pth_board(), _constraints(("TP1", "TP2", "J1")), _rules(), pitch=0.25)
        self.assertIsNone(board.grid.pth_hole_gap)
        self.assertTrue(board.grid.hole_site_clear((10.71, 3.0)))

    def test_a_larger_hole_clearance_still_binds(self):
        board = route_board(
            pth_board(),
            _constraints(("TP1", "TP2", "J1")),
            _rules(hole_clearance_mm=0.3, hole_to_hole_mm=0.25),
            pitch=0.25,
        )
        self.assertAlmostEqual(board.grid.pth_hole_gap, 0.3)


class FanoutTest(unittest.TestCase):
    def test_access_tails_and_the_bga_planner_see_footprint_areas(self):
        from pnr.fanout.geom import Pose
        from pnr.fanout.planner import _obstacles, inputs_sha256
        from pnr.route.detail.fanout import _keepout_areas

        graph = keepout_board()
        poly = footprint_keepouts(graph)[0]["polygon"]
        ((got, layers, allowed),) = _keepout_areas(graph, {})
        self.assertEqual(got, poly)
        self.assertEqual(layers, frozenset({"F.Cu"}))
        self.assertEqual(allowed, frozenset())
        comp = graph.component("TP1")
        obs = _obstacles(graph, {}, {}, comp, Pose(comp.pos, comp.rot), ["F.Cu", "B.Cu"], None)
        areas = {a[0]: a[2:] for a in obs.areas}
        self.assertEqual(areas["keepout:J3:0"], (frozenset({0}), True, frozenset()))
        # The planner's cache key moves with the area (and stays put without one).
        spec = dict(ref="TP1")
        args = (["F.Cu", "B.Cu"], set(), {"A"}, None)
        moved = keepout_board(pos=(11.0, 3.0))
        self.assertNotEqual(
            inputs_sha256(graph, {}, spec, *args), inputs_sha256(moved, {}, spec, *args)
        )
        bare, bare_moved = keepout_board(areas=()), keepout_board(pos=(11.0, 3.0), areas=())
        self.assertEqual(
            inputs_sha256(bare, {}, spec, *args), inputs_sha256(bare_moved, {}, spec, *args)
        )


class PlanePartitionTest(unittest.TestCase):
    def test_a_footprint_area_that_bars_pours_bars_the_partition(self):
        """A part's own rule area on the partitioned layer reaches the partition as a
        blocked polygon (KiCad's filler would leave it empty anyway; the partition must
        not count on copper there)."""
        import pnr.plane_partition as pp
        from pnr.constraints import compile_routing_rules

        def part(ref, at, nets, areas=()):
            pads = [
                Pad(str(k + 1), net, ((k - (len(nets) - 1) / 2), 0.0), (0.6, 0.6))
                for k, net in enumerate(nets)
            ]
            return Component(
                ref,
                "c",
                at,
                0.0,
                "top",
                (len(nets), 1.0),
                (1.0, 1.0),
                pads=pads,
                rule_areas=list(areas),
            )

        inner = dict(AREA, layers=["In2.Cu"], layers_bottom=["In1.Cu"], items=["pours"])
        comps = [
            part("J1", (2.0, 6.0), ["V1", "GND", "V2"]),
            part("C1", (8.0, 3.0), ["V1", "GND"]),
            part("C2", (16.0, 3.0), ["V1", "GND"]),
            part("C3", (8.0, 9.0), ["V2", "GND"]),
            part("C4", (16.0, 9.0), ["V2", "GND"], areas=[inner]),
        ]
        pins = {}
        for c in comps:
            for p in c.pads:
                pins.setdefault(p.net, []).append((c.ref, p.name))
        nets = [Net(n, k + 1, p) for k, (n, p) in enumerate(sorted(pins.items()))]
        from pnr.graph import BoardOutline

        g = BoardGraph("partition", comps, nets, BoardOutline(20, 12))
        g.stack = {
            "layers": [
                {"name": "F.Cu", "type": "signal", "zones": [], "copper_mm": 0.035},
                {"name": "In1.Cu", "type": "power", "zones": [], "copper_mm": 0.0175},
                {"name": "In2.Cu", "type": "power", "zones": [], "copper_mm": 0.0175},
                {"name": "B.Cu", "type": "signal", "zones": [], "copper_mm": 0.035},
            ]
        }
        doc = {
            "schema": "v0",
            "board": {"outline": {"w": 20, "h": 12}, "layers": 4},
            "fixed": {c.ref: {"at": list(c.pos), "rot": 0, "side": "top"} for c in comps},
            "net_class": {
                "gnd": {"nets": ["GND"], "plane_layer": "In1.Cu"},
                "v1": {"nets": ["V1"], "plane_layer": "In2.Cu"},
                "v2": {"nets": ["V2"], "plane_layer": "In2.Cu"},
            },
            "plane_partition": [
                {"layer": "In2.Cu", "nets": ["V*"], "min_width_mm": 1.0, "currents": {"V1": 2}}
            ],
        }
        c = compile_constraints(doc, g.refs)
        rules = compile_routing_rules(c, [n.name for n in g.nets])
        seen = []
        real = pp.partition

        def spy(*args, **kwargs):
            seen.append(kwargs.get("blocked_polygons"))
            return real(*args, **kwargs)

        with mock.patch.object(pp, "partition", side_effect=spy):
            route_board(g, c, rules, max_iters=2)
        self.assertTrue(seen)
        want = footprint_keepouts(g)[0]["polygon"]
        self.assertTrue(
            any(rings == [want] for polygons in seen for rings, _allowed in polygons or ()),
            seen,
        )


if __name__ == "__main__":
    unittest.main()
