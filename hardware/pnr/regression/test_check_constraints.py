"""The constraint checker's plane and microvia-span checks on small KiCad boards
(KiCad's Python only)."""

import importlib.util
import tempfile
import unittest
from pathlib import Path

NATIVE = importlib.util.find_spec("pcbnew") is not None


def plane_board(path, zones):
    """A 30 x 20 mm four-layer board with ``zones``: ``[(layer, net, x0..x1 fraction)]``."""
    import pcbnew as k

    b = k.BOARD()
    b.SetCopperLayerCount(4)
    nets = {}
    for name in ("GND", "CLOCK"):
        nets[name] = k.NETINFO_ITEM(b, name)
        b.Add(nets[name])
    edge = k.PCB_SHAPE(b)
    edge.SetShape(k.SHAPE_T_RECT)
    edge.SetStart(k.VECTOR2I(30_000_000, 30_000_000))
    edge.SetEnd(k.VECTOR2I(60_000_000, 50_000_000))
    edge.SetLayer(k.Edge_Cuts)
    edge.SetWidth(50_000)
    b.Add(edge)
    for priority, (layer, net, (f0, f1)) in enumerate(zones):
        z = k.ZONE(b)
        z.SetLayer(b.GetLayerID(layer))
        if net:
            z.SetNetCode(nets[net].GetNetCode())
        z.SetAssignedPriority(priority)
        outline = z.Outline()
        outline.NewOutline()
        x0, x1 = 30_000_000 + round(f0 * 30_000_000), 30_000_000 + round(f1 * 30_000_000)
        for x, y in ((x0, 30_000_000), (x1, 30_000_000), (x1, 50_000_000), (x0, 50_000_000)):
            outline.Append(k.VECTOR2I(x, y))
        b.Add(z)
    k.ZONE_FILLER(b).Fill(b.Zones())
    k.SaveBoard(str(path), b)


@unittest.skipUnless(NATIVE, "requires KiCad Python")
class PlaneCheck(unittest.TestCase):
    CHECK = dict(id="plane-In1-GND", kind="plane", layer="In1.Cu", net="GND", min_fill_fraction=0.5)

    def check(self, zones):
        from check_constraints import Board, check_plane

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "b.kicad_pcb"
            plane_board(path, zones)
            return check_plane(Board(path), self.CHECK)

    def test_a_full_plane_passes(self):
        ok, measured, _ = self.check([("In1.Cu", "GND", (0, 1)), ("In2.Cu", "CLOCK", (0, 1))])
        self.assertTrue(ok)
        self.assertEqual(measured["foreign_zones"], [])
        self.assertGreater(measured["fill_fraction"], 0.9)

    def test_another_nets_pour_on_the_plane_layer_fails(self):
        # GND still fills over half the layer, but CLOCK is poured over the rest.
        ok, measured, limit = self.check([("In1.Cu", "GND", (0, 1)), ("In1.Cu", "CLOCK", (0, 0.4))])
        self.assertFalse(ok)
        self.assertGreater(measured["fill_fraction"], 0.5)
        self.assertEqual(measured["foreign_zones"], ["CLOCK"])
        self.assertGreater(measured["foreign_fill_fraction"], 0.3)
        self.assertEqual(limit["foreign_fill_fraction"], 0.0)

    def test_a_pour_of_no_net_on_the_plane_layer_fails(self):
        ok, measured, _ = self.check([("In1.Cu", "GND", (0, 1)), ("In1.Cu", None, (0.8, 1))])
        self.assertFalse(ok)
        self.assertEqual(measured["foreign_zones"], ["<no net>"])

    def test_too_little_plane_fails(self):
        ok, measured, _ = self.check([("In1.Cu", "GND", (0, 0.3))])
        self.assertFalse(ok)
        self.assertLess(measured["fill_fraction"], 0.5)


@unittest.skipUnless(NATIVE, "requires KiCad Python")
class MicroviaSpanCheck(unittest.TestCase):
    CHECK = dict(id="microvia-span", kind="microvia_span", max_dielectrics=1)

    def check(self, vias):
        """``vias``: ``[(type, top layer, bottom layer)]`` on the four-layer board."""
        import pcbnew as k
        from check_constraints import Board, check_microvia_span

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "b.kicad_pcb"
            plane_board(path, [])
            b = k.LoadBoard(str(path))
            for n, (kind, top, bottom) in enumerate(vias):
                via = k.PCB_VIA(b)
                via.SetPosition(k.VECTOR2I(35_000_000 + n * 2_000_000, 40_000_000))
                via.SetViaType(kind)
                via.SetLayerPair(b.GetLayerID(top), b.GetLayerID(bottom))
                via.SetWidth(400_000)
                via.SetDrill(100_000)
                b.Add(via)
            k.SaveBoard(str(path), b)
            return check_microvia_span(Board(path), self.CHECK)

    def test_microvias_to_the_neighbouring_layer_pass(self):
        import pcbnew as k

        ok, measured, _ = self.check(
            [(k.VIATYPE_MICROVIA, "F.Cu", "In1.Cu"), (k.VIATYPE_MICROVIA, "In2.Cu", "B.Cu")]
        )
        self.assertTrue(ok)
        self.assertEqual((measured["microvias"], measured["too_deep"]), (2, 0))

    def test_a_microvia_across_two_dielectrics_fails(self):
        # KiCad's DRC passes this one; a blind via of the same span stays legal.
        import pcbnew as k

        ok, measured, limit = self.check(
            [(k.VIATYPE_MICROVIA, "F.Cu", "In2.Cu"), (k.VIATYPE_BLIND, "F.Cu", "In2.Cu")]
        )
        self.assertFalse(ok)
        self.assertEqual((measured["microvias"], measured["too_deep"]), (1, 1))
        self.assertEqual(measured["examples"][0]["layers"], ["F.Cu", "In2.Cu"])
        self.assertEqual(limit["max_dielectrics"], 1)


if __name__ == "__main__":
    unittest.main()
