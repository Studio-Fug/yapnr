"""Board framing: parts move into view, a fitted sheet and an Edge.Cuts rectangle, idempotent."""

from __future__ import annotations

import re
import unittest

from yapnr.frontends.atopile import outline

BOARD = """(kicad_pcb
\t(version 20241229)
\t(paper "A4")
\t(footprint "Lib:R"
\t\t(layer "F.Cu")
\t\t(at -5 -2 90)
\t\t(fp_line (start -1 0) (end 1 0) (layer "F.SilkS"))
\t)
\t(footprint "Lib:C"
\t\t(layer "F.Cu")
\t\t(at 5 3)
\t)
\t(segment (start -5 -2) (end 5 3) (width 0.2) (layer "F.Cu"))
\t(via (at 0 0) (size 0.6) (drill 0.3))
)
"""


class FrameTest(unittest.TestCase):
    def test_frame(self):
        framed = outline.frame(BOARD, 2.0)
        self.assertIn("(at 2.0000 2.0000 90)", framed)
        self.assertIn("(at 12.0000 7.0000)", framed)
        self.assertIn('(paper "User" 14.000 9.000)', framed)
        self.assertEqual(framed.count('(layer "Edge.Cuts")'), 4)
        # Footprint-local geometry does not move; top-level routing does.
        self.assertIn("(fp_line (start -1 0) (end 1 0)", framed)
        self.assertIn("(segment (start 2.0000 2.0000) (end 12.0000 7.0000) (width", framed)
        self.assertIn("(via (at 7.0000 4.0000)", framed)
        self.assertTrue(framed.rstrip().endswith(")"))

    def test_idempotent(self):
        once = outline.frame(BOARD, 2.0)
        twice = outline.frame(once, 2.0)
        self.assertEqual(twice.count(outline.SENTINEL), 4)
        self.assertEqual(
            re.findall(r"\(at [\d.-]+ [\d.-]+", once), re.findall(r"\(at [\d.-]+ [\d.-]+", twice)
        )

    def test_empty_board_is_unchanged(self):
        empty = '(kicad_pcb\n\t(paper "A4")\n)\n'
        self.assertEqual(outline.frame(empty, 2.0), empty)


if __name__ == "__main__":
    unittest.main()
