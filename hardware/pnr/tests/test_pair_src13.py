"""src13 regressions: PNR_PAIR_PER_RUN_UNCOUPLED (A), PNR_PAIR_STUB_MAX_MM (B),
PNR_PAIR_LANDING_RESERVE (C), PNR_PAIR_EARLY_EXIT and the derived hand-swap trial (D).

One file for both interpreters: the native classes need pcbnew (KiCad Python),
the placement classes need numpy/yaml/torch (the PnR runtime); each class skips
what its interpreter lacks, the pure-geometry classes run under both.

  KiCad:   PYTHONPATH=$PWD/..:$PWD .../KiCad.app/.../python3 -m unittest -v test_pair_src13
  runtime: PYTHONPATH=$PWD/..:$PWD .../pnr-regression-runtime/bin/python -m unittest -v test_pair_src13
"""

import json
import math
import os
import random
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

try:
    import pcbnew as k
except ImportError:
    k = None
try:
    import numpy  # noqa: F401
    import yaml  # noqa: F401

    from pnr.place.legalize import legalize  # imports pnr.place (torch)

    PLACE = True
except Exception:
    PLACE = False

from pnr.route.detail.coupled import (
    branch_lengths,
    capsule_interval,
    offset_path,
    path_metrics,
    path_steps,
    uncoupled_runs,
)

W, G = 0.2, 0.15  # width/gap: nominal lane pitch .35
FL, BL = 0, 1  # abstract layers for the pure-geometry tests
HEIGHTS = {FL: 0, BL: 1.6}
plen = lambda path: sum(math.dist(a, b) for a, b in zip(path, path[1:]))
steps = lambda layer, path: [(layer, a, b) for a, b in zip(path, path[1:])]


def env(**values):
    """patch.dict for os.environ that also removes keys given as None."""

    class _Env:
        def __enter__(self):
            self.saved = {key: os.environ.get(key) for key in values}
            for key, value in values.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = str(value)

        def __exit__(self, *exc):
            for key, value in self.saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    return _Env()


# --------------------------------------------------------------------------- A/B pure geometry
class CapsuleAndRunGeometryTest(unittest.TestCase):
    def test_capsule_interval_matches_dense_sampling(self):
        rng = random.Random(7)

        def dist(q, c, d):
            vx, vy = d[0] - c[0], d[1] - c[1]
            L2 = vx * vx + vy * vy
            t = 0 if L2 == 0 else max(0, min(1, ((q[0] - c[0]) * vx + (q[1] - c[1]) * vy) / L2))
            return math.dist(q, (c[0] + t * vx, c[1] + t * vy))

        for _ in range(300):
            a, b, c, d = [(rng.uniform(0, 4), rng.uniform(0, 4)) for _ in range(4)]
            if rng.random() < 0.1:
                d = c  # degenerate mate segment = disc
            r = rng.uniform(0.05, 1.5)
            inside = [
                i / 2000
                for i in range(2001)
                if dist((a[0] + i / 2000 * (b[0] - a[0]), a[1] + i / 2000 * (b[1] - a[1])), c, d)
                <= r
            ]
            got = capsule_interval(a, b, c, d, r)
            if not inside:
                self.assertTrue(got is None or got[1] - got[0] < 2e-3, (a, b, c, d, r, got))
                continue
            self.assertIsNotNone(got)
            self.assertAlmostEqual(got[0], inside[0], delta=1e-3)
            self.assertAlmostEqual(got[1], inside[-1], delta=1e-3)

    def test_fanouts_are_the_leading_and_trailing_runs(self):
        # Vertical 1 mm fanouts into a straight lane at nominal pitch .35: the
        # last (w+g)*tolerance of each fanout is within the coupled tolerance.
        p = [(0, 1.175), (0, 0.175), (8, 0.175), (8, 1.175)]
        n = [(0, -1.175), (0, -0.175), (8, -0.175), (8, -1.175)]
        runs = uncoupled_runs({"p": steps(FL, p), "n": steps(FL, n)}, W, G)
        expected = 1.0 - (W + G) * 0.1
        for net in ("p", "n"):
            self.assertEqual(len(runs[net]["runs"]), 2)
            self.assertAlmostEqual(runs[net]["leading_mm"], expected, places=6)
            self.assertAlmostEqual(runs[net]["trailing_mm"], expected, places=6)
            self.assertAlmostEqual(runs[net]["max_mm"], expected, places=6)

    def test_run_continues_through_a_via_hop_and_absorbs_short_slivers(self):
        p = [(FL, (0, 1), (0, 3)), (None, (0, 3), (0, 3)), (BL, (0, 3), (0, 4))]
        n = [(FL, (5, 1), (5, 3)), (None, (5, 3), (5, 3)), (BL, (5, 3), (5, 4))]
        runs = uncoupled_runs({"p": p, "n": n}, W, G)
        self.assertEqual([round(r["length_mm"], 9) for r in runs["p"]["runs"]], [3.0])
        # The mate's end touching p's coupling capsule for 2*sqrt(.385^2-.36^2)
        # = .27 mm (< min_coupled = w+g): that sliver stays in the run.
        p = [(0, 0), (4, 0)]
        n = [(2, -0.36), (2, -3)]
        runs = uncoupled_runs({"p": steps(FL, p), "n": steps(FL, n)}, W, G)
        self.assertEqual(len(runs["p"]["runs"]), 1)
        self.assertAlmostEqual(runs["p"]["max_mm"], 4.0, places=6)
        # A long coupled stretch splits it.
        n = [(1, -0.35), (3, -0.35), (3, -3)]
        runs = uncoupled_runs({"p": steps(FL, p), "n": steps(FL, n)}, W, G)
        self.assertEqual(len(runs["p"]["runs"]), 2)
        # A sliver at the very start (pads at lane pitch) counts into the leading
        # run, as one at the very end counts into the trailing run.
        p = [(0, 0), (0, 3)]
        n = [(0.35, 0), (0.35, -3)]
        runs = uncoupled_runs({"p": steps(FL, p), "n": steps(FL, n)}, W, G)
        self.assertAlmostEqual(runs["p"]["leading_mm"], 3.0, places=6)
        self.assertAlmostEqual(runs["p"]["trailing_mm"], 3.0, places=6)
        self.assertEqual(runs["p"]["runs"][0]["start"], (FL, 0, 0))

    def test_min_coupled_floor_splits_at_any_coupled_copper(self):
        # The .27 mm sliver above: merged under the first src13 convention
        # (min_coupled = w+g), a run boundary under the literal reading.
        p = [(0, 0), (4, 0)]
        n = [(2, -0.36), (2, -3)]
        runs = uncoupled_runs({"p": steps(FL, p), "n": steps(FL, n)}, W, G, min_coupled=0.001)
        self.assertEqual(len(runs["p"]["runs"]), 2)
        self.assertAlmostEqual(
            sum(r["length_mm"] for r in runs["p"]["runs"]),
            4.0 - 2 * math.sqrt(0.385**2 - 0.36**2),
            places=6,
        )

    def test_barrel_joins_the_run_it_lies_in(self):
        p = [(FL, (0, 1), (0, 3)), (None, (0, 3), (0, 3)), (BL, (0, 3), (0, 4))]
        n = [(FL, (5, 1), (5, 3)), (None, (5, 3), (5, 3)), (BL, (5, 3), (5, 4))]
        runs = uncoupled_runs({"p": p, "n": n}, W, G, barrel=1.6)
        self.assertEqual([round(r["length_mm"], 9) for r in runs["p"]["runs"]], [4.6])

    def test_a_break_point_ends_the_run(self):
        # p runs through a pad at (2, 0): with the pad as a break, two runs.
        p = [(0, 0), (2, 0), (4, 0)]
        n = [(0, 5), (4, 5)]
        whole = uncoupled_runs({"p": steps(FL, p), "n": steps(FL, n)}, W, G)
        split = uncoupled_runs(
            {"p": steps(FL, p), "n": steps(FL, n)},
            W,
            G,
            breaks=lambda la, q: math.dist(q, (2, 0)) < 1e-9,
        )
        self.assertEqual([round(r["length_mm"], 9) for r in whole["p"]["runs"]], [4.0])
        self.assertEqual([round(r["length_mm"], 9) for r in split["p"]["runs"]], [2.0, 2.0])
        self.assertEqual(split["p"]["runs"][0]["end"], (FL, 2, 0))

    def test_exempt_copper_ends_a_run_and_is_not_counted(self):
        p = [(0, 5), (2, 5), (4, 5)]
        n = [(0, 0), (4, 0)]
        exempt = lambda la, a, b: a[0] < 2 - 1e-9
        runs = uncoupled_runs({"p": steps(FL, p), "n": steps(FL, n)}, W, G, exempt=exempt)
        self.assertAlmostEqual(runs["p"]["max_mm"], 2.0, places=6)
        self.assertEqual(runs["p"]["leading_mm"], 0.0)

    def test_45_degree_lanes_are_coupled_through_the_corner(self):
        center = [(0, 0), (5, 0), (8, 3), (12, 3)]
        p, n = offset_path(center, (W + G) / 2), offset_path(center, -(W + G) / 2)
        runs = uncoupled_runs({"p": steps(FL, p), "n": steps(FL, n)}, W, G)
        self.assertEqual(runs["p"]["runs"], [])
        self.assertEqual(runs["n"]["runs"], [])

    def test_path_steps_marks_via_hops(self):
        nodes = [(0, 0, 0), (0, 1000000, 0), (1, 1000000, 0), (1, 2000000, 0)]
        self.assertEqual(
            path_steps(nodes), [(0, (0, 0), (1, 0)), (None, (1, 0), (1, 0)), (1, (1, 0), (2, 0))]
        )

    def test_branch_lengths_in_line_trace_stub_and_barrel_stub(self):
        S, T, D = ((0, 0), FL), ((10, 0), FL), (5, 0)
        inline = branch_lengths(
            [(FL, (0, 0), (5, 0)), (FL, (5, 0), (10, 0))],
            [],
            S,
            T,
            [(D, FL)],
            layer_heights=HEIGHTS,
        )
        self.assertEqual((inline[0]["on_path"], inline[0]["stub_mm"]), (True, 0.0))
        trace = branch_lengths(
            [(FL, (0, 0), (10, 0)), (FL, (5, 0), (5, 0.8))],
            [],
            S,
            T,
            [((5, 0.8), FL)],
            layer_heights=HEIGHTS,
        )
        self.assertFalse(trace[0]["on_path"])
        self.assertAlmostEqual(trace[0]["stub_mm"], 0.8, places=6)
        # Trunk on B between two vias; a third via at x=5 carries an F stub to D:
        # the stub is the barrel (1.6) plus the 0.7 mm trace.
        tracks = [
            (FL, (0, 0), (2, 0)),
            (BL, (2, 0), (8, 0)),
            (FL, (8, 0), (10, 0)),
            (FL, (5, 0), (5, 0.7)),
        ]
        vias = [((2, 0), [FL, BL]), ((8, 0), [FL, BL]), ((5, 0), [FL, BL])]
        barrel = branch_lengths(tracks, vias, S, T, [((5, 0.7), FL)], layer_heights=HEIGHTS)
        self.assertAlmostEqual(barrel[0]["stub_mm"], 2.3, places=6)
        # The stub path runs from the point to the junction (via hop included).
        self.assertEqual(
            [(q[0], q[1] / 1e6, q[2] / 1e6) for q in barrel[0]["path"]],
            [(FL, 5, 0.7), (FL, 5, 0), (BL, 5, 0)],
        )
        planar = branch_lengths(
            tracks, vias, S, T, [((5, 0.7), FL)], layer_heights=HEIGHTS, barrel=0.0
        )
        self.assertAlmostEqual(planar[0]["stub_mm"], 0.7, places=6)  # PNR_PAIR_STUB_PLANAR reading
        self.assertIsNone(
            branch_lengths(
                tracks + [(FL, (0, 0), (0, 1)), (FL, (0, 1), (2, 1)), (FL, (2, 1), (2, 0))],
                vias,
                S,
                T,
                [((5, 0.7), FL)],
                layer_heights=HEIGHTS,
            )
        )


