"""PNR_DETAIL_PLACE: detailed placement after legalization (pnr.place.detail; docs/design/
compact-placement.md section 13.H).

Each move kind on a board built for it (a turn, a slide toward the nets, a blocked slide that
shifts the part in front along, a swap of two parts pulled across each other, a part moved from
the far side of a bigger part to the side of the pin it connects to), the movement budget, the
parts that never move or only turn and slide, legality, the crossing estimate's pieces, the
switch, the placer's stage and its record, and determinism (the same bits twice, a golden hash).
"""

from __future__ import annotations

import hashlib
import math
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import compact_fixture as fixture  # noqa: E402

from pnr import legalize_flags  # noqa: E402
from pnr.constraints import compile_constraints  # noqa: E402
from pnr.graph import BoardGraph, BoardOutline, Component, Net, Pad  # noqa: E402
from pnr.place import detail  # noqa: E402
from pnr.place.metrics import hard_violations, overlap_pairs  # noqa: E402

W, H = 30.0, 16.0


def two_pad(ref, nets, pos, rot=0.0, size=(2.0, 1.0), footprint="R_0603"):
    pads = [
        Pad("1", nets[0], (-0.6, 0.0), (0.5, 0.5)),
        Pad("2", nets[1], (0.6, 0.0), (0.5, 0.5)),
    ]
    return Component(ref, footprint, pos, rot, "top", size, size, pads=pads)


def pin(ref, net, pos):
    pads = [Pad("1", net, (0.0, 0.0), (0.4, 0.4))]
    return Component(ref, "conn", pos, 0.0, "top", (1.0, 1.0), (1.0, 1.0), pads=pads)


def board(parts, fixed=(), **spec):
    nets = {}
    for comp in parts:
        for pad in comp.pads:
            if pad.net:
                nets.setdefault(pad.net, []).append((comp.ref, pad.name))
    g = BoardGraph(
        "detail",
        list(parts),
        [Net(n, i + 1, p) for i, (n, p) in enumerate(sorted(nets.items()))],
        BoardOutline(W, H),
    )
    doc = dict(
        schema="v0",
        board=dict(outline=dict(w=W, h=H), layers=2, default_clearance_mm=0.2),
        fixed={
            r: dict(at=list(g.component(r).pos), rot=g.component(r).rot, side="top") for r in fixed
        },
    )
    doc.update(spec)
    return g, compile_constraints(doc, g.refs)


def improve(g, cc, **kwargs):
    """The pass with the legalizer's settings of these boards; the move tests leave the
    compaction out (``compact_rounds=0``) to see each greedy move on its own."""
    kwargs.setdefault("clearance", 0.2)
    kwargs.setdefault("grid_mm", 0.25)
    kwargs.setdefault("compact_rounds", 0)
    return detail.improve_detail(g, cc, **kwargs)


def moved(before, after, ref):
    return math.dist(before.component(ref).pos, after.component(ref).pos)


def legal(test, out, cc):
    test.assertFalse(any(hard_violations(out, cc).values()))
    test.assertEqual(overlap_pairs(out, clearance=0.2), [])


