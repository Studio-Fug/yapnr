"""A rail's copper extracted from a KiCad board and solved (pnr.ir_extract; KiCad
Python only: python3 -m unittest tests.test_ir_extract_kicad)."""

import importlib.util
import math
import tempfile
import unittest
from pathlib import Path

NATIVE = importlib.util.find_spec("pcbnew") is not None
OFFSET = 30.0  # the board's outline corner, KiCad mm (y down)


def build(tmp):
    """A 4-layer 20 x 10 mm board: net V from pad A.1 by a 0.25 mm F.Cu track to a via,
    a filled In1 zone strip, a via up to pad B.1; pad C.1 on its own (an island)."""
    import pcbnew as k

    def v(x, y):
        return k.VECTOR2I(round((OFFSET + x) * 1e6), round((OFFSET + y) * 1e6))

    b = k.BOARD()
    b.SetCopperLayerCount(4)
    b.GetDesignSettings().m_HasStackup = True
    net = k.NETINFO_ITEM(b, "V")
    b.Add(net)
    edge = k.PCB_SHAPE(b)
    edge.SetShape(k.SHAPE_T_RECT)
    edge.SetStart(v(0, 0))
    edge.SetEnd(v(20, 10))
    edge.SetLayer(k.Edge_Cuts)
    edge.SetWidth(50000)
    b.Add(edge)
    for ref, (x, y) in (("A", (2.0, 5.0)), ("B", (18.0, 5.0)), ("C", (10.0, 9.0))):
        f = k.FOOTPRINT(b)
        f.SetReference(ref)
        b.Add(f)
        f.SetPosition(v(x, y))
        p = k.PAD(f)
        p.SetNumber("1")
        p.SetAttribute(k.PAD_ATTRIB_SMD)
        p.SetShape(k.PAD_SHAPE_RECT)
        p.SetSize(k.VECTOR2I(600000, 600000))
        ls = k.LSET()
        ls.AddLayer(k.F_Cu)
        p.SetLayerSet(ls)
        p.SetPosition(v(x, y))
        p.SetNet(net)
        f.Add(p)
    t = k.PCB_TRACK(b)
    t.SetStart(v(2.0, 5.0))
    t.SetEnd(v(4.0, 5.0))
    t.SetWidth(250000)
    t.SetLayer(k.F_Cu)
    t.SetNet(net)
    b.Add(t)
    for x in (4.0, 16.0):
        via = k.PCB_VIA(b)
        via.SetPosition(v(x, 5.0))
        via.SetDrill(200000)
        via.SetWidth(400000)
        via.SetViaType(k.VIATYPE_THROUGH)
        via.SetLayerPair(k.F_Cu, k.B_Cu)
        via.SetNet(net)
        b.Add(via)
    t = k.PCB_TRACK(b)
    t.SetStart(v(16.0, 5.0))
    t.SetEnd(v(18.0, 5.0))
    t.SetWidth(250000)
    t.SetLayer(k.F_Cu)
    t.SetNet(net)
    b.Add(t)
    z = k.ZONE(b)
    z.SetLayer(b.GetLayerID("In1.Cu"))
    z.SetNet(net)
    z.SetLocalClearance(200000)
    z.SetMinThickness(200000)
    outline = z.Outline()
    outline.NewOutline()
    for x, y in ((3.0, 3.0), (17.0, 3.0), (17.0, 7.0), (3.0, 7.0)):
        outline.Append(v(x, y))
    b.Add(z)
    k.ZONE_FILLER(b).Fill(b.Zones())
    path = str(Path(tmp) / "rail.kicad_pcb")
    k.SaveBoard(path, b)
    text = Path(path).read_text()
    if '(type "copper")' not in text:  # a stackup block, if this build wrote none
        rows = (
            '\n\t\t\t(layer "F.Cu" (type "copper") (thickness 0.035))'
            '\n\t\t\t(layer "dielectric 1" (type "prepreg") (thickness 0.2))'
            '\n\t\t\t(layer "In1.Cu" (type "copper") (thickness 0.0175))'
            '\n\t\t\t(layer "dielectric 2" (type "core") (thickness 1.0))'
            '\n\t\t\t(layer "In2.Cu" (type "copper") (thickness 0.0175))'
            '\n\t\t\t(layer "dielectric 3" (type "prepreg") (thickness 0.2))'
            '\n\t\t\t(layer "B.Cu" (type "copper") (thickness 0.035))'
        )
        text = text.replace("(setup", "(setup\n\t\t(stackup" + rows + "\n\t\t)", 1)
        Path(path).write_text(text)
    return path


