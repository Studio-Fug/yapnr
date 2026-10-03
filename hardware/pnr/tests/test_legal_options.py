"""Opt-in legalizer options (``legalize:``, :mod:`pnr.place.legal_options`): the parser,
``outline: exact`` (the courtyard-in-outline box shared with the hard check), and an
unchanged legalizer when the section is absent."""

import itertools
import math
import random
import unittest

import numpy as np

from pnr.constraints import ConstraintError, compile_constraints
from pnr.graph import BoardGraph, BoardOutline, Component, Pad
from pnr.place import legal_options
from pnr.place.geometry import courtyard_rect
from pnr.place.legalize import legalize, legalize_constraint_kwargs
from pnr.place.metrics import outside_outline

# The radar board of the stage-2 reproduction: 46.3 mm is not a whole number of 0.25 mm
# cells, so the legalizer's raster has a partial top row reaching 46.5 mm.
W, H = 60.0, 46.3


def part(ref, pos, size, rot=0.0, pads=()):
    return Component(ref, ref[:1], pos, rot, "top", size, size, pads=list(pads))


def board(parts, w=W, h=H):
    return BoardGraph("legal", list(parts), [], BoardOutline(w, h))


def doc(**sections):
    return dict(schema="v0", board=dict(outline=dict(w=W, h=H)), **sections)


class ParseTest(unittest.TestCase):
    def test_absent_is_none_and_kwargs_unchanged(self):
        c = compile_constraints(doc(), ["U1"])
        self.assertIsNone(c.legalize)
        self.assertEqual(legal_options.legalize_kwargs(c), {})
        g = board([part("U1", (5, 5), (2, 2))])
        self.assertEqual(
            sorted(legalize_constraint_kwargs(g, c, {})),
            ["fixed", "group_edges", "group_limits", "keepouts", "rotations"],
        )

    def test_values(self):
        c = compile_constraints(
            doc(legalize=dict(outline="exact", order="scarcity", lookahead="regions")), ["U1"]
        )
        self.assertEqual(c.legalize, dict(outline="exact", order="scarcity", lookahead="regions"))
        self.assertEqual(legal_options.legalize_kwargs(c)["outline"], "exact")
        # A default value is accepted and changes nothing.
        c = compile_constraints(doc(legalize=dict(outline="raster")), ["U1"])
        self.assertEqual(legal_options.legalize_kwargs(c), {})
        self.assertFalse(any("legalize" in w for w in c.warnings))

    def test_refusals(self):
        for bad in (
            dict(outline="tight"),
            dict(order=None),
            dict(lookahead="all"),
            dict(spread=2),
            ["exact"],
        ):
            with self.assertRaises(ConstraintError, msg=str(bad)):
                compile_constraints(doc(legalize=bad), ["U1"])


