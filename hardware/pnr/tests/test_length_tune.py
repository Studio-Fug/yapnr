"""Length tuning after detailed routing (pnr.route.detail.tune) and its rules."""

import math
import os
import random
import unittest
from unittest import mock

from pnr import length_model as lm
from pnr.constraints import compile_constraints
from pnr.graph import BoardGraph, Component, Net, Pad
from pnr.route.detail.grid import Cell
from pnr.route.detail.maze import RoutedNet
from pnr.route.detail.router import route_board
from pnr.route.detail.tune import (
    SQRT2,
    Bump,
    _corner,
    _mitre,
    _piece_cells,
    _seg_dist,
    bump_path,
    match_sets,
    path_cells_mm,
    residual_target,
    shape_rules,
    straight_runs,
    tune_board,
)

# An error inside tuning fails the test instead of leaving the route untuned (this
# module only: a test run that imports several modules keeps its own environment).
_STRICT = mock.patch.dict(os.environ, {"PNR_TUNE_STRICT": "1"})


def setUpModule():
    _STRICT.start()


def tearDownModule():
    _STRICT.stop()


FAB = {
    "track_width_mm": 0.25,
    "clearance_mm": 0.2,
    "via_diameter_mm": 0.6,
    "via_drill_mm": 0.3,
    "hole_clearance_mm": 0.25,
}


def pair_board(n_offset=1.5, extra=()):
    """Two connectors joined by a pair whose N leg is the longer, plus ``extra`` nets."""

    def pad(name, net, off):
        return Pad(name, net, off, (0.6, 0.6), land_corner=0.0)

    j1 = [pad("1", "D_P", (0, -0.5)), pad("2", "D_N", (0, 0.5))]
    j2 = [pad("1", "D_P", (0, -0.5)), pad("2", "D_N", (0, n_offset))]
    nets = [
        Net("D_P", 1, [("J1", "1"), ("J2", "1")]),
        Net("D_N", 2, [("J1", "2"), ("J2", "2")]),
    ]
    for k, (net, a, b) in enumerate(extra):
        j1.append(pad(str(10 + k), net, a))
        j2.append(pad(str(10 + k), net, b))
        nets.append(Net(net, 3 + k, [("J1", str(10 + k)), ("J2", str(10 + k))]))
    comps = [
        Component("J1", "conn", (2.0, 6.0), 0, "top", (1.5, 3.0), (1.5, 3.0), pads=j1),
        Component("J2", "conn", (16.0, 6.0), 0, "top", (1.5, 5.0), (1.5, 5.0), pads=j2),
    ]
    return BoardGraph("pair", comps, nets)


def pair_rules(**pair):
    spec = dict(name="d", p="D_P", n="D_N", width_mm=None, gap_mm=None, skew_mm=0.5)
    spec.update(pair)
    return {
        "layers": 2,
        "fab": dict(FAB),
        "net_classes": [],
        "diff_pairs": [spec],
        "length_match": [],
    }


def route(graph, rules):
    c = compile_constraints({"board": {"outline": {"w": 20, "h": 12}}}, graph.refs)
    return route_board(graph, c, rules, pitch=0.25, max_iters=4)


class RulesTest(unittest.TestCase):
    def test_shape_defaults_from_the_fab(self):
        # Legs three widths apart (edge to edge) by preference, the clearance at least.
        shape = shape_rules({}, 0.25, 0.25, 0.2)
        self.assertEqual(
            (shape.gap, shape.gap_min, shape.amp_min, shape.amp_max, shape.amp_cap),
            (4, 2, 1, 4, 16),
        )
        self.assertIsNone(shape.max_added_mm)
        shape = shape_rules(
            {"tuning": {"amplitude_max_mm": 0.5, "gap_mm": 0.6, "max_added_mm": 3}},
            0.25,
            0.25,
            0.2,
        )
        self.assertEqual((shape.gap, shape.gap_min, shape.amp_max, shape.amp_cap), (4, 4, 2, 2))
        self.assertEqual(shape.max_added_mm, 3.0)

    def test_sets_and_units(self):
        rules = pair_rules(skew_ps=3.0)
        rules["length_match"] = [{"name": "bus", "nets": ["A", "B", "A"], "tolerance_mm": 0.5}]
        sets = match_sets(rules)
        self.assertEqual(
            [(s.name, s.unit, s.budget, s.nets) for s in sets],
            [
                ("d", "ps", 3.0, ("D_P", "D_N")),
                ("bus", "mm", 0.5, ("A", "B")),
            ],
        )
        self.assertEqual(residual_target(1.0, 0.25), 0.5)
        self.assertAlmostEqual(residual_target(0.4, 0.25), 0.15)


