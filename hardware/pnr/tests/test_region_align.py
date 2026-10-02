"""Regions and alignments (pnr.place.regions): the parser, anchors at every rotation and
side, area containment, the legalizer and global placer on the 07-chaser-20 netlist,
rigid macros (line groups and blocks), the translation checker and relocation helpers,
and an unchanged path for designs without either constraint."""

import copy
import random
import unittest
from types import SimpleNamespace
from unittest import mock

from pnr.constraints import ConstraintError, Enforcement, compile_constraints, compile_routing_rules
from pnr.graph import BoardGraph, BoardOutline, Component, Net, Pad
from pnr.hier.blocks import extract_blocks
from pnr.hier.macro import collapse
from pnr.place import legalize as legalize_module
from pnr.place import model as model_module
from pnr.place import placer, regions
from pnr.place.geometry import Rect, resolve_fixed_poses, set_component_side
from pnr.place.legalize import LegalizationError, legalize, legalize_constraint_kwargs
from pnr.place.metrics import hard_violations, translation_checker

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
SIZE = (42.0, 32.0)
CLOCK = ["U1", "R1", "R2", "C1", "C2", "C3"]
WEST = [0.0, 0.0, SIZE[0] / 2, SIZE[1]]


def two_pin(ref, a, b, size, offset, pad):
    pads = [Pad("1", a, (-offset, 0.0), pad), Pad("2", b, (offset, 0.0), pad)]
    return Component(ref, ref[:1], (0.0, 0.0), 0.0, "top", size, size, pads=pads, smd_body=True)


def soic(ref, nets, size):
    half = len(nets) // 2
    pads = [
        Pad(
            str(i + 1),
            net,
            (-2.475 if i < half else 2.475, 1.27 * ((half - 1) / 2 - (i % half))),
            (1.95, 0.6),
        )
        for i, net in enumerate(nets[:half] + nets[half:][::-1])
    ]
    return Component(ref, "SOIC", (0.0, 0.0), 0.0, "top", size, size, pads=pads, smd_body=True)


def chaser():
    """The 20 parts of the ladder's 07-chaser-20 (TLC555 clock, CD4017B counter, five
    LEDs), with the stock footprints' courtyards and pads."""
    timer = ["GND", "TIMING", "CLOCK", "VCC", "CONTROL", "TIMING", "DISCHARGE", "VCC"]
    counter = [""] * 16
    for i, pin in enumerate((3, 2, 4, 7, 10)):
        counter[pin - 1] = "Q%d" % i
    counter[0] = "RESET"
    for pin, net in {8: "GND", 13: "GND", 14: "CLOCK", 15: "RESET", 16: "VCC"}.items():
        counter[pin - 1] = net
    parts = [
        Component(
            "J1",
            "PinHeader_1x02",
            (0.0, 0.0),
            0.0,
            "top",
            (3.63, 8.73),
            (3.63, 8.73),
            pads=[
                Pad("1", "VCC", (0.0, 0.0), (1.7, 1.7), True, (1.0, 1.0), True),
                Pad("2", "GND", (0.0, -2.54), (1.7, 1.7), True, (1.0, 1.0), True),
            ],
        ),
        soic("U1", timer, (7.49, 5.72)),
        soic("U2", counter, (7.49, 10.8)),
    ]
    resistor = dict(size=(3.45, 1.99), offset=0.912, pad=(1.025, 1.4))
    capacitor = dict(size=(3.49, 2.05), offset=0.95, pad=(1.0, 1.45))
    led = dict(size=(3.49, 2.04), offset=0.9375, pad=(0.975, 1.4))
    parts += [
        two_pin("R1", "VCC", "DISCHARGE", **resistor),
        two_pin("R2", "DISCHARGE", "TIMING", **resistor),
        two_pin("C1", "TIMING", "GND", **capacitor),
        two_pin("C2", "CONTROL", "GND", **capacitor),
        two_pin("C3", "VCC", "GND", **capacitor),
        two_pin("C4", "VCC", "GND", **capacitor),
        two_pin("C5", "VCC", "GND", **capacitor),
    ]
    for i in range(5):
        parts.append(two_pin("R%d" % (i + 3), "Q%d" % i, "LED%d" % i, **resistor))
        parts.append(two_pin("D%d" % (i + 1), "GND", "LED%d" % i, **led))
    nets = {}
    for comp in parts:
        for pad in comp.pads:
            if pad.net:
                nets.setdefault(pad.net, []).append((comp.ref, pad.name))
    return BoardGraph(
        "chaser",
        sorted(parts, key=lambda c: c.ref),
        [Net(n, i + 1, pins) for i, (n, pins) in enumerate(sorted(nets.items()))],
        BoardOutline(*SIZE),
    )


