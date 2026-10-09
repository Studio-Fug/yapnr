"""Timing uses recorded evidence, interval unions and honest missing ends."""

import json
import tempfile
import unittest
from pathlib import Path

from yapnr.agent import activity


class ActivityTest(unittest.TestCase):
    def test_overlapping_sessions_close_and_missing_end(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            client = "00000000-0000-0000-0000-000000000000"
            a = "00000000-0000-0000-0000-000000000001"
            b = "00000000-0000-0000-0000-000000000002"

            def event(session, action, seconds):
                return activity.presence(
                    root,
                    dict(client=client, session=session, action=action),
                    f"2026-01-01T00:00:{seconds:02d}+00:00",
                )

            event(a, "open", 0)
            event(a, "open", 2)
            event(b, "open", 5)
            event(b, "heartbeat", 15)
            event(b, "heartbeat", 8)
            event(a, "close", 10)
            now = activity.timestamp("2026-01-01T00:02:00+00:00")
            data = activity.timeline(root, now)
            self.assertEqual(data["project_open_seconds"], 15)
            self.assertEqual(data["sessions"][0]["start"], now - 120)
            self.assertIsNone(data["sessions"][1]["end"])
            self.assertIn("unknown", data["sessions"][1]["status"])
            self.assertEqual(data["tasks"], [])
            event(a, "heartbeat", 20)
            self.assertEqual(activity.timeline(root, now)["project_open_seconds"], 15)

    def test_parallel_tools_are_summed_and_duplicate_snapshots_deduplicated(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            snapshots = root / ".yapnr/workspace/opencode"
            snapshots.mkdir(parents=True)
            parts = [
                dict(
                    type="tool",
                    id=str(i),
                    tool="shell",
                    state=dict(status="completed", time=dict(start=1000, end=11000)),
                )
                for i in range(2)
            ]
            for name in ["one", "two"]:
                (snapshots / (name + ".json")).write_text(
                    json.dumps(dict(info=dict(id="session"), messages=[dict(parts=parts)]))
                )
            data = activity.timeline(root, 20)
            self.assertEqual(len(data["tasks"]), 2)
            self.assertEqual(data["summed_task_seconds"], 20)
            self.assertEqual(data["project_open_seconds"], 0)
            self.assertTrue(all(t["count"] == 1 for t in data["tasks"]))
            self.assertIn("CPU time unavailable", data["task_measure"])

    def test_repeated_stages_keep_distinct_occurrences_and_shared_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            state = root / ".yapnr/workflow/state.json"
            state.parent.mkdir(parents=True)
            history = [
                dict(
                    time=f"2026-01-01T00:00:0{i}+00:00",
                    receipt_sha256=("a" if i != 1 else "b") * 64,
                    to=("schematic" if i != 1 else "blocked"),
                    revision=1,
                )
                for i in range(3)
            ]
            state.write_text(json.dumps(dict(history=history)))
            rows = activity.timeline(root, activity.timestamp("2026-01-01T00:00:03+00:00"))[
                "stages"
            ]
            self.assertEqual(len({r["id"] for r in rows}), 3)
            self.assertEqual(rows[0]["receipt"], rows[2]["receipt"])
            self.assertEqual(rows[1]["label"], "blocked")
            self.assertIsNone(rows[2]["end"])

    def test_union_does_not_double_count_overlaps(self):
        self.assertEqual(activity.union([(0, 10), (5, 15), (20, 25), (22, 24)]), 20)


if __name__ == "__main__":
    unittest.main()