class TemplateTest(unittest.TestCase):
    line = [Cell(0, i, 5) for i in range(12)]

    def test_bump_adds_two_amplitudes(self):
        for amp in (1, 3):
            path = bump_path(self.line, [Bump(0, 3, 1, amp, 2)])
            self.assertAlmostEqual(
                path_cells_mm(path, 0.25) - path_cells_mm(self.line, 0.25),
                2 * amp * 0.25,
            )
            self.assertEqual(len(set(path)), len(path))  # no self-intersection

    def test_serpentine_and_accordion(self):
        same = bump_path(self.line, [Bump(0, 1, 1, 2, 2), Bump(0, 5, 1, 2, 2)])
        alt = bump_path(self.line, [Bump(0, 1, 1, 2, 2), Bump(0, 3, -1, 2, 2)])
        for path in (same, alt):
            self.assertAlmostEqual(path_cells_mm(path, 1.0) - path_cells_mm(self.line, 1.0), 8.0)
            self.assertEqual(len(set(path)), len(path))
            for a, b in zip(path, path[1:]):
                self.assertEqual(abs(a.i - b.i) + abs(a.j - b.j), 1)

    def test_mitre_takes_two_minus_root_two_cells(self):
        path = bump_path(self.line, [Bump(0, 3, 1, 2, 2)])
        corners = [k for k in range(len(path)) if _corner(path, k) is not None]
        self.assertEqual(len(corners), 4)
        k = corners[1]
        mitred = path[:k] + path[k + 1 :]
        self.assertAlmostEqual(path_cells_mm(path, 1.0) - path_cells_mm(mitred, 1.0), 2 - SQRT2)

    def test_diagonal_run_bump_and_mitres(self):
        diagonal = [Cell(0, i, i) for i in range(10)]
        for amp in (1, 2):
            path = bump_path(diagonal, [Bump(0, 3, 1, amp, 2)])
            self.assertAlmostEqual(
                path_cells_mm(path, 1.0) - path_cells_mm(diagonal, 1.0), 2 * amp * SQRT2
            )
            self.assertEqual(len(set(path)), len(path))
        path = bump_path(diagonal, [Bump(0, 3, 1, 2, 2)])
        cuts = [(k, _mitre(path, k)) for k in range(len(path))]
        cuts = [(k, c) for k, c in cuts if c is not None]
        self.assertEqual(len(cuts), 4)  # the bump's four 90-degree corners
        k, (replacement, cell, saved) = cuts[1]
        self.assertAlmostEqual(saved, 2 * SQRT2 - 2)
        trial = path[:k] + replacement + path[k + 1 :]
        self.assertAlmostEqual(path_cells_mm(path, 1.0) - path_cells_mm(trial, 1.0), saved)
        for a, b in zip(trial, trial[1:]):
            self.assertLessEqual(max(abs(a.i - b.i), abs(a.j - b.j)), 1)
        # An orthogonal corner loses its vertex.
        line = bump_path(self.line, [Bump(0, 3, 1, 2, 2)])
        k = next(k for k in range(len(line)) if _mitre(line, k) is not None)
        self.assertEqual(_mitre(line, k)[0], [])
        self.assertAlmostEqual(_mitre(line, k)[2], 2 - SQRT2)

    def test_straight_pieces_around_a_vertex(self):
        path = bump_path(self.line, [Bump(0, 3, 1, 2, 2)])  # legs of 2, top of 2
        k = path.index(Cell(0, 3, 7))  # the first leg's top corner
        self.assertEqual(sorted(_piece_cells(path, k)), [2.0, 2.0])
        trial = path[:k] + path[k + 1 :]  # mitred: 1-cell leg, diagonal, 1-cell top
        self.assertEqual(
            sorted(_piece_cells(trial, k - 1) + _piece_cells(trial, k)),
            [1.0, 1.0, SQRT2, SQRT2],
        )
        shape = shape_rules({"tuning": {"min_segment_mm": 0.5}}, 0.25, 0.25, 0.2)
        self.assertEqual((shape.amp_min, shape.min_seg_mm), (2, 0.5))

    def test_straight_runs_stop_at_breaking_cells(self):
        rn = RoutedNet("N")
        rn.segments = [(0, (i, 0), (i + 1, 0)) for i in range(6)] + [(0, (6, 0), (6, 1))]
        runs = straight_runs(rn, {Cell(0, 3, 0)})
        self.assertEqual([[c.i for c in r] for r in runs], [[0, 1, 2, 3], [3, 4, 5, 6]])
        rn.segments = [(0, (i, i), (i + 1, i + 1)) for i in range(4)]
        self.assertEqual([[c.i for c in r] for r in straight_runs(rn, set())], [[0, 1, 2, 3, 4]])
        self.assertEqual(straight_runs(rn, set(), diagonal=False), [])


class TuneMemberTest(unittest.TestCase):
    def test_a_run_beside_another_footprint_still_takes_meanders(self):
        # A 45-degree run passing a via of another net at the exact spacing: the
        # via's keep-out ring (radius 3 at this fab and pitch) covers side cells of
        # the run's own steps. Only a meander's new steps are judged, so the run
        # still takes bumps away from the via.
        from pnr.route.detail.grid import RouteGrid
        from pnr.route.detail.maze import RouteResult
        from pnr.route.detail.router import BoardRoute
        from pnr.route.detail.tune import Tuner

        grid = RouteGrid(10, 10, 0.25, layers=("F.Cu", "B.Cu"), clearance=0.2, track_width=0.25)
        a = RoutedNet("A")
        a.cells = [Cell(0, 5 + k, 30 - k) for k in range(21)]
        a.segments = [(0, (p.i, p.j), (q.i, q.j)) for p, q in zip(a.cells, a.cells[1:])]
        a.routed = True
        b = RoutedNet("B")
        b.cells = [Cell(0, 18, 21), Cell(1, 18, 21)]
        b.vias = [(18, 21)]
        b.routed = True
        board = BoardRoute(
            result=RouteResult(nets={"A": a, "B": b}, unrouted=[], iterations=0),
            grid=grid,
        )
        board.tracks = [
            ("A", "F.Cu", grid.center_of(*p), grid.center_of(*q), 0.25) for _l, p, q in a.segments
        ]
        board.vias = [("B",) + tuple(grid.center_of(18, 21))]
        g = BoardGraph("t", [], [Net("A", 1, []), Net("B", 2, [])])
        rules = pair_rules()
        rules["diff_pairs"] = []
        rules["length_match"] = [{"name": "ab", "nets": ["A", "B"], "tolerance_mm": 0.5}]
        tuner = Tuner(
            board,
            g,
            grid,
            rules,
            net_width={},
            default_width=0.25,
            net_halo={},
            via_keepout=3,
            access={"A": [a.cells[0], a.cells[-1]]},
            via_radius=0.3,
        )
        tuner.prepare()
        owner = tuner.owner
        # Side cells of the run's own steps lie in B's footprint (its via ring).
        corners = {
            Cell(0, q.i, p.j)
            for _l, p, q in [(0, Cell(0, *s[1]), Cell(0, *s[2])) for s in a.segments]
        }
        self.assertTrue(any("B" in owner.get(c, ()) for c in corners))
        shape = shape_rules(rules, grid.pitch, 0.25, 0.2)
        added, bumps, _mitres, _gap = tuner.tune_member("A", 2.0, "mm", shape)
        self.assertGreaterEqual(bumps, 1)
        self.assertGreater(added, 1.0)
        # Every new cell keeps out of B's footprint; the via keeps its clearance.
        b_fp = {c for c, n in owner.items() if "B" in n}
        old = {Cell(0, 5 + k, 30 - k) for k in range(21)}
        self.assertFalse((set(a.cells) - old) & b_fp)
        via = grid.center_of(18, 21)
        for t in board.tracks:
            self.assertGreaterEqual(_seg_dist(t[2], t[3], via, via) + 1e-6, 0.125 + 0.3 + 0.2)


