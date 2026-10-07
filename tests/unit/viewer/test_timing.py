"""yapnr.viewer.timing: stage aggregation, the event-timestamp fallback estimator, scope
filtering, caching, and running-lane handling -- against both a synthetic "observed" fixture
(stage_start/stage_end, as the engine now emits them) and a real event slice cut from a finished
ladder campaign's live directory (dp-ab2) that predates stage events, exercising the fallback.

``MirrorLagTests`` below uses three more real slices (``fixtures/timing/mirror-lag/``, cut from
the live-hub's finished lv2p2-rungs-all, dp-ab2 and timing-demo2 runs) that pin the actual bug
this branch fixes: a lane's last real marker (``source_round_start``/``phase_start``/
``geometry_result``, or in the "demo" slice's case a real ``stage_end``) followed, hours later, by
a mirror-synthesized ``task_complete`` -- before the fix, the whole mirror-detection gap got
charged to whichever stage that marker opened."""

import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from yapnr.viewer import timing
from yapnr.viewer.progress import STALE_SECONDS

FIXTURES = Path(__file__).parent / "fixtures" / "timing"


def _write_event(events_dir, idx, **fields):
    fields.setdefault("data", {})
    event = dict(schema="pnr-live-event-v1", id="%020d-%08x" % (idx, idx), **fields)
    (events_dir / ("%020d-%08x.json" % (idx, idx))).write_text(json.dumps(event))
    return event


