"""Route-then-compact (PNR_ROUTE_COMPACT, pnr.place.route_compact): the order-preserving 1D
compaction (gaps by routed copper, order, fixed parts, keep-outs, rigid line groups, grid),
the hull shapes it slides by, the back-off of the route loop, and the flags."""

import os
import unittest
from unittest import mock

from pnr.constraints import compile_constraints
from pnr.graph import BoardGraph, Component, Pad
from pnr.place import route_compact as rc
from pnr.place.geometry import courtyard_rect
from pnr.place.metrics import hard_violations

W, H = 60.0, 40.0
CL = 0.2


def part(ref, x, y, w=2.0, h=1.0, nets=("A", "B")):
    pads = [
        Pad(name="1", net=nets[0], offset=(-w / 4, 0.0), size=(0.5, 0.5)),
        Pad(name="2", net=nets[1], offset=(w / 4, 0.0), size=(0.5, 0.5)),
    ]
    return Component(
        ref=ref,
        footprint="R",
        pos=(x, y),
        rot=0.0,
        side="top",
        courtyard=(w, h),
        bbox=(w, h),
        pads=pads,
        smd_body=True,
    )


def board(parts, nets=()):
    g = BoardGraph(name="t", components=list(parts))
    g.nets = list(nets)
    return g


def compiled(graph, **doc):
    base = {"board": {"default_clearance_mm": CL, "layers": 2, "outline": {"w": W, "h": H}}}
    base.update(doc)
    return compile_constraints(base, graph.refs)


def items(graph, constraints, **kw):
    return rc.graph_items(graph, constraints, **kw)


def xs(graph):
    return {c.ref: c.pos[0] for c in graph.components}


def plan_and_apply(graph, constraints, axis=0, copper=None, **kw):
    its = items(graph, constraints)
    kw.setdefault("min_gap", CL)
    plan = rc.plan_axis(its, axis, copper, **kw)
    return plan, rc.apply(graph, its, plan)


