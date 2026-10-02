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