class SpreadSetTest(unittest.TestCase):
    def test_members_move_apart_where_the_board_has_room(self):
        # Three nets of a group run side by side at the closest spacing the router
        # allows (3 cells with a 1-cell halo); C, the longest, turns up at its end.
        # The middle one has no room for meanders until its neighbours move out.
        from pnr.route.detail.grid import RouteGrid
        from pnr.route.detail.maze import RouteResult
        from pnr.route.detail.router import BoardRoute
        from pnr.route.detail.tune import Tuner, net_footprint

        grid = RouteGrid(10, 7.5, 0.25, layers=("F.Cu", "B.Cu"), clearance=0.2, track_width=0.25)
        names = ["A", "B", "C"]
        nets = {}
        for n, j in (("A", 12), ("B", 15), ("C", 18)):
            rn = RoutedNet(n)
            rn.cells = [Cell(0, i, j) for i in range(2, 38)]
            if n == "C":
                rn.cells += [Cell(0, 37, j + k) for k in range(1, 7)]
            rn.segments = [(0, (p.i, p.j), (q.i, q.j)) for p, q in zip(rn.cells, rn.cells[1:])]
            rn.routed = True
            nets[n] = rn
        halo = {n: 1 for n in names}
        grid.routing_track_halos = halo
        grid.routing_via_keepout = 1
        board = BoardRoute(result=RouteResult(nets=nets, unrouted=[], iterations=0), grid=grid)
        board.tracks = [
            (n, "F.Cu", grid.center_of(*p), grid.center_of(*q), 0.25)
            for n, rn in nets.items()
            for _l, p, q in rn.segments
        ]
        g = BoardGraph("t", [], [Net(n, k + 1, []) for k, n in enumerate(names)])
        rules = pair_rules()
        rules["diff_pairs"] = []
        rules["length_match"] = [{"name": "bus", "nets": names, "tolerance_mm": 0.5}]
        tuner = Tuner(
            board,
            g,
            grid,
            rules,
            net_width={},
            default_width=0.25,
            net_halo=halo,
            via_keepout=1,
            access={n: [nets[n].cells[0], nets[n].cells[-1]] for n in names},
            via_radius=0.3,
        )
        longest = tuner.measure("C").total_mm
        tuner.prepare()
        owner = tuner.owner
        (s,) = match_sets(rules)
        moved = tuner.spread_set(s)
        self.assertIn("A", moved)
        self.assertNotIn("B", moved)
        # B now has at least 5 cells to each neighbour along its middle.
        for i in range(12, 28):
            rows = {n: [c.j for c in nets[n].cells if c.i == i] for n in names}
            self.assertGreaterEqual(15 - max(rows["A"]), 5)
            self.assertGreaterEqual(min(rows["C"]) - 15, 5)
        # Still connected end to end, no member longer than the longest was, the
        # footprints apart and the owner map the routes' own.
        for n in names:
            self.assertEqual(nets[n].cells[0].i, 2)
            self.assertLessEqual(tuner.measure(n).total_mm, longest + 1e-6)
        fps = {n: net_footprint(grid, n, nets[n], 1, 1) for n in names}
        self.assertFalse(fps["A"] & fps["B"] or fps["B"] & fps["C"] or fps["A"] & fps["C"])
        self.assertEqual({c: v for c, v in owner.items() if v}, tuner._owner_map())
        for a in (t for t in board.tracks if t[0] == "A"):
            for b in (t for t in board.tracks if t[0] == "B"):
                self.assertGreaterEqual(_seg_dist(a[2], a[3], b[2], b[3]) + 1e-6, 0.45)


