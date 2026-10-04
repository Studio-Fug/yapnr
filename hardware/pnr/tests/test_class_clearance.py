"""Net class clearances in the maze router (``board.class_clearance: maze``).

Off (the default), the halo router sizes every reservation from the fab clearance:
two nets may run one grid pitch apart even when one of them has a larger class
clearance, which KiCad's DRC then reports. On, each net's track halo, via keep-out
and static-copper tables follow its own class clearance, the exact pairwise rule
checks the routed nets (rerouting any pair still too close), and an mm audit of the
emitted copper is reported.
"""

import unittest

from pnr.constraints import ConstraintError, compile_constraints, compile_routing_rules
from pnr.graph import BoardGraph, Component, Net, Pad
from pnr.route.detail.class_check import copper_audit, repair
from pnr.route.detail.grid import Cell, RouteGrid
from pnr.route.detail.maze import RouteResult, _footprint, _Route, _to_geometry
from pnr.route.detail.router import route_board

FAB = dict(
    track_width_mm=0.15,
    clearance_mm=0.1,
    via_diameter_mm=0.4,
    via_drill_mm=0.2,
    hole_clearance_mm=0.15,
)


def _part(ref, x, y, net, w=0.4, h=0.4):
    return Component(
        ref=ref,
        footprint="test:" + ref,
        pos=(x, y),
        rot=0.0,
        side="top",
        courtyard=(w + 0.2, h + 0.2),
        bbox=(w + 0.2, h + 0.2),
        pads=[Pad(name="1", net=net, offset=(0.0, 0.0), size=(w, h))],
    )


def channel_board():
    """A 12 x 8 mm board where two 4 mm walls of no-net copper leave two channels of
    two grid rows (0.25 mm pitch) each, at y 3-4 and 6-7. X (class XTAL, 0.15 mm)
    runs through the lower one; Y (no class) is nearer the lower one too, and the
    upper one is its detour."""
    parts = [
        _part("X1", 1.0, 3.5, "X"),
        _part("X2", 11.0, 3.5, "X"),
        _part("Y1", 1.5, 4.8, "Y"),
        _part("Y2", 10.5, 4.8, "Y"),
        _part("W1", 6.0, 1.65, "", 4.0, 2.7),  # y 0.3-3.0
        _part("W2", 6.0, 5.0, "", 4.0, 2.0),  # y 4.0-6.0
        _part("W3", 6.0, 7.35, "", 4.0, 0.7),  # y 7.0-7.7
    ]
    nets = [
        Net(name="X", code=1, pins=[("X1", "1"), ("X2", "1")]),
        Net(name="Y", code=2, pins=[("Y1", "1"), ("Y2", "1")]),
    ]
    return BoardGraph(name="channel", components=parts, nets=nets)


def _compiled(extra_board=None):
    board = {"outline": {"w": 12, "h": 8}}
    board.update(extra_board or {})
    doc = {"board": board, "net_class": {"XTAL": {"nets": ["X"], "clearance_mm": 0.15}}}
    graph = channel_board()
    compiled = compile_constraints(doc, graph.refs)
    rules = compile_routing_rules(compiled, [n.name for n in graph.nets])
    rules["fab"] = dict(FAB)
    return graph, compiled, rules


class SwitchTest(unittest.TestCase):
    def test_declared_switch_becomes_a_rules_key(self):
        _, _, rules = _compiled({"class_clearance": "maze"})
        self.assertEqual(rules["class_clearance"], "maze")
        _, _, plain = _compiled()
        self.assertNotIn("class_clearance", plain)

    def test_unknown_value_is_an_error(self):
        with self.assertRaises(ConstraintError):
            compile_constraints({"board": {"class_clearance": "always"}}, [])


class HaloTest(unittest.TestCase):
    def test_class_net_halos_and_keepouts(self):
        graph, compiled, rules = _compiled({"class_clearance": "maze"})
        board = route_board(graph, compiled, rules, pitch=0.25)
        grid = board.grid
        # XTAL 0.15: ceil((0.075 + 0.15 + 0.075) / 0.25) - 1 = 1 cell; Y keeps 0.
        self.assertEqual(grid.routing_track_halos["X"], 1)
        self.assertEqual(grid.routing_track_halos["Y"], 0)
        # Vias: max(1, ceil((0.4 + 0.15) / 0.25) - 1) = 2 for X; the fab's 1 for Y.
        self.assertEqual(grid.routing_via_keepouts, {"X": 2})
        self.assertEqual(grid.class_tables, frozenset({"X", "Y"}))
        self.assertIn("X", grid.wide_via_net)
        plain, compiled, rules = _compiled()
        off = route_board(plain, compiled, rules, pitch=0.25)
        self.assertEqual(off.grid.routing_track_halos["X"], 0)
        self.assertIsNone(off.grid.class_tables)
        self.assertFalse(getattr(off.grid, "routing_via_keepouts", None))
        self.assertEqual(off.grid.wide_via_net, {})

    def test_footprint_uses_the_nets_own_via_keepout(self):
        grid = RouteGrid(4, 4, 0.25)
        grid.routing_via_keepouts = {"X": 2}
        cells = [Cell(0, 8, 8), Cell(1, 8, 8)]
        self.assertEqual(len({(c.i, c.j) for c in _footprint(grid, cells, 1, net="Y")}), 9)
        self.assertEqual(len({(c.i, c.j) for c in _footprint(grid, cells, 1, net="X")}), 25)


