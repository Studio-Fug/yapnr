"""Fixed copper blocks, arcs and per-layer / per-class copper keepouts (pure Python).

The KiCad side (export, digest, validate, writeback, custom rules) is
tests/test_fixed_block_kicad.py.
"""

import math
import random
import unittest

from pnr.constraints import ConstraintError, compile_constraints, compile_routing_rules
from pnr.fixed_block import (
    ARC_EPS_MM,
    arc_center,
    arc_length,
    arc_points,
    arc_segments,
    block_refs,
    components_touching,
    flatten,
    hold_out,
    pick_ports,
    port_candidates,
)
from pnr.graph import BoardGraph, Component, Net, Pad
from pnr.route.detail.fixed import reserve_fixed_copper
from pnr.route.detail.grid import RouteGrid
from pnr.route.detail.router import block_ports


def _seg_dist(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    den = dx * dx + dy * dy
    t = 0.0 if den == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / den))
    return math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy)


def _arc_samples(start, mid, end, n=400):
    (ux, uy), r, sweep = arc_center(start, mid, end)
    a0 = math.atan2(start[1] - uy, start[0] - ux)
    return [
        (ux + r * math.cos(a0 + sweep * k / n), uy + r * math.sin(a0 + sweep * k / n))
        for k in range(n + 1)
    ]


def _random_arc(rng):
    cx, cy = rng.uniform(3, 7), rng.uniform(3, 7)
    r = rng.uniform(0.2, 2.5)
    a0 = rng.uniform(-math.pi, math.pi)
    sweep = rng.choice([-1, 1]) * rng.uniform(0.2, 1.9 * math.pi)
    pts = [
        (cx + r * math.cos(a0 + sweep * t), cy + r * math.sin(a0 + sweep * t)) for t in (0, 0.5, 1)
    ]
    return pts, r, sweep


class ArcGeometryTest(unittest.TestCase):
    def test_chord_count_of_the_design_example(self):
        # 90 degrees at R = 1.403 mm: 21 chords for a 1 um sagitta.
        self.assertEqual(arc_segments(1.403, math.pi / 2), 21)
        self.assertEqual(arc_segments(0.0005, math.pi), 1)

    def test_center_sweep_and_length(self):
        (ux, uy), r, sweep = arc_center((1, 0), (0, 1), (-1, 0))
        self.assertAlmostEqual(ux, 0)
        self.assertAlmostEqual(uy, 0)
        self.assertAlmostEqual(r, 1)
        self.assertAlmostEqual(sweep, math.pi)
        _, _, cw = arc_center((-1, 0), (0, 1), (1, 0))
        self.assertAlmostEqual(cw, -math.pi)
        self.assertAlmostEqual(arc_length((1, 0), (0, 1), (-1, 0)), math.pi)
        self.assertIsNone(arc_center((0, 0), (1, 1), (2, 2)))
        self.assertEqual(arc_points((0, 0), (1, 1), (2, 2)), [(0.0, 0.0), (2.0, 2.0)])

    def test_chords_stay_within_eps_of_random_arcs(self):
        rng = random.Random(7)
        for _ in range(60):
            (s, m, e), r, sweep = _random_arc(rng)
            pts = arc_points(s, m, e)
            self.assertEqual(pts[0], (float(s[0]), float(s[1])))
            self.assertEqual(pts[-1], (float(e[0]), float(e[1])))
            (ux, uy), radius, _ = arc_center(s, m, e)
            for p in pts:  # vertices on the arc
                self.assertAlmostEqual(math.hypot(p[0] - ux, p[1] - uy), radius, places=9)
            for q in _arc_samples(s, m, e):  # every arc point within eps of a chord
                d = min(_seg_dist(q, a, b) for a, b in zip(pts, pts[1:]))
                self.assertLessEqual(d, ARC_EPS_MM + 1e-9)


