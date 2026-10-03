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


def bga_board(path, vias=(), tracks=()):
    """A 20 x 20 mm board with U1, a 3 x 3 array at 0.65 mm (balls A1..C3, B2 on GND,
    the others on S_<ball>) at the centre and its 2.5 mm square courtyard, plus ``vias``
    ``[(net, x, y, diameter, drill)]`` and F.Cu ``tracks`` ``[(net, (x, y), (x, y))]``
    (mm from the board's lower-left corner, y up), and C1 (two pads, S_A1 and GND)."""
    import pcbnew as k

    ox, oy = 30.0, 50.0  # the outline's lower-left corner in KiCad mm (y down)

    def v(x, y):
        return k.VECTOR2I(round((ox + x) * 1e6), round((oy - y) * 1e6))

    b = k.BOARD()
    b.SetCopperLayerCount(4)
    nets = {}

    def net(name):
        if name not in nets:
            nets[name] = k.NETINFO_ITEM(b, name)
            b.Add(nets[name])
        return nets[name]

    edge = k.PCB_SHAPE(b)
    edge.SetShape(k.SHAPE_T_RECT)
    edge.SetStart(v(0, 20))
    edge.SetEnd(v(20, 0))
    edge.SetLayer(k.Edge_Cuts)
    edge.SetWidth(50_000)
    b.Add(edge)

    def footprint(ref, at, pads, courtyard):
        fp = k.FOOTPRINT(b)
        fp.SetReference(ref)
        fp.SetPosition(v(*at))
        for name, netname, (x, y), size in pads:
            pad = k.PAD(fp)
            pad.SetNumber(name)
            pad.SetShape(k.PAD_SHAPE_CIRCLE)
            pad.SetAttribute(k.PAD_ATTRIB_SMD)
            pad.SetLayerSet(pad.SMDMask())
            pad.SetSize(k.VECTOR2I(round(size * 1e6), round(size * 1e6)))
            pad.SetPosition(v(at[0] + x, at[1] + y))
            pad.SetNet(net(netname))
            fp.Add(pad)
        rect = k.PCB_SHAPE(fp)
        rect.SetShape(k.SHAPE_T_RECT)
        rect.SetStart(v(at[0] - courtyard, at[1] + courtyard))
        rect.SetEnd(v(at[0] + courtyard, at[1] - courtyard))
        rect.SetLayer(k.F_CrtYd)
        rect.SetWidth(50_000)
        fp.Add(rect)
        b.Add(fp)

    balls = []
    for r, row in enumerate("ABC"):
        for c in range(3):
            name = "%s%d" % (row, c + 1)
            balls.append(
                (
                    name,
                    "GND" if name == "B2" else "S_" + name,
                    ((c - 1) * 0.65, (1 - r) * 0.65),
                    0.32,
                )
            )
    footprint("U1", (10, 10), balls, 1.25)
    footprint("C1", (14, 10), [("1", "S_A1", (-0.5, 0), 0.5), ("2", "GND", (0.5, 0), 0.5)], 1.0)
    for netname, x, y, d, h in vias:
        via = k.PCB_VIA(b)
        via.SetPosition(v(x, y))
        via.SetWidth(round(d * 1e6))
        via.SetDrill(round(h * 1e6))
        via.SetNet(net(netname))
        b.Add(via)
    for netname, a, z in tracks:
        t = k.PCB_TRACK(b)
        t.SetStart(v(*a))
        t.SetEnd(v(*z))
        t.SetWidth(100_000)
        t.SetLayer(k.F_Cu)
        t.SetNet(net(netname))
        b.Add(t)
    k.SaveBoard(str(path), b)


@unittest.skipUnless(NATIVE, "requires KiCad Python")
class FanoutChecks(unittest.TestCase):
    def run_check(self, kind, check, **board):
        import check_constraints

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "b.kicad_pcb"
            bga_board(path, **board)
            return check_constraints.KINDS[kind](check_constraints.Board(path), check)

    VIA = dict(ref="U1", nets=["GND"], diameter_mm=0.35, drill_mm=0.15, site="interstitial")

    def test_an_interstitial_plane_via_of_its_class_passes(self):
        ok, measured, _ = self.run_check(
            "via_class", self.VIA, vias=[("GND", 10.325, 10.325, 0.35, 0.15)]
        )
        self.assertTrue(ok, measured)
        self.assertEqual(measured["vias"], 1)

    def test_a_wrong_size_or_site_fails(self):
        ok, measured, _ = self.run_check(
            "via_class", self.VIA, vias=[("GND", 10.325, 10.325, 0.4, 0.2)]
        )
        self.assertFalse(ok)
        self.assertEqual(len(measured["wrong_size"]), 1)
        ok, measured, _ = self.run_check(
            "via_class", self.VIA, vias=[("GND", 10.325, 10.0, 0.35, 0.15)]
        )
        self.assertFalse(ok)
        self.assertEqual(len(measured["off_site"]), 1)

    def test_escape_by_via_or_by_leaving_the_courtyard(self):
        check = dict(ref="U1", pads=["A1", "C3"])
        a1 = (10 - 0.65, 10 + 0.65)
        tracks = [("S_A1", a1, (9.0, 11.0)), ("S_A1", (9.0, 11.0), (8.0, 12.0))]
        vias = [("S_C3", 10.65, 9.0, 0.4, 0.2)]
        tracks.append(("S_C3", (10.65, 10 - 0.65), (10.65, 9.0)))
        ok, measured, _ = self.run_check("escape", check, vias=vias, tracks=tracks)
        self.assertTrue(ok, measured)
        ok, measured, _ = self.run_check("escape", check, tracks=tracks[:1])
        self.assertFalse(ok)
        self.assertEqual(measured["not_escaped"], ["A1", "C3"])

    def test_pad_distance(self):
        check = dict(refs=["C1"], anchor="U1", max_mm=5.0)
        ok, measured, _ = self.run_check("pad_distance", check)
        self.assertTrue(ok, measured)
        self.assertAlmostEqual(measured["distance_mm"]["C1.2"], 4.5, places=3)
        ok, _, _ = self.run_check("pad_distance", dict(check, max_mm=4.0))
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
