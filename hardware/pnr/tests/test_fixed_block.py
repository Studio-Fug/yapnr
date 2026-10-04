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
from pnr.route.detail.grid import Cell, RouteGrid
from pnr.route.detail.router import _mark_copper_keepouts, block_ports


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


class RouterKeepoutTest(unittest.TestCase):
    def grid(self):
        g = RouteGrid(10, 10, 0.2, layers=("F.Cu", "B.Cu"), clearance=0.1, track_width=0.2)
        g.net_widths = {}
        return g

    def test_per_layer_static_keepout(self):
        g = self.grid()
        spec = dict(
            name="k",
            polygon=[[4, 4], [6, 4], [6, 6], [4, 6]],
            layers=["F.Cu"],
            items=["tracks"],
            allowed_nets=[],
        )
        _mark_copper_keepouts(g, BoardGraph("t"), {"copper_keepouts": [spec]})
        i, j = g.cell_of(5, 5)
        self.assertFalse(g.passable(0, i, j, "A"))
        self.assertTrue(g.passable(1, i, j, "A"))
        self.assertTrue(g.via_passable(0, i, j, "A"))  # vias not barred
        self.assertTrue(g.passable(0, *g.cell_of(7, 7), "A"))

    def test_plane_layer_keepout_bars_vias_only(self):
        g = self.grid()
        spec = dict(
            name="k",
            polygon=[[4, 4], [6, 4], [6, 6], [4, 6]],
            layers=["In1.Cu"],
            items=["tracks", "vias"],
            allowed_nets=[],
        )
        _mark_copper_keepouts(g, BoardGraph("t"), {"copper_keepouts": [spec]})
        i, j = g.cell_of(5, 5)
        self.assertTrue(g.passable(0, i, j, "A") and g.passable(1, i, j, "A"))
        self.assertFalse(g.via_passable(0, i, j, "A") or g.via_passable(1, i, j, "A"))

    def test_class_keepout_bars_only_other_nets(self):
        g = self.grid()
        spec = dict(
            name="guard",
            polygon=[[4, 4], [6, 4], [6, 6], [4, 6]],
            layers=["F.Cu", "B.Cu"],
            items=["tracks", "vias"],
            allow_classes=["pwr"],
            allow_nets=[],
            allowed_nets=["VCC"],
        )
        _mark_copper_keepouts(g, BoardGraph("t"), {"copper_keepouts": [spec]})
        self.assertFalse(g.blocked.any() or g.via_blocked.any())
        i, j = g.cell_of(5, 5)
        for la in range(2):
            self.assertTrue(g.passable(la, i, j, "VCC"))
            self.assertTrue(g.via_passable(la, i, j, "VCC"))
            self.assertFalse(g.passable(la, i, j, "SIG"))
            self.assertFalse(g.via_passable(la, i, j, "SIG"))
        self.assertTrue(g.net_blocked("SIG", 0, i, j))
        self.assertFalse(g.net_blocked("VCC", 0, i, j))

    def test_dense_fields_match_the_predicates(self):
        from pnr.route.detail.dense_maze import GridStatic, unmodelled

        g = self.grid()
        spec = dict(
            name="guard",
            polygon=[[3, 3], [7, 3], [7, 7], [3, 7]],
            layers=["F.Cu"],
            items=["tracks", "vias"],
            allow_classes=["pwr"],
            allow_nets=[],
            allowed_nets=["VCC"],
        )
        _mark_copper_keepouts(g, BoardGraph("t"), {"copper_keepouts": [spec]})
        reserve_fixed_copper(
            g, dict(frame="engine-mm-y-up", tracks=[], vias=[], blocks=[meander_block()])
        )
        self.assertIsNone(unmodelled(g))
        static = GridStatic(g)
        for net in ("VCC", "SIG", "CLK", "GND"):
            passable, via, _ = static.net(net)
            for la in range(g.nlayers):
                for j in range(g.ny):
                    for i in range(g.nx):
                        self.assertEqual(
                            bool(passable[la, j, i]), g.passable(la, i, j, net), (net, la, i, j)
                        )
                        self.assertEqual(
                            bool(via[la, j, i]), g.via_passable(la, i, j, net), (net, la, i, j)
                        )

    def test_kernels_route_identically_around_a_class_keepout(self):
        import os

        from pnr.route.detail.maze import route

        results = {}
        for kernel in ("reference", "packed"):
            g = self.grid()
            spec = dict(
                name="guard",
                polygon=[[4, 1], [6, 1], [6, 9], [4, 9]],
                layers=["F.Cu", "B.Cu"],
                items=["tracks", "vias"],
                allow_classes=["pwr"],
                allow_nets=[],
                allowed_nets=["VCC"],
            )
            _mark_copper_keepouts(g, BoardGraph("t"), {"copper_keepouts": [spec]})
            access = {
                "VCC": [Cell(0, *g.cell_of(1, 5)), Cell(0, *g.cell_of(9, 5))],
                "SIG": [Cell(0, *g.cell_of(1, 3)), Cell(0, *g.cell_of(9, 3))],
            }
            old = os.environ.get("PNR_MAZE_KERNEL")
            os.environ["PNR_MAZE_KERNEL"] = kernel
            try:
                r = route(g, access, max_iters=3, rrr_rounds=1)
            finally:
                if old is None:
                    os.environ.pop("PNR_MAZE_KERNEL", None)
                else:
                    os.environ["PNR_MAZE_KERNEL"] = old
            results[kernel] = {n: (rn.routed, list(rn.cells)) for n, rn in r.nets.items()}
            self.assertTrue(r.nets["VCC"].routed)
            self.assertTrue(r.nets["SIG"].routed)
            self.assertFalse(
                any(g.net_blocked("SIG", c.layer, c.i, c.j) for c in r.nets["SIG"].cells)
            )
            self.assertTrue(
                any(g.net_blocked("SIG", c.layer, c.i, c.j) for c in r.nets["VCC"].cells)
            )
        self.assertEqual(results["reference"], results["packed"])


