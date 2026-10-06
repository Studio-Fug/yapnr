"""yapnr.viewer.timing: stage aggregation, the event-timestamp fallback estimator, scope
filtering, caching, and running-lane handling -- against both a synthetic "observed" fixture
(stage_start/stage_end, as the engine now emits them) and a real event slice cut from a finished
ladder campaign's live directory (dp-ab2) that predates stage events, exercising the fallback."""

import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from yapnr.viewer import timing

FIXTURES = Path(__file__).parent / "fixtures" / "timing"


def _write_event(events_dir, idx, **fields):
    fields.setdefault("data", {})
    event = dict(schema="pnr-live-event-v1", id="%020d-%08x" % (idx, idx), **fields)
    (events_dir / ("%020d-%08x.json" % (idx, idx))).write_text(json.dumps(event))
    return event


class EstimatedFallbackTests(unittest.TestCase):
    """The real dp-ab2 slice: no stage_start/stage_end anywhere, so every span must come from
    event timestamps (candidate_start/complete, source_round_start, phase_start, geometry_result),
    clearly marked estimated."""

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

    def test_initial_start_children_get_a_setup_span(self):
        result = timing.aggregate(Path(self.tmp))
        lane = next(x for x in result["timeline"] if x["candidate"].endswith("initial-start-00"))
        self.assertEqual([s["stage"] for s in lane["spans"]], ["setup"])
        self.assertAlmostEqual(lane["spans"][0]["seconds"], 0.0609, places=3)

    def test_seed_lane_gets_global_placement_then_gloss_spans(self):
        result = timing.aggregate(Path(self.tmp))
        lane = next(
            x for x in result["timeline"] if x["candidate"] == "ladder/01-connector-led-2/s1"
        )
        stages = [s["stage"] for s in lane["spans"]]
        self.assertEqual(stages, ["global-placement", "gloss"])

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
        self.assertIn("setup", result["stages"])
        stats = result["stages"]["setup"]
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

    def _seed(self, candidate, stage_seconds):
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
        _write_event(
            self.events_dir,
            self._next_idx(),
            time=t,
            kind="candidate_complete",
            candidate=candidate,
            data={},
        )

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
        result = timing.aggregate(Path(self.tmp))
        lane = next(x for x in result["timeline"] if x["candidate"] == "ladder/b/s0")
        self.assertTrue(lane["running"])
        self.assertEqual(result["running_count"], 1)

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


if __name__ == "__main__":
    unittest.main()
