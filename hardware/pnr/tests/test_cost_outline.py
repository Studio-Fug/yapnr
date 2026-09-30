import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pnr.constraints import compile_constraints
from pnr.graph import BoardGraph, BoardOutline, Component, Net, Pad
from pnr.place.cost_capture import record_global_loss
from pnr.place.model import global_place


class CostOutlineTest(unittest.TestCase):
    def fixture(self):
        g = BoardGraph(
            "source-staging",
            [
                Component(
                    "U1",
                    "",
                    (0, 0),
                    0,
                    "top",
                    (2, 2),
                    (2, 2),
                    pads=[Pad("1", "N", (0, 0), (0.5, 0.5))],
                ),
                Component(
                    "C1",
                    "",
                    (6, 0),
                    0,
                    "top",
                    (1, 1),
                    (1, 1),
                    pads=[Pad("1", "N", (0, 0), (0.5, 0.5))],
                ),
            ],
            [Net("N", 1, [("U1", "1"), ("C1", "1")])],
            BoardOutline(1347, 7),
        )
        c = compile_constraints({"board": {"outline": {"w": 70, "h": 55}}}, g.refs)
        return g, c

    def test_actual_bounds_override_compiler_staging_outline(self):
        g, c = self.fixture()
        before = g.to_json()
        with tempfile.TemporaryDirectory() as d:
            with patch.dict(os.environ, {"PNR_COST_CAPTURE_DIR": "", "PNR_LIVE_DIR": ""}):
                a = global_place(g, c, 70, 55, seed=1, iters=15, inflation={"U1": 1.8})
            with patch.dict(os.environ, {"PNR_COST_CAPTURE_DIR": d, "PNR_LIVE_DIR": ""}):
                b = global_place(g, c, 70, 55, seed=1, iters=15, inflation={"U1": 1.8})
            self.assertEqual(a, b)
            self.assertEqual(g.to_json(), before)
            records = [json.loads(p.read_text()) for p in Path(d).glob("*.json")]
            self.assertEqual(len(records), 1)
            cap = records[0]
            self.assertEqual(cap["kind"], "global-objective")
            self.assertEqual(cap["graph"]["outline"]["width"], 70)
            self.assertEqual(cap["graph"]["outline"]["height"], 55)
            self.assertAlmostEqual(
                cap["report"]["board_total"], cap["report"]["actual_optimizer_loss"], places=3
            )

    def test_failed_diagnostic_cannot_abort_routing_or_claim_score(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(
            os.environ, {"PNR_COST_CAPTURE_DIR": d}
        ), patch(
            "pnr.place.cost_capture.global_loss", side_effect=ValueError("injected mismatch")
        ), patch(
            "pnr.live.emit"
        ) as emit:
            self.assertIsNone(record_global_loss())
            report = json.loads(next(Path(d).glob("failed-*.json")).read_text())
            self.assertEqual(report["status"], "unavailable")
            self.assertNotIn("board_total", report)
            self.assertEqual(emit.call_args.args[0], "cost_capture_failed")


if __name__ == "__main__":
    unittest.main()