class KeepoutRulesFileTest(unittest.TestCase):
    """The marked custom-rules block: written beside the profile's rules or a
    hand-written file's, replaced alone, carried through regenerations."""

    GUARD = dict(
        name="guard",
        polygon=[[0, 0], [1, 0], [1, 1]],
        layers=["F.Cu", "In1.Cu"],
        items=["tracks", "vias"],
        allow_classes=["pwr"],
        allow_nets=["V*"],
        allowed_nets=["VBAT", "VCC"],
        exempt_groups=["BLK"],
    )

    def test_keepout_dru_text(self):
        from pnr.fab_profile import KEEPOUT_DRU_BEGIN, KEEPOUT_DRU_END
        from pnr.writeback import keepout_dru

        plain = dict(
            self.GUARD,
            name="plain",
            allow_classes=[],
            allow_nets=[],
            allowed_nets=[],
            exempt_groups=[],
        )
        v0 = dict(name="U1", ref="U1", rect_mm=[0, 0, 1, 1])
        self.assertIsNone(keepout_dru({"copper_keepouts": [plain, v0]}))
        rules = {
            "copper_keepouts": [self.GUARD, plain],
            "net_classes": [dict(name="pwr", nets=["VCC"])],
        }
        text = keepout_dru(rules)
        self.assertTrue(text.startswith(KEEPOUT_DRU_BEGIN))
        self.assertTrue(text.rstrip().endswith(KEEPOUT_DRU_END))
        self.assertEqual(text.count("(rule "), 2)  # one per layer of the guard only
        self.assertIn('(layer "In1.Cu")', text)
        self.assertIn(
            "A.intersectsArea('PNR keepout:guard') && !A.hasNetclass('pwr') && "
            "A.NetName != 'VBAT' && !A.memberOfGroup('BLK')",
            text,
        )
        self.assertIn("(constraint disallow track via))", text)

    def test_append_replace_and_regenerate(self):
        import tempfile
        from pathlib import Path

        from pnr.fab_profile import (
            DRU_HEADER,
            KEEPOUT_ONLY_HEADER,
            append_board_rules,
            board_rules_block,
            write_dru,
        )
        from pnr.writeback import keepout_dru

        block = keepout_dru({"copper_keepouts": [self.GUARD]})
        other = block.replace("guard", "guard2")
        with tempfile.TemporaryDirectory() as tmp:
            board = Path(tmp) / "b.kicad_pcb"
            dru = board.with_suffix(".kicad_dru")
            # No file: one with the block alone, managed; legacy keeps it.
            append_board_rules(board, block)
            self.assertTrue(dru.read_text().startswith(KEEPOUT_ONLY_HEADER))
            write_dru(board, name="legacy")
            self.assertEqual(board_rules_block(dru.read_text()), block)
            # A profile regenerates its rules and keeps the block.
            write_dru(board, name="jlc-pofv")
            text = dru.read_text()
            self.assertIn(DRU_HEADER, text)
            self.assertEqual(board_rules_block(text), block)
            # Replacing changes the block only.
            append_board_rules(board, other)
            after = dru.read_text()
            self.assertEqual(board_rules_block(after), other)
            self.assertEqual(after.replace(other, ""), text.replace(block, ""))
            # A hand-written file keeps its own rules.
            dru.write_text("(version 1)\n(rule own (constraint clearance (min 0.2mm)))\n")
            append_board_rules(board, block)
            self.assertTrue(dru.read_text().startswith("(version 1)\n(rule own"))
            self.assertEqual(board_rules_block(dru.read_text()), block)
            write_dru(board, name="jlc-pofv")  # hand-written: untouched
            self.assertEqual(board_rules_block(dru.read_text()), block)
            append_board_rules(board, None)
            self.assertIsNone(board_rules_block(dru.read_text()))
            self.assertIn("(rule own", dru.read_text())
            # Legacy without a block: a generated file goes, as before.
            dru.unlink()
            write_dru(board, name="jlc-pofv")
            write_dru(board, name="legacy")
            self.assertFalse(dru.exists())