# --------------------------------------------------------------------------- A native
@unittest.skipIf(k is None, "needs pcbnew (KiCad Python)")
class PerRunBridgeBudgetTest(unittest.TestCase):
    def setUp(self):
        from test_native_electrical import FAB, board

        self.b = board()
        self.FAB = FAB
        self.pair = dict(
            name="usb",
            p="p",
            n="n",
            width_mm=W,
            gap_mm=G,
            skew_mm=0.3,
            max_uncoupled_mm=2,
            terminal_chain=[{"p": "J.1", "n": "J.2"}, {"p": "U.1", "n": "U.2"}],
        )
        self.rules = dict(
            fab={
                "track_width_mm": 0.2,
                "clearance_mm": 0.127,
                "via_diameter_mm": 0.45,
                "via_drill_mm": 0.3,
            },
            electrical_fab=FAB,
            net_classes=[],
        )

    def call(self, flag, head_prior=0.0, routed=False):
        from pnr.native_electrical import Oracle, pair_layer_bridge

        source = dict(
            sites={"p": (2, 5.3), "n": (2, 4.7)},
            paths={"p": [], "n": []},
            lengths={"p": 0.3, "n": 0.25},
            shift_mm=0,
        )
        target = dict(
            sites={"p": (15, 5.3), "n": (15, 4.7)},
            paths={"p": [], "n": []},
            lengths={"p": 1.2, "n": 1.1},
            shift_mm=0,
        )
        seen = []

        def ports(pair, terminals, rules, oracle, bounds, index):
            return [(source, target)[index]]

        def solve(*args, **kw):
            seen.append(kw)
            if routed:
                return dict(
                    status="routed",
                    paths={"p": [(2, 5.3), (15, 5.3)], "n": [(2, 4.7), (15, 4.7)]},
                    lengths={"p": 13.0, "n": 13.0},
                )
            return dict(status="no_coupled_channel", failures={"scripted": 1})

        with env(PNR_PAIR_PER_RUN_UNCOUPLED="1" if flag else None), patch(
            "pnr.native_electrical.pair_bridge_ports", side_effect=ports
        ), patch("pnr.native_electrical.solve_pair", side_effect=solve):
            result = pair_layer_bridge(
                self.b,
                self.pair,
                {"p": ((1, 5.2), (16, 5.2)), "n": ((1, 4.8), (16, 4.8))},
                self.rules,
                Oracle(self.b, self.rules),
                (0, 0, 20, 20),
                0.15,
                {"p": 0, "n": 0},
                head_prior_mm=head_prior,
            )
        return result, seen

    def test_default_shares_one_budget_between_both_ends(self):
        result, seen = self.call(False, head_prior=0.5)
        self.assertEqual(len(seen), 1)
        self.assertAlmostEqual(seen[0]["max_uncoupled"], 2 - 1.2)
        self.assertNotIn("max_uncoupled_head", seen[0])
        self.assertNotIn("max_uncoupled_tail", seen[0])

    def test_each_end_is_its_own_run_and_the_head_pays_the_arriving_run(self):
        result, seen = self.call(True, head_prior=0.5)
        # Pass 1: the legacy search (shared .8 at both ends); pass 2 only after
        # pass 1 found nothing: full per-end budgets.
        self.assertEqual(len(seen), 2)
        self.assertAlmostEqual(seen[0]["max_uncoupled_head"], 0.8)
        self.assertAlmostEqual(seen[0]["max_uncoupled_tail"], 0.8)
        self.assertAlmostEqual(seen[1]["max_uncoupled_head"], 2 - (0.3 + 0.5))
        self.assertAlmostEqual(seen[1]["max_uncoupled_tail"], 2 - 1.2)
        for kw in seen:
            self.assertAlmostEqual(
                kw["max_uncoupled"], 2 - 1.2
            )  # lane-shape scale: the legacy shared value
            self.assertEqual(kw["max_tuning_length"], 2)

    def test_pass_one_is_the_legacy_search_even_after_an_arriving_run(self):
        result, seen = self.call(True, head_prior=1.4)  # own head budget 2-(.3+1.4)=.3 < shared .8
        # Pass 1 keeps the legacy geometric bound (the exact run check decides);
        # pass 2 bounds the head by its own run budget.
        self.assertEqual(len(seen), 2)
        self.assertAlmostEqual(seen[0]["max_uncoupled_head"], 0.8)
        self.assertAlmostEqual(seen[0]["max_uncoupled_tail"], 0.8)
        self.assertAlmostEqual(seen[1]["max_uncoupled_head"], 0.3)
        self.assertAlmostEqual(seen[1]["max_uncoupled_tail"], 0.8)

    def test_no_head_budget_left_skips_the_candidate(self):
        result, seen = self.call(True, head_prior=1.75)
        self.assertEqual(seen, [])
        self.assertEqual(result["status"], "pair_no_matched_layer_bridge")
        self.assertEqual(result["failures"], {"uncoupled_budget": 1})

    def test_run_barrel_charges_both_via_hops(self):
        # Copper reading: head 2-(.3+1.6) = .1, tail 2-(1.2+1.6) < 0: no budget.
        with env(PNR_PAIR_RUN_BARREL="1"):
            result, seen = self.call(True)
        self.assertEqual(seen, [])
        self.assertEqual(result["failures"], {"uncoupled_budget": 1})

    def test_reference_trim_is_the_legacy_shared_budget_unless_per_end(self):
        # Pass-1 budgets with a 1.4 mm arriving run: head .3, tail .8 (shared .8).
        result, _ = self.call(True, head_prior=1.4, routed=True)
        self.assertEqual(result["uncoupled_budget_mm"]["reference_trim"], [0.8, 0.8])  # as src12b
        with env(PNR_PAIR_REF_TRIM_PER_END="1"):
            result, _ = self.call(True, head_prior=1.4, routed=True)
        self.assertEqual(
            [round(v, 9) for v in result["uncoupled_budget_mm"]["reference_trim"]], [0.3, 0.8]
        )

    def test_solve_pair_per_end_caps_default_to_the_shared_cap(self):
        from pnr.route.detail.coupled import solve_pair

        terminals = {"p": ((2, 5.6), (12, 5.175)), "n": ((2, 4.4), (12, 4.825))}
        free = lambda *a, **kw: True
        base = solve_pair(
            "p",
            "n",
            terminals,
            (0, 0, 14, 10),
            lambda net, a, b, w: True,
            lambda a, b, w: True,
            W,
            G,
            0.3,
            pitch=0.25,
            max_uncoupled=2.0,
        )
        same = solve_pair(
            "p",
            "n",
            terminals,
            (0, 0, 14, 10),
            lambda net, a, b, w: True,
            lambda a, b, w: True,
            W,
            G,
            0.3,
            pitch=0.25,
            max_uncoupled=2.0,
            max_uncoupled_head=2.0,
            max_uncoupled_tail=2.0,
        )
        self.assertEqual(base["status"], "routed")
        self.assertEqual(base["paths"], same["paths"])
        # A 0.3 mm head budget cannot reach the lane from pads 1.2 mm apart; the
        # same budget at the tail (pads already at pitch) still routes.
        head = solve_pair(
            "p",
            "n",
            terminals,
            (0, 0, 14, 10),
            lambda net, a, b, w: True,
            lambda a, b, w: True,
            W,
            G,
            0.3,
            pitch=0.25,
            max_uncoupled=2.0,
            max_uncoupled_head=0.3,
        )
        tail = solve_pair(
            "p",
            "n",
            terminals,
            (0, 0, 14, 10),
            lambda net, a, b, w: True,
            lambda a, b, w: True,
            W,
            G,
            0.3,
            pitch=0.25,
            max_uncoupled=2.0,
            max_uncoupled_tail=0.3,
        )
        self.assertNotEqual(head["status"], "routed")
        self.assertEqual(tail["status"], "routed")


