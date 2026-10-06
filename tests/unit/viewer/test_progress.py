"""yapnr.viewer.progress: the phase/progress model, exercised against lane shapes drawn from real
live directories (a "ladder" placement-screening sweep's candidate_start/candidate_complete/
candidate_failed lanes, a routed candidate with a route_result progress payload, a queued and a
not-yet-classifiable lane) plus odd shapes a future engine change could still produce.
"""

import unittest

from yapnr.viewer.progress import PHASE_KEYS, classify, classify_lane, humanize


class ClassifyTest(unittest.TestCase):
    def test_queued_lane_is_zero_and_queued(self):
        lane = dict(
            id="ladder/keep-on/case/s1/initial-start-00", status="queued", kind="candidate_queued"
        )
        c = classify(lane)
        self.assertEqual(c["state"], "queued")
        self.assertEqual(c["fraction"], 0.0)
        self.assertIsNone(c["phase_key"])

    def test_real_initial_placement_phase_classifies_as_generate(self):
        # Verbatim shape from a real ladder live directory's candidate_start event.
        lane = dict(
            id="ladder/07-chaser-20-4L-SGPS/s1/initial-start-00",
            status="start",
            kind="candidate_start",
            phase="initial-placement signals",
        )
        c = classify(lane)
        self.assertEqual(c["phase_key"], "generate")
        self.assertEqual(c["state"], "running")
        self.assertGreater(c["fraction"], 0)
        self.assertLess(c["fraction"], 1 / len(PHASE_KEYS))

    def test_real_screening_complete_is_done(self):
        lane = dict(
            id="ladder/07-chaser-20-6L-SGSGPS-BB/s0/initial-start-01",
            status="complete",
            kind="candidate_complete",
            phase="initial-placement screening complete",
            opens=0,
            violations=0,
        )
        c = classify(lane)
        self.assertEqual(c["state"], "done")
        self.assertEqual(c["fraction"], 1.0)

    def test_legalization_phase_classifies_as_legalize_ahead_of_generic_placement(self):
        lane = dict(id="a", status="start", kind="candidate_start", phase="legalization")
        c = classify(lane)
        self.assertEqual(c["phase_key"], "legalize")

    def test_global_placement_classifies_as_place(self):
        lane = dict(id="a", status="start", kind="candidate_start", phase="global-placement")
        c = classify(lane)
        self.assertEqual(c["phase_key"], "place")

    def test_route_result_progress_gives_exact_fraction(self):
        lane = dict(
            id="a",
            status="start",
            kind="route_result",
            phase="routed",
            last_route=dict(progress=dict(done=412, total=530)),
        )
        c = classify(lane)
        self.assertEqual(c["phase_key"], "route")
        route_index = PHASE_KEYS.index("route")
        expected = (route_index + 412 / 530) / len(PHASE_KEYS)
        self.assertAlmostEqual(c["fraction"], expected)

    def test_route_without_progress_dict_falls_back_to_midpoint(self):
        lane = dict(id="a", status="start", kind="route_result", phase="routed")
        c = classify(lane)
        self.assertEqual(c["phase_key"], "route")
        self.assertEqual(c["phase_fraction"], 0.5)

    def test_failed_lane_keeps_its_reached_fraction_but_is_marked_failed(self):
        running = classify(
            dict(id="a", status="start", kind="candidate_start", phase="legalization")
        )
        failed = classify(
            dict(id="a", status="failed", kind="candidate_failed", phase="legalization")
        )
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(failed["fraction"], running["fraction"])

    def test_rejected_iteration_is_its_own_state_not_failed(self):
        # A rejected Monte Carlo candidate (halving/beam-search discarding a losing branch) is a
        # normal search outcome, not a failure -- it must not be painted the same red "failed".
        c = classify(dict(id="a", status="rejected", kind="iteration_complete", phase="result"))
        self.assertEqual(c["state"], "rejected")
        self.assertNotEqual(c["state"], "failed")

    def test_stalled_lane_reads_stalled_not_running_when_idle_past_the_threshold(self):
        from yapnr.viewer.progress import STALE_SECONDS

        lane = dict(id="a", status="start", kind="candidate_start", phase="routed", time=1000.0)
        fresh = classify(lane, now=1000.0 + STALE_SECONDS - 1)
        stale = classify(lane, now=1000.0 + STALE_SECONDS + 1)
        self.assertEqual(fresh["state"], "running")
        self.assertEqual(stale["state"], "stalled")
        # Phase/fraction are unaffected -- only the state changes.
        self.assertEqual(stale["phase_key"], fresh["phase_key"])

    def test_route_result_kind_with_no_phase_classifies_as_route_not_check(self):
        # A route_result event with no phase string falls back to classifying `kind`, and the
        # literal string "route_result" contains "result" as a substring -- it must not be
        # misread as the generic check-family "result" keyword.
        c = classify(dict(id="a", status="start", kind="route_result", phase=None))
        self.assertEqual(c["phase_key"], "route")

    def test_source_pr_round_phase_is_classifiable_not_waiting_for_placement(self):
        c = classify(
            dict(id="a", status=None, kind="source_round_start", phase="source P/R round 1")
        )
        self.assertIsNotNone(c["phase_key"])

    def test_repeated_round_never_makes_the_bar_go_backwards(self):
        # Round 1 reaches "check" (near the end of the pipeline); round 2 restarts at "generate".
        # The lap-compounding model must keep the displayed fraction monotonically increasing.
        round1_mid = classify(
            dict(id="a", status=None, kind="candidate_start", phase="congestion", round=1)
        )
        round1_done = classify(
            dict(id="a", status=None, kind="candidate_start", phase="drc", round=1),
            held_over=round1_mid,
        )
        round2_start = classify(
            dict(id="a", status=None, kind="candidate_start", phase="initial-placement", round=2),
            held_over=round1_done,
        )
        self.assertGreaterEqual(round2_start["fraction"], round1_done["fraction"])
        # And it should still be visibly less than fully done.
        self.assertLess(round2_start["fraction"], 1.0)

    def test_a_lane_that_never_repeats_rounds_is_unaffected_by_lap_compounding(self):
        # No `round` field at all: behaves exactly like before this model was added.
        c = classify(dict(id="a", status="start", kind="candidate_start", phase="global-placement"))
        index = PHASE_KEYS.index("place")
        self.assertAlmostEqual(c["fraction"], (index + 0.5) / len(PHASE_KEYS))

    def test_unknown_phase_string_does_not_crash_and_has_no_phase_key(self):
        c = classify(
            dict(id="a", status="start", kind="candidate_start", phase="quantum-anneal-v7")
        )
        self.assertEqual(c["state"], "running")
        self.assertIsNone(c["phase_key"])

    def test_unknown_phase_holds_over_the_previous_classification(self):
        # A worker_config_applied event (no `phase`, no classifiable `kind`) must not reset a
        # lane that was last seen mid-route back to "no phase at all".
        held_over = classify(dict(id="a", status="start", kind="candidate_start", phase="routed"))
        lane = dict(id="a", status=None, kind="worker_config_applied", phase=None)
        c = classify(lane, held_over=held_over)
        self.assertEqual(c["phase_key"], "route")

    def test_no_classifiable_event_at_all_is_running_at_zero_not_queued(self):
        c = classify(dict(id="a", status=None, kind="source_round_start", phase=None))
        self.assertEqual(c["state"], "running")
        self.assertEqual(c["fraction"], 0.0)

    def test_empty_lane_dict_does_not_crash(self):
        c = classify({})
        self.assertEqual(c["state"], "running")
        self.assertEqual(c["fraction"], 0.0)


