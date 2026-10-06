"""stage() must no-op (byte-identical to not calling it) without PNR_LIVE_DIR, and emit a
matched stage_start/stage_end pair -- even on failure -- with PNR_LIVE_DIR set."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pnr import stage_timing


class StageTimingTests(unittest.TestCase):
    def test_noop_without_live_dir(self):
        # No PNR_LIVE_DIR: pnr.live.emit's own no-op means stage() writes nothing and raises
        # nothing, so it adds no behavior to an unprofiled, non-live run.
        with patch.dict(os.environ, {}, clear=True):
            with stage_timing.stage("placement"):
                pass

    def test_emits_matched_start_end_pair(self):
        with tempfile.TemporaryDirectory() as live_dir:
            with patch.dict(os.environ, {"PNR_LIVE_DIR": live_dir}, clear=True):
                with stage_timing.stage("placement"):
                    pass
            events = sorted(Path(live_dir, "events").glob("*.json"))
            self.assertEqual(len(events), 2)
            start, end = (json.loads(f.read_text()) for f in events)
            self.assertEqual(start["kind"], "stage_start")
            self.assertEqual(end["kind"], "stage_end")
            self.assertEqual(start["data"]["stage"], "setup")  # placement -> setup alias
            self.assertEqual(end["data"]["stage"], "setup")
            self.assertGreaterEqual(end["data"]["seconds"], 0)

    def test_stage_end_fires_on_exception_and_is_timed(self):
        with tempfile.TemporaryDirectory() as live_dir:
            with patch.dict(os.environ, {"PNR_LIVE_DIR": live_dir}, clear=True):
                with self.assertRaises(ValueError):
                    with stage_timing.stage("route"):
                        raise ValueError("boom")
            events = sorted(Path(live_dir, "events").glob("*.json"))
            kinds = [json.loads(f.read_text())["kind"] for f in events]
            self.assertEqual(kinds, ["stage_start", "stage_end"])

    def test_case_timer_accumulates_by_canonical_stage(self):
        timer = stage_timing.CaseTimer()
        with patch.dict(os.environ, {}, clear=True), patch("pnr.live.emit"):
            with stage_timing.stage("placement", timer=timer):
                pass
            with stage_timing.stage("assemble", timer=timer):
                pass
            with stage_timing.stage("coalesce", timer=timer):
                pass
        summary = timer.summary()
        self.assertEqual(set(summary), {"setup", "route"})  # placement+assemble share "setup"
        self.assertGreaterEqual(summary["setup"], 0)

    def test_emit_summary_sends_timing_summary_event(self):
        timer = stage_timing.CaseTimer()
        timer.add("setup", 1.5)
        calls = []
        with patch("pnr.stage_timing.emit", side_effect=lambda *a, **k: calls.append((a, k))):
            timer.emit_summary(extra="x")
        self.assertEqual(calls[0][0], ("timing_summary",))
        self.assertEqual(calls[0][1]["data"]["stage_seconds"], {"setup": 1.5})
        self.assertEqual(calls[0][1]["data"]["extra"], "x")

    def test_unaliased_name_passes_through(self):
        self.assertEqual(stage_timing.canonical("gloss-metrics"), "gloss-metrics")
        self.assertEqual(stage_timing.canonical("planes"), "plane-partition-pours")


if __name__ == "__main__":
    unittest.main()
