"""Double-sided placement: global placement, legalization, detail moves and search
under the board's side policy (pnr.place.sides)."""

import importlib.util
import math
import unittest
from unittest import mock

from pnr.constraints import compile_constraints
from pnr.graph import BoardGraph, BoardOutline, Component, Net, Pad
from pnr.place import sides as S
from pnr.place.geometry import courtyard_rect, set_component_side
from pnr.place.legalize import LegalizationError, legalize
from pnr.place.metrics import hard_violations, hpwl, pose_checker
from pnr.place.placer import place


def smd(ref, nets, size=(2.0, 1.2), pos=(0.0, 0.0), pitch=1.0, rows=1):
    """A surface part: ``nets`` on one row (or two facing rows) of pads."""
    per_row = math.ceil(len(nets) / rows)
    pads = []
    for i, net in enumerate(nets):
        row, col = divmod(i, per_row)
        x = (col - (per_row - 1) / 2) * pitch
        y = 0.0 if rows == 1 else (size[1] / 2 - 0.5) * (1 if row == 0 else -1)
        pads.append(Pad(str(i + 1), net, (x, y + 0.1), (0.5, 0.5)))
    return Component(ref, "smd:" + ref, pos, 0, "top", size, size, pads=pads)


def th(ref, nets, pos=(0.0, 0.0)):
    pads = [Pad(str(i + 1), n, (i * 2.54, 0.0), (1.7, 1.7), True) for i, n in enumerate(nets)]
    return Component(ref, "th:" + ref, pos, 0, "top", (2.54 * len(nets) + 1, 3), (5, 3), pads=pads)


def board(*parts, outline=(20, 14)):
    nets = {}
    for comp in parts:
        for pad in comp.pads:
            if pad.net:
                nets.setdefault(pad.net, []).append((comp.ref, pad.name))
    return BoardGraph(
        "t",
        list(parts),
        [Net(n, i + 1, pins) for i, (n, pins) in enumerate(sorted(nets.items()))],
        BoardOutline(*outline),
    )


def rules(graph, policy="double", outline=(20, 14), layers=2, **extra):
    doc = dict(board=dict(outline=dict(w=outline[0], h=outline[1]), layers=layers, sides=policy))
    doc.update(extra)
    return compile_constraints(doc, graph.refs)


def decoupled():
    """A fixed IC whose supply pins sit on opposite rows (as a SOIC's do), a drilled
    connector and a free decoupling cap; the supply nets are planes (DECOUPLED), so a
    side change costs no layer change and only the wirelength decides."""
    ic = smd("U1", ["A", "B", "C", "VCC", "GND", "D", "E", "F"], (6, 4), pitch=1.27, rows=2)
    return board(ic, th("J1", ["A", "B"]), smd("C1", ["VCC", "GND"]))


DECOUPLED_FIXED = dict(
    layers=4,
    fixed={"U1": dict(at=[10, 7], rot=0, side="top"), "J1": dict(at=[3, 3], rot=0, side="top")},
    net_class=dict(supply=dict(nets=["VCC", "GND"], plane_layer="In1.Cu")),
)


class SinglePolicyTest(unittest.TestCase):
    def test_single_policy_never_enters_the_side_path(self):
        g = decoupled()
        c = rules(g, "single", **DECOUPLED_FIXED)
        with mock.patch("pnr.place.model._side_terms", side_effect=AssertionError), mock.patch(
            "pnr.place.detail_moves.improve", side_effect=AssertionError
        ):
            placed, report = place(g, c, seed=0, iters=120)
        self.assertTrue(report.legal, report.summary())
        self.assertEqual({p.side for p in placed.components}, {"top"})

    def test_single_and_unset_policy_place_identically(self):
        g = decoupled()
        doc = {k: v for k, v in DECOUPLED_FIXED.items() if k != "layers"}
        doc["board"] = dict(outline=dict(w=20, h=14), layers=4)
        unset, _ = place(g, compile_constraints(doc, g.refs), seed=3, iters=120)
        single, _ = place(g, rules(g, "single", **DECOUPLED_FIXED), seed=3, iters=120)
        self.assertEqual(unset.to_json(), single.to_json())


