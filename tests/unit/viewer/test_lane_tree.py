"""yapnr.viewer.lane_tree: the experiment browser's hierarchy, built from lane id samples drawn
from real live directories (a ladder sweep's arm/case/seed/candidate ids, a bare single-segment
``controller`` id, the candidate-search audit's ``r03/search``) plus odd shapes: a lane id that
is itself a prefix of other lane ids (both a leaf and a group), empty input, and very deep
nesting.
"""

import unittest

from yapnr.viewer.lane_tree import build_tree

DONE = dict(status="complete", kind="candidate_complete", phase="result", opens=0, violations=0)
FAILED = dict(status="failed", kind="candidate_failed", phase="legalization")
RUNNING = dict(status="start", kind="candidate_start", phase="initial-placement signals")
QUEUED = dict(status="queued", kind="candidate_queued")


def lane(id_, **fields):
    return dict(dict(id=id_), **fields)


class RealShapeTest(unittest.TestCase):
    def setUp(self):
        # Verbatim id shapes from a real "ladder" live directory (lv2p2-rungs-all / r6-keep-ab):
        # campaign arm / board case / seed / placement-candidate, a bare single-segment id, and
        # the candidate-search audit's own bookkeeping lane.
        self.lanes = {
            "controller": lane("controller", **RUNNING),
            "ladder/keep-on/09-mcu-usb-31-6L-SGSGPS/s0": lane(
                "ladder/keep-on/09-mcu-usb-31-6L-SGSGPS/s0", status=None, kind="source_round_start"
            ),
            "ladder/keep-on/09-mcu-usb-31-6L-SGSGPS/s0/initial-start-00": lane(
                "ladder/keep-on/09-mcu-usb-31-6L-SGSGPS/s0/initial-start-00", **DONE
            ),
            "ladder/keep-on/09-mcu-usb-31-6L-SGSGPS/s0/initial-start-01": lane(
                "ladder/keep-on/09-mcu-usb-31-6L-SGSGPS/s0/initial-start-01", **FAILED
            ),
            "ladder/keep-on/09-mcu-usb-31-6L-SGSGPS/s1/initial-start-00": lane(
                "ladder/keep-on/09-mcu-usb-31-6L-SGSGPS/s1/initial-start-00", **QUEUED
            ),
            "r03/search": lane("r03/search", status=None, kind="batch_alternatives"),
        }
        self.tree = build_tree(self.lanes)

    def test_audit_search_lane_is_excluded(self):
        self.assertNotIn("r03", self.tree["children"])

    def test_single_segment_id_is_a_top_level_leaf(self):
        node = self.tree["children"]["controller"]
        self.assertEqual(node["lane_id"], "controller")
        self.assertEqual(node["children"], {})
        self.assertEqual(node["counts"]["total"], 1)

    def test_four_level_nesting_builds_the_full_path(self):
        s0 = self.tree["children"]["ladder"]["children"]["keep-on"]["children"][
            "09-mcu-usb-31-6L-SGSGPS"
        ]["children"]["s0"]
        self.assertIsNone(self.tree["children"]["ladder"]["lane_id"])
        self.assertEqual(s0["lane_id"], "ladder/keep-on/09-mcu-usb-31-6L-SGSGPS/s0")
        self.assertEqual(set(s0["children"]), {"initial-start-00", "initial-start-01"})

    def test_a_group_that_is_also_a_lane_counts_itself_once(self):
        # s0 is itself a lane (source_round_start) AND the parent of two candidate lanes: it must
        # show up in its own subtree's counts exactly once, not be silently dropped either way.
        s0 = self.tree["children"]["ladder"]["children"]["keep-on"]["children"][
            "09-mcu-usb-31-6L-SGSGPS"
        ]["children"]["s0"]
        self.assertEqual(s0["counts"]["total"], 3)  # itself + its 2 candidate children
        self.assertEqual(s0["counts"]["done"], 1)
        self.assertEqual(s0["counts"]["failed"], 1)
        self.assertEqual(s0["counts"]["running"], 1)  # the source_round_start lane itself

    def test_root_counts_every_non_audit_lane_exactly_once(self):
        self.assertEqual(self.tree["counts"]["total"], len(self.lanes) - 1)  # minus the audit lane

    def test_seed_children_sort_naturally_not_lexicographically(self):
        case = self.tree["children"]["ladder"]["children"]["keep-on"]["children"][
            "09-mcu-usb-31-6L-SGSGPS"
        ]
        self.assertEqual(case["order"], ["s0", "s1"])

    def test_best_leaf_prefers_failed_over_running_over_queued_over_done(self):
        case = self.tree["children"]["ladder"]["children"]["keep-on"]["children"][
            "09-mcu-usb-31-6L-SGSGPS"
        ]
        self.assertEqual(
            case["best_leaf"], "ladder/keep-on/09-mcu-usb-31-6L-SGSGPS/s0/initial-start-01"
        )


class OddShapeTest(unittest.TestCase):
    def test_empty_lanes_is_an_empty_root(self):
        tree = build_tree({})
        self.assertEqual(tree["children"], {})
        self.assertEqual(tree["counts"], dict(queued=0, running=0, done=0, failed=0, total=0))
        self.assertEqual(tree["fraction"], 0.0)
        self.assertIsNone(tree["best_leaf"])

    def test_only_audit_lanes_is_also_an_empty_root(self):
        tree = build_tree({"r01/search": lane("r01/search"), "r02/search": lane("r02/search")})
        self.assertEqual(tree["children"], {})

    def test_very_deep_nesting_has_no_hardcoded_depth_limit(self):
        deep_id = "/".join(f"level{i}" for i in range(12))
        tree = build_tree({deep_id: lane(deep_id, **DONE)})
        node = tree
        for i in range(12):
            node = node["children"][f"level{i}"]
        self.assertEqual(node["lane_id"], deep_id)
        self.assertEqual(tree["counts"]["total"], 1)

    def test_a_lane_id_with_empty_segments_is_tolerated(self):
        # A malformed id (leading/trailing/doubled slash) degrades to its non-empty segments
        # rather than raising or producing an unreachable empty-name node.
        tree = build_tree({"//a//b/": lane("//a//b/", **RUNNING)})
        self.assertEqual(set(tree["children"]), {"a"})
        self.assertEqual(tree["children"]["a"]["children"]["b"]["lane_id"], "//a//b/")

    def test_mc_halving_style_round_candidate_ids(self):
        # pnr.mc.halving-shaped ids: a round, then numbered candidates within it.
        lanes = {
            f"mc/round1/candidate{i}": lane(
                f"mc/round1/candidate{i}", iteration=1, **(DONE if i else FAILED)
            )
            for i in range(3)
        }
        tree = build_tree(lanes)
        round1 = tree["children"]["mc"]["children"]["round1"]
        self.assertEqual(round1["counts"]["total"], 3)
        self.assertEqual(set(round1["order"]), {"candidate0", "candidate1", "candidate2"})

    def test_single_lane_with_no_slashes_at_all(self):
        tree = build_tree({"solo": lane("solo", **RUNNING)})
        self.assertEqual(list(tree["order"]), ["solo"])
        self.assertEqual(tree["fraction"], tree["children"]["solo"]["fraction"])

    def test_group_fraction_is_the_mean_of_its_leaves_not_just_a_done_count(self):
        lanes = {
            "g/a": lane("g/a", **DONE),  # fraction 1.0
            "g/b": lane("g/b", **QUEUED),  # fraction 0.0
        }
        tree = build_tree(lanes)
        self.assertAlmostEqual(tree["children"]["g"]["fraction"], 0.5)


if __name__ == "__main__":
    unittest.main()
