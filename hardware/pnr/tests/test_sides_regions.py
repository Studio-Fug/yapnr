"""Regions and alignments (pnr.place.regions) on a double-sided board (pnr.place.sides):
the legalizer's other-side slot, the incremental checkers behind flips and swaps, the
global placer's side mixture, side-drawn starts, the detail pass's soft cost, and the
07-chaser-20 relations placed under ``board.sides: double``."""

import copy
import unittest

import torch
from test_region_align import absolute, chaser, relative

from pnr.constraints import compile_constraints
from pnr.graph import BoardGraph, BoardOutline, Component, Net, Pad
from pnr.place import placer, regions
from pnr.place import sides as S
from pnr.place.geometry import resolve_fixed_poses, set_component_side
from pnr.place.legalize import legalize, legalize_constraint_kwargs
from pnr.place.metrics import hard_violations, pose_checker

OUTLINE = (20.0, 30.0)
# P's body runs 4 mm south of its origin and 1 mm north (a header measured from pin 1);
# on the bottom it mirrors to 1 mm south and 4 mm north.
BODY = (-1.0, -4.0, 1.0, 1.0)
REGION = [0.0, 4.0, 20.0, 14.0]  # P's centre: y 8..13 on top, 5..10 on the bottom


def part(ref, pos, size, pads, body=None):
    return Component(ref, "fp:" + ref, pos, 0.0, "top", size, size, pads=pads, body=body)


def header(pos=(10.0, 12.0)):
    pads = [Pad("1", "A", (0.0, 0.0), (0.6, 0.6)), Pad("2", "B", (0.0, -2.5), (0.6, 0.6))]
    return part("P", pos, (2.0, 8.0), pads, BODY)


def board(*parts):
    nets = {}
    for comp in parts:
        for pad in comp.pads:
            if pad.net:
                nets.setdefault(pad.net, []).append((comp.ref, pad.name))
    return BoardGraph(
        "t",
        list(parts),
        [Net(n, i + 1, pins) for i, (n, pins) in enumerate(sorted(nets.items()))],
        BoardOutline(*OUTLINE),
    )


def compiled(graph, **extra):
    doc = dict(board=dict(outline=dict(w=OUTLINE[0], h=OUTLINE[1]), layers=2, sides="double"))
    doc.update(extra)
    return compile_constraints(doc, graph.refs)


class LegalizerTest(unittest.TestCase):
    def test_other_side_slot_uses_the_mirrored_body(self):
        """The top side has no slot inside the region (a top-only part covers it), so
        the part takes the bottom, inside the region by its mirrored body: the
        legalizer's region box is per side, not the top side's box reused."""
        cover = part("K", (10.0, 12.5), (20.0, 2.0), [Pad("1", "A", (0.0, 0.0), (0.6, 0.6))])
        graph = board(header(), cover)
        cc = compiled(
            graph,
            fixed=dict(K=dict(at=[10, 12.5], rot=0, side="top")),
            region=[dict(name="r", refs=["P"], rect=REGION)],
        )
        poses = resolve_fixed_poses(graph, cc)
        out = legalize(
            graph,
            *OUTLINE,
            allow_rotation=False,
            clearance=0.2,
            side_options={"P": ("top", "bottom")},
            **legalize_constraint_kwargs(graph, cc, poses),
        )
        p = out.component("P")
        self.assertEqual(p.side, "bottom")
        self.assertEqual(p.body, (-1.0, -1.0, 1.0, 4.0))
        self.assertTrue(5.0 - 1e-9 <= p.pos[1] <= 10.0 + 1e-9, p.pos)
        self.assertFalse(any(hard_violations(out, cc).values()), hard_violations(out, cc))


class CheckerTest(unittest.TestCase):
    def test_pose_checker_rejects_a_flip_out_of_the_region(self):
        graph = board(header((10.0, 12.0)))
        cc = compiled(graph, region=[dict(name="r", refs=["P"], rect=REGION)])
        legal = pose_checker(graph, cc)
        p = graph.component("P")
        set_component_side(p, "bottom")
        self.assertFalse(legal([p]))  # bottom body reaches y 16
        p.pos = (10.0, 9.0)
        self.assertTrue(legal([p]))

    def test_pose_checker_rejects_a_flip_off_the_align_line(self):
        """Two parts aligned on pin 1: flipping one mirrors its pin 1 off the line."""
        pads = [Pad("1", "A", (0.0, 1.0), (0.6, 0.6)), Pad("2", "B", (0.0, -1.0), (0.6, 0.6))]
        u1 = part("U1", (5.0, 10.0), (3.0, 3.0), copy.deepcopy(pads))
        u2 = part("U2", (15.0, 10.0), (3.0, 3.0), copy.deepcopy(pads))
        graph = board(u1, u2)
        cc = compiled(graph, align=[dict(name="pins", refs=["U1", "U2"], axis="y", anchor="pad1")])
        self.assertFalse(any(hard_violations(graph, cc).values()))
        legal = pose_checker(graph, cc)
        u2 = graph.component("U2")
        set_component_side(u2, "bottom")
        self.assertFalse(legal([u2]))
        u2.pos = (15.0, 12.0)  # pin 1 back at y 11
        self.assertTrue(legal([u2]))


