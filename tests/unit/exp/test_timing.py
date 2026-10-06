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


if __name__ == "__main__":
    unittest.main()