class DoublePolicyTest(unittest.TestCase):
    def test_board_too_small_for_one_side_becomes_legal(self):
        a, b = smd("U1", ["A", "B"], (6, 4)), smd("U2", ["A", "B"], (6, 4))
        g = board(a, b, outline=(7, 5))
        with self.assertRaises(LegalizationError):
            place(g, rules(g, "single", outline=(7, 5)), seed=0, iters=80)
        placed, report = place(g, rules(g, "double", outline=(7, 5)), seed=0, iters=80)
        self.assertTrue(report.legal, report.summary())
        self.assertEqual(sorted(c.side for c in placed.components), ["bottom", "top"])

    def test_capacitor_goes_under_its_ic(self):
        g = decoupled()
        single, _ = place(g, rules(g, "single", **DECOUPLED_FIXED), seed=0, iters=200)
        double, report = place(g, rules(g, "double", **DECOUPLED_FIXED), seed=0, iters=200)
        self.assertTrue(report.legal, report.summary())
        cap, ic = double.component("C1"), double.component("U1")
        self.assertEqual(cap.side, "bottom")
        under = courtyard_rect(ic)
        self.assertTrue(
            under.left <= cap.pos[0] <= under.right and under.bottom <= cap.pos[1] <= under.top
        )
        self.assertEqual(single.component("C1").side, "top")
        self.assertLess(hpwl(double), hpwl(single))

    def test_hard_and_fixed_sides_are_respected(self):
        g = decoupled()
        c = rules(g, "double", side=dict(bottom=["C1"]), **DECOUPLED_FIXED)
        plan = S.plan(g, c)
        self.assertEqual(plan.free, ())
        placed, report = place(g, c, seed=1, iters=120)
        self.assertTrue(report.legal, report.summary())
        self.assertEqual(placed.component("C1").side, "bottom")
        self.assertEqual(placed.component("U1").side, "top")
        self.assertEqual(placed.component("J1").side, "top")

    def test_held_part_flipped_is_an_error(self):
        g = decoupled()
        plan = S.plan(g, rules(g, "double", **DECOUPLED_FIXED))
        set_component_side(g.component("U1"), "bottom")
        with self.assertRaises(ValueError):
            S.check_held(g, plan)

    def test_deterministic(self):
        g = decoupled()
        c = rules(g, "double", **DECOUPLED_FIXED)
        a, _ = place(g, c, seed=4, iters=120)
        b, _ = place(g, c, seed=4, iters=120)
        self.assertEqual(a.to_json(), b.to_json())

    def test_edge_align_side_puts_the_part_there(self):
        g = decoupled()
        edge = dict(edge_align=dict(C1=dict(edge="west", side="bottom")))
        placed, report = place(g, rules(g, "double", **DECOUPLED_FIXED, **edge), seed=0, iters=120)
        self.assertTrue(report.legal, report.summary())
        self.assertEqual(placed.component("C1").side, "bottom")
        single, _ = place(g, rules(g, "single", **DECOUPLED_FIXED, **edge), seed=0, iters=120)
        self.assertEqual(single.component("C1").side, "top")

    def test_side_pref_moves_nothing_on_a_single_sided_board(self):
        g = decoupled()
        pref = dict(side_pref=dict(bottom=["C1"]))
        with_pref, _ = place(g, rules(g, "single", **DECOUPLED_FIXED, **pref), seed=2, iters=120)
        without, _ = place(g, rules(g, "single", **DECOUPLED_FIXED), seed=2, iters=120)
        self.assertEqual(with_pref.to_json(), without.to_json())

    def test_position_only_placement_turns_nothing(self):
        g = decoupled()
        c = rules(g, "double", **DECOUPLED_FIXED)
        placed, report = place(g, c, seed=0, iters=150, orient=False)
        self.assertTrue(report.legal, report.summary())
        for comp in placed.components:
            self.assertEqual(comp.rot, g.component(comp.ref).rot, comp.ref)


