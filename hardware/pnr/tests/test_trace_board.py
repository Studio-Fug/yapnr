"""pnr.trace_board: the stdlib .kicad_pcb reader, on small hand-written boards."""

import tempfile
import unittest
from pathlib import Path

from pnr import trace_board

BOARD = """(kicad_pcb (version 20260206) (generator "pcbnew")
  (layers (0 "F.Cu" signal) (4 "In1.Cu" signal) (6 "In2.Cu" signal) (2 "B.Cu" signal)
    (25 "Edge.Cuts" user))
  (gr_rect (start 10 20) (end 40 44) (stroke (width 0.1) (type default)) (layer "Edge.Cuts"))
  (footprint "Resistor_SMD:R_0805_2012Metric" (layer "F.Cu") (at 20 30 90)
    (property "Reference" "R1" (at 0 0 0) (layer "F.Fab"))
    (property "Value" "10k" (at 0 0 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -1 0 90) (size 1 1.4) (layers "F.Cu" "F.Mask")
      (roundrect_rratio 0.25) (net "VCC"))
    (pad "2" smd roundrect (at 1 0 90) (size 1 1.4) (layers "F.Cu" "F.Mask")
      (roundrect_rratio 0.25) (net "OUT")))
  (footprint "Connector:Pin" (layer "B.Cu") (at 12 22 0)
    (fp_text reference "J1" (at 0 0) (layer "B.SilkS"))
    (pad "1" thru_hole circle (at 0 0) (size 1.7 1.7) (drill 1) (layers "*.Cu") (net "GND")))
  (segment (start 20 29) (end 25 29) (width 0.25) (layer "F.Cu") (net "VCC") (uuid "a"))
  (arc (start 25 29) (mid 26.4142 29.5858) (end 27 31) (width 0.25) (layer "B.Cu") (net "VCC"))
  (via (at 25 29) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "VCC"))
  (zone (net "GND") (layer "In1.Cu")
    (filled_polygon (layer "In1.Cu") (pts (xy 10 20) (xy 40 20) (xy 40 44) (xy 10 44)))))
"""

LEGACY = """(kicad_pcb (version 20240108)
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
  (net 0 "") (net 1 "GND") (net 2 "SIG")
  (gr_line (start 0 0) (end 10 0) (layer "Edge.Cuts"))
  (gr_line (start 10 0) (end 10 5) (layer "Edge.Cuts"))
  (segment (start 1 1) (end 2 1) (width 0.2) (layer "B.Cu") (net 2))
  (via (at 2 1) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))
  (zone (net 1) (net_name "GND") (layer "F.Cu")
    (filled_polygon (layer "F.Cu") (pts (xy 0 0) (xy 1 0) (xy 1 1)))))
"""


class ReaderTest(unittest.TestCase):
    def test_parse_atoms_and_strings(self):
        tree = trace_board.parse('(a "b c" 1.5 (d "e\\"f"))')
        self.assertEqual(tree, ["a", "b c", "1.5", ["d", 'e"f']])
        self.assertIsInstance(tree[2], trace_board.Atom)
        self.assertNotIsInstance(tree[1], trace_board.Atom)
        with self.assertRaises(ValueError):
            trace_board.parse("(a (b)")

    def test_frame_layers_and_copper(self):
        board = trace_board.read_tree(trace_board.parse(BOARD))
        self.assertEqual(board["layers"], ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"])
        self.assertEqual(board["frame"], (10.0, 44.0))
        self.assertEqual(board["size"], [30000, 24000])
        tracks = board["copper"]["tracks"]
        self.assertIn([0, 10000, 15000, 15000, 15000, 250], tracks)
        arcs = [t for t in tracks if t[0] == 3]
        self.assertEqual(len(arcs), trace_board.ARC_SEGMENTS)
        self.assertEqual(arcs[0][1:3], [15000, 15000])
        self.assertEqual(sorted(arcs)[-1][3:5], [17000, 13000])
        self.assertEqual(board["copper"]["vias"], [[15000, 15000, 600, 300]])
        layer, net, rings = board["copper"]["zones"][0]
        self.assertEqual((layer, net, len(rings[0])), (1, "GND", 4))
        self.assertIn([0, 24000], rings[0])

    def test_footprints_pads_and_poses(self):
        board = trace_board.read_tree(trace_board.parse(BOARD))
        self.assertEqual(
            board["poses"], [["J1", 2000, 22000, 0.0, "bottom"], ["R1", 10000, 14000, 90.0, "top"]]
        )
        r1 = next(f for f in board["footprints"] if f["ref"] == "R1")
        self.assertEqual(r1["value"], "10k")
        pad1 = r1["pads"][0]
        # (at -1 0), west of the origin, turned 90 degrees counter-clockwise lies 1 mm south.
        self.assertEqual((pad1["x"], pad1["y"]), (10000, 13000))
        self.assertEqual(
            (pad1["shape"], pad1["corner"], pad1["local_angle"]), ("roundrect", 250, 0.0)
        )
        j1 = next(f for f in board["footprints"] if f["ref"] == "J1")
        self.assertEqual((j1["pads"][0]["drill"], j1["pads"][0]["net"]), ([1000, 1000], "GND"))

    def test_nets_written_as_codes(self):
        board = trace_board.read_tree(trace_board.parse(LEGACY))
        self.assertEqual(board["frame"], (0.0, 5.0))
        self.assertEqual(board["copper"]["tracks"], [[1, 1000, 4000, 2000, 4000, 200]])
        self.assertEqual(board["copper"]["zones"][0][1], "GND")

    def test_refine_header_and_read_from_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "b.kicad_pcb"
            path.write_text(BOARD)
            parsed = trace_board.read(path)
        header = dict(
            components=[
                dict(ref="R1", pads=[dict(name="1", size=[1400, 1000], shape="rect", corner=None)])
            ]
        )
        trace_board.refine_header(header, parsed)
        comp = header["components"][0]
        self.assertEqual(comp["value"], "10k")
        self.assertEqual(
            comp["pads"][0],
            dict(name="1", size=[1000, 1400], shape="roundrect", corner=250, angle=0.0),
        )

    def test_collinear_arc_is_a_line(self):
        self.assertEqual(trace_board.arc_points((0, 0), (1, 0), (2, 0)), [(0, 0), (2, 0)])


if __name__ == "__main__":
    unittest.main()
