import math
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import pcbnew as k
from pnr.native_electrical import (
    Oracle,
    bridge_half_plane,
    connector_origins_qualified,
    duplicate_endpoint_metrics,
    pair_plan,
    pair_topologies,
    vec,
)
from test_native_electrical import board, rules


class PairTopologyTest(unittest.TestCase):
    def pair(self):
        return dict(
            name="usb",
            p="p",
            n="n",
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

    def test_both_embeddings_and_four_literal_takeoffs(self):
        pair = self.pair()
        before = repr(pair)
        choices = pair_topologies(pair)
        self.assertEqual(
            {(c["bridge_hand"], c["takeoff"]) for c in choices if c["bridge_hand"]},
            {
                (h, t)
                for h in (-1, 1)
                for t in (
                    "declared",
                    "auxiliary_source",
                    "declared_p_source_n",
                    "source_p_declared_n",
                )
            },
        )
        self.assertEqual(
            [c["auxiliary_order"] for c in choices if not c["bridge_hand"]],
            [("p", "n"), ("n", "p")],
        )
        self.assertEqual(repr(pair), before)

    def test_half_planes_rigid_transform_and_symmetry(self):
        for angle in (0, 0.31, math.pi / 2, math.pi):
            transform = lambda p: (
                7 + p[0] * math.cos(angle) - p[1] * math.sin(angle),
                -3 + p[0] * math.sin(angle) + p[1] * math.cos(angle),
            )
            for hand in (-1, 1):
                side = bridge_half_plane(transform((0, 0)), transform((2, 0)), hand)
                self.assertTrue(side(transform((1, hand))))
                self.assertFalse(side(transform((1, -hand))))
                self.assertTrue(side(transform((1, 0))))
        with self.assertRaises(ValueError):
            bridge_half_plane((0, 0), (1, 0), 2)

    def test_shorter_complete_candidate_beats_first_success_without_leaking_copper(self):
        b = board()
        r = rules()
        oracle = Oracle(b, r, deadline=time.monotonic() + 2)
        deadline = oracle.deadline
        seen = []

        def attempt(b, pair, r, o, bounds, pitch, order, topology=None):
            self.assertTrue(o.clear("rail", k.F_Cu, (5, 7), (5, 9), 0.2))
            o.reserve_track("other", k.F_Cu, (3, 8), (8, 8), 0.2)
            seen.append(topology)
            distance = (
                1
                if topology and topology["bridge_hand"] == 1 and topology["takeoff"] == "declared"
                else 8
            )
            return dict(
                status="routed",
                pair_tracks=[("p", k.F_Cu, (1, 1), (1 + distance, 1), 0.2)],
                pair_vias=[],
                topology=topology,
            )

        with patch("pnr.native_electrical._pair_plan_order", side_effect=attempt):
            result = pair_plan(b, self.pair(), r, oracle, (0, 0, 20, 20), 0.15)
        self.assertEqual(len(seen), 10)
        self.assertEqual(result["topology"]["takeoff"], "declared")
        self.assertEqual(result["topology"]["bridge_hand"], 1)
        self.assertEqual(oracle.deadline, deadline)

    def test_all_failures_leave_parent_unmodified(self):
        b = board()
        r = rules()
        oracle = Oracle(b, r, deadline=time.monotonic() + 2)

        def attempt(b, pair, r, o, bounds, pitch, order, topology=None):
            o.reserve_track("other", k.F_Cu, (3, 8), (8, 8), 0.2)
            return dict(status="no_route")

        with patch("pnr.native_electrical._pair_plan_order", side_effect=attempt):
            result = pair_plan(b, self.pair(), r, oracle, (0, 0, 20, 20), 0.15)
        self.assertEqual(result["status"], "no_route")
        self.assertTrue(oracle.clear("rail", k.F_Cu, (5, 7), (5, 9), 0.2))

    def test_duplicate_timing_catches_interior_join_hidden_by_equal_auxiliary_lengths(self):
        pair = self.pair()
        positions = {
            "J.A+": (0, 0),
            "J.B+": (2, 0),
            "U.+": (0.5, 5),
            "J.A-": (0, 2),
            "J.B-": (2, 2),
            "U.-": (2, 5.5),
        }
        pads = {
            label: SimpleNamespace(GetPosition=lambda p=p: vec(p)) for label, p in positions.items()
        }
        tracks = [
            ("p", k.F_Cu, (0, 0), (2, 0), 0.2),
            ("p", k.F_Cu, (2, 0), (0.5, 0), 0.2),
            ("p", k.F_Cu, (0.5, 0), (0.5, 5), 0.2),
            ("n", k.F_Cu, (0, 2), (2, 2), 0.2),
            ("n", k.F_Cu, (2, 2), (2, 5.5), 0.2),
        ]
        metrics = duplicate_endpoint_metrics(pair, pads, tracks, [], 1.6)
        declared = metrics["J.A+/J.A-"]
        self.assertAlmostEqual(declared["p"]["length_mm"], declared["n"]["length_mm"])
        self.assertFalse(connector_origins_qualified(pair, metrics))
        self.assertAlmostEqual(
            abs(metrics["J.B+/J.B-"]["p"]["length_mm"] - metrics["J.B+/J.B-"]["n"]["length_mm"]), 3
        )

    def test_disconnected_or_ambiguous_timing_fails_closed(self):
        self.assertFalse(
            connector_origins_qualified(
                self.pair(), {"test": {"p": {"valid": False}, "n": {"valid": False}}}
            )
        )
        self.assertFalse(connector_origins_qualified(self.pair(), {}))


if __name__ == "__main__":
    unittest.main()
