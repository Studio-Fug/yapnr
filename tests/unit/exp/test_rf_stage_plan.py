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
import argparse, json, os
from yapnr.rf.spec import Spec
CASES = {"divider": None}

def spec_for(case, scale="full"):
    return Spec(name=case if scale == "full" else case + "-smoke")

def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m yapnr.rf.cases")
    ap.add_argument("action")
    ap.add_argument("case", choices=sorted(CASES))
    ap.add_argument("--out")
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--max-iterations", type=int)
    args = ap.parse_args(argv)
    s = spec_for(args.case, "smoke" if args.smoke else "full")
    os.makedirs(args.out)
    record = dict(name=s.name, seed=s.optimizer.seed, threads=s.solver.threads,
                  iterations=args.max_iterations, ok=True)
    json.dump(record, open(os.path.join(args.out, "validation.json"), "w"))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
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
id = "diag"
diagnostic = true
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
        self.assertEqual(len(manifest["warnings"]), 1)  # 8 threads, above the engine's 4
        bundle = out / "bundles" / manifest["source_bundle"]["path"]
        with tarfile.open(bundle) as tar:
            cases = tar.extractfile("yapnr/rf/cases.py").read().decode()
        self.assertIn("def spec_for", cases)

        plain = lines["divider"]
        self.assertEqual(
            plain["command"],
            ["${PYTHON}", "-m", "yapnr.rf.cases", "run", "divider", "--out", "out/divider"],
        )
        self.assertEqual([i["dest"] for i in plain["inputs"]], ["src"])
        self.assertEqual(plain["env"]["OMP_NUM_THREADS"], "4")
        self.assertEqual(plain["env"]["OPENBLAS_NUM_THREADS"], "1")
        self.assertEqual(
            plain["resources"], dict(cpus=4, memory_gb=6, disk_gb=10, max_wall_s=14400)
        )
        self.assertEqual(plain["checkpoint"], {"path": "out/divider", "sync_every_s": 120})
        self.assertEqual(plain["record"], "out/divider/validation.json")

        d1 = lines["d1-star"]
        self.assertEqual(d1["command"][:2], ["${PYTHON}", "job/rf_job.py"])
        self.assertIn("job/specs/d1-star.json", d1["command"])
        self.assertEqual(d1["command"][-3:], ["--smoke", "--max-iterations", "3"])
        self.assertEqual(d1["command"][-5:-3], ["--threads", "8"])
        self.assertEqual(d1["resources"]["cpus"], 8)
        self.assertTrue((out / "job" / "specs" / "d1-star.json").is_file())
        self.assertTrue((out / "job" / "rf_job.py").is_file())

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
        self.assertEqual(tasks["diag"]["restart"], "scratch")
        self.assertEqual(tasks["d1-star"]["verdict"]["json_path"], "ok")

    def test_plain_lines_leave_out_the_new_keys(self):
        _, manifest, lines = self.generate(extended=False)
        self.assertFalse(manifest["resumable"])
        for line in lines.values():
            self.assertFalse(set(line) & {"checkpoint", "prune", "verdict"})

    def test_bad_jobs_are_refused(self):
        errors = rf_stage_plan.check_jobs(
            {"name": "x", "jobs": [{"id": "a"}, {"id": "a", "case": "c", "spec": "s.txt"}]}
        )
        self.assertEqual(len(errors), 3)  # no case; a repeated id; not a spec file

    def test_runner_and_diagnostic_in_a_work_directory(self):
        out, _, lines = self.generate()
        work = self.root / "work"
        with tarfile.open(next((out / "bundles").iterdir())) as tar:
            safe = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
            tar.extractall(work / "src", **safe)
        (work / "job").mkdir()
        for path in (out / "job").rglob("*"):
            if path.is_file():
                target = work / "job" / path.relative_to(out / "job")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(path.read_bytes())
        env = dict(os.environ, PYTHONPATH="src")
        for jid in ("d1-star", "diag"):
            command = [sys.executable if x == "${PYTHON}" else x for x in lines[jid]["command"]]
            subprocess.run(command, cwd=work, env=env, capture_output=True, timeout=300)
        record = json.loads((work / "out" / "d1-star" / "validation.json").read_text())
        self.assertEqual(record, dict(name="d1", seed="star", threads=8, iterations=3, ok=True))
        diag = json.loads((work / "out" / "diag" / "diag.json").read_text())
        self.assertTrue(diag["from_bundle"])
        self.assertEqual(diag["cases"], ["divider"])
        self.assertEqual(diag["cases_help"]["exit_code"], 0)
        self.assertEqual(diag["compile"]["failures"], [])


if __name__ == "__main__":
    unittest.main()
