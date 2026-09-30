"""PNR_SHOVE=1 controller logic, QP and geometry (PnR runtime; no pcbnew).

Native (KiCad Python) checks of general in-pad sites, the make-room model and the
whole-transaction gate are in test_shove_native.py.
"""

import json
import math
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pnr import native_loop
from pnr.shove import control, geom, qp, targets


def job(net, source, target, distance, **extra):
    return dict(
        net=net,
        source=source,
        target=target,
        distance=distance,
        mode=extra.pop("mode", "power"),
        **extra
    )


class FlagOffTest(unittest.TestCase):
    def test_schedule_is_unchanged_without_flag(self):
        jobs = [
            job("a", "U1.1", "C1.1", 3.0, leaf_rms_a=0.01),
            job("b", "U1.2", "L1.1", 9.0, trunk=True),
            job("c", "U1.3", "R1.1", 1.0, mode="signal"),
        ]
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PNR_SHOVE", None)
            order = [t["net"] for t in native_loop.scheduled_route_jobs(jobs, {})]
        self.assertEqual(order, ["c", "a", "b"])  # shortest first, trunk/leaf keys ignored

    def test_bounds_are_unchanged_without_flag(self):
        self.assertEqual(
            native_loop.electrical_search_bounds("power", 1, [1, 1, 2, 2], [0, 0, 9, 9]),
            [1, 1, 2, 2],
        )


class ScheduleTest(unittest.TestCase):
    def test_trunks_first_then_shortest(self):
        jobs = [
            job("sw", "U5.21", "U5.25", 1.7, leaf_rms_a=0.5),
            job("sw", "U5.25", "C9.2", 4.0, leaf_rms_a=0.2),
            job("p5", "U5.13", "R13.2", 11.3, leaf_rms_a=0.01),
            job("sw", "U5.25", "L2.2", 11.7, trunk=True, leaf_rms_a=5),
            job("sig", "U5.8", "R9.1", 0.5, mode="signal"),
            job("vo", "U5.26", "C24.1", 3.9, trunk=True),
        ]
        with mock.patch.dict(os.environ, {"PNR_SHOVE": "1"}):
            order = [(t["source"], t["target"]) for t in native_loop.scheduled_route_jobs(jobs, {})]
        self.assertEqual(order[:2], [("U5.26", "C24.1"), ("U5.25", "L2.2")])
        self.assertEqual(
            order[2:], [("U5.8", "R9.1"), ("U5.21", "U5.25"), ("U5.25", "C9.2"), ("U5.13", "R13.2")]
        )

    def test_repeat_failures_still_go_after_untested_jobs(self):
        jobs = [
            job("sw", "U5.25", "L2.2", 11.7, trunk=True),
            job("sw", "U5.21", "U5.25", 1.7, trunk=True),
        ]
        attempts = {native_loop.route_job_key(jobs[0]): 2}
        with mock.patch.dict(os.environ, {"PNR_SHOVE": "1"}):
            order = [t["source"] for t in native_loop.scheduled_route_jobs(jobs, attempts)]
        self.assertEqual(order, ["U5.21", "U5.25"])


class BoundsTest(unittest.TestCase):
    def test_trunk_searches_the_board(self):
        self.assertEqual(
            targets.search_bounds(dict(trunk=True), 1, [4, 4, 5, 5], [0, 0, 30, 20]), [0, 0, 30, 20]
        )

    def test_branch_box_includes_root_copper(self):
        box = targets.search_bounds(dict(root_box=[10, 1, 12, 3]), 1, [4, 4, 5, 5], [0, 0, 30, 20])
        self.assertEqual(box, [4, 0, 13, 5])  # root box +1 mm, clipped to the board

    def test_no_root_box_keeps_local(self):
        self.assertEqual(targets.search_bounds({}, 1, [4, 4, 5, 5], [0, 0, 30, 20]), [4, 4, 5, 5])


class QPTest(unittest.TestCase):
    def test_single_inequality(self):
        x, lam, ok, _ = qp.hildreth([({0: 1.0}, 1.0, False)], [1.0])
        self.assertTrue(ok)
        self.assertAlmostEqual(x[0], 1.0, places=5)
        self.assertGreater(lam[0], 0)

    def test_weights_share_the_move(self):
        # x1 - x0 >= 0.3 (separation); x0 twice as stiff: x0 moves 0.1, x1 0.2.
        x, lam, ok, _ = qp.hildreth([({0: -1.0, 1: 1.0}, 0.3, False)], [2.0, 1.0])
        self.assertTrue(ok)
        self.assertAlmostEqual(x[0], -0.1, places=4)
        self.assertAlmostEqual(x[1], 0.2, places=4)

    def test_equality_and_box(self):
        cons = [({0: 1.0, 1: -1.0}, 0.0, True), ({0: 1.0}, 0.5, False), ({1: -1.0}, -0.4, False)]
        x, lam, ok, _ = qp.hildreth(cons, [1.0, 1.0], iters=20000)
        self.assertGreater(qp.residual(cons, x), 1e-3)  # x0 >= .5, x1 <= .4, x0 == x1 is infeasible
        self.assertFalse(ok)
        feasible = [
            ({0: 1.0, 1: -1.0}, 0.0, True),
            ({0: 1.0}, 0.3, False),
            ({1: -1.0}, -0.4, False),
        ]
        x, lam, ok, _ = qp.hildreth(feasible, [1.0, 1.0])
        self.assertTrue(ok)
        self.assertAlmostEqual(x[0], 0.3, places=5)
        self.assertAlmostEqual(x[1], 0.3, places=5)