@unittest.skipIf(k is None, "needs pcbnew (KiCad Python)")
class ExactMinimumRunTest(unittest.TestCase):
    """U6-like module edge: 1.5 x 0.9 pads at 1.26 pitch; exact min run ~ .75+.225+.127."""

    def ports(self, flag):
        from test_native_electrical import FAB, board, pad

        from pnr.native_electrical import Oracle, pair_bridge_ports

        b = board()
        for num, net, y in (
            ("12", "other", 7.89),
            ("13", "n", 6.63),
            ("14", "p", 5.37),
            ("15", "other", 4.11),
        ):
            pad(b, "U", num, net, (10, y), (1.5, 0.9))
        # jlc-pofv 5A via-to-SMD-pad 0.127 (as the real rules.json): also against
        # the via's own-net pad, unlike the legacy 0.05 mm keep-away.
        rules = dict(
            fab={
                "track_width_mm": 0.2,
                "clearance_mm": 0.127,
                "via_diameter_mm": 0.45,
                "via_drill_mm": 0.3,
                "hole_to_hole_mm": 0.25,
                "via_to_smd_pad_mm": 0.127,
            },
            electrical_fab=FAB,
            net_classes=[],
        )
        pair = dict(name="usb", p="p", n="n", width_mm=W, gap_mm=G, skew_mm=0.3, max_uncoupled_mm=2)
        terminals = {"p": ((2, 5.37), (10, 5.37)), "n": ((2, 6.63), (10, 6.63))}
        with env(PNR_PAIR_PER_RUN_UNCOUPLED="1" if flag else None):
            found = pair_bridge_ports(pair, terminals, rules, Oracle(b, rules), (0, 0, 20, 20), 1)
        return found, Oracle(b, rules)

    def test_grid_starts_at_1_2_and_bisection_adds_the_exact_run(self):
        off, _ = self.ports(False)
        on, oracle = self.ports(True)
        run = lambda port: abs((port["sites"]["p"][0] + port["sites"]["n"][0]) / 2 - 10)
        key = lambda port: (
            port["sites"]["p"][0] > 10,
            round(math.dist(port["sites"]["p"], port["sites"]["n"]), 3),
            port["shift_mm"],
        )
        grid = {}
        for port in off:
            grid[key(port)] = min(grid.get(key(port), 9), run(port))
        self.assertTrue(
            all(
                abs(run(port) * 10 - round(run(port) * 10)) < 1e-6 or round(run(port), 6) in (1.75,)
                for port in off
            )
        )  # grid runs only
        exact = [p for p in on if p.get("exact_min_run")]
        self.assertTrue(exact)
        self.assertEqual(on[: len(off)], off)  # the grid part is unchanged
        for port in exact:
            self.assertAlmostEqual(run(port), port["run_mm"], places=6)
            if key(port) in grid:
                self.assertLessEqual(
                    run(port), grid[key(port)] - 0.01
                )  # >= 10 um gained, else not added
            self.assertTrue(
                all(oracle.via(net, point, 0.45, 0.3) for net, point in port["sites"].items())
            )
            # 0.2 um closer to the pads is illegal: it is the exact boundary.
            sign = 1 if port["sites"]["p"][0] > 10 else -1
            closer = {net: (x - sign * 2e-4, y) for net, (x, y) in port["sites"].items()}
            self.assertFalse(
                all(oracle.via(net, point, 0.45, 0.3) for net, point in closer.items())
            )
        # Vias in line with the 1.5 mm pads (pad pitch, no shift): the grid's first
        # legal run is 1.2, the exact one .75 + .225 + (.127 + 1 um engine margin).
        aligned = [port for port in exact if key(port)[1:] == (1.26, 0)]
        self.assertTrue(aligned)
        for port in aligned:
            self.assertAlmostEqual(grid[key(port)], 1.2, places=6)
            self.assertAlmostEqual(run(port), 0.75 + 0.225 + 0.128, delta=2e-4)


