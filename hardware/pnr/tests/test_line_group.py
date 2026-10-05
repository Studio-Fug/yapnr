"""Line groups (pnr.place.line_group): the parser, the group layout, rigid placement, the
legality checks and the macro collapse defaults it builds on."""

import copy
import math
import os
import unittest
from unittest import mock

from pnr.constraints import ConstraintError, Enforcement, compile_constraints, compile_routing_rules
from pnr.graph import BoardGraph, BoardOutline, Component, Net, Pad
from pnr.hier.macro import collapse
from pnr.place import line_group
from pnr.place.metrics import hard_violations, translation_checker
from pnr.place.placer import place

FAB = dict(
    track_width_mm=0.25,
    clearance_mm=0.2,
    via_diameter_mm=0.6,
    via_drill_mm=0.3,
    hole_clearance_mm=0.25,
    edge_clearance_mm=0.3,
    min_through_drill_mm=0.3,
    via_annular_mm=0.15,
)
LINE = dict(name="leds", members=["D1", "D2", "D3", "D4"], pitch_mm=3.0, rot=90)


def spec(**extra):
    doc = dict(
        schema="v0",
        board=dict(outline=dict(w=32, h=24), layers=2, default_clearance_mm=0.4),
        fab=FAB,
        fixed={"J1": dict(at=[3, 12], rot=0, side="top")},
        net_class={"supply": dict(nets=["VCC"], width_mm=0.4)},
    )
    doc.update(extra)
    return doc


def smd(ref, x, y, a, b, size=(3.49, 2.04)):
    pads = [Pad("1", a, (-0.95, 0.0), (1.0, 1.45)), Pad("2", b, (0.95, 0.0), (1.0, 1.45))]
    return Component(ref, "0805", (x, y), 0.0, "top", size, size, pads=pads, smd_body=True)


def board(rows=True):
    """A 12-part board: J1, a counter-like U1, four LED/resistor pairs, two capacitors."""
    j1 = Component(
        "J1",
        "PinHeader_1x02",
        (3.0, 12.0),
        0.0,
        "top",
        (3.0, 5.6),
        (3.0, 5.6),
        pads=[
            Pad("1", "VCC", (0.0, 1.27), (1.7, 1.7), True, (1.0, 1.0), True),
            Pad("2", "GND", (0.0, -1.27), (1.7, 1.7), True, (1.0, 1.0), True),
        ],
    )
    u1 = Component(
        "U1",
        "SOIC-8",
        (40.0, 12.0),
        0.0,
        "top",
        (7.5, 5.7),
        (7.5, 5.7),
        pads=[
            Pad(str(i + 1), net, (-2.475 if i < 4 else 2.475, 1.905 - 1.27 * (i % 4)), (1.95, 0.6))
            for i, net in enumerate(["GND", "Q0", "Q1", "Q2", "Q3", "CLK", "CLK", "VCC"])
        ],
        smd_body=True,
    )
    parts = [j1, u1]
    for i in range(4):
        parts.append(smd("R%d" % (i + 1), 40.0 + 4 * i, 20.0, "Q%d" % i, "L%d" % i, (3.45, 1.99)))
        parts.append(smd("D%d" % (i + 1), 40.0 + 4 * i, 4.0, "GND", "L%d" % i))
    parts.append(smd("C1", 60.0, 12.0, "VCC", "GND"))
    parts.append(smd("C2", 64.0, 12.0, "CLK", "GND"))
    nets = {}
    for comp in parts:
        for pad in comp.pads:
            nets.setdefault(pad.net, []).append((comp.ref, pad.name))
    return BoardGraph(
        "line-group",
        sorted(parts, key=lambda c: c.ref),
        [Net(n, i + 1, pins) for i, (n, pins) in enumerate(sorted(nets.items()))],
        BoardOutline(32.0, 24.0),
    )


def compiled(graph, **extra):
    return compile_constraints(spec(**extra), graph.refs)


