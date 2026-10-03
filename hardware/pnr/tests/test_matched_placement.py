"""Placement keeps the legs of differential pairs and length-match groups even.

A pair whose legs run connector -> R1 / R2 -> IC is free (by wirelength) to put R1
and R2 anywhere along the way; the global term (pnr.place.model.length_mismatch) and
the post-legalization refinement (pnr.place.matched) even the estimated legs.
"""

import math
import unittest

from pnr.constraints import compile_constraints
from pnr.graph import BoardGraph, BoardOutline, Component, Net, Pad
from pnr.place.geometry import pin_positions, resolve_fixed_poses
from pnr.place.matched import matched_sets, mismatch, refine_matched
from pnr.place.metrics import hard_violations
from pnr.place.model import global_place

W, H = 40.0, 20.0


def series_board(r1=(20.0, 10.0), r2=(20.0, 6.0)):
    """J (west) -> R1 / R2 -> U (east): pair a = (AP, AN), pair b = (BP, BN)."""

    def part(ref, pos, pads, size):
        return Component(ref, "t", pos, 0, "top", size, size, pads=pads)

    comps = [
        part(
            "J",
            (3.0, 10.0),
            [Pad("P", "AP", (0, 1), (0.6, 0.6)), Pad("N", "AN", (0, -1), (0.6, 0.6))],
            (2, 4),
        ),
        part(
            "U",
            (37.0, 10.0),
            [Pad("P", "BP", (0, 1), (0.6, 0.6)), Pad("N", "BN", (0, -1), (0.6, 0.6))],
            (2, 4),
        ),
        part(
            "R1",
            r1,
            [Pad("1", "AP", (-0.5, 0), (0.5, 0.5)), Pad("2", "BP", (0.5, 0), (0.5, 0.5))],
            (2, 1.2),
        ),
        part(
            "R2",
            r2,
            [Pad("1", "AN", (-0.5, 0), (0.5, 0.5)), Pad("2", "BN", (0.5, 0), (0.5, 0.5))],
            (2, 1.2),
        ),
    ]
    nets = [
        Net("AP", 1, [("J", "P"), ("R1", "1")]),
        Net("AN", 2, [("J", "N"), ("R2", "1")]),
        Net("BP", 3, [("R1", "2"), ("U", "P")]),
        Net("BN", 4, [("R2", "2"), ("U", "N")]),
    ]
    return BoardGraph("series", comps, nets, BoardOutline(W, H))


def constraints(graph, pairs=True):
    doc = {
        "board": {"outline": {"w": W, "h": H}},
        "fixed": {
            "J": {"at": [3.0, 10.0], "rot": 0, "side": "top"},
            "U": {"at": [37.0, 10.0], "rot": 0, "side": "top"},
        },
    }
    if pairs:
        doc["diff_pair"] = [
            {"name": "a", "p": "AP", "n": "AN", "skew_mm": 1.0},
            {"name": "b", "p": "BP", "n": "BN", "skew_mm": 1.0},
        ]
    return compile_constraints(doc, graph.refs)


def legs(graph, pos, rot):
    xy = {}
    for comp in graph.components:
        th = math.radians(rot[comp.ref])
        for pad in comp.pads:
            ox, oy = pad.offset
            x = pos[comp.ref][0] + ox * math.cos(th) - oy * math.sin(th)
            y = pos[comp.ref][1] + ox * math.sin(th) + oy * math.cos(th)
            xy[(comp.ref, pad.name)] = (x, y)
    return {n.name: math.dist(xy[n.pins[0]], xy[n.pins[1]]) for n in graph.nets}


class GlobalTermTest(unittest.TestCase):
    def test_pairs_get_even_legs(self):
        g = series_board()
        pos, rot = global_place(g, constraints(g), W, H, seed=0, iters=300)
        length = legs(g, pos, rot)
        self.assertLess(abs(length["AP"] - length["AN"]), 1.0, length)
        self.assertLess(abs(length["BP"] - length["BN"]), 1.0, length)

    def test_no_pairs_no_term(self):
        g = series_board()
        cc = constraints(g, pairs=False)
        self.assertEqual(
            global_place(g, cc, W, H, seed=0, iters=50),
            global_place(g, cc, W, H, seed=0, iters=50, w_match=7.0),
        )


class RefineTest(unittest.TestCase):
    def test_uneven_series_parts_are_evened(self):
        # R1 near the connector, R2 near the IC: each pair's legs differ by ~18 mm.
        g = series_board(r1=(10.0, 13.0), r2=(30.0, 7.0))
        cc = constraints(g)
        sets = matched_sets(cc, g)
        self.assertEqual(sets, [("AP", "AN"), ("BP", "BN")])
        self.assertTrue(all(m > 10 for m in mismatch(g, sets)))
        out = refine_matched(
            g, cc, W, H, fixed=resolve_fixed_poses(g, cc), keepouts=[], grid_mm=0.25
        )
        self.assertTrue(all(m < 1.0 for m in mismatch(out, sets)), mismatch(out, sets))
        self.assertFalse(any(hard_violations(out, cc, clearance=0.0).values()))
        for ref in ("J", "U"):
            self.assertEqual(out.component(ref).pos, g.component(ref).pos)
        # Only small parts move: the fixed connector and IC stay, rotations stay.
        self.assertEqual([c.rot for c in out.components], [c.rot for c in g.components])

    def test_nothing_to_do(self):
        g = series_board(r1=(10.0, 13.0), r2=(30.0, 7.0))
        cc = constraints(g, pairs=False)
        self.assertIs(refine_matched(g, cc, W, H, fixed=resolve_fixed_poses(g, cc), keepouts=[]), g)
        even = series_board(r1=(20.0, 12.0), r2=(20.0, 8.0))
        cc = constraints(even)
        self.assertIs(
            refine_matched(even, cc, W, H, fixed=resolve_fixed_poses(even, cc), keepouts=[]),
            even,
        )

    def test_pin_estimate_is_octile(self):
        g = series_board(r1=(10.0, 13.0), r2=(30.0, 7.0))
        cc = constraints(g)
        (a, _b) = mismatch(g, matched_sets(cc, g))
        pins = {n: dict(pin_positions(g.component(n))) for n in ("J", "R1", "R2")}

        def octile(p, q):
            dx, dy = abs(p[0] - q[0]), abs(p[1] - q[1])
            return max(dx, dy) + (math.sqrt(2) - 1) * min(dx, dy)

        ap = octile(pins["J"]["P"], pins["R1"]["1"])
        an = octile(pins["J"]["N"], pins["R2"]["1"])
        self.assertAlmostEqual(a, abs(ap - an), places=9)


if __name__ == "__main__":
    unittest.main()
