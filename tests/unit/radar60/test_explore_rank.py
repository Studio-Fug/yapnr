"""examples/radar60/board/explore_rank.py: the ranking table over a fetched explore_jobs.toml
campaign's assembled/dataset.jsonl."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

EXAMPLE = Path(__file__).parents[3] / "examples/radar60"
sys.path.insert(0, str(EXAMPLE / "board"))

import explore_rank  # noqa: E402

OK_RECORD = {
    "ok": True,
    "wall_s": 700.0,
    "data": {
        "measure": {
            "drc_by_type": {"isolated_copper": 1, "via_dangling": 3},
            "drc_ignored_by_default": {"missing_courtyard": 5},
            "unconnected": 134,
            "nets_connected": 7,
            "nets_total": 7,
            "diff_pairs_connected_legs": 0,
            "diff_pairs_total_legs": 8,
            "qspi_max_length_mm": 21.3,
            "ir": {
                "1V0_BUCK": {"status": "fail"},
                "1V0_SH": {"status": "pass"},
            },
            "r1_macro": {"equal": True},
        }
    },
}
FAILED_RECORD = {
    "ok": False,
    "wall_s": 260.0,
    "steps": [{"step": "place", "returncode": 1}],
    "data": {"measure": None},
}


class RowTest(unittest.TestCase):
    def test_an_ok_task_reads_every_measure_field(self):
        r = explore_rank.row({"candidate": "cand-arm-a", "record": OK_RECORD})
        self.assertTrue(r["ok"])
        self.assertEqual(r["drc_total"], 4)
        self.assertEqual(r["drc_ignored_by_default"], 5)
        self.assertEqual(r["unconnected"], 134)
        self.assertEqual(r["diff_pairs_legs"], "0/8")
        self.assertEqual(r["ir_fails"], "1V0_BUCK")
        self.assertEqual(r["r1_macro"], {"equal": True})

    def test_a_failed_task_names_the_step_that_failed(self):
        r = explore_rank.row({"candidate": "s0", "record": FAILED_RECORD})
        self.assertFalse(r["ok"])
        self.assertEqual(r["failed_step"], "place")
        self.assertNotIn("drc_total", r)

    def test_no_ir_failures_is_a_dash(self):
        record = json.loads(json.dumps(OK_RECORD))
        record["data"]["measure"]["ir"] = {"1V0_SH": {"status": "pass"}}
        r = explore_rank.row({"candidate": "cand-arm-a", "record": record})
        self.assertEqual(r["ir_fails"], "-")


class RenderTest(unittest.TestCase):
    def test_render_is_a_markdown_table_with_every_present_column(self):
        rows = [
            explore_rank.row({"candidate": "cand-arm-a", "record": OK_RECORD}),
            explore_rank.row({"candidate": "s0", "record": FAILED_RECORD}),
        ]
        text = explore_rank.render(rows)
        lines = text.splitlines()
        self.assertTrue(lines[0].startswith("| task |"))
        self.assertIn("failed_step", lines[0])
        self.assertIn("cand-arm-a", lines[2])
        self.assertIn("s0", lines[3])


class MainTest(unittest.TestCase):
    def test_exit_code_is_1_when_any_task_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lines = [
                json.dumps({"candidate": "cand-arm-a", "record": OK_RECORD}),
                json.dumps({"candidate": "s0", "record": FAILED_RECORD}),
            ]
            (root / "dataset.jsonl").write_text("\n".join(lines) + "\n")
            self.assertEqual(explore_rank.main([str(root)]), 1)
            self.assertEqual(explore_rank.main([str(root), "--json"]), 1)

    def test_exit_code_is_0_when_every_task_passed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "dataset.jsonl").write_text(
                json.dumps({"candidate": "cand-arm-a", "record": OK_RECORD}) + "\n"
            )
            self.assertEqual(explore_rank.main([str(root)]), 0)


if __name__ == "__main__":
    unittest.main()