# --------------------------------------------------------------------------- A/B _pair_plan_order
@unittest.skipIf(k is None, "needs pcbnew (KiCad Python)")
class ChainRunAndStubTest(unittest.TestCase):
    """Real _pair_plan_order on the J -> D -> U chain of test_pair_post_bridge."""

    def plan(self, flags, stage1, bridge1=None):
        from test_pair_post_bridge import PADS, chain_board, stage0_bridge

        from pnr.native_electrical import Oracle, _pair_plan_order
        from pnr.route.detail import coupled

        b, pair, r = chain_board()
        oracle = Oracle(b, r, deadline=time.monotonic() + 60)
        calls = []
        real = coupled.solve_pair
        self.envelopes = []

        def solve(p, n, terminals, *args, **kw):
            start = {net: tuple(terminals[net][0]) for net in (p, n)}
            calls.append((start, kw))
            self.envelopes.append((start, args[2]))
            if start == {net: PADS["J"][net] for net in (p, n)}:
                return dict(status="no_coupled_channel", failures={"scripted": 1})
            if start == {net: PADS["D"][net] for net in (p, n)} and stage1 is not None:
                offsets = kw.get("offsets") or {}
                return dict(
                    status="routed",
                    paths={net: list(stage1[net]) for net in (p, n)},
                    lengths={net: plen(stage1[net]) + offsets.get(net, 0) for net in (p, n)},
                )
            if start == {net: PADS["D"][net] for net in (p, n)}:
                return dict(status="no_coupled_channel", failures={"scripted": 1})
            return real(p, n, terminals, *args, **kw)

        bridges = []

        def bridge(*args, **kw):
            bridges.append(kw)
            if len(bridges) == 1:
                return stage0_bridge()
            return (
                bridge1(*args, **kw)
                if bridge1
                else dict(status="pair_no_matched_layer_bridge", failures={})
            )

        with env(
            **{
                key: flags.get(key)
                for key in (
                    "PNR_PAIR_POST_BRIDGE_SURFACE",
                    "PNR_PAIR_PER_RUN_UNCOUPLED",
                    "PNR_PAIR_STUB_MAX_MM",
                    "PNR_PAIR_RUN_BREAK_AT_PADS",
                    "PNR_PAIR_STUB_PLANAR",
                    "PNR_PAIR_RUN_MIN_COUPLED_MM",
                    "PNR_PAIR_REF_TRIM_PER_END",
                    "PNR_PAIR_RUN_BARREL",
                )
            }
        ), patch("pnr.native_electrical.solve_pair", side_effect=solve), patch(
            "pnr.native_electrical.pair_layer_bridge", side_effect=bridge
        ):
            result = _pair_plan_order(b, pair, r, oracle, (1, 1, 19, 19), 0.2, ("p", "n"))
        return result, calls, bridges, pair

    # stage-0 stub (bridge vias -> D pad centre) of the scripted chain: 0.6 + 0.566 mm;
    # its last 0.1*sqrt(2) mm lies inside the 0.2 x 0.2 D pad (to where it enters it).
    STUB_CENTRE = 0.6 + math.dist((8.4, 5.6), (8, 5.2))
    STUB = STUB_CENTRE - 0.1 * math.sqrt(2)

    def test_stub_cap_skips_the_via_start_leg_and_the_via_reuse_and_bridges_in_line(self):
        from test_pair_post_bridge import CROSSING

        result, calls, bridges, _ = self.plan(
            {"PNR_PAIR_POST_BRIDGE_SURFACE": "1", "PNR_PAIR_STUB_MAX_MM": "1.0"}, CROSSING
        )
        self.assertEqual(result["status"], "pair_no_matched_layer_bridge")
        self.assertEqual(result["failed_stage"], 1)
        self.assertEqual([s["start"] for s in result["stub_skips"]], ["bridge_vias", "via_reuse"])
        self.assertAlmostEqual(result["stub_skips"][0]["stub_mm"], self.STUB, places=5)
        self.assertAlmostEqual(result["stub_skips"][1]["stub_mm"], 1.6 + self.STUB, places=5)
        self.assertIsNone(bridges[1].get("reuse_source"))  # D in line: bridge from the D pads
        self.assertEqual(len(calls), 2)  # no via-start solve

    def test_planar_stub_reading_drops_the_barrel_from_the_via_reuse_stub(self):
        from test_pair_post_bridge import CROSSING

        result, calls, bridges, _ = self.plan(
            {
                "PNR_PAIR_POST_BRIDGE_SURFACE": "1",
                "PNR_PAIR_STUB_MAX_MM": "1.0",
                "PNR_PAIR_STUB_PLANAR": "1",
            },
            CROSSING,
        )
        self.assertEqual([s["start"] for s in result["stub_skips"]], ["bridge_vias", "via_reuse"])
        self.assertAlmostEqual(
            result["stub_skips"][1]["stub_mm"], self.STUB, places=5
        )  # no 1.6 mm barrel
        result, *_ = self.plan(
            {
                "PNR_PAIR_POST_BRIDGE_SURFACE": "1",
                "PNR_PAIR_STUB_MAX_MM": "1.2",
                "PNR_PAIR_STUB_PLANAR": "1",
            },
            CROSSING,
        )
        self.assertEqual(result["status"], "routed")
        for net in ("p", "n"):
            self.assertAlmostEqual(
                result["stub_metrics"]["D.1/D.2"][net]["stub_mm"], self.STUB, places=5
            )

    def test_stub_within_cap_routes_and_reports_stub_lengths(self):
        from test_pair_post_bridge import CROSSING

        result, calls, bridges, _ = self.plan(
            {"PNR_PAIR_POST_BRIDGE_SURFACE": "1", "PNR_PAIR_STUB_MAX_MM": "1.2"}, CROSSING
        )
        self.assertEqual(result["status"], "routed", result.get("stub_skips"))
        stubs = result["stub_metrics"]["D.1/D.2"]
        for net in ("p", "n"):
            self.assertFalse(stubs[net]["on_path"])
            self.assertAlmostEqual(stubs[net]["stub_mm"], self.STUB, places=5)
            self.assertAlmostEqual(stubs[net]["stub_centre_mm"], self.STUB_CENTRE, places=5)
        from pnr.native_electrical import stub_over_cap

        self.assertEqual(stub_over_cap(result["stub_metrics"], 1.0), ["D.1/D.2", "D.1/D.2"])
        self.assertEqual(
            stub_over_cap(result["stub_metrics"], 1.05), []
        )  # 1.024 to the pad edge (1.166 to its centre)
        # The via-start leg is only skipped above the pad-edge stub, as the final check.
        result, *_ = self.plan(
            {"PNR_PAIR_POST_BRIDGE_SURFACE": "1", "PNR_PAIR_STUB_MAX_MM": "1.05"}, CROSSING
        )
        self.assertEqual(result["status"], "routed")
        self.assertEqual(result["stub_skips"], [])

    def test_in_line_route_has_zero_stub_and_passes_any_cap(self):
        from test_pair_post_bridge import CLEAN

        result, *_ = self.plan(
            {"PNR_PAIR_POST_BRIDGE_SURFACE": "1", "PNR_PAIR_STUB_MAX_MM": "0"}, CLEAN
        )
        self.assertEqual(result["status"], "routed")
        self.assertEqual(
            {net: v["stub_mm"] for net, v in result["stub_metrics"]["D.1/D.2"].items()},
            {"p": 0.0, "n": 0.0},
        )

    def test_flags_off_report_nothing_new(self):
        from test_pair_post_bridge import CLEAN

        result, *_ = self.plan({}, CLEAN)
        self.assertEqual(result["status"], "routed")
        for key in ("stub_metrics", "stub_skips", "uncoupled_run_metrics"):
            self.assertNotIn(key, result)

    def test_per_run_charges_the_arriving_run_and_measures_the_whole_route(self):
        from test_pair_post_bridge import CLEAN, PADS, stage0_bridge

        from pnr.native_electrical import route_uncoupled_runs

        # First src13 convention (coupled stretches < w+g merge into runs): the
        # stage-1 pad-start leg is charged the run arriving at the D pads: the
        # scripted stage 0 is 1.2 mm apart everywhere (never coupled), so the run
        # arriving at D exceeds the cap and the leg gets no budget at all.
        result, calls, bridges, pair = self.plan(
            {"PNR_PAIR_PER_RUN_UNCOUPLED": "1", "PNR_PAIR_RUN_MIN_COUPLED_MM": ".4"}, CLEAN
        )
        self.assertEqual(result["status"], "pair_no_matched_layer_bridge")
        self.assertEqual(result["failed_stage"], 1)
        self.assertEqual(result["surface_failure"]["status"], "pair_uncoupled_budget")
        self.assertGreater(result["surface_failure"]["arriving_mm"], 2)
        self.assertGreater(bridges[1]["head_prior_mm"], 2)  # the via-reuse bridge is charged too
        self.assertNotIn("stub_skips", result)  # no stub cap set
        # Literal reading (default): the D pads sit at lane pitch (0.4 = w+g), so
        # the stubs' last 40 um into them are coupled copper and end the arriving
        # run; the leg routes and the final measurement rejects the 5 mm
        # uncoupled B.Cu trunk.
        result, calls, bridges, pair = self.plan({"PNR_PAIR_PER_RUN_UNCOUPLED": "1"}, CLEAN)
        self.assertEqual(result["status"], "pair_uncoupled_run_limit")
        self.assertGreater(max(m["max_mm"] for m in result["uncoupled_run_metrics"].values()), 5)

    def test_per_run_surface_leg_sees_the_planned_via_apertures(self):
        from test_pair_post_bridge import CLEAN, PADS, VIAS1

        d_start = {net: PADS["D"][net] for net in ("p", "n")}
        through = lambda envelope: envelope(
            (VIAS1["p"][0] - 0.2, VIAS1["p"][1]), (VIAS1["p"][0] + 0.2, VIAS1["p"][1]), 0.55
        )
        self.plan({"PNR_PAIR_PER_RUN_UNCOUPLED": "1", "PNR_PAIR_RUN_BREAK_AT_PADS": "1"}, CLEAN)
        envelope = next(e for start, e in self.envelopes if start == d_start)
        self.assertFalse(through(envelope))  # the stage-0 via's clearance aperture in In1.Cu
        self.assertTrue(envelope((12, 10), (14, 10), 0.55))
        self.plan({}, CLEAN)
        legacy = next(e for start, e in self.envelopes if start == d_start)
        self.assertTrue(through(legacy))  # legacy search is blind to it (its 2 mm trims hide it)

    def test_pad_break_budgets_the_departing_breakout_separately(self):
        from test_pair_post_bridge import CLEAN, PADS

        result, calls, bridges, pair = self.plan(
            {
                "PNR_PAIR_PER_RUN_UNCOUPLED": "1",
                "PNR_PAIR_RUN_BREAK_AT_PADS": "1",
                "PNR_PAIR_RUN_MIN_COUPLED_MM": ".4",
            },
            CLEAN,
        )
        d_start = [
            kw for start, kw in calls if start == {net: PADS["D"][net] for net in ("p", "n")}
        ]
        self.assertEqual(
            d_start[0]["max_uncoupled_head"], 2
        )  # nothing charged from the arriving run
        # The scripted stage 0 is uncoupled over its whole 5 mm B.Cu trunk, so the
        # final measurement still rejects the route; its runs end at the D pads.
        self.assertEqual(result["status"], "pair_uncoupled_run_limit")
        for net in ("p", "n"):
            ends = [tuple(r["end"][1:]) for r in result["uncoupled_run_metrics"][net]["runs"]]
            self.assertIn(PADS["D"][net], ends)
        on, *_ = self.plan({"PNR_PAIR_PER_RUN_UNCOUPLED": "1"}, CLEAN)
        self.assertNotIn("PNR_PAIR_RUN_BREAK_AT_PADS", str(on.get("pair_engine_flags")))


@unittest.skipIf(k is None, "needs pcbnew (KiCad Python)")
class TwoTerminalRunLimitTest(unittest.TestCase):
    """J -> U pair; a scripted surface leg with an 18 mm uncoupled detour."""

    LONG = {
        "p": [(3, 5.2), (3, 8.2), (15, 8.2), (15, 5.2)],
        "n": [(3, 4.8), (3, 1.8), (15, 1.8), (15, 4.8)],
    }
    TIGHT = {"p": [(3, 5.2), (15, 5.2)], "n": [(3, 4.8), (15, 4.8)]}

    def plan(self, flag, leg):
        from test_native_electrical import FAB, board, pad

        from pnr.native_electrical import Oracle, _pair_plan_order

        b = board()
        for ref, x in (("J", 3), ("U", 15)):
            pad(b, ref, "1", "p", (x, 5.2), (0.2, 0.2))
            pad(b, ref, "2", "n", (x, 4.8), (0.2, 0.2))
        zone = k.ZONE(b)
        zone.SetLayer(k.In1_Cu)
        zone.SetNetCode(b.FindNet("rail").GetNetCode())
        outline = zone.Outline()
        outline.NewOutline()
        for x, y in [(1, 1), (19, 1), (19, 19), (1, 19)]:
            outline.Append(round(x * 1e6), round(y * 1e6))
        b.Add(zone)
        k.ZONE_FILLER(b).Fill(b.Zones())
        b.BuildConnectivity()
        pair = dict(
            name="usb",
            p="p",
            n="n",
            width_mm=0.2,
            gap_mm=0.2,
            skew_mm=0.1,
            max_uncoupled_mm=2,
            terminal_chain=[{"p": "J.1", "n": "J.2"}, {"p": "U.1", "n": "U.2"}],
            reference_layer="In1.Cu",
        )
        r = dict(
            fab={
                "track_width_mm": 0.2,
                "clearance_mm": 0.15,
                "via_diameter_mm": 0.6,
                "via_drill_mm": 0.3,
            },
            electrical_fab=FAB,
            net_classes=[dict(name="ground", nets=["rail"], plane_layer="In1.Cu")],
        )
        seen = []

        def solve(p, n, terminals, *args, **kw):
            seen.append(kw)
            return dict(
                status="routed",
                paths={net: list(leg[net]) for net in (p, n)},
                lengths={net: plen(leg[net]) for net in (p, n)},
            )

        with env(PNR_PAIR_PER_RUN_UNCOUPLED="1" if flag else None), patch(
            "pnr.native_electrical.solve_pair", side_effect=solve
        ):
            return (
                _pair_plan_order(
                    b,
                    pair,
                    r,
                    Oracle(b, r, deadline=time.monotonic() + 30),
                    (1, 1, 19, 19),
                    0.2,
                    ("p", "n"),
                ),
                seen,
            )

    def test_final_measurement_rejects_a_run_above_the_cap(self):
        off, seen = self.plan(False, self.LONG)
        self.assertEqual(off["status"], "routed")
        self.assertNotIn("uncoupled_run_metrics", off)
        self.assertNotIn("max_uncoupled_head", seen[0])
        on, seen = self.plan(True, self.LONG)
        self.assertEqual(on["status"], "pair_uncoupled_run_limit")
        self.assertEqual(seen[0]["max_uncoupled_head"], 2)
        self.assertEqual(seen[0]["max_uncoupled_tail"], 2)
        # Literal reading: the pads sit at lane pitch (0.4), so the first and last
        # 40 um of each net are coupled copper and not part of the run.
        for net in ("p", "n"):
            self.assertAlmostEqual(
                on["uncoupled_run_metrics"][net]["max_mm"], plen(self.LONG[net]) - 0.08, places=5
            )
        with env(PNR_PAIR_RUN_MIN_COUPLED_MM=".4"):  # first src13 convention: those slivers merge
            merged, _ = self.plan(True, self.LONG)
        for net in ("p", "n"):
            self.assertAlmostEqual(
                merged["uncoupled_run_metrics"][net]["max_mm"], plen(self.LONG[net]), places=5
            )

    def test_a_coupled_route_passes_and_reports_zero_runs(self):
        on, _ = self.plan(True, self.TIGHT)
        self.assertEqual(on["status"], "routed")
        self.assertEqual(
            {net: m["max_mm"] for net, m in on["uncoupled_run_metrics"].items()},
            {"p": 0.0, "n": 0.0},
        )
        self.assertEqual(
            on["segments"][0]["uncoupled_budget_mm"], dict(head=2, tail=2, arriving=0.0)
        )
        self.assertEqual(on["segments"][0]["reference_trim_mm"], [2, 2])


