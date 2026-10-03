"""tools/exp/rf_stage_plan.py: RF jobs become an mc-eval campaign whose lines `yapnr exp` takes,
with a source bundle of the committed revision only; the runner and the diagnostic it ships run
against a stand-in yapnr.rf."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import tomllib
import unittest
from pathlib import Path

from tools.exp import rf_stage_plan
from yapnr.exp import kinds, spec, testing

ENGINE = "torch.set_num_threads(max(1, min(4, int(threads))))\n"

# A stand-in for yapnr.rf: a frozen spec, and a cases runner that records what it ran.
SPEC_PY = """
import dataclasses, json

@dataclasses.dataclass(frozen=True)
class Opt:
    seed: object = None

@dataclasses.dataclass(frozen=True)
class Solver:
    threads: int = 4

@dataclasses.dataclass(frozen=True)
class Spec:
    name: str
    optimizer: Opt = Opt()
    solver: Solver = Solver()

    @classmethod
    def load(cls, path):
        return cls(name=json.load(open(path))["name"])
"""
CASES_PY = """
import argparse, dataclasses, json, os
from yapnr.rf import driver
from yapnr.rf.spec import Spec
CASES = {"divider": None}

@dataclasses.dataclass(frozen=True)
class Check:
    name: str
    kind: str
    ports: tuple = ()
    limit: float = 0.0
    ghz: object = None
    at_ghz: object = None

CRITERIA = {"divider": {"coarse": [Check("S21", "s_min", (2, 1), -3.5, (9.5, 10.5))], "fine": []}}
DENSE_GHZ = {"divider": [9.0, 10.0, 11.0]}

def spec_for(case, scale="full"):
    return Spec(name=case if scale == "full" else case + "-smoke")

def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m yapnr.rf.cases")
    ap.add_argument("action")
    ap.add_argument("case", choices=sorted(CASES))
    ap.add_argument("--out")
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--max-iterations", type=int)
    ap.add_argument("--finer", type=int)
    args = ap.parse_args(argv)
    if args.action == "run":
        s = spec_for(args.case, "smoke" if args.smoke else "full")
        os.makedirs(args.out, exist_ok=True)
        json.dump({"name": s.name}, open(os.path.join(args.out, "spec.json"), "w"))
        opt = driver.Optimizer(args.out)
        opt.run()
        opt.finish()
        record = dict(name=s.name, seed=s.optimizer.seed, threads=s.solver.threads,
                      max_iterations=args.max_iterations, iterations=len(opt.history))
    else:
        record = dict(name=json.load(open(os.path.join(args.out, "spec.json")))["name"],
                      files=sorted(os.listdir(args.out)))
    crit = CRITERIA[args.case]
    record.update(action=args.action, finer=args.finer, ok=True, dense=list(DENSE_GHZ[args.case]),
                  criteria={k: [dataclasses.astuple(c) for c in v] for k, v in crit.items()})
    json.dump(record, open(os.path.join(args.out, "validation.json"), "w"))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
"""
# A stand-in for the optimizer: FAKE_ITERATIONS iterations of FAKE_WALL seconds, checkpointed.
DRIVER_PY = """
import json, os, time
ITERATIONS = int(os.environ.get("FAKE_ITERATIONS", "2"))
WALL = float(os.environ.get("FAKE_WALL", "0"))

class Optimizer:
    def __init__(self, out_dir):
        self.out_dir = out_dir
        self.history = []

    def run(self):
        path = os.path.join(self.out_dir, "checkpoint.json")
        if os.path.exists(path):
            self.history = json.load(open(path))
        while len(self.history) < ITERATIONS:
            self.iterate()
            json.dump(self.history, open(path, "w"))

    def iterate(self):
        time.sleep(WALL)
        self.history.append({"iteration": len(self.history), "wall_s": WALL})
        return self.history[-1]

    def finish(self):
        return {}
