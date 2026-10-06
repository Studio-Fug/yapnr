"""Tests for tools/ci/check_run_inputs.py: the artifact-completeness check for a downloaded
regression-ladder run (ladder.yaml's animations job)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.ci import check_run_inputs


def _write_run(root, cases):
    """``cases``: {name: {"seed": int, "files": [...], "dirs": {name: [child, ...]}}}."""
    results = []
    for name, spec in cases.items():
        directory = root / name
        directory.mkdir()
        for filename in spec.get("files", []):
            (directory / filename).write_text("{}")
        for dirname, children in spec.get("dirs", {}).items():
            sub = directory / dirname
            sub.mkdir()
            for child in children:
                (sub / child).write_text("x")
        results.append({"case": name, "seed": spec.get("seed", 0), "directory": str(directory)})
    (root / "summary.json").write_text(json.dumps({"results": results}))


class CheckRunInputsTest(unittest.TestCase):
    def test_all_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_run(
                root,
                {
                    "01-connector-led-2": {
                        "files": ["design.json", "result.json"],
                        "dirs": {"trace": ["0.json"]},
                    }
                },
            )
            missing = check_run_inputs.missing_inputs(
                root, files=["design.json", "result.json"], dirs=["trace"]
            )
            self.assertEqual(missing, [])

    def test_missing_file_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_run(root, {"01-connector-led-2": {"files": ["result.json"]}})
            missing = check_run_inputs.missing_inputs(root, files=["design.json", "result.json"])
            self.assertEqual(missing, [("01-connector-led-2", 0, "design.json")])

    def test_empty_directory_counts_as_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_run(root, {"01-connector-led-2": {"dirs": {"trace": []}}})
            missing = check_run_inputs.missing_inputs(root, dirs=["trace"])
            self.assertEqual(missing, [("01-connector-led-2", 0, "trace/")])

    def test_seed_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_run(
                root,
                {
                    "case-seed-0": {"seed": 0, "files": []},
                    "case-seed-1": {"seed": 1, "files": ["design.json"]},
                },
            )
            missing = check_run_inputs.missing_inputs(root, files=["design.json"], seeds=[1])
            self.assertEqual(missing, [])

    def test_main_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            incomplete = Path(tmp) / "incomplete"
            incomplete.mkdir()
            _write_run(incomplete, {"01-connector-led-2": {"files": []}})
            self.assertEqual(check_run_inputs.main([str(incomplete), "--file", "design.json"]), 1)

            complete = Path(tmp) / "complete"
            complete.mkdir()
            _write_run(complete, {"01-connector-led-2": {"files": ["design.json"]}})
            self.assertEqual(check_run_inputs.main([str(complete), "--file", "design.json"]), 0)

    def test_main_requires_at_least_one_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                check_run_inputs.main([tmp])

    def test_missing_summary_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(check_run_inputs.main([tmp, "--file", "design.json"]), 1)


if __name__ == "__main__":
    unittest.main()