class ArcReservationTest(unittest.TestCase):
    def test_arc_reservation_is_conservative_and_tight(self):
        """Every cell a foreign track centre could not use is blocked (within
        ``w/2 + clearance + track/2`` of the arc), and nothing beyond that plus eps
        and a cell diagonal is."""
        rng = random.Random(3)
        pitch, clearance, track = 0.1, 0.1, 0.1
        for _ in range(12):
            (s, m, e), _r, _ = _random_arc(rng)
            w = rng.uniform(0.1, 0.4)
            g = RouteGrid(10, 10, pitch, clearance=clearance, track_width=track)
            arc = ["RF", "F.Cu", list(s), list(m), list(e), w]
            reserve_fixed_copper(g, dict(frame="engine-mm-y-up", tracks=[], arcs=[arc]))
            samples = _arc_samples(s, m, e, 600)
            near = w / 2 + clearance + track / 2
            far = near + ARC_EPS_MM + pitch / math.sqrt(2) + 1e-9
            blocked = g.blocked[0]
            for j in range(g.ny):
                for i in range(g.nx):
                    c = g.center_of(i, j)
                    d = min(math.dist(c, q) for q in samples[::3])
                    if d <= near - 0.01:  # sampled arc: a hair of margin
                        self.assertTrue(blocked[j, i], (c, d))
                    if d > far + 0.01:
                        self.assertFalse(blocked[j, i], (c, d))
            self.assertFalse(g.blocked[1].any())

    def test_schema_one_copper_reserves_as_before(self):
        copper = dict(
            frame="engine-mm-y-up",
            tracks=[["A", "F.Cu", [1.0, 1.0], [8.0, 3.3], 0.3]],
            vias=[dict(net="A", xy=[4, 6], diameter_mm=0.6, drill_mm=0.3, type="through")],
        )
        g = RouteGrid(10, 10, 0.25)
        reserve_fixed_copper(g, copper)
        self.assertEqual(int(g.blocked.sum()), 193)
        self.assertEqual(int(g.via_blocked.sum()), 254)
        self.assertEqual(g.fixed_owned, {})
        self.assertIs(flatten(copper), copper)


def meander_block(net="CLK", gnd_via=True):
    """A small block: a straight lead, a 180-degree arc and a return lead on F.Cu,
    two free ends; a GND via beside it."""
    block = dict(
        name="dl",
        group="DL",
        tracks=[
            [net, "F.Cu", [3.0, 5.0], [5.0, 5.0], 0.2],
            [net, "F.Cu", [5.0, 6.0], [3.5, 6.0], 0.2],
        ],
        arcs=[[net, "F.Cu", [5.0, 5.0], [5.5, 5.5], [5.0, 6.0], 0.2]],
        vias=[],
        polygons=[],
        refs=[],
    )
    if gnd_via:
        block["vias"].append(
            dict(net="GND", xy=[6.5, 5.5], diameter_mm=0.4, drill_mm=0.2, type="through")
        )
    return block