"""


def git(repo, *args):
    env = dict(
        os.environ,
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@example.com",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@example.com",
    )
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))


def fake_rf(root):
    write(root / "yapnr" / "__init__.py", "")
    write(root / "yapnr" / "rf" / "__init__.py", "")
    write(root / "yapnr" / "rf" / "spec.py", SPEC_PY)
    write(root / "yapnr" / "rf" / "cases.py", CASES_PY)
    write(root / "yapnr" / "rf" / "driver.py", DRIVER_PY)
    write(root / "yapnr" / "rf" / "fdtd" / "engine.py", ENGINE)


JOBS = """
name = "rf-test"
sync_every_s = 120

[defaults]
memory_gb = 6

[placement]
vm_vcpus = 8

[[jobs]]
id = "divider"
case = "divider"

[[jobs]]
id = "d1-star"
case = "divider"
spec = "specs/d1.json"
seed = "star"
threads = 8
max_iterations = 3
smoke = true

[[jobs]]
id = "short"
case = "divider"
attempt_s = 0

[[jobs]]
id = "d1-fine"
case = "divider"
validate = "fetched/d1-star"
criteria = "specs/d1-criteria.json"
args = ["--finer", "0"]

[[jobs]]
id = "diag"
diagnostic = true
"""
CRITERIA_JSON = """
{"dense_ghz": [[4.0, 6.0, 3], 5.5],
 "coarse": [{"name": "S21", "kind": "s_min", "ports": [2, 1], "limit": -3.6,
             "ghz": [4.25, 5.75]}],
 "fine": []}
