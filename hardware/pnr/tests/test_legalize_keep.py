"""PNR_LEGALIZE_KEEP: displacement-minimizing legalization (pnr.place.keep, pnr.place.motion).

A part or a block macro that is legal at its global pose keeps it (a grid snap) and its turn, even
with the wirelength term and its turn search on; a slight overlap is resolved by pushing the
parts apart in their global order (the neighbour beyond is pushed too, nothing else moves); a
part buried under another is relocated while the other stays; ``PNR_LEGALIZE_KEEP=0`` restores
the plain packer; the motion record (moved count, displacement, topology kept) and the triage
counts; the spreading solver on its own.
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from pnr import legalize_flags
from pnr.graph import BoardGraph, BoardOutline, Component, Net, Pad
from pnr.place import keep as keepmod
from pnr.place.legalize import legalize
from pnr.place.metrics import outside_outline, overlap_pairs
from pnr.place.motion import combine, motion, occlusion

GRID = 0.25


def part(ref, nets, pos, size=(2.0, 1.0), rot=0.0, footprint="R_0603"):
    pads = [
        Pad(str(i + 1), n, (-size[0] / 4 + i * size[0] / 2, 0.0), (0.4, 0.4))
        for i, n in enumerate(nets)
    ]
    return Component(ref, footprint, pos, rot, "top", size, size, pads=pads)


def board(parts, w=30.0, h=20.0):
    nets = {}
    for comp in parts:
        for pad in comp.pads:
            if pad.net:
                nets.setdefault(pad.net, []).append((comp.ref, pad.name))
    return BoardGraph(
        "keep",
        list(parts),
        [Net(n, i + 1, pins) for i, (n, pins) in enumerate(sorted(nets.items()))],
        BoardOutline(w, h),
    )


def run(g, keep=None, fixed=(), **kwargs):
    kwargs.setdefault("allow_rotation", True)
    kwargs.setdefault("grid_mm", GRID)
    return legalize(
        g,
        g.outline.width,
        g.outline.height,
        fixed={r: g.component(r).pos for r in fixed},
        keepouts=[],
        clearance=0.2,
        keep=keep,
        **kwargs,
    )


def spread_out():
    """Four parts well apart, wired across the board so the wirelength term would pull them
    together (and turn the reversed one)."""
    return board(
        [
            part("R1", ["A", "B"], (5.0, 5.0)),
            part("R2", ["B", "C"], (24.0, 5.0), rot=180.0),
            part("R3", ["C", "D"], (24.0, 15.0)),
            part("R4", ["D", "A"], (5.0, 15.0), rot=90.0),
        ]
    )


class LegalAtGlobalPoseTest(unittest.TestCase):
    def assert_held(self, before, after, refs):
        for ref in refs:
            a, b = before.component(ref), after.component(ref)
            self.assertLessEqual(abs(a.pos[0] - b.pos[0]), GRID / 2 + 1e-9, ref)
            self.assertLessEqual(abs(a.pos[1] - b.pos[1]), GRID / 2 + 1e-9, ref)
            self.assertEqual(a.rot % 360, b.rot % 360, ref)

    def test_legal_parts_do_not_move_even_with_the_wire_term(self):
        g = spread_out()
        placed = run(g, wire_weight=4.0)
        self.assert_held(g, placed, ["R1", "R2", "R3", "R4"])
        record = placed.legal_motion
        self.assertTrue(record["keep"])
        self.assertEqual(record["moved"], 0)
        self.assertEqual(record["topology"], 1.0)
        self.assertEqual((record["clean"], record["mild"], record["severe"]), (4, 0, 0))
        self.assertEqual(record["anchored"], 4)
        # Without it the wirelength term pulls them in and turns the reversed part.
        loose = run(g, keep=False, wire_weight=4.0)
        self.assertGreater(loose.legal_motion["moved"], 0)
        self.assertFalse(loose.legal_motion["keep"])

    def test_the_switch(self):
        g = spread_out()
        with mock.patch.dict(os.environ, {"PNR_LEGALIZE_KEEP": "0"}):
            self.assertFalse(legalize_flags.legalize_keep())
            self.assertEqual(legalize_flags.active().get("LEGALIZE_KEEP"), False)
            self.assertGreater(run(g, wire_weight=4.0).legal_motion["moved"], 0)
        with mock.patch.dict(os.environ, {"PNR_LEGALIZE_KEEP": ""}):
            self.assertTrue(legalize_flags.legalize_keep())
            self.assertNotIn("LEGALIZE_KEEP", legalize_flags.active())
        with mock.patch.dict(os.environ, {"PNR_LEGALIZE_KEEP": "yes"}):
            with self.assertRaises(ValueError):
                legalize_flags.legalize_keep()

    def test_legal_block_macros_do_not_move(self):
        blocks = [
            part("B1", ["X", "Y"], (6.0, 6.0), size=(8.0, 6.0), footprint="block:bank_a"),
            part("B2", ["Y", "Z"], (22.0, 6.0), size=(8.0, 6.0), footprint="block:bank_b"),
            part("B3", ["Z", "X"], (14.0, 15.0), size=(8.0, 6.0), footprint="block:clock"),
        ]
        g = board(blocks)
        placed = run(g, wire_weight=4.0)
        self.assert_held(g, placed, ["B1", "B2", "B3"])
        self.assertEqual(placed.legal_motion["moved"], 0)

    def test_fixed_parts_are_obstacles_not_moved(self):
        g = spread_out()
        g.components.append(part("J1", ["A"], (14.0, 10.0), size=(3.0, 3.0)))
        placed = run(g, fixed=("J1",), wire_weight=4.0)
        self.assertEqual(placed.component("J1").pos, (14.0, 10.0))
        self.assert_held(g, placed, ["R1", "R2", "R3", "R4"])
        self.assertEqual(placed.legal_motion["count"], 4)


class OverlapTest(unittest.TestCase):
    def test_a_slight_overlap_pushes_the_neighbours_apart_in_order(self):
        # A row R1 R2 R3, R1 and R2 overlapping by 0.4 mm, R3 just clear of R2; R4 far away.
        g = board(
            [
                part("R1", ["A", "B"], (10.0, 10.0)),
                part("R2", ["B", "C"], (11.8, 10.0)),
                part("R3", ["C", "D"], (14.1, 10.0)),
                part("R4", ["D", "A"], (25.0, 4.0)),
            ]
        )
        placed = run(g)
        self.assertEqual(overlap_pairs(placed, clearance=0.2), [])
        xs = [placed.component(r).pos[0] for r in ("R1", "R2", "R3")]
        self.assertEqual(xs, sorted(xs))  # the row keeps its order
        for ref in ("R1", "R2", "R3"):
            self.assertAlmostEqual(placed.component(ref).pos[1], 10.0, delta=GRID)
            self.assertLess(abs(placed.component(ref).pos[0] - g.component(ref).pos[0]), 1.0)
        self.assertLessEqual(abs(placed.component("R4").pos[0] - 25.0), GRID / 2 + 1e-9)
        record = placed.legal_motion
        self.assertEqual(record["severe"], 0)
        self.assertEqual(record["mild"], 2)
        self.assertEqual(record["relocated"], 0)
        self.assertEqual(record["topology"], 1.0)

    def test_a_buried_part_is_relocated_and_the_part_over_it_stays(self):
        big = part("U1", ["A", "B", "C", "D"], (12.0, 10.0), size=(8.0, 6.0))
        small = part("C1", ["A", "B"], (12.5, 10.5))
        g = board([big, small, part("R1", ["C", "D"], (25.0, 4.0))])
        placed = run(g)
        self.assertEqual(overlap_pairs(placed, clearance=0.2), [])
        self.assertEqual(outside_outline(placed, 30.0, 20.0), [])
        self.assertLessEqual(abs(placed.component("U1").pos[0] - 12.0), GRID / 2 + 1e-9)
        self.assertLessEqual(abs(placed.component("U1").pos[1] - 10.0), GRID / 2 + 1e-9)
        record = placed.legal_motion
        self.assertEqual((record["severe"], record["relocated"]), (1, 1))
        self.assertEqual(record["moved"], 1)
        self.assertGreater(record["parts"]["C1"][0], 2.0)

    def test_no_room_falls_back_to_the_plain_packer(self):
        # Two parts that only fit in the outline as the packer lays them out.
        g = board(
            [
                part("U1", ["A"], (3.5, 2.5), size=(6, 4)),
                part("U2", ["A"], (3.5, 2.5), size=(6, 4)),
            ],
            7,
            9,
        )
        placed = run(g)
        self.assertEqual(overlap_pairs(placed, clearance=0.2), [])
        self.assertIn("keep", placed.legal_motion)


class MotionTest(unittest.TestCase):
    def test_counts_distance_turns_and_topology(self):
        before = {"A": (0, 0, 0), "B": (10, 0, 0), "C": (5, 5, 90)}
        after = {"A": (0.1, 0, 0), "B": (-2, 0, 0), "C": (5, 5, 270)}
        m = motion(before, after)
        self.assertEqual(m["count"], 3)
        self.assertEqual(m["moved"], 2)  # B displaced, C turned; A within the snap
        self.assertEqual(m["turned"], 1)
        self.assertAlmostEqual(m["sum_mm"], 12.1)
        self.assertAlmostEqual(m["max_mm"], 12.0)
        # Pairs: A-B x flips, A-B y tie (skipped); A-C x, y kept; B-C x flips, y kept: 3 of 5.
        self.assertAlmostEqual(m["topology"], 0.6)
        total = combine([m, dict(m, sum_mm=1.0, max_mm=20.0)])
        self.assertEqual(total["count"], 6)
        self.assertEqual(total["max_mm"], 20.0)
        self.assertAlmostEqual(total["topology"], 0.6)

    def test_occlusion(self):
        g = board(
            [
                part("R1", ["A"], (5.0, 5.0)),
                part("R2", ["A"], (5.0, 5.0)),
                part("R3", ["A"], (0.5, 15.0)),
            ]
        )
        occ = occlusion(g.components, 30, 20, clearance=0.0, movable=["R1", "R2", "R3"])
        self.assertEqual(occ["R1"], 1.0)
        self.assertAlmostEqual(occ["R3"], 0.25)  # a quarter of it lies off the board
        self.assertEqual(
            occlusion(g.components, 30, 20, clearance=0.0, movable=["R3"], ignore=["R1", "R2"])[
                "R3"
            ],
            0.25,
        )


class SpreadTest(unittest.TestCase):
    def box(self, ref, x, y, w=4.0, h=4.0, bounds=(2, 28, 2, 18)):
        return keepmod.Box(ref, x, y, w, h, ("top",), weight=w * h, bounds=bounds)

    def test_least_squares_push_keeps_the_order(self):
        slots = [self.box("A", 5, 5), self.box("B", 8.5, 5), self.box("C", 12.5, 5)]
        centres, bad = keepmod.spread(slots, [])
        self.assertEqual(bad, [])
        self.assertAlmostEqual(centres["B"][0] - centres["A"][0], 4.0, places=4)
        self.assertGreaterEqual(centres["C"][0] - centres["B"][0], 4.0 - 1e-4)
        self.assertAlmostEqual(centres["A"][0], 5 - 1 / 3, places=3)
        self.assertAlmostEqual(centres["C"][1], 5.0)

    def test_a_channel_need_widens_the_push(self):
        # A and B just apart along x; a channel of 1 mm between them pushes them apart, C
        # (side by side with B, which moves) is pushed on in order; with a new conflict the solve
        # repeats with the need too.
        slots = [self.box("A", 5, 5), self.box("B", 9, 5), self.box("C", 13.2, 5)]

        def need(front, back, axis):
            return 5.0 if (front, back, axis) == ("A", "B", 0) else None

        centres, bad = keepmod.spread(slots, [], need=need)
        self.assertEqual(bad, [])
        self.assertAlmostEqual(centres["B"][0] - centres["A"][0], 5.0, places=3)
        self.assertGreaterEqual(centres["C"][0] - centres["B"][0], 4.0 - 1e-4)
        moved, drop = keepmod.resolve(slots, [], 30, 20, need=need, short={"A"})
        self.assertEqual(drop, [])
        self.assertAlmostEqual(moved["B"][0] - moved["A"][0], 5.0, places=3)
        # Without a short part or an overlap the cluster is left as it is.
        still, _ = keepmod.resolve(slots, [], 30, 20, need=need)
        self.assertEqual(still["B"], (9.0, 5.0))

    def test_an_obstacle_does_not_move_and_a_bound_holds(self):
        wall = keepmod.Box(None, 1.0, 5.0, 2.0, 10.0, ("top",))
        slots = [self.box("A", 3.5, 5, bounds=(2, 28, 2, 18))]
        centres, bad = keepmod.spread(slots, [wall])
        self.assertEqual(bad, [])
        self.assertAlmostEqual(centres["A"][0], 4.0, places=4)

    def test_triage_takes_the_buried_part(self):
        slots = [self.box("U1", 10, 10, 8, 6), self.box("C1", 10, 10, 1, 1)]
        severe, occ = keepmod.triage(slots, [], 30, 20)
        self.assertEqual(severe, ["C1"])
        self.assertEqual(occ["U1"], 0.0)

    def test_a_push_beyond_its_reach_relocates_the_worst_part(self):
        # A part squeezed between two walls with no room: spreading fails, it is relocated.
        walls = [
            keepmod.Box(None, 3.0, 10.0, 6.0, 20.0, ("top",)),
            keepmod.Box(None, 11.0, 10.0, 6.0, 20.0, ("top",)),
        ]
        slots = [self.box("A", 6.0, 10.0)]
        centres, drop = keepmod.resolve(slots, walls, 30, 20)
        self.assertEqual(drop, ["A"])


if __name__ == "__main__":
    unittest.main()
