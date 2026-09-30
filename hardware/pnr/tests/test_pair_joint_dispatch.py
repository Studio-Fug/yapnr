import copy
import math
import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from test_native_electrical import board, pad, rules

from pnr.native_electrical import Oracle, pair_plan
from pnr.pair_joint import branch_join_port, joint_topologies


class PairJointDispatchTest(unittest.TestCase):
    def pair(self):
        return dict(
            p="p",
            n="n",
            gap_mm=0.15,
            skew_mm=0.3,
            terminal_chain=[
                {"p": "J.A+", "n": "J.A-"},
                {"p": "D.+", "n": "D.-"},
                {"p": "U.+", "n": "U.-"},
            ],
            auxiliary_pairs=[
                dict(
                    source={"p": "J.B+", "n": "J.B-"},
                    target={"p": "J.A+", "n": "J.A-"},
                    max_length_mm=3,
                )
            ],
        )

    def positions(self):
        return {"J.B+": (1.5, 0), "J.A+": (0.5, 0), "J.B-": (0, 0), "J.A-": (1, 0)}

    def make_board(self):
        b = board()
        for label, pt in self.positions().items():
            ref, num = label.rsplit(".", 1)
            pad(b, ref, num, "p" if "+" in num else "n", pt, (0.2, 0.2))
        return b

    def test_default_is_three_targets_times_both_hands_and_preserves_polarity(self):
        p = self.pair()
        before = copy.deepcopy(p)
        choices = joint_topologies(p, self.positions())
        self.assertEqual(len(choices), 6)
        self.assertEqual(
            {(q["bridge_hand"], q["prefix_timing_target_mm"]) for q in choices},
            {(h, t) for h in (-1, 1) for t in (-0.3, 0.0, 0.3)},
        )
        self.assertTrue(all(q["auxiliary_budget_scope"] == "separate" for q in choices))
        self.assertEqual(p, before)
        self.assertAlmostEqual(choices[0]["bridge_depth_mm"], 0.95)
        self.assertEqual(choices[0]["join_fraction"], 0.65)

    def test_expanded_choices_cover_contact_takeoffs(self):
        c = joint_topologies(self.pair(), self.positions(), max_trials=18)
        self.assertEqual(len(c), 18)
        self.assertEqual({q["join_fraction"] for q in c}, {0.65, 0.35, 0.5})

    def test_geometry_seeds_rigid_transform_invariant_and_source_scaled(self):
        for angle in (0.31, math.pi / 2, math.pi):
            ps = {
                k: (
                    7 + x * math.cos(angle) - y * math.sin(angle),
                    -3 + x * math.sin(angle) + y * math.cos(angle),
                )
                for k, (x, y) in self.positions().items()
            }
            self.assertAlmostEqual(joint_topologies(self.pair(), ps)[0]["bridge_depth_mm"], 0.95)
        p = self.pair()
        p["auxiliary_pairs"][0]["max_length_mm"] = 2
        self.assertAlmostEqual(joint_topologies(p, self.positions())[0]["bridge_depth_mm"], 0.475)

    def test_missing_annotation_bad_geometry_or_ambiguous_group_fails_closed(self):
        p = self.pair()
        p["auxiliary_pairs"] = []
        self.assertEqual(joint_topologies(p, self.positions()), [])
        p = self.pair()
        p["auxiliary_pairs"] *= 2
        self.assertEqual(joint_topologies(p, self.positions()), [])
        self.assertEqual(joint_topologies(self.pair(), {}), [])
        p = self.pair()
        p["auxiliary_pairs"][0]["max_length_mm"] = 0.9
        self.assertEqual(joint_topologies(p, self.positions()), [])
        p = self.pair()
        p["skew_mm"] = float("nan")
        self.assertEqual(joint_topologies(p, self.positions()), [])
        for n in (0, 19, True, 1.5):
            with self.assertRaises(ValueError):
                joint_topologies(self.pair(), self.positions(), max_trials=n)
        with self.assertRaises(ValueError):
            joint_topologies(self.pair(), self.positions(), budget_scope="guess")

    def test_separate_scope_never_hides_actual_contact_timing(self):
        p = self.pair()
        p["max_uncoupled_mm"] = 2
        paths = {"p": [(0, 0, 0), (1, 0, 0), (1, 1, 0)], "n": [(3, 0, 0), (4, 0, 0), (4, 1, 0)]}
        oracle = SimpleNamespace(via=lambda *args: True)
        r = dict(fab=dict(via_diameter_mm=0.6, via_drill_mm=0.3, clearance_mm=0.15))
        separate = branch_join_port(p, paths, oracle, r, 0.25, budget_scope="separate")
        combined = branch_join_port(p, paths, oracle, r, 0.25, budget_scope="combined")
        self.assertEqual(separate["port"]["entry_budget_mm"], 0)
        self.assertEqual(combined["port"]["entry_budget_mm"], 1.5)
        self.assertEqual(separate["declared_offsets"], combined["declared_offsets"])
        self.assertEqual(separate["alternate_offsets"], combined["alternate_offsets"])
        self.assertEqual(separate["port"]["auxiliary_contact_length_mm"], 1.5)
        self.assertEqual(separate["port"]["auxiliary_budget_scope"], "separate")
        with self.assertRaises(ValueError):
            branch_join_port(p, paths, oracle, r, budget_scope="typo")

    def test_opt_in_dispatch_gives_joint_search_real_budget_and_restores_deadline(self):
        b = self.make_board()
        r = rules()
        o = Oracle(b, r, deadline=time.monotonic() + 180)
        deadline = o.deadline
        seen = []

        def attempt(b, p, r, trial, bounds, pitch, order, topology=None):
            seen.append((topology, trial.deadline - time.monotonic()))
            return dict(status="no_route")

        with patch.dict(
            os.environ,
            {
                "PNR_PAIR_JOINT_TOPOLOGIES": "1",
                "PNR_PAIR_JOINT_TRIAL_SECONDS": "90",
                "PNR_PAIR_JOINT_MAX_TRIALS": "6",
                "PNR_PAIR_AUXILIARY_SCOPE": "separate",
            },
        ), patch("pnr.native_electrical._pair_plan_order", side_effect=attempt):
            result = pair_plan(b, self.pair(), r, o, (0, 0, 20, 20), 0.15)
        self.assertEqual(len(seen), 16)
        self.assertEqual(seen[0][0]["takeoff"], "bridge_join_via")
        self.assertGreater(seen[0][1], 89)
        self.assertLessEqual(seen[0][1], 90)
        self.assertEqual(o.deadline, deadline)
        self.assertEqual(result["status"], "no_route")

    def test_small_total_budget_retains_legacy_reserve_and_never_exceeds_deadline(self):
        b = self.make_board()
        r = rules()
        clock = [100.0]
        o = Oracle(b, r, deadline=109.0)
        seen = []

        def attempt(b, p, r, trial, bounds, pitch, order, topology=None):
            seen.append((topology, trial.deadline))
            clock[0] = trial.deadline
            return dict(status="no_route")

        with patch.dict(
            os.environ,
            {
                "PNR_PAIR_JOINT_TOPOLOGIES": "1",
                "PNR_PAIR_JOINT_TRIAL_SECONDS": "90",
                "PNR_PAIR_JOINT_MAX_TRIALS": "6",
                "PNR_PAIR_AUXILIARY_SCOPE": "separate",
            },
        ), patch("pnr.native_electrical.time.monotonic", side_effect=lambda: clock[0]), patch(
            "pnr.native_electrical._pair_plan_order", side_effect=attempt
        ):
            result = pair_plan(b, self.pair(), r, o, (0, 0, 20, 20), 0.15)
        self.assertEqual(seen[0][1], 106.0)
        self.assertEqual(
            sum(a["status"] == "joint_phase_budget" for a in result["order_attempts"]), 5
        )
        self.assertTrue(any(t and t["takeoff"] == "declared" for t, d in seen))
        self.assertTrue(all(d <= 109 for t, d in seen))
        self.assertEqual(o.deadline, 109.0)

    def test_trial_budget_rejects_nonfinite_value(self):
        with patch.dict(
            os.environ, {"PNR_PAIR_JOINT_TOPOLOGIES": "1", "PNR_PAIR_JOINT_TRIAL_SECONDS": "nan"}
        ):
            with self.assertRaises(ValueError):
                pair_plan(
                    self.make_board(),
                    self.pair(),
                    rules(),
                    Oracle(self.make_board(), rules()),
                    (0, 0, 20, 20),
                    0.15,
                )


if __name__ == "__main__":
    unittest.main()