class LegalizerTest(unittest.TestCase):
    def test_falls_back_to_the_other_side(self):
        a, b = smd("U1", ["A"], (6, 4), pos=(3.5, 2.5)), smd("U2", ["A"], (6, 4), pos=(3.5, 2.5))
        g = board(a, b, outline=(7, 5))
        with self.assertRaises(LegalizationError):
            legalize(g, 7, 5, fixed={}, keepouts=[], grid_mm=0.25)
        placed = legalize(
            g, 7, 5, fixed={}, keepouts=[], grid_mm=0.25, side_options={"U2": ("top", "bottom")}
        )
        self.assertEqual(placed.component("U1").side, "top")
        self.assertEqual(placed.component("U2").side, "bottom")
        self.assertEqual(placed.component("U2").pads[0].offset[1], -0.1)  # mirrored


def twin_ics(policy="double", **extra):
    """Two eight-pin ICs wired pin for pin, so a back-to-back stack would shorten
    every net, and a two-pin capacitor on one of their nets."""
    nets = ["N%d" % i for i in range(8)]
    u1 = smd("U1", nets, (6, 4), (8, 7), pitch=1.27, rows=2)
    u2 = smd("U2", nets, (6, 4), (8, 7), pitch=1.27, rows=2)
    g = board(u1, u2, smd("C1", ["N0", "N7"], pos=(14, 12)), outline=(24, 14))
    return g, rules(g, policy, outline=(24, 14), **extra)


class StackingTest(unittest.TestCase):
    """Two parts that fan out never stack back to back while a part is side-free."""

    def test_rule_covers_parts_that_fan_out_under_a_side_free_plan(self):
        g, c = twin_ics()
        self.assertEqual(S.stack_refs(g, c), frozenset({"U1", "U2"}))
        g1, single = twin_ics("single")
        self.assertEqual(S.stack_refs(g1, single), frozenset())
        self.assertFalse(S.fans_out(th("J1", ["A", "B", "C"])))

    def test_back_to_back_ics_are_an_overlap_but_a_capacitor_under_one_is_not(self):
        g, c = twin_ics()
        set_component_side(g.component("U2"), "bottom")
        self.assertEqual(hard_violations(g, c)["overlaps"], [("U1", "U2")])
        g1, single = twin_ics("single")
        set_component_side(g1.component("U2"), "bottom")
        self.assertEqual(hard_violations(g1, single)["overlaps"], [])
        g2, c2 = twin_ics()
        g2.component("U2").pos = (18, 7)
        set_component_side(g2.component("C1"), "bottom")
        g2.component("C1").pos = (8, 7)
        self.assertEqual(hard_violations(g2, c2)["overlaps"], [])
        # Two fixed parts stacked by the designer stay legal.
        g3, _ = twin_ics()
        set_component_side(g3.component("U2"), "bottom")
        fixed = dict(
            U1=dict(at=[8, 7], rot=0, side="top"), U2=dict(at=[8, 7], rot=0, side="bottom")
        )
        self.assertEqual(
            hard_violations(g3, rules(g3, "double", outline=(24, 14), fixed=fixed))["overlaps"], []
        )

    def test_legalizer_keeps_two_ics_apart_across_sides(self):
        g, c = twin_ics()
        set_component_side(g.component("U2"), "bottom")
        kw = dict(fixed={}, keepouts=[], grid_mm=0.25, clearance=0.2)
        stacked = legalize(g, 24, 14, **kw)
        self.assertEqual(stacked.component("U1").pos, stacked.component("U2").pos)
        apart = legalize(g, 24, 14, stack=S.stack_refs(g, c), **kw)
        self.assertFalse(
            courtyard_rect(apart.component("U1")).overlaps(courtyard_rect(apart.component("U2")))
        )
        self.assertFalse(any(hard_violations(apart, c).values()))

    def test_pose_checker_rejects_a_flip_under_another_ic(self):
        g, c = twin_ics()
        g.component("U2").pos = (18, 7)
        legal = pose_checker(g, c)
        u2 = g.component("U2")
        set_component_side(u2, "bottom")
        u2.pos = (8, 7)
        self.assertFalse(legal([u2]))
        cap = g.component("C1")
        set_component_side(cap, "bottom")
        cap.pos = (8, 7)
        self.assertTrue(legal([cap]))

    def test_placement_never_stacks_two_ics(self):
        from pnr.place.metrics import stack_pairs

        # Plane nets: a side change costs no layer change, so only the rule keeps the
        # ICs from stacking (without it every seed below stacks them).
        nets = ["N%d" % i for i in range(8)]
        g, c = twin_ics(layers=4, net_class=dict(p=dict(nets=nets, plane_layer="In1.Cu")))
        for seed in (0, 1, 2):
            placed, report = place(g, c, seed=seed, iters=200)
            self.assertTrue(report.legal, report.summary())
            self.assertEqual(stack_pairs(placed, {"U1", "U2"}), [])

    def test_a_board_too_small_for_two_ics_on_one_side_stays_illegal(self):
        a = smd("U1", ["A", "B", "C"], (6, 4))
        b = smd("U2", ["A", "B", "C"], (6, 4))
        g = board(a, b, outline=(7, 5))
        with self.assertRaises(LegalizationError):
            place(g, rules(g, "double", outline=(7, 5)), seed=0, iters=80)