class TuneBoardTest(unittest.TestCase):
    def test_no_sets_no_change(self):
        g = pair_board()
        rules = pair_rules()
        rules["diff_pairs"] = []
        board = route(g, rules)
        self.assertIsNone(board.length_report)
        before = list(board.tracks)
        self.assertIsNone(tune_board(board, g, board.grid, rules))
        self.assertEqual(board.tracks, before)

    def test_pair_is_matched_within_its_budget(self):
        g = pair_board(n_offset=2.5)
        board = route(g, pair_rules(skew_mm=0.5))
        (report,) = board.length_report
        self.assertIn(report["status"], ("ok", "tuned"))
        self.assertLessEqual(report["spread"], report["target_residual"])
        lengths = lm.board_route_lengths(
            board, g, ["D_P", "D_N"], lm.default_stackup(2), via_radius=0.3
        )
        self.assertAlmostEqual(
            abs(lengths["D_P"].total_mm - lengths["D_N"].total_mm),
            report["spread"],
            places=6,
        )
        tuned = [m for m in report["members"] if m["bumps"]]
        self.assertEqual([m["net"] for m in tuned], ["D_P"])
        # The bumps add exactly their own track: none starts inside a via's or a pad's
        # copper, where KiCad would measure it straight.
        plain_rules = pair_rules(skew_mm=0.5)
        plain_rules["diff_pairs"] = []
        plain = route(g, plain_rules)
        before = lm.board_route_lengths(plain, g, ["D_P"], lm.default_stackup(2), via_radius=0.3)[
            "D_P"
        ].total_mm
        self.assertAlmostEqual(lengths["D_P"].total_mm - before, tuned[0]["added_mm"], delta=1e-5)

    def test_tuning_leaves_other_nets_alone_and_keeps_clearance(self):
        extra = [("S1", (0.4, -1.3), (0.4, -3.0)), ("S2", (0.4, 1.3), (0.4, 3.0))]
        g = pair_board(n_offset=2.0, extra=extra)
        rules = pair_rules(skew_mm=0.4)
        rules["diff_pairs"] = []
        plain = route(g, rules)
        tuned = route(g, pair_rules(skew_mm=0.4))
        others = lambda b: sorted(t for t in b.tracks if t[0] in ("S1", "S2"))  # noqa: E731
        self.assertEqual(others(plain), others(tuned))
        # Exact clearance of every tuned segment to the other nets' copper.
        pads = lm.graph_pads(g, conservative=True)
        rng = random.Random(7)
        mine = [t for t in tuned.tracks if t[0] in ("D_P", "D_N")]
        for net, layer, a, b, w in rng.sample(mine, min(60, len(mine))):
            for o in tuned.tracks:
                if o[0] != net and o[1] == layer:
                    self.assertGreaterEqual(
                        _seg_dist(a, b, o[2], o[3]) + 1e-6,
                        (w + o[4]) / 2 + FAB["clearance_mm"],
                    )
            for pad in pads:
                if pad.net != net and layer in pad.layers:
                    from pnr.route.detail.tune import _seg_poly_dist

                    self.assertGreaterEqual(
                        _seg_poly_dist(a, b, pad.outline) + 1e-6,
                        w / 2 + FAB["clearance_mm"],
                    )
        # Every grid cell a tuned net uses is passable for it.
        for net in ("D_P", "D_N"):
            for c in tuned.result.nets[net].cells:
                self.assertTrue(tuned.grid.passable(c.layer, c.i, c.j, net))

    def test_group_is_matched_within_its_tolerance(self):
        # An 8-net bus fanning out from a 0.5 mm pitch row to a 2 mm pitch row: the
        # outer nets run about 6 mm longer than the middle ones.
        def pad(name, net, off):
            return Pad(name, net, off, (0.3, 0.6), land_corner=0.0)

        nets = ["B%d" % k for k in range(8)]
        j1 = [pad(str(k + 1), n, ((k - 3.5) * 0.5, 0.0)) for k, n in enumerate(nets)]
        u1 = [pad(str(k + 1), n, ((k - 3.5) * 2.0, 0.0)) for k, n in enumerate(nets)]
        comps = [
            Component("J1", "conn", (12.0, 3.0), 0, "top", (5.0, 1.5), (5.0, 1.5), pads=j1),
            Component("U1", "dev", (12.0, 13.0), 0, "top", (16.0, 1.5), (16.0, 1.5), pads=u1),
        ]
        g = BoardGraph(
            "bus",
            comps,
            [Net(n, k + 1, [("J1", str(k + 1)), ("U1", str(k + 1))]) for k, n in enumerate(nets)],
        )
        rules = pair_rules()
        rules["diff_pairs"] = []
        rules["length_match"] = [{"name": "bus", "nets": nets, "tolerance_mm": 0.5}]
        c = compile_constraints({"board": {"outline": {"w": 24, "h": 16}}}, g.refs)
        board = route_board(g, c, rules, pitch=0.25, max_iters=4)
        self.assertTrue(board.fully_routed)
        (report,) = board.length_report
        self.assertEqual(report["status"], "tuned")
        nominal = report.get("nominal_spread", report["spread"])
        self.assertLessEqual(nominal, report["target_residual"])
        self.assertLessEqual(report["spread"], report["budget"])  # any merge order
        lengths = lm.board_route_lengths(board, g, nets, lm.default_stackup(2), via_radius=0.3)
        spread = max(x.total_mm for x in lengths.values()) - min(
            x.total_mm for x in lengths.values()
        )
        self.assertAlmostEqual(spread, nominal, places=6)
        self.assertGreaterEqual(sum(1 for m in report["members"] if m["bumps"]), 4)

    def test_a_tuning_failure_keeps_the_route(self):
        g = pair_board(n_offset=2.5)
        plain_rules = pair_rules()
        plain_rules["diff_pairs"] = []
        plain = route(g, plain_rules)
        with mock.patch(
            "pnr.route.detail.tune.Tuner.tune_member", side_effect=RuntimeError("boom")
        ), mock.patch.dict(os.environ, {"PNR_TUNE_STRICT": "0"}):
            board = route(g, pair_rules(skew_mm=0.5))
        self.assertEqual(sorted(board.tracks), sorted(plain.tracks))
        (report,) = board.length_report
        self.assertEqual(report["status"], "tuning_error")
        self.assertIn("boom", report["error"])

    def test_unmatched_set_reroutes_its_longest_member(self):
        from collections import defaultdict

        from pnr.route.detail import tune
        from pnr.route.detail.maze import _route_one, _to_geometry

        g = pair_board(n_offset=0.5)
        rules = pair_rules(skew_mm=0.5)
        # One short trombone at most: meanders alone cannot make up a detour.
        rules["tuning"] = {"style": "trombone", "amplitude_max_mm": 0.25}
        got = {}

        def capture(board, graph, grid, rules, **kwargs):
            got.update(board=board, kwargs=kwargs)

        with mock.patch.object(tune, "tune_board", side_effect=capture):
            route(g, rules)
        board, kwargs = got["board"], got["kwargs"]
        grid = board.grid
        # Send D_N over a wall across the board (as a congested negotiation might).
        rn = board.result.nets["D_N"]
        wall = {Cell(la, 36, j) for la in range(grid.nlayers) for j in range(0, 41)}
        p_cells = set(board.result.nets["D_P"].cells)
        access = sorted(kwargs["access"]["D_N"], key=lambda c: (c.layer, c.i, c.j))
        detour = _to_geometry(
            _route_one(
                grid,
                access,
                "D_N",
                defaultdict(int),
                defaultdict(float),
                12,
                0.0,
                wall | p_cells,
            )
        )
        old = {frozenset((grid.center_of(*a), grid.center_of(*b))) for _l, a, b in rn.segments}
        board.tracks = [
            t
            for t in board.tracks
            if not (t[0] == "D_N" and frozenset((tuple(t[2]), tuple(t[3]))) in old)
        ]
        board.tracks += [
            ("D_N", grid.layers[la], grid.center_of(*a), grid.center_of(*b), 0.25)
            for la, a, b in detour.segments
        ]
        board.vias = [v for v in board.vias if v[0] != "D_N"]
        board.vias += [("D_N",) + tuple(grid.center_of(i, j)) for i, j in detour.vias]
        rn.cells, rn.segments, rn.vias = detour.cells, detour.segments, detour.vias

        def lengths():
            return lm.board_route_lengths(
                board, g, ["D_P", "D_N"], lm.default_stackup(2), via_radius=0.3
            )

        before = lengths()
        self.assertGreater(before["D_N"].total_mm - before["D_P"].total_mm, 3.0)
        (report,) = tune.tune_board(board, g, grid, rules, **kwargs)
        self.assertEqual(report["rerouted"], ["D_N"])
        self.assertIn(report["status"], ("ok", "tuned"))
        after = lengths()
        self.assertLess(after["D_N"].total_mm, before["D_N"].total_mm - 1.0)
        self.assertLessEqual(
            abs(after["D_N"].total_mm - after["D_P"].total_mm),
            report["target_residual"] + 1e-9,
        )
        # The new route is the net's own grid route and copper; it clears D_P.
        self.assertTrue(all(grid.passable(c.layer, c.i, c.j, "D_N") for c in rn.cells))
        for t in (t for t in board.tracks if t[0] == "D_N"):
            for o in board.tracks:
                if o[0] == "D_P" and o[1] == t[1]:
                    self.assertGreaterEqual(
                        _seg_dist(t[2], t[3], o[2], o[3]) + 1e-6,
                        0.25 + FAB["clearance_mm"],
                    )

    def test_a_boxed_in_bus_member_gets_room(self):
        # Six nets from a 1 mm pitch column to a 1.27 mm pitch row above and to the
        # right: the bus turns a corner, the inner nets are the short ones and run
        # between their neighbours at the pins' pitch.
        from pnr.route.detail import tune

        def pad(name, net, off, size):
            return Pad(name, net, off, size, land_corner=0.0)

        nets = ["B%d" % k for k in range(6)]
        j1 = [pad(str(k + 1), n, (0.0, (2.5 - k) * 1.0), (0.8, 0.4)) for k, n in enumerate(nets)]
        u1 = [pad(str(k + 1), n, ((k - 2.5) * 1.27, 0.0), (0.4, 0.8)) for k, n in enumerate(nets)]
        comps = [
            Component("J1", "conn", (1.5, 6.5), 0, "top", (1.6, 7.0), (1.6, 7.0), pads=j1),
            Component("U1", "dev", (12.0, 10.0), 0, "top", (8.62, 1.6), (8.62, 1.6), pads=u1),
        ]
        g = BoardGraph(
            "corner",
            comps,
            [Net(n, k + 1, [("J1", str(k + 1)), ("U1", str(k + 1))]) for k, n in enumerate(nets)],
        )
        rules = pair_rules()
        rules["diff_pairs"] = []
        rules["length_match"] = [{"name": "bus", "nets": nets, "tolerance_mm": 0.5}]
        c = compile_constraints({"board": {"outline": {"w": 16, "h": 13}}}, g.refs)
        with mock.patch.object(tune, "SPREAD_PASSES", 0):
            plain = route_board(g, c, rules, pitch=0.25, max_iters=4)
        board = route_board(g, c, rules, pitch=0.25, max_iters=4)
        self.assertTrue(board.fully_routed)
        (report,) = board.length_report
        self.assertEqual(report["status"], "tuned")
        self.assertLessEqual(
            report.get("nominal_spread", report["spread"]), report["target_residual"]
        )
        if plain.length_report[0]["status"] == "length_unmatched":
            self.assertTrue(report.get("spaced"))
        lengths = lm.board_route_lengths(board, g, nets, lm.default_stackup(2), via_radius=0.3)
        self.assertAlmostEqual(
            max(x.total_mm for x in lengths.values()) - min(x.total_mm for x in lengths.values()),
            report.get("nominal_spread", report["spread"]),
            places=6,
        )
        for net in nets:
            for cell in board.result.nets[net].cells:
                self.assertTrue(board.grid.passable(cell.layer, cell.i, cell.j, net))
        mine = [t for t in board.tracks if t[0] in nets]
        for a in mine:
            for b in mine:
                if a[0] < b[0] and a[1] == b[1]:
                    self.assertGreaterEqual(
                        _seg_dist(a[2], a[3], b[2], b[3]) + 1e-6,
                        0.25 + FAB["clearance_mm"],
                    )

    def test_ps_budget(self):
        g = pair_board(n_offset=2.5)
        board = route(g, pair_rules(skew_ps=2.0))
        (report,) = board.length_report
        self.assertEqual(report["unit"], "ps")
        self.assertLessEqual(report["spread"], report["target_residual"])
        delays = [m["delay_ps"] for m in report["members"]]
        self.assertTrue(all(d and d > 0 for d in delays))
        self.assertTrue(math.isclose(max(delays) - min(delays), report["spread"], abs_tol=1e-3))