class HumanizeTest(unittest.TestCase):
    def test_queued_text(self):
        self.assertEqual(humanize(dict(status="queued", kind="candidate_queued")), "Queued")

    def test_routing_text_matches_the_documented_example(self):
        lane = dict(
            status="start",
            kind="route_result",
            phase="routed",
            last_route=dict(progress=dict(done=412, total=530)),
        )
        self.assertEqual(humanize(lane), "Routing: 412 of 530 connections (78%)")

    def test_failed_text_reports_opens_and_violations(self):
        lane = dict(
            status="failed", kind="candidate_failed", phase="legalization", opens=3, violations=1
        )
        self.assertEqual(humanize(lane), "Failed: 3 unconnected, 1 DRC error")

    def test_failed_text_pluralizes_multiple_drc_errors(self):
        lane = dict(
            status="failed", kind="candidate_failed", phase="legalization", opens=0, violations=2
        )
        self.assertEqual(humanize(lane), "Failed: 2 DRC errors")

    def test_failed_text_with_no_detail_is_still_plain(self):
        lane = dict(status="failed", kind="candidate_failed", phase="legalization")
        self.assertEqual(humanize(lane), "Failed")

    def test_done_text_reports_clean_drc_and_duration(self):
        lane = dict(
            status="complete",
            kind="candidate_complete",
            phase="result",
            opens=0,
            violations=0,
            started_at=100.0,
            time=112.4,
        )
        self.assertEqual(humanize(lane), "Done: all connected, DRC clean, 12.4 s")

    def test_done_text_without_duration_data_omits_it(self):
        lane = dict(
            status="complete", kind="candidate_complete", phase="result", opens=0, violations=0
        )
        self.assertEqual(humanize(lane), "Done: all connected, DRC clean")

    def test_done_text_never_claims_all_connected_when_connections_are_missing(self):
        # A finished lane that still has missing connections (opens > 0) must not read "all
        # connected" just because it reached state "done".
        lane = dict(
            status="complete", kind="candidate_complete", phase="result", opens=2, violations=0
        )
        self.assertEqual(humanize(lane), "Done: 2 unconnected, DRC clean")

    def test_done_text_never_claims_drc_clean_when_drc_never_ran(self):
        # No `opens`/`violations` on the lane at all (a screening-only run that never extracted a
        # board, so DRC never ran): must not claim "all connected, DRC clean" -- that is a real
        # fact the telemetry does not have.
        lane = dict(status="complete", kind="candidate_complete", phase="result")
        self.assertEqual(humanize(lane), "Done: finished")

    def test_waiting_text_for_a_lane_with_no_phase_yet(self):
        lane = dict(status=None, kind="source_round_start", phase=None)
        self.assertEqual(humanize(lane), "Waiting for placement")

    def test_checking_text_reports_opens_and_violations(self):
        lane = dict(
            status="start", kind="candidate_start", phase="congestion", opens=2, violations=0
        )
        self.assertEqual(humanize(lane), "Checking: 2 opens / 0 violations")

    def test_failed_text_falls_back_to_a_screening_only_candidates_own_payload(self):
        # A real candidate_failed/candidate_complete from a placement-screening-only run never
        # gets a board extracted (no `opens`/`violations` on the lane itself); apply_event stores
        # its raw data as lane["last_candidate"] instead, under the engine's own field names.
        lane = dict(
            status="failed",
            kind="candidate_failed",
            phase="legalization",
            last_candidate=dict(missing_connections=2, violations=1),
        )
        self.assertEqual(humanize(lane), "Failed: 2 unconnected, 1 DRC error")


class ClassifyLaneTest(unittest.TestCase):
    def test_attaches_status_text_and_progress_and_is_pure(self):
        lane = dict(
            id="a", status="start", kind="candidate_start", phase="initial-placement signals"
        )
        before = dict(lane)
        result = classify_lane(lane)
        self.assertEqual(lane, before)  # classify_lane never mutates its argument
        self.assertEqual(result["status_text"], humanize(lane))
        self.assertIn("fraction", result)

    def test_held_over_phase_comes_from_the_lanes_own_previous_progress_field(self):
        lane = dict(id="a", status=None, kind="worker_config_applied", phase=None)
        lane["progress"] = classify(
            dict(id="a", status="start", kind="candidate_start", phase="routed")
        )
        result = classify_lane(lane)
        self.assertEqual(result["phase_key"], "route")


if __name__ == "__main__":
    unittest.main()
