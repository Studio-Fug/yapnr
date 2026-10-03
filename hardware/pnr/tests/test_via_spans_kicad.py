"""Blind, buried and micro vias through KiCad (KiCad Python only): writeback of via
types and layer pairs, fixed copper round trip, the native checker's span, plane
drops and plane layers each joined by their own vias."""

import importlib.util
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

NATIVE = importlib.util.find_spec("pcbnew") is not None

SIX = ["F.Cu", "In1.Cu", "In2.Cu", "In3.Cu", "In4.Cu", "B.Cu"]
GAPS6 = [0.09, 0.55, 0.2, 0.55, 0.09]
FAB = dict(
    track_width_mm=0.25,
    clearance_mm=0.2,
    via_diameter_mm=0.6,
    via_drill_mm=0.3,
    hole_clearance_mm=0.25,
    edge_clearance_mm=0.3,
)


def policy(micro=True):
    from pnr.via_policy import resolve

    kinds = ["through", "blind", "buried"] + (["micro"] if micro else [])
    return resolve(
        dict(allowed=kinds, microvia=dict(diameter_mm=0.3, drill_mm=0.1)),
        SIX,
        gaps=GAPS6,
        bonds=["prepreg", "core", "prepreg", "core", "prepreg"],
    )


def vias(b):
    return sorted(
        (
            t.GetNetname(),
            round(t.GetPosition().x / 1e6, 3),
            round(t.GetPosition().y / 1e6, 3),
            b.GetLayerName(t.TopLayer()),
            b.GetLayerName(t.BottomLayer()),
            int(t.GetViaType()),
            round(t.GetWidth(t.TopLayer()) / 1e6, 3),
            round(t.GetDrillValue() / 1e6, 3),
        )
        for t in b.GetTracks()
        if t.GetClass() == "PCB_VIA"
    )


@unittest.skipUnless(NATIVE, "requires KiCad Python")
class Writeback(unittest.TestCase):
    def test_emit_routes_writes_types_pairs_and_sizes(self):
        import pcbnew as k
        from test_stack_kicad import board

        from pnr.writeback import emit_routes

        b, nets = board("SPSPPS")
        codes = {n: v.GetNetCode() for n, v in nets.items()}
        routes = dict(
            tracks=[],
            vias=[["SIG", 5.0, 5.0], ["GND", 8.0, 5.0], ["VCC", 11.0, 5.0], ["GND", 14.0, 5.0]],
            via_spans=[
                ["SIG", 5.0, 5.0, "F.Cu", "In2.Cu", "blind"],
                ["GND", 8.0, 5.0, "F.Cu", "In1.Cu", "micro"],
                ["VCC", 11.0, 5.0, "In2.Cu", "In4.Cu", "buried"],
            ],
        )
        rules = dict(fab=FAB, via_policy=policy())
        emit_routes(b, routes, 20.0, codes, fab=FAB, rules=rules)
        got = {(v[0], v[3], v[4]): v for v in vias(b)}
        self.assertEqual(got[("SIG", "F.Cu", "In2.Cu")][5], int(k.VIATYPE_BLIND))
        self.assertEqual(got[("SIG", "F.Cu", "In2.Cu")][6:], (0.6, 0.3))
        self.assertEqual(got[("GND", "F.Cu", "In1.Cu")][5], int(k.VIATYPE_MICROVIA))
        self.assertEqual(got[("GND", "F.Cu", "In1.Cu")][6:], (0.3, 0.1))
        self.assertEqual(got[("VCC", "In2.Cu", "In4.Cu")][5], int(k.VIATYPE_BURIED))
        self.assertEqual(got[("GND", "F.Cu", "B.Cu")][5], int(k.VIATYPE_THROUGH))
        # A blind via is on its span's layers only.
        blind = next(
            t for t in b.GetTracks() if t.GetClass() == "PCB_VIA" and t.GetNetname() == "SIG"
        )
        self.assertTrue(blind.IsOnLayer(b.GetLayerID("In2.Cu")))
        self.assertFalse(blind.IsOnLayer(b.GetLayerID("B.Cu")))

    def test_routes_without_spans_are_unchanged(self):
        from test_stack_kicad import board

        from pnr.writeback import emit_routes

        b, nets = board("SPSPPS")
        codes = {n: v.GetNetCode() for n, v in nets.items()}
        emit_routes(b, dict(tracks=[], vias=[["SIG", 5.0, 5.0]]), 20.0, codes, fab=FAB)
        (via,) = vias(b)
        self.assertEqual(via[3:5], ("F.Cu", "B.Cu"))
        self.assertEqual(via[6:], (0.6, 0.3))