class GeometryTest(unittest.TestCase):
    def test_segment_distance(self):
        d, t, s, p, q = geom.seg_seg((0, 0), (2, 0), (1, 1), (1, 3))
        self.assertAlmostEqual(d, 1.0)
        self.assertAlmostEqual(t, 0.5)
        self.assertEqual(s, 0.0)

    def test_crossing(self):
        self.assertTrue(geom.seg_intersect((0, 0), (2, 2), (0, 2), (2, 0)))
        self.assertFalse(
            geom.seg_intersect((0, 0), (1, 0), (1, 0), (2, 0))
        )  # touching is not crossing
        self.assertEqual(geom.seg_seg((0, 0), (2, 2), (0, 2), (2, 0))[0], 0.0)

    def test_point_in_poly(self):
        ring = [(0, 0), (2, 0), (2, 1), (1, 1), (1, 2), (0, 2)]  # L shape
        self.assertTrue(geom.point_in_poly((0.5, 1.5), ring))
        self.assertFalse(geom.point_in_poly((1.5, 1.5), ring))


class ControlTest(unittest.TestCase):
    def test_budget(self):
        budget = control.ShoveBudget(2)
        self.assertTrue(budget.reserve("h", ("n", "a", "b")))
        self.assertFalse(budget.reserve("h", ("n", "a", "b")))  # once per board and job
        self.assertTrue(budget.reserve("h2", ("n", "a", "b")))
        self.assertFalse(budget.reserve("h3", ("n", "a", "c")))  # loop limit
        budget.record(dict(accepted=True))
        self.assertEqual(budget.summary(), dict(limit=2, used=2, accepted=1))

    def test_eligible(self):
        target = dict(mode="power")
        failed = dict(status="no_current_sized_channel", accepted=False)
        self.assertTrue(control.eligible(target, failed, True, True, False, 200))
        self.assertFalse(control.eligible(target, failed, False, True, False, 200))  # flag off
        self.assertFalse(control.eligible(dict(mode="signal"), failed, True, True, False, 200))
        self.assertFalse(
            control.eligible(target, dict(failed, accepted=True), True, True, False, 200)
        )
        self.assertFalse(
            control.eligible(target, dict(status="worker_error"), True, True, False, 200)
        )
        self.assertFalse(control.eligible(target, failed, True, True, True, 200))  # placement trial
        self.assertFalse(control.eligible(target, failed, True, True, False, 60))  # no time left

    def test_failure_names_real_obstacles(self):
        outcome = dict(status="no_current_sized_channel", static_blockers={"t1": 3})
        shove = dict(solid_blockers={"t1": 0.5, "p9": 1.2}, solid_parts=["C24"])
        owners = {"t1": "U5", "p9": "C24", "p10": "C24"}
        merged = control.merge_failure(outcome, shove, owners)
        self.assertEqual(merged["static_blockers"]["t1"], 3 + 5)
        self.assertEqual(merged["static_blockers"]["p9"], 12 + 10)
        scores = native_loop.score_failures(
            {},
            [
                dict(
                    target=dict(source="U5.13", target="R13.2"),
                    static_blockers=merged["static_blockers"],
                )
            ],
            owners,
        )
        # Blocker pressure is normalised per failure; the solid part now dominates it.
        self.assertGreater(scores["C24"], 0.5)
        self.assertLess(scores.get("U5", 0), 2)

    def test_feedback_section(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "early-power").mkdir()
            ok = dict(
                stage="shove_repair",
                target=dict(source="U5.13", target="R13.2"),
                status="routed",
                accepted=True,
                rung=2,
                nudges=[dict(ref="C24", dx=0, dy=-0.15)],
                ripped=[],
                solid_parts=[],
            )
            bad = dict(
                stage="shove_repair",
                target=dict(source="U5.12", target="C24.1"),
                status="no_make_room",
                accepted=False,
                solid_parts=["C24"],
            )
            (root / "progress.json").write_text(json.dumps(dict(events=[bad, dict(stage="route")])))
            (root / "early-power" / "progress.json").write_text(json.dumps(dict(events=[ok])))
            section = control.feedback_section(root)
        self.assertEqual((section["transactions"], section["accepted"]), (2, 1))
        self.assertEqual(section["nudges"][0]["ref"], "C24")
        self.assertEqual(section["certificate_parts"], {"C24": 1})
        self.assertEqual(sorted(e["loop"] for e in section["events"]), [".", "early-power"])


