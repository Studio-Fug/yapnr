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


def sot23_5(ref, pos, nets, rot=0.0, side="top"):
    """A SOT-23-5 land pattern: pins 1-3 south at 0.95 mm pitch, pins 4-5 north
    (no middle pin), 0.6 x 1.325 mm pads on rows 2.275 mm apart."""
    at = [(-0.95, -1.1375), (0.0, -1.1375), (0.95, -1.1375), (0.95, 1.1375), (-0.95, 1.1375)]
    pads = [
        Pad(str(k + 1), net, xy, (0.6, 1.325), land_corner=0.0)
        for k, (net, xy) in enumerate(zip(nets, at))
    ]
    return Component(ref, "SOT-23-5", pos, rot, side, (3.3, 3.6), (3.3, 3.6), pads=pads)


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


class SharedPlaneLayer(unittest.TestCase):
    """GND and VCC both on a dedicated In1 (several nets on one plane layer): VCC
    fills its pads' bounding box at a higher priority, GND the rest of the outline.
    A GND drop inside VCC's box would touch only an island of GND copper, so the
    router must not take it, and must not call GND complete without it."""

    def test_drops_stay_in_their_own_region_and_the_rest_are_reported(self):
        from pnr.stack import PlaneAccess, plane_regions, resolve

        g = sample()
        g.stack = typed("SPSS")
        c, rules = compiled(g, 4, {"GND": "In1.Cu", "VCC": "In1.Cu"})
        route = route_board(g, c, rules, pitch=0.25, max_iters=8)
        stack = resolve(rules, g.stack)
        pads = {}
        for comp in g.components:
            for _name, net, rect in pad_rects(comp):
                pads.setdefault(net, []).append((rect.cx, rect.cy))
        access = PlaneAccess(stack, plane_regions(stack, g.stack, pads, 24.0, 16.0), 0.3, 0.75)
        drops = [(n, (x, y)) for n, x, y in route.vias if n in ("GND", "VCC")]
        self.assertTrue(drops)
        for net, p in drops:
            self.assertTrue(access.site_ok(net, p), (net, p))
        # VCC's box covers most GND pads of this sample: those have no drop and GND
        # is reported unrouted at each of them.
        self.assertIn("GND", route.result.unrouted)
        self.assertNotIn("VCC", route.result.unrouted)
        sites = route.failure_sites["GND"]
        self.assertTrue(sites)
        for p in sites:
            self.assertTrue(any(math.dist(p, q) < 1e-6 for q in pads["GND"]))
        self.assertIn("shared by GND, VCC", " ".join(route.stack_warnings))
        self.assertEqual(route.escape_diagnostics["stack_warnings"], route.stack_warnings)

    def test_with_a_second_ground_plane_every_pad_drops(self):
        g = sample()
        g.stack = typed("SPPS", {"In2.Cu": ["GND"]})
        c, rules = compiled(g, 4, {"GND": "In1.Cu", "VCC": "In1.Cu"})
        route = route_board(g, c, rules, pitch=0.25, max_iters=8)
        self.assertEqual(route.result.unrouted, [])


class DropWidths(unittest.TestCase):
    def test_each_drop_stub_has_its_own_pads_width(self):
        import pnr.pad_entry as pad_entry

        g = sample()
        g.stack = typed("SGPS")
        c, rules = compiled(g, 4, {"GND": "In1.Cu", "VCC": "In2.Cu"})
        original = pad_entry.terminal_required_width
        # A terminal contract widens only C1's ground pad.
        pad_entry.terminal_required_width = lambda ref, number, net, rules: (
            0.6 if (ref, number) == ("C1", "2") else original(ref, number, net, rules)
        )
        try:
            route = route_board(g, c, rules, pitch=0.25, max_iters=8)
        finally:
            pad_entry.terminal_required_width = original
        widths = {}
        for comp in g.components:
            for (name, net, rect), pad in zip(pad_rects(comp), comp.pads):
                if net == "GND":
                    stubs = [
                        t
                        for t in route.tracks
                        if t[0] == "GND" and math.dist(t[2], (rect.cx, rect.cy)) < 1e-6
                    ]
                    if stubs:
                        widths[(comp.ref, name)] = {t[4] for t in stubs}
        self.assertEqual(widths.pop(("C1", "2")), {0.6})
        self.assertTrue(widths)
        self.assertEqual(set().union(*widths.values()), {0.4})


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


