"""A plane partition drawn and filled by KiCad (KiCad Python only: python3 -m unittest
tests.test_plane_partition_kicad)."""

import importlib.util
import tempfile
import unittest
from pathlib import Path

NATIVE = importlib.util.find_spec("pcbnew") is not None
OFFSET = 30.0
W, H = 20.0, 12.0


def board():
    """4 layers, In2 typed power; rails A and B with three 0.6 mm pads each, a via
    under each pad to the plane, and a foreign (GND) via row between them."""
    import pcbnew as k

    def v(x, y):  # graph frame (y up) to KiCad
        return k.VECTOR2I(round((OFFSET + x) * 1e6), round((OFFSET + H - y) * 1e6))

    b = k.BOARD()
    b.SetCopperLayerCount(4)
    b.SetLayerType(b.GetLayerID("In2.Cu"), k.LT_POWER)
    b.GetDesignSettings().m_HasStackup = True
    nets = {}
    for name in ("A", "B", "GND"):
        nets[name] = k.NETINFO_ITEM(b, name)
        b.Add(nets[name])
    edge = k.PCB_SHAPE(b)
    edge.SetShape(k.SHAPE_T_RECT)
    edge.SetStart(v(0, H))
    edge.SetEnd(v(W, 0))
    edge.SetLayer(k.Edge_Cuts)
    edge.SetWidth(50000)
    b.Add(edge)
    pads = {
        "A": [(2.0, 2.0), (18.0, 2.0), (10.0, 4.5)],
        "B": [(2.0, 10.0), (18.0, 10.0), (10.0, 7.5)],
    }
    for net, points in pads.items():
        for n, (x, y) in enumerate(points):
            f = k.FOOTPRINT(b)
            f.SetReference("%s%d" % (net, n))
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
            p.SetNet(nets[net])
            f.Add(p)
            via = k.PCB_VIA(b)
            via.SetPosition(v(x, y))
            via.SetDrill(200000)
            via.SetWidth(400000)
            via.SetViaType(k.VIATYPE_THROUGH)
            via.SetLayerPair(k.F_Cu, k.B_Cu)
            via.SetNet(nets[net])
            b.Add(via)
    for x in (6.0, 8.0, 12.0, 14.0):
        via = k.PCB_VIA(b)
        via.SetPosition(v(x, 6.0))
        via.SetDrill(200000)
        via.SetWidth(400000)
        via.SetViaType(k.VIATYPE_THROUGH)
        via.SetLayerPair(k.F_Cu, k.B_Cu)
        via.SetNet(nets["GND"])
        b.Add(via)
    return b, pads, v


@unittest.skipUnless(NATIVE, "requires KiCad Python")
class DrawTest(unittest.TestCase):
    def test_zones_fill_apart_and_the_rails_are_whole(self):
        import pcbnew as k

        from pnr.ir_extract import report
        from pnr.plane_partition import _CACHE, Terminal, partition
        from pnr.writeback import draw_plane_regions

        b, pads, v = board()
        terms = {
            net: [Terminal("%s%d.1" % (net, n), "via", p, 0.2) for n, p in enumerate(points)]
            for net, points in pads.items()
        }
        blocked = [((x, 6.0), 0.3) for x in (6.0, 8.0, 12.0, 14.0)]
        entry = dict(
            layer="In2.Cu",
            nets=["A", "B"],
            order="current",
            split_gap_mm=0.3,
            min_width_mm=1.0,
            fill="GND",
            core_no_vias=True,
            terminal_reach_mm=0.8,
            h_mm=0.1,
        )
        _CACHE.clear()
        part = partition(entry, width=W, height=H, terminals=terms, blocked=blocked)
        rules = dict(fab=dict(clearance_mm=0.1, track_width_mm=0.1))
        full = [v(0, 0), v(W, 0), v(W, H), v(0, H)]
        made = draw_plane_regions(b, part.rows(), rules, lambda p: v(*p), full)
        self.assertEqual(sorted(z.GetNetname() for z in made), ["A", "B", "GND"])
        k.ZONE_FILLER(b).Fill(b.Zones())
        lid = b.GetLayerID("In2.Cu")
        fills = {z.GetNetname(): z.GetFilledPolysList(lid) for z in made}
        for net in ("A", "B"):
            self.assertGreater(fills[net].Area(), 50e12, net)
            self.assertEqual(fills[net].OutlineCount(), 1, net)  # one piece
        overlap = k.SHAPE_POLY_SET(fills["A"])
        overlap.BooleanIntersection(fills["B"])
        self.assertEqual(overlap.Area(), 0)
        overlap = k.SHAPE_POLY_SET(fills["A"])
        overlap.BooleanIntersection(fills["GND"])
        self.assertEqual(overlap.Area(), 0)
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "part.kicad_pcb")
            k.SaveBoard(path, b)
            b = k.LoadBoard(path)
            rules["ir_drop"] = [
                dict(net=net, sources=["%s0:1" % net], sinks="all", current_a=1.0)
                for net in ("A", "B")
            ]
            result = report(b, rules, Path(tmp) / "ir", path)
        for net in ("A", "B"):
            self.assertEqual(result[net]["status"], "pass", result[net].get("opens"))


if __name__ == "__main__":
    unittest.main()