def hand_board(routes, layers=("F.Cu", "B.Cu"), size=10.0, halo=1, vias=()):
    """A routed board from hand-made grid routes ``{net: [cells]}`` (consecutive
    cells joined), with ``vias`` ``[(net, i, j)]`` (the net's cells must hold both
    layers of the column). Returns (board, graph)."""
    from pnr.route.detail.grid import RouteGrid
    from pnr.route.detail.maze import RouteResult
    from pnr.route.detail.router import BoardRoute

    grid = RouteGrid(size, size, 0.25, layers=layers, clearance=0.2, track_width=0.25)
    grid.routing_track_halos = {n: halo for n in routes}
    grid.routing_via_keepout = 1
    nets = {}
    for n, cells in routes.items():
        rn = RoutedNet(n)
        rn.cells = list(dict.fromkeys(cells))
        rn.segments = [
            (p.layer, (p.i, p.j), (q.i, q.j))
            for p, q in zip(cells, cells[1:])
            if p.layer == q.layer
        ]
        rn.vias = [(i, j) for net, i, j in vias if net == n]
        rn.routed = True
        nets[n] = rn
    board = BoardRoute(result=RouteResult(nets=nets, unrouted=[], iterations=0), grid=grid)
    board.tracks = [
        (n, layers[la], grid.center_of(*p), grid.center_of(*q), 0.25)
        for n, rn in nets.items()
        for la, p, q in rn.segments
    ]
    board.vias = [(net, *grid.center_of(i, j)) for net, i, j in vias]
    g = BoardGraph("t", [], [Net(n, k + 1, []) for k, n in enumerate(routes)])
    return board, g