class MoveTest(unittest.TestCase):
    def test_a_turn_puts_the_pads_toward_their_nets(self):
        # R1's pad on A faces away from J1 (east) and its pad on B away from J2 (west).
        g, cc = board(
            [
                two_pad("R1", ["A", "B"], (15.0, 8.0)),
                pin("J1", "A", (24, 8)),
                pin("J2", "B", (6, 8)),
            ],
            fixed=("J1", "J2"),
        )
        out, record = improve(g, cc)
        self.assertEqual(out.component("R1").rot, 180.0)
        self.assertEqual(record["moves"]["turn"], 1)
        self.assertEqual(out.component("R1").pos, (15.0, 8.0))  # about its centre
        self.assertAlmostEqual(record["hpwl_before"] - record["hpwl_after"], 2.4)
        legal(self, out, cc)

    def test_a_slide_toward_the_nets_stops_at_the_budget(self):
        # Both of R1's nets end 9 and 11 mm east: it slides east, as far as its budget.
        g, cc = board(
            [
                two_pad("R1", ["A", "B"], (10.0, 8.0)),
                pin("J1", "A", (19, 8)),
                pin("J2", "B", (21, 8)),
            ],
            fixed=("J1", "J2"),
        )
        out, record = improve(g, cc)
        budget = detail.reach(g.component("R1"))
        self.assertEqual(budget, 2.0)
        self.assertAlmostEqual(out.component("R1").pos[0] - 10.0, budget)
        self.assertEqual(out.component("R1").pos[1], 8.0)
        self.assertGreater(record["moves"]["slide"], 0)
        self.assertLessEqual(record["max_mm"], budget + 1e-9)
        self.assertEqual(record["topology"], 1.0)
        legal(self, out, cc)

    def test_a_blocked_slide_shifts_the_part_in_front_along(self):
        # R2 sits 0.6 mm east of R1, which is pulled east; R2's wiring is the same anywhere
        # along x (one net west, one east), so it only moves when R1 pushes it along (it is
        # longer, so the two never swap).
        parts = [
            two_pad("R1", ["A", "B"], (10.0, 8.0)),
            two_pad("R2", ["C", "D"], (13.1, 8.0), size=(3.0, 1.0), footprint="R_1206"),
            pin("J1", "A", (20, 8)),
            pin("J2", "B", (20, 10)),
            pin("J3", "C", (3, 12)),
            pin("J4", "D", (25, 12)),
        ]
        g, cc = board(parts, fixed=("J1", "J2", "J3", "J4"))
        out, record = improve(g, cc)
        self.assertGreater(moved(g, out, "R1"), 0.6)  # beyond the 0.4 mm R1 had on its own
        self.assertLess(out.component("R1").pos[0], out.component("R2").pos[0])  # order kept
        legal(self, out, cc)

    def test_two_parts_pulled_across_each_other_swap(self):
        # R1 is wired west of R2, R2 east of R1: neither slide helps, the swap does.
        parts = [
            two_pad("R1", ["A", "B"], (13.0, 8.0)),
            two_pad("R2", ["C", "D"], (15.6, 8.0)),
            pin("J1", "A", (25, 8)),
            pin("J2", "B", (27, 8)),
            pin("J3", "C", (3, 8)),
            pin("J4", "D", (5, 8)),
        ]
        g, cc = board(parts, fixed=("J1", "J2", "J3", "J4"))
        out, record = improve(g, cc)
        self.assertEqual(record["moves"]["swap"], 1)
        self.assertGreater(out.component("R1").pos[0], out.component("R2").pos[0])
        legal(self, out, cc)

    def test_a_part_on_the_far_side_of_a_bigger_part_moves_to_its_pin(self):
        g, cc = self.buck_like()
        before = detail._State(g, cc, frozenset(), None, 0.0)
        self.assertGreater(before.totals()[1], 0.0)  # FB crosses U1's own body
        out, record = improve(g, cc)
        self.assertEqual(record["moves"]["wrong_side"], 1)
        r1, u1 = out.component("R1"), out.component("U1")
        self.assertGreater(r1.pos[0], u1.pos[0] + 2.5)  # east of U1, by its FB pin
        self.assertLess(record["crossing_after"], 1e-9)
        budget = 5.0 + 1.0 + 2.0 + 2 * 0.25  # U1's extent, the gap, R1's own, two cells
        self.assertLessEqual(moved(g, out, "R1"), budget + 1e-9)
        legal(self, out, cc)

    def test_the_wrong_side_move_keeps_the_pins_channel_open(self):
        class Model:
            classes = {}

            def penalty(self, comp, others, x, y):
                # Any part east of U1 closes its FB pin's escape.
                if comp.ref == "U1" and any(o.ref == "R1" and o.pos[0] > 12.0 for o in others):
                    return 1.0
                return 0.0

        g, cc = self.buck_like()
        out, record = improve(g, cc, channel_model=Model(), ratio=0.0)
        self.assertEqual(record["moves"]["wrong_side"], 0)
        self.assertLess(out.component("R1").pos[0], 10.0)

    def test_a_ball_array_has_no_far_side(self):
        # The same board with U1 a 6 x 6 ball grid (FB an edge ball): its routes drop through
        # its fanout vias, so no part moves to its other side.
        g, cc = self.buck_like()
        u1 = g.component("U1")
        pads = [p for p in u1.pads if p.net == "FB"]
        for i in range(6):
            for j in range(6):
                if (i, j) != (5, 2):
                    pads.append(
                        Pad("B%d%d" % (i, j), "", (-1.9 + 0.76 * i, -1.9 + 0.76 * j), (0.3, 0.3))
                    )
        u1.pads = pads
        st = detail._State(g, cc, frozenset(), None, 0.0)
        self.assertTrue(st.array["U1"])
        out, record = improve(g, cc)
        self.assertEqual(record["moves"]["wrong_side"], 0)

    def test_two_rows_of_pads_are_no_array(self):
        # An SOIC-16: its end pads sit nearer the short edges than the others, but two rows are
        # no ball grid.
        pads = [
            Pad(str(i), "", (-2.475 if i < 8 else 2.475, (i % 8) * 1.27 - 4.445), (1.95, 0.6))
            for i in range(16)
        ]
        u2 = Component(
            "U2", "SOIC-16", (12.0, 8.0), 0.0, "top", (7.4, 10.4), (7.4, 10.4), pads=pads
        )
        g, cc = board([u2, pin("J1", "", (3.0, 3.0))], fixed=("J1",))
        self.assertFalse(detail._State(g, cc, frozenset(), None, 0.0).array["U2"])

    def test_a_part_keeps_out_of_a_ball_arrays_escape_field(self):
        # R1 3.5 mm west of a ball array U1 and wired to its edge ball: the wirelength draws it
        # toward U1, but not into its 1.5 mm escape field.
        u1 = Component(
            "U1",
            "BGA",
            (12.0, 8.0),
            0.0,
            "top",
            (5.0, 5.0),
            (5.0, 5.0),
            pads=[
                Pad(
                    "B%d%d" % (i, j),
                    "SIG" if (i, j) == (0, 2) else "",
                    (-1.9 + 0.76 * i, -1.9 + 0.76 * j),
                    (0.3, 0.3),
                )
                for i in range(6)
                for j in range(6)
            ],
        )
        r1 = two_pad("R1", ["SIG", "X"], (5.0, 8.0))
        g, cc = board([u1, r1, pin("J1", "X", (9.0, 13.0))], fixed=("U1", "J1"))
        before = detail._State(g, cc, frozenset(), None, 0.0)
        gap0 = detail._gap(before.box["R1"], before.box["U1"])
        self.assertAlmostEqual(gap0, 3.5)
        self.assertFalse(before.by_array("R1"))
        for rounds in (0, detail.COMPACT_ROUNDS):
            out, record = improve(g, cc, compact_rounds=rounds)
            after = detail._State(out, cc, frozenset(), None, 0.0)
            gap = detail._gap(after.box["R1"], after.box["U1"])
            self.assertLess(gap, gap0)  # it did move closer
            self.assertGreaterEqual(gap, detail.ARRAY_ZONE_MM - 1e-9)
        # Within 3 mm of the array a part stays where it is.
        g.component("R1").pos = (7.0, 8.0)
        out, record = improve(g, cc)
        self.assertEqual(out.component("R1").pos, (7.0, 8.0))
        self.assertEqual(record["movable"], 0)

    def test_a_part_keeps_clear_of_a_pairs_corridor(self):
        # A differential pair runs straight between J1 and J2 at y 8 to 9; R1 is pulled south
        # across it, and stops 1 mm short of the pair's line.
        parts = [
            pin("J1", "P", (5.0, 8.0)),
            pin("J2", "N", (5.0, 10.0)),
            pin("J3", "P", (25.0, 8.0)),
            pin("J4", "N", (25.0, 10.0)),
            two_pad("R1", ["X", "Y"], (15.0, 13.0)),
            pin("J5", "X", (14.0, 2.0)),
            pin("J6", "Y", (16.0, 2.0)),
        ]
        g, cc = board(
            parts,
            fixed=("J1", "J2", "J3", "J4", "J5", "J6"),
            diff_pair=[dict(name="d", p="P", n="N")],
        )
        st = detail._State(g, cc, frozenset(), None, 0.0, matched=detail.matched_nets(cc))
        self.assertEqual(len(st.corridors), 2)
        for rounds in (0, detail.COMPACT_ROUNDS):
            out, record = improve(g, cc, compact_rounds=rounds)
            r1 = detail._box(out.component("R1"))
            self.assertLess(r1[2], 12.5)  # it did move south
            self.assertGreaterEqual(r1[2], 10.0 + detail.CORRIDOR_MM - 1e-9)

    @staticmethod
    def buck_like():
        """U1 (5 x 5 mm, fixed) with its FB pin on its east edge; R1 west of U1 between FB and
        a pin north-west: the FB route crosses U1's body (the 11-buck-pour R1/U1 case)."""
        u1_pads = [
            Pad("FB", "FB", (2.2, 0.0), (0.6, 0.3)),
            Pad("SW", "SW", (-2.2, 1.5), (0.6, 0.3)),
            Pad("GND", "GND", (0.0, -2.2), (0.3, 0.6)),
        ]
        u1 = Component("U1", "QFN", (10.0, 8.0), 0.0, "top", (5.0, 5.0), (5.0, 5.0), pads=u1_pads)
        r1 = two_pad("R1", ["FB", "VS"], (5.5, 8.0), rot=180.0)
        return board(
            [
                u1,
                r1,
                pin("J1", "VS", (10.0, 14.0)),
                pin("J2", "SW", (3.0, 12.0)),
                pin("J3", "GND", (10.0, 2.0)),
            ],
            fixed=("U1", "J1", "J2", "J3"),
        )


