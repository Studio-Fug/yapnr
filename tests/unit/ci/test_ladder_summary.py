"""Tests for tools/ci/ladder_summary.py: the step summary of the regression ladder lane."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from tools.ci import ladder_summary

SUMMARY = {
    "passed": False,
    "complete": True,
    "source_changed_during_run": [],
    "results": [
        {
            "case": "01-connector-led-2",
            "seed": 0,
            "passed": True,
            "reasons": [],
            "opens": 0,
            "violations": {},
            "vias": 0,
            "copper_length_mm": 9.27,
            "elapsed_seconds": 4.5,
            "stages": {},
        },
        {
            "case": "05-timer-led-10",
            "seed": 1,
            "passed": False,
            "reasons": ["native_drc_violations"],
            "opens": 0,
            "violations": {"clearance": 4, "track_dangling": 1},
            "vias": 7,
            "copper_length_mm": 120.1,
            "elapsed_seconds": 9.5,
            "stages": {},
        },
        {
            "case": "06-chaser-14",
            "seed": 0,
            "passed": False,
            "reasons": ["stage_failure"],
            "error": "No such file: /somewhere/private",
            "stages": {"generate": 1.0, "place-route": 20.0},
            "elapsed_seconds": 21.0,
        },
    ],
}


class LadderSummaryTest(unittest.TestCase):
    def test_table(self):
        text = ladder_summary.render(SUMMARY)
        self.assertIn("**1 of 3 cases passed** (complete run).", text)
        self.assertIn("| 01-connector-led-2 | 0 | pass | 0 | 0 | 0 | 9.3 | 4.5 |  |", text)
        self.assertIn("| 5 (clearance 4, track_dangling 1) |", text)
        self.assertIn("stage_failure (writeback)", text)
        self.assertNotIn("/somewhere", text)

    def test_gloss_table(self):
        self.assertNotIn("Gloss stage", ladder_summary.render(SUMMARY))
        glossed = dict(SUMMARY["results"][0])
        glossed["gloss"] = {
            "kept": True,
            "flags": {},
            "summary": {
                "accepted_transactions": 3,
                "proposed_transactions": 4,
                "stop": None,
                "metrics_before": {"bends_all": 33, "length_mm": 35.61},
                "metrics_after": {"bends_all": 26, "length_mm": 35.04},
            },
            "outer_gate": {"passed": True, "reasons": []},
        }
        failed = dict(SUMMARY["results"][1], gloss={"kept": False, "status": "error"})
        text = ladder_summary.render(dict(SUMMARY, results=[glossed, failed]))
        self.assertIn("**Gloss stage** (PNR_GLOSS): 1 of 2 cases kept an edit.", text)
        self.assertIn(
            "| 01-connector-led-2 | 0 | yes | 3 of 4 | - | pass | 33 -> 26 | 35.6 -> 35.0 |", text
        )
        self.assertIn(
            "| 05-timer-led-10 | 1 | no | - of - | - | **error** | - -> - | - -> - |", text
        )

    def test_main_and_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(ladder_summary.main([str(run)]), 0)
                self.assertEqual(ladder_summary.main([str(run), "--check"]), 1)
            self.assertIn("did not start", out.getvalue())
            (run / "summary.json").write_text(json.dumps(SUMMARY))
            with redirect_stdout(io.StringIO()):
                self.assertEqual(ladder_summary.main([str(run)]), 0)
                self.assertEqual(ladder_summary.main([str(run), "--check"]), 1)
            passing = dict(SUMMARY, passed=True, results=SUMMARY["results"][:1])
            (run / "summary.json").write_text(json.dumps(passing))
            with redirect_stdout(io.StringIO()):
                self.assertEqual(ladder_summary.main([str(run), "--check"]), 0)


if __name__ == "__main__":
    unittest.main()