def doc(**extra):
    out = dict(
        schema="v0",
        board=dict(outline=dict(w=SIZE[0], h=SIZE[1]), layers=2, default_clearance_mm=0.4),
        fab=FAB,
        fixed={"J1": dict(at=[4, SIZE[1] / 2], rot=0, side="top")},
        net_class={"supply": dict(nets=["VCC"], width_mm=0.4)},
    )
    out.update(extra)
    return out


def absolute(**region):
    """07-chaser-20-abs without its holes: LEDs on the south edge, ICs' rotations
    locked, a label keep-out, the clock parts in the west half."""
    entry = dict(name="clock", refs=CLOCK, rect=WEST)
    entry.update(region)
    k = [SIZE[0] / 2 - 6, SIZE[1] - 5, SIZE[0] / 2 + 6, SIZE[1]]
    return doc(
        edge_align={"D%d" % i: dict(edge="south", hard=True) for i in range(1, 6)},
        orientation={**{"D%d" % i: 0 for i in range(1, 6)}, "U1": 0, "U2": 0},
        keepout=[
            dict(name="label", polygon=[[k[0], k[1]], [k[2], k[1]], [k[2], k[3]], [k[0], k[3]]])
        ],
        region=[entry],
    )


def relative(**align):
    """07-chaser-20-rel: two line groups, two hard proximity groups, U1/U2 aligned."""
    entry = dict(name="ics", refs=["U1", "U2"], axis="y")
    entry.update(align)
    return doc(
        line_group=[
            dict(name="leds", members=["D1", "D2", "D3", "D4", "D5"], pitch_mm=3.0, rot=90),
            dict(name="timing", members=["R1", "R2"], pitch_mm=2.5, rot=90),
        ],
        group=[
            dict(members=["C1", "C2", "C3"], anchor="U1", hard=True, radius_mm=8.0),
            dict(members=["C4"], anchor="U2", hard=True, radius_mm=8.0),
        ],
        align=[entry],
    )


def protrusion(graph, cc):
    total = 0.0
    for con in regions.region_rules(cc):
        area = regions.area_of(con)
        for ref in con.refs:
            for b in regions.placed_boxes(graph.component(ref), con):
                total += area.protrusion2(b)
    return total