class BuckTest(unittest.TestCase):
    """11-buck-vqfnhr-4L-SGPS-pour seed 5 with KEEP alone (testdata/detail_place): R1, the FB
    divider, sits west of U1 while U1's FB pin faces east, so the FB route runs across U1's own
    body (and GND pin 7 lost its pad entry on that seed). Detailed placement takes R1 off U1's
    far side: the FB connection no longer crosses U1."""

    def test_r1_no_longer_crosses_u1(self):
        import json

        from pnr.place.channels import ChannelModel
        from pnr.place.sides import with_policy

        doc = json.loads((HERE.parent / "testdata/detail_place/buck-pour-s5.json").read_text())
        g = BoardGraph.from_json(json.dumps(doc["graph"]))
        cc = compile_constraints(with_policy(doc["constraints"], doc["sides"]), doc["refs"])
        kw = dict(doc["detail"])
        kw["pad_edge"] = tuple(kw["pad_edge"])
        with mock.patch.dict(os.environ, {"PNR_COMPACT": "1"}):
            model = ChannelModel(g, doc["rules"])
            skip = detail.plane_nets_of(model)
            before = detail._State(g, cc, skip, model, 6.25)
            self.assertGreater(self.fb_crossing(before), 0.5)
            out, record = detail.improve_detail(g, cc, channel_model=model, **kw)
            after = detail._State(out, cc, skip, model, 6.25)
            self.assertEqual(self.fb_crossing(after), 0.0)
            self.assertFalse(any(hard_violations(out, cc).values()))
        self.assertLess(record["crossing_after"], record["crossing_before"])
        self.assertGreater(math.dist(g.component("R1").pos, out.component("R1").pos), 0.4)
        self.assertEqual(g.component("U1").pos, out.component("U1").pos)

    @staticmethod
    def fb_crossing(st):
        """The detour of the FB segments that end on U1's FB pin, round U1's own body."""
        total = 0.0
        for bbox, a, b, pa, pb, _c in st.segs["FB"]:
            for ref, xy in ((a[0], pa), (b[0], pb)):
                if ref == "U1":
                    face = detail.facing(st.box["U1"], xy)
                    if face is not None:
                        total += detail.detour(bbox, detail.own_rect(st.box["U1"], xy, face))
        return total


