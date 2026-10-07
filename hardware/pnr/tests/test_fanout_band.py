"""PNR_FANOUT_BAND_MM: the placement band a declared fanout keeps clear beside its part."""

import os
import unittest
from unittest.mock import patch

from pnr.constraints import ConstraintError, compile_constraints
from pnr.graph import BoardGraph, BoardOutline, Component, Net, Pad
from pnr.place.geometry import keepout_rects


def doc(rot=0, forbidden=None, fixed=True):
    fanout = dict(
        ref="U1",
        via_classes={"g": {"diameter_mm": 0.35, "drill_mm": 0.15, "nets": ["GND"]}},
    )
    if forbidden is not None:
        fanout["forbidden_exits"] = forbidden
    d = {"schema": "v0", "board": {"outline": {"w": 40, "h": 40}}, "fanout": [fanout]}
    if fixed:
        d["fixed"] = {"U1": dict(at=[20.0, 20.0], rot=rot, side="top")}
    return d


def graph(rot=0):
    pads = [Pad(name="A1", net="GND", offset=(0.0, 0.0), size=(0.3, 0.3))]
    comps = [Component("U1", "bga", (20.0, 20.0), rot, "top", (12, 8), (12, 8), pads=pads)]
    return BoardGraph("t", comps, [Net("GND", 1, [("U1", "A1")])], BoardOutline(40, 40))


def bands(compiled):
    return {
        c.name: c.params["extent"]
        for c in compiled.constraints
        if c.kind == "keepout" and (c.name or "").startswith("fanout-band:")
    }


class FanoutBandTest(unittest.TestCase):
    def test_off_without_a_depth(self):
        with patch.dict(os.environ, {"PNR_FANOUT_BAND_MM": ""}):
            self.assertEqual(bands(compile_constraints(doc(), ["U1"])), {})
        with patch.dict(os.environ, {"PNR_FANOUT_BAND_MM": "0"}):
            self.assertEqual(bands(compile_constraints(doc(), ["U1"])), {})
        with patch.dict(os.environ, {"PNR_FANOUT_BAND_MM": "-1"}):
            with self.assertRaises(ConstraintError):
                compile_constraints(doc(), ["U1"])

    def test_a_band_on_each_surface_exit_edge_against_the_courtyard(self):
        with patch.dict(os.environ, {"PNR_FANOUT_BAND_MM": "1.5"}):
            compiled = compile_constraints(doc(forbidden=["south"]), ["U1"])
        got = bands(compiled)
        self.assertEqual(
            sorted(got), ["fanout-band:U1:east", "fanout-band:U1:north", "fanout-band:U1:west"]
        )
        self.assertTrue(all(e["depth_mm"] == 1.5 for e in got.values()))
        rects = {
            round(r.cx, 6): r
            for r in keepout_rects(graph(), compiled, {"U1": (20.0, 20.0)})
            if abs(r.cy - 20.0) < 1e-9
        }
        # The 12 x 8 courtyard's east and west edges, 1.5 mm out.
        self.assertAlmostEqual(rects[26.75].w, 1.5)
        self.assertAlmostEqual(rects[13.25].w, 1.5)
        self.assertAlmostEqual(rects[26.75].h, 8.0)

    def test_board_edges_follow_the_parts_rotation(self):
        # Turned 90 degrees, the part's own west edge faces board south.
        with patch.dict(os.environ, {"PNR_FANOUT_BAND_MM": "1"}):
            compiled = compile_constraints(doc(rot=90, forbidden=["north", "east", "west"]), ["U1"])
        self.assertEqual(
            bands(compiled), {"fanout-band:U1:south": {"edge": "west", "depth_mm": 1.0}}
        )
        (rect,) = keepout_rects(graph(rot=90), compiled, {"U1": (20.0, 20.0)})
        # Rotated, the courtyard is 8 wide and 12 tall: the band sits below it.
        self.assertAlmostEqual(rect.cy, 20.0 - 6.0 - 0.5)
        self.assertAlmostEqual(rect.w, 8.0)

    def test_an_unfixed_part_with_a_forbidden_edge_gets_no_band(self):
        with patch.dict(os.environ, {"PNR_FANOUT_BAND_MM": "1"}):
            self.assertEqual(
                bands(compile_constraints(doc(forbidden=["north"], fixed=False), ["U1"])), {}
            )
            self.assertEqual(len(bands(compile_constraints(doc(fixed=False), ["U1"]))), 4)


if __name__ == "__main__":
    unittest.main()
