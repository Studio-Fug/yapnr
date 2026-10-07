"""Hull-aware placement of block macros (PNR_HULL_NEST, pnr.place.hull): the nesting metrics
(rectangle overlap that hulls make legal, hull interlock, hull collision), the overlap a
displacement-keeping legalizer should see (``keep_overlap``), the legalizer keeping an
interlocked pose, the hull-shaped compaction (``nest``), the global-placement polish over hull
bodies, the compact metrics' hull area and the hierarchical driver's rectangle safety net."""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

import torch

from pnr.constraints import compile_constraints
from pnr.graph import BoardGraph, Component, Net, Pad
from pnr.hier import extent as E
from pnr.place import compact
from pnr.place import hull as H
from pnr.place.geometry import courtyard_rect
from pnr.place.legalize import legalize
from pnr.place.metrics import hard_violations, overlap_pairs

HERE = Path(__file__).resolve()
REGRESSION = HERE.parents[1] / "regression"
CL = 0.2  # placement clearance
G = 0.25  # placement grid
# A legalizer slot centre for the 10 x 6 macro (41 x 25 cells of 0.25 mm).
X0, Y0 = 15.125, 10.125


def half_macro(ref, pos, lower, rot=0.0):
    """A 10 x 6 block whose copper fills its lower (or upper) half: two of them dovetail
    into one 10 x 6 footprint."""
    y0, y1 = (-3.0, 0.125) if lower else (0.125, 3.0)
    geo = E.BlockGeometry(
        ok=True,
        extent=(-5, -3, 5, 3),
        c_cu=0.35,
        margin=0.3,
        track_width=0.2,
        shapes=dict(top=dict(rect=[[-5.0, y0, 5.0, y1]], cap=[]), bottom=dict(rect=[], cap=[])),
    )
    return Component(
        ref,
        "block:half",
        pos,
        rot,
        "top",
        (10.0, 6.0),
        (10.0, 6.0),
        pads=[Pad("X.1", "N", (0.0, 0.0), (1.0, 1.0))],
        hull=E.build_hull(geo, (0, 0), (10, 6), CL),
    )


def board(*comps, nets=()):
    return BoardGraph("t", list(comps), [Net(n, list(p)) for n, p in nets])


def constraints_for(graph, w=30, h=20):
    return compile_constraints({"board": {"outline": {"w": w, "h": h}}}, graph.refs)


HULL_ON = {"PNR_MACRO_HULL": "1"}


class NestingMetricsTest(unittest.TestCase):
    def test_interlocked_rectangles_overlap_but_hulls_do_not(self):
        # Two L shapes in a 10 x 10 frame: the second turned half a turn closes the first's notch.
        outlines = dict(A=(0, 0, 10, 10), B=(4, 4, 14, 14))
        shapes = dict(
            A=[("top", 0, 0, 3, 10), ("top", 3, 0, 10, 3)],
            B=[("top", 11, 4, 14, 14), ("top", 4, 11, 11, 14)],
        )
        m = H.nesting_metrics(outlines, shapes)
        self.assertEqual(m["macro_overlap_mm2"], 36.0)
        self.assertEqual(m["hull_interlock_mm2"], 36.0)  # their hull boxes overlap ...
        self.assertEqual(m["hull_collision_mm2"], 0.0)  # ... their hulls do not

    def test_collision_counts_shared_planes_only(self):
        outlines = dict(A=(0, 0, 4, 4), B=(2, 2, 6, 6))
        top_bottom = dict(A=[("top", 0, 0, 4, 4)], B=[("bottom", 2, 2, 6, 6)])
        self.assertEqual(H.nesting_metrics(outlines, top_bottom)["hull_collision_mm2"], 0.0)
        both_top = dict(A=[("top", 0, 0, 4, 4)], B=[("top", 2, 2, 6, 6)])
        self.assertEqual(H.nesting_metrics(outlines, both_top)["hull_collision_mm2"], 4.0)

    def test_disjoint_rectangles_report_zero(self):
        m = H.nesting_metrics(dict(A=(0, 0, 1, 1), B=(2, 2, 3, 3)), {})
        self.assertEqual(m, dict(macro_overlap_mm2=0, hull_interlock_mm2=0, hull_collision_mm2=0))

    def test_component_nesting_of_placed_halves(self):
        with mock.patch.dict(os.environ, HULL_ON):
            a = half_macro("MA", (X0, Y0), True)
            b = half_macro("MB", (X0, Y0 + 0.5), False)
            m = H.component_nesting([a, b])
        self.assertGreater(m["macro_overlap_mm2"], 50.0)
        self.assertEqual(m["hull_collision_mm2"], 0.0)


