"""Sharding helpers: the benchmark cells, Monte Carlo stages and RF runs expand into the tasks
the design describes, and their assembly writes the layouts the local tools read."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from yapnr.exp import kinds, spec, testing
from yapnr.exp.kinds import mc

CID = "20261002-bench-abcdef"


def context(tmp, visibility="public", **resources):
    return kinds.Context(
        campaign_id=CID if visibility == "public" else "20261002-p-abcdef",
        image="ghcr.io/studio-fug/yapnr@" + testing.DIGEST,
        visibility=visibility,
        determinism="wall_clock_budgeted",
        resources=dict(dict(cpus=1, memory_gb=3, disk_gb=4, max_wall_s=3600), **resources),
        source_input={"dest": "src", "bundle": "a" * 64, "kind": "source"},
        base_dir=Path(tmp),
        bundle_dir=Path(tmp) / "bundles",
    )


BENCH = {
    "schema": spec.CAMPAIGN_SCHEMA,
    "kind": "bench-cell",
    "matrix": {"rung": ["r1", "r2"], "tool": ["yapnr", "freerouting"], "seed": [0]},
    "tools": {
        "yapnr": {
            "command": ["${PYTHON}", "route.py", "{rung_dir}", "--out", "{out}"],
            "judge": ["${KICAD_PYTHON}", "measure.py", "{out}"],
        },
        "freerouting": {
            "command": ["java", "-jar", "fr.jar", "-mt", "4", "{rung_dir}"],
            "cpus": 4,
            "image": "ghcr.io/studio-fug/yapnr-bench-freerouting@" + testing.DIGEST,
        },
    },
}


class BenchTest(unittest.TestCase):
    def test_expand(self):
        kind = kinds.get("bench-cell")
        self.assertEqual(kind.check(BENCH), [])
        with tempfile.TemporaryDirectory() as tmp:
            tasks = kind.expand(BENCH, context(tmp))
        self.assertEqual(
            [t["id"] for t in tasks],
            [
                "bench/r1/yapnr/s0",
                "bench/r1/freerouting/s0",
                "bench/r2/yapnr/s0",
                "bench/r2/freerouting/s0",
            ],
        )
        yapnr, freerouting = tasks[0], tasks[1]
        script = yapnr["command"][2]
        self.assertIn("route.py bench/r1 --out out/r1/results/yapnr-s0", script)
        self.assertIn("measure.py out/r1/results/yapnr-s0", script)
        self.assertEqual(yapnr["env"]["OMP_NUM_THREADS"], "1")
        self.assertEqual(freerouting["resources"]["cpus"], 4)
        self.assertNotIn("OMP_NUM_THREADS", freerouting["env"])
        self.assertTrue(
            freerouting["image"].startswith("ghcr.io/studio-fug/yapnr-bench-freerouting@")
        )

    def test_unknown_tool_and_options_are_reported(self):
        campaign = dict(
            BENCH, matrix=dict(BENCH["matrix"], tool=["kicad"]), tools={"yapnr": {"cmd": []}}
        )
        errors = kinds.get("bench-cell").check(campaign)
        self.assertTrue(any("no [tools.kicad]" in e for e in errors), errors)
        self.assertTrue(any("tools.yapnr.cmd" in e for e in errors), errors)

    def test_assembly_writes_the_harness_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            tasks = kinds.get("bench-cell").expand(BENCH, context(tmp))
            fetched = tmp / "fetched"
            task = tasks[0]
            base = fetched / task["id"].replace("/", "~")
            (base / "summary" / "r1" / "results" / "yapnr-s0").mkdir(parents=True)
            (base / "summary" / "r1" / "results" / "yapnr-s0" / "measure.json").write_text("{}")
            (base / "_DONE").write_text("{}")
            report = kinds.get("bench-cell").assemble({}, tasks, fetched, tmp / "out")
            self.assertEqual(report["tasks"], 1)
            self.assertEqual(len(report["missing"]), 3)
            self.assertTrue(
                (tmp / "out" / "r1" / "results" / "yapnr-s0" / "measure.json").is_file()
            )
            cells = json.loads((tmp / "out" / "cells.json").read_text())["cells"]
            self.assertEqual(cells[0]["tool"], "yapnr")


class MonteCarloTest(unittest.TestCase):
    def test_stage_plan_becomes_evaluation_tasks_with_bundles(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "cand-03").mkdir()
            (tmp / "cand-03" / "placement.json").write_text("{}")
            lines = [
                {
                    "id": "rung1/cand-03",
                    "command": ["${PYTHON}", "-m", "pnr.full_iteration", "--out", "out/eval"],
                    "inputs": [{"dest": "candidate", "path": "cand-03"}],
                    "record": "out/eval/record.json",
                    "resources": {"cpus": 2, "memory_gb": 8, "max_wall_s": 7200},
                    "labels": {"rung": "1"},
                },
                {
                    "id": "rung1/cand-04",
                    "command": ["${PYTHON}", "-m", "pnr.full_iteration"],
                    "record": "out/eval/record.json",
                },
            ]
            (tmp / "stage.jsonl").write_text("".join(json.dumps(line) + "\n" for line in lines))
            campaign = {
                "schema": spec.CAMPAIGN_SCHEMA,
                "kind": "mc-eval",
                "config": {"stage_plan": "stage.jsonl"},
            }
            kind = kinds.get("mc-eval")
            self.assertEqual(kind.check(campaign), [])
            ctx = context(
                tmp, visibility="private", cpus=2, memory_gb=8, disk_gb=10, max_wall_s=14400
            )
            tasks = kind.expand(campaign, ctx)
            self.assertEqual(len(tasks), 2)
            for task in tasks:
                self.assertRegex(task["id"], r"^p/[0-9a-f]{16}$")
            first = tasks[0]
            self.assertEqual([i["dest"] for i in first["inputs"]], ["src", "candidate"])
            self.assertEqual(first["inputs"][1]["kind"], "private")
            self.assertTrue(
                (tmp / "bundles" / ("%s.tar.gz" % first["inputs"][1]["bundle"])).is_file()
            )
            self.assertEqual(first["resources"]["max_wall_s"], 7200)
            self.assertEqual(tasks[1]["resources"]["max_wall_s"], 14400)
            self.assertEqual(first["labels"]["candidate"], "rung1/cand-03")
            # Assembly: the records in campaign order, ready for the halving driver's import.
            fetched = tmp / "fetched"
            base = fetched / first["id"].replace("/", "~")
            (base / "summary" / "eval").mkdir(parents=True)
            (base / "summary" / "eval" / "record.json").write_text(json.dumps({"score": 1.5}))
            (base / "_DONE").write_text("{}")
            report = kind.assemble({}, tasks, fetched, tmp / "out")
            dataset = [
                json.loads(x) for x in (tmp / "out" / "dataset.jsonl").read_text().splitlines()
            ]
            self.assertEqual(dataset[0]["candidate"], "rung1/cand-03")
            self.assertEqual(dataset[0]["record"], {"score": 1.5})
            self.assertEqual(report["missing"], [tasks[1]["id"]])

    def test_malformed_stage_plan_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stage.jsonl"
            path.write_text(
                json.dumps({"id": "x", "command": [], "record": "elsewhere.json"}) + "\n"
            )
            with self.assertRaises(ValueError):
                mc.read_stage_plan(path)


class RfTest(unittest.TestCase):
    def test_rf_runs_are_resumable(self):
        campaign = {
            "schema": spec.CAMPAIGN_SCHEMA,
            "kind": "rf-run",
            "matrix": {"case": ["div", "hybrid"]},
            "config": {"sync_every_s": 120},
        }
        kind = kinds.get("rf-run")
        self.assertEqual(kind.check(campaign), [])
        with tempfile.TemporaryDirectory() as tmp:
            tasks = kind.expand(campaign, context(tmp, cpus=4))
        task = tasks[0]
        self.assertEqual(task["restart"], "resume")
        self.assertEqual(
            task["checkpoint"], {"path": "out/div", "sync_every_s": 120, "on_signal": True}
        )
        self.assertEqual(task["env"]["OMP_NUM_THREADS"], "4")
        self.assertEqual(task["command"][:4], ["${PYTHON}", "-m", "yapnr.rf.cases", "run"])


if __name__ == "__main__":
    unittest.main()
