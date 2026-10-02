"""The gerber/Excellon preview renders what KiCad writes (synthetic files, no KiCad)."""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from yapnr.fab import preview

COPPER = """%TF.FileFunction,Copper,L1,Top*%
%FSLAX46Y46*%
%MOMM*%
%AMRoundRect*
0 Rectangle with rounded corners*
4,1,4,$2,$3,$4,$5,$6,$7,$8,$9,$2,$3,0*
1,1,$1+$1,$2,$3*
1,1,$1+$1,$4,$5*
1,1,$1+$1,$6,$7*
1,1,$1+$1,$8,$9*
20,1,$1+$1,$2,$3,$4,$5,0*
21,1,0.5,0.5,0,0,45*%
%LPD*%
G01*
%ADD10C,0.250000*%
%ADD11RoundRect,0.250000X-0.450000X-0.350000X0.450000X-0.350000X0.450000X0.350000X-0.450000X0.350000X0*%
%ADD12R,1.700000X1.700000*%
%ADD13O,1.000000X2.000000*%
%ADD14P,1.000000X6X0.0*%
D10*
X1000000Y-1000000D02*
X5000000Y-1000000D01*
G75*
G02*
X7000000Y-3000000I0J-2000000D01*
G01*
D11*
X2000000Y-2000000D03*
D12*
X3000000Y-4000000D03*
D13*
X4000000Y-4000000D03*
D14*
X5000000Y-4000000D03*
G36*
X1000000Y-6000000D02*
X3000000Y-6000000D01*
X3000000Y-8000000D01*
X1000000Y-8000000D01*
X1000000Y-6000000D01*
G37*
%LPC*%
D10*
X2000000Y-7000000D03*
%LPD*%
M02*
"""
OUTLINE = """%TF.FileFunction,Profile,NP*%
%FSLAX46Y46*%
%MOMM*%
%ADD10C,0.100000*%
D10*
X0Y0D02*
X10000000Y0D01*
X10000000Y-10000000D01*
X0Y-10000000D01*
X0Y0D01*
M02*
"""
DRILL = """M48
; FORMAT={-:-/ absolute / inch / decimal}
FMAT,2
INCH
T1C0.0118
T2C0.0394
%
G90
G05
T1
X0.0394Y-0.0394
T2
X0.1575Y-0.1575G85X0.1575Y-0.2362
M30
"""


class PreviewTest(unittest.TestCase):
    def test_gerber_shapes(self):
        layer = preview.read_gerber(COPPER, "top.gtl")
        self.assertEqual(layer.function, "Copper,L1,Top")
        kinds = [svg.split(" ")[0] for _, svg in layer.items]
        # 2 draws (line, arc), the macro (outline, 4 circles, a vector line, a centre line), the
        # rectangle, the obround, the hexagon, the region, then a clear flash.
        self.assertEqual(len(layer.items), 2 + 7 + 3 + 1 + 1)
        self.assertEqual(kinds.count("<circle"), 5)
        self.assertFalse(layer.items[-1][0])  # LPC: clear
        self.assertIn(" A2,2 0 0,1 7,3", layer.items[1][1])  # a clockwise quarter arc
        x0, y0, x1, y1 = layer.bbox
        self.assertAlmostEqual(x0, 0.875)
        self.assertAlmostEqual(y1, -0.875)

    def test_drill_holes_and_slots(self):
        layer = preview.read_drill(DRILL, "b.XLN")
        self.assertEqual(len(layer.items), 2)
        self.assertIn('r="0.1499"', layer.items[0][1])  # 0.0118 in
        self.assertIn("stroke-width", layer.items[1][1])

    def test_unsupported_macro_primitives_are_refused(self):
        bad = COPPER.replace("21,1,0.5,0.5,0,0,45*", "7,0,0,1,0.8,0.1,0*")
        with self.assertRaises(preview.PreviewError):
            preview.read_gerber(bad, "x")

    def test_render_zip(self):
        with tempfile.TemporaryDirectory() as d:
            z = Path(d) / "g.zip"
            with zipfile.ZipFile(z, "w") as zf:
                zf.writestr("b.GTL", COPPER)
                zf.writestr("b.GKO", OUTLINE)
                zf.writestr("b.XLN", DRILL)
            written = preview.render_zip(z, Path(d) / "out")
            names = sorted(p.name for p in written)
            self.assertEqual(
                names,
                [
                    "composite-bottom.svg",
                    "composite-top.svg",
                    "layer-Drill.svg",
                    "layer-Edge_Cuts.svg",
                    "layer-F_Cu.svg",
                ],
            )
            top = (Path(d) / "out" / "composite-top.svg").read_text()
            self.assertTrue(top.startswith("<svg"))
            self.assertEqual(top.count("<mask"), 3)  # outline, copper, drills


if __name__ == "__main__":
    unittest.main()