class KeepOverlapTest(unittest.TestCase):
    """The hook a displacement-keeping legalizer (PNR_LEGALIZE_KEEP) calls between two parts."""

    def test_interlocked_macros_are_legal_where_they_are(self):
        with mock.patch.dict(os.environ, HULL_ON):
            a = half_macro("MA", (X0, Y0), True)
            b = half_macro("MB", (X0, Y0 + 0.5), False)
            self.assertTrue(courtyard_rect(a).overlaps(courtyard_rect(b)))
            self.assertEqual(H.keep_overlap(a, b, CL), 0.0)

    def test_colliding_hulls_overlap(self):
        with mock.patch.dict(os.environ, HULL_ON):
            a = half_macro("MA", (X0, Y0), True)
            b = half_macro("MB", (X0, Y0 - 1.0), False)
            self.assertGreater(H.keep_overlap(a, b, CL), 9.0)

    def test_clearance_is_kept_between_hulls(self):
        with mock.patch.dict(os.environ, HULL_ON):
            a = half_macro("MA", (X0, Y0), True)
            b = half_macro("MB", (X0, Y0 + 0.1), False)  # hull gap 0.1 < clearance 0.2
            self.assertGreater(H.keep_overlap(a, b, CL), 0.0)

    def test_plain_parts_leave_the_rectangle_test(self):
        r1 = Component("R1", "R0402", (1, 1), 0.0, "top", (1, 1), (1, 1))
        r2 = Component("R2", "R0402", (1.5, 1), 0.0, "top", (1, 1), (1, 1))
        self.assertIsNone(H.keep_overlap(r1, r2, CL))


class LegalizerAcceptsInterlockTest(unittest.TestCase):
    def test_interlocked_pose_is_kept(self):
        with mock.patch.dict(os.environ, HULL_ON):
            g = board(half_macro("MA", (X0, Y0), True), half_macro("MB", (X0, Y0 + 0.5), False))
            out = legalize(
                g,
                30,
                20,
                fixed={},
                keepouts=[],
                clearance=CL,
                grid_mm=G,
                rotations={"MA": 0.0, "MB": 0.0},
            )
            self.assertEqual(out.component("MA").pos, (X0, Y0))
            self.assertEqual(out.component("MB").pos, (X0, Y0 + 0.5))
            self.assertEqual(overlap_pairs(out), [])
            self.assertGreater(H.component_nesting(out.components)["macro_overlap_mm2"], 50.0)


class NestTest(unittest.TestCase):
    def nest(self, graph, fixed=()):
        con = constraints_for(graph)
        return H.nest(graph, con, 30, 20, clearance=CL, grid=G, fixed=set(fixed))

    def apart(self):
        a = half_macro("MA", (X0, Y0 - 3.0), True)
        b = half_macro("MB", (X0, Y0 + 4.0), False)  # rectangles 1 mm apart
        return board(a, b, nets=[("N", [("MA", "X.1"), ("MB", "X.1")])])

    def test_blocks_slide_into_each_other(self):
        with mock.patch.dict(os.environ, dict(HULL_ON, PNR_HULL_NEST="1")):
            g = self.apart()
            self.assertEqual(H.component_nesting(g.components)["macro_overlap_mm2"], 0.0)
            report = self.nest(g)
            m = H.component_nesting(g.components)
            self.assertGreater(report["moved"], 0)
            self.assertGreater(m["macro_overlap_mm2"], 30.0)  # the rectangles interlock
            self.assertEqual(m["hull_collision_mm2"], 0.0)
            self.assertEqual(overlap_pairs(g), [])
            self.assertFalse(any(hard_violations(g, constraints_for(g)).values()))
            self.assertEqual(report["nesting"], m)
            # The poses the moves started from, for the driver's un-nested fallback.
            self.assertEqual(report["nesting_before"]["macro_overlap_mm2"], 0.0)
            self.assertTrue(set(report["before"]) <= {"MA", "MB"})
            for ref, (x, y, _rot) in report["before"].items():
                self.assertEqual((x, y), self.apart().component(ref).pos)

    def test_fixed_macro_stays(self):
        with mock.patch.dict(os.environ, dict(HULL_ON, PNR_HULL_NEST="1")):
            g = self.apart()
            self.nest(g, fixed={"MA"})
            self.assertEqual(g.component("MA").pos, (X0, Y0 - 3.0))
            self.assertLess(g.component("MB").pos[1], Y0 + 4.0)

    def test_moves_stay_on_the_lattice(self):
        with mock.patch.dict(os.environ, dict(HULL_ON, PNR_HULL_NEST="1")):
            g = self.apart()
            self.nest(g)
            for c in g.components:
                self.assertIsNotNone(H._slot_of(c, G, CL), c.ref)

    def test_off_lattice_macro_is_left_alone(self):
        with mock.patch.dict(os.environ, dict(HULL_ON, PNR_HULL_NEST="1")):
            g = self.apart()
            g.component("MB").pos = (X0 + 0.1, Y0 + 4.0)
            report = self.nest(g)
            self.assertIn("lattice", report["skipped"])
            self.assertEqual(g.component("MB").pos, (X0 + 0.1, Y0 + 4.0))

    def test_flag(self):
        with mock.patch.dict(os.environ, dict(HULL_ON, PNR_HULL_NEST="1")):
            self.assertTrue(H.nest_enabled())
        with mock.patch.dict(os.environ, {"PNR_HULL_NEST": "1", "PNR_MACRO_HULL": "0"}):
            self.assertFalse(H.nest_enabled())  # inert without hulls


