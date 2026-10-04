"""Declared fanouts in the detailed router (pnr.route.detail.fanout, route_board)."""

import math
import unittest

from pnr.constraints import compile_constraints, compile_routing_rules
from pnr.graph import BoardGraph, BoardOutline, Component, Net, Pad
from pnr.route.detail.router import _late_copper, route_board

PITCH = 0.65
FAB = {
    "track_width_mm": 0.1,
    "clearance_mm": 0.1,
    "via_diameter_mm": 0.4,
    "via_drill_mm": 0.2,
    "hole_clearance_mm": 0.1524,
    "edge_clearance_mm": 0.3,
    "min_through_drill_mm": 0.15,
    "via_annular_mm": 0.0762,
    "smd_pad_clearance_mm": 0.1,
    "hole_to_hole_mm": 0.28,
    "via_to_smd_pad_mm": 0.1,
    "min_via_diameter_mm": 0.31,
}
STACK = {
    "layers": [
        {"name": "F.Cu", "type": "signal", "zones": []},
        {"name": "In1.Cu", "type": "power", "zones": ["GND"]},
        {"name": "In2.Cu", "type": "signal", "zones": []},
        {"name": "B.Cu", "type": "signal", "zones": []},
    ]
}


def board(n=6):
    """An n x n 0.65 mm array at (10, 10): the outer two rings signals S_*, the rest
    GND; each signal has a target pad on the board's edges."""
    half = (n - 1) / 2
    pads, pins = [], {}
    for r in range(n):
        for c in range(n):
            name = "%s%d" % ("ABCDEFGH"[r], c + 1)
            ring = min(r, c, n - 1 - r, n - 1 - c)
            net = ("S_" + name) if ring < 2 and (r + c) % 2 == 0 else "GND"
            pads.append(
                Pad(
                    name,
                    net,
                    ((c - half) * PITCH, (half - r) * PITCH),
                    (0.32, 0.32),
                    land_corner=0.16,
                )
            )
            pins.setdefault(net, []).append(("U1", name))
    comps = [Component("U1", "bga", (10.0, 10.0), 0.0, "top", (5.0, 5.0), (5.0, 5.0), True, pads)]
    signals = sorted(n for n in pins if n.startswith("S_"))
    for k, net in enumerate(signals):
        side = k % 4
        t = 2.0 + 16.0 * (k // 4 + 0.5) / ((len(signals) + 3) // 4)
        at = [(t, 1.0), (t, 19.0), (1.0, t), (19.0, t)][side]
        ref = "T%d" % k
        comps.append(
            Component(
                ref,
                "t",
                at,
                0.0,
                "top",
                (0.6, 0.6),
                (0.6, 0.6),
                pads=[Pad("1", net, (0, 0), (0.3, 0.3), land_corner=0.15)],
            )
        )
        pins[net].append((ref, "1"))
    comps.append(
        Component(
            "TG",
            "t",
            (19.0, 1.0),
            0.0,
            "top",
            (0.6, 0.6),
            (0.6, 0.6),
            pads=[Pad("1", "GND", (0, 0), (0.3, 0.3), land_corner=0.15)],
        )
    )
    pins["GND"].append(("TG", "1"))
    nets = [Net(name, i + 1, p) for i, (name, p) in enumerate(sorted(pins.items()))]
    g = BoardGraph("fanout-route", comps, nets, BoardOutline(20, 20))
    g.stack = STACK
    return g


def setup(fanout=True, **extra):
    g = board()
    doc = {
        "schema": "v0",
        "board": {"outline": {"w": 20, "h": 20}, "layers": 4},
        "fab": FAB,
        "fixed": {"U1": {"at": [10, 10], "rot": 0, "side": "top"}},
        "net_class": {"gnd": {"nets": ["GND"], "plane_layer": "In1.Cu"}},
    }
    if fanout:
        doc["fanout"] = [
            {
                "ref": "U1",
                "via_classes": {
                    "ground": {
                        "diameter_mm": 0.35,
                        "drill_mm": 0.15,
                        "nets": ["GND"],
                        "sites": ["interstitial"],
                    },
                    "default": {
                        "diameter_mm": 0.4,
                        "drill_mm": 0.2,
                        "sites": ["vacant", "outside", "interstitial"],
                    },
                },
                **extra,
            }
        ]
    c = compile_constraints(doc, g.refs)
    return g, c, compile_routing_rules(c, [n.name for n in g.nets])


class FanoutRouteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.g, cls.c, cls.rules = setup()
        cls.route = route_board(cls.g, cls.c, cls.rules, pitch=0.25, max_iters=6)

    def test_every_signal_routes_through_its_fanout(self):
        r = self.route
        self.assertEqual(r.result.unrouted, [])
        report = r.escape_diagnostics["fanout"]["U1"]
        self.assertEqual(report["signals_escaped"], report["signals"])
        self.assertEqual(report["drops_placed"], report["drops"])
        self.assertEqual(report["no_access"], [])
        self.assertEqual(report["conflicts_on_board"], [])

    def test_drop_vias_keep_their_class_and_the_copper_is_locked(self):
        r = self.route
        sizes = {(n, x, y): (d, h) for n, x, y, d, h in r.via_sizes}
        drops = [v for v in r.vias if v[0] == "GND" and math.dist(v[1:], (10, 10)) < 3]
        self.assertTrue(drops)
        for v in drops:
            self.assertEqual(sizes.get(v), (0.35, 0.15))
        self.assertTrue(r.extras()["locked"]["tracks"])
        locked = {tuple(v) for v in r.locked["vias"]}
        self.assertTrue({tuple(v) for v in drops} <= locked)

    def test_fanned_out_pads_are_not_late_copper(self):
        from pnr.route.detail.fanout import plan_fanouts
        from pnr.route.detail.grid import RouteGrid

        grid = RouteGrid.from_graph(
            self.g,
            20,
            20,
            pitch=0.25,
            layers=("F.Cu", "In2.Cu", "B.Cu"),
            clearance=0.1,
            track_width=0.1,
            via_radius=0.2,
        )
        signals = {n.name for n in self.g.nets if n.name != "GND"}
        fo = plan_fanouts(
            grid, self.g, self.rules, plane_nets={"GND"}, signal_nets=signals, via_keepout=1
        )
        self.assertIsNone(_late_copper(self.g, {"GND"}, set(), fo.escapes + self._target_drop()))
        self.assertEqual(len(fo.skip_pads), 36)

    def _target_drop(self):
        from pnr.route.detail.escape import Escape
        from pnr.route.detail.grid import Cell

        return [Escape("GND", "joint", Cell(0, 0, 0), (19.0, 1.0))]

    def test_fanout_copper_is_where_the_plan_put_it(self):
        from pnr.fanout import cached_plan, classify

        layers, drops, signals = classify(self.g, self.rules)
        plan = cached_plan(
            self.g,
            self.rules,
            self.rules["fanouts"][0],
            grid_layers=layers,
            plane_nets=drops,
            signal_nets=signals,
        )
        tracks = {(t[0], t[1], tuple(map(tuple, t[2:4]))) for t in self.route.tracks}
        for net, layer, a, b, _w in plan["copper"]["tracks"]:
            self.assertIn((net, layer, (tuple(a), tuple(b))), tracks)

    def test_no_extras_without_a_fanout(self):
        g, c, rules = setup(fanout=False)
        self.assertNotIn("fanouts", rules)
        r = route_board(g, c, rules, pitch=0.25, max_iters=2)
        self.assertEqual(r.extras(), {})
        self.assertNotIn("fanout", r.escape_diagnostics)
        self.assertTrue(all(math.isfinite(v[1]) for v in r.vias))

    def grid(self):
        from pnr.route.detail.grid import RouteGrid

        return RouteGrid.from_graph(
            self.g,
            20,
            20,
            pitch=0.25,
            layers=("F.Cu", "In2.Cu", "B.Cu"),
            clearance=0.1,
            track_width=0.1,
            via_radius=0.2,
        )

    def test_a_ball_without_access_gives_its_copper_back(self):
        # An exit whose outward ray is blocked has no access cell: the ball is not
        # emitted, so its planned copper leaves the escape tables (and its cells the
        # fanout's claims) instead of costing the maze capacity it never uses.
        from pnr.fanout import cached_plan, classify
        from pnr.route.detail.fanout import plan_fanouts

        layers, drops, signals = classify(self.g, self.rules)
        plan = cached_plan(
            self.g,
            self.rules,
            self.rules["fanouts"][0],
            grid_layers=["F.Cu", "In2.Cu", "B.Cu"],
            plane_nets={"GND"},
            signal_nets={n.name for n in self.g.nets if n.name != "GND"},
        )
        name, row = next((k, r) for k, r in sorted(plan["terminals"].items()) if r.get("exit"))
        grid = self.grid()
        la = grid.layers.index(row["layer"])
        ex, ey = row["exit"]
        ox, oy = row["outward"]
        ci, cj = grid.cell_of(ex, ey)
        for dj in range(-10, 11):  # everything beyond the exit, within 2.5 mm
            for di in range(-10, 11):
                c = grid.center_of(ci + di, cj + dj)
                if (c[0] - ex) * ox + (c[1] - ey) * oy > 0 and grid.in_bounds(ci + di, cj + dj):
                    grid.blocked[la, cj + dj, ci + di] = True
        fo = plan_fanouts(
            grid,
            self.g,
            self.rules,
            plane_nets={"GND"},
            signal_nets={n.name for n in self.g.nets if n.name != "GND"},
            via_keepout=1,
        )
        self.assertIn(name, fo.report["U1"]["no_access"])
        self.assertIn(row["net"], fo.blocked_nets)
        mine = [s for s in grid.escape_segments if s[1] == row["net"]]
        self.assertEqual(mine, [])
        self.assertNotIn(row["net"], [v[0] for v in grid.escape_vias])
        self.assertFalse([k for k in fo.via_sizes if k[0] == row["net"]])
        others = [s for s in grid.escape_segments if s[1] != row["net"]]
        self.assertTrue(others)  # the other balls keep theirs

    def test_a_blocked_ray_turns_its_tail(self):
        # A part against the array's edge blocks the exit's straight outward ray: the
        # tail turns to the nearest free cell beside it instead of failing the ball.
        from pnr.fanout import cached_plan
        from pnr.route.detail.fanout import plan_fanouts

        plan = cached_plan(
            self.g,
            self.rules,
            self.rules["fanouts"][0],
            grid_layers=["F.Cu", "In2.Cu", "B.Cu"],
            plane_nets={"GND"},
            signal_nets={n.name for n in self.g.nets if n.name != "GND"},
        )
        exits = {k: r for k, r in sorted(plan["terminals"].items()) if r.get("exit")}
        name = "A3" if "A3" in exits else sorted(exits)[0]  # an edge ball, not a corner
        row = exits[name]
        grid = self.grid()
        la = grid.layers.index(row["layer"])
        ex, ey = row["exit"]
        ox, oy = row["outward"]
        # A part's pad on the ray, 0.3 to 2.7 mm beyond the exit: its halo closes the
        # ray's cells; its sides stay open.
        from pnr.place.geometry import Rect

        size = (0.6, 2.4) if abs(ox) < 0.5 else (2.4, 0.6)
        grid.add_pad(la, "X", Rect(ex + ox * 1.5, ey + oy * 1.5, *size))
        fo = plan_fanouts(
            grid,
            self.g,
            self.rules,
            plane_nets={"GND"},
            signal_nets={n.name for n in self.g.nets if n.name != "GND"},
            via_keepout=1,
        )
        self.assertNotIn(name, fo.report["U1"]["no_access"])
        (cell,) = fo.access[row["net"]]
        centre = grid.center_of(cell.i, cell.j)
        along = (centre[0] - ex) * ox + (centre[1] - ey) * oy
        across = abs((centre[0] - ex) * oy - (centre[1] - ey) * ox)
        self.assertGreater(across, 1e-6)  # off the ray
        self.assertGreater(along, 0.25 * math.hypot(along, across) - 1e-9)

    def test_the_tail_judges_a_class_keepout_exactly(self):
        # A class keepout 0.12 mm beside A5's exit: the grid bars the exit's own cell
        # (half a track plus half a cell diagonal), the planner judged the exit exactly.
        # The hand-over judges the tail exactly too, so the ball keeps its access.
        from pnr.fanout import cached_plan
        from pnr.route.detail.fanout import plan_fanouts
        from pnr.route.detail.router import _mark_keepout_v1

        signals = {n.name for n in self.g.nets if n.name != "GND"}
        plan = cached_plan(
            self.g,
            self.rules,
            self.rules["fanouts"][0],
            grid_layers=["F.Cu", "In2.Cu", "B.Cu"],
            plane_nets={"GND"},
            signal_nets=signals,
        )
        a5, b4 = plan["terminals"]["A5"], plan["terminals"]["B4"]
        ex, ey = a5["exit"]
        x1 = ex - 0.12
        keepout = dict(
            name="beside",
            polygon=[[x1 - 0.5, ey - 0.2], [x1, ey - 0.2], [x1, ey + 3.0], [x1 - 0.5, ey + 3.0]],
            layers=["F.Cu"],
            items=["tracks"],
            allow_nets=[b4["net"]],
            allowed_nets=[b4["net"]],
        )
        rules = dict(self.rules, copper_keepouts=[keepout])
        grid = self.grid()
        _mark_keepout_v1(grid, self.g, keepout)
        i, j = grid.cell_of(ex, ey)
        self.assertTrue(grid.net_blocked(a5["net"], 0, i, j))  # the exit's own cell
        fo = plan_fanouts(
            grid, self.g, rules, plane_nets={"GND"}, signal_nets=signals, via_keepout=1
        )
        self.assertNotIn("A5", fo.report["U1"]["no_access"])

    def test_the_tail_judges_a_block_rule_area_exactly(self):
        # A fixed block's F.Cu rule area 0.225 mm east of A5's exit (0.2 mm grid): the
        # grid blocks the cells within half a track plus half a cell diagonal of it, so
        # every tail's side sample falls in a blocked cell; the last pass judges the
        # area exactly and the grid's grown obstacles on the centre line.
        import math as m

        from pnr.fanout import cached_plan
        from pnr.route.detail.fanout import plan_fanouts

        signals = {n.name for n in self.g.nets if n.name != "GND"}
        plan = cached_plan(
            self.g,
            self.rules,
            self.rules["fanouts"][0],
            grid_layers=["F.Cu", "In2.Cu", "B.Cu"],
            plane_nets={"GND"},
            signal_nets=signals,
        )
        ex, ey = plan["terminals"]["A5"]["exit"]
        x0 = ex + 0.225
        area = [[x0, ey - 0.2], [x0 + 0.5, ey - 0.2], [x0 + 0.5, ey + 3], [x0, ey + 3]]
        fixed = dict(
            frame="engine-mm-y-up",
            blocks=[
                dict(polygons=[dict(kind="rule_area", layers=["F.Cu"], outline=area, tracks=True)])
            ],
        )
        from pnr.route.detail.grid import RouteGrid

        grid = RouteGrid.from_graph(
            self.g,
            20,
            20,
            pitch=0.2,
            layers=("F.Cu", "In2.Cu", "B.Cu"),
            clearance=0.1,
            track_width=0.1,
            via_radius=0.2,
        )
        grid.block_polygon(area, [], [0], 0.05 + grid.pitch / m.sqrt(2), block_vias=False)
        fo = plan_fanouts(
            grid,
            self.g,
            self.rules,
            plane_nets={"GND"},
            signal_nets=signals,
            via_keepout=1,
            fixed_copper=fixed,
        )
        self.assertNotIn("A5", fo.report["U1"]["no_access"])

    def test_class_clearances_reach_the_halo_and_the_pad_checks(self):
        from pnr.place.geometry import Rect
        from pnr.route.detail.fanout import _clear_of_pads, _via_halo

        grid = self.grid()
        segment = [(1, (5.0, 5.0), (8.0, 5.0), 0.1)]
        plain = _via_halo(grid, segment, [])
        grid.net_clearances = {"PWR": 0.15}
        wide = _via_halo(grid, segment, [])
        self.assertTrue(plain < wide)  # a PWR via keeps 0.15 mm from this copper
        pad = [(0, "PWR", Rect(6.0, 5.32, 0.3, 0.3))]  # 0.12 mm from the track's edge
        self.assertTrue(_clear_of_pads(pad, "S", 0, (5.0, 5.0), (7.0, 5.0), 0.1, 0.1))
        classes = {"PWR": 0.15}
        self.assertFalse(_clear_of_pads(pad, "S", 0, (5.0, 5.0), (7.0, 5.0), 0.1, 0.1, classes))


class DropNetRouteTest(unittest.TestCase):
    def test_a_drop_nets_ball_is_routed_from_its_via(self):
        # fanout drop_nets: the corner signal S_A1 drops a via beside its ball and
        # the maze routes the net on from the via on the far layer (B.Cu).
        g, c, rules = setup(drop_nets=["S_A1"])
        self.assertEqual(rules["fanouts"][0]["drop_nets"], ["S_A1"])
        r = route_board(g, c, rules, pitch=0.25, max_iters=6)
        self.assertNotIn("S_A1", r.result.unrouted)
        report = r.escape_diagnostics["fanout"]["U1"]
        self.assertEqual(report["no_access"], [])
        vias = [v for v in r.vias if v[0] == "S_A1"]
        self.assertTrue(vias)
        self.assertTrue(any(t[0] == "S_A1" and t[1] == "B.Cu" for t in r.tracks))


if __name__ == "__main__":
    unittest.main()
