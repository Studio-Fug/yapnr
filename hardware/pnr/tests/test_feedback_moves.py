"""pnr.feedback.moves: PULL / RAND children and their guards.

The synthetic case always runs; the converter cases use the real Mini inputs
(hier/inputs10, the inputs of the nb5 block runs) and a real nb5-noshove parent
layout with the failed connections of the real rounds in testdata/feedback.
"""

import json
import math
import unittest
from pathlib import Path

from pnr.constraints import compile_constraints
from pnr.feedback.moves import (
    MoveBoard,
    conn_weight,
    default_anchors,
    pull_children,
    rand_child,
    seed_for,
)
from pnr.feedback.table import build
from pnr.graph import BoardGraph, BoardOutline, Component, Net, Pad

HERE = Path(__file__).resolve()
DATA = HERE.parents[1] / "testdata" / "feedback"
INPUTS = HERE.parents[4] / "inputs10"
CONSTRAINTS = HERE.parents[2] / "splanc_dev" / "mini-constraints.yaml"
HAVE_MINI = (INPUTS / "graph.json").exists() and CONSTRAINTS.exists()


def fb_of(*pairs, mode="signal"):
    conns = []
    for a, b in pairs:
        ends = sorted([a, b], key=lambda e: "%s.%s" % e)
        conns.append(
            dict(
                id="%s.%s|%s.%s" % (*ends[0], *ends[1]),
                a=list(ends[0]),
                b=list(ends[1]),
                mode=mode,
                net="n",
                no_room=False,
            )
        )
    return dict(router="plain", conns=conns)


class SyntheticPullTest(unittest.TestCase):
    """U (hub, 4 pads) at the centre, R far to the right, C blocking the direct line."""

    def board(self, tier1=(), **kw):
        pads = [
            Pad(str(i), "n%d" % i, (x, y), (0.4, 0.4))
            for i, (x, y) in enumerate([(-1, 0), (1, 0), (0, 1), (0, -1)], 1)
        ]
        comps = [
            Component("U", "u", (10, 10), 0, "top", (3, 3), (3, 3), pads=pads),
            Component(
                "R",
                "r",
                (16, 10),
                0,
                "top",
                (1.6, 0.8),
                (1.6, 0.8),
                pads=[Pad("1", "n2", (-0.5, 0), (0.4, 0.4)), Pad("2", "x", (0.5, 0), (0.4, 0.4))],
            ),
            Component(
                "C",
                "c",
                (13, 10),
                90,
                "top",
                (1.6, 0.8),
                (1.6, 0.8),
                pads=[Pad("1", "n9", (-0.5, 0), (0.4, 0.4)), Pad("2", "n9b", (0.5, 0), (0.4, 0.4))],
            ),
        ]
        nets = [Net("n2", 1, [("U", "2"), ("R", "1")])]
        g = BoardGraph("syn", comps, nets, BoardOutline(20, 20))
        cc = compile_constraints({"board": {"outline": {"w": 20, "h": 20}}}, g.refs)
        return MoveBoard(
            graph=g,
            constraints=cc,
            key_of={c.ref: c.ref for c in comps},
            anchors=default_anchors(g, cc, {"U"}),
            tier1=frozenset(tier1),
            clearance=0.2,
            **kw
        )

    def test_pull_moves_the_non_anchor_end_toward_its_peer(self):
        board = self.board()
        before = board.graph.to_json()
        kids = pull_children(board, fb_of((("U", "2"), ("R", "1"))), n=2)
        self.assertEqual(board.graph.to_json(), before)  # board restored
        self.assertEqual(len(kids), 1)
        d = kids[0]["detail"]
        self.assertEqual((d["mover"], list(kids[0]["poses"])), ("R", ["R"]))
        self.assertLessEqual(d["move_mm"], 3.0 + 1e-9)
        self.assertGreaterEqual(d["shortening_mm"], 0.25 - 1e-9)
        x, y = kids[0]["poses"]["R"][:2]
        self.assertLess(x, 16)  # toward U, still clear of C
        self.assertEqual(
            kids, pull_children(board, fb_of((("U", "2"), ("R", "1"))), n=2)
        )  # deterministic

    def test_anchor_tier1_and_lineage_rules(self):
        fb = fb_of((("U", "2"), ("R", "1")))
        self.assertEqual(pull_children(self.board(tier1={"R"}), fb), [])  # tier-1 R, non-tier-1 U
        kids = pull_children(self.board(tier1={"R", "U"}), fb)
        self.assertLessEqual(kids[0]["detail"]["move_mm"], 1.0 + 1e-9)  # tier1-tier1: at most 1 mm
        capped = pull_children(self.board(origin={"R": (16.4, 10)}), fb, lineage_cap=0.5)
        for kid in capped:
            self.assertLessEqual(math.dist(kid["poses"]["R"][:2], (16.4, 10)), 0.5 + 1e-9)
        self.assertEqual(pull_children(self.board(), fb, skip=lambda poses: True), [])
        self.assertEqual(
            pull_children(self.board(), fb_of((("U", "1"), ("U", "2")))), []
        )  # no movable end

    def test_rand_is_seeded_legal_and_matches_displacement(self):
        board = self.board()
        a = rand_child(board, 7, 1.0)
        self.assertEqual(a, rand_child(board, 7, 1.0))
        self.assertNotEqual(a["detail"]["mover"], "U")
        self.assertLessEqual(abs(a["detail"]["move_mm"] - 1.0), 0.18)

    def test_block_unit_moves_rigidly_by_translation(self):
        # R and C form one library block; U is an anchor, so the block is the mover and moves as a whole.
        board = self.board(units={"BLK": ("R", "C")})
        kids = pull_children(board, fb_of((("U", "2"), ("R", "1"))), n=1)
        self.assertEqual(len(kids), 1)
        d, poses = kids[0]["detail"], kids[0]["poses"]
        self.assertEqual((d["mover"], sorted(poses)), ("BLK", ["C", "R"]))
        self.assertIsNone(d["rot"])
        shift = {
            ref: (
                poses[ref][0] - board.by_ref[ref].pos[0],
                poses[ref][1] - board.by_ref[ref].pos[1],
            )
            for ref in poses
        }
        self.assertAlmostEqual(shift["R"][0], shift["C"][0])
        self.assertAlmostEqual(shift["R"][1], shift["C"][1])
        self.assertEqual([poses[r][2] for r in ("R", "C")], [0.0, 90.0])

    def test_weight(self):
        c = dict(id="x", mode="power", no_room=True)
        self.assertEqual(conn_weight(c), 5)  # 1 + 2 + 1 + 1
        t = build(
            [
                dict(id="p", parent=None, fb=dict(conns=[dict(c, a=["a", "1"], b=["b", "1"])])),
                dict(id="q", parent="p", fb=dict(conns=[dict(c, a=["a", "1"], b=["b", "1"])])),
            ]
        )
        self.assertEqual(conn_weight(dict(c, mode="signal", no_room=False), t, "q"), 5)  # 1 + 2*2
        self.assertEqual(seed_for(1, "a"), seed_for(1, "a"))
        self.assertNotEqual(seed_for(1, "a"), seed_for(1, "b"))


