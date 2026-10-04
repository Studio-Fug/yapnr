"""Writeback keeps the board's own outline (``board.edge: exact`` or
``board.keep_outline``) under KiCad's Python; it skips without pcbnew. The board is
built inline: one resistor footprint and a rounded 20 x 12 mm outline at a 0.05 mm
stroke, framed 7 mm away from writeback's page offset."""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

FOOTPRINT = """(footprint "R" (version 20240108) (generator "test") (layer "F.Cu")
  (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu" "F.Mask"))
  (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu" "F.Mask")))
"""

SCRIPT = """
import json, sys
import pcbnew
from pnr.board_edge import outline_text
from pnr.ingest import load
from pnr.writeback import writeback

work, lib, rules_json = sys.argv[1], sys.argv[2], sys.argv[3]
board = pcbnew.BOARD()
fp = pcbnew.FootprintLoad(lib, "R")
fp.SetReference("R1")
fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(47), pcbnew.FromMM(43)))
board.Add(fp)
src = work + "/src.kicad_pcb"
pcbnew.SaveBoard(src, board)
text = open(src).read().rstrip()
text = text[: text.rfind(")")] + outline_text(20, 12, radius=1.0, stroke=0.05, offset=37.0) + ")\\n"
open(src, "w").write(text)
graph = load(src)
rules = json.loads(rules_json)
writeback(src, graph, work + "/out.kicad_pcb", width=20.0, height=12.0, rules=rules, layers=2)
"""


@unittest.skipUnless(importlib.util.find_spec("pcbnew"), "requires native KiCad")
class KeepOutlineTest(unittest.TestCase):
    def _writeback(self, rules):
        from pnr.board_edge import parse_edges

        with tempfile.TemporaryDirectory() as work:
            lib = os.path.join(work, "Local.pretty")
            os.mkdir(lib)
            with open(os.path.join(lib, "R.kicad_mod"), "w", encoding="utf-8") as fh:
                fh.write(FOOTPRINT)
            env = dict(os.environ, PYTHONPATH=os.pathsep.join([ROOT, os.path.dirname(ROOT)]))
            result = subprocess.run(
                [sys.executable, "-c", SCRIPT, work, lib, json.dumps(rules)],
                capture_output=True,
                text=True,
                env=env,
                timeout=300,
            )
            self.assertEqual(result.returncode, 0, result.stderr[-3000:])
            with open(os.path.join(work, "out.kicad_pcb"), encoding="utf-8") as fh:
                return parse_edges(fh.read()), result.stderr

    def test_exact_edge_keeps_the_rounded_outline(self):
        edges, _ = self._writeback({"layers": 2, "edge": "exact"})
        self.assertEqual(edges["stroke_mm"], 0.05)
        self.assertEqual(sorted(it["kind"] for it in edges["items"]), ["arc"] * 4 + ["line"] * 4)
        # Moved to writeback's frame (the page offset), size unchanged.
        self.assertEqual(edges["origin"], [30.0, 30.0])
        self.assertEqual(edges["size"], [20.0, 12.0])

    def test_default_stamps_the_rectangle(self):
        edges, _ = self._writeback({"layers": 2})
        self.assertEqual(edges["stroke_mm"], 0.15)
        self.assertEqual([it["kind"] for it in edges["items"]], ["line"] * 4)


if __name__ == "__main__":
    unittest.main()
