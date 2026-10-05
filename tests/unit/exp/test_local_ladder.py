"""The local backend with a real ladder cell: plan, submit, fetch, assemble, ladder_summary.

Manual and KiCad-tagged: it needs a clean yapnr checkout and an owner config whose [local]
section names a Python with the engine's dependencies and the headless KiCad, and it runs the
engine for about 15 seconds. It is skipped unless both are given:

    YAPNR_EXP_LADDER_REPO=<checkout> YAPNR_CLOUD_CONFIG=<config> \
        python -m unittest tests.unit.exp.test_local_ladder
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from tools.ci import ladder_summary
from yapnr.exp import config, fetch
from yapnr.exp import live as livemod
from yapnr.exp import plan as planning
from yapnr.exp import testing
from yapnr.exp.backends import local

CAMPAIGN = """
schema = "yapnr-campaign-v1"
kind = "ladder-cell"
name = "ladder-one"
source = "HEAD"

[matrix]
case = ["01-connector-led-2"]
seed = [0]

[config]
timeout = 600

[resources]
cpus = 1
memory_gb = 3
max_wall_s = 900
"""

LIVE_CAMPAIGN = CAMPAIGN + '\n[live]\nenabled = true\ninterval_s = 2\nmode = "full"\n'


@unittest.skipUnless(
    os.environ.get("YAPNR_EXP_LADDER_REPO") and os.environ.get("YAPNR_CLOUD_CONFIG"),
    "needs YAPNR_EXP_LADDER_REPO and YAPNR_CLOUD_CONFIG (a [local] toolchain with KiCad)",
)
class LocalLadderTest(unittest.TestCase):
    def test_one_cell_end_to_end(self):
        repo = Path(os.environ["YAPNR_EXP_LADDER_REPO"])
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            cfg = config.load()
            cfg.local.store = str(tmp / "store")
            cfg.local.workers = 1
            campaign = testing.write_campaign(tmp / "ladder-one.toml", CAMPAIGN)
            plan = planning.make_plan(campaign, "local", cfg, repo=repo, offline=True)
            backend = local.Local()
            backend.wait = True
            done = backend.submit(plan, cfg, say=lambda s: None)
            self.assertEqual(done[0].record["job"]["exit"], 0)
            runs = backend.stores(plan, cfg).runs
            report = fetch.fetch(runs, plan.id, tmp / "fetched", cfg)
            self.assertEqual(report["done"], 1)
            run = tmp / "fetched" / plan.id / "assembled" / "default"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(ladder_summary.main([str(run), "--check"]), 0)
            provenance = json.loads((run / "provenance.json").read_text())
            self.assertEqual(provenance["engine_revision"], plan.meta["source"]["commit"])
            self.assertEqual(provenance["shards"][0]["verdict"], "pass")

    def test_one_cell_with_live_on_emits_and_mirrors_real_bundles(self):
        """The real `run.py` ladder cell, through the real `yapnr exp` wrapper, with [live]
        on: this is what the review's finding 1 broke (run.py stripped PNR_LIVE_DIR and
        PNR_LIVE_CANDIDATE before the engine ever saw them, so a ladder campaign emitted no
        live events and `yapnr exp live` mirrored zero bundles even though tasks passed)."""
        repo = Path(os.environ["YAPNR_EXP_LADDER_REPO"])
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            cfg = config.load()
            cfg.local.store = str(tmp / "store")
            cfg.local.workers = 1
            campaign = testing.write_campaign(tmp / "ladder-live.toml", LIVE_CAMPAIGN)
            plan = planning.make_plan(campaign, "local", cfg, repo=repo, offline=True)
            self.assertEqual(plan.meta["live"]["enabled"], True)
            backend = local.Local()
            backend.wait = True
            done = backend.submit(plan, cfg, say=lambda s: None)
            self.assertEqual(done[0].record["job"]["exit"], 0)
            runs = backend.stores(plan, cfg).runs
            bundles = livemod.list_bundles(runs, plan.id)
            self.assertGreaterEqual(len(bundles), 1, "no live bundles: see review finding 1")
            dest = tmp / "mirror"
            found = livemod.mirror_once(runs, plan.id, dest, livemod.LiveState())
            self.assertGreaterEqual(found["events"], 1)
            events = [json.loads(p.read_text()) for p in (dest / "events").glob("*.json")]
            for event in events:
                self.assertEqual(livemod.event_errors(event), [])  # pnr-live-event-v1


if __name__ == "__main__":
    unittest.main()
