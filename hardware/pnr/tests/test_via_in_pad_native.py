"""Filled via-in-pad on native KiCad items (run under KiCad Python).

The native Oracle, pnr.via_in_pad audit/sizing/acceptance gate, removed-pad hole
keepouts, pad entry (in-pad vias are never an entry) and the plane-fanout
fallback, under jlc-pofv (5A/5B) and legacy rules.
"""

import unittest

import pcbnew as k

from pnr import fab_profile as fp
from pnr.electrical import compile_policy

FAB = dict(
    outer_copper_oz=1,
    inner_copper_oz=1,
    delta_t_c=40,
    via_drill_mm=0.3,
    via_diameter_mm=0.6,
    min_via_plating_um=20,
    board_thickness_mm=1.6,
    copper_resistivity_ohm_mm=2.1e-5,
    via_barrel_loss_budget_w=0.01,
    via_array_peak_drop_v=0.01,
    neck_loss_budget_w=0.01,
    neck_peak_drop_v=0.005,
)
V = lambda x, y: k.VECTOR2I(round(x * 1e6), round(y * 1e6))


def rules(profile):
    base = compile_policy(
        dict(fab=dict(fp.LEGACY_FAB), net_classes=[], diff_pairs=[]),
        [
            dict(
                ref="U9",
                pads=["1"],
                net="rail",
                rms_current_a=5,
                peak_current_a=16,
                scope="terminal",
                source={},
            )
        ],
        FAB,
    )
    return base if profile == "legacy" else fp.apply_rules(base, profile)


def board():
    b = k.BOARD()
    b.SetCopperLayerCount(4)
    for name in ("sw", "lv", "out", "rail"):
        b.Add(k.NETINFO_ITEM(b, name))
    corners = [(0, 0), (20, 0), (20, 20), (0, 20), (0, 0)]
    for a, z in zip(corners, corners[1:]):
        e = k.PCB_SHAPE(b)
        e.SetShape(k.SHAPE_T_SEGMENT)
        e.SetLayer(k.Edge_Cuts)
        e.SetStart(V(*a))
        e.SetEnd(V(*z))
        e.SetWidth(50000)
        b.Add(e)
    return b


def pad(b, ref, num, net, pos, size, angle=0, pth_drill=None):
    f = next((f for f in b.GetFootprints() if f.GetReference() == ref), None)
    if f is None:
        f = k.FOOTPRINT(b)
        f.SetReference(ref)
        b.Add(f)
        f.SetPosition(V(*pos))
    p = k.PAD(f)
    p.SetNumber(num)
    if pth_drill:
        p.SetAttribute(k.PAD_ATTRIB_PTH)
        p.SetShape(k.PAD_SHAPE_CIRCLE)
        p.SetLayerSet(k.PAD.PTHMask())
        p.SetDrillSize(V(pth_drill, pth_drill))
    else:
        p.SetAttribute(k.PAD_ATTRIB_SMD)
        p.SetShape(k.PAD_SHAPE_RECT)
        ls = k.LSET()
        ls.AddLayer(k.F_Cu)
        p.SetLayerSet(ls)
    p.SetSize(V(*size))
    f.Add(p)
    p.SetPosition(V(*pos))
    p.SetOrientationDegrees(angle)
    p.SetNetCode(b.FindNet(net).GetNetCode())
    return p


def u5(b):
    """TPS552882 pins 24/25/26 (0.38 x 1.43 at 0.62 pitch) and pin 24's 0.20/0.45 footprint via."""
    pad(b, "U5", "24", "lv", (9.38, 10), (0.38, 1.43))
    pad(b, "U5", "24", "lv", (9.38, 10), (0.45, 0.45), pth_drill=0.2)
    strip = pad(b, "U5", "25", "sw", (10, 10), (0.38, 1.43))
    pad(b, "U5", "26", "out", (10.62, 10), (0.38, 1.43))
    return strip


