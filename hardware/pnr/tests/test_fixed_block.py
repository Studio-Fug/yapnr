"""Fixed copper blocks, arcs and per-layer / per-class copper keepouts (pure Python):
the constraint inputs and the shared geometry (pnr.fixed_block)."""

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
    hold_out,
    pick_ports,
    port_candidates,
)
from pnr.graph import BoardGraph, Component, Net, Pad


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


if __name__ == "__main__":
    unittest.main()