def hand_tuner(board, g, rules, **kw):
    from pnr.route.detail.tune import Tuner

    nets = board.result.nets
    return Tuner(
        board,
        g,
        board.grid,
        rules,
        net_width={},
        default_width=0.25,
        net_halo={n: 1 for n in nets},
        via_keepout=1,
        access={n: [rn.cells[0], rn.cells[-1]] for n, rn in nets.items()},
        via_radius=0.3,
        **kw,
    )


def row(j, i0, i1, layer=0):
    return [Cell(layer, i, j) for i in range(i0, i1 + 1)]


class SequentialRecoveryTest(unittest.TestCase):
    def fixture(self, enabled=True, cap=30):
        board, g = hand_board(
            {
                "A": row(20, 8, 24),
                "B": row(8, 8, 36),
                "C": row(17, 6, 32),
                "D": row(23, 6, 32),
            }
        )
        rules = pair_rules()
        rules["diff_pairs"] = [
            dict(name="ab", p="A", n="B", skew_mm=0.5),
            dict(name="cd", p="C", n="D", skew_mm=0.5),
        ]
        rules["tuning"] = dict(sequential=enabled, sequential_max_added_mm=cap)
        return board, g, rules

    def test_moves_blocking_pair_then_reconciles_both_sets(self):
        plain, g, rules = self.fixture(False)
        with mock.patch.dict(os.environ, {"PNR_TUNE_SEQUENTIAL": "0"}):
            base = hand_tuner(plain, g, rules).run()
        self.assertEqual(base[0].status, "length_unmatched")
        self.assertEqual(base[1].status, "ok")
        board, g, rules = self.fixture()
        reports = hand_tuner(board, g, rules).run()
        self.assertTrue(all(r.spread <= r.budget for r in reports))
        info = reports[0].sequential
        self.assertEqual(info["stop_reason"], "converged")
        self.assertTrue(info["accepted_moves"])
        self.assertLessEqual(info["added_mm"], 30)
        for a in board.tracks:
            for b in board.tracks:
                if a[0] < b[0] and a[1] == b[1]:
                    self.assertGreaterEqual(_seg_dist(a[2], a[3], b[2], b[3]) + 1e-6, 0.45)
        self.assertEqual(board.vias, [])

    def test_small_total_cap_rolls_back_displacement_and_keeps_prior_match(self):
        board, g, rules = self.fixture(cap=0.2)
        before = sorted(board.tracks)
        reports = hand_tuner(board, g, rules).run()
        self.assertEqual(reports[0].status, "length_unmatched")
        self.assertLessEqual(reports[1].spread, reports[1].budget)
        self.assertEqual(sorted(board.tracks), before)
        self.assertLessEqual(reports[0].sequential["added_mm"], 0.2)
        self.assertEqual(reports[0].sequential["accepted_moves"], [])

    def test_recovery_is_opt_in(self):
        board, g, rules = self.fixture(False)
        with mock.patch.dict(os.environ, {"PNR_TUNE_SEQUENTIAL": "0"}):
            reports = hand_tuner(board, g, rules).run()
        self.assertNotIn("sequential", reports[0].to_json())


class OverlappingSetsTest(unittest.TestCase):
    def report(self, x_end):
        g = pair_board(n_offset=1.5, extra=[("X", (0, -1.4), (0, -x_end))])
        rules = pair_rules()
        rules["length_match"] = [{"name": "grp", "nets": ["D_P", "D_N", "X"], "tolerance_mm": 0.5}]
        board = route(g, rules)
        return {r["name"]: r for r in board.length_report}

    def test_a_group_lengthens_both_legs_of_a_pair(self):
        # X as long as D_N, then X the longest: the group brings both legs up to it
        # and the pair stays within its own budget.
        for x_end in (4.4, 5.2):
            with self.subTest(x_end=x_end):
                got = self.report(x_end)
                for name in ("d", "grp"):
                    self.assertEqual(got[name]["status"], "tuned")
                    self.assertLessEqual(got[name]["spread"], got[name]["budget"])
                legs = {m["net"]: m["length_mm"] for m in got["grp"]["members"]}
                pair = {m["net"]: m["length_mm"] for m in got["d"]["members"]}
                self.assertEqual(legs["D_P"], pair["D_P"])  # one board, one measure
                self.assertAlmostEqual(got["d"]["spread"], abs(pair["D_P"] - pair["D_N"]), places=6)

    def test_a_set_that_would_break_an_earlier_one_is_undone(self):
        # Pair ab: A and B are 7 mm, matched. Group {A, X}: X is 9 mm, so the group
        # lengthens A; B, boxed in by C and D, cannot follow, so the pair would end
        # 2 mm apart: the group's tuning is undone and the group reported unmatched.
        routes = {
            "A": row(30, 2, 30),
            "B": row(12, 2, 30),
            "C": row(9, 2, 30),
            "D": row(15, 2, 30),
            "X": row(22, 2, 38),
        }
        board, g = hand_board(routes)
        rules = pair_rules()
        rules["diff_pairs"] = [dict(name="ab", p="A", n="B", skew_mm=0.5)]
        rules["length_match"] = [{"name": "grp", "nets": ["A", "X"], "tolerance_mm": 0.5}]
        before = sorted(board.tracks)
        reports = {r.name: r for r in hand_tuner(board, g, rules).run()}
        self.assertEqual(reports["ab"].status, "ok")
        self.assertEqual(reports["grp"].status, "length_unmatched")
        self.assertTrue(reports["grp"].reverted)
        self.assertEqual(reports["grp"].conflicts, ["ab"])
        self.assertEqual(sorted(board.tracks), before)

    def test_statuses_follow_the_final_board(self):
        # Whatever the order, every set's status says what its spread says.
        for x_end in (4.4, 5.2):
            for r in self.report(x_end).values():
                over = r["spread"] > r["budget"] + 1e-9
                self.assertEqual(r["status"] == "length_unmatched", over)


