import unittest
import pcbnew as k
from pnr.pad_entry import snapshot, repair, repair_changed_entries
from test_native_electrical import board, pad, add_track

RULES = {
    "fab": {"track_width_mm": 0.2, "clearance_mm": 0.15},
    "net_classes": [{"nets": ["rail"], "width_mm": 1.5}],
}


class Repair161(unittest.TestCase):
    def fixture(self):
        b = board()
        p = pad(b, "Q", "9", "rail", (5, 5), (4.2, 3.58))
        q = pad(b, "Q", "7", "rail", (5.63, 7.18), (0.57, 1.6))
        b.BuildConnectivity()
        before = snapshot(b, RULES)
        track = add_track(b, "rail", k.F_Cu, (6.91, 7.18), (9.555, 4.535), 1.5)
        b.BuildConnectivity()
        return b, p, q, track, before

    def test_large_land_and_cascade_are_full_width_repeatable(self):
        b, p, q, t, before = self.fixture()
        r = repair_changed_entries(b, RULES, before)
        self.assertEqual(r["lost_pad_entries"], [])
        self.assertEqual(r["new_bad_entries"], [])
        self.assertGreaterEqual(r["entry_repairs"]["passes"], 2)
        self.assertEqual({a["pad"] for a in r["entry_repairs"]["added"]}, {"Q.9", "Q.7"})
        self.assertTrue(all(a["width_mm"] == 1.5 for a in r["entry_repairs"]["added"]))
        self.assertTrue(all(snapshot(b, RULES).values()))
        self.assertEqual(repair_changed_entries(b, RULES, before)["entry_repairs"]["added"], [])

    def test_foreign_copper_still_blocks_large_land_branch(self):
        b, p, q, t, before = self.fixture()
        pad(b, "BLOCK", "1", "other", (5.8, 5.8), (0.6, 0.6))
        b.BuildConnectivity()
        r = repair_changed_entries(b, RULES, before)
        self.assertTrue(r["new_bad_entries"])
        self.assertFalse(r["entry_repairs"]["added"])

    def test_does_not_reduce_width_to_join_narrow_track(self):
        b, p, q, t, before = self.fixture()
        t.SetWidth(200000)
        r = repair_changed_entries(b, RULES, before)
        self.assertFalse(r["entry_repairs"]["added"])

    def test_scope_preserves_unselected_preexisting_bad_entry(self):
        b, p, q, t, before = self.fixture()
        bad = snapshot(b, RULES)
        r = repair_changed_entries(b, RULES, bad)
        self.assertFalse(r["entry_repairs"]["added"])


if __name__ == "__main__":
    unittest.main()
