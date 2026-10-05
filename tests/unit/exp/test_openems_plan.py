"""tools/exp/openems_plan.py: openEMS models become an mc-eval campaign that runs in the openEMS
image (its own interpreter, no yapnr launcher), packed K models to a VM; the job runner records a
model run with openEMS's speed lines; collect lays fetched results out like local runs."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from tools.exp import openems_plan
from yapnr.exp import plan as planning
from yapnr.exp import testing
from yapnr.exp.backends import gcp_batch

IMAGE = "us-west4-docker.pkg.dev/example-project/images/openems:0.37.0-rc3-x86-64-v4@" + (
    testing.DIGEST
)
# A stand-in model script: openEMS's closing lines, and a result in --out.
MODEL_PY = """
import json, os, sys
out = sys.argv[sys.argv.index("--out") + 1]
os.makedirs(out, exist_ok=True)
print("Create FDTD engine (compressed SSE + multi-threading)")
print("Time for 42082 iterations with 2288132.00 cells : 971.52 sec")
print("Speed: 99.11 MCells/s ")
json.dump({"threads": sys.argv[-1], "omp": os.environ.get("OMP_NUM_THREADS")},
          open(os.path.join(out, "result.json"), "w"))
sys.exit(int(os.environ.get("MODEL_EXIT", "0")))
"""


class OpenemsPlanTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config = testing.load_config(self.tmp)
        (self.tmp / "code").mkdir()
        (self.tmp / "code" / "model.py").write_text(MODEL_PY)
        (self.tmp / "code" / "__pycache__").mkdir()
        (self.tmp / "models").mkdir()
        (self.tmp / "models" / "a.json").write_text("{}")
        self.jobs = self.tmp / "jobs.toml"
        self.jobs.write_text(
            textwrap.dedent(
                """
                name = "em-smoke"
                image = "%s"
                threads = 4
                models_per_vm = 2

                [inputs]
                code = "code"
                models = "models"

                [env]
                RFMACRO_ROOT = "w"

                [[jobs]]
                id = "tx12-A-e1"
                script = "code/model.py"
                args = ["models/a.json", "--out", "{out}", "--threads", "{threads}"]

                [[jobs]]
                id = "col12-A-e1"
                script = "code/model.py"
                args = ["models/a.json", "--out", "{out}", "--threads", "{threads}"]
                max_wall_s = 3600
                """
                % IMAGE
            )
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_models_become_a_campaign_in_the_openems_image(self):
        out = self.tmp / "camp"
        manifest = openems_plan.generate(self.jobs, out, IMAGE)
        self.assertEqual(manifest["jobs"], ["tx12-A-e1", "col12-A-e1"])
        self.assertEqual(
            manifest["placement"], {"families": ["c4d", "c4"], "packing": "core", "vm_vcpus": 16}
        )
        self.assertFalse((out / "inputs" / "code" / "__pycache__").exists())
        lines = [json.loads(x) for x in (out / "stage.jsonl").read_text().splitlines()]
        self.assertEqual(
            lines[0]["command"][:7],
            ["${PYTHON}", "job/openems_job.py", "--id", "tx12-A-e1", "--threads", "4", "--"],
        )
        self.assertEqual(lines[0]["env"]["OMP_NUM_THREADS"], "4")
        self.assertEqual(lines[0]["env"]["RFMACRO_ROOT"], "w")
        self.assertEqual(lines[1]["resources"]["max_wall_s"], 3600)
        self.config.gcp.ranking = list(testing.RANKING)
        plan = planning.make_plan(
            out / "campaign.toml",
            "gcp-batch",
            self.config,
            out=self.tmp / "plan",
            offline=True,
            today=testing.TODAY,
        )
        self.assertEqual(plan.meta["image"]["ref"], IMAGE.replace(":0.37.0-rc3-x86-64-v4", ""))
        self.assertEqual(plan.meta["image"]["runtime"], openems_plan.RUNTIME)
        # Every task shares the input bundles (uploaded once): job, code, models.
        self.assertEqual(len(plan.meta["bundles"]), 3)
        cls = plan.classes[0]
        placement = plan.placement(cls.name)
        self.assertEqual((placement.shape, placement.tasks_per_vm), ("c4d-highcpu-16", 2))
        # Both AVX-512 families: C4 in Montreal is where a submit spills when us-west4 is full.
        self.assertEqual(
            [(p.pair, p.shape) for p in plan.candidates(cls.name)],
            [
                ("c4d/us-west4", "c4d-highcpu-16"),
                ("c4/northamerica-northeast1", "c4-highcpu-16"),
                ("c4/us-west4", "c4-highcpu-16"),
            ],
        )
        job = gcp_batch.render_job(
            plan.meta, self.config, cls, placement, 1, len(cls.lines), testing.DEADLINE
        )
        container = job["taskGroups"][0]["taskSpec"]["runnables"][0]["container"]
        self.assertNotIn("entrypoint", container)
        self.assertEqual(container["commands"][0], "/opt/openEMS/venv/bin/python")

    def test_vcpu_packing_and_shapes_without_a_template(self):
        doc = dict(openems_plan.DEFAULTS, threads=4, models_per_vm=4)
        self.assertEqual(openems_plan.placement(doc)["vm_vcpus"], 32)
        self.assertIs(openems_plan.placement(doc)["template"], False)
        doc["packing"] = "vcpu"
        self.assertNotIn("template", openems_plan.placement(doc))

    def test_engine_and_openems_options_reach_the_job_runner(self):
        doc = openems_plan.load_jobs(self.jobs)
        doc["engine"] = "sse"
        doc["openems_options"] = ["exact-endcriteria"]
        doc["jobs"][1]["engine"] = "multithreaded"
        doc = dict(openems_plan.DEFAULTS, **doc)
        first, second = (openems_plan.stage_line(job, doc) for job in doc["jobs"])
        self.assertEqual(
            first["command"][2:11],
            ["--id", "tx12-A-e1", "--threads", "4", "--engine", "sse"]
            + ["--openems-option", "exact-endcriteria", "--"],
        )
        self.assertEqual(second["command"][7], "multithreaded")
        self.assertEqual(first["labels"], {"model": "tx12-A-e1", "threads": "4", "engine": "sse"})
        errors = openems_plan.check_jobs(
            dict(doc, engine="avx", jobs=[dict(doc["jobs"][0], openems_options=["a b"])])
        )
        self.assertEqual(len(errors), 2, errors)

    def test_bad_jobs_are_listed(self):
        errors = openems_plan.check_jobs(
            {"name": "X", "image": "", "inputs": {"job": "x"}, "jobs": [{"id": "a"}, {"id": "a"}]}
        )
        joined = "; ".join(errors)
        for text in ("name", "image", "'job'", "script", "repeated"):
            self.assertIn(text, joined)

    def run_job(self, work, exit_code="0"):
        (work / "job").mkdir(parents=True)
        (work / "job" / "openems_job.py").write_bytes(
            (Path(openems_plan.__file__).parent / "openems_job.py").read_bytes()
        )
        (work / "code").mkdir()
        (work / "code" / "model.py").write_text(MODEL_PY)
        argv = [sys.executable, "job/openems_job.py", "--id", "m1", "--threads", "3", "--"]
        argv += ["code/model.py", "--out", "{out}", "--threads", "{threads}"]
        env = {"PATH": "/usr/bin:/bin", "MODEL_EXIT": exit_code}
        return subprocess.run(argv, cwd=work, capture_output=True, text=True, env=env, timeout=60)

    def test_the_job_runner_records_the_run(self):
        work = self.tmp / "work"
        done = self.run_job(work)
        self.assertEqual(done.returncode, 0, done.stderr)
        record = json.loads((work / "out" / "m1.job.json").read_text())
        self.assertTrue(record["ok"])
        self.assertEqual(record["threads"], 3)
        self.assertEqual(record["engines"], ["compressed SSE + multi-threading"])
        self.assertEqual(
            record["openems_runs"],
            [{"cells": 2288132.0, "iterations": 42082, "mcells_per_s": 99.11, "seconds": 971.52}],
        )
        result = json.loads((work / "out" / "m1" / "result.json").read_text())
        self.assertEqual(result, {"threads": "3", "omp": "3"})
        self.assertTrue((work / "out" / "m1.log").read_text().endswith("exit 0\n"))
        failed = self.tmp / "failed"
        self.assertEqual(self.run_job(failed, "3").returncode, 0)  # a result, not a retry
        record = json.loads((failed / "out" / "m1.job.json").read_text())
        self.assertEqual((record["ok"], record["exit"]), (False, 3))

    def test_the_job_runner_hands_the_engine_to_every_openems_run(self):
        work = self.tmp / "engine"
        (work / "job").mkdir(parents=True)
        (work / "job" / "openems_job.py").write_bytes(
            (Path(openems_plan.__file__).parent / "openems_job.py").read_bytes()
        )
        # A stand-in openEMS package and a script that imports it as the model scripts do.
        (work / "openEMS").mkdir()
        (work / "openEMS" / "__init__.py").write_text(
            "class openEMS:\n"
            "    def Run(self, sim_path, **kw):\n"
            "        print('Run', sim_path, sorted(kw.items()))\n"
        )
        (work / "code").mkdir()
        (work / "code" / "helper.py").write_text("NAME = 'helper'\n")
        (work / "code" / "model.py").write_text(
            "import sys\n"
            "import helper\n"
            "from openEMS import openEMS\n"
            "openEMS().Run(sys.argv[1], numThreads=2)\n"
            "print(helper.NAME, sys.argv[1:], openEMS.__name__)\n"
        )
        argv = [sys.executable, "job/openems_job.py", "--id", "m1", "--threads", "2"]
        argv += ["--engine", "sse", "--openems-option", "exact-endcriteria", "--"]
        argv += ["code/model.py", "{out}"]
        env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(work)}
        done = subprocess.run(argv, cwd=work, capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        log = (work / "out" / "m1.log").read_text()
        self.assertIn(
            "Run out/m1 [('engine', 'sse'), ('exact_endcriteria', True), ('numThreads', 2)]", log
        )
        self.assertIn("helper ['out/m1'] openEMS", log)
        record = json.loads((work / "out" / "m1.job.json").read_text())
        self.assertTrue(record["ok"], log)
        self.assertEqual(record["openems_options"], {"engine": "sse", "exact_endcriteria": True})
        bad = argv[:6] + ["--engine", "avx", "--"] + argv[-2:]
        done = subprocess.run(bad, cwd=work, capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(done.returncode, 2)

    def test_collect_lays_results_out_like_local_runs(self):
        plan_dir = self.tmp / "plans" / "20261003-mceval-abcdef"
        plan_dir.mkdir(parents=True)
        tasks = [
            {"id": "mc/m1", "labels": {"model": "m1"}},
            {"id": "mc/m2", "labels": {"model": "m2"}},
        ]
        (plan_dir / "tasks.jsonl").write_text("".join(json.dumps(t) + "\n" for t in tasks))
        fetched = self.tmp / "fetched" / plan_dir.name
        root = fetched / "tasks" / "mc~m1" / "result" / "out"
        (root / "m1").mkdir(parents=True)
        (root / "m1" / "s.csv").write_text("f,s11\n")
        (root / "m1.log").write_text("exit 0\n")
        record = {"ok": True, "wall_s": 10.0, "openems_runs": [{"mcells_per_s": 250.0}]}
        (root / "m1.job.json").write_text(json.dumps(record))
        (fetched / "tasks" / "mc~m1" / "_DONE").write_text("{}")
        tree = self.tmp / "tree"
        summary = openems_plan.collect(plan_dir, fetched, tree)
        self.assertEqual(summary["missing"], ["m2"])
        self.assertEqual(summary["models"]["m1"]["mcells_per_s"], [250.0])
        self.assertTrue((tree / "runs" / "m1" / "s.csv").is_file())
        self.assertEqual((tree / "runs" / "m1.log").read_text(), "exit 0\n")
        self.assertTrue((tree / "runs" / "20261003-mceval-abcdef.summary.json").is_file())
        # A plan directory given by path (--plan) still names the summary by the campaign.
        moved = self.tmp / "elsewhere"
        plan_dir.rename(moved)
        summary = openems_plan.collect(moved, fetched, tree, "20261003-mceval-abcdef")
        self.assertEqual(summary["campaign"], "20261003-mceval-abcdef")
        self.assertFalse((tree / "runs" / "elsewhere.summary.json").exists())


if __name__ == "__main__":
    unittest.main()
