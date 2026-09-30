import unittest

from pnr.native_loop import open_net_coverage


def report(*items):
    return {"unconnected_items": [{"items": i} for i in items]}


class CoverageTest(unittest.TestCase):
    def test_closed_net_disappears_from_current_native_counts(self):
        inventory = {"a": "FB", "b": "MODE"}
        initial = report([{"uuid": "a"}], [{"uuid": "b"}])
        latest = report([{"uuid": "b"}])
        self.assertEqual(open_net_coverage(initial, inventory), {"FB": 1, "MODE": 1})
        self.assertEqual(open_net_coverage(latest, inventory), {"MODE": 1})

    def test_new_track_uuid_uses_exact_known_net_from_native_description(self):
        result = open_net_coverage(
            report([{"uuid": "new", "description": "Track [MODE] on In2.Cu"}]),
            {"pad": "MODE", "other": "MODE_2"},
        )
        self.assertEqual(result, {"MODE": 1})

    def test_second_endpoint_resolves_missing_first_uuid(self):
        self.assertEqual(
            open_net_coverage(report([{"uuid": "new"}, {"uuid": "pad"}]), {"pad": "FB"}), {"FB": 1}
        )

    def test_unresolved_and_conflicting_names_remain_in_total(self):
        result = open_net_coverage(
            report([], [{"uuid": "lost"}], [{"uuid": "a"}, {"uuid": "b"}]), {"a": "A", "b": "B"}
        )
        self.assertEqual(result, {"unknown": 3})

    def test_bus_brackets_are_matched_literally(self):
        self.assertEqual(
            open_net_coverage(
                report([{"description": "Pad 1 [DATA[3]] of U1 on F.Cu"}]),
                {"a": "DATA[3]", "b": "DATA"},
            ),
            {"DATA[3]": 1},
        )

    def test_zero_open_report_does_not_carry_old_inventory_counts(self):
        self.assertEqual(open_net_coverage(report(), {"old": "MODE"}), {})


if __name__ == "__main__":
    unittest.main()
