"""pnr.hier.compact_block: violation counting (not the number of kinds a hard
violation spans)."""

import unittest

from pnr.hier.compact_block import _violation_count


class ViolationCountTest(unittest.TestCase):
    def test_counts_violations_not_kinds(self):
        # Two kinds, three violations total -- len(bad) would see 2, missing the second
        # overlap of a kind that was already present before the step.
        bad = {"overlaps": [("A", "B"), ("C", "D")], "outside_outline": ["E"]}
        self.assertEqual(_violation_count(bad), 3)

    def test_empty_is_zero(self):
        self.assertEqual(_violation_count({}), 0)

    def test_a_second_violation_of_an_existing_kind_counts(self):
        before = {"overlaps": [("A", "B")]}
        after = {"overlaps": [("A", "B"), ("C", "D")]}
        self.assertEqual(_violation_count(before), 1)
        self.assertEqual(_violation_count(after), 2)
        self.assertGreater(_violation_count(after), _violation_count(before))
        # len(bad) (the old, buggy comparison) cannot see this: both have one kind.
        self.assertEqual(len(before), len(after))


if __name__ == "__main__":
    unittest.main()
