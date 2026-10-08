"""PNR_LEGALIZE_KEEP: displacement-minimizing legalization (pnr.place.keep, pnr.place.motion).

A part or a block macro that is legal at its global pose keeps it (a grid snap) and its turn, even
with the wirelength term and its turn search on; a slight overlap is resolved by pushing the
parts apart in their global order (the neighbour beyond is pushed too, nothing else moves); a
part buried under another is relocated while the other stays; a part the push cannot clear or
whose slot is taken takes the nearest free slot around its pose (never the full search), and the
record names why; routing channels are a soft goal that never makes the push give up (the
hier-twin-bank-32 start-13 block: R6 overlapping D4 stays put); ``PNR_LEGALIZE_KEEP=0`` restores
the plain packer; the motion record (moved count, displacement, topology kept) and the triage
counts; the spreading solver on its own.
"""

from __future__ import annotations

import json
import math
import os
import unittest
from pathlib import Path
from unittest import mock

from pnr import legalize_flags
from pnr.graph import BoardGraph, BoardOutline, Component, Net, Pad
from pnr.place import keep as keepmod
from pnr.place.geometry import courtyard_rect
from pnr.place.legalize import legalize
from pnr.place.metrics import outside_outline, overlap_pairs
from pnr.place.motion import combine, motion, neighbour_order, occlusion

GRID = 0.25
DATA = Path(__file__).resolve().parent.parent / "testdata" / "legalize_keep"


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


class NearestFirstTest(unittest.TestCase):
    def test_a_part_the_push_cannot_clear_lands_nearest_its_pose(self):
        # A sits in a 2 mm gap between two fixed parts, just over F1: no push clears it (the
        # fixed parts do not move), so it takes the nearest free slot (here a quarter turn in the
        # gap, nearer than any slot at its own turn), not a slot the full search would score (the
        # wire term pulls it to R1 across the board).
        g = board(
            [
                part("F1", ["A", "B"], (6.0, 10.0), size=(4.0, 4.0)),
                part("F2", ["B", "C"], (12.0, 10.0), size=(4.0, 4.0)),
                part("A", ["C", "D"], (8.6, 10.0), size=(2.4, 1.0)),
                part("R1", ["D", "A"], (26.0, 3.0)),
            ]
        )
        placed = run(g, keep=True, fixed=("F1", "F2"), wire_weight=4.0)
        self.assertEqual(overlap_pairs(placed, clearance=0.2), [])
        record = placed.legal_motion
        self.assertEqual(record["relocations"], {"A": "push_infeasible"})
        self.assertEqual(
            (record["relocated_push_infeasible"], record["relocated_severe"], record["nudged"]),
            (1, 0, 1),
        )
        a, b = g.component("A"), placed.component("A")
        self.assertLess(math.dist(a.pos, b.pos), 1.0)

    def test_a_part_whose_slot_is_taken_lands_nearest_its_pose(self):
        # With the push switched off, the smaller of two overlapping parts finds its slot taken
        # by the bigger one (anchored first) and takes the nearest free slot at its turn.
        g = board(
            [
                part("U1", ["A", "B"], (10.0, 10.0), size=(4.0, 3.0)),
                part("R1", ["B", "C"], (12.2, 10.0), size=(2.0, 1.0), rot=90.0),
                part("R2", ["C", "A"], (26.0, 3.0)),
            ]
        )

        def unpushed(slots, obstacles, *a, **kw):
            return {s.ref: (s.x, s.y) for s in slots}, []

        with mock.patch.object(keepmod, "resolve", unpushed):
            placed = run(g, keep=True, wire_weight=4.0)
        self.assertEqual(overlap_pairs(placed, clearance=0.2), [])
        record = placed.legal_motion
        self.assertEqual(record["relocations"], {"R1": "slot_taken"})
        self.assertEqual(record["relocated_slot_taken"], 1)
        self.assertLessEqual(math.dist(placed.component("U1").pos, (10.0, 10.0)), GRID)
        self.assertLess(math.dist(placed.component("R1").pos, (12.2, 10.0)), 2.0)
        self.assertEqual(placed.component("R1").rot, 90.0)