class ParserTest(unittest.TestCase):
    def setUp(self):
        self.refs = board().refs

    def compile(self, *entries, **extra):
        return compile_constraints(dict(spec(**extra), line_group=list(entries)), self.refs)

    def test_hard_constraint_with_parameters(self):
        cc = self.compile(dict(LINE, reason="read as a row", edge="south"))
        (con,) = [c for c in cc.constraints if c.kind == "line_group"]
        self.assertIs(con.enforcement, Enforcement.HARD)
        self.assertEqual(con.refs, ("D1", "D2", "D3", "D4"))
        self.assertEqual(con.name, "leds")
        self.assertEqual(
            con.params,
            dict(pitch_mm=3.0, gap_mm=None, rot=90.0, edge="south", reason="read as a row"),
        )
        self.assertFalse(any("line_group" in w for w in cc.warnings))

    def test_gap_defaults_to_the_placement_clearance(self):
        cc = self.compile(dict(name="g", members=["D1", "D2"]))
        con = cc.constraints[-1]
        self.assertEqual((con.params["gap_mm"], con.params["pitch_mm"]), (0.4, None))
        self.assertEqual((con.params["rot"], con.params["edge"]), (0.0, "none"))
        self.assertEqual(
            self.compile(dict(name="g", members=["D1", "D2"], rot=-90))
            .constraints[-1]
            .params["rot"],
            270.0,
        )

    def test_rejected(self):
        bad = [
            dict(LINE, name=""),
            dict(LINE, name=None),
            dict(LINE, members=["D1"]),
            dict(LINE, members=["D1", "D1"]),
            dict(LINE, members=["D1", "D9"]),
            dict(LINE, members=["D1", "D*"]),
            dict(LINE, members="D1"),
            dict(LINE, gap_mm=1.0),
            dict(LINE, pitch_mm=0),
            dict(LINE, pitch_mm=float("nan")),
            dict(LINE, pitch_mm=True),
            dict(name="g", members=["D1", "D2"], gap_mm=0.1),
            dict(LINE, rot=45),
            dict(LINE, rot=float("inf")),
            dict(LINE, edge="up"),
            dict(LINE, reason=3),
            "D1",
        ]
        for entry in bad:
            with self.subTest(entry=entry), self.assertRaises(ConstraintError):
                self.compile(entry)
        with self.assertRaises(ConstraintError):
            self.compile(LINE, dict(LINE, members=["R1", "R2"]))  # duplicate name
        with self.assertRaises(ConstraintError):
            self.compile(LINE, dict(LINE, name="other", members=["D4", "R1"]))  # shared member
        with self.assertRaises(ConstraintError):
            compile_constraints(dict(spec(), line_group={"name": "x"}), self.refs)

    def test_members_may_not_carry_relations_a_rigid_line_cannot_honour(self):
        conflicts = [
            dict(fixed={"J1": dict(at=[3, 12]), "D2": dict(at=[10, 10])}),
            dict(row=[dict(members=["D2", "R1"], edge="any")]),
            dict(edge_align={"D3": dict(edge="south")}),
            dict(orientation={"D1": 90}),
            dict(side={"top": ["D4"]}),
            dict(keepout=[dict(name="k", ref="D1", extent=dict(edge="north", depth_mm=2))]),
            dict(group=[dict(members=["D2"], anchor="U1", radius_mm=5, hard=True)]),
            dict(group=[dict(members=["R1"], anchor="D2", radius_mm=5, hard=True)]),
        ]
        for extra in conflicts:
            with self.subTest(extra=extra), self.assertRaises(ConstraintError):
                self.compile(LINE, **extra)
        # A soft group and a polygon keepout are fine.
        self.compile(
            LINE,
            group=[dict(members=["D2"], anchor="U1", radius_mm=5)],
            keepout=[dict(name="k", polygon=[[0, 0], [1, 0], [1, 1]])],
        )


class LayoutTest(unittest.TestCase):
    def con(self, **params):
        graph = board()
        cc = compiled(graph, line_group=[dict(LINE, **params)])
        return graph, cc.constraints[-1]

    def test_pitch_and_rotation(self):
        graph, con = self.con()
        width, height, poses = line_group.layout(graph, con, 0.4)
        # rot 90: the 3.49 x 2.04 courtyards are 2.04 mm along the line.
        self.assertAlmostEqual(width, 3 * 3.0 + 2.04)
        self.assertAlmostEqual(height, 3.49)
        self.assertEqual([p[2] for p in poses.values()], [90.0] * 4)
        xs = [poses["D%d" % i][0] for i in range(1, 5)]
        self.assertEqual([round(b - a, 9) for a, b in zip(xs, xs[1:])], [3.0] * 3)
        self.assertAlmostEqual(xs[0], 1.02)
        self.assertTrue(all(abs(p[1] - height / 2) < 1e-12 for p in poses.values()))

    def test_gap_and_rot_zero(self):
        graph, con = self.con(pitch_mm=None, gap_mm=1.0, rot=0)
        width, height, poses = line_group.layout(graph, con, 0.4)
        self.assertAlmostEqual(width, 4 * 3.49 + 3 * 1.0)
        self.assertAlmostEqual(height, 2.04)
        self.assertAlmostEqual(poses["D2"][0] - poses["D1"][0], 3.49 + 1.0)

    def test_too_small_pitch_names_the_pair(self):
        graph, con = self.con(pitch_mm=2.2)
        with self.assertRaisesRegex(ValueError, "D1 and D2"):
            line_group.layout(graph, con, 0.4)