class CapacityProxyTest(unittest.TestCase):
    """The placement routability proxy sees v1 keepouts on their own layers and the
    fixed copper the rules carry."""

    def fixture(self):
        from pnr.graph import BoardOutline

        comps = [
            Component(
                ref,
                "",
                (x, 2),
                0,
                "top",
                (0.2, 0.2),
                (0.2, 0.2),
                pads=[Pad("1", "n0", (0, 0), (0.2, 0.2))],
                smd_body=True,
            )
            for ref, x in (("A0", 1), ("B0", 9))
        ]
        return BoardGraph("fixture", comps, [], BoardOutline(10, 4))

    def unreachable(self, rules):
        from pnr.place.capacity_proxy import score

        return score(self.fixture(), dict({"layers": 2}, **rules), passes=1)["unreachable_branches"]

    def keepout(self, **kw):
        spec = dict(
            name="wall",
            polygon=[[4, -1], [6, -1], [6, 5], [4, 5]],
            layers=["F.Cu", "B.Cu"],
            items=["tracks", "vias"],
            allowed_nets=[],
        )
        spec.update(kw)
        return {"copper_keepouts": [spec]}

    def test_keepouts_by_layer_and_class(self):
        self.assertEqual(self.unreachable({}), 0)
        self.assertEqual(self.unreachable(self.keepout()), 1)
        self.assertEqual(self.unreachable(self.keepout(layers=["F.Cu"])), 0)
        self.assertEqual(self.unreachable(self.keepout(items=["vias"])), 0)
        # A keepout with allow lists is not modelled (optimistic).
        self.assertEqual(self.unreachable(self.keepout(allow_nets=["V*"], allowed_nets=["VCC"])), 0)

    def test_fixed_copper_in_the_rules_is_an_obstacle(self):
        # A 2.5 mm wide wall across the board on both layers (the proxy models capacity
        # per 2 mm cell, so a wall must fill whole cells to cut the board).
        wall = [["X", la, [5.0, -0.5], [5.0, 4.5], 2.5] for la in ("F.Cu", "B.Cu")]
        copper = dict(
            frame="engine-mm-y-up",
            tracks=[],
            vias=[],
            blocks=[dict(meander_block(), tracks=wall, arcs=[], vias=[])],
        )
        self.assertEqual(self.unreachable({"fixed_copper": copper}), 1)
        # Only on F.Cu: the board stays routable on B.Cu.
        copper["blocks"][0]["tracks"] = wall[:1]
        self.assertEqual(self.unreachable({"fixed_copper": copper}), 0)


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

    def test_a_rail_without_free_ends_joins_at_a_via(self):
        """Block ground copper with no free end (a rail between two vias) is still a
        connection target: the router joins the net's pads to one of its vias."""
        from pnr.route.detail.router import route_board

        rail = dict(
            name="g",
            group="G",
            tracks=[["GND", "F.Cu", [6.0, 5.0], [8.0, 5.0], 0.3]],
            arcs=[],
            vias=[
                dict(net="GND", xy=[6.0, 5.0], diameter_mm=0.6, drill_mm=0.3, type="through"),
                dict(net="GND", xy=[8.0, 5.0], diameter_mm=0.6, drill_mm=0.3, type="through"),
            ],
            polygons=[],
            refs=[],
        )
        self.assertEqual(pick_ports(rail, "GND", [(1.0, 5.0)]), [((6.0, 5.0), "F.Cu")])
        comp = Component(
            "R1",
            "r",
            (1.5, 5.0),
            0,
            "top",
            (1.6, 0.8),
            (1.6, 0.8),
            pads=[Pad("1", "GND", (-0.5, 0), (0.5, 0.5)), Pad("2", "", (0.5, 0), (0.5, 0.5))],
        )
        graph = BoardGraph("t", [comp], [Net("GND", 1, [("R1", "1")])])
        cc = compile_constraints({"board": {"outline": {"w": 10, "h": 10}, "layers": 2}}, ["R1"])
        rules = compile_routing_rules(cc, ["GND"])
        copper = dict(frame="engine-mm-y-up", tracks=[], vias=[], blocks=[rail])
        result = route_board(graph, cc, rules, fixed_copper=copper)
        self.assertEqual(result.result.unrouted, [])
        self.assertEqual(result.escape_diagnostics["block_ports"], {"GND": [["F.Cu", 6.0, 5.0]]})

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
