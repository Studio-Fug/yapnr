import unittest
from types import SimpleNamespace

from pnr.pair_joint import branch_join_port, point_at_fraction


class PairJointTest(unittest.TestCase):
    def rules(self):
        return dict(fab=dict(via_diameter_mm=0.6, via_drill_mm=0.3, clearance_mm=0.15))

    def pair(self):
        return dict(p="p", n="n", gap_mm=0.15, max_uncoupled_mm=2)

    def paths(self):
        return dict(p=[(0, 0, 0), (1, 0, 0), (1, 1, 0)], n=[(3, 0, 0), (4, 0, 0), (4, 1, 0)])

    def test_arclength_midpoint_and_contact_offsets(self):
        self.assertEqual(
            point_at_fraction([(0, 0, 0), (1, 0, 0), (1, 3, 0)], 0.5), ((1.0, 1.0), 2.0, 2.0)
        )
        for fraction in (-0.1, 1.1):
            with self.assertRaises(ValueError):
                point_at_fraction(self.paths()["p"], fraction)

    def test_mirrored_half_branch_has_both_timing_offsets(self):
        report = branch_join_port(
            self.pair(), self.paths(), SimpleNamespace(via=lambda *args: True), self.rules(), 0.25
        )
        self.assertEqual(report["declared_offsets"], {"p": 1.5, "n": 1.5})
        self.assertEqual(report["alternate_offsets"], {"p": 0.5, "n": 0.5})
        self.assertEqual(report["port"]["entry_budget_mm"], 1.5)
        self.assertFalse(report["port"]["reuse"])

    def test_new_via_and_uncoupled_budget_fail_closed(self):
        report = branch_join_port(
            self.pair(), self.paths(), SimpleNamespace(via=lambda *args: False), self.rules()
        )
        self.assertEqual(report["status"], "pair_joint_via_blocked")
        report = branch_join_port(
            self.pair(), self.paths(), SimpleNamespace(via=lambda *args: True), self.rules(), 0
        )
        self.assertEqual(report["status"], "pair_joint_fanout_budget")

    def test_layer_transition_in_branch_rejected(self):
        paths = self.paths()
        paths["p"][1] = (1, 0, 1)
        report = branch_join_port(
            self.pair(), paths, SimpleNamespace(via=lambda *args: True), self.rules()
        )
        self.assertEqual(report["status"], "pair_joint_unsupported_branch")


if __name__ == "__main__":
    unittest.main()
