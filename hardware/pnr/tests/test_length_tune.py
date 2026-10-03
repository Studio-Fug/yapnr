"""Length tuning after detailed routing (pnr.route.detail.tune) and its rules."""

import math
import random
import unittest

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
    nets = [Net("D_P", 1, [("J1", "1"), ("J2", "1")]), Net("D_N", 2, [("J1", "2"), ("J2", "2")])]
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
        shape = shape_rules({}, 0.25, 0.25, 0.2)
        self.assertEqual((shape.gap, shape.amp_min, shape.amp_max, shape.amp_cap), (2, 1, 4, 16))
        shape = shape_rules({"tuning": {"amplitude_max_mm": 0.5, "gap_mm": 0.6}}, 0.25, 0.25, 0.2)
        self.assertEqual((shape.gap, shape.amp_max, shape.amp_cap), (4, 2, 2))

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
                path_cells_mm(path, 0.25) - path_cells_mm(self.line, 0.25), 2 * amp * 0.25
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
            sorted(_piece_cells(trial, k - 1) + _piece_cells(trial, k)), [1.0, 1.0, SQRT2, SQRT2]
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
            abs(lengths["D_P"].total_mm - lengths["D_N"].total_mm), report["spread"], places=6
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
                        _seg_dist(a, b, o[2], o[3]) + 1e-6, (w + o[4]) / 2 + FAB["clearance_mm"]
                    )
            for pad in pads:
                if pad.net != net and layer in pad.layers:
                    from pnr.route.detail.tune import _seg_poly_dist

                    self.assertGreaterEqual(
                        _seg_poly_dist(a, b, pad.outline) + 1e-6, w / 2 + FAB["clearance_mm"]
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
        from unittest import mock

        g = pair_board(n_offset=2.5)
        plain_rules = pair_rules()
        plain_rules["diff_pairs"] = []
        plain = route(g, plain_rules)
        with mock.patch(
            "pnr.route.detail.tune.Tuner.tune_member", side_effect=RuntimeError("boom")
        ):
            board = route(g, pair_rules(skew_mm=0.5))
        self.assertEqual(sorted(board.tracks), sorted(plain.tracks))
        (report,) = board.length_report
        self.assertEqual(report["status"], "tuning_error")
        self.assertIn("boom", report["error"])

    def test_ps_budget(self):
        g = pair_board(n_offset=2.5)
        board = route(g, pair_rules(skew_ps=2.0))
        (report,) = board.length_report
        self.assertEqual(report["unit"], "ps")
        self.assertLessEqual(report["spread"], report["target_residual"])
        delays = [m["delay_ps"] for m in report["members"]]
        self.assertTrue(all(d and d > 0 for d in delays))
        self.assertTrue(math.isclose(max(delays) - min(delays), report["spread"], abs_tol=1e-3))


if __name__ == "__main__":
    unittest.main()
