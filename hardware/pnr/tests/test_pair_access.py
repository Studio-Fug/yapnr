import math
import os
import unittest
from unittest.mock import patch

from tests.test_pair_route import plain_board, plain_rules

from pnr import length_model as lm
from pnr.native_pair_audit import measure_paths
from pnr.route.detail import pair_access
from pnr.route.detail.pair_route import PairRouter, Terminal
from pnr.route.detail.router import route_board


class PairAccessTest(unittest.TestCase):
    def test_whole_run_delay_uses_access_layer_and_via_barrel(self):
        router = PairRouter.__new__(PairRouter)
        router.st = lm.default_stackup(4)
        router.st["planes"] = ["In1.Cu", "In2.Cu"]
        router.st["layers"][-2]["er"] = 9
        router.delay, router.pads, router.frame, router.via_radius = None, [], None, 0.3
        p = Terminal(
            "P",
            "U",
            "1",
            (5, 0),
            ("B.Cu",),
            tracks=[("F.Cu", (0, 0), (5, 0), 0.25)],
            vias=[(5, 0, 0.3)],
        )
        n = Terminal("N", "U", "2", (5, 1), ("B.Cu",))
        tail = (
            Terminal("P", "J", "1", (15, 0), ("B.Cu",)),
            Terminal("N", "J", "2", (15, 1), ("B.Cu",)),
        )
        got = router.measure("P", [(5, 0), (15, 0)], "B.Cu", 0.25, [(p, n), tail], timing=True)
        delay = router.delay
        expected = 5 * delay.track("F.Cu", 0.25) + 10 * delay.track("B.Cu", 0.25)
        expected += lm.layer_distance(router.st, "F.Cu", "B.Cu") * delay.barrel_ps_per_mm
        self.assertAlmostEqual(got, expected, places=6)
        self.assertNotAlmostEqual(delay.track("F.Cu", 0.25), delay.track("B.Cu", 0.25))

    def test_equal_lengths_on_separate_layers_fail_coupling(self):
        paths = {"P": [("F.Cu", (0, 0), (10, 0))], "N": [("B.Cu", (0, 0.4), (10, 0.4))]}
        self.assertFalse(measure_paths(paths, 0.2, 0.2, 2)["passed"])

    def test_coupled_run_and_two_bounded_accesses_are_measured_together(self):
        paths = {
            net: [
                ("F.Cu", (0, y), (1, y)),
                ("B.Cu", (1, side), (9, side)),
                ("F.Cu", (9, y), (10, y)),
            ]
            for net, y, side in (("P", 1, 0.2), ("N", -1, -0.2))
        }
        result = measure_paths(paths, 0.2, 0.2, 2)
        self.assertTrue(result["passed"])
        self.assertEqual(result["uncoupled_mm"], {"P": 2, "N": 2})

    def test_rotated_access_and_identity(self):
        rows = pair_access.ports(
            {"P": (1, 0), "N": (-1, 0)},
            0.2,
            0.2,
            0.6,
            0.3,
            0.2,
            0.2,
            2,
            lambda *args: True,
            lambda net, q: q[1] > 0.7 if net == "P" else q[1] < 0.1,
        )
        self.assertTrue(rows)
        self.assertTrue(any(row["angle_deg"] == 90 for row in rows))
        for row in rows:
            self.assertEqual(row["paths"]["P"][0], (1, 0))
            self.assertEqual(row["paths"]["N"][0], (-1, 0))
            self.assertGreaterEqual(math.dist(*row["sites"].values()), 0.8)
            self.assertTrue(all(v < 2 for v in row["lengths"].values()))

    def test_blocked_vias_produce_no_partial_access(self):
        self.assertEqual(
            pair_access.ports(
                {"P": (1, 0), "N": (-1, 0)},
                0.2,
                0.2,
                0.6,
                0.3,
                0.2,
                0.2,
                2,
                lambda *args: True,
                lambda net, q: net == "P",
            ),
            [],
        )

    def test_front_obstacle_uses_matched_via_pairs_and_back_trunk(self):
        g = plain_board()
        c, rules = plain_rules(g, pair={"max_uncoupled_mm": 3.0})
        with patch.dict(os.environ, PNR_PAIR_ACCESS="1", PNR_PAIR_ACCESS_SECONDS="8"):
            board = route_board(
                g,
                c,
                rules,
                pitch=0.25,
                max_iters=2,
                fixed_copper={
                    "frame": "engine-mm-y-up",
                    "tracks": [["OBS", "F.Cu", [11, 0], [11, 20], 1.0]],
                    "vias": [],
                },
            )
        row = board.escape_diagnostics["coupled_pairs"]["pairs"]["usb"]
        self.assertEqual(row["status"], "coupled")
        self.assertEqual(row["start"], "paired_access")
        self.assertEqual(row["layer"], "B.Cu")
        self.assertEqual([v[0] for v in row["paired_vias"]].count("DP"), 2)
        self.assertEqual([v[0] for v in row["paired_vias"]].count("DN"), 2)
        self.assertLessEqual(row["skew_mm"], row["skew_budget_mm"])
        self.assertTrue(all(v <= 3 for v in row["uncoupled_mm"].values()))
        self.assertTrue(all(net not in board.result.unrouted for net in ("DP", "DN")))
        self.assertTrue(any(t[0] == "DP" and t[1] == "F.Cu" for t in board.tracks))
        self.assertTrue(any(t[0] == "DN" and t[1] == "F.Cu" for t in board.tracks))

    def test_no_joint_channel_is_not_accepted_as_independent_legs(self):
        g = plain_board()
        c, rules = plain_rules(g)
        with patch.dict(os.environ, PNR_PAIR_ACCESS="1", PNR_PAIR_ACCESS_SECONDS="2"):
            board = route_board(
                g,
                c,
                rules,
                pitch=0.25,
                max_iters=2,
                fixed_copper={
                    "frame": "engine-mm-y-up",
                    "tracks": [
                        ["OBS", layer, [11, 0], [11, 20], 1.0] for layer in ("F.Cu", "B.Cu")
                    ],
                    "vias": [],
                },
            )
        self.assertNotEqual(
            board.escape_diagnostics["coupled_pairs"]["pairs"]["usb"]["status"], "coupled"
        )
        self.assertTrue({"DP", "DN"} <= set(board.result.unrouted))


if __name__ == "__main__":
    unittest.main()