class OutlineTest(unittest.TestCase):
    def test_north_edge_breach_reproduced_and_fixed(self):
        # Stage-2 start p001: U6 (4.19 x 3.8 courtyard, turned 180) pulled to the north
        # edge lands at y 44.5 on the raster, its courtyard top at 46.4 > 46.3.
        g = board([part("U6", (48.0, 46.0), (4.19, 3.8), 180), part("C1", (10, 1), (1, 0.5))])
        raster = legalize(g, W, H, fixed={}, keepouts=[], grid_mm=0.25, clearance=0.2)
        self.assertEqual(raster.component("U6").pos, (48.0, 44.5))
        self.assertEqual(outside_outline(raster, W, H), ["U6"])
        exact = legalize(
            g, W, H, fixed={}, keepouts=[], grid_mm=0.25, clearance=0.2, outline="exact"
        )
        self.assertEqual(outside_outline(exact, W, H), [])
        self.assertEqual(exact.component("U6").pos, (48.0, 44.25))
        # A part nowhere near the partial row keeps its raster slot.
        self.assertEqual(exact.component("C1").pos, raster.component("C1").pos)

    def test_every_edge_and_rotation(self):
        w, h = 20.1, 10.3  # partial last column and row
        rng = random.Random(7)
        for k in range(40):
            size = (rng.uniform(0.6, 4.0), rng.uniform(0.6, 3.0))
            rot = rng.choice((0.0, 90.0, 180.0, 270.0))
            target = (rng.choice((0.0, w)), rng.choice((0.0, h)))
            g = board([part("U1", target, size, rot)], w, h)
            for grid in (0.25, 0.5):
                placed = legalize(
                    g, w, h, fixed={}, keepouts=[], grid_mm=grid, clearance=0.2, outline="exact"
                )
                self.assertEqual(outside_outline(placed, w, h), [], (k, size, rot, target, grid))

    def test_box_is_the_hard_check(self):
        # A pose inside outline_box <=> its courtyard passes metrics.outside_outline.
        rng = random.Random(3)
        for _ in range(500):
            c = part(
                "U1",
                (rng.uniform(-1, 21), rng.uniform(-1, 11)),
                (rng.uniform(0.2, 6), rng.uniform(0.2, 6)),
                rng.choice((0.0, 90.0, 180.0, 270.0)),
            )
            x0, x1, y0, y1 = legal_options.outline_box(c, 20.0, 10.0)
            inside = x0 - 1e-9 <= c.pos[0] <= x1 + 1e-9 and y0 - 1e-9 <= c.pos[1] <= y1 + 1e-9
            self.assertEqual(inside, courtyard_rect(c).inside(20.0, 10.0))

    def test_mark_outside(self):
        occ = np.zeros((int(math.ceil(H / 0.25)), int(math.ceil(20.1 / 0.25))), dtype=bool)
        legal_options.mark_outside(occ, 0.25, 20.1, H)
        self.assertTrue(occ[-1].all() and occ[:, -1].all())
        self.assertFalse(occ[:-1, :-1].any())

    def test_off_is_unchanged(self):
        # Without the option the legalizer's result is byte-identical (default kwargs).
        rng = random.Random(11)
        parts = [
            part(
                "P%d" % i,
                (rng.uniform(0, W), rng.uniform(0, H)),
                (rng.uniform(0.6, 3.0), rng.uniform(0.6, 3.0)),
                rng.choice((0.0, 90.0)),
            )
            for i in range(30)
        ]
        g = board(parts)
        a = legalize(g, W, H, fixed={}, keepouts=[], grid_mm=0.25, clearance=0.2)
        b = legalize(g, W, H, fixed={}, keepouts=[], grid_mm=0.25, clearance=0.2, outline=None)
        self.assertEqual(a.to_json(), b.to_json())
        # Exact keeps every part in; it may only move the ones the raster put out.
        c = legalize(g, W, H, fixed={}, keepouts=[], grid_mm=0.25, clearance=0.2, outline="exact")
        self.assertEqual(outside_outline(c, W, H), [])
        moved = {
            p.ref for p, q in zip(a.components, c.components) if p.pos != q.pos or p.rot != q.rot
        }
        if not outside_outline(a, W, H):
            self.assertEqual(moved, set())


def window_board(seed, one_slot=False):
    """A fixed U1 with eight caps in a roomy hard group (radius 5.5 mm), and J2 held only
    by a hard region: a window under U1 (or, ``one_slot``, a window with one slot). The
    caps' global targets lie in J2's window, so blocks-first legalization fills it."""
    rng = random.Random(seed)
    parts = [Component("U1", "U", (5, 6), 0, "top", (3, 3), (3, 3))]
    for i in range(8):
        pos = (rng.uniform(1, 7), rng.uniform(0.3, 2.2))
        parts.append(Component("C%d" % i, "C", pos, 0, "top", (1.5, 0.9), (1.5, 0.9)))
    parts.append(Component("J2", "J", (4, 1.2), 0, "top", (6, 2), (6, 2)))
    g = BoardGraph("window", parts, [], BoardOutline(20, 10))
    window = [0.85, 0.0, 7.0, 2.3] if one_slot else [0, 0, 8, 2.6]
    spec = dict(
        schema="v0",
        board=dict(outline=dict(w=20, h=10), default_clearance_mm=0.2),
        fixed={"U1": dict(at=[5, 6], rot=0, side="top")},
        orientation={"J2": 0},
        group=[dict(members=["C%d" % i for i in range(8)], anchor="U1", radius_mm=5.5, hard=True)],
        region=[dict(name="j2", refs=["J2"], rect=window)],
    )
    return g, spec


def legal_count(options, seeds=range(10), one_slot=False):
    from pnr.place.geometry import resolve_fixed_poses
    from pnr.place.legalize import LegalizationError
    from pnr.place.metrics import hard_violations

    count = 0
    for seed in seeds:
        g, spec = window_board(seed, one_slot)
        if options:
            spec["legalize"] = options
        c = compile_constraints(spec, [p.ref for p in g.components])
        kw = legalize_constraint_kwargs(g, c, resolve_fixed_poses(g, c))
        try:
            placed = legalize(g, 20, 10, clearance=0.2, grid_mm=0.25, backtrack_budget=50, **kw)
        except LegalizationError:
            continue
        count += not any(hard_violations(placed, c).values())
    return count


