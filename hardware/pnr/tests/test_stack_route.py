"""Stack-aware detailed routing (pnr.stack + route_board) on small typed boards.

Signals route only on the stack's signal layers, every surface pad of a net with a
dedicated plane drops a through via to it (planned with the signal exits), and a
two-layer stack routes exactly as the legacy heuristic does.
"""

import math
import unittest

from pnr.constraints import compile_constraints, compile_routing_rules
from pnr.graph import BoardGraph, BoardOutline, Component, Net, Pad
from pnr.place.geometry import pad_rects
from pnr.route.detail.router import route_board
from pnr.stack import copper_names, record_from_rows

FAB = dict(
    track_width_mm=0.25,
    clearance_mm=0.2,
    via_diameter_mm=0.6,
    via_drill_mm=0.3,
    hole_clearance_mm=0.25,
    edge_clearance_mm=0.3,
    min_through_drill_mm=0.3,
    via_annular_mm=0.15,
)


def soic(ref, pos, nets, rot=0.0, side="top"):
    """An SO-8 land pattern (1.27 mm pitch, 1.95 x 0.6 mm pads), pins 1-4 west."""
    pads = []
    for k, net in enumerate(nets):
        x = -2.475 if k < 4 else 2.475
        y = 1.905 - 1.27 * (k if k < 4 else 7 - k)
        pads.append(Pad(str(k + 1), net, (x, y), (1.95, 0.6), land_corner=0.0))
    return Component(ref, "SO-8", pos, rot, side, (7.0, 5.5), (7.0, 5.5), pads=pads)


def chip(ref, pos, a, b, rot=0.0, side="top"):
    """A 0603-class two-pad part."""
    pads = [
        Pad("1", a, (-0.8, 0.0), (0.9, 0.95), land_corner=0.0),
        Pad("2", b, (0.8, 0.0), (0.9, 0.95), land_corner=0.0),
    ]
    return Component(ref, "0603", pos, rot, side, (3.0, 1.6), (3.0, 1.6), pads=pads)


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


def compiled(graph, layers, planes=None, keepouts=None):
    doc = dict(
        schema="v0",
        board=dict(outline=dict(w=graph.outline.width, h=graph.outline.height), layers=layers),
        fab=FAB,
        net_class={
            "plane_" + net.lower(): dict(nets=[net], width_mm=0.4, plane_layer=layer)
            for net, layer in (planes or {}).items()
        },
    )
    c = compile_constraints(doc, graph.refs)
    rules = compile_routing_rules(c, [n.name for n in graph.nets])
    if keepouts:
        rules["copper_keepouts"] = keepouts
    return c, rules


def typed(code, zones=None):
    kinds = {"S": "signal", "G": "power", "P": "power"}
    return record_from_rows(
        [
            dict(name=name, type=kinds[role], zones=(zones or {}).get(name, []))
            for name, role in zip(copper_names(len(code)), code)
        ]
    )


def sample():
    """Two SO-8s, decoupling and a resistor ladder: GND and VCC on every part."""
    return board(
        [
            soic("U1", (6.0, 8.0), ["GND", "A", "B", "VCC", "C", "D", "E", "VCC"]),
            soic("U2", (17.0, 8.0), ["A", "B", "GND", "GND", "C", "D", "E", "VCC"]),
            chip("C1", (6.0, 3.0), "VCC", "GND"),
            chip("C2", (17.0, 3.0), "VCC", "GND"),
            chip("R1", (11.5, 13.0), "A", "GND"),
            chip("R2", (11.5, 3.0), "E", "VCC"),
        ]
    )