def placed_line(test, graph, cc, **kwargs):
    placed, report = place(graph, cc, iters=150, **kwargs)
    test.assertTrue(report.legal, report.summary())
    test.assertEqual(line_group.violations(placed, cc), [])
    parts = [placed.component("D%d" % i) for i in range(1, 5)]
    turn = (parts[0].rot - 90) % 360
    test.assertIn(turn, (0.0, 90.0, 180.0, 270.0))
    test.assertTrue(all(abs((c.rot - parts[0].rot + 180) % 360 - 180) < 1e-9 for c in parts))
    # Collinear at the declared pitch, in member order.
    for a, b in zip(parts, parts[1:]):
        test.assertAlmostEqual(math.dist(a.pos, b.pos), 3.0, places=5)
    d = (parts[1].pos[0] - parts[0].pos[0], parts[1].pos[1] - parts[0].pos[1])
    for a, b in zip(parts, parts[1:]):
        test.assertAlmostEqual(b.pos[0] - a.pos[0], d[0], places=5)
        test.assertAlmostEqual(b.pos[1] - a.pos[1], d[1], places=5)
    return placed, report


class PlaceTest(unittest.TestCase):
    def test_rigid_line_for_several_seeds(self):
        graph = board()
        cc = compiled(graph, line_group=[LINE])
        poses = set()
        for seed in range(4):
            with self.subTest(seed=seed):
                placed, _ = placed_line(self, graph, cc, seed=seed)
                poses.add(tuple(round(v, 3) for v in placed.component("D1").pos))
        self.assertGreater(len(poses), 1)

    def test_scattered_members_start_converges_to_a_line(self):
        graph = board()
        cc = compiled(graph, line_group=[LINE])
        scattered = {"D1": [4, 4], "D2": [28, 20], "D3": [16, 4], "D4": [6, 20]}
        placed_line(
            self,
            graph,
            cc,
            seed=1,
            initial_positions=scattered,
            initial_rotations={"D1": 180, "D2": 0, "D3": 90, "D4": 270},
        )

    def test_a_board_without_line_groups_places_as_before(self):
        graph = board()
        cc = compiled(graph)
        with mock.patch.object(line_group, "collapse", side_effect=AssertionError):
            place(graph, cc, iters=60)

    def test_power_first_and_fixed_members_raise(self):
        graph = board()
        cc = compiled(graph, line_group=[LINE])
        with mock.patch.dict(os.environ, {"PNR_POWER_FIRST": "1"}):
            with self.assertRaisesRegex(ValueError, "PNR_POWER_FIRST"):
                place(graph, cc, iters=20)
        with self.assertRaises(ConstraintError):
            compiled(graph, line_group=[LINE], fixed={"D1": dict(at=[5, 5])})
        rules = dict(plane_access_intents=[dict(ref="D2")])
        with self.assertRaisesRegex(ValueError, "plane-access"):
            line_group.collapse(graph, cc, rules)

    def test_an_smd_line_may_sit_above_a_bottom_side_part(self):
        """A line reserves its members' sides only, not both sides like a block macro."""
        from pnr.place.geometry import courtyard_rect, occupied_sides

        def with_cell(size):
            graph = board()
            cell = Component(
                "BT1",
                "coin",
                (18.5, 12.0),
                0.0,
                "bottom",
                size,
                size,
                pads=[Pad("1", "VCC", (-5.0, 0.0), (2.0, 2.0)), Pad("2", "GND", (5.0, 0.0))],
                smd_body=True,
            )
            graph.components.append(cell)
            for net in graph.nets:
                if net.name in ("VCC", "GND"):
                    net.pins.append(("BT1", "1" if net.name == "VCC" else "2"))
            fixed = {
                "J1": dict(at=[3, 12], rot=0, side="top"),
                "BT1": dict(at=[18.5, 12], rot=0, side="bottom"),
            }
            return graph, compiled(graph, line_group=[LINE], fixed=fixed)

        graph, cc = with_cell((22.0, 16.0))
        macro = line_group.collapse(graph, cc, {})[0].component("LG00")
        self.assertEqual(macro.footprint, "line:leds")
        self.assertEqual(occupied_sides(macro), ("top",))
        over = 0
        for seed in range(2):
            placed, _ = placed_line(self, graph, cc, seed=seed)
            cell = courtyard_rect(placed.component("BT1"))
            over += any(
                courtyard_rect(placed.component("D%d" % i)).overlaps(cell) for i in range(1, 5)
            )
        self.assertGreater(over, 0)
        # A 26 x 20 mm cell leaves no slot off it: the line still places, above the cell.
        graph, cc = with_cell((26.0, 20.0))
        placed_line(self, graph, cc, seed=0)
        # A drilled member reserves both sides again.
        graph, cc = with_cell((22.0, 16.0))
        d3 = graph.component("D3")
        d3.smd_body = False
        for pad in d3.pads:
            pad.through_hole, pad.drill_size = True, (0.8, 0.8)
        macro = line_group.collapse(graph, cc, {})[0].component("LG00")
        self.assertEqual(occupied_sides(macro), ("top", "bottom"))

    def test_a_locked_member_raises(self):
        """A source lock (preserve_source_locks) on a member would pin the whole line."""
        from pnr.place.initial_pool import preserve_source_locks

        graph = board()
        d2 = graph.component("D2")
        d2.pos, d2.rot, d2.locked = (16.0, 12.0), 90.0, True
        cc = preserve_source_locks(graph, compiled(graph, line_group=[LINE]))
        for con in (cc, compiled(graph, line_group=[LINE])):
            with self.assertRaisesRegex(ValueError, "member D2 also has a fixed constraint"):
                place(graph, con, iters=20)

    def test_violations_and_translation_checker(self):
        graph = board()
        cc = compiled(graph, line_group=[LINE])
        placed, _ = placed_line(self, graph, cc, seed=0)
        legal = translation_checker(placed, cc)
        d2 = placed.component("D2")
        old = d2.pos
        d2.pos = (old[0] + 0.25, old[1])
        self.assertEqual(line_group.violations(placed, cc), ["D1", "D2", "D3", "D4"])
        self.assertIn("D2", hard_violations(placed, cc)["group_outside"])
        self.assertFalse(legal(d2))
        d2.pos = old
        # Moving no member (a free part to an empty spot, or nowhere) stays legal.
        c1 = placed.component("C1")
        self.assertTrue(legal(c1))
        d3 = placed.component("D3")
        d3.rot = (d3.rot + 90) % 360
        self.assertEqual(line_group.violations(placed, cc), ["D1", "D2", "D3", "D4"])


