"""Native gate and chain topology for saved-copper matching."""

import unittest

from pnr.native_match import chains, native_safe


class NativeMatchGateTest(unittest.TestCase):
    def report(self, types=(), opens=()):
        return dict(violations=[dict(type=t) for t in types], unconnected_items=list(opens))

    def test_accepts_only_native_improvement_without_other_findings(self):
        before = self.report(["skew_out_of_range"])
        self.assertTrue(native_safe(before, self.report()))
        self.assertFalse(native_safe(before, before))
        self.assertFalse(native_safe(before, self.report(["clearance"])))
        self.assertFalse(native_safe(before, self.report(opens=[{}])))
        self.assertFalse(
            native_safe(self.report(["clearance", "skew_out_of_range"]), self.report())
        )

    def test_prior_matched_rule_cannot_become_a_new_failure(self):
        before = self.report(["skew_out_of_range", "skew_out_of_range"])
        for row, name in zip(before["violations"], ["a", "b"]):
            row["description"] = "Skew (rule '%s' max skew 1 mm)" % name
        after = self.report(["skew_out_of_range"])
        after["violations"][0]["description"] = "Skew (rule 'c' max skew 1 mm)"
        self.assertFalse(native_safe(before, after))

    def test_chains_keep_branch_endpoints(self):
        rows = [("F.Cu", (0, 0), (1, 0)), ("F.Cu", (1, 0), (2, 0)), ("F.Cu", (1, 0), (1, 1))]
        result = chains(rows)
        self.assertEqual(len(result), 3)
        self.assertEqual(sorted(i for _, ids in result for i in ids), [0, 1, 2])
        self.assertTrue(all((1, 0) in path for path, _ in result))


if __name__ == "__main__":
    unittest.main()