class ChannelTest(unittest.TestCase):
    """The A/B: the same board with the switch off and on."""

    @classmethod
    def setUpClass(cls):
        graph, compiled, rules = _compiled()
        cls.off = route_board(graph, compiled, rules, pitch=0.25)
        graph, compiled, rules = _compiled({"class_clearance": "maze"})
        cls.on = route_board(graph, compiled, rules, pitch=0.25)

    def _audit(self, board):
        return copper_audit(board.grid, board.tracks, board.vias, board.via_sizes)

    def test_off_runs_the_two_nets_a_pitch_apart(self):
        self.assertEqual(self.off.result.unrouted, [])
        audit = self._audit(self.off)
        self.assertGreater(audit["count"], 0)
        self.assertTrue(all({i["net"], i["other"]} == {"X", "Y"} for i in audit["items"]))
        self.assertLess(min(i["gap_mm"] for i in audit["items"]), 0.15 - 1e-6)

    def test_on_keeps_the_class_clearance_and_routes_both(self):
        self.assertEqual(self.on.result.unrouted, [])
        self.assertEqual(self._audit(self.on)["count"], 0)
        report = self.on.escape_diagnostics["class_clearance"]
        self.assertEqual(report["audit"]["count"], 0)
        self.assertEqual(report["halos"]["via_keepouts"], {"X": 2})

    def test_on_detours_the_plain_net(self):
        ys = [y for net, _, a, b, _ in self.on.tracks if net == "Y" for y in (a[1], b[1])]
        self.assertGreater(max(ys), 6.0)  # through the upper channel


class RepairTest(unittest.TestCase):
    def _grid(self):
        grid = RouteGrid(6, 3, 0.25, clearance=0.1, track_width=0.15, via_radius=0.2)
        grid.net_clearances = {"X": 0.15}
        grid.via_spacing = 0.2 + 0.28
        grid.via_drill_radius = 0.1
        return grid

    def _line(self, net, j, i0=2, i1=21, ends=None):
        """A route along row ``j`` from column ``i0`` to ``i1`` (F.Cu), entered from
        row ``ends`` at both ends when given."""
        cells = [Cell(0, i, j) for i in range(i0, i1 + 1)]
        if ends is not None:
            step = 1 if j > ends else -1
            first = [Cell(0, i0, k) for k in range(ends, j, step)]
            last = [Cell(0, i1, k) for k in range(j - step, ends - step, -step)]
            cells = first + cells + last
        route = _Route(cells, list(zip(cells, cells[1:])))
        rn = _to_geometry(route)
        rn.name, rn.routed = net, True
        return rn

    def test_a_pair_one_pitch_apart_is_rerouted_clear(self):
        grid = self._grid()
        # X (0.15 mm) leaves its terminals on row 2 to run along row 5, a pitch from Y,
        # which comes down from row 9 to run along row 6.
        nets = {"X": self._line("X", 5, ends=2), "Y": self._line("Y", 6, ends=9)}
        access = {n: [rn.cells[0], rn.cells[-1]] for n, rn in nets.items()}
        result = RouteResult(nets=dict(nets), unrouted=[], iterations=1)
        report = repair(grid, access, result, via_cost=12.0)
        self.assertEqual(report["ripped"], ["X"])  # equal size: the name decides
        self.assertEqual(report["pair_count"], 1)
        self.assertEqual(len(report["ripped"]), 1)
        self.assertEqual(result.unrouted, [])
        tracks = []
        for net, rn in result.nets.items():
            for la, p, q in rn.segments:
                tracks.append((net, "F.Cu", grid.center_of(*p), grid.center_of(*q), 0.15))
        grid.layers = ("F.Cu", "B.Cu")
        self.assertEqual(copper_audit(grid, tracks, [], ())["count"], 0)
        self.assertLessEqual(max(c.j for c in result.nets["X"].cells), 4)

    def test_clear_pair_is_left_alone(self):
        grid = self._grid()
        nets = {"X": self._line("X", 3), "Y": self._line("Y", 6)}
        access = {n: [rn.cells[0], rn.cells[-1]] for n, rn in nets.items()}
        result = RouteResult(nets=dict(nets), unrouted=[], iterations=1)
        before = {n: list(rn.cells) for n, rn in result.nets.items()}
        report = repair(grid, access, result, via_cost=12.0)
        self.assertEqual(report["pairs"], [])
        self.assertEqual({n: rn.cells for n, rn in result.nets.items()}, before)