class ParserTest(unittest.TestCase):
    def setUp(self):
        self.refs = chaser().refs

    def compile(self, **extra):
        return compile_constraints(doc(**extra), self.refs)

    def test_region_forms_and_defaults(self):
        cc = self.compile(region=[dict(name="clock", refs=["U1", "R[12]", "C*"], rect=WEST)])
        (con,) = [c for c in cc.constraints if c.kind == "region"]
        self.assertIs(con.enforcement, Enforcement.HARD)
        self.assertEqual(con.refs, ("U1", "R1", "R2", "C1", "C2", "C3", "C4", "C5"))
        self.assertEqual(con.params["areas"], [{"rect": WEST}])
        self.assertEqual(con.weight, 10.0)
        self.assertNotIn("region", " ".join(cc.warnings))
        poly = [[0, 0], [10, 0], [10, 10], [0, 10]]
        cc = self.compile(
            region=[
                dict(name="a", refs=["U1"], polygon=poly, hard=False, weight=3),
                dict(name="b", refs=["U2"], areas=[dict(rect=[0, 0, 5, 5]), dict(polygon=poly)]),
            ]
        )
        a, b = [c for c in cc.constraints if c.kind == "region"]
        self.assertIs(a.enforcement, Enforcement.SOFT)
        self.assertEqual(a.weight, 3.0)
        self.assertEqual(a.params["areas"], [{"polygon": [[float(x), float(y)] for x, y in poly]}])
        self.assertEqual(len(b.params["areas"]), 2)

    def test_region_rejects(self):
        for bad in (
            dict(refs=["U1"], rect=WEST),  # no name
            dict(name="r", refs=[], rect=WEST),
            dict(name="r", refs=["ZZ9"], rect=WEST),  # no known ref
            dict(name="r", refs=["U1"]),  # no area
            dict(name="r", refs=["U1"], rect=WEST, polygon=[[0, 0], [1, 0], [1, 1]]),
            dict(name="r", refs=["U1"], rect=[5, 0, 5, 10]),  # degenerate
            dict(name="r", refs=["U1"], rect=[0, 0, float("nan"), 10]),
            dict(name="r", refs=["U1"], polygon=[[0, 0], [1, 1], [2, 2]]),  # no area
            dict(name="r", refs=["U1"], polygon=[[0, 0], [1, 1]]),
            dict(name="r", refs=["U1"], areas=[]),
            dict(name="r", refs=["U1"], areas=[dict(rect=WEST, polygon=[[0, 0]])]),
            dict(name="r", refs=["U1"], rect=WEST, hard="yes"),
            dict(name="r", refs=["U1"], rect=WEST, weight=0),
        ):
            with self.subTest(bad=bad), self.assertRaises(ConstraintError):
                self.compile(region=[bad])
        with self.assertRaises(ConstraintError):
            self.compile(region=[dict(name="r", refs=["U1"], rect=WEST)] * 2)  # duplicate name

    def test_align_forms_and_defaults(self):
        cc = self.compile(align=[dict(name="ics", refs=["U1", "U2"], axis="y")])
        (con,) = [c for c in cc.constraints if c.kind == "align"]
        self.assertIs(con.enforcement, Enforcement.HARD)
        self.assertEqual(
            con.params,
            dict(axis="y", anchors={"U1": "origin", "U2": "origin"}, tol_mm=0.25, reason=None),
        )
        self.assertEqual(con.weight, 5.0)
        cc = self.compile(
            align=[
                dict(
                    name="edges",
                    refs=["U1", "U2", "C1"],
                    axis="x",
                    anchor={"U1": "west", "U2": "center", "C1": "pad:2"},
                    tol_mm=0.15,
                    hard=False,
                )
            ]
        )
        (con,) = [c for c in cc.constraints if c.kind == "align"]
        self.assertEqual(con.params["anchors"], {"U1": "west", "U2": "centre", "C1": "pad:2"})
        self.assertIs(con.enforcement, Enforcement.SOFT)

    def test_align_rejects(self):
        lines = dict(line_group=[dict(name="leds", members=["D1", "D2", "D3"], pitch_mm=3.0)])
        for bad, extra in (
            (dict(name="a", refs=["U1"], axis="y"), {}),  # one ref
            (dict(name="a", refs=["U1", "ZZ9"], axis="y"), {}),  # one known ref
            (dict(name="a", refs=["U1", "U2"]), {}),  # no axis
            (dict(name="a", refs=["U1", "U2"], axis="z"), {}),
            (dict(name="a", refs=["U1", "U2"], axis="y", anchor="west"), {}),  # wrong axis edge
            (dict(name="a", refs=["U1", "U2"], axis="x", anchor="north"), {}),
            (dict(name="a", refs=["U1", "U2"], axis="y", anchor="pin1"), {}),
            (dict(name="a", refs=["U1", "U2"], axis="y", anchor={"C1": "origin"}), {}),
            (dict(name="a", refs=["U1", "U2"], axis="y", tol_mm=0.1), {}),
            (dict(name="a", refs=["U1", "U2"], axis="y", hard=1), {}),
            (dict(name="a", refs=["D1", "D3", "U1"], axis="y"), lines),  # two in one line
        ):
            with self.subTest(bad=bad), self.assertRaises(ConstraintError):
                self.compile(align=[bad], **extra)
        # One member of a line group is fine (the line's macro carries its anchor).
        self.compile(align=[dict(name="a", refs=["D1", "U1"], axis="y")], **lines)


