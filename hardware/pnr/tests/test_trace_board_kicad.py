"""pnr.trace_board against pcbnew: a board built and saved by KiCad reads back with the same
items and coordinates (within 1 micrometre). Needs KiCad's Python (pcbnew); manual."""

import math
import tempfile
import unittest
from pathlib import Path

try:
    import pcbnew
except ImportError:  # pragma: no cover - outside KiCad's Python
    pcbnew = None

from pnr import trace_board


def mm(value):
    return pcbnew.FromMM(value)


@unittest.skipIf(pcbnew is None, "needs KiCad's pcbnew")
class PcbnewParityTest(unittest.TestCase):
    def test_board_items_match_pcbnew(self):
        board = pcbnew.BOARD()
        board.SetCopperLayerCount(4)
        net = pcbnew.NETINFO_ITEM(board, "SIG")
        board.Add(net)
        for (x0, y0), (x1, y1) in (
            ((10, 20), (40, 20)),
            ((40, 20), (40, 44)),
            ((40, 44), (10, 44)),
            ((10, 44), (10, 20)),
        ):
            edge = pcbnew.PCB_SHAPE(board)
            edge.SetShape(pcbnew.SHAPE_T_SEGMENT)
            edge.SetStart(pcbnew.VECTOR2I(mm(x0), mm(y0)))
            edge.SetEnd(pcbnew.VECTOR2I(mm(x1), mm(y1)))
            edge.SetLayer(pcbnew.Edge_Cuts)
            board.Add(edge)
        track = pcbnew.PCB_TRACK(board)
        track.SetStart(pcbnew.VECTOR2I(mm(12.5), mm(30.25)))
        track.SetEnd(pcbnew.VECTOR2I(mm(20.125), mm(31)))
        track.SetWidth(mm(0.25))
        track.SetLayer(pcbnew.B_Cu)
        track.SetNet(net)
        board.Add(track)
        via = pcbnew.PCB_VIA(board)
        via.SetPosition(pcbnew.VECTOR2I(mm(20.125), mm(31)))
        via.SetWidth(mm(0.6))
        via.SetDrill(mm(0.3))
        via.SetNet(net)
        board.Add(via)
        footprint = pcbnew.FOOTPRINT(board)
        footprint.SetReference("R1")
        pad = pcbnew.PAD(footprint)
        pad.SetNumber("1")
        pad.SetShape(pcbnew.PAD_SHAPE_RECTANGLE)
        pad.SetSize(pcbnew.VECTOR2I(mm(1.0), mm(1.4)))
        pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        pad.SetLayerSet(pad.SMDMask())
        pad.SetPosition(pcbnew.VECTOR2I(mm(-1), 0))
        footprint.Add(pad)
        board.Add(footprint)
        footprint.SetPosition(pcbnew.VECTOR2I(mm(25), mm(35)))
        footprint.SetOrientationDegrees(90)
        pad.SetNet(net)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "parity.kicad_pcb"
            pcbnew.SaveBoard(str(path), board)
            loaded = pcbnew.LoadBoard(str(path))
            parsed = trace_board.read(path)
        self.assertEqual(parsed["layers"], ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"])
        left, bottom = parsed["frame"]
        self.assertAlmostEqual(left, 10.0, places=6)
        self.assertAlmostEqual(bottom, 44.0, places=6)

        def engine(point):
            return ((point.x / 1e6 - left) * 1000.0, (bottom - point.y / 1e6) * 1000.0)

        tracks = [t for t in loaded.GetTracks() if t.GetClass() == "PCB_TRACK"]
        vias = [t for t in loaded.GetTracks() if t.GetClass() == "PCB_VIA"]
        self.assertEqual(
            (len(parsed["copper"]["tracks"]), len(parsed["copper"]["vias"])),
            (len(tracks), len(vias)),
        )
        row = parsed["copper"]["tracks"][0]
        for got, want in zip(row[1:5], engine(tracks[0].GetStart()) + engine(tracks[0].GetEnd())):
            self.assertLessEqual(abs(got - want), 1.0)
        self.assertEqual(row[0], 3)
        x, y = engine(vias[0].GetPosition())
        self.assertLessEqual(math.dist((x, y), parsed["copper"]["vias"][0][:2]), 1.0)
        fp = loaded.GetFootprints()[0]
        (pose,) = parsed["poses"]
        self.assertLessEqual(math.dist(engine(fp.GetPosition()), pose[1:3]), 1.0)
        self.assertAlmostEqual(pose[3], fp.GetOrientationDegrees() % 360, places=3)
        pad_xy = engine(fp.Pads()[0].GetPosition())
        read_pad = parsed["footprints"][0]["pads"][0]
        self.assertLessEqual(math.dist(pad_xy, (read_pad["x"], read_pad["y"])), 1.0)


if __name__ == "__main__":
    unittest.main()
