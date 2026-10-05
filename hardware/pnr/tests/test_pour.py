"""Declared outer-layer pours (``pour:``, pnr.pour, stage 3c E6): the router leaves
the net's pads on the pour's layer to the pour (no escape, no plane drop); KiCad's
side (draw, fill, stitch) is tests/test_pour_kicad.py."""

import unittest

from tests.test_stack_route import chip, compiled, soic, typed

from pnr.constraints import ConstraintError, compile_constraints, compile_routing_rules
from pnr.graph import BoardGraph, BoardOutline, Net
from pnr.power_spec import PowerSpecError, parse_pour


def board(components, size=(24.0, 16.0)):
    nets = {}
    for c in components:
        for p in c.pads:
            if p.net:
                nets.setdefault(p.net, []).append((c.ref, p.name))
    return BoardGraph(
        name="t",
        components=components,
        nets=[Net(n, i + 1, pins) for i, (n, pins) in enumerate(sorted(nets.items()))],
        outline=BoardOutline(*size),
    )


class SpecTest(unittest.TestCase):
    def test_entries(self):
        (row,) = parse_pour([dict(layer="B.Cu", net="GND")])
        self.assertEqual(row, dict(layer="B.Cu", net="GND", stitch=True, connect="thermal"))
        (row,) = parse_pour([dict(layer="F.Cu", net="GND", connect="solid", clearance_mm=0.2)])
        self.assertEqual(row["clearance_mm"], 0.2)
        self.assertEqual(parse_pour(None), [])
        for bad, msg in (
            (dict(layer="In1.Cu", net="GND"), "outer layer"),
            (dict(layer="B.Cu"), "names the pour"),
            (dict(layer="B.Cu", net="GND", stitch="yes"), "boolean"),
            (dict(layer="B.Cu", net="GND", connect="glue"), "solid or thermal"),
            (dict(layer="B.Cu", net="GND", fill=1), "unknown key"),
        ):
            with self.assertRaisesRegex(PowerSpecError, msg):
                parse_pour([bad])
        with self.assertRaisesRegex(PowerSpecError, "one entry per"):
            parse_pour([dict(layer="B.Cu", net="GND")] * 2)

    def test_rules_carry_the_section_only_when_declared(self):
        g = board([chip("C1", (5.0, 5.0), "VCC", "GND")])
        doc = {"schema": "v0", "board": {"outline": {"w": 24, "h": 16}}}
        c = compile_constraints(doc, g.refs)
        self.assertNotIn("pours", compile_routing_rules(c, ["VCC", "GND"]))
        doc["pour"] = [{"layer": "B.Cu", "net": "GND"}]
        c = compile_constraints(doc, g.refs)
        rules = compile_routing_rules(c, ["VCC", "GND"])
        self.assertEqual(rules["pours"][0]["net"], "GND")
        doc["pour"] = [{"layer": "In2.Cu", "net": "GND"}]
        with self.assertRaises(ConstraintError):
            compile_constraints(doc, g.refs)


class RouteTest(unittest.TestCase):
    """A bottom decoupling cap under an SO-8 (4 layers, GND and VCC planes): with a
    B.Cu GND pour its GND pad gets no drop of its own; the pour is its connection."""

    def route(self, pour):
        from pnr.route.detail.router import route_board

        g = board(
            [
                soic("U1", (8.0, 8.0), ["GND", "A", "B", "VCC", "C", "D", "E", "VCC"]),
                chip("C9", (8.0, 8.0), "VCC", "GND", side="bottom"),
                chip("R1", (16.0, 4.0), "A", "B"),
                chip("R2", (16.0, 12.0), "C", "D"),
                chip("R3", (3.0, 12.0), "E", "GND"),
            ]
        )
        g.stack = typed("SGPS")
        c, rules = compiled(g, 4, planes={"GND": "In1.Cu", "VCC": "In2.Cu"})
        if pour:
            rules["pours"] = parse_pour([dict(layer="B.Cu", net="GND")])
        return g, route_board(g, c, rules, max_iters=4)

    def test_the_pour_pads_get_no_drop(self):
        from pnr.place.geometry import pad_rects

        g, plain = self.route(False)
        _g, poured = self.route(True)
        self.assertNotIn("pour_pads", plain.escape_diagnostics)
        self.assertEqual(poured.escape_diagnostics["pour_pads"], ["C9.2"])
        pad = {name: r for name, _net, r in pad_rects(g.component("C9"))}["2"]
        at = (pad.cx, pad.cy)

        def stub(r):  # a GND drop stub from the pad
            return [t for t in r.tracks if t[0] == "GND" and t[1] == "B.Cu" and at in t[2:4]]

        self.assertTrue(stub(plain) or "GND" in plain.failure_sites)  # a drop, or a failure
        self.assertFalse(stub(poured))
        self.assertNotIn("GND", poured.failure_sites)
        # The VCC pad of the same part still drops (another net).
        vcc = {name: r for name, _net, r in pad_rects(g.component("C9"))}["1"]
        self.assertTrue(
            [t for t in poured.tracks if t[0] == "VCC" and (vcc.cx, vcc.cy) in t[2:4]]
            or "VCC" in poured.failure_sites
        )


if __name__ == "__main__":
    unittest.main()
