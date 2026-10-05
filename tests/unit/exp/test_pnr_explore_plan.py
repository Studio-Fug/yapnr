"""tools/exp/pnr_explore_plan.py: N placement seeds and one candidate placement become an
mc-eval campaign against a stand-in `integrate.py`-shaped driver, with a source bundle of the
committed revision only."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import textwrap
import tomllib
import unittest
from pathlib import Path

from tools.exp import pnr_explore_plan
from yapnr.exp import kinds, spec, testing

# A stand-in board driver: enough of integrate.py's step dispatch to exercise the generator
# (the real one is examples/radar60/board/integrate.py; this module knows nothing about it).
DRIVER_PY = """
import argparse, json
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("step")
ap.add_argument("--work", required=True)
ap.add_argument("--out", required=True)
ap.add_argument("--engine")
ap.add_argument("--placement")
ap.add_argument("--seed")
ap.add_argument("--n0")
ap.add_argument("--top-n")
a = ap.parse_args()
Path(a.work).mkdir(parents=True, exist_ok=True)
(Path(a.work) / (a.step + ".marker")).write_text(a.placement or a.seed or "ok")
if a.step == "check":
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "measure.json").write_text(json.dumps({"drc": 0, "unconnected": 1}))
"""

JOBS = """
schema = "yapnr-pnr-explore-v1"
name = "board-explore"
driver = "board/driver.py"
paths = ["board"]
image = "edge"

[defaults]
cpus = 1
memory_gb = 2
disk_gb = 4
max_wall_s = 900
work_dir = "work"
common_args = ["--work", "work", "--out", "out/board", "--engine", "src"]
post_steps = [["route"], ["check"]]
collect = ["work/route.marker", "out/board/measure.json"]
embed = [["measure", "out/board/measure.json"]]

[[seeds]]
id = "s0"
place_args = ["--seed", "0", "--n0", "1"]

[[seeds]]
id = "s1"
place_args = ["--seed", "1", "--n0", "1"]
max_wall_s = 1800

[[candidates]]
id = "cand-a"
placement = "placements/a.json"
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


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))


class CheckJobsTest(unittest.TestCase):
    def test_wants_at_least_one_seed_or_candidate(self):
        doc = {"schema": "yapnr-pnr-explore-v1", "name": "x", "driver": "d.py", "paths": ["a"]}
        self.assertIn(
            "seeds or candidates must name at least one task", pnr_explore_plan.check_jobs(doc)
        )

    def test_rejects_an_unknown_top_level_key(self):
        doc = dict(
            json.loads("{}"),
            schema="yapnr-pnr-explore-v1",
            name="x",
            driver="d.py",
            paths=["a"],
            seeds=[{"id": "s"}],
            bogus=1,
        )
        errors = pnr_explore_plan.check_jobs(doc)
        self.assertTrue(any("bogus" in e for e in errors))

    def test_rejects_a_repeated_id(self):
        doc = {
            "schema": "yapnr-pnr-explore-v1",
            "name": "x",
            "driver": "d.py",
            "paths": ["a"],
            "seeds": [{"id": "s0"}, {"id": "s0"}],
        }
        errors = pnr_explore_plan.check_jobs(doc)
        self.assertTrue(any("repeated" in e for e in errors))


class GenerateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        write(self.repo / "board" / "driver.py", DRIVER_PY)
        git(self.repo, "init", "-q")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "board")
        # Uncommitted work in the checkout never reaches the bundle.
        write(self.repo / "board" / "driver.py", "raise SystemExit('uncommitted')\n")
        write(self.root / "jobs" / "jobs.toml", JOBS)
        write(self.root / "jobs" / "placements" / "a.json", '{"parts": {}}')

    def tearDown(self):
        self.tmp.cleanup()

    def generate(self):
        out = self.root / "plan"
        manifest = pnr_explore_plan.generate(
            self.root / "jobs" / "jobs.toml", self.repo, "HEAD", out
        )
        lines = [json.loads(x) for x in (out / "stage.jsonl").read_text().splitlines()]
        return out, manifest, {line["id"]: line for line in lines}

    def test_bundle_holds_the_committed_driver_not_the_dirty_one(self):
        out, manifest, _ = self.generate()
        self.assertTrue(manifest["uncommitted_changes_left_out"])
        self.assertEqual(manifest["tasks"], 3)
        import tarfile

        with tarfile.open(out / "bundles" / manifest["source_bundle"]["path"]) as tar:
            text = tar.extractfile("board/driver.py").read().decode()
        self.assertIn('ap.add_argument("step")', text)
        self.assertNotIn("uncommitted", text)

    def test_seed_task_chains_place_select_finish_and_the_post_steps(self):
        out, _, lines = self.generate()
        s0 = lines["s0"]
        self.assertEqual(s0["command"][:2], ["${PYTHON}", "job/pnr_explore_job.py"])
        self.assertIn("--driver", s0["command"])
        self.assertEqual(s0["command"][s0["command"].index("--driver") + 1], "src/board/driver.py")
        steps = [
            json.loads(s0["command"][i + 1]) for i, a in enumerate(s0["command"]) if a == "--step"
        ]
        self.assertEqual([s[0] for s in steps], ["place", "select", "finish", "route", "check"])
        self.assertEqual(steps[0], ["place", "--seed", "0", "--n0", "1"])
        self.assertEqual(steps[1], ["select", "--top-n", "1"])  # the generator's own default
        self.assertEqual([i["dest"] for i in s0["inputs"]], ["src", "job"])
        self.assertEqual(s0["resources"], dict(cpus=1, memory_gb=2, disk_gb=4, max_wall_s=900))
        self.assertEqual(s0["record"], "out/s0/result.json")
        self.assertEqual(s0["verdict"], {"file": "out/s0/result.json", "json_path": "ok"})
        self.assertEqual(s0["labels"], {"task": "s0", "kind": "seed"})
        self.assertTrue((out / "job" / "pnr_explore_job.py").is_file())

    def test_a_seed_entry_may_override_a_resource(self):
        _, _, lines = self.generate()
        self.assertEqual(lines["s1"]["resources"]["max_wall_s"], 1800)
        self.assertEqual(lines["s1"]["resources"]["cpus"], 1)  # the default, unchanged

    def test_candidate_task_finishes_from_its_bundled_placement(self):
        out, _, lines = self.generate()
        cand = lines["cand-a"]
        steps = [
            json.loads(cand["command"][i + 1])
            for i, a in enumerate(cand["command"])
            if a == "--step"
        ]
        self.assertEqual(steps[0], ["finish", "--placement", "candidate/placement.json"])
        self.assertIn({"dest": "candidate", "path": "candidates/cand-a"}, cand["inputs"])
        self.assertEqual(cand["labels"]["kind"], "candidate")
        self.assertEqual(
            json.loads((out / "candidates" / "cand-a" / "placement.json").read_text()),
            {"parts": {}},
        )

    def test_collect_and_embed_reach_the_command(self):
        _, _, lines = self.generate()
        s0 = lines["s0"]
        self.assertIn("--collect", s0["command"])
        self.assertIn("work/route.marker", s0["command"])
        self.assertIn("--embed", s0["command"])
        self.assertIn("measure=out/board/measure.json", s0["command"])

    def test_campaign_and_stage_lines_are_valid_for_yapnr_exp(self):
        out, _, lines = self.generate()
        campaign = tomllib.loads((out / "campaign.toml").read_text())
        self.assertEqual(spec.campaign_errors(campaign), [])
        self.assertEqual(campaign["kind"], "mc-eval")
        self.assertEqual(campaign["source"], "none")
        kind = kinds.get("mc-eval")
        self.assertEqual(kind.check(campaign), [])
        ctx = kinds.Context(
            campaign_id="20261005-mceval-abcdef",
            image="ghcr.io/studio-fug/yapnr@" + testing.DIGEST,
            visibility="public",
            determinism="seeded",
            resources=dict(cpus=2, memory_gb=8, disk_gb=10, max_wall_s=14400),
            source_input=None,
            base_dir=out,
            bundle_dir=self.root / "bundles",
        )
        tasks = {t["labels"]["candidate"]: t for t in kind.expand(campaign, ctx)}
        self.assertEqual(set(tasks), {"s0", "s1", "cand-a"})
        self.assertEqual(tasks["s0"]["verdict"]["json_path"], "ok")
        self.assertEqual(tasks["s1"]["resources"]["max_wall_s"], 1800)

    def test_seed_from_is_copied_and_wired_to_every_task(self):
        write(self.root / "jobs" / "prepared" / "source.marker", "sourced")
        write(
            self.root / "jobs" / "jobs2.toml",
            JOBS.replace('work_dir = "work"', 'work_dir = "work"\nseed_from = "prepared"'),
        )
        out = self.root / "plan2"
        manifest = pnr_explore_plan.generate(
            self.root / "jobs" / "jobs2.toml", self.repo, "HEAD", out
        )
        self.assertEqual(manifest["seed_from"]["path"], "seed_from")
        self.assertEqual((out / "seed_from" / "source.marker").read_text(), "sourced")
        lines = {
            json.loads(x)["id"]: json.loads(x)
            for x in (out / "stage.jsonl").read_text().splitlines()
        }
        self.assertIn("--seed-from", lines["s0"]["command"])
        self.assertIn({"dest": "work0", "path": "seed_from"}, lines["s0"]["inputs"])


if __name__ == "__main__":
    unittest.main()