class AnchorTest(unittest.TestCase):
    def setUp(self):
        self.u1 = chaser().component("U1")  # pad 1 at (-2.475, 1.905), courtyard 7.49 x 5.72

    def test_anchors_turn_with_the_part(self):
        u1 = self.u1
        pad1 = {0: (-2.475, 1.905), 90: (-1.905, -2.475), 180: (2.475, -1.905), 270: (1.905, 2.475)}
        for rot, (px, py) in pad1.items():
            with self.subTest(rot=rot):
                self.assertEqual(regions.anchor_offset(u1, "origin", "x", rot), 0.0)
                self.assertAlmostEqual(regions.anchor_offset(u1, "pad1", "x", rot), px)
                self.assertAlmostEqual(regions.anchor_offset(u1, "pad1", "y", rot), py)
                self.assertAlmostEqual(regions.anchor_offset(u1, "pad:1", "y", rot), py)
                w, h = (7.49, 5.72) if rot % 180 == 0 else (5.72, 7.49)
                self.assertAlmostEqual(regions.anchor_offset(u1, "south", "y", rot), -h / 2)
                self.assertAlmostEqual(regions.anchor_offset(u1, "north", "y", rot), h / 2)
                self.assertAlmostEqual(regions.anchor_offset(u1, "west", "x", rot), -w / 2)
                self.assertAlmostEqual(regions.anchor_offset(u1, "east", "x", rot), w / 2)
                # The pad box is symmetric: its centre is the origin at every turn.
                self.assertAlmostEqual(regions.anchor_offset(u1, "centre", "x", rot), 0.0)
        u1.pos, u1.rot = (10.0, 5.0), 90.0
        self.assertAlmostEqual(regions.anchor_value(u1, "pad1", "x"), 10.0 - 1.905)
        with self.assertRaises(ValueError):
            regions.anchor_offset(u1, "pad:99", "x", 0)

    def test_bottom_side_mirrors_pad_anchors(self):
        u1 = self.u1
        set_component_side(u1, "bottom")
        self.assertAlmostEqual(regions.anchor_offset(u1, "pad1", "y", 0), -1.905)
        self.assertAlmostEqual(regions.anchor_offset(u1, "pad1", "x", 90), 1.905)
        self.assertAlmostEqual(regions.anchor_offset(u1, "south", "y", 0), -5.72 / 2)


class AreaTest(unittest.TestCase):
    def test_rectangle(self):
        area = regions.Area([("rect", (0, 0, 10, 5))])
        self.assertTrue(area.contains((0, 0, 10, 5)))
        self.assertTrue(area.contains((1, 1, 2, 2)))
        self.assertFalse(area.contains((9, 1, 10.01, 2)))
        self.assertEqual(area.dist2([5, 13], [2, 2]).tolist(), [0.0, 9.0])

    def test_concave_polygon(self):
        ell = [(0, 0), (10, 0), (10, 4), (4, 4), (4, 10), (0, 10)]
        area = regions.Area([("polygon", ell)])
        self.assertTrue(area.contains((0.5, 0.5, 9.5, 3.5)))
        self.assertTrue(area.contains((0, 0, 4, 10)))
        self.assertFalse(area.contains((3, 3, 6, 6)))  # the notch corner
        self.assertFalse(area.contains((5, 5, 6, 6)))  # in the notch
        # A slit: every corner of the box is inside, but an edge pair crosses it.
        slit = [(0, 0), (4.9, 0), (4.9, 8), (5.1, 8), (5.1, 0), (10, 0), (10, 10), (0, 10)]
        self.assertFalse(regions.Area([("polygon", slit)]).contains((2, 1, 8, 2)))
        self.assertGreater(area.dist2([7], [7])[0], 0.0)
        self.assertEqual(area.dist2([2], [7])[0], 0.0)

    def test_union_and_conservative_raster(self):
        union = regions.Area([("rect", (0, 0, 5, 5)), ("rect", (5, 0, 10, 5))])
        self.assertTrue(union.contains((4, 1, 6, 2)))  # straddles the two pieces
        self.assertFalse(union.contains((4, 1, 6, 5.5)))
        ell = regions.Area([("polygon", [(0, 0), (10, 0), (10, 4), (4.1, 4), (4.1, 10), (0, 10)])])
        rng = random.Random(0)
        accepted = 0
        for _ in range(400):
            x, y = rng.uniform(0, 9), rng.uniform(0, 9)
            box = (x, y, x + rng.uniform(0.2, 4), y + rng.uniform(0.2, 4))
            if ell.raster_contains([box[0]], [box[1]], [box[2]], [box[3]])[0]:
                accepted += 1
                self.assertTrue(ell.contains(box), box)  # the raster never over-accepts
        self.assertGreater(accepted, 10)