class StackRouting(unittest.TestCase):
    def assert_drops(self, graph, route, planes):
        """Each surface pad of a plane net starts a stub (>= its width) that ends at
        a through via clear of the pad's own lands."""
        vias = {(n, round(x, 6), round(y, 6)) for n, x, y in route.vias}
        for comp in graph.components:
            for (name, net, rect), pad in zip(pad_rects(comp), comp.pads):
                if net not in planes or pad.through_hole:
                    continue
                stubs = [
                    t
                    for t in route.tracks
                    if t[0] == net and math.dist(t[2], (rect.cx, rect.cy)) < 1e-6
                ]
                self.assertTrue(stubs, "no drop for %s.%s" % (comp.ref, name))
                self.assertTrue(all(t[4] >= 0.4 - 1e-9 for t in stubs))
                ends = {(net, round(t[3][0], 6), round(t[3][1], 6)) for t in stubs}
                # The drop's chain of segments ends at its via.
                chain = [t for t in route.tracks if t[0] == net]
                reached = set(ends)
                for _ in range(4):
                    reached |= {
                        (net, round(t[3][0], 6), round(t[3][1], 6))
                        for t in chain
                        if (net, round(t[2][0], 6), round(t[2][1], 6)) in reached
                    }
                hit = reached & vias
                self.assertTrue(hit, "drop of %s.%s has no via" % (comp.ref, name))
                for _, x, y in hit:
                    dx = max(rect.left - x, 0.0, x - rect.right)
                    dy = max(rect.bottom - y, 0.0, y - rect.top)
                    self.assertGreaterEqual(math.hypot(dx, dy), 0.3 + 0.2 - 1e-6)

    def test_four_layer_sgps_routes_outer_layers_and_drops_every_plane_pad(self):
        g = sample()
        g.stack = typed("SGPS")
        c, rules = compiled(g, 4, {"GND": "In1.Cu", "VCC": "In2.Cu"})
        route = route_board(g, c, rules, pitch=0.25, max_iters=8)
        self.assertEqual(route.grid.layers, ("F.Cu", "B.Cu"))
        self.assertEqual(route.result.unrouted, [])
        self.assertEqual({t[1] for t in route.tracks} - {"F.Cu", "B.Cu"}, set())
        self.assertFalse(any(t[0] in ("GND", "VCC") and t[1] != "F.Cu" for t in route.tracks))
        self.assert_drops(g, route, {"GND", "VCC"})
        # The legacy heuristic routed In1/In2 between the split planes instead.
        g.stack = None
        legacy = route_board(g, c, rules, pitch=0.25, max_iters=8)
        self.assertEqual(legacy.grid.layers, ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"))

    def test_second_ground_plane_from_a_source_zone(self):
        g = sample()
        g.stack = typed("SGGS", {"In2.Cu": ["GND"]})
        c, rules = compiled(g, 4, {"GND": "In1.Cu"})
        route = route_board(g, c, rules, pitch=0.25, max_iters=8)
        self.assertEqual(route.grid.layers, ("F.Cu", "B.Cu"))
        self.assertEqual(route.result.unrouted, [])
        # VCC has no plane on this stack: it is routed, GND drops.
        self.assertIn("VCC", route.result.nets)
        self.assert_drops(g, route, {"GND"})

    def test_six_layer_inner_signal_layer_carries_signals_past_outer_keepouts(self):
        g = sample()
        g.stack = typed("SGSSPS")
        # A copper wall across F and B between the two packages: no-net lands on
        # both sides leave only the inner signal layers to cross it.
        wall = Component(
            "W1",
            "wall",
            (11.5, 8.0),
            0.0,
            "top",
            (0.6, 9.0),
            (0.6, 9.0),
            pads=[Pad("1", "", (0.0, 0.0), (0.6, 9.0))],
        )
        back = Component(
            "W2",
            "wall",
            (11.5, 8.0),
            0.0,
            "bottom",
            (0.6, 9.0),
            (0.6, 9.0),
            pads=[Pad("1", "", (0.0, 0.0), (0.6, 9.0))],
        )
        g.components = [x for x in g.components if x.ref in ("U1", "U2")] + [wall, back]
        c, rules = compiled(g, 6, {"GND": "In1.Cu", "VCC": "In4.Cu"})
        route = route_board(g, c, rules, pitch=0.25, max_iters=4)
        self.assertEqual(route.grid.layers, ("F.Cu", "In2.Cu", "In3.Cu", "B.Cu"))
        self.assertEqual(route.result.unrouted, [])
        layers = {t[1] for t in route.tracks}
        self.assertTrue(layers & {"In2.Cu", "In3.Cu"})
        self.assertFalse(layers & {"In1.Cu", "In4.Cu"})
        self.assert_drops(g, route, {"GND", "VCC"})

    def test_two_layer_stack_routes_as_the_legacy_heuristic(self):
        g = board(
            [
                soic("U1", (6.0, 6.0), ["GND", "A", "B", "VCC", "C", "A", "B", "VCC"]),
                chip("C1", (6.0, 1.8), "VCC", "GND"),
                chip("R1", (11.0, 6.0), "C", "GND"),
            ],
            size=(14.0, 11.0),
        )
        c, rules = compiled(g, 2)
        legacy = route_board(g, c, rules, pitch=0.25, max_iters=4)
        g.stack = typed("SS")
        stacked = route_board(g, c, rules, pitch=0.25, max_iters=4)
        self.assertEqual(stacked.tracks, legacy.tracks)
        self.assertEqual(stacked.vias, legacy.vias)
        self.assertEqual(stacked.result.unrouted, legacy.result.unrouted)

    def test_a_pad_without_a_drop_is_unrouted_at_its_location(self):
        g = board(
            [
                chip("C1", (6.0, 6.0), "VCC", "GND"),
                chip("C2", (10.0, 6.0), "VCC", "GND"),
            ],
            size=(16.0, 12.0),
        )
        g.stack = typed("SGPS")
        # A keep-out over C1's ground land leaves it no via site.
        c, rules = compiled(g, 4, {"GND": "In1.Cu", "VCC": "In2.Cu"})
        rules["copper_keepouts"] = [dict(name="k", ref="C1", rect_mm=[0.2, -1.0, 2.0, 1.0])]
        route = route_board(g, c, rules, pitch=0.25, max_iters=4)
        self.assertIn("GND", route.result.unrouted)
        sites = route.failure_sites["GND"]
        self.assertTrue(any(math.dist(p, (6.8, 6.0)) < 1e-6 for p in sites))


class CurrentLayers(unittest.TestCase):
    def test_a_current_rated_net_stays_off_thin_inner_copper(self):
        from pnr.route.detail.router import current_layer_mask
        from pnr.stack import resolve

        rows = [
            dict(name="F.Cu", type="signal", copper_mm=0.035),
            dict(name="In1.Cu", type="signal", copper_mm=0.0152),
            dict(name="In2.Cu", type="power", copper_mm=0.0152),
            dict(name="B.Cu", type="signal", copper_mm=0.035),
        ]
        rules = dict(
            layers=4,
            net_classes=[
                dict(name="supply", nets=["VBUS"], width_mm=0.4, current_a=0.5, delta_t_c=10.0),
                dict(name="logic", nets=["VCC"], width_mm=0.4, current_a=0.1, delta_t_c=10.0),
                dict(name="plane_gnd", nets=["GND"], width_mm=0.4, plane_layer="In2.Cu"),
            ],
        )
        stack = resolve(rules, record_from_rows(rows))
        layers = stack.grid_layers
        self.assertEqual(layers, ("F.Cu", "In1.Cu", "B.Cu"))
        # IPC-2221 internal: 0.5 A at 10 C rise on 0.0152 mm needs about 0.69 mm.
        mask = current_layer_mask(stack, layers, rules, {"VBUS": 0.4, "VCC": 0.4}, 0.25)
        self.assertEqual(mask, {"VBUS": frozenset({0, 2})})
        # Wide enough for the inner copper: no restriction.
        self.assertEqual(
            current_layer_mask(stack, layers, rules, {"VBUS": 0.7, "VCC": 0.4}, 0.25), {}
        )


class DropPlanning(unittest.TestCase):
    def test_no_via_in_a_pad_without_an_in_pad_policy(self):
        from pnr.route.detail.grid import RouteGrid
        from pnr.route.detail.joint_escape import enumerate_drops

        g = sample()
        grid = RouteGrid.from_graph(
            g, 24.0, 16.0, pitch=0.25, clearance=0.2, track_width=0.25, via_radius=0.3
        )
        rect = pad_rects(g.component("C1"))[1][2]
        options = enumerate_drops(
            grid, "GND", (rect.cx, rect.cy), 0, rect=rect, width=0.4, via_keepout=3, reach=8
        )
        self.assertTrue(options)
        for o in options:
            ((x, y),) = o.vias
            gap = math.hypot(
                max(rect.left - x, 0, x - rect.right), max(rect.bottom - y, 0, y - rect.top)
            )
            self.assertGreaterEqual(gap, 0.3 + 0.2 - 1e-9)  # via radius + clearance

    def test_so8_row_resolves_drops_and_signal_exits_together(self):
        from pnr.route.detail.escape import plan_escapes
        from pnr.route.detail.grid import RouteGrid

        g = board(
            [soic("U1", (6.0, 6.0), ["GND", "A", "GND", "B", "VCC", "C", "VCC", "D"])],
            size=(12.0, 12.0),
        )
        grid = RouteGrid.from_graph(
            g, 12.0, 12.0, pitch=0.25, clearance=0.2, track_width=0.25, via_radius=0.3
        )
        grid.net_widths = {"GND": 0.4, "VCC": 0.4}
        plan = plan_escapes(
            grid,
            g,
            {"A", "B", "C", "D"},
            via_keepout=3,
            dogbone_reach=8,
            drop_widths={"GND": 0.4, "VCC": 0.4},
        )
        self.assertEqual(plan.blocked_nets, set())
        self.assertEqual(plan.drop_failures, {})
        self.assertNotIn("GND", plan.net_access)
        drops = [e for e in plan.escapes if e.net in ("GND", "VCC")]
        self.assertEqual(len(drops), 4)
        self.assertTrue(all(e.via_xy is not None for e in drops))
        self.assertTrue(plan.diagnostics["complete"])


if __name__ == "__main__":
    unittest.main()
