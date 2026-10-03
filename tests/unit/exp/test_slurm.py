"""The slurm backend: golden sbatch, submit.sh and status.sh scripts (bash -n, shellcheck when
installed), chunking against the site's array limit, and the private-campaign guard."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from yapnr.exp import plan as planning
from yapnr.exp import testing
from yapnr.exp.backends import slurm


class SlurmTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config = testing.load_config(self.tmp, slurm=True)
        self.repo = testing.fixture_repo(self.tmp)

    def tearDown(self):
        self._tmp.cleanup()

    def plan(self, text, **kw):
        path = testing.write_campaign(self.tmp / "campaign.toml", text)
        return planning.make_plan(
            path,
            "slurm",
            self.config,
            site="example-site",
            out=self.tmp / "plan",
            repo=self.repo,
            offline=True,
            image_digest=testing.DIGEST,
            today=testing.TODAY,
            **kw,
        )

    def check_shell(self, path):
        done = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        if shutil.which("shellcheck"):
            done = subprocess.run(
                ["shellcheck", "--severity=warning", str(path)],
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(done.returncode, 0, done.stdout)

    def test_rendered_scripts(self):
        plan = self.plan(testing.LADDER_CAMPAIGN)
        directory = plan.dir / "backend" / "slurm"
        self.assertEqual(
            sorted(p.name for p in directory.iterdir()),
            ["c1m3.indices", "c1m3.sbatch", "status.sh", "submit.sh"],
        )
        for name, golden in (
            ("c1m3.sbatch", "slurm-ladder-c1m3.sbatch.sh"),
            ("submit.sh", "slurm-ladder-submit.sh"),
            ("status.sh", "slurm-ladder-status.sh"),
        ):
            self.check_shell(directory / name)
            testing.check_golden(self, golden, (directory / name).read_text())
        self.assertEqual((directory / "c1m3.indices").read_text(), "0\n1\n2\n3\n")
        sbatch = (directory / "c1m3.sbatch").read_text()
        self.assertIn("#SBATCH --requeue", sbatch)
        self.assertIn("#SBATCH --signal=B:USR1@300", sbatch)
        self.assertIn("--chunk 2", sbatch)
        self.assertIn("apptainer exec --cleanenv --containall", sbatch)

    def test_chunk_grows_to_fit_the_array_limit(self):
        text = testing.LADDER_CAMPAIGN.replace("seed = [0, 1]", "seed = [0, 1, 2, 3, 4, 5]")
        plan = self.plan(text)  # 12 tasks, max_array 4: at least 3 tasks per element
        self.assertEqual(plan.meta["backend"]["chunk"], 3)

    def test_element_longer_than_the_site_allows_is_refused(self):
        self.config.slurm["example-site"].max_time_h = 1
        with self.assertRaises(slurm.SubmitError):
            self.plan(testing.LADDER_CAMPAIGN)

    def test_private_campaign_needs_a_private_ok_site(self):
        text = testing.LADDER_CAMPAIGN.replace('name = "ladder-small"', 'visibility = "private"')
        with self.assertRaises(planning.PlanError):
            self.plan(text)
        self.config.slurm["example-site"].private_ok = True
        self.config.local.private_results_root = str(self.tmp / "private")
        plan = self.plan(text)
        self.assertTrue(plan.private)

    def test_dry_run_launch_builds_the_sbatch_command(self):
        plan = self.plan(testing.LADDER_CAMPAIGN)
        import os

        os.environ["SCRATCH"] = str(self.tmp / "scratch")
        try:
            done = slurm.Slurm().submit(plan, self.config, dry_run=True, say=lambda s: None)
        finally:
            del os.environ["SCRATCH"]
        argv = done[0].record["job"]["argv"]
        self.assertEqual(argv[:3], ["sbatch", "--parsable", "--array=0-1%16"])
        store = self.tmp / "scratch" / "yapnr-store"
        self.assertTrue((store / "campaigns" / plan.id / "submissions" / "1.indices").is_file())
        self.assertTrue((store / "bundles").is_dir())


if __name__ == "__main__":
    unittest.main()