class AuditTest(unittest.TestCase):
    def test_track_to_via_shortfall_at_the_larger_class(self):
        # The radar trial-2 case: a 0.25 mm PWR track 0.118 mm from a signal via.
        grid = RouteGrid(4, 4, 0.25, clearance=0.1, track_width=0.15, via_radius=0.2)
        grid.net_clearances = {"PWR": 0.15}
        d = 0.125 + 0.2 + 0.118
        tracks = [("PWR", "F.Cu", (1.0, 2.0), (3.0, 2.0), 0.25)]
        vias = [("SIG", 2.0, 2.0 + d)]
        audit = copper_audit(grid, tracks, vias, ())
        self.assertEqual(audit["count"], 1)
        (item,) = audit["items"]
        self.assertAlmostEqual(item["gap_mm"], 0.118, places=3)
        self.assertAlmostEqual(item["need_mm"], 0.15)
        self.assertEqual(item["kind"], "track-via")
        # A via of a fanout's smaller size (via_sizes, 0.3 mm) is judged at its own
        # radius: 0.168 mm, clear.
        small = copper_audit(grid, tracks, vias, [["SIG", 2.0, 2.0 + d, 0.3, 0.15]])
        self.assertEqual(small["count"], 0)
        self.assertEqual(copper_audit(grid, tracks, [("SIG", 2.0, 2.6)], ())["count"], 0)

    def test_pad_own_clearance(self):
        from pnr.place.geometry import Rect

        grid = RouteGrid(4, 4, 0.25, clearance=0.1, track_width=0.15, via_radius=0.2)
        grid.add_pad(0, "", Rect(2.0, 2.0, 1.0, 1.0), 0.6)
        tracks = [("A", "F.Cu", (0.5, 2.8), (3.5, 2.8), 0.15)]
        audit = copper_audit(grid, tracks, [], ())
        self.assertEqual(audit["count"], 1)
        self.assertEqual(audit["items"][0]["kind"], "track-pad")
        self.assertAlmostEqual(audit["items"][0]["need_mm"], 0.6)


class KernelParityTest(unittest.TestCase):
    """The dense fields read the class tables and per-net via keep-outs as the grid's
    predicates and the reference search do."""

    def test_fields_match_the_predicates(self):
        from pnr.route.detail.dense_maze import GridStatic, unmodelled

        graph, compiled, rules = _compiled({"class_clearance": "maze"})
        grid = route_board(graph, compiled, rules, pitch=0.25).grid
        self.assertIsNone(unmodelled(grid))
        static = GridStatic(grid)
        for net in ("X", "Y"):
            passable, via, _plated = static.net(net)
            for la in range(grid.nlayers):
                for j in range(grid.ny):
                    for i in range(grid.nx):
                        self.assertEqual(bool(passable[la, j, i]), grid.passable(la, i, j, net))
                        self.assertEqual(bool(via[la, j, i]), grid.via_passable(la, i, j, net))

    def test_reference_and_packed_kernels_route_alike(self):
        import os
        from unittest import mock

        boards = {}
        for kernel in ("reference", "packed"):
            graph, compiled, rules = _compiled({"class_clearance": "maze"})
            with mock.patch.dict(os.environ, {"PNR_MAZE_KERNEL": kernel}):
                boards[kernel] = route_board(graph, compiled, rules, pitch=0.25)
        self.assertEqual(boards["reference"].tracks, boards["packed"].tracks)
        self.assertEqual(boards["reference"].vias, boards["packed"].vias)


class EmptyClassTest(unittest.TestCase):
    def test_switch_without_class_above_the_fab_routes_the_same(self):
        graph = channel_board()
        doc = {"board": {"outline": {"w": 12, "h": 8}}}
        a = compile_constraints(doc, graph.refs)
        rules_a = dict(compile_routing_rules(a, ["X", "Y"]), fab=dict(FAB))
        doc["board"]["class_clearance"] = "maze"
        b = compile_constraints(doc, graph.refs)
        rules_b = dict(compile_routing_rules(b, ["X", "Y"]), fab=dict(FAB))
        off = route_board(graph, a, rules_a, pitch=0.25)
        on = route_board(channel_board(), b, rules_b, pitch=0.25)
        self.assertEqual(off.tracks, on.tracks)
        self.assertEqual(off.vias, on.vias)


if __name__ == "__main__":
    unittest.main()