class BlockTest(unittest.TestCase):
    def test_flatten_merges_blocks_and_chords(self):
        copper = dict(frame="engine-mm-y-up", tracks=[], vias=[], blocks=[meander_block()])
        flat = flatten(copper)
        self.assertEqual(len(flat["vias"]), 1)
        self.assertGreater(len(flat["tracks"]), 3)
        self.assertTrue(all(len(t) == 5 for t in flat["tracks"]))
        chords = [t for t in flat["tracks"] if abs(t[4] - (0.2 + 2 * ARC_EPS_MM)) < 1e-12]
        length = sum(math.dist(t[2], t[3]) for t in chords)
        # Chords are short of the arc by well under 0.1 % at a 1 um sagitta.
        self.assertLess(math.pi * 0.5 - length, 1.5e-3)
        self.assertGreater(math.pi * 0.5 - length, 0)

    def test_block_copper_is_owned_by_its_nets(self):
        g = RouteGrid(10, 10, 0.1, clearance=0.1, track_width=0.1)
        copper = dict(frame="engine-mm-y-up", tracks=[], vias=[], blocks=[meander_block()])
        reserve_fixed_copper(g, copper)  # own_net False: blocks are owned anyway
        i, j = g.cell_of(5.5, 5.5)
        self.assertTrue(g.passable(0, i, j, "CLK"))
        self.assertFalse(g.passable(0, i, j, "X"))
        self.assertEqual(g.fixed_owned[(0, i, j)], "CLK")
        self.assertTrue(g.passable(1, i, j, "X"))  # F.Cu only
        vi, vj = g.cell_of(6.5, 5.5)
        for la in range(2):
            self.assertFalse(g.via_passable(la, vi, vj, "GND"))  # hole spacing: no via
            self.assertTrue(g.passable(la, vi, vj, "GND"))
            self.assertFalse(g.passable(la, vi, vj, "CLK"))
        self.assertFalse(g.blocked.any())

    def test_polygons_pads_zones_and_rule_areas(self):
        square = [[4, 4], [5, 4], [5, 5], [4, 5]]
        block = dict(
            name="b",
            group="B",
            tracks=[],
            arcs=[],
            vias=[],
            refs=["P1"],
            polygons=[
                dict(net="RF", layer="F.Cu", outline=square, holes=[], kind="pad"),
                dict(
                    net="GND",
                    layer="In2.Cu",
                    outline=[[1, 1], [2, 1], [2, 2], [1, 2]],
                    holes=[],
                    kind="zone",
                ),
                dict(
                    net="",
                    layers=["B.Cu"],
                    outline=[[7, 7], [8, 7], [8, 8], [7, 8]],
                    holes=[],
                    kind="rule_area",
                    tracks=True,
                    vias=False,
                ),
            ],
        )
        g = RouteGrid(10, 10, 0.1, clearance=0.1, track_width=0.1)
        reserve_fixed_copper(g, dict(frame="engine-mm-y-up", tracks=[], vias=[], blocks=[block]))
        i, j = g.cell_of(4.5, 4.5)
        self.assertTrue(g.passable(0, i, j, "RF"))
        self.assertFalse(g.passable(0, i, j, "X"))
        self.assertTrue(g.passable(1, i, j, "X"))
        # The In2 zone (not a routed layer here) keeps foreign vias out on every layer.
        zi, zj = g.cell_of(1.5, 1.5)
        self.assertTrue(g.passable(0, zi, zj, "X"))
        self.assertFalse(g.via_passable(0, zi, zj, "X"))
        self.assertFalse(g.via_passable(1, zi, zj, "X"))
        self.assertTrue(g.via_passable(0, zi, zj, "GND"))
        ri, rj = g.cell_of(7.5, 7.5)
        self.assertFalse(g.passable(1, ri, rj, "GND"))
        self.assertTrue(g.passable(0, ri, rj, "GND"))
        self.assertTrue(g.via_passable(0, ri, rj, "GND"))

    def test_hold_out_and_block_refs(self):
        comps = [
            Component("U1", "u", (5, 5), 0, "top", (1, 1), (1, 1), pads=[Pad("1", "RF", (0, 0))]),
            Component("ANT", "a", (5, 8), 0, "top", (1, 1), (1, 1), pads=[Pad("1", "RF", (0, 0))]),
        ]
        g = BoardGraph("t", comps, [Net("RF", 1, [("U1", "1"), ("ANT", "1")])])
        refs = block_refs([dict(refs=["ANT"])], dict(blocks=[dict(refs=["X"])]))
        self.assertEqual(refs, ["ANT", "X"])
        self.assertEqual(hold_out(g, refs), ["ANT"])
        self.assertEqual(g.refs, ["U1"])
        self.assertEqual(g.nets[0].pins, [("U1", "1")])
        self.assertEqual(hold_out(g, []), [])

    def test_ports_one_per_piece_nearest_the_pads(self):
        block = meander_block()
        ends = port_candidates(block, "CLK")
        self.assertEqual(sorted(p for p, _, _ in ends), [(3.0, 5.0), (3.5, 6.0)])
        self.assertEqual({c for _, _, c in ends}, {0})
        self.assertEqual(pick_ports(block, "CLK", [(3.5, 7.0)]), [((3.5, 6.0), "F.Cu")])
        self.assertEqual(pick_ports(block, "CLK", [(1.0, 4.0)]), [((3.0, 5.0), "F.Cu")])
        # A piece a pad already reaches gets no port.
        joined = components_touching(block, "CLK", [("F.Cu", 2.9, 4.9, 3.1, 5.1)])
        self.assertEqual(joined, {0})
        self.assertEqual(pick_ports(block, "CLK", [(1.0, 4.0)], joined), [])


def _parts(*refs):
    return list(refs)


BASE = {"schema": "v0", "board": {"outline": {"w": 20, "h": 20}, "layers": 4}}


def _compile(extra, refs=("U1", "R1")):
    doc = dict(BASE, **extra)
    return compile_constraints(doc, list(refs))