def placed_ok(test, graph, cc, seed, iters=150, **kwargs):
    placed, report = placer.place(graph, cc, seed=seed, iters=iters, spread=1.3, **kwargs)
    test.assertTrue(report.legal, report.summary())
    test.assertEqual(regions.violations(placed, cc), [])
    return placed


class LegalizeTest(unittest.TestCase):
    def test_hard_region_on_seeds_0_to_7(self):
        graph = chaser()
        cc = compile_constraints(absolute(), graph.refs)
        for seed in range(8):
            with self.subTest(seed=seed):
                placed = placed_ok(self, graph, cc, seed)
                for ref in CLOCK:
                    box = regions.placed_boxes(placed.component(ref), regions.region_rules(cc)[0])
                    self.assertLessEqual(box[0][2], SIZE[0] / 2 + 1e-6)

    def test_hard_align_on_seeds_0_to_7(self):
        graph = chaser()
        cc = compile_constraints(relative(), graph.refs)
        for seed in range(8):
            with self.subTest(seed=seed):
                placed = placed_ok(self, graph, cc, seed)
                u1, u2 = placed.component("U1"), placed.component("U2")
                self.assertLessEqual(abs(u1.pos[1] - u2.pos[1]), 0.25)

    def test_rotation_dependent_anchors(self):
        """Pad-1 anchors of free-turning parts: the band follows each tried rotation."""
        graph = chaser()
        cc = compile_constraints(
            doc(align=[dict(name="p", refs=["U1", "U2", "C1"], axis="x", anchor="pad1")]),
            graph.refs,
        )
        turns = set()
        for seed in range(8):
            with self.subTest(seed=seed):
                placed = placed_ok(self, graph, cc, seed)
                values = [
                    regions.anchor_value(placed.component(r), "pad1", "x")
                    for r in ("U1", "U2", "C1")
                ]
                self.assertLessEqual(max(values) - min(values), 0.25 + 1e-6)
                turns.update(placed.component(r).rot for r in ("U1", "U2", "C1"))
        self.assertGreater(len(turns), 1)

    def test_backtracking_moves_an_earlier_member(self):
        """A's nearest slot leaves B no room on A's line (a keep-out fills the rest of the
        band): the legalizer backtracks and moves A."""
        a = Component("A", "x", (3.0, 7.0), 0.0, "top", (2.0, 2.0), (2.0, 2.0), smd_body=True)
        b = Component("B", "x", (15.0, 7.0), 0.0, "top", (2.0, 2.0), (2.0, 2.0), smd_body=True)
        graph = BoardGraph("bt", [a, b], [], BoardOutline(20.0, 10.0))
        cc = compile_constraints(
            dict(
                board=dict(outline=dict(w=20, h=10), default_clearance_mm=0.2),
                align=[dict(name="ab", refs=["A", "B"], axis="y")],
            ),
            graph.refs,
        )
        keepout = Rect(12.0, 7.5, 16.0, 5.0)  # x 4..20, y 5..10: room for one part
        common = dict(fixed={}, keepouts=[keepout], clearance=0.2, grid_mm=0.25)
        plain = legalize(graph, 20.0, 10.0, **common)
        self.assertGreater(abs(plain.component("A").pos[1] - plain.component("B").pos[1]), 1.0)
        aligned = regions.legalize_kwargs(cc, graph.components)
        placed = legalize(graph, 20.0, 10.0, **common, **aligned)
        self.assertLessEqual(abs(placed.component("A").pos[1] - placed.component("B").pos[1]), 0.25)
        self.assertLess(placed.component("A").pos[1], 5.0)
        self.assertEqual(hard_violations(placed, cc)["group_outside"], [])
        with self.assertRaisesRegex(LegalizationError, "B: no free slot inside its edge band"):
            legalize(graph, 20.0, 10.0, **common, **aligned, backtrack_budget=0)

    def test_polygon_region_mask(self):
        graph = chaser()
        ell = [[0, 0], [21, 0], [21, 12], [12, 12], [12, 32], [0, 32]]
        cc = compile_constraints(doc(region=[dict(name="l", refs=CLOCK, polygon=ell)]), graph.refs)
        for seed in range(3):
            with self.subTest(seed=seed):
                placed_ok(self, graph, cc, seed)

    def test_soft_relations_are_penalties(self):
        graph = chaser()
        hard = compile_constraints(absolute(), graph.refs)
        soft = compile_constraints(absolute(hard=False, weight=50), graph.refs)
        free = compile_constraints(
            absolute(refs=["ZZ9", "U1"], hard=False, weight=1e-9), graph.refs
        )
        self.assertEqual(regions.violations(graph, soft), [])  # never a legality finding
        with_soft = sum(
            protrusion(placer.place(graph, soft, seed=s, iters=150, spread=1.3)[0], hard)
            for s in range(4)
        )
        without = sum(
            protrusion(placer.place(graph, free, seed=s, iters=150, spread=1.3)[0], hard)
            for s in range(4)
        )
        self.assertLess(with_soft, without)