@unittest.skipUnless(NATIVE, "requires KiCad Python")
class ExtractTest(unittest.TestCase):
    def test_extract_and_solve(self):
        import pcbnew as k

        from pnr.ir_drop import barrel_ohm, resistivity
        from pnr.ir_extract import extract, report, stack_depths

        with tempfile.TemporaryDirectory() as tmp:
            path = build(tmp)
            b = k.LoadBoard(path)
            depths = stack_depths(b, path)
            self.assertEqual([d["name"] for d in depths], ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"])
            copper = extract(b, "V", path)
            self.assertEqual(len(copper["tracks"]), 2)
            self.assertEqual(len(copper["vias"]), 2)
            self.assertEqual([z["layer"] for z in copper["zones"]], ["In1.Cu"])
            self.assertEqual(sorted(p["ref"] for p in copper["pads"]), ["A", "B", "C"])
            # The graph frame: the outline corner is the origin, y up.
            a = next(p for p in copper["pads"] if p["ref"] == "A")
            self.assertAlmostEqual(a["at"][0], 2.0, places=4)
            self.assertAlmostEqual(a["at"][1], 5.0, places=4)
            rules = dict(
                ir_drop=[
                    dict(net="V", sources=["A:1"], sinks=["B:1"], current_a=1.0, budget_mohm=50.0)
                ]
            )
            result = report(b, rules, Path(tmp) / "ir", path, heatmaps=True)
            self.assertTrue((Path(tmp) / "ir" / "ir.json").exists())
        rep = result["V"]
        self.assertEqual(rep["status"], "pass")
        rho = resistivity(25.0)
        z = {d["name"]: d["z_mm"] for d in depths}
        t_in = {d["name"]: d["copper_mm"] for d in depths}["In1.Cu"]
        # Two 2 mm tracks (the first 0.3 mm of each inside its pad), two barrels to
        # In1, 12 mm of a 4 mm strip, and the spreading round each via (at most
        # R_square / pi * ln(W / (pi r)) each, here 20 % over).
        barrels = 2 * barrel_ohm(z["In1.Cu"] - z["F.Cu"], 0.2, rho=rho)
        tracks = 2 * rho * 2.0 / (0.25 * 0.035)
        square = rho / t_in
        spread = 2 * 1.2 * square / math.pi * math.log(4.0 / (math.pi * 0.2))
        lower = barrels + tracks * 1.7 / 2.0 + square * 12.0 / 4.0
        upper = barrels + tracks + square * 12.0 / 4.0 + spread
        self.assertGreater(rep["r_eff_mohm"] / 1e3, lower)
        self.assertLess(rep["r_eff_mohm"] / 1e3, upper)
        self.assertLessEqual(rep["residual"], 1e-10)

    def test_an_unjoined_pad_is_open(self):
        import pcbnew as k

        from pnr.ir_extract import report

        with tempfile.TemporaryDirectory() as tmp:
            path = build(tmp)
            b = k.LoadBoard(path)
            rules = dict(ir_drop=[dict(net="V", sources=["A:1"], sinks="all", current_a=1.0)])
            rep = report(b, rules, Path(tmp) / "ir", path)["V"]
        self.assertEqual(rep["status"], "open")
        self.assertEqual(rep["opens"], ["C.1"])
        self.assertTrue(math.isfinite(rep["loss_w"]))


if __name__ == "__main__":
    unittest.main()
