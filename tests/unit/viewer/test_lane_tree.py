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

    def test_best_leaf_prefers_running_over_failed_over_queued_over_done(self):
        # s0 itself is a still-running source_round_start lane with a done and a failed child
        # underneath it; s1 has only a queued child. A single failed leaf must not steal the
        # selection away from the subtree that is still actively running.
        case = self.tree["children"]["ladder"]["children"]["keep-on"]["children"][
            "09-mcu-usb-31-6L-SGSGPS"
        ]
        self.assertEqual(case["best_leaf"], "ladder/keep-on/09-mcu-usb-31-6L-SGSGPS/s0")

    def test_best_leaf_prefers_failed_over_queued_when_nothing_is_running(self):
        lanes = {
            "g/a": lane("g/a", **FAILED),
            "g/b": lane("g/b", **QUEUED),
        }
        tree = build_tree(lanes)
        self.assertEqual(tree["children"]["g"]["best_leaf"], "g/a")


class OddShapeTest(unittest.TestCase):
    def test_empty_lanes_is_an_empty_root(self):
        tree = build_tree({})
        self.assertEqual(tree["children"], {})
        self.assertEqual(
            tree["counts"],
            dict(queued=0, running=0, stalled=0, rejected=0, done=0, failed=0, total=0),
        )
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

    def test_real_halving_style_parent_candidate_stage_ids(self):
        # The real pnr.mc.halving shape (hardware/pnr/pnr/mc/halving.py, PNR_LIVE_CANDIDATE =
        # f"{parent}/{cand.name}/{stage}"): a parent generation, numbered candidates within it,
        # each running a named stage -- not the "round"-as-a-path-segment shape a made-up fixture
        # once assumed. Grouping by generation/candidate falls straight out of the id split.
        lanes = {
            "mc/cand00/screen": lane("mc/cand00/screen", iteration=1, **FAILED),
            "mc/cand01/screen": lane("mc/cand01/screen", iteration=1, **RUNNING),
            "mc/cand01/place": lane("mc/cand01/place", iteration=1, **RUNNING),
            "mc/cand02/screen": lane("mc/cand02/screen", iteration=1, **DONE),
        }
        tree = build_tree(lanes)
        mc = tree["children"]["mc"]
        self.assertEqual(set(mc["order"]), {"cand00", "cand01", "cand02"})
        self.assertEqual(mc["counts"]["total"], 4)
        cand01 = mc["children"]["cand01"]
        self.assertEqual(set(cand01["order"]), {"screen", "place"})
        self.assertEqual(cand01["counts"]["running"], 2)

    def test_radar_style_flat_candidate_ids(self):
        # The radar60 campaign's Monte Carlo lanes are flat: mc/s0..mc/s3, no per-stage nesting
        # at all. build_tree must not assume any particular depth.
        lanes = {
            f"mc/s{i}": lane(f"mc/s{i}", iteration=1, **(DONE if i else RUNNING)) for i in range(4)
        }
        tree = build_tree(lanes)
        mc = tree["children"]["mc"]
        self.assertEqual(mc["counts"]["total"], 4)
        self.assertEqual(set(mc["order"]), {"s0", "s1", "s2", "s3"})
        for seg in mc["order"]:
            self.assertEqual(mc["children"][seg]["children"], {})  # leaves, no nesting

    def test_rejected_and_stalled_states_are_counted_separately_from_failed(self):
        lanes = {
            "g/a": lane("g/a", status="rejected", kind="iteration_complete", phase="result"),
            "g/b": lane("g/b", **FAILED),
        }
        tree = build_tree(lanes)
        g = tree["children"]["g"]
        self.assertEqual(g["counts"]["rejected"], 1)
        self.assertEqual(g["counts"]["failed"], 1)

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