class GlobalTest(unittest.TestCase):
    def test_terms_reduce_protrusion_and_spread(self):
        graph = chaser()
        for spec, measure in (
            (absolute(), "region"),
            (relative(), "align"),
        ):
            cc = compile_constraints(spec, graph.refs)
            bare = copy.deepcopy(cc)
            bare.constraints = [c for c in cc.constraints if c.kind not in ("region", "align")]
            got = {}
            for name, con in (("with", cc), ("without", bare)):
                total = 0.0
                for seed in range(3):
                    pos, rot = model_module.global_place(graph, con, *SIZE, seed=seed, iters=200)
                    g = BoardGraph.from_json(graph.to_json())
                    for comp in g.components:
                        comp.pos, comp.rot = pos[comp.ref], rot[comp.ref]
                    if measure == "region":
                        total += protrusion(g, cc)
                    else:
                        total += regions.align_spread(g, regions.align_rules(cc)[0])
                got[name] = total
            with self.subTest(measure=measure):
                self.assertLess(got["with"], got["without"] * 0.5 + 1e-9)


def macro_boxes_match(test, mgraph, mcon, flat, cc, plan, ref, rot):
    """Place macro ``plan.member_of[ref]`` at ``rot`` and compare its region bodies and
    anchors (``mcon``) with the expanded members' (``cc``)."""
    mref = plan.member_of[ref]
    placed = BoardGraph.from_json(mgraph.to_json())
    placed.component(mref).pos = (20.0, 15.0)
    placed.component(mref).rot = float(rot)
    expanded = plan.expand(placed, flat)
    for con in regions.region_rules(mcon):
        if mref in con.refs:
            original = next(c for c in regions.region_rules(cc) if c.name == con.name)
            got = sorted(regions.placed_boxes(placed.component(mref), con))
            want = sorted(
                b
                for r in original.refs
                if plan.member_of.get(r) == mref
                for b in regions.placed_boxes(expanded.component(r), original)
            )
            for g_, w_ in zip(got, want):
                for u, v in zip(g_, w_):
                    test.assertAlmostEqual(u, v, places=6)
            test.assertEqual(len(got), len(want))
    for con in regions.align_rules(mcon):
        if mref in con.refs:
            original = next(c for c in regions.align_rules(cc) if c.name == con.name)
            (member,) = [r for r in original.refs if plan.member_of.get(r) == mref]
            axis = con.params["axis"]
            got = regions.anchor_value(placed.component(mref), regions.anchor_spec(con, mref), axis)
            want = regions.anchor_value(
                expanded.component(member), regions.anchor_spec(original, member), axis
            )
            test.assertAlmostEqual(got, want, places=6)