class EstimatedFallbackTests(unittest.TestCase):
    """The real dp-ab2 slice: no stage_start/stage_end anywhere, so every span must come from
    event timestamps (candidate_start/complete, source_round_start, phase_start, geometry_result),
    clearly marked estimated. Its events are long in the past, so every lane in it reads finished
    (stale) with no ``now`` override -- this fixture predates this branch's "running" fix and is
    exactly the dp-ab2 slice that exposed it (all 552 of its lanes used to read "running")."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        shutil.copytree(FIXTURES / "estimated" / "events", Path(self.tmp) / "events")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_mode_is_estimated(self):
        result = timing.aggregate(Path(self.tmp))
        self.assertEqual(result["mode"], "estimated")
        self.assertTrue(
            all(span["estimated"] for lane in result["timeline"] for span in lane["spans"])
        )

    def test_old_campaign_reads_finished_not_running(self):
        # No `now` override: real wall-clock time is used, and every timestamp in this fixture is
        # years old, so every lane in it is stale -- never "running" just because it lacks a
        # terminal event (the bug this fixture originally caught).
        result = timing.aggregate(Path(self.tmp))
        self.assertEqual(result["running_count"], 0)
        self.assertTrue(all(not lane["running"] for lane in result["timeline"]))

    def test_initial_start_children_get_a_screening_span(self):
        result = timing.aggregate(Path(self.tmp))
        lane = next(x for x in result["timeline"] if x["candidate"].endswith("initial-start-00"))
        self.assertEqual([s["stage"] for s in lane["spans"]], ["initial-pool-screening"])
        self.assertAlmostEqual(lane["spans"][0]["seconds"], 0.0609, places=3)

    def test_seed_lane_gets_route_then_gloss_spans(self):
        # source_round_start opens the *routing* portion of a round, not placement (it fires
        # after placement/legalize is already done) -- the pre-fix fallback mislabeled this span
        # "global-placement".
        result = timing.aggregate(Path(self.tmp))
        lane = next(
            x for x in result["timeline"] if x["candidate"] == "ladder/01-connector-led-2/s1"
        )
        stages = [s["stage"] for s in lane["spans"]]
        self.assertEqual(stages, ["route", "gloss"])

    def test_scope_filters_to_tree_prefix(self):
        full = timing.aggregate(Path(self.tmp), scope="")
        scoped = timing.aggregate(Path(self.tmp), scope="ladder/01-connector-led-2/s1")
        self.assertEqual(scoped["lane_count"], full["lane_count"])  # this fixture is one lineage
        scoped_leaf = timing.aggregate(
            Path(self.tmp), scope="ladder/01-connector-led-2/s1/initial-start-00"
        )
        self.assertEqual(scoped_leaf["lane_count"], 1)

    def test_stage_stats_have_the_expected_shape(self):
        result = timing.aggregate(Path(self.tmp))
        self.assertIn("initial-pool-screening", result["stages"])
        stats = result["stages"]["initial-pool-screening"]
        for key in ("count", "total", "mean", "median", "p90", "max", "share", "histogram"):
            self.assertIn(key, stats)


class ObservedStageEventTests(unittest.TestCase):
    """A synthetic campaign using the new stage_start/stage_end shape end to end: observed spans
    must be used verbatim (exact seconds), never re-derived from timestamps."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.events_dir = Path(self.tmp) / "events"
        self.events_dir.mkdir()
        self._idx = 0  # shared across _seed() calls so filenames never collide across lanes

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _next_idx(self):
        self._idx += 1
        return self._idx

    def _seed(self, candidate, stage_seconds, terminal=True):
        t = 1000.0
        for stage, seconds in stage_seconds:
            _write_event(
                self.events_dir,
                self._next_idx(),
                time=t,
                kind="stage_start",
                candidate=candidate,
                data=dict(stage=stage, label=stage),
            )
            t += seconds
            _write_event(
                self.events_dir,
                self._next_idx(),
                time=t,
                kind="stage_end",
                candidate=candidate,
                data=dict(stage=stage, label=stage, seconds=seconds),
            )
        if terminal:
            _write_event(
                self.events_dir,
                self._next_idx(),
                time=t,
                kind="candidate_complete",
                candidate=candidate,
                data={},
            )
        return t

    def test_observed_mode_and_exact_seconds(self):
        self._seed("ladder/a/s0", [("setup", 1.5), ("route", 3.25)])
        result = timing.aggregate(Path(self.tmp))
        self.assertEqual(result["mode"], "observed")
        lane = result["timeline"][0]
        self.assertEqual([s["seconds"] for s in lane["spans"]], [1.5, 3.25])
        self.assertFalse(any(s["estimated"] for s in lane["spans"]))

    def test_stage_percentiles_across_multiple_lanes(self):
        self._seed("ladder/a/s0", [("route", 1.0)])
        self._seed("ladder/a/s1", [("route", 2.0)])
        self._seed("ladder/a/s2", [("route", 3.0)])
        result = timing.aggregate(Path(self.tmp))
        route = result["stages"]["route"]
        self.assertEqual(route["count"], 3)
        self.assertAlmostEqual(route["mean"], 2.0)
        self.assertAlmostEqual(route["median"], 2.0)
        self.assertAlmostEqual(route["total"], 6.0)
        self.assertAlmostEqual(route["share"], 1.0)

    def test_still_running_lane_is_marked_partial(self):
        _write_event(
            self.events_dir,
            self._next_idx(),
            time=2000.0,
            kind="stage_start",
            candidate="ladder/b/s0",
            data=dict(stage="route", label="route"),
        )
        # Viewed "live", moments after its last event -- well inside STALE_SECONDS.
        result = timing.aggregate(Path(self.tmp), now=2000.0 + STALE_SECONDS / 2)
        lane = next(x for x in result["timeline"] if x["candidate"] == "ladder/b/s0")
        self.assertTrue(lane["running"])
        self.assertEqual(result["running_count"], 1)

    def test_a_closed_stage_alone_does_not_mark_a_lane_finished(self):
        # A plain ladder-cell campaign (pnr.route.feedback) has no engine-level "this lane is
        # done" event yet -- only per-stage boundaries. A stage_end closing its own stage must
        # not be read as the whole lane finishing (it is very much still running after routing:
        # DRC, judge, artifacts). Viewed live (now close to its last event), it must read running
        # on the strength of that alone, same as before this branch's running fix.
        end = self._seed("ladder/c/s0", [("global-placement", 0.7), ("route", 0.1)], terminal=False)
        result = timing.aggregate(Path(self.tmp), now=end + 1.0)
        lane = next(x for x in result["timeline"] if x["candidate"] == "ladder/c/s0")
        self.assertTrue(lane["running"])

    def test_a_lane_with_no_terminal_event_reads_finished_once_stale(self):
        # The same shape as above, but viewed long after its last event (STALE_SECONDS behind):
        # lacking a terminal event is not proof the lane is still going.
        end = self._seed("ladder/c/s0", [("global-placement", 0.7), ("route", 0.1)], terminal=False)
        result = timing.aggregate(Path(self.tmp), now=end + STALE_SECONDS + 1.0)
        lane = next(x for x in result["timeline"] if x["candidate"] == "ladder/c/s0")
        self.assertFalse(lane["running"])

    def test_a_task_timing_run_span_closed_by_a_terminal_state_marks_a_lane_finished(self):
        self._write_task_timing("ladder/d/s0", "run", seconds=42.0, terminal=True)
        result = timing.aggregate(Path(self.tmp), now=3000.0 + 1.0)
        lane = next(x for x in result["timeline"] if x["candidate"] == "ladder/d/s0")
        self.assertFalse(lane["running"])

    def test_a_preempted_tasks_closed_run_span_does_not_mark_a_lane_finished(self):
        # yapnr.exp.timing closes a "run" span the moment a task leaves RUNNING for any reason,
        # including a preemption retry (back to QUEUED) -- only a genuinely terminal Batch state
        # sets `terminal`. Without it, a closed run span must not read the lane as done.
        self._write_task_timing("ladder/e/s0", "run", seconds=42.0, terminal=False)
        result = timing.aggregate(Path(self.tmp), now=3000.0 + 1.0)
        lane = next(x for x in result["timeline"] if x["candidate"] == "ladder/e/s0")
        self.assertTrue(lane["running"])

    def test_task_timing_spans_are_excluded_from_stage_totals(self):
        # queue-wait/boot-fetch/run are infrastructure, not a pipeline stage: they must never
        # appear in `stages`/`total_seconds`, or overlap with the engine's own spans on the same
        # lane would double count (High #3 in the review that found this).
        self._seed("ladder/f/s0", [("route", 1.0)])
        self._write_task_timing("ladder/f/s0", "run", seconds=1.0, terminal=True, time=1001.0)
        self._write_task_timing(
            "ladder/f/s0", "queue-wait", seconds=5.0, terminal=False, time=990.0
        )
        result = timing.aggregate(Path(self.tmp))
        self.assertNotIn("run", result["stages"])
        self.assertNotIn("queue-wait", result["stages"])
        self.assertAlmostEqual(result["total_seconds"], 1.0)
        self.assertIn("run", result["task_overhead"])
        self.assertIn("queue-wait", result["task_overhead"])
        self.assertAlmostEqual(result["task_overhead"]["queue-wait"]["total"], 5.0)
        # Still shown on the lane's own timeline (for the Gantt), just not in the stage stats.
        lane = next(x for x in result["timeline"] if x["candidate"] == "ladder/f/s0")
        self.assertEqual({s["stage"] for s in lane["spans"]}, {"route", "run", "queue-wait"})

    def test_observed_lane_keeps_an_estimated_span_for_an_unobserved_stage(self):
        # gloss has no stage_start/stage_end anywhere yet -- a lane with real observed spans for
        # other stages must still surface gloss from its phase_start/geometry_result markers
        # (High #2: dropping it silently erased 45% of a real campaign's time).
        end = self._seed("ladder/g/s0", [("route", 1.0)], terminal=False)
        _write_event(
            self.events_dir,
            self._next_idx(),
            time=end + 0.1,
            kind="phase_start",
            candidate="ladder/g/s0",
            data=dict(phase="gloss", label="gloss"),
        )
        _write_event(
            self.events_dir,
            self._next_idx(),
            time=end + 2.1,
            kind="geometry_result",
            candidate="ladder/g/s0",
            data=dict(phase="gloss"),
        )
        _write_event(
            self.events_dir,
            self._next_idx(),
            time=end + 2.1,
            kind="candidate_complete",
            candidate="ladder/g/s0",
            data={},
        )
        result = timing.aggregate(Path(self.tmp))
        lane = next(x for x in result["timeline"] if x["candidate"] == "ladder/g/s0")
        stages = [s["stage"] for s in lane["pipeline_spans"]]
        self.assertEqual(stages, ["route", "gloss"])
        self.assertFalse(lane["pipeline_spans"][0]["estimated"])
        self.assertTrue(lane["pipeline_spans"][1]["estimated"])
        self.assertEqual(result["mode"], "mixed")

    def _write_task_timing(self, candidate, stage, seconds, terminal=False, time=3000.0):
        _write_event(
            self.events_dir,
            self._next_idx(),
            time=time,
            kind="task_timing",
            candidate=candidate,
            data=dict(stage=stage, seconds=seconds, terminal=terminal),
        )

    def test_slowest_lanes_sorted_descending(self):
        self._seed("ladder/a/s0", [("route", 1.0)])
        self._seed("ladder/a/s1", [("route", 9.0)])
        result = timing.aggregate(Path(self.tmp))
        self.assertEqual(result["slowest"][0]["candidate"], "ladder/a/s1")

    def test_group_breakdown_stacks_shares_per_parent_path(self):
        self._seed("ladder/a/s0", [("setup", 1.0), ("route", 1.0)])
        result = timing.aggregate(Path(self.tmp))
        group = next(g for g in result["groups"] if g["group"] == "ladder/a")
        self.assertAlmostEqual(sum(group["shares"].values()), 1.0)

    def test_concurrency_series_covers_the_whole_span(self):
        self._seed("ladder/a/s0", [("route", 5.0)])
        result = timing.aggregate(Path(self.tmp))
        self.assertTrue(result["concurrency"])
        self.assertTrue(any(point["running"] >= 1 for point in result["concurrency"]))

    def test_concurrency_counts_leaf_lanes_not_parent_and_child_together(self):
        # A case/seed lane and its own initial-pool screening children overlap in wall time by
        # construction (the children run *during* the parent's placement search) -- counting both
        # as separately "running" overstates how many things were really concurrent.
        self._seed("ladder/a/s0", [("global-placement", 2.0)])
        self._seed("ladder/a/s0/initial-start-00", [("initial-pool-screening", 1.0)])
        result = timing.aggregate(Path(self.tmp))
        self.assertTrue(all(point["running"] <= 1 for point in result["concurrency"]))

    def test_cache_incremental_rescan_picks_up_new_events_without_reparsing(self):
        self._seed("ladder/a/s0", [("route", 1.0)])
        first = timing.aggregate(Path(self.tmp))
        self.assertEqual(first["lane_count"], 1)
        time.sleep(0.01)
        self._seed("ladder/a/s1", [("route", 1.0)])
        second = timing.aggregate(Path(self.tmp))
        self.assertEqual(second["lane_count"], 2)

    def test_empty_campaign(self):
        result = timing.aggregate(Path(self.tmp))
        self.assertEqual(result["mode"], "empty")
        self.assertEqual(result["lane_count"], 0)

    def test_wall_seconds_is_the_scopes_actual_span_not_a_lane_sum(self):
        self._seed("ladder/a/s0", [("route", 1.0)])
        self._seed("ladder/a/s1", [("route", 1.0)])
        result = timing.aggregate(Path(self.tmp))
        # Both lanes start at the same synthetic t=1000.0 and run 1s each in parallel: the wall
        # span is ~1s, not the 2s a naive sum across lanes would give.
        self.assertAlmostEqual(result["wall_seconds"], 1.0, places=3)
        self.assertAlmostEqual(result["total_seconds"], 2.0, places=3)


