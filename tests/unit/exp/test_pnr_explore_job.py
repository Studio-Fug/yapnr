"""tools/exp/pnr_explore_job.py: chains a stand-in ``integrate.py``-shaped driver's steps,
collects files and embeds JSON records into the result."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from tools.exp import pnr_explore_job

# A stand-in driver: `driver.py STEP --work WORK --out OUT [--fail]` writes WORK/STEP.marker
# (and, for "check", OUT/measure.json) and exits non-zero when told to.
DRIVER_PY = """
import argparse, json, sys
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("step")
ap.add_argument("--work", required=True)
ap.add_argument("--out", required=True)
ap.add_argument("--fail", action="store_true")
ap.add_argument("--placement")
a = ap.parse_args()
work = Path(a.work)
work.mkdir(parents=True, exist_ok=True)
(work / (a.step + ".marker")).write_text(a.placement or "ok")
if a.step == "check":
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "measure.json").write_text(json.dumps({"unconnected": 3, "drc": 0}))
if a.fail:
    sys.exit(7)
"""


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))


class RunStepsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.driver = self.root / "driver.py"
        write(self.driver, DRIVER_PY)

    def tearDown(self):
        self.tmp.cleanup()

    def test_steps_run_in_order_and_record_timing(self):
        out = self.root / "out" / "t0"
        work = self.root / "work"
        outcome = pnr_explore_job.run_steps(
            str(self.driver),
            sys.executable,
            ["--work", str(work), "--out", str(out)],
            [["place"], ["finish"]],
            out / "logs",
        )
        self.assertTrue(outcome["ok"])
        self.assertEqual([s["step"] for s in outcome["steps"]], ["place", "finish"])
        self.assertEqual(outcome["steps"][0]["returncode"], 0)
        self.assertTrue((work / "place.marker").is_file())
        self.assertTrue((work / "finish.marker").is_file())
        self.assertTrue((out / "logs" / "00-place.log").is_file())

    def test_a_failing_step_stops_the_chain(self):
        out = self.root / "out" / "t1"
        work = self.root / "work-fail"
        outcome = pnr_explore_job.run_steps(
            str(self.driver),
            sys.executable,
            ["--work", str(work), "--out", str(out)],
            [["place", "--fail"], ["finish"]],
            out / "logs",
        )
        self.assertFalse(outcome["ok"])
        self.assertEqual(len(outcome["steps"]), 1)
        self.assertEqual(outcome["steps"][0]["returncode"], 7)
        self.assertFalse((work / "finish.marker").exists())


class CollectEmbedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_collect_copies_files_and_directories(self):
        (self.root / "a.txt").write_text("a")
        (self.root / "sub").mkdir()
        (self.root / "sub" / "b.txt").write_text("b")
        dest = self.root / "dest"
        names = pnr_explore_job.collect_files(
            [str(self.root / "a.txt"), str(self.root / "sub")], dest
        )
        self.assertEqual(sorted(names), ["a.txt", "sub"])
        self.assertEqual((dest / "a.txt").read_text(), "a")
        self.assertEqual((dest / "sub" / "b.txt").read_text(), "b")

    def test_embed_reads_json_and_is_none_when_missing(self):
        path = self.root / "measure.json"
        path.write_text(json.dumps({"drc": 0}))
        data = pnr_explore_job.embed_data(
            [("measure", str(path)), ("missing", str(self.root / "x"))]
        )
        self.assertEqual(data, {"measure": {"drc": 0}, "missing": None})


class MainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.driver = self.root / "driver.py"
        write(self.driver, DRIVER_PY)

    def tearDown(self):
        self.tmp.cleanup()

    def test_end_to_end_success_with_seed_from_and_collect(self):
        prepared = self.root / "prepared"
        write(prepared / "seed.marker", "from-prepare")
        out = self.root / "out" / "s0"
        code = pnr_explore_job.main(
            [
                "--driver",
                str(self.driver),
                "--python",
                sys.executable,
                "--work-dir",
                str(self.root / "work"),
                "--seed-from",
                str(prepared),
                "--common",
                json.dumps(["--work", str(self.root / "work"), "--out", str(self.root / "board")]),
                "--step",
                json.dumps(["place"]),
                "--step",
                json.dumps(["finish"]),
                "--step",
                json.dumps(["check"]),
                "--collect",
                str(self.root / "work" / "place.marker"),
                "--embed",
                "measure=%s" % (self.root / "board" / "measure.json"),
                "--out",
                str(out),
            ]
        )
        self.assertEqual(code, 0)
        record = json.loads((out / "result.json").read_text())
        self.assertTrue(record["ok"])
        self.assertEqual([s["step"] for s in record["steps"]], ["place", "finish", "check"])
        self.assertEqual(record["data"]["measure"], {"unconnected": 3, "drc": 0})
        self.assertEqual(record["files"], ["place.marker"])
        # seed_from's own file survived the copy into the (fresh) work directory.
        self.assertTrue((self.root / "work" / "seed.marker").is_file())

    def test_failure_exit_code_and_ok_false(self):
        out = self.root / "out" / "s1"
        code = pnr_explore_job.main(
            [
                "--driver",
                str(self.driver),
                "--python",
                sys.executable,
                "--work-dir",
                str(self.root / "work2"),
                "--step",
                json.dumps(["place", "--fail"]),
                "--common",
                json.dumps(
                    ["--work", str(self.root / "work2"), "--out", str(self.root / "board2")]
                ),
                "--out",
                str(out),
            ]
        )
        self.assertEqual(code, 1)
        record = json.loads((out / "result.json").read_text())
        self.assertFalse(record["ok"])

    def test_work_placeholder_is_substituted_with_the_absolute_work_dir(self):
        # A driver whose subprocess cwd is pinned elsewhere (--engine, say) sees "{work}" as the
        # absolute --work-dir, not the (possibly relative) string this task was given.
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            code = pnr_explore_job.main(
                [
                    "--driver",
                    str(self.driver),
                    "--python",
                    sys.executable,
                    "--work-dir",
                    "relwork",
                    "--common",
                    json.dumps(["--work", "{work}", "--out", "{work}/board"]),
                    "--step",
                    json.dumps(["finish", "--placement", "{work}/token"]),
                    "--out",
                    "out/s2",
                ]
            )
        finally:
            os.chdir(previous)
        self.assertEqual(code, 0)
        record = json.loads((self.root / "out" / "s2" / "result.json").read_text())
        self.assertTrue(record["ok"])
        marker = (self.root / "relwork" / "finish.marker").read_text()
        self.assertEqual(marker, str((self.root / "relwork").resolve()) + "/token")


if __name__ == "__main__":
    unittest.main()
