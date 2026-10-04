"""``yapnr exp`` on the command line: registered under ``yapnr``, plans offline, reports errors
in one line."""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from yapnr import cli as yapnr_cli
from yapnr.exp import testing


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

    def test_errors_are_one_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = testing.write_config(Path(tmp))
            code, _, err = self.run_cli("exp", "--config", str(cfg), "status", "no-such-campaign")
            self.assertEqual(code, 2)
            self.assertTrue(err.startswith("yapnr exp: no plan directory"), err)
            self.assertNotIn("Traceback", err)

    def test_local_doctor_reports_the_toolchain(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = testing.write_config(Path(tmp))
            code, out, _ = self.run_cli("exp", "--config", str(cfg), "doctor", "--backend", "local")
            self.assertIn("toolchain PYTHON", out)


if __name__ == "__main__":
    unittest.main()