class CompactTest(unittest.TestCase):
    def test_compaction_closes_up_a_row_in_its_order(self):
        # Four resistors spread along a row, all wired to J1 and each to its own pin, all near
        # the middle: compaction pulls them together, in order, within its budget, legally.
        parts = [two_pad("R%d" % i, ["A", "B%d" % i], (4.0 + 6.0 * i, 8.0)) for i in range(4)]
        parts += [pin("J1", "A", (13.0, 2.0))]
        parts += [pin("J%d" % (i + 2), "B%d" % i, (10.0 + 2.0 * i, 14.0)) for i in range(4)]
        g, cc = board(parts, fixed=("J1", "J2", "J3", "J4", "J5"))
        out, record = improve(g, cc, compact_rounds=4)
        self.assertEqual(record["compact"], 4)
        self.assertEqual(record["moves"]["compact"], 4)
        self.assertLess(record["hpwl_after"], record["hpwl_before"])
        xs = [out.component("R%d" % i).pos[0] for i in range(4)]
        self.assertEqual(xs, sorted(xs))  # order kept
        for i in range(4):
            d = out.component("R%d" % i).pos[0] - g.component("R%d" % i).pos[0]
            self.assertLessEqual(abs(d), detail.COMPACT_REACH_MM + 1e-6)
        self.assertEqual(record["topology"], 1.0)
        legal(self, out, cc)