class FlagTest(unittest.TestCase):
    def test_off_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(rc.enabled())
            self.assertFalse(rc.enabled("TOP"))

    def test_parts(self):
        with mock.patch.dict(os.environ, {"PNR_ROUTE_COMPACT": "1"}, clear=True):
            self.assertTrue(all(rc.enabled(p) for p in rc.PARTS))
        with mock.patch.dict(os.environ, {"PNR_ROUTE_COMPACT": "top,block"}, clear=True):
            self.assertTrue(rc.enabled("TOP") and rc.enabled("BLOCK"))
            self.assertFalse(rc.enabled("FLAT"))
        env = {"PNR_ROUTE_COMPACT": "1", "PNR_ROUTE_COMPACT_FLAT": "0"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertFalse(rc.enabled("FLAT"))
            self.assertTrue(rc.enabled("TOP"))
        with mock.patch.dict(os.environ, {"PNR_ROUTE_COMPACT": "TOP,BOGUS"}, clear=True):
            with self.assertRaises(ValueError):
                rc.enabled()


class AxisTest(unittest.TestCase):
    def test_two_parts_close_on_the_centre(self):
        g = board([part("R1", 10, 20), part("R2", 40, 20)])
        c = compiled(g)
        plan, out = plan_and_apply(g, c)
        r1, r2 = courtyard_rect(out.component("R1")), courtyard_rect(out.component("R2"))
        gap = r2.left - r1.right
        self.assertGreaterEqual(gap, CL - 1e-9)
        self.assertLess(gap, CL + 0.25 + 1e-9)  # within one grid step of the clearance
        # Both moved toward the centre of the span they had (25 mm).
        self.assertGreater(out.component("R1").pos[0], 10)
        self.assertLess(out.component("R2").pos[0], 40)
        # Grid multiples.
        for d in plan.delta:
            self.assertAlmostEqual(d / 0.25, round(d / 0.25), places=6)
        self.assertEqual(hard_violations(out, c)["overlaps"], [])

    def test_gap_holds_the_routed_lanes(self):
        g = board([part("R1", 10, 20), part("R2", 40, 20)])
        c = compiled(g)
        # Three vertical tracks along the gutter, 0.25 mm wide, 1 mm apart, plus one track
        # crossing it (pad to pad), which needs no width.
        tracks = [["N%d" % k, "F.Cu", (20 + k, 15), (20 + k, 25), 0.25] for k in range(3)]
        tracks.append(["X", "F.Cu", (11, 20), (39, 20), 0.25])
        copper = rc.Copper.from_routes(tracks, [], 0.6, 0.2)
        plan, out = plan_and_apply(g, c, copper=copper, slack=0.25)
        lanes = 3 * (0.25 + 0.2)  # each track plus half a clearance either side
        (gutter,) = plan.gutters
        self.assertAlmostEqual(gutter["need"], lanes, places=6)
        r1, r2 = courtyard_rect(out.component("R1")), courtyard_rect(out.component("R2"))
        self.assertGreaterEqual(r2.left - r1.right, lanes + 0.25 - 1e-9)
        self.assertLess(r2.left - r1.right, lanes + 0.25 + 0.25 + 1e-9)

    def test_lanes_union_per_layer(self):
        copper = rc.Copper.from_routes(
            [
                ["A", "F.Cu", (5, 0), (5, 10), 0.2],
                ["B", "B.Cu", (5, 0), (5, 10), 0.2],  # same lane, other layer
                ["C", "F.Cu", (5.1, 0), (5.1, 10), 0.2],  # overlaps A's lane
            ],
            [["V", 7, 5]],
            0.6,
            0.2,
        )
        meter = rc.GutterMeter(copper, 0)
        need = meter.need(0, 10, 0, 10)
        # F.Cu: A+C union 0.5 mm, plus the via 0.8 mm (on every layer).
        self.assertAlmostEqual(need, 0.5 + 0.8, places=6)

    def test_order_is_kept_across_rows(self):
        # Two rows; the second row's part sits right of the first row's first part and
        # must stay right of it even though they never face each other.
        g = board([part("A", 5, 30), part("B", 50, 30), part("C", 20, 5), part("D", 45, 5)])
        c = compiled(g)
        plan, out = plan_and_apply(g, c)
        before = sorted(g.components, key=lambda q: q.pos[0])
        after = {q.ref: q.pos[0] for q in out.components}
        order = [q.ref for q in before]
        self.assertEqual(order, sorted(order, key=lambda r: (after[r], order.index(r))))
        self.assertTrue(rc.check_plan(items(g, c), plan))

    def test_fixed_part_holds(self):
        g = board([part("J1", 5, 20), part("R1", 30, 20), part("R2", 50, 20)])
        c = compiled(g, fixed={"J1": {"at": [5, 20], "rot": 0}})
        _plan, out = plan_and_apply(g, c)
        self.assertEqual(out.component("J1").pos, (5.0, 20.0))
        self.assertLess(out.component("R2").pos[0], 50)
        self.assertEqual(hard_violations(out, c)["fixed_misplaced"], [])

    def test_keepout_is_an_obstacle(self):
        g = board([part("R1", 10, 20), part("R2", 50, 20)])
        c = compiled(
            g, keepout=[{"name": "k", "polygon": [[28, 10], [32, 10], [32, 30], [28, 30]]}]
        )
        _plan, out = plan_and_apply(g, c)
        r1, r2 = courtyard_rect(out.component("R1")), courtyard_rect(out.component("R2"))
        self.assertLessEqual(r1.right, 28 - CL + 1e-9)
        self.assertGreaterEqual(r2.left, 32 + CL - 1e-9)

    def test_line_group_moves_rigidly(self):
        g = board([part("R1", 10, 20), part("R2", 10, 23), part("R3", 45, 21)])
        c = compiled(g, line_group=[{"name": "l", "members": ["R1", "R2"], "pitch_mm": 3}])
        its = items(g, c)
        self.assertIn(("R1", "R2"), [it.refs for it in its])
        _plan, out = plan_and_apply(g, c)
        d1 = out.component("R1").pos[0] - 10
        d2 = out.component("R2").pos[0] - 10
        self.assertAlmostEqual(d1, d2)
        self.assertGreater(d1, 0)

    def test_nothing_to_close(self):
        g = board([part("R1", 10, 20), part("R2", 10 + 2 + CL, 20)])
        c = compiled(g)
        plan, _ = plan_and_apply(g, c)
        self.assertEqual(plan.moved, 0)

    def test_other_sides_pass(self):
        # A bottom-side part under a top-side part: no gutter between them.
        bottom = part("R2", 12, 20)
        bottom.side = "bottom"
        g = board([part("R1", 10, 20), bottom, part("R3", 40, 20)])
        c = compiled(g)
        plan, out = plan_and_apply(g, c)
        self.assertEqual(hard_violations(out, c)["overlaps"], [])


class HullTest(unittest.TestCase):
    def test_dovetail_slides_into_the_notch(self):
        # An L-shaped body (a 10 x 10 square with its upper-right 6 x 5 quadrant free) and a
        # 4 x 4 block level with that notch: by rectangles the block stops at the L's
        # right edge; by shape it slides into the notch.
        l_shapes = [("top", 0, 0, 10, 5), ("top", 0, 5, 4, 10)]
        block = [("top", 30, 5.5, 34, 9.5)]
        its = [
            rc.make_item("L", (), l_shapes),
            rc.make_item("B", (), block),
        ]
        plan = rc.plan_axis(its, 0, None, min_gap=CL, grid=0.25)
        left_of_block = its[1].pos[0] + plan.delta[1] - 2.0
        right_of_l_stem = its[0].pos[0] + plan.delta[0] + 4 - 5.0  # stem right edge
        self.assertLess(left_of_block, its[0].pos[0] + plan.delta[0] + 5.0)  # inside the L box
        self.assertGreaterEqual(left_of_block - right_of_l_stem, CL - 1e-9)
        boxes = [rc.make_item("L", (), [("top", 0, 0, 10, 10)]), rc.make_item("B", (), block)]
        plan_box = rc.plan_axis(boxes, 0, None, min_gap=CL, grid=0.25)
        span_shape = plan.span_after
        self.assertLess(span_shape, plan_box.span_after - 3.0)

    def test_planes_do_not_block_each_other(self):
        its = [
            rc.make_item("A", (), [("top", 0, 0, 4, 4)]),
            rc.make_item("B", (), [("bottom", 10, 0, 14, 4)]),
        ]
        plan = rc.plan_axis(its, 0, None, min_gap=CL, grid=0.25)
        # Only the order edge holds: B may reach A's centre, not pass it.
        a = its[0].pos[0] + plan.delta[0]
        b = its[1].pos[0] + plan.delta[1]
        self.assertGreaterEqual(b, a - 1e-9)

    def test_hull_macro_shapes_from_cover(self):
        from pnr.place.hull import hull_placement_rects

        hull = dict(top=[[-5, -5, 5, 0], [-5, 0, -1, 5]], bottom=[], inner=[])
        comp = Component(
            ref="MB00",
            footprint="block:x",
            pos=(20, 20),
            rot=90.0,
            side="top",
            courtyard=(10, 10),
            bbox=(10, 10),
            hull=hull,
        )
        rects = hull_placement_rects(comp)
        self.assertEqual(len(rects), 2)


class DovetailTermTest(unittest.TestCase):
    """PNR_HULL_DOVETAIL: the smooth packing term of global placement over hull bodies."""

    def bodies(self):
        import torch

        return dict(
            owner=torch.tensor([0, 1]),
            off4=torch.zeros(2, 4, 2),
            half4=torch.ones(2, 4, 2),
            pair=torch.triu(torch.ones(2, 2), diagonal=1),
        )

    def test_pack_pulls_bodies_together(self):
        import torch

        from pnr.place.hull import gp_pack

        pos = torch.tensor([[0.0, 0.0], [10.0, 0.0]], requires_grad=True)
        p = torch.zeros(2, 4)
        p[:, 0] = 1.0
        value = gp_pack(self.bodies(), pos, p, 0.05)
        # Half-perimeter of the box around both 2 x 2 bodies: 12 + 2.
        self.assertAlmostEqual(float(value), 14.0, delta=0.1)  # smooth max: + gamma ln 2
        value.backward()
        self.assertLess(float(pos.grad[0, 0]), 0.0)  # left body: moving right shrinks it
        self.assertGreater(float(pos.grad[1, 0]), 0.0)  # right body: moving left does

    def test_weight_flag(self):
        from pnr.place.hull import dovetail_weight

        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(dovetail_weight(), 0.0)
        with mock.patch.dict(os.environ, {"PNR_HULL_DOVETAIL": "1.5"}, clear=True):
            self.assertEqual(dovetail_weight(), 1.5)
        with mock.patch.dict(os.environ, {"PNR_HULL_DOVETAIL": "-1"}, clear=True):
            with self.assertRaises(ValueError):
                dovetail_weight()

    def test_parts_match_the_runner(self):
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "regression"))
        try:
            import run
        except ImportError:  # the runner needs its own imports; the kinds check covers it
            self.skipTest("run.py not importable here")
        self.assertEqual(tuple(run.ROUTE_COMPACT_PARTS), rc.PARTS)


