import copy
import unittest

from pnr.power_bank_stage import quality_checks


class Gates(unittest.TestCase):
    def setUp(self):
        self.before = {"unconnected_items": [{}], "violations": []}
        self.old = {
            "partition": [["a", "b"]],
            "entries": {"a": True, "b": True},
            "reference_failures": [],
            "vias": 6,
            "electrical": {"subwidth_track_count": 3},
        }
        self.new = copy.deepcopy(self.old)
        self.new["vias"] = 3

    def accept(self):
        return quality_checks(self.before, self.before, self.old, self.new)[0]

    def test_current_capacity_quality_gate_accepts_equal_opens_fewer_vias(self):
        self.assertTrue(self.accept())

    def test_lost_branch_rejected(self):
        self.new["partition"] = [["a"], ["b"]]
        self.assertFalse(self.accept())

    def test_subwidth_regression_rejected(self):
        self.new["electrical"]["subwidth_track_count"] = 4
        self.assertFalse(self.accept())

    def test_new_bad_entry_rejected(self):
        self.new["entries"]["c"] = False
        self.assertFalse(self.accept())

    def test_lost_reference_rejected(self):
        self.new["reference_failures"] = ["pair"]
        self.assertFalse(self.accept())

    def test_no_quality_gain_rejected(self):
        self.new["vias"] = 6
        self.assertFalse(self.accept())

    def test_native_clearance_regression_rejected(self):
        after = copy.deepcopy(self.before)
        after["violations"] = [{"type": "clearance"}]
        self.assertFalse(quality_checks(self.before, after, self.old, self.new)[0])


if __name__ == "__main__":
    unittest.main()
