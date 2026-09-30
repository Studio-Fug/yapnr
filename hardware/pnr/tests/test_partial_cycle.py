"""Cycle cleanup must preserve original copper outside interior junctions.

The native cases run with PNR_PARTIAL_CYCLE_CLEANUP=1 (Electrical221, opt-in);
unset, cycle_candidates proposes whole-track chains only (src15).
"""

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pnr.track_graph import partial_cycle_cleanup_enabled, redundant_chains


class PartialFlagTests(unittest.TestCase):
    def test_flag_is_opt_in(self):
        with mock.patch.dict(os.environ, {"PNR_PARTIAL_CYCLE_CLEANUP": ""}):
            self.assertFalse(partial_cycle_cleanup_enabled())
        with mock.patch.dict(os.environ, {"PNR_PARTIAL_CYCLE_CLEANUP": "1"}):
            self.assertTrue(partial_cycle_cleanup_enabled())


def fixture():
    def s(i, a, b):
        return dict(
            id=i,
            a=tuple(round((v + 10) * 1e6) for v in a),
            b=tuple(round((v + 10) * 1e6) for v in b),
            width=200000,
        )

    return [s("left", (-1, -1), (1, 1)), s("right", (1, 1), (3, -1)), s("base", (0, 0), (2, 0))]


class PartialGraphTests(unittest.TestCase):
    def test_partial_cycle_keeps_both_external_tails(self):
        ss = fixture()
        self.assertEqual(redundant_chains(ss), [])
        choices = redundant_chains(ss, allow_partial=True)
        self.assertEqual(len(choices), 1)
        p = choices[0]
        self.assertEqual(p["remove_tracks"], ["left", "right"])
        self.assertEqual(
            {(tuple(x["a"]), tuple(x["b"])) for x in p["replacement_segments"]},
            {
                ((9000000, 9000000), (10000000, 10000000)),
                ((12000000, 10000000), (13000000, 9000000)),
            },
        )

    def test_interior_terminal_and_locked_track_prevent_partial_edit(self):
        ss = fixture()
        self.assertEqual(redundant_chains(ss, [(11000000, 11000000)], allow_partial=True), [])
        ss[0]["locked"] = True
        self.assertEqual(redundant_chains(ss, allow_partial=True), [])


@unittest.skipUnless(importlib.util.find_spec("pcbnew"), "native KiCad required")
@mock.patch.dict(os.environ, {"PNR_PARTIAL_CYCLE_CLEANUP": "1"})
class PartialNativeTests(unittest.TestCase):
    def fixture(self):
        import pcbnew as k

        b = k.BOARD()
        n = k.NETINFO_ITEM(b, "test")
        b.Add(n)
        for s in fixture():
            t = k.PCB_TRACK(b)
            t.SetStart(k.VECTOR2I(*s["a"]))
            t.SetEnd(k.VECTOR2I(*s["b"]))
            t.SetWidth(s["width"])
            t.SetLayer(k.F_Cu)
            t.SetNetCode(n.GetNetCode())
            b.Add(t)
        for i, pos in enumerate([(9000000, 9000000), (13000000, 9000000)]):
            f = k.FOOTPRINT(b)
            f.SetReference("P" + str(i))
            b.Add(f)
            p = k.PAD(f)
            p.SetNumber("1")
            p.SetPosition(k.VECTOR2I(*pos))
            p.SetSize(k.VECTOR2I(400000, 400000))
            p.SetShape(k.PAD_SHAPE_RECT)
            p.SetAttribute(k.PAD_ATTRIB_SMD)
            ls = k.LSET()
            ls.AddLayer(k.F_Cu)
            p.SetLayerSet(ls)
            p.SetNetCode(n.GetNetCode())
            f.Add(p)
        b.BuildConnectivity()
        return b

    def test_native_partial_cycle_preserves_pads_and_no_repeat(self):
        from pnr.pad_entry import snapshot
        from pnr.track_graph import apply_cycle, cycle_candidates
        from pnr.via_coalesce import partition, preserved

        b = self.fixture()
        before = partition(b)
        entries = snapshot(b, {})
        p = cycle_candidates(b, {}, [])[0]
        self.assertIn("replacement_segments", p)
        removed = apply_cycle(b, p)
        self.assertTrue(preserved(before, partition(b)))
        self.assertEqual(entries, snapshot(b, {}))
        self.assertEqual(cycle_candidates(b, {}, []), [])
        self.assertEqual(len(list(b.GetTracks())), 3)

    def test_stale_outside_replacement_is_rejected_before_mutation(self):
        from pnr.track_graph import apply_cycle, cycle_candidates

        b = self.fixture()
        p = cycle_candidates(b, {}, [])[0]
        p["replacement_segments"][0]["a"] = (50000000, 50000000)
        with self.assertRaisesRegex(ValueError, "invalid retained"):
            apply_cycle(b, p)
        self.assertEqual(len(list(b.GetTracks())), 3)

    def test_src15_default_proposes_no_partial_edit(self):
        from pnr.track_graph import cycle_candidates

        b = self.fixture()
        with mock.patch.dict(os.environ, {"PNR_PARTIAL_CYCLE_CLEANUP": ""}):
            self.assertEqual(cycle_candidates(b, {}, []), [])
        self.assertEqual(len(list(b.GetTracks())), 3)