@unittest.skipIf(k is None, "needs pcbnew (KiCad Python)")
class JointContactRunTest(unittest.TestCase):
    """Duplicate-contact connector J (A = declared origin, B = second contact) -> U,
    joint topology bridge_join_via, 'separate' scope; real branch routing, join
    port and final measurement, scripted stage-0 bridge (B.Cu lanes from the
    join vias, target vias next to U)."""

    PADS = {
        "J.A+": (3, 6),
        "J.A-": (3, 4),
        "J.B+": (5, 6),
        "J.B-": (5, 4),
        "U.+": (15, 5.175),
        "U.-": (15, 4.825),
    }

    def plan(self, flags):
        from test_native_electrical import FAB, board, pad

        from pnr.native_electrical import Oracle, _pair_plan_order

        b = board()
        for label, xy in self.PADS.items():
            ref, num = label.split(".")
            pad(b, ref, num, "p" if "+" in num else "n", xy, (0.2, 0.2))
        zone = k.ZONE(b)
        zone.SetLayer(k.In1_Cu)
        zone.SetNetCode(b.FindNet("rail").GetNetCode())
        outline = zone.Outline()
        outline.NewOutline()
        for x, y in [(1, 1), (19, 1), (19, 19), (1, 19)]:
            outline.Append(round(x * 1e6), round(y * 1e6))
        b.Add(zone)
        k.ZONE_FILLER(b).Fill(b.Zones())
        b.BuildConnectivity()
        pair = dict(
            name="usb",
            p="p",
            n="n",
            width_mm=W,
            gap_mm=G,
            skew_mm=5,
            max_uncoupled_mm=2,
            reference_layer="In1.Cu",
            terminal_chain=[{"p": "J.A+", "n": "J.A-"}, {"p": "U.+", "n": "U.-"}],
            auxiliary_pairs=[
                dict(
                    source={"p": "J.B+", "n": "J.B-"},
                    target={"p": "J.A+", "n": "J.A-"},
                    max_length_mm=3,
                )
            ],
        )
        r = dict(
            fab={
                "track_width_mm": 0.2,
                "clearance_mm": 0.15,
                "via_diameter_mm": 0.45,
                "via_drill_mm": 0.3,
            },
            electrical_fab=FAB,
            net_classes=[dict(name="ground", nets=["rail"], plane_layer="In1.Cu")],
        )
        topology = dict(
            bridge_hand=1,
            takeoff="bridge_join_via",
            auxiliary_order=("p", "n"),
            bridge_depth_mm=0.475,
            join_fraction=0.65,
            prefix_timing_target_mm=0.0,
            auxiliary_budget_scope="separate",
        )
        self.bridges = []

        def bridge(
            b, pair, terminals, rules, oracle, bounds, pitch, offsets, reuse_source=None, **kw
        ):
            self.bridges.append(dict(kw, reuse_source=reuse_source))
            src = reuse_source["sites"]
            vias = {"p": (14, 5.5), "n": (14, 4.5)}
            lanes = {
                "p": [
                    src["p"],
                    (src["p"][0] + (src["p"][1] - 5.175), 5.175),
                    (13.5, 5.175),
                    vias["p"],
                ],
                "n": [
                    src["n"],
                    (src["n"][0] + (4.825 - src["n"][1]), 4.825),
                    (13.5, 4.825),
                    vias["n"],
                ],
            }
            fan = {net: [vias[net], terminals[net][1]] for net in ("p", "n")}
            tracks = [
                (net, k.B_Cu, a, z, W)
                for net in ("p", "n")
                for a, z in zip(lanes[net], lanes[net][1:])
            ]
            tracks += [
                (net, k.F_Cu, a, z, W) for net in ("p", "n") for a, z in zip(fan[net], fan[net][1:])
            ]
            return dict(
                status="routed",
                pair_tracks=tracks,
                pair_vias=[(net, src[net]) for net in ("p", "n")]
                + [(net, vias[net]) for net in ("p", "n")],
                via_diameter_mm=0.45,
                via_drill_mm=0.3,
                lengths={net: 20.0 for net in ("p", "n")},
                reference_paths={
                    net: [(6, lanes[net][1][1]), (12, lanes[net][1][1])] for net in ("p", "n")
                },
                bridge_target=dict(
                    sites=vias,
                    paths={net: list(reversed(fan[net])) for net in ("p", "n")},
                    lengths={net: plen(fan[net]) for net in ("p", "n")},
                ),
            )

        with env(**{key: flags.get(key) for key in ("PNR_PAIR_PER_RUN_UNCOUPLED",)}), patch(
            "pnr.native_electrical.pair_layer_bridge", side_effect=bridge
        ):
            return _pair_plan_order(
                b,
                pair,
                r,
                Oracle(b, r, deadline=time.monotonic() + 60),
                (1, 1, 19, 19),
                0.15,
                ("p", "n"),
                topology,
            )

    def test_the_declared_contact_leg_to_the_join_is_measured_and_charged(self):
        from pnr.route.detail.coupled import uncoupled_runs

        result = self.plan({"PNR_PAIR_PER_RUN_UNCOUPLED": "1"})
        joint = result["joint_source_port"] if "joint_source_port" in result else None
        # The stage-0 bridge head is charged the declared contact's own leg to the
        # join (0.35 of the ~2.2 mm branch, never coupled: the mate's leg is 2 mm away).
        head = self.bridges[0]["head_prior_mm"]
        self.assertGreater(head, 0.7)
        self.assertLess(head, 1.0)
        # The final measurement starts at the declared pad and runs through the
        # leg, the join via and the B.Cu lead-in: over 2 mm, so rejected.
        self.assertEqual(result["status"], "pair_uncoupled_run_limit")
        for net, key in (("p", "J.A+"), ("n", "J.A-")):
            first = result["uncoupled_run_metrics"][net]["runs"][0]
            self.assertEqual(tuple(first["start"][1:]), self.PADS[key])
            self.assertGreater(first["length_mm"], head + 0.5)
        # Without the flag the same route is accepted (no run measurement at all).
        self.assertEqual(self.plan({})["status"], "routed")


