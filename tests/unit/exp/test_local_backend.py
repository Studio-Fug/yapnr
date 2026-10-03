"""The local backend end to end with smoke tasks (no KiCad): plan, submit, status, fetch,
assemble; a running pool blocks a second submit of its tasks; and cancel never signals a process
it did not start."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

from yapnr.exp import fetch
from yapnr.exp import plan as planning
from yapnr.exp import testing
from yapnr.exp.backends import base, local


class LocalBackendTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config = testing.load_config(self.tmp, gcp=False)

    def tearDown(self):
        self._tmp.cleanup()

    def plan(self, text):
        path = testing.write_campaign(self.tmp / "smoke.toml", text)
        return planning.make_plan(path, "local", self.config, offline=True, today=testing.TODAY)

    def test_smoke_campaign_end_to_end(self):
        plan = self.plan(testing.SMOKE_CAMPAIGN)
        self.assertEqual(plan.dir, self.tmp / "store" / "plans" / plan.id)
        backend = local.Local()
        backend.wait = True
        done = backend.submit(plan, self.config, say=lambda s: None)
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0].record["job"]["exit"], 0)
        runs = backend.stores(plan, self.config).runs
        markers = base.done_markers(runs, plan.id)
        self.assertEqual(sorted(markers), ["smoke/0", "smoke/1"])
        self.assertEqual({m["verdict"] for m in markers.values()}, {"done"})
        report = fetch.fetch(runs, plan.id, self.tmp / "fetched", self.config)
        self.assertEqual(report["done"], 2)
        rows = json.loads((self.tmp / "fetched" / plan.id / "assembled" / "smoke.json").read_text())
        self.assertEqual(rows[0]["info"]["python"].split(".")[0], "3")
        # A second submit has nothing left to do.
        self.assertEqual(backend.submit(plan, self.config, say=lambda s: None), [])

    def test_a_running_pool_blocks_a_second_submit_of_its_tasks(self):
        plan = self.plan(testing.SMOKE_CAMPAIGN.replace("sleep_s = 0", "sleep_s = 60"))
        backend = local.Local()
        record = backend.submit(plan, self.config, say=lambda s: None)[0].record
        runs = backend.stores(plan, self.config).runs
        try:
            with self.assertRaises(base.SubmitError) as ctx:
                backend.submit(plan, self.config, say=lambda s: None)
            self.assertIn("holds 2 of these tasks", str(ctx.exception))
            self.assertEqual(len(base.submissions(runs, plan.id)), 1)
        finally:
            backend.cancel(record, self.config)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if backend.state(plan.meta, record, self.config)["state"] != "RUNNING":
                break
            time.sleep(0.2)
        todo = base.pending(plan, base.done_markers(runs, plan.id))
        self.assertEqual(todo, {"c1m1": [0, 1]})  # stopped tasks record nothing
        self.assertEqual(backend.live_overlap(plan, self.config, runs, todo), [])

    def test_missing_toolchain_is_reported_before_anything_runs(self):
        self.config.local.python = None
        plan = self.plan(testing.SMOKE_CAMPAIGN)
        env = {k: v for k, v in os.environ.items() if k != "YAPNR_PYTHON"}
        with unittest.mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(base.SubmitError):
                local.Local().submit(plan, self.config, say=lambda s: None)

    def test_cancel_leaves_unrelated_processes_alone(self):
        sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            record = {
                "campaign": "20261002-smoke-000001",
                "submission": 1,
                "job": {"pid": sleeper.pid},
            }
            message = local.Local().cancel(record, self.config)
            self.assertIn("nothing signalled", message)
            self.assertIsNone(sleeper.poll())
        finally:
            sleeper.kill()
            sleeper.wait()

    def test_toolchain_profile_falls_back_to_the_environment(self):
        self.config.local.kicad_cli = None
        env = {"YAPNR_KICAD_CLI": "/opt/KiCad.app/Contents/MacOS/kicad-cli"}
        profile = local.toolchain_profile(self.config, env)
        self.assertEqual(profile["KICAD_CLI"], env["YAPNR_KICAD_CLI"])
        self.assertEqual(profile["FOOTPRINTS"], "/opt/KiCad.app/Contents/SharedSupport/footprints")


if __name__ == "__main__":
    unittest.main()