class DetailMovesTest(unittest.TestCase):
    def crossed(self):
        anchors = [
            smd("AL", ["X"], (1, 1), (1.5, 8.5)),
            smd("BL", ["Y"], (1, 1), (1.5, 1.5)),
            smd("AR", ["X2"], (1, 1), (18.5, 8.5)),
            smd("BR", ["Y2"], (1, 1), (18.5, 1.5)),
        ]
        p1 = smd("P1", ["X", "X2"], (2, 1.2), (10, 2.5))
        p2 = smd("P2", ["Y", "Y2"], (2, 1.2), (10, 7.5))
        g = board(*anchors, p1, p2, outline=(20, 10))
        fixed = {
            a.ref: dict(at=list(a.pos), rot=0, side="top") for a in g.components if len(a.pads) == 1
        }
        return g, rules(g, "double", outline=(20, 10), fixed=fixed)

    def test_swap_fixes_a_crossing(self):
        from pnr.place.detail_moves import LAST_STATS, improve

        g, c = self.crossed()
        before = hpwl(g)
        out = improve(g, c, S.plan(g, c), seed=0)
        self.assertGreater(out.component("P1").pos[1], out.component("P2").pos[1])
        self.assertLess(hpwl(out), before - 10)
        self.assertFalse(any(hard_violations(out, c).values()))
        self.assertGreaterEqual(LAST_STATS["swaps"], 1)
        # Flipping a part away from its top-side anchors only adds layer changes.
        self.assertEqual({p.side for p in out.components}, {"top"})

    def test_deterministic(self):
        from pnr.place.detail_moves import improve

        g, c = self.crossed()
        self.assertEqual(
            improve(g, c, S.plan(g, c), seed=5).to_json(),
            improve(g, c, S.plan(g, c), seed=5).to_json(),
        )

    def test_no_part_turns_without_rotation(self):
        from pnr.place.detail_moves import improve

        g, c = self.crossed()
        for comp in g.components:
            comp.rot = 90.0 if comp.ref in ("P1", "P2") else 0.0
        out = improve(g, c, S.plan(g, c), seed=0, allow_rotation=False)
        self.assertLess(hpwl(out), hpwl(g) - 10)  # the swap still happens
        self.assertEqual({out.component(r).rot for r in ("P1", "P2")}, {90.0})
        turned = improve(g, c, S.plan(g, c), seed=0)
        self.assertNotEqual({turned.component(r).rot for r in ("P1", "P2")}, {90.0})

    def test_inflation_grows_the_moved_part(self):
        g, c = self.crossed()
        p1 = g.component("P1")
        plain = pose_checker(g, c, clearance=0.2)
        grown = pose_checker(g, c, clearance=0.2, inflation={"P1": 3.0})
        p1.pos = (10.0, 5.5)  # 0.8 mm from P2 (at 7.5); grown 3x, 1.2 mm into it
        self.assertTrue(plain([p1]))
        self.assertFalse(grown([p1]))

    def test_soft_edge_and_group_terms_are_costed(self):
        from pnr.place.detail_moves import _Cost

        g, c = self.crossed()
        soft = rules(
            g,
            "double",
            outline=(20, 10),
            edge_align=dict(P1=dict(edge="south", weight=2.0)),
            group=[dict(members=["P1", "P2"], anchor="P2", radius_mm=1.0, weight=3.0)],
        )
        plain = _Cost(g, S.plan(g, c), c)
        cost = _Cost(g, S.plan(g, soft), soft)
        p1 = g.component("P1")
        # P1's courtyard 1.9 mm above the south edge; 5 mm from P2 (radius 1).
        extra = 2.0 * 1.9**2 + 3.0 * (5.0 - 1.0) ** 2
        self.assertAlmostEqual(cost.local([p1]) - plain.local([p1]), extra, places=6)

    def test_a_move_that_adds_channel_shortage_is_not_made(self):
        from pnr.place.detail_moves import LAST_STATS, improve

        g, c = self.crossed()
        home = {comp.ref: comp.pos for comp in g.components}

        class AwayIsCrowded:
            """A channel model under which a part away from its slot is short of room."""

            def penalty(self, comp, others, xs, ys):
                return 0.0 if comp.pos == home[comp.ref] else 100.0

        free = improve(g, c, S.plan(g, c), seed=0)
        self.assertLess(hpwl(free), hpwl(g) - 10)
        vetoed = improve(g, c, S.plan(g, c), seed=0, channel_model=AwayIsCrowded())
        self.assertEqual({p.ref: p.pos for p in vetoed.components}, home)
        self.assertGreater(LAST_STATS["channel_vetoes"], 0)

    def test_channel_reach_bounds_every_demand(self):
        import itertools

        from pnr.constraints import compile_routing_rules
        from pnr.place.channels import ChannelModel

        nets = ["D+", "D-", "VCC", "GND", "A", "B", "C", "E"]
        u1 = smd("U1", nets, (6, 4), (6, 7), pitch=1.27, rows=2)
        u2 = smd("U2", nets[::-1], (6, 4), (13, 7), pitch=1.27, rows=2)
        r1 = smd("R1", ["A", "B"], pos=(9.5, 3))
        g = board(u1, u2, r1, outline=(20, 14))
        c = rules(
            g,
            outline=(20, 14),
            layers=4,
            diff_pair=[dict(name="usb", p="D+", n="D-", width_mm=0.3, gap_mm=0.2)],
            net_class=dict(
                pwr=dict(nets=["VCC", "GND"], plane_layer="In1.Cu", clearance_mm=0.3),
                wide=dict(nets=["A"], width_mm=0.5, clearance_mm=0.25),
            ),
        )
        model = ChannelModel(g, compile_routing_rules(c, nets))
        reach = {x.ref: model.reach({p.net for p in x.pads if p.net}) for x in g.components}
        for a, b in itertools.permutations(g.components, 2):
            for _, gap, _, required, _ in model.interactions(a, b):
                self.assertLessEqual(float(required), reach[a.ref] + reach[b.ref] + 1e-9)

    def test_a_result_the_full_check_rejects_is_reverted(self):
        from pnr.place import detail_moves
        from pnr.place.detail_moves import LAST_STATS, improve

        g, c = self.crossed()
        with mock.patch.object(
            detail_moves, "hard_violations", return_value=dict(region_outside=["P1"])
        ):
            out = improve(g, c, S.plan(g, c), seed=0)
        self.assertTrue(LAST_STATS["reverted"])
        self.assertEqual(
            [(p.pos, float(p.rot), p.side) for p in out.components],
            [(p.pos, float(p.rot), p.side) for p in g.components],
        )

    def test_flip_rejected_into_drilled_bodies_and_keepouts(self):
        j1 = th("J1", ["A", "B"], (5, 5))
        u1 = smd("U1", ["A", "C"], (4, 3), (12, 5))
        c1 = smd("C1", ["B", "C"], (2, 1.2), (5, 10))
        g = board(j1, u1, c1, outline=(24, 14))
        c = rules(
            g,
            "double",
            outline=(24, 14),
            keepout=[dict(name="k", polygon=[[17, 3], [21, 3], [21, 7], [17, 7]])],
        )
        legal = pose_checker(g, c)
        cap = g.component("C1")
        set_component_side(cap, "bottom")
        for at, ok in (((6.2, 5), False), ((12, 5), True), ((19, 5), False), ((5, 10), True)):
            cap.pos = at
            self.assertEqual(legal([cap]), ok, at)