# --------------------------------------------------------------------------- D scheduling
@unittest.skipIf(k is None, "needs pcbnew (KiCad Python)")
class EarlyExitTest(unittest.TestCase):
    def run_plan(self, flags, routes):
        from test_native_electrical import board
        from test_native_electrical import rules as power_rules

        from pnr.native_electrical import Oracle, pair_plan

        b = board()
        o = Oracle(b, power_rules(), deadline=time.monotonic() + 30)
        seen = []
        routes = iter(routes)
        self.limits = []

        def attempt(b, pair, r, trial, bounds, pitch, order, topology=None, limits=None):
            seen.append(topology)
            self.limits.append(limits)
            spec = next(routes, None)
            if spec is None:
                return dict(status="no_route")
            name, segments, vias, length, *delay = spec
            if delay:
                time.sleep(delay[0])
            if name is None:
                return dict(status="no_route")
            return dict(
                status="routed",
                name=name,
                segments=segments,
                pair_vias=[("p", (0, 0))] * vias,
                pair_tracks=[("p", k.F_Cu, (0, 0), (length, 0), 0.2)],
            )

        # Four joint configurations (both hands of two seeds) ahead of the ten legacy ones.
        joint = [
            dict(
                bridge_hand=h,
                takeoff="bridge_join_via",
                auxiliary_order=("p", "n"),
                join_fraction=0.65,
                prefix_timing_target_mm=t,
            )
            for t in (0.3, -0.3)
            for h in (1, -1)
        ]
        with env(
            **{
                key: flags.get(key)
                for key in (
                    "PNR_PAIR_EARLY_EXIT",
                    "PNR_PAIR_PREFER_INLINE",
                    "PNR_PAIR_JOINT_TOPOLOGIES",
                )
            }
        ), patch("pnr.native_electrical._pair_plan_order", side_effect=attempt), patch(
            "pnr.pair_joint.joint_topologies", return_value=joint
        ):
            result = pair_plan(
                b,
                {"auxiliary_pairs": [{"source": {}, "target": {}}]},
                power_rules(),
                o,
                (0, 0, 20, 20),
                0.2,
            )
        return result, seen

    def test_default_keeps_searching_every_configuration(self):
        result, seen = self.run_plan({}, [("a", [], 6, 2.0), ("b", [], 4, 3.0)])
        self.assertEqual(result["name"], "b")
        self.assertEqual(len(seen), len(result["order_attempts"]))
        self.assertEqual(len(seen), 10)
        self.assertNotIn("early_exit", result)

    def test_early_exit_returns_the_first_checked_route(self):
        result, seen = self.run_plan(
            {"PNR_PAIR_EARLY_EXIT": "1"}, [("a", [], 6, 2.0), ("b", [], 4, 3.0)]
        )
        self.assertEqual(result["name"], "a")
        self.assertEqual(len(seen), 1)
        self.assertEqual(result["early_exit"]["config_index"], 0)
        self.assertEqual(result["pair_engine_flags"], {"PNR_PAIR_EARLY_EXIT": True})

    JOINT = dict(bridge_target=dict(sites={}), fanout_lengths=[{"p": 0, "n": 0}, {"p": 1, "n": 1}])
    STUB = [JOINT, dict(paths={}, post_bridge_start={})]
    INLINE = [JOINT, dict(paths={})]

    def test_with_prefer_inline_a_stub_route_starts_a_bounded_inline_hunt(self):
        # The stub route took 0.3 s: the hunt may spend as long again, only on
        # in-line routes with at most its 4 vias, and stops at the first one.
        flags = {
            "PNR_PAIR_EARLY_EXIT": "1",
            "PNR_PAIR_PREFER_INLINE": "1",
            "PNR_PAIR_JOINT_TOPOLOGIES": "1",
        }
        result, seen = self.run_plan(
            flags,
            [
                ("stub", self.STUB, 4, 1.0, 0.3),
                ("inline", self.INLINE, 4, 1.5),
                ("late", self.INLINE, 2, 1.0),
            ],
        )
        self.assertEqual(result["name"], "inline")
        self.assertEqual(len(seen), 2)
        self.assertEqual(self.limits, [None, dict(stub_cap=0.0, max_vias=4)])
        self.assertEqual(result["early_exit"]["reason"], "inline_route")
        self.assertEqual(result["early_exit"]["config_index"], 1)
        self.assertEqual(result["order_attempts"][1]["inline_only"], dict(stub_cap=0.0, max_vias=4))

    def test_the_inline_hunt_stops_when_its_grace_is_spent(self):
        flags = {
            "PNR_PAIR_EARLY_EXIT": "1",
            "PNR_PAIR_PREFER_INLINE": "1",
            "PNR_PAIR_JOINT_TOPOLOGIES": "1",
        }
        # A hunt configuration that overruns the grace (0.2 s) is its last one.
        result, seen = self.run_plan(
            flags,
            [
                ("stub", self.STUB, 4, 1.0, 0.2),
                (None, None, 0, 0, 0.3),
                ("inline", self.INLINE, 4, 1.5),
            ],
        )
        self.assertEqual(result["name"], "stub")
        self.assertEqual(len(seen), 2)
        self.assertEqual(result["early_exit"]["reason"], "inline_grace")
        self.assertEqual(result["early_exit"]["inline_configs_tried"], 1)
        self.assertAlmostEqual(result["early_exit"]["grace_seconds"], 0.2, delta=0.1)

    def test_without_early_exit_prefer_inline_still_ranks_every_route(self):
        result, seen = self.run_plan(
            {"PNR_PAIR_PREFER_INLINE": "1"},
            [
                ("stub", self.STUB, 4, 1.0),
                ("inline", self.INLINE, 4, 1.5),
                ("late", self.INLINE, 2, 1.0),
            ],
        )
        self.assertEqual(result["name"], "late")
        self.assertEqual(len(seen), 10)
        self.assertEqual(set(self.limits), {None})
        # Early exit without the in-line preference: the first checked route, stub or not.
        result, seen = self.run_plan(
            {"PNR_PAIR_EARLY_EXIT": "1", "PNR_PAIR_JOINT_TOPOLOGIES": "1"},
            [("stub", self.STUB, 4, 1.0), ("inline", self.INLINE, 4, 1.5)],
        )
        self.assertEqual(result["name"], "stub")
        self.assertEqual(len(seen), 1)
        self.assertNotIn("reason", result["early_exit"])

    def test_the_hunt_never_runs_legacy_topologies(self):
        flags = {
            "PNR_PAIR_EARLY_EXIT": "1",
            "PNR_PAIR_PREFER_INLINE": "1",
            "PNR_PAIR_JOINT_TOPOLOGIES": "1",
        }
        nothing = (None, None, 0, 0)
        result, seen = self.run_plan(
            flags,
            [
                nothing,
                nothing,
                nothing,
                ("stub", self.STUB, 6, 1.0, 0.2),
                ("inline", self.INLINE, 4, 1.0),
            ],
        )
        self.assertEqual(result["name"], "stub")
        self.assertEqual(len(seen), 4)
        self.assertEqual(result["early_exit"]["reason"], "joint_configs_done")


@unittest.skipIf(k is None, "needs pcbnew (KiCad Python)")
class TimeoutTraceTest(unittest.TestCase):
    def run_plan(self, flags):
        from test_native_electrical import board
        from test_native_electrical import rules as power_rules

        from pnr.native_electrical import Oracle, pair_plan

        b = board()
        o = Oracle(b, power_rules(), deadline=time.monotonic() + 30)

        def attempt(b, pair, r, trial, bounds, pitch, order, topology=None):
            trial.progress = dict(stage=1, step="bridge")
            raise TimeoutError("scripted")

        with env(
            **{
                key: flags.get(key)
                for key in (
                    "PNR_PAIR_EARLY_EXIT",
                    "PNR_PAIR_PER_RUN_UNCOUPLED",
                    "PNR_PAIR_JOINT_TOPOLOGIES",
                )
            }
        ), patch("pnr.native_electrical._pair_plan_order", side_effect=attempt):
            return pair_plan(
                b,
                {"auxiliary_pairs": [{"source": {}, "target": {}}]},
                power_rules(),
                o,
                (0, 0, 20, 20),
                0.2,
            )

    def test_a_timed_out_attempt_records_where_it_was_only_under_src13_flags(self):
        on = self.run_plan({"PNR_PAIR_PER_RUN_UNCOUPLED": "1"})
        self.assertEqual(on["status"], "time_budget")
        self.assertEqual(on["order_attempts"][0]["progress"], dict(stage=1, step="bridge"))
        off = self.run_plan({})
        self.assertNotIn("progress", off)
        self.assertNotIn("progress", off["order_attempts"][0])


class DerivedHandSwapTest(unittest.TestCase):
    POS = {"J.B+": (1.5, 0), "J.A+": (0.5, 0), "J.B-": (0, 0), "J.A-": (1, 0)}
    PAIR = dict(
        p="p",
        n="n",
        gap_mm=0.15,
        skew_mm=0.3,
        terminal_chain=[{"p": "J.A+", "n": "J.A-"}, {"p": "U.+", "n": "U.-"}],
        auxiliary_pairs=[
            dict(
                source={"p": "J.B+", "n": "J.B-"},
                target={"p": "J.A+", "n": "J.A-"},
                max_length_mm=3,
            )
        ],
    )

    def test_first_joint_hand_follows_the_worker_order(self):
        from pnr.pair_joint import first_joint_hand, joint_topologies

        self.assertEqual(
            first_joint_hand(self.PAIR, self.POS),
            joint_topologies(self.PAIR, self.POS)[0]["bridge_hand"],
        )
        self.assertEqual(first_joint_hand(self.PAIR, self.POS, hand_first="-1"), -1)
        self.assertIsNone(first_joint_hand(dict(self.PAIR, auxiliary_pairs=[]), self.POS))

    def test_swap_trial_runs_the_opposite_of_the_derived_hand(self):
        from pnr.paired_bootstrap import swap_trial_hand, trial_schedule

        poses = [None, {"ref": "D", "rotation": 90}]
        inventory = {
            "graph": {
                "components": [
                    dict(
                        ref="J",
                        pos=[0, 0],
                        rot=0,
                        pads=[
                            dict(name=label.split(".")[1], offset=list(xy))
                            for label, xy in self.POS.items()
                        ],
                    )
                ]
            }
        }
        with env(
            PNR_PAIR_HAND_SWAP_TRIAL="1",
            PNR_PAIR_JOINT_TOPOLOGIES="1",
            PNR_PAIR_JOINT_HAND_FIRST="-1",
        ):
            hand = swap_trial_hand(self.PAIR, inventory)  # trial 0 runs without HAND_FIRST
            self.assertEqual(hand, 1)
            self.assertEqual(
                trial_schedule(poses, hand), [(None, None), (None, -1), (poses[1], None)]
            )
            self.assertEqual(trial_schedule(poses, -1), [(None, None), (None, 1), (poses[1], None)])
            self.assertEqual(
                trial_schedule(poses, None), [(p, None) for p in poses]
            )  # no joint search
            self.assertIsNone(swap_trial_hand(dict(self.PAIR, auxiliary_pairs=[]), inventory))
        with env(PNR_PAIR_HAND_SWAP_TRIAL=None, PNR_PAIR_JOINT_TOPOLOGIES="1"):
            self.assertIsNone(swap_trial_hand(self.PAIR, inventory))


# --------------------------------------------------------------------------- C placement
def module(ref="U", pos=(30, 30), rot=0.0, side="top"):
    """U6-like module: pair pads 13/14 (1.5 x 0.9, pitch 1.26) on its left edge."""
    from pnr.graph import Component, Pad

    pads = [
        Pad(name, net, (-8.75, y), (1.5, 0.9))
        for name, net, y in (
            ("12", "x", -5.71),
            ("13", "Dn", -6.99),
            ("14", "Dp", -8.25),
            ("15", "y", -9.51),
        )
    ]
    return Component(
        ref, "mod", pos, rot, side, (19.0, 31.75), (19.0, 31.75), pads=pads, smd_body=True
    )


