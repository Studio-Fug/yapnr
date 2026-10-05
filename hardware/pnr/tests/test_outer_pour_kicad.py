"""An outer pour (plane_partition with a region, ``terminals: pad``, ``connect: solid``)
drawn and filled by KiCad (KiCad Python only: python3 -m unittest
tests.test_outer_pour_kicad). The hot-rod lands (0.25 x 1.82 mm at 0.5 mm pitch) of
three rails are each joined by their rail's pour alone; the pours keep their gap and a
foreign pad inside the region keeps its clearance. Without numpy in KiCad's Python the
test is skipped (the partition itself is numpy)."""

import importlib.util
import unittest

NATIVE = importlib.util.find_spec("pcbnew") is not None
NUMPY = importlib.util.find_spec("numpy") is not None
OFFSET = 30.0
W, H = 14.0, 10.0
HOT = (0.25, 1.82)
LANDS = {
    "SW": [("U2", "1", (5.0, 5.0), HOT), ("U2", "2", (5.5, 5.0), HOT)]
    + [("L1", "1", (5.25, 2.2), (1.4, 1.0))],
    "GND": [("U2", "3", (6.0, 5.0), HOT), ("U2", "4", (6.5, 5.0), HOT)]
    + [("C1", "2", (6.25, 7.6), (0.6, 0.6))],
    "VIN": [("U2", "5", (7.0, 5.0), HOT), ("U2", "6", (7.5, 5.0), HOT)]
    + [("C2", "1", (8.8, 5.0), (0.6, 0.9))],
}
FOREIGN = ("U2", "7", "FB", (9.6, 2.4), (0.3, 0.5))
REGION = [[3.5, 1.2], [10.5, 1.2], [10.5, 8.6], [3.5, 8.6]]


def board():
    import pcbnew as k

    def v(x, y):  # graph frame (y up) to KiCad
        return k.VECTOR2I(round((OFFSET + x) * 1e6), round((OFFSET + H - y) * 1e6))

    b = k.BOARD()
    b.SetCopperLayerCount(2)
    nets = {}
    for name in ("SW", "GND", "VIN", "FB"):
        nets[name] = k.NETINFO_ITEM(b, name)
        b.Add(nets[name])
    edge = k.PCB_SHAPE(b)
    edge.SetShape(k.SHAPE_T_RECT)
    edge.SetStart(v(0, H))
    edge.SetEnd(v(W, 0))
    edge.SetLayer(k.Edge_Cuts)
    edge.SetWidth(50000)
    b.Add(edge)
    feet = {}
    rows = [(ref, num, net, at, size) for net, ls in LANDS.items() for ref, num, at, size in ls]
    for ref, num, net, at, size in rows + [FOREIGN]:
        f = feet.get(ref)
        if f is None:
            f = feet[ref] = k.FOOTPRINT(b)
            f.SetReference(ref)
            b.Add(f)
            f.SetPosition(v(*at))
        p = k.PAD(f)
        p.SetNumber(num)
        p.SetAttribute(k.PAD_ATTRIB_SMD)
        p.SetShape(k.PAD_SHAPE_RECT)
        p.SetSize(k.VECTOR2I(round(size[0] * 1e6), round(size[1] * 1e6)))
        ls = k.LSET()
        ls.AddLayer(k.F_Cu)
        p.SetLayerSet(ls)
        p.SetPosition(v(*at))
        p.SetNet(nets[net])
        f.Add(p)
    return b, v


def rows(clearance):
    from pnr.plane_partition import _CACHE, Terminal, partition

    terms = {
        net: [
            Terminal("%s.%s" % (ref, num), "land", at, max(size) / 2, size)
            for ref, num, at, size in ls
        ]
        for net, ls in LANDS.items()
    }
    _r, _n, _net, (x, y), (w, h) = FOREIGN
    g = clearance
    ring = [(x - w / 2 - g, y - h / 2 - g), (x + w / 2 + g, y - h / 2 - g)]
    ring += [(x + w / 2 + g, y + h / 2 + g), (x - w / 2 - g, y + h / 2 + g)]
    entry = dict(
        layer="F.Cu",
        nets=["SW", "GND", "VIN"],
        order="current",
        split_gap_mm=0.2,
        min_width_mm=0.25,
        fill=None,
        core_no_vias=True,
        terminal_reach_mm=0.8,
        h_mm=0.05,
        region=REGION,
        terminals="pad",
        connect="solid",
    )
    _CACHE.clear()
    part = partition(
        entry,
        width=W,
        height=H,
        terminals=terms,
        blocked=[],
        blocked_polygons=[([ring], frozenset())],
        fill_min_mm=0.15,
        foreign_lands=[ring],
        corridor_mm=0.15 + 2 * clearance,
    )
    return part


@unittest.skipUnless(NATIVE and NUMPY, "requires KiCad Python with numpy")
class OuterPourTest(unittest.TestCase):
    def test_each_rail_joins_its_lands_alone(self):
        import pcbnew as k

        from pnr.writeback import draw_plane_regions

        clearance = 0.15
        part = rows(clearance)
        for net in LANDS:
            self.assertEqual(part.report["nets"][net]["unreached"], [], net)
        b, v = board()
        rules = dict(fab=dict(clearance_mm=clearance, track_width_mm=0.15))
        full = [v(0, 0), v(W, 0), v(W, H), v(0, H)]
        made = draw_plane_regions(b, part.rows(), rules, lambda p: v(*p), full)
        self.assertEqual(sorted({z.GetNetname() for z in made}), ["GND", "SW", "VIN"])
        for z in made:
            self.assertEqual(z.GetPadConnection(), k.ZONE_CONNECTION_FULL)
        k.ZONE_FILLER(b).Fill(b.Zones())
        b.BuildConnectivity()
        conn = b.GetConnectivity()
        lid = b.GetLayerID("F.Cu")
        fills = {}
        for z in made:
            fills.setdefault(z.GetNetname(), k.SHAPE_POLY_SET()).Append(z.GetFilledPolysList(lid))
        for net in LANDS:
            code = b.FindNet(net).GetNetCode()
            # Every land of the rail is in one connected cluster with the pour.
            pads = [p for f in b.GetFootprints() for p in f.Pads() if p.GetNetCode() == code]
            self.assertTrue(pads)
            items = conn.GetConnectedItems(pads[0])
            joined = {(p.GetParentFootprint().GetReference(), p.GetNumber()) for p in pads[1:]}
            seen = {
                (i.GetParentFootprint().GetReference(), i.GetNumber())
                for i in items
                if i.Type() == k.PCB_PAD_T
            }
            self.assertTrue(joined <= seen, (net, joined - seen))
        for a in fills:
            for c in fills:
                if a < c:
                    overlap = k.SHAPE_POLY_SET(fills[a])
                    overlap.BooleanIntersection(fills[c])
                    self.assertEqual(overlap.Area(), 0, (a, c))
        # No pour touches the foreign pad (KiCad keeps the clearance; the pours left
        # it a way out to the region's edge).
        fb = [p for f in b.GetFootprints() for p in f.Pads() if p.GetNetname() == "FB"][0]
        self.assertEqual([i for i in conn.GetConnectedItems(fb) if i.Type() == k.PCB_ZONE_T], [])
        self.assertNotIn("walled_in", part.report)
        # The rails' pours count as their connection: KiCad reports no open item.
        self.assertEqual(conn.GetUnconnectedCount(True), 0)


if __name__ == "__main__":
    unittest.main()