def l_land(b, ref, num, net, pos):
    """A custom L-shaped land (like TPS552882 pin 1) anchored at its inner corner:
    two 0.50-wide arms and a 0.10 anchor square; its graph box is 1.6 x 1.6 about
    the anchor."""
    f = k.FOOTPRINT(b)
    f.SetReference(ref)
    b.Add(f)
    f.SetPosition(V(*pos))
    p = k.PAD(f)
    p.SetNumber(num)
    p.SetAttribute(k.PAD_ATTRIB_SMD)
    ls = k.LSET()
    ls.AddLayer(k.F_Cu)
    p.SetLayerSet(ls)
    p.SetShape(k.PAD_SHAPE_CUSTOM)
    p.SetAnchorPadShape(k.F_Cu, k.PAD_SHAPE_RECT)
    p.SetSize(V(0.1, 0.1))
    arm = [(-0.8, -0.8), (0.05, -0.8), (0.05, -0.3), (-0.3, -0.3), (-0.3, 0.05), (-0.8, 0.05)]
    poly = k.VECTOR_VECTOR2I()
    for x, y in arm:
        poly.append(V(x, y))
    p.AddPrimitivePoly(k.F_Cu, poly, 0, True)
    f.Add(p)
    p.SetPosition(V(*pos))
    p.SetNetCode(b.FindNet(net).GetNetCode())
    return p


def via(b, net, pos, d=0.35, h=0.2):
    v = k.PCB_VIA(b)
    v.SetPosition(V(*pos))
    v.SetViaType(k.VIATYPE_THROUGH)
    v.SetLayerPair(k.F_Cu, k.B_Cu)
    v.SetFrontWidth(round(d * 1e6))
    v.SetDrill(round(h * 1e6))
    v.SetNetCode(b.FindNet(net).GetNetCode())
    b.Add(v)
    return v


class OracleTest(unittest.TestCase):
    def oracle(self, b, profile):
        from pnr.native_electrical import Oracle

        b.BuildConnectivity()
        return Oracle(b, rules(profile))

    def test_jlc_pofv_allows_same_net_filled_via_in_pad(self):
        b = board()
        u5(b)
        o = self.oracle(b, "jlc-pofv")
        for y in (9.5, 10, 10.5):  # the 5B row (pitch 0.50) in pin 25
            self.assertTrue(o.via("sw", (10, y), 0.35, 0.20), y)
        self.assertTrue(o.via("sw", (10, 10), 0.45, 0.20))  # 5B Alternative, centred
        self.assertFalse(o.via("sw", (10, 10), 0.45, 0.30))  # default via is not the in-pad class
        self.assertFalse(o.via("lv", (10, 10), 0.35, 0.20))  # foreign pad
        self.assertFalse(o.via("sw", (10.01, 10), 0.35, 0.20))  # hole edge 0.08 from the strip edge
        self.assertFalse(o.via("sw", (10, 10.6), 0.35, 0.20))  # hole edge 0.015 from the strip end

    def test_rotated_land(self):
        b = board()
        pad(b, "U7", "1", "sw", (5, 5), (0.38, 1.43), angle=90)  # long axis along board x
        o = self.oracle(b, "jlc-pofv")
        self.assertTrue(o.via("sw", (5.5, 5), 0.35, 0.20))  # along the rotated strip
        self.assertFalse(o.via("sw", (5, 5.25), 0.35, 0.20))  # straddles the rotated long edge
        self.assertFalse(o.via("sw", (5, 5.01), 0.35, 0.20))  # off the centreline
        self.assertTrue(o.via("sw", (5, 5.5), 0.35, 0.20))  # outside, copper 0.135 >= 0.127 away

    def test_legacy_is_unchanged(self):
        b = board()
        u5(b)
        pad(b, "U8", "1", "sw", (15, 10), (0.6, 0.6))
        o = self.oracle(b, "legacy")
        self.assertIsNone(o.geometry.in_pad)
        self.assertFalse(o.via("sw", (10, 10), 0.35, 0.20))  # no via in SMD pads
        # 0.10 from its own pad: legal before profiles (0.05 keep-away) ...
        self.assertTrue(o.via("sw", (15 + 0.3 + 0.3 + 0.10, 10), 0.6, 0.3))
        # ... not under 5A (via copper to SMD pad 0.127).
        o = self.oracle(b, "jlc-pofv")
        self.assertFalse(o.via("sw", (15 + 0.3 + 0.225 + 0.10, 10), 0.45, 0.30))
        self.assertTrue(o.via("sw", (15 + 0.3 + 0.225 + 0.14, 10), 0.45, 0.30))


