"""Initial exploration must vary the whole layout and preserve hard source rules."""

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

from pnr.constraints import compile_constraints
from pnr.graph import BoardGraph, BoardOutline, Component, Net, Pad
from pnr.place.initial_pool import (
    EXTRA_FINALISTS,
    POOL_RELOCATION_FRACTION_THRESHOLD,
    InitialPoolConfig,
    _route_metrics,
    diverse_shortlist,
    initial_starts,
    pose_distance,
    preserve_source_locks,
    route_rank,
    select_initial_placement,
    unfinished,
    wholesale_relocation,
)
from pnr.place.model import global_place
from pnr.place.placer import PlacementReport
from pnr.route.detail.maze import RoutedNet, RouteResult


def fixture():
    components = [
        Component(
            ref,
            "test",
            (x, y),
            rot,
            "top",
            (1, 1),
            (1, 1),
            locked=locked,
            pads=[Pad("1", "N", (0, 0), (0.3, 0.3))],
            smd_body=True,
        )
        for ref, x, y, rot, locked in [
            ("FIXED", 2, 2, 0, False),
            ("LOCKED", 15, 12, 90, True),
            ("A", 7, 5, 0, False),
            ("B", 10, 8, 0, False),
        ]
    ]
    graph = BoardGraph(
        "pool", components, [Net("N", 1, [(c.ref, "1") for c in components])], BoardOutline(20, 16)
    )
    constraints = compile_constraints(
        {"board": {"outline": {"w": 20, "h": 16}}, "fixed": {"FIXED": {"at": [2, 2], "rot": 0}}},
        graph.refs,
    )
    return graph, constraints


