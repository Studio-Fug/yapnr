"""yapnr.exp.timing: Batch status-event spans (queue wait / boot+fetch / run), the task_timing
mirror against a fake store of task records (no real GCP call), idempotent re-polls, and
tolerance of a preempted-and-retried task (repeated QUEUED/SCHEDULED/RUNNING)."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from yapnr.exp import timing


def _events(*pairs):
    """``pairs`` of (taskState, RFC3339 time) -> the statusEvents list Batch's API shape uses."""
    return [{"taskState": state, "eventTime": when} for state, when in pairs]


class TaskStateSpansTests(unittest.TestCase):
    def test_simple_lifecycle_gives_three_spans(self):
        events = _events(
            ("QUEUED", "2026-10-06T00:00:00Z"),
            ("SCHEDULED", "2026-10-06T00:00:10Z"),
            ("RUNNING", "2026-10-06T00:00:40Z"),
            ("SUCCEEDED", "2026-10-06T00:01:40Z"),
        )
        spans = timing.task_state_spans(events)
        self.assertEqual([s["stage"] for s in spans], ["queue-wait", "boot-fetch", "run"])
        self.assertAlmostEqual(spans[0]["seconds"], 10)
        self.assertAlmostEqual(spans[1]["seconds"], 30)
        self.assertAlmostEqual(spans[2]["seconds"], 60)

    def test_unordered_events_are_sorted_first(self):
        events = _events(
            ("RUNNING", "2026-10-06T00:00:40Z"),
            ("QUEUED", "2026-10-06T00:00:00Z"),
            ("SCHEDULED", "2026-10-06T00:00:10Z"),
        )
        now = timing._parse_rfc3339("2026-10-06T00:01:00Z")
        spans = timing.task_state_spans(events, now=now)
        self.assertEqual([s["stage"] for s in spans], ["queue-wait", "boot-fetch", "run"])

    def test_preempted_and_retried_task_gets_two_queue_waits(self):
        events = _events(
            ("QUEUED", "2026-10-06T00:00:00Z"),
            ("SCHEDULED", "2026-10-06T00:00:05Z"),
            ("RUNNING", "2026-10-06T00:00:10Z"),
            ("QUEUED", "2026-10-06T00:01:00Z"),  # preempted, requeued
            ("SCHEDULED", "2026-10-06T00:01:20Z"),
            ("RUNNING", "2026-10-06T00:01:30Z"),
            ("SUCCEEDED", "2026-10-06T00:02:00Z"),
        )
        spans = timing.task_state_spans(events)
        self.assertEqual(
            [s["stage"] for s in spans],
            ["queue-wait", "boot-fetch", "run", "queue-wait", "boot-fetch", "run"],
        )

    def test_still_running_task_uses_now_for_the_open_span(self):
        events = _events(
            ("QUEUED", "2026-10-06T00:00:00Z"),
            ("SCHEDULED", "2026-10-06T00:00:05Z"),
            ("RUNNING", "2026-10-06T00:00:10Z"),
        )
        now = timing._parse_rfc3339("2026-10-06T00:00:25Z")
        spans = timing.task_state_spans(events, now=now)
        self.assertEqual(spans[-1]["stage"], "run")
        self.assertAlmostEqual(spans[-1]["seconds"], 15)

    def test_malformed_events_are_skipped_not_raised(self):
        events = [{"taskState": "QUEUED"}, {"eventTime": "2026-10-06T00:00:00Z"}, {}]
        self.assertEqual(timing.task_state_spans(events), [])

    def test_run_span_closed_by_succeeded_is_terminal(self):
        events = _events(
            ("QUEUED", "2026-10-06T00:00:00Z"),
            ("SCHEDULED", "2026-10-06T00:00:05Z"),
            ("RUNNING", "2026-10-06T00:00:10Z"),
            ("SUCCEEDED", "2026-10-06T00:00:30Z"),
        )
        spans = timing.task_state_spans(events)
        run = next(s for s in spans if s["stage"] == "run")
        self.assertTrue(run["terminal"])

    def test_run_span_closed_by_a_preemption_retry_is_not_terminal(self):
        # RUNNING -> QUEUED again (not a terminal Batch state): the task was preempted and is
        # being retried, not actually done.
        events = _events(
            ("QUEUED", "2026-10-06T00:00:00Z"),
            ("SCHEDULED", "2026-10-06T00:00:05Z"),
            ("RUNNING", "2026-10-06T00:00:10Z"),
            ("QUEUED", "2026-10-06T00:01:00Z"),
        )
        spans = timing.task_state_spans(events)
        run = next(s for s in spans if s["stage"] == "run")
        self.assertFalse(run["terminal"])

    def test_still_open_run_span_is_not_terminal(self):
        events = _events(
            ("QUEUED", "2026-10-06T00:00:00Z"),
            ("SCHEDULED", "2026-10-06T00:00:05Z"),
            ("RUNNING", "2026-10-06T00:00:10Z"),
        )
        spans = timing.task_state_spans(events, now=timing._parse_rfc3339("2026-10-06T00:00:25Z"))
        run = next(s for s in spans if s["stage"] == "run")
        self.assertFalse(run["terminal"])


class EmitTaskTimingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_writes_one_event_per_span(self):
        events = _events(
            ("QUEUED", "2026-10-06T00:00:00Z"),
            ("SCHEDULED", "2026-10-06T00:00:05Z"),
            ("RUNNING", "2026-10-06T00:00:10Z"),
            ("SUCCEEDED", "2026-10-06T00:00:30Z"),
        )
        written = timing.emit_task_timing(self.tmp, "ladder/cell/task-0", events)
        self.assertEqual(written, 3)
        files = sorted((self.tmp / "events").glob("*.json"))
        self.assertEqual(len(files), 3)
        kinds = {json.loads(f.read_text())["kind"] for f in files}
        self.assertEqual(kinds, {"task_timing"})
        stages = {json.loads(f.read_text())["data"]["stage"] for f in files}
        self.assertEqual(stages, {"queue-wait", "boot-fetch", "run"})
        run = next(
            json.loads(f.read_text())
            for f in files
            if json.loads(f.read_text())["data"]["stage"] == "run"
        )
        self.assertTrue(run["data"]["terminal"])  # closed by SUCCEEDED

    def test_repoll_of_the_same_history_is_idempotent(self):
        events = _events(
            ("QUEUED", "2026-10-06T00:00:00Z"),
            ("SCHEDULED", "2026-10-06T00:00:05Z"),
        )
        first = timing.emit_task_timing(self.tmp, "ladder/cell/task-0", events)
        second = timing.emit_task_timing(self.tmp, "ladder/cell/task-0", events)
        self.assertEqual(first, 1)
        self.assertEqual(second, 0)
        self.assertEqual(len(list((self.tmp / "events").glob("*.json"))), 1)

    def test_event_candidate_matches_the_task_lane(self):
        events = _events(("QUEUED", "2026-10-06T00:00:00Z"), ("SCHEDULED", "2026-10-06T00:00:05Z"))
        timing.emit_task_timing(self.tmp, "ladder/cell/task-7", events)
        f = next((self.tmp / "events").glob("*.json"))
        self.assertEqual(json.loads(f.read_text())["candidate"], "ladder/cell/task-7")


