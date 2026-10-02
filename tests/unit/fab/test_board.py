"""Board facts from the .kicad_pcb file (the stdlib S-expression reader)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from yapnr.fab import board, testing


class ParseTest(unittest.TestCase):
    def test_atoms_strings_and_escapes(self):
        tree = board.parse('(a "b c" d (e "f \\"g\\"") (h))')
        self.assertEqual(tree, ["a", "b c", "d", ["e", 'f "g"'], ["h"]])
        self.assertIsInstance(tree[2], board.Sym)
        self.assertNotIsInstance(tree[1], board.Sym)

    def test_unbalanced_input_is_refused(self):
        for text in ("(a (b)", "(a))", ""):
            with self.assertRaises(board.BoardError):
                board.parse(text)

    def test_input_id_ignores_uuids(self):
        a = '(x (uuid "01dc7ed3-91dc-53b2-8106-2b6b5739eb1c"))'
        b = '(x (uuid "11111111-2222-3333-4444-555555555555"))'
        self.assertEqual(board.input_id(a), board.input_id(b))
        self.assertNotEqual(board.input_id(a), board.input_id("(y)"))

    def test_stats_values(self):
        self.assertEqual(board.stats_value("26.0000 mm"), 26.0)
        self.assertEqual(board.stats_value("520.00 mm²"), 520.0)
        self.assertIsNone(board.stats_value(None))


class FactsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def read(self, **kwargs):
        return board.read(testing.write_board(self.dir, "b", **kwargs))

    def test_layers_footprints_and_fields(self):
        b = self.read(layers=4, rf={"er": 3.61, "h_mm": 0.1999, "tan_delta": 0.009})
        self.assertEqual(b.copper_layers, ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"])
        self.assertEqual(b.layer_count, 4)
        refs = {fp.reference: fp for fp in b.footprints}
        self.assertEqual(sorted(refs), ["H1", "H2", "J1", "R1", "R2", "RF1"])
        r1, r2 = refs["R1"], refs["R2"]
        self.assertEqual((r1.side, r1.mount, r1.value), ("top", "SMD", "1k"))
        self.assertEqual(r1.field("LCSC Part #", "LCSC"), "C990000001")
        self.assertEqual(r1.field("Partnumber"), "YP-R0805-1K")
        self.assertEqual((r2.side, r2.at[2]), ("bottom", 90.0))
        self.assertTrue(refs["H1"].excluded_from_bom and refs["H1"].excluded_from_pos)
        self.assertEqual(refs["H2"].pads[0].drill, (1.0, 2.0))
        self.assertEqual(refs["J1"].mount, "THT")
        self.assertEqual(b.via_count, 1)
        self.assertEqual([c["name"] for c in b.netclasses], ["Default"])

    def test_rf_footprints_carry_their_assumed_substrate(self):
        b = self.read(rf={"er": 3.55, "tan_delta": 0.0027, "h_mm": 0.813})
        (rf,) = b.rf_footprints()
        self.assertEqual(
            {k: rf.rf[k] for k in ("name", "er", "tan_delta", "h_mm")},
            {"name": "test-line", "er": 3.55, "tan_delta": 0.0027, "h_mm": 0.813},
        )
        self.assertEqual(len(rf.rf["spec_sha256"]), 64)

    def test_castellated_pads(self):
        b = self.read(castellated=True)
        ((ref, pad),) = b.castellated()
        self.assertEqual((ref, pad.drill), ("J2", (0.6, 0.6)))

    def test_outline_loops_cutouts_and_panels(self):
        b = self.read(size=(30.0, 20.0))
        self.assertEqual(b.outline.size_mm, (30.0, 20.0))
        self.assertEqual(b.outline.closed_loops, 1)
        self.assertEqual(len(b.outline.outer_loops()), 1)
        self.assertEqual(b.outline.stroke_mm, 0.1)
        panel = self.read(second_outline=True)
        self.assertEqual(len(panel.outline.outer_loops()), 2)

    def test_lines_arcs_and_open_chains(self):
        lines = (
            '(gr_line (start 0 0) (end 10 0) (stroke (width 0.1) (type solid)) (layer "Edge.Cuts"))'
            '(gr_line (start 10 0) (end 10 10) (stroke (width 0.1) (type solid)) (layer "Edge.Cuts"))'
            '(gr_arc (start 10 10) (mid 5 12) (end 0 10) (stroke (width 0.2) (type solid)) (layer "Edge.Cuts"))'
            '(gr_line (start 0 10) (end 0 0) (stroke (width 0.1) (type solid)) (layer "Edge.Cuts"))'
            '(gr_circle (center 5 5) (end 6 5) (stroke (width 0.1) (type solid)) (layer "Edge.Cuts"))'
        )
        o = board.outline(board.parse(f"(kicad_pcb {lines})"))
        self.assertEqual((o.closed_loops, o.open_chains, o.stroke_mm), (2, 0, 0.2))
        self.assertEqual(len(o.outer_loops()), 1)  # the circle is a cutout
        self.assertEqual(o.size_mm, (10.0, 12.0))
        gap = lines.replace("(end 10 10) (stroke", "(end 10 9) (stroke", 1)
        o = board.outline(board.parse(f"(kicad_pcb {gap})"))
        self.assertEqual(o.open_chains, 1)

    def test_vias_in_smd_pads(self):
        self.assertEqual(self.read(layers=4).vias_in_smd_pads(), [])
        in_pad = self.read(layers=4, via_in_pad=(0.35, 0.20))
        self.assertEqual(in_pad.vias_in_smd_pads(), ["R1.2"])
        self.assertEqual(len(in_pad.vias), 1)
        self.assertEqual((in_pad.vias[0].diameter, in_pad.vias[0].drill), (0.35, 0.2))

    def test_pad_geometry_follows_kicads_absolute_pad_angle(self):
        # A footprint at -90 degrees: pad positions turn with it, the pad angle is absolute.
        text = (
            '(kicad_pcb (layers (0 "F.Cu" signal) (2 "B.Cu" signal))'
            '(footprint "R" (layer "F.Cu") (at 10 10 -90)'
            '(property "Reference" "R1")'
            '(pad "1" smd rect (at -1 0 270) (size 1 0.5) (layers "F.Cu" "F.Mask")))'
            '(via (at 10 9) (size 0.4) (drill 0.2) (layers "F.Cu" "B.Cu"))'
            '(via (at 10.4 9) (size 0.4) (drill 0.2) (layers "F.Cu" "B.Cu")))'
        )
        path = self.dir / "rot.kicad_pcb"
        path.write_text(text)
        b = board.read(path)
        (pad,) = b.footprints[0].pads
        self.assertAlmostEqual(pad.at[0], 10.0)
        self.assertAlmostEqual(pad.at[1], 9.0)  # (-1, 0) turned by -90 lands above (y down)
        self.assertEqual(pad.at[2], 270.0)
        # Rotated by 270, the 1 x 0.5 pad is 0.5 wide in x: the via 0.4 mm to the side is off it.
        self.assertTrue(pad.contains(10.0, 9.0))
        self.assertTrue(pad.contains(10.0, 9.45))
        self.assertFalse(pad.contains(10.4, 9.0))
        self.assertEqual(b.vias_in_smd_pads(), ["R1.1"])

    def test_rules_profile_from_a_generated_kicad_dru(self):
        self.assertIsNone(board.rules_profile(None))
        self.assertEqual(
            board.rules_profile(
                "(version 1)\n# Generated by pnr.fab_profile (profile oshpark-4l); x\n"
            ),
            "oshpark-4l",
        )
        self.assertEqual(board.rules_profile("(version 1)\n(rule a)\n"), "hand-written rules")
        path = testing.write_board(self.dir, "dru", layers=4)
        path.with_suffix(".kicad_dru").write_text(
            "(version 1)\n# Generated by pnr.fab_profile (profile jlc-pofv); docs\n"
        )
        self.assertEqual(board.read(path).rules_profile, "jlc-pofv")

    def test_not_a_board(self):
        path = self.dir / "x.kicad_pcb"
        path.write_text("(kicad_sch)")
        with self.assertRaises(board.BoardError):
            board.read(path)


if __name__ == "__main__":
    unittest.main()