class MirrorLagTests(unittest.TestCase):
    """Real event slices cut from finished live-hub campaigns, each with a lane whose last real
    event is followed, hours later, by a mirror-synthesized ``task_complete``
    (:data:`timing.MIRROR_TERMINAL_KINDS`). Before this branch, that whole gap was folded into
    whichever stage marker was still "open" when the mirror caught up -- see the module
    docstring's "Bounding spans to a lane's own event window" section, and the comment at the top
    of this file for where each slice comes from."""

    MIRROR_LAG = FIXTURES / "mirror-lag"

    def _aggregate(self, name):
        return timing.aggregate(self.MIRROR_LAG / name)

    def _lane_last_real_time(self, root, candidate):
        events = [e for e in timing.load_events(root) if e.get("candidate") == candidate]
        real = [e for e in events if e["kind"] not in timing.MIRROR_TERMINAL_KINDS]
        return (real or events)[-1]["time"]

    def test_no_span_exceeds_its_lanes_last_real_event(self):
        # The regression itself: a pipeline span's end must never reach past the lane's own last
        # non-mirror event, no matter how much later a mirror-synthesized terminal event landed.
        for name in ("lv2p2", "dp-ab2", "demo"):
            root = self.MIRROR_LAG / name
            result = self._aggregate(name)
            for lane in result["timeline"]:
                bound = self._lane_last_real_time(root, lane["candidate"])
                for span in lane["pipeline_spans"]:
                    self.assertLessEqual(
                        span["end"],
                        bound + 1e-6,
                        "%s: %s span %s end %.3f exceeds last real event %.3f"
                        % (name, lane["candidate"], span["stage"], span["end"], bound),
                    )

    def test_stage_medians_are_no_longer_hours(self):
        # The exact numbers the owner's spot check flagged: lv2p2's "route" and dp-ab2's "gloss"
        # used to report multi-hour medians (12702s / full campaign span). Any real pipeline
        # stage in these tiny slices is sub-minute.
        for name, stage in (("lv2p2", "route"), ("dp-ab2", "gloss")):
            result = self._aggregate(name)
            self.assertIn(stage, result["stages"])
            self.assertLess(result["stages"][stage]["max"], 60.0)

    def test_wall_seconds_equals_the_event_range_for_a_finished_campaign(self):
        for name in ("lv2p2", "dp-ab2", "demo"):
            root = self.MIRROR_LAG / name
            result = self._aggregate(name)
            events = timing.load_events(root)
            self.assertEqual(result["running_count"], 0)
            expected = max(e["time"] for e in events) - min(e["time"] for e in events)
            self.assertAlmostEqual(result["wall_seconds"], expected, places=3)

    def test_unattributed_absorbs_the_mirror_gap_not_the_stage(self):
        # The gap between the last real marker and the delayed task_complete is real wall-clock
        # time; after the fix it shows up as unattributed, not as part of "route"/"gloss".
        for name in ("lv2p2", "dp-ab2", "demo"):
            result = self._aggregate(name)
            self.assertIn("unattributed", result)
            self.assertGreater(result["unattributed"]["total"], 1000.0)
            self.assertLess(result["coverage"], 0.01)

    def test_coverage_is_attributed_over_lane_wall(self):
        for name in ("lv2p2", "dp-ab2", "demo"):
            result = self._aggregate(name)
            # None of these lanes are running (asserted above), so each lane's own wall time is
            # simply its last event minus its first -- `timeline` doesn't expose "seconds"
            # directly (that's a `slowest`-only field), so it's reconstructed here.
            lane_wall_total = sum(lane["end"] - lane["start"] for lane in result["timeline"])
            expected = result["total_seconds"] / lane_wall_total if lane_wall_total else 1.0
            self.assertAlmostEqual(result["coverage"], expected, places=6)
            # And the two rows account for the whole lane-wall total, by construction.
            self.assertAlmostEqual(
                result["total_seconds"] + result["unattributed"]["total"],
                lane_wall_total,
                places=3,
            )