class ConstraintTest(unittest.TestCase):
    def test_absent_keys_keep_rules_bytes(self):
        c = _compile({"copper_keepout": [{"ref": "U1", "rect_mm": [0, 0, 1, 1]}]})
        rules = compile_routing_rules(c, ["A"])
        self.assertNotIn("fixed_blocks", rules)
        self.assertNotIn("plane_fallback_drops", rules)
        self.assertEqual(
            rules["copper_keepouts"], [{"name": "U1", "ref": "U1", "rect_mm": [0, 0, 1, 1]}]
        )

    def test_v1_keepout_and_blocks(self):
        c = _compile(
            {
                "fixed": {"U1": {"at": [10, 10]}},
                "net_class": {"rf": {"nets": ["RF*"]}, "gnd": {"nets": ["GND"]}},
                "fixed_block": [
                    {"name": "m", "group": "MACRO", "anchor": "U1", "solid_layers": ["In2.Cu"]}
                ],
                "copper_keepout": [
                    {
                        "name": "rf",
                        "rect": [1, 1, 5, 5],
                        "layers": ["F.Cu", "In1.Cu"],
                        "items": ["tracks", "vias"],
                        "exempt_groups": ["MACRO"],
                    },
                    {
                        "name": "guard",
                        "polygon": [[0, 0], [3, 0], [0, 3]],
                        "allow_classes": ["rf"],
                        "allow_nets": ["V*"],
                    },
                ],
                "board": {
                    "outline": {"w": 20, "h": 20},
                    "layers": 4,
                    "plane_fallback_drops": False,
                },
            }
        )
        rules = compile_routing_rules(c, ["RF1", "RF2", "GND", "VCC", "VBAT", "SIG"])
        self.assertIs(rules["plane_fallback_drops"], False)
        self.assertEqual(rules["fixed_blocks"][0]["group"], "MACRO")
        rf, guard = rules["copper_keepouts"]
        self.assertEqual(rf["polygon"], [[1, 1], [5, 1], [5, 5], [1, 5]])
        self.assertEqual(rf["layers"], ["F.Cu", "In1.Cu"])
        self.assertEqual(rf["allowed_nets"], [])
        self.assertEqual(guard["layers"], ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"])
        self.assertEqual(guard["items"], ["tracks", "vias", "pours"])
        self.assertEqual(guard["allowed_nets"], ["RF1", "RF2", "VBAT", "VCC"])

    def test_errors(self):
        bad = [
            {"copper_keepout": [{"name": "k", "polygon": [[0, 0], [1, 1]]}]},
            {"copper_keepout": [{"name": "k", "rect": [0, 0, 1, 1], "layers": ["In3.Cu"]}]},
            {"copper_keepout": [{"name": "k", "rect": [0, 0, 1, 1], "items": ["pads"]}]},
            {"copper_keepout": [{"name": "k", "rect": [0, 0, 1, 1], "allow_classes": ["nope"]}]},
            {"copper_keepout": [{"name": "k", "rect": [0, 0, 1, 1], "exempt_groups": ["G"]}]},
            {"copper_keepout": [{"rect": [0, 0, 1, 1]}]},
            {
                "copper_keepout": [
                    {"name": "k", "rect": [0, 0, 1, 1], "polygon": [[0, 0], [1, 0], [0, 1]]}
                ]
            },
            {"fixed_block": [{"name": "m"}]},
            {"fixed_block": [{"name": "m", "group": "G", "anchor": "U1"}]},  # anchor not fixed
            {"fixed_block": [{"name": "m", "group": "G", "sha256": "xyz"}]},
            {"board": {"outline": {"w": 20, "h": 20}, "plane_fallback_drops": "no"}},
        ]
        for extra in bad:
            with self.subTest(extra=extra):
                with self.assertRaises(ConstraintError):
                    _compile(extra)


class RouteBoardPortTest(unittest.TestCase):
    def test_route_board_joins_a_one_pad_net_to_its_block_port(self):
        from pnr.route.detail.router import route_board

        comp = Component(
            "R1",
            "r",
            (1.5, 5.5),
            0,
            "top",
            (1.6, 0.8),
            (1.6, 0.8),
            pads=[Pad("1", "CLK", (-0.5, 0), (0.5, 0.5)), Pad("2", "", (0.5, 0), (0.5, 0.5))],
        )
        graph = BoardGraph("t", [comp], [Net("CLK", 1, [("R1", "1")])])
        cc = compile_constraints({"board": {"outline": {"w": 10, "h": 10}, "layers": 2}}, ["R1"])
        rules = compile_routing_rules(cc, ["CLK"])
        copper = dict(
            frame="engine-mm-y-up", tracks=[], vias=[], blocks=[meander_block(gnd_via=False)]
        )
        result = route_board(graph, cc, rules, fixed_copper=copper)
        self.assertEqual(result.result.unrouted, [])
        self.assertIn("CLK", result.escape_diagnostics["block_ports"])
        ends = {tuple(map(lambda v: round(v, 6), t[2])) for t in result.tracks} | {
            tuple(map(lambda v: round(v, 6), t[3])) for t in result.tracks
        }
        self.assertIn((3.0, 5.0), ends)  # the free end nearest the pad
        # Without the block the one-pad net is not routed at all.
        bare = route_board(graph, cc, rules)
        self.assertEqual(bare.tracks, [])
        # The same copper carried in the rules routes the same.
        again = route_board(graph, cc, dict(rules, fixed_copper=copper))
        self.assertEqual(again.tracks, result.tracks)

    def test_block_ports_skip_pieces_a_pad_reaches(self):
        g = RouteGrid(10, 10, 0.1, clearance=0.1, track_width=0.1)
        comp = Component(
            "U1",
            "u",
            (3.0, 5.0),
            0,
            "top",
            (0.4, 0.4),
            (0.4, 0.4),
            pads=[Pad("1", "CLK", (0, 0), (0.3, 0.3))],
        )
        graph = BoardGraph("t", [comp], [Net("CLK", 1, [("U1", "1")])])
        copper = dict(
            frame="engine-mm-y-up", tracks=[], vias=[], blocks=[meander_block(gnd_via=False)]
        )
        reserve_fixed_copper(g, copper)
        self.assertEqual(block_ports(g, graph, copper), {})


if __name__ == "__main__":
    unittest.main()