class PolishBodiesTest(unittest.TestCase):
    def test_parts_become_slots_and_hull_boxes_grow(self):
        with mock.patch.dict(os.environ, HULL_ON):
            comps = [
                half_macro("MA", (X0, Y0), True),
                Component("R1", "R0402", (2, 2), 0.0, "top", (1.0, 0.5), (1.0, 0.5)),
            ]
            bodies = H.gp_bodies(comps)
        frozen = dict(
            centre=torch.tensor([[0.0, 0.0], [0.1, 0.0]]),
            half=torch.tensor([[5.25, 3.25], [0.75, 0.5]]),
        )
        out = H.polish_bodies(bodies, comps, frozen, G, CL)
        part = int((bodies["owner"] == 1).nonzero()[0])
        self.assertTrue(torch.allclose(out["half4"][part], torch.tensor([[0.75, 0.5]] * 4)))
        self.assertTrue(torch.allclose(out["off4"][part], torch.tensor([[0.1, 0.0]] * 4)))
        macro = (bodies["owner"] == 0).nonzero()[:, 0]
        grown = bodies["half4"][macro] + CL / 2 + G / 2
        self.assertTrue(torch.allclose(out["half4"][macro], grown))

    def test_interlocked_hull_bodies_do_not_overlap(self):
        with mock.patch.dict(os.environ, HULL_ON):
            comps = [half_macro("MA", (X0, Y0), True), half_macro("MB", (X0, Y0 + 1.0), False)]
            bodies = H.gp_bodies(comps)
        frozen = dict(centre=torch.zeros(2, 2), half=torch.tensor([[5.25, 3.25]] * 2))
        out = H.polish_bodies(bodies, comps, frozen, G, CL)
        p = torch.nn.functional.one_hot(torch.zeros(2, dtype=torch.long), 4).float()
        pos = torch.tensor([[X0, Y0], [X0, Y0 + 1.0]])
        self.assertEqual(float(H.gp_overlap(out, pos, p, 0.0)), 0.0)
        pos = torch.tensor([[X0, Y0], [X0, Y0 - 1.0]])  # MB's half over MA's
        self.assertGreater(float(H.gp_overlap(out, pos, p, 0.0)), 0.0)

    def test_global_place_polishes_with_hull_macros(self):
        from pnr.place.gp_polish import Polish
        from pnr.place.model import global_place

        with mock.patch.dict(os.environ, HULL_ON):
            g = board(
                half_macro("MA", (X0, Y0), True),
                half_macro("MB", (X0, Y0 + 4.0), False),
                nets=[("N", [("MA", "X.1"), ("MB", "X.1")])],
            )
            con = constraints_for(g)
            polish = Polish(clearance=CL, grid_mm=G, steps=5)
            with mock.patch.object(Polish, "prepare", wraps=polish.prepare) as prepared:
                positions, _ = global_place(g, con, 30, 20, seed=1, iters=20, polish=polish)[:2]
            self.assertEqual(prepared.call_count, 1)  # the polish phase ran
            for x, y in positions.values():
                self.assertTrue(abs(x) < 1e6 and abs(y) < 1e6)


