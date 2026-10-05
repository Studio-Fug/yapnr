"""A declared pour drawn, filled and stitched by KiCad (pnr.pour; KiCad Python only:
python3 -m unittest tests.test_pour_kicad).

A 4-layer board with a GND plane on In1: two bottom caps whose GND pads have no via.
C1 sits in open board: the B.Cu GND pour holds it, and its island gets a stitch via
to the plane. C2 is boxed in by a foreign track ring tight enough that its island
has no room for a via: reported, not stitched."""

import importlib.util
import unittest

NATIVE = importlib.util.find_spec("pcbnew") is not None
OFFSET = 30.0
W, H = 20.0, 12.0
RULES = dict(
    fab=dict(clearance_mm=0.15, track_width_mm=0.15, via_diameter_mm=0.45, via_drill_mm=0.2),
    pours=[dict(layer="B.Cu", net="GND", stitch=True, connect="thermal")],
)


def board():
    import pcbnew as k

    def v(x, y):  # graph frame (y up) to KiCad
        return k.VECTOR2I(round((OFFSET + x) * 1e6), round((OFFSET + H - y) * 1e6))

    b = k.BOARD()
    b.SetCopperLayerCount(4)
    nets = {}
    for name in ("GND", "VCC", "SIG"):
        nets[name] = k.NETINFO_ITEM(b, name)
        b.Add(nets[name])
    edge = k.PCB_SHAPE(b)
    edge.SetShape(k.SHAPE_T_RECT)
    edge.SetStart(v(0, H))
    edge.SetEnd(v(W, 0))
    edge.SetLayer(k.Edge_Cuts)
    edge.SetWidth(50000)
    b.Add(edge)
    plane = k.ZONE(b)
    plane.SetLayer(b.GetLayerID("In1.Cu"))
    plane.SetNetCode(nets["GND"].GetNetCode())
    plane.SetZoneName("plane GND In1.Cu")
    outline = plane.Outline()
    outline.NewOutline()
    for p in ((0, 0), (W, 0), (W, H), (0, H)):
        outline.Append(v(*p))
    b.Add(plane)
    for ref, at in (("C1", (5.0, 6.0)), ("C2", (15.0, 6.0))):
        f = k.FOOTPRINT(b)
        f.SetReference(ref)
        b.Add(f)
        f.SetPosition(v(*at))
        for num, net, dx in (("1", "VCC", -0.5), ("2", "GND", 0.5)):
            p = k.PAD(f)
            p.SetNumber(num)
            p.SetAttribute(k.PAD_ATTRIB_SMD)
            p.SetShape(k.PAD_SHAPE_RECT)
            p.SetSize(k.VECTOR2I(500000, 500000))
            ls = k.LSET()
            ls.AddLayer(k.B_Cu)
            p.SetLayerSet(ls)
            p.SetPosition(v(at[0] + dx, at[1]))
            p.SetNet(nets[net])
            f.Add(p)
    # A SIG ring round C2's GND pad on B.Cu, 0.55 mm out: the island inside is too small
    # for a 0.45 mm via with 0.15 mm clearance to the ring.
    cx, cy, r = 15.5, 6.0, 0.55
    ring = [(cx - r, cy - r), (cx + r, cy - r), (cx + r, cy + r), (cx - r, cy + r)]
    for a, z in zip(ring, ring[1:] + ring[:1]):
        t = k.PCB_TRACK(b)
        t.SetStart(v(*a))
        t.SetEnd(v(*z))
        t.SetWidth(150000)
        t.SetLayer(k.B_Cu)
        t.SetNet(nets["SIG"])
        b.Add(t)
    return b, v


@unittest.skipUnless(NATIVE, "requires KiCad Python")
class StitchTest(unittest.TestCase):
    def test_islands_with_pads_are_stitched_or_reported(self):
        import pcbnew as k

        from pnr.pour import draw, stitch

        b, v = board()
        made = draw(b, RULES)
        self.assertEqual([z.GetZoneName() for z in made], ["pour GND B.Cu"])
        self.assertEqual(draw(b, RULES), [])  # drawn once
        k.ZONE_FILLER(b).Fill(b.Zones())
        report = stitch(b, RULES)
        row = report["GND B.Cu"]
        self.assertEqual(len(row["stitched"]), 1, row)
        self.assertEqual(len(row["unstitched"]), 1, row)
        (x, y) = row["stitched"][0]
        self.assertLess(abs(x - (OFFSET + 5.5)), 3.0)  # by C1, not by C2
        b.BuildConnectivity()
        conn = b.GetConnectivity()
        pads = {
            f.GetReference(): [p for p in f.Pads() if p.GetNetname() == "GND"][0]
            for f in b.GetFootprints()
        }

        def joined_to_plane(pad):
            items = conn.GetConnectedItems(pad)
            return any(
                i.Type() == k.PCB_ZONE_T and i.IsOnLayer(b.GetLayerID("In1.Cu")) for i in items
            )

        self.assertTrue(joined_to_plane(pads["C1"]))
        self.assertFalse(joined_to_plane(pads["C2"]))  # the reported island
        again = stitch(b, RULES)  # idempotent: the stitched island now holds a via
        self.assertEqual(again["GND B.Cu"]["stitched"], [])


if __name__ == "__main__":
    unittest.main()
