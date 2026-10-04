"""The slurm backend: golden sbatch, submit.sh and status.sh scripts (bash -n, shellcheck when
installed), the batch script's bounded requeues (run with stub apptainer and scontrol), the
sacct view, chunking against the site's array limit, and the private-campaign guard."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import time
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

    def test_the_campaign_runtime_launches_the_wrapper(self):
        runtime = '\n[runtime]\npython = "/opt/solver/bin/python"\nentrypoint = ""\n'
        plan = self.plan(testing.LADDER_CAMPAIGN + runtime)
        sbatch = (plan.dir / "backend" / "slurm" / "c1m3.sbatch").read_text()
        self.assertIn('"${YAPNR_SIF}" /opt/solver/bin/python\n', sbatch)
        self.assertNotIn(slurm.IMAGE_ENTRYPOINT, sbatch)

    def run_element(self, sbatch, restarts, signal_usr1, code=0):
        """Run the batch script with stub apptainer and scontrol; returns (exit, scontrol call)."""
        stubs = self.tmp / "stubs"
        stubs.mkdir(exist_ok=True)
        started = self.tmp / "started"
        if started.exists():
            started.unlink()
        log = self.tmp / "scontrol.log"
        log.write_text("")
        if signal_usr1:  # a long task that the time-limit signal stops (the wrapper exits 75)
            body = "trap 'exit 75' USR1\ntouch %s\nsleep 20 &\nwait $! || true\n" % started
        else:
            body = "touch %s\n" % started
        (stubs / "apptainer").write_text("#!/bin/bash\n%sexit %d\n" % (body, code))
        (stubs / "scontrol").write_text('#!/bin/bash\necho "$@" >> %s\n' % log)
        for stub in stubs.iterdir():
            stub.chmod(0o755)
        env = dict(
            os.environ,
            PATH="%s:%s" % (stubs, os.environ.get("PATH", "/usr/bin:/bin")),
            YAPNR_STORE=str(self.tmp / "store"),
            YAPNR_SIF=str(self.tmp / "image.sif"),
            YAPNR_SUBMISSION="1",
            SLURM_JOB_ID="42",
            SLURM_ARRAY_JOB_ID="41",
            SLURM_ARRAY_TASK_ID="0",
            SLURM_RESTART_COUNT=str(restarts),
            SLURM_TMPDIR=str(self.tmp / "node"),
        )
        proc = subprocess.Popen(["bash", str(sbatch)], env=env)
        if signal_usr1:
            deadline = time.monotonic() + 20
            while not started.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            time.sleep(0.3)
            proc.send_signal(signal.SIGUSR1)
        return proc.wait(timeout=30), log.read_text().split("\n")[0]

    def test_requeues_are_bounded_by_max_retries(self):
        plan = self.plan(testing.LADDER_CAMPAIGN)
        sbatch = plan.dir / "backend" / "slurm" / "c1m3.sbatch"
        # What submit.sh stages before sbatch: the submission's indices in the store.
        submissions = self.tmp / "store" / "campaigns" / plan.id / "submissions"
        submissions.mkdir(parents=True)
        (submissions / "1.indices").write_text("0\n1\n2\n3\n")
        # The time-limit signal requeues the element until SLURM_RESTART_COUNT reaches 3.
        self.assertEqual(self.run_element(sbatch, 0, True), (0, "requeue 41_0"))
        self.assertEqual(self.run_element(sbatch, 3, True), (75, ""))
        # So does a transient failure (the wrapper's 75); any other exit ends the element.
        self.assertEqual(self.run_element(sbatch, 2, False, code=75), (0, "requeue 41_0"))
        self.assertEqual(self.run_element(sbatch, 3, False, code=75), (75, ""))
        self.assertEqual(self.run_element(sbatch, 0, False, code=0), (0, ""))
        self.assertEqual(self.run_element(sbatch, 0, False, code=2), (2, ""))

    def test_array_view(self):
        live = "41_0|COMPLETED\n41_1|RUNNING\n41_[2-3]|PENDING\n"
        self.assertEqual(
            slurm.array_view(live),
            {
                "state": "RUNNING",
                "counts": {"COMPLETED": 1, "RUNNING": 1, "PENDING": 1},
                "preemptions": 0,
            },
        )
        ended = "41_0|COMPLETED\n41_1|CANCELLED by 1000\n41_2|PREEMPTED\n"
        view = slurm.array_view(ended)
        self.assertEqual((view["state"], view["preemptions"]), ("FINISHED", 1))
        self.assertEqual(slurm.array_view("")["state"], "UNKNOWN")

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

    def test_slots_run_wrappers_side_by_side(self):
        # A site that allocates whole nodes: four one-core tasks per element, one element.
        site = self.config.slurm["example-site"]
        site.slots = 4
        site.extra_sbatch = ["--exclusive"]
        plan = self.plan(testing.LADDER_CAMPAIGN)  # 4 tasks, chunk 2: indices 0 and 1 only
        directory = plan.dir / "backend" / "slurm"
        sbatch = (directory / "c1m3.sbatch").read_text()
        self.assertIn("#SBATCH --cpus-per-task=4\n", sbatch)
        self.assertIn("#SBATCH --mem=12288M\n", sbatch)
        self.assertIn("#SBATCH --exclusive\n", sbatch)
        self.check_shell(directory / "c1m3.sbatch")
        submit = (directory / "submit.sh").read_text()
        self.assertIn("elements=$(((count + 8 - 1) / 8))", submit)
        self.assertIn(
            'APPTAINER_CACHEDIR="${APPTAINER_CACHEDIR:-${store}/.apptainer/cache}"', submit
        )
        submissions = self.tmp / "store" / "campaigns" / plan.id / "submissions"
        submissions.mkdir(parents=True)
        (submissions / "1.indices").write_text("0\n1\n2\n3\n")
        calls = self.tmp / "calls.log"
        stubs = self.tmp / "stubs"
        stubs.mkdir()
        # Each wrapper records its --index (the last argument); index 1 fails transiently (75).
        (stubs / "apptainer").write_text(
            '#!/bin/bash\nfor a in "$@"; do last="$a"; done\n'
            'echo "index ${last}" >> %s\nif [ "${last}" = 1 ]; then exit 75; fi\nexit 0\n' % calls
        )
        (stubs / "scontrol").write_text('#!/bin/bash\necho "scontrol $*" >> %s\n' % calls)
        for stub in stubs.iterdir():
            stub.chmod(0o755)
        env = dict(
            os.environ,
            PATH="%s:%s" % (stubs, os.environ.get("PATH", "/usr/bin:/bin")),
            YAPNR_STORE=str(self.tmp / "store"),
            YAPNR_SIF=str(self.tmp / "image.sif"),
            YAPNR_SUBMISSION="1",
            SLURM_JOB_ID="42",
            SLURM_ARRAY_JOB_ID="41",
            SLURM_ARRAY_TASK_ID="0",
            SLURM_RESTART_COUNT="0",
            SLURM_TMPDIR=str(self.tmp / "node"),
        )
        done = subprocess.run(
            ["bash", str(directory / "c1m3.sbatch")], env=env, timeout=60, capture_output=True
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        lines = sorted(calls.read_text().split("\n")[:-1])
        self.assertEqual(lines, ["index 0", "index 1", "scontrol requeue 41_0"])
        # The submit packs the same way: one element for the four tasks.
        os.environ["SCRATCH"] = str(self.tmp / "scratch")
        try:
            done = slurm.Slurm().submit(plan, self.config, dry_run=True, say=lambda s: None)
        finally:
            del os.environ["SCRATCH"]
        self.assertEqual(done[0].record["job"]["argv"][2], "--array=0-0%16")

    def test_site_speed_scales_the_core_hours(self):
        # The core-hours an allocation request quotes assume the site's speed (default 0.5).
        estimate = self.plan(testing.LADDER_CAMPAIGN).meta["estimate"]
        self.assertIn("speed 0.50 of the reference core", estimate["assumptions"][0])
        self.config.slurm["example-site"].speed = 1.0
        faster = self.plan(testing.LADDER_CAMPAIGN, replace=True).meta["estimate"]
        self.assertIn("speed 1.00 of the reference core", faster["assumptions"][0])
        self.assertGreater(estimate["core_hours"], faster["core_hours"])

    def test_slots_and_speed_are_checked(self):
        from yapnr.exp import config as config_mod

        (self.tmp / "bad").mkdir()
        path = testing.write_config(self.tmp / "bad", "slots = 0\nspeed = 0\n", slurm=True)
        with self.assertRaises(config_mod.ConfigError) as caught:
            config_mod.load(str(path))
        self.assertIn("slots is at least 1", str(caught.exception))
        self.assertIn("speed is above 0", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