class AuditTest(unittest.TestCase):
    def test_audit_and_inner_pads(self):
        from pnr.via_in_pad import audit, style_in_pad_via

        b = board()
        u5(b)
        pad(b, "U8", "1", "sw", (15, 10), (0.6, 0.6))
        g = fp.geometry(rules("jlc-pofv"))
        vias = [via(b, "sw", (10, y)) for y in (9.5, 10, 10.5)]
        self.assertTrue(all(style_in_pad_via(g, v) for v in vias))
        self.assertTrue(
            all(
                v.Padstack().UnconnectedLayerMode()
                == k.UNCONNECTED_LAYER_MODE_REMOVE_EXCEPT_START_AND_END
                for v in vias
            )
        )
        self.assertFalse(style_in_pad_via(fp.geometry(rules("legacy")), via(b, "sw", (3, 3))))
        via(b, "sw", (15.35, 10), 0.45, 0.30)  # straddles U8.1's edge
        report = audit(b, rules("jlc-pofv"))
        self.assertEqual((report["in_pad_qualified"], report["forbidden"]), (3, 1))
        self.assertEqual(audit(b, rules("legacy"))["forbidden"], 4)  # legacy: none may touch a pad

    def test_sizing(self):
        from pnr.via_in_pad import in_pad_size

        b = board()
        strip = u5(b)
        pads = [p for f in b.GetFootprints() for p in f.Pads()]
        g = fp.geometry(rules("jlc-pofv"))
        self.assertEqual(in_pad_size(g, pads, "sw", (10, 10)), (0.35, 0.20))
        self.assertIsNone(in_pad_size(g, pads, "sw", (12, 12)))
        self.assertIsNone(in_pad_size(fp.geometry(rules("legacy")), pads, "sw", (10, 10)))

    def test_sizing_refuses_unqualified_sites(self):
        """Review repair: in_pad_size returned the 0.20/0.35 class for any point in a
        same-net pad, so emitters placed vias that break 5B where KiCad cannot see."""
        from pnr.via_in_pad import in_pad_size, qualifies

        b = board()
        u5(b)
        corner = l_land(b, "U6", "1", "sw", (5, 5))
        pads = [p for f in b.GetFootprints() for p in f.Pads()]
        g = fp.geometry(rules("jlc-pofv"))
        # 0.03 off the strip centreline: hole edge 0.06 from the edge (5B: 0.09).
        self.assertIsNone(in_pad_size(g, pads, "sw", (10.03, 10)))
        # The L land's anchor (0.10 anchor square only), a point of its box with no
        # copper, and a point of an arm 0.05 from its end (hole margin 0.09 fails).
        for point in [(5, 5), (4.79, 4.99), (4.45, 5.0)]:
            self.assertFalse(any(qualifies(g, corner, point, d, 0.2) for d in g.in_pad.diameters))
            self.assertIsNone(in_pad_size(g, pads, "sw", point), point)
        # The middle of an arm is a 5B site of the general (polygon) land.
        self.assertEqual(in_pad_size(g, pads, "sw", (4.45, 4.8)), (0.35, 0.20))