class OrderTest(unittest.TestCase):
    def test_window_part_before_roomy_group(self):
        # Blocks first: the caps fill J2's window and backtracking cannot recover it.
        self.assertEqual(legal_count(None), 0)
        self.assertEqual(legal_count(dict(order="scarcity")), 10)

    def test_scarcity_blocks(self):
        from pnr.place.legalize import _scarcity_blocks

        g, spec = window_board(0)
        spec["edge_align"] = {"C7": dict(edge="south", hard=True, tolerance_mm=1.0)}
        spec["group"][0]["members"] = ["C%d" % i for i in range(6)]
        c = compile_constraints(spec, [p.ref for p in g.components])
        kw = legalize_constraint_kwargs(g, c, {})
        from pnr.place.regions import LegalizeRules

        rules = LegalizeRules(kw["regions"], kw.get("aligns"), g.components)
        group = {"U1", "C0", "C1"}
        blocks = {r: group for r in group}
        out = _scarcity_blocks(blocks, g.components, rules, kw["edge_bands"])
        self.assertEqual(out["J2"], {"J2"})
        self.assertEqual(out["C7"], {"C7"})
        self.assertIs(out["C0"], group)
        self.assertNotIn("C6", out)  # held by nothing
        self.assertEqual(set(blocks), group)  # the input is not changed


class LookaheadTest(unittest.TestCase):
    def test_one_slot_region(self):
        # J2's window holds one slot; group caps aimed at it would take it.
        self.assertLess(legal_count(None, one_slot=True), 10)
        self.assertEqual(legal_count(dict(lookahead="regions"), one_slot=True), 10)

    def test_window_without_scarcity(self):
        self.assertEqual(legal_count(dict(lookahead="regions")), 10)
        self.assertEqual(legal_count(dict(order="scarcity", lookahead="regions")), 10)

    def test_reaches(self):
        c = part("J2", (4, 1), (6, 2))
        # Inside its centre box: reaches a slot there, not one far away.
        self.assertTrue(legal_options.reaches(c, (3, 4, 0, 1), [], (3, 5, 1, 1.3), None, 20, 10))
        self.assertFalse(legal_options.reaches(c, (15, 16, 8, 9), [], (3, 5, 1, 1.3), None, 20, 10))
        # A hard group disc bounds it too.
        self.assertFalse(
            legal_options.reaches(c, (15, 16, 0, 1), [(4, 1, 2.0)], None, None, 20, 10)
        )
        self.assertTrue(legal_options.reaches(c, (15, 16, 0, 1), [], None, None, 20, 10))


def pad_board(anchor_pos=(10.0, 5.0), member_pos=(18.0, 1.0)):
    """L2 (5 x 3 courtyard, pads 1 and 2 at x -2 / +2, the switch node pad 2 offset in y
    too) and the snubber C1 (1 x 0.5), on a 20 x 10 board."""
    pads = [Pad("1", "VIN", (-2.0, 0.0), (1.0, 2.0)), Pad("2", "SW", (2.0, 0.5), (1.0, 2.0))]
    return board(
        [
            part("L2", anchor_pos, (5.0, 3.0), pads=pads),
            part("C1", member_pos, (1.0, 0.5), pads=[Pad("1", "SW", (-0.4, 0), (0.4, 0.4))]),
        ],
        20,
        10,
    )


def pad_spec(fixed=None, **group):
    spec = dict(
        schema="v0",
        board=dict(outline=dict(w=20, h=10), default_clearance_mm=0.2),
        group=[dict(dict(members=["C1"], anchor="L2", radius_mm=1.6, hard=True), **group)],
    )
    if fixed:
        spec["fixed"] = fixed
    return spec


