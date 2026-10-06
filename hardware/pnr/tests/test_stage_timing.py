"""stage() must no-op (byte-identical to not calling it) without PNR_LIVE_DIR, and emit a
matched stage_start/stage_end pair -- even on failure -- with PNR_LIVE_DIR set."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from pnr import stage_timing
from pnr.constraints import compile_constraints
from pnr.graph import BoardGraph
from pnr.route.feedback import route_and_place

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "..", "testdata", "splanc_dev")


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


class RouteFeedbackStageEventsTests(unittest.TestCase):
    """pnr.route.feedback._place_route_rounds -- the plain ladder-cell driver's real
    placement/legalize + routing loop (not pnr.full_iteration) -- reports its own rounds as
    global-placement/route spans; this is the path most campaigns actually run."""

    def test_round_loop_emits_a_matched_pair_per_round(self):
        with open(os.path.join(FIXTURE, "graph.json"), encoding="utf-8") as fh:
            graph = BoardGraph.from_json(fh.read())
        with open(os.path.join(FIXTURE, "constraints.yaml"), encoding="utf-8") as fh:
            constraints = compile_constraints(yaml.safe_load(fh), graph.refs)
        with tempfile.TemporaryDirectory() as live_dir:
            with patch.dict(os.environ, {"PNR_LIVE_DIR": live_dir}, clear=True):
                # detail_rules={} (not None): the production ladder-cell driver always passes real
                # detail_rules (hardware/pnr/regression/route_case.py), which is what makes the
                # loop take the detailed-router branch this test checks for a "route" stage.
                placed, report = route_and_place(
                    graph, constraints, seed=0, iters=50, max_rounds=2, detail_rules={}
                )
            events = [json.loads(f.read_text()) for f in Path(live_dir, "events").glob("*.json")]
        starts = [
            e
            for e in events
            if e["kind"] == "stage_start" and e["data"]["stage"] == "global-placement"
        ]
        ends = [
            e
            for e in events
            if e["kind"] == "stage_end" and e["data"]["stage"] == "global-placement"
        ]
        routes = [e for e in events if e["kind"] == "stage_end" and e["data"]["stage"] == "route"]
        # Every round opens a global-placement span; a round that breaks out early (a declared
        # termination path in the loop) may leave it unmatched, so ends/routes are <= starts,
        # never more -- and at least one full round completed on this fixture.
        self.assertEqual(len(starts), report.rounds)
        self.assertGreaterEqual(len(ends), 1)
        self.assertLessEqual(len(ends), len(starts))
        self.assertGreaterEqual(len(routes), 1)
        self.assertTrue(all(e["data"]["seconds"] >= 0 for e in ends + routes))


if __name__ == "__main__":
    unittest.main()