class GateTest(unittest.TestCase):
    """new_forbidden: the acceptance gate for vias KiCad's DRU exempts (drill 0.20)."""

    def test_new_forbidden(self):
        import tempfile
        from pathlib import Path
        from pnr.via_in_pad import new_forbidden

        before = board()
        u5(before)
        l_land(before, "U6", "1", "sw", (5, 5))
        via(before, "sw", (10, 10))  # qualified in-pad via
        via(before, "sw", (10, 10.6))  # pre-existing break (hole edge 0.015 from the end)
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "before.kicad_pcb")
            k.SaveBoard(path, before)
            before, after = k.LoadBoard(path), k.LoadBoard(path)  # same UUIDs, like validate()
        self.assertEqual(new_forbidden(before, after, rules("jlc-pofv")), [])
        bad = [
            via(after, "sw", (10.03, 10)),
            via(after, "sw", (5, 5)),
        ]  # off-centre strip, L anchor
        via(after, "sw", (10, 9.5))  # a new qualified in-pad via is fine
        self.assertEqual(
            new_forbidden(before, after, rules("jlc-pofv")),
            sorted(v.m_Uuid.AsString() for v in bad),
        )
        self.assertEqual(
            new_forbidden(before, after, rules("legacy")), []
        )  # legacy: KiCad DRC only


class HoleKeepoutTest(unittest.TestCase):
    """Review repair: a removed-pad in-pad via is its bare 0.20 hole on In1/In2;
    foreign copper must keep the via hole clearance (0.20) from the wall, not net
    clearance (0.127) from the hole."""

    def styled(self):
        from pnr.via_in_pad import style_in_pad_via

        b = board()
        pad(b, "C1", "1", "sw", (10, 10), (0.54, 0.6))
        v = via(b, "sw", (10, 10))
        style_in_pad_via(fp.geometry(rules("jlc-pofv")), v)
        b.BuildConnectivity()
        return b, v

    def test_oracle(self):
        from pnr.native_electrical import Oracle

        b, v = self.styled()
        self.assertFalse(v.FlashLayer(k.In1_Cu))
        o = Oracle(b, rules("jlc-pofv"))
        # 0.2 mm In1 track, edge g from the hole wall (hole radius 0.10).
        track = lambda g: o.clear(
            "out", k.In1_Cu, (10 + 0.1 + g + 0.1, 8), (10 + 0.1 + g + 0.1, 12), 0.2
        )
        for g in (0.13, 0.15, 0.18):
            self.assertFalse(track(g), g)
        self.assertTrue(track(0.205))
        # F.Cu keeps the pad: net clearance from the land governs there.
        self.assertTrue(
            o.clear("out", k.F_Cu, (10.27 + 0.128 + 0.1, 8), (10.27 + 0.128 + 0.1, 12), 0.2)
        )
        # Legacy rules on the same board: the keepout follows the via, not the profile
        # (no pre-profile board has a removed-pad via).
        self.assertFalse(
            Oracle(b, rules("legacy")).clear(
                "out", k.In1_Cu, (10 + 0.1 + 0.15 + 0.1, 8), (10 + 0.1 + 0.15 + 0.1, 12), 0.2
            )
        )

    def test_shapes(self):
        from pnr.via_in_pad import clearance_shapes, hole_keepouts

        b, v = self.styled()
        g = fp.geometry(rules("jlc-pofv"))
        self.assertEqual(
            [la for la, _, gap in hole_keepouts(g, v, [k.F_Cu, k.In1_Cu, k.In2_Cu, k.B_Cu])],
            [k.F_Cu, k.In1_Cu, k.In2_Cu, k.B_Cu],
        )
        shapes = clearance_shapes(v, k.In1_Cu, 0.127, 0.20)
        self.assertEqual(len(shapes), 2)
        self.assertEqual(shapes[1].GetRadius(), 100000 + 73000)
        plain = via(b, "sw", (15, 15), 0.45, 0.30)
        self.assertEqual(hole_keepouts(g, plain, [k.In1_Cu]), [])
        self.assertEqual(len(clearance_shapes(plain, k.In1_Cu, 0.127, 0.20)), 1)


