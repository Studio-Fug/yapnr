"""PNR_LEGALIZE_REORIENT: the in-place turn pass after legalization (pnr.place.reorient;
docs/design/compact-placement.md section 11, ``C2``).

The pass picks the wirelength-optimal turn among the legal ones (a neighbour or a keep-out
rules a better turn out), never turns an excluded part, never raises a part's channel penalty,
is idempotent and deterministic, and in the placer only shortens the board.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import compact_fixture as fixture  # noqa: E402

from pnr.constraints import compile_constraints  # noqa: E402
from pnr.graph import BoardGraph, BoardOutline, Component, Net, Pad  # noqa: E402
from pnr.place.metrics import hard_violations, hpwl  # noqa: E402
from pnr.place.reorient import frozen_refs, reorient  # noqa: E402

W, H = 20.0, 12.0


def resistor(ref="R1", pos=(10.0, 6.0), rot=0.0, size=(2.0, 1.0)):
    pads = [Pad("1", "A", (-0.8, 0.0), (0.4, 0.4)), Pad("2", "B", (0.8, 0.0), (0.4, 0.4))]
    return Component(ref, "R_0603", pos, rot, "top", size, size, pads=pads)


def fixed_pin(ref, net, pos):
    pads = [Pad("1", net, (0.0, 0.0), (0.4, 0.4))]
    return Component(ref, "conn", pos, 0.0, "top", (1.0, 1.0), (1.0, 1.0), pads=pads)


def board(*extra, fix_r1=False, a_at=(16.0, 9.0), b_at=(10.0, 1.0), r1_rot=0.0, **spec_extra):
    """R1 between the pin of A (J1, north-east) and the pin of B (D1, south): its turns
    cost 15.6 (0 and 90), 14.0 (180) and 12.4 mm (270) of wirelength."""
    parts = [resistor(rot=r1_rot), fixed_pin("J1", "A", a_at), fixed_pin("D1", "B", b_at)]
    parts += list(extra)
    nets = {}
    for comp in parts:
        for pad in comp.pads:
            nets.setdefault(pad.net, []).append((comp.ref, pad.name))
    g = BoardGraph(
        "reorient",
        parts,
        [Net(n, i + 1, p) for i, (n, p) in enumerate(sorted(nets.items()))],
        BoardOutline(W, H),
    )
    # The pins and the obstacles ("block") are fixed; R1 too with ``fix_r1``.
    fixed = {
        c.ref: dict(at=list(c.pos), rot=0, side="top")
        for c in parts
        if c.ref in ("J1", "D1") or c.footprint == "block" or (c.ref == "R1" and fix_r1)
    }
    doc = dict(
        schema="v0",
        board=dict(outline=dict(w=W, h=H), layers=2, default_clearance_mm=0.2),
        fixed=fixed,
    )
    doc.update(spec_extra)
    return g, compile_constraints(doc, g.refs)


def turn(g, cc, **kwargs):
    kwargs.setdefault("clearance", 0.2)
    return reorient(g, cc, **kwargs)


class ChoiceTest(unittest.TestCase):
    def test_takes_the_best_turn(self):
        g, cc = board()
        out, turned = turn(g, cc)
        self.assertEqual(turned, {"R1": 270.0})
        self.assertEqual(out.component("R1").pos, (10.0, 6.0))  # about its centre
        self.assertAlmostEqual(hpwl(g) - hpwl(out), 15.6 - 12.4)
        self.assertFalse(any(hard_violations(out, cc).values()))

    def test_a_neighbour_rules_the_best_turn_out(self):
        # X1 sits 0.3 mm above R1: upright (90 or 270) R1 would overlap it, 180 still fits.
        x1 = Component("X1", "block", (10.0, 7.3), 0.0, "top", (4.0, 1.0), (4.0, 1.0))
        g, cc = board(x1)
        out, turned = turn(g, cc)
        self.assertEqual(turned, {"R1": 180.0})
        self.assertFalse(any(hard_violations(out, cc).values()))

    def test_a_keepout_rules_the_best_turn_out(self):
        g, cc = board(keepout=[dict(name="k", polygon=[[9, 6.7], [11, 6.7], [11, 8], [9, 8]])])
        self.assertEqual(turn(g, cc)[1], {"R1": 180.0})

    def test_the_spread_factor_is_kept(self):
        # Grown by 1.3 (the off-mode legalizer's floor) the upright turns reach X1 at 0.7 mm.
        x1 = Component("X1", "block", (10.0, 7.7), 0.0, "top", (4.0, 1.0), (4.0, 1.0))
        g, cc = board(x1)
        self.assertEqual(turn(g, cc)[1], {"R1": 270.0})
        self.assertEqual(turn(g, cc, spread=1.3)[1], {"R1": 180.0})

    def test_ties_keep_the_turn(self):
        # Both pins north-east of every pad: each turn has the same wirelength (22 mm).
        g, cc = board(a_at=(16.0, 10.0), b_at=(17.0, 11.0), r1_rot=90.0)
        self.assertEqual(turn(g, cc)[1], {})

    def test_idempotent(self):
        g, cc = board()
        out, _ = turn(g, cc)
        again, turned = turn(out, cc)
        self.assertEqual(turned, {})  # idempotent: nothing left to gain
        self.assertEqual(again.to_json(), out.to_json())

    def test_deterministic(self):
        x1 = Component("X1", "block", (10.0, 7.3), 0.0, "top", (4.0, 1.0), (4.0, 1.0))
        g, cc = board(x1)
        self.assertEqual(turn(g, cc)[0].to_json(), turn(g, cc)[0].to_json())


class ChannelTest(unittest.TestCase):
    def test_a_turn_that_raises_the_channel_penalty_is_refused(self):
        class Model:
            classes = {}

            def penalty(self, comp, others, x, y):
                return 1.0 if comp.ref == "R1" and comp.rot == 270.0 else 0.0

        g, cc = board()
        self.assertEqual(turn(g, cc, channel_model=Model())[1], {"R1": 180.0})
        # PNR_LEGALIZE_REORIENT=wire: no channel guard.
        self.assertEqual(turn(g, cc, channel_model=Model(), channel_guard=False)[1], {"R1": 270.0})

    def test_plane_nets_are_left_out(self):
        from pnr.place.channels import ChannelModel

        g, cc = board()
        # With B a plane net only A counts: 0 and 90 cost 9.8 mm, 180 and 270 8.2 mm, a tie
        # that the earlier turn (180) wins; a part whose turns all tie keeps its own.
        model = ChannelModel(g, dict(net_classes=[dict(nets=["B"], plane_layer="In1.Cu")]))
        self.assertEqual(turn(g, cc, channel_model=model)[1], {"R1": 180.0})


class ExclusionTest(unittest.TestCase):
    def test_excluded_parts_are_never_turned(self):
        cases = dict(
            fixed=dict(fix_r1=True),
            rotation=dict(orientation={"R1": 0}),
            pair=dict(diff_pair=[dict(name="p", p="A", n="B")]),
            matched=dict(length_match=[dict(name="m", nets=["A", "B"], tolerance_mm=1.0)]),
        )
        for name, extra in cases.items():
            with self.subTest(name):
                g, cc = board(**extra)
                self.assertIn("R1", frozen_refs(g, cc))
                self.assertEqual(turn(g, cc)[1], {})

    def test_line_group_members_rows_and_macros_are_frozen(self):
        g, cc = board(
            resistor("R2", pos=(14.0, 3.0)),
            line_group=[dict(name="l", members=["R1", "R2"], pitch_mm=4.0)],
        )
        self.assertTrue({"R1", "R2"} <= frozen_refs(g, cc))
        for footprint in ("line:l", "block:b"):
            g, cc = board()
            g.component("R1").footprint = footprint
            self.assertIn("R1", frozen_refs(g, cc))
            self.assertEqual(turn(g, cc)[1], {})

    def test_an_illegal_board_is_left_alone(self):
        x1 = Component("X1", "block", (10.0, 6.0), 0.0, "top", (4.0, 1.0), (4.0, 1.0))
        g, cc = board(x1)
        out, turned = turn(g, cc)
        self.assertEqual(turned, {})
        self.assertEqual(out.to_json(), g.to_json())


class PlacerTest(unittest.TestCase):
    """The pass in the placer, with detailed placement (on with KEEP by default, whose turns take
    its place: pnr.place.detail) off."""

    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"PNR_DETAIL_PLACE": "0"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_detailed_placement_takes_its_place(self):
        from pnr.place import placer
        from pnr.place import reorient as module

        graph, constraints, rules = fixture.load("04-inverter-leds-8")
        with mock.patch.dict(
            os.environ, {"PNR_DETAIL_PLACE": "1", "PNR_LEGALIZE_REORIENT": "1"}
        ), mock.patch.object(module, "reorient", side_effect=AssertionError("called")):
            _placed, report = placer.place(
                graph, constraints, seed=0, iters=60, spread=1.3, channel_rules=rules
            )
        self.assertIsNotNone(report.detail_motion)

    def test_placer_runs_the_pass_only_with_the_flag(self):
        from pnr.place import placer
        from pnr.place import reorient as module

        graph, constraints, rules = fixture.load("04-inverter-leds-8")
        saved = os.environ.pop("PNR_LEGALIZE_REORIENT", None)
        try:
            with mock.patch.object(module, "reorient", side_effect=AssertionError("called")):
                base, report = placer.place(
                    graph, constraints, seed=0, iters=60, spread=1.3, channel_rules=rules
                )
            os.environ["PNR_LEGALIZE_REORIENT"] = "1"
            calls = []
            real = module.reorient

            def spy(*args, **kwargs):
                out = real(*args, **kwargs)
                calls.append(out[1])
                return out

            with mock.patch.object(module, "reorient", spy):
                turned, report2 = placer.place(
                    graph, constraints, seed=0, iters=60, spread=1.3, channel_rules=rules
                )
        finally:
            os.environ.pop("PNR_LEGALIZE_REORIENT", None)
            if saved is not None:
                os.environ["PNR_LEGALIZE_REORIENT"] = saved
        self.assertEqual(len(calls), 1)
        self.assertTrue(report.legal and report2.legal)
        # Each turn shortens its part's nets: the board is never longer.
        self.assertLessEqual(report2.hpwl_placed, report.hpwl_placed + 1e-9)
        if calls[0]:
            self.assertLess(report2.hpwl_placed, report.hpwl_placed)
        for comp in base.components:
            if comp.ref not in calls[0]:
                self.assertEqual(comp.rot, turned.component(comp.ref).rot)

    def test_placer_never_turns_without_orient(self):
        """``orient=False`` (the caller forbids turns): the pass does not run."""
        from pnr.place import placer
        from pnr.place import reorient as module

        graph, constraints, rules = fixture.load("04-inverter-leds-8")
        saved = os.environ.get("PNR_LEGALIZE_REORIENT")
        os.environ["PNR_LEGALIZE_REORIENT"] = "wire"
        try:
            with mock.patch.object(module, "reorient", side_effect=AssertionError("called")):
                placed, report = placer.place(
                    graph,
                    constraints,
                    seed=0,
                    iters=60,
                    spread=1.3,
                    channel_rules=rules,
                    orient=False,
                )
        finally:
            os.environ.pop("PNR_LEGALIZE_REORIENT", None)
            if saved is not None:
                os.environ["PNR_LEGALIZE_REORIENT"] = saved
        self.assertTrue(report.legal)
        for comp in graph.components:
            self.assertEqual(placed.component(comp.ref).rot, comp.rot, comp.ref)


if __name__ == "__main__":
    unittest.main()