"""


class GenerateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        fake_rf(self.repo)
        git(self.repo, "init", "-q")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "rf")
        # Work in progress in the checkout never reaches the bundle.
        write(self.repo / "yapnr" / "rf" / "cases.py", "raise SystemExit('uncommitted')\n")
        write(self.root / "jobs" / "jobs.toml", JOBS)
        write(self.root / "jobs" / "specs" / "d1.json", '{"name": "d1"}')
        write(self.root / "jobs" / "specs" / "d1-criteria.json", CRITERIA_JSON)
        fetched = self.root / "jobs" / "fetched" / "d1-star"
        write(fetched / "spec.json", '{"name": "d1"}')
        write(fetched / "checkpoint.npz", "x")
        write(fetched / "validation.json", '{"ok": false}')  # the run's own report: left out
        write(fetched / "cache" / "big.npz", "cache")  # not a top-level file: left out

    def tearDown(self):
        self.tmp.cleanup()

    def generate(self, extended=True):
        out = self.root / ("plan" if extended else "plain")
        manifest = rf_stage_plan.generate(
            self.root / "jobs" / "jobs.toml", self.repo, "HEAD", out, extended=extended
        )
        lines = [json.loads(x) for x in (out / "stage.jsonl").read_text().splitlines()]
        return out, manifest, {line["id"]: line for line in lines}

    def test_lines_bundles_and_campaign(self):
        out, manifest, lines = self.generate()
        self.assertTrue(manifest["uncommitted_changes_left_out"])
        self.assertEqual(manifest["engine_thread_cap"], 4)
        # 8 threads, above the engine's 4; a spec of its own without criteria.
        self.assertEqual(len(manifest["warnings"]), 2, manifest["warnings"])
        bundle = out / "bundles" / manifest["source_bundle"]["path"]
        with tarfile.open(bundle) as tar:
            cases = tar.extractfile("yapnr/rf/cases.py").read().decode()
        self.assertIn("def spec_for", cases)

        divider = lines["divider"]
        # A run goes on over several attempts: it stops 1/24 of max_wall_s before the limit.
        self.assertEqual(
            divider["command"],
            ["${PYTHON}", "job/rf_job.py", "divider", "--out", "out/divider"]
            + ["--attempt-s", "13800", "--end-s", "3600"],
        )
        self.assertEqual([i["dest"] for i in divider["inputs"]], ["src", "job"])
        self.assertEqual(divider["env"]["OMP_NUM_THREADS"], "4")
        self.assertEqual(divider["env"]["OPENBLAS_NUM_THREADS"], "1")
        self.assertEqual(
            divider["resources"], dict(cpus=4, memory_gb=6, disk_gb=10, max_wall_s=14400)
        )
        self.assertEqual(divider["checkpoint"], {"path": "out/divider", "sync_every_s": 120})
        self.assertEqual(divider["record"], "out/divider/validation.json")

        # attempt_s = 0: one attempt, the cases runner itself.
        self.assertEqual(
            lines["short"]["command"],
            ["${PYTHON}", "-m", "yapnr.rf.cases", "run", "divider", "--out", "out/short"],
        )

        d1 = lines["d1-star"]
        self.assertEqual(d1["command"][:2], ["${PYTHON}", "job/rf_job.py"])
        self.assertIn("job/specs/d1-star.json", d1["command"])
        self.assertEqual(d1["command"][-3:], ["--smoke", "--max-iterations", "3"])
        self.assertEqual(
            d1["command"][-9:-3],
            ["--threads", "8"] + ["--attempt-s", "13800"] + ["--end-s", "3600"],
        )
        self.assertEqual(d1["resources"]["cpus"], 8)
        self.assertTrue((out / "job" / "specs" / "d1-star.json").is_file())
        self.assertTrue((out / "job" / "rf_job.py").is_file())

        fine = lines["d1-fine"]
        self.assertEqual(
            fine["command"],
            ["${PYTHON}", "job/rf_job.py", "divider", "--out", "out/d1-fine"]
            + ["--validate-from", "run", "--criteria", "job/criteria/d1-fine.json"]
            + ["--finer", "0"],
        )
        self.assertIn({"dest": "run", "path": "runs/d1-fine"}, fine["inputs"])
        self.assertNotIn("checkpoint", fine)  # not resumable: it starts again after a preemption
        self.assertEqual(
            sorted(p.name for p in (out / "runs" / "d1-fine").iterdir()),
            ["checkpoint.npz", "spec.json"],
        )

        diag = lines["diag"]
        self.assertEqual(diag["resources"]["cpus"], 1)
        self.assertNotIn("checkpoint", diag)
        self.assertIn("vm_vcpus = 8", (out / "campaign.toml").read_text())

        # yapnr exp takes the campaign and its lines.
        campaign = tomllib.loads((out / "campaign.toml").read_text())
        self.assertEqual(spec.campaign_errors(campaign), [])
        kind = kinds.get("mc-eval")
        self.assertEqual(kind.check(campaign), [])
        ctx = kinds.Context(
            campaign_id="20261003-mceval-abcdef",
            image="ghcr.io/studio-fug/yapnr@" + testing.DIGEST,
            visibility="public",
            determinism="seeded",
            resources=dict(cpus=2, memory_gb=8, disk_gb=10, max_wall_s=14400),
            source_input=None,
            base_dir=out,
            bundle_dir=self.root / "bundles",
        )
        tasks = {t["labels"]["candidate"]: t for t in kind.expand(campaign, ctx)}
        self.assertEqual(tasks["divider"]["restart"], "resume")
        self.assertEqual(tasks["d1-fine"]["restart"], "scratch")
        self.assertEqual(tasks["diag"]["restart"], "scratch")
        self.assertEqual(tasks["d1-star"]["verdict"]["json_path"], "ok")

    def test_plain_lines_leave_out_the_new_keys(self):
        _, manifest, lines = self.generate(extended=False)
        self.assertFalse(manifest["resumable"])
        for line in lines.values():
            self.assertFalse(set(line) & {"checkpoint", "prune", "verdict"})
            # Without checkpoints an attempt that stops early would start again: one attempt.
            self.assertNotIn("--attempt-s", line["command"])
        self.assertEqual(lines["divider"]["command"][1:3], ["-m", "yapnr.rf.cases"])

    def test_bad_jobs_are_refused(self):
        errors = rf_stage_plan.check_jobs(
            {"name": "x", "jobs": [{"id": "a"}, {"id": "a", "case": "c", "spec": "s.txt"}]}
        )
        self.assertEqual(len(errors), 3)  # no case; a repeated id; not a spec file
        errors = rf_stage_plan.check_jobs(
            {
                "name": "x",
                "jobs": [
                    {"id": "v", "case": "c", "validate": "run", "seed": "star"},
                    {"id": "t", "case": "c", "attempt_s": 1.5, "criteria": "c.txt"},
                ],
            }
        )
        self.assertEqual(len(errors), 3, errors)  # seed with validate; not seconds; not a file
        with self.assertRaises(rf_stage_plan.JobError):
            rf_stage_plan.attempt_times({"attempt_s": 600}, 600)  # not below max_wall_s

    def work_dir(self, out):
        work = self.root / "work"
        with tarfile.open(next((out / "bundles").iterdir())) as tar:
            safe = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
            tar.extractall(work / "src", **safe)
        for name in ("job", "runs"):
            for path in (out / name).rglob("*"):
                if path.is_file():
                    target = work / path.relative_to(out)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(path.read_bytes())
        (work / "run").symlink_to(work / "runs" / "d1-fine")
        return work

    def run_line(self, work, line, **env):
        command = [sys.executable if x == "${PYTHON}" else x for x in line["command"]]
        env = dict(os.environ, PYTHONPATH="src", **env)
        return subprocess.run(
            command, cwd=work, env=env, capture_output=True, text=True, timeout=300
        )

    def test_runner_and_diagnostic_in_a_work_directory(self):
        out, _, lines = self.generate()
        work = self.work_dir(out)
        for jid in ("d1-star", "d1-fine"):
            proc = self.run_line(work, lines[jid])
            self.assertEqual(proc.returncode, 0, jid + proc.stdout + proc.stderr)
        self.run_line(work, lines["diag"])  # not ok without numpy, torch and PyYAML here
        record = json.loads((work / "out" / "d1-star" / "validation.json").read_text())
        self.assertEqual(
            {k: record[k] for k in ("name", "seed", "threads", "max_iterations", "action")},
            dict(name="d1", seed="star", threads=8, max_iterations=3, action="run"),
        )
        self.assertEqual(record["dense"], [9.0, 10.0, 11.0])  # the case's own criteria
        # The re-validation: the run's files without its report, the criteria file's checks.
        record = json.loads((work / "out" / "d1-fine" / "validation.json").read_text())
        self.assertEqual(record["action"], "validate")
        self.assertEqual(record["files"], ["checkpoint.npz", "spec.json"])
        self.assertEqual(record["finer"], 0)
        self.assertEqual(record["dense"], [4.0, 5.0, 5.5, 6.0])
        self.assertEqual(
            record["criteria"]["coarse"], [["S21", "s_min", [2, 1], -3.6, [4.25, 5.75], None]]
        )
        diag = json.loads((work / "out" / "diag" / "diag.json").read_text())
        self.assertTrue(diag["from_bundle"])
        self.assertEqual(diag["cases"], ["divider"])
        self.assertEqual(diag["cases_help"]["exit_code"], 0)
        self.assertEqual(diag["compile"]["failures"], [])

    def test_a_long_run_goes_on_over_attempts(self):
        out, _, lines = self.generate()
        work = self.work_dir(out)
        line = dict(lines["divider"])
        # Attempts of 1 s, iterations of 0.4 s and an end that needs 0.5 s.
        line["command"] = line["command"][:5] + ["--attempt-s", "1", "--end-s", "0.5"]
        env = dict(FAKE_ITERATIONS="4", FAKE_WALL="0.4")
        checkpoint = work / "out" / "divider" / "checkpoint.json"
        codes, progress = [], []
        for _ in range(8):
            proc = self.run_line(work, line, **env)
            codes.append(proc.returncode)
            progress.append(len(json.loads(checkpoint.read_text())))
            if proc.returncode != 75:
                break
            self.assertIn("exit 75", proc.stderr)
        self.assertEqual(codes[-1], 0, codes)
        self.assertGreaterEqual(len(codes), 2, codes)  # 4 iterations and the end: 1.6 s at least
        # Every attempt but the one that only ends made progress, and none redid an iteration.
        self.assertEqual(progress[-1], 4)
        self.assertEqual(progress, sorted(progress))
        self.assertLessEqual(len(codes), 5, codes)
        record = json.loads((work / "out" / "divider" / "validation.json").read_text())
        self.assertEqual(record["iterations"], 4)


if __name__ == "__main__":
    unittest.main()