class AnchorPadTest(unittest.TestCase):
    def compiled(self, g, spec):
        return compile_constraints(spec, [c.ref for c in g.components])

    def test_parse(self):
        from pnr.place.geometry import hard_group_edges

        g = pad_board()
        c = self.compiled(g, pad_spec(anchor_pad=2))
        self.assertEqual(c.constraints[0].params["anchor_pad"], "2")
        self.assertEqual(hard_group_edges(c), [])
        self.assertEqual(legal_options.hard_pad_group_edges(c), [("L2", "2", "C1", 1.6)])
        # Without it the params keep their keys (byte-identical compiled constraints).
        c = self.compiled(g, pad_spec())
        self.assertEqual(sorted(c.constraints[0].params), ["anchor", "radius_mm"])
        self.assertEqual(hard_group_edges(c), [("L2", "C1", 1.6)])
        for bad in (dict(anchor_pad="2", hard=False), dict(anchor_pad=""), dict(anchor_pad=True)):
            with self.assertRaises(ConstraintError, msg=str(bad)):
                self.compiled(g, pad_spec(**bad))
        with self.assertRaises(ValueError):
            legalize_constraint_kwargs(g, self.compiled(g, pad_spec(anchor_pad="9")), {})

    def test_pad_point_turns_and_mirrors(self):
        from pnr.place.geometry import set_component_side

        g = pad_board()
        anchor = g.component("L2")
        self.assertEqual(legal_options.pad_point(anchor, "2"), (12.0, 5.5))
        anchor.rot = 90.0
        self.assertEqual(legal_options.pad_point(anchor, "2"), (9.5, 7.0))
        set_component_side(anchor, "bottom")  # pads mirror in y, as KiCad's Flip
        self.assertEqual(legal_options.pad_point(anchor, "2"), (10.5, 7.0))

    def legal(self, spec, g=None):
        from pnr.place.geometry import resolve_fixed_poses
        from pnr.place.metrics import hard_violations

        g = g or pad_board()
        c = self.compiled(g, spec)
        poses = resolve_fixed_poses(g, c)
        kw = legalize_constraint_kwargs(g, c, poses)
        placed = legalize(g, 20, 10, clearance=0.2, grid_mm=0.25, allow_rotation=True, **kw)
        return placed, c, hard_violations(placed, c)

    def test_member_at_the_pad_of_a_fixed_anchor(self):
        fixed = {"L2": dict(at=[10, 5], rot=90, side="top")}
        placed, c, bad = self.legal(pad_spec(fixed, anchor_pad="2"))
        self.assertFalse(any(bad.values()), bad)
        pad = legal_options.pad_point(placed.component("L2"), "2")
        self.assertLessEqual(math.dist(pad, placed.component("C1").pos), 1.6 + 1e-9)
        # Centre-anchored at the same radius, no slot clears L2's courtyard.
        from pnr.place.legalize import LegalizationError

        with self.assertRaises(LegalizationError):
            self.legal(pad_spec(fixed))

    def test_movable_anchor_and_movable_member(self):
        placed, c, bad = self.legal(pad_spec(anchor_pad="2"))
        self.assertFalse(any(bad.values()), bad)
        pad = legal_options.pad_point(placed.component("L2"), "2")
        self.assertLessEqual(math.dist(pad, placed.component("C1").pos), 1.6 + 1e-9)

    def test_fixed_member_bounds_the_anchor(self):
        # The reciprocal disc: the anchor's pad, at the rotation tried, near the member.
        fixed = {"C1": dict(at=[15, 6], rot=0, side="top")}
        placed, c, bad = self.legal(pad_spec(fixed, anchor_pad="2"))
        self.assertFalse(any(bad.values()), bad)
        pad = legal_options.pad_point(placed.component("L2"), "2")
        self.assertLessEqual(math.dist(pad, (15, 6)), 1.6 + 1e-9)

    def test_hard_check_and_checkers(self):
        from pnr.place.metrics import hard_violations, translation_checker

        fixed = {"L2": dict(at=[10, 5], rot=0, side="top")}
        g = pad_board(member_pos=(13.5, 5.5))  # 1.5 mm from pad 2, 3.54 mm from the centre
        c = self.compiled(g, pad_spec(fixed, anchor_pad="2"))
        self.assertEqual(hard_violations(g, c)["group_outside"], [])
        self.assertEqual(legal_options.pad_group_offenders(g, c), [])
        check = translation_checker(g, c)
        member = g.component("C1")
        member.pos = (13.4, 5.0)  # 1.49 mm from the pad
        self.assertTrue(check(member))
        member.pos = (16.0, 5.5)  # 4 mm from the pad
        self.assertFalse(check(member))
        self.assertEqual(hard_violations(g, c)["group_outside"], ["C1"])
        # The same board centre-anchored: 3.54 mm from L2's centre is outside 1.6 mm.
        member.pos = (13.5, 5.5)
        self.assertEqual(
            hard_violations(g, self.compiled(g, pad_spec(fixed)))["group_outside"], ["C1"]
        )


class PlacerTest(unittest.TestCase):
    def test_placer_passes_the_option(self):
        pads = [Pad("1", "A", (-0.5, 0), (0.5, 0.5)), Pad("2", "B", (0.5, 0), (0.5, 0.5))]
        parts = [part("R%d" % i, (W / 2, H / 2), (1.8, 1.0), pads=pads) for i in range(6)]
        g = board(parts)
        c = compile_constraints(doc(legalize=dict(outline="exact")), [p.ref for p in parts])
        kw = legalize_constraint_kwargs(g, c, {})
        self.assertEqual(kw["outline"], "exact")
        for ref, (dx, dy) in zip([p.ref for p in parts], itertools.product((-1, 1), (-1, 0, 1))):
            g.component(ref).pos = (W / 2 + dx * 40, H / 2 + dy * 40)
        placed = legalize(g, W, H, clearance=0.2, grid_mm=0.25, **kw)
        self.assertEqual(outside_outline(placed, W, H), [])


if __name__ == "__main__":
    unittest.main()