class BudgetAndExclusionTest(unittest.TestCase):
    def setUp(self):
        self.parts = lambda: [
            two_pad("R1", ["A", "B"], (10.0, 8.0)),
            pin("J1", "A", (19, 8)),
            pin("J2", "B", (21, 8)),
        ]

    def test_excluded_parts_never_move(self):
        cases = dict(
            fixed=dict(fixed=("R1", "J1", "J2")),
            rotation=dict(fixed=("J1", "J2"), orientation={"R1": 0}),
            pair=dict(fixed=("J1", "J2"), diff_pair=[dict(name="p", p="A", n="B")]),
            matched=dict(
                fixed=("J1", "J2"),
                length_match=[dict(name="m", nets=["A", "B"], tolerance_mm=1.0)],
            ),
        )
        for name, extra in cases.items():
            with self.subTest(name):
                g, cc = board(self.parts(), **extra)
                out, record = improve(g, cc)
                self.assertEqual(out.to_json(), g.to_json())
                self.assertEqual(sum(record["moves"].values()), 0)
        for footprint in ("block:b", "line:l"):
            with self.subTest(footprint):
                g, cc = board(self.parts(), fixed=("J1", "J2"))
                g.component("R1").footprint = footprint
                out, record = improve(g, cc)
                self.assertEqual(record["movable"], 0)
                self.assertEqual(out.to_json(), g.to_json())

    def test_a_held_part_slides_but_is_never_swapped(self):
        parts = [
            two_pad("R1", ["A", "B"], (13.0, 8.0)),
            two_pad("R2", ["C", "D"], (15.6, 8.0)),
            pin("J1", "A", (25, 8)),
            pin("J2", "B", (27, 8)),
            pin("J3", "C", (3, 8)),
            pin("J4", "D", (5, 8)),
        ]
        g, cc = board(
            parts,
            fixed=("J1", "J2", "J3", "J4"),
            group=[dict(name="g", anchor="J1", members=["R1"], radius_mm=13.0, hard=True)],
        )
        self.assertIn("R1", detail.held_refs(cc))
        out, record = improve(g, cc)
        self.assertEqual(record["moves"]["swap"], 0)
        self.assertLess(out.component("R1").pos[0], out.component("R2").pos[0])
        legal(self, out, cc)

    def test_an_illegal_board_is_left_alone(self):
        g, cc = board(self.parts() + [two_pad("R9", ["A", "B"], (10.5, 8.0))], fixed=("J1", "J2"))
        out, record = improve(g, cc)
        self.assertEqual(record.get("skipped"), "illegal_input")
        self.assertEqual(out.to_json(), g.to_json())

    def test_no_rotation_no_turns(self):
        g, cc = board(
            [
                two_pad("R1", ["A", "B"], (15.0, 8.0)),
                pin("J1", "A", (24, 8)),
                pin("J2", "B", (6, 8)),
            ],
            fixed=("J1", "J2"),
        )
        out, record = improve(g, cc, allow_rotation=False)
        self.assertEqual(out.component("R1").rot, 0.0)
        self.assertEqual(record["moves"]["turn"], 0)