class InitialStartsTest(unittest.TestCase):
    def test_explicit_starts_cover_board_and_are_reproducible(self):
        graph, constraints = fixture()
        before = graph.to_json()
        starts = initial_starts(graph, constraints, InitialPoolConfig(), seed=118)
        self.assertEqual(starts, initial_starts(graph, constraints, InitialPoolConfig(), seed=118))
        self.assertEqual(len(starts), 8)
        self.assertEqual(starts[0]["kind"], "legacy-global")
        self.assertEqual(starts[1]["positions"]["A"], [7, 5])
        self.assertEqual(graph.to_json(), before)
        points = [s["positions"]["A"] for s in starts[2:]]
        self.assertGreater(max(p[0] for p in points) - min(p[0] for p in points), 8)
        self.assertGreater(max(p[1] for p in points) - min(p[1] for p in points), 6)
        self.assertTrue(
            all(
                "LOCKED" not in s["positions"] and "FIXED" not in s["positions"] for s in starts[1:]
            )
        )
        self.assertGreater(len({s["rotations"]["A"] for s in starts[2:]}), 1)

    def test_global_optimizer_uses_explicit_centres_and_rotation_logits(self):
        graph, constraints = fixture()
        constraints = preserve_source_locks(graph, constraints)
        positions, rotations = global_place(
            graph,
            constraints,
            20,
            16,
            iters=0,
            seed=0,
            initial_positions={"A": (16, 3), "B": (4, 12), "FIXED": (10, 10)},
            initial_rotations={"A": 180, "B": 90},
        )
        self.assertEqual(positions["A"], (16, 3))
        self.assertEqual(positions["B"], (4, 12))
        self.assertEqual(positions["FIXED"], (2, 2))
        self.assertEqual(positions["LOCKED"], (15, 12))
        self.assertEqual(rotations["A"], 180)
        self.assertEqual(rotations["B"], 90)
        self.assertEqual(rotations["LOCKED"], 90)

    def test_locks_are_added_locally_without_overriding_authored_fixed_pose(self):
        graph, constraints = fixture()
        graph.component("FIXED").locked = True
        new = preserve_source_locks(graph, constraints)
        self.assertNotIn("LOCKED", constraints.locked_refs)
        self.assertIn("LOCKED", new.locked_refs)
        self.assertEqual(len([c for c in new.constraints if "FIXED" in c.refs]), 1)

    def test_hard_bottom_side_releases_xy_and_preserves_mirrored_pad_geometry(self):
        from pnr.constraints import ConstraintError
        from pnr.place.metrics import hard_violations
        from pnr.place.placer import place

        graph, _ = fixture()
        graph.component("A").pads[0].offset = (0.2, 0.4)
        constraints = compile_constraints(
            {"board": {"outline": {"w": 20, "h": 16}}, "side": {"bottom": ["@board.pogo"]}},
            graph.refs,
            {"board.pogo": "A"},
        )
        self.assertNotIn("A", constraints.locked_refs)
        self.assertIn("A", hard_violations(graph, constraints)["side_misplaced"])
        placed, report = place(
            graph,
            constraints,
            iters=0,
            initial_positions={"FIXED": (2, 2), "LOCKED": (15, 12), "A": (16, 3), "B": (4, 12)},
            initial_rotations={"A": 0},
            orient=False,
        )
        self.assertTrue(report.legal)
        self.assertEqual(placed.component("A").side, "bottom")
        self.assertEqual(placed.component("A").pads[0].offset, (0.2, -0.4))
        self.assertEqual(placed.component("A").pads[0].net, "N")
        self.assertGreater(abs(placed.component("A").pos[0] - graph.component("A").pos[0]), 5)
        self.assertFalse(any(hard_violations(placed, constraints).values()))
        with self.assertRaises(ConstraintError):
            compile_constraints({"side": {"bottom": ["A"], "top": ["A"]}}, graph.refs)
        with self.assertRaises(ConstraintError):
            compile_constraints(
                {"fixed": {"A": {"at": [1, 1], "side": "top"}}, "side": {"bottom": ["A"]}},
                graph.refs,
            )

    def test_shortlist_preserves_reference_and_distant_orientation_basin(self):
        graph, constraints = fixture()
        candidates = []
        for index, (xy, cost, rot) in enumerate(
            [((7, 5), 100, 0), ((7.1, 5), 1, 0), ((7.2, 5), 2, 0), ((17, 3), 5, 90)]
        ):
            g = copy.deepcopy(graph)
            g.component("A").pos = xy
            g.component("A").rot = rot
            candidates.append(dict(id=str(index), graph=g, cost=cost))
        selected = diverse_shortlist(candidates, 3, "cost", mandatory=["0"], refs=["A", "B"])
        self.assertEqual([c["id"] for c in selected], ["0", "1", "3"])
        self.assertGreater(pose_distance(selected[0]["graph"], selected[2]["graph"], ["A"]), 0.4)

    def test_shortlist_does_not_dilute_single_part_opposite_side_basin(self):
        graph, _ = fixture()
        candidates = []
        for name, cost, x, basin in [
            ("baseline", 100, 7, False),
            ("best", 1, 7.1, False),
            ("global", 2, 18, False),
            ("under-body", 101, 8, True),
        ]:
            g = copy.deepcopy(graph)
            g.component("A").pos = (x, 5)
            record = (
                {"basin_anchors": [{"ref": "A", "host": "FIXED", "side": "bottom"}]}
                if basin
                else {}
            )
            candidates.append(dict(id=name, graph=g, cost=cost, record=record))
        selected = diverse_shortlist(candidates, 3, "cost", mandatory=["baseline"], refs=["A", "B"])
        self.assertEqual([c["id"] for c in selected], ["baseline", "best", "under-body"])
        # No implicit expansion of the configured trial budget.
        self.assertEqual(
            [c["id"] for c in diverse_shortlist(candidates, 2, "cost", mandatory=["baseline"])],
            ["baseline", "best"],
        )

    def test_shortlist_covers_each_basin_once_before_remaining_diversity(self):
        graph, _ = fixture()
        candidates = []
        for name, cost, host in [
            ("baseline", 100, None),
            ("best", 1, None),
            ("basin-low", 2, "H"),
            ("basin-high", 3, "H"),
            ("basin-other", 4, "J"),
        ]:
            record = (
                {"basin_anchors": [{"ref": "A", "host": host, "side": "bottom"}]} if host else {}
            )
            candidates.append(dict(id=name, graph=copy.deepcopy(graph), cost=cost, record=record))
        selected = diverse_shortlist(candidates, 4, "cost", mandatory=["baseline"])
        self.assertEqual(
            [c["id"] for c in selected], ["baseline", "best", "basin-low", "basin-other"]
        )
        self.assertEqual(len({c["id"] for c in selected}), 4)

    def test_opposite_body_basin_reserves_a_topological_alternative(self):
        host = Component(
            "RADIO", "module", (10, 10), 0, "top", (10, 12), (10, 12), pads=[], smd_body=True
        )
        pogo = Component(
            "PAD_ARRAY",
            "test",
            (3, 3),
            0,
            "bottom",
            (3, 4),
            (3, 4),
            pads=[Pad(str(i), "N", (i * 0.1, 0), (0.2, 0.2)) for i in range(8)],
            smd_body=True,
        )
        graph = BoardGraph("basin", [host, pogo], [], BoardOutline(24, 22))
        constraints = compile_constraints(
            {
                "board": {"outline": {"w": 24, "h": 22}},
                "fixed": {"RADIO": {"at": [10, 10], "side": "top"}},
                "side": {"bottom": ["PAD_ARRAY"]},
            },
            graph.refs,
        )
        starts = initial_starts(graph, constraints, InitialPoolConfig(), seed=9)
        self.assertEqual(starts[2]["kind"], "opposite-body-global")
        self.assertEqual(starts[2]["basin_anchors"][0]["at"], [10, 10])
        self.assertNotIn("PAD_ARRAY", constraints.locked_refs)
        # A non-SMD through-hole body owns both surfaces and cannot host it.
        host.smd_body = False
        host.pads = [Pad("1", "N", (0, 0), (1, 1), through_hole=True)]
        starts = initial_starts(graph, constraints, InitialPoolConfig(), seed=9)
        self.assertFalse(any("basin_anchors" in start for start in starts))

    def test_under_body_basin_survives_unrelated_global_legalization_failure(self):
        from pnr.place.legalize import LegalizationError

        host = Component(
            "H", "module", (10, 10), 0, "top", (10, 12), (10, 12), pads=[], smd_body=True
        )
        pogo = Component(
            "P",
            "test",
            (3, 3),
            0,
            "bottom",
            (3, 4),
            (3, 4),
            pads=[Pad(str(i), "N", (i * 0.1, 0), (0.2, 0.2)) for i in range(8)],
            smd_body=True,
        )
        graph = BoardGraph("basin", [host, pogo], [], BoardOutline(24, 22))
        constraints = compile_constraints(
            {
                "board": {"outline": {"w": 24, "h": 22}},
                "fixed": {"H": {"at": [10, 10], "side": "top"}},
                "side": {"bottom": ["P"]},
            },
            graph.refs,
        )

        def placement(g, c, **kw):
            if "P" in c.locked_refs:
                raise LegalizationError("unrelated hard group")
            return copy.deepcopy(g), PlacementReport(24, 22, 1, 1)

        with patch("pnr.place.initial_pool.place", side_effect=placement), patch(
            "pnr.place.capacity_proxy.cheap_score", return_value=0
        ), patch("pnr.place.capacity_proxy.score", return_value={"score": 0}), patch(
            "pnr.route.detail.router.route_board"
        ) as route:
            _, _, _, report = select_initial_placement(
                graph,
                constraints,
                {"layers": 2},
                config=InitialPoolConfig(starts=4, proxy_budget=4),
                iters=1,
                proxy_only=True,
            )
        route.assert_not_called()
        basin = report["candidates"][2]
        self.assertEqual(basin["status"], "legal")
        self.assertEqual(basin["basin_fallback_from"], "start-00")
        self.assertEqual(basin["poses"]["P"][:2], [10, 10])
        self.assertNotIn("P", constraints.locked_refs)

    def test_config_is_opt_in_and_bounded(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(InitialPoolConfig.from_environment())
        with patch.dict(
            os.environ,
            {"PNR_INITIAL_POOL": "1", "PNR_INITIAL_STARTS": "4", "PNR_INITIAL_FINALISTS": "2"},
            clear=True,
        ):
            self.assertEqual(InitialPoolConfig.from_environment().starts, 4)
        for kwargs in [
            dict(starts=1),
            dict(route_finalists=9),
            dict(proxy_budget=1),
            dict(proxy_pitch_mm=0),
        ]:
            with self.assertRaises(ValueError):
                InitialPoolConfig(**kwargs)


class PairCouplingRankTest(unittest.TestCase):
    def test_a_pair_left_as_legs_ranks_after_a_coupled_one(self):
        # board.route_pairs: coupled: a finalist whose declared pair fell back to two legs
        # (KiCad's gap and uncoupled-length rules fail) ranks after one routed coupled, ahead
        # of the vias and copper (11-ufbga201-fanout-6L-SGSGPS-pairs seed 1 under
        # PNR_LEGALIZE_KEEP picked the fewer-vias finalist with its LVDS pair as legs).
        def board(status, vias):
            return NS(
                result=RouteResult({}, [], 1),
                deferred_nets=set(),
                tracks=[],
                vias=[None] * vias,
                escape_diagnostics=dict(
                    coupled_pairs=dict(pairs=dict(lvds=dict(status=status)), groups=[])
                ),
            )

        legs, coupled = _route_metrics(board("legs", 134)), _route_metrics(board("coupled", 139))
        self.assertEqual((legs["pairs_uncoupled"], coupled["pairs_uncoupled"]), (1, 0))
        self.assertLess(route_rank(coupled), route_rank(legs))
        # Without the coupled router's report the key is the previous one.
        plain = _route_metrics(
            NS(result=RouteResult({}, [], 1), deferred_nets=set(), tracks=[], vias=[])
        )
        self.assertNotIn("pairs_uncoupled", plain)

    def test_a_coupled_pair_off_its_gap_ranks_after_a_clean_one(self):
        # A coupled pair whose fanout runs parallel to the mate's lane off the gap (pair_route's
        # gap_breaks) fails KiCad's diff_pair_gap rule as a pair left as legs does (seed 9).
        def board(breaks, vias):
            row = dict(status="coupled", gap_breaks=breaks, coupling_judged=True)
            return NS(
                result=RouteResult({}, [], 1),
                deferred_nets=set(),
                tracks=[],
                vias=[None] * vias,
                escape_diagnostics=dict(coupled_pairs=dict(pairs=dict(lvds=row), groups=[])),
            )

        broken, clean = _route_metrics(board(2, 137)), _route_metrics(board(0, 140))
        self.assertEqual((broken["pairs_gap"], clean["pairs_gap"]), (1, 0))
        self.assertLess(route_rank(clean), route_rank(broken))
        # A pair whose coupling the design does not judge keeps the previous key.
        free = board(2, 137)
        free.escape_diagnostics["coupled_pairs"]["pairs"]["lvds"]["coupling_judged"] = False
        self.assertEqual(_route_metrics(free)["pairs_gap"], 0)


class InitialSelectionTest(unittest.TestCase):
    def test_routed_evidence_outvotes_proxy_and_baseline_has_same_budget(self):
        graph, constraints = fixture()
        before = graph.to_json()
        calls = []

        def placement(g, c, **kw):
            out = copy.deepcopy(g)
            # Deterministically distinct legal basins; fixed/native locks unchanged.
            index = (kw["seed"] // 104729) % 8
            out.component("A").pos = (5 + index, 4)
            out.component("B").pos = (12 - index * 0.5, 8)
            return out, PlacementReport(20, 16, 10, 10)

        def routed(g, c, r, **kw):
            calls.append((g.component("A").pos, kw))
            # The cheapest-capacity baseline is incomplete; another finalist wins.
            missing = 2 if g.component("A").pos[0] == 5 else 0
            net = RoutedNet("N", remaining_connections=missing)
            return NS(
                result=RouteResult({"N": net}, ["N"] if missing else [], 1),
                deferred_nets=set(),
                tracks=[],
                vias=[],
                escape_diagnostics={},
            )

        with tempfile.TemporaryDirectory() as tmp, patch(
            "pnr.place.initial_pool.place", side_effect=placement
        ), patch(
            "pnr.place.capacity_proxy.cheap_score", side_effect=lambda g, r: g.component("A").pos[0]
        ), patch(
            "pnr.place.capacity_proxy.score",
            side_effect=lambda g, r, **kw: {"score": g.component("A").pos[0]},
        ), patch(
            "pnr.route.detail.router.route_board", side_effect=routed
        ):
            chosen, prep, route, report = select_initial_placement(
                graph,
                constraints,
                {"layers": 2},
                config=InitialPoolConfig(starts=5, route_finalists=3, proxy_budget=4),
                iters=5,
                route_iters=7,
                pitch=0.25,
                output=tmp,
            )
            saved = json.loads((Path(tmp) / "report.json").read_text())
            self.assertEqual(saved["selected"], report["selected"])
            self.assertIn("start-00", report["route_finalists"])
            self.assertIn("start-01", report["route_finalists"])
            self.assertEqual(report["detailed_evaluations"], 3)
            self.assertEqual(report["proxy_evaluations"], 4)
            self.assertNotEqual(report["selected"], "start-00")
            self.assertEqual([kw for _, kw in calls], [{"pitch": 0.25, "max_iters": 7}] * 3)
            self.assertFalse(report["plateau_observed"])
            self.assertEqual(graph.to_json(), before)
            self.assertEqual(chosen.component("LOCKED").pos, (15, 12))

    def test_unfinished_finalists_route_the_next_by_the_screen(self):
        # Every finalist leaves a connection open: the next candidates by the proxy screen are
        # routed too (EXTRA_FINALISTS at most), and the first complete one is chosen.
        graph, constraints = fixture()
        routed_at = []

        def placement(g, c, **kw):
            out = copy.deepcopy(g)
            index = (kw["seed"] // 104729) % 8
            out.component("A").pos = (5 + index, 4)
            out.component("B").pos = (12 - index * 0.5, 8)
            return out, PlacementReport(20, 16, 10, 10)

        def routed(g, c, r, **kw):
            routed_at.append(g.component("A").pos[0])
            # Only the fifth-best placement by the screen routes completely.
            missing = 0 if len(routed_at) == 5 else 1
            net = RoutedNet("N", remaining_connections=missing)
            return NS(
                result=RouteResult({"N": net}, ["N"] if missing else [], 1),
                deferred_nets=set(),
                tracks=[],
                vias=[],
                escape_diagnostics={},
            )

        with patch("pnr.place.initial_pool.place", side_effect=placement), patch(
            "pnr.place.capacity_proxy.cheap_score", side_effect=lambda g, r: g.component("A").pos[0]
        ), patch(
            "pnr.place.capacity_proxy.score",
            side_effect=lambda g, r, **kw: {"score": g.component("A").pos[0]},
        ), patch(
            "pnr.route.detail.router.route_board", side_effect=routed
        ):
            _chosen, _prep, _route, report = select_initial_placement(
                graph,
                constraints,
                {"layers": 2},
                config=InitialPoolConfig(starts=8, route_finalists=3, proxy_budget=8),
                iters=5,
                route_iters=7,
                pitch=0.25,
            )
        self.assertEqual(report["detailed_evaluations"], 3 + EXTRA_FINALISTS)
        self.assertEqual(len(report["extra_finalists"]), EXTRA_FINALISTS)
        self.assertEqual(report["selected"], report["route_finalists"][4])
        self.assertTrue(unfinished(dict(objective=[1, 1, 0, 0.0])))
        self.assertTrue(unfinished(dict(objective=[0, 0, 0, 0.0], judged_pair_flaws=1)))
        self.assertFalse(unfinished(dict(objective=[0, 0, 0, 0.0], pairs_uncoupled=1)))

    def test_candidates_changing_pad_net_are_rejected_before_route(self):
        graph, constraints = fixture()

        def placement(g, c, **kw):
            out = copy.deepcopy(g)
            out.component("A").pads[0].net = "BAD"
            return out, PlacementReport(20, 16, 10, 10)

        with patch("pnr.place.initial_pool.place", side_effect=placement), patch(
            "pnr.place.capacity_proxy.cheap_score", return_value=0
        ), patch("pnr.place.capacity_proxy.score", return_value={"score": 0}), patch(
            "pnr.route.detail.router.route_board",
            return_value=NS(
                result=RouteResult({}, [], 1),
                deferred_nets=set(),
                tracks=[],
                vias=[],
                escape_diagnostics={},
            ),
        ) as route:
            _, _, _, report = select_initial_placement(
                graph,
                constraints,
                {"layers": 2},
                config=InitialPoolConfig(starts=3, route_finalists=2, proxy_budget=3),
                iters=0,
            )
        self.assertEqual(route.call_count, 1)  # only unchanged source incumbent survives
        self.assertEqual(report["selected"], "start-01")
        self.assertEqual(report["candidates"][0]["status"], "rejected_hard_constraints")

    def test_proxy_only_never_routes_or_claims_selected_placement(self):
        graph, constraints = fixture()
        with patch(
            "pnr.place.initial_pool.place", return_value=(graph, PlacementReport(20, 16, 10, 10))
        ), patch("pnr.place.capacity_proxy.cheap_score", return_value=0), patch(
            "pnr.place.capacity_proxy.score", return_value={"score": 3}
        ), patch(
            "pnr.route.detail.router.route_board"
        ) as route:
            chosen, prep, cached, report = select_initial_placement(
                graph,
                constraints,
                {"layers": 2},
                config=InitialPoolConfig(starts=3, route_finalists=2, proxy_budget=3),
                iters=0,
                proxy_only=True,
            )
        route.assert_not_called()
        self.assertIsNone(cached)
        self.assertIsNone(report["selected"])
        self.assertEqual(report["detailed_evaluations"], 0)
        self.assertEqual(report["termination"], "proxy_only_budget_completed")
        self.assertTrue(report["recommendation"])
        self.assertFalse(report["plateau_observed"])

    def test_feedback_reuses_initial_winner_route_without_extra_budget(self):
        from pnr.route.feedback import route_and_place

        graph, constraints = fixture()
        route = NS(result=RouteResult({}, [], 1), deferred_nets=set(), tracks=[], vias=[])
        pool_report = {"selected": "start-00", "candidates": [{"id": "start-00", "seed": 0}]}
        with patch.dict(os.environ, {}, clear=True), patch(
            "pnr.place.initial_pool.select_initial_placement",
            return_value=(graph, PlacementReport(20, 16, 10, 10), route, pool_report),
        ) as select, patch("pnr.route.detail.router.route_board") as route_again:
            chosen, report = route_and_place(
                graph, constraints, iters=1, max_rounds=1, detail_rules={}, initial_pool=True
            )
        select.assert_called_once()
        route_again.assert_not_called()
        self.assertIs(report.detail_result, route)
        self.assertIs(report.initial_pool, pool_report)


class PoolRelocationScreenTest(unittest.TestCase):
    """PNR_POOL_RELOCATION_SCREEN (review of #64/#70: the pool picked a start whose
    PNR_LEGALIZE_KEEP legalization relocated the whole board, not only the occluded part the
    owner's principle expects -- 11-ufbga201-fanout-6L-SGSGPS-rails s0, 07-chaser-20-4L-SGPS
    s0; the earlier cheap_score penalty never kept it out of the diversity fill)."""

    def test_wholesale_needs_more_than_one_part_and_more_than_the_share(self):
        prep = PlacementReport(20, 16, 10, 10)
        self.assertFalse(wholesale_relocation(prep, total_movable=10))  # no motion recorded
        prep.legal_motion = {"relocated": 0}
        self.assertFalse(wholesale_relocation(prep, total_movable=10))
        prep.legal_motion = {"relocated": 2}  # 20 %: exactly the threshold
        self.assertAlmostEqual(POOL_RELOCATION_FRACTION_THRESHOLD, 0.2)
        self.assertFalse(wholesale_relocation(prep, total_movable=10))
        prep.legal_motion = {"relocated": 3}
        self.assertTrue(wholesale_relocation(prep, total_movable=10))
        prep.legal_motion = {"relocated": 1}  # one occluded part, even on a two-part board
        self.assertFalse(wholesale_relocation(prep, total_movable=2))
        prep.legal_motion = {"relocated": 10}
        with patch.dict(os.environ, {"PNR_POOL_RELOCATION_SCREEN": "0"}):
            self.assertFalse(wholesale_relocation(prep, total_movable=10))
        with patch.dict(os.environ, {"PNR_LEGALIZE_KEEP": "0"}):
            self.assertFalse(wholesale_relocation(prep, total_movable=10))

    def run_pool(self, wholesale_calls):
        """select_initial_placement on the fixture; place() call numbers in
        ``wholesale_calls`` relocate every movable part and screen best (cheap and proxy)."""
        graph, constraints = fixture()
        calls = []

        def placement(g, c, **kw):
            calls.append(None)
            wholesale = len(calls) in wholesale_calls
            out = copy.deepcopy(g)
            out.component("A").pos = (6, 4) if wholesale else (5 - 0.1 * len(calls), 4)
            prep = PlacementReport(20, 16, 10, 10)
            prep.legal_motion = {"relocated": 2 if wholesale else 0}
            return out, prep

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True), patch(
            "pnr.place.initial_pool.place", side_effect=placement
        ), patch(
            "pnr.place.capacity_proxy.cheap_score",
            side_effect=lambda g, r: 1.0 if g.component("A").pos[0] == 6 else 10.0,
        ), patch(
            "pnr.place.capacity_proxy.score",
            side_effect=lambda g, r, **kw: {"score": 1.0 if g.component("A").pos[0] == 6 else 5.0},
        ), patch(
            "pnr.route.detail.router.route_board",
            side_effect=lambda g, c, r, **kw: NS(
                result=RouteResult({"N": RoutedNet("N", remaining_connections=0)}, [], 1),
                deferred_nets=set(),
                tracks=[],
                vias=[],
                escape_diagnostics={},
            ),
        ):
            select_initial_placement(
                graph,
                constraints,
                {"layers": 2},
                config=InitialPoolConfig(starts=5, route_finalists=2, proxy_budget=3),
                iters=5,
                route_iters=7,
                pitch=0.25,
                output=tmp,
            )
            return json.loads((Path(tmp) / "report.json").read_text())

    def test_a_wholesale_start_enters_no_shortlist_while_another_legal_start_exists(self):
        """It screens best on both the cheap and the proxy screen, and would take the best
        screen's finalist slot; the screen keeps it out of the proxy shortlist, the diversity
        fill and the routed finalists."""
        report = self.run_pool({2})
        flagged = [c for c in report["candidates"] if c.get("wholesale_relocation")]
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0]["relocated"], 2)
        self.assertEqual(report["relocation_screened"], [flagged[0]["id"]])
        self.assertNotIn("proxy_evaluated", flagged[0])
        self.assertNotIn(flagged[0]["id"], report["route_finalists"])
        self.assertEqual(len(report["route_finalists"]), 2)

    def test_every_start_wholesale_still_selects_one(self):
        # The source incumbent records no motion; flag it too, so no other legal start exists.
        with patch("pnr.place.initial_pool.wholesale_relocation", lambda prep, n: True):
            report = self.run_pool(set(range(1, 20)))
        self.assertTrue(all(c.get("wholesale_relocation") for c in report["candidates"]))
        self.assertNotIn("relocation_screened", report)
        self.assertTrue(report["route_finalists"])


if __name__ == "__main__":
    unittest.main()