class CompactHullAreaTest(unittest.TestCase):
    def test_hull_macro_area_is_its_hull(self):
        with mock.patch.dict(os.environ, HULL_ON):
            a = half_macro("MA", (X0, Y0), True)
            area = compact.part_area(a)
            self.assertLess(area, 0.6 * 60.0)  # half of the 10 x 6 block, on the lattice
            self.assertGreater(area, 0.4 * 60.0)
        with mock.patch.dict(os.environ, {"PNR_MACRO_HULL": "0"}):
            self.assertEqual(compact.part_area(a), 60.0)

    def test_interlocked_utilisation_stays_below_one(self):
        with mock.patch.dict(os.environ, HULL_ON):
            g = board(half_macro("MA", (X0, Y0), True), half_macro("MB", (X0, Y0 + 0.5), False))
            m = compact.metrics(g, 30, 20)
        self.assertLess(m["utilization"], 1.0)


class SafetyNetTest(unittest.TestCase):
    """regression/hier_case.py: a hull placement that does not knit (route-then-compact off)
    gets the rectangle placement of the same seed, and the better knit is kept."""

    def setUp(self):
        sys.path.insert(0, str(REGRESSION))
        import hier_case

        self.hc = hier_case

    def tearDown(self):
        sys.path.remove(str(REGRESSION))

    def record(self, missing, rid):
        return dict(
            id=rid,
            missing=missing,
            unresolved=[],
            split_nets=[],
            seed_index=0,
            objective=[missing, 0, 3, 10.0],
        )

    def run_net(self, rect_missing, env):
        hc = self.hc
        best = self.record(2, "top-00-route")
        rect = self.record(rect_missing, "top-00-rect-route")
        seen = {}

        def place(*args, **kwargs):
            seen["hull"] = os.environ.get("PNR_MACRO_HULL")
            report = mock.Mock(legal=True)
            return "flat", report, {}

        case = dict(budget=dict(top_iters=10))
        with (
            mock.patch.dict(os.environ, env, clear=False),
            mock.patch("pnr.hier.top.hierarchical_place", side_effect=place),
            mock.patch.object(hc, "knit", return_value=rect) as knit,
            mock.patch.object(hc, "route_rank", side_effect=lambda r: tuple(r["objective"])),
        ):
            out, report = hc.safety_net(case, best, [dict(seed=7)], None, None, None, {}, {})
            after = os.environ.get("PNR_MACRO_HULL")
        return out, report, seen, knit, after

    def test_rectangle_fallback_wins_when_it_knits(self):
        out, report, seen, knit, after = self.run_net(0, dict(HULL_ON, PNR_HULL_NEST="1"))
        self.assertEqual(out["id"], "top-00-rect-route")
        self.assertEqual(report["kept"], "rect")
        self.assertEqual(report["seed"], 7)
        self.assertIsNone(seen["hull"])  # placed without hulls ...
        self.assertEqual(after, "1")  # ... and the flag restored
        knit.assert_called_once()

    def test_hull_knit_kept_when_the_fallback_is_no_better(self):
        out, report, _seen, _knit, _after = self.run_net(3, dict(HULL_ON, PNR_HULL_NEST="1"))
        self.assertEqual(out["id"], "top-00-route")
        self.assertEqual(report["kept"], "hull")

    def test_off_without_the_flag(self):
        with mock.patch.dict(os.environ, HULL_ON):
            os.environ.pop("PNR_HULL_NEST", None)
            best = self.record(2, "top-00-route")
            out, report = self.hc.safety_net({}, best, [], None, None, None, {}, {})
        self.assertIs(out, best)
        self.assertIsNone(report)

    def test_incomplete(self):
        self.assertTrue(self.hc.knit_incomplete(self.record(1, "a")))
        self.assertFalse(self.hc.knit_incomplete(self.record(0, "a")))
        r = self.record(0, "a")
        r["split_nets"] = ["N"]
        self.assertTrue(self.hc.knit_incomplete(r))


class StageNamesTest(unittest.TestCase):
    def test_new_stages_have_canonical_buckets(self):
        from pnr.stage_timing import STAGES, canonical

        self.assertIn(canonical("hull-nest"), STAGES)
        self.assertIn(canonical("hull-safety-net"), STAGES)


if __name__ == "__main__":
    unittest.main()