@unittest.skipUnless(HAVE_MINI, "Mini inputs10 not present")
class ConverterPullTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from pnr.feedback.blocks import block_key
        from pnr.feedback.signals import read_round
        from pnr.hier.blocks import extract_blocks
        from pnr.mc.halving import _load

        cls.g, cls.c, cls.r = _load(INPUTS, CONSTRAINTS)
        cls.blocks = {b.name: b for b in extract_blocks(cls.g, cls.c)}
        key = block_key(cls.blocks["board.converter"])
        # a dense parent (22.25 x 33.25: its two tier-1 failures plus the signal failures of the
        # shove round) and a roomier one (33 x 35.75) with its own four failures
        cls.dense = json.loads((DATA / "converter-parent.json").read_text())
        own = read_round(DATA / "rounds" / "noshove-s1-22.25x33.25", key=key)
        other = read_round(DATA / "rounds" / "shove-s1-30x30", key=key)
        cls.dense_fb = dict(
            own, conns=own["conns"] + [c for c in other["conns"] if c["mode"] == "signal"]
        )
        cls.roomy = json.loads((DATA / "converter-parent-33x35.75.json").read_text())
        cls.roomy_fb = read_round(DATA / "rounds" / "noshove-s1-33x35.75", key=key)
        cls.parent, cls.fb = cls.roomy, cls.roomy_fb

    def board(self, parent=None, **kw):
        from pnr.feedback.blocks import block_board

        p = parent or self.parent
        return block_board(
            self.g,
            self.c,
            self.r,
            self.blocks,
            ["board.converter"],
            p["layout"],
            p["width"],
            p["height"],
            power_first=True,
            q_ref=p["power_quality"]["q_ref"],
            **kw
        )

    def test_dense_parent_moves_only_where_there_is_room(self):
        # Every signal-end resistor's improving lattice poses overlap a neighbour; one tier-1 cap moves.
        board = self.board(self.dense)
        kids = pull_children(board, self.dense_fb, n=3)
        self.assertEqual([k["detail"]["mover_keys"] for k in kids], [["output_cap0._p"]])
        self.assertLessEqual(kids[0]["detail"]["move_mm"], 1.0 + 1e-9)
        self.assertGreater(board.stats.get("overlap", 0), 100)

    def pad(self, layout, key, pad):
        from pnr.hier.synth import instance_board, local_key
        from pnr.place.geometry import pin_positions

        blk = self.blocks["board.converter"]
        g2, _, _ = instance_board(
            self.g, self.c, self.r, blk, layout, self.parent["width"], self.parent["height"]
        )
        comp = next(c for c in g2.components if local_key(blk, c.address) == key)
        return dict(pin_positions(comp))[pad]

    def test_children_are_legal_local_and_shorten_their_target(self):
        from pnr.feedback.blocks import child_layout, displacement
        from pnr.hier.synth_native import _layout_violations

        board = self.board()
        kids = pull_children(board, self.fb, n=3)
        self.assertGreaterEqual(len(kids), 2)
        self.assertEqual(
            len({k["detail"]["mover"] for k in kids}), len(kids)
        )  # k=1 differs from k=0
        self.assertEqual(kids, pull_children(self.board(), self.fb, n=3))  # deterministic
        layout = self.parent["layout"]
        blk = self.blocks["board.converter"]
        for kid in kids:
            d = kid["detail"]
            after = child_layout(layout, board, kid["poses"])
            moved = displacement(layout, after)
            self.assertEqual(list(moved), d["mover_keys"])  # every other part identical
            self.assertNotIn("ic", moved)  # the hub never moves
            self.assertLessEqual(max(moved.values()), d["max_move_mm"] + 1e-9)
            if d["mover"] in board.tier1:
                self.assertLessEqual(d["max_move_mm"], 1.0)
                self.assertLessEqual(d["pad_move_mm"], 1.0 + 1e-9)  # rotations included
            self.assertEqual(
                _layout_violations(
                    self.g,
                    self.c,
                    self.r,
                    blk,
                    layout,
                    after,
                    self.parent["width"],
                    self.parent["height"],
                ),
                {},
            )
            (ka, pa), (kb, pb) = [e.rsplit(".", 1) for e in d["conn"].split("|")]
            before_mm = math.dist(self.pad(layout, ka, pa), self.pad(layout, kb, pb))
            after_mm = math.dist(self.pad(after, ka, pa), self.pad(after, kb, pb))
            self.assertAlmostEqual(before_mm - after_mm, d["shortening_mm"], places=6)
            self.assertGreaterEqual(d["shortening_mm"], 0.25 - 1e-9)

    def test_lineage_cap_and_rand(self):
        from pnr.feedback.blocks import child_layout, displacement

        layout = self.parent["layout"]
        board = self.board()
        for kid in pull_children(board, self.fb, n=3, lineage_cap=0.3):
            self.assertLessEqual(
                max(displacement(layout, child_layout(layout, board, kid["poses"])).values()),
                0.3 + 1e-9,
            )
        kid = rand_child(board, seed_for(0, "x"), 1.0)
        self.assertIsNotNone(kid)
        self.assertNotIn("U5", kid["poses"])
        self.assertLessEqual(abs(kid["detail"]["move_mm"] - 1.0), 0.18)

    def test_power_guard_rejects_a_power_stage_regression(self):
        board = self.board()
        self.assertIsNotNone(board.guard)
        self.assertIsNone(board.guard(board.graph))
        by_ref = {c.ref: c for c in board.graph.components}
        w, h = self.parent["width"], self.parent["height"]
        for ref, pos, why in (
            ("L2", (2.0, 2.0), "q_band"),
            ("Q1", (w - 2.0, h - 2.0), "crossings"),
        ):
            saved = by_ref[ref].pos
            try:
                by_ref[ref].pos = pos
                self.assertIn(why, board.guard(board.graph) or "")
            finally:
                by_ref[ref].pos = saved
        self.assertIsNone(board.guard(board.graph))


if __name__ == "__main__":
    unittest.main()
