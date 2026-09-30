import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from cost_fixture import fixture

from pnr.place import place


class Capture(unittest.TestCase):
    def test_capture_does_not_change_placement_and_reconciles_objectives(self):
        g, cc = fixture()
        kw = dict(seed=17, iters=80)
        with patch.dict(os.environ, {"PNR_COST_CAPTURE_DIR": ""}):
            base, report = place(g, cc, **kw)
        with tempfile.TemporaryDirectory() as td, patch.dict(
            os.environ, {"PNR_COST_CAPTURE_DIR": td}
        ):
            actual, rep = place(g, cc, **kw)
            self.assertEqual(actual.to_json(), base.to_json())
            docs = [json.loads(p.read_text()) for p in Path(td).glob("*.json")]
            global_records = [d for d in docs if d["kind"] == "global-objective"]
            self.assertEqual(len(global_records), 1)
            record = global_records[0]
            self.assertLess(abs(record["report"]["replay_error"]), 0.005)
            self.assertEqual(
                record["report"]["scope"], "recorded-optimizer-soft-state-before-update"
            )
            decisions = [d for d in docs if d["kind"] == "legalizer-decision"]
            self.assertEqual(len(decisions), 1)
            d = decisions[0]
            self.assertAlmostEqual(d["total"], sum(t["weighted"] for t in d["terms"]), places=8)
            self.assertEqual(tuple(d["position"]), actual.component(d["ref"]).pos)
            field = next(f for f in d["candidate_fields"] if f["rotation"] == d["rotation"])
            v = np.load(field["path"])["values"]
            i = np.argmin(((v[:, :2] - d["position"]) ** 2).sum(1))
            self.assertLess(np.linalg.norm(v[i, :2] - d["position"]), 1e-8)
            self.assertAlmostEqual(d["total"], float(v[i, 2] + 25 * v[i, 3] + v[i, 4]), places=7)


if __name__ == "__main__":
    unittest.main()
