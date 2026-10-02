"""The board's copper stack through KiCad (KiCad Python only): the ingest record,
copper emitted on any named layer, and the plane layers writeback forms."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

NATIVE = importlib.util.find_spec("pcbnew") is not None


def board(code, *, zones=(), size=(30.0, 20.0), offset=30.0):
    """A board with ``len(code)`` copper layers typed from ``code`` (S signal, P power,
    M mixed), a rectangular outline at the writeback page offset, and two nets."""
    import pcbnew as k

    b = k.BOARD()
    b.SetCopperLayerCount(len(code))
    kinds = {"S": k.LT_SIGNAL, "P": k.LT_POWER, "M": k.LT_MIXED}
    for lid, role in zip(b.GetEnabledLayers().CuStack(), code):
        b.SetLayerType(lid, kinds[role])
    b.GetDesignSettings().m_HasStackup = True
    nets = {}
    for name in ("GND", "VCC", "SIG"):
        nets[name] = k.NETINFO_ITEM(b, name)
        b.Add(nets[name])
    edge = k.PCB_SHAPE(b)
    edge.SetShape(k.SHAPE_T_RECT)
    edge.SetStart(k.VECTOR2I(round(offset * 1e6), round(offset * 1e6)))
    edge.SetEnd(k.VECTOR2I(round((offset + size[0]) * 1e6), round((offset + size[1]) * 1e6)))
    edge.SetLayer(k.Edge_Cuts)
    edge.SetWidth(50000)
    b.Add(edge)
    for layer, net in zones:
        z = k.ZONE(b)
        z.SetLayer(b.GetLayerID(layer))
        z.SetNetCode(nets[net].GetNetCode())
        outline = z.Outline()
        outline.NewOutline()
        for x, y in ((0, 0), (size[0], 0), (size[0], size[1]), (0, size[1])):
            outline.Append(k.VECTOR2I(round((offset + x) * 1e6), round((offset + y) * 1e6)))
        b.Add(z)
    return b, nets


def smd(b, ref, at, pads, *, size=(0.9, 0.95), bottom=False):
    """A footprint at ``at`` (board mm) with SMD pads ``[(number, net, (dx, dy))]``."""
    import pcbnew as k

    f = k.FOOTPRINT(b)
    f.SetReference(ref)
    b.Add(f)
    f.SetPosition(k.VECTOR2I(round(at[0] * 1e6), round(at[1] * 1e6)))
    for number, net, (dx, dy) in pads:
        p = k.PAD(f)
        p.SetNumber(number)
        p.SetAttribute(k.PAD_ATTRIB_SMD)
        p.SetShape(k.PAD_SHAPE_RECT)
        p.SetSize(k.VECTOR2I(round(size[0] * 1e6), round(size[1] * 1e6)))
        layers = k.LSET()
        layers.AddLayer(k.F_Cu)
        p.SetLayerSet(layers)
        p.SetPosition(k.VECTOR2I(round((at[0] + dx) * 1e6), round((at[1] + dy) * 1e6)))
        p.SetNet(b.FindNet(net))
        f.Add(p)
    if bottom:
        f.Flip(f.GetPosition(), False)
    return f


@unittest.skipUnless(NATIVE, "requires KiCad Python")
class EmitOnNamedLayers(unittest.TestCase):
    def test_tracks_land_on_any_inner_layer_and_unknown_layers_raise(self):
        from pnr.writeback import copper_layer, emit_routes

        b, nets = board("SPSSPSPS")
        codes = {n: v.GetNetCode() for n, v in nets.items()}
        routes = dict(
            tracks=[
                ["SIG", "In5.Cu", [1.0, 1.0], [5.0, 1.0], 0.25],
                ["SIG", "In2.Cu", [1.0, 2.0], [5.0, 2.0], 0.25],
            ],
            vias=[["SIG", 5.0, 1.0]],
        )
        emit_routes(b, routes, 20.0, codes)
        layers = sorted(
            b.GetLayerName(t.GetLayer()) for t in b.GetTracks() if t.GetClass() == "PCB_TRACK"
        )
        self.assertEqual(layers, ["In2.Cu", "In5.Cu"])
        self.assertEqual(copper_layer(b, "B.Cu"), b.GetLayerID("B.Cu"))
        for name in ("In7.Cu", "Bogus.Cu"):
            with self.assertRaises(ValueError):
                copper_layer(b, name)
        with self.assertRaises(ValueError):
            emit_routes(b, dict(tracks=[["SIG", "In7.Cu", [1, 1], [2, 1], 0.25]]), 20.0, codes)


@unittest.skipUnless(NATIVE, "requires KiCad Python")
class IngestRecord(unittest.TestCase):
    def test_stackup_board_records_types_zones_and_thickness(self):
        import pcbnew as k

        from pnr.ingest import load
        from pnr.stack import resolve

        b, _ = board("SPSPPS", zones=[("In3.Cu", "GND")])
        smd(b, "C1", (40.0, 40.0), [("1", "VCC", (-0.8, 0)), ("2", "GND", (0.8, 0))])
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "six.kicad_pcb")
            k.SaveBoard(path, b)
            text = Path(path).read_text()
            # A stackup block states the copper thickness (KiCad 10 writes one when
            # the board declares a physical stackup; insert it if this build did not).
            if '(type "copper")' not in text:
                rows = "".join(
                    '\n\t\t\t(layer "%s" (type "copper") (thickness %s))'
                    % (name, "0.035" if name in ("F.Cu", "B.Cu") else "0.0152")
                    for name in ["F.Cu"] + ["In%d.Cu" % i for i in range(1, 5)] + ["B.Cu"]
                )
                text = text.replace("(setup", "(setup\n\t\t(stackup" + rows + "\n\t\t)", 1)
                Path(path).write_text(text)
            g = load(path)
        self.assertIsNotNone(g.stack)
        rows = g.stack["layers"]
        self.assertEqual([r["name"] for r in rows][:2], ["F.Cu", "In1.Cu"])
        self.assertEqual(
            [r["type"] for r in rows], ["signal", "power", "signal", "power", "power", "signal"]
        )
        self.assertEqual(rows[3]["zones"], ["GND"])
        self.assertAlmostEqual(rows[0]["copper_mm"], 0.035)
        self.assertAlmostEqual(rows[1]["copper_mm"], 0.0152)
        rules = dict(layers=6, net_classes=[dict(name="p", nets=["VCC"], plane_layer="In4.Cu")])
        stack = resolve(rules, g.stack)
        self.assertEqual(stack.grid_layers, ("F.Cu", "In2.Cu", "B.Cu"))
        self.assertEqual(stack.dedicated, (("In3.Cu", "GND"), ("In4.Cu", "VCC")))
        self.assertEqual(json.loads(g.to_json())["stack"], g.stack)

    def test_board_without_stackup_records_nothing(self):
        import pcbnew as k

        from pnr.ingest import load

        b, _ = board("SPPS")
        b.GetDesignSettings().m_HasStackup = False
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "plain.kicad_pcb")
            k.SaveBoard(path, b)
            g = load(path)
        self.assertIsNone(g.stack)
        self.assertNotIn('"stack"', g.to_json())


FAB = dict(
    track_width_mm=0.25,
    clearance_mm=0.2,
    via_diameter_mm=0.6,
    via_drill_mm=0.3,
    hole_clearance_mm=0.25,
    edge_clearance_mm=0.3,
)


def stack_of(code, planes, zones=()):
    from pnr.stack import copper_names, record_from_rows, resolve

    kinds = {"S": "signal", "P": "power"}
    rows = []
    for name, role in zip(copper_names(len(code)), code):
        rows.append(dict(name=name, type=kinds[role], zones=[n for la, n in zones if la == name]))
    rules = dict(
        layers=len(code),
        fab=FAB,
        net_classes=[
            dict(name="p_" + n.lower(), nets=[n], plane_layer=la, width_mm=0.4)
            for n, la in planes.items()
        ],
    )
    return resolve(rules, record_from_rows(rows)), rules


@unittest.skipUnless(NATIVE, "requires KiCad Python")
class FormPlanes(unittest.TestCase):
    def zones(self, b):
        return sorted(
            (b.GetLayerName(z.GetLayer()), z.GetNetname(), z.GetAssignedPriority())
            for z in b.Zones()
            if not z.GetIsRuleArea()
        )

    def test_every_dedicated_plane_gets_a_full_outline_zone_and_source_zones_stay(self):
        from pnr.writeback import form_planes

        b, _ = board("SPPPPS", zones=[("In2.Cu", "GND")])
        smd(b, "C1", (40.0, 40.0), [("1", "VCC", (-0.8, 0)), ("2", "GND", (0.8, 0))])
        stack, rules = stack_of(
            "SPPPPS",
            {"GND": "In1.Cu", "VCC": "In4.Cu"},
            zones=[("In2.Cu", "GND"), ("In3.Cu", "GND")],
        )
        source = [z for z in b.Zones()][0]
        formed = form_planes(b, stack, rules, 30.0, 20.0)
        self.assertEqual(
            sorted(formed["zones"]), [("In1.Cu", "GND"), ("In3.Cu", "GND"), ("In4.Cu", "VCC")]
        )
        self.assertEqual(
            self.zones(b),
            [
                ("In1.Cu", "GND", 0),
                ("In2.Cu", "GND", 0),
                ("In3.Cu", "GND", 0),
                ("In4.Cu", "VCC", 0),
            ],
        )
        self.assertIn(source, list(b.Zones()))
        for z in b.Zones():
            box = z.GetBoundingBox()
            self.assertEqual((box.GetWidth(), box.GetHeight()), (30_000_000, 20_000_000))
        # Idempotent: a second pass declares nothing new.
        self.assertEqual(form_planes(b, stack, rules, 30.0, 20.0)["zones"], [])

    def test_shared_plane_layer_gives_the_outline_to_the_net_with_most_pads(self):
        from pnr.writeback import form_planes

        b, _ = board("SPPS")
        smd(b, "C1", (36.0, 36.0), [("1", "VCC", (-0.8, 0)), ("2", "GND", (0.8, 0))])
        smd(b, "C2", (44.0, 36.0), [("1", "SIG", (-0.8, 0)), ("2", "GND", (0.8, 0))])
        stack, rules = stack_of(
            "SPPS", {"GND": "In1.Cu", "VCC": "In1.Cu"}, zones=[("In2.Cu", "GND")]
        )
        form_planes(b, stack, rules, 30.0, 20.0)
        rows = {
            (la, n): z
            for z in b.Zones()
            for la, n in [(b.GetLayerName(z.GetLayer()), z.GetNetname())]
        }
        gnd, vcc = rows[("In1.Cu", "GND")], rows[("In1.Cu", "VCC")]
        self.assertEqual(gnd.GetAssignedPriority(), 0)
        self.assertGreater(vcc.GetAssignedPriority(), gnd.GetAssignedPriority())
        self.assertEqual(gnd.GetBoundingBox().GetWidth(), 30_000_000)
        self.assertLess(vcc.GetBoundingBox().GetWidth(), 30_000_000)

    def test_fallback_drops_only_pads_without_a_through_contact(self):
        import pcbnew as k

        from pnr.writeback import _has_through_access, form_planes

        b, nets = board("SPPS")
        smd(b, "C1", (38.0, 40.0), [("1", "VCC", (-0.8, 0)), ("2", "GND", (0.8, 0))])
        smd(b, "C2", (46.0, 40.0), [("1", "VCC", (-0.8, 0)), ("2", "GND", (0.8, 0))])
        # C1.2 already drops to a via; the other plane pads have nothing.
        via = k.PCB_VIA(b)
        via.SetPosition(k.VECTOR2I(38_800_000, 42_000_000))
        via.SetViaType(k.VIATYPE_THROUGH)
        via.SetLayerPair(k.F_Cu, k.B_Cu)
        via.SetWidth(600_000)
        via.SetDrill(300_000)
        via.SetNetCode(nets["GND"].GetNetCode())
        b.Add(via)
        t = k.PCB_TRACK(b)
        t.SetStart(k.VECTOR2I(38_800_000, 40_000_000))
        t.SetEnd(k.VECTOR2I(38_800_000, 42_000_000))
        t.SetWidth(400_000)
        t.SetLayer(k.F_Cu)
        t.SetNetCode(nets["GND"].GetNetCode())
        b.Add(t)
        stack, rules = stack_of("SPPS", {"GND": "In1.Cu", "VCC": "In2.Cu"})
        formed = form_planes(b, stack, rules, 30.0, 20.0)
        self.assertEqual(formed["fallback_vias"], 3)
        vias = [x for x in b.GetTracks() if x.GetClass() == "PCB_VIA"]
        self.assertEqual(len(vias), 4)
        b.BuildConnectivity()
        for fp in b.GetFootprints():
            for pad in fp.Pads():
                self.assertTrue(_has_through_access(b, pad), fp.GetReference() + pad.GetNumber())
        # Nothing left to drop: a second pass adds no via.
        self.assertEqual(form_planes(b, stack, rules, 30.0, 20.0)["fallback_vias"], 0)


if __name__ == "__main__":
    unittest.main()