class LoopTest(unittest.TestCase):
    def setUp(self):
        self.g = board([part("R1", 10, 20), part("R2", 40, 20)])
        self.c = compiled(self.g)

    def run_loop(self, reroute, settings=None):
        return rc.compact_loop(
            self.g,
            dict(missing=0, unresolved=0, vias=4, copper_mm=100.0),
            constraints=self.c,
            items_of=lambda placed: rc.graph_items(placed, self.c),
            copper_of=lambda route: None,
            reroute=reroute,
            metrics_of=lambda route: route,
            min_gap=CL,
            outline=(W, H),
            label="t",
            settings=settings or rc.Settings(rounds=1, attempts=2),
        )

    def test_accepts_a_route_no_worse(self):
        calls = []

        def reroute(candidate, axis):
            calls.append(axis)
            return candidate, dict(missing=0, unresolved=0, vias=4, copper_mm=90.0)

        placed, route, report = self.run_loop(reroute)
        self.assertEqual(calls, [0])  # y: nothing to close (one row)
        self.assertEqual(report["accepted"], 1)
        self.assertLess(xs(placed)["R2"] - xs(placed)["R1"], 30)
        self.assertEqual(route["copper_mm"], 90.0)
        self.assertLess(report["after"]["bbox_mm2"], report["before"]["bbox_mm2"])

    def test_backs_off_with_more_room(self):
        gaps = []

        def reroute(candidate, axis):
            r1, r2 = (courtyard_rect(candidate.component(r)) for r in ("R1", "R2"))
            gaps.append(r2.left - r1.right)
            bad = len(gaps) == 1
            return candidate, dict(missing=1 if bad else 0, unresolved=0, vias=4, copper_mm=99.0)

        placed, _route, report = self.run_loop(reroute)
        self.assertEqual(len(gaps), 2)
        self.assertEqual(report["accepted"], 1)
        results = [a["result"] for a in report["attempts"]]
        self.assertTrue(results[0].startswith("back off: missing"))
        self.assertEqual(results[1], "accepted")

    def test_keeps_the_routed_placement_when_every_attempt_fails(self):
        def reroute(candidate, axis):
            return candidate, dict(missing=0, unresolved=0, vias=4, copper_mm=150.0)

        placed, route, report = self.run_loop(reroute)
        self.assertEqual(report["accepted"], 0)
        self.assertEqual(xs(placed), xs(self.g))
        self.assertEqual(route["copper_mm"], 100.0)

    def test_hard_violation_is_not_routed(self):
        g = board([part("U1", 10, 20), part("C1", 40, 20)])
        c = compiled(g, group=[{"anchor": "U1", "members": ["C1"], "radius_mm": 31, "hard": True}])
        calls = []

        def check(before, after):
            return {"group_outside": ["C1"]}

        def reroute(candidate, axis):
            calls.append(axis)
            return candidate, dict(missing=0, unresolved=0, vias=0, copper_mm=0.0)

        placed, _route, report = rc.compact_loop(
            g,
            dict(missing=0, unresolved=0, vias=0, copper_mm=0.0),
            constraints=c,
            items_of=lambda p: rc.graph_items(p, c),
            copper_of=lambda r: None,
            reroute=reroute,
            metrics_of=lambda r: r,
            min_gap=CL,
            outline=(W, H),
            label="t",
            settings=rc.Settings(rounds=1, attempts=2),
            check=check,
        )
        self.assertEqual(calls, [])
        self.assertEqual(report["accepted"], 0)
        self.assertEqual(report["attempts"][0]["result"], "hard violation")

    def test_not_worse(self):
        s = rc.Settings(copper_tol=0.01, via_tol=2)
        base = dict(missing=0, unresolved=0, vias=10, copper_mm=100.0)
        self.assertTrue(rc.not_worse(base, dict(base, copper_mm=100.9), s)[0])
        self.assertFalse(rc.not_worse(base, dict(base, copper_mm=101.2), s)[0])
        self.assertTrue(rc.not_worse(base, dict(base, vias=12), s)[0])
        self.assertFalse(rc.not_worse(base, dict(base, vias=13), s)[0])
        self.assertFalse(rc.not_worse(base, dict(base, unresolved=1), s)[0])


if __name__ == "__main__":
    unittest.main()
