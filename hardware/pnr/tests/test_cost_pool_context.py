"""Recorded cost identity follows the initial start, including failures."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cost_fixture import fixture

from pnr.place.cost_capture import initial_start_context, phase_context
from pnr.place.initial_pool import InitialPoolConfig, select_initial_placement
from pnr.place.legalize import LegalizationError


class CostPoolContext(unittest.TestCase):
    def test_nested_context_restores_caller_even_on_error(self):
        with patch.dict(
            os.environ,
            {
                "PNR_LIVE_CANDIDATE": "parent",
                "PNR_COST_INITIAL_START": "outer",
                "PNR_COST_INITIAL_SEED": "5",
            },
        ):
            before = dict(os.environ)
            with self.assertRaises(RuntimeError):
                with initial_start_context(dict(id="start-02", seed=27)):
                    self.assertEqual(phase_context()["candidate"], "parent/initial-start-02")
                    with initial_start_context(dict(id="nested", seed=9)):
                        self.assertEqual(phase_context()["initial_seed"], "9")
                    self.assertEqual(phase_context()["initial_seed"], "27")
                    raise RuntimeError("failed placement")
            self.assertEqual(dict(os.environ), before)

    def test_two_real_placements_capture_distinct_start_and_seed_without_pose_change(self):
        g, c = fixture()
        starts = [
            dict(id=f"start-{i:02}", seed=17 + i, kind="legacy-global", positions={}, rotations={})
            for i in range(2)
        ]
        args = dict(
            config=InitialPoolConfig(starts=2, route_finalists=1, proxy_budget=2),
            iters=80,
            proxy_only=True,
        )
        with tempfile.TemporaryDirectory() as td, patch(
            "pnr.place.initial_pool.initial_starts", return_value=starts
        ), patch("pnr.place.capacity_proxy.cheap_score", return_value=0), patch(
            "pnr.place.capacity_proxy.score", return_value={"score": 0}
        ):
            root = Path(td)
            with patch.dict(os.environ, {"PNR_COST_CAPTURE_DIR": "", "PNR_LIVE_DIR": ""}):
                baseline = select_initial_placement(g, c, {"layers": 2}, **args)[0]
            with patch.dict(
                os.environ,
                {
                    "PNR_COST_CAPTURE_DIR": str(root / "cost"),
                    "PNR_LIVE_DIR": str(root / "live"),
                    "PNR_LIVE_CANDIDATE": "test/source",
                    "PNR_LIVE_ITERATION": "7",
                },
            ):
                before = dict(os.environ)
                actual = select_initial_placement(g, c, {"layers": 2}, **args)[0]
                self.assertEqual(dict(os.environ), before)
            self.assertEqual(baseline.to_json(), actual.to_json())
            events = [json.loads(p.read_text()) for p in (root / "live/events").glob("*.json")]
            captures = [e for e in events if e["kind"] == "placement_cost_capture"]
            self.assertEqual(len(captures), 4)
            for e in captures:
                context = e["data"]["placement_context"]
                record = json.loads(Path(e["data"]["cost_capture"]["path"]).read_text())
                self.assertEqual(context, record["phase_context"])
                self.assertEqual(e["candidate"], "test/source/initial-" + context["initial_start"])
                self.assertEqual(context["iteration"], "7")
            self.assertEqual(
                {e["data"]["placement_context"]["initial_seed"] for e in captures}, {"17", "18"}
            )

    def test_failed_pool_restores_lane(self):
        g, c = fixture()
        starts = [dict(id="start-00", seed=1, kind="legacy-global", positions={}, rotations={})]
        with patch.dict(os.environ, {"PNR_LIVE_CANDIDATE": "test/source"}), patch(
            "pnr.place.initial_pool.initial_starts", return_value=starts
        ), patch("pnr.place.initial_pool.place", side_effect=LegalizationError("blocked")):
            before = dict(os.environ)
            with self.assertRaises(LegalizationError):
                select_initial_placement(
                    g,
                    c,
                    {"layers": 2},
                    config=InitialPoolConfig(starts=2, route_finalists=1, proxy_budget=2),
                )
            self.assertEqual(dict(os.environ), before)


if __name__ == "__main__":
    unittest.main()