class CurrentLayersEveryKernel(unittest.TestCase):
    """The current rule's layer mask (``RouteGrid.layer_mask``) holds whichever A*
    kernel routes. Each kernel switch is tried, including ``PNR_MAZE_KERNEL``
    values this tree may not know yet: a kernel that precomputes passability must
    still apply the mask (a merged tree runs them all here)."""

    def test_a_current_rated_net_never_takes_the_thin_inner_layer(self):
        import os

        rows = [
            dict(name="F.Cu", type="signal", copper_mm=0.035),
            dict(name="In1.Cu", type="signal", copper_mm=0.0152),
            dict(name="In2.Cu", type="power", copper_mm=0.0152),
            dict(name="B.Cu", type="signal", copper_mm=0.035),
        ]
        wall = [
            Component(
                ref,
                "wall",
                (12.0, 7.0),
                0.0,
                side,
                (0.6, 12.0),
                (0.6, 12.0),
                pads=[Pad("1", "", (0.0, 0.0), (0.6, 12.0))],
            )
            for ref, side in (("W1", "top"), ("W2", "bottom"))
        ]
        g = board(
            [
                chip("R1", (5.0, 7.0), "VBUS", "A"),
                chip("R2", (19.0, 7.0), "VBUS", "A"),
                chip("C1", (5.0, 10.0), "GND", "B"),
                chip("C2", (19.0, 10.0), "GND", "B"),
            ]
            + wall
        )
        g.stack = record_from_rows(rows)
        c, rules = compiled(g, 4, {"GND": "In2.Cu"})
        supply = dict(name="supply", nets=["VBUS"], width_mm=0.4, delta_t_c=10.0)
        rules["net_classes"].append(supply)
        # Without a current rating VBUS crosses the wall on In1 too.
        free = route_board(g, c, rules, pitch=0.25, max_iters=4)
        self.assertIn("In1.Cu", {t[1] for t in free.tracks if t[0] == "VBUS"})
        supply["current_a"] = 0.5
        saved = {k: os.environ.get(k) for k in ("PNR_PACKED_MAZE", "PNR_MAZE_KERNEL")}
        try:
            for packed in (None, "0", "1"):
                for kernel in (None, "reference", "packed", "native"):
                    for key, value in (("PNR_PACKED_MAZE", packed), ("PNR_MAZE_KERNEL", kernel)):
                        if value is None:
                            os.environ.pop(key, None)
                        else:
                            os.environ[key] = value
                    with self.subTest(packed=packed, kernel=kernel):
                        route = route_board(g, c, rules, pitch=0.25, max_iters=4)
                        self.assertEqual(route.grid.layers, ("F.Cu", "In1.Cu", "B.Cu"))
                        layers = {t[1] for t in route.tracks if t[0] == "VBUS"}
                        self.assertNotIn("In1.Cu", layers)
                        # The wall leaves In1 the only crossing: the signals use it.
                        self.assertIn("In1.Cu", {t[1] for t in route.tracks if t[0] in "AB"})
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


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

    def test_middle_pin_by_the_edge_drops_under_the_body_at_exact_clearance(self):
        """A SOT-23-5's middle pin facing the board edge: the only via sites are
        under the body, between foreign pads whose cell-rounded halos cover them.
        The pad rectangles are judged exactly; a halo another reservation wrote
        still rules a site out."""
        from pnr.route.detail.grid import RouteGrid
        from pnr.route.detail.joint_escape import enumerate_drops

        g = board([sot23_5("U1", (6.0, 3.0), ["A", "GND", "B", "", "C"])], size=(12.0, 8.0))

        def drops():
            grid = RouteGrid.from_graph(
                g, 12.0, 8.0, pitch=0.25, clearance=0.2, track_width=0.25, via_radius=0.3
            )
            rect = pad_rects(g.component("U1"))[1][2]
            point = (rect.cx, rect.cy)
            return grid, enumerate_drops(
                grid, "GND", point, 0, rect=rect, width=0.4, via_keepout=3, reach=8
            )

        grid, options = drops()
        self.assertTrue(options)
        rects = pad_rects(g.component("U1"))
        for o in options:
            ((x, y),) = o.vias
            for name, net, r in rects:
                gap = math.hypot(max(r.left - x, 0, x - r.right), max(r.bottom - y, 0, y - r.top))
                self.assertGreaterEqual(gap, 0.3 + 0.2 - 1e-9, (name, x, y))
            self.assertGreater(y, 3.0)  # under the body, not at the board edge
            # The cell-rounded halo of a foreign pad covers the site: only the exact
            # rectangle check admits it.
            i, j = grid.cell_of(x, y)
            owners = {grid.via_halo.get((0, i, j)), grid.pad_net.get((0, i, j))}
            self.assertTrue(owners - {None, "GND"}, (x, y))
        # A halo cell that is not a pad's own (fixed copper, say) still rejects.
        sites = {grid.cell_of(*o.vias[0]) for o in options}
        grid2, _ = drops()
        for i, j in sites:
            grid2.via_halo[(0, i, j)] = "X"
        rect = pad_rects(g.component("U1"))[1][2]
        again = enumerate_drops(
            grid2, "GND", (rect.cx, rect.cy), 0, rect=rect, width=0.4, via_keepout=3, reach=8
        )
        self.assertFalse({grid2.cell_of(*o.vias[0]) for o in again} & sites)
        # Own-net fixed copper of the neighbour pin's net under the body: where it
        # claims a pad's halo cell the cell is no longer a pad's alone, so no drop
        # via comes within clearance of that track.
        from pnr.route.detail.fixed import reserve_fixed_copper

        grid3, _ = drops()
        track = ["C", "F.Cu", [6.0, 3.0], [6.0, 4.0], 0.25]
        reserve_fixed_copper(grid3, dict(frame="engine-mm-y-up", tracks=[track]), own_net=True)
        popped = set(grid.pad_via_halo) - set(grid3.pad_via_halo)
        self.assertTrue(popped)
        for la, i, j in popped:
            x, y = grid3.center_of(i, j)
            near = math.hypot(x - 6.0, max(3.0 - y, 0, y - 4.0))
            self.assertLessEqual(near, 0.125 + 0.2 + 0.3 + grid3.pitch)
        third = enumerate_drops(
            grid3, "GND", (rect.cx, rect.cy), 0, rect=rect, width=0.4, via_keepout=3, reach=8
        )
        for o in third:
            ((x, y),) = o.vias
            gap = math.hypot(x - 6.0, max(3.0 - y, 0, y - 4.0))
            self.assertGreaterEqual(gap, 0.125 + 0.2 + 0.3 - 1e-9, (x, y))

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