class IngestLandTest(unittest.TestCase):
    """graph Pad.land_corner: exact analytic lands only."""

    def test_land_corner(self):
        from pnr.ingest import build_graph

        b = board()
        pad(b, "R1", "1", "sw", (5, 5), (0.54, 0.6))
        pad(b, "R2", "1", "sw", (8, 5), (0.54, 0.6), angle=90)
        pad(b, "R3", "1", "sw", (11, 5), (0.54, 0.6), angle=45)
        rr = pad(b, "R4", "1", "sw", (14, 5), (0.5, 0.8))
        rr.SetShape(k.PAD_SHAPE_ROUNDRECT)
        rr.SetRoundRectRadiusRatio(0.25)
        ov = pad(b, "R5", "1", "sw", (5, 9), (0.5, 0.8))
        ov.SetShape(k.PAD_SHAPE_OVAL)
        l_land(b, "U6", "1", "sw", (10, 12))
        corners = {c.ref: c.pads[0].land_corner for c in build_graph(b).components}
        self.assertEqual(corners["R1"], 0.0)
        self.assertEqual(corners["R2"], 0.0)
        self.assertIsNone(corners["R3"])
        self.assertAlmostEqual(corners["R4"], 0.125, places=6)
        self.assertAlmostEqual(corners["R5"], 0.25, places=6)
        self.assertIsNone(corners["U6"])


class PadEntryTest(unittest.TestCase):
    """Review repair: in-pad vias by themselves are never a pad entry. The array
    counted barrels in the pad, not what they reach (dangling vias qualified a
    0.2 mm lead). Only pnr.via_in_pad.array_attach (the barrels reach one
    full-width trunk on another layer) qualifies an in-pad terminal attach
    (test_in_pad_attach)."""

    def records(self, b, profile):
        from pnr.pad_entry import inspect

        b.BuildConnectivity()
        return {
            p.GetParentFootprint().GetReference() + "." + p.GetNumber(): good
            for p, _, _, _, good in inspect(b, rules(profile))
        }

    def test_in_pad_vias_are_not_an_entry(self):
        from pnr.via_in_pad import pad_array

        b = board()
        pad(b, "U9", "1", "rail", (10, 10), (0.38, 2.6))  # 5 A / 16 A terminal: 5 x 0.20 barrels
        t = k.PCB_TRACK(b)
        t.SetStart(V(10, 11))
        t.SetEnd(V(10, 12))
        t.SetWidth(200000)
        t.SetLayer(k.F_Cu)
        t.SetNetCode(b.FindNet("rail").GetNetCode())
        b.Add(t)  # thin lead
        self.assertEqual(self.records(b, "jlc-pofv"), {"U9.1": False})
        for y in (9.0, 9.5, 10, 10.5, 11.0):  # five dangling qualified in-pad vias
            via(b, "rail", (10, y))
        report = pad_array(
            b, next(p for f in b.GetFootprints() for p in f.Pads()), rules("jlc-pofv")
        )
        self.assertEqual(
            (report["count"], report["required"], report["carries"]), (5, 5, True)
        )  # report only
        for profile in ("jlc-pofv", "legacy"):
            self.assertEqual(self.records(b, profile), {"U9.1": False}, profile)


class PlaneFanoutTest(unittest.TestCase):
    def test_in_pad_fallback(self):
        from pnr.native_electrical import Oracle
        from pnr.writeback import _in_pad_plane_via

        b = board()
        p = pad(b, "C1", "2", "lv", (10, 10), (0.54, 0.6))
        b.BuildConnectivity()
        o = Oracle(b, rules("jlc-pofv"))
        self.assertTrue(_in_pad_plane_via(b, p, rules("jlc-pofv"), o, []))
        (v,) = [t for t in b.GetTracks() if t.GetClass() == "PCB_VIA"]
        self.assertEqual(
            (v.GetWidth(k.F_Cu), v.GetDrillValue(), v.GetPosition()), (350000, 200000, V(10, 10))
        )
        b = board()
        p = pad(b, "C1", "2", "lv", (10, 10), (0.54, 0.6))
        b.BuildConnectivity()
        self.assertFalse(_in_pad_plane_via(b, p, rules("legacy"), Oracle(b, rules("legacy")), []))


if __name__ == "__main__":
    unittest.main()