class GlobalTermsTest(unittest.TestCase):
    def test_a_side_free_part_mixes_both_sides(self):
        graph = board(header((10.0, 20.0)))
        cc = compiled(graph, region=[dict(name="r", refs=["P"], rect=REGION)])
        comps = graph.components
        angles = [0.0, 90.0, 180.0, 270.0]
        terms = regions.GlobalTerms(cc, comps, {"P": 0}, angles, [True], flippable=[0])
        flipped = copy.deepcopy(comps)
        set_component_side(flipped[0], "bottom")
        other = regions.GlobalTerms(cc, flipped, {"P": 0}, angles, [True])
        pos = torch.tensor([[10.0, 20.0]])
        probs = torch.tensor([[1.0, 0.0, 0.0, 0.0]])
        top = terms.loss(pos, probs)
        self.assertAlmostEqual(float(terms.loss(pos, probs, torch.tensor([0.0]))), float(top))
        bottom = float(other.loss(pos, probs))
        self.assertAlmostEqual(float(terms.loss(pos, probs, torch.tensor([1.0]))), bottom, 4)
        self.assertNotAlmostEqual(float(top), bottom, 2)
        half = float(terms.loss(pos, probs, torch.tensor([0.5])))
        self.assertAlmostEqual(half, (float(top) + bottom) / 2, 3)


class StartTest(unittest.TestCase):
    def test_a_start_drawn_on_the_bottom_projects_the_mirrored_body(self):
        from pnr.place.initial_pool import _project_start

        graph = board(header((10.0, 25.0)))
        cc = compiled(graph, region=[dict(name="r", refs=["P"], rect=REGION)])
        start = dict(positions={"P": [10.0, 25.0]}, rotations={"P": 0.0}, sides={"P": "bottom"})
        _project_start(graph, cc, start, fit=False)
        twin = copy.deepcopy(graph.component("P"))
        set_component_side(twin, "bottom")
        twin.pos = tuple(start["positions"]["P"])
        self.assertEqual(regions.region_offenders([twin], cc), [])
        self.assertEqual(graph.component("P").side, "top")  # the graph is not flipped


class DetailCostTest(unittest.TestCase):
    def test_soft_region_is_charged(self):
        from pnr.place.detail_moves import _Cost

        graph = board(header((10.0, 10.0)))
        cc = compiled(graph, region=[dict(name="r", refs=["P"], rect=REGION, hard=False)])
        cost = _Cost(graph, S.plan(graph, cc), cc)
        p = graph.component("P")
        inside = cost.part_cost(p)
        p.pos = (10.0, 20.0)
        self.assertGreater(cost.part_cost(p) - inside, 100.0)


class DoubleSidedChaserTest(unittest.TestCase):
    """07-chaser-20's region (abs) and align (rel) placed with every free part allowed
    on either side."""

    def place(self, doc, seed):
        doc["board"]["sides"] = "double"
        graph = chaser()
        cc = compile_constraints(doc, graph.refs)
        self.assertTrue(S.plan(graph, cc).active)
        placed, report = placer.place(graph, cc, seed=seed, iters=150, spread=1.3)
        self.assertTrue(report.legal, report.summary())
        self.assertEqual(regions.violations(placed, cc), [])
        self.assertFalse(any(hard_violations(placed, cc).values()))
        return placed

    def test_region(self):
        for seed in range(2):
            with self.subTest(seed=seed):
                self.place(absolute(), seed)

    def test_align(self):
        for seed in range(2):
            with self.subTest(seed=seed):
                placed = self.place(relative(), seed)
                ys = [placed.component(r).pos[1] for r in ("U1", "U2")]
                self.assertLessEqual(max(ys) - min(ys), 0.25 + 1e-6)


if __name__ == "__main__":
    unittest.main()
