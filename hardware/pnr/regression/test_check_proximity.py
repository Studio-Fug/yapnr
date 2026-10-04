"""The constraint checker's proximity check, centre and ``anchor_pad`` (a pad-anchored
hard group), on a small KiCad board (KiCad's Python only)."""

import importlib.util
import tempfile
import unittest
from pathlib import Path

NATIVE = importlib.util.find_spec("pcbnew") is not None


def pad_board(path, anchor_rot):
    """A 30 x 20 mm board: L1 at (10, 10) (pads 1 and 2 at -2 and +2 mm in x, turned
    ``anchor_rot``), C1 at (13, 11), outline from (0, 0); KiCad y down."""
    import pcbnew as k

    b = k.BOARD()
    edge = k.PCB_SHAPE(b)
    edge.SetShape(k.SHAPE_T_RECT)
    edge.SetStart(k.VECTOR2I(0, 0))
    edge.SetEnd(k.VECTOR2I(30_000_000, 20_000_000))
    edge.SetLayer(k.Edge_Cuts)
    edge.SetWidth(0)
    b.Add(edge)
    for ref, (x, y), pads in (
        ("L1", (10, 10), (("1", -2.0), ("2", 2.0))),
        ("C1", (13, 11), (("1", -0.5), ("2", 0.5))),
    ):
        fp = k.FOOTPRINT(b)
        fp.SetReference(ref)
        fp.SetPosition(k.VECTOR2I(x * 1_000_000, y * 1_000_000))
        for number, dx in pads:
            pad = k.PAD(fp)
            pad.SetNumber(number)
            pad.SetSize(k.VECTOR2I(800_000, 800_000))
            pad.SetFPRelativePosition(k.VECTOR2I(round(dx * 1_000_000), 0))
            fp.Add(pad)
        if ref == "L1":
            fp.SetOrientationDegrees(anchor_rot)
        b.Add(fp)
    k.SaveBoard(str(path), b)


@unittest.skipUnless(NATIVE, "requires KiCad Python")
class ProximityCheck(unittest.TestCase):
    def check(self, anchor_rot, **extra):
        from check_constraints import Board, check_proximity

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "b.kicad_pcb"
            pad_board(path, anchor_rot)
            spec = dict(id="near", kind="proximity", anchor="L1", refs=["C1"], max_mm=1.5)
            return check_proximity(Board(path), dict(spec, **extra))

    def test_centre(self):
        ok, measured, expected = self.check(0)
        self.assertFalse(ok)  # 3.16 mm from L1's origin
        self.assertAlmostEqual(measured["distance_mm"]["C1"], 3.162, places=3)
        self.assertNotIn("anchor_pad", expected)

    def test_anchor_pad(self):
        # Pad 2 at (12, 10) (frame y up: 10): C1 at (13, 11) is 1.414 mm from it.
        ok, measured, expected = self.check(0, anchor_pad="2")
        self.assertTrue(ok)
        self.assertAlmostEqual(measured["distance_mm"]["C1"], 1.414, places=3)
        self.assertEqual(expected["anchor_pad"], "2")
        # Turned 180 degrees pad 2 lies at (8, 10): 5.10 mm.
        ok, measured, _ = self.check(180, anchor_pad=2)
        self.assertFalse(ok)
        self.assertAlmostEqual(measured["distance_mm"]["C1"], 5.099, places=3)


if __name__ == "__main__":
    unittest.main()