class PairTest(unittest.TestCase):
    def test_a_pairs_legs_are_never_routed_apart(self):
        from pnr.route.detail import tune

        g = pair_board(n_offset=2.5)
        rules = pair_rules(skew_mm=0.5)
        rules["tuning"] = {"style": "trombone", "amplitude_max_mm": 0.25}
        with mock.patch.object(tune.Tuner, "spread_set") as spread:
            board = route(g, rules)
        spread.assert_not_called()
        (report,) = board.length_report
        self.assertNotIn("spaced", report)
        # Nor by default for a group that holds a pair's legs.
        routes = {"A": row(10, 2, 30), "B": row(20, 2, 30), "C": row(30, 2, 34)}
        hb, hg = hand_board(routes)
        rules = pair_rules()
        rules["diff_pairs"] = [dict(name="ab", p="A", n="B", skew_mm=0.5)]
        rules["length_match"] = [{"name": "grp", "nets": ["B", "C"], "tolerance_mm": 0.5}]
        tuner = hand_tuner(hb, hg, rules)
        tuner.prepare()
        self.assertFalse(tuner.may_reroute("B"))  # two sets
        self.assertTrue(tuner.may_reroute("A"))  # its pair only: rerouting, not spreading
        self.assertEqual(tuner.spread_set(match_sets(rules)[1]), [])

    def test_coupled_length(self):
        from pnr.route.detail.tune import coupled_length

        p = [("F.Cu", (0.0, 0.0), (10.0, 0.0))]
        n = [("F.Cu", (0.0, 0.45), (5.0, 0.45)), ("B.Cu", (5.0, 0.45), (10.0, 0.45))]
        coupled, total = coupled_length(p, n, 0.5)
        self.assertAlmostEqual(total, 10.0)
        # Beside the F.Cu half, up to where its end is 0.5 mm away (x = 5.218).
        self.assertAlmostEqual(coupled, 5.218, delta=0.06)
        self.assertEqual(coupled_length(p, n, 0.4)[0], 0.0)

    def test_the_report_gives_layers_gap_and_coupling(self):
        g = pair_board(n_offset=2.5)
        (report,) = route(g, pair_rules(skew_mm=0.5)).length_report
        self.assertIn("coupled_share", report)
        self.assertGreaterEqual(report["coupled_share"], 0.0)
        tuned = [m for m in report["members"] if m["bumps"]]
        self.assertTrue(tuned)
        for m in report["members"]:
            self.assertTrue(set(m["layers"]) <= {"F.Cu", "B.Cu"} and m["layers"])
        # Room beside the leg: the meander legs stand three widths apart.
        self.assertAlmostEqual(tuned[0]["gap_mm"], 0.75)


class UnmatchedTest(unittest.TestCase):
    def test_an_unmatched_set_goes_back_to_the_route_as_routed(self):
        g = pair_board(n_offset=4.0)
        rules = pair_rules(skew_mm=0.5)
        # One 0.5 mm bump of the 1.37 mm the pair needs: not half of the excess.
        rules["tuning"] = {"max_added_mm": 0.5}
        board = route(g, rules)
        (report,) = board.length_report
        self.assertEqual(report["status"], "length_unmatched")
        self.assertTrue(report["reverted"])
        self.assertTrue(all(m["bumps"] == 0 for m in report["members"]))
        plain_rules = pair_rules(skew_mm=0.5)
        plain_rules["tuning"] = {"meanders": False}
        plain = route(g, plain_rules)
        self.assertIsNone(plain.length_report)
        self.assertEqual(sorted(board.tracks), sorted(plain.tracks))

    def test_meanders_stop_at_the_added_length_cap(self):
        g = pair_board(n_offset=4.0)
        free = pair_rules(skew_mm=0.5)
        (report,) = route(g, free).length_report
        self.assertGreater(max(m["added_mm"] for m in report["members"]), 1.2)
        rules = pair_rules(skew_mm=0.5)
        rules["tuning"] = {"max_added_mm": 1.0}
        (report,) = route(g, rules).length_report
        self.assertAlmostEqual(max(m["added_mm"] for m in report["members"]), 1.0)
        self.assertEqual(report["status"], "tuned")  # 0.37 mm apart, inside 0.5