class PieceTest(unittest.TestCase):
    def test_detour_round_a_wall(self):
        # A 4 mm tall wall across a horizontal route: round its nearer end, twice.
        self.assertEqual(detail.detour((0, 10, 5, 5), (4, 6, 4, 8)), 2.0)
        self.assertEqual(detail.detour((0, 10, 5, 5), (4, 6, 6, 8)), 0.0)  # beside the route
        # A diagonal route goes round a corner block at no cost (another staircase).
        self.assertEqual(detail.detour((0, 10, 0, 10), (4, 6, 4, 6)), 0.0)
        # A block cutting the whole box in x: round its left or right end.
        self.assertEqual(detail.detour((2, 8, 0, 10), (1, 9, 4, 6)), 2.0)

    def test_facing_and_the_part_behind_a_pin(self):
        box = (0.0, 5.0, 0.0, 5.0)
        self.assertEqual(detail.facing(box, (4.8, 2.5)), 0)  # east
        self.assertEqual(detail.facing(box, (2.5, 0.1)), 3)  # south
        self.assertIsNone(detail.facing(box, (2.5, 2.5)))  # an inner pad
        rect = detail.own_rect(box, (4.8, 2.5), 0)
        self.assertLess(rect[1], 4.8)
        self.assertEqual(detail.detour((-3.0, 4.8, 2.5, 2.5), rect), 5.0 - 2 * 0.05)

    def test_mst(self):
        self.assertEqual(detail.mst([(0, 0), (5, 0), (1, 0)]), [(0, 2), (2, 1)])
        self.assertEqual(detail.mst([(0, 0)]), [])

    def test_quarter_turn_pins_are_exact(self):
        comp = two_pad("R1", ["A", "B"], (1.1, 2.3), rot=90.0)
        self.assertEqual(detail.pins_of(comp), [(1.1, 2.3 - 0.6), (1.1, 2.3 + 0.6)])