@unittest.skipUnless(NATIVE, "requires KiCad Python")
class FixedCopper(unittest.TestCase):
    def test_round_trip_keeps_type_and_layers_and_reserves_the_span(self):
        from test_stack_kicad import board

        from pnr.fixed_copper import extract
        from pnr.writeback import emit_routes

        b, nets = board("SPSPPS")
        codes = {n: v.GetNetCode() for n, v in nets.items()}
        routes = dict(
            tracks=[],
            vias=[["SIG", 5.0, 5.0], ["GND", 12.0, 5.0], ["VCC", 18.0, 5.0]],
            via_spans=[
                ["SIG", 5.0, 5.0, "F.Cu", "In2.Cu", "blind"],
                ["GND", 12.0, 5.0, "F.Cu", "In1.Cu", "micro"],
            ],
        )
        emit_routes(b, routes, 20.0, codes, fab=FAB, rules=dict(fab=FAB, via_policy=policy()))
        copper = extract(b)
        kinds = {v["net"]: (v["type"], v.get("layers")) for v in copper["vias"]}
        self.assertEqual(kinds["SIG"], ("blind", ["F.Cu", "In2.Cu"]))
        self.assertEqual(kinds["GND"], ("micro", ["F.Cu", "In1.Cu"]))
        self.assertEqual(kinds["VCC"], ("through", None))
        # Its frame is the engine's (mm, y up) at the board outline's corner.
        sig = next(v for v in copper["vias"] if v["net"] == "SIG")
        self.assertEqual([round(c, 3) for c in sig["xy"]], [5.0, 5.0])


@unittest.skipUnless(NATIVE, "requires KiCad Python")
class Checker(unittest.TestCase):
    def test_span_ignores_copper_below_it(self):
        """A foreign B.Cu track under a site rejects a through via, not an F-In1 one."""
        import pcbnew as k
        from test_stack_kicad import board

        from pnr.native_electrical import Oracle

        b, nets = board("SPSPPS")
        t = k.PCB_TRACK(b)
        t.SetStart(k.VECTOR2I(38_000_000, 40_000_000))
        t.SetEnd(k.VECTOR2I(42_000_000, 40_000_000))
        t.SetWidth(250_000)
        t.SetLayer(k.B_Cu)
        t.SetNetCode(nets["SIG"].GetNetCode())
        b.Add(t)
        oracle = Oracle(b, dict(fab=FAB, net_classes=[]))
        p = (40.0, 40.0)
        self.assertFalse(oracle.via("GND", p, 0.6, 0.3))
        span = (b.GetLayerID("F.Cu"), b.GetLayerID("In1.Cu"))
        self.assertTrue(oracle.via("GND", p, 0.3, 0.1, span=span))
        self.assertFalse(oracle.via("GND", p, 0.6, 0.3, span=(k.F_Cu, k.B_Cu)))
        # A reserved micro via blocks a later via at its hole, whatever the span.
        oracle.reserve_via("GND", p, 0.3, 0.1, span=span, kind="micro")
        self.assertFalse(oracle.via("VCC", (40.2, 40.0), 0.3, 0.1, span=span))