class RouteAndPlaceTest(unittest.TestCase):
    def test_initial_pool_result_keeps_the_group(self):
        from pnr.route.feedback import route_and_place

        graph = board()
        cc = compiled(graph, line_group=[LINE])
        rules = compile_routing_rules(cc, [n.name for n in graph.nets])
        placed, report = route_and_place(
            graph,
            cc,
            seed=0,
            iters=120,
            max_rounds=1,
            detail_rules=rules,
            detail_pitch_mm=0.25,
            detail_iters=3,
            spread=1.3,
            initial_pool=dict(starts=2, route_finalists=1, proxy_budget=2),
        )
        self.assertEqual(line_group.violations(placed, cc), [])
        self.assertFalse(any(hard_violations(placed, cc).values()))


class SatelliteTest(unittest.TestCase):
    """PNR_LINE_SATELLITES: each LED's series resistor rides flush beside it in the rigid line."""

    def flag(self, value):
        saved = os.environ.get("PNR_LINE_SATELLITES")
        if value is None:
            os.environ.pop("PNR_LINE_SATELLITES", None)
        else:
            os.environ["PNR_LINE_SATELLITES"] = value
        self.addCleanup(
            lambda: (
                os.environ.pop("PNR_LINE_SATELLITES", None)
                if saved is None
                else os.environ.__setitem__("PNR_LINE_SATELLITES", saved)
            )
        )

    def test_found_for_the_series_resistors_only(self):
        graph = board()
        cc = compiled(graph, line_group=[LINE])
        self.assertEqual(
            line_group.satellites(graph, cc),
            {"D%d" % i: [("R%d" % i, "2", "2")] for i in range(1, 5)},
        )
        # A part some constraint names, or one on a diff pair, is nobody's satellite.
        held = compiled(graph, line_group=[LINE], orientation={"R1": 0})
        self.assertNotIn("D1", line_group.satellites(graph, held))
        pair = compiled(graph, line_group=[LINE], diff_pair=[dict(name="p", p="Q1", n="L1")])
        self.assertNotIn("D2", line_group.satellites(graph, pair))
        self.assertEqual(line_group.satellites(graph, compiled(graph)), {})

    def test_layout_puts_each_resistor_flush_beside_its_led(self):
        graph = board()
        cc = compiled(graph, line_group=[LINE])
        con = line_group.groups(cc)[0]
        width, height, poses = line_group.layout(graph, con, 0.4)
        found = line_group.satellites(graph, cc)
        w2, h2, sat = line_group.satellite_layout(graph, con, width, height, poses, found, 0.4)
        self.assertAlmostEqual(w2, width)
        # LED across extent 3.49, the resistor's 3.45, 0.4 apart.
        self.assertAlmostEqual(h2, 3.49 + 0.4 + 3.45)
        for i in range(1, 5):
            dx, dy, drot = sat["D%d" % i]
            rx, ry, rrot = sat["R%d" % i]
            self.assertAlmostEqual(rx, dx)
            self.assertAlmostEqual(abs(ry - dy), 3.49 / 2 + 0.4 + 3.45 / 2)
            self.assertIn(rrot % 180, (90.0,))

    def test_placed_flush_and_in_line(self):
        from pnr.place.geometry import pin_positions

        graph = board()
        cc = compiled(graph, line_group=[LINE])
        self.flag("1")
        for seed in range(2):
            with self.subTest(seed=seed):
                placed, _ = placed_line(self, graph, cc, seed=seed)
                for i in range(1, 5):
                    d = placed.component("D%d" % i)
                    r = placed.component("R%d" % i)
                    (_, d1), (_, d2) = pin_positions(d)
                    (_, r1), (_, r2) = pin_positions(r)
                    ax = (d2[0] - d1[0], d2[1] - d1[1])
                    norm = math.hypot(*ax)
                    for q in (r1, r2):  # the resistor's pads on the LED's pad axis
                        cross = (q[0] - d1[0]) * ax[1] - (q[1] - d1[1]) * ax[0]
                        self.assertLess(abs(cross) / norm, 1e-6)
                    # The shared pad (L) faces the LED's L pad.
                    self.assertLess(math.dist(r2, d2), math.dist(r1, d2))
                    self.assertLess(math.dist(r2, d2), 2.5)

    def test_off_never_looks_for_satellites(self):
        graph = board()
        cc = compiled(graph, line_group=[LINE])
        self.flag(None)
        with mock.patch.object(line_group, "satellites", side_effect=AssertionError("called")):
            placed_line(self, graph, cc, seed=0)


