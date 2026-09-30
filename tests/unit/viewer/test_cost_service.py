"""Cost replay must never attach a cached score to a different placement phase."""

import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from cost_service import CostService


class CostBindingTest(unittest.TestCase):
    def setUp(self):
        self.repo = Path(
            os.environ.get("PNR_VIEWER_TEST_REPO", Path(__file__).resolve().parents[3])
        )
        self.runtime = Path(os.environ.get("PNR_COST_TEST_RUNTIME", self.repo / "hardware/pnr"))
        sys.path.insert(0, str(self.runtime))
        from pnr.constraints import compile_constraints
        from pnr.graph import BoardGraph, BoardOutline, Component, Net, Pad
        from pnr.place import place

        (self.repo / "output").mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix="cost-binding-", dir=self.repo / "output")
        self.root = Path(self.tmp.name)
        (self.root / "events").mkdir()
        parts = [
            Component(
                "C61",
                "fixture",
                (6, 6),
                0,
                "top",
                (2, 2),
                (2, 2),
                pads=[Pad("1", "V", (0, 0), (0.5, 0.5))],
            ),
            Component(
                "U1",
                "fixture",
                (12, 12),
                0,
                "top",
                (2, 2),
                (2, 2),
                pads=[Pad("1", "V", (0, 0), (0.5, 0.5))],
            ),
        ]
        graph = BoardGraph(
            "binding-test",
            parts,
            [Net("V", 1, [(c.ref, "1") for c in parts])],
            BoardOutline(20, 20),
        )
        cc = compile_constraints({"board": {"width_mm": 20, "height_mm": 20}}, graph.refs)
        with patch.dict(
            os.environ, {"PNR_COST_CAPTURE_DIR": str(self.root / "records"), "PNR_LIVE_DIR": ""}
        ):
            place(graph, cc, seed=0, iters=8)
        self.record = next(
            p
            for p in (self.root / "records").glob("*.json")
            if json.loads(p.read_text())["kind"] == "global-objective"
        )
        self.service = CostService(
            self.root, self.repo, Path(__file__).parent, runtime=self.runtime
        )
        self.capture = dict(
            path=str(self.record), sha256=hashlib.sha256(self.record.read_bytes()).hexdigest()
        )
        self.graph = json.loads(self.record.read_text())["graph"]

    def tearDown(self):
        self.service.pool.shutdown(wait=True)
        self.tmp.cleanup()

    def event(self, event_id, graph=None):
        e = dict(
            id=event_id,
            kind="placement_cost_capture",
            candidate="test",
            layout=graph or self.graph,
            data=dict(cost_capture=self.capture),
        )
        (self.root / "events" / (event_id + ".json")).write_text(json.dumps(e))
        return e

    def wait(self, id):
        end = time.monotonic() + 20
        while time.monotonic() < end:
            result = self.service.request(id, "C61")
            if result["status"] not in ("pending", "busy"):
                return result
            time.sleep(0.02)
        self.fail("cost service did not finish")

    def test_unevaluated_probe_component_is_explicitly_unavailable(self):
        d = json.loads(self.record.read_text())
        d.update(kind="routing-probe", component={"ref": "C61"}, components={"C61": {"ref": "C61"}})
        self.record.write_text(json.dumps(d))
        self.capture["sha256"] = hashlib.sha256(self.record.read_bytes()).hexdigest()
        self.event("7-abc")
        answer = self.service.request("7-abc", "U19")
        self.assertEqual(answer["status"], "unavailable")
        self.assertIn("not evaluated", answer["reason"])
        self.assertEqual(self.service.pending, {})

    def test_different_layout_cannot_reuse_cached_capture(self):
        self.event("1-abc")
        a = self.wait("1-abc")
        self.assertEqual(a["status"], "ready", a)
        bad = json.loads(json.dumps(self.graph))
        bad["components"][0]["pos"][0] += 0.1
        self.event("2-abc", bad)
        b = self.wait("2-abc")
        self.assertEqual(b["status"], "error", b)

    def test_changed_capture_is_rejected(self):
        self.capture["sha256"] = "0" * 64
        self.event("3-abc")
        with self.assertRaisesRegex(ValueError, "Capture hash mismatch"):
            self.service.request("3-abc", "C61")

    def test_missing_component_returns_explicit_unavailable(self):
        self.event("4-abc")
        self.assertEqual(self.service.request("4-abc")["status"], "unavailable")

    def test_changed_probe_source_refuses_cache_before_replay(self):
        d = json.loads(self.record.read_text())
        d["probe_sources"] = {"relocate.py": "0" * 64}
        self.record.write_text(json.dumps(d))
        self.capture["sha256"] = hashlib.sha256(self.record.read_bytes()).hexdigest()
        self.event("5-abc")
        with self.assertRaisesRegex(ValueError, "Routing-probe runtime changed"):
            self.service.request("5-abc", "C61")

    def test_probe_source_path_is_restricted(self):
        d = json.loads(self.record.read_text())
        d["probe_sources"] = {"../graph.py": "0" * 64}
        self.record.write_text(json.dumps(d))
        self.capture["sha256"] = hashlib.sha256(self.record.read_bytes()).hexdigest()
        self.event("6-abc")
        with self.assertRaisesRegex(ValueError, "Invalid routing-probe source"):
            self.service.request("6-abc", "C61")

    def test_record_requires_matching_replay_model(self):
        d = json.loads(self.record.read_text())
        self.assertEqual(d["runtime_sources"]["cost_inspect.py"], self.service.model_hash)


if __name__ == "__main__":
    unittest.main()