class BlockRegressionTest(unittest.TestCase):
    """hier-twin-bank-32 seed 0, block top.bank_a trial start-13 (testdata/legalize_keep): R6
    overlaps D4 slightly at its global pose, and every part is short of an escape channel. The
    push used to give up (the channels exceeded the cascade bound) and R6 went to the full search,
    10.7 mm away, north of the SOIC, with a quarter turn."""

    def legalize(self, keep, name="hier-twin-bank-32-start-13.json"):
        from pnr.place.channels import ChannelModel

        doc = json.loads((DATA / name).read_text())
        g = BoardGraph.from_json(json.dumps(doc["graph"]))
        kw = dict(doc["legalize"], pad_edge=tuple(doc["legalize"]["pad_edge"]))
        with mock.patch.dict(os.environ, {"PNR_COMPACT": "1"}):
            out = legalize(
                g,
                doc["width"],
                doc["height"],
                channel_model=ChannelModel(g, doc["rules"]),
                fixed={},
                keepouts=[],
                keep=keep,
                **kw,
            )
            self.overlaps = overlap_pairs(out, clearance=0.0)
        return g, out

    def test_r6_stays_by_its_global_pose(self):
        g, placed = self.legalize(True)
        self.assertEqual(self.overlaps, [])
        self.assertEqual(outside_outline(placed, g.outline.width, g.outline.height), [])
        before, after = g.component("R6"), placed.component("R6")
        rect = courtyard_rect(before)
        width = max(rect.w, rect.h)
        self.assertLessEqual(math.dist(before.pos, after.pos), width)
        self.assertEqual(after.rot, before.rot)
        record = placed.legal_motion
        self.assertEqual(record["relocated"], 0)
        self.assertGreater(record["pushed"], 0)
        self.assertGreater(record["channel_short"], 0)
        self.assertLess(record["max_mm"], 2.0)

    def test_start_14_d4_is_pushed_not_relocated(self):
        """Start-14 of the same block: the column R3, R6, R4, U2, D4, C5, D3 is 0.12 mm too tall
        for the outline when every pair is pushed apart along its smaller penetration, so the
        push used to fail and D4 went 10.1 mm east (nearest-first); a pair moved to its other
        axis (D4 beside C5) makes the push feasible."""
        g, placed = self.legalize(True, "hier-twin-bank-32-start-14.json")
        self.assertEqual(self.overlaps, [])
        before, after = g.component("D4"), placed.component("D4")
        self.assertLess(math.dist(before.pos, after.pos), 3.0)
        self.assertEqual(after.rot, before.rot)
        record = placed.legal_motion
        self.assertEqual(record["relocated"], 0)
        self.assertLess(record["max_mm"], 3.0)

    def test_the_plain_packer_still_differs(self):
        # The fixture still exercises the case: the plain packer moves R6 far.
        g, placed = self.legalize(False)
        self.assertGreater(math.dist(g.component("R6").pos, placed.component("R6").pos), 2.0)


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
        # Three parts are each other's neighbours: the same five relations.
        self.assertAlmostEqual(m["neighbour_order"], 0.6)
        self.assertEqual(m["neighbour_relations"], 5)
        self.assertAlmostEqual(total["neighbour_order"], 0.6)
        self.assertEqual(total["neighbour_relations"], 10)

    def test_neighbour_order_counts_near_pairs_only(self):
        before = {"A": (0, 0, 0), "B": (1, 0, 0), "C": (2, 0, 0), "D": (10, 0, 0)}
        after = dict(before, D=(-5, 0, 0))
        # Nearest neighbour only: A-B, B-C and C-D; C-D flips. Over all pairs, D's three flip.
        self.assertEqual(neighbour_order(before, after, sorted(before), k=1), (2, 3))
        self.assertAlmostEqual(motion(before, after)["topology"], 0.5)
        # A side-by-side pair's small vertical offset (under a quarter of its distance) is no
        # relation: only x counts here, and it is kept.
        self.assertEqual(
            neighbour_order(
                {"A": (0, 0, 0), "B": (4, 0.5, 0)}, {"A": (0, 0, 0), "B": (4, -0.5, 0)}, "AB"
            ),
            (1, 1),
        )

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
        opened = {}
        moved, drop = keepmod.resolve(slots, [], 30, 20, need=need, short={"A"}, opened=opened)
        self.assertEqual(drop, [])
        self.assertAlmostEqual(moved["B"][0] - moved["A"][0], 5.0, places=3)
        self.assertEqual(set(opened.values()), {True})
        # Without a short part or an overlap the cluster is left as it is.
        still, _ = keepmod.resolve(slots, [], 30, 20, need=need)
        self.assertEqual(still["B"], (9.0, 5.0))

    def test_a_channel_is_a_soft_goal_that_never_gives_a_part_up(self):
        # A overlaps B by 0.5 mm between two walls with 1.5 mm to spare: the overlap is cleared,
        # but the 4 mm channel A-B asks cannot open within the walls, so the overlap-only push
        # stands and no part is given up.
        walls = [
            keepmod.Box(None, 1.0, 10.0, 2.0, 20.0, ("top",)),
            keepmod.Box(None, 12.5, 10.0, 2.0, 20.0, ("top",)),
        ]
        slots = [self.box("A", 4.5, 10.0), self.box("B", 8.0, 10.0)]

        def need(front, back, axis):
            return 8.0 if (front, back, axis) == ("A", "B", 0) else None

        _, bad = keepmod.spread(slots, walls, need=need)
        self.assertNotEqual(bad, [])  # the whole channel is out of reach
        opened = {}
        moved, drop = keepmod.resolve(slots, walls, 30, 20, need=need, opened=opened)
        self.assertEqual(drop, [])
        self.assertAlmostEqual(moved["B"][0] - moved["A"][0], 4.0, places=3)  # overlap cleared
        self.assertEqual(set(opened.values()), {False})
        # Without the overlap (B just clear of A) and A short of the channel, nothing moves.
        clear = [self.box("A", 4.5, 10.0), self.box("B", 8.6, 10.0)]
        still, drop = keepmod.resolve(clear, walls, 30, 20, need=need, short={"A"})
        self.assertEqual((still["A"], still["B"], drop), ((4.5, 10.0), (8.6, 10.0), []))

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
