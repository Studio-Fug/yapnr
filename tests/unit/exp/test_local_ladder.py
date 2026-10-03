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


if __name__ == "__main__":
    unittest.main()