class NudgedLayoutTest(unittest.TestCase):
    def test_write_back_converts_to_layout_frame(self):
        from types import SimpleNamespace

        from pnr.hier.synth_native import _nudged_layout

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            comp = lambda ref, pos: dict(
                ref=ref, address="board.converter." + ref.lower(), pos=pos, rot=90.0, side="top"
            )
            (root / "placed.json").write_text(
                json.dumps(dict(components=[comp("C24", [10.2, 5.2]), comp("C9", [3.2, 3.2])]))
            )
            (root / "evaluated-placed.json").write_text(
                json.dumps(dict(components=[comp("C24", [10.2, 5.05]), comp("C9", [3.2, 3.2])]))
            )
            block = SimpleNamespace(prefix="board.converter")
            out = _nudged_layout(root, block, 0.2)
        self.assertEqual(list(out), ["c24"])
        self.assertTrue(
            all(math.isclose(a, b, abs_tol=1e-9) for a, b in zip(out["c24"][:2], [10.0, 4.85]))
        )
        self.assertEqual(out["c24"][2:], [90.0, "top"])


class NudgeMergeTest(unittest.TestCase):
    """synth_native write-back: every template instance must agree on each pose."""

    layout = {"c24": [10.0, 5.0, 90.0, "top"], "c9": [3.0, 3.0, 0.0, "top"]}

    def merge(self, *nudged):
        from pnr.hier.synth_native import merge_nudges

        return merge_nudges(
            self.layout, [dict(instance="board.led%d" % i, nudged=n) for i, n in enumerate(nudged)]
        )

    def test_instance_that_did_not_nudge_is_a_conflict(self):
        merged, conflict = self.merge({"c24": [10.0, 4.85, 90.0, "top"]}, {})
        self.assertIn("c24", conflict)
        self.assertNotIn("c24", merged)

    def test_different_nudges_conflict(self):
        _, conflict = self.merge(
            {"c24": [10.0, 4.85, 90.0, "top"]}, {"c24": [10.1, 5.0, 90.0, "top"]}
        )
        self.assertTrue(conflict)

    def test_unknown_nudges_conflict(self):
        _, conflict = self.merge({"c24": [10.0, 4.85, 90.0, "top"]}, None)
        self.assertIn("unknown", conflict)

    def test_agreeing_instances_merge(self):
        pose = [10.0, 4.85, 90.0, "top"]
        self.assertEqual(self.merge({"c24": pose}, {"c24": list(pose)}), ({"c24": pose}, ""))
        self.assertEqual(self.merge({"c24": pose}), ({"c24": pose}, ""))

    def test_no_nudges(self):
        self.assertEqual(self.merge({}, {}), ({}, ""))
        self.assertEqual(self.merge({}, None), ({}, ""))


class InvariantTest(unittest.TestCase):
    def test_line_length_limit_is_five_percent_floored_and_capped(self):
        # min(0.5 mm, max(5 %, 0.15 mm)); necks are rigid separately (rigid_necks), so a
        # short neck never uses the 0.15 mm floor.
        from pnr.shove.ladder import line_length_limit

        self.assertAlmostEqual(line_length_limit(0.2), 0.15)
        self.assertAlmostEqual(line_length_limit(1.35), 0.15)
        self.assertAlmostEqual(line_length_limit(4.0), 0.2)
        self.assertAlmostEqual(line_length_limit(30.0), 0.5)

    def test_protected_refs(self):
        from pnr.shove.placement import protected_refs

        rules = dict(
            plane_access_intents=[
                dict(kind="power_array", ref="U1"),
                dict(kind="return", ref="C1"),
            ],
            copper_keepouts=[dict(ref="J1")],
        )
        self.assertEqual(protected_refs(rules), {"U1", "J1"})


CASE = Path(
    "<repo>/output/hier/" "blocks/nb3-pf/d5be6b6d0e99/native/board_converter-s1-35.75x27.75"
)


@unittest.skipUnless((CASE / "placed.json").exists(), "converter case absent")
class PlacementLegalityTest(unittest.TestCase):
    """G0/G3 hard placement check on the converter block (HARD group: bootstrap,
    vcc_dec, input_hf, output_hf within 5 mm of U5)."""

    def moved(self, ref, radius):
        graph = json.loads((CASE / "placed.json").read_text())
        comps = {c["ref"]: c for c in graph["components"]}
        u5, part = comps["U5"]["pos"], comps[ref]["pos"]
        d = math.dist(u5, part)
        comps[ref]["pos"] = [
            u5[i] + (part[i] - u5[i]) * radius / d for i in (0, 1)
        ]  # courtyards are sizes
        return graph

    def test_nudge_out_of_the_hard_group_is_rejected(self):
        from pnr.shove.placement import new_violations

        before = json.loads((CASE / "placed.json").read_text())
        verdict = new_violations(before, self.moved("C24", 5.05), CASE / "constraints.yaml")
        self.assertIn("C24", verdict["new"].get("group_outside", []))

    def test_small_nudge_inside_the_group_passes(self):
        from pnr.shove.placement import new_violations

        before = json.loads((CASE / "placed.json").read_text())
        verdict = new_violations(before, self.moved("C24", 4.45), CASE / "constraints.yaml")
        self.assertEqual(verdict["new"].get("group_outside", []), [])


if __name__ == "__main__":
    unittest.main()
