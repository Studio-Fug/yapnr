"""place(..., pair_weights=): None is bit-identical to the placer before the hook; weights pull pads together.

testdata/feedback/place_golden.json was produced by the pristine src11.base tree
(same inputs, seed 3, 200 iterations, PNR_POWER_FIRST 0 and 1) before
``pair_weights`` existed.
"""

import json
import math
import os
import unittest
from pathlib import Path
from unittest import mock

from pnr.graph import BoardGraph, BoardOutline, Component, Net, Pad

HERE = Path(__file__).resolve()
GOLDEN = HERE.parents[1] / "testdata" / "feedback" / "place_golden.json"
INPUTS = HERE.parents[4] / "inputs10"
CONSTRAINTS = HERE.parents[2] / "splanc_dev" / "mini-constraints.yaml"
HAVE_MINI = (INPUTS / "graph.json").exists() and CONSTRAINTS.exists()


def pad_xy(comp, name):
    from pnr.place.geometry import pin_positions

    return dict(pin_positions(comp))[name]


class GlobalPlacePairTest(unittest.TestCase):
    def graph(self):
        comps = [
            Component(
                r,
                "t",
                (0, 0),
                0,
                "top",
                (1, 1),
                (1, 1),
                pads=[Pad("1", "n" + r, (0.3, 0), (0.2, 0.2))],
            )
            for r in ("A", "B", "C", "D")
        ]
        nets = [Net("ab", 1, [("A", "1"), ("C", "1")]), Net("cd", 2, [("B", "1"), ("D", "1")])]
        return BoardGraph("g", comps, nets, BoardOutline(30, 30))

    def test_pair_term_pulls_pads_and_none_is_unchanged(self):
        from pnr.constraints import compile_constraints
        from pnr.place.model import global_place

        g = self.graph()
        cc = compile_constraints({"board": {"outline": {"w": 30, "h": 30}}}, g.refs)
        base = global_place(g, cc, 30, 30, seed=1, iters=150)
        self.assertEqual(base, global_place(g, cc, 30, 30, seed=1, iters=150, pair_weights=None))
        self.assertEqual(base, global_place(g, cc, 30, 30, seed=1, iters=150, pair_weights={}))
        # unknown pads and same-part pairs are ignored rather than failing
        self.assertEqual(
            base,
            global_place(
                g,
                cc,
                30,
                30,
                seed=1,
                iters=150,
                pair_weights={("A", "9", "B", "1"): 5.0, ("A", "1", "A", "1"): 5.0},
            ),
        )
        pulled = global_place(
            g, cc, 30, 30, seed=1, iters=150, pair_weights={("A", "1", "B", "1"): 5.0}
        )
        d = lambda pos: math.dist(pos[0]["A"], pos[0]["B"])
        self.assertLess(d(pulled), d(base) - 1.0)


@unittest.skipUnless(HAVE_MINI, "Mini inputs10 not present")
class PlacePairWeightsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from pnr.hier.blocks import extract_blocks
        from pnr.mc.halving import _load

        cls.g, cls.c, cls.r = _load(INPUTS, CONSTRAINTS)
        cls.blocks = {b.name: b for b in extract_blocks(cls.g, cls.c)}
        cls.golden = json.loads(GOLDEN.read_text())

    def place(self, name, pf, pair_weights=None, iters=200):
        from pnr.hier.blocks import sub_board
        from pnr.place.placer import place

        w, h = self.golden["%s|pf=%s" % (name, pf)]["size"]
        sg, sc, sr = sub_board(self.g, self.c, self.r, self.blocks[name], w, h)
        with mock.patch.dict(os.environ, {"PNR_POWER_FIRST": pf}):
            return place(
                sg,
                sc,
                seed=3,
                iters=iters,
                orient=True,
                channel_rules=sr,
                pair_weights=pair_weights,
            )

    def test_none_is_bit_identical_to_the_placer_before_the_hook(self):
        for key, want in sorted(self.golden.items()):
            name, pf = key.split("|pf=")
            placed, report = self.place(name, pf)
            got = {x.ref: [x.pos[0], x.pos[1], x.rot, x.side] for x in placed.components}
            self.assertEqual(json.loads(json.dumps(got)), want["poses"], key)
            self.assertEqual(report.legal, want["legal"], key)

    def test_weights_shorten_the_weighted_pad_pairs(self):
        for pf in ("0", "1"):
            base, _ = self.place("board.pd", pf)
            by = {c.ref: c for c in base.components}
            # the three longest two-part connections of the unweighted layout
            pairs = []
            for net in base.nets:
                pins = [p for p in net.pins if p[0] in by]
                for i, (ra, pa) in enumerate(pins):
                    for rb, pb in pins[i + 1 :]:
                        if ra != rb:
                            pairs.append(
                                (math.dist(pad_xy(by[ra], pa), pad_xy(by[rb], pb)), ra, pa, rb, pb)
                            )
            pairs = sorted(pairs, reverse=True)[:3]
            weights = {(ra, pa, rb, pb): 20.0 for _, ra, pa, rb, pb in pairs}
            placed, report = self.place("board.pd", pf, weights)
            self.assertTrue(report.legal)
            by2 = {c.ref: c for c in placed.components}
            before = sum(d for d, *_ in pairs)
            after = sum(
                math.dist(pad_xy(by2[ra], pa), pad_xy(by2[rb], pb)) for _, ra, pa, rb, pb in pairs
            )
            self.assertLess(after, before, "PNR_POWER_FIRST=%s" % pf)


class MacroPairWeightsTest(unittest.TestCase):
    def test_flat_pairs_map_to_macro_pads(self):
        from pnr.hier.macro import MacroPlan
        from pnr.hier.top import macro_pair_weights

        plan = MacroPlan()
        plan.member_of.update({"U1": "MB00", "C1": "MB00", "U2": "MB01"})
        out = macro_pair_weights(
            {
                ("U1", "3", "R9", "1"): 2.0,
                ("U1", "3", "C1", "1"): 7.0,
                ("U1", "4", "U2", "1"): 1.5,
                ("R9", "1", "R8", "2"): 1.0,
            },
            plan,
        )
        self.assertEqual(
            out,
            {
                ("MB00", "U1.3", "R9", "1"): 2.0,
                ("MB00", "U1.4", "MB01", "U2.1"): 1.5,
                ("R9", "1", "R8", "2"): 1.0,
            },
        )
        self.assertIsNone(macro_pair_weights(None, plan))
        self.assertIsNone(macro_pair_weights({("U1", "3", "C1", "1"): 7.0}, plan))


if __name__ == "__main__":
    unittest.main()
