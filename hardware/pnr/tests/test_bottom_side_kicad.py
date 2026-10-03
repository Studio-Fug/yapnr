"""Bottom-side parts through writeback, judged by KiCad itself.

A part the engine places on the bottom side is flipped by writeback (KiCad ``Flip``)
and then turned to the engine's rotation. For stock footprints at every quarter turn
KiCad's pad centres must equal the engine's pad positions, every pad, graphic and
text of the part must sit on a ``B.*`` layer, and a copper keep-out tied to the part
(``copper_keepout.rect_mm``, footprint frame) must cover the same pad it names on the
top side.

Needs KiCad's Python (``pcbnew``); skipped otherwise. The footprints are written
inline (pads, courtyard, silkscreen, fabrication graphics and texts on ``F.*``, as a
library draws them), so no footprint library is needed.
"""

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# Two stock-like packages with pads asymmetric about the x axis (a wrong mirror moves
# a pad onto another's centre): name -> (courtyard half size, [(pad, x, y, w, h)]).
FOOTPRINTS = {
    "SOT-23-5": (
        (2.05, 1.7),
        [
            ("1", -1.1375, -0.95, 1.325, 0.6),
            ("2", -1.1375, 0.0, 1.325, 0.6),
            ("3", -1.1375, 0.95, 1.325, 0.6),
            ("4", 1.1375, 0.95, 1.325, 0.6),
            ("5", 1.1375, -0.95, 1.325, 0.6),
        ],
    ),
    "SOIC-8_3.9x4.9mm_P1.27mm": (
        (3.7, 2.7),
        [("%d" % (i + 1), -2.475, -1.905 + 1.27 * i, 1.95, 0.6) for i in range(4)]
        + [("%d" % (i + 5), 2.475, 1.905 - 1.27 * i, 1.95, 0.6) for i in range(4)],
    ),
}
ROTATIONS = (0, 90, 180, 270)
HEIGHT = 60.0


def kicad_mod(name, courtyard, pads):
    """A footprint file as a library draws it: everything on the front layers."""
    cx, cy = courtyard
    font = "(effects (font (size 1 1) (thickness 0.15)))"
    stroke = "(stroke (width %s) (type solid))"
    lines = [
        '(footprint "%s"' % name,
        "  (version 20240108)",
        '  (generator "pnr_test")',
        '  (layer "F.Cu")',
        '  (property "Reference" "REF**" (at 0 %s 0) (layer "F.SilkS") %s)' % (-cy - 0.7, font),
        '  (property "Value" "%s" (at 0 %s 0) (layer "F.Fab") %s)' % (name, cy + 0.7, font),
        "  (attr smd)",
        '  (fp_line (start %s %s) (end %s %s) %s (layer "F.SilkS"))'
        % (-cx + 0.6, -cy + 0.15, cx - 0.6, -cy + 0.15, stroke % 0.12),
        '  (fp_rect (start %s %s) (end %s %s) %s (fill none) (layer "F.CrtYd"))'
        % (-cx, -cy, cx, cy, stroke % 0.05),
        '  (fp_line (start %s %s) (end %s %s) %s (layer "F.Fab"))'
        % (-cx + 0.8, -cy + 0.3, cx - 0.8, cy - 0.3, stroke % 0.1),
        '  (fp_text user "${REFERENCE}" (at 0 0 0) (layer "F.Fab") %s)' % font,
    ]
    for number, x, y, w, h in pads:
        lines.append(
            '  (pad "%s" smd roundrect (at %s %s) (size %s %s) '
            '(layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25))' % (number, x, y, w, h)
        )
    return "\n".join(lines + [")", ""])


@unittest.skipUnless(importlib.util.find_spec("pcbnew") is not None, "requires KiCad's pcbnew")
class BottomSideWritebackTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import pcbnew

        from pnr.ingest import build_graph

        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        library = root / "inline.pretty"
        library.mkdir()
        for name, (courtyard, pads) in FOOTPRINTS.items():
            (library / (name + ".kicad_mod")).write_text(kicad_mod(name, courtyard, pads))
        board = pcbnew.BOARD()
        index = 0
        for name in FOOTPRINTS:
            for rot in ROTATIONS:
                fp = pcbnew.FootprintLoad(str(library), name)
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


@unittest.skipUnless(importlib.util.find_spec("pcbnew") is not None, "requires KiCad's pcbnew")
class CheckerSideTest(unittest.TestCase):
    """The ladder's constraint checker reads a part's side from its footprint layer
    and its surface pads: a footprint marked flipped whose pads stay on F.Cu (never
    mirrored) is on neither side."""

    def test_a_flip_needs_its_pads_on_the_bottom(self):
        import pcbnew

        spec = importlib.util.spec_from_file_location(
            "check_constraints",
            Path(__file__).resolve().parents[1] / "regression" / "check_constraints.py",
        )
        checker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(checker)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            library = root / "inline.pretty"
            library.mkdir()
            name = "SOT-23-5"
            (library / (name + ".kicad_mod")).write_text(kicad_mod(name, *FOOTPRINTS[name]))
            board = pcbnew.BOARD()
            edge = pcbnew.PCB_SHAPE(board)
            edge.SetShape(pcbnew.SHAPE_T_RECT)
            edge.SetStart(pcbnew.VECTOR2I(0, 0))
            edge.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(40), pcbnew.FromMM(20)))
            edge.SetLayer(pcbnew.Edge_Cuts)
            board.Add(edge)
            for i, ref in enumerate(("TOP", "FLIPPED", "FAKE")):
                fp = pcbnew.FootprintLoad(str(library), name)
                fp.SetReference(ref)
                fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(8 + 12 * i), pcbnew.FromMM(10)))
                board.Add(fp)
                if ref != "TOP":
                    fp.Flip(fp.GetPosition(), False)
                if ref == "FAKE":  # flipped back, then only the footprint layer set
                    fp.Flip(fp.GetPosition(), False)
                    fp.SetLayer(pcbnew.B_Cu)
            path = root / "sides.kicad_pcb"
            pcbnew.SaveBoard(str(path), board)
            judged = checker.Board(path)
            self.assertEqual(judged.side("TOP"), "top")
            self.assertEqual(judged.side("FLIPPED"), "bottom")
            self.assertEqual(judged.side("FAKE"), "mixed")
            ok, measured, _ = checker.check_side(judged, dict(refs=["FAKE"], side="bottom"))
            self.assertFalse(ok)
            self.assertEqual(measured["wrong_side"], ["FAKE"])


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
