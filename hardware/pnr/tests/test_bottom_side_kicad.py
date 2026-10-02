"""Bottom-side parts through writeback, judged by KiCad itself.

A part the engine places on the bottom side is flipped by writeback (KiCad ``Flip``)
and then turned to the engine's rotation. For stock footprints at every quarter turn
KiCad's pad centres must equal the engine's pad positions, every pad, graphic and
text of the part must sit on a ``B.*`` layer, and a copper keep-out tied to the part
(``copper_keepout.rect_mm``, footprint frame) must cover the same pad it names on the
top side.

Needs KiCad's Python (``pcbnew``) and its stock footprint library
(``PNR_KICAD_FOOTPRINTS``, the ``footprints`` folder); skipped otherwise.
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

LIBRARY = os.environ.get("PNR_KICAD_FOOTPRINTS", "")
FOOTPRINTS = (
    ("Package_TO_SOT_SMD", "SOT-23-5"),
    ("Package_SO", "SOIC-8_3.9x4.9mm_P1.27mm"),
)
ROTATIONS = (0, 90, 180, 270)
HEIGHT = 60.0


@unittest.skipUnless(
    importlib.util.find_spec("pcbnew") is not None and Path(LIBRARY).is_dir(),
    "requires KiCad's pcbnew and PNR_KICAD_FOOTPRINTS",
)
class BottomSideWritebackTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import pcbnew

        from pnr.ingest import build_graph

        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        board = pcbnew.BOARD()
        index = 0
        for lib, name in FOOTPRINTS:
            for rot in ROTATIONS:
                fp = pcbnew.FootprintLoad(str(Path(LIBRARY) / (lib + ".pretty")), name)
                fp.SetReference("U%d" % index)
                fp.Reference().SetLayer(pcbnew.F_Fab)
                board.Add(fp)
                for pad in fp.Pads():
                    net = pcbnew.NETINFO_ITEM(board, "U%d_%s" % (index, pad.GetNumber()))
                    board.Add(net)
                    pad.SetNet(net)
                fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(30 + 15 * index), pcbnew.FromMM(40)))
                index += 1
        source = root / "source.kicad_pcb"
        pcbnew.SaveBoard(str(source), board)
        graph = build_graph(pcbnew.LoadBoard(str(source)))
        cls.top = {c.ref: [tuple(p.offset) for p in c.pads] for c in graph.components}
        cls.names = {c.ref: [p.name for p in c.pads] for c in graph.components}
        for i, comp in enumerate(sorted(graph.components, key=lambda c: int(c.ref[1:]))):
            comp.pos = (10.0 + 12.0 * (i % 4), 12.0 + 20.0 * (i // 4))
            comp.rot = float(ROTATIONS[i % 4])
            comp.side = "bottom"  # writeback reads the side; pad offsets stay as ingested
        cls.graph = graph
        rules = dict(
            copper_keepouts=[
                dict(name=c.ref, ref=c.ref, rect_mm=cls._around(cls.top[c.ref][0]))
                for c in graph.components
            ]
        )
        (root / "placed.json").write_text(graph.to_json())
        (root / "rules.json").write_text(json.dumps(rules))
        out = root / "placed.kicad_pcb"
        # Mutate in a separate KiCad process, as production writeback does.
        script = (
            "import json, sys, pcbnew\n"
            "from pnr.graph import BoardGraph\n"
            "from pnr.writeback import apply_copper_keepouts, apply_placement\n"
            "board = pcbnew.LoadBoard(sys.argv[1])\n"
            "graph = BoardGraph.from_json(open(sys.argv[2]).read())\n"
            "rules = json.load(open(sys.argv[3]))\n"
            "apply_placement(board, graph, width=60.0, height=%r)\n"
            "apply_copper_keepouts(board, graph, rules, %r)\n"
            "pcbnew.SaveBoard(sys.argv[4], board)\n" % (HEIGHT, HEIGHT)
        )
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                str(source),
                str(root / "placed.json"),
                str(root / "rules.json"),
                str(out),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode:
            raise RuntimeError(result.stderr[-3000:])
        cls.board = pcbnew.LoadBoard(str(out))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @staticmethod
    def _around(offset, half=0.15):
        return [offset[0] - half, offset[1] - half, offset[0] + half, offset[1] + half]

    def footprints(self):
        return {fp.GetReference(): fp for fp in self.board.GetFootprints()}

    def expected(self, ref):
        """The engine's pad centres (nm) for ``ref``: footprint_point of the top-side
        offsets on the bottom, which is pin_positions after set_component_side."""
        from pnr.graph import footprint_point
        from pnr.writeback import to_pcb_nm

        comp = self.graph.component(ref)
        return {
            name: to_pcb_nm(*footprint_point(comp, *offset), HEIGHT)
            for name, offset in zip(self.names[ref], self.top[ref])
        }

    def test_pad_centres_equal_engine_positions_at_every_rotation(self):
        import pcbnew

        for ref, fp in self.footprints().items():
            comp = self.graph.component(ref)
            self.assertTrue(fp.IsFlipped(), ref)
            self.assertAlmostEqual(fp.GetOrientationDegrees() % 360, comp.rot, places=6)
            want = self.expected(ref)
            for pad in fp.Pads():
                x, y = want[pad.GetNumber()]
                got = pad.GetPosition()
                self.assertLessEqual(
                    max(abs(got.x - x), abs(got.y - y)), 2, "%s.%s" % (ref, pad.GetNumber())
                )
                self.assertTrue(pad.IsOnLayer(pcbnew.B_Cu))
                self.assertFalse(pad.IsOnLayer(pcbnew.F_Cu))

    def test_every_pad_graphic_and_text_is_on_a_bottom_layer(self):
        for ref, fp in self.footprints().items():
            layers = [self.board.GetLayerName(i.GetLayer()) for i in fp.GraphicalItems()]
            layers += [self.board.GetLayerName(fp.Reference().GetLayer())]
            layers += [self.board.GetLayerName(fp.Value().GetLayer())]
            for pad in fp.Pads():
                layers += [self.board.GetLayerName(la) for la in pad.GetLayerSet().Seq()]
            self.assertTrue(layers)
            self.assertEqual([la for la in layers if not la.startswith("B.")], [], ref)

    def test_copper_keepout_covers_its_pad_on_the_bottom(self):
        import pcbnew

        zones = {z.GetZoneName(): z for z in self.board.Zones()}
        for ref, fp in self.footprints().items():
            zone = zones["PNR keepout:" + ref]
            first = self.names[ref][0]
            for pad in fp.Pads():
                inside = zone.Outline().Contains(pcbnew.VECTOR2I(pad.GetPosition()))
                self.assertEqual(inside, pad.GetNumber() == first, "%s.%s" % (ref, pad.GetNumber()))


class FootprintPointTest(unittest.TestCase):
    """The engine side of the convention, without KiCad: footprint_point on the bottom
    equals pin_positions after set_component_side, at every quarter turn."""

    def test_footprint_point_matches_mirrored_pins(self):
        try:
            from pnr.place.geometry import pin_positions, set_component_side
        except ImportError:  # KiCad's Python has no torch (pnr.place imports it)
            self.skipTest("pnr.place needs torch")
        from pnr.graph import Component, Pad, footprint_point

        for rot in ROTATIONS:
            comp = Component(
                "U1",
                "t",
                (5.0, 7.0),
                rot,
                "top",
                (4, 4),
                (4, 4),
                pads=[Pad("1", "A", (-1.1, 0.95)), Pad("2", "B", (1.1, -0.3))],
            )
            top = [p.offset for p in comp.pads]
            set_component_side(comp, "bottom")
            for (_, got), offset in zip(pin_positions(comp), top):
                want = footprint_point(comp, *offset)
                self.assertAlmostEqual(got[0], want[0], places=9)
                self.assertAlmostEqual(got[1], want[1], places=9)


if __name__ == "__main__":
    unittest.main()