class MirrorTaskTimingFakeStoreTests(unittest.TestCase):
    """A fake ``batch tasks list``-shaped store: no real gcloud or network call."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_mirrors_every_task_in_the_fake_list(self):
        tasks = [
            {
                "name": "ladder/cell/task-0",
                "status": {
                    "statusEvents": _events(
                        ("QUEUED", "2026-10-06T00:00:00Z"),
                        ("SCHEDULED", "2026-10-06T00:00:05Z"),
                        ("RUNNING", "2026-10-06T00:00:10Z"),
                        ("SUCCEEDED", "2026-10-06T00:00:30Z"),
                    )
                },
            },
            {
                "name": "ladder/cell/task-1",
                "status": {
                    "statusEvents": _events(
                        ("QUEUED", "2026-10-06T00:00:00Z"),
                        ("SCHEDULED", "2026-10-06T00:00:08Z"),
                    )
                },
            },
        ]
        written = timing.mirror_task_timing(self.tmp, tasks)
        self.assertEqual(written, 4)  # 3 spans for task-0, 1 for task-1
        candidates = {
            json.loads(f.read_text())["candidate"] for f in (self.tmp / "events").glob("*.json")
        }
        self.assertEqual(candidates, {"ladder/cell/task-0", "ladder/cell/task-1"})

    def test_a_malformed_task_record_is_skipped_not_raised(self):
        tasks = [None, {"status": "not-a-dict"}, {"name": "ok", "status": {"statusEvents": []}}]
        self.assertEqual(timing.mirror_task_timing(self.tmp, tasks), 0)

    def test_task_with_no_candidate_is_skipped(self):
        tasks = [{"status": {"statusEvents": _events(("QUEUED", "2026-10-06T00:00:00Z"))}}]
        self.assertEqual(timing.mirror_task_timing(self.tmp, tasks), 0)


class GcpTaskCandidateMappingTests(unittest.TestCase):
    """candidate_for_gcp_task / mirror_gcp_batch_task_timing: a Batch task's own numeric index
    (Batch's fan-out, no chunking on this backend) through one submission's .indices file to the
    campaign's own task id -- keyed by task id, never Batch's own task name (High #4 in the
    review that found the mirror wasn't wired up this way at all)."""

    TASK_NAME = "projects/p/locations/us-central1/jobs/j/taskGroups/group0/tasks/%d"

    def test_maps_batch_index_through_indices_to_task_id(self):
        # Submission skipped task-line 0 (filtered out when planned, say); its three tasks are
        # tasks.jsonl lines 1, 2 and 4.
        indices = ["1", "2", "4"]
        task_ids = ["t0", "t1", "t2", "t3", "t4"]
        task = {"name": self.TASK_NAME % 1}  # Batch task index 1 -> indices[1] -> line 2 -> t2
        self.assertEqual(timing.candidate_for_gcp_task(task, indices, task_ids), "t2")

    def test_out_of_range_index_returns_none(self):
        task = {"name": self.TASK_NAME % 5}
        self.assertIsNone(timing.candidate_for_gcp_task(task, ["0", "1"], ["t0", "t1"]))

    def test_malformed_name_returns_none(self):
        self.assertIsNone(timing.candidate_for_gcp_task({"name": "not-a-task-name"}, [], []))
        self.assertIsNone(timing.candidate_for_gcp_task({}, [], []))

    def test_mirror_gcp_batch_task_timing_keys_events_by_task_id_not_batch_name(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        tasks = [
            {
                "name": self.TASK_NAME % 0,
                "status": {
                    "statusEvents": _events(
                        ("QUEUED", "2026-10-06T00:00:00Z"),
                        ("SCHEDULED", "2026-10-06T00:00:05Z"),
                    )
                },
            }
        ]
        written = timing.mirror_gcp_batch_task_timing(tmp, tasks, ["3"], ["a", "b", "c", "d"])
        self.assertEqual(written, 1)
        f = next((tmp / "events").glob("*.json"))
        event = json.loads(f.read_text())
        self.assertEqual(event["candidate"], "d")  # indices[0] == "3" -> task_ids[3] == "d"
        self.assertNotIn(self.TASK_NAME % 0, event["candidate"])


class FormatTableTests(unittest.TestCase):
    def test_includes_mode_stage_rows_and_slowest(self):
        result = dict(
            mode="observed",
            lane_count=2,
            running_count=0,
            stage_order=["route"],
            stages={
                "route": dict(
                    count=2, total=3.0, mean=1.5, median=1.5, p90=1.95, max=2.0, share=1.0
                )
            },
            slowest=[dict(candidate="a", seconds=2.0, running=False)],
        )
        table = timing.format_table(result)
        self.assertIn("mode: observed", table)
        self.assertIn("route", table)
        self.assertIn("a", table)

    def test_missing_wall_and_coverage_default_rather_than_raise(self):
        # Older callers' result dicts (and this test's own synthetic one above) may not carry
        # wall_seconds/coverage/unattributed -- format_table must not KeyError on them.
        result = dict(
            mode="empty", lane_count=0, running_count=0, stage_order=[], stages={}, slowest=[]
        )
        table = timing.format_table(result)
        self.assertIn("wall:", table)
        self.assertIn("coverage:", table)

    def test_includes_unattributed_row_and_coverage(self):
        result = dict(
            mode="estimated",
            lane_count=1,
            running_count=0,
            wall_seconds=100.0,
            coverage=0.2,
            stage_order=["route"],
            stages={
                "route": dict(
                    count=1, total=20.0, mean=20.0, median=20.0, p90=20.0, max=20.0, share=1.0
                )
            },
            unattributed=dict(
                count=1, total=80.0, mean=80.0, median=80.0, p90=80.0, max=80.0, share=0.8
            ),
            slowest=[dict(candidate="a", seconds=100.0, running=False)],
        )
        table = timing.format_table(result)
        self.assertIn("coverage: 20.0%", table)
        self.assertIn("unattributed", table)


if __name__ == "__main__":
    unittest.main()