class UnattributedSyntheticTests(unittest.TestCase):
    """Synthetic observed-mode fixtures isolating the unattributed/coverage math itself, and the
    "only extend to now while genuinely running" rule for an unclosed stage_start."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.events_dir = Path(self.tmp) / "events"
        self.events_dir.mkdir()
        self._idx = 0

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _next_idx(self):
        self._idx += 1
        return self._idx

    def test_fully_covered_lane_has_zero_unattributed(self):
        t = 1000.0
        _write_event(
            self.events_dir,
            self._next_idx(),
            time=t,
            kind="stage_start",
            candidate="ladder/a/s0",
            data=dict(stage="route", label="route"),
        )
        _write_event(
            self.events_dir,
            self._next_idx(),
            time=t + 2.0,
            kind="stage_end",
            candidate="ladder/a/s0",
            data=dict(stage="route", label="route", seconds=2.0),
        )
        _write_event(
            self.events_dir,
            self._next_idx(),
            time=t + 2.0,
            kind="candidate_complete",
            candidate="ladder/a/s0",
            data={},
        )
        result = timing.aggregate(Path(self.tmp))
        self.assertAlmostEqual(result["unattributed"]["total"], 0.0, places=6)
        self.assertAlmostEqual(result["coverage"], 1.0, places=6)

    def test_dangling_stage_start_closes_at_the_terminal_event_not_now(self):
        # A stage_start with no stage_end (the process died mid-stage) must become a span closed
        # at the lane's own terminal event, never silently dropped (the old behaviour) and never
        # stretched to whatever "now" happens to be when the panel is queried.
        t = 1000.0
        _write_event(
            self.events_dir,
            self._next_idx(),
            time=t,
            kind="stage_start",
            candidate="ladder/b/s0",
            data=dict(stage="route", label="route"),
        )
        _write_event(
            self.events_dir,
            self._next_idx(),
            time=t + 3.0,
            kind="candidate_complete",
            candidate="ladder/b/s0",
            data={},
        )
        result = timing.aggregate(Path(self.tmp), now=t + 999999.0)
        lane = result["timeline"][0]
        self.assertEqual(len(lane["pipeline_spans"]), 1)
        span = lane["pipeline_spans"][0]
        self.assertEqual(span["stage"], "route")
        self.assertAlmostEqual(span["seconds"], 3.0, places=6)
        self.assertTrue(span["estimated"])  # the end was inferred, not observed
        self.assertAlmostEqual(result["unattributed"]["total"], 0.0, places=6)

    def test_dangling_stage_start_on_a_running_lane_extends_to_now(self):
        t = 1000.0
        _write_event(
            self.events_dir,
            self._next_idx(),
            time=t,
            kind="stage_start",
            candidate="ladder/c/s0",
            data=dict(stage="route", label="route"),
        )
        now = t + STALE_SECONDS / 2
        result = timing.aggregate(Path(self.tmp), now=now)
        lane = result["timeline"][0]
        self.assertTrue(lane["running"])
        span = lane["pipeline_spans"][0]
        self.assertAlmostEqual(span["end"], now, places=6)

    def test_campaign_wall_seconds_extends_to_now_only_while_running(self):
        t = 1000.0
        _write_event(
            self.events_dir,
            self._next_idx(),
            time=t,
            kind="stage_start",
            candidate="ladder/d/s0",
            data=dict(stage="route", label="route"),
        )
        _write_event(
            self.events_dir,
            self._next_idx(),
            time=t + 1.0,
            kind="stage_end",
            candidate="ladder/d/s0",
            data=dict(stage="route", label="route", seconds=1.0),
        )
        running_now = t + 1.0 + STALE_SECONDS / 2
        result = timing.aggregate(Path(self.tmp), now=running_now)
        self.assertEqual(result["running_count"], 1)
        self.assertAlmostEqual(result["wall_seconds"], running_now - t, places=3)

        stale_now = t + 1.0 + STALE_SECONDS * 10
        result = timing.aggregate(Path(self.tmp), now=stale_now)
        self.assertEqual(result["running_count"], 0)
        self.assertAlmostEqual(result["wall_seconds"], 1.0, places=3)

    def test_a_native_terminal_event_is_never_excluded_from_the_open_bound(self):
        # candidate_complete/case_complete/case_failed/iteration_complete are emitted in-process
        # and are real timestamps -- only MIRROR_TERMINAL_KINDS (task_complete) is excluded.
        self.assertEqual(timing.MIRROR_TERMINAL_KINDS, frozenset({"task_complete"}))
        for kind in ("candidate_complete", "candidate_failed", "case_complete", "case_failed"):
            self.assertNotIn(kind, timing.MIRROR_TERMINAL_KINDS)
            self.assertIn(kind, timing.TERMINAL_KINDS)


if __name__ == "__main__":
    unittest.main()