def six_stack():
    from pnr.stack import record_from_rows, resolve

    rows = [
        dict(name=n, type=t, zones=z)
        for n, t, z in zip(
            SIX,
            ["signal", "power", "signal", "power", "power", "signal"],
            [[], ["GND"], [], ["GND"], ["VCC"], []],
        )
    ]
    return resolve(dict(layers=6), record_from_rows(rows))


@unittest.skipUnless(NATIVE, "requires KiCad Python")
class PlaneLayers(unittest.TestCase):
    def board(self, extra):
        """A six-layer board, GND planes on In1 and In3, a GND pad dropping by a
        micro via to In1, plus ``extra`` vias, filled."""
        import pcbnew as k
        from test_stack_kicad import board, smd

        from pnr.writeback import emit_routes

        b, nets = board("SPSPPS", zones=[("In1.Cu", "GND"), ("In3.Cu", "GND"), ("In4.Cu", "VCC")])
        smd(b, "C1", (40.0, 40.0), [("1", "VCC", (-0.8, 0)), ("2", "GND", (0.8, 0))])
        codes = {n: v.GetNetCode() for n, v in nets.items()}
        # Board frame: y down; the writeback frame maps engine (x, height - y).
        routes = dict(
            tracks=[["GND", "F.Cu", [10.8, 10.0], [12.0, 10.0], 0.4]],
            vias=[["GND", 12.0, 10.0]] + [v[:3] for v in extra],
            via_spans=[["GND", 12.0, 10.0, "F.Cu", "In1.Cu", "micro"]] + list(extra),
        )
        emit_routes(b, routes, 20.0, codes, fab=FAB, rules=dict(fab=FAB, via_policy=policy()))
        b.BuildConnectivity()
        k.ZONE_FILLER(b).Fill(b.Zones())
        return b

    def test_each_plane_layer_counts_its_own_vias(self):
        """A micro drop joins In1 only: In3 has no connection (KiCad keeps such a
        fill and its DRC reports isolated copper; the engine counts and reports it)
        until a buried In1-In4 tie joins it."""
        from pnr.writeback import plane_connections

        alone = plane_connections(self.board([]), six_stack())
        self.assertEqual(alone, {"GND": {"In1.Cu": 1, "In3.Cu": 0}, "VCC": {"In4.Cu": 0}})
        stitched = self.board([["GND", 15.0, 10.0, "In1.Cu", "In4.Cu", "buried"]])
        counts = plane_connections(stitched, six_stack())
        self.assertEqual(counts["GND"], {"In1.Cu": 2, "In3.Cu": 1})
        b = stitched
        area = {
            b.GetLayerName(lid): z.GetFilledPolysList(lid).Area()
            for z in b.Zones()
            for lid in z.GetLayerSet().CuStack()
        }
        self.assertGreater(area["In3.Cu"], 0)

    def test_plane_drop_span_follows_policy(self):
        import pcbnew as k
        from test_stack_kicad import board

        from pnr.writeback import plane_drop_span

        b, _ = board("SPSPPS")
        stack = six_stack()
        rules = dict(fab=FAB, via_policy=policy())
        gnd = plane_drop_span(b, rules, stack, "GND", k.F_Cu)
        self.assertEqual(
            (b.GetLayerName(gnd[0]), b.GetLayerName(gnd[1]), gnd[2]), ("F.Cu", "In1.Cu", "micro")
        )
        self.assertEqual(gnd[3:], (0.3, 0.1))
        vcc = plane_drop_span(b, rules, stack, "VCC", k.B_Cu)
        self.assertEqual(
            (b.GetLayerName(vcc[0]), b.GetLayerName(vcc[1]), vcc[2]), ("In4.Cu", "B.Cu", "micro")
        )
        bb = dict(fab=FAB, via_policy=policy(micro=False))
        self.assertEqual(plane_drop_span(b, bb, stack, "GND", k.F_Cu)[2], "blind")
        self.assertIsNone(plane_drop_span(b, dict(fab=FAB), stack, "GND", k.F_Cu))


if __name__ == "__main__":
    unittest.main()