class MacroTest(unittest.TestCase):
    def test_line_group_macro_is_exact_at_every_rotation(self):
        from pnr.place import line_group

        graph = chaser()
        spec = doc(
            line_group=[
                dict(name="leds", members=["D1", "D2", "D3", "D4", "D5"], pitch_mm=3.0, rot=90)
            ],
            region=[dict(name="r", refs=["D2", "D4", "U1"], rect=[0, 0, 30, 32])],
            align=[dict(name="a", refs=["D3", "U2"], axis="x", anchor="pad1")],
        )
        cc = compile_constraints(spec, graph.refs)
        flat = BoardGraph.from_json(graph.to_json())
        mgraph, mcon, _, plan = line_group.collapse(flat, cc, None)
        (region,) = regions.region_rules(mcon)
        self.assertEqual(region.refs, ("LG00", "U1"))
        self.assertEqual(len(region.params["bodies"]["LG00"]), 2)
        (align,) = regions.align_rules(mcon)
        self.assertIn("point", align.params["anchors"]["LG00"])
        for rot in (0, 90, 180, 270):
            with self.subTest(rot=rot):
                macro_boxes_match(self, mgraph, mcon, flat, cc, plan, "D2", rot)
                macro_boxes_match(self, mgraph, mcon, flat, cc, plan, "D3", rot)
        for seed in range(3):
            with self.subTest(seed=seed):
                placed_ok(self, graph, cc, seed)

    def test_block_macro_is_exact_and_aligns_are_interface_parts(self):
        graph = chaser()
        spec = doc(
            region=[dict(name="r", refs=["C1", "R3"], rect=[0, 0, 21, 32])],
            align=[dict(name="a", refs=["U1", "D1"], axis="y", anchor={"D1": "north"})],
        )
        cc = compile_constraints(spec, graph.refs)
        flat = BoardGraph.from_json(graph.to_json())
        rules = compile_routing_rules(cc, [n.name for n in graph.nets])
        layouts = []
        for name, refs in (("a", ["C1", "R1"]), ("b", ["R3", "D1"])):
            sub = BoardGraph(name)
            for i, ref in enumerate(refs):
                comp = copy.deepcopy(graph.component(ref))
                comp.pos, comp.rot = (2.5 + 4.5 * i, 2.0 + i), 90.0 * i
                sub.components.append(comp)
            layouts.append((SimpleNamespace(name=name), sub, 10.0, 5.0))
        mgraph, mcon, _, plan = collapse(flat, cc, rules, layouts)
        for rot in (0, 90, 180, 270):
            with self.subTest(rot=rot):
                macro_boxes_match(self, mgraph, mcon, flat, cc, plan, "C1", rot)
                macro_boxes_match(self, mgraph, mcon, flat, cc, plan, "R3", rot)
                macro_boxes_match(self, mgraph, mcon, flat, cc, plan, "D1", rot)
        # Block extraction keeps aligned parts top-level.
        for block in extract_blocks(graph, cc):
            self.assertFalse({"U1", "D1"} & set(block.refs))
        # Two aligned refs inside one macro cannot be expressed on it.
        both = compile_constraints(
            doc(align=[dict(name="a", refs=["R3", "D1"], axis="y")]), graph.refs
        )
        with self.assertRaises(ValueError):
            collapse(flat, both, rules, layouts)


class CheckerTest(unittest.TestCase):
    def test_translation_checker_and_relocation_helpers(self):
        graph = chaser()
        cc = compile_constraints(
            doc(
                region=[dict(name="clock", refs=CLOCK, rect=WEST)],
                align=[dict(name="ics", refs=["U1", "U2"], axis="y")],
            ),
            graph.refs,
        )
        placed = placed_ok(self, graph, cc, 0)
        legal = translation_checker(placed, cc)
        u1, u2 = placed.component("U1"), placed.component("U2")
        old = u1.pos
        u1.pos = (SIZE[0] - 5, old[1])  # out of the west half
        self.assertFalse(legal(u1))
        self.assertIn("U1", hard_violations(placed, cc)["group_outside"])
        u1.pos = (old[0], old[1] + 1.0)  # off the line
        self.assertFalse(legal(u1))
        self.assertEqual(sorted(regions.align_offenders(placed, cc)), ["U1", "U2"])
        snap = regions.align_snap(placed, cc, u1)
        self.assertEqual(snap, {1: u2.pos[1]})
        u1.pos = old
        self.assertTrue(legal(u1))
        soft = compile_constraints(
            doc(
                region=[dict(name="clock", refs=CLOCK, rect=WEST, hard=False)],
                align=[dict(name="ics", refs=["U1", "U2"], axis="y", hard=False)],
            ),
            graph.refs,
        )
        self.assertEqual(regions.align_snap(placed, soft, u1), {})
        here = regions.soft_penalty(placed, soft, u1, u1.pos)
        away = regions.soft_penalty(placed, soft, u1, (SIZE[0] - 5, u1.pos[1] + 3))
        self.assertGreater(away, here)
        self.assertGreater(away - here, 5 * 2.5**2)  # at least the align term


