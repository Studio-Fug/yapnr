"""``yapnr exp`` on the command line: registered under ``yapnr``, plans offline, reports errors
in one line."""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from yapnr import cli as yapnr_cli
from yapnr.exp import config as config_mod
from yapnr.exp import plan as planning
from yapnr.exp import testing
from yapnr.exp.backends import gcp_batch
from yapnr.exp.cloud import FakeCloud


class CliTest(unittest.TestCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = yapnr_cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_plan_and_dry_run_submit(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            cfg = testing.write_config(tmp)
            campaign = testing.write_campaign(tmp / "smoke.toml", testing.SMOKE_CAMPAIGN)
            code, out, err = self.run_cli(
                "exp",
                "--config",
                str(cfg),
                "plan",
                str(campaign),
                "--backend",
                "gcp-batch",
                "--offline",
                "--image-digest",
                testing.DIGEST,
                "--out",
                str(tmp / "plan"),
            )
            self.assertEqual(code, 0, err)
            self.assertIn("estimate  expected $", out)
            self.assertTrue((tmp / "plan" / "backend" / "gcp-batch" / "c1m1.job.json").is_file())
            code, out, err = self.run_cli(
                "exp", "--config", str(cfg), "submit", str(tmp / "plan"), "--dry-run"
            )
            self.assertEqual(code, 0, err)
            self.assertIn("batch jobs submit", out)
            code, out, err = self.run_cli(
                "exp", "--config", str(cfg), "cancel", str(tmp / "plan"), "--dry-run"
            )
            self.assertEqual(code, 0, err)

    def test_plan_lists_the_candidates_and_submit_takes_a_region_pin(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            cfg = testing.write_config(tmp)
            ranking = ", ".join('["%s", "%s"]' % pair for pair in testing.RANKING)
            cfg.write_text(cfg.read_text().replace("[gcp]\n", "[gcp]\nranking = [%s]\n" % ranking))
            text = testing.SMOKE_CAMPAIGN.replace(
                "[config]", testing.RANKED_FAMILIES + "\n[config]"
            )
            campaign = testing.write_campaign(tmp / "smoke.toml", text)
            code, out, err = self.run_cli(
                "exp",
                "--config",
                str(cfg),
                "plan",
                str(campaign),
                "--backend",
                "gcp-batch",
                "--offline",
                "--image-digest",
                testing.DIGEST,
                "--out",
                str(tmp / "plan"),
            )
            self.assertEqual(code, 0, err)
            self.assertRegex(out, r"1\. c4d-highcpu-16 +us-west4 +\$0\.\d{4}/VM-h")
            self.assertRegex(out, r"2\. c4-highcpu-16 +northamerica-northeast1 ")
            self.assertRegex(out, r"3\. c4-highcpu-16 +us-west4 ")
            self.assertIn("spill     submit places each class", out)
            code, out, err = self.run_cli(
                "exp",
                "--config",
                str(cfg),
                "submit",
                str(tmp / "plan"),
                "--dry-run",
                "--region",
                "northamerica-northeast1",
            )
            self.assertEqual(code, 0, err)
            self.assertIn("pinned to northamerica-northeast1", out)
            self.assertIn("--location=northamerica-northeast1", out)

    def test_errors_are_one_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = testing.write_config(Path(tmp))
            code, _, err = self.run_cli("exp", "--config", str(cfg), "status", "no-such-campaign")
            self.assertEqual(code, 2)
            self.assertTrue(err.startswith("yapnr exp: no plan directory"), err)
            self.assertNotIn("Traceback", err)

    def test_logs_follows_a_claim_to_the_task_that_actually_ran_the_line(self):
        # With claims on, Batch starts task indices in no particular order and each task
        # claims whichever line of the indices file it runs: the line's own position is no
        # longer that task's Batch index. `logs` must follow the claim, not use the position.
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            cfg_path = testing.write_config(tmp)
            cfg = config_mod.load(str(cfg_path))
            repo = testing.fixture_repo(tmp)
            campaign = testing.write_campaign(tmp / "smoke.toml", testing.SMOKE_CAMPAIGN)
            plan = planning.make_plan(
                campaign,
                "gcp-batch",
                cfg,
                repo=repo,
                offline=True,
                image_digest=testing.DIGEST,
                today=testing.TODAY,
                out=tmp / "plan",
            )
            cloud = FakeCloud()
            cloud.impersonate = cfg.gcp.submit_email
            gcp_batch.GcpBatch().submit(plan, cfg, cloud=cloud, say=lambda s: None)
            # Line 0 (plan.tasks[0]) was actually run by Batch task index 1, not task index 0.
            claim_key = "gs://%s/campaigns/%s/submissions/1.claims/0" % (
                cfg.gcp.runs_bucket,
                plan.id,
            )
            cloud.objects[claim_key] = b"1"
            with unittest.mock.patch.object(gcp_batch, "make_cloud", return_value=cloud):
                code, out, err = self.run_cli(
                    "exp",
                    "--config",
                    str(cfg_path),
                    "logs",
                    str(tmp / "plan"),
                    plan.tasks[0]["id"],
                )
            self.assertEqual(code, 0, err)
            self.assertIn("Cloud Logging", out)
            queries = [" ".join(call) for call in cloud.calls if call[1:3] == ["logging", "read"]]
            self.assertEqual(len(queries), 1)
            self.assertIn('"-group0-1/"', queries[0])  # the claiming task's index
            self.assertNotIn('"-group0-0/"', queries[0])  # not the line's own position

    def test_local_doctor_reports_the_toolchain(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = testing.write_config(Path(tmp))
            code, out, _ = self.run_cli("exp", "--config", str(cfg), "doctor", "--backend", "local")
            self.assertIn("toolchain PYTHON", out)


if __name__ == "__main__":
    unittest.main()