class SwitchTest(unittest.TestCase):
    def test_the_switch(self):
        """On by default with KEEP (not listed), off with ``0`` (listed), off with KEEP off
        unless ``1``."""
        with mock.patch.dict(os.environ, {"PNR_DETAIL_PLACE": "", "PNR_LEGALIZE_KEEP": ""}):
            self.assertTrue(legalize_flags.detail_place())
            self.assertNotIn("DETAIL_PLACE", legalize_flags.active())
        with mock.patch.dict(os.environ, {"PNR_DETAIL_PLACE": "0", "PNR_LEGALIZE_KEEP": ""}):
            self.assertFalse(legalize_flags.detail_place())
            self.assertIs(legalize_flags.active()["DETAIL_PLACE"], False)
        with mock.patch.dict(os.environ, {"PNR_DETAIL_PLACE": "", "PNR_LEGALIZE_KEEP": "0"}):
            self.assertFalse(legalize_flags.detail_place())
            self.assertNotIn("DETAIL_PLACE", legalize_flags.active())
        with mock.patch.dict(os.environ, {"PNR_DETAIL_PLACE": "1", "PNR_LEGALIZE_KEEP": "0"}):
            self.assertTrue(legalize_flags.detail_place())
            self.assertIs(legalize_flags.active()["DETAIL_PLACE"], True)
        with mock.patch.dict(os.environ, {"PNR_DETAIL_PLACE": "yes"}):
            with self.assertRaises(ValueError):
                legalize_flags.detail_place()


class PlacerTest(unittest.TestCase):
    def place(self, value):
        from pnr.place import placer

        graph, constraints, rules = fixture.load("04-inverter-leds-8")
        with mock.patch.dict(os.environ, {"PNR_DETAIL_PLACE": value, "PNR_COMPACT": "1"}):
            return placer.place(
                graph, constraints, seed=0, iters=60, spread=1.3, channel_rules=rules
            )

    def test_the_stage_runs_with_the_switch_and_records_its_moves(self):
        from pnr import trace as _trace

        events = []

        class Recorder:
            def detail(self, placed, moves, motion=None):
                events.append((placed, moves, motion))

        recorder = Recorder()
        with mock.patch.object(_trace, "detail", recorder.detail):
            placed, report = self.place("1")
        self.assertTrue(report.legal)
        record = report.detail_motion
        self.assertIsNotNone(record)
        self.assertNotIn("log", record)
        self.assertLessEqual(record["hpwl_after"], record["hpwl_before"])
        self.assertEqual(len(events), 1)
        log = events[0][1]
        # A swap or a band slide moves more than one part in one move.
        self.assertGreaterEqual(len(log), sum(record["moves"].values()))
        self.assertTrue({m[1] for m in log} <= set(detail.KINDS))
        off_placed, off = self.place("0")
        self.assertIsNone(off.detail_motion)

    def test_same_bits_twice(self):
        a, _ = self.place("1")
        b, _ = self.place("1")
        self.assertEqual(a.to_json(), b.to_json())


class GoldenTest(unittest.TestCase):
    def test_golden_hash(self):
        """A fixed legal board (no global placement, so no platform's torch is involved): the
        pass gives the same bits on every platform."""
        parts = [
            pin("J%d" % i, "N%d" % (i % 5), (2.0 + 3.1 * i, 1.5 if i % 2 else 14.5))
            for i in range(9)
        ]
        for i in range(8):
            parts.append(
                two_pad(
                    "R%d" % i,
                    ["N%d" % (i % 5), "N%d" % ((i + 2) % 5)],
                    (4.5 + 2.9 * i, 8.0 + (i % 2) * 1.6),
                    rot=90.0 * (i % 4),
                )
            )
        g, cc = board(parts, fixed=tuple("J%d" % i for i in range(9)))
        out, record = improve(g, cc, compact_rounds=detail.COMPACT_ROUNDS)
        legal(self, out, cc)
        self.assertGreater(record["moves"]["compact"], 0)
        self.assertGreater(sum(record["moves"].values()), record["moves"]["compact"])
        poses = "".join(
            "%s %r %r %r\n" % (c.ref, c.pos[0], c.pos[1], c.rot)
            for c in sorted(out.components, key=lambda c: c.ref)
        )
        self.assertEqual(hashlib.sha256(poses.encode()).hexdigest()[:16], GOLDEN)


GOLDEN = "ba44bf03b9dd54fb"


if __name__ == "__main__":
    unittest.main()