class SearchTest(unittest.TestCase):
    def test_pool_starts_carry_sides_only_when_parts_are_free(self):
        from pnr.place.initial_pool import InitialPoolConfig, initial_starts

        g = decoupled()
        single = initial_starts(g, rules(g, "single", **DECOUPLED_FIXED), InitialPoolConfig(), 7)
        double = initial_starts(g, rules(g, "double", **DECOUPLED_FIXED), InitialPoolConfig(), 7)
        self.assertFalse(any("sides" in s for s in single))
        self.assertNotIn("sides", double[0])
        self.assertNotIn("sides", double[1])
        self.assertTrue(all(set(s["sides"]) == {"C1"} for s in double[2:]))
        under = [s for s in double if s["kind"] == "under-body-sides"]
        self.assertEqual(len(under), 1)
        self.assertEqual(under[0]["sides"], {"C1": "bottom"})
        self.assertEqual(under[0]["positions"]["C1"], [10, 7])
        # Positions and rotations of every other start are the single-sided ones.
        for a, b in zip(single, double):
            if b["kind"] != "under-body-sides":
                self.assertEqual({k: v for k, v in b.items() if k != "sides"}, a)

    def test_source_check_accepts_free_flips_only(self):
        from pnr.place.initial_pool import _hard_and_source_errors

        g = decoupled()
        c = rules(g, "double", **DECOUPLED_FIXED)
        placed, _ = place(g, c, seed=0, iters=150)
        flipped = BoardGraph.from_json(placed.to_json())
        cap = flipped.component("C1")
        set_component_side(cap, "top" if cap.side == "bottom" else "bottom")
        cap.pos = (17.0, 11.5)
        self.assertEqual(_hard_and_source_errors(flipped, g, c), {})
        set_component_side(flipped.component("J1"), "bottom")
        errors = _hard_and_source_errors(flipped, g, c)
        self.assertEqual(errors["source_geometry_changed"], ["J1"])
        set_component_side(flipped.component("J1"), "top")
        flipped.component("C1").pads[0].offset = (3.0, 3.0)
        self.assertEqual(_hard_and_source_errors(flipped, g, c)["source_geometry_changed"], ["C1"])
        flipped = BoardGraph.from_json(placed.to_json())
        set_component_side(flipped.component("C1"), "bottom" if cap.side == "top" else "top")
        single = rules(g, "single", **DECOUPLED_FIXED)
        self.assertIn("source_geometry_changed", _hard_and_source_errors(flipped, g, single))
        # The routing rules hold a plane-access part, as the placer does.
        intents = dict(plane_access_intents=[dict(ref="C1", kind="via_array")])
        self.assertEqual(_hard_and_source_errors(flipped, g, c), {})
        self.assertEqual(
            _hard_and_source_errors(flipped, g, c, intents)["source_geometry_changed"], ["C1"]
        )

    def test_shortlist_keeps_one_other_side_candidate(self):
        from pnr.place.initial_pool import keep_other_side

        g = decoupled()
        plan = S.plan(g, rules(g, "double", **DECOUPLED_FIXED))
        top = BoardGraph.from_json(g.to_json())
        under = BoardGraph.from_json(g.to_json())
        set_component_side(under.component("C1"), "bottom")
        pool = [
            dict(id="a", graph=top, cost=1.0),
            dict(id="b", graph=under, cost=3.0),
            dict(id="c", graph=under, cost=2.0),
        ]
        self.assertEqual(keep_other_side(pool, ["a"], "cost", plan), ["a", "c"])
        self.assertEqual(keep_other_side(pool, ["b"], "cost", plan), ["b"])
        single = S.plan(g, rules(g, "single", **DECOUPLED_FIXED))
        self.assertEqual(keep_other_side(pool, ["a"], "cost", single), ["a"])

    def test_relocation_flip_is_legal(self):
        from pnr.place.relocate import propose

        # A on top beside B on the bottom, B's top side taken by an unrelated body:
        # on B's side A needs no layer change, on top it always needs one.
        a = smd("A", ["n"], (1, 1), (6, 3))
        b = smd("B", ["n"], (1, 1), (3, 3))
        cover = smd("K", [], (4, 4), (3, 3))
        set_component_side(b, "bottom")
        g = board(a, b, cover, outline=(30, 20))
        c = rules(
            g,
            "double",
            outline=(30, 20),
            fixed=dict(B=dict(at=[3, 3], side="bottom"), K=dict(at=[3, 3], side="top")),
        )
        result = propose(g, c, {}, [], pitch=1.0, max_parts=1)
        self.assertIsNotNone(result)
        new, report, event = result
        self.assertTrue(report.legal)
        self.assertEqual(event["moves"][0]["side"], "bottom")
        self.assertEqual(new.component("A").side, "bottom")
        self.assertFalse(any(hard_violations(new, c).values()))
        self.assertEqual(g.component("A").side, "top")

    @unittest.skipUnless(
        importlib.util.find_spec("pnr.mc") is not None, "pnr.mc is not in this build"
    )
    def test_mc_worker_places_with_start_sides(self):
        import json
        import tempfile
        from pathlib import Path

        import yaml

        from pnr.mc.halving import _place_impl

        g = decoupled()
        doc = {k: v for k, v in DECOUPLED_FIXED.items() if k != "layers"}
        doc["board"] = dict(outline=dict(w=20, h=14), layers=4, sides="double")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "graph.json").write_text(g.to_json())
            (root / "rules.json").write_text(json.dumps({}))
            (root / "constraints.yaml").write_text(yaml.safe_dump(doc))
            start = dict(
                id="p002",
                kind="stratified-global",
                seed=11,
                positions={"C1": [10, 7]},
                rotations={"C1": 0},
                sides={"C1": "bottom"},
            )
            record = _place_impl(
                root, root / "constraints.yaml", start, root / "cand", 120, None, False
            )
        self.assertEqual(record["status"], "legal", record)
        self.assertEqual(record["poses"]["C1"][3], "bottom")


class RoutingTest(unittest.TestCase):
    def test_bottom_pads_route_on_the_bottom_layer(self):
        from pnr.constraints import compile_routing_rules
        from pnr.route.detail.router import route_board

        a = smd("U1", ["S", "T"], (3, 2), (5, 5))
        b = smd("U2", ["S", "T"], (3, 2), (15, 5))
        g = board(a, b, outline=(20, 10))
        for comp in g.components:
            set_component_side(comp, "bottom")
        c = rules(g, "double", outline=(20, 10))
        routed = route_board(g, c, compile_routing_rules(c, ["S", "T"]), pitch=0.25, max_iters=4)
        self.assertEqual(routed.result.unrouted, [])
        self.assertTrue(routed.tracks)
        self.assertEqual({t[1] for t in routed.tracks}, {"B.Cu"})
        self.assertEqual(routed.vias, [])


if __name__ == "__main__":
    unittest.main()