def block_part(ref, pos, side, size=(4.0, 4.0), through=False):
    from pnr.graph import Component, Pad

    return Component(
        ref,
        "part",
        pos,
        0.0,
        side,
        size,
        size,
        pads=[Pad("1", "g", (0, 0), (1, 1), through)],
        smd_body=not through,
    )


RULES = dict(
    fab={
        "clearance_mm": 0.127,
        "via_diameter_mm": 0.45,
        "via_drill_mm": 0.3,
        "hole_to_hole_mm": 0.25,
        "via_to_smd_pad_mm": 0.127,
        "track_width_mm": 0.2,
    },
    diff_pairs=[
        dict(
            name="usb",
            p="Dp",
            n="Dn",
            width_mm=W,
            gap_mm=G,
            skew_mm=0.3,
            max_uncoupled_mm=2.0,
            terminal_chain=[{"p": "J.1", "n": "J.2"}, {"p": "U.14", "n": "U.13"}],
        )
    ],
)


@unittest.skipUnless(PLACE, "needs numpy/yaml/torch (PnR runtime)")
class LandingReserveTest(unittest.TestCase):
    def graph(self, *parts):
        from pnr.graph import BoardGraph

        return BoardGraph("t", list(parts), [])

    def rects(self, comp):
        from pnr.place.geometry import ReserveRect, placement_rects

        return [(s, r) for s, r in placement_rects(comp) if isinstance(r, ReserveRect)]

    def test_recipe_runs_are_the_exact_minimum_legal_via_runs(self):
        from pnr.place import pair_landing

        u = module()
        g = self.graph(u)
        self.assertEqual(pair_landing.attach(g, RULES), {"U": 1})
        recipe = u.reserves[0]
        self.assertEqual(
            (recipe["pads"], recipe["side"], recipe["pair"]), (["14", "13"], "opposite", "usb")
        )
        for run in recipe["runs_mm"].values():
            self.assertAlmostEqual(run, 0.75 + 0.225 + 0.127, delta=2e-4)
        self.assertAlmostEqual(recipe["half_width_mm"], 1.26 / 2 + 0.225 + 0.127, places=6)
        pair_landing.attach(g, RULES)
        self.assertEqual(len(u.reserves), 1)  # idempotent

    def test_recipe_uses_the_via_the_router_will_use(self):
        # Placement rules.json predates the fab profile (legacy 0.6 mm via, 0.15
        # clearance); the jlc-pofv router lands 0.45 mm vias at 0.127 clearance.
        from pnr.place import pair_landing

        legacy_fab = dict(
            RULES,
            fab={
                "track_width_mm": 0.2,
                "clearance_mm": 0.15,
                "via_diameter_mm": 0.6,
                "via_drill_mm": 0.3,
                "hole_clearance_mm": 0.2,
            },
        )
        with env(PNR_FAB_PROFILE=None):
            u = module()
            pair_landing.attach(self.graph(u), legacy_fab)
            profiled = u.reserves[0]
        self.assertAlmostEqual(profiled["via_radius_mm"], 0.45 / 2 + 0.127, places=6)
        for run in profiled["runs_mm"].values():
            self.assertAlmostEqual(run, 0.75 + 0.225 + 0.127, delta=2e-4)
        with env(PNR_FAB_PROFILE="legacy"):
            u = module()
            pair_landing.attach(self.graph(u), legacy_fab)
            legacy = u.reserves[0]
        self.assertAlmostEqual(legacy["via_radius_mm"], 0.6 / 2 + 0.15, places=6)
        # Rules that already carry a profile marker are taken as they are.
        marked = dict(legacy_fab, fab_profile="jlc-pofv")
        with env(PNR_FAB_PROFILE=None):
            u = module()
            pair_landing.attach(self.graph(u), marked)
        self.assertAlmostEqual(u.reserves[0]["via_radius_mm"], 0.6 / 2 + 0.15, places=6)

    def test_block_macro_counts_where_its_members_are_mounted(self):
        from pnr.graph import Component
        from pnr.place import pair_landing
        from pnr.place.metrics import overlap_pairs

        outward = (30 - 8.75 - 1.1, 30 - 7.62)

        def macro(sides):
            m = Component(
                "MB00",
                "block:blk",
                outward,
                0.0,
                "top",
                (0.6, 0.6),
                (0.6, 0.6),
                pads=[],
                smd_body=True,
            )
            if sides is not None:
                m.reserves = [dict(kind=pair_landing.MOUNT, sides=sides)]
            return m

        with env(PNR_PAIR_LANDING_RESERVE="1"):
            for sides, want in (
                (["top"], []),
                (["bottom"], [("U", "MB00")]),
                (["bottom", "top"], [("U", "MB00")]),
                (None, [("U", "MB00")]),
            ):
                g = self.graph(module(), macro(sides))
                pair_landing.attach(g, RULES)
                self.assertEqual(overlap_pairs(g), want, sides)
                self.assertEqual(
                    pair_landing.reserve_rects(g.component("MB00")), []
                )  # a mount record is not a reserve

    def test_flag_off_placement_rects_are_unchanged(self):
        from pnr.place import pair_landing
        from pnr.place.geometry import Rect, placement_rects

        u = module()
        before = placement_rects(u)
        pair_landing.attach(self.graph(u), RULES)
        with env(PNR_PAIR_LANDING_RESERVE=None):
            after = placement_rects(u)
        self.assertEqual(after, before)
        self.assertTrue(all(type(r) is Rect for _, r in after))

    def test_reserves_sit_opposite_next_to_the_pads_and_follow_rotation(self):
        from pnr.place import pair_landing
        from pnr.place.geometry import pin_positions

        with env(PNR_PAIR_LANDING_RESERVE="1"):
            for rot in (0.0, 90.0, 180.0, 270.0):
                u = module(rot=rot)
                pair_landing.attach(self.graph(u), RULES)
                reserves = self.rects(u)
                pins = dict(pin_positions(u))
                self.assertEqual(
                    sorted(r.label for _, r in reserves), ["usb:14/13:+", "usb:14/13:-"]
                )
                self.assertTrue(all(s == "bottom" for s, _ in reserves))
                mid = ((pins["13"][0] + pins["14"][0]) / 2, (pins["13"][1] + pins["14"][1]) / 2)
                for (
                    _,
                    r,
                ) in reserves:  # each zone touches the pad-pair midline and reaches run+radius
                    self.assertLessEqual(r.left - 1e-6, mid[0])
                    self.assertGreaterEqual(r.right + 1e-6, mid[0])
                    self.assertLessEqual(r.bottom - 1e-6, mid[1])
                    self.assertGreaterEqual(r.top + 1e-6, mid[1])
                    self.assertAlmostEqual(
                        min(r.w, r.h), 1.102 + 0.352, delta=2e-3
                    )  # midline -> far via clearance
                    self.assertAlmostEqual(
                        max(r.w, r.h), 1.26 + 2 * 0.352, delta=2e-3
                    )  # both vias + clearance

    def test_only_parts_mounted_on_the_reserve_side_conflict(self):
        from pnr.constraints import compile_constraints
        from pnr.place import pair_landing
        from pnr.place.metrics import hard_violations, overlap_pairs

        outward = (
            30 - 8.75 - 1.1,
            30 - 7.62,
        )  # outward via pair of U, left of its courtyard (x >= 20.5)
        with env(PNR_PAIR_LANDING_RESERVE="1"):
            tp = block_part("TP", outward, "bottom", size=(0.6, 0.6))
            g = self.graph(module(), tp)
            pair_landing.attach(g, RULES)
            self.assertEqual(overlap_pairs(g), [("U", "TP")])
            cc = compile_constraints({"board": {"outline": {"w": 60, "h": 60}}}, g.refs)
            self.assertEqual(hard_violations(g, cc)["overlaps"], [("U", "TP")])
            with env(PNR_PAIR_LANDING_RESERVE=None):
                self.assertEqual(overlap_pairs(g), [])
            # A top-mounted through-hole part only occupies the bottom with its pins.
            tht = block_part("J", outward, "top", size=(0.6, 0.6), through=True)
            g = self.graph(module(), tht)
            pair_landing.attach(g, RULES)
            self.assertEqual(overlap_pairs(g), [])

    def test_reserve_rect_semantics(self):
        from pnr.place.geometry import MountedRect, Rect, ReserveRect

        zone = ReserveRect(0, 0, 2, 2, side="bottom", owner="U")
        self.assertFalse(
            zone.overlaps(ReserveRect(0.5, 0.5, 2, 2, side="bottom", owner="V"))
        )  # reserves never exclude each other
        self.assertTrue(zone.overlaps(MountedRect(1, 1, 1, 1, mount="bottom")))
        self.assertTrue(MountedRect(1, 1, 1, 1, mount="bottom").overlaps(zone))
        self.assertTrue(zone.overlaps(MountedRect(1, 1, 1, 1, mount="both")))
        self.assertFalse(zone.overlaps(MountedRect(1, 1, 1, 1, mount="top")))
        self.assertTrue(
            zone.overlaps(Rect(1, 1, 1, 1)) and Rect(1, 1, 1, 1).overlaps(zone)
        )  # unknown owner: conservative
        self.assertFalse(zone.overlaps(MountedRect(3, 3, 1, 1, mount="bottom")))
        # 0.6 mm apart: overlap needs gap > 0.6 (each rect grows by gap/2).
        self.assertFalse(zone.overlaps(MountedRect(2.1, 0, 1, 1, mount="bottom"), gap=0.5))
        self.assertTrue(zone.overlaps(MountedRect(2.1, 0, 1, 1, mount="bottom"), gap=0.7))
        self.assertTrue(MountedRect(2.1, 0, 1, 1, mount="bottom").overlaps(zone, gap=0.7))

    def test_json_round_trip_keeps_reserves_and_omits_empty_ones(self):
        from pnr.graph import BoardGraph
        from pnr.place import pair_landing

        g = self.graph(module(), block_part("TP", (5, 5), "bottom"))
        plain = g.to_json()
        self.assertNotIn("reserves", plain)
        pair_landing.attach(g, RULES)
        text = g.to_json()
        back = BoardGraph.from_json(text)
        self.assertEqual(back.component("U").reserves, g.component("U").reserves)
        self.assertEqual(back.to_json(), text)
        self.assertEqual(back.component("TP").reserves, [])

    def test_side_flip_mirrors_the_reserve_with_the_pads(self):
        from pnr.place import pair_landing
        from pnr.place.geometry import pin_positions, set_component_side

        with env(PNR_PAIR_LANDING_RESERVE="1"):
            u = module()
            pair_landing.attach(self.graph(u), RULES)
            set_component_side(u, "bottom")
            pins = dict(pin_positions(u))
            reserves = self.rects(u)
            self.assertTrue(all(s == "top" for s, _ in reserves))
            mid_y = (pins["13"][1] + pins["14"][1]) / 2
            self.assertTrue(all(r.bottom - 1e-6 <= mid_y <= r.top + 1e-6 for _, r in reserves))

    def test_legalize_moves_an_opposite_side_part_off_the_landing(self):
        from pnr.place import pair_landing
        from pnr.place.metrics import overlap_pairs

        u = module(pos=(30, 25))
        tp = block_part("TP", (30 - 8.75 + 1.3, 25 - 7.62), "bottom", size=(3.0, 3.0))
        g = self.graph(u, tp)
        pair_landing.attach(g, RULES)
        with env(PNR_PAIR_LANDING_RESERVE=None):
            plain = legalize(
                g, 60, 50, fixed={"U": (30, 25)}, keepouts=[], clearance=0.2, grid_mm=0.25
            )
        self.assertLess(math.dist(plain.component("TP").pos, tp.pos), 0.5)  # sits on the landing
        with env(PNR_PAIR_LANDING_RESERVE="1"):
            self.assertEqual(overlap_pairs(plain), [("U", "TP")])
            placed = legalize(
                g, 60, 50, fixed={"U": (30, 25)}, keepouts=[], clearance=0.2, grid_mm=0.25
            )
            self.assertEqual(overlap_pairs(placed), [])
            self.assertEqual(placed.component("U").reserves, u.reserves)
        self.assertLess(
            math.dist(placed.component("TP").pos, tp.pos), 5
        )  # moved only off the landing

    def test_legalize_keeps_a_movable_terminal_part_landing_clear_of_a_fixed_bottom_part(self):
        from pnr.place import pair_landing
        from pnr.place.metrics import overlap_pairs

        tp = block_part("TP", (30 - 8.75 + 1.3, 25 - 7.62), "bottom", size=(3.0, 3.0))
        u = module(pos=(30, 25))
        g = self.graph(u, tp)
        pair_landing.attach(g, RULES)
        with env(PNR_PAIR_LANDING_RESERVE="1"):
            placed = legalize(
                g, 70, 60, fixed={"TP": tp.pos}, keepouts=[], clearance=0.2, grid_mm=0.25
            )
            self.assertEqual(overlap_pairs(placed), [])

    def test_macro_collapse_carries_the_member_reserve(self):
        from pnr.constraints import compile_constraints
        from pnr.graph import BoardGraph, Net
        from pnr.hier.macro import collapse
        from pnr.place import pair_landing

        u = module(pos=(12, 18))
        tp = block_part("TP", (50, 40), "bottom")
        flat = BoardGraph(
            "flat", [u, tp], [Net("Dp", 1, [("U", "14")]), Net("Dn", 2, [("U", "13")])]
        )
        pair_landing.attach(flat, RULES)
        sub = BoardGraph("blk", [module(pos=(12, 18))], [])
        pair_landing.attach(sub, RULES)
        cc = compile_constraints({"board": {"outline": {"w": 80, "h": 60}}}, flat.refs)
        mgraph, mcon, mrules, plan = collapse(
            flat, cc, RULES, [(SimpleNamespace(name="blk"), sub, 24.0, 36.0)]
        )
        macro = mgraph.component("MB00")
        self.assertEqual([r["pads"] for r in macro.reserves], [["U.14", "U.13"]])
        self.assertEqual(macro.reserves[0]["side"], "opposite")
        with env(PNR_PAIR_LANDING_RESERVE="1"):  # the flag also records the members' mount sides
            flagged = collapse(flat, cc, RULES, [(SimpleNamespace(name="blk"), sub, 24.0, 36.0)])[
                0
            ].component("MB00")
        self.assertEqual(flagged.reserves[-1], dict(kind=pair_landing.MOUNT, sides=["top"]))
        self.assertEqual(pair_landing.macro_mount(flagged), "top")
        self.assertEqual(pair_landing.macro_mount(macro), "both")
        with env(PNR_PAIR_LANDING_RESERVE="1"):
            macro.pos = (40, 30)
            expanded = plan.expand(mgraph, flat)
            member = expanded.component("U")
            self.assertEqual(member.reserves, u.reserves)
            got = sorted(
                (s, round(r.cx, 6), round(r.cy, 6), round(r.w, 6), round(r.h, 6))
                for s, r in self.rects(macro)
            )
            want = sorted(
                (s, round(r.cx, 6), round(r.cy, 6), round(r.w, 6), round(r.h, 6))
                for s, r in self.rects(member)
            )
            self.assertEqual(got, want)

    def test_relax_drops_only_the_reserves_an_existing_placement_violates(self):
        from pnr.constraints import compile_constraints
        from pnr.graph import Component, Pad
        from pnr.place import pair_landing
        from pnr.place.metrics import hard_violations

        u = module(pos=(30, 25))
        tp = block_part("TP", (30 - 8.75 + 1.3, 25 - 7.62), "bottom", size=(3.0, 3.0))
        j = Component(
            "J",
            "conn",
            (10, 40),
            0.0,
            "top",
            (3.0, 2.0),
            (3.0, 2.0),
            pads=[Pad("1", "Dp", (-0.5, 0), (0.3, 1.0)), Pad("2", "Dn", (0.5, 0), (0.3, 1.0))],
            smd_body=True,
        )
        g = self.graph(u, tp, j)
        self.assertEqual(pair_landing.attach(g, RULES), {"U": 1, "J": 1})
        cc = compile_constraints({"board": {"outline": {"w": 60, "h": 50}}}, g.refs)
        with env(PNR_PAIR_LANDING_RESERVE="1"):
            self.assertEqual(hard_violations(g, cc)["overlaps"], [("U", "TP")])
            self.assertEqual(pair_landing.relax_violated(g, lambda: hard_violations(g, cc)), ["U"])
            self.assertEqual(u.reserves, [])
            self.assertEqual(len(j.reserves), 1)  # J's landing stays enforced
            self.assertFalse(any(hard_violations(g, cc).values()))
            self.assertEqual(pair_landing.relax_violated(g, lambda: hard_violations(g, cc)), [])

    def test_refine_channels_keeps_a_terminal_part_landing_clear(self):
        from pnr.place import pair_landing
        from pnr.place.legalize import refine_channels
        from pnr.place.metrics import overlap_pairs

        u = module(pos=(30, 25))
        tp = block_part(
            "TP", (18.0, 25 - 7.62), "bottom", size=(3.0, 3.0)
        )  # 0.3 mm left of U's outward landing
        g = self.graph(u, tp)
        pair_landing.attach(g, RULES)

        class Pull:  # channel pressure that only a move of U to the left relieves
            def penalty(self, comp, others, xs, ys):
                import numpy as np

                return (
                    np.maximum(np.asarray(xs, dtype=float) - 27.0, 0.0)
                    if comp.ref == "U"
                    else np.zeros(np.shape(xs))
                )

        with env(PNR_PAIR_LANDING_RESERVE="1"):
            self.assertEqual(overlap_pairs(g), [])
            moved = refine_channels(
                g,
                60,
                50,
                fixed={"TP"},
                keepouts=[],
                channel_model=Pull(),
                max_move_mm=3.0,
                clearance=0.2,
            )
            self.assertEqual(overlap_pairs(moved), [])
        with env(PNR_PAIR_LANDING_RESERVE=None):
            free = refine_channels(
                g,
                60,
                50,
                fixed={"TP"},
                keepouts=[],
                channel_model=Pull(),
                max_move_mm=3.0,
                clearance=0.2,
            )
        with env(PNR_PAIR_LANDING_RESERVE="1"):
            self.assertEqual(
                overlap_pairs(free), [("U", "TP")]
            )  # without the flag the move lands on TP

    def test_placer_attaches_reserves_only_under_the_flag(self):
        from pnr.constraints import compile_constraints
        from pnr.place.placer import place

        g = self.graph(
            module(pos=(30, 25)), block_part("TP", (22.55, 17.38), "bottom", size=(3.0, 3.0))
        )
        cc = compile_constraints(
            {
                "board": {"outline": {"w": 60, "h": 50}},
                "fixed": {"U": {"at": [30, 25]}, "TP": {"side": "bottom"}},
            },
            g.refs,
        )
        with env(PNR_PAIR_LANDING_RESERVE=None):
            placed, report = place(
                g, cc, iters=5, orient=False, channel_rules=dict(RULES, net_classes=[])
            )
        self.assertEqual(placed.component("U").reserves, [])
        self.assertNotIn("reserves", placed.to_json())
        with env(PNR_PAIR_LANDING_RESERVE="1"):
            placed, report = place(
                g, cc, iters=5, orient=False, channel_rules=dict(RULES, net_classes=[])
            )
            self.assertTrue(report.legal, report.summary())
            self.assertEqual(len(placed.component("U").reserves), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