class MacroDefaultsTest(unittest.TestCase):
    def test_collapse_defaults_are_unchanged(self):
        """Default prefix and margin: MB macros with the fab edge clearance margin."""
        from types import SimpleNamespace

        graph = board()
        cc = compiled(graph)
        rules = compile_routing_rules(cc, [n.name for n in graph.nets])
        layouts = []
        for name, refs in (("a", ["R1", "D1"]), ("b", ["R2", "D2"])):
            sub = BoardGraph(name)
            for i, ref in enumerate(refs):
                comp = copy.deepcopy(graph.component(ref))
                comp.pos = (2.0 + 4 * i, 2.0)
                sub.components.append(comp)
            layouts.append((SimpleNamespace(name=name), sub, 8.0, 4.0))
        mgraph, mcon, mrules, plan = collapse(graph, cc, rules, layouts)
        explicit = collapse(graph, cc, rules, layouts, prefix="MB", margin=0.3)
        self.assertEqual(sorted(plan.macros), ["MB00", "MB01"])
        self.assertEqual(mgraph.to_json(), explicit[0].to_json())
        self.assertEqual(mgraph.component("MB00").courtyard, (8.6, 4.6))
        self.assertEqual(plan.member_of, {"R1": "MB00", "D1": "MB00", "R2": "MB01", "D2": "MB01"})
        lg = collapse(graph, cc, rules, layouts, prefix="LG", margin=0.0)
        self.assertEqual(lg[0].component("LG01").courtyard, (8.0, 4.0))
        # trace_rows poses the members exactly as expand does.
        placed = BoardGraph.from_json(mgraph.to_json())
        placed.component("MB00").pos = (10.0, 7.0)
        placed.component("MB00").rot = 90.0
        flat = plan.expand(placed, graph)
        rows, groups, members = plan.trace_rows([["MB00", 10000, 7000, 90.0, "top"]])
        self.assertEqual(groups, [["MB00", 10000, 7000, 90.0, "top"]])
        self.assertEqual(members, {"MB00": ["R1", "D1"]})
        for ref, x, y, rot, side in rows:
            comp = flat.component(ref)
            self.assertEqual((x, y), (round(comp.pos[0] * 1000), round(comp.pos[1] * 1000)))
            self.assertEqual(rot, comp.rot)


if __name__ == "__main__":
    unittest.main()