class FixedCopperTest(unittest.TestCase):
    def test_fixed_copper_counts_in_the_length_and_the_clearance(self):
        routes = {"A": row(10, 2, 30), "B": row(20, 2, 30)}
        board, g = hand_board(routes)
        rules = pair_rules()
        rules["diff_pairs"] = []
        rules["length_match"] = [{"name": "ab", "nets": ["A", "B"], "tolerance_mm": 0.5}]
        a_end = board.grid.center_of(30, 10)
        fixed = {
            "frame": "engine-mm-y-up",
            "tracks": [["A", "F.Cu", list(a_end), [a_end[0] + 2.0, a_end[1]], 0.25]],
            "vias": [
                dict(
                    net="Z",
                    xy=[5.0, 7.0],
                    diameter_mm=0.6,
                    drill_mm=0.3,
                    type="through",
                )
            ],
        }
        tuner = hand_tuner(board, g, rules, fixed_copper=fixed)
        self.assertAlmostEqual(tuner.measure("A").total_mm, 7.0 + 2.0, places=6)
        tuner.prepare()
        self.assertFalse(
            tuner.index.clear("B", "F.Cu", (5.0, 6.6), (5.5, 6.6), 0.125, lambda n: 0.2)
        )
        self.assertFalse(tuner.may_reroute("A"))  # its fixed copper stays joined
        reports = tuner.run()
        # B is lengthened to A's whole length, fixed part included.
        self.assertEqual(reports[0].status, "tuned")
        self.assertLessEqual(reports[0].spread, reports[0].budget)
        self.assertTrue(next(m for m in reports[0].members if m.net == "A").fixed)

    def test_a_set_leaving_a_block_is_partial(self):
        g = pair_board(n_offset=2.5)
        rules = pair_rules(skew_mm=0.5)
        rules["block_ports"] = ["D_P"]
        plain_rules = dict(rules, tuning={"meanders": False})
        board = route(g, rules)
        (report,) = board.length_report
        self.assertEqual(report["status"], "partial")
        self.assertEqual(sorted(board.tracks), sorted(route(g, plain_rules).tracks))

    def test_a_layer_change_through_a_plated_pad_is_not_routed_again(self):
        cells = row(10, 2, 15) + row(10, 15, 30, layer=1)
        board, g = hand_board({"A": cells, "B": row(20, 2, 30)})
        rules = pair_rules()
        rules["diff_pairs"] = []
        rules["length_match"] = [{"name": "ab", "nets": ["A", "B"], "tolerance_mm": 0.5}]
        tuner = hand_tuner(board, g, rules)
        tuner.prepare()
        self.assertTrue(tuner.may_reroute("A"))
        centre = board.grid.center_of(15, 10)
        board.grid.plated_ports = [("A", centre, 1.0)]
        self.assertFalse(tuner.may_reroute("A"))
        self.assertFalse(tuner.reroute_shorter("A", "mm"))


class ViaModelTest(unittest.TestCase):
    def test_no_member_is_routed_again_under_a_via_model(self):
        # A's route crosses W (F.Cu) by a detour on B.Cu. Through vias only, the
        # tuner routes it again, shorter, across W on In2.Cu. With blind and buried
        # vias allowed that route would change layers through F.Cu-In2.Cu vias the
        # board's via spans (recorded before tuning) do not hold, so A stays.
        from pnr.stack import copper_names
        from pnr.via_policy import BLIND, BURIED, THROUGH, GridVias, resolve

        layers = ("F.Cu", "In2.Cu", "B.Cu")

        def col(i, j0, j1, layer):
            step = 1 if j1 >= j0 else -1
            return [Cell(layer, i, j) for j in range(j0, j1 + step, step)]

        for model in (False, True):
            with self.subTest(via_model=model):
                a = row(10, 2, 12) + col(12, 10, 30, 2) + row(30, 12, 20, 2)
                a += col(20, 30, 10, 2) + row(10, 20, 30)
                routes = {"A": a, "W": col(16, 0, 39, 0), "C": row(36, 2, 30, 2)}
                board, g = hand_board(routes, layers=layers, vias=[("A", 12, 10), ("A", 20, 10)])
                if model:
                    policy = resolve(
                        dict(allowed=[THROUGH, BLIND, BURIED]),
                        copper_names(6),
                        gaps=[0.09, 0.55, 0.2, 0.55, 0.09],
                        bonds=["prepreg", "core", "prepreg", "core", "prepreg"],
                    )
                    board.grid.via_model = GridVias(policy, layers, 0.25, 0.2, (0.6, 0.3), 1)
                rules = pair_rules()
                rules.update(layers=6, stackup=lm.default_stackup(6), diff_pairs=[])
                rules["length_match"] = [{"name": "ac", "nets": ["A", "C"], "tolerance_mm": 0.5}]
                tuner = hand_tuner(board, g, rules)
                tuner.prepare()
                before = (list(board.tracks), list(board.vias))
                self.assertEqual(tuner.may_reroute("A"), not model)
                self.assertEqual(tuner.reroute_shorter("A", "mm"), not model)
                if model:
                    self.assertEqual((board.tracks, board.vias), before)
                    self.assertEqual(tuner.spread_set(match_sets(rules)[0]), [])
                else:
                    self.assertIn(1, {c.layer for c in board.result.nets["A"].cells})


class StackupTest(unittest.TestCase):
    def test_a_via_counts_its_span_on_four_layers(self):
        layers = ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")
        a = row(10, 2, 20) + row(10, 20, 30, layer=3)
        board, g = hand_board({"A": a, "B": row(22, 2, 30)}, layers=layers, vias=[("A", 20, 10)])
        st = lm.default_stackup(4)
        rules = pair_rules()
        rules.update(layers=4, stackup=st, diff_pairs=[])
        rules["length_match"] = [{"name": "ab", "nets": ["A", "B"], "tolerance_mm": 0.2}]
        tuner = hand_tuner(board, g, rules)
        span = lm.layer_distance(st, "F.Cu", "B.Cu")
        self.assertAlmostEqual(tuner.measure("A").via_mm, span)
        (report,) = tuner.run()
        self.assertEqual(report.status, "tuned")
        self.assertLessEqual(report.nominal_spread, report.target_residual)
        b = next(m for m in report.members if m.net == "B")
        self.assertGreater(b.added_mm, span - report.target_residual)
        # A stackup whose copper layers are not the board's is refused.
        with self.assertRaises(ValueError):
            hand_tuner(board, g, dict(rules, stackup=lm.default_stackup(2)))


if __name__ == "__main__":
    unittest.main()