class CostInspectTest(unittest.TestCase):
    def test_region_and_align_terms_at_rigid_poses(self):
        from pnr.place.cost_inspect import BASE_TERMS, TERMS, Objective

        graph = chaser()
        spec = doc(
            region=[dict(name="r", refs=["U1"], rect=[0, 0, 10, 32])],
            align=[dict(name="a", refs=["U1", "U2"], axis="y")],
        )
        cc = compile_constraints(spec, graph.refs)
        graph.component("U1").pos = (12.0, 10.0)  # courtyard x 8.255..15.745
        graph.component("U2").pos = (30.0, 12.0)
        objective = Objective(graph, cc)
        self.assertEqual(objective.term_keys, list(TERMS[:BASE_TERMS]) + ["region", "alignment"])
        report = objective.report()
        u1 = {t["key"]: t for t in report["components"]["U1"]["terms"]}
        u2 = {t["key"]: t for t in report["components"]["U2"]["terms"]}
        self.assertAlmostEqual(u1["region"]["raw_share"], 2 * 5.745**2, places=6)
        self.assertAlmostEqual(u1["region"]["weighted"], 40 * 2 * 5.745**2, places=4)
        self.assertEqual(u2["region"]["raw_share"], 0.0)
        for terms in (u1, u2):  # anchors 10 and 12: mean 11, shared equally
            self.assertAlmostEqual(terms["alignment"]["raw_share"], 1.0)
            self.assertAlmostEqual(terms["alignment"]["weighted"], 20.0)
        plain = compile_constraints(doc(), graph.refs)
        self.assertEqual(Objective(graph, plain).term_keys, list(TERMS[:BASE_TERMS]))


class UnchangedTest(unittest.TestCase):
    def test_designs_without_either_constraint_are_unchanged(self):
        graph = chaser()
        spec = absolute()
        del spec["region"]
        cc = compile_constraints(spec, graph.refs)
        empty = compile_constraints(dict(spec, region=[], align=[]), graph.refs)
        self.assertEqual(cc.constraints, empty.constraints)
        self.assertFalse(regions.declared(cc))
        poses = resolve_fixed_poses(graph, cc)
        kwargs = legalize_constraint_kwargs(graph, cc, poses)
        self.assertEqual(
            sorted(kwargs),
            ["edge_bands", "fixed", "group_edges", "group_limits", "keepouts", "rotations"],
        )
        real = legalize_module.legalize
        calls = []

        def spy(*args, **kw):
            calls.append(kw)
            return real(*args, **kw)

        with mock.patch.object(placer, "legalize", side_effect=spy), mock.patch.object(
            regions, "GlobalTerms", side_effect=AssertionError
        ), mock.patch.object(regions, "LegalizeRules", side_effect=AssertionError):
            spied, _ = placer.place(graph, cc, seed=2, iters=120, spread=1.3)
        self.assertTrue(calls)
        self.assertTrue(all("regions" not in kw and "aligns" not in kw for kw in calls))
        plain, _ = placer.place(graph, empty, seed=2, iters=120, spread=1.3)
        self.assertEqual(spied.to_json(), plain.to_json())

    def test_power_first_refuses_regions(self):
        graph = chaser()
        cc = compile_constraints(absolute(), graph.refs)
        with mock.patch.dict("os.environ", {"PNR_POWER_FIRST": "1"}):
            with self.assertRaises(ValueError):
                placer.place(graph, cc, seed=0, iters=20)


if __name__ == "__main__":
    unittest.main()
